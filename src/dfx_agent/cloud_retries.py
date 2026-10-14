"""SDK-owned backoff with explicit safe retries and one cancellable deadline."""

from __future__ import annotations

import asyncio
import email.utils
import json
import math
import ssl
import time
from collections.abc import AsyncIterator, Callable
from typing import Any, cast

import httpx
from openai import (
    APIConnectionError,
    APIResponseValidationError,
    APIStatusError,
    APITimeoutError,
    AsyncOpenAI,
)
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .progress import emit_progress


class CloudRetryPolicy(BaseModel):
    """Network policy, separate from Agent reasoning rounds and token limits."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, hide_input_in_errors=True)
    max_retries: int = Field(default=2, ge=0, le=5)
    request_timeout_seconds: float = Field(default=60.0, gt=0, le=600)
    connect_timeout_seconds: float = Field(default=10.0, gt=0, le=60)

    @model_validator(mode="after")
    def valid_timeouts(self) -> CloudRetryPolicy:
        if (not math.isfinite(self.request_timeout_seconds) or
                not math.isfinite(self.connect_timeout_seconds) or
                self.connect_timeout_seconds > self.request_timeout_seconds):
            raise ValueError("invalid retry timeouts")
        return self


class CloudRetryError(RuntimeError):
    """Fixed public failure category and counts; never provider exception text."""

    def __init__(self, reason: str, stats: dict[str, Any]):
        self.reason = reason
        self.stats = stats
        super().__init__(reason)


class _TerminalSignal(BaseException):
    """Escape the SDK's broad Exception retry catch; contained by this helper."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


class _Attempts:
    def __init__(self) -> None:
        self.entries: list[dict[str, Any]] = []

    def snapshot(self) -> dict[str, Any]:
        return {"network_attempts": len(self.entries),
                "network_retries": max(0, len(self.entries) - 1),
                "attempts": [dict(item) for item in self.entries]}


def _certificate_error(error: BaseException) -> bool:
    seen: set[int] = set()
    for _ in range(12):
        if isinstance(error, ssl.SSLCertVerificationError):
            return True
        if id(error) in seen:
            break
        seen.add(id(error))
        cause = error.__cause__ or error.__context__
        if cause is None:
            break
        error = cause
    return False


async def _close_stream(stream: httpx.AsyncByteStream | httpx.SyncByteStream) -> None:
    if isinstance(stream, httpx.AsyncByteStream):
        await stream.aclose()
    else:
        stream.close()


def _retry_delay(headers: httpx.Headers) -> float | None:
    """Inspect server waiting instructions; actual scheduling belongs to SDK."""
    milliseconds = headers.get("retry-after-ms")
    seconds = headers.get("retry-after")
    if any(value is not None and len(value) > 128 for value in (milliseconds, seconds)):
        return math.inf
    try:
        if milliseconds is not None:
            return float(milliseconds) / 1000
    except ValueError:
        pass
    try:
        if seconds is not None:
            return float(seconds)
    except ValueError:
        pass
    if seconds is not None:
        parsed = email.utils.parsedate_tz(seconds)
        if parsed is not None:
            return float(email.utils.mktime_tz(parsed) - time.time())
    return None


class _BoundedAsyncStream(httpx.AsyncByteStream):
    def __init__(self, stream: httpx.AsyncByteStream | httpx.SyncByteStream,
                 deadline: float, limit: int):
        self._stream = stream
        self._deadline = deadline
        self._limit = limit

    async def __aiter__(self) -> AsyncIterator[bytes]:
        count = 0

        def check(chunk: bytes) -> None:
            nonlocal count
            if time.monotonic() >= self._deadline:
                raise _TerminalSignal("provider_timeout")
            count += len(chunk)
            if count > self._limit:
                raise _TerminalSignal("response_byte_limit")

        if isinstance(self._stream, httpx.AsyncByteStream):
            async for chunk in self._stream:
                check(chunk)
                yield chunk
        else:
            # Compatibility for existing synchronous MockTransport fixtures;
            # the real HTTP transport always supplies an asynchronous stream.
            for chunk in self._stream:
                check(chunk)
                yield chunk

    async def aclose(self) -> None:
        await _close_stream(self._stream)


