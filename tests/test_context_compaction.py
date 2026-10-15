"""Synthetic summaries retain history, honest costs and recoverable request identities."""

from __future__ import annotations

import json
import os
from pathlib import Path

import httpx
import pytest
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)

from dfx_agent.context_compaction import (
    MAX_ARTIFACT_BYTES,
    SUMMARY_PROMPT,
    CompactionPlan,
    CompactionPolicy,
    CompactionStore,
    ContextCompactionError,
)
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

FIXED_PROFILE = {"transport": "fixed_response", "provider_id": "dfxfixed", "model_id": "offline"}


def history() -> list[BaseMessage]:
    return [SystemMessage(content="host policy"), HumanMessage(content="original repair task"),
            AIMessage(content="first hypothesis " + "x" * 200),
            AIMessage(content="", tool_calls=[{"id": "read-1", "name": "read_source", "args": {"path": "a.ets"}},
                                              {"id": "read-2", "name": "read_log", "args": {}}]),
            ToolMessage(content="source bytes " + "y" * 100, tool_call_id="read-1"),
            ToolMessage(content="failure, application result unknown", tool_call_id="read-2"),
            AIMessage(content="most recent complete group")]


def setup(tmp_path: Path, *, wire: bool = False, **options) -> tuple[CompactionStore, ModelCallStore]:
    policy = CompactionPolicy(enabled=True, trigger_bytes=128, retain_groups=1, **options)
    profile = FIXED_PROFILE
    if wire:
        profile = ChatCompletionsAdapter(ProviderSettings(provider_id="mock", model_id="model"),
                                         httpx.MockTransport(lambda _: pytest.fail("unexpected request"))).profile()
    return CompactionStore(tmp_path, "task1", "run1", policy, profile), ModelCallStore(tmp_path / "model-calls.sqlite")


def plan_for(store: CompactionStore) -> CompactionPlan:
    plan = store.select(history(), "model-2")
    assert plan is not None
    return plan


def response(content: str = "Earlier check failed. The root cause and application outcome remain unknown.") -> AIMessage:
    return AIMessage(content=content, usage_metadata={"input_tokens": 20, "output_tokens": 3, "total_tokens": 23})


def save_summary(ledger: ModelCallStore, plan: CompactionPlan, message: AIMessage | None = None) -> AIMessage:
    value, _ = invoke_accounted(ledger, plan.summary_intent(), lambda: message or response(), max_calls=5)
    return value


def solver_intent(plan: CompactionPlan, view: list[BaseMessage], request: PreparedRequest | None = None) -> ModelCallIntent:
    profile = json.loads(plan.model_profile_json)
    return ModelCallIntent(call_id=plan.parent_call_id, task_id=plan.task_id, run_id=plan.run_id, kind="solver",
                           provider_id="dfxfixed" if request is None else profile["settings"]["provider_id"],
                           model_id="offline" if request is None else profile["settings"]["model_id"],
                           prompt_sha256=sha256(canonical([item.model_dump(mode="json") for item in view]))
                           if request is None else sha256(canonical(request.messages)),
                           tool_schema_sha256=sha256(canonical([] if request is None else request.tools)),
                           parameters_sha256=sha256("fixed caller parameters") if request is None else
                           sha256(canonical({"wire_parameters": request.parameters, "body_sha256": request.body_sha256,
                                            "profile_sha256": request.profile_sha256})),
                           tool_names=() if request is None else tuple(item["function"]["name"] for item in request.tools))


