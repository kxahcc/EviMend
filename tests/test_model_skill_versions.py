"""Model inference remains model inference after exact host review and copying."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from test_knowledge_retrieval import guidance
from test_learning_source import learned

from dfx_agent.evidence_io import local_evidence_path
from dfx_agent.feedback import _ClosedTree
from dfx_agent.feedback_contracts import FeedbackLimits
from dfx_agent.knowledge_contracts import SolverProjection, content_hash
from dfx_agent.learning import LearningStore
from dfx_agent.learning_contracts import LearningDraftPayload
from dfx_agent.learning_source import LearningSourceStore
from dfx_agent.skill_evolution_contracts import (
    HostModelSkillVersionApproval,
    HostSkillVersionApproval,
    ModelVersionPayload,
    parse_version,
    parse_version_approval,
    parse_version_payload,
)
from dfx_agent.skill_validation import SkillValidationStore

pytest_plugins = ("learning_workflow_support",)


@pytest.fixture(scope="module")
def model_version(learning_feedback, tmp_path_factory: pytest.TempPathFactory):
    root = local_evidence_path(tmp_path_factory.mktemp("mv"))
    learning, learning_approval, draft, sent, source_store = learned(learning_feedback, root / "learning")
    source = source_store.capture(learning, draft.draft_id, approval=learning_approval)
    version_root = root / "version-store"
    version_root.mkdir()
    store = SkillValidationStore(version_root)
    projection = SolverProjection(guidance=guidance("width"), derivation_authority="host_curated", validation_scopes=())
    seed_approval = HostSkillVersionApproval(approval_id="host-seed", skill_id="capacity",
        projection_sha256=content_hash(projection), allowed_splits=("development", "candidate_validation", "confirmation"),
        learning_lineage_groups=("seed-lineage",))
    seed = store.create_seed(projection, approval=seed_approval)
    draft_payload = LearningDraftPayload.model_validate_json(draft.payload_json)
    approval = HostModelSkillVersionApproval(approval_id="review-exact-draft", skill_id="capacity",
        projection_sha256=content_hash(draft_payload.projection), parent_version_id=seed.version_id,
        source_draft_id=draft.draft_id, learning_source_id=source.source_id, allowed_splits=draft_payload.allowed_splits,
        excluded_lineage_groups=draft_payload.excluded_lineage_groups)
    version = store.create_model_revision(seed.version_id, source_store, draft.draft_id, approval=approval,
        parent_approval=seed_approval, learning_approval=learning_approval)
    return root, store, seed, seed_approval, version, approval, learning, learning_approval, source_store, draft, sent


def test_reviewed_model_version_retains_complete_originals_and_cost(model_version, monkeypatch: pytest.MonkeyPatch):
    _root, store, seed, seed_approval, version, approval, learning, learning_approval, source_store, draft, sent = model_version
    payload = parse_version_payload(version.payload_json)
    assert isinstance(payload, ModelVersionPayload)
    assert payload.projection.derivation_authority == payload.derivation_authority == "model_inference"
    assert payload.qualification == "unvalidated" and payload.application_qualification == "none"
    assert payload.draft_cost.reserved_calls == payload.operation_cost.reserved_calls == 2
    assert payload.operation_cost.total_tokens == 30 and payload.operation_cost.monetary_cost is None
    assert payload.learning_lineage_groups == tuple(sorted(set(payload.learning_lineage_groups)))
    assert "seed-lineage" in payload.learning_lineage_groups
    learning.root.rename(learning.root.with_name("operation-moved"))
    learning.feedback.root.rename(learning.feedback.root.with_name("feedback-moved"))
    source_store.root.rename(source_store.root.with_name("portable-moved"))
    monkeypatch.setattr(LearningStore, "invoke", lambda *_a, **_kw: pytest.fail("read cannot invoke"))
    assert store.read_version(version.version_id, approval=approval) == version
    assert store.read_version(seed.version_id, approval=seed_approval) == seed
    source = LearningSourceStore(store.root).read_source(approval.learning_source_id, approval=learning_approval)
    assert source.contents()["draft"]["draft_id"] == draft.draft_id and len(sent) == 2
    costs = store.version_learning_costs(version.version_id, approval=approval)
    assert costs["status"] == "observed" and len(costs["operations"]) == 1
    assert costs["cost"]["total_tokens"] == 30 and costs["cost"]["reserved_calls"] == 2


@pytest.mark.parametrize("change", ["review", "split", "parent", "source"])
def test_complete_original_approval_is_required(model_version, change: str):
    _root, store, _seed, _seed_approval, version, approval, *_rest = model_version
    updates = {"review": {"approval_id": "other-review"}, "split": {"allowed_splits": ("development",)},
               "parent": {"parent_version_id": "skill-version-" + "a" * 64},
               "source": {"learning_source_id": "learning-source-" + "b" * 64}}[change]
    with pytest.raises(ValueError):
        store.read_version(version.version_id, approval=approval.model_copy(update=updates))


@pytest.mark.parametrize("schema", [True, 1, 3])
def test_model_records_cannot_be_relabelled_as_manual_or_unknown(model_version, schema):
    _root, _store, _seed, _seed_approval, version, approval, *_rest = model_version
    for value, parser in ((version.model_dump(mode="json"), parse_version),
                          (approval.model_dump(mode="json"), parse_version_approval)):
        value["schema_version"] = schema
        with pytest.raises(ValueError):
            parser(value)


def test_bundle_keeps_empty_original_directories_and_missing_source_fails(model_version, tmp_path: Path):
    _root, store, _seed, _seed_approval, version, approval, *_rest = model_version
    files, directories = store.export_version_bundle(version.version_id, approval=approval)
    assert any(name.endswith("/operation/.pending") for name in directories)
    copied = local_evidence_path(tmp_path / "copy")
    copied.mkdir()
    for directory in directories:
        (copied / directory).mkdir(parents=True, exist_ok=True)
    for name, raw in files.items():
        (copied / name).parent.mkdir(parents=True, exist_ok=True)
        (copied / name).write_bytes(raw)
    reader = SkillValidationStore(copied)
    assert reader.read_version(version.version_id, approval=approval) == version
    owner = next(copied.rglob("operation/owner.json"))
    owner.unlink()
    before = {path.relative_to(copied).as_posix(): path.read_bytes() for path in copied.rglob("*") if path.is_file()}
    with pytest.raises(ValueError):
        reader.read_version(version.version_id, approval=approval)
    assert before == {path.relative_to(copied).as_posix(): path.read_bytes() for path in copied.rglob("*") if path.is_file()}


def test_capacity_rejects_before_new_version_namespace(model_version, tmp_path: Path):
    _root, store, seed, seed_approval, _version, approval, _learning, learning_approval, _source_store, draft, _sent = model_version
    destination = local_evidence_path(tmp_path / "bounded")
    destination.mkdir()
    (destination / "versions").mkdir()
    shutil.copyfile(store.root / "versions" / (seed.version_id + ".json"), destination / "versions" / (seed.version_id + ".json"))
    reader = SkillValidationStore(destination, limits=FeedbackLimits(max_total_bytes=1))
    before = sorted(path.relative_to(destination).as_posix() for path in destination.rglob("*"))
    with pytest.raises(ValueError):
        reader.create_model_revision(seed.version_id, LearningSourceStore(store.root), draft.draft_id, approval=approval,
            parent_approval=seed_approval, learning_approval=learning_approval)
    assert sorted(path.relative_to(destination).as_posix() for path in destination.rglob("*")) == before


def test_export_rejects_bad_capture_restored_before_final_authentication(model_version, monkeypatch: pytest.MonkeyPatch):
    _root, store, _seed, _seed_approval, version, approval, *_rest = model_version
    source = store.root / "source-learning" / approval.learning_source_id
    owner = next(source.rglob("operation/owner.json"))
    original = owner.read_bytes()
    original_read, original_scan = store._read_version, _ClosedTree.scan
    ready = captured = False

    def read(identifier, approved, budget, seen):
        nonlocal ready
        if identifier == version.version_id and captured:
            owner.write_bytes(original)
        result = original_read(identifier, approved, budget, seen)
        if identifier == version.version_id:
            ready = True
        return result

    def scan(tree, roots, **options):
        nonlocal captured
        if ready and not captured and tree.root == local_evidence_path(source):
            owner.write_bytes(original + b" ")
            captured = True
        return original_scan(tree, roots, **options)

    monkeypatch.setattr(store, "_read_version", read)
    monkeypatch.setattr(_ClosedTree, "scan", scan)
    try:
        with pytest.raises(ValueError, match="original (size|bytes|changed)"):
            store.export_version_bundle(version.version_id, approval=approval)
        assert ready and captured
    finally:
        owner.write_bytes(original)
