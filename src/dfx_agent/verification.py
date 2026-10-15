"""Candidate-bound offline checks and crash-reconcilable execution intents.

This module deliberately cannot certify application behavior. The separate
remote simulation and acceptance gate also require real execution evidence.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict

from .contracts import require_hash, require_path
from .task import digest
from .workspace import CandidateWorkspace


class CheckResult(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    candidate_id: str
    check_id: str
    idempotency_key: str
    protocol_sha256: str
    status: Literal["pass", "fail", "inconclusive"]
    detail: str
    level: Literal["offline_synthetic", "application_execution"] = "offline_synthetic"

    @property
    def execution_scope(self) -> Literal["synthetic", "application"]:
        return "application" if self.level == "application_execution" else "synthetic"


class OfflineExecutor(Protocol):
    protocol_sha256: str
    def check(self, *, key: str, source: Path) -> tuple[Literal["pass", "fail", "inconclusive"], str]: ...


class ReplayOnlyExecutor:
    """Expose a frozen check identity without supplying an execution capability."""

    def __init__(self, protocol_sha256: str):
        require_hash(protocol_sha256)
        self.protocol_sha256 = protocol_sha256

    def check(self, *, key: str, source: Path) -> tuple[Literal["pass", "fail", "inconclusive"], str]:
        raise ValueError("replay-only executor cannot run a check")


def _atomic_json(path: Path, value: dict[str, object]) -> None:
    temporary = path.with_name(path.name + ".new")
    if temporary.exists():
        if temporary.is_symlink() or not temporary.is_file():
            raise ValueError("unsafe interrupted record")
        temporary.unlink()
    with temporary.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


class CheckLedger:
    """Write intent before calling a backend; retry only with the same key."""

    def __init__(self, workspace: CandidateWorkspace, executor: OfflineExecutor, *, read_only: bool = False):
        if type(read_only) is not bool:
            raise ValueError("read_only must be a boolean")
        self.workspace = workspace
        self.executor = executor
        self.read_only = read_only
        self.root = workspace.root / "checks"
        if read_only:
            try:
                absolute = self.root.absolute()
                if ".." in absolute.parts:
                    raise ValueError("check ledger path is not canonical")
                for component in (*reversed(absolute.parents), absolute):
                    info = component.lstat()
                    if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode) or
                            getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)):
                        raise ValueError("check ledger directory is missing or linked")
            except OSError as exc:
                raise ValueError("check ledger directory is missing or inaccessible") from exc
            return
        self.root.mkdir(exist_ok=True)
        if self.root.is_symlink():
            raise ValueError("check ledger is linked")

    def _identity(self, candidate_id: str, check_id: str) -> tuple[str, Path]:
        require_hash(candidate_id)
        if check_id != "offline":
            raise ValueError("unsupported offline check")
        require_hash(self.executor.protocol_sha256)
        key = digest(f"{self.workspace.run_id}:{candidate_id}:{check_id}:{self.executor.protocol_sha256}".encode())
        return key, self.root / f"{key}.json"

    def _record(self, candidate_id: str, check_id: str, key: str, path: Path) -> dict[str, object] | None:
        if path.exists():
            if path.is_symlink():
                raise ValueError("check intent is linked")
            record = json.loads(path.read_text(encoding="utf-8"))
            if (record.get("idempotency_key") != key or record.get("candidate_id") != candidate_id or
                    record.get("check_id") != check_id or record.get("protocol_sha256") != self.executor.protocol_sha256):
                raise ValueError("check intent identity changed")
            if record.get("state") not in ("done", "pending"):
                raise ValueError("invalid check intent state")
            return record
        return None

    def completed(self, candidate_id: str, check_id: str = "offline") -> CheckResult | None:
        self.workspace.require_registered(candidate_id)
        key, path = self._identity(candidate_id, check_id)
        record = self._record(candidate_id, check_id, key, path)
        if record is None or record["state"] != "done":
            return None
        result = CheckResult.model_validate(record["result"])
        if (result.candidate_id != candidate_id or result.check_id != check_id or
                result.idempotency_key != key or result.protocol_sha256 != self.executor.protocol_sha256):
            raise ValueError("check result identity changed")
        from .local_execution import LocalApplicationExecutor

        if isinstance(self.executor, LocalApplicationExecutor) and self.executor.config.mode == "application":
            # Revalidate persisted process facts. Legacy application checks lack
            # execution-time oracle bindings and are exposed as inconclusive,
            # without changing either original ledger or execution receipts.
            evidence = self.executor.evidence(key)
            result = result.model_copy(update={"status": evidence["status"], "detail": evidence["detail"],
                                               "level": evidence["verification_level"]})
        return result

    def run(self, candidate_id: str, check_id: str = "offline") -> CheckResult:
        if self.read_only:
            raise ValueError("check ledger is read-only")
        source = self.workspace.require_registered(candidate_id)
        key, path = self._identity(candidate_id, check_id)
        done = self.completed(candidate_id, check_id)
        if done is not None:
            return done
        if not path.exists():
            _atomic_json(path, {"state": "pending", "idempotency_key": key, "candidate_id": candidate_id,
                                "check_id": check_id, "protocol_sha256": self.executor.protocol_sha256})
        status, detail = self.executor.check(key=key, source=source)
        from .local_execution import LocalApplicationExecutor

        level: Literal["offline_synthetic", "application_execution"] = (
            "application_execution" if isinstance(self.executor, LocalApplicationExecutor) and
            self.executor.config.mode == "application" else "offline_synthetic")
        result = CheckResult(candidate_id=candidate_id, check_id=check_id, idempotency_key=key,
                             protocol_sha256=self.executor.protocol_sha256, status=status, detail=detail, level=level)
        _atomic_json(path, {"state": "done", "idempotency_key": key, "candidate_id": candidate_id,
                            "check_id": check_id, "protocol_sha256": self.executor.protocol_sha256,
                            "result": result.model_dump()})
        return result


class FixedTextExecutor:
    """Deterministic test double; never reports a real compiler or device pass."""

    def __init__(self, path: str, expected_text: str,
                 cached: dict[str, tuple[Literal["pass", "fail", "inconclusive"], str]] | None = None,
                 failure_detail: str = "synthetic text expectation did not match"):
        require_path(path)
        self.path = path
        self.expected_text = expected_text
        self.calls: list[str] = []
        self.requests: list[str] = []
        self.cached = cached if cached is not None else {}
        self.failure_detail = failure_detail
        self.protocol_sha256 = digest(json.dumps({"path": path, "expected_text": expected_text,
                                                  "failure_detail": failure_detail},
                                                 sort_keys=True, separators=(",", ":")).encode())

    def check(self, *, key: str, source: Path) -> tuple[Literal["pass", "fail", "inconclusive"], str]:
        self.requests.append(key)
        if key in self.cached:
            return self.cached[key]
        self.calls.append(key)
        target = source / self.path
        if (not target.is_file() or target.is_symlink() or
                not target.resolve(strict=True).is_relative_to(source.resolve(strict=True))):
            raise ValueError("synthetic check path is missing or linked")
        content = target.read_text(encoding="utf-8")
        if content == self.expected_text:
            result: tuple[Literal["pass", "fail", "inconclusive"], str] = ("pass", "synthetic text expectation matched")
        else:
            result = ("fail", self.failure_detail)
        self.cached[key] = result
        return result
