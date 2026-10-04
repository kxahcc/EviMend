"""Explicitly copy approved knowledge into a run and reopen it without writes.

The caller's frozen session profile supplies authority when reopening a run.
The local profile is evidence to compare, never a source of fresh approval.
Only prepare needs the external library; restoration authenticates run originals.
"""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .applicability import ApplicabilityStore
from .applicability_contracts import TaskKnowledgeBinding
from .contracts import require_hash, require_path, require_unique
from .knowledge import KnowledgeError, KnowledgeStore, _copy, _exists, _path, _ReadSet
from .knowledge_contracts import (
    MAX_CLOSURE_BYTES,
    PermissionScope,
    RetrieverSpec,
    content_hash,
    encode,
)
from .knowledge_retrieval import KnowledgeRetriever, _sidecars
from .retrieval_contracts import MAX_INDEX_READ_BYTES, MAX_INDEX_READ_CALLS
from .task import digest
from .verification import CheckLedger
from .workspace import CandidateWorkspace

PROFILE_PATH = "knowledge-profile.json"
BINDING_PATH = "experience-bindings/approved.json"
_ORIGINAL_FOLDERS = {"materials", "entries", "snapshots", "indexes"}
_RESIDUALS = ("knowledge", PROFILE_PATH, "experience-bindings", "experience-queries",
              "applicability-contexts", "applicability-reviews", "applicability-materials")
_INVALID = (OSError, ValueError, TypeError, KeyError, UnicodeError, RecursionError,
            KnowledgeError, sqlite3.Error, OverflowError)


class RunKnowledgeError(ValueError):
    """Do not expose private paths or source text in integration failures."""

    def __init__(self, reason: str = "knowledge_run_invalid"):
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class HostKnowledgeInput:
    """Trusted host inputs, never accepted as model tool arguments."""

    retriever: KnowledgeRetriever
    approval: TaskKnowledgeBinding

    def __post_init__(self) -> None:
        try:
            if not isinstance(self.retriever, KnowledgeRetriever) or not isinstance(self.approval, TaskKnowledgeBinding):
                raise TypeError("knowledge input requires an explicit retriever and host binding")
            object.__setattr__(self, "approval", _copy(self.approval))
        except _INVALID as exc:
            raise RunKnowledgeError() from exc


