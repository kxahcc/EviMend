"""CLI workflow and reusable fixtures for actual Windows Job acceptance.

The optional phase only prepares or verifies synthetic fixtures. Root acceptance
invokes local.ps1 between phases; normal pytest runs the same CLI in this Job.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from test_agent_loop import task_snapshot
from test_knowledge_retrieval import corpus
from test_metrics import read_json, save_json
from test_provider_loop import wire_response
from test_static_skill_reporting import manifest5

from dfx_agent.applicability_contracts import TaskKnowledgeBinding
from dfx_agent.applicability_handoff import ApplicabilityPolicy
from dfx_agent.knowledge_contracts import content_hash
from dfx_agent.metrics import aggregate_evaluation, parse_evaluation_manifest
from dfx_agent.static_skills import StaticSkillPolicy
from dfx_agent.task import verify_task
from dfx_agent.working_context_plan import WorkingContextPolicy

REPOSITORY = Path(__file__).absolute().parents[1]
CASES = (("kc-on", False, True), ("kc-off", False, False), ("kc-wire", True, True))


def prepare_fixture(root: Path) -> dict:
    root.mkdir(parents=True)
    task = task_snapshot(root / "t")
    library = corpus(root / "l")
    info = verify_task(task)
    entry = library.entries["offset"]
    projection = library.store.read_projection(library.snapshot.snapshot_sha256, entry.entry_id)
    skill_id = "dfx-" + content_hash(projection)[:56]
    calls = [("search_experience", {"tags": ["DataView"], "text": "OFFSET"}),
             ("read_skill", {"skill_id": skill_id}), (None, {})]
    for run_id, wire, enabled in CASES:
        approval = TaskKnowledgeBinding(task_id=info["task_id"], run_id=run_id,
            source_snapshot_sha256=info["source_snapshot_sha256"], crash_sha256=info["crash_sha256"],
            snapshot_sha256=library.snapshot.snapshot_sha256, scope=library.scope)
        value = {"schema_version": 6 if wire else 5,
                 "working_context": WorkingContextPolicy(enabled=False).model_dump(mode="json"),
                 "knowledge": {"library_path": library.store.root.relative_to(REPOSITORY).as_posix(),
                     "approval": approval.model_dump(mode="json"),
                     "applicability_policy": ApplicabilityPolicy(enabled=False).model_dump(mode="json"),
                     "static_skill_policy": StaticSkillPolicy(enabled=enabled,
                         entry_ids=(entry.entry_id,)).model_dump(mode="json")},
                 "expected": {"path": "src/main.ets", "text": "value=good\n"},
                 "limits": {"max_calls": 4, "max_tool_calls": 4}}
        if wire:
            value["model"] = {"mode": "mock_chat_completions",
                "settings": {"provider_id": "mock-provider", "model_id": "synthetic-model"},
                "responses": [wire_response(i, [] if name is None else [(name, args)])
                              for i, (name, args) in enumerate(calls, 1)]}
        else:
            value["responses"] = [{"tool": name, "args": args,
                "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}}
                for name, args in calls]
        save_json(root / (run_id + ".json"), value)
    marker = {"task": task.relative_to(REPOSITORY).as_posix(),
              "library": library.store.root.relative_to(REPOSITORY).as_posix(), "skill_id": skill_id}
    save_json(root / "fixture.json", marker)
    return marker


def verify_fixture(root: Path, work_root: Path) -> None:
    marker = read_json(root / "fixture.json")
    task = REPOSITORY / marker["task"]
    reports = []
    for run_id, _wire, _enabled in CASES:
        work = work_root / ("offline-" + run_id)
        report = read_json(work / "agent-report.json")
        assert report["model_calls"] == 3 and report["tool_calls"] == 2
        assert report["known_tokens"] == 45
        assert report["completion"]["verification"]["accepted"] is False
        assert report["static_skill_facts"]["resources"][0]["actions"][0]["visible_solver_count"] == 1
        assert len(report["offered_tools"]) == 20
        reports.append(report)
    value = manifest5(REPOSITORY, task, work_root / "offline-kc-on")
    value["evaluation_id"] = "managed-knowledge-fixed"
    value["variants"][0]["variant_id"] = "on"
    value["runs"][0]["variant_id"] = "on"
    value["variants"].append({**value["variants"][0], "variant_id": "off", "static_skills_enabled": False})
    value["runs"].append({**value["runs"][0], "variant_id": "off", "run_id": "kc-off",
        "work_path": (work_root / "offline-kc-off").relative_to(REPOSITORY).as_posix()})
    save_json(root / "fixed-matrix.json", value)
    matrix = aggregate_evaluation(parse_evaluation_manifest(value), REPOSITORY)
    assert len(matrix["rows"]) == 2 and all(row["record_status"] == "verified" for row in matrix["rows"])
    assert all(row["cost"]["total_tokens"] == 45 for row in matrix["rows"])
    wire = manifest5(REPOSITORY, task, work_root / "offline-kc-wire")
    wire["evaluation_id"] = "managed-knowledge-wire"
    save_json(root / "wire-matrix.json", wire)
    assert aggregate_evaluation(parse_evaluation_manifest(wire), REPOSITORY)["rows"][0]["record_status"] == "verified"
    assert reports[0]["static_skill_profile"]["files"] == reports[1]["static_skill_profile"]["files"]


def test_managed_knowledge_complete_workflow(tmp_path_factory: pytest.TempPathFactory) -> None:
    phase = os.environ.get("DFX_KNOWLEDGE_SMOKE_PHASE")
    if phase:
        assert phase in {"prepare", "verify"}
        root = REPOSITORY / ".dfx-local/tmp/kc"
        if phase == "prepare":
            prepare_fixture(root)
        else:
            verify_fixture(root, REPOSITORY / ".dfx-local/work")
        return
    root = tmp_path_factory.mktemp("kw") / "kc"
    marker = prepare_fixture(root)
    work_root = root / "w"
    env = {**os.environ, "DFX_MANAGED_AGENT": "offline-v1", "PYTHONDONTWRITEBYTECODE": "1"}
    for run_id, _wire, _enabled in CASES:
        command = [sys.executable, str(REPOSITORY / "scripts/dfx.py"), "repair", "--task",
            str(REPOSITORY / marker["task"]), "--work", str(work_root / ("offline-" + run_id)),
            "--run-id", run_id, "--script", str(root / (run_id + ".json")), "--project-root", str(REPOSITORY)]
        reply = subprocess.run(command, env=env, capture_output=True, timeout=80, check=True)
        assert json.loads(reply.stdout)["schema_version"] == 10
        work = work_root / ("offline-" + run_id)
        save_json(work / "run-state.json", {"schema_version": 1, "task": "offline", "run_id": run_id,
            "status": "finished", "finished_at": "2026-10-05T00:00:00+00:00"})
    verify_fixture(root, work_root)
