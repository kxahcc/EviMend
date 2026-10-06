"""Immutable synthetic Skill versions, prospective plans and audit receipts.

These records cannot grant application eligibility or change live SDK knowledge.
Plan ordering is a serial host observation, not a trusted timestamp service.
"""

from __future__ import annotations

from typing import Literal, cast

from pydantic import Field, field_validator, model_validator

from .contracts import require_hash, require_identifiers, require_unique
from .feedback_contracts import (
    FeedbackFile,
    HostCandidateApproval,
    HostFeedbackApproval,
    Identifier,
    Reference,
    _Frozen,
    _identity,
    _object,
    content_hash,
    encode,
)
from .knowledge_contracts import KnowledgeSplit, SolverProjection
from .learning_contracts import HostLearningApproval, LearningCost, _PortableFrozen
from .metrics import EvaluationManifestV5, parse_evaluation_manifest
from .model_calls import canonical, sha256


class HostSkillVersionApproval(_Frozen):
    approval_id: Identifier
    skill_id: Identifier
    projection_sha256: str
    allowed_splits: tuple[KnowledgeSplit, ...] = Field(min_length=1, max_length=3)
    excluded_lineage_groups: tuple[Identifier, ...] = Field(default=(), max_length=4096)
    learning_lineage_groups: tuple[Identifier, ...] = Field(default=(), max_length=4096)
    parent_version_id: str | None = None
    source_candidate_id: str | None = None

    @model_validator(mode="after")
    def valid_approval(self) -> HostSkillVersionApproval:
        require_identifiers([self.approval_id, self.skill_id, *self.excluded_lineage_groups, *self.learning_lineage_groups])
        require_hash(self.projection_sha256)
        for values in (self.allowed_splits, self.excluded_lineage_groups, self.learning_lineage_groups):
            require_unique(list(values))
        if (self.parent_version_id is None) != (self.source_candidate_id is None):
            raise ValueError("Skill revision approval needs both parent and source candidate")
        if self.parent_version_id is not None:
            _identity(self.parent_version_id, "skill-version-")
            _identity(self.source_candidate_id or "", "experience-candidate-")
        return self


class VersionPayload(_Frozen):
    approval: HostSkillVersionApproval
    projection: SolverProjection
    parent_approval: HostSkillVersionApproval | None = None
    candidate_approval: HostCandidateApproval | None = None
    feedback_approval: HostFeedbackApproval | None = None
    learning_lineage_groups: tuple[Identifier, ...] = Field(max_length=4096)
    qualification: Literal["unvalidated"] = "unvalidated"
    application_qualification: Literal["none"] = "none"
    learning_model_call_status: Literal["not_run"] = "not_run"
    learning_tokens: None = None

    @model_validator(mode="after")
    def valid_payload(self) -> VersionPayload:
        if (self.projection.derivation_authority != "host_curated" or self.projection.validation_scopes or
                sha256(canonical(self.projection.model_dump(mode="json"))) != self.approval.projection_sha256):
            raise ValueError("Skill version requires exact approved host guidance without generalized validation")
        if self.approval.parent_version_id is None:
            if any(item is not None for item in (self.parent_approval, self.candidate_approval, self.feedback_approval)):
                raise ValueError("seed Skill cannot acquire revision sources")
        elif any(item is None for item in (self.parent_approval, self.candidate_approval, self.feedback_approval)):
            raise ValueError("Skill revision is missing its original source approvals")
        require_identifiers(list(self.learning_lineage_groups))
        require_unique(list(self.learning_lineage_groups))
        if list(self.learning_lineage_groups) != sorted(self.learning_lineage_groups):
            raise ValueError("Skill learning lineage must have one canonical order")
        return self


class SkillVersion(_Frozen):
    version_id: str
    content_sha256: str
    payload_json: str

    @model_validator(mode="after")
    def valid_record(self) -> SkillVersion:
        payload = VersionPayload.model_validate_json(encode(_object(self.payload_json)))
        if (encode(payload) != self.payload_json or content_hash(payload) != self.content_sha256 or
                self.version_id != "skill-version-" + self.content_sha256):
            raise ValueError("Skill version content identity changed")
        return self

    def contents(self) -> dict:
        return _object(self.payload_json)


