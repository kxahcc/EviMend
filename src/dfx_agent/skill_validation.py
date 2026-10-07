"""Synthetic Skill source closure, prospective validation and saved-run audits.

Two roles share an unchanged knowledge/resource pool and differ in the intended
procedure read. This is not an evaluation of different knowledge snapshots.
No Agent, model, check, Graph or application is executed by these interfaces.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast
from uuid import uuid4

from .evidence_io import local_evidence_path
from .exporting import _TASK_ENTRIES, _WORK_ENTRIES
from .feedback import (
    FeedbackStore,
    _ClosedTree,
    _closure_directory_anchor,
    _copy,
    _exists,
    _ReadBudget,
)
from .feedback_contracts import (
    FeedbackFile,
    FeedbackLimits,
    HostCandidateApproval,
    HostFeedbackApproval,
    _identity,
    _object,
    content_hash,
    encode,
)
from .knowledge import KnowledgeError, KnowledgeStore
from .knowledge_contracts import ExperiencePayload, SnapshotPayload, SolverProjection
from .learning import LearningStore, _cost
from .learning_contracts import (
    HostLearningApproval,
    LearningDraftPayload,
    LearningSourcePayload,
)
from .learning_source import LearningSourceStore
from .metrics import (
    _guard,
    _load,
    _overlap,
    aggregate_evaluation,
    parse_evaluation_manifest,
)
from .model_calls import ModelCallConflict, ModelCallRecord, canonical
from .skill_evolution_contracts import (
    HostModelSkillVersionApproval,
    HostSkillVersionApproval,
    ModelVersionPayload,
    PlanPayload,
    PlanPayloadV2,
    PlanSpec,
    PlanSpecV2,
    ReceiptPayload,
    ReceiptPayloadV2,
    SkillValidationPlan,
    SkillValidationPlanV2,
    SkillValidationReceipt,
    SkillValidationReceiptV2,
    SkillVersion,
    SkillVersionV2,
    ValidationApproval,
    ValidationPlanRecord,
    ValidationReceiptRecord,
    VersionApproval,
    VersionPayload,
    VersionRecord,
    parse_plan,
    parse_plan_payload,
    parse_receipt,
    parse_receipt_payload,
    parse_version,
    parse_version_payload,
)
from .task import digest, verify_task
from .working_context_materials import _directory_anchor
from .working_context_materials import _identity as file_identity

MAX_VERSION_DEPTH = 64


def _raw(reader: _ClosedTree, path: Path) -> bytes:
    reader._parents(path.parent)
    original, descriptor = reader._read(path)
    repeated, closing = reader._read(path)
    if (original != repeated or descriptor != closing or
            any(_closure_directory_anchor(parent.lstat()) != identity
                for parent, identity in reader.directory_identities.items())):
        raise ValueError("Skill immutable record changed during read")
    return original


def _write_file(path: Path, raw: bytes) -> None:
    _guard(path, missing=True)
    if _exists(path):
        raise ValueError("Skill publication target already exists")
    with path.open("xb") as output:
        output.write(raw)
        output.flush()
        os.fsync(output.fileno())


def _publish_tree(directory: Path, tree: _ClosedTree, metadata_name: str | None = None,
                  metadata: bytes | None = None) -> None:
    _guard(directory, missing=True)
    directory.mkdir()
    payload = directory if metadata_name is None else directory / "payload"
    if metadata_name is not None:
        payload.mkdir()
    for relative in sorted(tree.directories):
        path = payload / relative
        _guard(path, missing=True)
        path.mkdir(parents=True, exist_ok=True)
    for relative, raw in tree.raw.items():
        _write_file(payload / relative, raw)
    if metadata_name is not None and metadata is not None:
        _write_file(directory / metadata_name, metadata)


def _single(value: str, *, schema: int = 5) -> Any:
    manifest = parse_evaluation_manifest(_object(value))
    if manifest.schema_version != schema:
        raise ValueError("Skill validation requires its exact manifest generation")
    return manifest


def _learning_costs_from_bundle(files: dict[str, bytes], version_id: str,
                                approval: VersionApproval) -> dict[str, Any]:
    """Derive costs only from an already closed, semantically authenticated bundle.

    This pure helper grants no source authority. The callers export the complete
    immutable closure before using it; parsing below also fixes every linked
    version/approval/source ID instead of trusting a stored cost summary.
    """
    operations: dict[tuple[str, str], dict[str, Any]] = {}
    current, external = version_id, approval
    seen: set[str] = set()
    while True:
        if current in seen or len(seen) >= MAX_VERSION_DEPTH:
            raise ValueError("learning cost ancestry is cyclic or exceeds capacity")
        seen.add(current)
        record = parse_version(files["versions/" + current + ".json"].decode("utf-8"))
        body = parse_version_payload(record.payload_json)
        if record.version_id != current or body.approval != external:
            raise ValueError("learning cost bundle differs from linked original approval")
        if isinstance(body, ModelVersionPayload):
            from .learning_contracts import LearningSource

            source = LearningSource.model_validate_json(files["source-learning/" + body.learning_source_id + "/source.json"])
            payload = LearningSourcePayload.model_validate_json(source.payload_json)
            if source.source_id != body.learning_source_id or payload.approval != body.learning_approval:
                raise ValueError("learning cost bundle has another original source")
            key = (payload.approval.operation_id, payload.approval.run_id)
            owner_key = content_hash(payload.owner)
            item = operations.setdefault(key, {"operation_id": key[0], "run_id": key[1], "owner_sha256": owner_key,
                "source_binding_sha256": payload.owner.source_binding_sha256, "source_ids": [], "records": {}})
            if item["owner_sha256"] != owner_key or item["source_binding_sha256"] != payload.owner.source_binding_sha256:
                raise ValueError("learning operation ancestry changed its original owner")
            item["source_ids"].append(source.source_id)
            for call in _object(payload.operation_calls_json)["calls"]:
                entry = ModelCallRecord.model_validate_json(encode(call["record"]))
                if item["records"].setdefault(entry.intent.call_id, entry) != entry:
                    raise ValueError("learning ancestor call changed between source packages")
        if body.approval.parent_version_id is None or body.parent_approval is None:
            break
        current, external = body.approval.parent_version_id, body.parent_approval
    rows, all_records = [], []
    for key in sorted(operations):
        item = operations[key]
        records = list(item.pop("records").values())
        all_records.extend(records)
        item["source_ids"] = sorted(set(item["source_ids"]))
        item["cost"] = _cost(records).model_dump(mode="json")
        rows.append(item)
    return {"status": "not_run" if not rows else "observed", "operations": rows,
            "cost": None if not rows else _cost(all_records).model_dump(mode="json"),
            "monetary_cost": None, "scope": "offline_synthetic"}


def _timestamp(value: str) -> datetime:
    if type(value) is not str:
        raise ValueError("Skill validation timestamp is not text")
    actual = datetime.fromisoformat(value)
    if actual.tzinfo is None:
        raise ValueError("Skill validation timestamp has no timezone")
    return actual


def _aggregate(manifest: Any, root: Path) -> dict[str, Any]:
    try:
        return aggregate_evaluation(manifest, root)
    except (KnowledgeError, ModelCallConflict) as exc:
        raise ValueError("Skill validation original evidence is invalid") from exc


def _historical_lineages(root: Path, capsule) -> set[str]:
    payload = capsule.contents()
    manifest = parse_evaluation_manifest(_object(payload["manifest_json"]))
    groups = {task.lineage_group_id for task in manifest.tasks}
    work_path = manifest.runs[0].work_path
    if work_path is None:
        raise ValueError("Skill revision feedback has no run")
    work = root / "capsules" / capsule.capsule_id / "payload" / work_path
    if not _exists(work / "knowledge-profile.json"):
        return groups
    profile = _load(work / "knowledge-profile.json")
    store = KnowledgeStore(work / "knowledge")
    snapshot = store.read_snapshot(profile["approval"]["snapshot_sha256"])
    for ref in SnapshotPayload.model_validate_json(snapshot.payload_json).entries:
        history = ExperiencePayload.model_validate_json(store.read_entry(ref.entry_id).payload_json).history
        groups.update(item.record.lineage_group_id for item in history)
    return groups


def _cross_role_retrieved(knowledge: dict[str, Any], entry_id: str) -> bool:
    queries = {item["bound_query"]["record_id"]: item for item in knowledge.get("queries", [])}
    if any(query.get("actions") and any(hit["entry_id"] == entry_id
            for hit in query.get("retrieval", {}).get("hits", [])) for query in queries.values()):
        return True
    for read in knowledge.get("reads", []):
        if read["entry_id"] == entry_id:
            return True
        if read["entry_id"] is None:
            query = queries.get(read["query_id"])
            if query is None:
                raise ValueError("Skill procedure read has no authenticated complete query")
            if any(hit["entry_id"] == entry_id for hit in query.get("retrieval", {}).get("hits", [])):
                return True
    return False


class SkillValidationStore:
    """Explicit immutable publications; constructors and every read are pure."""

    def __init__(self, root: Path, *, limits: FeedbackLimits | None = None):
        self.root = local_evidence_path(root)
        _guard(self.root)
        if not self.root.is_dir():
            raise ValueError("Skill store root must be explicitly precreated")
        self.limits = _copy(limits if limits is not None else FeedbackLimits())

    def _folder(self, name: str, *, create: bool = False) -> Path:
        _guard(self.root)
        path = self.root / name
        if create and not _exists(path):
            path.mkdir()
        _guard(path)
        if not path.is_dir():
            raise ValueError("Skill namespace must be a directory")
        return path

    def _record(self, folder: str, identifier: str, model, budget: _ReadBudget):
        path = self._folder(folder) / (identifier + ".json")
        reader = _ClosedTree(path.parent, self.limits, budget)
        raw = _raw(reader, path)
        parser = model.model_validate_json if hasattr(model, "model_validate_json") else model
        record = parser(encode(_object(raw.decode("utf-8"))))
        if encode(record).encode("utf-8") != raw:
            raise ValueError("Skill saved record has noncanonical bytes")
        return record

    def _publish_record(self, folder: str, identifier: str, record, budget: _ReadBudget,
                        readback_bytes: int) -> None:
        raw = encode(record).encode("utf-8")
        if len(raw) > self.limits.max_file_bytes or len(raw) > self.limits.max_total_bytes:
            raise ValueError("Skill publication exceeds record capacity")
        destination = self.root / folder / (identifier + ".json")
        budget.require_remaining(readback_bytes + (2 * len(raw) if _exists(destination) else 0))
        directory = self._folder(folder, create=True)
        destination = directory / (identifier + ".json")
        if _exists(destination):
            original = _raw(_ClosedTree(directory, self.limits, budget), destination)
            if original != raw:
                raise ValueError("existing Skill record differs")
            return
        pending = directory / ("pending-" + uuid4().hex + ".json")
        _write_file(pending, raw)
        if _exists(destination):
            raise ValueError("Skill record publication raced another writer")
        budget.require_remaining(readback_bytes)
        os.rename(pending, destination)

    def create_seed(self, projection: SolverProjection, *, approval: HostSkillVersionApproval) -> SkillVersion:
        projection, approval = _copy(projection), _copy(approval)
        if approval.parent_version_id is not None:
            raise ValueError("seed Skill approval cannot name a revision")
        payload = VersionPayload(approval=approval, projection=projection,
                                 learning_lineage_groups=tuple(sorted(approval.learning_lineage_groups)))
        key = content_hash(payload)
        version = SkillVersion(version_id="skill-version-" + key, content_sha256=key, payload_json=encode(payload))
        budget = _ReadBudget(self.limits.max_capture_read_bytes)
        self._publish_record("versions", version.version_id, version, budget, 2 * len(encode(version).encode("utf-8")))
        return cast(SkillVersion, self._read_version(version.version_id, approval, budget, set()))

    def create_revision(self, parent_version_id: str, feedback_store: FeedbackStore, candidate_id: str, *,
                        approval: HostSkillVersionApproval, parent_approval: HostSkillVersionApproval,
                        candidate_approval: HostCandidateApproval, feedback_approval: HostFeedbackApproval) -> SkillVersion:
        approval, parent_approval = _copy(approval), _copy(parent_approval)
        candidate_approval, feedback_approval = _copy(candidate_approval), _copy(feedback_approval)
        feedback_store = FeedbackStore(feedback_store.root, limits=self.limits)
        if approval.parent_version_id != parent_version_id or approval.source_candidate_id != candidate_id:
            raise ValueError("Skill revision differs from external source approval")
        budget = _ReadBudget(self.limits.max_capture_read_bytes)
        initial = budget.used
        parent = self._read_version(parent_version_id, parent_approval, budget, set())
        parent_reads = budget.used - initial
        previous = VersionPayload.model_validate_json(parent.payload_json)
        initial = budget.used
        candidate = feedback_store._read_candidate(candidate_id, candidate_approval, feedback_approval, budget)
        candidate_reads = budget.used - initial
        content = candidate.contents()
        projection = SolverProjection.model_validate_json(canonical(content["projection"]))
        if (approval.skill_id != parent_approval.skill_id or
                approval.projection_sha256 == parent_approval.projection_sha256 or
                not set(approval.allowed_splits).issubset(parent_approval.allowed_splits) or
                not set(approval.allowed_splits).issubset(feedback_approval.allowed_splits) or
                not set(parent_approval.excluded_lineage_groups).issubset(approval.excluded_lineage_groups) or
                not set(feedback_approval.excluded_lineage_groups).issubset(approval.excluded_lineage_groups)):
            raise ValueError("Skill revision widens authority, changes identity or repeats its parent")
        initial = budget.used
        capsule = feedback_store._read_capsule(content["capsule_id"], feedback_approval, budget)
        capsule_reads = budget.used - initial
        groups = set(previous.learning_lineage_groups) | set(approval.learning_lineage_groups)
        groups |= _historical_lineages(feedback_store.root, capsule)
        payload = VersionPayload(approval=approval, projection=projection, parent_approval=parent_approval,
            candidate_approval=candidate_approval, feedback_approval=feedback_approval,
            learning_lineage_groups=tuple(sorted(groups)))
        key = content_hash(payload)
        version = SkillVersion(version_id="skill-version-" + key, content_sha256=key, payload_json=encode(payload))
        version_reads = 2 * len(encode(version).encode("utf-8")) + parent_reads + candidate_reads + capsule_reads
        sources = self.root / "source-feedback" / candidate_id
        if _overlap(feedback_store.root.resolve(), sources.resolve()):
            raise ValueError("Skill source copy must be separate from approved original feedback")
        if not _exists(sources):
            tree = _ClosedTree(feedback_store.root, self.limits, budget)
            tree.scan((("capsules/" + capsule.capsule_id, None),))
            path = feedback_store.root / "candidates" / (candidate_id + ".json")
            raw, descriptor = tree._read(path)
            relative = path.relative_to(feedback_store.root).as_posix()
            tree.raw[relative] = raw
            tree.files[relative] = FeedbackFile(path=relative, sha256=digest(raw), bytes=len(raw))
            # Use the exact same path/descriptor tuple as _ClosedTree capture.
            tree.receipts[path] = file_identity(path.lstat()), descriptor
            tree._parents(path.parent)
            tree.directories.add("candidates")
            tree.byte_count += len(raw)
            if tree.byte_count > self.limits.max_total_bytes:
                raise ValueError("Skill source feedback closure exceeds capacity")
            tree.finish()
            budget.require_remaining(candidate_reads + version_reads)
            source_originals = self._folder("source-feedback", create=True)
            pending = source_originals / ("pending-" + uuid4().hex)
            _publish_tree(pending, tree)
            internal = FeedbackStore(pending, limits=self.limits)
            if internal._read_candidate(candidate_id, candidate_approval, feedback_approval, budget) != candidate:
                raise ValueError("Skill copied feedback differs from approved original")
            budget.require_remaining(version_reads)
            os.rename(pending, sources)
        elif FeedbackStore(sources, limits=self.limits)._read_candidate(
                candidate_id, candidate_approval, feedback_approval, budget) != candidate:
            raise ValueError("existing Skill source feedback differs from approved original")
        self._publish_record("versions", version.version_id, version, budget, version_reads)
        return cast(SkillVersion, self._read_version(version.version_id, approval, budget, set()))

    def create_model_revision(self, parent_version_id: str, learning: LearningStore | LearningSourceStore,
                              draft_id: str, *, approval: HostModelSkillVersionApproval,
                              parent_approval: VersionApproval, learning_approval: HostLearningApproval) -> SkillVersionV2:
        approval, parent_approval, learning_approval = _copy(approval), _copy(parent_approval), _copy(learning_approval)
        if type(approval) is not HostModelSkillVersionApproval or approval.parent_version_id != parent_version_id or approval.source_draft_id != draft_id:
            raise ValueError("model revision requires its exact reviewed parent and draft")
        budget = _ReadBudget(self.limits.max_capture_read_bytes)
        parent = self._read_version(parent_version_id, parent_approval, budget, set())
        previous = parse_version_payload(parent.payload_json)
        sources = LearningSourceStore(self.root, limits=self.limits)
        if type(learning) is LearningStore:
            source = sources._capture(learning, draft_id, learning_approval, budget, publish=False)
        elif type(learning) is LearningSourceStore:
            source = learning._read_source(approval.learning_source_id, learning_approval, budget)
        else:
            raise ValueError("model revision needs authenticated original or portable learning")
        payload = LearningSourcePayload.model_validate_json(source.payload_json)
        draft = LearningDraftPayload.model_validate_json(payload.draft.payload_json)
        if (source.source_id != approval.learning_source_id or payload.draft.draft_id != draft_id or
                approval.skill_id != previous.approval.skill_id or approval.projection_sha256 == previous.approval.projection_sha256 or
                not set(approval.allowed_splits).issubset(previous.approval.allowed_splits) or
                not set(approval.allowed_splits).issubset(draft.allowed_splits) or
                not set(previous.approval.excluded_lineage_groups).issubset(approval.excluded_lineage_groups) or
                not set(draft.excluded_lineage_groups).issubset(approval.excluded_lineage_groups)):
            raise ValueError("model revision expands authority or changes its reviewed source")
        groups = set(previous.learning_lineage_groups) | set(draft.learning_lineage_groups) | set(approval.learning_lineage_groups)
        body = ModelVersionPayload(approval=approval, projection=draft.projection, parent_approval=parent_approval,
            learning_approval=learning_approval, learning_source_id=source.source_id, draft_cost=draft.cost,
            operation_cost=payload.operation_cost, learning_lineage_groups=tuple(sorted(groups)))
        if type(learning) is LearningStore:
            copied = sources._capture(learning, draft_id, learning_approval, budget)
        else:
            copied = sources._copy_source(cast(LearningSourceStore, learning), approval.learning_source_id, learning_approval, budget)
        if copied != source:
            raise ValueError("model source changed after external review authentication")
        key = content_hash(body)
        version = SkillVersionV2(version_id="skill-version-" + key, content_sha256=key, payload_json=encode(body))
        initial = budget.used
        # Authenticate the complete source and parent again, then reserve that
        # exact closure plus record reads before creating the version namespace.
        if self._read_version(parent_version_id, parent_approval, budget, set()) != parent or sources._read_source(source.source_id, learning_approval, budget) != source:
            raise ValueError("model revision source changed before publication")
        closure_reads = budget.used - initial
        self._publish_record("versions", version.version_id, version, budget,
                             closure_reads + 2 * len(encode(version).encode("utf-8")))
        result = self._read_version(version.version_id, approval, budget, set())
        if not isinstance(result, SkillVersionV2):
            raise TypeError("model revision unexpectedly changed schema")
        return result

    def read_version(self, version_id: str, *, approval: VersionApproval) -> VersionRecord:
        return self._read_version(version_id, _copy(approval), _ReadBudget(self.limits.max_capture_read_bytes), set())

    def _read_version(self, version_id: str, approval: VersionApproval,
                      budget: _ReadBudget, seen: set[str]) -> VersionRecord:
        _identity(version_id, "skill-version-")
        if version_id in seen or len(seen) >= MAX_VERSION_DEPTH:
            raise ValueError("Skill version parent cycle or depth limit")
        seen.add(version_id)
        version = self._record("versions", version_id, parse_version, budget)
        if version.version_id != version_id:
            raise ValueError("Skill version filename changed")
        payload = parse_version_payload(version.payload_json)
        if payload.approval != approval:
            raise ValueError("Skill version differs from external original approval")
        groups = set(approval.learning_lineage_groups)
        if isinstance(payload, ModelVersionPayload):
            parent = self._read_version(payload.approval.parent_version_id, payload.parent_approval, budget, seen)
            old = parse_version_payload(parent.payload_json)
            source = LearningSourceStore(self.root, limits=self.limits)._read_source(payload.learning_source_id, payload.learning_approval, budget)
            original = LearningSourcePayload.model_validate_json(source.payload_json)
            draft = LearningDraftPayload.model_validate_json(original.draft.payload_json)
            if (original.draft.draft_id != payload.approval.source_draft_id or payload.projection != draft.projection or
                    payload.draft_cost != draft.cost or payload.operation_cost != original.operation_cost or
                    payload.approval.skill_id != old.approval.skill_id or
                    payload.approval.projection_sha256 == old.approval.projection_sha256 or
                    not set(payload.approval.allowed_splits).issubset(old.approval.allowed_splits) or
                    not set(payload.approval.allowed_splits).issubset(draft.allowed_splits) or
                    not set(old.approval.excluded_lineage_groups).issubset(payload.approval.excluded_lineage_groups) or
                    not set(draft.excluded_lineage_groups).issubset(payload.approval.excluded_lineage_groups)):
                raise ValueError("saved model Skill expands authority or changes reviewed original")
            groups |= set(old.learning_lineage_groups) | set(draft.learning_lineage_groups)
            if tuple(sorted(groups)) != payload.learning_lineage_groups:
                raise ValueError("model Skill learning lineage closure differs")
            seen.remove(version_id)
            return version
        approval = payload.approval
        if approval.parent_version_id is not None:
            if payload.parent_approval is None or payload.candidate_approval is None or payload.feedback_approval is None:
                raise ValueError("Skill revision approvals missing")
            parent = self._read_version(approval.parent_version_id, payload.parent_approval, budget, seen)
            old = VersionPayload.model_validate_json(parent.payload_json)
            if (old.approval.skill_id != approval.skill_id or
                    not set(approval.allowed_splits).issubset(old.approval.allowed_splits) or
                    not set(old.approval.excluded_lineage_groups).issubset(approval.excluded_lineage_groups) or
                    not set(payload.feedback_approval.allowed_splits).issuperset(approval.allowed_splits) or
                    not set(payload.feedback_approval.excluded_lineage_groups).issubset(approval.excluded_lineage_groups)):
                raise ValueError("saved Skill revision changed inherited authority")
            source = self._folder("source-feedback") / (approval.source_candidate_id or "")
            feedback = FeedbackStore(source, limits=self.limits)
            candidate = feedback._read_candidate(approval.source_candidate_id or "", payload.candidate_approval,
                                                  payload.feedback_approval, budget)
            if canonical(candidate.contents()["projection"]) != canonical(payload.projection.model_dump(mode="json")):
                raise ValueError("Skill version differs from approved source candidate body")
            capsule_id = candidate.contents()["capsule_id"]
            capsule = feedback._read_capsule(capsule_id, payload.feedback_approval, budget)
            groups |= set(old.learning_lineage_groups) | _historical_lineages(feedback.root, capsule)
        if tuple(sorted(groups)) != payload.learning_lineage_groups:
            raise ValueError("Skill saved learning lineage closure changed")
        seen.remove(version_id)
        return version

    def export_version_sources(self, version_id: str, *, approval: VersionApproval) -> dict[str, bytes]:
        """Authenticate and return only this version's complete immutable closure."""
        return self._export_version_sources(version_id, _copy(approval), _ReadBudget(self.limits.max_capture_read_bytes))

    def export_version_bundle(self, version_id: str, *, approval: VersionApproval) -> tuple[dict[str, bytes], tuple[str, ...]]:
        return self._export_version_bundle(version_id, _copy(approval), _ReadBudget(self.limits.max_capture_read_bytes))

    def _export_version_bundle(self, version_id: str, approval: VersionApproval,
                               budget: _ReadBudget) -> tuple[dict[str, bytes], tuple[str, ...]]:
        directories: set[str] = set()
        files = self._export_version_sources(version_id, approval, budget, directories=directories)
        directories.update(parent.as_posix() for name in files for parent in Path(name).parents if parent.as_posix() != ".")
        return files, tuple(sorted(directories))

    def _export_version_sources(self, version_id: str, approval: VersionApproval, budget: _ReadBudget,
                                *, directories: set[str] | None = None) -> dict[str, bytes]:
        initial = budget.used
        original = self._read_version(version_id, approval, budget, set())
        semantic_reads = budget.used - initial
        files: dict[str, bytes] = {}
        visited: set[str] = set()
        captured: list[_ClosedTree] = []

        def add(relative: str, raw: bytes) -> None:
            if relative in files and files[relative] != raw:
                raise ValueError("version source export has conflicting originals")
            files[relative] = raw
            if len(files) > self.limits.max_entries or sum(map(len, files.values())) > self.limits.max_total_bytes:
                raise ValueError("version source export exceeds closure capacity")

        def collect(identifier: str, external: VersionApproval) -> None:
            if identifier in visited:
                return
            visited.add(identifier)
            path = self.root / "versions" / (identifier + ".json")
            record = _ClosedTree(self.root, self.limits, budget)
            raw = record.capture_file(path.relative_to(self.root).as_posix())
            captured.append(record)
            version = parse_version(raw.decode("utf-8"))
            if (encode(version).encode("utf-8") != raw or version.version_id != identifier or
                    parse_version_payload(version.payload_json).approval != external):
                raise ValueError("version export record differs from its authenticated link")
            add(path.relative_to(self.root).as_posix(), raw)
            body = parse_version_payload(version.payload_json)
            source = ("source-learning/" + body.learning_source_id if isinstance(body, ModelVersionPayload)
                      else None if body.approval.source_candidate_id is None else "source-feedback/" + body.approval.source_candidate_id)
            if source is not None:
                tree = _ClosedTree(self.root / source, self.limits, budget)
                tree.scan(((".", None),))
                captured.append(tree)
                if directories is not None:
                    directories.add(source)
                    directories.update(source + "/" + path for path in tree.directories)
                for relative, raw in tree.raw.items():
                    add(source + "/" + relative, raw)
            if body.approval.parent_version_id is not None and body.parent_approval is not None:
                collect(body.approval.parent_version_id, body.parent_approval)

        collect(version_id, approval)
        budget.require_remaining(semantic_reads + sum(tree.byte_count for tree in captured))
        if self._read_version(version_id, approval, budget, set()) != original:
            raise ValueError("version source closure changed during export")
        # Close the exact captured bytes after the final semantic read. A source
        # temporarily changed during capture and restored before that read must
        # never become the authenticated copy supplied to a new task.
        for tree in captured:
            tree.finish()
        return dict(sorted(files.items()))

    def export_version_source_directories(self, version_id: str, *, approval: VersionApproval) -> tuple[str, ...]:
        """Keep empty original directories in addition to exported file bytes."""
        return self._export_version_source_directories(version_id, _copy(approval), _ReadBudget(self.limits.max_capture_read_bytes))

    def _export_version_source_directories(self, version_id: str, approval: VersionApproval,
                                          budget: _ReadBudget) -> tuple[str, ...]:
        current = parse_version_payload(self._read_version(version_id, approval, budget, set()).payload_json)
        directories: set[str] = {"versions"}
        while True:
            source = ("source-learning/" + current.learning_source_id if isinstance(current, ModelVersionPayload)
                      else None if current.approval.source_candidate_id is None else "source-feedback/" + current.approval.source_candidate_id)
            if source is not None:
                tree = _ClosedTree(self.root / source, self.limits, budget)
                tree.scan(((".", None),))
                directories.add(source)
                directories.add(source.split("/")[0])
                directories.update(source + "/" + path for path in tree.directories)
                tree.finish()
            if current.approval.parent_version_id is None or current.parent_approval is None:
                break
            current = parse_version_payload(self._read_version(current.approval.parent_version_id, current.parent_approval, budget, set()).payload_json)
        return tuple(sorted(directories))

    def version_learning_costs(self, version_id: str, *, approval: VersionApproval) -> dict[str, Any]:
        return self._version_learning_costs(version_id, _copy(approval), _ReadBudget(self.limits.max_capture_read_bytes))

    def _version_learning_costs(self, version_id: str, approval: VersionApproval,
                                budget: _ReadBudget) -> dict[str, Any]:
        files, _directories = self._export_version_bundle(version_id, approval, budget)
        return _learning_costs_from_bundle(files, version_id, approval)

    def _validate_spec(self, spec: PlanSpec | PlanSpecV2, approval: ValidationApproval,
                       budget: _ReadBudget) -> tuple[set[str], list[Any]]:
        if spec.schema_version != approval.schema_version:
            raise ValueError("Skill plan and external approval mix incompatible generations")
        if spec.policy_json is not None:
            from .skill_transition_contracts import parse_synthetic_policy
            if parse_synthetic_policy(spec.policy_json).schema_version != spec.schema_version:
                raise ValueError("Skill policy changes the plan's cost-accounting generation")
        if content_hash(spec) != approval.spec_sha256:
            raise ValueError("Skill validation spec differs from external approval")
        old = self._read_version(spec.old_version_id, approval.old_version_approval, budget, set())
        new = self._read_version(spec.new_version_id, approval.new_version_approval, budget, set())
        new_payload = parse_version_payload(new.payload_json)
        if new_payload.schema_version != spec.schema_version:
            raise ValueError("Skill plan does not match its candidate generation")
        if new_payload.approval.parent_version_id != old.version_id:
            raise ValueError("Skill validation must compare a direct parent and revision")
        groups = set(old.contents()["learning_lineage_groups"]) | set(new_payload.learning_lineage_groups)
        manifests: list[Any] = []
        lineages, logs, tasks, works, runs = set(), set(), set(), set(), set()
        for pair in spec.pairs:
            for role in ("old", "new"):
                manifest = _single(getattr(pair, role + "_manifest_json"), schema=6 if spec.schema_version == 2 else 5)
                task, cell = manifest.tasks[0], manifest.runs[0]
                version_approval = getattr(approval, role + "_version_approval")
                if (task.lineage_group_id in groups or task.lineage_group_id in version_approval.excluded_lineage_groups or
                        "candidate_validation" not in version_approval.allowed_splits or
                        manifest.configuration.knowledge_profile["scope"]["split"] != "candidate_validation"):
                    raise ValueError("Skill validation is not independent permitted candidate-validation material")
                config = manifest.configuration.model_dump(mode="json")
                if spec.schema_version == 2:
                    files = config["shared_version_skill_profile"]["resources"]
                    target = [file for file in files if file["version_id"] == getattr(pair, role + "_entry_id")]
                    if getattr(pair, role + "_entry_id") != (spec.old_version_id if role == "old" else spec.new_version_id):
                        raise ValueError("model validation role names another frozen version")
                else:
                    files = config["shared_static_skill_profile"]["files"]
                    target = [file for file in files if file["entry_id"] == getattr(pair, role + "_entry_id")]
                if len(target) != 1 or target[0]["projection_sha256"] != version_approval.projection_sha256:
                    raise ValueError("Skill validation role target is not its complete approved version")
                if cell.run_id in runs or cell.work_path in works:
                    raise ValueError("Skill validation repeats a role run or work path")
                runs.add(cell.run_id)
                works.add(cell.work_path)
                manifests.append(manifest)
            task = manifests[-1].tasks[0]
            if task.lineage_group_id in lineages or task.crash_sha256 in logs or task.task_id in tasks:
                raise ValueError("Skill validation cannot count duplicate tasks, logs or lineage as independent")
            lineages.add(task.lineage_group_id)
            logs.add(task.crash_sha256)
            tasks.add(task.task_id)
        paths = [Path(task.task_path) for task in (item.tasks[0] for item in manifests[::2])]
        paths.extend(Path(item.runs[0].work_path or "") for item in manifests)
        paths = [Path(str(path).casefold()) for path in paths]
        if any(_overlap(left, right) for index, left in enumerate(paths) for right in paths[index + 1:]):
            raise ValueError("Skill validation input/output paths overlap")
        first_configuration = canonical(manifests[0].configuration.model_dump(mode="json"))
        if any(canonical(item.configuration.model_dump(mode="json")) != first_configuration for item in manifests):
            raise ValueError("Skill validation cases must share the same knowledge, model, tools and budgets")
        return groups, manifests

    def freeze_plan(self, spec: PlanSpec | PlanSpecV2, evidence_root: Path, *,
                    approval: ValidationApproval) -> ValidationPlanRecord:
        spec, approval = _copy(spec), _copy(approval)
        budget = _ReadBudget(self.limits.max_capture_read_bytes)
        initial = budget.used
        groups, manifests = self._validate_spec(spec, approval, budget)
        version_reads = budget.used - initial
        source = local_evidence_path(evidence_root)
        _guard(source)
        works = tuple(sorted(item.runs[0].work_path or "" for item in manifests))
        if any(_exists(source / path) for path in works):
            raise ValueError("Skill validation plan must precede every role run creation")
        source_identity = _directory_anchor(source.lstat())
        if any(_overlap((source / path).resolve(), self.root.resolve()) for path in works):
            raise ValueError("Skill validation store overlaps a prospective role run")
        for manifest in manifests:
            task = manifest.tasks[0]
            if _overlap((source / task.task_path).resolve(), self.root.resolve()):
                raise ValueError("Skill validation store overlaps an input task")
            actual = verify_task(source / task.task_path)
            if (actual.get("schema_version") != 2 or task.selection is None or
                    actual["case_identity_sha256"] != task.selection.sha256() or
                    any(actual.get(name) != getattr(task, name) for name in
                        ("task_id", "source_snapshot_sha256", "crash_sha256")) or
                    digest((source / task.task_path / "task.json").read_bytes()) != task.task_sha256):
                raise ValueError("Skill prospective task differs from its exact selection")
        payload_type: Any = PlanPayloadV2 if spec.schema_version == 2 else PlanPayload
        payload = payload_type(approval=approval, spec=spec,
            frozen_at=datetime.now(UTC).isoformat(), evidence_root=str(source),
            evidence_root_identity=_directory_anchor(source.lstat()), learning_lineage_groups=tuple(sorted(groups)),
            observed_absent_work_paths=works)
        key = content_hash(payload)
        record_type: Any = SkillValidationPlanV2 if spec.schema_version == 2 else SkillValidationPlan
        plan = record_type(plan_id="skill-plan-" + key, content_sha256=key, payload_json=encode(payload))
        if any(_exists(source / path) for path in works) or _directory_anchor(source.lstat()) != source_identity:
            raise ValueError("Skill role run appeared before plan publication")
        self._publish_record("plans", plan.plan_id, plan, budget,
                             2 * len(encode(plan).encode("utf-8")) + version_reads)
        return self._read_plan(plan.plan_id, approval, budget)

    def read_plan(self, plan_id: str, *, approval: ValidationApproval) -> ValidationPlanRecord:
        return self._read_plan(plan_id, _copy(approval), _ReadBudget(self.limits.max_capture_read_bytes))

    def _read_plan(self, plan_id: str, approval: ValidationApproval, budget: _ReadBudget) -> ValidationPlanRecord:
        _identity(plan_id, "skill-plan-")
        plan = self._record("plans", plan_id, parse_plan, budget)
        if plan.plan_id != plan_id:
            raise ValueError("Skill validation plan filename differs")
        payload = parse_plan_payload(plan.payload_json)
        if payload.approval != approval:
            raise ValueError("Skill validation plan differs from external original approval")
        _timestamp(payload.frozen_at)
        groups, manifests = self._validate_spec(payload.spec, approval, budget)
        if (tuple(sorted(groups)) != payload.learning_lineage_groups or
                tuple(sorted(item.runs[0].work_path or "" for item in manifests)) != payload.observed_absent_work_paths):
            raise ValueError("Skill plan scope or absent-run observation changed")
        return plan

    def _receipt_facts(self, plan: ValidationPlanRecord, case_id: str, role: Literal["old", "new"],
                       source: Path) -> tuple[Any, dict[str, Any],
                                              Literal["followed", "not_followed", "unknown"], tuple[str, ...]]:
        payload = parse_plan_payload(plan.payload_json)
        pair = next((item for item in payload.spec.pairs if item.case_id == case_id), None)
        if pair is None or role not in ("old", "new"):
            raise ValueError("Skill validation role is not in the frozen plan")
        manifest = _single(getattr(pair, role + "_manifest_json"), schema=6 if payload.schema_version == 2 else 5)
        work = source / (manifest.runs[0].work_path or "")
        session = _load(work / "session.json")
        if (_timestamp(session["started_at"]) < _timestamp(payload.frozen_at) or
                type(session.get("schema_version")) is not int or session["schema_version"] != (14 if payload.schema_version == 2 else 13) or
                session["knowledge_profile"]["approval"]["scope"]["split"] != "candidate_validation"):
            raise ValueError("Skill validation run precedes its plan or changes original scope")
        if payload.schema_version == 2:
            binding = session["version_skill_profile"]["approval"]
            if (binding["validation_spec_sha256"] != payload.approval.spec_sha256 or
                    binding["validation_case_id"] != case_id or binding["validation_role"] != role or
                    binding["validation_plan_json"] != encode(plan) or
                    binding["validation_plan_approval"] != payload.approval.model_dump(mode="json")):
                raise ValueError("model Skill role uses another prospective validation approval")
        result = _aggregate(manifest, source)
        row = result["rows"][0]
        if row["terminal_status"] not in {"finished", "failed", "canceled"}:
            raise ValueError("Skill validation requires an actual stopped role run")
        reasons: list[str] = []
        facts = row.get("version_skill_facts" if payload.schema_version == 2 else "static_skill_facts")
        status: Literal["followed", "not_followed", "unknown"] = "unknown"
        if isinstance(facts, dict):
            target_entry = getattr(pair, role + "_entry_id")
            other_entry = pair.new_entry_id if role == "old" else pair.old_entry_id
            resource_key = "version_id" if payload.schema_version == 2 else "entry_id"
            target = next((item for item in facts["resources"] if item[resource_key] == target_entry), None)
            other = next((item for item in facts["resources"] if item[resource_key] == other_entry), None)
            if target is None:
                raise ValueError("Skill validation receipt has no approved role resource")
            if not target["actions"]:
                reasons.append("target_procedure_not_read")
            elif not any(item["visible_solver_count"] > 0 for item in target["actions"]):
                reasons.append("target_procedure_not_visible_to_later_solver")
            if other is not None and other["actions"]:
                reasons.append("other_role_procedure_read")
            # Plain retrieval can expose the other procedure through the same
            # common tools; it must not turn into a hidden qualifying exception.
            knowledge = row.get("knowledge_facts")
            if isinstance(knowledge, dict):
                other_entries = [other_entry]
                if payload.schema_version == 2:
                    other_approval = payload.approval.new_version_approval if role == "old" else payload.approval.old_version_approval
                    snapshot = KnowledgeStore(work / "knowledge").read_snapshot(session["knowledge_profile"]["approval"]["snapshot_sha256"])
                    other_entries = [ref.entry_id for ref in SnapshotPayload.model_validate_json(snapshot.payload_json).entries
                                     if ref.projection_sha256 == other_approval.projection_sha256]
                if any(_cross_role_retrieved(knowledge, entry) for entry in other_entries):
                    reasons.append("other_role_procedure_retrieved")
            status = "not_followed" if reasons else "followed"
            if any(item["state"] != "completed" for item in facts["actions"]):
                reasons.append("procedure_action_result_unknown")
                if "other_role_procedure_read" not in reasons and "other_role_procedure_retrieved" not in reasons:
                    status = "unknown"
        else:
            reasons.append("actual_procedure_use_unknown")
        return manifest, result, status, tuple(reasons)

    def record_validation(self, plan_id: str, case_id: str, role: Literal["old", "new"], evidence_root: Path, *,
                          approval: ValidationApproval) -> ValidationReceiptRecord:
        approval = _copy(approval)
        budget = _ReadBudget(self.limits.max_capture_read_bytes)
        initial = budget.used
        plan = self._read_plan(plan_id, approval, budget)
        plan_reads = budget.used - initial
        plan_payload = parse_plan_payload(plan.payload_json)
        source = local_evidence_path(evidence_root)
        _guard(source)
        if str(source) != plan_payload.evidence_root or _directory_anchor(source.lstat()) != plan_payload.evidence_root_identity:
            raise ValueError("Skill validation source root differs from preplan host observation")
        pair = next((item for item in plan_payload.spec.pairs if item.case_id == case_id), None)
        if pair is None or role not in ("old", "new"):
            raise ValueError("Skill validation role is absent from frozen plan")
        manifest = _single(getattr(pair, role + "_manifest_json"), schema=6 if plan_payload.schema_version == 2 else 5)
        work = manifest.runs[0].work_path or ""
        if any(_overlap((source / relative).resolve(), self.root.resolve()) for relative in (manifest.tasks[0].task_path, work)):
            raise ValueError("Skill receipt store overlaps its original inputs")
        tree = _ClosedTree(source, self.limits, budget)
        tree.scan(((manifest.tasks[0].task_path, _TASK_ENTRIES), (work, _WORK_ENTRIES)), exclude_graph=True)
        manifest, aggregate, use_status, reasons = self._receipt_facts(plan, case_id, role, source)
        tree.finish()
        version = approval.old_version_approval if role == "old" else approval.new_version_approval
        version_id = plan_payload.spec.old_version_id if role == "old" else plan_payload.spec.new_version_id
        skill_digest = version_id.removeprefix("skill-version-") if plan_payload.schema_version == 2 else version.projection_sha256
        payload_type: Any = ReceiptPayloadV2 if plan_payload.schema_version == 2 else ReceiptPayload
        payload = payload_type(plan_id=plan_id, approval_sha256=content_hash(approval), case_id=case_id, role=role,
            version_id=version_id,
            manifest_json=canonical(manifest.model_dump(mode="json")), aggregate_json=canonical(aggregate),
            directories=tuple(sorted(tree.directories)), files=tuple(tree.files[key] for key in sorted(tree.files)),
            target_projection_sha256=version.projection_sha256, target_skill_id="dfx-" + skill_digest[:56],
            use_status=use_status, use_reasons=reasons)
        key = content_hash(payload)
        record_type: Any = SkillValidationReceiptV2 if plan_payload.schema_version == 2 else SkillValidationReceipt
        receipt = record_type(receipt_id="skill-receipt-" + key, content_sha256=key, payload_json=encode(payload))
        raw = encode(receipt).encode("utf-8")
        if len(raw) + tree.byte_count > self.limits.max_total_bytes or len(raw) > self.limits.max_file_bytes:
            raise ValueError("Skill receipt closure exceeds publication capacity")
        destination = self.root / "receipts" / receipt.receipt_id
        if _exists(destination):
            return self._read_receipt(receipt.receipt_id, approval, budget)
        receipt_reads = 2 * (len(raw) + tree.byte_count) + plan_reads
        budget.require_remaining(2 * receipt_reads)
        folder = self._folder("receipts", create=True)
        pending = folder / ("pending-" + uuid4().hex)
        _publish_tree(pending, tree, "receipt.json", raw)
        initial = budget.used
        self._read_receipt_directory(pending, receipt.receipt_id, approval, budget)
        budget.require_remaining(budget.used - initial)
        if _exists(destination):
            raise ValueError("Skill validation receipt raced another writer")
        os.rename(pending, destination)
        return self._read_receipt(receipt.receipt_id, approval, budget)

    def read_receipt(self, receipt_id: str, *, approval: ValidationApproval) -> ValidationReceiptRecord:
        return self._read_receipt(receipt_id, _copy(approval), _ReadBudget(self.limits.max_capture_read_bytes))

    def _read_receipt(self, receipt_id: str, approval: ValidationApproval,
                      budget: _ReadBudget) -> ValidationReceiptRecord:
        _identity(receipt_id, "skill-receipt-")
        return self._read_receipt_directory(self._folder("receipts") / receipt_id, receipt_id, approval, budget)

    def _receipt_closed(self, approval: ValidationApproval, receipt: ValidationReceiptRecord,
                        tree: _ClosedTree, metadata: _ClosedTree, budget: _ReadBudget) -> None:
        """Private operation views may retain this fully authenticated raw proof."""

    def _read_receipt_directory(self, directory: Path, receipt_id: str, approval: ValidationApproval,
                                budget: _ReadBudget) -> ValidationReceiptRecord:
        _guard(directory)
        if {item.name for item in directory.iterdir()} != {"receipt.json", "payload"}:
            raise ValueError("Skill receipt has extra or missing originals")
        metadata = _ClosedTree(directory, self.limits, budget)
        raw = metadata.capture_file("receipt.json")
        metadata.members[metadata.root] = ("payload", "receipt.json")
        receipt = parse_receipt(encode(_object(raw.decode("utf-8"))))
        if receipt.receipt_id != receipt_id or encode(receipt).encode("utf-8") != raw:
            raise ValueError("Skill receipt canonical bytes or filename changed")
        payload = parse_receipt_payload(receipt.payload_json)
        if payload.approval_sha256 != content_hash(approval):
            raise ValueError("Skill receipt differs from external original validation approval")
        plan = self._read_plan(payload.plan_id, approval, budget)
        tree = _ClosedTree(directory / "payload", self.limits, budget)
        tree.scan(((".", None),))
        if tuple(sorted(tree.directories)) != payload.directories or tuple(tree.files[key] for key in sorted(tree.files)) != payload.files:
            raise ValueError("Skill receipt complete original inventory changed")
        if len(raw) + tree.byte_count > self.limits.max_total_bytes:
            raise ValueError("Skill receipt exceeds read capacity")
        manifest, aggregate, status, reasons = self._receipt_facts(plan, payload.case_id, payload.role, tree.root)
        if (canonical(manifest.model_dump(mode="json")) != payload.manifest_json or canonical(aggregate) != payload.aggregate_json or
                status != payload.use_status or reasons != payload.use_reasons):
            raise ValueError("Skill receipt disagrees with independently recomputed full facts")
        version = approval.old_version_approval if payload.role == "old" else approval.new_version_approval
        planned = parse_plan_payload(plan.payload_json).spec
        if (payload.version_id != (planned.old_version_id if payload.role == "old" else planned.new_version_id) or
                payload.target_projection_sha256 != version.projection_sha256):
            raise ValueError("Skill receipt changes its intended version")
        tree.finish()
        metadata.finish()
        self._receipt_closed(approval, receipt, tree, metadata, budget)
        return receipt
