"""Arrange an already verified packet for the next repair decision.

This is a pure presentation boundary. It neither authenticates input nor grants
tool authority. The caller must retain the original packet and certify exactly
what it sends; a readback hint does not make unseen material visible.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

_OWNER_FIELDS = ("task_id", "run_id", "source_snapshot_sha256")
_RECORD_FIELDS = frozenset({
    "focus", "diagnoses", "evidence", "obligations", "candidates", "checks",
    "feedback", "decisions", "conditions", "historical_relevance",
})
_FRONTIER_QUESTION_LIMIT = 6
_FRONTIER_COUNTEREVIDENCE_LIMIT = 8


def _current_decision(value: dict[str, Any]) -> dict[str, Any] | None:
    """Use only the focused proposal for this exact diagnosis/candidate scope."""
    focus = value["focus"]
    identifier = focus.get("decision_id")
    record = value["decisions"].get(identifier)
    if record is None or record.get("record_id") != identifier:
        return None
    if any(record.get(key) != value[key] for key in _OWNER_FIELDS):
        return None
    request = record.get("request", {})
    if (request.get("task_id") != value["task_id"] or request.get("run_id") != value["run_id"] or
            request.get("diagnosis_id") != focus["diagnosis_id"] or
            request.get("candidate_id") != focus["current_candidate_id"] or
            request.get("plan", {}).get("source_snapshot_sha256") != value["source_snapshot_sha256"] or
            record.get("decision", {}).get("source_snapshot_sha256") != value["source_snapshot_sha256"]):
        return None
    return record


def _decision_question(value: dict[str, Any], record: dict[str, Any] | None) -> dict[str, Any] | None:
    """Locate the original diagnostic question behind a recorded check route."""
    if record is None:
        return None
    plan = record["request"]["plan"]
    check = plan.get("check")
    diagnosis = value["diagnoses"].get(value["focus"]["diagnosis_id"])
    if check is None or diagnosis is None:
        return None
    routes = check["observation_routes"]
    directions = {item["direction_id"]: item for item in plan["directions"]}
    for hypothesis in diagnosis["hypotheses"]:
        question = hypothesis.get("distinguishing_check")
        if question is not None and set(question["observation_routes"]) == set(routes) and all(
                direction_id in directions and question["observation_routes"][outcome] in
                directions[direction_id]["hypothesis_ids"] for outcome, direction_id in routes.items()):
            return {"source_hypothesis_id": hypothesis["hypothesis_id"],
                    "question": deepcopy(question), "check": deepcopy(check)}
    return None


def _board(value: dict[str, Any], latest_failure: dict[str, Any] | None,
           draft_unregistered: bool, *, decision_guidance: bool) -> dict[str, Any]:
    focus = value["focus"]
    diagnosis = value["diagnoses"].get(focus["diagnosis_id"])
    selected = None if diagnosis is None else diagnosis.get("selected_hypothesis_id")
    candidate_id = focus["current_candidate_id"]
    check = value["checks"].get(candidate_id)
    check_state = None if check is None else check["state"]
    result = None if check is None else check["result"]
    status = None if result is None else result["status"]
    decision = _current_decision(value) if decision_guidance else None
    question_source = _decision_question(value, decision)
    decision_applied = False

    if check_state == "pending":
        phase, question = "verification_pending", "resolve_pending_observation"
        prompt = "What is known about the pending check? Do not infer a result or reexecute unknown work."
    elif draft_unregistered:
        phase, question = "draft_review", "review_draft_before_freezing"
        prompt = "Does the unfrozen draft address the fault while preserving the required behavior?"
    elif status == "fail":
        phase, question = "revision", "interpret_failed_check"
        prompt = "What did this candidate actually fail, and which repair assumptions need reconsideration?"
    elif status == "pass":
        phase, question = "delivery_review", "review_delivery"
        prompt = "Do the recorded check scope, localization and delivery artifacts meet the host requirements?"
    elif status == "inconclusive":
        phase, question = "verification_unknown", "identify_missing_verification"
        prompt = "What missing or inconclusive observation prevents a reliable validation conclusion?"
    elif decision_guidance and candidate_id is None and latest_failure is not None:
        phase, question = "revision", "reconsider_root_with_historical_failure"
        prompt = "Does the new explanation account for the historical failure in its original candidate scope?"
    elif diagnosis is None:
        phase, question = "exploration", "establish_fault_and_follow_values"
        prompt = "What do the fault log and related source observations show, and what remains unexplained?"
    elif decision is not None:
        action = decision["next_action"]
        phase, question, prompt = {
            "direct_candidate": ("repair", "choose_behavior_preserving_patch",
                                 "Which change restores the runtime condition and preserves the business obligations?"),
            "read_existing": ("evidence_review", "read_existing_for_repair_choice",
                              "Which existing observation distinguishes the recorded repair choices?"),
            "attempt_with_unknown": ("repair", "attempt_with_known_unknowns",
                                     "Which proposed repair can be attempted while retaining the recorded unknowns?"),
            "unresolved": ("unresolved", "identify_unresolved_repair",
                           "Which recorded missing evidence or repair choice prevents a supported attempt?"),
            "reconsider_repair": ("revision", "reconsider_affected_repair_direction",
                                  "Which recorded direction needs reconsideration in its original feedback scope?"),
            "retain_uncertainty": ("diagnosis", "review_recorded_uncertainty",
                                   "Which recorded uncertainty still affects the repair choice?"),
            "ignore_stale": ("diagnosis", "review_current_evidence",
                             "Which current evidence supports the repair after retaining the stale observation as history?"),
            "run_probe": ("evidence_review", "review_diagnostic_proposal",
                          "What does the recorded diagnostic proposal require within the host's actual capabilities?"),
        }[action]
        if action == "read_existing" and question_source is not None:
            prompt = question_source["question"]["question"]
        decision_applied = True
    elif selected is None:
        phase, question = "diagnosis", "compare_hypotheses"
        prompt = "Which evidence distinguishes the competing roots without turning unknowns into facts?"
    elif candidate_id is None and latest_failure is not None:
        phase, question = "revision", "reconsider_root_with_historical_failure"
        prompt = "Does the new explanation account for the historical failure in its original candidate scope?"
    elif candidate_id is None:
        phase, question = "repair", "choose_behavior_preserving_patch"
        prompt = "Which change restores the runtime condition and preserves the business obligations?"
    else:
        phase, question = "verification_ready", "review_check_scope"
        prompt = "Which host-fixed checks are required for this registered candidate, and what remains unobserved?"
    board = {
        "authority": "host_record_projection", "advisory_only": True,
        "execution_permission": "not_granted", "repair_acceptance": "not_assessed",
        "phase": phase, "next_question_kind": question, "question": prompt,
        "diagnosis_id": focus["diagnosis_id"], "selected_hypothesis_id": selected,
        "current_candidate_id": candidate_id, "check_state": check_state,
        "check_status": status, "draft_unregistered": draft_unregistered,
        "historical_failure_candidate_id": None if latest_failure is None else latest_failure["candidate_id"],
    }
    if not decision_guidance:
        return board
    return {**board,
        "decision_id": None if decision is None else decision["record_id"],
        "decision_next_action": None if decision is None else decision["next_action"],
        "decision_check_id": None if decision is None else decision["decision"].get("check_id"),
        "decision_selected_direction_id": None if decision is None else decision["decision"].get("selected_direction_id"),
        "decision_applied": decision_applied,
    }


def _decision_frontier(value: dict[str, Any], readback: dict[str, Any]) -> dict[str, Any]:
    """Expose bounded original questions and counterevidence, without choosing an action."""
    diagnosis_id = value["focus"]["diagnosis_id"]
    diagnosis = value["diagnoses"].get(diagnosis_id)
    record = _current_decision(value)
    decision_question = _decision_question(value, record)
    questions: list[dict[str, Any]] = []
    counterevidence: list[dict[str, Any]] = []
    if decision_question is not None:
        questions.append({"kind": "recorded_decision_check", "diagnosis_id": diagnosis_id,
            "decision_id": record["record_id"] if record is not None else None,
            "source_hypothesis_id": decision_question["source_hypothesis_id"],
            "question": decision_question["question"]["question"],
            "hypothesis_observation_routes": decision_question["question"]["observation_routes"],
            "unknown_route": decision_question["question"]["unknown_route"],
            "check": decision_question["check"], "status": "unknown"})
    if diagnosis is not None:
        for hypothesis in diagnosis["hypotheses"]:
            identifier = hypothesis["hypothesis_id"]
            references = {"diagnosis_id": diagnosis_id, "hypothesis_id": identifier,
                          "obligation_ids": deepcopy(hypothesis["obligation_ids"])}
            original_question = hypothesis.get("distinguishing_check")
            if original_question is not None and (decision_question is None or
                                                  original_question != decision_question["question"]):
                questions.append({**references, "kind": "hypothesis_distinguishing_question",
                                  "question": deepcopy(original_question), "status": "unknown"})
            questions.extend({**deepcopy(references), "kind": "missing_evidence",
                              "question": text, "status": "unknown", "location": "not_specified"}
                             for text in hypothesis["missing_evidence"])
            for evidence_id in hypothesis["contradicting_evidence_ids"]:
                counterevidence.append({**deepcopy(references), "evidence_id": evidence_id,
                    "relation_authority": "original_model_hypothesis_reference", "causal_confirmation": "unknown",
                    "readback": deepcopy(readback["observations"].get(evidence_id))})
    return {
        "authority": "original_record_projection", "advisory_only": True,
        "requires_extra_check": False, "execution_permission": "not_granted",
        "diagnosis_id": diagnosis_id, "decision_id": None if record is None else record["record_id"],
        "unresolved_questions": questions[:_FRONTIER_QUESTION_LIMIT],
        "registered_counterevidence": counterevidence[:_FRONTIER_COUNTEREVIDENCE_LIMIT],
        "omitted_question_count": max(0, len(questions) - _FRONTIER_QUESTION_LIMIT),
        "omitted_counterevidence_count": max(0, len(counterevidence) - _FRONTIER_COUNTEREVIDENCE_LIMIT),
        "readback": None if diagnosis is None else {"tool": "inspect_analysis", "args": {"diagnosis_id": diagnosis_id}},
        "scope_note": "Missing evidence and contradiction links are original model proposals. They do not establish facts or require extra checks before a direct or uncertain attempt.",
    }


def _relationships(value: dict[str, Any]) -> dict[str, Any]:
    """Expose existing references, without inferring edges or causal truth."""
    return {
        "authority": "original_record_references", "causal_confirmation": "unknown",
        "hypotheses": {
            identifier: {hypothesis["hypothesis_id"]: {
                "support_evidence_ids": deepcopy(hypothesis["support_evidence_ids"]),
                "contradicting_evidence_ids": deepcopy(hypothesis["contradicting_evidence_ids"]),
                "obligation_ids": deepcopy(hypothesis["obligation_ids"]),
            } for hypothesis in diagnosis["hypotheses"]}
            for identifier, diagnosis in sorted(value["diagnoses"].items())
        },
        "obligations": {identifier: deepcopy(obligation["source_evidence_ids"])
                        for identifier, obligation in sorted(value["obligations"].items())},
        "candidates": {identifier: {
            "diagnosis_id": candidate["diagnosis_id"],
            "restores": deepcopy(candidate["restores"]), "preserves": deepcopy(candidate["preserves"]),
            "check_reference": identifier if identifier in value["checks"] else None,
        } for identifier, candidate in sorted(value["candidates"].items())},
    }


def _readback(value: dict[str, Any], latest_failure: dict[str, Any] | None) -> dict[str, Any]:
    observations: dict[str, Any] = {}
    for identifier, fact in sorted(value["evidence"].items()):
        location = fact["location"]
        if (fact["kind"] == "source_fact" and location is not None and
                location["snapshot_sha256"] == value["source_snapshot_sha256"]):
            observations[identifier] = {"tool": "read_source", "args": {
                "path": location["path"], "start_line": location["start_line"],
                "end_line": min(location["end_line"], location["start_line"] + 199), "draft": False,
            }, "original_end_line": location["end_line"]}
        elif fact["kind"] == "log_observation" and fact["log_line"] is not None:
            end_line = fact["log_line"] + fact["statement"].count("\n")
            observations[identifier] = {"tool": "read_log", "args": {
                "start_line": fact["log_line"],
                "end_line": min(end_line, fact["log_line"] + 199),
            }, "original_end_line": end_line}
    candidate_ids = set(value["candidates"])
    if latest_failure is not None:
        candidate_ids.add(latest_failure["candidate_id"])
    return {
        "authority": "navigation_hint_only", "grants_visibility": False,
        "analysis": [{"tool": "inspect_analysis", "args": {"diagnosis_id": identifier}}
                     for identifier in sorted(value["diagnoses"])],
        "observations": observations,
        "candidate_sources": [{"candidate_id": identifier,
            "read_source_args": {"candidate_id": identifier},
            "search_source_args": {"candidate_id": identifier},
            "guidance": "Supply an inspected relative path and bounded lines for read_source, or a literal query for search_source."}
            for identifier in sorted(candidate_ids)],
        "exploration": {"tool": "search_source",
                        "guidance": "Use a single-line literal to trace unresolved source relationships; inspect pagination and skipped files."},
        "scope_note": "read_source defaults to the baseline; draft=True selects the current draft; candidate_id selects a registered immutable candidate. Draft and candidate_id are exclusive. Candidate lines are not baseline root-cause coordinates.",
    }


def build_decision_context(value: dict[str, Any], *, latest_failure: dict[str, Any] | None,
                           draft_unregistered: bool, decision_guidance: bool = False) -> dict[str, Any]:
    """Compile semantic slots from a caller-verified packet or projection.

    All original record fields, alternatives, unknowns and reference metadata
    are preserved. Records keep their own owner and snapshot fields, including
    null or different snapshots; the public owner is navigation only.
    Phase questions are data suggestions, never action permissions or success.
    Knowledge bodies and selected original tool groups remain the caller's
    responsibility; this function never summarizes or rewrites them.
    Frozen older send plans retain decision_guidance=False to reconstruct
    their exact original board and absence of the derived frontier.
    """
    if type(draft_unregistered) is not bool:
        raise TypeError("draft_unregistered must be a boolean")
    if type(decision_guidance) is not bool:
        raise TypeError("decision_guidance must be a boolean")
    owner = {key: deepcopy(value[key]) for key in _OWNER_FIELDS}
    evidence: dict[str, dict[str, Any]] = {
        "input": {}, "source_reader": {}, "model": {}, "execution_backend": {},
    }
    for identifier, raw in sorted(value["evidence"].items()):
        evidence.setdefault(raw["producer"], {})[identifier] = deepcopy(raw)
    obligations: dict[str, dict[str, Any]] = {"runtime_condition": {}, "business_behavior": {}}
    for identifier, raw in sorted(value["obligations"].items()):
        obligations.setdefault(raw["kind"], {})[identifier] = deepcopy(raw)
    readback = _readback(value, latest_failure)
    return {
        "layout": "repair_decision_frontier_v1" if decision_guidance else "repair_decision_slots_v1", "owner": owner,
        "decision_board": _board(value, latest_failure, draft_unregistered, decision_guidance=decision_guidance),
        **({"decision_frontier": _decision_frontier(value, readback)} if decision_guidance else {}),
        "focus": deepcopy(value["focus"]),
        "diagnoses": deepcopy(value["diagnoses"]),
        "evidence_by_producer": evidence, "obligations_by_kind": obligations,
        "candidates": deepcopy(value["candidates"]),
        "checks": deepcopy(value["checks"]),
        "revision": {
            "latest_failure": deepcopy(latest_failure), "feedback": deepcopy(value["feedback"]),
            "decisions": deepcopy(value["decisions"]), "conditions": deepcopy(value["conditions"]),
            "historical_relevance": deepcopy(value["historical_relevance"]),
        },
        "relationships": _relationships(value), "readback": readback,
        "packet_metadata": {key: deepcopy(raw) for key, raw in value.items()
                            if key not in _RECORD_FIELDS and key not in _OWNER_FIELDS},
    }


def restore_decision_context(layout: dict[str, Any]) -> dict[str, Any]:
    """Recover the original packet data; suggestions add no packet authority."""
    if layout["layout"] not in {"repair_decision_slots_v1", "repair_decision_frontier_v1"}:
        raise ValueError("unsupported repair decision layout")
    evidence: dict[str, Any] = {}
    obligations: dict[str, Any] = {}
    for groups, records in ((layout["evidence_by_producer"], evidence),
                            (layout["obligations_by_kind"], obligations)):
        for group in groups.values():
            for identifier, raw in group.items():
                if identifier in records:
                    raise ValueError("duplicate repair decision record")
                records[identifier] = deepcopy(raw)
    revision = layout["revision"]
    return {
        **deepcopy(layout["packet_metadata"]), **deepcopy(layout["owner"]),
        "focus": deepcopy(layout["focus"]), "diagnoses": deepcopy(layout["diagnoses"]),
        "evidence": evidence, "obligations": obligations,
        "candidates": deepcopy(layout["candidates"]), "checks": deepcopy(layout["checks"]),
        "feedback": deepcopy(revision["feedback"]), "decisions": deepcopy(revision["decisions"]),
        "conditions": deepcopy(revision["conditions"]),
        "historical_relevance": deepcopy(revision["historical_relevance"]),
    }
