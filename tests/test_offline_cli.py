"""Managed CLI handoff with synthetic tasks and in-process mock wire fixtures."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from test_agent_loop import candidate_id, task_snapshot
from test_provider_loop import wire_response

from dfx_agent import cli, models
from dfx_agent.model_calls import ModelCallStore


def script_file(root: Path, *, wire: bool, responses: list, **extra: object) -> Path:
    script = {"schema_version": 2 if wire else 1,
              "expected": {"path": "src/main.ets", "text": "value=good\n"}, **extra}
    if wire:
        script["model"] = {"mode": "mock_chat_completions",
                           "settings": {"provider_id": "mock-provider", "model_id": "synthetic-model"},
                           "responses": responses}
    else:
        script["responses"] = responses
    path = root / "script.json"
    path.write_text(json.dumps(script), encoding="utf-8")
    return path


def invoke(monkeypatch: pytest.MonkeyPatch, task: Path, work: Path, script: Path, *, resume: bool = False) -> None:
    monkeypatch.setenv("DFX_MANAGED_AGENT", "offline-v1")
    monkeypatch.setattr(sys, "argv", ["dfx", "resume" if resume else "repair", "--task", str(task),
                                     "--work", str(work), "--run-id", "run1", "--script", str(script)])
    cli.main()


@pytest.mark.parametrize("wire", [False, True])
def test_cli_legacy_and_mock_wire_share_reviewable_repair_outputs(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, wire: bool) -> None:
    task = task_snapshot(tmp_path)
    good = candidate_id(task, tmp_path / "derive", "good")
    calls = [("record_diagnosis", {"path": "src/main.ets", "line": 1, "reason": "synthetic producer"}),
             ("edit_source", {"path": "src/main.ets", "old": "bad", "new": "good"}),
             ("register_candidate", {"diagnosis_id": "diagnosis-1"}), ("run_check", {"candidate_id": good})]
    if wire:
        responses = [wire_response(i, [call]) for i, call in enumerate(calls, 1)] + [wire_response(5)]
    else:
        responses = [{"tool": name, "args": args,
                      "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}}
                     for name, args in calls] + [{"tool": None, "args": {}, "usage": None}]
    script = script_file(tmp_path, wire=wire, responses=responses, limits={"max_calls": 6})
    work = tmp_path / "run"
    invoke(monkeypatch, task, work, script)
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "offline_checked" and report["application_verification"] == "not_run"
    assert report["completion"]["localization"]["initial"]["diagnosis_id"] == "diagnosis-1"
    artifact = report["completion"]["candidate_artifacts"][0]
    assert artifact["candidate"]["candidate_id"] == good
    assert (work / artifact["patch_path"]).is_file()
    assert report["completion"]["verification"]["accepted"] is False
    assert json.loads((work / "session.json").read_text())["max_calls"] == 6
    invoke(monkeypatch, task, work, script, resume=True)
    resumed = json.loads(capsys.readouterr().out)
    assert resumed["model_calls"] == report["model_calls"] == 5
    assert resumed["model_calls_this_turn"] == resumed["tool_calls"] - report["tool_calls"] == 0
    assert resumed["status"] == "offline_checked" and resumed["stopped_reason"] is None


def test_cli_resume_starts_after_completed_wire_fixtures(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    task = task_snapshot(tmp_path)
    work = tmp_path / "run"
    script = script_file(tmp_path, wire=True, responses=[wire_response(1, [("read_log", {})]), wire_response(2)])
    original = models.invoke_accounted

    class SyntheticCrash(RuntimeError):
        pass

    def crash_after_saved_reply(*args, **kwargs):
        original(*args, **kwargs)
        raise SyntheticCrash("saved response before checkpoint")

    monkeypatch.setattr(models, "invoke_accounted", crash_after_saved_reply)
    with pytest.raises(SyntheticCrash):
        invoke(monkeypatch, task, work, script)
    monkeypatch.setattr(models, "invoke_accounted", original)
    invoke(monkeypatch, task, work, script, resume=True)
    report = json.loads(capsys.readouterr().out)
    assert report["model_calls"] == 2 and report["model_calls_this_turn"] == 1
    assert report["uncertain_model_calls"] == 0 and report["stopped_reason"] is None
    assert report["tool_calls"] == 1 and report["completion"]["cost"]["total_tokens"] == 30
    saved = ModelCallStore(work / "model-calls.sqlite").get("model-2")
    assert saved is not None and saved.observation is not None and not saved.observation.tool_calls


def test_cli_limits_stop_before_next_wire_send(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    task = task_snapshot(tmp_path)
    script = script_file(tmp_path, wire=True, responses=[wire_response(1, [("read_log", {})]), wire_response(2)],
                         limits={"max_calls": 1})
    invoke(monkeypatch, task, tmp_path / "run", script)
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "budget_exhausted" and report["stopped_reason"] == "model_budget"
    assert report["model_calls"] == report["tool_calls"] == 1


def test_cli_does_not_open_unmanaged_agent(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("DFX_MANAGED_AGENT", raising=False)
    monkeypatch.setattr(sys, "argv", ["dfx", "repair", "--task", str(tmp_path), "--work", str(tmp_path / "run"),
                                     "--run-id", "run1", "--fixed-script", str(tmp_path / "missing.json")])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 2 and not (tmp_path / "run").exists()


@pytest.mark.parametrize("field", ["api_key", "endpoint"])
def test_cli_rejects_cloud_configuration_before_workspace_creation(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str) -> None:
    task = task_snapshot(tmp_path)
    script = script_file(tmp_path, wire=True, responses=[wire_response(1)])
    value = json.loads(script.read_text())
    value["model"]["settings"][field] = "not-a-real-secret-or-endpoint"
    script.write_text(json.dumps(value), encoding="utf-8")
    work = tmp_path / "run"
    with pytest.raises(SystemExit) as exc:
        invoke(monkeypatch, task, work, script)
    assert exc.value.code == 2 and not work.exists()
