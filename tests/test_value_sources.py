"""Synthetic host receipts preserve exact operands, source versions and event scope."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import pytest
from pydantic import ValidationError
from test_agent_loop import task_snapshot

from dfx_agent.conditions import ConditionKind
from dfx_agent.contracts import (
    Diagnosis,
    Evidence,
    Hypothesis,
    RuntimeConstraint,
    SourceLocation,
    ValueBinding,
)
from dfx_agent.diagnostics import AnalysisStore
from dfx_agent.task import digest, snapshot_digest
from dfx_agent.value_sources import (
    BindingAssessment,
    BindingProposal,
    BoundOperand,
    CapturedValue,
    CaptureIntent,
    CaptureReceipt,
    CaptureRequest,
    SemanticProfile,
    SourceAnchor,
    SyntheticBindingFixture,
    SyntheticCaptureFixture,
    SyntheticEvent,
    SyntheticValueAdapter,
    ValueSourceManifest,
    ValueSourceStore,
    content_sha256,
)
from dfx_agent.workspace import CandidateWorkspace, TextEdit

SOURCE = (
    'const note = "中文，不执行";\r\n'
    'const flag = "ready";\r\n'
    'const view = new Uint16Array(buffer, 8, 0); const other = new Uint16Array(second, 4, 1);\r\n'
    'consume(flag);\r\n'
).encode()
OPERATION = b"new Uint16Array(buffer, 8, 0)"
UNITS = {"capacity": "bytes", "offset": "bytes", "width": "bytes_per_element", "count": "elements",
         "index": "elements", "length": "elements", "state": "state"}
TOKENS = {"capacity": b"buffer", "offset": b"8", "width": b"Uint16Array", "count": b"0",
          "index": b"8", "length": b"0", "state": b"buffer"}
VALUES = {"capacity": 4, "offset": 8, "width": 2, "count": 0, "index": 0, "length": 3, "state": "ready"}
ALL_KINDS: list[ConditionKind] = ["array_index", "data_view_access", "typed_array_view", "ready_at_use"]


@dataclass
class HostContext:
    task: Path
    workspace: CandidateWorkspace
    proposal: BindingProposal
    fixture: SyntheticBindingFixture
    manifest: ValueSourceManifest
    adapter: SyntheticValueAdapter
    store: ValueSourceStore


def make_context(tmp_path: Path, kind: ConditionKind = "typed_array_view",
                 mode: Literal["explicit", "remaining"] = "explicit") -> HostContext:
    task = task_snapshot(tmp_path)
    (task / "source/src/main.ets").write_bytes(SOURCE)
    manifest = [{"path": "src/main.ets", "sha256": digest(SOURCE)}]
    (task / "source-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    metadata = json.loads((task / "task.json").read_text(encoding="utf-8"))
    metadata["source_snapshot_sha256"] = snapshot_digest(manifest)
    (task / "task.json").write_text(json.dumps(metadata), encoding="utf-8")
    workspace = CandidateWorkspace(task, tmp_path / "run", "run1")
    snapshot = workspace.source_snapshot_sha256
    location = SourceLocation(snapshot_sha256=snapshot, path="src/main.ets", start_line=3, end_line=3)
    identity = {"task_id": workspace.task_id, "run_id": workspace.run_id,
                "source_snapshot_sha256": snapshot, "created_at": datetime.now(UTC)}
    fact = Evidence(**identity, record_id="source1", kind="source_fact", producer="source_reader",
                    artifact_sha256=digest(SOURCE), statement="Synthetic source fixture", location=location)
    roles = {"array_index": ["index", "length"], "data_view_access": ["capacity", "offset", "width"],
             "typed_array_view": ["capacity", "offset", "width", "count"], "ready_at_use": ["state"]}[kind]
    if mode == "remaining":
        roles.remove("count")
    constraint = RuntimeConstraint(family="lifecycle" if kind == "ready_at_use" else "bounds", operation=location,
        bindings=[ValueBinding(binding_id=role, expression="Do not evaluate " + role,
                               role="state" if role == "state" else "value", evidence_ids=["source1"])
                  for role in roles], condition="Untrusted text, never executable", semantic_source_evidence_ids=["source1"])
    diagnosis = Diagnosis(**identity, record_id="diagnosis1", hypotheses=[Hypothesis(hypothesis_id="h1",
        support_status="unknown", support_evidence_ids=["source1"], constraint=constraint)])
    AnalysisStore(workspace).put_bundle(diagnosis, [fact], [])
    start = SOURCE.index(OPERATION)
    operation = SourceAnchor.from_bytes(snapshot, "src/main.ets", SOURCE, start, start + len(OPERATION))
    proposal = BindingProposal(task_id=workspace.task_id, run_id=workspace.run_id,
        source_snapshot_sha256=snapshot, evaluated_snapshot_sha256=snapshot,
        diagnosis_id="diagnosis1", hypothesis_id="h1", kind=kind, length_mode=mode,
        operation=operation, diagnostic_binding_ids=roles)
    operands = {}
    for role in roles:
        offset = start + OPERATION.index(TOKENS[role])
        anchor = SourceAnchor.from_bytes(snapshot, "src/main.ets", SOURCE, offset,
                                         offset + len(TOKENS[role]), operation)
        operands[role] = BoundOperand.model_validate({"diagnostic_binding_id": role, "anchor": anchor, "unit": UNITS[role]})
    fixture = SyntheticBindingFixture(fixture_id="operation1", proposal=proposal, operands=operands,
                                      support_basis=["Host-pinned synthetic operand slots; no parser or runtime claim"])
    adapter_manifest = ValueSourceManifest(adapter_id="synthetic-value-adapter", adapter_version="1",
        profile=SemanticProfile(profile_id="finite-fixture", semantic_sources={"fixture-premises": digest(b"pinned synthetic premises")},
                                supported_kinds=ALL_KINDS), probe_policy_sha256=digest(b"no sampler"))
    adapter = SyntheticValueAdapter(adapter_manifest, [fixture])
    return HostContext(task, workspace, proposal, fixture, adapter_manifest, adapter,
                       ValueSourceStore(workspace, adapter))


def bound_request(context: HostContext, *, execution: bool = False):
    binding = context.store.bind_operation(context.proposal, context.fixture.fixture_id)
    request = CaptureRequest(binding_id=binding.binding_id, binding_sha256=content_sha256(binding),
        kind=context.proposal.kind, length_mode=context.proposal.length_mode,
        mode="execution_sample" if execution else "source_literal", reason="Synthetic contract check",
        scenario_id="first_open" if execution else None, path_id="main_path" if execution else None,
        subject_id="buffer1" if execution else None, job_id="job1" if execution else None)
    return binding, request


def fixture_for(context: HostContext, request: CaptureRequest, *, identifier: str = "capture1") -> SyntheticCaptureFixture:
    event = None
    if request.mode == "execution_sample":
        event = SyntheticEvent(task_id=context.workspace.task_id, run_id=context.workspace.run_id,
            evaluated_snapshot_sha256=context.proposal.evaluated_snapshot_sha256,
            candidate_id=context.proposal.candidate_id, scenario_id="first_open", path_id="main_path",
            subject_id="buffer1", execution_id="execution1", job_id="job1", process_id="process1",
            thread_id="thread1", occurrence_id="use1", object_instance_id="buffer-instance1",
            object_generation="generation1", phase="at_use",
            operation_anchor_sha256=content_sha256(context.proposal.operation))
    return SyntheticCaptureFixture(fixture_id=identifier, request=request,
        values=[CapturedValue.model_validate({"role": role, "diagnostic_binding_id": operand.diagnostic_binding_id,
            "operand_anchor_sha256": content_sha256(operand.anchor), "unit": operand.unit,
            "value": VALUES[role], "event": event.model_copy(deep=True) if event is not None else None})
            for role, operand in context.fixture.operands.items()], normalized_integers=True,
        fixed_attached_buffer=True, state_at_use=True, observation_non_perturbing=True)


def collect(context: HostContext, fixture: SyntheticCaptureFixture) -> CaptureReceipt:
    context.adapter.add_capture(fixture)
    return context.store.capture_values(fixture.request, fixture.fixture_id)


def rewrite_record(path: Path, raw: dict, identity_field: str, prefix: str) -> Path:
    """Refresh an attacker's content address without relying on production ID helpers."""
    semantic = {key: value for key, value in raw.items() if key != identity_field}
    raw[identity_field] = prefix + digest(json.dumps(semantic, sort_keys=True, separators=(",", ":"),
                                                   ensure_ascii=False).encode())
    destination = path.with_name(f"{raw[identity_field]}.json")
    path.unlink()
    destination.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    return destination


