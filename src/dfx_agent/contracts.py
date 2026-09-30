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
    evaluated_candidate_id: str | None = None
    execution_snapshot_sha256: str | None = None
    execution_scope: Literal["synthetic", "application"] | None = None

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
        if any(value is not None for value in (self.evaluated_candidate_id, self.execution_snapshot_sha256,
                                               self.execution_scope)):
            if not self.kind.endswith("execution") or not all((self.evaluated_candidate_id,
                                                               self.execution_snapshot_sha256, self.execution_scope)):
                raise ValueError("candidate execution evidence needs complete version and scope")
            require_hash(self.evaluated_candidate_id or "")
            require_hash(self.execution_snapshot_sha256 or "")
        if self.location and self.location.snapshot_sha256 != self.source_snapshot_sha256:
            raise ValueError("evidence location refers to another snapshot")
        if len(set(self.basis_ids)) != len(self.basis_ids) or self.record_id in self.basis_ids:
            raise ValueError("duplicate or self-referential evidence basis")
        return self


class ValueBinding(Contract):
    binding_id: str
    expression: str = Field(min_length=1)
    role: Literal["value", "state", "unit", "operation"]
    location: SourceLocation | None = None
    evidence_ids: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def valid_binding(self) -> ValueBinding:
        require_identifiers([self.binding_id, *self.evidence_ids])
        require_unique(self.evidence_ids)
        return self


class RuntimeConstraint(Contract):
    family: str
    operation: SourceLocation
    bindings: list[ValueBinding] = Field(min_length=1)
    condition: str = Field(min_length=1)
    semantic_source_evidence_ids: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def valid_constraint(self) -> RuntimeConstraint:
        require_identifiers([self.family, *self.semantic_source_evidence_ids])
        require_unique([item.binding_id for item in self.bindings])
        require_unique(self.semantic_source_evidence_ids)
        return self


class CausalStep(Contract):
    step_id: str
    from_binding_id: str
    to_binding_id: str
    relation: str = Field(min_length=1)
    authority: Literal["source_fact", "model_inference"]
    evidence_ids: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def valid_step(self) -> CausalStep:
        require_identifiers([self.step_id, self.from_binding_id, self.to_binding_id, *self.evidence_ids])
        require_unique(self.evidence_ids)
        return self


class DiagnosticQuestion(Contract):
    question: str = Field(min_length=1)
    observation_routes: dict[str, str]
    unknown_route: Literal["retain_unknown"] = "retain_unknown"

    @model_validator(mode="after")
    def valid_question(self) -> DiagnosticQuestion:
        if len(set(self.observation_routes.values())) < 2 or any(not key.strip() for key in self.observation_routes):
            raise ValueError("question must distinguish at least two hypotheses")
        require_identifiers(list(self.observation_routes.values()))
        return self


class Hypothesis(Contract):
    hypothesis_id: str
    root_cause_locations: list[SourceLocation] = Field(default_factory=list)
    support_evidence_ids: list[str] = Field(default_factory=list)
    contradicting_evidence_ids: list[str] = Field(default_factory=list)
    support_status: Literal["supported", "contradicted", "unknown"]
    missing_evidence: list[str] = Field(default_factory=list)
    constraint: RuntimeConstraint | None = None
    causal_steps: list[CausalStep] = Field(default_factory=list)
    obligation_ids: list[str] = Field(default_factory=list)
    distinguishing_check: DiagnosticQuestion | None = None

    @model_validator(mode="after")
    def valid_evidence_sets(self) -> Hypothesis:
        if not IDENTIFIER.fullmatch(self.hypothesis_id):
            raise ValueError("invalid hypothesis identifier")
        if set(self.support_evidence_ids) & set(self.contradicting_evidence_ids):
            raise ValueError("support and contradiction evidence overlap")
        for values in (self.support_evidence_ids, self.contradicting_evidence_ids, self.obligation_ids):
            require_identifiers(values)
            require_unique(values)
        require_unique([item.step_id for item in self.causal_steps])
        bindings = {item.binding_id for item in self.constraint.bindings} if self.constraint else set()
        if any(item.from_binding_id not in bindings or item.to_binding_id not in bindings for item in self.causal_steps):
            raise ValueError("causal step names an absent binding")
        return self


class Diagnosis(Record):
    exception_locations: list[SourceLocation] = Field(default_factory=list)
    hypotheses: list[Hypothesis] = Field(default_factory=list)
    selected_hypothesis_id: str | None = None
    unknowns: list[str] = Field(default_factory=list)
    feedback_candidate_id: str | None = None

    @model_validator(mode="after")
    def valid_feedback_candidate(self) -> Diagnosis:
        if self.feedback_candidate_id is not None:
            require_hash(self.feedback_candidate_id)
        return self


