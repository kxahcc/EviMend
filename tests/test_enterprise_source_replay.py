"""Actual JS replay, negative repairs and an opt-in exported candidate check."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest
from test_office_source_replay import managed as managed  # noqa: PLC0414
from test_office_source_replay import ordinary_node as ordinary_node  # noqa: PLC0414

from dfx_agent.evidence_io import local_evidence_path
from dfx_agent.local_execution import (
    STAGES,
    IndependentOracle,
    LocalApplicationExecutor,
    LocalCheckConfig,
    LocalCommand,
)
from dfx_agent.task import digest, prepare_task
from dfx_agent.verification import CheckLedger
from dfx_agent.workspace import CandidateWorkspace, TextEdit, _copy_tree, _files

ROOT = Path(__file__).resolve().parents[1]
ADAPTER = ROOT / "resources/source-validation/enterprise-property/oracle.cjs"
SOURCE = "commons/component/src/main/ets/utils/PropUtil.js"
SCOPE = "Actual hasKey object/undefined API, inherited properties and immutability; no ArkUI or persistence"


def configuration(node: Path, project: Path) -> LocalCheckConfig:
    sha = digest(ADAPTER.read_bytes())
    return LocalCheckConfig(mode="source", project_root=str(project), validation_scope=SCOPE,
        mocked_interfaces=[], commands={stage: LocalCommand(argv=[str(node), str(ADAPTER), stage],
            timeout_seconds=10, oracle_sha256=sha) for stage in STAGES},
        independent_oracles={stage: IndependentOracle(path=str(ADAPTER), sha256=sha) for stage in STAGES},
        baseline_target_exit_codes=[9], baseline_fault_pattern="DFX009_TARGET_IN_NON_OBJECT", max_check_seconds=90)


@pytest.mark.parametrize("replacement,expected,failed_stage", [
    ("return obj !== undefined && obj !== null && key in obj;", "pass", None),
    ("return false;", "fail", "behavior"),
    ("return obj != null && Object.prototype.hasOwnProperty.call(obj, key);", "fail", "regression"),
    ("return key in obj; /* still broken */", "fail", "target"),
])
def test_source_protocol_reproduces_fault_and_rejects_behavior_loss(
        tmp_path: Path, ordinary_node: Path, managed: Path,
        replacement: str, expected: str, failed_stage: str | None) -> None:
    data, task = tmp_path / "data", tmp_path / "task"
    source = data / "app" / SOURCE
    source.parent.mkdir(parents=True)
    source.write_bytes(b"export function hasKey(obj, key) { return key in obj; }\n")
    log = data / "fault.log"
    log.write_bytes(b"Error name:TypeError\nError message:Cannot use 'in' operator in Non-Object\n")
    prepare_task(data, {"task_id": "synthetic-enterprise", "app": "app",
        "log": {"path": "fault.log", "sha256": digest(log.read_bytes())}}, task)
    workspace = CandidateWorkspace(task, tmp_path / "work", "enterprise-check")
    workspace.start_draft()
    workspace.apply_text_edits([TextEdit(path=SOURCE, old="return key in obj;", new=replacement)])
    candidate = workspace.register_candidate(1)
    executor = LocalApplicationExecutor(configuration(ordinary_node, data / "app"), workspace, managed_root=managed)
    result = CheckLedger(workspace, executor).run(candidate.candidate_id)
    assert result.status == expected and result.level == "source_execution"
    receipt = executor.evidence(result.idempotency_key)
    assert receipt["baseline"]["fault_reproduced"] is True
    if failed_stage is not None:
        assert receipt["stages"][-1]["stage"] == failed_stage
    assert receipt["application_verification"] == "not_run"


def check_exported_candidate(
        tmp_path: Path, node: Path, managed_root: Path, result_root: Path,
        adapter: Path, protocol: str, scope: str, marker: str, mocks: list[str]) -> None:
    """Run the existing executor against exact frozen export bytes; never amend its report."""
    summary = json.loads((result_root / "summary.json").read_bytes())
    task = result_root / "input-task"
    project = tmp_path / "original"
    _copy_tree(task / "source", project)
    workspace = CandidateWorkspace(task, tmp_path / "validation-work", "verify-export")
    workspace.start_draft()
    candidate_files = _files(result_root / "repaired-source")
    for relative, path in _files(workspace.draft).items():
        if relative not in candidate_files:
            local_evidence_path(path).unlink()
    for relative, path in candidate_files.items():
        target = local_evidence_path(workspace.draft / relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(local_evidence_path(path).read_bytes())
    candidate = workspace.register_candidate(1)
    assert candidate.candidate_snapshot_sha256 == summary["candidate_snapshot_sha256"]
    sha = digest(adapter.read_bytes())
    config = LocalCheckConfig(mode="source", project_root=str(project), validation_scope=scope,
        mocked_interfaces=mocks, commands={stage: LocalCommand(argv=[str(node), str(adapter), stage],
            timeout_seconds=10, oracle_sha256=sha) for stage in STAGES},
        independent_oracles={stage: IndependentOracle(path=str(adapter), sha256=sha) for stage in STAGES},
        baseline_target_exit_codes=[9], baseline_fault_pattern=marker, max_check_seconds=90)
    executor = LocalApplicationExecutor(config, workspace, managed_root=managed_root)
    checked = CheckLedger(workspace, executor).run(candidate.candidate_id)
    receipt = executor.evidence(checked.idempotency_key)
    assert executor.root is not None
    destination = result_root / "source-validation" / protocol
    assert not destination.exists(), "do not overwrite a prior verification"
    _copy_tree(executor.root / checked.idempotency_key, destination / "execution")
    shutil.copyfile(adapter, destination / "oracle.cjs")
    evidence = {"schema_version": 1, "scope": scope, "mocked_interfaces": mocks,
        "oracle_timing": "posthoc_candidate_validation",
        "original_run_id": summary["run_id"], "original_report_sha256": summary["report_sha256"],
        "original_candidate_id": summary["candidate_id"],
        "candidate_snapshot_sha256": candidate.candidate_snapshot_sha256,
        "source_snapshot_sha256": workspace.source_snapshot_sha256,
        "source_verification": checked.status, "application_verification": "not_run",
        "check": checked.model_dump(mode="json"), "execution": receipt,
        "oracle_sha256": sha, "model_calls": 0, "model_tokens": 0,
        "limitation": "Checks the exact exported candidate after generation; original model report remains analysis-only."}
    (destination / "result.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    assert checked.status == "pass" and checked.level == "source_execution"
    assert receipt["baseline"]["fault_reproduced"] is True


@pytest.mark.skipif(os.environ.get("DFX_REAL_EXPORT_ACCEPTANCE") != "1", reason="local exported candidate not requested")
def test_existing_enterprise_candidate_with_actual_source_and_exported_receipts(
        tmp_path: Path, ordinary_node: Path, managed: Path) -> None:
    check_exported_candidate(tmp_path, ordinary_node, managed,
        ROOT / "runs/exports/cases/b1-009-enterpriserecruitment2/live1017-retry05",
        ADAPTER, "enterprise-property", SCOPE, "DFX009_TARGET_IN_NON_OBJECT", [])
