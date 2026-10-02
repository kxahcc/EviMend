"""Stopped synthetic evidence can be audited without database or execution effects."""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Callable
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from test_agent_loop import candidate_id, response, task_snapshot
from test_compaction_loop import SUMMARY, large_task, policy
from test_model_calls import intent

from dfx_agent.actions import ToolActionStore
from dfx_agent.agent import run_fixed_agent
from dfx_agent.evidence_io import open_readonly_database
from dfx_agent.model_calls import ModelCallStore, invoke_accounted, sha256
from dfx_agent.reporting import read_completion
from dfx_agent.verification import CheckLedger, FixedTextExecutor, ReplayOnlyExecutor
from dfx_agent.workspace import CandidateWorkspace


def tree_bytes(root: Path) -> dict[str, tuple[bool, int, bytes]]:
    return {path.relative_to(root).as_posix(): (path.is_dir(), path.stat().st_mtime_ns,
                                               b"" if path.is_dir() else path.read_bytes())
            for path in root.rglob("*")}


def completed_calls(root: Path) -> tuple[ModelCallStore, AIMessage]:
    store = ModelCallStore(root / "model-calls.sqlite")
    message = AIMessage(content="synthetic answer", usage_metadata={
        "input_tokens": 12, "output_tokens": 4, "total_tokens": 16})
    invoke_accounted(store, intent(), lambda: message, max_calls=2)
    store.reserve(intent("pending"), max_calls=2)
    return store, message


def completed_actions(root: Path) -> tuple[ToolActionStore, ToolMessage]:
    store = ToolActionStore(root / "tool-actions.sqlite")
    message = ToolMessage(content="synthetic receipt", tool_call_id="tool1", name="read_source")
    store.invoke(action_id="action1", request_sha256=sha256("request"), model_call_id="call1",
                 tool_name="read_source", tool_call_id="tool1", proposal=None, rejection=None,
                 max_tool_calls=2, handler=lambda: message)
    return store, message


def test_readonly_call_records_replay_and_usage_preserve_tree(tmp_path: Path) -> None:
    original, message = completed_calls(tmp_path)
    expected_records = original.records("task1", "run1")
    expected_usage = original.usage_summary("task1", "run1")
    before = tree_bytes(tmp_path)
    store = ModelCallStore(original.path, read_only=True)
    assert store.get("call1") == expected_records[0]
    assert store.get("absent") is None
    assert store.records("task1", "run1") == expected_records
    assert store.records("task1", "run1", kind="solver") == expected_records
    assert store.records("other", "run1") == []
    assert store.replay(intent())[0] == message
    assert store.usage_summary("task1", "run1") == expected_usage
    assert tree_bytes(tmp_path) == before


def test_readonly_actions_and_receipts_preserve_tree(tmp_path: Path) -> None:
    original, message = completed_actions(tmp_path)
    expected_records = original.records()
    before = tree_bytes(tmp_path)
    store = ToolActionStore(original.path, read_only=True)
    assert store.records() == expected_records
    assert store.receipt("action1") == message
    assert tree_bytes(tmp_path) == before


@pytest.mark.parametrize("method", ["reserve", "dispatch", "complete", "mark_unknown", "accounted"])
def test_readonly_model_mutations_reject_before_any_effect(tmp_path: Path, method: str) -> None:
    original, message = completed_calls(tmp_path)
    store = ModelCallStore(original.path, read_only=True)
    before = tree_bytes(tmp_path)

    def forbidden_provider() -> AIMessage:
        pytest.fail("read-only accounting must not call a provider")

    operations: dict[str, Callable[[], Any]] = {
        "reserve": lambda: store.reserve(intent(), max_calls=2),
        "dispatch": lambda: store.dispatch("pending"),
        "complete": lambda: store.complete(intent(), message),
        "mark_unknown": lambda: store.mark_unknown("pending", "lost"),
        "accounted": lambda: invoke_accounted(store, intent(), forbidden_provider, max_calls=2),
    }
    with pytest.raises(ValueError, match="read-only"):
        operations[method]()
    assert tree_bytes(tmp_path) == before


