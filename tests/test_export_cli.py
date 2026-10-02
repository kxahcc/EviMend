"""Managed local export command handoff and argument rejection; no external service."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import BaseModel
from test_exporting import (
    EXPORT_ID,
    OTHER_OWNER_ID,
    OWNER_ID,
    REPOSITORY,
    ExportInputs,
    io_path,
    source_snapshot,
    tree_snapshot,
)
from test_exporting import export_inputs as export_inputs  # noqa: PLC0414
from test_metrics import base_evaluation as base_evaluation  # noqa: PLC0414
from test_metrics import read_json, save_json

from dfx_agent import cli
from dfx_agent.model_calls import ModelCallConflict
from dfx_agent.task import digest

OWNER = "5a9679ea-3d4a-461c-9fbb-fbe9b6d08b2f"
PLAN_ID = "a" * 64


def export_arguments(monkeypatch: pytest.MonkeyPatch, root: Path, *extra: str) -> tuple[Path, Path]:
    manifest = root / "manifest.json"
    plan = root / ".dfx-local" / "state" / "export-plan-export1.json"
    monkeypatch.setattr(sys, "argv", ["dfx", "export", "--manifest", str(manifest), "--root", str(root),
                                     "--export-id", "export1", "--owner-id", OWNER, "--plan", str(plan), *extra])
    return manifest, plan


@pytest.mark.parametrize("command", ["export", "verify-export"])
def test_export_requires_managed_entry_before_reading_any_input(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture, command: str) -> None:
    monkeypatch.delenv("DFX_MANAGED_EXPORT", raising=False)
    if command == "export":
        export_arguments(monkeypatch, tmp_path)
    else:
        monkeypatch.setattr(sys, "argv", ["dfx", command, "--package", str(tmp_path / "missing"), "--owner-id", OWNER])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 2 and not (tmp_path / ".dfx-local").exists()
    assert "managed offline export entry point" in capsys.readouterr().err


def test_export_preview_saves_frozen_plan_and_prints_only_a_short_receipt(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    from dfx_agent import exporting

    monkeypatch.setenv("DFX_MANAGED_EXPORT", "offline-v1")
    manifest, plan_path = export_arguments(monkeypatch, tmp_path)
    calls = []
    plan = {"schema_version": 1, "scope": "offline_synthetic", "export_id": "export1", "owner_id": OWNER,
            "plan_id": PLAN_ID, "estimated_bytes": 1234, "privacy": "private_local",
            "aggregate_sha256": "b" * 64, "inventory": [{"messages": "A" * 100_000}]}

    def preview(manifest_path: Path, root: Path, export_id: str, owner_id: str) -> dict:
        calls.append(("preview", manifest_path, root, export_id, owner_id))
        return plan

    def save(value: dict, path: Path) -> None:
        calls.append(("save", value, path))

    monkeypatch.setattr(exporting, "preview_export", preview)
    monkeypatch.setattr(exporting, "save_export_plan", save)
    cli.main()
    assert calls == [("preview", manifest, tmp_path, "export1", OWNER), ("save", plan, plan_path)]
    rendered = capsys.readouterr().out
    assert len(rendered) < 1024 and "messages" not in rendered and "inventory" not in rendered
    receipt = json.loads(rendered)
    assert receipt["plan_id"] == PLAN_ID and receipt["estimated_bytes"] == 1234
    assert receipt["status"] == "preview" and receipt["output"] == str(tmp_path / "runs/exports/export1")
    assert receipt["owner_id"] == OWNER and receipt["aggregate_sha256"] == "b" * 64
    assert not plan_path.exists()


def test_export_apply_forwards_exact_reviewed_plan_identity(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    from dfx_agent import exporting

    monkeypatch.setenv("DFX_MANAGED_EXPORT", "offline-v1")
    manifest, plan = export_arguments(monkeypatch, tmp_path, "--apply", "--plan-id", PLAN_ID)
    calls = []

    def apply(manifest_path: Path, root: Path, export_id: str, owner_id: str, plan_path: Path, plan_id: str) -> dict:
        calls.append((manifest_path, root, export_id, owner_id, plan_path, plan_id))
        return {"status": "verified", "export_id": "export1", "plan_id": plan_id, "owner_id": OWNER,
                "package_path": str(tmp_path / "runs/exports/export1"), "package_sha256": "c" * 64,
                "aggregate_sha256": "b" * 64, "private_messages": "A" * 100_000}

    monkeypatch.setattr(exporting, "apply_export", apply)
    cli.main()
    assert calls == [(manifest, tmp_path, "export1", OWNER, plan, PLAN_ID)]
    rendered = capsys.readouterr().out
    assert len(rendered) < 1024 and "private_messages" not in rendered
    receipt = json.loads(rendered)
    assert receipt["status"] == "verified" and receipt["package_sha256"] == "c" * 64
    assert receipt["output"] == receipt["package_path"]


def test_export_schema_error_does_not_print_private_input_details(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    from dfx_agent import exporting

    monkeypatch.setenv("DFX_MANAGED_EXPORT", "offline-v1")
    export_arguments(monkeypatch, tmp_path)

    class SyntheticInput(BaseModel):
        count: int

    def reject(*args: object) -> dict:
        SyntheticInput.model_validate({"count": "private-message-content"})
        raise AssertionError("Expected synthetic schema failure")

    monkeypatch.setattr(exporting, "preview_export", reject)
    with pytest.raises(SystemExit) as exc:
        cli.main()
    rendered = capsys.readouterr().err
    assert exc.value.code == 2 and "private input details omitted" in rendered
    assert "private-message-content" not in rendered


@pytest.mark.parametrize("stage", ["preview", "save", "apply", "verify"])
@pytest.mark.parametrize("error_type", [ValueError, RuntimeError, sqlite3.DatabaseError, ModelCallConflict])
def test_export_core_errors_never_print_private_exception_content(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture,
        stage: str, error_type: type[Exception]) -> None:
    from dfx_agent import exporting

    monkeypatch.setenv("DFX_MANAGED_EXPORT", "offline-v1")
    if stage == "verify":
        monkeypatch.setattr(sys, "argv", ["dfx", "verify-export", "--package", str(tmp_path / "package"), "--owner-id", OWNER])
    else:
        extra = ("--apply", "--plan-id", PLAN_ID) if stage == "apply" else ()
        export_arguments(monkeypatch, tmp_path, *extra)

    def fail(*args: object) -> dict:
        raise error_type("private-marker-in-source-and-message")

    if stage == "save":
        monkeypatch.setattr(exporting, "preview_export", lambda *args: {"plan_id": PLAN_ID})
    target = {"preview": "preview_export", "save": "save_export_plan", "apply": "apply_export", "verify": "verify_export"}[stage]
    monkeypatch.setattr(exporting, target, fail)
    with pytest.raises(SystemExit) as exc:
        cli.main()
    captured = capsys.readouterr()
    assert exc.value.code == 2 and "Export evidence rejected" in captured.err
    assert "private-marker-in-source-and-message" not in captured.err + captured.out
    assert not captured.out


@pytest.mark.parametrize(("arguments", "reason"), [
    (("--apply",), "Applying an export requires the reviewed plan ID"),
    (("--plan-id", PLAN_ID), "Plan ID requires --apply"),
])
def test_export_rejects_partial_approval_before_core_execution(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture,
        arguments: tuple[str, ...], reason: str) -> None:
    from dfx_agent import exporting

    monkeypatch.setenv("DFX_MANAGED_EXPORT", "offline-v1")
    export_arguments(monkeypatch, tmp_path, *arguments)

    def unexpected(*args: object) -> dict:
        raise AssertionError("Export core must not run")

    monkeypatch.setattr(exporting, "preview_export", unexpected)
    monkeypatch.setattr(exporting, "apply_export", unexpected)
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 2 and reason in capsys.readouterr().err
    assert not (tmp_path / ".dfx-local").exists()


@pytest.mark.parametrize("owner", [OWNER, "0b8a72af-8c06-49ee-8e43-3a4b32b8f257"])
def test_verify_export_reads_package_and_refuses_owner_mismatch(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture, owner: str) -> None:
    from dfx_agent import exporting

    monkeypatch.setenv("DFX_MANAGED_EXPORT", "offline-v1")
    package = tmp_path / "runs/exports/export1"
    monkeypatch.setattr(sys, "argv", ["dfx", "verify-export", "--package", str(package), "--owner-id", OWNER])
    calls = []

    def verify(path: Path) -> dict:
        calls.append(path)
        return {"owner_id": owner, "status": "verified", "export_id": "export1", "plan_id": PLAN_ID,
                "inventory": [{"message": "A" * 100_000}]}

    monkeypatch.setattr(exporting, "verify_export", verify)
    if owner == OWNER:
        cli.main()
        rendered = capsys.readouterr().out
        assert len(rendered) < 1024 and "inventory" not in rendered and "message" not in rendered
        assert json.loads(rendered)["status"] == "verified"
    else:
        with pytest.raises(SystemExit) as exc:
            cli.main()
        assert exc.value.code == 2 and "owner does not match" in capsys.readouterr().err
    assert calls == [package] and not package.exists()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows managed PowerShell entry point")
def test_managed_export_scripts_have_valid_powershell_syntax() -> None:
    shell = shutil.which("pwsh")
    assert shell is not None
    scripts = Path(__file__).resolve().parents[1] / "scripts"
    environment = {**os.environ, "DFX_EXPORT_SCRIPT_ROOT": str(scripts)}
    code = """
    foreach ($name in @('local.ps1', 'local_export.ps1')) {
        $errors = $null; $tokens = $null
        $null = [System.Management.Automation.Language.Parser]::ParseFile(
            (Join-Path $env:DFX_EXPORT_SCRIPT_ROOT $name), [ref]$tokens, [ref]$errors)
        if ($errors.Count -gt 0) { throw ($errors | Out-String) }
    }
    """
    completed = subprocess.run([shell, "-NoProfile", "-NonInteractive", "-Command", code],
                               env=environment, capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=20, check=False)
    assert completed.returncode == 0, completed.stdout + completed.stderr


@pytest.mark.skipif(sys.platform != "win32", reason="Windows managed PowerShell entry point")
@pytest.mark.parametrize(("arguments", "reason"), [
    (["Status", "-ExportId", "safe"], "Export parameters require the Export action"),
    (["Status", "-Verify"], "Export parameters require the Export action"),
    (["Export", "-ExportId", "safe", "-Task", "synthetic"], "Export does not accept task"),
    (["Export", "-ExportId", "safe", "-EvaluationWorkId", "safe"], "Export does not accept task"),
    (["Export", "-ExportId", "safe", "-Resume"], "Export does not accept task"),
    (["Export", "-ExportId", "safe", "-OfflineTaskPath", "task"], "Export does not accept task"),
    (["Export", "-ExportId", "../escape"], "ExportId must be a safe ID"),
    (["Export", "-ExportId", "safe", "-Apply"], "Applying an export requires the reviewed lowercase SHA256 PlanId"),
    (["Export", "-ExportId", "safe", "-PlanId", PLAN_ID], "Export PlanId requires -Apply"),
    (["Export", "-ExportId", "safe", "-Verify", "-Apply"], "Export verification accepts only ExportId"),
    (["Export", "-ExportId", "safe", "-Verify", "-EvaluationManifestPath", "missing.json"],
     "Export verification accepts only ExportId"),
    (["Export", "-ExportId", "safe", "-EvaluationManifestPath", "data_B/README.md"],
     "Evaluation manifest path has an unsafe or forbidden component"),
    (["Export", "-ExportId", "safe", "-EvaluationManifestPath", "missing.json"], "Evaluation manifest is missing"),
])
def test_managed_export_rejects_invalid_arguments_before_starting_a_job(arguments: list[str], reason: str) -> None:
    shell = shutil.which("pwsh")
    assert shell is not None
    manager = Path(__file__).resolve().parents[1] / "scripts" / "local.ps1"
    completed = subprocess.run([shell, "-NoProfile", "-NonInteractive", "-File", str(manager), *arguments],
                               capture_output=True, text=True, encoding="utf-8", errors="replace",
                               timeout=20, check=False)
    assert completed.returncode != 0
    assert reason in completed.stderr
    assert not completed.stdout.strip()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows managed PowerShell entry point")
def test_managed_verify_reads_old_package_after_a_new_prepare_identity(export_inputs: ExportInputs) -> None:
    """A package's origin ID does not grant authority over the new managed runtime."""
    shell = shutil.which("pwsh")
    assert shell is not None
    inputs = export_inputs
    original_receipt = inputs.apply(inputs.freeze())
    owner_path = inputs.root / ".dfx-local/owner.json"
    new_owner = read_json(owner_path)
    assert new_owner["project_instance_id"] == OWNER_ID
    new_owner["project_instance_id"] = OTHER_OWNER_ID
    save_json(owner_path, new_owner)

    # Reuse installed dependencies read-only; copy only the venv launcher and metadata.
    fixture_scripts = inputs.root / "scripts"
    io_path(fixture_scripts).mkdir(parents=True)
    for name in ("local.ps1", "local_job.cs", "local_export.ps1", "dfx.py"):
        shutil.copyfile(io_path(REPOSITORY / "scripts" / name), io_path(fixture_scripts / name))
    for relative in ("configs/local-runtime.json", "uv.lock", "pyproject.toml"):
        shutil.copyfile(io_path(REPOSITORY / relative), io_path(inputs.root / relative))
    existing_environment = REPOSITORY / ".dfx-local/env"
    fixture_environment = inputs.root / ".dfx-local/env"
    fixture_python = fixture_environment / "Scripts/python.exe"
    site_packages = fixture_environment / "Lib/site-packages"
    io_path(fixture_python.parent).mkdir(parents=True)
    io_path(site_packages).mkdir(parents=True)
    shutil.copyfile(io_path(existing_environment / "Scripts/python.exe"), io_path(fixture_python))
    shutil.copyfile(io_path(existing_environment / "pyvenv.cfg"), io_path(fixture_environment / "pyvenv.cfg"))
    io_path(site_packages / "dfx_fixture_readonly.pth").write_text(
        f"{REPOSITORY / 'src'}\n{existing_environment / 'Lib/site-packages'}\n", encoding="utf-8")
    environment = read_json(REPOSITORY / ".dfx-local/state/environment.json")
    environment.update({"environment_python": str(fixture_python),
                        "lock_sha256": digest(io_path(inputs.root / "uv.lock").read_bytes()).upper(),
                        "pyproject_sha256": digest(io_path(inputs.root / "pyproject.toml").read_bytes()).upper()})
    save_json(inputs.root / ".dfx-local/state/environment.json", environment)

    before_source = source_snapshot(inputs)
    before_package = tree_snapshot(inputs.target)
    owner_bytes = io_path(owner_path).read_bytes()
    # Pair the capture decoder with an explicit encoding, independent of the Windows code page.
    command = """
    [Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
    $OutputEncoding = [Console]::OutputEncoding
    & $env:DFX_FIXTURE_EXPORT_MANAGER Export -ExportId $env:DFX_FIXTURE_EXPORT_ID -Verify
    exit $LASTEXITCODE
    """
    process_environment = {**os.environ, "DFX_FIXTURE_EXPORT_MANAGER": str(fixture_scripts / "local.ps1"),
                           "DFX_FIXTURE_EXPORT_ID": EXPORT_ID}
    completed = subprocess.run(
        [shell, "-NoProfile", "-NonInteractive", "-Command", command], env=process_environment,
        capture_output=True, text=True, encoding="utf-8", errors="strict", timeout=90, check=False)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    job_record = json.loads(completed.stdout)
    assert job_record["task"] == "export" and job_record["status"] == "finished"
    assert job_record["reason"] == "exited_zero"
    assert not (inputs.root / ".dfx-local/state/active.json").exists()
    receipt = json.loads(io_path(Path(job_record["log_path"])).read_text(encoding="utf-8-sig"))
    assert receipt["status"] == "verified" and receipt["owner_id"] == OWNER_ID
    assert receipt["aggregate_sha256"] == original_receipt["aggregate_sha256"]
    assert receipt["package_sha256"] == original_receipt["package_sha256"]
    assert Path(receipt["package_path"]) == inputs.target
    assert source_snapshot(inputs) == before_source and tree_snapshot(inputs.target) == before_package
    assert io_path(owner_path).read_bytes() == owner_bytes
    assert read_json(owner_path)["project_instance_id"] == OTHER_OWNER_ID
