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


def _board(value: dict[str, Any], latest_failure: dict[str, Any] | None,
           draft_unregistered: bool) -> dict[str, Any]:
    focus = value["focus"]
    diagnosis = value["diagnoses"].get(focus["diagnosis_id"])
    selected = None if diagnosis is None else diagnosis.get("selected_hypothesis_id")
    candidate_id = focus["current_candidate_id"]
    check = value["checks"].get(candidate_id)
    check_state = None if check is None else check["state"]
    result = None if check is None else check["result"]
    status = None if result is None else result["status"]

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
    elif diagnosis is None:
        phase, question = "exploration", "establish_fault_and_follow_values"
        prompt = "What do the fault log and related source observations show, and what remains unexplained?"
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
    return {
        "authority": "host_record_projection", "advisory_only": True,
        "execution_permission": "not_granted", "repair_acceptance": "not_assessed",
        "phase": phase, "next_question_kind": question, "question": prompt,
        "diagnosis_id": focus["diagnosis_id"], "selected_hypothesis_id": selected,
        "current_candidate_id": candidate_id, "check_state": check_state,
        "check_status": status, "draft_unregistered": draft_unregistered,
        "historical_failure_candidate_id": None if latest_failure is None else latest_failure["candidate_id"],
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


def _readback(value: dict[str, Any]) -> dict[str, Any]:
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
    return {
        "authority": "navigation_hint_only", "grants_visibility": False,
        "analysis": [{"tool": "inspect_analysis", "args": {"diagnosis_id": identifier}}
                     for identifier in sorted(value["diagnoses"])],
        "observations": observations,
        "exploration": {"tool": "search_source",
                        "guidance": "Use a single-line literal to trace unresolved source relationships; inspect pagination and skipped files."},
        "scope_note": "read_source reads the fixed baseline or current draft. It cannot read an arbitrary historical candidate snapshot.",
    }


def build_decision_context(value: dict[str, Any], *, latest_failure: dict[str, Any] | None,
                           draft_unregistered: bool) -> dict[str, Any]:
    """Compile semantic slots from a caller-verified packet or projection.

    All original record fields, alternatives, unknowns and reference metadata
    are preserved. Records keep their own owner and snapshot fields, including
    null or different snapshots; the public owner is navigation only.
    Phase questions are data suggestions, never action permissions or success.
    Knowledge bodies and selected original tool groups remain the caller's
    responsibility; this function never summarizes or rewrites them.
    """
    if type(draft_unregistered) is not bool:
        raise TypeError("draft_unregistered must be a boolean")
    owner = {key: deepcopy(value[key]) for key in _OWNER_FIELDS}
    evidence: dict[str, dict[str, Any]] = {
        "input": {}, "source_reader": {}, "model": {}, "execution_backend": {},
    }
    for identifier, raw in sorted(value["evidence"].items()):
        evidence.setdefault(raw["producer"], {})[identifier] = deepcopy(raw)
    obligations: dict[str, dict[str, Any]] = {"runtime_condition": {}, "business_behavior": {}}
    for identifier, raw in sorted(value["obligations"].items()):
        obligations.setdefault(raw["kind"], {})[identifier] = deepcopy(raw)
    return {
        "layout": "repair_decision_slots_v1", "owner": owner,
        "decision_board": _board(value, latest_failure, draft_unregistered),
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
        "relationships": _relationships(value), "readback": _readback(value),
        "packet_metadata": {key: deepcopy(raw) for key, raw in value.items()
                            if key not in _RECORD_FIELDS and key not in _OWNER_FIELDS},
    }


def restore_decision_context(layout: dict[str, Any]) -> dict[str, Any]:
    """Recover the original packet data; suggestions add no packet authority."""
    if layout["layout"] != "repair_decision_slots_v1":
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
