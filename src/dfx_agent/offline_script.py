"""Strict offline CLI scripts with immutable configuration and isolated mock response streams."""

from __future__ import annotations

import json
import math
import os
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import httpx
from langchain_core.messages import AIMessage
from pydantic import ValidationError

from .actions import TOOL_ACTIONS
from .context_compaction import CompactionPolicy
from .contracts import require_path
from .model_provider import (
    AdapterTransportError,
    ChatCompletionsAdapter,
    ProviderSettings,
)
from .policies import PolicyFeatures
from .verification import FixedTextExecutor

MAX_SCRIPT_BYTES = 1_048_576
LIMIT_NAMES = frozenset({"max_calls", "max_known_tokens", "max_seconds", "max_candidates", "max_tool_calls"})
USAGE_NAMES = frozenset({"input_tokens", "output_tokens", "total_tokens"})


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("offline script has duplicate JSON keys")
        value[key] = item
    return value


def _invalid_constant(value: str) -> None:
    raise ValueError("offline script has a non-finite JSON number")


def _finite_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("offline script has a non-finite JSON number")
    return number


def _safe_path(path: Path) -> os.stat_result:
    absolute = path.absolute()
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current /= part
        info = current.lstat()
        if (stat.S_ISLNK(info.st_mode) or
                getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)):
            raise ValueError("offline script path is linked")
    info = absolute.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError("offline script must be an unlinked regular file")
    if info.st_size > MAX_SCRIPT_BYTES:
        raise ValueError("offline script exceeds 1 MiB")
    return info


def _limits(value: object) -> dict[str, int | float]:
    if not isinstance(value, dict) or set(value) - LIMIT_NAMES:
        raise ValueError("invalid offline script limits")
    for name, number in value.items():
        if name == "max_seconds":
            try:
                valid = type(number) in (int, float) and number > 0 and math.isfinite(number)
            except OverflowError:
                valid = False
        else:
            valid = type(number) is int and number > 0
        if not valid:
            raise ValueError("invalid offline script limits")
    return dict(value)


def _usage(value: object) -> None:
    if value is None:
        return
    if (not isinstance(value, dict) or set(value) != USAGE_NAMES or
            any(type(number) is not int or number < 0 for number in value.values()) or
            value["total_tokens"] != value["input_tokens"] + value["output_tokens"]):
        raise ValueError("invalid fixed-response usage")


def _compaction(value: object, *, fixed_mode: bool) -> None:
    if not isinstance(value, dict) or "policy" not in value or set(value) - {"policy", "responses"}:
        raise ValueError("invalid offline context compaction fields")
    try:
        CompactionPolicy.model_validate(value["policy"])
    except ValidationError:
        raise ValueError("invalid offline context compaction policy") from None
    if "responses" not in value:
        return
    if not fixed_mode:
        raise ValueError("mock context compaction responses must use the shared wire stream")
    responses = value["responses"]
    if not isinstance(responses, list):
        raise ValueError("fixed compaction responses must be a list")  # noqa: TRY004 -- Keep loader's validation contract.
    for item in responses:
        if (not isinstance(item, dict) or set(item) != {"text", "usage"} or
                not isinstance(item["text"], str)):
            raise ValueError("invalid fixed compaction response fields")
        try:
            item["text"].encode("utf-8")
        except UnicodeError:
            raise ValueError("invalid fixed compaction response text") from None
        _usage(item["usage"])


