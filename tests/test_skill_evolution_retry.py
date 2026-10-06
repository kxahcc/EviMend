"""Prospective retries preserve damage, unknowns and full source authentication."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest
from skill_evolution_support import (
    Inputs,
    Runs,
    bundle,
    clone_bundle,
    decide,
    freeze,
    fresh_inputs,
    known_role,
    policy,
    transition_approval,
)

from dfx_agent.skill_evolution import SkillTransitionStore
from dfx_agent.skill_transition_contracts import HostTransitionApproval

pytest_plugins = ("skill_evolution_support",)


def test_same_plan_can_fill_missing_role_without_replacing_or_double_charging_history(
        prepared_bundles, tmp_path_factory: pytest.TempPathFactory) -> None:
    value = clone_bundle(prepared_bundles(), tmp_path_factory)
    selected = [receipt for key, receipt in value.receipts.items() if key != ("edge", "new")]
    pending, pending_host = decide(value, approval=transition_approval(value, receipts=selected))
    missing = value.receipts["edge", "new"]
    filled_host = transition_approval(value, receipts=[missing], previous=pending.transition_id, identifier="fill-one-role")
    filled, _host = decide(value, approval=filled_host)
    facts = json.loads(filled.contents()["facts_json"])
    assert filled.contents()["state"] == "synthetic_eligible"
    assert facts["attempt_count"] == 1 and facts["history_receipt_count"] == 4 and facts["missing_history_roles"] == 0
    assert facts["validation_cost"]["total_tokens"] == 495 and facts["validation_cost"]["known_reserved_calls"] == 33
    assert set(pending.contents()["history_receipt_ids"]) < set(filled.contents()["history_receipt_ids"])
    assert value.store.read_transition(pending.transition_id, approval=pending_host) == pending


def test_success_loss_rejects_even_with_a_new_success(base_inputs: Inputs, tmp_path_factory: pytest.TempPathFactory) -> None:
    current = fresh_inputs(base_inputs, tmp_path_factory)
    plan, approved = freeze(current, policy(min_net_gain=0))
    outcomes = {("rep", "old"): "fail", ("rep", "new"): "pass",
                ("edge", "old"): "pass", ("edge", "new"): "fail"}
    reports = {(case, role): known_role(current, case, role, outcome=outcome) for (case, role), outcome in outcomes.items()}
    value = bundle(Runs(current, {"eligible": plan}, {"eligible": approved}, reports), tmp_path_factory)
    transition, first_host = decide(value)
    payload = transition.contents()
    facts = json.loads(payload["facts_json"])
    assert payload["state"] == "synthetic_rejected" and "success_loss_exceeds_policy" in payload["reasons"]
    assert facts["new_successes"] == 1 and facts["success_losses"] == 1 and facts["net_gain_numerator"] == 0
    assert facts["validation_cost"]["total_tokens"] == 450
    retry = fresh_inputs(replace(current, store=value.validation), tmp_path_factory, share_store=True, distinct_cases=True)
    next_plan, next_host = freeze(retry, policy(min_net_gain=0))
    outcomes = {("rep", "old"): "fail", ("rep", "new"): "pass",
                ("edge", "old"): "pass", ("edge", "new"): "pass"}
    receipts = []
    for (case, role), outcome in outcomes.items():
        known_role(retry, case, role, outcome=outcome)
        receipts.append(retry.store.record_validation(next_plan.plan_id, case, role, retry.root, approval=next_host))
    approved = HostTransitionApproval(approval_id="retry-after-loss", plan_id=next_plan.plan_id,
        receipt_ids=tuple(sorted(receipt.receipt_id for receipt in receipts)), previous_transition_id=transition.transition_id)
    retried = value.store.decide_skill_transition(next_plan.plan_id, approval=approved, plan_approval=next_host)
    aggregate = json.loads(retried.contents()["facts_json"])
    assert retried.contents()["state"] == "synthetic_rejected" and "success_loss_exceeds_policy" in retried.contents()["reasons"]
    assert aggregate["new_successes"] == 2 and aggregate["success_losses"] == 1 and aggregate["net_gain_numerator"] == 1
    assert aggregate["net_gain_denominator"] == 4 and aggregate["validation_cost"]["total_tokens"] == 945
    assert aggregate["attempts"][0]["pairs"][1]["roles"]["new"]["outcome"] == "fail"
    assert value.store.read_transition(transition.transition_id, approval=first_host) == transition


def test_retry_keeps_prior_failed_receipt_and_unfinished_attempt_cost_unknown(
        base_inputs: Inputs, tmp_path_factory: pytest.TempPathFactory) -> None:
    first = fresh_inputs(base_inputs, tmp_path_factory)
    first_plan, first_host = freeze(first, policy())
    first_report = known_role(first, "rep", "old", outcome="fail")
    first_receipt = first.store.record_validation(first_plan.plan_id, "rep", "old", first.root, approval=first_host)
    store = SkillTransitionStore(first.store)
    first_approval = HostTransitionApproval(approval_id="initial-incomplete", plan_id=first_plan.plan_id,
                                           receipt_ids=(first_receipt.receipt_id,))
    initial = store.decide_skill_transition(first_plan.plan_id, approval=first_approval, plan_approval=first_host)
    assert initial.contents()["state"] == "synthetic_pending"
    retry = fresh_inputs(first, tmp_path_factory, share_store=True, distinct_cases=True)
    next_plan, next_host = freeze(retry, policy())
    outcomes = {("rep", "old"): "fail", ("rep", "new"): "pass",
                ("edge", "old"): "pass", ("edge", "new"): "pass"}
    receipts = {}
    for (case, role), outcome in outcomes.items():
        known_role(retry, case, role, outcome=outcome)
        receipts[case, role] = retry.store.record_validation(next_plan.plan_id, case, role, retry.root, approval=next_host)
    approved = HostTransitionApproval(approval_id="retry-incomplete", plan_id=next_plan.plan_id,
        receipt_ids=tuple(sorted(receipt.receipt_id for receipt in receipts.values())), previous_transition_id=initial.transition_id)
    decision = store.decide_skill_transition(next_plan.plan_id, approval=approved, plan_approval=next_host)
    payload = decision.contents()
    facts = json.loads(payload["facts_json"])
    assert payload["state"] == "synthetic_pending" and "validation_cost_unknown" in payload["reasons"]
    assert set(payload["history_receipt_ids"]) == {first_receipt.receipt_id, *(receipt.receipt_id for receipt in receipts.values())}
    assert facts["attempt_count"] == 2 and facts["history_receipt_count"] == 5
    assert facts["missing_history_roles"] == 3
    assert facts["attempts"][0]["pairs"][0]["roles"]["old"]["outcome"] == "fail"
    assert facts["validation_cost"]["known_tokens_lower_bound"] == 585
    assert facts["validation_cost"]["known_reserved_calls"] == 39 and facts["validation_cost"]["total_tokens"] is None
    assert first_report["checks"][0]["status"] == "fail"
    assert retry.store.read_receipt(first_receipt.receipt_id, approval=first_host) == first_receipt
    assert store.read_transition(initial.transition_id, approval=first_approval) == initial


def test_completed_distinct_retry_obeys_original_attempt_budget(prepared_bundles, tmp_path_factory: pytest.TempPathFactory) -> None:
    value = clone_bundle(prepared_bundles("attempt_budget"), tmp_path_factory)
    first, _first_host = decide(value)
    assert first.contents()["state"] == "synthetic_eligible"
    retry = fresh_inputs(replace(value.runs.inputs, store=value.validation), tmp_path_factory,
                         share_store=True, distinct_cases=True)
    next_plan, next_host = freeze(retry, policy(max_validation_attempts=1))
    outcomes = {("rep", "old"): "fail", ("rep", "new"): "pass",
                ("edge", "old"): "pass", ("edge", "new"): "pass"}
    receipts = []
    for (case, role), outcome in outcomes.items():
        known_role(retry, case, role, outcome=outcome)
        receipts.append(retry.store.record_validation(next_plan.plan_id, case, role, retry.root, approval=next_host))
    approved = HostTransitionApproval(approval_id="over-attempt-budget", plan_id=next_plan.plan_id,
        receipt_ids=tuple(sorted(receipt.receipt_id for receipt in receipts)), previous_transition_id=first.transition_id)
    decision = value.store.decide_skill_transition(next_plan.plan_id, approval=approved, plan_approval=next_host)
    facts = json.loads(decision.contents()["facts_json"])
    assert decision.contents()["state"] == "synthetic_rejected" and "validation_attempt_budget" in decision.contents()["reasons"]
    assert facts["attempt_count"] == 2 and facts["history_receipt_count"] == 8
    assert facts["validation_cost"]["total_tokens"] == 990 and facts["validation_cost"]["known_reserved_calls"] == 66
