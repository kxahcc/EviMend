"""Host facts, causal hypotheses and obligations share version-bound references."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from test_agent_loop import response, task_snapshot

from dfx_agent.agent import _tools, run_fixed_agent
from dfx_agent.contracts import Diagnosis, Evidence, validate_references
from dfx_agent.diagnostics import AnalysisStore, DiagnosisSubmission
from dfx_agent.policies import PolicyFeatures
from dfx_agent.task import digest, snapshot_digest, verify_task
from dfx_agent.verification import CheckLedger, FixedTextExecutor
from dfx_agent.workspace import CandidateWorkspace, TextEdit

SOURCE = "const size = 3;\nconst buffer = new ArrayBuffer(size);\nconst view = new Uint16Array(buffer, 0, size);\n"
FULL = PolicyFeatures(obligation_tracking=True)


def causal_task(root: Path) -> Path:
    task = task_snapshot(root)
    (task / "source/src/main.ets").write_bytes(SOURCE.encode())
    manifest = [{"path": "src/main.ets", "sha256": digest(SOURCE.encode())}]
    (task / "source-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    metadata = json.loads((task / "task.json").read_text())
    metadata["source_snapshot_sha256"] = snapshot_digest(manifest)
    (task / "task.json").write_text(json.dumps(metadata), encoding="utf-8")
    return task


def submission(task: Path) -> dict:
    snapshot = verify_task(task)["source_snapshot_sha256"]

    def location(line: int) -> dict:
        return {"snapshot_sha256": snapshot, "path": "src/main.ets", "start_line": line, "end_line": line}

    def binding(identifier: str, expression: str, line: int, evidence: str) -> dict:
        return {"binding_id": identifier, "expression": expression, "role": "value", "location": location(line),
                "evidence_ids": [evidence]}

    identity = {"task_id": "task1", "run_id": "run1", "created_at": datetime.now(UTC).isoformat(),
                "source_snapshot_sha256": snapshot}
    bindings = [binding("capacity", "buffer.byteLength", 2, "allocate"),
                binding("extent", "size * Uint16Array.BYTES_PER_ELEMENT", 3, "consume")]
    return {"diagnosis": {**identity, "record_id": "causal-1", "exception_locations": [location(3)],
        "hypotheses": [{"hypothesis_id": "capacity-cause", "root_cause_locations": [location(2)],
            "support_status": "unknown", "support_evidence_ids": ["capacity-note"],
            "missing_evidence": ["Independent view-width semantics and downstream business behavior"],
            "constraint": {"family": "bounds", "operation": location(3), "bindings": bindings,
                "condition": "buffer.byteLength >= size * Uint16Array.BYTES_PER_ELEMENT",
                "semantic_source_evidence_ids": ["capacity-note"]},
            "causal_steps": [{"step_id": "allocation-to-consumption", "from_binding_id": "capacity",
                "to_binding_id": "extent", "relation": "Allocation may provide fewer bytes than required",
                "authority": "model_inference", "evidence_ids": ["capacity-note"]}],
            "obligation_ids": ["enough-capacity", "keep-elements"]}],
        "selected_hypothesis_id": "capacity-cause", "unknowns": ["No runtime confirmation"]},
        "source_citations": [{"evidence_id": "allocate", "location": location(2)},
                             {"evidence_id": "consume", "location": location(3)}],
        "log_citations": [{"evidence_id": "crash", "start_line": 1, "end_line": 1}],
        "inferences": [{"evidence_id": "capacity-note", "statement": "Capacity may use the wrong unit",
                        "basis_ids": ["allocate", "consume", "crash"]}],
        "obligations": [{**identity, "record_id": "enough-capacity", "kind": "runtime_condition",
            "statement": "Provide enough bytes for every intended element", "authority": "model_inference",
            "bindings": bindings, "source_evidence_ids": ["capacity-note"], "hypothesis_ids": ["capacity-cause"],
            "checkability": "executable", "scope": "view construction for non-empty input"},
            {**identity, "record_id": "keep-elements", "kind": "business_behavior",
            "statement": "Preserve the intended element sequence", "authority": "model_inference",
            "source_evidence_ids": ["consume"], "hypothesis_ids": ["capacity-cause"],
            "checkability": "unknown", "scope": "downstream consumers"}]}


def context(task: Path, root: Path):
    workspace = CandidateWorkspace(task, root / "run", "run1")
    store = AnalysisStore(workspace)
    ledger = CheckLedger(workspace, FixedTextExecutor("src/main.ets", "unmatched"))
    return workspace, store, ledger


def admit(store: AnalysisStore, ledger: CheckLedger, task: Path, raw: dict) -> dict:
    return store.admit(DiagnosisSubmission.model_validate_json(json.dumps(raw)), task=task,
                       log_path="crash.log", log_sha256=verify_task(task)["crash_sha256"],
                       ledger=ledger, allow_obligations=True)


def test_structured_sdk_diagnosis_drives_candidate_and_feedback(tmp_path: Path) -> None:
    task = causal_task(tmp_path)
    raw = submission(task)
    snapshot = CandidateWorkspace(task, tmp_path / "derive", "derive")
    snapshot.start_draft()
    snapshot.apply_text_edits([TextEdit(path="src/main.ets", old="ArrayBuffer(size)", new="ArrayBuffer(size * 2)")])
    candidate = snapshot.register_candidate(1).candidate_id
    report = run_fixed_agent(task, tmp_path / "run", "run1", [
        response(1, "record_diagnosis", {"diagnosis_json": json.dumps(raw)}),
        response(2, "edit_source", {"path": "src/main.ets", "old": "ArrayBuffer(size)", "new": "ArrayBuffer(size * 2)"}),
        response(3, "register_candidate", {"diagnosis_id": "causal-1", "restores": ["enough-capacity"],
                                             "preserves": ["keep-elements"]}),
        response(4, "run_check", {"candidate_id": candidate}),
        response(5, "link_failed_check", {"candidate_id": candidate, "violated_obligation_ids": ["keep-elements"]}),
        response(6),
    ], FixedTextExecutor("src/main.ets", "unmatched"), policy_features=FULL)
    assert report["rejected_tool_calls"] == 0 and report["checks"][0]["status"] == "fail"
    record = json.loads((tmp_path / "run/diagnoses/causal-1.json").read_text())
    assert record["diagnosis"]["exception_locations"][0]["start_line"] == 3
    assert record["diagnosis"]["hypotheses"][0]["root_cause_locations"][0]["start_line"] == 2
    facts = {item["record_id"]: item for item in record["evidence"]}
    assert facts["allocate"]["statement"] == SOURCE.splitlines()[1]
    assert facts["capacity-note"]["producer"] == "model"
    assert all(item["authority"] == "model_inference" and item["status"] == "unknown" for item in record["obligations"])
    candidate_record = json.loads((tmp_path / f"run/candidates/{candidate}/candidate.json").read_text())
    assert candidate_record["diagnosis_id"] == "causal-1" and candidate_record["preserves"] == ["keep-elements"]
    assert report["application_verification"] == "not_run"


@pytest.mark.parametrize("fault", ["snapshot", "run", "missing_basis", "missing_binding", "missing_obligation",
                                  "broken_link", "line", "authoritative_obligation", "confirmed_hypothesis",
                                  "source_relation", "execution_label", "evidence_cycle"])
def test_bad_diagnosis_is_rejected_without_partial_records(tmp_path: Path, fault: str) -> None:
    task = causal_task(tmp_path)
    workspace, store, ledger = context(task, tmp_path)
    raw = submission(task)
    hypothesis = raw["diagnosis"]["hypotheses"][0]
    if fault == "snapshot":
        raw["diagnosis"]["source_snapshot_sha256"] = digest(b"another snapshot")
    elif fault == "run":
        raw["diagnosis"]["run_id"] = "other"
    elif fault == "missing_basis":
        raw["inferences"][0]["basis_ids"] = ["missing"]
    elif fault == "missing_binding":
        hypothesis["causal_steps"][0]["from_binding_id"] = "missing"
    elif fault == "missing_obligation":
        raw["obligations"] = []
    elif fault == "broken_link":
        raw["obligations"][0]["hypothesis_ids"] = []
    elif fault == "line":
        raw["source_citations"][0]["location"]["end_line"] = 999
    elif fault == "authoritative_obligation":
        raw["obligations"][0]["authority"] = "verified_requirement"
    elif fault == "confirmed_hypothesis":
        hypothesis["support_status"] = "supported"
    elif fault == "source_relation":
        hypothesis["causal_steps"][0]["authority"] = "source_fact"
    elif fault == "execution_label":
        raw["inferences"][0]["kind"] = "application_execution"
    elif fault == "evidence_cycle":
        raw["inferences"][0]["basis_ids"] = ["second-note"]
        raw["inferences"].append({"evidence_id": "second-note", "statement": "Circular support", "basis_ids": ["capacity-note"]})
    with pytest.raises(ValueError):
        admit(store, ledger, task, raw)
    assert store.bundles() == store.obligations() == {}
    assert not workspace.draft.exists() and list(workspace.candidates.iterdir()) == []


def test_feedback_is_admitted_only_from_actual_matching_failed_candidate(tmp_path: Path) -> None:
    task = causal_task(tmp_path)
    workspace, store, ledger = context(task, tmp_path)
    workspace.start_draft()
    workspace.apply_text_edits([TextEdit(path="src/main.ets", old="ArrayBuffer(size)", new="ArrayBuffer(size * 2)")])
    first = workspace.register_candidate(1)
    result = ledger.run(first.candidate_id)
    raw = submission(task)
    raw["diagnosis"]["feedback_candidate_id"] = first.candidate_id
    raw["diagnosis"]["hypotheses"][0]["contradicting_evidence_ids"] = ["observed-failure"]
    raw["feedback_citations"] = [{"evidence_id": "observed-failure", "candidate_id": first.candidate_id,
        "candidate_snapshot_sha256": first.candidate_snapshot_sha256, "execution_id": result.idempotency_key}]
    value = admit(store, ledger, task, raw)
    fact = next(item for item in value["evidence"] if item["record_id"] == "observed-failure")
    assert fact["producer"] == "execution_backend" and fact["execution_scope"] == "synthetic"
    assert fact["execution_snapshot_sha256"] == first.candidate_snapshot_sha256
    assert value["diagnosis"]["hypotheses"][0]["support_status"] == "unknown"
    workspace.start_draft()
    workspace.apply_text_edits([TextEdit(path="src/main.ets", old="ArrayBuffer(size)", new="ArrayBuffer(size * 4)")])
    second = workspace.register_candidate(2)
    changed = json.loads(json.dumps(raw))
    changed["diagnosis"]["record_id"] = "causal-2"
    changed["diagnosis"]["feedback_candidate_id"] = second.candidate_id
    with pytest.raises(ValueError, match="another diagnosis candidate"):
        admit(store, ledger, task, changed)
    changed["diagnosis"]["feedback_candidate_id"] = first.candidate_id
    changed["feedback_citations"][0]["candidate_snapshot_sha256"] = second.candidate_snapshot_sha256
    with pytest.raises(ValueError, match="matching failed check"):
        admit(store, ledger, task, changed)
    changed["feedback_citations"] = []
    changed["existing_evidence_ids"] = ["observed-failure"]
    changed["diagnosis"]["feedback_candidate_id"] = second.candidate_id
    with pytest.raises(ValueError, match="another feedback candidate"):
        admit(store, ledger, task, changed)
    assert set(store.bundles()) == {"causal-1"}


def test_unknown_execution_and_obligation_disabled_do_not_create_facts(tmp_path: Path) -> None:
    task = causal_task(tmp_path)
    _, store, ledger = context(task, tmp_path)
    raw = submission(task)
    raw["diagnosis"]["feedback_candidate_id"] = digest(b"unregistered")
    raw["feedback_citations"] = [{"evidence_id": "fake-failure", "candidate_id": digest(b"unregistered"),
        "candidate_snapshot_sha256": digest(b"source"), "execution_id": "fake-execution"}]
    with pytest.raises(ValueError):
        admit(store, ledger, task, raw)
    raw = submission(task)
    with pytest.raises(ValueError, match="disabled"):
        store.admit(DiagnosisSubmission.model_validate_json(json.dumps(raw)), task=task, log_path="crash.log",
                    log_sha256=verify_task(task)["crash_sha256"], ledger=ledger, allow_obligations=False)
    assert store.bundles() == {}


def test_hypothesis_question_and_source_relation_references_are_checked(tmp_path: Path) -> None:
    task = causal_task(tmp_path)
    _, store, ledger = context(task, tmp_path)
    raw = submission(task)
    raw["diagnosis"]["hypotheses"][0]["distinguishing_check"] = {
        "question": "Which interpretation is required?", "observation_routes": {"words": "capacity-cause", "bytes": "absent"}}
    with pytest.raises(ValueError, match="absent hypothesis"):
        admit(store, ledger, task, raw)
    raw = submission(task)
    value = admit(store, ledger, task, raw)
    diagnosis = Diagnosis.model_validate_json(json.dumps(value["diagnosis"]))
    diagnosis.hypotheses[0].causal_steps[0].authority = "source_fact"
    facts = [Evidence.model_validate_json(json.dumps(item)) for item in value["evidence"]]
    with pytest.raises(ValueError, match="non-source evidence"):
        validate_references(diagnosis, facts, {"src/main.ets": 3})


def test_bundle_is_immutable_and_existing_fact_is_reused_without_promotion(tmp_path: Path) -> None:
    task = causal_task(tmp_path)
    _, store, ledger = context(task, tmp_path)
    raw = submission(task)
    first = admit(store, ledger, task, raw)
    before = (store.workspace.root / "diagnoses/causal-1.json").read_bytes()
    with pytest.raises(ValueError, match="already exists"):
        admit(store, ledger, task, raw)
    assert (store.workspace.root / "diagnoses/causal-1.json").read_bytes() == before
    raw["diagnosis"]["record_id"] = "causal-2"
    raw["diagnosis"]["hypotheses"][0].update(hypothesis_id="cause-2", obligation_ids=[])
    raw["diagnosis"]["selected_hypothesis_id"] = "cause-2"
    raw["source_citations"] = raw["log_citations"] = raw["inferences"] = raw["obligations"] = []
    raw["existing_evidence_ids"] = ["capacity-note"]
    second = admit(store, ledger, task, raw)
    assert {item["record_id"]: item for item in first["evidence"]} == {item["record_id"]: item for item in second["evidence"]}
    assert second["diagnosis"]["hypotheses"][0]["support_status"] == "unknown"


def test_schema_and_readback_expose_host_identity_and_same_saved_record(tmp_path: Path) -> None:
    task = causal_task(tmp_path)
    workspace, store, ledger = context(task, tmp_path)
    tools = {item.name: item for item in _tools(workspace, ledger, 3, task, "crash.log",
             verify_task(task)["crash_sha256"], FULL)}
    initial = json.loads(tools["inspect_analysis"].invoke({"include_schema": True}))
    assert initial["run_id"] == "run1" and initial["source_snapshot_sha256"] == workspace.source_snapshot_sha256
    assert "diagnosis" in initial["submission_schema"]["properties"] and initial["diagnosis_ids"] == []
    admitted = admit(store, ledger, task, submission(task))
    readback = json.loads(tools["inspect_analysis"].invoke({"diagnosis_id": "causal-1"}))
    assert readback["record"] == admitted and "submission_schema" not in readback


def test_missing_check_result_and_changed_log_are_not_admitted(tmp_path: Path) -> None:
    task = causal_task(tmp_path)
    workspace, store, ledger = context(task, tmp_path)
    workspace.start_draft()
    workspace.apply_text_edits([TextEdit(path="src/main.ets", old="ArrayBuffer(size)", new="ArrayBuffer(size * 2)")])
    candidate = workspace.register_candidate(1)
    raw = submission(task)
    raw["diagnosis"]["feedback_candidate_id"] = candidate.candidate_id
    raw["feedback_citations"] = [{"evidence_id": "not-run", "candidate_id": candidate.candidate_id,
        "candidate_snapshot_sha256": candidate.candidate_snapshot_sha256, "execution_id": "claimed-failure"}]
    with pytest.raises(ValueError, match="matching failed check"):
        admit(store, ledger, task, raw)
    raw = submission(task)
    log_hash = verify_task(task)["crash_sha256"]
    (task / "crash.log").write_text("changed", encoding="utf-8")
    with pytest.raises(ValueError, match="input log changed"):
        store.admit(DiagnosisSubmission.model_validate_json(json.dumps(raw)), task=task, log_path="crash.log",
                    log_sha256=log_hash, ledger=ledger, allow_obligations=True)
    assert store.bundles() == {}


@pytest.mark.parametrize("fault", ["wrong_run", "forged_execution"])
def test_invalid_structured_sdk_submission_has_no_analysis_effects(tmp_path: Path, fault: str) -> None:
    task = causal_task(tmp_path)
    raw = submission(task)
    if fault == "wrong_run":
        raw["diagnosis"]["run_id"] = "another"
    else:
        raw["inferences"][0].update(kind="application_execution", execution_id="claimed")
    executor = FixedTextExecutor("src/main.ets", "unmatched")
    report = run_fixed_agent(task, tmp_path / "run", "run1", [
        response(1, "record_diagnosis", {"diagnosis_json": json.dumps(raw)}), response(2),
    ], executor, policy_features=FULL)
    workspace = CandidateWorkspace.reopen(task, tmp_path / "run", "run1")
    assert AnalysisStore(workspace).bundles() == {} and not workspace.draft.exists()
    assert report["candidates"] == report["checks"] == [] and executor.requests == []
    assert report["application_verification"] == "not_run"
