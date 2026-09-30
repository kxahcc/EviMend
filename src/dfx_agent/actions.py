"""Host authorization and durable receipts for fixed-response SDK tool calls."""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Callable
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from typing import Any

from langchain.agents.middleware import AgentMiddleware, ToolCallRequest
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.types import Command
from pydantic import BaseModel, ValidationError

from .contracts import ActionProposal, authorize_action
from .diagnostics import AnalysisStore
from .model_calls import ModelCallStore, canonical, sha256
from .workspace import CandidateWorkspace

TOOL_ACTIONS: dict[str, tuple[str, str]] = {
    "read_log": ("inspect_existing", "read_log"),
    "list_source": ("inspect_existing", "read_source"),
    "read_source": ("inspect_existing", "read_source"),
    "inspect_analysis": ("inspect_existing", "read_source"),
    "record_diagnosis": ("record_analysis", "record_analysis"),
    "record_obligation": ("record_analysis", "record_analysis"),
    "link_failed_check": ("record_analysis", "record_analysis"),
    "consult_rule": ("inspect_existing", "read_source"),
    "plan_repair_action": ("inspect_existing", "read_source"),
    "edit_source": ("edit_draft", "edit_source"),
    "register_candidate": ("register_candidate", "register_candidate"),
    "run_check": ("run_verification", "verify_candidate"),
}
OFFLINE_CAPABILITIES = frozenset(capability for _, capability in TOOL_ACTIONS.values())


class ToolActionUncertain(RuntimeError):
    """A tool may have run; its effects must not be blindly repeated."""


class ToolActionStore:
    """Persist authorization before effects and a receipt before graph checkpointing."""

    def __init__(self, path: Path):
        self.path = path
        with closing(self._connect()) as connection, connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("""CREATE TABLE IF NOT EXISTS tool_actions (
                action_id TEXT PRIMARY KEY, request_sha256 TEXT NOT NULL,
                model_call_id TEXT NOT NULL, tool_name TEXT NOT NULL,
                proposal_json TEXT, rejection TEXT, state TEXT NOT NULL,
                message_json TEXT, message_sha256 TEXT)""")

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA synchronous=FULL")
        return connection

    def invoke(self, *, action_id: str, request_sha256: str, model_call_id: str,
               tool_name: str, tool_call_id: str, proposal: ActionProposal | None,
               rejection: str | None, max_tool_calls: int,
               handler: Callable[[], ToolMessage]) -> ToolMessage:
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM tool_actions WHERE action_id=?", (action_id,)).fetchone()
            if row is not None:
                if (row["request_sha256"] != request_sha256 or row["model_call_id"] != model_call_id or
                        row["tool_name"] != tool_name):
                    raise ValueError("tool call identity changed")
                if row["state"] != "completed":
                    raise ToolActionUncertain("tool result was not durably saved")
                if sha256(row["message_json"]) != row["message_sha256"]:
                    raise ValueError("tool receipt changed")
                message = ToolMessage.model_validate_json(row["message_json"])
                if message.tool_call_id != tool_call_id or message.name != tool_name:
                    raise ValueError("tool receipt belongs to another call")
                return message
            count = connection.execute("SELECT COUNT(*) FROM tool_actions").fetchone()[0]
            if count >= max_tool_calls:
                rejection = "budget_exhausted"
            connection.execute("INSERT INTO tool_actions VALUES (?,?,?,?,?,?,?,NULL,NULL)",
                               (action_id, request_sha256, model_call_id, tool_name,
                                proposal.model_dump_json() if proposal is not None else None,
                                rejection, "dispatched"))
        if rejection is not None:
            message = ToolMessage(content=f"rejected: {rejection}", tool_call_id=tool_call_id,
                                  name=tool_name, status="error")
        else:
            message = handler()
        self.complete(action_id, message)
        return message

    def complete(self, action_id: str, message: ToolMessage) -> None:
        payload = canonical(message.model_dump(mode="json"))
        with closing(self._connect()) as connection, connection:
            updated = connection.execute("UPDATE tool_actions SET state='completed', message_json=?, "
                                         "message_sha256=? WHERE action_id=? AND state='dispatched'",
                                         (payload, sha256(payload), action_id)).rowcount
            if updated != 1:
                raise ValueError("tool receipt has no dispatched action")

    def records(self) -> list[dict[str, Any]]:
        with closing(self._connect()) as connection:
            rows = connection.execute("SELECT * FROM tool_actions ORDER BY rowid").fetchall()
        return [{"action_id": row["action_id"], "model_call_id": row["model_call_id"],
                 "tool_name": row["tool_name"], "state": row["state"],
                 "authorization": "allowed" if row["rejection"] is None else "rejected",
                 "rejection": row["rejection"], "receipt_sha256": row["message_sha256"],
                 "proposal": json.loads(row["proposal_json"]) if row["proposal_json"] else None}
                for row in rows]


def _registered_references(workspace: CandidateWorkspace) -> tuple[set[str], set[str], set[str]]:
    evidence_ids, target_ids = AnalysisStore(workspace).reference_ids()
    candidates: set[str] = set()
    for path in workspace.candidates.iterdir():
        if not path.is_dir() or path.is_symlink():
            raise ValueError("candidate reference is unsafe")
        workspace.require_registered(path.name)
        candidates.add(path.name)
    return evidence_ids, target_ids, candidates


