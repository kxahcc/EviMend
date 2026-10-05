"""Actual synthetic SDK runs produce portable, non-promoted feedback candidates.

This exercises accounting and provenance through the Agent and stopped
evaluation path. Scripted responses are not evidence of autonomous reasoning,
learning improvement, application qualification, or transferable repair quality.
"""

from __future__ import annotations

import copy
import json
import shutil
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest
from test_agent_loop import candidate_id, response
from test_knowledge_reporting import manifest3
from test_knowledge_retrieval import admit, guidance
from test_metrics import (
    GOOD_TEXT,
    make_task,
    one_manifest,
    read_json,
    save_json,
    terminal,
)
from test_provider_loop import DEFAULT_USAGE, scripted_adapter, settings, wire_response
from test_static_skill_reporting import manifest5

from dfx_agent.agent import run_fixed_agent, run_provider_agent
from dfx_agent.applicability_contracts import TaskKnowledgeBinding
from dfx_agent.applicability_handoff import ApplicabilityPolicy
from dfx_agent.evidence_io import local_evidence_path
from dfx_agent.feedback import FeedbackStore
from dfx_agent.feedback_contracts import HostCandidateApproval, HostFeedbackApproval
from dfx_agent.knowledge import KnowledgeStore
from dfx_agent.knowledge_contracts import (
    PermissionScope,
    SolverProjection,
    content_hash,
)
from dfx_agent.knowledge_retrieval import KnowledgeRetriever
from dfx_agent.knowledge_run import HostKnowledgeInput
from dfx_agent.metrics import aggregate_evaluation, parse_evaluation_manifest
from dfx_agent.model_calls import canonical, sha256
from dfx_agent.model_provider import ChatCompletionsAdapter
from dfx_agent.static_skills import StaticSkillPolicy
from dfx_agent.task import CaseSelection, verify_task
from dfx_agent.verification import FixedTextExecutor


def inventory(root: Path) -> dict[str, bytes]:
    root = local_evidence_path(root)
    return {path.relative_to(root).as_posix(): path.read_bytes() for path in root.rglob("*") if path.is_file()}


def selected_task(root: Path, *, selected: bool = True) -> tuple[Path, CaseSelection | None]:
    task = make_task(root)
    if not selected:
        return task, None
    value = read_json(task / "task.json")
    selection = CaseSelection.model_validate({"task_id": value["task_id"], "case_key": "cases/synthetic",
        "lineage_group_id": "synthetic-lineage", "fault_variant_id": "synthetic-fault",
        "app": "app", "log": {"path": "crash.log", "sha256": value["crash_sha256"]}})
    value.update(schema_version=2, case_identity_sha256=selection.sha256())
    save_json(task / "task.json", value)
    return task, selection


@dataclass
class Seed:
    root: Path
    task: Path
    work: Path
    raw: dict[str, Any]
    report: dict[str, Any]
    skill_id: str
    selected_entry: str
    unused_entry: str
    library: Path