@pytest.mark.parametrize("wire", [False, True])
def test_summary_is_separate_no_tool_request_and_keeps_full_groups(tmp_path: Path, wire: bool) -> None:
    store, ledger = setup(tmp_path, wire=wire)
    original = history()
    before = canonical([item.model_dump(mode="json") for item in original])
    plan = store.select(original, "model-2")
    assert plan is not None and plan.pinned_count == 2 and plan.summarized_end == 6
    summary_messages = plan.summary_messages()
    assert summary_messages[0].content == SUMMARY_PROMPT
    quoted = json.loads(summary_messages[1].content)
    assert quoted["range"] == [2, 6]
    assert [item["type"] for item in quoted["messages"]] == ["ai", "ai", "tool", "tool"]
    intent = plan.summary_intent()
    assert intent.kind == "context_compaction" and intent.tool_names == ()
    assert intent.call_id == "compaction-model-2" and intent.tool_schema_sha256 == sha256(canonical([]))
    if wire:
        request = plan.summary_request()
        assert request is not None and request.tools == []
        assert "tools" not in json.loads(request.body) and "tool_choice" not in request.parameters
        assert intent.prompt_sha256 == sha256(canonical(request.messages))
    else:
        assert plan.summary_request() is None
    view = store.complete(plan, save_summary(ledger, plan), ledger)
    assert view[0:2] == original[0:2] and view[-1] == original[-1]
    assert len(view) == 4 and isinstance(view[2], HumanMessage)
    memory = json.loads(view[2].content)
    assert memory["authority"] == "model_inference" and "unknown" in memory["summary"]
    assert memory["source_sha256"] == sha256(before)
    assert canonical([item.model_dump(mode="json") for item in original]) == before
    assert ledger.usage_summary("task1", "run1")["known_total_tokens"] == 23
    assert not (tmp_path / "base").exists()
    assert store.audit(ledger)[0]["outcome"] == "valid"


def test_disabled_and_no_removable_prefix_do_not_create_artifacts(tmp_path: Path) -> None:
    store = CompactionStore(tmp_path, "task1", "run1", CompactionPolicy(), FIXED_PROFILE)
    assert store.select(history(), "model-1") is None
    assert not (tmp_path / "compactions").exists()
    enabled, _ = setup(tmp_path)
    assert enabled.select([HumanMessage(content="x" * 500), AIMessage(content="recent")], "model-1") is None
    assert not (tmp_path / "compactions").exists()


@pytest.mark.parametrize("mutation", ["orphan", "missing", "duplicate", "interrupt", "duplicate_call", "invalid"])
def test_dangling_tool_groups_rejected_before_plan(tmp_path: Path, mutation: str) -> None:
    store, ledger = setup(tmp_path)
    messages = history()
    if mutation == "orphan":
        messages.insert(2, ToolMessage(content="orphan", tool_call_id="absent"))
    elif mutation == "missing":
        del messages[5]
    elif mutation == "duplicate":
        messages.insert(5, messages[4].model_copy(deep=True))
    elif mutation == "interrupt":
        messages.insert(5, HumanMessage(content="interruption"))
    elif mutation == "duplicate_call":
        messages[3].tool_calls[1]["id"] = "read-1"
    else:
        messages[3].invalid_tool_calls = [{"name": "read_source", "args": "bad", "id": "broken", "error": "bad"}]
    with pytest.raises(ContextCompactionError, match="^context_format_unsupported$"):
        store.select(messages, "model-1")
    assert ledger.usage_summary("task1", "run1")["reserved_calls"] == 0
    assert not (tmp_path / "compactions").exists()


def test_reused_ids_in_later_complete_groups_preserve_compaction_source(tmp_path: Path) -> None:
    store, ledger = setup(tmp_path)
    messages = history()
    messages.extend([messages[3].model_copy(deep=True), messages[4].model_copy(deep=True),
                     messages[5].model_copy(deep=True), AIMessage(content="latest group")])
    frozen = canonical([item.model_dump(mode="json") for item in messages])
    plan = store.select(messages, "model-3")
    assert plan is not None and plan.pinned_count == 2 and plan.summarized_end == 10
    assert plan.source_messages_json == frozen
    view = store.complete(plan, save_summary(ledger, plan), ledger)
    assert view[:2] == messages[:2] and view[-1] == messages[-1]
    assert canonical([item.model_dump(mode="json") for item in messages]) == frozen
    assert store.audit(ledger)[0]["outcome"] == "valid"


@pytest.mark.parametrize("field", ["source", "policy", "profile"])
def test_existing_site_cannot_be_skipped_or_rebound(tmp_path: Path, field: str) -> None:
    store, ledger = setup(tmp_path)
    plan_for(store)
    messages = history()
    if field == "source":
        messages[2].content = "x"
    elif field == "policy":
        store = CompactionStore(tmp_path, "task1", "run1", CompactionPolicy(), FIXED_PROFILE)
    else:
        store, _ = setup(tmp_path, wire=True)
    with pytest.raises(ContextCompactionError, match="^context_format_unsupported$"):
        store.select(messages, "model-2")
    assert ledger.usage_summary("task1", "run1")["reserved_calls"] == 0


