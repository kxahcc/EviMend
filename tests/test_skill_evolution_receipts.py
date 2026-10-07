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
    FrozenSkillSelection,
    FrozenSkillSelectionV2,
    HostPublicationApproval,
    HostPublicationApprovalV2,
    HostTransitionApproval,
    HostTransitionApprovalV2,
    PublicationPayload,
    PublicationPayloadV2,
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
    case.validation.limits = FeedbackLimits(max_capture_read_bytes=2 * case.initial_bytes)
    store = SkillTransitionStore(case.validation)
    result, host, budgets = _public_reader(store, case, monkeypatch)
    assert store.read_transition(result.transition_id, approval=host) == result
    assert len(case.observations) == 1
    assert budgets[0].used == 2 * case.initial_bytes
    assert not budgets[0].active and not budgets[0].plans and not budgets[0].receipts
    assert store.read_transition(result.transition_id, approval=host) == result
    assert len(case.observations) == 2 and budgets[0] is not budgets[1]
    assert budgets[1].used == 2 * case.initial_bytes
    assert not budgets[1].active and not budgets[1].plans and not budgets[1].receipts


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
    with pytest.raises(ValueError, match="operation budgets"):
        reader._read_receipt(case.receipt.receipt_id, case.approval, budget)
    with pytest.raises(ValueError, match="completed operation budget"):
        store._plan_once(case.receipt.contents()["plan_id"], case.approval, budget)


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
    # Exactly enough for the initial scan. The deferred final operation close
    # must fail rather than bypassing or extending the original read limit.
    maximum = case.initial_bytes
    case.validation.limits = FeedbackLimits(max_capture_read_bytes=maximum)
    store = SkillTransitionStore(case.validation)
    result, host, budgets = _public_reader(store, case, monkeypatch)
    with pytest.raises(ValueError, match="cumulative capture read capacity"):
        store.read_transition(result.transition_id, approval=host)
    assert len(case.observations) == 1 and budgets[0].used == maximum
    assert not budgets[0].plans and not budgets[0].receipts


