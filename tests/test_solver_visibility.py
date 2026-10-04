"""Actual sent inputs, completed-parent authority, and immutable readback."""

from __future__ import annotations

import copy
import json
import os
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
from test_agent_loop import task_snapshot
from test_working_context_plan import FIXED, SyntheticHost, fixed_intent, frozen_files

from dfx_agent import solver_visibility
from dfx_agent.actions import ToolActionStore
from dfx_agent.context_compaction import CompactionPolicy, CompactionStore
from dfx_agent.model_calls import (
    ModelCallIntent,
    ModelCallStore,
    canonical,
    invoke_accounted,
    sha256,
)
from dfx_agent.model_provider import ChatCompletionsAdapter, ProviderSettings
from dfx_agent.solver_visibility import SolverVisibilityError, SolverVisibilityStore
from dfx_agent.verification import CheckLedger, FixedTextExecutor
from dfx_agent.workspace import CandidateWorkspace

NAMES = ["read_source", "record_diagnosis", "inspect_analysis", "search_experience"]


class Host:
    def __init__(self, root: Path, *, wire: bool = False):
        self.task = task_snapshot(root)
        self.workspace = CandidateWorkspace(self.task, root / "run", "run1")
        self.ledger = CheckLedger(self.workspace, FixedTextExecutor("src/main.ets", "value=good\n"))
        self.calls = ModelCallStore(self.workspace.root / "model-calls.sqlite")
        self.actions = ToolActionStore(self.workspace.root / "tool-actions.sqlite")
        self.adapter = (ChatCompletionsAdapter(ProviderSettings(provider_id="mock", model_id="synthetic"),
            httpx.MockTransport(lambda _: pytest.fail("visibility must not dispatch"))) if wire else None)
        self.profile = FIXED if self.adapter is None else self.adapter.profile()
        self.store = SolverVisibilityStore(self.workspace, self.task, self.ledger, self.profile)
        self.messages: list[BaseMessage] = [SystemMessage(content="synthetic host"), HumanMessage(content="repair")]

    def request(self, call_id: str, view: list[BaseMessage]):
        fixed, request = None, None
        if self.adapter is None:
            fixed = {"messages": [item.model_dump(mode="json") for item in view],
                "tools": [{"name": name, "description": "synthetic tool", "args": {}} for name in NAMES],
                "parameters": {"stop": None, "generate_kwargs": {}, "bind_kwargs": {}}}
            intent = ModelCallIntent(call_id=call_id, task_id="task1", run_id="run1", kind="solver",
                provider_id="dfxfixed", model_id="offline", prompt_sha256=sha256(canonical(fixed["messages"])),
                tool_schema_sha256=sha256(canonical(fixed["tools"])),
                parameters_sha256=sha256(canonical(fixed["parameters"])), tool_names=tuple(NAMES))
        else:
            request = self.adapter.prepare(view, [{"type": "function", "function": {"name": name,
                "parameters": {"type": "object", "properties": {}}}} for name in NAMES])
            intent = ModelCallIntent(call_id=call_id, task_id="task1", run_id="run1", kind="solver",
                provider_id="mock", model_id="synthetic", prompt_sha256=sha256(canonical(request.messages)),
                tool_schema_sha256=sha256(canonical(request.tools)), tool_names=tuple(NAMES),
                parameters_sha256=sha256(canonical({"wire_parameters": request.parameters,
                    "body_sha256": request.body_sha256, "profile_sha256": request.profile_sha256})))
        return intent, fixed, request

    def reserve(self, call_id: str, view: list[BaseMessage] | None = None):
        intent, fixed, request = self.request(call_id, self.messages if view is None else view)
        self.calls.reserve(intent, max_calls=100)
        return intent, fixed, request

    def capture(self, call_id: str, view: list[BaseMessage] | None = None):
        view = self.messages if view is None else view
        intent, fixed, request = self.reserve(call_id, view)
        self.store.capture(intent, view_messages=view, fixed_request=fixed, prepared_request=request)
        return intent

    def complete(self, intent: ModelCallIntent, response: AIMessage | None = None) -> AIMessage:
        response = response or AIMessage(content="unknown")
        response.response_metadata = {**response.response_metadata, "dfx_model_call_id": intent.call_id}
        self.calls.dispatch(intent.call_id)
        self.calls.complete(intent, response)
        return self.calls.replay(intent)[0]

    def step(self, number: int, name: str | None = None, answer: Any = None,
             args: dict[str, Any] | None = None, *, text: str = "unknown") -> None:
        intent = self.capture(f"model-{number}")
        call = None if name is None else {"name": name, "id": f"tool-{number}", "args": args or {}}
        response = AIMessage(content=text if call is None else "", tool_calls=[] if call is None else [call])
        saved = self.complete(intent, response)
        self.messages.append(saved)
        if call is None:
            return
        call = saved.tool_calls[0]
        identifier = "action-" + sha256(canonical(["task1", "run1", intent.call_id, call["id"]]))
        receipt = self.actions.invoke(action_id=identifier, request_sha256=sha256(canonical(call)),
            model_call_id=intent.call_id, tool_name=name or "", tool_call_id=call["id"], proposal=None,
            rejection=None, max_tool_calls=100, handler=lambda: ToolMessage(content=canonical(answer),
                name=name, tool_call_id=call["id"]))
        self.messages.append(receipt)


