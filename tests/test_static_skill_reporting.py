"""Static procedures remain accounted, comparable, and independently portable.

Only host-curated synthetic fixtures are used. Loading or reading a procedure
never establishes current applicability, a learned Skill, or application repair.
"""

from __future__ import annotations

import copy
import json
import os
import shutil
from pathlib import Path

import pytest
from test_agent_loop import response
from test_applicability_reporting import manifest4
from test_exporting import io_path, managed_inputs
from test_knowledge_reporting import open_run
from test_knowledge_retrieval import corpus
from test_metrics import make_task, one_manifest, read_json, run_synthetic, save_json
from test_readonly_evidence import tree_bytes

from dfx_agent import exporting, metrics, static_skill_handoff
from dfx_agent.applicability_contracts import TaskKnowledgeBinding
from dfx_agent.applicability_handoff import ApplicabilityPolicy
from dfx_agent.knowledge_contracts import content_hash
from dfx_agent.knowledge_run import HostKnowledgeInput
from dfx_agent.metrics import aggregate_evaluation, parse_evaluation_manifest
from dfx_agent.model_calls import canonical, sha256
from dfx_agent.reporting import build_completion, read_completion
from dfx_agent.static_skills import StaticSkillPolicy, shared_static_skill_profile
from dfx_agent.task import verify_task


def manifest5(root: Path, task: Path, work: Path) -> dict:
    value = manifest4(root, task, work)
    profile = read_json(work / "session.json")["static_skill_profile"]
    value["schema_version"] = 5
    value["configuration"]["shared_static_skill_profile"] = shared_static_skill_profile(profile)
    value["variants"][0]["static_skills_enabled"] = profile["enabled"]
    return value


def serialized_size(value: object) -> int:
    return len(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))


@pytest.fixture
def static_run(tmp_path_factory: pytest.TempPathFactory):
    # Publication adds a content identity to this path. A short fixture root
    # does not purport to verify the separate Windows long-path requirement.
    root = tmp_path_factory.mktemp("ssr")
    task = make_task(root / "t")
    library = corpus(root / "lib")
    info = verify_task(task)
    approval = TaskKnowledgeBinding(task_id=info["task_id"], run_id="r1",
        source_snapshot_sha256=info["source_snapshot_sha256"], crash_sha256=info["crash_sha256"],
        snapshot_sha256=library.snapshot.snapshot_sha256, scope=library.scope)
    entry = library.entries["offset"]  # width is explicitly model-derived.
    projection = library.store.read_projection(library.snapshot.snapshot_sha256, entry.entry_id)
    skill_id = "dfx-" + content_hash(projection)[:56]
    policy = StaticSkillPolicy(enabled=True, entry_ids=(entry.entry_id,))
    work = root / "r1"
    report = run_synthetic(task, work, "r1", [
        response(1, "read_skill", {"skill_id": skill_id}), response(2),
    ], knowledge=HostKnowledgeInput(library.retriever, approval),
        applicability_policy=ApplicabilityPolicy(enabled=True), static_skill_policy=policy)
    return root, task, work, library, approval, policy, skill_id, report


def test_new_generation_preserves_authenticated_read_and_all_cost(static_run) -> None:
    root, task, work, _library, _approval, _policy, skill_id, report = static_run
    session = read_json(work / "session.json")
    assert (session["schema_version"], report["schema_version"], report["completion"]["schema_version"]) == (13, 10, 7)
    facts = report["static_skill_facts"]
    assert facts == report["completion"]["static_skill_facts"]
    assert facts["profile"] == report["static_skill_profile"] == session["static_skill_profile"]
    assert len(facts["solver_inputs"]) == 2 and len(facts["actions"]) == 1
    assert facts["unread_skill_ids"] == []
    assert facts["derivation_authority"] == "host_curated"
    assert facts["epistemic_status"] == "historical_guidance_not_current_evidence"
    assert facts["application_verification"] == "not_run"
    assert len(report["offered_tools"]) == 20
    opened = open_run(task, work)
    action = next(row for row in opened[3].records() if row["tool_name"] == "read_skill")
    receipt = read_json(work / "static-skills-profile.json")["files"][0]
    content = opened[3].receipt(action["action_id"]).content
    assert isinstance(content, str)
    payload = json.loads(content)
    assert payload["skill_id"] == skill_id
    assert payload["body"] == (work / receipt["path"]).read_text(encoding="utf-8")
    cost = report["completion"]["cost"]
    assert cost["reserved_calls"] == cost["calls_by_kind"]["solver"] == 2
    assert cost["tool_calls"] == 1 and cost["total_tokens"] == 30
    assert report["completion"]["verification"]["accepted"] is False
    before = tree_bytes(root)
    assert read_completion(*opened, task_directory=task) == report["completion"]
    assert tree_bytes(root) == before


