"""Freeze each solver's actual input before dispatch and verify visibility on read.

Only complete sent tool groups and a verified, fully retained working-context
panel confer reference visibility. Summary prose never confers that authority.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any
from urllib.parse import quote

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage

from .actions import ToolActionStore, ToolActionUncertain
from .context_compaction import (
    MAX_ARTIFACT_BYTES,
    CompactionPlan,
    CompactionPolicy,
    CompactionStore,
    _adapter,
    _groups,
    _load,
    _messages,
    _serialized,
)
from .contracts import require_hash
from .evidence_io import local_evidence_path
from .model_calls import (
    ModelCallConflict,
    ModelCallIntent,
    ModelCallStore,
    canonical,
    sha256,
)
from .model_provider import AdapterError, PreparedRequest
from .verification import CheckLedger
from .working_context import WorkingContextFocus, WorkingContextPacket
from .working_context_plan import (
    _PARENT,
    WorkingContextPlan,
    WorkingContextStore,
    _answer,
    _focus,
    _normal_message,
    _visible,
)
from .working_context_projection import SolverContextProjection
from .workspace import CandidateWorkspace

MAX_SOLVER_INPUT_BYTES = MAX_ARTIFACT_BYTES
MAX_SOLVER_INPUT_TOTAL_BYTES = 8 * MAX_ARTIFACT_BYTES
_FIELDS = frozenset({
    "schema_version", "task_id", "run_id", "source_snapshot_sha256", "parent_call_id",
    "solver_intent", "model_profile_json", "final_view_messages_json", "final_view_sha256",
    "fixed_request", "prepared_request", "working_context_plan_json", "compaction_json",
})
_FAILURES = (ValueError, TypeError, KeyError, OSError, sqlite3.Error, UnicodeError,
             RecursionError, ModelCallConflict, ToolActionUncertain, AdapterError)


class SolverVisibilityError(ValueError):
    """A fixed public category that never quotes private messages or source."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


def visibility_report_fields(view: dict[str, Any] | None) -> dict[str, Any]:
    """Bound report links; complete focus and receipts remain in audited inputs."""
    if view is None:
        return {"focus_sha256": None, "visible_action_count": None, "visible_actions_sha256": None}
    identifiers = [item["action"]["action_id"] for item in view["visible_receipts"]]
    return {"focus_sha256": sha256(canonical(view["focus"])),
            "visible_action_count": len(identifiers), "visible_actions_sha256": sha256(canonical(identifiers))}


