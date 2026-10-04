"""Host-approved historical guidance and materialized immutable knowledge.

This independent slice exposes no Agent tools, retrieval, learning calls or
application validation. Host approval pins the complete visible document;
bytes and permissions do not certify its natural-language semantics. Raw
synthetic history stays audit-only, never current task evidence.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import BaseModel

from .contracts import require_hash, require_unique
from .knowledge_contracts import (
    MAX_CLOSURE_BYTES,
    MAX_PAYLOAD_BYTES,
    MAX_SNAPSHOT_ENTRIES,
    ExperienceDraft,
    ExperienceEntry,
    ExperiencePayload,
    HistoricalMaterial,
    HistoricalSource,
    HostMaterialManifest,
    KnowledgeSnapshot,
    MaterialGrant,
    PermissionScope,
    RetrieverSpec,
    SnapshotPayload,
    SnapshotRef,
    SolverProjection,
    content_hash,
    encode,
)
from .task import digest
from .working_context import _guard
from .working_context_materials import (
    _directory_anchor,
    _identity,
    _open_identity,
    _regular,
)

_INVALID = (OSError, ValueError, TypeError, KeyError, UnicodeError, RecursionError)


class KnowledgeError(RuntimeError):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


def _copy[T: BaseModel](value: T) -> T:
    return type(value).model_validate_json(encode(value))


def _object(raw: bytes) -> dict[str, Any]:
    """Allow approved source whitespace, never ambiguous duplicate keys."""
    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, item in pairs:
            if key in result:
                raise ValueError("duplicate knowledge material key")
            result[key] = item
        return result

    def nonfinite(_: str) -> Any:
        raise ValueError("nonfinite knowledge material")

    result = json.loads(raw.decode("utf-8"), object_pairs_hook=unique, parse_constant=nonfinite)
    if not isinstance(result, dict):
        raise TypeError("knowledge material must be an object")
    encode(result)
    return result


def _path(root: Path, path: Path, *, directory: bool = False) -> Path:
    root, path = root.absolute(), path.absolute()
    if ".." in root.parts or ".." in path.parts:
        raise ValueError("knowledge path is not canonical")
    path.relative_to(root)
    return _guard(path, directory=directory)


def _exists(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    return True


def _read(root: Path, path: Path, *, expected_hash: str | None = None,
          expected_bytes: int | None = None) -> bytes:
    path = _path(root, path)
    parents = {parent: _directory_anchor(parent.lstat()) for parent in path.parents}
    before = path.lstat()
    _regular(before)
    if before.st_size > MAX_PAYLOAD_BYTES or (expected_bytes is not None and before.st_size != expected_bytes):
        raise ValueError("knowledge material size differs or exceeds bound")
    with path.open("rb") as source:
        opened = os.fstat(source.fileno())
        if _open_identity(opened) != _open_identity(before):
            raise ValueError("knowledge file changed before open")
        raw = source.read(min(before.st_size + 1, MAX_PAYLOAD_BYTES + 1))
        if _identity(os.fstat(source.fileno())) != _identity(opened):
            raise ValueError("knowledge file changed during read")
    _path(root, path)
    after = path.lstat()
    _regular(after)
    if (_identity(before) != _identity(after) or len(raw) != before.st_size or
            any(_directory_anchor(parent.lstat()) != anchor for parent, anchor in parents.items())):
        raise ValueError("knowledge file or ancestor changed after read")
    if expected_hash is not None and digest(raw) != expected_hash:
        raise ValueError("knowledge original bytes differ")
    return raw


def _fd_identity(root: Path, path: Path) -> tuple[int, int, int, int, int, int]:
    """Same-interface change-time check, including Windows Python 3.13."""
    _path(root, path)
    before = path.lstat()
    with path.open("rb") as source:
        opened = os.fstat(source.fileno())
        _regular(opened)
        if _open_identity(opened) != _open_identity(before):
            raise ValueError("knowledge descriptor identity differs")
        identity = _identity(opened)
        if _identity(os.fstat(source.fileno())) != identity:
            raise ValueError("knowledge descriptor changed during observation")
    _path(root, path)
    if _identity(path.lstat()) != _identity(before):
        raise ValueError("knowledge path changed during descriptor observation")
    return identity


class _ReadSet:
    """Close this operation's complete referenced material, without a cache."""

    def __init__(self, root: Path):
        self.root = root
        self.receipts: dict[Path, tuple[str, int, tuple[int, int, int, int, int, int],
                                        tuple[int, int, int, int, int, int]]] = {}
        self.parents: dict[Path, tuple[int, int, int, int]] = {}
        self.sizes: dict[Path, int] = {}
        self.byte_count = 0

    def reserve(self, path: Path, size: int) -> None:
        if path in self.sizes:
            if self.sizes[path] != size:
                raise ValueError("knowledge closure size changed")
            return
        if size < 0 or self.byte_count + size > MAX_CLOSURE_BYTES:
            raise ValueError("knowledge referenced closure exceeds byte limit")
        self.sizes[path] = size
        self.byte_count += size

    def read(self, path: Path, *, expected_hash: str | None = None, expected_bytes: int | None = None) -> bytes:
        path = _path(self.root, path)
        before = path.lstat()
        _regular(before)
        if expected_bytes is not None and before.st_size != expected_bytes:
            raise ValueError("knowledge closure declared size differs")
        self.reserve(path, before.st_size)
        raw = _read(self.root, path, expected_hash=expected_hash, expected_bytes=before.st_size)
        receipt = digest(raw), len(raw), _identity(path.lstat()), _fd_identity(self.root, path)
        if path in self.receipts and self.receipts[path] != receipt:
            raise ValueError("knowledge source changed between closure reads")
        self.receipts[path] = receipt
        for parent in path.parents:
            anchor = _directory_anchor(parent.lstat())
            if parent in self.parents and self.parents[parent] != anchor:
                raise ValueError("knowledge ancestor changed between closure reads")
            self.parents[parent] = anchor
        return raw

    def finish(self) -> None:
        for path, (key, length, _, _) in self.receipts.items():
            _read(self.root, path, expected_hash=key, expected_bytes=length)
        # Close all receipts after the last byte reread. Windows lstat ctime
        # alone misses an in-place edit with restored mtime; fstat observes
        # change time. This remains a serial host protocol, not an OS lock.
        for path, (_, _, identity, descriptor) in self.receipts.items():
            _path(self.root, path)
            if _identity(path.lstat()) != identity or _fd_identity(self.root, path) != descriptor:
                raise ValueError("knowledge source changed after closure construction")
        if any(_directory_anchor(parent.lstat()) != anchor for parent, anchor in self.parents.items()):
            raise ValueError("knowledge ancestor changed after closure construction")


