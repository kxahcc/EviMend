"""Verify every scheduled pilot artifact, including negative and no-patch outcomes."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from test_exported_case_acceptance import _hashes

from dfx_agent.competition import verify_private_delivery
from dfx_agent.evidence_io import local_evidence_path
from dfx_agent.task import verify_task
from dfx_agent.workspace import _copy_tree

ROOT = Path(__file__).resolve().parents[1]
INDEX = ROOT / "runs/exports/evaluation-runs.json"
ENABLED = os.environ.get("DFX_PILOT_EXPORT_ACCEPTANCE") == "1"
ENTRIES = json.loads(INDEX.read_bytes())["results"] if ENABLED and INDEX.exists() else []


@pytest.mark.skipif(not ENABLED, reason="local pilot artifact acceptance not requested")
def test_current_navigation_and_valid_population() -> None:
    exports = ROOT / "runs/exports"
    assert not (exports / "phase2").exists()
    expected = {row["run_id"] for row in ENTRIES}
    assert len(expected) == len(ENTRIES)
    model = json.loads((exports / "robustness/model-index.json").read_bytes())["results"]
    comparisons = json.loads((exports / "ablations/index.json").read_bytes())["results"]
    assert {row["run_id"] for row in model + comparisons} == expected
    for entry in ENTRIES:
        assert entry["usage"]["usage_complete"] and not entry["stopped_reason"]
    pages = [exports / "README.md", exports / "cases/README.md",
             exports / "robustness/README.md", exports / "ablations/README.md"]
    pages.extend(exports / row["result"] / "RESULT.md" for row in ENTRIES)
    for page in pages:
        for relative in re.findall(r"\]\(([^)]+)\)", page.read_text(encoding="utf-8")):
            if "://" in relative or relative.startswith("#"):
                continue
            assert (page.parent / relative.split("#")[0]).exists(), (page, relative)


@pytest.mark.skipif(not ENABLED, reason="local pilot artifact acceptance not requested")
@pytest.mark.parametrize("entry", ENTRIES or [{}], ids=lambda row: row.get("run_id", "unavailable"))
def test_pilot_manifest_verification_and_actual_patch_application(tmp_path: Path, entry: dict) -> None:
    folder = ROOT / "runs/exports" / entry["result"]
    summary = json.loads((folder / "summary.json").read_bytes())
    assert summary["run_id"] == entry["run_id"] and summary["report_sha256"] == entry["report_sha256"]
    assert summary["usage"]["usage_complete"] is True
    for relative, metadata in summary["files"].items():
        path = local_evidence_path(folder / relative)
        assert path.resolve().is_relative_to(local_evidence_path(folder).resolve())
        assert path.stat().st_size == metadata["bytes"]
        with path.open("rb") as stream:
            assert hashlib.file_digest(stream, "sha256").hexdigest() == metadata["sha256"]
    verify_private_delivery(folder / "evidence.zip", 268435456)
    task = verify_task(folder / "input-task")
    assert task["source_snapshot_sha256"] == summary["source_snapshot_sha256"]
    verification = json.loads((folder / "verification.json").read_bytes())
    if summary["candidate_id"] is None:
        assert not verification["attempts"] and summary["metrics"]["candidate_count"] == 0
        assert summary["verification_level"] == "not_run" and summary["accepted"] is False
        return
    baseline = folder / "input-task" / task["source_root"]
    applied = tmp_path / "applied"
    _copy_tree(baseline, applied)
    git = shutil.which("git")
    assert git is not None
    command = [git, "-c", "core.autocrlf=false", "-c", "core.safecrlf=false", "-c", "core.longpaths=true", "-C", str(applied)]
    for arguments in (["init", "--quiet"], ["apply", "--check", str(folder / "patch.diff")],
                      ["apply", "--whitespace=nowarn", str(folder / "patch.diff")]):
        result = subprocess.run([*command, *arguments], capture_output=True, check=False, timeout=30)
        assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
    assert _hashes(applied) == _hashes(folder / "repaired-source")
    attempt = verification["attempts"][-1]
    assert attempt["candidate"]["candidate_id"] == summary["candidate_id"]
    assert attempt["execution"]["baseline"]["fault_reproduced"] is True
    if summary["accepted"]:
        assert attempt["check"]["status"] == "pass" and verification["source_verification"] == "pass"
        assert [s["stage"] for s in attempt["execution"]["stages"]] == ["build", "target", "behavior", "regression"]
        assert all(s["exit_code"] == 0 and s["source_binding_verified"] and s["oracle_binding_verified"]
                   for s in attempt["execution"]["stages"])
    else:
        assert attempt["check"]["status"] != "pass", "negative result must retain the actual failed verification"
