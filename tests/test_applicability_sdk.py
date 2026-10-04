"""Fixed SDK calls use complete experience context and durable review receipts."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from langchain_core.messages import ToolMessage
from test_agent_loop import response
from test_applicability import scenario
from test_decisions import FULL, diagnosis_submission
from test_readonly_evidence import tree_bytes

from dfx_agent import models
from dfx_agent.actions import ToolActionStore
from dfx_agent.agent import run_fixed_agent
from dfx_agent.applicability_handoff import ApplicabilityPolicy
from dfx_agent.knowledge_contracts import encode
from dfx_agent.knowledge_run import HostKnowledgeInput
from dfx_agent.verification import FixedTextExecutor


def proposal_from_result(prepared: dict, statuses: tuple[str, ...]) -> dict:
    context = prepared["context"]
    payload = json.loads(context["payload_json"])
    value = json.loads(payload["packet"]["canonical_payload_json"])
    hit = prepared["guidance"]["retrieval"]["hits"][0]
    focus = value["focus"]
    candidate = focus["current_candidate_id"]
    return {"context_id": context["context_id"], "entry_id": hit["entry_id"],
        "content_sha256": hit["content_sha256"], "projection_sha256": hit["projection_sha256"],
        "task_id": value["task_id"], "run_id": value["run_id"],
        "source_snapshot_sha256": value["source_snapshot_sha256"],
        "diagnosis_id": focus["diagnosis_id"], "hypothesis_ids": focus["hypothesis_ids"],
        "candidate_id": candidate,
        "candidate_snapshot_sha256": None if candidate is None else value["candidates"][candidate]["candidate_snapshot_sha256"],
        "premises": [{"premise_id": premise["premise_id"], "status": status,
            "support": [{"evidence_id": "source1", "scope": "original_analysis"}] if status == "supported" else [],
            "contradictions": [{"evidence_id": "source1", "scope": "original_analysis"}] if status == "contradicted" else [],
            "reason": "Synthetic model interpretation of the visible original source; causality remains unknown."}
            for premise, status in zip(hit["projection"]["guidance"]["premises"], statuses, strict=True)]}


def dynamic_fixed(monkeypatch, statuses: tuple[str, str]):
    sent = []
    original = models.FixedResponseModel._intent

    def intent(self, messages, stop, kwargs):
        sent.append([message.model_copy(deep=True) for message in messages])
        prior = {message.name: json.loads(message.content) for message in messages
                 if isinstance(message, ToolMessage) and message.name in {
                     "search_experience", "prepare_experience_context", "review_experience"}
                 and isinstance(message.content, str) and message.content.startswith("{")}
        if self.turn_calls == 2:
            self.responses[2] = response(3, "prepare_experience_context", {"query_id": prior["search_experience"]["query_id"]})
        elif self.turn_calls == 3:
            self.responses[3] = response(4, "review_experience", {"applicability_json": encode(
                proposal_from_result(prior["prepare_experience_context"], statuses))})
        elif self.turn_calls == 4:
            self.responses[4] = response(5, "read_experience_review", {"review_id": prior["review_experience"]["review"]["record_id"]})
        return original(self, messages, stop, kwargs)

    monkeypatch.setattr(models.FixedResponseModel, "_intent", intent)
    return sent


def run_applicability_sdk(tmp_path: Path, monkeypatch, *, statuses=("unknown", "supported")):
    """Reusable full-knowledge SDK fixture for report/export reconstruction."""
    s = scenario(tmp_path / "library", prepared=False)
    sent = dynamic_fixed(monkeypatch, statuses)
    script = [response(1, "search_experience", {"tags": ["ByteWidth"], "text": "容量"}),
              response(2, "record_diagnosis", {"diagnosis_json": encode(diagnosis_submission(s.task))}),
              response(3), response(4), response(5), response(6)]
    work = tmp_path / "run"
    report = run_fixed_agent(s.task, work, "run1", script, FixedTextExecutor("src/main.ets", "value=good\n"),
        knowledge=HostKnowledgeInput(s.retriever, s.approval), applicability_policy=ApplicabilityPolicy(), policy_features=FULL)
    return s, work, report, sent, script


@pytest.mark.parametrize("statuses,expected", [(("supported", "supported"), "supported"),
                                               (("unknown", "supported"), "unknown"),
                                               (("contradicted", "unknown"), "contradicted")])
def test_actual_sdk_sends_complete_review_then_reads_it_without_acceptance(tmp_path: Path, monkeypatch, statuses, expected) -> None:
    s, work, report, sent, _script = run_applicability_sdk(tmp_path, monkeypatch, statuses=statuses)
    facts = report["applicability_facts"]
    assert report["schema_version"] == 9 and report["status"] == "unresolved"
    assert len(facts["contexts"]) == len(facts["reviews"]) == 1
    assert facts["unreferenced_context_ids"] == facts["unreferenced_review_ids"] == []
    assert len(facts["solver_inputs"]) == 6
    review = next(message for message in sent[4] if isinstance(message, ToolMessage) and message.name == "review_experience")
    result = json.loads(review.content)
    assert result["guidance"]["status"] == expected
    assert result["guidance"]["projection"]["guidance"]["procedure"]
    assert result["guidance"]["semantic_authority"] == "model_inference"
    assert result["guidance"]["questions"] == ([{"schema_version": 1, "premise_id": "behavior",
        "question": "What independent behavior must the patch preserve?"}] if statuses[1] == "unknown" else
        ([{"schema_version": 1, "premise_id": "width", "question": "Which consumer determines the element encoding?"}]
         if statuses[0] == "unknown" else []))
    assert report["completion"]["verification"]["accepted"] is False
    assert report["model_calls"] == 6 and report["known_tokens"] == 90 and report["tool_calls"] == 5
    assert s.ledger.executor.requests == []
    assert len(list((work / "applicability-reviews").glob("*.json"))) == 1


def test_completed_review_replays_after_crash_without_republication(tmp_path: Path, monkeypatch) -> None:
    original = ToolActionStore.complete
    crash = True

    def complete(self, identifier, message):
        nonlocal crash
        original(self, identifier, message)
        if message.name == "review_experience" and crash:
            crash = False
            raise RuntimeError("synthetic crash after durable review receipt")

    monkeypatch.setattr(ToolActionStore, "complete", complete)
    with pytest.raises(RuntimeError, match="durable review"):
        run_applicability_sdk(tmp_path, monkeypatch)
    work = tmp_path / "run"
    contexts = tree_bytes(work / "applicability-contexts")
    reviews = tree_bytes(work / "applicability-reviews")
    materials = tree_bytes(work / "applicability-materials")
    # Recover the exact frozen task/knowledge authority, without rebuilding
    # the library or replacing any run original.
    approval = json.loads((work / "knowledge-profile.json").read_bytes())["approval"]
    from dfx_agent.applicability_contracts import TaskKnowledgeBinding

    monkeypatch.setattr(ToolActionStore, "complete", original)
    task = tmp_path / "library/input"
    script = [response(1, "search_experience", {"tags": ["ByteWidth"], "text": "容量"}),
              response(2), response(3), response(4), response(5), response(6)]
    report = run_fixed_agent(task, work, "run1", script, FixedTextExecutor("src/main.ets", "value=good\n"),
        resume=True, knowledge_approval=TaskKnowledgeBinding.model_validate_json(encode(approval)),
        applicability_policy=ApplicabilityPolicy(), policy_features=FULL)
    assert tree_bytes(work / "applicability-contexts") == contexts
    assert tree_bytes(work / "applicability-reviews") == reviews
    assert tree_bytes(work / "applicability-materials") == materials
    assert report["model_calls"] == 6 and report["tool_calls"] == 5
