"""Project-scoped Python management entry, using the Windows Job API directly.

Only production competition, bounded setup, and finite developer checks are
handled here. Historical research/export scripts remain separate legacy tools.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .windows_job import (
    BoundedJob,
    ProjectMutex,
    current_job_limits,
    job_quiescent,
    lease_worker_identity,
    physical_memory,
    process_identity,
    terminate_identity,
)

_CATEGORIES = ("tools", "python", "env", "cache", "tmp", "work", "artifacts", "state")
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
_FORBIDDEN = {"data_b", "archive", ".git", ".agents", ".codex"}


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _io_path(path: Path) -> Path:
    """Use Win32 extended paths for I/O, preserving normal ownership names.

    This does not change the registry or follow links. Validation and saved
    identities keep their original absolute paths; only filesystem calls use
    the Windows namespace that can address deep project/cache directories.
    """
    if os.name != "nt":
        return path
    value = os.path.abspath(path)
    if value.startswith("\\\\?\\"):
        return Path(value)
    return Path("\\\\?\\UNC\\" + value[2:] if value.startswith("\\\\") else "\\\\?\\" + value)


def checked(path: Path, root: Path, *, exists: bool = False) -> Path:
    path = Path(os.path.abspath(path))
    root = Path(os.path.abspath(root))
    if path != root and not path.is_relative_to(root):
        raise ValueError("path is outside the project")
    if any(ord(char) < 32 or ord(char) == 127 for char in str(path)):
        raise ValueError("path contains controls")
    for part in (path, *path.parents):
        try:
            info = _io_path(part).lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError("linked management path is refused")
    if exists and not _io_path(path).exists():
        raise FileNotFoundError(path)
    return path


def _read(path: Path, root: Path, limit: int = 4 * 1024 * 1024) -> dict[str, Any]:
    checked(path, root, exists=True)
    actual = _io_path(path)
    if not actual.is_file() or actual.stat().st_size > limit:
        raise ValueError("invalid metadata size or type")
    value = json.loads(actual.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise TypeError("metadata must be an object")
    return value


def _write(path: Path, value: dict[str, Any], root: Path, *, replace: bool = True) -> None:
    checked(path, root)
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_name(path.name + "." + uuid.uuid4().hex + ".new")
    checked(pending, root)
    try:
        with pending.open("x", encoding="utf-8", newline="\n") as output:
            json.dump(value, output, ensure_ascii=True, separators=(",", ":"))
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        if replace:
            pending.replace(path)
        else:
            pending.rename(path)
    finally:
        pending.unlink(missing_ok=True)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest().upper()


def _uuid(value: Any) -> str:
    if not isinstance(value, str) or str(uuid.UUID(value)) != value:
        raise ValueError("invalid manager identity")
    return value


def _name(value: str) -> str:
    if not _NAME.fullmatch(value):
        raise ValueError("invalid session name")
    return value


def _input(root: Path, value: str, *, directory: bool = False, original_copy: bool = False) -> Path:
    parts = value.replace("\\", "/").split("/")
    forbidden = _FORBIDDEN - {"data_b"} if original_copy else _FORBIDDEN
    if not value or Path(value).is_absolute() or any(part in ("", ".", "..") or
            part.casefold() in forbidden or part != part.rstrip(" .") or ":" in part for part in parts):
        raise ValueError("input must be a canonical reviewed project-relative path")
    result = checked(root / value, root, exists=True)
    if _io_path(result).is_dir() != directory:
        raise ValueError("input type differs")
    if not directory and _io_path(result).stat().st_nlink != 1:
        raise ValueError("linked input file is refused")
    return result


class Runtime:
    def __init__(self, root: Path):
        self.repo = Path(os.path.abspath(root))
        checked(self.repo, self.repo, exists=True)
        self.root = checked(self.repo / ".dfx-local", self.repo)
        self.state = self.root / "state"
        self.active = self.state / "active.json"
        self.exports = self.repo / "runs/exports"
        self.policy = _read(self.repo / "configs/local-runtime.json", self.repo)
        policy = self.policy
        if (policy.get("schema_version") != 1 or policy.get("enforcement_status") != "offline_agent_guarded" or
                policy.get("paths") != {"managed_root": ".dfx-local", "export_root": "runs/exports"}):
            raise ValueError("unsupported management policy")
        execution, cleanup = policy["execution"], policy["cleanup"]
        if (execution.get("application_target") != "local" or execution.get("require_windows_job_limits") is not True or
                any(execution.get(key) is not False for key in ("allow_background_services", "allow_local_model_weights")) or
                any(cleanup.get(key) is not True for key in ("default_dry_run", "require_owner_match",
                    "refuse_external_reparse_points", "require_quiescent_root", "require_export_or_explicit_discard"))):
            raise ValueError("managed protections cannot be disabled")

    def mutex(self) -> ProjectMutex:
        name = hashlib.sha256(str(self.repo).upper().encode()).hexdigest().upper()[:24]
        return ProjectMutex("Local\\DFX-" + name)

    def owner(self) -> dict[str, Any]:
        owner = _read(self.root / "owner.json", self.repo, 16384)
        if (owner.get("schema_version") != 1 or owner.get("repo_real_path") != str(self.repo) or
                owner.get("managed_real_path") != str(self.root)):
            raise ValueError("managed owner differs")
        _uuid(owner.get("project_instance_id"))
        return owner

    def prepare(self) -> dict[str, Any]:
        with self.mutex():
            if self.root.exists():
                owner = self.owner()
            else:
                checked(self.state, self.repo).mkdir(parents=True)
                owner = {"schema_version": 1, "project_instance_id": str(uuid.uuid4()),
                         "repo_real_path": str(self.repo), "managed_real_path": str(self.root), "created_at": _now()}
                _write(self.root / "owner.json", owner, self.repo, replace=False)
            return {"project_instance_id": owner["project_instance_id"], "prepared": True, "runtime_installed": False}

    def inventory(self, target: Path, *, strict_links: bool = False) -> tuple[int, list[str]]:
        checked(target, self.repo)
        if not target.exists():
            return 0, []
        total, inaccessible = 0, []
        pending = [target]
        while pending:
            folder = pending.pop()
            try:
                entries = [folder / child.name for child in _io_path(folder).iterdir()]
            except FileNotFoundError:
                continue
            except PermissionError:
                inaccessible.append(str(folder))
                continue
            for entry in entries:
                try:
                    info = _io_path(entry).lstat()
                    if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                        if strict_links or not entry.resolve(strict=True).is_relative_to(self.root):
                            raise ValueError("unsafe linked inventory entry")
                        checked(entry.resolve(strict=True), self.repo, exists=True)
                    elif stat.S_ISDIR(info.st_mode):
                        pending.append(entry)
                    elif stat.S_ISREG(info.st_mode):
                        total += info.st_size
                    else:
                        raise ValueError("unsupported inventory entry")
                except FileNotFoundError:
                    continue
                except PermissionError:
                    inaccessible.append(str(entry))
        return total, inaccessible

    def accounting(self) -> tuple[dict[str, int], int, list[str]]:
        sizes, inaccessible = {}, []
        for name in _CATEGORIES:
            sizes[name], errors = self.inventory(self.root / name)
            inaccessible.extend(errors)
        exports, errors = self.inventory(self.exports, strict_links=True)
        inaccessible.extend(errors)
        return sizes, exports, sorted(set(inaccessible))

    def verified_manager(self, marker: dict[str, Any]) -> tuple[int, str] | None:
        if marker.get("schema_version") != 1 or marker.get("project_instance_id") != self.owner()["project_instance_id"]:
            raise ValueError("invalid active owner")
        _uuid(marker.get("run_id"))
        for key in ("manager_pid", "manager_start_utc_ticks", "child_pid", "child_start_utc_ticks"):
            if type(marker.get(key)) is not int or marker[key] < 1:
                raise ValueError("invalid active process identity")
        if marker.get("job_name") != "Local\\DFX-Guard-" + marker["run_id"]:
            raise ValueError("invalid active Job identity")
        identity = process_identity(marker["manager_pid"])
        if identity is not None and identity != (marker["manager_start_utc_ticks"], marker["manager_executable"]):
            raise ValueError("manager PID identity changed")
        return identity

    def status(self) -> dict[str, Any]:
        try:
            self.owner()
            owned = True
        except (OSError, ValueError):
            owned = False
        sizes, exports, inaccessible = self.accounting()
        active_state = "none"
        if self.active.exists():
            try:
                marker = _read(self.active, self.repo, 16384)
                active_state = "running" if self.verified_manager(marker) else "stale_requires_stop"
            except (OSError, ValueError, KeyError):
                active_state = "identity_unverified"
        remote = (self.state / "remote-jobs.json").exists()
        return {"managed_root": str(self.root), "owner_valid": owned, "managed_bytes_by_category": sizes,
                "export_bytes": exports, "resource_accounting": "incomplete" if inaccessible else "complete",
                "inaccessible_paths": inaccessible, "active_marker": self.active.exists(), "active_state": active_state,
                "remote_jobs_unknown": remote, "resource_enforcement": "native_windows_job",
                "cleanup_available": owned and active_state == "none" and not inaccessible and not remote}

    def limits(self, timeout: int) -> dict[str, int]:
        values = self.policy["limits"]
        bounds = {"max_concurrent_runs": (1, 1), "max_active_processes": (1, 1024), "cpu_rate_percent": (1, 100),
                  "job_memory_cap_bytes": (16777216, 2**63-1), "monitor_interval_seconds": (1, 30),
                  "default_wall_timeout_seconds": (1, 1800), "stop_grace_seconds": (1, 60),
                  "max_log_file_bytes": (1024, 2**63-1), "max_run_log_bytes": (1024, 2**63-1),
                  "min_free_memory_bytes": (1, 2**63-1), "min_free_disk_bytes": (1, 2**63-1),
                  "managed_disk_budget_bytes": (1, 2**63-1)}
        for key, (lower, upper) in bounds.items():
            if type(values.get(key)) is not int or not lower <= values[key] <= upper:
                raise ValueError("invalid resource policy")
        for key in ("job_memory_fraction_of_physical", "min_free_disk_fraction"):
            if type(values.get(key)) not in (int, float) or not 0 < values[key] <= 1:
                raise ValueError("invalid resource fraction")
        if values["max_run_log_bytes"] < values["max_log_file_bytes"]:
            raise ValueError("invalid log policy")
        physical, free = physical_memory()
        memory = min(values["job_memory_cap_bytes"], int(physical * values["job_memory_fraction_of_physical"]))
        if memory < 16777216 or free < memory + values["min_free_memory_bytes"]:
            raise ValueError("insufficient physical memory reserve")
        disk = shutil.disk_usage(self.root)
        reserve = max(values["min_free_disk_bytes"], int(disk.total * values["min_free_disk_fraction"] + 0.999999))
        sizes, exports, inaccessible = self.accounting()
        if inaccessible:
            raise ValueError("managed resource accounting is incomplete")
        if disk.free < reserve or sum(sizes.values()) + exports > values["managed_disk_budget_bytes"]:
            raise ValueError("managed disk limit or reserve exceeded")
        return {"cpu_percent": values["cpu_rate_percent"], "max_processes": values["max_active_processes"],
                "memory_bytes": memory, "disk_reserve_bytes": reserve, "disk_budget_bytes": values["managed_disk_budget_bytes"],
                "min_free_memory_bytes": values["min_free_memory_bytes"], "max_log_file_bytes": values["max_log_file_bytes"],
                "max_run_log_bytes": values["max_run_log_bytes"],
                "monitor_seconds": values["monitor_interval_seconds"], "timeout_seconds": min(timeout, values["default_wall_timeout_seconds"])}

    def capture_bytes(self, log: Path, work: Path | None) -> int:
        total = log.stat().st_size if log.exists() else 0
        if work is not None:
            execution = checked(work / "local-execution", self.repo)
            if execution.is_dir():
                for receipt in execution.iterdir():
                    checked(receipt, self.repo)
                    if receipt.is_dir() and re.fullmatch(r"[a-f0-9]{64}", receipt.name):
                        for path in receipt.iterdir():
                            checked(path, self.repo)
                            if path.is_file() and path.name.endswith((".stdout.txt", ".stderr.txt")):
                                total += path.stat().st_size
        return total

    def environment_python(self) -> Path:
        path = checked(self.root / "env/Scripts/python.exe", self.repo, exists=True)
        metadata = _read(self.state / "environment.json", self.repo, 16384)
        if (metadata.get("environment_python") != str(path) or
                metadata.get("lock_sha256") != _sha(checked(self.repo / "uv.lock", self.repo, exists=True)) or
                metadata.get("pyproject_sha256") != _sha(checked(self.repo / "pyproject.toml", self.repo, exists=True))):
            raise ValueError("managed environment no longer matches its lock or path")
        return path

    def stopped(self, run_id: str) -> bool:
        for path in (self.state / ("stop-" + run_id + ".json"), self.state / "stop-request.json"):
            if path.exists():
                request = _read(path, self.repo, 16384)
                if request.get("run_id") != run_id or request.get("schema_version") != 1:
                    raise ValueError("stop request belongs to another run")
                return True
        return False

    def stop(self, expected: str | None) -> dict[str, Any]:
        if expected is not None:
            _uuid(expected)
        if not self.active.exists():
            return {"stopped": True, "active_run": False, "remote_jobs_unknown": (self.state / "remote-jobs.json").exists()}
        marker = _read(self.active, self.repo, 16384)
        if expected and marker.get("run_id") != expected:
            raise ValueError("active manager changed; stop refused")
        identity = self.verified_manager(marker)
        request = self.state / ("stop-" + marker["run_id"] + ".json")
        if identity:
            _write(request, {"schema_version": 1, "run_id": marker["run_id"], "requested_at": _now()}, self.repo)
            deadline = time.monotonic() + self.policy["limits"]["stop_grace_seconds"]
            while time.monotonic() < deadline and self.verified_manager(marker):
                time.sleep(0.1)
            if self.verified_manager(marker):
                terminate_identity(marker["manager_pid"], *identity)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not job_quiescent(marker["job_name"]):
            time.sleep(0.1)
        child = process_identity(marker["child_pid"])
        if (child and child[0] == marker["child_start_utc_ticks"]) or not job_quiescent(marker["job_name"]):
            raise ValueError("managed process tree stop could not be confirmed")
        # Serialize removal with the same Run/cleanup mutex; an unrelated new
        # lease must never be erased by a delayed stop.
        with self.mutex():
            if self.active.exists():
                current = _read(self.active, self.repo, 16384)
                if current.get("run_id") != marker["run_id"]:
                    raise ValueError("active manager changed after stop")
                self.active.unlink()
            request.unlink(missing_ok=True)
        return {"stopped": True, "active_run": False, "run_id": marker["run_id"],
                "remote_jobs_unknown": (self.state / "remote-jobs.json").exists()}

    def run(self, args: argparse.Namespace) -> tuple[int, dict[str, Any]]:
        with self.mutex():
            owner = self.owner()
            if self.active.exists() or (self.state / "remote-jobs.json").exists():
                raise ValueError("unresolved activity blocks execution")
            for folder in (self.root / "tmp", self.root / "artifacts"):
                checked(folder, self.repo).mkdir(parents=True, exist_ok=True)
            task = "bootstrap" if args.action == "Init" else args.Task
            if task not in {"bootstrap", "competition", "case-export", "provider", "prepare", "test", "docs", "ruff", "pyright"}:
                raise ValueError("unsupported native task")
            effective = self.limits(args.TimeoutSeconds)
            run_id = str(uuid.uuid4())
            name = _name(args.CompetitionWorkId) if task in {"competition", "case-export"} else None
            work = None
            if task == "competition":
                assert name is not None
                work = checked(self.root / "work" / ("competition-" + name), self.repo)
            python = Path(sys.executable) if task == "bootstrap" else self.environment_python()
            env = os.environ.copy()
            for variable in ("VIRTUAL_ENV", "CONDA_PREFIX", "PYTHONSTARTUP"):
                env.pop(variable, None)
            env.update({"TEMP": str(self.root / "tmp"), "TMP": str(self.root / "tmp"), "PYTHONDONTWRITEBYTECODE": "1",
                        "PYTHONPATH": str(self.repo / "src"), "PYTHONPYCACHEPREFIX": str(self.root / "cache/pycache"),
                        "PYRIGHT_PYTHON_CACHE_DIR": str(self.root / "cache/pyright"),
                        "PYRIGHT_PYTHON_ENV_DIR": str(self.root / "tools/pyright-node"), "RUFF_CACHE_DIR": str(self.root / "cache/ruff")})
            argv = [str(python), "-m", "dfx_agent.managed_runtime", "--workspace", str(self.repo), "Worker", "-Task", task]
            if task == "bootstrap":
                if args.OfflineSync:
                    argv += ["-OfflineSync"]
            elif task == "competition":
                assert name is not None
                config = _input(self.repo, args.CompetitionConfigPath)
                profile = _read(config, self.repo)
                if profile.get("model_mode") == "mock_demo":
                    if args.CompetitionTaskPath:
                        raise ValueError("demo must not receive private task input")
                    source = self.root / "tmp" / ("competition-" + name + "-demo-task")
                else:
                    source = _input(self.repo, args.CompetitionTaskPath, directory=True)
                assert work is not None
                if work.exists() != args.Resume:
                    raise ValueError("new/resume work existence differs")
                argv += ["-CompetitionConfigPath", str(config), "-CompetitionTaskPath", str(source), "-CompetitionWorkId", name]
                if args.Resume:
                    argv += ["-Resume"]
            elif task == "case-export":
                assert name is not None
                checked(self.root / "work" / ("competition-" + name), self.repo, exists=True)
                argv += ["-CompetitionWorkId", name]
                if args.ExportProjectPath:
                    project = _input(self.repo, args.ExportProjectPath, directory=True, original_copy=True)
                    argv += ["-ExportProjectPath", str(project)]
            elif task == "prepare":
                config = _input(self.repo, args.PreparationConfigPath)
                if config.parent != self.state:
                    raise ValueError("preparation config must be inside the managed state directory")
                argv += ["-PreparationConfigPath", str(config)]
            elif task == "test":
                test = _input(self.repo, args.TestPath)
                if not test.is_relative_to(self.repo / "tests") or not re.fullmatch(r"test_[A-Za-z0-9_]+\.py", test.name):
                    raise ValueError("test must name one project test file")
                argv += ["-TestPath", str(test)]
            job = None
            reason, status = "before_launch", "failed"
            child_pid, child_ticks = 0, 0
            worker_exit = None
            log = self.root / "artifacts" / (run_id + ".log")
            record = self.root / "artifacts" / (run_id + ".json")
            marker_created = False
            try:
                job = BoundedJob("Local\\DFX-Guard-" + run_id, effective["cpu_percent"], effective["memory_bytes"], effective["max_processes"])
                job.start_suspended(argv, self.repo, env, log, effective["max_log_file_bytes"])
                child_pid = job.pid
                child = process_identity(child_pid)
                manager = process_identity(os.getpid())
                if not child or not manager:
                    raise ValueError("managed process identity unavailable")
                child_ticks = child[0]
                marker = {"schema_version": 1, "run_id": run_id, "project_instance_id": owner["project_instance_id"],
                          "manager_pid": os.getpid(), "manager_start_utc_ticks": manager[0], "manager_executable": manager[1],
                          "child_pid": child_pid, "child_start_utc_ticks": child_ticks, "task": task,
                          "job_name": "Local\\DFX-Guard-" + run_id, "effective_limits": effective, "started_at": _now(),
                          "competition_work_id": name, "manager_backend": "python_win32"}
                _write(self.active, marker, self.repo, replace=False)
                marker_created = True
                if self.stopped(run_id):
                    reason, status = "stop_requested", "canceled"
                    job.terminate()
                else:
                    job.resume()
                    reason = "running"
                started = time.monotonic()
                while not job.exited():
                    if self.stopped(run_id):
                        reason, status = "stop_requested", "canceled"
                    elif job.log_exceeded:
                        reason = "log_limit"
                    elif job.output_error:
                        reason = "output_capture_failed"
                    elif self.capture_bytes(log, work) > effective["max_run_log_bytes"]:
                        reason = "run_log_limit"
                    elif time.monotonic() - started >= effective["timeout_seconds"]:
                        reason, status = "timeout", "canceled"
                    elif physical_memory()[1] < effective["min_free_memory_bytes"]:
                        reason = "memory_reserve"
                    else:
                        sizes, exports, inaccessible = self.accounting()
                        if inaccessible or shutil.disk_usage(self.root).free < effective["disk_reserve_bytes"] or sum(sizes.values()) + exports > effective["disk_budget_bytes"]:
                            reason = "disk_limit_or_incomplete_accounting"
                    if reason != "running":
                        job.terminate()
                        break
                    time.sleep(effective["monitor_seconds"])
                if reason == "running":
                    worker_exit = job.exit_code()
                    status = "finished" if worker_exit == 0 else "failed"
                    reason = "exited_zero" if worker_exit == 0 else "child_exit_nonzero"
            except (OSError, ValueError, KeyError, TypeError):
                reason, status = "managed_execution_rejected", "failed"
                if job:
                    job.terminate()
            finally:
                if job:
                    job.close()
                    if job.log_exceeded:
                        reason, status = "log_limit", "failed"
                    elif job.output_error:
                        reason, status = "output_capture_failed", "failed"
                    elif self.capture_bytes(log, work) > effective["max_run_log_bytes"]:
                        reason, status = "run_log_limit", "failed"
                receipt = {"schema_version": 1, "run_id": run_id, "task": task, "competition_work_id": name,
                           "status": status, "reason": reason, "child_pid": child_pid, "log_path": str(log),
                           "log_truncated": bool(job and job.log_exceeded), "finished_at": _now(), "manager_backend": "python_win32"}
                _write(record, receipt, self.repo)
                remaining = process_identity(child_pid) if child_pid else None
                quiescent = remaining is None or remaining[0] != child_ticks
                if marker_created and quiescent and job_quiescent("Local\\DFX-Guard-" + run_id):
                    current = _read(self.active, self.repo, 16384)
                    if current.get("run_id") != run_id:
                        raise ValueError("active lease changed during execution")
                    self.active.unlink()
                    for path in (self.state / ("stop-" + run_id + ".json"), self.state / "stop-request.json"):
                        if path.exists() and _read(path, self.repo, 16384).get("run_id") == run_id:
                            path.unlink()
                if work and work.is_dir() and not (args.Resume and worker_exit == 3):
                    _write(work / "run-state.json", {"schema_version": 1, "status": status, "task": "competition",
                            "run_id": name, "finished_at": _now()}, self.repo)
            return (0 if status == "finished" else 2), receipt

    def targets(self, scope: str) -> list[Path]:
        children = {"cache": ("cache", "tmp"), "testcache": ("cache/pytest", "cache/pycache"),
                    "tmp": ("tmp",), "work": ("work",), "env": ("tools", "python", "env"), "all": (".",)}[scope]
        return [path for child in children if (path := checked(self.root / child, self.repo)).exists()]

    def quiescent(self, scope: str, discard: bool) -> None:
        if self.active.exists() or (self.state / "remote-jobs.json").exists():
            raise ValueError("unresolved activity blocks cleanup")
        if scope in {"work", "all"} and (self.root / "work").exists():
            for work in (self.root / "work").iterdir():
                checked(work, self.repo)
                state = _read(work / "run-state.json", self.repo, 16384)
                if state.get("status") not in {"finished", "failed", "canceled"}:
                    raise ValueError("nonterminal work blocks cleanup")
            if not discard and any((self.root / "work").iterdir()):
                raise ValueError("work evidence requires explicit discard after review/export")
        if not discard and scope in {"cache", "tmp", "all"}:
            roots = [self.root / "tmp"]
            if scope in {"cache", "all"}:
                roots.append(self.root / "cache")
            for temporary in roots:
                actual_root = _io_path(temporary)
                if actual_root.exists():
                    for actual in actual_root.rglob("*"):
                        entry = temporary / actual.relative_to(actual_root)
                        checked(entry, self.repo)
                        if entry.name in {"task.json", "source-manifest.json", "package.json"} or actual.is_dir() and entry.name.startswith("ex-"):
                            raise ValueError("temporary evidence requires explicit discard")
        if scope == "all" and not discard:
            artifacts = self.root / "artifacts"
            if artifacts.exists() and any(artifacts.iterdir()):
                raise ValueError("artifacts require explicit discard")
            if any(entry.name != "cleanup-plan.json" for entry in self.state.iterdir()):
                raise ValueError("state evidence requires explicit discard")

    def fingerprint(self, targets: list[Path]) -> tuple[str, int]:
        records, total = [], 0
        for target in targets:
            pending = [target]
            while pending:
                entry = pending.pop()
                checked(entry, self.repo, exists=True)
                if entry == self.state / "cleanup-plan.json":
                    continue
                actual = _io_path(entry)
                info = actual.stat()
                if actual.is_dir():
                    records.append((str(entry.relative_to(self.root)), True, 0, 0))
                    pending.extend(entry / child.name for child in actual.iterdir())
                elif actual.is_file():
                    records.append((str(entry.relative_to(self.root)), False, info.st_size, info.st_mtime_ns))
                    total += info.st_size
                else:
                    raise ValueError("unsupported cleanup member")
        raw = json.dumps(sorted(records), ensure_ascii=True, separators=(",", ":")).encode()
        return hashlib.sha256(raw).hexdigest(), total

    def clean(self, args: argparse.Namespace) -> dict[str, Any]:
        with self.mutex():
            owner = self.owner()
            self.quiescent(args.Scope, args.DiscardEvidence)
            targets = self.targets(args.Scope)
            # A running owned interpreter cannot be deleted on Windows. Use
            # the independently installed setup interpreter for env/all.
            if args.Apply and any(Path(sys.executable).is_relative_to(target) for target in targets):
                raise ValueError("cleanup needs the external setup Python interpreter")
            fingerprint, size = self.fingerprint(targets)
            path = self.state / "cleanup-plan.json"
            if not args.Apply:
                plan = {"schema_version": 1, "plan_id": str(uuid.uuid4()), "project_instance_id": owner["project_instance_id"],
                        "scope": args.Scope, "discard_evidence": args.DiscardEvidence, "fingerprint": fingerprint,
                        "estimated_bytes": size, "target_count": len(targets), "created_at": _now(), "manager_backend": "python_win32"}
                _write(path, plan, self.repo)
                return {"plan_id": plan["plan_id"], "scope": args.Scope, "estimated_bytes": size, "target_count": len(targets),
                        "dry_run": True, "apply_requires_plan_id": True, "preserved": ["data_B", "archive", "runs/exports", "tracked files"]}
            plan = _read(path, self.repo, 16384)
            age = (datetime.now(UTC) - datetime.fromisoformat(plan["created_at"])).total_seconds()
            if (not 0 <= age <= 600 or plan.get("schema_version") != 1 or plan.get("plan_id") != args.PlanId or
                    plan.get("project_instance_id") != owner["project_instance_id"] or plan.get("scope") != args.Scope or
                    plan.get("discard_evidence") != args.DiscardEvidence or plan.get("fingerprint") != fingerprint or
                    plan.get("estimated_bytes") != size or plan.get("target_count") != len(targets) or
                    plan.get("manager_backend") != "python_win32"):
                raise ValueError("cleanup plan changed or expired")
            self.quiescent(args.Scope, args.DiscardEvidence)
            removed = []
            for target in targets:
                checked(target, self.repo, exists=True)
                # Recheck every member immediately before deleting; no link
                # following, shell interpolation, or cross-root deletion.
                self.fingerprint([target])
                shutil.rmtree(_io_path(target))
                removed.append(str(target))
            receipt = {"plan_id": args.PlanId, "applied": True, "removed": removed,
                       "preserved": ["data_B", "archive", "runs/exports", "tracked files"]}
            if args.Scope == "all":
                _write(self.exports / ("cleanup-" + args.PlanId + ".json"), {**receipt, "finished_at": _now()}, self.repo, replace=False)
            else:
                path.unlink(missing_ok=True)
            return receipt


def require_managed_worker(root: Path, task: str) -> dict[str, Any]:
    runtime = Runtime(root)
    marker = _read(runtime.active, runtime.repo, 16384)
    if runtime.verified_manager(marker) is None:
        raise ValueError("worker manager is no longer alive")
    if marker.get("task") != task or not lease_worker_identity(marker):
        raise ValueError("worker does not match its exact managed lease")
    actual = current_job_limits(marker["job_name"])
    if any(marker.get("effective_limits", {}).get(key) != value for key, value in actual.items()):
        raise ValueError("worker Job limits differ from the lease")
    return marker


def _worker(args: argparse.Namespace, root: Path) -> int:
    try:
        require_managed_worker(root, args.Task)
    except (OSError, ValueError, KeyError, TypeError) as error:
        # This pipe is captured only in the owned private manager log. The
        # product frontend receives the bounded generic manager receipt.
        print("managed worker admission: " + type(error).__name__ + ": " + str(error), file=sys.stderr, flush=True)
        raise
    if args.Task == "bootstrap":
        from .managed_bootstrap import bootstrap
        bootstrap(root, offline=args.OfflineSync)
        return 0
    if args.Task == "provider":
        from .provider_probe import run_probe

        print(json.dumps(run_probe(root), ensure_ascii=True), flush=True)
        return 0
    if args.Task == "prepare":
        from .pilot_preparation import run_preparation

        print(json.dumps(run_preparation(root, Path(args.PreparationConfigPath)), ensure_ascii=True), flush=True)
        return 0
    if args.Task == "case-export":
        from .case_results import export_case_result

        project = Path(args.ExportProjectPath) if args.ExportProjectPath else None
        try:
            exported = export_case_result(root, args.CompetitionWorkId, project=project)
        except (OSError, ValueError, TypeError, KeyError) as error:
            # This is the private managed artifact log, not the public CLI.
            # Keep the concrete local failure so saved repairs can be exported
            # without repeating a paid model run.
            print("case export: " + type(error).__name__ + ": " + str(error), file=sys.stderr, flush=True)
            raise
        print(json.dumps(exported, ensure_ascii=True), flush=True)
        return 0
    if args.Task == "competition":
        from .cli import main
        os.environ["DFX_MANAGED_COMPETITION"] = "local-v1"
        work = root / ".dfx-local/work" / ("competition-" + _name(args.CompetitionWorkId))
        argv = ["competition", "--task", args.CompetitionTaskPath, "--work", str(work), "--run-id", args.CompetitionWorkId,
                "--config", args.CompetitionConfigPath, "--project-root", str(root)]
        if args.Resume:
            argv += ["--resume"]
        sys.argv = ["dfx-internal", *argv]
        main()
        return 0
    if args.Task == "test":
        import subprocess
        os.environ["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
        os.environ["PYTEST_ADDOPTS"] = ""
        test = checked(Path(args.TestPath), root, exists=True)
        if not test.is_relative_to(root / "tests") or not re.fullmatch(r"test_[A-Za-z0-9_]+\.py", test.name):
            raise ValueError("test input differs")
        temporary = root / ".dfx-local/tmp" / ("pytest-" + uuid.uuid4().hex)
        temporary.mkdir()
        try:
            # Let the mature pytest process own its imports, SQLite handles
            # and fixtures. Its exit releases those handles before this
            # worker removes only the temporary tree it just created. The
            # child remains in the same bounded Windows Job.
            return subprocess.run([sys.executable, "-m", "pytest", str(test), "-c",
                str(root / "pyproject.toml"), "--basetemp", str(temporary), "-p",
                "no:cacheprovider", "-q"], cwd=root, check=False, shell=False).returncode
        finally:
            runtime = Runtime(root)
            runtime.fingerprint([temporary])
            shutil.rmtree(_io_path(temporary))
    if args.Task == "docs":
        import runpy
        runpy.run_path(str(root / "scripts/check_docs.py"), run_name="__main__")
        return 0
    if args.Task == "ruff":
        import subprocess

        from ruff import find_ruff_bin
        return subprocess.call([find_ruff_bin(), "check", "--no-cache", "src", "tests", "scripts/check_docs.py", "scripts/dfx.py"])
    if args.Task == "pyright":
        import pyright
        return pyright.run("--pythonpath", sys.executable, "src/dfx_agent", "tests/test_sdk_compat.py").returncode
    raise ValueError("unknown worker task")


def parser() -> argparse.ArgumentParser:
    cli = argparse.ArgumentParser(prog="dfx-managed")
    cli.add_argument("--workspace", type=Path, required=True)
    cli.add_argument("action", choices=("Prepare", "Status", "Stop", "Clean", "Init", "Run", "Worker"))
    cli.add_argument("-Task", choices=("competition", "case-export", "provider", "prepare", "test", "docs", "ruff", "pyright", "bootstrap"), default="competition")
    for name in ("CompetitionTaskPath", "CompetitionConfigPath", "CompetitionWorkId", "PreparationConfigPath", "ExportProjectPath", "TestPath", "ExpectedRunId", "PlanId"):
        cli.add_argument("-" + name, default="")
    cli.add_argument("-Scope", choices=("cache", "testcache", "tmp", "work", "env", "all"), default="cache")
    for name in ("Resume", "OfflineSync", "Apply", "DiscardEvidence"):
        cli.add_argument("-" + name, action="store_true")
    cli.add_argument("-TimeoutSeconds", type=int, default=1800)
    return cli


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if not 1 <= args.TimeoutSeconds <= 1800:
            raise ValueError("invalid task timeout")
        runtime = Runtime(args.workspace)
        if args.action == "Worker":
            return _worker(args, runtime.repo)
        if args.action == "Prepare":
            result, code = runtime.prepare(), 0
        elif args.action == "Status":
            result, code = runtime.status(), 0
        elif args.action == "Stop":
            result, code = runtime.stop(args.ExpectedRunId or None), 0
        elif args.action == "Clean":
            result, code = runtime.clean(args), 0
        else:
            code, result = runtime.run(args)
        print(json.dumps(result, ensure_ascii=True, separators=(",", ":")), flush=True)
        return code
    except (OSError, ValueError, TypeError, KeyError) as error:
        # Provider keys, source paths and exception bodies remain private.
        if args.action == "Worker" and args.Task == "bootstrap":
            print("managed bootstrap: " + type(error).__name__ + ": " + str(error), file=sys.stderr, flush=True)
        print('{"status":"failed","reason":"managed_command_rejected","manager_backend":"python_win32"}', flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
