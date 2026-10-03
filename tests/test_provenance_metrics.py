"""Version-aware aggregation retains historical unknowns and the frozen denominator."""

from __future__ import annotations

import copy
import json
import shutil
from pathlib import Path
from typing import Any

import pytest
from test_agent_loop import response
from test_metrics import make_task, one_manifest, run_synthetic, validated
from test_readonly_evidence import tree_bytes

from dfx_agent import provenance
from dfx_agent.actions import ToolActionStore
from dfx_agent.metrics import aggregate_evaluation
from dfx_agent.model_calls import ModelCallStore, canonical
from dfx_agent.reporting import build_completion
from dfx_agent.verification import CheckLedger, ReplayOnlyExecutor
from dfx_agent.workspace import CandidateWorkspace

OLD_PROVENANCE = {"git_commit": None, "lock_sha256": None, "sdk_version": None,
                  "status": "not_recorded_in_source_session"}


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _save(path: Path, value: dict[str, Any]) -> None:
    path.write_text(json.dumps(value), encoding="utf-8")


@pytest.fixture(scope="module")
def captured_evaluation(tmp_path_factory: pytest.TempPathFactory):
    root = tmp_path_factory.mktemp("metrics-provenance")
    task = make_task(root / "tasks/task1")
    first = root / "work/run1"
    run_synthetic(task, first, "run1", [response(1)])
    _historical_v9(root, "run1")
    raw = one_manifest(root, task, first, "run1")
    second = root / "work/run2"
    run_synthetic(task, second, "run2", [response(1)])
    _historical_v9(root, "run2")
    raw["repeat_ids"].append("repeat2")
    raw["runs"].append({**raw["runs"][0], "repeat_id": "repeat2", "run_id": "run2", "work_path": "work/run2"})
    return root, raw


def _clone(source, tmp_path: Path):
    root, raw = source
    copied = tmp_path / "evidence"
    shutil.copytree(root, copied)
    return copied, copy.deepcopy(raw)


def _rebuild(root: Path, run_id: str) -> None:
    """Bind an explicitly transformed synthetic session to its saved summary."""
    work = root / "work" / run_id
    session = _load(work / "session.json")
    workspace = CandidateWorkspace.reopen(root / "tasks/task1/input", work, run_id)
    ledger = CheckLedger(workspace, ReplayOnlyExecutor(session["protocol_sha256"]), read_only=True)
    calls = ModelCallStore(work / "model-calls.sqlite", read_only=True)
    actions = ToolActionStore(work / "tool-actions.sqlite", read_only=True)
    report = _load(work / "agent-report.json")
    value = build_completion(workspace, ledger, calls, actions, stopped_reason=report["stopped_reason"])
    report.update(schema_version={2: 5, 3: 6, 4: 7}[value["schema_version"]], completion=value)
    if "execution_provenance" in value:
        report["execution_provenance"] = value["execution_provenance"]
    else:
        report.pop("execution_provenance", None)
    if "working_context_plans" in value:
        report["working_context_plans"] = value["working_context_plans"]
    else:
        report.pop("working_context_plans", None)
    _save(work / "agent-report.json", report)


def _historical_v9(root: Path, run_id: str) -> None:
    """Construct a source-version fixture, never rewrite production evidence."""
    path = root / "work" / run_id / "session.json"
    session = _load(path)
    session["schema_version"] = 9
    session["context_profile"].pop("host_working_context", None)
    session["context_profile"].pop("working_context_prompt_sha256", None)
    _save(path, session)
    _rebuild(root, run_id)


def _legacy(root: Path, run_id: str) -> None:
    path = root / "work" / run_id / "session.json"
    session = _load(path)
    session["schema_version"] = 8
    session.pop("execution_provenance")
    _save(path, session)
    _rebuild(root, run_id)


def _record_change(root: Path, run_id: str, change: str) -> dict[str, Any]:
    path = root / "work" / run_id / "session.json"
    session = _load(path)
    record = session["execution_provenance"]
    if change == "runtime":
        record["runtime"]["distributions"]["deepagents"] = "historical-synthetic-version"
    elif change == "source":
        record["source_inventory"][0]["sha256"] = "b" * 64
    elif change == "git":
        record["git"] = {"available": True, "commit": "a" * 40, "dirty": True,
                         "status_entries": 1, "status_sha256": "b" * 64}
    else:
        raise AssertionError("unknown synthetic record transform")
    session["execution_provenance"] = provenance._seal(record)
    _save(path, session)
    _rebuild(root, run_id)
    return session["execution_provenance"]