@pytest.mark.parametrize("field", ["static_skill_profile", "static_skill_facts"])
@pytest.mark.parametrize("change", ["missing", "top", "summary"])
def test_report_does_not_trust_forged_or_rehashed_static_facts(static_run, field: str, change: str) -> None:
    _root, task, work, _library, _approval, _policy, _skill_id, report = static_run
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


@pytest.mark.parametrize("change", ["missing_profile", "downgrade", "unknown_profile", "session_facts", "enabled"])
def test_session_cannot_grant_itself_changed_resources_or_static_authority(static_run, change: str) -> None:
    _root, task, work, _library, _approval, _policy, _skill_id, _report = static_run
    session = read_json(work / "session.json")
    if change == "missing_profile":
        session.pop("static_skill_profile")
    elif change == "downgrade":
        session["schema_version"] = 12
    elif change == "unknown_profile":
        session["static_skill_profile"]["invented"] = True
    elif change == "session_facts":
        session["static_skill_facts"] = {}
    else:
        session["static_skill_profile"]["enabled"] = False
    save_json(work / "session.json", session)
    before = tree_bytes(work)
    with pytest.raises(ValueError):
        read_completion(*open_run(task, work), task_directory=task)
    assert tree_bytes(work) == before


def test_same_length_body_change_with_restored_mtime_is_not_accepted(static_run) -> None:
    _root, task, work, _library, _approval, _policy, _skill_id, report = static_run
    body = work / report["static_skill_profile"]["files"][0]["path"]
    metadata = body.stat()
    raw = body.read_bytes()
    assert b"Preserve" in raw
    body.write_bytes(raw.replace(b"Preserve", b"Discard", 1))
    os.utime(body, ns=(metadata.st_atime_ns, metadata.st_mtime_ns))
    before = tree_bytes(work)
    with pytest.raises(ValueError):
        read_completion(*open_run(task, work), task_directory=task)
    assert tree_bytes(work) == before


def test_final_capacity_gate_measures_whole_double_report_fragment(static_run, monkeypatch) -> None:
    _root, task, work, _library, _approval, _policy, _skill_id, _report = static_run
    monkeypatch.setattr(static_skill_handoff, "MAX_STATIC_SKILL_REPORT_BYTES", 1)
    before = tree_bytes(work)
    with pytest.raises(ValueError, match="report fragments exceed capacity"):
        build_completion(*open_run(task, work), task_directory=task, stopped_reason=None)
    assert tree_bytes(work) == before


@pytest.mark.parametrize("field", ["static_skill_profile", "static_skill_facts"])
def test_old_report_cannot_acquire_static_fields(tmp_path_factory: pytest.TempPathFactory, field: str) -> None:
    root = tmp_path_factory.mktemp("ssl")
    task = make_task(root / "t")
    work = root / "r"
    report = run_synthetic(task, work, "r", [response(1)])
    assert (report["schema_version"], report["completion"]["schema_version"]) == (7, 4)
    report[field] = None
    save_json(work / "agent-report.json", report)
    with pytest.raises(ValueError, match="cannot acquire static Skill facts"):
        read_completion(*open_run(task, work))


