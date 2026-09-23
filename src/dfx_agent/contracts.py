"""Strict, source-bound records for the offline repair workflow."""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import PurePosixPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

SHA256 = re.compile(r"^[0-9a-f]{64}$")
IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    schema_version: Literal[1] = 1


class SourceLocation(Contract):
    snapshot_sha256: str
    path: str
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    symbol: str | None = None

    @model_validator(mode="after")
    def valid_location(self) -> SourceLocation:
        require_hash(self.snapshot_sha256)
        require_path(self.path)
        if self.end_line < self.start_line:
            raise ValueError("end_line precedes start_line")
        return self


class RecordIdentity(Contract):
    task_id: str
    run_id: str
    record_id: str
    created_at: datetime

    @model_validator(mode="after")
    def valid_identity(self) -> RecordIdentity:
        for value in (self.task_id, self.run_id, self.record_id):
            if not IDENTIFIER.fullmatch(value):
                raise ValueError("invalid task, run or record identifier")
        if self.created_at.tzinfo is None or self.created_at.utcoffset() is None:
            raise ValueError("created_at must have a timezone")
        return self


class Record(RecordIdentity):
    source_snapshot_sha256: str

    @model_validator(mode="after")
    def valid_source_snapshot(self) -> Record:
        require_hash(self.source_snapshot_sha256)
        return self


class Evidence(RecordIdentity):
    source_snapshot_sha256: str | None = None
    kind: Literal["log_observation", "source_fact", "model_inference", "local_execution", "application_execution"]
    producer: Literal["input", "source_reader", "model", "execution_backend"]
    artifact_sha256: str
    statement: str = Field(min_length=1)
    basis_ids: list[str] = Field(default_factory=list)
    derivation: str | None = None
    location: SourceLocation | None = None
    log_line: int | None = Field(default=None, ge=1)
    execution_id: str | None = None

    @model_validator(mode="after")
    def valid_provenance(self) -> Evidence:
        require_hash(self.artifact_sha256)
        if self.source_snapshot_sha256 is not None:
            require_hash(self.source_snapshot_sha256)
        allowed = {
            "log_observation": "input", "source_fact": "source_reader",
            "model_inference": "model", "local_execution": "execution_backend",
            "application_execution": "execution_backend",
        }
        if self.producer != allowed[self.kind]:
            raise ValueError("evidence kind and producer disagree")
        if self.kind.endswith("execution") and not self.execution_id:
            raise ValueError("execution evidence requires execution_id")
        if self.kind != "log_observation" and self.source_snapshot_sha256 is None:
            raise ValueError("non-log evidence requires a source snapshot")
        if not self.kind.endswith("execution") and self.execution_id is not None:
            raise ValueError("non-execution evidence cannot name execution_id")
        if self.location and self.location.snapshot_sha256 != self.source_snapshot_sha256:
            raise ValueError("evidence location refers to another snapshot")
        if len(set(self.basis_ids)) != len(self.basis_ids) or self.record_id in self.basis_ids:
            raise ValueError("duplicate or self-referential evidence basis")
        return self


class Hypothesis(Contract):
    hypothesis_id: str
    root_cause_locations: list[SourceLocation] = Field(default_factory=list)
    support_evidence_ids: list[str] = Field(default_factory=list)
    contradicting_evidence_ids: list[str] = Field(default_factory=list)
    support_status: Literal["supported", "contradicted", "unknown"]
    missing_evidence: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def valid_evidence_sets(self) -> Hypothesis:
        if not IDENTIFIER.fullmatch(self.hypothesis_id):
            raise ValueError("invalid hypothesis identifier")
        if set(self.support_evidence_ids) & set(self.contradicting_evidence_ids):
            raise ValueError("support and contradiction evidence overlap")
        return self


class Diagnosis(Record):
    exception_locations: list[SourceLocation] = Field(default_factory=list)
    hypotheses: list[Hypothesis] = Field(default_factory=list)
    selected_hypothesis_id: str | None = None
    unknowns: list[str] = Field(default_factory=list)


