"""Skill immutable projection, source permissions and strict synthetic contracts."""

from __future__ import annotations

import copy
import os
import shutil
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from test_knowledge_contracts import guidance
from test_skill_validation_integration import plan_approval, run_role, setup, spec

import dfx_agent.skill_validation as implementation
from dfx_agent.evidence_io import local_evidence_path
from dfx_agent.feedback import FeedbackStore, _ReadBudget
from dfx_agent.feedback_contracts import FeedbackLimits, content_hash, encode
from dfx_agent.knowledge import KnowledgeStore
from dfx_agent.knowledge_contracts import LimitedValidationScope, SolverProjection
from dfx_agent.knowledge_retrieval import KnowledgeRetriever
from dfx_agent.model_calls import canonical, sha256
from dfx_agent.skill_evolution_contracts import (
    HostSkillVersionApproval,
    HostValidationPlanApproval,
    SkillVersion,
    VersionPayload,
)
from dfx_agent.skill_validation import SkillValidationStore, _cross_role_retrieved


def tree(root: Path) -> dict[str, bytes]:
    root = local_evidence_path(root)
    return {path.relative_to(root).as_posix(): path.read_bytes() for path in root.rglob("*") if path.is_file()}


def projection() -> SolverProjection:
    return SolverProjection(guidance=guidance(), derivation_authority="host_curated")


def seed_approval(value: SolverProjection | None = None, **changes: Any) -> HostSkillVersionApproval:
    projected = value if value is not None else projection()
    return HostSkillVersionApproval(approval_id="seed-host", skill_id="view-capacity",
        projection_sha256=sha256(canonical(projected.model_dump(mode="json"))),
        allowed_splits=("candidate_validation", "confirmation"), **changes)


@pytest.mark.parametrize("schema", [True, 1.0, "1", 2])
def test_skill_approval_schema_is_an_exact_integer(schema: Any) -> None:
    with pytest.raises(ValueError):
        seed_approval(schema_version=schema)


@pytest.mark.parametrize("permission", [True, 0, 1, "false", None])
def test_validation_approval_never_has_learning_permission(permission: Any) -> None:
    approved = seed_approval()
    with pytest.raises(ValueError):
        HostValidationPlanApproval(approval_id="host", spec_sha256="a" * 64, allow_learning=permission,
                                  old_version_approval=approved, new_version_approval=approved)