@pytest.mark.parametrize("entry", ["static-skills", "static-skills-profile.json"])
def test_old_run_rejects_static_namespace_even_without_report(tmp_path_factory: pytest.TempPathFactory, entry: str) -> None:
    root = tmp_path_factory.mktemp("ssn")
    task = make_task(root / "t")
    work = root / "r"
    run_synthetic(task, work, "r", [response(1)])
    raw = one_manifest(root, task, work, "r")
    if entry.endswith(".json"):
        save_json(work / entry, {})
    else:
        (work / entry).mkdir()
    with pytest.raises(ValueError, match="cannot acquire static Skill evidence"):
        build_completion(*open_run(task, work), stopped_reason=None)
    (work / "agent-report.json").unlink()
    before = tree_bytes(work)
    with pytest.raises(ValueError, match="cannot acquire static Skill evidence"):
        aggregate_evaluation(parse_evaluation_manifest(raw), root)
    assert tree_bytes(work) == before


def test_manifest5_catalog_switch_keeps_same_full_bodies_tools_and_budget(static_run) -> None:
    root, task, work, library, approval, policy, skill_id, on_report = static_run
    off_work = root / "r2"
    off = run_synthetic(task, off_work, "r2", [response(1, "read_skill", {"skill_id": skill_id}), response(2)],
        knowledge=HostKnowledgeInput(library.retriever, approval.model_copy(update={"run_id": "r2"})),
        applicability_policy=ApplicabilityPolicy(enabled=True), static_skill_policy=policy.model_copy(update={"enabled": False}))
    raw = manifest5(root, task, work)
    raw["variants"][0]["variant_id"] = "on"
    raw["variants"].append({**raw["variants"][0], "variant_id": "off", "static_skills_enabled": False})
    raw["runs"][0]["variant_id"] = "on"
    raw["runs"].append({**raw["runs"][0], "variant_id": "off", "run_id": "r2", "work_path": "r2"})
    before = tree_bytes(root)
    aggregate = aggregate_evaluation(parse_evaluation_manifest(raw), root)
    assert aggregate["schema_version"] == 5
    assert [row["static_skills_enabled"] for row in aggregate["rows"]] == [False, True]
    assert on_report["offered_tools"] == off["offered_tools"]
    assert shared_static_skill_profile(on_report["static_skill_profile"]) == shared_static_skill_profile(off["static_skill_profile"])
    assert all(row["cost"]["reserved_calls"] == 2 and row["cost"]["tool_calls"] == 1 for row in aggregate["rows"])
    assert all(value is None for value in aggregate["application_metrics"].values())
    assert tree_bytes(root) == before
    raw["variants"][0]["static_skills_enabled"] = False
    with pytest.raises(ValueError, match="declared evaluation switch"):
        aggregate_evaluation(parse_evaluation_manifest(raw), root)


@pytest.mark.parametrize("generation", [1, 2, 3, 4])
def test_previous_manifests_reject_static_run(static_run, generation: int) -> None:
    root, task, work, _library, _approval, _policy, _skill_id, _report = static_run
    raw = manifest5(root, task, work)
    raw["schema_version"] = generation
    raw["configuration"].pop("shared_static_skill_profile")
    raw["variants"][0].pop("static_skills_enabled")
    if generation < 4:
        raw["variants"][0].pop("applicability_enabled")
    if generation < 3:
        raw["configuration"].pop("knowledge_profile")
    if generation < 2:
        raw["configuration"].pop("working_context")
        raw["variants"][0].pop("working_context_enabled")
    with pytest.raises(ValueError):
        aggregate_evaluation(parse_evaluation_manifest(raw), root)


@pytest.mark.parametrize("missing", [None, "model-calls.sqlite", "tool-actions.sqlite"])
def test_missing_report_audits_static_originals_but_does_not_invent_use_or_cost(static_run, missing: str | None) -> None:
    root, task, work, _library, _approval, _policy, _skill_id, report = static_run
    raw = manifest5(root, task, work)
    (work / "agent-report.json").unlink()
    if missing is not None:
        (work / missing).unlink()
    before = tree_bytes(root)
    row = aggregate_evaluation(parse_evaluation_manifest(raw), root)["rows"][0]
    assert row["record_status"] == "missing" and row["cost"] is None
    assert row["static_skill_profile"] == report["static_skill_profile"] and row["static_skill_facts"] is None
    assert tree_bytes(root) == before
    (work / report["static_skill_profile"]["files"][0]["path"]).write_bytes(b"forged")
    before = tree_bytes(root)
    with pytest.raises(ValueError):
        aggregate_evaluation(parse_evaluation_manifest(raw), root)
    assert tree_bytes(root) == before
    if missing is not None:
        assert not (work / missing).exists()


