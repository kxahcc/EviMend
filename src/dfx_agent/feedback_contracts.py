"""Separate host-reviewed feedback candidates; never upgrade knowledge schema 1.

Hashes establish internal consistency. External approvals declare permission;
they do not prove that free text is label-free or that a repair generalizes.
"""

from __future__ import annotations

import json
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .contracts import require_hash, require_identifiers, require_unique
from .knowledge_contracts import KnowledgeSplit, SolverProjection
from .model_calls import canonical, sha256
from .task import require_relative_reference

MAX_RECORD_BYTES = 16_777_216
MAX_CAPSULE_BYTES = 67_108_864
MAX_ENTRIES = 10_000
Identifier = Annotated[str, Field(min_length=1, max_length=128)]
Reference = Annotated[str, Field(min_length=1, max_length=2048)]


def encode(value: BaseModel | dict[str, Any]) -> str:
    data = value.model_dump(mode="json") if isinstance(value, BaseModel) else value
    result = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    if len(result.encode("utf-8")) > MAX_RECORD_BYTES:
        raise ValueError("feedback record exceeds format capacity")
    return result


def content_hash(value: BaseModel | dict[str, Any]) -> str:
    return sha256(encode(value))


def _object(value: str) -> dict[str, Any]:
    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        found: dict[str, Any] = {}
        for key, item in pairs:
            if key in found:
                raise ValueError("duplicate feedback JSON key")
            found[key] = item
        return found

    def invalid(_: str) -> Any:
        raise ValueError("nonfinite feedback JSON")

    if type(value) is not str or len(value.encode("utf-8")) > MAX_RECORD_BYTES:
        raise ValueError("feedback JSON exceeds format capacity")
    try:
        result = json.loads(value, object_pairs_hook=unique, parse_constant=invalid)
    except (RecursionError, json.JSONDecodeError) as exc:
        raise ValueError("invalid feedback JSON") from exc
    if type(result) is not dict or encode(result) != value:
        raise ValueError("feedback JSON must be an exact canonical object")
    return result


def _reference(value: str) -> None:
    require_relative_reference(value)
    if (":" in value or any(ord(char) < 32 for char in value) or
            any(part.endswith((".", " ")) for part in value.split("/"))):
        raise ValueError("unsafe feedback relative path")


def _identity(value: str, prefix: str) -> None:
    if not value.startswith(prefix):
        raise ValueError("invalid feedback content identity")
    require_hash(value.removeprefix(prefix))


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, revalidate_instances="always")
    schema_version: Literal[1] = 1

    @field_validator("schema_version", mode="before")
    @classmethod
    def exact_schema(cls, value: object) -> object:
        if type(value) is not int or value != 1:
            raise ValueError("feedback schema must be integer 1")
        return value


class FeedbackLimits(_Frozen):
    max_file_bytes: int = Field(default=MAX_RECORD_BYTES, gt=0, le=MAX_RECORD_BYTES)
    max_total_bytes: int = Field(default=MAX_CAPSULE_BYTES, gt=0, le=MAX_CAPSULE_BYTES)
    max_entries: int = Field(default=MAX_ENTRIES, gt=0, le=MAX_ENTRIES)
    max_capture_read_bytes: int = Field(default=2 * MAX_CAPSULE_BYTES, gt=0, le=2 * MAX_CAPSULE_BYTES)


class HostFeedbackApproval(_Frozen):
    approval_id: Identifier
    manifest_sha256: str
    origin_split: Literal["development"] = "development"
    allow_learning: Literal[True] = True
    allowed_splits: tuple[KnowledgeSplit, ...] = Field(min_length=1, max_length=3)
    excluded_lineage_groups: tuple[Identifier, ...] = Field(default=(), max_length=4096)

    @field_validator("allow_learning", mode="before")
    @classmethod
    def exact_permission(cls, value: object) -> object:
        if type(value) is not bool or value is not True:
            raise ValueError("feedback learning permission must be boolean true")
        return value

    @model_validator(mode="after")
    def valid_approval(self) -> HostFeedbackApproval:
        require_identifiers([self.approval_id, *self.excluded_lineage_groups])
        require_hash(self.manifest_sha256)
        require_unique(list(self.allowed_splits))
        require_unique(list(self.excluded_lineage_groups))
        return self


class HostCandidateApproval(_Frozen):
    approval_id: Identifier
    capsule_id: str
    projection_sha256: str

    @model_validator(mode="after")
    def valid_approval(self) -> HostCandidateApproval:
        require_identifiers([self.approval_id])
        _identity(self.capsule_id, "feedback-")
        require_hash(self.projection_sha256)
        return self


class FeedbackFile(_Frozen):
    path: Reference
    sha256: str
    bytes: int = Field(ge=0, le=MAX_RECORD_BYTES)

    @model_validator(mode="after")
    def valid_file(self) -> FeedbackFile:
        _reference(self.path)
        require_hash(self.sha256)
        return self


