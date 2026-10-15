"""Current evidence checks for transferring historical guidance, without execution.

Host approval fixes task and knowledge scope. Semantic assessments remain model
inferences; the host authenticates registered frozen sources, not applicability.
Historical reads use captured bytes, never the latest check state or library.
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .applicability_contracts import (
    MAX_CONTEXT_BYTES,
    MAX_CONTEXT_FILES,
    MAX_CONTEXT_READ_BYTES,
    MAX_CONTEXT_READ_CALLS,
    ApplicabilityContext,
    ApplicabilityProposal,
    ApplicabilityReview,
    BoundQuery,
    BoundQueryPayload,
    ContextMaterial,
    ContextPayload,
    EvidenceUse,
    NextAction,
    PremiseQuestion,
    ReviewPayload,
    TaskKnowledgeBinding,
)
from .contracts import (
    CandidatePatch,
    Diagnosis,
    Evidence,
    RepairObligation,
    execution_evidence_kind,
    require_hash,
    validate_references,
)
from .knowledge import KnowledgeError, KnowledgeStore, _copy, _object, _path, _ReadSet
from .knowledge_contracts import SnapshotPayload, content_hash, encode
from .knowledge_retrieval import KnowledgeRetriever
from .retrieval_contracts import MemoryQuery, RetrievalPayload, RetrievalRecord
from .task import digest
from .verification import CheckLedger
from .working_context import (
    WorkingContextFocus,
    build_working_context,
    candidate_focus_matches,
)
from .working_context_plan import _capture_material, _verify_raw_material
from .workspace import CandidateWorkspace

_INVALID = (OSError, ValueError, TypeError, KeyError, UnicodeError, RecursionError, KnowledgeError)


class ApplicabilityError(ValueError):
    """Fixed categories do not echo model or private task text."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


class _Operation:
    def __init__(self, store: ApplicabilityStore):
        self.store = store
        self.local = _ReadSet(store.workspace.root, max_read_bytes=MAX_CONTEXT_READ_BYTES,
                              max_read_calls=MAX_CONTEXT_READ_CALLS)
        self.task = _ReadSet(store.task_directory, max_read_bytes=MAX_CONTEXT_READ_BYTES,
                             max_read_calls=MAX_CONTEXT_READ_CALLS)
        self.knowledge_bytes = 0
        self.queries: dict[str, tuple[BoundQuery, RetrievalRecord]] = {}
        self.closed = False

    def capacity(self, extra: int = 0) -> None:
        # KnowledgeRetriever independently reconstructs this authenticated
        # cost. Across roots this is conservative, not a global CAS dedup.
        if self.local.byte_count + self.task.byte_count + self.knowledge_bytes + extra > MAX_CONTEXT_BYTES:
            raise ValueError("applicability complete closure exceeds capacity")

    def read(self, path: Path, *, expected_hash: str | None = None, expected_bytes: int | None = None) -> bytes:
        if self.closed:
            raise ValueError("closed applicability read operation cannot be extended")
        path = path.absolute()
        reads = self.local if path.is_relative_to(self.store.workspace.root) else self.task
        path = _path(reads.root, path)
        size = path.lstat().st_size
        self.capacity(0 if path in reads.sizes else size)
        if (self.local.read_bytes + self.task.read_bytes + size > MAX_CONTEXT_READ_BYTES or
                self.local.read_calls + self.task.read_calls + 1 > MAX_CONTEXT_READ_CALLS):
            raise ValueError("applicability local capture/read budget exceeded")
        return reads.read(path, expected_hash=expected_hash, expected_bytes=expected_bytes)

    def reserve(self, path: Path, size: int) -> None:
        if self.closed and (path not in self.local.sizes or self.local.sizes[path] != size):
            raise ValueError("closed applicability publication reservation cannot be extended")
        self.capacity(0 if path in self.local.sizes else size)
        self.local.reserve(path, size)

    def preflight_audit(self, reads: list[tuple[Path, int]]) -> None:
        """Simulate the independent audit, including repeated CAS references.

        Owner originals and their CAS copies are different paths. Initial
        reads count every reference; closing reads count each unique path.
        This checks budgets only, before publication, without performing I/O.
        """
        unique: dict[Path, int] = {}
        for path, size in reads:
            if path in unique and unique[path] != size:
                raise ValueError("audit path has inconsistent byte sizes")
            unique[path] = size
        if sum(unique.values()) + self.knowledge_bytes > MAX_CONTEXT_BYTES:
            raise ValueError("applicability audit closure exceeds capacity")
        if (sum(size for _, size in reads) + sum(unique.values()) > MAX_CONTEXT_READ_BYTES or
                len(reads) + len(unique) > MAX_CONTEXT_READ_CALLS):
            raise ValueError("applicability audit closing read budget exceeded")

    def finish(self, *, future_bytes: int = 0, future_calls: int = 0) -> None:
        if self.closed:
            return
        repeated_bytes = sum(item[1] for reads in (self.local, self.task) for item in reads.receipts.values())
        repeated_calls = len(self.local.receipts) + len(self.task.receipts)
        if (self.local.read_bytes + self.task.read_bytes + repeated_bytes + future_bytes > MAX_CONTEXT_READ_BYTES or
                self.local.read_calls + self.task.read_calls + repeated_calls + future_calls > MAX_CONTEXT_READ_CALLS):
            raise ValueError("applicability local closing read budget exceeded")
        self.local.finish()
        self.task.finish()
        self.closed = True


