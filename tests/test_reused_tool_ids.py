"""A provider's per-response tool IDs remain local to each completed batch."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from test_agent_loop import task_snapshot

from dfx_agent.agent import run_provider_agent
from dfx_agent.cloud_retries import CloudRetryPolicy
from dfx_agent.model_provider import (
    CloudChatCompletionsAdapter,
    CloudProviderConfig,
    ProviderSettings,
)
from dfx_agent.verification import FixedTextExecutor
from dfx_agent.working_context_plan import WorkingContextPolicy


@pytest.mark.parametrize("frontier", [False, True], ids=["default-context", "cli-frontier"])
def test_sdk_reused_tool_ids_complete_both_edits_with_working_context(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, frontier: bool) -> None:
    """Run real SDK serialization and Agent middleware, without live I/O."""
    def forbid_transport(*args: Any, **kwargs: Any) -> None:
        pytest.fail("the synthetic SDK test must not construct a live transport")

    monkeypatch.setattr(httpx, "HTTPTransport", forbid_transport)
    monkeypatch.setattr(httpx, "AsyncHTTPTransport", forbid_transport)
    monkeypatch.setenv("DFX_REUSED_ID_TEST_KEY", "synthetic-reused-id-key-never-real")
    requests: list[dict[str, Any]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        number = len(requests)
        message: dict[str, Any] = {"role": "assistant", "content": "done"}
        if number <= 2:
            old, new = ("bad", "almost") if number == 1 else ("almost", "good")
            message = {"role": "assistant", "content": "", "tool_calls": [{
                "id": "call-1", "type": "function", "function": {
                    "name": "edit_source", "arguments": json.dumps({
                        "path": "src/main.ets", "old": old, "new": new})}}]}
        return httpx.Response(200, json={
            "id": f"message-{number}", "model": "synthetic-model", "choices": [{
                "index": 0, "message": message,
                "finish_reason": "tool_calls" if number <= 2 else "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}})

    adapter = CloudChatCompletionsAdapter(
        ProviderSettings(provider_id="synthetic-provider", model_id="synthetic-model", timeout_seconds=30.0),
        CloudProviderConfig(endpoint="https://provider.invalid/v1/chat/completions",
                            credential_env="DFX_REUSED_ID_TEST_KEY", network=CloudRetryPolicy()),
        transport=httpx.MockTransport(handle))
    policy = (WorkingContextPolicy(enabled=True, history_selection="repair_frontier", context_layout="decision_frontier")
              if frontier else WorkingContextPolicy(enabled=True))
    task = task_snapshot(tmp_path)
    work = tmp_path / "run"
    report = run_provider_agent(task, work, "run1", adapter, FixedTextExecutor("src/main.ets", "value=good\n"),
        working_context_policy=policy, max_calls=4, max_known_tokens=1_000_000, max_seconds=120)

    assert len(requests) == report["model_calls"] == 3
    assert report["stopped_reason"] is None and report["adapter_error_reason"] is None
    assert report["tool_calls"] == 2 and report["rejected_tool_calls"] == 0
    assert [item["model_call_id"] for item in report["actions"]] == ["model-1", "model-2"]
    assert len({item["action_id"] for item in report["actions"]}) == 2
    assert (work / "draft/src/main.ets").read_text(encoding="utf-8") == "value=good\n"
    assert [item["tool_call_id"] for item in requests[-1]["messages"] if item["role"] == "tool"] == ["call-1", "call-1"]
    assert len(list((work / "working-context").glob("*.plan.json"))) == 3
    assert report["application_verification"] == "not_run"
