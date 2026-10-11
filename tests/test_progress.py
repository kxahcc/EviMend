"""User observations are bounded and private; they never authorize execution."""

from __future__ import annotations

import json
import os
from contextvars import Context
from datetime import UTC, datetime
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.runnables.config import get_executor_for_config

from dfx_agent.actions import ToolActionStore
from dfx_agent.model_calls import (
    ModelCallIntent,
    ModelCallStore,
    invoke_accounted,
    sha256,
)
from dfx_agent.progress import emit_progress, progress_scope, read_progress


def _intent() -> ModelCallIntent:
    return ModelCallIntent(call_id="model-1", task_id="task1", run_id="run1", kind="solver",
        provider_id="synthetic", model_id="model", prompt_sha256=sha256("prompt"),
        tool_schema_sha256=sha256("tools"), parameters_sha256=sha256("parameters"), tool_names=())


def test_no_scope_or_unadmitted_workspace_is_created(tmp_path: Path) -> None:
    work = tmp_path / "missing"
    emit_progress("run_started")
    with progress_scope(work, "run1") as tracker:
        assert not work.exists()
        emit_progress("run_started")
        assert tracker.state == "unknown"
    assert not work.exists()


def test_resume_appends_observations_and_preserves_original_bytes(tmp_path: Path) -> None:
    with progress_scope(tmp_path, "run1"):
        emit_progress("run_started")
        emit_progress("model_completed", call_id="model-1", usage_status="missing")
    before = (tmp_path / "events.jsonl").read_bytes()
    with progress_scope(tmp_path, "run1"):
        emit_progress("resume_started")
    after = (tmp_path / "events.jsonl").read_bytes()
    assert after.startswith(before)
    result = read_progress(tmp_path, after_sequence=1, max_events=1)
    assert result["next_sequence"] == 2 and result["last_sequence"] == 3
    assert result["tracking"] == "available"
    all_events = read_progress(tmp_path)["events"]
    assert [event["sequence"] for event in all_events] == [1, 2, 3]
    for event in all_events:
        assert datetime.fromisoformat(event["observed_at"]).utcoffset() == UTC.utcoffset(datetime.now(UTC))
    original = (tmp_path / "events.jsonl").read_bytes()
    with progress_scope(tmp_path, "another-run") as tracker:
        emit_progress("run_started")
        assert tracker.state == "unknown"
    assert (tmp_path / "events.jsonl").read_bytes() == original


def test_nested_and_empty_contexts_do_not_leak_run_observations(tmp_path: Path) -> None:
    outer, inner = tmp_path / "outer", tmp_path / "inner"
    outer.mkdir()
    inner.mkdir()
    with progress_scope(outer, "outer"):
        emit_progress("run_started")
        with progress_scope(inner, "inner"):
            emit_progress("run_started")
        Context().run(emit_progress, "model_unknown", call_id="model-1")
        emit_progress("run_finished", status="unresolved", accepted=False)
    emit_progress("model_dispatched", call_id="model-2")
    assert [event["event"] for event in read_progress(outer)["events"]] == ["run_started", "run_finished"]
    assert [event["run_id"] for event in read_progress(inner)["events"]] == ["inner"]


def test_sdk_executor_propagates_scope_to_synchronous_tool_workers(tmp_path: Path) -> None:
    # ToolNode uses this SDK executor; its child threads copy ContextVars.
    with progress_scope(tmp_path, "run1"), get_executor_for_config({"max_concurrency": 1}) as executor:
        first = executor.submit(emit_progress, "model_dispatched", call_id="model-1")
        first.result(timeout=5)
        second = executor.submit(emit_progress, "tool_completed", tool_name="read_log", state="completed")
        second.result(timeout=5)
    assert [event["event"] for event in read_progress(tmp_path)["events"]] == ["model_dispatched", "tool_completed"]


def test_capacity_retains_prefix_and_records_tracking_limit(tmp_path: Path) -> None:
    with progress_scope(tmp_path, "run1", max_bytes=1024) as tracker:
        for index in range(100):
            emit_progress("model_dispatched", call_id=f"model-{index}")
        assert tracker.state == "truncated"
    raw = (tmp_path / "events.jsonl").read_bytes()
    result = read_progress(tmp_path, max_bytes=1024)
    assert len(raw) <= 1024 and result["tracking"] == "truncated"
    assert result["events"][-1]["event"] == "tracking_limited"
    with progress_scope(tmp_path, "run1", max_bytes=1024):
        emit_progress("resume_started")
    assert (tmp_path / "events.jsonl").read_bytes() == raw