@pytest.mark.parametrize("mode", ["ordinary", "evolution"])
def test_drift_during_fact_reconstruction_cannot_escape_original_closure(
        receipt_case: ReceiptCase, monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    case = receipt_case
    reconstruct = SkillValidationStore._receipt_facts

    def changed_facts(*args: Any, **kwargs: Any):
        facts = reconstruct(*args, **kwargs)
        case.source.write_bytes(b"drift after semantic read\n")
        return facts

    monkeypatch.setattr(SkillValidationStore, "_receipt_facts", changed_facts)
    budgets: list[_EvolutionBudget] = []
    with pytest.raises(ValueError, match="original size changed"):
        if mode == "ordinary":
            # The standalone public receipt reader has not delegated closure.
            case.validation.read_receipt(case.receipt.receipt_id, approval=case.approval)
        else:
            store = SkillTransitionStore(case.validation)
            result, host, budgets = _public_reader(store, case, monkeypatch)
            store.read_transition(result.transition_id, approval=host)
    assert len(case.observations) == 1
    if mode == "evolution":
        assert not budgets[0].active and not budgets[0].plans and not budgets[0].receipts


@pytest.mark.parametrize("mode", ["deployment", "export"])
def test_last_deployment_or_export_source_read_is_closed_before_success(
        receipt_case: ReceiptCase, monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    case = receipt_case
    store = SkillTransitionStore(case.validation)
    host = HostPublicationApproval(approval_id="final-source-read", alias="capacity", action="initialize_seed",
        version_id="skill-version-" + sha256("old"))
    selection = FrozenSkillSelection(alias=host.alias, publication_id="skill-publication-" + sha256("publication"),
        version_id=host.version_id, projection_sha256=case.approval.old_version_approval.projection_sha256,
        split=host.split)
    budgets: list[_EvolutionBudget] = []

    def last_read(_value: Any, _host: Any, budget: _EvolutionBudget):
        budgets.append(budget)
        _read_twice(store, case, budget)
        if len(budgets) == 2:
            case.source.write_bytes(b"drift in final source audit\n")
        return selection

    if mode == "deployment":
        monkeypatch.setattr(store, "_freeze_alias", last_read)
        monkeypatch.setattr(store, "_activation_head", lambda *_args: None)
        monkeypatch.setattr(store, "_selected_model_head", lambda *_args: None)
        monkeypatch.setattr(store, "_read_selection", lambda *_args: None)
        monkeypatch.setattr(store, "_chain", lambda name, *_args, **_kwargs: [case.metadata] if name == "publications" else [])
        monkeypatch.setattr(store, "_read", lambda *_args: SimpleNamespace(publication_id=selection.publication_id))
    else:
        monkeypatch.setattr(store, "_read_selection", last_read)
        monkeypatch.setattr(store, "_publications", lambda *_args, **_kwargs: [])
    with pytest.raises(ValueError, match="original size changed"):
        if mode == "deployment":
            store.assert_current_selection(selection, publication_approval=host)
        else:
            store.export_selection_bundle(selection, publication_approval=host)
    assert len(budgets) == 2 and budgets[0] is budgets[1] and len(case.observations) == 1
    assert not budgets[0].active and not budgets[0].plans and not budgets[0].receipts


@pytest.mark.parametrize("changed_registry", ["publications", "transitions"])
def test_current_deployment_rejects_head_advance_during_final_receipt_close(
        receipt_case: ReceiptCase, monkeypatch: pytest.MonkeyPatch, changed_registry: str) -> None:
    case = receipt_case
    store = SkillTransitionStore(case.validation)
    host = HostPublicationApprovalV2(approval_id="closing-head", alias="capacity", action="rollback",
        version_id="skill-version-" + sha256("selected-model"),
        previous_publication_id="skill-publication-" + sha256("prior"))
    selection = FrozenSkillSelectionV2(alias=host.alias, publication_id="skill-publication-" + sha256("head"),
        version_id=host.version_id, projection_sha256=case.approval.old_version_approval.projection_sha256,
        split=host.split)
    budgets: list[_EvolutionBudget] = []
    eligible = SimpleNamespace(transition_id="skill-transition-" + sha256("authenticated-eligible"))
    original_transition = store.root / "authenticated-eligible.json"
    chains: dict[str, list[Path]] = {"publications": [case.metadata], "transitions": [original_transition]}

    def consume(_alias: str, _approval: Any, budget: _EvolutionBudget):
        budgets.append(budget)
        _read_twice(store, case, budget)
        return selection

    monkeypatch.setattr(store, "_freeze_alias", consume)
    monkeypatch.setattr(store, "_activation_head", lambda *_args: None)
    # Eligibility reconstruction is stubbed, as in the other source-proof
    # fixtures. The selected model's previously authenticated head is retained.
    monkeypatch.setattr(store, "_selected_model_head", lambda *_args: eligible)
    monkeypatch.setattr(store, "_read_selection", lambda *_args: None)
    monkeypatch.setattr(store, "_chain", lambda name, *_args, **_kwargs: list(chains[name]))
    monkeypatch.setattr(store, "_read", lambda path, *_args: eligible if path == original_transition else
                        SimpleNamespace(publication_id=selection.publication_id))
    close = store._close_plans

    def advance_during_close(budget: _EvolutionBudget):
        # The original receipt still closes successfully; a later event makes
        # the previously authenticated deployment head stale during that close.
        tree = budget.receipts[case.receipt.receipt_id][2]
        finish = tree.finish

        def advance():
            finish()
            later = "later-rejected-transition.json" if changed_registry == "transitions" else "later-publication.json"
            chains[changed_registry].append(store.root / later)

        monkeypatch.setattr(tree, "finish", advance)
        close(budget)

    monkeypatch.setattr(store, "_close_plans", advance_during_close)
    with pytest.raises(ValueError, match="registry changed during source closure"):
        store.assert_current_selection(selection, publication_approval=host)
    assert len(case.observations) == 1 and budgets[0].used == 2 * case.initial_bytes
    assert not budgets[0].active and not budgets[0].plans and not budgets[0].receipts


def test_activation_cannot_publish_after_rejected_transition_is_appended_during_receipt_close(
        receipt_case: ReceiptCase, monkeypatch: pytest.MonkeyPatch) -> None:
    case = receipt_case
    store = SkillTransitionStore(case.validation)
    prior = HostPublicationApproval(approval_id="activation-parent", alias="capacity", action="initialize_seed",
        version_id="skill-version-" + sha256("old"))
    previous_id = "skill-publication-" + sha256("previous")
    transition_id = "skill-transition-" + sha256("eligible")
    transition_host = HostTransitionApprovalV2(approval_id="qualifying-transition",
        plan_id=case.receipt.contents()["plan_id"], receipt_ids=(case.receipt.receipt_id,))
    host = HostPublicationApprovalV2(approval_id="activating-model", alias=prior.alias, action="activate",
        version_id="skill-version-" + sha256("new"), transition_id=transition_id,
        previous_publication_id=previous_id)
    payload = PublicationPayloadV2(approval=host, version_approval=case.approval.new_version_approval,
        transition_approval=transition_host, previous_approval=prior, sequence=2,
        projection_sha256=case.approval.new_version_approval.projection_sha256)
    budgets: list[_EvolutionBudget] = []
    old_publication = store.root / "previous-publication.json"
    eligible_path = store.root / "eligible-transition.json"
    chains: dict[str, list[Path]] = {"publications": [old_publication], "transitions": [eligible_path]}

    def publication_sources(_host: Any, _version: Any, _transition: Any, _prior: Any, budget: _EvolutionBudget):
        budgets.append(budget)
        _read_twice(store, case, budget)
        return payload

    monkeypatch.setattr(store, "_publication_payload", publication_sources)
    monkeypatch.setattr(store, "_publications", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(store, "_chain", lambda name, *_args, **_kwargs: list(chains[name]))
    monkeypatch.setattr(store, "_read", lambda path, *_args: SimpleNamespace(transition_id=transition_id)
                        if path == eligible_path else SimpleNamespace(publication_id=previous_id))
    close = store._close_plans

    def advance_during_close(budget: _EvolutionBudget):
        tree = budget.receipts[case.receipt.receipt_id][2]
        finish = tree.finish

        def reject():
            finish()
            chains["transitions"].append(store.root / "later-rejected-transition.json")

        monkeypatch.setattr(tree, "finish", reject)
        close(budget)

    monkeypatch.setattr(store, "_close_plans", advance_during_close)
    writes: list[Any] = []
    monkeypatch.setattr(store, "_publish", lambda *args: writes.append(args))
    with pytest.raises(ValueError, match="registry changed during source closure"):
        store.publish_alias(approval=host, version_approval=case.approval.new_version_approval,
                            transition_approval=transition_host)
    assert not writes and not (store.root / "publications").exists()
    assert len(case.observations) == 1 and budgets[0].used == 2 * case.initial_bytes
    assert not budgets[0].active and not budgets[0].plans and not budgets[0].receipts


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
