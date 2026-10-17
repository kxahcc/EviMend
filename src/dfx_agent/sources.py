"""Resolve stack locations conservatively against a fixed source snapshot."""

from __future__ import annotations

import os
import re
import stat
from collections import defaultdict
from pathlib import Path

from .evidence_io import local_evidence_path
from .logs import decode_text, framework_path, normalize_frame_path
from .task import _resolve_logical

SOURCE_SUFFIXES = {".ets", ".ts", ".js", ".c", ".cpp", ".h", ".hpp"}


class SourceIndex:
    def __init__(self, root: Path):
        self.root = _resolve_logical(root)
        self.paths: dict[str, Path] = {}
        self.by_name: dict[str, list[str]] = defaultdict(list)
        self.by_folded_path: dict[str, list[str]] = defaultdict(list)
        self._lines: dict[str, list[str]] = {}
        paths: list[Path] = []
        pending = [self.root]
        while pending:
            folder = pending.pop()
            with os.scandir(local_evidence_path(folder)) as entries:
                for entry in entries:
                    info = entry.stat(follow_symlinks=False)
                    if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & getattr(
                            stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0):
                        continue
                    path = folder / entry.name
                    if entry.is_dir(follow_symlinks=False):
                        pending.append(path)
                    elif entry.is_file(follow_symlinks=False) and path.suffix.lower() in SOURCE_SUFFIXES:
                        paths.append(path)
        for path in sorted(paths):
            relative = path.relative_to(self.root).as_posix()
            self.paths[relative] = path
            self.by_name[path.name.casefold()].append(relative)
            self.by_folded_path[relative.casefold()].append(relative)

    def resolve(self, frame_path: str) -> dict:
        normalized = normalize_frame_path(frame_path)
        if ".." in normalized.split("/"):
            return {"status": "rejected", "method": "path_traversal", "paths": []}
        if framework_path(normalized):
            return {"status": "framework", "method": None, "paths": []}
        if normalized in self.paths:
            return {"status": "resolved", "method": "exact", "paths": [normalized]}
        folded = self.by_folded_path.get(normalized.casefold(), [])
        if folded:
            return self._result(folded, "case_normalized")
        relative = normalized.lstrip("/")
        if "/" in relative and not re.match(r"^[A-Za-z]:", relative):
            matches = [p for p in self.paths if p.casefold().endswith("/" + relative.casefold())]
            if matches:
                return self._result(matches, "path_suffix")
        return self._result(self.by_name.get(normalized.rsplit("/", 1)[-1].casefold(), []), "basename")

    @staticmethod
    def _result(paths: list[str], method: str) -> dict:
        return {"status": "resolved" if len(paths) == 1 else "ambiguous" if paths else "unresolved", "method": method, "paths": sorted(paths)}

    def lines(self, path: str) -> list[str]:
        if path not in self.paths:
            raise ValueError("Path is not in the source index")
        if path not in self._lines:
            self._lines[path] = decode_text(local_evidence_path(self.paths[path]).read_bytes())[0].splitlines()
        return self._lines[path]

    def context(self, path: str, line: int, radius: int = 6) -> dict:
        if radius < 0:
            raise ValueError("Context radius must be nonnegative")
        lines = self.lines(path)
        in_range = 1 <= line <= len(lines)
        if not in_range:
            return {"line_in_range": False, "line_count": len(lines), "lines": []}
        start, end = max(1, line - radius), min(len(lines), line + radius)
        return {"line_in_range": True, "line_count": len(lines), "lines": [{"number": n, "text": lines[n - 1]} for n in range(start, end + 1)]}
