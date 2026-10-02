"""Stopped synthetic evidence exports remain complete, immutable and portable."""

from __future__ import annotations

import copy
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from langchain_core.messages import AIMessage
from test_agent_loop import candidate_id, response
from test_compaction_loop import SUMMARY, large_task, policy
from test_metrics import (
    LOCATION,
    clone_evaluation,
    make_task,
    one_manifest,
    passing_script,
    read_json,
    run_synthetic,
    save_json,
    terminal,
    validated,
)
from test_metrics import base_evaluation as base_evaluation  # noqa: PLC0414

from dfx_agent import exporting
from dfx_agent.metrics import aggregate_evaluation
from dfx_agent.model_calls import canonical, sha256
from dfx_agent.task import digest, snapshot_digest

OWNER_ID = "019949bd-84bd-7ccd-bb22-b79e1b7c76b6"
OTHER_OWNER_ID = "019949bd-84bd-7ccd-bb22-b79e1b7c76b7"
EXPORT_ID = "synthetic-export"
REPOSITORY = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("invalid_pid", [None, True, 0, -1, "123"])
def test_worker_ancestry_rejects_invalid_or_unrelated_process_identity(invalid_pid: Any) -> None:
    assert exporting._worker_ancestor(invalid_pid) is False


def test_worker_ancestry_accepts_the_actual_managed_worker_despite_windows_venv_launcher() -> None:
    lease = REPOSITORY / ".dfx-local/state/active.json"
    if not lease.exists():
        pytest.skip("no managed test lease is available")
    active = read_json(lease)
    assert active["task"] == "test"
    assert exporting._worker_ancestor(active["child_pid"]) is True
    assert exporting._worker_ancestor(os.getpid()) is False


@dataclass
class ExportInputs:
    root: Path
    raw: dict[str, Any]
    manifest_path: Path
    plan_path: Path

    @property
    def target(self) -> Path:
        return self.root / "runs/exports" / EXPORT_ID

    @property
    def work(self) -> Path:
        return self.root / self.raw["runs"][0]["work_path"]

    def save_manifest(self) -> None:
        save_json(self.manifest_path, self.raw)

    def preview(self) -> dict[str, Any]:
        return exporting.preview_export(self.manifest_path, self.root, EXPORT_ID, OWNER_ID)

    def freeze(self) -> dict[str, Any]:
        plan = self.preview()
        exporting.save_export_plan(plan, self.plan_path)
        return plan

    def apply(self, plan: dict[str, Any]) -> dict[str, Any]:
        return exporting.apply_export(self.manifest_path, self.root, EXPORT_ID, OWNER_ID,
                                      self.plan_path, plan["plan_id"])


def managed_inputs(root: Path, raw: dict[str, Any]) -> ExportInputs:
    """Prepare only fixture-owned paths, without invoking the real manager."""
    managed = root / ".dfx-local"
    for directory in (managed / "state", managed / "tmp", root / "configs", root / "runs/exports"):
        directory.mkdir(parents=True, exist_ok=True)
    settings = read_json(REPOSITORY / "configs/local-runtime.json")
    settings["limits"]["max_export_package_bytes"] = 536_870_912
    save_json(root / "configs/local-runtime.json", settings)
    save_json(managed / "owner.json", {
        "schema_version": 1, "project_instance_id": OWNER_ID,
        "repo_real_path": str(root.resolve()), "managed_real_path": str(managed.resolve()),
        "created_at": "2026-10-02T00:00:00+00:00",
    })
    result = ExportInputs(root, raw, managed / "state/evaluation.json",
                          managed / f"state/export-plan-{EXPORT_ID}.json")
    result.save_manifest()
    return result


@pytest.fixture
def export_inputs(base_evaluation, tmp_path: Path) -> ExportInputs:
    root, raw = clone_evaluation(tmp_path, base_evaluation)
    return managed_inputs(root, raw)


def io_path(path: Path) -> Path:
    """Use native extended-length spelling for deep Windows fixture file operations."""
    if os.name == "nt" and not str(path).startswith("\\\\?\\"):
        absolute = str(path.absolute())
        return Path("\\\\?\\UNC\\" + absolute[2:] if absolute.startswith("\\\\") else "\\\\?\\" + absolute)
    return path


def tree_snapshot(root: Path) -> dict[str, bytes | None]:
    """Include empty directories: an undeclared directory is still an input change."""
    root = io_path(root)
    return {path.relative_to(root).as_posix(): None if path.is_dir() else path.read_bytes()
            for path in root.rglob("*")}


def source_snapshot(inputs: ExportInputs) -> dict[str, Any]:
    paths = [task["task_path"] for task in inputs.raw["tasks"]]
    paths += [cell["work_path"] for cell in inputs.raw["runs"] if cell["work_path"] is not None]
    return {"manifest": inputs.manifest_path.read_bytes(),
            **{relative: tree_snapshot(inputs.root / relative) if (inputs.root / relative).exists() else None
               for relative in paths}}