@pytest.mark.parametrize("kind,mode,execution", [
    ("typed_array_view", "explicit", False), ("typed_array_view", "explicit", True),
    ("typed_array_view", "remaining", False), ("data_view_access", "explicit", True),
    ("array_index", "explicit", False), ("ready_at_use", "explicit", True),
])
def test_round_trip_is_scoped_synthetic_and_never_confirms_causality(tmp_path: Path, kind, mode, execution) -> None:
    context = make_context(tmp_path, kind, mode)
    binding, request = bound_request(context, execution=execution)
    fixture = fixture_for(context, request)
    receipt = collect(context, fixture)
    assert binding.status == "verified" and binding.support_origin == "synthetic_fixture"
    assert binding.target_platform_status == "unknown"
    assert receipt.status == "collected" and receipt.origin == "synthetic_fixture"
    assert receipt.application_verification == "not_run" and receipt.root_cause_confirmation == "unknown"
    assert (receipt.coherent_event is not None) is execution
    reopened_workspace = CandidateWorkspace.reopen(context.task, context.workspace.root, "run1")
    reopened = ValueSourceStore(reopened_workspace, SyntheticValueAdapter(context.manifest, [context.fixture]))
    reopened.adapter.add_capture(fixture)
    assert reopened.binding(binding.binding_id) == binding
    assert reopened.intent(receipt.intent.request_id) == receipt.intent
    assert reopened.receipts() == {receipt.receipt_id: receipt}
    assert context.store.capture_values(request, fixture.fixture_id) == receipt
    diagnosis = AnalysisStore(context.workspace).bundles()["diagnosis1"]["diagnosis"]
    assert diagnosis.hypotheses[0].support_status == "unknown"
    assert not context.workspace.draft.exists() and not (context.workspace.root / "checks").exists()


