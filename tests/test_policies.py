"""Synthetic branch checks for optional rule and decision guidance."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from test_agent_loop import response, task_snapshot

from dfx_agent.agent import _tools, run_fixed_agent
from dfx_agent.cli import main as cli_main
from dfx_agent.policies import (
    Counterexample,
    DistinguishingCheck,
    ObligationClaim,
    PolicyFeatures,
    PolicyInput,
    RepairDirection,
    candidate_conflicts,
    choose_action,
    reconsider_after_feedback,
    relevant_counterexample,
    rule_guidance,
)
from dfx_agent.task import digest
from dfx_agent.verification import CheckLedger, FixedTextExecutor
from dfx_agent.workspace import CandidateWorkspace

SOURCE = digest(b"source")
FIRST = RepairDirection(direction_id="widen", repair_key="resize_buffer", acceptance_key="keep_encoding",
                        obligation_ids=["valid_capacity", "keep_data"])
SECOND = RepairDirection(direction_id="reinterpret", repair_key="change_view", acceptance_key="keep_encoding",
                         obligation_ids=["valid_capacity", "keep_byte_semantics"])
SAME_REPAIR = RepairDirection(direction_id="widen_other_cause", repair_key="resize_buffer",
                              acceptance_key="keep_encoding")
CHECK = DistinguishingCheck(check_id="consumer_semantics", kind="existing_evidence",
                            capability="read_source", observation_routes={"word_consumer": "widen",
                                                                             "byte_consumer": "reinterpret"})
PROBE = DistinguishingCheck(check_id="decode_probe", kind="execution_probe",
                            capability="diagnostic_execution", observation_routes={"words": "widen",
                                                                                      "bytes": "reinterpret"})
FULL = PolicyFeatures(rule_guidance=True, obligation_tracking=True, decision_checks=True)


def plan(directions: list[RepairDirection], check: DistinguishingCheck | None = None,
         selected: str | None = "widen", capabilities: set[str] | None = None,
         probe_budget: int = 1) -> PolicyInput:
    return PolicyInput(source_snapshot_sha256=SOURCE, directions=directions,
                       selected_direction_id=selected, check=check,
                       available_capabilities=capabilities or set(),
                       remaining_probe_budget=probe_budget)


def test_same_repair_never_forces_a_probe() -> None:
    same_repair_probe = DistinguishingCheck(check_id="same_repair_probe", kind="execution_probe",
                                           capability="diagnostic_execution",
                                           observation_routes={"cause_a": "widen",
                                                               "cause_b": "widen_other_cause"})
    decision = choose_action(plan([FIRST, SAME_REPAIR], same_repair_probe,
                                  capabilities={"diagnostic_execution"}), FULL)
    assert decision.action == "direct_candidate"
    assert decision.check_id is None


def test_differing_repair_reads_existing_evidence_first() -> None:
    decision = choose_action(plan([FIRST, SECOND], CHECK, capabilities={"read_source"}), FULL)
    assert decision.action == "read_existing" and decision.check_id == "consumer_semantics"
    assert decision.selected_direction_id is None


def test_same_edit_with_different_acceptance_still_needs_distinguishing_evidence() -> None:
    alternate = RepairDirection(direction_id="other_acceptance", repair_key="resize_buffer",
                                acceptance_key="truncate_tail")
    check = DistinguishingCheck(check_id="tail_contract", kind="existing_evidence",
                                capability="read_source", observation_routes={"keep": "widen",
                                                                                  "drop": "other_acceptance"})
    decision = choose_action(plan([FIRST, alternate], check, capabilities={"read_source"}), FULL)
    assert decision.action == "read_existing"


def test_uninformative_or_unavailable_check_does_not_execute() -> None:
    no_difference = DistinguishingCheck(check_id="same", kind="execution_probe",
                                        capability="diagnostic_execution",
                                        observation_routes={"a": "widen", "b": "widen"})
    assert choose_action(plan([FIRST, SECOND], no_difference,
                              capabilities={"diagnostic_execution"}), FULL).action == "attempt_with_unknown"
    assert choose_action(plan([FIRST, SECOND], PROBE, capabilities={"diagnostic_execution"},
                              probe_budget=0), FULL).action == "attempt_with_unknown"
    assert choose_action(plan([FIRST, SECOND], PROBE,
                              capabilities={"diagnostic_execution"}), PolicyFeatures()).action == "attempt_with_unknown"
    assert choose_action(plan([FIRST, SECOND], PROBE,
                              capabilities={"diagnostic_execution"}), FULL).action == "run_probe"


def test_model_inference_does_not_hard_reject_and_business_counterexample_is_bound() -> None:
    inferred = ObligationClaim(obligation_id="keep_data", source_snapshot_sha256=SOURCE,
                               authority="model_inference", evidence_ids=["model_note"],
                               statement="Preserve output data")
    verified = ObligationClaim(obligation_id="verified_output", source_snapshot_sha256=SOURCE,
                               authority="verified_requirement", evidence_ids=["requirement_1"],
                               statement="Output must contain every element")
    prior_id = digest(b"candidate-1")
    prior_source = digest(b"candidate-source-1")
    feedback = Counterexample(candidate_id=prior_id, candidate_snapshot_sha256=prior_source,
                              execution_id="check-1", evidence_kind="local_execution",
                              scope="synthetic", link_authority="model_inference",
                              violated_obligation_ids=["keep_data", "verified_output"],
                              observation="Synthetic check found an output mismatch")
    hard, advisory = candidate_conflicts([inferred, verified], source_snapshot_sha256=SOURCE,
                                         feedback=feedback)
    assert hard == [] and advisory == ["keep_data", "verified_output"]
    trusted = feedback.model_copy(update={"link_authority": "verified_check"})
    hard, advisory = candidate_conflicts([inferred, verified], source_snapshot_sha256=SOURCE,
                                         feedback=trusted)
    assert hard == ["verified_output"] and advisory == ["keep_data"]
    assert relevant_counterexample(feedback, candidate_id=prior_id, candidate_snapshot_sha256=prior_source)
    assert not relevant_counterexample(feedback, candidate_id=digest(b"candidate-2"),
                                       candidate_snapshot_sha256=digest(b"candidate-source-2"))
    current = reconsider_after_feedback(plan([FIRST, SECOND]), feedback, candidate_id=prior_id,
                                        candidate_snapshot_sha256=prior_source, features=FULL)
    assert current.action == "reconsider_repair" and current.affected_direction_ids == ["widen"]
    stale = reconsider_after_feedback(plan([FIRST, SECOND]), feedback, candidate_id=digest(b"candidate-2"),
                                      candidate_snapshot_sha256=digest(b"candidate-source-2"), features=FULL)
    assert stale.action == "ignore_stale"


def test_unknown_family_and_invalid_route_preserve_generic_path() -> None:
    assert rule_guidance("unknown", FULL) is None
    assert rule_guidance("bounds", PolicyFeatures()) is None
    assert rule_guidance("bounds", FULL).family == "bounds"  # type: ignore[union-attr]
    with pytest.raises(ValueError, match="absent direction"):
        plan([FIRST, SECOND], DistinguishingCheck(check_id="wrong", kind="execution_probe",
             capability="diagnostic_execution", observation_routes={"a": "widen", "b": "missing"}))


def test_agent_keeps_common_tools_and_resume_freezes_policy_features(tmp_path: Path) -> None:
    task = task_snapshot(tmp_path)
    executor = FixedTextExecutor("src/main.ets", "value=good\n")
    base = run_fixed_agent(task, tmp_path / "base", "base", [response(1)], executor)
    enhanced = run_fixed_agent(task, tmp_path / "enhanced", "enhanced", [response(1)], executor,
                               policy_features=FULL)
    assert base["offered_tools"] == enhanced["offered_tools"]
    assert {"read_source", "edit_source", "run_check", "consult_rule", "plan_repair_action"} <= set(base["offered_tools"])
    with pytest.raises(ValueError, match="session identity"):
        run_fixed_agent(task, tmp_path / "enhanced", "enhanced", [response(1)], executor,
                        resume=True, policy_features=PolicyFeatures())


def test_model_cannot_grant_itself_probe_capability(tmp_path: Path) -> None:
    task = task_snapshot(tmp_path)
    workspace = CandidateWorkspace(task, tmp_path / "policy-run", "run1")
    ledger = CheckLedger(workspace, FixedTextExecutor("src/main.ets", "value=good\n"))
    tools = {item.name: item for item in _tools(workspace, ledger, 3, task,
             "crash.log", digest(b"synthetic crash"), FULL)}
    requested = plan([FIRST, SECOND], PROBE, capabilities={"diagnostic_execution"})
    requested.source_snapshot_sha256 = workspace.source_snapshot_sha256
    answer = tools["plan_repair_action"].invoke({"plan_json": requested.model_dump_json()})
    assert '"action":"attempt_with_unknown"' in answer


def test_agent_links_inferred_obligation_to_candidate_and_failed_check(tmp_path: Path) -> None:
    task = task_snapshot(tmp_path)
    workspace = CandidateWorkspace(task, tmp_path / "linked", "run1")
    ledger = CheckLedger(workspace, FixedTextExecutor("src/main.ets", "value=good\n"))
    tools = {item.name: item for item in _tools(workspace, ledger, 3, task,
             "crash.log", digest(b"synthetic crash"), FULL)}
    diagnosis = json.loads(tools["record_diagnosis"].invoke({"path": "src/main.ets", "line": 1,
                                                                 "reason": "bad value"}))
    assert diagnosis["support_status"] == "unknown"
    claim = json.loads(tools["record_obligation"].invoke({"obligation_id": "preserve_value",
        "statement": "Keep the expected value", "evidence_ids": ["inference-1"]}))
    assert claim["authority"] == "model_inference"
    tools["edit_source"].invoke({"path": "src/main.ets", "old": "bad", "new": "almost"})
    candidate = json.loads(tools["register_candidate"].invoke({"diagnosis_id": diagnosis["diagnosis_id"],
        "restores": ["preserve_value"], "preserves": []}))
    candidate_id = candidate["candidate_id"]
    assert json.loads((workspace.candidates / candidate_id / "candidate.json").read_text())["restores"] == ["preserve_value"]
    assert json.loads(tools["run_check"].invoke({"candidate_id": candidate_id}))["status"] == "fail"
    feedback = json.loads(tools["link_failed_check"].invoke({"candidate_id": candidate_id,
        "violated_obligation_ids": ["preserve_value"]}))
    assert feedback["scope"] == "synthetic" and feedback["link_authority"] == "model_inference"
    assert feedback["candidate_id"] == candidate_id


def test_managed_cli_script_can_select_policy_features(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                       capsys: pytest.CaptureFixture[str]) -> None:
    task = task_snapshot(tmp_path)
    script = tmp_path / "fixed.json"
    script.write_text(json.dumps({"schema_version": 1, "responses": [{"tool": None, "args": {},
        "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}}],
        "expected": {"path": "src/main.ets", "text": "value=good\n"},
        "policy_features": FULL.model_dump()}), encoding="utf-8")
    monkeypatch.setenv("DFX_MANAGED_AGENT", "offline-v1")
    monkeypatch.setattr(sys, "argv", ["dfx", "repair", "--task", str(task), "--work",
                                  str(tmp_path / "cli-work"), "--run-id", "cli-run", "--fixed-script", str(script)])
    cli_main()
    assert json.loads(capsys.readouterr().out)["policy_features"] == FULL.model_dump()
