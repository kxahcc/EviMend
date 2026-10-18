"""Local frozen-source regression controls and opt-in posthoc exported evidence."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from test_enterprise_source_replay import check_exported_candidate
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
from dfx_agent.pilot_preparation import REVIEWED_MOCKS, REVIEWED_ORACLES
from dfx_agent.task import digest
from dfx_agent.verification import CheckLedger
from dfx_agent.workspace import CandidateWorkspace, _files

ROOT = Path(__file__).resolve().parents[1]
EXPRESS = "resources/source-validation/express-template/oracle.cjs"
NOTES = "resources/source-validation/notes-serialization/oracle.cjs"
CASES = {EXPRESS: "b1-010-express1/live1017-case06", NOTES: "b1-006-notes1/live1017-case09"}


@pytest.mark.parametrize("oracle,mutation,failed_stage", [
    (EXPRESS, "unrepaired", "target"), (EXPRESS, "goods", "behavior"), (EXPRESS, "payment", "behavior"),
    (NOTES, "unrepaired", "target"), (NOTES, "persistence", "behavior"), (NOTES, "update_cycle", "behavior"),
])
def test_local_frozen_source_negative_controls(
        tmp_path: Path, ordinary_node: Path, managed: Path, oracle: str, mutation: str, failed_stage: str) -> None:
    result_root = ROOT / "runs/exports/cases" / CASES[oracle]
    if not result_root.is_dir():
        pytest.skip("optional private frozen data are not available in this checkout")
    relative, marker, scope = REVIEWED_ORACLES[oracle]
    task = result_root / "input-task"
    work = CandidateWorkspace(task, tmp_path / "work", "negative-check")
    work.start_draft()
    if mutation != "unrepaired":
        for name, path in _files(result_root / "repaired-source").items():
            destination = local_evidence_path(work.draft / name)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(local_evidence_path(path).read_bytes())
    target = local_evidence_path(work.draft / relative)
    source = target.read_text(encoding="utf-8-sig")
    replacements = {
        "goods": ("count: 1,", "count: 0,"),
        "payment": ("this.selectedPayMethod = payMethod;", "this.selectedPayMethod = { text: 'wrong', value: 1 };"),
        "persistence": ("JSON.stringify(note).replace", "JSON.stringify({ id: note.id }).replace"),
        "update_cycle": ("this.notes.splice(index, 1, note);", "this.notes.splice(index, 1, note); note.noteService = this;"),
    }
    if mutation != "unrepaired":
        before, after = replacements[mutation]
        assert source.count(before) == 1
        source = source.replace(before, after)
    else:
        source += "\n/* unchanged fault */\n"
    target.write_text(source, encoding="utf-8")
    candidate = work.register_candidate(1)
    adapter = ROOT / oracle
    sha = digest(adapter.read_bytes())
    config = LocalCheckConfig(mode="source", project_root=str(task / "source"), validation_scope=scope,
        mocked_interfaces=REVIEWED_MOCKS[oracle], commands={stage: LocalCommand(
            argv=[str(ordinary_node), str(adapter), stage], timeout_seconds=10, oracle_sha256=sha) for stage in STAGES},
        independent_oracles={stage: IndependentOracle(path=str(adapter), sha256=sha) for stage in STAGES},
        baseline_target_exit_codes=[9], baseline_fault_pattern=marker, max_check_seconds=90)
    executor = LocalApplicationExecutor(config, work, managed_root=managed)
    result = CheckLedger(work, executor).run(candidate.candidate_id)
    receipt = executor.evidence(result.idempotency_key)
    assert receipt["baseline"]["fault_reproduced"] is True
    assert result.status == "fail" and receipt["stages"][-1]["stage"] == failed_stage


@pytest.mark.skipif(os.environ.get("DFX_REAL_EXPORT_ACCEPTANCE") != "1", reason="local exported candidates not requested")
@pytest.mark.parametrize("oracle,protocol", [(EXPRESS, "express-template"), (NOTES, "notes-serialization")])
def test_existing_real_candidates(
        tmp_path: Path, ordinary_node: Path, managed: Path, oracle: str, protocol: str) -> None:
    _, marker, scope = REVIEWED_ORACLES[oracle]
    check_exported_candidate(tmp_path, ordinary_node, managed, ROOT / "runs/exports/cases" / CASES[oracle],
        ROOT / oracle, protocol, scope, marker, REVIEWED_MOCKS[oracle])