def assert_receipt(receipt: dict[str, Any], target: Path, aggregate: dict[str, Any]) -> None:
    assert receipt["status"] == "verified"
    assert receipt["export_id"] == EXPORT_ID and receipt["owner_id"] == OWNER_ID
    assert Path(receipt["package_path"]) == target
    assert receipt["aggregate_sha256"] == aggregate["report_sha256"]
    assert len(receipt["package_sha256"]) == 64


def test_preview_binds_existing_evidence_and_discloses_local_raw_content_without_creating_package(
        export_inputs: ExportInputs) -> None:
    inputs = export_inputs
    before = tree_snapshot(inputs.root)
    aggregate = aggregate_evaluation(validated(inputs.raw), inputs.root)
    plan = inputs.preview()
    assert tree_snapshot(inputs.root) == before
    assert not inputs.target.exists()
    assert plan["schema_version"] == 1 and plan["scope"] == "offline_synthetic"
    assert plan["export_id"] == EXPORT_ID and plan["owner_id"] == OWNER_ID
    assert plan["manifest_sha256"] == digest(inputs.manifest_path.read_bytes())
    assert plan["aggregate_sha256"] == aggregate["report_sha256"]
    assert 0 < plan["estimated_bytes"] <= 536_870_912
    assert len(plan["plan_id"]) == 64
    assert plan["privacy"]["local_only"] is True and plan["privacy"]["redacted"] is False


def test_export_preserves_every_registered_byte_and_can_reaggregate_without_source_access(
        export_inputs: ExportInputs, tmp_path: Path) -> None:
    inputs = export_inputs
    before = source_snapshot(inputs)
    original = aggregate_evaluation(validated(inputs.raw), inputs.root)
    plan = inputs.freeze()
    receipt = inputs.apply(plan)
    assert_receipt(receipt, inputs.target, original)
    assert source_snapshot(inputs) == before
    assert (inputs.target / "evaluation.json").read_bytes() == inputs.manifest_path.read_bytes()
    assert read_json(inputs.target / "aggregate-report.json") == original
    for relative, expected in before.items():
        if relative != "manifest" and expected is not None:
            assert tree_snapshot(inputs.target / "payload" / relative) == expected
    # Undeclared candidate-derivation fixtures are not silently added to the package.
    assert not (inputs.target / "payload/derive").exists()
    assert aggregate_evaluation(validated(inputs.raw), inputs.target / "payload") == original
    package_before = tree_snapshot(inputs.target)
    verified = exporting.verify_export(inputs.target)
    assert_receipt(verified, inputs.target, original)
    assert tree_snapshot(inputs.target) == package_before
    assert verified == receipt
    # Verification must depend on packaged bytes, not the original absolute paths.
    moved = tmp_path / "portable-package"
    shutil.copytree(io_path(inputs.target), io_path(moved))
    relocated = exporting.verify_export(moved)
    assert_receipt(relocated, moved, original)
    assert {key: value for key, value in relocated.items() if key != "package_path"} == {
        key: value for key, value in receipt.items() if key != "package_path"}


def test_not_run_missing_directory_and_stopped_missing_report_remain_in_exported_denominator(
        export_inputs: ExportInputs) -> None:
    inputs = export_inputs
    raw = inputs.raw
    raw["repeat_ids"] = ["repeat1", "repeat2", "repeat3", "repeat4"]
    original_cell = raw["runs"][0]
    raw["runs"] += [
        {**original_cell, "repeat_id": "repeat2", "run_id": "not-run", "work_path": None},
        {**original_cell, "repeat_id": "repeat3", "run_id": "missing-dir", "work_path": "work/missing-dir"},
        {**original_cell, "repeat_id": "repeat4", "run_id": "missing-report", "work_path": "work/missing-report"},
    ]
    terminal(inputs.root / "work/missing-report", "missing-report", status="failed")
    inputs.save_manifest()
    original = aggregate_evaluation(validated(raw), inputs.root)
    plan = inputs.freeze()
    inputs.apply(plan)
    recovered = aggregate_evaluation(validated(raw), inputs.target / "payload")
    assert recovered == original == read_json(inputs.target / "aggregate-report.json")
    assert [row["record_status"] for row in recovered["rows"]] == ["verified", "not_run", "missing", "missing"]
    assert recovered["variants"][0]["expected_runs"] == 4
    assert recovered["variants"][0]["cost"]["total_tokens"] is None
    assert not (inputs.target / "payload/work/missing-dir").exists()
    assert (inputs.target / "payload/work/missing-report/run-state.json").is_file()
    assert exporting.verify_export(inputs.target)["status"] == "verified"


