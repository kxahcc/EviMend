"""Actual SDK message/search handoff with scripted responses, not a model benchmark."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from langchain_core.messages import SystemMessage
from test_agent_loop import response, task_snapshot

from dfx_agent import agent, source_search
from dfx_agent.actions import OFFLINE_CAPABILITIES, TOOL_ACTIONS, ToolActionStore
from dfx_agent.agent import run_fixed_agent
from dfx_agent.applicability_handoff import APPLICABILITY_TOOLS
from dfx_agent.knowledge_handoff import KNOWLEDGE_TOOLS
from dfx_agent.model_calls import ModelCallStore, canonical, sha256
from dfx_agent.models import FixedResponseModel
from dfx_agent.policies import PolicyFeatures
from dfx_agent.source_search import SourceSearch
from dfx_agent.task import snapshot_digest, verify_task
from dfx_agent.task_strategy import build_system_prompt
from dfx_agent.verification import FixedTextExecutor
from dfx_agent.workspace import CandidateWorkspace, TextEdit, _manifest


def multi_file_task(tmp_path: Path) -> Path:
    task = task_snapshot(tmp_path)
    source = task / "source"
    (source / "src/main.ets").write_text("consume(makeValue());\n", encoding="utf-8")
    (source / "src/provider.ets").write_text("export function makeValue() { return bad; }\n", encoding="utf-8")
    manifest = _manifest(source)
    (task / "source-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    info = json.loads((task / "task.json").read_text(encoding="utf-8"))
    info["source_snapshot_sha256"] = snapshot_digest(manifest)
    (task / "task.json").write_text(json.dumps(info), encoding="utf-8")
    return task


def derive(task: Path, root: Path, value: str) -> str:
    workspace = CandidateWorkspace(task, root, "derive")
    workspace.start_draft()
    workspace.apply_text_edits([TextEdit(path="src/provider.ets", old="bad", new=value)])
    return workspace.register_candidate(1).candidate_id


def receipts(work: Path) -> list[dict]:
    with closing(sqlite3.connect(work / "tool-actions.sqlite")) as connection:
        return [json.loads(row[0]) for row in connection.execute(
            "SELECT message_json FROM tool_actions ORDER BY rowid")]


@pytest.mark.parametrize("enhanced", [False, True])
def test_sdk_uses_task_prompt_and_cross_file_search_before_failed_check_revision(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, enhanced: bool) -> None:
    task = multi_file_task(tmp_path)
    almost = derive(task, tmp_path / "derive1", "almost")
    good = derive(task, tmp_path / "derive2", "good")
    features = PolicyFeatures(rule_guidance=enhanced, obligation_tracking=enhanced, decision_checks=enhanced)
    captured = []
    actual_intent = FixedResponseModel._intent

    def capture(self, messages, stop, kwargs):
        captured.append(messages)
        return actual_intent(self, messages, stop, kwargs)

    monkeypatch.setattr(FixedResponseModel, "_intent", capture)
    work = tmp_path / "run"
    report = run_fixed_agent(task, work, "run1", [
        response(1, "read_log"), response(2, "search_source", {"query": "makeValue"}),
        response(3, "read_source", {"path": "src/provider.ets"}),
        response(4, "record_diagnosis", {"path": "src/provider.ets", "line": 1, "reason": "bad producer value"}),
        response(5, "edit_source", {"path": "src/provider.ets", "old": "bad", "new": "almost"}),
        response(6, "register_candidate", {"diagnosis_id": "diagnosis-1"}),
        response(7, "run_check", {"candidate_id": almost}),
        response(8, "search_source", {"query": "almost", "candidate_id": almost}),
        response(9, "edit_source", {"path": "src/provider.ets", "old": "bad", "new": "good"}),
        response(10, "register_candidate", {"diagnosis_id": "diagnosis-1"}),
        response(11, "run_check", {"candidate_id": good}), response(12),
    ], FixedTextExecutor("src/provider.ets", "export function makeValue() { return good; }\n"),
                             policy_features=features)
    assert report["status"] == "offline_checked" and report["application_verification"] == "not_run"
    assert [check["status"] for check in report["checks"]] == ["fail", "pass"]
    assert report["candidates"] == [almost, good]
    assert report["model_calls"] == 12 and report["known_tokens"] == 180
    assert set(report["offered_tools"]) == (set(TOOL_ACTIONS) - KNOWLEDGE_TOOLS - APPLICABILITY_TOOLS)
    assert report["tool_calls"] == 11 and report["rejected_tool_calls"] == 0
    first = captured[0]
    assert isinstance(first[0], SystemMessage)
    assert build_system_prompt(features) in first[0].content
    startup = json.loads(first[-1].content)
    assert startup["source_snapshot_sha256"] == verify_task(task)["source_snapshot_sha256"]
    assert startup["capabilities"] == sorted(OFFLINE_CAPABILITIES)
    saved = ModelCallStore(work / "model-calls.sqlite").get("model-1")
    assert saved is not None and saved.intent.prompt_sha256 == sha256(canonical([
        message.model_dump(mode="json") for message in first]))
    saved_receipts = receipts(work)
    search = json.loads(saved_receipts[1]["content"])
    assert [hit["path"] for hit in search["matches"]] == ["src/main.ets", "src/provider.ets"]
    candidate_search = json.loads(saved_receipts[7]["content"])
    assert candidate_search["scope"] == "candidate" and candidate_search["candidate_id"] == almost
    assert report["actions"][7]["proposal"]["candidate_id"] == almost
    assert (work / "base/src/provider.ets").read_text() == "export function makeValue() { return bad; }\n"


@pytest.mark.parametrize("change", ["prompt", "task_message", "tool_schema", "limits", "old_schema"])
def test_resume_rejects_changed_strategy_tools_or_search_limits_before_model_call(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str) -> None:
    task = task_snapshot(tmp_path)
    work = tmp_path / "run"
    executor = FixedTextExecutor("src/main.ets", "value=good\n")
    scripted = [response(1)]
    run_fixed_agent(task, work, "run1", scripted, executor)
    if change == "prompt":
        original = agent.build_system_prompt
        monkeypatch.setattr(agent, "build_system_prompt", lambda features: original(features) + "changed")
    elif change == "task_message":
        original_message = agent.build_task_message
        monkeypatch.setattr(agent, "build_task_message", lambda **kwargs: original_message(**kwargs) + "changed")
    elif change == "tool_schema":
        original_tools = agent._tools

        def changed_tools(*args, **kwargs):
            tools = original_tools(*args, **kwargs)
            tools[0].description += "changed"
            return tools

        monkeypatch.setattr(agent, "_tools", changed_tools)
    elif change == "limits":
        monkeypatch.setattr(source_search, "MAX_SNIPPET_CHARS", 239)
    else:
        session = json.loads((work / "session.json").read_text())
        session["schema_version"] = 5
        (work / "session.json").write_text(json.dumps(session), encoding="utf-8")
    with pytest.raises(ValueError, match="session identity"):
        run_fixed_agent(task, work, "run1", scripted, executor, resume=True)
    assert ModelCallStore(work / "model-calls.sqlite").usage_summary("task1", "run1")["reserved_calls"] == 1
    assert ToolActionStore(work / "tool-actions.sqlite").records() == []


def test_search_uses_read_capability_actual_candidate_and_shared_tool_budget(tmp_path: Path) -> None:
    task = task_snapshot(tmp_path)
    work = tmp_path / "run"
    report = run_fixed_agent(task, work, "run1", [
        response(1, "search_source", {"query": "bad"}), response(2),
    ], FixedTextExecutor("src/main.ets", "value=good\n"), capabilities=frozenset({"read_log"}))
    assert report["actions"][0]["rejection"] == "capability_unavailable"
    other = run_fixed_agent(task, tmp_path / "other", "run2", [
        response(1, "search_source", {"query": "bad", "candidate_id": "f" * 64}),
        response(2, "search_source", {"query": "bad", "root": "../../data_B"}),
        response(3, "search_source", {"query": "bad"}), response(4),
    ], FixedTextExecutor("src/main.ets", "value=good\n"), max_tool_calls=2)
    assert [action["rejection"] for action in other["actions"]] == [
        "candidate_not_registered", "invalid_proposal_or_arguments", "budget_exhausted"]
    assert other["status"] == "budget_exhausted" and other["candidates"] == []


def test_saved_search_receipt_replays_after_checkpoint_crash_without_rescanning(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    task = task_snapshot(tmp_path)
    work = tmp_path / "run"
    executor = FixedTextExecutor("src/main.ets", "value=good\n")
    scripted = [response(1, "search_source", {"query": "bad"}), response(2)]
    original_search, original_complete = SourceSearch.search, ToolActionStore.complete
    scans = 0

    def search(self, *args, **kwargs):
        nonlocal scans
        scans += 1
        return original_search(self, *args, **kwargs)

    def crash(self, action_id, message):
        original_complete(self, action_id, message)
        raise RuntimeError("checkpoint crash after receipt")

    monkeypatch.setattr(SourceSearch, "search", search)
    monkeypatch.setattr(ToolActionStore, "complete", crash)
    with pytest.raises(RuntimeError, match="checkpoint crash"):
        run_fixed_agent(task, work, "run1", scripted, executor)
    monkeypatch.setattr(ToolActionStore, "complete", original_complete)
    report = run_fixed_agent(task, work, "run1", scripted, executor, resume=True)
    assert scans == 1 and report["tool_calls"] == 1 and report["model_calls"] == 2
    assert json.loads(receipts(work)[0]["content"])["matches"][0]["path"] == "src/main.ets"
