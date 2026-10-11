"""Finite text/tool Chat Completions with separate mock and host-gated cloud profiles."""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import time
from collections.abc import Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from importlib.metadata import version
from typing import Any, Literal, cast

import httpx
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SerializerFunctionWrapHandler,
    model_serializer,
    model_validator,
)

from .contracts import IDENTIFIER, require_hash

MOCK_ENDPOINT = "https://model.invalid/chat/completions"
MAX_REQUEST_BYTES = 1_048_576
MAX_RESPONSE_BYTES = 2_097_152
WIRE_PROFILE = "chat-completions-text-tools-v1"
INPUT_TOKEN_ESTIMATOR = "utf8-body-bytes-and-message-framing-v1"
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}\Z")
_SDK_PRIVATE_CALL: ContextVar[bool] = ContextVar("dfx_private_provider_call", default=False)


class _PrivateSdkLogFilter(logging.Filter):
    """SDK debug/error tracebacks are private while this adapter owns the call."""

    def filter(self, record: logging.LogRecord) -> bool:
        return not _SDK_PRIVATE_CALL.get()


@contextmanager
def _private_sdk_call():
    # Do not disable a user's global logger. Only suppress SDK internals in the
    # current call context; the adapter reports fixed public failure categories.
    for name in ("openai._base_client", "openai._response", "openai._legacy_response"):
        logger = logging.getLogger(name)
        if not any(isinstance(item, _PrivateSdkLogFilter) for item in logger.filters):
            logger.addFilter(_PrivateSdkLogFilter())
    token = _SDK_PRIVATE_CALL.set(True)
    try:
        yield
    finally:
        _SDK_PRIVATE_CALL.reset(token)


class AdapterError(RuntimeError):
    """A safe, fixed-category adapter failure; never carries provider exception text."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


class AdapterRequestError(AdapterError):
    """The request is unsupported and must not be sent."""


class AdapterTransportError(AdapterError):
    """A dispatched request did not yield a successful bounded response."""


class AdapterResponseError(AdapterError):
    """The response cannot be represented by the finite wire profile."""


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _invalid_constant(value: str) -> None:
    raise ValueError("non-finite JSON number")


def _finite_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("non-finite JSON number")
    return number


def _decode(value: str | bytes) -> Any:
    decoded = json.loads(value, object_pairs_hook=_unique_object, parse_constant=_invalid_constant, parse_float=_finite_float)
    _canonical(decoded).encode("utf-8")
    return decoded


def _plain_json(value: object) -> Any:
    """Validate JSON-only input, producing a fresh object with no caller-owned aliases."""
    def check(item: object) -> None:
        if item is None or type(item) in (str, bool, int):
            return
        if type(item) is float:
            if not math.isfinite(item):
                raise ValueError("non-finite input")
            return
        if type(item) is list:
            for element in item:
                check(element)
            return
        if type(item) is dict:
            for key, element in item.items():
                if not isinstance(key, str):
                    raise TypeError("non-text object key")
                check(element)
            return
        raise ValueError("non-JSON input")

    try:
        check(value)
        return _decode(_canonical(value))
    except (TypeError, ValueError, OverflowError, UnicodeError, RecursionError):
        raise AdapterRequestError("unsupported_json_value") from None


class ProviderSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    provider_id: str = Field(min_length=1, max_length=100)
    model_id: str = Field(min_length=1, max_length=200)
    max_output_tokens: int = Field(default=2048, ge=1, le=131_072)
    temperature: float | None = Field(default=None, ge=0, le=2)
    timeout_seconds: float = Field(default=30.0, gt=0, le=600)
    thinking_mode: Literal["enabled", "disabled"] | None = None
    reasoning_effort: Literal["none", "minimal", "low", "medium", "high", "xhigh", "max"] | None = None
    max_output_parameter: Literal["max_tokens", "max_completion_tokens"] | None = None

    @model_serializer(mode="wrap")
    def serialized(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        value = handler(self)
        # Preserve the exact settings shape of historical profiles that never
        # declared a thinking mode; an explicit mode has a distinct identity.
        for field in ("thinking_mode", "reasoning_effort", "max_output_parameter"):
            if value.get(field) is None:
                value.pop(field, None)
        return value

    @model_validator(mode="after")
    def valid_settings(self) -> ProviderSettings:
        if not IDENTIFIER.fullmatch(self.provider_id):
            raise ValueError("invalid provider identifier")
        if any(ord(character) < 32 for character in self.model_id) or not self.model_id.strip():
            raise ValueError("invalid model identifier")
        if not math.isfinite(self.timeout_seconds) or (
            self.temperature is not None and not math.isfinite(self.temperature)
        ):
            raise ValueError("non-finite provider setting")
        if self.reasoning_effort is not None and self.thinking_mode != "enabled" and self.provider_id != "openai":
            raise ValueError("reasoning effort requires explicit thinking mode")
        if self.thinking_mode == "enabled" and self.temperature is not None:
            raise ValueError("temperature is not supported in explicit thinking mode")
        return self


class CloudProviderConfig(BaseModel):
    """Public connection identity. The credential value is never a configuration field."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, hide_input_in_errors=True)
    endpoint: str = Field(min_length=1, max_length=2048)
    credential_env: str = Field(min_length=1, max_length=128)

    @model_validator(mode="after")
    def valid_connection(self) -> CloudProviderConfig:
        try:
            url = httpx.URL(self.endpoint)
            if (url.scheme != "https" or not url.host or url.userinfo or url.query or url.fragment or
                    "?" in self.endpoint or "#" in self.endpoint or
                    any(ord(character) <= 32 or ord(character) >= 127 for character in self.endpoint) or
                    str(url) != self.endpoint):
                raise ValueError("unsupported endpoint")
        except (ValueError, TypeError, httpx.InvalidURL):
            raise ValueError("invalid cloud endpoint") from None
        if not _ENV_NAME.fullmatch(self.credential_env):
            raise ValueError("invalid credential environment name")
        return self


