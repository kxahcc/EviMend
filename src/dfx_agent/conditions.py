"""Finite condition calculations over host-pinned synthetic inputs, never app verdicts.

The registry is supplied by trusted test/preparation code, not by an Agent tool.
Expressions in the diagnosis are references only; this module never evaluates them.
"""

from __future__ import annotations

import json
from typing import Literal

from pydantic import Field, model_validator

from .contracts import (
    Contract,
    SourceLocation,
    require_hash,
    require_identifiers,
    require_unique,
)
from .diagnostics import AnalysisStore, safe_source
from .task import digest, snapshot_digest
from .workspace import CandidateWorkspace, _manifest

ConditionKind = Literal["array_index", "data_view_access", "typed_array_view", "ready_at_use"]
Status = Literal["satisfied", "violated", "unknown"]
MAX_SAFE_INTEGER = 2**53 - 1


class ConditionScope(Contract):
    task_id: str
    run_id: str
    source_snapshot_sha256: str
    evaluated_snapshot_sha256: str
    candidate_id: str | None = None
    operation: SourceLocation
    scenario_id: str
    path_id: str
    subject_id: str
    sample_id: str

    @model_validator(mode="after")
    def valid_scope(self) -> ConditionScope:
        require_identifiers([self.task_id, self.run_id, self.scenario_id, self.path_id, self.subject_id, self.sample_id])
        require_hash(self.source_snapshot_sha256)
        require_hash(self.evaluated_snapshot_sha256)
        if self.candidate_id is not None:
            require_hash(self.candidate_id)
        elif self.evaluated_snapshot_sha256 != self.source_snapshot_sha256:
            raise ValueError("original scope must use the analysis snapshot")
        if self.operation.snapshot_sha256 != self.evaluated_snapshot_sha256:
            raise ValueError("operation must use the evaluated snapshot")
        return self


class ConditionRequest(Contract):
    check_id: str
    diagnosis_id: str
    hypothesis_id: str
    scope: ConditionScope
    kind: ConditionKind
    binding_ids: dict[str, str]
    length_mode: Literal["explicit", "remaining"] = "explicit"
    profile: Literal["finite_condition_v1"] = "finite_condition_v1"

    @model_validator(mode="after")
    def valid_roles(self) -> ConditionRequest:
        require_identifiers([self.check_id, self.diagnosis_id, self.hypothesis_id, *self.binding_ids.values()])
        roles = {
            "array_index": {"index", "length"},
            "data_view_access": {"capacity", "offset", "width"},
            "typed_array_view": {"capacity", "offset", "width", "count"},
            "ready_at_use": {"state"},
        }[self.kind].copy()
        if self.length_mode == "remaining":
            if self.kind != "typed_array_view":
                raise ValueError("remaining length applies only to a typed array view")
            roles.remove("count")
        if set(self.binding_ids) != roles:
            raise ValueError("condition roles are incomplete or unsupported")
        return self


class ObservedValue(Contract):
    binding_id: str
    unit: Literal["bytes", "elements", "bytes_per_element", "state"]
    value: int | bool | str | None = None

    @model_validator(mode="after")
    def valid_value(self) -> ObservedValue:
        require_identifiers([self.binding_id])
        if isinstance(self.value, str) and len(self.value) > 128:
            raise ValueError("condition value is not a small scalar")
        return self


class ObservationBatch(Contract):
    observation_id: str
    scope: ConditionScope
    origin: Literal["model_inference", "synthetic_fixture"] = "model_inference"
    values: list[ObservedValue] = Field(default_factory=list, max_length=64)
    normalized_integers: bool | None = None
    fixed_attached_buffer: bool | None = None
    state_at_use: bool | None = None

    @model_validator(mode="after")
    def valid_batch(self) -> ObservationBatch:
        require_identifiers([self.observation_id])
        require_unique([item.binding_id for item in self.values])
        return self