def test_utf8_offsets_and_crlf_are_raw_bytes_not_display_columns(tmp_path: Path) -> None:
    context = make_context(tmp_path)
    anchor = context.proposal.operation
    assert SOURCE[anchor.start:anchor.end] == OPERATION
    assert anchor.start == SOURCE.index(OPERATION) and anchor.start > SOURCE.decode().index(OPERATION.decode())
    assert anchor.start_line == anchor.end_line == 3
    assert anchor.file_sha256 == digest(SOURCE)
    assert anchor.file_sha256 != digest(SOURCE.replace(b"\r\n", b"\n"))
    context.store.bind_operation(context.proposal, "operation1")
    inside_character = SOURCE.index("中".encode()) + 1
    with pytest.raises(UnicodeDecodeError):
        SourceAnchor.from_bytes(context.workspace.source_snapshot_sha256, "src/main.ets", SOURCE,
                                inside_character, inside_character + 1)
    with pytest.raises(UnicodeDecodeError):
        SourceAnchor.from_bytes(context.workspace.source_snapshot_sha256, "src/main.ets", SOURCE,
                                0, inside_character)


@pytest.mark.parametrize("fault", ["line", "parent", "wrong_operation", "outside_parent"])
def test_host_checks_exact_spans_and_parent_operation(tmp_path: Path, fault: str) -> None:
    context = make_context(tmp_path)
    proposal = context.proposal.model_copy(deep=True)
    fixture = context.fixture.model_copy(deep=True)
    if fault == "line":
        proposal.operation.start_line = 2
    elif fault == "parent":
        fixture.operands["offset"].anchor.parent_anchor_sha256 = digest(b"other parent")
    elif fault == "wrong_operation":
        start = SOURCE.index(b"new Uint16Array(second, 4, 1)")
        proposal.operation = SourceAnchor.from_bytes(proposal.evaluated_snapshot_sha256, "src/main.ets", SOURCE,
                                                     start, start + len(b"new Uint16Array(second, 4, 1)"))
    else:
        start = SOURCE.index(b"second")
        fixture.operands["capacity"].anchor = SourceAnchor.from_bytes(proposal.evaluated_snapshot_sha256,
            "src/main.ets", SOURCE, start, start + len(b"second"), proposal.operation)
    adapter = SyntheticValueAdapter(context.manifest, [fixture])
    with pytest.raises(ValueError):
        ValueSourceStore(context.workspace, adapter).bind_operation(proposal, "operation1")