def _live_read_connection(path: Path) -> sqlite3.Connection:
    """Read active ledgers without DDL, checkpoints, or ignoring their WAL."""
    path = local_evidence_path(path)
    uri = ("file://" + quote(str(path).replace("\\", "/"), safe="/:")
           if str(path).startswith("\\\\?\\") else path.as_uri())
    connection = sqlite3.connect(uri + "?mode=ro", uri=True, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


class _LiveReadModelCalls(ModelCallStore):
    def _connect(self) -> sqlite3.Connection:
        return _live_read_connection(self.path)


class _LiveReadActions(ToolActionStore):
    def _connect(self) -> sqlite3.Connection:
        return _live_read_connection(self.path)


class SolverVisibilityStore:
    def __init__(self, workspace: CandidateWorkspace, task_directory: Path | None,
                 ledger: CheckLedger | None, model_profile: dict[str, Any],
                 working_context_store: WorkingContextStore | None = None):
        self.workspace, self.task_directory, self.ledger = workspace, task_directory, ledger
        self.working_context_store = working_context_store
        try:
            self.io = CompactionStore(workspace.root, workspace.task_id, workspace.run_id,
                                      CompactionPolicy(), model_profile)
            self.profile_json = self.io.profile_json
            self.directory = workspace.root / "solver-inputs"
            self.io.directory = self.directory
        except _FAILURES as exc:
            raise SolverVisibilityError("applicability_input_invalid") from exc

    def _path(self, call_id: str) -> Path:
        if not isinstance(call_id, str) or not _PARENT.fullmatch(call_id):
            raise ValueError("invalid solver input identity")
        return self.directory / f"{call_id}.json"

    def _namespace(self) -> dict[str, int]:
        try:
            self.directory.lstat()
        except FileNotFoundError:
            return {}
        self.io._guard(self.directory, file=False)
        entries: dict[str, int] = {}
        total = 0
        for path in self.directory.iterdir():
            self.io._guard(path)
            parent = path.name.removesuffix(".json")
            if path.name != f"{parent}.json" or not _PARENT.fullmatch(parent):
                raise ValueError("unexpected solver input artifact")
            size = path.stat().st_size
            if size > MAX_SOLVER_INPUT_BYTES:
                raise SolverVisibilityError("applicability_input_limit")
            total += size
            if total > MAX_SOLVER_INPUT_TOTAL_BYTES:
                raise SolverVisibilityError("applicability_input_limit")
            entries[parent] = size
        return entries

    def _ledgers(self, calls: ModelCallStore, actions: ToolActionStore) -> None:
        if (calls.path != self.workspace.root / "model-calls.sqlite" or
                actions.path != self.workspace.root / "tool-actions.sqlite"):
            raise ValueError("solver input ledgers belong to another run")
        for path in (calls.path, actions.path):
            self.io._guard(path)
            for suffix in ("-wal", "-shm", "-journal"):
                sidecar = path.with_name(path.name + suffix)
                try:
                    sidecar.lstat()
                except FileNotFoundError:
                    continue
                self.io._guard(sidecar)

    def _value(self, intent: ModelCallIntent, view: list[BaseMessage],
               fixed_request: dict[str, Any] | None, request: PreparedRequest | None,
               plan: WorkingContextPlan | None, compaction: CompactionPlan | None) -> dict[str, Any]:
        intent = ModelCallIntent.model_validate_json(intent.model_dump_json())
        self._path(intent.call_id)
        if (intent.kind != "solver" or intent.task_id != self.workspace.task_id or
                intent.run_id != self.workspace.run_id):
            raise ValueError("solver input intent owner differs")
        view_json = _serialized(view)
        _groups(view)
        profile = _load(self.profile_json)
        if profile.get("transport") == "fixed_response":
            if (request is not None or not isinstance(fixed_request, dict) or
                    set(fixed_request) != {"messages", "tools", "parameters"}):
                raise ValueError("fixed solver input has no exact request")
            fixed_request = _load(canonical(fixed_request))
            if not isinstance(fixed_request, dict):
                raise ValueError("fixed solver request normalization lost its object")
            if (canonical(fixed_request["messages"]) != view_json or
                    intent.provider_id != profile["provider_id"] or intent.model_id != profile["model_id"] or
                    intent.prompt_sha256 != sha256(view_json) or
                    intent.tool_schema_sha256 != sha256(canonical(fixed_request["tools"])) or
                    intent.parameters_sha256 != sha256(canonical(fixed_request["parameters"])) or
                    intent.tool_names != tuple(item["name"] for item in fixed_request["tools"])):
                raise ValueError("fixed solver request differs from intent")
        else:
            if request is None or fixed_request is not None:
                raise ValueError("wire solver input has no exact request")
            request = PreparedRequest.model_validate_json(request.model_dump_json())
            options = {key: request.parameters[key] for key in ("tool_choice", "parallel_tool_calls")
                       if key in request.parameters}
            prepared = _adapter(profile).prepare(view, request.tools, stop=request.parameters.get("stop"),
                                                 generate_options=options)
            if (prepared != request or intent.prompt_sha256 != sha256(canonical(request.messages)) or
                    intent.tool_schema_sha256 != sha256(canonical(request.tools)) or
                    intent.parameters_sha256 != sha256(canonical({"wire_parameters": request.parameters,
                        "body_sha256": request.body_sha256, "profile_sha256": request.profile_sha256})) or
                    intent.tool_names != tuple(item["function"]["name"] for item in request.tools) or
                    intent.provider_id != profile["settings"]["provider_id"] or
                    intent.model_id != profile["settings"]["model_id"]):
                raise ValueError("wire solver request differs from intent")
        if plan is not None:
            plan = WorkingContextPlan.model_validate_json(plan.model_dump_json())
            if (plan.parent_call_id != intent.call_id or plan.task_id != intent.task_id or
                    plan.run_id != intent.run_id or plan.model_profile_json != self.profile_json or
                    plan.source_snapshot_sha256 != self.workspace.source_snapshot_sha256):
                raise ValueError("solver input working context belongs to another site")
            if compaction is None and plan.stage_messages_json != view_json:
                raise ValueError("solver input differs from working context stage")
        if compaction is not None:
            compaction = CompactionPlan.model_validate_json(compaction.model_dump_json())
            if (compaction.parent_call_id != intent.call_id or compaction.task_id != intent.task_id or
                    compaction.run_id != intent.run_id or compaction.model_profile_json != self.profile_json or
                    (plan is not None and compaction.source_messages_json != plan.stage_messages_json)):
                raise ValueError("solver input compaction belongs to another stage")
        return {"schema_version": 1, "task_id": intent.task_id, "run_id": intent.run_id,
            "source_snapshot_sha256": self.workspace.source_snapshot_sha256, "parent_call_id": intent.call_id,
            "solver_intent": intent.model_dump(mode="json"), "model_profile_json": self.profile_json,
            "final_view_messages_json": view_json, "final_view_sha256": sha256(view_json),
            "fixed_request": fixed_request, "prepared_request": None if request is None else request.model_dump(mode="json"),
            "working_context_plan_json": None if plan is None else plan.model_dump_json(),
            "compaction_json": None if compaction is None else compaction.model_dump_json()}

    def _history(self, messages: list[BaseMessage], parent: str, calls: ModelCallStore,
                 actions: ToolActionStore) -> list[dict[str, Any]]:
        _groups(messages)
        previous, limit = 0, int(parent[6:])
        for message in messages:
            if type(message) is not AIMessage:
                continue
            identifier = message.response_metadata.get("dfx_model_call_id")
            if not isinstance(identifier, str) or not _PARENT.fullmatch(identifier):
                raise ValueError("solver input history lacks an accounted solver")
            number = int(identifier[6:])
            record = calls.get(identifier)
            if (not previous < number < limit or record is None or record.state != "completed" or
                    record.intent.kind != "solver" or record.intent.task_id != self.workspace.task_id or
                    record.intent.run_id != self.workspace.run_id):
                raise ValueError("solver input history has nonhistorical solver sites")
            saved, observation = calls.replay(record.intent)
            if (_normal_message(message, saved) != canonical(saved.model_dump(mode="json")) or
                    observation.format_status == "format_error"):
                raise ValueError("solver input history differs from its model ledger")
            previous = number
        return _visible(messages, calls, actions, self.workspace)

    def _original(self, value: dict[str, Any]) -> tuple[
        ModelCallIntent, WorkingContextPlan | None, CompactionPlan | None, list[BaseMessage]
    ]:
        if set(value) != _FIELDS or type(value["schema_version"]) is not int or value["schema_version"] != 1:
            raise ValueError("unsupported solver input record")
        intent = ModelCallIntent.model_validate_json(canonical(value["solver_intent"]))
        request = (None if value["prepared_request"] is None else
                   PreparedRequest.model_validate_json(canonical(value["prepared_request"])))
        plan = (None if value["working_context_plan_json"] is None else
                WorkingContextPlan.model_validate_json(value["working_context_plan_json"]))
        compact = (None if value["compaction_json"] is None else
                   CompactionPlan.model_validate_json(value["compaction_json"]))
        view = _messages(value["final_view_messages_json"])
        if self._value(intent, view, value["fixed_request"], request, plan, compact) != value:
            raise ValueError("saved solver input changed")
        return intent, plan, compact, view

    def _working_original(self, plan: WorkingContextPlan) -> tuple[WorkingContextStore, dict[str, bytes]]:
        store = self.working_context_store or WorkingContextStore(self.workspace, None, None, plan.policy,
            _load(self.profile_json), format_version=plan.schema_version)
        saved, material = store._read_plan_material(store.directory / f"{plan.parent_call_id}.plan.json")
        if saved != plan:
            raise ValueError("solver input working context plan changed")
        return store, material

    def _checked(self, value: dict[str, Any], calls: ModelCallStore,
                 actions: ToolActionStore, *, require_completed: bool) -> dict[str, Any]:
        intent, plan, compact, view = self._original(value)
        self._ledgers(calls, actions)
        parent = calls.get(intent.call_id)
        if parent is None or parent.intent != intent or (require_completed and parent.state != "completed"):
            raise ValueError("solver input has no completed matching parent")
        if parent.state == "completed":
            calls.replay(intent)
        receipts = self._history(view, intent.call_id, calls, actions)
        packet, quoted = None, []
        if plan is not None:
            store, material = self._working_original(plan)
            store._check_receipts_material(plan, calls, actions, material)
            stage = plan.view_messages()
            pinned, _ = _groups(stage)
            # The exact startup retains the entire original panel, including
            # mandatory quoted groups. A fragment or summarized panel gives no authority.
            if _serialized(view[:pinned]) == _serialized(stage[:pinned]):
                packet = (WorkingContextPacket.model_validate_json(plan.packet_json).contents()
                          if plan.solver_projection_json is None else
                          SolverContextProjection.model_validate_json(plan.solver_projection_json).contents())
                original = _messages(plan.original_messages_json)
                ranges = _load(plan.selection_json)["required_unrepresented_ranges"]
                quoted = [{"range": pair, "messages": _load(_serialized(original[pair[0]:pair[1]]))}
                          for pair in ranges]
                quoted_starts = {pair[0] for pair in ranges}
                receipts.extend(item for item in _load(plan.visible_receipts_json)
                                if item["group_start"] in quoted_starts)
        if compact is not None:
            store = CompactionStore(self.workspace.root, intent.task_id, intent.run_id, compact.policy,
                                    _load(self.profile_json))
            saved = store._require_plan(compact)
            result = store._result(saved, calls)
            if (store._read(store.directory / f"{intent.call_id}.result.json") != result or
                    result["view_messages_json"] != value["final_view_messages_json"]):
                raise ValueError("solver input compaction result changed")
            self._history(compact.source_messages(), intent.call_id, calls, actions)
        by_id = {item["action"]["action_id"]: item for item in receipts}
        receipts = sorted(by_id.values(), key=lambda item: item["ledger_position"])
        focus = (_focus(receipts) if packet is None else
                 WorkingContextFocus.model_validate_json(canonical(packet["focus"])))
        diagnosis = None if packet is None else packet["diagnoses"].get(focus.diagnosis_id)
        for item in receipts:
            answer = _answer(item)
            if answer is None or not isinstance(answer.get("record"), dict):
                continue
            seen = answer["record"].get("diagnosis")
            tool = item["action"]["tool_name"]
            if (isinstance(seen, dict) and seen.get("record_id") == focus.diagnosis_id and
                    ((tool == "record_diagnosis" and answer.get("diagnosis_id") == focus.diagnosis_id) or
                     (tool == "inspect_analysis" and item["tool_call"]["args"].get("diagnosis_id") == focus.diagnosis_id))):
                diagnosis = seen
        selected = None if diagnosis is None else diagnosis.get("selected_hypothesis_id")
        if (not focus.hypothesis_ids and isinstance(selected, str) and diagnosis is not None and
                selected in {item["hypothesis_id"] for item in diagnosis.get("hypotheses", [])}):
            focus = WorkingContextFocus.model_validate_json(canonical({**focus.model_dump(mode="json"),
                                                                       "hypothesis_ids": [selected]}))
        return {**value, "model_profile": _load(self.profile_json),
            "request": {"fixed_request": value["fixed_request"], "prepared_request": value["prepared_request"]},
            "request_sha256": intent.sha256(), "solver_state": parent.state,
            "visible_receipts": receipts, "visible_packet": packet,
            "required_unrepresented_groups": quoted, "focus": focus.model_dump(mode="json"),
            "authority": "completed_solver_input" if parent.state == "completed" else "not_authoritative"}

    def capture(self, intent: ModelCallIntent, *, view_messages: list[BaseMessage],
                fixed_request: dict[str, Any] | None = None, prepared_request: PreparedRequest | None = None,
                working_context_plan: WorkingContextPlan | None = None,
                compaction: CompactionPlan | None = None) -> dict[str, Any]:
        try:
            entries = self._namespace()
            value = self._value(intent, view_messages, fixed_request, prepared_request, working_context_plan, compaction)
            for ledger_path in (self.workspace.root / "model-calls.sqlite", self.workspace.root / "tool-actions.sqlite"):
                self.io._guard(ledger_path)
            calls = _LiveReadModelCalls(self.workspace.root / "model-calls.sqlite", read_only=True)
            actions = _LiveReadActions(self.workspace.root / "tool-actions.sqlite", read_only=True)
            parent = calls.get(intent.call_id)
            path = self._path(intent.call_id)
            if parent is None or parent.intent != intent or (intent.call_id not in entries and parent.state != "reserved"):
                raise ValueError("new solver input needs an undispatched matching reservation")
            self._checked(value, calls, actions, require_completed=False)
            size = len(canonical(value).encode("utf-8"))
            if (size > MAX_SOLVER_INPUT_BYTES or
                    sum(entries.values()) - entries.get(intent.call_id, 0) + size > MAX_SOLVER_INPUT_TOTAL_BYTES):
                raise SolverVisibilityError("applicability_input_limit")
            self.io._save(path, value)
            return _load(canonical(value))
        except SolverVisibilityError:
            raise
        except _FAILURES as exc:
            raise SolverVisibilityError("applicability_input_invalid") from exc

    def read(self, call_id: str, model_calls: ModelCallStore, actions: ToolActionStore) -> dict[str, Any]:
        try:
            self._namespace()
            value = self.io._read(self._path(call_id))
            if value.get("parent_call_id") != call_id:
                raise ValueError("solver input filename differs")
            return self._checked(value, model_calls, actions, require_completed=True)
        except SolverVisibilityError:
            raise
        except _FAILURES as exc:
            raise SolverVisibilityError("applicability_input_invalid") from exc

    def audit(self, model_calls: ModelCallStore, actions: ToolActionStore) -> list[dict[str, Any]]:
        try:
            entries = self._namespace()
            self._ledgers(model_calls, actions)
            records = model_calls.records(self.workspace.task_id, self.workspace.run_id, kind="solver")
            if set(entries) - {record.intent.call_id for record in records}:
                raise ValueError("orphan solver input artifact")
            found = []
            for record in records:
                parent = record.intent.call_id
                self._path(parent)
                checked = None
                value = None
                if parent in entries:
                    value = self.io._read(self._path(parent))
                    if value.get("parent_call_id") != parent:
                        raise ValueError("solver input filename differs")
                    checked = self._checked(value, model_calls, actions, require_completed=False)
                elif record.state != "reserved":
                    raise ValueError("dispatched solver has no frozen input")
                authorized = checked is not None and record.state == "completed"
                found.append({"parent_call_id": parent, "solver_state": record.state,
                    "input_saved": checked is not None, "input_bytes": entries.get(parent, 0),
                    "input_sha256": None if value is None else sha256(canonical(value)),
                    "final_view_sha256": None if checked is None else checked["final_view_sha256"],
                    "request_sha256": record.intent.sha256(),
                    **visibility_report_fields(checked if authorized else None),
                    "authority": "completed_solver_input" if authorized else "not_authoritative"})
            return found
        except SolverVisibilityError:
            raise
        except _FAILURES as exc:
            raise SolverVisibilityError("applicability_input_invalid") from exc

    def audit_stored(self) -> list[dict[str, Any]]:
        """Inspect surviving originals with no ledger, replay, or visibility claim."""
        try:
            entries = self._namespace()
            found = []
            for parent in sorted(entries, key=lambda item: int(item[6:])):
                value = self.io._read(self._path(parent))
                if value.get("parent_call_id") != parent:
                    raise ValueError("solver input filename differs")
                intent, plan, compact, view = self._original(value)
                if plan is not None:
                    self._working_original(plan)
                if compact is not None:
                    store = CompactionStore(self.workspace.root, intent.task_id, intent.run_id, compact.policy,
                                            _load(self.profile_json))
                    store._require_plan(compact)
                    result = store._read(store.directory / f"{parent}.result.json")
                    if (set(result) != {"schema_version", "task_id", "run_id", "parent_call_id", "summary_call_id",
                            "source_sha256", "response_sha256", "authority", "view_messages_json", "view_sha256"} or
                            type(result["schema_version"]) is not int or result["schema_version"] != 1 or
                            result["task_id"] != intent.task_id or result["run_id"] != intent.run_id or
                            result["parent_call_id"] != parent or result["summary_call_id"] != compact.summary_call_id or
                            result["source_sha256"] != compact.source_sha256 or result["authority"] != "model_inference" or
                            result["view_messages_json"] != value["final_view_messages_json"] or
                            result["view_sha256"] != value["final_view_sha256"]):
                        raise ValueError("stored solver compaction result differs")
                    require_hash(result["response_sha256"])
                    source = compact.source_messages()
                    summary = view[compact.pinned_count]
                    if type(summary) is not HumanMessage or not isinstance(summary.content, str):
                        raise ValueError("stored compaction summary has another shape")
                    content = _load(summary.content)
                    if (not isinstance(content, dict) or set(content) != {"authority", "source_sha256", "range",
                            "summary_call_id", "summary"} or content["authority"] != "model_inference" or
                            content["source_sha256"] != compact.source_sha256 or
                            content["range"] != [compact.pinned_count, compact.summarized_end] or
                            content["summary_call_id"] != compact.summary_call_id or
                            not isinstance(content["summary"], str) or not content["summary"].strip() or
                            len(content["summary"].encode("utf-8")) > compact.policy.max_summary_bytes or
                            _serialized(view) != _serialized(source[:compact.pinned_count] + [summary] +
                                                            source[compact.summarized_end:]) or
                            len(value["final_view_messages_json"].encode("utf-8")) >=
                            len(compact.source_messages_json.encode("utf-8"))):
                        raise ValueError("stored compaction view differs from its source")
                found.append({"parent_call_id": parent, "input_saved": True, "input_bytes": entries[parent],
                    "input_sha256": sha256(canonical(value)), "final_view_sha256": value["final_view_sha256"],
                    "request_sha256": intent.sha256(), "authority": "stored_input_not_authenticated"})
            return found
        except SolverVisibilityError:
            raise
        except (*_FAILURES, IndexError) as exc:
            raise SolverVisibilityError("applicability_input_invalid") from exc
