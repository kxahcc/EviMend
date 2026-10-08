"""Bounded user-facing observations, separate from execution evidence and acceptance.

These events describe when the host observed a committed operation. They are not
a second ledger, historical reconstruction, or a reason to retry an operation.
"""

from __future__ import annotations

import json
import os
import re
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from typing import Any

MAX_PROGRESS_BYTES = 131072
_LIMIT_NOTICE_BYTES = 512
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_EVENTS = frozenset({"run_started", "resume_started", "run_finished", "model_reserved",
    "model_dispatched", "model_completed", "model_unknown", "model_replayed",
    "tool_dispatched", "tool_completed", "tool_replayed", "check_stage_prepared",
    "check_stage_started", "check_stage_completed", "check_source_bound", "report_saved",
    "tracking_limited"})
_ENUMS = {
    "state": {"reserved", "dispatched", "completed", "unknown"},
    "status": {"pass", "fail", "inconclusive", "not_run", "unresolved", "synthetic_checked",
               "application_verified", "failed", "finished", "budget_exhausted"},
    "usage_status": {"known", "missing", "invalid"},
    "authorization": {"allowed", "rejected"},
    "tool_status": {"success", "error"},
    "stage": {"build", "target", "behavior", "regression"},
    "role": {"baseline", "candidate"},
    "verification_level": {"not_run", "offline_synthetic", "application_execution"},
    "reason": {"capacity", "before_launch", "stop_requested", "check_timeout", "exited",
               "output_capture_failed", "output_limit", "timeout", "output_drain_unknown",
               "subtree_stop_unknown", "launch_or_stop_unknown"},
}
_TOOL_NAMES = frozenset({"read_log", "search_experience", "read_experience", "read_skill",
    "prepare_experience_context", "review_experience", "read_experience_review", "list_source",
    "read_source", "search_source", "inspect_analysis", "record_diagnosis", "record_obligation",
    "link_failed_check", "consult_rule", "plan_repair_action", "evaluate_condition", "edit_source",
    "register_candidate", "run_check"})
_IDS = frozenset({"call_id", "action_id"})
_HASHES = frozenset({"candidate_id", "check_key"})
_COUNTS = frozenset({"input_tokens", "output_tokens", "total_tokens", "candidate_count", "model_calls"})
_BOOLEANS = frozenset({"accepted", "process_started", "source_binding_verified"})
_TRACKER: ContextVar[ProgressTracker | None] = ContextVar("dfx_progress_tracker", default=None)


def _safe_fields(fields: dict[str, object]) -> dict[str, object]:
    """Only host identifiers, fixed vocabulary, booleans and counts may be displayed."""
    result: dict[str, object] = {}
    for key, value in fields.items():
        if key in _ENUMS and isinstance(value, str) and value in _ENUMS[key]:
            result[key] = value
        elif key == "tool_name" and isinstance(value, str) and value in _TOOL_NAMES:
            result[key] = value
        elif key in _IDS and isinstance(value, str) and _IDENTIFIER.fullmatch(value):
            result[key] = value
        elif key in _HASHES and isinstance(value, str) and _HASH.fullmatch(value):
            result[key] = value
        elif key in _COUNTS and type(value) is int and 0 <= value <= 2**63 - 1:
            result[key] = value
        elif key in _BOOLEANS and type(value) is bool:
            result[key] = value
    return result


def _normal(path: Path, *, missing_file: bool = False) -> os.stat_result | None:
    path = path.absolute()
    if ".." in path.parts:
        raise ValueError("progress path is not canonical")
    for component in (*reversed(path.parents), path):
        try:
            info = component.lstat()
        except FileNotFoundError:
            if component == path and missing_file:
                return None
            raise
        if (stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) &
                getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)):
            raise ValueError("progress path is linked")
        if component == path:
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError("progress file is not independent")
        elif not stat.S_ISDIR(info.st_mode):
            raise ValueError("progress parent is not a directory")
    return info


def _open(path: Path, flags: int) -> int:
    before = _normal(path, missing_file=bool(flags & os.O_CREAT))
    descriptor = os.open(path, flags | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0), 0o600)
    try:
        opened = os.fstat(descriptor)
        after = _normal(path)
        if (after is None or not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1 or
                (opened.st_dev, opened.st_ino) != (after.st_dev, after.st_ino) or
                before is not None and (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)):
            raise ValueError("progress file changed while opening")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _decode(raw: bytes) -> tuple[list[dict[str, Any]], bool]:
    partial = bool(raw and not raw.endswith(b"\n"))
    lines = raw.split(b"\n")[:-1]
    events = []
    run_id = None
    for sequence, line in enumerate(lines, 1):
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError("progress event is invalid")
        fixed = {"schema_version", "sequence", "observed_at", "run_id", "event"}
        if (value.get("schema_version") != 1 or type(value.get("sequence")) is not int or
                value["sequence"] != sequence or value.get("event") not in _EVENTS or
                not isinstance(value.get("run_id"), str) or not _IDENTIFIER.fullmatch(value["run_id"])):
            raise ValueError("progress event identity is invalid")
        if run_id is not None and value["run_id"] != run_id:
            raise ValueError("progress event run differs")
        run_id = value["run_id"]
        timestamp = datetime.fromisoformat(value["observed_at"])
        if timestamp.tzinfo is None or timestamp.utcoffset() != UTC.utcoffset(timestamp):
            raise ValueError("progress observation time is not UTC")
        fields = {key: item for key, item in value.items() if key not in fixed}
        if _safe_fields(fields) != fields:
            raise ValueError("progress event contains unsupported fields")
        events.append(value)
    return events, partial


