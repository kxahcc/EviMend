"""Bind advisory repair decisions to registered analysis and actual failed checks."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from .conditions import ConditionRecord, ConditionScope, ConditionStore
from .contracts import (
    Contract,
    Record,
    require_hash,
    require_identifiers,
    require_unique,
)
from .diagnostics import AnalysisStore
from .model_calls import canonical, sha256
from .policies import (
    Counterexample,
    FeedbackDecision,
    ObligationClaim,
    PolicyDecision,
    PolicyFeatures,
    PolicyInput,
    candidate_conflicts,
    choose_action,
    reconsider_after_feedback,
)
from .verification import CheckLedger, _atomic_json
from .workspace import CandidateWorkspace


class DecisionRequest(Contract):
    task_id: str
    run_id: str
    diagnosis_id: str
    plan: PolicyInput
    candidate_id: str | None = None
    feedback_id: str | None = None
    condition_scope: ConditionScope | None = None
    condition_result_ids: list[str] = Field(default_factory=list, max_length=16)

    @model_validator(mode="after")
    def valid_request(self) -> DecisionRequest:
        require_identifiers([self.task_id, self.run_id, self.diagnosis_id])
        require_identifiers(self.condition_result_ids)
        require_unique(self.condition_result_ids)
        if bool(self.condition_result_ids) != (self.condition_scope is not None):
            raise ValueError("condition references need an explicit expected scope")
        if (self.candidate_id is None) != (self.feedback_id is None):
            raise ValueError("feedback needs both candidate and saved feedback identity")
        for value in (self.candidate_id, self.feedback_id):
            if value is not None:
                require_hash(value)
        return self


class DecisionRecord(Record):
    request: DecisionRequest
    context_sha256: str
    features: PolicyFeatures
    decision: PolicyDecision
    feedback: FeedbackDecision | None = None
    condition_review: FeedbackDecision | None = None
    next_action: Literal["direct_candidate", "read_existing", "run_probe", "attempt_with_unknown", "unresolved",
                         "reconsider_repair", "retain_uncertainty", "ignore_stale"]
    advisory_obligation_ids: list[str] = Field(default_factory=list)
    hard_rejected_obligation_ids: list[str] = Field(default_factory=list)
    interpretation_authority: Literal["model_inference"] = "model_inference"

    @model_validator(mode="after")
    def valid_context(self) -> DecisionRecord:
        require_hash(self.context_sha256)
        if (self.request.task_id != self.task_id or self.request.run_id != self.run_id or
                self.request.plan.source_snapshot_sha256 != self.source_snapshot_sha256 or
                self.decision.source_snapshot_sha256 != self.source_snapshot_sha256):
            raise ValueError("decision context belongs to another owner or snapshot")
        require_identifiers(self.advisory_obligation_ids + self.hard_rejected_obligation_ids)
        return self


def feedback_key(feedback: Counterexample) -> str:
    return sha256(feedback.execution_id + ":" + ",".join(sorted(feedback.violated_obligation_ids)))


def _request_value(request: DecisionRequest) -> dict:
    value = request.model_dump(mode="json")
    value["plan"]["available_capabilities"] = sorted(request.plan.available_capabilities)
    value["condition_result_ids"] = sorted(request.condition_result_ids)
    return value


class DecisionStore:
    def __init__(self, workspace: CandidateWorkspace):
        self.workspace = workspace
        self.analysis = AnalysisStore(workspace)

    def _folder(self, name: str) -> Path:
        path = self.workspace.root / name
        if path.exists() and (not path.is_dir() or path.is_symlink()):
            raise ValueError("decision or feedback folder is linked")
        return path

    def records(self) -> dict[str, DecisionRecord]:
        found: dict[str, DecisionRecord] = {}
        for path in sorted(self._folder("decisions").glob("*.json")):
            if path.is_symlink() or not path.is_file():
                raise ValueError("decision record is linked")
            record = DecisionRecord.model_validate_json(path.read_text(encoding="utf-8"))
            if (path.stem != record.record_id or record.task_id != self.workspace.task_id or
                    record.run_id != self.workspace.run_id or
                    record.source_snapshot_sha256 != self.workspace.source_snapshot_sha256):
                raise ValueError("decision record has another identity or version")
            expected = "decision-" + sha256(canonical({"request": _request_value(record.request),
                "context_sha256": record.context_sha256, "features": record.features.model_dump()}))
            if record.record_id != expected:
                raise ValueError("decision request identity changed")
            review = self._condition_review(record.request, self._conditions(record.request), record.features)
            next_action = ("reconsider_repair" if ((record.feedback and record.feedback.action == "reconsider_repair") or
                           (review and review.action == "reconsider_repair")) else record.decision.action)
            if (record.condition_review != review or record.next_action != next_action or
                    record.decision != choose_action(record.request.plan, record.features)):
                raise ValueError("saved decision or condition review changed")
            found[record.record_id] = record
        return found

    def feedback_ids(self) -> list[str]:
        paths = sorted(self._folder("feedback").glob("*.json"))
        if any(path.is_symlink() or not path.is_file() for path in paths):
            raise ValueError("feedback record is linked")
        return [path.stem for path in paths]

    def _feedback(self, request: DecisionRequest, bundle: dict, ledger: CheckLedger) -> Counterexample | None:
        if request.feedback_id is None or request.candidate_id is None:
            return None
        path = self._folder("feedback") / f"{request.feedback_id}.json"
        if not path.is_file() or path.is_symlink():
            raise ValueError("feedback is not registered")
        feedback = Counterexample.model_validate_json(path.read_text(encoding="utf-8"))
        if feedback_key(feedback) != request.feedback_id or feedback.candidate_id != request.candidate_id:
            raise ValueError("feedback belongs to another candidate or identity")
        source = self.workspace.require_registered(request.candidate_id)
        candidate = json.loads((source.parent / "candidate.json").read_text(encoding="utf-8"))
        diagnosis = bundle["diagnosis"]
        if (candidate["diagnosis_id"] != request.diagnosis_id and
                diagnosis.feedback_candidate_id != request.candidate_id):
            raise ValueError("feedback candidate belongs to another diagnosis")
        result = ledger.completed(request.candidate_id)
        if (result is None or result.status != "fail" or result.idempotency_key != feedback.execution_id or
                feedback.candidate_snapshot_sha256 != candidate["candidate_snapshot_sha256"] or
                feedback.observation != result.detail or feedback.scope != "synthetic" or
                feedback.evidence_kind != "local_execution" or feedback.link_authority != "model_inference"):
            raise ValueError("feedback has no matching saved failed synthetic check")
        if not set(feedback.violated_obligation_ids) <= set(candidate["restores"] + candidate["preserves"]):
            raise ValueError("feedback obligation is not linked to the candidate")
        return feedback

    def _bind(self, request: DecisionRequest, bundle: dict) -> None:
        hypotheses = {item.hypothesis_id: item for item in bundle["diagnosis"].hypotheses}
        evidence = {item.record_id: item for item in bundle["evidence"]}
        obligations = self.analysis.obligations()
        for direction in request.plan.directions:
            if not direction.hypothesis_ids or not set(direction.hypothesis_ids) <= set(hypotheses):
                raise ValueError("direction refers to an absent diagnosis hypothesis")
            if not direction.support_evidence_ids or not set(direction.support_evidence_ids) <= set(evidence):
                raise ValueError("direction has no registered diagnosis evidence")
            if not set(direction.obligation_ids) <= set(obligations):
                raise ValueError("direction refers to an absent obligation")
            required = {value for identifier in direction.hypothesis_ids for value in hypotheses[identifier].obligation_ids}
            if not required <= set(direction.obligation_ids):
                raise ValueError("direction omits its hypothesis obligations")
            for identifier in direction.obligation_ids:
                obligation = obligations[identifier]
                if obligation.hypothesis_ids:
                    if not set(obligation.hypothesis_ids).intersection(direction.hypothesis_ids):
                        raise ValueError("direction obligation belongs to another hypothesis")
                elif not set(obligation.source_evidence_ids) <= set(evidence):
                    raise ValueError("direction obligation has another diagnosis evidence basis")
        check = request.plan.check
        if check is None:
            return
        if ((check.kind == "existing_evidence" and check.capability not in {"read_source", "read_log"}) or
                (check.kind == "execution_probe" and check.capability != "diagnostic_execution")):
            raise ValueError("check kind and capability disagree")
        directions = {item.direction_id: item for item in request.plan.directions}
        questions = [item.distinguishing_check for item in hypotheses.values() if item.distinguishing_check]
        if not any(set(question.observation_routes) == set(check.observation_routes) and all(
                hypothesis_id in directions[check.observation_routes[outcome]].hypothesis_ids
                for outcome, hypothesis_id in question.observation_routes.items()) for question in questions):
            raise ValueError("check routes do not match a registered diagnosis question")

    def _conditions(self, request: DecisionRequest) -> list[ConditionRecord]:
        if request.condition_scope is None:
            return []
        scope = request.condition_scope
        if (scope.task_id != request.task_id or scope.run_id != request.run_id or
                scope.source_snapshot_sha256 != request.plan.source_snapshot_sha256 or
                (request.candidate_id is not None and scope.candidate_id != request.candidate_id)):
            raise ValueError("condition scope belongs to another decision owner or feedback candidate")
        records = ConditionStore(self.workspace).records()
        hypotheses = {identifier for direction in request.plan.directions for identifier in direction.hypothesis_ids}
        selected: list[ConditionRecord] = []
        for identifier in sorted(request.condition_result_ids):
            record = records.get(identifier)
            if record is None:
                raise ValueError("condition result is not registered")
            checked = record.result.request
            if (checked.scope != scope or checked.diagnosis_id != request.diagnosis_id or
                    checked.hypothesis_id not in hypotheses):
                raise ValueError("condition result has another scope, diagnosis or direction hypothesis")
            selected.append(record)
        return selected

    @staticmethod
    def _condition_review(request: DecisionRequest, records: list[ConditionRecord],
                          features: PolicyFeatures) -> FeedbackDecision | None:
        if not records:
            return None
        if not features.obligation_tracking:
            return FeedbackDecision(action="retain_uncertainty", reason="condition association is disabled")
        violated = {record.result.request.hypothesis_id for record in records if record.result.status == "violated"}
        affected = [direction.direction_id for direction in request.plan.directions
                    if violated.intersection(direction.hypothesis_ids)]
        if not affected:
            return FeedbackDecision(action="retain_uncertainty",
                reason="scoped synthetic calculations do not confirm a root cause or successful repair")
        return FeedbackDecision(action="reconsider_repair", affected_direction_ids=affected,
            reason="a scoped synthetic condition is violated; inspect related directions, applicability remains inferred")

    def decide(self, request: DecisionRequest, *, ledger: CheckLedger, features: PolicyFeatures,
               capabilities: frozenset[str]) -> DecisionRecord:
        if (request.task_id != self.workspace.task_id or request.run_id != self.workspace.run_id or
                request.plan.source_snapshot_sha256 != self.workspace.source_snapshot_sha256):
            raise ValueError("decision request has another task, run or snapshot")
        bundles = self.analysis.bundles()
        if request.diagnosis_id not in bundles:
            raise ValueError("decision diagnosis is not registered")
        bundle = bundles[request.diagnosis_id]
        self._bind(request, bundle)
        feedback = self._feedback(request, bundle, ledger)
        conditions = self._conditions(request)
        plan = request.plan.model_copy(update={"available_capabilities": set(capabilities) & {"read_source", "read_log"},
                                              "remaining_probe_budget": 0})
        request = request.model_copy(update={"plan": plan})
        decision = choose_action(plan, features)
        condition_review = self._condition_review(request, conditions, features)
        reconsideration = None
        hard: list[str] = []
        advisory: list[str] = []
        if feedback is not None:
            reconsideration = reconsider_after_feedback(plan, feedback, candidate_id=feedback.candidate_id,
                candidate_snapshot_sha256=feedback.candidate_snapshot_sha256, features=features)
            claims = [ObligationClaim(obligation_id=item.record_id,
                source_snapshot_sha256=item.source_snapshot_sha256, authority=item.authority,
                evidence_ids=item.source_evidence_ids, statement=item.statement)
                for item in self.analysis.obligations().values()]
            if features.obligation_tracking:
                hard, advisory = candidate_conflicts(claims, source_snapshot_sha256=self.workspace.source_snapshot_sha256,
                                                     feedback=feedback)
        # Admission timestamps are audit metadata, not repair-decision inputs.
        context = {"bundle": {"diagnosis": bundle["diagnosis"].model_dump(mode="json", exclude={"created_at"}),
                    "evidence": [item.model_dump(mode="json", exclude={"created_at"}) for item in bundle["evidence"]],
                    "obligations": [item.model_dump(mode="json", exclude={"created_at"}) for item in bundle["obligations"]]},
                   "feedback": feedback.model_dump() if feedback else None,
                   "conditions": [record.model_dump(mode="json") for record in conditions],
                   "referenced_obligations": {identifier: item.model_dump(mode="json", exclude={"created_at"}) for identifier, item in
                       self.analysis.obligations().items() if any(identifier in direction.obligation_ids
                                                                for direction in plan.directions)}}
        context_hash = sha256(canonical(context))
        identifier = "decision-" + sha256(canonical({"request": _request_value(request),
                                                    "context_sha256": context_hash, "features": features.model_dump()}))
        next_action = ("reconsider_repair" if ((reconsideration and reconsideration.action == "reconsider_repair") or
                       (condition_review and condition_review.action == "reconsider_repair"))
                       else decision.action)
        record = DecisionRecord(task_id=self.workspace.task_id, run_id=self.workspace.run_id, record_id=identifier,
            created_at=datetime.now(UTC), source_snapshot_sha256=self.workspace.source_snapshot_sha256,
            request=request, context_sha256=context_hash, features=features, decision=decision, feedback=reconsideration,
            condition_review=condition_review,
            next_action=next_action, advisory_obligation_ids=advisory, hard_rejected_obligation_ids=hard)
        existing = self.records().get(identifier)
        if existing is not None:
            if existing.model_dump(exclude={"created_at"}) != record.model_dump(exclude={"created_at"}):
                raise ValueError("decision result changed")
            return existing
        folder = self._folder("decisions")
        folder.mkdir(exist_ok=True)
        _atomic_json(folder / f"{identifier}.json", record.model_dump(mode="json"))
        return record

    def candidate_link(self, decision_id: str, direction_id: str, diagnosis_id: str,
                       obligations: list[str]) -> None:
        record = self.records().get(decision_id)
        if record is None or record.request.diagnosis_id != diagnosis_id:
            raise ValueError("candidate decision is not registered for its diagnosis")
        directions = {item.direction_id: item for item in record.request.plan.directions}
        if direction_id not in directions or not set(obligations) <= set(directions[direction_id].obligation_ids):
            raise ValueError("candidate direction or obligation does not match its decision")