class _RetryTransport(httpx.AsyncBaseTransport):
    def __init__(self, delegate: httpx.AsyncBaseTransport, *, deadline: float,
                 verify_request: Callable[[httpx.Request], None],
                 max_response_bytes: int, max_attempts: int, attempts: _Attempts):
        self._delegate = delegate
        self._deadline = deadline
        self._verify_request = verify_request
        self._max_response_bytes = max_response_bytes
        self._max_attempts = max_attempts
        self._attempts = attempts

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        remaining = self._deadline - time.monotonic()
        if remaining <= 0:
            raise _TerminalSignal("provider_timeout")
        if len(self._attempts.entries) >= self._max_attempts:
            raise _TerminalSignal("provider_transport_error")
        try:
            self._verify_request(request)
        except Exception as error:  # noqa: BLE001 - Guard errors must bypass SDK's blanket retry boundary.
            reason = getattr(error, "reason", None)
            allowed = {"provider_time_budget", "sdk_prepared_request_changed", "provider_credential_exposed",
                       "provider_credential_missing", "provider_credential_changed", "provider_credential_invalid",
                       "provider_credential_file_invalid", "provider_credential_endpoint_changed"}
            raise _TerminalSignal(reason if reason in allowed else "sdk_prepared_request_changed") from None
        # The SDK supplies the public HTTPX timeout extension. Every attempt
        # shares the same remaining wall-clock allowance; connect is shorter.
        request.extensions["timeout"] = {
            name: min(value, remaining) if isinstance(value, (int, float)) else remaining
            for name, value in request.extensions.get("timeout", {}).items()}
        entry: dict[str, Any] = {"attempt": len(self._attempts.entries) + 1}
        self._attempts.entries.append(entry)
        emit_progress("model_network_attempt", network_attempts=len(self._attempts.entries),
                      network_retries=max(0, len(self._attempts.entries) - 1))
        try:
            response = await self._delegate.handle_async_request(request)
        except (httpx.ConnectError, httpx.ConnectTimeout) as error:
            if _certificate_error(error):
                entry["outcome"] = "certificate_error"
                raise _TerminalSignal("provider_transport_error") from None
            entry["outcome"] = "connect_timeout" if isinstance(error, httpx.ConnectTimeout) else "connect_error"
            # These failures occur before an HTTP inference request is sent.
            # The official SDK owns retry count, exponential backoff and jitter.
            raise
        except httpx.TimeoutException:
            entry["outcome"] = "execution_unknown"
            raise _TerminalSignal("provider_timeout") from None
        except asyncio.CancelledError:
            entry["outcome"] = "execution_unknown"
            raise
        except Exception:  # noqa: BLE001 - Unknown sends cannot be retried or expose transport exception text.
            entry["outcome"] = "execution_unknown"
            raise _TerminalSignal("provider_transport_error") from None

        try:
            return await self._handle_response(response, request, entry)
        except Exception:  # noqa: BLE001 - Response processing errors must not become another inference send.
            # Header inspection and closing rejected bodies are inside send(),
            # too. Their failures must not enter the SDK's blanket retry catch.
            raise _TerminalSignal("provider_transport_error") from None

    async def _handle_response(self, response: httpx.Response, request: httpx.Request,
                               entry: dict[str, Any]) -> httpx.Response:
        entry["http_status"] = response.status_code
        entry["outcome"] = "http_response"
        emit_progress("model_network_attempt", network_attempts=len(self._attempts.entries),
                      network_retries=max(0, len(self._attempts.entries) - 1), http_status=response.status_code)
        if not 200 <= response.status_code < 300:
            headers: dict[str, str] = {"content-type": "application/json"}
            if response.status_code == 429:
                # Explicit rejection can be retried. Do not read an arbitrary
                # private error body or let headers enable retries for 5xx.
                delay = _retry_delay(response.headers)
                if delay is not None and (not math.isfinite(delay) or delay > 60):
                    # The SDK falls back to its shorter default for delays over
                    # 60s. Stop instead of violating the server's requested wait.
                    entry["outcome"] = "retry_delay_unsupported"
                    await _close_stream(response.stream)
                    raise _TerminalSignal("provider_http_error")
                for name in ("retry-after", "retry-after-ms"):
                    value = response.headers.get(name)
                    if value is not None and len(value) <= 128:
                        headers[name] = value
                if response.headers.get("x-should-retry") == "false":
                    headers["x-should-retry"] = "false"
            else:
                headers["x-should-retry"] = "false"
            await _close_stream(response.stream)
            return httpx.Response(response.status_code, headers=headers, content=b"", request=request)
        if response.headers.get("content-encoding", "identity").lower() not in ("", "identity"):
            await _close_stream(response.stream)
            raise _TerminalSignal("unsupported_response_encoding")
        length = response.headers.get("content-length")
        if length is not None and length.isdecimal() and (len(length) > 20 or int(length) > self._max_response_bytes):
            await _close_stream(response.stream)
            raise _TerminalSignal("response_byte_limit")
        response.stream = _BoundedAsyncStream(response.stream, self._deadline, self._max_response_bytes)
        return response

    async def aclose(self) -> None:
        await self._delegate.aclose()


