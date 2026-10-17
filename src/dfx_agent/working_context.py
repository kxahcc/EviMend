"""Read-only, source-checked repair context packets, not SDK send views.

The host supplies an explicit focus. This slice checks its registered content
and provenance, but does not authenticate past tool actions, infer the latest
focus, select LangChain messages, or certify a cause or application repair.
"""

from __future__ import annotations

import json
import stat
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .conditions import ConditionRecord
from .contracts import (
    CandidatePatch,
    Diagnosis,
    Evidence,
    RepairObligation,
    execution_evidence_kind,
    require_hash,
    require_identifiers,
    require_path,
    require_unique,
)
from .decisions import DecisionRecord, DecisionStore
from .diagnostics import AnalysisStore
from .evidence_io import local_evidence_path
from .logs import saved_log_excerpt_matches
from .model_calls import canonical, sha256
from .policies import (
    Counterexample,
    ObligationClaim,
    candidate_conflicts,
    reconsider_after_feedback,
)
from .task import digest, snapshot_digest, verify_task
from .verification import CheckLedger, CheckResult, ReplayOnlyExecutor
from .workspace import CandidateWorkspace, _files, _manifest

PROFILE = "registered_working_context_v1"
MAX_SOURCE_RECORD_BYTES = 8_388_608


class WorkingContextError(ValueError):
    """Fixed host categories do not quote task or model text."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


class WorkingContextFocus(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    diagnosis_id: str | None = None
    hypothesis_ids: tuple[str, ...] = ()
    current_candidate_id: str | None = None
    decision_id: str | None = None
    feedback_decision_ids: tuple[str, ...] = ()

    @model_validator(mode="after")
    def valid_focus(self) -> WorkingContextFocus:
        require_identifiers([value for value in (self.diagnosis_id, self.decision_id) if value is not None])
        require_identifiers(list(self.hypothesis_ids + self.feedback_decision_ids))
        require_unique(list(self.hypothesis_ids))
        require_unique(list(self.feedback_decision_ids))
        if self.current_candidate_id is not None:
            require_hash(self.current_candidate_id)
        if self.diagnosis_id is None and (
            self.hypothesis_ids or self.current_candidate_id or self.decision_id or self.feedback_decision_ids
        ):
            raise ValueError("a noninitial focus needs an explicit diagnosis")
        if self.decision_id in self.feedback_decision_ids:
            raise ValueError("a feedback decision cannot duplicate the selected decision")
        return self


class WorkingContextPacket(BaseModel):
    """Canonical payload is the sole content; contents() always returns a copy."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    schema_version: Literal[1] = 1
    profile: Literal["registered_working_context_v1"] = PROFILE
    canonical_payload_json: str
    payload_sha256: str
    max_bytes: int = Field(gt=0)

    @model_validator(mode="after")
    def valid_packet(self) -> WorkingContextPacket:
        require_hash(self.payload_sha256)
        value = _load(self.canonical_payload_json)
        if not isinstance(value, dict) or canonical(value) != self.canonical_payload_json:
            raise ValueError("working context payload is not a canonical object")
        if sha256(self.canonical_payload_json) != self.payload_sha256:
            raise ValueError("working context payload hash changed")
        if len(self.canonical_payload_json.encode("utf-8")) > self.max_bytes:
            raise WorkingContextError("working_context_required_limit")
        return self

    def contents(self) -> dict[str, Any]:
        return _load(self.canonical_payload_json)


