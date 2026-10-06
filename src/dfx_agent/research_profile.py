"""Pure, conservative cost profile reconstruction across agent stages.

``ResearchProfile`` is deliberately a data contract.  It does not open a
ledger, inspect a workspace, or infer that an application was deployed.  A
caller supplies a stopped completion/report (and, optionally, explicitly
saved learning/validation facts); absent evidence is represented as unknown.
This keeps the profile useful for later cross-operation aggregation without
changing the meaning of the existing session and manifest schemas.
"""

from __future__ import annotations

import math
from typing import Any, Literal

from pydantic import Field, model_validator

from .contracts import IDENTIFIER
from .feedback_contracts import _Frozen, content_hash

StageStatus = Literal["observed", "not_run", "unknown", "inconclusive"]
DEFAULT_STAGES = ("learning", "validation", "deployment", "rollback")


def _identifier(value: str, label: str) -> None:
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise ValueError(f"invalid {label}")


def _identifiers(values: tuple[str, ...], label: str) -> None:
    if len(values) != len(set(values)) or tuple(sorted(values)) != values:
        raise ValueError(f"{label} must be unique and canonical")
    for value in values:
        if not isinstance(value, str) or not value or len(value) > 512:
            raise ValueError(f"invalid {label}")


def _finite_nonnegative(value: float | None, label: str) -> None:
    if value is not None and (not math.isfinite(value) or value < 0):
        raise ValueError(f"{label} must be a finite nonnegative number")


class ResearchStageCost(_Frozen):
    """One explicitly evidenced stage; no field is filled from a guess."""

    stage: str
    status: StageStatus
    reserved_calls: int | None = Field(default=None, ge=0)
    known_tokens_lower_bound: int = Field(default=0, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)
    unknown_calls: int = Field(default=0, ge=0)
    elapsed_ms: float | None = Field(default=None, ge=0)
    deadline_ms: float | None = Field(default=None, ge=0)
    source_ids: tuple[str, ...] = ()
    dedup_keys: tuple[str, ...] = ()
    unknown_reasons: tuple[str, ...] = ()

    @model_validator(mode="after")
    def valid(self) -> ResearchStageCost:
        _identifier(self.stage, "research stage")
        _identifiers(self.source_ids, "source IDs")
        _identifiers(self.dedup_keys, "dedup keys")
        _identifiers(self.unknown_reasons, "unknown reasons")
        _finite_nonnegative(self.elapsed_ms, "elapsed_ms")
        _finite_nonnegative(self.deadline_ms, "deadline_ms")
        if self.total_tokens is not None and self.total_tokens < self.known_tokens_lower_bound:
            raise ValueError("total tokens cannot be below the known lower bound")
        if self.status == "observed" and self.unknown_calls:
            raise ValueError("an observed stage cannot contain unknown calls")
        if self.status == "unknown" and not self.unknown_reasons:
            raise ValueError("unknown stage must retain a reason")
        if self.total_tokens is None and self.status == "observed" and self.reserved_calls is not None:
            raise ValueError("observed stage with calls needs a total token value")
        return self


class ResearchCostSummary(_Frozen):
    """Conservative sum of stage costs; ``None`` means the total is unknown."""

    reserved_calls: int | None = Field(default=None, ge=0)
    known_tokens_lower_bound: int = Field(default=0, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)
    unknown_calls: int = Field(default=0, ge=0)
    elapsed_ms: float | None = Field(default=None, ge=0)
    deadline_ms: float | None = Field(default=None, ge=0)
    unknown_reasons: tuple[str, ...] = ()
    monetary_cost: None = None

    @model_validator(mode="after")
    def valid(self) -> ResearchCostSummary:
        _identifiers(self.unknown_reasons, "unknown reasons")
        _finite_nonnegative(self.elapsed_ms, "elapsed_ms")
        _finite_nonnegative(self.deadline_ms, "deadline_ms")
        if self.total_tokens is not None and self.total_tokens < self.known_tokens_lower_bound:
            raise ValueError("total tokens cannot be below the known lower bound")
        return self


