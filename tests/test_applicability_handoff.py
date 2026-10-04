"""Synthetic component checks for actual-input permissions and complete guidance.

Visibility fixtures represent the trusted host boundary, not model claims.
The actual SDK and wire integration are exercised in separate test modules.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest
from test_applicability import packet, scenario
from test_knowledge_retrieval import inventory

from dfx_agent import actions, applicability_handoff
from dfx_agent.actions import ToolActionStore
from dfx_agent.applicability_handoff import (
    APPLICABILITY_TOOLS,
    ApplicabilityPolicy,
    applicability_tools,
    audit_stored_applicability,
    context_result,
    normalize_applicability_profile,
    review_result,
)
from dfx_agent.knowledge_contracts import encode
from dfx_agent.knowledge_handoff import experience_result
from dfx_agent.model_calls import ModelCallStore, canonical, sha256


class Visibility:
    def __init__(self, view: dict[str, Any]):
        self.view = view
        self.parents: list[str] = []

    def read(self, parent: str, model_calls: ModelCallStore, action_store: ToolActionStore):
        assert parent == "model-1"
        self.parents.append(parent)
        return copy.deepcopy(self.view)


def seen(name: str, answer: dict[str, Any], args: dict[str, Any] | None = None):
    return {"action": {"tool_name": name, "authorization": "allowed", "action_id": "action-1"},
            "tool_call": {"name": name, "args": args or {}, "id": "tool-1"},
            "receipt": {"content": encode(answer), "status": "success"}}


def services(s, *, enabled: bool = True, focus: bool = True):
    assert s.bound is not None
    value = (packet(s) if s.context is not None else
             applicability_handoff.build_working_context(s.workspace, s.focus, task_directory=s.task,
                 ledger=s.ledger, max_bytes=applicability_handoff.MAX_CONTEXT_BYTES).contents())
    visibility = Visibility({"focus": s.focus.model_dump(mode="json") if focus else {},
        "visible_packet": value if focus else None,
        "visible_receipts": [seen("search_experience", experience_result(s.store, s.bound.record_id))]})
    call_store = ModelCallStore(s.workspace.root / "model-calls.sqlite")
    action_store = ToolActionStore(s.workspace.root / "tool-actions.sqlite")
    tools = {item.name: item for item in applicability_tools(s.store, visibility, call_store, action_store,
                                                           ApplicabilityPolicy(enabled=enabled))}
    return visibility, tools


def invoke(tool, args):
    token = actions._ACTIVE_SOLVER_CALL.set("model-1")
    try:
        return tool.invoke(args)
    finally:
        actions._ACTIVE_SOLVER_CALL.reset(token)


def queried(tmp_path: Path):
    s = scenario(tmp_path, prepared=False)
    s.store.bind()
    s.bound = s.store.record_query(tags=("ByteWidth",), text="容量")
    return s


@pytest.mark.parametrize("value", [{}, {"enabled": True}, {"schema_version": True, "enabled": True},
    {"schema_version": 1, "enabled": 1}, {"schema_version": 2, "enabled": True},
    {"schema_version": 1, "enabled": True, "scope": "other"}])
def test_profile_requires_complete_strict_canonical_identity(value) -> None:
    with pytest.raises(ValueError):
        normalize_applicability_profile(value)
    assert normalize_applicability_profile({"schema_version": 1, "enabled": False}) == {
        "schema_version": 1, "enabled": False}


def test_tool_schema_is_identical_with_mechanism_on_off_and_uninitialized_ledgers(tmp_path: Path) -> None:
    s = queried(tmp_path)
    visibility, tools = services(s)
    off = applicability_tools(s.store, visibility, None, None, ApplicabilityPolicy(enabled=False))
    assert set(tools) == APPLICABILITY_TOOLS
    assert {item.name: item.args_schema.model_json_schema() for item in off} == {
        name: item.args_schema.model_json_schema() for name, item in tools.items()}
    assert off[0].invoke({"query_id": s.bound.record_id}) == "rejected: applicability_source_invalid"
    assert set(tools["prepare_experience_context"].args_schema.model_fields) == {"query_id", "proposal_json"}
    assert set(tools["review_experience"].args_schema.model_fields) == {"applicability_json", "proposal_json"}


def test_active_parent_is_host_owned_and_required(tmp_path: Path) -> None:
    s = queried(tmp_path)
    visibility, tools = services(s)
    before = inventory(tmp_path)
    assert tools["prepare_experience_context"].invoke({"query_id": s.bound.record_id}) == (
        "rejected: applicability_source_invalid")
    assert visibility.parents == [] and inventory(tmp_path) == before


def test_prepare_returns_complete_frozen_context_and_guidance(tmp_path: Path) -> None:
    s = queried(tmp_path)
    visibility, tools = services(s)
    result = json.loads(invoke(tools["prepare_experience_context"], {"query_id": s.bound.record_id}))
    context = result["context"]
    assert result == context_result(s.store, context["context_id"])
    assert result["review_proposal_schema"] == applicability_handoff.ApplicabilityProposal.model_json_schema()
    assert result["guidance"]["retrieval"]["hits"][0]["projection"]["guidance"]["procedure"]
    assert result["guidance"]["retrieval"]["hits"][0]["projection"]["guidance"]["counterexamples"]
    assert visibility.parents == ["model-1"]
    assert s.ledger.executor.requests == []


def test_unseen_query_and_no_focus_never_select_latest_registered_diagnosis(tmp_path: Path) -> None:
    s = queried(tmp_path)
    visibility, tools = services(s, focus=False)
    before = inventory(tmp_path)
    visibility.view["visible_receipts"] = []
    assert invoke(tools["prepare_experience_context"], {"query_id": s.bound.record_id}) == (
        "rejected: applicability_source_invalid")
    visibility.view["visible_receipts"] = [seen("search_experience", experience_result(s.store, s.bound.record_id))]
    unknown = json.loads(invoke(tools["prepare_experience_context"], {"query_id": s.bound.record_id}))
    assert unknown["status"] == "unknown" and unknown["reason"] == "no_visible_focus"
    assert unknown["next_action"] == "ordinary_retrieval"
    assert inventory(tmp_path) == before and not (s.workspace.root / "applicability-contexts").exists()


@pytest.mark.parametrize("statuses,expected,next_action", [
    (("supported", "supported"), "supported", "offer_process"),
    (("contradicted", "unknown"), "contradicted", "avoid_copy"),
    (("unknown", "supported"), "unknown", "clarify"),
])
def test_review_preserves_process_questions_and_model_inference(tmp_path: Path, statuses, expected, next_action) -> None:
    s = scenario(tmp_path)
    visibility, tools = services(s)
    visibility.view["visible_receipts"].append(seen("prepare_experience_context", context_result(s.store, s.context.context_id)))
    result = json.loads(invoke(tools["review_experience"], {"applicability_json": s.proposal(statuses).model_dump_json()}))
    contents = result["guidance"]
    assert contents["status"] == expected and contents["next_action"] == next_action
    assert contents["semantic_authority"] == "model_inference"
    assert contents["application_verification"] == "not_run"
    assert contents["projection"]["guidance"]["procedure"]
    expected_questions = [premise["unknown_question"] for premise, status in
                          zip(contents["projection"]["guidance"]["premises"], statuses, strict=True)
                          if status == "unknown"]
    assert [question["question"] for question in contents["questions"]] == expected_questions
    visibility.view["visible_receipts"].append(seen("review_experience", result))
    assert json.loads(invoke(tools["read_experience_review"], {"review_id": result["review"]["record_id"]})) == result
    assert s.ledger.executor.requests == []


@pytest.mark.parametrize("fault", ["context-unseen", "context-incomplete", "changed-hypothesis", "unseen-evidence"])
def test_review_rejects_sibling_unseen_incomplete_or_different_focus(tmp_path: Path, fault: str) -> None:
    s = scenario(tmp_path)
    visibility, tools = services(s)
    result = context_result(s.store, s.context.context_id)
    if fault != "context-unseen":
        if fault == "context-incomplete":
            result["guidance"]["retrieval"]["hits"][0]["projection"]["guidance"]["premises"].clear()
        visibility.view["visible_receipts"].append(seen("prepare_experience_context", result))
    if fault == "changed-hypothesis":
        visibility.view["focus"]["hypothesis_ids"] = ["other-hypothesis"]
    proposal = s.proposal(evidence_id="not-visible" if fault == "unseen-evidence" else "source1")
    before = inventory(tmp_path)
    assert invoke(tools["review_experience"], {"applicability_json": proposal.model_dump_json()}) == (
        "rejected: applicability_source_invalid")
    assert inventory(tmp_path) == before and not (s.workspace.root / "applicability-reviews").exists()


def test_cross_candidate_context_cannot_be_read_as_current(tmp_path: Path) -> None:
    s = scenario(tmp_path, candidates=True)
    visibility, tools = services(s)
    result = review_result(s.store, s.store.review(s.proposal()).record_id)
    visibility.view["visible_receipts"].append(seen("review_experience", result))
    visibility.view["focus"]["current_candidate_id"] = s.old
    before = inventory(tmp_path)
    assert invoke(tools["read_experience_review"], {"review_id": result["review"]["record_id"]}) == (
        "rejected: applicability_source_invalid")
    assert inventory(tmp_path) == before


def test_later_sibling_check_cannot_enter_prepared_context(tmp_path: Path) -> None:
    s = scenario(tmp_path, candidates=True)
    _visibility, tools = services(s)
    s.ledger.run(s.current)
    requests_before = len(s.ledger.executor.requests)
    before = inventory(tmp_path)
    assert invoke(tools["prepare_experience_context"], {"query_id": s.bound.record_id}) == (
        "rejected: applicability_source_invalid")
    assert inventory(tmp_path) == before and len(s.ledger.executor.requests) == requests_before


def test_disabled_tools_read_same_knowledge_without_scoring_or_publication(tmp_path: Path) -> None:
    s = queried(tmp_path)
    _, tools = services(s, enabled=False)
    before = inventory(tmp_path)
    results = [invoke(tools["prepare_experience_context"], {"query_id": s.bound.record_id}),
               invoke(tools["review_experience"], {"applicability_json": "no scoring requested"}),
               invoke(tools["read_experience_review"], {"review_id": "not a three-state record"})]
    for raw in results:
        result = json.loads(raw)
        assert result["status"] == "disabled" and result["next_action"] == "ordinary_retrieval"
        assert result["guidance"] == experience_result(s.store, s.bound.record_id)
    assert inventory(tmp_path) == before and s.ledger.executor.requests == []


def test_repeated_read_links_do_not_repeat_large_focus_or_visible_history() -> None:
    focus = {"diagnosis_id": "diagnosis-1", "hypothesis_ids": [f"h-{number:04d}-" + "x" * 120
        for number in range(4096)]}
    identifiers = ["action-" + sha256(str(number)) for number in range(300)]
    view = {"parent_call_id": "model-4", "request_sha256": sha256("accounted request"),
        "final_view_messages_json": canonical([{"complete": "sent history"}]), "focus": focus,
        "visible_receipts": [{"action": {"action_id": identifier}} for identifier in identifiers]}
    before = copy.deepcopy(view)
    source = applicability_handoff._source(view)
    assert source["visible_action_count"] == 300
    assert source["visible_actions_sha256"] == sha256(canonical(identifiers))
    assert source["focus_sha256"] == sha256(canonical(focus))
    assert "solver_intent" not in source and "focus" not in source and "visible_action_ids" not in source
    link = {"action_id": "action-" + sha256("read"), "model_call_id": "model-4",
        "tool_name": "read_experience_review", "state": "completed", "authorization": "allowed",
        "receipt_sha256": sha256("complete receipt")}
    nested = {**link, "solver_source": source}
    # Include both top-level action links and their nested context/review
    # locations, then both complete report placements with actual indentation.
    one = {"actions": [link], "reviews": [{"actions": [nested]}]}
    wrapper = {"applicability_facts": one, "completion": {"applicability_facts": one}}
    assert len(json.dumps(wrapper, ensure_ascii=True, indent=2).encode("utf-8")) < 4096
    repeated = {"actions": [link] * 600, "reviews": [{"actions": [nested] * 600}]}
    applicability_handoff._require_report_fragment_capacity(repeated)
    assert view == before
    view["focus"]["hypothesis_ids"][-1] = "changed-visible-hypothesis"
    assert applicability_handoff._source(view)["focus_sha256"] != source["focus_sha256"]
    view["visible_receipts"].reverse()
    assert applicability_handoff._source(view)["visible_actions_sha256"] != source["visible_actions_sha256"]
    view["visible_receipts"][0]["action"]["action_id"] = "changed-action"
    assert applicability_handoff._source(view)["visible_actions_sha256"] != source["visible_actions_sha256"]


def test_final_complete_report_fragment_has_an_exact_capacity_gate(monkeypatch) -> None:
    facts = {"actions": [{"unchanged": "完整原件"}], "budget_rejections": []}
    fragment = {"applicability_facts": facts, "completion": {"applicability_facts": facts}}
    size = len(json.dumps(fragment, ensure_ascii=True, indent=2).encode("utf-8"))
    monkeypatch.setattr(applicability_handoff, "MAX_APPLICABILITY_REPORT_BYTES", size)
    applicability_handoff._require_report_fragment_capacity(facts)
    monkeypatch.setattr(applicability_handoff, "MAX_APPLICABILITY_REPORT_BYTES", size - 1)
    with pytest.raises(ValueError, match="report fragments exceed capacity"):
        applicability_handoff._require_report_fragment_capacity(facts)


@pytest.mark.parametrize("namespace", ["solver-inputs", "applicability-contexts", "applicability-reviews", "applicability-materials"])
def test_legacy_configuration_refuses_new_namespaces_without_model_ledgers(tmp_path: Path, namespace: str) -> None:
    s = queried(tmp_path)
    (s.workspace.root / namespace).mkdir()
    before = inventory(tmp_path)
    with pytest.raises(ValueError):
        audit_stored_applicability(s.workspace, s.task, s.ledger, None, None)
    assert inventory(tmp_path) == before


@pytest.mark.parametrize("operation", ["context", "review"])
def test_ascii_duplicate_report_capacity_rejects_before_first_publication(tmp_path: Path, monkeypatch, operation: str) -> None:
    s = queried(tmp_path) if operation == "context" else scenario(tmp_path)
    visibility, tools = services(s)
    if operation == "review":
        visibility.view["visible_receipts"].append(seen("prepare_experience_context", context_result(s.store, s.context.context_id)))
    before = inventory(tmp_path)
    monkeypatch.setattr(applicability_handoff, "MAX_APPLICABILITY_REPORT_BYTES", 1)
    name, args = (("prepare_experience_context", {"query_id": s.bound.record_id}) if operation == "context" else
                  ("review_experience", {"applicability_json": s.proposal().model_dump_json()}))
    assert invoke(tools[name], args) == "rejected: applicability_source_invalid"
    assert inventory(tmp_path) == before and s.ledger.executor.requests == []
