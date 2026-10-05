"""Static procedure reads and frozen catalog authentication at the SDK boundary.

Procedures remain complete historical guidance. A saved resource, a completed
read, and a read actually sent to a later solver are deliberately separate
facts. None establishes current applicability or application qualification.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from langchain_core.messages import BaseMessage, SystemMessage
from langchain_core.tools import tool

from .actions import ToolActionStore
from .applicability_handoff import _visibility
from .context_compaction import _messages
from .knowledge import _exists, _object, _ReadSet
from .knowledge_contracts import MAX_PAYLOAD_BYTES, encode
from .knowledge_run import read_run_knowledge
from .model_calls import ModelCallStore, canonical, sha256
from .solver_visibility import (
    MAX_SOLVER_INPUT_BYTES,
    MAX_SOLVER_INPUT_TOTAL_BYTES,
    visibility_report_fields,
)
from .static_skills import (
    NAMESPACE,
    PROFILE_PATH,
    STATIC_SKILL_MARKER,
    StaticSkillError,
    StaticSkillService,
    normalize_static_skill_profile,
    read_run_static_skills,
    static_skill_section,
)
from .task import digest
from .verification import CheckLedger
from .workspace import CandidateWorkspace

STATIC_SKILL_TOOLS = frozenset({"read_skill"})
MAX_STATIC_SKILL_REPORT_BYTES = 4_194_304
_AUTHORITY = {"derivation_authority": "host_curated",
              "epistemic_status": "historical_guidance_not_current_evidence",
              "application_verification": "not_run"}
_LINK_KEYS = ("action_id", "model_call_id", "tool_name", "state", "authorization", "receipt_sha256")


def static_skill_result(service: StaticSkillService, skill_id: str) -> dict[str, Any]:
    """Return exactly one selected complete original without adding permissions."""
    body = service.body(skill_id)
    profile = service.profile
    file = next(file for file in profile["files"] if file["skill_id"] == skill_id)
    raw = body.encode("utf-8")
    if digest(raw) != file["sha256"] or len(raw) != file["bytes"]:
        raise StaticSkillError()
    return {"skill_id": skill_id,
            "snapshot_sha256": profile["snapshot_sha256"], "entry_id": file["entry_id"],
            "content_sha256": file["content_sha256"], "projection_sha256": file["projection_sha256"],
            "body_sha256": file["sha256"], "body_bytes": file["bytes"], "path": file["path"],
            "metadata": file["metadata"], "body": body, **_AUTHORITY}


def static_skill_tools(service: StaticSkillService) -> list[Any]:
    @tool
    def read_skill(skill_id: str, proposal_json: str | None = None) -> str:
        """Read one complete host-approved static procedure from the frozen selection.

        A skill ID is 'dfx-' followed by the first 56 characters of its approved
        projection_sha256, also supplied by ordinary experience retrieval. Both
        catalog modes allow the same selected IDs without prior catalog access.
        These are historical procedures: check premises, retain unknowns and
        preserve business behavior. They grant no additional execution rights
        and do not establish applicability, learned qualification or a repair.
        """
        try:
            return encode(static_skill_result(service, skill_id))
        except (ValueError, OSError, TypeError, KeyError, UnicodeError, RecursionError):
            return "rejected: static_skill_source_invalid"

    return [read_skill]


def _check_catalog(profile: dict[str, Any], messages: list[BaseMessage], section: str | None) -> None:
    """Authenticate the native appended block, including absence in off mode."""
    if type(messages) is not list or section != static_skill_section(profile):
        raise StaticSkillError()
    systems = [message for message in messages if type(message) is SystemMessage]
    matches = []
    for message in systems:
        blocks = message.content_blocks
        for index, block in enumerate(blocks):
            text = block.get("text") if isinstance(block, dict) else None
            if isinstance(text, str) and STATIC_SKILL_MARKER in text:
                matches.append((blocks, index, block))
    if section is None:
        if matches:
            raise StaticSkillError()
        return
    if (section.count(STATIC_SKILL_MARKER) != 1 or len(matches) != 1):
        raise StaticSkillError()
    blocks, index, block = matches[0]
    expected = ("\n\n" if index else "") + section
    if index != len(blocks) - 1 or block != {"type": "text", "text": expected}:
        raise StaticSkillError()


def validate_static_skill_messages(service: StaticSkillService, messages: list[BaseMessage]) -> None:
    """Read-only catalog check for the final actual request before dispatch."""
    _check_catalog(service.profile, messages, service.expected_section())


def validate_static_skill_report(profile: dict[str, Any], facts: dict[str, Any]) -> None:
    """Exact final writer representation of both static report locations."""
    fragment = {"static_skill_profile": profile, "static_skill_facts": facts,
                "completion": {"static_skill_profile": profile, "static_skill_facts": facts}}
    if len(json.dumps(fragment, ensure_ascii=True, indent=2).encode("utf-8")) > MAX_STATIC_SKILL_REPORT_BYTES:
        raise ValueError("static skill complete report fragments exceed capacity")


def preflight_static_skill_report(profile: dict[str, Any], expected_section: str | None,
                                 max_calls: int, max_tool_calls: int) -> None:
    """Reserve frozen originals' ASCII projection and bounded links before writes."""
    profile = normalize_static_skill_profile(profile)
    if (type(max_calls) is not int or type(max_tool_calls) is not int or
            min(max_calls, max_tool_calls) < 1 or expected_section != static_skill_section(profile)):
        raise StaticSkillError()
    full_catalog = static_skill_section({**profile, "enabled": True})
    if full_catalog is None or full_catalog.count(STATIC_SKILL_MARKER) != 1:
        raise StaticSkillError()
    # Body bytes live only in the originals and durable receipts. The native
    # catalog is included conservatively here; final facts retain its digest.
    fragment = {"static_skill_profile": profile, "static_skill_facts": {
        "profile": profile, "resources": [{**file, "actions": []} for file in profile["files"]],
        "catalog": full_catalog, "unread_skill_ids": [file["skill_id"] for file in profile["files"]]}}
    size = 2 * len(json.dumps(fragment, ensure_ascii=True, indent=4).encode("utf-8"))
    size += 16_384 * max_calls + 4_096 * max_tool_calls + 65_536
    if size > MAX_STATIC_SKILL_REPORT_BYTES:
        raise ValueError("static skill complete report fragments exceed capacity")