class ResearchProfile(_Frozen):
    """Cross-stage evidence attached to one task/run lineage.

    ``lineage_group_id`` is optional because completion reports from older
    schemas do not contain the evaluation manifest's lineage.  It remains
    ``None`` until a manifest supplies the identity; it is never guessed from
    a task ID or a path.
    """

    schema_version: Literal[1] = 1
    profile_id: str
    content_sha256: str
    task_id: str
    run_id: str
    lineage_group_id: str | None = None
    case_key: str | None = None
    stages: tuple[ResearchStageCost, ...] = ()
    total: ResearchCostSummary
    source_ids: tuple[str, ...] = ()
    dedup_keys: tuple[str, ...] = ()

    @model_validator(mode="after")
    def valid(self) -> ResearchProfile:
        _identifier(self.profile_id, "profile ID")
        if not self.profile_id.startswith("research-profile-"):
            raise ValueError("profile ID has the wrong prefix")
        _identifier(self.task_id, "task ID")
        _identifier(self.run_id, "run ID")
        if self.lineage_group_id is not None:
            _identifier(self.lineage_group_id, "lineage group ID")
        _identifiers(self.source_ids, "profile source IDs")
        _identifiers(self.dedup_keys, "profile dedup keys")
        names = [stage.stage for stage in self.stages]
        if len(names) != len(set(names)) or names != sorted(names):
            raise ValueError("profile stages must be unique and canonical")
        unsigned = self.model_dump(mode="json", exclude={"profile_id", "content_sha256"})
        if self.content_sha256 != content_hash(unsigned):
            raise ValueError("research profile content identity changed")
        if self.profile_id != "research-profile-" + self.content_sha256:
            raise ValueError("research profile ID differs from its content")
        return self

    def contents(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude={"profile_id", "content_sha256"})


def _int(value: Any, default: int | None = None) -> int | None:
    return value if type(value) is int and value >= 0 else default


def _float(value: Any) -> float | None:
    valid = isinstance(value, (int, float)) and not isinstance(value, bool)
    return value if valid and math.isfinite(value) and value >= 0 else None


def _tuple_strings(value: Any) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(sorted({item for item in value if isinstance(item, str) and item}))


def _cost_stage(stage: str, raw: Any, *, source_ids: tuple[str, ...] = (),
                dedup_keys: tuple[str, ...] = (), elapsed_ms: float | None = None,
                deadline_ms: float | None = None) -> ResearchStageCost:
    """Convert an already persisted cost object without widening its facts."""
    if not isinstance(raw, dict):
        return ResearchStageCost(stage=stage, status="unknown", source_ids=source_ids,
                                 dedup_keys=dedup_keys, elapsed_ms=elapsed_ms, deadline_ms=deadline_ms,
                                 unknown_reasons=("cost_evidence_absent",))
    reserved = _int(raw.get("reserved_calls"), _int(raw.get("known_reserved_calls")))
    lower = _int(raw.get("known_total_tokens"), _int(raw.get("known_tokens_lower_bound"), 0)) or 0
    total = _int(raw.get("total_tokens"), _int(raw.get("tokens")))
    uncertain = (_int(raw.get("uncertain_calls"), 0) or 0) + (_int(raw.get("missing_usage_calls"), 0) or 0)
    uncertain += _int(raw.get("invalid_usage_calls"), 0) or 0
    status_value = raw.get("status")
    if status_value == "not_run":
        return ResearchStageCost(stage=stage, status="not_run", reserved_calls=0, source_ids=source_ids,
                                 dedup_keys=dedup_keys, elapsed_ms=elapsed_ms, deadline_ms=deadline_ms)
    reasons = set(_tuple_strings(raw.get("unknown_reasons")))
    if uncertain or raw.get("usage_complete") is False or total is None:
        reasons.add("cost_total_unknown" if total is None else "call_usage_unknown")
    status: StageStatus = "unknown" if reasons else "observed"
    if status == "unknown" and not reasons:
        reasons.add("cost_evidence_unknown")
    return ResearchStageCost(stage=stage, status=status, reserved_calls=reserved,
                             known_tokens_lower_bound=lower, total_tokens=total if not reasons else None,
                             unknown_calls=uncertain, source_ids=source_ids, dedup_keys=dedup_keys,
                             unknown_reasons=tuple(sorted(reasons)), elapsed_ms=elapsed_ms, deadline_ms=deadline_ms)


