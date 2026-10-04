"""Frozen, source-bound assessments of historical guidance in a current task.

These contracts establish shape and internal consistency. Registered sources
and current evidence must still be reread by the host runtime. A model's premise
assessment remains an inference, including when its status is supported.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field, model_validator

from .contracts import require_hash, require_identifiers, require_path, require_unique
from .knowledge_contracts import (
    MAX_CLOSURE_BYTES,
    MAX_PAYLOAD_BYTES,
    Identifier,
    PermissionScope,
    RelativePath,
    SolverProjection,
    Text,
    _Frozen,
    _load,
    _text,
    content_hash,
    encode,
)
from .working_context import WorkingContextPacket

MAX_CONTEXT_FILES = 4096
MAX_CONTEXT_BYTES = MAX_CLOSURE_BYTES
MAX_CONTEXT_READ_BYTES = 4 * MAX_CONTEXT_BYTES
MAX_CONTEXT_READ_CALLS = 16_384
AssessmentStatus = Literal["supported", "contradicted", "unknown"]
NextAction = Literal["offer_process", "avoid_copy", "clarify"]
_WINDOWS_DEVICES = {"con", "prn", "aux", "nul"} | {
    prefix + suffix for prefix in ("com", "lpt") for suffix in (*map(str, range(1, 10)), "¹", "²", "³")
}


def _record_id(value: str, prefix: str, digest: str | None = None) -> None:
    require_identifiers([value])
    if not value.startswith(prefix):
        raise ValueError("record reference requires its content-addressed prefix")
    require_hash(value[len(prefix):])
    if digest is not None and value != prefix + digest:
        raise ValueError("record identity disagrees with its content hash")


def _candidate(identifier: str | None, snapshot: str | None) -> None:
    if (identifier is None) != (snapshot is None):
        raise ValueError("candidate identity requires its snapshot hash")
    if identifier is not None:
        require_hash(identifier)
        require_hash(snapshot or "")


def _record(payload_json: str, digest: str, identifier: str, prefix: str, payload_type: type[_Frozen]) -> None:
    require_hash(digest)
    _record_id(identifier, prefix, digest)
    _load(payload_json)
    payload = payload_type.model_validate_json(payload_json)
    if encode(payload) != payload_json or content_hash(payload) != digest:
        raise ValueError("record canonical content or hash changed")


class TaskKnowledgeBinding(_Frozen):
    task_id: Identifier
    run_id: Identifier
    source_snapshot_sha256: str
    crash_sha256: str
    snapshot_sha256: str
    scope: PermissionScope

    @model_validator(mode="after")
    def valid_binding(self) -> TaskKnowledgeBinding:
        require_identifiers([self.task_id, self.run_id])
        for digest in (self.source_snapshot_sha256, self.crash_sha256, self.snapshot_sha256):
            require_hash(digest)
        return self


class BoundQueryPayload(_Frozen):
    binding_sha256: str
    retrieval_id: str
    retrieval_sha256: str

    @model_validator(mode="after")
    def valid_payload(self) -> BoundQueryPayload:
        require_hash(self.binding_sha256)
        require_hash(self.retrieval_sha256)
        _record_id(self.retrieval_id, "retrieval-", self.retrieval_sha256)
        return self


class BoundQuery(_Frozen):
    record_id: str
    content_sha256: str
    payload_json: str = Field(min_length=1, max_length=MAX_PAYLOAD_BYTES)

    @model_validator(mode="after")
    def valid_record(self) -> BoundQuery:
        _record(self.payload_json, self.content_sha256, self.record_id, "bound-query-", BoundQueryPayload)
        return self

    def contents(self) -> dict[str, Any]:
        return _load(self.payload_json)


class ContextMaterial(_Frozen):
    reference: RelativePath
    sha256: str
    bytes: int = Field(ge=0, le=MAX_PAYLOAD_BYTES)

    @model_validator(mode="after")
    def valid_material(self) -> ContextMaterial:
        require_path(self.reference)
        parts = self.reference.split("/")
        if len(parts) < 2 or parts[0] not in ("task", "workspace"):
            raise ValueError("context material must belong to the task or workspace")
        for part in parts:
            if (part.endswith((".", " ")) or any(ord(char) < 32 or char in '<>"|?*' for char in part)
                    or part.split(".", 1)[0].casefold() in _WINDOWS_DEVICES):
                raise ValueError("context material path has ambiguous platform spelling")
        require_hash(self.sha256)
        return self


class ContextPayload(_Frozen):
    binding_sha256: str
    bound_query_id: str
    packet: WorkingContextPacket
    materials: tuple[ContextMaterial, ...] = Field(max_length=MAX_CONTEXT_FILES)

    @model_validator(mode="after")
    def valid_payload(self) -> ContextPayload:
        require_hash(self.binding_sha256)
        _record_id(self.bound_query_id, "bound-query-")
        # The existing packet class is frozen but does not revalidate instances.
        # Reparse its canonical representation to reject constructed/mutated input.
        WorkingContextPacket.model_validate_json(encode(self.packet))
        references = tuple(item.reference for item in self.materials)
        require_unique([item.casefold() for item in references])
        if references != tuple(sorted(references)):
            raise ValueError("context materials require ascending reference order")
        if sum(item.bytes for item in self.materials) > MAX_CONTEXT_BYTES:
            raise ValueError("context materials exceed the shared byte limit")
        return self


class ApplicabilityContext(_Frozen):
    payload_sha256: str
    context_id: str
    payload_json: str = Field(min_length=1, max_length=MAX_PAYLOAD_BYTES)

    @model_validator(mode="after")
    def valid_record(self) -> ApplicabilityContext:
        _record(self.payload_json, self.payload_sha256, self.context_id, "app-context-", ContextPayload)
        return self

    def contents(self) -> dict[str, Any]:
        return _load(self.payload_json)


class EvidenceUse(_Frozen):
    evidence_id: Identifier
    scope: Literal["original_analysis", "current_candidate"]
    candidate_id: str | None = None
    candidate_snapshot_sha256: str | None = None

    @model_validator(mode="after")
    def valid_use(self) -> EvidenceUse:
        require_identifiers([self.evidence_id])
        _candidate(self.candidate_id, self.candidate_snapshot_sha256)
        if (self.scope == "original_analysis") != (self.candidate_id is None):
            raise ValueError("evidence use scope disagrees with candidate identity")
        return self


class PremiseAssessment(_Frozen):
    premise_id: Identifier
    status: AssessmentStatus
    support: tuple[EvidenceUse, ...] = Field(default=(), max_length=4096)
    contradictions: tuple[EvidenceUse, ...] = Field(default=(), max_length=4096)
    reason: Text

    @model_validator(mode="after")
    def valid_assessment(self) -> PremiseAssessment:
        require_identifiers([self.premise_id])
        _text(self.reason)
        support_ids = [item.evidence_id for item in self.support]
        contra_ids = [item.evidence_id for item in self.contradictions]
        require_unique(support_ids)
        require_unique(contra_ids)
        if set(support_ids) & set(contra_ids):
            raise ValueError("the same evidence cannot support and contradict one premise")
        if self.status == "supported" and (not self.support or self.contradictions):
            raise ValueError("a supported premise needs support and no contradictions")
        if self.status == "contradicted" and not self.contradictions:
            raise ValueError("a contradicted premise needs contradictory evidence")
        return self


class ApplicabilityProposal(_Frozen):
    context_id: str
    entry_id: str
    content_sha256: str
    projection_sha256: str
    task_id: Identifier
    run_id: Identifier
    source_snapshot_sha256: str
    diagnosis_id: Identifier
    hypothesis_ids: tuple[Identifier, ...] = Field(min_length=1, max_length=4096)
    candidate_id: str | None = None
    candidate_snapshot_sha256: str | None = None
    premises: tuple[PremiseAssessment, ...] = Field(min_length=1, max_length=128)

    @model_validator(mode="after")
    def valid_proposal(self) -> ApplicabilityProposal:
        _record_id(self.context_id, "app-context-")
        require_hash(self.content_sha256)
        require_hash(self.projection_sha256)
        require_hash(self.source_snapshot_sha256)
        _record_id(self.entry_id, "experience-", self.content_sha256)
        require_identifiers([self.task_id, self.run_id, self.diagnosis_id, *self.hypothesis_ids])
        require_unique(list(self.hypothesis_ids))
        require_unique([item.premise_id for item in self.premises])
        _candidate(self.candidate_id, self.candidate_snapshot_sha256)
        for premise in self.premises:
            for use in (*premise.support, *premise.contradictions):
                if use.scope == "current_candidate" and (
                    use.candidate_id != self.candidate_id or
                    use.candidate_snapshot_sha256 != self.candidate_snapshot_sha256
                ):
                    raise ValueError("candidate evidence use differs from the proposal focus")
        return self


class PremiseQuestion(_Frozen):
    premise_id: Identifier
    question: Text

    @model_validator(mode="after")
    def valid_question(self) -> PremiseQuestion:
        require_identifiers([self.premise_id])
        _text(self.question)
        return self


class ReviewPayload(_Frozen):
    binding: TaskKnowledgeBinding
    context_id: str
    bound_query_id: str
    retrieval_id: str
    proposal: ApplicabilityProposal
    projection: SolverProjection
    status: AssessmentStatus
    next_action: NextAction
    questions: tuple[PremiseQuestion, ...] = Field(max_length=128)
    obligation_ids: tuple[Identifier, ...] = Field(max_length=4096)
    semantic_authority: Literal["model_inference"] = "model_inference"
    current_evidence_authentication: Literal["registered_frozen_sources"] = "registered_frozen_sources"
    focus_recency: Literal["explicit_host_input_not_authenticated"] = "explicit_host_input_not_authenticated"
    application_verification: Literal["not_run"] = "not_run"

    @model_validator(mode="after")
    def valid_review(self) -> ReviewPayload:
        _record_id(self.context_id, "app-context-")
        _record_id(self.bound_query_id, "bound-query-")
        _record_id(self.retrieval_id, "retrieval-")
        proposal = self.proposal
        if (proposal.context_id != self.context_id or proposal.task_id != self.binding.task_id or
                proposal.run_id != self.binding.run_id or
                proposal.source_snapshot_sha256 != self.binding.source_snapshot_sha256):
            raise ValueError("applicability proposal belongs to another task or context")
        if content_hash(self.projection) != proposal.projection_sha256:
            raise ValueError("applicability projection differs from its proposal hash")
        guidance_premises = self.projection.guidance.premises
        if tuple(item.premise_id for item in proposal.premises) != tuple(
                item.premise_id for item in guidance_premises):
            raise ValueError("applicability proposal must cover every guidance premise in order")
        states = {item.status for item in proposal.premises}
        expected: AssessmentStatus = "contradicted" if "contradicted" in states else (
            "unknown" if "unknown" in states else "supported")
        action: NextAction = "avoid_copy" if expected == "contradicted" else (
            "clarify" if expected == "unknown" else "offer_process")
        if self.status != expected or self.next_action != action:
            raise ValueError("review status or action disagrees with complete premise assessments")
        questions = tuple((item.premise_id, item.question) for item in self.questions)
        required_questions = tuple((premise.premise_id, premise.unknown_question)
                                   for premise, assessment in zip(guidance_premises, proposal.premises, strict=True)
                                   if assessment.status == "unknown")
        if questions != required_questions:
            raise ValueError("unknown premise questions must retain the complete original wording and order")
        require_identifiers(list(self.obligation_ids))
        require_unique(list(self.obligation_ids))
        if self.obligation_ids != tuple(sorted(self.obligation_ids)):
            raise ValueError("review obligations require ascending identity order")
        return self


class ApplicabilityReview(_Frozen):
    record_id: str
    content_sha256: str
    payload_json: str = Field(min_length=1, max_length=MAX_PAYLOAD_BYTES)

    @model_validator(mode="after")
    def valid_record(self) -> ApplicabilityReview:
        _record(self.payload_json, self.content_sha256, self.record_id, "app-review-", ReviewPayload)
        return self

    def contents(self) -> dict[str, Any]:
        return _load(self.payload_json)