def _forbid_current_host(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*_args, **_kwargs):
        raise AssertionError("aggregation must not inspect the reader host for execution provenance")

    for name in ("capture_execution_provenance", "ensure_execution_compatible", "verify_host_baseline"):
        monkeypatch.setattr(provenance, name, forbidden)


def test_new_rows_group_observed_saved_records_and_preserve_inputs(
    captured_evaluation, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, raw = captured_evaluation
    before = tree_bytes(root)
    _forbid_current_host(monkeypatch)
    result = aggregate_evaluation(validated(raw), root)
    summary = result["execution_provenance"]
    assert summary["status"] == "observed"
    assert summary["expected_runs"] == summary["captured_runs"] == 2
    assert summary["unknown_runs"] == 0 and summary["unknown_run_ids"] == []
    group = summary["captured_records"][0]
    assert group["run_ids"] == ["run1", "run2"]
    assert sum(len(record["run_ids"]) for record in group["records"]) == 2
    for row in result["rows"]:
        session = _load(root / row["work_path"] / "session.json")
        assert row["execution_provenance"] == session["execution_provenance"]
    assert all(value is None for value in result["application_metrics"].values())
    assert tree_bytes(root) == before


def test_old_only_aggregate_keeps_exact_legacy_shape_and_hash(
    captured_evaluation, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, raw = _clone(captured_evaluation, tmp_path)
    for run_id in ("run1", "run2"):
        _legacy(root, run_id)
    expected = canonical(aggregate_evaluation(validated(raw), root))
    before = tree_bytes(root)
    _forbid_current_host(monkeypatch)
    result = aggregate_evaluation(validated(raw), root)
    assert canonical(result) == expected
    assert result["execution_provenance"] == OLD_PROVENANCE
    assert all("execution_provenance" not in row for row in result["rows"])
    assert tree_bytes(root) == before


def test_mixed_legacy_not_run_and_missing_remain_unknown_in_full_matrix(
    captured_evaluation, tmp_path: Path,
) -> None:
    root, raw = _clone(captured_evaluation, tmp_path)
    _legacy(root, "run2")
    raw["repeat_ids"] += ["repeat3", "repeat4"]
    raw["runs"] += [{**raw["runs"][0], "repeat_id": "repeat3", "run_id": "not-run", "work_path": None},
                    {**raw["runs"][0], "repeat_id": "repeat4", "run_id": "missing", "work_path": "work/missing"}]
    result = aggregate_evaluation(validated(raw), root)
    summary = result["execution_provenance"]
    assert summary["status"] == "partial" and summary["expected_runs"] == 4
    assert summary["captured_runs"] == 1 and summary["unknown_runs"] == 3
    assert summary["unknown_run_ids"] == ["missing", "not-run", "run2"]
    assert len(result["rows"]) == result["variants"][0]["expected_runs"] == 4
    assert result["variants"][0]["cost"]["total_tokens"] is None
    assert [row["record_status"] for row in result["rows"]] == ["verified", "verified", "not_run", "missing"]
    assert all("execution_provenance" not in row for row in result["rows"][1:])


def test_missing_new_report_preserves_only_bound_session_provenance(captured_evaluation, tmp_path: Path) -> None:
    root, raw = _clone(captured_evaluation, tmp_path)
    (root / "work/run2/agent-report.json").unlink()
    before = tree_bytes(root)
    result = aggregate_evaluation(validated(raw), root)
    row = result["rows"][1]
    assert row["record_status"] == "missing" and row["missing_reason"] == "completion_missing"
    assert row["cost"] is None and row["synthetic_outcome"] == "unknown"
    assert row["execution_provenance"] == _load(root / "work/run2/session.json")["execution_provenance"]
    assert result["execution_provenance"]["status"] == "observed"
    assert result["execution_provenance"]["captured_runs"] == 2
    assert tree_bytes(root) == before


def test_missing_legacy_reports_keep_exact_unknown_provenance_shape(captured_evaluation, tmp_path: Path) -> None:
    root, raw = _clone(captured_evaluation, tmp_path)
    for run_id in ("run1", "run2"):
        _legacy(root, run_id)
        (root / "work" / run_id / "agent-report.json").unlink()
    before = tree_bytes(root)
    result = aggregate_evaluation(validated(raw), root)
    assert result["execution_provenance"] == OLD_PROVENANCE
    assert len(result["rows"]) == 2
    assert all(row["record_status"] == "missing" and row["cost"] is None for row in result["rows"])
    assert all("execution_provenance" not in row for row in result["rows"])
    assert tree_bytes(root) == before


@pytest.mark.parametrize("damage", ["legacy_injected", "unsupported_schema", "boolean_schema", "not_object"])
def test_missing_report_cannot_hide_invalid_session_provenance(
        captured_evaluation, tmp_path: Path, damage: str) -> None:
    root, raw = _clone(captured_evaluation, tmp_path)
    session_path = root / "work/run2/session.json"
    session: Any = _load(session_path)
    if damage == "legacy_injected":
        session["schema_version"] = 8
    elif damage == "unsupported_schema":
        session["schema_version"] = 11
    elif damage == "boolean_schema":
        session["schema_version"] = True
    else:
        session = []
    session_path.write_text(json.dumps(session), encoding="utf-8")
    (root / "work/run2/agent-report.json").unlink()
    before = tree_bytes(root)
    with pytest.raises(ValueError):
        aggregate_evaluation(validated(raw), root)
    assert tree_bytes(root) == before


def test_git_differences_are_separate_records_with_one_compatible_execution(captured_evaluation, tmp_path: Path) -> None:
    root, raw = _clone(captured_evaluation, tmp_path)
    old = _load(root / "work/run2/session.json")["execution_provenance"]
    changed = _record_change(root, "run2", "git")
    assert changed["compatibility_sha256"] == old["compatibility_sha256"]
    assert changed["provenance_sha256"] != old["provenance_sha256"]
    result = aggregate_evaluation(validated(raw), root)
    records = result["execution_provenance"]["captured_records"]
    assert len(records) == 1 and len(records[0]["records"]) == 2


@pytest.mark.parametrize("change", ["runtime", "source"])
def test_different_compatibility_records_cannot_be_pooled(captured_evaluation, tmp_path: Path, change: str) -> None:
    root, raw = _clone(captured_evaluation, tmp_path)
    _record_change(root, "run2", change)
    with pytest.raises(ValueError, match="execution compatibility differs"):
        aggregate_evaluation(validated(raw), root)


def test_historical_versions_are_read_from_saved_record_without_current_host_comparison(
    captured_evaluation, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, raw = _clone(captured_evaluation, tmp_path)
    expected = _record_change(root, "run1", "runtime")
    _record_change(root, "run2", "runtime")
    before = tree_bytes(root)
    _forbid_current_host(monkeypatch)
    result = aggregate_evaluation(validated(raw), root)
    assert result["execution_provenance"]["status"] == "observed"
    assert result["rows"][0]["execution_provenance"] == expected
    assert tree_bytes(root) == before


@pytest.mark.parametrize("target", ["session", "report", "completion", "missing_session"])
def test_tampered_or_unbound_new_provenance_rejects_the_aggregate(
    captured_evaluation, tmp_path: Path, target: str,
) -> None:
    root, raw = _clone(captured_evaluation, tmp_path)
    if target in ("session", "missing_session"):
        path = root / "work/run2/session.json"
        value = _load(path)
        value["execution_provenance"]["runtime"]["python"]["version"] = "0.0.0"
        if target == "missing_session":
            (root / "work/run2/agent-report.json").unlink()
    else:
        path = root / "work/run2/agent-report.json"
        value = _load(path)
        selected = value if target == "report" else value["completion"]
        selected["execution_provenance"]["compatibility_sha256"] = "0" * 64
    _save(path, value)
    with pytest.raises(ValueError):
        aggregate_evaluation(validated(raw), root)