class RepairObligation(Record):
    kind: Literal["runtime_condition", "business_behavior"]
    statement: str = Field(min_length=1)
    authority: Literal["verified_requirement", "interface_rule", "existing_behavior", "model_inference"]
    source_evidence_ids: list[str] = Field(default_factory=list)
    status: Literal["satisfied", "violated", "unknown"] = "unknown"
    bindings: list[ValueBinding] = Field(default_factory=list)
    hypothesis_ids: list[str] = Field(default_factory=list)
    checkability: Literal["executable", "manual", "unknown"] = "unknown"
    scope: str = Field(default="unspecified", min_length=1)

    @model_validator(mode="after")
    def valid_obligation(self) -> RepairObligation:
        for values in (self.source_evidence_ids, self.hypothesis_ids):
            require_identifiers(values)
            require_unique(values)
        require_unique([item.binding_id for item in self.bindings])
        return self


class CandidatePatch(Record):
    candidate_id: str
    ordinal: int = Field(ge=1)
    parent_snapshot_sha256: str
    candidate_snapshot_sha256: str
    diff_sha256: str
    diagnosis_id: str | None = None
    decision_id: str | None = None
    direction_id: str | None = None
    changed_paths: list[str] = Field(min_length=1)
    restores: list[str] = Field(default_factory=list)
    preserves: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def valid_candidate(self) -> CandidatePatch:
        if (self.decision_id is None) != (self.direction_id is None):
            raise ValueError("candidate decision and direction must be supplied together")
        require_identifiers([value for value in (self.diagnosis_id, self.decision_id, self.direction_id) if value is not None])
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
        if any(not IDENTIFIER.fullmatch(value) for value in (*self.restores, *self.preserves)):
            raise ValueError("invalid candidate obligation identifier")
        if len(set(self.restores)) != len(self.restores) or len(set(self.preserves)) != len(self.preserves):
            raise ValueError("duplicate candidate obligation")
        return self


class ActionProposal(Record):
    """Model-authored intent; validation never implies that an action was executed."""

    action: Literal["inspect_existing", "record_analysis", "diagnostic_probe", "edit_draft",
                    "register_candidate", "run_verification", "finish"]
    target_ids: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    candidate_id: str | None = None
    reason: str = Field(min_length=1)
    required_capability: Literal["read_source", "read_log", "record_analysis", "diagnostic_execution",
                                 "edit_source", "register_candidate", "verify_candidate", "finish"]
    observation_routes: dict[str, str] = Field(default_factory=dict)
    unknown_route: Literal["retain_unknown"] | None = None
    estimated_cost_basis: str = Field(min_length=1)

    @model_validator(mode="after")
    def valid_action(self) -> ActionProposal:
        if any(not IDENTIFIER.fullmatch(value) for value in (*self.target_ids, *self.evidence_ids)):
            raise ValueError("invalid proposal reference")
        if self.candidate_id is not None:
            require_hash(self.candidate_id)
        required = {
            "diagnostic_probe": {"diagnostic_execution"},
            "edit_draft": {"edit_source"},
            "register_candidate": {"register_candidate"},
            "run_verification": {"verify_candidate"},
            "finish": {"finish"},
            "inspect_existing": {"read_source", "read_log"},
            "record_analysis": {"record_analysis"},
        }
        if self.required_capability not in required[self.action]:
            raise ValueError("action and capability disagree")
        if self.action in ("run_verification", "finish") and self.candidate_id is None:
            raise ValueError("action needs a registered candidate")
        if self.action == "diagnostic_probe":
            if (len(self.target_ids) < 2 or len(self.observation_routes) < 2 or
                    len(set(self.observation_routes.values())) < 2 or
                    any(value not in self.target_ids for value in self.observation_routes.values()) or
                    self.unknown_route != "retain_unknown"):
                raise ValueError("probe has no complete distinguishing routes")
        elif self.observation_routes or self.unknown_route is not None:
            raise ValueError("only a diagnostic probe may predict observation routes")
        return self


AuthorizationReason = Literal["allowed", "wrong_task_or_version", "unknown_reference", "capability_unavailable",
                              "candidate_not_registered", "acceptance_not_verified", "budget_exhausted"]


