"""Real SDK + mock wire + actual bounded toy subprocesses, never HarmonyOS effects."""

from __future__ import annotations

import json
import os
import zipfile
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from test_applicability_provider import tool_result

from dfx_agent import competition, models
from dfx_agent.agent import ResumeAdmissionRejected
from dfx_agent.competition import (
    CompetitionConfig,
    _previous_report,
    _report,
    run_competition,
    verify_private_delivery,
)
from dfx_agent.local_execution import LocalApplicationExecutor
from dfx_agent.model_calls import ModelCallStore, canonical, sha256
from dfx_agent.working_context_plan import WORKING_CONTEXT_PROMPT

REPO = Path(__file__).resolve().parents[1]


def config(tmp_path: Path, **overrides: object) -> Path:
    value = json.loads((REPO / "configs/competition-demo.json").read_bytes())
    value.update(overrides)
    path = tmp_path / "config.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def test_complete_sdk_local_repair_feedback_skills_report_and_resume(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DFX_MANAGED_COMPETITION", "local-v1")
    requests = []
    original = competition._DemoModel.__call__

    def observe(model, request):
        requests.append(json.loads(request.content))
        return original(model, request)

    monkeypatch.setattr(competition._DemoModel, "__call__", observe)
    settings = config(tmp_path, max_calls=15)
    work = tmp_path / "run"
    result = run_competition(tmp_path / "unused", work, "software-acceptance", settings, REPO)
    assert result["status"] == "synthetic_checked", (work / "agent-report.json").read_text(encoding="utf-8")
    assert result["accepted"] is False and result["application_verification"] == "not_run"
    assert result["candidate_count"] == 2 and result["model_calls"] == 15
    report = json.loads((work / "competition-report.json").read_bytes())
    assert report["schema_version"] == 3
    assert report["localization_result"]["final"]["complete"] is True
    assert report["localization_result"]["final"]["selected"]["root_cause_locations"][0]["start_line"] == 1
    assert [attempt["check"]["status"] for attempt in report["attempts"]] == ["fail", "pass"]
    first, last = report["attempts"]
    assert first["execution"]["stages"][-1]["stage"] == "behavior"
    assert "required value semantics lost" in first["check"]["detail"]
    assert all(attempt["execution"]["baseline"]["fault_reproduced"] for attempt in report["attempts"])
    assert [stage["stage"] for stage in last["execution"]["stages"]] == ["build", "target", "behavior", "regression"]
    assert len(report["decisions"]) == 2 and report["decisions"][1]["next_action"] == "reconsider_repair"
    assert last["candidate"]["preserves"] == ["preserve-value"]
    assert report["metrics"]["first_attempt_pass"] is False
    assert report["metrics"]["root_cause_accuracy"] is None
    assert report["cost"]["total_tokens"] == 225
    solver = json.loads((work / "agent-report.json").read_bytes())
    assert report["localization"] == solver["completion"]["localization"]
    assert len(solver["static_skill_facts"]["resources"]) == 4
    assert any(resource["actions"] for resource in solver["static_skill_facts"]["resources"])
    assert solver["working_context_plans"]
    plans = [json.loads(path.read_bytes()) for path in (work / "working-context").glob("*.plan.json")]
    assert plans and all(plan["policy"]["history_selection"] == "repair_frontier" and
                         plan["policy"]["context_layout"] == "decision_layers" for plan in plans)
    assert any(json.loads(plan["selection_json"])["omitted_ranges"] for plan in plans)
    assert len(requests) == 15
    panels = [json.loads(body["messages"][1]["content"].split(WORKING_CONTEXT_PROMPT + "\n", 1)[1])
              for body in requests]
    assert all("packet" not in panel and "decision_context" in panel for panel in panels)
    contexts = [panel["decision_context"] for panel in panels]
    assert contexts[0]["decision_board"]["phase"] == "exploration"
    failures = [context["revision"]["latest_failure"] for context in contexts
                if context["revision"]["latest_failure"] is not None]
    assert failures and all(failure["candidate_id"] == first["candidate"]["candidate_id"] and
                            failure["check_result"]["status"] == "fail" for failure in failures)
    assert contexts[-1]["decision_board"]["phase"] == "delivery_review"
    assert contexts[-1]["revision"]["latest_failure"] is None
    assert all(context["decision_board"]["execution_permission"] == "not_granted" and
               context["decision_board"]["repair_acceptance"] == "not_assessed" for context in contexts)
    skill = tool_result(requests[4], "read_skill")
    assert skill["body"] and tool_result(requests[-1], "read_skill") == skill
    manifest = verify_private_delivery(work / "delivery.zip", 268435456)
    assert any(item["path"].endswith("patch.diff") for item in manifest["files"])
    with zipfile.ZipFile(work / "delivery.zip") as archive:
        assert all(b"dfx-synthetic-credential-never-real" not in archive.read(item) for item in archive.namelist())
    assert "DFX_DEMO_PROVIDER_KEY" not in os.environ
    checks = {path.name: path.read_bytes() for path in (work / "checks").glob("*.json")}
    resumed = run_competition(tmp_path / "unused", work, "software-acceptance", settings, REPO, resume=True)
    assert resumed["status"] == "synthetic_checked" and resumed["model_calls"] == 15
    restored_report = json.loads((work / "competition-report.json").read_bytes())
    assert restored_report["timing"] == report["timing"]
    assert restored_report["report_sha256"] == report["report_sha256"]
    assert checks == {path.name: path.read_bytes() for path in (work / "checks").glob("*.json")}
    assert len(requests) == 15
    assert ModelCallStore(work / "model-calls.sqlite").usage_summary("competition-demo", "software-acceptance")["completed_calls"] == 15
    # Original host approval and original configuration are required on recovery.
    settings.write_text(settings.read_text(encoding="utf-8").replace('"max_calls": 15', '"max_calls": 16'), encoding="utf-8")
    before = (work / "competition-report.json").read_bytes()
    with pytest.raises(ResumeAdmissionRejected):
        run_competition(tmp_path / "unused", work, "software-acceptance", settings, REPO, resume=True)
    assert (work / "competition-report.json").read_bytes() == before


