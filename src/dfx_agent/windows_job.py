"""Small Win32 Job adapter; no PowerShell, compiler or external DLL is required.

This translates the existing audited Job policy to the Windows ABI. The worker
is created suspended, assigned to a queried Job, then resumed after its lease is
published. Closing the sole host Job handle kills the whole process tree.
"""

from __future__ import annotations

import ctypes
import os
import subprocess
import threading
from ctypes import wintypes as w
from pathlib import Path
from typing import Any, Self


class _Basic(ctypes.Structure):
    _fields_ = [("process_time", ctypes.c_int64), ("job_time", ctypes.c_int64),
                ("flags", w.DWORD), ("min_working", ctypes.c_size_t),
                ("max_working", ctypes.c_size_t), ("active", w.DWORD),
                ("affinity", ctypes.c_size_t), ("priority", w.DWORD), ("scheduling", w.DWORD)]


class _IO(ctypes.Structure):
    _fields_ = [(key, ctypes.c_uint64) for key in ("read", "write", "other", "read_bytes", "write_bytes", "other_bytes")]


class _Extended(ctypes.Structure):
    _fields_ = [("basic", _Basic), ("io", _IO), ("process_memory", ctypes.c_size_t),
                ("job_memory", ctypes.c_size_t), ("peak_process", ctypes.c_size_t), ("peak_job", ctypes.c_size_t)]


class _CPU(ctypes.Structure):
    _fields_ = [("flags", w.DWORD), ("rate", w.DWORD)]


class _Startup(ctypes.Structure):
    _fields_ = [("size", w.DWORD), ("reserved", w.LPWSTR), ("desktop", w.LPWSTR),
                ("title", w.LPWSTR), *[(key, w.DWORD) for key in ("x", "y", "width", "height", "chars_x", "chars_y", "fill", "flags")],
                ("show", w.WORD), ("reserved_size", w.WORD), ("reserved_bytes", ctypes.c_void_p),
                ("stdin", w.HANDLE), ("stdout", w.HANDLE), ("stderr", w.HANDLE)]


class _Process(ctypes.Structure):
    _fields_ = [("process", w.HANDLE), ("thread", w.HANDLE), ("pid", w.DWORD), ("tid", w.DWORD)]


class _Security(ctypes.Structure):
    _fields_ = [("size", w.DWORD), ("descriptor", ctypes.c_void_p), ("inherit", w.BOOL)]


class _Memory(ctypes.Structure):
    _fields_ = [("size", w.DWORD), ("load", w.DWORD), *[(key, ctypes.c_uint64) for key in
                ("total", "available", "page_total", "page_available", "virtual_total", "virtual_available", "extended")]]


class _Accounting(ctypes.Structure):
    _fields_ = [*( (key, ctypes.c_int64) for key in ("user", "kernel", "period_user", "period_kernel")),
               *[(key, w.DWORD) for key in ("faults", "total_processes", "active_processes", "terminated_processes")]]


class _ProcessEntry(ctypes.Structure):
    _fields_ = [("size", w.DWORD), ("usage", w.DWORD), ("pid", w.DWORD), ("heap", ctypes.c_size_t),
                ("module", w.DWORD), ("threads", w.DWORD), ("parent", w.DWORD), ("priority", w.LONG),
                ("flags", w.DWORD), ("executable", ctypes.c_wchar * 260)]


