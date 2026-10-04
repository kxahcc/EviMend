"""Explicit applicability evidence stays read-only, fairly compared and portable."""

from __future__ import annotations

import copy
import shutil
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage
from test_agent_loop import response
from test_applicability_sdk import run_applicability_sdk
from test_exporting import io_path, managed_inputs
from test_knowledge_reporting import manifest3, open_run
from test_knowledge_retrieval import corpus
from test_metrics import make_task, read_json, run_synthetic, save_json, terminal
from test_readonly_evidence import tree_bytes

from dfx_agent import exporting
from dfx_agent.actions import ToolActionStore
from dfx_agent.applicability_contracts import TaskKnowledgeBinding
from dfx_agent.applicability_handoff import ApplicabilityPolicy
from dfx_agent.knowledge_run import HostKnowledgeInput
from dfx_agent.metrics import aggregate_evaluation, parse_evaluation_manifest
from dfx_agent.model_calls import canonical, sha256
from dfx_agent.reporting import build_completion, read_completion
from dfx_agent.task import verify_task


def manifest4(root: Path, task: Path, work: Path) -> dict:
    value = manifest3(root, task, work)
    value["schema_version"] = 4
    value["variants"][0]["applicability_enabled"] = read_json(work / "session.json")["applicability_profile"]["enabled"]
    return value


@pytest.fixture
def applicability_run(tmp_path: Path):
    root = tmp_path / "evaluation"
    task = make_task(root / "tasks/task1")
    library = corpus(tmp_path / "curated")
    info = verify_task(task)
    approval = TaskKnowledgeBinding(task_id=info["task_id"], run_id="run1",
        source_snapshot_sha256=info["source_snapshot_sha256"], crash_sha256=info["crash_sha256"],
        snapshot_sha256=library.snapshot.snapshot_sha256, scope=library.scope)
    work = root / "work/run1"
    report = run_synthetic(task, work, "run1", [
        response(1, "search_experience", {"tags": ["ByteWidth"], "text": "容量"}), response(2),
    ], knowledge=HostKnowledgeInput(library.retriever, approval), applicability_policy=ApplicabilityPolicy(enabled=True))
    return root, task, work, library, report


def _run_budget_rejection_sdk(tmp_path: Path, *, rejection_count: int = 2, max_calls: int | None = None):
    root = tmp_path / "evaluation"
    task = make_task(root / "tasks/task1")
    library = corpus(tmp_path / "curated")
    info = verify_task(task)
    approval = TaskKnowledgeBinding(task_id=info["task_id"], run_id="run1",
        source_snapshot_sha256=info["source_snapshot_sha256"], crash_sha256=info["crash_sha256"],
        snapshot_sha256=library.snapshot.snapshot_sha256, scope=library.scope)
    work = root / "work/run1"
    calls = [{"name": "search_experience", "args": {"tags": ["ByteWidth"], "text": "容量"}, "id": "call-search"}]
    calls.extend({"name": "prepare_experience_context", "args": {"query_id": "bound-query-" + str(index) * 64},
                  "id": f"call-prepare{index}"} for index in range(1, rejection_count + 1))
    siblings = AIMessage(content="", tool_calls=calls,
        usage_metadata={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15})
    options = {} if max_calls is None else {"max_calls": max_calls}
    report = run_synthetic(task, work, "run1", [siblings, response(2)],
        knowledge=HostKnowledgeInput(library.retriever, approval), applicability_policy=ApplicabilityPolicy(),
        max_tool_calls=1, **options)
    return root, task, work, report


@pytest.fixture
def budget_rejection_run(tmp_path: Path):
    return _run_budget_rejection_sdk(tmp_path)


