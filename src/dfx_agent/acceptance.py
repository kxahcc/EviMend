"""Candidate-neutral application acceptance contract and conservative evidence gate.

This module only checks records supplied by trusted preparation and execution
boundaries. It does not run an application or turn synthetic results into proof.
"""

from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .contracts import IDENTIFIER, require_hash

CheckRole = Literal["build", "target_replay", "behavior", "regression"]
VerdictStatus = Literal["pass", "fail", "not_run", "blocked", "inconclusive"]


class ProtocolCheck(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    check_id: str
    role: CheckRole
    scenario_id: str
    oracle_origin: Literal["verified_requirement", "interface_rule", "existing_behavior",
                           "independent_test", "model_inference", "construction_test"]
    oracle_ref_sha256: str | None = None
    visibility: Literal["solver_visible", "evaluator_only"]
    required_capability: Literal["build", "device_replay", "behavior_test", "regression_test"]
    timeout_seconds: int = Field(ge=1, le=3600)
    required: bool
    repetitions: int = Field(ge=1, le=20)

    @model_validator(mode="after")
    def valid_check(self) -> ProtocolCheck:
        if not IDENTIFIER.fullmatch(self.check_id) or not IDENTIFIER.fullmatch(self.scenario_id):
            raise ValueError("invalid check or scenario identifier")
        if self.oracle_ref_sha256 is not None:
            require_hash(self.oracle_ref_sha256)
        if self.required and (self.oracle_origin in ("model_inference", "construction_test") or
                              self.oracle_ref_sha256 is None):
            raise ValueError("required acceptance check needs an independent oracle")
        capability = {"build": "build", "target_replay": "device_replay",
                      "behavior": "behavior_test", "regression": "regression_test"}
        if self.required_capability != capability[self.role]:
            raise ValueError("check role and required capability disagree")
        return self


class AcceptanceProtocol(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    schema_version: Literal[1] = 1
    task_id: str
    lineage_group_id: str
    fault_variant_id: str
    crash_log_sha256: str
    target_fault_signature_sha256: str
    original_analysis_sha256: str
    original_runtime_sha256: str
    environment_id: str
    initial_state_id: str
    checks: tuple[ProtocolCheck, ...] = Field(min_length=1)
    producer: Literal["evaluation_preparer"]

    @field_validator("checks", mode="before")
    @classmethod
    def json_checks(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value

    @model_validator(mode="after")
    def valid_protocol(self) -> AcceptanceProtocol:
        for value in (self.task_id, self.lineage_group_id, self.fault_variant_id,
                      self.environment_id, self.initial_state_id):
            if not IDENTIFIER.fullmatch(value):
                raise ValueError("invalid protocol identifier")
        require_hash(self.crash_log_sha256)
        require_hash(self.target_fault_signature_sha256)
        require_hash(self.original_analysis_sha256)
        require_hash(self.original_runtime_sha256)
        ids = [check.check_id for check in self.checks]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate protocol check")
        return self

    def sha256(self) -> str:
        canonical = json.dumps(self.model_dump(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()


def admit_protocol(payload: dict[str, object], *, channel: str) -> AcceptanceProtocol:
    """Keep model-authored checks separate from the independent common protocol."""
    if channel != "evaluation_preparer" or ("producer" in payload and payload["producer"] != channel):
        raise ValueError("untrusted acceptance protocol producer")
    return AcceptanceProtocol.model_validate({**payload, "producer": channel})


class CandidateRuntimeBinding(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    task_id: str
    candidate_id: str
    parent_analysis_sha256: str
    candidate_analysis_sha256: str
    diff_sha256: str
    runtime_source_sha256: str
    shared_sources_manifest_sha256: str
    packaging_manifest_sha256: str
    producer: Literal["trusted_packager"]

    @model_validator(mode="after")
    def valid_binding(self) -> CandidateRuntimeBinding:
        if not IDENTIFIER.fullmatch(self.task_id):
            raise ValueError("invalid binding task")
        for value in (self.candidate_id, self.parent_analysis_sha256,
                      self.candidate_analysis_sha256, self.diff_sha256,
                      self.runtime_source_sha256, self.shared_sources_manifest_sha256,
                      self.packaging_manifest_sha256):
            require_hash(value)
        expected = hashlib.sha256((self.parent_analysis_sha256 + self.candidate_analysis_sha256 +
                                   self.diff_sha256).encode()).hexdigest()
        if self.candidate_id != expected:
            raise ValueError("candidate identity does not match the frozen diff")
        return self


def admit_runtime_binding(payload: dict[str, object], *, channel: str) -> CandidateRuntimeBinding:
    if channel != "trusted_packager" or ("producer" in payload and payload["producer"] != channel):
        raise ValueError("untrusted runtime binding producer")
    return CandidateRuntimeBinding.model_validate({**payload, "producer": channel})


class VerificationVerdict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    task_id: str
    check_id: str
    protocol_sha256: str
    run_role: Literal["original", "candidate"]
    candidate_id: str | None = None
    runtime_source_sha256: str
    environment_id: str
    initial_state_id: str
    repetition: int = Field(ge=0)
    status: VerdictStatus
    failure_kind: Literal["target_fault", "build_error", "behavior_mismatch", "regression",
                          "environment", "timeout", "resource_limit", "other"] | None = None
    observed_fault_signature_sha256: str | None = None
    scenario_reached: bool | None = None
    execution_id: str | None = None
    artifact_sha256: str | None = None
    duration_ms: int | None = Field(default=None, ge=0)
    scope: Literal["synthetic", "application"]
    producer: Literal["execution_backend"]

    @model_validator(mode="after")
    def valid_verdict(self) -> VerificationVerdict:
        for value in (self.task_id, self.check_id, self.environment_id, self.initial_state_id):
            if not IDENTIFIER.fullmatch(value):
                raise ValueError("invalid verdict identifier")
        require_hash(self.protocol_sha256)
        require_hash(self.runtime_source_sha256)
        if self.artifact_sha256 is not None:
            require_hash(self.artifact_sha256)
        if self.observed_fault_signature_sha256 is not None:
            require_hash(self.observed_fault_signature_sha256)
        if self.run_role == "original" and self.candidate_id is not None:
            raise ValueError("original run cannot name a candidate")
        if self.run_role == "candidate":
            if self.candidate_id is None:
                raise ValueError("candidate verdict needs an identity")
            require_hash(self.candidate_id)
        if self.execution_id is not None and not IDENTIFIER.fullmatch(self.execution_id):
            raise ValueError("invalid execution identifier")
        if self.status in ("pass", "fail"):
            if self.execution_id is None or self.artifact_sha256 is None or self.duration_ms is None:
                raise ValueError("executed verdict needs execution evidence")
            if self.status == "pass" and self.failure_kind is not None:
                raise ValueError("pass cannot carry a failure kind")
            if self.status == "fail" and self.failure_kind is None:
                raise ValueError("fail needs a failure kind")
            if self.failure_kind == "target_fault" and self.observed_fault_signature_sha256 is None:
                raise ValueError("target fault needs an observed signature")
        if self.status == "not_run" and (self.execution_id is not None or self.artifact_sha256 is not None or
                                          self.duration_ms is not None):
            raise ValueError("not_run cannot carry execution evidence")
        return self


def admit_verdict(payload: dict[str, object], *, channel: str) -> VerificationVerdict:
    """Only a trusted execution adapter may label a result as execution evidence."""
    if channel != "execution_backend" or ("producer" in payload and payload["producer"] != channel):
        raise ValueError("untrusted verification producer")
    return VerificationVerdict.model_validate({**payload, "producer": channel})


class AcceptanceDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    status: Literal["verified_under_protocol", "not_verified"]
    protocol_sha256: str
    candidate_id: str
    reasons: tuple[str, ...]


def evaluate_acceptance(protocol: AcceptanceProtocol, binding: CandidateRuntimeBinding,
                        verdicts: list[VerificationVerdict]) -> AcceptanceDecision:
    """Require original-fault evidence and all common candidate checks at fixed identities."""
    protocol_sha = protocol.sha256()
    reasons: list[str] = []
    if binding.task_id != protocol.task_id or binding.parent_analysis_sha256 != protocol.original_analysis_sha256:
        reasons.append("candidate_source_lineage_mismatch")
    expected_candidate_id = hashlib.sha256((binding.parent_analysis_sha256 + binding.candidate_analysis_sha256 +
                                            binding.diff_sha256).encode()).hexdigest()
    if binding.candidate_id != expected_candidate_id:
        reasons.append("candidate_identity_mismatch")
    required = [check for check in protocol.checks if check.required]
    roles = {check.role for check in required}
    for role in ("build", "target_replay", "behavior", "regression"):
        if role not in roles:
            reasons.append(f"protocol_missing_{role}")
    index: dict[tuple[str, str, int], VerificationVerdict] = {}
    for item in verdicts:
        key = (item.check_id, item.run_role, item.repetition)
        if key in index:
            reasons.append(f"duplicate_verdict:{item.check_id}:{item.run_role}:{item.repetition}")
        else:
            index[key] = item
    for check in required:
        for repetition in range(check.repetitions):
            for run_role in ("original", "candidate"):
                label = f"{check.check_id}:{run_role}:{repetition}"
                item = index.get((check.check_id, run_role, repetition))
                if item is None:
                    reasons.append(f"missing_verdict:{label}")
                    continue
                runtime_sha = (protocol.original_runtime_sha256 if run_role == "original"
                               else binding.runtime_source_sha256)
                candidate_id = None if run_role == "original" else binding.candidate_id
                if (item.task_id != protocol.task_id or item.protocol_sha256 != protocol_sha or
                        item.runtime_source_sha256 != runtime_sha or item.candidate_id != candidate_id or
                        item.environment_id != protocol.environment_id or
                        item.initial_state_id != protocol.initial_state_id):
                    reasons.append(f"identity_mismatch:{label}")
                    continue
                if item.scope != "application":
                    reasons.append(f"non_application_evidence:{label}")
                    continue
                if run_role == "original" and check.role == "target_replay":
                    if (item.status != "fail" or item.failure_kind != "target_fault" or
                            item.observed_fault_signature_sha256 != protocol.target_fault_signature_sha256 or
                            item.scenario_reached is not True):
                        reasons.append(f"original_fault_not_reproduced:{label}")
                elif item.status != "pass" or (check.role != "build" and item.scenario_reached is not True):
                    reasons.append(f"required_check_not_passed:{label}")
    return AcceptanceDecision(status="not_verified" if reasons else "verified_under_protocol",
                              protocol_sha256=protocol_sha, candidate_id=binding.candidate_id,
                              reasons=tuple(reasons))
