"""Run the existing local cleanup counterexamples inside the managed test Job."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.skipif(os.name != "nt", reason="Windows local manager contract")
def test_cleanup_requires_explicit_discard_and_keeps_other_owned_resources() -> None:
    powershell = shutil.which("pwsh")
    if powershell is None:
        pytest.skip("PowerShell 7 is unavailable")
    script = Path(__file__).resolve().parents[1] / "scripts/test_local_runtime.ps1"
    result = subprocess.run([powershell, "-NoProfile", "-File", str(script)], capture_output=True,
                            text=True, encoding="utf-8", errors="replace", timeout=150, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "W01 path and cleanup assertions" in result.stdout