def test_actual_sdk_sibling_budget_rejections_preserve_full_cost_and_authenticated_summary(budget_rejection_run) -> None:
    root, task, work, report = budget_rejection_run
    opened = open_run(task, work)
    rows = opened[3].records()
    assert report["actions"] == report["completion"]["execution_facts"]["actions"] == rows
    assert len(rows) == 3 and {row["model_call_id"] for row in rows} == {rows[0]["model_call_id"]}
    assert rows[0]["tool_name"] == "search_experience" and rows[0]["authorization"] == "allowed"
    assert rows[0]["state"] == "completed" and rows[0]["rejection"] is None
    rejected = [row for row in rows if row["rejection"] == "budget_exhausted"]
    assert len(rejected) == 2
    assert all(row["tool_name"] == "prepare_experience_context" and row["authorization"] == "rejected"
               and row["state"] == "completed" for row in rejected)
    links = [{key: row[key] for key in (
        "action_id", "model_call_id", "tool_name", "state", "authorization", "receipt_sha256", "rejection"
    )} for row in rejected]
    facts = report["applicability_facts"]
    assert facts == report["completion"]["applicability_facts"]
    assert facts["budget_rejections"] == [{"model_call_id": rows[0]["model_call_id"], "count": 2,
        "completed_count": 2, "unknown_count": 0, "links_sha256": sha256(canonical(links)),
        "reason": "budget_exhausted", "authority": "rejected_action_records"}]
    assert facts["actions"] == facts["contexts"] == facts["reviews"] == []
    assert facts["materials"] == []
    assert facts["unreferenced_context_ids"] == facts["unreferenced_review_ids"] == []
    assert not any((work / name).exists() for name in (
        "applicability-contexts", "applicability-reviews", "applicability-materials"
    ))
    assert len(report["knowledge_facts"]["queries"]) == 1
    assert len(report["knowledge_facts"]["queries"][0]["actions"]) == 1
    cost = report["completion"]["cost"]
    assert report["tool_calls"] == cost["tool_calls"] == 3
    assert report["rejected_tool_calls"] == cost["rejected_tool_calls"] == 2
    assert report["uncertain_tool_calls"] == cost["uncertain_tool_calls"] == 0
    assert cost["reserved_calls"] == cost["calls_by_kind"]["solver"] == 2
    assert cost["total_tokens"] == 30 and cost["usage_complete"] is True
    assert report["completion"]["verification"]["accepted"] is False
    before = tree_bytes(root)
    assert read_completion(*opened, task_directory=task) == report["completion"]
    assert tree_bytes(root) == before


def test_sdk_interrupted_budget_rejection_receipts_remain_unknown(tmp_path: Path, monkeypatch) -> None:
    original = ToolActionStore.complete

    def interrupt(self, identifier, message):
        if message.name == "prepare_experience_context" and message.content == "rejected: budget_exhausted":
            raise RuntimeError("synthetic interruption before budget rejection receipt")
        return original(self, identifier, message)

    monkeypatch.setattr(ToolActionStore, "complete", interrupt)
    with pytest.raises(RuntimeError, match="before budget rejection receipt"):
        _run_budget_rejection_sdk(tmp_path)
    task, work = tmp_path / "evaluation/tasks/task1/input", tmp_path / "evaluation/work/run1"
    opened = open_run(task, work)
    rows = opened[3].records()
    rejected = [row for row in rows if row["rejection"] == "budget_exhausted"]
    assert rejected and all(row["state"] != "completed" and row["receipt_sha256"] is None for row in rejected)
    links = [{key: row[key] for key in (
        "action_id", "model_call_id", "tool_name", "state", "authorization", "receipt_sha256", "rejection"
    )} for row in rejected]
    before = tree_bytes(work)
    completion = build_completion(*opened, stopped_reason="action_result_unknown", task_directory=task)
    facts = completion["applicability_facts"]
    assert facts["budget_rejections"] == [{"model_call_id": rejected[0]["model_call_id"], "count": len(rejected),
        "completed_count": 0, "unknown_count": len(rejected), "links_sha256": sha256(canonical(links)),
        "reason": "budget_exhausted", "authority": "rejected_action_records"}]
    assert facts["actions"] == facts["contexts"] == facts["reviews"] == []
    assert completion["cost"]["tool_calls"] == len(rows)
    assert completion["cost"]["rejected_tool_calls"] == completion["cost"]["uncertain_tool_calls"] == len(rejected)
    assert completion["suggested_status"] == "unresolved"
    assert "action_result_unknown" in completion["unresolved_reasons"]
    assert completion["verification"]["accepted"] is False
    assert not (work / "agent-report.json").exists()
    assert tree_bytes(work) == before