def capture_working(host: SyntheticHost, *, compact: bool = False):
    plan = host.plan()
    view = plan.view_messages()
    compaction = None
    if compact:
        compressor = CompactionStore(host.workspace.root, "task1", "run1",
            CompactionPolicy(enabled=True, trigger_bytes=128, retain_groups=1), FIXED)
        compaction = compressor.select(view, plan.parent_call_id)
        assert compaction is not None
        summary, _ = invoke_accounted(host.calls, compaction.summary_intent(),
            lambda: AIMessage(content="Historical references stay unconfirmed."), max_calls=100)
        view = compressor.complete(compaction, summary, host.calls)
    intent, fixed = fixed_intent(plan, view, list(host.tools))
    host.calls.reserve(intent, max_calls=100)
    store = SolverVisibilityStore(host.workspace, host.task, host.ledger, FIXED, host.store)
    store.capture(intent, view_messages=view, fixed_request=fixed,
                  working_context_plan=plan, compaction=compaction)
    host.calls.dispatch(intent.call_id)
    host.calls.complete(intent, AIMessage(content="unknown", response_metadata={"dfx_model_call_id": intent.call_id}))
    return store, intent


def test_construction_and_missing_read_never_create_storage(tmp_path: Path) -> None:
    host = Host(tmp_path)
    assert not host.store.directory.exists()
    with pytest.raises(SolverVisibilityError, match="^applicability_input_invalid$"):
        host.store.read("model-1", host.calls, host.actions)
    assert not host.store.directory.exists()
    assert host.store.audit(host.calls, host.actions) == []


@pytest.mark.parametrize("wire", [False, True])
def test_fixed_and_wire_capture_are_immutable_and_read_from_completed_input(tmp_path: Path, wire: bool) -> None:
    host = Host(tmp_path, wire=wire)
    intent, fixed, request = host.reserve("model-1")
    host.store.capture(intent, view_messages=host.messages, fixed_request=fixed, prepared_request=request)
    host.store.capture(intent, view_messages=host.messages, fixed_request=fixed, prepared_request=request)
    before = frozen_files(host.store.directory)
    with pytest.raises(SolverVisibilityError, match="^applicability_input_invalid$"):
        host.store.read(intent.call_id, host.calls, host.actions)
    pending = host.store.audit(host.calls, host.actions)[0]
    assert pending["input_saved"] and pending["authority"] == "not_authoritative" and pending["focus_sha256"] is None
    host.complete(intent)
    saved = host.store.read(intent.call_id, host.calls, host.actions)
    assert saved["request_sha256"] == intent.sha256() and saved["solver_state"] == "completed"
    assert saved["authority"] == "completed_solver_input" and saved["visible_packet"] is None
    assert saved["visible_receipts"] == [] and saved["focus"]["diagnosis_id"] is None
    saved["request"]["fixed_request"] = {"tampered": True}
    saved["model_profile"]["tampered"] = True
    assert "tampered" not in host.store.read(intent.call_id, host.calls, host.actions)["model_profile"]
    assert frozen_files(host.store.directory) == before
    assert host.store.audit(host.calls, host.actions)[0]["input_saved"]


