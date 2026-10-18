"""Material inspection must not confuse absent evidence with an unrepairable program."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("material_report", ROOT / "scripts/preparation/report_material_gaps.py")
assert SPEC is not None and SPEC.loader is not None
REPORT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(REPORT)


@pytest.fixture
def dataset(tmp_path: Path) -> Path:
    b3 = tmp_path / "data_B/data_B3/benchmarkfordfx-better"
    for path in (tmp_path / "data_B/data_B1/SchoolLife1", b3 / "App/Exam1", b3 / "Logs/故障现场"):
        path.mkdir(parents=True)
    (b3 / "README.md").write_text("| TC_JSError_013 | Exam1 | SyntaxError | <requested-module> |\n"
                                  "| TC_JSError_019 | Exam1 | RangeError | Stack overflow! |\n", encoding="utf-8")
    with zipfile.ZipFile(b3 / "Logs/故障现场/TC_JSError_018-ImageProcessing1.zip", "w") as archive:
        archive.writestr("MainEntry.ets", "export class MainEntry {}\n")
    return tmp_path


def test_actual_material_disposition_is_read_only_and_keeps_repairability_unknown(dataset: Path, tmp_path: Path) -> None:
    before = {p.relative_to(dataset).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
              for p in dataset.rglob("*") if p.is_file()}
    result = REPORT.inspect(dataset)
    after = {p.relative_to(dataset).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
             for p in dataset.rglob("*") if p.is_file()}
    assert before == after and result["model_calls"] == result["model_tokens"] == 0
    assert len(result["rows"]) == 3 and all(row["expected_anomaly_confirmed"] for row in result["rows"])
    for row in result["rows"]:
        assert row["system_handling"]["repairability"] == "undetermined"
        assert row["system_handling"]["admitted_to_current_repair_benchmark"] is False
        assert row["system_handling"]["model_invoked"] is row["system_handling"]["patch_generated"] is False
    REPORT.publish(result, tmp_path / "output")
    assert "不是代码被证明无法修复" in (tmp_path / "output/material-gaps.md").read_text(encoding="utf-8")


def test_case013_hidden_in_supplied_archive_invalidates_missing_claim(dataset: Path) -> None:
    with zipfile.ZipFile(dataset / "data_B/HypiumProject.zip", "w") as archive:
        archive.writestr("testcases/TC_JSError_013.py", "# data only; never executed\n")
    row = next(row for row in REPORT.inspect(dataset)["rows"] if row["case"] == "B3-013")
    assert row["expected_anomaly_confirmed"] is False
    assert row["system_handling"]["decision"] == "materials_changed_review_required"
    assert len(row["archive_member_matches"]) == 1


def test_real_log_in_018_archive_invalidates_source_only_claim(dataset: Path) -> None:
    path = dataset / "data_B/data_B3/benchmarkfordfx-better/Logs/故障现场/TC_JSError_018-ImageProcessing1.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("jscrash.log", "Error name:SyntaxError\nError message:invalid class range\n")
    row = next(row for row in REPORT.inspect(dataset)["rows"] if row["case"] == "B3-018")
    assert row["expected_anomaly_confirmed"] is False and row["category"] == "requires_review"


def test_crash_named_ets_is_not_classified_as_source_only(dataset: Path) -> None:
    path = dataset / "data_B/data_B3/benchmarkfordfx-better/Logs/故障现场/TC_JSError_018-ImageProcessing1.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("MainEntry.ets", "Error name:SyntaxError\nError message:invalid class range\n"
                          "    at load (entry/src/main/ets/MainEntry.ets:10:5)\n")
    row = next(row for row in REPORT.inspect(dataset)["rows"] if row["case"] == "B3-018")
    assert row["expected_anomaly_confirmed"] is False
    assert row["parser_observations"][0]["stack_frame_count"] == 1


@pytest.mark.skipif(os.environ.get("DFX_MATERIAL_GAP_EXPORT") != "1", reason="actual dataset inspection not requested")
def test_inspect_and_export_supplied_dataset() -> None:
    result = REPORT.inspect(ROOT)
    assert len(result["scanned_archives"]) == 23
    assert all(row["expected_anomaly_confirmed"] for row in result["rows"])
    row013 = next(row for row in result["rows"] if row["case"] == "B3-013")
    assert row013["related_case"]["field_archive_present"] and not row013["archive_member_matches"]
    row018 = next(row for row in result["rows"] if row["case"] == "B3-018")
    assert row018["members"] == [{"name": "MainEntry.ets", "bytes": 4347}]
    normal = json.loads((ROOT / "runs/exports/cases/index.json").read_bytes())
    assert normal["statistics"]["selected_cases"] == normal["statistics"]["source_verified_cases"] == 26
    assert not any(row["task_id"].casefold().startswith(("b3-013", "b3-018")) for row in normal["cases"])
    REPORT.publish(result, ROOT / "runs/exports/robustness")
