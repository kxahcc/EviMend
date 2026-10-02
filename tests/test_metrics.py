"""Offline evaluation keeps frozen cells, failed history and unknown costs visible."""

from __future__ import annotations

import copy
import json
import os
import shutil
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessage
from test_agent_loop import candidate_id, response, task_snapshot
from test_compaction_loop import SUMMARY, large_task, policy

from dfx_agent import metrics
from dfx_agent.agent import run_fixed_agent
from dfx_agent.metrics import (
    MAX_CELLS,
    MAX_MANIFEST_BYTES,
    EvaluationManifest,
    aggregate_evaluation,
    load_evaluation_manifest,
    write_evaluation_report,
)
from dfx_agent.model_calls import canonical, sha256
from dfx_agent.policies import PolicyFeatures
from dfx_agent.task import CaseSelection, digest, snapshot_digest
from dfx_agent.verification import FixedTextExecutor

BASE_FEATURES = PolicyFeatures().model_dump()
ENHANCED_FEATURES = PolicyFeatures(rule_guidance=True).model_dump()
LOCATION = {"path": "src/main.ets", "line": 1, "reason": "synthetic upstream bad value"}
GOOD_TEXT = "value=good\nother=stable\n"


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def save_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def validated(value: dict[str, Any]) -> EvaluationManifest:
    # JSON is the strict model's public representation, including tuple fields.
    return EvaluationManifest.model_validate_json(json.dumps(value))


def make_task(root: Path, task_id: str = "task1") -> Path:
    task = task_snapshot(root)
    source = b"value=bad\nother=stable\n"
    (task / "source/src/main.ets").write_bytes(source)
    entries = [{"path": "src/main.ets", "sha256": digest(source)}]
    save_json(task / "source-manifest.json", entries)
    value = read_json(task / "task.json")
    crash = f"synthetic crash for {task_id}".encode()
    (task / "crash.log").write_bytes(crash)
    value.update(task_id=task_id, source_snapshot_sha256=snapshot_digest(entries), crash_sha256=digest(crash))
    save_json(task / "task.json", value)
    return task


def terminal(work: Path, run_id: str, *, status: str = "finished") -> None:
    save_json(work / "run-state.json", {
        "schema_version": 1, "task": "offline", "run_id": run_id,
        "status": status, "finished_at": "2026-10-02T00:00:00+00:00",
    })


def run_synthetic(task: Path, work: Path, run_id: str, scripted: list[AIMessage],
                  *, features: dict[str, bool] | None = None, **kwargs: Any) -> dict[str, Any]:
    report = run_fixed_agent(task, work, run_id, scripted, FixedTextExecutor("src/main.ets", GOOD_TEXT),
                             policy_features=PolicyFeatures.model_validate(features or BASE_FEATURES), **kwargs)
    terminal(work, run_id)
    return report


def passing_script(task: Path, derive: Path) -> list[AIMessage]:
    good = candidate_id(task, derive, "good")
    return [response(1, "record_diagnosis", LOCATION),
            response(2, "edit_source", {"path": "src/main.ets", "old": "bad", "new": "good"}),
            response(3, "register_candidate", {"diagnosis_id": "diagnosis-1"}),
            response(4, "run_check", {"candidate_id": good}), response(5)]


def failing_script(task: Path, derive: Path) -> list[AIMessage]:
    almost = candidate_id(task, derive, "almost")
    return [response(1, "record_diagnosis", LOCATION),
            response(2, "edit_source", {"path": "src/main.ets", "old": "bad", "new": "almost"}),
            response(3, "register_candidate", {"diagnosis_id": "diagnosis-1"}),
            response(4, "run_check", {"candidate_id": almost}), response(5)]


def configuration(session: dict[str, Any]) -> dict[str, Any]:
    return {
        "model_profile_sha256": sha256(canonical(session["model_profile"])),
        "context_profile_sha256": sha256(canonical(session["context_profile"])),
        "tool_schema_sha256": session["tool_schema_sha256"],
        "search_limits_sha256": sha256(canonical(session["search_limits"])),
        **{key: session[key] for key in ("capabilities", "max_calls", "max_known_tokens", "max_seconds",
                                         "max_candidates", "max_tool_calls")},
    }