def _api() -> Any:
    if os.name != "nt":
        raise OSError("DFX managed execution currently requires Windows")
    kernel: Any = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    signatures = {
        "CreateJobObjectW": ([ctypes.c_void_p, w.LPCWSTR], w.HANDLE),
        "OpenJobObjectW": ([w.DWORD, w.BOOL, w.LPCWSTR], w.HANDLE),
        "SetInformationJobObject": ([w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD], w.BOOL),
        "QueryInformationJobObject": ([w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD, ctypes.c_void_p], w.BOOL),
        "AssignProcessToJobObject": ([w.HANDLE, w.HANDLE], w.BOOL),
        "TerminateJobObject": ([w.HANDLE, w.UINT], w.BOOL),
        "CloseHandle": ([w.HANDLE], w.BOOL),
        "CreateMutexW": ([ctypes.c_void_p, w.BOOL, w.LPCWSTR], w.HANDLE),
        "ReleaseMutex": ([w.HANDLE], w.BOOL),
        "WaitForSingleObject": ([w.HANDLE, w.DWORD], w.DWORD),
        "GlobalMemoryStatusEx": ([ctypes.POINTER(_Memory)], w.BOOL),
        "CreatePipe": ([ctypes.POINTER(w.HANDLE), ctypes.POINTER(w.HANDLE), ctypes.POINTER(_Security), w.DWORD], w.BOOL),
        "SetHandleInformation": ([w.HANDLE, w.DWORD, w.DWORD], w.BOOL),
        "CreateFileW": ([w.LPCWSTR, w.DWORD, w.DWORD, ctypes.POINTER(_Security), w.DWORD, w.DWORD, w.HANDLE], w.HANDLE),
        "CreateProcessW": ([w.LPCWSTR, w.LPWSTR, ctypes.c_void_p, ctypes.c_void_p, w.BOOL, w.DWORD,
                            ctypes.c_void_p, w.LPCWSTR, ctypes.POINTER(_Startup), ctypes.POINTER(_Process)], w.BOOL),
        "ResumeThread": ([w.HANDLE], w.DWORD),
        "ReadFile": ([w.HANDLE, ctypes.c_void_p, w.DWORD, ctypes.POINTER(w.DWORD), ctypes.c_void_p], w.BOOL),
        "GetExitCodeProcess": ([w.HANDLE, ctypes.POINTER(w.DWORD)], w.BOOL),
        "OpenProcess": ([w.DWORD, w.BOOL, w.DWORD], w.HANDLE),
        "GetProcessTimes": ([w.HANDLE, ctypes.POINTER(w.FILETIME), ctypes.POINTER(w.FILETIME),
                             ctypes.POINTER(w.FILETIME), ctypes.POINTER(w.FILETIME)], w.BOOL),
        "QueryFullProcessImageNameW": ([w.HANDLE, w.DWORD, w.LPWSTR, ctypes.POINTER(w.DWORD)], w.BOOL),
        "TerminateProcess": ([w.HANDLE, w.UINT], w.BOOL),
        "GetCurrentProcess": ([], w.HANDLE),
        "IsProcessInJob": ([w.HANDLE, w.HANDLE, ctypes.POINTER(w.BOOL)], w.BOOL),
        "CreateToolhelp32Snapshot": ([w.DWORD, w.DWORD], w.HANDLE),
        "Process32FirstW": ([w.HANDLE, ctypes.POINTER(_ProcessEntry)], w.BOOL),
        "Process32NextW": ([w.HANDLE, ctypes.POINTER(_ProcessEntry)], w.BOOL),
    }
    for name, (arguments, result) in signatures.items():
        method = getattr(kernel, name)
        method.argtypes, method.restype = arguments, result
    return kernel


def _require(ok: Any, action: str) -> None:
    if not ok:
        raise ctypes.WinError(ctypes.get_last_error(), action)  # type: ignore[attr-defined]


def physical_memory() -> tuple[int, int]:
    status = _Memory(size=ctypes.sizeof(_Memory))
    _require(_api().GlobalMemoryStatusEx(ctypes.byref(status)), "physical memory query failed")
    return status.total, status.available


def _identity(kernel: Any, handle: Any) -> tuple[int, str]:
    times = [w.FILETIME() for _ in range(4)]
    _require(kernel.GetProcessTimes(handle, *(ctypes.byref(value) for value in times)), "process creation time unavailable")
    buffer = ctypes.create_unicode_buffer(32768)
    length = w.DWORD(len(buffer))
    _require(kernel.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(length)), "process image unavailable")
    ticks = (times[0].dwHighDateTime << 32) + times[0].dwLowDateTime + 504911232000000000
    return ticks, buffer.value


def process_identity(pid: int) -> tuple[int, str] | None:
    kernel = _api()
    handle = kernel.OpenProcess(0x1000 | 0x100000, False, pid)
    if not handle:
        if ctypes.get_last_error() == 87:  # type: ignore[attr-defined]
            return None
        _require(False, "process identity unavailable")
    try:
        if kernel.WaitForSingleObject(handle, 0) == 0:
            return None
        return _identity(kernel, handle)
    finally:
        kernel.CloseHandle(handle)


def terminate_identity(pid: int, ticks: int, executable: str) -> None:
    kernel = _api()
    handle = kernel.OpenProcess(0x1000 | 0x100000 | 1, False, pid)
    _require(handle, "cannot open exact process for stop")
    try:
        if _identity(kernel, handle) != (ticks, executable):
            raise ValueError("process identity changed; stop refused")
        _require(kernel.TerminateProcess(handle, 1), "exact process stop failed")
        if kernel.WaitForSingleObject(handle, 5000) != 0:
            raise OSError("exact process did not stop")
    finally:
        kernel.CloseHandle(handle)