def read_progress(work: Path, *, after_sequence: int = 0, max_events: int = 100,
                  max_bytes: int = MAX_PROGRESS_BYTES) -> dict[str, Any]:
    """Read display observations without modifying files, databases, or original reports."""
    if (type(after_sequence) is not int or after_sequence < 0 or type(max_events) is not int or
            not 1 <= max_events <= 1000 or type(max_bytes) is not int or not 512 <= max_bytes <= MAX_PROGRESS_BYTES):
        raise ValueError("invalid progress read limits")
    result: dict[str, Any] = {"tracking": "unavailable", "events": [], "next_sequence": after_sequence,
        "last_sequence": 0, "partial_line": False, "size_bytes": 0, "capacity_bytes": max_bytes}
    path = work / "events.jsonl"
    try:
        descriptor = _open(path, os.O_RDONLY)
        try:
            size = os.fstat(descriptor).st_size
            if size > max_bytes:
                raise ValueError("progress file exceeds capacity")
            with os.fdopen(descriptor, "rb", closefd=False) as stream:
                raw = stream.read(max_bytes + 1)
        finally:
            os.close(descriptor)
        if len(raw) > max_bytes:
            raise ValueError("progress file exceeds capacity")
        events, partial = _decode(raw)
        selected = [item for item in events if item["sequence"] > after_sequence][:max_events]
        result.update(tracking="unknown" if partial else "truncated"
            if events and events[-1]["event"] == "tracking_limited" else "available",
            events=selected, next_sequence=selected[-1]["sequence"] if selected else after_sequence,
            last_sequence=len(events), partial_line=partial, size_bytes=len(raw))
    except FileNotFoundError:
        pass
    except (OSError, ValueError, TypeError, KeyError, UnicodeError):
        result["tracking"] = "unknown"
    return result


class ProgressTracker:
    """One serialized best-effort display writer for an already admitted task."""

    def __init__(self, work: Path, run_id: str, max_bytes: int):
        if (not _IDENTIFIER.fullmatch(run_id) or type(max_bytes) is not int or
                not 512 <= max_bytes <= MAX_PROGRESS_BYTES):
            raise ValueError("invalid progress scope")
        self.work, self.run_id, self.max_bytes = work, run_id, max_bytes
        self.state = "unavailable"
        self._lock = RLock()

    def emit(self, event: str, fields: dict[str, object]) -> None:
        with self._lock:
            if self.state in ("unknown", "truncated"):
                return
            try:
                if event not in _EVENTS:
                    raise ValueError("unsupported progress event")
                previous = read_progress(self.work, max_events=1000, max_bytes=self.max_bytes)
                if previous["tracking"] in ("unknown", "truncated"):
                    self.state = previous["tracking"]
                    return
                if previous["events"] and previous["events"][0]["run_id"] != self.run_id:
                    raise ValueError("progress run identity differs")
                sequence = previous["last_sequence"] + 1
                value = {"schema_version": 1, "sequence": sequence,
                    "observed_at": datetime.now(UTC).isoformat(), "run_id": self.run_id,
                    "event": event, **_safe_fields(fields)}
                raw = (json.dumps(value, ensure_ascii=True, separators=(",", ":")) + "\n").encode()
                limited = previous["size_bytes"] + len(raw) + _LIMIT_NOTICE_BYTES > self.max_bytes
                if limited:
                    value = {key: value[key] for key in ("schema_version", "sequence", "observed_at", "run_id")}
                    value.update(event="tracking_limited", reason="capacity")
                    raw = (json.dumps(value, separators=(",", ":")) + "\n").encode()
                descriptor = _open(self.work / "events.jsonl", os.O_WRONLY | os.O_APPEND | os.O_CREAT)
                try:
                    if os.fstat(descriptor).st_size != previous["size_bytes"]:
                        raise ValueError("progress file changed before append")
                    if previous["size_bytes"] + len(raw) > self.max_bytes:
                        self.state = "truncated"
                        return
                    if os.write(descriptor, raw) != len(raw):
                        raise OSError("progress append is incomplete")
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
                self.state = "truncated" if limited else "available"
            except Exception:
                # Display failure never turns a completed effect into a retry.
                self.state = "unknown"


@contextmanager
def progress_scope(work: Path, run_id: str, *, max_bytes: int = MAX_PROGRESS_BYTES) -> Iterator[ProgressTracker]:
    """Install task-local observation delivery; entering never creates a workspace."""
    tracker = ProgressTracker(work, run_id, max_bytes)
    token = _TRACKER.set(tracker)
    try:
        yield tracker
    finally:
        _TRACKER.reset(token)


def emit_progress(event: str, **fields: object) -> None:
    tracker = _TRACKER.get()
    if tracker is not None:
        tracker.emit(event, fields)
