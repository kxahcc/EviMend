"""Cloud software channel tests use exact MockTransport, never a live API."""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from pydantic import ValidationError

from dfx_agent.context_compaction import CompactionPolicy, CompactionStore
from dfx_agent.model_calls import ModelCallStore, ModelCallUncertain, canonical
from dfx_agent.model_provider import (
    MAX_RESPONSE_BYTES,
    AdapterRequestError,
    AdapterResponseError,
    AdapterTransportError,
    ChatCompletionsAdapter,
    CloudChatCompletionsAdapter,
    CloudProviderConfig,
    PreparedRequest,
    ProviderSettings,
    adapter_from_profile,
)
from dfx_agent.models import AccountedProviderModel, FixedResponseModel, ModelLimit

KEY_NAME = "DFX_CLOUD_TEST_KEY"
SECRET = "fixture-private-credential-do-not-save"
ENDPOINT = "https://provider.invalid/v1/chat/completions"


@pytest.fixture(autouse=True)
def reject_live_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> None:
        pytest.fail("cloud software tests must never construct a real transport")

    monkeypatch.setattr(httpx, "HTTPTransport", forbidden)


def tools() -> list[dict[str, Any]]:
    return [{"type": "function", "function": {"name": "read_source", "parameters": {
        "type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}}]


def response(*, tool: bool = False) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant", "content": "" if tool else "done"}
    if tool:
        message["tool_calls"] = [{"id": "cloud-tool-1", "type": "function", "function": {
            "name": "read_source", "arguments": '{"path":"src/中文.ets"}'}}]
    return {"id": "cloud-message-1", "model": "configured/model", "choices": [{"index": 0,
            "message": message, "finish_reason": "tool_calls" if tool else "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}


def adapter(monkeypatch: pytest.MonkeyPatch,
            handle: Callable[[httpx.Request], httpx.Response] | None = None, **settings: Any
            ) -> CloudChatCompletionsAdapter:
    monkeypatch.setenv(KEY_NAME, SECRET)
    return CloudChatCompletionsAdapter(
        ProviderSettings(provider_id="configured-provider", model_id="configured/model", **settings),
        CloudProviderConfig(endpoint=ENDPOINT, credential_env=KEY_NAME), allow_network=False,
        transport=httpx.MockTransport(handle or (lambda _: httpx.Response(200, json=response()))))


def prepare(model: ChatCompletionsAdapter) -> PreparedRequest:
    return model.prepare([SystemMessage(content="host rules"), HumanMessage(content="定位故障")], tools(),
                         bind_options={"tool_choice": "auto"}, generate_options={"parallel_tool_calls": False})


def invoke(model: CloudChatCompletionsAdapter, request: PreparedRequest) -> AIMessage:
    admitted = model.admission(request, remaining_tokens=1_000_000, remaining_seconds=10.0)
    return model.invoke(request, admission=admitted)


def test_exact_cloud_wire_tools_usage_and_credential_isolation(
        monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    received: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        received.append(request)
        return httpx.Response(200, json=response(tool=True))

    caplog.set_level(logging.DEBUG)
    model = adapter(monkeypatch, handle, max_output_tokens=77, timeout_seconds=30.0)
    request = prepare(model)
    profile_bytes = canonical(model.profile())
    admitted = model.admission(request, remaining_tokens=1_000_000, remaining_seconds=2.0)
    message = model.invoke(request, admission=admitted)
    assert len(received) == 1 and str(received[0].url) == ENDPOINT
    assert received[0].content.decode("utf-8") == request.body
    assert received[0].headers["Authorization"] == "Bearer " + SECRET
    assert all(0 < value <= 2.0 for value in received[0].extensions["timeout"].values())
    assert message.tool_calls[0]["args"] == {"path": "src/中文.ets"}
    assert message.usage_metadata == {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}
    assert message.response_metadata["dfx_provider_usage_raw"]["total_tokens"] == 15
    assert canonical(model.profile()) == profile_bytes
    assert SECRET not in profile_bytes + request.model_dump_json() + admitted.model_dump_json()
    assert SECRET not in message.model_dump_json() + caplog.text
    assert model.profile()["automatic_retries"] == 0
    assert model.profile()["follow_redirects"] is False and model.profile()["trust_env"] is False


@pytest.mark.parametrize("endpoint", [
    "http://provider.invalid/chat/completions", "https://user:password@provider.invalid/v1",
    "https://@provider.invalid/v1", "https://provider.invalid/v1?key=secret", "https://provider.invalid/v1?",
    "https://provider.invalid/v1#fragment", "https://provider.invalid/v1#", "https:///v1", "https://provider.invalid/\n",
])
def test_endpoint_boundary_rejects_unsupported_urls(endpoint: str) -> None:
    with pytest.raises(ValidationError, match="invalid cloud endpoint"):
        CloudProviderConfig(endpoint=endpoint, credential_env=KEY_NAME)


@pytest.mark.parametrize("config", [{"credential_env": "bad name"}, {"credential_env": "KEY\n"},
                                    {"api_key": SECRET}, {"credential": SECRET}])
def test_credentials_cannot_be_literal_config_fields(config: dict[str, Any]) -> None:
    with pytest.raises(ValidationError) as error:
        CloudProviderConfig.model_validate({"endpoint": ENDPOINT, "credential_env": KEY_NAME, **config})
    assert SECRET not in str(error.value)


def test_profile_prepare_validate_and_readonly_reconstruction_never_read_a_key(
        monkeypatch: pytest.MonkeyPatch) -> None:
    model = adapter(monkeypatch)
    lookups: list[str] = []
    original = type(os.environ).get

    def lookup(environment: Any, key: str, default: Any = None) -> Any:
        if key == KEY_NAME:
            lookups.append(key)
        return original(environment, key, default)

    monkeypatch.setattr(type(os.environ), "get", lookup)
    profile = model.profile()
    readonly = adapter_from_profile(profile)
    request = prepare(readonly)
    assert readonly.validate(request) == request and request == prepare(model)
    assert isinstance(readonly, CloudChatCompletionsAdapter)
    assert readonly.profile() == profile and not lookups
    monkeypatch.setattr(httpx, "Client", lambda **_: pytest.fail("readonly must reject before client construction"))
    with pytest.raises(AdapterRequestError, match="provider_readonly"):
        readonly.invoke(request)
    assert not lookups


def test_request_config_and_profile_copies_cannot_mutate_the_frozen_channel(
        monkeypatch: pytest.MonkeyPatch) -> None:
    model = adapter(monkeypatch)
    request = prepare(model)
    profile = model.profile()
    profile["settings"]["max_output_tokens"] = 999
    profile["endpoint"] = "https://changed.invalid/v1"
    request.tools.clear()
    request.messages[0]["content"] = "tampered"
    assert model.profile()["endpoint"] == ENDPOINT and request.messages[0]["content"] == "host rules"
    assert request.tools
    with pytest.raises(ValidationError, match="frozen"):
        model.config.endpoint = "https://changed.invalid/v1"
    changed = request.model_copy(update={"profile_sha256": "a" * 64})
    with pytest.raises(AdapterRequestError, match="prepared_request_changed"):
        model.validate(changed)


def test_missing_key_and_disabled_network_refuse_without_constructing_a_client(
        monkeypatch: pytest.MonkeyPatch) -> None:
    model = adapter(monkeypatch, lambda _: pytest.fail("missing key must not send"))
    request = prepare(model)
    monkeypatch.delenv(KEY_NAME)
    monkeypatch.setattr(httpx, "Client", lambda **_: pytest.fail("must reject before client construction"))
    with pytest.raises(AdapterRequestError, match="provider_credential_missing"):
        model.admission(request, remaining_tokens=1_000_000, remaining_seconds=10.0)
    disabled = CloudChatCompletionsAdapter(model.settings, model.config, allow_network=False)
    with pytest.raises(AdapterRequestError, match="provider_network_disabled"):
        disabled.invoke(prepare(disabled))
    monkeypatch.setenv(KEY_NAME, SECRET)
    with pytest.raises(AdapterRequestError, match="provider_network_disabled"):
        disabled.admission(prepare(disabled), remaining_tokens=1_000_000, remaining_seconds=10.0)


def test_explicit_network_permission_is_strict_and_external_real_transport_is_rejected(
        monkeypatch: pytest.MonkeyPatch) -> None:
    model = adapter(monkeypatch)
    with pytest.raises(AdapterRequestError, match="invalid_network_permission"):
        CloudChatCompletionsAdapter(model.settings, model.config, allow_network=1)  # type: ignore[arg-type]
    with pytest.raises(AdapterRequestError, match="unsupported_cloud_transport"):
        CloudChatCompletionsAdapter(model.settings, model.config, allow_network=True,
                                   transport=object())  # type: ignore[arg-type]
    real = CloudChatCompletionsAdapter(model.settings, model.config, allow_network=True)
    assert real.profile() == model.profile()
    assert prepare(real) == prepare(model)


def test_budget_admission_reserves_input_and_worst_output_before_sending(
        monkeypatch: pytest.MonkeyPatch) -> None:
    model = adapter(monkeypatch, lambda _: pytest.fail("inadmissible requests must not send"), max_output_tokens=99)
    request = prepare(model)
    required = model.reservation_tokens(request)
    assert required > len(request.body.encode("utf-8")) + 99
    with pytest.raises(AdapterRequestError, match="provider_token_budget"):
        model.admission(request, remaining_tokens=required - 1, remaining_seconds=1.0)
    with pytest.raises(AdapterRequestError, match="provider_time_budget"):
        model.admission(request, remaining_tokens=required, remaining_seconds=0.0)
    admitted = model.admission(request, remaining_tokens=required, remaining_seconds=1.0)
    assert admitted.reserved_tokens == required
    with pytest.raises(AdapterRequestError, match="provider_budget_required"):
        model.invoke(request)
    with pytest.raises(AdapterRequestError, match="provider_admission_changed"):
        model.invoke(request, admission=admitted.model_copy(update={"body_sha256": "a" * 64}))
    with pytest.raises(AdapterRequestError, match="provider_time_budget"):
        model.invoke(request, admission=admitted.model_copy(update={"deadline": time.monotonic() - 1.0}))


@pytest.mark.parametrize("kind,reason", [("timeout", "provider_timeout"), ("transport", "provider_transport_error"),
    ("redirect", "provider_http_error"), ("http", "provider_http_error"), ("json", "invalid_response_json"),
    ("format", "invalid_response_choices"), ("limit", "response_byte_limit"),
    ("credential", "provider_credential_exposed"), ("escaped-credential", "provider_credential_exposed")])
def test_failures_are_bounded_safe_and_never_retried(monkeypatch: pytest.MonkeyPatch, kind: str, reason: str) -> None:
    calls = 0

    def handle(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if kind == "timeout":
            raise httpx.ReadTimeout(SECRET)
        if kind == "transport":
            raise RuntimeError(SECRET)
        if kind in ("redirect", "http"):
            return httpx.Response(302 if kind == "redirect" else 429, text=SECRET,
                                  headers={"Location": "https://other.invalid/?key=" + SECRET})
        if kind == "limit":
            return httpx.Response(200, content=b"x" * (MAX_RESPONSE_BYTES + 1))
        if kind == "json":
            return httpx.Response(200, content=b"{broken")
        body = response()
        if kind == "format":
            body["choices"] = []
        else:
            body["choices"][0]["message"]["content"] = SECRET
        if kind == "escaped-credential":
            encoded = json.dumps(body).replace(SECRET, "".join(f"\\u{ord(character):04x}" for character in SECRET))
            return httpx.Response(200, content=encoded)
        return httpx.Response(200, json=body)

    model = adapter(monkeypatch, handle)
    with pytest.raises((AdapterTransportError, AdapterResponseError), match=reason) as failure:
        invoke(model, prepare(model))
    assert calls == 1 and str(failure.value) == reason and SECRET not in str(failure.value)


def test_streamed_response_deadline_stops_between_chunks(monkeypatch: pytest.MonkeyPatch) -> None:
    class Chunks(httpx.SyncByteStream):
        def __iter__(self) -> Iterator[bytes]:
            yield b"{"
            yield b"}"

    ticks = iter([10.0, 10.0, 10.2, 11.1])
    monkeypatch.setattr("dfx_agent.model_provider.time", SimpleNamespace(monotonic=lambda: next(ticks)))
    model = adapter(monkeypatch, lambda _: httpx.Response(200, stream=Chunks()))
    request = prepare(model)
    admitted = model.admission(request, remaining_tokens=1_000_000, remaining_seconds=1.0)
    with pytest.raises(AdapterTransportError, match="provider_timeout"):
        model.invoke(request, admission=admitted)


def test_sdk_tool_binding_invokes_cloud_channel_and_saves_usage_without_key(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json=response(tool=True))

    cloud = adapter(monkeypatch, handle)
    ledger = ModelCallStore(tmp_path / "model-calls.sqlite")
    sdk = AccountedProviderModel(responses=[], adapter=cloud, call_store=ledger, task_id="task1", run_id="run1",
                                max_calls=2, max_known_tokens=100_000, deadline=time.monotonic() + 10.0)
    sdk.bind_tools(tools(), tool_choice="auto")
    message = sdk.invoke([HumanMessage(content="read source")])
    assert isinstance(message, AIMessage) and message.tool_calls[0]["name"] == "read_source"
    assert len(calls) == 1 and json.loads(calls[0].content)["tools"] == tools()
    record = ledger.get("model-1")
    assert record is not None and record.state == "completed" and record.observation is not None
    assert record.observation.usage is not None and record.observation.usage.total_tokens == 15
    assert SECRET not in record.intent.model_dump_json() + record.observation.model_dump_json()
    replay = AccountedProviderModel(responses=[], adapter=cloud, call_store=ledger, task_id="task1", run_id="run1",
                                   max_calls=2, max_known_tokens=100_000, deadline=time.monotonic() + 10.0)
    replay.bind_tools(tools(), tool_choice="auto")
    monkeypatch.delenv(KEY_NAME)
    assert replay.invoke([HumanMessage(content="read source")]) == message and len(calls) == 1


def test_sdk_thinking_keeps_all_reasoning_across_tools_final_answer_and_saved_replay(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    requests: list[dict[str, Any]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        index = len(requests)
        body = response(tool=index <= 2)
        body["id"] = f"thinking-message-{index}"
        body["choices"][0]["message"]["reasoning_content"] = f"reasoning-{index}"
        if index <= 2:
            body["choices"][0]["message"]["tool_calls"][0]["id"] = f"thinking-tool-{index}"
        return httpx.Response(200, json=body)

    cloud = adapter(monkeypatch, handle, thinking_mode="enabled", reasoning_effort="high")
    ledger = ModelCallStore(tmp_path / "model-calls.sqlite")
    sdk = AccountedProviderModel(responses=[], adapter=cloud, call_store=ledger, task_id="task1", run_id="run1",
                                max_calls=4, max_known_tokens=100_000, deadline=time.monotonic() + 10.0)
    sdk.bind_tools(tools(), tool_choice="auto")
    history: list[BaseMessage] = [HumanMessage(content="inspect source")]
    for index in (1, 2):
        message = sdk.invoke(history)
        assert isinstance(message, AIMessage) and message.tool_calls[0]["id"] == f"thinking-tool-{index}"
        history.extend([message, ToolMessage(content=f"source-{index}", tool_call_id=f"thinking-tool-{index}")])
    final = sdk.invoke(history)
    assert isinstance(final, AIMessage) and not final.tool_calls
    history.extend([final, HumanMessage(content="another question")])
    next_answer = sdk.invoke(history)
    assert len(requests) == 4
    assistants = [item for item in requests[-1]["messages"] if item["role"] == "assistant"]
    assert [item["reasoning_content"] for item in assistants] == [f"reasoning-{index}" for index in (1, 2, 3)]
    assert requests[-1]["thinking"] == {"type": "enabled"} and requests[-1]["reasoning_effort"] == "high"
    assert [item["tool_call_id"] for item in requests[-1]["messages"] if item["role"] == "tool"] == [
        "thinking-tool-1", "thinking-tool-2"]
    replay = AccountedProviderModel(responses=[], adapter=cloud, call_store=ledger, task_id="task1", run_id="run1",
                                   max_calls=4, max_known_tokens=100_000, deadline=time.monotonic() + 10.0,
                                   turn_calls=3)
    replay.bind_tools(tools(), tool_choice="auto")
    monkeypatch.delenv(KEY_NAME)
    assert replay.invoke(history) == next_answer and len(requests) == 4


def test_thinking_missing_historical_reasoning_refuses_before_reservation(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cloud = adapter(monkeypatch, lambda _: pytest.fail("missing reasoning must not send"), thinking_mode="enabled")
    ledger = ModelCallStore(tmp_path / "model-calls.sqlite")
    sdk = AccountedProviderModel(responses=[], adapter=cloud, call_store=ledger, task_id="task1", run_id="run1",
                                max_calls=2, max_known_tokens=100_000, deadline=time.monotonic() + 10.0)
    sdk.bind_tools(tools(), tool_choice="auto")
    with pytest.raises(ModelLimit, match="provider_request_unsupported"):
        sdk.invoke([HumanMessage(content="task"), AIMessage(content="old answer"), HumanMessage(content="continue")])
    assert sdk.request_error_reason == "thinking_history_requires_reasoning" and ledger.get("model-1") is None


def test_thinking_rejects_summary_before_any_auxiliary_send(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cloud = adapter(monkeypatch, lambda _: pytest.fail("thinking compaction must not send"), thinking_mode="enabled")
    ledger = ModelCallStore(tmp_path / "model-calls.sqlite")
    compactor = CompactionStore(tmp_path, "task1", "run1", CompactionPolicy(enabled=True), cloud.profile())
    sdk = AccountedProviderModel(responses=[], adapter=cloud, call_store=ledger, task_id="task1", run_id="run1",
                                max_calls=2, max_known_tokens=100_000, deadline=time.monotonic() + 10.0,
                                compactor=compactor)
    sdk.bind_tools(tools(), tool_choice="auto")
    with pytest.raises(ModelLimit, match="thinking_compaction_unsupported"):
        sdk.invoke([HumanMessage(content="task")])
    assert ledger.records("task1", "run1") == []


def test_thinking_does_not_admit_a_view_that_removed_an_old_complete_turn(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cloud = adapter(monkeypatch, lambda _: pytest.fail("thinking history deletion must not send"), thinking_mode="enabled")
    ledger = ModelCallStore(tmp_path / "model-calls.sqlite")
    sdk = AccountedProviderModel(responses=[], adapter=cloud, call_store=ledger, task_id="task1", run_id="run1",
                                max_calls=2, max_known_tokens=100_000, deadline=time.monotonic() + 10.0)
    sdk.bind_tools(tools(), tool_choice="auto")
    monkeypatch.setattr(FixedResponseModel, "_view", lambda self, messages: [messages[0], messages[-1]])
    history = [HumanMessage(content="task"), AIMessage(content="old", additional_kwargs={"reasoning_content": "old reason"}),
               AIMessage(content="recent", additional_kwargs={"reasoning_content": "recent reason"})]
    with pytest.raises(ModelLimit, match="thinking_history_selection_unsupported"):
        sdk.invoke(history)
    assert ledger.get("model-1") is None


def test_sdk_dispatched_timeout_remains_unknown_and_cannot_blindly_retry(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0

    def handle(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout(SECRET)

    cloud = adapter(monkeypatch, handle)
    ledger = ModelCallStore(tmp_path / "model-calls.sqlite")
    for _ in range(2):
        sdk = AccountedProviderModel(responses=[], adapter=cloud, call_store=ledger, task_id="task1", run_id="run1",
                                    max_calls=2, max_known_tokens=100_000, deadline=time.monotonic() + 10.0)
        with pytest.raises(ModelCallUncertain):
            sdk.invoke([HumanMessage(content="read source")])
    assert calls == 1
    record = ledger.get("model-1")
    assert record is not None and record.state == "unknown" and record.observation is None


def test_sdk_budget_refusal_precedes_ledger_reservation_and_wire_send(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cloud = adapter(monkeypatch, lambda _: pytest.fail("budget refusal must not send"))
    ledger = ModelCallStore(tmp_path / "model-calls.sqlite")
    sdk = AccountedProviderModel(responses=[], adapter=cloud, call_store=ledger, task_id="task1", run_id="run1",
                                max_calls=2, max_known_tokens=1, deadline=time.monotonic() + 10.0)
    with pytest.raises(ModelLimit):
        sdk.invoke([HumanMessage(content="read source")])
    assert sdk.request_error_reason == "provider_token_budget"
    assert ledger.get("model-1") is None and sdk.calls == 0


@pytest.mark.parametrize("usage", [None, {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 999}])
def test_sdk_unknown_usage_retains_response_and_stops_new_requests(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, usage: Any) -> None:
    calls = 0

    def handle(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        body = response()
        body["usage"] = usage
        return httpx.Response(200, json=body)

    cloud = adapter(monkeypatch, handle)
    ledger = ModelCallStore(tmp_path / "model-calls.sqlite")
    sdk = AccountedProviderModel(responses=[], adapter=cloud, call_store=ledger, task_id="task1", run_id="run1",
                                max_calls=3, max_known_tokens=100_000, deadline=time.monotonic() + 10.0)
    message = sdk.invoke([HumanMessage(content="first request")])
    assert message.content == "done"
    record = ledger.get("model-1")
    assert record is not None and record.state == "completed" and record.observation is not None
    assert record.observation.usage_status == ("missing" if usage is None else "invalid")
    with pytest.raises(ModelLimit, match="provider_usage_unknown"):
        sdk.invoke([HumanMessage(content="next request")])
    assert calls == 1 and ledger.get("model-2") is None


def test_compaction_rebuilds_cloud_summary_request_without_environment_or_network(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cloud = adapter(monkeypatch)
    monkeypatch.delenv(KEY_NAME)
    monkeypatch.setattr(httpx, "Client", lambda **_: pytest.fail("compaction preparation must not send"))
    store = CompactionStore(tmp_path, "task1", "run1",
                            CompactionPolicy(enabled=True, trigger_bytes=128, retain_groups=1), cloud.profile())
    messages = [SystemMessage(content="policy"), HumanMessage(content="task"),
                AIMessage(content="old failed hypothesis " * 100), AIMessage(content="keep recent")]
    plan = store.select(messages, "model-2")
    assert plan is not None
    request = plan.summary_request()
    assert request is not None and request.tools == []
    assert request == cloud.prepare(plan.summary_messages(), [])
    assert plan.summary_intent().provider_id == cloud.settings.provider_id


def test_mock_profile_roundtrip_keeps_its_original_shape_and_readonly_never_dispatches(
        monkeypatch: pytest.MonkeyPatch) -> None:
    mock = ChatCompletionsAdapter(ProviderSettings(provider_id="mock", model_id="model"),
                                 httpx.MockTransport(lambda _: pytest.fail("mock profile must not send")))
    profile = mock.profile()
    assert profile["transport"] == "mock-only" and "credential_env" not in profile
    readonly = adapter_from_profile(profile)
    assert type(readonly) is ChatCompletionsAdapter and readonly.profile() == profile
    request = prepare(readonly)
    monkeypatch.setattr(httpx, "Client", lambda **_: pytest.fail("readonly mock must not construct a client"))
    with pytest.raises(AdapterRequestError, match="provider_readonly"):
        readonly.invoke(request)
    profile["automatic_retries"] = 1
    with pytest.raises(AdapterRequestError, match="unsupported_model_profile"):
        adapter_from_profile(profile)
