"""Installer identity and isolation; never download or install in these tests."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from dfx_agent import managed_bootstrap as install


def _fixture(tmp_path: Path) -> tuple[Path, dict]:
    repo = tmp_path / "deployment"
    root = repo / ".dfx-local"
    for relative in ("configs", ".dfx-local/tools", ".dfx-local/state", ".dfx-local/env/Scripts", ".dfx-local/tmp"):
        (repo / relative).mkdir(parents=True, exist_ok=True)
    config = json.loads((Path(__file__).parents[1] / "configs/bootstrap.json").read_text())
    policy = json.loads((Path(__file__).parents[1] / "configs/local-runtime.json").read_text())
    (repo / "configs/bootstrap.json").write_text(json.dumps(config))
    (repo / "configs/local-runtime.json").write_text(json.dumps(policy))
    for filename in ("uv.lock", "pyproject.toml"):
        (repo / filename).write_text("frozen")
    (root / "tools/uv.exe").write_bytes(b"fixture-not-executable")
    (root / "state/bootstrap.json").write_text(json.dumps({"uv_version": config["uv_version"],
        "uv_exe_sha256": install._hash(root / "tools/uv.exe").upper()}))
    for filename in ("python.exe", "dfx.exe"):
        (root / "env/Scripts" / filename).write_bytes(b"fixture-not-executable")
    return repo, config


def test_offline_uv_reuses_verified_executable_without_network(tmp_path, monkeypatch):
    repo, config = _fixture(tmp_path)
    monkeypatch.setattr(install.urllib.request, "build_opener", lambda *args: pytest.fail("network forbidden"))
    assert install._uv(repo / ".dfx-local", config, True) == repo / ".dfx-local/tools/uv.exe"


def test_uv_changed_executable_is_rejected_before_execution(tmp_path):
    repo, config = _fixture(tmp_path)
    (repo / ".dfx-local/tools/uv.exe").write_bytes(b"replaced")
    with pytest.raises(ValueError, match="identity"):
        install._uv(repo / ".dfx-local", config, False)


def test_offline_missing_uv_never_downloads(tmp_path):
    root = tmp_path / ".dfx-local"
    root.mkdir()
    with pytest.raises(ValueError, match="offline"):
        install._uv(root, {}, True)


def test_bootstrap_requires_managed_worker_before_install(tmp_path, monkeypatch):
    from dfx_agent import managed_runtime

    repo, _ = _fixture(tmp_path)
    def reject(*args):
        raise ValueError("no managed lease")
    monkeypatch.setattr(managed_runtime, "require_managed_worker", reject)
    monkeypatch.setattr(install.subprocess, "run", lambda *args, **kwargs: pytest.fail("executed without lease"))
    with pytest.raises(ValueError, match="lease"):
        install.bootstrap(repo, offline=True)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows installation profile")
def test_setup_is_locked_and_keeps_all_cache_inside_owned_root(tmp_path, monkeypatch):
    from dfx_agent import managed_runtime

    repo, _ = _fixture(tmp_path)
    called = []
    monkeypatch.setattr(managed_runtime, "require_managed_worker", lambda *args: None)
    monkeypatch.setenv("VIRTUAL_ENV", "outside-project")
    monkeypatch.setenv("CONDA_PREFIX", "outside-project")
    monkeypatch.setenv("PYTHONPATH", "outside-project")
    monkeypatch.setenv("PYTHONHOME", "outside-project")
    monkeypatch.setattr(install.subprocess, "run", lambda arguments, **kw: called.append((arguments, kw)))
    install.bootstrap(repo, offline=True)
    sync, options = next(item for item in called if "sync" in item[0])
    assert "--locked" in sync and "--offline" in sync
    assert not any("lock" in args for args, _ in called)
    environment = options["env"]
    assert all(key not in environment for key in ("VIRTUAL_ENV", "CONDA_PREFIX", "PYTHONPATH", "PYTHONHOME"))
    for name in ("UV_CACHE_DIR", "UV_PYTHON_INSTALL_DIR", "UV_PROJECT_ENVIRONMENT", "TEMP", "TMP"):
        assert Path(environment[name]).is_relative_to(repo / ".dfx-local")
    assert environment["UV_PYTHON_NO_REGISTRY"] == "1"
    assert options["shell"] is False and options["cwd"] == repo
    state = install._read(repo / ".dfx-local/state/environment.json")
    assert state["lock_sha256"] == install._hash(repo / "uv.lock").upper()
    assert state["environment_python"] == str(repo / ".dfx-local/env/Scripts/python.exe")


@pytest.mark.skipif(sys.platform != "win32", reason="Windows installation profile")
def test_failed_install_does_not_publish_new_environment_identity(tmp_path, monkeypatch):
    from dfx_agent import managed_runtime

    repo, _ = _fixture(tmp_path)
    state = repo / ".dfx-local/state/environment.json"
    state.write_text('{"completed_at":"previous-failed-is-not-new"}')
    original = state.read_bytes()
    monkeypatch.setattr(managed_runtime, "require_managed_worker", lambda *args: None)
    def fail(arguments, **kwargs):
        if "sync" in arguments:
            raise install.subprocess.CalledProcessError(1, "fixture sync")
    monkeypatch.setattr(install.subprocess, "run", fail)
    with pytest.raises(install.subprocess.CalledProcessError):
        install.bootstrap(repo, offline=True)
    assert state.read_bytes() == original