def seed(root: Path, *, mock: bool = False, split: str = "development", selected: bool = True,
         full_repair: bool = True, ordinary: bool = False) -> Seed:
    task, selection = selected_task(root / "t", selected=selected)
    library = root / "k"
    library.mkdir()
    store = KnowledgeStore(library)
    selected_entry = admit(store, root / "p1", "width")
    unused_entry = admit(store, root / "p2", "offset")
    scope = PermissionScope(split=split)
    snapshot = store.freeze_snapshot(tuple(sorted((selected_entry.entry_id, unused_entry.entry_id))), scope)
    retriever = KnowledgeRetriever(store)
    retriever.build_index(snapshot.snapshot_sha256, scope)
    info = verify_task(task)
    approval = TaskKnowledgeBinding(task_id=info["task_id"], run_id="r",
        source_snapshot_sha256=info["source_snapshot_sha256"], crash_sha256=info["crash_sha256"],
        snapshot_sha256=snapshot.snapshot_sha256, scope=scope)
    projection = store.read_projection(snapshot.snapshot_sha256, selected_entry.entry_id)
    skill_id = "dfx-" + content_hash(projection)[:56]
    instructions = [("search_experience", {"tags": ["ByteWidth"], "text": "容量"})] if ordinary else [
        ("read_skill", {"skill_id": skill_id})]
    if full_repair:
        almost = candidate_id(task, root / "d1", "almost")
        good = candidate_id(task, root / "d2", "good")
        instructions.extend([
            ("record_diagnosis", {"path": "src/main.ets", "line": 1, "reason": "synthetic upstream bad value"}),
            ("edit_source", {"path": "src/main.ets", "old": "bad", "new": "almost"}),
            ("register_candidate", {"diagnosis_id": "diagnosis-1"}), ("run_check", {"candidate_id": almost}),
            ("edit_source", {"path": "src/main.ets", "old": "bad", "new": "good"}),
            ("register_candidate", {"diagnosis_id": "diagnosis-1"}), ("run_check", {"candidate_id": good}),
        ])
    work = root / "r"
    executor = FixedTextExecutor("src/main.ets", GOOD_TEXT, failure_detail="synthetic almost remains wrong")
    options = {"knowledge": HostKnowledgeInput(retriever, approval), "max_calls": 12, "max_tool_calls": 12}
    if not ordinary:
        options.update({"applicability_policy": ApplicabilityPolicy(enabled=False),
                        "static_skill_policy": StaticSkillPolicy(enabled=True, entry_ids=(selected_entry.entry_id,))})
    if mock:
        stream = [wire_response(index, [(name, args)], usage=None if index == 2 else DEFAULT_USAGE)
                  for index, (name, args) in enumerate(instructions, 1)]
        stream.append(wire_response(len(instructions) + 1, usage=None))
        sent: list[dict] = []
        report = run_provider_agent(task, work, "r", scripted_adapter(stream, sent), executor, **options)
        assert len(sent) == len(instructions) + 1
    else:
        stream = [response(index, name, args, usage=index != 2)
                  for index, (name, args) in enumerate(instructions, 1)]
        stream.append(response(len(instructions) + 1, usage=False))
        report = run_fixed_agent(task, work, "r", stream, executor, **options)
    terminal(work, "r")
    raw = manifest3(root, task, work) if ordinary else manifest5(root, task, work)
    raw["tasks"][0]["selection"] = None if selection is None else selection.model_dump(mode="json")
    if selection is not None:
        for name in ("case_key", "lineage_group_id", "fault_variant_id"):
            raw["tasks"][0][name] = getattr(selection, name)
    return Seed(root, task, work, raw, report, skill_id, selected_entry.entry_id, unused_entry.entry_id, library)


@pytest.fixture(scope="module", params=(False, True), ids=("fixed", "mock"))
def repair_seed(tmp_path_factory: pytest.TempPathFactory, request: pytest.FixtureRequest) -> Seed:
    # Short roots isolate this mechanism from the unresolved generic long-path issue.
    return seed(tmp_path_factory.mktemp("fi"), mock=request.param)


@pytest.fixture(scope="module")
def ordinary_seed(tmp_path_factory: pytest.TempPathFactory) -> Seed:
    return seed(tmp_path_factory.mktemp("fo"), ordinary=True, full_repair=False)


def feedback_approval(manifest, *, excluded: tuple[str, ...] = ()) -> HostFeedbackApproval:
    return HostFeedbackApproval(approval_id="host-feedback", manifest_sha256=sha256(canonical(manifest.model_dump(mode="json"))),
        origin_split="development", allow_learning=True, allowed_splits=("development", "candidate_validation", "confirmation"),
        excluded_lineage_groups=excluded)


def candidate_approval(capsule, projection: SolverProjection) -> HostCandidateApproval:
    return HostCandidateApproval(approval_id="host-candidate", capsule_id=capsule.capsule_id,
                                 projection_sha256=sha256(canonical(projection.model_dump(mode="json"))))


def cloned_seed(tmp_path_factory: pytest.TempPathFactory, source: Seed) -> Seed:
    root = tmp_path_factory.mktemp("fc")
    # Only the declared stopped task/run and original library matter. Extra
    # derivation workspaces are not inside the capsule's accepted inventory.
    for folder in ("t", "r", "k"):
        shutil.copytree(source.root / folder, root / folder)
    return Seed(root, root / "t/input", root / "r", copy.deepcopy(source.raw), copy.deepcopy(source.report),
                source.skill_id, source.selected_entry, source.unused_entry, root / "k")


def new_store(tmp_path_factory: pytest.TempPathFactory) -> FeedbackStore:
    return FeedbackStore(tmp_path_factory.mktemp("fs"))


