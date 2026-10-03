"""SDK and CLI resume gates over synthetic inputs; no live model or application."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from test_agent_loop import response, task_snapshot
from test_offline_cli import invoke, script_file
from test_provider_loop import scripted_adapter, wire_response

from dfx_agent import agent, model_calls, models, provenance
from dfx_agent.verification import FixedTextExecutor


def _tree(root: Path) -> dict[str, tuple[int, int, int, bytes | None]]:
    """Include directories and mtimes, so even a rejected host write is visible."""
    paths = [root, *root.rglob("*")]
    return {path.relative_to(root).as_posix(): (
        path.stat().st_mode, path.stat().st_size, path.stat().st_mtime_ns,
        path.read_bytes() if path.is_file() else None,
    ) for path in paths}


def _completed_run(root: Path, *, wire: bool) -> tuple[Path, Path, FixedTextExecutor, list[dict], dict[str, Any]]:
    task, work = task_snapshot(root), root / "run"
    executor = FixedTextExecutor("src/main.ets", "value=good\n")
    requests: list[dict] = []
    if wire:
        result = agent.run_provider_agent(task, work, "run1", scripted_adapter([wire_response(1)], requests), executor)
    else:
        result = agent.run_fixed_agent(task, work, "run1", [response(1)], executor)
    return task, work, executor, requests, result


def _session(work: Path) -> dict[str, Any]:
    return json.loads((work / "session.json").read_text(encoding="utf-8"))


def _changed_provenance(saved: dict[str, Any], kind: str) -> dict[str, Any]:
    changed = copy.deepcopy(saved)
    if kind == "source":
        changed["source_inventory"][0]["sha256"] = "1" * 64
    elif kind == "lock":
        changed["repository_inputs"]["uv.lock"]["sha256"] = "2" * 64
    else:
        assert kind == "runtime"
        changed["runtime"]["distributions"]["deepagents"] = "999.0.0"
    return provenance._seal(changed)


def _forbid_resume_effects(monkeypatch: pytest.MonkeyPatch, executor: FixedTextExecutor) -> None:
    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        pytest.fail("resume crossed the provenance gate before rejecting incompatible execution")

    for name in ("CheckLedger", "ConditionStore", "ModelCallStore", "ToolActionStore"):
        monkeypatch.setattr(agent, name, forbidden)
    monkeypatch.setattr(models, "invoke_accounted", forbidden)
    monkeypatch.setattr(executor, "check", forbidden)


@pytest.mark.parametrize("wire", [False, True])
def test_new_sdk_run_freezes_one_provenance_record_through_the_report(tmp_path: Path, wire: bool) -> None:
    _task, work, _executor, requests, report = _completed_run(tmp_path, wire=wire)
    session = _session(work)
    assert session["schema_version"] == 10
    assert report["schema_version"] == 7
    assert report["completion"]["schema_version"] == 4
    frozen = provenance.validate_session_provenance(session)
    assert frozen == session["execution_provenance"]
    assert report["execution_provenance"] == report["completion"]["execution_provenance"] == frozen
    assert "execution_provenance" in report["completion"]["report_validation"]["verified_top_level_fields"]
    assert report["model_calls"] == 1 and report["known_tokens"] == 15
    assert len(requests) == int(wire)
    assert report["application_verification"] == "not_run"


@pytest.mark.parametrize("kind", ["source", "lock", "runtime"])
@pytest.mark.parametrize("wire", [False, True])
def test_resume_execution_drift_rejects_before_ledgers_models_or_checks(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str, wire: bool) -> None:
    task, work, executor, requests, first = _completed_run(tmp_path, wire=wire)
    changed = _changed_provenance(_session(work)["execution_provenance"], kind)
    monkeypatch.setattr(provenance, "capture_execution_provenance", lambda: copy.deepcopy(changed))
    _forbid_resume_effects(monkeypatch, executor)
    before, task_before, request_count = _tree(work), _tree(task), len(requests)
    with pytest.raises(ValueError, match="provenance|execution|compatib|identity"):
        if wire:
            agent.run_provider_agent(task, work, "run1", scripted_adapter([wire_response(2)], requests),
                                     executor, resume=True)
        else:
            agent.run_fixed_agent(task, work, "run1", [response(2)], executor, resume=True)
    assert _tree(work) == before and _tree(task) == task_before
    assert len(requests) == request_count
    assert first["model_calls"] == 1


@pytest.mark.parametrize("wire", [False, True])
@pytest.mark.parametrize("legacy_schema", [8, 9])
def test_historical_resume_keeps_all_bytes_untouched(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, wire: bool, legacy_schema: int) -> None:
    task, work, executor, requests, _first = _completed_run(tmp_path, wire=wire)
    session = _session(work)
    session["schema_version"] = legacy_schema
    if legacy_schema == 8:
        session.pop("execution_provenance")
    session["context_profile"].pop("host_working_context")
    session["context_profile"].pop("working_context_prompt_sha256")
    session["context_profile"].pop("working_context_format", None)
    (work / "session.json").write_text(json.dumps(session), encoding="utf-8")
    _forbid_resume_effects(monkeypatch, executor)
    before, task_before, request_count = _tree(work), _tree(task), len(requests)
    with pytest.raises(ValueError, match="session|schema|provenance|resume"):
        if wire:
            agent.run_provider_agent(task, work, "run1", scripted_adapter([wire_response(2)], requests),
                                     executor, resume=True)
        else:
            agent.run_fixed_agent(task, work, "run1", [response(2)], executor, resume=True)
    assert _tree(work) == before and _tree(task) == task_before
    assert len(requests) == request_count


@pytest.mark.parametrize("wire", [False, True])
@pytest.mark.parametrize("initial_git_known", [False, True])
def test_git_only_observation_change_allows_resume_and_retains_original_git(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, wire: bool, initial_git_known: bool) -> None:
    initial_git = {"available": initial_git_known, "commit": "a" * 40 if initial_git_known else None,
                   "dirty": False if initial_git_known else None,
                   "status_entries": 0 if initial_git_known else None,
                   "status_sha256": hashlib.sha256(b"").hexdigest() if initial_git_known else None}
    monkeypatch.setattr(provenance, "_git_facts", lambda: copy.deepcopy(initial_git))
    task, work, executor, requests, first = _completed_run(tmp_path, wire=wire)
    saved = _session(work)["execution_provenance"]
    assert saved["git"] == initial_git
    changed = copy.deepcopy(saved)
    changed["git"] = {"available": True, "commit": "f" * 40, "dirty": False,
                      "status_entries": 0, "status_sha256": hashlib.sha256(b"").hexdigest()}
    changed = provenance._seal(changed)
    assert changed["compatibility_sha256"] == saved["compatibility_sha256"]
    assert changed["provenance_sha256"] != saved["provenance_sha256"]
    monkeypatch.setattr(provenance, "capture_execution_provenance", lambda: copy.deepcopy(changed))
    initial_session_bytes = (work / "session.json").read_bytes()
    request_count = len(requests)
    if wire:
        resumed = agent.run_provider_agent(task, work, "run1", scripted_adapter([], requests), executor, resume=True)
    else:
        resumed = agent.run_fixed_agent(task, work, "run1", [], executor, resume=True)
    assert (work / "session.json").read_bytes() == initial_session_bytes
    assert resumed["execution_provenance"] == resumed["completion"]["execution_provenance"] == saved
    assert resumed["execution_provenance"]["git"] == initial_git
    assert resumed["model_calls"] == first["model_calls"] == 1
    assert resumed["model_calls_this_turn"] == resumed["known_tokens_this_turn"] == 0
    assert len(requests) == request_count and executor.calls == []
    # A terminal resume must remain re-readable by aggregation/export, even
    # though this turn never binds tools through a fresh solver call.
    from dfx_agent.actions import ToolActionStore
    from dfx_agent.model_calls import ModelCallStore
    from dfx_agent.reporting import read_completion
    from dfx_agent.verification import CheckLedger
    from dfx_agent.workspace import CandidateWorkspace

    workspace = CandidateWorkspace.reopen(task, work, "run1")
    rebuilt = read_completion(workspace, CheckLedger(workspace, executor, read_only=True),
                              ModelCallStore(work / "model-calls.sqlite", read_only=True),
                              ToolActionStore(work / "tool-actions.sqlite", read_only=True))
    assert rebuilt == resumed["completion"]


@pytest.mark.parametrize("wire", [False, True])
@pytest.mark.parametrize("damage", ["started_at_missing", "condition_inputs_malformed"])
def test_resume_damaged_admission_facts_reject_before_writable_resources_and_preserve_work(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, wire: bool, damage: str) -> None:
    task, work, executor, requests, _first = _completed_run(tmp_path, wire=wire)
    if damage == "started_at_missing":
        session = _session(work)
        session.pop("started_at")
        (work / "session.json").write_text(json.dumps(session), encoding="utf-8")
    else:
        (work / "condition-inputs.json").write_text('{"malformed": true}', encoding="utf-8")
    (work / "run-state.json").write_text(
        '{"schema_version": 1, "task": "offline", "run_id": "run1", "status": "finished"}',
        encoding="utf-8")

    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        pytest.fail("damaged resume entered writable execution before admission")

    for name in ("ModelCallStore", "ToolActionStore", "create_deep_agent"):
        monkeypatch.setattr(agent, name, forbidden)
    monkeypatch.setattr(models, "invoke_accounted", forbidden)
    monkeypatch.setattr(executor, "check", forbidden)
    before, task_before, request_count = _tree(work), _tree(task), len(requests)
    with pytest.raises(agent.ResumeAdmissionRejected):
        if wire:
            agent.run_provider_agent(task, work, "run1", scripted_adapter([wire_response(2)], requests),
                                     executor, resume=True)
        else:
            agent.run_fixed_agent(task, work, "run1", [response(2)], executor, resume=True)
    assert _tree(work) == before and _tree(task) == task_before
    assert len(requests) == request_count


def test_source_capture_failure_precedes_creation_of_a_new_workspace(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    task, work = task_snapshot(tmp_path), tmp_path / "run"
    task_before = _tree(task)

    def reject_capture() -> dict[str, Any]:
        raise ValueError("execution source changed before capture")

    monkeypatch.setattr(agent, "capture_execution_provenance", reject_capture)
    executor = FixedTextExecutor("src/main.ets", "value=good\n")
    _forbid_resume_effects(monkeypatch, executor)
    with pytest.raises(ValueError, match="execution source"):
        agent.run_fixed_agent(task, work, "run1", [response(1)], executor)
    assert not work.exists() and _tree(task) == task_before


@pytest.mark.parametrize("wire", [False, True])
def test_cli_resume_rejects_drift_before_script_loading_and_writable_offset_ledger(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, wire: bool) -> None:
    responses = [wire_response(1)] if wire else [{"tool": None, "args": {}, "usage": None}]
    task, work = task_snapshot(tmp_path), tmp_path / "run"
    script = script_file(tmp_path, wire=wire, responses=responses)
    invoke(monkeypatch, task, work, script)
    capsys.readouterr()
    changed = _changed_provenance(_session(work)["execution_provenance"], "lock")
    monkeypatch.setattr(provenance, "capture_execution_provenance", lambda: copy.deepcopy(changed))
    before, task_before = _tree(work), _tree(task)

    def forbidden_store(*_args: Any, **_kwargs: Any) -> Any:
        pytest.fail("CLI opened the offset ledger before rejecting provenance")

    monkeypatch.setattr(model_calls, "ModelCallStore", forbidden_store)
    # A missing script must not hide an incompatible resume behind a loader error.
    script.unlink()
    with pytest.raises(SystemExit) as exc:
        invoke(monkeypatch, task, work, script, resume=True)
    assert exc.value.code == 3
    diagnostic = capsys.readouterr().err.lower()
    assert "provenance" in diagnostic or "execution" in diagnostic or "compatib" in diagnostic
    assert _tree(work) == before and _tree(task) == task_before


def test_cli_compatible_mock_resume_reads_offset_without_a_writable_ledger(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    task, work = task_snapshot(tmp_path), tmp_path / "run"
    script = script_file(tmp_path, wire=True, responses=[wire_response(1)])
    invoke(monkeypatch, task, work, script)
    first = json.loads(capsys.readouterr().out)
    actual_store = model_calls.ModelCallStore
    offset_modes: list[bool] = []

    def offset_store(path: Path, *, read_only: bool = False) -> model_calls.ModelCallStore:
        offset_modes.append(read_only)
        assert read_only, "CLI offset read unexpectedly opened a writable ledger"
        return actual_store(path, read_only=read_only)

    monkeypatch.setattr(model_calls, "ModelCallStore", offset_store)
    invoke(monkeypatch, task, work, script, resume=True)
    resumed = json.loads(capsys.readouterr().out)
    assert offset_modes == [True]
    assert resumed["execution_provenance"] == first["execution_provenance"]
    assert resumed["model_calls"] == first["model_calls"] == 1
    assert resumed["model_calls_this_turn"] == resumed["known_tokens_this_turn"] == 0


@pytest.mark.parametrize("command,exit_code", [("resume", 3), ("repair", 1)])
def test_standard_entry_source_initialization_rejection_precedes_cli_and_preserves_saved_work(
        tmp_path: Path, command: str, exit_code: int) -> None:
    repository = Path(__file__).resolve().parents[1]
    host = tmp_path / "host"
    package = host / "src/dfx_agent"
    scripts = host / "scripts"
    package.mkdir(parents=True)
    scripts.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    shutil.copyfile(repository / "src/dfx_agent/provenance.py", package / "provenance.py")
    shutil.copyfile(repository / "scripts/dfx.py", scripts / "dfx.py")
    for name in ("local_agent.ps1", "local.ps1", "local_job.cs"):
        (scripts / name).write_text("synthetic host fixture", encoding="utf-8")
    # A model/CLI boundary sentinel must never load after this source rejection.
    marker = host / "cli-imported.txt"
    (package / "cli.py").write_text(
        "from pathlib import Path\n"
        "Path(__file__).parents[2].joinpath('cli-imported.txt').write_text('loaded')\n"
        "def main():\n"
        "    raise AssertionError('CLI was invoked before source admission')\n", encoding="utf-8")
    (package / "hidden.json").write_text('{"unregistered": true}', encoding="utf-8")
    task, work = tmp_path / "task", tmp_path / "work"
    task.mkdir()
    work.mkdir()
    (task / "input.txt").write_text("unchanged task input", encoding="utf-8")
    (work / "run-state.json").write_text(
        '{"schema_version": 1, "task": "offline", "run_id": "run1", "status": "finished"}\n',
        encoding="utf-8")
    task_before, work_before = _tree(task), _tree(work)
    environment = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONIOENCODING": "utf-8"}
    completed = subprocess.run(
        [sys.executable, "-B", str(scripts / "dfx.py"), command, "--task", str(task),
         "--work", str(work), "--run-id", "run1", "--fixed-script", str(tmp_path / "missing.json")],
        cwd=host, env=environment, capture_output=True, text=True, encoding="utf-8",
        errors="strict", timeout=15, check=False)
    assert completed.returncode == exit_code, completed.stderr
    assert not marker.exists()
    assert _tree(task) == task_before and _tree(work) == work_before
    if command == "resume":
        assert "before execution" in completed.stderr
        assert "Traceback" not in completed.stderr