@pytest.mark.parametrize("wire", [False, True])
def test_input_limit_fails_before_intent_or_model_budget(tmp_path: Path, wire: bool) -> None:
    store, ledger = setup(tmp_path, wire=wire, max_summary_input_bytes=128)
    with pytest.raises(ContextCompactionError, match="^context_compaction_input_limit$"):
        store.select(history(), "model-2")
    assert ledger.usage_summary("task1", "run1")["reserved_calls"] == 0
    assert not (tmp_path / "compactions").exists()


@pytest.mark.parametrize("invalid", ["empty", "blank", "tools", "invalid_tools", "too_large", "unicode"])
def test_invalid_summary_is_paid_and_auditable_without_valid_result(tmp_path: Path, invalid: str) -> None:
    store, ledger = setup(tmp_path, max_summary_bytes=20)
    plan = plan_for(store)
    value = response("short summary")
    if invalid == "empty":
        value.content = ""
    elif invalid == "blank":
        value.content = "  \n"
    elif invalid == "tools":
        value = AIMessage(content="short summary", usage_metadata=value.usage_metadata,
                          tool_calls=[{"id": "evil", "name": "edit_source", "args": {}}])
    elif invalid == "invalid_tools":
        value = AIMessage(content="short summary", usage_metadata=value.usage_metadata,
                          invalid_tool_calls=[{"id": "evil", "name": "edit_source", "args": "bad", "error": "bad"}])
    elif invalid == "too_large":
        value.content = "x" * 21
    else:
        value.content = "界" * 7
    saved = save_summary(ledger, plan, value)
    with pytest.raises(ContextCompactionError, match="^context_summary_invalid$"):
        store.complete(plan, saved, ledger)
    facts = store.audit(ledger)
    assert facts[0]["outcome"] == "invalid" and facts[0]["effective_view_sha256"] is None
    assert not facts[0]["result_saved"] and facts[0]["solver_call_id"] is None
    usage = ledger.usage_summary("task1", "run1")
    assert usage["reserved_calls"] == usage["completed_calls"] == 1 and usage["known_total_tokens"] == 23


@pytest.mark.parametrize("state", ["absent", "reserved", "dispatched", "unknown", "completed"])
def test_audit_crash_windows_is_read_only_and_honest(tmp_path: Path, state: str) -> None:
    store, ledger = setup(tmp_path)
    plan = plan_for(store)
    intent = plan.summary_intent()
    if state != "absent":
        ledger.reserve(intent, max_calls=5)
    if state in ("dispatched", "unknown", "completed"):
        ledger.dispatch(intent.call_id)
    if state == "unknown":
        ledger.mark_unknown(intent.call_id, "test crash")
    if state == "completed":
        ledger.complete(intent, response())
    before = {path.name: path.read_bytes() for path in (tmp_path / "compactions").iterdir()}
    facts = store.audit(ledger)
    assert facts[0]["outcome"] == {"absent": "not_requested", "reserved": "pending", "dispatched": "unknown",
                                    "unknown": "unknown", "completed": "valid"}[state]
    assert not facts[0]["result_saved"]
    assert {path.name: path.read_bytes() for path in (tmp_path / "compactions").iterdir()} == before
    if state == "completed":
        replay, _ = invoke_accounted(ledger, intent, lambda: pytest.fail("completed summary resent"), max_calls=1)
        view = store.complete(plan, replay, ledger)
        assert len(view) == 4
        assert ledger.usage_summary("task1", "run1")["reserved_calls"] == 1


@pytest.mark.parametrize("wire", [False, True])
def test_solver_link_pins_effective_view_and_ledger(tmp_path: Path, wire: bool) -> None:
    store, ledger = setup(tmp_path, wire=wire)
    plan = plan_for(store)
    view = store.complete(plan, save_summary(ledger, plan), ledger)
    request = None
    if wire:
        profile = json.loads(plan.model_profile_json)
        adapter = ChatCompletionsAdapter(ProviderSettings.model_validate(profile["settings"]),
                                         httpx.MockTransport(lambda _: pytest.fail("no network")))
        request = adapter.prepare(view, [{"type": "function", "function": {
            "name": "read_source", "parameters": {"type": "object", "properties": {}}}}])
    intent = solver_intent(plan, view, request)
    ledger.reserve(intent, max_calls=5)
    store.link(plan, intent, request)
    store.link(plan, intent, request)
    facts = store.audit(ledger)
    assert facts[0]["solver_call_id"] == "model-2" and facts[0]["solver_state"] == "reserved"
    assert facts[0]["effective_view_sha256"] == sha256(canonical([item.model_dump(mode="json") for item in view]))
    ledger.dispatch(intent.call_id)
    ledger.complete(intent, AIMessage(content="solver finished"))
    assert store.audit(ledger)[0]["solver_state"] == "completed"
    assert ledger.usage_summary("task1", "run1")["reserved_calls"] == 2


