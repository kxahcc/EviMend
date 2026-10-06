"""A learning owner binds physical identity while rejecting unsafe root changes."""

from __future__ import annotations

import os
import stat
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from dfx_agent.evidence_io import local_evidence_path
from dfx_agent.learning import _root_physical_identity, learning_root_binding


def changed(info: os.stat_result, **changes: int) -> os.stat_result:
    # Preserve real values while changing only the explicit counterexample
    # field. These proxy objects never leave this software boundary test.
    return cast(os.stat_result, SimpleNamespace(**{
        "st_dev": info.st_dev, "st_ino": info.st_ino, "st_mode": info.st_mode,
        "st_file_attributes": getattr(info, "st_file_attributes", 0), "st_nlink": info.st_nlink,
        **changes}))


def test_non_identity_archive_attribute_drift_preserves_real_binding(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = local_evidence_path(tmp_path)
    original = learning_root_binding(root)
    real_lstat = Path.lstat
    reads = 0

    def alternating(path: Path) -> os.stat_result:
        nonlocal reads
        info = real_lstat(path)
        if path == root:
            reads += 1
            # Archive does not denote a link or a directory's physical identity.
            attributes = getattr(info, "st_file_attributes", 0)
            return changed(info, st_file_attributes=attributes ^ (getattr(stat, "FILE_ATTRIBUTE_ARCHIVE", 32) if reads % 2 else 0))
        return info

    monkeypatch.setattr(Path, "lstat", alternating)
    assert learning_root_binding(root) == original
    assert learning_root_binding(root) == original and reads >= 4


@pytest.mark.parametrize("field", ["st_ino", "st_dev"])
def test_physical_identity_changed_during_binding_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str) -> None:
    root = local_evidence_path(tmp_path)
    real_resolve, real_lstat = Path.resolve, Path.lstat
    resolved = False

    def mark_resolved(path: Path, strict: bool = False) -> Path:
        nonlocal resolved
        result = real_resolve(path, strict=strict)
        if path == root:
            resolved = True
        return result

    def replacement(path: Path) -> os.stat_result:
        info = real_lstat(path)
        return changed(info, **{field: getattr(info, field) + 1}) if resolved and path == root else info

    monkeypatch.setattr(Path, "resolve", mark_resolved)
    monkeypatch.setattr(Path, "lstat", replacement)
    with pytest.raises(ValueError, match="changed during external binding.*->"):
        learning_root_binding(root)


@pytest.mark.parametrize("change", ["file", "link", "reparse", "unknown_inode"])
def test_final_identity_cannot_hide_unsafe_type_or_unknown_identity(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str) -> None:
    root = local_evidence_path(tmp_path)
    real_lstat = Path.lstat
    reads = 0

    def last_only(path: Path) -> os.stat_result:
        nonlocal reads
        info = real_lstat(path)
        if path != root:
            return info
        reads += 1
        # _guard, is_dir (stat, not lstat), first identity, second _guard,
        # final identity: mutate precisely the final direct lstat (fourth).
        if reads != 4:
            return info
        if change == "unknown_inode":
            return changed(info, st_ino=0)
        if change == "reparse":
            return changed(info, st_file_attributes=getattr(info, "st_file_attributes", 0) | getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 1024))
        mode = stat.S_IFLNK if change == "link" else stat.S_IFREG
        return changed(info, st_mode=mode | (info.st_mode & 0o777))

    monkeypatch.setattr(Path, "lstat", last_only)
    with pytest.raises(ValueError, match="identity"):
        learning_root_binding(root)
    assert reads == 4


def test_real_other_root_and_renamed_recreated_root_have_different_bindings(tmp_path: Path) -> None:
    first = local_evidence_path(tmp_path / "first")
    second = local_evidence_path(tmp_path / "second")
    first.mkdir()
    second.mkdir()
    original = learning_root_binding(first)
    assert learning_root_binding(second) != original
    first.rename(local_evidence_path(tmp_path / "retained-original"))
    first.mkdir()
    assert learning_root_binding(first) != original


def test_identity_tuple_keeps_real_device_inode_and_directory_type(tmp_path: Path) -> None:
    actual = tmp_path.lstat()
    assert _root_physical_identity(actual) == (actual.st_dev, actual.st_ino, stat.S_IFDIR)
