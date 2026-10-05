"""Preplanned, source-bound Skill validation from actual synthetic SDK runs.

Scripted repair outcomes only verify the bookkeeping and use gates. Distinct
fixture lineage declarations do not establish real-world independence, and
these results never prove learning benefit or application qualification.
"""

from __future__ import annotations

import copy
import json
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from test_agent_loop import candidate_id, response
from test_feedback_integration import (
    candidate_approval,
    feedback_approval,
    inventory,
    seed,
)
from test_knowledge_retrieval import admit, guidance
from test_metrics import GOOD_TEXT, make_task, read_json, save_json, terminal
from test_static_skill_reporting import manifest5

from dfx_agent.agent import run_fixed_agent
from dfx_agent.applicability_contracts import TaskKnowledgeBinding
from dfx_agent.applicability_handoff import ApplicabilityPolicy
from dfx_agent.evidence_io import local_evidence_path
from dfx_agent.feedback import FeedbackStore
from dfx_agent.knowledge import KnowledgeStore
from dfx_agent.knowledge_contracts import (
    ExperienceContent,
    PermissionScope,
    SolverProjection,
    content_hash,
)
from dfx_agent.knowledge_retrieval import KnowledgeRetriever
from dfx_agent.knowledge_run import HostKnowledgeInput
from dfx_agent.metrics import parse_evaluation_manifest
from dfx_agent.model_calls import ModelCallStore, canonical, sha256
from dfx_agent.skill_evolution_contracts import (
    HostSkillVersionApproval,
    HostValidationPlanApproval,
    PlanSpec,
    ValidationPair,
)
from dfx_agent.skill_validation import SkillValidationStore
from dfx_agent.static_skills import StaticSkillPolicy
from dfx_agent.task import CaseSelection, digest, snapshot_digest, verify_task
from dfx_agent.verification import FixedTextExecutor


def revised_projection() -> SolverProjection:
    original = guidance("width").model_dump(mode="json")
    original["trigger"] = "Capacity/element mismatch: inspect current producer and preserve boundary behavior."
    original["counterexamples"].append("A passing final repair does not erase the first failed attempt.")
    return SolverProjection(guidance=ExperienceContent.model_validate_json(canonical(original)),
                            derivation_authority="host_curated", validation_scopes=())


def selection_task(root: Path, *, identifier: str, lineage: str, boundary: bool = False) -> tuple[Path, CaseSelection]:
    task = make_task(root, task_id=identifier)
    info = read_json(task / "task.json")
    if boundary:
        source = b"value=bad\nother=boundary-stable\n"
        (task / "source/src/main.ets").write_bytes(source)
        entries = [{"path": "src/main.ets", "sha256": digest(source)}]
        save_json(task / "source-manifest.json", entries)
        info["source_snapshot_sha256"] = snapshot_digest(entries)
    selection = CaseSelection.model_validate({"task_id": identifier, "case_key": "cases/" + identifier,
        "lineage_group_id": lineage, "fault_variant_id": "fault-" + identifier, "app": "app/" + identifier,
        "log": {"path": "logs/" + identifier + ".log", "sha256": info["crash_sha256"]}})
    info.update(schema_version=2, case_identity_sha256=selection.sha256())
    save_json(task / "task.json", info)
    return task, selection


@dataclass
class Inputs:
    root: Path
    store: Any
    old_version: Any
    new_version: Any
    old_approval: Any
    new_approval: Any
    knowledge: Any
    scope: PermissionScope
    snapshot: Any
    old_entry_id: str
    new_entry_id: str
    old_skill_id: str
    new_skill_id: str
    tasks: dict[str, Path]
    selections: dict[str, CaseSelection]
    template: dict[str, Any]