class RepairObligation(Record):
    kind: Literal["runtime_condition", "business_behavior"]
    statement: str = Field(min_length=1)
    authority: Literal["verified_requirement", "interface_rule", "existing_behavior", "model_inference"]
    source_evidence_ids: list[str] = Field(default_factory=list)
    status: Literal["satisfied", "violated", "unknown"] = "unknown"


class CandidatePatch(Record):
    candidate_id: str
    ordinal: int = Field(ge=1)
    parent_snapshot_sha256: str
    candidate_snapshot_sha256: str
    diff_sha256: str
    diagnosis_id: str | None = None
    changed_paths: list[str] = Field(min_length=1)
    restores: list[str] = Field(default_factory=list)
    preserves: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def valid_candidate(self) -> CandidatePatch:
        for value in (self.candidate_id, self.parent_snapshot_sha256, self.candidate_snapshot_sha256, self.diff_sha256):
            require_hash(value)
        if self.parent_snapshot_sha256 != self.source_snapshot_sha256:
            raise ValueError("candidate parent differs from source snapshot")
        if len(set(self.changed_paths)) != len(self.changed_paths):
            raise ValueError("duplicate changed path")
        for path in self.changed_paths:
            require_path(path)
        if set(self.restores) & set(self.preserves):
            raise ValueError("same obligation listed as restore and preserve")
        return self


def require_hash(value: str) -> None:
    if not SHA256.fullmatch(value):
        raise ValueError("expected lowercase SHA-256")


def require_path(value: str) -> None:
    path = PurePosixPath(value)
    if (not value or "\\" in value or value.startswith("/") or path.is_absolute() or
            ":" in value or any(part in ("", ".", "..") for part in value.split("/"))):
        raise ValueError("expected normalized relative POSIX path")


def admit_evidence(payload: dict[str, object], *, channel: Literal["input", "source_reader", "model", "execution_backend"]) -> Evidence:
    """Set provenance from the trusted caller, never from a model-produced field."""
    if "producer" in payload and payload["producer"] != channel:
        raise ValueError("evidence producer conflicts with trusted channel")
    return Evidence.model_validate({**payload, "producer": channel})


def validate_references(
    diagnosis: Diagnosis,
    evidence: list[Evidence],
    line_counts: dict[str, int],
) -> None:
    """Check references and line ranges; never promote an inference to an observation."""
    ids = [item.record_id for item in evidence]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate evidence identifier")
    by_id = {item.record_id: item for item in evidence}
    for item in evidence:
        if (item.task_id != diagnosis.task_id or item.run_id != diagnosis.run_id or
                (item.source_snapshot_sha256 != diagnosis.source_snapshot_sha256 and
                 not (item.kind == "log_observation" and item.source_snapshot_sha256 is None))):
            raise ValueError("evidence belongs to another task, run or snapshot")
        if any(basis not in by_id for basis in item.basis_ids):
            raise ValueError("unknown evidence basis")
        if item.location:
            _check_location(item.location, diagnosis.source_snapshot_sha256, line_counts)
    hypothesis_ids = [item.hypothesis_id for item in diagnosis.hypotheses]
    if len(hypothesis_ids) != len(set(hypothesis_ids)):
        raise ValueError("duplicate hypothesis identifier")
    if diagnosis.selected_hypothesis_id is not None and diagnosis.selected_hypothesis_id not in hypothesis_ids:
        raise ValueError("selected hypothesis does not exist")
    for location in diagnosis.exception_locations:
        _check_location(location, diagnosis.source_snapshot_sha256, line_counts)
    for hypothesis in diagnosis.hypotheses:
        for location in hypothesis.root_cause_locations:
            _check_location(location, diagnosis.source_snapshot_sha256, line_counts)
        for evidence_id in (*hypothesis.support_evidence_ids, *hypothesis.contradicting_evidence_ids):
            if evidence_id not in by_id:
                raise ValueError("hypothesis refers to missing evidence")


def _check_location(location: SourceLocation, snapshot_sha256: str, line_counts: dict[str, int]) -> None:
    if location.snapshot_sha256 != snapshot_sha256:
        raise ValueError("source location refers to another snapshot")
    if location.path not in line_counts or location.end_line > line_counts[location.path]:
        raise ValueError("source location is outside the source snapshot")
