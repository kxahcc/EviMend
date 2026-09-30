"""Immutable synthetic condition inputs and results must survive readback safely."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from test_conditions import observations, setup

from dfx_agent.conditions import (
    ConditionRequest,
    ConditionResult,
    ConditionStore,
    SyntheticObservations,
)
from dfx_agent.task import digest
from dfx_agent.workspace import CandidateWorkspace, TextEdit

VALUES = {"capacity": 3, "offset": 0, "width": 2, "count": 3}


def store_context(tmp_path: Path):
    workspace, request = setup(tmp_path)
    batch = observations(request, VALUES)
    store = ConditionStore(workspace, SyntheticObservations([batch]))
    return workspace, request, batch, store


def test_registry_accessors_copy_values_and_use_stable_order(tmp_path: Path) -> None:
    _, request = setup(tmp_path)
    first = observations(request, VALUES)
    second = observations(request, {**VALUES, "capacity": 6}, observation_id="fixture2")
    registry = SyntheticObservations([second, first])
    fingerprint = registry.fingerprint()
    assert SyntheticObservations([first, second]).fingerprint() == fingerprint
    assert [item["observation_id"] for item in registry.value()] == ["fixture1", "fixture2"]
    assert all("values" not in item for item in registry.descriptions())
    copy = registry.get("fixture1")
    copy.values[0].value = 99
    raw = registry.value()
    raw[0]["values"][0]["value"] = 99
    first.values[0].value = 99
    assert registry.fingerprint() == fingerprint
    assert registry.get("fixture1").values[0].value == 3
    with pytest.raises(ValueError, match="not registered"):
        registry.get("absent")


def test_store_is_deterministic_and_reopens_from_saved_host_inputs(tmp_path: Path) -> None:
    workspace, request, _, store = store_context(tmp_path)
    first = store.evaluate(request, "fixture1")
    assert store.evaluate(request, "fixture1") == first
    assert first.result.status == "violated" and first.result.support_level == "synthetic"
    assert first.result.applicability_authority == "model_inference"
    assert first.result.root_cause_confirmation == "unknown" and first.result.application_verification == "not_run"
    reopened_workspace = CandidateWorkspace.reopen(tmp_path / "input", workspace.root, "run1")
    reopened = ConditionStore(reopened_workspace)
    assert reopened.observations_sha256 == store.observations_sha256
    assert reopened.evaluate(request, "fixture1") == first
    assert reopened.records() == {first.result_id: first}
    assert not (workspace.root / "checks").exists() and not workspace.draft.exists()


def test_distinct_pinned_samples_keep_distinct_results_without_overwrite(tmp_path: Path) -> None:
    workspace, request = setup(tmp_path)
    bad = observations(request, VALUES)
    good = observations(request, {**VALUES, "capacity": 6}, observation_id="fixture2")
    store = ConditionStore(workspace, SyntheticObservations([bad, good]))
    before, after = store.evaluate(request, "fixture1"), store.evaluate(request, "fixture2")
    assert before.result.status == "violated" and after.result.status == "satisfied"
    assert before.result_id != after.result_id and len(store.records()) == 2
    after.result.status = "violated"
    assert store.records()[after.result_id].result.status == "satisfied"


def test_missing_inputs_and_missing_observation_have_different_meanings(tmp_path: Path) -> None:
    workspace, request = setup(tmp_path)
    absent = ConditionStore(workspace)
    assert absent.records() == {}
    with pytest.raises(ValueError, match="registry is missing"):
        absent.evaluate(request)
    store = ConditionStore(workspace, SyntheticObservations([]))
    assert store.evaluate(request).result.unknown_reasons == ["missing_observations"]
    with pytest.raises(ValueError, match="not registered"):
        store.evaluate(request, "invented")


def test_host_pinned_scope_mismatch_stays_unknown_when_saved(tmp_path: Path) -> None:
    workspace, request = setup(tmp_path)
    batch = observations(request, VALUES).model_copy(deep=True)
    batch.scope.path_id = "other_path"
    store = ConditionStore(workspace, SyntheticObservations([batch]))
    result = store.evaluate(request, "fixture1")
    assert result.result.unknown_reasons == ["observation_scope_mismatch"]
    assert ConditionStore(workspace).records()[result.result_id] == result


def test_registry_cannot_be_replaced_or_mutated_during_readback(tmp_path: Path) -> None:
    workspace, request, batch, store = store_context(tmp_path)
    first = store.evaluate(request, "fixture1")
    batch.values[0].value = 6
    with pytest.raises(ValueError, match="inputs changed"):
        ConditionStore(workspace, SyntheticObservations([batch]))
    assert store.records()[first.result_id] == first
    path = workspace.root / "condition-inputs.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["observations"][0]["values"][0]["value"] = 6
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="inputs changed"):
        store.records()
    with pytest.raises(ValueError, match="input registry"):
        ConditionStore(workspace).records()


@pytest.mark.parametrize("field", ["task_id", "run_id", "source_snapshot_sha256"])
def test_registry_owner_cannot_be_changed(tmp_path: Path, field: str) -> None:
    workspace, _, _, _ = store_context(tmp_path)
    path = workspace.root / "condition-inputs.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw[field] = digest(b"another") if field == "source_snapshot_sha256" else "other"
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="another task"):
        ConditionStore(workspace)


@pytest.mark.parametrize("fault", ["status", "operands", "observation_hash", "request_hash", "authority",
                                  "root_cause", "application"])
def test_result_content_cannot_be_rewritten(tmp_path: Path, fault: str) -> None:
    workspace, request, _, store = store_context(tmp_path)
    record = store.evaluate(request, "fixture1")
    path = workspace.root / "conditions" / f"{record.result_id}.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    result = raw["result"]
    if fault == "status":
        result["status"] = "satisfied"
    elif fault == "operands":
        result["findings"][0]["operands"]["offset"] = 1
    elif fault == "observation_hash":
        result["observation_sha256"] = digest(b"another")
    elif fault == "request_hash":
        result["request_sha256"] = digest(b"another")
    elif fault == "authority":
        result["applicability_authority"] = "verified_check"
    elif fault == "root_cause":
        result["root_cause_confirmation"] = "supported"
    else:
        result["application_verification"] = "passed"
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValueError):
        ConditionStore(workspace).records()


def test_recomputed_content_address_does_not_hide_forged_predicate(tmp_path: Path) -> None:
    workspace, request, _, store = store_context(tmp_path)
    record = store.evaluate(request, "fixture1")
    path = workspace.root / "conditions" / f"{record.result_id}.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["result"]["status"] = "satisfied"
    for finding in raw["result"]["findings"]:
        finding["status"] = "satisfied"
    # A coherent object and refreshed content address still need host recomputation.
    ConditionResult.model_validate_json(json.dumps(raw["result"]))
    semantic = {key: raw[key] for key in ("result", "observations_sha256", "diagnosis_sha256")}
    raw["result_id"] = "condition-" + digest(json.dumps(semantic, sort_keys=True, separators=(",", ":"),
                                                       ensure_ascii=False).encode())
    path.unlink()
    (path.parent / f"{raw['result_id']}.json").write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="calculation result changed"):
        ConditionStore(workspace).records()


def test_changed_diagnosis_or_source_cannot_reuse_saved_calculation(tmp_path: Path) -> None:
    workspace, request, _, store = store_context(tmp_path)
    record = store.evaluate(request, "fixture1")
    diagnosis_path = workspace.root / "diagnoses/diagnosis1.json"
    raw = json.loads(diagnosis_path.read_text(encoding="utf-8"))
    raw["diagnosis"]["created_at"] = "2026-10-02T00:00:00Z"
    diagnosis_path.write_text(json.dumps(raw), encoding="utf-8")
    assert store.records()[record.result_id] == record
    raw["diagnosis"]["hypotheses"][0]["constraint"]["condition"] = "Another proposed meaning"
    diagnosis_path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="diagnosis changed"):
        store.records()
    raw["diagnosis"]["hypotheses"][0]["constraint"]["condition"] = "This text must never be evaluated"
    diagnosis_path.write_text(json.dumps(raw), encoding="utf-8")
    (workspace.base / "src/main.ets").write_text("changed\n", encoding="utf-8")
    with pytest.raises(ValueError, match="analysis snapshot changed"):
        store.records()


def test_saved_result_requires_its_input_file(tmp_path: Path) -> None:
    workspace, request, _, store = store_context(tmp_path)
    store.evaluate(request, "fixture1")
    (workspace.root / "condition-inputs.json").unlink()
    with pytest.raises(ValueError, match="registry is missing"):
        ConditionStore(workspace).records()


def test_saved_candidate_calculation_rechecks_frozen_source_bytes(tmp_path: Path) -> None:
    workspace, original = setup(tmp_path)
    workspace.start_draft()
    workspace.apply_text_edits([TextEdit(path="src/main.ets", old="bad", new="good")])
    candidate = workspace.register_candidate(1, "diagnosis1")
    raw = original.model_dump(mode="json")
    raw["scope"].update(candidate_id=candidate.candidate_id,
                        evaluated_snapshot_sha256=candidate.candidate_snapshot_sha256)
    raw["scope"]["operation"]["snapshot_sha256"] = candidate.candidate_snapshot_sha256
    request = ConditionRequest.model_validate_json(json.dumps(raw))
    store = ConditionStore(workspace, SyntheticObservations([observations(request, VALUES)]))
    record = store.evaluate(request, "fixture1")
    assert store.records()[record.result_id] == record
    frozen = workspace.require_registered(candidate.candidate_id) / "src/main.ets"
    frozen.write_text("tampered\n", encoding="utf-8")
    with pytest.raises(ValueError):
        ConditionStore(workspace).records()


@pytest.mark.parametrize("target", ["condition-inputs.json", "conditions"])
def test_linked_condition_paths_are_rejected(tmp_path: Path, target: str) -> None:
    workspace, request, _, store = store_context(tmp_path)
    record = store.evaluate(request, "fixture1")
    if target == "condition-inputs.json":
        path = workspace.root / target
        outside = tmp_path / "outside-input.json"
        outside.write_bytes(path.read_bytes())
        path.unlink()
        os.link(outside, path)
        with pytest.raises(ValueError, match="linked"):
            ConditionStore(workspace)
        assert outside.read_bytes()
    else:
        path = workspace.root / target / f"{record.result_id}.json"
        outside = tmp_path / "outside-result.json"
        outside.write_bytes(path.read_bytes())
        path.unlink()
        os.link(outside, path)
        with pytest.raises(ValueError, match="linked"):
            store.records()


def test_mutated_request_is_revalidated_without_writing_a_result(tmp_path: Path) -> None:
    workspace, request, _, store = store_context(tmp_path)
    request.binding_ids["count"] = "not a legal identifier"
    with pytest.raises(ValueError):
        store.evaluate(request, "fixture1")
    assert not (workspace.root / "conditions").exists()
