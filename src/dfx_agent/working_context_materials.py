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
)
from .contracts import require_hash, require_path
from .model_calls import canonical
from .task import digest
from .working_context import WorkingContextError
from .working_context import _guard as _source_guard

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


def _regular(info: os.stat_result) -> None:
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or
            getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)):
        raise ValueError("material is not an independent regular file")


def _directory_anchor(info: os.stat_result) -> tuple[int, int, int, int]:
    # Ancestors may contain unrelated active files. Bind their directory
    # identity/type, rather than treating sibling creation as a source edit.
    return info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode), getattr(info, "st_file_attributes", 0)


class _NamespaceRead:
    """One closed read of direct children, never a reusable filesystem cache.

    The directory and every ancestor are guarded before and after this read.
    Each opened leaf must match its independently captured original identity;
    fd and raw-byte checks remain in _read_file. No other path is admitted.
    """

    def __init__(self, directory: Path, paths: list[Path]):
        self.directory = _source_guard(directory, directory=True)
        self.parents = {path: _directory_anchor(path.lstat()) for path in (*reversed(self.directory.parents), self.directory)}
        self.directory_identity = _identity(self.directory.lstat())
        self.files: dict[Path, tuple[int, int, int, int, int, int]] = {}
        for path in paths:
            if path.absolute().parent != self.directory:
                raise ValueError("material read is outside its direct namespace")
            info = path.lstat()
            _regular(info)
            self.files[path] = _identity(info)

    def guard(self, path: Path) -> None:
        if path not in self.files or _identity(self.directory.lstat()) != self.directory_identity:
            raise ValueError("material read namespace changed")
        info = path.lstat()
        _regular(info)
        if _identity(info) != self.files[path]:
            raise ValueError("material leaf changed within namespace read")

    def finish(self) -> None:
        _source_guard(self.directory, directory=True)
        if (_identity(self.directory.lstat()) != self.directory_identity or
                any(_directory_anchor(path.lstat()) != expected for path, expected in self.parents.items())):
            raise ValueError("material namespace changed within read")
        for path, expected in self.files.items():
            info = path.lstat()
            _regular(info)
            if _identity(info) != expected:
                raise ValueError("material leaf changed within read")


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
            # Component containment precedes I/O; do not resolve away a link
            # or '..'. The shared source guard checks every unresolved parent,
            # including those above the run root. Reads still bracket this
            # check with independent path/descriptor identity observations.
            root, target = self.io.root.absolute(), path.absolute()
            if ".." in root.parts or ".." in target.parts:
                raise ValueError("material path is not canonical")
            target.relative_to(root)
            _source_guard(target, directory=not file)
        except (OSError, ValueError) as exc:
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
        before = _identity(self.directory.lstat())
        for path in self.directory.iterdir():
            _regular(path.lstat())
            if _BLOB.fullmatch(path.name) is None and _PENDING.fullmatch(path.name) is None:
                raise ValueError("unexpected material namespace entry")
            found.append(path)
            if len(found) > MAX_MATERIAL_FILES:
                raise WorkingContextError("working_context_material_limit")
        self._guard(self.directory, file=False)
        if _identity(self.directory.lstat()) != before:
            raise ValueError("material inventory changed")
        return sorted(found)

    def _read_file(self, path: Path, *, expected_hash: str | None = None,
                   expected_bytes: int | None = None, namespace: _NamespaceRead | None = None) -> bytes:
        guard = self._guard if namespace is None else namespace.guard
        guard(path)
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
            # Bound the request to observed size plus one growth sentinel;
            # small blobs must not request an 8MiB buffer on every reread.
            raw = source.read(min(before.st_size + 1, MAX_ARTIFACT_BYTES + 1))
            if _identity(os.fstat(source.fileno())) != _identity(opened):
                raise ValueError("material changed during read")
        guard(path)
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

    def put_manifest(self, material: dict[str, bytes]) -> dict[str, dict[str, Any]]:
        """Publish unique exact bytes once, retaining every scoped reference.

        All input validation precedes publication. Deduplication exists only
        inside this invocation; later invocations reread existing blobs in a
        closed namespace operation. The caller still verifies receipts and the complete namespace
        before publishing a plan; this method does not certify a whole plan.
        """
        try:
            if not isinstance(material, dict):
                raise TypeError("raw material manifest must be an object")
            if len(material) > MAX_MATERIAL_FILES:
                raise WorkingContextError("working_context_material_limit")
            manifest: dict[str, dict[str, Any]] = {}
            unique: dict[str, bytes] = {}
            for reference, raw in material.items():
                if not isinstance(raw, bytes):
                    raise TypeError("material must be immutable bytes")
                if len(raw) > MAX_ARTIFACT_BYTES:
                    raise WorkingContextError("working_context_material_limit")
                key = digest(raw)
                previous = unique.get(key)
                if previous is not None and (len(previous) != len(raw) or previous != raw):
                    raise ValueError("same digest has conflicting original material bytes")
                unique[key] = raw
                manifest[reference] = {"sha256": key, "bytes": len(raw)}
            checked = _manifest(manifest)
            if not unique:
                return checked
            self._ensure_directory()
            existing = {key for key in unique if self._exists(self.directory / (key + ".blob"))}
            current = self.resolve({reference: item for reference, item in checked.items()
                                    if item["sha256"] in existing})
            if any(raw != material[reference] for reference, raw in current.items()):
                raise ValueError("existing batch material differs from requested original bytes")
            for key, raw in unique.items():
                if key in existing:
                    continue
                if self.put(raw) != {"sha256": key, "bytes": len(raw)}:
                    raise ValueError("published material descriptor differs from its manifest")
            return checked
        except WorkingContextError:
            raise
        except (OSError, ValueError, TypeError) as exc:
            raise WorkingContextError("working_context_material_invalid") from exc

    def resolve(self, manifest: dict[str, Any]) -> dict[str, bytes]:
        try:
            checked = _manifest(manifest)
            if not checked:
                return {}
            self._guard(self.directory, file=False)
            paths = [self.directory / (key + ".blob") for key in sorted({item["sha256"] for item in checked.values()})]
            namespace = _NamespaceRead(self.directory, paths)
            unique: dict[str, bytes] = {}
            raw: dict[str, bytes] = {}
            for reference, item in checked.items():
                blob = unique.get(item["sha256"])
                if blob is None:
                    blob = self._read_file(self.directory / (item["sha256"] + ".blob"),
                                           expected_hash=item["sha256"], expected_bytes=item["bytes"],
                                           namespace=namespace)
                    unique[item["sha256"]] = blob
                if len(blob) != item["bytes"]:
                    raise ValueError("same digest has conflicting material sizes")
                raw[reference] = blob
            namespace.finish()
            return raw
        except WorkingContextError:
            raise
        except (OSError, ValueError, TypeError) as exc:
            raise WorkingContextError("working_context_material_invalid") from exc

    def inspect(self, manifest: dict[str, Any]) -> tuple[dict[str, bytes], dict[str, int]]:
        """Resolve scoped originals while auditing the same complete namespace.

        Each inventory entry receives the existing guarded original-byte
        verification. Only referenced blobs remain in memory; orphan and
        pending entries contribute observations without becoming task facts.
        The final independent inventory and identities close this invocation,
        whose raw materials are never cached for a later host operation.
        """
        try:
            checked = _manifest(manifest)
            wanted = {item["sha256"] for item in checked.values()}
            result = {"unique_blob_count": 0, "unique_blob_bytes": 0, "pending_count": 0, "pending_bytes": 0}
            entries = self._entries()
            identities = {path: _identity(path.lstat()) for path in entries}
            namespace = _NamespaceRead(self.directory, entries) if entries else None
            unique: dict[str, bytes] = {}
            for path in entries:
                match = _BLOB.fullmatch(path.name)
                raw = self._read_file(path, expected_hash=None if match is None else match[1], namespace=namespace)
                prefix = "pending" if match is None else "unique_blob"
                result[prefix + "_count"] += 1
                result[prefix + "_bytes"] += len(raw)
                if match is not None and match[1] in wanted:
                    unique[match[1]] = raw
            material: dict[str, bytes] = {}
            for reference, item in checked.items():
                raw = unique.get(item["sha256"])
                if raw is None or len(raw) != item["bytes"]:
                    raise ValueError("referenced material is missing or has conflicting size")
                material[reference] = raw
            if self._entries() != entries or any(_identity(path.lstat()) != identities[path] for path in entries):
                raise ValueError("material namespace changed during audit")
            if namespace is not None:
                namespace.finish()
            return material, result
        except WorkingContextError:
            raise
        except (OSError, ValueError, TypeError) as exc:
            raise WorkingContextError("working_context_material_invalid") from exc

    def audit_namespace(self) -> dict[str, int]:
        return self.inspect({})[1]
