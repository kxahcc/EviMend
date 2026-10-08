"""Run startup ordering and existing process counterexamples in the managed Job."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.skipif(os.name != "nt", reason="Windows local manager contract")
def test_lease_publication_precedes_worker_and_failure_stops_processes() -> None:
    repo = Path(__file__).resolve().parents[1]
    lease_path = repo / ".dfx-local/state/active.json"
    if not lease_path.exists():
        pytest.skip("process checks require the managed test Job")
    lease = json.loads(lease_path.read_text(encoding="utf-8-sig"))
    assert lease["task"] == "test", "run this test through the managed test entry"
    powershell = shutil.which("pwsh")
    if powershell is None:
        pytest.skip("PowerShell 7 is unavailable")
    script = repo / "scripts/test_local_process.ps1"
    result = subprocess.run(
        [powershell, "-NoProfile", "-NonInteractive", "-File", str(script)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=240, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "W02 bounded-process assertions" in result.stdout