def test_cloud_mode_refused_before_writable_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DFX_MANAGED_COMPETITION", "local-v1")
    settings = REPO / "configs/competition-cloud.template.json"
    with pytest.raises(ValueError, match="real model experiments are disabled"):
        run_competition(tmp_path / "missing", tmp_path / "run", "no-network", settings, REPO)
    assert not (tmp_path / "run").exists()


def test_interruption_after_saved_model_response_replays_then_continues(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DFX_MANAGED_COMPETITION", "local-v1")
    original = models.invoke_accounted

    def interrupt(*args: object, **kwargs: object) -> object:
        result = original(*args, **kwargs)  # type: ignore[arg-type]
        if args[1].call_id == "model-6":  # type: ignore[attr-defined]
            raise KeyboardInterrupt("fixture loss after persisted provider response")
        return result

    monkeypatch.setattr(models, "invoke_accounted", interrupt)
    settings = config(tmp_path)
    work = tmp_path / "run"
    with pytest.raises(KeyboardInterrupt):
        run_competition(tmp_path / "unused", work, "interrupted", settings, REPO)
    monkeypatch.setattr(models, "invoke_accounted", original)
    result = run_competition(tmp_path / "unused", work, "interrupted", settings, REPO, resume=True)
    assert result["status"] == "synthetic_checked" and result["model_calls"] == 15
    assert result["candidate_count"] == 2


def test_request_budget_refusal_has_truthful_report(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DFX_MANAGED_COMPETITION", "local-v1")
    settings = config(tmp_path, max_known_tokens=64)
    result = run_competition(tmp_path / "unused", tmp_path / "run", "no-send", settings, REPO)
    assert result["status"] == "unresolved" and result["candidate_count"] == 0 and result["model_calls"] == 0
    report = json.loads((tmp_path / "run/competition-report.json").read_bytes())
    assert report["adapter_error_reason"] == "provider_token_budget" and report["accepted"] is False
    assert report["metrics"]["first_attempt_pass"] is None


def localization_fixture() -> dict[str, Any]:
    location = {"schema_version": 1, "snapshot_sha256": "b" * 64, "path": "src/main.ets",
                "start_line": 2, "end_line": 2, "symbol": None}
    return {"diagnosis_id": "diagnosis-1", "action_id": "action-1",
        "root_cause_locations": [location], "authority": "model_inference", "root_cause_confirmation": "unknown",
        "selected_hypothesis_id": "chosen", "evidence": [], "obligations": [],
        "diagnosis": {"record_id": "diagnosis-1", "source_snapshot_sha256": "b" * 64,
            "selected_hypothesis_id": "chosen", "hypotheses": [{"hypothesis_id": "chosen",
                "root_cause_locations": [deepcopy(location)], "support_status": "unknown"}]}}


def report_fixture(tmp_path: Path, *, mode: str = "application", status: str = "pass",
                   linked: bool = True, checked: bool = True, stopped: str | None = None) -> tuple[
                       Path, dict[str, Any], LocalApplicationExecutor, CompetitionConfig]:
    """Only host-receipt fixtures; no application process or cloud request."""
    work = tmp_path / "report"
    work.mkdir()
    (work / "session.json").write_text(json.dumps({"started_at": "2026-10-08T00:00:00+00:00"}), encoding="utf-8")
    level = "application_execution" if mode == "application" else "offline_synthetic"
    execution = {"mode": mode, "status": status, "verification_level": level,
        "application_verification": status if mode == "application" else "not_run",
        "accepted": mode == "application" and status == "pass", "stages": [], "baseline": {"fault_reproduced": True}}
    check = {"status": status, "level": level, "idempotency_key": "a" * 64, "detail": "fixture check"}
    candidate = {"candidate_id": "d" * 64, "ordinal": 1, "diagnosis_id": "diagnosis-1" if linked else None}
    localized = localization_fixture()
    solver = {"task_id": "fixture-task", "run_id": "fixture-run", "source_snapshot_sha256": "b" * 64,
        "stopped_reason": stopped, "adapter_error_reason": None, "uncertain_model_calls": 0,
        "uncertain_tool_calls": 0, "actions": [], "model_calls": 1, "known_tokens": 15, "missing_usage_calls": 0,
        "decisions": [], "completion": {"execution_provenance": {"scope": "fixture"}, "cost": {"known_total_tokens": 15},
            "localization": {"candidate_linked": deepcopy(localized) if linked else None,
                             "initial": None, "latest": localized},
            "candidate_artifacts": [{"candidate": candidate, "patch_path": "candidate/patch.diff",
                "patch_sha256": "c" * 64, "check": check if checked else None}]}}
    executor = cast(LocalApplicationExecutor, SimpleNamespace(evidence=lambda _key: execution))
    settings = CompetitionConfig.model_validate_json((REPO / "configs/competition-demo.json").read_bytes())
    return work, solver, executor, settings


@pytest.mark.parametrize("mode,check_status,linked,checked,stopped,expected_state,expected_outcome,accepted", [
    ("application", "pass", True, True, None, "pass", "application_verified", True),
    ("application", "pass", False, True, None, "pass", "unresolved", False),
    ("application", "fail", True, True, None, "fail", "unresolved", False),
    ("application", "inconclusive", True, True, None, "inconclusive", "unresolved", False),
    ("application", "pass", True, False, None, "not_run", "unresolved", False),
    ("application", "pass", True, True, "tool_limit", "pass", "unresolved", False),
    ("synthetic", "pass", True, True, None, "not_run", "synthetic_checked", False),
    ("synthetic", "fail", True, True, None, "not_run", "unresolved", False),
])
def test_report_distinguishes_execution_from_product_acceptance(tmp_path: Path, mode: str, check_status: str,
        linked: bool, checked: bool, stopped: str | None, expected_state: str, expected_outcome: str, accepted: bool) -> None:
    work, solver, executor, settings = report_fixture(tmp_path, mode=mode, status=check_status,
        linked=linked, checked=checked, stopped=stopped)
    report = _report(work, solver, executor, settings, observed_at=datetime(2026, 10, 8, 0, 0, 10, tzinfo=UTC))
    assert report["application_verification"] == expected_state
    assert report["status"] == expected_outcome and report["accepted"] is accepted
    assert report["verification_level"] == ("not_run" if not checked else
                                           "application_execution" if mode == "application" else "offline_synthetic")
    assert expected_state in (work / "result.md").read_text(encoding="utf-8")


@pytest.mark.parametrize("missing", ["selected", "selected_location", "candidate_link", "source"])
def test_application_pass_does_not_supply_missing_final_root_cause(tmp_path: Path, missing: str) -> None:
    work, solver, executor, settings = report_fixture(tmp_path)
    localized = solver["completion"]["localization"]["candidate_linked"]
    if missing == "selected":
        localized["diagnosis"]["selected_hypothesis_id"] = None
        localized["selected_hypothesis_id"] = None
    elif missing == "selected_location":
        alternative = deepcopy(localized["diagnosis"]["hypotheses"][0])
        alternative["hypothesis_id"] = "alternative"
        localized["diagnosis"]["hypotheses"].append(alternative)
        localized["diagnosis"]["hypotheses"][0]["root_cause_locations"] = []
        # The old merged field remains nonempty: only the alternative has a line.
        assert localized["root_cause_locations"]
    elif missing == "candidate_link":
        solver["completion"]["candidate_artifacts"][0]["candidate"]["diagnosis_id"] = "another-diagnosis"
    else:
        localized["diagnosis"]["hypotheses"][0]["root_cause_locations"][0]["snapshot_sha256"] = "e" * 64
    original = deepcopy(solver["completion"])
    report = _report(work, solver, executor, settings)
    assert report["application_verification"] == "pass" and report["verification_level"] == "application_execution"
    assert report["accepted"] is False and report["status"] == "unresolved"
    assert report["localization_result"]["final"]["complete"] is False
    assert report["localization_result"]["final"]["incomplete_reasons"]
    assert report["metrics"]["root_cause_accuracy"] is None
    assert solver["completion"] == original and report["localization"] == original["localization"]


def test_selected_root_cause_is_distinct_from_alternatives_and_latest_analysis(tmp_path: Path) -> None:
    work, solver, executor, settings = report_fixture(tmp_path)
    localized = solver["completion"]["localization"]["candidate_linked"]
    alternative = deepcopy(localized["diagnosis"]["hypotheses"][0])
    alternative["hypothesis_id"] = "alternative"
    alternative["root_cause_locations"][0].update(start_line=9, end_line=9)
    localized["diagnosis"]["hypotheses"].append(alternative)
    localized["root_cause_locations"].extend(deepcopy(alternative["root_cause_locations"]))
    latest = solver["completion"]["localization"]["latest"]
    latest["diagnosis_id"] = latest["diagnosis"]["record_id"] = "later-unbound-diagnosis"
    latest["diagnosis"]["hypotheses"][0]["root_cause_locations"][0].update(start_line=7, end_line=7)
    report = _report(work, solver, executor, settings)
    final = report["localization_result"]["final"]
    assert final["complete"] is True and report["accepted"] is True
    assert final["diagnosis_id"] == "diagnosis-1" and final["candidate_id"] == "d" * 64
    assert final["selected"]["root_cause_locations"][0]["start_line"] == 2
    assert final["alternatives"][0]["root_cause_locations"][0]["start_line"] == 9
    assert report["localization_result"]["latest"]["diagnosis_id"] == "later-unbound-diagnosis"
    assert report["metrics"]["root_cause_accuracy"] is None
    final_text = (work / "result.md").read_text(encoding="utf-8").split("最新候选关联的最终定位：")[1]
    selected_text = final_text.split("备选假设")[0]
    assert "src/main.ets:2-2" in selected_text and "src/main.ets:9-9" not in selected_text
    assert "备选假设（未作为所选根因）：alternative" in final_text


def test_terminal_report_timing_reuses_same_facts_but_refreshes_new_progress(tmp_path: Path) -> None:
    work, solver, executor, settings = report_fixture(tmp_path)
    finish = datetime(2026, 10, 8, 0, 0, 10, tzinfo=UTC)
    first = _report(work, solver, executor, settings, observed_at=finish)
    previous = _previous_report(work, {"task_id": "fixture-task", "source_snapshot_sha256": "b" * 64}, "fixture-run")
    replay = _report(work, solver, executor, settings, previous_report=previous, observed_at=finish + timedelta(days=2))
    assert replay["timing"] == first["timing"] and replay["timing"]["agent_wall_ms"] == 10000
    assert replay["report_sha256"] == first["report_sha256"]
    progressed = deepcopy(solver)
    progressed["completion"]["cost"]["known_total_tokens"] += 15
    refreshed = _report(work, progressed, executor, settings, previous_report=replay, observed_at=finish + timedelta(seconds=5))
    assert refreshed["timing"]["agent_wall_ms"] == 15000
    assert refreshed["timing"]["terminal_facts_sha256"] != first["timing"]["terminal_facts_sha256"]


def test_schema_two_upgrade_keeps_terminal_observation_when_only_acceptance_changes(tmp_path: Path) -> None:
    work, solver, executor, settings = report_fixture(tmp_path)
    solver["completion"]["localization"]["candidate_linked"]["diagnosis"]["hypotheses"][0]["root_cause_locations"] = []
    finish = datetime(2026, 10, 8, 0, 0, 10, tzinfo=UTC)
    legacy = _report(work, solver, executor, settings, observed_at=finish)
    legacy["schema_version"] = 2
    legacy.pop("localization_result")
    legacy["accepted"] = True
    legacy["status"] = "application_verified"
    legacy["metrics"].update(functional_correctness=True, repair_success=True)
    legacy["timing"]["terminal_facts_sha256"] = sha256(canonical({key: value for key, value in legacy.items()
                                                               if key not in {"timing", "report_sha256"}}))
    legacy["report_sha256"] = sha256(canonical({key: value for key, value in legacy.items() if key != "report_sha256"}))
    (work / "competition-report.json").write_text(canonical(legacy), encoding="utf-8")
    previous = _previous_report(work, {"task_id": "fixture-task", "source_snapshot_sha256": "b" * 64}, "fixture-run")
    upgraded = _report(work, solver, executor, settings, previous_report=previous, observed_at=finish + timedelta(days=2))
    assert upgraded["schema_version"] == 3 and upgraded["accepted"] is False
    assert upgraded["application_verification"] == "pass"
    assert upgraded["timing"]["finished_at"] == legacy["timing"]["finished_at"]
    assert upgraded["timing"]["agent_wall_ms"] == legacy["timing"]["agent_wall_ms"] == 10000
    assert upgraded["timing"]["terminal_facts_sha256"] != legacy["timing"]["terminal_facts_sha256"]
    repeated = _report(work, solver, executor, settings, previous_report=upgraded, observed_at=finish + timedelta(days=3))
    assert repeated["timing"] == upgraded["timing"] and repeated["report_sha256"] == upgraded["report_sha256"]
    progressed = deepcopy(solver)
    changed_location = progressed["completion"]["localization"]["latest"]["diagnosis"]["hypotheses"][0]["root_cause_locations"][0]
    changed_location.update(start_line=3, end_line=3)
    refreshed = _report(work, progressed, executor, settings, previous_report=previous,
                        observed_at=finish + timedelta(seconds=5))
    assert refreshed["timing"]["agent_wall_ms"] == 15000


def test_legacy_report_has_unknown_finish_and_timing_drift_is_rejected(tmp_path: Path) -> None:
    work, solver, executor, settings = report_fixture(tmp_path)
    finish = datetime(2026, 10, 8, 0, 0, 10, tzinfo=UTC)
    legacy = _report(work, solver, executor, settings, observed_at=finish)
    legacy["schema_version"] = 1
    legacy.pop("localization_result")
    legacy.pop("verification_level")
    legacy["timing"] = {"agent_wall_ms": 999999}
    legacy["stopped_reason"] = "model_budget"
    legacy["status"] = "unresolved"
    legacy["report_sha256"] = sha256(canonical({key: value for key, value in legacy.items() if key != "report_sha256"}))
    path = work / "competition-report.json"
    path.write_text(canonical(legacy), encoding="utf-8")
    previous = _previous_report(work, {"task_id": "fixture-task", "source_snapshot_sha256": "b" * 64}, "fixture-run")
    current = _report(work, solver, executor, settings, previous_report=previous, observed_at=finish + timedelta(days=1))
    assert current["timing"]["agent_wall_ms"] is None and current["timing"]["finished_at"] is None
    assert current["timing"]["reason"] == "legacy_terminal_time_unknown"
    repeated = _report(work, solver, executor, settings, previous_report=current, observed_at=finish + timedelta(days=2))
    assert repeated["timing"] == current["timing"]
    current["timing"]["agent_wall_ms"] = 10
    current["report_sha256"] = sha256(canonical({key: value for key, value in current.items() if key != "report_sha256"}))
    path.write_text(canonical(current), encoding="utf-8")
    before = path.read_bytes()
    with pytest.raises(ResumeAdmissionRejected, match="terminal timing"):
        _previous_report(work, {"task_id": "fixture-task", "source_snapshot_sha256": "b" * 64}, "fixture-run")
    assert path.read_bytes() == before