@pytest.mark.parametrize("method", ["invoke", "complete"])
def test_readonly_tool_mutations_reject_before_handler(tmp_path: Path, method: str) -> None:
    original, message = completed_actions(tmp_path)
    store = ToolActionStore(original.path, read_only=True)
    before = tree_bytes(tmp_path)

    def forbidden_handler() -> ToolMessage:
        pytest.fail("read-only actions must not execute a tool")

    with pytest.raises(ValueError, match="read-only"):
        if method == "invoke":
            store.invoke(action_id="action1", request_sha256=sha256("request"), model_call_id="call1",
                         tool_name="read_source", tool_call_id="tool1", proposal=None, rejection=None,
                         max_tool_calls=2, handler=forbidden_handler)
        else:
            store.complete("action1", message)
    assert tree_bytes(tmp_path) == before


@pytest.mark.parametrize("opener", [open_readonly_database,
                                   lambda path: ModelCallStore(path, read_only=True),
                                   lambda path: ToolActionStore(path, read_only=True)])
def test_readonly_missing_database_is_not_created(tmp_path: Path, opener: Callable) -> None:
    before = tree_bytes(tmp_path)
    with pytest.raises(ValueError, match="missing"):
        opener(tmp_path / "missing.sqlite")
    assert tree_bytes(tmp_path) == before


@pytest.mark.parametrize("store_type", [ModelCallStore, ToolActionStore])
def test_readonly_does_not_create_missing_table(tmp_path: Path, store_type: Any) -> None:
    path = tmp_path / "empty.sqlite"
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("CREATE TABLE other(value TEXT)")
        connection.commit()
    before = tree_bytes(tmp_path)
    with pytest.raises(ValueError, match="table is missing"):
        store_type(path, read_only=True)
    assert tree_bytes(tmp_path) == before


@pytest.mark.parametrize("suffix", ["-wal", "-journal"])
def test_nonempty_journal_is_rejected_without_checkpoint(tmp_path: Path, suffix: str) -> None:
    original, _ = completed_calls(tmp_path)
    sidecar = original.path.with_name(original.path.name + suffix)
    sidecar.write_bytes(b"unmerged synthetic journal")
    before = tree_bytes(tmp_path)
    with pytest.raises(ValueError, match="unmerged journal"):
        ModelCallStore(original.path, read_only=True)
    assert tree_bytes(tmp_path) == before


def test_committed_live_wal_is_not_silently_ignored(tmp_path: Path) -> None:
    path = tmp_path / "live.sqlite"
    with closing(sqlite3.connect(path)) as writer:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("CREATE TABLE current(value TEXT)")
        writer.execute("INSERT INTO current VALUES ('only-in-wal')")
        writer.commit()
        wal = path.with_name(path.name + "-wal")
        assert wal.stat().st_size > 0
        before = tree_bytes(tmp_path)
        with pytest.raises(ValueError, match="unmerged journal"):
            open_readonly_database(path)
        assert tree_bytes(tmp_path) == before


def test_immutable_query_only_connection_does_not_write_sidecars(tmp_path: Path) -> None:
    original, _ = completed_calls(tmp_path)
    for suffix in ("-wal", "-journal", "-shm"):
        original.path.with_name(original.path.name + suffix).write_bytes(b"")
    before = tree_bytes(tmp_path)
    with closing(open_readonly_database(original.path)) as connection:
        assert connection.row_factory is sqlite3.Row
        assert connection.execute("PRAGMA query_only").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM model_calls").fetchone()[0] == 2
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            connection.execute("DELETE FROM model_calls")
    assert tree_bytes(tmp_path) == before


@pytest.mark.parametrize("suffix", ["", "-wal", "-journal", "-shm"])
def test_database_or_sidecar_with_shared_inode_is_rejected(tmp_path: Path, suffix: str) -> None:
    original, _ = completed_calls(tmp_path)
    target = original.path.with_name(original.path.name + suffix)
    if suffix:
        target.write_bytes(b"")
    os.link(target, tmp_path / "shared")
    before = tree_bytes(tmp_path)
    with pytest.raises(ValueError, match="independent regular file"):
        open_readonly_database(original.path)
    assert tree_bytes(tmp_path) == before


@pytest.mark.parametrize("linked_target", ["database", "parent", "sidecar"])
def test_linked_database_chain_or_sidecar_is_rejected(tmp_path: Path, linked_target: str) -> None:
    parent = tmp_path / "original"
    parent.mkdir()
    original, _ = completed_calls(parent)
    path = original.path
    try:
        if linked_target == "database":
            path = tmp_path / "linked.sqlite"
            path.symlink_to(original.path)
        elif linked_target == "parent":
            linked = tmp_path / "linked"
            linked.symlink_to(parent, target_is_directory=True)
            path = linked / original.path.name
        else:
            original.path.with_name(original.path.name + "-shm").symlink_to(original.path)
    except OSError as exc:
        pytest.skip(f"symbolic link capability unavailable: {exc}")
    with pytest.raises(ValueError, match="linked"):
        open_readonly_database(path)


