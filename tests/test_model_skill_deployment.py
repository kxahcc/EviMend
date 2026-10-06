"""Actual synthetic SDK validation, publication and portable model-version use.

Every repair response is scripted. These tests establish complete bookkeeping,
not autonomous repair benefit, causal accuracy or application qualification.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from test_agent_loop import candidate_id, response
from test_applicability_reporting import manifest4
from test_exporting import io_path, managed_inputs
from test_feedback_integration import feedback_approval, inventory
from test_metrics import read_json, save_json, terminal
from test_model_skill_sdk import binding_for
from test_model_skill_sdk import (
    planned_model_pool as planned_model_pool,  # noqa: PLC0414 -- pytest fixture re-export
)
from test_model_skill_versions import (
    model_version as model_version,  # noqa: PLC0414 -- pytest fixture re-export
)
from test_skill_validation_integration import expected_text, selection_task

from dfx_agent import exporting
from dfx_agent.agent import run_fixed_agent
from dfx_agent.applicability_contracts import TaskKnowledgeBinding
from dfx_agent.applicability_handoff import ApplicabilityPolicy
from dfx_agent.evidence_io import local_evidence_path
from dfx_agent.feedback import FeedbackStore
from dfx_agent.knowledge_contracts import PermissionScope
from dfx_agent.knowledge_run import HostKnowledgeInput
from dfx_agent.metrics import aggregate_evaluation, parse_evaluation_manifest
from dfx_agent.skill_evolution import SkillTransitionStore
from dfx_agent.skill_transition_contracts import (
    HostPublicationApproval,
    HostPublicationApprovalV2,
    HostTransitionApprovalV2,
)
from dfx_agent.task import verify_task
from dfx_agent.verification import FixedTextExecutor
from dfx_agent.version_skill_contracts import (
    TaskVersionSkillBinding,
    VersionSkillGrant,
    shared_version_skill_profile,
)
from dfx_agent.version_skills import (
    HostDerivedSkillInput,
    HostValidationSkillInput,
    _compile,
    authenticated_learning_lineages,
)

pytest_plugins = ("learning_workflow_support",)


def repair_script(task: Path, root: Path, skill_id: str, *, passes: bool = True):
    almost = candidate_id(task, root / "almost", "almost")
    instructions = [
        ("read_skill", {"skill_id": skill_id}),
        ("record_diagnosis", {"path": "src/main.ets", "line": 1,
                              "reason": "synthetic upstream producer"}),
        ("edit_source", {"path": "src/main.ets", "old": "bad", "new": "almost"}),
        ("register_candidate", {"diagnosis_id": "diagnosis-1"}),
        ("run_check", {"candidate_id": almost}),
    ]
    if passes:
        good = candidate_id(task, root / "good", "good")
        instructions.extend([
            ("edit_source", {"path": "src/main.ets", "old": "bad", "new": "good"}),
            ("register_candidate", {"diagnosis_id": "diagnosis-1"}),
            ("run_check", {"candidate_id": good}),
        ])
    return [*[response(index, name, args) for index, (name, args) in enumerate(instructions, 1)],
            response(len(instructions) + 1)]


@pytest.fixture(scope="module")
def model_validation_runs(planned_model_pool):
    inputs, _grants, plan, plan_host, _raws = planned_model_pool
    reports, receipts = {}, {}
    for case, role, passes in (("rep", "old", False), ("rep", "new", True),
                               ("edge", "old", True), ("edge", "new", True)):
        task, run = inputs.tasks[case], case + "-" + role
        work = inputs.root / run
        assert not work.exists(), "Every role must start after the prospective plan."
        info = verify_task(task)
        approved = binding_for(planned_model_pool, case, role)
        knowledge_host = TaskKnowledgeBinding(task_id=case, run_id=run,
            source_snapshot_sha256=info["source_snapshot_sha256"], crash_sha256=info["crash_sha256"],
            snapshot_sha256=inputs.snapshot.snapshot_sha256, scope=inputs.scope)
        target = inputs.old_skill_id if role == "old" else inputs.new_skill_id
        reports[case, role] = run_fixed_agent(task, work, run,
            repair_script(task, inputs.root / (run + "-derive"), target, passes=passes),
            FixedTextExecutor("src/main.ets", expected_text(case)),
            knowledge=HostKnowledgeInput(inputs.knowledge, knowledge_host),
            applicability_policy=ApplicabilityPolicy(enabled=False),
            version_skills=HostValidationSkillInput(inputs.store, approved, enabled=False),
            max_calls=12, max_tool_calls=12)
        terminal(work, run)
        assert reports[case, role]["model_calls"] == (9 if passes else 6), reports[case, role]["stopped_reason"]
        receipts[case, role] = inputs.store.record_validation(plan.plan_id, case, role, inputs.root, approval=plan_host)
    return inputs, plan, plan_host, reports, receipts


def test_four_sdk_roles_preserve_first_failure_actual_use_and_full_known_cost(model_validation_runs):
    inputs, plan, plan_host, reports, receipts = model_validation_runs
    assert plan.schema_version == 2
    assert set(plan.contents()["observed_absent_work_paths"]) == {"rep-old", "rep-new", "edge-old", "edge-new"}
    for (case, role), receipt in receipts.items():
        report = reports[case, role]
        assert len(report["offered_tools"]) == 20
        assert [check["status"] for check in report["checks"]] == (["fail"] if (case, role) == ("rep", "old")
                                                                else ["fail", "pass"])
        assert report["completion"]["verification"]["accepted"] is False
        value = receipt.contents()
        assert receipt.schema_version == 2 and value["use_status"] == "followed"
        assert value["application_qualification"] == "none" and value["allow_learning"] is False
        row = json.loads(value["aggregate_json"])["rows"][0]
        assert row["record_status"] == "verified" and row["first_candidate_check_status"] == "fail"
        assert row["cost"]["total_tokens"] == (90 if (case, role) == ("rep", "old") else 135)
        target = inputs.old_skill_id if role == "old" else inputs.new_skill_id
        resource = next(item for item in row["version_skill_facts"]["resources"] if item["skill_id"] == target)
        assert len(resource["actions"]) == 1 and resource["actions"][0]["visible_solver_count"] >= 1
    assert inputs.store.read_plan(plan.plan_id, approval=plan_host) == plan


@pytest.fixture(scope="module")
def model_eligible(model_validation_runs):
    inputs, plan, plan_host, _reports, receipts = model_validation_runs
    store = SkillTransitionStore(inputs.store)
    approved = HostTransitionApprovalV2(approval_id="model-complete-validation", plan_id=plan.plan_id,
        receipt_ids=tuple(sorted(receipt.receipt_id for receipt in receipts.values())))
    transition = store.decide_skill_transition(plan.plan_id, approval=approved, plan_approval=plan_host)
    assert transition.contents()["state"] == "synthetic_eligible", transition.contents()["reasons"]
    return inputs, store, transition, approved


def test_current_v2_qualification_accounts_learning_separately_from_all_validation_roles(model_eligible):
    inputs, store, transition, approved = model_eligible
    facts = json.loads(transition.contents()["facts_json"])
    assert transition.schema_version == 2 and transition.contents()["application_qualification"] == "none"
    assert facts["net_gain_numerator"] == 1 and facts["success_losses"] == 0
    assert facts["validation_cost"]["total_tokens"] == 495
    assert facts["validation_cost"]["known_reserved_calls"] == 33
    assert facts["learning_cost"]["status"] == "observed"
    assert facts["learning_cost"]["tokens"] == 30 and facts["learning_cost"]["known_reserved_calls"] == 2
    assert facts["learning_cost"]["monetary_cost"] is None
    assert store.read_transition(transition.transition_id, approval=approved) == transition
    assert json.loads(inputs.new_version.payload_json)["application_qualification"] == "none"


@pytest.fixture(scope="module")
def model_deployment(model_eligible):
    inputs, transitions, transition, transition_host = model_eligible
    seed_host = HostPublicationApproval(approval_id="model-seed-publication", alias="model-capacity",
        action="initialize_seed", version_id=inputs.old_version.version_id, split="development")
    seed_publication = transitions.publish_alias(approval=seed_host, version_approval=inputs.old_approval)
    activated_host = HostPublicationApprovalV2(approval_id="model-revision-publication", alias="model-capacity",
        action="activate", version_id=inputs.new_version.version_id, transition_id=transition.transition_id,
        previous_publication_id=seed_publication.publication_id, split="development")
    activated = transitions.publish_alias(approval=activated_host, version_approval=inputs.new_approval,
        transition_approval=transition_host)
    selected = transitions.freeze_alias("model-capacity", approval=activated_host)
    assert selected.schema_version == 2 and selected.publication_id == activated.publication_id
    task, case = selection_task(inputs.root / "deployment-task", identifier="deployment", lineage="unseen-deployment")
    info, run = verify_task(task), "deployment-run"
    scope = PermissionScope(split="development")
    knowledge_store = inputs.knowledge.store
    snapshot = knowledge_store.freeze_snapshot(tuple(sorted((inputs.old_entry_id, inputs.new_entry_id))), scope)
    inputs.knowledge.build_index(snapshot.snapshot_sha256, scope)
    knowledge_host = TaskKnowledgeBinding(task_id="deployment", run_id=run,
        source_snapshot_sha256=info["source_snapshot_sha256"], crash_sha256=info["crash_sha256"],
        snapshot_sha256=snapshot.snapshot_sha256, scope=scope)
    grant = VersionSkillGrant(version_id=selected.version_id, version_approval=inputs.new_approval.model_dump(mode="json"),
        selection=selected.model_dump(mode="json"), publication_approval=activated_host.model_dump(mode="json"))
    approved = TaskVersionSkillBinding(approval_id="model-development-startup", task_id="deployment", run_id=run,
        source_snapshot_sha256=info["source_snapshot_sha256"], crash_sha256=info["crash_sha256"],
        case_selection=case, scope=scope, purpose="deployment", grants=(grant,))
    work = inputs.root / run
    report = run_fixed_agent(task, work, run, [response(1, "read_skill", {"skill_id": inputs.new_skill_id}), response(2)],
        FixedTextExecutor("src/main.ets", expected_text("rep")), knowledge=HostKnowledgeInput(inputs.knowledge, knowledge_host),
        applicability_policy=ApplicabilityPolicy(enabled=False),
        version_skills=HostDerivedSkillInput(transitions, approved, enabled=True), max_calls=12, max_tool_calls=12)
    terminal(work, run)
    assert report["model_calls"] == 2, report["stopped_reason"]
    raw = manifest4(inputs.root, task, work)
    raw["schema_version"] = 6
    raw["configuration"]["shared_version_skill_profile"] = shared_version_skill_profile(report["version_skill_profile"])
    raw["variants"][0]["version_skills_enabled"] = True
    raw["tasks"][0].update(selection=case.model_dump(mode="json"), case_key=case.case_key,
                          lineage_group_id=case.lineage_group_id, fault_variant_id=case.fault_variant_id)
    manifest = parse_evaluation_manifest(raw)
    return inputs, transitions, activated, activated_host, task, work, approved, knowledge_host, report, manifest


def test_published_model_version_enters_actual_development_sdk_and_old_run_keeps_original_after_rollback(model_deployment):
    inputs, transitions, activated, _activated_host, task, work, approved, knowledge_host, report, manifest = model_deployment
    resource = report["version_skill_facts"]["resources"][0]
    assert resource["derivation_authority"] == "model_inference"
    assert len(resource["actions"]) == 1 and resource["actions"][0]["visible_solver_count"] == 1
    assert resource["source_costs"]["ancestry"]["cost"]["total_tokens"] == 30
    assert report["completion"]["cost"]["total_tokens"] == 30
    rollback_host = HostPublicationApprovalV2(approval_id="model-rollback-to-seed", alias="model-capacity",
        action="rollback", version_id=inputs.old_version.version_id,
        previous_publication_id=activated.publication_id, split="development")
    rollback = transitions.publish_alias(approval=rollback_host, version_approval=inputs.old_approval)
    seed_selected = transitions.freeze_alias("model-capacity", approval=rollback_host)
    assert seed_selected.publication_id == rollback.publication_id
    seed_grant = VersionSkillGrant(version_id=seed_selected.version_id,
        version_approval=inputs.old_approval.model_dump(mode="json"), selection=seed_selected.model_dump(mode="json"),
        publication_approval=rollback_host.model_dump(mode="json"))
    shared, bodies, originals = _compile(inputs.store, (seed_grant,), transitions=transitions, capture_current=True)
    rolled_resource = shared["resources"][0]
    assert rolled_resource["version_id"] == inputs.old_version.version_id
    assert rolled_resource["derivation_authority"] == "host_curated" and len(bodies) == 1
    assert "synthetic-lineage" not in rolled_resource["learning_lineage_groups"]
    authority = rolled_resource["source_authority"]
    assert set(authority["version_ids"]) == {inputs.old_version.version_id, inputs.new_version.version_id}
    assert {"seed-lineage", "synthetic-lineage"} <= set(authority["learning_lineage_groups"])
    assert set(authority["allowed_splits"]) == set(inputs.old_approval.allowed_splits).intersection(inputs.new_approval.allowed_splits)
    assert any(path.endswith("/versions/" + inputs.new_version.version_id + ".json") for path in originals)
    source_root = inputs.store.root
    moved_source = source_root.with_name(source_root.name + "-deployment-source-moved")
    source_root.rename(moved_source)
    try:
        resumed = run_fixed_agent(task, work, "deployment-run", [], FixedTextExecutor("src/main.ets", expected_text("rep")),
            resume=True, knowledge_approval=knowledge_host, version_skill_approval=approved,
            applicability_policy=ApplicabilityPolicy(enabled=False), max_calls=12, max_tool_calls=12)
    finally:
        moved_source.rename(source_root)
    terminal(work, "deployment-run")
    assert resumed["version_skill_profile"] == report["version_skill_profile"]
    assert resumed["version_skill_facts"] == report["version_skill_facts"]
    assert resumed["model_calls"] == 2 and resumed["completion"]["cost"] == report["completion"]["cost"]
    assert aggregate_evaluation(manifest, inputs.root)["rows"][0]["record_status"] == "verified"


def test_private_export_and_feedback_keep_full_original_version_closure_and_declared_lineages(
        model_deployment, tmp_path_factory: pytest.TempPathFactory):
    inputs, _transitions, _activated, _activated_host, _task, work, _approved, _knowledge_host, report, manifest = model_deployment
    host = feedback_approval(manifest)
    feedback = FeedbackStore(local_evidence_path(tmp_path_factory.mktemp("model-feedback")))
    before = inventory(work)
    capsule = feedback.capture(manifest, inputs.root, approval=host)
    assert inventory(work) == before
    files = {item["path"] for item in capsule.contents()["files"]}
    assert "deployment-run/version-skills-profile.json" in files
    assert any(path.startswith("deployment-run/version-skill-sources/") and path.endswith("/source.json") for path in files)
    assert any(path.startswith("deployment-run/version-skill-sources/") and path.endswith("/owner.json") for path in files)
    assert any("/publications/" in path for path in files)
    groups = authenticated_learning_lineages(work, report["version_skill_profile"], host)
    assert {"seed-lineage", "synthetic-lineage"} <= groups
    row = json.loads(capsule.contents()["aggregate_json"])["rows"][0]
    assert groups == set(row["version_skill_profile"]["resources"][0]["learning_lineage_groups"])
    before_feedback = inventory(feedback.root)
    assert FeedbackStore(feedback.root).read_capsule(capsule.capsule_id, approval=host) == capsule
    assert inventory(feedback.root) == before_feedback
    export = managed_inputs(inputs.root, manifest.model_dump(mode="json"))
    # The shared helper has a larger export-only fixture limit. This mechanism
    # must exercise the repository's real configured limit instead.
    original_settings = read_json(Path(__file__).resolve().parents[1] / "configs/local-runtime.json")
    save_json(inputs.root / "configs/local-runtime.json", original_settings)
    receipt = export.apply(export.freeze())
    destination = io_path(tmp_path_factory.mktemp("model-export") / "package")
    shutil.move(str(receipt["package_path"]), str(destination))
    moved = inputs.root.with_name(inputs.root.name + "-originals-moved")
    inputs.root.rename(moved)
    try:
        package_before = inventory(destination)
        verified = exporting.verify_export(destination)
        assert verified["aggregate_sha256"] == receipt["aggregate_sha256"]
        exported = read_json(destination / "aggregate-report.json")["rows"][0]
        assert exported["version_skill_facts"] == report["version_skill_facts"]
        assert (destination / "payload/deployment-run/version-skills-profile.json").is_file()
        assert inventory(destination) == package_before
    finally:
        moved.rename(inputs.root)