def _validate(value: object) -> Literal["fixed_response", "mock_chat_completions"]:
    if not isinstance(value, dict) or type(value.get("schema_version")) is not int or value["schema_version"] not in (1, 2):
        raise ValueError("unsupported offline script schema")
    required = {"schema_version", "expected", "responses" if value["schema_version"] == 1 else "model"}
    if not required <= set(value) or set(value) - required - {"policy_features", "limits", "context_compaction"}:
        raise ValueError("invalid offline script fields")
    expected = value["expected"]
    if (not isinstance(expected, dict) or set(expected) != {"path", "text"} or
            not isinstance(expected["path"], str) or not isinstance(expected["text"], str)):
        raise ValueError("invalid synthetic expectation")
    try:
        require_path(expected["path"])
        if any(ord(character) < 32 or ord(character) == 127 for character in expected["path"]):
            raise ValueError("control character")
    except ValueError:
        raise ValueError("invalid synthetic expectation path") from None
    _limits(value.get("limits", {}))
    if "context_compaction" in value:
        _compaction(value["context_compaction"], fixed_mode=value["schema_version"] == 1)
    try:
        PolicyFeatures.model_validate(value.get("policy_features", {}))
    except ValidationError:
        raise ValueError("invalid offline policy features") from None
    if value["schema_version"] == 1:
        responses = value["responses"]
        if not isinstance(responses, list):
            raise ValueError("fixed responses must be a list")
        for item in responses:
            if not isinstance(item, dict) or set(item) != {"tool", "args", "usage"}:
                raise ValueError("invalid fixed-response fields")
            name, arguments = item["tool"], item["args"]
            if name is not None and (not isinstance(name, str) or name not in TOOL_ACTIONS):
                raise ValueError("invalid fixed-response tool")
            if not isinstance(arguments, dict) or (name is None and arguments):
                raise ValueError("invalid fixed-response arguments")
            _usage(item["usage"])
        return "fixed_response"
    model = value["model"]
    if (not isinstance(model, dict) or set(model) != {"mode", "settings", "responses"} or
            model["mode"] != "mock_chat_completions"):
        raise ValueError("invalid offline model configuration")
    try:
        ProviderSettings.model_validate(model["settings"])
    except ValidationError:
        raise ValueError("invalid offline model settings") from None
    if not isinstance(model["responses"], list) or any(not isinstance(item, dict) for item in model["responses"]):
        raise ValueError("mock wire responses must be a list of objects")
    return "mock_chat_completions"


@dataclass(frozen=True, slots=True)
class OfflineScript:
    """Only immutable JSON is stored; every returned model, message and option is fresh."""

    mode: Literal["fixed_response", "mock_chat_completions"]
    _document: str = field(repr=False)

    def executor(self) -> FixedTextExecutor:
        expected = json.loads(self._document)["expected"]
        return FixedTextExecutor(expected["path"], expected["text"])

    def features(self) -> PolicyFeatures:
        return PolicyFeatures.model_validate(json.loads(self._document).get("policy_features", {}))

    def options(self) -> dict[str, int | float]:
        return json.loads(self._document).get("limits", {})

    def compaction_policy(self) -> CompactionPolicy:
        value = json.loads(self._document).get("context_compaction", {})
        return CompactionPolicy.model_validate(value.get("policy", {}))

    def compaction_responses(self) -> list[AIMessage]:
        if self.mode != "fixed_response":
            return []
        value = json.loads(self._document).get("context_compaction", {})
        return [AIMessage(content=item["text"], usage_metadata=item["usage"])
                for item in value.get("responses", [])]

    def fixed_responses(self) -> list[AIMessage]:
        if self.mode != "fixed_response":
            raise ValueError("offline script mode does not provide fixed responses")
        found = []
        for index, item in enumerate(json.loads(self._document)["responses"], start=1):
            calls = [] if item["tool"] is None else [{"name": item["tool"], "args": item["args"], "id": f"call-{index}"}]
            found.append(AIMessage(content="done" if not calls else "", tool_calls=calls, usage_metadata=item["usage"]))
        return found

    def adapter(self, *, response_offset: int = 0) -> ChatCompletionsAdapter:
        if self.mode != "mock_chat_completions":
            raise ValueError("offline script mode does not provide a mock adapter")
        if type(response_offset) is not int or response_offset < 0:
            raise ValueError("mock response offset must be a nonnegative integer")
        model = json.loads(self._document)["model"]
        settings = ProviderSettings.model_validate(model["settings"])
        responses = iter(tuple(_canonical(item).encode("utf-8") for item in model["responses"][response_offset:]))

        def handle(request: httpx.Request) -> httpx.Response:
            try:
                body = next(responses)
            except StopIteration:
                raise AdapterTransportError("mock_responses_exhausted") from None
            return httpx.Response(200, content=body, headers={"Content-Type": "application/json"})

        return ChatCompletionsAdapter(settings, httpx.MockTransport(handle))


def load_offline_script(path: Path) -> OfflineScript:
    """Load a bounded schema-1 fixed script or schema-2 mock-only wire script."""
    before = _safe_path(path)
    with path.open("rb") as stream:
        opened = os.fstat(stream.fileno())
        raw = stream.read(MAX_SCRIPT_BYTES + 1)
    after = _safe_path(path)
    if ((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) !=
            (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns) or
            (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns) !=
            (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)):
        raise ValueError("offline script changed while loading")
    if len(raw) > MAX_SCRIPT_BYTES:
        raise ValueError("offline script exceeds 1 MiB")
    try:
        value: Any = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object,
                                parse_constant=_invalid_constant, parse_float=_finite_float)
        document = _canonical(value)
        document.encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise ValueError("invalid offline script JSON") from None
    mode = _validate(value)
    return OfflineScript(mode=mode, _document=document)
