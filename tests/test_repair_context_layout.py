"""Pure repair context presentation, not source or SDK authentication."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest

from dfx_agent.model_calls import canonical
from dfx_agent.repair_context_layout import build_decision_context, restore_decision_context

BASE = "a" * 64
CANDIDATE = "b" * 64
OLD_CANDIDATE = "c" * 64


def _packet() -> dict[str, Any]:
    owner = {"task_id": "task1", "run_id": "run1", "source_snapshot_sha256": BASE}
    identity = {**owner, "schema_version": 1, "created_at": "2026-10-11T00:00:00+00:00"}
    location = {"schema_version": 1, "snapshot_sha256": BASE, "path": "src/main.ets",
                "start_line": 3, "end_line": 4, "symbol": "consume"}

    def fact(identifier: str, kind: str, producer: str, **fields: Any) -> dict[str, Any]:
        return {**identity, "record_id": identifier, "kind": kind, "producer": producer,
                "artifact_sha256": "d" * 64, "statement": "preserve every original statement",
                "basis_ids": [], "derivation": None, "location": None, "log_line": None,
                "execution_id": None, "evaluated_candidate_id": None, "execution_snapshot_sha256": None,
                "execution_scope": None, **fields}

    hypotheses = [
        {"schema_version": 1, "hypothesis_id": "h1", "root_cause_locations": [location],
         "support_evidence_ids": ["infer1"], "contradicting_evidence_ids": ["log1"],
         "support_status": "unknown", "missing_evidence": ["producer capacity not established"],
         "constraint": {"family": "bounds", "bindings": [{"binding_id": "capacity",
                        "expression": "allocated bytes", "evidence_ids": ["source1"]}]},
         "causal_steps": [{"step_id": "step1", "relation": "may consume more than allocated",
                          "authority": "model_inference", "evidence_ids": ["infer1"]}],
         "obligation_ids": ["runtime1", "behavior1"],
         "distinguishing_check": {"question": "Which producer supplies this buffer?",
                                  "observation_routes": {"producer A": "h1", "producer B": "h2"},
                                  "unknown_route": "retain_unknown"}},
        {"schema_version": 1, "hypothesis_id": "h2", "root_cause_locations": [],
         "support_evidence_ids": ["source1"], "contradicting_evidence_ids": [],
         "support_status": "unknown", "missing_evidence": ["caller lifecycle not established"],
         "constraint": None, "causal_steps": [], "obligation_ids": ["behavior1"],
         "distinguishing_check": None},
    ]
    return {
        "schema_version": 1, "profile": "registered_working_context_v1", **owner,
        "focus": {"diagnosis_id": "diagnosis1", "hypothesis_ids": [], "current_candidate_id": None,
                  "decision_id": None, "feedback_decision_ids": []},
        "focus_authority": "explicit_host_input", "focus_recency": "not_authenticated",
        "root_cause_confirmation": "unknown", "application_verification": "not_run",
        "tool_action_authentication": "not_performed",
        "diagnoses": {"diagnosis1": {**identity, "record_id": "diagnosis1",
                                    "exception_locations": [location], "hypotheses": hypotheses,
                                    "selected_hypothesis_id": "h1", "unknowns": ["encoding and order unknown"],
                                    "feedback_candidate_id": None}},
        "evidence": {
            "log1": fact("log1", "log_observation", "input", source_snapshot_sha256=None,
                         log_line=2, statement="RangeError\nconsumer frame"),
            "source1": fact("source1", "source_fact", "source_reader", location=location),
            "infer1": fact("infer1", "model_inference", "model", basis_ids=["source1"],
                           derivation="source supports a possibility, not a confirmed cause"),
            "failure1": fact("failure1", "local_execution", "execution_backend", execution_id="check-old",
                             evaluated_candidate_id=OLD_CANDIDATE, execution_snapshot_sha256="e" * 64,
                             execution_scope="synthetic"),
        },
        "obligations": {
            "runtime1": {**identity, "record_id": "runtime1", "kind": "runtime_condition",
                         "statement": "capacity covers the consumer width", "authority": "model_inference",
                         "source_evidence_ids": ["infer1"], "status": "unknown", "bindings": [],
                         "hypothesis_ids": ["h1"], "checkability": "unknown", "scope": "consumer operation"},
            "behavior1": {**identity, "record_id": "behavior1", "kind": "business_behavior",
                          "statement": "preserve the entire checksum and its order", "authority": "existing_behavior",
                          "source_evidence_ids": ["source1"], "status": "unknown", "bindings": [],
                          "hypothesis_ids": ["h1", "h2"], "checkability": "manual", "scope": "business output"},
        },
        "candidates": {}, "checks": {}, "feedback": {},
        "decisions": {"decision1": {**identity, "record_id": "decision1", "next_action": "attempt_with_unknown",
                                    "request": {"diagnosis_id": "diagnosis1"}}},
        "conditions": {"condition1": {"result_id": "condition1", "result": {
            "status": "unknown", "request": {"scope": {**owner, "candidate_id": OLD_CANDIDATE,
                                                         "candidate_snapshot_sha256": "e" * 64}}}}},
        "historical_relevance": {"old": {"authority": "host_reference_overlap", "semantic_relevance": "unknown"}},
        "sources": [{"reference": "workspace/diagnoses/diagnosis1.json", "bytes": 17, "sha256": "f" * 64}],
        "interpretation": "Reference overlap does not prove causality.",
    }


def _current(value: dict[str, Any], state: str, status: str | None) -> None:
    value["focus"]["current_candidate_id"] = CANDIDATE
    value["candidates"][CANDIDATE] = {
        "schema_version": 1, "record_id": "candidate1", "task_id": "task1", "run_id": "run1",
        "source_snapshot_sha256": BASE, "created_at": "2026-10-11T00:00:01+00:00",
        "candidate_id": CANDIDATE, "ordinal": 2, "parent_snapshot_sha256": BASE,
        "candidate_snapshot_sha256": "9" * 64, "diff_sha256": "8" * 64,
        "diagnosis_id": "diagnosis1", "decision_id": None, "direction_id": None,
        "changed_paths": ["src/main.ets"], "restores": ["runtime1"], "preserves": ["behavior1"],
    }
    result = None if status is None else {
        "candidate_id": CANDIDATE, "check_id": "offline", "idempotency_key": "check-current",
        "protocol_sha256": "7" * 64, "status": status,
        "detail": "actual recorded check detail", "level": "offline_synthetic",
    }
    value["checks"][CANDIDATE] = {"state": state, "candidate_id": CANDIDATE,
                                   "candidate_snapshot_sha256": "9" * 64, "result": result}


def test_layout_separates_producers_and_preserves_alternatives_unknowns_and_obligations() -> None:
    value = _packet()
    original = deepcopy(value)
    layout = build_decision_context(value, latest_failure=None, draft_unregistered=False)
    assert set(layout["evidence_by_producer"]["input"]) == {"log1"}
    assert set(layout["evidence_by_producer"]["source_reader"]) == {"source1"}
    assert set(layout["evidence_by_producer"]["model"]) == {"infer1"}
    assert set(layout["evidence_by_producer"]["execution_backend"]) == {"failure1"}
    assert layout["evidence_by_producer"]["model"]["infer1"] == value["evidence"]["infer1"]
    assert layout["diagnoses"] == value["diagnoses"]
    assert layout["obligations_by_kind"]["business_behavior"]["behavior1"] == value["obligations"]["behavior1"]
    assert layout["obligations_by_kind"]["runtime_condition"]["runtime1"] == value["obligations"]["runtime1"]
    assert layout["packet_metadata"]["root_cause_confirmation"] == "unknown"
    assert canonical(restore_decision_context(layout)) == canonical(value)
    layout["diagnoses"]["diagnosis1"]["unknowns"].append("output mutation")
    assert value == original


@pytest.mark.parametrize(("state", "status", "phase", "question"), [
    ("not_requested", None, "verification_ready", "review_check_scope"),
    ("pending", None, "verification_pending", "resolve_pending_observation"),
    ("completed", "fail", "revision", "interpret_failed_check"),
    ("completed", "pass", "delivery_review", "review_delivery"),
    ("completed", "inconclusive", "verification_unknown", "identify_missing_verification"),
])
def test_board_uses_recorded_candidate_checks_without_granting_success(
        state: str, status: str | None, phase: str, question: str) -> None:
    value = _packet()
    _current(value, state, status)
    layout = build_decision_context(value, latest_failure=None, draft_unregistered=False)
    board = layout["decision_board"]
    assert board["phase"] == phase and board["next_question_kind"] == question
    assert board["check_status"] == status
    assert board["advisory_only"] is True and board["execution_permission"] == "not_granted"
    assert board["repair_acceptance"] == "not_assessed"
    assert layout["checks"] == value["checks"]
    assert canonical(restore_decision_context(layout)) == canonical(value)


def test_empty_packet_and_unselected_diagnosis_remain_exploratory_or_competing() -> None:
    value = _packet()
    value["diagnoses"]["diagnosis1"]["selected_hypothesis_id"] = None
    board = build_decision_context(value, latest_failure=None, draft_unregistered=False)["decision_board"]
    assert board["phase"] == "diagnosis" and board["next_question_kind"] == "compare_hypotheses"
    value["focus"]["diagnosis_id"] = None
    for field in ("diagnoses", "evidence", "obligations", "decisions", "conditions", "historical_relevance"):
        value[field] = {}
    layout = build_decision_context(value, latest_failure=None, draft_unregistered=False)
    assert layout["decision_board"]["phase"] == "exploration"
    assert layout["readback"]["analysis"] == []
    assert canonical(restore_decision_context(layout)) == canonical(value)


@pytest.mark.parametrize(("state", "status", "phase"), [
    ("pending", None, "verification_pending"),
    ("completed", "fail", "revision"),
    ("completed", "inconclusive", "verification_unknown"),
    ("completed", "pass", "delivery_review"),
])
def test_actual_candidate_observations_take_priority_over_an_unselected_root(
        state: str, status: str | None, phase: str) -> None:
    value = _packet()
    value["diagnoses"]["diagnosis1"]["selected_hypothesis_id"] = None
    _current(value, state, status)
    layout = build_decision_context(value, latest_failure=None, draft_unregistered=False)
    assert layout["decision_board"]["phase"] == phase
    assert layout["diagnoses"]["diagnosis1"]["selected_hypothesis_id"] is None
    assert layout["decision_board"]["repair_acceptance"] == "not_assessed"


def test_pending_observation_remains_pending_when_the_draft_has_changed() -> None:
    value = _packet()
    _current(value, "pending", None)
    layout = build_decision_context(value, latest_failure=None, draft_unregistered=True)
    assert layout["decision_board"]["phase"] == "verification_pending"
    assert layout["decision_board"]["draft_unregistered"] is True


def test_unregistered_draft_prevents_pass_from_selecting_delivery_review() -> None:
    value = _packet()
    _current(value, "completed", "pass")
    layout = build_decision_context(value, latest_failure=None, draft_unregistered=True)
    assert layout["decision_board"]["phase"] == "draft_review"
    assert layout["decision_board"]["next_question_kind"] == "review_draft_before_freezing"
    assert layout["checks"][CANDIDATE]["result"]["status"] == "pass"
    with pytest.raises(TypeError, match="draft_unregistered must be a boolean"):
        build_decision_context(value, latest_failure=None, draft_unregistered=1)  # type: ignore[arg-type]


def test_historical_failure_keeps_original_version_and_unknown_causal_relevance() -> None:
    value = _packet()
    failure = {"candidate_id": OLD_CANDIDATE, "diagnosis_id": "diagnosis-old",
               "check_result": {"candidate_id": OLD_CANDIDATE, "status": "fail", "level": "offline_synthetic",
                                "detail": "no crash, but the checksum is wrong"},
               "is_current_candidate_observation": False, "causal_relevance": "unknown"}
    original_failure = deepcopy(failure)
    layout = build_decision_context(value, latest_failure=failure, draft_unregistered=False)
    assert layout["decision_board"]["phase"] == "revision"
    assert layout["revision"]["latest_failure"] == failure
    assert layout["evidence_by_producer"]["execution_backend"]["failure1"]["evaluated_candidate_id"] == OLD_CANDIDATE
    assert value["candidates"] == {} and value["checks"] == {}
    layout["revision"]["latest_failure"]["check_result"]["status"] = "inconclusive"
    assert failure == original_failure


def test_records_keep_exact_original_owner_fields_absence_and_different_snapshots() -> None:
    # Pure preservation fixture; changing these fields does not authenticate
    # them as a valid admitted packet or turn them into current evidence.
    value = _packet()
    del value["evidence"]["infer1"]["task_id"]
    value["evidence"]["source1"]["source_snapshot_sha256"] = "1" * 64
    value["evidence"]["source1"]["location"]["snapshot_sha256"] = "1" * 64
    layout = build_decision_context(value, latest_failure=None, draft_unregistered=False)
    source = layout["evidence_by_producer"]["source_reader"]["source1"]
    assert source == value["evidence"]["source1"] and source["source_snapshot_sha256"] != layout["owner"]["source_snapshot_sha256"]
    assert "task_id" not in layout["evidence_by_producer"]["model"]["infer1"]
    assert layout["evidence_by_producer"]["input"]["log1"]["source_snapshot_sha256"] is None
    assert "source1" not in layout["readback"]["observations"]
    assert canonical(restore_decision_context(layout)) == canonical(value)


def test_relationships_are_exact_original_references_and_readback_uses_existing_tools() -> None:
    value = _packet()
    _current(value, "not_requested", None)
    value["evidence"]["source1"]["location"]["end_line"] = 500
    value["evidence"]["log1"]["statement"] = "\n".join("log page" for _ in range(240))
    layout = build_decision_context(value, latest_failure=None, draft_unregistered=False)
    relations = layout["relationships"]
    assert relations["hypotheses"]["diagnosis1"]["h1"] == {
        "support_evidence_ids": ["infer1"], "contradicting_evidence_ids": ["log1"],
        "obligation_ids": ["runtime1", "behavior1"],
    }
    assert relations["obligations"]["behavior1"] == ["source1"]
    assert relations["candidates"][CANDIDATE] == {"diagnosis_id": "diagnosis1", "restores": ["runtime1"],
                                                "preserves": ["behavior1"], "check_reference": CANDIDATE}
    readback = layout["readback"]
    assert readback["analysis"] == [{"tool": "inspect_analysis", "args": {"diagnosis_id": "diagnosis1"}}]
    assert readback["exploration"]["tool"] == "search_source" and readback["grants_visibility"] is False
    assert readback["observations"]["source1"]["args"] == {"path": "src/main.ets", "start_line": 3,
                                                           "end_line": 202, "draft": False}
    assert readback["observations"]["source1"]["original_end_line"] == 500
    assert readback["observations"]["log1"]["args"] == {"start_line": 2, "end_line": 201}
    assert readback["observations"]["log1"]["original_end_line"] == 241


def test_projection_metadata_and_all_semantic_slots_restore_exactly() -> None:
    value = _packet()
    value.update(schema_version=2, profile="repair_decision_context_v2", audit_packet_sha256="2" * 64,
                 audit_sources={"source_count": 1, "total_reference_bytes": 17, "manifest_sha256": "3" * 64})
    del value["sources"]
    layout = build_decision_context(value, latest_failure=None, draft_unregistered=False)
    assert layout["revision"]["conditions"] == value["conditions"]
    assert layout["revision"]["decisions"] == value["decisions"]
    assert layout["revision"]["historical_relevance"] == value["historical_relevance"]
    assert canonical(restore_decision_context(layout)) == canonical(value)
    layout["evidence_by_producer"]["model"]["source1"] = deepcopy(value["evidence"]["source1"])
    with pytest.raises(ValueError, match="duplicate repair decision record"):
        restore_decision_context(layout)