def test_skill_constructor_and_missing_reads_create_nothing(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        SkillValidationStore(tmp_path / "missing")
    assert not (tmp_path / "missing").exists()
    store = SkillValidationStore(tmp_path)
    with pytest.raises(ValueError):
        store.read_version("skill-version-" + "a" * 64, approval=seed_approval())
    assert not list(tmp_path.iterdir())


def test_seed_is_complete_immutable_and_unvalidated(tmp_path: Path) -> None:
    store = SkillValidationStore(tmp_path)
    value, approved = projection(), seed_approval(learning_lineage_groups=("seed-family",))
    version = store.create_seed(value, approval=approved)
    body = version.contents()
    assert body["projection"] == value.model_dump(mode="json")
    assert body["qualification"] == "unvalidated" and body["application_qualification"] == "none"
    assert body["learning_tokens"] is None and body["learning_model_call_status"] == "not_run"
    assert body["learning_lineage_groups"] == ["seed-family"]
    before = tree(store.root)
    assert store.read_version(version.version_id, approval=approved) == version
    assert store.create_seed(value, approval=approved) == version
    assert tree(store.root) == before
    body["projection"]["guidance"]["trigger"] = "Modified returned copy."
    assert version.contents()["projection"]["guidance"]["trigger"] != body["projection"]["guidance"]["trigger"]
    assert content_hash(version.contents()) == version.content_sha256


@pytest.mark.parametrize("change", ["inference", "validation", "hash", "revision"])
def test_seed_requires_exact_explicit_host_projection(tmp_path: Path, change: str) -> None:
    value = projection()
    if change == "inference":
        value = value.model_copy(update={"derivation_authority": "model_inference"})
    elif change == "validation":
        value = value.model_copy(update={"validation_scopes": (LimitedValidationScope(level="synthetic_fixture",
            result="passed", scope_names=("generalized-scope",)),)})
    approved = seed_approval(value)
    if change == "hash":
        approved = approved.model_copy(update={"projection_sha256": "b" * 64})
    elif change == "revision":
        approved = approved.model_copy(update={"parent_version_id": "skill-version-" + "b" * 64,
                                               "source_candidate_id": "experience-candidate-" + "b" * 64})
    store = SkillValidationStore(tmp_path)
    with pytest.raises(ValueError):
        store.create_seed(value, approval=approved)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("bound", ["file", "total", "reads"])
def test_seed_respects_publication_and_shared_read_limits(tmp_path: Path, bound: str) -> None:
    key = {"file": "max_file_bytes", "total": "max_total_bytes", "reads": "max_capture_read_bytes"}[bound]
    store = SkillValidationStore(tmp_path, limits=FeedbackLimits(**{key: 1}))
    with pytest.raises(ValueError):
        store.create_seed(projection(), approval=seed_approval())
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("change", ["permission", "learning_lineage", "host", "schema_bypass"])
def test_read_requires_external_original_approval(tmp_path: Path, change: str) -> None:
    store = SkillValidationStore(tmp_path)
    original = seed_approval()
    version = store.create_seed(projection(), approval=original)
    replacement = {"permission": {"allowed_splits": ("candidate_validation",)},
        "learning_lineage": {"learning_lineage_groups": ("new-family",)}, "host": {"approval_id": "other-host"},
        "schema_bypass": {"schema_version": True}}[change]
    altered = original.model_copy(update=replacement)
    before = tree(store.root)
    with pytest.raises(ValueError):
        store.read_version(version.version_id, approval=altered)
    assert tree(store.root) == before


@pytest.mark.parametrize("change", ["raw", "shared", "rehash", "qualification"])
def test_seed_record_tamper_cannot_be_repaired_on_read(tmp_path: Path, change: str) -> None:
    store = SkillValidationStore(tmp_path)
    approved = seed_approval()
    version = store.create_seed(projection(), approval=approved)
    path = local_evidence_path(store.root / "versions" / (version.version_id + ".json"))
    if change == "shared":
        os.link(path, tmp_path / "shared")
    elif change == "raw":
        path.write_bytes(path.read_bytes() + b" ")
    else:
        record = copy.deepcopy(version.model_dump(mode="json"))
        body = version.contents()
        if change == "qualification":
            body["application_qualification"] = "eligible"
        else:
            body["learning_lineage_groups"] = ["hidden-family"]
        record["payload_json"] = canonical(body)
        path.write_text(canonical(record), encoding="utf-8")
    before = tree(store.root)
    with pytest.raises(ValueError):
        store.read_version(version.version_id, approval=approved)
    assert tree(store.root) == before


def test_revision_approval_needs_both_parent_and_candidate() -> None:
    with pytest.raises(ValueError):
        seed_approval(parent_version_id="skill-version-" + "a" * 64)
    with pytest.raises(ValueError):
        seed_approval(source_candidate_id="experience-candidate-" + "a" * 64)


def test_skill_contract_does_not_accept_an_untracked_projection_change(tmp_path: Path) -> None:
    version = SkillValidationStore(tmp_path).create_seed(projection(), approval=seed_approval())
    altered = version.model_dump(mode="json")
    body = version.contents()
    body["projection"]["guidance"]["preserves"] = ["Discard intended encoding."]
    altered["payload_json"] = canonical(body)
    with pytest.raises(ValueError):
        SkillVersion.model_validate_json(canonical(altered))
    assert encode(version) == canonical(version.model_dump(mode="json"))


@pytest.mark.parametrize("mode, expected", [
    ("unread_orphan", False), ("successful_search", True), ("entry_read", True),
    ("full_page_read", True), ("other_entry_read", False), ("unrelated_page", False),
])
def test_cross_role_detection_counts_complete_reads_without_search_actions(mode: str, expected: bool) -> None:
    query = {"bound_query": {"record_id": "query"}, "actions": [],
             "retrieval": {"hits": [{"entry_id": "other"}, {"entry_id": "target"}]}}
    facts = {"queries": [query], "reads": []}
    if mode == "successful_search":
        query["actions"] = [{"action_id": "search"}]
    elif mode != "unread_orphan":
        facts["reads"] = [{"query_id": "query", "entry_id": "other" if mode == "entry_read" else
                           "target" if mode == "other_entry_read" else None}]
        if mode == "unrelated_page":
            query["retrieval"]["hits"] = [{"entry_id": "target"}]
    assert _cross_role_retrieved(facts, "other") is expected


@pytest.fixture(scope="module")
def budget_inputs(tmp_path_factory: pytest.TempPathFactory):
    return setup(tmp_path_factory.mktemp("sb"), tmp_path_factory)


def budget_clone(original, tmp_path_factory: pytest.TempPathFactory):
    root = local_evidence_path(tmp_path_factory.mktemp("bc"))
    for folder in ("k", "rep", "edge"):
        shutil.copytree(local_evidence_path(original.root / folder), root / folder)
    store_root = local_evidence_path(tmp_path_factory.mktemp("bs"))
    shutil.copytree(local_evidence_path(original.store.root), store_root, dirs_exist_ok=True)
    return replace(original, root=root, store=SkillValidationStore(store_root),
        knowledge=KnowledgeRetriever(KnowledgeStore(root / "k")),
        tasks={case: root / case / "input" for case in ("rep", "edge")},
        selections=dict(original.selections), template=copy.deepcopy(original.template))


def observed_budgets(monkeypatch: pytest.MonkeyPatch) -> list[_ReadBudget]:
    observed = []

    class ObservedBudget(_ReadBudget):
        def __init__(self, maximum: int):
            super().__init__(maximum)
            observed.append(self)

    monkeypatch.setattr(implementation, "_ReadBudget", ObservedBudget)
    return observed


def test_revision_reserves_entire_recursive_readback_before_source_publication(
        budget_inputs, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    payload = VersionPayload.model_validate_json(budget_inputs.new_version.payload_json)
    assert payload.candidate_approval is not None and payload.feedback_approval is not None
    candidate_id = budget_inputs.new_approval.source_candidate_id
    assert candidate_id is not None
    source = FeedbackStore(budget_inputs.store.root / "source-feedback" / candidate_id)

    def prepared(name: str, limits: FeedbackLimits | None = None) -> SkillValidationStore:
        root = local_evidence_path(tmp_path / name)
        (root / "versions").mkdir(parents=True)
        filename = budget_inputs.old_version.version_id + ".json"
        shutil.copyfile(local_evidence_path(budget_inputs.store.root / "versions" / filename), root / "versions" / filename)
        return SkillValidationStore(root, limits=limits)

    def revise(store: SkillValidationStore):
        return store.create_revision(budget_inputs.old_version.version_id, source, candidate_id,
            approval=budget_inputs.new_approval, parent_approval=budget_inputs.old_approval,
            candidate_approval=payload.candidate_approval, feedback_approval=payload.feedback_approval)

    observed = observed_budgets(monkeypatch)
    assert revise(prepared("success")) == budget_inputs.new_version
    capacity = observed[-1].used - 1
    constrained = prepared("limited", FeedbackLimits(max_capture_read_bytes=capacity))
    before = tree(constrained.root)
    with pytest.raises(ValueError):
        revise(constrained)
    assert tree(constrained.root) == before
    assert not (constrained.root / "source-feedback").exists()


def test_plan_reserves_version_sources_before_formal_publication(
        budget_inputs, tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    current = budget_clone(budget_inputs, tmp_path_factory)
    value = spec(current)
    approval = plan_approval(current, value)
    observed = observed_budgets(monkeypatch)
    current.store.freeze_plan(value, current.root, approval=approval)
    capacity = observed[-1].used - 1
    shutil.rmtree(current.store.root / "plans")
    constrained = SkillValidationStore(current.store.root, limits=FeedbackLimits(max_capture_read_bytes=capacity))
    before = tree(constrained.root)
    with pytest.raises(ValueError):
        constrained.freeze_plan(value, current.root, approval=approval)
    assert tree(constrained.root) == before
    assert not (constrained.root / "plans").exists()


def test_receipt_reserves_plan_and_recursive_sources_before_formal_publication(
        budget_inputs, tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    current = budget_clone(budget_inputs, tmp_path_factory)
    value = spec(current)
    approval = plan_approval(current, value)
    plan = current.store.freeze_plan(value, current.root, approval=approval)
    run_role(current, "rep", "new")
    observed = observed_budgets(monkeypatch)
    current.store.record_validation(plan.plan_id, "rep", "new", current.root, approval=approval)
    capacity = observed[-1].used - 1
    shutil.rmtree(current.store.root / "receipts")
    constrained = SkillValidationStore(current.store.root, limits=FeedbackLimits(max_capture_read_bytes=capacity))
    before = tree(constrained.root)
    with pytest.raises(ValueError):
        constrained.record_validation(plan.plan_id, "rep", "new", current.root, approval=approval)
    assert tree(constrained.root) == before
    assert not (constrained.root / "receipts").exists()
