"""Browsable case exports use frozen software evidence without a model call."""

from __future__ import annotations

import json
import os
import zipfile
from pathlib import Path
from typing import Any

import pytest

from dfx_agent import case_results, managed_runtime
from dfx_agent.case_results import case_evaluation, export_case_result
from dfx_agent.competition import private_delivery
from dfx_agent.evidence_io import local_evidence_path
from dfx_agent.model_calls import canonical
from dfx_agent.task import digest, prepare_task, verify_task
from dfx_agent.workspace import CandidateWorkspace, TextEdit, _files


def _write(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def _fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, candidate: bool = True,
             task_id: str = "case-fixture") -> dict[str, Any]:
    repo = tmp_path / "deployment"
    repo.mkdir()
    _write(repo / "configs/local-runtime.json", {"limits": {"max_export_package_bytes": 8_388_608}})
    _write(repo / "configs/model.local.json", {"api_key": "never-export-private-key"})
    project = repo / "data_B/application"
    (project / "src").mkdir(parents=True)
    (project / "src/main.js").write_text("export const value = 1;\n", encoding="utf-8")
    (project / "src/deleted.js").write_text("export const unused = 0;\n", encoding="utf-8")
    (project / "assets").mkdir()
    (project / "assets/icon.bin").write_bytes(b"sample-image-resource")
    (project / "build-profile.json5").write_text("{modules: []}\n", encoding="utf-8")
    (project / "README.md").write_text("private benchmark construction hints", encoding="utf-8")
    (project / ".env").write_text("SECRET=do-not-export", encoding="utf-8")
    for relative in (".git/config", "node_modules/package/index.js", "TC_JSError_999/answer.json"):
        path = project / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("excluded", encoding="utf-8")
    log = repo / "data_B/fault.log"
    log.write_text("FAULT in main.js:1\n", encoding="utf-8")
    task = repo / ".dfx-local/tmp/reviewed-task"
    prepare_task(repo / "data_B", {"task_id": task_id, "app": "application",
        "log": {"path": "fault.log", "sha256": digest(log.read_bytes())}}, task)
    run_id = "sample-run"
    work = repo / ".dfx-local/work" / ("competition-" + run_id)
    workspace = CandidateWorkspace(task, work, run_id)
    attempts = []
    selected_id = None
    if candidate:
        workspace.start_draft()
        workspace.apply_text_edits([TextEdit(path="src/main.js", old="value = 1", new="value = 2")])
        (workspace.draft / "src/deleted.js").unlink()
        (workspace.draft / "src/added.js").write_text("export const added = true;\n", encoding="utf-8")
        frozen_candidate = workspace.register_candidate(1)
        selected_id = frozen_candidate.candidate_id
        attempts = [{"candidate": frozen_candidate.model_dump(mode="json"),
                     "patch_path": f"candidates/{selected_id}/patch.diff", "patch_sha256": frozen_candidate.diff_sha256,
                     "check": None, "execution": None}]
    _write(work / "product-launch.json", {"task": str(task), "run_id": run_id})
    _write(work / "competition-config.json", {"config": {"model_mode": "cloud"}, "task": verify_task(task), "execution": None})
    report = {"schema_version": 4, "scope": "competition_task", "task_id": task_id, "run_id": run_id,
        "source_snapshot_sha256": workspace.source_snapshot_sha256, "status": "unresolved",
        "accepted": False, "verification_level": "not_run", "application_verification": "not_run",
        "source_verification": "not_run", "validation_scope": "analysis_only", "mocked_interfaces": [],
        "localization": {"initial": None, "latest": None, "candidate_linked": None},
        "localization_result": {"final": {"candidate_id": selected_id, "complete": False}}, "attempts": attempts,
        "cost": {"known_total_tokens": 25, "total_tokens": None, "missing_usage_calls": 1},
        "metrics": {"repair_success": None}, "stopped_reason": "model_result_unknown"}
    report["report_sha256"] = digest(canonical(report).encode())
    _write(work / "competition-report.json", report)
    (work / "result.md").write_text("# Original private repair explanation\n", encoding="utf-8")
    private_delivery(work, task, 8_388_608)
    _write(work / "run-state.json", {"task": "competition", "run_id": run_id,
        "status": "failed", "finished_at": "2026-10-17T00:00:00+00:00"})
    calls: list[tuple[Path, str]] = []
    monkeypatch.setattr(managed_runtime, "require_managed_worker", lambda root, task: calls.append((root, task)) or {})
    return {"root": repo, "project": project, "task": task, "work": work, "run_id": run_id,
            "candidate_id": selected_id, "calls": calls, "report": report}


