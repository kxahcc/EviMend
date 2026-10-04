"""Run knowledge is explicit, cost-accounted and portable without its source library."""

from __future__ import annotations

import copy
import os
import shutil
from pathlib import Path

import pytest
from test_agent_loop import response
from test_exporting import io_path, managed_inputs
from test_knowledge_retrieval import corpus
from test_metrics import make_task, one_manifest, read_json, run_synthetic, save_json
from test_readonly_evidence import tree_bytes

from dfx_agent import exporting
from dfx_agent.actions import ToolActionStore
from dfx_agent.applicability_contracts import TaskKnowledgeBinding
from dfx_agent.knowledge_run import HostKnowledgeInput, shared_knowledge_profile
from dfx_agent.metrics import (
    aggregate_evaluation,
    parse_evaluation_manifest,
    shared_context_profile,
)
from dfx_agent.model_calls import ModelCallStore, canonical, sha256
from dfx_agent.reporting import build_completion, read_completion
from dfx_agent.task import verify_task
from dfx_agent.verification import CheckLedger, ReplayOnlyExecutor
from dfx_agent.workspace import CandidateWorkspace


def open_run(task: Path, work: Path):
    session = read_json(work / "session.json")
    workspace = CandidateWorkspace.reopen(task, work, session["run_id"])
    ledger = CheckLedger(workspace, ReplayOnlyExecutor(session["protocol_sha256"]), read_only=True)
    return workspace, ledger, ModelCallStore(work / "model-calls.sqlite", read_only=True), ToolActionStore(
        work / "tool-actions.sqlite", read_only=True)


@pytest.fixture
def knowledge_run(tmp_path: Path):
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
    ], knowledge=HostKnowledgeInput(library.retriever, approval))
    return root, task, work, library, report


def manifest3(root: Path, task: Path, work: Path) -> dict:
    session = read_json(work / "session.json")
    value = one_manifest(root, task, work, session["run_id"])
    value["schema_version"] = 3
    value["configuration"]["context_profile_sha256"] = sha256(canonical(shared_context_profile(
        session["context_profile"])))
    value["configuration"]["working_context"] = {key: item for key, item in
        session["context_profile"]["host_working_context"].items() if key != "enabled"}
    value["configuration"]["knowledge_profile"] = shared_knowledge_profile(session["knowledge_profile"])
    value["variants"][0]["working_context_enabled"] = False
    return value


def test_knowledge_generation_recomputes_full_queries_and_single_accounted_cost(knowledge_run) -> None:
    root, task, work, _library, report = knowledge_run
    session = read_json(work / "session.json")
    assert (session["schema_version"], report["schema_version"], report["completion"]["schema_version"]) == (11, 8, 5)
    facts = report["knowledge_facts"]
    assert facts == report["completion"]["knowledge_facts"]
    assert facts["profile"] == report["knowledge_profile"] == session["knowledge_profile"]
    assert len(facts["queries"]) == 1 and facts["queries"][0]["actions"]
    assert facts["queries"][0]["retrieval"]["hits"][0]["projection"]["guidance"]["premises"]
    assert facts["relevance_authority"] == "relevance_not_applicability"
    assert facts["application_verification"] == "not_run"
    cost = report["completion"]["cost"]
    assert cost["reserved_calls"] == cost["calls_by_kind"]["solver"] == 2
    assert cost["tool_calls"] == 1 and cost["total_tokens"] == 30
    assert report["completion"]["verification"]["accepted"] is False
    before = tree_bytes(root)
    assert read_completion(*open_run(task, work), task_directory=task) == report["completion"]
    assert tree_bytes(root) == before


def test_knowledge_readback_needs_explicit_external_task_directory(knowledge_run) -> None:
    _root, task, work, _library, _report = knowledge_run
    with pytest.raises(ValueError, match="explicit task directory"):
        read_completion(*open_run(task, work))


@pytest.mark.parametrize("field", ["knowledge_profile", "knowledge_facts"])
@pytest.mark.parametrize("change", ["missing", "top", "summary"])
def test_report_cannot_hide_or_rehash_changed_knowledge_facts(knowledge_run, field: str, change: str) -> None:
    _root, task, work, _library, report = knowledge_run
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
    with pytest.raises(ValueError):
        read_completion(*open_run(task, work), task_directory=task)


@pytest.mark.parametrize("change", ["missing_profile", "downgrade", "snapshot", "index"])
def test_session_and_frozen_run_knowledge_must_match(knowledge_run, change: str) -> None:
    _root, task, work, _library, _report = knowledge_run
    session = read_json(work / "session.json")
    if change == "missing_profile":
        session.pop("knowledge_profile")
    elif change == "downgrade":
        session["schema_version"] = 10
    else:
        target = work / next(file["path"] for file in session["knowledge_profile"]["files"]
            if file["path"].startswith("knowledge/" + ("snapshots/" if change == "snapshot" else "indexes/")))
        before = target.stat()
        raw = target.read_bytes()
        target.write_bytes(bytes([raw[0] ^ 1]) + raw[1:])
        os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns))
    if change in ("missing_profile", "downgrade"):
        save_json(work / "session.json", session)
    with pytest.raises(ValueError):
        read_completion(*open_run(task, work), task_directory=task)