class ValidationPair(_Frozen):
    case_id: Identifier
    kind: Literal["representative", "boundary"]
    old_manifest_json: str
    new_manifest_json: str
    old_entry_id: str
    new_entry_id: str

    @model_validator(mode="after")
    def valid_pair(self) -> ValidationPair:
        require_identifiers([self.case_id])
        for value in (self.old_entry_id, self.new_entry_id):
            _identity(value, "experience-")
        manifests = [parse_evaluation_manifest(_object(value)) for value in (self.old_manifest_json, self.new_manifest_json)]
        for manifest in manifests:
            if (not isinstance(manifest, EvaluationManifestV5) or len(manifest.tasks) != 1 or
                    len(manifest.variants) != 1 or len(manifest.repeat_ids) != 1 or len(manifest.runs) != 1 or
                    manifest.tasks[0].selection is None or manifest.runs[0].work_path is None):
                raise ValueError("Skill validation requires exact single-run v5 manifests")
        old, new = manifests
        if (old.tasks != new.tasks or old.variants != new.variants or old.repeat_ids != new.repeat_ids or
                canonical(old.configuration.model_dump(mode="json")) != canonical(new.configuration.model_dump(mode="json")) or
                old.runs[0].run_id == new.runs[0].run_id or old.runs[0].work_path == new.runs[0].work_path):
            raise ValueError("Skill validation roles must share tasks, knowledge, resources, tools and budgets")
        return self


class PlanSpec(_Frozen):
    old_version_id: str
    new_version_id: str
    pairs: tuple[ValidationPair, ...] = Field(min_length=2, max_length=64)
    policy_json: str | None = None
    comparison: Literal["same_pool_targeted_procedure"] = "same_pool_targeted_procedure"

    def _validate_common(self) -> PlanSpec:
        for value in (self.old_version_id, self.new_version_id):
            _identity(value, "skill-version-")
        if self.old_version_id == self.new_version_id:
            raise ValueError("Skill validation needs distinct parent and revision")
        require_unique([pair.case_id for pair in self.pairs])
        if {pair.kind for pair in self.pairs} != {"representative", "boundary"}:
            raise ValueError("Skill validation needs representative and boundary coverage")
        if self.policy_json is not None and _object(self.policy_json).get("scope") != "offline_synthetic":
            raise ValueError("Skill validation policy must be explicitly synthetic")
        return self

    @model_validator(mode="after")
    def valid_spec(self) -> PlanSpec:
        return self._validate_common()


class HostValidationPlanApproval(_Frozen):
    approval_id: Identifier
    spec_sha256: str
    old_version_approval: HostSkillVersionApproval
    new_version_approval: HostSkillVersionApproval
    origin_split: Literal["candidate_validation"] = "candidate_validation"
    allow_learning: Literal[False] = False

    @field_validator("allow_learning", mode="before")
    @classmethod
    def exact_permission(cls, value: object) -> object:
        if type(value) is not bool or value is not False:
            raise ValueError("Skill validation cannot grant learning permission")
        return value

    def _validate_common(self) -> HostValidationPlanApproval:
        require_identifiers([self.approval_id])
        require_hash(self.spec_sha256)
        if self.old_version_approval.skill_id != self.new_version_approval.skill_id:
            raise ValueError("Skill validation must concern one stable Skill identity")
        return self

    @model_validator(mode="after")
    def valid_approval(self) -> HostValidationPlanApproval:
        return self._validate_common()


class PlanPayload(_Frozen):
    approval: HostValidationPlanApproval
    spec: PlanSpec
    frozen_at: str
    evidence_root: str
    evidence_root_identity: tuple[int, int, int, int]
    learning_lineage_groups: tuple[Identifier, ...]
    observed_absent_work_paths: tuple[Reference, ...]
    time_authority: Literal["host_observation"] = "host_observation"
    application_qualification: Literal["none"] = "none"

    def _validate_common(self) -> PlanPayload:
        if content_hash(self.spec) != self.approval.spec_sha256:
            raise ValueError("Skill plan differs from explicit external approval")
        return self

    @model_validator(mode="after")
    def valid_payload(self) -> PlanPayload:
        return self._validate_common()


class SkillValidationPlan(_Frozen):
    plan_id: str
    content_sha256: str
    payload_json: str

    @model_validator(mode="after")
    def valid_record(self) -> SkillValidationPlan:
        payload = PlanPayload.model_validate_json(encode(_object(self.payload_json)))
        if (encode(payload) != self.payload_json or content_hash(payload) != self.content_sha256 or
                self.plan_id != "skill-plan-" + self.content_sha256):
            raise ValueError("Skill validation plan content identity changed")
        return self

    def contents(self) -> dict:
        return _object(self.payload_json)