def test_actual_sdk_repair_preserves_failed_candidate_unknown_usage_and_full_skill(
        repair_seed: Seed, tmp_path_factory: pytest.TempPathFactory) -> None:
    s = repair_seed
    assert s.report["status"] == "offline_checked"
    assert [check["status"] for check in s.report["checks"]] == ["fail", "pass"]
    assert s.report["completion"]["verification"]["accepted"] is False
    assert s.report["completion"]["cost"]["reserved_calls"] == 9
    assert s.report["completion"]["cost"]["known_total_tokens"] == 105
    assert s.report["completion"]["cost"]["missing_usage_calls"] == 2
    assert s.report["completion"]["cost"]["total_tokens"] is None
    assert s.report["static_skill_facts"]["resources"][0]["actions"][0]["visible_solver_count"] >= 1
    manifest = parse_evaluation_manifest(s.raw)
    expected = aggregate_evaluation(manifest, s.root)
    approval = feedback_approval(manifest)
    store = new_store(tmp_path_factory)
    before = inventory(s.root)
    capsule = store.capture(manifest, s.root, approval=approval)
    assert inventory(s.root) == before
    assert json.loads(capsule.contents()["aggregate_json"]) == expected
    row = expected["rows"][0]
    assert row["identity_provenance"] == "selection_hash_bound"
    assert row["first_candidate_check_status"] == "fail" and row["latest_candidate_check_status"] == "pass"
    assert row["cost"]["known_total_tokens"] == 105 and row["cost"]["total_tokens"] is None
    assert row["verification"]["accepted"] is False and row["verification"]["application_verification"] == "not_run"
    payload = local_evidence_path(store.root / "capsules" / capsule.capsule_id / "payload")
    files = {item["path"]: item for item in capsule.contents()["files"]}
    for relative in ("r/model-calls.sqlite", "r/tool-actions.sqlite", "r/agent-report.json", "r/session.json",
                     "r/static-skills-profile.json", "t/input/task.json", "t/input/source/src/main.ets"):
        assert relative in files and (payload / relative).read_bytes() == local_evidence_path(s.root / relative).read_bytes()
    assert any(path.startswith("r/candidates/" + s.report["candidates"][0]) for path in files)
    assert any(path.startswith("r/candidates/" + s.report["candidates"][1]) for path in files)
    assert any(s.unused_entry in path for path in files), "Unqueried knowledge ancestors must remain in the capsule."
    store_before = inventory(store.root)
    assert store.read_capsule(capsule.capsule_id, approval=approval) == capsule
    assert inventory(store.root) == store_before


def test_moved_originals_are_not_needed_for_independent_read_and_pending_candidate(
        repair_seed: Seed, tmp_path_factory: pytest.TempPathFactory) -> None:
    s = cloned_seed(tmp_path_factory, repair_seed)
    manifest = parse_evaluation_manifest(s.raw)
    approval = feedback_approval(manifest)
    store = new_store(tmp_path_factory)
    capsule = store.capture(manifest, s.root, approval=approval)
    moved = s.root.with_name(s.root.name + "-moved")
    s.root.rename(moved)
    assert not s.root.exists()
    before = inventory(store.root)
    reopened = FeedbackStore(store.root).read_capsule(capsule.capsule_id, approval=approval)
    assert reopened == capsule and inventory(store.root) == before
    projection = SolverProjection(guidance=guidance("width"), derivation_authority="host_curated", validation_scopes=())
    host = candidate_approval(capsule, projection)
    candidate = store.propose_candidate(capsule.capsule_id, projection, approval=host, feedback_approval=approval)
    value = candidate.contents()
    assert value["projection"] == projection.model_dump(mode="json")
    assert value["status"] == "pending_host_review" and value["application_qualification"] == "none"
    assert value["learning_model_call_status"] == "not_run"
    assert value["learning_tokens"] is value["learning_monetary_cost"] is None
    after = inventory(store.root)
    assert store.read_candidate(candidate.candidate_id, approval=host, feedback_approval=approval) == candidate
    assert inventory(store.root) == after
    assert not (store.root / "entries").exists() and not (store.root / "snapshots").exists()


