"""Freeze host execution facts without importing or executing project components.

Hashes bind recorded bytes and version claims. They do not authenticate a checkout,
installed package binaries, or a later application execution.
"""

from __future__ import annotations

import copy
import hashlib
import importlib.metadata
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).absolute().parents[2]
_HOST_SCRIPTS = (
    "scripts/dfx.py", "scripts/local_agent.ps1", "scripts/local.ps1", "scripts/local_job.cs",
)
_INPUTS = ("uv.lock", "pyproject.toml", ".python-version", "configs/local-runtime.json")
_MAX_FILES = 4096
_MAX_FILE_BYTES = 16 * 1024 * 1024
_MAX_SOURCE_BYTES = 64 * 1024 * 1024
_MAX_DISTRIBUTIONS = 4096
_REQUIRED_DISTRIBUTIONS = frozenset({"deepagents", "langchain-core", "langgraph",
                                     "langgraph-checkpoint-sqlite", "pydantic", "httpx"})
_GIT_OUTPUT_BYTES = 1024 * 1024
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_DIST_NAME = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")


def _canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")


def _sha(value: object) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _keys(value: object, expected: set[str], label: str) -> dict[str, Any]:
    if type(value) is not dict or set(value) != expected:
        raise ValueError(f"invalid execution provenance {label}")
    return value


def _text(value: object, limit: int = 256) -> str:
    if (type(value) is not str or not value or len(value) > limit or value.strip() != value or
            any(ord(char) < 32 or ord(char) == 127 for char in value)):
        raise ValueError("invalid execution provenance text")
    return value


def _hash(value: object) -> str:
    if type(value) is not str or not _HASH.fullmatch(value):
        raise ValueError("invalid execution provenance hash")
    return value


def _file_record(value: object) -> dict[str, Any]:
    record = _keys(value, {"size_bytes", "sha256"}, "file record")
    size = record["size_bytes"]
    if type(size) is not int or not 0 <= size <= _MAX_FILE_BYTES:
        raise ValueError("invalid execution provenance file size")
    _hash(record["sha256"])
    return record


def _source_path(value: object) -> str:
    path = _text(value, 1024)
    if path in _HOST_SCRIPTS:
        return path
    parts = path.split("/")
    if (len(parts) < 3 or parts[:2] != ["src", "dfx_agent"] or
            not path.endswith(".py") or any(part in {"", ".", "..", "__pycache__"}
                                            for part in parts) or
            any("\\" in part or ":" in part for part in parts)):
        raise ValueError("invalid execution provenance source path")
    return path


def _seal(value: dict[str, Any]) -> dict[str, Any]:
    """Recompute the three hash tiers; strict validation remains a separate step."""
    result = copy.deepcopy(value)
    result["source_inventory_sha256"] = _sha(result["source_inventory"])
    result["compatibility_sha256"] = _sha({
        "source_inventory_sha256": result["source_inventory_sha256"],
        "repository_inputs": result["repository_inputs"], "runtime": result["runtime"],
    })
    result.pop("provenance_sha256", None)
    result["provenance_sha256"] = _sha(result)
    return result


