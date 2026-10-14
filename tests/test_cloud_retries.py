"""Offline admission/retry coverage through the official asynchronous SDK."""

from __future__ import annotations

import gzip
import json
import logging
import ssl
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import anyio
import httpx
import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from openai import AsyncOpenAI
from openai.resources.chat.completions import AsyncCompletions

from dfx_agent.cloud_retries import CloudRetryPolicy
from dfx_agent.model_calls import ModelCallStore, ModelCallUncertain, canonical
from dfx_agent.model_provider import (
    MAX_RESPONSE_BYTES,
    AdapterError,
    AdapterRequestError,
    AdapterResponseError,
    AdapterTransportError,
    CloudChatCompletionsAdapter,
    CloudProviderConfig,
    PreparedRequest,
    ProviderSettings,
    adapter_from_profile,
)
from dfx_agent.models import AccountedProviderModel

KEY_NAME = "DFX_RETRY_TEST_KEY"
SECRET = "retry-test-private-key-must-not-be-saved"
ENDPOINT = "https://provider.invalid/v1/chat/completions"


@pytest.fixture(autouse=True)
def reject_live_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> None:
        pytest.fail("retry tests must use exact MockTransport, never a network transport")

    monkeypatch.setattr(httpx, "HTTPTransport", forbidden)
    monkeypatch.setattr(httpx, "AsyncHTTPTransport", forbidden)


