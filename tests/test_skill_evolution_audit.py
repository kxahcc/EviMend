"""Comparison and source audits preserve approved provenance and operation closure."""

from __future__ import annotations

import copy
import json
from dataclasses import replace

import pytest
from skill_evolution_support import (
    Inputs,
    Runs,
    bundle,
    clone_bundle,
    clone_store,
    decide,
    freeze,
    fresh_inputs,
    known_role,
    policy,
    transition_approval,
)
from test_feedback_integration import inventory
from test_skill_validation_integration import plan_approval

from dfx_agent import provenance
from dfx_agent.metrics import parse_evaluation_manifest
from dfx_agent.model_calls import canonical
from dfx_agent.skill_evolution import SkillTransitionStore
from dfx_agent.skill_evolution_contracts import PlanPayload, ReceiptPayload
from dfx_agent.skill_transition_contracts import HostTransitionApproval

pytest_plugins = ("skill_evolution_support",)


@pytest.mark.parametrize("problem", ["duplicate_identity", "policy_reset", "configuration_reset"])
def test_retry_cannot_recount_old_cases_or_reset_frozen_policy(
        eligible_bundle, tmp_path_factory: pytest.TempPathFactory, problem: str) -> None:
    original, transition, _host = eligible_bundle
    validation = clone_store(original.validation, tmp_path_factory)
    retry = fresh_inputs(replace(original.runs.inputs, store=validation), tmp_path_factory,
                         share_store=True, distinct_cases=problem != "duplicate_identity")
    new_plan, new_host = freeze(retry, policy(min_net_gain=0) if problem == "policy_reset" else policy())
    if problem == "configuration_reset":
        specification = PlanPayload.model_validate_json(new_plan.payload_json).spec
        pairs = []
        for pair in specification.pairs:
            changes = {}
            for role in ("old", "new"):
                raw = json.loads(getattr(pair, role + "_manifest_json"))
                raw["configuration"]["max_calls"] += 1
                changes[role + "_manifest_json"] = canonical(parse_evaluation_manifest(raw).model_dump(mode="json"))
            pairs.append(pair.model_copy(update=changes))
        changed = specification.model_copy(update={"pairs": tuple(pairs)})
        new_host = plan_approval(retry, changed)
        new_plan = validation.freeze_plan(changed, retry.root, approval=new_host)
    approved = HostTransitionApproval(approval_id="unsafe-retry", plan_id=new_plan.plan_id,
        receipt_ids=(), previous_transition_id=transition.transition_id)
    before = inventory(validation.root)
    with pytest.raises(ValueError):
        SkillTransitionStore(validation).decide_skill_transition(new_plan.plan_id, approval=approved, plan_approval=new_host)
    assert inventory(validation.root) == before