def _tree(root: Path) -> dict[str, str]:
    return {name: digest(local_evidence_path(path).read_bytes()) for name, path in _files(root).items()}


def test_export_browsable_candidate_and_complete_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data = _fixture(tmp_path, monkeypatch)
    before = _tree(data["work"])
    result = export_case_result(data["root"], data["run_id"], project=data["project"])
    output = Path(result["path"])
    assert output == data["root"] / "runs/exports/cases/case-fixture/sample-run"
    assert data["calls"] == [(data["root"], "case-export")]
    assert result["accepted"] is False and result["candidate_id"] == data["candidate_id"]
    assert (output / "repaired-source/src/main.js").read_text() == "export const value = 2;\n"
    assert not (output / "repaired-source/src/deleted.js").exists()
    assert (output / "repaired-project/src/added.js").is_file()
    assert not (output / "repaired-project/src/deleted.js").exists()
    assert (output / "repaired-project/assets/icon.bin").read_bytes() == b"sample-image-resource"
    assert (output / "repaired-project/build-profile.json5").is_file()
    for excluded in ("README.md", ".env", ".git", "node_modules", "TC_JSError_999"):
        assert not (output / "repaired-project" / excluded).exists()
    assert (output / "patch.diff").read_bytes() == local_evidence_path(data["work"] / "candidates" / data["candidate_id"] / "patch.diff").read_bytes()
    assert (output / "evidence.zip").read_bytes() == (data["work"] / "delivery.zip").read_bytes()
    assert not os.path.samefile(output / "evidence.zip", data["work"] / "delivery.zip")
    summary = json.loads((output / "summary.json").read_bytes())
    assert summary["report_sha256"] == data["report"]["report_sha256"]
    for relative, metadata in summary["files"].items():
        raw = (output / relative).read_bytes()
        assert metadata == {"bytes": len(raw), "sha256": digest(raw)}
    verification = json.loads((output / "verification.json").read_bytes())
    assert verification["cost"]["total_tokens"] is None
    assert verification["stopped_reason"] == "model_result_unknown"
    readme = (output / "README.md").read_text(encoding="utf-8")
    assert "NEW_RUN_ID" in readme and "不是完整应用工程" in readme
    assert _tree(data["work"]) == before
    assert "never-export-private-key" not in "".join(path.read_text(encoding="utf-8")
        for path in output.glob("*.json"))


def test_analysis_without_candidate_exports_original_without_patch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data = _fixture(tmp_path, monkeypatch, candidate=False)
    result = export_case_result(data["root"], data["run_id"])
    output = Path(result["path"])
    assert result["candidate_id"] is None
    assert not (output / "patch.diff").exists()
    assert not (output / "repaired-project").exists()
    assert (output / "repaired-source/src/main.js").read_text() == "export const value = 1;\n"


@pytest.mark.parametrize("task_id", ["b3-012-homestay1.0.6", "b3-014-driverlicenseexam1.0.7"])
def test_export_preserves_versioned_task_identity(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, task_id: str) -> None:
    data = _fixture(tmp_path, monkeypatch, task_id=task_id)
    result = export_case_result(data["root"], data["run_id"])
    output = Path(result["path"])
    assert output == data["root"] / "runs/exports/cases" / task_id / data["run_id"]
    assert json.loads((output / "summary.json").read_bytes())["task_id"] == task_id


def test_existing_export_is_never_overwritten(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data = _fixture(tmp_path, monkeypatch)
    first = export_case_result(data["root"], data["run_id"])
    before = _tree(Path(first["path"]))
    with pytest.raises(ValueError, match="already exists"):
        export_case_result(data["root"], data["run_id"])
    assert _tree(Path(first["path"])) == before


@pytest.mark.parametrize("failure", ["active", "report", "candidate", "delivery", "project", "limit"])
def test_reject_invalid_export_without_publishing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str) -> None:
    data = _fixture(tmp_path, monkeypatch)
    project = None
    if failure == "active":
        _write(data["work"] / "run-state.json", {"task": "competition", "run_id": data["run_id"], "status": "running"})
    elif failure == "report":
        report = data["report"] | {"accepted": True}
        _write(data["work"] / "competition-report.json", report)
    elif failure == "candidate":
        local_evidence_path(data["work"] / "candidates" / data["candidate_id"] / "source/src/main.js").write_text("changed")
    elif failure == "delivery":
        (data["work"] / "delivery.zip").write_bytes(b"invalid zip")
    elif failure == "project":
        project = data["project"]
        (project / "src/main.js").write_text("another source")
    else:
        _write(data["root"] / "configs/local-runtime.json", {"limits": {"max_export_package_bytes": 10}})
    with pytest.raises((ValueError, OSError, zipfile.BadZipFile)):
        export_case_result(data["root"], data["run_id"], project=project)
    assert not (data["root"] / "runs/exports/cases/case-fixture/sample-run").exists()
    exports = data["root"] / "runs/exports"
    assert not exports.exists() or not list(exports.rglob(".case-result-*"))


