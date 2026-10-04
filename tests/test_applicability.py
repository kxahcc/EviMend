"""Current registered evidence scopes historical guidance without promoting it.

All fixtures are synthetic host declarations. A persisted supported proposal
remains model inference and never certifies application repair or root cause.
"""

from __future__ import annotations

import copy
import json
import os
import stat
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from test_decisions import diagnosis_submission
from test_knowledge_retrieval import admit, guidance, inventory
from test_working_context import failed_then_current, registered

from dfx_agent import applicability
from dfx_agent.applicability import ApplicabilityError, ApplicabilityStore
from dfx_agent.applicability_contracts import (
    ApplicabilityContext,
    ApplicabilityProposal,
    ApplicabilityReview,
    BoundQuery,
    TaskKnowledgeBinding,
)
from dfx_agent.knowledge import KnowledgeStore
from dfx_agent.knowledge_contracts import (
    ExperienceContent,
    PermissionScope,
    content_hash,
    encode,
)
from dfx_agent.knowledge_retrieval import KnowledgeRetriever
from dfx_agent.task import digest, verify_task
from dfx_agent.working_context import WorkingContextFocus


def two_premises() -> ExperienceContent:
    raw = guidance("width").model_dump(mode="json")
    raw["premises"] = [
        {"premise_id": "width", "statement": "The intended encoding uses two-byte elements.",
         "unknown_question": "Which consumer determines the element encoding?"},
        {"premise_id": "behavior", "statement": "The payload length must retain its business meaning.",
         "unknown_question": "What independent behavior must the patch preserve?"},
    ]
    return ExperienceContent.model_validate_json(encode(raw))


@dataclass
class Scenario:
    root: Path
    task: Path
    workspace: Any
    ledger: Any
    tools: dict[str, Any]
    focus: WorkingContextFocus
    retriever: KnowledgeRetriever
    approval: TaskKnowledgeBinding
    store: ApplicabilityStore
    entry_id: str
    bound: BoundQuery | None = None
    context: ApplicabilityContext | None = None
    old: str | None = None
    current: str | None = None

    def prepare(self) -> ApplicabilityContext:
        self.store.bind()
        self.bound = self.store.record_query(tags=("ByteWidth",), text="容量")
        self.context = self.store.prepare_context(self.bound.record_id, self.focus)
        return self.context

    def hit(self) -> dict[str, Any]:
        assert self.bound is not None
        retrieval = self.retriever.read_record(self.bound.contents()["retrieval_id"])
        return next(hit for hit in retrieval.contents()["hits"] if hit["entry_id"] == self.entry_id)

    def proposal(self, statuses: tuple[str, str] = ("supported", "supported"), *,
                 evidence_id: str = "source1", scope: str = "original_analysis",
                 evidence_candidate: str | None = None) -> ApplicabilityProposal:
        assert self.context is not None
        hit = self.hit()
        candidate = self.focus.current_candidate_id
        version = None if candidate is None else candidate_version(self, candidate)
        evidence_version = None if evidence_candidate is None else candidate_version(self, evidence_candidate)
        use = {"evidence_id": evidence_id, "scope": scope, "candidate_id": evidence_candidate,
               "candidate_snapshot_sha256": evidence_version}
        premises = []
        for premise_id, status in zip(("width", "behavior"), statuses, strict=True):
            premises.append({"premise_id": premise_id, "status": status,
                             "support": [use] if status == "supported" else [],
                             "contradictions": [use] if status == "contradicted" else [],
                             "reason": "A proposed semantic reading of registered facts; not verified causality."})
        raw = {"context_id": self.context.context_id, "entry_id": self.entry_id,
               "content_sha256": hit["content_sha256"], "projection_sha256": hit["projection_sha256"],
               "task_id": self.workspace.task_id, "run_id": self.workspace.run_id,
               "source_snapshot_sha256": self.workspace.source_snapshot_sha256,
               "diagnosis_id": self.focus.diagnosis_id, "hypothesis_ids": list(self.focus.hypothesis_ids),
               "candidate_id": candidate, "candidate_snapshot_sha256": version, "premises": premises}
        return ApplicabilityProposal.model_validate_json(encode(raw))


def scenario(tmp_path: Path, *, candidates: bool = False, prepared: bool = True) -> Scenario:
    if candidates:
        task, workspace, ledger, tools, focus, old, current, _ = failed_then_current(tmp_path)
    else:
        task, workspace, ledger, tools, _ = registered(tmp_path)
        focus = WorkingContextFocus(diagnosis_id="diagnosis1", hypothesis_ids=("h1",))
        old = current = None
    knowledge_root = tmp_path / "knowledge"
    knowledge_root.mkdir()
    knowledge = KnowledgeStore(knowledge_root)
    entry = admit(knowledge, tmp_path / "curated", "width", content=two_premises())
    scope = PermissionScope(split="development")
    snapshot = knowledge.freeze_snapshot((entry.entry_id,), scope)
    retriever = KnowledgeRetriever(knowledge)
    retriever.build_index(snapshot.snapshot_sha256, scope)
    approval = TaskKnowledgeBinding(task_id=workspace.task_id, run_id=workspace.run_id,
        source_snapshot_sha256=workspace.source_snapshot_sha256,
        crash_sha256=verify_task(task)["crash_sha256"], snapshot_sha256=snapshot.snapshot_sha256, scope=scope)
    store = ApplicabilityStore(workspace, task_directory=task, ledger=ledger, retriever=retriever, approval=approval)
    result = Scenario(tmp_path, task, workspace, ledger, tools, focus, retriever, approval, store,
                      entry.entry_id, old=old, current=current)
    if prepared:
        result.prepare()
    return result


