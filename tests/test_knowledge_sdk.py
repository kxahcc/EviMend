"""Complete approved guidance travels through the real SDK with synthetic models."""

from __future__ import annotations

import json
import os
import sqlite3
import stat
from contextlib import closing
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from test_agent_loop import candidate_id, response
from test_knowledge_reporting import manifest3, open_run
from test_knowledge_retrieval import admit, guidance
from test_knowledge_run import inputs
from test_metrics import terminal
from test_provider_loop import scripted_adapter, wire_response
from test_readonly_evidence import tree_bytes

from dfx_agent import knowledge_handoff, models
from dfx_agent.actions import ToolActionStore
from dfx_agent.agent import ResumeAdmissionRejected, run_fixed_agent, run_provider_agent
from dfx_agent.applicability_contracts import BoundQueryPayload, TaskKnowledgeBinding
from dfx_agent.knowledge_contracts import ExperienceContent, content_hash, encode
from dfx_agent.knowledge_handoff import experience_tools
from dfx_agent.knowledge_run import HostKnowledgeInput
from dfx_agent.metrics import aggregate_evaluation, parse_evaluation_manifest
from dfx_agent.model_calls import (
    ModelCallIntent,
    ModelCallStore,
    canonical,
    normalize_message,
    sha256,
)
from dfx_agent.reporting import build_completion, read_completion
from dfx_agent.retrieval_contracts import MemoryQuery
from dfx_agent.verification import CheckLedger, FixedTextExecutor
from dfx_agent.working_context_plan import WorkingContextPolicy
from dfx_agent.workspace import CandidateWorkspace


def expected_query(host, *, tags=("ByteWidth",), text="") -> str:
    record = host.retriever.query(MemoryQuery(snapshot_sha256=host.approval.snapshot_sha256,
        scope=host.approval.scope, tags=tags, text=text))
    payload = BoundQueryPayload(binding_sha256=content_hash(host.approval), retrieval_id=record.record_id,
                                retrieval_sha256=record.content_sha256)
    return "bound-query-" + content_hash(payload)


def test_complete_guidance_is_sent_then_used_in_the_existing_repair_loop(tmp_path: Path, monkeypatch) -> None:
    data = inputs(tmp_path)
    query_id = expected_query(data.host)
    good = candidate_id(data.task, tmp_path / "derive", "good")
    observed = []
    original = models.FixedResponseModel._intent

    def observe(self, messages, stop, kwargs):
        observed.append(list(messages))
        return original(self, messages, stop, kwargs)

    monkeypatch.setattr(models.FixedResponseModel, "_intent", observe)
    external_before = tree_bytes(data.source.store.root)
    work = tmp_path / "sdk-run"
    report = run_fixed_agent(data.task, work, "run1", [
        response(1, "search_experience", {"tags": ["ByteWidth"]}),
        response(2, "read_experience", {"query_id": query_id, "entry_id": data.selected_entry}),
        response(3, "record_diagnosis", {"path": "src/main.ets", "line": 1, "reason": "bad value"}),
        response(4, "edit_source", {"path": "src/main.ets", "old": "bad", "new": "good"}),
        response(5, "register_candidate", {"diagnosis_id": "diagnosis-1"}),
        response(6, "run_check", {"candidate_id": good}), response(7),
    ], FixedTextExecutor("src/main.ets", "value=good\n"), knowledge=data.host)
    sent_page = next(item for item in observed[1] if isinstance(item, ToolMessage) and item.name == "search_experience")
    sent_entry = next(item for item in observed[2] if isinstance(item, ToolMessage) and item.name == "read_experience")
    page = json.loads(sent_page.content)
    projection = json.loads(sent_entry.content)["projection"]
    hit = next(hit for hit in page["retrieval"]["hits"] if hit["entry_id"] == data.selected_entry)
    assert hit["projection"] == projection
    assert projection["guidance"]["premises"] and projection["guidance"]["procedure"]
    assert projection["guidance"]["counterexamples"]
    assert report["status"] == "offline_checked" and report["application_verification"] == "not_run"
    assert report["model_calls"] == 7 and report["known_tokens"] == 105 and report["tool_calls"] == 6
    facts = report["knowledge_facts"]
    assert len(facts["queries"]) == len(facts["reads"]) == 1
    assert facts["unreferenced_query_ids"] == facts["unreferenced_retrieval_ids"] == []
    assert facts["queries"][0]["actions"][0]["model_call_id"] == "model-1"
    assert facts["reads"][0]["model_call_id"] == "model-2"
    assert tree_bytes(data.source.store.root) == external_before
    assert not (work / "applicability-contexts").exists()


