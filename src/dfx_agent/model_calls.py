"""Isolated model-call boundary: durable intent, response, usage and uncertainty.

Fixed and mock-wire models share this boundary. Real provider transport is not
configured; future SDK-originated requests must use the same durable accounting.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable
from contextlib import closing
from pathlib import Path
from typing import Any, Literal, get_args

from langchain_core.messages import AIMessage
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .contracts import IDENTIFIER, require_hash
from .evidence_io import open_readonly_database


def canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class ModelCallIntent(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    schema_version: Literal[1] = 1
    call_id: str
    task_id: str
    run_id: str
    kind: Literal["solver", "format_repair", "context_compaction", "sdk_retry"]
    provider_id: str = Field(min_length=1, max_length=100)
    model_id: str = Field(min_length=1, max_length=200)
    prompt_sha256: str
    tool_schema_sha256: str
    parameters_sha256: str
    tool_names: tuple[str, ...]

    @model_validator(mode="after")
    def valid_intent(self) -> ModelCallIntent:
        for value in (self.call_id, self.task_id, self.run_id):
            if not IDENTIFIER.fullmatch(value):
                raise ValueError("invalid model call identity")
        for value in (self.prompt_sha256, self.tool_schema_sha256, self.parameters_sha256):
            require_hash(value)
        if len(set(self.tool_names)) != len(self.tool_names) or any(not IDENTIFIER.fullmatch(x) for x in self.tool_names):
            raise ValueError("invalid model tool names")
        return self

    def sha256(self) -> str:
        return sha256(canonical(self.model_dump(mode="json")))


class ModelUsage(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)


class ToolCallFingerprint(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    call_id: str | None
    name: str
    arguments_sha256: str | None

    @model_validator(mode="after")
    def valid_fingerprint(self) -> ToolCallFingerprint:
        if self.arguments_sha256 is not None:
            require_hash(self.arguments_sha256)
        return self


class ModelObservation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    schema_version: Literal[1] = 1
    request_sha256: str
    response_sha256: str
    provider_message_id: str | None
    format_status: Literal["tool_calls", "text_only", "format_error"]
    format_errors: tuple[str, ...]
    tool_calls: tuple[ToolCallFingerprint, ...]
    usage_status: Literal["known", "missing", "invalid"]
    usage: ModelUsage | None
    usage_raw_sha256: str | None

    @model_validator(mode="after")
    def valid_observation(self) -> ModelObservation:
        require_hash(self.request_sha256)
        require_hash(self.response_sha256)
        if self.usage_raw_sha256 is not None:
            require_hash(self.usage_raw_sha256)
        if (self.usage_status == "known") != (self.usage is not None):
            raise ValueError("usage status and normalized counts disagree")
        if (self.usage_status == "missing") != (self.usage_raw_sha256 is None):
            raise ValueError("usage status and raw metadata disagree")
        return self


def normalize_message(intent: ModelCallIntent, message: AIMessage) -> ModelObservation:
    """Classify a LangChain message without treating its claims as execution facts."""
    errors: list[str] = []
    calls: list[ToolCallFingerprint] = []
    seen_ids: set[str] = set()
    if message.invalid_tool_calls:
        errors.append("invalid_tool_call_syntax")
    for call in message.tool_calls:
        call_id = call.get("id")
        name = call.get("name", "")
        if not call_id:
            errors.append("missing_tool_call_id")
        elif call_id in seen_ids:
            errors.append("duplicate_tool_call_id")
        else:
            seen_ids.add(call_id)
        if name not in intent.tool_names:
            errors.append("unknown_tool_name")
        arguments = call.get("args")
        try:
            if not isinstance(arguments, dict):
                raise TypeError("tool arguments must be an object")
            arguments_hash = sha256(canonical(arguments))
        except (TypeError, ValueError):
            arguments_hash = None
            errors.append("invalid_tool_arguments")
        calls.append(ToolCallFingerprint(call_id=call_id, name=name, arguments_sha256=arguments_hash))
    if not calls and not message.content:
        errors.append("empty_response")
    provider_raw = message.response_metadata.get("dfx_provider_usage_raw")
    usage_raw: Any = provider_raw if provider_raw is not None else message.usage_metadata
    usage: ModelUsage | None = None
    usage_status: Literal["known", "missing", "invalid"] = "missing" if usage_raw is None else "invalid"
    usage_raw_sha = sha256(canonical(usage_raw)) if usage_raw is not None else None
    if usage_raw is not None:
        try:
            if provider_raw is not None:
                usage = ModelUsage.model_validate({"input_tokens": usage_raw["prompt_tokens"],
                                                   "output_tokens": usage_raw["completion_tokens"],
                                                   "total_tokens": usage_raw["total_tokens"]})
                if (usage.total_tokens != usage.input_tokens + usage.output_tokens or
                        message.usage_metadata is None or any(
                            message.usage_metadata[key] != usage.model_dump()[key] for key in
                            ("input_tokens", "output_tokens", "total_tokens"))):
                    raise ValueError("provider usage differs from normalized usage")
            else:
                usage = ModelUsage.model_validate({key: usage_raw[key] for key in
                                                   ("input_tokens", "output_tokens", "total_tokens")})
            usage_status = "known"
        except (KeyError, TypeError, ValueError):
            usage = None
    status: Literal["tool_calls", "text_only", "format_error"] = (
        "format_error" if errors else "tool_calls" if calls else "text_only")
    return ModelObservation(request_sha256=intent.sha256(),
                            response_sha256=sha256(canonical(message.model_dump(mode="json"))),
                            provider_message_id=message.id, format_status=status,
                            format_errors=tuple(dict.fromkeys(errors)), tool_calls=tuple(calls),
                            usage_status=usage_status, usage=usage, usage_raw_sha256=usage_raw_sha)


class ModelCallConflict(RuntimeError):
    """A call identity, transition or replay disagrees with durable state."""


class ModelCallBudget(RuntimeError):
    """The configured call-count budget has been consumed."""


class ModelCallUncertain(RuntimeError):
    """The provider may have processed this call; do not resend its identity."""


CallState = Literal["reserved", "dispatched", "completed", "unknown"]


class ModelCallRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    intent: ModelCallIntent
    state: CallState
    observation: ModelObservation | None
    uncertainty_reason: str | None


class ModelCallStore:
    """SQLite ledger; only opaque request hashes and completed responses are retained."""

    def __init__(self, path: Path, *, read_only: bool = False):
        if type(read_only) is not bool:
            raise ValueError("read_only must be a boolean")
        if not path.parent.is_dir():
            raise ValueError("model ledger parent must already exist")
        self.path, self.read_only = path, read_only
        if read_only:
            with closing(self._connect()) as connection:
                row = connection.execute(
                    "SELECT name FROM sqlite_master WHERE name='model_calls' AND type='table'").fetchone()
                if row is None:
                    raise ValueError("model ledger table is missing")
            return
        with closing(self._connect()) as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("""CREATE TABLE IF NOT EXISTS model_calls (
                call_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, run_id TEXT NOT NULL,
                request_sha256 TEXT NOT NULL, intent_json TEXT NOT NULL,
                state TEXT NOT NULL, observation_json TEXT, message_json TEXT,
                uncertainty_reason TEXT)""")
            connection.commit()

    def _connect(self) -> sqlite3.Connection:
        if self.read_only:
            return open_readonly_database(self.path)
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA synchronous=FULL")
        return connection

    @staticmethod
    def _record(row: sqlite3.Row) -> ModelCallRecord:
        intent = ModelCallIntent.model_validate_json(row["intent_json"])
        if (intent.sha256() != row["request_sha256"] or row["call_id"] != intent.call_id
                or row["task_id"] != intent.task_id or row["run_id"] != intent.run_id):
            raise ModelCallConflict("stored request identity changed")
        observation = (ModelObservation.model_validate_json(row["observation_json"])
                       if row["observation_json"] is not None else None)
        if observation is not None and observation.request_sha256 != intent.sha256():
            raise ModelCallConflict("stored observation belongs to another request")
        if row["state"] == "completed" and (observation is None or row["message_json"] is None):
            raise ModelCallConflict("completed model call has no durable response")
        if row["state"] != "completed" and (observation is not None or row["message_json"] is not None):
            raise ModelCallConflict("unfinished model call carries a response")
        return ModelCallRecord(intent=intent, state=row["state"], observation=observation,
                               uncertainty_reason=row["uncertainty_reason"])

    def get(self, call_id: str) -> ModelCallRecord | None:
        with closing(self._connect()) as connection:
            row = connection.execute("SELECT * FROM model_calls WHERE call_id=?", (call_id,)).fetchone()
        return self._record(row) if row is not None else None

    def records(self, task_id: str, run_id: str, *, kind: str | None = None) -> list[ModelCallRecord]:
        """Read every owned call in reservation order, optionally selecting its kind."""
        if any(not isinstance(value, str) or not IDENTIFIER.fullmatch(value) for value in (task_id, run_id)):
            raise ValueError("invalid model ledger owner")
        if kind is not None and kind not in get_args(ModelCallIntent.model_fields["kind"].annotation):
            raise ValueError("invalid model call kind")
        with closing(self._connect()) as connection:
            rows = connection.execute("SELECT call_id FROM model_calls WHERE task_id=? AND run_id=? ORDER BY rowid",
                                      (task_id, run_id)).fetchall()
        result: list[ModelCallRecord] = []
        for row in rows:
            record = self.get(row["call_id"])
            if record is None or record.intent.task_id != task_id or record.intent.run_id != run_id:
                raise ModelCallConflict("stored model call owner changed")
            if kind is None or record.intent.kind == kind:
                result.append(record)
        return result

    def reserve(self, intent: ModelCallIntent, *, max_calls: int) -> ModelCallRecord:
        if self.read_only:
            raise ValueError("model ledger is read-only")
        if max_calls < 1:
            raise ValueError("max_calls must be positive")
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM model_calls WHERE call_id=?", (intent.call_id,)).fetchone()
            if row is not None:
                record = self._record(row)
                if record.intent.sha256() != intent.sha256():
                    raise ModelCallConflict("call ID reused for a different request")
                return record
            count = connection.execute("SELECT COUNT(*) FROM model_calls WHERE task_id=? AND run_id=?",
                                       (intent.task_id, intent.run_id)).fetchone()[0]
            if count >= max_calls:
                raise ModelCallBudget("model call count exhausted")
            connection.execute("INSERT INTO model_calls VALUES (?,?,?,?,?,?,NULL,NULL,NULL)",
                               (intent.call_id, intent.task_id, intent.run_id, intent.sha256(),
                                canonical(intent.model_dump(mode="json")), "reserved"))
            connection.commit()
        record = self.get(intent.call_id)
        assert record is not None
        return record

    def dispatch(self, call_id: str) -> None:
        if self.read_only:
            raise ValueError("model ledger is read-only")
        with closing(self._connect()) as connection, connection:
            updated = connection.execute("UPDATE model_calls SET state='dispatched' "
                                         "WHERE call_id=? AND state='reserved'", (call_id,)).rowcount
            if updated != 1:
                raise ModelCallConflict("model call is not an undispatched reservation")

    def complete(self, intent: ModelCallIntent, message: AIMessage) -> ModelObservation:
        if self.read_only:
            raise ValueError("model ledger is read-only")
        observation = normalize_message(intent, message)
        message_json = canonical(message.model_dump(mode="json"))
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM model_calls WHERE call_id=?", (intent.call_id,)).fetchone()
            record = self._record(row) if row is not None else None
            if record is None or record.intent.sha256() != intent.sha256():
                raise ModelCallConflict("response has no matching request intent")
            assert row is not None
            if row["state"] == "completed":
                if row["message_json"] != message_json or record.observation != observation:
                    raise ModelCallConflict("same call ID has a different response")
                assert record.observation is not None
                return record.observation
            if row["state"] != "dispatched":
                raise ModelCallConflict("response was not dispatched")
            connection.execute("UPDATE model_calls SET state='completed', observation_json=?, message_json=? "
                               "WHERE call_id=?", (canonical(observation.model_dump(mode="json")),
                                                    message_json, intent.call_id))
        return observation

    def mark_unknown(self, call_id: str, reason: str) -> None:
        if self.read_only:
            raise ValueError("model ledger is read-only")
        if not reason or len(reason) > 200:
            raise ValueError("invalid uncertainty reason")
        with closing(self._connect()) as connection, connection:
            updated = connection.execute("UPDATE model_calls SET state='unknown', uncertainty_reason=? "
                                         "WHERE call_id=? AND state='dispatched'", (reason, call_id)).rowcount
            if updated != 1:
                raise ModelCallConflict("only dispatched calls can become unknown")

    def replay(self, intent: ModelCallIntent) -> tuple[AIMessage, ModelObservation]:
        with closing(self._connect()) as connection:
            row = connection.execute("SELECT * FROM model_calls WHERE call_id=?", (intent.call_id,)).fetchone()
        if row is None or row["state"] != "completed" or row["message_json"] is None:
            raise ModelCallUncertain("no completed response to replay")
        record = self._record(row)
        if record.intent.sha256() != intent.sha256() or record.observation is None:
            raise ModelCallConflict("replay request identity differs")
        message = AIMessage.model_validate_json(row["message_json"])
        if sha256(canonical(message.model_dump(mode="json"))) != record.observation.response_sha256:
            raise ModelCallConflict("stored response changed")
        if normalize_message(intent, message) != record.observation:
            raise ModelCallConflict("stored observation changed")
        return message, record.observation

    def usage_summary(self, task_id: str, run_id: str) -> dict[str, int]:
        with closing(self._connect()) as connection:
            rows = connection.execute("SELECT * FROM model_calls WHERE task_id=? AND run_id=?",
                                      (task_id, run_id)).fetchall()
        records = [self._record(row) for row in rows]
        known = [item.observation.usage for item in records if item.observation and item.observation.usage]
        return {"reserved_calls": len(records), "completed_calls": sum(item.state == "completed" for item in records),
                "uncertain_calls": sum(item.state in ("dispatched", "unknown") for item in records),
                "missing_usage_calls": sum(item.observation is not None and
                                            item.observation.usage_status == "missing" for item in records),
                "invalid_usage_calls": sum(item.observation is not None and
                                            item.observation.usage_status == "invalid" for item in records),
                "known_input_tokens": sum(item.input_tokens for item in known),
                "known_output_tokens": sum(item.output_tokens for item in known),
                "known_total_tokens": sum(item.total_tokens for item in known)}


def invoke_accounted(store: ModelCallStore, intent: ModelCallIntent, provider: Callable[[], AIMessage],
                     *, max_calls: int) -> tuple[AIMessage, ModelObservation]:
    """One logical call. A dispatched call is never silently sent again on resume."""
    state = store.reserve(intent, max_calls=max_calls).state
    if state == "completed":
        return store.replay(intent)
    if state != "reserved":
        raise ModelCallUncertain("provider result may exist; reconcile or use a new accounted call")
    store.dispatch(intent.call_id)
    try:
        message = provider()
        if not isinstance(message, AIMessage):
            raise TypeError("provider did not return an AIMessage")
    except Exception as exc:
        store.mark_unknown(intent.call_id, type(exc).__name__)
        raise
    return message, store.complete(intent, message)