def setup(root: Path, tmp_path_factory: pytest.TempPathFactory) -> Inputs:
    # Learning input is a real scripted development run. The proposed change is
    # explicit host curation, rather than a disguised learning-model response.
    source = seed(tmp_path_factory.mktemp("sl"), mock=True)
    source_manifest = parse_evaluation_manifest(source.raw)
    source_approval = feedback_approval(source_manifest)
    feedback = FeedbackStore(tmp_path_factory.mktemp("sf"))
    capsule = feedback.capture(source_manifest, source.root, approval=source_approval)
    new_projection = revised_projection()
    candidate_host = candidate_approval(capsule, new_projection)
    candidate = feedback.propose_candidate(capsule.capsule_id, new_projection,
        approval=candidate_host, feedback_approval=source_approval)
    old_projection = SolverProjection(guidance=guidance("width"), derivation_authority="host_curated", validation_scopes=())
    store = SkillValidationStore(tmp_path_factory.mktemp("sv"))
    old_host = HostSkillVersionApproval(approval_id="seed-version", skill_id="capacity-process",
        projection_sha256=sha256(canonical(old_projection.model_dump(mode="json"))),
        allowed_splits=("development", "candidate_validation", "confirmation"), learning_lineage_groups=("seed-lineage",))
    old_version = store.create_seed(old_projection, approval=old_host)
    new_host = HostSkillVersionApproval(approval_id="revision-version", skill_id="capacity-process",
        projection_sha256=sha256(canonical(new_projection.model_dump(mode="json"))),
        allowed_splits=("development", "candidate_validation", "confirmation"),
        parent_version_id=old_version.version_id, source_candidate_id=candidate.candidate_id)
    new_version = store.create_revision(old_version.version_id, feedback, candidate.candidate_id,
        approval=new_host, parent_approval=old_host, candidate_approval=candidate_host, feedback_approval=source_approval)
    source.root.rename(source.root.with_name(source.root.name + "-moved"))
    feedback.root.rename(feedback.root.with_name(feedback.root.name + "-moved"))
    assert store.read_version(new_version.version_id, approval=new_host) == new_version
    knowledge_root = root / "k"
    knowledge_root.mkdir(parents=True)
    knowledge_store = KnowledgeStore(knowledge_root)
    old_entry = admit(knowledge_store, root / "p1", "width", content=old_projection.guidance)
    new_entry = admit(knowledge_store, root / "p2", "width", content=new_projection.guidance)
    scope = PermissionScope(split="candidate_validation")
    snapshot = knowledge_store.freeze_snapshot(tuple(sorted((old_entry.entry_id, new_entry.entry_id))), scope)
    retriever = KnowledgeRetriever(knowledge_store)
    retriever.build_index(snapshot.snapshot_sha256, scope)
    tasks = {}
    selections = {}
    for case, boundary in (("rep", False), ("edge", True)):
        tasks[case], selections[case] = selection_task(root / case, identifier=case, lineage="validation-" + case,
                                                     boundary=boundary)
    calibrate_task, _selection = selection_task(root / "c", identifier="cal", lineage="calibration-only")
    calibrate_info = verify_task(calibrate_task)
    host = HostKnowledgeInput(retriever, TaskKnowledgeBinding(task_id="cal", run_id="c0",
        source_snapshot_sha256=calibrate_info["source_snapshot_sha256"], crash_sha256=calibrate_info["crash_sha256"],
        snapshot_sha256=snapshot.snapshot_sha256, scope=scope))
    calibration = root / "c0"
    calibration_report = run_fixed_agent(calibrate_task, calibration, "c0", [], FixedTextExecutor("src/main.ets", GOOD_TEXT),
        knowledge=host, applicability_policy=ApplicabilityPolicy(enabled=False),
        static_skill_policy=StaticSkillPolicy(enabled=True, entry_ids=tuple(sorted((old_entry.entry_id, new_entry.entry_id)))),
        max_calls=12, max_tool_calls=12)
    terminal(calibration, "c0")
    assert calibration_report["model_calls"] == 0
    assert ModelCallStore(calibration / "model-calls.sqlite", read_only=True).records("cal", "c0") == []
    template = manifest5(root, calibrate_task, calibration)
    old_skill_id = "dfx-" + content_hash(old_projection)[:56]
    new_skill_id = "dfx-" + content_hash(new_projection)[:56]
    return Inputs(root, store, old_version, new_version, old_host, new_host, retriever, scope, snapshot,
                  old_entry.entry_id, new_entry.entry_id, old_skill_id, new_skill_id, tasks, selections, template)


