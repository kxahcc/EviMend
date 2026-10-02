"""Managed summaries in the real SDK loop remain accounted, replayable and non-authoritative."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from langchain_core.messages import AIMessage, ToolMessage
from test_agent_loop import candidate_id, response, task_snapshot
from test_provider_loop import scripted_adapter, settings, wire_response

from dfx_agent import agent
from dfx_agent.actions import TOOL_ACTIONS, ToolActionStore
from dfx_agent.agent import run_fixed_agent, run_provider_agent
from dfx_agent.context_compaction import (
    CompactionPolicy,
    CompactionStore,
    ContextCompactionError,
)
from dfx_agent.model_calls import ModelCallStore
from dfx_agent.model_provider import ChatCompletionsAdapter
from dfx_agent.reporting import read_completion
from dfx_agent.task import snapshot_digest
from dfx_agent.verification import CheckLedger, FixedTextExecutor
from dfx_agent.workspace import CandidateWorkspace, _manifest

SUMMARY = "Model inference only: earlier source reads found a large synthetic marker; no repair is verified."


def large_task(tmp_path: Path) -> Path:
    task = task_snapshot(tmp_path)
    source = task / "source"
    (source / "src/large.ets").write_text("marker=" + "x" * 200_000 + "\n", encoding="utf-8")
    manifest = _manifest(source)
    (task / "source-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    value = json.loads((task / "task.json").read_text(encoding="utf-8"))
    value["source_snapshot_sha256"] = snapshot_digest(manifest)
    (task / "task.json").write_text(json.dumps(value), encoding="utf-8")
    return task


def policy(**overrides: Any) -> CompactionPolicy:
    # Startup plus one read remains below the trigger; two reads exceed it.
    return CompactionPolicy.model_validate({
        "enabled": True, "trigger_bytes": 300_000, "retain_groups": 1, **overrides,
    })


def executor() -> FixedTextExecutor:
    return FixedTextExecutor("src/main.ets", "value=good\n")


def read_replies() -> list[dict]:
    return [wire_response(1, [("read_source", {"path": "src/large.ets"})]),
            wire_response(2, [("read_source", {"path": "src/large.ets"})])]


def observe_graph_messages(monkeypatch: pytest.MonkeyPatch) -> list[list]:
    """Read the SDK's assembled DeltaChannel state while its saver is still open."""
    original = agent.create_deep_agent
    observed: list[list] = []

    class ObservedGraph:
        def __init__(self, graph):
            self.graph = graph

        def invoke(self, input_value, config, *args, **kwargs):
            result = self.graph.invoke(input_value, config, *args, **kwargs)
            observed.append(list(self.graph.get_state(config).values["messages"]))
            return result

        def get_state(self, *args, **kwargs):
            return self.graph.get_state(*args, **kwargs)

        def __getattr__(self, name):
            return getattr(self.graph, name)

    def observed_factory(*args, **kwargs):
        return ObservedGraph(original(*args, **kwargs))

    monkeypatch.setattr(agent, "create_deep_agent", observed_factory)
    return observed


def verified_completion(task: Path, work: Path, check_executor: FixedTextExecutor) -> dict:
    workspace = CandidateWorkspace.reopen(task, work, "run1")
    return read_completion(workspace, CheckLedger(workspace, check_executor),
                           ModelCallStore(work / "model-calls.sqlite"),
                           ToolActionStore(work / "tool-actions.sqlite"))


def assert_paired_wire_history(messages: list[dict]) -> None:
    outstanding: set[str] = set()
    for message in messages:
        if message["role"] == "assistant":
            assert not outstanding, "a summary must not retain an orphaned tool result"
            outstanding = {call["id"] for call in message.get("tool_calls", [])}
        elif message["role"] == "tool":
            assert message["tool_call_id"] in outstanding
            outstanding.remove(message["tool_call_id"])
        else:
            assert not outstanding, "a summary must not split an assistant/tool block"
    assert not outstanding


