"""Three-state experience tools bound to the solver's actual frozen input.

The wrapper authenticates actual solver visibility in addition to the inner
independent store's explicit-host-input contract. It does not rewrite that
inner record's authority. Semantic premise assessments remain model inferences
and never change candidate acceptance or permissions.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

from langchain_core.tools import tool
from pydantic import BaseModel, ConfigDict

from .actions import ToolActionStore
from .applicability import ApplicabilityError, ApplicabilityStore, _record
from .applicability_contracts import (
    MAX_CONTEXT_BYTES,
    MAX_CONTEXT_FILES,
    MAX_CONTEXT_READ_BYTES,
    MAX_CONTEXT_READ_CALLS,
    ApplicabilityContext,
    ApplicabilityProposal,
    ApplicabilityReview,
    ContextMaterial,
    ContextPayload,
)
from .contracts import require_hash
from .knowledge import KnowledgeError, _exists, _object, _path, _ReadSet
from .knowledge_contracts import content_hash, encode
from .knowledge_handoff import _records, experience_result
from .knowledge_run import read_run_knowledge
from .model_calls import ModelCallStore, canonical, sha256
from .solver_visibility import visibility_report_fields
from .task import digest
from .verification import CheckLedger
from .working_context import (
    WorkingContextFocus,
    WorkingContextPacket,
    build_working_context,
)
from .working_context_plan import (
    WorkingContextPolicy,
    WorkingContextStore,
    _answer,
    _capture_material,
    _proof_packet,
)
from .workspace import CandidateWorkspace

APPLICABILITY_TOOLS = frozenset({"prepare_experience_context", "review_experience", "read_experience_review"})
MAX_APPLICABILITY_REPORT_BYTES = 4_194_304
_NAMESPACES = ("applicability-contexts", "applicability-reviews", "applicability-materials")
_MAPS = ("diagnoses", "evidence", "obligations", "candidates", "checks", "decisions", "feedback", "conditions")
_INVALID = (ApplicabilityError, KnowledgeError, ValueError, OSError, TypeError, KeyError, UnicodeError, RecursionError)


class ApplicabilityPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    schema_version: Literal[1] = 1
    enabled: bool = True


def normalize_applicability_profile(value: dict[str, Any]) -> dict[str, Any]:
    """Require an explicit complete canonical profile; never infer approval."""
    if (type(value) is not dict or set(value) != {"schema_version", "enabled"} or
            type(value["schema_version"]) is not int or type(value["enabled"]) is not bool):
        raise ValueError("applicability profile must be an explicit object")
    checked = ApplicabilityPolicy.model_validate_json(encode(value)).model_dump(mode="json")
    if checked != value:
        raise ValueError("applicability profile must contain every canonical field")
    return checked


def _reject_namespaces(workspace: CandidateWorkspace, *, visibility: bool) -> None:
    for name in (*_NAMESPACES, *(("solver-inputs",) if visibility else ())):
        if _exists(workspace.root / name):
            raise ValueError("run configuration cannot acquire applicability evidence")


def _source(view: dict[str, Any]) -> dict[str, Any]:
    return {"parent_call_id": view["parent_call_id"],
            "final_view_sha256": sha256(view["final_view_messages_json"]),
            "request_sha256": view["request_sha256"], **visibility_report_fields(view),
            "authority": "completed_solver_input"}


def context_result(store: ApplicabilityStore, identifier: str) -> dict[str, Any]:
    context = store.read_context(identifier)
    return {"applicability_enabled": True, "query_id": context.contents()["bound_query_id"],
            "context": context.model_dump(mode="json"),
            "review_proposal_schema": ApplicabilityProposal.model_json_schema(),
            "guidance": experience_result(store, context.contents()["bound_query_id"]),
            "focus_authority": "actual_solver_input", "semantic_authority": "model_inference",
            "application_verification": "not_run"}


def review_result(store: ApplicabilityStore, identifier: str) -> dict[str, Any]:
    review = store.read_review(identifier)
    return {"applicability_enabled": True, "query_id": review.contents()["bound_query_id"],
            "review": review.model_dump(mode="json"), "guidance": review.contents(),
            "focus_authority": "actual_solver_input", "semantic_authority": "model_inference",
            "application_verification": "not_run"}


def _ordinary(store: ApplicabilityStore, query_id: str, *, enabled: bool, reason: str) -> dict[str, Any]:
    return {"applicability_enabled": enabled, "query_id": query_id,
            "status": "unknown" if enabled else "disabled", "reason": reason,
            "next_action": "ordinary_retrieval", "guidance": experience_result(store, query_id),
            "semantic_authority": "model_inference", "application_verification": "not_run"}


def _visible_query(store: ApplicabilityStore, view: dict[str, Any], query_id: str) -> None:
    """An ID appearing in free text or a sibling result does not grant access."""
    for item in view["visible_receipts"]:
        answer = _answer(item)
        if answer is None or answer.get("query_id") != query_id:
            continue
        name = item["action"]["tool_name"]
        args = item["tool_call"]["args"]
        if name in {"search_experience", "read_experience"}:
            expected = experience_result(store, query_id, args.get("entry_id") if name == "read_experience" else None)
        elif name == "prepare_experience_context" and isinstance(answer.get("context"), dict):
            expected = context_result(store, answer["context"]["context_id"])
        elif name in {"review_experience", "read_experience_review"} and isinstance(answer.get("review"), dict):
            expected = review_result(store, answer["review"]["record_id"])
        elif name in APPLICABILITY_TOOLS and answer.get("next_action") == "ordinary_retrieval":
            expected = _ordinary(store, query_id, enabled=answer["applicability_enabled"], reason=answer["reason"])
        else:
            continue
        if answer == expected and item["receipt"]["content"] == encode(expected):
            return
    raise ValueError("query was not present as complete approved guidance in the solver input")


def _last_query(store: ApplicabilityStore, view: dict[str, Any]) -> str:
    for item in reversed(view["visible_receipts"]):
        answer = _answer(item)
        if answer is not None and isinstance(answer.get("query_id"), str):
            _visible_query(store, view, answer["query_id"])
            return answer["query_id"]
    raise ValueError("disabled applicability has no actually visible ordinary query")


def _visible_context(store: ApplicabilityStore, view: dict[str, Any], identifier: str) -> ApplicabilityContext:
    expected = context_result(store, identifier)
    if not any(_answer(item) == expected and item["receipt"]["content"] == encode(expected)
               for item in view["visible_receipts"]
               if item["action"]["tool_name"] == "prepare_experience_context"):
        raise ValueError("review context was not completely present in the solver input")
    return ApplicabilityContext.model_validate_json(encode(expected["context"]))


def _same_focus(context: ApplicabilityContext, view: dict[str, Any]) -> None:
    frozen = ContextPayload.model_validate_json(context.payload_json).packet.contents()["focus"]
    actual = WorkingContextFocus.model_validate_json(encode(view["focus"])).model_dump(mode="json")
    if frozen != actual:
        raise ValueError("experience context belongs to another actual solver focus")


def _visible_packet(packet: WorkingContextPacket, material: dict[str, bytes], view: dict[str, Any]) -> None:
    """Every frozen member is either fully sent in the panel or a real receipt."""
    value = packet.contents()
    panel = view.get("visible_packet") or {}
    remaining = json.loads(encode(value))
    for field in _MAPS:
        remaining[field] = {key: record for key, record in value[field].items()
                            if panel.get(field, {}).get(key) != record}
    payload = canonical(remaining)
    partial = WorkingContextPacket(canonical_payload_json=payload, payload_sha256=sha256(payload),
                                   max_bytes=packet.max_bytes)
    _proof_packet(partial, view["visible_receipts"], material)


def _preview_context(store: ApplicabilityStore, query_id: str, view: dict[str, Any]) -> ApplicabilityContext:
    focus = WorkingContextFocus.model_validate_json(encode(view["focus"]))
    with store._operation() as operation:
        store._query(query_id, operation)
        packet = build_working_context(store.workspace, focus, task_directory=store.task_directory,
                                       ledger=store.ledger, max_bytes=MAX_CONTEXT_BYTES)
        material = _capture_material(store.workspace, store.task_directory, packet, reader=operation.read,
                                     max_references=MAX_CONTEXT_FILES)
        _visible_packet(packet, material, view)
        payload = ContextPayload(binding_sha256=content_hash(store.approval), bound_query_id=query_id, packet=packet,
            materials=tuple(ContextMaterial(reference=key, sha256=digest(raw), bytes=len(raw))
                            for key, raw in sorted(material.items())))
        store._frozen_owner(payload, material)
        return _record(ApplicabilityContext, "app-context-", payload)


def _review_proposal(store: ApplicabilityStore, view: dict[str, Any], raw: str) -> ApplicabilityProposal:
    proposal = ApplicabilityProposal.model_validate_json(raw)
    context = _visible_context(store, view, proposal.context_id)
    _same_focus(context, view)
    value = ContextPayload.model_validate_json(context.payload_json).packet.contents()
    # The complete context is itself visible current evidence; authenticated
    # store admission recursively verifies inference bases and candidate scope.
    for premise in proposal.premises:
        for use in (*premise.support, *premise.contradictions):
            if use.evidence_id not in value["evidence"]:
                raise ValueError("premise evidence was not in the actually visible frozen context")
    return proposal


def _stored(store: ApplicabilityStore, reads: _ReadSet) -> dict[str, Any]:
    contexts = {key: {"context": store.read_context(key).model_dump(mode="json"), "actions": []}
                for key in _records(store.workspace.root, "applicability-contexts", "app-context-", reads)}
    reviews = {key: {"review": store.read_review(key).model_dump(mode="json"), "actions": []}
               for key in _records(store.workspace.root, "applicability-reviews", "app-review-", reads)}
    materials = []
    folder = store.workspace.root / "applicability-materials"
    if _exists(folder):
        _path(store.workspace.root, folder, directory=True)
        for path in sorted(folder.iterdir()):
            if len(materials) >= MAX_CONTEXT_FILES:
                raise ValueError("applicability material namespace exceeds capacity")
            if path.name != path.stem + ".blob":
                raise ValueError("applicability material namespace has an unexpected member")
            require_hash(path.stem)
            raw = reads.read(path, expected_hash=path.stem)
            materials.append({"sha256": path.stem, "bytes": len(raw)})
    if reads.byte_count > MAX_CONTEXT_BYTES:
        raise ValueError("applicability cumulative namespace exceeds capacity")
    referenced = {item["sha256"] for context in contexts.values()
                  for item in ApplicabilityContext.model_validate_json(encode(context["context"])).contents()["materials"]}
    return {"contexts": contexts, "reviews": reviews, "materials": materials,
            "unreferenced_material_sha256": sorted({item["sha256"] for item in materials} - referenced)}


def _reads(workspace: CandidateWorkspace) -> _ReadSet:
    return _ReadSet(workspace.root, max_read_bytes=MAX_CONTEXT_READ_BYTES, max_read_calls=MAX_CONTEXT_READ_CALLS)


def _require_report_fragment_capacity(facts: dict[str, Any]) -> None:
    fragment = {"applicability_facts": facts, "completion": {"applicability_facts": facts}}
    if len(json.dumps(fragment, ensure_ascii=True, indent=2).encode("utf-8")) > MAX_APPLICABILITY_REPORT_BYTES:
        raise ValueError("applicability complete report fragments exceed capacity")


def _report_capacity(store: ApplicabilityStore, *, context: ApplicabilityContext | None = None,
                     review: ApplicabilityReview | None = None) -> None:
    reads = _reads(store.workspace)
    stored = _stored(store, reads)
    if context is not None:
        stored["contexts"][context.context_id] = {"context": context.model_dump(mode="json"), "actions": []}
        reads.reserve(store.workspace.root / "applicability-contexts" / (context.context_id + ".json"),
                      len(encode(context).encode("utf-8")))
        known_materials = {item["sha256"] for item in stored["materials"]}
        for item in ContextPayload.model_validate_json(context.payload_json).materials:
            reads.reserve(store.workspace.root / "applicability-materials" / (item.sha256 + ".blob"), item.bytes)
            if item.sha256 not in known_materials:
                stored["materials"].append({"sha256": item.sha256, "bytes": item.bytes})
                known_materials.add(item.sha256)
        if len(known_materials) > MAX_CONTEXT_FILES:
            raise ValueError("applicability material namespace exceeds capacity")
    if review is not None:
        stored["reviews"][review.record_id] = {"review": review.model_dump(mode="json"), "actions": []}
        reads.reserve(store.workspace.root / "applicability-reviews" / (review.record_id + ".json"),
                      len(encode(review).encode("utf-8")))
    if reads.byte_count > MAX_CONTEXT_BYTES:
        raise ValueError("applicability cumulative namespace exceeds capacity")
    if max(len(stored["contexts"]), len(stored["reviews"])) > MAX_CONTEXT_FILES:
        raise ValueError("applicability record namespace exceeds capacity")
    max_calls = max_actions = 0
    if _exists(store.workspace.root / "session.json"):
        session = _object(reads.read(store.workspace.root / "session.json"))
        max_calls, max_actions = session["max_calls"], session["max_tool_calls"]
        if type(max_calls) is not int or type(max_actions) is not int or min(max_calls, max_actions) < 1:
            raise ValueError("applicability report has no valid shared budgets")
    # Both report locations use an ASCII writer. Reserve bounded links and
    # solver summaries (including grouped budget rejections); complete request
    # originals and every action row remain in their original stores.
    size = 2 * len(json.dumps(stored, ensure_ascii=True, indent=4).encode("utf-8"))
    size += 16_384 * max_calls + 4_096 * max_actions + 65_536
    if size > MAX_APPLICABILITY_REPORT_BYTES:
        raise ValueError("applicability complete report fragments exceed capacity")
    reads.finish()


def _prepare(store: ApplicabilityStore, view: dict[str, Any], query_id: str, policy: ApplicabilityPolicy):
    _visible_query(store, view, query_id)
    if not policy.enabled:
        return _ordinary(store, query_id, enabled=False, reason="applicability_disabled")
    focus = WorkingContextFocus.model_validate_json(encode(view["focus"]))
    if focus.diagnosis_id is None or not focus.hypothesis_ids:
        return _ordinary(store, query_id, enabled=True, reason="no_visible_focus")
    preview = _preview_context(store, query_id, view)
    _report_capacity(store, context=preview)
    context = store.prepare_context(query_id, focus)
    if context != preview:
        raise ValueError("applicability context changed after visibility preflight")
    return context_result(store, context.context_id)


def _review(store: ApplicabilityStore, view: dict[str, Any], raw: str, policy: ApplicabilityPolicy):
    if not policy.enabled:
        return _ordinary(store, _last_query(store, view), enabled=False, reason="applicability_disabled")
    proposal = _review_proposal(store, view, raw)
    with store._operation() as operation:
        preview = _record(ApplicabilityReview, "app-review-", store._review_value(proposal, operation))
    _report_capacity(store, review=preview)
    review = store.review(proposal)
    if review != preview:
        raise ValueError("applicability review changed after preflight")
    return review_result(store, review.record_id)


def _read_review(store: ApplicabilityStore, view: dict[str, Any], identifier: str, policy: ApplicabilityPolicy):
    if not policy.enabled:
        return _ordinary(store, _last_query(store, view), enabled=False, reason="applicability_disabled")
    expected = review_result(store, identifier)
    if not any(_answer(item) == expected and item["receipt"]["content"] == encode(expected)
               for item in view["visible_receipts"]
               if item["action"]["tool_name"] in {"review_experience", "read_experience_review"}):
        raise ValueError("review was not completely present in the actual solver input")
    context = store.read_context(expected["guidance"]["context_id"])
    _same_focus(context, view)
    return expected


def applicability_tools(store: ApplicabilityStore, visibility: Any, model_calls: ModelCallStore | None,
                        actions: ToolActionStore | None, policy: ApplicabilityPolicy) -> list[Any]:
    policy = ApplicabilityPolicy.model_validate_json(policy.model_dump_json())

    def run(name: str, value: str) -> str:
        try:
            if model_calls is None or actions is None or visibility is None:
                raise ValueError("applicability has no initialized solver ledgers")
            if not policy.enabled:
                _reject_namespaces(store.workspace, visibility=False)
            parent = actions.current_solver_call()
            if parent is None:
                raise ValueError("applicability has no host-authorized active parent")
            view = visibility.read(parent, model_calls, actions)
            operation = {"prepare_experience_context": _prepare, "review_experience": _review,
                         "read_experience_review": _read_review}[name]
            return encode(operation(store, view, value, policy))
        except _INVALID:
            return "rejected: applicability_source_invalid"

    @tool
    def prepare_experience_context(query_id: str, proposal_json: str | None = None) -> str:
        """Freeze experience context from the actual solver input and return complete guidance.

        The query must already be visible. The host supplies diagnosis,
        hypothesis and candidate focus. Missing focus retains unknown and
        ordinary retrieval. The response includes review_proposal_schema for
        the next review submission. Disabled mode returns ordinary guidance.
        """
        return run("prepare_experience_context", query_id)

    @tool
    def review_experience(applicability_json: str, proposal_json: str | None = None) -> str:
        """Review all experience premises using the complete context already sent to this solver.

        Submit an ApplicabilityProposal. Supported offers the complete process,
        contradicted avoids copying, unknown retains original questions. All
        judgments remain model inference. Disabled mode uses ordinary guidance.
        """
        return run("review_experience", applicability_json)

    @tool
    def read_experience_review(review_id: str, proposal_json: str | None = None) -> str:
        """Read a complete review already visible for this solver's exact current focus.

        Do not treat historical guidance or supported inference as a verified
        cause or accepted patch. Disabled mode returns ordinary guidance.
        """
        return run("read_experience_review", review_id)

    return [prepare_experience_context, review_experience, read_experience_review]


def _visibility(workspace: CandidateWorkspace, task_directory: Path, ledger: CheckLedger, session: dict[str, Any]):
    context = session["context_profile"]
    working = WorkingContextStore(workspace, task_directory, ledger,
        WorkingContextPolicy.model_validate(context["host_working_context"]), session["model_profile"],
        format_version=context.get("working_context_format", 1))
    from .solver_visibility import SolverVisibilityStore

    return SolverVisibilityStore(workspace, task_directory, ledger, session["model_profile"], working)


def audit_stored_applicability(workspace: CandidateWorkspace, task_directory: Path | None, ledger: CheckLedger,
                               knowledge_profile: dict[str, Any] | None,
                               applicability_profile: dict[str, Any] | None) -> dict[str, Any] | None:
    """Authenticate originals without inventing a solver or an actual tool use."""
    if applicability_profile is None:
        _reject_namespaces(workspace, visibility=True)
        return None
    profile = normalize_applicability_profile(applicability_profile)
    if task_directory is None or knowledge_profile is None:
        raise ValueError("applicability requires the actual approved knowledge task")
    store = read_run_knowledge(workspace, task_directory, knowledge_profile, ledger=ledger)
    if store is None:
        raise ValueError("applicability requires approved run knowledge")
    if not profile["enabled"]:
        _reject_namespaces(workspace, visibility=False)
    reads = _reads(workspace)
    stored = _stored(store, reads)
    solver_inputs = []
    if _exists(workspace.root / "session.json"):
        session = _object(reads.read(workspace.root / "session.json"))
        if session.get("schema_version") != 12 or session.get("applicability_profile") != profile:
            raise ValueError("applicability profile differs from its actual session")
        solver_inputs = _visibility(workspace, task_directory, ledger, session).audit_stored()
    elif _exists(workspace.root / "solver-inputs"):
        raise ValueError("saved solver input has no actual session profile")
    reads.finish()
    _report_capacity(store)
    facts = {"profile": profile, "contexts": list(stored["contexts"].values()),
            "reviews": list(stored["reviews"].values()), "materials": stored["materials"],
            "unreferenced_context_ids": sorted(stored["contexts"]),
            "unreferenced_review_ids": sorted(stored["reviews"]),
            "unreferenced_material_sha256": stored["unreferenced_material_sha256"],
            "actions": [], "budget_rejections": [], "solver_inputs": solver_inputs, "use_authentication": "not_accounted",
            "semantic_authority": "model_inference", "application_verification": "not_run"}
    _require_report_fragment_capacity(facts)
    return facts


def audit_run_applicability(workspace: CandidateWorkspace, task_directory: Path | None, ledger: CheckLedger,
                            knowledge_profile: dict[str, Any] | None,
                            applicability_profile: dict[str, Any] | None, model_calls: ModelCallStore,
                            actions: ToolActionStore) -> dict[str, Any] | None:
    """Reconstruct successful tools from their exact solver and durable receipt."""
    rows = [row for row in actions.records() if row["tool_name"] in APPLICABILITY_TOOLS]
    calls = model_calls.records(workspace.task_id, workspace.run_id, kind="solver")
    if applicability_profile is None:
        _reject_namespaces(workspace, visibility=True)
        if rows or any(set(call.intent.tool_names) & APPLICABILITY_TOOLS for call in calls):
            raise ValueError("historical run cannot acquire applicability tools")
        return None
    facts = audit_stored_applicability(workspace, task_directory, ledger, knowledge_profile, applicability_profile)
    if facts is None or task_directory is None:
        raise ValueError("applicability has no auditable approved run")
    if (model_calls.path != workspace.root / "model-calls.sqlite" or
            actions.path != workspace.root / "tool-actions.sqlite"):
        raise ValueError("applicability ledgers belong to another run")
    store = read_run_knowledge(workspace, task_directory, knowledge_profile, ledger=ledger)
    if store is None:
        raise ValueError("applicability has no run knowledge")
    reads = _reads(workspace)
    session = _object(reads.read(workspace.root / "session.json"))
    profile = normalize_applicability_profile(applicability_profile)
    if session.get("schema_version") != 12 or session.get("applicability_profile") != profile:
        raise ValueError("applicability profile differs from its actual session")
    policy = ApplicabilityPolicy.model_validate(profile)
    visibility = _visibility(workspace, task_directory, ledger, session)
    facts["solver_inputs"] = visibility.audit(model_calls, actions)
    schemas = {item.name: item.args_schema for item in applicability_tools(store, visibility, model_calls, actions, policy)}
    contexts = {item["context"]["context_id"]: item for item in facts["contexts"]}
    reviews = {item["review"]["record_id"]: item for item in facts["reviews"]}
    budget_rejections: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        parent = model_calls.get(row["model_call_id"])
        if (parent is None or parent.state != "completed" or parent.intent.kind != "solver" or
                (parent.intent.task_id, parent.intent.run_id) != (workspace.task_id, workspace.run_id) or
                row["tool_name"] not in parent.intent.tool_names):
            raise ValueError("applicability action has no completed accounted solver")
        response, observation = model_calls.replay(parent.intent)
        requested = [call for call in response.tool_calls if call["name"] == row["tool_name"] and
            "action-" + sha256(canonical([workspace.task_id, workspace.run_id, parent.intent.call_id, call["id"]])) == row["action_id"]]
        if (observation.format_status == "format_error" or len(requested) != 1 or
                row["request_sha256"] != sha256(canonical(requested[0]))):
            raise ValueError("applicability action differs from its actual solver response")
        link = {key: row[key] for key in ("action_id", "model_call_id", "tool_name", "state", "authorization", "receipt_sha256")}
        # Rejected siblings can exceed max_tool_calls. Authenticate each row,
        # but summarize this unbounded tail once per accounted solver call.
        # Cost still comes from the complete action ledger, never these links.
        if row["rejection"] == "budget_exhausted":
            if row["authorization"] != "rejected":
                raise ValueError("applicability budget rejection was authorized")
            if row["state"] == "completed":
                rejected = actions.receipt(row["action_id"])
                if (rejected.tool_call_id != requested[0]["id"] or rejected.status != "error" or
                        rejected.content != "rejected: budget_exhausted"):
                    raise ValueError("applicability budget rejection receipt changed")
            budget_rejections.setdefault(row["model_call_id"], []).append({**link, "rejection": row["rejection"]})
            continue
        facts["actions"].append(link)
        if row["state"] != "completed":
            continue
        receipt = actions.receipt(row["action_id"])
        if receipt.tool_call_id != requested[0]["id"]:
            raise ValueError("applicability receipt belongs to another request")
        if row["authorization"] != "allowed" or receipt.status == "error":
            continue
        if not isinstance(receipt.content, str):
            raise ValueError("applicability receipt has no complete textual result")  # noqa: TRY004 -- Evidence rejection.
        if receipt.content.startswith("rejected: "):
            if receipt.content != "rejected: applicability_source_invalid":
                raise ValueError("applicability rejection changed")
            continue
        schema = schemas[row["tool_name"]]
        if set(requested[0]["args"]) - set(schema.model_fields):
            raise ValueError("applicability tool has unsupported parameters")
        args = schema.model_validate(requested[0]["args"]).model_dump()
        view = visibility.read(parent.intent.call_id, model_calls, actions)
        name = row["tool_name"]
        # Historical audit never invokes publishing prepare/review entrypoints.
        if name == "prepare_experience_context":
            _visible_query(store, view, args["query_id"])
            answer = _object(receipt.content.encode("utf-8"))
            if not policy.enabled:
                expected = _ordinary(store, args["query_id"], enabled=False, reason="applicability_disabled")
            elif "context" not in answer:
                focus = WorkingContextFocus.model_validate_json(encode(view["focus"]))
                if focus.diagnosis_id is not None and focus.hypothesis_ids:
                    raise ValueError("focused applicability tool silently returned ordinary retrieval")
                expected = _ordinary(store, args["query_id"], enabled=True, reason="no_visible_focus")
            else:
                identifier = answer["context"]["context_id"]
                frozen = store.read_context(identifier)
                _same_focus(frozen, view)
                contents = ContextPayload.model_validate_json(frozen.payload_json)
                material = {item.reference: reads.read(workspace.root / "applicability-materials" / (item.sha256 + ".blob"),
                    expected_hash=item.sha256, expected_bytes=item.bytes) for item in contents.materials}
                _visible_packet(contents.packet, material, view)
                if contents.bound_query_id != args["query_id"]:
                    raise ValueError("applicability tool returned another query")
                expected = context_result(store, identifier)
                contexts[identifier]["actions"].append({**link, "solver_source": _source(view)})
        elif name == "review_experience":
            if not policy.enabled:
                expected = _ordinary(store, _last_query(store, view), enabled=False, reason="applicability_disabled")
            else:
                proposal = _review_proposal(store, view, args["applicability_json"])
                with store._operation() as operation:
                    frozen = _record(ApplicabilityReview, "app-review-", store._review_value(proposal, operation))
                expected = review_result(store, frozen.record_id)
                reviews[frozen.record_id]["actions"].append({**link, "solver_source": _source(view)})
        else:
            expected = _read_review(store, view, args["review_id"], policy)
            if policy.enabled:
                reviews[args["review_id"]]["actions"].append({**link, "solver_source": _source(view)})
        if receipt.content != encode(expected):
            raise ValueError("applicability receipt differs from complete frozen guidance")
    reads.finish()
    facts["unreferenced_context_ids"] = sorted(key for key, item in contexts.items() if not item["actions"])
    facts["unreferenced_review_ids"] = sorted(key for key, item in reviews.items() if not item["actions"])
    facts["budget_rejections"] = [{"model_call_id": parent, "count": len(links),
        "completed_count": sum(link["state"] == "completed" for link in links),
        "unknown_count": sum(link["state"] != "completed" for link in links),
        "links_sha256": sha256(canonical(links)), "reason": "budget_exhausted",
        "authority": "rejected_action_records"} for parent, links in budget_rejections.items()]
    facts["use_authentication"] = "accounted_solver_and_action_receipts"
    _require_report_fragment_capacity(facts)
    return facts