def test_missing_session_is_not_authority_for_saved_static_originals(static_run) -> None:
    root, task, work, _library, _approval, _policy, _skill_id, _report = static_run
    raw = manifest5(root, task, work)
    (work / "agent-report.json").unlink()
    (work / "session.json").unlink()
    before = tree_bytes(root)
    with pytest.raises(ValueError, match="unapproved evaluation"):
        aggregate_evaluation(parse_evaluation_manifest(raw), root)
    assert tree_bytes(root) == before


def test_missing_report_reconstructs_real_tool_definitions_instead_of_declared_schema(static_run) -> None:
    root, task, work, _library, _approval, _policy, _skill_id, _report = static_run
    raw = manifest5(root, task, work)
    (work / "agent-report.json").unlink()
    session = read_json(work / "session.json")
    session["tool_schema_sha256"] = "f" * 64
    raw["configuration"]["tool_schema_sha256"] = session["tool_schema_sha256"]
    save_json(work / "session.json", session)
    before = tree_bytes(root)
    with pytest.raises(ValueError, match="installed tool definitions"):
        aggregate_evaluation(parse_evaluation_manifest(raw), root)
    assert tree_bytes(root) == before


def test_not_run_cell_retains_unknown_static_use_and_cost_in_matrix(static_run) -> None:
    root, task, work, _library, _approval, _policy, _skill_id, _report = static_run
    raw = manifest5(root, task, work)
    raw["repeat_ids"].append("repeat2")
    raw["runs"].append({**raw["runs"][0], "repeat_id": "repeat2", "run_id": "nr", "work_path": None})
    aggregate = aggregate_evaluation(parse_evaluation_manifest(raw), root)
    row = next(row for row in aggregate["rows"] if row["repeat_id"] == "repeat2")
    assert row["record_status"] == "not_run" and row["static_skill_profile"] is None
    assert row["static_skill_facts"] is None and row["cost"] is None
    assert aggregate["variants"][0]["expected_runs"] == 2
    assert aggregate["variants"][0]["cost"]["unknown_cost_rows"] == 1


def test_matrix_stops_at_cumulative_utf8_row_limit_before_later_cells(static_run, monkeypatch) -> None:
    root, task, work, _library, _approval, _policy, _skill_id, _report = static_run
    raw = manifest5(root, task, work)
    single = aggregate_evaluation(parse_evaluation_manifest(raw), root)
    raw["repeat_ids"] = [f"repeat{index}" for index in range(1, 9)]
    raw["runs"] += [{**raw["runs"][0], "repeat_id": f"repeat{index}", "run_id": f"nr{index}", "work_path": None}
                    for index in range(2, 9)]
    manifest = parse_evaluation_manifest(raw)
    # Explicit synthetic row extension isolates UTF-8 accounting. The original
    # scenario's host metadata is ASCII, so it cannot establish this boundary.
    unicode_note = {"capacity_probe": "中文" * 16}
    first = {**single["rows"][0], **unicode_note}
    second = {**metrics._row(manifest.tasks[0], manifest.runs[1], manifest.variants[0],
                           manifest.configuration, root), **unicode_note}
    envelope = {**single, "expected_runs": len(manifest.runs), "rows": [], "variants": [],
                "paired_comparisons": [], "execution_provenance": None, "report_sha256": "0" * 64}
    # The limit is one byte below two actual UTF-8 rows plus their separator.
    # The explicit Chinese probe would fit if incorrectly measured as
    # the Python string length instead of bytes in the actual writer format.
    limit = serialized_size(envelope) + 1 + serialized_size(first) + serialized_size(second)
    character_size = len(canonical(envelope)) + len(canonical(first)) + len(canonical(second)) + 2
    assert character_size < limit
    seen = []
    original = metrics._row

    def observed_row(task, cell, variant, configuration, root):
        seen.append(cell.repeat_id)
        return {**original(task, cell, variant, configuration, root), **unicode_note}

    monkeypatch.setattr(metrics, "_row", observed_row)
    monkeypatch.setattr(metrics, "MAX_JSON_BYTES", limit)
    before = tree_bytes(root)
    with pytest.raises(ValueError, match="aggregate report exceeds format limit"):
        aggregate_evaluation(manifest, root)
    assert seen == ["repeat1", "repeat2"]
    assert tree_bytes(root) == before


