"""Pure ResearchProfile contract checks; no model or application execution."""

from __future__ import annotations

import pytest

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

    with pytest.raises(ValueError, match="content identity"):
        type(profile).model_validate({**profile.model_dump(mode="json"), "run_id": "run-other"})


def test_missing_cost_does_not_become_zero() -> None:
    facts = {"task_id": "task-research-1", "run_id": "run-research-1"}
    profile = profile_from_facts(facts)
    assert profile.total.total_tokens is None
    assert profile.total.known_tokens_lower_bound == 0
    assert all(stage.status == "unknown" for stage in profile.stages)
