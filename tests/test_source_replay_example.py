"""A portable JS example executes candidate bytes through protected source checks."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any

import pytest

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
EXAMPLE = REPOSITORY / "resources/source-validation/example"
SCOPE = "Execute actual CommonJS lastValue source against replay, normal and boundary inputs; no platform APIs"


@pytest.fixture
def ordinary_node() -> Path:
    command = shutil.which("node")
    if command is None:
        pytest.skip("the portable JS example requires an existing ordinary Node.js installation")
    executable = Path(command).resolve(strict=True)
    if ".codex" in {part.casefold() for part in executable.parts}:
        pytest.skip("the example must not depend on a Codex-bundled Node.js runtime")
    if executable.suffix.casefold() in {".cmd", ".bat", ".ps1"}:
        pytest.skip("the existing Node command is a shell wrapper rather than a native executable")
    return executable


@pytest.fixture
def managed(monkeypatch: pytest.MonkeyPatch) -> Path:
    root = REPOSITORY / ".dfx-local"
    if os.name != "nt" or not (root / "state/active.json").exists():
        pytest.skip("source execution requires the existing managed Windows test Job")
    lease = json.loads((root / "state/active.json").read_text(encoding="utf-8-sig"))
    assert lease["task"] == "test"
    monkeypatch.setenv("DFX_MANAGED_COMPETITION", "local-v1")
    return root


def _setup(tmp_path: Path, node: Path, patch: str) -> tuple[CandidateWorkspace, Path, LocalCheckConfig]:
    example = tmp_path / "example"
    shutil.copytree(EXAMPLE, example)
    log = example / "fault.log"
    # Static fixture input is not an application observation. The executor
    # independently runs the actual original program to prove reproduction.
    log.write_text("RangeError: LastValueIndexFault\n    at lastValue (src/last-value.js)\n", encoding="utf-8")
    task = tmp_path / "task"
    prepare_task(example, {"task_id": "node-source-example", "app": "project",
                           "log": {"path": "fault.log", "sha256": digest(log.read_bytes())}}, task)
    assert {row["path"] for row in json.loads((task / "source-manifest.json").read_bytes())} == {
        "src/last-value.js"}
    work = CandidateWorkspace(task, tmp_path / "work", "node-source-replay")
    work.start_draft()
    work.apply_text_edits([TextEdit(path="src/last-value.js", old="const index = values.length;", new=patch)])
    candidate = work.register_candidate(1)
    source = work.require_registered(candidate.candidate_id)
    oracle = example / "oracle.cjs"
    sha256 = digest(oracle.read_bytes())
    config = LocalCheckConfig(mode="source", project_root=str(example / "project"),
        validation_scope=SCOPE, mocked_interfaces=[], commands={stage: LocalCommand(
            argv=[str(node), str(oracle), stage], oracle_sha256=sha256, timeout_seconds=10) for stage in STAGES},
        independent_oracles={stage: IndependentOracle(path=str(oracle), sha256=sha256) for stage in STAGES},
        baseline_target_exit_codes=[7], baseline_fault_pattern="LastValueIndexFault", max_check_seconds=90)
    return work, source, config


def _stage_source_hash(root: Path, row: dict[str, Any]) -> str:
    output = row["stdout"]
    assert output is not None
    first_line = (root / output["path"]).read_text(encoding="utf-8").splitlines()[0]
    observed = json.loads(first_line)
    assert observed["stage"] == row["stage"]
    return observed["source_sha256"]


@pytest.mark.parametrize("patch,expected,stages", [
    ("const index = values.length - 1;", "pass", ["build", "target", "behavior", "regression"]),
    ("const index = 0;", "fail", ["build", "target", "behavior"]),
])
def test_node_replay_executes_real_original_and_candidate_in_each_stage(
        tmp_path: Path, ordinary_node: Path, managed: Path,
        patch: str, expected: str, stages: list[str]) -> None:
    workspace, source, config = _setup(tmp_path, ordinary_node, patch)
    executor = LocalApplicationExecutor(config, workspace, managed_root=managed)
    ledger = CheckLedger(workspace, executor)
    result = ledger.run(source.parent.name)
    assert result.status == expected and result.level == "source_execution" and result.execution_scope == "source"
    evidence = executor.evidence(result.idempotency_key)
    assert executor.root is not None
    receipt_root = executor.root / result.idempotency_key
    baseline = evidence["baseline"]
    assert baseline["fault_reproduced"] is True
    assert [row["stage"] for row in baseline["stages"]] == ["build", "target"]
    original_path = Path(config.project_root) / "src/last-value.js"
    original_hash = digest(original_path.read_bytes())
    assert all(_stage_source_hash(receipt_root, row) == original_hash for row in baseline["stages"])
    target = baseline["stages"][-1]
    assert target["exit_code"] == 7
    target_error = (receipt_root / target["stderr"]["path"]).read_text(encoding="utf-8")
    assert "RangeError: LastValueIndexFault" in target_error and "last-value.js" in target_error
    candidate_hash = digest((source / "src/last-value.js").read_bytes())
    assert candidate_hash != original_hash
    assert [row["stage"] for row in evidence["stages"]] == stages
    assert all(_stage_source_hash(receipt_root, row) == candidate_hash for row in evidence["stages"])
    assert all(row["source_binding_verified"] and row["oracle_binding_verified"] for row in evidence["stages"])
    assert evidence["source_verification"] == expected
    assert evidence["validation_scope"] == SCOPE and evidence["mocked_interfaces"] == []
    assert evidence["application_verification"] == "not_run"
    assert evidence["application_success"] is False and evidence["accepted"] is False
    if expected == "fail":
        assert [row["status"] for row in evidence["stages"]] == ["pass", "pass", "fail"]
        failure = evidence["stages"][-1]
        assert "last element semantics changed" in (receipt_root / failure["stderr"]["path"]).read_text(encoding="utf-8")
    assert ledger.run(source.parent.name) == result and ledger.completed(source.parent.name) == result
    assert original_path.read_bytes() == (EXAMPLE / "project/src/last-value.js").read_bytes()