class ReceiptPayload(_Frozen):
    plan_id: str
    approval_sha256: str
    case_id: Identifier
    role: Literal["old", "new"]
    version_id: str
    manifest_json: str
    aggregate_json: str
    directories: tuple[Reference, ...]
    files: tuple[FeedbackFile, ...]
    target_projection_sha256: str
    target_skill_id: str
    use_status: Literal["followed", "not_followed", "unknown"]
    use_reasons: tuple[str, ...]
    validation_level: Literal["offline_synthetic"] = "offline_synthetic"
    origin_split: Literal["candidate_validation"] = "candidate_validation"
    allow_learning: Literal[False] = False
    application_qualification: Literal["none"] = "none"

    @field_validator("allow_learning", mode="before")
    @classmethod
    def exact_permission(cls, value: object) -> object:
        if type(value) is not bool or value is not False:
            raise ValueError("Skill validation receipt cannot become learning material")
        return value

    @model_validator(mode="after")
    def valid_payload(self) -> ReceiptPayload:
        _identity(self.plan_id, "skill-plan-")
        _identity(self.version_id, "skill-version-")
        require_hash(self.approval_sha256)
        require_hash(self.target_projection_sha256)
        if self.target_skill_id != "dfx-" + self.target_projection_sha256[:56]:
            raise ValueError("Skill validation target content identity changed")
        _object(self.manifest_json)
        _object(self.aggregate_json)
        return self


class SkillValidationReceipt(_Frozen):
    receipt_id: str
    content_sha256: str
    payload_json: str

    @model_validator(mode="after")
    def valid_record(self) -> SkillValidationReceipt:
        payload = ReceiptPayload.model_validate_json(encode(_object(self.payload_json)))
        if (encode(payload) != self.payload_json or content_hash(payload) != self.content_sha256 or
                self.receipt_id != "skill-receipt-" + self.content_sha256):
            raise ValueError("Skill validation receipt content identity changed")
        return self

    def contents(self) -> dict:
        return _object(self.payload_json)


class HostModelSkillVersionApproval(_PortableFrozen):
    approval_id: Identifier
    skill_id: Identifier
    projection_sha256: str
    parent_version_id: str
    source_draft_id: str
    learning_source_id: str
    allowed_splits: tuple[KnowledgeSplit, ...] = Field(min_length=1, max_length=3)
    excluded_lineage_groups: tuple[Identifier, ...] = Field(default=(), max_length=4096)
    learning_lineage_groups: tuple[Identifier, ...] = Field(default=(), max_length=4096)
    review: Literal["approve_exact_model_draft"] = "approve_exact_model_draft"
    parent_relation_authority: Literal["external_host_review"] = "external_host_review"

    @model_validator(mode="after")
    def valid(self) -> HostModelSkillVersionApproval:
        require_identifiers([self.approval_id, self.skill_id, *self.excluded_lineage_groups, *self.learning_lineage_groups])
        require_hash(self.projection_sha256)
        _identity(self.parent_version_id, "skill-version-")
        _identity(self.source_draft_id, "learning-draft-")
        _identity(self.learning_source_id, "learning-source-")
        for values in (self.allowed_splits, self.excluded_lineage_groups, self.learning_lineage_groups):
            require_unique(list(values))
        return self


VersionApproval = HostSkillVersionApproval | HostModelSkillVersionApproval


class ModelVersionPayload(_PortableFrozen):
    approval: HostModelSkillVersionApproval
    projection: SolverProjection
    parent_approval: VersionApproval
    learning_approval: HostLearningApproval
    learning_source_id: str
    draft_cost: LearningCost
    operation_cost: LearningCost
    learning_lineage_groups: tuple[Identifier, ...] = Field(max_length=4096)
    qualification: Literal["unvalidated"] = "unvalidated"
    application_qualification: Literal["none"] = "none"
    derivation_authority: Literal["model_inference"] = "model_inference"
    learning_model_call_status: Literal["observed"] = "observed"

    @model_validator(mode="after")
    def valid(self) -> ModelVersionPayload:
        if (self.projection.derivation_authority != "model_inference" or self.projection.validation_scopes or
                content_hash(self.projection) != self.approval.projection_sha256 or
                self.learning_source_id != self.approval.learning_source_id):
            raise ValueError("model Skill differs from its explicitly reviewed draft")
        require_identifiers(list(self.learning_lineage_groups))
        require_unique(list(self.learning_lineage_groups))
        if list(self.learning_lineage_groups) != sorted(self.learning_lineage_groups):
            raise ValueError("model Skill lineage must have canonical order")
        return self


