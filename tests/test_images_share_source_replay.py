"""Private source controls for model normalization and share lifecycle."""

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
IMAGES = "resources/source-validation/post-images/oracle.cjs"
SHARE = "resources/source-validation/share-fingerprint/oracle.cjs"
CASES = {IMAGES: "b3-012-homestay1.0.6", SHARE: "b2-024-webshortdrama1"}


@pytest.mark.parametrize("oracle,mutation,status,failed_stage", [
    (IMAGES, "good", "pass", None), (IMAGES, "unrepaired", "fail", "target"),
    (IMAGES, "empty", "fail", "target"), (IMAGES, "lose_json", "fail", "behavior"),
    (SHARE, "good", "pass", None), (SHARE, "unrepaired", "fail", "target"),
    (SHARE, "constant", "fail", "behavior"), (SHARE, "no_hooks", "fail", "target"),
])
def test_normalization_and_lifecycle_controls(
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
    if oracle == IMAGES:
        before = "this.images = JSON.parse(item.images || '[]');"
        assert original.count(before) == 1
        replacement = """try {
        const parsed = JSON.parse(item.images || '[]');
        this.images = Array.isArray(parsed) ? parsed : (typeof parsed === 'string' ? [parsed] : []);
      } catch { this.images = [item.images]; }"""
        if mutation == "empty":
            replacement = "this.images = [];"
        elif mutation == "lose_json":
            replacement = "this.images = [item.images];"
        repaired = original.replace(before, replacement)
    else:
        before = "new Uint32Array(contentBytes.buffer)"
        assert original.count(before) == 1
        repaired = original.replace(before, "new Uint8Array(contentBytes.buffer)")
        if mutation == "constant":
            repaired = repaired.replace("this.data.contentHash = hash;", "this.data.contentHash = 1;")
        elif mutation == "no_hooks":
            repaired = repaired.replace("this.systemShare.onKnockShare();", "/* missing setup */")
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
        last = receipt["stages"][-1]
        assert last["stage"] == failed_stage
        detail = executor.inspection(result.idempotency_key)["failed_stage"]
        assert ('"post_input"' if oracle == IMAGES else '"share_input"') in detail["output"]["stdout"]["text"]
