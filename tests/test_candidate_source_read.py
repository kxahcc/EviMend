"""Versioned source reading components; no model or application execution."""

from __future__ import annotations

import json
from pathlib import Path

from test_agent_loop import task_snapshot

from dfx_agent.agent import _tools
from dfx_agent.policies import PolicyFeatures
from dfx_agent.task import verify_task
from dfx_agent.verification import CheckLedger, FixedTextExecutor
from dfx_agent.workspace import CandidateWorkspace, TextEdit


def reader(tmp_path: Path):
    task = task_snapshot(tmp_path)
    workspace = CandidateWorkspace(task, tmp_path / "work", "run1")
    ledger = CheckLedger(workspace, FixedTextExecutor("src/main.ets", "value=good\n"))
    tools = _tools(workspace, ledger, 4, task, "crash.log", verify_task(task)["crash_sha256"], PolicyFeatures())
    return workspace, next(tool for tool in tools if tool.name == "read_source")


def test_baseline_draft_and_registered_candidate_are_distinct(tmp_path: Path) -> None:
    workspace, read = reader(tmp_path)
    original = json.loads(read.invoke({"path": "src/main.ets"}))
    assert original == {"path": "src/main.ets", "start_line": 1, "lines": ["value=bad"]}
    workspace.start_draft()
    workspace.apply_text_edits([TextEdit(path="src/main.ets", old="bad", new="good")])
    frozen = workspace.register_candidate(1)
    workspace.start_draft()
    workspace.apply_text_edits([TextEdit(path="src/main.ets", old="bad", new="draft-only")])
    candidate = json.loads(read.invoke({"path": "src/main.ets", "candidate_id": frozen.candidate_id}))
    assert candidate == {"path": "src/main.ets", "start_line": 1, "lines": ["value=good"],
                         "scope": "candidate", "candidate_id": frozen.candidate_id,
                         "snapshot_sha256": frozen.candidate_snapshot_sha256}
    assert json.loads(read.invoke({"path": "src/main.ets"})) == original
    assert json.loads(read.invoke({"path": "src/main.ets", "draft": True})) == {
        "path": "src/main.ets", "start_line": 1, "lines": ["value=draft-only"]}
    assert read.invoke({"path": "src/main.ets", "draft": True,
                        "candidate_id": frozen.candidate_id}).startswith("rejected:")
    assert read.invoke({"path": "src/main.ets", "candidate_id": "f" * 64}).startswith("rejected:")


def test_candidate_read_rejects_snapshot_drift_and_invalid_range(tmp_path: Path) -> None:
    workspace, read = reader(tmp_path)
    workspace.start_draft()
    workspace.apply_text_edits([TextEdit(path="src/main.ets", old="bad", new="good")])
    frozen = workspace.register_candidate(1)
    args = {"path": "src/main.ets", "candidate_id": frozen.candidate_id}
    assert read.invoke({**args, "start_line": 0}).startswith("rejected:")
    assert read.invoke({**args, "end_line": 201}).startswith("rejected:")
    assert read.invoke({**args, "path": "../candidate.json"}).startswith("rejected:")
    source = workspace.require_registered(frozen.candidate_id)
    (source / "src/main.ets").write_bytes(b"value=drifted\n")
    assert read.invoke(args).startswith("rejected:")
