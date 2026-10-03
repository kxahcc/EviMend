"""Saved execution provenance is report evidence, not a reader-host observation."""

from __future__ import annotations

import copy
import json
import shutil
from pathlib import Path
from typing import Any

import pytest
from test_agent_loop import response
from test_readonly_evidence import tree_bytes
from test_reporting import run_case

from dfx_agent import provenance
from dfx_agent.actions import ToolActionStore
from dfx_agent.model_calls import ModelCallStore, canonical, sha256
from dfx_agent.reporting import build_completion, read_completion
from dfx_agent.verification import CheckLedger, ReplayOnlyExecutor
from dfx_agent.workspace import CandidateWorkspace


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _save(path: Path, value: dict[str, Any]) -> None:
    path.write_text(json.dumps(value), encoding="utf-8")


def _open(root: Path):
    work = root / "run"
    workspace = CandidateWorkspace.reopen(root / "input", work, "run1")
    session = _load(work / "session.json")
    ledger = CheckLedger(workspace, ReplayOnlyExecutor(session["protocol_sha256"]), read_only=True)
    return (workspace, ledger, ModelCallStore(work / "model-calls.sqlite", read_only=True),
            ToolActionStore(work / "tool-actions.sqlite", read_only=True))


@pytest.fixture(scope="module")
def captured_run(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("report-provenance")
    run_case(root, [response(1)])
    return root


def _clone(source: Path, tmp_path: Path) -> Path:
    root = tmp_path / "evidence"
    shutil.copytree(source, root)
    return root


def legacy_report(root: Path) -> dict[str, Any]:
    """Make an explicit synthetic historical fixture, never a production migration."""
    session_path = root / "run/session.json"
    session = _load(session_path)
    session["schema_version"] = 8
    session.pop("execution_provenance", None)
    session["context_profile"].pop("host_working_context", None)
    session["context_profile"].pop("working_context_prompt_sha256", None)
    _save(session_path, session)
    case = _open(root)
    report = _load(root / "run/agent-report.json")
    value = build_completion(*case, stopped_reason=report["stopped_reason"])
    assert value["schema_version"] == 2
    report.update(schema_version=5, completion=value)
    report.pop("execution_provenance", None)
    report.pop("working_context_plans", None)
    _save(root / "run/agent-report.json", report)
    return report


def _forbid_host_capture(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*_args, **_kwargs):
        raise AssertionError("historical report must not observe or compare the reader host")

    for name in ("capture_execution_provenance", "ensure_execution_compatible", "verify_host_baseline"):
        monkeypatch.setattr(provenance, name, forbidden)


def test_new_report_preserves_saved_provenance_without_current_host_calls(
    captured_run: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = _load(captured_run / "run/agent-report.json")
    session = _load(captured_run / "run/session.json")
    assert (session["schema_version"], report["schema_version"], report["completion"]["schema_version"]) == (10, 7, 4)
    before = tree_bytes(captured_run)
    _forbid_host_capture(monkeypatch)
    value = read_completion(*_open(captured_run))
    assert value == report["completion"]
    assert value["execution_provenance"] == session["execution_provenance"] == report["execution_provenance"]
    assert "execution_provenance" in value["report_validation"]["verified_top_level_fields"]
    assert tree_bytes(captured_run) == before


def test_legacy_report_has_exact_old_shape_and_no_backfill(
    captured_run: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _clone(captured_run, tmp_path)
    report = legacy_report(root)
    expected = canonical(report["completion"])
    before = tree_bytes(root)
    _forbid_host_capture(monkeypatch)
    value = read_completion(*_open(root))
    assert canonical(value) == expected
    assert "execution_provenance" not in value
    assert "execution_provenance" not in value["report_validation"]["verified_top_level_fields"]
    assert tree_bytes(root) == before


@pytest.mark.parametrize("tamper", ["missing_top", "changed_top", "changed_completion"])
def test_new_report_rejects_provenance_tamper_even_when_summary_is_rehashed(
    captured_run: Path, tmp_path: Path, tamper: str,
) -> None:
    root = _clone(captured_run, tmp_path)
    report_path = root / "run/agent-report.json"
    report = _load(report_path)
    if tamper == "missing_top":
        report.pop("execution_provenance")
    elif tamper == "changed_top":
        report["execution_provenance"] = copy.deepcopy(report["execution_provenance"])
        report["execution_provenance"]["provenance_sha256"] = "0" * 64
    else:
        value = report["completion"]
        value["execution_provenance"]["provenance_sha256"] = "0" * 64
        value["summary_sha256"] = sha256(canonical({key: item for key, item in value.items()
                                                   if key != "summary_sha256"}))
    _save(report_path, report)
    with pytest.raises(ValueError, match="differs|differ"):
        read_completion(*_open(root))


@pytest.mark.parametrize("report_schema,session_schema", [(5, 10), (6, 10), (7, 8)])
def test_report_and_session_generations_must_match(
    captured_run: Path, tmp_path: Path, report_schema: int, session_schema: int,
) -> None:
    root = _clone(captured_run, tmp_path)
    if session_schema == 8:
        legacy_report(root)
    report_path = root / "run/agent-report.json"
    report = _load(report_path)
    report["schema_version"] = report_schema
    _save(report_path, report)
    with pytest.raises(ValueError, match="report and session schema differ"):
        read_completion(*_open(root))


@pytest.mark.parametrize("change", ["new_missing", "legacy_injected", "invalid_hash"])
def test_session_provenance_cannot_be_missing_injected_or_changed(
    captured_run: Path, tmp_path: Path, change: str,
) -> None:
    root = _clone(captured_run, tmp_path)
    session_path = root / "run/session.json"
    session = _load(session_path)
    if change == "new_missing":
        session.pop("execution_provenance")
    elif change == "legacy_injected":
        session["schema_version"] = 8
    else:
        session["execution_provenance"]["compatibility_sha256"] = "0" * 64
    _save(session_path, session)
    with pytest.raises(ValueError):
        read_completion(*_open(root))


def test_legacy_top_level_provenance_claim_is_rejected(captured_run: Path, tmp_path: Path) -> None:
    root = _clone(captured_run, tmp_path)
    original = _load(root / "run/agent-report.json")["execution_provenance"]
    report = legacy_report(root)
    report["execution_provenance"] = original
    _save(root / "run/agent-report.json", report)
    with pytest.raises(ValueError, match="legacy completion report cannot claim"):
        read_completion(*_open(root))