class FeedbackPayload(_Frozen):
    approval: HostFeedbackApproval
    manifest_json: str
    aggregate_json: str
    directories: tuple[Reference, ...] = Field(max_length=MAX_ENTRIES)
    files: tuple[FeedbackFile, ...] = Field(min_length=1, max_length=MAX_ENTRIES)
    source_authority: Literal["recomputed_stopped_run"] = "recomputed_stopped_run"
    permission_authority: Literal["external_host_declaration"] = "external_host_declaration"
    validation_level: Literal["offline_synthetic"] = "offline_synthetic"
    application_qualification: Literal["none"] = "none"
    local_only: Literal[True] = True
    redacted: Literal[False] = False
    share_ready: Literal[False] = False

    @model_validator(mode="after")
    def valid_payload(self) -> FeedbackPayload:
        manifest, aggregate = _object(self.manifest_json), _object(self.aggregate_json)
        if sha256(self.manifest_json) != self.approval.manifest_sha256:
            raise ValueError("feedback manifest differs from external approval")
        if aggregate.get("manifest_sha256") != self.approval.manifest_sha256:
            raise ValueError("feedback aggregate differs from its manifest")
        if (manifest.get("scope") != "offline_synthetic" or
                aggregate.get("scope") != "offline_synthetic"):
            raise ValueError("feedback cannot claim application validation")
        paths = [item.path for item in self.files]
        for value in self.directories:
            _reference(value)
        if (paths != sorted(paths) or list(self.directories) != sorted(self.directories) or
                len({value.casefold() for value in (*paths, *self.directories)}) != len(paths) + len(self.directories) or
                sum(item.bytes for item in self.files) > MAX_CAPSULE_BYTES or
                len(paths) + len(self.directories) > MAX_ENTRIES):
            raise ValueError("feedback inventory has duplicate, unordered or excessive entries")
        return self


class FeedbackCapsule(_Frozen):
    capsule_id: str
    content_sha256: str
    payload_json: str

    @model_validator(mode="after")
    def valid_capsule(self) -> FeedbackCapsule:
        require_hash(self.content_sha256)
        _identity(self.capsule_id, "feedback-")
        payload = FeedbackPayload.model_validate_json(encode(_object(self.payload_json)))
        if (encode(payload) != self.payload_json or content_hash(payload) != self.content_sha256 or
                self.capsule_id != "feedback-" + self.content_sha256):
            raise ValueError("feedback capsule content identity changed")
        return self

    def contents(self) -> dict[str, Any]:
        return _object(self.payload_json)


class CandidatePayload(_Frozen):
    capsule_id: str
    approval: HostCandidateApproval
    feedback_approval_sha256: str
    projection: SolverProjection
    allowed_splits: tuple[KnowledgeSplit, ...] = Field(min_length=1, max_length=3)
    excluded_lineage_groups: tuple[Identifier, ...] = Field(default=(), max_length=4096)
    status: Literal["pending_host_review"] = "pending_host_review"
    application_qualification: Literal["none"] = "none"
    origin_split: Literal["development"] = "development"
    derivation_authority: Literal["host_curated"] = "host_curated"
    historical_validation_authority: Literal["capsule_only_not_generalized"] = "capsule_only_not_generalized"
    learning_model_call_status: Literal["not_run"] = "not_run"
    learning_tokens: None = None
    learning_monetary_cost: None = None

    @model_validator(mode="after")
    def valid_payload(self) -> CandidatePayload:
        _identity(self.capsule_id, "feedback-")
        if self.capsule_id != self.approval.capsule_id:
            raise ValueError("candidate capsule and approval differ")
        require_hash(self.feedback_approval_sha256)
        require_unique(list(self.allowed_splits))
        require_unique(list(self.excluded_lineage_groups))
        require_identifiers(list(self.excluded_lineage_groups))
        if (self.projection.derivation_authority != "host_curated" or self.projection.validation_scopes or
                sha256(canonical(self.projection.model_dump(mode="json"))) != self.approval.projection_sha256):
            raise ValueError("candidate requires the approved host projection without generalized validation")
        return self


class ExperienceCandidate(_Frozen):
    candidate_id: str
    content_sha256: str
    payload_json: str

    @model_validator(mode="after")
    def valid_candidate(self) -> ExperienceCandidate:
        require_hash(self.content_sha256)
        _identity(self.candidate_id, "experience-candidate-")
        payload = CandidatePayload.model_validate_json(encode(_object(self.payload_json)))
        if (encode(payload) != self.payload_json or content_hash(payload) != self.content_sha256 or
                self.candidate_id != "experience-candidate-" + self.content_sha256):
            raise ValueError("experience candidate content identity changed")
        return self

    def contents(self) -> dict[str, Any]:
        return _object(self.payload_json)
