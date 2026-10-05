"""Permission, byte closure and non-promoted host proposal boundaries."""

from __future__ import annotations

import copy
import json
import os
import shutil
from pathlib import Path
from typing import Any

import pytest
from test_agent_loop import response
from test_knowledge_contracts import guidance
from test_metrics import make_task, one_manifest, read_json, run_synthetic, save_json

from dfx_agent import feedback
from dfx_agent.evidence_io import local_evidence_path
from dfx_agent.feedback import FeedbackStore
from dfx_agent.feedback_contracts import (
    ExperienceCandidate,
    FeedbackCapsule,
    FeedbackLimits,
    HostCandidateApproval,
    HostFeedbackApproval,
    content_hash,
    encode,
)
from dfx_agent.knowledge_contracts import LimitedValidationScope, SolverProjection
from dfx_agent.metrics import parse_evaluation_manifest
from dfx_agent.model_calls import canonical, sha256
from dfx_agent.task import CaseSelection, LogReference


def tree(root: Path) -> dict[str, bytes]:
    root = local_evidence_path(root)
    return {path.relative_to(root).as_posix(): path.read_bytes() for path in root.rglob("*") if path.is_file()}


def approved(manifest) -> HostFeedbackApproval:
    return HostFeedbackApproval(approval_id="dev-host", manifest_sha256=sha256(canonical(manifest.model_dump(mode="json"))),
                                allowed_splits=("development",))


@pytest.fixture(scope="module")
def stopped(tmp_path_factory: pytest.TempPathFactory):
    root = tmp_path_factory.mktemp("fb")
    task = make_task(root / "t")
    record = read_json(task / "task.json")
    selection = CaseSelection(task_id=record["task_id"], case_key="cases/dev", lineage_group_id="family-dev",
        fault_variant_id="fault-dev", app="app", log=LogReference(path="log.txt", sha256=record["crash_sha256"]))
    record.update(schema_version=2, case_identity_sha256=selection.sha256())
    save_json(task / "task.json", record)
    work = root / "r"
    run_synthetic(task, work, "r", [response(1)])
    raw = one_manifest(root, task, work, "r", selection=selection.model_dump(mode="json"))
    manifest = parse_evaluation_manifest(raw)
    return root, manifest, approved(manifest)


@pytest.fixture(scope="module")
def captured(stopped, tmp_path_factory: pytest.TempPathFactory):
    source, manifest, approval = stopped
    store = FeedbackStore(tmp_path_factory.mktemp("fs"))
    capsule = store.capture(manifest, source, approval=approval)
    return store, capsule, approval


@pytest.mark.parametrize("schema", [True, 1.0, "1", 2])
def test_approval_rejects_coerced_schema_numbers(schema: Any) -> None:
    with pytest.raises(ValueError):
        HostFeedbackApproval(schema_version=schema, approval_id="host", manifest_sha256="a" * 64,
                             allowed_splits=("development",))


@pytest.mark.parametrize("permission", [1, 0, False, "true", None])
def test_approval_requires_actual_true_boolean(permission: Any) -> None:
    with pytest.raises(ValueError):
        HostFeedbackApproval(approval_id="host", manifest_sha256="a" * 64, allow_learning=permission,
                             allowed_splits=("development",))


@pytest.mark.parametrize("origin", ["curated", "candidate_validation", "confirmation"])
def test_learning_origin_cannot_be_reassigned(origin: str) -> None:
    with pytest.raises(ValueError):
        HostFeedbackApproval(approval_id="host", manifest_sha256="a" * 64, origin_split=origin,
                             allowed_splits=("development",))


