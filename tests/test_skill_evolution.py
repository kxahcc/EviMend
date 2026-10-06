"""Basic synthetic Skill states, accounted costs and actual-use gates."""

from __future__ import annotations

import json

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
from test_feedback_integration import inventory
from test_skill_validation_integration import run_role

pytest_plugins = ("skill_evolution_support",)


def test_only_synthetic_eligible_with_known_actual_cost_and_complete_use(eligible_bundle) -> None:
    value, transition, approved = eligible_bundle
    payload = transition.contents()
    facts = json.loads(payload["facts_json"])
    assert payload["state"] == "synthetic_eligible" and payload["application_qualification"] == "none"
    assert facts["net_gain_numerator"] == 1 and facts["success_losses"] == 0
    assert facts["validation_cost"]["total_tokens"] == 495
    assert facts["validation_cost"]["known_reserved_calls"] == 33
    assert facts["validation_cost"]["monetary_cost"] is None
    assert facts["learning_cost"] == {"status": "not_run", "tokens": None, "monetary_cost": None}
    assert all(pair["comparable"] for pair in facts["pairs"])
    assert facts["pairs"][0]["roles"]["new"]["first_candidate_check_status"] == "fail"
    before = inventory(value.validation.root)
    assert value.store.read_transition(transition.transition_id, approval=approved) == transition
    assert inventory(value.validation.root) == before


@pytest.mark.parametrize("name", ["unset", "partial"])
def test_unset_thresholds_keep_known_results_pending(prepared_bundles, tmp_path_factory: pytest.TempPathFactory, name: str) -> None:
    value = clone_bundle(prepared_bundles(name), tmp_path_factory)
    transition, _approved = decide(value)
    assert transition.contents()["state"] == "synthetic_pending"
    assert "policy_thresholds_unset" in transition.contents()["reasons"]
    assert json.loads(transition.contents()["facts_json"])["validation_cost"]["total_tokens"] == 495


@pytest.mark.parametrize(("name", "reason"), [("net_gain", "net_gain_below_policy"),
    ("token_budget", "validation_token_budget"), ("call_budget", "validation_call_budget"),
    ("coverage", "boundary_coverage_insufficient")])
def test_explicit_failed_policy_rejects_without_erasing_results(
        prepared_bundles, tmp_path_factory: pytest.TempPathFactory, name: str, reason: str) -> None:
    value = clone_bundle(prepared_bundles(name), tmp_path_factory)
    transition, _approved = decide(value)
    payload = transition.contents()
    assert payload["state"] == "synthetic_rejected" and reason in payload["reasons"]
    assert payload["application_qualification"] == "none"
    assert json.loads(payload["facts_json"])["validation_cost"]["total_tokens"] == 495


def test_missing_planned_role_is_pending_and_never_zero_cost(prepared_bundles, tmp_path_factory: pytest.TempPathFactory) -> None:
    value = clone_bundle(prepared_bundles(), tmp_path_factory)
    selected = [receipt for key, receipt in value.receipts.items() if key != ("edge", "new")]
    transition, _approved = decide(value, approval=transition_approval(value, receipts=selected))
    payload = transition.contents()
    facts = json.loads(payload["facts_json"])
    assert payload["state"] == "synthetic_pending" and "incomplete_planned_roles" in payload["reasons"]
    assert facts["validation_cost"]["total_tokens"] is None
    assert facts["validation_cost"]["known_tokens_lower_bound"] == 360
    assert facts["pairs"][1]["roles"]["new"]["cost"] is None


def test_usage_unknown_survives_full_actual_validation(base_inputs: Inputs, tmp_path_factory: pytest.TempPathFactory) -> None:
    current = fresh_inputs(base_inputs, tmp_path_factory)
    plan, approved = freeze(current, policy())
    reports = {(case, role): run_role(current, case, role) for case in ("rep", "edge") for role in ("old", "new")}
    value = bundle(Runs(current, {"eligible": plan}, {"eligible": approved}, reports), tmp_path_factory)
    transition, _approval = decide(value)
    facts = json.loads(transition.contents()["facts_json"])
    assert transition.contents()["state"] == "synthetic_pending"
    assert "validation_cost_unknown" in transition.contents()["reasons"]
    assert facts["validation_cost"]["known_tokens_lower_bound"] == 420
    assert facts["validation_cost"]["known_reserved_calls"] == 36 and facts["validation_cost"]["total_tokens"] is None


def test_catalog_without_actual_read_is_pending_and_keeps_known_cost(base_inputs: Inputs, tmp_path_factory: pytest.TempPathFactory) -> None:
    current = fresh_inputs(base_inputs, tmp_path_factory)
    plan, approved = freeze(current, policy())
    report = known_role(current, "rep", "new", read_target=False)
    value = bundle(Runs(current, {"eligible": plan}, {"eligible": approved}, {("rep", "new"): report}), tmp_path_factory)
    transition, _approval = decide(value)
    assert transition.contents()["state"] == "synthetic_pending"
    assert "target_use_not_verified" in transition.contents()["reasons"]
    facts = json.loads(transition.contents()["facts_json"])
    assert facts["pairs"][0]["roles"]["new"]["use_status"] == "not_followed"
    assert facts["validation_cost"]["known_tokens_lower_bound"] == 135