@pytest.mark.parametrize("field", ["knowledge_profile", "knowledge_facts"])
def test_no_knowledge_reports_keep_generation_and_reject_new_field(tmp_path: Path, field: str) -> None:
    task = make_task(tmp_path / "task")
    work = tmp_path / "run"
    report = run_synthetic(task, work, "run1", [response(1)])
    assert (read_json(work / "session.json")["schema_version"], report["schema_version"],
            report["completion"]["schema_version"]) == (10, 7, 4)
    assert "knowledge_profile" not in report and "knowledge_facts" not in report
    report[field] = None
    save_json(work / "agent-report.json", report)
    with pytest.raises(ValueError, match="cannot acquire knowledge"):
        read_completion(*open_run(task, work))


def test_no_knowledge_session_cannot_acquire_run_knowledge_directory(tmp_path: Path) -> None:
    task = make_task(tmp_path / "task")
    work = tmp_path / "run"
    run_synthetic(task, work, "run1", [response(1)])
    (work / "knowledge").mkdir()
    with pytest.raises(ValueError, match="cannot acquire knowledge"):
        build_completion(*open_run(task, work), stopped_reason=None)


def test_manifest3_uses_common_frozen_knowledge_without_new_variant_factor(knowledge_run) -> None:
    root, task, work, _library, report = knowledge_run
    raw = manifest3(root, task, work)
    before = tree_bytes(root)
    aggregate = aggregate_evaluation(parse_evaluation_manifest(raw), root)
    assert aggregate["schema_version"] == 3
    assert aggregate["rows"][0]["knowledge_facts"] == report["knowledge_facts"]
    assert aggregate["variants"][0]["cost"]["reserved_calls"] == 2
    assert aggregate["configuration"]["knowledge_profile"] == shared_knowledge_profile(report["knowledge_profile"])
    assert all(value is None for value in aggregate["application_metrics"].values())
    assert tree_bytes(root) == before


@pytest.mark.parametrize("generation", [1, 2])
def test_old_evaluation_generation_cannot_accept_knowledge_run(knowledge_run, generation: int) -> None:
    root, task, work, _library, _report = knowledge_run
    raw = manifest3(root, task, work)
    raw["schema_version"] = generation
    raw["configuration"].pop("knowledge_profile")
    if generation == 1:
        raw["configuration"].pop("working_context")
        raw["variants"][0].pop("working_context_enabled")
    with pytest.raises(ValueError):
        aggregate_evaluation(parse_evaluation_manifest(raw), root)


@pytest.mark.parametrize("field", ["snapshot_sha256", "index_sha256", "scope", "retriever", "files"])
def test_manifest3_does_not_relax_knowledge_configuration_identity(knowledge_run, field: str) -> None:
    root, task, work, _library, _report = knowledge_run
    raw = manifest3(root, task, work)
    profile = raw["configuration"]["knowledge_profile"]
    if field in ("snapshot_sha256", "index_sha256"):
        profile[field] = "f" * 64
    elif field == "scope":
        profile[field]["excluded_lineage_groups"] = ["different-lineage"]
    elif field == "retriever":
        profile[field]["tag_weight"] += 1
    else:
        profile[field][0]["sha256"] = "e" * 64
    with pytest.raises(ValueError):
        aggregate_evaluation(parse_evaluation_manifest(raw), root)


def test_missing_report_still_authenticates_saved_knowledge_and_keeps_unknown_cost(knowledge_run) -> None:
    root, task, work, _library, report = knowledge_run
    raw = manifest3(root, task, work)
    (work / "agent-report.json").unlink()
    before = tree_bytes(root)
    row = aggregate_evaluation(parse_evaluation_manifest(raw), root)["rows"][0]
    assert row["record_status"] == "missing" and row["cost"] is None
    assert row["knowledge_profile"] == report["knowledge_profile"] and row["knowledge_facts"] is None
    assert tree_bytes(root) == before
    index = work / next(file["path"] for file in report["knowledge_profile"]["files"]
        if file["path"].endswith(".sqlite"))
    index.unlink()
    with pytest.raises(ValueError):
        aggregate_evaluation(parse_evaluation_manifest(raw), root)


def test_missing_report_does_not_ignore_corrupted_query_evidence(knowledge_run) -> None:
    root, task, work, _library, _report = knowledge_run
    raw = manifest3(root, task, work)
    (work / "agent-report.json").unlink()
    query_path = next((work / "experience-queries").iterdir())
    saved = read_json(query_path)
    saved["content_sha256"] = "f" * 64
    save_json(query_path, saved)
    with pytest.raises(ValueError):
        aggregate_evaluation(parse_evaluation_manifest(raw), root)


