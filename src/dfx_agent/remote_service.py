"""Durable synthetic HTTP execution service; no device or background worker."""

from __future__ import annotations

import os
import secrets
import sqlite3
from contextlib import closing
from pathlib import Path
from uuid import uuid4

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import Response

from .backends import (
    ArtifactReference,
    Capabilities,
    ExecutionJob,
    ExecutionOutcome,
    SubmitEnvelope,
)
from .contracts import require_hash
from .task import digest


class SimulatedJobStore:
    """SQLite job table with explicit test-worker transitions and restart audit."""

    def __init__(self, database: Path, artifacts: Path):
        self.database = database
        self.artifacts = artifacts
        database.parent.mkdir(parents=True, exist_ok=True)
        artifacts.mkdir(parents=True, exist_ok=True)
        if database.is_symlink() or artifacts.is_symlink():
            raise ValueError("remote store paths cannot be links")
        with closing(self._connect()) as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS jobs (
                    job_id TEXT PRIMARY KEY,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    request_sha256 TEXT NOT NULL,
                    request_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    outcome_json TEXT
                );
                CREATE TABLE IF NOT EXISTS artifacts (
                    artifact_id TEXT PRIMARY KEY,
                    job_id TEXT NOT NULL,
                    sha256 TEXT NOT NULL,
                    size INTEGER NOT NULL,
                    FOREIGN KEY(job_id) REFERENCES jobs(job_id)
                );
            """)
            unknown = ExecutionOutcome(status="inconclusive",
                                       detail="service restarted before worker state could be confirmed")
            conn.execute("UPDATE jobs SET status='inconclusive', outcome_json=? "
                         "WHERE status IN ('running', 'cancel_requested')", (unknown.model_dump_json(),))

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.database, timeout=5, isolation_level=None)
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    @staticmethod
    def _job(row: tuple | None) -> ExecutionJob:
        if row is None:
            raise KeyError("job not found")
        job_id, key, request_sha, status, outcome_json = row
        return ExecutionJob(job_id=job_id, idempotency_key=key,
                            request_sha256=request_sha, status=status,
                            outcome=ExecutionOutcome.model_validate_json(outcome_json) if outcome_json else None)

    def submit(self, envelope: SubmitEnvelope) -> ExecutionJob:
        key = envelope.idempotency_key
        request_sha = envelope.request.sha256()
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT job_id, idempotency_key, request_sha256, status, outcome_json "
                               "FROM jobs WHERE idempotency_key=?", (key,)).fetchone()
            if row is not None:
                if row[2] != request_sha:
                    conn.rollback()
                    raise ValueError("idempotency key reused for another request")
                conn.commit()
                return self._job(row)
            job_id = str(uuid4())
            conn.execute("INSERT INTO jobs(job_id, idempotency_key, request_sha256, request_json, status) "
                         "VALUES (?, ?, ?, ?, 'queued')",
                         (job_id, key, request_sha, envelope.request.model_dump_json()))
            conn.commit()
            return ExecutionJob(job_id=job_id, idempotency_key=key,
                                request_sha256=request_sha, status="queued")

    def get(self, job_id: str) -> ExecutionJob:
        with closing(self._connect()) as conn:
            return self._job(conn.execute("SELECT job_id, idempotency_key, request_sha256, status, outcome_json "
                                          "FROM jobs WHERE job_id=?", (job_id,)).fetchone())

    def count_jobs(self) -> int:
        with closing(self._connect()) as conn:
            return int(conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0])

    def mark_running(self, job_id: str) -> ExecutionJob:
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            job = self._job(conn.execute("SELECT job_id, idempotency_key, request_sha256, status, outcome_json "
                                         "FROM jobs WHERE job_id=?", (job_id,)).fetchone())
            if job.status != "queued":
                conn.rollback()
                raise ValueError("only a queued job can start")
            conn.execute("UPDATE jobs SET status='running' WHERE job_id=?", (job_id,))
            conn.commit()
        return self.get(job_id)

    def finish(self, job_id: str, passed: bool, detail: str, artifact_bytes: bytes | None = None) -> ExecutionJob:
        if not detail.strip() or (artifact_bytes is not None and len(artifact_bytes) > 1_048_576):
            raise ValueError("invalid synthetic result")
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            job = self._job(conn.execute("SELECT job_id, idempotency_key, request_sha256, status, outcome_json "
                                         "FROM jobs WHERE job_id=?", (job_id,)).fetchone())
            if job.status != "running":
                conn.rollback()
                raise ValueError("only a running job can finish")
            reference = None
            if artifact_bytes is not None:
                sha = digest(artifact_bytes)
                artifact_id = digest(f"{job_id}:{sha}".encode())
                path = self.artifacts / artifact_id
                if path.exists():
                    if path.is_symlink() or path.read_bytes() != artifact_bytes:
                        conn.rollback()
                        raise ValueError("artifact identity changed")
                else:
                    temporary = self.artifacts / f"{artifact_id}.new"
                    if temporary.exists():
                        if temporary.is_symlink() or not temporary.is_file():
                            conn.rollback()
                            raise ValueError("unsafe interrupted artifact")
                        temporary.unlink()
                    with temporary.open("xb") as stream:
                        stream.write(artifact_bytes)
                        stream.flush()
                        os.fsync(stream.fileno())
                    temporary.replace(path)
                conn.execute("INSERT INTO artifacts(artifact_id, job_id, sha256, size) VALUES (?, ?, ?, ?)",
                             (artifact_id, job_id, sha, len(artifact_bytes)))
                reference = ArtifactReference(artifact_id=artifact_id, job_id=job_id,
                                              sha256=sha, size=len(artifact_bytes))
            outcome = ExecutionOutcome(status="pass" if passed else "fail", detail=detail, artifact=reference)
            conn.execute("UPDATE jobs SET status=?, outcome_json=? WHERE job_id=?",
                         ("completed" if passed else "failed", outcome.model_dump_json(), job_id))
            conn.commit()
        return self.get(job_id)

    def request_cancel(self, job_id: str) -> ExecutionJob:
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            job = self._job(conn.execute("SELECT job_id, idempotency_key, request_sha256, status, outcome_json "
                                         "FROM jobs WHERE job_id=?", (job_id,)).fetchone())
            if job.status in ("queued", "running"):
                conn.execute("UPDATE jobs SET status='cancel_requested' WHERE job_id=?", (job_id,))
            conn.commit()
        return self.get(job_id)

    def confirm_cancel(self, job_id: str) -> ExecutionJob:
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            job = self._job(conn.execute("SELECT job_id, idempotency_key, request_sha256, status, outcome_json "
                                         "FROM jobs WHERE job_id=?", (job_id,)).fetchone())
            if job.status != "cancel_requested":
                conn.rollback()
                raise ValueError("cancellation was not requested")
            conn.execute("UPDATE jobs SET status='canceled' WHERE job_id=?", (job_id,))
            conn.commit()
        return self.get(job_id)

    def artifact(self, job_id: str, artifact_id: str) -> bytes:
        require_hash(artifact_id)
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT sha256, size FROM artifacts WHERE artifact_id=? AND job_id=?",
                               (artifact_id, job_id)).fetchone()
        if row is None:
            raise KeyError("artifact not found")
        path = self.artifacts / artifact_id
        if not path.is_file() or path.is_symlink() or path.stat().st_size > 1_048_576:
            raise ValueError("artifact is missing or linked")
        content = path.read_bytes()
        if len(content) != row[1] or digest(content) != row[0]:
            raise ValueError("artifact checksum changed")
        return content


def create_app(database: Path, artifacts: Path, token: str) -> FastAPI:
    """Build an in-process or controlled-network simulation service."""
    if not token:
        raise ValueError("service token is required")
    store = SimulatedJobStore(database, artifacts)
    app = FastAPI(title="DFX synthetic execution protocol", docs_url=None, redoc_url=None)
    app.state.store = store

    def authorize(x_dfx_token: str | None = Header(default=None)) -> None:
        if x_dfx_token is None or not secrets.compare_digest(x_dfx_token, token):
            raise HTTPException(status_code=401, detail="unauthorized")

    @app.get("/v1/capabilities", dependencies=[Depends(authorize)])
    def capabilities() -> Capabilities:
        return Capabilities(available=True, levels=["build", "replay", "behavior", "regression"],
                            max_concurrent_jobs=1)

    @app.post("/v1/jobs", dependencies=[Depends(authorize)])
    def submit(envelope: SubmitEnvelope) -> ExecutionJob:
        try:
            return store.submit(envelope)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/v1/jobs/{job_id}", dependencies=[Depends(authorize)])
    def poll(job_id: str) -> ExecutionJob:
        try:
            return store.get(job_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="job not found") from exc

    @app.post("/v1/jobs/{job_id}/cancel", dependencies=[Depends(authorize)])
    def cancel(job_id: str) -> ExecutionJob:
        try:
            return store.request_cancel(job_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="job not found") from exc

    @app.get("/v1/jobs/{job_id}/artifacts/{artifact_id}", dependencies=[Depends(authorize)])
    def fetch_artifact(job_id: str, artifact_id: str) -> Response:
        try:
            return Response(content=store.artifact(job_id, artifact_id), media_type="application/octet-stream")
        except (KeyError, ValueError) as exc:
            raise HTTPException(status_code=404, detail="artifact unavailable") from exc

    return app
