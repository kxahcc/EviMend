"""Deterministic solver projection of an already checked working-context packet.

Only the complete audit source receipt list is replaced by a bounded summary.
Every other task-data field remains byte-for-byte equivalent after canonical
serialization. This pure boundary reads no source, authenticates no receipt,
and makes no new causal or application claim.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .conditions import ConditionRecord
from .contracts import (
    CandidatePatch,
    Diagnosis,
    Evidence,
    RepairObligation,
    require_hash,
    require_identifiers,
    require_path,
)
from .decisions import DecisionRecord
from .model_calls import canonical, sha256
from .policies import Counterexample
from .verification import CheckResult
from .working_context import (
    PROFILE as PACKET_PROFILE,
)
from .working_context import (
    WorkingContextError,
    WorkingContextFocus,
    WorkingContextPacket,
    _load,
)

PROFILE = "repair_decision_context_v2"
_PACKET_KEYS = frozenset({
    "schema_version", "profile", "task_id", "run_id", "source_snapshot_sha256", "focus",
    "focus_authority", "focus_recency", "root_cause_confirmation", "application_verification",
    "tool_action_authentication", "diagnoses", "evidence", "obligations", "candidates", "checks",
    "decisions", "feedback", "conditions", "historical_relevance", "sources", "interpretation",
})
_PROJECTION_KEYS = (_PACKET_KEYS - {"sources"}) | {"audit_packet_sha256", "audit_sources"}


class _AuditSource(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    reference: str
    bytes: int = Field(ge=0)
    sha256: str

    @model_validator(mode="after")
    def valid_source(self) -> _AuditSource:
        scope, separator, relative = self.reference.partition("/")
        if separator != "/" or scope not in {"workspace", "task"}:
            raise ValueError("invalid audit source scope")
        require_path(relative)
        require_hash(self.sha256)
        return self


class _AuditSourcesSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    source_count: int = Field(ge=0)
    total_reference_bytes: int = Field(ge=0)
    manifest_sha256: str

    @model_validator(mode="after")
    def valid_manifest(self) -> _AuditSourcesSummary:
        require_hash(self.manifest_sha256)
        return self


def _budget(max_bytes: int) -> None:
    if type(max_bytes) is not int or max_bytes <= 0:
        raise ValueError("max_bytes must be a positive integer")


def _semantic_shape(value: dict[str, Any]) -> None:
    """Check syntax/ownership; the caller's full packet verifier checks sources."""
    require_identifiers([value["task_id"], value["run_id"]])
    snapshot = value["source_snapshot_sha256"]
    require_hash(snapshot)
    WorkingContextFocus.model_validate_json(canonical(value["focus"]))
    fixed = {
        "focus_authority": "explicit_host_input", "focus_recency": "not_authenticated",
        "root_cause_confirmation": "unknown", "application_verification": "not_run",
        "tool_action_authentication": "not_performed",
    }
    if any(value[key] != expected for key, expected in fixed.items()):
        raise ValueError("working context declarations changed")
    if not isinstance(value["interpretation"], str) or not value["interpretation"]:
        raise ValueError("working context interpretation must be text")

    for field, cls, identity in (
        ("diagnoses", Diagnosis, "record_id"), ("evidence", Evidence, "record_id"),
        ("obligations", RepairObligation, "record_id"), ("candidates", CandidatePatch, "candidate_id"),
        ("decisions", DecisionRecord, "record_id"),
    ):
        records = value[field]
        if not isinstance(records, dict):
            raise TypeError("working context records must be objects")
        for identifier, raw in records.items():
            cls.model_validate_json(canonical(raw))
            if (raw[identity] != identifier or raw["task_id"] != value["task_id"] or
                    raw["run_id"] != value["run_id"]):
                raise ValueError("working context record owner or identity differs")
            if raw.get("source_snapshot_sha256") != snapshot and not (
                field == "evidence" and raw["kind"] == "log_observation" and
                raw.get("source_snapshot_sha256") is None
            ):
                raise ValueError("working context record baseline differs")

    if not isinstance(value["checks"], dict):
        raise TypeError("working context checks must be an object")
    for identifier, check in value["checks"].items():
        require_hash(identifier)
        if (not isinstance(check, dict) or set(check) != {
            "state", "candidate_id", "candidate_snapshot_sha256", "result"
        } or check["candidate_id"] != identifier or
                check["state"] not in {"not_requested", "pending", "completed"}):
            raise ValueError("working context check shape differs")
        require_hash(check["candidate_snapshot_sha256"])
        if check["result"] is not None:
            result = CheckResult.model_validate_json(canonical(check["result"]))
            if result.candidate_id != identifier or check["state"] != "completed":
                raise ValueError("working context check result scope differs")
        elif check["state"] == "completed":
            raise ValueError("completed working context check lacks its result")

    if not isinstance(value["conditions"], dict) or not isinstance(value["feedback"], dict):
        raise TypeError("working context scoped records must be objects")
    for identifier, raw in value["conditions"].items():
        condition = ConditionRecord.model_validate_json(canonical(raw))
        scope = condition.result.request.scope
        if (condition.result_id != identifier or scope.task_id != value["task_id"] or
                scope.run_id != value["run_id"] or scope.source_snapshot_sha256 != snapshot):
            raise ValueError("working context condition owner or baseline differs")
    for identifier, raw in value["feedback"].items():
        require_hash(identifier)
        Counterexample.model_validate_json(canonical(raw))
    if not isinstance(value["historical_relevance"], dict):
        raise TypeError("historical relevance must be an object")
    for identifier, relevance in value["historical_relevance"].items():
        require_identifiers([identifier])
        if (not isinstance(relevance, dict) or set(relevance) != {
            "shared_obligation_ids", "authority", "semantic_relevance", "observed_candidate_id",
            "current_candidate_id", "is_current_candidate_observation",
        } or relevance["authority"] != "host_reference_overlap" or relevance["semantic_relevance"] != "unknown" or
                type(relevance["is_current_candidate_observation"]) is not bool):
            raise ValueError("historical relevance scope differs")
        require_identifiers(relevance["shared_obligation_ids"])
        require_hash(relevance["observed_candidate_id"])
        if relevance["current_candidate_id"] is not None:
            require_hash(relevance["current_candidate_id"])


