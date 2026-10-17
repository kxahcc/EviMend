"""Synthetic DataManager executes its own Map and actual candidate query methods."""

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
ADAPTER = REPOSITORY / "resources/source-validation/office-tasks/oracle.cjs"
SOURCE = "scenes/agency/src/main/ets/mock/DataManager.ets"
SCOPE = "Actual DataManager Map/query methods after Node type erasure; no preferences persistence or UI"
BAD_EXPRESSION = "new RegExp('(' + keyword, 'i')"
GOOD_EXPRESSION = "new RegExp(keyword, 'i')"
SYNTHETIC_SOURCE = r"""import preferences from '@ohos.data.preferences';
import { TaskItem } from '../types/Types';
export class DataManager {
  private static DATA_MAP: Map<string, TaskItem[]> = new Map();
  private static readonly KEY: string = 'todos';
  static async saveTodos(todos: TaskItem[]) { DataManager.DATA_MAP.set(DataManager.KEY, todos); }
  static loadTodos(): TaskItem[] { return DataManager.DATA_MAP.get(DataManager.KEY) as TaskItem[] || []; }
  static loadTodosByState(isFinished: boolean): TaskItem[] {
    return DataManager.loadTodos().filter(t => t.isFinished === isFinished);
  }
  static queryTasks(name: string, isFinished: boolean): TaskItem[] {
    let taskItems: TaskItem[] = DataManager.loadTodosByState(isFinished);
    let keyword: string = name.replace(/[.*+^${}()|[\]\\]/g, '\\$&');
    let searchExp: RegExp = new RegExp('(' + keyword, 'i');
    return taskItems.filter(t => {
      if (t.title) { return searchExp.test(t.title); }
      return false;
    });
  }
  static queryById(id: string): TaskItem | undefined {
    return DataManager.loadTodos().filter(t => t.taskID === id)[0];
  }
  static querySubTasks(id: string): TaskItem[] {
    return DataManager.loadTodos().filter(t => t.parentID === id);
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
    result = subprocess.run([str(executable), "-e",
        "process.exit(typeof require('node:module').stripTypeScriptTypes === 'function' ? 0 : 3)"],
        capture_output=True, timeout=10, check=False)
    if result.returncode == 3:
        pytest.skip("Node lacks official type stripping; production explicitly refuses this runtime")
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
    return executable


def _setup(tmp_path: Path, node: Path, mutation: str) -> tuple[CandidateWorkspace, Path, LocalCheckConfig]:
    project = tmp_path / "fixture/project"
    target = local_evidence_path(project / SOURCE)
    target.parent.mkdir(parents=True)
    target.write_text(SYNTHETIC_SOURCE, encoding="utf-8")
    log = project.parent / "fault.log"
    log.write_text("SyntaxError: synthetic invalid capture group", encoding="utf-8")
    task = tmp_path / "task"
    prepare_task(project.parent, {"task_id": "synthetic-office-replay", "app": "project",
        "log": {"path": "fault.log", "sha256": digest(log.read_bytes())}}, task)
    work = CandidateWorkspace(task, tmp_path / "work", "synthetic-office")
    work.start_draft()
    work.apply_text_edits([TextEdit(path=SOURCE, old=BAD_EXPRESSION, new=GOOD_EXPRESSION)])
    if mutation == "case_sensitive":
        work.apply_text_edits([TextEdit(path=SOURCE, old=GOOD_EXPRESSION, new="new RegExp(keyword)")])
    elif mutation == "stock_mutation":
        statement = "let taskItems: TaskItem[] = DataManager.loadTodosByState(isFinished);"
        work.apply_text_edits([TextEdit(path=SOURCE, old=statement,
            new=statement + "\n    DataManager.saveTodos(taskItems);")])
    elif mutation == "platform_use":
        work.apply_text_edits([TextEdit(path=SOURCE,
            old="let keyword: string = name.replace", new="preferences.getPreferencesSync({});\n    let keyword: string = name.replace")])
    elif mutation == "pending_save":
        work.apply_text_edits([TextEdit(path=SOURCE,
            old="static async saveTodos(todos: TaskItem[]) { DataManager.DATA_MAP.set(DataManager.KEY, todos); }",
            new="static async saveTodos(todos: TaskItem[]) { return new Promise(() => {}); }")])
    candidate = work.register_candidate(1)
    source = work.require_registered(candidate.candidate_id)
    oracle = tmp_path / "office-oracle.cjs"
    shutil.copyfile(ADAPTER, oracle)
    sha = digest(oracle.read_bytes())
    config = LocalCheckConfig(mode="source", project_root=str(project), validation_scope=SCOPE,
        mocked_interfaces=[], commands={stage: LocalCommand(argv=[str(node), str(oracle), stage],
            timeout_seconds=10, oracle_sha256=sha) for stage in STAGES}, independent_oracles={stage:
            IndependentOracle(path=str(oracle), sha256=sha) for stage in STAGES},
        baseline_target_exit_codes=[9], baseline_fault_pattern="DFX017_TARGET_REGEX_SYNTAX_ERROR",
        max_check_seconds=90)
    return work, source, config


def _output(root: Path, stage: dict[str, Any], name: str) -> str:
    ref = stage.get(name)
    return "" if ref is None else local_evidence_path(root / ref["path"]).read_text(encoding="utf-8")


@pytest.mark.parametrize("mutation,status,stages", [
    ("good", "pass", ["build", "target", "behavior", "regression"]),
    ("case_sensitive", "fail", ["build", "target", "behavior"]),
    ("stock_mutation", "fail", ["build", "target"]),
    ("platform_use", "fail", ["build"]),
    ("pending_save", "fail", ["build", "target"]),
])
def test_real_synthetic_map_and_queries_reject_loss_of_behavior_and_platform_use(
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
    baseline = evidence["baseline"]["stages"]
    assert evidence["baseline"]["fault_reproduced"] is True
    assert baseline[0]["exit_code"] == 0 and baseline[1]["exit_code"] == 9
    assert baseline[1]["reason"] == "exited" and baseline[1]["subtree_stopped"] is True
    baseline_error = _output(receipt_root, baseline[1], "stderr")
    assert "DFX017_TARGET_REGEX_SYNTAX_ERROR" in baseline_error and "SyntaxError" in baseline_error
    assert [row["stage"] for row in evidence["stages"]] == stages
    assert evidence["source_verification"] == status
    assert evidence["application_verification"] == "not_run" and evidence["application_success"] is False
    for rows, checked_source in ((baseline, Path(config.project_root)), (evidence["stages"], source)):
        sha = digest(local_evidence_path(checked_source / SOURCE).read_bytes())
        for row in rows:
            if row["stage"] != "build" or checked_source != source or mutation != "platform_use":
                header = json.loads(_output(receipt_root, row, "stdout").splitlines()[0])
                assert header["source_sha256"] == sha and header["stage"] == row["stage"]
            assert row["source_binding_verified"] and row["oracle_binding_verified"]
    if mutation == "case_sensitive":
        assert "queryTasks returned wrong IDs" in _output(receipt_root, evidence["stages"][-1], "stderr")
    elif mutation == "stock_mutation":
        assert "changed stored task inventory" in _output(receipt_root, evidence["stages"][-1], "stderr")
    elif mutation == "platform_use":
        assert "UnsupportedPlatformDependency" in _output(receipt_root, evidence["stages"][-1], "stderr")
    elif mutation == "pending_save":
        target = evidence["stages"][-1]
        assert target["exit_code"] == 2
        assert "OracleAsyncCompletionTimeout" in _output(receipt_root, target, "stderr")
        assert "DFX017_TARGET_REGEX_SYNTAX_ERROR" not in _output(receipt_root, target, "stderr")
    assert ledger.run(source.parent.name) == result and ledger.completed(source.parent.name) == result
    assert local_evidence_path(Path(config.project_root) / SOURCE).read_text(encoding="utf-8") == SYNTHETIC_SOURCE