def test_constructor_and_missing_read_create_nothing(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        FeedbackStore(tmp_path / "missing")
    assert not (tmp_path / "missing").exists()
    store = FeedbackStore(tmp_path)
    before = list(tmp_path.iterdir())
    approval = HostFeedbackApproval(approval_id="host", manifest_sha256="a" * 64, allowed_splits=("development",))
    with pytest.raises(ValueError):
        store.read_capsule("feedback-" + "a" * 64, approval=approval)
    assert list(tmp_path.iterdir()) == before


def test_capture_is_idempotent_and_constructor_read_are_pure(captured) -> None:
    store, capsule, approval = captured
    before = tree(store.root)
    assert FeedbackStore(store.root).read_capsule(capsule.capsule_id, approval=approval) == capsule
    assert tree(store.root) == before
    value = capsule.contents()
    value["files"][0]["sha256"] = "f" * 64
    assert capsule.contents()["files"][0]["sha256"] != "f" * 64


@pytest.mark.parametrize("change", ["hash", "approval_id", "scopes"])
def test_read_requires_exact_original_external_approval(captured, change: str) -> None:
    store, capsule, approval = captured
    altered = approval.model_dump(mode="json")
    if change == "hash":
        altered["manifest_sha256"] = "b" * 64
    elif change == "approval_id":
        altered["approval_id"] = "other-host"
    else:
        altered["allowed_splits"].append("confirmation")
    before = tree(store.root)
    with pytest.raises(ValueError):
        store.read_capsule(capsule.capsule_id, approval=HostFeedbackApproval.model_validate_json(canonical(altered)))
    assert tree(store.root) == before


@pytest.mark.parametrize("bypass", ["schema", "permission", "literal"])
def test_api_revalidates_model_copy_approval(stopped, tmp_path: Path, bypass: str) -> None:
    source, manifest, approval = stopped
    changes = {"schema_version": True} if bypass == "schema" else {"allow_learning": 1 if bypass == "literal" else False}
    unchecked = approval.model_copy(update=changes)
    store = FeedbackStore(tmp_path)
    with pytest.raises(ValueError):
        store.capture(manifest, source, approval=unchecked)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("bound", ["file", "total", "entries", "reads"])
def test_capture_capacity_rejects_before_publication(stopped, tmp_path: Path, bound: str) -> None:
    source, manifest, approval = stopped
    changes = {"max_" + {"file": "file_bytes", "total": "total_bytes", "entries": "entries",
                        "reads": "capture_read_bytes"}[bound]: 1}
    store = FeedbackStore(tmp_path, limits=FeedbackLimits(**changes))
    before = tree(source)
    with pytest.raises(ValueError):
        store.capture(manifest, source, approval=approval)
    assert tree(source) == before and not list(tmp_path.iterdir())


def cloned_capsule(tmp_path: Path, captured):
    original, capsule, approval = captured
    root = tmp_path / "s"
    shutil.copytree(original.root, root)
    store = FeedbackStore(root)
    directory = local_evidence_path(store.root / "capsules" / capsule.capsule_id)
    return store, capsule, approval, directory


@pytest.mark.parametrize("change", ["bytes", "extra", "empty_dir", "record", "shared"])
def test_exact_capsule_inventory_and_record_reject_tampering(captured, tmp_path: Path, change: str) -> None:
    store, capsule, approval, directory = cloned_capsule(tmp_path, captured)
    payload = directory / "payload"
    path = payload / capsule.contents()["files"][0]["path"]
    if change == "bytes":
        before = path.stat()
        raw = path.read_bytes()
        path.write_bytes(bytes([raw[0] ^ 1]) + raw[1:])
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    elif change == "extra":
        (payload / "extra.txt").write_bytes(b"extra")
    elif change == "empty_dir":
        (payload / "unexpected-empty").mkdir()
    elif change == "shared":
        os.link(path, tmp_path / "shared")
    else:
        record = json.loads((directory / "capsule.json").read_bytes())
        changed = json.loads(record["payload_json"])
        changed["application_qualification"] = "verified"
        record["payload_json"] = canonical(changed)
        (directory / "capsule.json").write_text(canonical(record), encoding="utf-8")
    before = tree(store.root)
    with pytest.raises(ValueError):
        store.read_capsule(capsule.capsule_id, approval=approval)
    assert tree(store.root) == before


def test_missing_report_is_retained_as_unknown_not_filled(stopped, tmp_path: Path) -> None:
    source, manifest, approval = stopped
    evidence = tmp_path / "e"
    shutil.copytree(source, evidence)
    (evidence / "r/agent-report.json").unlink()
    root = tmp_path / "s"
    root.mkdir()
    store = FeedbackStore(root)
    capsule = store.capture(manifest, evidence, approval=approval)
    row = json.loads(capsule.contents()["aggregate_json"])["rows"][0]
    assert row["record_status"] == "missing" and row["missing_reason"] == "completion_missing"
    assert row["cost"] is None
    assert not (evidence / "r/agent-report.json").exists()
    assert store.read_capsule(capsule.capsule_id, approval=approval) == capsule


def host_projection(**changes: Any) -> SolverProjection:
    return SolverProjection(guidance=guidance(), derivation_authority="host_curated", **changes)


def proposal_approval(capsule, projection) -> HostCandidateApproval:
    return HostCandidateApproval(approval_id="proposal-host", capsule_id=capsule.capsule_id,
                                 projection_sha256=sha256(canonical(projection.model_dump(mode="json"))))


def test_host_candidate_remains_pending_and_has_no_model_or_application_qualification(captured) -> None:
    store, capsule, approval = captured
    projection = host_projection()
    host = proposal_approval(capsule, projection)
    candidate = store.propose_candidate(capsule.capsule_id, projection, approval=host, feedback_approval=approval)
    value = candidate.contents()
    assert value["capsule_id"] == capsule.capsule_id and value["projection"] == projection.model_dump(mode="json")
    assert value["status"] == "pending_host_review" and value["application_qualification"] == "none"
    assert value["learning_model_call_status"] == "not_run"
    assert value["learning_tokens"] is value["learning_monetary_cost"] is None
    before = tree(store.root)
    assert store.propose_candidate(capsule.capsule_id, projection, approval=host, feedback_approval=approval) == candidate
    assert store.read_candidate(candidate.candidate_id, approval=host, feedback_approval=approval) == candidate
    assert tree(store.root) == before
    assert not (store.root / "entries").exists() and not (store.root / "snapshots").exists()
    value["projection"]["guidance"]["trigger"] = "Changed outside record."
    assert candidate.contents()["projection"]["guidance"]["trigger"] != value["projection"]["guidance"]["trigger"]


@pytest.mark.parametrize("change", ["inferred", "scopes", "hash", "capsule"])
def test_candidate_requires_explicit_host_body_and_no_generalized_validation(captured, tmp_path: Path, change: str) -> None:
    store, capsule, approval, _directory = cloned_capsule(tmp_path, captured)
    projection = host_projection()
    if change == "inferred":
        projection = SolverProjection(guidance=guidance(), derivation_authority="model_inference")
    elif change == "scopes":
        projection = host_projection(validation_scopes=(LimitedValidationScope(level="synthetic_fixture",
            result="passed", scope_names=("generalized-test",)),))
    host = proposal_approval(capsule, projection)
    if change in ("hash", "capsule"):
        host = host.model_copy(update={"projection_sha256": "c" * 64} if change == "hash" else
                              {"capsule_id": "feedback-" + "c" * 64})
    before = tree(store.root)
    with pytest.raises(ValueError):
        store.propose_candidate(capsule.capsule_id, projection, approval=host, feedback_approval=approval)
    assert tree(store.root) == before


def test_candidate_content_hash_and_pending_state_cannot_be_rewritten(captured, tmp_path: Path) -> None:
    store, capsule, approval, _directory = cloned_capsule(tmp_path, captured)
    projection = host_projection()
    host = proposal_approval(capsule, projection)
    candidate = store.propose_candidate(capsule.capsule_id, projection, approval=host, feedback_approval=approval)
    original = candidate.model_dump(mode="json")
    for key, value in (("status", "activated"), ("learning_tokens", 0), ("application_qualification", "passed")):
        altered = copy.deepcopy(candidate.contents())
        altered[key] = value
        modified = {**original, "payload_json": canonical(altered)}
        with pytest.raises(ValueError):
            ExperienceCandidate.model_validate_json(canonical(modified))
    path = local_evidence_path(store.root / "candidates" / (candidate.candidate_id + ".json"))
    path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(ValueError):
        store.read_candidate(candidate.candidate_id, approval=host, feedback_approval=approval)


def test_capture_detects_change_after_aggregation_before_publication(stopped, tmp_path: Path,
                                                                  monkeypatch: pytest.MonkeyPatch) -> None:
    source, manifest, approval = stopped
    evidence = tmp_path / "e"
    shutil.copytree(source, evidence)
    root = tmp_path / "s"
    root.mkdir()
    store = FeedbackStore(root)
    original = feedback._aggregate

    def change_after_aggregate(*args: Any, **kwargs: Any):
        result = original(*args, **kwargs)
        path = local_evidence_path(evidence / "r/run-state.json")
        info = path.stat()
        raw = path.read_bytes()
        path.write_bytes(raw.replace(b"finished", b"canceled"))
        os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns))
        return result

    monkeypatch.setattr(feedback, "_aggregate", change_after_aggregate)
    with pytest.raises(ValueError):
        store.capture(manifest, evidence, approval=approval)
    assert not list(root.iterdir())