def test_reserved_missing_input_is_honest_but_never_authorizes_read(tmp_path: Path) -> None:
    host = Host(tmp_path)
    intent, _, _ = host.reserve("model-1")
    fact = host.store.audit(host.calls, host.actions)[0]
    assert fact["input_saved"] is False and fact["input_bytes"] == 0 and fact["focus_sha256"] is None
    assert fact["visible_action_count"] is None and not host.store.directory.exists()
    with pytest.raises(SolverVisibilityError, match="^applicability_input_invalid$"):
        host.store.read(intent.call_id, host.calls, host.actions)


def test_report_hashes_bind_the_complete_audited_focus_and_actions(tmp_path: Path) -> None:
    host = Host(tmp_path)
    host.step(1, "read_source", {"path": "src/main.ets", "lines": ["value=bad"]}, {"path": "src/main.ets"})
    host.step(2)
    view = host.store.read("model-2", host.calls, host.actions)
    saved = host.store.audit(host.calls, host.actions)[1]
    identifiers = [item["action"]["action_id"] for item in view["visible_receipts"]]
    assert saved["visible_action_count"] == len(identifiers) == 1
    assert saved["visible_actions_sha256"] == sha256(canonical(identifiers))
    assert saved["focus_sha256"] == sha256(canonical(view["focus"]))
    assert "focus" not in saved and "visible_action_ids" not in saved
    original = host.store.directory / "model-2.json"
    value = json.loads(original.read_text(encoding="utf-8"))
    assert value["final_view_messages_json"] == view["final_view_messages_json"]
    assert identifiers[0] == view["visible_receipts"][0]["action"]["action_id"]


@pytest.mark.parametrize("state", ["dispatched", "unknown", "completed"])
def test_dispatched_parent_without_saved_input_is_rejected(tmp_path: Path, state: str) -> None:
    host = Host(tmp_path)
    intent, fixed, request = host.reserve("model-1")
    host.calls.dispatch(intent.call_id)
    if state == "unknown":
        host.calls.mark_unknown(intent.call_id, "synthetic interrupted send")
    elif state == "completed":
        host.calls.complete(intent, AIMessage(content="unknown"))
    with pytest.raises(SolverVisibilityError, match="^applicability_input_invalid$"):
        host.store.audit(host.calls, host.actions)
    with pytest.raises(SolverVisibilityError, match="^applicability_input_invalid$"):
        host.store.capture(intent, view_messages=host.messages, fixed_request=fixed, prepared_request=request)


@pytest.mark.parametrize("field", ["messages", "tools", "parameters", "profile", "owner", "kind"])
def test_capture_refuses_request_and_intent_mismatch(tmp_path: Path, field: str) -> None:
    host = Host(tmp_path)
    intent, fixed, _ = host.reserve("model-1")
    assert fixed is not None
    if field == "messages":
        fixed["messages"][1]["content"] = "fabricated context"
    elif field == "tools":
        fixed["tools"] = []
    elif field == "parameters":
        fixed["parameters"]["generate_kwargs"] = {"changed": True}
    else:
        replacement = {"profile": {"provider_id": "other"}, "owner": {"run_id": "other"},
                       "kind": {"kind": "format_repair"}}[field]
        intent = intent.model_copy(update=replacement)
    with pytest.raises(SolverVisibilityError, match="^applicability_input_invalid$"):
        host.store.capture(intent, view_messages=host.messages, fixed_request=fixed)
    assert not host.store.directory.exists()


def test_actual_group_receipts_and_selected_hypothesis_define_focus(tmp_path: Path) -> None:
    host = Host(tmp_path)
    diagnosis = {"record_id": "diagnosis1", "selected_hypothesis_id": "h1",
                 "hypotheses": [{"hypothesis_id": "h1"}, {"hypothesis_id": "h2"}]}
    host.step(1, "record_diagnosis", {"diagnosis_id": "diagnosis1", "record": {"diagnosis": diagnosis}})
    intent = host.capture("model-2")
    host.complete(intent)
    saved = host.store.read(intent.call_id, host.calls, host.actions)
    assert saved["focus"]["diagnosis_id"] == "diagnosis1" and saved["focus"]["hypothesis_ids"] == ["h1"]
    assert len(saved["visible_receipts"]) == 1 and saved["visible_packet"] is None