def task_record(root: Path, task: Path, session: dict[str, Any], *, selection: dict | None = None) -> dict[str, Any]:
    value = read_json(task / "task.json")
    identity = selection or {"case_key": f"cases/{value['task_id']}", "lineage_group_id": "synthetic-lineage",
                             "fault_variant_id": f"fault-{value['task_id']}"}
    return {
        "task_id": value["task_id"],
        **{key: identity[key] for key in ("case_key", "lineage_group_id", "fault_variant_id")},
        "task_path": task.relative_to(root).as_posix(), "task_sha256": digest((task / "task.json").read_bytes()),
        **{key: value[key] for key in ("source_snapshot_sha256", "crash_sha256")},
        **{key: session[key] for key in ("protocol_sha256", "observations_sha256")},
        "selection": selection,
    }


def one_manifest(root: Path, task: Path, work: Path, run_id: str,
                 *, selection: dict | None = None) -> dict[str, Any]:
    session = read_json(work / "session.json")
    return {
        "schema_version": 1, "evaluation_id": "synthetic-evaluation", "scope": "offline_synthetic",
        "configuration": configuration(session), "tasks": [task_record(root, task, session, selection=selection)],
        "variants": [{"variant_id": "base", "policy_features": session["policy_features"]}],
        "repeat_ids": ["repeat1"],
        "runs": [{"task_id": session.get("task_id", read_json(task / "task.json")["task_id"]),
                  "variant_id": "base", "repeat_id": "repeat1", "run_id": run_id,
                  "work_path": work.relative_to(root).as_posix()}],
    }


def tree_bytes(root: Path) -> dict[str, bytes]:
    return {path.relative_to(root).as_posix(): path.read_bytes() for path in root.rglob("*") if path.is_file()}


@pytest.fixture(scope="module")
def base_evaluation(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, dict[str, Any], dict[str, Any]]:
    root = tmp_path_factory.mktemp("metrics-frozen")
    task = make_task(root / "tasks/task1")
    work = root / "work/run1"
    report = run_synthetic(task, work, "run1", passing_script(task, root / "derive"))
    return root, one_manifest(root, task, work, "run1"), report


def clone_evaluation(tmp_path: Path, fixture) -> tuple[Path, dict[str, Any]]:
    source, value, _ = fixture
    root = tmp_path / "evaluation"
    shutil.copytree(source, root)
    return root, copy.deepcopy(value)


def test_verified_rows_retain_host_facts_and_read_only_inputs(base_evaluation) -> None:
    root, raw, original = base_evaluation
    before = tree_bytes(root)
    result = aggregate_evaluation(validated(raw), root)
    assert tree_bytes(root) == before
    assert result["schema_version"] == 1 and result["scope"] == "offline_synthetic"
    row = result["rows"][0]
    assert row["record_status"] == "verified" and row["status"] == "offline_checked"
    assert row["localization"] == original["completion"]["localization"]
    assert row["candidate_artifacts"] == original["completion"]["candidate_artifacts"]
    assert row["first_candidate_id"] == row["latest_candidate_id"] == original["candidates"][0]
    assert row["first_candidate_check_status"] == row["latest_candidate_check_status"] == "pass"
    assert row["candidate_count"] == 1
    assert row["cost"]["reserved_calls"] == 5 and row["cost"]["total_tokens"] == 75
    group = result["variants"][0]
    assert group["task_count"] == group["expected_runs"] == group["verified_runs"] == 1
    assert group["synthetic_latest_pass"] == group["synthetic_first_candidate_pass"] == {
        "passed": 1, "total": 1, "rate": 1.0,
    }
    assert all(value is None for value in result["application_metrics"].values())


