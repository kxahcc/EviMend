"""Source permission, exact request reconstruction and non-promotion counterexamples."""

from __future__ import annotations

import json
import shutil
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

import httpx
import pytest
from langchain_core.messages import AIMessage
from learning_workflow_support import adapter_for, approval_for, inventory, response
from test_knowledge_contracts import guidance

from dfx_agent.evidence_io import local_evidence_path
from dfx_agent.feedback import FeedbackStore
from dfx_agent.feedback_contracts import FeedbackLimits, HostFeedbackApproval
from dfx_agent.learning import LearningStore, _cost, _projection
from dfx_agent.learning_contracts import HostLearningApproval
from dfx_agent.model_calls import (
    ModelCallIntentV2,
    ModelCallRecord,
    ModelCallStore,
    canonical,
    sha256,
)
from dfx_agent.model_provider import AdapterRequestError

pytest_plugins = ("learning_workflow_support",)


@pytest.mark.parametrize("variant", ["duplicate", "extra", "boolean_schema", "missing_schema", "nonfinite", "surrogate", "tools", "incomplete"])
def test_learning_content_cannot_self_authorize_or_accept_ambiguous_json(variant: str) -> None:
    value = guidance().model_dump(mode="json")
    text = canonical(value)
    if variant == "duplicate":
        text = text[:-1] + ',"trigger":"overwritten"}'
    elif variant == "extra":
        value["derivation_authority"] = "host_curated"
        value["validation_scopes"] = [{"level": "synthetic_fixture", "result": "passed", "scope_names": ["all"]}]
        text = canonical(value)
    elif variant == "boolean_schema":
        value["schema_version"] = True
        text = canonical(value)
    elif variant == "missing_schema":
        value.pop("schema_version")
        text = canonical(value)
    elif variant == "nonfinite":
        text = text[:-1] + ',"unknown_cost":NaN}'
    elif variant == "surrogate":
        text = "\ud800"
    message = AIMessage(content=text, response_metadata={"finish_reason": "length" if variant == "incomplete" else "stop"})
    if variant == "tools":
        message = AIMessage(content=text, tool_calls=[{"id": "bad", "name": "edit_source", "args": {}}],
            response_metadata={"finish_reason": "stop"})
    with pytest.raises(ValueError):
        _projection(message)


@pytest.mark.parametrize("origin", ["candidate_validation", "confirmation"])
def test_learning_cannot_reassign_validation_feedback_origin(origin: str) -> None:
    with pytest.raises(ValueError):
        HostFeedbackApproval(approval_id="no-learning", manifest_sha256="a" * 64, origin_split=origin,
            allowed_splits=("development",))


@pytest.mark.parametrize("schema", [True, 1.0, "1", 2])
def test_learning_approval_requires_exact_schema(schema: Any) -> None:
    with pytest.raises(ValueError):
        HostLearningApproval(schema_version=schema, approval_id="host", operation_id="op", run_id="r",
            capsule_id="feedback-" + "a" * 64,
            root_binding_sha256="b" * 64,
            feedback_approval=HostFeedbackApproval(approval_id="f", manifest_sha256="a" * 64, allowed_splits=("development",)),
            provider_settings={"provider_id": "mock", "model_id": "m"}, max_calls=1, max_known_tokens=1)


def test_changed_feedback_original_rejected_before_owner_request_or_writable_ledger(learning_feedback, tmp_path: Path) -> None:
    source, capsule, _, _ = learning_feedback
    copied = local_evidence_path(tmp_path / "feedback")
    shutil.copytree(source.root, copied)
    feedback = FeedbackStore(copied)
    original = copied / "capsules" / capsule.capsule_id / "payload"
    patch = next(path for path in original.rglob("patch.diff"))
    patch.write_bytes(patch.read_bytes() + b"changed")
    root = tmp_path / "operation"
    root.mkdir()
    store = LearningStore(root, feedback)
    approval = approval_for(learning_feedback, store.root)
    sent: list[dict] = []
    with pytest.raises(ValueError):
        store.invoke("extract", approval=approval, adapter=adapter_for(approval, [response()], sent))
    assert not sent and inventory(store.root) == {}


def test_original_host_approval_and_self_consistent_request_drift_are_rejected(learning_feedback, tmp_path: Path) -> None:
    feedback, _, _, _ = learning_feedback
    store = LearningStore(tmp_path, feedback)
    approval = approval_for(learning_feedback, store.root)
    sent: list[dict] = []
    adapter = adapter_for(approval, [response()], sent)
    store.invoke("extract", approval=approval, adapter=adapter)
    original = inventory(store.root)
    changed = approval.model_copy(update={"approval_id": "other-host"})
    with pytest.raises(ValueError):
        store.invoke("extract", approval=changed, adapter=adapter)
    assert inventory(store.root) == original and len(sent) == 1
    path = store.root / "requests/extract.json"
    value = json.loads(path.read_bytes())
    body = json.loads(value["prepared"]["body"])
    body["messages"][0]["content"] = "changed instructions"
    value["prepared"]["body"] = canonical(body)
    value["prepared"]["body_sha256"] = sha256(canonical(body))
    value["intent"]["prompt_sha256"] = sha256(canonical(body["messages"]))
    path.write_text(canonical(value), encoding="utf-8")
    before = inventory(store.root)
    with pytest.raises(ValueError):
        store.invoke("extract", approval=approval, adapter=adapter)
    assert inventory(store.root) == before and len(sent) == 1


