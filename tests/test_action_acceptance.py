"""Synthetic P0 contract tests; no model, application, SDK or device is run."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from dfx_agent.acceptance import (
    AcceptanceProtocol,
    CandidateRuntimeBinding,
    VerificationVerdict,
    admit_protocol,
    admit_runtime_binding,
    admit_verdict,
    evaluate_acceptance,
)
from dfx_agent.contracts import ActionProposal, authorize_action


def sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def protocol() -> AcceptanceProtocol:
    roles = ("build", "target_replay", "behavior", "regression")
    capabilities = ("build", "device_replay", "behavior_test", "regression_test")
    return admit_protocol({"task_id": "task1", "lineage_group_id": "app_family_1",
        "fault_variant_id": "fault_021", "crash_log_sha256": sha("original-crash-log"),
        "target_fault_signature_sha256": sha("target-fault-signature"),
        "original_analysis_sha256": sha("analysis-original"), "original_runtime_sha256": sha("runtime-original"),
        "environment_id": "env1", "initial_state_id": "clean",
        "checks": [{"check_id": role, "role": role, "scenario_id": f"scenario_{role}",
                    "oracle_origin": "independent_test", "oracle_ref_sha256": sha(role),
                    "visibility": "evaluator_only", "required_capability": capability,
                    "timeout_seconds": 30, "required": True, "repetitions": 1}
                   for role, capability in zip(roles, capabilities, strict=True)]}, channel="evaluation_preparer")


def binding() -> CandidateRuntimeBinding:
    parent, candidate, diff = sha("analysis-original"), sha("analysis-candidate"), sha("patch")
    candidate_id = hashlib.sha256((parent + candidate + diff).encode()).hexdigest()
    return admit_runtime_binding({"task_id": "task1", "candidate_id": candidate_id,
        "parent_analysis_sha256": parent, "candidate_analysis_sha256": candidate,
        "diff_sha256": diff, "runtime_source_sha256": sha("runtime-candidate"),
        "shared_sources_manifest_sha256": sha("shared-sources"),
        "packaging_manifest_sha256": sha("package")}, channel="trusted_packager")


def verdicts(p: AcceptanceProtocol, b: CandidateRuntimeBinding) -> list[VerificationVerdict]:
    results = []
    for check in p.checks:
        for run_role in ("original", "candidate"):
            target_fault = check.role == "target_replay" and run_role == "original"
            payload = {"task_id": p.task_id, "check_id": check.check_id,
                       "protocol_sha256": p.sha256(), "run_role": run_role,
                       "candidate_id": b.candidate_id if run_role == "candidate" else None,
                       "runtime_source_sha256": b.runtime_source_sha256 if run_role == "candidate"
                                                else p.original_runtime_sha256,
                       "environment_id": p.environment_id, "initial_state_id": p.initial_state_id,
                       "repetition": 0, "status": "fail" if target_fault else "pass",
                       "failure_kind": "target_fault" if target_fault else None,
                       "observed_fault_signature_sha256": p.target_fault_signature_sha256 if target_fault else None,
                       "scenario_reached": True if check.role != "build" else None,
                       "execution_id": f"exec_{check.check_id}_{run_role}",
                       "artifact_sha256": sha(f"artifact_{check.check_id}_{run_role}"),
                       "duration_ms": 10, "scope": "application"}
            results.append(admit_verdict(payload, channel="execution_backend"))
    return results


def test_complete_protocol_is_candidate_neutral_and_requires_original_failure() -> None:
    p, b = protocol(), binding()
    items = verdicts(p, b)
    decision = evaluate_acceptance(p, b, items)
    assert decision.status == "verified_under_protocol" and not decision.reasons
    assert p.sha256() == protocol().sha256()
    missing_original = [item for item in items if not (item.check_id == "target_replay" and
                                                        item.run_role == "original")]
    assert "missing_verdict:target_replay:original:0" in evaluate_acceptance(p, b, missing_original).reasons


@pytest.mark.parametrize(("change", "reason"), [
    ("synthetic", "non_application_evidence"),
    ("wrong_runtime", "identity_mismatch"),
    ("wrong_environment", "identity_mismatch"),
    ("wrong_protocol", "identity_mismatch"),
    ("target_not_reached", "original_fault_not_reproduced"),
    ("wrong_fault", "original_fault_not_reproduced"),
    ("wrong_signature", "original_fault_not_reproduced"),
    ("behavior_failed", "required_check_not_passed"),
])
def test_gate_rejects_unproven_or_mismatched_success(change: str, reason: str) -> None:
    p, b = protocol(), binding()
    items = verdicts(p, b)
    index = next(i for i, item in enumerate(items) if item.check_id ==
                 ("behavior" if change == "behavior_failed" else "target_replay") and
                 item.run_role == ("candidate" if change in ("behavior_failed", "synthetic", "wrong_runtime",
                                                                "wrong_environment", "wrong_protocol") else "original"))
    updates: dict[str, object] = {
        "synthetic": {"scope": "synthetic"},
        "wrong_runtime": {"runtime_source_sha256": sha("old-candidate")},
        "wrong_environment": {"environment_id": "other_env"},
        "wrong_protocol": {"protocol_sha256": sha("another-protocol")},
        "target_not_reached": {"scenario_reached": False},
        "wrong_fault": {"failure_kind": "environment"},
        "wrong_signature": {"observed_fault_signature_sha256": sha("different-fault")},
        "behavior_failed": {"status": "fail", "failure_kind": "behavior_mismatch"},
    }[change]  # type: ignore[assignment]
    items[index] = VerificationVerdict.model_validate({**items[index].model_dump(), **updates})
    decision = evaluate_acceptance(p, b, items)
    assert decision.status == "not_verified"
    assert any(item.startswith(reason) for item in decision.reasons)


def test_missing_regression_duplicate_and_wrong_lineage_cannot_pass() -> None:
    p, b = protocol(), binding()
    items = verdicts(p, b)
    assert evaluate_acceptance(p, b, items + [items[0]]).status == "not_verified"
    no_regression = [item for item in items if item.check_id != "regression"]
    assert evaluate_acceptance(p, b, no_regression).status == "not_verified"
    wrong_parent = b.model_copy(update={"parent_analysis_sha256": sha("different-parent")})
    assert "candidate_source_lineage_mismatch" in evaluate_acceptance(p, wrong_parent, items).reasons
    wrong_identity = b.model_copy(update={"candidate_analysis_sha256": sha("different-candidate")})
    assert "candidate_identity_mismatch" in evaluate_acceptance(p, wrong_identity, items).reasons


def test_protocol_rejects_construction_or_model_oracle_and_untrusted_admission() -> None:
    p = protocol()
    bad = p.model_dump()
    bad["checks"][0]["oracle_origin"] = "construction_test"
    with pytest.raises(ValidationError, match="independent oracle"):
        AcceptanceProtocol.model_validate(bad)
    wrong_capability = p.model_dump()
    wrong_capability["checks"][1]["required_capability"] = "build"
    with pytest.raises(ValidationError, match="required capability"):
        AcceptanceProtocol.model_validate(wrong_capability)
    with pytest.raises(ValueError, match="untrusted"):
        admit_protocol(p.model_dump(), channel="model")
    with pytest.raises(ValueError, match="untrusted"):
        admit_runtime_binding(binding().model_dump(), channel="model")
    with pytest.raises(ValueError, match="untrusted"):
        admit_verdict(verdicts(p, binding())[0].model_dump(), channel="model")


def proposal(**updates: object) -> ActionProposal:
    payload = {"task_id": "task1", "run_id": "run1", "record_id": "proposal1",
               "created_at": datetime.now(UTC), "source_snapshot_sha256": sha("analysis-original"),
               "action": "diagnostic_probe", "target_ids": ["direction1", "direction2"],
               "evidence_ids": ["fact1"], "reason": "Consumer semantics distinguish two repairs",
               "required_capability": "diagnostic_execution", "observation_routes": {
                   "word_consumer": "direction1", "byte_consumer": "direction2"},
               "unknown_route": "retain_unknown", "estimated_cost_basis": "one bounded local probe"}
    return ActionProposal.model_validate({**payload, **updates})


def authorize(item: ActionProposal, **updates: object):
    context = {"task_id": "task1", "run_id": "run1", "source_snapshot_sha256": sha("analysis-original"),
               "known_evidence_ids": {"fact1"}, "known_target_ids": {"direction1", "direction2"},
               "registered_candidate_ids": set(), "accepted_candidate_ids": set(),
               "capabilities": {"diagnostic_execution"},
               "remaining_model_calls": 3, "remaining_probe_budget": 1}
    return authorize_action(item, **{**context, **updates})  # type: ignore[arg-type]


def test_action_proposal_requires_distinguishing_routes_and_host_permission() -> None:
    assert authorize(proposal()).allowed
    assert authorize(proposal(), capabilities=set()).reason == "capability_unavailable"
    assert authorize(proposal(), remaining_probe_budget=0).reason == "budget_exhausted"
    assert authorize(proposal(), remaining_model_calls=0).reason == "budget_exhausted"
    assert authorize(proposal(), remaining_model_calls=0, model_response_reserved=True).allowed
    assert authorize(proposal(), source_snapshot_sha256=sha("old-source")).reason == "wrong_task_or_version"
    assert authorize(proposal(), run_id="other_run").reason == "wrong_task_or_version"
    assert authorize(proposal(), known_evidence_ids=set()).reason == "unknown_reference"
    with pytest.raises(ValidationError, match="distinguishing"):
        proposal(observation_routes={"a": "direction1", "b": "direction1"})
    with pytest.raises(ValidationError, match="distinguishing"):
        proposal(unknown_route=None)


def test_finish_proposal_cannot_override_acceptance_gate() -> None:
    candidate_id = binding().candidate_id
    finish = proposal(action="finish", candidate_id=candidate_id,
                      required_capability="finish", target_ids=[], observation_routes={},
                      unknown_route=None)
    assert authorize(finish, registered_candidate_ids={candidate_id},
                     capabilities={"finish"}).reason == "acceptance_not_verified"
    assert authorize(finish, registered_candidate_ids={candidate_id},
                     accepted_candidate_ids={candidate_id}, capabilities={"finish"},
                     remaining_model_calls=0).allowed
