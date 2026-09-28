"""Synthetic case selection checks; no dataset script or application is executed."""

from __future__ import annotations

import json
from pathlib import Path
from zipfile import ZipFile

import pytest
from pydantic import ValidationError

from dfx_agent.task import CaseCatalog, digest, prepare_task, verify_task


def fixtures(tmp_path: Path) -> tuple[Path, dict[str, object]]:
    data = tmp_path / "data"
    (data / "app" / "src").mkdir(parents=True)
    (data / "app" / "src" / "Main.ets").write_text("let value = 1;\n", encoding="utf-8")
    (data / "app" / "README.md").write_text("analyst material", encoding="utf-8")
    (data / "testcases").mkdir()
    script = data / "testcases" / "case.py"
    script.write_text("raise RuntimeError('never execute')\n", encoding="utf-8")
    log = b"Error name:TypeError\n at main (src/Main.ets:1:1)\n"
    archive = data / "logs.zip"
    with ZipFile(archive, "w") as handle:
        handle.writestr("faultlogger/crash.log", log)
    selection = {
        "task_id": "task1", "case_key": "group/case1", "lineage_group_id": "family1",
        "fault_variant_id": "fault1", "app": "app",
        "log": {"path": "logs.zip", "member": "faultlogger/crash.log", "sha256": digest(log)},
        "scenario": {"path": "testcases/case.py", "sha256": digest(script.read_bytes())},
    }
    return data, selection


def test_explicit_case_selection_prepares_analysis_only(tmp_path: Path) -> None:
    data, selection = fixtures(tmp_path)
    catalog = CaseCatalog.model_validate({"schema_version": 2, "cases": [selection]})
    output = tmp_path / "task"
    result = prepare_task(data, catalog.select("task1").model_dump(exclude_none=True), output)
    task = verify_task(output)
    assert task["schema_version"] == 2
    assert task["case_identity_sha256"] == catalog.select("task1").sha256()
    assert result["case_identity_sha256"] == task["case_identity_sha256"]
    assert task["source_log_alignment"] == "not_verified"
    assert task["runtime_verification"] == "not_run"
    assert (output / "crash.log").read_bytes() == b"Error name:TypeError\n at main (src/Main.ets:1:1)\n"
    paths = [item["path"] for item in json.loads((output / "source-manifest.json").read_text())]
    assert paths == ["src/Main.ets"]
    assert not (output / "source" / "testcases").exists()


@pytest.mark.parametrize("mutation", ["scenario_hash", "log_hash", "member", "scenario_visible"])
def test_changed_selection_evidence_fails_before_snapshot(tmp_path: Path, mutation: str) -> None:
    data, selection = fixtures(tmp_path)
    if mutation == "scenario_hash":
        selection["scenario"]["sha256"] = digest(b"changed")  # type: ignore[index]
    elif mutation == "log_hash":
        selection["log"]["sha256"] = digest(b"changed")  # type: ignore[index]
    elif mutation == "member":
        selection["log"]["member"] = "faultlogger/absent.log"  # type: ignore[index]
    else:
        source = data / "app" / "src" / "Main.ets"
        selection["scenario"] = {"path": "app/src/Main.ets", "sha256": digest(source.read_bytes())}
    output = tmp_path / "task"
    with pytest.raises((ValueError, KeyError)):
        prepare_task(data, selection, output)
    assert not output.exists()


def test_catalog_rejects_duplicate_or_unsafe_selection(tmp_path: Path) -> None:
    _, selection = fixtures(tmp_path)
    duplicate = {**selection, "task_id": "task2", "case_key": "group/case2"}
    with pytest.raises(ValidationError, match="duplicate log bytes"):
        CaseCatalog.model_validate({"schema_version": 2, "cases": [selection, duplicate]})
    with pytest.raises(ValidationError, match="duplicate case key"):
        CaseCatalog.model_validate({"schema_version": 2, "cases": [selection, {**duplicate,
                                    "case_key": "group/case1", "log": {**selection["log"],
                                    "sha256": digest(b"other")}}]})  # type: ignore[arg-type]
    with pytest.raises(ValidationError, match="canonical relative"):
        CaseCatalog.model_validate({"schema_version": 2, "cases": [{**selection, "app": "../other"}]})
    with pytest.raises(ValidationError, match="explicit member"):
        CaseCatalog.model_validate({"schema_version": 2, "cases": [{**selection,
                                    "log": {"path": "logs.zip", "sha256": digest(b"other")}}]})
    with pytest.raises(ValidationError):
        CaseCatalog.model_validate({"schema_version": 1, "cases": [selection]})


def test_v2_task_requires_case_digest_and_legacy_snapshot_remains_readable(tmp_path: Path) -> None:
    data, selection = fixtures(tmp_path)
    output = tmp_path / "task"
    prepare_task(data, selection, output)
    task_path = output / "task.json"
    task = json.loads(task_path.read_text(encoding="utf-8"))
    del task["case_identity_sha256"]
    task_path.write_text(json.dumps(task), encoding="utf-8")
    with pytest.raises(ValueError, match="SHA-256"):
        verify_task(output)
    task["schema_version"] = 1
    task_path.write_text(json.dumps(task), encoding="utf-8")
    assert verify_task(output)["schema_version"] == 1
