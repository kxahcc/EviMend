"""Finite mock-provider wire tests; no SDK reasoning or cloud capability is measured."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    BaseMessage,
    ChatMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from pydantic import ValidationError

from dfx_agent.model_calls import ModelCallIntent, canonical, normalize_message, sha256
from dfx_agent.model_provider import (
    MAX_REQUEST_BYTES,
    MAX_RESPONSE_BYTES,
    MOCK_ENDPOINT,
    AdapterRequestError,
    AdapterResponseError,
    AdapterTransportError,
    ChatCompletionsAdapter,
    PreparedRequest,
    ProviderSettings,
    adapter_from_profile,
)


def descriptors() -> list[dict[str, Any]]:
    return [{"type": "function", "function": {"name": "read_source", "description": "Read a frozen source file.",
            "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"],
                           "additionalProperties": False}}}]


def wire_response(*, content: str | None = "done", arguments: str | None = None,
                  finish: str | None = None) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if arguments is not None:
        message["tool_calls"] = [{"id": "wire-call-1", "type": "function",
                                  "function": {"name": "read_source", "arguments": arguments}}]
    return {"id": "wire-message-1", "model": "configured/model", "object": "chat.completion", "created": 1,
            "choices": [{"index": 0, "message": message,
                         "finish_reason": finish or ("tool_calls" if arguments is not None else "stop")}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}


def adapter(handler: Callable[[httpx.Request], httpx.Response] | None = None, **settings: Any) -> ChatCompletionsAdapter:
    return ChatCompletionsAdapter(ProviderSettings(provider_id="configured-provider", model_id="configured/model",
                                                 **settings),
                                  httpx.MockTransport(handler or (lambda _: httpx.Response(200, json=wire_response()))))


def prepared(model: ChatCompletionsAdapter) -> PreparedRequest:
    return model.prepare([SystemMessage(content="Analyze the fault."), HumanMessage(content="Inspect source.")],
                         descriptors(), stop=None, bind_options={}, generate_options={})


def intent(request: PreparedRequest) -> ModelCallIntent:
    return ModelCallIntent(call_id="model-1", task_id="task1", run_id="run1", kind="solver",
                           provider_id="configured-provider", model_id="configured/model",
                           prompt_sha256=sha256(canonical(request.messages)),
                           tool_schema_sha256=sha256(canonical(request.tools)),
                           parameters_sha256=sha256(canonical({**request.parameters, "body_sha256": request.body_sha256})),
                           tool_names=("read_source",))


def test_exact_canonical_wire_body_and_all_request_groups_are_bound() -> None:
    received: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        received.append(request)
        return httpx.Response(200, json=wire_response(arguments='{"path":"src/中文.ets"}', content=None))

    model = adapter(handle, temperature=0.0, max_output_tokens=77, timeout_seconds=12.0)
    request = model.prepare([SystemMessage(content="找原因"), HumanMessage(content="定位")], descriptors(),
                            stop=["END"], bind_options={"tool_choice": "auto"},
                            generate_options={"parallel_tool_calls": False})
    message = model.invoke(request)
    assert len(received) == 1
    outgoing = received[0]
    assert str(outgoing.url) == MOCK_ENDPOINT and outgoing.method == "POST"
    assert outgoing.content.decode("utf-8") == request.body
    assert "\\u627e" not in request.body
    assert hashlib.sha256(outgoing.content).hexdigest() == request.body_sha256
    assert json.loads(outgoing.content) == {**request.parameters, "messages": request.messages, "tools": request.tools}
    assert request.parameters == {"model": "configured/model", "max_tokens": 77, "stream": False,
                                  "stop": ["END"], "temperature": 0.0, "tool_choice": "auto",
                                  "parallel_tool_calls": False}
    assert outgoing.extensions["timeout"] == {"connect": 12.0, "read": 12.0, "write": 12.0, "pool": 12.0}
    assert "authorization" not in outgoing.headers
    assert message.id == "wire-message-1"
    assert message.tool_calls == [{"name": "read_source", "args": {"path": "src/中文.ets"},
                                   "id": "wire-call-1", "type": "tool_call"}]
    assert message.response_metadata["model_name"] == "configured/model"


def test_request_views_cannot_mutate_wire_or_caller_inputs() -> None:
    model = adapter()
    source_tools = descriptors()
    messages = [HumanMessage(content="original")]
    request = model.prepare(messages, source_tools)
    source_tools[0]["function"]["name"] = "changed"
    messages[0].content = "changed"
    request.messages[0]["content"] = "changed"
    request.tools[0]["function"]["parameters"]["type"] = "changed"
    request.parameters["max_tokens"] = 100_000
    assert request.messages == [{"role": "user", "content": "original"}]
    assert request.tools[0]["function"]["name"] == "read_source"
    assert request.parameters["max_tokens"] == 2048
    with pytest.raises(ValidationError, match="frozen"):
        request.body = "{}"


def test_profile_is_config_only_mock_only_and_changes_with_settings() -> None:
    model = adapter()
    profile = model.profile()
    assert profile["transport"] == "mock-only" and profile["endpoint"] == MOCK_ENDPOINT
    assert profile["automatic_retries"] == 0 and profile["follow_redirects"] is False and profile["trust_env"] is False
    assert profile["max_request_bytes"] == MAX_REQUEST_BYTES and profile["max_response_bytes"] == MAX_RESPONSE_BYTES
    assert "messages" not in profile and "api_key" not in profile
    profile["settings"]["model_id"] = "tampered"
    assert model.profile()["settings"]["model_id"] == "configured/model"
    assert prepared(model).profile_sha256 != prepared(adapter(max_output_tokens=99)).profile_sha256


@pytest.mark.parametrize("changes", [
    {"api_key": "secret"}, {"base_url": "https://actual.example"}, {"provider_id": "bad\nidentifier"},
    {"model_id": "\n"}, {"max_output_tokens": True}, {"max_output_tokens": 0}, {"max_output_tokens": "77"},
    {"temperature": float("nan")}, {"temperature": 2.1}, {"timeout_seconds": 0},
    {"timeout_seconds": float("inf")},
])
def test_invalid_provider_settings_are_rejected(changes: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        ProviderSettings.model_validate({"provider_id": "configured-provider", "model_id": "configured/model", **changes})


def test_real_transport_and_clients_are_rejected_without_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> None:
        pytest.fail("a real client must not be constructed")

    monkeypatch.setattr(httpx, "Client", forbidden)
    settings = ProviderSettings(provider_id="configured-provider", model_id="configured/model")
    with pytest.raises(AdapterRequestError, match="mock_transport_required"):
        ChatCompletionsAdapter(settings, object())  # type: ignore[arg-type]


def test_continuation_preserves_reasoning_and_native_tool_result_identity() -> None:
    response = wire_response(content=None, arguments='{"path":"src/Main.ets"}')
    response["choices"][0]["message"]["reasoning_content"] = "Track the assignment upstream."
    model = adapter(lambda _: httpx.Response(200, json=response))
    message = model.invoke(prepared(model))
    continuation = model.prepare([HumanMessage(content="Find the bug."), message,
                                  ToolMessage(content='{"line":7}', tool_call_id="wire-call-1", name="read_source")],
                                 descriptors())
    assert continuation.messages[1] == {"role": "assistant", "content": "",
                                       "reasoning_content": "Track the assignment upstream.",
                                       "tool_calls": [{"id": "wire-call-1", "type": "function", "function": {
                                           "name": "read_source", "arguments": '{"path":"src/Main.ets"}'}}]}
    assert continuation.messages[2] == {"role": "tool", "content": '{"line":7}', "tool_call_id": "wire-call-1"}


@pytest.mark.parametrize("messages,reason", [
    ([], "missing_messages"),
    ([HumanMessage(content=[{"type": "image_url", "image_url": {"url": "https://example.invalid/image.png"}}])],
     "unsupported_message_content"),
    ([HumanMessage(content="text", additional_kwargs={"attachment": "bytes"})], "unsupported_message_attachment"),
    ([ChatMessage(content="custom", role="unknown")], "unsupported_message_role"),
    ([AIMessageChunk(content="partial")], "unsupported_message_role"),
    ([AIMessage(content="text", additional_kwargs={"reasoning_content": 7})], "invalid_reasoning_content"),
    ([AIMessage(content="text", additional_kwargs={"prefix": True})], "unsupported_assistant_message"),
    ([ToolMessage(content="orphan", tool_call_id="missing")], "orphan_or_duplicate_tool_result"),
    ([ToolMessage(content="tool", tool_call_id="missing", artifact={"bytes": "attachment"})],
     "unsupported_tool_message_attachment"),
])
def test_unsupported_messages_reject_before_dispatch(messages: list[Any], reason: str) -> None:
    def forbidden(_: httpx.Request) -> httpx.Response:
        pytest.fail("invalid messages must not dispatch")

    with pytest.raises(AdapterRequestError, match=reason):
        adapter(forbidden).prepare(messages, descriptors())


def test_native_system_text_blocks_preserve_complete_ordered_wire_text() -> None:
    content = [{"type": "text", "text": "original system 中文"},
               {"type": "text", "text": "\n\n完整 static procedure catalog\n"}]
    model = adapter()
    messages = [SystemMessage(content=content), HumanMessage(content="task")]
    before = messages[0].model_dump_json()
    blocks = model.prepare(messages, descriptors())
    ordinary = model.prepare([SystemMessage(content="".join(block["text"] for block in content)),
                              HumanMessage(content="task")], descriptors())
    assert blocks == ordinary
    assert messages[0].model_dump_json() == before


@pytest.mark.parametrize("content", [[], ["text"], [{"type": "image_url", "image_url": {"url": "invalid"}}],
    [{"type": "text", "text": "text", "cache_control": {"type": "ephemeral"}}]])
def test_system_block_transport_does_not_accept_unsupported_block_shapes(content) -> None:
    with pytest.raises(AdapterRequestError, match="unsupported_message_content"):
        adapter().prepare([SystemMessage(content=content)], descriptors())


def test_tool_history_must_resolve_unique_calls_before_next_turn() -> None:
    model = adapter()
    call = AIMessage(content="", tool_calls=[{"name": "read_source", "args": {}, "id": "tool1"}])
    with pytest.raises(AdapterRequestError, match="unfinished_tool_results"):
        model.prepare([HumanMessage(content="read"), call], descriptors())
    with pytest.raises(AdapterRequestError, match="unfinished_tool_results"):
        model.prepare([call, HumanMessage(content="continue")], descriptors())
    with pytest.raises(AdapterRequestError, match="orphan_or_duplicate_tool_result"):
        model.prepare([call, ToolMessage(content="ok", tool_call_id="tool1"),
                       ToolMessage(content="again", tool_call_id="tool1")], descriptors())
    with pytest.raises(AdapterRequestError, match="unfinished_tool_results"):
        model.prepare([call, ToolMessage(content="ok", tool_call_id="tool1"), call], descriptors())
    duplicate_batch = AIMessage(content="", tool_calls=[*call.tool_calls, *call.tool_calls])
    with pytest.raises(AdapterRequestError, match="invalid_history_tool_call"):
        model.prepare([duplicate_batch], descriptors())


def test_completed_tool_groups_may_reuse_ids_without_rewriting_frozen_requests() -> None:
    model = adapter()
    call = AIMessage(content="", tool_calls=[{"name": "read_source", "args": {}, "id": "tool1"}])
    messages: list[BaseMessage] = [HumanMessage(content="read"), call,
        ToolMessage(content="first", tool_call_id="tool1"), call.model_copy(deep=True),
        ToolMessage(content="second", tool_call_id="tool1")]
    request = model.prepare(messages, descriptors())
    frozen = request.model_dump_json()
    assert model.validate(request).model_dump_json() == frozen
    assert [item["tool_call_id"] for item in request.messages if item["role"] == "tool"] == ["tool1", "tool1"]


@pytest.mark.parametrize("tool_choice", ["auto", "none", "required",
                                       {"type": "function", "function": {"name": "read_source"}}])
def test_supported_native_tool_choices_are_preserved(tool_choice: Any) -> None:
    request = adapter().prepare([HumanMessage(content="read")], descriptors(), bind_options={"tool_choice": tool_choice})
    assert request.parameters["tool_choice"] == tool_choice


@pytest.mark.parametrize("bind,generate,reason", [
    ({"max_retries": 5}, {}, "unsupported_generation_option"),
    ({}, {"stream": True}, "unsupported_generation_option"),
    ({}, {"max_tokens": 7}, "unsupported_generation_option"),
    ({"tool_choice": "auto"}, {"tool_choice": "none"}, "duplicate_generation_option"),
    ({"tool_choice": "any"}, {}, "invalid_tool_choice"),
    ({"tool_choice": {"type": "function", "function": {"name": "not_offered"}}}, {}, "invalid_tool_choice"),
    ({"parallel_tool_calls": "false"}, {}, "invalid_parallel_tool_calls"),
    ({"tool_choice": {"type": "function", "function": {"name": "read_source", "extra": 1}}}, {},
     "invalid_tool_choice"),
])
def test_unsupported_bind_and_generate_options_are_not_dropped(bind: dict[str, Any], generate: dict[str, Any],
                                                            reason: str) -> None:
    with pytest.raises(AdapterRequestError, match=reason):
        adapter().prepare([HumanMessage(content="read")], descriptors(), bind_options=bind, generate_options=generate)


@pytest.mark.parametrize("stop", [[], [""], [7], "stop", ["end"] * 17])
def test_invalid_stop_rejects_without_dispatch(stop: Any) -> None:
    with pytest.raises(AdapterRequestError, match="invalid_stop"):
        adapter().prepare([HumanMessage(content="read")], descriptors(), stop=stop)


@pytest.mark.parametrize("changes,reason", [
    ({"type": "custom"}, "unsupported_tool_descriptor"),
    ({"extra": True}, "unsupported_tool_descriptor"),
    ({"function": {"name": "read_source", "parameters": {}, "unsupported": True}}, "unsupported_tool_function"),
    ({"function": {"name": "read_source", "parameters": {"type": "array"}}}, "unsupported_tool_parameters"),
    ({"function": {"name": "bad name", "parameters": {"type": "object"}}}, "invalid_tool_name"),
    ({"function": {"name": "read_source", "parameters": {"type": "object"}, "description": 7}},
     "invalid_tool_description"),
    ({"function": {"name": "read_source", "parameters": {"type": "object"}, "strict": "true"}}, "invalid_tool_strict"),
])
def test_tool_schema_profile_is_finite(changes: dict[str, Any], reason: str) -> None:
    tool = {**descriptors()[0], **changes}
    with pytest.raises(AdapterRequestError, match=reason):
        adapter().prepare([HumanMessage(content="read")], [tool])


def test_duplicate_tools_non_json_keys_and_no_tool_options_reject() -> None:
    model = adapter()
    with pytest.raises(AdapterRequestError, match="invalid_tool_name"):
        model.prepare([HumanMessage(content="read")], descriptors() * 2)
    tools = descriptors()
    tools[0]["function"]["parameters"]["properties"][1] = {"type": "string"}
    with pytest.raises(AdapterRequestError, match="unsupported_json_value"):
        model.prepare([HumanMessage(content="read")], tools)
    with pytest.raises(AdapterRequestError, match="tool_choice_without_tools"):
        model.prepare([HumanMessage(content="read")], [], bind_options={"tool_choice": "required"})


@pytest.mark.parametrize("mutation", ["body", "profile", "noncanonical", "invalidbodyhash"])
def test_prepared_request_tampering_rejects_before_transport(mutation: str) -> None:
    def forbidden(_: httpx.Request) -> httpx.Response:
        pytest.fail("changed requests must not dispatch")

    model = adapter(forbidden)
    request = prepared(model)
    changes: dict[str, Any] = {}
    if mutation == "body":
        body = json.loads(request.body)
        body["messages"][0]["content"] = "changed"
        changes["body"] = canonical(body)
    elif mutation == "profile":
        changes["profile_sha256"] = "a" * 64
    elif mutation == "noncanonical":
        changes["body"] = " " + request.body
    else:
        changes["body_sha256"] = "a" * 64
    with pytest.raises(AdapterRequestError, match="prepared_request_changed"):
        model.invoke(request.model_copy(update=changes))


def test_changed_adapter_settings_cannot_send_old_request() -> None:
    first = adapter()
    request = prepared(first)
    second = adapter(max_output_tokens=7)
    with pytest.raises(AdapterRequestError, match="prepared_request_changed"):
        second.invoke(request)


@pytest.mark.parametrize("mutation", ["role", "attachment", "unknown-option", "tool-schema", "bool-max-tokens"])
def test_handcrafted_self_hashed_requests_still_obey_finite_profile(mutation: str) -> None:
    def forbidden(_: httpx.Request) -> httpx.Response:
        pytest.fail("unsupported request profile must not dispatch")

    model = adapter(forbidden)
    original = prepared(model)
    body = json.loads(original.body)
    if mutation == "role":
        body["messages"][0]["role"] = "unknown"
    elif mutation == "attachment":
        body["messages"][0]["image_url"] = "private"
    elif mutation == "unknown-option":
        body["retry"] = 100
    elif mutation == "tool-schema":
        body["tools"][0]["function"]["parameters"] = {"type": "array"}
    else:
        body["max_tokens"] = True
    encoded = canonical(body)
    request = PreparedRequest(body=encoded, body_sha256=sha256(encoded), profile_sha256=original.profile_sha256)
    with pytest.raises(AdapterRequestError, match="prepared_request_changed"):
        model.invoke(request)


def test_transport_replacement_cannot_open_real_client(monkeypatch: pytest.MonkeyPatch) -> None:
    model = adapter()
    request = prepared(model)
    model._transport = object()  # type: ignore[assignment]
    monkeypatch.setattr(httpx, "Client", lambda **_: pytest.fail("must reject before client construction"))
    with pytest.raises(AdapterRequestError, match="mock_transport_required"):
        model.invoke(request)


def test_request_byte_limit_includes_utf8_and_is_checked_before_send() -> None:
    with pytest.raises(AdapterRequestError, match="request_byte_limit"):
        adapter().prepare([HumanMessage(content="界" * (MAX_REQUEST_BYTES // 3))], [])


@pytest.mark.parametrize("exception,reason", [
    (httpx.ReadTimeout("SECRET https://private.invalid/?key=secret"), "provider_timeout"),
    (httpx.ConnectError("SECRET bearer token"), "provider_transport_error"),
    (RuntimeError("SECRET custom transport failure"), "provider_transport_error"),
])
def test_transport_errors_are_safe_and_never_retry(exception: Exception, reason: str) -> None:
    attempts = 0

    def handle(_: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        raise exception

    model = adapter(handle)
    with pytest.raises(AdapterTransportError, match=reason) as error:
        model.invoke(prepared(model))
    assert attempts == 1 and error.value.reason == reason and str(error.value) == reason
    assert error.value.__suppress_context__


@pytest.mark.parametrize("status", [301, 401, 429, 500])
def test_http_error_redirects_and_rate_limits_do_not_retry_or_leak(status: int) -> None:
    attempts = 0

    def handle(_: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(status, headers={"Location": "https://private.invalid?key=SECRET"},
                              text="SECRET provider error")

    model = adapter(handle)
    with pytest.raises(AdapterTransportError, match="provider_http_error") as error:
        model.invoke(prepared(model))
    assert attempts == 1 and str(error.value) == "provider_http_error"


def test_bounded_response_is_not_parsed_when_over_limit() -> None:
    model = adapter(lambda _: httpx.Response(200, content=b"x" * (MAX_RESPONSE_BYTES + 1)))
    with pytest.raises(AdapterResponseError, match="response_byte_limit"):
        model.invoke(prepared(model))


@pytest.mark.parametrize("body", [b"not-json SECRET", b"\xff", b'{"id":"first","id":"duplicate"}',
                                 b'{"usage":NaN}', b"[]"])
def test_non_json_nonfinite_duplicate_and_wrong_response_roots_reject_safely(body: bytes) -> None:
    model = adapter(lambda _: httpx.Response(200, content=body))
    with pytest.raises(AdapterResponseError) as error:
        model.invoke(prepared(model))
    assert str(error.value) in ("invalid_response_json", "invalid_response_identity")
    assert "SECRET" not in str(error.value)


@pytest.mark.parametrize("mutation,reason", [
    ("no-id", "invalid_response_identity"), ("no-model", "invalid_response_identity"),
    ("multiple-choices", "invalid_response_choices"), ("no-choices", "invalid_response_choices"),
    ("wrong-role", "invalid_response_message"), ("no-content", "invalid_response_message"),
    ("non-text", "invalid_response_message"), ("wrong-index", "invalid_response_finish"),
    ("no-finish", "invalid_response_finish"), ("attachment", "unsupported_response_message_attachment"),
    ("reasoning-type", "invalid_response_reasoning"),
])
def test_response_format_failures_reject(mutation: str, reason: str) -> None:
    body = wire_response()
    message = body["choices"][0]["message"]
    if mutation == "no-id":
        del body["id"]
    elif mutation == "no-model":
        del body["model"]
    elif mutation == "multiple-choices":
        body["choices"] *= 2
    elif mutation == "no-choices":
        del body["choices"]
    elif mutation == "wrong-role":
        message["role"] = "user"
    elif mutation == "no-content":
        del message["content"]
    elif mutation == "non-text":
        message["content"] = [{"type": "image"}]
    elif mutation == "wrong-index":
        body["choices"][0]["index"] = 1
    elif mutation == "no-finish":
        del body["choices"][0]["finish_reason"]
    elif mutation == "attachment":
        message["audio"] = "unsupported"
    elif mutation == "reasoning-type":
        message["reasoning_content"] = 7
    model = adapter(lambda _: httpx.Response(200, json=body))
    with pytest.raises(AdapterResponseError, match=reason):
        model.invoke(prepared(model))


@pytest.mark.parametrize("arguments", ["{bad", "[]", "null", '{"path":"one","path":"two"}', '{"value":NaN}'])
def test_invalid_tool_arguments_return_accountable_format_error_not_retry(arguments: str) -> None:
    body = wire_response(content=None, arguments=arguments)
    model = adapter(lambda _: httpx.Response(200, json=body))
    request = prepared(model)
    message = model.invoke(request)
    assert not message.tool_calls and len(message.invalid_tool_calls) == 1
    assert message.invalid_tool_calls[0]["args"] == arguments
    observation = normalize_message(intent(request), message)
    assert observation.format_status == "format_error" and observation.usage_status == "known"
    assert observation.usage is not None and observation.usage.total_tokens == 15


@pytest.mark.parametrize("finish", ["length", "content_filter", "insufficient_system_resource", "aborted"])
def test_incomplete_generation_never_exposes_partial_tool_actions(finish: str) -> None:
    body = wire_response(content="unfinished", arguments='{"path":"src/Main.ets"}', finish=finish)
    model = adapter(lambda _: httpx.Response(200, json=body))
    message = model.invoke(prepared(model))
    assert not message.tool_calls and message.invalid_tool_calls[0]["error"] == "incomplete_generation"
    assert message.response_metadata["finish_reason"] == finish
    assert message.usage_metadata == {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}


@pytest.mark.parametrize("mode,effort", [("disabled", None), ("enabled", "high")])
def test_explicit_thinking_mode_is_frozen_in_actual_body_and_readonly_profile(mode: str, effort: str | None) -> None:
    model = adapter(thinking_mode=mode, reasoning_effort=effort)
    request = prepared(model)
    assert request.parameters["thinking"] == {"type": mode}
    assert "extra_body" not in request.parameters
    assert request.parameters.get("reasoning_effort") == effort
    readonly = adapter_from_profile(model.profile())
    assert readonly.profile() == model.profile() and readonly.validate(request) == request
    assert model.profile()["settings"]["thinking_mode"] == mode
    assert "thinking_mode" not in adapter().profile()["settings"]
    assert "reasoning_effort" not in adapter().profile()["settings"]
    assert request.profile_sha256 != prepared(adapter()).profile_sha256


@pytest.mark.parametrize("settings", [
    {"reasoning_effort": "high"}, {"thinking_mode": "disabled", "reasoning_effort": "low"},
    {"thinking_mode": "enabled", "temperature": 0.0}, {"thinking_mode": "unknown"},
])
def test_unsupported_thinking_configuration_is_not_silently_ignored(settings: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        adapter(**settings)


@pytest.mark.parametrize("choice", ["required", {"type": "function", "function": {"name": "read_source"}}])
def test_thinking_rejects_forced_tool_before_send(choice: Any) -> None:
    model = adapter(lambda _: pytest.fail("unsupported thinking choice must not send"), thinking_mode="enabled")
    with pytest.raises(AdapterRequestError, match="thinking_tool_choice_unsupported"):
        model.prepare([HumanMessage(content="inspect")], descriptors(), bind_options={"tool_choice": choice})


def test_nullable_reasoning_and_tool_fields_roundtrip_without_inventing_a_call() -> None:
    body = wire_response()
    body["choices"][0]["message"].update(reasoning_content=None, tool_calls=None)
    model = adapter(lambda _: httpx.Response(200, json=body), thinking_mode="disabled")
    message = model.invoke(prepared(model))
    assert not message.tool_calls and message.additional_kwargs == {"reasoning_content": None}
    request = model.prepare([HumanMessage(content="first"), message, HumanMessage(content="next")], descriptors())
    assert request.messages[1]["reasoning_content"] is None and "tool_calls" not in request.messages[1]
    model.validate(request)


@pytest.mark.parametrize("finish", ["length", "aborted"])
def test_truncated_text_is_accounted_as_format_error_not_normal_finish(finish: str) -> None:
    model = adapter(lambda _: httpx.Response(200, json=wire_response(content="partial", finish=finish)))
    request = prepared(model)
    message = model.invoke(request)
    observation = normalize_message(intent(request), message)
    assert observation.format_status == "format_error" and "incomplete_generation" in observation.format_errors
    assert observation.usage_status == "known" and observation.usage is not None
    assert observation.usage.total_tokens == 15 and not message.tool_calls


def test_tool_calls_with_inconsistent_finish_or_duplicate_identity_are_format_errors() -> None:
    body = wire_response(content=None, arguments='{"path":"src/Main.ets"}', finish="stop")
    model = adapter(lambda _: httpx.Response(200, json=body))
    request = prepared(model)
    message = model.invoke(request)
    assert not message.tool_calls and message.invalid_tool_calls[0]["error"] == "unexpected_tool_finish"
    assert normalize_message(intent(request), message).format_status == "format_error"
    body["choices"][0]["finish_reason"] = "tool_calls"
    body["choices"][0]["message"]["tool_calls"] *= 2
    message = model.invoke(request)
    assert message.invalid_tool_calls[0]["error"] == "duplicate_tool_call_id"
    assert normalize_message(intent(request), message).format_status == "format_error"


@pytest.mark.parametrize("change", [{"id": None}, {"id": ""}, {"id": 7}, {"type": "custom"},
    {"function": {}}, {"function": None}, {"function": "invalid"},
    {"function": {"name": 7, "arguments": "{}"}}, {"function": {"name": "read_source", "arguments": []}},
    {"function": {"name": "read_source", "arguments": "{}", "extra": "unsupported"}},
    {"extra": "unsupported"}, {"index": None}, {"index": True}, {"index": -1}, {"index": "0"}])
def test_malformed_tool_call_descriptors_are_not_sent_to_host_tools(change: dict[str, Any]) -> None:
    body = wire_response(content=None, arguments="{}")
    body["choices"][0]["message"]["tool_calls"][0].update(change)
    model = adapter(lambda _: httpx.Response(200, json=body))
    request = prepared(model)
    message = model.invoke(request)
    assert not message.tool_calls and message.invalid_tool_calls[0]["error"] == "invalid_response_tool_call"
    assert message.response_metadata["dfx_provider_tool_calls_raw"] == body["choices"][0]["message"]["tool_calls"]
    observation = normalize_message(intent(request), message)
    assert observation.format_status == "format_error" and observation.usage_status == "known"
    assert observation.usage is not None and observation.usage.total_tokens == 15
    assert AIMessage.model_validate_json(message.model_dump_json()) == message


@pytest.mark.parametrize("index", [0, 1, 1000])
def test_nonnegative_tool_index_is_metadata_without_execution_parameters(index: int) -> None:
    body = wire_response(content=None, arguments='{"path":"src/Main.ets"}')
    body["choices"][0]["message"]["tool_calls"][0]["index"] = index
    model = adapter(lambda _: httpx.Response(200, json=body))
    request = prepared(model)
    message = model.invoke(request)
    assert message.tool_calls == [{"name": "read_source", "args": {"path": "src/Main.ets"},
                                   "id": "wire-call-1", "type": "tool_call"}]
    assert not message.invalid_tool_calls
    assert message.response_metadata["dfx_provider_tool_calls_raw"][0]["index"] == index
    assert normalize_message(intent(request), message).format_status == "tool_calls"
    continuation = model.prepare([HumanMessage(content="task"), message,
        ToolMessage(content="source", tool_call_id="wire-call-1", name="read_source")], descriptors())
    assert "index" not in continuation.messages[1]["tool_calls"][0]


@pytest.mark.parametrize("wire_calls", [None, [], "not a list", {}, [None], ["not an object"]])
def test_missing_or_invalid_tool_container_retains_usage_as_format_error(wire_calls: Any) -> None:
    body = wire_response(content=None, finish="tool_calls")
    body["choices"][0]["message"]["tool_calls"] = wire_calls
    model = adapter(lambda _: httpx.Response(200, json=body))
    request = prepared(model)
    message = model.invoke(request)
    assert not message.tool_calls and message.invalid_tool_calls
    assert message.response_metadata["dfx_provider_tool_calls_raw"] == wire_calls
    observation = normalize_message(intent(request), message)
    assert observation.format_status == "format_error" and observation.usage_status == "known"


@pytest.mark.parametrize("usage,expected", [
    (None, "missing"), ({"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}, "known"),
    ({}, "invalid"), ({"prompt_tokens": "10", "completion_tokens": 5, "total_tokens": 15}, "invalid"),
    ({"prompt_tokens": True, "completion_tokens": 5, "total_tokens": 6}, "invalid"),
    ({"prompt_tokens": -1, "completion_tokens": 5, "total_tokens": 4}, "invalid"),
    ({"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 99}, "invalid"),
    ([10, 5, 15], "invalid"),
])
def test_wire_usage_raw_survives_strict_classification_and_message_roundtrip(usage: Any, expected: str) -> None:
    body = wire_response()
    body["usage"] = usage
    model = adapter(lambda _: httpx.Response(200, json=body))
    request = prepared(model)
    message = model.invoke(request)
    restored = AIMessage.model_validate_json(message.model_dump_json())
    assert restored == message
    if usage is not None:
        assert restored.response_metadata["dfx_provider_usage_raw"] == usage
    else:
        assert "dfx_provider_usage_raw" not in restored.response_metadata
    observation = normalize_message(intent(request), restored)
    assert observation.usage_status == expected
    if expected != "known":
        assert restored.usage_metadata is None


def test_missing_usage_is_not_guessed_from_content_or_reasoning() -> None:
    body = wire_response(content="long text" * 100)
    body["choices"][0]["message"]["reasoning_content"] = "reason" * 100
    del body["usage"]
    model = adapter(lambda _: httpx.Response(200, json=body))
    request = prepared(model)
    message = model.invoke(request)
    assert message.usage_metadata is None
    assert normalize_message(intent(request), message).usage_status == "missing"
