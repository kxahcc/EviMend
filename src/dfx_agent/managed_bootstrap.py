"""Pinned project installation, executed only inside the managed Windows Job.

This module uses only the Python standard library so a normal Python launcher
can prepare the project before its Agent dependencies have been installed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import stat
import subprocess
import sys
import urllib.request
import zipfile
from datetime import UTC, datetime
from pathlib import Path


def _checked(path: Path, *, exists: bool = True) -> Path:
    path = path.absolute()
    for part in (path, *path.parents):
        try:
            info = part.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError("bootstrap paths may not contain links")
    return path.resolve(strict=exists)


def _hash(path: Path) -> str:
    return hashlib.sha256(_checked(path).read_bytes()).hexdigest()


def _read(path: Path) -> dict:
    raw = _checked(path)
    if raw.stat().st_size > 65536:
        raise ValueError("oversized bootstrap metadata")
    result = json.loads(raw.read_text(encoding="utf-8-sig"))
    if not isinstance(result, dict):
        raise TypeError("bootstrap metadata must be an object")
    return result


def _save(path: Path, value: dict) -> None:
    _checked(path, exists=False)
    pending = path.with_name(path.name + ".new")
    _checked(pending, exists=False)
    pending.write_text(json.dumps(value, ensure_ascii=True, indent=2), encoding="utf-8")
    pending.replace(path)


def _uv(root: Path, config: dict, offline: bool) -> Path:
    executable = _checked(root / "tools/uv.exe", exists=False)
    state_path = root / "state/bootstrap.json"
    if executable.exists() or state_path.exists():
        state = _read(state_path)
        if (state.get("uv_version") != config["uv_version"] or
                _hash(executable) != str(state.get("uv_exe_sha256", "")).lower()):
            raise ValueError("recorded project uv identity differs")
        return executable
    if offline:
        raise ValueError("offline setup needs an existing project uv executable")
    temporary = root / "tmp/uv-bootstrap.zip"
    extracted = root / "tmp/uv-bootstrap.exe"
    for path in (temporary, extracted):
        _checked(path, exists=False)
        if path.exists():
            raise ValueError("unfinished bootstrap download exists; inspect managed tmp")
    maximum = config["uv_archive_max_bytes"]
    # Only this pinned installer archive is downloaded here; SDK/models are not.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(config["uv_archive_url"], timeout=180) as response, temporary.open("xb") as output:
            total = 0
            while chunk := response.read(65536):
                total += len(chunk)
                if total > maximum:
                    raise ValueError("uv archive exceeds configured size")
                output.write(chunk)
        if _hash(temporary) != config["uv_archive_sha256"]:
            raise ValueError("uv archive checksum differs")
        with zipfile.ZipFile(temporary) as archive:
            matches = [item for item in archive.infolist() if item.filename == "uv.exe"]
            if len(matches) != 1 or matches[0].file_size > maximum:
                raise ValueError("uv executable missing or oversized")
            with archive.open(matches[0]) as source, extracted.open("xb") as output:
                total = 0
                while chunk := source.read(65536):
                    total += len(chunk)
                    if total > maximum:
                        raise ValueError("expanded uv executable exceeds configured size")
                    output.write(chunk)
        extracted.replace(executable)
        _save(state_path, {"schema_version": 1, "uv_version": config["uv_version"],
            "uv_archive_sha256": config["uv_archive_sha256"], "uv_exe_sha256": _hash(executable),
            "installed_at": datetime.now(UTC).isoformat()})
    finally:
        # Exact files created by this download only; interrupted files stay for
        # the managed cleaner, rather than being swept on the next startup.
        if temporary.is_file():
            temporary.unlink()
        if extracted.is_file():
            extracted.unlink()
    return executable


def bootstrap(repo: Path, *, offline: bool = False) -> None:
    from .managed_runtime import require_managed_worker

    repo = _checked(repo)
    root = _checked(repo / ".dfx-local")
    require_managed_worker(repo, "bootstrap")
    config = _read(repo / "configs/bootstrap.json")
    if (os.name != "nt" or platform.machine().upper() not in {"AMD64", "X86_64"} or
            config.get("schema_version") != 1 or config.get("uv_version") != "0.12.9" or
            config.get("python_version") != "3.13.5" or
            config.get("uv_archive_url") != "https://releases.astral.sh/github/uv/releases/download/0.12.9/uv-x86_64-pc-windows-msvc.zip" or
            config.get("uv_archive_sha256") != "ddbfcee1ac615a0499f6aa97b5ec8ebdf3ee4a7714a48055ec2ba0030e3cf810" or
            type(config.get("uv_archive_max_bytes")) is not int or
            not 1048576 <= config["uv_archive_max_bytes"] <= 52428800):
        raise ValueError("unsupported pinned bootstrap configuration")
    policy = _read(repo / "configs/local-runtime.json")
    environment = dict(os.environ)
    mapping = {"UV_CACHE_DIR": "cache/uv", "UV_PYTHON_CACHE_DIR": "cache/python",
        "UV_PYTHON_INSTALL_DIR": "python", "UV_PYTHON_BIN_DIR": "tools/python-bin",
        "UV_PROJECT_ENVIRONMENT": "env", "UV_TOOL_DIR": "tools/uv-tools",
        "UV_TOOL_BIN_DIR": "tools/bin", "PYTHONPYCACHEPREFIX": "cache/pycache",
        "RUFF_CACHE_DIR": "cache/ruff", "TEMP": "tmp", "TMP": "tmp"}
    for key, relative in mapping.items():
        path = _checked(root / relative, exists=False)
        path.mkdir(parents=True, exist_ok=True)
        environment[key] = str(path)
    for key in ("VIRTUAL_ENV", "CONDA_PREFIX", "PYTHONPATH", "PYTHONHOME"):
        environment.pop(key, None)
    environment.update(UV_MANAGED_PYTHON="1", UV_PYTHON_NO_REGISTRY="1", UV_NO_CONFIG="1",
        UV_CONCURRENT_DOWNLOADS=str(policy["limits"]["max_download_concurrency"]),
        UV_CONCURRENT_BUILDS=str(policy["limits"]["max_build_concurrency"]))
    if offline:
        environment["UV_PYTHON_DOWNLOADS"] = "never"
    uv = _uv(root, config, offline)

    def run(arguments: list[str]) -> None:
        subprocess.run(arguments, cwd=repo, env=environment, check=True, shell=False)

    run([str(uv), "--version"])
    python = root / "env/Scripts/python.exe"
    if offline:
        _checked(python)
        run([str(python), "-c", "import sys; raise SystemExit(0 if sys.version_info[:3] == (3,13,5) else 1)"])
    else:
        run([str(uv), "python", "install", config["python_version"], "--no-progress"])
    arguments = [str(uv), "sync", "--locked", "--python",
                 str(python) if offline else config["python_version"], "--no-progress"]
    if offline:
        arguments.append("--offline")
    run(arguments)
    _checked(python)
    run([str(python), "-c", "import deepagents, pydantic, langgraph, openai; print('Agent dependencies ready')"])
    product = _checked(root / "env/Scripts/dfx.exe")
    run([str(product), "--version"])
    run([str(product), "--help"])
    previous = _read(root / "state/environment.json") if (root / "state/environment.json").exists() else {}
    host = _checked(Path(sys.executable))
    host_python = str(host) if not host.is_relative_to(root) else previous.get("bootstrap_host_python")
    _save(root / "state/environment.json", {"schema_version": 1,
        "uv_version": config["uv_version"], "python_version": config["python_version"],
        "uv_exe_sha256": _hash(uv).upper(), "lock_sha256": _hash(repo / "uv.lock").upper(),
        "pyproject_sha256": _hash(repo / "pyproject.toml").upper(),
        "environment_python": str(python), "bootstrap_host_python": host_python,
        "completed_at": datetime.now(UTC).isoformat()})


def main() -> int:
    parser = argparse.ArgumentParser(description="Managed project dependency worker")
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--offline", action="store_true")
    args = parser.parse_args()
    bootstrap(args.workspace, offline=args.offline)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