@pytest.mark.parametrize("fault", ["self_authority", "coerced_offset", "injected_values", "bad_schema"])
def test_model_proposals_cannot_supply_authority_values_or_coerced_fields(tmp_path: Path, fault: str) -> None:
    context = make_context(tmp_path)
    raw = context.proposal.model_dump(mode="json")
    if fault == "self_authority":
        raw["status"] = "verified"
    elif fault == "coerced_offset":
        raw["operation"]["start"] = str(raw["operation"]["start"])
    elif fault == "injected_values":
        raw["values"] = {"capacity": 999}
    else:
        raw["schema_version"] = 2
    with pytest.raises(ValidationError):
        BindingProposal.model_validate_json(json.dumps(raw))
    assert not (context.store.root / "bindings").exists()


def test_adapter_and_record_accessors_copy_nested_values(tmp_path: Path) -> None:
    context = make_context(tmp_path)
    binding, request = bound_request(context)
    fixture = fixture_for(context, request)
    receipt = collect(context, fixture)
    context.manifest.profile.semantic_sources["fixture-premises"] = digest(b"mutated")
    context.fixture.operands["offset"].anchor.start_line = 1
    fixture.values[0].value = 777
    context.adapter.manifest.profile.supported_kinds.clear()
    context.adapter.binding("operation1").operands.clear()
    context.adapter.capture("capture1").values.clear()
    binding.unknown_reasons.append("caller mutation")
    receipt.fixture.values.clear()
    assert context.store.binding(binding.binding_id).unknown_reasons == []
    restored = context.store.receipt(receipt.receipt_id)
    assert len(restored.fixture.values) == 4 and restored.fixture.values[0].value == 4
    assert context.store.adapter.manifest.profile.supported_kinds == ALL_KINDS


def test_dynamic_fixture_append_preserves_static_manifest_and_prior_receipts(tmp_path: Path) -> None:
    context = make_context(tmp_path)
    _, request = bound_request(context)
    first = collect(context, fixture_for(context, request))
    manifest_before = (context.store.root / "manifest.json").read_bytes()
    first_before = (context.store.root / "receipts" / f"{first.receipt_id}.json").read_bytes()
    second_fixture = fixture_for(context, request, identifier="capture2")
    second_fixture.values[0].value = 16
    second = collect(context, second_fixture)
    assert second.receipt_id != first.receipt_id and len(context.store.receipts()) == 2
    assert (context.store.root / "manifest.json").read_bytes() == manifest_before
    assert (context.store.root / "receipts" / f"{first.receipt_id}.json").read_bytes() == first_before
    context.adapter.add_capture(second_fixture.model_copy(deep=True))
    second_fixture.values[0].value = 17
    with pytest.raises(ValueError, match="cannot be replaced"):
        context.adapter.add_capture(second_fixture)
    assert context.store.receipt(second.receipt_id).fixture.values[0].value == 16


@pytest.mark.parametrize("fault", ["same_unit_roles", "operator", "length_mode"])
def test_capture_cannot_swap_equal_unit_roles_or_change_operator(tmp_path: Path, fault: str) -> None:
    context = make_context(tmp_path)
    _, request = bound_request(context)
    fixture = fixture_for(context, request)
    if fault == "same_unit_roles":
        capacity, offset = fixture.values[:2]
        assert capacity.unit == offset.unit == "bytes"
        capacity.diagnostic_binding_id, offset.diagnostic_binding_id = offset.diagnostic_binding_id, capacity.diagnostic_binding_id
        capacity.operand_anchor_sha256, offset.operand_anchor_sha256 = offset.operand_anchor_sha256, capacity.operand_anchor_sha256
        with pytest.raises(ValueError, match="host operand mapping"):
            collect(context, fixture)
    else:
        request.kind = "data_view_access" if fault == "operator" else request.kind
        request.length_mode = "remaining" if fault == "length_mode" else request.length_mode
        with pytest.raises(ValueError, match="operator or binding identity"):
            context.store.request_capture(request)
    assert context.store.receipts() == {}