@pytest.mark.parametrize("field", ["kind", "owner", "parent", "prompt", "request"])
def test_wrong_solver_identity_or_send_view_rejected(tmp_path: Path, field: str) -> None:
    store, ledger = setup(tmp_path, wire=True)
    plan = plan_for(store)
    view = store.complete(plan, save_summary(ledger, plan), ledger)
    profile = json.loads(plan.model_profile_json)
    adapter = ChatCompletionsAdapter(ProviderSettings.model_validate(profile["settings"]),
                                     httpx.MockTransport(lambda _: pytest.fail("no request")))
    request = adapter.prepare(view, [])
    intent = solver_intent(plan, view, request)
    if field == "kind":
        intent = intent.model_copy(update={"kind": "context_compaction"})
    elif field == "owner":
        intent = intent.model_copy(update={"task_id": "other"})
    elif field == "parent":
        intent = intent.model_copy(update={"call_id": "model-3"})
    elif field == "prompt":
        intent = intent.model_copy(update={"prompt_sha256": sha256("changed")})
    else:
        view[2].content = "changed memory"
        request = adapter.prepare(view, [])
        intent = solver_intent(plan, view, request)
    with pytest.raises(ContextCompactionError, match="^context_format_unsupported$"):
        store.link(plan, intent, request)
    assert not (tmp_path / "compactions/model-2.solver.json").exists()


@pytest.mark.parametrize("artifact", ["plan", "result", "solver"])
def test_artifact_tampering_rejected_even_when_hashes_are_refreshed(tmp_path: Path, artifact: str) -> None:
    store, ledger = setup(tmp_path)
    plan = plan_for(store)
    view = store.complete(plan, save_summary(ledger, plan), ledger)
    intent = solver_intent(plan, view)
    ledger.reserve(intent, max_calls=5)
    store.link(plan, intent, None)
    path = tmp_path / f"compactions/model-2.{artifact}.json"
    value = json.loads(path.read_text(encoding="utf-8"))
    if artifact == "plan":
        source = json.loads(value["source_messages_json"])
        source[2]["content"] = "rewritten original inference"
        value["source_messages_json"] = canonical(source)
        value["source_sha256"] = sha256(value["source_messages_json"])
    elif artifact == "result":
        changed = json.loads(value["view_messages_json"])
        changed[2]["content"] = "all earlier failed checks passed"
        value["view_messages_json"] = canonical(changed)
        value["view_sha256"] = sha256(value["view_messages_json"])
    else:
        value["solver_intent"]["parameters_sha256"] = sha256("replaced parameters")
    path.write_text(canonical(value), encoding="utf-8")
    with pytest.raises(ContextCompactionError, match="^context_format_unsupported$"):
        store.audit(ledger)
    if artifact == "result":
        with pytest.raises(ContextCompactionError, match="^context_format_unsupported$"):
            store.link(plan, intent, None)


def test_fresh_messages_cannot_mutate_frozen_source_or_replayed_view(tmp_path: Path) -> None:
    store, ledger = setup(tmp_path)
    plan = plan_for(store)
    plan.source_messages()[2].content = "mutation"
    plan.summary_messages()[1].content = "mutation"
    saved = save_summary(ledger, plan)
    view = store.complete(plan, saved, ledger)
    view[2].content = "mutation"
    assert store.complete(plan, saved, ledger)[2].content != "mutation"
    assert store.select(history(), "model-2") == plan


@pytest.mark.parametrize("artifact", ["plan", "result", "solver", "unexpected"])
def test_hardlinked_compaction_file_is_rejected(tmp_path: Path, artifact: str) -> None:
    store, ledger = setup(tmp_path)
    plan = plan_for(store)
    view = store.complete(plan, save_summary(ledger, plan), ledger)
    intent = solver_intent(plan, view)
    ledger.reserve(intent, max_calls=5)
    store.link(plan, intent, None)
    path = tmp_path / f"compactions/model-2.{artifact}.json"
    if artifact == "unexpected":
        path.write_text("{}", encoding="utf-8")
    os.link(path, tmp_path / "external-shared-file")
    with pytest.raises(ContextCompactionError, match="^context_format_unsupported$"):
        store.audit(ledger)


