"""Candidate snapshots must be complete, independent and registered before checks."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from deepagents.backends import FilesystemBackend
from deepagents.backends.protocol import EditResult

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


def test_sdk_edit_runs_only_in_temporary_draft_and_preserves_original_and_candidate(tmp_path: Path, monkeypatch) -> None:
    workspace = CandidateWorkspace(task_snapshot(tmp_path), tmp_path / "work", "run1")
    baseline_bytes = (workspace.base / "src/main.ets").read_bytes()
    workspace.start_draft()
    workspace.apply_text_edits([TextEdit(path="src/main.ets", old="bad", new="first")])
    first = workspace.register_candidate(1)
    frozen = workspace.require_registered(first.candidate_id)
    frozen_bytes = (frozen / "src/main.ets").read_bytes()
    workspace.start_draft()
    original = FilesystemBackend.edit
    observed = []

    def capture(backend, file_path, old_string, new_string, replace_all=False):
        observed.append((backend, file_path, replace_all))
        assert backend.virtual_mode is True and backend.cwd.parent == workspace.root.resolve()
        assert backend.cwd.name.startswith("candidate-temp-")
        assert (workspace.draft / "src/main.ets").read_text(encoding="utf-8") == "value=bad\n"
        return original(backend, file_path, old_string, new_string, replace_all)

    monkeypatch.setattr(FilesystemBackend, "edit", capture)
    workspace.apply_text_edits([TextEdit(path="src/main.ets", old="bad", new="second")])
    assert len(observed) == 1 and observed[0][1:] == ("/src/main.ets", False)
    assert (workspace.draft / "src/main.ets").read_bytes() == b"value=second\n"
    assert (workspace.base / "src/main.ets").read_bytes() == baseline_bytes
    assert (frozen / "src/main.ets").read_bytes() == frozen_bytes


def test_sdk_edit_failure_after_partial_temporary_write_preserves_complete_draft(tmp_path: Path, monkeypatch) -> None:
    workspace = CandidateWorkspace(task_snapshot(tmp_path), tmp_path / "work", "run1")
    workspace.start_draft()
    before = {name: path.read_bytes() for name, path in {
        "main": workspace.draft / "src/main.ets", "other": workspace.draft / "src/other.ets"}.items()}
    original = FilesystemBackend.edit

    def fail_second(backend, file_path, old_string, new_string, replace_all=False):
        result = original(backend, file_path, old_string, new_string, replace_all)
        return EditResult(error="synthetic backend failure after write") if file_path.endswith("other.ets") else result

    monkeypatch.setattr(FilesystemBackend, "edit", fail_second)
    with pytest.raises(ValueError, match="SDK edit failed"):
        workspace.apply_text_edits([TextEdit(path="src/main.ets", old="bad", new="changed"),
                                    TextEdit(path="src/other.ets", old="1", new="2")])
    assert (workspace.draft / "src/main.ets").read_bytes() == before["main"]
    assert (workspace.draft / "src/other.ets").read_bytes() == before["other"]
    assert not list(workspace.root.glob("candidate-temp-*"))
    assert not (workspace.root / "draft-backup").exists()


def test_sdk_edit_handles_inspected_crlf_anchor_and_preserves_eof_without_newline(tmp_path: Path) -> None:
    workspace = CandidateWorkspace(task_snapshot(tmp_path), tmp_path / "work", "run1")
    workspace.start_draft()
    (workspace.draft / "src/main.ets").write_bytes(b"first\r\nvalue=bad")
    workspace.apply_text_edits([TextEdit(path="src/main.ets", old="first\r\nvalue=bad", new="first\r\nvalue=good")])
    assert (workspace.draft / "src/main.ets").read_bytes() == b"first\nvalue=good"
    before = (workspace.draft / "src/main.ets").read_bytes()
    with pytest.raises(ValueError, match="does not end with a newline"):
        workspace.apply_text_edits([TextEdit(path="src/main.ets", old="value=good\n", new="value=wrong\n")])
    assert (workspace.draft / "src/main.ets").read_bytes() == before


def test_sdk_edit_rejects_ambiguous_anchor_without_replacing_all(tmp_path: Path) -> None:
    workspace = CandidateWorkspace(task_snapshot(tmp_path), tmp_path / "work", "run1")
    workspace.start_draft()
    (workspace.draft / "src/main.ets").write_bytes(b"bad bad\n")
    with pytest.raises(ValueError, match="exactly once"):
        workspace.apply_text_edits([TextEdit(path="src/main.ets", old="bad", new="good")])
    assert (workspace.draft / "src/main.ets").read_bytes() == b"bad bad\n"


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


@pytest.mark.parametrize("scope", ["task", "baseline"])
def test_public_reopen_rejects_same_size_change_with_restored_mtime(tmp_path: Path, scope: str) -> None:
    task = task_snapshot(tmp_path)
    workspace = CandidateWorkspace(task, tmp_path / "work", "run1")
    assert CandidateWorkspace.reopen(task, workspace.root, "run1").root == workspace.root
    path = (task / "source" if scope == "task" else workspace.base) / "src/other.ets"
    before = path.stat()
    original = path.read_bytes()
    changed = original.replace(b"1", b"2")
    assert changed != original and len(changed) == len(original)
    path.write_bytes(changed)
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert path.stat().st_size == before.st_size and path.stat().st_mtime_ns == before.st_mtime_ns
    with pytest.raises(ValueError, match="Source file is missing or has changed|workspace baseline changed"):
        CandidateWorkspace.reopen(task, workspace.root, "run1")
    assert path.read_bytes() == changed


@pytest.mark.parametrize("scope", ["task", "baseline"])
@pytest.mark.parametrize("mutation", ["add", "remove"])
def test_public_reopen_rechecks_complete_source_membership(tmp_path: Path, scope: str, mutation: str) -> None:
    task = task_snapshot(tmp_path)
    workspace = CandidateWorkspace(task, tmp_path / "work", "run1")
    assert CandidateWorkspace.reopen(task, workspace.root, "run1").root == workspace.root
    source = task / "source" if scope == "task" else workspace.base
    if mutation == "add":
        (source / "src/unselected.ets").write_bytes(b"unselected=1\n")
    else:
        (source / "src/other.ets").unlink()
    with pytest.raises(ValueError, match="Unexpected or missing files|Source file is missing|workspace baseline changed"):
        CandidateWorkspace.reopen(task, workspace.root, "run1")


@pytest.mark.parametrize("field", ["schema_version", "task_id", "run_id", "source_snapshot_sha256"])
def test_public_reopen_rejects_workspace_owner_changes(tmp_path: Path, field: str) -> None:
    task = task_snapshot(tmp_path)
    workspace = CandidateWorkspace(task, tmp_path / "work", "run1")
    assert CandidateWorkspace.reopen(task, workspace.root, "run1").root == workspace.root
    path = workspace.root / "workspace.json"
    metadata = json.loads(path.read_text(encoding="utf-8"))
    metadata[field] = 2 if field == "schema_version" else ("0" * 64 if field == "source_snapshot_sha256" else "other")
    path.write_text(json.dumps(metadata), encoding="utf-8")
    with pytest.raises(ValueError, match="workspace owner or source snapshot differs"):
        CandidateWorkspace.reopen(task, workspace.root, "run1")
