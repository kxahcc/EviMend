"""Immutable, bounded contracts for host-admitted historical guidance.

These objects prove shape and internal consistency, not source authenticity,
permission admission, semantic applicability or application repair quality.
Only a host registry and a source-reading store can establish which raw
materials were actually admitted. Tuple order is preserved and hashed; only
snapshot entry references have a required ascending order.
"""

from __future__ import annotations

import hashlib
import json
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .contracts import require_hash, require_identifiers, require_path, require_unique

MaterialRole = Literal[
    "curated_projection", "inference_projection", "synthetic_history", "hidden_test",
    "reference_patch", "evaluation_label", "raw_session",
]
KnowledgeSplit = Literal["development", "candidate_validation", "confirmation"]
KnowledgeUse = Literal["learn", "solve"]

MAX_PAYLOAD_BYTES = 8_388_608
MAX_CLOSURE_BYTES = MAX_PAYLOAD_BYTES
MAX_SNAPSHOT_ENTRIES = 4096
Text = Annotated[str, Field(min_length=1, max_length=4096)]
Tag = Annotated[str, Field(min_length=1, max_length=128)]
Identifier = Annotated[str, Field(min_length=1, max_length=128)]
RelativePath = Annotated[str, Field(min_length=1, max_length=2048)]


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, revalidate_instances="always")
    schema_version: Literal[1] = 1


def _text(*values: str) -> None:
    if any(not value.strip() for value in values):
        raise ValueError("knowledge text cannot be blank")


def _unique(values: tuple[str, ...]) -> None:
    require_unique(list(values))


def _grants(grants: tuple[MaterialGrant, ...]) -> None:
    require_unique([item.material_id for item in grants])
    require_unique([item.path.casefold() for item in grants])


def encode(value: BaseModel | dict[str, Any]) -> str:
    """Canonical object serialization; no truncation or nonfinite numbers."""
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    elif type(value) is not dict:
        raise ValueError("knowledge encoding requires an object")
    try:
        result = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    except RecursionError as exc:
        raise ValueError("knowledge payload nesting is invalid") from exc
    if len(result.encode("utf-8")) > MAX_PAYLOAD_BYTES:
        raise ValueError("knowledge payload exceeds its byte limit")
    return result


def content_hash(value: BaseModel | dict[str, Any]) -> str:
    return hashlib.sha256(encode(value).encode("utf-8")).hexdigest()


def _load(value: str) -> dict[str, Any]:
    if len(value.encode("utf-8")) > MAX_PAYLOAD_BYTES:
        raise ValueError("knowledge payload exceeds its byte limit")

    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, item in pairs:
            if key in result:
                raise ValueError("duplicate knowledge payload key")
            result[key] = item
        return result

    def invalid_constant(_: str) -> Any:
        raise ValueError("nonfinite knowledge payload value")

    try:
        result = json.loads(value, object_pairs_hook=unique, parse_constant=invalid_constant)
    except (json.JSONDecodeError, RecursionError) as exc:
        raise ValueError("knowledge payload JSON is invalid") from exc
    if not isinstance(result, dict) or encode(result) != value:
        raise ValueError("knowledge payload is not a canonical object")
    return result


class MaterialGrant(_Frozen):
    material_id: Identifier
    path: RelativePath
    sha256: str
    bytes: int = Field(ge=0, le=MAX_PAYLOAD_BYTES)
    role: MaterialRole
    origin_split: Literal["curated", "development", "candidate_validation", "confirmation"]
    visibility: Literal["solver_approved", "audit_only", "restricted"]
    allowed_uses: tuple[KnowledgeUse, ...] = Field(max_length=2)
    allowed_splits: tuple[KnowledgeSplit, ...] = Field(max_length=3)
    derivation_call_id: Identifier | None = None
    known_tokens: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def valid_grant(self) -> MaterialGrant:
        require_identifiers([self.material_id])
        require_path(self.path)
        require_hash(self.sha256)
        _unique(self.allowed_uses)
        _unique(self.allowed_splits)
        if self.derivation_call_id is not None:
            require_identifiers([self.derivation_call_id])
        if self.known_tokens is not None and self.derivation_call_id is None:
            raise ValueError("declared extraction cost requires a derivation call identity")
        return self


class HostMaterialManifest(_Frozen):
    registry_id: Identifier
    grants: tuple[MaterialGrant, ...] = Field(max_length=4096)

    @model_validator(mode="after")
    def valid_manifest(self) -> HostMaterialManifest:
        require_identifiers([self.registry_id])
        _grants(self.grants)
        return self


class ExperiencePremise(_Frozen):
    premise_id: Identifier
    statement: Text
    unknown_question: Text

    @model_validator(mode="after")
    def valid_premise(self) -> ExperiencePremise:
        require_identifiers([self.premise_id])
        _text(self.statement, self.unknown_question)
        return self


