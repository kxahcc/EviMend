"""Version-bound, optional repair guidance; never a substitute for execution evidence."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_serializer, model_validator

from .contracts import IDENTIFIER, require_hash, require_unique


class PolicyFeatures(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    rule_guidance: bool = False
    obligation_tracking: bool = False
    decision_checks: bool = False


class RuleCard(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    family: str
    inspect: tuple[str, ...]
    runtime_condition: str
    behavior_question: str
    unsafe_shortcut: str


RULES: dict[str, RuleCard] = {
    "lifecycle": RuleCard(
        family="lifecycle",
        inspect=("first assignment", "use site", "branch and callback order", "async dependency"),
        runtime_condition="A usable value exists on every target path before consumption.",
        behavior_question="Should an absent result wait, initialize, or follow an allowed empty path?",
        unsafe_shortcut="Optional chaining may hide the required operation.",
    ),
    "bounds": RuleCard(
        family="bounds",
        inspect=("index and length", "byte offset and length", "element width", "downstream format"),
        runtime_condition="Access is within the relevant element or byte extent; typed views obey alignment.",
        behavior_question="Are units, payload tail, and result format preserved?",
        unsafe_shortcut="Clamping or truncation may silently discard valid data.",
    ),
    "recursion": RuleCard(
        family="recursion",
        inspect=("recursive argument", "edge relation", "termination", "output order and duplicates"),
        runtime_condition="Traversal terminates on finite input without losing required work.",
        behavior_question="Are shared nodes repeated, and in which order?",
        unsafe_shortcut="A global visited set can change path-level traversal semantics.",
    ),
    "dynamic_pattern": RuleCard(
        family="dynamic_pattern",
        inspect=("raw input", "expression construction", "flags and escaping", "caller expectation"),
        runtime_condition="The constructed pattern or parser input is valid.",
        behavior_question="Is user input literal text or an intentional pattern?",
        unsafe_shortcut="Catching an error and returning no matches can break search behavior.",
    ),
}


def rule_guidance(family: str, features: PolicyFeatures) -> RuleCard | None:
    """An explicit family hint selects guidance; unknown families remain generic."""
    return RULES.get(family) if features.rule_guidance else None


class RepairDirection(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    direction_id: str
    repair_key: str
    acceptance_key: str
    support_evidence_ids: list[str] = Field(default_factory=list)
    obligation_ids: list[str] = Field(default_factory=list)
    hypothesis_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def valid_ids(self) -> RepairDirection:
        for value in (self.direction_id, self.repair_key, self.acceptance_key,
                      *self.support_evidence_ids, *self.obligation_ids, *self.hypothesis_ids):
            if not IDENTIFIER.fullmatch(value):
                raise ValueError("invalid policy identifier")
        for values in (self.support_evidence_ids, self.obligation_ids, self.hypothesis_ids):
            require_unique(values)
        return self


class DistinguishingCheck(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    check_id: str
    kind: Literal["existing_evidence", "execution_probe"]
    capability: str
    observation_routes: dict[str, str]
    unknown_route: Literal["retain_unknown"] = "retain_unknown"

    @model_validator(mode="after")
    def valid_routes(self) -> DistinguishingCheck:
        if not IDENTIFIER.fullmatch(self.check_id) or not IDENTIFIER.fullmatch(self.capability):
            raise ValueError("invalid check identifier")
        if len(self.observation_routes) < 2:
            raise ValueError("a distinguishing check needs at least two outcomes")
        if any(not IDENTIFIER.fullmatch(key) or not IDENTIFIER.fullmatch(value)
               for key, value in self.observation_routes.items()):
            raise ValueError("invalid observation route")
        return self


class PolicyInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    source_snapshot_sha256: str
    directions: list[RepairDirection] = Field(min_length=1)
    selected_direction_id: str | None = None
    check: DistinguishingCheck | None = None
    available_capabilities: set[str] = Field(default_factory=set)
    remaining_probe_budget: int = Field(ge=0)

    @field_serializer("available_capabilities", when_used="json")
    def sorted_capabilities(self, value: set[str]) -> list[str]:
        # Frozen decisions must reconstruct identically in a new process;
        # Python's hash seed must not determine a JSON array's order.
        return sorted(value)

    @model_validator(mode="after")
    def valid_plan(self) -> PolicyInput:
        require_hash(self.source_snapshot_sha256)
        ids = [direction.direction_id for direction in self.directions]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate repair direction")
        if self.selected_direction_id is not None and self.selected_direction_id not in ids:
            raise ValueError("selected direction is absent")
        if self.check is not None and any(value not in ids for value in self.check.observation_routes.values()):
            raise ValueError("check routes to an absent direction")
        return self


class PolicyDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    action: Literal["direct_candidate", "read_existing", "run_probe", "attempt_with_unknown", "unresolved"]
    reason: str
    selected_direction_id: str | None = None
    check_id: str | None = None
    source_snapshot_sha256: str


def choose_action(plan: PolicyInput, features: PolicyFeatures) -> PolicyDecision:
    """Recommend a next step from visible proposals; do not execute or select a root cause."""
    directions = plan.directions
    common = {"source_snapshot_sha256": plan.source_snapshot_sha256}
    equivalent = len({(item.repair_key, item.acceptance_key) for item in directions}) == 1
    if len(directions) == 1 or equivalent:
        return PolicyDecision(action="direct_candidate", reason="one repair and acceptance direction",
                              selected_direction_id=plan.selected_direction_id or directions[0].direction_id, **common)
    check = plan.check
    informative = (check is not None and len(set(check.observation_routes.values())) > 1 and
                   len({(item.repair_key, item.acceptance_key) for item in directions
                        if item.direction_id in check.observation_routes.values()}) > 1)
    if (features.decision_checks and informative and check is not None and
            check.capability in plan.available_capabilities):
        if check.kind == "existing_evidence":
            return PolicyDecision(action="read_existing", reason="existing evidence can change the repair",
                                  check_id=check.check_id, **common)
        if plan.remaining_probe_budget > 0:
            return PolicyDecision(action="run_probe", reason="probe can change the repair",
                                  check_id=check.check_id, **common)
    if plan.selected_direction_id is not None:
        return PolicyDecision(action="attempt_with_unknown", reason="repair ambiguity remains",
                              selected_direction_id=plan.selected_direction_id, **common)
    return PolicyDecision(action="unresolved", reason="no supported repair direction was selected", **common)


class ObligationClaim(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    obligation_id: str
    source_snapshot_sha256: str
    authority: Literal["verified_requirement", "interface_rule", "existing_behavior", "model_inference"]
    evidence_ids: list[str] = Field(default_factory=list)
    statement: str = Field(min_length=1)

    @model_validator(mode="after")
    def valid_claim(self) -> ObligationClaim:
        if not IDENTIFIER.fullmatch(self.obligation_id):
            raise ValueError("invalid obligation identifier")
        require_hash(self.source_snapshot_sha256)
        return self


def candidate_conflicts(claims: list[ObligationClaim], *, source_snapshot_sha256: str,
                        feedback: Counterexample) -> tuple[list[str], list[str]]:
    """Only trusted, current-version feedback on authoritative claims is hard."""
    hard: list[str] = []
    advisory: list[str] = []
    for claim in claims:
        if (claim.obligation_id not in feedback.violated_obligation_ids or
                claim.source_snapshot_sha256 != source_snapshot_sha256):
            continue
        if (claim.authority in ("verified_requirement", "interface_rule") and claim.evidence_ids and
                feedback.link_authority == "verified_check"):
            hard.append(claim.obligation_id)
        else:
            advisory.append(claim.obligation_id)
    return hard, advisory


class Counterexample(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    candidate_id: str
    candidate_snapshot_sha256: str
    execution_id: str
    evidence_kind: Literal["local_execution", "application_execution"]
    scope: Literal["synthetic", "application"]
    link_authority: Literal["model_inference", "verified_check"]
    violated_obligation_ids: list[str] = Field(min_length=1)
    observation: str = Field(min_length=1)

    @model_validator(mode="after")
    def valid_reference(self) -> Counterexample:
        for value in (self.candidate_id, self.candidate_snapshot_sha256):
            require_hash(value)
        if not IDENTIFIER.fullmatch(self.execution_id):
            raise ValueError("invalid execution identifier")
        if any(not IDENTIFIER.fullmatch(value) for value in self.violated_obligation_ids):
            raise ValueError("invalid feedback obligation identifier")
        require_unique(self.violated_obligation_ids)
        return self


def relevant_counterexample(counterexample: Counterexample, *, candidate_id: str,
                            candidate_snapshot_sha256: str) -> bool:
    return (counterexample.candidate_id == candidate_id and
            counterexample.candidate_snapshot_sha256 == candidate_snapshot_sha256)


class FeedbackDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    action: Literal["reconsider_repair", "retain_uncertainty", "ignore_stale"]
    affected_direction_ids: list[str] = Field(default_factory=list)
    reason: str


def reconsider_after_feedback(plan: PolicyInput, counterexample: Counterexample,
                              *, candidate_id: str, candidate_snapshot_sha256: str,
                              features: PolicyFeatures) -> FeedbackDecision:
    """Link observed feedback to an obligation without banning a repair family."""
    if not relevant_counterexample(counterexample, candidate_id=candidate_id,
                                   candidate_snapshot_sha256=candidate_snapshot_sha256):
        return FeedbackDecision(action="ignore_stale", reason="feedback belongs to another candidate version")
    if not features.obligation_tracking:
        return FeedbackDecision(action="retain_uncertainty", reason="obligation linking is disabled")
    violated = set(counterexample.violated_obligation_ids)
    affected = [item.direction_id for item in plan.directions if violated.intersection(item.obligation_ids)]
    if not affected:
        return FeedbackDecision(action="retain_uncertainty", reason="no repair direction cites the violated obligation")
    return FeedbackDecision(action="reconsider_repair", affected_direction_ids=affected,
                            reason="observed feedback challenges a candidate obligation; inspect alternatives")
