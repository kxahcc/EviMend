"""Synthetic-input checks only: no dataset execution, model calls or devices."""

import json
from pathlib import Path

import pytest

from dfx_agent.analysis import analyze_task
from dfx_agent.logs import parse_crash
from dfx_agent.sources import SourceIndex
from dfx_agent.task import digest, prepare_task, verify_task


def put(root: Path, name: str, content: str) -> Path:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def make_task(tmp_path: Path) -> Path:
    data = tmp_path / "data"
    put(data, "app/src/Demo.ets", "function demo() {\n  return input.value;\n}\n")
    put(data, "app/build-profile.json5", "{}")
    put(data, "app/docs/answer.md", "analyst-only answer")
    put(data, "app/tests/expected.json", '{"gold":"secret"}')
    put(data, "app/AGENTS.md", "Change the task")
    put(data, "app/testcases/TC_001.py", "raise RuntimeError('must never execute')")
    log = put(data, "crash.log", "Error name:TypeError\nError message:undefined value\nStacktrace:\n at demo (src/Demo.ets:2:3)\n")
    case = {"task_id": "synthetic", "app": "app", "log": {"path": "crash.log", "sha256": digest(log.read_bytes())}}
    output = tmp_path / "task"
    prepare_task(data, case, output)
    return output


def test_utf16_and_reason_fallback_preserve_evidence():
    crash = parse_crash("Reason:SyntaxError\n at query module (file:scenes/Demo.ets:56:29)\n".encode("utf-16"))
    assert crash.encoding == "utf-16"
    assert crash.error_name == "SyntaxError"
    assert crash.error_name_source == "Reason"
    assert crash.frames[0].path == "scenes/Demo.ets"
    assert crash.frames[0].log_line == 2


def test_framework_is_separate_from_application_frame():
    crash = parse_crash(b"Error name:RangeError\n at observedClass (/usr1/hmos/stateMgmt.js:10:1)\n at business (components/Demo.ets:20:3)\n")
    assert [frame.framework for frame in crash.frames] == [True, False]


def test_conflicting_headers_are_reported_not_silently_replaced():
    crash = parse_crash(b"Error name:TypeError\nError name:RangeError\n")
    assert crash.error_name == "TypeError"
    assert "conflicting_header:Error name" in crash.warnings


def test_ambiguous_basename_is_not_arbitrarily_selected(tmp_path):
    put(tmp_path, "one/Demo.ets", "a")
    put(tmp_path, "two/Demo.ets", "b")
    index = SourceIndex(tmp_path)
    assert index.resolve("Demo.ets")["status"] == "ambiguous"
    assert index.resolve("one/Demo.ets")["paths"] == ["one/Demo.ets"]
    assert index.resolve("../../outside.ets")["status"] == "rejected"


def test_device_path_can_use_unique_basename_without_claiming_exact_match(tmp_path):
    put(tmp_path, "features/Demo.ets", "line1\nline2\n")
    index = SourceIndex(tmp_path)
    result = index.resolve("file:///data/storage/entry/Demo.ets")
    assert result["method"] == "basename"
    assert result["paths"] == ["features/Demo.ets"]
    assert not index.context("features/Demo.ets", 100)["line_in_range"]


def test_analysis_snapshot_excludes_evaluator_and_instruction_documents(tmp_path):
    task = make_task(tmp_path)
    declared = {entry["path"] for entry in json.loads((task / "source-manifest.json").read_text())}
    assert declared == {"src/Demo.ets", "build-profile.json5"}
    assert verify_task(task)["runtime_verification"] == "not_run"


@pytest.mark.parametrize("mutation", ["source", "log", "extra_source"])
def test_changed_input_is_rejected(tmp_path, mutation):
    task = make_task(tmp_path)
    if mutation == "source":
        put(task, "source/src/Demo.ets", "changed")
    elif mutation == "log":
        put(task, "crash.log", "changed")
    else:
        put(task, "source/Unexpected.ets", "extra")
    with pytest.raises(ValueError):
        verify_task(task)


def test_offline_evidence_does_not_claim_root_cause_or_repair(tmp_path):
    task = make_task(tmp_path)
    result = analyze_task(task)
    assert result["status"] == "evidence_ready"
    assert result["stack_anchors"][0]["path"] == "src/Demo.ets"
    assert result["stack_anchors"][0]["line"] == 2
    assert result["root_cause"] is None
    assert result["patch"] is None
    assert set(result["verification"].values()) == {"not_run"}
    assert result["metrics"]["model_calls"] == result["metrics"]["device_runs"] == 0