def candidate_version(s: Scenario, candidate: str) -> str:
    source = s.workspace.require_registered(candidate)
    return json.loads((source.parent / "candidate.json").read_text(encoding="utf-8"))["candidate_snapshot_sha256"]


def packet(s: Scenario, context: ApplicabilityContext | None = None) -> dict[str, Any]:
    selected = context or s.context
    assert selected is not None
    return json.loads(selected.contents()["packet"]["canonical_payload_json"])


def mutated(proposal: ApplicabilityProposal, update) -> ApplicabilityProposal:
    raw = proposal.model_dump(mode="json")
    update(raw)
    return ApplicabilityProposal.model_validate_json(encode(raw))


def test_constructor_and_missing_reads_do_not_create_or_execute(tmp_path: Path) -> None:
    s = scenario(tmp_path, prepared=False)
    before = inventory(tmp_path)
    store = ApplicabilityStore(s.workspace, task_directory=s.task, ledger=s.ledger,
                              retriever=s.retriever, approval=s.approval)
    for call, identifier in ((store.read_query, "bound-query-" + "a" * 64),
                             (store.read_context, "app-context-" + "b" * 64),
                             (store.read_review, "app-review-" + "c" * 64)):
        with pytest.raises(ApplicabilityError):
            call(identifier)
    assert inventory(tmp_path) == before and s.ledger.executor.requests == []
    assert not (s.workspace.root / "experience-bindings").exists()


def test_query_requires_explicit_binding_and_never_selects_a_snapshot_from_disk(tmp_path: Path) -> None:
    s = scenario(tmp_path, prepared=False)
    before = inventory(tmp_path)
    with pytest.raises(ApplicabilityError):
        s.store.record_query(tags=("ByteWidth",))
    assert inventory(tmp_path) == before
    assert not (s.workspace.root / "experience-queries").exists()


def test_binding_is_idempotent_and_pins_host_approval_for_readers(tmp_path: Path) -> None:
    s = scenario(tmp_path)
    before = inventory(tmp_path)
    assert s.store.bind() == s.approval
    assert inventory(tmp_path) == before
    altered = s.approval.model_copy(update={"snapshot_sha256": "a" * 64})
    other = ApplicabilityStore(s.workspace, task_directory=s.task, ledger=s.ledger,
                               retriever=s.retriever, approval=altered)
    assert s.bound is not None
    with pytest.raises(ApplicabilityError):
        other.bind()
    with pytest.raises(ApplicabilityError):
        other.read_query(s.bound.record_id)
    assert inventory(tmp_path) == before


@pytest.mark.parametrize("field,value", [("task_id", "other-task"), ("run_id", "other-run"),
    ("source_snapshot_sha256", "a" * 64), ("crash_sha256", "b" * 64)], ids=["task", "run", "source", "crash"])
def test_host_binding_must_match_actual_task_and_run(tmp_path: Path, field: str, value: str) -> None:
    s = scenario(tmp_path, prepared=False)
    approval = s.approval.model_copy(update={field: value})
    before = inventory(tmp_path)
    with pytest.raises(ApplicabilityError):
        store = ApplicabilityStore(s.workspace, task_directory=s.task, ledger=s.ledger,
                                   retriever=s.retriever, approval=approval)
        store.bind()
    assert inventory(tmp_path) == before and s.ledger.executor.requests == []


def test_bound_query_keeps_complete_common_projection_and_host_frozen_scope(tmp_path: Path) -> None:
    s = scenario(tmp_path)
    assert s.bound is not None
    before = inventory(tmp_path)
    restored = s.store.read_query(s.bound.record_id)
    retrieval = s.retriever.read_record(restored.contents()["retrieval_id"])
    raw = retrieval.contents()
    assert raw["query"]["snapshot_sha256"] == s.approval.snapshot_sha256
    assert raw["query"]["scope"] == s.approval.scope.model_dump(mode="json")
    assert raw["hits"][0]["projection"]["guidance"] == two_premises().model_dump(mode="json")
    assert restored == s.bound and inventory(tmp_path) == before
    assert s.ledger.executor.requests == []


@pytest.mark.parametrize("focus", [WorkingContextFocus(), WorkingContextFocus(diagnosis_id="diagnosis1"),
    WorkingContextFocus(diagnosis_id="absent", hypothesis_ids=("h1",)),
    WorkingContextFocus(diagnosis_id="diagnosis1", hypothesis_ids=("absent",))],
    ids=["initial", "no-hypothesis", "wrong-diagnosis", "wrong-hypothesis"])
