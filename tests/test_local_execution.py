"""Short fake programs use the actual enclosing managed Windows test Job."""

from __future__ import annotations

import ctypes
import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest

import dfx_agent.local_execution as local
from dfx_agent.local_execution import (
    LocalApplicationExecutor,
    LocalCheckConfig,
    LocalCommand,
)
from dfx_agent.task import PROFILE, digest, snapshot_digest
from dfx_agent.workspace import CandidateWorkspace, TextEdit

REPOSITORY = Path(__file__).resolve().parents[1]
PROGRAM = '''from pathlib import Path
import json, os, subprocess, sys, time
settings = json.loads(Path("checks.json").read_text())
stage = sys.argv[1]
assert "CLOUD_TEST_SECRET" not in os.environ
assert Path("assets/image.bin").read_bytes() == bytes(range(256))
assert Path("empty-assets").is_dir()
if settings.get("noise_stage") == stage:
    os.write(1, b"x" * 65536)
if settings.get("sleep_stage") == stage:
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    Path("grandchild.pid").write_text(str(child.pid))
    time.sleep(30)
value_before = Path(settings["source"]).read_text()
role = "baseline" if "bad" in value_before else "candidate"
if stage == "prepare":
    cache = Path(os.environ["npm_config_cache"])
    assert cache.is_relative_to(Path.cwd()) and cache.is_dir()
    assert cache.parent == Path(os.environ["DFX_PROJECT_CACHE_ROOT"])
    Path(".prepared").write_text("project dependencies prepared")
    if settings.get("fail_prepare_role") == role:
        print("Synthetic dependency unavailable", flush=True)
        sys.exit(7)
elif settings.get("require_prepared"):
    assert Path(".prepared").is_file(), "project dependencies were not prepared"
if settings.get("mutate_stage") == stage and settings.get("mutate_role", role) == role:
    Path(settings["source"]).write_text(settings.get("mutate_value", "value=bad\\n"))
if settings.get("mutate_oracle_stage") == stage and settings.get("mutate_oracle_role", role) == role:
    Path(settings["oracle_path"]).write_text("changed acceptance definition")
if stage == "target" and "bad" in Path(settings["source"]).read_text():
    print("SyntheticTargetFault", flush=True)
    sys.exit(9)
if settings.get("fail_stage") == stage:
    print("Synthetic business failure", file=sys.stderr, flush=True)
    sys.exit(2)
print(stage + " passed", flush=True)
'''


@pytest.fixture
def managed(monkeypatch: pytest.MonkeyPatch) -> Path:
    root = REPOSITORY / ".dfx-local"
    if os.name != "nt" or not (root / "state/active.json").exists():
        pytest.skip("local process checks require the managed Windows test Job")
    lease = json.loads((root / "state/active.json").read_text(encoding="utf-8-sig"))
    assert lease["task"] == "test"
    monkeypatch.setenv("DFX_MANAGED_COMPETITION", "local-v1")
    monkeypatch.setenv("CLOUD_TEST_SECRET", "never-copy-this-credential")
    return root


