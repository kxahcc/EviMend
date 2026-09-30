"""Bounded fixed-response model with durable accounting for offline tests.

Provider adapters and real token accounting remain a later work package.
"""

from __future__ import annotations

import time
from typing import Any

from langchain_core.language_models.base import LangSmithParams
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import ConfigDict, Field

from .model_calls import (
    ModelCallBudget,
    ModelCallIntent,
    ModelCallStore,
    canonical,
    invoke_accounted,
    sha256,
)


class ModelLimit(RuntimeError):
    """The controller must terminate without another model request."""


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