def test_not_run_missing_and_verified_are_in_the_frozen_denominator(base_evaluation, tmp_path: Path) -> None:
    root, raw = clone_evaluation(tmp_path, base_evaluation)
    raw["repeat_ids"] = ["repeat1", "repeat2", "repeat3", "repeat4"]
    template = raw["runs"][0]
    raw["runs"] += [
        {**template, "repeat_id": "repeat2", "run_id": "not-run", "work_path": None},
        {**template, "repeat_id": "repeat3", "run_id": "missing-dir", "work_path": "work/missing-dir"},
        {**template, "repeat_id": "repeat4", "run_id": "missing-report", "work_path": "work/missing-report"},
    ]
    terminal(root / "work/missing-report", "missing-report", status="failed")
    result = aggregate_evaluation(validated(raw), root)
    assert [row["record_status"] for row in result["rows"]] == ["verified", "not_run", "missing", "missing"]
    group = result["variants"][0]
    assert group["expected_runs"] == 4 and group["verified_runs"] == 1
    assert group["record_status_counts"] == {"verified": 1, "not_run": 1, "missing": 2}
    assert group["synthetic_latest_pass"] == {"passed": 1, "total": 4, "rate": 0.25}
    assert group["synthetic_first_candidate_pass"] == {"passed": 1, "total": 4, "rate": 0.25}
    assert group["cost"]["known_total_tokens_lower_bound"] == 75
    assert group["cost"]["known_reserved_calls_lower_bound"] == 5
    assert group["cost"]["total_tokens"] is None and group["cost"]["reserved_calls"] is None
    assert group["cost"]["unknown_cost_rows"] >= 2


def test_first_failure_latest_failure_preserve_intermediate_pass_and_distinct_locations(tmp_path: Path) -> None:
    root = tmp_path
    task = make_task(root / "task")
    almost = candidate_id(task, root / "derive-almost", "almost")
    good = candidate_id(task, root / "derive-good", "good")
    worse = candidate_id(task, root / "derive-worse", "worse")
    script = [response(1, "record_diagnosis", LOCATION)]
    for start, replacement, candidate in ((2, "almost", almost), (5, "good", good)):
        script += [response(start, "edit_source", {"path": "src/main.ets", "old": "bad", "new": replacement}),
                   response(start + 1, "register_candidate", {"diagnosis_id": "diagnosis-1"}),
                   response(start + 2, "run_check", {"candidate_id": candidate})]
    script += [response(8, "record_diagnosis", {**LOCATION, "line": 2, "reason": "revised synthetic proposal"}),
               response(9, "edit_source", {"path": "src/main.ets", "old": "bad", "new": "worse"}),
               response(10, "register_candidate", {"diagnosis_id": "diagnosis-2"}),
               response(11, "run_check", {"candidate_id": worse}), response(12)]
    work = root / "work/run1"
    report = run_synthetic(task, work, "run1", script)
    result = aggregate_evaluation(validated(one_manifest(root, task, work, "run1")), root)
    row = result["rows"][0]
    assert row["status"] == "unresolved" and row["candidate_count"] == 3
    assert row["first_candidate_id"] == almost and row["first_candidate_check_status"] == "fail"
    assert row["latest_candidate_id"] == worse and row["latest_candidate_check_status"] == "fail"
    assert [item["check"]["status"] for item in row["candidate_artifacts"]] == ["fail", "pass", "fail"]
    assert row["localization"]["initial"] == report["completion"]["localization"]["initial"]
    assert row["localization"]["latest"] == report["completion"]["localization"]["latest"]
    assert row["localization"]["initial"] != row["localization"]["latest"]
    assert result["variants"][0]["synthetic_latest_pass"]["passed"] == 0


def test_missing_usage_keeps_cost_unknown_instead_of_zero(tmp_path: Path) -> None:
    task = make_task(tmp_path / "task")
    work = tmp_path / "work/run1"
    run_synthetic(task, work, "run1", [response(1, "list_source"), response(2, usage=False)])
    result = aggregate_evaluation(validated(one_manifest(tmp_path, task, work, "run1")), tmp_path)
    row = result["rows"][0]
    assert row["cost"]["missing_usage_calls"] == 1
    assert row["cost"]["known_total_tokens"] == 15 and row["cost"]["total_tokens"] is None
    group = result["variants"][0]
    assert group["cost"]["known_total_tokens_lower_bound"] == 15
    assert group["cost"]["total_tokens"] is None and group["cost"]["unknown_cost_rows"] == 1
    assert group["cost"]["reserved_calls"] == 2