def test_completed_search_receipt_replays_after_graph_crash_without_another_query(tmp_path: Path, monkeypatch) -> None:
    data = inputs(tmp_path)
    query_id = expected_query(data.host)
    work = tmp_path / "sdk-run"
    scripted = [response(1, "search_experience", {"tags": ["ByteWidth"]}),
                response(2, "read_experience", {"query_id": query_id}), response(3)]
    original = ToolActionStore.complete
    crashed = False

    def complete(self, action_id, message):
        nonlocal crashed
        original(self, action_id, message)
        if message.name == "search_experience" and not crashed:
            crashed = True
            raise RuntimeError("synthetic crash after durable search receipt")

    monkeypatch.setattr(ToolActionStore, "complete", complete)
    with pytest.raises(RuntimeError, match="durable search"):
        run_fixed_agent(data.task, work, "run1", scripted,
                        FixedTextExecutor("src/main.ets", "value=good\n"), knowledge=data.host)
    originals = tree_bytes(work / "knowledge")
    saved_queries = tree_bytes(work / "experience-queries")
    monkeypatch.setattr(ToolActionStore, "complete", original)
    # The external library is unnecessary for restoration and is unavailable.
    data.source.store.root.rename(data.source.store.root.with_name("unavailable"))
    report = run_fixed_agent(data.task, work, "run1", scripted,
        FixedTextExecutor("src/main.ets", "value=good\n"), resume=True, knowledge_approval=data.host.approval)
    assert tree_bytes(work / "knowledge") == originals and tree_bytes(work / "experience-queries") == saved_queries
    assert report["model_calls"] == 3 and report["model_calls_this_turn"] == 2
    assert report["tool_calls"] == 2 and len(report["knowledge_facts"]["queries"]) == 1
    before = tree_bytes(work)
    again = run_fixed_agent(data.task, work, "run1", scripted,
        FixedTextExecutor("src/main.ets", "value=good\n"), resume=True, knowledge_approval=data.host.approval)
    assert again["model_calls_this_turn"] == 0 and again["tool_calls"] == 2
    assert tree_bytes(work / "knowledge") == originals
    # The reconciled report may change this-turn counters, never guidance.
    assert {key: value for key, value in tree_bytes(work).items() if key.startswith("experience-")} == {
        key: value for key, value in before.items() if key.startswith("experience-")}


@pytest.mark.parametrize("args", [{"scope": "confirmation"}, {"path": "data_B/answer"},
                                 {"snapshot_sha256": "f" * 64}, {"limit": 0}, {"offset": -1}],
                         ids=["scope", "path", "snapshot", "limit", "offset"])
def test_model_cannot_change_knowledge_approval_or_bypass_query_bounds(tmp_path: Path, args: dict) -> None:
    data = inputs(tmp_path)
    work = tmp_path / "sdk-run"
    report = run_fixed_agent(data.task, work, "run1", [response(1, "search_experience", args), response(2)],
        FixedTextExecutor("src/main.ets", "value=good\n"), knowledge=data.host)
    assert report["knowledge_facts"]["queries"] == []
    assert report["tool_calls"] == 1 and report["application_verification"] == "not_run"
    assert not (work / "experience-queries").exists()


def test_read_cannot_reach_an_entry_outside_the_actual_returned_page(tmp_path: Path) -> None:
    data = inputs(tmp_path)
    query_id = expected_query(data.host)
    work = tmp_path / "sdk-run"
    report = run_fixed_agent(data.task, work, "run1", [
        response(1, "search_experience", {"tags": ["ByteWidth"]}),
        response(2, "read_experience", {"query_id": query_id, "entry_id": data.other_entry}), response(3),
    ], FixedTextExecutor("src/main.ets", "value=good\n"), knowledge=data.host)
    assert report["knowledge_facts"]["reads"] == []
    assert len(report["knowledge_facts"]["queries"]) == 1