def test_same_parent_mixed_completed_and_unknown_budget_rejections_keep_ordered_hash(tmp_path: Path) -> None:
    root, task, work, report = _run_budget_rejection_sdk(tmp_path, rejection_count=3, max_calls=1)
    assert report["completion"]["cost"]["reserved_calls"] == 1
    assert report["stopped_reason"] == "model_budget"
    rows = open_run(task, work)[3].records()
    rejected = [row for row in rows if row["rejection"] == "budget_exhausted"]
    assert len(rows) == 4 and len(rejected) == 3
    # No later solver input contains these receipts: dropping one retained
    # result tests ledger uncertainty without corrupting an authenticated input.
    with closing(sqlite3.connect(work / "tool-actions.sqlite")) as connection, connection:
        assert connection.execute("UPDATE tool_actions SET state='dispatched', message_json=NULL, "
            "message_sha256=NULL WHERE action_id=?", (rejected[1]["action_id"],)).rowcount == 1
    opened = open_run(task, work)
    rows = opened[3].records()
    rejected = [row for row in rows if row["rejection"] == "budget_exhausted"]
    assert [row["state"] for row in rejected] == ["completed", "dispatched", "completed"]
    links = [{key: row[key] for key in (
        "action_id", "model_call_id", "tool_name", "state", "authorization", "receipt_sha256", "rejection"
    )} for row in rejected]
    before = tree_bytes(root)
    completion = build_completion(*opened, stopped_reason=report["stopped_reason"], task_directory=task)
    facts = completion["applicability_facts"]
    assert facts["budget_rejections"] == [{"model_call_id": rejected[0]["model_call_id"], "count": 3,
        "completed_count": 2, "unknown_count": 1, "links_sha256": sha256(canonical(links)),
        "reason": "budget_exhausted", "authority": "rejected_action_records"}]
    assert facts["actions"] == facts["contexts"] == facts["reviews"] == []
    assert completion["cost"]["tool_calls"] == 4 and completion["cost"]["rejected_tool_calls"] == 3
    assert completion["cost"]["uncertain_tool_calls"] == 1
    assert completion["cost"]["reserved_calls"] == 1 and completion["cost"]["total_tokens"] == 15
    assert completion["suggested_status"] == "unresolved"
    assert completion["verification"]["accepted"] is False
    assert tree_bytes(root) == before


@pytest.mark.parametrize("field", ["count", "completed_count", "unknown_count", "links_sha256"])
def test_budget_rejection_report_summary_cannot_be_rehashed_to_hide_changed_rows(budget_rejection_run, field: str) -> None:
    root, task, work, report = budget_rejection_run
    value = copy.deepcopy(report)
    for facts in (value["applicability_facts"], value["completion"]["applicability_facts"]):
        summary = facts["budget_rejections"][0]
        summary[field] = "f" * 64 if field == "links_sha256" else summary[field] + 1
    value["completion"]["summary_sha256"] = sha256(canonical({
        key: item for key, item in value["completion"].items() if key != "summary_sha256"
    }))
    save_json(work / "agent-report.json", value)
    before = tree_bytes(root)
    with pytest.raises(ValueError):
        read_completion(*open_run(task, work), task_directory=task)
    assert tree_bytes(root) == before