def test_context_compaction_is_in_total_cost_and_not_solver_call_count(tmp_path: Path) -> None:
    task = large_task(tmp_path / "task")
    work = tmp_path / "work/run1"
    report = run_synthetic(task, work, "run1", [response(1, "read_source", {"path": "src/large.ets"}),
                                       response(2, "read_source", {"path": "src/large.ets"}), response(3)],
                  compaction_policy=policy(), compaction_responses=[AIMessage(
                      content=SUMMARY, usage_metadata={"input_tokens": 7, "output_tokens": 2, "total_tokens": 9})])
    result = aggregate_evaluation(validated(one_manifest(tmp_path, task, work, "run1")), tmp_path)
    assert result["rows"][0]["context_compactions"] == report["completion"]["context_compactions"]
    cost = result["rows"][0]["cost"]
    assert cost["reserved_calls"] == 4 and cost["total_tokens"] == 54
    assert cost["calls_by_kind"]["solver"] == 3 and cost["calls_by_kind"]["context_compaction"] == 1
    group_cost = result["variants"][0]["cost"]
    assert group_cost["reserved_calls"] == 4 and group_cost["total_tokens"] == 54
    assert group_cost["calls_by_kind"]["solver"] == 3 and group_cost["calls_by_kind"]["context_compaction"] == 1


def test_paired_repeats_stay_grouped_by_task_instead_of_claiming_independent_cases(tmp_path: Path) -> None:
    task = make_task(tmp_path / "task")
    script = passing_script(task, tmp_path / "derive")
    failed_script = failing_script(task, tmp_path / "derive-failed")
    cells = [("base", "repeat1", False), ("enhanced", "repeat1", True),
             ("base", "repeat2", True), ("enhanced", "repeat2", False)]
    runs = []
    first_work = tmp_path / "work/base-repeat1"
    for variant, repeat, passed in cells:
        run_id = f"{variant}-{repeat}"
        work = tmp_path / "work" / run_id
        run_synthetic(task, work, run_id, script if passed else failed_script,
                      features=BASE_FEATURES if variant == "base" else ENHANCED_FEATURES)
        runs.append({"task_id": "task1", "variant_id": variant, "repeat_id": repeat,
                     "run_id": run_id, "work_path": work.relative_to(tmp_path).as_posix()})
    raw = one_manifest(tmp_path, task, first_work, "base-repeat1")
    raw.update(variants=[{"variant_id": "base", "policy_features": BASE_FEATURES},
                         {"variant_id": "enhanced", "policy_features": ENHANCED_FEATURES}],
               repeat_ids=["repeat1", "repeat2"], runs=runs)
    result = aggregate_evaluation(validated(raw), tmp_path)
    paired = result["paired_comparisons"][0]
    assert paired["base_variant_id"] == "base" and paired["compared_variant_id"] == "enhanced"
    assert paired["expected_pairs"] == paired["eligible_pairs"] == 2 and paired["unknown_pairs"] == 0
    assert paired["synthetic_gain_count"] == paired["synthetic_loss_count"] == 1
    assert paired["same_pass_count"] == paired["same_fail_count"] == 0
    assert len(paired["by_task"]) == 1 and paired["by_task"][0]["task_id"] == "task1"
    assert all(group["task_count"] == 1 and group["expected_runs"] == 2 for group in result["variants"])
    assert all(value is None for value in result["application_metrics"].values())


def test_missing_paired_result_is_unknown_rather_than_a_failure(base_evaluation) -> None:
    root, original, _ = base_evaluation
    raw = copy.deepcopy(original)
    raw["variants"].append({"variant_id": "other", "policy_features": BASE_FEATURES})
    raw["runs"].append({**raw["runs"][0], "variant_id": "other", "run_id": "not-run", "work_path": None})
    result = aggregate_evaluation(validated(raw), root)
    paired = result["paired_comparisons"][0]
    assert paired["expected_pairs"] == paired["unknown_pairs"] == 1 and paired["eligible_pairs"] == 0
    assert paired["synthetic_gain_count"] == paired["synthetic_loss_count"] == 0
    assert paired["same_pass_count"] == paired["same_fail_count"] == 0
    assert len(paired["by_task"]) == 1


@pytest.mark.parametrize("problem", ["missing-cell", "duplicate-cell", "duplicate-run-id", "duplicate-work-path",
                                     "duplicate-task", "duplicate-case-key", "duplicate-crash", "path-escape",
                                     "task-work-overlap"])