class HostMaterialRegistry:
    """Read-only capture of explicit host approvals, not a model proposal.

    Construction captures approved bytes independently of their original
    files. Later edits require a new manifest/registry; they cannot update
    this capture. Publication copies the captured bytes, and every store
    read independently authenticates the materialized copies. Only the
    aggregate bounded input and explicit grants are admitted, never a tree
    scan or a guessed role based on the document's own instructions.
    """

    def __init__(self, source_root: Path, manifest: HostMaterialManifest):
        try:
            checked = _copy(manifest)
            if sum(item.bytes for item in checked.grants) > MAX_PAYLOAD_BYTES:
                raise ValueError("knowledge registry material aggregate exceeds bound")
            root = _guard(source_root, directory=True)
            reads = _ReadSet(root)
            captured: dict[str, tuple[MaterialGrant, bytes]] = {}
            for grant in checked.grants:
                raw = reads.read(root / grant.path, expected_hash=grant.sha256, expected_bytes=grant.bytes)
                captured[grant.material_id] = grant, raw
            reads.finish()
            self._manifest_json = encode(checked)
            self._captured = captured
        except _INVALID as exc:
            raise KnowledgeError("knowledge_material_invalid") from exc

    def material(self, material_id: str) -> tuple[MaterialGrant, bytes]:
        try:
            grant, raw = self._captured[material_id]
            return _copy(grant), raw
        except _INVALID as exc:
            raise KnowledgeError("knowledge_material_invalid") from exc

    def manifest(self) -> HostMaterialManifest:
        return HostMaterialManifest.model_validate_json(self._manifest_json)


def _learn(grants: tuple[MaterialGrant, ...]) -> None:
    for grant in grants:
        if (grant.role not in {"curated_projection", "inference_projection", "synthetic_history"} or
                "learn" not in grant.allowed_uses or grant.origin_split not in {"curated", "development"} or
                grant.visibility == "restricted"):
            raise KnowledgeError("knowledge_permission_denied")
        if grant.role.endswith("projection") and grant.visibility != "solver_approved":
            raise KnowledgeError("knowledge_permission_denied")


def _solve(payload: ExperiencePayload, scope: PermissionScope) -> None:
    _learn(payload.source_grants)
    if any(scope.use not in grant.allowed_uses or scope.split not in grant.allowed_splits
           for grant in payload.source_grants):
        raise KnowledgeError("knowledge_permission_denied")
    if any(item.record.lineage_group_id in scope.excluded_lineage_groups for item in payload.history):
        raise KnowledgeError("knowledge_permission_denied")