def _record(cls, prefix: str, payload):
    key = content_hash(payload)
    if cls is ApplicabilityContext:
        return cls(context_id=prefix + key, payload_sha256=key, payload_json=encode(payload))
    return cls(record_id=prefix + key, content_sha256=key, payload_json=encode(payload))


class ApplicabilityStore:
    """Independent host API, with explicit publication and pure historical reads.

    Approval must come from the host, not model text or saved self-declarations.
    Focus is explicit host input, not authenticated SDK recency. No process,
    check, model request, candidate modification or permission is emitted here.
    """

    def __init__(self, workspace: CandidateWorkspace, task_directory: Path, ledger: CheckLedger,
                 retriever: KnowledgeRetriever, approval: TaskKnowledgeBinding):
        self.workspace = workspace
        self.task_directory = task_directory.absolute()
        self.ledger = ledger
        self.retriever = retriever
        self.approval = _copy(approval)
        self.io = KnowledgeStore(workspace.root)

    @contextmanager
    def _operation(self):
        try:
            operation = _Operation(self)
            self._owner(operation)
            yield operation
            operation.finish()
        except ApplicabilityError:
            raise
        except _INVALID as exc:
            raise ApplicabilityError("applicability_source_invalid") from exc

    def _owner(self, op: _Operation) -> None:
        approved = self.approval
        if (self.ledger.workspace is not self.workspace or
                self.ledger.root != self.workspace.root / "checks" or
                (self.workspace.task_id, self.workspace.run_id, self.workspace.source_snapshot_sha256) !=
                (approved.task_id, approved.run_id, approved.source_snapshot_sha256)):
            raise ValueError("applicability workspace or ledger owner differs")
        meta = _object(op.read(self.workspace.root / "workspace.json"))
        if meta != {"schema_version": 1, "task_id": approved.task_id, "run_id": approved.run_id,
                    "source_snapshot_sha256": approved.source_snapshot_sha256}:
            raise ValueError("applicability registered workspace differs")
        task = _object(op.read(self.task_directory / "task.json"))
        if (task["task_id"] != approved.task_id or task["source_snapshot_sha256"] != approved.source_snapshot_sha256 or
                task["crash_sha256"] != approved.crash_sha256):
            raise ValueError("applicability approved task differs")
        if digest(op.read(self.task_directory / task["crash_log"])) != approved.crash_sha256:
            raise ValueError("applicability approved log differs")

    def _binding(self, op: _Operation) -> None:
        raw = op.read(self.workspace.root / "experience-bindings" / "approved.json")
        actual = TaskKnowledgeBinding.model_validate_json(encode(_object(raw)))
        if actual != self.approval or raw != encode(actual).encode("utf-8"):
            raise ValueError("saved knowledge binding differs from external host approval")

    def _saved(self, folder: str, identifier: str, prefix: str, cls, op: _Operation):
        if not isinstance(identifier, str) or not identifier.startswith(prefix):
            raise ValueError("applicability record identity invalid")
        require_hash(identifier.removeprefix(prefix))
        raw = op.read(self.workspace.root / folder / (identifier + ".json"))
        result = cls.model_validate_json(encode(_object(raw)))
        key = result.context_id if cls is ApplicabilityContext else result.record_id
        if key != identifier or encode(result).encode("utf-8") != raw:
            raise ValueError("applicability saved canonical identity differs")
        return result

    def _publish(self, folder: str, identifier: str, value, op: _Operation) -> None:
        raw = encode(value).encode("utf-8")
        op.reserve(self.workspace.root / folder / (identifier + ".json"), len(raw))
        self.io._publish(folder, identifier + ".json", raw)

    def _query(self, identifier: str, op: _Operation) -> tuple[BoundQuery, RetrievalRecord]:
        if identifier in op.queries:
            return op.queries[identifier]
        self._binding(op)
        bound = self._saved("experience-queries", identifier, "bound-query-", BoundQuery, op)
        payload = BoundQueryPayload.model_validate_json(bound.payload_json)
        if payload.binding_sha256 != content_hash(self.approval):
            raise ValueError("query belongs to another host approval")
        retrieved = self.retriever.read_record(payload.retrieval_id)
        query = RetrievalPayload.model_validate_json(retrieved.payload_json)
        if (retrieved.content_sha256 != payload.retrieval_sha256 or
                query.query.snapshot_sha256 != self.approval.snapshot_sha256 or query.query.scope != self.approval.scope):
            raise ValueError("query widened the approved knowledge scope")
        op.knowledge_bytes = query.source_closure_bytes + query.index_bytes + len(encode(retrieved).encode("utf-8"))
        op.capacity()
        op.queries[identifier] = bound, retrieved
        return bound, retrieved

    def _frozen_owner(self, contents: ContextPayload, raw: dict[str, bytes]) -> dict[str, Any]:
        value = contents.packet.contents()
        approved = self.approval
        owner = approved.task_id, approved.run_id, approved.source_snapshot_sha256
        if tuple(value[key] for key in ("task_id", "run_id", "source_snapshot_sha256")) != owner:
            raise ValueError("frozen context belongs to another host task")
        task = _object(raw["task/task.json"])
        workspace = _object(raw["workspace/workspace.json"])
        if (workspace != {"schema_version": 1, "task_id": approved.task_id, "run_id": approved.run_id,
                          "source_snapshot_sha256": approved.source_snapshot_sha256} or
                task["task_id"] != approved.task_id or task["source_snapshot_sha256"] != approved.source_snapshot_sha256 or
                task["crash_sha256"] != approved.crash_sha256):
            raise ValueError("frozen task or workspace approval differs")
        _verify_raw_material(contents.packet, raw, approved.source_snapshot_sha256)
        line_counts = {reference[len("workspace/base/"):]: len(blob.decode("utf-8").splitlines())
                       for reference, blob in raw.items() if reference.startswith("workspace/base/")}
        # Re-run original admission's cross-reference/range/version contract.
        # Self-consistent new hashes cannot legitimize an invalid raw bundle.
        for reference, blob in raw.items():
            if reference.startswith("workspace/diagnoses/") and reference.endswith(".json"):
                original = _object(blob)
                checked_diagnosis = Diagnosis.model_validate_json(encode(original["diagnosis"]))
                facts = [Evidence.model_validate_json(encode(item)) for item in original["evidence"]]
                claims = [RepairObligation.model_validate_json(encode(item)) for item in original.get("obligations", [])]
                if (checked_diagnosis.task_id, checked_diagnosis.run_id, checked_diagnosis.source_snapshot_sha256) != owner:
                    raise ValueError("frozen admission bundle belongs to another task")
                validate_references(checked_diagnosis, facts, line_counts, claims)
        focus = WorkingContextFocus.model_validate_json(encode(value["focus"]))
        if focus.diagnosis_id is None or not focus.hypothesis_ids:
            raise ValueError("applicability needs an explicit diagnosis and hypothesis focus")
        diagnosis = Diagnosis.model_validate_json(encode(value["diagnoses"][focus.diagnosis_id]))
        if not set(focus.hypothesis_ids) <= {h.hypothesis_id for h in diagnosis.hypotheses}:
            raise ValueError("frozen hypotheses belong to another diagnosis")
        for name in ("diagnoses", "obligations", "candidates", "decisions"):
            for record in value[name].values():
                if tuple(record[key] for key in ("task_id", "run_id", "source_snapshot_sha256")) != owner:
                    raise ValueError("frozen member owner or source differs")
        for record in value["diagnoses"].values():
            for hypothesis in record["hypotheses"]:
                if any(step["authority"] != "model_inference" for step in hypothesis["causal_steps"]):
                    raise ValueError("frozen causal inference authority was promoted")
        for identifier, record in value["evidence"].items():
            fact = Evidence.model_validate_json(encode(record))
            if ((fact.task_id, fact.run_id) != owner[:2] or
                    (fact.source_snapshot_sha256 != owner[2] and not
                     (fact.kind == "log_observation" and fact.source_snapshot_sha256 is None))):
                raise ValueError("frozen evidence owner or source differs")
            if fact.kind in {"source_fact", "log_observation"}:
                if fact.basis_ids or fact.execution_id is not None:
                    raise ValueError("frozen original fact has execution or model basis")
                if fact.kind == "source_fact":
                    location = fact.location
                    if location is None:
                        raise ValueError("frozen source fact lacks an original anchor")
                    lines = raw["workspace/base/" + location.path].decode("utf-8").splitlines()
                    if (location.snapshot_sha256 != owner[2] or location.end_line > len(lines) or
                            location.end_line - location.start_line + 1 > 200):
                        raise ValueError("frozen source fact range differs")
                else:
                    lines = raw["task/" + task["crash_log"]].decode("utf-8", errors="replace").splitlines()
                    count = fact.statement.count("\n") + 1
                    if fact.log_line is None or count > 200 or fact.log_line + count - 1 > len(lines):
                        raise ValueError("frozen log range differs")
            elif fact.kind == "model_inference":
                if not fact.basis_ids or fact.execution_id is not None:
                    raise ValueError("frozen inference lacks its registered basis")
            elif fact.kind in ("local_execution", "source_execution", "application_execution"):
                candidate = value["candidates"][fact.evaluated_candidate_id]
                if (fact.execution_snapshot_sha256 != candidate["candidate_snapshot_sha256"] or
                        fact.execution_scope is None or fact.kind != execution_evidence_kind(fact.execution_scope) or
                        fact.location is not None or fact.basis_ids):
                    raise ValueError("frozen execution candidate version differs")
            else:
                raise ValueError("application evidence is not admitted in this slice")
            if identifier != fact.record_id:
                raise ValueError("frozen evidence identity differs")
        if focus.current_candidate_id is not None:
            candidate = value["candidates"][focus.current_candidate_id]
            original = _object(raw["workspace/diagnoses/" + focus.diagnosis_id + ".json"])
            if not candidate_focus_matches(diagnosis, CandidatePatch.model_validate_json(encode(candidate)),
                    [Evidence.model_validate_json(encode(item)) for item in original["evidence"]],
                    {key: CandidatePatch.model_validate_json(encode(item)) for key, item in value["candidates"].items()}):
                raise ValueError("frozen candidate belongs to another focused diagnosis")
        for condition in value["conditions"].values():
            scope = condition["result"]["request"]["scope"]
            if tuple(scope[key] for key in ("task_id", "run_id", "source_snapshot_sha256")) != owner:
                raise ValueError("frozen condition owner differs")
        return value

    def _context(self, identifier: str, op: _Operation) -> tuple[ApplicabilityContext, dict[str, Any], RetrievalRecord]:
        context = self._saved("applicability-contexts", identifier, "app-context-", ApplicabilityContext, op)
        contents = ContextPayload.model_validate_json(context.payload_json)
        if contents.binding_sha256 != content_hash(self.approval):
            raise ValueError("context belongs to another host knowledge approval")
        _, retrieved = self._query(contents.bound_query_id, op)
        material = {}
        for ref in contents.materials:
            material[ref.reference] = op.read(self.workspace.root / "applicability-materials" / (ref.sha256 + ".blob"),
                                               expected_hash=ref.sha256, expected_bytes=ref.bytes)
        value = self._frozen_owner(contents, material)
        return context, value, retrieved

    def _use(self, use: EvidenceUse, value: dict[str, Any], proposal: ApplicabilityProposal,
             allowed: set[str]) -> None:
        if use.evidence_id not in allowed:
            raise ValueError("premise reference is not registered in the focused diagnosis")
        visiting: set[str] = set()
        visited: dict[str, bool] = {}

        def candidate_scoped(identifier: str) -> bool:
            if identifier in visiting:
                raise ValueError("premise inference basis cycle")
            if identifier in visited:
                return visited[identifier]
            visiting.add(identifier)
            fact = Evidence.model_validate_json(encode(value["evidence"][identifier]))
            current = fact.kind in ("local_execution", "source_execution", "application_execution")
            if current and (proposal.candidate_id is None or fact.evaluated_candidate_id != proposal.candidate_id or
                            fact.execution_snapshot_sha256 != proposal.candidate_snapshot_sha256):
                raise ValueError("historical candidate evidence cannot be used as current support")
            for basis in fact.basis_ids:
                current = candidate_scoped(basis) or current
            visiting.remove(identifier)
            visited[identifier] = current
            return current

        current = candidate_scoped(use.evidence_id)
        if current != (use.scope == "current_candidate"):
            raise ValueError("premise evidence scope disguises original or candidate observations")
        if current and (use.candidate_id != proposal.candidate_id or
                        use.candidate_snapshot_sha256 != proposal.candidate_snapshot_sha256):
            raise ValueError("premise evidence evaluated version differs")

    def _review_value(self, proposal: ApplicabilityProposal, op: _Operation) -> ReviewPayload:
        proposal = _copy(proposal)
        context, value, retrieved = self._context(proposal.context_id, op)
        focus = WorkingContextFocus.model_validate_json(encode(value["focus"]))
        candidate_id = focus.current_candidate_id
        candidate_snapshot = None if candidate_id is None else value["candidates"][candidate_id]["candidate_snapshot_sha256"]
        if ((proposal.task_id, proposal.run_id, proposal.source_snapshot_sha256) !=
                (self.approval.task_id, self.approval.run_id, self.approval.source_snapshot_sha256) or
                proposal.diagnosis_id != focus.diagnosis_id or proposal.hypothesis_ids != focus.hypothesis_ids or
                proposal.candidate_id != candidate_id or proposal.candidate_snapshot_sha256 != candidate_snapshot):
            raise ValueError("proposal differs from the explicit frozen focus")
        contents = ContextPayload.model_validate_json(context.payload_json)
        retrieval = RetrievalPayload.model_validate_json(retrieved.payload_json)
        hit = next((item for item in retrieval.hits if item.entry_id == proposal.entry_id), None)
        if hit is None or hit.content_sha256 != proposal.content_sha256 or hit.projection_sha256 != proposal.projection_sha256:
            raise ValueError("proposal experience is not in the approved returned query page")
        bundle = value["diagnoses"][focus.diagnosis_id]
        original = _object(op.read(self.workspace.root / "applicability-materials" / next(
            item.sha256 + ".blob" for item in contents.materials
            if item.reference == "workspace/diagnoses/" + str(focus.diagnosis_id) + ".json")))
        allowed = {item["record_id"] for item in original["evidence"]}
        for premise in proposal.premises:
            for use in (*premise.support, *premise.contradictions):
                self._use(use, value, proposal, allowed)
        states = {item.status for item in proposal.premises}
        status = "contradicted" if "contradicted" in states else ("unknown" if "unknown" in states else "supported")
        actions: dict[str, NextAction] = {"supported": "offer_process", "contradicted": "avoid_copy", "unknown": "clarify"}
        questions = tuple(PremiseQuestion(premise_id=premise.premise_id, question=premise.unknown_question)
                          for premise, assessment in zip(hit.projection.guidance.premises, proposal.premises, strict=True)
                          if assessment.status == "unknown")
        obligations = {identifier for hypothesis in bundle["hypotheses"] if hypothesis["hypothesis_id"] in focus.hypothesis_ids
                       for identifier in hypothesis["obligation_ids"]}
        if candidate_id is not None:
            candidate = value["candidates"][candidate_id]
            obligations.update((*candidate["restores"], *candidate["preserves"]))
        if not obligations <= set(value["obligations"]):
            raise ValueError("review verification obligations are not registered")
        return ReviewPayload(binding=self.approval, context_id=proposal.context_id,
                             bound_query_id=contents.bound_query_id, retrieval_id=retrieved.record_id, proposal=proposal,
                             projection=hit.projection, status=status, next_action=actions[status], questions=questions,
                             obligation_ids=tuple(sorted(obligations)))

    def bind(self) -> TaskKnowledgeBinding:
        """Explicit host publication; an existing run cannot change approval."""
        with self._operation() as op:
            knowledge_reads = self.retriever._reads()
            snapshot, _ = self.retriever.store._snapshot(self.approval.snapshot_sha256, reads=knowledge_reads)
            op.knowledge_bytes = knowledge_reads.byte_count
            contents = SnapshotPayload.model_validate_json(snapshot.payload_json)
            if contents.scope != self.approval.scope:
                raise ValueError("host approval scope differs from the frozen knowledge scope")
            raw = encode(self.approval).encode("utf-8")
            op.reserve(self.workspace.root / "experience-bindings" / "approved.json", len(raw))
            knowledge_reads.finish()
            op.finish(future_bytes=2 * len(raw), future_calls=2)
            self.io._publish("experience-bindings", "approved.json", raw)
            with self._operation() as audit:
                self._binding(audit)
            return _copy(self.approval)

    def record_query(self, *, tags: tuple[str, ...] = (), text: str = "", offset: int = 0,
                     limit: int | None = None) -> BoundQuery:
        """Only retrieval hints are variable; the host fills snapshot and scope."""
        with self._operation() as op:
            self._binding(op)
            query = MemoryQuery(snapshot_sha256=self.approval.snapshot_sha256, scope=self.approval.scope,
                                tags=tags, text=text, offset=offset, limit=limit)
            # First query without publication: reject total local+knowledge+
            # output capacity before a new retrieval record can be published.
            retrieved = self.retriever.query(query)
            payload = RetrievalPayload.model_validate_json(retrieved.payload_json)
            op.knowledge_bytes = payload.source_closure_bytes + payload.index_bytes + len(encode(retrieved).encode("utf-8"))
            result = _record(BoundQuery, "bound-query-", BoundQueryPayload(binding_sha256=content_hash(self.approval),
                              retrieval_id=retrieved.record_id, retrieval_sha256=retrieved.content_sha256))
            op.reserve(self.workspace.root / "experience-queries" / (result.record_id + ".json"),
                       len(encode(result).encode("utf-8")))
            op.finish(future_bytes=2 * len(encode(result).encode("utf-8")), future_calls=2)
            recorded = self.retriever.record(query)
            if recorded != retrieved:
                raise ValueError("query changed before bound publication")
            self._publish("experience-queries", result.record_id, result, op)
            if self.read_query(result.record_id) != result:
                raise ValueError("published bound query differs")
            return result

    def read_query(self, identifier: str) -> BoundQuery:
        with self._operation() as op:
            return self._query(identifier, op)[0]

    def prepare_context(self, bound_query_id: str, focus: WorkingContextFocus) -> ApplicabilityContext:
        """Capture a new host-selected source point; do not replace old context."""
        with self._operation() as op:
            self._query(bound_query_id, op)
            focus = WorkingContextFocus.model_validate_json(focus.model_dump_json())
            if focus.diagnosis_id is None or not focus.hypothesis_ids:
                raise ValueError("applicability needs a diagnosis and nonempty hypothesis focus")
            # Existing current-task verification reads are governed by the
            # outer managed Job. Its historical packet API has no cumulative
            # read accounting; the new capture and audit phases do below.
            packet = build_working_context(self.workspace, focus, task_directory=self.task_directory,
                                           ledger=self.ledger, max_bytes=MAX_CONTEXT_BYTES)
            material = _capture_material(self.workspace, self.task_directory, packet,
                                         reader=op.read, max_references=MAX_CONTEXT_FILES)
            references = tuple(ContextMaterial(reference=reference, sha256=digest(raw), bytes=len(raw))
                               for reference, raw in sorted(material.items()))
            payload = ContextPayload(binding_sha256=content_hash(self.approval), bound_query_id=bound_query_id,
                                     packet=packet, materials=references)
            self._frozen_owner(payload, material)
            context = _record(ApplicabilityContext, "app-context-", payload)
            context_path = self.workspace.root / "applicability-contexts" / (context.context_id + ".json")
            context_bytes = len(encode(context).encode("utf-8"))
            task = _object(material["task/task.json"])
            audit_reads = [
                (self.workspace.root / "workspace.json", len(material["workspace/workspace.json"])),
                (self.task_directory / "task.json", len(material["task/task.json"])),
                (self.task_directory / task["crash_log"], len(material["task/" + task["crash_log"]])),
                (context_path, context_bytes),
            ]
            for path in (self.workspace.root / "experience-bindings" / "approved.json",
                         self.workspace.root / "experience-queries" / (bound_query_id + ".json")):
                audit_reads.append((path, op.local.sizes[path]))
            audit_reads.extend((self.workspace.root / "applicability-materials" / (ref.sha256 + ".blob"), ref.bytes)
                               for ref in references)
            op.preflight_audit(audit_reads)
            # Reserve the complete context before publishing even one blob.
            op.reserve(context_path, context_bytes)
            op.finish(future_bytes=2 * context_bytes, future_calls=2)
            for raw in {digest(raw): raw for raw in material.values()}.values():
                self.io._publish("applicability-materials", digest(raw) + ".blob", raw)
            self._publish("applicability-contexts", context.context_id, context, op)
            # Re-read newly published bytes in a separate closed audit;
            # no historical reconstruction from current checks is performed.
            checked = self.read_context(context.context_id)
            if checked != context:
                raise ValueError("published applicability context differs")
            return context

    def read_context(self, identifier: str) -> ApplicabilityContext:
        with self._operation() as op:
            return self._context(identifier, op)[0]

    def review(self, proposal: ApplicabilityProposal) -> ApplicabilityReview:
        with self._operation() as op:
            payload = self._review_value(proposal, op)
            result = _record(ApplicabilityReview, "app-review-", payload)
            op.reserve(self.workspace.root / "applicability-reviews" / (result.record_id + ".json"),
                       len(encode(result).encode("utf-8")))
            op.finish(future_bytes=2 * len(encode(result).encode("utf-8")), future_calls=2)
            self._publish("applicability-reviews", result.record_id, result, op)
            if self.read_review(result.record_id) != result:
                raise ValueError("published applicability review differs")
            return result

    def read_review(self, identifier: str) -> ApplicabilityReview:
        with self._operation() as op:
            saved = self._saved("applicability-reviews", identifier, "app-review-", ApplicabilityReview, op)
            payload = ReviewPayload.model_validate_json(saved.payload_json)
            actual = _record(ApplicabilityReview, "app-review-", self._review_value(payload.proposal, op))
            if saved != actual:
                raise ValueError("review differs from the frozen source and complete proposal")
            return saved
