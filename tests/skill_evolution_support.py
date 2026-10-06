"""Shared actual-SDK fixtures and immutable evidence copies for Skill tests.

This module is not collected as a test. Thresholds are synthetic fixture choices;
copied receipts retain original provenance and are authenticated by every API.
"""

from __future__ import annotations

import copy
import shutil
from dataclasses import dataclass, replace
from typing import Any

import pytest
from test_agent_loop import candidate_id, response
from test_metrics import read_json, save_json, terminal
from test_skill_validation_integration import (
    Inputs,
    expected_text,
    plan_approval,
    setup,
)

from dfx_agent.agent import run_fixed_agent
from dfx_agent.applicability_contracts import TaskKnowledgeBinding
from dfx_agent.applicability_handoff import ApplicabilityPolicy
from dfx_agent.evidence_io import local_evidence_path
from dfx_agent.knowledge import KnowledgeStore
from dfx_agent.knowledge_retrieval import KnowledgeRetriever
from dfx_agent.knowledge_run import HostKnowledgeInput
from dfx_agent.metrics import parse_evaluation_manifest
from dfx_agent.model_calls import canonical
from dfx_agent.skill_evolution import SkillTransitionStore
from dfx_agent.skill_evolution_contracts import PlanSpec, ValidationPair
from dfx_agent.skill_transition_contracts import (
    HostPublicationApproval,
    HostTransitionApproval,
    SyntheticValidationPolicy,
)
from dfx_agent.skill_validation import SkillValidationStore
from dfx_agent.static_skills import StaticSkillPolicy
from dfx_agent.task import CaseSelection, digest, verify_task
from dfx_agent.verification import FixedTextExecutor


def policy(**changes: Any) -> SyntheticValidationPolicy:
    # All numbers are fixture choices, never proposed real promotion thresholds.
    values = {"min_representative_pairs": 1, "min_boundary_pairs": 1, "min_net_gain": 1,
        "max_success_losses": 0, "max_validation_tokens": 1000, "max_validation_calls": 100,
        "max_validation_attempts": 2}
    values.update(changes)
    return SyntheticValidationPolicy(**values)


def clone_store(original: SkillValidationStore, factory: pytest.TempPathFactory) -> SkillValidationStore:
    destination = local_evidence_path(factory.mktemp("ec"))
    shutil.copytree(original.root, destination, dirs_exist_ok=True)
    return SkillValidationStore(destination)


def fresh_inputs(original: Inputs, factory: pytest.TempPathFactory, *, share_store: bool = False,
                 distinct_cases: bool = False) -> Inputs:
    root = factory.mktemp("er")
    for folder in ("k", "rep", "edge"):
        shutil.copytree(original.root / folder, root / folder)
    store = original.store if share_store else clone_store(original.store, factory)
    result = replace(original, root=root, store=store, knowledge=KnowledgeRetriever(KnowledgeStore(root / "k")),
        tasks={case: root / case / "input" for case in ("rep", "edge")},
        selections=dict(original.selections), template=copy.deepcopy(original.template))
    if distinct_cases:
        for case in ("rep", "edge"):
            task = result.tasks[case]
            info = read_json(task / "task.json")
            identifier = case + "-" + root.name
            crash = ("synthetic distinct validation crash " + identifier).encode("utf-8")
            (task / "crash.log").write_bytes(crash)
            selection = CaseSelection.model_validate({"task_id": identifier, "case_key": "cases/" + identifier,
                "lineage_group_id": "independent-" + identifier, "fault_variant_id": "fault-" + identifier,
                "app": "app/" + identifier, "log": {"path": "logs/" + identifier + ".log", "sha256": digest(crash)}})
            info.update(task_id=identifier, crash_sha256=digest(crash), case_identity_sha256=selection.sha256())
            save_json(task / "task.json", info)
            result.selections[case] = selection
    return result