def test_readonly_check_directory_is_not_created(tmp_path: Path) -> None:
    task = task_snapshot(tmp_path)
    workspace = CandidateWorkspace(task, tmp_path / "run", "run1")
    before = tree_bytes(tmp_path)
    with pytest.raises(ValueError, match="missing"):
        CheckLedger(workspace, ReplayOnlyExecutor(sha256("protocol")), read_only=True)
    assert not (workspace.root / "checks").exists()
    assert tree_bytes(tmp_path) == before


def test_readonly_check_path_must_be_a_normal_directory(tmp_path: Path) -> None:
    task = task_snapshot(tmp_path)
    workspace = CandidateWorkspace(task, tmp_path / "run", "run1")
    (workspace.root / "checks").write_bytes(b"not a directory")
    before = tree_bytes(tmp_path)
    with pytest.raises(ValueError, match="directory"):
        CheckLedger(workspace, ReplayOnlyExecutor(sha256("protocol")), read_only=True)
    assert tree_bytes(tmp_path) == before


def test_replay_only_executor_rejects_invalid_protocol_identity() -> None:
    with pytest.raises(ValueError):
        ReplayOnlyExecutor("not-a-hash")


def test_completion_readback_cannot_call_model_tool_or_application(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    task = task_snapshot(tmp_path)
    good = candidate_id(task, tmp_path / "derive", "good")
    work = tmp_path / "run"
    executor = FixedTextExecutor("src/main.ets", "value=good\n")
    report = run_fixed_agent(task, work, "run1", [
        response(1, "record_diagnosis", {"path": "src/main.ets", "line": 1, "reason": "synthetic value"}),
        response(2, "edit_source", {"path": "src/main.ets", "old": "bad", "new": "good"}),
        response(3, "register_candidate", {"diagnosis_id": "diagnosis-1"}),
        response(4, "run_check", {"candidate_id": good}), response(5),
    ], executor)
    before = tree_bytes(tmp_path)

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("completion audit must not execute a model, tool or application")

    monkeypatch.setattr("dfx_agent.models.FixedResponseModel._generate", forbidden)
    monkeypatch.setattr(ToolActionStore, "invoke", forbidden)
    monkeypatch.setattr(FixedTextExecutor, "check", forbidden)
    workspace = CandidateWorkspace.reopen(task, work, "run1")
    replay = ReplayOnlyExecutor(executor.protocol_sha256)
    ledger = CheckLedger(workspace, replay, read_only=True)
    calls = ModelCallStore(work / "model-calls.sqlite", read_only=True)
    actions = ToolActionStore(work / "tool-actions.sqlite", read_only=True)
    assert read_completion(workspace, ledger, calls, actions) == report["completion"]
    assert ledger.completed(good) is not None
    with pytest.raises(ValueError, match="read-only"):
        ledger.run(good)
    with pytest.raises(ValueError, match="cannot run"):
        replay.check(key="unused", source=workspace.base)
    assert tree_bytes(tmp_path) == before


def test_readonly_compaction_audit_preserves_all_frozen_artifacts(tmp_path: Path) -> None:
    task = large_task(tmp_path)
    work = tmp_path / "run"
    executor = FixedTextExecutor("src/main.ets", "value=good\n")
    report = run_fixed_agent(task, work, "run1", [
        response(1, "read_source", {"path": "src/large.ets"}),
        response(2, "read_source", {"path": "src/large.ets"}), response(3),
    ], executor, compaction_policy=policy(), compaction_responses=[
        AIMessage(content=SUMMARY, usage_metadata={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}),
    ])
    assert len(report["completion"]["context_compactions"]) == 1
    before = tree_bytes(tmp_path)
    workspace = CandidateWorkspace.reopen(task, work, "run1")
    value = read_completion(workspace,
        CheckLedger(workspace, ReplayOnlyExecutor(executor.protocol_sha256), read_only=True),
        ModelCallStore(work / "model-calls.sqlite", read_only=True),
        ToolActionStore(work / "tool-actions.sqlite", read_only=True))
    assert value == report["completion"]
    assert tree_bytes(tmp_path) == before