def manifest(inputs: Inputs, case: str, role: str) -> dict[str, Any]:
    raw = copy.deepcopy(inputs.template)
    info = verify_task(inputs.tasks[case])
    selection = inputs.selections[case]
    raw["evaluation_id"] = "validation-" + case + "-" + role
    raw["tasks"][0].update(task_id=case, case_key=selection.case_key, lineage_group_id=selection.lineage_group_id,
        fault_variant_id=selection.fault_variant_id, task_path=inputs.tasks[case].relative_to(inputs.root).as_posix(),
        task_sha256=digest((inputs.tasks[case] / "task.json").read_bytes()),
        source_snapshot_sha256=info["source_snapshot_sha256"], crash_sha256=info["crash_sha256"],
        selection=selection.model_dump(mode="json"), protocol_sha256=FixedTextExecutor("src/main.ets", expected_text(case)).protocol_sha256)
    raw["runs"][0].update(task_id=case, run_id=case + "-" + role, work_path=case + "-" + role)
    return parse_evaluation_manifest(raw).model_dump(mode="json")


def expected_text(case: str) -> str:
    return "value=good\nother=boundary-stable\n" if case == "edge" else GOOD_TEXT


def spec(inputs: Inputs, *, mode: str = "normal") -> PlanSpec:
    pairs = []
    for case, kind in (("rep", "representative"), ("edge", "boundary")):
        old = manifest(inputs, case, "old")
        new = manifest(inputs, case, "new")
        if mode == "budget_drift" and case == "rep":
            new["configuration"]["max_calls"] += 1
        pairs.append(ValidationPair(case_id=case, kind=kind, old_manifest_json=canonical(old), new_manifest_json=canonical(new),
                                    old_entry_id=inputs.old_entry_id, new_entry_id=inputs.new_entry_id))
    return PlanSpec(old_version_id=inputs.old_version.version_id, new_version_id=inputs.new_version.version_id, pairs=tuple(pairs))


def plan_approval(inputs: Inputs, value: PlanSpec) -> HostValidationPlanApproval:
    return HostValidationPlanApproval(approval_id="host-validation-plan", spec_sha256=sha256(canonical(value.model_dump(mode="json"))),
        origin_split="candidate_validation", allow_learning=False,
        old_version_approval=inputs.old_approval, new_version_approval=inputs.new_approval)


def run_role(inputs: Inputs, case: str, role: str, *, mode: str = "normal") -> dict[str, Any]:
    task = inputs.tasks[case]
    work = inputs.root / (case + "-" + role)
    target = inputs.old_skill_id if role == "old" else inputs.new_skill_id
    if mode == "wrong_skill":
        target = inputs.old_skill_id if role == "new" else inputs.new_skill_id
    call = ("read_source", {"path": "src/main.ets"}) if mode == "no_read" else ("read_skill", {"skill_id": target})
    instructions = [call]
    if mode != "no_later_input":
        almost = candidate_id(task, inputs.root / (case + role + "d1"), "almost")
        good = candidate_id(task, inputs.root / (case + role + "d2"), "good")
        instructions.extend([
            ("record_diagnosis", {"path": "src/main.ets", "line": 1, "reason": "synthetic bad producer"}),
            ("edit_source", {"path": "src/main.ets", "old": "bad", "new": "almost"}),
            ("register_candidate", {"diagnosis_id": "diagnosis-1"}), ("run_check", {"candidate_id": almost}),
            ("edit_source", {"path": "src/main.ets", "old": "bad", "new": "good"}),
            ("register_candidate", {"diagnosis_id": "diagnosis-1"}), ("run_check", {"candidate_id": good}),
        ])
    responses = [response(index, name, args, usage=index != 2) for index, (name, args) in enumerate(instructions, 1)]
    if mode != "no_later_input":
        responses.append(response(len(instructions) + 1, usage=False))
    info = verify_task(task)
    host = HostKnowledgeInput(inputs.knowledge, TaskKnowledgeBinding(task_id=case, run_id=case + "-" + role,
        source_snapshot_sha256=info["source_snapshot_sha256"], crash_sha256=info["crash_sha256"],
        snapshot_sha256=inputs.snapshot.snapshot_sha256, scope=inputs.scope))
    report = run_fixed_agent(task, work, case + "-" + role, responses,
        FixedTextExecutor("src/main.ets", expected_text(case)), knowledge=host,
        applicability_policy=ApplicabilityPolicy(enabled=False),
        static_skill_policy=StaticSkillPolicy(enabled=True, entry_ids=tuple(sorted((inputs.old_entry_id, inputs.new_entry_id)))),
        max_calls=12, max_tool_calls=12)
    terminal(work, case + "-" + role)
    return report