def test_private_payloads_and_unknown_tool_names_are_not_serialized(tmp_path: Path) -> None:
    secret = "private-log-source-credential"
    with progress_scope(tmp_path, "run1"):
        emit_progress("tool_completed", action_id="action-1", tool_name="read_log", state="completed",
            body=secret, parameters={"content": secret}, exception=secret, reasoning=secret,
            status=secret, total_tokens=True)
        emit_progress("tool_dispatched", tool_name=secret)
    raw = (tmp_path / "events.jsonl").read_bytes()
    assert secret.encode() not in raw
    event = read_progress(tmp_path)["events"][0]
    assert event["tool_name"] == "read_log" and "status" not in event and "total_tokens" not in event


def test_partial_line_is_unknown_and_never_rewritten_or_extended(tmp_path: Path) -> None:
    with progress_scope(tmp_path, "run1"):
        emit_progress("run_started")
    path = tmp_path / "events.jsonl"
    path.write_bytes(path.read_bytes() + b'{"sequence":2')
    original = path.read_bytes()
    result = read_progress(tmp_path)
    assert result["tracking"] == "unknown" and result["partial_line"] is True
    assert len(result["events"]) == 1 and result["last_sequence"] == 1
    with progress_scope(tmp_path, "run1") as tracker:
        emit_progress("resume_started")
        assert tracker.state == "unknown"
    assert path.read_bytes() == original


def test_corrupt_or_private_complete_line_is_not_displayed(tmp_path: Path) -> None:
    with progress_scope(tmp_path, "run1"):
        emit_progress("run_started")
    path = tmp_path / "events.jsonl"
    value = json.loads(path.read_bytes())
    value["private"] = "secret"
    path.write_text(json.dumps(value) + "\n", encoding="utf-8")
    result = read_progress(tmp_path)
    assert result["tracking"] == "unknown" and result["events"] == []


def test_hard_link_target_is_rejected_without_touching_original(tmp_path: Path) -> None:
    original = tmp_path / "original"
    original.write_bytes(b"preserved")
    work = tmp_path / "work"
    work.mkdir()
    os.link(original, work / "events.jsonl")
    with progress_scope(work, "run1") as tracker:
        emit_progress("run_started")
        assert tracker.state == "unknown"
    assert read_progress(work)["tracking"] == "unknown" and original.read_bytes() == b"preserved"


def test_symlink_parent_is_rejected_without_creating_target_file(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation permission is unavailable")
    with progress_scope(link, "run1") as tracker:
        emit_progress("run_started")
        assert tracker.state == "unknown"
    assert not (target / "events.jsonl").exists()


def test_tracking_failure_does_not_repeat_a_completed_model_or_tool(tmp_path: Path) -> None:
    model = ModelCallStore(tmp_path / "models.db")
    actions = ToolActionStore(tmp_path / "actions.db")
    private = "private model response and private tool content"
    model_count = tool_count = 0

    def provide() -> AIMessage:
        nonlocal model_count
        model_count += 1
        return AIMessage(content=private, usage_metadata={"input_tokens": 2, "output_tokens": 1, "total_tokens": 3})

    def act() -> ToolMessage:
        nonlocal tool_count
        tool_count += 1
        return ToolMessage(content=private, tool_call_id="tool1", name="read_log")

    # A missing observation parent makes tracking fail, while durable receipts remain valid.
    with progress_scope(tmp_path / "missing", "run1") as tracker:
        first, _ = invoke_accounted(model, _intent(), provide, max_calls=1)
        result = actions.invoke(action_id="action-1", request_sha256=sha256("request"),
            model_call_id="model-1", tool_name="read_log", tool_call_id="tool1", proposal=None,
            rejection=None, max_tool_calls=1, handler=act)
        assert tracker.state == "unknown"
        assert first.content == private and result.content == private
    with progress_scope(tmp_path, "run1"):
        invoke_accounted(model, _intent(), provide, max_calls=1)
        actions.invoke(action_id="action-1", request_sha256=sha256("request"), model_call_id="model-1",
            tool_name="read_log", tool_call_id="tool1", proposal=None, rejection=None, max_tool_calls=1, handler=act)
    assert model_count == tool_count == 1
    assert [event["event"] for event in read_progress(tmp_path)["events"]] == ["model_replayed", "tool_replayed"]
    assert private.encode() not in (tmp_path / "events.jsonl").read_bytes()