def test_prepare_requires_registered_explicit_diagnosis_hypotheses(tmp_path: Path, focus: WorkingContextFocus) -> None:
    s = scenario(tmp_path, prepared=False)
    s.store.bind()
    bound = s.store.record_query(tags=("ByteWidth",))
    before = inventory(tmp_path)
    with pytest.raises(ApplicabilityError):
        s.store.prepare_context(bound.record_id, focus)
    assert inventory(tmp_path) == before and s.ledger.executor.requests == []


@pytest.mark.parametrize("statuses,status,action,question_ids", [
    (("supported", "supported"), "supported", "offer_process", []),
    (("unknown", "supported"), "unknown", "clarify", ["width"]),
    (("supported", "unknown"), "unknown", "clarify", ["behavior"]),
    (("unknown", "unknown"), "unknown", "clarify", ["width", "behavior"]),
    (("contradicted", "supported"), "contradicted", "avoid_copy", []),
    (("supported", "contradicted"), "contradicted", "avoid_copy", []),
    (("unknown", "contradicted"), "contradicted", "avoid_copy", ["width"]),
], ids=["supported", "unknown-first", "unknown-second", "unknown-both", "conflict",
        "same-trigger-different-business", "conflict-over-unknown"])
def test_tristate_guidance_retains_complete_projection_and_inference_authority(
        tmp_path: Path, statuses, status: str, action: str, question_ids: list[str]) -> None:
    s = scenario(tmp_path)
    review = s.store.review(s.proposal(statuses))
    raw = review.contents()
    assert raw["status"] == status and raw["next_action"] == action
    assert raw["semantic_authority"] == "model_inference"
    assert raw["current_evidence_authentication"] == "registered_frozen_sources"
    assert raw["application_verification"] == "not_run"
    assert raw["projection"] == s.hit()["projection"]
    assert [question["premise_id"] for question in raw["questions"]] == question_ids
    expected = {p.premise_id: p.unknown_question for p in two_premises().premises}
    assert all(question["question"] == expected[question["premise_id"]] for question in raw["questions"])
    assert "keep_value" in raw["obligation_ids"]
    before = inventory(tmp_path)
    assert s.store.read_review(review.record_id) == review and inventory(tmp_path) == before
    assert s.ledger.executor.requests == []
    assert packet(s)["diagnoses"]["diagnosis1"]["hypotheses"][0]["support_status"] == "unknown"


def test_model_inference_reference_remains_inference_after_persistence(tmp_path: Path) -> None:
    s = scenario(tmp_path)
    review = s.store.review(s.proposal(evidence_id="infer1"))
    assert review.contents()["semantic_authority"] == "model_inference"
    assert packet(s)["evidence"]["infer1"]["kind"] == "model_inference"
    assert s.store.read_review(review.record_id) == review


def test_cited_but_inconclusive_evidence_still_returns_original_unknown_question(tmp_path: Path) -> None:
    s = scenario(tmp_path)
    proposal = s.proposal(("unknown", "supported"))
    supported = s.proposal().model_dump(mode="json")["premises"][0]["support"]
    proposal = mutated(proposal, lambda raw: raw["premises"][0].update(support=supported))
    review = s.store.review(proposal)
    assert review.contents()["status"] == "unknown"
    assert review.contents()["questions"][0]["question"] == two_premises().premises[0].unknown_question
    assert review.contents()["semantic_authority"] == "model_inference"


@pytest.mark.parametrize("fault", ["task", "run", "source", "diagnosis", "hypothesis", "context",
    "entry", "content", "projection", "missing-premise", "extra-premise", "fake-ref", "candidate"],
    ids=["task", "run", "source", "diagnosis", "hypothesis", "context", "entry", "content", "projection",
         "missing-premise", "extra-premise", "fake-ref", "candidate"])
def test_invalid_owner_hit_coverage_or_reference_writes_no_review(tmp_path: Path, fault: str) -> None:
    s = scenario(tmp_path)
    raw = s.proposal().model_dump(mode="json")
    if fault in {"task", "run"}:
        raw[f"{fault}_id"] = "other"
    elif fault in {"content", "projection"}:
        raw[f"{fault}_sha256"] = "a" * 64
        if fault == "content":
            raw["entry_id"] = "experience-" + "a" * 64
    elif fault == "source":
        raw["source_snapshot_sha256"] = "b" * 64
    elif fault == "diagnosis":
        raw["diagnosis_id"] = "absent"
    elif fault == "hypothesis":
        raw["hypothesis_ids"] = ["h2"]
    elif fault == "context":
        raw["context_id"] = "app-context-" + "c" * 64
    elif fault == "entry":
        raw.update(entry_id="experience-" + "d" * 64, content_sha256="d" * 64)
    elif fault == "missing-premise":
        raw["premises"].pop()
    elif fault == "extra-premise":
        raw["premises"].append({**raw["premises"][0], "premise_id": "invented"})
    elif fault == "fake-ref":
        raw["premises"][0]["support"][0]["evidence_id"] = "invented"
    else:
        raw.update(candidate_id="e" * 64, candidate_snapshot_sha256="f" * 64)
    proposal = ApplicabilityProposal.model_validate_json(encode(raw))
    before = inventory(tmp_path)
    with pytest.raises(ApplicabilityError):
        s.store.review(proposal)
    assert inventory(tmp_path) == before and s.ledger.executor.requests == []


