"""Opt-in, immutable accounting of already authenticated version source history.

This pure adapter does not authenticate a source bundle, read a ledger, publish a
Skill, or enforce a research budget. The caller must first authenticate the
complete copied closure. Existing profiles, reports and manifests are unchanged.
Validation aggregates supply run-cost atoms, not individual model-call records.
"""

from __future__ import annotations

import math
from typing import Any, Literal

from langchain_core.messages import AIMessage
from pydantic import Field, model_validator

from .contracts import require_hash, require_identifiers, require_path
from .feedback_contracts import (
    MAX_CAPSULE_BYTES,
    MAX_ENTRIES,
    MAX_RECORD_BYTES,
    _Frozen,
    _identity,
    _object,
    content_hash,
    encode,
)
from .learning import _cost, _projection
from .learning_contracts import (
    LearningDraftPayload,
    LearningRequest,
    LearningSource,
    LearningSourcePayload,
)
from .model_calls import ModelCallIntentV2, ModelCallRecord, normalize_message, sha256
from .skill_evolution_contracts import (
    ModelVersionPayload,
    parse_plan,
    parse_plan_payload,
    parse_receipt,
    parse_receipt_payload,
    parse_version,
    parse_version_payload,
)
from .skill_transition_contracts import (
    parse_publication,
    parse_publication_payload,
    parse_transition,
    parse_transition_payload,
)

# The source compiler/JSON format use these same 64 MiB / 10,000 / 16 MiB
# ceilings. Import their defining feedback constants to avoid a service cycle.
MAX_BUNDLE_BYTES = MAX_CAPSULE_BYTES
MAX_INVENTORY_ENTRIES = MAX_ENTRIES
MAX_INVENTORY_BYTES = MAX_RECORD_BYTES
Purpose = Literal["candidate_validation", "deployment"]


def _ordered_strings(values: tuple[str, ...], prefix: str) -> None:
    if values != tuple(sorted(set(values))):
        raise ValueError("research references must be unique and canonical")
    for value in values:
        _identity(value, prefix)


def _ordered(values: tuple[Any, ...], keys: list[Any]) -> None:
    if len(values) > MAX_INVENTORY_ENTRIES or keys != sorted(set(keys)):
        raise ValueError("research entries must be bounded, unique and canonical")


