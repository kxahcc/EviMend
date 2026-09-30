"""SDK tool authorization must precede effects and survive checkpoint failures."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from test_agent_loop import candidate_id, response, task_snapshot

from dfx_agent.actions import ToolActionStore
from dfx_agent.agent import run_fixed_agent
from dfx_agent.contracts import ActionProposal
from dfx_agent.decisions import DecisionRequest
from dfx_agent.policies import PolicyFeatures, PolicyInput, RepairDirection
from dfx_agent.task import digest, verify_task
from dfx_agent.verification import FixedTextExecutor
from dfx_agent.workspace import CandidateWorkspace

EDIT = {"path": "src/main.ets", "old": "bad", "new": "good"}


def edit_proposal(task: Path, **updates: object) -> str:
    payload = {"record_id": "model-proposal", "created_at": datetime.now(UTC),
               "task_id": "task1", "run_id": "run1",
               "source_snapshot_sha256": verify_task(task)["source_snapshot_sha256"],
               "action": "edit_draft", "required_capability": "edit_source",
               "reason": "Replace the synthetic bad value", "estimated_cost_basis": "one edit"}
    return ActionProposal.model_validate({**payload, **updates}).model_dump_json()


@pytest.mark.parametrize(("updates", "reason"), [
    ({"source_snapshot_sha256": digest(b"another version")}, "wrong_task_or_version"),
    ({"run_id": "other_run"}, "wrong_task_or_version"),
    ({"evidence_ids": ["missing_fact"]}, "unknown_reference"),
    ({"target_ids": ["missing_hypothesis"]}, "unknown_reference"),
    ({"action": "record_analysis", "required_capability": "record_analysis"}, "invalid_proposal_or_arguments"),
])
def test_explicit_proposal_rejection_never_creates_a_draft(tmp_path: Path, updates: dict, reason: str) -> None:
    task = task_snapshot(tmp_path)
    executor = FixedTextExecutor("src/main.ets", "value=good\n")
    report = run_fixed_agent(task, tmp_path / "run", "run1", [
        response(1, "edit_source", {**EDIT, "proposal_json": edit_proposal(task, **updates)}), response(2),
    ], executor)
    assert report["actions"][0]["rejection"] == reason
    assert not (tmp_path / "run/draft").exists()
    assert report["candidates"] == report["checks"] == [] and executor.calls == []
    assert report["application_verification"] == "not_run"


def test_host_capability_and_unknown_candidate_rejections_have_no_effects(tmp_path: Path) -> None:
    task = task_snapshot(tmp_path)
    executor = FixedTextExecutor("src/main.ets", "value=good\n")
    report = run_fixed_agent(task, tmp_path / "run", "run1", [
        response(1, "edit_source", EDIT),
        response(2, "run_check", {"candidate_id": digest(b"not registered")}), response(3),
    ], executor, capabilities=frozenset({"read_source"}))
    assert [item["rejection"] for item in report["actions"]] == ["capability_unavailable", "candidate_not_registered"]
    assert not (tmp_path / "run/draft").exists()
    assert list((tmp_path / "run/checks").iterdir()) == [] and executor.requests == []


def test_tool_budget_rejects_before_edit_and_resume_cannot_increase_it(tmp_path: Path) -> None:
    task = task_snapshot(tmp_path)
    executor = FixedTextExecutor("src/main.ets", "value=good\n")
    scripted = [response(1, "read_log"), response(2, "edit_source", EDIT), response(3)]
    report = run_fixed_agent(task, tmp_path / "run", "run1", scripted, executor, max_tool_calls=1)
    assert report["status"] == "budget_exhausted" and report["rejected_tool_calls"] == 1
    assert not (tmp_path / "run/draft").exists()
    with pytest.raises(ValueError, match="session identity"):
        run_fixed_agent(task, tmp_path / "run", "run1", scripted, executor, resume=True, max_tool_calls=2)
    with pytest.raises(ValueError, match="session identity"):
        run_fixed_agent(task, tmp_path / "run", "run1", scripted, executor, resume=True,
                        max_tool_calls=1, capabilities=frozenset({"read_log"}))


def test_last_paid_model_response_can_complete_its_action(tmp_path: Path) -> None:
    task = task_snapshot(tmp_path)
    report = run_fixed_agent(task, tmp_path / "run", "run1", [
        response(1, "edit_source", {**EDIT, "proposal_json": edit_proposal(task)}),
    ],
                             FixedTextExecutor("src/main.ets", "value=good\n"), max_calls=1)
    assert report["model_calls"] == 1 and report["status"] == "budget_exhausted"
    assert report["actions"][0]["authorization"] == "allowed"
    assert report["actions"][0]["proposal"]["record_id"] == report["actions"][0]["action_id"]
    assert (tmp_path / "run/draft/src/main.ets").read_text() == "value=good\n"


def test_candidate_budget_gate_precedes_second_registration(tmp_path: Path) -> None:
    task = task_snapshot(tmp_path)
    report = run_fixed_agent(task, tmp_path / "run", "run1", [
        response(1, "edit_source", EDIT), response(2, "register_candidate"),
        response(3, "edit_source", {**EDIT, "new": "another"}), response(4, "register_candidate"), response(5),
    ], FixedTextExecutor("src/main.ets", "value=good\n"), max_candidates=1)
    assert len(report["candidates"]) == 1
    assert report["actions"][-1]["rejection"] == "budget_exhausted"
    assert report["checks"] == []


def test_all_offline_tools_pass_through_authorization(tmp_path: Path) -> None:
    task = task_snapshot(tmp_path)
    almost = candidate_id(task, tmp_path / "derive", "almost")
    plan = PolicyInput(source_snapshot_sha256=verify_task(task)["source_snapshot_sha256"],
                       directions=[RepairDirection(direction_id="direction1", repair_key="repair1",
                                                   acceptance_key="accept1", hypothesis_ids=["hypothesis-1"],
                                                   support_evidence_ids=["inference-1"])], remaining_probe_budget=0)
    request = DecisionRequest(task_id="task1", run_id="run1", diagnosis_id="diagnosis-1", plan=plan)
    calls = [("read_log", {}), ("list_source", {}), ("read_source", {"path": "src/main.ets"}),
             ("record_diagnosis", {"path": "src/main.ets", "line": 1, "reason": "bad value"}),
             ("record_obligation", {"obligation_id": "preserve_value", "statement": "Keep the expected value",
                                    "evidence_ids": ["inference-1"]}),
             ("edit_source", {**EDIT, "new": "almost"}),
             ("register_candidate", {"diagnosis_id": "diagnosis-1", "restores": ["preserve_value"]}),
             ("run_check", {"candidate_id": almost}),
             ("link_failed_check", {"candidate_id": almost, "violated_obligation_ids": ["preserve_value"]}),
             ("consult_rule", {"family": "bounds"}), ("plan_repair_action", {"plan_json": request.model_dump_json()}),
             ("inspect_analysis", {"diagnosis_id": "diagnosis-1", "include_schema": True})]
    scripted = [response(index, name, args) for index, (name, args) in enumerate(calls, start=1)] + [response(13)]
    report = run_fixed_agent(task, tmp_path / "run", "run1", scripted,
                             FixedTextExecutor("src/main.ets", "value=good\n"),
                             policy_features=PolicyFeatures(rule_guidance=True, obligation_tracking=True,
                                                            decision_checks=True))
    assert report["tool_calls"] == 12 and report["rejected_tool_calls"] == report["uncertain_tool_calls"] == 0
    assert {item["tool_name"] for item in report["actions"]} == set(report["offered_tools"])
    assert report["checks"][0]["status"] == "fail" and report["application_verification"] == "not_run"
    feedback = json.loads(next((tmp_path / "run/feedback").glob("*.json")).read_text())
    assert feedback["scope"] == "synthetic" and feedback["link_authority"] == "model_inference"


@pytest.mark.parametrize("receipt_saved", [True, False])
def test_tool_checkpoint_crash_does_not_repeat_edit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                  receipt_saved: bool) -> None:
    task = task_snapshot(tmp_path)
    executor = FixedTextExecutor("src/main.ets", "value=good\n")
    work = tmp_path / "run"
    scripted = [response(1, "edit_source", EDIT), response(2)]
    complete = ToolActionStore.complete
    apply = CandidateWorkspace.apply_text_edits
    edits = 0

    def observe_edit(self, changes):
        nonlocal edits
        edits += 1
        return apply(self, changes)

    def crash(self, action_id, message):
        if receipt_saved:
            complete(self, action_id, message)
        raise RuntimeError("synthetic tool checkpoint crash")

    monkeypatch.setattr(CandidateWorkspace, "apply_text_edits", observe_edit)
    monkeypatch.setattr(ToolActionStore, "complete", crash)
    with pytest.raises(RuntimeError, match="synthetic tool checkpoint crash"):
        run_fixed_agent(task, work, "run1", scripted, executor)
    monkeypatch.setattr(ToolActionStore, "complete", complete)
    report = run_fixed_agent(task, work, "run1", scripted, executor, resume=True)
    assert edits == 1 and (work / "draft/src/main.ets").read_text() == "value=good\n"
    assert report["tool_calls"] == 1 and executor.requests == []
    if receipt_saved:
        assert report["uncertain_tool_calls"] == 0 and report["model_calls"] == 2
        assert report["rejected_tool_calls"] == 0
    else:
        assert report["uncertain_tool_calls"] == 1 and report["model_calls"] == 1
        assert report["status"] == "unresolved" and report["stopped_reason"] == "action_result_unknown"


def test_reused_provider_tool_id_in_another_response_is_a_new_action(tmp_path: Path) -> None:
    task = task_snapshot(tmp_path)
    second = AIMessage(content="", tool_calls=[{"name": "edit_source", "id": "call-1",
                                               "args": {"path": "src/main.ets", "old": "almost", "new": "good"}}],
                       usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
                       response_metadata={"dfx_model_call_id": "model-1"})
    report = run_fixed_agent(task, tmp_path / "run", "run1", [
        response(1, "edit_source", {**EDIT, "new": "almost"}), second, response(3),
    ], FixedTextExecutor("src/main.ets", "value=good\n"))
    assert report["tool_calls"] == 2 and report["rejected_tool_calls"] == 0
    assert len({item["action_id"] for item in report["actions"]}) == 2
    assert [item["model_call_id"] for item in report["actions"]] == ["model-1", "model-2"]
    assert (tmp_path / "run/draft/src/main.ets").read_text() == "value=good\n"


def test_tool_batch_is_serial_and_shares_the_action_budget(tmp_path: Path) -> None:
    task = task_snapshot(tmp_path)
    batch = AIMessage(content="", tool_calls=[
        {"name": "edit_source", "id": "first", "args": {**EDIT, "new": "almost"}},
        {"name": "edit_source", "id": "second", "args": {"path": "src/main.ets", "old": "almost", "new": "good"}},
    ], usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2})
    report = run_fixed_agent(task, tmp_path / "run", "run1", [batch, response(2)],
                             FixedTextExecutor("src/main.ets", "value=good\n"), max_tool_calls=1)
    assert [item["rejection"] for item in report["actions"]] == [None, "budget_exhausted"]
    assert (tmp_path / "run/draft/src/main.ets").read_text() == "value=almost\n"


def test_tool_receipt_rejects_same_identity_with_changed_arguments(tmp_path: Path) -> None:
    store = ToolActionStore(tmp_path / "actions.sqlite")
    effects = 0

    def effect() -> ToolMessage:
        nonlocal effects
        effects += 1
        return ToolMessage(content="edited", name="edit_source", tool_call_id="call1")

    parameters = {"action_id": "action1", "model_call_id": "model-1", "tool_name": "edit_source",
                  "tool_call_id": "call1", "proposal": None, "rejection": None, "max_tool_calls": 1,
                  "handler": effect}
    first = store.invoke(**parameters, request_sha256=digest(b"args1"))
    assert store.invoke(**parameters, request_sha256=digest(b"args1")) == first
    with pytest.raises(ValueError, match="identity changed"):
        store.invoke(**parameters, request_sha256=digest(b"args2"))
    assert effects == 1
