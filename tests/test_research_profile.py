"""Pure ResearchProfile contract checks; no model or application execution."""

from __future__ import annotations

import pytest

from dfx_agent.model_calls import canonical
from dfx_agent.research_profile import profile_from_facts


def _facts() -> dict:
    return {
        "task_id": "task-research-1",
        "run_id": "run-research-1",
        "session": {"max_seconds": 60.0},
        "cost": {
            "reserved_calls": 2,
            "completed_calls": 2,
            "uncertain_calls": 0,
            "known_total_tokens": 30,
            "total_tokens": 30,
            "usage_complete": True,
        },
        "timing": {"repair_ms": 120.0},
        "validation_cost": {
            "known_tokens_lower_bound": 8,
            "total_tokens": 8,
            "known_reserved_calls": 1,
        },
    }


def test_profile_keeps_observed_cost_and_marks_absent_stages_unknown() -> None:
    profile = profile_from_facts(_facts(), lineage_group_id="lineage-research-1")

    assert profile.total.known_tokens_lower_bound == 38
    assert profile.total.total_tokens is None
    assert profile.total.monetary_cost is None
    # Missing stage timings keep the cross-stage total unknown; the observed
    # solver timing remains available on its own stage.
    assert profile.total.elapsed_ms is None
    stages = {stage.stage: stage for stage in profile.stages}
    assert stages["solver"].status == "observed"
    assert stages["validation"].status == "observed"
    assert stages["deployment"].status == "unknown"
    assert stages["deployment"].unknown_reasons == ("cost_evidence_absent",)
    assert stages["rollback"].status == "unknown"


def test_profile_identity_is_content_bound() -> None:
    profile = profile_from_facts(_facts())
    assert profile.profile_id == "research-profile-" + profile.content_sha256
    assert profile.contents()["lineage_group_id"] is None
    assert type(profile).model_validate_json(profile.model_dump_json()) == profile

    with pytest.raises(ValueError, match="content identity"):
        type(profile).model_validate_json(canonical({**profile.model_dump(mode="json"), "run_id": "run-other"}))


def test_missing_cost_does_not_become_zero() -> None:
    facts = {"task_id": "task-research-1", "run_id": "run-research-1"}
    profile = profile_from_facts(facts)
    assert profile.total.total_tokens is None
    assert profile.total.known_tokens_lower_bound == 0
    assert all(stage.status == "unknown" for stage in profile.stages)


@pytest.mark.parametrize("missing", [False, True])
def test_solver_totals_preserve_unknown_values(missing: bool) -> None:
    facts = _facts()
    del facts["validation_cost"]
    if missing:
        facts["cost"].update(reserved_calls=None, total_tokens=None, usage_complete=False)
        facts["timing"] = {"repair_ms": None}
    profile = profile_from_facts(facts, expected_stages=())

    assert profile.total.reserved_calls == (None if missing else 2)
    assert profile.total.total_tokens == (None if missing else 30)
    assert profile.total.elapsed_ms == (None if missing else 120.0)
    assert profile.total.known_tokens_lower_bound == 30