def freeze(inputs: Inputs, value: SyntheticValidationPolicy | None):
    pairs = []
    for case, kind in (("rep", "representative"), ("edge", "boundary")):
        manifests = {}
        for role in ("old", "new"):
            raw = copy.deepcopy(inputs.template)
            task = inputs.tasks[case]
            info = verify_task(task)
            selection = inputs.selections[case]
            raw["evaluation_id"] = "evolution-" + selection.task_id + "-" + role
            raw["tasks"][0].update(task_id=selection.task_id, case_key=selection.case_key,
                lineage_group_id=selection.lineage_group_id, fault_variant_id=selection.fault_variant_id,
                task_path=task.relative_to(inputs.root).as_posix(), task_sha256=digest((task / "task.json").read_bytes()),
                source_snapshot_sha256=info["source_snapshot_sha256"], crash_sha256=info["crash_sha256"],
                selection=selection.model_dump(mode="json"),
                protocol_sha256=FixedTextExecutor("src/main.ets", expected_text(case)).protocol_sha256)
            raw["runs"][0].update(task_id=selection.task_id, run_id=case + "-" + role, work_path=case + "-" + role)
            manifests[role] = canonical(parse_evaluation_manifest(raw).model_dump(mode="json"))
        pairs.append(ValidationPair(case_id=case, kind=kind, old_manifest_json=manifests["old"],
            new_manifest_json=manifests["new"], old_entry_id=inputs.old_entry_id, new_entry_id=inputs.new_entry_id))
    specification = PlanSpec(old_version_id=inputs.old_version.version_id, new_version_id=inputs.new_version.version_id,
        pairs=tuple(pairs), policy_json=None if value is None else canonical(value.model_dump(mode="json")))
    approval = plan_approval(inputs, specification)
    plan = inputs.store.freeze_plan(specification, inputs.root, approval=approval)
    return plan, approval


def known_role(inputs: Inputs, case: str, role: str, *, outcome: str = "pass", read_target: bool = True) -> dict:
    task = inputs.tasks[case]
    work = inputs.root / (case + "-" + role)
    target = inputs.old_skill_id if role == "old" else inputs.new_skill_id
    first = ("read_skill", {"skill_id": target}) if read_target else ("read_source", {"path": "src/main.ets"})
    almost = candidate_id(task, inputs.root / (case + role + "da"), "almost")
    instructions = [first,
        ("record_diagnosis", {"path": "src/main.ets", "line": 1, "reason": "synthetic upstream producer"}),
        ("edit_source", {"path": "src/main.ets", "old": "bad", "new": "almost"}),
        ("register_candidate", {"diagnosis_id": "diagnosis-1"}), ("run_check", {"candidate_id": almost})]
    if outcome == "pass":
        good = candidate_id(task, inputs.root / (case + role + "db"), "good")
        instructions.extend([
            ("edit_source", {"path": "src/main.ets", "old": "bad", "new": "good"}),
            ("register_candidate", {"diagnosis_id": "diagnosis-1"}), ("run_check", {"candidate_id": good})])
    responses = [response(index, name, args) for index, (name, args) in enumerate(instructions, 1)]
    responses.append(response(len(instructions) + 1))
    info = verify_task(task)
    approval = TaskKnowledgeBinding(task_id=info["task_id"], run_id=case + "-" + role,
        source_snapshot_sha256=info["source_snapshot_sha256"], crash_sha256=info["crash_sha256"],
        snapshot_sha256=inputs.snapshot.snapshot_sha256, scope=inputs.scope)
    report = run_fixed_agent(task, work, case + "-" + role, responses,
        FixedTextExecutor("src/main.ets", expected_text(case)), knowledge=HostKnowledgeInput(inputs.knowledge, approval),
        applicability_policy=ApplicabilityPolicy(enabled=False),
        static_skill_policy=StaticSkillPolicy(enabled=True, entry_ids=tuple(sorted((inputs.old_entry_id, inputs.new_entry_id)))),
        max_calls=12, max_tool_calls=12)
    terminal(work, case + "-" + role)
    return report


@dataclass
class Runs:
    inputs: Inputs
    plans: dict[str, Any]
    approvals: dict[str, Any]
    reports: dict[tuple[str, str], dict]


