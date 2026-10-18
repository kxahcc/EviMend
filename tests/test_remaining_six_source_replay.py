"""Private component controls for the six remaining reviewed inputs."""

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
from dfx_agent.task import digest, prepare_task
from dfx_agent.verification import CheckLedger
from dfx_agent.workspace import CandidateWorkspace

ROOT = Path(__file__).resolve().parents[1]
CASES = [
    ("video-share", "b2-011-childreneducation1"), ("order-logistics", "b2-001-comprehensivemall1"),
    ("news-author", "b2-002-comprehensivenews1"), ("news-author", "b2-003-comprehensivenews1"),
    ("calendar-selection", "b2-005-moneytrack1"), ("cache-read", "b3-024-webshortdrama1"),
]


@pytest.mark.parametrize("protocol,case,mutation", [(p, c, m) for p, c in CASES for m in ("good", "unrepaired", "degraded")])
def test_remaining_source_controls(
        tmp_path: Path, ordinary_node: Path, managed: Path, protocol: str, case: str, mutation: str) -> None:
    task = ROOT / ".dfx-local/tmp/reviewed" / case
    if not task.is_dir():
        pytest.skip("optional private prepared data are unavailable")
    oracle = f"resources/source-validation/{protocol}/oracle.cjs"
    relative, marker, scope = REVIEWED_ORACLES[oracle]
    data, component = tmp_path / "data", tmp_path / "component"
    fixture = local_evidence_path(data / "app" / relative)
    fixture.parent.mkdir(parents=True)
    fixture.write_bytes(local_evidence_path(task / "source" / relative).read_bytes())
    log = data / "fault.log"
    log.write_bytes((task / "crash.log").read_bytes())
    prepare_task(data, {"task_id": "source-oracle-controls", "app": "app",
        "log": {"path": "fault.log", "sha256": digest(log.read_bytes())}}, component)
    work = CandidateWorkspace(component, tmp_path / "work", "domain-control")
    work.start_draft()
    target = local_evidence_path(work.draft / relative)
    original = target.read_text(encoding="utf-8-sig")
    if protocol == "video-share":
        assert original.count("const picker = shareFactory({") == 1
        repaired = original.replace("const picker = shareFactory({", "const picker = new shareFactory({")
        if mutation == "degraded":
            repaired = repaired.replace("picker.show();", "/* dropped show */")
    elif protocol == "news-author":
        before = "this.newsFeedVM.articleDetails.author.authorNickName"
        assert original.count(before) == 1
        repaired = original.replace(before, "this.newsFeedVM.articleDetails.author?.authorNickName ?? ''" if mutation == "good" else "''")
    elif protocol == "order-logistics":
        before = "if (this.vm.orderItem?.logisticInfo)"
        assert original.count(before) == 1
        repaired = original.replace(before, "if (this.vm.logisticInfo)" if mutation == "good" else "if (false)")
    elif protocol == "calendar-selection":
        before = "const target = this.vm.dataProcessing.list!.find((item) => item.dateStr === dateStr)!;"
        assert original.count(before) == 1
        repaired = original.replace(before, "const target = { dateStr: dateStr };")
        if mutation == "degraded":
            repaired = repaired.replace("this.vm.getSelectedDateData(target.dateStr);", "/* dropped selection refresh */")
    else:
        assert original.count("new Int32Array(buffer)") == 1
        repaired = original.replace("new Int32Array(buffer)", "new Int32Array(buffer, 0, Math.floor(buffer.byteLength / Int32Array.BYTES_PER_ELEMENT))")
        if mutation == "degraded":
            repaired = repaired.replace("FileUtils.safeClose(file);", "/* dropped close */")
    if mutation == "unrepaired":
        repaired = original + "\n/* unchanged fault */\n"
    target.write_text(repaired, encoding="utf-8")
    candidate = work.register_candidate(1)
    adapter = ROOT / oracle
    sha = digest(adapter.read_bytes())
    config = LocalCheckConfig(mode="source", project_root=str(data / "app"), validation_scope=scope,
        mocked_interfaces=REVIEWED_MOCKS[oracle], commands={stage: LocalCommand(argv=[str(ordinary_node), str(adapter), stage],
            timeout_seconds=10, oracle_sha256=sha) for stage in STAGES},
        independent_oracles={stage: IndependentOracle(path=str(adapter), sha256=sha) for stage in STAGES},
        baseline_target_exit_codes=[9], baseline_fault_pattern=marker, max_check_seconds=90)
    executor = LocalApplicationExecutor(config, work, managed_root=managed)
    result = CheckLedger(work, executor).run(candidate.candidate_id)
    evidence = executor.evidence(result.idempotency_key)
    if evidence["baseline"]["fault_reproduced"] is not True:
        pytest.fail(executor.inspection(result.idempotency_key)["failed_stage"]["output"]["stderr"]["text"])
    assert result.status == ("pass" if mutation == "good" else "fail")
    if mutation != "good":
        stage = "behavior" if mutation == "degraded" and protocol in {"news-author", "order-logistics"} else "target"
        assert evidence["stages"][-1]["stage"] == stage