def test_simple_diagnosis_needs_actual_inspect_analysis_for_hypothesis_focus(tmp_path: Path) -> None:
    host = Host(tmp_path)
    host.step(1, "record_diagnosis", {"diagnosis_id": "diagnosis1"})
    first = host.capture("model-2")
    host.complete(first)
    assert host.store.read(first.call_id, host.calls, host.actions)["focus"]["hypothesis_ids"] == []
    # Complete input only: a live record outside this receipt is not consulted.
    diagnosis = {"record_id": "diagnosis1", "selected_hypothesis_id": "h1", "hypotheses": [{"hypothesis_id": "h1"}]}
    host.step(3, "inspect_analysis", {"record": {"diagnosis": diagnosis}}, {"diagnosis_id": "diagnosis1"})
    final = host.capture("model-4")
    host.complete(final)
    assert host.store.read(final.call_id, host.calls, host.actions)["focus"]["hypothesis_ids"] == ["h1"]


@pytest.mark.parametrize("fault", ["orphan", "missing_tool", "changed_receipt", "changed_response", "future", "reordered"])
def test_only_complete_matching_earlier_model_groups_are_visible(tmp_path: Path, fault: str) -> None:
    host = Host(tmp_path)
    host.step(1, "read_source", {"text": "original"})
    host.step(2)
    view = copy.deepcopy(host.messages)
    if fault == "orphan":
        view.append(ToolMessage(content="later sibling", tool_call_id="new"))
    elif fault == "missing_tool":
        del view[3]
    elif fault == "changed_receipt":
        view[3].content = "rewritten source"
    elif fault == "changed_response":
        view[4].content = "rewritten inference"
    elif fault == "future":
        future, _, _ = host.reserve("model-4", view[:2])
        view.append(host.complete(future))
    else:
        view = view[:2] + [view[4]] + view[2:4]
    intent, fixed, request = host.reserve("model-3", view)
    with pytest.raises(SolverVisibilityError, match="^applicability_input_invalid$"):
        host.store.capture(intent, view_messages=view, fixed_request=fixed, prepared_request=request)
    assert not (host.store.directory / "model-3.json").exists()


def test_same_parent_sibling_result_never_enters_original_input(tmp_path: Path) -> None:
    host = Host(tmp_path)
    host.step(1, "read_source", {"text": "seen"})
    intent = host.capture("model-2")
    response = host.complete(intent, AIMessage(content="", tool_calls=[
        {"id": "later", "name": "record_diagnosis", "args": {}}]))
    call = response.tool_calls[0]
    identifier = "action-" + sha256(canonical(["task1", "run1", intent.call_id, call["id"]]))
    host.actions.invoke(action_id=identifier, request_sha256=sha256(canonical(call)), model_call_id=intent.call_id,
        tool_name=call["name"], tool_call_id=call["id"], proposal=None, rejection=None, max_tool_calls=100,
        handler=lambda: ToolMessage(content=canonical({"diagnosis_id": "later_diagnosis"}),
                                    name=call["name"], tool_call_id=call["id"]))
    saved = host.store.read(intent.call_id, host.calls, host.actions)
    assert saved["focus"]["diagnosis_id"] is None
    assert [item["action"]["model_call_id"] for item in saved["visible_receipts"]] == ["model-1"]


def test_summary_ids_never_restore_compressed_receipt_authority(tmp_path: Path) -> None:
    host = Host(tmp_path)
    host.step(1, "record_diagnosis", {"diagnosis_id": "diagnosis1"})
    host.step(2, text="unconfirmed earlier thought " * 50)
    host.step(3, text="recent unconfirmed thought")
    compactor = CompactionStore(host.workspace.root, "task1", "run1",
        CompactionPolicy(enabled=True, trigger_bytes=128, retain_groups=1), FIXED)
    plan = compactor.select(host.messages, "model-4")
    assert plan is not None
    summary, _ = invoke_accounted(host.calls, plan.summary_intent(),
        lambda: AIMessage(content="diagnosis1 h1 seen; no actual receipt remains."), max_calls=100)
    view = compactor.complete(plan, summary, host.calls)
    intent, fixed, request = host.reserve("model-4", view)
    host.store.capture(intent, view_messages=view, fixed_request=fixed, prepared_request=request, compaction=plan)
    host.complete(intent)
    saved = host.store.read(intent.call_id, host.calls, host.actions)
    assert "diagnosis1" in saved["final_view_messages_json"]
    assert saved["visible_receipts"] == [] and saved["visible_packet"] is None
    assert saved["focus"]["diagnosis_id"] is None and saved["focus"]["hypothesis_ids"] == []