@pytest.mark.parametrize("defect", ["report", "model_ledger", "action_ledger", "failed_candidate", "latest_candidate", "static_body"])
def test_tampered_source_evidence_cannot_become_learnable_feedback(
        repair_seed: Seed, tmp_path_factory: pytest.TempPathFactory, defect: str) -> None:
    s = cloned_seed(tmp_path_factory, repair_seed)
    if defect == "report":
        report = read_json(s.work / "agent-report.json")
        report["completion"]["cost"]["known_total_tokens"] = 1
        report["completion"]["summary_sha256"] = sha256(canonical({
            key: value for key, value in report["completion"].items() if key != "summary_sha256"}))
        save_json(s.work / "agent-report.json", report)
    elif defect in ("model_ledger", "action_ledger"):
        ledger = s.work / ("model-calls.sqlite" if defect == "model_ledger" else "tool-actions.sqlite")
        table = "model_calls" if defect == "model_ledger" else "tool_actions"
        with closing(sqlite3.connect(ledger)) as connection, connection:
            connection.execute(f"UPDATE {table} SET request_sha256=?", ("6" * 64,))
    elif defect == "static_body":
        path = s.work / s.report["static_skill_profile"]["files"][0]["path"]
        path.write_bytes(path.read_bytes().replace(b"Preserve", b"Discard", 1))
    else:
        index = 0 if defect == "failed_candidate" else 1
        (s.work / "candidates" / s.report["candidates"][index] / "source/src/main.ets").write_bytes(b"tampered\n")
    manifest = parse_evaluation_manifest(s.raw)
    store = new_store(tmp_path_factory)
    before = inventory(store.root)
    source_before = inventory(s.root)
    with pytest.raises(ValueError):
        store.capture(manifest, s.root, approval=feedback_approval(manifest))
    assert inventory(store.root) == before
    assert inventory(s.root) == source_before


@pytest.mark.parametrize("defect", ["run_id", "lineage", "excluded_lineage"])
def test_manifest_or_approval_cannot_reassign_run_or_source_lineage(
        repair_seed: Seed, tmp_path_factory: pytest.TempPathFactory, defect: str) -> None:
    s = cloned_seed(tmp_path_factory, repair_seed)
    if defect == "run_id":
        s.raw["runs"][0]["run_id"] = "other-run"
    elif defect == "lineage":
        s.raw["tasks"][0]["lineage_group_id"] = "another-family"
        s.raw["tasks"][0]["selection"]["lineage_group_id"] = "another-family"
    manifest = parse_evaluation_manifest(s.raw)
    approval = feedback_approval(manifest, excluded=("synthetic-lineage",) if defect == "excluded_lineage" else ())
    store = new_store(tmp_path_factory)
    before = inventory(store.root)
    with pytest.raises(ValueError):
        store.capture(manifest, s.root, approval=approval)
    assert inventory(store.root) == before


@pytest.mark.parametrize("split", ["candidate_validation", "confirmation"])
def test_non_development_knowledge_ancestor_is_rejected_even_with_unused_entry(
        tmp_path_factory: pytest.TempPathFactory, split: str) -> None:
    s = seed(tmp_path_factory.mktemp("fa"), split=split, full_repair=False)
    assert s.report["knowledge_facts"]["queries"] == []
    assert s.report["static_skill_facts"]["unread_skill_ids"] == []
    assert s.unused_entry != s.selected_entry
    manifest = parse_evaluation_manifest(s.raw)
    store = new_store(tmp_path_factory)
    before = inventory(store.root)
    with pytest.raises(ValueError):
        store.capture(manifest, s.root, approval=feedback_approval(manifest))
    assert inventory(store.root) == before


def test_old_declared_task_lineage_does_not_gain_authenticated_learning_identity(tmp_path_factory: pytest.TempPathFactory) -> None:
    s = seed(tmp_path_factory.mktemp("fl"), selected=False, full_repair=False)
    manifest = parse_evaluation_manifest(s.raw)
    report = aggregate_evaluation(manifest, s.root)
    assert report["rows"][0]["identity_provenance"] == "manifest_declaration"
    store = new_store(tmp_path_factory)
    before = inventory(store.root)
    with pytest.raises(ValueError):
        store.capture(manifest, s.root, approval=feedback_approval(manifest))
    assert inventory(store.root) == before


def test_missing_report_retains_unknown_ordinary_feedback_without_fabricating_cost(
        ordinary_seed: Seed, tmp_path_factory: pytest.TempPathFactory) -> None:
    s = cloned_seed(tmp_path_factory, ordinary_seed)
    assert read_json(s.work / "session.json")["schema_version"] == 11
    assert len(s.report["knowledge_facts"]["queries"]) == 1
    (s.work / "agent-report.json").unlink()
    manifest = parse_evaluation_manifest(s.raw)
    store = new_store(tmp_path_factory)
    approval = feedback_approval(manifest)
    before = inventory(s.root)
    capsule = store.capture(manifest, s.root, approval=approval)
    row = json.loads(capsule.contents()["aggregate_json"])["rows"][0]
    assert row["record_status"] == "missing" and row["missing_reason"] == "completion_missing"
    assert row["cost"] is None and row["synthetic_outcome"] == "unknown"
    assert row["verification"]["accepted"] is False
    assert "r/model-calls.sqlite" in {item["path"] for item in capsule.contents()["files"]}
    assert inventory(s.root) == before


