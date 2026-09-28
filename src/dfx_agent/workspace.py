"""Independent repair drafts and frozen, source-bound candidate snapshots."""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import shutil
import stat
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from .contracts import CandidatePatch, require_hash, require_path
from .task import digest, snapshot_digest, verify_task


class TextEdit(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    path: str
    old: str = Field(min_length=1)
    new: str

    def checked_path(self) -> str:
        require_path(self.path)
        return self.path


def _files(root: Path) -> dict[str, Path]:
    """Enumerate without following links, including Windows junctions."""
    root_info = root.lstat()
    if stat.S_ISLNK(root_info.st_mode) or getattr(root_info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0):
        raise ValueError("source or workspace root is a link")
    found: dict[str, Path] = {}
    pending = [root]
    while pending:
        folder = pending.pop()
        for entry in os.scandir(folder):
            info = entry.stat(follow_symlinks=False)
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0):
                raise ValueError("source or workspace contains a link")
            path = Path(entry.path)
            if entry.is_dir(follow_symlinks=False):
                pending.append(path)
            elif entry.is_file(follow_symlinks=False):
                relative = path.relative_to(root).as_posix()
                require_path(relative)
                found[relative] = path
            else:
                raise ValueError("unsupported source entry")
    return found


def _manifest(root: Path) -> list[dict[str, str]]:
    return [
        {"path": path, "sha256": digest(file.read_bytes())}
        for path, file in sorted(_files(root).items())
    ]


def _copy_tree(source: Path, target: Path) -> None:
    files = _files(source)
    target.mkdir(parents=True, exist_ok=False)
    for relative, file in files.items():
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(file, destination)
        if os.stat(file).st_ino == os.stat(destination).st_ino and os.stat(file).st_dev == os.stat(destination).st_dev:
            raise ValueError("source and copy share a writable inode")


def _remove_owned_tree(path: Path, owner: Path) -> None:
    if path.parent != owner or not path.name.startswith(("draft", "candidate-temp-")):
        raise ValueError("refusing to remove a non-owned workspace path")
    if path.exists():
        _files(path)
        shutil.rmtree(path)


def _diff(base: Path, changed: Path) -> tuple[str, list[str]]:
    old_files, new_files = _files(base), _files(changed)
    output: list[str] = []
    changed_paths: list[str] = []
    for path in sorted(set(old_files) | set(new_files)):
        old = old_files[path].read_bytes() if path in old_files else b""
        new = new_files[path].read_bytes() if path in new_files else b""
        if old == new and (path in old_files) == (path in new_files):
            continue
        changed_paths.append(path)
        old_lines = old.decode("utf-8").splitlines(keepends=True)
        new_lines = new.decode("utf-8").splitlines(keepends=True)
        output.extend(difflib.unified_diff(
            old_lines, new_lines,
            fromfile=f"a/{path}" if path in old_files else "/dev/null",
            tofile=f"b/{path}" if path in new_files else "/dev/null",
        ))
    return "".join(output), changed_paths