def provider_connection(provider: Literal["openai", "deepseek", "compatible"], model_id: str, *,
                        base_url: str | None = None, credential_env: str | None = None,
                        **settings: Any) -> tuple[CloudProviderConfig, ProviderSettings]:
    """Resolve vendor ergonomics onto the official SDK's supported Chat API.

    A preset selects connection defaults, never a model, mode or secret value.
    Other protocols are not silently treated as OpenAI-compatible endpoints.
    """
    presets = {"openai": ("https://api.openai.com/v1", "OPENAI_API_KEY"),
               "deepseek": ("https://api.deepseek.com", "DEEPSEEK_API_KEY"),
               "compatible": (None, "DFX_PROVIDER_API_KEY")}
    if provider not in presets:
        raise AdapterRequestError("unsupported_provider_preset")
    default_url, default_env = presets[provider]
    selected_url = base_url if base_url is not None else default_url
    if not selected_url:
        raise AdapterRequestError("provider_base_url_required")
    endpoint = selected_url.rstrip("/") + "/chat/completions"
    if provider == "openai" and settings.get("thinking_mode") is not None:
        raise AdapterRequestError("provider_thinking_mode_unsupported")
    connection = CloudProviderConfig(endpoint=endpoint,
                                     credential_env=credential_env if credential_env is not None else default_env)
    model = ProviderSettings(provider_id=provider, model_id=model_id, **settings)
    return connection, model


class CloudCallAdmission(BaseModel):
    """Ephemeral host budget reservation; excluded from the persisted provider profile."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    body_sha256: str
    profile_sha256: str
    reserved_tokens: int = Field(ge=1)
    deadline: float = Field(gt=0)

    @model_validator(mode="after")
    def valid_admission(self) -> CloudCallAdmission:
        require_hash(self.body_sha256)
        require_hash(self.profile_sha256)
        if not math.isfinite(self.deadline):
            raise ValueError("invalid admission deadline")
        return self


class PreparedRequest(BaseModel):
    """The canonical body is the immutable truth; parsed views are fresh copies."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    body: str
    body_sha256: str
    profile_sha256: str

    @model_validator(mode="after")
    def valid_body(self) -> PreparedRequest:
        require_hash(self.profile_sha256)
        require_hash(self.body_sha256)
        try:
            encoded = self.body.encode("utf-8")
            if len(encoded) > MAX_REQUEST_BYTES:
                raise ValueError("request body too large")
            value = _decode(self.body)
            if not isinstance(value, dict) or _canonical(value) != self.body or (
                _digest(self.body) != self.body_sha256
            ):
                raise ValueError("invalid canonical body")
            if not isinstance(value.get("messages"), list) or not isinstance(value.get("tools", []), list):
                raise TypeError("invalid body groups")
        except (TypeError, ValueError, OverflowError, UnicodeError, RecursionError):
            raise ValueError("invalid prepared request body") from None
        return self

    @property
    def messages(self) -> list[dict[str, Any]]:
        return _decode(self.body)["messages"]

    @property
    def tools(self) -> list[dict[str, Any]]:
        return _decode(self.body).get("tools", [])

    @property
    def parameters(self) -> dict[str, Any]:
        return {key: value for key, value in _decode(self.body).items() if key not in ("messages", "tools")}