def test_explicit_generation_audits_every_solver_and_keeps_single_cost(applicability_run) -> None:
    root, task, work, _library, report = applicability_run
    session = read_json(work / "session.json")
    assert (session["schema_version"], report["schema_version"], report["completion"]["schema_version"]) == (12, 9, 6)
    assert session["knowledge_profile"]["schema_version"] == 1
    profile = {"schema_version": 1, "enabled": True}
    assert report["applicability_profile"] == session["applicability_profile"] == profile
    facts = report["applicability_facts"]
    assert facts == report["completion"]["applicability_facts"]
    assert facts["profile"] == profile
    assert len(facts["solver_inputs"]) == 2
    assert facts["semantic_authority"] == "model_inference"
    assert facts["application_verification"] == "not_run"
    cost = report["completion"]["cost"]
    assert cost["reserved_calls"] == cost["calls_by_kind"]["solver"] == 2
    assert cost["tool_calls"] == 1 and cost["total_tokens"] == 30
    assert len(report["offered_tools"]) == 19
    assert report["completion"]["verification"]["accepted"] is False
    before = tree_bytes(root)
    assert read_completion(*open_run(task, work), task_directory=task) == report["completion"]
    assert tree_bytes(root) == before


@pytest.mark.parametrize("field", ["applicability_profile", "applicability_facts"])
@pytest.mark.parametrize("change", ["missing", "top", "summary"])
def test_report_cannot_hide_or_rehash_changed_applicability(applicability_run, field: str, change: str) -> None:
    _root, task, work, _library, report = applicability_run
    value = copy.deepcopy(report)
    if change == "missing":
        value.pop(field)
    elif change == "top":
        value[field] = {"invented": True}
    else:
        value["completion"][field] = {"invented": True}
        value["completion"]["summary_sha256"] = sha256(canonical({
            key: item for key, item in value["completion"].items() if key != "summary_sha256"
        }))
    save_json(work / "agent-report.json", value)
    before = tree_bytes(work)
    with pytest.raises(ValueError):
        read_completion(*open_run(task, work), task_directory=task)
    assert tree_bytes(work) == before


@pytest.mark.parametrize("change", ["missing_profile", "downgrade", "unknown_profile", "session_facts"])
def test_session_does_not_inherit_applicability_authority(applicability_run, change: str) -> None:
    _root, task, work, _library, _report = applicability_run
    value = read_json(work / "session.json")
    if change == "missing_profile":
        value.pop("applicability_profile")
    elif change == "downgrade":
        value["schema_version"] = 11
    elif change == "unknown_profile":
        value["applicability_profile"]["invented"] = True
    else:
        value["applicability_facts"] = {}
    save_json(work / "session.json", value)
    with pytest.raises(ValueError):
        read_completion(*open_run(task, work), task_directory=task)


@pytest.mark.parametrize("entry", ["solver-inputs", "applicability-contexts", "applicability-reviews", "applicability-materials"])
def test_ordinary_knowledge_run_cannot_acquire_new_namespace(tmp_path: Path, entry: str) -> None:
    root = tmp_path / "evaluation"
    task = make_task(root / "tasks/task1")
    library = corpus(tmp_path / "curated")
    info = verify_task(task)
    approval = TaskKnowledgeBinding(task_id=info["task_id"], run_id="run1",
        source_snapshot_sha256=info["source_snapshot_sha256"], crash_sha256=info["crash_sha256"],
        snapshot_sha256=library.snapshot.snapshot_sha256, scope=library.scope)
    work = root / "work/run1"
    report = run_synthetic(task, work, "run1", [response(1)], knowledge=HostKnowledgeInput(library.retriever, approval))
    assert (report["schema_version"], report["completion"]["schema_version"]) == (8, 5)
    (work / entry).mkdir()
    before = tree_bytes(work)
    with pytest.raises(ValueError, match="cannot acquire applicability"):
        build_completion(*open_run(task, work), stopped_reason=None, task_directory=task)
    raw = manifest3(root, task, work)
    (work / "agent-report.json").unlink()
    with pytest.raises(ValueError, match="cannot acquire applicability"):
        aggregate_evaluation(parse_evaluation_manifest(raw), root)
    assert tree_bytes(work) == {key: value for key, value in before.items() if key != "agent-report.json"}


