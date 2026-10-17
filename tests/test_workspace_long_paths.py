"""Long real source paths retain snapshot identity and SDK edits on Windows."""

from __future__ import annotations

import os
import stat
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from deepagents.backends import FilesystemBackend

from dfx_agent.agent import _safe_file
from dfx_agent.diagnostics import AnalysisStore, safe_source
from dfx_agent.evidence_io import local_evidence_path
from dfx_agent.source_search import SourceSearch
from dfx_agent.sources import SourceIndex
from dfx_agent.task import (
    _resolve_logical,
    digest,
    prepare_task,
    safe_path,
    verify_task,
)
from dfx_agent.verification import AnalysisOnlyExecutor, CheckLedger
from dfx_agent.working_context import WorkingContextFocus, build_working_context
from dfx_agent.working_context_plan import _capture_material
from dfx_agent.workspace import CandidateWorkspace, TextEdit

RELATIVE = ("commons/lib_foundation/src/main/ets/storage/persistence/repository/orders/requests/validation/"
            "compatibility_checks_for_request_checksum_and_unchanged_business_inputs/OrderRequestChecksum.ets")
ORIGINAL = "export function checksum(value: number): number {\n  return value - 1;\n}\n"


def assert_long_source_consumers(workspace: CandidateWorkspace, task: Path, candidate_id: str) -> None:
    """Exercise production readers and real material capture without a model."""
    candidate = workspace.require_registered(candidate_id)
    fixed = ORIGINAL.replace("value - 1", "value + 1").encode()
    for root, expected in ((workspace.base, ORIGINAL.encode()), (candidate, fixed)):
        for reader in (_safe_file, safe_source):
            observed = reader(root, RELATIVE)
            # Readers may return an extended I/O path; externally meaningful
            # coordinates remain ordinary logical paths and source references.
            assert _resolve_logical(observed) == root / RELATIVE
            assert observed.read_bytes() == expected
    assert AnalysisStore(workspace).line_counts() == {RELATIVE: 3}
    search = SourceSearch(workspace)
    baseline = search.search("value - 1")
    frozen = search.search("value + 1", candidate_id=candidate_id)
    assert baseline["scope"] == "baseline" and frozen["scope"] == "candidate"
    for result, expected in ((baseline, ORIGINAL.encode()), (frozen, fixed)):
        assert result["complete"] is True and result["total_matched_lines"] == 1
        match = result["matches"][0]
        assert match["path"] == RELATIVE and match["line"] == 2
        assert match["file_sha256"] == digest(expected)
        assert "\\\\?\\" not in str(result)
    assert search.search("value - 1", candidate_id=candidate_id)["matches"] == []
    assert search.search("value + 1")["matches"] == []

    ledger = CheckLedger(workspace, AnalysisOnlyExecutor())
    packet = build_working_context(workspace, WorkingContextFocus(), task_directory=task,
                                   ledger=ledger, max_bytes=262144)
    # No injected reader: this is the same actual original-file read used when
    # the production context plan freezes its source materials.
    material = _capture_material(workspace, task, packet)
    reference = "workspace/base/" + RELATIVE
    assert material[reference] == ORIGINAL.encode()
    assert "\\\\?\\" not in packet.canonical_payload_json
    assert all(not key.startswith(("/", "\\")) and "\\\\?\\" not in key for key in material)


def task_snapshot(tmp_path: Path) -> tuple[Path, Path]:
    dataset = tmp_path / "dataset"
    source = dataset / "app" / RELATIVE
    assert len(str(source)) > 260, "the fixture must exercise actual long-path I/O"
    local_evidence_path(source.parent).mkdir(parents=True)
    local_evidence_path(source).write_bytes(ORIGINAL.encode("utf-8"))
    log = dataset / "fault.log"
    local_evidence_path(log).write_bytes(b"CHECKSUM_FAULT at OrderRequestChecksum.ets:2")
    task = tmp_path / "reviewed-task"
    prepare_task(dataset, {"task_id": "long-path-case", "app": "app",
        "log": {"path": "fault.log", "sha256": digest(local_evidence_path(log).read_bytes())}}, task)
    return task, source


def test_long_source_preparation_verification_and_index_keep_logical_paths(tmp_path: Path) -> None:
    task, original = task_snapshot(tmp_path)
    record = verify_task(task)
    assert record["task_id"] == "long-path-case"
    selected = safe_path(task / "source", RELATIVE)
    assert not str(selected).startswith("\\\\?\\")
    assert selected.relative_to(task / "source").as_posix() == RELATIVE
    assert local_evidence_path(selected).read_text(encoding="utf-8") == ORIGINAL
    assert local_evidence_path(original).read_text(encoding="utf-8") == ORIGINAL
    index = SourceIndex(task / "source")
    assert index.resolve("OrderRequestChecksum.ets")["paths"] == [RELATIVE]
    assert index.context(RELATIVE, 2)["lines"][1]["text"] == "  return value - 1;"
    local_evidence_path(selected).write_text("changed source", encoding="utf-8")
    with pytest.raises(ValueError, match="missing or has changed"):
        verify_task(task)


