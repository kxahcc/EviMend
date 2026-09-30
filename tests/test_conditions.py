"""Boundary cases and provenance counterexamples for finite condition calculations."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError
from test_agent_loop import task_snapshot

from dfx_agent.conditions import (
    ConditionEvaluator,
    ConditionRequest,
    ConditionScope,
    ObservationBatch,
    ObservedValue,
    SyntheticObservations,
)
from dfx_agent.contracts import (
    Diagnosis,
    Evidence,
    Hypothesis,
    RuntimeConstraint,
    SourceLocation,
    ValueBinding,
)
from dfx_agent.diagnostics import AnalysisStore
from dfx_agent.task import digest
from dfx_agent.workspace import CandidateWorkspace, TextEdit

UNITS = {"index": "elements", "length": "elements", "capacity": "bytes", "offset": "bytes",
         "width": "bytes_per_element", "count": "elements", "state": "state"}


def setup(tmp_path: Path, kind: str = "typed_array_view", *, mode: str = "explicit"):
    task = task_snapshot(tmp_path)
    workspace = CandidateWorkspace(task, tmp_path / "run", "run1")
    identity = {"task_id": workspace.task_id, "run_id": workspace.run_id,
                "source_snapshot_sha256": workspace.source_snapshot_sha256, "created_at": datetime.now(UTC)}
    location = SourceLocation(snapshot_sha256=workspace.source_snapshot_sha256, path="src/main.ets", start_line=1, end_line=1)
    fact = Evidence(**identity, record_id="source1", kind="source_fact", producer="source_reader",
                    artifact_sha256=digest(b"value=bad\n"), statement="Synthetic source", location=location)
    roles = {"array_index": ["index", "length"], "data_view_access": ["capacity", "offset", "width"],
             "typed_array_view": ["capacity", "offset", "width", "count"], "ready_at_use": ["state"]}[kind]
    if mode == "remaining":
        roles.remove("count")
    bindings = [ValueBinding(binding_id=role, expression="Untrusted expression: " + role,
                             role="state" if role == "state" else "value", evidence_ids=["source1"]) for role in roles]
    constraint = RuntimeConstraint(family="lifecycle" if kind == "ready_at_use" else "bounds", operation=location,
        bindings=bindings, condition="This text must never be evaluated", semantic_source_evidence_ids=["source1"])
    diagnosis = Diagnosis(**identity, record_id="diagnosis1", hypotheses=[Hypothesis(hypothesis_id="h1",
                          support_status="unknown", constraint=constraint, support_evidence_ids=["source1"])])
    AnalysisStore(workspace).put_bundle(diagnosis, [fact], [])
    scope = ConditionScope(task_id=workspace.task_id, run_id=workspace.run_id,
                          source_snapshot_sha256=workspace.source_snapshot_sha256,
                          evaluated_snapshot_sha256=workspace.source_snapshot_sha256, operation=location,
                          scenario_id="first_open", path_id="main_path", subject_id="buffer1", sample_id="at_use1")
    request = ConditionRequest.model_validate({"check_id": "condition1", "diagnosis_id": "diagnosis1",
        "hypothesis_id": "h1", "scope": scope, "kind": kind, "length_mode": mode,
        "binding_ids": {role: role for role in roles}})
    return workspace, request


def observations(request: ConditionRequest, values: dict, **changes) -> ObservationBatch:
    raw = {"observation_id": "fixture1", "scope": request.scope, "origin": "synthetic_fixture",
           "normalized_integers": True, "fixed_attached_buffer": True, "state_at_use": True,
           "values": [ObservedValue(binding_id=key, unit=UNITS[key], value=value) for key, value in values.items()], **changes}
    return ObservationBatch.model_validate(raw)


def evaluate(workspace: CandidateWorkspace, request: ConditionRequest, batch: ObservationBatch):
    return ConditionEvaluator(workspace, SyntheticObservations([batch])).evaluate(request, batch)


@pytest.mark.parametrize("kind,mode,values,expected", [
    ("array_index", "explicit", {"index": 0, "length": 0}, "violated"),
    ("array_index", "explicit", {"index": -1, "length": 3}, "violated"),
    ("array_index", "explicit", {"index": 2, "length": 3}, "satisfied"),
    ("array_index", "explicit", {"index": 3, "length": 3}, "violated"),
    ("data_view_access", "explicit", {"capacity": 5, "offset": 1, "width": 4}, "satisfied"),
    ("data_view_access", "explicit", {"capacity": 5, "offset": 2, "width": 4}, "violated"),
    ("data_view_access", "explicit", {"capacity": 0, "offset": 0, "width": 1}, "violated"),
    ("typed_array_view", "explicit", {"capacity": 3, "offset": 0, "width": 2, "count": 3}, "violated"),
    ("typed_array_view", "explicit", {"capacity": 6, "offset": 0, "width": 2, "count": 3}, "satisfied"),
    ("typed_array_view", "explicit", {"capacity": 6, "offset": 1, "width": 2, "count": 2}, "violated"),
    ("typed_array_view", "explicit", {"capacity": 6, "offset": 6, "width": 2, "count": 0}, "satisfied"),
    ("typed_array_view", "explicit", {"capacity": 6, "offset": 8, "width": 2, "count": 0}, "violated"),
    ("typed_array_view", "explicit", {"capacity": 5, "offset": 0, "width": 2, "count": 2}, "satisfied"),
    ("typed_array_view", "remaining", {"capacity": 5, "offset": 0, "width": 2}, "violated"),
    ("typed_array_view", "remaining", {"capacity": 6, "offset": 2, "width": 2}, "satisfied"),
    ("typed_array_view", "remaining", {"capacity": 0, "offset": 0, "width": 2}, "satisfied"),
    ("typed_array_view", "remaining", {"capacity": 6, "offset": 8, "width": 2}, "violated"),
    ("ready_at_use", "explicit", {"state": "ready"}, "satisfied"),
    ("ready_at_use", "explicit", {"state": "uninitialized"}, "violated"),
    ("ready_at_use", "explicit", {"state": "released"}, "violated"),
])
def test_boundary_predicates_report_only_scoped_synthetic_findings(tmp_path: Path, kind, mode, values, expected) -> None:
    workspace, request = setup(tmp_path, kind, mode=mode)
    result = evaluate(workspace, request, observations(request, values))
    assert result.status == expected and result.support_level == "synthetic" and result.findings
    assert result.application_verification == "not_run" and result.root_cause_confirmation == "unknown"
    assert result.request.scope == request.scope and result.observation_sha256
    assert AnalysisStore(workspace).bundles()["diagnosis1"]["diagnosis"].hypotheses[0].support_status == "unknown"
    assert not workspace.draft.exists() and list(workspace.candidates.iterdir()) == []


@pytest.mark.parametrize("fault,reason", [
    ("missing", "missing_value:count"), ("null", "missing_value:count"), ("unit", "unit_mismatch:count"),
    ("boolean", "outside_integer_profile:count"), ("expression", "outside_integer_profile:count"),
    ("large", "outside_integer_profile:count"), ("negative", "outside_integer_profile:offset"),
    ("zero_width", "unsupported_element_width"), ("other_width", "unsupported_element_width"),
    ("not_normalized", "integer_normalization_unconfirmed"), ("detached_or_resizing", "fixed_attached_buffer_unconfirmed"),
])
def test_unknown_inputs_do_not_become_success_or_execute_expressions(tmp_path: Path, monkeypatch, fault, reason) -> None:
    workspace, request = setup(tmp_path)
    monkeypatch.chdir(tmp_path)
    sentinel = tmp_path / "must-not-exist"
    values = {"capacity": 8, "offset": 0, "width": 2, "count": 4}
    changes = {}
    if fault == "missing":
        values.pop("count")
    elif fault == "null":
        values["count"] = None
    elif fault == "unit":
        pass
    elif fault in {"boolean", "expression", "large"}:
        values["count"] = {"boolean": True, "expression": "open('must-not-exist', 'w')", "large": 2**53}[fault]
    elif fault == "negative":
        values["offset"] = -1
    elif fault in {"zero_width", "other_width"}:
        values["width"] = 0 if fault == "zero_width" else 3
    elif fault == "not_normalized":
        changes["normalized_integers"] = None
    else:
        changes["fixed_attached_buffer"] = False
    batch = observations(request, values, **changes)
    if fault == "unit":
        next(item for item in batch.values if item.binding_id == "count").unit = "bytes"
    result = evaluate(workspace, request, batch)
    assert result.status == "unknown" and result.unknown_reasons == [reason] and result.findings == []
    assert not sentinel.exists()


@pytest.mark.parametrize("field,new_value", [("task_id", "other"), ("run_id", "other"),
    ("scenario_id", "reopen"), ("path_id", "async_path"), ("subject_id", "another_buffer"), ("sample_id", "after_use")])
def test_values_from_other_owner_path_object_or_point_stay_unknown(tmp_path: Path, field, new_value) -> None:
    workspace, request = setup(tmp_path)
    batch = observations(request, {"capacity": 8, "offset": 0, "width": 2, "count": 4})
    raw = batch.model_dump()
    raw["scope"][field] = new_value
    other = ObservationBatch.model_validate(raw)
    result = evaluate(workspace, request, other)
    assert result.status == "unknown" and result.unknown_reasons == ["observation_scope_mismatch"]


def test_model_labels_and_changed_fixture_values_cannot_grant_provenance(tmp_path: Path) -> None:
    workspace, request = setup(tmp_path)
    host = observations(request, {"capacity": 3, "offset": 0, "width": 2, "count": 3})
    evaluator = ConditionEvaluator(workspace, SyntheticObservations([host]))
    assert evaluator.evaluate(request, None).unknown_reasons == ["missing_observations"]
    raw = host.model_dump()
    raw["origin"] = "model_inference"
    model = ObservationBatch.model_validate(raw)
    assert evaluator.evaluate(request, model).unknown_reasons == ["observations_not_host_pinned"]
    model.origin = "synthetic_fixture"
    next(item for item in model.values if item.binding_id == "capacity").value = 6
    assert evaluator.evaluate(request, model).status == "unknown"
    # Registry copied the host input; mutating that same object cannot rewrite admission.
    next(item for item in host.values if item.binding_id == "capacity").value = 6
    assert evaluator.evaluate(request, host).status == "unknown"
    with pytest.raises(ValueError):
        SyntheticObservations([ObservationBatch.model_validate({**raw, "origin": "model_inference"})])
    with pytest.raises(ValueError):
        SyntheticObservations([model, model])
    with pytest.raises(ValidationError):
        ObservationBatch.model_validate({**raw, "origin": "application_execution"})


def test_readiness_must_be_at_use_and_does_not_prove_other_paths(tmp_path: Path) -> None:
    workspace, request = setup(tmp_path, "ready_at_use")
    for values, changes, reason in [({"state": "ready"}, {"state_at_use": False}, "state_not_captured_at_use"),
                                  ({"state": "unknown"}, {}, "unsupported_state")]:
        result = evaluate(workspace, request, observations(request, values, **changes))
        assert result.status == "unknown" and result.unknown_reasons == [reason]
    ready = observations(request, {"state": "ready"})
    evaluator = ConditionEvaluator(workspace, SyntheticObservations([ready]))
    raw = request.model_dump()
    raw["scope"]["path_id"] = "other_path"
    assert evaluator.evaluate(ConditionRequest.model_validate(raw), ready).status == "unknown"


def test_candidate_values_cannot_migrate_to_another_or_changed_candidate(tmp_path: Path) -> None:
    workspace, original = setup(tmp_path)
    old_values = observations(original, {"capacity": 8, "offset": 0, "width": 2, "count": 4})
    workspace.start_draft()
    workspace.apply_text_edits([TextEdit(path="src/main.ets", old="bad", new="good")])
    candidate = workspace.register_candidate(1, "diagnosis1")
    raw = original.model_dump()
    raw["scope"].update(candidate_id=candidate.candidate_id, evaluated_snapshot_sha256=candidate.candidate_snapshot_sha256)
    raw["scope"]["operation"]["snapshot_sha256"] = candidate.candidate_snapshot_sha256
    current = ConditionRequest.model_validate(raw)
    assert evaluate(workspace, current, old_values).status == "unknown"
    current_values = observations(current, {"capacity": 8, "offset": 0, "width": 2, "count": 4})
    assert evaluate(workspace, current, current_values).status == "satisfied"
    workspace.start_draft()
    workspace.apply_text_edits([TextEdit(path="src/main.ets", old="bad", new="another")])
    another = workspace.register_candidate(2, "diagnosis1")
    another_raw = current.model_dump()
    another_raw["scope"].update(candidate_id=another.candidate_id, evaluated_snapshot_sha256=another.candidate_snapshot_sha256)
    another_raw["scope"]["operation"]["snapshot_sha256"] = another.candidate_snapshot_sha256
    assert evaluate(workspace, ConditionRequest.model_validate(another_raw), current_values).status == "unknown"
    raw["scope"]["evaluated_snapshot_sha256"] = digest(b"stale")
    raw["scope"]["operation"]["snapshot_sha256"] = digest(b"stale")
    with pytest.raises(ValueError, match="stale candidate"):
        evaluate(workspace, ConditionRequest.model_validate(raw), current_values)
    frozen = workspace.require_registered(candidate.candidate_id) / "src/main.ets"
    frozen.write_text("tampered\n", encoding="utf-8")
    with pytest.raises(ValueError):
        evaluate(workspace, current, current_values)


@pytest.mark.parametrize("fault", ["diagnosis", "hypothesis", "binding", "family", "operation", "task", "run", "snapshot"])
def test_invalid_requests_are_rejected_without_creating_results(tmp_path: Path, fault) -> None:
    workspace, request = setup(tmp_path)
    raw = request.model_dump()
    if fault in {"diagnosis", "hypothesis"}:
        raw[f"{fault}_id"] = "missing"
    elif fault == "binding":
        raw["binding_ids"]["count"] = "missing"
    elif fault == "family":
        raw.update(kind="ready_at_use", binding_ids={"state": "capacity"})
    elif fault in {"task", "run"}:
        raw["scope"][f"{fault}_id"] = "other"
    elif fault == "snapshot":
        raw["scope"].update(source_snapshot_sha256=digest(b"other"), evaluated_snapshot_sha256=digest(b"other"))
        raw["scope"]["operation"]["snapshot_sha256"] = digest(b"other")
    else:
        raw["scope"]["operation"]["end_line"] = 2
    with pytest.raises(ValueError):
        ConditionEvaluator(workspace, SyntheticObservations([])).evaluate(ConditionRequest.model_validate(raw), None)
    assert not workspace.draft.exists()


def test_unsupported_or_incomplete_condition_schema_is_rejected(tmp_path: Path) -> None:
    _, request = setup(tmp_path)
    for changes in [{"kind": "dynamic_pattern"}, {"binding_ids": {"capacity": "capacity"}},
                    {"profile": "full_ArkTS_validator"}, {"kind": "data_view_access", "length_mode": "remaining"}]:
        with pytest.raises(ValidationError):
            ConditionRequest.model_validate({**request.model_dump(), **changes})
    batch = observations(request, {"capacity": 6})
    with pytest.raises(ValidationError):
        ObservationBatch.model_validate({**batch.model_dump(), "values": batch.values + batch.values})
    assert ConditionRequest.model_validate_json(request.model_dump_json()) == request


def test_changed_original_bytes_cannot_use_an_old_version_label(tmp_path: Path) -> None:
    workspace, request = setup(tmp_path)
    batch = observations(request, {"capacity": 6, "offset": 0, "width": 2, "count": 3})
    (workspace.base / "src/main.ets").write_text("changed\n", encoding="utf-8")
    with pytest.raises(ValueError, match="analysis snapshot changed"):
        evaluate(workspace, request, batch)


def test_only_calculation_changes_when_host_values_restore_capacity(tmp_path: Path) -> None:
    workspace, request = setup(tmp_path)
    bad = observations(request, {"capacity": 3, "offset": 0, "width": 2, "count": 3})
    good = observations(request, {"capacity": 6, "offset": 0, "width": 2, "count": 3}, observation_id="fixture2")
    evaluator = ConditionEvaluator(workspace, SyntheticObservations([bad, good]))
    before, after = evaluator.evaluate(request, bad), evaluator.evaluate(request, good)
    assert (before.status, after.status) == ("violated", "satisfied")
    assert before.request_sha256 == after.request_sha256 and before.observation_sha256 != after.observation_sha256
    assert before.application_verification == after.application_verification == "not_run"
    assert not (workspace.root / "checks").exists()
    # Result is a programmatic calculation, not a synthetic CheckLedger pass or a changed diagnosis.
    assert json.loads(after.model_dump_json())["root_cause_confirmation"] == "unknown"
