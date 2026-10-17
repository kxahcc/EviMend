"""Verification discovery and saved counterexamples are read-only, candidate-bound tools."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from test_source_execution import managed as managed  # noqa: PLC0414
from test_source_execution import source_config, workspace

from dfx_agent.local_execution import LocalApplicationExecutor
from dfx_agent.task import digest
from dfx_agent.verification import AnalysisOnlyExecutor, CheckLedger, _atomic_json


@pytest.mark.parametrize("patch,status", [("return n + 1", "pass"), ("return 3", "fail")])
def test_saved_real_execution_is_inspected_without_relaunch(
        tmp_path: Path, managed: Path, monkeypatch: pytest.MonkeyPatch, patch: str, status: str) -> None:
    work, source, project = workspace(tmp_path, patch=patch)
    executor = LocalApplicationExecutor(source_config(tmp_path, project), work, managed_root=managed)
    ledger = CheckLedger(work, executor)
    available = ledger.inspect()
    assert available["execution_configured"] is True and available["autonomous_test_generation"] is False
    assert available["available_stages"] == ["build", "target", "behavior", "regression"]
    assert ledger.inspect(source.parent.name)["saved_check"] == {"state": "not_run"}
    checked = ledger.run(source.parent.name)
    assert checked.status == status
    assert executor.root is not None
    execution = executor.root / checked.idempotency_key / "execution.json"
    before = execution.read_bytes(), execution.stat().st_mtime_ns

    def forbidden(**_kwargs: object) -> None:
        pytest.fail("inspection must not execute a check")

    monkeypatch.setattr(executor, "check", forbidden)
    monkeypatch.setattr(executor, "_stage", forbidden)
    result = ledger.inspect(source.parent.name)["saved_check"]
    assert isinstance(result, dict) and result["status"] == status
    assert result["verification_level"] == "source_execution" and result["baseline_fault_reproduced"] is True
    if status == "fail":
        failed = result["failed_stage"]
        assert failed["role"] == "candidate" and failed["stage"] == "behavior"
        assert "normal input behavior changed" in failed["output"]["stderr"]["text"]
    else:
        assert "failed_stage" not in result, "the expected original target failure is not a candidate failure"
    assert (execution.read_bytes(), execution.stat().st_mtime_ns) == before
    assert CheckLedger(work, executor, read_only=True).inspect(source.parent.name) == ledger.inspect(source.parent.name)
    with pytest.raises(ValueError):
        ledger.inspect(digest(b"unknown candidate"))


def test_analysis_and_pending_inspection_never_upgrade_to_pass(tmp_path: Path) -> None:
    work, source, _ = workspace(tmp_path)
    ledger = CheckLedger(work, AnalysisOnlyExecutor())
    value = ledger.inspect(source.parent.name)
    assert value["execution_configured"] is False and value["verification_level"] == "not_run"
    assert value["saved_check"] == {"state": "not_run"}
    key, path = ledger._identity(source.parent.name, "offline")
    _atomic_json(path, {"state": "pending", "idempotency_key": key, "candidate_id": source.parent.name,
        "check_id": "offline", "protocol_sha256": ledger.executor.protocol_sha256})
    before = path.read_bytes()
    assert ledger.inspect(source.parent.name)["saved_check"] == {
        "state": "pending", "status": "inconclusive", "relaunch": False}
    assert path.read_bytes() == before and json.loads(before)["state"] == "pending"
