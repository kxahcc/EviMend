"""Host completion facts remain distinct from model claims and application validation."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage
from test_agent_loop import candidate_id, response, task_snapshot

from dfx_agent.actions import ToolActionStore, ToolActionUncertain
from dfx_agent.agent import run_fixed_agent
from dfx_agent.model_calls import ModelCallStore, canonical, sha256
from dfx_agent.reporting import build_completion, read_completion
from dfx_agent.verification import CheckLedger, FixedTextExecutor
from dfx_agent.workspace import CandidateWorkspace

LOCATION = {"path": "src/main.ets", "line": 1, "reason": "synthetic bad value"}
EDIT = {"path": "src/main.ets", "old": "bad", "new": "good"}


def run_case(tmp_path: Path, scripted: list[AIMessage], **kwargs):
    task = task_snapshot(tmp_path) if not (tmp_path / "input").exists() else tmp_path / "input"
    work = tmp_path / "run"
    executor = FixedTextExecutor("src/main.ets", "value=good\n")
    report = run_fixed_agent(task, work, "run1", scripted, executor, **kwargs)
    workspace = CandidateWorkspace.reopen(task, work, "run1")
    ledger = CheckLedger(workspace, executor)
    calls = ModelCallStore(work / "model-calls.sqlite")
    actions = ToolActionStore(work / "tool-actions.sqlite")
    return workspace, ledger, calls, actions, report


def completion(case, *, stopped_reason=None):
    workspace, ledger, calls, actions, report = case
    reason = report["stopped_reason"] if stopped_reason is None else stopped_reason
    return build_completion(workspace, ledger, calls, actions, stopped_reason=reason)


def save(case, value):
    workspace, _, _, _, report = case
    report = {**report, "schema_version": 4, "completion": value,
              "status": value["suggested_status"], "stopped_reason": value["stopped_reason"]}
    report.update(model_calls=value["cost"]["reserved_calls"], known_tokens=value["cost"]["known_total_tokens"],
                  missing_usage_calls=value["cost"]["missing_usage_calls"] + value["cost"]["invalid_usage_calls"],
                  uncertain_model_calls=value["cost"]["uncertain_calls"], tool_calls=value["cost"]["tool_calls"],
                  rejected_tool_calls=value["cost"]["rejected_tool_calls"], uncertain_tool_calls=value["cost"]["uncertain_tool_calls"],
                  candidates=[item["candidate"]["candidate_id"] for item in value["candidate_artifacts"]],
                  checks=[item["check"] for item in value["candidate_artifacts"] if item["check"] is not None])
    (workspace.root / "agent-report.json").write_text(json.dumps(report), encoding="utf-8")
    return report


def passing_case(tmp_path: Path):
    task = task_snapshot(tmp_path)
    good = candidate_id(task, tmp_path / "derive", "good")
    return run_case(tmp_path, [response(1, "record_diagnosis", LOCATION), response(2, "edit_source", EDIT),
                               response(3, "register_candidate", {"diagnosis_id": "diagnosis-1"}),
                               response(4, "run_check", {"candidate_id": good}), response(5)])


def test_completion_is_reproducible_and_read_only(tmp_path: Path) -> None:
    case = passing_case(tmp_path)
    workspace, ledger, calls, actions, _ = case
    value = completion(case)
    assert completion(case) == value
    assert value["suggested_status"] == "offline_checked"
    assert value["verification"]["level"] == "offline_synthetic"
    assert value["verification"]["application_verification"] == "not_run"
    assert value["verification"]["accepted"] is False
    assert value["localization"]["initial"]["diagnosis_id"] == "diagnosis-1"
    assert value["localization"]["latest"] == value["localization"]["candidate_linked"]
    assert value["localization"]["latest"]["root_cause_confirmation"] == "unknown"
    artifact = value["candidate_artifacts"][0]
    assert artifact["patch_path"] == f"candidates/{value['latest_candidate_id']}/patch.diff"
    assert artifact["patch_sha256"] == artifact["candidate"]["diff_sha256"]
    assert value["cost"]["known_input_tokens"] == 50
    assert value["cost"]["known_output_tokens"] == 25
    assert value["cost"]["total_tokens"] == 75
    assert value["cost"]["monetary_cost"] is None
    assert value["timing"] == {"localization_ms": None, "repair_ms": None}
    before = (len(ledger.executor.calls), calls.usage_summary("task1", "run1"), actions.records())
    save(case, value)
    assert read_completion(workspace, ledger, calls, actions) == value
    assert (len(ledger.executor.calls), calls.usage_summary("task1", "run1"), actions.records()) == before
    assert not list(workspace.root.glob("completion*"))


def test_model_text_cannot_claim_success_or_create_a_location(tmp_path: Path) -> None:
    case = run_case(tmp_path, [AIMessage(content="I repaired it. All application tests passed.")])
    value = completion(case)
    assert value["suggested_status"] == "unresolved"
    assert value["localization"]["latest"] is None
    assert value["latest_candidate_id"] is None
    assert value["verification"]["accepted"] is False
    assert value["cost"]["usage_complete"] is False and value["cost"]["total_tokens"] is None
    assert value["cost"]["missing_usage_calls"] == 1
    assert {"localization_missing", "candidate_missing", "application_verification_not_run"} <= set(value["unresolved_reasons"])


@pytest.mark.parametrize("first_edit", [EDIT, {**EDIT, "old": "missing"}])
def test_initial_location_must_precede_even_an_unsuccessful_authorized_edit(tmp_path: Path, first_edit: dict) -> None:
    case = run_case(tmp_path, [response(1, "edit_source", first_edit),
                               response(2, "record_diagnosis", LOCATION), response(3)])
    value = completion(case)
    assert value["localization"]["initial"] is None
    assert value["localization"]["latest"]["diagnosis_id"] == "diagnosis-1"
    assert value["localization"]["initial_cutoff_action_id"] == case[3].records()[0]["action_id"]


def test_rejected_diagnosis_is_not_initial_or_latest(tmp_path: Path) -> None:
    case = run_case(tmp_path, [response(1, "record_diagnosis", {**LOCATION, "line": 999}),
                               response(2, "edit_source", EDIT), response(3, "record_diagnosis", LOCATION), response(4)])
    value = completion(case)
    assert value["localization"]["initial"] is None
    assert value["localization"]["diagnosis_order"] == ["diagnosis-1"]


def test_host_action_order_ignores_model_dates_and_lexical_filenames(tmp_path: Path) -> None:
    scripted = [response(index, "record_diagnosis", {**LOCATION, "reason": f"proposal {index}"})
                for index in range(1, 13)] + [response(13)]
    case = run_case(tmp_path, scripted)
    workspace = case[0]
    for number, date in ((1, "2030-01-01T00:00:00+00:00"), (12, "2000-01-01T00:00:00+00:00")):
        path = workspace.root / f"diagnoses/diagnosis-{number}.json"
        bundle = json.loads(path.read_text(encoding="utf-8"))
        bundle["diagnosis"]["created_at"] = date
        path.write_text(json.dumps(bundle), encoding="utf-8")
    value = completion(case)
    assert value["localization"]["initial"]["diagnosis_id"] == "diagnosis-1"
    assert value["localization"]["latest"]["diagnosis_id"] == "diagnosis-12"
    assert value["localization"]["diagnosis_order"] == [f"diagnosis-{number}" for number in range(1, 13)]


def test_latest_diagnosis_and_latest_candidate_association_stay_separate(tmp_path: Path) -> None:
    task = task_snapshot(tmp_path)
    good = candidate_id(task, tmp_path / "derive", "good")
    case = run_case(tmp_path, [response(1, "record_diagnosis", LOCATION), response(2, "edit_source", EDIT),
                               response(3, "register_candidate", {"diagnosis_id": "diagnosis-1"}),
                               response(4, "run_check", {"candidate_id": good}),
                               response(5, "record_diagnosis", {**LOCATION, "reason": "a revised explanation"}), response(6)])
    value = completion(case)
    assert value["localization"]["latest"]["diagnosis_id"] == "diagnosis-2"
    assert value["localization"]["candidate_linked"]["diagnosis_id"] == "diagnosis-1"
    assert "latest_diagnosis_not_linked_to_latest_candidate" in value["unresolved_reasons"]


@pytest.mark.parametrize("check_latest", [False, True])
def test_old_pass_never_masks_latest_unchecked_or_failed_candidate(tmp_path: Path, check_latest: bool) -> None:
    task = task_snapshot(tmp_path)
    good = candidate_id(task, tmp_path / "derive1", "good")
    bad = candidate_id(task, tmp_path / "derive2", "worse")
    scripted = [response(1, "edit_source", EDIT), response(2, "register_candidate"),
                response(3, "run_check", {"candidate_id": good}),
                response(4, "edit_source", {**EDIT, "new": "worse"}), response(5, "register_candidate")]
    if check_latest:
        scripted.append(response(6, "run_check", {"candidate_id": bad}))
    scripted.append(response(7))
    value = completion(run_case(tmp_path, scripted))
    assert value["latest_candidate_id"] == bad
    assert value["latest_candidate_check_status"] == ("fail" if check_latest else "not_run")
    assert value["historical_passed_candidate_ids"] == [good]
    assert value["suggested_status"] == "unresolved"


def test_model_budget_has_a_host_reason_and_known_cost(tmp_path: Path) -> None:
    value = completion(run_case(tmp_path, [response(1, "edit_source", EDIT)], max_calls=1))
    assert value["suggested_status"] == "budget_exhausted"
    assert {"budget_exhausted", "model_budget"} <= set(value["unresolved_reasons"])
    assert value["cost"]["reserved_calls"] == 1 and value["cost"]["total_tokens"] == 15


def test_model_usage_invalid_is_distinct_from_missing(tmp_path: Path) -> None:
    message = response(1).model_copy(update={"usage_metadata": {"input_tokens": -1, "output_tokens": 5, "total_tokens": 4}})
    value = completion(run_case(tmp_path, [message]))
    assert value["cost"]["invalid_usage_calls"] == 1
    assert value["cost"]["missing_usage_calls"] == 0
    assert value["cost"]["total_tokens"] is None


def test_unknown_model_result_is_not_zero_cost_or_a_success(tmp_path: Path) -> None:
    case = run_case(tmp_path, [response(1)])
    with closing(sqlite3.connect(case[2].path)) as connection, connection:
        connection.execute("UPDATE model_calls SET state='unknown', observation_json=NULL, message_json=NULL")
    value = completion(case, stopped_reason="model_result_unknown")
    assert value["suggested_status"] == "unresolved"
    assert value["cost"]["uncertain_calls"] == 1
    assert value["cost"]["total_tokens"] is None
    save(case, value)
    assert read_completion(*case[:4]) == value


def test_unknown_action_overrides_a_completed_pass_without_reexecution(tmp_path: Path) -> None:
    case = passing_case(tmp_path)
    action = next(item for item in case[3].records() if item["tool_name"] == "edit_source")
    with closing(sqlite3.connect(case[3].path)) as connection, connection:
        connection.execute("UPDATE tool_actions SET state='dispatched', message_json=NULL, message_sha256=NULL WHERE action_id=?",
                           (action["action_id"],))
    value = completion(case, stopped_reason="action_result_unknown")
    assert value["suggested_status"] == "unresolved"
    assert value["latest_candidate_check_status"] == "pass"
    assert value["cost"]["uncertain_tool_calls"] == 1
    assert len(case[1].executor.calls) == 1
    with pytest.raises(ToolActionUncertain):
        case[3].receipt(action["action_id"])


def test_pending_check_is_unknown_and_is_not_retried_by_reporting(tmp_path: Path) -> None:
    case = passing_case(tmp_path)
    path = next(case[1].root.glob("*.json"))
    intent = json.loads(path.read_text(encoding="utf-8"))
    intent.pop("result")
    intent["state"] = "pending"
    path.write_text(json.dumps(intent), encoding="utf-8")
    value = completion(case)
    assert value["candidate_artifacts"][0]["check_state"] == "pending"
    assert value["latest_candidate_check_status"] == "not_run"
    assert value["verification"]["pending_checks"] == 1
    assert "check_result_unknown" in value["unresolved_reasons"]
    assert len(case[1].executor.calls) == 1


@pytest.mark.parametrize("reason", ["model_result_unknown", "action_result_unknown", "model_budget", "action_budget"])
def test_unsubstantiated_unknown_or_budget_reason_is_rejected(tmp_path: Path, reason: str) -> None:
    case = run_case(tmp_path, [response(1)])
    with pytest.raises(ValueError, match="without ledger evidence"):
        completion(case, stopped_reason=reason)


@pytest.mark.parametrize("field,value", [("schema_version", 3), ("task_id", "another_task"),
                                         ("status", "verified_under_protocol"), ("application_verification", "pass")])
def test_read_rejects_changed_report_identity_or_success_claim(tmp_path: Path, field: str, value: object) -> None:
    case = passing_case(tmp_path)
    report = save(case, completion(case))
    report[field] = value
    (case[0].root / "agent-report.json").write_text(json.dumps(report), encoding="utf-8")
    with pytest.raises(ValueError, match="differs|claim"):
        read_completion(*case[:4])


def test_read_rejects_changed_completion_even_if_its_hash_is_recomputed(tmp_path: Path) -> None:
    case = passing_case(tmp_path)
    value = completion(case)
    value["cost"]["known_total_tokens"] = 0
    value["summary_sha256"] = sha256(canonical({key: item for key, item in value.items() if key != "summary_sha256"}))
    save(case, value)
    with pytest.raises(ValueError, match="current host facts"):
        read_completion(*case[:4])


@pytest.mark.parametrize("artifact", ["patch", "source", "diagnosis", "model_response", "tool_receipt"])
def test_read_rechecks_registered_artifacts_and_receipts(tmp_path: Path, artifact: str) -> None:
    case = passing_case(tmp_path)
    value = completion(case)
    save(case, value)
    workspace = case[0]
    candidate = workspace.candidates / value["latest_candidate_id"]
    if artifact == "patch":
        (candidate / "patch.diff").write_text("not the frozen patch", encoding="utf-8")
    elif artifact == "source":
        (candidate / "source/src/main.ets").write_text("value=changed\n", encoding="utf-8")
    elif artifact == "diagnosis":
        path = workspace.root / "diagnoses/diagnosis-1.json"
        bundle = json.loads(path.read_text(encoding="utf-8"))
        bundle["diagnosis"]["unknowns"].append("changed report input")
        path.write_text(json.dumps(bundle), encoding="utf-8")
    else:
        database = case[2].path if artifact == "model_response" else case[3].path
        table = "model_calls" if artifact == "model_response" else "tool_actions"
        with closing(sqlite3.connect(database)) as connection, connection:
            connection.execute(f"UPDATE {table} SET message_json='{{}}' WHERE rowid=1")
    with pytest.raises(ValueError):
        read_completion(*case[:4])


def test_read_rejects_changed_tool_request_fingerprint(tmp_path: Path) -> None:
    case = passing_case(tmp_path)
    save(case, completion(case))
    with closing(sqlite3.connect(case[3].path)) as connection, connection:
        connection.execute("UPDATE tool_actions SET request_sha256=? WHERE rowid=1", ("0" * 64,))
    with pytest.raises(ValueError, match="tool request fingerprint"):
        read_completion(*case[:4])


@pytest.mark.parametrize("field", ["actions", "decisions", "condition_results", "policy_features", "offered_tools"])
def test_read_rejects_changed_top_level_execution_facts(tmp_path: Path, field: str) -> None:
    case = passing_case(tmp_path)
    report = save(case, completion(case))
    report[field] = {"rule_guidance": True} if field == "policy_features" else ["forged"]
    (case[0].root / "agent-report.json").write_text(json.dumps(report), encoding="utf-8")
    with pytest.raises(ValueError, match="top-level facts"):
        read_completion(*case[:4])


def test_report_declares_unverifiable_per_turn_and_adapter_diagnostics(tmp_path: Path) -> None:
    case = passing_case(tmp_path)
    value = completion(case)
    report = save(case, value)
    scope = value["report_validation"]
    assert {"actions", "decisions", "condition_results", "policy_features", "offered_tools"} <= set(
        scope["verified_top_level_fields"])
    assert {"adapter_error_reason", "model_calls_this_turn", "known_tokens_this_turn", "missing_usage_calls_this_turn"} <= set(
        scope["unverified_top_level_fields"])
    for field in scope["unverified_top_level_fields"]:
        report[field] = "unverified diagnostic only"
    (case[0].root / "agent-report.json").write_text(json.dumps(report), encoding="utf-8")
    assert read_completion(*case[:4]) == value


def test_read_rechecks_unused_condition_inputs_against_frozen_session(tmp_path: Path) -> None:
    from test_condition_handoff import inputs

    case = run_case(tmp_path, [response(1)])
    save(case, completion(case))
    _, _, registry = inputs(tmp_path / "input")
    path = case[0].root / "condition-inputs.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["observations"] = registry.value()
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="condition inputs differ from its session"):
        read_completion(*case[:4])


def test_receipt_is_read_only_and_requires_a_registered_completed_action(tmp_path: Path) -> None:
    case = passing_case(tmp_path)
    records = case[3].records()
    receipt = case[3].receipt(records[0]["action_id"])
    assert json.loads(receipt.content)["diagnosis_id"] == "diagnosis-1"
    assert case[3].records() == records
    with pytest.raises(ValueError, match="not registered"):
        case[3].receipt("not_an_action")


def test_rejected_wrong_owner_proposal_remains_a_rejection_fact(tmp_path: Path) -> None:
    from test_tool_authorization import edit_proposal

    task = task_snapshot(tmp_path)
    case = run_case(tmp_path, [response(1, "edit_source", {**EDIT, "proposal_json": edit_proposal(task, run_id="other_run")}),
                               response(2)])
    value = completion(case)
    assert value["cost"]["rejected_tool_calls"] == 1
    assert value["localization"]["initial_cutoff_action_id"] is None
    assert value["latest_candidate_id"] is None


def test_diagnosis_registration_uses_json_receipt_and_host_record_not_message_status(tmp_path: Path) -> None:
    case = passing_case(tmp_path)
    action = case[3].records()[0]
    message = case[3].receipt(action["action_id"])
    message.status = "error"
    payload = canonical(message.model_dump(mode="json"))
    with closing(sqlite3.connect(case[3].path)) as connection, connection:
        connection.execute("UPDATE tool_actions SET message_json=?, message_sha256=? WHERE action_id=?",
                           (payload, sha256(payload), action["action_id"]))
    value = completion(case)
    assert value["localization"]["initial"]["diagnosis_id"] == "diagnosis-1"


def test_unsequenced_analysis_is_exposed_but_not_selected_as_final(tmp_path: Path) -> None:
    case = passing_case(tmp_path)
    folder = case[0].root / "diagnoses"
    bundle = json.loads((folder / "diagnosis-1.json").read_text(encoding="utf-8"))
    bundle["diagnosis"]["record_id"] = "diagnosis-outside-actions"
    (folder / "diagnosis-outside-actions.json").write_text(json.dumps(bundle), encoding="utf-8")
    value = completion(case)
    assert value["localization"]["unsequenced_diagnosis_ids"] == ["diagnosis-outside-actions"]
    assert value["localization"]["latest"]["diagnosis_id"] == "diagnosis-1"


@pytest.mark.parametrize("field", ["candidates", "checks", "model_calls", "known_tokens", "tool_calls"])
def test_read_rejects_contradictory_top_level_summary(tmp_path: Path, field: str) -> None:
    case = passing_case(tmp_path)
    report = save(case, completion(case))
    report[field] = [] if isinstance(report[field], list) else -1
    (case[0].root / "agent-report.json").write_text(json.dumps(report), encoding="utf-8")
    with pytest.raises(ValueError, match="top-level facts"):
        read_completion(*case[:4])