@pytest.mark.parametrize("change", ["source", "terminal-state", "manifest", "owner", "configuration", "plan"])
def test_apply_refuses_drift_after_preview_instead_of_implicitly_refreshing_the_plan(
        export_inputs: ExportInputs, change: str) -> None:
    inputs = export_inputs
    plan = inputs.freeze()
    if change == "source":
        path = inputs.root / inputs.raw["tasks"][0]["task_path"] / "source/src/main.ets"
        path.write_bytes(path.read_bytes().replace(b"bad", b"BAD"))
    elif change == "terminal-state":
        state = read_json(inputs.work / "run-state.json")
        state["status"] = "running"
        save_json(inputs.work / "run-state.json", state)
    elif change == "manifest":
        # Even semantically equal JSON must match the exact previewed source file.
        inputs.manifest_path.write_bytes(inputs.manifest_path.read_bytes() + b" ")
    elif change == "owner":
        owner_path = inputs.root / ".dfx-local/owner.json"
        owner = read_json(owner_path)
        owner["project_instance_id"] = OTHER_OWNER_ID
        save_json(owner_path, owner)
    elif change == "configuration":
        path = inputs.root / "configs/local-runtime.json"
        settings = read_json(path)
        settings["limits"]["max_export_package_bytes"] -= 1
        save_json(path, settings)
    else:
        altered = read_json(inputs.plan_path)
        altered["aggregate_sha256"] = "f" * 64
        save_json(inputs.plan_path, altered)
    changed = source_snapshot(inputs)
    with pytest.raises(ValueError):
        inputs.apply(plan)
    assert not inputs.target.exists()
    assert source_snapshot(inputs) == changed


@pytest.mark.parametrize("argument", ["owner", "export-id", "plan-id"])
def test_apply_refuses_mismatched_explicit_identity(export_inputs: ExportInputs, argument: str) -> None:
    inputs = export_inputs
    plan = inputs.freeze()
    owner = OTHER_OWNER_ID if argument == "owner" else OWNER_ID
    export_id = "another-export" if argument == "export-id" else EXPORT_ID
    plan_id = "f" * 64 if argument == "plan-id" else plan["plan_id"]
    with pytest.raises(ValueError):
        exporting.apply_export(inputs.manifest_path, inputs.root, export_id, owner, inputs.plan_path, plan_id)
    assert not inputs.target.exists() and not (inputs.target.parent / "another-export").exists()


def test_apply_never_overwrites_an_existing_export_target(export_inputs: ExportInputs) -> None:
    inputs = export_inputs
    plan = inputs.freeze()
    inputs.target.mkdir()
    sentinel = inputs.target / "retain.txt"
    sentinel.write_bytes(b"user evidence remains")
    with pytest.raises(ValueError):
        inputs.apply(plan)
    assert tree_snapshot(inputs.target) == {"retain.txt": b"user evidence remains"}


@pytest.mark.parametrize("filename", ["unregistered-notes.txt", ".env", "credentials.json"])
def test_unknown_or_credential_work_files_are_not_silently_exported(
        export_inputs: ExportInputs, filename: str) -> None:
    inputs = export_inputs
    (inputs.work / filename).write_bytes(b"synthetic private content")
    before = source_snapshot(inputs)
    with pytest.raises(ValueError):
        inputs.preview()
    assert source_snapshot(inputs) == before and not inputs.target.exists()


@pytest.mark.parametrize("suffix", ["-wal", "-journal"])
def test_graph_checkpoint_with_unmerged_journal_is_refused_without_recovering_or_checkpointing(
        export_inputs: ExportInputs, suffix: str) -> None:
    inputs = export_inputs
    (inputs.work / f"checkpoints.sqlite{suffix}").write_bytes(b"unmerged synthetic graph journal")
    before = source_snapshot(inputs)
    with pytest.raises(ValueError):
        inputs.preview()
    assert source_snapshot(inputs) == before and not inputs.target.exists()


@pytest.mark.parametrize("suffix", ["-wal", "-shm"])
@pytest.mark.parametrize("content", [b"", b"orphaned synthetic sidecar"])
def test_orphaned_host_database_sidecars_are_refused_even_when_empty(
        export_inputs: ExportInputs, suffix: str, content: bytes) -> None:
    inputs = export_inputs
    # This separate stopped fixture has no database or report. Existing run evidence stays untouched.
    orphaned_work = inputs.root / "work/orphaned-run"
    terminal(orphaned_work, "run1", status="failed")
    (orphaned_work / f"checkpoints.sqlite{suffix}").write_bytes(content)
    inputs.raw["runs"][0]["work_path"] = orphaned_work.relative_to(inputs.root).as_posix()
    inputs.save_manifest()
    before = source_snapshot(inputs)
    assert not (orphaned_work / "checkpoints.sqlite").exists()
    with pytest.raises(ValueError):
        inputs.preview()
    assert source_snapshot(inputs) == before and not inputs.target.exists()
    assert (orphaned_work / f"checkpoints.sqlite{suffix}").read_bytes() == content