def cloned_inputs(original: Inputs, tmp_path_factory: pytest.TempPathFactory) -> Inputs:
    root = tmp_path_factory.mktemp("sc")
    for folder in ("k", "rep", "edge"):
        shutil.copytree(local_evidence_path(original.root / folder), local_evidence_path(root / folder))
    store_root = tmp_path_factory.mktemp("sx")
    shutil.copytree(original.store.root, local_evidence_path(store_root), dirs_exist_ok=True)
    return Inputs(root, SkillValidationStore(store_root), original.old_version, original.new_version,
        original.old_approval, original.new_approval, KnowledgeRetriever(KnowledgeStore(root / "k")),
        original.scope, original.snapshot, original.old_entry_id, original.new_entry_id,
        original.old_skill_id, original.new_skill_id, {case: root / case / "input" for case in ("rep", "edge")},
        dict(original.selections), copy.deepcopy(original.template))


@pytest.fixture(scope="module")
def inputs(tmp_path_factory: pytest.TempPathFactory) -> Inputs:
    return setup(tmp_path_factory.mktemp("si"), tmp_path_factory)


@pytest.fixture(scope="module")
def validated_runs(inputs: Inputs):
    value = spec(inputs)
    approval = plan_approval(inputs, value)
    plan = inputs.store.freeze_plan(value, inputs.root, approval=approval)
    reports = {}
    receipts = {}
    for case in ("rep", "edge"):
        for role in ("old", "new"):
            assert not (inputs.root / (case + "-" + role)).exists()
            reports[case, role] = run_role(inputs, case, role)
            receipts[case, role] = inputs.store.record_validation(plan.plan_id, case, role, inputs.root, approval=approval)
    return plan, approval, reports, receipts


def test_plan_before_actual_solver_and_receipts_preserve_first_failure_and_unknown_cost(inputs: Inputs, validated_runs) -> None:
    plan, approval, reports, receipts = validated_runs
    plan_value = plan.contents()
    assert plan_value["time_authority"] == "host_observation"
    assert set(plan_value["observed_absent_work_paths"]) == {"rep-old", "rep-new", "edge-old", "edge-new"}
    assert {"seed-lineage", "synthetic-lineage"} <= set(plan_value["learning_lineage_groups"])
    assert inputs.store.read_plan(plan.plan_id, approval=approval) == plan
    for (case, role), receipt in receipts.items():
        session = read_json(inputs.root / (case + "-" + role) / "session.json")
        assert datetime.fromisoformat(plan_value["frozen_at"]) <= datetime.fromisoformat(session["started_at"])
        report = reports[case, role]
        assert [check["status"] for check in report["checks"]] == ["fail", "pass"]
        assert report["completion"]["cost"]["reserved_calls"] == 9
        assert report["completion"]["cost"]["known_total_tokens"] == 105
        assert report["completion"]["cost"]["total_tokens"] is None
        value = receipt.contents()
        assert value["use_status"] == "followed"
        assert value["origin_split"] == "candidate_validation" and value["allow_learning"] is False
        assert value["application_qualification"] == "none"
        aggregate = json.loads(value["aggregate_json"])
        row = aggregate["rows"][0]
        assert row["first_candidate_check_status"] == "fail" and row["latest_candidate_check_status"] == "pass"
        assert row["cost"]["known_total_tokens"] == 105 and row["cost"]["total_tokens"] is None
        assert row["verification"]["accepted"] is False
        assert inputs.store.read_receipt(receipt.receipt_id, approval=approval) == receipt