class ObservationBranch(_Frozen):
    observation: Text
    next_action: Text

    @model_validator(mode="after")
    def valid_branch(self) -> ObservationBranch:
        _text(self.observation, self.next_action)
        return self


class ProcedureStep(_Frozen):
    step_id: Identifier
    instruction: Text
    branches: tuple[ObservationBranch, ...] = Field(max_length=64)

    @model_validator(mode="after")
    def valid_step(self) -> ProcedureStep:
        require_identifiers([self.step_id])
        _text(self.instruction)
        require_unique([item.observation for item in self.branches])
        return self


class ExperienceContent(_Frozen):
    tags: tuple[Tag, ...] = Field(max_length=128)
    trigger: Text
    premises: tuple[ExperiencePremise, ...] = Field(min_length=1, max_length=64)
    procedure: tuple[ProcedureStep, ...] = Field(min_length=1, max_length=128)
    preserves: tuple[Text, ...] = Field(min_length=1, max_length=128)
    counterexamples: tuple[Text, ...] = Field(min_length=1, max_length=128)
    exit_conditions: tuple[Text, ...] = Field(min_length=1, max_length=128)

    @model_validator(mode="after")
    def valid_content(self) -> ExperienceContent:
        _text(self.trigger, *self.tags, *self.preserves, *self.counterexamples, *self.exit_conditions)
        for values in (self.tags, self.preserves, self.counterexamples, self.exit_conditions):
            _unique(values)
        require_unique([item.premise_id for item in self.premises])
        require_unique([item.step_id for item in self.procedure])
        return self


class LimitedValidationScope(_Frozen):
    level: Literal["none", "synthetic_fixture"]
    result: Literal["unknown", "passed", "failed"]
    scope_names: tuple[Tag, ...] = Field(max_length=128)

    @model_validator(mode="after")
    def valid_scope(self) -> LimitedValidationScope:
        _text(*self.scope_names)
        _unique(self.scope_names)
        if self.level == "none" and (self.result != "unknown" or self.scope_names):
            raise ValueError("no validation requires unknown result and empty scope")
        if self.level == "synthetic_fixture" and not self.scope_names:
            raise ValueError("synthetic validation requires an explicit limited scope")
        return self


class SolverProjection(_Frozen):
    guidance: ExperienceContent
    derivation_authority: Literal["host_curated", "model_inference"]
    validation_scopes: tuple[LimitedValidationScope, ...] = Field(default=(), max_length=1024)
    epistemic_status: Literal["historical_guidance_not_current_evidence"] = "historical_guidance_not_current_evidence"


class HistoricalSource(_Frozen):
    task_id: Identifier
    run_id: Identifier
    lineage_group_id: Identifier
    source_snapshot_sha256: str
    candidate_id: str | None = None
    candidate_snapshot_sha256: str | None = None
    report_sha256: str
    artifact_sha256: str
    validation: LimitedValidationScope
    oracle_descriptor: Text
    environment_descriptor: Text

    @model_validator(mode="after")
    def valid_history(self) -> HistoricalSource:
        require_identifiers([self.task_id, self.run_id, self.lineage_group_id])
        for value in (self.source_snapshot_sha256, self.report_sha256, self.artifact_sha256):
            require_hash(value)
        if (self.candidate_id is None) != (self.candidate_snapshot_sha256 is None):
            raise ValueError("historical candidate requires both candidate and snapshot hashes")
        if self.candidate_id is not None:
            require_hash(self.candidate_id)
            require_hash(self.candidate_snapshot_sha256 or "")
        if self.validation.level != "synthetic_fixture":
            raise ValueError("historical fixture cannot claim another validation level")
        _text(self.oracle_descriptor, self.environment_descriptor)
        return self


class ExperienceDraft(_Frozen):
    projection_material_id: Identifier
    history_material_ids: tuple[Identifier, ...] = Field(default=(), max_length=1024)

    @model_validator(mode="after")
    def valid_draft(self) -> ExperienceDraft:
        require_identifiers([self.projection_material_id, *self.history_material_ids])
        _unique(self.history_material_ids)
        if self.projection_material_id in self.history_material_ids:
            raise ValueError("projection material cannot also be historical material")
        return self


class HistoricalMaterial(_Frozen):
    material_id: Identifier
    record: HistoricalSource

    @model_validator(mode="after")
    def valid_material(self) -> HistoricalMaterial:
        require_identifiers([self.material_id])
        return self