def _version_learning_stages(value: Any) -> list[ResearchStageCost]:
    """Read source costs from version Skill facts, deduplicating operations."""
    if not isinstance(value, dict):
        return []
    found: dict[str, ResearchStageCost] = {}
    resources = value.get("resources", value.get("files", ()))
    if isinstance(resources, dict):
        resources = resources.values()
    if not isinstance(resources, (list, tuple)):
        return []
    for resource in resources:
        if not isinstance(resource, dict):
            continue
        costs = resource.get("source_costs")
        ancestry = costs.get("ancestry") if isinstance(costs, dict) else None
        operations = ancestry.get("operations", ()) if isinstance(ancestry, dict) else ()
        if not isinstance(operations, (list, tuple)):
            continue
        for operation in operations:
            if not isinstance(operation, dict):
                continue
            operation_id, run_id = operation.get("operation_id"), operation.get("run_id")
            if not isinstance(operation_id, str) or not isinstance(run_id, str):
                continue
            # _learning_costs_from_bundle has already de-duplicated every
            # persisted call ID while closing the source tree.  The portable
            # summary intentionally exposes that operation-level result, not
            # the private call ledger; retain owner/source binding in the
            # dedup identity so two grants cannot be charged as one merely
            # because they share an operation/run pair.
            owner = operation.get("owner_sha256")
            binding = operation.get("source_binding_sha256")
            if not isinstance(owner, str) or not isinstance(binding, str):
                continue
            key = f"{operation_id}/{run_id}/{owner}/{binding}"
            source_ids = _tuple_strings(operation.get("source_ids"))
            stage = _cost_stage("learning", operation.get("cost"), source_ids=source_ids,
                                dedup_keys=("operation-cost/" + key,))
            old = found.get(key)
            if old is not None and old != stage:
                raise ValueError("learning operation has conflicting persisted costs")
            found[key] = stage
    return [found[key] for key in sorted(found)]


def _profile_id(contents: dict[str, Any]) -> tuple[str, str]:
    digest = content_hash(contents)
    return "research-profile-" + digest, digest


def profile_from_facts(facts: dict[str, Any], *, lineage_group_id: str | None = None,
                      case_key: str | None = None,
                      expected_stages: tuple[str, ...] = DEFAULT_STAGES) -> ResearchProfile:
    """Reconstruct a profile from a saved completion/report or transition facts.

    This function is pure.  It only consumes dictionaries already read by the
    caller and turns omitted deployment/application evidence into ``unknown``.
    """
    if not isinstance(facts, dict):
        raise TypeError("research profile facts must be an object")
    task_id, run_id = facts.get("task_id"), facts.get("run_id")
    if not isinstance(task_id, str) or not isinstance(run_id, str):
        raise TypeError("research profile requires the saved task and run IDs")
    if lineage_group_id is not None:
        _identifier(lineage_group_id, "lineage group ID")
    stages: dict[str, ResearchStageCost] = {}
    cost = facts.get("cost")
    timing_value, session_value = facts.get("timing"), facts.get("session")
    timing = timing_value if isinstance(timing_value, dict) else {}
    session = session_value if isinstance(session_value, dict) else {}
    solver_deadline = _float(session.get("max_seconds"))
    if solver_deadline is not None:
        solver_deadline *= 1000
    if isinstance(cost, dict):
        stages["solver"] = _cost_stage("solver", cost, elapsed_ms=_float(timing.get("repair_ms")),
                                        deadline_ms=solver_deadline)
    version_facts = facts.get("version_skill_facts")
    learning = _version_learning_stages(version_facts)
    explicit_learning = facts.get("learning_cost")
    if isinstance(explicit_learning, dict):
        learning = [_cost_stage("learning", explicit_learning,
                                source_ids=_tuple_strings(explicit_learning.get("source_ids")),
                                dedup_keys=_tuple_strings(explicit_learning.get("dedup_keys")))]
    if learning:
        # Keep one stage while preserving dedup identities in the profile.
        stages["learning"] = _combine_stage("learning", learning)
    elif "learning_cost" in facts or version_facts is not None:
        stages["learning"] = _cost_stage("learning", explicit_learning)
    validation = facts.get("validation_cost")
    if isinstance(validation, dict):
        stages["validation"] = _cost_stage("validation", validation,
                                             source_ids=_tuple_strings(validation.get("source_ids")),
                                             dedup_keys=_tuple_strings(validation.get("dedup_keys")))
    elif "validation_cost" in facts:
        stages["validation"] = _cost_stage("validation", None)
    for stage in expected_stages:
        _identifier(stage, "research stage")
        if stage not in stages:
            stages[stage] = _cost_stage(stage, None)
    ordered = tuple(stages[key] for key in sorted(stages))
    sources = tuple(sorted({item for stage in ordered for item in stage.source_ids}))
    dedup = tuple(sorted({item for stage in ordered for item in stage.dedup_keys}))
    total = _combine_total(ordered)
    contents = {"schema_version": 1, "task_id": task_id, "run_id": run_id,
                "lineage_group_id": lineage_group_id, "case_key": case_key,
                "stages": [item.model_dump(mode="json") for item in ordered],
                "total": total.model_dump(mode="json"), "source_ids": list(sources),
                "dedup_keys": list(dedup)}
    profile_id, digest = _profile_id(contents)
    return ResearchProfile(profile_id=profile_id, content_sha256=digest, task_id=task_id, run_id=run_id,
                           lineage_group_id=lineage_group_id, case_key=case_key, stages=ordered,
                           total=total, source_ids=sources, dedup_keys=dedup)