def test_long_sdk_edit_freeze_reopen_and_discard_preserve_bytes(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    task, original = task_snapshot(tmp_path)
    workspace = CandidateWorkspace(task, tmp_path / "actual-run", "run1")
    workspace.start_draft()
    sdk_edit = FilesystemBackend.edit
    observed: list[tuple[Path, str]] = []

    def capture(backend: FilesystemBackend, file_path: str, old_string: str,
                new_string: str, replace_all: bool = False) -> Any:
        observed.append((backend.cwd, file_path))
        assert len(str(backend.cwd / RELATIVE)) > 260
        if os.name == "nt":
            assert str(backend.cwd).startswith("\\\\?\\")
        return sdk_edit(backend, file_path, old_string, new_string, replace_all)

    monkeypatch.setattr(FilesystemBackend, "edit", capture)
    workspace.apply_text_edits([TextEdit(path=RELATIVE, old="value - 1", new="value + 1")])
    candidate = workspace.register_candidate(1)
    frozen = workspace.require_registered(candidate.candidate_id)
    assert candidate.changed_paths == [RELATIVE] and len(observed) == 1
    assert observed[0][1] == "/" + RELATIVE
    assert not str(workspace.root).startswith("\\\\?\\")
    assert not str(frozen).startswith("\\\\?\\")
    assert len(str(frozen / RELATIVE)) > 260
    assert local_evidence_path(frozen / RELATIVE).read_text(encoding="utf-8") == ORIGINAL.replace("value - 1", "value + 1")
    assert local_evidence_path(original).read_text(encoding="utf-8") == ORIGINAL
    assert local_evidence_path(workspace.base / RELATIVE).read_text(encoding="utf-8") == ORIGINAL
    reopened = CandidateWorkspace.reopen(task, workspace.root, "run1")
    assert reopened.require_registered(candidate.candidate_id) == frozen
    reopened.start_draft()
    reopened.discard_draft()
    assert not local_evidence_path(reopened.draft).exists()
    assert_long_source_consumers(reopened, task, candidate.candidate_id)
    local_evidence_path(frozen / RELATIVE).write_text("tampered candidate", encoding="utf-8")
    with pytest.raises(ValueError, match="contents changed"):
        reopened.require_registered(candidate.candidate_id)


def test_long_edit_failure_and_path_traversal_do_not_replace_draft(tmp_path: Path) -> None:
    task, _ = task_snapshot(tmp_path)
    workspace = CandidateWorkspace(task, tmp_path / "atomic-run", "run1")
    workspace.start_draft()
    for edit in (TextEdit(path=RELATIVE, old="absent anchor", new="changed"),
                 TextEdit(path="../escape.ets", old="value", new="changed")):
        with pytest.raises(ValueError):
            workspace.apply_text_edits([edit])
        assert local_evidence_path(workspace.draft / RELATIVE).read_text(encoding="utf-8") == ORIGINAL
    with pytest.raises(ValueError, match="relative path"):
        safe_path(task / "source", "../../outside.ets")
    assert not local_evidence_path(workspace.root / "draft-backup").exists()
    assert not any(path.name.startswith("candidate-temp-") for path in workspace.root.iterdir())


def test_safe_long_path_rejects_reparse_parent_before_resolving(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    task, _ = task_snapshot(tmp_path)
    parent = task / "source" / Path(RELATIVE).parent
    original_lstat = Path.lstat

    def linked(path: Path, *args: Any, **kwargs: Any) -> Any:
        if path == local_evidence_path(parent):
            return SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 1024))
        return original_lstat(path, *args, **kwargs)

    monkeypatch.setattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 1024, raising=False)
    monkeypatch.setattr(Path, "lstat", linked)
    with pytest.raises(ValueError, match="links"):
        safe_path(task / "source", RELATIVE)


def test_long_snapshot_does_not_follow_real_file_link(tmp_path: Path) -> None:
    task, _ = task_snapshot(tmp_path)
    outside = tmp_path / "outside.ets"
    outside.write_text("outside secret", encoding="utf-8")
    link = task / "source" / Path(RELATIVE).with_name("LinkedChecksum.ets")
    try:
        local_evidence_path(link).symlink_to(local_evidence_path(outside))
    except OSError:
        pytest.skip("this Windows account cannot create native symbolic links")
    with pytest.raises(ValueError, match="links"):
        verify_task(task)
