"""Metamorphic and mismatch checks through prepare -> verify -> parse -> map."""
from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from dfx_agent.analysis import analyze_task
from dfx_agent.task import digest, prepare_task, verify_task

BASE = "Error name:TypeError\n at lookup (src/Demo.ets:2:3)\n"
METAMORPHIC = {
    "plain": BASE.encode(), "crlf": BASE.replace("\n", "\r\n").encode(),
    "utf8_bom": b"\xef\xbb\xbf" + BASE.encode(), "utf16le": BASE.encode("utf-16"),
    "utf16be": b"\xfe\xff" + BASE.encode("utf-16-be"),
    "gb18030": ("现场日志\n" + BASE).encode("gb18030"),
    "leading_noise": ("INFO unrelated startup\n" * 200 + BASE).encode(),
    "trailing_noise": (BASE + "INFO other telemetry\n" * 200).encode(),
    "hybrid_stack": BASE.replace(" at lookup", "#123 at lookup").encode(),
}
MISMATCHES = {
    "missing_file": ("src/Absent.ets", "unresolved"),
    "ambiguous_basename": ("Demo.ets", "ambiguous"),
    "device_ambiguous": ("file:///data/storage/entry/Demo.ets", "ambiguous"),
    "line_out_of_range": ("src/Demo.ets:999", "line_out_of_range"),
    "zero_line": ("src/Demo.ets:0", "line_out_of_range"),
    "parent_path": ("../src/Demo.ets", "rejected"),
}


def inspect(root: Path, log: bytes, *, duplicate: bool = False) -> dict:
    data = root / "data"
    source = data / "app/src/Demo.ets"
    source.parent.mkdir(parents=True)
    source.write_text("function lookup(value) {\n return value.name;\n}\n" + "// context\n" * 10, encoding="utf-8")
    if duplicate:
        other = data / "app/other/Demo.ets"
        other.parent.mkdir()
        other.write_text("// separate module\n" * 13, encoding="utf-8")
    (data / "fault.log").write_bytes(log)
    task = root / "task"
    prepare_task(data, {"task_id": "evidence-robustness", "app": "app",
        "log": {"path": "fault.log", "sha256": digest(log)}}, task)
    result = analyze_task(task)
    assert result["crash_sha256"] == digest(log)
    assert verify_task(task)["source_snapshot_sha256"] == result["source_snapshot_sha256"]
    assert (task / "crash.log").read_bytes() == log
    assert result["root_cause"] is result["patch"] is None
    assert set(result["verification"].values()) == {"not_run"}
    assert result["metrics"]["model_calls"] == result["metrics"]["device_runs"] == 0
    return result


@pytest.mark.parametrize("name", list(METAMORPHIC))
def test_equivalent_log_forms_preserve_source_evidence(tmp_path: Path, name: str) -> None:
    result = inspect(tmp_path, METAMORPHIC[name])
    assert result["status"] == "evidence_ready"
    assert [(p["path"], p["line"], p["column"]) for p in result["stack_anchors"]] == [("src/Demo.ets", 2, 3)]


@pytest.mark.parametrize("name", list(MISMATCHES))
def test_mismatched_frames_remain_unresolved_without_invented_locations(tmp_path: Path, name: str) -> None:
    path, expected = MISMATCHES[name]
    suffix = ":3" if name in {"line_out_of_range", "zero_line"} else ":2:3"
    result = inspect(tmp_path, f"Error name:TypeError\n at lookup ({path}{suffix})\n".encode(), duplicate=True)
    assert result["status"] == "needs_input_review" and result["stack_anchors"] == []
    unresolved = result["unresolved_frames"][0]
    assert unresolved.get("reason", unresolved.get("resolution", {}).get("status")) == expected


def test_mixed_framework_and_repeated_business_frames_keep_counts_and_original_anchor(tmp_path: Path) -> None:
    log = ("Error name:TypeError\n at observe (/usr1/hmos/stateMgmt.js:10:1)\n" +
           "#2 at lookup (src/Demo.ets:2:3)\n" * 10000).encode()
    result = inspect(tmp_path, log)
    assert result["parsed_frame_count"] == 10001 and result["framework_frame_count"] == 1
    assert result["distinct_mapped_locations"] == 1
    assert result["stack_anchors"][0]["first_log_line"] == 3
    assert result["stack_anchors"][0]["occurrences"] == 10000


def test_many_distinct_locations_are_bounded_and_truncation_is_explicit(tmp_path: Path) -> None:
    log = ("Error name:TypeError\n" + "".join(f" at lookup (src/Demo.ets:{n}:1)\n" for n in range(1, 14))).encode()
    result = inspect(tmp_path, log)
    assert result["distinct_mapped_locations"] == 13 and result["anchors_truncated"] is True
    assert len(result["stack_anchors"]) == 8 and result["stack_anchors"][-1]["line"] == 8


@pytest.mark.skipif(os.environ.get("DFX_EVIDENCE_ROBUSTNESS_EXPORT") != "1", reason="local evidence matrix export not requested")
def test_export_evidence_matrix(tmp_path: Path) -> None:
    rows = []
    for name, log in METAMORPHIC.items():
        result = inspect(tmp_path / name, log)
        assert result["stack_anchors"][0]["path"] == "src/Demo.ets" and result["stack_anchors"][0]["line"] == 2
        rows.append({"name": name, "category": "equivalent_representation", "passed": True, "evidence": result})
    for name, (path, expected) in MISMATCHES.items():
        suffix = ":3" if name in {"line_out_of_range", "zero_line"} else ":2:3"
        result = inspect(tmp_path / name, f"Error name:TypeError\n at lookup ({path}{suffix})\n".encode(), duplicate=True)
        assert not result["stack_anchors"]
        unresolved = result["unresolved_frames"][0]
        assert unresolved.get("reason", unresolved.get("resolution", {}).get("status")) == expected
        rows.append({"name": name, "category": "source_log_mismatch", "expected": expected, "passed": True, "evidence": result})
    assert len({row["evidence"]["source_snapshot_sha256"] for row in rows[:len(METAMORPHIC)]}) == 1
    repeated = inspect(tmp_path / "repeated-frames", ("Error name:TypeError\n" +
        " at observe (/usr1/hmos/stateMgmt.js:10:1)\n" + "#2 at lookup (src/Demo.ets:2:3)\n" * 10000).encode())
    assert repeated["stack_anchors"][0]["occurrences"] == 10000 and repeated["framework_frame_count"] == 1
    rows.append({"name": "mixed_framework_and_10000_repeats", "category": "evidence_size", "passed": True, "evidence": repeated})
    bounded = inspect(tmp_path / "bounded-anchors", ("Error name:TypeError\n" +
        "".join(f" at lookup (src/Demo.ets:{n}:1)\n" for n in range(1, 14))).encode())
    assert bounded["anchors_truncated"] and bounded["distinct_mapped_locations"] == 13 and len(bounded["stack_anchors"]) == 8
    rows.append({"name": "explicit_anchor_truncation", "category": "evidence_size", "passed": True, "evidence": bounded})
    destination = Path(__file__).resolve().parents[1] / "runs/exports/robustness/evidence-matrix.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps({"generated_at_utc": datetime.now(UTC).isoformat(),
        "scope": "Synthetic full input preparation and source-evidence mapping, not model root-cause accuracy",
        "model_calls": 0, "model_cost_cny": "0", "rows": rows}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
