"""Version-bound HTTP execution protocol and recoverable local submission ledger."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal
from uuid import UUID

import httpx
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .contracts import IDENTIFIER, require_hash
from .task import digest
from .verification import _atomic_json


class ExecutionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    schema_version: Literal[1] = 1
    task_id: str
    candidate_id: str
    runtime_source_sha256: str
    patch_sha256: str
    protocol_sha256: str
    environment_id: str
    initial_state_id: str
    level: Literal["build", "replay", "behavior", "regression"]
    repetition: int = Field(ge=0)
    timeout_seconds: int = Field(ge=1, le=3600)

    @model_validator(mode="after")
    def valid_identity(self) -> ExecutionRequest:
        for value in (self.candidate_id, self.runtime_source_sha256,
                      self.patch_sha256, self.protocol_sha256):
            require_hash(value)
        for value in (self.task_id, self.environment_id, self.initial_state_id):
            if not IDENTIFIER.fullmatch(value):
                raise ValueError("invalid execution identifier")
        return self

    def sha256(self) -> str:
        return digest(json.dumps(self.model_dump(), sort_keys=True, separators=(",", ":")).encode())


def submission_key(run_id: str, request: ExecutionRequest) -> str:
    if not IDENTIFIER.fullmatch(run_id):
        raise ValueError("invalid run identifier")
    return digest(f"remote-v1:{run_id}:{request.sha256()}".encode())


class SubmitEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    run_id: str
    idempotency_key: str
    request: ExecutionRequest

    @model_validator(mode="after")
    def valid_key(self) -> SubmitEnvelope:
        require_hash(self.idempotency_key)
        if self.idempotency_key != submission_key(self.run_id, self.request):
            raise ValueError("idempotency key does not bind request")
        return self


class ArtifactReference(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    artifact_id: str
    job_id: str
    sha256: str
    size: int = Field(ge=0, le=1_048_576)

    @model_validator(mode="after")
    def valid_hashes(self) -> ArtifactReference:
        require_hash(self.artifact_id)
        require_hash(self.sha256)
        UUID(self.job_id)
        return self


class ExecutionOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    scope: Literal["synthetic"] = "synthetic"
    status: Literal["pass", "fail", "inconclusive"]
    detail: str
    artifact: ArtifactReference | None = None
    application_verification: Literal["not_run"] = "not_run"


class ExecutionJob(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    schema_version: Literal[1] = 1
    job_id: str
    idempotency_key: str
    request_sha256: str
    status: Literal["queued", "running", "completed", "failed", "cancel_requested", "canceled", "inconclusive"]
    outcome: ExecutionOutcome | None = None

    @model_validator(mode="after")
    def valid_binding(self) -> ExecutionJob:
        UUID(self.job_id)
        require_hash(self.idempotency_key)
        require_hash(self.request_sha256)
        if self.status in ("completed", "failed", "inconclusive") and self.outcome is None:
            raise ValueError("terminal outcome is missing")
        if self.outcome is not None and self.status not in ("completed", "failed", "inconclusive"):
            raise ValueError("nonterminal job has outcome")
        return self


class Capabilities(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    schema_version: Literal[1] = 1
    available: bool
    mode: Literal["synthetic"] = "synthetic"
    application_available: Literal[False] = False
    levels: list[Literal["build", "replay", "behavior", "regression"]]
    max_concurrent_jobs: int = Field(ge=0)
    reason: str | None = None


class RemoteUncertain(RuntimeError):
    """A request may have reached the server; reconcile before another action."""


class RemoteRejected(RuntimeError):
    """The server gave a definitive client-side rejection."""


class HttpExecutionBackend:
    """HTTPX adapter; the caller owns the client and its socket lifecycle."""

    def __init__(self, client: httpx.Client, base_url: str, token: str, ledger_root: Path):
        if not base_url.startswith(("http://", "https://")) or not token:
            raise ValueError("remote URL and token are required")
        self.client = client
        self.base_url = base_url.rstrip("/")
        self.headers = {"X-DFX-Token": token}
        self.ledger_root = ledger_root
        self.ledger_root.mkdir(parents=True, exist_ok=True)
        if self.ledger_root.is_symlink():
            raise ValueError("remote ledger is linked")

    def _url(self, path: str) -> str:
        return self.base_url + "/v1/" + path

    def _response(self, response: httpx.Response) -> dict:
        if response.status_code >= 500:
            raise RemoteUncertain(f"remote HTTP {response.status_code}")
        if response.status_code >= 400:
            raise RemoteRejected(f"remote HTTP {response.status_code}")
        return response.json()

    def describe(self) -> Capabilities:
        try:
            return Capabilities.model_validate(self._response(self.client.get(self._url("capabilities"), headers=self.headers)))
        except (httpx.RequestError, RemoteUncertain):
            return Capabilities(available=False, levels=[], max_concurrent_jobs=0, reason="remote_unreachable")

    def submit(self, run_id: str, request: ExecutionRequest) -> ExecutionJob:
        key = submission_key(run_id, request)
        path = self.ledger_root / f"{key}.json"
        identity = {"schema_version": 1, "idempotency_key": key,
                    "request_sha256": request.sha256(), "run_id": run_id}
        if path.exists():
            if path.is_symlink():
                raise ValueError("remote intent is linked")
            intent = json.loads(path.read_text(encoding="utf-8"))
            if any(intent.get(field) != value for field, value in identity.items()):
                raise ValueError("remote intent identity changed")
            if intent.get("state") == "submitted":
                return self.poll(intent["job_id"])
            if intent.get("state") != "pending":
                raise ValueError("invalid remote intent state")
        else:
            _atomic_json(path, {**identity, "state": "pending"})
        envelope = SubmitEnvelope(run_id=run_id, idempotency_key=key, request=request)
        try:
            response = self.client.post(self._url("jobs"), headers=self.headers,
                                        json=envelope.model_dump())
            job = ExecutionJob.model_validate(self._response(response))
        except httpx.RequestError as exc:
            raise RemoteUncertain("remote submit response unavailable") from exc
        if job.idempotency_key != key or job.request_sha256 != request.sha256():
            raise ValueError("remote submit returned another request")
        _atomic_json(path, {**identity, "state": "submitted", "job_id": job.job_id})
        return job

    def _known_job(self, job_id: str) -> dict:
        for path in self.ledger_root.glob("*.json"):
            if path.is_symlink():
                raise ValueError("remote intent is linked")
            intent = json.loads(path.read_text(encoding="utf-8"))
            if intent.get("state") == "submitted" and intent.get("job_id") == job_id:
                return intent
        raise ValueError("job is not registered in local ledger")

    def poll(self, job_id: str) -> ExecutionJob:
        intent = self._known_job(job_id)
        try:
            job = ExecutionJob.model_validate(self._response(
                self.client.get(self._url(f"jobs/{job_id}"), headers=self.headers)))
        except httpx.RequestError as exc:
            raise RemoteUncertain("remote poll response unavailable") from exc
        if (job.job_id != job_id or job.idempotency_key != intent["idempotency_key"] or
                job.request_sha256 != intent["request_sha256"]):
            raise ValueError("remote job identity changed")
        return job

    def cancel(self, job_id: str) -> ExecutionJob:
        intent = self._known_job(job_id)
        try:
            job = ExecutionJob.model_validate(self._response(
                self.client.post(self._url(f"jobs/{job_id}/cancel"), headers=self.headers)))
        except httpx.RequestError as exc:
            raise RemoteUncertain("remote cancel response unavailable") from exc
        if (job.job_id != job_id or job.idempotency_key != intent["idempotency_key"] or
                job.request_sha256 != intent["request_sha256"]):
            raise ValueError("remote cancellation belongs to another job")
        return job

    def fetch_artifact(self, reference: ArtifactReference) -> bytes:
        self._known_job(reference.job_id)
        try:
            with self.client.stream("GET", self._url(f"jobs/{reference.job_id}/artifacts/{reference.artifact_id}"),
                                    headers=self.headers) as response:
                if response.status_code >= 500:
                    raise RemoteUncertain(f"remote HTTP {response.status_code}")
                if response.status_code >= 400:
                    raise RemoteRejected(f"remote HTTP {response.status_code}")
                chunks = []
                size = 0
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > reference.size or size > 1_048_576:
                        raise ValueError("remote artifact exceeds declared size")
                    chunks.append(chunk)
        except httpx.RequestError as exc:
            raise RemoteUncertain("remote artifact response unavailable") from exc
        content = b"".join(chunks)
        if len(content) != reference.size or digest(content) != reference.sha256:
            raise ValueError("remote artifact hash or size differs")
        return content
