"""Host-owned, separately metered learning; no legacy candidate migration.

These records retain declared permission and inferred guidance. They do not
approve a Skill, establish semantic applicability or qualify an application.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .contracts import require_hash, require_identifiers, require_unique
from .feedback_contracts import (
    FeedbackCapsule,
    FeedbackFile,
    HostFeedbackApproval,
    Identifier,
    _Frozen,
    _identity,
    _object,
    _reference,
    content_hash,
    encode,
)
from .knowledge_contracts import KnowledgeSplit, SolverProjection
from .model_calls import ModelCallIntentV2
from .model_provider import PreparedRequest, ProviderSettings


class HostLearningApproval(_Frozen):
    approval_id: Identifier
    operation_id: Identifier
    run_id: Identifier
    capsule_id: str
    root_binding_sha256: str
    feedback_approval: HostFeedbackApproval
    provider_settings: ProviderSettings
    max_calls: int = Field(gt=0, le=128)
    max_known_tokens: int = Field(gt=0)
    origin_split: Literal["development"] = "development"
    scope: Literal["offline_synthetic"] = "offline_synthetic"
    application_qualification: Literal["none"] = "none"

    @model_validator(mode="after")
    def valid(self) -> HostLearningApproval:
        require_identifiers([self.approval_id, self.operation_id, self.run_id])
        _identity(self.capsule_id, "feedback-")
        require_hash(self.root_binding_sha256)
        return self


class LearningInputText(_Frozen):
    path: str = Field(min_length=1, max_length=2048)
    sha256: str
    bytes: int = Field(ge=0, le=16_777_216)
    text: str
    role: Literal["original_crash", "registered_candidate_patch"]
    candidate_id: str | None = None

    @model_validator(mode="after")
    def valid(self) -> LearningInputText:
        from .model_calls import sha256

        _reference(self.path)
        require_hash(self.sha256)
        if len(self.text.encode("utf-8")) != self.bytes or sha256(self.text) != self.sha256:
            raise ValueError("learning input text differs from original bytes")
        if self.role == "registered_candidate_patch":
            require_hash(self.candidate_id or "")
        elif self.candidate_id is not None:
            raise ValueError("original crash cannot claim a candidate identity")
        return self


class LearningOwner(_Frozen):
    approval: HostLearningApproval
    source: FeedbackCapsule
    source_binding_sha256: str
    provider_profile_json: str
    learning_lineage_groups: tuple[Identifier, ...] = Field(min_length=1, max_length=4096)
    input_texts: tuple[LearningInputText, ...] = Field(min_length=1, max_length=10_000)
    recovery_source: Literal["original_feedback_capsule_required"] = "original_feedback_capsule_required"
    parent_skill_version_id: None = None

    @model_validator(mode="after")
    def valid(self) -> LearningOwner:
        require_hash(self.source_binding_sha256)
        _object(self.provider_profile_json)
        if (self.source.capsule_id != self.approval.capsule_id or
                self.source_binding_sha256 != content_hash(self.source) or
                self.source.contents()["approval"] != self.approval.feedback_approval.model_dump(mode="json")):
            raise ValueError("learning owner source differs from complete original approval")
        require_identifiers(list(self.learning_lineage_groups))
        require_unique(list(self.learning_lineage_groups))
        if list(self.learning_lineage_groups) != sorted(self.learning_lineage_groups):
            raise ValueError("learning lineage must be ordered")
        require_unique([item.path.casefold() for item in self.input_texts])
        if self.input_texts[0].role != "original_crash" or any(
                item.role != "registered_candidate_patch" for item in self.input_texts[1:]):
            raise ValueError("learning inputs require original crash followed by registered patches")
        return self


class LearningRequest(_Frozen):
    intent: ModelCallIntentV2
    prepared: PreparedRequest

    @model_validator(mode="after")
    def valid(self) -> LearningRequest:
        if self.prepared.tools or self.intent.tool_names:
            raise ValueError("learning requests cannot grant tools")
        return self


class LearningCost(_Frozen):
    reserved_calls: int = Field(ge=0)
    undispatched_calls: int = Field(ge=0)
    completed_calls: int = Field(ge=0)
    uncertain_calls: int = Field(ge=0)
    missing_usage_calls: int = Field(ge=0)
    invalid_usage_calls: int = Field(ge=0)
    known_input_tokens: int = Field(ge=0)
    known_output_tokens: int = Field(ge=0)
    known_lower_bound: int = Field(ge=0)
    total_tokens: int | None = Field(ge=0)
    monetary_cost: None = None
    monetary_cost_authority: Literal["unknown"] = "unknown"
    scope: Literal["offline_synthetic"] = "offline_synthetic"


class LearningDraftPayload(_Frozen):
    owner: LearningOwner
    learning_call_id: Identifier
    calls_json: str
    cost: LearningCost
    projection: SolverProjection
    allowed_splits: tuple[KnowledgeSplit, ...] = Field(min_length=1, max_length=3)
    excluded_lineage_groups: tuple[Identifier, ...] = Field(max_length=4096)
    learning_lineage_groups: tuple[Identifier, ...] = Field(min_length=1, max_length=4096)
    status: Literal["pending_host_review"] = "pending_host_review"
    derivation_authority: Literal["model_inference"] = "model_inference"
    validation_scope: Literal["unvalidated_inference"] = "unvalidated_inference"
    application_qualification: Literal["none"] = "none"
    parent_skill_version_id: None = None

    @model_validator(mode="after")
    def valid(self) -> LearningDraftPayload:
        require_identifiers([self.learning_call_id])
        _object(self.calls_json)
        approval = self.owner.approval.feedback_approval
        if (self.projection.derivation_authority != "model_inference" or self.projection.validation_scopes or
                self.allowed_splits != approval.allowed_splits or
                self.excluded_lineage_groups != approval.excluded_lineage_groups or
                self.learning_lineage_groups != self.owner.learning_lineage_groups):
            raise ValueError("learning draft expands authority or permission")
        return self


class LearningDraft(_Frozen):
    draft_id: str
    content_sha256: str
    payload_json: str

    @model_validator(mode="after")
    def valid(self) -> LearningDraft:
        require_hash(self.content_sha256)
        _identity(self.draft_id, "learning-draft-")
        payload = LearningDraftPayload.model_validate_json(encode(_object(self.payload_json)))
        if (encode(payload) != self.payload_json or content_hash(payload) != self.content_sha256 or
                self.draft_id != "learning-draft-" + self.content_sha256):
            raise ValueError("learning draft identity differs")
        return self

    def contents(self) -> dict[str, Any]:
        return _object(self.payload_json)


class _PortableFrozen(BaseModel):
    """New read-only source records; the execution owner's schema stays unchanged."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, revalidate_instances="always")
    schema_version: Literal[2] = 2

    @field_validator("schema_version", mode="before")
    @classmethod
    def exact_schema(cls, value: object) -> object:
        if type(value) is not int or value != 2:
            raise ValueError("portable learning schema must be integer 2")
        return value