def test_capsule_canonical_record_cannot_be_rehashed_into_another_identity(captured) -> None:
    _store, capsule, _approval = captured
    data = capsule.model_dump(mode="json")
    data["content_sha256"] = "d" * 64
    with pytest.raises(ValueError):
        FeedbackCapsule.model_validate_json(canonical(data))
    assert content_hash(json.loads(capsule.payload_json)) == capsule.content_sha256
    assert encode(capsule) == canonical(capsule.model_dump(mode="json"))


def test_store_can_share_repository_anchor_without_overlapping_declared_inputs(stopped, tmp_path: Path) -> None:
    source, manifest, approval = stopped
    repo = tmp_path / "repo"
    shutil.copytree(source, repo)
    managed = repo / ".dfx-local/feedback"
    managed.mkdir(parents=True)
    store = FeedbackStore(managed)
    before = tree(repo / "t")
    capsule = store.capture(manifest, repo, approval=approval)
    assert tree(repo / "t") == before
    assert store.read_capsule(capsule.capsule_id, approval=approval) == capsule


@pytest.mark.parametrize("target", ["t/input", "r", "r/nested"])
def test_store_cannot_overlap_declared_task_or_work(stopped, tmp_path: Path, target: str) -> None:
    source, manifest, approval = stopped
    repo = tmp_path / "repo"
    shutil.copytree(source, repo)
    destination = repo / target
    destination.mkdir(parents=True, exist_ok=True)
    store = FeedbackStore(destination)
    before = tree(repo)
    with pytest.raises(ValueError, match="declared input trees"):
        store.capture(manifest, repo, approval=approval)
    assert tree(repo) == before


