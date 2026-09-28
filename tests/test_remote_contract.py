"""Synthetic HTTP protocol tests; TestClient creates no background service."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from dfx_agent.backends import (
    ExecutionRequest,
    HttpExecutionBackend,
    RemoteRejected,
    RemoteUncertain,
    SubmitEnvelope,
    submission_key,
)
from dfx_agent.remote_service import create_app


def request(repetition: int = 0) -> ExecutionRequest:
    return ExecutionRequest(task_id="task1", candidate_id="a" * 64,
                            runtime_source_sha256="b" * 64, patch_sha256="c" * 64,
                            protocol_sha256="d" * 64, environment_id="env1",
                            initial_state_id="clean", level="build", repetition=repetition,
                            timeout_seconds=30)


def backend(wire: httpx.Client, root: Path) -> HttpExecutionBackend:
    return HttpExecutionBackend(wire, "http://testserver", "test-secret", root / "local-ledger")


def test_duplicate_submit_and_poll_do_not_create_jobs(tmp_path: Path) -> None:
    app = create_app(tmp_path / "server/jobs.sqlite", tmp_path / "server/artifacts", "test-secret")
    with TestClient(app) as wire:
        remote = backend(wire, tmp_path)
        assert remote.describe().available
        first = remote.submit("run1", request())
        assert first.status == "queued"
        assert remote.submit("run1", request()).job_id == first.job_id
        envelope = SubmitEnvelope(run_id="run1", idempotency_key=submission_key("run1", request()), request=request())
        assert wire.post("/v1/jobs", json=envelope.model_dump(), headers={"X-DFX-Token": "test-secret"}).json()["job_id"] == first.job_id
        for _ in range(3):
            assert remote.poll(first.job_id).status == "queued"
        assert app.state.store.count_jobs() == 1
        second = remote.submit("run1", request(repetition=1))
        assert second.job_id != first.job_id and app.state.store.count_jobs() == 2


class DropFirstSubmit:
    def __init__(self, wire: TestClient):
        self.wire = wire
        self.dropped = False

    def post(self, url: str, **kwargs: object) -> httpx.Response:
        response = self.wire.post(url, **kwargs)
        if url.endswith("/jobs") and not self.dropped:
            self.dropped = True
            raise httpx.ReadTimeout("lost after acceptance", request=httpx.Request("POST", url))
        return response

    def get(self, url: str, **kwargs: object) -> httpx.Response:
        return self.wire.get(url, **kwargs)


def test_response_lost_after_acceptance_reuses_same_key(tmp_path: Path) -> None:
    database, artifacts = tmp_path / "server/jobs.sqlite", tmp_path / "server/artifacts"
    app = create_app(database, artifacts, "test-secret")
    with TestClient(app) as wire:
        dropped = DropFirstSubmit(wire)
        remote = backend(dropped, tmp_path)  # type: ignore[arg-type]
        with pytest.raises(RemoteUncertain, match="submit response"):
            remote.submit("run1", request())
        assert app.state.store.count_jobs() == 1
        ledger = next((tmp_path / "local-ledger").glob("*.json"))
        assert json.loads(ledger.read_text())["state"] == "pending"
    reopened_app = create_app(database, artifacts, "test-secret")
    with TestClient(reopened_app) as wire:
        recovered = backend(wire, tmp_path).submit("run1", request())
        assert recovered.status == "queued"
        assert reopened_app.state.store.count_jobs() == 1
        assert json.loads(ledger.read_text())["job_id"] == recovered.job_id


def test_restart_marks_unobserved_running_job_inconclusive(tmp_path: Path) -> None:
    database, artifacts = tmp_path / "server/jobs.sqlite", tmp_path / "server/artifacts"
    first_app = create_app(database, artifacts, "test-secret")
    with TestClient(first_app) as wire:
        first = backend(wire, tmp_path).submit("run1", request())
        first_app.state.store.mark_running(first.job_id)
    reopened_app = create_app(database, artifacts, "test-secret")
    with TestClient(reopened_app) as wire:
        status = backend(wire, tmp_path).poll(first.job_id)
        assert status.status == "inconclusive"
        assert status.outcome is not None and status.outcome.status == "inconclusive"
        assert status.outcome.application_verification == "not_run"
        assert reopened_app.state.store.count_jobs() == 1


def test_cancel_request_is_not_confirmation(tmp_path: Path) -> None:
    app = create_app(tmp_path / "server/jobs.sqlite", tmp_path / "server/artifacts", "test-secret")
    with TestClient(app) as wire:
        remote = backend(wire, tmp_path)
        job = remote.submit("run1", request())
        assert remote.cancel(job.job_id).status == "cancel_requested"
        assert remote.poll(job.job_id).status == "cancel_requested"
        assert app.state.store.confirm_cancel(job.job_id).status == "canceled"
        assert remote.poll(job.job_id).status == "canceled"


def test_artifact_hash_is_checked_by_service_and_client(tmp_path: Path) -> None:
    app = create_app(tmp_path / "server/jobs.sqlite", tmp_path / "server/artifacts", "test-secret")
    with TestClient(app) as wire:
        remote = backend(wire, tmp_path)
        job = remote.submit("run1", request())
        app.state.store.mark_running(job.job_id)
        app.state.store.finish(job.job_id, passed=True, detail="synthetic pass", artifact_bytes=b"synthetic log")
        completed = remote.poll(job.job_id)
        assert completed.status == "completed" and completed.outcome is not None
        assert completed.outcome.scope == "synthetic"
        assert completed.outcome.application_verification == "not_run"
        reference = completed.outcome.artifact
        assert reference is not None
        assert remote.fetch_artifact(reference) == b"synthetic log"
        with pytest.raises(ValueError, match="hash or size"):
            remote.fetch_artifact(reference.model_copy(update={"sha256": "0" * 64}))
        other = remote.submit("run1", request(repetition=1))
        with pytest.raises(RemoteRejected, match="404"):
            remote.fetch_artifact(reference.model_copy(update={"job_id": other.job_id}))
        (tmp_path / "server/artifacts" / reference.artifact_id).write_bytes(b"tampered")
        with pytest.raises(RemoteRejected, match="404"):
            remote.fetch_artifact(reference)
        assert app.state.store.count_jobs() == 2


def test_offline_and_auth_rejection_preserve_unknown(tmp_path: Path) -> None:
    def disconnected(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline", request=req)

    with httpx.Client(transport=httpx.MockTransport(disconnected)) as wire:
        remote = backend(wire, tmp_path)
        assert remote.describe().available is False
        with pytest.raises(RemoteUncertain, match="submit response"):
            remote.submit("run1", request())
        assert json.loads(next((tmp_path / "local-ledger").glob("*.json")).read_text())["state"] == "pending"
    app = create_app(tmp_path / "server/jobs.sqlite", tmp_path / "server/artifacts", "test-secret")
    with TestClient(app) as wire:
        unauthenticated = HttpExecutionBackend(wire, "http://testserver", "wrong", tmp_path / "wrong-ledger")
        with pytest.raises(RemoteRejected, match="401"):
            unauthenticated.submit("run1", request())
        assert app.state.store.count_jobs() == 0


def test_request_identity_and_server_key_are_strict(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="SHA-256"):
        ExecutionRequest.model_validate({**request().model_dump(), "candidate_id": "short"})
    app = create_app(tmp_path / "server/jobs.sqlite", tmp_path / "server/artifacts", "test-secret")
    with TestClient(app) as wire:
        payload = {"run_id": "run1", "idempotency_key": "0" * 64, "request": request().model_dump()}
        response = wire.post("/v1/jobs", json=payload, headers={"X-DFX-Token": "test-secret"})
        assert response.status_code == 422
        assert app.state.store.count_jobs() == 0
