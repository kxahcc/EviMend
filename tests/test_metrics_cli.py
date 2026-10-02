"""Managed command handoff for saved-evidence aggregation; no model execution."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from dfx_agent import cli


def arguments(monkeypatch: pytest.MonkeyPatch, root: Path) -> tuple[Path, Path]:
    manifest, output = root / "manifest.json", root / "work" / "aggregate-report.json"
    monkeypatch.setattr(sys, "argv", ["dfx", "aggregate", "--manifest", str(manifest),
                                     "--root", str(root), "--output", str(output)])
    return manifest, output


def test_aggregate_rejects_unmanaged_entry_without_reading_inputs(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    monkeypatch.delenv("DFX_MANAGED_METRICS", raising=False)
    _, output = arguments(monkeypatch, tmp_path)
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 2 and not output.parent.exists()
    assert "managed offline metrics entry point" in capsys.readouterr().err


def test_aggregate_forwards_only_saved_evidence_paths_and_prints_bounded_receipt(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    from dfx_agent import metrics

    monkeypatch.setenv("DFX_MANAGED_METRICS", "offline-v1")
    manifest, output = arguments(monkeypatch, tmp_path)
    received = []

    def writer(manifest_path: Path, root: Path, report_path: Path) -> dict:
        received.append((manifest_path, root, report_path))
        return {"schema_version": 1, "scope": "offline_synthetic", "evaluation_id": "evaluation-1",
                "rows": [{"private_evidence": "A" * 100_000}, {}]}

    monkeypatch.setattr(metrics, "write_evaluation_report", writer)
    cli.main()
    assert received == [(manifest, tmp_path, output)]
    rendered = capsys.readouterr().out
    assert len(rendered) < 1024 and "private_evidence" not in rendered
    assert json.loads(rendered) == {"schema_version": 1, "scope": "offline_synthetic",
                                    "evaluation_id": "evaluation-1", "output": str(output), "row_count": 2}
    assert not output.parent.exists()


def test_aggregate_validation_failure_leaves_no_output(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    from dfx_agent import metrics

    monkeypatch.setenv("DFX_MANAGED_METRICS", "offline-v1")
    _, output = arguments(monkeypatch, tmp_path)

    def reject(*args: object) -> dict:
        raise ValueError("synthetic evidence is incomplete")

    monkeypatch.setattr(metrics, "write_evaluation_report", reject)
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 2 and not output.parent.exists()
    assert "synthetic evidence is incomplete" in capsys.readouterr().err


@pytest.mark.skipif(sys.platform != "win32", reason="Windows managed PowerShell entry point")
def test_managed_metrics_scripts_have_valid_powershell_syntax() -> None:
    shell = shutil.which("pwsh")
    assert shell is not None
    scripts = Path(__file__).resolve().parents[1] / "scripts"
    environment = {**os.environ, "DFX_METRICS_SCRIPT_ROOT": str(scripts)}
    code = """
    foreach ($name in @('local.ps1', 'local_metrics.ps1')) {
        $errors = $null; $tokens = $null
        $null = [System.Management.Automation.Language.Parser]::ParseFile(
            (Join-Path $env:DFX_METRICS_SCRIPT_ROOT $name), [ref]$tokens, [ref]$errors)
        if ($errors.Count -gt 0) { throw ($errors | Out-String) }
    }
    """
    completed = subprocess.run([shell, "-NoProfile", "-NonInteractive", "-Command", code],
                               env=environment, capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=20, check=False)
    assert completed.returncode == 0, completed.stdout + completed.stderr


@pytest.mark.skipif(sys.platform != "win32", reason="Windows managed PowerShell entry point")
@pytest.mark.parametrize(("arguments", "reason"), [
    (["Status", "-Task", "aggregate"], "Evaluation parameters require Run -Task aggregate"),
    (["Run", "-EvaluationManifestPath", "missing.json", "-EvaluationWorkId", "safe"],
     "Evaluation parameters require Run -Task aggregate"),
    (["Run", "-Task", "aggregate", "-EvaluationManifestPath", "missing.json", "-EvaluationWorkId", "safe",
      "-Resume"], "Aggregate does not accept test, offline or resume parameters"),
    (["Run", "-Task", "aggregate", "-EvaluationManifestPath", "missing.json", "-EvaluationWorkId", "../escape"],
     "EvaluationWorkId must be a new safe ID"),
    (["Run", "-Task", "aggregate", "-EvaluationManifestPath", "data_B/README.md", "-EvaluationWorkId", "safe"],
     "Evaluation manifest path has an unsafe or forbidden component"),
    (["Run", "-Task", "aggregate", "-EvaluationManifestPath", "../outside.json", "-EvaluationWorkId", "safe"],
     "Evaluation manifest path has an unsafe or forbidden component"),
    (["Run", "-Task", "aggregate", "-EvaluationManifestPath", "missing.json", "-EvaluationWorkId", "safe"],
     "Evaluation manifest is missing"),
])
def test_managed_aggregate_rejects_invalid_arguments_before_starting_a_job(arguments: list[str], reason: str) -> None:
    shell = shutil.which("pwsh")
    assert shell is not None
    manager = Path(__file__).resolve().parents[1] / "scripts" / "local.ps1"
    completed = subprocess.run([shell, "-NoProfile", "-NonInteractive", "-File", str(manager), *arguments],
                               capture_output=True, text=True, encoding="utf-8", errors="replace",
                               timeout=20, check=False)
    assert completed.returncode != 0
    assert reason in completed.stderr
    assert not completed.stdout.strip()
