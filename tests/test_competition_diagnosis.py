"""Candidate localization must be repairable before the draft is frozen."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from test_agent_loop import candidate_id, response, task_snapshot

from dfx_agent.actions import ToolActionStore
from dfx_agent.agent import ResumeAdmissionRejected, _tools, run_fixed_agent
from dfx_agent.contracts import Diagnosis, Hypothesis, SourceLocation
from dfx_agent.diagnostics import AnalysisStore
from dfx_agent.policies import PolicyFeatures
from dfx_agent.task import verify_task
from dfx_agent.task_strategy import build_system_prompt, build_task_message
from dfx_agent.verification import CheckLedger, FixedTextExecutor
from dfx_agent.working_context_plan import WorkingContextPolicy
from dfx_agent.workspace import CandidateWorkspace


def test_sdk_missing_diagnosis_rejects_before_freezing_and_can_reuse_same_draft(tmp_path: Path) -> None:
    task = task_snapshot(tmp_path)
    good = candidate_id(task, tmp_path / "derive", "good")
    work = tmp_path / "run"
    report = run_fixed_agent(task, work, "run1", [
        response(1, "edit_source", {"path": "src/main.ets", "old": "bad", "new": "good"}),
        response(2, "register_candidate"),
        response(3, "record_diagnosis", {"path": "src/main.ets", "line": 1, "reason": "Bad value causes the failure"}),
        response(4, "register_candidate", {"diagnosis_id": "diagnosis-1"}),
        response(5, "run_check", {"candidate_id": good}), response(6),
    ], FixedTextExecutor("src/main.ets", "value=good\n"),
        require_candidate_diagnosis=True,
        working_context_policy=WorkingContextPolicy(enabled=True, history_selection="repair_frontier",
                                                   context_layout="decision_frontier"))
    assert report["status"] == "offline_checked" and report["candidates"] == [good]
    assert report["checks"][0]["status"] == "pass"
    assert [action["tool_name"] for action in report["actions"]].count("edit_source") == 1
    rejected = ToolActionStore(work / "tool-actions.sqlite").receipt(report["actions"][1]["action_id"])
    assert isinstance(rejected.content, str)
    assert "diagnosis_id from record_diagnosis" in rejected.content
    assert "draft is unchanged" in rejected.content
    candidate = json.loads((work / "candidates" / good / "candidate.json").read_text(encoding="utf-8"))
    assert candidate["diagnosis_id"] == "diagnosis-1" and candidate["ordinal"] == 1
    assert json.loads((work / "session.json").read_text(encoding="utf-8"))["require_candidate_diagnosis"] is True


@pytest.mark.parametrize("missing", ["selection", "root_lines"])
def test_incomplete_selected_root_rejects_without_consuming_candidate_or_draft(tmp_path: Path, missing: str) -> None:
    task = task_snapshot(tmp_path)
    workspace = CandidateWorkspace(task, tmp_path / "run", "run1")
    ledger = CheckLedger(workspace, FixedTextExecutor("src/main.ets", "value=good\n"))
    tools = {item.name: item for item in _tools(workspace, ledger, 1, task, "crash.log",
        verify_task(task)["crash_sha256"], PolicyFeatures(), require_candidate_diagnosis=True)}
    location = SourceLocation(snapshot_sha256=workspace.source_snapshot_sha256,
                              path="src/main.ets", start_line=1, end_line=1)
    identity = {"task_id": workspace.task_id, "run_id": workspace.run_id,
                "source_snapshot_sha256": workspace.source_snapshot_sha256, "created_at": datetime.now(UTC)}
    store = AnalysisStore(workspace)
    store.put_bundle(Diagnosis(**identity, record_id="incomplete", hypotheses=[
        Hypothesis(hypothesis_id="h-incomplete", support_status="unknown",
                   root_cause_locations=[] if missing == "root_lines" else [location])],
        selected_hypothesis_id=None if missing == "selection" else "h-incomplete"), [], [])
    tools["edit_source"].invoke({"path": "src/main.ets", "old": "bad", "new": "good"})
    rejected = tools["register_candidate"].invoke({"diagnosis_id": "incomplete"})
    assert "selected hypothesis with original root-cause lines" in rejected
    assert (workspace.draft / "src/main.ets").read_bytes() == b"value=good\n"
    assert list(workspace.candidates.iterdir()) == []
    store.put_bundle(Diagnosis(**identity, record_id="complete", hypotheses=[
        Hypothesis(hypothesis_id="h-complete", support_status="unknown", root_cause_locations=[location])],
        selected_hypothesis_id="h-complete"), [], [])
    registered = json.loads(tools["register_candidate"].invoke({"diagnosis_id": "complete"}))
    assert registered["ordinal"] == 1 and not workspace.draft.exists()
    assert workspace.require_registered(registered["candidate_id"]).is_dir()


def test_candidate_requirement_is_frozen_and_cannot_change_on_resume(tmp_path: Path) -> None:
    task = task_snapshot(tmp_path)
    work = tmp_path / "run"
    executor = FixedTextExecutor("src/main.ets", "value=good\n")
    run_fixed_agent(task, work, "run1", [response(1)], executor, require_candidate_diagnosis=True)
    before = (work / "session.json").read_bytes()
    with pytest.raises(ResumeAdmissionRejected, match="session identity"):
        run_fixed_agent(task, work, "run1", [response(1)], executor, resume=True)
    assert (work / "session.json").read_bytes() == before


def test_strict_prompt_and_startup_explain_the_required_candidate_link() -> None:
    features = PolicyFeatures()
    assert build_system_prompt(features) == build_system_prompt(features, require_candidate_diagnosis=False)
    assert "Pass the actual diagnosis_id" in build_system_prompt(features, require_candidate_diagnosis=True)
    ordinary = build_task_message(task_id="task1", run_id="run1", source_snapshot_sha256="a" * 64,
                                  capabilities=frozenset({"read_source"}), resume=False)
    strict = build_task_message(task_id="task1", run_id="run1", source_snapshot_sha256="a" * 64,
                                capabilities=frozenset({"read_source"}), resume=False,
                                require_candidate_diagnosis=True)
    assert "candidate_localization" not in json.loads(ordinary)
    assert "register_candidate" in json.loads(strict)["candidate_localization"]