def test_actual_sdk_receipts_with_different_execution_runtime_cannot_publish_transition(
        base_inputs: Inputs, tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    current = fresh_inputs(base_inputs, tmp_path_factory)
    plan, approved = freeze(current, policy())
    changed_runtime = copy.deepcopy(provenance._runtime_facts())
    changed_runtime["distributions"]["dfx-synthetic-runtime"] = "1"
    changed_runtime["distributions"] = dict(sorted(changed_runtime["distributions"].items()))
    reports = {}
    for case in ("rep", "edge"):
        for role in ("old", "new"):
            if (case, role) == ("rep", "new"):
                # Fault-inject the captured runtime inventory through the real
                # capture and compatibility paths, rather than forging a receipt.
                with monkeypatch.context() as scope:
                    scope.setattr(provenance, "_runtime_facts", lambda: copy.deepcopy(changed_runtime))
                    reports[case, role] = known_role(current, case, role)
            else:
                reports[case, role] = known_role(current, case, role)
    value = bundle(Runs(current, {"eligible": plan}, {"eligible": approved}, reports), tmp_path_factory)
    observed = {json.loads(receipt.contents()["aggregate_json"])["rows"][0]["execution_provenance"]["compatibility_sha256"]
                for receipt in value.receipts.values()}
    assert len(observed) == 2
    before = inventory(value.validation.root)
    with pytest.raises(ValueError):
        decide(value)
    assert inventory(value.validation.root) == before
    for receipt in value.receipts.values():
        assert value.validation.read_receipt(receipt.receipt_id, approval=approved) == receipt


@pytest.mark.parametrize("problem", ["wire_drift", "wire_missing", "provenance_missing"])
def test_comparison_boundary_preserves_unknown_or_rejects_known_tool_schema_drift(eligible_bundle, problem: str) -> None:
    # These altered in-memory copies exercise the private comparison boundary;
    # they are not accepted or published as authenticated validation receipts.
    value, _transition, _approved = eligible_bundle
    plan = PlanPayload.model_validate_json(value.plan.payload_json)
    history = {receipt.receipt_id: ReceiptPayload.model_validate_json(receipt.payload_json)
               for receipt in value.receipts.values()}
    identifier = next(iter(history))
    selected = history[identifier]
    raw = json.loads(selected.aggregate_json)
    row = raw["rows"][0]
    if problem == "wire_drift":
        row["solver_tool_schema_sha256"] = ["f" * 64]
    elif problem == "wire_missing":
        row["solver_tool_schema_sha256"] = []
    else:
        row.pop("execution_provenance")
    history[identifier] = selected.model_copy(update={"aggregate_json": canonical(raw)})
    if problem == "wire_drift":
        with pytest.raises(ValueError):
            value.store._facts(plan, history, {value.plan.plan_id: plan})
    else:
        facts = value.store._facts(plan, history, {value.plan.plan_id: plan})
        state, reasons = value.store._decision(facts, policy())
        assert state == "synthetic_pending" and "execution_compatibility_unknown" in reasons
        assert facts["validation_cost"]["total_tokens"] == 495


def test_transition_requires_current_head_and_reauthenticates_evidence(eligible_bundle, tmp_path_factory: pytest.TempPathFactory) -> None:
    original, transition, approved = eligible_bundle
    validation = clone_store(original.validation, tmp_path_factory)
    value = replace(original, validation=validation, store=SkillTransitionStore(validation))
    stale = transition_approval(value, identifier="stale-first-transition")
    before = inventory(validation.root)
    with pytest.raises(ValueError):
        decide(value, approval=stale)
    assert inventory(validation.root) == before
    appended_approval = transition_approval(value, previous=transition.transition_id, identifier="append-same-attempt")
    appended, _approval = decide(value, approval=appended_approval)
    facts = json.loads(appended.contents()["facts_json"])
    assert facts["attempt_count"] == 1 and facts["history_receipt_count"] == 4
    assert facts["validation_cost"]["total_tokens"] == 495
    assert value.store.read_transition(transition.transition_id, approval=approved) == transition
    receipt = next(iter(value.receipts.values()))
    (validation.root / "receipts" / receipt.receipt_id / "receipt.json").write_bytes(b"{}")
    corrupt = inventory(validation.root)
    with pytest.raises(ValueError):
        value.store.read_transition(appended.transition_id, approval=appended_approval)
    assert inventory(validation.root) == corrupt

def test_public_read_reauthenticates_plan_twice_with_one_budget_per_call(
        eligible_bundle, tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    original, transition, approved = eligible_bundle
    validation = clone_store(original.validation, tmp_path_factory)
    store = SkillTransitionStore(validation)
    real_read = validation._read_plan
    observations = []

    def observe(plan_id, plan_host, budget):
        observations.append((plan_id, budget))
        return real_read(plan_id, plan_host, budget)

    monkeypatch.setattr(validation, "_read_plan", observe)
    before = inventory(validation.root)
    assert store.read_transition(transition.transition_id, approval=approved) == transition
    assert [plan_id for plan_id, _budget in observations] == [original.plan.plan_id] * 2
    first_budget = observations[0][1]
    assert observations[1][1] is first_budget
    observations.clear()
    assert store.read_transition(transition.transition_id, approval=approved) == transition
    assert [plan_id for plan_id, _budget in observations] == [original.plan.plan_id] * 2
    assert observations[0][1] is observations[1][1] and observations[0][1] is not first_budget
    assert inventory(validation.root) == before


def test_plan_closure_source_drift_rejects_before_formal_transition_publication(
        prepared_bundles, tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    value = clone_bundle(prepared_bundles(), tmp_path_factory)
    validation = value.validation
    real_read = validation._read_plan
    observations = []
    version = validation.root / "versions" / (value.runs.inputs.new_version.version_id + ".json")
    before = inventory(validation.root)
    assert not (validation.root / "transitions").exists()

    def corrupt_before_closing_read(plan_id, plan_host, budget):
        observations.append((plan_id, budget))
        if len(observations) == 2:
            version.write_bytes(b"{}")
        return real_read(plan_id, plan_host, budget)

    monkeypatch.setattr(validation, "_read_plan", corrupt_before_closing_read)
    with pytest.raises(ValueError):
        decide(value)
    assert [plan_id for plan_id, _budget in observations] == [value.plan.plan_id] * 2
    assert observations[0][1] is observations[1][1]
    assert not (validation.root / "transitions").exists() and version.read_bytes() == b"{}"
    expected = dict(before)
    expected[version.relative_to(validation.root).as_posix()] = b"{}"
    assert inventory(validation.root) == expected
    with pytest.raises(ValueError):
        decide(value)
    assert inventory(validation.root) == expected