def lease_worker_identity(marker: dict[str, Any]) -> bool:
    """Accept the exact child or its live venv redirector descendant.

    Windows venv python.exe can delegate to the actual Python image. Ancestry
    does not replace named Job membership/readback; the caller requires both.
    """
    kernel = _api()
    snapshot = kernel.CreateToolhelp32Snapshot(2, 0)
    if snapshot in (None, ctypes.c_void_p(-1).value):
        _require(False, "worker ancestry unavailable")
    parents: dict[int, int] = {}
    try:
        entry = _ProcessEntry(size=ctypes.sizeof(_ProcessEntry))
        present = kernel.Process32FirstW(snapshot, ctypes.byref(entry))
        while present:
            parents[entry.pid] = entry.parent
            present = kernel.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel.CloseHandle(snapshot)
    current, seen = os.getpid(), set()
    worker = marker.get("child_pid")
    if type(worker) is not int:
        return False
    while current and current != worker and current not in seen:
        seen.add(current)
        current = parents.get(current, 0)
    if current != worker:
        return False
    identity = process_identity(worker)
    return identity is not None and identity[0] == marker.get("child_start_utc_ticks")


class ProjectMutex:
    def __init__(self, name: str):
        self.kernel = _api()
        self.handle = self.kernel.CreateMutexW(None, False, name)
        _require(self.handle, "project mutex creation failed")
        self.acquired = False

    def __enter__(self) -> Self:
        result = self.kernel.WaitForSingleObject(self.handle, 0)
        self.acquired = result in (0, 0x80)
        if not self.acquired:
            self.kernel.CloseHandle(self.handle)
            raise ValueError("managed project is busy")
        return self

    def __exit__(self, *_: object) -> None:
        if self.acquired:
            self.kernel.ReleaseMutex(self.handle)
        self.kernel.CloseHandle(self.handle)


