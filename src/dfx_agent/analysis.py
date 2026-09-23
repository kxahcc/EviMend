"""Produce stack evidence; root-cause reasoning remains a separate stage."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter

from .logs import parse_crash
from .sources import SourceIndex
from .task import safe_path, verify_task


def analyze_task(directory: Path, max_anchors: int = 8, radius: int = 6) -> dict:
    if max_anchors < 1 or radius < 0:
        raise ValueError("Invalid analysis limits")
    started = perf_counter()
    task = verify_task(directory)
    checked_at = perf_counter()
    crash = parse_crash(safe_path(directory, task["crash_log"]).read_bytes())
    index = SourceIndex(safe_path(directory, task["source_root"]))
    indexed_at = perf_counter()
    anchors, unresolved = [], []
    existing = {}
    for frame in crash.frames:
        resolution = index.resolve(frame.path)
        if resolution["status"] == "framework":
            continue
        if resolution["status"] != "resolved":
            unresolved.append({"path": frame.path, "line": frame.line, "resolution": resolution})
            continue
        relative = resolution["paths"][0]
        key = (relative, frame.line)
        if key in existing:
            existing[key]["occurrences"] += 1
            continue
        context = index.context(relative, frame.line, radius)
        if not context["line_in_range"]:
            unresolved.append({"path": relative, "line": frame.line, "reason": "line_out_of_range"})
            continue
        anchor = {"path": relative, "line": frame.line, "column": frame.column, "symbol": frame.symbol,
                  "first_log_line": frame.log_line, "mapping_method": resolution["method"],
                  "occurrences": 1, "context": context}
        existing[key] = anchor
        anchors.append(anchor)
    finished = perf_counter()
    return {
        "schema_version": 1, "task_id": task["task_id"], "created_at": datetime.now(UTC).isoformat(),
        "stage": "stack_evidence", "status": "evidence_ready" if anchors else "needs_input_review",
        "source_snapshot_sha256": task["source_snapshot_sha256"], "crash_sha256": crash.sha256,
        "error_name": crash.error_name, "error_name_source": crash.error_name_source,
        "error_message": crash.headers.get("Error message"), "encoding": crash.encoding,
        "warnings": crash.warnings, "parsed_frame_count": len(crash.frames),
        "framework_frame_count": sum(f.framework for f in crash.frames),
        "distinct_mapped_locations": len(anchors), "anchors_truncated": len(anchors) > max_anchors,
        "stack_anchors": anchors[:max_anchors], "unresolved_frames": unresolved,
        "root_cause": None, "patch": None,
        "verification": {"compile": "not_run", "target_replay": "not_run", "functional": "not_run", "regression": "not_run"},
        "metrics": {"model_calls": 0, "model_tokens": 0, "device_runs": 0,
                    "input_check_seconds": checked_at - started, "parse_index_seconds": indexed_at - checked_at,
                    "resolve_seconds": finished - indexed_at, "total_seconds": finished - started},
        "interpretation": "Stack anchors are evidence locations, not predicted or validated root-cause lines.",
    }