def test_registered_evidence_from_unfocused_diagnosis_cannot_be_used(tmp_path: Path) -> None:
    s = scenario(tmp_path, prepared=False)
    raw = diagnosis_submission(s.task)
    raw["diagnosis"]["record_id"] = "other-diagnosis"
    raw["diagnosis"]["hypotheses"] = [{"hypothesis_id": "h_other", "support_status": "unknown",
                                          "support_evidence_ids": ["source_other"]}]
    raw["diagnosis"]["selected_hypothesis_id"] = "h_other"
    raw["source_citations"][0]["evidence_id"] = "source_other"
    raw.update(inferences=[], obligations=[])
    result = json.loads(s.tools["record_diagnosis"].invoke({"diagnosis_json": json.dumps(raw)}))
    assert result["diagnosis_id"] == "other-diagnosis"
    s.prepare()
    before = inventory(tmp_path)
    with pytest.raises(ApplicabilityError):
        s.store.review(s.proposal(evidence_id="source_other"))
    assert inventory(tmp_path) == before


def test_entry_not_returned_on_page_cannot_be_reviewed(tmp_path: Path) -> None:
    s = scenario(tmp_path, prepared=False)
    second = admit(s.retriever.store, tmp_path / "other-guidance", "offset")
    snapshot = s.retriever.store.freeze_snapshot((s.entry_id, second.entry_id), s.approval.scope)
    s.retriever.build_index(snapshot.snapshot_sha256, s.approval.scope)
    approval = s.approval.model_copy(update={"snapshot_sha256": snapshot.snapshot_sha256})
    s.approval = approval
    s.store = ApplicabilityStore(s.workspace, task_directory=s.task, ledger=s.ledger,
                                retriever=s.retriever, approval=approval)
    s.store.bind()
    s.bound = s.store.record_query(offset=2, limit=1)
    s.context = s.store.prepare_context(s.bound.record_id, s.focus)
    entry = s.retriever.store.read_entry(s.entry_id)
    projection = entry.contents()["projection"]
    raw = {"context_id": s.context.context_id, "entry_id": s.entry_id,
           "content_sha256": entry.content_sha256, "projection_sha256": content_hash(projection),
           "task_id": s.workspace.task_id, "run_id": s.workspace.run_id,
           "source_snapshot_sha256": s.workspace.source_snapshot_sha256,
           "diagnosis_id": "diagnosis1", "hypothesis_ids": ["h1"], "premises": [
               {"premise_id": premise.premise_id, "status": "unknown", "reason": "not established"}
               for premise in two_premises().premises]}
    before = inventory(tmp_path)
    with pytest.raises(ApplicabilityError):
        s.store.review(ApplicabilityProposal.model_validate_json(encode(raw)))
    assert inventory(tmp_path) == before


def execution_diagnosis(s: Scenario, observed: str, *, inference: bool) -> None:
    result = s.ledger.completed(observed)
    assert result is not None and result.status == "fail"
    raw = diagnosis_submission(s.task)
    raw["diagnosis"].update(record_id="execution-diagnosis", selected_hypothesis_id="h_execution",
        feedback_candidate_id=observed, hypotheses=[{"hypothesis_id": "h_execution", "support_status": "unknown",
            "support_evidence_ids": ["infer_execution" if inference else "execution1"]}])
    raw.update(source_citations=[], obligations=[], inferences=[], existing_evidence_ids=["source1"])
    raw["feedback_citations"] = [{"evidence_id": "execution1", "candidate_id": observed,
        "candidate_snapshot_sha256": candidate_version(s, observed), "execution_id": result.idempotency_key}]
    if inference:
        raw["inferences"] = [
            {"evidence_id": "infer_middle", "statement": "An intermediate reading of the failed check",
             "basis_ids": ["execution1"]},
            {"evidence_id": "infer_execution", "statement": "An inference from an intermediate failed-check reading",
             "basis_ids": ["infer_middle"]},
        ]
    saved = json.loads(s.tools["record_diagnosis"].invoke({"diagnosis_json": json.dumps(raw)}))
    assert saved["diagnosis_id"] == "execution-diagnosis"
    s.focus = WorkingContextFocus(diagnosis_id="execution-diagnosis", hypothesis_ids=("h_execution",),
                                  current_candidate_id=s.current)


def test_new_diagnosis_cannot_claim_candidate_focus_without_registered_failed_observation(tmp_path: Path) -> None:
    s = scenario(tmp_path, candidates=True, prepared=False)
    assert s.current is not None
    raw = diagnosis_submission(s.task)
    raw["diagnosis"].update(record_id="claimed-feedback", selected_hypothesis_id="h_claim",
        feedback_candidate_id=s.current, hypotheses=[{"hypothesis_id": "h_claim", "support_status": "unknown",
                                                     "support_evidence_ids": ["source1"]}])
    raw.update(source_citations=[], obligations=[], inferences=[], existing_evidence_ids=["source1"])
    result = json.loads(s.tools["record_diagnosis"].invoke({"diagnosis_json": json.dumps(raw)}))
    assert result["diagnosis_id"] == "claimed-feedback"
    focus = WorkingContextFocus(diagnosis_id="claimed-feedback", hypothesis_ids=("h_claim",),
                                current_candidate_id=s.current)
    s.store.bind()
    bound = s.store.record_query(tags=("ByteWidth",))
    before = inventory(tmp_path)
    with pytest.raises(ApplicabilityError):
        s.store.prepare_context(bound.record_id, focus)
    assert inventory(tmp_path) == before and len(s.ledger.executor.requests) == 1