@pytest.mark.parametrize("approval", ["missing", "changed"])
def test_resume_requires_original_external_approval_before_any_writable_resource(tmp_path: Path, approval: str) -> None:
    data = inputs(tmp_path)
    work = tmp_path / "sdk-run"
    run_fixed_agent(data.task, work, "run1", [response(1)],
                    FixedTextExecutor("src/main.ets", "value=good\n"), knowledge=data.host)
    chosen = None if approval == "missing" else data.host.approval.model_copy(update={"snapshot_sha256": "f" * 64})
    before = tree_bytes(work)
    with pytest.raises(ResumeAdmissionRejected):
        run_fixed_agent(data.task, work, "run1", [response(1)], FixedTextExecutor("src/main.ets", "value=good\n"),
                        resume=True, knowledge_approval=chosen)
    assert tree_bytes(work) == before


def test_shared_tool_budget_rejects_search_before_publication(tmp_path: Path) -> None:
    data = inputs(tmp_path)
    work = tmp_path / "sdk-run"
    report = run_fixed_agent(data.task, work, "run1", [response(1, "read_log"),
        response(2, "search_experience", {"tags": ["ByteWidth"]}), response(3)],
        FixedTextExecutor("src/main.ets", "value=good\n"), max_tool_calls=1, knowledge=data.host)
    assert report["status"] == "budget_exhausted"
    assert report["knowledge_facts"]["queries"] == [] and report["tool_calls"] == 2
    assert not (work / "experience-queries").exists()


def test_query_drift_is_rejected_before_resume_opens_writable_ledgers(tmp_path: Path) -> None:
    data = inputs(tmp_path)
    work = tmp_path / "sdk-run"
    run_fixed_agent(data.task, work, "run1", [response(1, "search_experience"), response(2)],
                    FixedTextExecutor("src/main.ets", "value=good\n"), knowledge=data.host)
    query_path = next((work / "experience-queries").glob("*.json"))
    record = json.loads(query_path.read_bytes())
    record["content_sha256"] = "f" * 64
    query_path.write_text(encode(record), encoding="utf-8")
    before = tree_bytes(work)
    with pytest.raises(ResumeAdmissionRejected):
        run_fixed_agent(data.task, work, "run1", [response(1)], FixedTextExecutor("src/main.ets", "value=good\n"),
                        resume=True, knowledge_approval=data.host.approval)
    assert tree_bytes(work) == before


def test_query_namespace_capacity_is_rejected_before_first_record(tmp_path: Path, monkeypatch) -> None:
    data = inputs(tmp_path)
    profile = data.prepare()
    store = data.read(profile)
    assert store is not None
    before = tree_bytes(data.workspace.root)
    monkeypatch.setattr(knowledge_handoff, "MAX_CLOSURE_BYTES", 64)
    result = experience_tools(store)[0].invoke({"tags": ["ByteWidth"]})
    assert result == "rejected: knowledge_source_invalid"
    assert tree_bytes(data.workspace.root) == before


def test_rehashed_tool_receipt_cannot_omit_an_unknown_question(tmp_path: Path) -> None:
    data = inputs(tmp_path)
    work = tmp_path / "sdk-run"
    run_fixed_agent(data.task, work, "run1", [response(1, "search_experience", {"tags": ["ByteWidth"]}), response(2)],
                    FixedTextExecutor("src/main.ets", "value=good\n"), knowledge=data.host)
    with closing(sqlite3.connect(work / "tool-actions.sqlite")) as connection, connection:
        key, raw = connection.execute("SELECT action_id,message_json FROM tool_actions").fetchone()
        message = json.loads(raw)
        result = json.loads(message["content"])
        del result["retrieval"]["hits"][0]["projection"]["guidance"]["premises"][0]["unknown_question"]
        message["content"] = encode(result)
        altered = canonical(message)
        connection.execute("UPDATE tool_actions SET message_json=?,message_sha256=? WHERE action_id=?",
                           (altered, sha256(altered), key))
    workspace = CandidateWorkspace.reopen(data.task, work, "run1")
    ledger = CheckLedger(workspace, FixedTextExecutor("src/main.ets", "value=good\n"), read_only=True)
    with pytest.raises(ValueError, match="complete frozen guidance"):
        build_completion(workspace, ledger, ModelCallStore(work / "model-calls.sqlite", read_only=True),
                         ToolActionStore(work / "tool-actions.sqlite", read_only=True), stopped_reason=None,
                         task_directory=data.task)


