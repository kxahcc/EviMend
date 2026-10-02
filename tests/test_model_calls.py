"""Synthetic model-call accounting and crash-window tests; no provider is contacted."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage

from dfx_agent.model_calls import (
    ModelCallBudget,
    ModelCallConflict,
    ModelCallIntent,
    ModelCallStore,
    ModelCallUncertain,
    invoke_accounted,
    sha256,
)


def intent(call_id: str = "call1", **changes: object) -> ModelCallIntent:
    data = {"call_id": call_id, "task_id": "task1", "run_id": "run1", "kind": "solver",
            "provider_id": "synthetic-provider",
            "model_id": "provider/model-v1", "prompt_sha256": sha256("prompt"),
            "tool_schema_sha256": sha256("tool schema"), "parameters_sha256": sha256("params"),
            "tool_names": ("inspect_source",)}
    return ModelCallIntent.model_validate({**data, **changes})


def response(*, name: str = "inspect_source", usage: bool = True) -> AIMessage:
    return AIMessage(content="", id="provider-response-1",
                     tool_calls=[{"name": name, "args": {"path": "src/Main.ets"}, "id": "tool1"}],
                     usage_metadata=({"input_tokens": 12, "output_tokens": 4, "total_tokens": 16}
                                     if usage else None))


def test_completed_response_is_replayed_after_reopen_without_provider_call(tmp_path: Path) -> None:
    path = tmp_path / "calls.db"
    store = ModelCallStore(path)
    calls = 0

    def provider() -> AIMessage:
        nonlocal calls
        calls += 1
        return response()

    first, first_observation = invoke_accounted(store, intent(), provider, max_calls=2)
    assert calls == 1
    assert first_observation.format_status == "tool_calls"
    assert first_observation.usage_status == "known"
    assert first_observation.tool_calls[0].arguments_sha256 == sha256('{"path":"src/Main.ets"}')
    reopened = ModelCallStore(path)
    replayed, observation = invoke_accounted(reopened, intent(), provider, max_calls=2)
    assert calls == 1 and replayed == first and observation == first_observation
    assert reopened.usage_summary("task1", "run1") == {
        "reserved_calls": 1, "completed_calls": 1, "uncertain_calls": 0,
        "missing_usage_calls": 0, "invalid_usage_calls": 0, "known_input_tokens": 12,
        "known_output_tokens": 4, "known_total_tokens": 16}
    with pytest.raises(ModelCallConflict, match="different request"):
        reopened.reserve(intent(prompt_sha256=sha256("changed")), max_calls=2)


def test_dispatched_call_is_not_resent_after_process_restart(tmp_path: Path) -> None:
    path = tmp_path / "calls.db"
    store = ModelCallStore(path)
    store.reserve(intent(), max_calls=2)
    store.dispatch("call1")
    reopened = ModelCallStore(path)
    called = False

    def forbidden() -> AIMessage:
        nonlocal called
        called = True
        return response()

    with pytest.raises(ModelCallUncertain, match="may exist"):
        invoke_accounted(reopened, intent(), forbidden, max_calls=2)
    assert not called
    reopened.mark_unknown("call1", "response_lost")
    with pytest.raises(ModelCallBudget):
        reopened.reserve(intent("call2", kind="format_repair"), max_calls=1)
    reopened.reserve(intent("call2", kind="format_repair"), max_calls=2)
    assert reopened.usage_summary("task1", "run1")["uncertain_calls"] == 1
    assert reopened.usage_summary("task1", "run1")["reserved_calls"] == 2


def test_provider_exception_preserves_unknown_cost_and_prevents_same_id_retry(tmp_path: Path) -> None:
    store = ModelCallStore(tmp_path / "calls.db")

    def failed_provider() -> AIMessage:
        raise TimeoutError("synthetic timeout")

    with pytest.raises(TimeoutError, match="synthetic timeout"):
        invoke_accounted(store, intent(), failed_provider, max_calls=2)
    assert store.get("call1").state == "unknown"  # type: ignore[union-attr]
    assert store.get("call1").uncertainty_reason == "TimeoutError"  # type: ignore[union-attr]
    with pytest.raises(ModelCallUncertain):
        invoke_accounted(store, intent(), response, max_calls=2)
    assert store.usage_summary("task1", "run1")["known_total_tokens"] == 0


def test_bad_format_and_missing_usage_are_retained_as_a_completed_call(tmp_path: Path) -> None:
    store = ModelCallStore(tmp_path / "calls.db")
    _, observation = invoke_accounted(store, intent(), lambda: response(name="unoffered_tool", usage=False),
                                      max_calls=2)
    assert observation.format_status == "format_error"
    assert "unknown_tool_name" in observation.format_errors
    assert observation.usage_status == "missing"
    assert observation.usage is None
    assert store.usage_summary("task1", "run1")["missing_usage_calls"] == 1
    repair = intent("call2", kind="format_repair")
    _, fixed = invoke_accounted(store, repair, response, max_calls=2)
    assert fixed.format_status == "tool_calls"
    summary = store.usage_summary("task1", "run1")
    assert summary["reserved_calls"] == summary["completed_calls"] == 2
    assert summary["known_total_tokens"] == 16
    assert summary["missing_usage_calls"] == 1


def test_completion_is_idempotent_only_for_the_same_response(tmp_path: Path) -> None:
    store = ModelCallStore(tmp_path / "calls.db")
    request = intent()
    store.reserve(request, max_calls=1)
    with pytest.raises(ModelCallConflict, match="not dispatched"):
        store.complete(request, response())
    store.dispatch(request.call_id)
    observed = store.complete(request, response())
    assert store.complete(request, response()) == observed
    with pytest.raises(ModelCallConflict, match="different response"):
        store.complete(request, AIMessage(content="different", usage_metadata=None))
    with pytest.raises(ModelCallConflict, match="not an undispatched"):
        store.dispatch(request.call_id)


def test_replay_rejects_changed_local_response(tmp_path: Path) -> None:
    path = tmp_path / "calls.db"
    store = ModelCallStore(path)
    invoke_accounted(store, intent(), response, max_calls=1)
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE model_calls SET message_json=? WHERE call_id=?",
                           (AIMessage(content="tampered").model_dump_json(), "call1"))
    with pytest.raises(ModelCallConflict, match="stored response changed"):
        store.replay(intent())


def test_replay_rejects_changed_observation(tmp_path: Path) -> None:
    path = tmp_path / "calls.db"
    store = ModelCallStore(path)
    invoke_accounted(store, intent(), response, max_calls=1)
    with sqlite3.connect(path) as connection:
        original = connection.execute("SELECT observation_json FROM model_calls WHERE call_id='call1'").fetchone()[0]
        changed = json.loads(original)
        changed["format_status"] = "text_only"
        connection.execute("UPDATE model_calls SET observation_json=? WHERE call_id='call1'", (json.dumps(changed),))
    with pytest.raises(ModelCallConflict, match="stored observation changed"):
        store.replay(intent())


def test_summary_rejects_changed_ledger_identity(tmp_path: Path) -> None:
    path = tmp_path / "calls.db"
    store = ModelCallStore(path)
    store.reserve(intent(), max_calls=1)
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE model_calls SET run_id=? WHERE call_id=?", ("other_run", "call1"))
    with pytest.raises(ModelCallConflict, match="stored request identity changed"):
        store.usage_summary("task1", "other_run")


def test_records_keep_reservation_order_all_states_and_interleaved_kinds(tmp_path: Path) -> None:
    store = ModelCallStore(tmp_path / "calls.db")
    store.reserve(intent("z-solver"), max_calls=10)
    invoke_accounted(store, intent("a-compaction", kind="context_compaction"), response, max_calls=10)
    store.reserve(intent("m-solver"), max_calls=10)
    store.dispatch("m-solver")
    store.mark_unknown("m-solver", "response_lost")
    store.reserve(intent("b-compaction", kind="context_compaction"), max_calls=10)
    store.dispatch("b-compaction")
    invoke_accounted(store, intent("f-format", kind="format_repair"), response, max_calls=10)
    store.reserve(intent("r-retry", kind="sdk_retry"), max_calls=10)

    records = store.records("task1", "run1")
    assert [item.intent.call_id for item in records] == [
        "z-solver", "a-compaction", "m-solver", "b-compaction", "f-format", "r-retry"]
    assert [item.state for item in records] == [
        "reserved", "completed", "unknown", "dispatched", "completed", "reserved"]
    assert records[1] == store.get("a-compaction")
    assert records[1].observation is not None
    assert records[1].observation.tool_calls[0].arguments_sha256 == sha256('{"path":"src/Main.ets"}')
    assert records[2].observation is None and records[2].uncertainty_reason == "response_lost"
    assert [item.intent.call_id for item in store.records("task1", "run1", kind="solver")] == [
        "z-solver", "m-solver"]
    assert [item.intent.call_id for item in store.records("task1", "run1", kind="context_compaction")] == [
        "a-compaction", "b-compaction"]
    assert [item.intent.call_id for item in store.records("task1", "run1", kind="format_repair")] == ["f-format"]
    assert [item.intent.call_id for item in store.records("task1", "run1", kind="sdk_retry")] == ["r-retry"]
    assert store.usage_summary("task1", "run1") == {
        "reserved_calls": 6, "completed_calls": 2, "uncertain_calls": 2,
        "missing_usage_calls": 0, "invalid_usage_calls": 0, "known_input_tokens": 24,
        "known_output_tokens": 8, "known_total_tokens": 32}


def test_records_use_exact_owner_and_do_not_expand_an_empty_match(tmp_path: Path) -> None:
    store = ModelCallStore(tmp_path / "calls.db")
    store.reserve(intent("owned"), max_calls=2)
    store.reserve(intent("other-run", run_id="run2", kind="context_compaction"), max_calls=2)
    store.reserve(intent("other-task", task_id="task2", kind="context_compaction"), max_calls=2)
    assert [item.intent.call_id for item in store.records("task1", "run1")] == ["owned"]
    assert [item.intent.call_id for item in store.records("task1", "run2")] == ["other-run"]
    assert [item.intent.call_id for item in store.records("task2", "run1")] == ["other-task"]
    assert store.records("absent-task", "run1") == []
    assert store.records("task1", "run1", kind="context_compaction") == []


@pytest.mark.parametrize("kind", ["", "all", "SOLVER", "solver%", "solver OR 1=1"])
def test_records_reject_unsupported_kind_without_widening(tmp_path: Path, kind: str) -> None:
    store = ModelCallStore(tmp_path / "calls.db")
    store.reserve(intent(), max_calls=1)
    with pytest.raises(ValueError, match="invalid model call kind"):
        store.records("task1", "run1", kind=kind)


@pytest.mark.parametrize(("task_id", "run_id"), [("", "run1"), ("task1", ""), ("%", "run1"),
                                                  ("task1", "run1' OR 1=1")])
def test_records_reject_invalid_owner_without_widening(tmp_path: Path, task_id: str, run_id: str) -> None:
    store = ModelCallStore(tmp_path / "calls.db")
    store.reserve(intent(), max_calls=1)
    with pytest.raises(ValueError, match="invalid model ledger owner"):
        store.records(task_id, run_id)


def test_records_are_read_only_fresh_values(tmp_path: Path) -> None:
    path = tmp_path / "calls.db"
    store = ModelCallStore(path)
    invoke_accounted(store, intent(), response, max_calls=2)
    store.reserve(intent("pending", kind="context_compaction"), max_calls=2)
    with sqlite3.connect(path) as connection:
        before_version = connection.execute("PRAGMA data_version").fetchone()[0]
        before_rows = list(connection.iterdump())
        first = store.records("task1", "run1")
        second = store.records("task1", "run1")
        assert first == second and first is not second
        assert first[0] is not second[0]
        assert first[0].intent is not second[0].intent
        assert first[0].observation is not second[0].observation
        first.clear()
        assert store.records("task1", "run1") == second
        assert store.records("task1", "run1", kind="context_compaction") == [second[1]]
        assert connection.execute("PRAGMA data_version").fetchone()[0] == before_version
        assert list(connection.iterdump()) == before_rows


def test_records_validate_owned_records_before_kind_filter(tmp_path: Path) -> None:
    path = tmp_path / "calls.db"
    store = ModelCallStore(path)
    store.reserve(intent("summary", kind="context_compaction"), max_calls=1)
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE model_calls SET request_sha256=? WHERE call_id=?", (sha256("changed"), "summary"))
    with pytest.raises(ModelCallConflict, match="stored request identity changed"):
        store.records("task1", "run1", kind="solver")