@pytest.fixture
def instant_backoff(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    waits: list[float] = []

    async def sleep(delay: float, *args: Any, **kwargs: Any) -> None:
        waits.append(delay)

    monkeypatch.setattr(anyio, "sleep", sleep)
    return waits


def response() -> dict[str, Any]:
    return {
        "id": "retry-message", "model": "configured/model",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "done"},
                     "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }


def adapter(monkeypatch: pytest.MonkeyPatch,
            handle: Callable[[httpx.Request], httpx.Response], *,
            policy: CloudRetryPolicy | None = None) -> CloudChatCompletionsAdapter:
    monkeypatch.setenv(KEY_NAME, SECRET)
    return CloudChatCompletionsAdapter(
        ProviderSettings(provider_id="configured-provider", model_id="configured/model"),
        CloudProviderConfig(endpoint=ENDPOINT, credential_env=KEY_NAME,
                            network=policy or CloudRetryPolicy()),
        transport=httpx.MockTransport(handle))


def prepare(cloud: CloudChatCompletionsAdapter) -> PreparedRequest:
    return cloud.prepare([SystemMessage(content="host rules"), HumanMessage(content="定位故障")], [])


def invoke(cloud: CloudChatCompletionsAdapter, *, seconds: float = 10.0) -> AIMessage:
    request = prepare(cloud)
    admitted = cloud.admission(request, remaining_tokens=1_000_000, remaining_seconds=seconds)
    return cloud.invoke(request, admission=admitted)


@pytest.mark.parametrize("failure", [httpx.ConnectError, httpx.ConnectTimeout])
def test_safe_connection_failure_retries_same_frozen_request_with_sdk_backoff(
        monkeypatch: pytest.MonkeyPatch, instant_backoff: list[float], failure: type[httpx.TransportError]) -> None:
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if len(seen) == 1:
            raise failure(SECRET, request=request)
        return httpx.Response(200, json=response())

    cloud = adapter(monkeypatch, handle)
    message = invoke(cloud)
    assert message.content == "done" and len(seen) == 2
    assert canonical(json.loads(seen[0].content)) == canonical(json.loads(seen[1].content))
    assert seen[0].headers["Authorization"] == seen[1].headers["Authorization"] == "Bearer " + SECRET
    assert [request.headers["x-stainless-retry-count"] for request in seen] == ["0", "1"]
    assert len(instant_backoff) == 1 and instant_backoff[0] > 0
    network = message.response_metadata["dfx_network"]
    assert network["network_attempts"] == 2 and network["network_retries"] == 1
    assert SECRET not in canonical(cloud.profile()) + message.model_dump_json()


def test_rate_limit_uses_sdk_retry_after_without_saving_error_body_or_key(
        monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
        instant_backoff: list[float]) -> None:
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if len(seen) == 1:
            return httpx.Response(429, json={"error": {"message": SECRET}},
                                  headers={"Retry-After": "0.01"})
        return httpx.Response(200, json=response())

    caplog.set_level(logging.DEBUG)
    cloud = adapter(monkeypatch, handle)
    message = invoke(cloud)
    assert len(seen) == 2 and instant_backoff == [0.01]
    assert message.usage_metadata == {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}
    assert message.response_metadata["dfx_network"]["network_attempts"] == 2
    assert SECRET not in message.model_dump_json() + canonical(cloud.profile()) + caplog.text


def test_rate_limit_respects_explicit_no_retry_header(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0

    def handle(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(429, text=SECRET, headers={"x-should-retry": "false", "Retry-After": "60"})

    cloud = adapter(monkeypatch, handle, policy=CloudRetryPolicy(max_retries=5))
    with pytest.raises(AdapterTransportError) as caught:
        invoke(cloud)
    assert calls == 1 and SECRET not in str(caught.value)


def test_invalid_tls_certificate_is_permanent_even_when_wrapped_in_connect_error(
        monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ConnectError(SECRET, request=request) from ssl.SSLCertVerificationError("certificate invalid")

    cloud = adapter(monkeypatch, handle, policy=CloudRetryPolicy(max_retries=5))
    with pytest.raises(AdapterTransportError) as caught:
        invoke(cloud)
    assert calls == 1 and SECRET not in str(caught.value)


@pytest.mark.parametrize("max_retries", [0, 1, 2, 5])
def test_sdk_retry_limit_is_configured_and_counts_additional_sends(
        monkeypatch: pytest.MonkeyPatch, instant_backoff: list[float], max_retries: int) -> None:
    calls = 0
    sdk_retry_limits: list[int] = []
    original = AsyncOpenAI.__init__

    def sdk_init(self: Any, *args: Any, **kwargs: Any) -> None:
        sdk_retry_limits.append(kwargs["max_retries"])
        original(self, *args, **kwargs)

    monkeypatch.setattr(AsyncOpenAI, "__init__", sdk_init)

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ConnectError(SECRET, request=request)

    cloud = adapter(monkeypatch, handle, policy=CloudRetryPolicy(max_retries=max_retries))
    with pytest.raises(AdapterTransportError) as caught:
        invoke(cloud)
    assert calls == max_retries + 1
    assert len(instant_backoff) == max_retries and sdk_retry_limits == [max_retries]
    assert SECRET not in str(caught.value)


@pytest.mark.parametrize("status", [400, 401, 403, 408, 409, 500, 502, 503])
@pytest.mark.parametrize("override", [None, "true"])
def test_unsafe_or_permanent_http_failures_do_not_retry_even_if_vendor_header_requests_it(
        monkeypatch: pytest.MonkeyPatch, status: int, override: str | None) -> None:
    calls = 0

    def handle(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        headers = {"x-should-retry": override} if override is not None else {}
        return httpx.Response(status, text=SECRET, headers=headers)

    cloud = adapter(monkeypatch, handle, policy=CloudRetryPolicy(max_retries=5))
    with pytest.raises(AdapterTransportError) as caught:
        invoke(cloud)
    assert calls == 1 and SECRET not in str(caught.value)


@pytest.mark.parametrize("failure", [httpx.ReadTimeout, httpx.WriteTimeout,
                                    httpx.ReadError, httpx.WriteError, httpx.RemoteProtocolError])
def test_unknown_result_transport_failure_is_not_repeated_on_ledger_recovery(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: type[httpx.TransportError]) -> None:
    calls = 0

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise failure(SECRET, request=request)

    cloud = adapter(monkeypatch, handle, policy=CloudRetryPolicy(max_retries=5))
    ledger = ModelCallStore(tmp_path / "model-calls.sqlite")
    for _ in range(2):
        model = AccountedProviderModel(
            responses=[], adapter=cloud, call_store=ledger, task_id="task1", run_id="run1",
            max_calls=2, max_known_tokens=100_000, deadline=time.monotonic() + 10.0)
        with pytest.raises(ModelCallUncertain):
            model.invoke([HumanMessage(content="inspect failure")])
    assert calls == 1
    record = ledger.get("model-1")
    assert record is not None and record.state == "unknown" and record.observation is None
    assert SECRET not in record.model_dump_json()


def test_retry_after_cannot_extend_admitted_task_deadline_or_dispatch_a_second_request(
        monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0

    def handle(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(429, json={"error": {"message": "rate limited"}},
                              headers={"Retry-After": "60"})

    cloud = adapter(monkeypatch, handle)
    with pytest.raises(AdapterTransportError, match="provider_timeout"):
        invoke(cloud, seconds=0.05)
    assert calls == 1


@pytest.mark.parametrize("kind", ["encoding", "bytes", "json", "json_scalar", "json_identity", "credential"])
def test_success_response_validation_failures_do_not_trigger_another_generation(
        monkeypatch: pytest.MonkeyPatch, kind: str) -> None:
    calls = 0

    def handle(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if kind == "encoding":
            return httpx.Response(200, content=gzip.compress(b"{}"), headers={"Content-Encoding": "gzip"})
        if kind == "bytes":
            return httpx.Response(200, content=b"x" * (MAX_RESPONSE_BYTES + 1))
        if kind == "json":
            return httpx.Response(200, content=b"{broken")
        if kind == "json_scalar":
            return httpx.Response(200, content=b"[]")
        if kind == "json_identity":
            return httpx.Response(200, content=b"{}")
        body = response()
        body["choices"][0]["message"]["content"] = SECRET
        return httpx.Response(200, json=body)

    cloud = adapter(monkeypatch, handle, policy=CloudRetryPolicy(max_retries=5))
    with pytest.raises(AdapterResponseError) as caught:
        invoke(cloud)
    assert calls == 1 and SECRET not in str(caught.value)


def test_sdk_request_mutation_is_rejected_before_dispatch_without_retries(
        monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0
    original = AsyncCompletions.create

    async def changed_create(self: Any, **kwargs: Any) -> Any:
        return await original(self, **{**kwargs, "model": "tampered-model"})

    monkeypatch.setattr(AsyncCompletions, "create", changed_create)

    def handle(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        pytest.fail("a changed SDK request must not reach transport")

    cloud = adapter(monkeypatch, handle)
    with pytest.raises(AdapterError, match="sdk_prepared_request_changed"):
        invoke(cloud)
    assert calls == 0


def test_retry_observation_is_persisted_with_one_logical_call_and_replayed_without_new_send(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, instant_backoff: list[float]) -> None:
    calls = 0

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ConnectError(SECRET, request=request)
        return httpx.Response(200, json=response())

    cloud = adapter(monkeypatch, handle)
    ledger = ModelCallStore(tmp_path / "model-calls.sqlite")
    model = AccountedProviderModel(
        responses=[], adapter=cloud, call_store=ledger, task_id="task1", run_id="run1",
        max_calls=1, max_known_tokens=100_000, deadline=time.monotonic() + 10.0)
    message = model.invoke([HumanMessage(content="inspect failure")])
    assert isinstance(message, AIMessage) and calls == 2
    assert message.response_metadata["dfx_network"]["network_attempts"] == 2
    assert message.response_metadata["dfx_network"]["network_retries"] == 1
    records = ledger.records("task1", "run1")
    assert len(records) == 1 and records[0].state == "completed"
    saved, observation = ledger.replay(records[0].intent)
    assert saved == message and saved.response_metadata["dfx_network"] == message.response_metadata["dfx_network"]
    assert observation.usage is not None and observation.usage.total_tokens == 15
    replay = AccountedProviderModel(
        responses=[], adapter=cloud, call_store=ledger, task_id="task1", run_id="run1",
        max_calls=1, max_known_tokens=100_000, deadline=time.monotonic() + 10.0)
    monkeypatch.delenv(KEY_NAME)
    assert replay.invoke([HumanMessage(content="inspect failure")]) == message and calls == 2
    assert SECRET not in saved.model_dump_json() + records[0].model_dump_json()


def test_configured_async_policy_profile_restores_readonly_without_credentials(
        monkeypatch: pytest.MonkeyPatch) -> None:
    policy = CloudRetryPolicy(max_retries=5, request_timeout_seconds=30.0, connect_timeout_seconds=3.0)
    cloud = adapter(monkeypatch, lambda _: pytest.fail("profile operations must not dispatch"), policy=policy)
    profile = cloud.profile()
    assert profile["network"] == policy.model_dump(mode="json")
    assert profile["sdk"]["api"] == "chat.completions.async"
    monkeypatch.delenv(KEY_NAME)
    readonly = adapter_from_profile(profile)
    assert readonly.profile() == profile
    assert readonly.validate(prepare(readonly)) == prepare(cloud)
    with pytest.raises(AdapterRequestError, match="provider_readonly"):
        readonly.invoke(prepare(readonly))


def test_legacy_sdk_profile_remains_zero_retry_and_keeps_original_identity(
        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(KEY_NAME, SECRET)
    cloud = CloudChatCompletionsAdapter(
        ProviderSettings(provider_id="legacy-provider", model_id="configured/model"),
        CloudProviderConfig(endpoint=ENDPOINT, credential_env=KEY_NAME),
        transport=httpx.MockTransport(lambda _: pytest.fail("legacy profile verification must not dispatch")))
    profile = cloud.profile()
    assert "network" not in profile and profile["automatic_retries"] == 0
    assert profile["sdk"]["api"] == "chat.completions"
    assert adapter_from_profile(profile).profile() == profile


@pytest.mark.parametrize("headers", [{"Retry-After": "120"}, {"retry-after-ms": "120000"}])
def test_long_server_wait_is_not_shortened_to_sdk_default_backoff(
        monkeypatch: pytest.MonkeyPatch, instant_backoff: list[float], headers: dict[str, str]) -> None:
    calls = 0

    def handle(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(429, headers=headers, content=b"private rate limit error body")

    cloud = adapter(monkeypatch, handle)
    with pytest.raises(AdapterTransportError, match="provider_http_error"):
        invoke(cloud)
    assert calls == 1 and instant_backoff == []