def test_validation_partition_cannot_return_as_development_learning_feedback(
        inputs: Inputs, validated_runs, tmp_path_factory: pytest.TempPathFactory) -> None:
    _plan, _approval, _reports, receipts = validated_runs
    receipt = receipts["rep", "new"]
    source_manifest = parse_evaluation_manifest(json.loads(receipt.contents()["manifest_json"]))
    feedback = FeedbackStore(tmp_path_factory.mktemp("sx"))
    before = inventory(feedback.root)
    with pytest.raises(ValueError):
        feedback.capture(source_manifest, inputs.root, approval=feedback_approval(source_manifest))
    assert inventory(feedback.root) == before


def test_receipts_independently_read_after_original_validation_runs_move(inputs: Inputs, validated_runs) -> None:
    _plan, approval, _reports, receipts = validated_runs
    moved = inputs.root.with_name(inputs.root.name + "-moved")
    inputs.root.rename(moved)
    try:
        before = inventory(inputs.store.root)
        reopened = SkillValidationStore(inputs.store.root)
        for receipt in receipts.values():
            assert reopened.read_receipt(receipt.receipt_id, approval=approval) == receipt
        assert inventory(inputs.store.root) == before and not inputs.root.exists()
    finally:
        moved.rename(inputs.root)


@pytest.mark.parametrize("mode", ["no_read", "wrong_skill", "no_later_input"])
def test_catalog_or_wrong_or_unseen_read_never_grants_validation_use(
        inputs: Inputs, tmp_path_factory: pytest.TempPathFactory, mode: str) -> None:
    current = cloned_inputs(inputs, tmp_path_factory)
    value = spec(current)
    approval = plan_approval(current, value)
    plan = current.store.freeze_plan(value, current.root, approval=approval)
    report = run_role(current, "rep", "new", mode=mode)
    receipt = current.store.record_validation(plan.plan_id, "rep", "new", current.root, approval=approval)
    content = receipt.contents()
    assert content["use_status"] in {"not_followed", "unknown"}
    aggregate = json.loads(content["aggregate_json"])
    assert aggregate["rows"][0]["cost"]["reserved_calls"] == report["completion"]["cost"]["reserved_calls"]
    assert content["application_qualification"] == "none"


def test_existing_solver_reservation_prevents_retrospective_plan_registration(inputs: Inputs, tmp_path_factory: pytest.TempPathFactory) -> None:
    current = cloned_inputs(inputs, tmp_path_factory)
    run_role(current, "rep", "old")
    actual = ModelCallStore(current.root / "rep-old/model-calls.sqlite", read_only=True).records("rep", "rep-old")
    assert actual and any(record.intent.kind == "solver" for record in actual)
    value = spec(current)
    approval = plan_approval(current, value)
    before = inventory(current.store.root)
    with pytest.raises(ValueError):
        current.store.freeze_plan(value, current.root, approval=approval)
    assert inventory(current.store.root) == before


def test_pair_resource_or_budget_change_is_rejected_before_any_role_run(inputs: Inputs, tmp_path_factory: pytest.TempPathFactory) -> None:
    current = cloned_inputs(inputs, tmp_path_factory)
    before = inventory(current.store.root)
    with pytest.raises(ValueError):
        value = spec(current, mode="budget_drift")
        current.store.freeze_plan(value, current.root, approval=plan_approval(current, value))
    assert inventory(current.store.root) == before


@pytest.mark.parametrize("lineage", ["seed-lineage", "synthetic-lineage"])
def test_parent_and_feedback_source_lineages_cannot_be_independent_validation_cases(
        inputs: Inputs, tmp_path_factory: pytest.TempPathFactory, lineage: str) -> None:
    current = cloned_inputs(inputs, tmp_path_factory)
    changed = current.selections["rep"].model_dump(mode="json")
    changed["lineage_group_id"] = lineage
    current.selections["rep"] = CaseSelection.model_validate_json(canonical(changed))
    task = read_json(current.tasks["rep"] / "task.json")
    task["case_identity_sha256"] = current.selections["rep"].sha256()
    save_json(current.tasks["rep"] / "task.json", task)
    value = spec(current)
    before = inventory(current.store.root)
    with pytest.raises(ValueError):
        current.store.freeze_plan(value, current.root, approval=plan_approval(current, value))
    assert inventory(current.store.root) == before