def _load(value: str | bytes) -> Any:
    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        found: dict[str, Any] = {}
        for key, item in pairs:
            if key in found:
                raise ValueError("duplicate working context source key")
            found[key] = item
        return found

    result = json.loads(value, object_pairs_hook=unique,
                        parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite source")))
    canonical(result).encode("utf-8")
    return result


def candidate_focus_matches(diagnosis: Diagnosis, candidate: CandidatePatch, facts: list[Evidence],
                            related: dict[str, CandidatePatch]) -> bool:
    """A failed candidate may be reviewed by a new, explicitly linked diagnosis.

    This reference predicate follows source/check authentication, never replaces
    it. A model-declared feedback_candidate_id alone is insufficient.
    """
    if candidate.diagnosis_id == diagnosis.record_id:
        return True
    previous = related.get(diagnosis.feedback_candidate_id or "")
    if previous is None:
        return False
    same_owner = (diagnosis.task_id, diagnosis.run_id, diagnosis.source_snapshot_sha256)
    if any((item.task_id, item.run_id, item.source_snapshot_sha256) != same_owner for item in (candidate, previous)):
        return False
    # A revised candidate from the same originating diagnosis may be the new
    # host-selected focus. Its predecessor's observation stays historical.
    if candidate.candidate_id != previous.candidate_id and (
            candidate.diagnosis_id is None or candidate.diagnosis_id != previous.diagnosis_id):
        return False
    return any(
        fact.kind in ("local_execution", "source_execution", "application_execution") and
        fact.execution_scope is not None and fact.kind == execution_evidence_kind(fact.execution_scope) and
        fact.evaluated_candidate_id == previous.candidate_id and
        fact.execution_snapshot_sha256 == previous.candidate_snapshot_sha256
        for fact in facts
    )


def _guard(path: Path, *, directory: bool = False) -> Path:
    """Check the unresolved path chain, including junctions and shared files."""
    path = path.absolute()
    if ".." in path.parts:
        raise ValueError("working context source path is not canonical")
    for component in (*reversed(path.parents), path):
        info = local_evidence_path(component).lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & getattr(
            stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0
        ):
            raise ValueError("working context source is linked")
        expected_directory = component != path or directory
        if expected_directory:
            if not stat.S_ISDIR(info.st_mode):
                raise ValueError("working context parent is not a directory")
        elif not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError("working context source is not an independent regular file")
    return path


class _Sources:
    def __init__(self, workspace: CandidateWorkspace, task: Path):
        self.workspace = _guard(workspace.root, directory=True)
        self.task = _guard(task, directory=True)
        self.receipts: dict[str, dict[str, Any]] = {}
        self.initial_bytes: dict[Path, bytes] = {}

    def read(self, path: Path, *, retain: bool = True) -> bytes:
        path = _guard(path)
        if path.is_relative_to(self.workspace):
            reference = "workspace/" + path.relative_to(self.workspace).as_posix()
        elif path.is_relative_to(self.task):
            reference = "task/" + path.relative_to(self.task).as_posix()
        else:
            raise ValueError("working context source belongs to another root")
        before = local_evidence_path(path).stat()
        if before.st_size > MAX_SOURCE_RECORD_BYTES:
            raise ValueError("working context source record exceeds bound")
        raw = local_evidence_path(path).read_bytes()
        after = local_evidence_path(_guard(path)).stat()
        if ((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) !=
                (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) or len(raw) != before.st_size):
            raise ValueError("working context source changed during read")
        if path in self.initial_bytes and raw != self.initial_bytes[path]:
            raise ValueError("working context source changed between reads")
        self.initial_bytes[path] = raw
        if retain:
            receipt = {"reference": reference, "bytes": len(raw), "sha256": digest(raw)}
            if reference in self.receipts and self.receipts[reference] != receipt:
                raise ValueError("working context source changed")
            self.receipts[reference] = receipt
        return raw

    def json(self, path: Path, *, retain: bool = True) -> Any:
        return _load(self.read(path, retain=retain))

    def scan_records(self, folder: str) -> None:
        path = self.workspace / folder
        if path.exists():
            _guard(path, directory=True)
            for file in path.glob("*.json"):
                self.json(file, retain=False)

    def recheck_retained(self) -> None:
        for reference in tuple(self.receipts):
            scope, relative = reference.split("/", 1)
            root = self.workspace if scope == "workspace" else self.task
            self.read(root / relative, retain=False)


class _Builder:
    def __init__(self, workspace: CandidateWorkspace, task: Path, ledger: CheckLedger):
        if ledger.workspace is not workspace or ledger.root != workspace.root / "checks":
            raise ValueError("working context ledger belongs to another workspace")
        require_hash(ledger.executor.protocol_sha256)
        self.workspace = workspace
        self.sources = _Sources(workspace, task)
        task_record = self.sources.json(task / "task.json")
        for key in ("source_root", "source_manifest", "crash_log"):
            require_path(task_record[key])
        manifest = self.sources.json(task / task_record["source_manifest"])
        self.source_file_hashes = {entry["path"]: entry["sha256"] for entry in manifest}
        self.log = self.sources.read(task / task_record["crash_log"])
        self.sources.json(workspace.root / "workspace.json")
        for root in (workspace.base, task / task_record["source_root"]):
            for file in _files(_guard(root, directory=True)).values():
                _guard(file)
        checked = verify_task(task)
        CandidateWorkspace._reopen_verified_task(task, workspace.root, workspace.run_id, checked)
        if (checked["task_id"] != workspace.task_id or
                checked["source_snapshot_sha256"] != workspace.source_snapshot_sha256 or
                digest(self.log) != checked["crash_sha256"]):
            raise ValueError("working context task or baseline differs")
        self.task_record = checked
        _guard(ledger.root, directory=True)
        self.ledger = CheckLedger(workspace, ReplayOnlyExecutor(ledger.executor.protocol_sha256), read_only=True)
        for folder in ("diagnoses", "obligations", "decisions", "feedback", "conditions"):
            self.sources.scan_records(folder)
        condition_inputs = workspace.root / "condition-inputs.json"
        if condition_inputs.exists():
            self.sources.json(condition_inputs, retain=False)
        self.analysis = AnalysisStore(workspace)
        self.bundles = self.analysis.bundles()
        self.all_evidence = self.analysis.evidence()
        self.all_obligations = self.analysis.obligations()
        self.decisions = DecisionStore(workspace)
        self.all_decisions = self.decisions.records()
        self._bind_loaded_records()
        self._validate_namespace()
        self.selected_diagnoses: dict[str, Any] = {}
        self.evidence: dict[str, Any] = {}
        self.obligations: dict[str, Any] = {}
        self.candidates: dict[str, Any] = {}
        self.checks: dict[str, Any] = {}
        self.selected_decisions: dict[str, Any] = {}
        self.feedback: dict[str, Any] = {}
        self.conditions: dict[str, Any] = {}
        self.relevance: dict[str, Any] = {}

    def _bind_loaded_records(self) -> None:
        """Bind independently loaded typed objects to the first guarded bytes.

        Store helpers may read a record several times. A stable single read is
        insufficient: object A and an artifact hash from later bytes B must
        never enter one packet, even if each file is individually well formed.
        """
        expected_evidence: dict[str, Evidence] = {}
        expected_obligations: dict[str, RepairObligation] = {}
        for identifier, bundle in self.bundles.items():
            raw = self.sources.json(self.workspace.root / "diagnoses" / f"{identifier}.json", retain=False)
            diagnosis = Diagnosis.model_validate_json(canonical(raw["diagnosis"]))
            facts = [Evidence.model_validate_json(canonical(item)) for item in raw["evidence"]]
            obligations = [RepairObligation.model_validate_json(canonical(item))
                           for item in raw.get("obligations", [])]
            if (diagnosis != bundle["diagnosis"] or facts != bundle["evidence"] or
                    obligations != bundle["obligations"]):
                raise ValueError("working context loaded diagnosis differs from initial bytes")
            for fact in facts:
                if fact.record_id in expected_evidence and expected_evidence[fact.record_id] != fact:
                    raise ValueError("working context evidence identity changed")
                expected_evidence[fact.record_id] = fact
            for obligation in obligations:
                expected_obligations[obligation.record_id] = obligation
        for path in (self.workspace.root / "obligations").glob("*.json"):
            raw = self.sources.json(path, retain=False)
            obligation = RepairObligation.model_validate_json(canonical(raw))
            if obligation.hypothesis_ids or obligation.bindings:
                raise ValueError("standalone obligation cannot introduce structured associations")
            expected_obligations[obligation.record_id] = obligation
        if expected_evidence != self.all_evidence or expected_obligations != self.all_obligations:
            raise ValueError("working context loaded analysis differs between reads")
        for identifier, record in self.all_decisions.items():
            raw = self.sources.json(self.workspace.root / "decisions" / f"{identifier}.json", retain=False)
            if DecisionRecord.model_validate_json(canonical(raw)) != record:
                raise ValueError("working context loaded decision differs from initial bytes")

    def _validate_namespace(self) -> None:
        """Repeat AnalysisStore admission's target/evidence namespace rules."""
        targets: list[str] = []
        for identifier, bundle in self.bundles.items():
            targets.append(identifier)
            targets.extend(h.hypothesis_id for h in bundle["diagnosis"].hypotheses)
            targets.extend(item.record_id for item in bundle["obligations"])
        targets.extend(RepairObligation.model_validate_json(canonical(
            self.sources.json(path, retain=False))).record_id
            for path in (self.workspace.root / "obligations").glob("*.json"))
        if len(targets) != len(set(targets)) or set(targets).intersection(self.all_evidence):
            raise ValueError("working context analysis target namespace is ambiguous")
        for item in self.all_obligations.values():
            if not item.source_evidence_ids or not set(item.source_evidence_ids) <= set(self.all_evidence):
                raise ValueError("working context obligation lacks its registered evidence basis")

    def obligation(self, identifier: str) -> None:
        if identifier in self.obligations:
            return
        item = self.all_obligations.get(identifier)
        if item is None:
            raise ValueError("working context obligation is not registered")
        if item.authority != "model_inference" or item.status != "unknown":
            raise ValueError("offline obligation authority or status was promoted")
        self.obligations[identifier] = item.model_dump(mode="json")
        standalone = self.workspace.root / "obligations" / f"{identifier}.json"
        if standalone.exists():
            self.sources.json(standalone)
        for fact in item.source_evidence_ids:
            self.fact(fact)
        for binding in item.bindings:
            for fact in binding.evidence_ids:
                self.fact(fact)
        if item.hypothesis_ids:
            # A hypothesis label alone is not an owner. Check the bundle that
            # contains this obligation, even when a same-named hypothesis has
            # already entered the closure from another diagnosis.
            owners = [key for key, bundle in self.bundles.items()
                      if any(claim.record_id == identifier and claim == item for claim in bundle["obligations"])]
            if not owners:
                required_facts = set(item.source_evidence_ids)
                required_facts.update(value for binding in item.bindings for value in binding.evidence_ids)
                owners = [key for key, bundle in self.bundles.items()
                          if set(item.hypothesis_ids) <= {h.hypothesis_id for h in bundle["diagnosis"].hypotheses}
                          and required_facts <= {fact.record_id for fact in bundle["evidence"]}]
            if len(owners) != 1:
                raise ValueError("working context obligation hypothesis owner is absent or ambiguous")
            owner = owners[0]
            if not set(item.hypothesis_ids) <= {h.hypothesis_id for h in self.bundles[owner]["diagnosis"].hypotheses}:
                raise ValueError("working context obligation hypothesis belongs to another bundle")
            self.diagnosis(owner)

    def fact(self, identifier: str) -> None:
        if identifier in self.evidence:
            return
        item: Evidence | None = self.all_evidence.get(identifier)
        if item is None:
            raise ValueError("working context evidence is not registered")
        if (item.task_id != self.workspace.task_id or item.run_id != self.workspace.run_id or
                (item.source_snapshot_sha256 != self.workspace.source_snapshot_sha256 and
                 not (item.kind == "log_observation" and item.source_snapshot_sha256 is None))):
            raise ValueError("working context evidence owner or version differs")
        if item.kind == "source_fact":
            if item.location is None or item.execution_id is not None or item.basis_ids:
                raise ValueError("source fact lacks its original source location")
            raw = self.sources.read(self.workspace.base / item.location.path)
            if digest(raw) != self.source_file_hashes.get(item.location.path):
                raise ValueError("source fact file no longer belongs to the original snapshot")
            lines = raw.decode("utf-8").splitlines()
            text = AnalysisStore._range(lines, item.location.start_line, item.location.end_line)
            if text != item.statement or digest(text.encode()) != item.artifact_sha256:
                raise ValueError("source fact does not match original source bytes")
        elif item.kind == "log_observation":
            if item.log_line is None or item.location is not None or item.basis_ids:
                raise ValueError("log observation lacks its original line anchor")
            # Old Evidence schema stores only the first line; the statement fixes
            # its span. Admission's bounded 1..200 line range is rechecked here.
            if (item.artifact_sha256 != self.task_record["crash_sha256"] or
                    not saved_log_excerpt_matches(self.log, item.log_line, item.statement)):
                raise ValueError("log observation does not match the verified task log")
        elif item.kind == "model_inference":
            if item.execution_id is not None or not item.basis_ids:
                raise ValueError("model inference lacks its registered basis")
            if digest(item.statement.encode()) != item.artifact_sha256:
                raise ValueError("model inference content hash changed")
        elif item.kind in ("local_execution", "source_execution", "application_execution"):
            if (item.execution_scope is None or item.kind != execution_evidence_kind(item.execution_scope) or
                    item.evaluated_candidate_id is None or
                    item.location is not None or item.basis_ids):
                raise ValueError("execution fact is not registered candidate feedback in the same scope")
            candidate, result = self.candidate(item.evaluated_candidate_id)
            if (result is None or result.status != "fail" or result.execution_scope != item.execution_scope or
                    item.execution_snapshot_sha256 != candidate.candidate_snapshot_sha256 or
                    item.execution_id != result.idempotency_key or item.statement != result.detail or
                    item.artifact_sha256 != digest(result.model_dump_json().encode())):
                raise ValueError("execution fact does not match a saved failed check")
        else:
            raise ValueError("working context cannot admit application evidence")
        self.evidence[identifier] = item.model_dump(mode="json")
        for basis in item.basis_ids:
            self.fact(basis)

    def diagnosis(self, identifier: str) -> None:
        if identifier in self.selected_diagnoses:
            return
        bundle = self.bundles.get(identifier)
        if bundle is None:
            raise ValueError("working context diagnosis is not registered")
        diagnosis = bundle["diagnosis"]
        if any(h.support_status != "unknown" for h in diagnosis.hypotheses):
            raise ValueError("offline hypothesis support was promoted")
        if any(step.authority != "model_inference" for h in diagnosis.hypotheses for step in h.causal_steps):
            raise ValueError("offline causal step authority was promoted")
        self.sources.json(self.workspace.root / "diagnoses" / f"{identifier}.json")
        self.selected_diagnoses[identifier] = bundle
        for fact in bundle["evidence"]:
            self.fact(fact.record_id)
        for obligation in bundle["obligations"]:
            self.obligation(obligation.record_id)
        for hypothesis in diagnosis.hypotheses:
            for obligation in hypothesis.obligation_ids:
                self.obligation(obligation)
        if diagnosis.feedback_candidate_id is not None:
            self.candidate(diagnosis.feedback_candidate_id)

    def candidate(self, identifier: str) -> tuple[CandidatePatch, CheckResult | None]:
        folder = self.workspace.candidates / identifier
        raw = self.sources.json(folder / "candidate.json")
        candidate = CandidatePatch.model_validate_json(canonical(raw))
        if identifier in self.candidates:
            found = self.checks[identifier]["result"]
            return candidate, None if found is None else CheckResult.model_validate_json(canonical(found))
        self.sources.read(folder / "patch.diff")
        self.sources.json(folder / "manifest.json")
        for file in _files(_guard(folder / "source", directory=True)).values():
            self.sources.read(file)
        # completed() always revalidates the registered candidate, including
        # when no check exists. Keep the separate final packet revalidation.
        self.candidates[identifier] = candidate.model_dump(mode="json")
        key, check_path = self.ledger._identity(identifier, "offline")
        intent = self.sources.json(check_path) if check_path.exists() else None
        result = self.ledger.completed(identifier)
        if result is not None and (intent is None or CheckResult.model_validate_json(
            canonical(intent["result"])) != result):
            raise ValueError("loaded check differs from initial receipt bytes")
        state = "not_requested" if intent is None else ("completed" if result is not None else "pending")
        if intent is not None and (intent["idempotency_key"] != key or
                                   (result is None and intent.get("state") != "pending")):
            raise ValueError("working context check state differs")
        self.checks[identifier] = {"state": state, "candidate_id": identifier,
            "candidate_snapshot_sha256": candidate.candidate_snapshot_sha256,
            "result": None if result is None else result.model_dump(mode="json")}
        if candidate.diagnosis_id is not None:
            self.diagnosis(candidate.diagnosis_id)
        for obligation in candidate.restores + candidate.preserves:
            self.obligation(obligation)
        if candidate.decision_id is not None:
            self.decision(candidate.decision_id)
            self.decisions.candidate_link(candidate.decision_id, candidate.direction_id or "",
                                          candidate.diagnosis_id or "", candidate.restores + candidate.preserves)
        return candidate, result

    def decision(self, identifier: str) -> DecisionRecord:
        record = self.all_decisions.get(identifier)
        if record is None:
            raise ValueError("working context decision is not registered")
        if identifier in self.selected_decisions:
            return record
        self.sources.json(self.workspace.root / "decisions" / f"{identifier}.json")
        self.selected_decisions[identifier] = record.model_dump(mode="json")
        self.diagnosis(record.request.diagnosis_id)
        bundle = self.bundles[record.request.diagnosis_id]
        self.decisions._bind(record.request, bundle)
        if record.request.plan.remaining_probe_budget != 0 or not record.request.plan.available_capabilities <= {
            "read_source", "read_log"
        }:
            raise ValueError("offline decision capabilities were promoted")
        for direction in record.request.plan.directions:
            for obligation in direction.obligation_ids:
                self.obligation(obligation)
            for fact in direction.support_evidence_ids:
                self.fact(fact)
        feedback = self.decisions._feedback(record.request, bundle, self.ledger)
        if feedback is not None:
            feedback_id = record.request.feedback_id
            if feedback_id is None:
                raise ValueError("feedback has no saved identity")
            raw = self.sources.json(self.workspace.root / "feedback" / f"{feedback_id}.json")
            if Counterexample.model_validate_json(canonical(raw)) != feedback:
                raise ValueError("loaded feedback differs from initial bytes")
            self.feedback[feedback_id] = feedback.model_dump(mode="json")
            self.candidate(feedback.candidate_id)
        conditions = self.decisions._conditions(record.request)
        for condition in conditions:
            raw = self.sources.json(self.workspace.root / "conditions" / f"{condition.result_id}.json")
            if ConditionRecord.model_validate_json(canonical(raw)) != condition:
                raise ValueError("loaded condition differs from initial bytes")
            self.sources.json(self.workspace.root / "condition-inputs.json")
            self.conditions[condition.result_id] = condition.model_dump(mode="json")
            scope_candidate = condition.result.request.scope.candidate_id
            if scope_candidate is not None:
                self.candidate(scope_candidate)
        expected_feedback = None
        hard: list[str] = []
        advisory: list[str] = []
        if feedback is not None:
            expected_feedback = reconsider_after_feedback(record.request.plan, feedback,
                candidate_id=feedback.candidate_id, candidate_snapshot_sha256=feedback.candidate_snapshot_sha256,
                features=record.features)
            if record.features.obligation_tracking:
                claims = [ObligationClaim(obligation_id=item.record_id,
                    source_snapshot_sha256=item.source_snapshot_sha256, authority=item.authority,
                    evidence_ids=item.source_evidence_ids, statement=item.statement)
                    for item in self.all_obligations.values()]
                hard, advisory = candidate_conflicts(claims,
                    source_snapshot_sha256=self.workspace.source_snapshot_sha256, feedback=feedback)
        context = {"bundle": {"diagnosis": bundle["diagnosis"].model_dump(mode="json", exclude={"created_at"}),
            "evidence": [item.model_dump(mode="json", exclude={"created_at"}) for item in bundle["evidence"]],
            "obligations": [item.model_dump(mode="json", exclude={"created_at"}) for item in bundle["obligations"]]},
            "feedback": feedback.model_dump() if feedback else None,
            "conditions": [item.model_dump(mode="json") for item in conditions],
            "referenced_obligations": {key: item.model_dump(mode="json", exclude={"created_at"})
                for key, item in self.all_obligations.items() if any(key in d.obligation_ids
                                                                  for d in record.request.plan.directions)}}
        if (record.context_sha256 != sha256(canonical(context)) or record.feedback != expected_feedback or
                record.advisory_obligation_ids != advisory or record.hard_rejected_obligation_ids != hard):
            raise ValueError("working context decision differs from its registered source context")
        return record

    def packet_value(self, focus: WorkingContextFocus) -> dict[str, Any]:
        if focus.diagnosis_id is not None:
            self.diagnosis(focus.diagnosis_id)
            diagnosis = self.bundles[focus.diagnosis_id]["diagnosis"]
            hypotheses = {item.hypothesis_id for item in diagnosis.hypotheses}
            if not set(focus.hypothesis_ids) <= hypotheses:
                raise ValueError("working context hypothesis belongs to another diagnosis")
            if focus.current_candidate_id is not None:
                candidate, _ = self.candidate(focus.current_candidate_id)
                if not candidate_focus_matches(diagnosis, candidate, self.bundles[focus.diagnosis_id]["evidence"],
                        {key: CandidatePatch.model_validate_json(canonical(item)) for key, item in self.candidates.items()}):
                    raise ValueError("current candidate belongs to another focus diagnosis")
            if focus.decision_id is not None:
                decision = self.decision(focus.decision_id)
                if decision.request.diagnosis_id != focus.diagnosis_id:
                    raise ValueError("selected decision belongs to another focus diagnosis")
            current_obligations = {value for item in diagnosis.hypotheses for value in item.obligation_ids}
            current_obligations.update(item.record_id for item in self.bundles[focus.diagnosis_id]["obligations"])
            if focus.current_candidate_id is not None:
                candidate = self.candidates[focus.current_candidate_id]
                current_obligations.update(candidate["restores"] + candidate["preserves"])
            for identifier in focus.feedback_decision_ids:
                decision = self.decision(identifier)
                if decision.request.feedback_id is None or decision.request.candidate_id is None:
                    raise ValueError("historical feedback decision has no actual failed candidate")
                feedback = self.feedback[decision.request.feedback_id]
                shared = sorted(current_obligations.intersection(feedback["violated_obligation_ids"]))
                if not shared:
                    raise ValueError("historical feedback decision has no shared focus obligation")
                self.relevance[identifier] = {"shared_obligation_ids": shared,
                    "authority": "host_reference_overlap", "semantic_relevance": "unknown",
                    "observed_candidate_id": feedback["candidate_id"],
                    "current_candidate_id": focus.current_candidate_id,
                    "is_current_candidate_observation": feedback["candidate_id"] == focus.current_candidate_id}
        # Selected bundles retain all alternatives and questions, not only the
        # focus hypotheses. Packet contents are quoted data, never new facts.
        return {"schema_version": 1, "profile": PROFILE, "task_id": self.workspace.task_id,
            "run_id": self.workspace.run_id, "source_snapshot_sha256": self.workspace.source_snapshot_sha256,
            "focus": focus.model_dump(mode="json"), "focus_authority": "explicit_host_input",
            "focus_recency": "not_authenticated", "root_cause_confirmation": "unknown",
            "application_verification": "not_run", "tool_action_authentication": "not_performed",
            "diagnoses": {key: bundle["diagnosis"].model_dump(mode="json")
                          for key, bundle in sorted(self.selected_diagnoses.items())},
            "evidence": dict(sorted(self.evidence.items())), "obligations": dict(sorted(self.obligations.items())),
            "candidates": dict(sorted(self.candidates.items())), "checks": dict(sorted(self.checks.items())),
            "decisions": dict(sorted(self.selected_decisions.items())), "feedback": dict(sorted(self.feedback.items())),
            "conditions": dict(sorted(self.conditions.items())), "historical_relevance": dict(sorted(self.relevance.items())),
            "sources": [self.sources.receipts[key] for key in sorted(self.sources.receipts)],
            "interpretation": "Source checks preserve original provenance; reference overlap does not prove causality."}


def build_working_context(workspace: CandidateWorkspace, focus: WorkingContextFocus, *,
                          task_directory: Path, ledger: CheckLedger, max_bytes: int) -> WorkingContextPacket:
    """Read registered sources and freeze their necessary closure; execute nothing."""
    if type(max_bytes) is not int or max_bytes <= 0:
        raise ValueError("working context byte budget must be a positive integer")
    try:
        focus = WorkingContextFocus.model_validate_json(focus.model_dump_json())
        builder = _Builder(workspace, task_directory, ledger)
        value = builder.packet_value(focus)
        builder.sources.recheck_retained()
        for identifier in builder.candidates:
            workspace.require_registered(identifier)
        if snapshot_digest(_manifest(workspace.base)) != workspace.source_snapshot_sha256:
            raise ValueError("working context baseline changed while building")
        if verify_task(task_directory) != builder.task_record:
            raise ValueError("working context task changed while building")
        builder.sources.recheck_retained()
        payload = canonical(value)
        if len(payload.encode("utf-8")) > max_bytes:
            raise WorkingContextError("working_context_required_limit")
        return WorkingContextPacket(canonical_payload_json=payload, payload_sha256=sha256(payload), max_bytes=max_bytes)
    except WorkingContextError:
        raise
    except (OSError, ValueError, KeyError, TypeError, UnicodeError, RecursionError) as exc:
        raise WorkingContextError("working_context_source_invalid") from exc


def verify_working_context(packet: WorkingContextPacket, workspace: CandidateWorkspace, *,
                           task_directory: Path, ledger: CheckLedger, max_bytes: int) -> WorkingContextPacket:
    """Rebuild this same source point read-only; never replace frozen content.

    This is not replay of a historical SDK send plan. Even legitimate subsequent
    check progress changes the source point and requires a new packet. Future
    send-plan recovery must preserve the sources as they existed at dispatch.
    """
    try:
        checked = WorkingContextPacket.model_validate_json(packet.model_dump_json())
        if checked.max_bytes != max_bytes:
            raise ValueError("working context frozen byte budget differs")
        focus = WorkingContextFocus.model_validate_json(canonical(checked.contents()["focus"]))
        rebuilt = build_working_context(workspace, focus, task_directory=task_directory, ledger=ledger, max_bytes=max_bytes)
        if checked != rebuilt:
            raise WorkingContextError("working_context_frozen_mismatch")
        return checked
    except WorkingContextError:
        raise
    except (ValueError, KeyError, TypeError, UnicodeError, RecursionError) as exc:
        raise WorkingContextError("working_context_source_invalid") from exc
