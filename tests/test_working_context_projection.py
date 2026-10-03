"""Pure bounded solver views preserve real registered synthetic semantics."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from pydantic import ValidationError
from test_agent_loop import task_snapshot
from test_condition_handoff import context as condition_context
from test_decisions import FULL
from test_working_context import failed_then_current, packet, tree_bytes

from dfx_agent.decisions import DecisionRequest, DecisionStore
from dfx_agent.model_calls import canonical, sha256
from dfx_agent.working_context import (
    WorkingContextError,
    WorkingContextFocus,
    WorkingContextPacket,
)
from dfx_agent.working_context_projection import (
    PROFILE,
    SolverContextProjection,
    project_working_context,
    verify_solver_projection,
)

BUDGET = 262_144


@pytest.fixture
def checked_packet(tmp_path: Path) -> WorkingContextPacket:
    task, workspace, ledger, _, focus, _, _, _ = failed_then_current(tmp_path)
    return packet(task, workspace, ledger, focus)


def repackage(value: dict) -> WorkingContextPacket:
    payload = canonical(value)
    return WorkingContextPacket(canonical_payload_json=payload, payload_sha256=sha256(payload),
                                max_bytes=len(payload.encode("utf-8")))


def reproject(value: dict, *, max_bytes: int = BUDGET) -> SolverContextProjection:
    payload = canonical(value)
    return SolverContextProjection(canonical_payload_json=payload, payload_sha256=sha256(payload),
                                   audit_packet_sha256=value["audit_packet_sha256"], max_bytes=max_bytes)


def test_actual_registered_semantics_remain_complete_and_no_sources_are_read(
    checked_packet: WorkingContextPacket, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    before = tree_bytes(tmp_path)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("the solver projection must not reread sources")

    with monkeypatch.context() as pure_boundary:
        pure_boundary.setattr(Path, "read_bytes", forbidden)
        pure_boundary.setattr(Path, "read_text", forbidden)
        projected = project_working_context(checked_packet, max_bytes=BUDGET)
    original, value = checked_packet.contents(), projected.contents()
    assert value["schema_version"] == 2 and value["profile"] == PROFILE
    assert value["audit_packet_sha256"] == checked_packet.payload_sha256
    assert "sources" not in value
    assert set(value) == (set(original) - {"sources"}) | {"audit_packet_sha256", "audit_sources"}
    for name in set(original) - {"schema_version", "profile", "sources"}:
        assert value[name] == original[name]
    assert value["audit_sources"] == {
        "source_count": len(original["sources"]),
        "total_reference_bytes": sum(source["bytes"] for source in original["sources"]),
        "manifest_sha256": sha256(canonical(original["sources"])),
    }
    assert len(value["diagnoses"]["diagnosis1"]["hypotheses"]) == 2
    assert set(value["obligations"]) == {"keep_value", "keep_state"}
    assert value["evidence"]["source1"]["statement"] == "value=bad"
    assert value["historical_relevance"]
    assert all(item["semantic_relevance"] == "unknown" and not item["is_current_candidate_observation"]
               for item in value["historical_relevance"].values())
    assert tree_bytes(tmp_path) == before


@pytest.mark.parametrize("status", ["violated", "satisfied", "unknown"])
def test_full_condition_scope_findings_and_unknowns_survive_without_authority_upgrade(tmp_path: Path, status: str) -> None:
    task = task_snapshot(tmp_path)
    workspace, ledger, _, _, request, record, raw = condition_context(task, tmp_path / "run", status=status)
    decision = DecisionStore(workspace).decide(DecisionRequest.model_validate_json(json.dumps(raw)), ledger=ledger,
        features=FULL, capabilities=frozenset({"read_source"}))
    focus = WorkingContextFocus(diagnosis_id="diagnosis1", hypothesis_ids=("h1",), decision_id=decision.record_id)
    original = packet(task, workspace, ledger, focus)
    before = tree_bytes(tmp_path)
    result = project_working_context(original, max_bytes=BUDGET).contents()
    assert result["conditions"][record.result_id] == record.model_dump(mode="json")
    condition = result["conditions"][record.result_id]["result"]
    assert condition["request"]["scope"] == request.scope.model_dump(mode="json")
    assert condition["status"] == status
    assert condition["support_level"] == ("unknown" if status == "unknown" else "synthetic")
    assert bool(condition["unknown_reasons"]) == (status == "unknown")
    assert bool(condition["findings"]) == (status != "unknown")
    assert condition["root_cause_confirmation"] == "unknown" and condition["application_verification"] == "not_run"
    assert result["decisions"][decision.record_id] == original.contents()["decisions"][decision.record_id]
    assert tree_bytes(tmp_path) == before and ledger.executor.requests == []


def test_thousands_of_audit_receipts_do_not_become_model_metadata(checked_packet: WorkingContextPacket) -> None:
    original = project_working_context(checked_packet, max_bytes=BUDGET)
    expanded = checked_packet.contents()
    expanded["sources"] = sorted(expanded["sources"] + [
        {"reference": f"workspace/base/irrelevant/file-{index:05d}.ets", "bytes": 4096,
         "sha256": sha256(f"synthetic independent receipt {index}")} for index in range(5_000)
    ], key=lambda item: item["reference"])
    # This is an explicit package-shape fixture, not a claim that the added
    # receipts have been source-authenticated by the pure projection boundary.
    large = repackage(expanded)
    projected = project_working_context(large, max_bytes=BUDGET)
    left, right = original.contents(), projected.contents()
    assert projected.contents()["audit_sources"]["source_count"] == len(expanded["sources"])
    assert projected.contents()["audit_sources"]["total_reference_bytes"] == sum(
        item["bytes"] for item in expanded["sources"])
    for value in (left, right):
        value.pop("audit_packet_sha256")
        value.pop("audit_sources")
    assert left == right
    assert abs(len(projected.canonical_payload_json.encode("utf-8")) -
               len(original.canonical_payload_json.encode("utf-8"))) < 32
    assert "file-04999.ets" not in projected.canonical_payload_json
    assert len(large.canonical_payload_json.encode("utf-8")) > len(projected.canonical_payload_json.encode("utf-8")) * 10


def test_projection_is_deterministic_immutable_and_restores_independent_copies(
    checked_packet: WorkingContextPacket,
) -> None:
    first = project_working_context(checked_packet, max_bytes=BUDGET)
    assert project_working_context(checked_packet, max_bytes=BUDGET) == first
    copied = first.contents()
    copied["evidence"].clear()
    copied["obligations"]["keep_value"]["authority"] = "verified_requirement"
    assert first.contents()["evidence"]
    assert first.contents()["obligations"]["keep_value"]["authority"] == "model_inference"
    with pytest.raises(ValidationError):
        first.audit_packet_sha256 = "0" * 64  # type: ignore[misc]
    restored = SolverContextProjection.model_validate_json(first.model_dump_json())
    assert restored == verify_solver_projection(restored, checked_packet, max_bytes=BUDGET)
    assert restored.canonical_payload_json == canonical(restored.contents())


@pytest.mark.parametrize("tamper", ["omit_unknowns", "omit_evidence", "authority", "candidate_scope", "audit_count"])
def test_rehashed_semantic_or_audit_edits_cannot_replace_the_frozen_projection(
    checked_packet: WorkingContextPacket, tamper: str,
) -> None:
    original = project_working_context(checked_packet, max_bytes=BUDGET)
    value = original.contents()
    if tamper == "omit_unknowns":
        value["diagnoses"]["diagnosis1"].pop("unknowns")
    elif tamper == "omit_evidence":
        value["evidence"].pop("source1")
    elif tamper == "authority":
        value["obligations"]["keep_value"]["authority"] = "verified_requirement"
    elif tamper == "candidate_scope":
        next(iter(value["feedback"].values()))["candidate_snapshot_sha256"] = "0" * 64
    else:
        value["audit_sources"]["source_count"] += 1
    forged = reproject(value)
    with pytest.raises(WorkingContextError, match="working_context_projection_mismatch"):
        verify_solver_projection(forged, checked_packet, max_bytes=BUDGET)


@pytest.mark.parametrize("tamper", ["task", "run", "snapshot", "profile", "header_authority", "payload_hash",
                                  "packet_hash", "noncanonical", "unknown_key", "list_in_payload"])
def test_projection_syntax_owner_profile_and_hash_are_strict(checked_packet: WorkingContextPacket, tamper: str) -> None:
    original = project_working_context(checked_packet, max_bytes=BUDGET)
    value = original.contents()
    if tamper == "task":
        value["task_id"] = "another_task"
    elif tamper == "run":
        value["run_id"] = "another_run"
    elif tamper == "snapshot":
        value["source_snapshot_sha256"] = "0" * 64
    elif tamper == "profile":
        value["profile"] = "unregistered_projection"
    elif tamper == "header_authority":
        value["root_cause_confirmation"] = "confirmed"
    elif tamper == "unknown_key":
        value["extra"] = "unregistered model material"
    elif tamper == "list_in_payload":
        value["sources"] = checked_packet.contents()["sources"]
    forged = copy.deepcopy(original.model_dump(mode="json"))
    payload = canonical(value)
    forged.update(canonical_payload_json=payload, payload_sha256=sha256(payload))
    if tamper == "payload_hash":
        forged["payload_sha256"] = "0" * 64
    elif tamper == "packet_hash":
        forged["audit_packet_sha256"] = "0" * 64
    elif tamper == "noncanonical":
        forged["canonical_payload_json"] += " "
    with pytest.raises(ValidationError):
        SolverContextProjection.model_validate(forged)


def test_required_utf8_budget_never_truncates_or_drops_semantics(checked_packet: WorkingContextPacket) -> None:
    projected = project_working_context(checked_packet, max_bytes=BUDGET)
    size = len(projected.canonical_payload_json.encode("utf-8"))
    assert size > len(projected.canonical_payload_json)
    assert project_working_context(checked_packet, max_bytes=size).canonical_payload_json == projected.canonical_payload_json
    with pytest.raises(WorkingContextError, match="working_context_required_limit"):
        project_working_context(checked_packet, max_bytes=size - 1)
    with pytest.raises(WorkingContextError, match="working_context_projection_mismatch"):
        verify_solver_projection(projected, checked_packet, max_bytes=size)


def test_unchecked_model_copy_cannot_bypass_packet_or_projection_hash_validation(
    checked_packet: WorkingContextPacket,
) -> None:
    forged_packet = checked_packet.model_copy(update={"payload_sha256": "0" * 64})
    with pytest.raises(WorkingContextError, match="working_context_projection_invalid"):
        project_working_context(forged_packet, max_bytes=BUDGET)
    original = project_working_context(checked_packet, max_bytes=BUDGET)
    forged_projection = original.model_copy(update={"payload_sha256": "0" * 64})
    with pytest.raises(WorkingContextError, match="working_context_projection_invalid"):
        verify_solver_projection(forged_projection, checked_packet, max_bytes=BUDGET)


@pytest.mark.parametrize("max_bytes", [True, False, 0, -1, 1.5, "200"])
def test_invalid_byte_budget_is_not_coerced(checked_packet: WorkingContextPacket, max_bytes) -> None:
    with pytest.raises(ValueError, match="positive integer"):
        project_working_context(checked_packet, max_bytes=max_bytes)
    with pytest.raises(ValueError, match="positive integer"):
        verify_solver_projection(project_working_context(checked_packet, max_bytes=BUDGET), checked_packet,
                                 max_bytes=max_bytes)


@pytest.mark.parametrize("fault", ["audit_profile", "duplicate_reference", "unsorted_references", "negative_bytes",
                                 "boolean_bytes", "bad_scope", "bad_path", "bad_hash", "extra_receipt_field"])
def test_input_packet_and_audit_manifest_syntax_is_checked_without_source_certification(
    checked_packet: WorkingContextPacket, fault: str,
) -> None:
    value = checked_packet.contents()
    if fault == "audit_profile":
        value["profile"] = "unknown_audit_profile"
    elif fault == "duplicate_reference":
        value["sources"].append(copy.deepcopy(value["sources"][0]))
    elif fault == "unsorted_references":
        value["sources"].reverse()
    elif fault == "negative_bytes":
        value["sources"][0]["bytes"] = -1
    elif fault == "boolean_bytes":
        value["sources"][0]["bytes"] = True
    elif fault == "bad_scope":
        value["sources"][0]["reference"] = "outside/file.ets"
    elif fault == "bad_path":
        value["sources"][0]["reference"] = "workspace/../file.ets"
    elif fault == "bad_hash":
        value["sources"][0]["sha256"] = "not a digest"
    else:
        value["sources"][0]["source_text"] = "unregistered material"
    with pytest.raises(WorkingContextError, match="working_context_projection_invalid"):
        project_working_context(repackage(value), max_bytes=BUDGET)