@pytest.mark.parametrize("problem", ["same-size-source", "missing-file", "extra-file", "extra-directory", "metadata"])
def test_verify_rejects_tampered_missing_or_undeclared_package_entries(
        export_inputs: ExportInputs, problem: str) -> None:
    inputs = export_inputs
    plan = inputs.freeze()
    inputs.apply(plan)
    packaged_source = inputs.target / "payload" / inputs.raw["tasks"][0]["task_path"] / "source/src/main.ets"
    if problem == "same-size-source":
        original = packaged_source.read_bytes()
        packaged_source.write_bytes(original.replace(b"bad", b"BAD"))
        assert packaged_source.stat().st_size == len(original)
    elif problem == "missing-file":
        packaged_source.unlink()
    elif problem == "extra-file":
        (inputs.target / "extra-private.txt").write_bytes(b"must not be accepted")
    elif problem == "extra-directory":
        (inputs.target / "extra-empty-directory").mkdir()
    else:
        metadata = read_json(inputs.target / "package.json")
        metadata["owner_id"] = OTHER_OWNER_ID
        save_json(inputs.target / "package.json", metadata)
    before = tree_snapshot(inputs.target)
    with pytest.raises(ValueError):
        exporting.verify_export(inputs.target)
    assert tree_snapshot(inputs.target) == before


def test_one_byte_above_package_capacity_refuses_preview_before_publishing(export_inputs: ExportInputs) -> None:
    inputs = export_inputs
    estimated = inputs.preview()["estimated_bytes"]
    settings_path = inputs.root / "configs/local-runtime.json"
    settings = read_json(settings_path)
    settings["limits"]["max_export_package_bytes"] = estimated - 1
    save_json(settings_path, settings)
    before = source_snapshot(inputs)
    # Capacity is checked while producing the reviewable plan as well as at apply.
    with pytest.raises(ValueError):
        inputs.preview()
    assert source_snapshot(inputs) == before and not inputs.target.exists()


def test_hardlinked_source_evidence_is_not_owned_even_when_the_copy_would_have_identical_bytes(
        export_inputs: ExportInputs, tmp_path: Path) -> None:
    inputs = export_inputs
    linked = tmp_path / "shared-checkpoint.sqlite"
    checkpoint = inputs.work / "checkpoints.sqlite"
    os.link(checkpoint, linked)
    with pytest.raises(ValueError):
        inputs.preview()
    assert linked.read_bytes() == checkpoint.read_bytes()
    assert not inputs.target.exists()


def test_linked_export_parent_is_refused_before_writing_outside_the_owned_root(
        export_inputs: ExportInputs, tmp_path: Path) -> None:
    inputs = export_inputs
    actual = tmp_path / "external-export-parent"
    actual.mkdir()
    inputs.target.parent.rmdir()
    try:
        inputs.target.parent.symlink_to(actual, target_is_directory=True)
    except OSError:
        pytest.skip("current Windows account cannot create symbolic links")
    with pytest.raises(ValueError):
        inputs.preview()
    assert list(actual.iterdir()) == []


def test_compaction_messages_and_cost_are_retained_as_saved_evidence_not_reinvoked(tmp_path: Path) -> None:
    root = tmp_path / "evaluation"
    task = large_task(root / "task")
    work = root / "work/run1"
    original = run_synthetic(task, work, "run1", [
        response(1, "read_source", {"path": "src/large.ets"}),
        response(2, "read_source", {"path": "src/large.ets"}), response(3),
    ], compaction_policy=policy(), compaction_responses=[AIMessage(
        content=SUMMARY, usage_metadata={"input_tokens": 7, "output_tokens": 2, "total_tokens": 9})])
    inputs = managed_inputs(root, one_manifest(root, task, work, "run1"))
    before = source_snapshot(inputs)
    plan = inputs.freeze()
    receipt = inputs.apply(plan)
    assert source_snapshot(inputs) == before
    aggregate = read_json(inputs.target / "aggregate-report.json")
    row = aggregate["rows"][0]
    assert row["context_compactions"] == original["completion"]["context_compactions"]
    assert row["cost"]["calls_by_kind"]["context_compaction"] == 1
    assert row["cost"]["reserved_calls"] == 4 and row["cost"]["total_tokens"] == 54
    assert exporting.verify_export(inputs.target) == receipt