def _absent(workspace: CandidateWorkspace) -> None:
    if any(_exists(workspace.root / name) for name in (PROFILE_PATH, NAMESPACE)):
        raise ValueError("historical run cannot acquire static skill resources")


def _approved(workspace: CandidateWorkspace, task_directory: Path | None, ledger: CheckLedger,
              knowledge_profile: dict[str, Any] | None, profile: dict[str, Any]) -> StaticSkillService:
    if task_directory is None or knowledge_profile is None:
        raise ValueError("static skills require the actual approved knowledge task")
    store = read_run_knowledge(workspace, task_directory, knowledge_profile, ledger=ledger)
    if store is None:
        raise ValueError("static skills require approved run knowledge")
    service = read_run_static_skills(store, profile)
    if service is None:
        raise ValueError("static skills have no approved frozen resources")
    return service


def _session(workspace: CandidateWorkspace, knowledge: dict[str, Any] | None,
             profile: dict[str, Any]) -> dict[str, Any]:
    reads = _ReadSet(workspace.root, max_read_bytes=2 * MAX_PAYLOAD_BYTES, max_read_calls=2)
    value = _object(reads.read(workspace.root / "session.json"))
    if (value.get("schema_version") != 13 or value.get("static_skill_profile") != profile or
            value.get("knowledge_profile") != knowledge or not isinstance(value.get("applicability_profile"), dict)):
        raise ValueError("static skill profile differs from its actual session")
    preflight_static_skill_report(profile, static_skill_section(profile), value["max_calls"], value["max_tool_calls"])
    reads.finish()
    return value


class _StaticSolverReadSet(_ReadSet):
    """Close frozen solver originals under their own existing shared limits.

    Knowledge originals retain their independent 8 MiB closure. Solver inputs
    allow the shared 64 MiB namespace, and the final byte/hash/descriptor close
    pass consumes the same bytes and read calls as the initial pass.
    """

    def __init__(self, root: Path, max_calls: int):
        if type(max_calls) is not int or max_calls < 1:
            raise ValueError("static skill solver read budget is invalid")
        super().__init__(root, max_read_bytes=2 * MAX_SOLVER_INPUT_TOTAL_BYTES,
                         max_read_calls=2 * max_calls)

    def reserve(self, path: Path, size: int) -> None:
        if type(size) is not int or not 0 <= size <= MAX_SOLVER_INPUT_BYTES:
            raise ValueError("static skill solver input exceeds the shared per-file limit")
        if path in self.sizes:
            if self.sizes[path] != size:
                raise ValueError("static skill solver closure size changed")
            return
        if self.byte_count + size > MAX_SOLVER_INPUT_TOTAL_BYTES:
            raise ValueError("static skill solver closure exceeds the shared total limit")
        self.sizes[path] = size
        self.byte_count += size