class LearningSourcePayload(_PortableFrozen):
    approval: HostLearningApproval
    owner: LearningOwner
    draft: LearningDraft
    operation_cost: LearningCost
    operation_calls_json: str
    directories: tuple[str, ...]
    files: tuple[FeedbackFile, ...]
    source_authority: Literal["authenticated_original_operation"] = "authenticated_original_operation"
    recovery: Literal["read_only_portable_closure"] = "read_only_portable_closure"
    application_qualification: Literal["none"] = "none"

    @model_validator(mode="after")
    def valid(self) -> LearningSourcePayload:
        if self.owner.approval != self.approval:
            raise ValueError("portable learning owner differs from original approval")
        draft = LearningDraftPayload.model_validate_json(self.draft.payload_json)
        if draft.owner != self.owner:
            raise ValueError("portable learning draft has another original owner")
        _object(self.operation_calls_json)
        paths = [file.path for file in self.files]
        for path in self.directories:
            _reference(path)
        if (paths != sorted(paths) or list(self.directories) != sorted(self.directories) or
                len({path.casefold() for path in (*paths, *self.directories)}) != len(paths) + len(self.directories)):
            raise ValueError("portable learning inventory is unordered or duplicated")
        return self


class LearningSource(_PortableFrozen):
    source_id: str
    content_sha256: str
    payload_json: str

    @model_validator(mode="after")
    def valid(self) -> LearningSource:
        payload = LearningSourcePayload.model_validate_json(encode(_object(self.payload_json)))
        if (encode(payload) != self.payload_json or content_hash(payload) != self.content_sha256 or
                self.source_id != "learning-source-" + self.content_sha256):
            raise ValueError("portable learning source identity differs")
        return self

    def contents(self) -> dict[str, Any]:
        return _object(self.payload_json)