@pytest.mark.parametrize("missing", ["session", "profile"])
def test_knowledge_residual_cannot_be_treated_as_unapproved_no_knowledge_run(
        stopped, tmp_path: Path, missing: str) -> None:
    source, manifest, approval = stopped
    evidence = tmp_path / "e"
    shutil.copytree(source, evidence)
    work = evidence / "r"
    (work / "knowledge").mkdir()
    if missing == "session":
        (work / "session.json").unlink()
    # Exercise the ancestry guard directly: aggregate rejection of a particular
    # newer namespace must not be its only protection against missing ancestry.
    before = tree(evidence)
    with pytest.raises(ValueError, match="authenticated source"):
        feedback._learning_ancestors(manifest, local_evidence_path(evidence), approval)
    assert tree(evidence) == before


def test_capsule_read_budget_is_shared_between_metadata_and_payload(captured) -> None:
    store, capsule, approval = captured
    record_bytes = len(encode(capsule).encode("utf-8"))
    originals = sum(item["bytes"] for item in capsule.contents()["files"])
    # Each isolated reader would fit. Their complete combined reads do not.
    limit = 2 * max(record_bytes, originals)
    limited = FeedbackStore(store.root, limits=FeedbackLimits(max_capture_read_bytes=limit))
    before = tree(store.root)
    with pytest.raises(ValueError, match="cumulative capture read"):
        limited.read_capsule(capsule.capsule_id, approval=approval)
    assert tree(store.root) == before


def test_capture_read_budget_is_shared_across_original_staging_and_publication(stopped, tmp_path: Path) -> None:
    source, manifest, approval = stopped
    originals = sum(path.stat().st_size for prefix in ("t", "r") for path in (source / prefix).rglob("*")
                    if path.is_file() and not path.name.startswith("checkpoints.sqlite"))
    store = FeedbackStore(tmp_path, limits=FeedbackLimits(max_capture_read_bytes=3 * originals))
    before = tree(source)
    with pytest.raises(ValueError, match="cumulative capture read"):
        store.capture(manifest, source, approval=approval)
    assert tree(source) == before and not list(tmp_path.iterdir())


def test_candidate_read_budget_includes_its_referenced_capsule(captured, tmp_path: Path) -> None:
    store, capsule, approval, _directory = cloned_capsule(tmp_path, captured)
    projection = host_projection()
    host = proposal_approval(capsule, projection)
    candidate = store.propose_candidate(capsule.capsule_id, projection, approval=host, feedback_approval=approval)
    capsule_size = len(encode(capsule).encode("utf-8")) + sum(item["bytes"] for item in capsule.contents()["files"])
    limited = FeedbackStore(store.root, limits=FeedbackLimits(max_capture_read_bytes=2 * capsule_size))
    before = tree(store.root)
    with pytest.raises(ValueError, match="cumulative capture read"):
        limited.read_candidate(candidate.candidate_id, approval=host, feedback_approval=approval)
    assert tree(store.root) == before


def test_candidate_proposal_uses_one_budget_for_initial_and_final_capsule_read(captured, tmp_path: Path) -> None:
    store, capsule, approval, _directory = cloned_capsule(tmp_path, captured)
    projection = host_projection()
    host = proposal_approval(capsule, projection)
    capsule_size = len(encode(capsule).encode("utf-8")) + sum(item["bytes"] for item in capsule.contents()["files"])
    limited = FeedbackStore(store.root, limits=FeedbackLimits(max_capture_read_bytes=3 * capsule_size))
    before = tree(store.root)
    with pytest.raises(ValueError, match="cumulative capture read"):
        limited.propose_candidate(capsule.capsule_id, projection, approval=host, feedback_approval=approval)
    assert tree(store.root) == before
