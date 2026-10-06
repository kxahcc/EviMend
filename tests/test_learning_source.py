"""Portable originals authenticate full learning operations without dispatch."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from learning_workflow_support import adapter_for, approval_for, inventory, response

from dfx_agent.evidence_io import local_evidence_path
from dfx_agent.feedback import FeedbackStore, _ReadBudget
from dfx_agent.feedback_contracts import FeedbackLimits, content_hash, encode
from dfx_agent.learning import LearningStore
from dfx_agent.learning_contracts import (
    LearningDraft,
    LearningDraftPayload,
    LearningOwner,
    LearningRequest,
    LearningSource,
    LearningSourcePayload,
)
from dfx_agent.learning_source import LearningSourceStore
from dfx_agent.model_calls import ModelCallStore
from dfx_agent.task import digest

pytest_plugins = ("learning_workflow_support",)


def learned(captured, root: Path, *, missing: bool = False):
    root = local_evidence_path(root)
    feedback, capsule, feedback_approval, seed = captured
    shutil.copytree(feedback.root, root / "feedback")
    original = FeedbackStore(root / "feedback")
    (root / "operation").mkdir()
    (root / "destination").mkdir()
    learning = LearningStore(root / "operation", original)
    approval = approval_for((original, capsule, feedback_approval, seed), learning.root)
    sent: list[dict] = []
    adapter = adapter_for(approval, [response("invalid first response"), response(usage=not missing)], sent)
    learning.invoke("extract", approval=approval, adapter=adapter)
    learning.invoke("repair", approval=approval, adapter=adapter, repair_of_call_id="extract")
    draft = learning.propose_draft("repair", approval=approval)
    return learning, approval, draft, sent, LearningSourceStore(root / "destination")


@pytest.mark.parametrize("missing", [False, True])
def test_portable_source_survives_original_removal_preserves_calls_cost_and_authority(
        learning_feedback, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, missing: bool) -> None:
    learning, approval, draft, sent, store = learned(learning_feedback, tmp_path, missing=missing)
    source = store.capture(learning, draft.draft_id, approval=approval)
    payload = LearningSourcePayload.model_validate_json(source.payload_json)
    assert payload.owner.approval == approval and payload.draft == draft
    assert payload.operation_cost.reserved_calls == payload.operation_cost.completed_calls == 2
    assert payload.operation_cost.known_lower_bound == (15 if missing else 30)
    assert payload.operation_cost.total_tokens == (None if missing else 30)
    assert payload.operation_cost.monetary_cost is None
    assert "invalid first response" in payload.operation_calls_json
    assert LearningDraftPayload.model_validate_json(draft.payload_json).projection.derivation_authority == "model_inference"
    learning.root.rename(tmp_path / "original-operation-moved")
    learning.feedback.root.rename(tmp_path / "original-feedback-moved")
    before = inventory(store.root)

    def forbid(*_args, **_kwargs):
        raise AssertionError("portable reads cannot invoke")

    monkeypatch.setattr(LearningStore, "invoke", forbid)
    assert store.read_source(source.source_id, approval=approval) == source
    assert store.read_draft(source.source_id, approval=approval) == draft
    assert inventory(store.root) == before and len(sent) == 2
    assert not hasattr(store, "invoke") and not hasattr(store, "reserve")
    changed = approval.model_copy(update={"root_binding_sha256": "a" * 64})
    with pytest.raises(ValueError):
        store.read_source(source.source_id, approval=changed)
    assert inventory(store.root) == before


@pytest.mark.parametrize("relative", ["operation/model-calls.sqlite", "operation/requests/repair.json", "operation/owner.json"])
def test_portable_missing_original_is_rejected_without_repair_or_resend(
        learning_feedback, tmp_path: Path, relative: str) -> None:
    learning, approval, draft, sent, store = learned(learning_feedback, tmp_path)
    source = store.capture(learning, draft.draft_id, approval=approval)
    path = store.root / "source-learning" / source.source_id / "payload" / relative
    path.unlink()
    before = inventory(store.root)
    with pytest.raises(ValueError):
        store.read_source(source.source_id, approval=approval)
    assert inventory(store.root) == before and len(sent) == 2


def test_capacity_and_approval_fail_before_destination_writes(learning_feedback, tmp_path: Path) -> None:
    learning, approval, draft, sent, store = learned(learning_feedback, tmp_path)
    constrained = LearningSourceStore(store.root, limits=FeedbackLimits(max_total_bytes=1))
    before = inventory(store.root)
    with pytest.raises(ValueError):
        constrained.capture(learning, draft.draft_id, approval=approval)
    assert inventory(store.root) == before and not list(store.root.iterdir())
    with pytest.raises(ValueError):
        store.capture(learning, draft.draft_id, approval=approval.model_copy(update={"max_calls": 1}))
    assert inventory(store.root) == before and len(sent) == 2


def test_copied_request_drift_and_unknown_schema_never_reauthorize(learning_feedback, tmp_path: Path) -> None:
    learning, approval, draft, sent, store = learned(learning_feedback, tmp_path)
    source = store.capture(learning, draft.draft_id, approval=approval)
    directory = store.root / "source-learning" / source.source_id
    path = directory / "payload" / "operation" / "requests" / "repair.json"
    path.write_bytes(path.read_bytes().replace(b"invalid first response", b"different response"))
    before = inventory(store.root)
    with pytest.raises(ValueError):
        store.read_source(source.source_id, approval=approval)
    assert inventory(store.root) == before and len(sent) == 2
    for schema in (True, 1, 3):
        with pytest.raises(ValueError):
            type(source).model_validate_json(source.model_dump_json().replace('"schema_version":2',
                '"schema_version":' + ("true" if schema is True else str(schema)), 1))


@pytest.mark.parametrize("change", ["input", "provider"])
def test_rehashed_outer_metadata_cannot_authorize_different_original_inputs(
        learning_feedback, tmp_path: Path, change: str) -> None:
    learning, approval, draft, sent, store = learned(learning_feedback, tmp_path)
    source = store.capture(learning, draft.draft_id, approval=approval)
    original = LearningSourcePayload.model_validate_json(source.payload_json)
    owner_raw = json.loads(encode(original.owner))
    if change == "input":
        text = owner_raw["input_texts"][0]["text"] + "\nforged extra input"
        owner_raw["input_texts"][0].update(text=text, bytes=len(text.encode()), sha256=digest(text.encode()))
    else:
        profile = json.loads(owner_raw["provider_profile_json"])
        profile["model_id"] = "unapproved-model"
        owner_raw["provider_profile_json"] = encode(profile)
    owner = LearningOwner.model_validate_json(encode(owner_raw))
    draft_body = LearningDraftPayload.model_validate_json(draft.payload_json).model_copy(update={"owner": owner})
    draft_hash = content_hash(draft_body)
    changed_draft = LearningDraft(draft_id="learning-draft-" + draft_hash,
        content_sha256=draft_hash, payload_json=encode(draft_body))
    body = original.model_copy(update={"owner": owner, "draft": changed_draft})
    key = content_hash(body)
    changed_source = LearningSource(source_id="learning-source-" + key,
        content_sha256=key, payload_json=encode(body))
    directory = store.root / "source-learning" / source.source_id
    (directory / "source.json").write_text(encode(changed_source), encoding="utf-8")
    before = inventory(store.root)
    # Use the new valid outer ID; failure must be inner reconstruction, rather
    # than the old source filename/hash or inventory checks.
    with pytest.raises(ValueError, match="portable learning (owner|provider)"):
        store._read_directory(directory, changed_source.source_id, approval,
                              _ReadBudget(store.limits.max_capture_read_bytes))
    assert inventory(store.root) == before and len(sent) == 2


def test_complete_duplicate_prepared_history_is_rejected_without_copy_or_send(
        learning_feedback, tmp_path: Path) -> None:
    learning, approval, draft, sent, store = learned(learning_feedback, tmp_path)
    request = LearningRequest.model_validate_json((learning.root / "requests/extract.json").read_bytes())
    duplicate = request.model_copy(update={"intent": request.intent.model_copy(update={"call_id": "duplicate-extract"})})
    ledger = ModelCallStore(learning.root / "model-calls.sqlite")
    response_message = ledger.replay(request.intent)[0]
    ledger.reserve(duplicate.intent, max_calls=approval.max_calls, max_known_tokens=approval.max_known_tokens)
    ledger.dispatch(duplicate.intent.call_id)
    ledger.complete(duplicate.intent, response_message)
    (learning.root / "requests/duplicate-extract.json").write_text(encode(duplicate), encoding="utf-8")
    before = inventory(store.root)
    with pytest.raises(ValueError, match="duplicate a complete prepared request"):
        store.capture(learning, draft.draft_id, approval=approval)
    assert inventory(store.root) == before and len(sent) == 2