@dataclass
class Bundle:
    runs: Runs
    validation: SkillValidationStore
    store: SkillTransitionStore
    plan: Any
    plan_approval: Any
    receipts: dict[tuple[str, str], Any]


def bundle(runs: Runs, factory: pytest.TempPathFactory, name: str = "eligible") -> Bundle:
    validation = clone_store(runs.inputs.store, factory)
    plan, approved = runs.plans[name], runs.approvals[name]
    receipts = {(case, role): validation.record_validation(plan.plan_id, case, role, runs.inputs.root, approval=approved)
                for case, role in runs.reports}
    return Bundle(runs, validation, SkillTransitionStore(validation), plan, approved, receipts)


def clone_bundle(original: Bundle, factory: pytest.TempPathFactory) -> Bundle:
    # Copy immutable, already authenticated originals, without copying an
    # admission decision. Every consumer still revalidates the complete receipt.
    validation = clone_store(original.validation, factory)
    return replace(original, validation=validation, store=SkillTransitionStore(validation),
                   receipts=dict(original.receipts))


def transition_approval(value: Bundle, *, receipts=None, previous: str | None = None,
                        identifier: str = "transition-fixture") -> HostTransitionApproval:
    selected = value.receipts.values() if receipts is None else receipts
    return HostTransitionApproval(approval_id=identifier, plan_id=value.plan.plan_id,
        receipt_ids=tuple(sorted(receipt.receipt_id for receipt in selected)), previous_transition_id=previous)


def decide(value: Bundle, *, approval: HostTransitionApproval | None = None):
    approved = transition_approval(value) if approval is None else approval
    return value.store.decide_skill_transition(value.plan.plan_id, approval=approved, plan_approval=value.plan_approval), approved


@pytest.fixture(scope="session")
def base_inputs(tmp_path_factory: pytest.TempPathFactory) -> Inputs:
    return setup(tmp_path_factory.mktemp("eb"), tmp_path_factory)


@pytest.fixture(scope="session")
def known_runs(base_inputs: Inputs, tmp_path_factory: pytest.TempPathFactory) -> Runs:
    current = fresh_inputs(base_inputs, tmp_path_factory)
    plans, approvals = {}, {}
    variants = {"eligible": policy(), "net_gain": policy(min_net_gain=2),
        "token_budget": policy(max_validation_tokens=200), "call_budget": policy(max_validation_calls=20),
        "attempt_budget": policy(max_validation_attempts=1),
        "coverage": policy(min_boundary_pairs=2), "unset": None, "partial": policy(min_net_gain=None)}
    # Freeze every software-test policy before inspecting any role outcome.
    for name, value in variants.items():
        plans[name], approvals[name] = freeze(current, value)
    outcomes = {("rep", "old"): "fail", ("rep", "new"): "pass",
                ("edge", "old"): "pass", ("edge", "new"): "pass"}
    reports = {(case, role): known_role(current, case, role, outcome=outcome)
               for (case, role), outcome in outcomes.items()}
    return Runs(current, plans, approvals, reports)


@pytest.fixture(scope="session")
def prepared_bundles(known_runs: Runs, tmp_path_factory: pytest.TempPathFactory):
    # Lazily prepare each prospective policy's receipts once. Do not precompute
    # or reuse eligibility: isolated stores are audited by the production API.
    prepared: dict[str, Bundle] = {}

    def prepare(name: str = "eligible") -> Bundle:
        if name not in prepared:
            prepared[name] = bundle(known_runs, tmp_path_factory, name)
        return prepared[name]

    return prepare


@pytest.fixture(scope="session")
def eligible_bundle(prepared_bundles, tmp_path_factory: pytest.TempPathFactory):
    value = clone_bundle(prepared_bundles(), tmp_path_factory)
    transition, approved = decide(value)
    return value, transition, approved


def seed_publication(value: Bundle):
    inputs = value.runs.inputs
    approved = HostPublicationApproval(approval_id="initialize-alias", alias="capacity", action="initialize_seed",
        version_id=inputs.old_version.version_id, split="development")
    published = value.store.publish_alias(approval=approved, version_approval=inputs.old_approval)
    return published, approved