def _tool_descriptors(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    descriptors = _plain_json(tools)
    if not isinstance(descriptors, list):
        raise AdapterRequestError("unsupported_tool_descriptors")
    names: set[str] = set()
    for item in descriptors:
        if not isinstance(item, dict) or set(item) != {"type", "function"} or item["type"] != "function":
            raise AdapterRequestError("unsupported_tool_descriptor")
        function = item["function"]
        if not isinstance(function, dict) or not {"name", "parameters"}.issubset(function) or (
            set(function) - {"name", "description", "parameters", "strict"}
        ):
            raise AdapterRequestError("unsupported_tool_function")
        name = function["name"]
        if not isinstance(name, str) or not IDENTIFIER.fullmatch(name) or name in names:
            raise AdapterRequestError("invalid_tool_name")
        names.add(name)
        if not isinstance(function["parameters"], dict) or function["parameters"].get("type") != "object":
            raise AdapterRequestError("unsupported_tool_parameters")
        if "description" in function and not isinstance(function["description"], str):
            raise AdapterRequestError("invalid_tool_description")
        if "strict" in function and type(function["strict"]) is not bool:
            raise AdapterRequestError("invalid_tool_strict")
    return descriptors


def _message_payload(messages: list[BaseMessage]) -> list[dict[str, Any]]:
    if not isinstance(messages, list) or not messages:
        raise AdapterRequestError("missing_messages")
    payload: list[dict[str, Any]] = []
    outstanding: set[str] = set()
    all_ids: set[str] = set()
    for message in messages:
        if not isinstance(message, BaseMessage):
            raise AdapterRequestError("unsupported_message_role")
        content = message.content
        if not isinstance(content, str):
            # The native Skills middleware appends ordered text blocks to the
            # SystemMessage. Preserve every character, including its own
            # separators, when sending the same text through Chat Completions.
            # Other roles, multimodal blocks and provider extras remain denied.
            if (type(message) is not SystemMessage or not isinstance(content, list) or not content or
                    any(type(block) is not dict or set(block) != {"type", "text"} or
                        block["type"] != "text" or not isinstance(block["text"], str) for block in content)):
                raise AdapterRequestError("unsupported_message_content")
            content = "".join(cast(dict[str, Any], block)["text"] for block in content)
        if type(message) is ToolMessage:
            if message.additional_kwargs or message.artifact is not None:
                raise AdapterRequestError("unsupported_tool_message_attachment")
            if message.tool_call_id not in outstanding:
                raise AdapterRequestError("orphan_or_duplicate_tool_result")
            outstanding.remove(message.tool_call_id)
            payload.append({"role": "tool", "content": message.content, "tool_call_id": message.tool_call_id})
            continue
        if outstanding:
            raise AdapterRequestError("unfinished_tool_results")
        item: dict[str, Any]
        if type(message) in (SystemMessage, HumanMessage):
            if message.additional_kwargs:
                raise AdapterRequestError("unsupported_message_attachment")
            item = {"role": "system" if isinstance(message, SystemMessage) else "user", "content": content}
        elif type(message) is AIMessage:
            if message.invalid_tool_calls or set(message.additional_kwargs) - {"reasoning_content"}:
                raise AdapterRequestError("unsupported_assistant_message")
            item = {"role": "assistant", "content": message.content}
            if "reasoning_content" in message.additional_kwargs:
                reasoning = message.additional_kwargs["reasoning_content"]
                if reasoning is not None and not isinstance(reasoning, str):
                    raise AdapterRequestError("invalid_reasoning_content")
                item["reasoning_content"] = reasoning
            calls: list[dict[str, Any]] = []
            for call in message.tool_calls:
                call_id, name, arguments = call.get("id"), call.get("name"), call.get("args")
                if not isinstance(call_id, str) or not call_id or call_id in all_ids or (
                    not isinstance(name, str) or not IDENTIFIER.fullmatch(name) or not isinstance(arguments, dict)
                ):
                    raise AdapterRequestError("invalid_history_tool_call")
                all_ids.add(call_id)
                outstanding.add(call_id)
                calls.append({"id": call_id, "type": "function", "function": {
                    "name": name, "arguments": _canonical(_plain_json(arguments))}})
            if calls:
                item["tool_calls"] = calls
        else:
            raise AdapterRequestError("unsupported_message_role")
        if message.name is not None:
            if not isinstance(message.name, str) or not IDENTIFIER.fullmatch(message.name):
                raise AdapterRequestError("invalid_message_name")
            item["name"] = message.name
        payload.append(item)
    if outstanding:
        raise AdapterRequestError("unfinished_tool_results")
    return payload


def _options(bind_options: dict[str, Any], generate_options: dict[str, Any], tools: list[dict[str, Any]]) -> dict[str, Any]:
    if not isinstance(bind_options, dict) or not isinstance(generate_options, dict):
        raise AdapterRequestError("unsupported_generation_options")
    if set(bind_options) & set(generate_options):
        raise AdapterRequestError("duplicate_generation_option")
    options = _plain_json({**bind_options, **generate_options})
    if set(options) - {"tool_choice", "parallel_tool_calls"}:
        raise AdapterRequestError("unsupported_generation_option")
    if "parallel_tool_calls" in options and type(options["parallel_tool_calls"]) is not bool:
        raise AdapterRequestError("invalid_parallel_tool_calls")
    if "tool_choice" in options:
        choice = options["tool_choice"]
        if isinstance(choice, str):
            if choice not in ("auto", "none", "required"):
                raise AdapterRequestError("invalid_tool_choice")
        elif isinstance(choice, dict):
            function = choice.get("function")
            if set(choice) != {"type", "function"} or choice["type"] != "function" or (
                not isinstance(function, dict) or set(function) != {"name"} or
                function["name"] not in {item["function"]["name"] for item in tools}
            ):
                raise AdapterRequestError("invalid_tool_choice")
        else:
            raise AdapterRequestError("invalid_tool_choice")
        if not tools and choice != "none":
            raise AdapterRequestError("tool_choice_without_tools")
    return options


def _wire_messages_valid(messages: Any) -> bool:
    """Reject handcrafted bodies that bypass prepare's finite message profile."""
    if not isinstance(messages, list) or not messages:
        return False
    outstanding: set[str] = set()
    seen_ids: set[str] = set()
    for item in messages:
        if not isinstance(item, dict) or not isinstance(item.get("content"), str):
            return False
        role = item.get("role")
        if role == "tool":
            call_id = item.get("tool_call_id")
            if set(item) != {"role", "content", "tool_call_id"} or not isinstance(call_id, str) or (
                call_id not in outstanding
            ):
                return False
            outstanding.remove(call_id)
            continue
        if outstanding or role not in ("system", "user", "assistant"):
            return False
        allowed = {"role", "content", "name"}
        if "name" in item and (not isinstance(item["name"], str) or not IDENTIFIER.fullmatch(item["name"])):
            return False
        if role == "assistant":
            allowed |= {"reasoning_content", "tool_calls"}
            if "reasoning_content" in item and item["reasoning_content"] is not None and not isinstance(item["reasoning_content"], str):
                return False
            calls = item.get("tool_calls", [])
            if not isinstance(calls, list):
                return False
            for call in calls:
                if not isinstance(call, dict) or set(call) != {"id", "type", "function"}:
                    return False
                function = call["function"]
                if call["type"] != "function" or not isinstance(call["id"], str) or not call["id"] or (
                    call["id"] in seen_ids or not isinstance(function, dict) or
                    set(function) != {"name", "arguments"} or not isinstance(function["name"], str) or
                    not IDENTIFIER.fullmatch(function["name"]) or not isinstance(function["arguments"], str)
                ):
                    return False
                try:
                    if not isinstance(_decode(function["arguments"]), dict):
                        return False
                except (TypeError, ValueError, UnicodeError, RecursionError):
                    return False
                outstanding.add(call["id"])
                seen_ids.add(call["id"])
        if set(item) - allowed:
            return False
    return not outstanding


class ChatCompletionsAdapter:
    """Synchronous single-attempt adapter with no executable real-network path."""

    def __init__(self, settings: ProviderSettings, transport: httpx.MockTransport):
        if not isinstance(settings, ProviderSettings) or type(transport) is not httpx.MockTransport:
            raise AdapterRequestError("mock_transport_required")
        self.settings = ProviderSettings.model_validate(settings.model_dump())
        self._transport = transport
        self._readonly = False

    def profile(self) -> dict[str, Any]:
        return {"schema_version": 1, "wire_profile": WIRE_PROFILE, "transport": "mock-only",
                "endpoint": MOCK_ENDPOINT, "settings": self.settings.model_dump(mode="json"),
                "max_request_bytes": MAX_REQUEST_BYTES, "max_response_bytes": MAX_RESPONSE_BYTES,
                "automatic_retries": 0, "follow_redirects": False, "trust_env": False}

    def prepare(self, messages: list[BaseMessage], tools: list[dict[str, Any]], *,
                stop: list[str] | None = None, bind_options: dict[str, Any] | None = None,
                generate_options: dict[str, Any] | None = None) -> PreparedRequest:
        descriptors = _tool_descriptors(tools)
        output_parameter = self.settings.max_output_parameter or "max_tokens"
        body: dict[str, Any] = {"model": self.settings.model_id, "messages": _message_payload(messages),
                                output_parameter: self.settings.max_output_tokens, "stream": False}
        if descriptors:
            body["tools"] = descriptors
        if self.settings.temperature is not None:
            body["temperature"] = self.settings.temperature
        if self.settings.thinking_mode is not None:
            body["thinking"] = {"type": self.settings.thinking_mode}
        if self.settings.reasoning_effort is not None:
            body["reasoning_effort"] = self.settings.reasoning_effort
        if stop is not None:
            if not isinstance(stop, list) or not 1 <= len(stop) <= 16 or any(
                not isinstance(item, str) or not item for item in stop
            ):
                raise AdapterRequestError("invalid_stop")
            body["stop"] = list(stop)
        body.update(_options({} if bind_options is None else bind_options,
                             {} if generate_options is None else generate_options, descriptors))
        self._validate_thinking(body)
        try:
            encoded = _canonical(body)
            if len(encoded.encode("utf-8")) > MAX_REQUEST_BYTES:
                raise AdapterRequestError("request_byte_limit")
            return PreparedRequest(body=encoded, body_sha256=_digest(encoded),
                                   profile_sha256=_digest(_canonical(self.profile())))
        except AdapterError:
            raise
        except (TypeError, ValueError, OverflowError, UnicodeError, RecursionError):
            raise AdapterRequestError("unsupported_request") from None

    def _validate_transport(self) -> None:
        if type(self._transport) is not httpx.MockTransport:
            raise AdapterRequestError("mock_transport_required")

    def _validate_thinking(self, body: dict[str, Any]) -> None:
        expected = None if self.settings.thinking_mode is None else {"type": self.settings.thinking_mode}
        if body.get("thinking") != expected or (expected is None and "thinking" in body) or (
                body.get("reasoning_effort") != self.settings.reasoning_effort or
                (self.settings.reasoning_effort is None and "reasoning_effort" in body)):
            raise AdapterRequestError("thinking_profile_changed")
        if self.settings.thinking_mode == "enabled":
            choice = body.get("tool_choice")
            if choice is not None and choice not in ("auto", "none"):
                raise AdapterRequestError("thinking_tool_choice_unsupported")
            if body.get("tools") and any(item.get("role") == "assistant" and
                    not isinstance(item.get("reasoning_content"), str) for item in body["messages"]):
                raise AdapterRequestError("thinking_history_requires_reasoning")

    def validate(self, request: PreparedRequest) -> PreparedRequest:
        """Pure admission before ledger dispatch; invoke repeats it at the wire boundary."""
        self._validate_transport()
        try:
            validated = PreparedRequest.model_validate(request.model_dump())
            if validated.profile_sha256 != _digest(_canonical(self.profile())):
                raise ValueError("profile differs")
            body = _decode(validated.body)
            allowed = {"model", "messages", "tools", "max_tokens", "max_completion_tokens", "stream", "temperature", "stop",
                       "tool_choice", "parallel_tool_calls", "thinking", "reasoning_effort"}
            output_parameter = self.settings.max_output_parameter or "max_tokens"
            if set(body) - allowed or body["model"] != self.settings.model_id or body["stream"] is not False or (
                type(body[output_parameter]) is not int or body[output_parameter] != self.settings.max_output_tokens or
                any(key != output_parameter and key in body for key in ("max_tokens", "max_completion_tokens")) or
                body.get("temperature") != self.settings.temperature or
                ("temperature" in body and (self.settings.temperature is None or
                                            type(body["temperature"]) not in (float, int)))
            ):
                raise ValueError("request parameters differ")
            if not _wire_messages_valid(body["messages"]):
                raise ValueError("message profile differs")
            self._validate_thinking(body)
            tools = _tool_descriptors(body.get("tools", []))
            _options({}, {key: body[key] for key in ("tool_choice", "parallel_tool_calls") if key in body}, tools)
            if "stop" in body and (not isinstance(body["stop"], list) or not 1 <= len(body["stop"]) <= 16 or
                                   any(not isinstance(item, str) or not item for item in body["stop"])):
                raise ValueError("stop profile differs")
        except AdapterRequestError:
            raise AdapterRequestError("prepared_request_changed") from None
        except (AttributeError, KeyError, TypeError, ValueError, UnicodeError, RecursionError):
            raise AdapterRequestError("prepared_request_changed") from None
        return validated

    def invoke(self, request: PreparedRequest) -> AIMessage:
        validated = self.validate(request)
        if self._readonly:
            raise AdapterRequestError("provider_readonly")
        try:
            with (httpx.Client(transport=self._transport, timeout=self.settings.timeout_seconds,
                               follow_redirects=False, trust_env=False) as client,
                  client.stream("POST", MOCK_ENDPOINT, content=validated.body.encode("utf-8"),
                                headers={"Content-Type": "application/json", "Accept": "application/json"}) as response):
                if not 200 <= response.status_code < 300:
                    raise AdapterTransportError("provider_http_error")
                chunks: list[bytes] = []
                size = 0
                for chunk in response.iter_bytes(chunk_size=8192):
                    size += len(chunk)
                    if size > MAX_RESPONSE_BYTES:
                        raise AdapterResponseError("response_byte_limit")
                    chunks.append(chunk)
                raw = b"".join(chunks)
        except AdapterError:
            raise
        except httpx.TimeoutException:
            raise AdapterTransportError("provider_timeout") from None
        except httpx.RequestError:
            raise AdapterTransportError("provider_transport_error") from None
        except Exception:  # noqa: BLE001 - Mock callbacks must not expose arbitrary transport exception text.
            raise AdapterTransportError("provider_transport_error") from None
        try:
            result = _decode(raw)
        except (TypeError, ValueError, UnicodeError, RecursionError):
            raise AdapterResponseError("invalid_response_json") from None
        return _response_message(result)


class _BoundedResponseStream(httpx.SyncByteStream):
    """Apply task byte/deadline limits while the official SDK reads its response."""

    def __init__(self, stream: httpx.SyncByteStream, deadline: float):
        self.stream, self.deadline = stream, deadline

    def __iter__(self):
        size = 0
        for chunk in self.stream:
            if time.monotonic() >= self.deadline:
                raise AdapterTransportError("provider_timeout")
            size += len(chunk)
            if size > MAX_RESPONSE_BYTES:
                raise AdapterResponseError("response_byte_limit")
            yield chunk

    def close(self) -> None:
        self.stream.close()


class CloudChatCompletionsAdapter(ChatCompletionsAdapter):
    """Official OpenAI SDK Chat API with the host's durable budget boundary.

    The SDK owns credentials, Chat API invocation and response models. This
    wrapper only freezes supported task inputs, bounds I/O, and certifies the
    resulting text/tool/usage facts before the Agent consumes them. An exact
    MockTransport substitutes network I/O in offline acceptance.
    """

    def __init__(self, settings: ProviderSettings, config: CloudProviderConfig, *,
                 allow_network: bool = False, transport: httpx.MockTransport | None = None):
        if not isinstance(settings, ProviderSettings) or not isinstance(config, CloudProviderConfig):
            raise AdapterRequestError("invalid_cloud_configuration")
        if type(allow_network) is not bool:
            raise AdapterRequestError("invalid_network_permission")
        if transport is not None and type(transport) is not httpx.MockTransport:
            raise AdapterRequestError("unsupported_cloud_transport")
        self.settings = ProviderSettings.model_validate(settings.model_dump())
        self.config = CloudProviderConfig.model_validate(config.model_dump())
        self._allow_network = allow_network
        self._cloud_transport = transport
        self._readonly = False
        self._profile_override: dict[str, Any] | None = None

    def _validate_transport(self) -> None:
        # Structural validation remains useful for read-only recovery with networking
        # disabled. The actual permission and credential gate is repeated in invoke.
        if self._cloud_transport is not None and type(self._cloud_transport) is not httpx.MockTransport:
            raise AdapterRequestError("unsupported_cloud_transport")
        if type(self._allow_network) is not bool:
            raise AdapterRequestError("invalid_network_permission")

    def profile(self) -> dict[str, Any]:
        if self._profile_override is not None:
            return _decode(_canonical(self._profile_override))
        return {"schema_version": 2, "wire_profile": WIRE_PROFILE, "transport": "openai-sdk",
                "sdk": {"package": "openai", "version": version("openai"),
                        "api": "chat.completions", "request_identity": "canonical-json"},
                "endpoint": self.config.endpoint, "credential_env": self.config.credential_env,
                "settings": self.settings.model_dump(mode="json"),
                "max_request_bytes": MAX_REQUEST_BYTES, "max_response_bytes": MAX_RESPONSE_BYTES,
                "automatic_retries": 0, "follow_redirects": False, "trust_env": False,
                "input_token_estimator": INPUT_TOKEN_ESTIMATOR}

    def _credential(self) -> str:
        self._validate_transport()
        if self._readonly:
            raise AdapterRequestError("provider_readonly")
        if self._cloud_transport is None and not self._allow_network:
            raise AdapterRequestError("provider_network_disabled")
        if not self.config.endpoint.endswith("/chat/completions"):
            raise AdapterRequestError("provider_chat_endpoint_required")
        value = os.environ.get(self.config.credential_env)
        if value is None or not value:
            raise AdapterRequestError("provider_credential_missing")
        if len(value) > 8192 or any(ord(character) <= 32 or ord(character) >= 127 for character in value):
            raise AdapterRequestError("provider_credential_invalid")
        return value

    def reservation_tokens(self, request: PreparedRequest) -> int:
        """Conservative estimate plus the entire configured output allowance.

        UTF-8 wire bytes and framing deliberately overestimate common tokenizers;
        this does not certify any as-yet unverified provider's exact tokenizer.
        """
        validated = self.validate(request)
        return len(validated.body.encode("utf-8")) + 16 * len(validated.messages) + 64 + self.settings.max_output_tokens

    def admission(self, request: PreparedRequest, *, remaining_tokens: int,
                  remaining_seconds: float) -> CloudCallAdmission:
        """Check readiness and reserve a bounded single attempt before ledger dispatch."""
        validated = self.validate(request)
        reserved = self.reservation_tokens(validated)
        if type(remaining_tokens) is not int or remaining_tokens < reserved:
            raise AdapterRequestError("provider_token_budget")
        if (type(remaining_seconds) not in (int, float) or not math.isfinite(remaining_seconds) or
                remaining_seconds <= 0):
            raise AdapterRequestError("provider_time_budget")
        # Do not cache this value on the adapter or in admission. Repeat immediately
        # before sending so key removal/rotation cannot send a cached credential.
        credential = self._credential()
        if credential in validated.body or credential in _canonical(self.profile()):
            raise AdapterRequestError("provider_credential_exposed")
        return CloudCallAdmission(body_sha256=validated.body_sha256, profile_sha256=validated.profile_sha256,
                                  reserved_tokens=reserved,
                                  deadline=time.monotonic() + min(self.settings.timeout_seconds, remaining_seconds))

    def invoke(self, request: PreparedRequest, *, admission: CloudCallAdmission | None = None) -> AIMessage:
        validated = self.validate(request)
        # This ordering also makes a read-only reconstructed profile refuse before
        # constructing a client, even if the corresponding key is present.
        credential = self._credential()
        try:
            if admission is None:
                raise AdapterRequestError("provider_budget_required")
            admitted = CloudCallAdmission.model_validate(admission.model_dump())
            if (admitted.body_sha256 != validated.body_sha256 or
                    admitted.profile_sha256 != validated.profile_sha256 or
                    admitted.reserved_tokens != self.reservation_tokens(validated)):
                raise AdapterRequestError("provider_admission_changed")
        except AdapterError:
            raise
        except (AttributeError, TypeError, ValueError):
            raise AdapterRequestError("provider_admission_changed") from None
        timeout = min(self.settings.timeout_seconds, admitted.deadline - time.monotonic())
        if timeout <= 0:
            raise AdapterRequestError("provider_time_budget")
        if credential in validated.body or credential in _canonical(self.profile()):
            raise AdapterRequestError("provider_credential_exposed")
        from openai import APIConnectionError, APIStatusError, APITimeoutError, OpenAI

        def check_request(outgoing: httpx.Request) -> None:
            remaining = admitted.deadline - time.monotonic()
            if remaining <= 0:
                raise AdapterRequestError("provider_time_budget")
            # SDK setup/import time belongs to the original task allowance. Do
            # not dispatch with the stale timeout computed before that setup.
            outgoing.extensions["timeout"] = {key: min(value, remaining) for key, value in
                                               outgoing.extensions["timeout"].items()}
            try:
                if (outgoing.method != "POST" or str(outgoing.url) != self.config.endpoint or
                        len(outgoing.content) > MAX_REQUEST_BYTES or
                        _canonical(_decode(outgoing.content)) != validated.body):
                    raise AdapterRequestError("sdk_prepared_request_changed")
            except AdapterError:
                raise
            except (TypeError, ValueError, UnicodeError, RecursionError):
                raise AdapterRequestError("sdk_prepared_request_changed") from None

        def bound_response(incoming: httpx.Response) -> None:
            # The SDK normally reads HTTP error bodies before raising. Refuse
            # those before that unbounded read or private error text can escape.
            if not 200 <= incoming.status_code < 300:
                incoming.close()
                raise AdapterTransportError("provider_http_error")
            if incoming.headers.get("Content-Encoding", "identity").lower() not in ("identity", ""):
                incoming.close()
                raise AdapterResponseError("unsupported_response_encoding")
            length = incoming.headers.get("Content-Length")
            if length is not None and length.isdecimal() and int(length) > MAX_RESPONSE_BYTES:
                incoming.close()
                raise AdapterResponseError("response_byte_limit")
            incoming.stream = _BoundedResponseStream(cast(httpx.SyncByteStream, incoming.stream), admitted.deadline)

        payload = _decode(validated.body)
        extra_body = {"thinking": payload.pop("thinking")} if "thinking" in payload else {}

        def sdk_client(http_client: httpx.Client) -> OpenAI:
            client = OpenAI(api_key=credential, base_url=self.config.endpoint[:-len("/chat/completions")],
                            organization="", project="", max_retries=0, timeout=timeout,
                            default_headers={"Accept-Encoding": "identity"}, http_client=http_client)
            # Explicit empty arguments prevent SDK environment fallback; public
            # None attributes then omit these unrelated optional vendor headers.
            client.organization = None
            client.project = None
            return client

        try:
            # No private SDK API or hand-written POST. The standard SDK resource
            # serializes the Chat API call; hooks verify its frozen JSON meaning.
            transport = self._cloud_transport or httpx.HTTPTransport(retries=0, trust_env=False)
            with (_private_sdk_call(),
                  httpx.Client(transport=transport, timeout=timeout, follow_redirects=False, trust_env=False,
                               event_hooks={"request": [check_request], "response": [bound_response]}) as http_client,
                  sdk_client(http_client) as client,
                  client.chat.completions.with_streaming_response.create(
                      **cast(Any, payload), extra_body=extra_body) as response):
                raw = response.read()
                if time.monotonic() >= admitted.deadline:
                    raise AdapterTransportError("provider_timeout")
                if credential.encode("utf-8") in raw:
                    raise AdapterResponseError("provider_credential_exposed")
                result = _decode(raw)
                if credential in _canonical(result):
                    raise AdapterResponseError("provider_credential_exposed")
                # SDK response models retain provider extension fields such as
                # DeepSeek reasoning_content. The raw certification also rejects
                # duplicate JSON keys and preserves invalid/missing usage facts.
                message = _response_message(result)
                sdk_result = response.parse().model_dump(mode="json", exclude_unset=True)
                if _canonical(sdk_result) != _canonical(result):
                    raise AdapterResponseError("sdk_response_changed")
                return message
        except AdapterError:
            raise
        except (APITimeoutError, httpx.TimeoutException):
            raise AdapterTransportError("provider_timeout") from None
        except APIConnectionError as error:
            if isinstance(error.__cause__, AdapterError):
                raise error.__cause__ from None
            raise AdapterTransportError("provider_transport_error") from None
        except APIStatusError:
            raise AdapterTransportError("provider_http_error") from None
        except (TypeError, ValueError, UnicodeError, RecursionError):
            raise AdapterResponseError("invalid_response_json") from None
        except Exception:  # noqa: BLE001 - Provider internals may contain private body/header text.
            raise AdapterTransportError("provider_transport_error") from None


def adapter_from_profile(profile: dict[str, Any]) -> ChatCompletionsAdapter:
    """Reconstruct a non-dispatching adapter for request/source verification only."""
    try:
        value = _plain_json(profile)
        settings = ProviderSettings.model_validate(value["settings"])
        if value["transport"] == "mock-only":
            def never(_: httpx.Request) -> httpx.Response:
                raise AdapterRequestError("provider_readonly")

            adapter: ChatCompletionsAdapter = ChatCompletionsAdapter(settings, httpx.MockTransport(never))
        elif value["transport"] in ("cloud-httpx", "openai-sdk"):
            adapter = CloudChatCompletionsAdapter(settings, CloudProviderConfig(
                endpoint=value["endpoint"], credential_env=value["credential_env"]), allow_network=False)
            if value["transport"] == "cloud-httpx":
                expected = {"schema_version": 1, "wire_profile": WIRE_PROFILE, "transport": "cloud-httpx",
                            "endpoint": adapter.config.endpoint, "credential_env": adapter.config.credential_env,
                            "settings": settings.model_dump(mode="json"), "max_request_bytes": MAX_REQUEST_BYTES,
                            "max_response_bytes": MAX_RESPONSE_BYTES, "automatic_retries": 0,
                            "follow_redirects": False, "trust_env": False,
                            "input_token_estimator": INPUT_TOKEN_ESTIMATOR}
                if _canonical(value) != _canonical(expected):
                    raise ValueError("historical provider profile differs")
                adapter._profile_override = expected
            else:
                sdk = value.get("sdk")
                if not isinstance(sdk, dict) or set(sdk) != {"package", "version", "api", "request_identity"} or (
                        sdk["package"] != "openai" or sdk["api"] != "chat.completions" or
                        sdk["request_identity"] != "canonical-json" or not isinstance(sdk["version"], str) or
                        not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", sdk["version"])):
                    raise ValueError("SDK provider profile differs")
                expected = adapter.profile()
                expected["sdk"] = sdk
                if _canonical(expected) != _canonical(value):
                    raise ValueError("SDK provider profile differs")
                adapter._profile_override = expected
        else:
            raise ValueError("unsupported provider profile")
        if _canonical(adapter.profile()) != _canonical(value):
            raise ValueError("provider profile differs")
        adapter._readonly = True
        return adapter
    except (AdapterError, AttributeError, KeyError, TypeError, ValueError, UnicodeError, RecursionError):
        raise AdapterRequestError("unsupported_model_profile") from None


def _response_message(result: Any) -> AIMessage:
    if not isinstance(result, dict) or not isinstance(result.get("id"), str) or not result["id"] or (
        not isinstance(result.get("model"), str) or not result["model"]
    ):
        raise AdapterResponseError("invalid_response_identity")
    choices = result.get("choices")
    if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
        raise AdapterResponseError("invalid_response_choices")
    choice = choices[0]
    if type(choice.get("index")) is not int or choice["index"] != 0 or choice.get("finish_reason") not in (
        "stop", "tool_calls", "length", "content_filter", "insufficient_system_resource", "aborted"
    ):
        raise AdapterResponseError("invalid_response_finish")
    message = choice.get("message")
    if not isinstance(message, dict) or message.get("role") != "assistant" or "content" not in message or (
        message["content"] is not None and not isinstance(message["content"], str)
    ):
        raise AdapterResponseError("invalid_response_message")
    if set(message) - {"role", "content", "reasoning_content", "tool_calls", "refusal", "annotations",
                       "audio", "function_call"} or message.get("annotations") not in (None, []) or (
            message.get("audio") is not None or message.get("function_call") is not None):
        raise AdapterResponseError("unsupported_response_message_attachment")
    if message.get("refusal") is not None and not isinstance(message["refusal"], str):
        raise AdapterResponseError("invalid_response_refusal")
    kwargs: dict[str, Any] = {}
    if "reasoning_content" in message:
        if message["reasoning_content"] is not None and not isinstance(message["reasoning_content"], str):
            raise AdapterResponseError("invalid_response_reasoning")
        kwargs["reasoning_content"] = message["reasoning_content"]
    wire_calls = message.get("tool_calls")
    if wire_calls is None:
        wire_calls = []
    if not isinstance(wire_calls, list):
        raise AdapterResponseError("invalid_response_tool_calls")
    calls: list[Any] = []
    invalid: list[Any] = []
    seen_call_ids: set[str] = set()
    incomplete = choice["finish_reason"] in ("length", "content_filter", "insufficient_system_resource", "aborted")
    for call in wire_calls:
        function = call.get("function") if isinstance(call, dict) else None
        if not isinstance(function, dict) or set(function) != {"name", "arguments"} or (
            not isinstance(call, dict) or set(call) != {"id", "type", "function"} or call["type"] != "function" or
            not isinstance(call["id"], str) or not call["id"] or not isinstance(function["name"], str) or
            not function["name"] or not isinstance(function["arguments"], str)
        ):
            raise AdapterResponseError("invalid_response_tool_call")
        try:
            arguments = _decode(function["arguments"])
            if not isinstance(arguments, dict):
                raise TypeError("arguments are not an object")
        except (TypeError, ValueError, UnicodeError, RecursionError):
            invalid.append({"name": function["name"], "args": function["arguments"], "id": call["id"],
                            "error": "invalid_tool_arguments"})
            continue
        issue = ("incomplete_generation" if incomplete else
                 "unexpected_tool_finish" if choice["finish_reason"] != "tool_calls" else
                 "duplicate_tool_call_id" if call["id"] in seen_call_ids else None)
        seen_call_ids.add(call["id"])
        if issue is not None:
            invalid.append({"name": function["name"], "args": function["arguments"], "id": call["id"],
                            "error": issue})
        else:
            calls.append({"name": function["name"], "args": arguments, "id": call["id"]})
    if choice["finish_reason"] == "tool_calls" and not wire_calls:
        raise AdapterResponseError("missing_response_tool_calls")
    metadata: dict[str, Any] = {"model_name": result["model"], "finish_reason": choice["finish_reason"],
                               "wire_profile": WIRE_PROFILE}
    if message.get("refusal"):
        metadata["dfx_provider_refusal"] = message["refusal"]
        metadata["dfx_provider_incomplete"] = True
    if incomplete and not wire_calls:
        # A truncated text-only response must not be a normal SDK finish. This
        # marker applies to newly parsed responses, not historical observations.
        metadata["dfx_provider_incomplete"] = True
    usage = result.get("usage")
    usage_metadata: Any = None
    if usage is not None:
        metadata["dfx_provider_usage_raw"] = usage
        if isinstance(usage, Mapping):
            counts: dict[str, Any] = {"input_tokens": usage.get("prompt_tokens"),
                                      "output_tokens": usage.get("completion_tokens"),
                                      "total_tokens": usage.get("total_tokens")}
            if all(type(value) is int and value >= 0 for value in counts.values()) and (
                counts["total_tokens"] == counts["input_tokens"] + counts["output_tokens"]
            ):
                usage_metadata = counts
    return AIMessage(content=message["content"] or "", id=result["id"], additional_kwargs=kwargs,
                     tool_calls=calls, invalid_tool_calls=invalid, usage_metadata=usage_metadata,
                     response_metadata=metadata)