@pytest.mark.parametrize("field", ["task_id", "run_id", "diagnosis_id", "hypothesis_id"])
def test_proposals_require_registered_task_run_and_diagnosis(tmp_path: Path, field: str) -> None:
    context = make_context(tmp_path)
    proposal = context.proposal.model_copy(deep=True)
    setattr(proposal, field, "another")
    with pytest.raises(ValueError):
        context.store.bind_operation(proposal, "operation1")
    assert not (context.store.root / "bindings").exists()


def test_recovery_revalidates_diagnosis_and_source_instead_of_disk_labels(tmp_path: Path) -> None:
    context = make_context(tmp_path)
    binding, request = bound_request(context)
    receipt = collect(context, fixture_for(context, request))
    diagnosis_path = context.workspace.root / "diagnoses/diagnosis1.json"
    raw = json.loads(diagnosis_path.read_text(encoding="utf-8"))
    raw["diagnosis"]["created_at"] = "2026-10-02T00:00:00Z"
    diagnosis_path.write_text(json.dumps(raw), encoding="utf-8")
    assert context.store.receipt(receipt.receipt_id) == receipt
    raw["diagnosis"]["hypotheses"][0]["constraint"]["condition"] = "Changed semantic premise"
    diagnosis_path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="host assessment"):
        context.store.binding(binding.binding_id)
    raw["diagnosis"]["hypotheses"][0]["constraint"]["condition"] = "Untrusted text, never executable"
    diagnosis_path.write_text(json.dumps(raw), encoding="utf-8")
    (context.workspace.base / "src/main.ets").write_bytes(SOURCE.replace(b"\r\n", b"\n"))
    with pytest.raises(ValueError, match="snapshot changed"):
        context.store.receipt(receipt.receipt_id)


def test_frozen_manifest_rejects_a_new_adapter_profile(tmp_path: Path) -> None:
    context = make_context(tmp_path)
    binding, _ = bound_request(context)
    manifest = context.manifest.model_copy(deep=True)
    manifest.profile.semantic_sources["fixture-premises"] = digest(b"another semantic source")
    with pytest.raises(ValueError, match="immutable record or manifest changed"):
        ValueSourceStore(context.workspace, SyntheticValueAdapter(manifest, [context.fixture]))
    assert context.store.binding(binding.binding_id) == binding


def test_reopening_a_store_with_missing_manifest_never_recreates_trust(tmp_path: Path) -> None:
    context = make_context(tmp_path)
    _, request = bound_request(context)
    collect(context, fixture_for(context, request))
    manifest = context.store.root / "manifest.json"
    manifest.unlink()
    with pytest.raises(ValueError):
        ValueSourceStore(context.workspace, context.adapter)
    assert not manifest.exists()


def test_source_request_and_fixture_cannot_dispatch_or_claim_an_observed_event(tmp_path: Path) -> None:
    context = make_context(tmp_path)
    _, request = bound_request(context, execution=True)
    fixture = fixture_for(context, request)
    source = request.model_copy(deep=True)
    source.mode, source.job_id = "source_literal", None
    fixture.request = source
    with pytest.raises(ValueError, match="source facts cannot carry"):
        collect(context, fixture)
    source.job_id = "hidden-job"
    with pytest.raises(ValidationError, match="cannot dispatch"):
        CaptureRequest.model_validate_json(source.model_dump_json())


@pytest.mark.parametrize("field,value", [("origin", "execution_sample"), ("root_cause_confirmation", "supported"),
                                          ("application_verification", "passed")])
