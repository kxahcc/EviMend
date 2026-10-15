"""Source replay executes the changed program without a platform or device."""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

import pytest
import test_applicability as applicability_fixtures
from pydantic import ValidationError

from dfx_agent.applicability import ApplicabilityError
from dfx_agent.contracts import execution_evidence_kind
from dfx_agent.local_execution import (
    IndependentOracle,
    LocalApplicationExecutor,
    LocalCheckConfig,
    LocalCommand,
)
from dfx_agent.task import PROFILE, digest, snapshot_digest
from dfx_agent.verification import AnalysisOnlyExecutor, CheckLedger, CheckResult
from dfx_agent.workspace import CandidateWorkspace, TextEdit

REPOSITORY = Path(__file__).resolve().parents[1]
ORACLE = '''from pathlib import Path
import runpy, sys
stage = sys.argv[1]
if stage == "build":
    compile(Path("calc.py").read_text(), "calc.py", "exec")
else:
    transform = runpy.run_path("calc.py")["transform"]
    if stage == "target":
        if transform(2) != 3:
            print("ReplayTargetFault", flush=True)
            sys.exit(9)
    elif stage == "behavior":
        assert transform(0) == 1, "normal input behavior changed"
    elif stage == "regression":
        assert all(transform(n) == n + 1 for n in (-100, -1, 1, 100)), "boundary behavior changed"
'''


