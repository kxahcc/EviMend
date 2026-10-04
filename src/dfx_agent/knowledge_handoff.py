"""Approved historical guidance at the SDK tool and durable receipt boundary.

These tools offer relevance-ranked complete guidance. They do not manufacture
current evidence, judge applicability or change the common acceptance gate.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from langchain_core.tools import tool

from .actions import ToolActionStore
from .applicability import ApplicabilityError, ApplicabilityStore
from .applicability_contracts import MAX_CONTEXT_FILES, BoundQuery, BoundQueryPayload
from .knowledge import KnowledgeError, _exists, _object, _path, _ReadSet
from .knowledge_contracts import MAX_CLOSURE_BYTES, content_hash, encode
from .knowledge_run import read_run_knowledge
from .model_calls import ModelCallStore, canonical, sha256
from .retrieval_contracts import MemoryQuery, RetrievalRecord
from .verification import CheckLedger
from .workspace import CandidateWorkspace

KNOWLEDGE_TOOLS = frozenset({"search_experience", "read_experience"})
MAX_KNOWLEDGE_REPORT_BYTES = 4_194_304


def _report_capacity(store: ApplicabilityStore, bound: BoundQuery, retrieved: RetrievalRecord,
                     originals: dict[str, bytes], reads: _ReadSet) -> None:
    """Reserve both report copies in the writer's ASCII/indented representation."""
    profile = _object(reads.read(store.workspace.root / "knowledge-profile.json"))
    queries = {key: BoundQuery.model_validate_json(raw) for key, raw in originals.items()
               if key.startswith("bound-query-")}
    retrievals = {key: RetrievalRecord.model_validate_json(raw) for key, raw in originals.items()
                  if key.startswith("retrieval-")}
    queries[bound.record_id], retrievals[retrieved.record_id] = bound, retrieved
    if any(query.contents()["retrieval_id"] not in retrievals for query in queries.values()):
        raise ValueError("knowledge query has no complete retrieval original")
    pages = [{"bound_query": query.model_dump(mode="json"),
              "retrieval": retrievals[query.contents()["retrieval_id"]].contents(), "actions": []}
             for query in queries.values()]
    fragments = {"knowledge_profile": profile, "knowledge_facts": {"profile": profile, "queries": pages,
        "unreferenced_query_ids": list(queries), "unreferenced_retrieval_ids": list(retrievals)}}
    # These fragments appear once at the top and once inside completion.
    # Four-space nesting is conservative for their actual two-space writer.
    size = 2 * len(json.dumps(fragments, ensure_ascii=True, indent=4).encode("utf-8"))
    session_path = store.workspace.root / "session.json"
    max_actions = 0
    if _exists(session_path):
        session = _object(reads.read(session_path))
        max_actions = session.get("max_tool_calls")
        if type(max_actions) is not int or max_actions < 1:
            raise ValueError("knowledge report has no valid shared action budget")
    # Bound action/read links, orphan IDs and fixed fact keys separately from
    # the complete pages. Direct pre-session module calls have no SDK actions.
    size += 2_048 * max_actions + 65_536
    if size > MAX_KNOWLEDGE_REPORT_BYTES:
        raise ValueError("knowledge complete report fragments exceed capacity")


def _preflight_query(store: ApplicabilityStore, tags: tuple[str, ...], text: str, offset: int,
                      limit: int | None) -> str:
    """Reserve the cumulative audit namespace before publishing a new query."""
    query = MemoryQuery(snapshot_sha256=store.approval.snapshot_sha256, scope=store.approval.scope,
                        tags=tags, text=text, offset=offset, limit=limit)
    retrieved = store.retriever.query(query)
    payload = BoundQueryPayload(binding_sha256=content_hash(store.approval), retrieval_id=retrieved.record_id,
                                retrieval_sha256=retrieved.content_sha256)
    key = content_hash(payload)
    bound = BoundQuery(record_id="bound-query-" + key, content_sha256=key, payload_json=encode(payload))
    reads = _ReadSet(store.workspace.root, max_read_bytes=4 * MAX_CLOSURE_BYTES,
                     max_read_calls=4 * MAX_CONTEXT_FILES)
    originals: dict[str, bytes] = {}
    existing_queries = _records(store.workspace.root, "experience-queries", "bound-query-", reads, originals)
    existing_retrievals = _records(store.workspace.root, "knowledge/retrievals", "retrieval-", reads, originals)
    if (len(set(existing_queries) | {bound.record_id}) > MAX_CONTEXT_FILES or
            len(set(existing_retrievals) | {retrieved.record_id}) > MAX_CONTEXT_FILES):
        raise ValueError("knowledge query audit namespace exceeds capacity")
    for folder, record in (("experience-queries", bound), ("knowledge/retrievals", retrieved)):
        reads.reserve(store.workspace.root / folder / (record.record_id + ".json"), len(encode(record).encode("utf-8")))
    if reads.byte_count > MAX_CLOSURE_BYTES:
        raise ValueError("knowledge cumulative query namespace exceeds capacity")
    _report_capacity(store, bound, retrieved, originals, reads)
    reads.finish()
    return bound.record_id


