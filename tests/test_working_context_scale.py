"""Managed synthetic scale measurements; no real model or application."""

from __future__ import annotations

import cProfile
import json
import time
from pathlib import Path

import pytest
from test_agent_loop import response, task_snapshot

from dfx_agent.agent import run_fixed_agent
from dfx_agent.task import digest, snapshot_digest
from dfx_agent.verification import FixedTextExecutor
from dfx_agent.working_context_plan import WorkingContextPolicy
from dfx_agent.workspace import CandidateWorkspace


@pytest.mark.parametrize(("enabled", "profiled", "distinct"), [
    (False, True, False), (True, True, False),
    (True, False, False), (True, False, True),
])
def test_thousand_file_managed_measurement(
    tmp_path: Path, monkeypatch, capsys, enabled: bool, profiled: bool, distinct: bool,
) -> None:
    task, work = task_snapshot(tmp_path), tmp_path / "run"
    for number in range(1_200):
        raw = (f"// independent synthetic source {number:04}\n".encode() if distinct
               else b"// independent synthetic source\n")
        (task / "source/src" / f"unrelated-{number:04}.ets").write_bytes(raw)
    manifest = [{"path": path.relative_to(task / "source").as_posix(), "sha256": digest(path.read_bytes())}
                for path in sorted((task / "source").rglob("*.ets"))]
    (task / "source-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    value = json.loads((task / "task.json").read_text(encoding="utf-8"))
    value["source_snapshot_sha256"] = snapshot_digest(manifest)
    (task / "task.json").write_text(json.dumps(value), encoding="utf-8")
    # Prepare a known target fixture without a second invocation of the edit
    # pipeline. The measured Agent still executes edit_source and registration.
    expected = CandidateWorkspace(task, tmp_path / "derive", "derive")
    expected.start_draft()
    (expected.draft / "src/main.ets").write_text("value=good\n", encoding="utf-8")
    good = expected.register_candidate(1).candidate_id
    reads = {"calls": 0, "bytes": 0}
    original_read = Path.read_bytes

    def observed_read(path):
        raw = original_read(path)
        reads["calls"] += 1
        reads["bytes"] += len(raw)
        return raw

    if profiled:
        monkeypatch.setattr(Path, "read_bytes", observed_read)
    profiler = cProfile.Profile()
    started = time.monotonic()
    if profiled:
        profiler.enable()
    try:
        report = run_fixed_agent(task, work, "run1", [
            response(1, "record_diagnosis", {"path": "src/main.ets", "line": 1, "reason": "unknown bad value"}),
            response(2, "edit_source", {"path": "src/main.ets", "old": "bad", "new": "good"}),
            response(3, "register_candidate", {"diagnosis_id": "diagnosis-1"}),
            response(4, "run_check", {"candidate_id": good}), response(5)],
            FixedTextExecutor("src/main.ets", "value=good\n"),
            working_context_policy=WorkingContextPolicy(enabled=enabled),
            **({"max_seconds": 300} if profiled else {}))
        returned_at = time.monotonic()
    finally:
        profiler.disable()
    rows = []
    for entry in profiler.getstats():
        code = entry.code
        name = str(code) if isinstance(code, str) else Path(code.co_filename).name + ":" + code.co_name
        rows.append({"function": name, "calls": entry.callcount,
                     "self_seconds": entry.inlinetime, "cumulative_seconds": entry.totaltime})
    own_modules = {"working_context.py", "working_context_plan.py", "working_context_materials.py",
                   "working_context_projection.py", "workspace.py", "task.py", "context_compaction.py"}
    measurement = {"scope": "offline_synthetic_profiled" if profiled else "offline_synthetic_default_budget",
        "source_files": 1201, "distinct_unrelated_bytes": distinct,
        "enabled": enabled, "budget_seconds": 300 if profiled else 60,
        "wall_seconds": returned_at - started,
        "status": report["status"], "stop": report["stopped_reason"], "read_bytes": reads if profiled else None,
        "instrumentation": "cProfile current thread; Path.read_bytes proxy, excludes other read APIs" if profiled else None,
        "model_calls": report["model_calls"],
        "selection_ms": [plan["selection_ms"] for plan in report["completion"]["working_context_plans"]],
        "module_functions": sorted([row for row in rows if row["function"].split(":", 1)[0] in own_modules],
                                   key=lambda row: row["cumulative_seconds"], reverse=True),
        "top_cumulative": sorted(rows, key=lambda row: row["cumulative_seconds"], reverse=True)[:35],
        "top_self": sorted(rows, key=lambda row: row["self_seconds"], reverse=True)[:20]}
    with capsys.disabled():
        print("DFX_SCALE_MEASUREMENT=" + json.dumps(measurement, sort_keys=True))
    # A successful default policy is not a whole-call wall <= 60s promise:
    # workspace creation and final readonly reporting are outside its timer.
    saved = json.loads((work / "session.json").read_text(encoding="utf-8"))
    assert saved["max_seconds"] == measurement["budget_seconds"]
    assert report["status"] == "offline_checked", report["stopped_reason"]