@pytest.mark.parametrize("missing", ["model-calls.sqlite", "tool-actions.sqlite"])
def test_resume_without_queries_still_rejects_missing_initialized_ledger(tmp_path: Path, missing: str) -> None:
    data = inputs(tmp_path)
    work = tmp_path / "sdk-run"
    run_fixed_agent(data.task, work, "run1", [response(1)],
                    FixedTextExecutor("src/main.ets", "value=good\n"), knowledge=data.host)
    (work / missing).unlink()
    before = tree_bytes(work)
    with pytest.raises(ResumeAdmissionRejected):
        run_fixed_agent(data.task, work, "run1", [response(1)],
                        FixedTextExecutor("src/main.ets", "value=good\n"), resume=True,
                        knowledge_approval=data.host.approval)
    assert tree_bytes(work) == before and not (work / missing).exists()


def test_dangling_query_directory_is_rejected_before_listing(tmp_path: Path, monkeypatch) -> None:
    """Simulate lstat of a dangling link without requiring Windows symlink privilege."""
    data = inputs(tmp_path)
    target = data.workspace.root / "experience-queries"
    original = Path.lstat

    def lstat(self, *args, **kwargs):
        if self == target:
            return os.stat_result((stat.S_IFLNK | 0o777, 1, 1, 1, 0, 0, 0, 0, 0, 0))
        return original(self, *args, **kwargs)

    assert not target.exists()
    monkeypatch.setattr(Path, "lstat", lstat)
    reads = knowledge_handoff._ReadSet(data.workspace.root)
    with pytest.raises(ValueError, match="linked"):
        knowledge_handoff._records(data.workspace.root, "experience-queries", "bound-query-", reads)
    assert reads.read_calls == 0


@pytest.mark.parametrize("working_context", [False, True], ids=["ordinary-context", "working-context"])
def test_complete_guidance_reaches_mock_wire_with_and_without_working_context(
        tmp_path: Path, working_context: bool) -> None:
    data = inputs(tmp_path)
    requests = []
    adapter = scripted_adapter([wire_response(1, [("search_experience", {"tags": ["ByteWidth"]})]),
                                wire_response(2)], requests)
    work = tmp_path / "sdk-run"
    policy = WorkingContextPolicy(enabled=working_context)
    report = run_provider_agent(data.task, work, "run1", adapter,
        FixedTextExecutor("src/main.ets", "value=good\n"), knowledge=data.host, working_context_policy=policy)
    sent = next(item for item in requests[1]["messages"]
                if item["role"] == "tool" and item["tool_call_id"] == "call-1-0")
    page = json.loads(sent["content"])
    hit = next(item for item in page["retrieval"]["hits"] if item["entry_id"] == data.selected_entry)
    guidance = hit["projection"]["guidance"]
    assert guidance["premises"][0]["unknown_question"]
    assert guidance["procedure"] and guidance["counterexamples"]
    assert {item["function"]["name"] for item in requests[0]["tools"]} == set(report["offered_tools"])
    assert {"search_experience", "read_experience"} <= set(report["offered_tools"])
    assert report["schema_version"] == 8 and report["completion"]["schema_version"] == 5
    assert report["model_calls"] == 2 and report["known_tokens"] == 30 and report["tool_calls"] == 1
    assert report["application_verification"] == "not_run"
    again = run_provider_agent(data.task, work, "run1", adapter,
        FixedTextExecutor("src/main.ets", "value=good\n"), resume=True,
        knowledge_approval=data.host.approval, working_context_policy=policy)
    assert len(requests) == 2 and again["model_calls_this_turn"] == 0