@pytest.mark.parametrize("inference", [False, True], ids=["direct-execution", "inference-basis"])
def test_historical_candidate_observation_cannot_be_current_or_original_evidence(tmp_path: Path, inference: bool) -> None:
    s = scenario(tmp_path, candidates=True, prepared=False)
    assert s.old is not None and s.current is not None
    execution_diagnosis(s, s.old, inference=inference)
    s.prepare()
    evidence = "infer_execution" if inference else "execution1"
    for scope, candidate in (("original_analysis", None), ("current_candidate", s.current)):
        proposal = s.proposal(evidence_id=evidence, scope=scope, evidence_candidate=candidate)
        before = inventory(tmp_path)
        with pytest.raises(ApplicabilityError):
            s.store.review(proposal)
        assert inventory(tmp_path) == before
    old_scope = s.proposal(evidence_id=evidence, scope="current_candidate", evidence_candidate=s.current)
    raw = old_scope.model_dump(mode="json")
    raw.update(candidate_id=s.old, candidate_snapshot_sha256=candidate_version(s, s.old))
    for premise in raw["premises"]:
        premise["support"][0].update(candidate_id=s.old, candidate_snapshot_sha256=candidate_version(s, s.old))
    old_scope = ApplicabilityProposal.model_validate_json(encode(raw))
    before = inventory(tmp_path)
    with pytest.raises(ApplicabilityError):
        s.store.review(old_scope)
    assert inventory(tmp_path) == before
    assert len(s.ledger.executor.requests) == 1


@pytest.mark.parametrize("inference", [False, True], ids=["direct-execution", "inference-basis"])
def test_actual_current_candidate_observation_requires_exact_execution_scope(tmp_path: Path, inference: bool) -> None:
    s = scenario(tmp_path, prepared=False)
    s.tools["edit_source"].invoke({"path": "src/main.ets", "old": "bad", "new": "almost"})
    s.current = json.loads(s.tools["register_candidate"].invoke(
        {"diagnosis_id": "diagnosis1", "preserves": ["keep_value"]}))["candidate_id"]
    assert s.current is not None
    # Register and run a genuinely failed current synthetic candidate; the
    # check protocol and receipt retain the original executor's identity.
    source = s.workspace.require_registered(s.current)
    result = s.ledger.run(s.current)
    assert result.status == "fail" and source.exists()
    execution_diagnosis(s, s.current, inference=inference)
    s.prepare()
    evidence = "infer_execution" if inference else "execution1"
    review = s.store.review(s.proposal(evidence_id=evidence, scope="current_candidate", evidence_candidate=s.current))
    assert review.contents()["semantic_authority"] == "model_inference"
    before = inventory(tmp_path)
    with pytest.raises(ApplicabilityError):
        s.store.review(s.proposal(evidence_id=evidence))
    assert inventory(tmp_path) == before
    proposal = s.proposal(evidence_id=evidence, scope="current_candidate", evidence_candidate=s.current)
    raw = proposal.model_dump(mode="json")
    raw["candidate_snapshot_sha256"] = "a" * 64
    for premise in raw["premises"]:
        premise["support"][0]["candidate_snapshot_sha256"] = "a" * 64
    wrong_version = ApplicabilityProposal.model_validate_json(encode(raw))
    with pytest.raises(ApplicabilityError):
        s.store.review(wrong_version)
    assert inventory(tmp_path) == before


def test_original_source_cannot_be_relabelled_as_current_candidate_observation(tmp_path: Path) -> None:
    s = scenario(tmp_path, candidates=True)
    assert s.current is not None
    before = inventory(tmp_path)
    with pytest.raises(ApplicabilityError):
        s.store.review(s.proposal(scope="current_candidate", evidence_candidate=s.current))
    assert inventory(tmp_path) == before


def test_later_check_changes_new_context_not_old_frozen_readback(tmp_path: Path) -> None:
    s = scenario(tmp_path, candidates=True)
    assert s.context is not None and s.bound is not None and s.current is not None
    old_context = s.context
    old_review = s.store.review(s.proposal())
    assert packet(s)["checks"][s.current]["state"] == "not_requested"
    saved = json.loads(s.tools["run_check"].invoke({"candidate_id": s.current}))
    assert saved["status"] == "pass"
    calls = copy.deepcopy(s.ledger.executor.requests)
    before = inventory(tmp_path)
    assert s.store.read_context(old_context.context_id) == old_context
    assert s.store.read_review(old_review.record_id) == old_review
    assert inventory(tmp_path) == before and s.ledger.executor.requests == calls
    new = s.store.prepare_context(s.bound.record_id, s.focus)
    assert new.context_id != old_context.context_id
    assert packet(s, new)["checks"][s.current]["result"]["status"] == "pass"
    assert packet(s, old_context)["checks"][s.current]["result"] is None


