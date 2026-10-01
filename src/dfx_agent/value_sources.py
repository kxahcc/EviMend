"""Independent operation bindings and receipts from host-pinned synthetic adapters.

No parser, sampler, expression evaluation or Agent tool is provided here. Even a
verified binding proves only coherence with a trusted *synthetic* fixture.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import Field, model_validator

from .conditions import ConditionKind
from .contracts import (
    Contract,
    require_hash,
    require_identifiers,
    require_path,
    require_unique,
)
from .diagnostics import AnalysisStore
from .task import digest, snapshot_digest
from .workspace import CandidateWorkspace, _manifest

Unit = Literal["bytes", "elements", "bytes_per_element", "state"]
CaptureStatus = Literal["collected", "not_reached", "blocked", "inconclusive", "unknown"]
UNITS: dict[str, Unit] = {"capacity": "bytes", "offset": "bytes", "width": "bytes_per_element",
                         "count": "elements", "index": "elements", "length": "elements", "state": "state"}


def canonical(value: Contract) -> str:
    return json.dumps(value.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def content_sha256(value: Contract) -> str:
    return digest(canonical(value).encode("utf-8"))


def _copy[T: Contract](value: T) -> T:
    # model_copy does not validate updated values; JSON round trips do.
    return type(value).model_validate_json(canonical(value))


def _id(prefix: str, value: Contract, field: str) -> str:
    raw = value.model_dump(mode="json", exclude={field})
    return prefix + digest(json.dumps(raw, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode())


def _roles(kind: ConditionKind, mode: str) -> set[str]:
    roles = {"array_index": {"index", "length"}, "data_view_access": {"capacity", "offset", "width"},
             "typed_array_view": {"capacity", "offset", "width", "count"}, "ready_at_use": {"state"}}[kind].copy()
    if mode == "remaining":
        if kind != "typed_array_view":
            raise ValueError("remaining length requires typed_array_view")
        roles.remove("count")
    return roles


class SourceAnchor(Contract):
    snapshot_sha256: str
    path: str
    file_sha256: str
    start: int = Field(ge=0)
    end: int = Field(gt=0)
    span_sha256: str
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    parent_anchor_sha256: str | None = None

    @model_validator(mode="after")
    def valid_anchor(self) -> SourceAnchor:
        require_path(self.path)
        for value in (self.snapshot_sha256, self.file_sha256, self.span_sha256):
            require_hash(value)
        if self.parent_anchor_sha256 is not None:
            require_hash(self.parent_anchor_sha256)
        if self.end <= self.start or self.end_line < self.start_line:
            raise ValueError("invalid source span")
        return self

    @classmethod
    def from_bytes(cls, snapshot_sha256: str, path: str, raw: bytes, start: int, end: int,
                   parent: SourceAnchor | None = None) -> SourceAnchor:
        raw.decode("utf-8", errors="strict")
        if type(start) is not int or type(end) is not int or not 0 <= start < end <= len(raw):
            raise ValueError("source span is outside the file")
        raw[:start].decode("utf-8", errors="strict")
        raw[start:end].decode("utf-8", errors="strict")
        return cls(snapshot_sha256=snapshot_sha256, path=path, file_sha256=digest(raw), start=start, end=end,
                   span_sha256=digest(raw[start:end]), start_line=raw[:start].count(b"\n") + 1,
                   end_line=raw[:end - 1].count(b"\n") + 1,
                   parent_anchor_sha256=content_sha256(parent) if parent is not None else None)


class BindingProposal(Contract):
    task_id: str
    run_id: str
    source_snapshot_sha256: str
    evaluated_snapshot_sha256: str
    candidate_id: str | None = None
    diagnosis_id: str
    hypothesis_id: str
    kind: ConditionKind
    length_mode: Literal["explicit", "remaining"] = "explicit"
    operation: SourceAnchor
    diagnostic_binding_ids: list[str] = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def valid_proposal(self) -> BindingProposal:
        require_identifiers([self.task_id, self.run_id, self.diagnosis_id, self.hypothesis_id,
                             *self.diagnostic_binding_ids])
        require_unique(self.diagnostic_binding_ids)
        require_hash(self.source_snapshot_sha256)
        require_hash(self.evaluated_snapshot_sha256)
        if self.candidate_id is not None:
            require_hash(self.candidate_id)
        elif self.evaluated_snapshot_sha256 != self.source_snapshot_sha256:
            raise ValueError("original binding must use the analysis snapshot")
        if self.operation.snapshot_sha256 != self.evaluated_snapshot_sha256 or self.operation.parent_anchor_sha256:
            raise ValueError("operation anchor has another snapshot or a parent")
        _roles(self.kind, self.length_mode)
        return self


class BoundOperand(Contract):
    diagnostic_binding_id: str
    anchor: SourceAnchor
    unit: Unit

    @model_validator(mode="after")
    def valid_operand(self) -> BoundOperand:
        require_identifiers([self.diagnostic_binding_id])
        return self


class SemanticProfile(Contract):
    profile_id: str
    formula_version: Literal["finite_condition_v1"] = "finite_condition_v1"
    semantic_sources: dict[str, str] = Field(min_length=1)
    supported_kinds: list[ConditionKind] = Field(min_length=1)
    target_platform_status: Literal["unknown"] = "unknown"

    @model_validator(mode="after")
    def valid_profile(self) -> SemanticProfile:
        require_identifiers([self.profile_id, *self.semantic_sources])
        require_unique([*self.supported_kinds])
        for value in self.semantic_sources.values():
            require_hash(value)
        return self


class ValueSourceManifest(Contract):
    adapter_id: str
    adapter_version: str
    profile: SemanticProfile
    probe_policy_sha256: str
    admitted_origin: Literal["synthetic_fixture"] = "synthetic_fixture"

    @model_validator(mode="after")
    def valid_manifest(self) -> ValueSourceManifest:
        require_identifiers([self.adapter_id, self.adapter_version])
        require_hash(self.probe_policy_sha256)
        return self


class SyntheticBindingFixture(Contract):
    fixture_id: str
    proposal: BindingProposal
    operands: dict[str, BoundOperand]
    status: Literal["verified", "unknown"] = "verified"
    support_basis: list[str] = Field(default_factory=list)
    unknown_reasons: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def valid_fixture(self) -> SyntheticBindingFixture:
        require_identifiers([self.fixture_id])
        if self.status == "verified" and (not self.support_basis or self.unknown_reasons):
            raise ValueError("synthetic verified binding needs its fixture premises")
        if self.status == "unknown" and not self.unknown_reasons:
            raise ValueError("unknown binding needs reasons")
        if set(self.operands) != _roles(self.proposal.kind, self.proposal.length_mode):
            raise ValueError("host operand roles are incomplete")
        for role, operand in self.operands.items():
            if operand.unit != UNITS[role] or operand.diagnostic_binding_id not in self.proposal.diagnostic_binding_ids:
                raise ValueError("host operand unit or diagnosis reference differs")
        return self


class BindingAssessment(Contract):
    binding_id: str
    proposal: BindingProposal
    manifest_sha256: str
    diagnosis_sha256: str
    fixture: SyntheticBindingFixture | None
    fixture_sha256: str | None
    status: Literal["verified", "unknown"]
    unknown_reasons: list[str]
    support_origin: Literal["synthetic_fixture", "unknown"]
    target_platform_status: Literal["unknown"] = "unknown"

    @model_validator(mode="after")
    def valid_assessment(self) -> BindingAssessment:
        for value in (self.manifest_sha256, self.diagnosis_sha256):
            require_hash(value)
        if (self.fixture is None) != (self.fixture_sha256 is None):
            raise ValueError("binding fixture identity is incomplete")
        if self.fixture is not None and self.fixture_sha256 != content_sha256(self.fixture):
            raise ValueError("binding fixture content changed")
        if self.binding_id != _id("binding-", self, "binding_id"):
            raise ValueError("binding content identity changed")
        return self


class CaptureRequest(Contract):
    binding_id: str
    binding_sha256: str
    kind: ConditionKind
    length_mode: Literal["explicit", "remaining"] = "explicit"
    mode: Literal["source_literal", "execution_sample"]
    scenario_id: str | None = None
    path_id: str | None = None
    subject_id: str | None = None
    job_id: str | None = None
    probe_plan_id: str | None = None
    reason: str = Field(min_length=1, max_length=2048)

    @model_validator(mode="after")
    def valid_request(self) -> CaptureRequest:
        require_identifiers([self.binding_id, *[v for v in (self.scenario_id, self.path_id, self.subject_id,
                                                           self.job_id, self.probe_plan_id) if v is not None]])
        require_hash(self.binding_sha256)
        _roles(self.kind, self.length_mode)
        if self.mode == "source_literal" and any(v is not None for v in (self.job_id, self.probe_plan_id)):
            raise ValueError("source request cannot dispatch a job")
        if self.mode == "execution_sample" and not all((self.scenario_id, self.path_id, self.subject_id)):
            raise ValueError("execution request needs intended scope")
        return self


class SyntheticEvent(Contract):
    task_id: str
    run_id: str
    evaluated_snapshot_sha256: str
    operation_anchor_sha256: str
    candidate_id: str | None = None
    scenario_id: str
    path_id: str
    subject_id: str
    execution_id: str
    job_id: str
    process_id: str
    thread_id: str
    occurrence_id: str
    object_instance_id: str | None = None
    object_generation: str | None = None
    phase: Literal["before_use", "at_use", "after_use"] | None = None

    @model_validator(mode="after")
    def valid_event(self) -> SyntheticEvent:
        require_identifiers([self.task_id, self.run_id, self.scenario_id, self.path_id, self.subject_id,
                             self.execution_id, self.job_id, self.process_id, self.thread_id, self.occurrence_id])
        if self.object_generation is not None:
            require_identifiers([self.object_generation])
        if self.object_instance_id is not None:
            require_identifiers([self.object_instance_id])
        require_hash(self.evaluated_snapshot_sha256)
        require_hash(self.operation_anchor_sha256)
        if self.candidate_id is not None:
            require_hash(self.candidate_id)
        return self


class CapturedValue(Contract):
    role: str
    diagnostic_binding_id: str
    operand_anchor_sha256: str
    unit: Unit
    value: int | bool | str | None = None
    event: SyntheticEvent | None = None

    @model_validator(mode="after")
    def valid_value(self) -> CapturedValue:
        if self.role not in UNITS:
            raise ValueError("unsupported value role")
        require_identifiers([self.diagnostic_binding_id])
        require_hash(self.operand_anchor_sha256)
        if isinstance(self.value, str) and len(self.value) > 128:
            raise ValueError("captured value is not a small scalar")
        return self


class SyntheticCaptureFixture(Contract):
    fixture_id: str
    request: CaptureRequest
    values: list[CapturedValue] = Field(default_factory=list, max_length=64)
    status: CaptureStatus = "collected"
    normalized_integers: bool | None = None
    fixed_attached_buffer: bool | None = None
    state_at_use: bool | None = None
    observation_non_perturbing: bool | None = None
    unknown_reasons: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def valid_capture(self) -> SyntheticCaptureFixture:
        require_identifiers([self.fixture_id])
        require_unique([item.role for item in self.values])
        return self


class CaptureIntent(Contract):
    request_id: str
    request: CaptureRequest
    manifest_sha256: str

    @model_validator(mode="after")
    def valid_intent(self) -> CaptureIntent:
        require_hash(self.manifest_sha256)
        if self.request_id != _id("capture-", self, "request_id"):
            raise ValueError("capture request identity changed")
        return self


class CaptureReceipt(Contract):
    receipt_id: str
    intent: CaptureIntent
    fixture: SyntheticCaptureFixture
    fixture_sha256: str
    origin: Literal["synthetic_fixture"] = "synthetic_fixture"
    status: CaptureStatus
    unknown_reasons: list[str]
    coherent_event: SyntheticEvent | None
    root_cause_confirmation: Literal["unknown"] = "unknown"
    application_verification: Literal["not_run"] = "not_run"

    @model_validator(mode="after")
    def valid_receipt(self) -> CaptureReceipt:
        if self.fixture_sha256 != content_sha256(self.fixture):
            raise ValueError("capture fixture changed")
        if self.receipt_id != _id("receipt-", self, "receipt_id"):
            raise ValueError("receipt content identity changed")
        return self


class SyntheticValueAdapter:
    """Trusted host-only fixture configuration, not a JSON/Agent admission API.

    Fixture IDs are immutable; new fixtures may be appended after startup. The
    static manifest deliberately does not depend on that dynamic collection.
    Reopening a store requires this trusted adapter, never disk labels alone.
    """

    def __init__(self, manifest: ValueSourceManifest, bindings: list[SyntheticBindingFixture]):
        self._manifest = canonical(_copy(manifest))
        self._bindings: dict[str, str] = {}
        self._captures: dict[str, str] = {}
        for fixture in bindings:
            value = _copy(fixture)
            self._append(self._bindings, value.fixture_id, canonical(value))

    @staticmethod
    def _append(registry: dict[str, str], identifier: str, payload: str) -> None:
        if identifier in registry and registry[identifier] != payload:
            raise ValueError("host fixture identity cannot be replaced")
        registry[identifier] = payload

    @property
    def manifest(self) -> ValueSourceManifest:
        return ValueSourceManifest.model_validate_json(self._manifest)

    def binding(self, identifier: str) -> SyntheticBindingFixture:
        if identifier not in self._bindings:
            raise ValueError("binding fixture is not registered by the host")
        return SyntheticBindingFixture.model_validate_json(self._bindings[identifier])

    def add_capture(self, fixture: SyntheticCaptureFixture) -> None:
        value = _copy(fixture)
        self._append(self._captures, value.fixture_id, canonical(value))

    def capture(self, identifier: str) -> SyntheticCaptureFixture:
        if identifier not in self._captures:
            raise ValueError("capture fixture is not registered by the host")
        return SyntheticCaptureFixture.model_validate_json(self._captures[identifier])


class ValueSourceStore:
    """Append-only, source-revalidated bindings, intents and synthetic receipts."""

    def __init__(self, workspace: CandidateWorkspace, adapter: SyntheticValueAdapter):
        self.workspace, self.adapter = workspace, adapter
        self.root = workspace.root / "value-sources"
        self._expected_manifest = canonical(adapter.manifest)
        self._workspace()
        if self.root.exists() and not (self.root / "manifest.json").exists():
            raise ValueError("existing value-source store has no frozen manifest")
        self._safe(self.root, directory=True).mkdir(exist_ok=True)
        path = self._safe(self.root / "manifest.json")
        self._write_once(path, self._manifest_text())
        self._manifest()

    def _safe(self, path: Path, *, directory: bool = False) -> Path:
        if not path.is_relative_to(self.workspace.root):
            raise ValueError("value-source path is outside workspace")
        parts = [self.workspace.root]
        for part in path.relative_to(self.workspace.root).parts:
            parts.append(parts[-1] / part)
        for index, item in enumerate(parts):
            try:
                info = item.lstat()
            except FileNotFoundError:
                continue
            if (stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) &
                    getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)):
                raise ValueError("value-source path is linked")
            is_directory = index < len(parts) - 1 or directory
            if ((is_directory and not stat.S_ISDIR(info.st_mode)) or
                    (not is_directory and (not stat.S_ISREG(info.st_mode) or info.st_nlink > 1))):
                raise ValueError("value-source path has wrong type or a hard link")
        return path

    def _write_once(self, path: Path, text: str) -> None:
        self._safe(path)
        if path.exists():
            if path.read_bytes() != text.encode("utf-8"):
                raise ValueError("immutable record or manifest changed")
            return
        # A same-directory hard-link publication is exclusive and never replaces
        # an existing destination. Remove only this call's owned temporary file.
        temp = self._safe(path.with_name("write-" + uuid4().hex + ".tmp"))
        try:
            with temp.open("xb") as stream:
                stream.write(text.encode("utf-8"))
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temp, path)
            except FileExistsError:
                self._safe(path)
                if path.read_bytes() != text.encode("utf-8"):
                    raise ValueError("immutable record conflict") from None
        finally:
            if temp.exists():
                temp.unlink()
        self._safe(path)

    def _workspace(self) -> None:
        expected = {"schema_version": 1, "task_id": self.workspace.task_id, "run_id": self.workspace.run_id,
                    "source_snapshot_sha256": self.workspace.source_snapshot_sha256}
        if json.loads(self._safe(self.workspace.root / "workspace.json").read_bytes()) != expected:
            raise ValueError("workspace owner changed")
        if snapshot_digest(_manifest(self.workspace.base)) != self.workspace.source_snapshot_sha256:
            raise ValueError("analysis source snapshot changed")

    def _manifest_text(self) -> str:
        return json.dumps({"task_id": self.workspace.task_id, "run_id": self.workspace.run_id,
                           "source_snapshot_sha256": self.workspace.source_snapshot_sha256,
                           "manifest": json.loads(self._expected_manifest)}, sort_keys=True,
                          separators=(",", ":"), ensure_ascii=False)

    def _manifest(self) -> str:
        self._workspace()
        if canonical(self.adapter.manifest) != self._expected_manifest:
            raise ValueError("trusted adapter manifest changed")
        path = self._safe(self.root / "manifest.json")
        if path.read_bytes() != self._manifest_text().encode("utf-8"):
            raise ValueError("frozen manifest changed")
        return digest(self._expected_manifest.encode("utf-8"))

    def _source(self, proposal: BindingProposal) -> Path:
        if (proposal.task_id != self.workspace.task_id or proposal.run_id != self.workspace.run_id or
                proposal.source_snapshot_sha256 != self.workspace.source_snapshot_sha256):
            raise ValueError("binding belongs to another task, run or snapshot")
        root = self.workspace.base
        if proposal.candidate_id is not None:
            folder = self._safe(self.workspace.candidates / proposal.candidate_id, directory=True)
            for name in ("candidate.json", "manifest.json", "patch.diff"):
                self._safe(folder / name)
            root = self.workspace.require_registered(proposal.candidate_id)
            record = json.loads(self._safe(root.parent / "candidate.json").read_bytes())
            if record["candidate_snapshot_sha256"] != proposal.evaluated_snapshot_sha256:
                raise ValueError("binding has a stale candidate snapshot")
        return root

    def _anchor(self, anchor: SourceAnchor, source: Path, parent: SourceAnchor | None = None) -> None:
        raw = self._safe(source / anchor.path).read_bytes()
        actual = SourceAnchor.from_bytes(anchor.snapshot_sha256, anchor.path, raw, anchor.start, anchor.end, parent)
        if anchor != actual:
            raise ValueError("source anchor bytes or coordinates changed")
        if parent is not None and (anchor.path != parent.path or anchor.snapshot_sha256 != parent.snapshot_sha256 or
                                   not parent.start <= anchor.start < anchor.end <= parent.end):
            raise ValueError("operand is outside its parent operation")

    def _diagnosis(self, proposal: BindingProposal) -> str:
        for folder_name in ("diagnoses", "obligations"):
            folder = self._safe(self.workspace.root / folder_name, directory=True)
            for path in folder.glob("*.json"):
                self._safe(path)
        bundles = AnalysisStore(self.workspace).bundles()
        bundle = bundles.get(proposal.diagnosis_id)
        if bundle is None:
            raise ValueError("binding diagnosis is not registered")
        hypothesis = next((h for h in bundle["diagnosis"].hypotheses if h.hypothesis_id == proposal.hypothesis_id), None)
        if hypothesis is None or hypothesis.constraint is None:
            raise ValueError("binding hypothesis has no constraint")
        constraint = hypothesis.constraint
        family = "lifecycle" if proposal.kind == "ready_at_use" else "bounds"
        if constraint.family != family or not set(proposal.diagnostic_binding_ids) <= {v.binding_id for v in constraint.bindings}:
            raise ValueError("binding family or diagnosis references differ")
        if proposal.operation.path != constraint.operation.path:
            raise ValueError("binding operation has another path")
        if proposal.candidate_id is None and (proposal.operation.start_line < constraint.operation.start_line or
                                              proposal.operation.end_line > constraint.operation.end_line):
            raise ValueError("original operation is outside the diagnosis operation")
        semantic = {"diagnosis": bundle["diagnosis"].model_dump(mode="json", exclude={"created_at"}),
                    "evidence": [v.model_dump(mode="json", exclude={"created_at"})
                                 for v in sorted(bundle["evidence"], key=lambda v: v.record_id)],
                    "obligations": [v.model_dump(mode="json", exclude={"created_at"})
                                    for v in sorted(bundle["obligations"], key=lambda v: v.record_id)]}
        return digest(json.dumps(semantic, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode())

    def _assess(self, proposal: BindingProposal, fixture_id: str | None) -> BindingAssessment:
        manifest_sha256 = self._manifest()
        source = self._source(proposal)
        self._anchor(proposal.operation, source)
        diagnosis_sha256 = self._diagnosis(proposal)
        fixture = self.adapter.binding(fixture_id) if fixture_id is not None else None
        if fixture is not None:
            if fixture.proposal != proposal:
                raise ValueError("proposal differs from the host operation binding")
            for operand in fixture.operands.values():
                self._anchor(operand.anchor, source, proposal.operation)
        reasons = fixture.unknown_reasons if fixture is not None else ["unsupported_operation_adapter"]
        status = fixture.status if fixture is not None else "unknown"
        if proposal.kind not in self.adapter.manifest.profile.supported_kinds:
            status, reasons = "unknown", [*reasons, "unsupported_semantic_profile"]
        raw = {"proposal": proposal, "manifest_sha256": manifest_sha256, "diagnosis_sha256": diagnosis_sha256,
               "fixture": fixture, "fixture_sha256": content_sha256(fixture) if fixture is not None else None,
               "status": status, "unknown_reasons": reasons,
               "support_origin": "synthetic_fixture" if fixture is not None else "unknown",
               "target_platform_status": "unknown", "schema_version": 1}
        # Construct the canonical identity before invoking the full validator.
        value = BindingAssessment.model_construct(binding_id="", **raw)
        return BindingAssessment.model_validate_json(canonical(value.model_copy(update={"binding_id": _id("binding-", value, "binding_id")})))

    def _save(self, folder_name: str, identifier: str, value: Contract) -> None:
        require_identifiers([identifier])
        folder = self._safe(self.root / folder_name, directory=True)
        folder.mkdir(exist_ok=True)
        self._write_once(folder / f"{identifier}.json", canonical(value))

    def _read[T: Contract](self, folder_name: str, identifier: str, model: type[T]) -> T:
        require_identifiers([identifier])
        path = self._safe(self.root / folder_name / f"{identifier}.json")
        if not path.is_file():
            raise ValueError("value-source record is not registered")
        return model.model_validate_json(path.read_bytes())

    def bind_operation(self, proposal: BindingProposal, fixture_id: str | None = None) -> BindingAssessment:
        result = self._assess(_copy(proposal), fixture_id)
        self._save("bindings", result.binding_id, result)
        return _copy(result)

    def binding(self, binding_id: str) -> BindingAssessment:
        saved = self._read("bindings", binding_id, BindingAssessment)
        expected = self._assess(saved.proposal, saved.fixture.fixture_id if saved.fixture is not None else None)
        if saved != expected or saved.binding_id != binding_id:
            raise ValueError("saved binding differs from host assessment")
        return expected

    def _capture_intent(self, request: CaptureRequest) -> CaptureIntent:
        request = _copy(request)
        binding = self.binding(request.binding_id)
        if (request.binding_sha256 != content_sha256(binding) or request.kind != binding.proposal.kind or
                request.length_mode != binding.proposal.length_mode):
            raise ValueError("capture operator or binding identity differs")
        raw = CaptureIntent.model_construct(request_id="", request=request, manifest_sha256=self._manifest(), schema_version=1)
        result = CaptureIntent.model_validate_json(canonical(raw.model_copy(update={"request_id": _id("capture-", raw, "request_id")})))
        return result

    def request_capture(self, request: CaptureRequest) -> CaptureIntent:
        result = self._capture_intent(request)
        self._save("requests", result.request_id, result)
        return _copy(result)

    def intent(self, request_id: str) -> CaptureIntent:
        saved = self._read("requests", request_id, CaptureIntent)
        # Readback must not create any new intent, even from coherent forgeries.
        expected = self._capture_intent(saved.request)
        if saved != expected or saved.request_id != request_id:
            raise ValueError("saved capture intent changed")
        return expected

    def _receipt(self, intent: CaptureIntent, fixture_id: str) -> CaptureReceipt:
        binding = self.binding(intent.request.binding_id)
        fixture = self.adapter.capture(fixture_id)
        if fixture.request != intent.request:
            raise ValueError("capture fixture belongs to another request")
        reasons = list(fixture.unknown_reasons)
        event: SyntheticEvent | None = None
        if binding.status != "verified":
            reasons.append("operation_binding_unknown")
        if binding.fixture is None and fixture.values:
            raise ValueError("values have no host operand mapping")
        roles = _roles(intent.request.kind, intent.request.length_mode)
        if {v.role for v in fixture.values} != roles:
            reasons.append("missing_required_roles")
        events = []
        for value in fixture.values:
            operand = binding.fixture.operands.get(value.role) if binding.fixture is not None else None
            if (operand is None or value.diagnostic_binding_id != operand.diagnostic_binding_id or
                    value.operand_anchor_sha256 != content_sha256(operand.anchor)):
                raise ValueError("captured role differs from host operand mapping")
            if value.unit != operand.unit:
                reasons.append("unit_mismatch:" + value.role)
            if value.value is None:
                reasons.append("missing_value:" + value.role)
            elif ((value.role != "state" and (type(value.value) is not int or abs(value.value) > 2**53 - 1)) or
                    (value.role in ("capacity", "offset", "length", "count") and isinstance(value.value, int) and value.value < 0)):
                reasons.append("outside_integer_profile:" + value.role)
            elif value.role == "width" and value.value not in (1, 2, 4, 8):
                reasons.append("unsupported_element_width")
            elif value.role == "state" and value.value not in ("ready", "uninitialized", "released", "pending"):
                reasons.append("unsupported_state")
            if value.event is not None:
                observed = value.event
                proposal = binding.proposal
                if ((observed.task_id, observed.run_id, observed.evaluated_snapshot_sha256, observed.candidate_id) !=
                        (proposal.task_id, proposal.run_id, proposal.evaluated_snapshot_sha256, proposal.candidate_id)):
                    raise ValueError("sample belongs to another task, run or candidate")
                if observed.operation_anchor_sha256 != content_sha256(proposal.operation):
                    raise ValueError("sample belongs to another operation")
                request = intent.request
                if ((observed.scenario_id, observed.path_id, observed.subject_id) !=
                        (request.scenario_id, request.path_id, request.subject_id) or
                        (request.job_id is not None and observed.job_id != request.job_id)):
                    raise ValueError("sample scope contradicts the intended request")
                events.append(observed)
        if intent.request.mode == "source_literal":
            if events:
                raise ValueError("source facts cannot carry operation events")
        else:
            if len(events) != len(fixture.values) or not events:
                reasons.append("missing_operation_event")
            elif any(v != events[0] for v in events):
                reasons.append("inconsistent_operation_event")
            elif events[0].object_instance_id is None or events[0].object_generation is None or events[0].phase != "at_use":
                reasons.append("use_point_or_generation_unconfirmed")
            else:
                event = events[0]
            if fixture.observation_non_perturbing is not True:
                reasons.append("observation_non_perturbing_unconfirmed")
        if intent.request.kind != "ready_at_use" and fixture.normalized_integers is not True:
            reasons.append("integer_normalization_unconfirmed")
        if intent.request.kind in ("typed_array_view", "data_view_access") and fixture.fixed_attached_buffer is not True:
            reasons.append("fixed_attached_buffer_unconfirmed")
        if intent.request.kind == "ready_at_use" and fixture.state_at_use is not True:
            reasons.append("state_at_use_unconfirmed")
        status = fixture.status
        if status != "collected":
            reasons.append("capture_" + status)
        elif reasons:
            status = "unknown"
        reasons = sorted(set(reasons))
        raw = CaptureReceipt.model_construct(receipt_id="", intent=intent, fixture=fixture,
            fixture_sha256=content_sha256(fixture), origin="synthetic_fixture", status=status,
            unknown_reasons=reasons, coherent_event=event if status == "collected" else None,
            root_cause_confirmation="unknown", application_verification="not_run", schema_version=1)
        return CaptureReceipt.model_validate_json(canonical(raw.model_copy(update={"receipt_id": _id("receipt-", raw, "receipt_id")})))

    def capture_values(self, request: CaptureRequest, fixture_id: str) -> CaptureReceipt:
        intent = self.request_capture(request)
        result = self._receipt(intent, fixture_id)
        self._save("receipts", result.receipt_id, result)
        return _copy(result)

    def receipt(self, receipt_id: str) -> CaptureReceipt:
        saved = self._read("receipts", receipt_id, CaptureReceipt)
        intent = self.intent(saved.intent.request_id)
        expected = self._receipt(intent, saved.fixture.fixture_id)
        if saved != expected or saved.receipt_id != receipt_id:
            raise ValueError("saved receipt differs from host collection")
        return expected

    def receipts(self) -> dict[str, CaptureReceipt]:
        self._manifest()
        folder = self._safe(self.root / "receipts", directory=True)
        return {p.stem: self.receipt(p.stem) for p in sorted(folder.glob("*.json"))}
