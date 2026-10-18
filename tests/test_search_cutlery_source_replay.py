"""Private actual-source controls for search and order callbacks."""

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
CASES = {"exam-search": "b3-014-driverlicenseexam1.0.7", "fitness-search": "b3-015-fitnesscenter1.0.3", "cutlery-count": "b3-020-teadrinkorders1"}


@pytest.mark.parametrize("protocol,mutation", [(name, mutation) for name in CASES for mutation in ("good", "unrepaired", "degraded")])
def test_search_and_order_controls(
        tmp_path: Path, ordinary_node: Path, managed: Path, protocol: str, mutation: str) -> None:
    task = ROOT / ".dfx-local/tmp/reviewed" / CASES[protocol]
    if not task.is_dir():
        pytest.skip("optional private prepared data are unavailable")
    oracle = f"resources/source-validation/{protocol}/oracle.cjs"
    relative, marker, scope = REVIEWED_ORACLES[oracle]
    # Unit controls need only this exact source file; production admission/model runs
    # still bind the complete reviewed task, not this reduced component fixture.
    data, component = tmp_path / "data", tmp_path / "component"
    fixture = data / "app" / relative
    local_evidence_path(fixture.parent).mkdir(parents=True)
    local_evidence_path(fixture).write_bytes(local_evidence_path(task / "source" / relative).read_bytes())
    log = data / "fault.log"
    log.write_bytes((task / "crash.log").read_bytes())
    prepare_task(data, {"task_id": "source-oracle-controls", "app": "app",
        "log": {"path": "fault.log", "sha256": digest(log.read_bytes())}}, component)
    work = CandidateWorkspace(component, tmp_path / "work", "domain-control")
    work.start_draft()
    target = local_evidence_path(work.draft / relative)
    original = target.read_text(encoding="utf-8-sig")
    if protocol == "exam-search":
        before = "new RegExp(searchCondition)"
        replacement = "{ test: (text: string) => text.includes(searchCondition) }" if mutation == "good" else "{ test: () => true }"
        assert original.count(before) == 1
        repaired = original.replace(before, replacement)
    elif protocol == "fitness-search":
        before = "const storeMatcher = new RegExp(`(?<matched)${value}`, 'i');"
        assert original.count(before) == 1
        repaired = original.replace(before, "").replace("storeMatcher.test(store.address)", "store.address.toLowerCase().includes(value.toLowerCase())")
        if mutation == "degraded":
            repaired = repaired.replace("store.address.toLowerCase().includes(value.toLowerCase())", "true")
    else:
        before = "new Array<string>(cutleryCount)"
        assert original.count(before) == 1
        repaired = original.replace(before, "new Array<string>(Math.max(0, cutleryCount))" if mutation == "good" else "new Array<string>(0)")
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
    receipt = executor.evidence(result.idempotency_key)
    if receipt["baseline"]["fault_reproduced"] is not True:
        failure = executor.inspection(result.idempotency_key)["failed_stage"]
        pytest.fail(failure["output"]["stderr"]["text"])
    assert result.status == ("pass" if mutation == "good" else "fail")
    if mutation != "good":
        assert receipt["stages"][-1]["stage"] == ("behavior" if protocol == "cutlery-count" and mutation == "degraded" else "target")