@pytest.mark.parametrize("encoding", ["utf-8", "utf-16"])
def test_business_json_bytes_are_exported_without_parsing_them_as_host_evidence(
        tmp_path: Path, encoding: str) -> None:
    root = tmp_path / "evaluation"
    task = make_task(root / "task")
    source = task / "source"
    business = source / "src/resource.json"
    raw_bytes = '{"same":1,"same":2}'.encode(encoding)
    business.write_bytes(raw_bytes)
    entries = sorted([{"path": path.relative_to(source).as_posix(), "sha256": digest(path.read_bytes())}
                      for path in source.rglob("*") if path.is_file()], key=lambda entry: entry["path"])
    save_json(task / "source-manifest.json", entries)
    task_value = read_json(task / "task.json")
    task_value["source_snapshot_sha256"] = snapshot_digest(entries)
    save_json(task / "task.json", task_value)
    work = root / "work/run1"
    run_synthetic(task, work, "run1", passing_script(task, root / "derive"))
    inputs = managed_inputs(root, one_manifest(root, task, work, "run1"))
    expected = aggregate_evaluation(validated(inputs.raw), root)
    before = source_snapshot(inputs)
    plan = inputs.freeze()
    receipt = inputs.apply(plan)
    packaged = inputs.target / "payload" / task.relative_to(root) / "source/src/resource.json"
    assert io_path(packaged).read_bytes() == raw_bytes
    assert source_snapshot(inputs) == before
    assert aggregate_evaluation(validated(inputs.raw), inputs.target / "payload") == expected
    assert exporting.verify_export(inputs.target) == receipt


def test_application_sqlite_named_asset_is_preserved_as_arbitrary_bytes_not_a_host_database(
        tmp_path: Path) -> None:
    root = tmp_path / "evaluation"
    task = make_task(root / "task")
    source = task / "source"
    business = source / "src/resource.sqlite"
    raw_bytes = b"\x00\xffbusiness asset bytes; deliberately not a valid SQLite database\n"
    business.write_bytes(raw_bytes)
    entries = sorted([{"path": path.relative_to(source).as_posix(), "sha256": digest(path.read_bytes())}
                      for path in source.rglob("*") if path.is_file()], key=lambda entry: entry["path"])
    save_json(task / "source-manifest.json", entries)
    task_value = read_json(task / "task.json")
    task_value["source_snapshot_sha256"] = snapshot_digest(entries)
    save_json(task / "task.json", task_value)
    work = root / "work/run1"
    run_synthetic(task, work, "run1", passing_script(task, root / "derive"))
    inputs = managed_inputs(root, one_manifest(root, task, work, "run1"))
    expected = aggregate_evaluation(validated(inputs.raw), root)
    before = source_snapshot(inputs)
    plan = inputs.freeze()
    receipt = inputs.apply(plan)
    packaged = inputs.target / "payload" / task.relative_to(root) / "source/src/resource.sqlite"
    assert io_path(packaged).read_bytes() == raw_bytes
    assert source_snapshot(inputs) == before
    assert aggregate_evaluation(validated(inputs.raw), inputs.target / "payload") == expected
    assert exporting.verify_export(inputs.target) == receipt


def test_first_failure_and_latest_failure_do_not_discard_the_intermediate_passing_candidate(tmp_path: Path) -> None:
    root = tmp_path / "evaluation"
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
    run_synthetic(task, work, "run1", script)
    inputs = managed_inputs(root, one_manifest(root, task, work, "run1"))
    original = aggregate_evaluation(validated(inputs.raw), root)
    plan = inputs.freeze()
    inputs.apply(plan)
    aggregate = aggregate_evaluation(validated(inputs.raw), inputs.target / "payload")
    assert aggregate == original
    row = aggregate["rows"][0]
    assert [item["check"]["status"] for item in row["candidate_artifacts"]] == ["fail", "pass", "fail"]
    assert row["first_candidate_check_status"] == row["latest_candidate_check_status"] == "fail"
    assert row["localization"]["initial"] != row["localization"]["latest"]
    assert aggregate["variants"][0]["synthetic_latest_pass"]["passed"] == 0


def test_explicit_preview_refresh_only_replaces_the_state_plan_and_invalidates_the_previous_id(
        export_inputs: ExportInputs) -> None:
    inputs = export_inputs
    first = inputs.freeze()
    inputs.manifest_path.write_bytes(inputs.manifest_path.read_bytes() + b" ")
    retained = inputs.plan_path.parent / "retained-other-state.json"
    save_json(retained, {"synthetic": "retain this state"})
    before = source_snapshot(inputs)
    other_state = {name: value for name, value in tree_snapshot(inputs.plan_path.parent).items()
                   if name != inputs.plan_path.name}
    second = inputs.preview()
    assert second["plan_id"] != first["plan_id"]
    exporting.save_export_plan(second, inputs.plan_path)
    assert read_json(inputs.plan_path) == second
    assert {name: value for name, value in tree_snapshot(inputs.plan_path.parent).items()
            if name != inputs.plan_path.name} == other_state
    assert source_snapshot(inputs) == before
    with pytest.raises(ValueError):
        inputs.apply(first)
    assert not inputs.target.exists()
    assert inputs.apply(second)["status"] == "verified"


