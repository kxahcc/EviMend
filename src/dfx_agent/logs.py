"""Parse crash evidence without interpreting embedded text as instructions."""

from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass
from typing import Any

MAX_LOG_BYTES = 10_000_000
FRAME = re.compile(r"^\s*at\s+(.*?)\((.+?\.(?:ets|ts|js)):(\d+):(\d+)\)\s*$")
FIELDS = ("Module name", "Reason", "Error name", "Error message")


def decode_text(data: bytes) -> tuple[str, str, list[str]]:
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        try:
            return data.decode("utf-16"), "utf-16", []
        except UnicodeError:
            return data.decode("utf-16", errors="replace"), "utf-16", ["invalid_utf16_bytes"]
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            return data.decode(encoding), encoding, []
        except UnicodeError:
            pass
    return data.decode("utf-8", errors="replace"), "utf-8", ["undecodable_bytes_replaced"]


def log_window(data: bytes, start_line: int = 1, end_line: int = 100,
               query: str | None = None) -> dict[str, Any]:
    """Read exact line anchors, or locate a literal and return a small surrounding window."""
    if (type(start_line) is not int or type(end_line) is not int or start_line < 1 or
            query is None and (end_line < start_line or end_line - start_line > 199)):
        raise ValueError("invalid or oversized line range")
    if len(data) > MAX_LOG_BYTES:
        raise ValueError("crash log exceeds input limit")
    text, encoding, warnings = decode_text(data)
    lines = text.splitlines()
    if query is None:
        return {"start_line": start_line, "lines": lines[start_line - 1:end_line],
                "encoding": encoding, "warnings": warnings}
    if (not isinstance(query, str) or not query.strip() or len(query) > 200 or
            any(c in query for c in ("\n", "\r", "\0"))):
        raise ValueError("query must be a nonempty bounded single-line literal")
    hit = next((i for i in range(start_line - 1, len(lines)) if query in lines[i]), None)
    first = max(0, hit - 3) if hit is not None else start_line - 1
    last = min(len(lines), hit + 13) if hit is not None else first
    next_hit = (next((i + 1 for i in range(hit + 1, len(lines)) if query in lines[i]), None)
                if hit is not None else None)
    return {"start_line": first + 1, "lines": lines[first:last], "encoding": encoding,
            "warnings": warnings, "query": query, "matched_line": None if hit is None else hit + 1,
            "next_match_line": next_hit, "total_lines": len(lines)}


def saved_log_excerpt_matches(data: bytes, start_line: int, statement: str) -> bool:
    """Check saved text without rewriting its original decoding interpretation.

    New admission uses decode_text only. Older receipts used UTF-8 replacement;
    that view remains valid solely when authenticating an already saved excerpt.
    Callers still verify the original log hash and receipt references.
    """
    count = statement.count("\n") + 1
    if start_line < 1 or count > 200:
        return False
    start, end = start_line - 1, start_line - 1 + count
    current = decode_text(data)[0].splitlines()
    if end <= len(current) and "\n".join(current[start:end]) == statement:
        return True
    legacy = data.decode("utf-8", errors="replace").splitlines()
    return end <= len(legacy) and "\n".join(legacy[start:end]) == statement


def normalize_frame_path(value: str) -> str:
    value = value.replace("\\", "/")
    value = value.removeprefix("file:")
    return re.sub(r"/+", "/", value)


def framework_path(value: str) -> bool:
    path = normalize_frame_path(value)
    return path.startswith(("/usr", "/system/", "obj/foundation/", "internal/")) or path.rsplit("/", 1)[-1] in ("stateMgmt.js", "jsXNode.js")


@dataclass(frozen=True)
class Frame:
    symbol: str
    path: str
    line: int
    column: int
    log_line: int
    framework: bool


@dataclass
class Crash:
    sha256: str
    encoding: str
    headers: dict[str, str]
    error_name: str | None
    error_name_source: str | None
    frames: list[Frame]
    warnings: list[str]

    def to_dict(self) -> dict:
        return asdict(self)


def parse_crash(data: bytes) -> Crash:
    if len(data) > MAX_LOG_BYTES:
        raise ValueError("Crash log exceeds supported size")
    text, encoding, warnings = decode_text(data)
    headers: dict[str, str] = {}
    frames: list[Frame] = []
    for number, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        for field in FIELDS:
            if stripped.startswith(field + ":"):
                value = stripped.split(":", 1)[1].strip()
                if field in headers and headers[field] != value:
                    warnings.append("conflicting_header:" + field)
                else:
                    headers[field] = value
        if match := FRAME.match(line):
            path = normalize_frame_path(match[2])
            frames.append(Frame(match[1].strip(), path, int(match[3]), int(match[4]), number, framework_path(path)))
    error_name = headers.get("Error name")
    error_source = "Error name" if error_name else None
    if not error_name and re.fullmatch(r"[A-Za-z]*Error", headers.get("Reason", "")):
        error_name = headers["Reason"]
        error_source = "Reason"
        warnings.append("error_name_inferred_from_reason")
    if not frames:
        warnings.append("no_source_frames")
    if not error_name:
        warnings.append("missing_error_name")
    return Crash(hashlib.sha256(data).hexdigest(), encoding, headers, error_name, error_source, frames, list(dict.fromkeys(warnings)))
