"""Construction/lifecycle source checks reject repairs which only suppress the fault."""

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
FACTORY = "resources/source-validation/accounting-formatter/oracle.cjs"
BOOK = "resources/source-validation/book-lifecycle/oracle.cjs"
FACTORY_SOURCE = """class Chart {
private _formatterBuilder: (referenceDate: string) => IAxisValueFormatter = MyAxisValueFormatter;
}
class MyAxisValueFormatter implements IAxisValueFormatter {
  referenceDate: string;
  constructor(referenceDate: string) { this.referenceDate = referenceDate; }
  getFormattedValue(value: number): string {
    const date = dayjs(this.referenceDate).add(value - 1, 'day');
    return date.format('M.D');
  }
}
"""
BOOK_SOURCE = """class Page {
  aboutToAppear(): void {
    const p = TCRouter.getParams<CategoryInfo>(Constants.BOOK_LIST_ROUTE);
    this.category = p?.category;
    this.books!.load(p?.books ?? []);
    this.books = new LazyDataVM<BookInfo>(p?.books);
  }
  build() {}
}
"""
BOOK_DATA = """export class LazyDataSource<T> implements IDataSource {}
export class LazyDataVM<T> extends LazyDataSource<T> {
  private dataArray: T[] = [];
  constructor(data?: T[]) { super(); if (data) this.dataArray = data; }
  totalCount(): number { return this.dataArray.length; }
  getData(index: number): T { return this.dataArray[index]; }
  getAllData(): T[] { return this.dataArray; }
  load(data: T[]): void { this.dataArray = [...data]; }
}
@ObservedV2
export class ObservedArray<T> extends Array<T> {}
"""


@pytest.mark.parametrize("oracle,mutation,status,failed_stage", [
    (FACTORY, "good", "pass", None), (FACTORY, "constant", "fail", "behavior"),
    (FACTORY, "offset", "fail", "target"), (FACTORY, "unrepaired", "fail", "target"),
    (BOOK, "good", "pass", None), (BOOK, "empty", "fail", "target"),
    (BOOK, "category", "fail", "behavior"), (BOOK, "unrepaired", "fail", "target"),
])
def test_checks_reproduce_and_reject_semantic_loss(
        tmp_path: Path, ordinary_node: Path, managed: Path,
        oracle: str, mutation: str, status: str, failed_stage: str | None) -> None:
    relative, marker, scope = REVIEWED_ORACLES[oracle]
    data, task = tmp_path / "data", tmp_path / "task"
    source = data / "app" / relative
    source.parent.mkdir(parents=True)
    original = FACTORY_SOURCE if oracle == FACTORY else BOOK_SOURCE
    source.write_text(original, encoding="utf-8")
    if oracle == FACTORY:
        repaired = original.replace("= MyAxisValueFormatter;", "= (referenceDate: string) => new MyAxisValueFormatter(referenceDate);")
        if mutation == "constant":
            repaired = repaired.replace("new MyAxisValueFormatter(referenceDate)", "new MyAxisValueFormatter('2026-10-01')")
        elif mutation == "offset":
            repaired = repaired.replace("value - 1", "value")
    else:
        dependency = data / "app/components/swiper_card/src/main/ets/model/LazyDataVM.ets"
        dependency.parent.mkdir(parents=True)
        dependency.write_text(BOOK_DATA, encoding="utf-8")
        repaired = original.replace("    this.books!.load(p?.books ?? []);\n", "").replace(
            "    this.books = new LazyDataVM<BookInfo>(p?.books);",
            "    this.books = new LazyDataVM<BookInfo>(p?.books);\n    this.books.load(p?.books ?? []);")
        if mutation == "empty":
            repaired = repaired.replace("(p?.books)", "([])").replace("(p?.books ?? [])", "([])")
        elif mutation == "category":
            repaired = repaired.replace("this.category = p?.category;", "this.category = 'novels';")
    if mutation == "unrepaired":
        repaired = original + "\n/* unchanged fault */\n"
    log = data / "fault.log"
    log.write_bytes(b"TypeError: synthetic construction/lifecycle fault\n")
    prepare_task(data, {"task_id": "synthetic-factory-lifecycle", "app": "app",
        "log": {"path": "fault.log", "sha256": digest(log.read_bytes())}}, task)
    work = CandidateWorkspace(task, tmp_path / "work", "domain-check")
    work.start_draft()
    work.apply_text_edits([TextEdit(path=relative, old=original, new=repaired)])
    candidate = work.register_candidate(1)
    adapter = ROOT / oracle
    sha = digest(adapter.read_bytes())
    config = LocalCheckConfig(mode="source", project_root=str(data / "app"), validation_scope=scope,
        mocked_interfaces=REVIEWED_MOCKS[oracle], commands={stage: LocalCommand(
            argv=[str(ordinary_node), str(adapter), stage], timeout_seconds=10, oracle_sha256=sha) for stage in STAGES},
        independent_oracles={stage: IndependentOracle(path=str(adapter), sha256=sha) for stage in STAGES},
        baseline_target_exit_codes=[9], baseline_fault_pattern=marker, max_check_seconds=90)
    executor = LocalApplicationExecutor(config, work, managed_root=managed)
    result = CheckLedger(work, executor).run(candidate.candidate_id)
    assert result.status == status and result.level == "source_execution"
    receipt = executor.evidence(result.idempotency_key)
    assert receipt["baseline"]["fault_reproduced"] is True
    if failed_stage is not None:
        assert receipt["stages"][-1]["stage"] == failed_stage


@pytest.mark.skipif(os.environ.get("DFX_REAL_EXPORT_ACCEPTANCE") != "1", reason="local exported candidates not requested")
@pytest.mark.parametrize("oracle,relative,protocol", [
    (FACTORY, "b1-002-accountingapp1/live1017-case01", "accounting-formatter"),
    (BOOK, "b1-004-bookread2/live1017-retry12", "book-lifecycle"),
])
def test_existing_real_candidates(
        tmp_path: Path, ordinary_node: Path, managed: Path, oracle: str, relative: str, protocol: str) -> None:
    _, marker, scope = REVIEWED_ORACLES[oracle]
    check_exported_candidate(tmp_path, ordinary_node, managed, ROOT / "runs/exports/cases" / relative,
        ROOT / oracle, protocol, scope, marker, REVIEWED_MOCKS[oracle])
