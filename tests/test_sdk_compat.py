"""Offline compatibility gate for the locked Deep Agents SDK, not the production repair loop."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from deepagents import (
    GeneralPurposeSubagentProfile,
    HarnessProfile,
    create_deep_agent,
    register_harness_profile,
)
from deepagents.backends import FilesystemBackend
from langchain_core.language_models.base import LangSmithParams
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables.config import RunnableConfig
from langchain_core.tools import tool
from langgraph.checkpoint.sqlite import SqliteSaver
from pydantic import Field


class FixedChatModel(BaseChatModel):
    model_name: str = "offline"
    responses: list[AIMessage]
    calls: int = 0
    offered_tools: list[str] = Field(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "dfxfixed"

    def _get_ls_params(self, stop: list[str] | None = None, **kwargs: Any) -> LangSmithParams:
        return LangSmithParams(ls_provider="dfxfixed", ls_model_name=self.model_name)

    def bind_tools(self, tools: Any, **kwargs: Any) -> FixedChatModel:
        for item in tools:
            name = item.name if hasattr(item, "name") else item.get("function", item).get("name")
            self.offered_tools.append(name)
        return self

    def _generate(self, messages: list[BaseMessage], stop: list[str] | None = None,
                  run_manager: Any = None, **kwargs: Any) -> ChatResult:
        if self.calls >= len(self.responses):
            raise AssertionError("SDK made an unplanned model call")
        message = self.responses[self.calls]
        self.calls += 1
        return ChatResult(generations=[ChatGeneration(message=message)])


def response(index: int, name: str | None = None, args: dict[str, Any] | None = None) -> AIMessage:
    calls = [] if name is None else [{"name": name, "args": args or {}, "id": f"call-{index}"}]
    return AIMessage(
        content="finished" if name is None else "",
        tool_calls=calls,
        usage_metadata={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
    )


def test_fixed_response_agent_contract(tmp_path: Path) -> None:
    register_harness_profile(
        "dfxfixed",
        HarnessProfile(
            excluded_tools=frozenset({"execute"}),
            general_purpose_subagent=GeneralPurposeSubagentProfile(enabled=False),
        ),
    )
    source = tmp_path / "sample.txt"
    source.write_text("value=bad\n", encoding="utf-8")
    digest = lambda value: hashlib.sha256(value.encode()).hexdigest()
    first_id = digest("value=almost\n")
    second_id = digest("value=good\n")
    candidates: dict[str, str] = {}
    checks: list[str] = []

    @tool
    def register_candidate() -> str:
        """Freeze the current sample contents and return its immutable candidate ID."""
        content = source.read_text(encoding="utf-8")
        candidate_id = digest(content)
        candidates[candidate_id] = content
        return candidate_id

    @tool
    def run_check(candidate_id: str) -> str:
        """Check a registered candidate against the fixed synthetic expectation."""
        if candidate_id not in candidates:
            return "rejected: candidate was not registered"
        if source.read_text(encoding="utf-8") != candidates[candidate_id]:
            return "rejected: candidate changed after registration"
        checks.append(candidate_id)
        return "pass" if candidates[candidate_id] == "value=good\n" else "fail: expected good"

    scripted = [
        response(1, "read_file", {"file_path": "/sample.txt"}),
        response(2, "edit_file", {"file_path": "/sample.txt", "old_string": "value=bad", "new_string": "value=almost"}),
        response(3, "register_candidate"),
        response(4, "run_check", {"candidate_id": first_id}),
        response(5, "edit_file", {"file_path": "/sample.txt", "old_string": "value=almost", "new_string": "value=good"}),
        response(6, "register_candidate"),
        response(7, "run_check", {"candidate_id": second_id}),
        response(8),
    ]
    model = FixedChatModel(responses=scripted)
    db_path = tmp_path / "sdk-checkpoints.sqlite"
    config: RunnableConfig = {"configurable": {"thread_id": "fixed-agent-contract"}, "recursion_limit": 32}
    with SqliteSaver.from_conn_string(str(db_path)) as saver:
        agent = create_deep_agent(
            model=model,
            tools=[register_candidate, run_check],
            backend=FilesystemBackend(root_dir=tmp_path, virtual_mode=True),
            checkpointer=saver,
            subagents=[],
        )
        result = agent.invoke({"messages": [{"role": "user", "content": "Repair sample.txt"}]}, config)
        assert model.calls == len(scripted)
        assert "task" not in model.offered_tools
        assert "execute" not in model.offered_tools
        assert {"read_file", "edit_file", "register_candidate", "run_check"} <= set(model.offered_tools)
        assert source.read_text(encoding="utf-8") == "value=good\n"
        assert checks == [first_id, second_id]
        assert list(candidates) == [first_id, second_id]
        ai_messages = [m for m in result["messages"] if isinstance(m, AIMessage)]
        assert len(ai_messages) == len(scripted)
        usages = [m.usage_metadata for m in ai_messages]
        assert all(usage is not None for usage in usages)
        assert sum(usage["total_tokens"] for usage in usages if usage is not None) == 120

    with SqliteSaver.from_conn_string(str(db_path)) as reopened:
        restored_agent = create_deep_agent(
            model=FixedChatModel(responses=[response(1)]),
            tools=[register_candidate, run_check],
            backend=FilesystemBackend(root_dir=tmp_path, virtual_mode=True),
            checkpointer=reopened,
            subagents=[],
        )
        state = restored_agent.get_state(config)
        assert len([m for m in state.values["messages"] if isinstance(m, AIMessage)]) == len(scripted)
        assert checks == [first_id, second_id]
