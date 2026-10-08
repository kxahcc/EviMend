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
if settings.get("mutate_stage") == stage and settings.get("mutate_role", role) == role:
    Path(settings["source"]).write_text(settings.get("mutate_value", "value=bad\\n"))
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
            output: int = 131072, fault: str = "SyntheticTargetFault") -> tuple[
                CandidateWorkspace, Path, LocalCheckConfig, LocalApplicationExecutor]:
    project = tmp_path / "original-project"
    full_source = "module/main.ets" if mapping else "src/main.ets"
    (project / Path(full_source).parent).mkdir(parents=True)
    (project / full_source).write_bytes(b"value=bad\n")
    (project / "assets").mkdir()
    (project / "assets/image.bin").write_bytes(bytes(range(256)))
    (project / "empty-assets").mkdir()
    (project / "verify.py").write_text(PROGRAM, encoding="utf-8")
    (project / "checks.json").write_text(json.dumps({"source": full_source, **(settings or {})}), encoding="utf-8")
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
    config = LocalCheckConfig(project_root=str(project), commands={
        stage: LocalCommand(argv=[sys.executable, "verify.py", stage], timeout_seconds=timeout)
        for stage in local.STAGES}, baseline_target_exit_codes=[9], baseline_fault_pattern=fault,
        source_mapping={"src/main.ets": full_source} if mapping else {},
        max_output_bytes=output, max_check_seconds=30, credential_env_names=["CLOUD_TEST_SECRET"])
    executor = LocalApplicationExecutor(config, workspace, managed_root=managed_root)
    return workspace, source, config, executor


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