@pytest.mark.parametrize("defect", ["legacy_8", "missing_provenance", "missing_profile", "missing_session"])
def test_missing_report_ordinary_knowledge_cannot_bypass_authenticated_learning_admission(
        ordinary_seed: Seed, tmp_path_factory: pytest.TempPathFactory, defect: str) -> None:
    s = cloned_seed(tmp_path_factory, ordinary_seed)
    (s.work / "agent-report.json").unlink()
    session = read_json(s.work / "session.json")
    assert session["schema_version"] == 11
    if defect == "missing_session":
        (s.work / "session.json").unlink()
    else:
        if defect == "legacy_8":
            session["schema_version"] = 8
            session.pop("execution_provenance")
        elif defect == "missing_provenance":
            session.pop("execution_provenance")
        else:
            session.pop("knowledge_profile")
        save_json(s.work / "session.json", session)
    manifest = parse_evaluation_manifest(s.raw)
    store = new_store(tmp_path_factory)
    before = inventory(store.root)
    source_before = inventory(s.root)
    with pytest.raises(ValueError):
        store.capture(manifest, s.root, approval=feedback_approval(manifest))
    assert inventory(store.root) == before
    assert inventory(s.root) == source_before


@pytest.mark.parametrize("split", ["candidate_validation", "confirmation"])
def test_missing_report_cannot_launder_original_non_development_ordinary_knowledge(
        tmp_path_factory: pytest.TempPathFactory, split: str) -> None:
    s = seed(tmp_path_factory.mktemp("fv"), ordinary=True, full_repair=False, split=split)
    assert read_json(s.work / "session.json")["schema_version"] == 11
    assert len(s.report["knowledge_facts"]["queries"]) == 1
    (s.work / "agent-report.json").unlink()
    manifest = parse_evaluation_manifest(s.raw)
    store = new_store(tmp_path_factory)
    before = inventory(store.root)
    with pytest.raises(ValueError):
        store.capture(manifest, s.root, approval=feedback_approval(manifest))
    assert inventory(store.root) == before


def test_unknown_provider_result_remains_unknown_and_candidate_is_never_promoted(tmp_path_factory: pytest.TempPathFactory) -> None:
    root = tmp_path_factory.mktemp("fu")
    task, selection = selected_task(root / "t")
    work = root / "r"
    sends = []

    def timeout(request: httpx.Request) -> httpx.Response:
        sends.append(request)
        raise httpx.ReadTimeout("synthetic provider result unknown", request=request)

    adapter = ChatCompletionsAdapter(settings(), httpx.MockTransport(timeout))
    report = run_provider_agent(task, work, "r", adapter, FixedTextExecutor("src/main.ets", GOOD_TEXT))
    terminal(work, "r", status="failed")
    assert report["stopped_reason"] == "model_result_unknown" and len(sends) == 1
    raw = one_manifest(root, task, work, "r", selection=selection.model_dump(mode="json"))
    manifest = parse_evaluation_manifest(raw)
    approval = feedback_approval(manifest)
    store = new_store(tmp_path_factory)
    capsule = store.capture(manifest, root, approval=approval)
    row = json.loads(capsule.contents()["aggregate_json"])["rows"][0]
    assert row["synthetic_outcome"] == "unknown" and row["terminal_status"] == "failed"
    assert row["cost"]["reserved_calls"] == row["cost"]["uncertain_calls"] == 1
    assert row["cost"]["total_tokens"] is None and row["cost"]["monetary_cost"] is None
    projection = SolverProjection(guidance=guidance("width"), derivation_authority="host_curated", validation_scopes=())
    candidate = store.propose_candidate(capsule.capsule_id, projection, approval=candidate_approval(capsule, projection),
                                       feedback_approval=approval)
    assert candidate.contents()["status"] == "pending_host_review"
    assert candidate.contents()["application_qualification"] == "none"
    assert len(sends) == 1