class KnowledgeStore:
    """Serial host store under an explicitly precreated managed directory.

    Construction and all read methods create nothing. Publication preserves
    bounded partial writes as non-authoritative evidence; no latest alias,
    empty-library fallback or automatic cleanup is provided. This is an
    independent API until session/export/SDK integration is explicitly added.
    """

    def __init__(self, root: Path):
        self.root = root.absolute()

    def _directory(self, name: str) -> Path:
        _path(self.root, self.root, directory=True)
        directory = self.root / name
        if not _exists(directory):
            directory.mkdir()
        return _path(self.root, directory, directory=True)

    def _publish(self, folder: str, filename: str, raw: bytes) -> None:
        if len(raw) > MAX_PAYLOAD_BYTES:
            raise ValueError("knowledge record exceeds bound")
        path = self._directory(folder) / filename
        if _exists(path):
            if _read(self.root, path) != raw:
                raise ValueError("existing knowledge record differs")
            return
        temporary = path.parent / ("pending-" + uuid4().hex + ".tmp")
        with temporary.open("xb") as output:
            output.write(raw)
            output.flush()
            os.fsync(output.fileno())
        if _read(self.root, temporary, expected_hash=digest(raw), expected_bytes=len(raw)) != raw:
            raise ValueError("pending knowledge bytes differ")
        # Host publication is serial under the managed job. Do not repair an
        # existing record or use replacement as a recovery path.
        if _exists(path):
            if _read(self.root, path) != raw:
                raise ValueError("knowledge publication target differs")
            return
        os.rename(temporary, path)
        if _read(self.root, path, expected_hash=digest(raw), expected_bytes=len(raw)) != raw:
            raise ValueError("published knowledge bytes differ")

    def admit_entry(self, draft: ExperienceDraft, registry: HostMaterialRegistry) -> ExperienceEntry:
        try:
            draft = _copy(draft)
            projection_grant, raw = registry.material(draft.projection_material_id)
            grants, materials = [projection_grant], [raw]
            for identifier in draft.history_material_ids:
                grant, raw = registry.material(identifier)
                grants.append(grant)
                materials.append(raw)
            _learn(tuple(grants))
            if projection_grant.role not in {"curated_projection", "inference_projection"}:
                raise KnowledgeError("knowledge_permission_denied")
            projection = SolverProjection.model_validate_json(encode(_object(materials[0])))
            history = tuple(HistoricalMaterial(material_id=grant.material_id,
                             record=HistoricalSource.model_validate_json(encode(_object(raw))))
                            for grant, raw in zip(grants[1:], materials[1:], strict=True))
            if any(grant.role != "synthetic_history" for grant in grants[1:]):
                raise KnowledgeError("knowledge_permission_denied")
            payload = ExperiencePayload(projection=projection, source_grants=tuple(grants), history=history,
                extraction_call_id=projection_grant.derivation_call_id, known_tokens=projection_grant.known_tokens,
                cost_authority="unknown" if projection_grant.known_tokens is None else "host_declaration")
            key = content_hash(payload)
            entry = ExperienceEntry(entry_id="experience-" + key, content_sha256=key, payload_json=encode(payload))
            encoded = encode(entry).encode("utf-8")
            material_sizes = {grant.sha256: grant.bytes for grant in grants}
            if len(encoded) + sum(material_sizes.values()) > MAX_CLOSURE_BYTES:
                raise ValueError("knowledge entry referenced closure exceeds byte limit")
            # All semantic shape/permission/capacity checks precede publication.
            for grant, raw in zip(grants, materials, strict=True):
                self._publish("materials", grant.sha256 + ".blob", raw)
            self._publish("entries", entry.entry_id + ".json", encoded)
            return self.read_entry(entry.entry_id)
        except _INVALID as exc:
            raise KnowledgeError("knowledge_entry_invalid") from exc

    def _entry(self, entry_id: str, reads: _ReadSet) -> tuple[ExperienceEntry, ExperiencePayload]:
        if not isinstance(entry_id, str) or not entry_id.startswith("experience-"):
            raise ValueError("knowledge entry identity is invalid")
        require_hash(entry_id.removeprefix("experience-"))
        raw = reads.read(self.root / "entries" / (entry_id + ".json"))
        entry = ExperienceEntry.model_validate_json(encode(_object(raw)))
        if entry.entry_id != entry_id or encode(entry).encode("utf-8") != raw:
            raise ValueError("knowledge entry filename or canonical bytes differ")
        payload = ExperiencePayload.model_validate_json(entry.payload_json)
        _learn(payload.source_grants)
        projections = [item for item in payload.source_grants if item.role.endswith("projection")]
        history = {item.material_id: item.record for item in payload.history}
        for grant in payload.source_grants:
            material = reads.read(self.root / "materials" / (grant.sha256 + ".blob"),
                                  expected_hash=grant.sha256, expected_bytes=grant.bytes)
            if grant == projections[0]:
                if SolverProjection.model_validate_json(encode(_object(material))) != payload.projection:
                    raise ValueError("knowledge projection differs from its original bytes")
            elif HistoricalSource.model_validate_json(encode(_object(material))) != history[grant.material_id]:
                raise ValueError("knowledge history differs from its original bytes")
        return entry, payload

    def read_entry(self, entry_id: str) -> ExperienceEntry:
        try:
            reads = _ReadSet(self.root)
            entry = self._entry(entry_id, reads)[0]
            reads.finish()
            return entry
        except _INVALID as exc:
            raise KnowledgeError("knowledge_entry_invalid") from exc

    def freeze_snapshot(self, entry_ids: tuple[str, ...], scope: PermissionScope,
                        retriever: RetrieverSpec | None = None) -> KnowledgeSnapshot:
        try:
            scope, retriever = _copy(scope), _copy(retriever if retriever is not None else RetrieverSpec())
            if type(entry_ids) is not tuple:
                raise TypeError("knowledge selection must be an explicit tuple")
            if len(entry_ids) > MAX_SNAPSHOT_ENTRIES:
                raise ValueError("knowledge selection exceeds snapshot entry limit")
            require_unique(list(entry_ids))
            reads = _ReadSet(self.root)
            refs: list[SnapshotRef] = []
            for entry_id in sorted(entry_ids):
                entry, payload = self._entry(entry_id, reads)
                _solve(payload, scope)
                refs.append(SnapshotRef(entry_id=entry.entry_id, content_sha256=entry.content_sha256,
                                        projection_sha256=content_hash(payload.projection)))
            payload = SnapshotPayload(entries=tuple(refs), scope=scope, retriever=retriever)
            snapshot = KnowledgeSnapshot(snapshot_sha256=content_hash(payload), payload_json=encode(payload))
            raw = encode(snapshot).encode("utf-8")
            reads.reserve(self.root / "snapshots" / (snapshot.snapshot_sha256 + ".json"), len(raw))
            reads.finish()
            self._publish("snapshots", snapshot.snapshot_sha256 + ".json", raw)
            return self.read_snapshot(snapshot.snapshot_sha256)
        except _INVALID as exc:
            raise KnowledgeError("knowledge_snapshot_invalid") from exc

    def _snapshot(self, snapshot_sha256: str) -> tuple[KnowledgeSnapshot, dict[str, SolverProjection]]:
        require_hash(snapshot_sha256)
        reads = _ReadSet(self.root)
        raw = reads.read(self.root / "snapshots" / (snapshot_sha256 + ".json"))
        snapshot = KnowledgeSnapshot.model_validate_json(encode(_object(raw)))
        if snapshot.snapshot_sha256 != snapshot_sha256 or encode(snapshot).encode("utf-8") != raw:
            raise ValueError("knowledge snapshot filename or canonical bytes differ")
        contents = SnapshotPayload.model_validate_json(snapshot.payload_json)
        projections: dict[str, SolverProjection] = {}
        for ref in contents.entries:
            entry, payload = self._entry(ref.entry_id, reads)
            _solve(payload, contents.scope)
            if entry.content_sha256 != ref.content_sha256 or content_hash(payload.projection) != ref.projection_sha256:
                raise ValueError("knowledge snapshot entry or projection binding differs")
            projections[ref.entry_id] = payload.projection
        reads.finish()
        return snapshot, projections

    def read_snapshot(self, snapshot_sha256: str) -> KnowledgeSnapshot:
        try:
            return self._snapshot(snapshot_sha256)[0]
        except _INVALID as exc:
            raise KnowledgeError("knowledge_snapshot_invalid") from exc

    def read_projection(self, snapshot_sha256: str, entry_id: str) -> SolverProjection:
        try:
            _, projections = self._snapshot(snapshot_sha256)
            if entry_id not in projections:
                raise KnowledgeError("knowledge_permission_denied")
            return _copy(projections[entry_id])
        except _INVALID as exc:
            raise KnowledgeError("knowledge_snapshot_invalid") from exc