@pytest.mark.parametrize("compact", [False, True])
def test_fully_retained_working_panel_remains_visible_through_compaction(tmp_path: Path, compact: bool) -> None:
    host = SyntheticHost(tmp_path)
    host.diagnosis()
    host.step(None, text="earlier speculation " * 100)
    host.step(None, text="recent unknown")
    store, intent = capture_working(host, compact=compact)
    saved = store.read(intent.call_id, host.calls, host.actions)
    assert saved["visible_packet"]["diagnoses"]["diagnosis1"]["selected_hypothesis_id"] == "h1"
    assert saved["focus"]["diagnosis_id"] == "diagnosis1" and saved["focus"]["hypothesis_ids"] == ["h1"]
    before = frozen_files(store.directory)
    # A legal later observation does not rebuild this historical sending site.
    host.number += 1
    host.step(None, text="later observation")
    assert store.read(intent.call_id, host.calls, host.actions)["final_view_sha256"] == saved["final_view_sha256"]
    assert frozen_files(store.directory) == before


def test_complete_required_quoted_groups_supply_visible_receipts(tmp_path: Path) -> None:
    host = SyntheticHost(tmp_path)
    host.diagnosis()
    host.step("record_obligation", {"obligation_id": "standalone", "statement": "Preserve extra output",
                                  "evidence_ids": ["source1"]})
    candidate = host.candidate("almost", diagnosis="")
    host.step("run_check", {"candidate_id": candidate})
    host.step(None, text="later speculation " * 100)
    host.step(None, text="latest unknown")
    store, intent = capture_working(host, compact=True)
    saved = store.read(intent.call_id, host.calls, host.actions)
    quoted = saved["required_unrepresented_groups"]
    assert [group["messages"][0]["tool_calls"][0]["name"] for group in quoted] == [
        "record_obligation", "register_candidate", "run_check"]
    assert {item["action"]["tool_name"] for item in saved["visible_receipts"]} >= {
        "record_obligation", "register_candidate", "run_check"}
    assert all(len(group["messages"]) == 2 for group in quoted)


@pytest.mark.parametrize("fault", ["rehashed_messages", "deleted", "hardlink", "unexpected", "profile"])
def test_saved_readback_rejects_drift_and_path_aliases(tmp_path: Path, fault: str) -> None:
    host = Host(tmp_path)
    intent = host.capture("model-1")
    host.complete(intent)
    path = host.store.directory / "model-1.json"
    if fault == "deleted":
        path.unlink()
    elif fault == "hardlink":
        os.link(path, tmp_path / "shared.json")
    elif fault == "unexpected":
        (host.store.directory / "unexpected.json").write_text("{}", encoding="utf-8")
    else:
        value = json.loads(path.read_text(encoding="utf-8"))
        if fault == "profile":
            value["model_profile_json"] = canonical({**FIXED, "provider_id": "other"})
        else:
            messages = json.loads(value["final_view_messages_json"])
            messages[1]["content"] = "new private message"
            value["final_view_messages_json"] = canonical(messages)
            value["final_view_sha256"] = sha256(value["final_view_messages_json"])
            value["fixed_request"]["messages"] = messages
            value["solver_intent"]["prompt_sha256"] = value["final_view_sha256"]
        path.write_text(canonical(value), encoding="utf-8")
    with pytest.raises(SolverVisibilityError, match="^applicability_input_invalid$"):
        host.store.read(intent.call_id, host.calls, host.actions)
    with pytest.raises(SolverVisibilityError, match="^applicability_input_invalid$"):
        host.store.audit(host.calls, host.actions)


def test_same_call_cannot_replace_frozen_input_even_after_completion(tmp_path: Path) -> None:
    host = Host(tmp_path)
    intent = host.capture("model-1")
    host.complete(intent)
    view = copy.deepcopy(host.messages)
    view[1].content = "changed"
    replacement, fixed, request = host.request(intent.call_id, view)
    before = frozen_files(host.store.directory)
    with pytest.raises(SolverVisibilityError, match="^applicability_input_invalid$"):
        host.store.capture(replacement, view_messages=view, fixed_request=fixed, prepared_request=request)
    assert frozen_files(host.store.directory) == before


