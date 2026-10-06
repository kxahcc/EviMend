"""Synthetic independent learning ledger tests; no transport or real model is used."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Never

import pytest
from langchain_core.messages import AIMessage
from pydantic import ValidationError

from dfx_agent.model_calls import (
    MAX_LEARNING_PARENT_DEPTH,
    ModelCallBudget,
    ModelCallConflict,
    ModelCallIntent,
    ModelCallIntentV2,
    ModelCallStore,
    ModelCallUncertain,
    canonical,
    invoke_accounted,
    normalize_message,
    parse_model_call_intent,
    sha256,
)


def learning_intent(call_id: str = "extract-1", **changes: Any) -> ModelCallIntentV2:
    return ModelCallIntentV2.model_validate({
        "call_id": call_id, "task_id": "learning-task", "run_id": "learning-run",
        "kind": "experience_extraction", "provider_id": "synthetic-provider", "model_id": "synthetic-model",
        "prompt_sha256": sha256("learning prompt"), "tool_schema_sha256": sha256("[]"),
        "parameters_sha256": sha256("learning parameters"), "tool_names": (),
        "source_binding_sha256": sha256("approved source"), "learning_owner_sha256": sha256("learning owner"),
        **changes,
    })


def legacy_intent(call_id: str = "solver-1", **changes: Any) -> ModelCallIntent:
    return ModelCallIntent.model_validate({
        "call_id": call_id, "task_id": "learning-task", "run_id": "learning-run", "kind": "solver",
        "provider_id": "synthetic-provider", "model_id": "synthetic-model",
        "prompt_sha256": sha256("solver prompt"), "tool_schema_sha256": sha256("[]"),
        "parameters_sha256": sha256("solver parameters"), "tool_names": (), **changes,
    })


def response(*, usage: bool = True) -> AIMessage:
    return AIMessage(content="pending procedure", id="synthetic-response",
                     usage_metadata={"input_tokens": 12, "output_tokens": 4, "total_tokens": 16} if usage else None)


def repair(call_id: str, parent: str, **changes: Any) -> ModelCallIntentV2:
    return learning_intent(call_id, kind="learning_format_repair", parent_call_id=parent,
                           prompt_sha256=sha256("repair " + call_id), **changes)


def test_explicit_generation_preserves_legacy_identity_and_closed_kind_enum() -> None:
    original = legacy_intent()
    assert parse_model_call_intent(original.model_dump_json()) == original
    assert original.sha256() == sha256(canonical(original.model_dump(mode="json")))
    with pytest.raises(ValidationError):
        legacy_intent(kind="experience_extraction")
    current = learning_intent()
    assert parse_model_call_intent(current.model_dump_json()) == current
    assert current.schema_version == 2 and current.tool_names == ()


@pytest.mark.parametrize("schema", [True, False, 1, 2.0, "2", 3])
def test_learning_schema_requires_exact_integer_two(schema: object) -> None:
    with pytest.raises(ValidationError):
        learning_intent(schema_version=schema)


@pytest.mark.parametrize("changes", [
    {"tool_names": ("run_command",)}, {"kind": "solver"}, {"parent_call_id": "prior"},
    {"kind": "learning_format_repair"}, {"kind": "learning_format_repair", "parent_call_id": "extract-1"},
    {"learning_owner_sha256": "not-a-hash"}, {"source_binding_sha256": "not-a-hash"},
])
def test_learning_intent_cannot_grant_tools_or_omit_lineage(changes: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        learning_intent(**changes)


def test_preflight_failure_is_unsent_reserved_and_recovery_reuses_reservation(tmp_path: Path) -> None:
    store = ModelCallStore(tmp_path / "calls.db")
    request = learning_intent()
    sent = 0

    def provider() -> AIMessage:
        nonlocal sent
        sent += 1
        return response()

    def reject() -> None:
        raise ValueError("preflight source drift")

    with pytest.raises(ValueError, match="preflight source drift"):
        invoke_accounted(store, request, provider, max_calls=1, preflight=reject)
    saved = store.get(request.call_id)
    assert saved is not None and saved.state == "reserved"
    assert saved.observation is None and saved.uncertainty_reason is None and sent == 0
    assert store.usage_summary(request.task_id, request.run_id)["reserved_calls"] == 1
    with pytest.raises(ModelCallBudget):
        store.reserve(learning_intent("extract-2"), max_calls=1)
    message, observation = invoke_accounted(store, request, provider, max_calls=1, preflight=lambda: None)
    assert sent == 1 and message == response() and observation.usage_status == "known"
    assert store.usage_summary(request.task_id, request.run_id)["reserved_calls"] == 1


def test_dispatched_unknown_never_preflights_or_resends_on_resume(tmp_path: Path) -> None:
    path = tmp_path / "calls.db"
    store = ModelCallStore(path)
    request = learning_intent()

    def fail() -> AIMessage:
        raise TimeoutError("private provider exception body")

    with pytest.raises(TimeoutError):
        invoke_accounted(store, request, fail, max_calls=2)
    record = store.get(request.call_id)
    assert record is not None and record.state == "unknown" and record.uncertainty_reason == "TimeoutError"

    def forbidden() -> Never:
        pytest.fail("unknown call attempted preflight or dispatch")

    with pytest.raises(ModelCallUncertain):
        invoke_accounted(ModelCallStore(path), request, forbidden, max_calls=2, preflight=forbidden)
    assert "private provider exception body" not in path.read_bytes().decode("utf-8", errors="ignore")
    usage = store.usage_summary(request.task_id, request.run_id)
    assert usage["uncertain_calls"] == 1 and usage["completed_calls"] == 0


def test_completed_replay_survives_reopen_and_exhausted_new_request_budget(tmp_path: Path) -> None:
    path = tmp_path / "calls.db"
    request = learning_intent()
    first = invoke_accounted(ModelCallStore(path), request, response, max_calls=1, max_known_tokens=1)

    def forbidden() -> AIMessage:
        pytest.fail("completed learning call was repeated")

    replayed = invoke_accounted(ModelCallStore(path), request, forbidden, max_calls=1,
                               max_known_tokens=1, preflight=lambda: pytest.fail("replay performed preflight"))
    assert replayed == first
    assert first[1].usage is not None and first[1].usage.total_tokens == 16
    with pytest.raises(ModelCallBudget, match="token threshold"):
        invoke_accounted(ModelCallStore(path), learning_intent("extract-2"), forbidden,
                         max_calls=2, max_known_tokens=1)


def test_extraction_and_format_repair_share_call_and_token_budget(tmp_path: Path) -> None:
    store = ModelCallStore(tmp_path / "calls.db")
    first, _ = invoke_accounted(store, learning_intent(), response, max_calls=2, max_known_tokens=100)
    second, _ = invoke_accounted(store, repair("repair-1", "extract-1"), response,
                                 max_calls=2, max_known_tokens=100)
    assert first == second
    assert [row.intent.kind for row in store.records("learning-task", "learning-run")] == [
        "experience_extraction", "learning_format_repair"]
    assert store.usage_summary("learning-task", "learning-run")["known_total_tokens"] == 32
    with pytest.raises(ModelCallBudget, match="count exhausted"):
        store.reserve(learning_intent("extract-2"), max_calls=2)
    with pytest.raises(ModelCallBudget, match="token threshold"):
        store.reserve(learning_intent("extract-2"), max_calls=3, max_known_tokens=32)


def test_reserved_unsent_request_cannot_dispatch_after_other_calls_consume_tokens(tmp_path: Path) -> None:
    store = ModelCallStore(tmp_path / "calls.db")
    waiting = learning_intent("extract-waiting")
    store.reserve(waiting, max_calls=2, max_known_tokens=16)
    invoke_accounted(store, learning_intent(), response, max_calls=2, max_known_tokens=16)
    with pytest.raises(ModelCallBudget, match="token threshold"):
        invoke_accounted(store, waiting, lambda: pytest.fail("unsent reservation bypassed token cutoff"),
                         max_calls=2, max_known_tokens=16)
    record = store.get(waiting.call_id)
    assert record is not None and record.state == "reserved"


@pytest.mark.parametrize("limit", [True, 1.0, "1", 0])
def test_learning_call_budget_requires_exact_positive_integer(tmp_path: Path, limit: Any) -> None:
    with pytest.raises((ValueError, TypeError)):
        ModelCallStore(tmp_path / "calls.db").reserve(learning_intent(), max_calls=limit)


@pytest.mark.parametrize("usage_kind", ["missing", "invalid_raw", "invalid_normalized"])
def test_incomplete_usage_keeps_known_subtotal_and_unknown_count(tmp_path: Path, usage_kind: str) -> None:
    store = ModelCallStore(tmp_path / "calls.db")
    if usage_kind == "missing":
        message = response(usage=False)
    elif usage_kind == "invalid_raw":
        message = AIMessage(content="pending", response_metadata={"dfx_provider_usage_raw": {
            "prompt_tokens": 12, "completion_tokens": 4, "total_tokens": 99}})
    else:
        message = AIMessage(content="pending", usage_metadata={"input_tokens": 12, "output_tokens": 4,
                                                               "total_tokens": 99})
    _, observation = invoke_accounted(store, learning_intent(), lambda: message, max_calls=2)
    assert observation.usage is None
    assert observation.usage_status == ("missing" if usage_kind == "missing" else "invalid")
    invoke_accounted(store, repair("repair-1", "extract-1"), response, max_calls=2)
    summary = store.usage_summary("learning-task", "learning-run")
    assert summary["known_total_tokens"] == 16 and summary["reserved_calls"] == 2
    assert summary["missing_usage_calls"] + summary["invalid_usage_calls"] == 1


def test_format_error_is_completed_accounted_and_repair_retains_parent(tmp_path: Path) -> None:
    store = ModelCallStore(tmp_path / "calls.db")
    invalid = AIMessage(content="", tool_calls=[{"name": "unsafe", "args": {}, "id": "tool1"}],
                        usage_metadata={"input_tokens": 12, "output_tokens": 4, "total_tokens": 16})
    _, first = invoke_accounted(store, learning_intent(), lambda: invalid, max_calls=3)
    assert first.format_status == "format_error" and first.usage_status == "known"
    invoke_accounted(store, repair("repair-1", "extract-1"), response, max_calls=3)
    invoke_accounted(store, repair("repair-2", "repair-1"), response, max_calls=3)
    rows = store.records("learning-task", "learning-run", kind="learning_format_repair")
    assert [row.intent.parent_call_id for row in rows if isinstance(row.intent, ModelCallIntentV2)] == [
        "extract-1", "repair-1"]
    assert store.usage_summary("learning-task", "learning-run")["known_total_tokens"] == 48


@pytest.mark.parametrize("state", ["missing", "reserved", "dispatched", "unknown"])
def test_format_repair_requires_saved_completed_parent(tmp_path: Path, state: str) -> None:
    store = ModelCallStore(tmp_path / "calls.db")
    if state != "missing":
        store.reserve(learning_intent(), max_calls=2)
    if state in {"dispatched", "unknown"}:
        store.dispatch("extract-1")
    if state == "unknown":
        store.mark_unknown("extract-1", "TimeoutError")
    with pytest.raises(ModelCallConflict):
        store.reserve(repair("repair-1", "extract-1"), max_calls=2)
    assert store.get("repair-1") is None


@pytest.mark.parametrize("changes", [
    {"task_id": "other-task"}, {"run_id": "other-run"}, {"provider_id": "other-provider"},
    {"model_id": "other-model"}, {"source_binding_sha256": sha256("other-source")},
    {"learning_owner_sha256": sha256("other-owner")},
])
def test_format_repair_cannot_change_parent_ownership_or_source(tmp_path: Path, changes: dict[str, Any]) -> None:
    store = ModelCallStore(tmp_path / "calls.db")
    invoke_accounted(store, learning_intent(), response, max_calls=2)
    with pytest.raises(ModelCallConflict):
        store.reserve(repair("repair-1", "extract-1", **changes), max_calls=2)
    assert store.get("repair-1") is None


@pytest.mark.parametrize("order", ["legacy-first", "learning-first"])
def test_same_owner_cannot_mix_legacy_and_learning_generations(tmp_path: Path, order: str) -> None:
    store = ModelCallStore(tmp_path / "calls.db")
    first, second = (legacy_intent(), learning_intent()) if order == "legacy-first" else (
        learning_intent(), legacy_intent())
    store.reserve(first, max_calls=2)
    with pytest.raises(ModelCallConflict, match="generations"):
        store.reserve(second, max_calls=2)
    assert store.get(second.call_id) is None


@pytest.mark.parametrize("field", ["source_binding_sha256", "learning_owner_sha256"])
def test_source_owner_binding_is_frozen_across_all_extractions(tmp_path: Path, field: str) -> None:
    store = ModelCallStore(tmp_path / "calls.db")
    store.reserve(learning_intent(), max_calls=2)
    with pytest.raises(ModelCallConflict, match="mix sources"):
        store.reserve(learning_intent("extract-2", **{field: sha256("changed")}), max_calls=2)
    assert store.get("extract-2") is None


@pytest.mark.parametrize("column", ["message_json", "observation_json", "request_sha256", "task_id"])
def test_learning_read_and_summary_reject_tampered_complete_record(tmp_path: Path, column: str) -> None:
    path = tmp_path / "calls.db"
    store = ModelCallStore(path)
    invoke_accounted(store, learning_intent(), response, max_calls=2)
    values = {"message_json": AIMessage(content="changed").model_dump_json(),
              "request_sha256": sha256("changed"), "task_id": "other-task"}
    with sqlite3.connect(path) as connection:
        if column == "observation_json":
            data = json.loads(connection.execute("SELECT observation_json FROM model_calls").fetchone()[0])
            data["usage"] = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
            changed = canonical(data)
        else:
            changed = values[column]
        connection.execute(f"UPDATE model_calls SET {column}=? WHERE call_id='extract-1'", (changed,))
    with pytest.raises(ModelCallConflict):
        store.get("extract-1")
    with pytest.raises(ModelCallConflict):
        store.usage_summary("other-task" if column == "task_id" else "learning-task", "learning-run")
    with pytest.raises(ModelCallConflict):
        store.reserve(learning_intent("extract-2", task_id="other-task" if column == "task_id" else "learning-task"),
                      max_calls=2, max_known_tokens=100)
    assert store.get("extract-2") is None


def test_repaired_read_reaudits_original_parent_response(tmp_path: Path) -> None:
    path = tmp_path / "calls.db"
    store = ModelCallStore(path)
    invoke_accounted(store, learning_intent(), response, max_calls=2)
    request = repair("repair-1", "extract-1")
    invoke_accounted(store, request, response, max_calls=2)
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE model_calls SET message_json=? WHERE call_id='extract-1'",
                           (AIMessage(content="changed").model_dump_json(),))
    with pytest.raises(ModelCallConflict, match="stored response changed"):
        store.replay(request)


def test_repaired_parent_cycle_cannot_pass_rehashed_records(tmp_path: Path) -> None:
    path = tmp_path / "calls.db"
    store = ModelCallStore(path)
    invoke_accounted(store, learning_intent(), response, max_calls=2)
    invoke_accounted(store, repair("repair-1", "extract-1"), response, max_calls=2)
    forged = repair("extract-1", "repair-1")
    message = response()
    observation = normalize_message(forged, message)
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE model_calls SET intent_json=?,request_sha256=?,observation_json=? "
                           "WHERE call_id='extract-1'", (canonical(forged.model_dump(mode="json")), forged.sha256(),
                                                       canonical(observation.model_dump(mode="json"))))
    with pytest.raises(ModelCallConflict, match="cyclic"):
        store.get("repair-1")


def test_format_repair_does_not_accept_completed_legacy_solver_parent(tmp_path: Path) -> None:
    store = ModelCallStore(tmp_path / "calls.db")
    invoke_accounted(store, legacy_intent(), response, max_calls=2)
    with pytest.raises(ModelCallConflict, match="ownership or source differs"):
        store.reserve(repair("repair-1", "solver-1"), max_calls=2)
    assert store.get("repair-1") is None


def test_learning_parent_depth_is_bounded_even_for_internally_rehashed_records(tmp_path: Path) -> None:
    path = tmp_path / "calls.db"
    store = ModelCallStore(path)
    previous: str | None = None
    with sqlite3.connect(path) as connection:
        for index in range(MAX_LEARNING_PARENT_DEPTH + 1):
            call_id = f"chain-{index}"
            request = learning_intent(call_id) if previous is None else repair(call_id, previous)
            message = response()
            observation = normalize_message(request, message)
            connection.execute("INSERT INTO model_calls VALUES (?,?,?,?,?,?,?,?,NULL)", (
                request.call_id, request.task_id, request.run_id, request.sha256(),
                canonical(request.model_dump(mode="json")), "completed",
                canonical(observation.model_dump(mode="json")), canonical(message.model_dump(mode="json"))))
            previous = call_id
    assert previous is not None
    with pytest.raises(ModelCallConflict, match="excessive"):
        store.get(previous)


def test_learning_same_call_identity_cannot_reuse_changed_prompt(tmp_path: Path) -> None:
    store = ModelCallStore(tmp_path / "calls.db")
    store.reserve(learning_intent(), max_calls=1)
    with pytest.raises(ModelCallConflict, match="different request"):
        store.reserve(learning_intent(prompt_sha256=sha256("changed prompt")), max_calls=1)
    assert len(store.records("learning-task", "learning-run")) == 1


def test_learning_records_and_read_only_replay_do_not_write_or_reset_counts(tmp_path: Path) -> None:
    path = tmp_path / "calls.db"
    store = ModelCallStore(path)
    invoke_accounted(store, learning_intent(), response, max_calls=2)
    store.reserve(repair("repair-1", "extract-1"), max_calls=2)
    with sqlite3.connect(path) as connection:
        before = list(connection.iterdump())
    reader = ModelCallStore(path, read_only=True)
    assert [item.intent.call_id for item in reader.records("learning-task", "learning-run")] == [
        "extract-1", "repair-1"]
    assert reader.replay(learning_intent())[0] == response()
    assert reader.usage_summary("learning-task", "learning-run")["reserved_calls"] == 2
    with pytest.raises(ValueError, match="read-only"):
        reader.reserve(learning_intent("extract-3"), max_calls=3)
    with sqlite3.connect(path) as connection:
        assert list(connection.iterdump()) == before
