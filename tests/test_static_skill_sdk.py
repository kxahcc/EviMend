"""Actual SDK mock-wire and restore paths keep static reads controlled."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from test_agent_loop import candidate_id, response
from test_metrics import read_json
from test_provider_loop import scripted_adapter, wire_response
from test_readonly_evidence import tree_bytes
from test_static_skills import prepare, scenario

from dfx_agent.actions import ToolActionStore
from dfx_agent.agent import ResumeAdmissionRejected, run_fixed_agent, run_provider_agent
from dfx_agent.applicability_handoff import ApplicabilityPolicy
from dfx_agent.knowledge import KnowledgeStore
from dfx_agent.knowledge_retrieval import KnowledgeRetriever
from dfx_agent.knowledge_run import HostKnowledgeInput
from dfx_agent.static_skills import (
    STATIC_SKILL_MARKER,
    StaticSkillPolicy,
    shared_static_skill_profile,
)
from dfx_agent.verification import FixedTextExecutor
from dfx_agent.working_context_plan import WorkingContextPolicy


@pytest.fixture
def root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("sk")


def prepared_inputs(root: Path, *, enabled: bool = True):
    s = scenario(root)
    profile, _service = prepare(s, enabled=enabled)
    policy = StaticSkillPolicy(enabled=enabled, entry_ids=s.selected)
    approval = s.store.approval.model_copy(update={"run_id": "run2"})
    host = HostKnowledgeInput(KnowledgeRetriever(KnowledgeStore(s.library)), approval)
    return s, root / "runtime", host, policy, profile["files"][0]["skill_id"]


@pytest.mark.parametrize("working_context", [False, True])
def test_mock_wire_static_modes_share_tools_full_body_and_budget_through_failed_repair(
        root: Path, working_context: bool) -> None:
    tools = []
    sessions = []
    bodies = []
    profiles = []
    for enabled in (True, False):
        s, work, host, policy, skill_id = prepared_inputs(root / str(enabled), enabled=enabled)
        almost = candidate_id(s.task, root / str(enabled) / "almost", "almost")
        good = candidate_id(s.task, root / str(enabled) / "good", "good")
        scripted = [
            wire_response(1, [("read_skill", {"skill_id": skill_id})]),
            wire_response(2, [("record_diagnosis", {"path": "src/main.ets", "line": 1,
                                                   "reason": "synthetic bad producer value"})]),
            wire_response(3, [("edit_source", {"path": "src/main.ets", "old": "bad", "new": "almost"})]),
            wire_response(4, [("register_candidate", {"diagnosis_id": "diagnosis-1"})]),
            wire_response(5, [("run_check", {"candidate_id": almost})]),
            wire_response(6, [("edit_source", {"path": "src/main.ets", "old": "bad", "new": "good"})]),
            wire_response(7, [("register_candidate", {"diagnosis_id": "diagnosis-1"})]),
            wire_response(8, [("run_check", {"candidate_id": good})]), wire_response(9),
        ]
        sent = []
        adapter = scripted_adapter(scripted, sent)
        executor = FixedTextExecutor("src/main.ets", "value=good\n", failure_detail="synthetic almost remains wrong")
        external_before = tree_bytes(s.library)
        report = run_provider_agent(s.task, work, "run2", adapter, executor, knowledge=host,
            applicability_policy=ApplicabilityPolicy(enabled=False), static_skill_policy=policy,
            working_context_policy=WorkingContextPolicy(enabled=working_context), max_calls=12,
            max_tool_calls=12, max_candidates=3)
        assert len(sent) == report["model_calls"] == 9
        assert report["known_tokens"] == report["completion"]["cost"]["total_tokens"] == 135
        assert report["tool_calls"] == 8 and report["rejected_tool_calls"] == report["uncertain_tool_calls"] == 0
        assert report["status"] == "offline_checked" and report["application_verification"] == "not_run"
        assert report["completion"]["verification"]["accepted"] is False
        assert report["candidates"] == [almost, good]
        assert [item["status"] for item in report["checks"]] == ["fail", "pass"]
        assert len(executor.calls) == 2
        full_read = next(message for message in sent[1]["messages"] if message["role"] == "tool"
                         and message["tool_call_id"] == "call-1-0")
        result = json.loads(full_read["content"])
        file = report["static_skill_profile"]["files"][0]
        assert result["body"].encode() == (work / file["path"]).read_bytes()
        assert result["metadata"] == file["metadata"] and result["metadata"]["allowed_tools"] == []
        assert result["application_verification"] == "not_run"
        assert report["static_skill_facts"]["resources"][0]["actions"][0]["visible_solver_count"] >= 1
        assert all((STATIC_SKILL_MARKER in message["content"]) is enabled for request in sent
                   for message in request["messages"] if message["role"] == "system")
        assert len(report["offered_tools"]) == 20
        assert {tool["function"]["name"] for tool in sent[0]["tools"]} == set(report["offered_tools"])
        assert tree_bytes(s.library) == external_before
        tools.append(sent[0]["tools"])
        bodies.append(result["body"])
        profiles.append(shared_static_skill_profile(report["static_skill_profile"]))
        sessions.append(read_json(work / "session.json"))
        before = len(sent)
        again = run_provider_agent(s.task, work, "run2", adapter,
            FixedTextExecutor("src/main.ets", "value=good\n", failure_detail="synthetic almost remains wrong"),
            resume=True, knowledge_approval=host.approval,
            applicability_policy=ApplicabilityPolicy(enabled=False), static_skill_policy=policy,
            working_context_policy=WorkingContextPolicy(enabled=working_context), max_calls=12,
            max_tool_calls=12, max_candidates=3)
        assert len(sent) == before and again["model_calls_this_turn"] == 0
    assert tools[0] == tools[1] and bodies[0] == bodies[1] and profiles[0] == profiles[1]
    for key in ("max_calls", "max_known_tokens", "max_seconds", "max_tool_calls", "max_candidates", "capabilities"):
        assert sessions[0][key] == sessions[1][key]


@pytest.mark.parametrize("receipt_saved", [True, False])
def test_fixed_static_read_crash_restores_receipt_without_executing_tool_again(
        root: Path, monkeypatch: pytest.MonkeyPatch, receipt_saved: bool) -> None:
    s, work, host, policy, skill_id = prepared_inputs(root)
    scripted = [response(1, "read_skill", {"skill_id": skill_id}), response(2)]
    original_invoke = ToolActionStore.invoke
    original_complete = ToolActionStore.complete
    executions = 0

    def invoke(self, **kwargs):
        handler = kwargs["handler"]

        def observed_handler():
            nonlocal executions
            if kwargs["tool_name"] == "read_skill":
                executions += 1
            return handler()

        return original_invoke(self, **{**kwargs, "handler": observed_handler})

    def complete(self, action_id, message):
        if message.name != "read_skill":
            return original_complete(self, action_id, message)
        if receipt_saved:
            original_complete(self, action_id, message)
        raise RuntimeError("synthetic static read checkpoint crash")

    monkeypatch.setattr(ToolActionStore, "invoke", invoke)
    monkeypatch.setattr(ToolActionStore, "complete", complete)
    with pytest.raises(RuntimeError, match="static read checkpoint crash"):
        run_fixed_agent(s.task, work, "run2", scripted, FixedTextExecutor("src/main.ets", "value=good\n"),
            knowledge=host, applicability_policy=ApplicabilityPolicy(enabled=False), static_skill_policy=policy)
    assert executions == 1
    originals = tree_bytes(work / "static-skills")
    profile = (work / "static-skills-profile.json").read_bytes()
    monkeypatch.setattr(ToolActionStore, "complete", original_complete)
    s.library.rename(s.library.with_name("library-unavailable"))
    report = run_fixed_agent(s.task, work, "run2", scripted, FixedTextExecutor("src/main.ets", "value=good\n"),
        resume=True, knowledge_approval=host.approval,
        applicability_policy=ApplicabilityPolicy(enabled=False), static_skill_policy=policy)
    assert executions == 1 and tree_bytes(work / "static-skills") == originals
    assert (work / "static-skills-profile.json").read_bytes() == profile
    assert report["tool_calls"] == 1
    facts = report["static_skill_facts"]
    if receipt_saved:
        assert report["uncertain_tool_calls"] == 0 and report["model_calls"] == 2
        assert facts["unread_skill_ids"] == []
        assert facts["resources"][0]["actions"][0]["visible_solver_count"] == 1
    else:
        assert report["uncertain_tool_calls"] == 1 and report["model_calls"] == 1
        assert report["status"] == "unresolved" and report["stopped_reason"] == "action_result_unknown"
        assert facts["unread_skill_ids"] == [skill_id] and facts["resources"][0]["actions"] == []


@pytest.mark.parametrize("change", ["missing_policy", "toggle", "selection", "profile", "body", "support_file"])
def test_static_policy_and_resource_drift_reject_before_writable_resume(root: Path, change: str) -> None:
    s, work, host, policy, _skill_id = prepared_inputs(root)
    scripted = [response(1)]
    report = run_fixed_agent(s.task, work, "run2", scripted, FixedTextExecutor("src/main.ets", "value=good\n"),
        knowledge=host, applicability_policy=ApplicabilityPolicy(enabled=False), static_skill_policy=policy)
    chosen = policy
    if change == "missing_policy":
        chosen = None
    elif change == "toggle":
        chosen = policy.model_copy(update={"enabled": False})
    elif change == "selection":
        chosen = policy.model_copy(update={"entry_ids": (s.second.entry_id,)})
    elif change == "profile":
        value = read_json(work / "static-skills-profile.json")
        value["enabled"] = False
        (work / "static-skills-profile.json").write_text(json.dumps(value), encoding="utf-8")
    elif change == "body":
        body = work / report["static_skill_profile"]["files"][0]["path"]
        body.write_bytes(body.read_bytes().replace(b"Unknown question", b"Unknown QUESTION"))
    else:
        body = work / report["static_skill_profile"]["files"][0]["path"]
        body.with_name("helper.py").write_text("raise RuntimeError('must never run')", encoding="utf-8")
    before = tree_bytes(work)
    with pytest.raises(ResumeAdmissionRejected):
        run_fixed_agent(s.task, work, "run2", scripted, FixedTextExecutor("src/main.ets", "value=good\n"),
            resume=True, knowledge_approval=host.approval,
            applicability_policy=ApplicabilityPolicy(enabled=False), static_skill_policy=chosen)
    assert tree_bytes(work) == before
