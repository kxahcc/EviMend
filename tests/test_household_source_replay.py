"""Synthetic .ets fixtures exercise the narrow actual-source checksum adapter."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

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
from dfx_agent.workspace import CandidateWorkspace, TextEdit

REPOSITORY = Path(__file__).resolve().parents[1]
ADAPTER = REPOSITORY / "resources/source-validation/household-checksum/oracle.cjs"
SOURCE = "commons/lib_foundation/src/main/ets/http/OrderRequestChecksum.ets"
SCOPE = "Execute actual create/verify after Node type erasure; no PRE_ORDER, UI, device or server coverage"
SYNTHETIC_SOURCE = """import { ISubmitOrderRequest } from '../model/IRequest';
export class OrderRequestChecksum {
  static readonly HEADER_NAME: string = 'X-Order-Checksum';
  private static calculate(units: Uint16Array): string {
    let hash: number = 0;
    for (let i = 0; i < units.length; i++) { hash = (hash * 31 + units[i]) >>> 0; }
    return hash.toString(16);
  }
  private static fill(text: string, units: Uint16Array): void {
    for (let i = 0; i < text.length; i++) { units[i] = text.charCodeAt(i); }
  }
  static create(data: ISubmitOrderRequest): string {
    const payload = JSON.stringify(data);
    const buffer = new ArrayBuffer(payload.length);
    const units = new Uint16Array(buffer, 0, payload.length);
    OrderRequestChecksum.fill(payload, units);
    return OrderRequestChecksum.calculate(units);
  }
  static verify(data: ISubmitOrderRequest, checksum: string): boolean {
    const payload = JSON.stringify(data);
    const units = new Uint16Array(payload.length);
    OrderRequestChecksum.fill(payload, units);
    return OrderRequestChecksum.calculate(units) === checksum;
  }
}
"""


@pytest.fixture
def managed(monkeypatch: pytest.MonkeyPatch) -> Path:
    root = REPOSITORY / ".dfx-local"
    if os.name != "nt" or not (root / "state/active.json").exists():
        pytest.skip("source replay requires the existing managed Windows test Job")
    assert json.loads((root / "state/active.json").read_text(encoding="utf-8-sig"))["task"] == "test"
    monkeypatch.setenv("DFX_MANAGED_COMPETITION", "local-v1")
    return root


@pytest.fixture
def native_node(managed: Path) -> Path:
    command = shutil.which("node")
    if command is None:
        pytest.skip("an existing ordinary Node installation is required")
    executable = Path(command).resolve(strict=True)
    if ".codex" in {part.casefold() for part in executable.parts}:
        pytest.skip("a Codex runtime cannot be a product dependency")
    if executable.suffix.casefold() in {".cmd", ".bat", ".ps1"}:
        pytest.skip("the existing Node command must be a native executable")
    return executable


@pytest.fixture
def ordinary_node(native_node: Path) -> Path:
    executable = native_node
    result = subprocess.run([str(executable), "-e",
        "process.exit(typeof require('node:module').stripTypeScriptTypes === 'function' ? 0 : 3)"],
        check=False, capture_output=True, timeout=10)
    if result.returncode == 3:
        pytest.skip("this Node lacks the required official stripTypeScriptTypes; production explicitly rejects it")
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
    return executable


def _setup(tmp_path: Path, node: Path, mutation: str) -> tuple[CandidateWorkspace, Path, LocalCheckConfig]:
    project = tmp_path / "fixture/project"
    target = project / SOURCE
    target.parent.mkdir(parents=True)
    target.write_text(SYNTHETIC_SOURCE, encoding="utf-8")
    log = project.parent / "fault.log"
    log.write_text("RangeError: synthetic TypedArray bounds fixture", encoding="utf-8")
    task = tmp_path / "task"
    prepare_task(project.parent, {"task_id": "synthetic-checksum-replay", "app": "project",
        "log": {"path": "fault.log", "sha256": digest(log.read_bytes())}}, task)
    work = CandidateWorkspace(task, tmp_path / "work", "synthetic-checksum")
    work.start_draft()
    work.apply_text_edits([TextEdit(path=SOURCE, old="new ArrayBuffer(payload.length)",
        new="new ArrayBuffer(payload.length * 2)")])
    if mutation == "constant":
        work.apply_text_edits([TextEdit(path=SOURCE,
            old="return OrderRequestChecksum.calculate(units);", new='return "1e09f387";')])
    elif mutation == "extra_import":
        work.apply_text_edits([TextEdit(path=SOURCE,
            old="export class OrderRequestChecksum", new='import fs from "node:fs";\nexport class OrderRequestChecksum')])
    candidate = work.register_candidate(1)
    source = work.require_registered(candidate.candidate_id)
    oracle = tmp_path / "host-oracle.cjs"
    shutil.copyfile(ADAPTER, oracle)
    sha256 = digest(oracle.read_bytes())
    config = LocalCheckConfig(mode="source", project_root=str(project),
        validation_scope=SCOPE, mocked_interfaces=[], commands={stage: LocalCommand(
            argv=[str(node), str(oracle), stage], timeout_seconds=10, oracle_sha256=sha256) for stage in STAGES},
        independent_oracles={stage: IndependentOracle(path=str(oracle), sha256=sha256) for stage in STAGES},
        baseline_target_exit_codes=[9], baseline_fault_pattern="DFX022_TARGET_RANGE_ERROR", max_check_seconds=90)
    return work, source, config


def _read_output(root: Path, stage: dict[str, Any], key: str) -> str:
    ref = stage.get(key)
    return "" if ref is None else (root / ref["path"]).read_text(encoding="utf-8")


@pytest.mark.parametrize("mutation,status,stages", [
    ("good", "pass", ["build", "target", "behavior", "regression"]),
    ("constant", "fail", ["build", "target", "behavior"]),
    ("extra_import", "fail", ["build"]),
])
def test_adapter_executes_actual_synthetic_candidate_and_rejects_shortcuts(
        tmp_path: Path, ordinary_node: Path, managed: Path,
        mutation: str, status: str, stages: list[str]) -> None:
    workspace, source, config = _setup(tmp_path, ordinary_node, mutation)
    executor = LocalApplicationExecutor(config, workspace, managed_root=managed)
    ledger = CheckLedger(workspace, executor)
    result = ledger.run(source.parent.name)
    assert result.status == status and result.level == "source_execution"
    evidence = executor.evidence(result.idempotency_key)
    assert executor.root is not None
    receipt_root = executor.root / result.idempotency_key
    assert evidence["baseline"]["fault_reproduced"] is True
    assert evidence["baseline"]["stages"][-1]["exit_code"] == 9
    assert "DFX022_TARGET_RANGE_ERROR" in _read_output(receipt_root, evidence["baseline"]["stages"][-1], "stderr")
    assert [row["stage"] for row in evidence["stages"]] == stages
    assert evidence["source_verification"] == status
    assert evidence["application_verification"] == "not_run" and evidence["application_success"] is False
    original_hash = digest(local_evidence_path(Path(config.project_root) / SOURCE).read_bytes())
    candidate_hash = digest(local_evidence_path(source / SOURCE).read_bytes())
    assert original_hash != candidate_hash
    for row in evidence["baseline"]["stages"]:
        output = json.loads(_read_output(receipt_root, row, "stdout").splitlines()[0])
        assert output["source_sha256"] == original_hash
    for row in evidence["stages"]:
        if row["stage"] != "build" or mutation != "extra_import":
            output = json.loads(_read_output(receipt_root, row, "stdout").splitlines()[0])
            assert output["source_sha256"] == candidate_hash
        assert row["source_binding_verified"] and row["oracle_binding_verified"]
    if mutation == "constant":
        assert "fixed checksum mismatch" in _read_output(receipt_root, evidence["stages"][-1], "stderr")
    elif mutation == "extra_import":
        assert "UnsupportedSourceImport" in _read_output(receipt_root, evidence["stages"][-1], "stderr")
    assert ledger.run(source.parent.name) == result and ledger.completed(source.parent.name) == result
    assert (Path(config.project_root) / SOURCE).read_text(encoding="utf-8") == SYNTHETIC_SOURCE


def test_missing_official_type_stripper_is_an_explicit_refusal(
        tmp_path: Path, native_node: Path) -> None:
    fixture = tmp_path / "source.ets"
    fixture.write_text(SYNTHETIC_SOURCE, encoding="utf-8")
    probe = tmp_path / "unsupported.cjs"
    probe.write_text("const fs = require('node:fs'); const assert = require('node:assert/strict');\n"
        + "const oracle = require(" + json.dumps(str(ADAPTER)) + ");\n"
        + "assert.throws(() => oracle.compileCandidate(fs.readFileSync(process.argv[2]), process.argv[2], null), "
        + "/UnsupportedTypeScriptRuntime/);\n", encoding="utf-8")
    result = subprocess.run([str(native_node), str(probe), str(fixture)],
        capture_output=True, check=False, timeout=10)
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