def _combine_stage(stage: str, values: list[ResearchStageCost]) -> ResearchStageCost:
    if not values:
        return _cost_stage(stage, None)
    calls = [value.reserved_calls for value in values]
    totals = [value.total_tokens for value in values]
    elapsed = [value.elapsed_ms for value in values]
    known_calls = [value for value in calls if value is not None]
    known_totals = [value for value in totals if value is not None]
    known_elapsed = [value for value in elapsed if value is not None]
    reasons = sorted({reason for value in values for reason in value.unknown_reasons})
    return ResearchStageCost(stage=stage, status="unknown" if reasons else "observed",
                             reserved_calls=None if len(known_calls) != len(calls) else sum(known_calls),
                             known_tokens_lower_bound=sum(value.known_tokens_lower_bound for value in values),
                             total_tokens=None if reasons or len(known_totals) != len(totals) else sum(known_totals),
                             unknown_calls=sum(value.unknown_calls for value in values),
                             elapsed_ms=None if len(known_elapsed) != len(elapsed) else sum(known_elapsed),
                             deadline_ms=None,
                             source_ids=tuple(sorted({item for value in values for item in value.source_ids})),
                             dedup_keys=tuple(sorted({item for value in values for item in value.dedup_keys})),
                             unknown_reasons=tuple(reasons))


def _combine_total(stages: tuple[ResearchStageCost, ...]) -> ResearchCostSummary:
    reasons = sorted({reason for stage in stages for reason in stage.unknown_reasons})
    calls = [stage.reserved_calls for stage in stages]
    totals = [stage.total_tokens for stage in stages]
    elapsed = [stage.elapsed_ms for stage in stages]
    known_calls = [value for value in calls if value is not None]
    known_totals = [value for value in totals if value is not None]
    known_elapsed = [value for value in elapsed if value is not None]
    deadlines = [stage.deadline_ms for stage in stages if stage.deadline_ms is not None]
    return ResearchCostSummary(reserved_calls=None if len(known_calls) != len(calls) else sum(known_calls),
                               known_tokens_lower_bound=sum(stage.known_tokens_lower_bound for stage in stages),
                               total_tokens=None if reasons or len(known_totals) != len(totals) else sum(known_totals),
                               unknown_calls=sum(stage.unknown_calls for stage in stages),
                               elapsed_ms=None if len(known_elapsed) != len(elapsed) else sum(known_elapsed),
                               deadline_ms=max(deadlines) if deadlines else None,
                               unknown_reasons=tuple(reasons))


reconstruct_research_profile = profile_from_facts