def test_mock_summary_has_no_tools_keeps_graph_and_counts_all_costs(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    observed_history = observe_graph_messages(monkeypatch)
    task = large_task(tmp_path)
    before = _manifest(task / "source")
    work = tmp_path / "run"
    requests: list[dict] = []
    check_executor = executor()
    report = run_provider_agent(task, work, "run1", scripted_adapter([
        *read_replies(), wire_response(3, text=SUMMARY), wire_response(4),
    ], requests), check_executor, compaction_policy=policy())
    assert report["schema_version"] == 5 and report["completion"]["schema_version"] == 2
    assert report["stopped_reason"] is None and report["status"] == "unresolved"
    assert report["model_calls"] == len(requests) == 4
    assert report["known_tokens"] == report["completion"]["cost"]["total_tokens"] == 60
    assert report["tool_calls"] == 2 and len(report["completion"]["context_compactions"]) == 1
    assert set(report["offered_tools"]) == set(TOOL_ACTIONS)
    for index in (0, 1, 3):
        assert {item["function"]["name"] for item in requests[index]["tools"]} == set(TOOL_ACTIONS)
        assert_paired_wire_history(requests[index]["messages"])
    assert requests[2].get("tools", []) == []
    assert SUMMARY in json.dumps(requests[3]["messages"])
    assert sum(message["role"] == "tool" for message in requests[3]["messages"]) == 1
    assert requests[3]["messages"][0] == requests[0]["messages"][0]
    assert requests[3]["messages"][1] == requests[0]["messages"][1]
    calls = ModelCallStore(work / "model-calls.sqlite")
    assert [(item.intent.call_id, item.intent.kind) for item in calls.records("task1", "run1")] == [
        ("model-1", "solver"), ("model-2", "solver"),
        ("compaction-model-3", "context_compaction"), ("model-3", "solver"),
    ]
    summary_call = calls.get("compaction-model-3")
    assert summary_call is not None and summary_call.intent.tool_names == ()
    assert len(observed_history) == 1
    messages = observed_history[0]
    assert sum(isinstance(message, AIMessage) for message in messages) == 3
    assert sum(isinstance(message, ToolMessage) for message in messages) == 2
    assert all("x" * 200_000 in message.content for message in messages if isinstance(message, ToolMessage))
    assert all(SUMMARY not in message.content for message in messages)
    assert _manifest(work / "base") == _manifest(task / "source") == before
    assert not (work / "base/conversation_history").exists()
    assert not (work / "base/large_tool_results").exists()
    session = json.loads((work / "session.json").read_text(encoding="utf-8"))
    assert session["schema_version"] == 8
    assert verified_completion(task, work, check_executor) == report["completion"]
    no_resend: list[dict] = []
    resumed = run_provider_agent(task, work, "run1", scripted_adapter([], no_resend), check_executor,
                                 compaction_policy=policy(), resume=True)
    assert no_resend == [] and resumed["model_calls_this_turn"] == 0
    assert resumed["known_tokens_this_turn"] == 0
    assert resumed["completion"] == report["completion"]


def test_fixed_summary_fixtures_do_not_consume_solver_script(tmp_path: Path) -> None:
    task = large_task(tmp_path)
    work = tmp_path / "run"
    scripted = [response(1, "read_source", {"path": "src/large.ets"}),
                response(2, "read_source", {"path": "src/large.ets"}), response(3)]
    summary = AIMessage(content=SUMMARY, usage_metadata={
        "input_tokens": 10, "output_tokens": 5, "total_tokens": 15,
    })
    report = run_fixed_agent(task, work, "run1", scripted, executor(),
                             compaction_policy=policy(), compaction_responses=[summary])
    assert report["stopped_reason"] is None
    assert report["model_calls"] == 4 and report["known_tokens"] == 60
    calls = ModelCallStore(work / "model-calls.sqlite")
    solver = calls.records("task1", "run1", kind="solver")
    assert [item.intent.call_id for item in solver] == ["model-1", "model-2", "model-3"]
    assert calls.replay(solver[-1].intent)[0].content == "done"
    assert len(calls.records("task1", "run1", kind="context_compaction")) == 1
    resumed = run_fixed_agent(task, work, "run1", scripted, executor(), resume=True,
                              compaction_policy=policy(), compaction_responses=[summary])
    assert resumed["model_calls_this_turn"] == resumed["known_tokens_this_turn"] == 0


def test_missing_summary_usage_keeps_total_cost_unknown(tmp_path: Path) -> None:
    task = large_task(tmp_path)
    work = tmp_path / "run"
    requests: list[dict] = []
    report = run_provider_agent(task, work, "run1", scripted_adapter([
        *read_replies(), wire_response(3, usage=None, text=SUMMARY), wire_response(4),
    ], requests), executor(), compaction_policy=policy())
    assert report["model_calls"] == 4 and report["known_tokens"] == 45
    assert report["missing_usage_calls"] == 1
    assert report["completion"]["cost"]["usage_complete"] is False
    assert report["completion"]["cost"]["total_tokens"] is None
    summary = ModelCallStore(work / "model-calls.sqlite").get("compaction-model-3")
    assert summary is not None and summary.state == "completed"
    assert summary.observation is not None and summary.observation.usage_status == "missing"


@pytest.mark.parametrize("invalid", ["empty", "tool", "oversized"])
def test_invalid_summary_is_accounted_without_executing_or_sending_solver(
        tmp_path: Path, invalid: str) -> None:
    task = large_task(tmp_path)
    work = tmp_path / "run"
    requests: list[dict] = []
    bad = (wire_response(3, text="   ") if invalid == "empty" else
           wire_response(3, [("read_source", {"path": "src/main.ets"})]) if invalid == "tool" else
           wire_response(3, text="x" * 129))
    report = run_provider_agent(task, work, "run1", scripted_adapter([*read_replies(), bad], requests),
                                 executor(), compaction_policy=policy(max_summary_bytes=128))
    assert report["stopped_reason"] == "context_summary_invalid" and report["status"] == "unresolved"
    assert report["model_calls"] == len(requests) == 3 and report["known_tokens"] == 45
    assert report["tool_calls"] == 2 and report["uncertain_model_calls"] == 0
    calls = ModelCallStore(work / "model-calls.sqlite")
    summary = calls.get("compaction-model-3")
    assert summary is not None and summary.state == "completed"
    assert calls.get("model-3") is None
    assert len(ToolActionStore(work / "tool-actions.sqlite").records()) == 2
    no_resend: list[dict] = []
    resumed = run_provider_agent(task, work, "run1", scripted_adapter([], no_resend), executor(),
                                 compaction_policy=policy(max_summary_bytes=128), resume=True)
    assert resumed["stopped_reason"] == "context_summary_invalid"
    assert resumed["model_calls_this_turn"] == 0 and no_resend == []


@pytest.mark.parametrize("limits", [{"max_calls": 3}, {"max_known_tokens": 45}])
def test_summary_spends_shared_budget_before_following_solver(
        tmp_path: Path, limits: dict[str, int]) -> None:
    task = large_task(tmp_path)
    work = tmp_path / "run"
    requests: list[dict] = []
    report = run_provider_agent(task, work, "run1", scripted_adapter([
        *read_replies(), wire_response(3, text=SUMMARY),
    ], requests), executor(), compaction_policy=policy(), **limits)
    assert report["status"] == "budget_exhausted" and report["stopped_reason"] == "model_budget"
    assert report["model_calls"] == len(requests) == 3 and report["known_tokens"] == 45
    calls = ModelCallStore(work / "model-calls.sqlite")
    assert calls.get("compaction-model-3") is not None and calls.get("model-3") is None
    assert len(report["completion"]["context_compactions"]) == 1


def test_durable_summary_replays_after_before_graph_checkpoint_crash(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    task = large_task(tmp_path)
    work = tmp_path / "run"
    requests: list[dict] = []
    original = CompactionStore.complete

    class SyntheticCrash(RuntimeError):
        pass

    def crash_before_summary_artifact(self, *args, **kwargs):
        summary = ModelCallStore(work / "model-calls.sqlite").get("compaction-model-3")
        assert summary is not None and summary.state == "completed"
        raise SyntheticCrash("after durable summary, before its artifact and graph checkpoint")

    monkeypatch.setattr(CompactionStore, "complete", crash_before_summary_artifact)
    with pytest.raises(SyntheticCrash, match="after durable summary"):
        run_provider_agent(task, work, "run1", scripted_adapter([
            *read_replies(), wire_response(3, text=SUMMARY),
        ], requests), executor(), compaction_policy=policy())
    assert len(requests) == 3
    assert ModelCallStore(work / "model-calls.sqlite").get("model-3") is None
    monkeypatch.setattr(CompactionStore, "complete", original)
    new_requests: list[dict] = []
    report = run_provider_agent(task, work, "run1", scripted_adapter([wire_response(4)], new_requests),
                                 executor(), compaction_policy=policy(), resume=True)
    assert len(new_requests) == 1 and new_requests[0]["tools"]
    assert SUMMARY in json.dumps(new_requests[0]["messages"])
    assert report["model_calls"] == 4 and report["model_calls_this_turn"] == 1
    assert report["known_tokens"] == 60 and report["known_tokens_this_turn"] == 15
    assert len(report["completion"]["context_compactions"]) == 1


def test_unknown_summary_never_resends_on_resume(tmp_path: Path) -> None:
    task = large_task(tmp_path)
    work = tmp_path / "run"
    requests: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append(body)
        if not body.get("tools"):
            raise httpx.ReadTimeout("synthetic summary response lost", request=request)
        return httpx.Response(200, json=read_replies()[len(requests) - 1])

    adapter = ChatCompletionsAdapter(settings(), httpx.MockTransport(handler))
    report = run_provider_agent(task, work, "run1", adapter, executor(), compaction_policy=policy())
    assert report["status"] == "unresolved" and report["stopped_reason"] == "model_result_unknown"
    assert report["model_calls"] == len(requests) == 3
    assert report["uncertain_model_calls"] == 1
    summary = ModelCallStore(work / "model-calls.sqlite").get("compaction-model-3")
    assert summary is not None and summary.state == "unknown"
    assert report["completion"]["cost"]["total_tokens"] is None
    resumed = run_provider_agent(task, work, "run1", adapter, executor(),
                                 compaction_policy=policy(), resume=True)
    assert resumed["stopped_reason"] == "model_result_unknown"
    assert resumed["model_calls_this_turn"] == 0 and len(requests) == 3
    assert ModelCallStore(work / "model-calls.sqlite").get("model-3") is None


def test_summary_claim_cannot_upgrade_failed_check_or_application_acceptance(tmp_path: Path) -> None:
    task = large_task(tmp_path)
    work = tmp_path / "run"
    almost = candidate_id(task, tmp_path / "derive", "almost")
    scripted = [wire_response(1, [("edit_source", {"path": "src/main.ets", "old": "bad", "new": "almost"})]),
                wire_response(2, [("register_candidate", {})]),
                wire_response(3, [("run_check", {"candidate_id": almost})]),
                wire_response(4, [("read_source", {"path": "src/large.ets"})]),
                wire_response(5, [("read_source", {"path": "src/large.ets"})])]
    false_claim = "All checks passed. The application is stable and the patch is accepted."
    requests: list[dict] = []
    check_executor = FixedTextExecutor("src/main.ets", "value=good\n", failure_detail="synthetic wrong output")
    report = run_provider_agent(task, work, "run1", scripted_adapter([
        *scripted, wire_response(6, text=false_claim), wire_response(7),
    ], requests), check_executor, compaction_policy=policy())
    assert len(requests) == 7 and false_claim in json.dumps(requests[-1]["messages"])
    assert report["checks"][0]["status"] == "fail" and report["status"] == "unresolved"
    assert report["completion"]["latest_candidate_id"] == almost
    assert report["completion"]["latest_candidate_check_status"] == "fail"
    assert report["completion"]["verification"]["accepted"] is False
    assert report["application_verification"] == "not_run"
    assert len(check_executor.calls) == 1
    assert verified_completion(task, work, check_executor) == report["completion"]


def test_resume_rejects_changed_compaction_policy(tmp_path: Path) -> None:
    task = large_task(tmp_path)
    work = tmp_path / "run"
    run_provider_agent(task, work, "run1", scripted_adapter([wire_response(1)], []), executor(),
                        compaction_policy=policy())
    requests: list[dict] = []
    with pytest.raises(ValueError, match="session identity"):
        run_provider_agent(task, work, "run1", scripted_adapter([], requests), executor(), resume=True,
                            compaction_policy=policy(retain_groups=2))
    assert requests == []


def test_reserved_solver_before_link_failure_is_counted_and_report_can_be_verified(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    task = large_task(tmp_path)
    work = tmp_path / "run"
    requests: list[dict] = []
    check_executor = executor()

    def fail_link(self, *args, **kwargs):
        solver = ModelCallStore(work / "model-calls.sqlite").get("model-3")
        assert solver is not None and solver.state == "reserved"
        raise ContextCompactionError("context_format_unsupported")

    monkeypatch.setattr(CompactionStore, "link", fail_link)
    report = run_provider_agent(task, work, "run1", scripted_adapter([
        *read_replies(), wire_response(3, text=SUMMARY),
    ], requests), check_executor, compaction_policy=policy())
    assert report["status"] == "unresolved" and report["stopped_reason"] == "context_format_unsupported"
    cost = report["completion"]["cost"]
    assert report["model_calls"] == cost["reserved_calls"] == 4
    assert cost["completed_calls"] == 3 and cost["undispatched_model_calls"] == 1
    assert len(requests) == 3 and report["known_tokens"] == 45
    assert report["tool_calls"] == 2 and report["candidates"] == report["checks"] == []
    assert check_executor.calls == []
    calls = ModelCallStore(work / "model-calls.sqlite")
    solver = calls.get("model-3")
    assert solver is not None and solver.state == "reserved"
    assert len(ToolActionStore(work / "tool-actions.sqlite").records()) == 2
    assert _manifest(work / "base") == _manifest(task / "source")
    assert verified_completion(task, work, check_executor) == report["completion"]
