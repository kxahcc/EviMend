"""Bounded fixed-response model for offline controller tests.

Provider adapters and real token accounting remain a later work package.
"""

from __future__ import annotations

import time
from typing import Any

from langchain_core.language_models.base import LangSmithParams
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field


class ModelLimit(RuntimeError):
    """The controller must terminate without another model request."""


class FixedResponseModel(BaseChatModel):
    model_name: str = "offline"
    responses: list[AIMessage]
    max_calls: int = Field(ge=1)
    max_known_tokens: int = Field(ge=1)
    deadline: float
    calls: int = 0
    turn_calls: int = 0
    known_tokens: int = 0
    missing_usage_calls: int = 0
    offered_tools: list[str] = Field(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "dfxfixed"

    def _get_ls_params(self, stop: list[str] | None = None, **kwargs: Any) -> LangSmithParams:
        return LangSmithParams(ls_provider="dfxfixed", ls_model_name=self.model_name)

    def bind_tools(self, tools: Any, **kwargs: Any) -> FixedResponseModel:
        for item in tools:
            name = item.name if hasattr(item, "name") else item.get("function", item).get("name")
            self.offered_tools.append(name)
        return self

    def _generate(self, messages: list[BaseMessage], stop: list[str] | None = None,
                  run_manager: Any = None, **kwargs: Any) -> ChatResult:
        if time.monotonic() >= self.deadline:
            raise ModelLimit("wall_time")
        if self.calls >= self.max_calls or self.known_tokens >= self.max_known_tokens:
            raise ModelLimit("model_budget")
        if self.turn_calls >= len(self.responses):
            raise ModelLimit("fixed_responses_exhausted")
        message = self.responses[self.turn_calls]
        self.calls += 1
        self.turn_calls += 1
        usage = message.usage_metadata
        if usage is None:
            self.missing_usage_calls += 1
        else:
            self.known_tokens += usage["total_tokens"]
        return ChatResult(generations=[ChatGeneration(message=message)])