def _catalog_sources(workspace: CandidateWorkspace, visibility: Any, inputs: list[dict[str, Any]],
                     profile: dict[str, Any], max_calls: int) -> list[dict[str, Any]]:
    section = static_skill_section(profile)
    if len(inputs) > max_calls:
        raise ValueError("static skill solver input count exceeds the frozen budget")
    reads = _StaticSolverReadSet(workspace.root, max_calls)
    found = []
    for item in inputs:
        if not item["input_saved"]:
            found.append({**item, "catalog_sha256": None, "catalog_bytes": 0, "catalog_entries": 0})
            continue
        value = _object(reads.read(visibility._path(item["parent_call_id"])))
        if sha256(canonical(value)) != item["input_sha256"]:
            raise ValueError("static catalog solver input changed during audit")
        _check_catalog(profile, _messages(value["final_view_messages_json"]), section)
        found.append({**item, "catalog_sha256": None if section is None else sha256(section),
            "catalog_bytes": 0 if section is None else len(section.encode("utf-8")),
            "catalog_entries": len(profile["files"]) if section is not None else 0})
    reads.finish()
    return found


def audit_stored_static_skills(workspace: CandidateWorkspace, task_directory: Path | None, ledger: CheckLedger,
                               knowledge_profile: dict[str, Any] | None,
                               static_profile: dict[str, Any] | None) -> None:
    """Authenticate surviving originals without inventing actual use or cost."""
    if static_profile is None:
        _absent(workspace)
        return
    profile = normalize_static_skill_profile(static_profile)
    _approved(workspace, task_directory, ledger, knowledge_profile, profile)
    session = _session(workspace, knowledge_profile, profile)
    if task_directory is None:
        raise ValueError("static skills have no actual task")
    visibility = _visibility(workspace, task_directory, ledger, session)
    _catalog_sources(workspace, visibility, visibility.audit_stored(), profile, session["max_calls"])


def _source(view: dict[str, Any]) -> dict[str, Any]:
    return {"parent_call_id": view["parent_call_id"], "final_view_sha256": view["final_view_sha256"],
            "request_sha256": view["request_sha256"], **visibility_report_fields(view),
            "authority": "completed_solver_input"}


