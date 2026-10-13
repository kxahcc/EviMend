"""Pure repair context presentation, not source or SDK authentication."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest

from dfx_agent.model_calls import canonical
from dfx_agent.repair_context_layout import (
    build_decision_context,
    restore_decision_context,
)

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


def _decision(value: dict[str, Any], action: str) -> dict[str, Any]:
    """Presentation fixture for an already registered, focused decision."""
    equivalent = action == "direct_candidate"
    directions = [{"direction_id": f"direction{number}", "hypothesis_ids": [f"h{number}"],
        "support_evidence_ids": ["source1"], "obligation_ids": ["runtime1", "behavior1"],
        "repair_key": "same-repair" if equivalent else f"repair{number}",
        "acceptance_key": "same-acceptance" if equivalent else f"acceptance{number}"}
        for number in (1, 2)]
    check = None if equivalent else {"check_id": "producer-check", "kind": "existing_evidence",
        "capability": "read_source", "observation_routes": {"producer A": "direction1", "producer B": "direction2"},
        "unknown_route": "retain_unknown"}
    owner = {key: value[key] for key in ("task_id", "run_id", "source_snapshot_sha256")}
    record = {**owner, "schema_version": 1, "record_id": "decision-current", "context_sha256": "6" * 64,
        "created_at": "2026-10-13T00:00:00+00:00", "interpretation_authority": "model_inference",
        "request": {"task_id": owner["task_id"], "run_id": owner["run_id"],
            "diagnosis_id": value["focus"]["diagnosis_id"], "candidate_id": value["focus"]["current_candidate_id"],
            "plan": {"source_snapshot_sha256": BASE, "directions": directions, "check": check,
                     "selected_direction_id": "direction1" if equivalent else None,
                     "available_capabilities": ["read_source"], "remaining_probe_budget": 0}},
        "decision": {"source_snapshot_sha256": BASE, "action": action, "reason": "original recorded recommendation",
                     "selected_direction_id": "direction1" if equivalent else None,
                     "check_id": None if check is None else check["check_id"]},
        "next_action": action, "feedback": None, "condition_review": None,
        "advisory_obligation_ids": [], "hard_rejected_obligation_ids": []}
    value["decisions"][record["record_id"]] = record
    value["focus"]["decision_id"] = record["record_id"]
    return record


def _guided(value: dict[str, Any], *, latest_failure: dict[str, Any] | None = None,
            draft_unregistered: bool = False) -> dict[str, Any]:
    return build_decision_context(value, latest_failure=latest_failure, draft_unregistered=draft_unregistered,
                                  decision_guidance=True)


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
    assert readback["candidate_sources"][0]["read_source_args"] == {"candidate_id": CANDIDATE}


def test_historical_failed_candidate_has_readback_without_becoming_current() -> None:
    value = _packet()
    failure = {"candidate_id": OLD_CANDIDATE, "diagnosis_id": "old-diagnosis",
               "is_current_candidate_observation": False, "causal_relevance": "unknown"}
    layout = build_decision_context(value, latest_failure=failure, draft_unregistered=False)
    assert layout["readback"]["candidate_sources"][0]["read_source_args"] == {"candidate_id": OLD_CANDIDATE}
    assert layout["focus"]["current_candidate_id"] is None and layout["candidates"] == {}
    assert canonical(restore_decision_context(layout)) == canonical(value)


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


def test_current_read_decision_uses_original_question_even_after_a_root_was_selected() -> None:
    value = _packet()
    record = _decision(value, "read_existing")
    original = deepcopy(value)
    layout = _guided(value)
    board = layout["decision_board"]
    assert board["phase"] == "evidence_review" and board["next_question_kind"] == "read_existing_for_repair_choice"
    assert board["question"] == "Which producer supplies this buffer?"
    assert board["selected_hypothesis_id"] == "h1" and board["decision_applied"] is True
    assert board["decision_id"] == record["record_id"] and board["decision_check_id"] == "producer-check"
    item = layout["decision_frontier"]["unresolved_questions"][0]
    assert item["check"] == record["request"]["plan"]["check"]
    assert item["hypothesis_observation_routes"] == {"producer A": "h1", "producer B": "h2"}
    assert item["status"] == "unknown"
    assert board["execution_permission"] == "not_granted" and board["repair_acceptance"] == "not_assessed"
    assert layout["layout"] == "repair_decision_frontier_v1"
    assert canonical(restore_decision_context(layout)) == canonical(value)
    item["check"]["observation_routes"]["producer A"] = "changed"
    assert value == original


def test_equivalent_direct_decision_does_not_require_a_selected_root_or_extra_questions() -> None:
    value = _packet()
    value["diagnoses"]["diagnosis1"]["selected_hypothesis_id"] = None
    _decision(value, "direct_candidate")
    layout = _guided(value)
    board = layout["decision_board"]
    assert board["phase"] == "repair" and board["next_question_kind"] == "choose_behavior_preserving_patch"
    assert board["selected_hypothesis_id"] is None and board["decision_applied"] is True
    assert board["decision_selected_direction_id"] == "direction1"
    frontier = layout["decision_frontier"]
    assert frontier["requires_extra_check"] is False and frontier["unresolved_questions"]
    assert value["diagnoses"]["diagnosis1"]["selected_hypothesis_id"] is None


def test_unknown_attempt_retains_missing_questions_without_turning_them_into_prerequisites() -> None:
    value = _packet()
    _decision(value, "attempt_with_unknown")
    layout = _guided(value)
    assert layout["decision_board"]["next_question_kind"] == "attempt_with_known_unknowns"
    frontier = layout["decision_frontier"]
    missing = [item for item in frontier["unresolved_questions"] if item["kind"] == "missing_evidence"]
    assert [(item["hypothesis_id"], item["question"]) for item in missing] == [
        ("h1", "producer capacity not established"), ("h2", "caller lifecycle not established")]
    assert all(item["status"] == "unknown" and item["location"] == "not_specified" for item in missing)
    assert frontier["requires_extra_check"] is False and frontier["execution_permission"] == "not_granted"
    assert all(hypothesis["support_status"] == "unknown" for hypothesis in layout["diagnoses"]["diagnosis1"]["hypotheses"])
    assert frontier["registered_counterevidence"][0] == {
        "diagnosis_id": "diagnosis1", "hypothesis_id": "h1", "obligation_ids": ["runtime1", "behavior1"],
        "evidence_id": "log1", "relation_authority": "original_model_hypothesis_reference",
        "causal_confirmation": "unknown", "readback": layout["readback"]["observations"]["log1"]}


@pytest.mark.parametrize(("state", "status", "draft", "phase"), [
    ("pending", None, False, "verification_pending"),
    ("pending", None, True, "verification_pending"),
    ("completed", "fail", False, "revision"),
    ("completed", "pass", False, "delivery_review"),
    ("completed", "inconclusive", False, "verification_unknown"),
    ("completed", "pass", True, "draft_review"),
])
def test_actual_observations_and_unfrozen_draft_override_matching_advice(
        state: str, status: str | None, draft: bool, phase: str) -> None:
    value = _packet()
    _current(value, state, status)
    _decision(value, "direct_candidate")
    layout = _guided(value, draft_unregistered=draft)
    assert layout["decision_board"]["phase"] == phase
    assert layout["decision_board"]["decision_applied"] is False
    assert layout["decision_board"]["decision_id"] == "decision-current"
    assert canonical(restore_decision_context(layout)) == canonical(value)


@pytest.mark.parametrize("mismatch", ["record_owner", "request_owner", "diagnosis", "candidate", "snapshot", "decision_snapshot"])
def test_different_scope_decision_cannot_direct_the_current_board_or_frontier(mismatch: str) -> None:
    value = _packet()
    record = _decision(value, "read_existing")
    if mismatch == "record_owner":
        record["run_id"] = "old-run"
    elif mismatch == "request_owner":
        record["request"]["task_id"] = "another-task"
    elif mismatch == "diagnosis":
        record["request"]["diagnosis_id"] = "old-diagnosis"
    elif mismatch == "candidate":
        record["request"]["candidate_id"] = OLD_CANDIDATE
    elif mismatch == "snapshot":
        record["request"]["plan"]["source_snapshot_sha256"] = "f" * 64
    else:
        record["decision"]["source_snapshot_sha256"] = "f" * 64
    layout = _guided(value)
    assert layout["decision_board"]["phase"] == "repair"
    assert layout["decision_board"]["decision_id"] is None and layout["decision_board"]["decision_applied"] is False
    assert layout["decision_frontier"]["decision_id"] is None
    assert all(item["kind"] != "recorded_decision_check" for item in layout["decision_frontier"]["unresolved_questions"])
    assert layout["revision"]["decisions"] == value["decisions"]


def test_new_candidate_does_not_inherit_a_pre_candidate_read_decision() -> None:
    value = _packet()
    _decision(value, "read_existing")
    _current(value, "not_requested", None)
    layout = _guided(value)
    assert layout["decision_board"]["phase"] == "verification_ready"
    assert layout["decision_board"]["decision_id"] is None


def test_new_diagnosis_does_not_inherit_a_historical_decision() -> None:
    value = _packet()
    _decision(value, "read_existing")
    value["diagnoses"]["diagnosis2"] = deepcopy(value["diagnoses"]["diagnosis1"])
    value["diagnoses"]["diagnosis2"]["record_id"] = "diagnosis2"
    value["focus"]["diagnosis_id"] = "diagnosis2"
    layout = _guided(value)
    assert layout["decision_board"]["phase"] == "repair" and layout["decision_board"]["decision_id"] is None
    assert all(item["diagnosis_id"] == "diagnosis2" for item in layout["decision_frontier"]["unresolved_questions"])


def test_historical_actual_failure_precedes_a_new_direct_recommendation() -> None:
    value = _packet()
    value["diagnoses"]["diagnosis1"]["selected_hypothesis_id"] = None
    _decision(value, "direct_candidate")
    failure = {"candidate_id": OLD_CANDIDATE, "diagnosis_id": "old-diagnosis",
               "check_result": {"candidate_id": OLD_CANDIDATE, "status": "fail"}, "causal_relevance": "unknown"}
    layout = _guided(value, latest_failure=failure)
    assert layout["decision_board"]["phase"] == "revision"
    assert layout["decision_board"]["next_question_kind"] == "reconsider_root_with_historical_failure"
    assert layout["decision_board"]["decision_applied"] is False
    assert layout["revision"]["latest_failure"] == failure


def test_frontier_bounds_derived_items_and_keeps_complete_originals_readable() -> None:
    value = _packet()
    hypothesis = value["diagnoses"]["diagnosis1"]["hypotheses"][0]
    hypothesis["missing_evidence"] = [f"original missing question {number}" for number in range(10)]
    hypothesis["contradicting_evidence_ids"] = [f"counter{number}" for number in range(10)]
    for identifier in hypothesis["contradicting_evidence_ids"]:
        value["evidence"][identifier] = {**deepcopy(value["evidence"]["log1"]), "record_id": identifier}
    original = deepcopy(value)
    layout = _guided(value)
    frontier = layout["decision_frontier"]
    assert len(frontier["unresolved_questions"]) == 6 and frontier["omitted_question_count"] == 6
    assert len(frontier["registered_counterevidence"]) == 8 and frontier["omitted_counterevidence_count"] == 2
    assert frontier["readback"] == {"tool": "inspect_analysis", "args": {"diagnosis_id": "diagnosis1"}}
    assert layout["diagnoses"] == value["diagnoses"] and canonical(restore_decision_context(layout)) == canonical(value)
    frontier["unresolved_questions"][0]["question"]["observation_routes"]["producer A"] = "changed"
    assert value == original


def test_legacy_layout_keeps_its_exact_board_and_never_backfills_new_guidance() -> None:
    value = _packet()
    _decision(value, "read_existing")
    layout = build_decision_context(value, latest_failure=None, draft_unregistered=False)
    assert layout["layout"] == "repair_decision_slots_v1" and "decision_frontier" not in layout
    assert layout["decision_board"] == {
        "authority": "host_record_projection", "advisory_only": True,
        "execution_permission": "not_granted", "repair_acceptance": "not_assessed",
        "phase": "repair", "next_question_kind": "choose_behavior_preserving_patch",
        "question": "Which change restores the runtime condition and preserves the business obligations?",
        "diagnosis_id": "diagnosis1", "selected_hypothesis_id": "h1", "current_candidate_id": None,
        "check_state": None, "check_status": None, "draft_unregistered": False,
        "historical_failure_candidate_id": None}
    assert canonical(restore_decision_context(layout)) == canonical(value)
    value["diagnoses"]["diagnosis1"]["selected_hypothesis_id"] = None
    failure = {"candidate_id": OLD_CANDIDATE}
    legacy = build_decision_context(value, latest_failure=failure, draft_unregistered=False, decision_guidance=False)
    assert legacy["decision_board"]["phase"] == "diagnosis"
    with pytest.raises(TypeError, match="decision_guidance must be a boolean"):
        build_decision_context(value, latest_failure=None, draft_unregistered=False, decision_guidance=1)  # type: ignore[arg-type]