def test_fixture_receipts_cannot_be_promoted_to_runtime_or_success(tmp_path: Path, field: str, value: str) -> None:
    context = make_context(tmp_path)
    _, request = bound_request(context, execution=True)
    receipt = collect(context, fixture_for(context, request))
    raw = receipt.model_dump(mode="json")
    raw[field] = value
    with pytest.raises(ValidationError):
        CaptureReceipt.model_validate_json(json.dumps(raw))
    assert context.store.receipt(receipt.receipt_id) == receipt


@pytest.mark.parametrize("field,value,reason", [
    ("object_instance_id", "buffer-instance2", "inconsistent_operation_event"),
    ("object_generation", "generation2", "inconsistent_operation_event"),
    ("occurrence_id", "use2", "inconsistent_operation_event"),
    ("thread_id", "thread2", "inconsistent_operation_event"),
    ("phase", "after_use", "inconsistent_operation_event"),
])
def test_fragments_from_other_generations_times_and_threads_stay_unknown(tmp_path: Path, field, value, reason) -> None:
    context = make_context(tmp_path)
    _, request = bound_request(context, execution=True)
    fixture = fixture_for(context, request)
    event = fixture.values[1].event
    assert event is not None
    setattr(event, field, value)
    receipt = collect(context, fixture)
    assert receipt.status == "unknown" and reason in receipt.unknown_reasons and receipt.coherent_event is None
    assert context.store.receipt(receipt.receipt_id) == receipt


@pytest.mark.parametrize("field", ["task_id", "subject_id", "job_id", "operation_anchor_sha256"])
def test_sample_owner_object_job_and_operation_contradictions_are_rejected(tmp_path: Path, field: str) -> None:
    context = make_context(tmp_path)
    _, request = bound_request(context, execution=True)
    fixture = fixture_for(context, request)
    event = fixture.values[0].event
    assert event is not None
    setattr(event, field, digest(b"other operation") if field == "operation_anchor_sha256" else "other")
    with pytest.raises(ValueError):
        collect(context, fixture)
    assert context.store.receipts() == {}


@pytest.mark.parametrize("status", ["not_reached", "blocked", "inconclusive"])
def test_failed_collection_keeps_its_status_without_pass_or_refutation(tmp_path: Path, status: str) -> None:
    context = make_context(tmp_path)
    _, request = bound_request(context, execution=True)
    fixture = fixture_for(context, request)
    fixture.status = status
    fixture.values.clear()
    receipt = collect(context, fixture)
    assert receipt.status == status and "capture_" + status in receipt.unknown_reasons
    assert receipt.coherent_event is None and receipt.root_cause_confirmation == "unknown"
    assert receipt.application_verification == "not_run"


@pytest.mark.parametrize("fault,reason", [
    ("missing", "missing_required_roles"), ("null", "missing_value:count"),
    ("negative_capacity", "outside_integer_profile:capacity"), ("negative_offset", "outside_integer_profile:offset"),
    ("negative_count", "outside_integer_profile:count"), ("boolean", "outside_integer_profile:count"),
    ("large", "outside_integer_profile:count"), ("width", "unsupported_element_width"),
    ("expression", "outside_integer_profile:count"),
])
def test_missing_or_unsupported_scalars_stay_unknown_without_evaluation(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str, reason: str) -> None:
    context = make_context(tmp_path)
    _, request = bound_request(context)
    fixture = fixture_for(context, request)
    by_role = {value.role: value for value in fixture.values}
    monkeypatch.chdir(tmp_path)
    sentinel = tmp_path / "must-not-exist"
    if fault == "missing":
        fixture.values = [value for value in fixture.values if value.role != "count"]
    elif fault == "null":
        by_role["count"].value = None
    elif fault.startswith("negative_"):
        by_role[fault.removeprefix("negative_")].value = -1
    else:
        role = "width" if fault == "width" else "count"
        by_role[role].value = {"boolean": True, "large": 2**53, "width": 3,
                              "expression": "open('must-not-exist', 'w')"}[fault]
    receipt = collect(context, fixture)
    assert receipt.status == "unknown" and reason in receipt.unknown_reasons
    assert not sentinel.exists()


def test_negative_array_length_is_not_reinterpreted_as_a_valid_extent(tmp_path: Path) -> None:
    context = make_context(tmp_path, "array_index")
    _, request = bound_request(context)
    fixture = fixture_for(context, request)
    next(value for value in fixture.values if value.role == "length").value = -1
    receipt = collect(context, fixture)
    assert receipt.status == "unknown" and "outside_integer_profile:length" in receipt.unknown_reasons


