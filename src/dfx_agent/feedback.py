"""Stopped development-run capsules and explicitly host-curated candidates.

No model, tool, Graph or application is executed. Readback reaggregates copied
raw facts with existing readers. Copy/closure byte limits do not cover every
internal read made by aggregation or establish total process I/O or memory.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import BaseModel

from .evidence_io import local_evidence_path
from .exporting import _TASK_ENTRIES, _WORK_ENTRIES
from .feedback_contracts import (
    CandidatePayload,
    ExperienceCandidate,
    FeedbackCapsule,
    FeedbackFile,
    FeedbackLimits,
    FeedbackPayload,
    HostCandidateApproval,
    HostFeedbackApproval,
    _identity,
    _object,
    _reference,
    content_hash,
    encode,
)
from .knowledge import KnowledgeError, KnowledgeStore
from .knowledge_contracts import ExperiencePayload, SnapshotPayload, SolverProjection
from .knowledge_run import normalize_run_knowledge_profile, read_run_knowledge
from .metrics import (
    AnyEvaluationManifest,
    _guard,
    _load,
    _overlap,
    aggregate_evaluation,
    parse_evaluation_manifest,
)
from .metrics import (
    _session as _evaluation_session,
)
from .model_calls import canonical, sha256
from .provenance import validate_session_provenance
from .reporting import _KNOWLEDGE_ENTRIES, _STATIC_SKILL_ENTRIES
from .task import digest
from .verification import CheckLedger, ReplayOnlyExecutor
from .working_context_materials import (
    _directory_anchor,
    _open_identity,
    _regular,
)
from .working_context_materials import (
    _identity as _file_identity,
)
from .workspace import CandidateWorkspace

_GRAPH = frozenset({"checkpoints.sqlite", "checkpoints.sqlite-wal", "checkpoints.sqlite-shm", "checkpoints.sqlite-journal"})


def _copy[T: BaseModel](value: T) -> T:
    """Validate even model_copy/model_construct bypasses without filling fields."""
    original = encode(value)
    checked = type(value).model_validate_json(original)
    if encode(checked) != original:
        raise ValueError("feedback input was not an exact validated object")
    return checked


def _exists(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    return True


def _closure_directory_anchor(info: os.stat_result) -> tuple[int, int, int, int]:
    """Normalize only the NTFS on-disk directory bit for ephemeral closure.

    NTFS defines its directory attribute as 0x10000000, while WinNT exposes
    FILE_ATTRIBUTE_DIRECTORY as 0x10. A captured empty directory was observed
    changing only that high bit. Keep every other flag and identity component;
    path guards still reject reparse points. Persistent identities use the
    original working_context_materials anchor without this normalization.
    Sources: linux/fs/ntfs3/ntfs.h and Microsoft's File Attribute Constants.
    """
    device, inode, kind, attributes = _directory_anchor(info)
    if stat.S_ISDIR(info.st_mode):
        attributes &= ~0x10000000
    return device, inode, kind, attributes


class _ReadBudget:
    """One API operation shares this counter across every copy/closure phase."""

    def __init__(self, maximum: int):
        self.maximum, self.used = maximum, 0

    def require_remaining(self, size: int) -> None:
        if size < 0 or self.used + size > self.maximum:
            raise ValueError("feedback cumulative capture read capacity exceeded")

    def charge(self, size: int) -> None:
        self.require_remaining(size)
        self.used += size


class _ClosedTree:
    """Bounded raw reads and a final FD/byte/membership closure, with no cache reuse."""

    def __init__(self, root: Path, limits: FeedbackLimits, budget: _ReadBudget | None = None):
        self.root, self.limits = local_evidence_path(root), limits
        self.budget = budget if budget is not None else _ReadBudget(limits.max_capture_read_bytes)
        _guard(self.root)
        if not self.root.is_dir():
            raise ValueError("feedback source root must be an existing directory")
        self.raw: dict[str, bytes] = {}
        self.files: dict[str, FeedbackFile] = {}
        self.directories: set[str] = set()
        self.members: dict[Path, tuple[str, ...]] = {}
        self.directory_identities: dict[Path, tuple[int, int, int, int]] = {}
        self.receipts: dict[Path, tuple[tuple[int, int, int, int, int, int],
                                      tuple[int, int, int, int, int, int]]] = {}
        self.byte_count = self.entry_count = 0

    def _read(self, path: Path, *, expected: FeedbackFile | None = None) -> tuple[bytes, tuple[int, int, int, int, int, int]]:
        _guard(path)
        before = path.lstat()
        _regular(before)
        if before.st_size > self.limits.max_file_bytes:
            raise ValueError("feedback capture read capacity exceeded")
        if expected is not None and before.st_size != expected.bytes:
            raise ValueError("feedback original size changed")
        self.budget.charge(before.st_size)
        with path.open("rb") as stream:
            opened = os.fstat(stream.fileno())
            _regular(opened)
            if _open_identity(opened) != _open_identity(before):
                raise ValueError("feedback file changed before descriptor open")
            raw = stream.read(before.st_size)
            descriptor = _file_identity(os.fstat(stream.fileno()))
            if descriptor != _file_identity(opened):
                raise ValueError("feedback file changed during descriptor read")
        _guard(path)
        if _file_identity(path.lstat()) != _file_identity(before) or len(raw) != before.st_size:
            raise ValueError("feedback file changed after descriptor read")
        if expected is not None and digest(raw) != expected.sha256:
            raise ValueError("feedback original bytes changed")
        return raw, descriptor

    def _parents(self, path: Path) -> None:
        for parent in (path, *path.parents):
            anchor = _closure_directory_anchor(parent.lstat())
            previous = self.directory_identities.setdefault(parent, anchor)
            if previous != anchor:
                raise ValueError("feedback ancestor identity changed")

    def capture_file(self, relative: str) -> bytes:
        """Capture one exact record without admitting its unrelated siblings.

        The caller must finish this reader after its semantic authentication.
        This shares the same byte budget and descriptor closure as tree scans.
        """
        _reference(relative)
        if relative in self.raw:
            raise ValueError("feedback input trees overlap")
        path = self.root / relative
        self._parents(path.parent)
        if self.entry_count + 1 > self.limits.max_entries:
            raise ValueError("feedback tree entry capacity exceeded")
        raw, descriptor = self._read(path)
        if self.byte_count + len(raw) > self.limits.max_total_bytes:
            raise ValueError("feedback raw closure capacity exceeded")
        self.entry_count += 1
        self.byte_count += len(raw)
        self.raw[relative] = raw
        self.files[relative] = FeedbackFile(path=relative, sha256=digest(raw), bytes=len(raw))
        self.receipts[path] = _file_identity(path.lstat()), descriptor
        self.directories.update(parent.as_posix() for parent in Path(relative).parents if parent.as_posix() != ".")
        if len(self.directories) + len(self.files) + 1 > self.limits.max_entries:
            raise ValueError("feedback complete inventory capacity exceeded")
        return raw

    def scan(self, roots: tuple[tuple[str, frozenset[str] | None], ...], *, exclude_graph: bool = False) -> None:
        for relative, allowed in roots:
            if relative != ".":
                _reference(relative)
            start = self.root if relative == "." else self.root / relative
            _guard(start)
            if not start.is_dir():
                raise ValueError("feedback input tree is missing")
            pending = [start]
            while pending:
                directory = pending.pop()
                _guard(directory)
                self._parents(directory)
                key = directory.relative_to(self.root).as_posix()
                if key != ".":
                    _reference(key)
                    self.directories.add(key)
                names: list[str] = []
                children: list[Path] = []
                folded: set[str] = set()
                for child in directory.iterdir():
                    names.append(child.name)
                    if child.name.casefold() in folded:
                        raise ValueError("feedback directory has case-insensitive collisions")
                    folded.add(child.name.casefold())
                    if directory == start and allowed is not None and child.name not in allowed:
                        raise ValueError("feedback source has an unregistered top-level entry")
                    if exclude_graph and directory == start and allowed == _WORK_ENTRIES and child.name in _GRAPH:
                        continue
                    self.entry_count += 1
                    if self.entry_count > self.limits.max_entries:
                        raise ValueError("feedback tree entry capacity exceeded")
                    _guard(child)
                    info = child.lstat()
                    if stat.S_ISDIR(info.st_mode):
                        children.append(child)
                        continue
                    path = child.relative_to(self.root).as_posix()
                    _reference(path)
                    if path in self.raw:
                        raise ValueError("feedback input trees overlap")
                    if self.byte_count + info.st_size > self.limits.max_total_bytes:
                        raise ValueError("feedback raw closure capacity exceeded")
                    raw, descriptor = self._read(child)
                    self.byte_count += len(raw)
                    self.raw[path] = raw
                    self.files[path] = FeedbackFile(path=path, sha256=digest(raw), bytes=len(raw))
                    self.receipts[child] = _file_identity(child.lstat()), descriptor
                self.members[directory] = tuple(sorted(names))
                pending.extend(sorted(children, reverse=True))
        # Materialize intermediate directories without admitting their siblings.
        for key in (*self.directories, *self.files):
            for parent in Path(key).parents:
                if parent.as_posix() != ".":
                    self.directories.add(parent.as_posix())
        if len(self.directories) + len(self.files) + 1 > self.limits.max_entries:
            raise ValueError("feedback complete inventory capacity exceeded")

    def finish(self) -> None:
        for path, (identity, descriptor) in self.receipts.items():
            expected = self.files[path.relative_to(self.root).as_posix()]
            _, actual_descriptor = self._read(path, expected=expected)
            if _file_identity(path.lstat()) != identity or actual_descriptor != descriptor:
                raise ValueError("feedback original changed during closure")
        for directory, members in self.members.items():
            _guard(directory)
            if tuple(sorted(child.name for child in directory.iterdir())) != members:
                raise ValueError("feedback tree membership changed during closure")
        for directory, identity in self.directory_identities.items():
            _guard(directory)
            observed = _closure_directory_anchor(directory.lstat())
            if observed != identity:
                raise ValueError(f"feedback ancestor changed during closure: {directory}; "
                                 f"expected={identity}, observed={observed}")
        # After the last byte reread, close every descriptor identity once more.
        for path, (identity, descriptor) in self.receipts.items():
            _guard(path)
            with path.open("rb") as stream:
                opened = os.fstat(stream.fileno())
                _regular(opened)
                if _open_identity(opened) != _open_identity(path.lstat()):
                    raise ValueError("feedback closure descriptor differs from its path")
                if _file_identity(opened) != descriptor:
                    raise ValueError("feedback descriptor changed after closure")
            if _file_identity(path.lstat()) != identity:
                raise ValueError("feedback path changed after closure")


def _approved_manifest(manifest: AnyEvaluationManifest, approval: HostFeedbackApproval) -> AnyEvaluationManifest:
    value = parse_evaluation_manifest(manifest.model_dump(mode="json"))
    if (sha256(canonical(value.model_dump(mode="json"))) != approval.manifest_sha256 or
            len(value.tasks) != 1 or len(value.variants) != 1 or len(value.repeat_ids) != 1 or
            len(value.runs) != 1 or value.runs[0].work_path is None or value.tasks[0].selection is None):
        raise ValueError("feedback requires an approved single-run manifest and exact case selection")
    if value.tasks[0].lineage_group_id in approval.excluded_lineage_groups:
        raise ValueError("feedback task belongs to an excluded lineage")
    return value


def _learning_ancestors(manifest: AnyEvaluationManifest, root: Path, approval: HostFeedbackApproval) -> None:
    """Inspect every frozen knowledge entry, including those never retrieved."""
    work_path = manifest.runs[0].work_path
    if work_path is None:
        raise ValueError("feedback requires an existing run")
    work = root / work_path
    session_path = work / "session.json"
    needs_knowledge = (getattr(manifest.configuration, "knowledge_profile", None) is not None or
                       any(_exists(work / name) for name in (*_KNOWLEDGE_ENTRIES, *_STATIC_SKILL_ENTRIES)))
    if not _exists(session_path):
        if needs_knowledge:
            raise ValueError("feedback knowledge ancestry has no authenticated source session")
        return
    session = _load(session_path)
    if type(session) is not dict:
        raise ValueError("feedback source session must be an object")
    if "knowledge_profile" not in session:
        if needs_knowledge:
            raise ValueError("feedback knowledge ancestry has no authenticated source profile")
        return
    profile = normalize_run_knowledge_profile(session["knowledge_profile"])
    if type(session.get("schema_version")) is not int or session["schema_version"] not in (11, 12, 13, 14):
        raise ValueError("feedback knowledge ancestry requires its original knowledge generation")
    if validate_session_provenance(session) is None:
        raise ValueError("feedback knowledge ancestry has no recorded execution provenance")
    task = manifest.tasks[0]
    cell = manifest.runs[0]
    workspace = CandidateWorkspace.reopen(root / task.task_path, work, cell.run_id)
    # Missing-report aggregation intentionally has weaker historical branches.
    # Learning must still authenticate this exact original session and all
    # profile/binding/snapshot originals rather than trusting a local scope.
    authenticated = _evaluation_session(task, cell, manifest.variants[0], manifest.configuration, workspace)
    if authenticated != session:
        raise ValueError("feedback knowledge source session changed during authentication")
    readable = read_run_knowledge(workspace, root / task.task_path, profile,
        ledger=CheckLedger(workspace, ReplayOnlyExecutor(task.protocol_sha256), read_only=True))
    if readable is None:
        raise ValueError("feedback knowledge source has no authenticated original materials")
    scope = profile["approval"]["scope"]
    if scope["split"] != "development":
        raise ValueError("feedback cannot learn from validation or confirmation runs")
    if session["schema_version"] == 14:
        from .version_skills import (
            authenticated_learning_lineages,
            read_run_version_skills,
        )

        service = read_run_version_skills(workspace, root / task.task_path, session["version_skill_profile"],
            ledger=CheckLedger(workspace, ReplayOnlyExecutor(task.protocol_sha256), read_only=True))
        if service is None:
            raise ValueError("feedback version source has no authenticated original resources")
        groups = authenticated_learning_lineages(work, session["version_skill_profile"], approval)
        if groups.intersection(approval.excluded_lineage_groups):
            raise ValueError("feedback version ancestor belongs to excluded learning lineage")
    library = KnowledgeStore(work / "knowledge")
    try:
        snapshot = library.read_snapshot(profile["approval"]["snapshot_sha256"])
        contents = SnapshotPayload.model_validate_json(snapshot.payload_json)
        for ref in contents.entries:
            entry = library.read_entry(ref.entry_id)
            payload = ExperiencePayload.model_validate_json(entry.payload_json)
            for grant in payload.source_grants:
                if (grant.origin_split not in {"curated", "development"} or "learn" not in grant.allowed_uses or
                        grant.visibility == "restricted" or
                        grant.role not in {"curated_projection", "inference_projection", "synthetic_history"}):
                    raise ValueError("feedback knowledge ancestor lacks learning permission")
                if not set(approval.allowed_splits).issubset(grant.allowed_splits):
                    raise ValueError("feedback permission exceeds an ancestor's allowed deployment splits")
            if any(item.record.lineage_group_id in approval.excluded_lineage_groups for item in payload.history):
                raise ValueError("feedback knowledge ancestor belongs to an excluded lineage")
    except KnowledgeError as exc:
        raise ValueError("feedback knowledge ancestor originals are invalid") from exc


def _aggregate(manifest: AnyEvaluationManifest, root: Path, approval: HostFeedbackApproval) -> dict[str, Any]:
    result = aggregate_evaluation(manifest, root)
    _learning_ancestors(manifest, root, approval)
    # A stopped missing report can be retained as unknown, never filled in.
    row = result["rows"][0]
    if row.get("terminal_status") not in {"finished", "failed", "canceled"}:
        raise ValueError("feedback source has no matching stopped state")
    if any(value is not None for value in result["application_metrics"].values()):
        raise ValueError("feedback aggregate cannot claim application metrics")
    return result


class FeedbackStore:
    """Explicit publications under a precreated host-managed root; reads create nothing."""

    def __init__(self, root: Path, *, limits: FeedbackLimits | None = None):
        self.root = local_evidence_path(root)
        _guard(self.root)
        if not self.root.is_dir():
            raise ValueError("feedback store root must be explicitly precreated")
        self.limits = _copy(limits if limits is not None else FeedbackLimits())

    def _folder(self, name: str, *, create: bool = False) -> Path:
        _guard(self.root)
        result = self.root / name
        if create and not _exists(result):
            result.mkdir()
        _guard(result)
        if not result.is_dir():
            raise ValueError("feedback namespace must be a directory")
        return result

    def capture(self, manifest: AnyEvaluationManifest, evidence_root: Path, *,
                approval: HostFeedbackApproval) -> FeedbackCapsule:
        approval = _copy(approval)
        manifest = _approved_manifest(manifest, approval)
        budget = _ReadBudget(self.limits.max_capture_read_bytes)
        source = local_evidence_path(evidence_root)
        work = manifest.runs[0].work_path
        if work is None:
            raise ValueError("feedback source run is missing")
        if any(_overlap((source / relative).resolve(), self.root.resolve())
               for relative in (manifest.tasks[0].task_path, work)):
            raise ValueError("feedback store and declared input trees must be separate")
        captured = _ClosedTree(source, self.limits, budget)
        captured.scan(((manifest.tasks[0].task_path, _TASK_ENTRIES), (work, _WORK_ENTRIES)), exclude_graph=True)
        aggregate = _aggregate(manifest, source, approval)
        captured.finish()
        payload = FeedbackPayload(approval=approval, manifest_json=canonical(manifest.model_dump(mode="json")),
            aggregate_json=canonical(aggregate), directories=tuple(sorted(captured.directories)),
            files=tuple(captured.files[key] for key in sorted(captured.files)))
        key = content_hash(payload)
        capsule = FeedbackCapsule(capsule_id="feedback-" + key, content_sha256=key, payload_json=encode(payload))
        raw = encode(capsule).encode("utf-8")
        if (len(raw) > self.limits.max_file_bytes or
                len(raw) + captured.byte_count > self.limits.max_total_bytes):
            raise ValueError("feedback capsule and raw originals exceed publication capacity")
        destination = self.root / "capsules" / capsule.capsule_id
        if _exists(destination):
            return self._read_capsule(capsule.capsule_id, approval, budget)
        budget.require_remaining(4 * (len(raw) + captured.byte_count))
        folder = self._folder("capsules", create=True)
        pending = folder / ("pending-" + uuid4().hex)
        pending.mkdir()
        material = pending / "payload"
        material.mkdir()
        for relative in payload.directories:
            path = material / relative
            _guard(path, missing=True)
            path.mkdir(parents=True, exist_ok=True)
        for relative, original in captured.raw.items():
            path = material / relative
            _guard(path, missing=True)
            with path.open("xb") as stream:
                stream.write(original)
                stream.flush()
                os.fsync(stream.fileno())
        with (pending / "capsule.json").open("xb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        # Independently reaggregate before publishing. Failed staging survives
        # as non-authoritative evidence, and no source file is overwritten.
        self._read_capsule_directory(pending, capsule.capsule_id, approval, budget)
        _guard(folder)
        if _exists(destination):
            raise ValueError("feedback capsule publication raced another writer")
        os.rename(pending, destination)
        return self._read_capsule(capsule.capsule_id, approval, budget)

    def _read_capsule_directory(self, directory: Path, capsule_id: str,
                                approval: HostFeedbackApproval, budget: _ReadBudget) -> FeedbackCapsule:
        _guard(directory)
        if not directory.is_dir() or {item.name for item in directory.iterdir()} != {"capsule.json", "payload"}:
            raise ValueError("feedback capsule has missing or additional top-level materials")
        metadata = _ClosedTree(directory, self.limits, budget)
        metadata._parents(directory)
        raw, descriptor = metadata._read(directory / "capsule.json")
        capsule = FeedbackCapsule.model_validate_json(encode(_object(raw.decode("utf-8"))))
        if capsule.capsule_id != capsule_id or encode(capsule).encode("utf-8") != raw:
            raise ValueError("feedback capsule record identity or canonical bytes changed")
        payload = FeedbackPayload.model_validate_json(capsule.payload_json)
        if payload.approval != approval:
            raise ValueError("feedback capsule differs from external original approval")
        manifest = _approved_manifest(parse_evaluation_manifest(_object(payload.manifest_json)), approval)
        tree = _ClosedTree(directory / "payload", self.limits, budget)
        tree.scan(((".", None),))
        if (tuple(sorted(tree.directories)) != payload.directories or
                tuple(tree.files[key] for key in sorted(tree.files)) != payload.files):
            raise ValueError("feedback capsule originals differ from their complete inventory")
        if len(raw) + tree.byte_count > self.limits.max_total_bytes:
            raise ValueError("feedback capsule exceeds read capacity")
        aggregate = _aggregate(manifest, tree.root, approval)
        if canonical(aggregate) != payload.aggregate_json:
            raise ValueError("feedback capsule aggregate differs from its original facts")
        tree.finish()
        after, after_descriptor = metadata._read(directory / "capsule.json")
        if (after != raw or after_descriptor != descriptor or
                {item.name for item in directory.iterdir()} != {"capsule.json", "payload"}):
            raise ValueError("feedback capsule changed during readback")
        if any(_closure_directory_anchor(parent.lstat()) != identity
               for parent, identity in metadata.directory_identities.items()):
            raise ValueError("feedback capsule ancestor changed during readback")
        return capsule

    def read_capsule(self, capsule_id: str, *, approval: HostFeedbackApproval) -> FeedbackCapsule:
        _identity(capsule_id, "feedback-")
        approval = _copy(approval)
        return self._read_capsule(capsule_id, approval, _ReadBudget(self.limits.max_capture_read_bytes))

    def _read_capsule(self, capsule_id: str, approval: HostFeedbackApproval,
                      budget: _ReadBudget) -> FeedbackCapsule:
        directory = self._folder("capsules") / capsule_id
        return self._read_capsule_directory(directory, capsule_id, approval, budget)

    def propose_candidate(self, capsule_id: str, projection: SolverProjection, *,
                          approval: HostCandidateApproval,
                          feedback_approval: HostFeedbackApproval) -> ExperienceCandidate:
        approval, feedback_approval, projection = _copy(approval), _copy(feedback_approval), _copy(projection)
        _identity(capsule_id, "feedback-")
        budget = _ReadBudget(self.limits.max_capture_read_bytes)
        capsule = self._read_capsule(capsule_id, feedback_approval, budget)
        if capsule.capsule_id != approval.capsule_id:
            raise ValueError("candidate approval names another feedback capsule")
        payload = CandidatePayload(capsule_id=capsule_id, approval=approval,
            feedback_approval_sha256=content_hash(feedback_approval), projection=projection,
            allowed_splits=feedback_approval.allowed_splits,
            excluded_lineage_groups=feedback_approval.excluded_lineage_groups)
        key = content_hash(payload)
        candidate = ExperienceCandidate(candidate_id="experience-candidate-" + key,
            content_sha256=key, payload_json=encode(payload))
        raw = encode(candidate).encode("utf-8")
        if len(raw) > self.limits.max_file_bytes or len(raw) > self.limits.max_total_bytes:
            raise ValueError("feedback candidate exceeds publication capacity")
        capsule_files = FeedbackPayload.model_validate_json(capsule.payload_json).files
        capsule_size = len(encode(capsule).encode("utf-8")) + sum(item.bytes for item in capsule_files)
        budget.require_remaining(2 * (len(raw) + capsule_size))
        folder = self._folder("candidates", create=True)
        path = folder / (candidate.candidate_id + ".json")
        if _exists(path):
            existing = self._read_candidate(candidate.candidate_id, approval, feedback_approval, budget)
            if existing != candidate:
                raise ValueError("existing feedback candidate differs")
            return existing
        pending = folder / ("pending-" + uuid4().hex + ".json")
        with pending.open("xb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        _guard(folder)
        if _exists(path):
            raise ValueError("feedback candidate publication raced another writer")
        os.rename(pending, path)
        return self._read_candidate(candidate.candidate_id, approval, feedback_approval, budget)

    def read_candidate(self, candidate_id: str, *, approval: HostCandidateApproval,
                       feedback_approval: HostFeedbackApproval) -> ExperienceCandidate:
        _identity(candidate_id, "experience-candidate-")
        approval, feedback_approval = _copy(approval), _copy(feedback_approval)
        return self._read_candidate(candidate_id, approval, feedback_approval,
                                    _ReadBudget(self.limits.max_capture_read_bytes))

    def _read_candidate(self, candidate_id: str, approval: HostCandidateApproval,
                        feedback_approval: HostFeedbackApproval, budget: _ReadBudget) -> ExperienceCandidate:
        folder = self._folder("candidates")
        path = folder / (candidate_id + ".json")
        reader = _ClosedTree(folder, self.limits, budget)
        reader._parents(folder)
        raw, descriptor = reader._read(path)
        candidate = ExperienceCandidate.model_validate_json(encode(_object(raw.decode("utf-8"))))
        if candidate.candidate_id != candidate_id or encode(candidate).encode("utf-8") != raw:
            raise ValueError("feedback candidate record identity or canonical bytes changed")
        payload = CandidatePayload.model_validate_json(candidate.payload_json)
        if (payload.approval != approval or payload.feedback_approval_sha256 != content_hash(feedback_approval) or
                payload.allowed_splits != feedback_approval.allowed_splits or
                payload.excluded_lineage_groups != feedback_approval.excluded_lineage_groups):
            raise ValueError("feedback candidate differs from external original approvals")
        self._read_capsule(payload.capsule_id, feedback_approval, budget)
        after, after_descriptor = reader._read(path)
        if after != raw or after_descriptor != descriptor:
            raise ValueError("feedback candidate changed during readback")
        if any(_closure_directory_anchor(parent.lstat()) != identity
               for parent, identity in reader.directory_identities.items()):
            raise ValueError("feedback candidate ancestor changed during readback")
        return candidate