def test_aggregate_capacity_includes_digest_and_newline_at_exact_boundary(static_run, monkeypatch) -> None:
    root, task, work, _library, _approval, _policy, _skill_id, _report = static_run
    manifest = parse_evaluation_manifest(manifest5(root, task, work))
    original = aggregate_evaluation(manifest, root)
    size = serialized_size(original) + 1
    before = tree_bytes(root)
    monkeypatch.setattr(metrics, "MAX_JSON_BYTES", size)
    assert aggregate_evaluation(manifest, root) == original
    monkeypatch.setattr(metrics, "MAX_JSON_BYTES", size - 1)
    with pytest.raises(ValueError, match="aggregate report exceeds format limit"):
        aggregate_evaluation(manifest, root)
    assert tree_bytes(root) == before


def test_large_variant_matrix_stops_pair_generation_before_unbounded_accumulation(static_run, monkeypatch) -> None:
    root, task, work, _library, _approval, _policy, _skill_id, _report = static_run
    raw = manifest5(root, task, work)
    raw["variants"] += [{**raw["variants"][0], "variant_id": f"v{index}"} for index in range(1, 13)]
    raw["runs"] += [{**raw["runs"][0], "variant_id": f"v{index}", "run_id": f"nr{index}", "work_path": None}
                    for index in range(1, 13)]
    manifest = parse_evaluation_manifest(raw)
    original = aggregate_evaluation(manifest, root)
    assert len(original["paired_comparisons"]) == 78
    cut = {**original, "paired_comparisons": original["paired_comparisons"][:2]}
    limit = serialized_size(cut)
    observed = []
    append = metrics._AggregateCapacity.append

    def watched_append(capacity, collection, item):
        if "base_variant_id" in item:
            observed.append((item["base_variant_id"], item["compared_variant_id"]))
        return append(capacity, collection, item)

    monkeypatch.setattr(metrics._AggregateCapacity, "append", watched_append)
    monkeypatch.setattr(metrics, "MAX_JSON_BYTES", limit)
    before = tree_bytes(root)
    with pytest.raises(ValueError, match="aggregate report exceeds format limit"):
        aggregate_evaluation(manifest, root)
    assert observed == [(pair["base_variant_id"], pair["compared_variant_id"]) for pair in original["paired_comparisons"][:2]]
    assert tree_bytes(root) == before


def test_private_package_keeps_complete_resources_after_source_and_library_move(static_run, tmp_path_factory) -> None:
    root, task, work, library, _approval, _policy, _skill_id, report = static_run
    inputs = managed_inputs(root, manifest5(root, task, work))
    before = tree_bytes(work)
    receipt = inputs.apply(inputs.freeze())
    assert tree_bytes(work) == before
    destination = tmp_path_factory.mktemp("ssp") / "m"
    shutil.move(str(receipt["package_path"]), destination)
    library.root.rename(library.root.with_name("gone"))
    root.rename(root.with_name(root.name + "-gone"))
    destination = io_path(destination)
    before = tree_bytes(destination)
    verified = exporting.verify_export(destination)
    assert verified["aggregate_sha256"] == receipt["aggregate_sha256"]
    row = read_json(destination / "aggregate-report.json")["rows"][0]
    assert row["static_skill_facts"] == report["static_skill_facts"]
    assert (destination / "payload/r1/static-skills-profile.json").is_file()
    body = report["static_skill_profile"]["files"][0]["path"]
    assert (destination / "payload/r1" / body).is_file()
    assert tree_bytes(destination) == before