@pytest.mark.parametrize("suffix", ["", ".new"])
def test_manifest_cannot_be_the_plan_destination_or_its_atomic_temporary_file(
        export_inputs: ExportInputs, suffix: str) -> None:
    inputs = export_inputs
    colliding_manifest = inputs.plan_path.with_name(inputs.plan_path.name + suffix)
    colliding_manifest.write_bytes(inputs.manifest_path.read_bytes())
    before = tree_snapshot(inputs.root)
    with pytest.raises(ValueError):
        exporting.preview_export(colliding_manifest, inputs.root, EXPORT_ID, OWNER_ID)
    assert tree_snapshot(inputs.root) == before
    assert colliding_manifest.read_bytes() == inputs.manifest_path.read_bytes()
    assert not inputs.target.exists()


@pytest.mark.parametrize("existing", ["ordinary-json", "original-manifest"])
def test_saving_a_preview_never_overwrites_a_non_plan_json_file_even_at_the_fixed_plan_name(
        export_inputs: ExportInputs, existing: str) -> None:
    inputs = export_inputs
    plan = inputs.preview()
    if existing == "ordinary-json":
        save_json(inputs.plan_path, {"user_owned": "ordinary JSON must remain"})
    else:
        inputs.plan_path.write_bytes(inputs.manifest_path.read_bytes())
    before = tree_snapshot(inputs.root)
    with pytest.raises(ValueError):
        exporting.save_export_plan(plan, inputs.plan_path)
    assert tree_snapshot(inputs.root) == before


