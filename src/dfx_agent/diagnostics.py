"""Admit model diagnosis proposals against host-read facts and frozen checks."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from pydantic import Field

from .contracts import (
    Contract,
    Diagnosis,
    Evidence,
    RepairObligation,
    SourceLocation,
    admit_evidence,
    execution_evidence_kind,
    require_identifiers,
    require_path,
    validate_references,
)
from .logs import decode_text
from .task import digest
from .verification import CheckLedger, _atomic_json
from .workspace import CandidateWorkspace


class SourceCitation(Contract):
    evidence_id: str
    location: SourceLocation


class LogCitation(Contract):
    evidence_id: str
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)


class ModelNote(Contract):
    evidence_id: str
    statement: str = Field(min_length=1)
    basis_ids: list[str] = Field(min_length=1)


class FeedbackCitation(Contract):
    evidence_id: str
    candidate_id: str
    candidate_snapshot_sha256: str
    execution_id: str


class DiagnosisSubmission(Contract):
    diagnosis: Diagnosis
    source_citations: list[SourceCitation] = Field(default_factory=list)
    log_citations: list[LogCitation] = Field(default_factory=list)
    inferences: list[ModelNote] = Field(default_factory=list)
    feedback_citations: list[FeedbackCitation] = Field(default_factory=list)
    existing_evidence_ids: list[str] = Field(default_factory=list)
    obligations: list[RepairObligation] = Field(default_factory=list)


def safe_source(root: Path, relative: str) -> Path:
    require_path(relative)
    path = root / relative
    if not path.is_file() or path.is_symlink() or not path.resolve(strict=True).is_relative_to(root.resolve(strict=True)):
        raise ValueError("source path is missing or linked")
    return path


class AnalysisStore:
    """Immutable diagnosis bundles; facts are produced by trusted code, not JSON labels."""

    def __init__(self, workspace: CandidateWorkspace):
        self.workspace = workspace

    def _paths(self, folder_name: str) -> list[Path]:
        folder = self.workspace.root / folder_name
        if not folder.exists():
            return []
        if not folder.is_dir() or folder.is_symlink():
            raise ValueError("analysis folder is missing or linked")
        paths = sorted(folder.glob("*.json"))
        if any(path.is_symlink() or not path.is_file() for path in paths):
            raise ValueError("analysis record is linked")
        return paths

    def _owner(self, record: Diagnosis | Evidence | RepairObligation) -> None:
        if (record.task_id != self.workspace.task_id or record.run_id != self.workspace.run_id or
                record.source_snapshot_sha256 != self.workspace.source_snapshot_sha256):
            raise ValueError("analysis record belongs to another task, run or snapshot")

    def bundles(self) -> dict[str, dict]:
        found = {}
        for path in self._paths("diagnoses"):
            raw = json.loads(path.read_text(encoding="utf-8"))
            diagnosis = Diagnosis.model_validate_json(json.dumps(raw["diagnosis"]))
            self._owner(diagnosis)
            if path.stem != diagnosis.record_id:
                raise ValueError("diagnosis identity differs from its file")
            facts = [Evidence.model_validate_json(json.dumps(item)) for item in raw["evidence"]]
            obligations = [RepairObligation.model_validate_json(json.dumps(item)) for item in raw.get("obligations", [])]
            validate_references(diagnosis, facts, self.line_counts(), obligations)
            found[diagnosis.record_id] = {"diagnosis": diagnosis, "evidence": facts, "obligations": obligations}
        return found

    def evidence(self) -> dict[str, Evidence]:
        found: dict[str, Evidence] = {}
        for bundle in self.bundles().values():
            for fact in bundle["evidence"]:
                if fact.record_id in found and found[fact.record_id] != fact:
                    raise ValueError("evidence identity changed")
                found[fact.record_id] = fact
        return found

    def obligations(self) -> dict[str, RepairObligation]:
        found: dict[str, RepairObligation] = {}
        records = [item for bundle in self.bundles().values() for item in bundle["obligations"]]
        for path in self._paths("obligations"):
            item = RepairObligation.model_validate_json(path.read_text(encoding="utf-8"))
            if path.stem != item.record_id:
                raise ValueError("obligation identity differs from its file")
            records.append(item)
        for item in records:
            self._owner(item)
            if item.record_id in found:
                raise ValueError("duplicate obligation identity")
            found[item.record_id] = item
        return found

    def reference_ids(self) -> tuple[set[str], set[str]]:
        bundles = self.bundles()
        evidence = self.evidence()
        targets = set(bundles) | set(self.obligations())
        for bundle in bundles.values():
            targets.update(item.hypothesis_id for item in bundle["diagnosis"].hypotheses)
        return set(evidence), targets

    def line_counts(self) -> dict[str, int]:
        return {path.relative_to(self.workspace.base).as_posix():
                len(path.read_text(encoding="utf-8").splitlines())
                for path in self.workspace.base.rglob("*") if path.is_file()}

    def put_bundle(self, diagnosis: Diagnosis, evidence: list[Evidence],
                   obligations: list[RepairObligation]) -> dict:
        self._owner(diagnosis)
        validate_references(diagnosis, evidence, self.line_counts(), obligations)
        bundles, existing_facts, existing_obligations = self.bundles(), self.evidence(), self.obligations()
        used_targets = set(bundles) | set(existing_obligations)
        for bundle in bundles.values():
            used_targets.update(item.hypothesis_id for item in bundle["diagnosis"].hypotheses)
        new_targets = [diagnosis.record_id, *(item.hypothesis_id for item in diagnosis.hypotheses),
                       *(item.record_id for item in obligations)]
        if (len(set(new_targets)) != len(new_targets) or set(new_targets) & used_targets or
                set(new_targets) & (set(existing_facts) | {item.record_id for item in evidence})):
            raise ValueError("analysis target identity already exists or overlaps")
        for fact in evidence:
            if fact.record_id in used_targets or (fact.record_id in existing_facts and existing_facts[fact.record_id] != fact):
                raise ValueError("evidence identity changed or overlaps a target")
        value = {"diagnosis": diagnosis.model_dump(mode="json"),
                 "evidence": [item.model_dump(mode="json") for item in evidence],
                 "obligations": [item.model_dump(mode="json") for item in obligations]}
        folder = self.workspace.root / "diagnoses"
        folder.mkdir(exist_ok=True)
        if folder.is_symlink():
            raise ValueError("diagnosis directory is linked")
        _atomic_json(folder / f"{diagnosis.record_id}.json", value)
        return value

    def put_obligation(self, obligation: RepairObligation) -> RepairObligation:
        self._owner(obligation)
        facts = self.evidence()
        if not obligation.source_evidence_ids or any(value not in facts for value in obligation.source_evidence_ids):
            raise ValueError("obligation evidence is not registered")
        if obligation.hypothesis_ids or obligation.bindings:
            raise ValueError("structured obligations must be admitted with their diagnosis bundle")
        evidence_ids, targets = self.reference_ids()
        if obligation.record_id in targets or obligation.record_id in evidence_ids:
            raise ValueError("obligation identity already exists")
        if obligation.authority != "model_inference" or obligation.status != "unknown":
            raise ValueError("model obligation must retain unknown authority and status")
        folder = self.workspace.root / "obligations"
        folder.mkdir(exist_ok=True)
        if folder.is_symlink():
            raise ValueError("obligation directory is linked")
        _atomic_json(folder / f"{obligation.record_id}.json", obligation.model_dump(mode="json"))
        return obligation

    def admit(self, submission: DiagnosisSubmission, *, task: Path, log_path: str,
              log_sha256: str, ledger: CheckLedger, allow_obligations: bool) -> dict:
        diagnosis = submission.diagnosis
        self._owner(diagnosis)
        if any(item.support_status != "unknown" for item in diagnosis.hypotheses):
            raise ValueError("model hypotheses must retain unknown support status")
        if any(step.authority != "model_inference" for item in diagnosis.hypotheses for step in item.causal_steps):
            raise ValueError("model causal relations must remain model inference")
        if submission.obligations and not allow_obligations:
            raise ValueError("obligation tracking is disabled")
        now = datetime.now(UTC)
        diagnosis = diagnosis.model_copy(update={"created_at": now})
        identity = {"task_id": self.workspace.task_id, "run_id": self.workspace.run_id,
                    "source_snapshot_sha256": self.workspace.source_snapshot_sha256, "created_at": now}
        facts: list[Evidence] = []
        existing = self.evidence()
        require_identifiers(submission.existing_evidence_ids)
        pending = list(submission.existing_evidence_ids)
        included: set[str] = set()
        while pending:
            identifier = pending.pop()
            if identifier in included:
                continue
            if identifier not in existing:
                raise ValueError("existing evidence is not registered")
            included.add(identifier)
            facts.append(existing[identifier])
            pending.extend(existing[identifier].basis_ids)
        for citation in submission.source_citations:
            location = citation.location
            if location.snapshot_sha256 != self.workspace.source_snapshot_sha256:
                raise ValueError("source citation has another snapshot")
            lines = safe_source(self.workspace.base, location.path).read_text(encoding="utf-8").splitlines()
            text = self._range(lines, location.start_line, location.end_line)
            facts.append(admit_evidence({**identity, "record_id": citation.evidence_id, "kind": "source_fact",
                         "artifact_sha256": digest(text.encode()), "statement": text, "location": location},
                         channel="source_reader"))
        if submission.log_citations:
            raw_log = safe_source(task, log_path).read_bytes()
            if digest(raw_log) != log_sha256:
                raise ValueError("input log changed")
            lines = decode_text(raw_log)[0].splitlines()
            for log in submission.log_citations:
                text = self._range(lines, log.start_line, log.end_line)
                facts.append(admit_evidence({**identity, "record_id": log.evidence_id, "kind": "log_observation",
                             "artifact_sha256": log_sha256, "statement": text, "log_line": log.start_line}, channel="input"))
        for feedback in submission.feedback_citations:
            if feedback.candidate_id != diagnosis.feedback_candidate_id:
                raise ValueError("feedback belongs to another diagnosis candidate")
            candidate_source = self.workspace.require_registered(feedback.candidate_id)
            candidate = json.loads((candidate_source.parent / "candidate.json").read_text(encoding="utf-8"))
            result = ledger.completed(feedback.candidate_id)
            if (candidate["candidate_snapshot_sha256"] != feedback.candidate_snapshot_sha256 or result is None or
                    result.status != "fail" or result.idempotency_key != feedback.execution_id):
                raise ValueError("feedback has no matching failed check and version")
            facts.append(admit_evidence({**identity, "record_id": feedback.evidence_id,
                         "kind": execution_evidence_kind(result.execution_scope),
                         "artifact_sha256": digest(result.model_dump_json().encode()), "statement": result.detail,
                         "execution_id": result.idempotency_key, "evaluated_candidate_id": feedback.candidate_id,
                         "execution_snapshot_sha256": feedback.candidate_snapshot_sha256, "execution_scope": result.execution_scope},
                         channel="execution_backend"))
        for note in submission.inferences:
            facts.append(admit_evidence({**identity, "record_id": note.evidence_id, "kind": "model_inference",
                         "artifact_sha256": digest(note.statement.encode()), "statement": note.statement,
                         "basis_ids": note.basis_ids}, channel="model"))
        obligations = []
        for obligation in submission.obligations:
            self._owner(obligation)
            if (obligation.authority != "model_inference" or obligation.status != "unknown" or
                    not obligation.source_evidence_ids):
                raise ValueError("model obligation must retain unknown authority and status with evidence")
            obligations.append(obligation.model_copy(update={"created_at": now}))
        return self.put_bundle(diagnosis, facts, obligations)

    @staticmethod
    def _range(lines: list[str], start: int, end: int) -> str:
        if start < 1 or end < start or end > len(lines) or end - start > 199:
            raise ValueError("citation line range is outside the artifact or too large")
        return "\n".join(lines[start - 1:end])
