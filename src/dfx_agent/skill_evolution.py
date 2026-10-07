"""Accounted synthetic decisions, append-only sandbox aliases and frozen selections.

Actual run facts are read through SkillValidationStore. This layer does not
execute an Agent, refine a model, grant application eligibility or publish SDK
knowledge. Serial managed writers are required; this is not a filesystem lock.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel

from .contracts import require_identifiers
from .feedback import _ClosedTree, _closure_directory_anchor, _exists, _ReadBudget
from .feedback_contracts import FeedbackLimits, _identity, _object, content_hash, encode
from .metrics import _guard, _provenance_summary
from .skill_evolution_contracts import (
    HostModelSkillVersionApproval,
    HostSkillVersionApproval,
    HostValidationPlanApproval,
    HostValidationPlanApprovalV2,
    ModelVersionPayload,
    ValidationApproval,
    ValidationPlanBody,
    ValidationPlanRecord,
    ValidationReceiptBody,
    ValidationReceiptRecord,
    VersionApproval,
    parse_plan_payload,
    parse_receipt_payload,
    parse_version_payload,
)
from .skill_transition_contracts import (
    MAX_EVOLUTION_EVENTS,
    FrozenSkillSelection,
    FrozenSkillSelectionV2,
    HostPublicationApproval,
    HostPublicationApprovalV2,
    HostTransitionApproval,
    HostTransitionApprovalV2,
    PublicationApproval,
    PublicationBody,
    PublicationPayload,
    PublicationPayloadV2,
    PublicationRecord,
    SkillPublication,
    SkillPublicationV2,
    SkillSelection,
    SkillTransition,
    SkillTransitionV2,
    SyntheticValidationPolicy,
    SyntheticValidationPolicyV2,
    TransitionApproval,
    TransitionBody,
    TransitionPayload,
    TransitionPayloadV2,
    TransitionRecord,
    parse_publication,
    parse_publication_payload,
    parse_synthetic_policy,
    parse_transition,
    parse_transition_payload,
)
from .skill_validation import SkillValidationStore


def _typed(kind: type[BaseModel], value: BaseModel) -> Any:
    compatible = {HostSkillVersionApproval: (HostSkillVersionApproval, HostModelSkillVersionApproval),
        HostValidationPlanApproval: (HostValidationPlanApproval, HostValidationPlanApprovalV2),
        HostTransitionApproval: (HostTransitionApproval, HostTransitionApprovalV2),
        HostPublicationApproval: (HostPublicationApproval, HostPublicationApprovalV2),
        FrozenSkillSelection: (FrozenSkillSelection, FrozenSkillSelectionV2)}
    if type(value) not in compatible.get(kind, (kind,)):
        raise ValueError("evolution requires the exact external approval type")
    raw = encode(value)
    checked = type(value).model_validate_json(raw)
    if encode(checked) != raw:
        raise ValueError("evolution input is not an exact canonical approved object")
    return checked


def _policy(raw: str | None) -> SyntheticValidationPolicy | SyntheticValidationPolicyV2 | None:
    if raw is None:
        return None
    value = parse_synthetic_policy(raw)
    if encode(value) != raw:
        raise ValueError("synthetic validation policy must contain its complete canonical fields")
    return value


class _EvolutionBudget(_ReadBudget):
    """One operation's approved sources, always closed before success."""

    def __init__(self, maximum: int):
        super().__init__(maximum)
        self.plans: dict[str, tuple[ValidationApproval, ValidationPlanRecord]] = {}
        self.receipts: dict[str, tuple[ValidationApproval, ValidationReceiptRecord, _ClosedTree, _ClosedTree]] = {}

    def clear(self) -> None:
        self.plans.clear()
        self.receipts.clear()


class _PlanReadView(SkillValidationStore):
    """Private receipt reader; neither persisted nor exposed as a public store."""

    def __init__(self, owner: SkillTransitionStore, budget: _EvolutionBudget):
        super().__init__(owner.validation.root, limits=owner.limits)
        self.owner, self.budget = owner, budget

    def _read_plan(self, plan_id: str, approval: ValidationApproval, budget: _ReadBudget) -> ValidationPlanRecord:
        if budget is not self.budget:
            raise ValueError("plan read view cannot cross operation budgets")
        return self.owner._plan_once(plan_id, approval, self.budget)

    def _read_receipt(self, receipt_id: str, approval: ValidationApproval,
                      budget: _ReadBudget) -> ValidationReceiptRecord:
        if budget is not self.budget:
            raise ValueError("receipt read view cannot cross operation budgets")
        _identity(receipt_id, "skill-receipt-")
        approval = _typed(HostValidationPlanApproval, approval)
        cached = self.budget.receipts.get(receipt_id)
        if cached is not None:
            if cached[0] != approval:
                raise ValueError("same operation receipt changed its original approval")
            return cached[1]
        return super()._read_receipt(receipt_id, approval, budget)

    def _receipt_closed(self, approval: ValidationApproval, receipt: ValidationReceiptRecord,
                        tree: _ClosedTree, metadata: _ClosedTree, budget: _ReadBudget) -> None:
        if budget is not self.budget or tree.budget is not budget or metadata.budget is not budget:
            raise ValueError("receipt raw proof cannot cross operation budgets")
        if receipt.receipt_id in self.budget.receipts:
            raise ValueError("receipt view cannot replace its original raw proof")
        self.budget.receipts[receipt.receipt_id] = approval, receipt, tree, metadata


