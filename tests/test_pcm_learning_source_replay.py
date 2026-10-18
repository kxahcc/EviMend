"""Private prepared-source controls before paid PCM/learning-order runs."""

from __future__ import annotations

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
from dfx_agent.pilot_preparation import REVIEWED_MOCKS, REVIEWED_ORACLES
from dfx_agent.task import digest
from dfx_agent.verification import CheckLedger
from dfx_agent.workspace import CandidateWorkspace

ROOT = Path(__file__).resolve().parents[1]
PCM = "resources/source-validation/pcm-frames/oracle.cjs"
LEARNING = "resources/source-validation/learning-order/oracle.cjs"
CASES = {PCM: "b3-021-audioediting1", LEARNING: "b3-019-exam1"}


@pytest.mark.parametrize("oracle,mutation,status,failed_stage", [
    (PCM, "good", "pass", None), (PCM, "unrepaired", "fail", "target"),
    (PCM, "silent", "fail", "target"), (PCM, "channel_sum", "fail", "behavior"),
    (LEARNING, "good", "pass", None), (LEARNING, "unrepaired", "fail", "target"),
    (LEARNING, "truncate", "fail", "target"), (LEARNING, "ignore_links", "fail", "behavior"),
])
def test_original_and_control_candidates(
        tmp_path: Path, ordinary_node: Path, managed: Path,
        oracle: str, mutation: str, status: str, failed_stage: str | None) -> None:
    task = ROOT / ".dfx-local/tmp/reviewed" / CASES[oracle]
    if not task.is_dir():
        pytest.skip("optional private prepared data are unavailable")
    relative, marker, scope = REVIEWED_ORACLES[oracle]
    work = CandidateWorkspace(task, tmp_path / "work", "domain-control")
    work.start_draft()
    target = local_evidence_path(work.draft / relative)
    original = target.read_text(encoding="utf-8-sig")
    if oracle == PCM:
        assert original.count("frameIdx <= totalFrames") == 1
        repaired = original.replace("frameIdx <= totalFrames", "frameIdx < Math.floor(totalFrames)")
        if mutation == "silent":
            repaired = repaired.replace("amplitudes.push(avgAmp)", "amplitudes.push(0)")
        elif mutation == "channel_sum":
            repaired = repaired.replace("totalAmp / this.channelLayout", "totalAmp")
    else:
        anchor = "    let current: ItemDetail | undefined = undefined;"
        assert original.count(anchor) == 1
        repaired = original.replace(anchor, "    if (target.some(item => item.id === currentId)) return;\n" + anchor)
        if mutation == "truncate":
            repaired = repaired.replace("this.appendLearningOrder(nextId, source, target);", "return;")
        elif mutation == "ignore_links":
            repaired = repaired.replace("let nextId: string = current.nextId;", "let nextId: string = '';")
    if mutation == "unrepaired":
        repaired = original + "\n/* unchanged fault */\n"
    assert repaired != original
    target.write_text(repaired, encoding="utf-8")
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
    assert result.status == status and result.level == "source_execution"
    if failed_stage is not None:
        assert receipt["stages"][-1]["stage"] == failed_stage
        field = '"pcm_input"' if oracle == PCM else '"learning_input"'
        detail = executor.inspection(result.idempotency_key)["failed_stage"]
        assert field in detail["output"]["stdout"]["text"], "failed source checks must expose a reproducible input"