def test_managed_admission_required_before_io(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def reject(root: Path, task: str) -> dict[str, Any]:
        raise ValueError("managed admission required")
    monkeypatch.setattr(managed_runtime, "require_managed_worker", reject)
    with pytest.raises(ValueError, match="managed admission"):
        export_case_result(tmp_path, "sample-run")


def test_unknown_usage_retains_original_report_and_full_cap_reservation(tmp_path: Path) -> None:
    report = {"cost": {"known_total_tokens": 25, "total_tokens": None, "missing_usage_calls": 1},
              "metrics": {}, "attempts": []}
    frozen = {"config": {"model_mode": "cloud", "max_known_tokens": 256000,
        "model": {"model_id": "deepseek-flash"},
        "provider": {"endpoint": "https://api.deepseek.com/chat/completions"}}}
    value = case_evaluation(tmp_path, "unknown", "case", frozen, report, {})
    assert value["cost"] == report["cost"]
    assert value["usage"]["total_tokens"] is None
    assert value["usage"]["cache_hit_tokens"] is None
    assert value["model_cost"]["status"] == "unknown"
    assert value["model_cost"]["estimated_ceiling_cny"] is None
    assert value["model_cost"]["conservative_reservation_cny"] == "2.048"


def test_complete_usage_uses_readonly_cache_price_view(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    report = {"cost": {"known_input_tokens": 100, "known_output_tokens": 10, "known_total_tokens": 110,
        "total_tokens": 110, "reserved_calls": 1, "completed_calls": 1, "usage_complete": True},
        "metrics": {}, "attempts": []}
    frozen = {"config": {"model_mode": "cloud", "max_known_tokens": 1000,
        "model": {"model_id": "deepseek-flash"},
        "provider": {"endpoint": "https://api.deepseek.com/chat/completions"}}}
    monkeypatch.setattr(case_results, "deepseek_flash_peak_cost", lambda *args, **kwargs: {
        "logical_calls": 1, "known_input_tokens": 100, "known_output_tokens": 10, "known_total_tokens": 110,
        "cache_usage_complete": True, "cache_hit_tokens": 90, "cache_miss_tokens": 10,
        "estimated_ceiling_cny": "0.0001036", "pricing_as_of": "2026-10-17"})
    value = case_evaluation(tmp_path, "complete", "case", frozen, report, {})
    assert value["usage"]["cache_hit_tokens"] == 90
    assert value["model_cost"]["estimated_ceiling_cny"] == "0.0001036"
    assert value["model_cost"]["status"] == "estimated"
    assert "not_account_bill" in value["model_cost"]["authority"]
    assert "monetary_cost" not in report["cost"]


def test_saved_phase_times_normalize_timezones_without_fabricating_missing_phases(tmp_path: Path) -> None:
    report = {"cost": {}, "metrics": {}, "timing": {"finished_at": "2026-10-17T08:00:10+08:00", "agent_wall_ms": 10000},
        "localization": {"initial": {"diagnosis": {"created_at": "2026-10-17T00:00:01.500Z"}}},
        "attempts": [{"candidate": {"created_at": "2026-10-17T00:00:03.500Z"},
            "execution": {"started_at": "2026-10-17T08:00:04+08:00", "finished_at": "2026-10-17T00:00:08Z"}}]}
    value = case_evaluation(tmp_path, "sample", "case", {}, report, {})
    assert {key: value["phase_timing"][key] for key in (
        "localization_ms", "repair_ms", "time_to_first_candidate_ms", "verification_ms")} == {
        "localization_ms": 1500, "repair_ms": 2000, "time_to_first_candidate_ms": 3500, "verification_ms": 4000}
    report["localization"] = None
    report["attempts"] = []
    phases = case_evaluation(tmp_path, "sample", "case", {}, report, {})["phase_timing"]
    assert phases["localization_ms"] is None and phases["repair_ms"] is None and phases["verification_ms"] is None