def test_verifier_reaggregates_only_the_moved_package_payload_using_the_real_validator(
        export_inputs: ExportInputs, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    inputs = export_inputs
    plan = inputs.freeze()
    receipt = inputs.apply(plan)
    package = tmp_path / "independent-package"
    shutil.copytree(io_path(inputs.target), io_path(package))
    original = exporting.aggregate_evaluation
    observed: list[Path] = []

    def validate_packaged_inputs(manifest, root: Path):
        # A Windows extended-length spelling still names the same package payload.
        normalized = Path(str(root).removeprefix("\\\\?\\"))
        assert normalized == package / "payload"
        assert normalized != inputs.root
        observed.append(normalized)
        return original(manifest, root)

    monkeypatch.setattr(exporting, "aggregate_evaluation", validate_packaged_inputs)
    before = tree_snapshot(package)
    verified = exporting.verify_export(package)
    assert observed == [package / "payload"]
    assert tree_snapshot(package) == before
    assert verified["aggregate_sha256"] == receipt["aggregate_sha256"]


@pytest.mark.parametrize("change", ["same-size-source", "new-file"])
def test_source_change_after_an_actual_copy_refuses_publication_and_never_deletes_source_evidence(
        export_inputs: ExportInputs, monkeypatch: pytest.MonkeyPatch, change: str) -> None:
    inputs = export_inputs
    plan = inputs.freeze()
    copied: list[Path] = []
    source = inputs.root / inputs.raw["tasks"][0]["task_path"] / "source/src/main.ets"
    original_bytes = source.read_bytes()
    original_copy = exporting._copy_file

    def copy_then_change(source_path: Path, destination: Path, *args: Any, **kwargs: Any) -> None:
        original_copy(source_path, destination, *args, **kwargs)
        copied.append(source_path)
        if len(copied) == 1:
            if change == "same-size-source":
                source.write_bytes(original_bytes.replace(b"bad", b"BAD"))
            else:
                (inputs.work / "new-unregistered-file.txt").write_bytes(b"appeared while exporting")

    monkeypatch.setattr(exporting, "_copy_file", copy_then_change)
    with pytest.raises(ValueError):
        inputs.apply(plan)
    assert copied, "the mutation occurs after a successful real file copy"
    assert not inputs.target.exists()
    assert source.is_file() and (inputs.work / "checkpoints.sqlite").is_file()
    if change == "same-size-source":
        assert source.read_bytes() == original_bytes.replace(b"bad", b"BAD")
        assert source.stat().st_size == len(original_bytes)
    else:
        assert source.read_bytes() == original_bytes
        assert (inputs.work / "new-unregistered-file.txt").read_bytes() == b"appeared while exporting"


def test_manifest_growth_during_copy_is_rejected_before_aggregation_or_any_staged_metadata_write(
        export_inputs: ExportInputs, monkeypatch: pytest.MonkeyPatch) -> None:
    inputs = export_inputs
    plan = inputs.freeze()
    before = source_snapshot(inputs)
    config = inputs.root / "configs/local-runtime.json"
    original_config = config.read_bytes()
    temporary_root = inputs.root / ".dfx-local/tmp"
    prior_entries = {path.name for path in temporary_root.iterdir()}
    original_copy = exporting._copy_file
    original_aggregate = exporting.aggregate_evaluation
    grown_manifest = before["manifest"] + b"\n" + b" " * 65_536
    mutated = False
    copies = 0

    def copy_then_grow_manifest(*args: Any, **kwargs: Any) -> None:
        nonlocal mutated, copies
        original_copy(*args, **kwargs)
        copies += 1
        if not mutated:
            # Trailing JSON whitespace is valid, but exceeds the exact bytes reserved in the plan.
            inputs.manifest_path.write_bytes(grown_manifest)
            mutated = True

    def reject_aggregation_after_unchecked_growth(manifest, root: Path):
        assert not mutated, "changed manifest bytes must be rejected before expensive reaggregation"
        return original_aggregate(manifest, root)

    monkeypatch.setattr(exporting, "_copy_file", copy_then_grow_manifest)
    monkeypatch.setattr(exporting, "aggregate_evaluation", reject_aggregation_after_unchecked_growth)
    with pytest.raises(ValueError):
        inputs.apply(plan)
    assert copies > 0 and mutated
    assert not inputs.target.exists()
    created = [path for path in temporary_root.iterdir() if path.name not in prior_entries]
    assert len(created) == 1 and created[0].is_dir(), "failed export leaves only its owned unpublished stage"
    stage = io_path(created[0])
    assert (stage / "payload").is_dir()
    assert all(not (stage / filename).exists() for filename in (
        "evaluation.json", "aggregate-report.json", "package.json"))
    assert source_snapshot(inputs) == {**before, "manifest": grown_manifest}
    assert config.read_bytes() == original_config


@pytest.mark.parametrize("marker", ["active", "remote-jobs"])
def test_unresolved_activity_markers_block_export_without_clearing_them(
        export_inputs: ExportInputs, monkeypatch: pytest.MonkeyPatch, marker: str) -> None:
    inputs = export_inputs
    monkeypatch.delenv("DFX_EXPORT_RUN_ID", raising=False)
    marker_path = inputs.root / ".dfx-local/state" / f"{marker}.json"
    save_json(marker_path, {"task": "offline", "run_id": "another-run", "project_instance_id": OWNER_ID,
                            "child_pid": os.getppid(), "status": "running"})
    before = tree_snapshot(inputs.root)
    with pytest.raises(ValueError):
        inputs.preview()
    assert tree_snapshot(inputs.root) == before


def test_only_the_matching_managed_export_worker_can_preview_with_an_active_marker(
        export_inputs: ExportInputs, monkeypatch: pytest.MonkeyPatch) -> None:
    inputs = export_inputs
    run_id = "this-export-worker"
    monkeypatch.setenv("DFX_EXPORT_RUN_ID", run_id)
    marker_path = inputs.root / ".dfx-local/state/active.json"
    save_json(marker_path, {"task": "export", "run_id": run_id, "project_instance_id": OWNER_ID,
                            "child_pid": os.getppid()})
    before = source_snapshot(inputs)
    assert inputs.preview()["export_id"] == EXPORT_ID
    assert source_snapshot(inputs) == before
    active = read_json(marker_path)
    active["child_pid"] += 1
    save_json(marker_path, active)
    with pytest.raises(ValueError):
        inputs.preview()


@pytest.mark.parametrize("shortfall", [0, 1])
def test_free_disk_reserve_includes_all_pending_package_bytes_and_has_an_exact_boundary(
        export_inputs: ExportInputs, monkeypatch: pytest.MonkeyPatch, shortfall: int) -> None:
    inputs = export_inputs
    expected = inputs.preview()["estimated_bytes"]
    limits = read_json(inputs.root / "configs/local-runtime.json")["limits"]
    total = 128 * 1024**3
    reserve = max(limits["min_free_disk_bytes"], int(total * limits["min_free_disk_fraction"]))
    free = reserve + expected - shortfall
    monkeypatch.setattr(exporting.shutil, "disk_usage", lambda _: SimpleNamespace(
        total=total, used=total - free, free=free))
    before = source_snapshot(inputs)
    if shortfall:
        with pytest.raises(ValueError, match="free disk reserve"):
            inputs.preview()
    else:
        assert inputs.preview()["estimated_bytes"] == expected
    assert source_snapshot(inputs) == before and not inputs.target.exists()


@pytest.mark.parametrize("shortfall", [0, 1])
def test_shared_storage_budget_counts_existing_managed_and_retained_export_bytes(
        export_inputs: ExportInputs, monkeypatch: pytest.MonkeyPatch, shortfall: int) -> None:
    inputs = export_inputs
    (inputs.target.parent / "retained-evidence.bin").write_bytes(b"retain this existing evidence")
    expected = inputs.preview()["estimated_bytes"]
    used = sum(path.stat().st_size for directory in (inputs.root / ".dfx-local", inputs.target.parent)
               for path in directory.rglob("*") if path.is_file())
    config_path = inputs.root / "configs/local-runtime.json"
    settings = read_json(config_path)
    settings["limits"]["managed_disk_budget_bytes"] = used + expected - shortfall
    save_json(config_path, settings)
    monkeypatch.setattr(exporting.shutil, "disk_usage", lambda _: SimpleNamespace(
        total=128 * 1024**3, used=0, free=128 * 1024**3))
    before = source_snapshot(inputs)
    if shortfall:
        with pytest.raises(ValueError, match="shared storage budget"):
            inputs.preview()
    else:
        assert inputs.preview()["estimated_bytes"] == expected
    assert source_snapshot(inputs) == before and not inputs.target.exists()


def test_disk_reserve_lost_after_copy_stops_an_unpublished_stage_without_deleting_sources(
        export_inputs: ExportInputs, monkeypatch: pytest.MonkeyPatch) -> None:
    inputs = export_inputs
    plan = inputs.freeze()
    before = source_snapshot(inputs)
    settings = read_json(inputs.root / "configs/local-runtime.json")["limits"]
    total = 128 * 1024**3
    reserve = max(settings["min_free_disk_bytes"], int(total * settings["min_free_disk_fraction"]))
    original_copy = exporting._copy_file
    copies = 0

    def copy_then_lose_reserve(*args: Any, **kwargs: Any) -> None:
        nonlocal copies
        original_copy(*args, **kwargs)
        copies += 1
        monkeypatch.setattr(exporting.shutil, "disk_usage", lambda _: SimpleNamespace(
            total=total, used=total - reserve, free=reserve))

    monkeypatch.setattr(exporting, "_copy_file", copy_then_lose_reserve)
    with pytest.raises(ValueError, match="free disk reserve"):
        inputs.apply(plan)
    assert copies == 1
    assert not inputs.target.exists() and source_snapshot(inputs) == before


def reseal_metadata_for_test(package: Path, metadata: dict[str, Any]) -> None:
    """Update both hashes and actual size so a refusal must inspect the malformed binding."""
    package = io_path(package)
    payload_bytes = sum(path.stat().st_size for path in package.rglob("*")
                        if path.is_file() and path != package / "package.json")
    plan = metadata["plan"]
    for _ in range(20):
        plan["plan_id"] = sha256(canonical({key: value for key, value in plan.items() if key != "plan_id"}))
        metadata["plan_id"] = plan["plan_id"]
        metadata["package_sha256"] = sha256(canonical({
            key: value for key, value in metadata.items() if key != "package_sha256"}))
        raw = canonical(metadata).encode("utf-8")
        actual_size = payload_bytes + len(raw)
        if plan["estimated_bytes"] == actual_size:
            (package / "package.json").write_bytes(raw)
            return
        plan["estimated_bytes"] = actual_size
    raise AssertionError("test metadata size calculation did not converge")


@pytest.mark.parametrize("problem", [
    "missing-inventory", "missing-source-snapshot", "inventory-not-dict", "invalid-entry-kind",
    "source-snapshot-mismatch", "unknown-plan-field", "unsafe-inventory-reference",
])
def test_recomputed_metadata_hashes_and_sizes_do_not_make_malformed_bindings_valid(
        export_inputs: ExportInputs, problem: str) -> None:
    inputs = export_inputs
    plan = inputs.freeze()
    inputs.apply(plan)
    metadata = copy.deepcopy(read_json(inputs.target / "package.json"))
    changed = metadata["plan"]
    if problem == "missing-inventory":
        del changed["inventory"]
    elif problem == "missing-source-snapshot":
        del changed["source_snapshot"]
    elif problem == "inventory-not-dict":
        changed["inventory"] = []
    elif problem == "invalid-entry-kind":
        changed["inventory"]["payload"]["kind"] = "pipe"
    elif problem == "source-snapshot-mismatch":
        changed["source_snapshot"][inputs.raw["runs"][0]["work_path"]] = None
    elif problem == "unknown-plan-field":
        changed["invented_verified_claim"] = "claims must not be added by rehashing metadata"
    else:
        changed["inventory"]["../outside.json"] = {"kind": "file", "size": 0, "sha256": digest(b"")}
    reseal_metadata_for_test(inputs.target, metadata)
    before = tree_snapshot(inputs.target)
    with pytest.raises(ValueError):
        exporting.verify_export(inputs.target)
    assert tree_snapshot(inputs.target) == before
