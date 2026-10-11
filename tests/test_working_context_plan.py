"""Frozen SDK sending views from real synthetic tools and accounted receipts.

These component tests use explicit synthetic host authorization. SDK middleware
authorization is exercised separately; no real model or application is called.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, Literal

import httpx
import pytest
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from pydantic import ValidationError
from test_agent_loop import task_snapshot
from test_decisions import FULL, decision_request, diagnosis_submission

from dfx_agent import working_context_plan
from dfx_agent.actions import ToolActionStore
from dfx_agent.agent import _tools
from dfx_agent.context_compaction import CompactionPolicy, CompactionStore
from dfx_agent.model_calls import (
    ModelCallIntent,
    ModelCallStore,
    canonical,
    invoke_accounted,
    sha256,
)
from dfx_agent.model_provider import (
    ChatCompletionsAdapter,
    PreparedRequest,
    ProviderSettings,
)
from dfx_agent.task import digest, snapshot_digest, verify_task
from dfx_agent.verification import CheckLedger, FixedTextExecutor
from dfx_agent.working_context import WorkingContextError, WorkingContextPacket
from dfx_agent.working_context_plan import (
    WORKING_CONTEXT_PROMPT,
    WorkingContextPlan,
    WorkingContextPolicy,
    WorkingContextStore,
    _latest_failure,
    _select_messages,
)
from dfx_agent.working_context_projection import project_working_context
from dfx_agent.workspace import CandidateWorkspace

FIXED = {"transport": "fixed_response", "provider_id": "dfxfixed", "model_id": "offline"}


def message_json(messages: list[BaseMessage]) -> str:
    return canonical([message.model_dump(mode="json") for message in messages])


def fixed_intent(plan: WorkingContextPlan, view: list[BaseMessage], names: list[str]):
    payload = {"messages": [message.model_dump(mode="json") for message in view],
        "tools": [{"name": name, "description": "synthetic tool", "parameters": {}} for name in names],
        "parameters": {"stop": None, "generate_kwargs": {}, "bind_kwargs": {}}}
    return ModelCallIntent(call_id=plan.parent_call_id, task_id=plan.task_id, run_id=plan.run_id, kind="solver",
        provider_id="dfxfixed", model_id="offline", prompt_sha256=sha256(canonical(payload["messages"])),
        tool_schema_sha256=sha256(canonical(payload["tools"])), parameters_sha256=sha256(canonical(payload["parameters"])),
        tool_names=tuple(names)), payload


class SyntheticHost:
    def __init__(self, root: Path, *, max_view_bytes: int = 786_432,
                 history_selection: Literal["legacy_refill", "repair_relevance", "repair_frontier"] = "legacy_refill",
                 exploration_read_groups: int = 6, extra_sources: dict[str, str] | None = None,
                 crash_text: str | None = None, context_layout: Literal["record_panel", "decision_layers"] = "record_panel"):
        self.task = task_snapshot(root)
        if crash_text is not None:
            (self.task / "crash.log").write_bytes(crash_text.encode("utf-8"))
            identity_path = self.task / "task.json"
            identity = json.loads(identity_path.read_text(encoding="utf-8"))
            identity["crash_sha256"] = digest(crash_text.encode("utf-8"))
            identity_path.write_text(json.dumps(identity), encoding="utf-8")
        if extra_sources:
            manifest_path = self.task / "source-manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            for relative, content in extra_sources.items():
                target = self.task / "source" / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content.encode("utf-8"))
                manifest.append({"path": relative, "sha256": digest(content.encode("utf-8"))})
            manifest.sort(key=lambda row: row["path"])
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            task_path = self.task / "task.json"
            identity = json.loads(task_path.read_text(encoding="utf-8"))
            identity["source_snapshot_sha256"] = snapshot_digest(manifest)
            task_path.write_text(json.dumps(identity), encoding="utf-8")
        self.workspace = CandidateWorkspace(self.task, root / "run", "run1")
        self.ledger = CheckLedger(self.workspace, FixedTextExecutor("src/main.ets", "value=good\n"))
        self.tools = {tool.name: tool for tool in _tools(self.workspace, self.ledger, 5, self.task, "crash.log",
                     verify_task(self.task)["crash_sha256"], FULL)}
        self.actions = ToolActionStore(self.workspace.root / "tool-actions.sqlite")
        self.calls = ModelCallStore(self.workspace.root / "model-calls.sqlite")
        self.policy = WorkingContextPolicy(enabled=True, max_view_bytes=max_view_bytes, retain_groups=1,
                                           history_selection=history_selection, exploration_read_groups=exploration_read_groups,
                                           context_layout=context_layout)
        self.store = WorkingContextStore(self.workspace, self.task, self.ledger, self.policy, FIXED)
        self.messages: list[BaseMessage] = [SystemMessage(content="synthetic host policy"),
            HumanMessage(content="Repair the synthetic task; application verification is unavailable.")]
        self.number = 1

    def plan(self) -> WorkingContextPlan:
        selected = self.store.select(self.messages, f"model-{self.number}", self.calls, self.actions)
        assert selected is not None
        return selected

    def step(self, name: str | None, args: dict[str, Any] | None = None, *, text: str = "unknown") -> Any:
        plan = self.plan()
        view = plan.view_messages()
        intent, payload = fixed_intent(plan, view, list(self.tools))
        self.calls.reserve(intent, max_calls=100)
        self.store.link(plan, intent, None, view_messages=view, compaction=None, fixed_request=payload)
        call = None if name is None else {"name": name, "args": args or {}, "id": f"tool-{self.number}"}
        response = AIMessage(content=text if name is None else "", tool_calls=[] if call is None else [call],
            response_metadata={"dfx_model_call_id": intent.call_id},
            usage_metadata={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15})
        saved, _ = invoke_accounted(self.calls, intent, lambda: response, max_calls=100)
        self.messages.append(saved)
        self.number += 1
        if call is None:
            return None
        call = saved.tool_calls[0]
        identifier = "action-" + sha256(canonical(["task1", "run1", intent.call_id, call["id"]]))
        receipt = self.actions.invoke(action_id=identifier, request_sha256=sha256(canonical(call)),
            model_call_id=intent.call_id, tool_name=call["name"], tool_call_id=call["id"], proposal=None,
            rejection=None, max_tool_calls=100, handler=lambda: ToolMessage(
                content=self.tools[call["name"]].invoke(call["args"]), name=call["name"], tool_call_id=call["id"]))
        self.messages.append(receipt)
        assert isinstance(receipt.content, str) and not receipt.content.startswith("rejected:")
        try:
            return json.loads(receipt.content)
        except json.JSONDecodeError:
            return receipt.content

    def diagnosis(self) -> None:
        self.step("record_diagnosis", {"diagnosis_json": json.dumps(diagnosis_submission(self.task))})

    def candidate(self, replacement: str, *, diagnosis: str = "diagnosis1", decision: str | None = None) -> str:
        self.step("edit_source", {"path": "src/main.ets", "old": "bad", "new": replacement})
        args: dict[str, Any] = {"diagnosis_id": diagnosis}
        if diagnosis:
            args["preserves"] = ["keep_value"]
        if decision is not None:
            args.update(decision_id=decision, direction_id="repair1")
        return self.step("register_candidate", args)["candidate_id"]


def frozen_files(directory: Path) -> dict[str, bytes]:
    return {path.relative_to(directory).as_posix(): path.read_bytes()
            for path in directory.rglob("*") if path.is_file()}


def packet(plan: WorkingContextPlan) -> dict[str, Any]:
    return WorkingContextPacket.model_validate_json(plan.packet_json).contents()


def test_initial_unknown_packet_is_frozen_and_views_are_independent(tmp_path: Path) -> None:
    host = SyntheticHost(tmp_path)
    before = message_json(host.messages)
    selected = host.plan()
    assert packet(selected)["focus"]["diagnosis_id"] is None
    assert selected.selection_ms >= 0
    assert message_json(host.messages) == before
    view = selected.view_messages()
    assert WORKING_CONTEXT_PROMPT in str(view[1].content)
    view[1].content = "mutated view"
    assert selected.view_messages()[1].content != "mutated view"
    restored = WorkingContextPlan.model_validate_json(selected.model_dump_json())
    assert restored == selected
    with pytest.raises(ValidationError):
        selected.selection_ms = 0  # type: ignore[misc]
    audited = host.store.audit(host.calls, host.actions, [])
    assert audited[0]["selection_ms"] == selected.selection_ms
    assert audited[0]["original_bytes"] == len(before.encode("utf-8"))
    assert audited[0]["stage_bytes"] == len(selected.stage_messages_json.encode("utf-8"))
    assert audited[0]["final_bytes"] is None and audited[0]["materialized_bytes"] > 0


@pytest.mark.parametrize("history_selection", ["legacy_refill", "repair_relevance"])
def test_actual_failed_A_remains_separate_from_current_B(tmp_path: Path,
        history_selection: Literal["legacy_refill", "repair_relevance"]) -> None:
    host = SyntheticHost(tmp_path, history_selection=history_selection)
    host.diagnosis()
    initial = host.step("plan_repair_action", {"plan_json": json.dumps(decision_request(host.task))})
    old = host.candidate("almost", decision=initial["record_id"])
    host.step("run_check", {"candidate_id": old})
    feedback = host.step("link_failed_check", {"candidate_id": old, "violated_obligation_ids": ["keep_value"]})
    request = {**decision_request(host.task), "candidate_id": old, "feedback_id": feedback["feedback_id"]}
    historical = host.step("plan_repair_action", {"plan_json": json.dumps(request)})
    current = host.candidate("good", decision=initial["record_id"])
    selected = host.plan()
    value = packet(selected)
    assert value["focus"]["current_candidate_id"] == current
    assert set(value["candidates"]) == {old, current}
    assert value["checks"][old]["result"]["status"] == "fail"
    assert value["checks"][current]["state"] == "not_requested"
    relevance = value["historical_relevance"][historical["record_id"]]
    assert relevance["observed_candidate_id"] == old and relevance["current_candidate_id"] == current
    assert not relevance["is_current_candidate_observation"] and relevance["semantic_relevance"] == "unknown"
    assert value["application_verification"] == "not_run"
    assert len(host.store.audit(host.calls, host.actions, [])) == host.number


@pytest.mark.parametrize("history_selection", ["legacy_refill", "repair_relevance"])
def test_historical_plan_replays_after_legal_checks_and_unrelated_records_progress(tmp_path: Path,
        history_selection: Literal["legacy_refill", "repair_relevance"]) -> None:
    host = SyntheticHost(tmp_path, history_selection=history_selection)
    host.diagnosis()
    identifier = host.candidate("good")
    original = [message.model_copy(deep=True) for message in host.messages]
    selected = host.plan()
    assert packet(selected)["checks"][identifier]["state"] == "not_requested"
    host.step("run_check", {"candidate_id": identifier})
    host.step("record_obligation", {"obligation_id": "other", "statement": "Unselected unknown behavior",
                                  "evidence_ids": ["source1"]})
    before = frozen_files(host.workspace.root / "working-context")
    restored = host.store.select(original, selected.parent_call_id, host.calls, host.actions)
    assert restored == selected
    assert packet(restored)["checks"][identifier]["state"] == "not_requested"
    readonly = WorkingContextStore(host.workspace, None, None, host.policy, FIXED)
    assert readonly.audit(host.calls, host.actions, [])
    assert frozen_files(host.workspace.root / "working-context") == before
    assert len(host.ledger.executor.requests) == 1


@pytest.mark.parametrize("mode", ["direct_ledger", "invisible_tool"])
def test_new_plan_refuses_check_results_without_visible_authorized_receipt(tmp_path: Path, mode: str) -> None:
    host = SyntheticHost(tmp_path)
    host.diagnosis()
    identifier = host.candidate("good")
    original = [message.model_copy(deep=True) for message in host.messages]
    parent = f"model-{host.number}"
    if mode == "direct_ledger":
        host.ledger.run(identifier)
    else:
        host.step("run_check", {"candidate_id": identifier})
        # A distinct future site has no saved plan for this earlier input.
        parent = f"model-{host.number}"
    with pytest.raises(WorkingContextError, match="working_context_plan_invalid"):
        host.store.select(original, parent, host.calls, host.actions)


def test_generic_old_candidate_failure_and_standalone_obligation_are_exact_required_groups(tmp_path: Path) -> None:
    host = SyntheticHost(tmp_path)
    host.diagnosis()
    host.step("record_obligation", {"obligation_id": "standalone", "statement": "Preserve an unknown extra output",
                                  "evidence_ids": ["source1"]})
    old = host.candidate("almost", diagnosis="")
    host.step("run_check", {"candidate_id": old})
    for _ in range(3):
        host.step("read_source", {"path": "src/main.ets"})
    selected = host.plan()
    value = packet(selected)
    assert not value["candidates"] and "standalone" not in value["obligations"]
    panel = json.loads(str(selected.view_messages()[1].content).split(WORKING_CONTEXT_PROMPT + "\n", 1)[1])
    names = [group["messages"][0]["tool_calls"][0]["name"] for group in panel["required_unrepresented_groups"]]
    assert names == ["record_obligation", "register_candidate", "run_check"]
    assert "read_source" not in names
    fail_group = panel["required_unrepresented_groups"][-1]
    assert json.loads(fail_group["messages"][1]["content"])["status"] == "fail"


def test_selection_keeps_complete_recent_groups_and_omits_optional_old_groups(tmp_path: Path) -> None:
    host = SyntheticHost(tmp_path, max_view_bytes=30_000)
    host.diagnosis()
    for index in range(4):
        host.step(None, text=f"unconfirmed reading {index}: " + "字" * 2400)
    selected = host.plan()
    selection = json.loads(selected.selection_json)
    assert selection["omitted_ranges"] and selection["retained_ranges"][-1] == [len(host.messages) - 1, len(host.messages)]
    assert len(selected.stage_messages_json.encode("utf-8")) <= host.policy.max_view_bytes
    assert len(host.messages) == 8


@pytest.mark.parametrize("history_selection", ["legacy_refill", "repair_relevance"])
def test_required_budget_failure_does_not_reserve_or_write_plan(tmp_path: Path,
        history_selection: Literal["legacy_refill", "repair_relevance"]) -> None:
    host = SyntheticHost(tmp_path, max_view_bytes=512, history_selection=history_selection)
    before = frozen_files(tmp_path)
    with pytest.raises(WorkingContextError, match="working_context_required_limit"):
        host.plan()
    assert host.calls.usage_summary("task1", "run1")["reserved_calls"] == 0
    assert frozen_files(tmp_path) == before


def test_legacy_policy_keeps_original_serialization_and_selection_shape(tmp_path: Path) -> None:
    expected = {"enabled": False, "max_packet_bytes": 131_072, "max_view_bytes": 786_432, "retain_groups": 2}
    assert WorkingContextPolicy().model_dump(mode="json") == expected
    assert json.loads(WorkingContextPolicy(history_selection="legacy_refill").model_dump_json()) == expected
    assert WorkingContextPolicy().model_dump(mode="json", exclude_defaults=True) == {}
    assert WorkingContextPolicy().model_dump(mode="json", exclude_unset=True) == {}
    assert WorkingContextPolicy().model_dump(mode="json", exclude={"history_selection"}) == expected
    assert WorkingContextPolicy.model_validate_json(canonical(expected)).history_selection == "legacy_refill"
    host = SyntheticHost(tmp_path)
    host.step(None, text="old unconfirmed thought")
    selected = host.plan()
    assert "history_selection" not in selected.model_dump(mode="json")["policy"]
    selection = json.loads(selected.selection_json)
    assert "history_selection" not in selection and "group_decisions" not in selection
    assert not selection["omitted_ranges"]
    assert WorkingContextPlan.model_validate_json(selected.model_dump_json()) == selected


def test_frontier_keeps_baseline_and_candidate_reads_separate_and_deduplicates_same_version(tmp_path: Path) -> None:
    host = SyntheticHost(tmp_path, history_selection="repair_frontier")
    host.diagnosis()
    candidate = host.candidate("good")
    host.step("read_source", {"path": "src/main.ets", "candidate_id": candidate})
    host.step("read_source", {"path": "src/main.ets"})
    host.step("read_source", {"path": "src/main.ets", "candidate_id": candidate})
    host.step(None, text="candidate values are not baseline evidence")
    selected = host.plan()
    reads = [call for message in selected.view_messages() if type(message) is AIMessage
             for call in message.tool_calls if call["name"] == "read_source"]
    assert [call["id"] for call in reads] == ["tool-5", "tool-6"]
    decisions = json.loads(selected.selection_json)["group_decisions"]
    assert any(row["reason"] == "duplicate_versioned_read" and not row["retained"] for row in decisions)


def test_repair_selection_removes_repeated_and_unrelated_history_with_large_surplus(tmp_path: Path) -> None:
    host = SyntheticHost(tmp_path, history_selection="repair_relevance")
    host.step("read_source", {"path": "src/main.ets"})
    host.step("read_source", {"path": "src/main.ets"})
    host.step("read_log", {})
    host.step(None, text="old unconfirmed thought: " + "x" * 30_000)
    host.diagnosis()
    host.step(None, text="current thought remains unknown")
    original = message_json(host.messages)
    selected = host.plan()
    view = selected.view_messages()
    reads = [call for message in view if type(message) is AIMessage for call in message.tool_calls]
    assert [call["name"] for call in reads] == ["read_source"]
    assert reads[0]["id"] == "tool-2"
    assert "old unconfirmed thought" not in selected.stage_messages_json
    assert "Preserve the expected value" in selected.stage_messages_json
    assert message_json(host.messages) == original
    decisions = json.loads(selected.selection_json)["group_decisions"]
    assert any(row["reason"] == "duplicate_versioned_read" and not row["retained"] for row in decisions)
    assert len(selected.stage_messages_json.encode("utf-8")) < host.policy.max_view_bytes // 2
    assert len(decisions) == 6
    assert WorkingContextPlan.model_validate_json(selected.model_dump_json()) == selected


def test_frontier_preserves_cross_file_exploration_before_first_diagnosis(tmp_path: Path) -> None:
    sources = {"src/caller.ets": "invoke producer then consumer\n", "src/producer.ets": "allocate buffer\n",
               "src/consumer.ets": "check complete result\n"}
    host = SyntheticHost(tmp_path, history_selection="repair_frontier", extra_sources=sources)
    host.step("read_log", {})
    for path in ["src/main.ets", *sources]:
        host.step("read_source", {"path": path})
    host.step("read_source", {"path": "src/main.ets"})
    for _ in range(3):
        host.step(None, text="unconfirmed reasoning is not a substitute for observations")
    original = message_json(host.messages)
    selected = host.plan()
    calls = [call for message in selected.view_messages() if type(message) is AIMessage for call in message.tool_calls]
    assert {call["args"]["path"] for call in calls if call["name"] == "read_source"} == {"src/main.ets", *sources}
    assert [call["id"] for call in calls if call["name"] == "read_source" and call["args"]["path"] == "src/main.ets"] == ["tool-6"]
    assert [call["name"] for call in calls].count("read_log") == 1
    assert packet(selected)["focus"]["diagnosis_id"] is None
    assert message_json(host.messages) == original
    assert WorkingContextPlan.model_validate_json(selected.model_dump_json()) == selected


def test_frontier_exploration_after_diagnosis_is_not_assumed_to_support_it(tmp_path: Path) -> None:
    host = SyntheticHost(tmp_path, history_selection="repair_frontier",
                         extra_sources={"src/other.ets": "independent unexplained behavior\n"})
    host.diagnosis()
    host.step("read_source", {"path": "src/other.ets"})
    for _ in range(3):
        host.step(None, text="the registered focus may be wrong")
    selected = host.plan()
    assert "independent unexplained behavior" in selected.stage_messages_json
    assert all(fact["location"] is None or fact["location"]["path"] != "src/other.ets"
               for fact in packet(selected)["evidence"].values())
    assert any(row["reason"] == "unabsorbed_exploration_read" and row["retained"]
               for row in json.loads(selected.selection_json)["group_decisions"])


def test_frontier_preserves_early_fault_page_before_later_trace_page(tmp_path: Path) -> None:
    host = SyntheticHost(tmp_path, history_selection="repair_frontier",
                         crash_text="first page: observed failure\nsecond page: surrounding trace\n")
    host.step("read_log", {"start_line": 1, "end_line": 1})
    host.step("read_log", {"start_line": 2, "end_line": 2})
    host.step("read_source", {"path": "src/main.ets"})
    for _ in range(3):
        host.step(None, text="a later trace does not replace the original fault")
    selected = host.plan()
    calls = [call for message in selected.view_messages() if type(message) is AIMessage for call in message.tool_calls]
    assert [call["args"]["start_line"] for call in calls if call["name"] == "read_log"] == [1, 2]
    assert "observed failure" in selected.stage_messages_json and "surrounding trace" in selected.stage_messages_json


def test_frontier_cap_and_common_byte_budget_do_not_refill_every_old_read(tmp_path: Path) -> None:
    sources = {f"src/file{index}.ets": f"observed file {index}\n" for index in range(4)}
    host = SyntheticHost(tmp_path, history_selection="repair_frontier", exploration_read_groups=2, extra_sources=sources)
    host.step("read_log", {})
    for path in sources:
        host.step("read_source", {"path": path})
    host.step(None, text="current unknown")
    selected = host.plan()
    calls = [call for message in selected.view_messages() if type(message) is AIMessage for call in message.tool_calls]
    assert [call["args"]["path"] for call in calls if call["name"] == "read_source"] == ["src/file2.ets", "src/file3.ets"]
    base = [message for message in selected.view_messages() if not (
        type(message) is AIMessage and any(call["name"] == "read_source" for call in message.tool_calls) or
        type(message) is ToolMessage and message.name == "read_source")]
    policy = host.policy.model_copy(update={"max_view_bytes": len(message_json(base).encode("utf-8"))})
    audit_packet = WorkingContextPacket.model_validate_json(selected.packet_json)
    projection = project_working_context(audit_packet, max_bytes=policy.max_packet_bytes)
    view, selection = _select_messages(host.messages, audit_packet,
                                      json.loads(selected.visible_receipts_json), policy, projection=projection)
    assert not any(message.name == "read_source" for message in view if type(message) is ToolMessage)
    assert "synthetic crash" in message_json(view)
    assert any(row["reason"] == "exploration_exceeds_view_limit" and not row["retained"]
               for row in selection["group_decisions"])
    with pytest.raises(WorkingContextError, match="working_context_required_limit"):
        _select_messages(host.messages, audit_packet, json.loads(selected.visible_receipts_json),
                         policy.model_copy(update={"max_view_bytes": 512}), projection=projection)


def test_frontier_keeps_original_failure_when_root_and_obligation_focus_change(tmp_path: Path) -> None:
    host = SyntheticHost(tmp_path, history_selection="repair_frontier")
    host.diagnosis()
    old = host.candidate("almost")
    observed = host.step("run_check", {"candidate_id": old})
    latest = host.step("record_diagnosis", {"path": "src/main.ets", "line": 1, "reason": "different root, still unknown"})
    for _ in range(3):
        host.step(None, text="new focus does not erase the old observed business failure")
    selected = host.plan()
    assert packet(selected)["focus"]["diagnosis_id"] == latest["diagnosis_id"]
    assert old not in packet(selected)["candidates"] and not packet(selected)["obligations"]
    panel = json.loads(str(selected.view_messages()[1].content).split(WORKING_CONTEXT_PROMPT + "\n", 1)[1])
    failure = panel["latest_failure"]
    assert failure["candidate_id"] == old and failure["diagnosis_id"] == "diagnosis1"
    assert failure["check_result"] == observed and failure["causal_relevance"] == "unknown"
    assert failure["is_current_candidate_observation"] is False
    names = [group["messages"][0]["tool_calls"][0]["name"] for group in panel["required_unrepresented_groups"]]
    assert names == ["register_candidate", "run_check"]
    assert "record_diagnosis" not in names
    assert WorkingContextPlan.model_validate_json(selected.model_dump_json()) == selected


def test_frontier_failure_history_is_bounded_and_cleared_only_by_actual_pass(tmp_path: Path) -> None:
    host = SyntheticHost(tmp_path, history_selection="repair_frontier")
    host.diagnosis()
    first = host.candidate("almost")
    host.step("run_check", {"candidate_id": first})
    last = host.candidate("wrong")
    host.step("run_check", {"candidate_id": last})
    host.step("record_diagnosis", {"path": "src/main.ets", "line": 1, "reason": "start a new focus"})
    host.step(None, text="do not retain every previous failure")
    selected = host.plan()
    calls = [call for message in selected.view_messages() if type(message) is AIMessage for call in message.tool_calls]
    assert [call["args"]["candidate_id"] for call in calls if call["name"] == "run_check"] == [last]
    good = host.candidate("good")
    host.step("run_check", {"candidate_id": good})
    host.step("record_diagnosis", {"path": "src/main.ets", "line": 1, "reason": "later unknown focus"})
    host.step(None, text="a model thought alone did not clear the failure")
    selected = host.plan()
    panel = json.loads(str(selected.view_messages()[1].content).split(WORKING_CONTEXT_PROMPT + "\n", 1)[1])
    assert panel["latest_failure"] is None
    assert not any(call["name"] == "run_check" for message in selected.view_messages()
                   if type(message) is AIMessage for call in message.tool_calls)


def test_frontier_parameters_are_frozen_without_changing_old_profile_shapes() -> None:
    old = WorkingContextPolicy(history_selection="repair_relevance").model_dump(mode="json")
    assert "exploration_read_groups" not in old
    assert WorkingContextPolicy.model_validate_json(canonical(old)).model_dump(mode="json") == old
    current = WorkingContextPolicy(history_selection="repair_frontier", exploration_read_groups=3)
    assert WorkingContextPolicy.model_validate_json(current.model_dump_json()) == current
    assert current.model_dump(mode="json")["exploration_read_groups"] == 3
    with pytest.raises(ValidationError, match="exploration_read_groups requires repair_frontier"):
        WorkingContextPolicy(exploration_read_groups=3)
    with pytest.raises(ValidationError, match="decision_layers requires repair_frontier"):
        WorkingContextPolicy(context_layout="decision_layers")


def test_decision_layers_cannot_hide_a_required_behavior_before_plan_or_reservation(tmp_path: Path, monkeypatch) -> None:
    host = SyntheticHost(tmp_path, history_selection="repair_frontier", context_layout="decision_layers")
    host.diagnosis()
    original = working_context_plan.build_decision_context

    def incomplete(value, **kwargs):
        layout = original(value, **kwargs)
        layout["obligations_by_kind"]["business_behavior"].clear()
        return layout

    monkeypatch.setattr(working_context_plan, "build_decision_context", incomplete)
    before = frozen_files(host.workspace.root)
    usage = host.calls.usage_summary("task1", "run1")
    with pytest.raises(WorkingContextError, match="working_context_layout_content_loss"):
        host.plan()
    assert frozen_files(host.workspace.root) == before and host.calls.usage_summary("task1", "run1") == usage


@pytest.mark.parametrize("status,protocol,level", [
    ("inconclusive", "same", "application_execution"),
    ("pass", "different", "application_execution"),
    ("pass", "same", "offline_synthetic"),
])
def test_frontier_unknown_or_incompatible_pass_does_not_clear_failure(status: str, protocol: str, level: str) -> None:
    # Pure selection helper: not an application execution or SDK authentication fixture.
    def receipt(answer: dict[str, Any]) -> dict[str, Any]:
        return {"action": {"authorization": "allowed", "tool_name": "run_check"},
                "receipt": {"content": canonical(answer)}}

    failure = receipt({"status": "fail", "protocol_sha256": "same", "level": "application_execution"})
    following = receipt({"status": status, "protocol_sha256": protocol, "level": level})
    assert _latest_failure([failure, following]) is failure


def test_repair_selection_prioritizes_frozen_contradicting_read_under_limit(tmp_path: Path) -> None:
    host = SyntheticHost(tmp_path, history_selection="repair_relevance")
    host.step("read_log", {})
    host.step("read_source", {"path": "src/main.ets"})
    raw = diagnosis_submission(host.task)
    raw["log_citations"] = [{"evidence_id": "log1", "start_line": 1, "end_line": 1}]
    raw["diagnosis"]["hypotheses"][0]["contradicting_evidence_ids"] = ["log1"]
    host.step("record_diagnosis", {"diagnosis_json": json.dumps(raw)})
    host.step(None, text="current repair focus")
    frozen = host.plan()
    # Budget the existing complete panel/recent group plus only the older
    # contradiction read. Newer source reading must not displace it.
    source_ids = {call["id"] for message in frozen.view_messages() if type(message) is AIMessage
                  for call in message.tool_calls if call["name"] == "read_source"}
    budget_view = [message for message in frozen.view_messages() if not (
        type(message) is AIMessage and any(call["id"] in source_ids for call in message.tool_calls) or
        type(message) is ToolMessage and message.tool_call_id in source_ids)]
    policy = host.policy.model_copy(update={"max_view_bytes": len(message_json(budget_view).encode("utf-8"))})
    audit_packet = WorkingContextPacket.model_validate_json(frozen.packet_json)
    view, selection = _select_messages(host.messages, audit_packet, json.loads(frozen.visible_receipts_json), policy,
                                      projection=project_working_context(audit_packet, max_bytes=policy.max_packet_bytes))
    assert [call["name"] for message in view if type(message) is AIMessage for call in message.tool_calls] == ["read_log"]
    assert any(row["reason"] == "related_evidence_exceeds_view_limit" and not row["retained"]
               for row in selection["group_decisions"])


def test_repair_selection_does_not_force_old_unrelated_candidate_or_diagnosis(tmp_path: Path) -> None:
    host = SyntheticHost(tmp_path, history_selection="repair_relevance")
    host.diagnosis()
    old = host.candidate("almost")
    host.step("run_check", {"candidate_id": old})
    latest = host.step("record_diagnosis", {"path": "src/main.ets", "line": 1, "reason": "new unconfirmed root"})
    host.step(None, text="retain only the current repair focus")
    selected = host.plan()
    assert packet(selected)["focus"]["diagnosis_id"] == latest["diagnosis_id"]
    assert old not in packet(selected)["candidates"]
    names = [call["name"] for message in selected.view_messages() if type(message) is AIMessage for call in message.tool_calls]
    assert "register_candidate" not in names and "run_check" not in names
    assert not json.loads(selected.selection_json)["required_unrepresented_ranges"]


def test_repair_selection_keeps_successful_unregistered_edit_after_recent_thoughts(tmp_path: Path) -> None:
    host = SyntheticHost(tmp_path, history_selection="repair_relevance")
    host.diagnosis()
    registered = host.candidate("good")
    host.step("edit_source", {"path": "src/main.ets", "old": "bad", "new": "almost"})
    for _ in range(3):
        host.step(None, text="draft has not yet been frozen")
    selected = host.plan()
    assert packet(selected)["focus"]["current_candidate_id"] == registered
    calls = [call for message in selected.view_messages() if type(message) is AIMessage for call in message.tool_calls]
    assert [call["args"]["new"] for call in calls if call["name"] == "edit_source"] == ["almost"]
    assert any(row["reason"] == "unregistered_draft_edit" and row["retained"]
               for row in json.loads(selected.selection_json)["group_decisions"])


def test_repair_selection_keeps_current_unrepresented_behavior_obligation(tmp_path: Path) -> None:
    host = SyntheticHost(tmp_path, history_selection="repair_relevance")
    host.diagnosis()
    host.step("record_obligation", {"obligation_id": "extra_behavior", "statement": "Keep an unresolved output",
                                  "evidence_ids": ["source1"]})
    host.step(None, text="recent thought does not replace the extra behavior")
    selected = host.plan()
    panel = json.loads(str(selected.view_messages()[1].content).split(WORKING_CONTEXT_PROMPT + "\n", 1)[1])
    assert "extra_behavior" not in panel["packet"]["obligations"]
    assert panel["required_unrepresented_groups"][0]["messages"][0]["tool_calls"][0]["name"] == "record_obligation"
    assert "Keep an unresolved output" in selected.stage_messages_json


def test_repair_selection_keeps_related_failure_without_a_followup_decision(tmp_path: Path) -> None:
    host = SyntheticHost(tmp_path, history_selection="repair_relevance")
    host.diagnosis()
    old = host.candidate("almost")
    host.step("run_check", {"candidate_id": old})
    failure = host.step("link_failed_check", {"candidate_id": old, "violated_obligation_ids": ["keep_value"]})
    current = host.candidate("good")
    host.step(None, text="the prior failed behavior still matters")
    selected = host.plan()
    assert packet(selected)["focus"]["current_candidate_id"] == current
    assert old not in packet(selected)["candidates"]
    panel = json.loads(str(selected.view_messages()[1].content).split(WORKING_CONTEXT_PROMPT + "\n", 1)[1])
    names = [group["messages"][0]["tool_calls"][0]["name"] for group in panel["required_unrepresented_groups"]]
    assert names == ["register_candidate", "run_check", "link_failed_check"]
    assert failure["candidate_snapshot_sha256"] in selected.stage_messages_json
    assert "keep_value" in selected.stage_messages_json


def test_repair_selection_keeps_observed_failure_without_optional_model_link(tmp_path: Path) -> None:
    host = SyntheticHost(tmp_path, history_selection="repair_relevance")
    host.diagnosis()
    old = host.candidate("almost")
    observed = host.step("run_check", {"candidate_id": old})
    current = host.candidate("good")
    host.step(None, text="the last registered candidate is current")
    selected = host.plan()
    assert packet(selected)["focus"]["current_candidate_id"] == current
    assert old not in packet(selected)["candidates"]
    panel = json.loads(str(selected.view_messages()[1].content).split(WORKING_CONTEXT_PROMPT + "\n", 1)[1])
    names = [group["messages"][0]["tool_calls"][0]["name"] for group in panel["required_unrepresented_groups"]]
    assert names == ["register_candidate", "run_check"]
    assert observed["status"] == "fail" and observed["candidate_id"] == old
    assert observed["idempotency_key"] in selected.stage_messages_json
    assert not packet(selected)["feedback"]


def test_repair_selection_retains_complete_guidance_and_current_unknown_reviews(tmp_path: Path) -> None:
    host = SyntheticHost(tmp_path, history_selection="repair_relevance")
    host.diagnosis()
    identifier = host.candidate("good")
    frozen = host.plan()
    value = packet(frozen)
    focus = {**value["focus"], "hypothesis_ids": ["h1"]}
    messages = [message.model_copy(deep=True) for message in host.messages]
    receipts = json.loads(frozen.visible_receipts_json)
    ranges: dict[str, list[int]] = {}

    def append(name: str, answer: dict[str, Any], label: str) -> None:
        start = len(messages)
        call = {"name": name, "args": {}, "id": label}
        receipt = ToolMessage(content=canonical(answer), name=name, tool_call_id=label)
        messages.extend([AIMessage(content="", tool_calls=[call]), receipt])
        receipts.append({"group_start": start, "action": {"action_id": label,
            "authorization": "allowed", "tool_name": name}, "tool_call": call,
            "receipt": receipt.model_dump(mode="json")})
        ranges[label] = [start, start + 2]

    # Pure selector fixtures use the guidance-bearing fields read here. They
    # do not authenticate the store/schema or prove SDK tool visibility.
    skill = {"skill_id": "dfx-selected", "snapshot_sha256": "a" * 64, "content_sha256": "b" * 64,
             "projection_sha256": "c" * 64, "body_sha256": "d" * 64,
             "body": "complete operational procedure; preserve behavior; check prerequisites"}
    append("read_skill", skill, "old-skill")
    append("read_skill", skill, "latest-skill")
    page = {"query_id": "bound-query-selected", "bound_query": {"content_sha256": "e" * 64},
            "retrieval": {"hits": [], "unknown_questions": ["unanswered prerequisite"]}}
    append("search_experience", page, "old-page")
    append("read_experience", page, "latest-page")
    entry = {"query_id": page["query_id"], "entry_id": "experience-selected",
             "projection": {"procedure": "complete entry process", "premises": ["unanswered prerequisite"]}}
    append("read_experience", entry, "old-entry")
    append("read_experience", entry, "latest-entry")
    context = {"query_id": page["query_id"], "context": {"context_id": "app-context-selected",
        "payload_json": canonical({"packet": {"canonical_payload_json": canonical({"focus": focus})}})}}
    append("prepare_experience_context", context, "current-context")
    proposal = {"diagnosis_id": focus["diagnosis_id"], "hypothesis_ids": focus["hypothesis_ids"],
        "candidate_id": identifier, "candidate_snapshot_sha256": value["candidates"][identifier]["candidate_snapshot_sha256"],
        "source_snapshot_sha256": value["source_snapshot_sha256"], "entry_id": entry["entry_id"],
        "content_sha256": "b" * 64, "projection_sha256": "c" * 64}
    review = {"query_id": page["query_id"], "review": {"record_id": "app-review-selected"},
              "guidance": {"proposal": proposal, "status": "unknown", "questions": ["unanswered prerequisite"],
                           "projection": {"procedure": "complete entry process"}}}
    append("review_experience", review, "old-current-review")
    append("read_experience_review", review, "latest-current-review")
    historical = copy.deepcopy(review)
    historical["guidance"]["proposal"]["candidate_id"] = "f" * 64
    append("read_experience_review", historical, "old-focus-review")
    fallback = {"query_id": page["query_id"], "status": "unknown", "reason": "no_visible_focus",
                "next_action": "ordinary_retrieval", "guidance": page}
    append("prepare_experience_context", fallback, "ordinary-unknown")
    messages.append(AIMessage(content="current thought keeps no new guidance authority"))
    original = message_json(messages)
    view, selection = _select_messages(messages, WorkingContextPacket.model_validate_json(frozen.packet_json),
                                      receipts, host.policy)
    retained = selection["retained_ranges"]
    for label in ("latest-skill", "latest-page", "latest-entry", "current-context", "latest-current-review", "ordinary-unknown"):
        assert ranges[label] in retained
    for label in ("old-skill", "old-page", "old-entry", "old-current-review", "old-focus-review"):
        assert ranges[label] in selection["omitted_ranges"]
    assert "complete operational procedure" in message_json(view)
    assert "unanswered prerequisite" in message_json(view)
    assert "unknown" in message_json(view)
    assert message_json(messages) == original


def test_repair_selection_keeps_multi_tool_groups_whole(tmp_path: Path) -> None:
    host = SyntheticHost(tmp_path, history_selection="repair_relevance")
    host.diagnosis()
    frozen = host.plan()
    messages = [message.model_copy(deep=True) for message in host.messages]
    start = len(messages)
    calls = [{"name": "read_source", "args": {"path": "src/main.ets"}, "id": "source-with-sibling"},
             {"name": "read_log", "args": {}, "id": "log-with-sibling"}]
    answers = [{"path": "src/main.ets", "start_line": 1, "lines": ["value=bad"]},
               {"start_line": 1, "lines": ["synthetic crash"]}]
    messages.append(AIMessage(content="", tool_calls=calls))
    receipts = json.loads(frozen.visible_receipts_json)
    # Pure selector fixture. Production select authenticates these facts against
    # model/action ledgers before entering the same whole-group boundary.
    for index, (call, answer) in enumerate(zip(calls, answers, strict=True)):
        receipt = ToolMessage(content=canonical(answer), name=call["name"], tool_call_id=call["id"])
        messages.append(receipt)
        receipts.append({"group_start": start, "action": {"action_id": f"synthetic-multi-{index}",
            "authorization": "allowed", "tool_name": call["name"]}, "tool_call": call,
            "receipt": receipt.model_dump(mode="json")})
    messages.append(AIMessage(content="current no-tool thought"))
    original = message_json(messages)
    view, selection = _select_messages(messages, WorkingContextPacket.model_validate_json(frozen.packet_json),
                                     receipts, host.policy)
    retained = [message for message in view if type(message) is ToolMessage]
    assert {message.tool_call_id for message in retained} == {"source-with-sibling", "log-with-sibling"}
    assert [start, start + 3] in selection["retained_ranges"]
    assert message_json(messages) == original


def test_repair_selection_rejects_reasoning_history_before_plan_or_reservation(tmp_path: Path) -> None:
    host = SyntheticHost(tmp_path, history_selection="repair_relevance")
    host.step(None, text="an unconfirmed thought")
    host.messages[-1].additional_kwargs["reasoning_content"] = "provider-specific reasoning history"
    before = frozen_files(host.workspace.root)
    usage = host.calls.usage_summary("task1", "run1")
    with pytest.raises(WorkingContextError, match="working_context_reasoning_history_unsupported"):
        host.plan()
    assert frozen_files(host.workspace.root) == before
    assert host.calls.usage_summary("task1", "run1") == usage


def test_repair_selection_reason_tampering_cannot_rebuild_a_frozen_plan(tmp_path: Path) -> None:
    host = SyntheticHost(tmp_path, history_selection="repair_relevance")
    host.step(None, text="old thought")
    host.step(None, text="current thought")
    selected = host.plan()
    raw = selected.model_dump(mode="json")
    changed = json.loads(raw["selection_json"])
    changed["group_decisions"][0]["reason"] = "fabricated selection reason"
    raw["selection_json"] = canonical(changed)
    with pytest.raises(ValidationError, match="stage selection changed"):
        WorkingContextPlan.model_validate_json(canonical(raw))


@pytest.mark.parametrize("fault", ["delete_diagnosis", "delete_support", "delete_check", "unknowns", "authority",
                                  "application", "focus_recency", "source_statement"])
def test_rehashed_packet_cannot_change_original_projection_or_delete_required_closure(tmp_path: Path, fault: str) -> None:
    host = SyntheticHost(tmp_path)
    host.diagnosis()
    identifier = host.candidate("good")
    selected = host.plan()
    value = packet(selected)
    if fault == "delete_diagnosis":
        value["diagnoses"].clear()
    elif fault == "delete_support":
        value["evidence"].pop("infer1")
    elif fault == "delete_check":
        value["checks"].pop(identifier)
    elif fault == "unknowns":
        value["diagnoses"]["diagnosis1"]["unknowns"] = ["coherently rehashed fabricated claim"]
    elif fault == "authority":
        value["root_cause_confirmation"] = "confirmed"
    elif fault == "application":
        value["application_verification"] = "pass"
    elif fault == "focus_recency":
        value["focus_recency"] = "verified_latest"
    else:
        value["evidence"]["infer1"]["statement"] = "rehashed unconfirmed alteration"
        value["evidence"]["infer1"]["artifact_sha256"] = sha256(value["evidence"]["infer1"]["statement"])
    replacement = WorkingContextPacket(canonical_payload_json=canonical(value), payload_sha256=sha256(canonical(value)),
        max_bytes=json.loads(selected.packet_json)["max_bytes"])
    raw = selected.model_dump(mode="json")
    with pytest.raises(WorkingContextError):
        projection = project_working_context(replacement, max_bytes=host.policy.max_packet_bytes)
        stage, selection = _select_messages(host.messages, replacement,
            json.loads(selected.visible_receipts_json), host.policy, projection=projection)
        raw.update(packet_json=replacement.model_dump_json(), solver_projection_json=projection.model_dump_json(),
            stage_messages_json=message_json(stage), stage_sha256=sha256(message_json(stage)),
            selection_json=canonical(selection))
        path = host.store.directory / f"{selected.parent_call_id}.plan.json"
        path.write_text(canonical(raw), encoding="utf-8")
        host.store.audit(host.calls, host.actions, [])


@pytest.mark.parametrize("field", ["messages", "tools", "parameters"])
def test_actual_fixed_request_payload_must_match_every_intent_hash(tmp_path: Path, field: str) -> None:
    host = SyntheticHost(tmp_path)
    selected = host.plan()
    intent, payload = fixed_intent(selected, selected.view_messages(), list(host.tools))
    changed = copy.deepcopy(payload)
    if field == "messages":
        changed[field][1]["content"] = "different actual prompt"
    elif field == "tools":
        changed[field][0]["description"] = "different actual schema"
    else:
        changed[field]["bind_kwargs"] = {"tool_choice": "none"}
    with pytest.raises(WorkingContextError, match="working_context_plan_invalid"):
        host.store.link(selected, intent, None, view_messages=selected.view_messages(), compaction=None, fixed_request=changed)
    assert not (host.store.directory / "model-1.solver.json").exists()


def test_wire_actual_request_matches_view_tools_parameters_and_profile(tmp_path: Path) -> None:
    host = SyntheticHost(tmp_path)
    adapter = ChatCompletionsAdapter(ProviderSettings(provider_id="mock", model_id="synthetic"),
        httpx.MockTransport(lambda _: pytest.fail("unexpected model dispatch")))
    store = WorkingContextStore(host.workspace, host.task, host.ledger, host.policy, adapter.profile())
    selected = store.select(host.messages, "model-1", host.calls, host.actions)
    assert selected is not None
    request = adapter.prepare(selected.view_messages(), [])
    intent = ModelCallIntent(call_id="model-1", task_id="task1", run_id="run1", kind="solver", provider_id="mock",
        model_id="synthetic", prompt_sha256=sha256(canonical(request.messages)),
        tool_schema_sha256=sha256(canonical(request.tools)), parameters_sha256=sha256(canonical({
            "wire_parameters": request.parameters, "body_sha256": request.body_sha256,
            "profile_sha256": request.profile_sha256})), tool_names=())
    host.calls.reserve(intent, max_calls=10)
    store.link(selected, intent, request, view_messages=selected.view_messages(), compaction=None)
    assert store.audit(host.calls, host.actions, [])[0]["solver_link_saved"]
    forged = adapter.prepare([HumanMessage(content="different actual wire messages")], [])
    with pytest.raises(WorkingContextError, match="working_context_plan_invalid"):
        store.link(selected, intent, forged, view_messages=selected.view_messages(), compaction=None)


def test_malformed_wire_tools_have_a_typed_rejection_in_link_and_saved_audit(tmp_path: Path) -> None:
    host = SyntheticHost(tmp_path)
    adapter = ChatCompletionsAdapter(ProviderSettings(provider_id="mock", model_id="synthetic"),
        httpx.MockTransport(lambda _: pytest.fail("unexpected model dispatch")))
    store = WorkingContextStore(host.workspace, host.task, host.ledger, host.policy, adapter.profile())
    selected = store.select(host.messages, "model-1", host.calls, host.actions)
    assert selected is not None
    request = adapter.prepare(selected.view_messages(), [])
    intent = ModelCallIntent(call_id="model-1", task_id="task1", run_id="run1", kind="solver", provider_id="mock",
        model_id="synthetic", prompt_sha256=sha256(canonical(request.messages)),
        tool_schema_sha256=sha256(canonical(request.tools)), parameters_sha256=sha256(canonical({
            "wire_parameters": request.parameters, "body_sha256": request.body_sha256,
            "profile_sha256": request.profile_sha256})), tool_names=())
    host.calls.reserve(intent, max_calls=10)
    body = json.loads(request.body)
    body["tools"] = [{}]
    malformed = PreparedRequest(body=canonical(body), body_sha256=sha256(canonical(body)),
        profile_sha256=request.profile_sha256)
    with pytest.raises(WorkingContextError, match="working_context_plan_invalid"):
        store.link(selected, intent, malformed, view_messages=selected.view_messages(), compaction=None)
    store.link(selected, intent, request, view_messages=selected.view_messages(), compaction=None)
    path = store.directory / "model-1.solver.json"
    saved = json.loads(path.read_text(encoding="utf-8"))
    saved["prepared_request"] = malformed.model_dump(mode="json")
    path.write_text(canonical(saved), encoding="utf-8")
    with pytest.raises(WorkingContextError, match="working_context_plan_invalid"):
        store.audit(host.calls, host.actions, [])


def test_generic_summary_is_second_step_and_preserves_exact_startup_panel(tmp_path: Path) -> None:
    host = SyntheticHost(tmp_path)
    host.diagnosis()
    host.step(None, text="unknown earlier thought")
    host.step(None, text="unknown recent thought")
    selected = host.plan()
    compact = CompactionStore(host.workspace.root, "task1", "run1",
        CompactionPolicy(enabled=True, trigger_bytes=128, retain_groups=1), FIXED)
    compression = compact.select(selected.view_messages(), selected.parent_call_id)
    assert compression is not None
    summary, _ = invoke_accounted(host.calls, compression.summary_intent(),
        lambda: AIMessage(content="Earlier model claims remain unconfirmed."), max_calls=100)
    final = compact.complete(compression, summary, host.calls)
    assert final[1] == selected.view_messages()[1]
    intent, payload = fixed_intent(selected, final, list(host.tools))
    host.calls.reserve(intent, max_calls=100)
    compact.link(compression, intent, None)
    host.store.link(selected, intent, None, view_messages=final, compaction=compression, fixed_request=payload)
    facts = host.store.audit(host.calls, host.actions, compact.audit(host.calls))
    assert facts[-1]["final_bytes"] == len(message_json(final).encode("utf-8"))
    assert facts[-1]["final_view_sha256"] != facts[-1]["stage_sha256"]
    with pytest.raises(WorkingContextError, match="working_context_plan_invalid"):
        host.store.audit(host.calls, host.actions, [])


def test_same_call_unknown_reuses_saved_plan_but_never_reconstructs_missing_one(tmp_path: Path) -> None:
    host = SyntheticHost(tmp_path)
    selected = host.plan()
    intent, payload = fixed_intent(selected, selected.view_messages(), list(host.tools))
    host.calls.reserve(intent, max_calls=10)
    host.store.link(selected, intent, None, view_messages=selected.view_messages(), compaction=None, fixed_request=payload)
    host.calls.dispatch(intent.call_id)
    host.calls.mark_unknown(intent.call_id, "synthetic crash")
    before = frozen_files(host.store.directory)
    assert host.plan() == selected
    assert host.store.audit(host.calls, host.actions, [])[0]["solver_state"] == "unknown"
    assert frozen_files(host.store.directory) == before
    (host.store.directory / "model-1.plan.json").unlink()
    with pytest.raises(WorkingContextError, match="working_context_plan_invalid"):
        host.plan()


@pytest.mark.parametrize("fault", ["receipt", "tool_args", "solver_site", "source_bytes", "watermark"])
def test_recovery_rejects_changed_receipts_sources_and_solver_chronology(tmp_path: Path, fault: str) -> None:
    host = SyntheticHost(tmp_path)
    host.diagnosis()
    selected = host.plan()
    if fault == "receipt":
        host.messages[-1].content = "changed receipt"
    elif fault == "tool_args":
        host.messages[-2].tool_calls[0]["args"] = {}  # type: ignore[attr-defined]
    elif fault == "solver_site":
        host.messages[-2].response_metadata["dfx_model_call_id"] = "model-2"
    else:
        path = host.store.directory / f"{selected.parent_call_id}.plan.json"
        raw = json.loads(path.read_text(encoding="utf-8"))
        if fault == "source_bytes":
            material = json.loads(raw["source_material_json"])
            material["workspace/diagnoses/diagnosis1.json"]["bytes"] += 1
            raw["source_material_json"] = canonical(material)
        else:
            receipt = json.loads(raw["visible_receipts_json"])
            receipt[0]["ledger_position"] += 10
            raw["visible_receipts_json"] = canonical(receipt)
        path.write_text(canonical(raw), encoding="utf-8")
    expected_error = "working_context_material_invalid" if fault == "source_bytes" else "working_context_plan_invalid"
    with pytest.raises(WorkingContextError, match=expected_error):
        host.plan()


def test_disabled_policy_and_unexpected_material_have_explicit_behavior(tmp_path: Path) -> None:
    host = SyntheticHost(tmp_path)
    disabled = WorkingContextStore(host.workspace, None, None, WorkingContextPolicy(), FIXED)
    assert disabled.select(host.messages, "model-1", host.calls, host.actions) is None
    assert disabled.audit(host.calls, host.actions, []) == []
    assert not disabled.directory.exists()
    host.plan()
    with pytest.raises(WorkingContextError, match="working_context_plan_invalid"):
        disabled.audit(host.calls, host.actions, [])


def test_orphan_and_pending_observation_cannot_change_frozen_plan_or_authority(tmp_path: Path) -> None:
    host = SyntheticHost(tmp_path)
    selected = host.plan()
    original = host.store.audit(host.calls, host.actions, [])[0]
    host.store.materials.put(b"unreferenced synthetic crash residue")
    pending = host.store.materials.directory / ("pending-" + "a" * 32 + ".blob-part")
    pending.write_bytes(b"partial")
    before = frozen_files(host.store.directory)
    observed = host.store.audit(host.calls, host.actions, [])[0]
    summary = observed.pop("run_material_observation")
    previous = original.pop("run_material_observation")
    assert observed == original
    assert summary["unique_blob_count"] == previous["unique_blob_count"] + 1
    assert summary["pending_count"] == 1 and summary["pending_bytes"] == 7
    assert host.plan() == selected
    assert host.calls.usage_summary("task1", "run1")["reserved_calls"] == 0
    assert frozen_files(host.store.directory) == before


@pytest.mark.parametrize("invalid", [float("inf"), float("nan"), -1.0])
def test_selection_duration_is_observational_but_finite_and_nonnegative(tmp_path: Path, invalid: float) -> None:
    host = SyntheticHost(tmp_path)
    raw = host.plan().model_dump(mode="json")
    raw["selection_ms"] = invalid
    with pytest.raises(ValidationError):
        WorkingContextPlan.model_validate(raw)