def experience_result(store: ApplicabilityStore, query_id: str, entry_id: str | None = None) -> dict[str, Any]:
    """Recompute the entire page, or one complete projection from that page."""
    bound = store.read_query(query_id)
    record = store.retriever.read_record(bound.contents()["retrieval_id"])
    if entry_id is None:
        return {"query_id": query_id, "bound_query": bound.model_dump(mode="json"), "retrieval": record.contents()}
    projection = store.retriever.read_projection(record.record_id, entry_id)
    return {"query_id": query_id, "entry_id": entry_id, "projection": projection.model_dump(mode="json"),
            "authority": "historical_guidance_not_current_evidence"}


def experience_tools(store: ApplicabilityStore) -> list[Any]:
    @tool
    def search_experience(tags: list[str] | None = None, text: str = "", offset: int = 0,
                          limit: int | None = None, proposal_json: str | None = None) -> str:
        """Search host-approved frozen guidance; return the complete page and its query_id.

        Rank is relevance, not applicability. Preserve all premises, unknown
        questions and counterexamples; historical success is not current proof.
        The host fixes knowledge scope. No source paths or approval are accepted.
        """
        try:
            expected_id = _preflight_query(store, tuple(tags or ()), text, offset, limit)
            bound = store.record_query(tags=tuple(tags or ()), text=text, offset=offset, limit=limit)
            if bound.record_id != expected_id:
                raise ValueError("knowledge query changed after preflight")
            return encode(experience_result(store, bound.record_id))
        except (ApplicabilityError, KnowledgeError, ValueError, OSError, TypeError):
            return "rejected: knowledge_source_invalid"

    @tool
    def read_experience(query_id: str, entry_id: str | None = None, proposal_json: str | None = None) -> str:
        """Re-read a saved query, or complete guidance for an entry in its actual returned page.

        This is a read of historical guidance, never a current diagnostic fact
        or application check. Missing, changed or nonreturned entries reject.
        """
        try:
            return encode(experience_result(store, query_id, entry_id))
        except (ApplicabilityError, KnowledgeError, ValueError, OSError, TypeError):
            return "rejected: knowledge_source_invalid"

    return [search_experience, read_experience]


def _records(root: Path, folder: str, prefix: str, reads: _ReadSet,
             originals: dict[str, bytes] | None = None) -> list[str]:
    path = root / folder
    if not _exists(path):
        return []
    _path(root, path, directory=True)
    identifiers = []
    for child in path.iterdir():
        if len(identifiers) >= MAX_CONTEXT_FILES:
            raise ValueError("knowledge record count exceeds capacity")
        _path(root, child)
        name = child.name
        identifier = name.removesuffix(".json")
        if (name != identifier + ".json" or not identifier.startswith(prefix) or
                len(identifier.removeprefix(prefix)) != 64):
            raise ValueError("knowledge record namespace has an unexpected member")
        int(identifier.removeprefix(prefix), 16)
        raw = reads.read(child)
        if originals is not None:
            originals[identifier] = raw
        identifiers.append(identifier)
    return sorted(identifiers)


def _stored_queries(store: ApplicabilityStore, reads: _ReadSet):
    query_ids = _records(store.workspace.root, "experience-queries", "bound-query-", reads)
    retrieval_ids = _records(store.workspace.root, "knowledge/retrievals", "retrieval-", reads)
    queries: dict[str, dict[str, Any]] = {}
    references = set()
    for identifier in query_ids:
        bound = store.read_query(identifier)
        retrieval_id = bound.contents()["retrieval_id"]
        record = store.retriever.read_record(retrieval_id)
        references.add(retrieval_id)
        queries[identifier] = {"bound_query": bound.model_dump(mode="json"), "retrieval": record.contents(),
                               "actions": []}
    for identifier in retrieval_ids:
        record = store.retriever.read_record(identifier)
        query = record.contents()["query"]
        if (query["snapshot_sha256"] != store.approval.snapshot_sha256 or
                query["scope"] != store.approval.scope.model_dump(mode="json")):
            raise ValueError("orphan retrieval widened approved knowledge")
    return queries, retrieval_ids, references


def audit_stored_queries(workspace: CandidateWorkspace, task_directory: Path, ledger: CheckLedger,
                          profile: dict[str, Any]) -> None:
    """Missing ledgers never excuse corrupt originals, or establish tool use."""
    store = read_run_knowledge(workspace, task_directory, profile, ledger=ledger)
    if store is None:
        raise ValueError("stored-query audit requires a knowledge approval")
    reads = _ReadSet(workspace.root, max_read_bytes=4 * MAX_CLOSURE_BYTES, max_read_calls=4 * MAX_CONTEXT_FILES)
    _stored_queries(store, reads)
    reads.finish()