VersionBody = VersionPayload | ModelVersionPayload


def _parse(raw: str | dict, old, new):
    value = _object(raw) if isinstance(raw, str) else raw
    schema = value.get("schema_version")
    if type(schema) is not int or schema not in (1, 2):
        raise ValueError("Skill schema must be explicitly integer 1 or 2")
    return (old if schema == 1 else new).model_validate_json(encode(value))


def parse_version_approval(raw: str | dict) -> VersionApproval:
    return _parse(raw, HostSkillVersionApproval, HostModelSkillVersionApproval)


def parse_version_payload(raw: str | dict) -> VersionBody:
    return _parse(raw, VersionPayload, ModelVersionPayload)


class SkillVersionV2(_PortableFrozen):
    version_id: str
    content_sha256: str
    payload_json: str

    @model_validator(mode="after")
    def valid(self) -> SkillVersionV2:
        payload = ModelVersionPayload.model_validate_json(encode(_object(self.payload_json)))
        if (encode(payload) != self.payload_json or content_hash(payload) != self.content_sha256 or
                self.version_id != "skill-version-" + self.content_sha256):
            raise ValueError("model Skill content identity differs")
        return self

    def contents(self) -> dict:
        return _object(self.payload_json)


VersionRecord = SkillVersion | SkillVersionV2


def parse_version(raw: str | dict) -> VersionRecord:
    return _parse(raw, SkillVersion, SkillVersionV2)


class ValidationPairV2(_PortableFrozen):
    case_id: Identifier
    kind: Literal["representative", "boundary"]
    old_manifest_json: str
    new_manifest_json: str
    old_entry_id: str
    new_entry_id: str

    @model_validator(mode="after")
    def valid(self) -> ValidationPairV2:
        require_identifiers([self.case_id])
        for value in (self.old_entry_id, self.new_entry_id):
            _identity(value, "skill-version-")
        manifests = [parse_evaluation_manifest(_object(value)) for value in (self.old_manifest_json, self.new_manifest_json)]
        for manifest in manifests:
            if (manifest.schema_version != 6 or len(manifest.tasks) != 1 or len(manifest.variants) != 1 or
                    len(manifest.repeat_ids) != 1 or len(manifest.runs) != 1 or manifest.tasks[0].selection is None or
                    manifest.runs[0].work_path is None):
                raise ValueError("model Skill validation requires exact single-run manifest 6")
        old, new = manifests
        if (old.tasks != new.tasks or old.variants != new.variants or old.repeat_ids != new.repeat_ids or
                canonical(old.configuration.model_dump(mode="json")) != canonical(new.configuration.model_dump(mode="json")) or
                old.runs[0].run_id == new.runs[0].run_id or old.runs[0].work_path == new.runs[0].work_path):
            raise ValueError("model validation roles must share knowledge, tools, resources and budgets")
        return self


class PlanSpecV2(_PortableFrozen):
    old_version_id: str
    new_version_id: str
    pairs: tuple[ValidationPairV2, ...] = Field(min_length=2, max_length=64)
    policy_json: str | None = None
    comparison: Literal["same_pool_targeted_procedure"] = "same_pool_targeted_procedure"

    @model_validator(mode="after")
    def valid(self) -> PlanSpecV2:
        PlanSpec._validate_common(cast(PlanSpec, self))
        return self


class HostValidationPlanApprovalV2(_PortableFrozen):
    approval_id: Identifier
    spec_sha256: str
    old_version_approval: VersionApproval
    new_version_approval: HostModelSkillVersionApproval
    origin_split: Literal["candidate_validation"] = "candidate_validation"
    allow_learning: Literal[False] = False

    @field_validator("allow_learning", mode="before")
    @classmethod
    def exact_permission(cls, value: object) -> object:
        return HostValidationPlanApproval.exact_permission(value)

    @model_validator(mode="after")
    def valid(self) -> HostValidationPlanApprovalV2:
        HostValidationPlanApproval._validate_common(cast(HostValidationPlanApproval, self))
        return self


ValidationApproval = HostValidationPlanApproval | HostValidationPlanApprovalV2