def fixture(tmp_path: Path, managed_root: Path, *, mapping: bool = False,
            settings: dict[str, Any] | None = None, timeout: int = 3,
            output: int = 131072, fault: str = "SyntheticTargetFault", prepare: bool = False,
            oracle_binding: bool = False) -> tuple[
                CandidateWorkspace, Path, LocalCheckConfig, LocalApplicationExecutor]:
    project = tmp_path / "original-project"
    full_source = "module/main.ets" if mapping else "src/main.ets"
    (project / Path(full_source).parent).mkdir(parents=True)
    (project / full_source).write_bytes(b"value=bad\n")
    (project / "assets").mkdir()
    (project / "assets/image.bin").write_bytes(bytes(range(256)))
    (project / "empty-assets").mkdir()
    (project / "verify.py").write_text(PROGRAM, encoding="utf-8")
    oracle = tmp_path / "host-acceptance.txt"
    if oracle_binding:
        oracle.write_bytes(b"host-reviewed acceptance definition")
    (project / "checks.json").write_text(json.dumps({"source": full_source, "oracle_path": str(oracle),
                                                    **(settings or {})}), encoding="utf-8")
    task = tmp_path / "task"
    (task / "source/src").mkdir(parents=True)
    (task / "source/src/main.ets").write_bytes(b"value=bad\n")
    manifest = [{"path": "src/main.ets", "sha256": digest(b"value=bad\n")}]
    (task / "source-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (task / "crash.log").write_bytes(b"synthetic crash")
    (task / "task.json").write_text(json.dumps({
        "schema_version": 1, "profile": PROFILE, "task_id": "local-task", "source_root": "source",
        "source_manifest": "source-manifest.json", "source_snapshot_sha256": snapshot_digest(manifest),
        "crash_log": "crash.log", "crash_sha256": digest(b"synthetic crash")}), encoding="utf-8")
    workspace = CandidateWorkspace(task, tmp_path / "work", "local-run")
    workspace.start_draft()
    workspace.apply_text_edits([TextEdit(path="src/main.ets", old="bad", new="good")])
    patch = workspace.register_candidate(1)
    source = workspace.require_registered(patch.candidate_id)
    oracle_sha256 = digest(oracle.read_bytes()) if oracle_binding else None
    config = LocalCheckConfig(project_root=str(project), commands={
        stage: LocalCommand(argv=[sys.executable, "verify.py", stage], timeout_seconds=timeout,
                            oracle_sha256=oracle_sha256)
        for stage in local.STAGES}, baseline_target_exit_codes=[9], baseline_fault_pattern=fault,
        independent_oracles={stage: local.IndependentOracle(path=str(oracle), sha256=oracle_sha256)
                             for stage in local.STAGES} if oracle_sha256 is not None else None,
        preparation=LocalCommand(argv=[sys.executable, "verify.py", "prepare"], timeout_seconds=timeout) if prepare else None,
        source_mapping={"src/main.ets": full_source} if mapping else {},
        max_output_bytes=output, max_check_seconds=30, credential_env_names=["CLOUD_TEST_SECRET"])
    executor = LocalApplicationExecutor(config, workspace, managed_root=managed_root)
    return workspace, source, config, executor


def test_optional_preparation_keeps_existing_protocol_serialization(tmp_path: Path) -> None:
    # No process or application condition is needed to check old JSON identities.
    config = LocalCheckConfig(project_root=str(tmp_path), commands={
        stage: LocalCommand(argv=[sys.executable, "check.py", stage]) for stage in local.STAGES},
        baseline_target_exit_codes=[9])
    assert "preparation" not in config.model_dump(mode="json")
    assert "preparation" not in json.loads(config.model_dump_json())
    assert "independent_oracles" not in config.model_dump(mode="json")
    assert "independent_oracles" not in json.loads(config.model_dump_json())


def test_frozen_oracle_drift_prevents_any_process_launch(tmp_path: Path, managed: Path,
                                                      monkeypatch: pytest.MonkeyPatch) -> None:
    _, source, config, executor = fixture(tmp_path, managed, oracle_binding=True)
    assert config.independent_oracles is not None
    Path(config.independent_oracles["target"].path).write_bytes(b"changed after configure")
    monkeypatch.setattr(local.subprocess, "Popen", lambda *_a, **_kw: pytest.fail("changed oracle launched"))
    with pytest.raises(ValueError, match="oracle bytes differ"):
        executor.check(key=key("oracle-drift-before-check"), source=source)
    assert executor.root is not None
    assert not (executor.root / key("oracle-drift-before-check")).exists()


@pytest.mark.parametrize("role,stage", [("baseline", "build"), ("baseline", "target"),
                                        ("candidate", "behavior"), ("candidate", "regression")])
def test_oracle_mutation_during_execution_is_inconclusive(tmp_path: Path, managed: Path,
        role: str, stage: str) -> None:
    _, source, _, executor = fixture(tmp_path, managed, oracle_binding=True,
        settings={"mutate_oracle_role": role, "mutate_oracle_stage": stage})
    receipt_key = key("oracle-mutated-" + role + "-" + stage)
    status, _ = executor.check(key=receipt_key, source=source)
    assert status == "inconclusive"
    evidence = executor.evidence(receipt_key)
    stages = evidence["baseline"]["stages"] if role == "baseline" else evidence["stages"]
    assert evidence["schema_version"] == 3
    assert stages[-1]["stage"] == stage and stages[-1]["process_started"] is True
    assert stages[-1]["reason"] == "oracle_binding_changed"
    assert stages[-1]["oracle_binding_verified"] is False
    assert evidence["accepted"] is False


def test_oracle_drift_between_stages_prevents_the_next_launch(tmp_path: Path, managed: Path,
                                                           monkeypatch: pytest.MonkeyPatch) -> None:
    _, source, config, executor = fixture(tmp_path, managed, oracle_binding=True)
    original = executor._stage
    seen: list[tuple[str, str]] = []

    def drift(stage: str, role: str, *args: Any, **kwargs: Any) -> dict[str, Any]:
        result = original(stage, role, *args, **kwargs)
        seen.append((role, stage))
        if (role, stage) == ("baseline", "build"):
            assert config.independent_oracles is not None
            Path(config.independent_oracles["target"].path).write_bytes(b"changed between stages")
        return result

    monkeypatch.setattr(executor, "_stage", drift)
    receipt_key = key("oracle-between-stages")
    assert executor.check(key=receipt_key, source=source)[0] == "inconclusive"
    evidence = executor.evidence(receipt_key)
    assert seen == [("baseline", "build"), ("baseline", "target")]
    target = evidence["baseline"]["stages"][-1]
    assert target["process_started"] is False and target["oracle_binding_verified"] is False
    assert evidence["stages"] == []


def test_completed_bound_checks_replay_without_reapproving_current_oracle_bytes(tmp_path: Path, managed: Path,
        monkeypatch: pytest.MonkeyPatch) -> None:
    _, source, config, executor = fixture(tmp_path, managed, oracle_binding=True)
    receipt_key = key("oracle-completed-replay")
    assert executor.check(key=receipt_key, source=source)[0] == "pass"
    assert config.independent_oracles is not None
    Path(config.independent_oracles["target"].path).unlink()
    monkeypatch.setattr(local.subprocess, "Popen", lambda *_a, **_kw: pytest.fail("completed check relaunched"))
    evidence = executor.evidence(receipt_key)
    assert evidence["status"] == "pass" and evidence["schema_version"] == 3
    assert all(row["oracle_binding_verified"] for row in evidence["stages"])
    assert executor.check(key=receipt_key, source=source)[0] == "pass"
    with pytest.raises(ValueError, match="path is missing"):
        executor.check(key=key("oracle-new-check-missing"), source=source)


def test_old_application_configuration_cannot_authorize_new_checks(tmp_path: Path) -> None:
    executor = LocalApplicationExecutor.__new__(LocalApplicationExecutor)
    executor.config = LocalCheckConfig(mode="application", project_root=str(tmp_path), commands={
        stage: LocalCommand(argv=[sys.executable, "check.py", stage], oracle_sha256=key(stage))
        for stage in local.STAGES}, baseline_target_exit_codes=[9], baseline_fault_pattern="OriginalFault")
    with pytest.raises(ValueError, match="requires bound independent oracle"):
        executor.verify_oracles()


def test_old_application_pass_is_only_read_as_unbound_and_does_not_change_originals(tmp_path: Path, managed: Path,
        monkeypatch: pytest.MonkeyPatch) -> None:
    from dfx_agent.verification import CheckLedger

    workspace, source, _, executor = fixture(tmp_path, managed)
    ledger = CheckLedger(workspace, executor)
    candidate_id = source.parent.name
    assert ledger.run(candidate_id).status == "pass"
    receipt_key, ledger_path = ledger._identity(candidate_id, "offline")
    assert executor.root is not None
    root = executor.root
    path = local.local_evidence_path(root / receipt_key / "execution.json")
    # Literal legacy application receipt projected from authenticated synthetic
    # stage facts: no process is ever launched in application mode.
    record = json.loads(path.read_bytes())
    record.update(mode="application", verification_level="application_execution",
                  application_verification="pass", application_success=True, accepted=True)
    path.write_text(json.dumps(record), encoding="utf-8")
    saved_ledger = json.loads(ledger_path.read_bytes())
    saved_ledger["result"]["level"] = "application_execution"
    ledger_path.write_text(json.dumps(saved_ledger), encoding="utf-8")
    executor.config = executor.config.model_copy(update={"mode": "application"})
    monkeypatch.setattr(executor, "_bound", lambda: (workspace, root))
    monkeypatch.setattr(local.subprocess, "Popen", lambda *_a, **_kw: pytest.fail("legacy check relaunched"))
    before = {path: path.read_bytes(), ledger_path: ledger_path.read_bytes()}
    evidence = executor.evidence(receipt_key)
    assert evidence["status"] == "inconclusive" and evidence["application_verification"] == "inconclusive"
    assert evidence["accepted"] is False and evidence["application_success"] is False
    result = ledger.completed(candidate_id)
    assert result is not None and result.status == "inconclusive" and result.level == "application_execution"
    assert ledger.run(candidate_id) == result
    assert before == {path: path.read_bytes(), ledger_path: ledger_path.read_bytes()}


def test_preparation_precedes_checks_in_each_independent_copy(tmp_path: Path, managed: Path,
                                                           monkeypatch: pytest.MonkeyPatch) -> None:
    workspace, source, config, executor = fixture(tmp_path, managed, prepare=True, settings={"require_prepared": True})
    receipt_key = key("prepared-project")
    status, detail = executor.check(key=receipt_key, source=source)
    assert status == "pass", detail
    evidence = executor.evidence(receipt_key)
    assert [(row["role"], row["stage"]) for row in evidence["preparation"]] == [
        ("baseline", "prepare"), ("candidate", "prepare")]
    assert all(row["source_binding_verified"] and row["subtree_stopped"] for row in evidence["preparation"])
    assert evidence["accepted"] is False and evidence["application_verification"] == "not_run"
    assert not (Path(config.project_root) / ".prepared").exists()
    assert not (Path(config.project_root) / ".dfx-tool-cache").exists()
    monkeypatch.setattr(local.subprocess, "Popen", lambda *_a, **_kw: pytest.fail("completed preparation relaunched"))
    restored = LocalApplicationExecutor(config, workspace, managed_root=managed)
    assert restored.check(key=receipt_key, source=source) == (status, detail)


@pytest.mark.parametrize("role", ["baseline", "candidate"])
def test_preparation_failure_is_unavailable_environment_not_bad_patch(tmp_path: Path, managed: Path,
                                                                    role: str) -> None:
    _, source, _, executor = fixture(tmp_path, managed, prepare=True, settings={"fail_prepare_role": role})
    receipt_key = key("preparation-failure-" + role)
    status, detail = executor.check(key=receipt_key, source=source)
    assert status == "inconclusive" and "dependency preparation unavailable" in detail
    evidence = executor.evidence(receipt_key)
    assert evidence["preparation"][-1]["exit_code"] == 7
    assert evidence["baseline"]["stages"] == [] and evidence["stages"] == []
    assert evidence["accepted"] is False


@pytest.mark.parametrize("role", ["baseline", "candidate"])
def test_preparation_cannot_modify_bound_source(tmp_path: Path, managed: Path, role: str) -> None:
    _, source, _, executor = fixture(tmp_path, managed, prepare=True,
        settings={"mutate_role": role, "mutate_stage": "prepare", "mutate_value": "changed-source\n"})
    receipt_key = key("preparation-source-" + role)
    status, detail = executor.check(key=receipt_key, source=source)
    assert status == "inconclusive" and "changed the bound execution source" in detail
    evidence = executor.evidence(receipt_key)
    assert evidence["preparation"][-1]["source_binding_verified"] is False
    assert evidence["stages"] == [] and evidence["accepted"] is False


def test_unknown_preparation_is_never_reinstalled_on_recovery(tmp_path: Path, managed: Path,
                                                           monkeypatch: pytest.MonkeyPatch) -> None:
    workspace, source, config, executor = fixture(tmp_path, managed, prepare=True)
    original = executor._stage

    def interrupt(*args: Any, **kwargs: Any) -> dict[str, Any]:
        original(*args, **kwargs)
        raise RuntimeError("preparation response lost")

    monkeypatch.setattr(executor, "_stage", interrupt)
    receipt_key = key("preparation-unknown")
    with pytest.raises(RuntimeError, match="response lost"):
        executor.check(key=receipt_key, source=source)
    monkeypatch.setattr(local.subprocess, "Popen", lambda *_a, **_kw: pytest.fail("unknown preparation relaunched"))
    restored = LocalApplicationExecutor(config, workspace, managed_root=managed)
    assert restored.check(key=receipt_key, source=source)[0] == "inconclusive"
    assert restored.evidence(receipt_key)["accepted"] is False


def test_preparation_receipt_cannot_be_removed_from_a_pass(tmp_path: Path, managed: Path) -> None:
    workspace, source, _, executor = fixture(tmp_path, managed, prepare=True)
    receipt_key = key("preparation-receipt-drift")
    assert executor.check(key=receipt_key, source=source)[0] == "pass"
    path = local.local_evidence_path(workspace.root / "local-execution" / receipt_key / "execution.json")
    value = json.loads(path.read_bytes())
    del value["preparation"]
    path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(ValueError, match="missing required evidence"):
        executor.evidence(receipt_key)


def key(name: str) -> str:
    return digest(name.encode())


def test_complete_copy_overlay_receipts_and_replay(tmp_path: Path, managed: Path,
                                                 monkeypatch: pytest.MonkeyPatch) -> None:
    workspace, source, config, executor = fixture(tmp_path, managed, mapping=True)
    original = local._inventory(Path(config.project_root), config.max_project_bytes)
    status, detail = executor.check(key=key("success"), source=source)
    assert status == "pass" and "synthetic" in detail, detail
    receipt = executor.evidence(key("success"))
    assert receipt["baseline"]["fault_reproduced"] is True
    assert [row["stage"] for row in receipt["stages"]] == list(local.STAGES)
    assert all(row["exit_code"] == 0 and row["subtree_stopped"] for row in receipt["stages"])
    assert receipt["application_verification"] == "not_run"
    assert receipt["application_success"] is False and receipt["accepted"] is False
    assert local._inventory(Path(config.project_root), config.max_project_bytes) == original
    assert (workspace.base / "src/main.ets").read_bytes() == b"value=bad\n"
    copied = workspace.root / "local-execution" / ("c-" + key("success")[:16])
    assert (copied / "module/main.ets").read_bytes() == (source / "src/main.ets").read_bytes()
    assert (copied / "assets/image.bin").read_bytes() == bytes(range(256))
    assert (copied / "empty-assets").is_dir()
    assert not os.path.samefile(copied / "assets/image.bin", Path(config.project_root) / "assets/image.bin")
    monkeypatch.setattr(local.subprocess, "Popen", lambda *_a, **_kw: pytest.fail("completed check relaunched"))
    restored = LocalApplicationExecutor(config, workspace, managed_root=managed)
    assert restored.check(key=key("success"), source=source) == (status, detail)


@pytest.mark.parametrize("stage", ["target", "behavior", "regression"])
def test_candidate_failure_feedback(stage: str, tmp_path: Path, managed: Path) -> None:
    _, source, _, executor = fixture(tmp_path, managed, settings={"fail_stage": stage})
    status, detail = executor.check(key=key(stage), source=source)
    assert status == "fail" and stage in detail, detail
    receipt = executor.evidence(key(stage))
    assert receipt["baseline"]["fault_reproduced"] is True
    assert receipt["stages"][-1]["stage"] == stage
    assert receipt["stages"][-1]["exit_code"] == 2
    assert receipt["accepted"] is False


def test_nonzero_exit_without_expected_fault_is_not_reproduction(tmp_path: Path, managed: Path) -> None:
    _, source, _, executor = fixture(tmp_path, managed, fault="unrelated fault")
    assert executor.check(key=key("wrong-fault"), source=source)[0] == "fail"
    receipt = executor.evidence(key("wrong-fault"))
    assert receipt["baseline"]["fault_reproduced"] is False and receipt["stages"] == []


def test_overlay_mismatch_rejected_before_copy(tmp_path: Path, managed: Path) -> None:
    workspace, _, config, _ = fixture(tmp_path, managed)
    project = Path(config.project_root)
    (project / "src/main.ets").write_bytes(b"value=different\n")
    other_work = tmp_path / "other-work"
    # Reuse the frozen analysis input; do not change its expected hash.
    other = CandidateWorkspace(tmp_path / "task", other_work, "other-run")
    with pytest.raises(ValueError, match="mapping differs"):
        LocalApplicationExecutor(config, other, managed_root=managed)
    assert not (other.root / "local-execution").exists()
    assert (workspace.base / "src/main.ets").read_bytes() == b"value=bad\n"


def test_incomplete_mapping_rejected(tmp_path: Path, managed: Path) -> None:
    workspace, _, config, _ = fixture(tmp_path, managed)
    values = config.model_dump()
    values["source_mapping"] = {"wrong.ets": "src/main.ets"}
    with pytest.raises(ValueError, match="complete analysis baseline"):
        LocalApplicationExecutor(LocalCheckConfig.model_validate(values), workspace, managed_root=managed)


def _alive(pid: int) -> bool:
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.restype = ctypes.c_void_p
    kernel.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        return False
    try:
        code = ctypes.c_ulong()
        assert kernel.GetExitCodeProcess(handle, ctypes.byref(code))
        return code.value == 259
    finally:
        kernel.CloseHandle(handle)


def test_timeout_stops_subtree_and_preserves_unknown(tmp_path: Path, managed: Path) -> None:
    workspace, source, _, executor = fixture(tmp_path, managed, settings={"sleep_stage": "target"}, timeout=1)
    assert executor.check(key=key("timeout"), source=source)[0] == "inconclusive"
    receipt = executor.evidence(key("timeout"))
    target = receipt["baseline"]["stages"][-1]
    assert target["reason"] == "timeout" and target["subtree_stopped"]
    pidfile = workspace.root / "local-execution" / ("b-" + key("timeout")[:16]) / "grandchild.pid"
    assert not _alive(int(pidfile.read_text(encoding="utf-8")))


def test_output_limit_has_bounded_evidence(tmp_path: Path, managed: Path) -> None:
    _, source, _, executor = fixture(tmp_path, managed, settings={"noise_stage": "build"}, output=256)
    assert executor.check(key=key("noisy"), source=source)[0] == "inconclusive"
    result = executor.evidence(key("noisy"))["baseline"]["stages"][0]
    assert result["reason"] == "output_limit"
    assert result["stdout"]["size_bytes"] == 256
    assert result["stderr"]["size_bytes"] <= 256


def test_unknown_recovery_never_relaunches(tmp_path: Path, managed: Path,
                                         monkeypatch: pytest.MonkeyPatch) -> None:
    workspace, source, config, executor = fixture(tmp_path, managed)
    original = executor._stage

    def interrupt(*args: Any, **kwargs: Any) -> dict[str, Any]:
        original(*args, **kwargs)
        raise RuntimeError("simulate loss after command completion, before saved result")

    monkeypatch.setattr(executor, "_stage", interrupt)
    with pytest.raises(RuntimeError, match="simulate loss"):
        executor.check(key=key("unknown"), source=source)
    monkeypatch.setattr(local.subprocess, "Popen", lambda *_a, **_kw: pytest.fail("unknown check relaunched"))
    restored = LocalApplicationExecutor(config, workspace, managed_root=managed)
    assert restored.check(key=key("unknown"), source=source)[0] == "inconclusive"
    assert restored.evidence(key("unknown"))["accepted"] is False


@pytest.mark.parametrize("application_view", [False, True], ids=["synthetic", "application-receipt-view"])
def test_empty_receipt_directory_stays_inconclusive_through_ledger_reads(tmp_path: Path, managed: Path,
        monkeypatch: pytest.MonkeyPatch, application_view: bool) -> None:
    from dfx_agent.verification import CheckLedger

    workspace, source, _, executor = fixture(tmp_path, managed)
    assert executor.root is not None
    root = executor.root
    if application_view:
        # Exercise the application read contract without launching an app or
        # changing the runtime's application experiment permission.
        executor.config = executor.config.model_copy(update={"mode": "application"})
        monkeypatch.setattr(executor, "_bound", lambda: (workspace, root))
    ledger = CheckLedger(workspace, executor)
    candidate_id = source.parent.name
    receipt_key, ledger_path = ledger._identity(candidate_id, "offline")
    folder = local.local_evidence_path(root / receipt_key)
    folder.mkdir()
    monkeypatch.setattr(local.subprocess, "Popen", lambda *_a, **_kw: pytest.fail("unknown initialization relaunched"))

    first = ledger.run(candidate_id)
    assert first.status == "inconclusive" and "initialization was interrupted" in first.detail
    ledger_before = ledger_path.read_bytes()
    assert ledger.completed(candidate_id) == first
    assert ledger.run(candidate_id) == first
    evidence = executor.evidence(receipt_key)
    assert evidence["state"] == "initialization_interrupted" and evidence["status"] == "inconclusive"
    assert evidence["application_verification"] == ("inconclusive" if application_view else "not_run")
    assert evidence["accepted"] is False and evidence["application_success"] is False
    assert evidence["baseline"] is None and evidence["stages"] == []
    assert list(folder.iterdir()) == [] and ledger_path.read_bytes() == ledger_before


@pytest.mark.parametrize("artifact", ["baseline-build.json", "candidate-target.stdout.txt", "unexpected.json"])
def test_missing_execution_receipt_with_other_artifacts_is_rejected(tmp_path: Path, managed: Path,
        monkeypatch: pytest.MonkeyPatch, artifact: str) -> None:
    _, source, _, executor = fixture(tmp_path, managed)
    assert executor.root is not None
    receipt_key = key("missing-receipt-" + artifact)
    folder = local.local_evidence_path(executor.root / receipt_key)
    folder.mkdir()
    leftover = folder / artifact
    leftover.write_bytes(b"unbound execution artifact")
    monkeypatch.setattr(local.subprocess, "Popen", lambda *_a, **_kw: pytest.fail("damaged check relaunched"))
    for read in (lambda: executor.check(key=receipt_key, source=source), lambda: executor.evidence(receipt_key)):
        with pytest.raises(ValueError, match="missing from a nonempty evidence directory"):
            read()
    assert leftover.read_bytes() == b"unbound execution artifact"
    assert not (folder / "execution.json").exists()


def test_task_deadline_prevents_launch(tmp_path: Path, managed: Path,
                                     monkeypatch: pytest.MonkeyPatch) -> None:
    _, source, _, executor = fixture(tmp_path, managed)
    executor.set_deadline(1.0)
    monkeypatch.setattr(local.subprocess, "Popen", lambda *_a, **_kw: pytest.fail("expired check launched"))
    assert executor.check(key=key("deadline"), source=source)[0] == "inconclusive"
    assert executor.evidence(key("deadline"))["baseline"]["stages"][0]["reason"] == "check_timeout"


def test_parent_entry_and_job_are_required(tmp_path: Path, managed: Path,
                                         monkeypatch: pytest.MonkeyPatch) -> None:
    _, source, config, executor = fixture(tmp_path, managed)
    monkeypatch.delenv("DFX_MANAGED_COMPETITION")
    with pytest.raises(ValueError, match="managed competition"):
        executor.check(key=key("unguarded"), source=source)
    monkeypatch.setenv("DFX_MANAGED_COMPETITION", "local-v1")
    monkeypatch.setattr(local, "_job_limits", lambda _name: (_ for _ in ()).throw(ValueError("no real Job")))
    with pytest.raises(ValueError, match="no real Job"):
        executor.check(key=key("no-job"), source=source)
    assert executor.root is not None
    assert not (executor.root / key("no-job")).exists()
    assert config.mode == "synthetic"


def test_wrong_named_job_is_rejected(managed: Path) -> None:
    lease = json.loads((managed / "state/active.json").read_text(encoding="utf-8-sig"))
    actual = local._job_limits(lease["job_name"])
    assert actual == {name: lease["effective_limits"][name] for name in ("cpu_percent", "memory_bytes", "max_processes")}
    with pytest.raises(ValueError, match="cannot be opened"):
        local._job_limits("Local\\DFX-Guard-" + key("nonexistent-managed-job"))


def test_output_drift_rejected_on_read(tmp_path: Path, managed: Path) -> None:
    workspace, source, _, executor = fixture(tmp_path, managed)
    assert executor.check(key=key("drift"), source=source)[0] == "pass"
    output = local.local_evidence_path(workspace.root / "local-execution" / key("drift") / "candidate-target.stdout.txt")
    output.write_bytes(b"changed")
    with pytest.raises(ValueError, match="output evidence changed"):
        executor.evidence(key("drift"))


def test_synthetic_external_shell_and_application_modes_rejected(tmp_path: Path, managed: Path) -> None:
    workspace, source, config, _ = fixture(tmp_path, managed)
    values = config.model_dump()
    values["commands"]["build"]["argv"][0] = str(REPOSITORY / "scripts/local.ps1")
    altered = LocalCheckConfig.model_validate(values)
    # A different protocol cannot overwrite an already bound protocol.
    with pytest.raises(ValueError, match="protocol or project changed"):
        LocalApplicationExecutor(altered, workspace, managed_root=managed)
    values = config.model_dump()
    values["mode"] = "application"
    for stage in local.STAGES:
        values["commands"][stage]["oracle_sha256"] = key(stage)
    application = LocalCheckConfig.model_validate(values)
    with pytest.raises(ValueError, match="application experiments are disabled"):
        LocalApplicationExecutor(application, workspace, managed_root=managed, allow_application=True)
    assert source.is_dir()


def test_explicit_stop_prevents_launch(tmp_path: Path, managed: Path,
                                     monkeypatch: pytest.MonkeyPatch) -> None:
    _, source, _, executor = fixture(tmp_path, managed)
    lease = json.loads((managed / "state/active.json").read_text(encoding="utf-8-sig"))
    executor.stop_path = tmp_path / "local-stop.json"
    executor.stop_path.write_text(json.dumps({"run_id": lease["run_id"]}), encoding="utf-8")
    monkeypatch.setattr(local.subprocess, "Popen", lambda *_a, **_kw: pytest.fail("stopped check launched"))
    assert executor.check(key=key("stopped"), source=source)[0] == "inconclusive"
    assert executor.evidence(key("stopped"))["baseline"]["stages"][0]["reason"] == "stop_requested"


def test_links_and_mapping_aliases_rejected(tmp_path: Path, managed: Path) -> None:
    _, _, config, _ = fixture(tmp_path, managed)
    values = config.model_dump()
    values["source_mapping"] = {"src/main.ets": "../outside.ets"}
    with pytest.raises(ValueError, match="relative POSIX"):
        LocalCheckConfig.model_validate(values)
    project = Path(config.project_root)
    external = tmp_path / "external.bin"
    external.write_bytes(b"outside resource")
    link = project / "linked.bin"
    try:
        os.symlink(external, link)
    except OSError:
        pytest.skip("symlink creation unavailable on this Windows account")
    try:
        with pytest.raises(ValueError, match="link"):
            LocalApplicationExecutor(config, managed_root=managed)
        assert external.read_bytes() == b"outside resource"
    finally:
        link.unlink()


@pytest.mark.parametrize("mode,state,status,stages,expected", [
    ("synthetic", "done", "pass", [{"reason": "exited", "process_started": True}], "not_run"),
    ("application", "done", "pass", [{"reason": "exited", "process_started": True}], "pass"),
    ("application", "done", "fail", [{"reason": "exited", "process_started": True}], "fail"),
    ("application", "done", "inconclusive", [{"reason": "timeout", "process_started": True}], "inconclusive"),
    ("application", "done", "inconclusive", [{"reason": "stop_requested", "process_started": True}], "inconclusive"),
    ("application", "done", "inconclusive", [{"reason": "stop_requested", "process_started": False}], "not_run"),
    ("application", "done", "inconclusive", [{"reason": "check_timeout", "process_started": False}], "not_run"),
    ("application", "done", "inconclusive", [], "not_run"),
    ("application", "pending", "inconclusive", [], "inconclusive"),
    ("application", "done", "fail", [{"reason": "exited"}], "fail"),
    ("application", "done", "inconclusive", [{"reason": "launch_or_stop_unknown"}], "inconclusive"),
    ("application", "done", "inconclusive", [{"reason": "stop_requested", "exit_code": -1}], "inconclusive"),
    ("application", "done", "inconclusive", [{"reason": "stop_requested", "stdout": {"size_bytes": 0}}], "inconclusive"),
    ("application", "done", "inconclusive", [{"reason": "stop_requested"}], "inconclusive"),
    ("application", "done", "inconclusive", [{"reason": "before_launch"}], "not_run"),
    ("application", "done", "inconclusive", [{"reason": "check_timeout"}], "not_run"),
])
def test_application_receipt_state_is_execution_not_solver_acceptance(mode: str, state: str, status: str,
        stages: list[dict[str, Any]], expected: str) -> None:
    # Literal host receipt fixtures, never an application-mode experiment.
    record = {"schema_version": 1, "mode": mode, "state": state, "status": status,
              "stages": stages, "accepted": False, "application_verification": "not_run"}
    assert local.application_check_state(record) == expected


def test_legacy_execution_receipt_is_read_without_rewriting(tmp_path: Path, managed: Path,
                                                           monkeypatch: pytest.MonkeyPatch) -> None:
    workspace, source, _, executor = fixture(tmp_path, managed)
    receipt_key = key("legacy-read")
    assert executor.check(key=receipt_key, source=source)[0] == "pass"
    folder = local.local_evidence_path(workspace.root / "local-execution" / receipt_key)
    receipt_path = folder / "execution.json"
    original = json.loads(receipt_path.read_bytes())
    original["schema_version"] = 1
    original.pop("verification_level")
    receipt_path.write_text(json.dumps(original), encoding="utf-8")
    before = receipt_path.read_bytes()
    monkeypatch.setattr(local.subprocess, "Popen", lambda *_a, **_kw: pytest.fail("legacy read relaunched"))
    read = executor.evidence(receipt_key)
    assert read["schema_version"] == 1 and read["verification_level"] == "offline_synthetic"
    assert read["application_verification"] == "not_run" and read["accepted"] is False
    assert receipt_path.read_bytes() == before


@pytest.mark.parametrize("role,stage", [("baseline", "build"), ("baseline", "target"),
                                       ("candidate", "build"), ("candidate", "target"), ("candidate", "behavior")])
def test_executed_source_drift_keeps_stage_receipts_without_accepting(tmp_path: Path, managed: Path,
        monkeypatch: pytest.MonkeyPatch, role: str, stage: str) -> None:
    workspace, source, _, executor = fixture(tmp_path, managed, settings={"mutate_role": role,
        "mutate_stage": stage, "mutate_value": "value=bad changed\n"})
    receipt_key = key("source-drift-" + role + "-" + stage)
    status, detail = executor.check(key=receipt_key, source=source)
    assert status == "inconclusive" and "changed the bound execution source" in detail
    evidence = executor.evidence(receipt_key)
    saved_stages = evidence["baseline"]["stages"] if role == "baseline" else evidence["stages"]
    changed = saved_stages[-1]
    assert changed["stage"] == stage and changed["role"] == role
    assert changed["process_started"] is True and changed["source_binding_verified"] is False
    assert changed["stdout"] is not None and changed["finished_at"] is not None
    assert evidence["baseline"]["fault_reproduced"] is (role == "candidate")
    assert evidence["accepted"] is False and evidence["application_verification"] == "not_run"
    # Exercise the application classifier using these authenticated synthetic
    # stage facts, without changing the runtime mode or launching an app.
    assert local.application_check_state({**evidence, "mode": "application"}) == "inconclusive"
    monkeypatch.setattr(local.subprocess, "Popen", lambda *_a, **_kw: pytest.fail("source drift check relaunched"))
    assert executor.check(key=receipt_key, source=source) == (status, detail)
    folder = local.local_evidence_path(workspace.root / "local-execution" / receipt_key)
    path = folder / "execution.json"
    altered = json.loads(path.read_bytes())
    altered_stage = altered["baseline"]["stages"][-1] if role == "baseline" else altered["stages"][-1]
    altered_stage["source_binding_verified"] = True
    path.write_text(json.dumps(altered), encoding="utf-8")
    with pytest.raises(ValueError, match="stage receipt changed"):
        executor.evidence(receipt_key)