class _File(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    path: str = Field(min_length=1, max_length=2048)
    sha256: str
    bytes: int = Field(ge=0, le=MAX_CLOSURE_BYTES)

    @model_validator(mode="after")
    def valid_file(self) -> _File:
        require_path(self.path)
        require_hash(self.sha256)
        if self.path == BINDING_PATH:
            return self
        parts = self.path.split("/")
        if len(parts) != 3 or parts[0] != "knowledge" or parts[1] not in _ORIGINAL_FOLDERS:
            raise ValueError("profile does not reference a knowledge original")
        filename = parts[2]
        suffix = ".blob" if parts[1] == "materials" else ".json"
        if parts[1] == "indexes" and filename.endswith(".sqlite"):
            suffix = ".sqlite"
        if not filename.endswith(suffix):
            raise ValueError("knowledge original filename differs")
        stem = filename.removesuffix(suffix)
        if parts[1] == "entries":
            if not stem.startswith("experience-"):
                raise ValueError("knowledge entry filename differs")
            stem = stem.removeprefix("experience-")
        require_hash(stem)
        return self


def _files(files: tuple[_File, ...], *, binding: bool) -> None:
    paths = [file.path for file in files]
    require_unique([path.casefold() for path in paths])
    if paths != sorted(paths) or (BINDING_PATH in paths) != binding:
        raise ValueError("knowledge original manifest order or binding differs")
    if sum(file.bytes for file in files) > MAX_CLOSURE_BYTES:
        raise ValueError("run knowledge original manifest exceeds closure budget")


def _snapshot_files(files: tuple[_File, ...], snapshot: str, index: str) -> None:
    expected = {"knowledge/snapshots/" + snapshot + ".json",
                "knowledge/indexes/" + snapshot + ".json", "knowledge/indexes/" + snapshot + ".sqlite"}
    actual = {file.path for file in files if file.path.startswith(("knowledge/snapshots/", "knowledge/indexes/"))}
    if actual != expected or next(file.sha256 for file in files
                                 if file.path == "knowledge/indexes/" + snapshot + ".json") != index:
        raise ValueError("knowledge manifest snapshot or descriptor identity differs")


class _Profile(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    schema_version: Literal[1]
    approval: TaskKnowledgeBinding
    index_sha256: str
    retriever: RetrieverSpec
    files: tuple[_File, ...] = Field(min_length=4, max_length=MAX_INDEX_READ_CALLS)
    profile_sha256: str

    @model_validator(mode="after")
    def valid_profile(self) -> _Profile:
        require_hash(self.index_sha256)
        require_hash(self.profile_sha256)
        _files(self.files, binding=True)
        _snapshot_files(self.files, self.approval.snapshot_sha256, self.index_sha256)
        binding = next(file for file in self.files if file.path == BINDING_PATH)
        approved_raw = encode(self.approval).encode("utf-8")
        if binding.sha256 != digest(approved_raw) or binding.bytes != len(approved_raw):
            raise ValueError("knowledge manifest approval differs from frozen binding")
        value = self.model_dump(mode="json", exclude={"profile_sha256"})
        if content_hash(value) != self.profile_sha256:
            raise ValueError("run knowledge profile content changed")
        if sum(file.bytes for file in self.files) + len(encode(self).encode("utf-8")) > MAX_CLOSURE_BYTES:
            raise ValueError("run knowledge profile and originals exceed closure budget")
        return self


class _SharedProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    snapshot_sha256: str
    scope: PermissionScope
    retriever: RetrieverSpec
    index_sha256: str
    files: tuple[_File, ...] = Field(min_length=3, max_length=MAX_INDEX_READ_CALLS)

    @model_validator(mode="after")
    def valid_profile(self) -> _SharedProfile:
        require_hash(self.snapshot_sha256)
        require_hash(self.index_sha256)
        _files(self.files, binding=False)
        _snapshot_files(self.files, self.snapshot_sha256, self.index_sha256)
        return self


def normalize_run_knowledge_profile(value: dict[str, Any]) -> dict[str, Any]:
    """Validate caller-supplied frozen identity, with no I/O or authority grant."""
    try:
        if type(value) is not dict:
            raise TypeError("run knowledge identity must be an explicit object")
        return _Profile.model_validate_json(encode(value)).model_dump(mode="json")
    except _INVALID as exc:
        raise RunKnowledgeError() from exc


def normalize_shared_knowledge_profile(value: dict[str, Any]) -> dict[str, Any]:
    """Validate the common knowledge identity used across comparison runs."""
    try:
        if type(value) is not dict:
            raise TypeError("shared knowledge identity must be an explicit object")
        return _SharedProfile.model_validate_json(encode(value)).model_dump(mode="json")
    except _INVALID as exc:
        raise RunKnowledgeError() from exc


def shared_knowledge_profile(profile: dict[str, Any]) -> dict[str, Any]:
    value = normalize_run_knowledge_profile(profile)
    return normalize_shared_knowledge_profile({
        "snapshot_sha256": value["approval"]["snapshot_sha256"],
        "scope": value["approval"]["scope"], "retriever": value["retriever"],
        "index_sha256": value["index_sha256"],
        "files": [file for file in value["files"] if file["path"].startswith("knowledge/")],
    })


class _Capture(_ReadSet):
    def __init__(self, root: Path):
        super().__init__(root, max_read_bytes=MAX_INDEX_READ_BYTES, max_read_calls=MAX_INDEX_READ_CALLS)
        self.originals: dict[Path, bytes] = {}

    def read(self, path: Path, *, expected_hash: str | None = None,
             expected_bytes: int | None = None) -> bytes:
        raw = super().read(path, expected_hash=expected_hash, expected_bytes=expected_bytes)
        self.originals[path.absolute()] = raw
        return raw


def _disabled(workspace: CandidateWorkspace) -> None:
    _path(workspace.root, workspace.root, directory=True)
    if any(_exists(workspace.root / name) for name in _RESIDUALS):
        raise ValueError("knowledge configuration cannot discard existing run evidence")


def _services(workspace: CandidateWorkspace, task_directory: Path, ledger: CheckLedger,
              approval: TaskKnowledgeBinding) -> ApplicabilityStore:
    return ApplicabilityStore(workspace, task_directory=task_directory, ledger=ledger,
        retriever=KnowledgeRetriever(KnowledgeStore(workspace.root / "knowledge")), approval=approval)


def _owner(store: ApplicabilityStore, *, binding: bool = False) -> None:
    with store._operation() as op:
        if binding:
            store._binding(op)


def _original_names(workspace: CandidateWorkspace) -> set[str]:
    """Inspect only this run's fixed immutable namespaces; never external trees."""
    root = _path(workspace.root, workspace.root / "knowledge", directory=True)
    found: set[str] = set()
    with os.scandir(root) as children:
        for child in children:
            if child.name not in _ORIGINAL_FOLDERS | {"retrievals"}:
                raise ValueError("run knowledge has an unexpected namespace")
            folder = _path(workspace.root, Path(child.path), directory=True)
            if child.name == "retrievals":
                continue
            with os.scandir(folder) as leaves:
                for leaf in leaves:
                    path = _path(workspace.root, Path(leaf.path))
                    found.add(path.relative_to(workspace.root).as_posix())
                    if len(found) > MAX_INDEX_READ_CALLS:
                        raise ValueError("run knowledge namespace exceeds file budget")
    binding_root = _path(workspace.root, workspace.root / "experience-bindings", directory=True)
    with os.scandir(binding_root) as bindings:
        binding_names = {_path(workspace.root, Path(file.path)).name for file in bindings}
    if binding_names != {"approved.json"}:
        raise ValueError("run has an unexpected approval original")
    return found


def prepare_run_knowledge(workspace: CandidateWorkspace, task_directory: Path,
                          host_input: HostKnowledgeInput | None, *, ledger: CheckLedger
                          ) -> dict[str, Any] | None:
    """The sole explicit external-copy entry; partial evidence is never repaired."""
    try:
        _disabled(workspace)
        if host_input is None:
            return None
        if not isinstance(host_input, HostKnowledgeInput):
            raise TypeError("knowledge input must be an explicit host approval")
        approved = _copy(host_input.approval)
        local = _services(workspace, task_directory, ledger, approved)
        _owner(local)
        source = host_input.retriever
        source_root = _path(source.store.root, source.store.root, directory=True)
        target_root = (workspace.root / "knowledge").absolute()
        if source_root.is_relative_to(workspace.root.absolute()) or workspace.root.absolute().is_relative_to(source_root):
            raise ValueError("run and external knowledge library must be separate")
        reads = _Capture(source_root)
        contents, projections = source._bundle(approved.snapshot_sha256, approved.scope, reads)
        descriptor, _ = source._index(contents, projections, approved.snapshot_sha256, reads)
        originals = {"knowledge/" + path.relative_to(source_root).as_posix(): raw
                     for path, raw in reads.originals.items()}
        originals[BINDING_PATH] = encode(approved).encode("utf-8")
        payload: dict[str, Any] = {
            "schema_version": 1, "approval": approved.model_dump(mode="json"),
            "index_sha256": content_hash(descriptor), "retriever": contents.retriever.model_dump(mode="json"),
            "files": [{"path": path, "sha256": digest(raw), "bytes": len(raw)}
                      for path, raw in sorted(originals.items())],
        }
        profile = normalize_run_knowledge_profile({**payload, "profile_sha256": content_hash(payload)})
        # Simulate bind with actual owner reads before creating the knowledge
        # root. Include the complete source/index closure conservatively;
        # capacity and closing checks use ApplicabilityStore's own limits.
        binding_bytes = len(originals[BINDING_PATH])
        with local._operation() as operation:
            operation.knowledge_bytes = reads.byte_count
            operation.reserve(workspace.root / BINDING_PATH, binding_bytes)
            operation.finish(future_bytes=2 * binding_bytes, future_calls=2)
        # All output capacity and source closing checks precede the first write.
        reads.finish()
        # Source closes once more after copying. The independent local audit
        # repeats this first closed read plus the profile and binding twice.
        extra_bytes = 2 * (len(encode(profile).encode("utf-8")) + len(originals[BINDING_PATH]))
        if (reads.read_calls + len(reads.receipts) > MAX_INDEX_READ_CALLS or
                reads.read_bytes + reads.byte_count > MAX_INDEX_READ_BYTES or
                reads.read_calls + 4 > MAX_INDEX_READ_CALLS or
                reads.read_bytes + extra_bytes > MAX_INDEX_READ_BYTES):
            raise ValueError("run knowledge post-copy closing or audit budget exceeded")
        _sidecars(source_root, source_root / "indexes" / (approved.snapshot_sha256 + ".sqlite"))
        _path(workspace.root, workspace.root, directory=True)
        _disabled(workspace)
        target_root.mkdir()
        _path(workspace.root, target_root, directory=True)
        for path, raw in sorted(originals.items()):
            if path == BINDING_PATH:
                continue
            _, folder, filename = path.split("/")
            local.retriever.store._publish(folder, filename, raw)
        local.bind()
        # Detect edits to the external source made while copying its captured bytes.
        reads.finish()
        _sidecars(source_root, source_root / "indexes" / (approved.snapshot_sha256 + ".sqlite"))
        KnowledgeStore(workspace.root)._publish(".", PROFILE_PATH, encode(profile).encode("utf-8"))
        read_run_knowledge(workspace, task_directory, profile, ledger=ledger)
        return json.loads(encode(profile))
    except RunKnowledgeError:
        raise
    except _INVALID as exc:
        raise RunKnowledgeError() from exc


def read_run_knowledge(workspace: CandidateWorkspace, task_directory: Path,
                       profile: dict[str, Any] | None, *, ledger: CheckLedger
                       ) -> ApplicabilityStore | None:
    """Authenticate against external frozen session identity, never publish."""
    try:
        if profile is None:
            _disabled(workspace)
            return None
        value = normalize_run_knowledge_profile(profile)
        approved = TaskKnowledgeBinding.model_validate_json(encode(value["approval"]))
        store = _services(workspace, task_directory, ledger, approved)
        _owner(store, binding=True)
        reads = _Capture(workspace.root)
        profile_raw = encode(value).encode("utf-8")
        if reads.read(workspace.root / PROFILE_PATH, expected_hash=digest(profile_raw),
                      expected_bytes=len(profile_raw)) != profile_raw:
            raise ValueError("saved run profile differs from caller identity")
        contents, projections = store.retriever._bundle(approved.snapshot_sha256, approved.scope, reads)
        descriptor, _ = store.retriever._index(contents, projections, approved.snapshot_sha256, reads)
        if (content_hash(descriptor) != value["index_sha256"] or
                contents.retriever.model_dump(mode="json") != value["retriever"]):
            raise ValueError("saved index or retriever differs from frozen run profile")
        approved_raw = encode(approved).encode("utf-8")
        if reads.read(workspace.root / BINDING_PATH) != approved_raw:
            raise ValueError("local approval differs from caller session authority")
        actual = [{"path": path.relative_to(workspace.root).as_posix(),
                   "sha256": digest(raw), "bytes": len(raw)}
                  for path, raw in sorted(reads.originals.items()) if path != workspace.root / PROFILE_PATH]
        if actual != value["files"]:
            raise ValueError("saved manifest differs from complete run knowledge originals")
        expected = {file["path"] for file in value["files"] if file["path"].startswith("knowledge/")}
        if _original_names(workspace) != expected:
            raise ValueError("run has unapproved knowledge originals")
        reads.finish()
        _sidecars(store.retriever.store.root,
                  store.retriever.store.root / "indexes" / (approved.snapshot_sha256 + ".sqlite"))
        if _original_names(workspace) != expected:
            raise ValueError("run original namespace changed during audit")
        return store
    except RunKnowledgeError:
        raise
    except _INVALID as exc:
        raise RunKnowledgeError() from exc