@pytest.mark.parametrize("fault", ["missing", "corrupt", "same-size-restored-time"],
                         ids=["missing", "corrupt", "same-size-restored-time"])
def test_frozen_material_missing_or_changed_rejects_without_republication(tmp_path: Path, fault: str) -> None:
    s = scenario(tmp_path)
    assert s.context is not None
    review = s.store.review(s.proposal())
    material = s.context.contents()["materials"][0]
    path = s.workspace.root / "applicability-materials" / (material["sha256"] + ".blob")
    original = path.read_bytes()
    info = path.stat()
    if fault == "missing":
        path.unlink()
    else:
        path.write_bytes(b"!" + original[1:] if fault == "same-size-restored-time" else b"broken")
        if fault == "same-size-restored-time":
            os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns))
    before = inventory(tmp_path)
    with pytest.raises(ApplicabilityError):
        s.store.read_context(s.context.context_id)
    with pytest.raises(ApplicabilityError):
        s.store.read_review(review.record_id)
    assert inventory(tmp_path) == before and s.ledger.executor.requests == []


def test_context_reads_use_frozen_materials_and_do_not_require_live_registration(tmp_path: Path) -> None:
    s = scenario(tmp_path)
    assert s.context is not None
    review = s.store.review(s.proposal())
    (s.workspace.root / "diagnoses/diagnosis1.json").unlink()
    before = inventory(tmp_path)
    assert s.store.read_context(s.context.context_id) == s.context
    assert s.store.read_review(review.record_id) == review
    assert inventory(tmp_path) == before and s.ledger.executor.requests == []


@pytest.mark.parametrize("constant", ["MAX_CONTEXT_BYTES", "MAX_CONTEXT_READ_BYTES", "MAX_CONTEXT_READ_CALLS"],
                         ids=["closure-capacity", "cumulative-read-bytes", "cumulative-read-calls"])
def test_read_limits_fail_closed_without_writing_or_executing(tmp_path: Path, monkeypatch, constant: str) -> None:
    s = scenario(tmp_path)
    assert s.context is not None
    review = s.store.review(s.proposal())
    monkeypatch.setattr(applicability, constant, 1)
    before = inventory(tmp_path)
    with pytest.raises(ApplicabilityError):
        s.store.read_review(review.record_id)
    assert inventory(tmp_path) == before and s.ledger.executor.requests == []


def test_forged_review_with_consistent_new_hash_cannot_change_verified_guidance(tmp_path: Path) -> None:
    s = scenario(tmp_path)
    review = s.store.review(s.proposal())
    raw = review.model_dump(mode="json")
    payload = review.contents()
    payload["projection"]["guidance"]["procedure"][0]["instruction"] = "Silently discard the business payload."
    payload["proposal"]["projection_sha256"] = content_hash(payload["projection"])
    raw["payload_json"] = encode(payload)
    raw["content_sha256"] = content_hash(payload)
    raw["record_id"] = "app-review-" + raw["content_sha256"]
    ApplicabilityReview.model_validate_json(encode(raw))
    path = s.workspace.root / "applicability-reviews" / (raw["record_id"] + ".json")
    path.write_text(encode(raw), encoding="utf-8")
    before = inventory(tmp_path)
    with pytest.raises(ApplicabilityError):
        s.store.read_review(raw["record_id"])
    assert inventory(tmp_path) == before and s.ledger.executor.requests == []


def test_forged_context_cannot_change_a_fact_by_rehashing_every_envelope(tmp_path: Path) -> None:
    s = scenario(tmp_path)
    assert s.context is not None
    payload = s.context.contents()
    frozen_packet = payload["packet"]
    facts = json.loads(frozen_packet["canonical_payload_json"])
    facts["evidence"]["source1"].update(statement="value=good", artifact_sha256=digest(b"value=good"))
    frozen_packet["canonical_payload_json"] = encode(facts)
    frozen_packet["payload_sha256"] = digest(frozen_packet["canonical_payload_json"].encode("utf-8"))
    raw = s.context.model_dump(mode="json")
    raw["payload_json"] = encode(payload)
    raw["payload_sha256"] = content_hash(payload)
    raw["context_id"] = "app-context-" + raw["payload_sha256"]
    ApplicabilityContext.model_validate_json(encode(raw))
    (s.workspace.root / "applicability-contexts" / (raw["context_id"] + ".json")).write_text(
        encode(raw), encoding="utf-8")
    before = inventory(tmp_path)
    with pytest.raises(ApplicabilityError):
        s.store.read_context(raw["context_id"])
    assert inventory(tmp_path) == before and s.ledger.executor.requests == []


@pytest.mark.parametrize("fault", ["knowledge-material", "retrieval-record", "bound-query", "binding"],
                         ids=["knowledge-material", "retrieval-record", "bound-query", "binding"])
