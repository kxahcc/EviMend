"""Run-local immutable raw materials for historical working-context plans.

Digest names share bytes inside one run, never files or authority across runs.
Plans authenticate their own exact reference manifests. Complete unreferenced
blobs and bounded partial writes remain inspectable evidence, not task facts.
"""

from __future__ import annotations

import os
import re
import stat
import uuid
from pathlib import Path
from typing import Any

from .context_compaction import (
    MAX_ARTIFACT_BYTES,
    CompactionStore,
    ContextCompactionError,
)
from .contracts import require_hash, require_path
from .model_calls import canonical
from .task import digest
from .working_context import WorkingContextError

# The existing evidence-tree format bound in metrics.MAX_TREE_FILES. Importing
# metrics here would cycle through models and working_context_plan.
MAX_MATERIAL_FILES = 100_000
_BLOB = re.compile(r"([0-9a-f]{64})\.blob\Z")
_PENDING = re.compile(r"pending-[0-9a-f]{32}\.blob-part\Z")


def _identity(info: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_nlink


def _open_identity(info: os.stat_result) -> tuple[int, int, int, int, int]:
    # Windows Python 3.13 lstat exposes creation time as st_ctime, while
    # fstat exposes change time. Compare each interface's ctime to itself;
    # the cross-interface identity still requires the exact volume/inode,
    # size, modification time and independent link count.
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_nlink


def _reference(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {"sha256", "bytes"}:
        raise ValueError("invalid material reference fields")
    if not isinstance(value["sha256"], str) or type(value["bytes"]) is not int:
        raise TypeError("invalid material reference types")
    require_hash(value["sha256"])
    if value["bytes"] < 0:
        raise ValueError("negative material size")
    if value["bytes"] > MAX_ARTIFACT_BYTES:
        raise WorkingContextError("working_context_material_limit")
    return {"sha256": value["sha256"], "bytes": value["bytes"]}


def _manifest(value: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(value, dict):
        raise TypeError("material manifest must be an object")
    if len(value) > MAX_MATERIAL_FILES:
        raise WorkingContextError("working_context_material_limit")
    checked: dict[str, dict[str, Any]] = {}
    folded: set[str] = set()
    for reference, item in value.items():
        if not isinstance(reference, str):
            raise TypeError("material scope reference must be text")
        require_path(reference)
        parts = reference.split("/")
        if (len(parts) < 2 or parts[0] not in {"workspace", "task"} or reference.casefold() in folded or
                any(ord(char) < 32 for char in reference) or any(part.endswith((".", " ")) for part in parts)):
            raise ValueError("invalid or colliding material scope reference")
        folded.add(reference.casefold())
        checked[reference] = _reference(item)
    if len(canonical(checked).encode("utf-8")) > MAX_ARTIFACT_BYTES:
        raise WorkingContextError("working_context_material_limit")
    return checked


class WorkingContextMaterials:
    """A serial managed-run store; construction and audit never create files."""

    def __init__(self, io: CompactionStore):
        self.io = io
        self.directory = io.root / "working-context" / "materials"
        self._entry_count: int | None = None

    @staticmethod
    def validate_manifest(value: Any) -> None:
        """Pure shape and capacity checks; no disk reads or source certification."""
        try:
            _manifest(value)
        except WorkingContextError:
            raise
        except (ValueError, TypeError) as exc:
            raise WorkingContextError("working_context_material_invalid") from exc

    def _guard(self, path: Path, *, file: bool = True) -> None:
        try:
            self.io._guard(path, file=file)
        except ContextCompactionError as exc:
            raise WorkingContextError("working_context_material_invalid") from exc

    @staticmethod
    def _exists(path: Path) -> bool:
        try:
            path.lstat()
        except FileNotFoundError:
            return False
        return True

    def _has_directory(self) -> bool:
        self._guard(self.io.root, file=False)
        parent = self.directory.parent
        if self._exists(parent):
            self._guard(parent, file=False)
        if not self._exists(self.directory):
            return False
        self._guard(self.directory, file=False)
        return True

    def _ensure_directory(self) -> None:
        self._guard(self.io.root, file=False)
        for path in (self.directory.parent, self.directory):
            if not self._exists(path):
                path.mkdir()
            self._guard(path, file=False)

    def _entries(self) -> list[Path]:
        if not self._has_directory():
            return []
        found: list[Path] = []
        for path in self.directory.iterdir():
            self._guard(path)
            if _BLOB.fullmatch(path.name) is None and _PENDING.fullmatch(path.name) is None:
                raise ValueError("unexpected material namespace entry")
            found.append(path)
            if len(found) > MAX_MATERIAL_FILES:
                raise WorkingContextError("working_context_material_limit")
        return sorted(found)

    def _read_file(self, path: Path, *, expected_hash: str | None = None,
                   expected_bytes: int | None = None) -> bytes:
        self._guard(path)
        before = path.lstat()
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise ValueError("material is not an independent regular file")
        if before.st_size > MAX_ARTIFACT_BYTES:
            raise WorkingContextError("working_context_material_limit")
        if expected_bytes is not None and before.st_size != expected_bytes:
            raise ValueError("material declared size differs")
        with path.open("rb") as source:
            opened = os.fstat(source.fileno())
            if _open_identity(opened) != _open_identity(before):
                raise ValueError("material identity changed before read")
            raw = source.read(MAX_ARTIFACT_BYTES + 1)
            if _identity(os.fstat(source.fileno())) != _identity(opened):
                raise ValueError("material changed during read")
        self._guard(path)
        if _identity(path.lstat()) != _identity(before) or len(raw) != before.st_size:
            raise ValueError("material identity changed after read")
        if len(raw) > MAX_ARTIFACT_BYTES:
            raise WorkingContextError("working_context_material_limit")
        if expected_hash is not None and digest(raw) != expected_hash:
            raise ValueError("material original bytes differ")
        return raw

    def put(self, raw: bytes) -> dict[str, Any]:
        try:
            if not isinstance(raw, bytes):
                raise TypeError("material must be immutable bytes")
            if len(raw) > MAX_ARTIFACT_BYTES:
                raise WorkingContextError("working_context_material_limit")
            reference = {"sha256": digest(raw), "bytes": len(raw)}
            target = self.directory / (reference["sha256"] + ".blob")
            self._ensure_directory()
            if self._exists(target):
                if self.read(reference) != raw:
                    raise ValueError("existing material differs from requested bytes")
                return reference
            # The managed Graph is a single writer. Count its own new entries
            # after one bounded inventory rather than rescan for every blob.
            # New instances and audit always obtain a fresh namespace view.
            if self._entry_count is None:
                self._entry_count = len(self._entries())
            if self._entry_count >= MAX_MATERIAL_FILES:
                raise WorkingContextError("working_context_material_limit")
            temporary = self.directory / ("pending-" + uuid.uuid4().hex + ".blob-part")
            # A process crash can leave a bounded regular pending file. It is
            # retained and reported, never interpreted as a published blob.
            with temporary.open("xb") as output:
                self._entry_count += 1
                output.write(raw)
                output.flush()
                os.fsync(output.fileno())
            if self._read_file(temporary) != raw:
                raise ValueError("pending material differs after write")
            # Managed Graph execution is serial. Recheck a published target;
            # never repair or replace a pre-existing mismatched blob.
            if self._exists(target):
                if self.read(reference) != raw:
                    raise ValueError("material target changed before publication")
                return reference
            try:
                os.rename(temporary, target)
            except FileExistsError:
                if self.read(reference) != raw:
                    raise ValueError("material target differs at publication") from None
            if self.read(reference) != raw:
                raise ValueError("published material differs after write")
            return reference
        except WorkingContextError:
            raise
        except (OSError, ValueError, TypeError) as exc:
            raise WorkingContextError("working_context_material_invalid") from exc

    def read(self, ref: dict[str, Any]) -> bytes:
        try:
            reference = _reference(ref)
            return self._read_file(self.directory / (reference["sha256"] + ".blob"),
                                   expected_hash=reference["sha256"], expected_bytes=reference["bytes"])
        except WorkingContextError:
            raise
        except (OSError, ValueError, TypeError) as exc:
            raise WorkingContextError("working_context_material_invalid") from exc

    def resolve(self, manifest: dict[str, Any]) -> dict[str, bytes]:
        try:
            checked = _manifest(manifest)
            unique: dict[str, bytes] = {}
            raw: dict[str, bytes] = {}
            for reference, item in checked.items():
                blob = unique.get(item["sha256"])
                if blob is None:
                    blob = self.read(item)
                    unique[item["sha256"]] = blob
                if len(blob) != item["bytes"]:
                    raise ValueError("same digest has conflicting material sizes")
                raw[reference] = blob
            return raw
        except WorkingContextError:
            raise
        except (OSError, ValueError, TypeError) as exc:
            raise WorkingContextError("working_context_material_invalid") from exc

    def audit_namespace(self) -> dict[str, int]:
        try:
            result = {"unique_blob_count": 0, "unique_blob_bytes": 0, "pending_count": 0, "pending_bytes": 0}
            entries = self._entries()
            identities = {path: _identity(path.lstat()) for path in entries}
            for path in entries:
                match = _BLOB.fullmatch(path.name)
                raw = self._read_file(path, expected_hash=None if match is None else match[1])
                prefix = "pending" if match is None else "unique_blob"
                result[prefix + "_count"] += 1
                result[prefix + "_bytes"] += len(raw)
            if self._entries() != entries or any(_identity(path.lstat()) != identities[path] for path in entries):
                raise ValueError("material namespace changed during audit")
            return result
        except WorkingContextError:
            raise
        except (OSError, ValueError, TypeError) as exc:
            raise WorkingContextError("working_context_material_invalid") from exc