def validate_execution_provenance(value: object) -> dict[str, Any]:
    """Validate exact JSON types, field sets and all three canonical hashes."""
    record = _keys(value, {"schema_version", "source_inventory", "source_inventory_sha256",
                           "repository_inputs", "runtime", "git", "compatibility_sha256",
                           "provenance_sha256"}, "record")
    if type(record["schema_version"]) is not int or record["schema_version"] != 1:
        raise ValueError("unsupported execution provenance schema")
    files = record["source_inventory"]
    if type(files) is not list or not 1 <= len(files) <= _MAX_FILES:
        raise ValueError("invalid execution provenance inventory")
    paths = []
    total = 0
    for item in files:
        entry = _keys(item, {"path", "size_bytes", "sha256"}, "source entry")
        paths.append(_source_path(entry["path"]))
        _file_record({"size_bytes": entry["size_bytes"], "sha256": entry["sha256"]})
        total += entry["size_bytes"]
    if (paths != sorted(paths) or len({path.casefold() for path in paths}) != len(paths) or
            not set(_HOST_SCRIPTS).issubset(paths) or
            "src/dfx_agent/provenance.py" not in paths or total > _MAX_SOURCE_BYTES):
        raise ValueError("invalid execution provenance inventory scope")
    inputs = _keys(record["repository_inputs"], set(_INPUTS), "repository inputs")
    for item in inputs.values():
        _file_record(item)
    runtime = _keys(record["runtime"], {"python", "distributions"}, "runtime")
    python = _keys(runtime["python"], {"implementation", "version", "cache_tag", "platform",
                                       "machine"}, "Python runtime")
    for item in python.values():
        _text(item)
    if not re.fullmatch(r"\d+\.\d+\.\d+", python["version"]):
        raise ValueError("invalid execution provenance Python version")
    distributions = runtime["distributions"]
    if (type(distributions) is not dict or not 1 <= len(distributions) <= _MAX_DISTRIBUTIONS or
            not _REQUIRED_DISTRIBUTIONS.issubset(distributions)):
        raise ValueError("invalid execution provenance distributions")
    for name, version in distributions.items():
        if type(name) is not str or len(name) > 256 or not _DIST_NAME.fullmatch(name):
            raise ValueError("invalid execution provenance distribution name")
        _text(version)
    git = _keys(record["git"], {"available", "commit", "dirty", "status_sha256",
                                "status_entries"}, "Git facts")
    if type(git["available"]) is not bool:
        raise ValueError("invalid execution provenance Git availability")
    if git["available"]:
        if (type(git["commit"]) is not str or
                not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", git["commit"]) or
                type(git["dirty"]) is not bool or type(git["status_entries"]) is not int or
                not 0 <= git["status_entries"] <= _GIT_OUTPUT_BYTES or
                git["dirty"] != (git["status_entries"] > 0)):
            raise ValueError("invalid execution provenance Git facts")
        _hash(git["status_sha256"])
        if not git["dirty"] and git["status_sha256"] != hashlib.sha256(b"").hexdigest():
            raise ValueError("invalid execution provenance clean Git status")
    elif any(git[key] is not None for key in git if key != "available"):
        raise ValueError("unknown Git facts must remain null")
    for key in ("source_inventory_sha256", "compatibility_sha256", "provenance_sha256"):
        _hash(record[key])
    if _seal(record) != record:
        raise ValueError("execution provenance hash mismatch")
    return copy.deepcopy(record)


def validate_session_provenance(session: object) -> dict[str, Any] | None:
    if type(session) is not dict or type(session.get("schema_version")) is not int:
        raise ValueError("invalid session provenance schema")
    schema = session["schema_version"]
    if schema == 8:
        if "execution_provenance" in session:
            raise ValueError("historical session cannot acquire execution provenance")
        return None
    if schema not in (9, 10, 11) or "execution_provenance" not in session:
        raise ValueError("unsupported session provenance schema")
    return validate_execution_provenance(session["execution_provenance"])


def _normal(path: Path, *, directory: bool) -> os.stat_result:
    info: os.stat_result | None = None
    try:
        for component in (*reversed(path.parents), path):
            info = component.lstat()
            if (stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) &
                    getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)):
                raise ValueError("execution provenance path is linked")
            if component != path and not stat.S_ISDIR(info.st_mode):
                raise ValueError("execution provenance parent is not a directory")
        assert info is not None  # The traversal always includes path itself.
        if directory:
            if not stat.S_ISDIR(info.st_mode):
                raise ValueError("execution provenance directory is missing")
        elif not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError("execution provenance file is not independent")
        return info
    except OSError as exc:
        raise ValueError("execution provenance path is missing or inaccessible") from exc