@pytest.mark.parametrize("missing", ["model-calls.sqlite", "tool-actions.sqlite"])
def test_missing_ledger_preserves_unknown_cost_and_still_authenticates_query_originals(knowledge_run, missing: str) -> None:
    root, task, work, _library, _report = knowledge_run
    raw = manifest3(root, task, work)
    (work / "agent-report.json").unlink()
    (work / missing).unlink()
    before = tree_bytes(root)
    row = aggregate_evaluation(parse_evaluation_manifest(raw), root)["rows"][0]
    assert row["record_status"] == "missing" and row["cost"] is None and row["knowledge_facts"] is None
    assert not (work / missing).exists() and tree_bytes(root) == before
    query_path = next((work / "experience-queries").iterdir())
    saved = read_json(query_path)
    saved["content_sha256"] = "f" * 64
    save_json(query_path, saved)
    before = tree_bytes(root)
    with pytest.raises(ValueError):
        aggregate_evaluation(parse_evaluation_manifest(raw), root)
    assert not (work / missing).exists() and tree_bytes(root) == before


@pytest.mark.parametrize("generation", [1, 2])
@pytest.mark.parametrize("session_schema", [None, 8])
@pytest.mark.parametrize("entry", ["knowledge-profile.json", "knowledge", "experience-bindings", "experience-queries",
                                   "applicability-contexts", "applicability-reviews", "applicability-materials"])
def test_legacy_missing_report_rejects_knowledge_roots_even_without_current_session(
    tmp_path: Path, generation: int, session_schema: int | None, entry: str,
) -> None:
    task = make_task(tmp_path / "tasks/task1")
    work = tmp_path / "work/run1"
    run_synthetic(task, work, "run1", [response(1)])
    session = read_json(work / "session.json")
    raw = one_manifest(tmp_path, task, work, "run1")
    if generation == 2:
        raw["schema_version"] = 2
        raw["configuration"]["context_profile_sha256"] = sha256(canonical(shared_context_profile(
            session["context_profile"])))
        raw["configuration"]["working_context"] = {key: value for key, value in
            session["context_profile"]["host_working_context"].items() if key != "enabled"}
        raw["variants"][0]["working_context_enabled"] = False
    (work / "agent-report.json").unlink()
    if session_schema is None:
        (work / "session.json").unlink()
    else:
        session["schema_version"] = session_schema
        session.pop("execution_provenance")
        for name in ("host_working_context", "working_context_prompt_sha256", "working_context_format"):
            session["context_profile"].pop(name, None)
        if generation == 1:
            raw["configuration"]["context_profile_sha256"] = sha256(canonical(session["context_profile"]))
        save_json(work / "session.json", session)
    if entry.endswith(".json"):
        save_json(work / entry, {"unapproved": True})
    else:
        (work / entry).mkdir()
    before = tree_bytes(work)
    with pytest.raises(ValueError, match="cannot acquire knowledge"):
        aggregate_evaluation(parse_evaluation_manifest(raw), tmp_path)
    inputs = managed_inputs(tmp_path, raw)
    with pytest.raises(ValueError):
        inputs.preview()
    assert tree_bytes(work) == before


def test_manifest3_without_session_keeps_local_knowledge_profile_unapproved(knowledge_run) -> None:
    root, task, work, _library, _report = knowledge_run
    raw = manifest3(root, task, work)
    (work / "agent-report.json").unlink()
    (work / "session.json").unlink()
    before = tree_bytes(root)
    row = aggregate_evaluation(parse_evaluation_manifest(raw), root)["rows"][0]
    assert row["record_status"] == "missing" and row["cost"] is None
    assert row["knowledge_profile"] is None and row["knowledge_facts"] is None
    assert row["synthetic_outcome"] == "unknown" and row["verification"]["accepted"] is False
    assert tree_bytes(root) == before


def test_export_copies_knowledge_and_verifies_after_original_paths_disappear(knowledge_run, tmp_path: Path) -> None:
    root, task, work, library, _report = knowledge_run
    inputs = managed_inputs(root, manifest3(root, task, work))
    before = tree_bytes(work)
    plan = inputs.freeze()
    receipt = inputs.apply(plan)
    assert tree_bytes(work) == before
    package = Path(receipt["package_path"])
    target = tmp_path / "relocated-package"
    shutil.move(str(package), target)
    library.root.rename(library.root.with_name("original-library-unavailable"))
    root.rename(root.with_name("original-evaluation-unavailable"))
    # Keep fixture assertions on the same Windows long-path API used by Verify.
    target = io_path(target)
    before = tree_bytes(target)
    result = exporting.verify_export(target)
    assert result["aggregate_sha256"] == receipt["aggregate_sha256"]
    assert (target / "payload/work/run1/knowledge").is_dir()
    assert tree_bytes(target) == before
