"""Preparation uses complete filtered snapshots; no model or native process runs."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from dfx_agent import managed_runtime, pilot_preparation
from dfx_agent.local_execution import LocalCheckConfig
from dfx_agent.task import digest, verify_task


def test_available_reviewed_pilot_initializes_without_network(tmp_path: Path) -> None:
    """Optional local integration: no key read, request, repair or data mutation."""
    from dfx_agent.competition import CompetitionConfig
    from dfx_agent.model_provider import CloudChatCompletionsAdapter
    from dfx_agent.workspace import CandidateWorkspace

    root = Path(__file__).resolve().parents[1]
    profile = root / "configs/pilot022-canonical.local.json"
    task = root / ".dfx-local/tmp/reviewed/task022-canonical"
    if not profile.exists() or not task.exists():
        pytest.skip("the explicitly reviewed local pilot has not been prepared")
    settings = CompetitionConfig.model_validate_json(profile.read_bytes())
    adapter = CloudChatCompletionsAdapter(settings.model, settings.provider, allow_network=False)
    assert adapter.profile()["settings"]["model_id"] == "deepseek-flash"
    workspace = CandidateWorkspace(task, tmp_path / "admission-only", "no-network")
    assert workspace.source_snapshot_sha256 == verify_task(task)["source_snapshot_sha256"]


def test_prepared_manifest_uses_host_independent_case_order(tmp_path: Path) -> None:
    from dfx_agent.task import prepare_task
    from dfx_agent.workspace import CandidateWorkspace

    data = tmp_path / "dataset"
    app = data / "app"
    app.mkdir(parents=True)
    for name in ("build-profile.json5", "Index.ets", "hvigorfile.ts", "feature/Page.ets"):
        path = app / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"export const value = 1;\n")
    (data / "fault.log").write_bytes(b"synthetic fault")
    task = tmp_path / "reviewed"
    prepare_task(data, {"task_id": "mixed-case", "app": "app",
        "log": {"path": "fault.log", "sha256": digest(b"synthetic fault")}}, task)
    manifest = json.loads((task / "source-manifest.json").read_bytes())
    assert [entry["path"] for entry in manifest] == [
        "Index.ets", "build-profile.json5", "feature/Page.ets", "hvigorfile.ts"]
    workspace = CandidateWorkspace(task, tmp_path / "work", "run")
    workspace.start_draft()
    assert workspace.source_snapshot_sha256 == verify_task(task)["source_snapshot_sha256"]


def _fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path, dict[str, Any]]:
    root = tmp_path / "repo"
    for folder in (".dfx-local/state", ".dfx-local/tmp", "data_B/app", "data_B/logs"):
        (root / folder).mkdir(parents=True)
    target = root / "data_B/app" / pilot_preparation.SOURCE
    target.parent.mkdir(parents=True)
    target.write_text("export class OrderRequestChecksum {}", encoding="utf-8")
    (root / "data_B/app/other.ets").write_text("export const other = 1", encoding="utf-8")
    (root / "data_B/app/AGENTS.md").write_text("not an instruction", encoding="utf-8")
    (root / "data_B/app/Prompt.md").write_text("evaluator construction answer", encoding="utf-8")
    (root / "data_B/app/tests").mkdir()
    (root / "data_B/app/tests/answer.ets").write_text("construction answer", encoding="utf-8")
    log = root / "data_B/logs/selected.log"
    log.write_bytes(b"RangeError: synthetic original fault")
    oracle = root / pilot_preparation.ORACLE
    oracle.parent.mkdir(parents=True)
    oracle.write_text("// fake host oracle; native stage is mocked", encoding="utf-8")
    node = root / "node.exe"
    node.write_bytes(b"not executed")
    config = root / ".dfx-local/state/prepare.json"
    value = {"case": {"task_id": "synthetic022", "case_key": "group/synthetic022",
        "lineage_group_id": "synthetic-family", "fault_variant_id": "synthetic-variant", "app": "app",
        "log": {"path": "logs/selected.log", "sha256": digest(log.read_bytes())}},
        "data_root": "data_B", "output": ".dfx-local/tmp/reviewed/task022",
        "validation_profile": ".dfx-local/state/source022.json", "node": str(node),
        "oracle": pilot_preparation.ORACLE, "record": ".dfx-local/state/source022-admission.json"}
    config.write_text(json.dumps(value), encoding="utf-8")
    def admit(_root: Path, task: str) -> dict[str, str]:
        assert _root == root and task == "prepare"
        return {"task": task}

    monkeypatch.setattr(managed_runtime, "require_managed_worker", admit)

    def stage(_node: Path, _oracle: Path, source: Path, name: str) -> dict[str, Any]:
        header = {"stage": name, "source_sha256": digest((source / pilot_preparation.SOURCE).read_bytes())}
        return {"stage": name, "exit_code": 0 if name == "build" else 9,
            "stdout": json.dumps(header) + "\n", "stderr": "" if name == "build" else pilot_preparation.MARKER,
            "stdout_sha256": digest(json.dumps(header).encode()), "stderr_sha256": digest(b"fixture")}

    monkeypatch.setattr(pilot_preparation, "_baseline_stage", stage)
    return root, config, value


def test_preparation_keeps_allowed_full_source_and_excludes_construction_material(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, config, value = _fixture(tmp_path, monkeypatch)
    original = (root / "data_B/app" / pilot_preparation.SOURCE).read_bytes()
    result = pilot_preparation.run_preparation(root, config)
    assert result["status"] == "source_baseline_admitted" and result["fault_reproduced"] is True
    assert result["repair_success"] is None and result["candidate_created"] is False and result["model_calls"] == 0
    assert result["application_verification"] == "not_run"
    output = root / value["output"]
    verify_task(output)
    selected = {row["path"] for row in json.loads((output / "source-manifest.json").read_bytes())}
    assert selected == {pilot_preparation.SOURCE, "other.ets"}
    assert (root / "data_B/app" / pilot_preparation.SOURCE).read_bytes() == original
    profile = LocalCheckConfig.model_validate_json((root / value["validation_profile"]).read_bytes())
    assert profile.mode == "source" and profile.project_root == str(output / "source")
    assert profile.mocked_interfaces == [] and profile.source_mapping == {}
    assert "1e09f387" not in json.dumps(profile.model_dump())


@pytest.mark.parametrize("field,value", [
    ("output", "data_B/overwritten"), ("output", ".dfx-local/tmp/../outside"),
    ("validation_profile", "configs/profile.json"), ("record", "data_B/record.json"),
    ("oracle", "data_B/arbitrary.cjs"), ("data_root", "../outside"),
    ("data_root", ".dfx-local/state"),
])
def test_preparation_rejects_paths_outside_declared_owners(tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch, field: str, value: str) -> None:
    root, config, data = _fixture(tmp_path, monkeypatch)
    data[field] = value
    config.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError):
        pilot_preparation.run_preparation(root, config)
    assert not (root / ".dfx-local/tmp/reviewed/task022").exists()


@pytest.mark.parametrize("field", ["output", "validation_profile", "record"])
def test_existing_outputs_are_never_overwritten(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                               field: str) -> None:
    root, config, data = _fixture(tmp_path, monkeypatch)
    existing = root / data[field]
    existing.parent.mkdir(parents=True, exist_ok=True)
    existing.write_text("keep", encoding="utf-8")
    with pytest.raises(ValueError, match="existing"):
        pilot_preparation.run_preparation(root, config)
    assert existing.read_text(encoding="utf-8") == "keep"


@pytest.mark.parametrize("target_exit,target_stderr", [(0, ""), (9, "unrelated RangeError")])
def test_baseline_must_reproduce_before_profile_is_written(tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch, target_exit: int, target_stderr: str) -> None:
    root, config, value = _fixture(tmp_path, monkeypatch)

    def not_reproduced(_node: Path, _oracle: Path, source: Path, name: str) -> dict[str, Any]:
        return {"stage": name, "exit_code": 0 if name == "build" else target_exit,
            "stdout": json.dumps({"stage": name,
                "source_sha256": digest((source / pilot_preparation.SOURCE).read_bytes())}),
            "stderr": "" if name == "build" else target_stderr}

    monkeypatch.setattr(pilot_preparation, "_baseline_stage", not_reproduced)
    with pytest.raises(ValueError, match="did not reproduce"):
        pilot_preparation.run_preparation(root, config)
    assert not (root / value["validation_profile"]).exists()
    assert json.loads((root / value["record"]).read_bytes())["status"] == "blocked"


def test_wrong_managed_lease_is_refused_before_creating_outputs(tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch) -> None:
    root, config, value = _fixture(tmp_path, monkeypatch)

    def reject(_root: Path, _task: str) -> dict[str, Any]:
        raise ValueError("wrong managed lease")

    monkeypatch.setattr(managed_runtime, "require_managed_worker", reject)
    with pytest.raises(ValueError, match="managed lease"):
        pilot_preparation.run_preparation(root, config)
    assert not (root / value["record"]).exists() and not (root / value["output"]).exists()


def test_stage_source_drift_is_blocked_and_preserved(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, config, value = _fixture(tmp_path, monkeypatch)

    def changed(_node: Path, _oracle: Path, source: Path, name: str) -> dict[str, Any]:
        (source / "other.ets").write_text("changed", encoding="utf-8")
        return {"stage": name, "exit_code": 0, "stdout": "{}", "stderr": ""}

    monkeypatch.setattr(pilot_preparation, "_baseline_stage", changed)
    with pytest.raises(ValueError, match="changed"):
        pilot_preparation.run_preparation(root, config)
    assert not (root / value["validation_profile"]).exists()
    assert (root / value["output"]).exists()
    assert json.loads((root / value["record"]).read_bytes())["status"] == "blocked"
