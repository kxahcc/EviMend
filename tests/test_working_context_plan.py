"""Frozen SDK sending views from real synthetic tools and accounted receipts.

These component tests use explicit synthetic host authorization. SDK middleware
authorization is exercised separately; no real model or application is called.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

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
from dfx_agent.task import verify_task
from dfx_agent.verification import CheckLedger, FixedTextExecutor
from dfx_agent.working_context import WorkingContextError, WorkingContextPacket
from dfx_agent.working_context_plan import (
    WORKING_CONTEXT_PROMPT,
    WorkingContextPlan,
    WorkingContextPolicy,
    WorkingContextStore,
    _select_messages,
)
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
    def __init__(self, root: Path, *, max_view_bytes: int = 786_432):
        self.task = task_snapshot(root)
        self.workspace = CandidateWorkspace(self.task, root / "run", "run1")
        self.ledger = CheckLedger(self.workspace, FixedTextExecutor("src/main.ets", "value=good\n"))
        self.tools = {tool.name: tool for tool in _tools(self.workspace, self.ledger, 5, self.task, "crash.log",
                     verify_task(self.task)["crash_sha256"], FULL)}
        self.actions = ToolActionStore(self.workspace.root / "tool-actions.sqlite")
        self.calls = ModelCallStore(self.workspace.root / "model-calls.sqlite")
        self.policy = WorkingContextPolicy(enabled=True, max_view_bytes=max_view_bytes, retain_groups=1)
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


def test_actual_failed_A_remains_separate_from_current_B(tmp_path: Path) -> None:
    host = SyntheticHost(tmp_path)
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


def test_historical_plan_replays_after_legal_checks_and_unrelated_records_progress(tmp_path: Path) -> None:
    host = SyntheticHost(tmp_path)
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


def test_required_budget_failure_does_not_reserve_or_write_plan(tmp_path: Path) -> None:
    host = SyntheticHost(tmp_path, max_view_bytes=512)
    before = frozen_files(tmp_path)
    with pytest.raises(WorkingContextError, match="working_context_required_limit"):
        host.plan()
    assert host.calls.usage_summary("task1", "run1")["reserved_calls"] == 0
    assert frozen_files(tmp_path) == before


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
        max_bytes=host.policy.max_packet_bytes)
    raw = selected.model_dump(mode="json")
    stage, selection = _select_messages(host.messages, replacement, json.loads(selected.visible_receipts_json), host.policy)
    raw.update(packet_json=replacement.model_dump_json(), stage_messages_json=message_json(stage),
               stage_sha256=sha256(message_json(stage)), selection_json=canonical(selection))
    with pytest.raises((ValueError, KeyError), match="closure|authority|projection|reference"):
        WorkingContextPlan.model_validate_json(canonical(raw))


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
            material["workspace/diagnoses/diagnosis1.json"]["base64"] = "e30="
            raw["source_material_json"] = canonical(material)
        else:
            receipt = json.loads(raw["visible_receipts_json"])
            receipt[0]["ledger_position"] += 10
            raw["visible_receipts_json"] = canonical(receipt)
        path.write_text(canonical(raw), encoding="utf-8")
    with pytest.raises(WorkingContextError, match="working_context_plan_invalid"):
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


@pytest.mark.parametrize("invalid", [float("inf"), float("nan"), -1.0])
def test_selection_duration_is_observational_but_finite_and_nonnegative(tmp_path: Path, invalid: float) -> None:
    host = SyntheticHost(tmp_path)
    raw = host.plan().model_dump(mode="json")
    raw["selection_ms"] = invalid
    with pytest.raises(ValidationError):
        WorkingContextPlan.model_validate(raw)
