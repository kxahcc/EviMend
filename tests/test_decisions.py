"""Repair advice must use registered diagnosis and candidate-bound observations."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from test_agent_loop import response, task_snapshot

from dfx_agent.agent import _tools, run_fixed_agent
from dfx_agent.decisions import DecisionRequest, DecisionStore
from dfx_agent.policies import PolicyFeatures
from dfx_agent.task import digest, verify_task
from dfx_agent.verification import CheckLedger, FixedTextExecutor
from dfx_agent.workspace import CandidateWorkspace

FULL = PolicyFeatures(obligation_tracking=True, decision_checks=True)


def diagnosis_submission(task: Path) -> dict:
    snapshot = verify_task(task)["source_snapshot_sha256"]
    identity = {"task_id": "task1", "run_id": "run1", "created_at": datetime.now(UTC).isoformat(),
                "source_snapshot_sha256": snapshot}
    location = {"snapshot_sha256": snapshot, "path": "src/main.ets", "start_line": 1, "end_line": 1}
    return {"diagnosis": {**identity, "record_id": "diagnosis1", "exception_locations": [location],
        "hypotheses": [{"hypothesis_id": "h1", "support_evidence_ids": ["infer1"], "support_status": "unknown",
            "obligation_ids": ["keep_value"], "distinguishing_check": {"question": "Which behavior is required?",
                "observation_routes": {"value": "h1", "state": "h2"}}},
            {"hypothesis_id": "h2", "support_evidence_ids": ["infer2"], "support_status": "unknown",
             "obligation_ids": ["keep_state"]}], "selected_hypothesis_id": "h1"},
        "source_citations": [{"evidence_id": "source1", "location": location}],
        "inferences": [{"evidence_id": "infer1", "statement": "The value may be wrong", "basis_ids": ["source1"]},
                       {"evidence_id": "infer2", "statement": "The state may be wrong", "basis_ids": ["source1"]}],
        "obligations": [{**identity, "record_id": "keep_value", "kind": "business_behavior",
            "statement": "Preserve the expected value", "authority": "model_inference", "source_evidence_ids": ["infer1"],
            "hypothesis_ids": ["h1"]}, {**identity, "record_id": "keep_state", "kind": "business_behavior",
            "statement": "Preserve the expected state", "authority": "model_inference", "source_evidence_ids": ["infer2"],
            "hypothesis_ids": ["h2"]}]}


def decision_request(task: Path, *, check: bool = False) -> dict:
    plan = {"source_snapshot_sha256": verify_task(task)["source_snapshot_sha256"],
        "directions": [{"direction_id": "repair1", "repair_key": "change_value", "acceptance_key": "keep_output",
            "hypothesis_ids": ["h1"], "support_evidence_ids": ["infer1"], "obligation_ids": ["keep_value"]},
            {"direction_id": "repair2", "repair_key": "change_state", "acceptance_key": "keep_output",
            "hypothesis_ids": ["h2"], "support_evidence_ids": ["infer2"], "obligation_ids": ["keep_state"]}],
        "selected_direction_id": "repair1", "remaining_probe_budget": 999,
        "available_capabilities": ["diagnostic_execution", "read_source"]}
    if check:
        plan["check"] = {"check_id": "which_behavior", "kind": "existing_evidence", "capability": "read_source",
                         "observation_routes": {"value": "repair1", "state": "repair2"}}
    return {"task_id": "task1", "run_id": "run1", "diagnosis_id": "diagnosis1", "plan": plan}


def registered_context(task: Path, root: Path):
    workspace = CandidateWorkspace(task, root, "run1")
    executor = FixedTextExecutor("src/main.ets", "value=good\n")
    ledger = CheckLedger(workspace, executor)
    tools = {item.name: item for item in _tools(workspace, ledger, 3, task, "crash.log",
             verify_task(task)["crash_sha256"], FULL)}
    answer = json.loads(tools["record_diagnosis"].invoke({"diagnosis_json": json.dumps(diagnosis_submission(task))}))
    assert answer["diagnosis_id"] == "diagnosis1"
    return workspace, ledger, tools


def decide(store: DecisionStore, ledger: CheckLedger, raw: dict, features: PolicyFeatures = FULL,
           capabilities: frozenset[str] = frozenset({"read_source", "read_log"})):
    return store.decide(DecisionRequest.model_validate_json(json.dumps(raw)), ledger=ledger,
                        features=features, capabilities=capabilities)


def test_sdk_decision_candidate_failure_and_reconsideration_path(tmp_path: Path) -> None:
    task = task_snapshot(tmp_path)
    derive, ledger, tools = registered_context(task, tmp_path / "derive")
    raw = decision_request(task)
    initial = json.loads(tools["plan_repair_action"].invoke({"plan_json": json.dumps(raw)}))
    assert initial["next_action"] == "attempt_with_unknown"
    tools["edit_source"].invoke({"path": "src/main.ets", "old": "bad", "new": "almost"})
    candidate = json.loads(tools["register_candidate"].invoke({"diagnosis_id": "diagnosis1",
        "decision_id": initial["record_id"], "direction_id": "repair1", "preserves": ["keep_value"]}))["candidate_id"]
    tools["run_check"].invoke({"candidate_id": candidate})
    feedback = json.loads(tools["link_failed_check"].invoke({"candidate_id": candidate,
                                                          "violated_obligation_ids": ["keep_value"]}))
    revised = {**raw, "candidate_id": candidate, "feedback_id": feedback["feedback_id"]}
    after = json.loads(tools["plan_repair_action"].invoke({"plan_json": json.dumps(revised)}))
    assert after["next_action"] == "reconsider_repair" and after["feedback"]["affected_direction_ids"] == ["repair1"]
    assert after["hard_rejected_obligation_ids"] == [] and after["advisory_obligation_ids"] == ["keep_value"]
    base_advice = decide(DecisionStore(derive), ledger, revised, features=PolicyFeatures())
    assert base_advice.next_action == "attempt_with_unknown" and base_advice.advisory_obligation_ids == []
    assert base_advice.feedback is not None and base_advice.feedback.action == "retain_uncertainty"
    # Fixed scripts derive IDs from the same immutable facts and failed check, as for candidate hashes.
    report = run_fixed_agent(task, tmp_path / "run", "run1", [
        response(1, "inspect_analysis", {"include_schema": True}),
        response(2, "record_diagnosis", {"diagnosis_json": json.dumps(diagnosis_submission(task))}),
        response(3, "plan_repair_action", {"plan_json": json.dumps(raw)}),
        response(4, "edit_source", {"path": "src/main.ets", "old": "bad", "new": "almost"}),
        response(5, "register_candidate", {"diagnosis_id": "diagnosis1", "decision_id": initial["record_id"],
                                           "direction_id": "repair1", "preserves": ["keep_value"]}),
        response(6, "run_check", {"candidate_id": candidate}),
        response(7, "link_failed_check", {"candidate_id": candidate, "violated_obligation_ids": ["keep_value"]}),
        response(8, "plan_repair_action", {"plan_json": json.dumps(revised)}),
        response(9, "inspect_analysis", {"decision_id": after["record_id"]}), response(10),
    ], FixedTextExecutor("src/main.ets", "value=good\n"), policy_features=FULL)
    assert report["rejected_tool_calls"] == report["uncertain_tool_calls"] == 0
    assert [item["record_id"] for item in report["decisions"]] == sorted([initial["record_id"], after["record_id"]])
    saved = json.loads((tmp_path / f"run/candidates/{candidate}/candidate.json").read_text())
    assert saved["decision_id"] == initial["record_id"] and saved["direction_id"] == "repair1"
    assert report["checks"][0]["status"] == "fail" and report["application_verification"] == "not_run"
    assert len(DecisionStore(derive).records()) == 3 and len(ledger.executor.calls) == 1


@pytest.mark.parametrize("fault", ["task", "run", "snapshot", "diagnosis", "hypothesis", "evidence",
                                  "obligation", "omit_obligation", "question_routes", "capability_kind"])
def test_invalid_decision_references_produce_no_record_or_execution(tmp_path: Path, fault: str) -> None:
    task = task_snapshot(tmp_path)
    workspace, ledger, _ = registered_context(task, tmp_path / "run")
    raw = decision_request(task, check=True)
    direction = raw["plan"]["directions"][0]
    if fault in {"task", "run"}:
        raw[f"{fault}_id"] = "other"
    elif fault == "snapshot":
        raw["plan"]["source_snapshot_sha256"] = digest(b"another version")
    elif fault == "diagnosis":
        raw["diagnosis_id"] = "absent"
    elif fault == "hypothesis":
        direction["hypothesis_ids"] = ["absent"]
    elif fault == "evidence":
        direction["support_evidence_ids"] = ["absent"]
    elif fault == "obligation":
        direction["obligation_ids"] = ["absent"]
    elif fault == "omit_obligation":
        direction["obligation_ids"] = []
    elif fault == "question_routes":
        raw["plan"]["check"]["observation_routes"] = {"invented": "repair1", "state": "repair2"}
    elif fault == "capability_kind":
        raw["plan"]["check"]["capability"] = "diagnostic_execution"
    store = DecisionStore(workspace)
    with pytest.raises(ValueError):
        decide(store, ledger, raw)
    assert store.records() == {} and ledger.executor.requests == [] and not workspace.draft.exists()


def test_equivalent_repairs_and_host_capabilities_control_check_advice(tmp_path: Path) -> None:
    task = task_snapshot(tmp_path)
    workspace, ledger, _ = registered_context(task, tmp_path / "run")
    store = DecisionStore(workspace)
    raw = decision_request(task, check=True)
    assert decide(store, ledger, raw).next_action == "read_existing"
    assert decide(store, ledger, raw, capabilities=frozenset({"read_log"})).next_action == "attempt_with_unknown"
    assert decide(store, ledger, raw, features=PolicyFeatures()).next_action == "attempt_with_unknown"
    raw["plan"]["directions"][1]["repair_key"] = "change_value"
    assert decide(store, ledger, raw).next_action == "direct_candidate"
    raw["plan"]["directions"][1]["acceptance_key"] = "another_behavior"
    assert decide(store, ledger, raw).next_action == "read_existing"
    raw["plan"]["check"].update(kind="execution_probe", capability="diagnostic_execution")
    advice = decide(store, ledger, raw, capabilities=frozenset({"diagnostic_execution", "read_source"}))
    assert advice.next_action == "attempt_with_unknown" and advice.request.plan.remaining_probe_budget == 0
    assert "diagnostic_execution" not in advice.request.plan.available_capabilities and ledger.executor.requests == []
    raw["plan"]["selected_direction_id"] = None
    assert decide(store, ledger, raw).next_action == "unresolved"
    raw = decision_request(task, check=True)
    raw["plan"]["directions"][0].update(hypothesis_ids=["h1", "h2"], obligation_ids=["keep_value", "keep_state"])
    raw["plan"]["check"]["observation_routes"]["state"] = "repair1"
    assert decide(store, ledger, raw).next_action == "attempt_with_unknown"


@pytest.mark.parametrize("fault", ["missing_result", "candidate", "snapshot", "execution", "observation", "authority", "scope"])
def test_feedback_must_match_saved_failed_candidate_and_scope(tmp_path: Path, fault: str) -> None:
    task = task_snapshot(tmp_path)
    workspace, ledger, tools = registered_context(task, tmp_path / "run")
    tools["edit_source"].invoke({"path": "src/main.ets", "old": "bad", "new": "almost"})
    candidate = json.loads(tools["register_candidate"].invoke({"diagnosis_id": "diagnosis1",
                                                              "preserves": ["keep_value"]}))["candidate_id"]
    tools["run_check"].invoke({"candidate_id": candidate})
    feedback = json.loads(tools["link_failed_check"].invoke({"candidate_id": candidate,
                                                          "violated_obligation_ids": ["keep_value"]}))
    feedback_id = feedback.pop("feedback_id")
    raw = {**decision_request(task), "candidate_id": candidate, "feedback_id": feedback_id}
    if fault == "missing_result":
        next((workspace.root / "checks").glob("*.json")).unlink()
    elif fault == "candidate":
        tools["edit_source"].invoke({"path": "src/main.ets", "old": "bad", "new": "good"})
        raw["candidate_id"] = json.loads(tools["register_candidate"].invoke({"diagnosis_id": "diagnosis1"}))["candidate_id"]
    else:
        if fault == "snapshot":
            feedback["candidate_snapshot_sha256"] = digest(b"new version")
        elif fault == "execution":
            feedback["execution_id"] = "invented"
        elif fault == "observation":
            feedback["observation"] = "Model claimed a different result"
        elif fault == "authority":
            feedback["link_authority"] = "verified_check"
        elif fault == "scope":
            feedback.update(scope="application", evidence_kind="application_execution")
        (workspace.root / f"feedback/{feedback_id}.json").write_text(json.dumps(feedback), encoding="utf-8")
    with pytest.raises(ValueError):
        decide(DecisionStore(workspace), ledger, raw)
    assert DecisionStore(workspace).records() == {}


def test_candidate_cannot_misattribute_a_decision_and_cache_does_not_change_result(tmp_path: Path) -> None:
    task = task_snapshot(tmp_path)
    workspace, ledger, tools = registered_context(task, tmp_path / "run")
    store = DecisionStore(workspace)
    raw = decision_request(task)
    first = decide(store, ledger, raw)
    assert decide(store, ledger, raw) == first and len(store.records()) == 1
    reopened = DecisionStore(CandidateWorkspace.reopen(task, workspace.root, "run1"))
    assert reopened.records()[first.record_id] == first
    tools["edit_source"].invoke({"path": "src/main.ets", "old": "bad", "new": "almost"})
    for updates in [{"direction_id": "absent"}, {"direction_id": None}, {"preserves": ["keep_state"]}, {"diagnosis_id": "other"}]:
        answer = tools["register_candidate"].invoke({"diagnosis_id": "diagnosis1", "decision_id": first.record_id,
            "direction_id": "repair1", "preserves": ["keep_value"], **updates})
        assert answer.startswith("rejected:") and list(workspace.candidates.iterdir()) == []
    # Advice stays advisory: a different proposed direction can still be registered.
    answer = json.loads(tools["register_candidate"].invoke({"diagnosis_id": "diagnosis1", "decision_id": first.record_id,
        "direction_id": "repair2", "preserves": ["keep_state"]}))
    assert answer["ordinal"] == 1


def test_sdk_invalid_request_is_rejected_before_any_decision_file(tmp_path: Path) -> None:
    task = task_snapshot(tmp_path)
    raw = decision_request(task)
    raw["run_id"] = "other"
    report = run_fixed_agent(task, tmp_path / "run", "run1", [
        response(1, "record_diagnosis", {"diagnosis_json": json.dumps(diagnosis_submission(task))}),
        response(2, "plan_repair_action", {"plan_json": json.dumps(raw)}), response(3),
    ], FixedTextExecutor("src/main.ets", "value=good\n"), policy_features=FULL)
    assert report["decisions"] == report["candidates"] == report["checks"] == []
    assert report["application_verification"] == "not_run"