@pytest.mark.parametrize("fault,reason", [
    ("normalized_integers", "integer_normalization_unconfirmed"),
    ("fixed_attached_buffer", "fixed_attached_buffer_unconfirmed"),
    ("observation_non_perturbing", "observation_non_perturbing_unconfirmed"),
    ("generation", "use_point_or_generation_unconfirmed"),
    ("instance", "use_point_or_generation_unconfirmed"),
])
def test_unconfirmed_normalization_buffer_observer_and_generation_are_unknown(tmp_path: Path, fault: str, reason: str) -> None:
    context = make_context(tmp_path)
    _, request = bound_request(context, execution=True)
    fixture = fixture_for(context, request)
    if fault in ("generation", "instance"):
        for value in fixture.values:
            assert value.event is not None
            if fault == "generation":
                value.event.object_generation = None
            else:
                value.event.object_instance_id = None
    else:
        setattr(fixture, fault, None)
    receipt = collect(context, fixture)
    assert receipt.status == "unknown" and reason in receipt.unknown_reasons


def test_ready_before_or_after_use_and_missing_state_proof_do_not_establish_use_state(tmp_path: Path) -> None:
    context = make_context(tmp_path, "ready_at_use")
    _, request = bound_request(context, execution=True)
    for phase in ("before_use", "after_use"):
        fixture = fixture_for(context, request, identifier=phase)
        assert fixture.values[0].event is not None
        fixture.values[0].event.phase = phase
        receipt = collect(context, fixture)
        assert receipt.status == "unknown" and "use_point_or_generation_unconfirmed" in receipt.unknown_reasons
    fixture = fixture_for(context, request)
    fixture.state_at_use = None
    assert "state_at_use_unconfirmed" in collect(context, fixture).unknown_reasons


def test_unknown_binding_and_unsupported_semantic_profile_preserve_unknown(tmp_path: Path) -> None:
    context = make_context(tmp_path)
    unbound = context.store.bind_operation(context.proposal)
    assert unbound.status == "unknown" and unbound.support_origin == "unknown"
    request = CaptureRequest(binding_id=unbound.binding_id, binding_sha256=content_sha256(unbound),
        kind=context.proposal.kind, mode="source_literal", reason="No supported binding")
    fixture = SyntheticCaptureFixture(fixture_id="empty", request=request)
    receipt = collect(context, fixture)
    assert receipt.status == "unknown" and "operation_binding_unknown" in receipt.unknown_reasons
    assert "missing_required_roles" in receipt.unknown_reasons
    assert context.store.binding(unbound.binding_id) == unbound
    # A new workspace owns its own static profile, so it can record unsupported semantics without changing the first one.
    another_workspace = CandidateWorkspace(context.task, tmp_path / "unsupported", "run1")
    manifest = context.manifest.model_copy(deep=True)
    manifest.profile.supported_kinds = ["array_index"]
    store = ValueSourceStore(another_workspace, SyntheticValueAdapter(manifest, [context.fixture]))
    AnalysisStore(another_workspace).put_bundle(
        AnalysisStore(context.workspace).bundles()["diagnosis1"]["diagnosis"],
        list(AnalysisStore(context.workspace).evidence().values()), [])
    assessment = store.bind_operation(context.proposal, "operation1")
    assert assessment.status == "unknown" and "unsupported_semantic_profile" in assessment.unknown_reasons