class ExperiencePayload(_Frozen):
    projection: SolverProjection
    source_grants: tuple[MaterialGrant, ...] = Field(min_length=1, max_length=1025)
    history: tuple[HistoricalMaterial, ...] = Field(default=(), max_length=1024)
    extraction_call_id: Identifier | None = None
    known_tokens: int | None = Field(default=None, ge=0)
    cost_authority: Literal["host_declaration", "unknown"]
    application_qualification: Literal["none"] = "none"

    @model_validator(mode="after")
    def valid_payload(self) -> ExperiencePayload:
        _grants(self.source_grants)
        require_unique([item.material_id for item in self.history])
        projections = [item for item in self.source_grants
                       if item.role in ("curated_projection", "inference_projection")]
        historical = [item for item in self.source_grants if item.role == "synthetic_history"]
        if len(projections) != 1 or len(projections) + len(historical) != len(self.source_grants):
            raise ValueError("experience requires exactly one projection and only synthetic history grants")
        if {item.material_id for item in historical} != {item.material_id for item in self.history}:
            raise ValueError("historical materials and source grants disagree")
        expected_authority = "host_curated" if projections[0].role == "curated_projection" else "model_inference"
        if self.projection.derivation_authority != expected_authority:
            raise ValueError("projection authority disagrees with its host grant")
        if self.projection.validation_scopes != tuple(item.record.validation for item in self.history):
            raise ValueError("projection validation scopes disagree with the historical fixtures")
        if (self.extraction_call_id != projections[0].derivation_call_id or
                self.known_tokens != projections[0].known_tokens):
            raise ValueError("extraction identity or cost disagrees with its host grant")
        expected_cost = "unknown" if projections[0].known_tokens is None else "host_declaration"
        if self.cost_authority != expected_cost:
            raise ValueError("cost authority disagrees with declared token knowledge")
        if self.extraction_call_id is not None:
            require_identifiers([self.extraction_call_id])
        return self


class ExperienceEntry(_Frozen):
    """The canonical payload is the sole content, and contents() is a copy."""

    entry_id: str
    content_sha256: str
    payload_json: str = Field(min_length=1, max_length=MAX_PAYLOAD_BYTES)

    @model_validator(mode="after")
    def valid_entry(self) -> ExperienceEntry:
        require_hash(self.content_sha256)
        require_identifiers([self.entry_id])
        _load(self.payload_json)
        payload = ExperiencePayload.model_validate_json(self.payload_json)
        if encode(payload) != self.payload_json or content_hash(payload) != self.content_sha256:
            raise ValueError("experience canonical content or hash changed")
        if self.entry_id != "experience-" + self.content_sha256:
            raise ValueError("experience identity must bind all of its content")
        return self

    def contents(self) -> dict[str, Any]:
        return _load(self.payload_json)


class SnapshotRef(_Frozen):
    entry_id: str
    content_sha256: str
    projection_sha256: str

    @model_validator(mode="after")
    def valid_reference(self) -> SnapshotRef:
        require_hash(self.content_sha256)
        require_hash(self.projection_sha256)
        require_identifiers([self.entry_id])
        if self.entry_id != "experience-" + self.content_sha256:
            raise ValueError("snapshot entry identity disagrees with its content hash")
        return self


class PermissionScope(_Frozen):
    use: Literal["solve"] = "solve"
    split: KnowledgeSplit
    excluded_lineage_groups: tuple[Identifier, ...] = Field(default=(), max_length=4096)

    @model_validator(mode="after")
    def valid_permission(self) -> PermissionScope:
        require_identifiers(list(self.excluded_lineage_groups))
        _unique(self.excluded_lineage_groups)
        return self


class RetrieverSpec(_Frozen):
    algorithm: Literal["tags_text_v1"] = "tags_text_v1"
    tag_weight: int = Field(default=2, ge=0)
    text_weight: int = Field(default=1, ge=0)
    max_results: int = Field(default=5, ge=1, le=20)
    max_query_chars: int = Field(default=2048, ge=1, le=65_536)
    tie_breaker: Literal["entry_id"] = "entry_id"
    unknown_tag_policy: Literal["keep"] = "keep"


class SnapshotPayload(_Frozen):
    entries: tuple[SnapshotRef, ...] = Field(max_length=MAX_SNAPSHOT_ENTRIES)
    scope: PermissionScope
    retriever: RetrieverSpec
    skill_files: tuple[str, ...] = Field(default=(), max_length=0)

    @model_validator(mode="after")
    def valid_snapshot(self) -> SnapshotPayload:
        identifiers = [item.entry_id for item in self.entries]
        require_unique(identifiers)
        if identifiers != sorted(identifiers):
            raise ValueError("snapshot entries must be in ascending identity order")
        return self


class KnowledgeSnapshot(_Frozen):
    snapshot_sha256: str
    payload_json: str = Field(min_length=1, max_length=MAX_PAYLOAD_BYTES)

    @model_validator(mode="after")
    def valid_snapshot(self) -> KnowledgeSnapshot:
        require_hash(self.snapshot_sha256)
        _load(self.payload_json)
        payload = SnapshotPayload.model_validate_json(self.payload_json)
        if encode(payload) != self.payload_json or content_hash(payload) != self.snapshot_sha256:
            raise ValueError("knowledge snapshot canonical content or hash changed")
        return self

    def contents(self) -> dict[str, Any]:
        return _load(self.payload_json)