class SkillTransitionStore:
    """Independent records under an explicitly precreated validation store root."""

    def __init__(self, validation: SkillValidationStore):
        if not isinstance(validation, SkillValidationStore):
            raise TypeError("evolution requires a readable SkillValidationStore")
        self.validation = validation
        self.root = validation.root
        self.limits: FeedbackLimits = validation.limits
        _guard(self.root)

    def _plan_once(self, plan_id: str, approval: ValidationApproval,
                   budget: _EvolutionBudget) -> ValidationPlanRecord:
        cached = budget.plans.get(plan_id)
        if cached is not None:
            if cached[0] != approval:
                raise ValueError("same operation plan changed its original approval")
            return cached[1]
        value = self.validation._read_plan(plan_id, approval, budget)
        budget.plans[plan_id] = approval, value
        return value

    def _close_plans(self, budget: _EvolutionBudget) -> None:
        # Bypass the private view: reauthenticate every parent/source closure.
        for plan_id, (approval, original) in budget.plans.items():
            if self.validation._read_plan(plan_id, approval, budget) != original:
                raise ValueError("approved plan sources changed before operation closure")
        # Reaggregation is unnecessary once its complete input closure has been
        # authenticated. Reclose the original raw proofs, not fresh snapshots:
        # restored bytes still fail if any file or directory identity changed.
        for _approval, _receipt, tree, metadata in budget.receipts.values():
            tree.finish()
            metadata.finish()

    def _namespace(self, name: str, *, create: bool = False) -> Path:
        result = self.root / name
        _guard(self.root)
        if create and not _exists(result):
            result.mkdir()
        _guard(result)
        if not result.is_dir():
            raise ValueError("evolution namespace must be a directory")
        return result

    def _chain(self, name: str, key: str, *, create: bool = False) -> list[Path]:
        if not create and not _exists(self.root / name):
            return []
        namespace = self._namespace(name, create=create)
        folder = namespace / key
        if not _exists(folder):
            if not create:
                return []
            if len(list(namespace.iterdir())) >= MAX_EVOLUTION_EVENTS:
                raise ValueError("evolution registry capacity exceeded")
            folder.mkdir()
        _guard(folder)
        files = sorted(folder.iterdir())
        if len(files) > MAX_EVOLUTION_EVENTS:
            raise ValueError("evolution chain capacity exceeded")
        prefix = "skill-transition-" if name == "transitions" else "skill-publication-"
        for ordinal, path in enumerate(files, 1):
            if not re.fullmatch(rf"{ordinal:04d}-{prefix}[0-9a-f]{{64}}\.json", path.name):
                raise ValueError("evolution chain contains missing or unregistered events")
            _guard(path)
            if not path.is_file():
                raise ValueError("evolution chain event is not a regular file")
        return files

    def _read(self, path: Path, kind, budget: _ReadBudget | None = None) -> Any:
        reader = _ClosedTree(self.root, self.limits, budget)
        reader._parents(path.parent)
        raw, descriptor = reader._read(path)
        parser = kind.model_validate_json if hasattr(kind, "model_validate_json") else kind
        value = parser(encode(_object(raw.decode("utf-8"))))
        if encode(value).encode("utf-8") != raw:
            raise ValueError("evolution event canonical bytes changed")
        after, after_descriptor = reader._read(path)
        if raw != after or descriptor != after_descriptor:
            raise ValueError("evolution event changed during read")
        if any(_closure_directory_anchor(parent.lstat()) != identity for parent, identity in reader.directory_identities.items()):
            raise ValueError("evolution ancestor changed during read")
        return value

    def _publish(self, name: str, key: str, sequence: int, identity: str, value: BaseModel,
                 budget: _ReadBudget) -> None:
        raw = encode(value).encode("utf-8")
        if len(raw) > self.limits.max_file_bytes or len(raw) > self.limits.max_total_bytes:
            raise ValueError("evolution event exceeds publication capacity")
        budget.require_remaining(2 * len(raw))
        files = self._chain(name, key, create=True)
        if len(files) != sequence - 1 or sequence > MAX_EVOLUTION_EVENTS:
            raise ValueError("evolution publication lost its expected head")
        folder = self.root / name / key
        path = folder / f"{sequence:04d}-{identity}.json"
        # Staging belongs outside the chain, so interruption is never an event.
        pending = self.root / ("pending-evolution-" + uuid4().hex + ".json")
        _guard(pending, missing=True)
        with pending.open("xb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        if self._chain(name, key) != files or _exists(path):
            raise ValueError("evolution publication raced another managed writer")
        _guard(folder)
        os.rename(pending, path)
        if self._read(path, type(value), budget) != value:
            raise ValueError("published evolution event differs from its validated bytes")

    @staticmethod
    def _pair_facts(plan: ValidationPlanBody, receipts: tuple[ValidationReceiptBody, ...]) -> tuple[list[dict[str, Any]], list[str]]:
        assigned: dict[tuple[str, str], ValidationReceiptBody] = {}
        for receipt in receipts:
            key = receipt.case_id, receipt.role
            if key in assigned:
                raise ValueError("evolution cannot choose among receipts for the same planned role")
            assigned[key] = receipt
        pairs, problems = [], []
        for pair in plan.spec.pairs:
            roles = {}
            for role in ("old", "new"):
                receipt = assigned.pop((pair.case_id, role), None)
                if receipt is None:
                    roles[role] = {"outcome": "unknown", "use_status": "unknown", "cost": None,
                                   "missing_reason": "receipt_missing"}
                    problems.append("incomplete_planned_roles")
                    continue
                row = _object(receipt.aggregate_json)["rows"][0]
                roles[role] = {"outcome": row["synthetic_outcome"], "use_status": receipt.use_status,
                    "use_reasons": list(receipt.use_reasons), "cost": row["cost"],
                    "first_candidate_check_status": row["first_candidate_check_status"],
                    "latest_candidate_check_status": row["latest_candidate_check_status"],
                    "candidate_count": row["candidate_count"], "verification": row["verification"]}
                if receipt.use_status != "followed":
                    problems.append("target_use_not_verified")
                if row["synthetic_outcome"] not in ("pass", "fail"):
                    problems.append("outcome_unknown")
            comparable = all(roles[role]["use_status"] == "followed" and
                             roles[role]["outcome"] in ("pass", "fail") for role in ("old", "new"))
            pairs.append({"case_id": pair.case_id, "kind": pair.kind, "roles": roles, "comparable": comparable})
        if assigned:
            raise ValueError("evolution receipt names an unplanned role")
        return pairs, sorted(set(problems))

    def _facts(self, plan: ValidationPlanBody, history: dict[str, ValidationReceiptBody],
               plans: dict[str, ValidationPlanBody]) -> dict[str, Any]:
        attempts, missing = [], 0
        current_pairs: list[dict[str, Any]] = []
        all_problems: list[str] = []
        all_pairs: list[dict[str, Any]] = []
        for plan_id, historical in plans.items():
            pairs, problems = self._pair_facts(historical, tuple(r for r in history.values() if r.plan_id == plan_id))
            missing += sum("missing_reason" in pair["roles"][role] for pair in pairs for role in ("old", "new"))
            attempts.append({"plan_id": plan_id, "pairs": pairs, "problems": problems})
            all_problems.extend(problems)
            all_pairs.extend(pairs)
            if historical == plan:
                current_pairs = pairs
        if not current_pairs:
            raise ValueError("evolution current plan is absent from history")
        gain = sum(pair["comparable"] and pair["roles"]["old"]["outcome"] == "fail" and
                   pair["roles"]["new"]["outcome"] == "pass" for pair in all_pairs)
        loss = sum(pair["comparable"] and pair["roles"]["old"]["outcome"] == "pass" and
                   pair["roles"]["new"]["outcome"] == "fail" for pair in all_pairs)
        known_tokens = known_calls = 0
        unknown_cost = missing > 0
        for receipt in history.values():
            cost = _object(receipt.aggregate_json)["rows"][0]["cost"]
            if cost is None:
                unknown_cost = True
                continue
            known_tokens += cost["known_total_tokens"]
            known_calls += cost["reserved_calls"]
            if cost["total_tokens"] is None or cost["usage_complete"] is not True:
                unknown_cost = True
        problems = [*all_problems]
        rows = [_object(receipt.aggregate_json)["rows"][0] for receipt in history.values()]
        provenance = _provenance_summary(rows)
        schemas = {schema for row in rows for schema in row["solver_tool_schema_sha256"]}
        if len(schemas) > 1:
            raise ValueError("actual solver tool schemas differ between validation roles")
        if provenance["status"] != "observed" or any(not row["solver_tool_schema_sha256"] for row in rows):
            problems.append("execution_compatibility_unknown")
        if missing:
            problems.append("history_planned_roles_incomplete")
        if unknown_cost:
            problems.append("validation_cost_unknown")
        return {"scope": "offline_synthetic", "comparison": plan.spec.comparison,
            "planned_pairs": len(plan.spec.pairs), "planned_roles": 2 * len(plan.spec.pairs), "pairs": current_pairs,
            "new_successes": gain, "success_losses": loss, "net_gain_numerator": gain - loss,
            "net_gain_denominator": len(all_pairs), "attempt_plan_ids": list(plans),
            "attempt_count": len(plans), "attempts": attempts, "history_receipt_count": len(history),
            "missing_history_roles": missing,
            "execution_provenance": provenance, "solver_tool_schema_sha256": sorted(schemas),
            "validation_cost": {"known_tokens_lower_bound": known_tokens, "known_reserved_calls": known_calls,
                "total_tokens": None if unknown_cost else known_tokens, "monetary_cost": None},
            "learning_cost": {"status": "not_run", "tokens": None, "monetary_cost": None},
            "problems": sorted(set(problems)), "application_qualification": "none"}

    @staticmethod
    def _decision(facts: dict[str, Any], policy: SyntheticValidationPolicy | SyntheticValidationPolicyV2 | None
                  ) -> tuple[Literal["synthetic_pending", "synthetic_rejected", "synthetic_eligible"], tuple[str, ...]]:
        if policy is None or not policy.thresholds_complete():
            return "synthetic_pending", ("policy_thresholds_unset", *facts["problems"])
        if facts["problems"]:
            return "synthetic_pending", tuple(facts["problems"])
        reasons = []
        primary = min(sum(pair["kind"] == "representative" for pair in attempt["pairs"]) for attempt in facts["attempts"])
        boundary = min(sum(pair["kind"] == "boundary" for pair in attempt["pairs"]) for attempt in facts["attempts"])
        for failed, reason in (
            (primary < (policy.min_representative_pairs or 0), "representative_coverage_insufficient"),
            (boundary < (policy.min_boundary_pairs or 0), "boundary_coverage_insufficient"),
            (facts["net_gain_numerator"] < (policy.min_net_gain or 0), "net_gain_below_policy"),
            (facts["success_losses"] > (policy.max_success_losses or 0), "success_loss_exceeds_policy"),
            (facts["validation_cost"]["total_tokens"] > (policy.max_validation_tokens or 0), "validation_token_budget"),
            (facts["validation_cost"]["known_reserved_calls"] > (policy.max_validation_calls or 0), "validation_call_budget"),
            (facts["attempt_count"] > (policy.max_validation_attempts or 0), "validation_attempt_budget"),
        ):
            if failed:
                reasons.append(reason)
        if isinstance(policy, SyntheticValidationPolicyV2):
            if facts["learning_cost"]["tokens"] > (policy.max_learning_tokens or 0):
                reasons.append("learning_token_budget")
            if facts["learning_cost"]["known_reserved_calls"] > (policy.max_learning_calls or 0):
                reasons.append("learning_call_budget")
        return ("synthetic_rejected", tuple(reasons)) if reasons else ("synthetic_eligible", ())

    def _derive(self, approval: TransitionApproval, plan_approval: ValidationApproval,
                previous: TransitionBody | None, plans: dict[str, ValidationPlanBody],
                history: dict[str, ValidationReceiptBody], budget: _EvolutionBudget) -> TransitionBody:
        record = self._plan_once(approval.plan_id, plan_approval, budget)
        plan = parse_plan_payload(record.payload_json)
        if approval.schema_version != plan.schema_version or (previous is not None and previous.schema_version != plan.schema_version):
            raise ValueError("transition mixes incompatible plan, cost or approval generations")
        if previous is not None:
            if previous.version_id != plan.spec.new_version_id:
                raise ValueError("transition predecessor belongs to another candidate")
            if any(old.spec.policy_json != plan.spec.policy_json or old.spec.old_version_id != plan.spec.old_version_id
                   for old in plans.values()):
                raise ValueError("retry cannot reset its frozen policy or parent")
            configuration = _object(plan.spec.pairs[0].old_manifest_json)["configuration"]
            if any(_object(old.spec.pairs[0].old_manifest_json)["configuration"] != configuration
                   for old in plans.values()):
                raise ValueError("retry cannot change its frozen model, budgets or shared resources")
            if approval.plan_id not in plans:
                previous_tasks = [pair.old_manifest_json for old in plans.values() for pair in old.spec.pairs]
                for pair in plan.spec.pairs:
                    task = _object(pair.old_manifest_json)["tasks"][0]
                    if any(any(task[key] == _object(saved)["tasks"][0][key] for key in
                               ("task_id", "lineage_group_id", "crash_sha256")) for saved in previous_tasks):
                        raise ValueError("retry cannot count duplicate task, log or learning lineage as new evidence")
        if approval.plan_id not in plans and len(plans) >= MAX_EVOLUTION_EVENTS:
            raise ValueError("transition attempts exceed capacity")
        plans[approval.plan_id] = plan
        for receipt_id in approval.receipt_ids:
            record = _PlanReadView(self, budget)._read_receipt(receipt_id, plan_approval, budget)
            receipt = parse_receipt_payload(record.payload_json)
            if receipt.plan_id != approval.plan_id:
                raise ValueError("transition receipt belongs to another plan")
            if any(old.plan_id == receipt.plan_id and old.case_id == receipt.case_id and old.role == receipt.role and
                   key != receipt_id for key, old in history.items()):
                raise ValueError("retry cannot replace an earlier receipt for the same planned role")
            history[receipt_id] = receipt
        facts = self._facts(plan, history, plans)
        learning_costs = None
        if plan.schema_version == 2:
            learning_costs = self.validation._version_learning_costs(plan.spec.new_version_id, plan.approval.new_version_approval, budget)
            cost = learning_costs["cost"]
            if cost is None or learning_costs["status"] != "observed":
                raise ValueError("model transition has no authenticated learning operation")
            facts["learning_cost"] = {"status": "observed", "tokens": cost["total_tokens"],
                "known_tokens_lower_bound": cost["known_lower_bound"], "known_reserved_calls": cost["reserved_calls"],
                "operations": learning_costs["operations"], "monetary_cost": None}
            if cost["total_tokens"] is None:
                facts["problems"] = sorted({*facts["problems"], "learning_cost_unknown"})
        state, reasons = self._decision(facts, _policy(plan.spec.policy_json))
        payload_type: Any = TransitionPayloadV2 if plan.schema_version == 2 else TransitionPayload
        extra = {} if learning_costs is None else {"learning_costs_json": encode(learning_costs)}
        return payload_type(approval=approval, plan_approval=plan_approval, version_id=plan.spec.new_version_id,
            skill_id=plan.approval.new_version_approval.skill_id, state=state, reasons=reasons, facts_json=encode(facts),
            history_receipt_ids=tuple(sorted(history)), attempt_plan_ids=tuple(plans),
            previous_approval=None if previous is None else previous.approval, **extra)

    def _locate(self, name: str, identity: str) -> Path:
        namespace = self._namespace(name)
        folders = sorted(namespace.iterdir())
        if len(folders) > MAX_EVOLUTION_EVENTS:
            raise ValueError("evolution registry exceeds capacity")
        found = []
        for folder in folders:
            if name == "transitions":
                _identity(folder.name, "skill-version-")
            else:
                # Revalidate path-safe aliases, including stale registry members.
                require_identifiers([folder.name])
            found.extend(path for path in self._chain(name, folder.name) if path.name.endswith(identity + ".json"))
        if len(found) != 1:
            raise ValueError("evolution event has no unique registry identity")
        return found[0]

    def _transitions(self, version_id: str, budget: _EvolutionBudget, *, until: str | None = None
                     ) -> tuple[list[TransitionRecord], dict[str, ValidationPlanBody], dict[str, ValidationReceiptBody]]:
        files = self._chain("transitions", version_id)
        records: list[TransitionRecord] = []
        plans: dict[str, ValidationPlanBody] = {}
        history: dict[str, ValidationReceiptBody] = {}
        previous = None
        for path in files:
            value = self._read(path, parse_transition, budget)
            original = parse_transition_payload(value.payload_json)
            expected_id = None if not records else records[-1].transition_id
            if original.approval.previous_transition_id != expected_id or original.version_id != version_id:
                raise ValueError("transition chain changed its predecessor or candidate")
            derived = self._derive(original.approval, original.plan_approval, previous, plans, history, budget)
            if original != derived or not path.name.endswith(value.transition_id + ".json"):
                raise ValueError("transition differs from complete original validation facts")
            records.append(value)
            previous = original
            if value.transition_id == until:
                break
        if until is not None and (not records or records[-1].transition_id != until):
            raise ValueError("transition predecessor is missing")
        if self._chain("transitions", version_id) != files:
            raise ValueError("transition registry changed during read")
        for path, record in zip(files, records, strict=False):
            if self._read(path, parse_transition, budget) != record:
                raise ValueError("transition changed during decision audit")
        return records, plans, history

    def decide_skill_transition(self, plan_id: str, *, approval: TransitionApproval,
                               plan_approval: ValidationApproval) -> TransitionRecord:
        budget = _EvolutionBudget(self.limits.max_capture_read_bytes)
        try:
            return self._decide(plan_id, approval, plan_approval, budget)
        finally:
            budget.clear()

    def _decide(self, plan_id: str, approval: TransitionApproval,
                plan_approval: ValidationApproval, budget: _EvolutionBudget) -> TransitionRecord:
        approval, plan_approval = _typed(HostTransitionApproval, approval), _typed(HostValidationPlanApproval, plan_approval)
        if approval.plan_id != plan_id:
            raise ValueError("transition approval names another plan")
        plan = self._plan_once(plan_id, plan_approval, budget)
        version_id = parse_plan_payload(plan.payload_json).spec.new_version_id
        records, plans, history = self._transitions(version_id, budget, until=approval.previous_transition_id)
        previous = None if not records else parse_transition_payload(records[-1].payload_json)
        if approval.previous_transition_id is None and records:
            # Exact repeat is handled below; deriving a new first event cannot reset history.
            plans, history, previous = {}, {}, None
        payload = self._derive(approval, plan_approval, previous, plans, history, budget)
        key = content_hash(payload)
        record_type: Any = SkillTransitionV2 if payload.schema_version == 2 else SkillTransition
        value = record_type(transition_id="skill-transition-" + key, content_sha256=key, payload_json=encode(payload))
        chain = self._chain("transitions", version_id)
        if any(path.name.endswith(value.transition_id + ".json") for path in chain):
            original = self._read_transition(value.transition_id, approval, budget)
            self._close_plans(budget)
            return original
        head = None if not chain else self._read(chain[-1], parse_transition, budget).transition_id
        if head != approval.previous_transition_id:
            raise ValueError("transition requires the current expected head")
        # Reaudit every old source and the proposed current facts before mutation.
        checked, retry_plans, retry_history = self._transitions(version_id, budget, until=approval.previous_transition_id)
        prior = None if not checked else parse_transition_payload(checked[-1].payload_json)
        if self._derive(approval, plan_approval, prior, retry_plans, retry_history, budget) != payload:
            raise ValueError("transition source changed before publication")
        self._close_plans(budget)
        self._publish("transitions", version_id, len(chain) + 1, value.transition_id, value, budget)
        return value

    def read_transition(self, transition_id: str, *, approval: TransitionApproval) -> TransitionRecord:
        _identity(transition_id, "skill-transition-")
        approval = _typed(HostTransitionApproval, approval)
        budget = _EvolutionBudget(self.limits.max_capture_read_bytes)
        try:
            value = self._read_transition(transition_id, approval, budget)
            self._close_plans(budget)
            return value
        finally:
            budget.clear()

    def _read_transition(self, transition_id: str, approval: TransitionApproval,
                         budget: _EvolutionBudget) -> TransitionRecord:
        path = self._locate("transitions", transition_id)
        value = self._read(path, parse_transition, budget)
        payload = parse_transition_payload(value.payload_json)
        if value.transition_id != transition_id or payload.approval != approval:
            raise ValueError("transition differs from external original approval")
        records, _, _ = self._transitions(payload.version_id, budget, until=transition_id)
        if records[-1] != value:
            raise ValueError("transition changed during original-source audit")
        return value

    def _publication_payload(self, approval: PublicationApproval, version_approval: VersionApproval,
                             transition_approval: TransitionApproval | None, previous: list[PublicationRecord],
                             budget: _EvolutionBudget
                             ) -> PublicationBody:
        version = self.validation._read_version(approval.version_id, version_approval, budget, set())
        body = parse_version_payload(version.payload_json)
        if approval.schema_version == 1 and (body.schema_version != 1 or any(old.schema_version != 1 for old in previous)):
            raise ValueError("legacy publication cannot acquire a model version or model history")
        if approval.split not in version_approval.allowed_splits:
            raise ValueError("sandbox split is outside original version authority")
        head = None if not previous else parse_publication_payload(previous[-1].payload_json)
        if approval.previous_publication_id != (None if not previous else previous[-1].publication_id):
            raise ValueError("publication requires the expected prior head")
        if head is not None and (head.approval.alias != approval.alias or head.approval.split != approval.split or
                                 head.version_approval.skill_id != version_approval.skill_id):
            raise ValueError("publication cannot widen split, change alias or change Skill identity")
        if approval.action == "initialize_seed":
            if head is not None or body.approval.parent_version_id is not None or isinstance(body, ModelVersionPayload):
                raise ValueError("only an original seed initializes a sandbox alias")
        elif approval.action == "activate":
            if transition_approval is None or approval.transition_id is None or head is None:
                raise ValueError("activation lacks an approved transition or old selection")
            transition = self._read_transition(approval.transition_id, transition_approval, budget)
            decision = parse_transition_payload(transition.payload_json)
            plan = self._plan_once(decision.approval.plan_id, decision.plan_approval, budget)
            comparison = parse_plan_payload(plan.payload_json).spec
            if (decision.state != "synthetic_eligible" or decision.version_id != approval.version_id or
                    comparison.old_version_id != head.approval.version_id or comparison.new_version_id != approval.version_id):
                raise ValueError("activation must qualify the currently selected parent and this revision")
        elif approval.action == "rollback":
            if head is None or not any(parse_publication_payload(old.payload_json).approval.version_id ==
                                       approval.version_id for old in previous[:-1]):
                raise ValueError("rollback target was not an earlier publication of this alias")
            # Previously published versions are not necessarily ancestors (branches).
            current = parse_version_payload(self.validation._read_version(
                head.approval.version_id, head.version_approval, budget, set()).payload_json)
            found = False
            for _ in range(MAX_EVOLUTION_EVENTS):
                if current.approval.parent_version_id is None or current.parent_approval is None:
                    break
                ancestor = self.validation._read_version(current.approval.parent_version_id, current.parent_approval, budget, set())
                if ancestor.version_id == approval.version_id:
                    found = True
                    break
                current = parse_version_payload(ancestor.payload_json)
            if not found:
                raise ValueError("rollback target is not a parent ancestor of the current selection")
        payload_type: Any = PublicationPayloadV2 if approval.schema_version == 2 else PublicationPayload
        return payload_type(approval=approval, version_approval=version_approval,
            transition_approval=transition_approval, previous_approval=None if head is None else head.approval,
            sequence=len(previous) + 1, projection_sha256=body.approval.projection_sha256)

    def _publications(self, alias: str, budget: _EvolutionBudget, *, until: str | None = None) -> list[PublicationRecord]:
        files = self._chain("publications", alias)
        records: list[PublicationRecord] = []
        for path in files:
            value = self._read(path, parse_publication, budget)
            original = parse_publication_payload(value.payload_json)
            if original.approval.alias != alias:
                raise ValueError("publication changed its registry alias")
            derived = self._publication_payload(original.approval, original.version_approval,
                                                 original.transition_approval, records, budget)
            if original != derived or not path.name.endswith(value.publication_id + ".json"):
                raise ValueError("sandbox publication differs from its complete sources")
            records.append(value)
            if value.publication_id == until:
                break
        if until is not None and (not records or records[-1].publication_id != until):
            raise ValueError("sandbox publication is missing")
        if self._chain("publications", alias) != files:
            raise ValueError("sandbox registry changed during read")
        for path, record in zip(files, records, strict=False):
            if self._read(path, parse_publication, budget) != record:
                raise ValueError("sandbox publication changed during source audit")
        return records

    def publish_alias(self, *, approval: PublicationApproval, version_approval: VersionApproval,
                      transition_approval: TransitionApproval | None = None) -> PublicationRecord:
        budget = _EvolutionBudget(self.limits.max_capture_read_bytes)
        try:
            return self._publish_alias(approval, version_approval, transition_approval, budget)
        finally:
            budget.clear()

    def _publish_alias(self, approval: PublicationApproval, version_approval: VersionApproval,
                       transition_approval: TransitionApproval | None, budget: _EvolutionBudget) -> PublicationRecord:
        approval, version_approval = _typed(HostPublicationApproval, approval), _typed(HostSkillVersionApproval, version_approval)
        if transition_approval is not None:
            transition_approval = _typed(HostTransitionApproval, transition_approval)
        records = self._publications(approval.alias, budget, until=approval.previous_publication_id)
        if approval.previous_publication_id is None and records:
            records = []
        payload = self._publication_payload(approval, version_approval, transition_approval, records, budget)
        key = content_hash(payload)
        record_type: Any = SkillPublicationV2 if payload.schema_version == 2 else SkillPublication
        value = record_type(publication_id="skill-publication-" + key, content_sha256=key, payload_json=encode(payload))
        chain = self._chain("publications", approval.alias)
        if any(path.name.endswith(value.publication_id + ".json") for path in chain):
            original = self._read_publication(value.publication_id, approval, budget)
            self._close_plans(budget)
            return original
        head = None if not chain else self._read(chain[-1], parse_publication, budget).publication_id
        if head != approval.previous_publication_id:
            raise ValueError("publication requires the current expected head")
        self._activation_head(approval, budget)
        checked = self._publications(approval.alias, budget, until=approval.previous_publication_id)
        if self._publication_payload(approval, version_approval, transition_approval, checked, budget) != payload:
            raise ValueError("publication source changed before mutation")
        self._close_plans(budget)
        self._activation_head(approval, budget)
        self._publish("publications", approval.alias, len(chain) + 1, value.publication_id, value, budget)
        return value

    def _activation_head(self, approval: PublicationApproval, budget: _EvolutionBudget) -> None:
        # New activation cannot cherry-pick a formerly eligible prefix. Reads
        # and exact idempotent replay retain the original historical approval.
        if approval.action == "activate":
            chain = self._chain("transitions", approval.version_id)
            if not chain or self._read(chain[-1], parse_transition, budget).transition_id != approval.transition_id:
                raise ValueError("activation requires the candidate's current transition head")

    def read_publication(self, publication_id: str, *, approval: PublicationApproval) -> PublicationRecord:
        _identity(publication_id, "skill-publication-")
        approval = _typed(HostPublicationApproval, approval)
        budget = _EvolutionBudget(self.limits.max_capture_read_bytes)
        try:
            value = self._read_publication(publication_id, approval, budget)
            self._close_plans(budget)
            return value
        finally:
            budget.clear()

    def _read_publication(self, publication_id: str, approval: PublicationApproval,
                          budget: _EvolutionBudget) -> PublicationRecord:
        path = self._locate("publications", publication_id)
        value = self._read(path, parse_publication, budget)
        original = parse_publication_payload(value.payload_json)
        if value.publication_id != publication_id or original.approval != approval:
            raise ValueError("publication differs from external original approval")
        records = self._publications(approval.alias, budget, until=publication_id)
        if records[-1] != value:
            raise ValueError("publication changed during source audit")
        return value

    def freeze_alias(self, alias: str, *, approval: PublicationApproval) -> SkillSelection:
        budget = _EvolutionBudget(self.limits.max_capture_read_bytes)
        try:
            value = self._freeze_alias(alias, approval, budget)
            self._close_plans(budget)
            return value
        finally:
            budget.clear()

    def _freeze_alias(self, alias: str, approval: PublicationApproval, budget: _EvolutionBudget) -> SkillSelection:
        approval = _typed(HostPublicationApproval, approval)
        if alias != approval.alias:
            raise ValueError("selection names another alias")
        records = self._publications(alias, budget)
        if not records:
            raise ValueError("selection alias has no publication")
        latest = records[-1]
        payload = parse_publication_payload(latest.payload_json)
        if payload.approval != approval:
            raise ValueError("selection must approve the current alias head")
        selection_type: Any = FrozenSkillSelectionV2 if payload.schema_version == 2 else FrozenSkillSelection
        return selection_type(alias=alias, publication_id=latest.publication_id,
            version_id=payload.approval.version_id, projection_sha256=payload.projection_sha256, split=payload.approval.split)

    def read_selection(self, selection: SkillSelection, *, publication_approval: PublicationApproval):
        budget = _EvolutionBudget(self.limits.max_capture_read_bytes)
        try:
            value = self._read_selection(selection, publication_approval, budget)
            self._close_plans(budget)
            return value
        finally:
            budget.clear()

    def _read_selection(self, selection: SkillSelection, publication_approval: PublicationApproval,
                        budget: _EvolutionBudget):
        selection = _typed(FrozenSkillSelection, selection)
        publication_approval = _typed(HostPublicationApproval, publication_approval)
        record = self._read_publication(selection.publication_id, publication_approval, budget)
        payload = parse_publication_payload(record.payload_json)
        selection_type: Any = FrozenSkillSelectionV2 if payload.schema_version == 2 else FrozenSkillSelection
        expected = selection_type(alias=payload.approval.alias, publication_id=record.publication_id,
            version_id=payload.approval.version_id, projection_sha256=payload.projection_sha256, split=payload.approval.split)
        if selection != expected:
            raise ValueError("frozen selection differs from its original publication")
        version = self.validation._read_version(selection.version_id, payload.version_approval, budget, set())
        return parse_version_payload(version.payload_json).projection

    def assert_current_selection(self, selection: SkillSelection, *, publication_approval: PublicationApproval) -> None:
        """New deployment requires the current publication and candidate decision."""
        selection = _typed(FrozenSkillSelection, selection)
        publication_approval = _typed(HostPublicationApproval, publication_approval)
        budget = _EvolutionBudget(self.limits.max_capture_read_bytes)
        try:
            if self._freeze_alias(selection.alias, publication_approval, budget) != selection:
                raise ValueError("new deployment must freeze the current exact alias selection")
            self._activation_head(publication_approval, budget)
            self._selected_model_head(selection, publication_approval, budget)
            self._read_selection(selection, publication_approval, budget)
            self._close_plans(budget)
            self._activation_head(publication_approval, budget)
            self._selected_model_head(selection, publication_approval, budget)
            if self._freeze_alias(selection.alias, publication_approval, budget) != selection:
                raise ValueError("new deployment alias changed before current-selection closure")
        finally:
            budget.clear()

    def _selected_model_head(self, selection: SkillSelection, approval: PublicationApproval, budget: _EvolutionBudget) -> None:
        publication = self._read_publication(selection.publication_id, approval, budget)
        body = parse_publication_payload(publication.payload_json)
        if isinstance(body.version_approval, HostModelSkillVersionApproval):
            events, _plans, _receipts = self._transitions(selection.version_id, budget)
            if not events or parse_transition_payload(events[-1].payload_json).state != "synthetic_eligible":
                raise ValueError("new deployment requires selected model version's current eligible decision")

    def export_selection_sources(self, selection: SkillSelection, *,
                                 publication_approval: PublicationApproval) -> dict[str, bytes]:
        return self._export_selection_sources(selection, publication_approval)[0]

    def export_selection_bundle(self, selection: SkillSelection, *,
                                publication_approval: PublicationApproval) -> tuple[dict[str, bytes], tuple[str, ...]]:
        return self._export_selection_sources(selection, publication_approval)

    def export_selection_source_directories(self, selection: SkillSelection, *,
                                            publication_approval: PublicationApproval) -> tuple[str, ...]:
        return self._export_selection_sources(selection, publication_approval)[1]

    def _export_selection_sources(self, selection: SkillSelection, publication_approval: PublicationApproval
                                  ) -> tuple[dict[str, bytes], tuple[str, ...]]:
        """Closed historical selection; later alias updates are irrelevant."""
        selection = _typed(FrozenSkillSelection, selection)
        publication_approval = _typed(HostPublicationApproval, publication_approval)
        budget = _EvolutionBudget(self.limits.max_capture_read_bytes)
        files: dict[str, bytes] = {}
        directories: set[str] = set()
        exported_versions: set[str] = set()
        captured: list[_ClosedTree] = []

        def add(relative: str, raw: bytes) -> None:
            if relative in files and files[relative] != raw:
                raise ValueError("selection export has conflicting original bytes")
            files[relative] = raw
            if len(files) > self.limits.max_entries or sum(map(len, files.values())) > self.limits.max_total_bytes:
                raise ValueError("selection export exceeds bounded source closure")
            directories.update(parent.as_posix() for parent in Path(relative).parents if parent.as_posix() != ".")

        def version_sources(identifier: str, external: VersionApproval) -> None:
            if identifier in exported_versions:
                return
            exported_versions.add(identifier)
            originals, original_directories = self.validation._export_version_bundle(identifier, external, budget)
            for relative, raw in originals.items():
                add(relative, raw)
            directories.update(original_directories)

        def file(path: Path, expected: BaseModel) -> None:
            reader = _ClosedTree(self.root, self.limits, budget)
            relative = path.relative_to(self.root).as_posix()
            raw = reader.capture_file(relative)
            if raw != encode(expected).encode("utf-8"):
                raise ValueError("selection export record differs from its authenticated original")
            captured.append(reader)
            add(relative, raw)

        try:
            projection = self._read_selection(selection, publication_approval, budget)
            publications = self._publications(selection.alias, budget, until=selection.publication_id)
            receipt_ids: set[str] = set()
            for record in publications:
                file(self._locate("publications", record.publication_id), record)
                body = parse_publication_payload(record.payload_json)
                version_sources(body.approval.version_id, body.version_approval)
                if body.approval.transition_id is not None:
                    transitions, plans, history = self._transitions(body.approval.version_id, budget, until=body.approval.transition_id)
                    for event in transitions:
                        file(self._locate("transitions", event.transition_id), event)
                    for plan_id, plan in plans.items():
                        file(self.root / "plans" / (plan_id + ".json"), budget.plans[plan_id][1])
                        version_sources(plan.spec.old_version_id, plan.approval.old_version_approval)
                        version_sources(plan.spec.new_version_id, plan.approval.new_version_approval)
                    for receipt_id in history:
                        if receipt_id in receipt_ids:
                            continue
                        receipt_ids.add(receipt_id)
                        relative = "receipts/" + receipt_id
                        tree = _ClosedTree(self.root / relative, self.limits, budget)
                        tree.scan(((".", None),))
                        captured.append(tree)
                        for name, raw in tree.raw.items():
                            add(relative + "/" + name, raw)
                        directories.add(relative)
                        directories.update(relative + "/" + name for name in tree.directories)
            self._close_plans(budget)
            if self._read_selection(selection, publication_approval, budget) != projection:
                raise ValueError("selection export sources changed during closure")
            budget.require_remaining(sum(tree.byte_count for tree in captured))
            for tree in captured:
                tree.finish()
            if len(files) + len(directories) > self.limits.max_entries:
                raise ValueError("selection export complete inventory exceeds capacity")
            return dict(sorted(files.items())), tuple(sorted(directories))
        finally:
            budget.clear()
