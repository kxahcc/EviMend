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

from .actions import ToolActionStore
from .context_compaction import (
    SUMMARY_PROMPT,
    CompactionPlan,
    CompactionPolicy,
    CompactionStore,
    ContextCompactionError,
)
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
from .working_context import WorkingContextError
from .working_context_plan import (
    WORKING_CONTEXT_PROMPT,
    WorkingContextPlan,
    WorkingContextPolicy,
    WorkingContextStore,
)


class ModelLimit(RuntimeError):
    """The controller must terminate without another model request."""


MAX_SERIALIZED_MESSAGES_BYTES = 1_048_576


def context_profile(policy: CompactionPolicy | None = None,
                    working_context: WorkingContextPolicy | None = None) -> dict[str, Any]:
    """The finite SDK context policy is frozen alongside the model profile."""
    return {"max_serialized_messages_bytes": MAX_SERIALIZED_MESSAGES_BYTES, "automatic_summarization": False,
            "human_message_offload": False, "tool_result_offload": False, "implicit_model_retries": False,
            "host_compaction": (policy or CompactionPolicy()).model_dump(mode="json"),
            "compaction_prompt_sha256": sha256(SUMMARY_PROMPT),
            "host_working_context": (working_context or WorkingContextPolicy()).model_dump(mode="json"),
            "working_context_prompt_sha256": sha256(WORKING_CONTEXT_PROMPT)}


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
    compactor: CompactionStore | None = Field(default=None, exclude=True)
    compaction_responses: list[AIMessage] = Field(default_factory=list, exclude=True)
    active_compaction: CompactionPlan | None = Field(default=None, exclude=True)
    working_context_store: WorkingContextStore | None = Field(default=None, exclude=True)
    action_store: ToolActionStore | None = Field(default=None, exclude=True)
    active_working_context: WorkingContextPlan | None = Field(default=None, exclude=True)
    active_view: list[BaseMessage] = Field(default_factory=list, exclude=True)
    active_fixed_request: dict[str, Any] | None = Field(default=None, exclude=True)

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
        self.active_fixed_request = {
            "messages": [item.model_dump(mode="json") for item in messages],
            "tools": self.bound_tool_descriptors,
            "parameters": {"stop": stop, "generate_kwargs": kwargs, "bind_kwargs": self.bound_tool_options},
        }
        return ModelCallIntent(
            call_id=f"model-{self.turn_calls + 1}", task_id=self.task_id, run_id=self.run_id,
            kind="solver", provider_id="dfxfixed", model_id=self.model_name,
            prompt_sha256=sha256(canonical([item.model_dump(mode="json") for item in messages])),
            tool_schema_sha256=sha256(canonical(self.bound_tool_descriptors)),
            parameters_sha256=sha256(canonical({"stop": stop, "generate_kwargs": kwargs,
                                                "bind_kwargs": self.bound_tool_options})),
            tool_names=tuple(item["name"] for item in self.bound_tool_descriptors),
        )

    def _refresh_usage(self) -> None:
        usage = self.call_store.usage_summary(self.task_id, self.run_id)
        self.calls, self.known_tokens = usage["reserved_calls"], usage["known_total_tokens"]
        self.missing_usage_calls = usage["missing_usage_calls"] + usage["invalid_usage_calls"]

    def _guard_budget(self, intent: ModelCallIntent) -> None:
        self._refresh_usage()
        existing = self.call_store.get(intent.call_id)
        if (existing is None and self.calls >= self.max_calls) or (
            (existing is None or existing.state == "reserved") and self.known_tokens >= self.max_known_tokens
        ):
            raise ModelLimit("model_budget")
        if time.monotonic() >= self.deadline:
            raise ModelLimit("wall_time")

    def _summary(self, plan: CompactionPlan) -> AIMessage:
        intent = plan.summary_intent()
        self._guard_budget(intent)
        records = self.call_store.records(self.task_id, self.run_id, kind="context_compaction")
        existing = self.call_store.get(intent.call_id)
        index = next((index for index, item in enumerate(records) if item.intent.call_id == intent.call_id), len(records))
        if (existing is None or existing.state == "reserved") and index >= len(self.compaction_responses):
            raise ModelLimit("fixed_compaction_responses_exhausted")

        def provide() -> AIMessage:
            response = self.compaction_responses[index].model_copy(deep=True)
            response.response_metadata = {**response.response_metadata, "dfx_model_call_id": intent.call_id}
            return response

        try:
            message, _ = invoke_accounted(self.call_store, intent, provide, max_calls=self.max_calls)
            return message
        except ModelCallBudget as exc:
            raise ModelLimit("model_budget") from exc
        finally:
            self._refresh_usage()

    def _view(self, messages: list[BaseMessage]) -> list[BaseMessage]:
        self.active_compaction = None
        self.active_working_context = None
        if self.working_context_store is not None:
            if self.action_store is None:
                raise ModelLimit("working_context_source_invalid")
            try:
                self.active_working_context = self.working_context_store.select(
                    messages, f"model-{self.turn_calls + 1}", self.call_store, self.action_store)
                if self.active_working_context is not None:
                    messages = self.active_working_context.view_messages()
            except WorkingContextError as exc:
                raise ModelLimit(exc.reason) from exc
        if self.compactor is not None:
            try:
                plan = self.compactor.select(messages, f"model-{self.turn_calls + 1}")
                if plan is not None:
                    message = self._summary(plan)
                    messages = self.compactor.complete(plan, message, self.call_store)
                    self.active_compaction = plan
            except ContextCompactionError as exc:
                raise ModelLimit(exc.reason) from exc
        _check_context(messages)
        self.active_view = [item.model_copy(deep=True) for item in messages]
        return messages

    def _reserve_solver(self, intent: ModelCallIntent, request: PreparedRequest | None = None) -> None:
        self._guard_budget(intent)
        try:
            self.call_store.reserve(intent, max_calls=self.max_calls)
            if self.active_compaction is not None and self.compactor is not None:
                self.compactor.link(self.active_compaction, intent, request)
            if self.active_working_context is not None and self.working_context_store is not None:
                self.working_context_store.link(
                    self.active_working_context, intent, request,
                    view_messages=self.active_view, compaction=self.active_compaction,
                    fixed_request=self.active_fixed_request)
        except ModelCallBudget as exc:
            raise ModelLimit("model_budget") from exc
        except ContextCompactionError as exc:
            raise ModelLimit(exc.reason) from exc
        except WorkingContextError as exc:
            raise ModelLimit(exc.reason) from exc
        finally:
            self._refresh_usage()

    def _generate(self, messages: list[BaseMessage], stop: list[str] | None = None,
                  run_manager: Any = None, **kwargs: Any) -> ChatResult:
        if time.monotonic() >= self.deadline:
            raise ModelLimit("wall_time")
        messages = self._view(messages)
        intent = self._intent(messages, stop, kwargs)
        existing = self.call_store.get(intent.call_id)
        self._guard_budget(intent)
        if (existing is None or existing.state == "reserved") and self.turn_calls >= len(self.responses):
            raise ModelLimit("fixed_responses_exhausted")
        self._reserve_solver(intent)
        try:
            def provide() -> AIMessage:
                response = self.responses[self.turn_calls].model_copy(deep=True)
                response.response_metadata = {**response.response_metadata, "dfx_model_call_id": intent.call_id}
                return response

            message, _ = invoke_accounted(self.call_store, intent, provide, max_calls=self.max_calls)
        except ModelCallBudget as exc:
            raise ModelLimit("model_budget") from exc
        finally:
            self._refresh_usage()
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

    def _summary(self, plan: CompactionPlan) -> AIMessage:
        try:
            request = plan.summary_request()
            if request is None:
                raise ModelLimit("context_format_unsupported")
        except AdapterError as exc:
            self.request_error_reason = exc.reason
            raise ModelLimit("context_compaction_input_limit") from exc
        intent = plan.summary_intent()
        self._guard_budget(intent)

        def provide() -> AIMessage:
            message = self.adapter.invoke(request).model_copy(deep=True)
            message.response_metadata = {**message.response_metadata, "dfx_model_call_id": intent.call_id}
            return message

        try:
            message, _ = invoke_accounted(self.call_store, intent, provide, max_calls=self.max_calls)
            return message
        except ModelCallBudget as exc:
            raise ModelLimit("model_budget") from exc
        except AdapterError as exc:
            self.request_error_reason = exc.reason
            raise ModelCallUncertain("compaction request has no saved response") from exc
        finally:
            self._refresh_usage()

    def _generate(self, messages: list[BaseMessage], stop: list[str] | None = None,
                  run_manager: Any = None, **kwargs: Any) -> ChatResult:
        if time.monotonic() >= self.deadline:
            raise ModelLimit("wall_time")
        messages = self._view(messages)
        try:
            request = self.adapter.prepare(messages, self.bound_tool_descriptors, stop=stop,
                                           bind_options=self.bound_tool_options, generate_options=kwargs)
        except AdapterError as exc:
            self.request_error_reason = exc.reason
            raise ModelLimit("provider_request_unsupported") from exc
        intent = self._request_intent(request)
        self._reserve_solver(intent, request)

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
            self._refresh_usage()
        self.turn_calls += 1
        if observation.format_status == "format_error":
            raise ModelLimit("provider_format_error")
        return ChatResult(generations=[ChatGeneration(message=message)])
