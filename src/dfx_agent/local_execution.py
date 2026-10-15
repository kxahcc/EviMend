"""Host-fixed source replay and application checks in the existing managed Job.

The synthetic mode exercises the same copy/overlay/process/receipt path, but
never produces source or application success. Source replay declares its local
coverage and mocks without claiming full application behavior. This is a process boundary, not an OS
filesystem sandbox: commands and their independent oracles belong to the host.
"""

from __future__ import annotations

import ctypes
import json
import os
import shutil
import stat
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, BinaryIO, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .contracts import require_hash, require_path
from .evidence_io import local_evidence_path
from .progress import emit_progress
from .task import digest, snapshot_digest
from .verification import VerificationLevel, _atomic_json
from .workspace import CandidateWorkspace, _manifest

STAGES = ("build", "target", "behavior", "regression")
CheckStatus = Literal["pass", "fail", "inconclusive"]


def application_check_state(record: dict[str, Any]) -> str:
    """Derive execution scope/result without depending on solver acceptance.

    Legacy stage receipts lack process_started. Their exited/timeout evidence
    can prove an attempt; ambiguous launches remain inconclusive, not not_run.
    """
    return execution_check_state(record, "application")


def execution_check_state(record: dict[str, Any], mode: Literal["source", "application"]) -> str:
    """Derive an actual attempt for one scope without upgrading another scope."""
    if record.get("mode") != mode:
        return "not_run"
    if record.get("state") == "pending":
        return "inconclusive"
    stages = [*(record.get("baseline") or {}).get("stages", []), *record.get("stages", [])]
    attempted = any(stage.get("process_started") is True or stage.get("exit_code") is not None or
                    stage.get("stdout") is not None or stage.get("stderr") is not None or
                    (stage.get("process_started") is not False and stage.get("reason") == "stop_requested") or
                    stage.get("reason") not in {"before_launch", "check_timeout", "stop_requested"} for stage in stages)
    if not attempted:
        return "not_run"
    return record["status"]