class BoundedJob:
    def __init__(self, name: str, cpu_percent: int, memory_bytes: int, max_processes: int):
        if not 1 <= cpu_percent <= 100 or memory_bytes < 16777216 or max_processes < 1:
            raise ValueError("invalid Job policy")
        self.kernel = _api()
        self.handle = self.kernel.CreateJobObjectW(None, name)
        existed = ctypes.get_last_error() == 183  # type: ignore[attr-defined]
        _require(self.handle, "Job creation failed")
        self.process: Any = None
        self.thread: Any = None
        self.read: Any = None
        self.output: threading.Thread | None = None
        self.pid = 0
        self.log_exceeded = False
        self.output_error = False
        try:
            if existed:
                raise ValueError("named Job already exists")
            limits = _Extended()
            limits.basic.flags = 0x2000 | 0x8 | 0x200
            limits.basic.active = max_processes
            limits.job_memory = memory_bytes
            cpu = _CPU(flags=0x1 | 0x4, rate=cpu_percent * 100)
            for kind, value in ((9, limits), (15, cpu)):
                _require(self.kernel.SetInformationJobObject(self.handle, kind, ctypes.byref(value), ctypes.sizeof(value)), "Job policy could not be applied")
            actual, actual_cpu = _Extended(), _CPU()
            for kind, value in ((9, actual), (15, actual_cpu)):
                _require(self.kernel.QueryInformationJobObject(self.handle, kind, ctypes.byref(value), ctypes.sizeof(value), None), "Job policy readback failed")
            if (actual.basic.flags, actual.basic.active, actual.job_memory, actual_cpu.flags, actual_cpu.rate) != (
                    limits.basic.flags, max_processes, memory_bytes, cpu.flags, cpu.rate):
                raise ValueError("Job policy readback differs")
        except BaseException:
            self.kernel.CloseHandle(self.handle)
            self.handle = None
            raise

    def start_suspended(self, argv: list[str], cwd: Path, env: dict[str, str], log: Path, max_bytes: int) -> None:
        if self.process is not None:
            raise ValueError("Job already started")
        security = _Security(size=ctypes.sizeof(_Security), inherit=True)
        read, write = w.HANDLE(), w.HANDLE()
        _require(self.kernel.CreatePipe(ctypes.byref(read), ctypes.byref(write), ctypes.byref(security), 0), "output pipe failed")
        self.read = read
        null: Any = None
        try:
            _require(self.kernel.SetHandleInformation(read, 1, 0), "pipe inheritance restriction failed")
            null = self.kernel.CreateFileW("NUL", 0x80000000, 3, ctypes.byref(security), 3, 0, None)
            if null == ctypes.c_void_p(-1).value:
                _require(False, "null input creation failed")
            startup = _Startup(size=ctypes.sizeof(_Startup), flags=0x100, stdin=null, stdout=write, stderr=write)
            info = _Process()
            block = ctypes.create_unicode_buffer("\0".join(f"{key}={value}" for key, value in sorted(env.items(), key=lambda item: item[0].casefold())) + "\0\0")
            _require(self.kernel.CreateProcessW(argv[0], ctypes.create_unicode_buffer(subprocess.list2cmdline(argv)),
                None, None, True, 0x4 | 0x400 | 0x08000000, block, str(cwd), ctypes.byref(startup), ctypes.byref(info)), "suspended worker creation failed")
            self.process, self.thread, self.pid = info.process, info.thread, info.pid
            _require(self.kernel.AssignProcessToJobObject(self.handle, self.process), "worker could not enter Job")
            self.output = threading.Thread(target=self._drain, args=(log, max_bytes), daemon=True)
            self.output.start()
        except BaseException:
            if self.process:
                self.kernel.TerminateProcess(self.process, 1)
            raise
        finally:
            self.kernel.CloseHandle(write)
            if null and null != ctypes.c_void_p(-1).value:
                self.kernel.CloseHandle(null)

    def _drain(self, path: Path, max_bytes: int) -> None:
        try:
            total = 0
            with path.open("xb") as output:
                buffer = ctypes.create_string_buffer(8192)
                count = w.DWORD()
                while True:
                    if not self.kernel.ReadFile(self.read, buffer, len(buffer), ctypes.byref(count), None):
                        if ctypes.get_last_error() == 109:  # type: ignore[attr-defined]
                            break
                        _require(False, "output pipe read failed")
                    if not count.value:
                        continue
                    room = max(0, max_bytes - total)
                    output.write(buffer.raw[:min(room, count.value)])
                    output.flush()
                    total += count.value
                    if total > max_bytes:
                        self.log_exceeded = True
                        self.terminate()
        except (OSError, ValueError):
            self.output_error = True
            if self.handle:
                self.kernel.TerminateJobObject(self.handle, 1)

    def resume(self) -> None:
        if self.output_error or self.log_exceeded or self.kernel.ResumeThread(self.thread) != 1:
            self.terminate()
            raise OSError("suspended worker could not safely resume")
        self.kernel.CloseHandle(self.thread)
        self.thread = None

    def exited(self) -> bool:
        return bool(self.process and self.kernel.WaitForSingleObject(self.process, 0) == 0)

    def terminate(self) -> None:
        if self.handle:
            _require(self.kernel.TerminateJobObject(self.handle, 1), "Job stop failed")

    def exit_code(self) -> int:
        if not self.exited():
            raise ValueError("worker is still running")
        code = w.DWORD()
        _require(self.kernel.GetExitCodeProcess(self.process, ctypes.byref(code)), "worker exit unavailable")
        return code.value

    def close(self) -> None:
        # Kill remaining descendants even if the primary worker has exited.
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None
        if self.process:
            self.kernel.WaitForSingleObject(self.process, 5000)
        if self.output:
            self.output.join(timeout=5)
            self.output_error |= self.output.is_alive()
        for handle in (self.thread, self.process, self.read):
            if handle:
                self.kernel.CloseHandle(handle)
        self.thread = self.process = self.read = None


def job_quiescent(name: str) -> bool:
    kernel = _api()
    handle = kernel.OpenJobObjectW(4, False, name)
    if not handle:
        if ctypes.get_last_error() == 2:  # type: ignore[attr-defined]
            return True
        _require(False, "Job state unavailable")
    try:
        result = _Accounting()
        _require(kernel.QueryInformationJobObject(handle, 1, ctypes.byref(result), ctypes.sizeof(result), None), "Job process tree state unavailable")
        return result.active_processes == 0
    finally:
        kernel.CloseHandle(handle)


def current_job_limits(name: str) -> dict[str, int]:
    kernel = _api()
    handle = kernel.OpenJobObjectW(4, False, name)
    _require(handle, "managed Job is unavailable")
    try:
        member = w.BOOL()
        _require(kernel.IsProcessInJob(kernel.GetCurrentProcess(), handle, ctypes.byref(member)), "Job membership query failed")
        if not member.value:
            raise ValueError("worker is outside the named managed Job")
        extended, cpu = _Extended(), _CPU()
        for kind, value in ((9, extended), (15, cpu)):
            _require(kernel.QueryInformationJobObject(handle, kind, ctypes.byref(value), ctypes.sizeof(value), None), "worker Job limit query failed")
        if extended.basic.flags != 0x2000 | 0x8 | 0x200 or cpu.flags != 0x1 | 0x4:
            raise ValueError("worker Job protections differ")
        return {"cpu_percent": cpu.rate // 100, "memory_bytes": extended.job_memory,
                "max_processes": extended.basic.active}
    finally:
        kernel.CloseHandle(handle)
