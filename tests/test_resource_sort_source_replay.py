"""Execute real sort method bytes, reject no-op/semantic-loss repairs and replay one export."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from test_enterprise_source_replay import check_exported_candidate
from test_office_source_replay import managed as managed  # noqa: PLC0414
from test_office_source_replay import ordinary_node as ordinary_node  # noqa: PLC0414

from dfx_agent.local_execution import (
    STAGES,
    IndependentOracle,
    LocalApplicationExecutor,
    LocalCheckConfig,
    LocalCommand,
)
from dfx_agent.pilot_preparation import REVIEWED_MOCKS, REVIEWED_ORACLES
from dfx_agent.task import digest, prepare_task
from dfx_agent.verification import CheckLedger
from dfx_agent.workspace import CandidateWorkspace, TextEdit

ROOT = Path(__file__).resolve().parents[1]
ORACLE = "resources/source-validation/resource-sort/oracle.cjs"
ADAPTER = ROOT / ORACLE
SOURCE, MARKER, SCOPE = REVIEWED_ORACLES[ORACLE]
GUARD = "if (index1 >= list.length) return;"
FIX = "if (index1 < 0 || index2 < 0 || index1 >= list.length || index2 >= list.length) return;"
SYNTHETIC = """export class ResourceManageVM {
  public customSortResource(index1: number, index2: number) {
    const list = this.balanceType === BalanceChangeType.INCOME ? this.resource!.income : this.resource!.expense;
    if (index1 >= list.length) return;
    let temp = list[index1];
    let sortNum1 = list[index1].sortNum;
    let sortNum2 = list[index2].sortNum;
    list[index1] = list[index2]; list[index1].sortNum = sortNum1;
    list[index2] = temp; list[index2].sortNum = sortNum2;
  }
}
"""


@pytest.mark.parametrize("replacement,expected,failed_stage", [
    (FIX, "pass", None),
    ("return;", "fail", "behavior"),
    (GUARD + " /* not repaired */", "fail", "target"),
    (FIX + " if (this.balanceType === BalanceChangeType.INCOME) return;", "fail", "behavior"),
])
def test_sort_protocol_checks_both_categories_and_rejects_noop(
        tmp_path: Path, ordinary_node: Path, managed: Path,
        replacement: str, expected: str, failed_stage: str | None) -> None:
    data, task = tmp_path / "data", tmp_path / "task"
    source = data / "app" / SOURCE
    source.parent.mkdir(parents=True)
    source.write_text(SYNTHETIC, encoding="utf-8")
    log = data / "fault.log"
    log.write_bytes(b"TypeError: Cannot read property sortNum of undefined\n")
    prepare_task(data, {"task_id": "synthetic-resource-sort", "app": "app",
        "log": {"path": "fault.log", "sha256": digest(log.read_bytes())}}, task)
    workspace = CandidateWorkspace(task, tmp_path / "work", "sort-check")
    workspace.start_draft()
    workspace.apply_text_edits([TextEdit(path=SOURCE, old=GUARD, new=replacement)])
    candidate = workspace.register_candidate(1)
    sha = digest(ADAPTER.read_bytes())
    config = LocalCheckConfig(mode="source", project_root=str(data / "app"), validation_scope=SCOPE,
        mocked_interfaces=REVIEWED_MOCKS[ORACLE], commands={stage: LocalCommand(
            argv=[str(ordinary_node), str(ADAPTER), stage], timeout_seconds=10, oracle_sha256=sha) for stage in STAGES},
        independent_oracles={stage: IndependentOracle(path=str(ADAPTER), sha256=sha) for stage in STAGES},
        baseline_target_exit_codes=[9], baseline_fault_pattern=MARKER, max_check_seconds=90)
    executor = LocalApplicationExecutor(config, workspace, managed_root=managed)
    result = CheckLedger(workspace, executor).run(candidate.candidate_id)
    assert result.status == expected and result.level == "source_execution"
    receipt = executor.evidence(result.idempotency_key)
    assert receipt["baseline"]["fault_reproduced"] is True
    if failed_stage is not None:
        assert receipt["stages"][-1]["stage"] == failed_stage
    assert receipt["application_verification"] == "not_run"


@pytest.mark.skipif(os.environ.get("DFX_REAL_EXPORT_ACCEPTANCE") != "1", reason="local exported candidate not requested")
def test_existing_comprehensive_candidate_with_actual_source_and_exported_receipts(
        tmp_path: Path, ordinary_node: Path, managed: Path) -> None:
    check_exported_candidate(tmp_path, ordinary_node, managed,
        ROOT / "runs/exports/cases/b1-001-comprehensivetool1/live1017-retry04",
        ADAPTER, "resource-sort", SCOPE, MARKER, REVIEWED_MOCKS[ORACLE])