@pytest.mark.parametrize("record_kind", ["binding", "receipt"])
def test_recomputed_content_addresses_do_not_hide_forged_host_records(tmp_path: Path, record_kind: str) -> None:
    context = make_context(tmp_path)
    binding, request = bound_request(context)
    receipt = collect(context, fixture_for(context, request))
    if record_kind == "binding":
        path = context.store.root / "bindings" / f"{binding.binding_id}.json"
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw["status"], raw["unknown_reasons"] = "unknown", ["Forged alternative interpretation"]
        replacement = rewrite_record(path, raw, "binding_id", "binding-")
        BindingAssessment.model_validate_json(replacement.read_bytes())
        with pytest.raises(ValueError, match="host assessment"):
            context.store.binding(replacement.stem)
    else:
        path = context.store.root / "receipts" / f"{receipt.receipt_id}.json"
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw["fixture"]["values"][0]["value"] = 400
        raw["fixture_sha256"] = digest(json.dumps(raw["fixture"], sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode())
        replacement = rewrite_record(path, raw, "receipt_id", "receipt-")
        CaptureReceipt.model_validate_json(replacement.read_bytes())
        with pytest.raises(ValueError, match="host collection"):
            context.store.receipt(replacement.stem)


def test_forged_intent_recovery_is_read_only_even_with_refreshed_content_id(tmp_path: Path) -> None:
    context = make_context(tmp_path)
    _, request = bound_request(context)
    intent = context.store.request_capture(request)
    path = context.store.root / "requests" / f"{intent.request_id}.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["manifest_sha256"] = digest(b"another frozen manifest")
    replacement = rewrite_record(path, raw, "request_id", "capture-")
    CaptureIntent.model_validate_json(replacement.read_bytes())
    paths_before = {item.name: item.read_bytes() for item in replacement.parent.iterdir()}
    with pytest.raises(ValueError, match="capture intent changed"):
        context.store.intent(replacement.stem)
    assert {item.name: item.read_bytes() for item in replacement.parent.iterdir()} == paths_before


@pytest.mark.parametrize("target", ["manifest", "receipt"])
def test_hard_linked_manifest_and_receipt_paths_are_rejected_without_affecting_target(tmp_path: Path, target: str) -> None:
    context = make_context(tmp_path)
    _, request = bound_request(context)
    receipt = collect(context, fixture_for(context, request))
    path = (context.store.root / "manifest.json" if target == "manifest" else
            context.store.root / "receipts" / f"{receipt.receipt_id}.json")
    outside = tmp_path / "outside.json"
    original = path.read_bytes()
    outside.write_bytes(original)
    path.unlink()
    os.link(outside, path)
    try:
        with pytest.raises(ValueError, match="hard link"):
            context.store.receipts()
        assert outside.read_bytes() == original
    finally:
        path.unlink()


def test_candidate_binding_requires_new_byte_anchors_and_rechecks_frozen_candidate(tmp_path: Path) -> None:
    context = make_context(tmp_path)
    context.workspace.start_draft()
    context.workspace.apply_text_edits([TextEdit(path="src/main.ets", old='const flag = "ready";',
        new='const prefix = 1;\nconst flag = "ready";')])
    candidate = context.workspace.register_candidate(1, "diagnosis1")
    frozen = context.workspace.require_registered(candidate.candidate_id) / "src/main.ets"
    raw = frozen.read_bytes()
    proposal = context.proposal.model_copy(deep=True)
    proposal.candidate_id, proposal.evaluated_snapshot_sha256 = candidate.candidate_id, candidate.candidate_snapshot_sha256
    with pytest.raises(ValueError):
        context.store.bind_operation(proposal, "operation1")
    start = raw.index(OPERATION)
    proposal.operation = SourceAnchor.from_bytes(candidate.candidate_snapshot_sha256, "src/main.ets", raw, start, start + len(OPERATION))
    fixture = context.fixture.model_copy(deep=True)
    fixture.fixture_id, fixture.proposal = "candidate-operation", proposal
    for role, operand in fixture.operands.items():
        offset = start + OPERATION.index(TOKENS[role])
        operand.anchor = SourceAnchor.from_bytes(candidate.candidate_snapshot_sha256, "src/main.ets", raw,
                                                 offset, offset + len(TOKENS[role]), proposal.operation)
    adapter = SyntheticValueAdapter(context.manifest, [fixture])
    store = ValueSourceStore(context.workspace, adapter)
    binding = store.bind_operation(proposal, "candidate-operation")
    assert binding.proposal.operation.start_line == 4 and store.binding(binding.binding_id) == binding
    frozen.write_bytes(raw.replace(b"buffer, 8", b"buffer, 6"))
    with pytest.raises(ValueError):
        store.binding(binding.binding_id)