def test_action_cannot_claim_a_tool_absent_from_its_accounted_solver(tmp_path: Path) -> None:
    data = inputs(tmp_path)
    work = tmp_path / "sdk-run"
    report = run_fixed_agent(data.task, work, "run1", [response(1, "search_experience"), response(2)],
        FixedTextExecutor("src/main.ets", "value=good\n"), knowledge=data.host)
    with closing(sqlite3.connect(work / "model-calls.sqlite")) as connection, connection:
        intent_raw, message_raw = connection.execute(
            "SELECT intent_json,message_json FROM model_calls WHERE call_id='model-1'").fetchone()
        value = json.loads(intent_raw)
        value["tool_names"].remove("search_experience")
        intent = ModelCallIntent.model_validate_json(encode(value))
        # Coherently rehash the request and normalize its original response.
        observation = normalize_message(intent, AIMessage.model_validate_json(message_raw))
        connection.execute("UPDATE model_calls SET intent_json=?,request_sha256=?,observation_json=? "
                           "WHERE call_id='model-1'", (canonical(intent.model_dump(mode="json")), intent.sha256(),
                                                    canonical(observation.model_dump(mode="json"))))
    workspace = CandidateWorkspace.reopen(data.task, work, "run1")
    ledger = CheckLedger(workspace, FixedTextExecutor("src/main.ets", "value=good\n"), read_only=True)
    with pytest.raises(ValueError, match="not offered"):
        knowledge_handoff.audit_run_knowledge(workspace, data.task, ledger, report["knowledge_profile"],
            ModelCallStore(work / "model-calls.sqlite", read_only=True),
            ToolActionStore(work / "tool-actions.sqlite", read_only=True))


def test_large_chinese_guidance_batch_rejects_extra_pages_before_publication_and_keeps_report_readable(
        tmp_path: Path) -> None:
    data = inputs(tmp_path)
    value = guidance("width").model_dump(mode="json")
    value["premises"] = [{"premise_id": f"large-premise-{number}", "statement": "容量" * 1750,
                          "unknown_question": "依据" * 1750} for number in range(32)]
    content = ExperienceContent.model_validate_json(encode(value))
    assert 600_000 < len(encode(content).encode("utf-8")) < 750_000
    entry = admit(data.source.store, tmp_path / "large-guidance", "width", content=content)
    snapshot = data.source.store.freeze_snapshot((entry.entry_id,), data.host.approval.scope)
    data.source.build_index(snapshot.snapshot_sha256, data.host.approval.scope)
    approval = data.host.approval.model_dump(mode="json")
    approval["snapshot_sha256"] = snapshot.snapshot_sha256
    host = HostKnowledgeInput(data.source, TaskKnowledgeBinding.model_validate_json(encode(approval)))
    requests = [{"name": "search_experience", "args": {"text": f"容量{number}", "tags": ["ByteWidth"]},
                 "id": f"large-query-{number}"} for number in range(12)]
    work = tmp_path / "large-sdk-run"
    external_before = tree_bytes(data.source.store.root)
    report = run_fixed_agent(data.task, work, "run1", [
        AIMessage(content="", tool_calls=requests,
                  usage_metadata={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}),
        response(2),
    ], FixedTextExecutor("src/main.ets", "value=good\n"), knowledge=host, max_tool_calls=len(requests))
    terminal(work, "run1")
    actions = ToolActionStore(work / "tool-actions.sqlite", read_only=True)
    receipts = [actions.receipt(row["action_id"]).content for row in actions.records()]
    rejected = [item for item in receipts if item == "rejected: knowledge_source_invalid"]
    pages = [json.loads(item) for item in receipts if isinstance(item, str) and item.startswith("{")]
    assert len(pages) == 1 and len(rejected) == len(requests) - 1
    hit = pages[0]["retrieval"]["hits"][0]
    assert hit["entry_id"] == entry.entry_id
    assert hit["projection"]["guidance"]["premises"] == content.model_dump(mode="json")["premises"]
    assert len(list((work / "experience-queries").glob("*.json"))) == 1
    assert len(list((work / "knowledge/retrievals").glob("*.json"))) == 1
    assert len(report["knowledge_facts"]["queries"]) == 1
    assert report["tool_calls"] == len(requests) and report["model_calls"] == 2
    assert report["application_verification"] == "not_run"
    assert (work / "agent-report.json").stat().st_size < 16_777_216
    assert tree_bytes(data.source.store.root) == external_before
    before = tree_bytes(work)
    assert read_completion(*open_run(data.task, work), task_directory=data.task) == report["completion"]
    aggregate = aggregate_evaluation(parse_evaluation_manifest(manifest3(tmp_path, data.task, work)), tmp_path)
    assert aggregate["rows"][0]["record_status"] == "verified"
    assert aggregate["rows"][0]["knowledge_facts"] == report["knowledge_facts"]
    assert tree_bytes(work) == before
