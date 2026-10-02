"""Open stopped SQLite evidence without modifying its database or sidecars."""

from __future__ import annotations

import os
import sqlite3
import stat
from pathlib import Path
from urllib.parse import quote


def local_evidence_path(path: Path) -> Path:
    """Preserve unresolved path components while enabling Windows long-path APIs."""
    path = path.absolute()
    if os.name == "nt" and not str(path).startswith("\\\\?\\"):
        absolute = str(path)
        return Path("\\\\?\\UNC\\" + absolute[2:] if absolute.startswith("\\\\") else "\\\\?\\" + absolute)
    return path


def _normal_file(path: Path) -> None:
    """Inspect the unresolved path chain; resolving first would hide links."""
    if ".." in path.parts:
        raise ValueError("database evidence path is not canonical")
    for component in (*reversed(path.parents), path):
        info = component.lstat()
        if (stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) &
                getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)):
            raise ValueError("database evidence path is linked")
        if component == path:
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError("database evidence is not an independent regular file")
        elif not stat.S_ISDIR(info.st_mode):
            raise ValueError("database evidence parent is not a directory")


def open_readonly_database(path: Path) -> sqlite3.Connection:
    """Require quiescent evidence and reject journals that immutable mode would ignore.

    The caller must stop the source run and check its complete evidence tree for
    concurrent changes. This function never checkpoints, removes sidecars or
    creates a missing database.
    """
    path = local_evidence_path(path)
    try:
        _normal_file(path)
        for suffix in ("-wal", "-journal", "-shm"):
            sidecar = path.with_name(path.name + suffix)
            try:
                sidecar.lstat()
            except FileNotFoundError:
                continue
            _normal_file(sidecar)
            if suffix != "-shm" and sidecar.stat().st_size:
                raise ValueError("database evidence has an unmerged journal")
    except OSError as exc:
        raise ValueError("database evidence path is missing or inaccessible") from exc
    # Path.as_uri encodes the Windows extended prefix as a URI authority (%3F),
    # which SQLite refuses. Preserve it in the local filename, with empty authority.
    uri = ("file://" + quote(str(path).replace("\\", "/"), safe="/:")
           if str(path).startswith("\\\\?\\") else path.as_uri())
    connection = sqlite3.connect(uri + "?mode=ro&immutable=1", uri=True, timeout=10)
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        connection.execute("SELECT name FROM sqlite_master LIMIT 1").fetchall()
        return connection
    except BaseException:
        connection.close()
        raise
