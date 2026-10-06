"""Synthetic evolution decisions and sandbox publications, never application grants."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field, model_validator

from .contracts import require_hash, require_identifiers, require_unique
from .feedback_contracts import (
    Identifier,
    _Frozen,
    _identity,
    _object,
    content_hash,
    encode,
)
from .knowledge_contracts import KnowledgeSplit
from .skill_evolution_contracts import (
    HostSkillVersionApproval,
    HostValidationPlanApproval,
)

MAX_EVOLUTION_EVENTS = 64


class SyntheticValidationPolicy(_Frozen):
    scope: Literal["offline_synthetic"] = "offline_synthetic"
    comparison: Literal["same_pool_targeted_procedure"] = "same_pool_targeted_procedure"
    min_representative_pairs: int | None = Field(default=None, ge=1, le=64)
    min_boundary_pairs: int | None = Field(default=None, ge=1, le=64)
    min_net_gain: int | None = Field(default=None, ge=0, le=64)
    max_success_losses: int | None = Field(default=None, ge=0, le=64)
    max_validation_tokens: int | None = Field(default=None, gt=0)
    max_validation_calls: int | None = Field(default=None, gt=0)
    max_validation_attempts: int | None = Field(default=None, gt=0, le=MAX_EVOLUTION_EVENTS)
    unknown_handling: Literal["pending"] = "pending"
    measured_cost_axes: Literal["synthetic_tokens_and_reserved_calls"] = "synthetic_tokens_and_reserved_calls"
    application_qualification: Literal["none"] = "none"

    def thresholds_complete(self) -> bool:
        return all(getattr(self, name) is not None for name in (
            "min_representative_pairs", "min_boundary_pairs", "min_net_gain", "max_success_losses",
            "max_validation_tokens", "max_validation_calls", "max_validation_attempts",
        ))


class HostTransitionApproval(_Frozen):
    approval_id: Identifier
    plan_id: str
    receipt_ids: tuple[str, ...] = Field(max_length=128)
    previous_transition_id: str | None = None

    @model_validator(mode="after")
    def valid_approval(self) -> HostTransitionApproval:
        require_identifiers([self.approval_id])
        _identity(self.plan_id, "skill-plan-")
        for identifier in self.receipt_ids:
            _identity(identifier, "skill-receipt-")
        if list(self.receipt_ids) != sorted(self.receipt_ids):
            raise ValueError("transition receipt identities must have canonical order")
        require_unique(list(self.receipt_ids))
        if self.previous_transition_id is not None:
            _identity(self.previous_transition_id, "skill-transition-")
        return self


class TransitionPayload(_Frozen):
    approval: HostTransitionApproval
    plan_approval: HostValidationPlanApproval
    version_id: str
    skill_id: Identifier
    state: Literal["synthetic_pending", "synthetic_rejected", "synthetic_eligible"]
    reasons: tuple[str, ...]
    facts_json: str
    history_receipt_ids: tuple[str, ...]
    attempt_plan_ids: tuple[str, ...]
    previous_approval: HostTransitionApproval | None = None
    application_qualification: Literal["none"] = "none"
    learning_model_call_status: Literal["not_run"] = "not_run"
    learning_tokens: None = None
    learning_monetary_cost: None = None

    @model_validator(mode="after")
    def valid_payload(self) -> TransitionPayload:
        _identity(self.version_id, "skill-version-")
        require_identifiers([self.skill_id])
        _object(self.facts_json)
        if (self.approval.previous_transition_id is None) != (self.previous_approval is None):
            raise ValueError("transition needs its original predecessor approval")
        for value in self.history_receipt_ids:
            _identity(value, "skill-receipt-")
        for value in self.attempt_plan_ids:
            _identity(value, "skill-plan-")
        require_unique(list(self.history_receipt_ids))
        require_unique(list(self.attempt_plan_ids))
        if len(self.attempt_plan_ids) > MAX_EVOLUTION_EVENTS:
            raise ValueError("evolution history exceeds its capacity")
        return self


class SkillTransition(_Frozen):
    transition_id: str
    content_sha256: str
    payload_json: str

    @model_validator(mode="after")
    def valid_record(self) -> SkillTransition:
        payload = TransitionPayload.model_validate_json(encode(_object(self.payload_json)))
        if (encode(payload) != self.payload_json or content_hash(payload) != self.content_sha256 or
                self.transition_id != "skill-transition-" + self.content_sha256):
            raise ValueError("Skill transition content identity changed")
        return self

    def contents(self) -> dict[str, Any]:
        return _object(self.payload_json)


class HostPublicationApproval(_Frozen):
    approval_id: Identifier
    alias: Identifier
    action: Literal["initialize_seed", "activate", "rollback"]
    version_id: str
    transition_id: str | None = None
    previous_publication_id: str | None = None
    split: KnowledgeSplit = "development"
    scope: Literal["synthetic_sandbox"] = "synthetic_sandbox"

    @model_validator(mode="after")
    def valid_approval(self) -> HostPublicationApproval:
        require_identifiers([self.approval_id, self.alias])
        _identity(self.version_id, "skill-version-")
        if self.transition_id is not None:
            _identity(self.transition_id, "skill-transition-")
        if self.previous_publication_id is not None:
            _identity(self.previous_publication_id, "skill-publication-")
        if self.action == "initialize_seed":
            if self.transition_id is not None or self.previous_publication_id is not None:
                raise ValueError("seed initialization cannot acquire a transition or predecessor")
        elif self.previous_publication_id is None:
            raise ValueError("sandbox update requires its expected previous publication")
        if (self.action == "activate") != (self.transition_id is not None):
            raise ValueError("only activation requires a qualifying transition")
        return self


class PublicationPayload(_Frozen):
    approval: HostPublicationApproval
    version_approval: HostSkillVersionApproval
    transition_approval: HostTransitionApproval | None = None
    previous_approval: HostPublicationApproval | None = None
    sequence: int = Field(ge=1, le=MAX_EVOLUTION_EVENTS)
    projection_sha256: str
    application_qualification: Literal["none"] = "none"
    scope: Literal["synthetic_sandbox"] = "synthetic_sandbox"

    @model_validator(mode="after")
    def valid_payload(self) -> PublicationPayload:
        if self.projection_sha256 != self.version_approval.projection_sha256:
            raise ValueError("sandbox publication content and version differ")
        if (self.approval.previous_publication_id is None) != (self.previous_approval is None):
            raise ValueError("publication needs its original predecessor approval")
        if (self.approval.transition_id is None) != (self.transition_approval is None):
            raise ValueError("publication needs its original transition approval")
        return self


class SkillPublication(_Frozen):
    publication_id: str
    content_sha256: str
    payload_json: str

    @model_validator(mode="after")
    def valid_record(self) -> SkillPublication:
        payload = PublicationPayload.model_validate_json(encode(_object(self.payload_json)))
        if (encode(payload) != self.payload_json or content_hash(payload) != self.content_sha256 or
                self.publication_id != "skill-publication-" + self.content_sha256):
            raise ValueError("Skill publication content identity changed")
        return self

    def contents(self) -> dict[str, Any]:
        return _object(self.payload_json)


class FrozenSkillSelection(_Frozen):
    alias: Identifier
    publication_id: str
    version_id: str
    projection_sha256: str
    split: KnowledgeSplit
    scope: Literal["synthetic_sandbox"] = "synthetic_sandbox"
    application_qualification: Literal["none"] = "none"

    @model_validator(mode="after")
    def valid_selection(self) -> FrozenSkillSelection:
        require_identifiers([self.alias])
        _identity(self.publication_id, "skill-publication-")
        _identity(self.version_id, "skill-version-")
        require_hash(self.projection_sha256)
        return self
