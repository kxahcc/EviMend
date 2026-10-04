"""Complete conditional guidance reaches the actual SDK wire with common costs.

The transport and decisions are synthetic; this exercises visibility and handoff,
not autonomous reasoning or successful HarmonyOS repairs.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from test_agent_loop import candidate_id
from test_knowledge_run import inputs
from test_knowledge_sdk import expected_query
from test_provider_loop import settings, wire_response

from dfx_agent.actions import current_solver_call
from dfx_agent.agent import run_provider_agent
from dfx_agent.applicability_handoff import ApplicabilityPolicy
from dfx_agent.model_provider import MOCK_ENDPOINT, ChatCompletionsAdapter
from dfx_agent.verification import FixedTextExecutor
from dfx_agent.working_context_plan import WorkingContextPolicy


def tool_result(body: dict, tool_name: str) -> dict:
    """Find an actual complete sent tool result, including a host quoted group."""
    answers = []
    names = {call["id"]: call["function"]["name"] for message in body["messages"]
             for call in message.get("tool_calls", [])}
    for message in body["messages"]:
        if message.get("role") == "tool" and names.get(message.get("tool_call_id")) == tool_name:
            answers.append(json.loads(message["content"]))
        if message.get("role") != "user" or "required_unrepresented_groups" not in message["content"]:
            continue
        # The marker precedes the complete host-authored panel. Do not scan
        # arbitrary summaries or recover omitted content from disk.
        panel = None
        # Task startup contains a JSON prefix; locate the known host panel by
        # decoding a JSON object at each line boundary, never substring IDs.
        for line in message["content"].splitlines():
            try:
                value = json.loads(line)
            except ValueError:
                continue
            if isinstance(value, dict) and "required_unrepresented_groups" in value:
                panel = value
        if panel is not None:
            for group in panel["required_unrepresented_groups"]:
                for raw in group["messages"]:
                    if raw.get("type") == "tool" and raw.get("name") == tool_name:
                        answers.append(json.loads(raw["content"]))
    assert answers, f"{tool_name} was not sent to the mock provider"
    assert all(answer == answers[0] for answer in answers)
    return answers[-1]


@pytest.mark.parametrize("working_context", [False, True], ids=["ordinary-view", "working-view"])
@pytest.mark.parametrize("state", ["supported", "contradicted", "unknown"])
def test_three_states_send_complete_guidance_then_keep_the_repair_loop(
        tmp_path: Path, working_context: bool, state: str) -> None:
    data = inputs(tmp_path)
    query_id = expected_query(data.host)
    good = candidate_id(data.task, tmp_path / "derive", "good")
    requests = []
    reviewed = None

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal reviewed
        assert str(request.url) == MOCK_ENDPOINT and "authorization" not in request.headers
        body = json.loads(request.content)
        requests.append(body)
        number = len(requests)
        calls = {
            1: [("search_experience", {"tags": ["ByteWidth"]})],
            2: [("record_diagnosis", {"path": "src/main.ets", "line": 1, "reason": "synthetic bad value"})],
            3: [("inspect_analysis", {"diagnosis_id": "diagnosis-1"})],
            4: [("prepare_experience_context", {"query_id": query_id})],
            6: [("edit_source", {"path": "src/main.ets", "old": "bad", "new": "good"})],
            7: [("register_candidate", {"diagnosis_id": "diagnosis-1"})],
            8: [("run_check", {"candidate_id": good})],
        }
        if number == 5:
            prepared = tool_result(body, "prepare_experience_context")
            assert "context" in prepared, prepared
            assert prepared["review_proposal_schema"]["required"]
            context = prepared["context"]
            packet = json.loads(json.loads(context["payload_json"])["packet"]["canonical_payload_json"])
            hit = prepared["guidance"]["retrieval"]["hits"][0]
            focus = packet["focus"]
            evidence = {"evidence_id": next(iter(packet["evidence"])), "scope": "original_analysis"}
            premises = [{"premise_id": premise["premise_id"], "status": state,
                "support": [evidence] if state == "supported" else [],
                "contradictions": [evidence] if state == "contradicted" else [],
                "reason": "synthetic inference, not independent semantic proof"}
                for premise in hit["projection"]["guidance"]["premises"]]
            proposal = {"context_id": context["context_id"], "entry_id": hit["entry_id"],
                "content_sha256": hit["content_sha256"], "projection_sha256": hit["projection_sha256"],
                "task_id": packet["task_id"], "run_id": packet["run_id"],
                "source_snapshot_sha256": packet["source_snapshot_sha256"],
                "diagnosis_id": focus["diagnosis_id"], "hypothesis_ids": focus["hypothesis_ids"],
                "candidate_id": None, "candidate_snapshot_sha256": None, "premises": premises}
            calls[number] = [("review_experience", {"applicability_json": json.dumps(proposal)})]
        if number == 6:
            reviewed = tool_result(body, "review_experience")
            assert reviewed["guidance"]["status"] == state
            assert reviewed["guidance"]["semantic_authority"] == "model_inference"
            assert reviewed["guidance"]["projection"]["guidance"]["procedure"]
            assert reviewed["guidance"]["projection"]["guidance"]["counterexamples"]
            if state == "unknown":
                original = reviewed["guidance"]["projection"]["guidance"]["premises"]
                assert [question["question"] for question in reviewed["guidance"]["questions"]] == [
                    premise["unknown_question"] for premise in original]
        assert number <= 9
        return httpx.Response(200, json=wire_response(number, calls.get(number)))

    adapter = ChatCompletionsAdapter(settings(), httpx.MockTransport(handler))
    work = tmp_path / "sdk-run"
    report = run_provider_agent(data.task, work, "run1", adapter,
        FixedTextExecutor("src/main.ets", "value=good\n"), knowledge=data.host,
        applicability_policy=ApplicabilityPolicy(enabled=True),
        working_context_policy=WorkingContextPolicy(enabled=working_context))
    assert reviewed is not None, {"requests": len(requests), "status": report["status"],
        "stopped_reason": report["stopped_reason"], "adapter_error_reason": report["adapter_error_reason"]}
    assert report["schema_version"] == 9 and report["completion"]["schema_version"] == 6
    assert len(requests) == report["model_calls"] == 9
    assert report["known_tokens"] == 135 and report["tool_calls"] == 8
    assert report["status"] == "offline_checked", report
    assert report["completion"]["verification"]["accepted"] is False
    assert report["application_verification"] == "not_run"
    assert len(list((work / "solver-inputs").glob("*.json"))) == 9
    assert len(list((work / "applicability-contexts").glob("*.json"))) == 1
    assert len(list((work / "applicability-reviews").glob("*.json"))) == 1
    assert current_solver_call() is None


def test_disabled_sdk_review_keeps_ordinary_guidance_without_parsing_premises(tmp_path: Path) -> None:
    data = inputs(tmp_path)
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append(body)
        number = len(requests)
        calls = {
            1: [("search_experience", {"tags": ["ByteWidth"]})],
            2: [("review_experience", {"applicability_json": "not-json"})],
        }
        if number == 3:
            result = tool_result(body, "review_experience")
            assert result["status"] == "disabled"
            assert result["next_action"] == "ordinary_retrieval"
            assert result["guidance"]["retrieval"]["hits"][0]["projection"]["guidance"]["procedure"]
        assert number <= 3
        return httpx.Response(200, json=wire_response(number, calls.get(number)))

    adapter = ChatCompletionsAdapter(settings(), httpx.MockTransport(handler))
    work = tmp_path / "sdk-run"
    report = run_provider_agent(data.task, work, "run1", adapter,
        FixedTextExecutor("src/main.ets", "value=good\n"), knowledge=data.host,
        applicability_policy=ApplicabilityPolicy(enabled=False))
    assert len(requests) == report["model_calls"] == 3, report
    assert report["known_tokens"] == 45 and report["tool_calls"] == 2
    assert report["applicability_profile"] == {"schema_version": 1, "enabled": False}
    assert len(requests[0]["tools"]) == 19
    assert not (work / "applicability-contexts").exists()
    assert not (work / "applicability-reviews").exists()
    assert current_solver_call() is None
