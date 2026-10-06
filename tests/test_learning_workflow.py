"""Metered text learning retains genuine repair process and never self-approves."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from learning_workflow_support import adapter_for, approval_for, inventory, response

from dfx_agent.learning import LearningStore
from dfx_agent.learning_contracts import LearningDraftPayload
from dfx_agent.model_calls import (
    ModelCallBudget,
    ModelCallConflict,
    ModelCallStore,
    ModelCallUncertain,
)

pytest_plugins = ("learning_workflow_support",)


def test_wire_includes_crash_both_failed_successful_patches_and_complete_source(learning_feedback, tmp_path: Path) -> None:
    feedback, capsule, _, seed = learning_feedback
    store = LearningStore(tmp_path, feedback)
    approval = approval_for(learning_feedback, store.root)
    sent: list[dict] = []
    adapter = adapter_for(approval, [response()], sent)
    message, observation = store.invoke("extract", approval=approval, adapter=adapter)
    assert observation.usage is not None and observation.usage.total_tokens == 15
    assert len(sent) == 1 and "tools" not in sent[0]
    data = json.loads(sent[0]["messages"][1]["content"])
    texts = data["input_texts"]
    assert texts[0]["text"] == seed.task.joinpath("crash.log").read_text(encoding="utf-8")
    assert len(texts) == 3 and [text["role"] for text in texts] == ["original_crash", "registered_candidate_patch", "registered_candidate_patch"]
    assert "+value=almost" in texts[1]["text"] and "+value=good" in texts[2]["text"]
    assert data["aggregate"] == json.loads(capsule.contents()["aggregate_json"])
    assert data["permission"] == approval.feedback_approval.model_dump(mode="json")
    assert data["learning_lineage_groups"] == ["synthetic-lineage"]
    assert all(not item["path"].endswith((".sqlite", "session.json")) for item in texts)
    draft = store.propose_draft("extract", approval=approval)
    payload = LearningDraftPayload.model_validate_json(draft.payload_json)
    assert payload.projection.derivation_authority == "model_inference" and not payload.projection.validation_scopes
    assert payload.status == "pending_host_review" and payload.application_qualification == "none"
    assert payload.parent_skill_version_id is None
    assert payload.allowed_splits == approval.feedback_approval.allowed_splits
    assert payload.cost.reserved_calls == 1 and payload.cost.total_tokens == 15 and payload.cost.monetary_cost is None
    original = inventory(store.root)
    assert store.read_draft(draft.draft_id, approval=approval) == draft
    replay, replay_observation = store.invoke("extract", approval=approval, adapter=adapter)
    assert replay == message and replay_observation == observation and len(sent) == 1
    assert inventory(store.root) == original
    with pytest.raises(ModelCallConflict):
        store.invoke("other-id", approval=approval, adapter=adapter)
    assert inventory(store.root) == original


def test_format_repair_is_explicit_has_original_response_and_all_calls_cost(learning_feedback, tmp_path: Path) -> None:
    feedback, _, _, _ = learning_feedback
    first = tmp_path / "first"
    first.mkdir()
    store = LearningStore(first, feedback)
    approval = approval_for(learning_feedback, store.root)
    sent: list[dict] = []
    adapter = adapter_for(approval, [response("not a JSON procedure"), response()], sent)
    store.invoke("extract", approval=approval, adapter=adapter)
    with pytest.raises(ValueError):
        store.propose_draft("extract", approval=approval)
    assert len(sent) == 1
    store.invoke("repair", approval=approval, adapter=adapter, repair_of_call_id="extract")
    parent = json.loads(sent[1]["messages"][2]["content"])
    assert parent["parent_call_id"] == "extract" and parent["response"]["content"] == "not a JSON procedure"
    assert sent[0]["messages"][1] == sent[1]["messages"][1]
    draft = store.propose_draft("repair", approval=approval)
    payload = LearningDraftPayload.model_validate_json(draft.payload_json)
    calls = json.loads(payload.calls_json)["calls"]
    assert [call["record"]["intent"]["kind"] for call in calls] == ["experience_extraction", "learning_format_repair"]
    assert [call["learning_content_status"] for call in calls] == ["invalid_experience_content", "valid_experience_content"]
    assert payload.cost.total_tokens == 30 and payload.cost.reserved_calls == 2
    with pytest.raises(ValueError):
        store.invoke("invalid-repair", approval=approval, adapter=adapter, repair_of_call_id="repair")
    assert len(sent) == 2


def test_preflight_failure_is_unsent_reserved_resume_uses_same_call(learning_feedback, tmp_path: Path) -> None:
    feedback, _, _, _ = learning_feedback
    store = LearningStore(tmp_path, feedback)
    approval = approval_for(learning_feedback, store.root)
    sent: list[dict] = []
    adapter = adapter_for(approval, [response()], sent)

    def fail() -> None:
        raise RuntimeError("synthetic preflight failure")

    with pytest.raises(RuntimeError, match="synthetic preflight"):
        store.invoke("extract", approval=approval, adapter=adapter, preflight=fail)
    ledger = ModelCallStore(store.root / "model-calls.sqlite", read_only=True)
    record = ledger.get("extract")
    assert record is not None and record.state == "reserved" and not sent
    cost = store.summary(approval=approval)
    assert cost.undispatched_calls == 1 and cost.uncertain_calls == 0 and cost.total_tokens == 0
    store.invoke("extract", approval=approval, adapter=adapter)
    assert len(sent) == 1 and store.summary(approval=approval).reserved_calls == 1


def test_sent_failure_and_missing_usage_remain_unknown_without_blind_retry(learning_feedback, tmp_path: Path) -> None:
    feedback, _, _, _ = learning_feedback
    first = tmp_path / "first"
    first.mkdir()
    store = LearningStore(first, feedback)
    approval = approval_for(learning_feedback, store.root)
    sent: list[dict] = []

    def timeout(_: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("synthetic")

    adapter = adapter_for(approval, [], sent, callback=timeout)
    with pytest.raises(RuntimeError):
        store.invoke("extract", approval=approval, adapter=adapter)
    with pytest.raises(ModelCallUncertain):
        store.invoke("extract", approval=approval, adapter=adapter)
    with pytest.raises(ModelCallUncertain):
        store.invoke("fresh-id", approval=approval, adapter=adapter)
    cost = store.summary(approval=approval)
    assert len(sent) == 1 and cost.reserved_calls == cost.uncertain_calls == 1
    assert cost.known_lower_bound == 0 and cost.total_tokens is None and cost.monetary_cost is None
    another = tmp_path / "another"
    another.mkdir()
    separate = LearningStore(another, feedback)
    missing_approval = approval_for(learning_feedback, separate.root, operation_id="missing-usage-op", run_id="missing-usage-run")
    missing_sent: list[dict] = []
    separate.invoke("extract", approval=missing_approval, adapter=adapter_for(missing_approval, [response(usage=False)], missing_sent))
    draft = LearningDraftPayload.model_validate_json(separate.propose_draft("extract", approval=missing_approval).payload_json)
    assert draft.cost.missing_usage_calls == 1 and draft.cost.total_tokens is None


def test_owner_shared_call_and_known_token_budgets_include_format_repair(learning_feedback, tmp_path: Path) -> None:
    feedback, _, _, _ = learning_feedback
    first = tmp_path / "first"
    first.mkdir()
    store = LearningStore(first, feedback)
    approval = approval_for(learning_feedback, store.root, max_calls=1)
    sent: list[dict] = []
    adapter = adapter_for(approval, [response("bad"), response()], sent)
    store.invoke("extract", approval=approval, adapter=adapter)
    with pytest.raises(ModelCallBudget):
        store.invoke("repair", approval=approval, adapter=adapter, repair_of_call_id="extract")
    assert len(sent) == 1 and store.summary(approval=approval).reserved_calls == 1
    another = tmp_path / "token-operation"
    another.mkdir()
    token_store = LearningStore(another, feedback)
    token_approval = approval_for(learning_feedback, token_store.root, operation_id="token-op", run_id="token-run", max_known_tokens=15)
    token_sent: list[dict] = []
    token_adapter = adapter_for(token_approval, [response("bad"), response()], token_sent)
    token_store.invoke("extract", approval=token_approval, adapter=token_adapter)
    with pytest.raises(ModelCallBudget):
        token_store.invoke("repair", approval=token_approval, adapter=token_adapter, repair_of_call_id="extract")
    assert len(token_sent) == 1 and token_store.summary(approval=token_approval).known_lower_bound == 15