def test_manifest_rejects_reused_identity_incomplete_matrix_and_unsafe_paths(base_evaluation, problem: str) -> None:
    _, original, _ = base_evaluation
    raw = copy.deepcopy(original)
    cell = raw["runs"][0]
    if problem == "missing-cell":
        raw["repeat_ids"].append("repeat2")
    elif problem == "duplicate-cell":
        raw["runs"].append({**cell, "run_id": "run2", "work_path": "work/run2"})
    elif problem in {"duplicate-run-id", "duplicate-work-path"}:
        raw["repeat_ids"].append("repeat2")
        raw["runs"].append({**cell, "repeat_id": "repeat2",
                            "run_id": "run1" if problem == "duplicate-run-id" else "run2",
                            "work_path": "work/run1" if problem == "duplicate-work-path" else "work/run2"})
    elif problem.startswith("duplicate-"):
        other = {**raw["tasks"][0], "task_id": "task2", "case_key": "cases/task2",
                 "task_path": "tasks/task2/input", "crash_sha256": "f" * 64}
        if problem == "duplicate-task":
            other["task_id"] = "task1"
        elif problem == "duplicate-case-key":
            other["case_key"] = raw["tasks"][0]["case_key"]
        else:
            other["crash_sha256"] = raw["tasks"][0]["crash_sha256"]
        raw["tasks"].append(other)
        raw["runs"].append({**cell, "task_id": other["task_id"], "run_id": "run2", "work_path": "work/run2"})
    elif problem == "path-escape":
        cell["work_path"] = "../elsewhere"
    else:
        cell["work_path"] = raw["tasks"][0]["task_path"] + "/nested-run"
    with pytest.raises(ValueError):
        validated(raw)


@pytest.mark.parametrize("field", ["model_profile_sha256", "context_profile_sha256", "tool_schema_sha256",
                                   "search_limits_sha256", "max_calls", "max_known_tokens", "max_seconds",
                                   "max_candidates", "max_tool_calls", "capabilities"])
def test_configuration_drift_is_rejected_against_actual_session(base_evaluation, field: str) -> None:
    root, original, _ = base_evaluation
    raw = copy.deepcopy(original)
    value = raw["configuration"][field]
    raw["configuration"][field] = ([] if field == "capabilities" else "f" * 64 if field.endswith("sha256")
                                   else value + 1)
    with pytest.raises(ValueError):
        aggregate_evaluation(validated(raw), root)


@pytest.mark.parametrize("field", ["protocol_sha256", "observations_sha256", "source_snapshot_sha256", "task_sha256"])
def test_frozen_task_and_acceptance_identity_cannot_drift(base_evaluation, field: str) -> None:
    root, original, _ = base_evaluation
    raw = copy.deepcopy(original)
    raw["tasks"][0][field] = "f" * 64
    with pytest.raises(ValueError):
        aggregate_evaluation(validated(raw), root)


def test_variant_policy_is_rechecked_even_with_a_valid_report(base_evaluation) -> None:
    root, original, _ = base_evaluation
    raw = copy.deepcopy(original)
    raw["variants"][0]["policy_features"] = ENHANCED_FEATURES
    with pytest.raises(ValueError):
        aggregate_evaluation(validated(raw), root)


@pytest.mark.parametrize("problem", ["absent", "running", "wrong-run", "wrong-task", "corrupt"])
def test_existing_work_requires_a_matching_terminal_managed_marker(base_evaluation, tmp_path: Path, problem: str) -> None:
    root, raw = clone_evaluation(tmp_path, base_evaluation)
    marker = root / raw["runs"][0]["work_path"] / "run-state.json"
    value = read_json(marker)
    if problem == "absent":
        marker.unlink()
    elif problem == "corrupt":
        marker.write_text("{", encoding="utf-8")
    else:
        value[{"running": "status", "wrong-run": "run_id", "wrong-task": "task"}[problem]] = {
            "running": "running", "wrong-run": "other-run", "wrong-task": "test",
        }[problem]
        save_json(marker, value)
    with pytest.raises(ValueError):
        aggregate_evaluation(validated(raw), root)


@pytest.mark.parametrize("problem", ["status", "candidate-pass", "source", "protocol", "system-prompt", "task-prompt",
                                     "nonempty-wal", "duplicate-json-key", "nonfinite-json"])
