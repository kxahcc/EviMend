"""Immutable portable learning originals. This module has no dispatch API.

An original physical-root approval authorizes capture, never a new execution
at the copy. Every read reconstructs inputs, requests, ledger and draft costs.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import cast
from uuid import uuid4

from .evidence_io import local_evidence_path
from .feedback import FeedbackStore, _ClosedTree, _copy, _exists, _ReadBudget
from .feedback_contracts import (
    FeedbackFile,
    FeedbackLimits,
    FeedbackPayload,
    _identity,
    encode,
)
from .knowledge_contracts import (
    ExperienceEntry,
    ExperiencePayload,
    KnowledgeSnapshot,
    SnapshotPayload,
)
from .learning import LearningStore, _cost, _mock_adapter, _strict_json
from .learning_contracts import (
    HostLearningApproval,
    LearningDraft,
    LearningInputText,
    LearningOwner,
    LearningSource,
    LearningSourcePayload,
)
from .metrics import _guard, _overlap, parse_evaluation_manifest


class _OperationView:
    """Only the original audit implementation, without mutation methods."""

    _request = staticmethod(LearningStore._request)
    _validate_draft = staticmethod(LearningStore._validate_draft)

    def __init__(self, root: Path, limits: FeedbackLimits):
        self.root, self.limits = root, limits


def _owner(feedback: FeedbackStore, expected: LearningOwner, budget: _ReadBudget) -> LearningOwner:
    approval = expected.approval
    capsule = feedback._read_capsule(approval.capsule_id, approval.feedback_approval, budget)
    payload = FeedbackPayload.model_validate_json(capsule.payload_json)
    manifest = parse_evaluation_manifest(_strict_json(payload.manifest_json))
    work = manifest.runs[0].work_path
    if work is None:
        raise ValueError("portable learning source has no original run")
    reader = _ClosedTree(feedback.root / "capsules" / capsule.capsule_id / "payload", feedback.limits, budget)
    reader.scan(((".", None),))
    if tuple(reader.files[key] for key in sorted(reader.files)) != payload.files:
        raise ValueError("portable learning feedback inventory differs")
    lineage = {manifest.tasks[0].lineage_group_id}
    session = _strict_json(reader.raw[work + "/session.json"].decode("utf-8"))
    profile = session.get("knowledge_profile")
    if profile is not None:
        snapshot_id = profile["approval"]["snapshot_sha256"]
        snapshot = KnowledgeSnapshot.model_validate_json(reader.raw[work + "/knowledge/snapshots/" + snapshot_id + ".json"])
        for reference in SnapshotPayload.model_validate_json(snapshot.payload_json).entries:
            entry = ExperienceEntry.model_validate_json(reader.raw[work + "/knowledge/entries/" + reference.entry_id + ".json"])
            lineage.update(item.record.lineage_group_id for item in ExperiencePayload.model_validate_json(entry.payload_json).history)
    crash = manifest.tasks[0].task_path + "/crash.log"
    raw = reader.raw[crash]
    texts = [LearningInputText(path=crash, sha256=manifest.tasks[0].crash_sha256,
                             bytes=len(raw), text=raw.decode("utf-8"), role="original_crash")]
    for artifact in _strict_json(payload.aggregate_json)["rows"][0]["candidate_artifacts"]:
        path = work + "/" + artifact["patch_path"]
        raw = reader.raw[path]
        texts.append(LearningInputText(path=path, sha256=artifact["patch_sha256"], bytes=len(raw),
            text=raw.decode("utf-8"), role="registered_candidate_patch", candidate_id=artifact["candidate"]["candidate_id"]))
    if lineage.intersection(approval.feedback_approval.excluded_lineage_groups):
        raise ValueError("portable learning source ancestry is excluded")
    rebuilt = LearningOwner(approval=approval, source=capsule, source_binding_sha256=expected.source_binding_sha256,
        provider_profile_json=expected.provider_profile_json, learning_lineage_groups=tuple(sorted(lineage)), input_texts=tuple(texts))
    if _mock_adapter(rebuilt).profile() != _strict_json(rebuilt.provider_profile_json):
        raise ValueError("portable learning provider differs from original approval")
    reader.finish()
    if feedback._read_capsule(approval.capsule_id, approval.feedback_approval, budget) != capsule:
        raise ValueError("portable learning feedback changed during owner closure")
    if rebuilt != expected:
        raise ValueError("portable learning owner differs from original inputs")
    return rebuilt


class LearningSourceStore:
    """An existing destination root containing source-learning packages."""

    def __init__(self, root: Path, *, limits: FeedbackLimits | None = None):
        self.root = local_evidence_path(root)
        _guard(self.root)
        if not self.root.is_dir():
            raise ValueError("learning source destination must be precreated")
        self.limits = _copy(limits if limits is not None else FeedbackLimits())

    def capture(self, learning: LearningStore, draft_id: str, *, approval: HostLearningApproval) -> LearningSource:
        return self._capture(learning, draft_id, _copy(approval), _ReadBudget(self.limits.max_capture_read_bytes))

    def preview(self, learning: LearningStore, draft_id: str, *, approval: HostLearningApproval) -> LearningSource:
        return self._capture(learning, draft_id, _copy(approval), _ReadBudget(self.limits.max_capture_read_bytes), publish=False)

    def _capture(self, learning: LearningStore, draft_id: str, approval: HostLearningApproval,
                 budget: _ReadBudget, *, publish: bool = True) -> LearningSource:
        if type(learning) is not LearningStore:
            raise ValueError("learning capture requires the original execution store")
        if _overlap(self.root.resolve(), learning.root.resolve()) or _overlap(self.root.resolve(), learning.feedback.root.resolve()):
            raise ValueError("portable learning destination overlaps its original sources")
        draft = learning._read_draft(draft_id, approval, budget)
        owner = learning._source(approval, budget)
        requests, records, responses = learning._audit(owner, budget)
        if any(record.state in {"reserved", "dispatched"} for record in records):
            raise ValueError("portable learning requires a stable complete operation")
        operation = _ClosedTree(learning.root, self.limits, budget)
        operation.scan(((".", None),))
        feedback = _ClosedTree(learning.feedback.root, self.limits, budget)
        feedback.scan((("capsules/" + owner.source.capsule_id, None),))
        files = tuple(sorted((FeedbackFile(path=prefix + "/" + file.path, sha256=file.sha256, bytes=file.bytes)
            for prefix, tree in (("operation", operation), ("feedback", feedback)) for file in tree.files.values()), key=lambda file: file.path))
        directories = tuple(sorted({"operation", "feedback", *("operation/" + path for path in operation.directories),
                                    *("feedback/" + path for path in feedback.directories)}))
        payload = LearningSourcePayload(approval=approval, owner=owner, draft=draft, operation_cost=_cost(records),
            operation_calls_json=LearningStore._calls(requests, records, responses), directories=directories, files=files)
        from .feedback_contracts import content_hash
        key = content_hash(payload)
        source = LearningSource(source_id="learning-source-" + key, content_sha256=key, payload_json=encode(payload))
        metadata = encode(source).encode("utf-8")
        total = sum(file.bytes for file in files) + len(metadata)
        if (len(metadata) > self.limits.max_file_bytes or total > self.limits.max_total_bytes or
                len(files) + len(directories) + 2 > self.limits.max_entries):
            raise ValueError("portable learning publication exceeds bounded closure")
        operation.finish()
        feedback.finish()
        if learning._source(approval, budget) != owner or learning._audit(owner, budget)[1] != records:
            raise ValueError("learning original changed before portable publication")
        if not publish:
            return source
        destination = self.root / "source-learning" / source.source_id
        # Reserve staged and final complete audits before any destination write.
        audit_cost = budget.used
        budget.require_remaining(2 * audit_cost + 4 * total)
        if _exists(destination):
            if self._read_source(source.source_id, approval, budget) != source:
                raise ValueError("existing portable learning source differs")
            return source
        namespace = destination.parent
        _guard(namespace, missing=True)
        namespace.mkdir(exist_ok=True)
        pending = namespace / ("pending-" + uuid4().hex)
        _guard(pending, missing=True)
        pending.mkdir()
        for path in directories:
            (pending / "payload" / path).mkdir(parents=True, exist_ok=True)
        for prefix, tree in (("operation", operation), ("feedback", feedback)):
            for relative, raw in tree.raw.items():
                self._write(pending / "payload" / prefix / relative, raw)
        self._write(pending / "source.json", metadata)
        if self._read_directory(pending, source.source_id, approval, budget) != source:
            raise ValueError("staged portable learning closure differs")
        operation.finish()
        feedback.finish()
        _guard(namespace)
        if _exists(destination):
            raise ValueError("portable learning publication raced another writer")
        os.rename(pending, destination)
        return self._read_source(source.source_id, approval, budget)

    @staticmethod
    def _write(path: Path, raw: bytes) -> None:
        _guard(path, missing=True)
        with path.open("xb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())

    def read_source(self, source_id: str, *, approval: HostLearningApproval) -> LearningSource:
        return self._read_source(source_id, _copy(approval), _ReadBudget(self.limits.max_capture_read_bytes))

    def copy_source(self, original: LearningSourceStore, source_id: str, *,
                    approval: HostLearningApproval) -> LearningSource:
        return self._copy_source(original, source_id, _copy(approval), _ReadBudget(self.limits.max_capture_read_bytes))

    def _copy_source(self, original: LearningSourceStore, source_id: str, approval: HostLearningApproval,
                     budget: _ReadBudget) -> LearningSource:
        initial = budget.used
        source = original._read_source(source_id, approval, budget)
        reads = budget.used - initial
        if self.root.resolve() == original.root.resolve():
            return source
        origin = original.root / "source-learning" / source_id
        destination = self.root / "source-learning" / source_id
        if _overlap(origin.resolve(), destination.resolve()):
            raise ValueError("portable learning copies cannot overlap")
        tree = _ClosedTree(origin, self.limits, budget)
        tree.scan(((".", None),))
        tree.finish()
        budget.require_remaining(2 * reads + 2 * tree.byte_count)
        if _exists(destination):
            if self._read_source(source_id, approval, budget) != source:
                raise ValueError("existing learning source copy differs")
            return source
        namespace = destination.parent
        _guard(namespace, missing=True)
        namespace.mkdir(exist_ok=True)
        pending = namespace / ("pending-" + uuid4().hex)
        pending.mkdir()
        for path in sorted(tree.directories):
            (pending / path).mkdir(parents=True, exist_ok=True)
        for relative, raw in tree.raw.items():
            self._write(pending / relative, raw)
        if self._read_directory(pending, source_id, approval, budget) != source:
            raise ValueError("portable learning staged copy differs")
        tree.finish()
        _guard(namespace)
        if _exists(destination):
            raise ValueError("portable learning copy raced another writer")
        os.rename(pending, destination)
        return self._read_source(source_id, approval, budget)

    def _read_source(self, source_id: str, approval: HostLearningApproval, budget: _ReadBudget) -> LearningSource:
        _identity(source_id, "learning-source-")
        return self._read_directory(self.root / "source-learning" / source_id, source_id, approval, budget)

    def _read_directory(self, directory: Path, source_id: str, approval: HostLearningApproval,
                        budget: _ReadBudget) -> LearningSource:
        tree = _ClosedTree(directory, self.limits, budget)
        tree.scan(((".", frozenset({"source.json", "payload"})),))
        raw = tree.raw["source.json"]
        source = LearningSource.model_validate_json(raw)
        if source.source_id != source_id or encode(source).encode("utf-8") != raw:
            raise ValueError("portable learning canonical identity differs")
        payload = LearningSourcePayload.model_validate_json(source.payload_json)
        if payload.approval != approval:
            raise ValueError("portable learning differs from external original approval")
        inventory = tuple(FeedbackFile(path=file.path.removeprefix("payload/"), sha256=file.sha256, bytes=file.bytes)
            for name, file in sorted(tree.files.items()) if name != "source.json")
        directories = tuple(sorted(path.removeprefix("payload/") for path in tree.directories if path != "payload"))
        if inventory != payload.files or directories != payload.directories:
            raise ValueError("portable learning complete inventory differs")
        feedback = FeedbackStore(directory / "payload" / "feedback", limits=self.limits)
        owner = _owner(feedback, payload.owner, budget)
        view = _OperationView(directory / "payload" / "operation", self.limits)
        requests, records, responses = LearningStore._audit(cast(LearningStore, view), owner, budget)
        if any(record.state in {"reserved", "dispatched"} for record in records):
            raise ValueError("portable learning operation is not stable")
        LearningStore._validate_draft(payload.draft, owner, requests, records, responses)
        if (_cost(records) != payload.operation_cost or
                LearningStore._calls(requests, records, responses) != payload.operation_calls_json):
            raise ValueError("portable learning full operation cost differs")
        tree.finish()
        return source

    def read_draft(self, source_id: str, *, approval: HostLearningApproval) -> LearningDraft:
        return LearningSourcePayload.model_validate_json(self.read_source(source_id, approval=approval).payload_json).draft
