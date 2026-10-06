"""Actual fixed-response SDK consumption of a reviewed model version.

Candidate validation is deliberately separate from eligible deployment. These
scripted reads test integration, not autonomous learning or repair benefits.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any, cast

import pytest
from test_agent_loop import response
from test_knowledge_reporting import open_run
from test_metrics import read_json, terminal
from test_model_skill_versions import model_version as model_version  # noqa: PLC0414 -- pytest fixture re-export
from test_skill_validation_integration import expected_text, manifest, setup

from dfx_agent.agent import _tools, run_fixed_agent
from dfx_agent.applicability_contracts import TaskKnowledgeBinding
from dfx_agent.applicability_handoff import ApplicabilityPolicy
from dfx_agent.conditions import ConditionStore
from dfx_agent.evidence_io import local_evidence_path
from dfx_agent.knowledge_run import HostKnowledgeInput, read_run_knowledge
from dfx_agent.metrics import aggregate_evaluation, parse_evaluation_manifest
from dfx_agent.model_calls import canonical, sha256
from dfx_agent.policies import PolicyFeatures
from dfx_agent.skill_evolution_contracts import (
    HostValidationPlanApprovalV2,
    PlanSpecV2,
    ValidationPairV2,
)
from dfx_agent.skill_transition_contracts import SyntheticValidationPolicyV2
from dfx_agent.solver_visibility import SolverVisibilityStore
from dfx_agent.task import digest, snapshot_digest, verify_task
from dfx_agent.verification import FixedTextExecutor
from dfx_agent.version_skill_contracts import TaskVersionSkillBinding, VersionSkillGrant
from dfx_agent.version_skills import (
    HostValidationSkillInput,
    preview_version_skill_profile,
    read_run_version_skills,
)
from dfx_agent.working_context_plan import WorkingContextPolicy, WorkingContextStore

pytest_plugins = ("learning_workflow_support",)


@pytest.fixture(scope="module")
def planned_model_pool(model_version, tmp_path_factory: pytest.TempPathFactory):
    root = local_evidence_path(tmp_path_factory.mktemp("msdk"))
    inputs = setup(root, tmp_path_factory)
    (_model_root, store, seed, seed_approval, version, approval, _learning,
     _learning_approval, _source, _draft, _sent) = model_version
    inputs.store, inputs.old_version, inputs.new_version = store, seed, version
    inputs.old_approval, inputs.new_approval = seed_approval, approval
    inputs.old_skill_id = "dfx-" + seed.content_sha256[:56]
    inputs.new_skill_id = "dfx-" + version.content_sha256[:56]
    grants = tuple(sorted((VersionSkillGrant(version_id=seed.version_id,
        version_approval=seed_approval.model_dump(mode="json")), VersionSkillGrant(version_id=version.version_id,
        version_approval=approval.model_dump(mode="json"))), key=lambda grant: grant.version_id))
    shared = preview_version_skill_profile(store, grants)
    # Reconstruct installed descriptors from an actual calibration workspace.
    # The descriptor-only proxy has no body/execute method and is never invoked.
    calibration_work = root / "c0"
    calibration_task = next((root / "c").rglob("task.json")).parent
    workspace, ledger, _calls, _actions = open_run(calibration_task, calibration_work)
    session = read_json(calibration_work / "session.json")
    knowledge = read_run_knowledge(workspace, calibration_task, session["knowledge_profile"], ledger=ledger)
    context = session["context_profile"]
    working = WorkingContextStore(workspace, None, None,
        WorkingContextPolicy.model_validate(context["host_working_context"]), session["model_profile"],
        format_version=context.get("working_context_format", 1))
    visibility = SolverVisibilityStore(workspace, calibration_task, ledger, session["model_profile"],
        working_context_store=working)
    tools = _tools(workspace, ledger, session["max_candidates"], calibration_task, "crash.log",
        verify_task(calibration_task)["crash_sha256"], PolicyFeatures.model_validate(session["policy_features"]),
        frozenset(session["capabilities"]), ConditionStore(workspace), knowledge_store=knowledge,
        applicability_policy=ApplicabilityPolicy(enabled=False),
        solver_visibility=visibility, model_calls=_calls, actions=_actions,
        static_skill_service=cast(Any, SimpleNamespace(profile={"source_type": "approved_versions"})))
    descriptors = [{"name": item.name, "description": item.description,
        "schema": item.args_schema.model_json_schema()} for item in tools]
    pairs, raws = [], {}
    policy = SyntheticValidationPolicyV2(min_representative_pairs=1, min_boundary_pairs=1,
        min_net_gain=1, max_success_losses=0, max_validation_tokens=1000,
        max_validation_calls=100, max_validation_attempts=2, max_learning_tokens=1000, max_learning_calls=4)
    for case, kind in (("rep", "representative"), ("edge", "boundary")):
        for role in ("old", "new"):
            raw = manifest(inputs, case, role)
            raw["schema_version"] = 6
            raw["configuration"].pop("shared_static_skill_profile")
            raw["configuration"].update(shared_version_skill_profile=shared, tool_schema_sha256=sha256(canonical(descriptors)))
            raw["variants"][0].pop("static_skills_enabled")
            raw["variants"][0]["version_skills_enabled"] = False
            raws[case, role] = parse_evaluation_manifest(raw)
        pairs.append(ValidationPairV2(case_id=case, kind=kind,
            old_manifest_json=canonical(raws[case, "old"].model_dump(mode="json")),
            new_manifest_json=canonical(raws[case, "new"].model_dump(mode="json")),
            old_entry_id=seed.version_id, new_entry_id=version.version_id))
    spec = PlanSpecV2(old_version_id=seed.version_id, new_version_id=version.version_id,
        pairs=tuple(pairs), policy_json=canonical(policy.model_dump(mode="json")))
    plan_approval = HostValidationPlanApprovalV2(approval_id="model-validation-plan",
        spec_sha256=sha256(canonical(spec.model_dump(mode="json"))),
        old_version_approval=seed_approval, new_version_approval=approval)
    plan = store.freeze_plan(spec, root, approval=plan_approval)
    return inputs, grants, plan, plan_approval, raws


def binding_for(planned, case="rep", role="new"):
    inputs, grants, plan, approved, _raws = planned
    info = verify_task(inputs.tasks[case])
    return TaskVersionSkillBinding(approval_id="startup-" + case + "-" + role,
        task_id=case, run_id=case + "-" + role, source_snapshot_sha256=info["source_snapshot_sha256"],
        crash_sha256=info["crash_sha256"], case_selection=inputs.selections[case], scope=inputs.scope,
        purpose="candidate_validation", grants=grants, validation_spec_sha256=approved.spec_sha256,
        validation_plan_json=canonical(plan.model_dump(mode="json")), validation_plan_approval=approved.model_dump(mode="json"),
        validation_case_id=case, validation_role=role)


@pytest.fixture(scope="module")
def model_sdk_run(planned_model_pool):
    inputs, _grants, _plan, _approved, _raws = planned_model_pool
    task, work, run = inputs.tasks["rep"], inputs.root / "rep-new", "rep-new"
    info, approved = verify_task(task), binding_for(planned_model_pool)
    knowledge_approval = TaskKnowledgeBinding(task_id="rep", run_id=run,
        source_snapshot_sha256=info["source_snapshot_sha256"], crash_sha256=info["crash_sha256"],
        snapshot_sha256=inputs.snapshot.snapshot_sha256, scope=inputs.scope)
    report = run_fixed_agent(task, work, run, [response(1, "read_skill", {"skill_id": inputs.new_skill_id}), response(2)],
        FixedTextExecutor("src/main.ets", expected_text("rep")),
        knowledge=HostKnowledgeInput(inputs.knowledge, knowledge_approval), applicability_policy=ApplicabilityPolicy(enabled=False),
        version_skills=HostValidationSkillInput(inputs.store, approved, enabled=False), max_calls=12, max_tool_calls=12)
    terminal(work, run)
    return inputs, task, work, approved, knowledge_approval, report


def test_actual_sdk_keeps_model_authority_full_read_cost_and_visibility(model_sdk_run, planned_model_pool):
    inputs, _task, work, _approved, _knowledge_approval, report = model_sdk_run
    assert (read_json(work / "session.json")["schema_version"], report["schema_version"],
            report["completion"]["schema_version"]) == (14, 11, 8)
    assert report["model_calls"] == 2, report["stopped_reason"]
    assert len(report["offered_tools"]) == 20
    resource = next(item for item in report["version_skill_facts"]["resources"] if item["skill_id"] == inputs.new_skill_id)
    assert resource["derivation_authority"] == "model_inference" and len(resource["actions"]) == 1
    assert resource["actions"][0]["visible_solver_count"] == 1
    assert resource["source_costs"]["ancestry"]["cost"]["reserved_calls"] == 2
    assert report["completion"]["cost"]["reserved_calls"] == 2
    assert report["completion"]["cost"]["total_tokens"] == 30
    row = aggregate_evaluation(planned_model_pool[4]["rep", "new"], inputs.root)["rows"][0]
    assert row["record_status"] == "verified" and row["version_skill_profile"] == report["version_skill_profile"]


def test_default_resume_of_disabled_catalog_keeps_original_version_without_source_library(model_sdk_run):
    inputs, task, work, approved, knowledge_approval, report = model_sdk_run
    inputs.store.root.rename(inputs.store.root.with_name("source-version-library-moved"))
    resumed = run_fixed_agent(task, work, "rep-new", [], FixedTextExecutor("src/main.ets", expected_text("rep")),
        resume=True, knowledge_approval=knowledge_approval, version_skill_approval=approved,
        applicability_policy=ApplicabilityPolicy(enabled=False), max_calls=12, max_tool_calls=12)
    terminal(work, "rep-new")
    assert resumed["version_skill_profile"] == report["version_skill_profile"]
    assert resumed["version_skill_facts"] == report["version_skill_facts"]
    assert resumed["completion"]["cost"] == report["completion"]["cost"]
    assert resumed["model_calls"] == 2


def test_warm_service_rejects_self_consistent_changed_task_source(model_sdk_run):
    _inputs, task, work, _approved, _knowledge_approval, _report = model_sdk_run
    workspace, ledger, _calls, _actions = open_run(task, work)
    profile = read_json(work / "session.json")["version_skill_profile"]
    service = read_run_version_skills(workspace, task, profile, ledger=ledger)
    assert service is not None
    original_paths = [task / "source/src/main.ets", task / "source-manifest.json", task / "task.json"]
    original = {path: path.read_bytes() for path in original_paths}
    try:
        changed = original[original_paths[0]] + b"\n// self-consistent different source\n"
        original_paths[0].write_bytes(changed)
        entries = read_json(original_paths[1])
        next(item for item in entries if item["path"] == "src/main.ets")["sha256"] = digest(changed)
        original_paths[1].write_text(json.dumps(entries), encoding="utf-8")
        info = read_json(original_paths[2])
        info["source_snapshot_sha256"] = snapshot_digest(entries)
        original_paths[2].write_text(json.dumps(info), encoding="utf-8")
        assert verify_task(task)["source_snapshot_sha256"] != workspace.source_snapshot_sha256
        with pytest.raises(ValueError, match="actual workspace"):
            service._validate()
        with pytest.raises(ValueError, match="actual workspace"):
            service.expected_section()
    finally:
        for path, raw in original.items():
            path.write_bytes(raw)


def test_copied_body_change_rejects_resume(model_sdk_run):
    _inputs, task, work, approved, knowledge_approval, _report = model_sdk_run
    profile = read_json(work / "version-skills-profile.json")
    resource = next(item for item in profile["resources"] if item["derivation_authority"] == "model_inference")
    path = work / resource["path"]
    path.write_bytes(path.read_bytes() + b"\nUnapproved extra procedure\n")
    with pytest.raises(ValueError):
        run_fixed_agent(task, work, "rep-new", [], FixedTextExecutor("src/main.ets", expected_text("rep")),
            resume=True, knowledge_approval=knowledge_approval, version_skill_approval=approved,
            applicability_policy=ApplicabilityPolicy(enabled=False), max_calls=12, max_tool_calls=12)
    assert json.loads((work / "agent-report.json").read_text(encoding="utf-8"))["model_calls"] == 2