@pytest.mark.parametrize("field", ["applicability_profile", "applicability_facts"])
def test_no_knowledge_report_rejects_new_fields(tmp_path: Path, field: str) -> None:
    task = make_task(tmp_path / "task")
    work = tmp_path / "run"
    report = run_synthetic(task, work, "run1", [response(1)])
    assert (report["schema_version"], report["completion"]["schema_version"]) == (7, 4)
    report[field] = None
    save_json(work / "agent-report.json", report)
    with pytest.raises(ValueError, match="cannot acquire applicability"):
        read_completion(*open_run(task, work))


def test_manifest4_changes_only_explicit_switch_on_same_knowledge_tools_and_budget(applicability_run) -> None:
    root, task, work, library, on_report = applicability_run
    approval = TaskKnowledgeBinding.model_validate_json(canonical(
        read_json(work / "session.json")["knowledge_profile"]["approval"]))
    off_work = root / "work/run2"
    off_report = run_synthetic(task, off_work, "run2", [
        response(1, "search_experience", {"tags": ["ByteWidth"], "text": "容量"}), response(2),
    ], knowledge=HostKnowledgeInput(library.retriever, approval.model_copy(update={"run_id": "run2"})),
        applicability_policy=ApplicabilityPolicy(enabled=False))
    raw = manifest4(root, task, work)
    raw["variants"][0]["variant_id"] = "on"
    raw["variants"].append({**raw["variants"][0], "variant_id": "off", "applicability_enabled": False})
    raw["runs"][0]["variant_id"] = "on"
    raw["runs"].append({**raw["runs"][0], "variant_id": "off", "run_id": "run2",
                         "work_path": off_work.relative_to(root).as_posix()})
    before = tree_bytes(root)
    aggregate = aggregate_evaluation(parse_evaluation_manifest(raw), root)
    assert aggregate["schema_version"] == 4
    assert [row["applicability_enabled"] for row in aggregate["rows"]] == [False, True]
    assert on_report["offered_tools"] == off_report["offered_tools"]
    assert on_report["knowledge_facts"]["queries"][0]["retrieval"]["hits"] == off_report["knowledge_facts"]["queries"][0]["retrieval"]["hits"]
    assert all(row["cost"]["reserved_calls"] == 2 for row in aggregate["rows"])
    assert all(value is None for value in aggregate["application_metrics"].values())
    assert tree_bytes(root) == before
    raw["variants"][0]["applicability_enabled"] = False
    with pytest.raises(ValueError, match="declared evaluation switch"):
        aggregate_evaluation(parse_evaluation_manifest(raw), root)


@pytest.mark.parametrize("generation", [1, 2, 3])
def test_old_evaluation_generations_reject_explicit_run(applicability_run, generation: int) -> None:
    root, task, work, _library, _report = applicability_run
    raw = manifest4(root, task, work)
    raw["schema_version"] = generation
    raw["variants"][0].pop("applicability_enabled")
    if generation < 3:
        raw["configuration"].pop("knowledge_profile")
    if generation == 1:
        raw["configuration"].pop("working_context")
        raw["variants"][0].pop("working_context_enabled")
    with pytest.raises(ValueError):
        aggregate_evaluation(parse_evaluation_manifest(raw), root)


@pytest.mark.parametrize("missing", [None, "model-calls.sqlite", "tool-actions.sqlite"])
def test_missing_report_audits_originals_without_inventing_cost(applicability_run, missing: str | None) -> None:
    root, task, work, _library, report = applicability_run
    raw = manifest4(root, task, work)
    (work / "agent-report.json").unlink()
    if missing is not None:
        (work / missing).unlink()
    before = tree_bytes(root)
    row = aggregate_evaluation(parse_evaluation_manifest(raw), root)["rows"][0]
    assert row["record_status"] == "missing" and row["cost"] is None
    assert row["applicability_profile"] == report["applicability_profile"] and row["applicability_facts"] is None
    assert tree_bytes(root) == before
    original = next((work / "solver-inputs").glob("*.json"))
    original.write_text("{}", encoding="utf-8")
    before = tree_bytes(root)
    with pytest.raises(ValueError):
        aggregate_evaluation(parse_evaluation_manifest(raw), root)
    assert tree_bytes(root) == before
    if missing is not None:
        assert not (work / missing).exists()