def audit_run_static_skills(workspace: CandidateWorkspace, task_directory: Path | None, ledger: CheckLedger,
                            knowledge_profile: dict[str, Any] | None, static_profile: dict[str, Any] | None,
                            model_calls: ModelCallStore, actions: ToolActionStore) -> dict[str, Any] | None:
    """Rebuild reads from original body, accounted request and complete receipt."""
    rows = [row for row in actions.records() if row["tool_name"] in STATIC_SKILL_TOOLS]
    calls = model_calls.records(workspace.task_id, workspace.run_id, kind="solver")
    if static_profile is None:
        _absent(workspace)
        if rows or any(set(call.intent.tool_names) & STATIC_SKILL_TOOLS for call in calls):
            raise ValueError("historical run cannot acquire static skill tools")
        return None
    profile = normalize_static_skill_profile(static_profile)
    service = _approved(workspace, task_directory, ledger, knowledge_profile, profile)
    session = _session(workspace, knowledge_profile, profile)
    if task_directory is None or (model_calls.path != workspace.root / "model-calls.sqlite" or
            actions.path != workspace.root / "tool-actions.sqlite"):
        raise ValueError("static skill ledgers belong to another run")
    visibility = _visibility(workspace, task_directory, ledger, session)
    inputs = _catalog_sources(workspace, visibility, visibility.audit(model_calls, actions),
                              profile, session["max_calls"])
    resources = {file["skill_id"]: {**file, "actions": []} for file in profile["files"]}
    facts: dict[str, Any] = {"profile": profile, "resources": list(resources.values()), "solver_inputs": inputs,
        "actions": [], "budget_rejections": [], "unread_skill_ids": sorted(resources),
        "use_authentication": "accounted_solver_and_action_receipts", **_AUTHORITY}
    schema = static_skill_tools(service)[0].args_schema
    budget_rejections: dict[str, list[dict[str, Any]]] = {}
    if (len(calls) > session["max_calls"] or
            sum(row["rejection"] != "budget_exhausted" for row in rows) > session["max_tool_calls"]):
        raise ValueError("static skill accounted sources exceed the frozen shared budgets")
    views: dict[str, dict[str, Any]] = {}

    def actual_view(parent: str) -> dict[str, Any]:
        if parent not in views:
            views[parent] = visibility.read(parent, model_calls, actions)
        return views[parent]

    for row in rows:
        parent = model_calls.get(row["model_call_id"])
        if (parent is None or parent.state != "completed" or parent.intent.kind != "solver" or
                (parent.intent.task_id, parent.intent.run_id) != (workspace.task_id, workspace.run_id) or
                "read_skill" not in parent.intent.tool_names):
            raise ValueError("static skill action has no completed accounted solver")
        response, observation = model_calls.replay(parent.intent)
        requested = [call for call in response.tool_calls if call["name"] == "read_skill" and
            "action-" + sha256(canonical([workspace.task_id, workspace.run_id, parent.intent.call_id, call["id"]]))
            == row["action_id"]]
        if (observation.format_status == "format_error" or len(requested) != 1 or
                row["request_sha256"] != sha256(canonical(requested[0]))):
            raise ValueError("static skill action differs from its actual solver response")
        link = {key: row[key] for key in _LINK_KEYS}
        if row["rejection"] == "budget_exhausted":
            if row["authorization"] != "rejected":
                raise ValueError("static skill budget rejection was authorized")
            if row["state"] == "completed":
                rejected = actions.receipt(row["action_id"])
                if (rejected.tool_call_id != requested[0]["id"] or rejected.status != "error" or
                        rejected.content != "rejected: budget_exhausted"):
                    raise ValueError("static skill budget rejection receipt changed")
            budget_rejections.setdefault(row["model_call_id"], []).append({**link, "rejection": row["rejection"]})
            continue
        facts["actions"].append(link)
        if row["state"] != "completed":
            continue
        receipt = actions.receipt(row["action_id"])
        if receipt.tool_call_id != requested[0]["id"]:
            raise ValueError("static skill receipt belongs to another request")
        if row["authorization"] != "allowed" or receipt.status == "error":
            continue
        if not isinstance(receipt.content, str):
            raise ValueError("static skill receipt has no complete textual result")  # noqa: TRY004 -- Evidence rejection.
        if receipt.content.startswith("rejected: "):
            if receipt.content != "rejected: static_skill_source_invalid":
                raise ValueError("static skill rejection changed")
            continue
        if schema is None or isinstance(schema, dict) or set(requested[0]["args"]) - set(schema.model_fields):
            raise ValueError("static skill tool has unsupported parameters")
        args = schema.model_validate(requested[0]["args"]).model_dump()
        expected = static_skill_result(service, args["skill_id"])
        if receipt.content != encode(expected):
            raise ValueError("static skill receipt differs from complete frozen guidance")
        view = actual_view(parent.intent.call_id)
        _check_catalog(profile, _messages(view["final_view_messages_json"]), static_skill_section(profile))
        seen = []
        # A complete successful read may still never reach a later solver.
        for item in inputs:
            if item["authority"] != "completed_solver_input":
                continue
            later = actual_view(item["parent_call_id"])
            if any(saved["action"]["action_id"] == row["action_id"] and saved["receipt"]["content"] == receipt.content
                   for saved in later["visible_receipts"]):
                seen.append(item["parent_call_id"])
        resources[args["skill_id"]]["actions"].append({**link, "solver_source": _source(view),
            "visible_solver_count": len(seen), "visible_solver_ids_sha256": sha256(canonical(seen))})
    facts["unread_skill_ids"] = sorted(key for key, resource in resources.items() if not resource["actions"])
    facts["budget_rejections"] = [{"model_call_id": parent, "count": len(links),
        "completed_count": sum(link["state"] == "completed" for link in links),
        "unknown_count": sum(link["state"] != "completed" for link in links),
        "links_sha256": sha256(canonical(links)), "reason": "budget_exhausted",
        "authority": "rejected_action_records"} for parent, links in budget_rejections.items()]
    validate_static_skill_report(profile, facts)
    return facts
