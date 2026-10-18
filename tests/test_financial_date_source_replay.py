"""Actual date/getter source replay and controls against behavior-destroying repairs."""

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
DATE = "resources/source-validation/calendar-days/oracle.cjs"
ASSET = "resources/source-validation/asset-pie/oracle.cjs"
BAD_DATE = "return Math.floor(BigInt(interval) as number);"
DATE_SOURCE = """export function calculateDaysBetweenDates(date1: string, date2: string): number {
  const interval = Math.abs(new Date(date2).getTime() - new Date(date1).getTime()) / 86400000;
  return Math.floor(BigInt(interval) as number);
}
export function getLunarDay(date: Date): string { return ''; }
"""
ASSET_SOURCE = """export class Assets {
  public get pieChartData(): BillPieChartItem[] {
    const list: BillPieChartItem[] = [];
    const sourceList = this.curType === AssetChangeType.NET_ASSET
      ? this.assetProcessing.assetGroup.fund : this.assetProcessing.assetGroup.credit;
    const templateRecord = sourceList[0] ?? this.assetProcessing.assetGroup.fund[0] ?? this.assetProcessing.assetGroup.credit[0];
    const legendFieldOrder = Object.keys(templateRecord);
    if (legendFieldOrder.length === 0) return [];
    sourceList.forEach(item => {
      list.push({ label: item.name,
        value: this.curType === AssetChangeType.NET_ASSET ? item.amount : -item.amount,
        resource: item.subType });
    });
    return list;
  }
  @Computed
  public get chartColors(): number[] { return []; }
}
"""


@pytest.mark.parametrize("oracle,mutation,status,failed_stage", [
    (DATE, "good", "pass", None), (DATE, "constant", "fail", "behavior"),
    (DATE, "signed", "fail", "behavior"), (DATE, "unrepaired", "fail", "target"),
    (ASSET, "good", "pass", None), (ASSET, "empty", "fail", "behavior"),
    (ASSET, "wrong_sign", "fail", "behavior"), (ASSET, "unrepaired", "fail", "target"),
])
def test_protocol_reproduces_original_and_rejects_semantic_loss(
        tmp_path: Path, ordinary_node: Path, managed: Path,
        oracle: str, mutation: str, status: str, failed_stage: str | None) -> None:
    relative, marker, scope = REVIEWED_ORACLES[oracle]
    data, task = tmp_path / "data", tmp_path / "task"
    source = data / "app" / relative
    source.parent.mkdir(parents=True)
    original = DATE_SOURCE if oracle == DATE else ASSET_SOURCE
    source.write_text(original, encoding="utf-8")
    if oracle == DATE:
        expression = {"good": "return Math.floor(interval);", "constant": "return 2;",
            "signed": "return Math.floor((new Date(date2).getTime() - new Date(date1).getTime()) / 86400000);",
            "unrepaired": BAD_DATE + " /* still broken */"}[mutation]
        repaired = original.replace(BAD_DATE, expression)
    else:
        repaired = original.replace("Object.keys(templateRecord)", "Object.keys(templateRecord ?? {})")
        if mutation == "empty":
            repaired = repaired.replace("return list;", "return [];")
        elif mutation == "wrong_sign":
            repaired = repaired.replace(": -item.amount", ": item.amount")
        elif mutation == "unrepaired":
            repaired = original.replace("Object.keys(templateRecord)", "Object.keys(templateRecord) /* still broken */")
    log = data / "fault.log"
    log.write_bytes(b"TypeError: synthetic function/getter fault\n")
    prepare_task(data, {"task_id": "synthetic-date-assets", "app": "app",
        "log": {"path": "fault.log", "sha256": digest(log.read_bytes())}}, task)
    work = CandidateWorkspace(task, tmp_path / "work", "domain-check")
    work.start_draft()
    work.apply_text_edits([TextEdit(path=relative, old=original, new=repaired)])
    candidate = work.register_candidate(1)
    adapter = ROOT / oracle
    sha = digest(adapter.read_bytes())
    config = LocalCheckConfig(mode="source", project_root=str(data / "app"), validation_scope=scope,
        mocked_interfaces=REVIEWED_MOCKS.get(oracle, []), commands={stage: LocalCommand(
            argv=[str(ordinary_node), str(adapter), stage], timeout_seconds=10, oracle_sha256=sha) for stage in STAGES},
        independent_oracles={stage: IndependentOracle(path=str(adapter), sha256=sha) for stage in STAGES},
        baseline_target_exit_codes=[9], baseline_fault_pattern=marker, max_check_seconds=90)
    executor = LocalApplicationExecutor(config, work, managed_root=managed)
    result = CheckLedger(work, executor).run(candidate.candidate_id)
    assert result.status == status and result.level == "source_execution"
    evidence = executor.evidence(result.idempotency_key)
    assert evidence["baseline"]["fault_reproduced"] is True
    if failed_stage is not None:
        assert evidence["stages"][-1]["stage"] == failed_stage


@pytest.mark.skipif(os.environ.get("DFX_REAL_EXPORT_ACCEPTANCE") != "1", reason="local exported candidates not requested")
@pytest.mark.parametrize("oracle,relative,protocol", [
    (DATE, "b1-007-calendar1/live1017-retry10", "calendar-days"),
    (ASSET, "b1-005-moneytrack1_2/live1017-case07", "asset-pie"),
])
def test_existing_real_candidates(
        tmp_path: Path, ordinary_node: Path, managed: Path, oracle: str, relative: str, protocol: str) -> None:
    _, marker, scope = REVIEWED_ORACLES[oracle]
    check_exported_candidate(tmp_path, ordinary_node, managed, ROOT / "runs/exports/cases" / relative,
        ROOT / oracle, protocol, scope, marker, REVIEWED_MOCKS.get(oracle, []))