class ToolAuthorizationMiddleware(AgentMiddleware):
    """The synchronous fixed-response loop has no shell or device capabilities."""

    def __init__(self, workspace: CandidateWorkspace, model_calls: ModelCallStore,
                 actions: ToolActionStore, *, capabilities: frozenset[str], max_calls: int,
                 max_candidates: int, max_tool_calls: int, deadline: float):
        self.workspace, self.model_calls, self.actions = workspace, model_calls, actions
        self.capabilities = capabilities
        self.max_calls, self.max_candidates = max_calls, max_candidates
        self.max_tool_calls, self.deadline = max_tool_calls, deadline
        self.lock = RLock()

    def _parent_call(self, request: ToolCallRequest) -> str:
        messages = [item for item in request.state["messages"] if isinstance(item, AIMessage)]
        if not messages:
            raise ValueError("tool has no model response")
        call_id = messages[-1].response_metadata.get("dfx_model_call_id")
        if not isinstance(call_id, str):
            raise TypeError("tool response has no host call identity")
        parent = self.model_calls.get(call_id)
        if (parent is None or parent.state != "completed" or parent.intent.task_id != self.workspace.task_id or
                parent.intent.run_id != self.workspace.run_id):
            raise ValueError("tool has no accounted model response")
        saved, observation = self.model_calls.replay(parent.intent)
        if (saved.model_dump(exclude={"id"}) != messages[-1].model_dump(exclude={"id"}) or
                request.tool_call not in messages[-1].tool_calls or observation.format_status == "format_error"):
            raise ValueError("tool does not match the accounted response")
        return parent.intent.call_id

    def _proposal(self, action_id: str, request: ToolCallRequest) -> ActionProposal:
        name, args = request.tool_call["name"], request.tool_call["args"]
        action, capability = TOOL_ACTIONS[name]
        target_ids = ([args["diagnosis_id"]] if args.get("diagnosis_id") else [])
        for field in ("restores", "preserves", "violated_obligation_ids"):
            target_ids.extend(args.get(field) or [])
        evidence_ids = args.get("evidence_ids") or []
        candidate_id = args.get("candidate_id")
        payload = {"record_id": action_id, "created_at": datetime.now(UTC),
                   "task_id": self.workspace.task_id, "run_id": self.workspace.run_id,
                   "source_snapshot_sha256": self.workspace.source_snapshot_sha256,
                   "action": action, "required_capability": capability,
                   "reason": f"Model selected {name}", "estimated_cost_basis": "host-owned offline budget",
                   "target_ids": target_ids, "evidence_ids": evidence_ids, "candidate_id": candidate_id}
        if args.get("proposal_json") is not None:
            proposed = ActionProposal.model_validate_json(args["proposal_json"])
            if (proposed.action != action or proposed.required_capability != capability or
                    proposed.candidate_id != candidate_id):
                raise ValueError("proposal does not match the requested tool")
            payload.update(proposed.model_dump())
            payload.update(record_id=action_id, created_at=datetime.now(UTC),
                           target_ids=sorted(set(proposed.target_ids + target_ids)),
                           evidence_ids=sorted(set(proposed.evidence_ids + evidence_ids)))
        return ActionProposal.model_validate(payload)

    def wrap_tool_call(self, request: ToolCallRequest,
                       handler: Callable[[ToolCallRequest], ToolMessage | Command[Any]]) -> ToolMessage:
        with self.lock:
            parent_call = self._parent_call(request)
            name, tool_id = request.tool_call["name"], request.tool_call["id"]
            if not isinstance(tool_id, str) or not tool_id:
                raise ValueError("tool call has no stable identity")
            action_id = "action-" + sha256(canonical([self.workspace.task_id, self.workspace.run_id,
                                                    parent_call, tool_id]))
            fingerprint = sha256(canonical(request.tool_call))
            proposal: ActionProposal | None = None
            rejection: str | None = None
            try:
                if name not in TOOL_ACTIONS or request.tool is None:
                    rejection = "capability_unavailable"
                else:
                    schema = request.tool.args_schema
                    if not isinstance(schema, type) or not issubclass(schema, BaseModel):
                        raise ValueError("tool has no validated argument schema")
                    schema.model_validate(request.tool_call["args"])
                    proposal = self._proposal(action_id, request)
                    evidence, targets, candidates = _registered_references(self.workspace)
                    remaining = self.max_calls - self.model_calls.usage_summary(
                        self.workspace.task_id, self.workspace.run_id)["reserved_calls"]
                    authorization = authorize_action(
                        proposal, task_id=self.workspace.task_id, run_id=self.workspace.run_id,
                        source_snapshot_sha256=self.workspace.source_snapshot_sha256,
                        known_evidence_ids=evidence, known_target_ids=targets,
                        registered_candidate_ids=candidates, accepted_candidate_ids=set(),
                        capabilities=set(self.capabilities), remaining_model_calls=remaining,
                        remaining_probe_budget=0, model_response_reserved=True)
                    if not authorization.allowed:
                        rejection = authorization.reason
                    elif time.monotonic() >= self.deadline or (
                            name == "register_candidate" and len(candidates) >= self.max_candidates):
                        rejection = "budget_exhausted"
            except (ValueError, TypeError, KeyError, ValidationError):
                rejection = "invalid_proposal_or_arguments"

            def invoke() -> ToolMessage:
                result = handler(request)
                if not isinstance(result, ToolMessage):
                    raise ToolActionUncertain("offline tool returned an unsupported command")
                return result

            return self.actions.invoke(action_id=action_id, request_sha256=fingerprint,
                                       model_call_id=parent_call, tool_name=name, tool_call_id=tool_id,
                                       proposal=proposal, rejection=rejection,
                                       max_tool_calls=self.max_tool_calls, handler=invoke)