def audit_run_knowledge(workspace: CandidateWorkspace, task_directory: Path | None, ledger: CheckLedger,
                        profile: dict[str, Any] | None, model_calls: ModelCallStore,
                        actions: ToolActionStore) -> dict[str, Any] | None:
    """Pure reconstruction; successful files and successful tools stay distinct."""
    rows = actions.records()
    knowledge_rows = [row for row in rows if row["tool_name"] in KNOWLEDGE_TOOLS]
    if task_directory is None:
        if profile is not None:
            raise ValueError("knowledge audit requires the actual task directory")
        # None never authorizes artifacts, including an injected knowledge root.
        for name in ("knowledge", "knowledge-profile.json", "experience-bindings", "experience-queries"):
            try:
                (workspace.root / name).lstat()
            except FileNotFoundError:
                continue
            raise ValueError("historical run cannot acquire knowledge artifacts")
        if knowledge_rows:
            raise ValueError("historical run cannot acquire knowledge actions")
        return None
    store = read_run_knowledge(workspace, task_directory, profile, ledger=ledger)
    if store is None:
        if knowledge_rows:
            raise ValueError("run without knowledge cannot acquire knowledge actions")
        return None
    if (model_calls.path != workspace.root / "model-calls.sqlite" or
            actions.path != workspace.root / "tool-actions.sqlite"):
        raise ValueError("knowledge audit ledgers belong to another run")
    reads = _ReadSet(workspace.root, max_read_bytes=4 * MAX_CLOSURE_BYTES, max_read_calls=4 * MAX_CONTEXT_FILES)
    queries, retrieval_ids, references = _stored_queries(store, reads)
    known_reads = []
    seen_query_ids = set()
    schemas = {item.name: item.args_schema for item in experience_tools(store)}
    for row in knowledge_rows:
        parent = model_calls.get(row["model_call_id"])
        if (parent is None or parent.state != "completed" or parent.intent.kind != "solver" or
                (parent.intent.task_id, parent.intent.run_id) != (workspace.task_id, workspace.run_id)):
            raise ValueError("knowledge tool has no accounted solver response")
        if row["tool_name"] not in parent.intent.tool_names:
            raise ValueError("knowledge action was not offered to its solver call")
        response, observation = model_calls.replay(parent.intent)
        if observation.format_status == "format_error":
            raise ValueError("knowledge tool follows an invalid model response")
        calls = [call for call in response.tool_calls if call["name"] == row["tool_name"] and
                 "action-" + sha256(canonical([workspace.task_id, workspace.run_id,
                                               parent.intent.call_id, call["id"]])) == row["action_id"]]
        if len(calls) != 1 or sha256(canonical(calls[0])) != row["request_sha256"]:
            raise ValueError("knowledge tool request differs from solver response")
        if row["state"] != "completed":
            continue  # Retain unknown results; do not dispatch, complete or repair.
        receipt = actions.receipt(row["action_id"])
        if receipt.tool_call_id != calls[0]["id"]:
            raise ValueError("knowledge tool receipt belongs to another request")
        if row["authorization"] != "allowed" or receipt.status == "error":
            continue
        if not isinstance(receipt.content, str):
            raise ValueError("knowledge receipt has no complete textual result")  # noqa: TRY004 -- Evidence rejection.
        if receipt.content.startswith("rejected: "):
            if receipt.content != "rejected: knowledge_source_invalid":
                raise ValueError("knowledge tool rejection changed")
            continue
        schema = schemas[row["tool_name"]]
        if set(calls[0]["args"]) - set(schema.model_fields):
            raise ValueError("knowledge tool has unsupported parameters")
        args = schema.model_validate(calls[0]["args"]).model_dump()
        answer = _object(receipt.content.encode("utf-8"))
        query_id = answer["query_id"]
        if query_id not in queries:
            raise ValueError("successful knowledge tool has no frozen query")
        if row["tool_name"] == "search_experience":
            requested = queries[query_id]["retrieval"]["query"]
            if (requested["tags"] != (args["tags"] or []) or requested["text"] != args["text"] or
                    requested["offset"] != args["offset"] or requested["limit"] != args["limit"]):
                raise ValueError("knowledge search differs from its actual requested hints")
            expected = experience_result(store, query_id)
            link = {key: row[key] for key in ("action_id", "model_call_id", "tool_name", "receipt_sha256")}
            queries[query_id]["actions"].append(link)
        else:
            if query_id != args["query_id"]:
                raise ValueError("knowledge read returned another query")
            expected = experience_result(store, query_id, args["entry_id"])
            known_reads.append({"query_id": query_id, "entry_id": args["entry_id"],
                                **{key: row[key] for key in ("action_id", "model_call_id", "receipt_sha256")}})
        if receipt.content != encode(expected) or answer != expected:
            raise ValueError("knowledge tool output differs from complete frozen guidance")
        seen_query_ids.add(query_id)
    reads.finish()
    return {"profile": profile, "queries": [queries[key] for key in sorted(queries)], "reads": known_reads,
            "unreferenced_query_ids": sorted(set(queries) - seen_query_ids),
            "unreferenced_retrieval_ids": sorted(set(retrieval_ids) - references),
            "relevance_authority": "relevance_not_applicability", "application_verification": "not_run"}