def _nonnegative_int(value: Any, label: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError("invalid saved research " + label)
    return value


def _saved_elapsed(row: dict[str, Any]) -> float | None:
    timing = row.get("timing")
    value = timing.get("repair_ms") if isinstance(timing, dict) else None
    if value is None:
        return None
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise ValueError("invalid saved validation elapsed time")
    return float(value)


class ResearchInventoryContext(_Frozen):
    task_id: str
    run_id: str
    purpose: Purpose

    @model_validator(mode="after")
    def valid(self) -> ResearchInventoryContext:
        require_identifiers([self.task_id, self.run_id])
        return self

    @property
    def key(self) -> tuple[str, str, str]:
        return self.task_id, self.run_id, self.purpose


class ResearchVersionReference(_Frozen):
    version_id: str
    learning_source_id: str | None = None

    @model_validator(mode="after")
    def valid(self) -> ResearchVersionReference:
        _identity(self.version_id, "skill-version-")
        if self.learning_source_id is not None:
            _identity(self.learning_source_id, "learning-source-")
        return self


class ResearchLearningSourceCallMember(_Frozen):
    call_id: str
    call_item_sha256: str

    @model_validator(mode="after")
    def valid(self) -> ResearchLearningSourceCallMember:
        require_identifiers([self.call_id])
        require_hash(self.call_item_sha256)
        return self


class ResearchLearningSourceMembership(_Frozen):
    """Captured full membership, not independent proof of source authenticity.

    The adapter captures this from a source authenticated by its caller. The
    inventory binds that capture and checks that no captured call is omitted or
    substituted. A caller-created membership cannot authenticate an origin.
    """

    source_id: str
    operation_id: str
    run_id: str
    owner_sha256: str
    source_binding_sha256: str
    expected_calls: tuple[ResearchLearningSourceCallMember, ...] = Field(min_length=1, max_length=MAX_ENTRIES)
    membership_sha256: str

    def contents(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude={"membership_sha256"})

    @model_validator(mode="after")
    def valid(self) -> ResearchLearningSourceMembership:
        _identity(self.source_id, "learning-source-")
        require_identifiers([self.operation_id, self.run_id])
        require_hash(self.owner_sha256)
        require_hash(self.source_binding_sha256)
        _ordered(self.expected_calls, [item.call_id for item in self.expected_calls])
        if self.membership_sha256 != content_hash(self.contents()):
            raise ValueError("captured research source membership identity changed")
        return self


class LearningCallAtom(_Frozen):
    operation_id: str
    run_id: str
    call_id: str
    owner_sha256: str
    source_binding_sha256: str
    source_ids: tuple[str, ...] = Field(min_length=1, max_length=MAX_ENTRIES)
    call_item_json: str

    @property
    def key(self) -> tuple[str, str, str]:
        return self.operation_id, self.run_id, self.call_id

    @property
    def record(self) -> ModelCallRecord:
        return ModelCallRecord.model_validate_json(encode(_object(self.call_item_json)["record"]))

    @model_validator(mode="after")
    def valid(self) -> LearningCallAtom:
        require_identifiers([self.operation_id, self.run_id, self.call_id])
        require_hash(self.owner_sha256)
        require_hash(self.source_binding_sha256)
        _ordered_strings(self.source_ids, "learning-source-")
        item = _object(self.call_item_json)
        if set(item) != {"request", "record", "response", "learning_content_status"}:
            raise ValueError("learning call atom needs the complete original call item")
        record = self.record
        request = LearningRequest.model_validate_json(encode(item["request"]))
        intent = record.intent
        if (not isinstance(intent, ModelCallIntentV2) or request.intent != intent or
                (intent.task_id, intent.run_id, intent.call_id) != self.key or
                intent.learning_owner_sha256 != self.owner_sha256 or
                intent.source_binding_sha256 != self.source_binding_sha256):
            raise ValueError("learning call atom changed its original owner or request")
        status = item["learning_content_status"]
        if status not in {"valid_experience_content", "invalid_experience_content", "not_completed"}:
            raise ValueError("unknown learning content status")
        if record.state == "completed":
            if record.observation is None or item["response"] is None or status == "not_completed":
                raise ValueError("completed learning atom is missing its original response")
            response = AIMessage.model_validate_json(encode(item["response"]))
            if normalize_message(intent, response) != record.observation or record.uncertainty_reason is not None:
                raise ValueError("learning response differs from its saved observation")
            try:
                _projection(response)
            except ValueError:
                expected_status = "invalid_experience_content"
            else:
                expected_status = "valid_experience_content"
            if status != expected_status:
                raise ValueError("learning content classification differs from its original response")
        elif record.observation is not None or item["response"] is not None or status != "not_completed":
            raise ValueError("uncompleted learning atom cannot claim a completed response")
        return self


class ValidationRunCostAtom(_Frozen):
    task_id: str
    run_id: str
    receipt_ids: tuple[str, ...] = Field(min_length=1, max_length=MAX_ENTRIES)
    row_json: str

    @property
    def key(self) -> tuple[str, str]:
        return self.task_id, self.run_id

    @model_validator(mode="after")
    def valid(self) -> ValidationRunCostAtom:
        require_identifiers([self.task_id, self.run_id])
        _ordered_strings(self.receipt_ids, "skill-receipt-")
        row = _object(self.row_json)
        if (row.get("task_id"), row.get("run_id")) != self.key:
            raise ValueError("validation cost atom changed its saved run identity")
        cost = row.get("cost")
        if cost is not None:
            if not isinstance(cost, dict):
                raise ValueError("validation cost must be an object or unknown")
            reserved = _nonnegative_int(cost.get("reserved_calls"), "reserved calls")
            lower = _nonnegative_int(cost.get("known_total_tokens"), "token lower bound")
            components = {name: _nonnegative_int(cost[name], name) for name in
                ("known_input_tokens", "known_output_tokens") if name in cost}
            if len(components) == 2 and sum(components.values()) != lower:
                raise ValueError("validation known token components differ from their saved sum")
            if any(value > lower for value in components.values()):
                raise ValueError("validation known token component exceeds its saved sum")
            total = cost.get("total_tokens")
            if total is not None and _nonnegative_int(total, "total tokens") < lower:
                raise ValueError("validation total is below its known lower bound")
            if type(cost.get("usage_complete")) is not bool:
                raise ValueError("validation usage completeness is unknown")
            if cost["usage_complete"] and total is None:
                raise ValueError("complete validation usage needs a token total")
            if cost["usage_complete"] and total != lower:
                raise ValueError("complete validation total differs from its known token sum")
            if not cost["usage_complete"] and total is not None:
                raise ValueError("incomplete validation usage cannot claim a token total")
            if reserved == 0 and (lower != 0 or total not in (0, None)):
                raise ValueError("validation tokens cannot exist without reserved calls")
            counters = {name: _nonnegative_int(cost[name], name) for name in
                ("completed_calls", "uncertain_calls", "missing_usage_calls", "invalid_usage_calls",
                 "undispatched_model_calls") if name in cost}
            if any(value > reserved for value in counters.values()):
                raise ValueError("validation call classification exceeds reserved calls")
            # State classes are disjoint. Missing/invalid usage classify completed
            # observations; absent counters remain absent, never inferred as zero.
            states = [counters[name] for name in
                ("completed_calls", "uncertain_calls", "undispatched_model_calls") if name in counters]
            unknown_usage = [counters[name] for name in
                ("missing_usage_calls", "invalid_usage_calls") if name in counters]
            if sum(states) > reserved:
                raise ValueError("validation state classifications exceed reserved calls")
            completed = counters.get("completed_calls")
            if sum(unknown_usage) > (reserved if completed is None else completed):
                raise ValueError("validation usage classifications exceed completed calls")
            unknown_states = [counters[name] for name in
                ("uncertain_calls", "undispatched_model_calls") if name in counters]
            if sum(unknown_usage) + sum(unknown_states) > reserved:
                raise ValueError("validation unknown classifications exceed reserved calls")
            if cost["usage_complete"] and completed is not None and completed != reserved:
                raise ValueError("complete validation usage cannot contain incomplete calls")
            if cost["usage_complete"] and any(counters[name] for name in
                    ("uncertain_calls", "missing_usage_calls", "invalid_usage_calls", "undispatched_model_calls")
                    if name in counters):
                raise ValueError("complete validation usage cannot contain unknown calls")
            if all(name in counters for name in ("completed_calls", "uncertain_calls", "undispatched_model_calls")) and sum(states) != reserved:
                raise ValueError("validation state classifications do not cover reserved calls")
            if all(name in counters for name in ("completed_calls", "missing_usage_calls", "invalid_usage_calls")):
                known_calls = counters["completed_calls"] - sum(unknown_usage)
                if known_calls == 0 and lower != 0:
                    raise ValueError("validation known tokens require a completed call with valid usage")
        _saved_elapsed(row)
        return self


class ResearchValidationSlot(_Frozen):
    plan_id: str
    case_id: str
    role: Literal["old", "new"]
    task_id: str
    run_id: str
    manifest_sha256: str
    receipt_ids: tuple[str, ...] = Field(default=(), max_length=MAX_ENTRIES)

    @property
    def key(self) -> tuple[str, str, str]:
        return self.plan_id, self.case_id, self.role

    @model_validator(mode="after")
    def valid(self) -> ResearchValidationSlot:
        _identity(self.plan_id, "skill-plan-")
        require_identifiers([self.case_id, self.task_id, self.run_id])
        require_hash(self.manifest_sha256)
        _ordered_strings(self.receipt_ids, "skill-receipt-")
        return self


class ResearchValidationPlanSlotMember(_Frozen):
    case_id: str
    role: Literal["old", "new"]
    slot_sha256: str

    @property
    def key(self) -> tuple[str, str]:
        return self.case_id, self.role

    @model_validator(mode="after")
    def valid(self) -> ResearchValidationPlanSlotMember:
        require_identifiers([self.case_id])
        require_hash(self.slot_sha256)
        return self


class ResearchValidationPlanMembership(_Frozen):
    """Bind caller-authenticated plan content and its complete planned matrix.

    This capture detects omissions/substitution relative to the captured matrix.
    Caller-created membership is not independent proof of plan authenticity.
    Receipt references can grow without changing the original planned slots.
    """

    plan_id: str
    plan_content_sha256: str
    expected_slots: tuple[ResearchValidationPlanSlotMember, ...] = Field(min_length=1, max_length=MAX_ENTRIES)
    membership_sha256: str

    def contents(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude={"membership_sha256"})

    @model_validator(mode="after")
    def valid(self) -> ResearchValidationPlanMembership:
        _identity(self.plan_id, "skill-plan-")
        require_hash(self.plan_content_sha256)
        if self.plan_id != "skill-plan-" + self.plan_content_sha256:
            raise ValueError("captured validation plan content identity changed")
        _ordered(self.expected_slots, [item.key for item in self.expected_slots])
        if self.membership_sha256 != content_hash(self.contents()):
            raise ValueError("captured validation plan membership identity changed")
        return self


def _planned_slot_member(slot: ResearchValidationSlot) -> ResearchValidationPlanSlotMember:
    return ResearchValidationPlanSlotMember(case_id=slot.case_id, role=slot.role,
        slot_sha256=content_hash(slot.model_dump(mode="json", exclude={"receipt_ids"})))


class ResearchPublicationEvent(_Frozen):
    publication_id: str
    event_json: str

    @model_validator(mode="after")
    def valid(self) -> ResearchPublicationEvent:
        _identity(self.publication_id, "skill-publication-")
        record = parse_publication(_object(self.event_json))
        if record.publication_id != self.publication_id:
            raise ValueError("research publication event changed identity")
        return self

    @property
    def action(self) -> str:
        return parse_publication_payload(parse_publication(_object(self.event_json)).payload_json).approval.action


class ResearchInventoryStageSummary(_Frozen):
    status: Literal["observed", "not_run", "unknown"]
    known_reserved_calls_lower_bound: int = Field(ge=0)
    reserved_calls: int | None = Field(ge=0)
    known_tokens_lower_bound: int = Field(ge=0)
    total_tokens: int | None = Field(ge=0)
    unknown_calls: int = Field(ge=0)
    elapsed_ms: float | None = Field(default=None, ge=0)
    deadline_ms: None = None
    monetary_cost: None = None
    unknown_reasons: tuple[str, ...] = ()


class ResearchInventorySummary(_Frozen):
    learning: ResearchInventoryStageSummary
    validation: ResearchInventoryStageSummary
    deployment: ResearchInventoryStageSummary
    rollback: ResearchInventoryStageSummary
    total: ResearchInventoryStageSummary
    planned_validation_roles: int = Field(ge=0)
    missing_validation_roles: int = Field(ge=0)
    rollback_event_ids: tuple[str, ...]


def _unknown_stage(reason: str) -> ResearchInventoryStageSummary:
    return ResearchInventoryStageSummary(status="unknown", known_reserved_calls_lower_bound=0,
        reserved_calls=None, known_tokens_lower_bound=0, total_tokens=None, unknown_calls=0,
        unknown_reasons=(reason,))


def _summary(versions: tuple[ResearchVersionReference, ...], calls: tuple[LearningCallAtom, ...],
             runs: tuple[ValidationRunCostAtom, ...], slots: tuple[ResearchValidationSlot, ...],
             events: tuple[ResearchPublicationEvent, ...], absent: bool) -> ResearchInventorySummary:
    if calls:
        cost = _cost([atom.record for atom in calls])
        uncertain = cost.uncertain_calls + cost.missing_usage_calls + cost.invalid_usage_calls
        reasons = tuple(sorted({*(('learning_usage_unknown',) if cost.total_tokens is None else ()),
                                *(('source_inventory_absent',) if absent else ())}))
        learning = ResearchInventoryStageSummary(status="unknown" if reasons else "observed",
            known_reserved_calls_lower_bound=cost.reserved_calls,
            reserved_calls=None if absent else cost.reserved_calls,
            known_tokens_lower_bound=cost.known_lower_bound,
            total_tokens=None if absent else cost.total_tokens, unknown_calls=uncertain,
            unknown_reasons=reasons)
    elif versions and all(version.learning_source_id is None for version in versions) and not absent:
        learning = ResearchInventoryStageSummary(status="not_run", known_reserved_calls_lower_bound=0,
            reserved_calls=0, known_tokens_lower_bound=0, total_tokens=None, unknown_calls=0)
    else:
        learning = _unknown_stage("learning_source_absent")
    missing = sum(not slot.receipt_ids for slot in slots)
    if runs or slots:
        costs = [_object(atom.row_json).get("cost") for atom in runs]
        known_costs = [cost for cost in costs if isinstance(cost, dict)]
        lower = sum(cost["known_total_tokens"] for cost in known_costs)
        call_lower = sum(cost["reserved_calls"] for cost in known_costs)
        reasons_set = set()
        if absent:
            reasons_set.add("source_inventory_absent")
        if missing:
            reasons_set.add("planned_validation_roles_missing")
        if len(known_costs) != len(costs):
            reasons_set.add("validation_cost_absent")
        if any(cost["total_tokens"] is None or not cost["usage_complete"] for cost in known_costs):
            reasons_set.add("validation_usage_unknown")
        if any(any(name not in cost for name in
                ("completed_calls", "uncertain_calls", "missing_usage_calls", "invalid_usage_calls",
                 "undispatched_model_calls")) for cost in known_costs):
            reasons_set.add("validation_call_classification_absent")
        if any(any(name not in cost for name in
                ("known_input_tokens", "known_output_tokens")) for cost in known_costs):
            reasons_set.add("validation_token_components_absent")
        elapsed = [_saved_elapsed(_object(atom.row_json)) for atom in runs]
        validation = ResearchInventoryStageSummary(status="unknown" if reasons_set else "observed",
            known_reserved_calls_lower_bound=call_lower,
            reserved_calls=None if absent or missing or len(known_costs) != len(costs) else call_lower,
            known_tokens_lower_bound=lower,
            total_tokens=None if reasons_set else sum(cost["total_tokens"] for cost in known_costs),
            unknown_calls=sum(cost.get("uncertain_calls", 0) + cost.get("missing_usage_calls", 0) +
                              cost.get("invalid_usage_calls", 0) + cost.get("undispatched_model_calls", 0)
                              for cost in known_costs),
            elapsed_ms=None if absent or missing or any(value is None for value in elapsed) else
                       sum(value for value in elapsed if value is not None),
            unknown_reasons=tuple(sorted(reasons_set)))
    else:
        validation = _unknown_stage("validation_history_absent")
    deployment = _unknown_stage("deployment_cost_unmeasured")
    rollback = _unknown_stage("rollback_cost_unmeasured")
    stages = (learning, validation, deployment, rollback)
    total = ResearchInventoryStageSummary(status="unknown",
        known_reserved_calls_lower_bound=sum(stage.known_reserved_calls_lower_bound for stage in stages),
        reserved_calls=None, known_tokens_lower_bound=sum(stage.known_tokens_lower_bound for stage in stages),
        total_tokens=None, unknown_calls=sum(stage.unknown_calls for stage in stages),
        unknown_reasons=tuple(sorted({"source_history_scope_only", *(reason for stage in stages
            for reason in stage.unknown_reasons), *(('source_inventory_absent',) if absent else ())})))
    return ResearchInventorySummary(learning=learning, validation=validation, deployment=deployment,
        rollback=rollback, total=total, planned_validation_roles=len(slots), missing_validation_roles=missing,
        rollback_event_ids=tuple(sorted(event.publication_id for event in events if event.action == "rollback")))


class ResearchCostInventory(_Frozen):
    inventory_id: str
    content_sha256: str
    scope: Literal["authenticated_source_history"] = "authenticated_source_history"
    application_qualification: Literal["none"] = "none"
    contexts: tuple[ResearchInventoryContext, ...]
    versions: tuple[ResearchVersionReference, ...]
    learning_sources: tuple[ResearchLearningSourceMembership, ...]
    learning_calls: tuple[LearningCallAtom, ...]
    validation_plans: tuple[ResearchValidationPlanMembership, ...]
    validation_runs: tuple[ValidationRunCostAtom, ...]
    validation_slots: tuple[ResearchValidationSlot, ...]
    publications: tuple[ResearchPublicationEvent, ...]
    unknown_inventory_present: bool = False
    summary: ResearchInventorySummary

    def contents(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude={"inventory_id", "content_sha256"})

    @model_validator(mode="after")
    def valid(self) -> ResearchCostInventory:
        _ordered(self.contexts, [item.key for item in self.contexts])
        _ordered(self.versions, [item.version_id for item in self.versions])
        _ordered(self.learning_sources, [item.source_id for item in self.learning_sources])
        _ordered(self.learning_calls, [item.key for item in self.learning_calls])
        _ordered(self.validation_plans, [item.plan_id for item in self.validation_plans])
        _ordered(self.validation_runs, [item.key for item in self.validation_runs])
        _ordered(self.validation_slots, [item.key for item in self.validation_slots])
        _ordered(self.publications, [item.publication_id for item in self.publications])
        if (sum(map(len, (self.contexts, self.versions, self.learning_sources, self.learning_calls, self.validation_plans, self.validation_runs,
                         self.validation_slots, self.publications))) +
                sum(len(source.expected_calls) for source in self.learning_sources) +
                sum(len(plan.expected_slots) for plan in self.validation_plans) > MAX_INVENTORY_ENTRIES):
            raise ValueError("research inventory exceeds entry capacity")
        context_purposes: dict[tuple[str, str], str] = {}
        for context in self.contexts:
            key = (context.task_id, context.run_id)
            if context_purposes.setdefault(key, context.purpose) != context.purpose:
                raise ValueError("research source context changed its original purpose")
        owner_bindings: dict[tuple[str, str], tuple[str, str]] = {}
        for atom in self.learning_calls:
            key = (atom.operation_id, atom.run_id)
            binding = (atom.owner_sha256, atom.source_binding_sha256)
            if owner_bindings.setdefault(key, binding) != binding:
                raise ValueError("learning operation changed its original owner")
        sources = {version.learning_source_id for version in self.versions if version.learning_source_id is not None}
        atom_sources = {source for atom in self.learning_calls for source in atom.source_ids}
        captured_sources = {source.source_id for source in self.learning_sources}
        if sources != atom_sources or sources != captured_sources:
            raise ValueError("research learning source coverage is incomplete")
        for source in self.learning_sources:
            observed_calls = []
            for atom in self.learning_calls:
                if source.source_id not in atom.source_ids:
                    continue
                if ((atom.operation_id, atom.run_id, atom.owner_sha256, atom.source_binding_sha256) !=
                        (source.operation_id, source.run_id, source.owner_sha256, source.source_binding_sha256)):
                    raise ValueError("captured learning source changed its original owner")
                observed_calls.append(ResearchLearningSourceCallMember(call_id=atom.call_id,
                    call_item_sha256=sha256(atom.call_item_json)))
            if tuple(observed_calls) != source.expected_calls:
                raise ValueError("captured learning source has incomplete or changed call membership")
        slot_plans = {slot.plan_id for slot in self.validation_slots}
        if slot_plans != {plan.plan_id for plan in self.validation_plans}:
            raise ValueError("research validation plan coverage is incomplete")
        for plan in self.validation_plans:
            observed_slots = tuple(_planned_slot_member(slot) for slot in self.validation_slots if slot.plan_id == plan.plan_id)
            if observed_slots != plan.expected_slots:
                raise ValueError("captured validation plan has incomplete or changed slot membership")
        receipt_runs: dict[str, tuple[str, str]] = {}
        for atom in self.validation_runs:
            for receipt in atom.receipt_ids:
                if receipt_runs.setdefault(receipt, atom.key) != atom.key:
                    raise ValueError("validation receipt changed its original run")
        slot_receipts: set[str] = set()
        for slot in self.validation_slots:
            for receipt in slot.receipt_ids:
                if receipt in slot_receipts or receipt_runs.get(receipt) != (slot.task_id, slot.run_id):
                    raise ValueError("validation receipt has conflicting planned-role coverage")
                slot_receipts.add(receipt)
        if slot_receipts != set(receipt_runs):
            raise ValueError("validation cost lacks its planned-role provenance")
        expected = _summary(self.versions, self.learning_calls, self.validation_runs, self.validation_slots,
                            self.publications, self.unknown_inventory_present)
        if self.summary != expected:
            raise ValueError("research inventory summary differs from its saved atoms")
        digest = content_hash(self.contents())
        if self.content_sha256 != digest or self.inventory_id != "research-cost-inventory-" + digest:
            raise ValueError("research inventory content identity changed")
        if len(encode(self).encode("utf-8")) > MAX_INVENTORY_BYTES:
            raise ValueError("research inventory exceeds JSON capacity")
        return self


def create_research_cost_inventory(*, contexts: tuple[ResearchInventoryContext, ...] = (),
        versions: tuple[ResearchVersionReference, ...] = (),
        learning_sources: tuple[ResearchLearningSourceMembership, ...] = (), learning_calls: tuple[LearningCallAtom, ...] = (),
        validation_plans: tuple[ResearchValidationPlanMembership, ...] = (),
        validation_runs: tuple[ValidationRunCostAtom, ...] = (), validation_slots: tuple[ResearchValidationSlot, ...] = (),
        publications: tuple[ResearchPublicationEvent, ...] = (), unknown_inventory_present: bool = False
        ) -> ResearchCostInventory:
    """Canonical construction, not source authentication or permission approval."""
    contexts = tuple(sorted(contexts, key=lambda item: item.key))
    versions = tuple(sorted(versions, key=lambda item: item.version_id))
    learning_sources = tuple(sorted(learning_sources, key=lambda item: item.source_id))
    learning_calls = tuple(sorted(learning_calls, key=lambda item: item.key))
    validation_plans = tuple(sorted(validation_plans, key=lambda item: item.plan_id))
    validation_runs = tuple(sorted(validation_runs, key=lambda item: item.key))
    validation_slots = tuple(sorted(validation_slots, key=lambda item: item.key))
    publications = tuple(sorted(publications, key=lambda item: item.publication_id))
    summary = _summary(versions, learning_calls, validation_runs, validation_slots, publications, unknown_inventory_present)
    contents = {"schema_version": 1, "scope": "authenticated_source_history", "application_qualification": "none",
        "contexts": [item.model_dump(mode="json") for item in contexts],
        "versions": [item.model_dump(mode="json") for item in versions],
        "learning_sources": [item.model_dump(mode="json") for item in learning_sources],
        "learning_calls": [item.model_dump(mode="json") for item in learning_calls],
        "validation_plans": [item.model_dump(mode="json") for item in validation_plans],
        "validation_runs": [item.model_dump(mode="json") for item in validation_runs],
        "validation_slots": [item.model_dump(mode="json") for item in validation_slots],
        "publications": [item.model_dump(mode="json") for item in publications],
        "unknown_inventory_present": unknown_inventory_present, "summary": summary.model_dump(mode="json")}
    digest = content_hash(contents)
    return ResearchCostInventory(inventory_id="research-cost-inventory-" + digest, content_sha256=digest,
        contexts=contexts, versions=versions, learning_sources=learning_sources, learning_calls=learning_calls, validation_runs=validation_runs,
        validation_plans=validation_plans,
        validation_slots=validation_slots, publications=publications, unknown_inventory_present=unknown_inventory_present,
        summary=summary)


def union_research_cost_inventories(inventories: tuple[ResearchCostInventory | None, ...]) -> ResearchCostInventory:
    """Union exact cost atoms; absent input is retained as unknown, never zero."""
    if len(inventories) > MAX_INVENTORY_ENTRIES:
        raise ValueError("research inventory union exceeds input capacity")
    contexts: dict[tuple[str, str, str], ResearchInventoryContext] = {}
    versions: dict[str, ResearchVersionReference] = {}
    sources: dict[str, ResearchLearningSourceMembership] = {}
    calls: dict[tuple[str, str, str], LearningCallAtom] = {}
    plans: dict[str, ResearchValidationPlanMembership] = {}
    runs: dict[tuple[str, str], ValidationRunCostAtom] = {}
    slots: dict[tuple[str, str, str], ResearchValidationSlot] = {}
    events: dict[str, ResearchPublicationEvent] = {}
    serialized: dict[str, str] = {}
    input_bytes = 0
    absent = not inventories
    for original in inventories:
        if original is None:
            absent = True
            continue
        raw = original.model_dump_json()
        previous_bytes = serialized.get(original.content_sha256)
        if previous_bytes is not None:
            if previous_bytes != raw:
                raise ValueError("research inventory identity has conflicting serialized evidence")
            continue
        input_bytes += len(raw.encode("utf-8"))
        if input_bytes > MAX_BUNDLE_BYTES:
            raise ValueError("research inventory union exceeds input byte capacity")
        value = ResearchCostInventory.model_validate_json(raw)
        serialized[value.content_sha256] = raw
        absent = absent or value.unknown_inventory_present
        for item in value.contexts:
            contexts[item.key] = item
        for version in value.versions:
            if versions.setdefault(version.version_id, version) != version:
                raise ValueError("research version changed its learning source")
        for source in value.learning_sources:
            if sources.setdefault(source.source_id, source) != source:
                raise ValueError("learning source has conflicting captured call membership")
        for atom in value.learning_calls:
            previous = calls.get(atom.key)
            if previous is not None:
                if (previous.owner_sha256, previous.source_binding_sha256, previous.call_item_json) != (
                        atom.owner_sha256, atom.source_binding_sha256, atom.call_item_json):
                    raise ValueError("learning call has conflicting original evidence")
                atom = LearningCallAtom(**{**atom.model_dump(exclude={"source_ids"}),
                    "source_ids": tuple(sorted(set(previous.source_ids) | set(atom.source_ids)))})
            calls[atom.key] = atom
        for plan in value.validation_plans:
            if plans.setdefault(plan.plan_id, plan) != plan:
                raise ValueError("validation plan has conflicting captured slot membership")
        for atom in value.validation_runs:
            previous_run = runs.get(atom.key)
            if previous_run is not None:
                if previous_run.row_json != atom.row_json:
                    raise ValueError("validation run has conflicting original rows")
                atom = ValidationRunCostAtom(**{**atom.model_dump(exclude={"receipt_ids"}),
                    "receipt_ids": tuple(sorted(set(previous_run.receipt_ids) | set(atom.receipt_ids)))})
            runs[atom.key] = atom
        for slot in value.validation_slots:
            previous_slot = slots.get(slot.key)
            if previous_slot is not None:
                if previous_slot.model_dump(exclude={"receipt_ids"}) != slot.model_dump(exclude={"receipt_ids"}):
                    raise ValueError("validation slot changed its planned run identity")
                slot = ResearchValidationSlot(**{**slot.model_dump(exclude={"receipt_ids"}),
                    "receipt_ids": tuple(sorted(set(previous_slot.receipt_ids) | set(slot.receipt_ids)))})
            slots[slot.key] = slot
        for event in value.publications:
            if events.setdefault(event.publication_id, event) != event:
                raise ValueError("research publication event changed its original evidence")
        if (sum(map(len, (contexts, versions, sources, calls, plans, runs, slots, events))) +
                sum(len(source.expected_calls) for source in sources.values()) +
                sum(len(plan.expected_slots) for plan in plans.values()) > MAX_INVENTORY_ENTRIES):
            raise ValueError("research inventory union exceeds entry capacity")
    return create_research_cost_inventory(contexts=tuple(contexts.values()), versions=tuple(versions.values()),
        learning_sources=tuple(sources.values()), learning_calls=tuple(calls.values()), validation_runs=tuple(runs.values()), validation_slots=tuple(slots.values()),
        validation_plans=tuple(plans.values()),
        publications=tuple(events.values()), unknown_inventory_present=absent)


def inventory_from_authenticated_bundle(files: dict[str, bytes], *, task_id: str, run_id: str,
                                        purpose: Purpose) -> ResearchCostInventory:
    """Derive retained history only; the caller authenticates the entire closure.

    Top-level namespaces include every linked historical version exported by a
    selection. Nested receipt/feedback copies are not separately charged. All
    top-level model versions are consumed even when the selected version is a
    manual ancestor after rollback. No selected ancestry filtering occurs here.
    """
    context = ResearchInventoryContext(task_id=task_id, run_id=run_id, purpose=purpose)
    if type(files) is not dict or len(files) > MAX_INVENTORY_ENTRIES:
        raise ValueError("research bundle exceeds entry capacity")
    total_bytes = 0
    for name, raw in files.items():
        require_path(name)
        if type(raw) is not bytes or len(raw) > MAX_RECORD_BYTES:
            raise ValueError("research bundle has invalid or oversized original bytes")
        total_bytes += len(raw)
        if total_bytes > MAX_BUNDLE_BYTES:
            raise ValueError("research bundle exceeds byte capacity")
    versions: dict[str, Any] = {}
    plans: dict[str, Any] = {}
    plan_hashes: dict[str, str] = {}
    receipts: dict[str, Any] = {}
    transitions: dict[str, Any] = {}
    publications: dict[str, tuple[Any, str]] = {}
    for path, raw in sorted(files.items()):
        parts = path.split("/")
        text = None
        if ((parts[0] in {"versions", "plans"} and len(parts) == 2 and path.endswith(".json")) or
                (parts[0] in {"transitions", "publications"} and len(parts) == 3 and path.endswith(".json")) or
                (parts[0] == "receipts" and len(parts) == 3 and parts[-1] == "receipt.json")):
            text = raw.decode("utf-8")
        if text is None:
            continue
        if parts[0] == "versions":
            record = parse_version(text)
            if path != "versions/" + record.version_id + ".json":
                raise ValueError("research version filename differs from content identity")
            versions[record.version_id] = parse_version_payload(record.payload_json)
        elif parts[0] == "plans":
            record = parse_plan(text)
            if path != "plans/" + record.plan_id + ".json":
                raise ValueError("research plan filename differs from content identity")
            plans[record.plan_id] = parse_plan_payload(record.payload_json)
            plan_hashes[record.plan_id] = record.content_sha256
        elif parts[0] == "receipts":
            record = parse_receipt(text)
            if parts[1] != record.receipt_id:
                raise ValueError("research receipt directory differs from content identity")
            receipts[record.receipt_id] = parse_receipt_payload(record.payload_json)
        elif parts[0] == "transitions":
            record = parse_transition(text)
            if not parts[-1].endswith("-" + record.transition_id + ".json"):
                raise ValueError("research transition filename differs from content identity")
            body = parse_transition_payload(record.payload_json)
            if parts[1] != body.version_id:
                raise ValueError("research transition namespace differs from its original version")
            if transitions.setdefault(record.transition_id, body) != body:
                raise ValueError("research transition has conflicting original evidence")
        else:
            record = parse_publication(text)
            body = parse_publication_payload(record.payload_json)
            if parts[1] != body.approval.alias or parts[-1] != f"{body.sequence:04d}-{record.publication_id}.json":
                raise ValueError("research publication filename differs from content identity")
            item = (body, encode(record))
            if publications.setdefault(record.publication_id, item) != item:
                raise ValueError("research publication has conflicting original evidence")
    if not versions:
        raise ValueError("research bundle has no linked approved versions")
    references, fragments = [], []
    for version_id, body in sorted(versions.items()):
        parent_id = body.approval.parent_version_id
        if parent_id is not None and (parent_id not in versions or versions[parent_id].approval != body.parent_approval):
            raise ValueError("research version lacks its linked original parent")
        source_id = body.learning_source_id if isinstance(body, ModelVersionPayload) else None
        reference = ResearchVersionReference(version_id=version_id, learning_source_id=source_id)
        references.append(reference)
        if not isinstance(body, ModelVersionPayload):
            continue
        source = LearningSource.model_validate_json(files["source-learning/" + body.learning_source_id + "/source.json"])
        payload = LearningSourcePayload.model_validate_json(source.payload_json)
        if source.source_id != source_id or payload.approval != body.learning_approval:
            raise ValueError("research model version has another original learning source")
        draft = LearningDraftPayload.model_validate_json(payload.draft.payload_json)
        if body.approval.source_draft_id != payload.draft.draft_id or body.draft_cost != draft.cost:
            raise ValueError("research model version changed its original draft or cost prefix")
        raw_calls = _object(payload.operation_calls_json).get("calls")
        if not isinstance(raw_calls, list) or not raw_calls or len(raw_calls) > MAX_INVENTORY_ENTRIES:
            raise ValueError("research learning source has incomplete or excessive calls")
        atoms = []
        for item in raw_calls:
            if not isinstance(item, dict):
                raise TypeError("research learning call must be an object")
            record = ModelCallRecord.model_validate_json(encode(item["record"]))
            atoms.append(LearningCallAtom(operation_id=payload.approval.operation_id, run_id=payload.approval.run_id,
                call_id=record.intent.call_id, owner_sha256=content_hash(payload.owner),
                source_binding_sha256=payload.owner.source_binding_sha256, source_ids=(source.source_id,),
                call_item_json=encode(item)))
        if _cost([atom.record for atom in atoms]) != payload.operation_cost or payload.operation_cost != body.operation_cost:
            raise ValueError("research learning costs differ from complete original calls")
        members = tuple(sorted((ResearchLearningSourceCallMember(call_id=atom.call_id,
            call_item_sha256=sha256(atom.call_item_json)) for atom in atoms), key=lambda item: item.call_id))
        capture = {"schema_version": 1, "source_id": source.source_id, "operation_id": payload.approval.operation_id,
            "run_id": payload.approval.run_id, "owner_sha256": content_hash(payload.owner),
            "source_binding_sha256": payload.owner.source_binding_sha256,
            "expected_calls": [member.model_dump(mode="json") for member in members]}
        membership = ResearchLearningSourceMembership.model_validate_json(encode({**capture,
            "membership_sha256": content_hash(capture)}))
        fragments.append(create_research_cost_inventory(contexts=(context,), versions=(reference,),
            learning_sources=(membership,), learning_calls=tuple(atoms)))
    # Each fragment contains one source's full prefix. Union compatible prefixes
    # by call ID, retaining every source reference without charging twice.
    manual = tuple(reference for reference in references if reference.learning_source_id is None)
    if manual:
        fragments.append(create_research_cost_inventory(contexts=(context,), versions=manual))
    slots: dict[tuple[str, str, str], ResearchValidationSlot] = {}
    plan_memberships: list[ResearchValidationPlanMembership] = []
    for plan_id, body in plans.items():
        for identifier, approved in ((body.spec.old_version_id, body.approval.old_version_approval),
                                     (body.spec.new_version_id, body.approval.new_version_approval)):
            if identifier not in versions or versions[identifier].approval != approved:
                raise ValueError("research validation plan lacks its linked original version")
        for pair in body.spec.pairs:
            for role in ("old", "new"):
                manifest_text = getattr(pair, role + "_manifest_json")
                manifest = _object(manifest_text)
                cell = manifest["runs"][0]
                slot = ResearchValidationSlot(plan_id=plan_id, case_id=pair.case_id, role=role,
                    task_id=cell["task_id"], run_id=cell["run_id"], manifest_sha256=sha256(manifest_text))
                slots[slot.key] = slot
        expected_slots = tuple(sorted((_planned_slot_member(slot) for slot in slots.values()
            if slot.plan_id == plan_id), key=lambda item: item.key))
        capture = {"schema_version": 1, "plan_id": plan_id, "plan_content_sha256": plan_hashes[plan_id],
            "expected_slots": [member.model_dump(mode="json") for member in expected_slots]}
        plan_memberships.append(ResearchValidationPlanMembership.model_validate_json(encode({**capture,
            "membership_sha256": content_hash(capture)})))
    runs: dict[tuple[str, str], ValidationRunCostAtom] = {}
    for receipt_id, body in receipts.items():
        key = (body.plan_id, body.case_id, body.role)
        slot = slots.get(key)
        if slot is None or slot.manifest_sha256 != sha256(body.manifest_json):
            raise ValueError("research receipt changed its exact planned role")
        aggregate = _object(body.aggregate_json)
        rows = aggregate.get("rows")
        if not isinstance(rows, list) or len(rows) != 1 or aggregate.get("manifest_sha256") != slot.manifest_sha256:
            raise ValueError("research receipt has no exact single-run aggregate")
        atom = ValidationRunCostAtom(task_id=slot.task_id, run_id=slot.run_id, receipt_ids=(receipt_id,), row_json=encode(rows[0]))
        if slot.receipt_ids:
            raise ValueError("research bundle has conflicting receipts for a planned role")
        slots[key] = ResearchValidationSlot(**{**slot.model_dump(exclude={"receipt_ids"}), "receipt_ids": (receipt_id,)})
        previous_run = runs.get(atom.key)
        if previous_run is not None:
            if previous_run.row_json != atom.row_json:
                raise ValueError("research receipt run has conflicting original rows")
            atom = ValidationRunCostAtom(**{**atom.model_dump(exclude={"receipt_ids"}),
                "receipt_ids": tuple(sorted(set(previous_run.receipt_ids) | set(atom.receipt_ids)))})
        runs[atom.key] = atom
    for body in transitions.values():
        if body.approval.plan_id not in plans or plans[body.approval.plan_id].approval != body.plan_approval:
            raise ValueError("research transition lacks its linked original plan")
        if not set(body.attempt_plan_ids) <= set(plans) or not set(body.history_receipt_ids) <= set(receipts):
            raise ValueError("research transition has incomplete historical cost coverage")
        previous_id = body.approval.previous_transition_id
        if previous_id is not None and (previous_id not in transitions or
                transitions[previous_id].approval != body.previous_approval):
            raise ValueError("research transition lacks its original predecessor")
    events = []
    for publication_id, (body, event_json) in publications.items():
        if body.approval.version_id not in versions or versions[body.approval.version_id].approval != body.version_approval:
            raise ValueError("research publication lacks its historical original version")
        transition_id = body.approval.transition_id
        if transition_id is not None and (transition_id not in transitions or
                transitions[transition_id].approval != body.transition_approval):
            raise ValueError("research publication lacks its historical original transition")
        previous_id = body.approval.previous_publication_id
        if previous_id is not None and (previous_id not in publications or
                publications[previous_id][0].approval != body.previous_approval):
            raise ValueError("research publication lacks its original predecessor")
        events.append(ResearchPublicationEvent(publication_id=publication_id, event_json=event_json))
    fragments.append(create_research_cost_inventory(contexts=(context,), validation_runs=tuple(runs.values()),
        validation_plans=tuple(plan_memberships), validation_slots=tuple(slots.values()), publications=tuple(events)))
    return union_research_cost_inventories(tuple(fragments))