class SolverContextProjection(BaseModel):
    """Frozen canonical view; contents() never exposes mutable stored state."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    schema_version: Literal[2] = 2
    profile: Literal["repair_decision_context_v2"] = PROFILE
    canonical_payload_json: str
    payload_sha256: str
    audit_packet_sha256: str
    max_bytes: int = Field(gt=0)

    @model_validator(mode="after")
    def consistent(self) -> SolverContextProjection:
        require_hash(self.payload_sha256)
        require_hash(self.audit_packet_sha256)
        value = _load(self.canonical_payload_json)
        if (not isinstance(value, dict) or set(value) != _PROJECTION_KEYS or
                canonical(value) != self.canonical_payload_json or
                sha256(self.canonical_payload_json) != self.payload_sha256 or
                type(value["schema_version"]) is not int or value["schema_version"] != 2 or
                value["profile"] != PROFILE or value["audit_packet_sha256"] != self.audit_packet_sha256):
            raise ValueError("invalid solver context projection payload")
        _AuditSourcesSummary.model_validate_json(canonical(value["audit_sources"]))
        _semantic_shape(value)
        if len(self.canonical_payload_json.encode("utf-8")) > self.max_bytes:
            raise WorkingContextError("working_context_required_limit")
        return self

    def contents(self) -> dict[str, Any]:
        return _load(self.canonical_payload_json)


def project_working_context(verified_packet: WorkingContextPacket, *, max_bytes: int) -> SolverContextProjection:
    """Project a caller-verified packet without expanding or interpreting facts.

    ``verified_packet`` names a precondition, not a new authority: this pure
    function validates package syntax, identities and hashes, not source truth.
    ``total_reference_bytes`` counts logical references, not physical storage or
    model Tokens. The full source list remains in the original audit packet.
    """
    _budget(max_bytes)
    try:
        checked = WorkingContextPacket.model_validate_json(verified_packet.model_dump_json())
        value = checked.contents()
        if (set(value) != _PACKET_KEYS or type(value["schema_version"]) is not int or
                value["schema_version"] != 1 or value["profile"] != PACKET_PROFILE):
            raise ValueError("unsupported working context audit packet")
        _semantic_shape(value)
        sources = value.pop("sources")
        if not isinstance(sources, list):
            raise TypeError("audit source list must be an array")
        validated = [_AuditSource.model_validate_json(canonical(source)) for source in sources]
        references = [source.reference for source in validated]
        if references != sorted(references) or len(references) != len(set(references)):
            raise ValueError("audit source references must be unique and sorted")
        value.update(schema_version=2, profile=PROFILE, audit_packet_sha256=checked.payload_sha256,
                     audit_sources={"source_count": len(validated),
                         "total_reference_bytes": sum(source.bytes for source in validated),
                         "manifest_sha256": sha256(canonical(sources))})
        payload = canonical(value)
        if len(payload.encode("utf-8")) > max_bytes:
            raise WorkingContextError("working_context_required_limit")
        return SolverContextProjection(canonical_payload_json=payload, payload_sha256=sha256(payload),
                                       audit_packet_sha256=checked.payload_sha256, max_bytes=max_bytes)
    except WorkingContextError:
        raise
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        raise WorkingContextError("working_context_projection_invalid") from exc


def verify_solver_projection(projection: SolverContextProjection, packet: WorkingContextPacket, *,
                             max_bytes: int) -> SolverContextProjection:
    """Reproject this same audit packet and compare all frozen output fields."""
    _budget(max_bytes)
    expected = project_working_context(packet, max_bytes=max_bytes)
    try:
        checked = SolverContextProjection.model_validate_json(projection.model_dump_json())
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        raise WorkingContextError("working_context_projection_invalid") from exc
    if checked != expected:
        raise WorkingContextError("working_context_projection_mismatch")
    return checked