class ActionAuthorization(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    proposal_id: str
    allowed: bool
    reason: AuthorizationReason


def authorize_action(proposal: ActionProposal, *, task_id: str, run_id: str, source_snapshot_sha256: str,
                     known_evidence_ids: set[str], known_target_ids: set[str],
                     registered_candidate_ids: set[str], accepted_candidate_ids: set[str],
                     capabilities: set[str],
                     remaining_model_calls: int, remaining_probe_budget: int,
                     model_response_reserved: bool = False) -> ActionAuthorization:
    """Host-owned gate for a proposal; the model cannot grant capabilities or budget."""
    reason: AuthorizationReason
    if (proposal.task_id != task_id or proposal.run_id != run_id or
            proposal.source_snapshot_sha256 != source_snapshot_sha256):
        reason = "wrong_task_or_version"
    elif (not set(proposal.evidence_ids) <= known_evidence_ids or
          not set(proposal.target_ids) <= known_target_ids):
        reason = "unknown_reference"
    elif proposal.candidate_id is not None and proposal.candidate_id not in registered_candidate_ids:
        reason = "candidate_not_registered"
    elif proposal.action == "finish" and proposal.candidate_id not in accepted_candidate_ids:
        reason = "acceptance_not_verified"
    elif proposal.required_capability not in capabilities:
        reason = "capability_unavailable"
    elif ((proposal.action != "finish" and remaining_model_calls <= 0 and not model_response_reserved) or
          (proposal.action == "diagnostic_probe" and remaining_probe_budget <= 0)):
        reason = "budget_exhausted"
    else:
        reason = "allowed"
    return ActionAuthorization(proposal_id=proposal.record_id, allowed=reason == "allowed", reason=reason)


def require_hash(value: str) -> None:
    if not SHA256.fullmatch(value):
        raise ValueError("expected lowercase SHA-256")


def require_identifiers(values: list[str]) -> None:
    if any(not IDENTIFIER.fullmatch(value) for value in values):
        raise ValueError("invalid record reference")


def require_unique(values: list[str]) -> None:
    if len(values) != len(set(values)):
        raise ValueError("duplicate record reference")


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
    obligations: list[RepairObligation] | None = None,
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
        if item.evaluated_candidate_id is not None and item.evaluated_candidate_id != diagnosis.feedback_candidate_id:
            raise ValueError("execution evidence belongs to another feedback candidate")
        if item.location:
            _check_location(item.location, diagnosis.source_snapshot_sha256, line_counts)
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(identifier: str) -> None:
        if identifier in visiting:
            raise ValueError("cyclic evidence basis")
        if identifier in visited:
            return
        visiting.add(identifier)
        for basis in by_id[identifier].basis_ids:
            visit(basis)
        visiting.remove(identifier)
        visited.add(identifier)

    for identifier in by_id:
        visit(identifier)
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
        constraint = hypothesis.constraint
        if constraint is not None:
            _check_location(constraint.operation, diagnosis.source_snapshot_sha256, line_counts)
            _check_evidence_ids(constraint.semantic_source_evidence_ids, by_id)
            _check_bindings(constraint.bindings, diagnosis.source_snapshot_sha256, line_counts, by_id)
        for step in hypothesis.causal_steps:
            _check_evidence_ids(step.evidence_ids, by_id)
            if step.authority == "source_fact" and any(by_id[value].kind != "source_fact" for value in step.evidence_ids):
                raise ValueError("causal source fact relies on non-source evidence")
        if hypothesis.distinguishing_check and not set(hypothesis.distinguishing_check.observation_routes.values()) <= set(hypothesis_ids):
            raise ValueError("question routes to an absent hypothesis")
    obligation_ids = [item.record_id for item in obligations or []]
    require_unique(obligation_ids)
    for obligation in obligations or []:
        if (obligation.task_id != diagnosis.task_id or obligation.run_id != diagnosis.run_id or
                obligation.source_snapshot_sha256 != diagnosis.source_snapshot_sha256):
            raise ValueError("obligation belongs to another task, run or snapshot")
        _check_evidence_ids(obligation.source_evidence_ids, by_id)
        _check_bindings(obligation.bindings, diagnosis.source_snapshot_sha256, line_counts, by_id)
        if not set(obligation.hypothesis_ids) <= set(hypothesis_ids):
            raise ValueError("obligation refers to an absent hypothesis")
    for hypothesis in diagnosis.hypotheses:
        if not set(hypothesis.obligation_ids) <= set(obligation_ids):
            raise ValueError("hypothesis refers to a missing obligation")
        for obligation in obligations or []:
            if ((obligation.record_id in hypothesis.obligation_ids) !=
                    (hypothesis.hypothesis_id in obligation.hypothesis_ids)):
                raise ValueError("hypothesis and obligation links disagree")


def _check_evidence_ids(values: list[str], by_id: dict[str, Evidence]) -> None:
    if any(value not in by_id for value in values):
        raise ValueError("missing evidence reference")


def _check_bindings(bindings: list[ValueBinding], snapshot: str, line_counts: dict[str, int],
                    by_id: dict[str, Evidence]) -> None:
    for binding in bindings:
        _check_evidence_ids(binding.evidence_ids, by_id)
        if binding.location is not None:
            _check_location(binding.location, snapshot, line_counts)


def _check_location(location: SourceLocation, snapshot_sha256: str, line_counts: dict[str, int]) -> None:
    if location.snapshot_sha256 != snapshot_sha256:
        raise ValueError("source location refers to another snapshot")
    if location.path not in line_counts or location.end_line > line_counts[location.path]:
        raise ValueError("source location is outside the source snapshot")
