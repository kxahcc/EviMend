"""Accounted SDK models for fixed responses and mock-only wire requests.

Cloud transport and application experiments are outside this development slice.
"""

from __future__ import annotations

import time
from typing import Any

from langchain_core.language_models.base import LangSmithParams
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.utils.function_calling import convert_to_openai_tool
from pydantic import ConfigDict, Field

from .model_calls import (
    ModelCallBudget,
    ModelCallIntent,
    ModelCallStore,
    ModelCallUncertain,
    canonical,
    invoke_accounted,
    sha256,
)
from .model_provider import AdapterError, ChatCompletionsAdapter, PreparedRequest


class ModelLimit(RuntimeError):
    """The controller must terminate without another model request."""


MAX_SERIALIZED_MESSAGES_BYTES = 1_048_576


def context_profile() -> dict[str, Any]:
    """The finite SDK context policy is frozen alongside the model profile."""
    return {"max_serialized_messages_bytes": MAX_SERIALIZED_MESSAGES_BYTES, "automatic_summarization": False,
            "human_message_offload": False, "tool_result_offload": False, "implicit_model_retries": False}


def _check_context(messages: list[BaseMessage]) -> None:
    try:
        size = len(canonical([item.model_dump(mode="json") for item in messages]).encode("utf-8"))
    except (TypeError, ValueError, UnicodeError, RecursionError, OverflowError):
        raise ModelLimit("context_format_unsupported") from None
    if size > MAX_SERIALIZED_MESSAGES_BYTES:
        raise ModelLimit("context_limit")


class FixedResponseModel(BaseChatModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)
    model_name: str = "offline"
    responses: list[AIMessage]
    call_store: ModelCallStore = Field(exclude=True)
    task_id: str
    run_id: str
    max_calls: int = Field(ge=1)
    max_known_tokens: int = Field(ge=1)
    deadline: float
    calls: int = 0
    turn_calls: int = 0
    known_tokens: int = 0
    missing_usage_calls: int = 0
    offered_tools: list[str] = Field(default_factory=list)
    bound_tool_descriptors: list[dict[str, Any]] = Field(default_factory=list, exclude=True)
    bound_tool_options: dict[str, Any] = Field(default_factory=dict, exclude=True)

    @property
    def _llm_type(self) -> str:
        return "dfxfixed"

    def _get_ls_params(self, stop: list[str] | None = None, **kwargs: Any) -> LangSmithParams:
        return LangSmithParams(ls_provider="dfxfixed", ls_model_name=self.model_name)

    def bind_tools(self, tools: Any, **kwargs: Any) -> FixedResponseModel:
        descriptors: list[dict[str, Any]] = []
        for item in tools:
            name = item.name if hasattr(item, "name") else item.get("function", item).get("name")
            self.offered_tools.append(name)
            if hasattr(item, "name"):
                descriptors.append({"name": name, "description": item.description, "args": item.args})
            else:
                descriptors.append({"name": name, "definition": item})
        self.bound_tool_descriptors = descriptors
        self.bound_tool_options = kwargs
        return self

    def _intent(self, messages: list[BaseMessage], stop: list[str] | None,
                kwargs: dict[str, Any]) -> ModelCallIntent:
        return ModelCallIntent(
            call_id=f"model-{self.turn_calls + 1}", task_id=self.task_id, run_id=self.run_id,
            kind="solver", provider_id="dfxfixed", model_id=self.model_name,
            prompt_sha256=sha256(canonical([item.model_dump(mode="json") for item in messages])),
            tool_schema_sha256=sha256(canonical(self.bound_tool_descriptors)),
            parameters_sha256=sha256(canonical({"stop": stop, "generate_kwargs": kwargs,
                                                "bind_kwargs": self.bound_tool_options})),
            tool_names=tuple(item["name"] for item in self.bound_tool_descriptors),
        )

    def _generate(self, messages: list[BaseMessage], stop: list[str] | None = None,
                  run_manager: Any = None, **kwargs: Any) -> ChatResult:
        if time.monotonic() >= self.deadline:
            raise ModelLimit("wall_time")
        _check_context(messages)
        intent = self._intent(messages, stop, kwargs)
        existing = self.call_store.get(intent.call_id)
        if existing is None and (self.calls >= self.max_calls or self.known_tokens >= self.max_known_tokens):
            raise ModelLimit("model_budget")
        if (existing is None or existing.state == "reserved") and self.turn_calls >= len(self.responses):
            raise ModelLimit("fixed_responses_exhausted")
        try:
            def provide() -> AIMessage:
                response = self.responses[self.turn_calls].model_copy(deep=True)
                response.response_metadata = {**response.response_metadata, "dfx_model_call_id": intent.call_id}
                return response

            message, _ = invoke_accounted(self.call_store, intent, provide, max_calls=self.max_calls)
        except ModelCallBudget as exc:
            raise ModelLimit("model_budget") from exc
        finally:
            summary = self.call_store.usage_summary(self.task_id, self.run_id)
            self.calls = summary["reserved_calls"]
            self.known_tokens = summary["known_total_tokens"]
            self.missing_usage_calls = summary["missing_usage_calls"] + summary["invalid_usage_calls"]
        self.turn_calls += 1
        return ChatResult(generations=[ChatGeneration(message=message)])


