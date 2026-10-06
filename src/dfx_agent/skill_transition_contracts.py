"""Synthetic evolution decisions and sandbox publications, never application grants."""

from __future__ import annotations

from typing import Any, Literal, cast

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
from .learning_contracts import _PortableFrozen
from .skill_evolution_contracts import (
    HostSkillVersionApproval,
    HostValidationPlanApproval,
    HostValidationPlanApprovalV2,
    VersionApproval,
    _parse,
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

    def _validate_common(self) -> HostTransitionApproval:
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

    @model_validator(mode="after")
    def valid_approval(self) -> HostTransitionApproval:
        return self._validate_common()


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

    def _validate_common(self) -> TransitionPayload:
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

    @model_validator(mode="after")
    def valid_payload(self) -> TransitionPayload:
        return self._validate_common()


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

    def _validate_common(self) -> HostPublicationApproval:
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

    @model_validator(mode="after")
    def valid_approval(self) -> HostPublicationApproval:
        return self._validate_common()


class PublicationPayload(_Frozen):
    approval: HostPublicationApproval
    version_approval: HostSkillVersionApproval
    transition_approval: HostTransitionApproval | None = None
    previous_approval: HostPublicationApproval | None = None
    sequence: int = Field(ge=1, le=MAX_EVOLUTION_EVENTS)
    projection_sha256: str
    application_qualification: Literal["none"] = "none"
    scope: Literal["synthetic_sandbox"] = "synthetic_sandbox"

    def _validate_common(self) -> PublicationPayload:
        if self.projection_sha256 != self.version_approval.projection_sha256:
            raise ValueError("sandbox publication content and version differ")
        if (self.approval.previous_publication_id is None) != (self.previous_approval is None):
            raise ValueError("publication needs its original predecessor approval")
        if (self.approval.transition_id is None) != (self.transition_approval is None):
            raise ValueError("publication needs its original transition approval")
        return self

    @model_validator(mode="after")
    def valid_payload(self) -> PublicationPayload:
        return self._validate_common()


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

    def _validate_common(self) -> FrozenSkillSelection:
        require_identifiers([self.alias])
        _identity(self.publication_id, "skill-publication-")
        _identity(self.version_id, "skill-version-")
        require_hash(self.projection_sha256)
        return self

    @model_validator(mode="after")
    def valid_selection(self) -> FrozenSkillSelection:
        return self._validate_common()


class SyntheticValidationPolicyV2(_PortableFrozen):
    scope: Literal["offline_synthetic"] = "offline_synthetic"
    comparison: Literal["same_pool_targeted_procedure"] = "same_pool_targeted_procedure"
    min_representative_pairs: int | None = Field(default=None, ge=1, le=64)
    min_boundary_pairs: int | None = Field(default=None, ge=1, le=64)
    min_net_gain: int | None = Field(default=None, ge=0, le=64)
    max_success_losses: int | None = Field(default=None, ge=0, le=64)
    max_validation_tokens: int | None = Field(default=None, gt=0)
    max_validation_calls: int | None = Field(default=None, gt=0)
    max_validation_attempts: int | None = Field(default=None, gt=0, le=MAX_EVOLUTION_EVENTS)
    max_learning_tokens: int | None = Field(default=None, gt=0)
    max_learning_calls: int | None = Field(default=None, gt=0)
    unknown_handling: Literal["pending"] = "pending"
    measured_cost_axes: Literal["synthetic_learning_and_validation_tokens_and_reserved_calls"] = "synthetic_learning_and_validation_tokens_and_reserved_calls"
    application_qualification: Literal["none"] = "none"

    def thresholds_complete(self) -> bool:
        return all(getattr(self, name) is not None for name in (
            "min_representative_pairs", "min_boundary_pairs", "min_net_gain", "max_success_losses",
            "max_validation_tokens", "max_validation_calls", "max_validation_attempts", "max_learning_tokens", "max_learning_calls"))


class HostTransitionApprovalV2(_PortableFrozen):
    approval_id: Identifier
    plan_id: str
    receipt_ids: tuple[str, ...] = Field(max_length=128)
    previous_transition_id: str | None = None

    @model_validator(mode="after")
    def valid(self) -> HostTransitionApprovalV2:
        HostTransitionApproval._validate_common(cast(HostTransitionApproval, self))
        return self


class TransitionPayloadV2(_PortableFrozen):
    approval: HostTransitionApprovalV2
    plan_approval: HostValidationPlanApprovalV2
    version_id: str
    skill_id: Identifier
    state: Literal["synthetic_pending", "synthetic_rejected", "synthetic_eligible"]
    reasons: tuple[str, ...]
    facts_json: str
    history_receipt_ids: tuple[str, ...]
    attempt_plan_ids: tuple[str, ...]
    previous_approval: HostTransitionApprovalV2 | None = None
    learning_costs_json: str
    application_qualification: Literal["none"] = "none"
    learning_model_call_status: Literal["observed"] = "observed"
    learning_monetary_cost: None = None

    @model_validator(mode="after")
    def valid(self) -> TransitionPayloadV2:
        TransitionPayload._validate_common(cast(TransitionPayload, self))
        if _object(self.learning_costs_json).get("status") != "observed":
            raise ValueError("model transition must retain its observed learning source costs")
        return self


class SkillTransitionV2(_PortableFrozen):
    transition_id: str
    content_sha256: str
    payload_json: str

    @model_validator(mode="after")
    def valid(self) -> SkillTransitionV2:
        payload = TransitionPayloadV2.model_validate_json(encode(_object(self.payload_json)))
        if (encode(payload) != self.payload_json or content_hash(payload) != self.content_sha256 or
                self.transition_id != "skill-transition-" + self.content_sha256):
            raise ValueError("model Skill transition identity differs")
        return self

    def contents(self) -> dict[str, Any]:
        return _object(self.payload_json)


class HostPublicationApprovalV2(_PortableFrozen):
    approval_id: Identifier
    alias: Identifier
    action: Literal["activate", "rollback"]
    version_id: str
    transition_id: str | None = None
    previous_publication_id: str
    split: KnowledgeSplit = "development"
    scope: Literal["synthetic_sandbox"] = "synthetic_sandbox"

    @model_validator(mode="after")
    def valid(self) -> HostPublicationApprovalV2:
        HostPublicationApproval._validate_common(cast(HostPublicationApproval, self))
        return self


PublicationApproval = HostPublicationApproval | HostPublicationApprovalV2


def parse_publication_approval(raw: str | dict) -> PublicationApproval:
    return _parse(raw, HostPublicationApproval, HostPublicationApprovalV2)


def parse_transition_approval(raw: str | dict) -> HostTransitionApproval | HostTransitionApprovalV2:
    return _parse(raw, HostTransitionApproval, HostTransitionApprovalV2)


class PublicationPayloadV2(_PortableFrozen):
    approval: HostPublicationApprovalV2
    version_approval: VersionApproval
    transition_approval: HostTransitionApprovalV2 | None = None
    previous_approval: PublicationApproval
    sequence: int = Field(ge=1, le=MAX_EVOLUTION_EVENTS)
    projection_sha256: str
    application_qualification: Literal["none"] = "none"
    scope: Literal["synthetic_sandbox"] = "synthetic_sandbox"

    @model_validator(mode="after")
    def valid(self) -> PublicationPayloadV2:
        PublicationPayload._validate_common(cast(PublicationPayload, self))
        return self


class SkillPublicationV2(_PortableFrozen):
    publication_id: str
    content_sha256: str
    payload_json: str

    @model_validator(mode="after")
    def valid(self) -> SkillPublicationV2:
        payload = PublicationPayloadV2.model_validate_json(encode(_object(self.payload_json)))
        if (encode(payload) != self.payload_json or content_hash(payload) != self.content_sha256 or
                self.publication_id != "skill-publication-" + self.content_sha256):
            raise ValueError("model Skill publication identity differs")
        return self

    def contents(self) -> dict[str, Any]:
        return _object(self.payload_json)


class FrozenSkillSelectionV2(_PortableFrozen):
    alias: Identifier
    publication_id: str
    version_id: str
    projection_sha256: str
    split: KnowledgeSplit
    scope: Literal["synthetic_sandbox"] = "synthetic_sandbox"
    application_qualification: Literal["none"] = "none"

    @model_validator(mode="after")
    def valid(self) -> FrozenSkillSelectionV2:
        FrozenSkillSelection._validate_common(cast(FrozenSkillSelection, self))
        return self


TransitionApproval = HostTransitionApproval | HostTransitionApprovalV2
TransitionBody = TransitionPayload | TransitionPayloadV2
TransitionRecord = SkillTransition | SkillTransitionV2
PublicationBody = PublicationPayload | PublicationPayloadV2
PublicationRecord = SkillPublication | SkillPublicationV2
SkillSelection = FrozenSkillSelection | FrozenSkillSelectionV2


def parse_transition(raw: str | dict) -> TransitionRecord:
    return _parse(raw, SkillTransition, SkillTransitionV2)


def parse_transition_payload(raw: str | dict) -> TransitionBody:
    return _parse(raw, TransitionPayload, TransitionPayloadV2)


def parse_publication(raw: str | dict) -> PublicationRecord:
    return _parse(raw, SkillPublication, SkillPublicationV2)


def parse_publication_payload(raw: str | dict) -> PublicationBody:
    return _parse(raw, PublicationPayload, PublicationPayloadV2)


def parse_selection(raw: str | dict) -> SkillSelection:
    return _parse(raw, FrozenSkillSelection, FrozenSkillSelectionV2)


def parse_synthetic_policy(raw: str | dict) -> SyntheticValidationPolicy | SyntheticValidationPolicyV2:
    return _parse(raw, SyntheticValidationPolicy, SyntheticValidationPolicyV2)
