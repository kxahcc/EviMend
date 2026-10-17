"""Synthetic .ets methods exercise the registered tourist module, not its namesake."""

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
ADAPTER = REPOSITORY / "resources/source-validation/tourist-phone/oracle.cjs"
SOURCE = "components/module_base/src/main/ets/util/CommonUtil.ets"
NAMESAKE = "commons/common/src/main/ets/utils/CommonUtil.ets"
SCOPE = "Actual module_base CommonUtil phone rule and existing utility methods after Node type erasure; no UI"
BAD_PATTERN = "'^1(?3|4|5|7|8|9)\\\\d{9}$'"
GOOD_PATTERN = "'^1(3|4|5|7|8|9)\\\\d{9}$'"
SYNTHETIC_SOURCE = """export class CommonUtil {
  static padWithZeros(num: number) {
    if (num) { return num.toString().padStart(2, '0'); }
    return '00';
  }
  static verifyPhoneNo(phone: string) {
    const phoneReg = new RegExp(""" + BAD_PATTERN + """);
    return phoneReg.test(phone);
  }
  static verifyCardNo(cardNo: string) {
    const cardNoReg = /^[1-9]\\d{5}(18|19|20)\\d{2}((0[1-9])|(1[0-2]))(([0-2][1-9])|10|20|30|31)\\d{3}[0-9Xx]$/;
    return cardNoReg.test(cardNo);
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
def ordinary_node(managed: Path) -> Path:
    command = shutil.which("node")
    if command is None:
        pytest.skip("an existing ordinary Node installation is required")
    executable = Path(command).resolve(strict=True)
    if ".codex" in {part.casefold() for part in executable.parts} or executable.suffix.lower() != ".exe":
        pytest.skip("a native ordinary Node executable is required")
    probe = subprocess.run([str(executable), "-e",
        "process.exit(typeof require('node:module').stripTypeScriptTypes === 'function' ? 0 : 3)"],
        capture_output=True, timeout=10, check=False)
    if probe.returncode == 3:
        pytest.skip("Node lacks the required official type stripping API; production explicitly refuses")
    assert probe.returncode == 0, probe.stderr.decode("utf-8", errors="replace")
    return executable


def _setup(tmp_path: Path, node: Path, mutation: str) -> tuple[CandidateWorkspace, Path, LocalCheckConfig]:
    project = tmp_path / "fixture/project"
    for relative, content in ((SOURCE, SYNTHETIC_SOURCE),
                              (NAMESAKE, SYNTHETIC_SOURCE.replace(BAD_PATTERN, GOOD_PATTERN))):
        target = local_evidence_path(project / relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    log = project.parent / "fault.log"
    log.write_text("SyntaxError: synthetic invalid RegExp group", encoding="utf-8")
    task = tmp_path / "task"
    prepare_task(project.parent, {"task_id": "synthetic-tourist-replay", "app": "project",
        "log": {"path": "fault.log", "sha256": digest(log.read_bytes())}}, task)
    work = CandidateWorkspace(task, tmp_path / "work", "synthetic-tourist")
    work.start_draft()
    if mutation == "namesake_only":
        work.apply_text_edits([TextEdit(path=NAMESAKE, old="return '00';", new="return '000';")])
    else:
        work.apply_text_edits([TextEdit(path=SOURCE, old=BAD_PATTERN, new=GOOD_PATTERN)])
        if mutation == "accept_all":
            work.apply_text_edits([TextEdit(path=SOURCE, old="return phoneReg.test(phone);", new="return true;")])
        elif mutation == "utility_regression":
            work.apply_text_edits([TextEdit(path=SOURCE,
                old="return num.toString().padStart(2, '0');", new="return '00';")])
    candidate = work.register_candidate(1)
    source = work.require_registered(candidate.candidate_id)
    oracle = tmp_path / "tourist-oracle.cjs"
    shutil.copyfile(ADAPTER, oracle)
    sha = digest(oracle.read_bytes())
    config = LocalCheckConfig(mode="source", project_root=str(project), validation_scope=SCOPE,
        mocked_interfaces=[], commands={stage: LocalCommand(argv=[str(node), str(oracle), stage],
            oracle_sha256=sha, timeout_seconds=10) for stage in STAGES}, independent_oracles={stage:
            IndependentOracle(path=str(oracle), sha256=sha) for stage in STAGES},
        baseline_target_exit_codes=[9], baseline_fault_pattern="DFX016_TARGET_REGEX_SYNTAX_ERROR",
        max_check_seconds=90)
    return work, source, config


def _output(root: Path, stage: dict[str, Any], name: str) -> str:
    ref = stage.get(name)
    return "" if ref is None else local_evidence_path(root / ref["path"]).read_text(encoding="utf-8")


@pytest.mark.parametrize("mutation,status,stages", [
    ("good", "pass", ["build", "target", "behavior", "regression"]),
    ("accept_all", "fail", ["build", "target", "behavior"]),
    ("utility_regression", "fail", ["build", "target", "behavior", "regression"]),
    ("namesake_only", "fail", ["build", "target"]),
])
def test_actual_registered_class_replay_rejects_shortcuts_and_wrong_namesake(
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
    original = evidence["baseline"]["stages"]
    assert original[0]["exit_code"] == 0 and original[1]["exit_code"] == 9
    original_error = _output(receipt_root, original[1], "stderr")
    assert "DFX016_TARGET_REGEX_SYNTAX_ERROR" in original_error and "SyntaxError" in original_error
    assert original[1]["reason"] == "exited" and original[1]["subtree_stopped"] is True
    assert [row["stage"] for row in evidence["stages"]] == stages
    assert evidence["source_verification"] == status
    assert evidence["application_verification"] == "not_run" and evidence["application_success"] is False
    for rows, selected_source in ((original, Path(config.project_root)), (evidence["stages"], source)):
        sha = digest(local_evidence_path(selected_source / SOURCE).read_bytes())
        for row in rows:
            observed = json.loads(_output(receipt_root, row, "stdout").splitlines()[0])
            assert observed["source_sha256"] == sha and observed["stage"] == row["stage"]
            assert row["source_binding_verified"] and row["oracle_binding_verified"]
    if mutation == "accept_all":
        assert "phone validation rule changed" in _output(receipt_root, evidence["stages"][-1], "stderr")
    elif mutation == "utility_regression":
        assert "padWithZeros regression" in _output(receipt_root, evidence["stages"][-1], "stderr")
    elif mutation == "namesake_only":
        assert "DFX016_TARGET_REGEX_SYNTAX_ERROR" in _output(receipt_root, evidence["stages"][-1], "stderr")
    assert ledger.run(source.parent.name) == result and ledger.completed(source.parent.name) == result
    assert local_evidence_path(Path(config.project_root) / SOURCE).read_text(encoding="utf-8") == SYNTHETIC_SOURCE
