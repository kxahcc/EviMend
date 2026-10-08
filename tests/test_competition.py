"""Real SDK + mock wire + actual bounded toy subprocesses, never HarmonyOS effects."""

from __future__ import annotations

import json
import os
import zipfile
from pathlib import Path

import pytest

from dfx_agent import models
from dfx_agent.agent import ResumeAdmissionRejected
from dfx_agent.competition import run_competition, verify_private_delivery
from dfx_agent.model_calls import ModelCallStore

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
    settings = config(tmp_path)
    work = tmp_path / "run"
    result = run_competition(tmp_path / "unused", work, "software-acceptance", settings, REPO)
    assert result["status"] == "synthetic_checked", (work / "agent-report.json").read_text(encoding="utf-8")
    assert result["accepted"] is False and result["application_verification"] == "not_run"
    assert result["candidate_count"] == 2 and result["model_calls"] == 15
    report = json.loads((work / "competition-report.json").read_bytes())
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
    assert len(solver["static_skill_facts"]["resources"]) == 4
    assert any(resource["actions"] for resource in solver["static_skill_facts"]["resources"])
    assert solver["working_context_plans"]
    manifest = verify_private_delivery(work / "delivery.zip", 268435456)
    assert any(item["path"].endswith("patch.diff") for item in manifest["files"])
    with zipfile.ZipFile(work / "delivery.zip") as archive:
        assert all(b"dfx-synthetic-credential-never-real" not in archive.read(item) for item in archive.namelist())
    assert "DFX_DEMO_PROVIDER_KEY" not in os.environ
    checks = {path.name: path.read_bytes() for path in (work / "checks").glob("*.json")}
    resumed = run_competition(tmp_path / "unused", work, "software-acceptance", settings, REPO, resume=True)
    assert resumed["status"] == "synthetic_checked" and resumed["model_calls"] == 15
    assert checks == {path.name: path.read_bytes() for path in (work / "checks").glob("*.json")}
    assert ModelCallStore(work / "model-calls.sqlite").usage_summary("competition-demo", "software-acceptance")["completed_calls"] == 15
    # Original host approval and original configuration are required on recovery.
    settings.write_text(settings.read_text(encoding="utf-8").replace('"max_calls": 40', '"max_calls": 41'), encoding="utf-8")
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
