"""Candidate snapshots must be complete, independent and registered before checks."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from dfx_agent.task import PROFILE, digest, snapshot_digest
from dfx_agent.workspace import CandidateWorkspace, TextEdit


def task_snapshot(root: Path) -> Path:
    task = root / "input"
    source = task / "source/src"
    source.mkdir(parents=True)
    (source / "main.ets").write_text("value=bad\n", encoding="utf-8")
    (source / "other.ets").write_text("other=1\n", encoding="utf-8")
    entries = [
        {"path": f"src/{name}", "sha256": digest((source / name).read_bytes())}
        for name in ("main.ets", "other.ets")
    ]
    (task / "source-manifest.json").write_text(json.dumps(entries), encoding="utf-8")
    (task / "crash.log").write_text("synthetic crash", encoding="utf-8")
    (task / "task.json").write_text(json.dumps({
        "schema_version": 1, "profile": PROFILE, "task_id": "task1",
        "source_root": "source", "source_manifest": "source-manifest.json",
        "source_snapshot_sha256": snapshot_digest(entries),
        "crash_log": "crash.log", "crash_sha256": digest(b"synthetic crash"),
    }), encoding="utf-8")
    return task


def test_candidate_is_frozen_and_baseline_is_untouched(tmp_path: Path) -> None:
    task = task_snapshot(tmp_path)
    workspace = CandidateWorkspace(task, tmp_path / "work", "run1")
    draft = workspace.start_draft()
    source_file = task / "source/src/main.ets"
    draft_file = draft / "src/main.ets"
    assert not os.path.samefile(source_file, draft_file)
    workspace.apply_text_edits([TextEdit(path="src/main.ets", old="bad", new="good")])
    candidate = workspace.register_candidate(1)
    frozen = workspace.require_registered(candidate.candidate_id)
    assert (frozen / "src/main.ets").read_text(encoding="utf-8") == "value=good\n"
    assert source_file.read_text(encoding="utf-8") == "value=bad\n"
    assert candidate.changed_paths == ["src/main.ets"]
    assert "-value=bad" in (workspace.candidates / candidate.candidate_id / "patch.diff").read_text(encoding="utf-8")
    with pytest.raises(ValueError, match="no changes"):
        workspace.start_draft()
        workspace.register_candidate(2)


def test_failed_edit_is_atomic_and_unregistered_candidate_cannot_be_checked(tmp_path: Path) -> None:
    workspace = CandidateWorkspace(task_snapshot(tmp_path), tmp_path / "work", "run1")
    draft = workspace.start_draft()
    before = (draft / "src/main.ets").read_bytes()
    with pytest.raises(ValueError, match="exactly once"):
        workspace.apply_text_edits([
            TextEdit(path="src/main.ets", old="bad", new="changed"),
            TextEdit(path="src/other.ets", old="absent", new="changed"),
        ])
    assert (draft / "src/main.ets").read_bytes() == before
    with pytest.raises(ValueError, match="not registered"):
        workspace.require_registered("0" * 64)
    workspace.discard_draft()
    assert workspace.start_draft().is_dir()
    assert (workspace.draft / "src/main.ets").read_bytes() == before


def test_changed_candidate_is_rejected_before_check(tmp_path: Path) -> None:
    workspace = CandidateWorkspace(task_snapshot(tmp_path), tmp_path / "work", "run1")
    workspace.start_draft()
    workspace.apply_text_edits([TextEdit(path="src/main.ets", old="bad", new="good")])
    candidate = workspace.register_candidate(1)
    frozen = workspace.require_registered(candidate.candidate_id)
    (frozen / "src/main.ets").write_text("value=tampered\n", encoding="utf-8")
    with pytest.raises(ValueError, match="identity or contents changed"):
        workspace.require_registered(candidate.candidate_id)


def test_source_with_link_is_not_copied(tmp_path: Path) -> None:
    task = task_snapshot(tmp_path)
    external = tmp_path / "external.txt"
    external.write_text("secret", encoding="utf-8")
    link = task / "source/src/link.ets"
    try:
        os.symlink(external, link)
    except OSError:
        pytest.skip("symlink creation unavailable on this Windows account")
    with pytest.raises(ValueError, match="link"):
        CandidateWorkspace(task, tmp_path / "work", "run1")
    assert external.read_text(encoding="utf-8") == "secret"


def test_source_hardlink_becomes_independent_copy(tmp_path: Path) -> None:
    task = task_snapshot(tmp_path)
    original = task / "source/src/main.ets"
    external = tmp_path / "outside-main.ets"
    os.link(original, external)
    workspace = CandidateWorkspace(task, tmp_path / "work", "run1")
    draft = workspace.start_draft()
    assert not os.path.samefile(original, draft / "src/main.ets")
    workspace.apply_text_edits([TextEdit(path="src/main.ets", old="bad", new="good")])
    assert original.read_text(encoding="utf-8") == "value=bad\n"
    assert external.read_text(encoding="utf-8") == "value=bad\n"