def test_audit_reads_recheck_entire_frozen_knowledge_and_binding_closure(tmp_path: Path, fault: str) -> None:
    s = scenario(tmp_path)
    assert s.bound is not None and s.context is not None
    review = s.store.review(s.proposal())
    if fault == "knowledge-material":
        path = next((s.retriever.store.root / "materials").glob("*.blob"))
    elif fault == "retrieval-record":
        path = s.retriever.store.root / "retrievals" / (s.bound.contents()["retrieval_id"] + ".json")
    elif fault == "bound-query":
        path = s.workspace.root / "experience-queries" / (s.bound.record_id + ".json")
    else:
        path = s.workspace.root / "experience-bindings/approved.json"
    path.write_bytes(b"broken")
    before = inventory(tmp_path)
    for reader, record_id in ((s.store.read_query, s.bound.record_id),
                              (s.store.read_context, s.context.context_id),
                              (s.store.read_review, review.record_id)):
        with pytest.raises(ApplicabilityError):
            reader(record_id)
    assert inventory(tmp_path) == before and s.ledger.executor.requests == []


@pytest.mark.parametrize("folder", ["applicability-materials", "applicability-contexts", "applicability-reviews"],
                         ids=["material-parent", "context-parent", "review-parent"])
def test_reparse_parent_is_rejected_before_opening_frozen_children(tmp_path: Path, monkeypatch, folder: str) -> None:
    s = scenario(tmp_path)
    review = s.store.review(s.proposal())
    parent = s.workspace.root / folder
    original = Path.lstat
    reads: list[Path] = []
    before = inventory(tmp_path)

    def linked(path, *args, **kwargs):
        info = original(path, *args, **kwargs)
        if path.absolute() == parent.absolute():
            values = {key: getattr(info, key) for key in dir(info) if key.startswith("st_")}
            values["st_file_attributes"] = getattr(info, "st_file_attributes", 0) | getattr(
                stat, "FILE_ATTRIBUTE_REPARSE_POINT", 1024)
            return SimpleNamespace(**values)
        if path.absolute().is_relative_to(parent.absolute()):
            reads.append(path)
        return info

    monkeypatch.setattr(Path, "lstat", linked)
    with pytest.raises(ApplicabilityError):
        s.store.read_review(review.record_id)
    assert reads == []
    monkeypatch.undo()
    assert inventory(tmp_path) == before and s.ledger.executor.requests == []


def test_identical_prepare_and_review_reuse_exact_original_records(tmp_path: Path) -> None:
    s = scenario(tmp_path)
    assert s.bound is not None and s.context is not None
    proposal = s.proposal()
    review = s.store.review(proposal)
    before = inventory(tmp_path)
    assert s.store.prepare_context(s.bound.record_id, s.focus) == s.context
    assert s.store.review(proposal) == review
    assert inventory(tmp_path) == before and s.ledger.executor.requests == []


def test_context_and_review_return_independent_copies(tmp_path: Path) -> None:
    s = scenario(tmp_path)
    assert s.context is not None
    review = s.store.review(s.proposal(("unknown", "supported")))
    returned = review.contents()
    returned["projection"]["guidance"]["premises"].clear()
    returned["questions"].clear()
    returned_context = s.context.contents()
    returned_context["materials"].clear()
    assert len(review.contents()["projection"]["guidance"]["premises"]) == 2
    assert len(review.contents()["questions"]) == 1 and s.context.contents()["materials"]
    assert ApplicabilityReview.model_validate_json(review.model_dump_json()) == review
    assert s.store.read_review(review.record_id) == review


@pytest.mark.parametrize("operation", ["bind", "query", "context", "review"],
                         ids=["bind-close", "query-close", "context-close", "review-close"])
def test_closing_read_budget_is_checked_before_new_publication(tmp_path: Path, monkeypatch, operation: str) -> None:
    s = scenario(tmp_path, prepared=operation == "review")
    if operation == "bind":
        call = s.store.bind
        limit = 5  # Three initial owner reads fit; closing reads cannot fit.
    elif operation == "query":
        s.store.bind()
        call = partial(s.store.record_query, tags=("ByteWidth",))
        limit = 7  # Owner and binding fit, but closing and next audit do not.
    elif operation == "context":
        s.store.bind()
        bound = s.store.record_query(tags=("ByteWidth",))
        call = partial(s.store.prepare_context, bound.record_id, s.focus)
        limit = 13
    else:
        assert s.context is not None
        proposal = s.proposal()
        call = partial(s.store.review, proposal)
        limit = len(s.context.contents()["materials"]) + 8
    before = inventory(tmp_path)
    requests = list(s.ledger.executor.requests)
    monkeypatch.setattr(applicability, "MAX_CONTEXT_READ_CALLS", limit)
    with pytest.raises(ApplicabilityError) as rejected:
        call()
    assert "closing read budget" in str(rejected.value.__cause__)
    assert inventory(tmp_path) == before and s.ledger.executor.requests == requests