def _identity(info: os.stat_result) -> tuple[int, int, int, int]:
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def _read_record(path: Path) -> dict[str, Any]:
    before = _normal(path, directory=False)
    if before.st_size > _MAX_FILE_BYTES:
        raise ValueError("execution provenance file exceeds its bound")
    digest = hashlib.sha256()
    size = 0
    try:
        with path.open("rb") as stream:
            if _identity(os.fstat(stream.fileno())) != _identity(before):
                raise ValueError("execution provenance file changed before reading")
            while block := stream.read(65536):
                size += len(block)
                if size > _MAX_FILE_BYTES:
                    raise ValueError("execution provenance file exceeds its bound")
                digest.update(block)
            if _identity(os.fstat(stream.fileno())) != _identity(before):
                raise ValueError("execution provenance file changed while reading")
    except OSError as exc:
        raise ValueError("execution provenance file cannot be read") from exc
    if _identity(_normal(path, directory=False)) != _identity(before) or size != before.st_size:
        raise ValueError("execution provenance file changed after reading")
    return {"size_bytes": size, "sha256": digest.hexdigest()}


def _source_files() -> list[Path]:
    files = [_ROOT / path for path in _HOST_SCRIPTS]
    pending = [_ROOT / "src" / "dfx_agent"]
    nodes = 0
    seen: set[str] = set()
    while pending:
        directory = pending.pop()
        _normal(directory, directory=True)
        try:
            children = sorted(directory.iterdir(), key=lambda item: item.name)
        except OSError as exc:
            raise ValueError("execution provenance source directory cannot be read") from exc
        for path in children:
            nodes += 1
            if nodes > _MAX_FILES * 4:
                raise ValueError("execution provenance source traversal exceeds its bound")
            relative = path.relative_to(_ROOT).as_posix()
            if relative.casefold() in seen:
                raise ValueError("execution provenance source has a case collision")
            seen.add(relative.casefold())
            try:
                info = path.lstat()
            except OSError as exc:
                raise ValueError("execution provenance source node cannot be inspected") from exc
            if stat.S_ISDIR(info.st_mode):
                _normal(path, directory=True)
                if path.name != "__pycache__":
                    pending.append(path)
            else:
                _normal(path, directory=False)
                if path.suffix == ".py":
                    files.append(path)
                elif path.suffix.lower() in {".py", ".pyd", ".so", ".dll"}:
                    raise ValueError("execution provenance source contains unsupported code")
                elif relative != "src/dfx_agent/README.md":
                    raise ValueError("execution provenance source contains an unregistered adjunct")
            if len(files) > _MAX_FILES:
                raise ValueError("execution provenance source inventory exceeds its bound")
    return sorted(files, key=lambda item: item.relative_to(_ROOT).as_posix())


def _inventory() -> list[dict[str, Any]]:
    paths = _source_files()
    result = [{"path": path.relative_to(_ROOT).as_posix(), **_read_record(path)} for path in paths]
    if sum(item["size_bytes"] for item in result) > _MAX_SOURCE_BYTES:
        raise ValueError("execution provenance source bytes exceed their bound")
    if _source_files() != paths:
        raise ValueError("execution provenance source inventory changed")
    return result


def _runtime_facts() -> dict[str, Any]:
    versions: dict[str, str] = {}
    for distribution in importlib.metadata.distributions():
        name = distribution.metadata.get("Name")
        if type(name) is not str:
            raise ValueError("installed distribution has no name")
        normalized = re.sub(r"[-_.]+", "-", name).lower()
        if normalized in versions:
            raise ValueError("installed distribution identity is duplicated")
        versions[normalized] = distribution.version
        if len(versions) > _MAX_DISTRIBUTIONS:
            raise ValueError("installed distribution inventory exceeds its bound")
    return {"python": {
        "implementation": sys.implementation.name,
        "version": ".".join(map(str, sys.version_info[:3])),
        "cache_tag": sys.implementation.cache_tag, "platform": sys.platform,
        "machine": platform.machine(),
    }, "distributions": dict(sorted(versions.items()))}


