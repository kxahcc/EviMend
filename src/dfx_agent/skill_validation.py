"""Synthetic Skill source closure, prospective validation and saved-run audits.

Two roles share an unchanged knowledge/resource pool and differ in the intended
procedure read. This is not an evaluation of different knowledge snapshots.
No Agent, model, check, Graph or application is executed by these interfaces.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from .evidence_io import local_evidence_path
from .exporting import _TASK_ENTRIES, _WORK_ENTRIES
from .feedback import FeedbackStore, _ClosedTree, _copy, _exists, _ReadBudget
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
from .metrics import (
    EvaluationManifestV5,
    _guard,
    _load,
    _overlap,
    aggregate_evaluation,
    parse_evaluation_manifest,
)
from .model_calls import ModelCallConflict, canonical
from .skill_evolution_contracts import (
    HostSkillVersionApproval,
    HostValidationPlanApproval,
    PlanPayload,
    PlanSpec,
    ReceiptPayload,
    SkillValidationPlan,
    SkillValidationReceipt,
    SkillVersion,
    VersionPayload,
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
            any(_directory_anchor(parent.lstat()) != identity
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


def _single(value: str) -> EvaluationManifestV5:
    manifest = parse_evaluation_manifest(_object(value))
    if not isinstance(manifest, EvaluationManifestV5):
        raise ValueError("Skill validation requires manifest 5")  # noqa: TRY004 - unsupported schema is invalid evidence
    return manifest


def _timestamp(value: str) -> datetime:
    if type(value) is not str:
        raise ValueError("Skill validation timestamp is not text")
    actual = datetime.fromisoformat(value)
    if actual.tzinfo is None:
        raise ValueError("Skill validation timestamp has no timezone")
    return actual


def _aggregate(manifest: EvaluationManifestV5, root: Path) -> dict[str, Any]:
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
        record = model.model_validate_json(encode(_object(raw.decode("utf-8"))))
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
        return self._read_version(version.version_id, approval, budget, set())

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
        return self._read_version(version.version_id, approval, budget, set())

    def read_version(self, version_id: str, *, approval: HostSkillVersionApproval) -> SkillVersion:
        return self._read_version(version_id, _copy(approval), _ReadBudget(self.limits.max_capture_read_bytes), set())

    def _read_version(self, version_id: str, approval: HostSkillVersionApproval,
                      budget: _ReadBudget, seen: set[str]) -> SkillVersion:
        _identity(version_id, "skill-version-")
        if version_id in seen or len(seen) >= MAX_VERSION_DEPTH:
            raise ValueError("Skill version parent cycle or depth limit")
        seen.add(version_id)
        version = self._record("versions", version_id, SkillVersion, budget)
        if version.version_id != version_id:
            raise ValueError("Skill version filename changed")
        payload = VersionPayload.model_validate_json(version.payload_json)
        if payload.approval != approval:
            raise ValueError("Skill version differs from external original approval")
        groups = set(approval.learning_lineage_groups)
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

    def _validate_spec(self, spec: PlanSpec, approval: HostValidationPlanApproval,
                       budget: _ReadBudget) -> tuple[set[str], list[EvaluationManifestV5]]:
        if content_hash(spec) != approval.spec_sha256:
            raise ValueError("Skill validation spec differs from external approval")
        old = self._read_version(spec.old_version_id, approval.old_version_approval, budget, set())
        new = self._read_version(spec.new_version_id, approval.new_version_approval, budget, set())
        new_payload = VersionPayload.model_validate_json(new.payload_json)
        if new_payload.approval.parent_version_id != old.version_id:
            raise ValueError("Skill validation must compare a direct parent and revision")
        groups = set(old.contents()["learning_lineage_groups"]) | set(new_payload.learning_lineage_groups)
        manifests: list[EvaluationManifestV5] = []
        lineages, logs, tasks, works, runs = set(), set(), set(), set(), set()
        for pair in spec.pairs:
            for role in ("old", "new"):
                manifest = _single(getattr(pair, role + "_manifest_json"))
                task, cell = manifest.tasks[0], manifest.runs[0]
                version_approval = getattr(approval, role + "_version_approval")
                if (task.lineage_group_id in groups or task.lineage_group_id in version_approval.excluded_lineage_groups or
                        "candidate_validation" not in version_approval.allowed_splits or
                        manifest.configuration.knowledge_profile["scope"]["split"] != "candidate_validation"):
                    raise ValueError("Skill validation is not independent permitted candidate-validation material")
                files = manifest.configuration.shared_static_skill_profile["files"]
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

    def freeze_plan(self, spec: PlanSpec, evidence_root: Path, *,
                    approval: HostValidationPlanApproval) -> SkillValidationPlan:
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
        payload = PlanPayload(approval=approval, spec=spec,
            frozen_at=datetime.now(UTC).isoformat(), evidence_root=str(source),
            evidence_root_identity=_directory_anchor(source.lstat()), learning_lineage_groups=tuple(sorted(groups)),
            observed_absent_work_paths=works)
        key = content_hash(payload)
        plan = SkillValidationPlan(plan_id="skill-plan-" + key, content_sha256=key, payload_json=encode(payload))
        if any(_exists(source / path) for path in works) or _directory_anchor(source.lstat()) != source_identity:
            raise ValueError("Skill role run appeared before plan publication")
        self._publish_record("plans", plan.plan_id, plan, budget,
                             2 * len(encode(plan).encode("utf-8")) + version_reads)
        return self._read_plan(plan.plan_id, approval, budget)

    def read_plan(self, plan_id: str, *, approval: HostValidationPlanApproval) -> SkillValidationPlan:
        return self._read_plan(plan_id, _copy(approval), _ReadBudget(self.limits.max_capture_read_bytes))

    def _read_plan(self, plan_id: str, approval: HostValidationPlanApproval, budget: _ReadBudget) -> SkillValidationPlan:
        _identity(plan_id, "skill-plan-")
        plan = self._record("plans", plan_id, SkillValidationPlan, budget)
        if plan.plan_id != plan_id:
            raise ValueError("Skill validation plan filename differs")
        payload = PlanPayload.model_validate_json(plan.payload_json)
        if payload.approval != approval:
            raise ValueError("Skill validation plan differs from external original approval")
        _timestamp(payload.frozen_at)
        groups, manifests = self._validate_spec(payload.spec, approval, budget)
        if (tuple(sorted(groups)) != payload.learning_lineage_groups or
                tuple(sorted(item.runs[0].work_path or "" for item in manifests)) != payload.observed_absent_work_paths):
            raise ValueError("Skill plan scope or absent-run observation changed")
        return plan

    def _receipt_facts(self, plan: SkillValidationPlan, case_id: str, role: Literal["old", "new"],
                       source: Path) -> tuple[EvaluationManifestV5, dict[str, Any],
                                              Literal["followed", "not_followed", "unknown"], tuple[str, ...]]:
        payload = PlanPayload.model_validate_json(plan.payload_json)
        pair = next((item for item in payload.spec.pairs if item.case_id == case_id), None)
        if pair is None or role not in ("old", "new"):
            raise ValueError("Skill validation role is not in the frozen plan")
        manifest = _single(getattr(pair, role + "_manifest_json"))
        work = source / (manifest.runs[0].work_path or "")
        session = _load(work / "session.json")
        if (_timestamp(session["started_at"]) < _timestamp(payload.frozen_at) or
                type(session.get("schema_version")) is not int or session["schema_version"] != 13 or
                session["knowledge_profile"]["approval"]["scope"]["split"] != "candidate_validation"):
            raise ValueError("Skill validation run precedes its plan or changes original scope")
        result = _aggregate(manifest, source)
        row = result["rows"][0]
        if row["terminal_status"] not in {"finished", "failed", "canceled"}:
            raise ValueError("Skill validation requires an actual stopped role run")
        reasons: list[str] = []
        facts = row.get("static_skill_facts")
        status: Literal["followed", "not_followed", "unknown"] = "unknown"
        if isinstance(facts, dict):
            target_entry = getattr(pair, role + "_entry_id")
            other_entry = pair.new_entry_id if role == "old" else pair.old_entry_id
            target = next((item for item in facts["resources"] if item["entry_id"] == target_entry), None)
            other = next((item for item in facts["resources"] if item["entry_id"] == other_entry), None)
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
            if isinstance(knowledge, dict) and _cross_role_retrieved(knowledge, other_entry):
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
                          approval: HostValidationPlanApproval) -> SkillValidationReceipt:
        approval = _copy(approval)
        budget = _ReadBudget(self.limits.max_capture_read_bytes)
        initial = budget.used
        plan = self._read_plan(plan_id, approval, budget)
        plan_reads = budget.used - initial
        plan_payload = PlanPayload.model_validate_json(plan.payload_json)
        source = local_evidence_path(evidence_root)
        _guard(source)
        if str(source) != plan_payload.evidence_root or _directory_anchor(source.lstat()) != plan_payload.evidence_root_identity:
            raise ValueError("Skill validation source root differs from preplan host observation")
        pair = next((item for item in plan_payload.spec.pairs if item.case_id == case_id), None)
        if pair is None or role not in ("old", "new"):
            raise ValueError("Skill validation role is absent from frozen plan")
        manifest = _single(getattr(pair, role + "_manifest_json"))
        work = manifest.runs[0].work_path or ""
        if any(_overlap((source / relative).resolve(), self.root.resolve()) for relative in (manifest.tasks[0].task_path, work)):
            raise ValueError("Skill receipt store overlaps its original inputs")
        tree = _ClosedTree(source, self.limits, budget)
        tree.scan(((manifest.tasks[0].task_path, _TASK_ENTRIES), (work, _WORK_ENTRIES)), exclude_graph=True)
        manifest, aggregate, use_status, reasons = self._receipt_facts(plan, case_id, role, source)
        tree.finish()
        version = approval.old_version_approval if role == "old" else approval.new_version_approval
        payload = ReceiptPayload(plan_id=plan_id, approval_sha256=content_hash(approval), case_id=case_id, role=role,
            version_id=plan_payload.spec.old_version_id if role == "old" else plan_payload.spec.new_version_id,
            manifest_json=canonical(manifest.model_dump(mode="json")), aggregate_json=canonical(aggregate),
            directories=tuple(sorted(tree.directories)), files=tuple(tree.files[key] for key in sorted(tree.files)),
            target_projection_sha256=version.projection_sha256, target_skill_id="dfx-" + version.projection_sha256[:56],
            use_status=use_status, use_reasons=reasons)
        key = content_hash(payload)
        receipt = SkillValidationReceipt(receipt_id="skill-receipt-" + key, content_sha256=key, payload_json=encode(payload))
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

    def read_receipt(self, receipt_id: str, *, approval: HostValidationPlanApproval) -> SkillValidationReceipt:
        return self._read_receipt(receipt_id, _copy(approval), _ReadBudget(self.limits.max_capture_read_bytes))

    def _read_receipt(self, receipt_id: str, approval: HostValidationPlanApproval,
                      budget: _ReadBudget) -> SkillValidationReceipt:
        _identity(receipt_id, "skill-receipt-")
        return self._read_receipt_directory(self._folder("receipts") / receipt_id, receipt_id, approval, budget)

    def _read_receipt_directory(self, directory: Path, receipt_id: str, approval: HostValidationPlanApproval,
                                budget: _ReadBudget) -> SkillValidationReceipt:
        _guard(directory)
        if {item.name for item in directory.iterdir()} != {"receipt.json", "payload"}:
            raise ValueError("Skill receipt has extra or missing originals")
        metadata = _ClosedTree(directory, self.limits, budget)
        metadata._parents(directory)
        raw, descriptor = metadata._read(directory / "receipt.json")
        receipt = SkillValidationReceipt.model_validate_json(encode(_object(raw.decode("utf-8"))))
        if receipt.receipt_id != receipt_id or encode(receipt).encode("utf-8") != raw:
            raise ValueError("Skill receipt canonical bytes or filename changed")
        payload = ReceiptPayload.model_validate_json(receipt.payload_json)
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
        planned = PlanPayload.model_validate_json(plan.payload_json).spec
        if (payload.version_id != (planned.old_version_id if payload.role == "old" else planned.new_version_id) or
                payload.target_projection_sha256 != version.projection_sha256):
            raise ValueError("Skill receipt changes its intended version")
        tree.finish()
        after, after_descriptor = metadata._read(directory / "receipt.json")
        if (after != raw or after_descriptor != descriptor or
                any(_directory_anchor(parent.lstat()) != identity
                    for parent, identity in metadata.directory_identities.items()) or
                {item.name for item in directory.iterdir()} != {"receipt.json", "payload"}):
            raise ValueError("Skill receipt changed during readback")
        return receipt