def test_missing_session_does_not_authorize_solver_originals(applicability_run) -> None:
    root, task, work, _library, _report = applicability_run
    raw = manifest4(root, task, work)
    (work / "agent-report.json").unlink()
    (work / "session.json").unlink()
    before = tree_bytes(root)
    with pytest.raises(ValueError, match="unapproved evaluation"):
        aggregate_evaluation(parse_evaluation_manifest(raw), root)
    assert tree_bytes(root) == before


def test_missing_report_rebuilds_host_tool_definitions_instead_of_trusting_declared_hash(applicability_run) -> None:
    root, task, work, _library, _report = applicability_run
    raw = manifest4(root, task, work)
    (work / "agent-report.json").unlink()
    session = read_json(work / "session.json")
    session["tool_schema_sha256"] = "f" * 64
    raw["configuration"]["tool_schema_sha256"] = session["tool_schema_sha256"]
    save_json(work / "session.json", session)
    before = tree_bytes(root)
    with pytest.raises(ValueError, match="installed tool definitions"):
        aggregate_evaluation(parse_evaluation_manifest(raw), root)
    assert tree_bytes(root) == before


def test_export_keeps_solver_originals_after_source_paths_disappear(applicability_run, tmp_path: Path) -> None:
    root, task, work, library, _report = applicability_run
    inputs = managed_inputs(root, manifest4(root, task, work))
    before = tree_bytes(work)
    plan = inputs.freeze()
    receipt = inputs.apply(plan)
    assert tree_bytes(work) == before
    package = Path(receipt["package_path"])
    target = tmp_path / "relocated-package"
    shutil.move(str(package), target)
    library.root.rename(library.root.with_name("source-library-unavailable"))
    root.rename(root.with_name("source-evaluation-unavailable"))
    target = io_path(target)
    before = tree_bytes(target)
    result = exporting.verify_export(target)
    assert result["aggregate_sha256"] == receipt["aggregate_sha256"]
    assert (target / "payload/work/run1/solver-inputs").is_dir()
    assert tree_bytes(target) == before


def test_complete_sdk_context_and_review_reconstruct_from_relocated_private_package(
        tmp_path: Path, tmp_path_factory, monkeypatch) -> None:
    root = tmp_path_factory.getbasetemp() / "portable-tri"
    root.mkdir()
    s, work, report, _sent, _script = run_applicability_sdk(root, monkeypatch)
    # The shared helper leaves the run stopped; export needs an explicit host
    # terminal marker, never a model statement or fabricated application pass.
    terminal(work, "run1")
    raw = manifest4(root, s.task, work)
    before = tree_bytes(work)
    inputs = managed_inputs(root, raw)
    receipt = inputs.apply(inputs.freeze())
    assert tree_bytes(work) == before
    target = tmp_path / "relocated"
    shutil.move(str(receipt["package_path"]), target)
    root.rename(root.with_name("portable-tri-unavailable"))
    target = io_path(target)
    before = tree_bytes(target)
    verified = exporting.verify_export(target)
    assert verified["aggregate_sha256"] == receipt["aggregate_sha256"]
    row = read_json(target / "aggregate-report.json")["rows"][0]
    assert row["applicability_facts"] == report["completion"]["applicability_facts"]
    assert len(list((target / "payload/run/applicability-contexts").glob("*.json"))) == 1
    assert len(list((target / "payload/run/applicability-reviews").glob("*.json"))) == 1
    assert tree_bytes(target) == before
