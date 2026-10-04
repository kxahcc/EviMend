"""Actual SDK context gates cannot introduce unaccounted calls or backend writes."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import deepagents.graph as sdk_graph
import pytest
from deepagents.backends import FilesystemBackend
from deepagents.middleware.summarization import SummarizationMiddleware
from langchain_core.messages import AIMessage, HumanMessage
from test_agent_loop import response, task_snapshot
from test_provider_loop import scripted_adapter, wire_response

from dfx_agent import agent
from dfx_agent.actions import TOOL_ACTIONS
from dfx_agent.agent import run_fixed_agent, run_provider_agent
from dfx_agent.applicability_handoff import APPLICABILITY_TOOLS
from dfx_agent.knowledge_handoff import KNOWLEDGE_TOOLS
from dfx_agent.model_calls import ModelCallStore, canonical
from dfx_agent.models import (
    MAX_SERIALIZED_MESSAGES_BYTES,
    AccountedProviderModel,
    FixedResponseModel,
    ModelLimit,
    context_profile,
)
from dfx_agent.policies import PolicyFeatures
from dfx_agent.task import snapshot_digest
from dfx_agent.verification import FixedTextExecutor
from dfx_agent.workspace import _manifest


def _features(enhanced: bool) -> PolicyFeatures:
    return PolicyFeatures(rule_guidance=enhanced, obligation_tracking=enhanced, decision_checks=enhanced)


def _large_source(task: Path) -> None:
    source = task / "source"
    (source / "src/large.ets").write_text("marker=" + "x" * 90_000 + "\n", encoding="utf-8")
    manifest = _manifest(source)
    (task / "source-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    info = json.loads((task / "task.json").read_text(encoding="utf-8"))
    info["source_snapshot_sha256"] = snapshot_digest(manifest)
    (task / "task.json").write_text(json.dumps(info), encoding="utf-8")


def _run(mode: str, task: Path, work: Path, requests: list[dict], *, enhanced: bool = False,
         resume: bool = False, read_large_source: bool = False) -> dict[str, Any]:
    executor = FixedTextExecutor("src/main.ets", "value=good\n")
    options = {"policy_features": _features(enhanced), "resume": resume}
    if mode == "fixed":
        replies = ([response(1, "read_source", {"path": "src/large.ets"}), response(2)]
                   if read_large_source else [response(1)])
        return run_fixed_agent(task, work, "run1", replies, executor, **options)
    scripted = ([wire_response(1, [("read_source", {"path": "src/large.ets"})]), wire_response(2)]
                if read_large_source else [wire_response(1)])
    return run_provider_agent(task, work, "run1", scripted_adapter(scripted, requests), executor, **options)


def _observe_backend_writes(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    writes: list[str] = []
    for name in ("write", "edit", "upload_files"):
        original = getattr(FilesystemBackend, name)

        def observed(self, *args, _original=original, _name=name, **kwargs):
            writes.append(_name)
            return _original(self, *args, **kwargs)

        monkeypatch.setattr(FilesystemBackend, name, observed)
    return writes


@pytest.mark.parametrize("mode", ["fixed", "mock"])
@pytest.mark.parametrize("enhanced", [False, True])
def test_sdk_context_and_large_results_never_summarize_or_write_backend(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str, enhanced: bool) -> None:
    task = task_snapshot(tmp_path)
    _large_source(task)
    before = _manifest(task / "source")
    factories: list[object] = []
    summaries: list[object] = []

    def low_threshold_factory(model, backend, **kwargs):
        middleware = SummarizationMiddleware(model=model, backend=backend, trigger=("messages", 1),
                                             keep=("messages", 1), truncate_args_settings=None)
        factories.append(middleware)
        return middleware

    original_summary = SummarizationMiddleware._create_summary

    def observed_summary(self, messages):
        summaries.append(messages)
        return original_summary(self, messages)

    original_task_message = agent.build_task_message

    def large_task_message(**kwargs):
        value = json.loads(original_task_message(**kwargs))
        # Larger than SDK's default human-message eviction threshold, below our admission cap.
        value["synthetic_context"] = "x" * 210_000
        return canonical(value)

    monkeypatch.setattr(sdk_graph, "create_summarization_middleware", low_threshold_factory)
    monkeypatch.setattr(SummarizationMiddleware, "_create_summary", observed_summary)
    monkeypatch.setattr(agent, "build_task_message", large_task_message)
    writes = _observe_backend_writes(monkeypatch)
    requests: list[dict] = []
    work = tmp_path / "run"
    report = _run(mode, task, work, requests, enhanced=enhanced, read_large_source=True)
    assert factories, "the actual SDK default summarization slot was constructed before exclusion"
    assert summaries == [] and writes == []
    assert report["model_calls"] == 2 and report["tool_calls"] == 1
    assert report["stopped_reason"] is None
    assert set(report["offered_tools"]) == (set(TOOL_ACTIONS) - KNOWLEDGE_TOOLS - APPLICABILITY_TOOLS)
    assert _manifest(work / "base") == before
    assert not (work / "base/conversation_history").exists()
    assert not (work / "base/large_tool_results").exists()
    assert not (work / "base/large_human_messages").exists()
    session = json.loads((work / "session.json").read_text(encoding="utf-8"))
    assert session["context_profile"] == context_profile()
    assert report["completion"]["verification"]["application_verification"] == "not_run"
    if mode == "mock":
        assert len(requests) == 2
        assert len(requests[0]["messages"][-1]["content"]) > 200_000
        assert len(requests[1]["messages"][-1]["content"]) > 80_000


@pytest.mark.parametrize("mode", ["fixed", "mock"])
@pytest.mark.parametrize("encoding", ["ascii", "utf8"])
def test_context_limit_rejects_before_reservation_or_transport(
        tmp_path: Path, mode: str, encoding: str) -> None:
    requests: list[dict] = []
    store = ModelCallStore(tmp_path / "model-calls.sqlite")
    common = {"call_store": store, "task_id": "task1", "run_id": "run1", "max_calls": 2,
              "max_known_tokens": 4000, "deadline": time.monotonic() + 60}
    model = (FixedResponseModel(responses=[AIMessage(content="done")], **common) if mode == "fixed" else
             AccountedProviderModel(responses=[], adapter=scripted_adapter([wire_response(1)], requests), **common))
    content = ("x" * MAX_SERIALIZED_MESSAGES_BYTES if encoding == "ascii" else
               "界" * (MAX_SERIALIZED_MESSAGES_BYTES // 3 + 1))
    with pytest.raises(ModelLimit, match="^context_limit$"):
        model._generate([HumanMessage(content=content)])
    assert store.usage_summary("task1", "run1")["reserved_calls"] == 0
    assert model.turn_calls == model.calls == 0 and requests == []


@pytest.mark.parametrize("mode", ["fixed", "mock"])
@pytest.mark.parametrize("enhanced", [False, True])
def test_sdk_long_startup_is_unresolved_without_call_or_offload(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str, enhanced: bool) -> None:
    task = task_snapshot(tmp_path)
    before = _manifest(task / "source")
    monkeypatch.setattr(agent, "build_task_message", lambda **kwargs: "x" * MAX_SERIALIZED_MESSAGES_BYTES)
    writes = _observe_backend_writes(monkeypatch)
    requests: list[dict] = []
    work = tmp_path / "run"
    report = _run(mode, task, work, requests, enhanced=enhanced)
    assert report["status"] == "unresolved" and report["stopped_reason"] == "context_limit"
    assert report["model_calls"] == report["tool_calls"] == 0 and requests == [] and writes == []
    assert report["completion"]["cost"]["reserved_calls"] == 0
    assert _manifest(work / "base") == before


@pytest.mark.parametrize("mode", ["fixed", "mock"])
@pytest.mark.parametrize("field", ["max_serialized_messages_bytes", "automatic_summarization", "human_message_offload",
                                 "tool_result_offload", "implicit_model_retries"])
def test_resume_rejects_changed_context_profile_before_new_calls(tmp_path: Path, mode: str, field: str) -> None:
    task = task_snapshot(tmp_path)
    work = tmp_path / "run"
    requests: list[dict] = []
    _run(mode, task, work, requests)
    path = work / "session.json"
    session = json.loads(path.read_text(encoding="utf-8"))
    assert session["context_profile"] == context_profile()
    value = session["context_profile"][field]
    session["context_profile"][field] = not value if isinstance(value, bool) else value - 1
    path.write_text(json.dumps(session), encoding="utf-8")
    before = ModelCallStore(work / "model-calls.sqlite").usage_summary("task1", "run1")
    attempted: list[dict] = []
    with pytest.raises(ValueError, match="session identity"):
        _run(mode, task, work, attempted, resume=True)
    assert attempted == []
    assert ModelCallStore(work / "model-calls.sqlite").usage_summary("task1", "run1") == before


@pytest.mark.parametrize("mode", ["fixed", "mock"])
def test_non_utf8_context_is_rejected_before_accounting(tmp_path: Path, mode: str) -> None:
    requests: list[dict] = []
    store = ModelCallStore(tmp_path / "model-calls.sqlite")
    common = {"call_store": store, "task_id": "task1", "run_id": "run1", "max_calls": 2,
              "max_known_tokens": 4000, "deadline": time.monotonic() + 60}
    model = (FixedResponseModel(responses=[AIMessage(content="done")], **common) if mode == "fixed" else
             AccountedProviderModel(responses=[], adapter=scripted_adapter([wire_response(1)], requests), **common))
    with pytest.raises(ModelLimit, match="^context_format_unsupported$"):
        model._generate([HumanMessage(content="\ud800")])
    assert store.usage_summary("task1", "run1")["reserved_calls"] == 0 and requests == []