def _payload(value: Contract) -> str:
    return json.dumps(value.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


class SyntheticObservations:
    """Host-only registry: copy fixtures so later edits cannot change admitted inputs.

    Pinning a fixture proves equality to a supplied test input, not execution.
    There is deliberately no application or source-reader admission path yet.
    """

    def __init__(self, fixtures: list[ObservationBatch]):
        self._payloads: dict[str, str] = {}
        for fixture in fixtures:
            if fixture.origin != "synthetic_fixture" or fixture.observation_id in self._payloads:
                raise ValueError("synthetic registry requires unique host fixtures")
            self._payloads[fixture.observation_id] = _payload(fixture)

    def matches(self, proposed: ObservationBatch) -> bool:
        return (proposed.origin == "synthetic_fixture" and
                self._payloads.get(proposed.observation_id) == _payload(proposed))


class PredicateFinding(Contract):
    predicate: str
    status: Literal["satisfied", "violated"]
    operands: dict[str, int | str]


class ConditionResult(Contract):
    request: ConditionRequest
    request_sha256: str
    observation_id: str | None = None
    observation_sha256: str | None = None
    status: Status
    support_level: Literal["synthetic", "unknown"]
    findings: list[PredicateFinding] = Field(default_factory=list)
    unknown_reasons: list[str] = Field(default_factory=list)
    root_cause_confirmation: Literal["unknown"] = "unknown"
    application_verification: Literal["not_run"] = "not_run"


class ConditionEvaluator:
    def __init__(self, workspace: CandidateWorkspace, observations: SyntheticObservations):
        self.workspace = workspace
        self.observations = observations

    def _bind(self, request: ConditionRequest) -> None:
        scope = request.scope
        if (scope.task_id != self.workspace.task_id or scope.run_id != self.workspace.run_id or
                scope.source_snapshot_sha256 != self.workspace.source_snapshot_sha256):
            raise ValueError("condition request belongs to another task, run or analysis snapshot")
        if snapshot_digest(_manifest(self.workspace.base)) != self.workspace.source_snapshot_sha256:
            raise ValueError("condition analysis snapshot changed")
        source = self.workspace.base
        if scope.candidate_id is not None:
            source = self.workspace.require_registered(scope.candidate_id)
            candidate = json.loads((source.parent / "candidate.json").read_text(encoding="utf-8"))
            if scope.evaluated_snapshot_sha256 != candidate["candidate_snapshot_sha256"]:
                raise ValueError("condition request has a stale candidate snapshot")
        bundles = AnalysisStore(self.workspace).bundles()
        if request.diagnosis_id not in bundles:
            raise ValueError("condition diagnosis is not registered")
        hypotheses = {item.hypothesis_id: item for item in bundles[request.diagnosis_id]["diagnosis"].hypotheses}
        hypothesis = hypotheses.get(request.hypothesis_id)
        if hypothesis is None or hypothesis.constraint is None:
            raise ValueError("condition hypothesis has no registered constraint")
        constraint = hypothesis.constraint
        family = "lifecycle" if request.kind == "ready_at_use" else "bounds"
        if constraint.family != family or not set(request.binding_ids.values()) <= {item.binding_id for item in constraint.bindings}:
            raise ValueError("condition family or binding is not registered for this hypothesis")
        if scope.operation.path != constraint.operation.path:
            raise ValueError("condition operation has another source path")
        if scope.candidate_id is None and scope.operation != constraint.operation:
            raise ValueError("original condition operation differs from its constraint")
        # Candidate coordinates are fixture-specific; automatic line mapping is not implemented.
        lines = safe_source(source, scope.operation.path).read_text(encoding="utf-8").splitlines()
        if scope.operation.end_line > len(lines):
            raise ValueError("condition operation is outside evaluated source")

    def evaluate(self, request: ConditionRequest, batch: ObservationBatch | None) -> ConditionResult:
        self._bind(request)
        common = {"request": request, "request_sha256": digest(_payload(request).encode()),
                  "observation_id": batch.observation_id if batch else None,
                  "observation_sha256": digest(_payload(batch).encode()) if batch else None}

        def unknown(reason: str) -> ConditionResult:
            return ConditionResult(**common, status="unknown", support_level="unknown", unknown_reasons=[reason])

        if batch is None:
            return unknown("missing_observations")
        if not self.observations.matches(batch):
            return unknown("observations_not_host_pinned")
        if batch.scope != request.scope:
            return unknown("observation_scope_mismatch")
        values = {item.binding_id: item for item in batch.values}
        selected: dict[str, ObservedValue] = {}
        for role, identifier in request.binding_ids.items():
            if identifier not in values or values[identifier].value is None:
                return unknown("missing_value:" + role)
            selected[role] = values[identifier]
        findings: list[PredicateFinding] = []

        def finding(name: str, holds: bool, **operands: int | str) -> None:
            findings.append(PredicateFinding(predicate=name, status="satisfied" if holds else "violated", operands=operands))

        if request.kind == "ready_at_use":
            state = selected["state"]
            if state.unit != "state":
                return unknown("unit_mismatch:state")
            if batch.state_at_use is not True:
                return unknown("state_not_captured_at_use")
            if state.value not in ("ready", "uninitialized", "released"):
                return unknown("unsupported_state")
            finding("ready_at_use", state.value == "ready", state=str(state.value))
        else:
            if batch.normalized_integers is not True:
                return unknown("integer_normalization_unconfirmed")
            if request.kind != "array_index" and batch.fixed_attached_buffer is not True:
                return unknown("fixed_attached_buffer_unconfirmed")
            expected_units = {"index": "elements", "length": "elements", "capacity": "bytes", "offset": "bytes",
                              "width": "bytes_per_element", "count": "elements"}
            numbers: dict[str, int] = {}
            for role, observed in selected.items():
                if observed.unit != expected_units[role]:
                    return unknown("unit_mismatch:" + role)
                value = observed.value
                if not isinstance(value, int) or isinstance(value, bool) or abs(value) > MAX_SAFE_INTEGER:
                    return unknown("outside_integer_profile:" + role)
                if role != "index" and value < 0:
                    return unknown("outside_integer_profile:" + role)
                numbers[role] = value
            if request.kind == "array_index":
                finding("nonnegative_index", numbers["index"] >= 0, index=numbers["index"])
                finding("index_below_length", numbers["index"] < numbers["length"], **numbers)
            else:
                capacity, offset, width = numbers["capacity"], numbers["offset"], numbers["width"]
                if width not in (1, 2, 4, 8):
                    return unknown("unsupported_element_width")
                if request.kind == "data_view_access":
                    finding("access_within_view", offset + width <= capacity, **numbers)
                else:
                    finding("aligned_offset", offset % width == 0, offset=offset, width=width)
                    if request.length_mode == "explicit":
                        extent = offset + numbers["count"] * width
                        finding("view_within_buffer", extent <= capacity, extent=extent, **numbers)
                    else:
                        finding("offset_within_buffer", offset <= capacity, offset=offset, capacity=capacity)
                        finding("whole_remaining_elements", (capacity - offset) % width == 0, **numbers)
        status: Status = "violated" if any(item.status == "violated" for item in findings) else "satisfied"
        return ConditionResult(**common, status=status, support_level="synthetic", findings=findings)
