"""Exercise working context at the real SDK/model boundary with synthetic tasks."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from test_agent_loop import candidate_id, response, task_snapshot
from test_provider_loop import scripted_adapter, settings, wire_response

from dfx_agent import models
from dfx_agent.actions import ToolActionStore
from dfx_agent.agent import run_fixed_agent, run_provider_agent
from dfx_agent.context_compaction import MAX_ARTIFACT_BYTES, CompactionPolicy
from dfx_agent.model_calls import ModelCallStore, canonical
from dfx_agent.model_provider import ChatCompletionsAdapter
from dfx_agent.offline_script import load_offline_script
from dfx_agent.reporting import read_completion
from dfx_agent.task import digest, snapshot_digest, verify_task
from dfx_agent.verification import CheckLedger, FixedTextExecutor
from dfx_agent.working_context_plan import WORKING_CONTEXT_PROMPT, WorkingContextPolicy
from dfx_agent.workspace import CandidateWorkspace


def checked_completion(task: Path, work: Path) -> dict:
    workspace = CandidateWorkspace.reopen(task, work, "run1")
    return read_completion(
        workspace, CheckLedger(workspace, FixedTextExecutor("src/main.ets", "value=good\n"), read_only=True),
        ModelCallStore(work / "model-calls.sqlite", read_only=True),
        ToolActionStore(work / "tool-actions.sqlite", read_only=True))


def test_fixed_sdk_packet_changes_send_view_and_old_plans_survive_later_checks(tmp_path: Path, monkeypatch) -> None:
    task, work = task_snapshot(tmp_path), tmp_path / "run"
    good = candidate_id(task, tmp_path / "derive", "good")
    observed = []
    original = models.FixedResponseModel._intent

    def observe(self, messages, stop, kwargs):
        observed.append([item.model_dump(mode="json") for item in messages])
        return original(self, messages, stop, kwargs)

    monkeypatch.setattr(models.FixedResponseModel, "_intent", observe)
    policy = WorkingContextPolicy(enabled=True)
    scripted = [response(1, "read_log"), response(2, "record_diagnosis", {
        "path": "src/main.ets", "line": 1, "reason": "unknown bad value"}),
        response(3, "edit_source", {"path": "src/main.ets", "old": "bad", "new": "good"}),
        response(4, "register_candidate", {"diagnosis_id": "diagnosis-1"}),
        response(5, "run_check", {"candidate_id": good}), response(6)]
    executor = FixedTextExecutor("src/main.ets", "value=good\n")
    report = run_fixed_agent(task, work, "run1", scripted, executor, working_context_policy=policy)
    assert report["status"] == "offline_checked", report["stopped_reason"]
    assert report["schema_version"] == 7 and report["completion"]["schema_version"] == 4
    assert len(observed) == report["model_calls"] == 6
    assert all(WORKING_CONTEXT_PROMPT in item[1]["content"] for item in observed)
    assert report["application_verification"] == "not_run"
    plans = report["completion"]["working_context_plans"]
    assert len(plans) == 6
    # Recomputing the whole report now must not rebuild pre-check packets from
    # the newer verification state. Exact historical send receipts remain valid.
    assert checked_completion(task, work) == report["completion"]
    before = {path.relative_to(work).as_posix(): path.read_bytes()
              for path in (work / "working-context").rglob("*") if path.is_file()}
    resumed = run_fixed_agent(task, work, "run1", scripted, executor, resume=True, working_context_policy=policy)
    assert resumed["model_calls_this_turn"] == 0
    assert resumed["completion"] == report["completion"]
    assert before == {path.relative_to(work).as_posix(): path.read_bytes()
                      for path in (work / "working-context").rglob("*") if path.is_file()}


def test_generic_candidate_path_without_diagnosis_remains_available(tmp_path: Path) -> None:
    task, work = task_snapshot(tmp_path), tmp_path / "run"
    good = candidate_id(task, tmp_path / "derive", "good")
    report = run_fixed_agent(task, work, "run1", [
        response(1, "edit_source", {"path": "src/main.ets", "old": "bad", "new": "good"}),
        response(2, "register_candidate"), response(3, "run_check", {"candidate_id": good}), response(4)],
        FixedTextExecutor("src/main.ets", "value=good\n"), working_context_policy=WorkingContextPolicy(enabled=True))
    assert report["status"] == "offline_checked", report["stopped_reason"]
    assert checked_completion(task, work) == report["completion"]
    assert "diagnosis-1" not in canonical(report["completion"]["working_context_plans"])


def test_mock_sdk_request_is_bound_to_augmented_messages_and_offered_tools(tmp_path: Path) -> None:
    task, work = task_snapshot(tmp_path), tmp_path / "run"
    requests = []
    adapter = scripted_adapter([wire_response(1)], requests)
    report = run_provider_agent(task, work, "run1", adapter, FixedTextExecutor("src/main.ets", "value=good\n"),
                                working_context_policy=WorkingContextPolicy(enabled=True))
    assert report["stopped_reason"] is None
    assert len(requests) == 1 and WORKING_CONTEXT_PROMPT in requests[0]["messages"][1]["content"]
    assert requests[0]["tools"] and report["completion"]["working_context_plans"]
    assert checked_completion(task, work) == report["completion"]


def test_decision_layers_replace_flat_packet_on_actual_sdk_wire_and_resume(tmp_path: Path) -> None:
    task, work = task_snapshot(tmp_path), tmp_path / "run"
    good = candidate_id(task, tmp_path / "derive", "good")
    requests: list[dict] = []
    scripted = [wire_response(1, [("read_log", {})]), wire_response(2, [("record_diagnosis", {
        "path": "src/main.ets", "line": 1, "reason": "unknown bad value"})]),
        wire_response(3, [("edit_source", {"path": "src/main.ets", "old": "bad", "new": "good"})]),
        wire_response(4, [("register_candidate", {"diagnosis_id": "diagnosis-1"})]),
        wire_response(5, [("read_source", {"path": "src/main.ets", "candidate_id": good})]),
        wire_response(6, [("run_check", {"candidate_id": good})]), wire_response(7)]
    policy = WorkingContextPolicy(enabled=True, history_selection="repair_frontier", context_layout="decision_layers")
    adapter = scripted_adapter(scripted, requests, configuration=settings(thinking_mode="disabled"))
    executor = FixedTextExecutor("src/main.ets", "value=good\n")
    report = run_provider_agent(task, work, "run1", adapter, executor, working_context_policy=policy)
    assert report["status"] == "offline_checked", report["stopped_reason"]
    assert len(requests) == report["model_calls"] == 7
    candidate_reads = [json.loads(message["content"]) for message in requests[5]["messages"]
                       if message.get("role") == "tool" and message.get("tool_call_id") == "call-5-0"]
    assert candidate_reads and candidate_reads[-1]["candidate_id"] == good
    assert candidate_reads[-1]["lines"] == ["value=good"]
    assert all(body["thinking"] == {"type": "disabled"} for body in requests)
    panels = [json.loads(body["messages"][1]["content"].split(WORKING_CONTEXT_PROMPT + "\n", 1)[1]) for body in requests]
    assert all("packet" not in panel and "decision_context" in panel for panel in panels)
    contexts = [panel["decision_context"] for panel in panels]
    assert all(context["layout"] == "repair_decision_slots_v1" for context in contexts)
    assert contexts[0]["decision_board"]["phase"] == "exploration"
    assert contexts[-1]["decision_board"]["phase"] == "delivery_review"
    assert all(context["decision_board"]["execution_permission"] == "not_granted" and
               context["decision_board"]["repair_acceptance"] == "not_assessed" for context in contexts)
    assert contexts[-1]["evidence_by_producer"]["model"]
    assert all(fact["producer"] == "model" for fact in contexts[-1]["evidence_by_producer"]["model"].values())
    assert report["application_verification"] == "not_run"
    assert checked_completion(task, work) == report["completion"]
    resumed = run_provider_agent(task, work, "run1", adapter, executor, resume=True, working_context_policy=policy)
    assert resumed["model_calls_this_turn"] == 0 and len(requests) == 7
    assert resumed["completion"] == report["completion"]


def test_unknown_mock_result_keeps_the_same_frozen_plan_without_resending(tmp_path: Path) -> None:
    task, work = task_snapshot(tmp_path), tmp_path / "run"
    sent = []

    def fail(request: httpx.Request) -> httpx.Response:
        sent.append(request.content)
        raise httpx.ReadTimeout("synthetic lost response")

    policy = WorkingContextPolicy(enabled=True)
    report = run_provider_agent(task, work, "run1", ChatCompletionsAdapter(settings(), httpx.MockTransport(fail)),
                                FixedTextExecutor("src/main.ets", "value=good\n"), working_context_policy=policy)
    assert report["stopped_reason"] == "model_result_unknown" and len(sent) == 1
    frozen = {path.relative_to(work).as_posix(): path.read_bytes()
              for path in (work / "working-context").rglob("*") if path.is_file()}
    resumed = run_provider_agent(task, work, "run1", ChatCompletionsAdapter(settings(), httpx.MockTransport(fail)),
                                 FixedTextExecutor("src/main.ets", "value=good\n"), resume=True,
                                 working_context_policy=policy)
    assert resumed["stopped_reason"] == "model_result_unknown" and len(sent) == 1
    assert resumed["model_calls_this_turn"] == 0
    assert frozen == {path.relative_to(work).as_posix(): path.read_bytes()
                      for path in (work / "working-context").rglob("*") if path.is_file()}


def test_completed_reply_before_checkpoint_recovers_from_frozen_plan(tmp_path: Path, monkeypatch) -> None:
    task, work = task_snapshot(tmp_path), tmp_path / "run"
    original = models.invoke_accounted
    policy = WorkingContextPolicy(enabled=True)

    class SyntheticCrash(RuntimeError):
        pass

    def crash(*args, **kwargs):
        original(*args, **kwargs)
        raise SyntheticCrash("after durable response")

    monkeypatch.setattr(models, "invoke_accounted", crash)
    with pytest.raises(SyntheticCrash):
        run_fixed_agent(task, work, "run1", [response(1)], FixedTextExecutor("src/main.ets", "value=good\n"),
                        working_context_policy=policy)
    monkeypatch.setattr(models, "invoke_accounted", original)
    report = run_fixed_agent(task, work, "run1", [response(1)], FixedTextExecutor("src/main.ets", "value=good\n"),
                             resume=True, working_context_policy=policy)
    assert report["model_calls_this_turn"] == 0 and report["model_calls"] == 1
    assert checked_completion(task, work) == report["completion"]


def test_required_packet_budget_stops_before_a_solver_request(tmp_path: Path) -> None:
    report = run_fixed_agent(task_snapshot(tmp_path), tmp_path / "run", "run1", [response(1)],
                             FixedTextExecutor("src/main.ets", "value=good\n"),
                             working_context_policy=WorkingContextPolicy(enabled=True, max_packet_bytes=128))
    assert report["stopped_reason"] == "working_context_required_limit"
    assert report["model_calls"] == 0


@pytest.mark.parametrize("size", [6_400_000, MAX_ARTIFACT_BYTES + 1])
def test_raw_material_capacity_supports_large_source_but_preserves_single_blob_limit(tmp_path: Path, size: int) -> None:
    task = task_snapshot(tmp_path)
    large = b"//" + b"x" * size + b"\n"
    (task / "source/src/large.ets").write_bytes(large)
    manifest = [{"path": "src/large.ets", "sha256": digest(large)},
                {"path": "src/main.ets", "sha256": digest(b"value=bad\n")}]
    (task / "source-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    value = json.loads((task / "task.json").read_text(encoding="utf-8"))
    value["source_snapshot_sha256"] = snapshot_digest(manifest)
    (task / "task.json").write_text(json.dumps(value), encoding="utf-8")
    assert verify_task(task)["source_snapshot_sha256"] == value["source_snapshot_sha256"]
    report = run_fixed_agent(task, tmp_path / "run", "run1", [response(1)],
                             FixedTextExecutor("src/main.ets", "value=good\n"),
                             working_context_policy=WorkingContextPolicy(enabled=True))
    if len(large) > MAX_ARTIFACT_BYTES:
        assert report["stopped_reason"] == "working_context_material_limit"
        assert report["model_calls"] == 0 and report["completion"]["working_context_plans"] == []
    else:
        assert report["stopped_reason"] is None and report["model_calls"] == 1
        plan = report["completion"]["working_context_plans"][0]
        assert plan["format_version"] == 2 and plan["materialized_bytes"] > 6_400_000
        assert checked_completion(task, tmp_path / "run") == report["completion"]


def test_thousand_file_candidate_uses_complete_audit_material_but_bounded_solver_projection(tmp_path: Path) -> None:
    task, work = task_snapshot(tmp_path), tmp_path / "run"
    for number in range(1_200):
        (task / "source/src" / f"unrelated-{number:04}.ets").write_bytes(b"// independent synthetic source\n")
    manifest = [{"path": path.relative_to(task / "source").as_posix(), "sha256": digest(path.read_bytes())}
                for path in sorted((task / "source").rglob("*.ets"))]
    (task / "source-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    value = json.loads((task / "task.json").read_text(encoding="utf-8"))
    value["source_snapshot_sha256"] = snapshot_digest(manifest)
    (task / "task.json").write_text(json.dumps(value), encoding="utf-8")
    good = candidate_id(task, tmp_path / "derive", "good")
    report = run_fixed_agent(task, work, "run1", [
        response(1, "record_diagnosis", {"path": "src/main.ets", "line": 1, "reason": "unknown bad value"}),
        response(2, "edit_source", {"path": "src/main.ets", "old": "bad", "new": "good"}),
        response(3, "register_candidate", {"diagnosis_id": "diagnosis-1"}),
        response(4, "run_check", {"candidate_id": good}), response(5)],
        # Capacity stress has its own explicit time budget. The default 60s
        # failure is recorded separately; this does not certify default speed.
        FixedTextExecutor("src/main.ets", "value=good\n"), max_seconds=300,
        working_context_policy=WorkingContextPolicy(enabled=True))
    assert report["status"] == "offline_checked", report["stopped_reason"]
    plans = report["completion"]["working_context_plans"]
    assert any(plan["audit_packet_bytes"] > 131_072 for plan in plans)
    assert all(plan["solver_projection_bytes"] < 20_000 for plan in plans)
    assert plans[-1]["unique_referenced_material_bytes"] < plans[-1]["materialized_bytes"]
    assert plans[-1]["run_material_observation"]["unique_blob_count"] < 20
    assert checked_completion(task, work) == report["completion"]


def test_compaction_and_working_context_are_independent_but_share_accounting(tmp_path: Path) -> None:
    report = run_fixed_agent(task_snapshot(tmp_path), tmp_path / "run", "run1", [
        response(1, "list_source"), response(2, "read_log"), response(3, "list_source"),
        response(4, "read_log"), response(5)], FixedTextExecutor("src/main.ets", "value=good\n"),
        compaction_policy=CompactionPolicy(enabled=True, trigger_bytes=128, retain_groups=1),
        compaction_responses=[response(1), response(2), response(3)],
        working_context_policy=WorkingContextPolicy(enabled=True))
    assert report["stopped_reason"] is None
    assert report["completion"]["working_context_plans"] and report["completion"]["context_compactions"]
    cost = report["completion"]["cost"]
    assert cost["calls_by_kind"]["solver"] == 5 and cost["calls_by_kind"]["context_compaction"] > 0
    assert cost["reserved_calls"] == sum(cost["calls_by_kind"].values())
    assert cost["total_tokens"] == cost["reserved_calls"] * 15


@pytest.mark.parametrize("schema", [3, 4])
def test_new_script_schemas_explicitly_freeze_working_context(tmp_path: Path, schema: int) -> None:
    from test_offline_script import fixed_document, mock_document

    value = fixed_document() if schema == 3 else mock_document()
    value.update(schema_version=schema, working_context={"enabled": True, "retain_groups": 1})
    path = tmp_path / "script.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    script = load_offline_script(path)
    assert script.working_context_policy() == WorkingContextPolicy(enabled=True, retain_groups=1)
    assert script.mode == ("fixed_response" if schema == 3 else "mock_chat_completions")


@pytest.mark.parametrize("schema", [1, 2])
def test_old_script_schemas_cannot_silently_enable_working_context(tmp_path: Path, schema: int) -> None:
    from test_offline_script import fixed_document, mock_document

    value = fixed_document() if schema == 1 else mock_document()
    value["working_context"] = {"enabled": True}
    path = tmp_path / "script.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(ValueError, match="fields"):
        load_offline_script(path)