def test_present_but_corrupt_evidence_rejects_the_entire_evaluation(base_evaluation, tmp_path: Path, problem: str) -> None:
    root, raw = clone_evaluation(tmp_path, base_evaluation)
    work = root / raw["runs"][0]["work_path"]
    report_path = work / "agent-report.json"
    if problem in {"status", "candidate-pass"}:
        report = read_json(report_path)
        if problem == "status":
            report["status"] = "unresolved"
        else:
            report["completion"]["candidate_artifacts"][0]["check"]["status"] = "fail"
        save_json(report_path, report)
    elif problem == "source":
        (root / raw["tasks"][0]["task_path"] / "source/src/main.ets").write_text("value=secret-answer\n", encoding="utf-8")
    elif problem in {"protocol", "system-prompt", "task-prompt"}:
        session = read_json(work / "session.json")
        session[{"protocol": "protocol_sha256", "system-prompt": "system_prompt_sha256",
                 "task-prompt": "task_message_sha256"}[problem]] = "f" * 64
        save_json(work / "session.json", session)
    elif problem == "nonempty-wal":
        (work / "model-calls.sqlite-wal").write_bytes(b"unfinished WAL data")
    elif problem == "duplicate-json-key":
        text = report_path.read_text(encoding="utf-8")
        report_path.write_text('{"schema_version":6,' + text[1:], encoding="utf-8")
    else:
        text = report_path.read_text(encoding="utf-8")
        report_path.write_text('{"untrusted_extra":NaN,' + text[1:], encoding="utf-8")
    with pytest.raises(ValueError):
        aggregate_evaluation(validated(raw), root)


def test_hardlinked_evidence_is_not_owned_even_when_bytes_match(base_evaluation, tmp_path: Path) -> None:
    root, raw = clone_evaluation(tmp_path, base_evaluation)
    report = root / raw["runs"][0]["work_path"] / "agent-report.json"
    linked = tmp_path / "shared-report.json"
    os.link(report, linked)
    with pytest.raises(ValueError):
        aggregate_evaluation(validated(raw), root)
    assert linked.read_bytes() == report.read_bytes()


@pytest.mark.parametrize("text", ['{"schema_version":1,"schema_version":1}', '{"schema_version":NaN}'])
def test_manifest_loader_rejects_duplicate_keys_and_nonfinite_json(tmp_path: Path, text: str) -> None:
    path = tmp_path / "evaluation.json"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError):
        load_evaluation_manifest(path)


def test_manifest_loader_rejects_one_byte_above_its_bound(base_evaluation, tmp_path: Path) -> None:
    _, raw, _ = base_evaluation
    payload = json.dumps(raw).encode("utf-8")
    assert len(payload) <= MAX_MANIFEST_BYTES
    path = tmp_path / "oversized-evaluation.json"
    path.write_bytes(payload + b" " * (MAX_MANIFEST_BYTES + 1 - len(payload)))
    assert path.stat().st_size == MAX_MANIFEST_BYTES + 1
    with pytest.raises(ValueError):
        load_evaluation_manifest(path)


