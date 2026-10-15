"""The public CLI supports analysis and declared local checks without a device."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest
from test_product_cli import _observation, _Process, _reject_external_actions

from dfx_agent import product_cli as cli
from dfx_agent.local_execution import STAGES, LocalCheckConfig
from dfx_agent.source_profile import load_source_profile, source_preflight
from dfx_agent.task import digest


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / "configs").mkdir()
    (tmp_path / "src").mkdir()
    (tmp_path / ".dfx-local/work/competition-local").mkdir(parents=True)
    (tmp_path / "configs/model.local.json").write_text(json.dumps({
        "provider": "deepseek", "model": "test-local-source-model", "api_key": "private-key-do-not-export",
        "budget": {"max_calls": 9, "max_seconds": 120, "max_tool_calls": 25}}), encoding="utf-8")
    monkeypatch.setattr(cli, "_workspace", lambda _: tmp_path)
    return tmp_path


@pytest.fixture
def validation(workspace: Path) -> Path:
    project = workspace / "local-source"
    project.mkdir()
    (project / "main.ets").write_text("export function value(): number { return 1; }\n", encoding="utf-8")
    oracle = workspace / "approved-source-check.py"
    oracle.write_text("# independent host check; preflight never executes this file\n", encoding="utf-8")
    sha256 = digest(oracle.read_bytes())
    value = {"mode": "source", "project_root": str(project), "validation_scope": "local buffer boundary replay",
             "mocked_interfaces": ["@ohos.file.fs"],
             "commands": {stage: {"argv": [sys.executable, str(oracle), stage], "oracle_sha256": sha256}
                          for stage in STAGES},
             "independent_oracles": {stage: {"path": str(oracle), "sha256": sha256} for stage in STAGES},
             "baseline_target_exit_codes": [7], "baseline_fault_pattern": "ObservedOriginalFault"}
    path = workspace / "configs/source.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def test_configure_defaults_to_analysis_without_an_application_or_device(
        workspace: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    _reject_external_actions(monkeypatch)
    before = (workspace / "configs/model.local.json").read_bytes()
    assert cli.main(["--output-format", "json", "configure", "--save", "configs/analysis.json"]) == 0
    receipt = json.loads(capsys.readouterr().out)
    raw = (workspace / "configs/analysis.json").read_text(encoding="utf-8")
    profile = json.loads(raw)
    assert profile["execution"] is None and profile["model_mode"] == "cloud"
    assert receipt["verification_scope"] == "analysis_only"
    assert receipt["validation_prerequisites"] == "not_configured"
    assert receipt["application_prerequisites"] == "not_requested"
    assert receipt["model_verification"] == "not_run" and receipt["experiments_enabled"] is False
    assert profile["max_calls"] == 9 and profile["max_tool_calls"] == 25
    assert "private-key-do-not-export" not in raw + json.dumps(receipt)
    assert (workspace / "configs/model.local.json").read_bytes() == before


def test_environment_key_mode_also_supports_analysis_only(
        workspace: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    _reject_external_actions(monkeypatch)
    assert cli.main(["--output-format", "json", "configure", "--provider", "deepseek", "--model", "legacy-model",
                     "--key-env", "DFX_TEST_PRIVATE_KEY", "--save", "configs/env-analysis.json"]) == 0
    receipt = json.loads(capsys.readouterr().out)
    profile = json.loads((workspace / "configs/env-analysis.json").read_bytes())
    assert profile["execution"] is None
    assert profile["provider"] == {"endpoint": "https://api.deepseek.com/chat/completions",
                                   "credential_env": "DFX_TEST_PRIVATE_KEY"}
    assert receipt["credential_source"] == "environment"


def test_source_configure_and_doctor_need_no_sdk_device_signing_or_harmony_project_files(
        workspace: Path, validation: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    _reject_external_actions(monkeypatch)
    before = {path: path.read_bytes() for path in workspace.rglob("*") if path.is_file()}
    assert cli.main(["--output-format", "json", "doctor", "--validation", "configs/source.json"]) == 0
    observed = json.loads(capsys.readouterr().out)
    assert observed["status"] == observed["model"]["status"] == "configured"
    assert observed["validation_scope"] == "local buffer boundary replay"
    assert observed["mocked_interfaces"] == ["@ohos.file.fs"]
    assert observed["observations"] == {"source_execution": "not_probed", "source_verification": "not_run",
                                         "application_verification": "not_run"}
    assert {path: path.read_bytes() for path in workspace.rglob("*") if path.is_file()} == before
    assert cli.main(["--output-format", "json", "configure", "--validation", "configs/source.json",
                     "--save", "configs/source-run.json"]) == 0
    receipt = json.loads(capsys.readouterr().out)
    profile = json.loads((workspace / "configs/source-run.json").read_bytes())
    assert receipt["verification_scope"] == "source_execution"
    assert receipt["validation_prerequisites"] == "configured"
    assert receipt["application_prerequisites"] == "not_requested"
    expected = load_source_profile(validation)
    assert profile["execution"]["mode"] == "source"
    assert profile["execution"]["validation_scope"] == expected.validation_scope
    assert profile["execution"]["mocked_interfaces"] == expected.mocked_interfaces
    assert profile["execution"]["independent_oracles"] == {
        stage: oracle.model_dump() for stage, oracle in (expected.independent_oracles or {}).items()}


@pytest.mark.parametrize("change,expected", [
    ("oracle_changed", "oracle_unavailable"), ("oracle_inside", "oracle_inside_project"),
    ("relative_executable", "absolute_path_required"), ("shell_wrapper", "native_executable_required"),
])
def test_source_preflight_rejects_invalid_local_prerequisites_without_running_them(
        workspace: Path, validation: Path, monkeypatch: pytest.MonkeyPatch, change: str, expected: str) -> None:
    _reject_external_actions(monkeypatch)
    value = json.loads(validation.read_bytes())
    if change == "oracle_changed":
        Path(value["independent_oracles"]["target"]["path"]).write_text("changed oracle", encoding="utf-8")
    elif change == "oracle_inside":
        oracle = Path(value["project_root"]) / "check.py"
        oracle.write_bytes(b"host file moved inside writable project")
        sha256 = digest(oracle.read_bytes())
        value["independent_oracles"]["target"].update(path=str(oracle), sha256=sha256)
        value["commands"]["target"]["oracle_sha256"] = sha256
    elif change == "relative_executable":
        value["commands"]["target"]["argv"][0] = "node.exe"
    else:
        wrapper = workspace / "wrapper.cmd"
        wrapper.write_text("@echo not invoked", encoding="utf-8")
        value["commands"]["target"]["argv"][0] = str(wrapper)
    result = source_preflight(LocalCheckConfig.model_validate(value))
    assert result["status"] == "blocked"
    assert expected in {row["code"] for row in result["issues"]}
    assert result["observations"]["source_verification"] == "not_run"


def test_source_profile_does_not_accept_synthetic_declarations(validation: Path) -> None:
    value = json.loads(validation.read_bytes())
    value["mode"] = "synthetic"
    validation.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(ValueError, match="source-mode"):
        load_source_profile(validation)


def test_source_doctor_can_run_without_private_model_file_for_environment_key_users(
        workspace: Path, validation: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    _reject_external_actions(monkeypatch)
    (workspace / "configs/model.local.json").unlink()
    monkeypatch.setenv("DFX_TEST_PRIVATE_KEY", "environment-key-never-send")
    assert cli.main(["--output-format", "json", "configure", "--provider", "deepseek", "--model", "legacy-model",
                     "--key-env", "DFX_TEST_PRIVATE_KEY", "--validation", "configs/source.json",
                     "--save", "configs/env-source.json"]) == 0
    capsys.readouterr()
    assert cli.main(["--output-format", "json", "doctor", "--validation", "configs/source.json"]) == 0
    raw = capsys.readouterr().out
    result = json.loads(raw)
    assert result["status"] == "configured" and "model" not in result
    assert result["observations"]["source_verification"] == "not_run"
    assert "environment-key-never-send" not in raw
    assert not (workspace / "configs/model.local.json").exists()


def test_validation_and_optional_application_are_mutually_exclusive() -> None:
    for arguments in (["configure", "--save", "run.json"], ["doctor"]):
        with pytest.raises(SystemExit) as error:
            cli.parser().parse_args([*arguments, "--validation", "source.json", "--application", "app.json"])
        assert error.value.code == 2


def test_source_project_cannot_include_private_model_configuration(
        workspace: Path, validation: Path, capsys: pytest.CaptureFixture[str]) -> None:
    value = json.loads(validation.read_bytes())
    value["project_root"] = str(workspace)
    validation.write_text(json.dumps(value), encoding="utf-8")
    assert cli.main(["--output-format", "json", "configure", "--validation", "configs/source.json",
                     "--save", "configs/rejected.json"]) == 1
    assert not (workspace / "configs/rejected.json").exists()
    assert "private-key-do-not-export" not in capsys.readouterr().out


def test_initializing_private_model_cannot_also_select_validation(workspace: Path) -> None:
    with pytest.raises(SystemExit) as error:
        cli.main(["configure", "--init-model", "--validation", "configs/source.json"])
    assert error.value.code == 2


def _report(workspace: Path, **overrides: Any) -> None:
    value = {"schema_version": 4, "run_id": "local", "status": "source_verified", "accepted": True,
             "application_verification": "not_run", "source_verification": "pass",
             "verification_level": "source_execution", "attempts": [{}], "cost": {},
             "validation_scope": "local buffer boundary replay", "mocked_interfaces": ["@ohos.file.fs"]}
    (workspace / ".dfx-local/work/competition-local/competition-report.json").write_text(
        json.dumps(value | overrides), encoding="utf-8")


def test_cli_summary_keeps_source_acceptance_distinct_from_application_verification(workspace: Path) -> None:
    _report(workspace)
    result = cli._summary(workspace, "local")
    assert result["accepted"] is True and result["source_verification"] == "pass"
    assert result["application_verification"] == "not_run"
    assert result["verification_level"] == "source_execution"
    assert result["validation_scope"] == "local buffer boundary replay"
    assert result["mocked_interfaces"] == ["@ohos.file.fs"]


@pytest.mark.parametrize("overrides", [{"application_verification": "pass"}, {"source_verification": "not_run"},
                                        {"verification_level": "application_execution"}, {"accepted": False}])
def test_cli_summary_rejects_contradictory_source_success(workspace: Path, overrides: dict[str, Any]) -> None:
    _report(workspace, **overrides)
    with pytest.raises(ValueError, match="summary fields"):
        cli._summary(workspace, "local")


@pytest.mark.parametrize("status", ["analysis_completed", "patch_proposed"])
def test_analysis_completion_has_a_successful_exit_without_claiming_a_verified_patch(
        workspace: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], status: str) -> None:
    _report(workspace, status=status, accepted=False, source_verification="not_run", verification_level="not_run")
    process = _Process({"status": "finished"})
    monkeypatch.setattr(cli, "_backend", lambda *args: ["fixed-manager"])
    monkeypatch.setattr(cli.subprocess, "Popen", lambda *args, **options: process)
    monkeypatch.setattr(cli, "_active", lambda _: None)
    monkeypatch.setattr(cli, "read_progress", lambda *args, **options: _observation([]))
    monkeypatch.setattr(cli.time, "sleep", lambda _: None)
    assert cli._run(workspace, "task", "configs/analysis.json", "local", False, "json") == 0
    result = json.loads(capsys.readouterr().out)
    assert result["accepted"] is False and result["status"] == status
    assert result["source_verification"] == result["application_verification"] == "not_run"