class LocalCommand(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    argv: list[str] = Field(min_length=1, max_length=128)
    timeout_seconds: int = Field(default=30, ge=1, le=1800)
    oracle_sha256: str | None = None

    @model_validator(mode="after")
    def valid_command(self) -> LocalCommand:
        if any(not item or "\x00" in item or len(item) > 32768 for item in self.argv):
            raise ValueError("invalid command argument")
        if self.oracle_sha256 is not None:
            require_hash(self.oracle_sha256)
        return self


class IndependentOracle(BaseModel):
    """Host-owned acceptance file, frozen into the execution protocol."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    path: str = Field(min_length=1, max_length=32768)
    sha256: str
    max_bytes: int = Field(default=1048576, ge=1, le=16777216)

    @model_validator(mode="after")
    def valid_hash(self) -> IndependentOracle:
        require_hash(self.sha256)
        return self


class LocalCheckConfig(BaseModel):
    """Trusted configuration fixed before any candidate is inspected.

    Empty source_mapping means the complete analysis tree uses identical
    relative paths in the full project. An explicit mapping must cover exactly
    the analysis baseline; additions then need a separately prepared protocol.
    """

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    schema_version: int = Field(default=1, ge=1, le=1)
    mode: Literal["synthetic", "source", "application"] = "synthetic"
    validation_scope: str | None = Field(default=None, min_length=1, max_length=8192,
                                         exclude_if=lambda value: value is None)
    mocked_interfaces: list[str] | None = Field(default=None, max_length=128,
                                              exclude_if=lambda value: value is None)
    project_root: str
    source_mapping: dict[str, str] = Field(default_factory=dict)
    commands: dict[str, LocalCommand]
    # A host-approved dependency command runs in each independent project copy.
    # Omitting it preserves serialized identities of existing check protocols.
    preparation: LocalCommand | None = Field(default=None, exclude_if=lambda value: value is None)
    independent_oracles: dict[str, IndependentOracle] | None = Field(
        default=None, exclude_if=lambda value: value is None)
    baseline_target_exit_codes: list[int] = Field(min_length=1)
    baseline_fault_pattern: str | None = None
    max_project_bytes: int = Field(default=268435456, ge=1)
    max_output_bytes: int = Field(default=1048576, ge=1)
    max_check_seconds: int = Field(default=120, ge=1, le=1800)
    credential_env_names: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def valid_protocol(self) -> LocalCheckConfig:
        if set(self.commands) != set(STAGES):
            raise ValueError("exactly build, target, behavior and regression commands are required")
        if len(set(self.baseline_target_exit_codes)) != len(self.baseline_target_exit_codes) or any(
                code <= 0 or code > 255 for code in self.baseline_target_exit_codes):
            raise ValueError("baseline target requires explicit nonzero exit codes")
        targets: set[str] = set()
        for source, target in self.source_mapping.items():
            require_path(source)
            require_path(target)
            if target.casefold() in targets:
                raise ValueError("source mapping has duplicate project destinations")
            targets.add(target.casefold())
        if self.baseline_fault_pattern is not None and (not self.baseline_fault_pattern or
                                                       len(self.baseline_fault_pattern) > 4096):
            raise ValueError("invalid baseline fault marker")
        for name in self.credential_env_names:
            if not name or not name.replace("_", "a").isalnum() or "=" in name:
                raise ValueError("invalid credential environment name")
        if self.mode in ("source", "application") and (not self.baseline_fault_pattern or
                any(command.oracle_sha256 is None for command in self.commands.values())):
            raise ValueError("source/application checks require a fixed fault marker and independent oracle hashes")
        if self.mode == "source" and self.independent_oracles is None:
            raise ValueError("source checks require independent oracle file bindings")
        if self.mode == "source" and (not self.validation_scope or not self.validation_scope.strip() or
                                      self.mocked_interfaces is None):
            raise ValueError("source checks require validation scope and an explicit mocked interface list")
        if self.mocked_interfaces is not None and (len(set(self.mocked_interfaces)) != len(self.mocked_interfaces) or
                any(not item.strip() or len(item) > 1024 or "\x00" in item for item in self.mocked_interfaces)):
            raise ValueError("invalid mocked interface declaration")
        if self.independent_oracles is not None:
            if set(self.independent_oracles) != set(STAGES):
                raise ValueError("independent oracle bindings are required for all four stages")
            if any(self.commands[stage].oracle_sha256 != oracle.sha256
                   for stage, oracle in self.independent_oracles.items()):
                raise ValueError("independent oracle bindings and command hashes disagree")
        return self


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _checked(path: Path, *, exists: bool = True) -> Path:
    """Check every lexical component before resolving, including junctions."""
    absolute = path.absolute()
    if ".." in absolute.parts:
        raise ValueError("noncanonical local execution path")
    for component in (*reversed(absolute.parents), absolute):
        try:
            info = component.lstat()
        except FileNotFoundError:
            if exists:
                raise ValueError("local execution path is missing") from None
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & getattr(
                stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0):
            raise ValueError("local execution path contains a link")
    return absolute.resolve(strict=exists)


def _inventory(root: Path, cap: int) -> dict[str, Any]:
    root = _checked(root)
    if not root.is_dir():
        raise ValueError("full project root is not a directory")
    files: list[dict[str, Any]] = []
    directories: list[str] = []
    names: set[str] = set()
    total = 0
    pending = [root]
    while pending:
        folder = pending.pop()
        with os.scandir(folder) as entries:
            for entry in entries:
                path = Path(entry.path)
                info = entry.stat(follow_symlinks=False)
                relative = path.relative_to(root).as_posix()
                require_path(relative)
                if relative.casefold() in names:
                    raise ValueError("full project has case-colliding paths")
                names.add(relative.casefold())
                if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & getattr(
                        stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0):
                    raise ValueError("full project contains a link")
                if stat.S_ISDIR(info.st_mode):
                    directories.append(relative)
                    pending.append(path)
                elif stat.S_ISREG(info.st_mode):
                    total += info.st_size
                    if total > cap:
                        raise ValueError("full project exceeds copy byte limit")
                    content = path.read_bytes()
                    if len(content) != info.st_size:
                        raise ValueError("full project changed during inventory")
                    files.append({"path": relative, "sha256": digest(content), "size_bytes": len(content)})
                else:
                    raise ValueError("full project contains an unsupported entry")
    return {"files": sorted(files, key=lambda item: item["path"]),
            "directories": sorted(directories), "size_bytes": total}


def _copy_project(source: Path, destination: Path, manifest: dict[str, Any], cap: int) -> None:
    _checked(destination, exists=False)
    if destination.exists():
        raise ValueError("local project copy already exists")
    destination.mkdir()
    for directory in manifest["directories"]:
        (destination / directory).mkdir(parents=True, exist_ok=True)
    for item in manifest["files"]:
        old, new = source / item["path"], destination / item["path"]
        _checked(old)
        new.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(old, new)
        if os.path.samefile(old, new):
            raise ValueError("local project copy shares a writable inode")
    if _inventory(source, cap) != manifest or _inventory(destination, cap) != manifest:
        raise ValueError("full project changed while making an independent copy")


class _BasicLimits(ctypes.Structure):
    _fields_ = [("ProcessTime", ctypes.c_longlong), ("JobTime", ctypes.c_longlong),
                ("Flags", ctypes.c_ulong), ("MinWorkingSet", ctypes.c_size_t),
                ("MaxWorkingSet", ctypes.c_size_t), ("ActiveProcessLimit", ctypes.c_ulong),
                ("Affinity", ctypes.c_size_t), ("PriorityClass", ctypes.c_ulong),
                ("SchedulingClass", ctypes.c_ulong)]


class _IoCounters(ctypes.Structure):
    _fields_ = [(name, ctypes.c_ulonglong) for name in
                ("ReadOperations", "WriteOperations", "OtherOperations", "ReadBytes", "WriteBytes", "OtherBytes")]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [("Basic", _BasicLimits), ("Io", _IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemory", ctypes.c_size_t), ("PeakJobMemory", ctypes.c_size_t)]


class _CpuLimits(ctypes.Structure):
    _fields_ = [("Flags", ctypes.c_ulong), ("CpuRate", ctypes.c_ulong)]


class _ThreadEntry(ctypes.Structure):
    _fields_ = [("Size", ctypes.c_ulong), ("Usage", ctypes.c_ulong),
                ("ThreadId", ctypes.c_ulong), ("ProcessId", ctypes.c_ulong),
                ("BasePriority", ctypes.c_long), ("DeltaPriority", ctypes.c_long), ("Flags", ctypes.c_ulong)]


class _ProcessEntry(ctypes.Structure):
    _fields_ = [("Size", ctypes.c_ulong), ("Usage", ctypes.c_ulong), ("ProcessId", ctypes.c_ulong),
                ("HeapId", ctypes.c_size_t), ("ModuleId", ctypes.c_ulong), ("Threads", ctypes.c_ulong),
                ("ParentId", ctypes.c_ulong), ("Priority", ctypes.c_long), ("Flags", ctypes.c_ulong),
                ("Executable", ctypes.c_wchar * 260)]


class _Accounting(ctypes.Structure):
    _fields_ = [("UserTime", ctypes.c_longlong), ("KernelTime", ctypes.c_longlong),
                ("PeriodUserTime", ctypes.c_longlong), ("PeriodKernelTime", ctypes.c_longlong),
                ("PageFaults", ctypes.c_ulong), ("Processes", ctypes.c_ulong),
                ("ActiveProcesses", ctypes.c_ulong), ("TerminatedProcesses", ctypes.c_ulong)]


def _kernel() -> Any:
    if os.name != "nt":
        raise ValueError("local execution requires a managed Windows Job")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    for name in ("GetCurrentProcess", "CreateJobObjectW", "CreateToolhelp32Snapshot", "OpenThread", "OpenProcess", "OpenJobObjectW"):
        getattr(kernel, name).restype = ctypes.c_void_p
    # All handles are pointer-sized; leaving ctypes' default int arguments would
    # truncate handles on 64-bit Windows.
    kernel.IsProcessInJob.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_int)]
    kernel.QueryInformationJobObject.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p,
                                                ctypes.c_ulong, ctypes.c_void_p]
    kernel.SetInformationJobObject.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_ulong]
    kernel.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    kernel.TerminateJobObject.argtypes = [ctypes.c_void_p, ctypes.c_uint]
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel.Thread32First.argtypes = [ctypes.c_void_p, ctypes.POINTER(_ThreadEntry)]
    kernel.Thread32Next.argtypes = [ctypes.c_void_p, ctypes.POINTER(_ThreadEntry)]
    kernel.ResumeThread.argtypes = [ctypes.c_void_p]
    kernel.Process32FirstW.argtypes = [ctypes.c_void_p, ctypes.POINTER(_ProcessEntry)]
    kernel.Process32NextW.argtypes = [ctypes.c_void_p, ctypes.POINTER(_ProcessEntry)]
    kernel.GetProcessTimes.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                                      ctypes.c_void_p, ctypes.c_void_p]
    kernel.OpenJobObjectW.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_wchar_p]
    return kernel


def _lease_worker(lease: dict[str, Any]) -> bool:
    kernel = _kernel()
    snapshot = kernel.CreateToolhelp32Snapshot(2, 0)
    if snapshot in (None, ctypes.c_void_p(-1).value):
        raise ValueError("cannot verify managed worker ancestry")
    parents: dict[int, int] = {}
    try:
        entry = _ProcessEntry()
        entry.Size = ctypes.sizeof(entry)
        present = kernel.Process32FirstW(snapshot, ctypes.byref(entry))
        while present:
            parents[entry.ProcessId] = entry.ParentId
            present = kernel.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel.CloseHandle(snapshot)
    worker = lease.get("child_pid")
    current = os.getpid()
    seen: set[int] = set()
    while current and current != worker and current not in seen:
        seen.add(current)
        current = parents.get(current, 0)
    if current != worker:
        return False
    handle = kernel.OpenProcess(0x1000, False, worker)
    if not handle:
        return False
    try:
        created, exited, user, cpu = (ctypes.c_ulonglong() for _ in range(4))
        if not kernel.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited),
                                      ctypes.byref(user), ctypes.byref(cpu)):
            return False
        # .NET ticks start at year 1; Win32 FILETIME starts at year 1601.
        return created.value + 504911232000000000 == lease.get("child_start_utc_ticks")
    finally:
        kernel.CloseHandle(handle)


def _job_limits(job_name: str) -> dict[str, int]:
    """Query the lease's precise Job, rather than an ambient nested Job."""
    kernel = _kernel()
    job = kernel.OpenJobObjectW(4, False, job_name)  # JOB_OBJECT_QUERY only.
    if not job:
        raise ValueError("named managed Windows Job cannot be opened")
    try:
        member = ctypes.c_int()
        if not kernel.IsProcessInJob(kernel.GetCurrentProcess(), job, ctypes.byref(member)) or not member.value:
            raise ValueError("process is outside the named managed Windows Job")
        extended, cpu = _ExtendedLimits(), _CpuLimits()
        if not kernel.QueryInformationJobObject(job, 9, ctypes.byref(extended), ctypes.sizeof(extended), None) or not (
                kernel.QueryInformationJobObject(job, 15, ctypes.byref(cpu), ctypes.sizeof(cpu), None)):
            raise ValueError("managed Windows Job limits cannot be queried")
        if (extended.Basic.Flags != 0x2208 or cpu.Flags != 5 or
                not extended.JobMemoryLimit or not extended.Basic.ActiveProcessLimit):
            raise ValueError("managed Windows Job protections are incomplete: "
                             f"flags={extended.Basic.Flags:#x}, cpu_flags={cpu.Flags:#x}, cpu_rate={cpu.CpuRate}, "
                             f"job_memory={extended.JobMemoryLimit}, active_process_limit={extended.Basic.ActiveProcessLimit}, "
                             f"layout={ctypes.sizeof(_BasicLimits)}/{ctypes.sizeof(_ExtendedLimits)}/{ctypes.sizeof(_CpuLimits)}")
        return {"cpu_percent": cpu.CpuRate // 100, "memory_bytes": extended.JobMemoryLimit,
                "max_processes": extended.Basic.ActiveProcessLimit}
    finally:
        kernel.CloseHandle(job)


class _ProcessTree:
    """Nested kill-on-close Job; suspend until assignment so no child escapes."""

    def __init__(self, process: subprocess.Popen[bytes]):
        self.kernel = _kernel()
        self.handle = self.kernel.CreateJobObjectW(None, None)
        if not self.handle:
            raise OSError("cannot create command Job")
        limits = _ExtendedLimits()
        limits.Basic.Flags = 0x2000
        try:
            if not self.kernel.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
                raise OSError("cannot configure command Job")
            if not self.kernel.AssignProcessToJobObject(self.handle, int(process._handle)):  # type: ignore[attr-defined]
                raise OSError("command could not inherit nested Job protection")
            snapshot = self.kernel.CreateToolhelp32Snapshot(4, 0)
            if snapshot in (None, ctypes.c_void_p(-1).value):
                raise OSError("cannot find suspended command thread")
            try:
                entry = _ThreadEntry()
                entry.Size = ctypes.sizeof(entry)
                available = self.kernel.Thread32First(snapshot, ctypes.byref(entry))
                thread_ids: list[int] = []
                while available:
                    if entry.ProcessId == process.pid:
                        thread_ids.append(entry.ThreadId)
                    available = self.kernel.Thread32Next(snapshot, ctypes.byref(entry))
                if len(thread_ids) != 1:
                    raise OSError("suspended command thread identity is ambiguous")
                thread = self.kernel.OpenThread(2, False, thread_ids[0])
                if not thread:
                    raise OSError("cannot open suspended command thread")
                try:
                    if self.kernel.ResumeThread(thread) != 1:
                        raise OSError("cannot resume protected command")
                finally:
                    self.kernel.CloseHandle(thread)
            finally:
                self.kernel.CloseHandle(snapshot)
        except Exception:
            self.close()
            raise

    def close(self) -> bool:
        stopped = True
        if self.handle:
            stopped = bool(self.kernel.TerminateJobObject(self.handle, 1))
            expires = time.monotonic() + 5
            accounting = _Accounting()
            while stopped:
                if not self.kernel.QueryInformationJobObject(self.handle, 1, ctypes.byref(accounting),
                                                             ctypes.sizeof(accounting), None):
                    stopped = False
                    break
                if accounting.ActiveProcesses == 0:
                    break
                if time.monotonic() >= expires:
                    stopped = False
                    break
                time.sleep(0.02)
            stopped = bool(self.kernel.CloseHandle(self.handle)) and stopped
            self.handle = None
        return stopped


class LocalApplicationExecutor:
    """OfflineExecutor interface plus read-only structured execution evidence."""

    @property
    def verification_level(self) -> VerificationLevel:
        if self.config.mode == "source":
            return "source_execution"
        if self.config.mode == "application":
            return "application_execution"
        return "offline_synthetic"

    def __init__(self, config: LocalCheckConfig, workspace: CandidateWorkspace | None = None, *,
                 managed_root: Path, lease_path: Path | None = None, stop_path: Path | None = None,
                 allow_application: bool = False, excluded_env_names: tuple[str, ...] = ()):
        # Copy nested mutable containers: Pydantic frozen models alone are shallow.
        self.config = LocalCheckConfig.model_validate_json(config.model_dump_json())
        self.managed_root = _checked(managed_root)
        self.lease_path = lease_path or self.managed_root / "state/active.json"
        self.stop_path = stop_path or self.managed_root / "state/stop-request.json"
        self.allow_application = allow_application
        self.excluded_env_names = {name.casefold() for name in (*excluded_env_names, *config.credential_env_names)}
        self.project_root = _checked(Path(config.project_root))
        self.project_manifest = _inventory(self.project_root, config.max_project_bytes)
        self.command_sha256 = digest(json.dumps(config.model_dump(mode="json"), sort_keys=True,
                                               separators=(",", ":")).encode())
        self.project_sha256 = digest(json.dumps(self.project_manifest, sort_keys=True,
                                               separators=(",", ":")).encode())
        self.protocol_sha256 = digest(json.dumps({"schema_version": 1, "command_sha256": self.command_sha256,
            "project_sha256": self.project_sha256}, sort_keys=True, separators=(",", ":")).encode())
        self.workspace: CandidateWorkspace | None = None
        self.root: Path | None = None
        self.deadline: float | None = None
        if workspace is not None:
            self.bind_workspace(workspace)

    def _guard(self) -> dict[str, Any]:
        if os.environ.get("DFX_MANAGED_COMPETITION") != "local-v1":
            raise ValueError("local checks require the managed competition entry point")
        owner = json.loads(_checked(self.managed_root / "owner.json").read_text(encoding="utf-8-sig"))
        lease = json.loads(_checked(self.lease_path).read_text(encoding="utf-8-sig"))
        if (owner.get("schema_version") != 1 or lease.get("schema_version") != 1 or
                owner.get("managed_real_path") != str(self.managed_root) or
                lease.get("task") not in ("competition", "test") or
                lease.get("project_instance_id") != owner.get("project_instance_id") or not _lease_worker(lease)):
            raise ValueError("managed local check lease does not belong to this process")
        job_name = lease.get("job_name")
        if not isinstance(job_name, str) or job_name != "Local\\DFX-Guard-" + lease.get("run_id", ""):
            raise ValueError("managed local check lease has an invalid named Job identity")
        actual = _job_limits(job_name)
        effective = lease.get("effective_limits", {})
        if any(effective.get(name) != value for name, value in actual.items()):
            raise ValueError("managed local check lease and actual Job limits differ")
        if self.config.max_output_bytes > effective.get("max_log_file_bytes", 0) or (
                self.config.max_check_seconds > effective.get("timeout_seconds", 0)):
            raise ValueError("local check limits exceed the managed lease")
        if self.config.mode == "application":
            policy_path = Path(owner["repo_real_path"]) / "configs/local-runtime.json"
            policy = json.loads(_checked(policy_path).read_text(encoding="utf-8"))
            if not self.allow_application or policy.get("execution", {}).get("allow_application_experiments") is not True:
                raise ValueError("real application experiments are disabled")
        return lease

    def bind_workspace(self, workspace: CandidateWorkspace) -> None:
        self._guard()
        root = _checked(workspace.root)
        if not root.is_relative_to(self.managed_root) or root == self.managed_root:
            raise ValueError("local check workspace must be inside the managed root")
        if root.is_relative_to(self.project_root) or self.project_root.is_relative_to(root):
            raise ValueError("full project and managed workspace overlap")
        base_manifest = _manifest(workspace.base)
        if snapshot_digest(base_manifest) != workspace.source_snapshot_sha256:
            raise ValueError("analysis baseline changed")
        mapping = self.config.source_mapping or {item["path"]: item["path"] for item in base_manifest}
        if set(mapping) != {item["path"] for item in base_manifest}:
            raise ValueError("source mapping does not cover the complete analysis baseline")
        project_files = {item["path"]: item["sha256"] for item in self.project_manifest["files"]}
        for item in base_manifest:
            if project_files.get(mapping[item["path"]]) != item["sha256"]:
                raise ValueError("analysis source mapping differs from the full project baseline")
        self.workspace = workspace
        self.root = local_evidence_path(root / "local-execution")
        identity = {"schema_version": 1, "protocol_sha256": self.protocol_sha256,
                    "command_sha256": self.command_sha256, "project_sha256": self.project_sha256,
                    "source_snapshot_sha256": workspace.source_snapshot_sha256,
                    "task_id": workspace.task_id, "run_id": workspace.run_id,
                    "config": self.config.model_dump(mode="json"), "project_manifest": self.project_manifest}
        if self.root.exists():
            _checked(self.root)
            saved = json.loads(_checked(self.root / "protocol.json").read_text(encoding="utf-8"))
            if saved != identity or _inventory(self.root / "project-base", self.config.max_project_bytes) != self.project_manifest:
                raise ValueError("frozen local execution protocol or project changed")
            return
        self.root.mkdir()
        _copy_project(self.project_root, self.root / "project-base", self.project_manifest, self.config.max_project_bytes)
        _atomic_json(self.root / "protocol.json", identity)

    def _bound(self) -> tuple[CandidateWorkspace, Path]:
        if self.workspace is None or self.root is None:
            raise ValueError("local executor is not bound to a candidate workspace")
        _checked(self.root)
        current_command_sha = digest(json.dumps(self.config.model_dump(mode="json"), sort_keys=True,
                                               separators=(",", ":")).encode())
        if current_command_sha != self.command_sha256:
            raise ValueError("host local check configuration changed after freezing")
        if snapshot_digest(_manifest(self.workspace.base)) != self.workspace.source_snapshot_sha256:
            raise ValueError("analysis baseline changed after local binding")
        return self.workspace, self.root

    def set_deadline(self, deadline: float) -> None:
        """Use the enclosing task's monotonic deadline; never extend a prior cap."""
        if not isinstance(deadline, (int, float)) or not 0 < deadline < float("inf"):
            raise ValueError("invalid local check deadline")
        self.deadline = min(self.deadline, deadline) if self.deadline is not None else deadline

    def verify_oracles(self) -> None:
        """Check fixed acceptance bytes without executing them or approving semantics.

        Legacy protocols remain readable but cannot authorize new application
        processes. The hash is part of the frozen command protocol; neither a
        later configure nor a successful process exit reapproves changed bytes.
        """
        references = self.config.independent_oracles
        if references is None:
            if self.config.mode in ("application", "source"):
                raise ValueError("source/application execution requires bound independent oracle files; reconfigure for a new run")
            return
        for oracle in references.values():
            path = Path(oracle.path)
            if not path.is_absolute():
                raise ValueError("independent oracle path must be absolute")
            actual = _checked(path)
            if actual.is_relative_to(self.project_root) or (self.workspace is not None and
                    actual.is_relative_to(self.workspace.root)):
                raise ValueError("independent oracle is inside a writable project or run")
            if not actual.is_file() or actual.stat().st_size > oracle.max_bytes:
                raise ValueError("independent oracle is unavailable or exceeds its read limit")
            with actual.open("rb") as stream:
                content = stream.read(oracle.max_bytes + 1)
            if len(content) > oracle.max_bytes or digest(content) != oracle.sha256:
                raise ValueError("independent oracle bytes differ from the frozen execution protocol")

    def _environment(self, project: Path) -> dict[str, str]:
        # Credentials for the cloud provider never reach build/test processes.
        environment = {name: value for name, value in os.environ.items()
                       if name.casefold() not in self.excluded_env_names and
                       name.casefold() not in {"npm_config_cache", "dfx_project_cache_root"} and
                       not name.startswith("DFX_COMPETITION_")}
        temporary = project / ".dfx-command-tmp"
        temporary.mkdir(exist_ok=True)
        cache = project / ".dfx-tool-cache"
        (cache / "npm").mkdir(parents=True, exist_ok=True)
        # npm documents npm_config_* as process-local settings. HarmonyOS's
        # other tools must use reviewed cache arguments/provisioning; this
        # project variable is for the approved runner, not a guessed SDK API.
        environment.update({"TEMP": str(temporary), "TMP": str(temporary), "PYTHONDONTWRITEBYTECODE": "1",
                            "npm_config_cache": str(cache / "npm"), "DFX_PROJECT_CACHE_ROOT": str(cache)})
        return environment

    def _stopped(self, lease: dict[str, Any]) -> bool:
        if not self.stop_path.exists():
            return False
        stop = json.loads(_checked(self.stop_path).read_text(encoding="utf-8-sig"))
        if stop.get("run_id") != lease["run_id"]:
            raise ValueError("managed stop request has a different run identity")
        return True

    def _stage(self, stage: str, role: str, project: Path, receipt_root: Path,
               lease: dict[str, Any], deadline: float) -> dict[str, Any]:
        command = self.config.preparation if stage == "prepare" else self.config.commands[stage]
        if command is None:
            raise ValueError("project dependency preparation is not configured")
        executable = _checked(Path(command.argv[0]))
        if self.config.mode == "synthetic" and executable != _checked(Path(sys.executable)):
            raise ValueError("synthetic checks only allow the current managed Python executable")
        if executable.suffix.casefold() in (".cmd", ".bat", ".ps1"):
            raise ValueError("local checks do not launch shell scripts")
        receipt: dict[str, Any] = {"stage": stage, "role": role, "argv_sha256": digest(
            json.dumps(command.argv, separators=(",", ":")).encode()), "started_at": _now(),
            "finished_at": None, "status": "inconclusive", "reason": "before_launch", "exit_code": None,
            "subtree_stopped": False, "process_started": False, "stdout": None, "stderr": None}
        if self.config.independent_oracles is not None:
            receipt["oracle_binding_verified"] = False
        record_path = receipt_root / f"{role}-{stage}.json"
        _atomic_json(record_path, receipt)
        emit_progress("check_stage_prepared", stage=stage, role=role, process_started=False)
        if self._stopped(lease) or time.monotonic() >= deadline:
            receipt.update(reason="stop_requested" if self._stopped(lease) else "check_timeout", finished_at=_now())
            _atomic_json(record_path, receipt)
            emit_progress("check_stage_completed", stage=stage, role=role, status=receipt["status"],
                          reason=receipt["reason"], process_started=False)
            return receipt
        paths = [receipt_root / f"{role}-{stage}.{stream}.txt" for stream in ("stdout", "stderr")]
        exceeded = threading.Event()
        errors: list[str] = []
        counts = [0, 0]

        def collect(pipe: BinaryIO, path: Path, index: int) -> None:
            try:
                with path.open("xb") as output:
                    while chunk := pipe.read(8192):
                        remaining = self.config.max_output_bytes - counts[index]
                        output.write(chunk[:max(0, remaining)])
                        counts[index] += min(len(chunk), max(0, remaining))
                        if len(chunk) > remaining:
                            exceeded.set()
                            break
                    output.flush()
                    os.fsync(output.fileno())
            except (OSError, ValueError) as exc:
                errors.append(type(exc).__name__)
                exceeded.set()
            finally:
                pipe.close()

        process: subprocess.Popen[bytes] | None = None
        tree: _ProcessTree | None = None
        threads: list[threading.Thread] = []
        try:
            self._guard()  # Recheck the persistent lease immediately before launch.
            self.verify_oracles()
            flags = 0x4 | 0x08000000  # CREATE_SUSPENDED | CREATE_NO_WINDOW.
            process = subprocess.Popen(command.argv, cwd=project, env=self._environment(project), shell=False,
                                       stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       creationflags=flags)
            assert process.stdout is not None and process.stderr is not None
            for index, pipe in enumerate((process.stdout, process.stderr)):
                thread = threading.Thread(target=collect, args=(pipe, paths[index], index), daemon=True)
                thread.start()
                threads.append(thread)
            tree = _ProcessTree(process)
            receipt["process_started"] = True
            _atomic_json(record_path, receipt)
            emit_progress("check_stage_started", stage=stage, role=role, process_started=True)
            expires = min(deadline, time.monotonic() + command.timeout_seconds)
            reason = "exited"
            while process.poll() is None:
                if exceeded.is_set():
                    reason = "output_capture_failed" if errors else "output_limit"
                    break
                if self._stopped(lease):
                    reason = "stop_requested"
                    break
                if time.monotonic() >= expires:
                    reason = "timeout"
                    break
                time.sleep(0.02)
            stopped = tree.close()  # Also closes surviving build grandchildren after parent exit.
            tree = None
            process.wait(timeout=5)
            for thread in threads:
                thread.join(timeout=5)
            if any(thread.is_alive() for thread in threads):
                reason = "output_drain_unknown"
            elif exceeded.is_set():
                reason = "output_capture_failed" if errors else "output_limit"
            if not stopped:
                reason = "subtree_stop_unknown"
            receipt.update(exit_code=process.returncode, reason=reason,
                           subtree_stopped=stopped and not any(thread.is_alive() for thread in threads))
            if reason == "exited":
                receipt["status"] = "pass" if process.returncode == 0 else "fail"
        except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
            receipt.update(reason="launch_or_stop_unknown", error_type=type(exc).__name__, error_detail=str(exc)[:1024])
        finally:
            if tree is not None:
                tree.close()
            if process is not None and process.poll() is None:
                # A failed Job assignment leaves the process suspended; it never
                # ran or spawned children. This terminates only that exact PID.
                process.kill()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    receipt["subtree_stopped"] = False
            for thread in threads:
                thread.join(timeout=1)
            if self.config.independent_oracles is not None:
                try:
                    self.verify_oracles()
                except (OSError, ValueError) as exc:
                    receipt.update(status="inconclusive", reason="oracle_binding_changed",
                                   error_type=type(exc).__name__, error_detail=str(exc)[:1024])
                else:
                    # A not-started stage cannot supply an execution binding.
                    receipt["oracle_binding_verified"] = receipt["process_started"]
            receipt["finished_at"] = _now()
            for label, path, size in zip(("stdout", "stderr"), paths, counts, strict=True):
                if path.exists() and not any(thread.is_alive() for thread in threads):
                    receipt[label] = {"path": path.relative_to(receipt_root).as_posix(), "size_bytes": size,
                                      "sha256": digest(_checked(path).read_bytes())}
            _atomic_json(record_path, receipt)
            emit_progress("check_stage_completed", stage=stage, role=role, status=receipt["status"],
                          reason=receipt["reason"], process_started=receipt["process_started"])
        return receipt

    def _overlay(self, project: Path, candidate: Path, workspace: CandidateWorkspace) -> str:
        baseline = {item["path"]: item["sha256"] for item in _manifest(workspace.base)}
        current = _manifest(candidate)
        mapping = self.config.source_mapping
        candidate_names = {item["path"] for item in current}
        if mapping and not candidate_names.issubset(mapping):
            raise ValueError("candidate addition has no fixed full-project source mapping")
        project_files = {item["path"] for item in self.project_manifest["files"]}
        for source, source_sha in baseline.items():
            destination = project / mapping.get(source, source)
            if digest(_checked(destination).read_bytes()) != source_sha:
                raise ValueError("candidate overlay base differs from the frozen full project")
            if source not in candidate_names:
                destination.unlink()
        for item in current:
            relative = mapping.get(item["path"], item["path"])
            destination = _checked(project / relative, exists=False)
            if item["path"] not in baseline and relative in project_files:
                raise ValueError("candidate addition would overwrite an unrelated full-project resource")
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(candidate / item["path"], destination)
            if digest(destination.read_bytes()) != item["sha256"]:
                raise ValueError("candidate changed while overlaying the full project")
        return snapshot_digest(current)

    def _assert_source_binding(self, project: Path, manifest: list[dict[str, str]], removed: set[str]) -> None:
        for item in manifest:
            relative = self.config.source_mapping.get(item["path"], item["path"])
            if digest(_checked(project / relative).read_bytes()) != item["sha256"]:
                raise ValueError("command changed the bound execution source")
        for source in removed:
            path = project / self.config.source_mapping.get(source, source)
            _checked(path, exists=False)
            if path.exists():
                raise ValueError("command restored a deleted candidate source")

    def _record_source_binding(self, result: dict[str, Any], project: Path, manifest: list[dict[str, str]],
                               removed: set[str], receipt_root: Path, record: dict[str, Any], path: Path) -> None:
        """Keep completed process evidence even when its source postcheck fails."""
        _atomic_json(path, record)
        try:
            self._assert_source_binding(project, manifest, removed)
        except (OSError, ValueError):
            result["source_binding_verified"] = False
            raise
        else:
            result["source_binding_verified"] = True
        finally:
            _atomic_json(receipt_root / f"{result['role']}-{result['stage']}.json", result)
            _atomic_json(path, record)
            emit_progress("check_source_bound", stage=result["stage"], role=result["role"],
                          source_binding_verified=result["source_binding_verified"])

    def check(self, *, key: str, source: Path) -> tuple[CheckStatus, str]:
        require_hash(key)
        workspace, root = self._bound()
        candidate = workspace.require_registered(source.parent.name)
        if _checked(source) != candidate:
            raise ValueError("local check source is not the registered complete candidate")
        candidate_sha = snapshot_digest(_manifest(candidate))
        identity = {"idempotency_key": key, "candidate_id": candidate.parent.name,
                    "candidate_snapshot_sha256": candidate_sha, "source_snapshot_sha256": workspace.source_snapshot_sha256,
                    "protocol_sha256": self.protocol_sha256, "project_sha256": self.project_sha256,
                    "command_sha256": self.command_sha256, "mode": self.config.mode}
        receipt_root = root / key
        path = receipt_root / "execution.json"
        if receipt_root.exists():
            _checked(receipt_root)
            if not path.exists():
                evidence = self.evidence(key)
                return evidence["status"], evidence["detail"]
            saved = json.loads(_checked(path).read_text(encoding="utf-8"))
            if any(saved.get(name) != value for name, value in identity.items()):
                raise ValueError("local check key was reused with different source or protocol")
            if saved.get("state") == "done":
                evidence = self.evidence(key)
                return evidence["status"], evidence["detail"]
            if saved.get("state") != "pending":
                raise ValueError("invalid local check receipt state")
            return "inconclusive", "local check outcome is unknown after interruption; no relaunch"
        lease = self._guard()
        self.verify_oracles()
        if _inventory(root / "project-base", self.config.max_project_bytes) != self.project_manifest:
            raise ValueError("frozen full project changed")
        receipt_root.mkdir()
        record: dict[str, Any] = {**identity, "schema_version": (4 if self.config.mode == "source" else
            3 if self.config.independent_oracles is not None else 2),
            "state": "pending", "started_at": _now(),
            "finished_at": None, "status": "inconclusive", "detail": "check pending", "baseline": None, "stages": [],
            "verification_level": self.verification_level,
            "application_verification": "not_run", "application_success": False, "accepted": False}
        if self.config.mode == "source":
            record["source_verification"] = "not_run"
            record["validation_scope"] = self.config.validation_scope
            record["mocked_interfaces"] = self.config.mocked_interfaces
        _atomic_json(path, record)
        deadline = time.monotonic() + self.config.max_check_seconds
        if self.deadline is not None:
            deadline = min(deadline, self.deadline)
        try:
            # CreateProcessW cannot use an arbitrarily long current directory.
            # Keep the complete key in the receipt; compact physical copies
            # remain inside this run and any prefix collision is refused by
            # _copy_project, which never overwrites an existing destination.
            base = root / ("b-" + key[:16])
            project = root / ("c-" + key[:16])
            if os.name == "nt" and max(len(str(base)), len(str(project))) > 258:
                raise ValueError("managed run path is too long for the Windows command working directory")
            record.update(baseline_project=base.relative_to(root).as_posix(),
                          candidate_project=project.relative_to(root).as_posix())
            _copy_project(root / "project-base", base, self.project_manifest, self.config.max_project_bytes)
            _copy_project(root / "project-base", project, self.project_manifest, self.config.max_project_bytes)
            self._overlay(project, candidate, workspace)
            candidate_manifest = _manifest(candidate)
            base_manifest = _manifest(workspace.base)
            removed = {item["path"] for item in base_manifest} - {item["path"] for item in candidate_manifest}
            record["execution_source_sha256"] = digest(json.dumps(_inventory(project, self.config.max_project_bytes),
                sort_keys=True, separators=(",", ":")).encode())
            baseline_stages: list[dict[str, Any]] = []
            record["baseline"] = {"fault_reproduced": False, "stages": baseline_stages}
            if self.config.preparation is not None:
                record["preparation"] = []
                for role, prepared, manifest, deleted in (
                        ("baseline", base, base_manifest, set()),
                        ("candidate", project, candidate_manifest, removed)):
                    preparation = self._stage("prepare", role, prepared, receipt_root, lease, deadline)
                    record["preparation"].append(preparation)
                    self._record_source_binding(preparation, prepared, manifest, deleted, receipt_root, record, path)
                    if preparation["status"] != "pass":
                        raise ValueError(f"{role} project dependency preparation unavailable: "
                                         f"{preparation['reason']}, exit_code={preparation['exit_code']}")
            build = self._stage("build", "baseline", base, receipt_root, lease, deadline)
            baseline_stages.append(build)
            self._record_source_binding(build, base, base_manifest, set(), receipt_root, record, path)
            reproduced = False
            if build["status"] == "pass":
                target = self._stage("target", "baseline", base, receipt_root, lease, deadline)
                baseline_stages.append(target)
                self._record_source_binding(target, base, base_manifest, set(), receipt_root, record, path)
                marker = self.config.baseline_fault_pattern
                captured = "".join((receipt_root / result["path"]).read_text(encoding="utf-8", errors="replace")
                    for result in (target["stdout"], target["stderr"]) if result is not None)
                reproduced = (target["reason"] == "exited" and target["subtree_stopped"] and
                              target["exit_code"] in self.config.baseline_target_exit_codes and
                              (marker is None or marker in captured))
            record["baseline"] = {"fault_reproduced": reproduced, "stages": baseline_stages}
            if reproduced:
                for stage in STAGES:
                    result = self._stage(stage, "candidate", project, receipt_root, lease, deadline)
                    record["stages"].append(result)
                    self._record_source_binding(result, project, candidate_manifest, removed, receipt_root, record, path)
                    if result["status"] != "pass":
                        detail = f"candidate {stage}: {result['reason']}, exit_code={result['exit_code']}"
                        output = result["stderr"]
                        if output is None or output["size_bytes"] == 0:
                            output = result["stdout"]
                        if output is not None:
                            with _checked(receipt_root / output["path"]).open("rb") as stream:
                                excerpt = stream.read(1024).decode("utf-8", errors="replace").strip()
                            if excerpt:
                                detail += "; output: " + excerpt
                        record.update(status=result["status"], detail=detail)
                        break
                else:
                    record.update(status="pass", detail=f"{self.config.mode} baseline and all four candidate stages passed")
                    if self.config.mode == "application":
                        record.update(application_verification="pass", application_success=True, accepted=True)
            else:
                unknown = any(result["status"] == "inconclusive" for result in baseline_stages)
                failed_stage = baseline_stages[-1]
                detail = ("baseline fault reproduction is unknown" if unknown else
                          "baseline did not reproduce the fixed target fault")
                detail += f"; {failed_stage['stage']}: {failed_stage['reason']}"
                if failed_stage.get("error_detail"):
                    detail += "; " + failed_stage["error_detail"]
                record.update(status="inconclusive" if unknown else "fail",
                              detail=detail)
        except (OSError, ValueError) as exc:
            record.update(status="inconclusive", detail="local execution preparation or capture failed: " + str(exc)[:1024],
                          error_type=type(exc).__name__, error_detail=str(exc)[:1024])
        # Exceptions not explicitly reconciled (including interruption) retain
        # the pending intent and may only be read as unknown on recovery.
        record.update(state="done", finished_at=_now())
        record["application_verification"] = application_check_state(record)
        if self.config.mode == "source":
            record["source_verification"] = execution_check_state(record, "source")
        _atomic_json(path, record)
        return record["status"], record["detail"]

    def evidence(self, key: str) -> dict[str, Any]:
        """Read and validate receipts without creating or executing anything."""
        require_hash(key)
        workspace, root = self._bound()
        folder = _checked(root / key)
        path = folder / "execution.json"
        if not path.exists():
            if any(folder.iterdir()):
                raise ValueError("local execution receipt is missing from a nonempty evidence directory")
            # mkdir precedes the first durable intent. An empty directory has
            # no execution facts to authenticate; retain uncertainty without
            # creating a receipt, approving a pass or relaunching a process.
            return {"state": "initialization_interrupted", "idempotency_key": key,
                    "status": "inconclusive", "detail": "local check initialization was interrupted; no relaunch",
                    "verification_level": self.verification_level,
                    "application_verification": "inconclusive" if self.config.mode == "application" else "not_run",
                    "application_success": False, "accepted": False, "baseline": None, "stages": [],
                    **({"source_verification": "inconclusive"} if self.config.mode == "source" else {})}
        record = json.loads(_checked(path).read_text(encoding="utf-8"))
        if (record.get("protocol_sha256") != self.protocol_sha256 or record.get("idempotency_key") != key or
                record.get("project_sha256") != self.project_sha256 or record.get("command_sha256") != self.command_sha256 or
                record.get("source_snapshot_sha256") != workspace.source_snapshot_sha256 or record.get("mode") != self.config.mode):
            raise ValueError("local execution evidence identity changed")
        candidate = workspace.require_registered(record["candidate_id"])
        if snapshot_digest(_manifest(candidate)) != record["candidate_snapshot_sha256"]:
            raise ValueError("local execution candidate evidence changed")
        if record.get("state") == "pending":
            return {**record, "status": "inconclusive", "detail": "local check outcome is unknown; no relaunch",
                    "verification_level": self.verification_level,
                    "application_verification": application_check_state(record),
                    "application_success": False, "accepted": False,
                    **({"source_verification": execution_check_state(record, "source")}
                       if self.config.mode == "source" else {})}
        if record.get("state") != "done" or record.get("status") not in ("pass", "fail", "inconclusive"):
            raise ValueError("invalid local execution evidence state")
        schema = record.get("schema_version")
        if schema not in (1, 2, 3, 4):
            raise ValueError("unsupported local execution evidence schema")
        if schema in (3, 4) and self.config.independent_oracles is None:
            raise ValueError("bound local execution evidence has no independent oracle protocol")
        if (self.config.mode == "source") != (schema == 4):
            raise ValueError("source execution evidence requires its explicit source scope schema")
        if schema == 4 and (record.get("validation_scope") != self.config.validation_scope or
                            record.get("mocked_interfaces") != self.config.mocked_interfaces):
            raise ValueError("source execution validation scope declaration changed")
        preparations = record.get("preparation", [])
        if self.config.preparation is None and preparations:
            raise ValueError("unexpected project preparation evidence")
        if preparations and [item["role"] for item in preparations] not in (["baseline"], ["baseline", "candidate"]):
            raise ValueError("invalid project preparation order")
        results = [*preparations, *(record.get("baseline") or {}).get("stages", []), *record["stages"]]
        captured: dict[tuple[str, str], str] = {}
        for result in results:
            stage, role = result["stage"], result["role"]
            command = self.config.preparation if stage == "prepare" else self.config.commands.get(stage)
            if command is None or role not in ("baseline", "candidate") or result["argv_sha256"] != digest(
                    json.dumps(command.argv, separators=(",", ":")).encode()):
                raise ValueError("local stage command identity changed")
            expected_status = ("pass" if result["exit_code"] == 0 else "fail") if result["reason"] == "exited" else "inconclusive"
            if result["status"] != expected_status or (result["reason"] == "exited" and not result["subtree_stopped"]):
                raise ValueError("local stage status disagrees with process outcome")
            if schema in (2, 3, 4) and (type(result.get("process_started")) is not bool or
                    (result["reason"] == "exited" and result["process_started"] is not True)):
                raise ValueError("local stage execution observation changed")
            if schema in (2, 3, 4) and type(result.get("source_binding_verified")) is not bool:
                raise ValueError("local stage source binding observation is missing")
            if schema in (3, 4) and type(result.get("oracle_binding_verified")) is not bool:
                raise ValueError("local stage oracle binding observation is missing")
            stage_path = folder / f"{result['role']}-{result['stage']}.json"
            if json.loads(_checked(stage_path).read_text(encoding="utf-8")) != result:
                raise ValueError("local stage receipt changed")
            for label in ("stdout", "stderr"):
                output = result[label]
                if output is None:
                    continue
                require_path(output["path"])
                if output["path"] != f"{role}-{stage}.{label}.txt":
                    raise ValueError("local stage output belongs to another command")
                content = _checked(folder / output["path"]).read_bytes()
                if len(content) > self.config.max_output_bytes or len(content) != output["size_bytes"] or digest(content) != output["sha256"]:
                    raise ValueError("local stage output evidence changed")
                captured[(role, stage)] = captured.get((role, stage), "") + content.decode("utf-8", errors="replace")
        baseline = record.get("baseline")
        if baseline is not None:
            base_results = baseline["stages"]
            reproduced = (len(base_results) == 2 and [result["stage"] for result in base_results] == ["build", "target"] and
                all(result["role"] == "baseline" for result in base_results) and base_results[0]["status"] == "pass" and
                (schema == 1 or all(result["source_binding_verified"] for result in base_results)) and
                (schema not in (3, 4) or all(result["oracle_binding_verified"] for result in base_results)) and
                base_results[1]["reason"] == "exited" and base_results[1]["subtree_stopped"] and
                base_results[1]["exit_code"] in self.config.baseline_target_exit_codes and
                (self.config.baseline_fault_pattern is None or self.config.baseline_fault_pattern in captured.get(("baseline", "target"), "")))
            if baseline["fault_reproduced"] is not reproduced:
                raise ValueError("baseline fault reproduction disagrees with fixed protocol")
        preparation_passed = (self.config.preparation is None or
            (len(preparations) == 2 and all(result["stage"] == "prepare" and result["status"] == "pass" and
                                          result["subtree_stopped"] and result.get("source_binding_verified") is True
                                          and (schema not in (3, 4) or result.get("oracle_binding_verified") is True)
                                          for result in preparations)))
        passed = (record["status"] == "pass" and preparation_passed and
                  (record.get("baseline") or {}).get("fault_reproduced") is True and
                  [result["stage"] for result in record["stages"]] == list(STAGES) and
                  (schema == 1 or all(result["source_binding_verified"] for result in record["stages"])) and
                  (schema not in (3, 4) or all(result["oracle_binding_verified"] for result in record["stages"])) and
                  all(result["role"] == "candidate" and result["status"] == "pass" and result["subtree_stopped"] for result in record["stages"]))
        if record["status"] == "pass" and not passed:
            raise ValueError("local execution pass is missing required evidence")
        application = passed and self.config.mode == "application"
        level = self.verification_level
        application_state = application_check_state(record)
        saved_state = ("pass" if application else "not_run") if schema == 1 else application_state
        if (record["application_success"] is not application or record["accepted"] is not application or
                record["application_verification"] != saved_state or
                (schema in (2, 3, 4) and record.get("verification_level") != level)):
            raise ValueError("local execution evidence overstates application verification")
        if self.config.mode == "source" and record.get("source_verification") != execution_check_state(record, "source"):
            raise ValueError("local execution evidence overstates source verification")
        if self.config.mode == "application" and schema != 3:
            # Preserve validation of legacy originals above, but never upgrade
            # their unchecked oracle declaration into present-day acceptance.
            return {**record, "status": "inconclusive", "verification_level": level,
                    "application_verification": "inconclusive" if application_state != "not_run" else "not_run",
                    "application_success": False, "accepted": False,
                    "detail": "legacy application evidence has no execution-time independent oracle binding"}
        # Do not rewrite legacy originals; expose the outcome derived from
        # their authenticated stages separately from the saved schema-1 label.
        return {**record, "verification_level": level, "application_verification": application_state}
