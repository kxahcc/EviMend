"""Native ownership/cleanup boundaries and real Windows suspended Job behavior."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from contextlib import nullcontext
from pathlib import Path

import pytest

from dfx_agent import managed_runtime as runtime
from dfx_agent.windows_job import BoundedJob, process_identity


def test_original_project_copy_exception_does_not_enable_data_as_executable_input(tmp_path: Path) -> None:
    original = tmp_path / "data_B/application"
    original.mkdir(parents=True)
    with pytest.raises(ValueError):
        runtime._input(tmp_path, "data_B/application", directory=True)
    assert runtime._input(tmp_path, "data_B/application", directory=True, original_copy=True) == original.resolve()
    for forbidden in ("../application", "archive/application", ".git/application"):
        with pytest.raises(ValueError):
            runtime._input(tmp_path, forbidden, directory=True, original_copy=True)


@pytest.fixture
def managed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> runtime.Runtime:
    root = tmp_path / "project"
    config = root / "configs"
    config.mkdir(parents=True)
    source = Path(__file__).parents[1] / "configs/local-runtime.json"
    shutil.copyfile(source, config / "local-runtime.json")
    monkeypatch.setattr(runtime.Runtime, "mutex", lambda self: nullcontext())
    value = runtime.Runtime(root)
    value.prepare()
    return value


def _clean(value: runtime.Runtime, *options: str) -> dict:
    args = runtime.parser().parse_args(["--workspace", str(value.repo), "Clean", *options])
    return value.clean(args)


@pytest.mark.skipif(os.name != "nt", reason="Windows standalone launcher")
@pytest.mark.parametrize("scope", ["env", "all"])
def test_external_setup_launcher_applies_preview_and_preserves_project_and_exports(
    managed: runtime.Runtime, scope: str,
) -> None:
    """Use the actual setup launcher after the installed CLI has exited.

    The temporary deployment contains only standard-library management code;
    its environment files are deliberate non-executable cleanup fixtures. No
    model/dependency install is performed. The parent managed Job still bounds
    the ordinary Python launcher and its filesystem work.
    """
    repository = Path(__file__).parents[1]
    scripts = managed.repo / "scripts"
    package = managed.repo / "src/dfx_agent"
    scripts.mkdir()
    package.mkdir(parents=True)
    for relative in ("scripts/bootstrap.py", "setup.cmd", "src/dfx_agent/__init__.py",
                     "src/dfx_agent/managed_runtime.py", "src/dfx_agent/windows_job.py"):
        shutil.copyfile(repository / relative, managed.repo / relative)
    for relative in ("tools/owned.txt", "python/owned.txt", "env/owned.txt"):
        target = managed.root / relative
        target.parent.mkdir(parents=True)
        target.write_bytes(b"explicit fixture owned by this deployment")
    sentinels = {managed.repo / "data_B/keep.txt": b"original data",
                 managed.exports / "keep.json": b"exported evidence",
                 managed.repo / "pyproject.toml": b"tracked configuration"}
    for path, content in sentinels.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    launcher = Path(os.environ["SystemRoot"]) / "py.exe"
    assert launcher.is_file(), "this acceptance requires an ordinary external Python launcher"
    environment = dict(os.environ)
    environment["PATH"] = str(launcher.parent / "System32") + os.pathsep + str(launcher.parent)
    for key in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "CONDA_PREFIX", "DFX_PROJECT_ROOT"):
        environment.pop(key, None)

    def external(*options: str) -> dict:
        result = subprocess.run([str(launcher.parent / "System32/cmd.exe"), "/d", "/c", "setup.cmd",
                                 "--clean", "--scope", scope, *options], cwd=managed.repo,
                                env=environment, capture_output=True, text=True, check=True, timeout=20)
        return json.loads(result.stdout)

    preview = external(*(["--discard-evidence"] if scope == "all" else []))
    assert preview["dry_run"] is True
    assert (managed.root / "env/owned.txt").exists()
    result = external("--apply", "--plan-id", preview["plan_id"],
                      *(["--discard-evidence"] if scope == "all" else []))
    assert result["applied"] is True
    assert not (managed.root / "env").exists()
    assert (managed.root.exists()) is (scope == "env")
    assert all(path.read_bytes() == content for path, content in sentinels.items())


def test_native_prepare_preserves_legacy_owner_and_refuses_foreign_owner(managed: runtime.Runtime) -> None:
    path = managed.root / "owner.json"
    original = path.read_bytes()
    managed.prepare()
    assert path.read_bytes() == original
    owner = json.loads(original)
    owner["repo_real_path"] = str(managed.repo / "other")
    path.write_text(json.dumps(owner))
    with pytest.raises(ValueError, match="owner differs"):
        managed.prepare()


@pytest.mark.skipif(os.name != "nt", reason="Windows extended filesystem paths")
def test_deep_inventory_and_cleanup_keep_canonical_owner_and_foreign_sentinels(managed: runtime.Runtime) -> None:
    directory = managed.root / "cache" / ("deep-project-" * 12) / ("nested-" * 12)
    filename = directory / "evidence.json"
    assert len(str(filename)) > 260
    runtime._io_path(directory).mkdir(parents=True)
    runtime._io_path(filename).write_bytes(b"same owned filesystem bytes")
    checked = runtime.checked(filename, managed.repo, exists=True)
    assert checked == filename and not str(checked).startswith("\\\\?\\")
    size, errors = managed.inventory(managed.root / "cache")
    assert size == len(b"same owned filesystem bytes") and errors == []
    external = managed.repo / "data_B/keep.txt"
    external.parent.mkdir()
    external.write_bytes(b"foreign original data")
    preview = _clean(managed, "-Scope", "cache")
    assert preview["estimated_bytes"] == size
    result = _clean(managed, "-Scope", "cache", "-Apply", "-PlanId", preview["plan_id"])
    assert result["applied"] is True and not (managed.root / "cache").exists()
    assert external.read_bytes() == b"foreign original data"


@pytest.mark.parametrize("value", ["../private", "data_B/task", "archive/task", ".git/config", "a//b", "a/./b", "a/b.", "a:b"])
def test_native_input_rejects_unsafe_or_original_material(managed: runtime.Runtime, value: str) -> None:
    with pytest.raises(ValueError):
        runtime._input(managed.repo, value)


def test_native_status_preserves_incomplete_accounting(managed: runtime.Runtime, monkeypatch: pytest.MonkeyPatch) -> None:
    blocked = managed.root / "tmp/blocked"
    blocked.mkdir(parents=True)
    original = runtime.os.scandir

    def directories(path: Path):
        if Path(path) == runtime._io_path(blocked):
            raise PermissionError("not readable")
        return original(path)

    monkeypatch.setattr(runtime.os, "scandir", directories)
    observation = managed.status()
    assert observation["resource_accounting"] == "incomplete"
    assert observation["inaccessible_paths"] == [str(blocked)]
    assert observation["cleanup_available"] is False


@pytest.mark.skipif(os.name != "nt", reason="Windows deep evidence paths")
@pytest.mark.parametrize("scope", ["tmp", "cache"])
def test_deep_task_evidence_still_requires_explicit_discard(managed: runtime.Runtime, scope: str) -> None:
    task = managed.root / scope / ("deep-" * 32) / ("nested-" * 12) / "task.json"
    assert len(str(task)) > 260
    runtime._io_path(task.parent).mkdir(parents=True)
    runtime._io_path(task).write_bytes(b"host evidence must not be discarded implicitly")
    with pytest.raises(ValueError, match="evidence requires explicit discard"):
        _clean(managed, "-Scope", scope)
    assert runtime._io_path(task).read_bytes() == b"host evidence must not be discarded implicitly"


def test_native_cleanup_preview_apply_and_drift(managed: runtime.Runtime) -> None:
    cache = managed.root / "cache"
    cache.mkdir()
    target = cache / "owned.txt"
    target.write_text("first")
    plan = _clean(managed, "-Scope", "testcache")
    assert plan["dry_run"] is True
    assert target.exists()
    plan = _clean(managed, "-Scope", "cache")
    target.write_text("changed and bigger")
    with pytest.raises(ValueError, match="changed or expired"):
        _clean(managed, "-Scope", "cache", "-Apply", "-PlanId", plan["plan_id"])
    assert target.exists()
    plan = _clean(managed, "-Scope", "cache")
    receipt = _clean(managed, "-Scope", "cache", "-Apply", "-PlanId", plan["plan_id"])
    assert receipt["applied"] is True
    assert not cache.exists()
    assert (managed.root / "owner.json").exists()


def test_native_cleanup_refuses_unknown_activity_and_evidence(managed: runtime.Runtime) -> None:
    managed.state.mkdir(exist_ok=True)
    active = managed.state / "active.json"
    active.write_text("{}")
    with pytest.raises(ValueError, match="activity"):
        _clean(managed, "-Scope", "tmp", "-DiscardEvidence")
    active.unlink()
    work = managed.root / "work/competition-existing"
    work.mkdir(parents=True)
    with pytest.raises(FileNotFoundError):
        _clean(managed, "-Scope", "work", "-DiscardEvidence")
    (work / "run-state.json").write_text('{"status":"finished"}')
    with pytest.raises(ValueError, match="evidence"):
        _clean(managed, "-Scope", "work")
    assert _clean(managed, "-Scope", "work", "-DiscardEvidence")["dry_run"] is True


def test_native_cleanup_all_keeps_reviewed_owner_and_exports_receipt(managed: runtime.Runtime) -> None:
    retained = managed.repo / "retained.txt"
    retained.write_text("private input remains")
    plan = _clean(managed, "-Scope", "all", "-DiscardEvidence")
    result = _clean(managed, "-Scope", "all", "-DiscardEvidence", "-Apply", "-PlanId", plan["plan_id"])
    assert result["applied"] is True
    assert not managed.root.exists()
    assert retained.read_text() == "private input remains"
    assert (managed.exports / ("cleanup-" + plan["plan_id"] + ".json")).is_file()


def test_native_environment_rejects_lock_or_path_drift(managed: runtime.Runtime) -> None:
    python = managed.root / "env/Scripts/python.exe"
    python.parent.mkdir(parents=True)
    python.write_bytes(b"fixture executable, never run")
    (managed.repo / "uv.lock").write_text("locked")
    (managed.repo / "pyproject.toml").write_text("project")
    state = {"environment_python": str(python), "lock_sha256": runtime._sha(managed.repo / "uv.lock"),
             "pyproject_sha256": runtime._sha(managed.repo / "pyproject.toml")}
    (managed.state / "environment.json").write_text(json.dumps(state))
    assert managed.environment_python() == python
    (managed.repo / "uv.lock").write_text("different")
    with pytest.raises(ValueError, match="lock or path"):
        managed.environment_python()


def test_native_exact_stop_refuses_replaced_run_before_any_process_operation(
    managed: runtime.Runtime, monkeypatch: pytest.MonkeyPatch,
) -> None:
    (managed.state / "active.json").write_text(json.dumps({"run_id": str(uuid.uuid4())}))
    monkeypatch.setattr(runtime, "process_identity", lambda _: pytest.fail("must not inspect unrelated PID"))
    with pytest.raises(ValueError, match="changed"):
        managed.stop(str(uuid.uuid4()))


@pytest.mark.parametrize("fault", ["manager_dead", "unrelated_worker", "wrong_task", "weakened_job"])
def test_native_worker_admission_requires_owner_live_manager_ancestry_and_actual_limits(
    managed: runtime.Runtime, monkeypatch: pytest.MonkeyPatch, fault: str,
) -> None:
    manager_id = str(uuid.uuid4())
    marker = {"schema_version": 1, "project_instance_id": managed.owner()["project_instance_id"],
              "run_id": manager_id, "manager_pid": 17, "manager_start_utc_ticks": 100,
              "manager_executable": "approved-python.exe", "child_pid": 20, "child_start_utc_ticks": 200,
              "task": "competition" if fault == "wrong_task" else "bootstrap",
              "job_name": "Local\\DFX-Guard-" + manager_id,
              "effective_limits": {"cpu_percent": 20, "memory_bytes": 134217728, "max_processes": 4}}
    (managed.state / "active.json").write_text(json.dumps(marker))
    monkeypatch.setattr(runtime, "process_identity", lambda _: None if fault == "manager_dead" else (100, "approved-python.exe"))
    monkeypatch.setattr(runtime, "lease_worker_identity", lambda _: fault != "unrelated_worker")
    monkeypatch.setattr(runtime, "current_job_limits", lambda _: {"cpu_percent": 100 if fault == "weakened_job" else 20,
                        "memory_bytes": 134217728, "max_processes": 4})
    with pytest.raises(ValueError):
        runtime.require_managed_worker(managed.repo, "bootstrap")


def test_native_backend_command_has_no_shell_or_codex_requirement(managed: runtime.Runtime, monkeypatch: pytest.MonkeyPatch) -> None:
    from dfx_agent import product_cli
    monkeypatch.setattr(product_cli.os, "name", "nt")
    result = product_cli._backend(managed.repo, ["Status"])
    assert result == [str(Path(getattr(sys, "_base_executable", sys.executable)).resolve()), "-B", "-m", "dfx_agent.managed_runtime", "--workspace", str(managed.repo), "Status"]
    assert not any("pwsh" in argument or ".ps1" in argument for argument in result)


@pytest.mark.skipif(os.name != "nt", reason="Windows Job ABI")
def test_native_job_assigns_before_resume_and_kills_worker_and_descendant(tmp_path: Path) -> None:
    marker = tmp_path / "resumed.txt"
    late = tmp_path / "descendant.txt"
    script = tmp_path / "worker.py"
    script.write_text("import pathlib,subprocess,sys,time\n"
                      "pathlib.Path(sys.argv[1]).write_text('started')\n"
                      "subprocess.Popen([sys.executable,'-c',\"import time,pathlib; time.sleep(5); pathlib.Path(\"+repr(sys.argv[2])+\").write_text('escaped')\"])\n"
                      "time.sleep(30)\n")
    job = BoundedJob("Local\\DFX-native-test-" + uuid.uuid4().hex, 20, 134217728, 4)
    try:
        job.start_suspended([sys.executable, str(script), str(marker), str(late)], tmp_path, dict(os.environ), tmp_path / "job.log", 4096)
        identity = process_identity(job.pid)
        assert identity is not None
        time.sleep(0.1)
        assert not marker.exists()
        job.resume()
        deadline = time.monotonic() + 5
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert marker.read_text() == "started"
        time.sleep(0.1)
    finally:
        pid = job.pid
        job.close()
    assert process_identity(pid) is None
    time.sleep(5.1)
    assert not late.exists()
    assert job.output_error is False


@pytest.mark.skipif(os.name != "nt", reason="Windows Job ABI")
def test_native_job_caps_and_stops_oversized_output(tmp_path: Path) -> None:
    job = BoundedJob("Local\\DFX-native-log-" + uuid.uuid4().hex, 20, 134217728, 2)
    log = tmp_path / "job.log"
    try:
        job.start_suspended([sys.executable, "-c", "import sys,time; sys.stdout.write('x'*16384); sys.stdout.flush(); time.sleep(30)"],
                            tmp_path, dict(os.environ), log, 1024)
        job.resume()
        deadline = time.monotonic() + 5
        while not job.exited() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert job.exited()
    finally:
        job.close()
    assert job.log_exceeded is True
    assert log.stat().st_size == 1024