def workspace(tmp_path: Path, *, patch: str = "return n + 1") -> tuple[CandidateWorkspace, Path, Path]:
    project = tmp_path / "source-project"
    project.mkdir()
    original = b"def transform(n):\n    return n - 1\n"
    (project / "calc.py").write_bytes(original)
    task = tmp_path / "task"
    (task / "source").mkdir(parents=True)
    (task / "source/calc.py").write_bytes(original)
    manifest = [{"path": "calc.py", "sha256": digest(original)}]
    (task / "source-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (task / "crash.log").write_bytes(b"ReplayTargetFault")
    (task / "task.json").write_text(json.dumps({
        "schema_version": 1, "profile": PROFILE, "task_id": "source-replay-task", "source_root": "source",
        "source_manifest": "source-manifest.json", "source_snapshot_sha256": snapshot_digest(manifest),
        "crash_log": "crash.log", "crash_sha256": digest(b"ReplayTargetFault")}), encoding="utf-8")
    result = CandidateWorkspace(task, tmp_path / "work", "source-replay-run")
    result.start_draft()
    result.apply_text_edits([TextEdit(path="calc.py", old="return n - 1", new=patch)])
    candidate = result.register_candidate(1)
    return result, result.require_registered(candidate.candidate_id), project


def source_config(tmp_path: Path, project: Path) -> LocalCheckConfig:
    oracle = tmp_path / "independent-oracle.py"
    oracle.write_text(ORACLE, encoding="utf-8")
    sha = digest(oracle.read_bytes())
    return LocalCheckConfig(mode="source", project_root=str(project),
        validation_scope="Execute the actual calc.py function against reproduced, normal and boundary inputs",
        mocked_interfaces=[], commands={stage: LocalCommand(
            argv=[sys.executable, str(oracle), stage], oracle_sha256=sha)
            for stage in ("build", "target", "behavior", "regression")},
        independent_oracles={stage: IndependentOracle(path=str(oracle), sha256=sha)
                             for stage in ("build", "target", "behavior", "regression")},
        baseline_target_exit_codes=[9], baseline_fault_pattern="ReplayTargetFault", max_check_seconds=30)


@pytest.fixture
def managed(monkeypatch: pytest.MonkeyPatch) -> Path:
    root = REPOSITORY / ".dfx-local"
    if os.name != "nt" or not (root / "state/active.json").exists():
        pytest.skip("source process checks require the managed Windows test Job")
    assert json.loads((root / "state/active.json").read_text(encoding="utf-8-sig"))["task"] == "test"
    monkeypatch.setenv("DFX_MANAGED_COMPETITION", "local-v1")
    return root


@pytest.mark.parametrize("patch,status,stages", [
    ("return n + 1", "pass", ["build", "target", "behavior", "regression"]),
    ("return 3", "fail", ["build", "target", "behavior"]),
    ("return n - 2", "fail", ["build", "target"]),
])
def test_source_replay_checks_actual_candidate(tmp_path: Path, managed: Path,
        patch: str, status: str, stages: list[str]) -> None:
    work, source, project = workspace(tmp_path, patch=patch)
    executor = LocalApplicationExecutor(source_config(tmp_path, project), work, managed_root=managed)
    ledger = CheckLedger(work, executor)
    result = ledger.run(source.parent.name)
    assert result.status == status and result.level == "source_execution" and result.execution_scope == "source"
    evidence = executor.evidence(result.idempotency_key)
    assert evidence["schema_version"] == 4
    assert evidence["baseline"]["fault_reproduced"] is True
    assert [item["stage"] for item in evidence["stages"]] == stages
    assert evidence["source_verification"] == status
    assert evidence["application_verification"] == "not_run"
    assert evidence["application_success"] is False and evidence["accepted"] is False
    assert evidence["validation_scope"] == executor.config.validation_scope and evidence["mocked_interfaces"] == []
    assert ledger.completed(source.parent.name) == result
    assert ledger.run(source.parent.name) == result
    assert (project / "calc.py").read_bytes() == b"def transform(n):\n    return n - 1\n"


@pytest.mark.parametrize("field,value", [
    ("independent_oracles", None), ("baseline_fault_pattern", None),
    ("validation_scope", None), ("validation_scope", "  "), ("mocked_interfaces", None),
])
def test_source_protocol_requires_explicit_scope_and_independent_checks(tmp_path: Path,
        field: str, value: object) -> None:
    config = source_config(tmp_path, tmp_path)
    data = config.model_dump(mode="json")
    data[field] = value
    with pytest.raises(ValidationError):
        LocalCheckConfig.model_validate_json(json.dumps(data))


def test_analysis_only_never_creates_execution_success(tmp_path: Path) -> None:
    work, source, _ = workspace(tmp_path)
    executor = AnalysisOnlyExecutor()
    ledger = CheckLedger(work, executor)
    result = ledger.run(source.parent.name)
    assert result.status == "inconclusive" and result.level == "not_run" and result.execution_scope is None
    assert "unverified" in result.detail and ledger.completed(source.parent.name) == result
    assert not (work.root / "local-execution").exists()
    with pytest.raises(ValueError, match="unexecuted"):
        execution_evidence_kind(result.execution_scope)
    with pytest.raises(ValidationError, match="unexecuted"):
        CheckResult.model_validate({**result.model_dump(), "status": "pass"})


def test_source_receipt_cannot_upgrade_to_application_or_change_declared_scope(tmp_path: Path, managed: Path) -> None:
    work, source, project = workspace(tmp_path)
    executor = LocalApplicationExecutor(source_config(tmp_path, project), work, managed_root=managed)
    result = CheckLedger(work, executor).run(source.parent.name)
    assert executor.root is not None
    path = executor.root / result.idempotency_key / "execution.json"
    original = json.loads(path.read_text(encoding="utf-8"))
    for change in ({"application_success": True}, {"application_verification": "pass"},
                   {"source_verification": "not_run"}, {"mocked_interfaces": ["unreported mock"]}):
        path.write_text(json.dumps({**original, **change}), encoding="utf-8")
        with pytest.raises(ValueError, match="overstates|scope declaration"):
            executor.evidence(result.idempotency_key)
    path.write_text(json.dumps(original), encoding="utf-8")
    assert executor.evidence(result.idempotency_key)["source_verification"] == "pass"


def test_source_execution_kind_has_no_synthetic_fallback() -> None:
    assert execution_evidence_kind("source") == "source_execution"
    assert execution_evidence_kind("application") == "application_execution"
    assert execution_evidence_kind("synthetic") == "local_execution"


@pytest.mark.parametrize("inference", [False, True], ids=["execution-fact", "inference-chain"])
def test_source_failure_reaches_context_and_three_state_guidance(tmp_path: Path, managed: Path,
                                                               inference: bool) -> None:
    # This tiny Python-compatible .ets fixture exercises provenance plumbing,
    # not a production ArkTS translation or HarmonyOS API emulation.
    s = applicability_fixtures.scenario(tmp_path, prepared=False)
    project = tmp_path / "replay-project"
    shutil.copytree(s.workspace.base, project)
    oracle = tmp_path / "fixture-replay.py"
    oracle.write_text('''from pathlib import Path
import sys
text = Path("src/main.ets").read_text()
if sys.argv[1] == "build":
    compile(text, "src/main.ets", "exec")
else:
    namespace = {"bad": "bad", "almost": "almost", "good": "good"}
    exec(compile(text, "src/main.ets", "exec"), namespace)
    if namespace["value"] != "good":
        print("ReplayTargetFault", flush=True)
        sys.exit(9)
''', encoding="utf-8")
    sha = digest(oracle.read_bytes())
    config = LocalCheckConfig(mode="source", project_root=str(project),
        validation_scope="Execute the Python-compatible synthetic .ets assignment fixture; no ArkTS claim",
        mocked_interfaces=["bad/almost/good fixture symbols"], commands={stage: LocalCommand(
            argv=[sys.executable, str(oracle), stage], oracle_sha256=sha)
            for stage in ("build", "target", "behavior", "regression")},
        independent_oracles={stage: IndependentOracle(path=str(oracle), sha256=sha)
                             for stage in ("build", "target", "behavior", "regression")},
        baseline_target_exit_codes=[9], baseline_fault_pattern="ReplayTargetFault", max_check_seconds=30)
    s.ledger.executor = LocalApplicationExecutor(config, s.workspace, managed_root=managed)
    s.tools["edit_source"].invoke({"path": "src/main.ets", "old": "bad", "new": "almost"})
    s.current = json.loads(s.tools["register_candidate"].invoke(
        {"diagnosis_id": "diagnosis1", "preserves": ["keep_value"]}))["candidate_id"]
    assert s.current is not None
    check = json.loads(s.tools["run_check"].invoke({"candidate_id": s.current}))
    assert check["status"] == "fail" and check["level"] == "source_execution"
    linked = json.loads(s.tools["link_failed_check"].invoke(
        {"candidate_id": s.current, "violated_obligation_ids": ["keep_value"]}))
    feedback = json.loads((s.workspace.root / "feedback" / (linked["feedback_id"] + ".json")).read_text())
    assert feedback["scope"] == "source" and feedback["evidence_kind"] == "source_execution"
    applicability_fixtures.execution_diagnosis(s, s.current, inference=inference)
    s.prepare()
    assert s.context is not None
    packet = applicability_fixtures.packet(s)
    assert packet["evidence"]["execution1"]["execution_scope"] == "source"
    assert packet["evidence"]["execution1"]["kind"] == "source_execution"
    assert packet["application_verification"] == "not_run"
    assert s.store.read_context(s.context.context_id) == s.context
    evidence_id = "infer_execution" if inference else "execution1"
    review = s.store.review(s.proposal(evidence_id=evidence_id, scope="current_candidate",
                                      evidence_candidate=s.current))
    assert review.contents()["semantic_authority"] == "model_inference"
    with pytest.raises(ApplicabilityError):
        s.store.review(s.proposal(evidence_id=evidence_id))
