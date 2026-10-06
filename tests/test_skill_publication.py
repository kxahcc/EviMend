"""Sandbox publication changes heads while retaining immutable frozen guidance."""

from __future__ import annotations

from dataclasses import replace

import pytest
from skill_evolution_support import (
    clone_store,
    decide,
    freeze,
    fresh_inputs,
    policy,
    seed_publication,
    transition_approval,
)
from test_feedback_integration import inventory
from test_knowledge_retrieval import guidance

from dfx_agent.knowledge_contracts import SolverProjection
from dfx_agent.model_calls import canonical, sha256
from dfx_agent.skill_evolution import SkillTransitionStore
from dfx_agent.skill_evolution_contracts import HostSkillVersionApproval
from dfx_agent.skill_transition_contracts import HostPublicationApproval

pytest_plugins = ("skill_evolution_support",)


def test_initialize_activate_and_rollback_preserve_old_frozen_full_guidance(
        eligible_bundle, tmp_path_factory: pytest.TempPathFactory) -> None:
    original, transition, transition_host = eligible_bundle
    validation = clone_store(original.validation, tmp_path_factory)
    value = replace(original, validation=validation, store=SkillTransitionStore(validation))
    inputs = value.runs.inputs
    seed_record, seed_host = seed_publication(value)
    seed_selection = value.store.freeze_alias("capacity", approval=seed_host)
    old_projection = value.store.read_selection(seed_selection, publication_approval=seed_host)
    activate_host = HostPublicationApproval(approval_id="activate-revision", alias="capacity", action="activate",
        version_id=inputs.new_version.version_id, transition_id=transition.transition_id,
        previous_publication_id=seed_record.publication_id, split="development")
    activated = value.store.publish_alias(approval=activate_host, version_approval=inputs.new_approval, transition_approval=transition_host)
    revised_selection = value.store.freeze_alias("capacity", approval=activate_host)
    revised = value.store.read_selection(revised_selection, publication_approval=activate_host)
    assert revised.guidance != old_projection.guidance
    assert value.store.read_selection(seed_selection, publication_approval=seed_host) == old_projection
    future = fresh_inputs(replace(inputs, store=validation), tmp_path_factory, share_store=True, distinct_cases=True)
    future_plan, future_host = freeze(future, policy())
    future_bundle = replace(value, plan=future_plan, plan_approval=future_host, receipts={})
    pending_host = transition_approval(future_bundle, receipts=[], previous=transition.transition_id,
                                      identifier="later-pending")
    later_pending, _pending_host = decide(future_bundle, approval=pending_host)
    assert later_pending.contents()["state"] == "synthetic_pending"
    # Later validation does not rewrite the approved historical publication.
    assert value.store.read_publication(activated.publication_id, approval=activate_host) == activated
    assert value.store.read_selection(revised_selection, publication_approval=activate_host) == revised
    assert value.store.publish_alias(approval=activate_host, version_approval=inputs.new_approval,
                                    transition_approval=transition_host) == activated
    rollback_host = HostPublicationApproval(approval_id="rollback-parent", alias="capacity", action="rollback",
        version_id=inputs.old_version.version_id, previous_publication_id=activated.publication_id, split="development")
    rollback = value.store.publish_alias(approval=rollback_host, version_approval=inputs.old_approval)
    latest = value.store.freeze_alias("capacity", approval=rollback_host)
    assert latest.version_id == inputs.old_version.version_id and latest.application_qualification == "none"
    assert value.store.read_selection(latest, publication_approval=rollback_host) == old_projection
    assert value.store.read_selection(revised_selection, publication_approval=activate_host) == revised
    assert value.store.read_publication(seed_record.publication_id, approval=seed_host) == seed_record
    assert value.store.read_publication(rollback.publication_id, approval=rollback_host) == rollback
    assert [seed_record.contents()["sequence"], activated.contents()["sequence"], rollback.contents()["sequence"]] == [1, 2, 3]
    assert all(record.contents()["application_qualification"] == "none" for record in (seed_record, activated, rollback))


@pytest.mark.parametrize("problem", ["scope_widening", "ineligible", "stale_transition", "foreign_rollback", "stale_head"])
def test_alias_publication_rejects_unqualified_or_unrelated_changes_without_writes(
        eligible_bundle, tmp_path_factory: pytest.TempPathFactory, problem: str) -> None:
    original, transition, transition_host = eligible_bundle
    validation = clone_store(original.validation, tmp_path_factory)
    value = replace(original, validation=validation, store=SkillTransitionStore(validation))
    inputs = value.runs.inputs
    seed_record, _seed_host = seed_publication(value)
    version_host = inputs.new_approval
    if problem == "foreign_rollback":
        projection = SolverProjection(guidance=guidance("offset"), derivation_authority="host_curated", validation_scopes=())
        version_host = HostSkillVersionApproval(approval_id="unpublished-seed", skill_id="capacity-process",
            projection_sha256=sha256(canonical(projection.model_dump(mode="json"))),
            allowed_splits=("development", "candidate_validation", "confirmation"))
        foreign = validation.create_seed(projection, approval=version_host)
        approved = HostPublicationApproval(approval_id="foreign-rollback", alias="capacity", action="rollback",
            version_id=foreign.version_id, previous_publication_id=seed_record.publication_id)
        transition_host = None
    else:
        if problem in {"ineligible", "stale_transition"}:
            future = fresh_inputs(replace(inputs, store=validation), tmp_path_factory, share_store=True, distinct_cases=True)
            future_plan, future_host = freeze(future, policy())
            future_bundle = replace(value, plan=future_plan, plan_approval=future_host, receipts={})
            pending_host = transition_approval(future_bundle, receipts=[], previous=transition.transition_id,
                                               identifier="pending-publication")
            pending, pending_approval = decide(future_bundle, approval=pending_host)
            if problem == "ineligible":
                transition_id, transition_host = pending.transition_id, pending_approval
            else:
                transition_id = transition.transition_id
        else:
            transition_id = transition.transition_id
        approved = HostPublicationApproval(approval_id="unsafe-update", alias="capacity", action="activate",
            version_id=inputs.new_version.version_id, transition_id=transition_id,
            previous_publication_id="skill-publication-" + "f" * 64 if problem == "stale_head" else seed_record.publication_id,
            split="candidate_validation" if problem == "scope_widening" else "development")
    before = inventory(validation.root)
    with pytest.raises(ValueError):
        value.store.publish_alias(approval=approved, version_approval=version_host, transition_approval=transition_host)
    assert inventory(validation.root) == before


def test_alias_read_rejects_chain_or_external_selection_drift(eligible_bundle, tmp_path_factory: pytest.TempPathFactory) -> None:
    original, _transition, _host = eligible_bundle
    validation = clone_store(original.validation, tmp_path_factory)
    value = replace(original, validation=validation, store=SkillTransitionStore(validation))
    publication, approved = seed_publication(value)
    selection = value.store.freeze_alias("capacity", approval=approved)
    changed = selection.model_copy(update={"projection_sha256": "f" * 64})
    before = inventory(validation.root)
    with pytest.raises(ValueError):
        value.store.read_selection(changed, publication_approval=approved)
    assert inventory(validation.root) == before
    path = validation.root / "publications" / "capacity" / ("0001-" + publication.publication_id + ".json")
    path.write_bytes(b"{}")
    corrupt = inventory(validation.root)
    with pytest.raises(ValueError):
        value.store.read_publication(publication.publication_id, approval=approved)
    assert inventory(validation.root) == corrupt
