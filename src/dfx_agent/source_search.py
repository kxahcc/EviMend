"""Bounded literal retrieval from an independently checked frozen source tree."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .contracts import require_hash, require_path
from .evidence_io import local_evidence_path
from .task import digest, snapshot_digest
from .workspace import CandidateWorkspace, _files

MAX_FILES = 2048
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_TREE_BYTES = 16 * 1024 * 1024
MAX_QUERY_CHARS = 200
MAX_SNIPPET_CHARS = 240


def search_limits() -> dict[str, int]:
    return {"max_files": MAX_FILES, "max_file_bytes": MAX_FILE_BYTES, "max_tree_bytes": MAX_TREE_BYTES,
            "max_query_chars": MAX_QUERY_CHARS, "max_snippet_chars": MAX_SNIPPET_CHARS,
            "max_page_size": 100, "max_offset": MAX_TREE_BYTES}


def _bounded_files(root: Path) -> dict[str, Path]:
    files = _files(root)
    if len(files) > MAX_FILES:
        raise ValueError("source search file count exceeds limit; use a smaller task snapshot")
    total = 0
    for path in files.values():
        size = local_evidence_path(path).stat().st_size
        if size > MAX_FILE_BYTES:
            raise ValueError("source search file bytes exceed limit")
        total += size
        if total > MAX_TREE_BYTES:
            raise ValueError("source search tree bytes exceed limit")
    return files


def _read_tree(root: Path) -> dict[str, bytes]:
    values: dict[str, bytes] = {}
    total = 0
    for relative, path in sorted(_bounded_files(root).items()):
        with local_evidence_path(path).open("rb") as stream:
            raw = stream.read(MAX_FILE_BYTES + 1)
        if len(raw) > MAX_FILE_BYTES:
            raise ValueError("source search file bytes exceed limit")
        total += len(raw)
        if total > MAX_TREE_BYTES:
            raise ValueError("source search tree bytes exceed limit")
        values[relative] = raw
    return values


class SourceSearch:
    """Search is retrieval, never a causal or condition-value conclusion."""

    def __init__(self, workspace: CandidateWorkspace):
        self.workspace = workspace

    def list_files(self, *, path_prefix: str | None = None, offset: int = 0,
                   limit: int = 50) -> dict[str, Any]:
        """Page paths without flooding the model with a complete project inventory."""
        if type(offset) is not int or type(limit) is not int or offset < 0 or not 1 <= limit <= 100:
            raise ValueError("invalid source listing page")
        if path_prefix is not None:
            if isinstance(path_prefix, str):
                path_prefix = path_prefix.rstrip("/")
            require_path(path_prefix)
        files = sorted(path for path in _files(self.workspace.base)
                       if path_prefix is None or path == path_prefix or path.startswith(path_prefix + "/"))
        page = files[offset:offset + limit]
        has_more = offset + len(page) < len(files)
        return {"scope": "baseline", "source_snapshot_sha256": self.workspace.source_snapshot_sha256,
                "path_prefix": path_prefix, "files": page, "total_files": len(files),
                "offset": offset, "has_more": has_more,
                "next_offset": offset + len(page) if has_more else None}

    def search(self, query: str, *, path_prefix: str | None = None, candidate_id: str | None = None,
               case_sensitive: bool = True, offset: int = 0, limit: int = 30) -> dict[str, Any]:
        if (not isinstance(query, str) or not query.strip() or len(query) > MAX_QUERY_CHARS or
                any(character in query for character in ("\n", "\r", "\0"))):
            raise ValueError("query must be a nonempty bounded single-line literal")
        if type(case_sensitive) is not bool or type(offset) is not int or type(limit) is not int:
            raise ValueError("invalid search option type")
        # A matching nonempty line consumes at least one source byte; all pages are reachable.
        if not 0 <= offset <= MAX_TREE_BYTES or not 1 <= limit <= 100:
            raise ValueError("invalid search page")
        if path_prefix is not None:
            if isinstance(path_prefix, str):
                path_prefix = path_prefix.rstrip("/")
            require_path(path_prefix)
        base_values = _read_tree(self.workspace.base)
        base_manifest = [{"path": path, "sha256": digest(raw)} for path, raw in base_values.items()]
        if snapshot_digest(base_manifest) != self.workspace.source_snapshot_sha256:
            raise ValueError("workspace baseline changed")
        snapshot_sha256 = self.workspace.source_snapshot_sha256
        values = base_values
        if candidate_id is not None:
            require_hash(candidate_id)
            root = self.workspace.candidates / candidate_id / "source"
            # Bound both trees before the existing full candidate/diff validation.
            _bounded_files(root.parent)
            root = self.workspace.require_registered(candidate_id)
            values = _read_tree(root)
            snapshot_sha256 = snapshot_digest([
                {"path": path, "sha256": digest(raw)} for path, raw in values.items()
            ])
            record = json.loads(local_evidence_path(root.parent / "candidate.json").read_text(encoding="utf-8"))
            if snapshot_sha256 != record["candidate_snapshot_sha256"]:
                raise ValueError("candidate changed during source search")
        expression = re.compile(re.escape(query), 0 if case_sensitive else re.IGNORECASE)
        matches: list[dict[str, Any]] = []
        skipped: list[dict[str, str]] = []
        count = 0
        for relative, raw in values.items():
            if path_prefix is not None and relative != path_prefix and not relative.startswith(path_prefix + "/"):
                continue
            if b"\0" in raw:
                skipped.append({"path": relative, "reason": "binary_content"})
                continue
            try:
                content = raw.decode("utf-8")
            except UnicodeDecodeError:
                skipped.append({"path": relative, "reason": "unsupported_encoding"})
                continue
            file_sha256 = digest(raw)
            for line_number, line in enumerate(content.splitlines(), start=1):
                match = expression.search(line)
                if match is None:
                    continue
                if offset <= count < offset + limit:
                    start = max(0, match.start() - (MAX_SNIPPET_CHARS - len(query)) // 2)
                    text = line[start:start + MAX_SNIPPET_CHARS]
                    matches.append({"path": relative, "line": line_number, "column": match.start() + 1,
                                    "text": text, "snippet_start_column": start + 1,
                                    "text_truncated": len(text) < len(line),
                                    "file_sha256": file_sha256})
                count += 1
        has_more = offset + len(matches) < count
        return {"source_snapshot_sha256": self.workspace.source_snapshot_sha256,
                "snapshot_sha256": snapshot_sha256, "candidate_id": candidate_id,
                "scope": "baseline" if candidate_id is None else "candidate",
                "query": query, "path_prefix": path_prefix, "case_sensitive": case_sensitive,
                "matches": matches, "total_matched_lines": count,
                "has_more": has_more, "next_offset": offset + len(matches) if has_more else None,
                "complete": not skipped, "skipped": skipped}