def test_deleted_plan_cannot_hide_summary_ledger(tmp_path: Path) -> None:
    store, ledger = setup(tmp_path)
    plan = plan_for(store)
    save_summary(ledger, plan)
    (tmp_path / "compactions/model-2.plan.json").unlink()
    with pytest.raises(ContextCompactionError, match="^context_format_unsupported$"):
        store.audit(ledger)


@pytest.mark.parametrize("state", ["reserved", "dispatched", "unknown", "completed"])
def test_missing_solver_link_only_allows_undispatched_reservation(tmp_path: Path, state: str) -> None:
    store, ledger = setup(tmp_path)
    plan = plan_for(store)
    view = store.complete(plan, save_summary(ledger, plan), ledger)
    intent = solver_intent(plan, view)
    ledger.reserve(intent, max_calls=5)
    if state != "reserved":
        ledger.dispatch(intent.call_id)
    if state == "unknown":
        ledger.mark_unknown(intent.call_id, "test crash")
    if state == "completed":
        ledger.complete(intent, AIMessage(content="finished"))
    if state == "reserved":
        facts = store.audit(ledger)
        assert facts[0]["solver_call_id"] == "model-2" and facts[0]["solver_state"] == "reserved"
    else:
        with pytest.raises(ContextCompactionError, match="^context_format_unsupported$"):
            store.audit(ledger)


def test_solver_link_cannot_mask_a_deleted_summary_result(tmp_path: Path) -> None:
    store, ledger = setup(tmp_path)
    plan = plan_for(store)
    view = store.complete(plan, save_summary(ledger, plan), ledger)
    intent = solver_intent(plan, view)
    ledger.reserve(intent, max_calls=5)
    store.link(plan, intent, None)
    (tmp_path / "compactions/model-2.result.json").unlink()
    with pytest.raises(ContextCompactionError, match="^context_format_unsupported$"):
        store.audit(ledger)


def test_summary_that_does_not_shorten_history_is_paid_and_invalid(tmp_path: Path) -> None:
    store, ledger = setup(tmp_path)
    plan = plan_for(store)
    saved = save_summary(ledger, plan, response("expanded speculative history " * 200))
    with pytest.raises(ContextCompactionError, match="^context_summary_invalid$"):
        store.complete(plan, saved, ledger)
    assert store.audit(ledger)[0]["outcome"] == "invalid"
    assert ledger.usage_summary("task1", "run1")["completed_calls"] == 1


def test_oversized_artifact_is_rejected_before_parsing(tmp_path: Path) -> None:
    store, ledger = setup(tmp_path)
    plan_for(store)
    path = tmp_path / "compactions/model-2.plan.json"
    with path.open("wb") as output:
        output.truncate(MAX_ARTIFACT_BYTES + 1)
    with pytest.raises(ContextCompactionError, match="^context_format_unsupported$"):
        store.audit(ledger)


def test_interrupted_atomic_result_keeps_completed_summary_replayable(tmp_path: Path,
                                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    store, ledger = setup(tmp_path)
    plan = plan_for(store)
    saved = save_summary(ledger, plan)
    replace = os.replace

    def interrupted(source, destination):
        raise OSError("simulated before publish")

    monkeypatch.setattr(os, "replace", interrupted)
    with pytest.raises(ContextCompactionError, match="^context_format_unsupported$"):
        store.complete(plan, saved, ledger)
    assert not (tmp_path / "compactions/model-2.result.json").exists()
    assert not list(tmp_path.glob(".compaction-write-*.tmp"))
    assert store.audit(ledger)[0]["outcome"] == "valid"
    monkeypatch.setattr(os, "replace", replace)
    store.complete(plan, saved, ledger)
    assert store.audit(ledger)[0]["result_saved"]


@pytest.mark.parametrize("field", ["enabled", "trigger_bytes", "retain_groups", "max_summary_bytes", "extra"])
def test_policy_is_strict_and_bounded(field: str) -> None:
    values = {"enabled": "true", "trigger_bytes": 127, "retain_groups": 0, "max_summary_bytes": 65_537, "extra": 1}
    with pytest.raises(ValueError):
        CompactionPolicy.model_validate({field: values[field]})
