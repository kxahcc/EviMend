"""Scoped condition records reach the SDK without becoming execution evidence."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from test_agent_loop import response, task_snapshot
from test_conditions import observations
from test_decisions import FULL, decision_request, diagnosis_submission

from dfx_agent.actions import OFFLINE_CAPABILITIES, ToolActionStore
from dfx_agent.agent import _tools, run_fixed_agent
from dfx_agent.conditions import ConditionRequest, ConditionStore, SyntheticObservations
from dfx_agent.decisions import DecisionRequest, DecisionStore
from dfx_agent.diagnostics import AnalysisStore
from dfx_agent.policies import PolicyFeatures
from dfx_agent.task import verify_task
from dfx_agent.verification import CheckLedger, FixedTextExecutor
from dfx_agent.workspace import CandidateWorkspace, TextEdit


def inputs(task: Path, *, status: str = "violated", obligations: bool = True):
    submission = diagnosis_submission(task)
    if not obligations:
        submission["obligations"] = []
        for hypothesis in submission["diagnosis"]["hypotheses"]:
            hypothesis["obligation_ids"] = []
    location = submission["diagnosis"]["exception_locations"][0]
    bindings = {role: role for role in ("capacity", "offset", "width", "count")}
    submission["diagnosis"]["hypotheses"][0]["constraint"] = {
        "family": "bounds", "operation": location, "condition": "offset + count * width <= capacity",
        "semantic_source_evidence_ids": ["source1"],
        "bindings": [{"binding_id": role, "expression": role, "role": "value", "evidence_ids": ["source1"]}
                     for role in bindings]}
    request = ConditionRequest.model_validate_json(json.dumps({"check_id": "view1", "diagnosis_id": "diagnosis1",
        "hypothesis_id": "h1", "kind": "typed_array_view", "binding_ids": bindings,
        "scope": {"task_id": "task1", "run_id": "run1", "operation": location,
                  "source_snapshot_sha256": location["snapshot_sha256"],
                  "evaluated_snapshot_sha256": location["snapshot_sha256"],
                  "scenario_id": "open", "path_id": "main", "subject_id": "buffer1", "sample_id": "use1"}}))
    values = {"capacity": 3 if status == "violated" else 6, "offset": 0, "width": 2, "count": 3}
    if status == "unknown":
        values.pop("count")
    registry = SyntheticObservations([observations(request, values)])
    return submission, request, registry


def context(task: Path, root: Path, *, status: str = "violated", features: PolicyFeatures = FULL):
    submission, request, registry = inputs(task, status=status, obligations=features.obligation_tracking)
    workspace = CandidateWorkspace(task, root, "run1")
    ledger = CheckLedger(workspace, FixedTextExecutor("src/main.ets", "value=good\n"))
    store = ConditionStore(workspace, registry)
    tools = {item.name: item for item in _tools(workspace, ledger, 3, task, "crash.log",
        verify_task(task)["crash_sha256"], features, condition_store=store)}
    assert json.loads(tools["record_diagnosis"].invoke({"diagnosis_json": json.dumps(submission)}))["diagnosis_id"] == "diagnosis1"
    record = store.evaluate(request, "fixture1")
    raw = {**decision_request(task), "condition_scope": request.scope.model_dump(mode="json"),
           "condition_result_ids": [record.result_id]}
    if not features.obligation_tracking:
        for direction in raw["plan"]["directions"]:
            direction["obligation_ids"] = []
    return workspace, ledger, store, tools, request, record, raw


@pytest.mark.parametrize("enhanced", [False, True])
def test_sdk_condition_readback_decision_and_candidate_use_shared_tools(tmp_path: Path, enhanced: bool) -> None:
    task = task_snapshot(tmp_path)
    features = FULL if enhanced else PolicyFeatures()
    workspace, ledger, _, tools, request, record, raw = context(task, tmp_path / "derive", features=features)
    decision = json.loads(tools["plan_repair_action"].invoke({"plan_json": json.dumps(raw)}))
    tools["edit_source"].invoke({"path": "src/main.ets", "old": "bad", "new": "good"})
    candidate = json.loads(tools["register_candidate"].invoke({"diagnosis_id": "diagnosis1",
        "decision_id": decision["record_id"], "direction_id": "repair1",
        "preserves": ["keep_value"] if enhanced else []}))["candidate_id"]
    submission, _, registry = inputs(task, obligations=enhanced)
    report = run_fixed_agent(task, tmp_path / "run", "run1", [
        response(1, "inspect_analysis", {"include_schema": True}),
        response(2, "record_diagnosis", {"diagnosis_json": json.dumps(submission)}),
        response(3, "evaluate_condition", {"request_json": request.model_dump_json(), "observation_id": "fixture1"}),
        response(4, "inspect_analysis", {"condition_result_id": record.result_id}),
        response(5, "plan_repair_action", {"plan_json": json.dumps(raw)}),
        response(6, "edit_source", {"path": "src/main.ets", "old": "bad", "new": "good"}),
        response(7, "register_candidate", {"diagnosis_id": "diagnosis1", "decision_id": decision["record_id"],
                                           "direction_id": "repair1", "preserves": ["keep_value"] if enhanced else []}),
        response(8, "run_check", {"candidate_id": candidate}), response(9),
    ], FixedTextExecutor("src/main.ets", "value=good\n"), policy_features=features, observations=registry)
    assert report["schema_version"] == 3 and report["tool_calls"] == 8
    assert report["rejected_tool_calls"] == report["uncertain_tool_calls"] == 0
    assert report["condition_results"] == [record.model_dump(mode="json")]
    assert report["decisions"][0]["condition_review"]["affected_direction_ids"] == (["repair1"] if enhanced else [])
    assert report["decisions"][0]["next_action"] == ("reconsider_repair" if enhanced else "attempt_with_unknown")
    assert report["decisions"][0]["hard_rejected_obligation_ids"] == []
    assert report["checks"][0]["status"] == "pass" and report["application_verification"] == "not_run"
    assert report["status"] == "offline_checked" and len(ledger.executor.calls) == 0
    assert set(report["offered_tools"]) == {tool.name for tool in tools.values()}
    assert AnalysisStore(workspace).bundles()["diagnosis1"]["diagnosis"].hypotheses[0].support_status == "unknown"
    targets = report["actions"][4]["proposal"]["target_ids"]
    assert {record.result_id, "diagnosis1", "h1", "h2"} <= set(targets)


@pytest.mark.parametrize("status", ["violated", "satisfied", "unknown"])
@pytest.mark.parametrize("tracking", [False, True])
def test_condition_association_is_advisory_and_can_be_disabled(tmp_path: Path, status: str, tracking: bool) -> None:
    task = task_snapshot(tmp_path)
    workspace, ledger, _, _, _, record, raw = context(task, tmp_path / "run", status=status)
    result = DecisionStore(workspace).decide(DecisionRequest.model_validate_json(json.dumps(raw)), ledger=ledger,
        features=PolicyFeatures(obligation_tracking=tracking), capabilities=frozenset({"read_source"}))
    assert record.result.status == status
    assert result.condition_review is not None
    reconsider = tracking and status == "violated"
    assert result.condition_review.action == ("reconsider_repair" if reconsider else "retain_uncertainty")
    assert result.condition_review.affected_direction_ids == (["repair1"] if reconsider else [])
    assert result.hard_rejected_obligation_ids == result.advisory_obligation_ids == []
    assert ledger.executor.requests == [] and not workspace.draft.exists()
    assert all(item.status == "unknown" for item in AnalysisStore(workspace).obligations().values())


@pytest.mark.parametrize("fault", ["missing", "scope_missing", "scope_only", "duplicate", "task", "run",
                                  "path", "scenario", "subject", "sample", "operation", "hypothesis"])
def test_condition_citations_cannot_change_scope_or_associated_hypothesis(tmp_path: Path, fault: str) -> None:
    task = task_snapshot(tmp_path)
    workspace, ledger, _, _, _, _, raw = context(task, tmp_path / "run")
    if fault == "missing":
        raw["condition_result_ids"] = ["condition-" + "0" * 64]
    elif fault == "scope_missing":
        raw.pop("condition_scope")
    elif fault == "scope_only":
        raw["condition_result_ids"] = []
    elif fault == "duplicate":
        raw["condition_result_ids"] *= 2
    elif fault in {"task", "run", "path", "scenario", "subject", "sample"}:
        raw["condition_scope"][fault + "_id"] = "another"
    elif fault == "operation":
        raw["condition_scope"]["operation"]["end_line"] = 2
    else:
        raw["plan"]["directions"] = raw["plan"]["directions"][1:]
        raw["plan"]["selected_direction_id"] = "repair2"
    with pytest.raises(ValueError):
        DecisionStore(workspace).decide(DecisionRequest.model_validate_json(json.dumps(raw)), ledger=ledger,
                                       features=FULL, capabilities=frozenset({"read_source"}))
    assert DecisionStore(workspace).records() == {} and ledger.executor.requests == []


def test_candidate_scope_is_explicit_and_original_results_cannot_be_feedback_for_it(tmp_path: Path) -> None:
    task = task_snapshot(tmp_path)
    workspace, ledger, store, _, request, original, raw = context(task, tmp_path / "run")
    workspace.start_draft()
    workspace.apply_text_edits([TextEdit(path="src/main.ets", old="bad", new="good")])
    candidate = workspace.register_candidate(1, "diagnosis1")
    candidate_raw = request.model_dump(mode="json")
    candidate_raw["scope"].update(candidate_id=candidate.candidate_id,
                                  evaluated_snapshot_sha256=candidate.candidate_snapshot_sha256)
    candidate_raw["scope"]["operation"]["snapshot_sha256"] = candidate.candidate_snapshot_sha256
    candidate_request = ConditionRequest.model_validate_json(json.dumps(candidate_raw))
    # Scope declaration cannot transport an original result into a candidate even though line/path remain equal.
    raw["condition_scope"] = candidate_request.scope.model_dump(mode="json")
    with pytest.raises(ValueError, match="another scope"):
        DecisionStore(workspace).decide(DecisionRequest.model_validate_json(json.dumps(raw)), ledger=ledger,
                                       features=FULL, capabilities=frozenset({"read_source"}))
    # Missing candidate observations produce unknown, not reuse of the original fixed values.
    unknown = store.evaluate(candidate_request, "fixture1")
    assert unknown.result.status == "unknown" and unknown.result.unknown_reasons == ["observation_scope_mismatch"]
    assert original.result.status == "violated"


@pytest.mark.parametrize("fault", ["capability", "budget", "unknown_reference", "model_values"])
def test_sdk_condition_tool_uses_host_authorization_and_shared_budget(tmp_path: Path, fault: str) -> None:
    task = task_snapshot(tmp_path)
    submission, request, registry = inputs(task)
    args = {"request_json": request.model_dump_json(), "observation_id": "fixture1"}
    options = {}
    expected = "invalid_proposal_or_arguments" if fault == "model_values" else "unknown_reference"
    if fault == "capability":
        options["capabilities"] = OFFLINE_CAPABILITIES - {"record_analysis"}
        expected = "unknown_reference"  # diagnosis itself was denied, so the later target is absent too
    elif fault == "budget":
        options["max_tool_calls"] = 1
        expected = "budget_exhausted"
    elif fault == "unknown_reference":
        changed = request.model_dump(mode="json")
        changed["hypothesis_id"] = "invented"
        args["request_json"] = json.dumps(changed)
    else:
        args["values"] = {"capacity": 999}
    report = run_fixed_agent(task, tmp_path / "run", "run1", [
        response(1, "record_diagnosis", {"diagnosis_json": json.dumps(submission)}),
        response(2, "evaluate_condition", args), response(3),
    ], FixedTextExecutor("src/main.ets", "value=good\n"), policy_features=FULL, observations=registry, **options)
    assert report["actions"][1]["rejection"] == expected
    assert report["condition_results"] == report["checks"] == report["candidates"] == []


def test_resume_freezes_fixture_inputs_and_old_schema_cannot_continue(tmp_path: Path) -> None:
    task = task_snapshot(tmp_path)
    _, _, registry = inputs(task)
    executor = FixedTextExecutor("src/main.ets", "value=good\n")
    work = tmp_path / "run"
    run_fixed_agent(task, work, "run1", [response(1)], executor, observations=registry)
    _, _, changed = inputs(task, status="satisfied")
    with pytest.raises(ValueError, match="condition inputs differ"):
        run_fixed_agent(task, work, "run1", [response(1)], executor, observations=changed, resume=True)
    report = run_fixed_agent(task, work, "run1", [response(1), response(2)], executor, resume=True)
    assert report["model_calls"] == 2
    session = json.loads((work / "session.json").read_text())
    assert session["schema_version"] == 5 and session["observations_sha256"] == registry.fingerprint()
    session["schema_version"] = 4
    (work / "session.json").write_text(json.dumps(session), encoding="utf-8")
    with pytest.raises(ValueError, match="session identity"):
        run_fixed_agent(task, work, "run1", [response(1), response(2)], executor, resume=True)


@pytest.mark.parametrize("fault", ["directions", "next_action"])
def test_saved_condition_review_cannot_be_rewritten(tmp_path: Path, fault: str) -> None:
    task = task_snapshot(tmp_path)
    workspace, ledger, _, _, _, _, raw = context(task, tmp_path / "run")
    store = DecisionStore(workspace)
    record = store.decide(DecisionRequest.model_validate_json(json.dumps(raw)), ledger=ledger,
                         features=FULL, capabilities=frozenset({"read_source"}))
    path = workspace.root / "decisions" / f"{record.record_id}.json"
    saved = json.loads(path.read_text())
    if fault == "directions":
        saved["condition_review"]["affected_direction_ids"] = ["repair2"]
    else:
        saved["next_action"] = "direct_candidate"
    path.write_text(json.dumps(saved), encoding="utf-8")
    with pytest.raises(ValueError, match="condition review changed"):
        store.records()


@pytest.mark.parametrize("receipt_saved", [False, True])
def test_condition_receipt_recovery_does_not_repeat_registration_action(tmp_path: Path, monkeypatch, receipt_saved: bool) -> None:
    task = task_snapshot(tmp_path)
    submission, request, registry = inputs(task)
    scripted = [response(1, "record_diagnosis", {"diagnosis_json": json.dumps(submission)}),
                response(2, "evaluate_condition", {"request_json": request.model_dump_json(), "observation_id": "fixture1"}),
                response(3)]
    original_complete = ToolActionStore.complete
    original_evaluate = ConditionStore.evaluate
    calculations = 0

    def count(self, condition, observation_id=None):
        nonlocal calculations
        calculations += 1
        return original_evaluate(self, condition, observation_id)

    def crash(self, action_id, message):
        if message.name == "evaluate_condition":
            if receipt_saved:
                original_complete(self, action_id, message)
            raise RuntimeError("synthetic condition checkpoint crash")
        return original_complete(self, action_id, message)

    monkeypatch.setattr(ConditionStore, "evaluate", count)
    monkeypatch.setattr(ToolActionStore, "complete", crash)
    with pytest.raises(RuntimeError, match="condition checkpoint crash"):
        run_fixed_agent(task, tmp_path / "run", "run1", scripted, FixedTextExecutor("src/main.ets", "value=good\n"),
                        observations=registry, policy_features=FULL)
    monkeypatch.setattr(ToolActionStore, "complete", original_complete)
    report = run_fixed_agent(task, tmp_path / "run", "run1", scripted, FixedTextExecutor("src/main.ets", "value=good\n"),
                             observations=registry, policy_features=FULL, resume=True)
    assert calculations == 1 and len(report["condition_results"]) == 1 and report["checks"] == []
    assert report["uncertain_tool_calls"] == (0 if receipt_saved else 1)
    assert report["stopped_reason"] == (None if receipt_saved else "action_result_unknown")