def test_transport_change_in_preflight_stays_unsent_reserved(learning_feedback, tmp_path: Path) -> None:
    feedback, _, _, _ = learning_feedback
    store = LearningStore(tmp_path, feedback)
    approval = approval_for(learning_feedback, store.root)
    sent: list[dict] = []
    adapter = adapter_for(approval, [response()], sent)
    saved = adapter._transport

    def change() -> None:
        adapter._transport = object()  # type: ignore[assignment]

    with pytest.raises(AdapterRequestError):
        store.invoke("extract", approval=approval, adapter=adapter, preflight=change)
    adapter._transport = saved
    record = ModelCallStore(store.root / "model-calls.sqlite", read_only=True).get("extract")
    assert record is not None and record.state == "reserved" and not sent
    store.invoke("extract", approval=approval, adapter=adapter)
    assert len(sent) == 1


def test_insufficient_response_capacity_refuses_before_mutable_ledger(learning_feedback, tmp_path: Path) -> None:
    feedback, _, _, _ = learning_feedback
    for field in ("max_total_bytes", "max_file_bytes"):
        root = tmp_path / field
        root.mkdir()
        limits = FeedbackLimits.model_validate({**FeedbackLimits().model_dump(), field: 1000})
        store = LearningStore(root, feedback, limits=limits)
        approval = approval_for(learning_feedback, store.root)
        sent: list[dict] = []
        with pytest.raises(ValueError, match="headroom"):
            store.invoke("extract", approval=approval, adapter=adapter_for(approval, [response()], sent))
        assert not sent and inventory(store.root) == {}


def test_identical_external_owner_cannot_run_in_another_physical_root(learning_feedback, tmp_path: Path) -> None:
    feedback, _, _, _ = learning_feedback
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()
    one, two = LearningStore(first, feedback), LearningStore(second, feedback)
    approval = approval_for(learning_feedback, one.root, max_calls=1)
    sent: list[dict] = []
    adapter = adapter_for(approval, [response()], sent)
    one.invoke("extract", approval=approval, adapter=adapter)
    with pytest.raises(ValueError, match="physical root"):
        two.invoke("extract", approval=approval, adapter=adapter)
    assert len(sent) == 1 and inventory(two.root) == {}


@pytest.mark.parametrize("state", ["completed", "unknown"])
@pytest.mark.parametrize("damage", ["missing_database", "deleted_row"])
def test_lost_original_ledger_cannot_reset_budget_or_resend(learning_feedback, tmp_path: Path, state: str, damage: str) -> None:
    feedback, _, _, _ = learning_feedback
    store = LearningStore(tmp_path, feedback)
    approval = approval_for(learning_feedback, store.root)
    sent: list[dict] = []

    def timeout(_: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("synthetic")

    adapter = adapter_for(approval, [response()], sent, callback=timeout if state == "unknown" else None)
    if state == "unknown":
        with pytest.raises(RuntimeError):
            store.invoke("extract", approval=approval, adapter=adapter)
    else:
        store.invoke("extract", approval=approval, adapter=adapter)
    path = store.root / "model-calls.sqlite"
    if damage == "missing_database":
        path.unlink()
    else:
        with closing(sqlite3.connect(path)) as connection, connection:
            connection.execute("DELETE FROM model_calls WHERE call_id='extract'")
    before = inventory(store.root)
    with pytest.raises(ValueError):
        store.summary(approval=approval)
    with pytest.raises(ValueError):
        store.invoke("extract", approval=approval, adapter=adapter)
    assert inventory(store.root) == before and len(sent) == 1


def test_unknown_and_future_call_costs_do_not_turn_a_frozen_prefix_into_free_learning() -> None:
    # A pure prefix semantics test; these constructed records are not accepted
    # stopped-run evidence, provider receipts or an exercised post-Draft append.
    intent = ModelCallIntentV2(call_id="first", task_id="op", run_id="r", kind="experience_extraction",
        provider_id="mock", model_id="m", prompt_sha256="a" * 64, tool_schema_sha256="b" * 64,
        parameters_sha256="c" * 64, tool_names=(), source_binding_sha256="d" * 64, learning_owner_sha256="e" * 64)
    unknown = ModelCallRecord(intent=intent, state="unknown", observation=None, uncertainty_reason="timeout")
    prefix = [unknown]
    original = _cost(prefix)
    future = ModelCallRecord(intent=intent.model_copy(update={"call_id": "later"}), state="reserved", observation=None,
        uncertainty_reason=None)
    all_records = prefix + [future]
    assert _cost(all_records).reserved_calls == 2
    assert _cost(all_records[:1]) == original and original.reserved_calls == 1
    assert original.total_tokens is None and original.monetary_cost is None
