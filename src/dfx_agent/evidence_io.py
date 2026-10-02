"""Open stopped SQLite evidence without modifying its database or sidecars."""

from __future__ import annotations

import sqlite3
import stat
from pathlib import Path


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
    path = path.absolute()
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
    connection = sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True, timeout=10)
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        connection.execute("SELECT name FROM sqlite_master LIMIT 1").fetchall()
        return connection
    except BaseException:
        connection.close()
        raise
