"""Completed checks survive a final budget stop; unknown work never does."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from test_competition import REPO, config, report_fixture

from dfx_agent.competition import _report, run_competition
from dfx_agent.model_calls import ModelCallStore


@pytest.mark.parametrize("stop", ["model_budget", "request_token_budget", "action_budget", "wall_time"])
def test_completed_application_receipt_needs_no_final_model_message(tmp_path: Path, stop: str) -> None:
    work, solver, executor, settings = report_fixture(tmp_path, stopped=stop)
    report = _report(work, solver, executor, settings)
    assert report["accepted"] is True and report["status"] == "application_verified"
    assert report["stopped_reason"] == stop
    assert report["metrics"]["repair_success"] is True


@pytest.mark.parametrize("stop", ["model_budget", "request_token_budget"])
@pytest.mark.parametrize("blocker", ["unknown_model", "unknown_tool", "missing_localization", "failed_latest"])
def test_budget_stop_cannot_accept_missing_or_unknown_work(tmp_path: Path, blocker: str, stop: str) -> None:
    work, solver, executor, settings = report_fixture(
        tmp_path, stopped=stop, linked=blocker != "missing_localization",
        status="fail" if blocker == "failed_latest" else "pass")
    if blocker == "unknown_model":
        solver["uncertain_model_calls"] = 1
    elif blocker == "unknown_tool":
        solver["uncertain_tool_calls"] = 1
    report = _report(work, solver, executor, settings)
    assert report["accepted"] is False and report["status"] == "unresolved"


def test_last_model_call_can_finish_repair_and_resume_without_a_goodbye(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Actual SDK/mock and toy programs, no application or real cloud run."""
    monkeypatch.setenv("DFX_MANAGED_COMPETITION", "local-v1")
    settings = config(tmp_path, max_calls=14)
    value = json.loads(settings.read_bytes())
    value["provider"]["network"] = {"max_retries": 2, "request_timeout_seconds": 30.0,
                                      "connect_timeout_seconds": 10.0}
    settings.write_text(json.dumps(value), encoding="utf-8")
    work = tmp_path / "run"
    result = run_competition(tmp_path / "unused", work, "last-call", settings, REPO)
    assert result["status"] == "synthetic_checked" and result["model_calls"] == 14
    report_path = work / "competition-report.json"
    first = report_path.read_bytes()
    report = json.loads(first)
    assert report["stopped_reason"] == "model_budget"
    assert report["localization_result"]["final"]["complete"] is True
    assert [attempt["check"]["status"] for attempt in report["attempts"]] == ["fail", "pass"]
    assert report["accepted"] is False and report["application_verification"] == "not_run"
    before = ModelCallStore(work / "model-calls.sqlite").usage_summary("competition-demo", "last-call")
    replay = run_competition(tmp_path / "unused", work, "last-call", settings, REPO, resume=True)
    assert replay == result and report_path.read_bytes() == first
    assert ModelCallStore(work / "model-calls.sqlite").usage_summary("competition-demo", "last-call") == before