class CandidateWorkspace:
    """One run's baseline, mutable draft and immutable registered candidates."""

    def __init__(self, task_directory: Path, work_directory: Path, run_id: str):
        task_directory = task_directory.resolve(strict=True)
        work_directory = work_directory.parent.resolve() / work_directory.name
        if work_directory.exists() or work_directory.is_relative_to(task_directory) or task_directory.is_relative_to(work_directory):
            raise ValueError("work directory must be new and separate from the task snapshot")
        task = verify_task(task_directory)
        source = task_directory / task["source_root"]
        if _manifest(source) != json.loads((task_directory / task["source_manifest"]).read_text(encoding="utf-8")):
            raise ValueError("source manifest does not match source tree")
        _files(source)
        self.root = work_directory
        self.base = self.root / "base"
        self.draft = self.root / "draft"
        self.candidates = self.root / "candidates"
        self.task_id = task["task_id"]
        self.run_id = run_id
        self.source_snapshot_sha256 = task["source_snapshot_sha256"]
        self.root.mkdir(parents=True, exist_ok=False)
        try:
            _copy_tree(source, self.base)
            self.candidates.mkdir()
            (self.root / "workspace.json").write_text(json.dumps({
                "schema_version": 1, "task_id": self.task_id, "run_id": run_id,
                "source_snapshot_sha256": self.source_snapshot_sha256,
            }, sort_keys=True), encoding="utf-8")
        except Exception:
            _files(self.root)
            shutil.rmtree(self.root)
            raise

    @classmethod
    def reopen(cls, task_directory: Path, work_directory: Path, run_id: str) -> CandidateWorkspace:
        """Reopen an owned run only after rechecking its task and frozen baseline."""
        task_directory = task_directory.resolve(strict=True)
        task = verify_task(task_directory)
        root = work_directory.resolve(strict=True)
        if root.is_relative_to(task_directory) or task_directory.is_relative_to(root):
            raise ValueError("work directory overlaps the task snapshot")
        meta = json.loads((root / "workspace.json").read_text(encoding="utf-8"))
        expected = {
            "schema_version": 1, "task_id": task["task_id"], "run_id": run_id,
            "source_snapshot_sha256": task["source_snapshot_sha256"],
        }
        if meta != expected:
            raise ValueError("workspace owner or source snapshot differs")
        instance = cls.__new__(cls)
        instance.root, instance.base = root, root / "base"
        instance.draft, instance.candidates = root / "draft", root / "candidates"
        instance.task_id, instance.run_id = task["task_id"], run_id
        instance.source_snapshot_sha256 = task["source_snapshot_sha256"]
        if snapshot_digest(_manifest(instance.base)) != instance.source_snapshot_sha256:
            raise ValueError("workspace baseline changed")
        if not instance.candidates.is_dir() or instance.candidates.is_symlink():
            raise ValueError("workspace candidates directory is missing or linked")
        if instance.draft.exists():
            _files(instance.draft)
        return instance

    def start_draft(self) -> Path:
        if self.draft.exists():
            raise ValueError("draft already exists; register or discard it first")
        if snapshot_digest(_manifest(self.base)) != self.source_snapshot_sha256:
            raise ValueError("baseline changed")
        try:
            _copy_tree(self.base, self.draft)
        except Exception:
            _remove_owned_tree(self.draft, self.root)
            raise
        return self.draft

    def discard_draft(self) -> None:
        _remove_owned_tree(self.draft, self.root)

    def apply_text_edits(self, edits: list[TextEdit]) -> None:
        """Apply exact edits to a temporary copy; a failed edit leaves the draft intact."""
        if not self.draft.is_dir() or not edits:
            raise ValueError("an active draft and at least one edit are required")
        temp = self.root / ("candidate-temp-" + uuid4().hex)
        backup = self.root / "draft-backup"
        if backup.exists():
            raise ValueError("unfinished draft swap requires recovery")
        try:
            _copy_tree(self.draft, temp)
        except Exception:
            _remove_owned_tree(temp, self.root)
            raise
        try:
            for edit in edits:
                path = temp / edit.checked_path()
                if not path.is_file() or path.is_symlink():
                    raise ValueError("edit target is missing or linked")
                content = path.read_text(encoding="utf-8")
                if content.count(edit.old) != 1:
                    raise ValueError("edit anchor must match exactly once")
                path.write_text(content.replace(edit.old, edit.new, 1), encoding="utf-8")
            self.draft.rename(backup)
            try:
                temp.rename(self.draft)
            except Exception:
                backup.rename(self.draft)
                raise
            _files(backup)
            shutil.rmtree(backup)
        finally:
            _remove_owned_tree(temp, self.root)

    def register_candidate(self, ordinal: int, diagnosis_id: str | None = None) -> CandidatePatch:
        """Freeze a complete diff and copy before any candidate-specific check."""
        if not self.draft.is_dir() or ordinal < 1:
            raise ValueError("draft and positive ordinal required")
        if snapshot_digest(_manifest(self.base)) != self.source_snapshot_sha256:
            raise ValueError("baseline changed")
        diff, changed_paths = _diff(self.base, self.draft)
        if not changed_paths:
            raise ValueError("candidate has no changes")
        candidate_manifest = _manifest(self.draft)
        candidate_snapshot_sha256 = snapshot_digest(candidate_manifest)
        diff_sha256 = digest(diff.encode("utf-8"))
        candidate_id = hashlib.sha256((self.source_snapshot_sha256 + candidate_snapshot_sha256 + diff_sha256).encode()).hexdigest()
        destination = self.candidates / candidate_id
        if destination.exists():
            raise ValueError("candidate already registered")
        record = CandidatePatch(
            task_id=self.task_id, run_id=self.run_id, record_id=f"candidate-{ordinal}",
            created_at=datetime.now(UTC), source_snapshot_sha256=self.source_snapshot_sha256,
            candidate_id=candidate_id, ordinal=ordinal, parent_snapshot_sha256=self.source_snapshot_sha256,
            candidate_snapshot_sha256=candidate_snapshot_sha256, diff_sha256=diff_sha256,
            diagnosis_id=diagnosis_id, changed_paths=changed_paths,
        )
        temp = self.candidates / ("candidate-temp-" + uuid4().hex)
        temp.mkdir()
        try:
            _copy_tree(self.draft, temp / "source")
            if _manifest(temp / "source") != candidate_manifest:
                raise ValueError("draft changed while freezing candidate")
            (temp / "manifest.json").write_text(json.dumps(candidate_manifest, sort_keys=True), encoding="utf-8")
            (temp / "patch.diff").write_bytes(diff.encode("utf-8"))
            (temp / "candidate.json").write_text(record.model_dump_json(), encoding="utf-8")
            temp.rename(destination)
        except Exception:
            if temp.exists():
                _files(temp)
                shutil.rmtree(temp)
            raise
        self.discard_draft()
        return record

    def require_registered(self, candidate_id: str) -> Path:
        """Return only a frozen snapshot whose record, diff and content still agree."""
        require_hash(candidate_id)
        folder = self.candidates / candidate_id
        if not folder.is_dir():
            raise ValueError("candidate is not registered")
        record = CandidatePatch.model_validate_json((folder / "candidate.json").read_text(encoding="utf-8"))
        source = folder / "source"
        manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
        actual_diff, actual_paths = _diff(self.base, source)
        expected_id = hashlib.sha256((self.source_snapshot_sha256 + record.candidate_snapshot_sha256 + record.diff_sha256).encode()).hexdigest()
        if (record.candidate_id != candidate_id or expected_id != candidate_id or
                record.task_id != self.task_id or record.run_id != self.run_id or
                record.source_snapshot_sha256 != self.source_snapshot_sha256 or
                _manifest(source) != manifest or snapshot_digest(manifest) != record.candidate_snapshot_sha256 or
                digest((folder / "patch.diff").read_bytes()) != record.diff_sha256 or
                actual_diff.encode("utf-8") != (folder / "patch.diff").read_bytes() or
                actual_paths != record.changed_paths):
            raise ValueError("registered candidate identity or contents changed")
        return source
