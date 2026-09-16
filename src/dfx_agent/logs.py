"""Parse crash evidence without interpreting embedded text as instructions."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import re


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


def normalize_frame_path(value: str) -> str:
    value = value.replace("\\", "/")
    if value.startswith("file:"):
        value = value[5:]
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