def test_oversized_matrix_is_rejected_before_expansion(base_evaluation, monkeypatch: pytest.MonkeyPatch) -> None:
    _, original, _ = base_evaluation
    raw = copy.deepcopy(original)
    raw["repeat_ids"] = [f"repeat{number}" for number in range(1, MAX_CELLS + 2)]

    def forbidden_expansion(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("matrix capacity must be checked before expanding its Cartesian product")

    monkeypatch.setattr(metrics.itertools, "product", forbidden_expansion)
    with pytest.raises(ValueError):
        validated(raw)


def test_not_run_cell_still_requires_a_real_frozen_task_snapshot(base_evaluation) -> None:
    root, original, _ = base_evaluation
    raw = copy.deepcopy(original)
    raw["tasks"][0]["task_path"] = "tasks/nonexistent-task"
    raw["runs"][0]["work_path"] = None
    with pytest.raises(ValueError):
        aggregate_evaluation(validated(raw), root)


def test_input_tree_drift_after_report_read_is_rejected(base_evaluation, tmp_path: Path,
                                                      monkeypatch: pytest.MonkeyPatch) -> None:
    root, raw = clone_evaluation(tmp_path, base_evaluation)
    source = root / raw["tasks"][0]["task_path"] / "source/src/main.ets"
    original = metrics.read_completion
    observed = 0

    def change_source_after_read(*args: Any, **kwargs: Any) -> dict[str, Any]:
        nonlocal observed
        result = original(*args, **kwargs)
        observed += 1
        source.write_bytes(b"changed after completion was verified\n")
        return result

    monkeypatch.setattr(metrics, "read_completion", change_source_after_read)
    with pytest.raises(ValueError):
        aggregate_evaluation(validated(raw), root)
    assert observed == 1, "the refusal must cover drift after successful report verification"


def test_write_report_uses_the_same_read_only_verified_aggregation(base_evaluation, tmp_path: Path) -> None:
    root, raw = clone_evaluation(tmp_path, base_evaluation)
    manifest = root / "evaluation.json"
    save_json(manifest, raw)
    before = tree_bytes(root)
    expected = aggregate_evaluation(validated(raw), root)
    output = tmp_path / "exports/evaluation-report.json"
    report = write_evaluation_report(manifest, root, output)
    assert report == expected == read_json(output)
    assert tree_bytes(root) == before


@pytest.mark.parametrize("target", ["existing", "task-child", "work-child", "task-parent", "work-parent"])
def test_report_write_refuses_existing_output_or_any_input_overlap(base_evaluation, tmp_path: Path, target: str) -> None:
    root, raw = clone_evaluation(tmp_path, base_evaluation)
    manifest = tmp_path / "evaluation.json"
    save_json(manifest, raw)
    paths = {
        "existing": tmp_path / "existing.json",
        "task-child": root / raw["tasks"][0]["task_path"] / "new-report.json",
        "work-child": root / raw["runs"][0]["work_path"] / "new-report.json",
        "task-parent": (root / raw["tasks"][0]["task_path"]).parent,
        "work-parent": (root / raw["runs"][0]["work_path"]).parent,
    }
    output = paths[target]
    if target == "existing":
        output.write_text("retain me", encoding="utf-8")
    before = tree_bytes(root)
    with pytest.raises(ValueError):
        write_evaluation_report(manifest, root, output)
    assert tree_bytes(root) == before
    if target == "existing":
        assert output.read_text(encoding="utf-8") == "retain me"


def test_report_write_refuses_a_linked_output_parent(base_evaluation, tmp_path: Path) -> None:
    root, raw = clone_evaluation(tmp_path, base_evaluation)
    manifest = tmp_path / "evaluation.json"
    save_json(manifest, raw)
    real = tmp_path / "real-output"
    real.mkdir()
    linked = tmp_path / "linked-output"
    try:
        linked.symlink_to(real, target_is_directory=True)
    except OSError:
        pytest.skip("current Windows account cannot create symbolic links")
    with pytest.raises(ValueError):
        write_evaluation_report(manifest, root, linked / "report.json")
    assert list(real.iterdir()) == []


def test_schema_two_task_requires_and_rechecks_the_selected_case_identity(tmp_path: Path) -> None:
    task = make_task(tmp_path / "task")
    value = read_json(task / "task.json")
    selection = CaseSelection.model_validate({
        "task_id": "task1", "case_key": "cases/task1", "lineage_group_id": "synthetic-lineage",
        "fault_variant_id": "fault-task1", "app": "apps/synthetic",
        "log": {"path": "logs/task1.log", "sha256": value["crash_sha256"]},
    })
    value.update(schema_version=2, case_identity_sha256=selection.sha256())
    save_json(task / "task.json", value)
    work = tmp_path / "work/run1"
    run_synthetic(task, work, "run1", [response(1)])
    raw = one_manifest(tmp_path, task, work, "run1", selection=selection.model_dump())
    assert aggregate_evaluation(validated(raw), tmp_path)["rows"][0]["record_status"] == "verified"
    for changed in (None, {**selection.model_dump(), "fault_variant_id": "another-fault"}):
        invalid = copy.deepcopy(raw)
        invalid["tasks"][0]["selection"] = changed
        if changed is not None:
            invalid["tasks"][0]["fault_variant_id"] = changed["fault_variant_id"]
        with pytest.raises(ValueError):
            aggregate_evaluation(validated(invalid), tmp_path)


@pytest.mark.parametrize("encoding", ["utf-8", "utf-16"])
def test_business_json_is_preserved_as_bytes_not_parsed_as_host_evidence(tmp_path: Path, encoding: str) -> None:
    task = make_task(tmp_path / "task")
    resource = task / "source/src/resource.json"
    resource.write_bytes('{"same":1,"same":2}'.encode(encoding))
    entries = sorted([{"path": path.relative_to(task / "source").as_posix(), "sha256": digest(path.read_bytes())}
                      for path in (task / "source").rglob("*") if path.is_file()], key=lambda item: item["path"])
    save_json(task / "source-manifest.json", entries)
    value = read_json(task / "task.json")
    value["source_snapshot_sha256"] = snapshot_digest(entries)
    save_json(task / "task.json", value)
    work = tmp_path / "work/run1"
    run_synthetic(task, work, "run1", passing_script(task, tmp_path / "derive"))
    before = tree_bytes(tmp_path)
    result = aggregate_evaluation(validated(one_manifest(tmp_path, task, work, "run1")), tmp_path)
    assert result["rows"][0]["record_status"] == "verified"
    assert tree_bytes(tmp_path) == before


def test_declared_and_persisted_tool_schema_agreement_cannot_replace_reconstruction(
        base_evaluation, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, raw = clone_evaluation(tmp_path, base_evaluation)
    work = root / raw["runs"][0]["work_path"]
    session = read_json(work / "session.json")
    session["tool_schema_sha256"] = raw["configuration"]["tool_schema_sha256"] = "f" * 64
    save_json(work / "session.json", session)
    # Recompute a matching host report; refusal must concern actual tool descriptors.
    workspace = metrics.CandidateWorkspace.reopen(root / raw["tasks"][0]["task_path"], work, "run1")
    ledger = metrics.CheckLedger(workspace, metrics.ReplayOnlyExecutor(session["protocol_sha256"]), read_only=True)
    calls = metrics.ModelCallStore(work / "model-calls.sqlite", read_only=True)
    actions = metrics.ToolActionStore(work / "tool-actions.sqlite", read_only=True)
    from dfx_agent.reporting import build_completion

    report = read_json(work / "agent-report.json")
    report["completion"] = build_completion(workspace, ledger, calls, actions, stopped_reason=report["stopped_reason"])
    save_json(work / "agent-report.json", report)
    with pytest.raises(ValueError, match="installed tool definitions"):
        aggregate_evaluation(validated(raw), root)


def test_manifest_changed_during_aggregation_is_refused_before_output(
        base_evaluation, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, raw = clone_evaluation(tmp_path, base_evaluation)
    manifest_path = root / "evaluation.json"
    save_json(manifest_path, raw)
    original = metrics.aggregate_evaluation

    def change_manifest(manifest, root):
        result = original(manifest, root)
        changed = copy.deepcopy(raw)
        changed["evaluation_id"] = "another-evaluation"
        save_json(manifest_path, changed)
        return result

    monkeypatch.setattr(metrics, "aggregate_evaluation", change_manifest)
    output = tmp_path / "new-report.json"
    with pytest.raises(ValueError, match="manifest changed"):
        write_evaluation_report(manifest_path, root, output)
    assert not output.exists()


def test_mock_wire_profile_and_tool_descriptors_are_verified_without_resending(tmp_path: Path) -> None:
    from test_provider_loop import scripted_adapter, wire_response

    from dfx_agent.agent import run_provider_agent

    task = make_task(tmp_path / "task")
    work = tmp_path / "work/run1"
    requests: list[dict] = []
    adapter = scripted_adapter([wire_response(1)], requests)
    run_provider_agent(task, work, "run1", adapter, FixedTextExecutor("src/main.ets", GOOD_TEXT))
    terminal(work, "run1")
    assert len(requests) == 1
    before = tree_bytes(tmp_path)
    result = aggregate_evaluation(validated(one_manifest(tmp_path, task, work, "run1")), tmp_path)
    assert result["rows"][0]["record_status"] == "verified"
    assert result["rows"][0]["cost"]["total_tokens"] == 15
    assert len(requests) == 1 and tree_bytes(tmp_path) == before
