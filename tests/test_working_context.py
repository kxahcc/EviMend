"""Real registered synthetic records become source-checked independent packets."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from pydantic import ValidationError
from test_agent_loop import task_snapshot
from test_condition_handoff import context as condition_context
from test_decisions import FULL, decision_request, diagnosis_submission

from dfx_agent import working_context
from dfx_agent.agent import _tools
from dfx_agent.decisions import DecisionRequest, DecisionStore
from dfx_agent.diagnostics import AnalysisStore
from dfx_agent.model_calls import canonical, sha256
from dfx_agent.task import digest, verify_task
from dfx_agent.verification import CheckLedger, FixedTextExecutor
from dfx_agent.working_context import (
    WorkingContextError,
    WorkingContextFocus,
    WorkingContextPacket,
    build_working_context,
    verify_working_context,
)
from dfx_agent.workspace import CandidateWorkspace

BUDGET = 262_144


def tree_bytes(root: Path) -> dict[str, bytes]:
    return {file.relative_to(root).as_posix(): file.read_bytes() for file in root.rglob("*") if file.is_file()}


def registered(tmp_path: Path):
    task = task_snapshot(tmp_path)
    workspace = CandidateWorkspace(task, tmp_path / "run", "run1")
    executor = FixedTextExecutor("src/main.ets", "value=good\n", failure_detail="保留输出的合成检查失败")
    ledger = CheckLedger(workspace, executor)
    tools = {item.name: item for item in _tools(workspace, ledger, 3, task, "crash.log",
        verify_task(task)["crash_sha256"], FULL)}
    raw = diagnosis_submission(task)
    raw["log_citations"] = [{"evidence_id": "log1", "start_line": 1, "end_line": 1}]
    result = json.loads(tools["record_diagnosis"].invoke({"diagnosis_json": json.dumps(raw)}))
    assert result["diagnosis_id"] == "diagnosis1"
    initial = json.loads(tools["plan_repair_action"].invoke({"plan_json": json.dumps(decision_request(task))}))
    return task, workspace, ledger, tools, initial


def failed_then_current(tmp_path: Path):
    task, workspace, ledger, tools, initial = registered(tmp_path)
    tools["edit_source"].invoke({"path": "src/main.ets", "old": "bad", "new": "almost"})
    old = json.loads(tools["register_candidate"].invoke({"diagnosis_id": "diagnosis1",
        "decision_id": initial["record_id"], "direction_id": "repair1", "preserves": ["keep_value"]}))["candidate_id"]
    assert json.loads(tools["run_check"].invoke({"candidate_id": old}))["status"] == "fail"
    feedback = json.loads(tools["link_failed_check"].invoke({"candidate_id": old,
        "violated_obligation_ids": ["keep_value"]}))
    revised = {**decision_request(task), "candidate_id": old, "feedback_id": feedback["feedback_id"]}
    historical = json.loads(tools["plan_repair_action"].invoke({"plan_json": json.dumps(revised)}))
    tools["edit_source"].invoke({"path": "src/main.ets", "old": "bad", "new": "good"})
    current = json.loads(tools["register_candidate"].invoke({"diagnosis_id": "diagnosis1",
        "decision_id": initial["record_id"], "direction_id": "repair1", "preserves": ["keep_value"]}))["candidate_id"]
    focus = WorkingContextFocus(diagnosis_id="diagnosis1", hypothesis_ids=("h1",), current_candidate_id=current,
        decision_id=initial["record_id"], feedback_decision_ids=(historical["record_id"],))
    return task, workspace, ledger, tools, focus, old, current, feedback["feedback_id"]


def packet(task, workspace, ledger, focus, *, max_bytes=BUDGET):
    return build_working_context(workspace, focus, task_directory=task, ledger=ledger, max_bytes=max_bytes)


def test_actual_registration_retains_old_counterexample_and_current_candidate_scope(tmp_path: Path) -> None:
    task, workspace, ledger, _, focus, old, current, feedback_id = failed_then_current(tmp_path)
    before = tree_bytes(tmp_path)
    requests = list(ledger.executor.requests)
    result = packet(task, workspace, ledger, focus)
    value = result.contents()
    assert set(value["candidates"]) == {old, current}
    assert value["checks"][old]["result"]["status"] == "fail"
    assert value["checks"][current]["state"] == "not_requested"
    assert value["checks"][current]["result"] is None
    assert value["feedback"][feedback_id]["candidate_id"] == old
    relevance = value["historical_relevance"][focus.feedback_decision_ids[0]]
    assert relevance["shared_obligation_ids"] == ["keep_value"]
    assert relevance["observed_candidate_id"] == old and relevance["current_candidate_id"] == current
    assert relevance["is_current_candidate_observation"] is False
    assert relevance["semantic_relevance"] == "unknown"
    assert set(value["obligations"]) == {"keep_value", "keep_state"}
    assert len(value["diagnoses"]["diagnosis1"]["hypotheses"]) == 2
    assert set(value["evidence"]) == {"source1", "infer1", "infer2", "log1"}
    assert value["evidence"]["source1"]["producer"] == "source_reader"
    assert value["evidence"]["log1"]["producer"] == "input"
    assert value["evidence"]["infer1"]["kind"] == "model_inference"
    assert value["tool_action_authentication"] == "not_performed"
    assert value["root_cause_confirmation"] == "unknown" and value["application_verification"] == "not_run"
    assert ledger.executor.requests == requests and tree_bytes(tmp_path) == before


def test_frozen_readback_is_canonical_and_returns_independent_copies(tmp_path: Path) -> None:
    task, workspace, ledger, _, focus, _, _, _ = failed_then_current(tmp_path)
    original = packet(task, workspace, ledger, focus)
    assert original.canonical_payload_json == canonical(original.contents())
    copied = original.contents()
    copied["obligations"]["keep_value"]["authority"] = "verified_requirement"
    copied["evidence"].clear()
    assert original.contents()["obligations"]["keep_value"]["authority"] == "model_inference"
    restored = WorkingContextPacket.model_validate_json(original.model_dump_json())
    assert verify_working_context(restored, workspace, task_directory=task, ledger=ledger, max_bytes=BUDGET) == original
    assert packet(task, workspace, ledger, focus) == original
    with pytest.raises(ValidationError):
        original.payload_sha256 = "0" * 64  # type: ignore[misc]


def test_appending_unrelated_valid_registration_does_not_replace_frozen_focus(tmp_path: Path) -> None:
    task, workspace, ledger, tools, focus, _, _, _ = failed_then_current(tmp_path)
    original = packet(task, workspace, ledger, focus)
    added = json.loads(tools["record_diagnosis"].invoke({"path": "src/main.ets", "line": 1,
                                                       "reason": "unrelated proposed cause"}))
    assert added["diagnosis_id"] != focus.diagnosis_id
    before = tree_bytes(tmp_path)
    assert verify_working_context(original, workspace, task_directory=task, ledger=ledger, max_bytes=BUDGET) == original
    assert added["diagnosis_id"] not in original.contents()["diagnoses"]
    assert tree_bytes(tmp_path) == before


def test_unknown_starting_focus_reads_without_records_or_creating_directories(tmp_path: Path) -> None:
    task = task_snapshot(tmp_path)
    workspace = CandidateWorkspace(task, tmp_path / "run", "run1")
    ledger = CheckLedger(workspace, FixedTextExecutor("src/main.ets", "value=good\n"))
    before = tree_bytes(tmp_path)
    result = packet(task, workspace, ledger, WorkingContextFocus())
    assert result.contents()["focus"]["diagnosis_id"] is None
    for key in ("diagnoses", "evidence", "obligations", "candidates", "decisions", "conditions", "feedback"):
        assert result.contents()[key] == {}
    assert result.contents()["focus_recency"] == "not_authenticated"
    assert not (workspace.root / "diagnoses").exists() and not (workspace.root / "conditions").exists()
    assert ledger.executor.requests == [] and tree_bytes(tmp_path) == before


def test_necessary_packet_budget_is_exact_utf8_bytes_and_never_truncates(tmp_path: Path) -> None:
    task, workspace, ledger, _, focus, _, _, _ = failed_then_current(tmp_path)
    original = packet(task, workspace, ledger, focus)
    size = len(original.canonical_payload_json.encode("utf-8"))
    assert size > len(original.canonical_payload_json)
    exact = packet(task, workspace, ledger, focus, max_bytes=size)
    assert exact.canonical_payload_json == original.canonical_payload_json
    before = tree_bytes(tmp_path)
    with pytest.raises(WorkingContextError, match="working_context_required_limit"):
        packet(task, workspace, ledger, focus, max_bytes=size - 1)
    assert tree_bytes(tmp_path) == before


@pytest.mark.parametrize("budget", [True, False, 0, -1, 1.5, "200"])
def test_invalid_byte_budgets_do_not_read_or_write_sources(tmp_path: Path, budget) -> None:
    task, workspace, ledger, _, _ = registered(tmp_path)
    before = tree_bytes(tmp_path)
    with pytest.raises(ValueError, match="positive integer"):
        packet(task, workspace, ledger, WorkingContextFocus(), max_bytes=budget)
    assert tree_bytes(tmp_path) == before


@pytest.mark.parametrize("fault", ["source_text", "source_hash", "log_text", "log_hash", "inference_hash",
                                  "hypothesis_status", "obligation_authority", "obligation_status", "causal_authority",
                                  "missing_basis", "wrong_snapshot", "empty_obligation_basis", "target_evidence_collision"])
def test_saved_analysis_cannot_promote_authority_or_replace_original_facts(tmp_path: Path, fault: str) -> None:
    task, workspace, ledger, _, _ = registered(tmp_path)
    path = workspace.root / "diagnoses/diagnosis1.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    by_id = {item["record_id"]: item for item in raw["evidence"]}
    if fault == "source_text":
        by_id["source1"].update(statement="value=good", artifact_sha256=digest(b"value=good"))
    elif fault == "source_hash":
        by_id["source1"]["artifact_sha256"] = digest(b"another")
    elif fault == "log_text":
        by_id["log1"]["statement"] = "claimed different crash"
    elif fault == "log_hash":
        by_id["log1"]["artifact_sha256"] = digest(b"another log")
    elif fault == "inference_hash":
        by_id["infer1"]["statement"] = "new inferred text"
    elif fault == "hypothesis_status":
        raw["diagnosis"]["hypotheses"][0]["support_status"] = "supported"
    elif fault == "obligation_authority":
        raw["obligations"][0]["authority"] = "verified_requirement"
    elif fault == "obligation_status":
        raw["obligations"][0]["status"] = "satisfied"
    elif fault == "causal_authority":
        hypothesis = raw["diagnosis"]["hypotheses"][0]
        location = raw["diagnosis"]["exception_locations"][0]
        hypothesis["constraint"] = {"family": "bounds", "operation": location, "condition": "capacity sufficient",
            "semantic_source_evidence_ids": ["source1"], "bindings": [
                {"binding_id": identifier, "expression": identifier, "role": "value", "evidence_ids": ["source1"]}
                for identifier in ("a", "b")]}
        hypothesis["causal_steps"] = [{"step_id": "step1", "from_binding_id": "a", "to_binding_id": "b",
                                       "relation": "claimed relation", "authority": "source_fact", "evidence_ids": ["source1"]}]
    elif fault == "missing_basis":
        by_id["infer1"]["basis_ids"] = ["absent"]
    elif fault == "empty_obligation_basis":
        raw["obligations"][0]["source_evidence_ids"] = []
    elif fault == "target_evidence_collision":
        hypothesis = raw["diagnosis"]["hypotheses"][0]
        hypothesis["hypothesis_id"] = "source1"
        hypothesis["distinguishing_check"]["observation_routes"]["value"] = "source1"
        raw["diagnosis"]["selected_hypothesis_id"] = "source1"
        raw["obligations"][0]["hypothesis_ids"] = ["source1"]
    else:
        by_id["source1"]["source_snapshot_sha256"] = digest(b"other snapshot")
    path.write_text(json.dumps(raw), encoding="utf-8")
    before = tree_bytes(tmp_path)
    with pytest.raises(WorkingContextError, match="working_context_source_invalid"):
        packet(task, workspace, ledger, WorkingContextFocus(diagnosis_id="diagnosis1"))
    assert tree_bytes(tmp_path) == before


@pytest.mark.parametrize("fault", ["observation", "authority", "scope", "candidate_version", "execution", "missing_check"])
def test_historical_feedback_must_keep_actual_failed_receipt_and_candidate_version(tmp_path: Path, fault: str) -> None:
    task, workspace, ledger, _, focus, old, _, feedback_id = failed_then_current(tmp_path)
    path = workspace.root / f"feedback/{feedback_id}.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    if fault == "observation":
        raw["observation"] = "claimed success"
    elif fault == "authority":
        raw["link_authority"] = "verified_check"
    elif fault == "scope":
        raw.update(scope="application", evidence_kind="application_execution")
    elif fault == "candidate_version":
        raw["candidate_snapshot_sha256"] = digest(b"another candidate")
    elif fault == "execution":
        raw["execution_id"] = "unknown_execution"
    else:
        _, check_path = ledger._identity(old, "offline")
        check_path.unlink()
    if fault != "missing_check":
        path.write_text(json.dumps(raw), encoding="utf-8")
    before = tree_bytes(tmp_path)
    with pytest.raises(WorkingContextError, match="working_context_source_invalid"):
        packet(task, workspace, ledger, focus)
    assert tree_bytes(tmp_path) == before


def test_frozen_packet_detects_semantically_valid_inference_revision(tmp_path: Path) -> None:
    task, workspace, ledger, _, _ = registered(tmp_path)
    focus = WorkingContextFocus(diagnosis_id="diagnosis1")
    original = packet(task, workspace, ledger, focus)
    path = workspace.root / "diagnoses/diagnosis1.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    note = next(item for item in raw["evidence"] if item["record_id"] == "infer1")
    note.update(statement="A different, still unconfirmed inference", artifact_sha256=digest(b"A different, still unconfirmed inference"))
    path.write_text(json.dumps(raw), encoding="utf-8")
    # Content hashes are not signatures. A newly built explicit packet can quote
    # coherent unconfirmed text; it must not replace the previously frozen one.
    revised = packet(task, workspace, ledger, focus)
    assert revised.contents()["evidence"]["infer1"]["kind"] == "model_inference"
    assert revised.payload_sha256 != original.payload_sha256
    with pytest.raises(WorkingContextError, match="working_context_frozen_mismatch"):
        verify_working_context(original, workspace, task_directory=task, ledger=ledger, max_bytes=BUDGET)


def test_explicit_focus_must_bind_registered_diagnosis_hypotheses_and_current_candidate(tmp_path: Path) -> None:
    task, workspace, ledger, tools, focus, _, current, _ = failed_then_current(tmp_path)
    unrelated = json.loads(tools["record_diagnosis"].invoke({"path": "src/main.ets", "line": 1, "reason": "other"}))
    for invalid in (WorkingContextFocus(diagnosis_id="absent"),
                    WorkingContextFocus(diagnosis_id="diagnosis1", hypothesis_ids=("absent",)),
                    WorkingContextFocus(diagnosis_id=unrelated["diagnosis_id"], current_candidate_id=current),
                    WorkingContextFocus(diagnosis_id=unrelated["diagnosis_id"], decision_id=focus.decision_id),
                    WorkingContextFocus(diagnosis_id="diagnosis1", feedback_decision_ids=(focus.decision_id,))):
        with pytest.raises(WorkingContextError, match="working_context_source_invalid"):
            packet(task, workspace, ledger, invalid)
    irrelevant = WorkingContextFocus(diagnosis_id=unrelated["diagnosis_id"],
                                      feedback_decision_ids=focus.feedback_decision_ids)
    with pytest.raises(WorkingContextError, match="working_context_source_invalid"):
        packet(task, workspace, ledger, irrelevant)


def duplicate_hypothesis_bundle(workspace: CandidateWorkspace) -> str:
    """Simulate coherent disk replacement, which past-action audit would catch.

    Ordinary AnalysisStore admission forbids target reuse. The independent
    packet still needs to handle ambiguous same-named targets in existing files
    rather than use their lexicographic order to fabricate an owner.
    """
    raw = json.loads((workspace.root / "diagnoses/diagnosis1.json").read_text(encoding="utf-8"))
    raw["diagnosis"]["record_id"] = "aaa_other_diagnosis"
    raw["obligations"] = []
    for hypothesis in raw["diagnosis"]["hypotheses"]:
        hypothesis["obligation_ids"] = []
    (workspace.root / "diagnoses/aaa_other_diagnosis.json").write_text(json.dumps(raw), encoding="utf-8")
    return raw["diagnosis"]["record_id"]


def test_same_hypothesis_labels_in_another_diagnosis_violate_global_admission_namespace(tmp_path: Path) -> None:
    task, workspace, ledger, tools, _ = registered(tmp_path)
    other = duplicate_hypothesis_bundle(workspace)
    tools["edit_source"].invoke({"path": "src/main.ets", "old": "bad", "new": "good"})
    current = json.loads(tools["register_candidate"].invoke({"diagnosis_id": other,
                                                            "preserves": ["keep_value"]}))["candidate_id"]
    with pytest.raises(WorkingContextError, match="working_context_source_invalid"):
        packet(task, workspace, ledger, WorkingContextFocus(diagnosis_id=other, current_candidate_id=current))


@pytest.mark.parametrize("association", ["hypothesis", "binding"])
def test_standalone_obligation_cannot_add_structured_associations(tmp_path: Path, association: str) -> None:
    task, workspace, ledger, tools, _ = registered(tmp_path)
    saved = json.loads(tools["record_obligation"].invoke({"obligation_id": "keep_extra",
        "statement": "An additional unconfirmed business constraint", "evidence_ids": ["infer1"]}))
    # Structured standalone obligations are not admitted by the normal tool.
    # Simulate this extra association being written to disk after admission.
    if association == "hypothesis":
        saved["hypothesis_ids"] = ["h1"]
    else:
        saved["bindings"] = [{"binding_id": "extra", "expression": "value", "role": "value",
                              "evidence_ids": ["infer1"]}]
    (workspace.root / "obligations/keep_extra.json").write_text(json.dumps(saved), encoding="utf-8")
    tools["edit_source"].invoke({"path": "src/main.ets", "old": "bad", "new": "good"})
    current = json.loads(tools["register_candidate"].invoke({"diagnosis_id": "diagnosis1",
                                                            "preserves": ["keep_extra"]}))["candidate_id"]
    before = tree_bytes(tmp_path)
    with pytest.raises(WorkingContextError, match="working_context_source_invalid"):
        packet(task, workspace, ledger, WorkingContextFocus(diagnosis_id="diagnosis1", current_candidate_id=current))
    assert tree_bytes(tmp_path) == before


def revise_inference(path: Path) -> None:
    raw = json.loads(path.read_text(encoding="utf-8"))
    item = next(item for item in raw["evidence"] if item["record_id"] == "infer1")
    item.update(statement="A coherent but changed inference", artifact_sha256=digest(b"A coherent but changed inference"))
    path.write_text(json.dumps(raw), encoding="utf-8")


@pytest.mark.parametrize("restore", [False, True])
def test_valid_content_replacement_between_store_reads_cannot_mix_object_and_source_hash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, restore: bool
) -> None:
    task, workspace, ledger, _, _ = registered(tmp_path)
    original = AnalysisStore.bundles
    changed = False

    def swap_after_first_read(store):
        nonlocal changed
        path = workspace.root / "diagnoses/diagnosis1.json"
        if not changed and restore:
            initial = path.read_bytes()
            revise_inference(path)
            result = original(store)
            path.write_bytes(initial)
            changed = True
            return result
        result = original(store)
        if not changed:
            changed = True
            revise_inference(path)
        return result

    monkeypatch.setattr(AnalysisStore, "bundles", swap_after_first_read)
    with pytest.raises(WorkingContextError, match="working_context_source_invalid"):
        packet(task, workspace, ledger, WorkingContextFocus(diagnosis_id="diagnosis1"))
    assert changed and ledger.executor.requests == []


def test_valid_content_replacement_after_packet_assembly_is_caught_by_final_source_recheck(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    task, workspace, ledger, _, _ = registered(tmp_path)
    original = working_context._Builder.packet_value

    def swap_after_assembly(builder, focus):
        result = original(builder, focus)
        revise_inference(workspace.root / "diagnoses/diagnosis1.json")
        return result

    monkeypatch.setattr(working_context._Builder, "packet_value", swap_after_assembly)
    with pytest.raises(WorkingContextError, match="working_context_source_invalid"):
        packet(task, workspace, ledger, WorkingContextFocus(diagnosis_id="diagnosis1"))
    assert ledger.executor.requests == []


@pytest.mark.parametrize("mutation", ["change", "add", "remove"])
def test_selected_candidate_source_drift_after_assembly_cannot_escape_final_revalidation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    task, workspace, ledger, _, focus, _, current, _ = failed_then_current(tmp_path)
    original = working_context._Builder.packet_value
    requests = list(ledger.executor.requests)

    def swap_after_assembly(builder, selected_focus):
        result = original(builder, selected_focus)
        source = workspace.candidates / current / "source/src/main.ets"
        if mutation == "change":
            source.write_text("value=changed_after_validation\n", encoding="utf-8")
        elif mutation == "add":
            source.with_name("extra.ets").write_text("unregistered content", encoding="utf-8")
        else:
            source.unlink()
        return result

    monkeypatch.setattr(working_context._Builder, "packet_value", swap_after_assembly)
    with pytest.raises(WorkingContextError, match="working_context_source_invalid"):
        packet(task, workspace, ledger, focus)
    assert ledger.executor.requests == requests


def test_other_diagnosis_can_retain_history_by_shared_obligation_without_transferring_observation(tmp_path: Path) -> None:
    task, workspace, ledger, tools, old_focus, old, _, feedback_id = failed_then_current(tmp_path)
    other = json.loads(tools["record_diagnosis"].invoke({"path": "src/main.ets", "line": 1,
                                                       "reason": "A new unknown cause hypothesis"}))["diagnosis_id"]
    tools["edit_source"].invoke({"path": "src/main.ets", "old": "bad", "new": "different"})
    current = json.loads(tools["register_candidate"].invoke({"diagnosis_id": other,
                                                            "preserves": ["keep_value"]}))["candidate_id"]
    focus = WorkingContextFocus(diagnosis_id=other, current_candidate_id=current,
                                feedback_decision_ids=old_focus.feedback_decision_ids)
    result = packet(task, workspace, ledger, focus)
    value = result.contents()
    assert value["candidates"][current]["diagnosis_id"] == other
    assert value["feedback"][feedback_id]["candidate_id"] == old
    relevance = value["historical_relevance"][focus.feedback_decision_ids[0]]
    assert relevance["shared_obligation_ids"] == ["keep_value"]
    assert relevance["semantic_relevance"] == "unknown" and relevance["is_current_candidate_observation"] is False
    assert value["checks"][current]["state"] == "not_requested"


def test_legitimate_check_progress_requires_new_packet_not_historical_plan_replay(tmp_path: Path) -> None:
    task, workspace, ledger, _, focus, _, current, _ = failed_then_current(tmp_path)
    original = packet(task, workspace, ledger, focus)
    assert original.contents()["checks"][current]["state"] == "not_requested"
    assert ledger.run(current).status == "pass"
    requests = list(ledger.executor.requests)
    before = tree_bytes(tmp_path)
    with pytest.raises(WorkingContextError, match="working_context_frozen_mismatch"):
        verify_working_context(original, workspace, task_directory=task, ledger=ledger, max_bytes=BUDGET)
    new_packet = packet(task, workspace, ledger, focus)
    assert new_packet.contents()["checks"][current]["result"]["status"] == "pass"
    assert new_packet.payload_sha256 != original.payload_sha256
    assert original.contents()["checks"][current]["state"] == "not_requested"
    assert tree_bytes(tmp_path) == before and ledger.executor.requests == requests


def test_selected_decision_preserves_full_revalidated_condition_scope(tmp_path: Path) -> None:
    task = task_snapshot(tmp_path)
    workspace, ledger, _, _, request, record, raw = condition_context(task, tmp_path / "run")
    decision = DecisionStore(workspace).decide(DecisionRequest.model_validate_json(json.dumps(raw)), ledger=ledger,
        features=FULL, capabilities=frozenset({"read_source"}))
    focus = WorkingContextFocus(diagnosis_id="diagnosis1", hypothesis_ids=("h1",), decision_id=decision.record_id)
    before = tree_bytes(tmp_path)
    result = packet(task, workspace, ledger, focus)
    value = result.contents()
    assert value["conditions"][record.result_id] == record.model_dump(mode="json")
    assert value["conditions"][record.result_id]["result"]["request"]["scope"] == request.scope.model_dump(mode="json")
    assert value["conditions"][record.result_id]["result"]["support_level"] == "synthetic"
    assert value["decisions"][decision.record_id]["condition_review"]["action"] == "reconsider_repair"
    assert value["application_verification"] == "not_run"
    assert tree_bytes(tmp_path) == before and ledger.executor.requests == []
    path = workspace.root / f"conditions/{record.result_id}.json"
    changed = json.loads(path.read_text(encoding="utf-8"))
    changed["result"]["request"]["scope"]["subject_id"] = "other"
    path.write_text(json.dumps(changed), encoding="utf-8")
    with pytest.raises(WorkingContextError, match="working_context_source_invalid"):
        verify_working_context(result, workspace, task_directory=task, ledger=ledger, max_bytes=BUDGET)


@pytest.mark.parametrize("fault", ["payload_hash", "payload_content", "budget", "foreign_ledger"])
def test_packet_or_verification_identity_drift_is_rejected(tmp_path: Path, fault: str) -> None:
    task, workspace, ledger, _, _ = registered(tmp_path)
    result = packet(task, workspace, ledger, WorkingContextFocus(diagnosis_id="diagnosis1"))
    budget = BUDGET
    if fault == "payload_hash":
        result = result.model_copy(update={"payload_sha256": "0" * 64})
    elif fault == "payload_content":
        value = result.contents()
        value["root_cause_confirmation"] = "confirmed"
        payload = canonical(value)
        result = result.model_copy(update={"canonical_payload_json": payload, "payload_sha256": sha256(payload)})
    elif fault == "budget":
        budget -= 1
    else:
        other = CandidateWorkspace(task, tmp_path / "other", "run1")
        ledger = CheckLedger(other, FixedTextExecutor("src/main.ets", "value=good\n"))
    with pytest.raises(WorkingContextError):
        verify_working_context(result, workspace, task_directory=task, ledger=ledger, max_bytes=budget)


def test_linked_record_rejected_without_following_or_modifying_it(tmp_path: Path) -> None:
    task, workspace, ledger, _, _ = registered(tmp_path)
    source = workspace.root / "diagnoses/diagnosis1.json"
    alias = workspace.root / "shared-record.copy"
    os.link(source, alias)
    before = tree_bytes(tmp_path)
    with pytest.raises(WorkingContextError, match="working_context_source_invalid"):
        packet(task, workspace, ledger, WorkingContextFocus(diagnosis_id="diagnosis1"))
    assert tree_bytes(tmp_path) == before


@pytest.mark.parametrize("raw", [{"hypothesis_ids": ("h1",)}, {"current_candidate_id": "0" * 64},
                                 {"diagnosis_id": "diagnosis1", "hypothesis_ids": ("h1", "h1")},
                                 {"diagnosis_id": "diagnosis1", "decision_id": "d1", "feedback_decision_ids": ("d1",)},
                                 {"diagnosis_id": "../outside"}])
def test_focus_rejects_missing_owner_duplicates_and_paths(raw) -> None:
    with pytest.raises(ValidationError):
        WorkingContextFocus.model_validate(raw)
