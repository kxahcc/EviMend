"""Operation-local receipt reuse still closes exact raw sources before success.

Small serialized receipts isolate the optimization from SDK execution. Only
expensive plan/fact reconstruction is stubbed; the production receipt parser,
inventory, byte reads, FD/path identity checks and public finalizers run normally.
These fixtures cannot qualify a Skill or establish application evidence.
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from hashlib import sha256 as byte_sha256
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from dfx_agent import skill_validation as validation_implementation
from dfx_agent.evidence_io import local_evidence_path
from dfx_agent.feedback import _closure_directory_anchor, _ReadBudget
from dfx_agent.feedback_contracts import (
    FeedbackFile,
    FeedbackLimits,
    content_hash,
    encode,
)
from dfx_agent.model_calls import canonical, sha256
from dfx_agent.skill_evolution import (
    SkillTransitionStore,
    _EvolutionBudget,
    _PlanReadView,
)
from dfx_agent.skill_evolution_contracts import (
    HostSkillVersionApproval,
    HostValidationPlanApproval,
    ReceiptPayload,
    SkillValidationReceipt,
)
from dfx_agent.skill_transition_contracts import (
    HostPublicationApproval,
    HostTransitionApproval,
    PublicationPayload,
    SkillTransition,
    TransitionPayload,
)
from dfx_agent.skill_validation import SkillValidationStore
from dfx_agent.working_context_materials import _directory_anchor


@dataclass
class ReceiptCase:
    validation: SkillValidationStore
    approval: HostValidationPlanApproval
    receipt: SkillValidationReceipt
    source: Path
    metadata: Path
    observations: list[Path]

    @property
    def initial_bytes(self) -> int:
        return self.source.stat().st_size + self.metadata.stat().st_size


@pytest.fixture
def receipt_case(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ReceiptCase:
    root = local_evidence_path(tmp_path)
    validation = SkillValidationStore(root)
    old = HostSkillVersionApproval(approval_id="old-host", skill_id="capacity",
        projection_sha256=sha256("old-projection"), allowed_splits=("development",))
    new = HostSkillVersionApproval(approval_id="new-host", skill_id="capacity",
        projection_sha256=sha256("new-projection"), allowed_splits=("development",))
    approved = HostValidationPlanApproval(approval_id="receipt-plan-host", spec_sha256=sha256("stubbed-plan"),
        old_version_approval=old, new_version_approval=new)
    plan_id, old_id, new_id = (prefix + sha256(label) for prefix, label in
        (("skill-plan-", "plan"), ("skill-version-", "old"), ("skill-version-", "new")))
    # Plan reconstruction is an explicit test double. This suite exercises the
    # receipt source proof, rather than creating misleading validation outcomes.
    plan = SimpleNamespace(payload_json=encode({"fixture": "stubbed-plan"}))
    plan_body = SimpleNamespace(spec=SimpleNamespace(old_version_id=old_id, new_version_id=new_id))
    monkeypatch.setattr(validation_implementation, "parse_plan_payload", lambda _raw: plan_body)

    def read_plan(identifier: str, external: Any, _budget: _ReadBudget):
        assert identifier == plan_id and external == approved
        return plan

    monkeypatch.setattr(validation, "_read_plan", read_plan)
    original = b"raw-fixture\n"
    manifest = {"fixture": "no-SDK-or-application-evidence"}
    aggregate = {"rows": [{"synthetic_outcome": "unknown"}]}
    body = ReceiptPayload(plan_id=plan_id, approval_sha256=content_hash(approved), case_id="representative",
        role="old", version_id=old_id, manifest_json=canonical(manifest), aggregate_json=canonical(aggregate),
        directories=("empty",), files=(FeedbackFile(path="source.txt", sha256=byte_sha256(original).hexdigest(), bytes=len(original)),),
        target_projection_sha256=old.projection_sha256, target_skill_id="dfx-" + old.projection_sha256[:56],
        use_status="unknown", use_reasons=())
    key = content_hash(body)
    receipt = SkillValidationReceipt(receipt_id="skill-receipt-" + key, content_sha256=key, payload_json=encode(body))
    directory = root / "receipts" / receipt.receipt_id
    (directory / "payload/empty").mkdir(parents=True)
    source, metadata = directory / "payload/source.txt", directory / "receipt.json"
    source.write_bytes(original)
    metadata.write_bytes(encode(receipt).encode("utf-8"))
    observations: list[Path] = []

    def reconstruct(_self, _plan, case_id, role, evidence_root):
        assert (case_id, role) == ("representative", "old")
        assert (evidence_root / "source.txt").read_bytes() == original
        observations.append(evidence_root)
        return SimpleNamespace(model_dump=lambda **_kwargs: manifest), aggregate, "unknown", ()

    monkeypatch.setattr(SkillValidationStore, "_receipt_facts", reconstruct)
    return ReceiptCase(validation, approved, receipt, source, metadata, observations)


def _read_twice(store: SkillTransitionStore, case: ReceiptCase, budget: _EvolutionBudget):
    first = _PlanReadView(store, budget)._read_receipt(case.receipt.receipt_id, case.approval, budget)
    assert _PlanReadView(store, budget)._read_receipt(case.receipt.receipt_id, case.approval, budget) is first
    return first


def _public_reader(store: SkillTransitionStore, case: ReceiptCase, monkeypatch: pytest.MonkeyPatch):
    host = HostTransitionApproval(approval_id="read-transition", plan_id=case.receipt.contents()["plan_id"],
        receipt_ids=(case.receipt.receipt_id,))
    body = TransitionPayload(approval=host, plan_approval=case.approval,
        version_id="skill-version-" + sha256("new"), skill_id="capacity", state="synthetic_pending",
        reasons=("fixture_semantics_unknown",), facts_json=encode({}),
        history_receipt_ids=(case.receipt.receipt_id,), attempt_plan_ids=(host.plan_id,))
    key = content_hash(body)
    result = SkillTransition(transition_id="skill-transition-" + key, content_sha256=key, payload_json=encode(body))
    budgets: list[_EvolutionBudget] = []

    def read(_identifier, _approval, budget):
        budgets.append(budget)
        _read_twice(store, case, budget)
        return result

    monkeypatch.setattr(store, "_read_transition", read)
    return result, host, budgets


def test_public_reads_aggregate_once_per_operation_and_reauthenticate_next_call(
        receipt_case: ReceiptCase, monkeypatch: pytest.MonkeyPatch) -> None:
    case = receipt_case
    store = SkillTransitionStore(case.validation)
    result, host, budgets = _public_reader(store, case, monkeypatch)
    assert store.read_transition(result.transition_id, approval=host) == result
    assert len(case.observations) == 1
    assert budgets[0].used == 3 * case.initial_bytes
    assert not budgets[0].plans and not budgets[0].receipts
    assert store.read_transition(result.transition_id, approval=host) == result
    assert len(case.observations) == 2 and budgets[0] is not budgets[1]
    assert not budgets[1].plans and not budgets[1].receipts


def test_cached_receipt_cannot_change_external_approval_or_operation_budget(receipt_case: ReceiptCase) -> None:
    case = receipt_case
    store = SkillTransitionStore(case.validation)
    budget = _EvolutionBudget(store.limits.max_capture_read_bytes)
    reader = _PlanReadView(store, budget)
    assert reader._read_receipt(case.receipt.receipt_id, case.approval, budget) == case.receipt
    with pytest.raises(ValueError, match="original approval"):
        reader._read_receipt(case.receipt.receipt_id, case.approval.model_copy(update={"approval_id": "other-host"}), budget)
    with pytest.raises(ValueError, match="operation budgets"):
        reader._read_receipt(case.receipt.receipt_id, case.approval, _EvolutionBudget(budget.maximum))
    assert len(case.observations) == 1
    store._close_plans(budget)
    budget.clear()


@pytest.mark.parametrize("change", ["payload", "payload_restored", "metadata_restored", "membership"])
def test_cached_original_drift_rejects_before_publication(
        receipt_case: ReceiptCase, monkeypatch: pytest.MonkeyPatch, change: str) -> None:
    case = receipt_case
    store = SkillTransitionStore(case.validation)
    host = HostPublicationApproval(approval_id="publication-closure", alias="capacity", action="initialize_seed",
        version_id="skill-version-" + sha256("old"))
    payload = PublicationPayload(approval=host, version_approval=case.approval.old_version_approval, sequence=1,
        projection_sha256=case.approval.old_version_approval.projection_sha256)
    budgets: list[_EvolutionBudget] = []

    def publication_sources(_host, _version_host, _transition_host, _history, budget):
        budgets.append(budget)
        _read_twice(store, case, budget)
        if len(budgets) == 2:
            if change == "membership":
                (case.source.parent / "unregistered.txt").write_bytes(b"extra source")
            else:
                path = case.metadata if change == "metadata_restored" else case.source
                original, observed = path.read_bytes(), path.stat()
                path.write_bytes(original + b"unapproved change")
                if change.endswith("restored"):
                    path.write_bytes(original)
                    # Retain the old size and mtime. FD/change-time authentication
                    # must still reject a transient edit with restored bytes.
                    os.utime(path, ns=(observed.st_atime_ns, observed.st_mtime_ns))
                    assert path.read_bytes() == original and path.stat().st_size == observed.st_size
        return payload

    monkeypatch.setattr(store, "_publication_payload", publication_sources)
    with pytest.raises(ValueError, match="(original|membership|descriptor).*changed"):
        store.publish_alias(approval=host, version_approval=case.approval.old_version_approval)
    assert len(case.observations) == 1
    assert budgets[0] is budgets[1] and not budgets[0].plans and not budgets[0].receipts
    assert not (store.root / "publications").exists()


def test_final_receipt_closure_still_charges_and_enforces_the_original_read_budget(
        receipt_case: ReceiptCase, monkeypatch: pytest.MonkeyPatch) -> None:
    case = receipt_case
    # Exactly enough for initial scan and its semantic-close rereads; the final
    # operation close must fail rather than bypassing or extending this limit.
    maximum = 2 * case.initial_bytes
    case.validation.limits = FeedbackLimits(max_capture_read_bytes=maximum)
    store = SkillTransitionStore(case.validation)
    result, host, budgets = _public_reader(store, case, monkeypatch)
    with pytest.raises(ValueError, match="cumulative capture read capacity"):
        store.read_transition(result.transition_id, approval=host)
    assert len(case.observations) == 1 and budgets[0].used == maximum
    assert not budgets[0].plans and not budgets[0].receipts


def _directory_attributes(monkeypatch: pytest.MonkeyPatch, directory: Path, state: dict[str, int]) -> None:
    """Simulate the exact observed stat attribute difference on one directory."""
    original_lstat = Path.lstat

    def observe(path: Path, *args: Any, **kwargs: Any):
        info = original_lstat(path, *args, **kwargs)
        if path != directory:
            return info
        return SimpleNamespace(st_dev=info.st_dev, st_ino=info.st_ino, st_mode=info.st_mode,
            st_size=info.st_size, st_nlink=info.st_nlink,
            st_file_attributes=(getattr(info, "st_file_attributes", 0) & ~0x10000000) | state["bits"])

    monkeypatch.setattr(Path, "lstat", observe)


def test_empty_directory_high_ntfs_bit_flip_keeps_ephemeral_closure_only(
        receipt_case: ReceiptCase, monkeypatch: pytest.MonkeyPatch) -> None:
    case = receipt_case
    empty = case.source.parent / "empty"
    state = {"bits": 0x10000000}
    _directory_attributes(monkeypatch, empty, state)
    persistent = _directory_anchor(empty.lstat())
    store = SkillTransitionStore(case.validation)
    budget = _EvolutionBudget(store.limits.max_capture_read_bytes)
    _read_twice(store, case, budget)
    state["bits"] = 0
    observed = _directory_anchor(empty.lstat())
    assert persistent[:3] == observed[:3] and persistent[3] ^ observed[3] == 0x10000000
    # The persistent helper retains the difference. Only the raw reader's
    # operation-local directory comparison treats this directory bit equally.
    store._close_plans(budget)
    assert len(case.observations) == 1
    budget.clear()


def test_empty_directory_replacement_with_same_members_keeps_identity_rejection(receipt_case: ReceiptCase) -> None:
    case = receipt_case
    empty = case.source.parent / "empty"
    original = _directory_anchor(empty.lstat())
    store = SkillTransitionStore(case.validation)
    budget = _EvolutionBudget(store.limits.max_capture_read_bytes)
    _read_twice(store, case, budget)
    empty.rename(store.root / "retained-original-empty")
    empty.mkdir()
    assert not list(empty.iterdir()) and _directory_anchor(empty.lstat())[:2] != original[:2]
    with pytest.raises(ValueError, match="ancestor changed during closure"):
        store._close_plans(budget)
    budget.clear()


def test_reparse_attribute_is_retained_and_rejected_after_directory_bit_normalization(
        receipt_case: ReceiptCase, monkeypatch: pytest.MonkeyPatch) -> None:
    case = receipt_case
    empty = case.source.parent / "empty"
    state = {"bits": 0}
    _directory_attributes(monkeypatch, empty, state)
    store = SkillTransitionStore(case.validation)
    budget = _EvolutionBudget(store.limits.max_capture_read_bytes)
    _read_twice(store, case, budget)
    state["bits"] = 0x10000000 | stat.FILE_ATTRIBUTE_REPARSE_POINT
    assert _closure_directory_anchor(empty.lstat())[3] & stat.FILE_ATTRIBUTE_REPARSE_POINT
    # The high attribute bit on a regular file is outside this normalization.
    regular = SimpleNamespace(st_dev=1, st_ino=2, st_mode=stat.S_IFREG, st_file_attributes=0x10000000)
    assert _closure_directory_anchor(cast(os.stat_result, regular))[3] == 0x10000000
    with pytest.raises(ValueError, match="evidence path is linked"):
        store._close_plans(budget)
    budget.clear()


def test_other_directory_attribute_drift_still_rejects_with_diagnostics(
        receipt_case: ReceiptCase, monkeypatch: pytest.MonkeyPatch) -> None:
    case = receipt_case
    empty = case.source.parent / "empty"
    state = {"bits": 0}
    _directory_attributes(monkeypatch, empty, state)
    store = SkillTransitionStore(case.validation)
    budget = _EvolutionBudget(store.limits.max_capture_read_bytes)
    _read_twice(store, case, budget)
    state["bits"] = 0x10000000 | stat.FILE_ATTRIBUTE_HIDDEN
    with pytest.raises(ValueError, match="ancestor changed during closure") as rejected:
        store._close_plans(budget)
    assert "expected=" in str(rejected.value) and "observed=" in str(rejected.value)
    budget.clear()
