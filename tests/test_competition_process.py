"""Synthetic completion survives a new process using the saved product profile."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from test_competition import config

REPO = Path(__file__).resolve().parents[1]
ENTRY = """
import json
import sys
from pathlib import Path
from dfx_agent.competition import run_competition
work, profile, repo, mode = map(Path, sys.argv[1:])
result = run_competition(work.parent / "unused", work, "fresh-process",
                         profile, repo, resume=str(mode) == "resume")
print(json.dumps(result))
"""


def test_completed_product_profile_resume_in_fresh_process(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DFX_MANAGED_COMPETITION", "local-v1")
    work = tmp_path / "run"
    profile = config(tmp_path, max_calls=15)
    before = None
    checks = None
    for mode, seed in (("new", "1"), ("resume", "7")):
        result = subprocess.run(
            [sys.executable, "-c", ENTRY, str(work), str(profile), str(REPO), mode],
            capture_output=True, text=True, timeout=120, check=False,
            env={**os.environ, "PYTHONHASHSEED": seed},
        )
        assert result.returncode == 0, result.stderr
        value = json.loads(result.stdout)
        assert value["status"] == "synthetic_checked" and value["model_calls"] == 15
        assert value["accepted"] is False and value["application_verification"] == "not_run"
        report = json.loads((work / "competition-report.json").read_bytes())
        saved_checks = {path.name: path.read_bytes() for path in (work / "checks").glob("*.json")}
        if mode == "new":
            for decision in report["decisions"]:
                capabilities = decision["request"]["plan"]["available_capabilities"]
                assert capabilities == sorted(capabilities)
            before, checks = report, saved_checks
            profile = work / "product-config.json"
        else:
            assert report == before
            assert saved_checks == checks