@pytest.mark.parametrize("which", ["record", "total"])
def test_capture_enforces_explicit_individual_and_total_byte_bounds(tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch, which: str) -> None:
    host = Host(tmp_path)
    if which == "record":
        monkeypatch.setattr(solver_visibility, "MAX_SOLVER_INPUT_BYTES", 128)
    else:
        host.step(1)
        size = sum(path.stat().st_size for path in host.store.directory.iterdir())
        monkeypatch.setattr(solver_visibility, "MAX_SOLVER_INPUT_TOTAL_BYTES", size + 1)
    number = 1 if which == "record" else 2
    intent, fixed, request = host.reserve(f"model-{number}")
    with pytest.raises(SolverVisibilityError, match="^applicability_input_limit$"):
        host.store.capture(intent, view_messages=host.messages, fixed_request=fixed, prepared_request=request)
    assert not (host.store.directory / f"model-{number}.json").exists()


def test_read_rejects_oversized_record_before_parsing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    host = Host(tmp_path)
    intent = host.capture("model-1")
    host.complete(intent)
    monkeypatch.setattr(solver_visibility, "MAX_SOLVER_INPUT_BYTES", 16)
    with pytest.raises(SolverVisibilityError, match="^applicability_input_limit$"):
        host.store.read(intent.call_id, host.calls, host.actions)


@pytest.mark.parametrize("which", ["artifact", "directory"])
def test_actual_symlink_namespace_is_rejected(tmp_path: Path, which: str) -> None:
    host = Host(tmp_path)
    intent = host.capture("model-1")
    host.complete(intent)
    path = host.store.directory / "model-1.json"
    if which == "artifact":
        destination = tmp_path / "outside.json"
        path.replace(destination)
        source = path
    else:
        destination = tmp_path / "outside-inputs"
        host.store.directory.replace(destination)
        source = host.store.directory
    try:
        source.symlink_to(destination, target_is_directory=which == "directory")
    except OSError:
        pytest.skip("actual symlink creation requires local account privileges")
    with pytest.raises(SolverVisibilityError, match="^applicability_input_invalid$"):
        host.store.read(intent.call_id, host.calls, host.actions)


def test_frozen_input_survives_interrupted_atomic_publication(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    host = Host(tmp_path)
    intent, fixed, request = host.reserve("model-1")

    def fail_replace(source: Any, destination: Any) -> None:
        raise OSError("synthetic publication interrupted")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(SolverVisibilityError, match="^applicability_input_invalid$"):
        host.store.capture(intent, view_messages=host.messages, fixed_request=fixed, prepared_request=request)
    assert not (host.store.directory / "model-1.json").exists()
    assert not list(host.workspace.root.glob(".compaction-write-*.tmp"))
    assert host.store.audit(host.calls, host.actions)[0]["input_saved"] is False


def test_stored_audit_needs_no_ledgers_and_never_authenticates_use(tmp_path: Path) -> None:
    host = Host(tmp_path)
    intent = host.capture("model-1")
    host.complete(intent)
    host.calls.path.unlink()
    host.actions.path.unlink()
    before = frozen_files(host.store.directory)
    facts = host.store.audit_stored()
    assert facts[0]["authority"] == "stored_input_not_authenticated"
    assert "focus" not in facts[0] and "visible_action_ids" not in facts[0]
    assert facts[0]["request_sha256"] == intent.sha256()
    assert not host.calls.path.exists() and not host.actions.path.exists()
    assert frozen_files(host.store.directory) == before
    (host.store.directory / "model-1.json").write_text("{}", encoding="utf-8")
    with pytest.raises(SolverVisibilityError, match="^applicability_input_invalid$"):
        host.store.audit_stored()


def test_stored_working_plan_still_checks_frozen_materials_without_ledgers(tmp_path: Path) -> None:
    host = SyntheticHost(tmp_path)
    host.diagnosis()
    host.step(None, text="earlier unknown " * 100)
    host.step(None, text="recent unknown")
    store, intent = capture_working(host, compact=True)
    host.calls.path.unlink()
    host.actions.path.unlink()
    assert store.audit_stored()[0]["parent_call_id"] == intent.call_id
    path = host.workspace.root / "compactions" / f"{intent.call_id}.result.json"
    value = json.loads(path.read_text(encoding="utf-8"))
    value["source_sha256"] = sha256("forged")
    path.write_text(canonical(value), encoding="utf-8")
    with pytest.raises(SolverVisibilityError, match="^applicability_input_invalid$"):
        store.audit_stored()
