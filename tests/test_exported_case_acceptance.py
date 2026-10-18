"""Opt-in local artifact acceptance; applies patches, never executes application source."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

import pytest

from dfx_agent.evidence_io import local_evidence_path
from dfx_agent.task import verify_task
from dfx_agent.workspace import _copy_tree, _files

ROOT = Path(__file__).resolve().parents[1]
INDEX = ROOT / "runs/exports/cases/index.json"
ENABLED = os.environ.get("DFX_REAL_EXPORT_ACCEPTANCE") == "1"
ENTRIES = ([row for row in json.loads(INDEX.read_bytes())["cases"] if row.get("run_id")]
           if ENABLED and INDEX.is_file() else [])


def _hashes(root: Path) -> dict[str, str]:
    return {name: hashlib.sha256(local_evidence_path(path).read_bytes()).hexdigest()
            for name, path in _files(root).items() if not name.startswith(".git/")}


@pytest.mark.skipif(not ENABLED, reason="explicit local exported-case acceptance is not enabled")
@pytest.mark.parametrize("entry", ENTRIES or [{}], ids=lambda row: row.get("case_id", "unavailable"))
def test_published_candidate_manifest_and_git_application(tmp_path: Path, entry: dict) -> None:
    case = entry.get("case_id", entry["task_id"])
    folder = ROOT / "runs/exports/cases" / case
    records = []
    for path in folder.glob("*/summary.json"):
        summary = json.loads(path.read_bytes())
        if (path.parent.name == summary["run_id"] and summary["candidate_id"] and
                summary["status"] in {"patch_proposed", "source_verified", "application_verified"}):
            records.append((datetime.fromisoformat(summary["timing"]["finished_at"]), path.parent, summary))
    assert records, f"No effective published result for {case}"
    _, result, summary = max(records, key=lambda row: row[0])
    assert summary["task_id"] == entry["task_id"] and summary["usage"]["usage_complete"]
    assert summary["model_cost"]["status"] == "estimated"
    for name, metadata in summary["files"].items():
        path = result / name
        assert local_evidence_path(path).resolve().is_relative_to(local_evidence_path(result).resolve()), "manifest path escapes the result"
        with local_evidence_path(path).open("rb") as stream:
            assert hashlib.file_digest(stream, "sha256").hexdigest() == metadata["sha256"], name
        assert local_evidence_path(path).stat().st_size == metadata["bytes"], name
    task = verify_task(result / "input-task")
    assert task["source_snapshot_sha256"] == summary["source_snapshot_sha256"]
    baseline = result / "input-task" / task["source_root"]
    applied = tmp_path / "apply"
    _copy_tree(baseline, applied)
    git = shutil.which("git")
    assert git is not None, "Git is required for local patch acceptance"
    options = [git, "-c", "core.autocrlf=false", "-c", "core.safecrlf=false",
               "-c", "core.longpaths=true", "-C", str(applied)]
    for arguments in (["init", "--quiet"], ["apply", "--check", str(result / "patch.diff")],
                      ["apply", "--whitespace=nowarn", str(result / "patch.diff")]):
        checked = subprocess.run([*options, *arguments], capture_output=True, check=False, timeout=30)
        assert checked.returncode == 0, checked.stderr.decode("utf-8", errors="replace")
    assert _hashes(applied) == _hashes(result / "repaired-source"), "applied bytes differ from frozen candidate"
    verification = json.loads((result / "verification.json").read_bytes())
    assert verification["verification_level"] == summary["verification_level"]
    if summary["status"] == "patch_proposed":
        assert summary["accepted"] is False and summary["verification_level"] == "not_run"
    else:
        assert summary["accepted"] is True