def parse_validation_approval(raw: str | dict) -> ValidationApproval:
    return _parse(raw, HostValidationPlanApproval, HostValidationPlanApprovalV2)


class PlanPayloadV2(_PortableFrozen):
    approval: HostValidationPlanApprovalV2
    spec: PlanSpecV2
    frozen_at: str
    evidence_root: str
    evidence_root_identity: tuple[int, int, int, int]
    learning_lineage_groups: tuple[Identifier, ...]
    observed_absent_work_paths: tuple[Reference, ...]
    time_authority: Literal["host_observation"] = "host_observation"
    application_qualification: Literal["none"] = "none"

    @model_validator(mode="after")
    def valid(self) -> PlanPayloadV2:
        PlanPayload._validate_common(cast(PlanPayload, self))
        return self


class SkillValidationPlanV2(_PortableFrozen):
    plan_id: str
    content_sha256: str
    payload_json: str

    @model_validator(mode="after")
    def valid(self) -> SkillValidationPlanV2:
        payload = PlanPayloadV2.model_validate_json(encode(_object(self.payload_json)))
        if (encode(payload) != self.payload_json or content_hash(payload) != self.content_sha256 or
                self.plan_id != "skill-plan-" + self.content_sha256):
            raise ValueError("model Skill plan identity differs")
        return self

    def contents(self) -> dict:
        return _object(self.payload_json)


class ReceiptPayloadV2(_PortableFrozen):
    plan_id: str
    approval_sha256: str
    case_id: Identifier
    role: Literal["old", "new"]
    version_id: str
    manifest_json: str
    aggregate_json: str
    directories: tuple[Reference, ...]
    files: tuple[FeedbackFile, ...]
    target_projection_sha256: str
    target_skill_id: str
    use_status: Literal["followed", "not_followed", "unknown"]
    use_reasons: tuple[str, ...]
    validation_level: Literal["offline_synthetic"] = "offline_synthetic"
    origin_split: Literal["candidate_validation"] = "candidate_validation"
    allow_learning: Literal[False] = False
    application_qualification: Literal["none"] = "none"

    @field_validator("allow_learning", mode="before")
    @classmethod
    def exact_permission(cls, value: object) -> object:
        return ReceiptPayload.exact_permission(value)

    @model_validator(mode="after")
    def valid(self) -> ReceiptPayloadV2:
        _identity(self.plan_id, "skill-plan-")
        _identity(self.version_id, "skill-version-")
        require_hash(self.approval_sha256)
        require_hash(self.target_projection_sha256)
        if self.target_skill_id != "dfx-" + self.version_id.removeprefix("skill-version-")[:56]:
            raise ValueError("model Skill target differs from the actual version catalog identity")
        _object(self.manifest_json)
        _object(self.aggregate_json)
        return self


class SkillValidationReceiptV2(_PortableFrozen):
    receipt_id: str
    content_sha256: str
    payload_json: str

    @model_validator(mode="after")
    def valid(self) -> SkillValidationReceiptV2:
        payload = ReceiptPayloadV2.model_validate_json(encode(_object(self.payload_json)))
        if (encode(payload) != self.payload_json or content_hash(payload) != self.content_sha256 or
                self.receipt_id != "skill-receipt-" + self.content_sha256):
            raise ValueError("model Skill receipt identity differs")
        return self

    def contents(self) -> dict:
        return _object(self.payload_json)


def parse_plan(raw: str | dict) -> SkillValidationPlan | SkillValidationPlanV2:
    return _parse(raw, SkillValidationPlan, SkillValidationPlanV2)


def parse_plan_payload(raw: str | dict) -> PlanPayload | PlanPayloadV2:
    return _parse(raw, PlanPayload, PlanPayloadV2)


def parse_receipt(raw: str | dict) -> SkillValidationReceipt | SkillValidationReceiptV2:
    return _parse(raw, SkillValidationReceipt, SkillValidationReceiptV2)


def parse_receipt_payload(raw: str | dict) -> ReceiptPayload | ReceiptPayloadV2:
    return _parse(raw, ReceiptPayload, ReceiptPayloadV2)


ValidationPlanRecord = SkillValidationPlan | SkillValidationPlanV2
ValidationReceiptRecord = SkillValidationReceipt | SkillValidationReceiptV2
ValidationPlanBody = PlanPayload | PlanPayloadV2
ValidationReceiptBody = ReceiptPayload | ReceiptPayloadV2