@pytest.mark.parametrize("limit", [22, 23])
def test_context_audit_owner_copies_are_budgeted_before_any_publication(tmp_path: Path, monkeypatch, limit: int) -> None:
    s = scenario(tmp_path, prepared=False)
    s.store.bind()
    bound = s.store.record_query(tags=("ByteWidth",))
    before = inventory(tmp_path)
    monkeypatch.setattr(applicability, "MAX_CONTEXT_READ_CALLS", limit)
    with pytest.raises(ApplicabilityError) as rejected:
        s.store.prepare_context(bound.record_id, s.focus)
    assert "audit closing read budget" in str(rejected.value.__cause__)
    assert inventory(tmp_path) == before and s.ledger.executor.requests == []


@pytest.mark.parametrize("metric", ["calls", "bytes", "capacity"])
def test_context_preflight_matches_actual_separate_audit(tmp_path: Path, monkeypatch, metric: str) -> None:
    s = scenario(tmp_path, prepared=False)
    s.store.bind()
    bound = s.store.record_query(tags=("ByteWidth",))
    measured = []
    initial = []
    original_read = applicability._Operation.read
    original_finish = applicability._Operation.finish

    def read(op, *args, **kwargs):
        result = original_read(op, *args, **kwargs)
        initial.append((args[0], len(result)))
        return result

    def finish(op, **kwargs):
        if not op.closed:
            measured.append({"calls": op.local.read_calls + op.task.read_calls +
                             len(op.local.receipts) + len(op.task.receipts),
                             "bytes": op.local.read_bytes + op.task.read_bytes +
                             sum(item[1] for reads in (op.local, op.task) for item in reads.receipts.values()),
                             "capacity": op.local.byte_count + op.task.byte_count + op.knowledge_bytes})
        return original_finish(op, **kwargs)

    context = s.store.prepare_context(bound.record_id, s.focus)
    monkeypatch.setattr(applicability._Operation, "read", read)
    monkeypatch.setattr(applicability._Operation, "finish", finish)
    assert s.store.read_context(context.context_id) == context
    assert len(measured) == 1
    costs = measured[0]
    monkeypatch.setattr(applicability._Operation, "read", original_read)
    monkeypatch.setattr(applicability._Operation, "finish", original_finish)
    # Reuse the measured audit sequence with its real knowledge allocation.
    with s.store._operation() as op:
        s.store._query(bound.record_id, op)
        op.preflight_audit(initial)
        constant = {"calls": "MAX_CONTEXT_READ_CALLS", "bytes": "MAX_CONTEXT_READ_BYTES",
                    "capacity": "MAX_CONTEXT_BYTES"}[metric]
        before = inventory(tmp_path)
        monkeypatch.setattr(applicability, constant, costs[metric] - 1)
        with pytest.raises(ValueError, match="audit"):
            op.preflight_audit(initial)
        monkeypatch.undo()
        assert inventory(tmp_path) == before
    assert s.ledger.executor.requests == []


@pytest.mark.parametrize("fault", ["missing-hypothesis-basis", "foreign-root-cause-version"],
                         ids=["missing-hypothesis-basis", "foreign-root-cause-version"])
def test_rehashed_frozen_bundle_must_repeat_original_cross_reference_admission(tmp_path: Path, fault: str) -> None:
    s = scenario(tmp_path)
    assert s.context is not None
    payload = s.context.contents()
    reference = "workspace/diagnoses/diagnosis1.json"
    material = next(item for item in payload["materials"] if item["reference"] == reference)
    bundle_path = s.workspace.root / "applicability-materials" / (material["sha256"] + ".blob")
    bundle = json.loads(bundle_path.read_bytes())
    hypothesis = bundle["diagnosis"]["hypotheses"][0]
    if fault == "missing-hypothesis-basis":
        hypothesis["support_evidence_ids"] = ["not_registered"]
    else:
        hypothesis["root_cause_locations"] = [{"path": "src/main.ets", "start_line": 1, "end_line": 1,
                                                "snapshot_sha256": "f" * 64}]
    raw_bundle = encode(bundle).encode("utf-8")
    material.update(sha256=digest(raw_bundle), bytes=len(raw_bundle))
    (s.workspace.root / "applicability-materials" / (material["sha256"] + ".blob")).write_bytes(raw_bundle)
    frozen_packet = payload["packet"]
    value = json.loads(frozen_packet["canonical_payload_json"])
    value["diagnoses"]["diagnosis1"] = bundle["diagnosis"]
    for source in value["sources"]:
        if source["reference"] == reference:
            source.update(sha256=material["sha256"], bytes=material["bytes"])
    frozen_packet["canonical_payload_json"] = encode(value)
    frozen_packet["payload_sha256"] = digest(frozen_packet["canonical_payload_json"].encode("utf-8"))
    raw = s.context.model_dump(mode="json")
    raw.update(payload_json=encode(payload), payload_sha256=content_hash(payload),
               context_id="app-context-" + content_hash(payload))
    forged = ApplicabilityContext.model_validate_json(encode(raw))
    (s.workspace.root / "applicability-contexts" / (forged.context_id + ".json")).write_text(encode(forged), encoding="utf-8")
    before = inventory(tmp_path)
    with pytest.raises(ApplicabilityError):
        s.store.read_context(forged.context_id)
    assert inventory(tmp_path) == before and s.ledger.executor.requests == []
