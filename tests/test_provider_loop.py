"""SDK/provider handoff over MockTransport; scripted replies do not test reasoning."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from test_agent_loop import candidate_id, task_snapshot
from test_task_handoff import derive, multi_file_task, receipts

from dfx_agent import models
from dfx_agent.actions import OFFLINE_CAPABILITIES, TOOL_ACTIONS
from dfx_agent.agent import run_provider_agent
from dfx_agent.model_calls import ModelCallStore, canonical, sha256
from dfx_agent.model_provider import (
    MOCK_ENDPOINT,
    ChatCompletionsAdapter,
    ProviderSettings,
)
from dfx_agent.policies import PolicyFeatures
from dfx_agent.task import verify_task
from dfx_agent.task_strategy import build_system_prompt
from dfx_agent.verification import FixedTextExecutor

DEFAULT_USAGE = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}


def settings(**overrides: Any) -> ProviderSettings:
    return ProviderSettings.model_validate({
        "provider_id": "mock-provider", "model_id": "synthetic-model",
        "max_output_tokens": 128, "timeout_seconds": 1.0, **overrides,
    })


def wire_response(index: int, calls: list[tuple[str, dict | str]] | None = None, *,
                  usage: object = DEFAULT_USAGE, text: str = "done") -> dict:
    message: dict = {"role": "assistant", "content": "" if calls else text}
    if calls:
        message["tool_calls"] = [{
            "id": f"call-{index}-{position}", "type": "function",
            "function": {"name": name, "arguments": arguments if isinstance(arguments, str)
                         else json.dumps(arguments)},
        } for position, (name, arguments) in enumerate(calls)]
    result = {"id": f"wire-{index}", "model": "synthetic-model", "choices": [{
        "index": 0, "finish_reason": "tool_calls" if calls else "stop", "message": message,
    }]}
    if usage is not None:
        result["usage"] = usage
    return result


def scripted_adapter(scripted: list[dict], requests: list[dict], *,
                     configuration: ProviderSettings | None = None) -> ChatCompletionsAdapter:
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == MOCK_ENDPOINT
        assert request.method == "POST" and "authorization" not in request.headers
        requests.append(json.loads(request.content))
        assert len(requests) <= len(scripted), "unexpected additional provider send"
        return httpx.Response(200, json=scripted[len(requests) - 1])

    return ChatCompletionsAdapter(configuration or settings(), httpx.MockTransport(handler))


def test_wire_task_strategy_tools_and_accounted_intent_are_identical(tmp_path: Path) -> None:
    task = task_snapshot(tmp_path)
    work = tmp_path / "run"
    requests: list[dict] = []
    adapter = scripted_adapter([wire_response(1)], requests)
    report = run_provider_agent(task, work, "run1", adapter,
                                FixedTextExecutor("src/main.ets", "value=good\n"))
    assert report["schema_version"] == 4
    assert report["model_calls"] == 1 and report["known_tokens"] == 15, report["adapter_error_reason"]
    assert report["uncertain_model_calls"] == 0
    assert report["completion"]["latest_candidate_id"] is None
    assert report["completion"]["cost"]["total_tokens"] == 15
    assert report["completion"]["suggested_status"] == report["status"]
    assert set(report["offered_tools"]) == set(TOOL_ACTIONS)
    body = requests[0]
    assert body["messages"][0]["role"] == "system"
    assert build_system_prompt(PolicyFeatures()) in body["messages"][0]["content"]
    task_message = json.loads(body["messages"][-1]["content"])
    assert task_message["source_snapshot_sha256"] == verify_task(task)["source_snapshot_sha256"]
    assert task_message["capabilities"] == sorted(OFFLINE_CAPABILITIES)
    assert task_message["task_id"] == "task1" and task_message["run_id"] == "run1"
    assert {item["function"]["name"] for item in body["tools"]} == set(TOOL_ACTIONS)
    assert not {"execute", "task", "write_file", "grep"} & set(report["offered_tools"])
    record = ModelCallStore(work / "model-calls.sqlite").get("model-1")
    assert record is not None and record.state == "completed"
    assert record.intent.provider_id == adapter.settings.provider_id
    assert record.intent.model_id == adapter.settings.model_id
    assert record.intent.prompt_sha256 == sha256(canonical(body["messages"]))
    assert record.intent.tool_schema_sha256 == sha256(canonical(body["tools"]))
    assert record.intent.parameters_sha256 == sha256(canonical({
        "wire_parameters": {key: value for key, value in body.items() if key not in ("messages", "tools")},
        "body_sha256": sha256(canonical(body)), "profile_sha256": sha256(canonical(adapter.profile())),
    }))
    session = json.loads((work / "session.json").read_text(encoding="utf-8"))
    assert session["schema_version"] == 7 and session["model_profile"] == adapter.profile()


def test_provider_search_diagnosis_and_failed_candidate_revision(tmp_path: Path) -> None:
    task = multi_file_task(tmp_path)
    almost = derive(task, tmp_path / "derive1", "almost")
    good = derive(task, tmp_path / "derive2", "good")
    scripted = [
        wire_response(1, [("search_source", {"query": "makeValue"})]),
        wire_response(2, [("read_source", {"path": "src/provider.ets"})]),
        wire_response(3, [("record_diagnosis", {
            "path": "src/provider.ets", "line": 1, "reason": "bad producer value",
        })]),
        wire_response(4, [("edit_source", {"path": "src/provider.ets", "old": "bad", "new": "almost"})]),
        wire_response(5, [("register_candidate", {"diagnosis_id": "diagnosis-1"})]),
        wire_response(6, [("run_check", {"candidate_id": almost})]),
        wire_response(7, [("edit_source", {"path": "src/provider.ets", "old": "bad", "new": "good"})]),
        wire_response(8, [("register_candidate", {"diagnosis_id": "diagnosis-1"})]),
        wire_response(9, [("run_check", {"candidate_id": good})]), wire_response(10),
    ]
    requests: list[dict] = []
    work = tmp_path / "run"
    executor = FixedTextExecutor("src/provider.ets", "export function makeValue() { return good; }\n",
                                 failure_detail="synthetic compiler rejected almost")
    report = run_provider_agent(task, work, "run1", scripted_adapter(scripted, requests), executor)
    assert report["status"] == "offline_checked" and report["application_verification"] == "not_run"
    assert report["candidates"] == [almost, good]
    assert [check["status"] for check in report["checks"]] == ["fail", "pass"]
    assert report["model_calls"] == len(requests) == 10 and report["known_tokens"] == 150
    assert report["tool_calls"] == 9 and report["rejected_tool_calls"] == 0
    assert report["completion"]["latest_candidate_id"] == good
    assert report["completion"]["latest_candidate_check_status"] == "pass"
    assert report["completion"]["localization"]["initial"]["diagnosis_id"] == "diagnosis-1"
    assert report["completion"]["verification"]["accepted"] is False
    assert report["completion"]["cost"]["total_tokens"] == 150
    search = json.loads(receipts(work)[0]["content"])
    assert [hit["path"] for hit in search["matches"]] == ["src/main.ets", "src/provider.ets"]
    feedback = requests[6]["messages"][-1]
    assert feedback["role"] == "tool" and "synthetic compiler rejected almost" in feedback["content"]
    assert (work / "base/src/provider.ets").read_text() == "export function makeValue() { return bad; }\n"
    assert len(executor.calls) == 2


def test_provider_baseline_and_enhanced_share_tools_and_budgets(tmp_path: Path) -> None:
    task = task_snapshot(tmp_path)
    offered = []
    sessions = []
    for enhanced in (False, True):
        requests: list[dict] = []
        work = tmp_path / str(enhanced)
        report = run_provider_agent(task, work, "run1", scripted_adapter([wire_response(1)], requests),
                                    FixedTextExecutor("src/main.ets", "value=good\n"), max_calls=1,
                                    max_known_tokens=30, max_candidates=2, max_tool_calls=3,
                                    policy_features=PolicyFeatures(rule_guidance=enhanced,
                                        obligation_tracking=enhanced, decision_checks=enhanced))
        assert set(report["offered_tools"]) == set(TOOL_ACTIONS)
        assert report["model_calls"] == len(requests) == 1
        offered.append(requests[0]["tools"])
        sessions.append(json.loads((work / "session.json").read_text(encoding="utf-8")))
    assert offered[0] == offered[1]
    for key in ("max_calls", "max_known_tokens", "max_candidates", "max_tool_calls", "max_seconds", "capabilities"):
        assert sessions[0][key] == sessions[1][key]
    assert sessions[0]["system_prompt_sha256"] != sessions[1]["system_prompt_sha256"]


@pytest.mark.parametrize("failure", ["timeout", "http", "invalid_json"])
def test_dispatched_provider_failure_is_unknown_and_resume_never_resends(
        tmp_path: Path, failure: str) -> None:
    task = task_snapshot(tmp_path)
    work = tmp_path / "run"
    sends = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal sends
        sends += 1
        if failure == "timeout":
            raise httpx.ReadTimeout("secret-provider-text", request=request)
        if failure == "http":
            return httpx.Response(503, text="secret-provider-text")
        return httpx.Response(200, text="{secret-provider-text")

    adapter = ChatCompletionsAdapter(settings(), httpx.MockTransport(handler))
    executor = FixedTextExecutor("src/main.ets", "value=good\n")
    first = run_provider_agent(task, work, "run1", adapter, executor)
    assert first["status"] == "unresolved" and first["stopped_reason"] == "model_result_unknown"
    assert first["model_calls"] == first["uncertain_model_calls"] == sends == 1
    record = ModelCallStore(work / "model-calls.sqlite").get("model-1")
    assert record is not None and record.state == "unknown" and record.observation is None
    second = run_provider_agent(task, work, "run1", adapter, executor, resume=True)
    assert second["stopped_reason"] == "model_result_unknown"
    assert second["model_calls_this_turn"] == 0 and sends == 1
    assert "secret-provider-text" not in (work / "agent-report.json").read_text(encoding="utf-8")


def test_completed_provider_response_replays_after_pre_checkpoint_crash(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    task = task_snapshot(tmp_path)
    work = tmp_path / "run"
    requests: list[dict] = []
    original = models.invoke_accounted

    class SyntheticCrash(RuntimeError):
        pass

    def crash_after_completion(*args, **kwargs):
        original(*args, **kwargs)
        raise SyntheticCrash("after durable provider response")

    monkeypatch.setattr(models, "invoke_accounted", crash_after_completion)
    executor = FixedTextExecutor("src/main.ets", "value=good\n")
    with pytest.raises(SyntheticCrash, match="after durable provider response"):
        run_provider_agent(task, work, "run1", scripted_adapter([wire_response(1)], requests), executor)
    assert ModelCallStore(work / "model-calls.sqlite").usage_summary("task1", "run1")["completed_calls"] == 1
    monkeypatch.setattr(models, "invoke_accounted", original)
    no_resend: list[dict] = []
    report = run_provider_agent(task, work, "run1", scripted_adapter([], no_resend), executor, resume=True)
    assert len(requests) == 1 and no_resend == []
    assert report["model_calls"] == 1 and report["model_calls_this_turn"] == 0
    assert report["known_tokens"] == 15 and report["known_tokens_this_turn"] == 0
    assert report["uncertain_model_calls"] == 0


@pytest.mark.parametrize("usage", [None, {"prompt_tokens": "10", "completion_tokens": 5, "total_tokens": 15}])
def test_missing_or_invalid_usage_preserves_completed_response_and_unknown_cost(
        tmp_path: Path, usage: object) -> None:
    task = task_snapshot(tmp_path)
    work = tmp_path / "run"
    requests: list[dict] = []
    report = run_provider_agent(task, work, "run1", scripted_adapter([wire_response(1, usage=usage)], requests),
                                FixedTextExecutor("src/main.ets", "value=good\n"))
    store = ModelCallStore(work / "model-calls.sqlite")
    record = store.get("model-1")
    assert record is not None and record.state == "completed" and record.observation is not None
    expected = "missing" if usage is None else "invalid"
    assert record.observation.usage_status == expected and record.observation.usage is None
    assert report["model_calls"] == len(requests) == 1 and report["known_tokens"] == 0
    assert report["missing_usage_calls"] == 1 and report["uncertain_model_calls"] == 0
    assert report["completion"]["cost"]["total_tokens"] is None
    assert report["completion"]["cost"]["usage_complete"] is False
    if usage is not None:
        assert record.observation.usage_raw_sha256 == sha256(canonical(usage))
        message, _ = store.replay(record.intent)
        assert message.response_metadata["dfx_provider_usage_raw"] == usage


def test_known_token_budget_prevents_next_provider_send_after_tool_receipt(tmp_path: Path) -> None:
    task = task_snapshot(tmp_path)
    work = tmp_path / "run"
    requests: list[dict] = []
    report = run_provider_agent(task, work, "run1", scripted_adapter([
        wire_response(1, [("search_source", {"query": "bad"})]),
    ], requests), FixedTextExecutor("src/main.ets", "value=good\n"), max_known_tokens=15)
    assert report["status"] == "budget_exhausted" and report["stopped_reason"] == "model_budget"
    assert report["model_calls"] == len(requests) == 1 and report["known_tokens"] == 15
    assert report["tool_calls"] == 1 and report["rejected_tool_calls"] == 0
    assert json.loads(receipts(work)[0]["content"])["matches"][0]["path"] == "src/main.ets"


@pytest.mark.parametrize("invalid", [
    ("search_source", "[]"), ("search_source", "{broken"), ("unknown_tool", "{}"),
])
def test_mixed_valid_and_invalid_calls_stop_whole_response_without_edit(
        tmp_path: Path, invalid: tuple[str, str]) -> None:
    task = task_snapshot(tmp_path)
    work = tmp_path / "run"
    requests: list[dict] = []
    report = run_provider_agent(task, work, "run1", scripted_adapter([
        wire_response(1, [("edit_source", {"path": "src/main.ets", "old": "bad", "new": "good"}), invalid]),
    ], requests), FixedTextExecutor("src/main.ets", "value=good\n"))
    assert report["stopped_reason"] == "provider_format_error"
    assert report["status"] != "offline_checked"
    assert report["model_calls"] == len(requests) == 1 and report["known_tokens"] == 15
    assert report["tool_calls"] == 0 and report["candidates"] == []
    assert (work / "base/src/main.ets").read_text() == "value=bad\n"
    assert not (work / "draft").exists()
    record = ModelCallStore(work / "model-calls.sqlite").get("model-1")
    assert record is not None and record.state == "completed" and record.observation is not None
    assert record.observation.format_status == "format_error"


@pytest.mark.parametrize("change", ["model_id", "max_output_tokens", "old_schema"])
def test_resume_rejects_changed_provider_identity_or_old_schema_before_send(
        tmp_path: Path, change: str) -> None:
    task = task_snapshot(tmp_path)
    work = tmp_path / "run"
    requests: list[dict] = []
    executor = FixedTextExecutor("src/main.ets", "value=good\n")
    run_provider_agent(task, work, "run1", scripted_adapter([wire_response(1)], requests), executor)
    configuration = settings()
    if change == "old_schema":
        session = json.loads((work / "session.json").read_text(encoding="utf-8"))
        session["schema_version"] = 6
        (work / "session.json").write_text(json.dumps(session), encoding="utf-8")
    else:
        configuration = settings(**{change: "different-model" if change == "model_id" else 129})
    attempted: list[dict] = []
    with pytest.raises(ValueError, match="session identity"):
        run_provider_agent(task, work, "run1", scripted_adapter([], attempted, configuration=configuration),
                            executor, resume=True)
    assert len(requests) == 1 and attempted == []
    assert ModelCallStore(work / "model-calls.sqlite").usage_summary("task1", "run1")["reserved_calls"] == 1


def test_last_allowed_provider_response_can_check_candidate_before_budget_stop(tmp_path: Path) -> None:
    task = task_snapshot(tmp_path)
    good = candidate_id(task, tmp_path / "derive", "good")
    work = tmp_path / "run"
    requests: list[dict] = []
    report = run_provider_agent(task, work, "run1", scripted_adapter([
        wire_response(1, [("edit_source", {"path": "src/main.ets", "old": "bad", "new": "good"})]),
        wire_response(2, [("register_candidate", {})]), wire_response(3, [("run_check", {"candidate_id": good})]),
    ], requests), FixedTextExecutor("src/main.ets", "value=good\n"), max_calls=3)
    assert report["model_calls"] == len(requests) == 3 and report["tool_calls"] == 3
    assert report["checks"][0]["status"] == "pass"
    assert report["application_verification"] == "not_run"
    assert report["stopped_reason"] == "model_budget"