def _git_output(executable: str, arguments: list[str]) -> bytes:
    """Bound stdout while reading it; never enable hooks, fsmonitor or index writes."""
    # Inherited routing variables could name another checkout; tracing variables
    # could create files even during a nominally read-only status operation.
    environment = {key: value for key, value in os.environ.items()
                   if not key.upper().startswith("GIT_")}
    environment.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
                        "GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0"})
    command = [executable, "-c", "core.fsmonitor=false", "-c", "core.hooksPath=" + os.devnull,
               "-c", "core.untrackedCache=false", "-c", "status.submoduleSummary=false",
               *arguments]
    output = bytearray()
    failure: list[BaseException] = []
    with subprocess.Popen(command, cwd=_ROOT, env=environment, stdin=subprocess.DEVNULL,
                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as process:
        assert process.stdout is not None

        def consume() -> None:
            try:
                assert process.stdout is not None
                while block := process.stdout.read(65536):
                    if len(output) + len(block) > _GIT_OUTPUT_BYTES:
                        failure.append(ValueError("Git fact output exceeds its bound"))
                        process.kill()
                        return
                    output.extend(block)
            except OSError as exc:
                failure.append(exc)

        reader = threading.Thread(target=consume, daemon=True)
        reader.start()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=1)
            raise ValueError("Git fact query exceeded its timeout") from None
        finally:
            reader.join(timeout=1)
        if reader.is_alive() or failure or process.returncode != 0:
            raise ValueError("Git facts are unavailable")
    return bytes(output)


def _git_facts() -> dict[str, Any]:
    unknown = {"available": False, "commit": None, "dirty": None, "status_sha256": None,
               "status_entries": None}
    try:
        executable = shutil.which("git")
        if executable is None:
            return unknown
        commit = _git_output(executable, ["rev-parse", "--verify", "HEAD"]).decode("ascii").strip()
        if not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", commit):
            return unknown
        raw = _git_output(executable, ["status", "--porcelain=v1", "-z", "--untracked-files=normal",
                                      "--ignore-submodules=all"])
        pieces = raw.split(b"\0") if raw else [b""]
        if pieces[-1] != b"":
            return unknown
        entries = 0
        index = 0
        while index < len(pieces) - 1:
            item = pieces[index]
            if len(item) < 4 or item[2:3] != b" ":
                return unknown
            entries += 1
            index += 2 if b"R" in item[:2] or b"C" in item[:2] else 1
        if index != len(pieces) - 1:
            return unknown
        return {"available": True, "commit": commit, "dirty": entries > 0,
                "status_sha256": hashlib.sha256(raw).hexdigest(), "status_entries": entries}
    except (OSError, ValueError, UnicodeError, subprocess.SubprocessError):
        return unknown


# Import this module before the rest of the host. A later edit cannot be mistaken
# for the source whose Python code has already been imported into this process.
_SOURCE_BASELINE = _sha(_inventory())


def verify_host_baseline() -> None:
    if _sha(_inventory()) != _SOURCE_BASELINE:
        raise ValueError("host source changed since provenance import")


def capture_execution_provenance() -> dict[str, Any]:
    verify_host_baseline()
    inventory = _inventory()
    if _sha(inventory) != _SOURCE_BASELINE:
        raise ValueError("host source changed since provenance import")
    inputs = {path: _read_record(_ROOT / path) for path in _INPUTS}
    runtime = _runtime_facts()
    git = _git_facts()
    verify_host_baseline()
    if inputs != {path: _read_record(_ROOT / path) for path in _INPUTS} or runtime != _runtime_facts():
        raise ValueError("execution inputs changed while provenance was captured")
    return validate_execution_provenance(_seal({
        "schema_version": 1, "source_inventory": inventory,
        "repository_inputs": inputs, "runtime": runtime, "git": git,
    }))


def ensure_execution_compatible(saved: object) -> None:
    previous = validate_execution_provenance(saved)
    current = capture_execution_provenance()
    if previous["compatibility_sha256"] != current["compatibility_sha256"]:
        raise ValueError("execution provenance is incompatible with the current host")