class AccountedProviderModel(FixedResponseModel):
    """Reuse the SDK/budget fields with an explicit mock-only wire adapter."""

    adapter: ChatCompletionsAdapter = Field(exclude=True)
    request_error_reason: str | None = None

    @property
    def _llm_type(self) -> str:
        return "dfxmock"

    def _get_ls_params(self, stop: list[str] | None = None, **kwargs: Any) -> LangSmithParams:
        return LangSmithParams(ls_provider="dfxmock", ls_model_name=self.adapter.settings.model_id)

    def bind_tools(self, tools: Any, **kwargs: Any) -> AccountedProviderModel:
        descriptors = [convert_to_openai_tool(item) for item in tools]
        self.offered_tools.extend(item["function"]["name"] for item in descriptors)
        self.bound_tool_descriptors = descriptors
        # LangChain explicitly passes None for its unspecified tool-choice default.
        self.bound_tool_options = {key: value for key, value in kwargs.items()
                                   if not (key == "tool_choice" and value is None)}
        return self

    def _request_intent(self, request: PreparedRequest) -> ModelCallIntent:
        return ModelCallIntent(
            call_id=f"model-{self.turn_calls + 1}", task_id=self.task_id, run_id=self.run_id,
            kind="solver", provider_id=self.adapter.settings.provider_id, model_id=self.adapter.settings.model_id,
            prompt_sha256=sha256(canonical(request.messages)),
            tool_schema_sha256=sha256(canonical(request.tools)),
            parameters_sha256=sha256(canonical({"wire_parameters": request.parameters,
                                                "body_sha256": request.body_sha256,
                                                "profile_sha256": request.profile_sha256})),
            tool_names=tuple(item["function"]["name"] for item in request.tools),
        )

    def _generate(self, messages: list[BaseMessage], stop: list[str] | None = None,
                  run_manager: Any = None, **kwargs: Any) -> ChatResult:
        if time.monotonic() >= self.deadline:
            raise ModelLimit("wall_time")
        _check_context(messages)
        try:
            request = self.adapter.prepare(messages, self.bound_tool_descriptors, stop=stop,
                                           bind_options=self.bound_tool_options, generate_options=kwargs)
        except AdapterError as exc:
            self.request_error_reason = exc.reason
            raise ModelLimit("provider_request_unsupported") from exc
        intent = self._request_intent(request)
        if self.call_store.get(intent.call_id) is None and (
                self.calls >= self.max_calls or self.known_tokens >= self.max_known_tokens):
            raise ModelLimit("model_budget")

        def provide() -> AIMessage:
            message = self.adapter.invoke(request).model_copy(deep=True)
            message.response_metadata = {**message.response_metadata, "dfx_model_call_id": intent.call_id}
            return message

        try:
            message, observation = invoke_accounted(self.call_store, intent, provide, max_calls=self.max_calls)
        except ModelCallBudget as exc:
            raise ModelLimit("model_budget") from exc
        except AdapterError as exc:
            self.request_error_reason = exc.reason
            raise ModelCallUncertain("provider request has no saved response") from exc
        finally:
            usage = self.call_store.usage_summary(self.task_id, self.run_id)
            self.calls, self.known_tokens = usage["reserved_calls"], usage["known_total_tokens"]
            self.missing_usage_calls = usage["missing_usage_calls"] + usage["invalid_usage_calls"]
        self.turn_calls += 1
        if observation.format_status == "format_error":
            raise ModelLimit("provider_format_error")
        return ChatResult(generations=[ChatGeneration(message=message)])
