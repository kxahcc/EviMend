"""One actual stopped SDK repair fixture, reused by learning tests."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from test_feedback_integration import feedback_approval, seed
from test_knowledge_contracts import guidance

from dfx_agent.evidence_io import local_evidence_path
from dfx_agent.feedback import FeedbackStore
from dfx_agent.learning import learning_root_binding
from dfx_agent.learning_contracts import HostLearningApproval
from dfx_agent.metrics import parse_evaluation_manifest
from dfx_agent.model_calls import canonical
from dfx_agent.model_provider import ChatCompletionsAdapter, ProviderSettings


@pytest.fixture(scope="session")
def learning_feedback(tmp_path_factory: pytest.TempPathFactory):
    value = seed(tmp_path_factory.mktemp("lf"))
    manifest = parse_evaluation_manifest(value.raw)
    approval = feedback_approval(manifest)
    store = FeedbackStore(tmp_path_factory.mktemp("lc"))
    capsule = store.capture(manifest, value.root, approval=approval)
    return store, capsule, approval, value


def approval_for(captured, root: Path, **changes: Any) -> HostLearningApproval:
    _, capsule, approval, _ = captured
    value = {"approval_id": "learning-host", "operation_id": "learning-operation", "run_id": "learning-run",
        "capsule_id": capsule.capsule_id, "feedback_approval": approval.model_dump(mode="json"),
        "root_binding_sha256": learning_root_binding(root),
        "provider_settings": ProviderSettings(provider_id="mock", model_id="mock-learning", max_output_tokens=1024).model_dump(mode="json"),
        "max_calls": 4, "max_known_tokens": 1000}
    value.update(changes)
    return HostLearningApproval.model_validate_json(canonical(value))


def response(content: str | None = None, *, usage: bool = True, tools: bool = False) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant", "content": canonical(guidance().model_dump(mode="json")) if content is None else content}
    if tools:
        message["tool_calls"] = [{"id": "forbidden-tool", "type": "function",
            "function": {"name": "edit_source", "arguments": "{}"}}]
    result: dict[str, Any] = {"id": "mock-learning-response", "model": "mock-learning", "choices": [
        {"index": 0, "finish_reason": "tool_calls" if tools else "stop", "message": message}]}
    if usage:
        result["usage"] = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
    return result


def adapter_for(approval: HostLearningApproval, replies: list[dict[str, Any]], sent: list[dict[str, Any]],
                *, callback: Callable[[httpx.Request], httpx.Response] | None = None) -> ChatCompletionsAdapter:
    def send(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        if callback is not None:
            return callback(request)
        return httpx.Response(200, json=replies.pop(0))
    return ChatCompletionsAdapter(approval.provider_settings, httpx.MockTransport(send))


def inventory(root: Path) -> dict[str, bytes]:
    root = local_evidence_path(root)
    return {path.relative_to(root).as_posix(): path.read_bytes() for path in root.rglob("*") if path.is_file()}