async def request_with_retries(*, payload: dict[str, Any], endpoint: str, credential: str,
                               policy: CloudRetryPolicy, deadline: float,
                               transport: httpx.MockTransport | None,
                               verify_request: Callable[[httpx.Request], None],
                               max_response_bytes: int) -> tuple[bytes, dict[str, Any], dict[str, Any]]:
    """One SDK call; cancel backoff and network awaits at the original deadline."""
    attempts = _Attempts()
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise CloudRetryError("provider_timeout", attempts.snapshot())
    try:
        async with asyncio.timeout(remaining):
            timeout = httpx.Timeout(policy.request_timeout_seconds, connect=policy.connect_timeout_seconds)
            delegate = transport or httpx.AsyncHTTPTransport(retries=0, trust_env=False)
            wrapper = _RetryTransport(delegate, deadline=deadline, verify_request=verify_request,
                                      max_response_bytes=max_response_bytes, max_attempts=policy.max_retries + 1,
                                      attempts=attempts)
            async with (httpx.AsyncClient(transport=wrapper, timeout=timeout,
                                          follow_redirects=False, trust_env=False) as http_client,
                        AsyncOpenAI(api_key=credential, base_url=endpoint[:-len("/chat/completions")],
                                    organization="", project="", max_retries=policy.max_retries,
                                    timeout=timeout, default_headers={"Accept-Encoding": "identity"},
                                    http_client=http_client) as client):
                client.organization = None
                client.project = None
                data = dict(payload)
                extra_body = {"thinking": data.pop("thinking")} if "thinking" in data else {}
                async with client.chat.completions.with_streaming_response.create(
                        **cast(Any, data), extra_body=extra_body) as response:
                    raw = await response.read()
                    if time.monotonic() >= deadline:
                        raise _TerminalSignal("provider_timeout")
                    # Certify JSON syntax before SDK parsing; the outer adapter
                    # still performs duplicate-key and exact protocol checks.
                    if not isinstance(json.loads(raw), dict):
                        raise _TerminalSignal("invalid_response_json")
                    sdk_result = (await response.parse()).model_dump(mode="json", exclude_unset=True)
                    return raw, sdk_result, attempts.snapshot()
    except _TerminalSignal as error:
        raise CloudRetryError(error.reason, attempts.snapshot()) from None
    except (TimeoutError, APITimeoutError, httpx.TimeoutException):
        raise CloudRetryError("provider_timeout", attempts.snapshot()) from None
    except APIStatusError:
        raise CloudRetryError("provider_http_error", attempts.snapshot()) from None
    except APIConnectionError:
        raise CloudRetryError("provider_transport_error", attempts.snapshot()) from None
    except (APIResponseValidationError, AttributeError):
        raise CloudRetryError("invalid_response_json", attempts.snapshot()) from None
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise CloudRetryError("invalid_response_json", attempts.snapshot()) from None
    except Exception:  # noqa: BLE001 - Provider internals may carry private response/header text.
        raise CloudRetryError("provider_transport_error", attempts.snapshot()) from None
    finally:
        emit_progress("model_network_finished", network_attempts=len(attempts.entries),
                      network_retries=max(0, len(attempts.entries) - 1))
