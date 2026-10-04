"""A frozen run carries approved knowledge without depending on its old library."""

from __future__ import annotations

import copy
import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from test_agent_loop import task_snapshot
from test_knowledge_retrieval import admit, inventory

from dfx_agent import applicability, knowledge_run
from dfx_agent.applicability_contracts import TaskKnowledgeBinding
from dfx_agent.knowledge import KnowledgeStore
from dfx_agent.knowledge_contracts import PermissionScope, content_hash, encode
from dfx_agent.knowledge_retrieval import KnowledgeRetriever
from dfx_agent.knowledge_run import (
    BINDING_PATH,
    PROFILE_PATH,
    HostKnowledgeInput,
    RunKnowledgeError,
    normalize_run_knowledge_profile,
    normalize_shared_knowledge_profile,
    prepare_run_knowledge,
    read_run_knowledge,
    shared_knowledge_profile,
)
from dfx_agent.retrieval_contracts import MemoryQuery
from dfx_agent.task import digest, verify_task
from dfx_agent.verification import CheckLedger, FixedTextExecutor
from dfx_agent.workspace import CandidateWorkspace


@dataclass
class Inputs:
    task: Path
    workspace: CandidateWorkspace
    ledger: CheckLedger
    source: KnowledgeRetriever
    host: HostKnowledgeInput
    selected_entry: str
    other_entry: str

    def prepare(self) -> dict[str, Any]:
        profile = prepare_run_knowledge(self.workspace, self.task, self.host, ledger=self.ledger)
        assert profile is not None
        return profile

    def read(self, profile: dict[str, Any] | None):
        return read_run_knowledge(self.workspace, self.task, profile, ledger=self.ledger)


def inputs(tmp_path: Path, *, indexed: bool = True) -> Inputs:
    task = task_snapshot(tmp_path)
    workspace = CandidateWorkspace(task, tmp_path / "run", "run1")
    ledger = CheckLedger(workspace, FixedTextExecutor("src/main.ets", "value=good\n"))
    source_root = tmp_path / "external" / "store"
    source_root.mkdir(parents=True)
    knowledge = KnowledgeStore(source_root)
    selected = admit(knowledge, tmp_path / "external" / "width", "width", with_history=True)
    other = admit(knowledge, tmp_path / "external" / "offset", "offset")
    scope = PermissionScope(split="development")
    snapshot = knowledge.freeze_snapshot((selected.entry_id,), scope)
    source = KnowledgeRetriever(knowledge)
    if indexed:
        source.build_index(snapshot.snapshot_sha256, scope)
        source.record(MemoryQuery(snapshot_sha256=snapshot.snapshot_sha256, scope=scope, text="容量"))
    approval = TaskKnowledgeBinding(task_id=workspace.task_id, run_id=workspace.run_id,
        source_snapshot_sha256=workspace.source_snapshot_sha256,
        crash_sha256=verify_task(task)["crash_sha256"], snapshot_sha256=snapshot.snapshot_sha256, scope=scope)
    return Inputs(task, workspace, ledger, source, HostKnowledgeInput(source, approval),
                  selected.entry_id, other.entry_id)


def rehash(value: dict[str, Any]) -> dict[str, Any]:
    value["profile_sha256"] = content_hash({key: item for key, item in value.items() if key != "profile_sha256"})
    return value


def test_disabled_and_pure_schema_helpers_create_nothing(tmp_path: Path) -> None:
    s = inputs(tmp_path)
    before = inventory(tmp_path)
    assert prepare_run_knowledge(s.workspace, s.task, None, ledger=s.ledger) is None
    assert s.read(None) is None
    HostKnowledgeInput(s.source, s.host.approval)
    with pytest.raises(RunKnowledgeError):
        normalize_run_knowledge_profile({})
    with pytest.raises(RunKnowledgeError):
        normalize_shared_knowledge_profile({})
    assert inventory(tmp_path) == before
    assert not (s.workspace.root / "knowledge").exists()


def test_prepare_copies_only_complete_selected_originals_and_frozen_binding(tmp_path: Path) -> None:
    s = inputs(tmp_path)
    before = inventory(s.source.store.root)
    profile = s.prepare()
    assert inventory(s.source.store.root) == before
    assert profile == normalize_run_knowledge_profile(profile)
    assert json.loads((s.workspace.root / PROFILE_PATH).read_bytes()) == profile
    assert not (s.workspace.root / "knowledge" / "retrievals").exists()
    assert not (s.workspace.root / "knowledge" / "entries" / (s.other_entry + ".json")).exists()
    selected = {file["path"] for file in profile["files"]}
    actual = {"knowledge/" + path for path in inventory(s.workspace.root / "knowledge")}
    assert selected == actual | {BINDING_PATH}
    for file in profile["files"]:
        raw = (s.workspace.root / file["path"]).read_bytes()
        assert file["bytes"] == len(raw) and file["sha256"] == digest(raw)
        if file["path"].startswith("knowledge/"):
            source = s.source.store.root / file["path"].removeprefix("knowledge/")
            assert raw == source.read_bytes()
            assert (source.stat().st_dev, source.stat().st_ino) != (
                (s.workspace.root / file["path"]).stat().st_dev,
                (s.workspace.root / file["path"]).stat().st_ino)
    assert str(s.source.store.root) not in encode(profile)
    assert profile["retriever"] == json.loads(s.source.store.read_snapshot(
        s.host.approval.snapshot_sha256).payload_json)["retriever"]


def test_restore_and_query_need_no_external_library_and_audit_never_writes(tmp_path: Path) -> None:
    s = inputs(tmp_path)
    profile = s.prepare()
    shutil.rmtree(s.source.store.root.parent)
    before = inventory(tmp_path)
    local = s.read(profile)
    assert local is not None
    assert local.retriever.store.root == s.workspace.root / "knowledge"
    assert inventory(tmp_path) == before
    bound = local.record_query(tags=("ByteWidth",), text="容量")
    assert local.read_query(bound.record_id) == bound
    retrieved = local.retriever.read_record(bound.contents()["retrieval_id"])
    assert [hit["entry_id"] for hit in retrieved.contents()["hits"]] == [s.selected_entry]
    before = inventory(tmp_path)
    assert s.read(profile) is not None
    assert inventory(tmp_path) == before


def test_copied_run_reopens_with_same_profile_and_new_absolute_paths(tmp_path: Path) -> None:
    s = inputs(tmp_path)
    profile = s.prepare()
    moved = tmp_path / "package"
    moved.mkdir()
    task = moved / "task"
    work = moved / "work"
    shutil.copytree(s.task, task)
    shutil.copytree(s.workspace.root, work)
    shutil.rmtree(s.source.store.root.parent)
    workspace = CandidateWorkspace.reopen(task, work, s.workspace.run_id)
    ledger = CheckLedger(workspace, s.ledger.executor, read_only=True)
    before = inventory(moved)
    local = read_run_knowledge(workspace, task, profile, ledger=ledger)
    assert local is not None and local.retriever.store.root == work / "knowledge"
    assert inventory(moved) == before


def test_shared_identity_excludes_task_run_and_approval_original(tmp_path: Path) -> None:
    s = inputs(tmp_path)
    first = s.prepare()
    workspace = CandidateWorkspace(s.task, tmp_path / "run2", "run2")
    ledger = CheckLedger(workspace, s.ledger.executor)
    changed = s.host.approval.model_dump(mode="json")
    changed["run_id"] = "run2"
    approval = TaskKnowledgeBinding.model_validate_json(encode(changed))
    second = prepare_run_knowledge(workspace, s.task, HostKnowledgeInput(s.source, approval), ledger=ledger)
    assert second is not None and first != second
    common = shared_knowledge_profile(first)
    assert shared_knowledge_profile(second) == common
    assert set(common) == {"snapshot_sha256", "scope", "retriever", "index_sha256", "files"}
    assert all(file["path"].startswith("knowledge/") for file in common["files"])
    common["files"].clear()
    assert shared_knowledge_profile(first)["files"]


@pytest.mark.parametrize("field,value", [
    ("task_id", "another-task"), ("run_id", "another-run"),
    ("source_snapshot_sha256", "a" * 64), ("crash_sha256", "b" * 64),
    ("scope", {"schema_version": 1, "use": "solve", "split": "confirmation", "excluded_lineage_groups": []}),
])
def test_wrong_host_owner_or_scope_is_rejected_before_any_publication(
        tmp_path: Path, field: str, value: Any) -> None:
    s = inputs(tmp_path)
    changed = s.host.approval.model_dump(mode="json")
    changed[field] = value
    host = HostKnowledgeInput(s.source, TaskKnowledgeBinding.model_validate_json(encode(changed)))
    before = inventory(tmp_path)
    with pytest.raises(RunKnowledgeError):
        prepare_run_knowledge(s.workspace, s.task, host, ledger=s.ledger)
    assert inventory(tmp_path) == before
    assert not (s.workspace.root / "knowledge").exists()


def test_missing_index_does_not_build_or_fall_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    s = inputs(tmp_path, indexed=False)
    monkeypatch.setattr(KnowledgeRetriever, "build_index", lambda *_: pytest.fail("implicit index build"))
    before = inventory(tmp_path)
    with pytest.raises(RunKnowledgeError):
        s.prepare()
    assert inventory(tmp_path) == before


@pytest.mark.parametrize("prefix", ["knowledge/materials/", "knowledge/entries/", "knowledge/snapshots/",
                                    "knowledge/indexes/", "experience-bindings/"])
def test_missing_run_original_rejects_and_never_repairs(tmp_path: Path, prefix: str) -> None:
    s = inputs(tmp_path)
    profile = s.prepare()
    file = next(file for file in profile["files"] if file["path"].startswith(prefix))
    (s.workspace.root / file["path"]).unlink()
    before = inventory(tmp_path)
    with pytest.raises(RunKnowledgeError):
        s.read(profile)
    assert inventory(tmp_path) == before


@pytest.mark.parametrize("residual", ["knowledge", PROFILE_PATH, "experience-bindings", "experience-queries"])
def test_none_profile_cannot_discard_existing_or_partial_knowledge(tmp_path: Path, residual: str) -> None:
    s = inputs(tmp_path)
    path = s.workspace.root / residual
    if residual == PROFILE_PATH:
        path.write_bytes(b"partial")
    else:
        path.mkdir()
    before = inventory(tmp_path)
    with pytest.raises(RunKnowledgeError):
        s.read(None)
    with pytest.raises(RunKnowledgeError):
        prepare_run_knowledge(s.workspace, s.task, None, ledger=s.ledger)
    assert inventory(tmp_path) == before


def test_local_profile_cannot_replace_the_external_session_approval(tmp_path: Path) -> None:
    s = inputs(tmp_path)
    profile = s.prepare()
    widened = copy.deepcopy(profile)
    widened["approval"]["scope"]["split"] = "confirmation"
    rehash(widened)
    (s.workspace.root / PROFILE_PATH).write_bytes(encode(widened).encode("utf-8"))
    before = inventory(tmp_path)
    with pytest.raises(RunKnowledgeError):
        s.read(profile)
    with pytest.raises(RunKnowledgeError):
        s.read(widened)
    assert inventory(tmp_path) == before


def test_complete_manifest_rejects_omitted_or_additional_knowledge_originals(tmp_path: Path) -> None:
    s = inputs(tmp_path)
    profile = s.prepare()
    omitted = copy.deepcopy(profile)
    omitted["files"] = [file for file in omitted["files"] if not file["path"].startswith("knowledge/materials/")]
    rehash(omitted)
    (s.workspace.root / PROFILE_PATH).write_bytes(encode(omitted).encode("utf-8"))
    with pytest.raises(RunKnowledgeError):
        s.read(omitted)
    (s.workspace.root / PROFILE_PATH).write_bytes(encode(profile).encode("utf-8"))
    shutil.copyfile(s.source.store.root / "entries" / (s.other_entry + ".json"),
                    s.workspace.root / "knowledge" / "entries" / (s.other_entry + ".json"))
    before = inventory(tmp_path)
    with pytest.raises(RunKnowledgeError):
        s.read(profile)
    assert inventory(tmp_path) == before


def test_profile_shape_and_paths_are_strict_and_hash_is_not_self_authority(tmp_path: Path) -> None:
    s = inputs(tmp_path)
    profile = s.prepare()
    for changes in ({"schema_version": 2}, {"unknown": "value"}, {"profile_sha256": "a" * 64}):
        with pytest.raises(RunKnowledgeError):
            normalize_run_knowledge_profile({**profile, **changes})
    for path in ("C:/private/source.json", "../source.json", "knowledge/retrievals/old.json"):
        changed = copy.deepcopy(profile)
        changed["files"][0]["path"] = path
        with pytest.raises(RunKnowledgeError):
            normalize_run_knowledge_profile(rehash(changed))


def test_output_closure_is_reserved_before_any_copy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    s = inputs(tmp_path)
    profile = s.prepare()
    required = sum(file["bytes"] for file in profile["files"]) + len(encode(profile).encode("utf-8"))
    workspace = CandidateWorkspace(s.task, tmp_path / "fresh-run", "run1")
    ledger = CheckLedger(workspace, s.ledger.executor)
    monkeypatch.setattr(knowledge_run, "MAX_CLOSURE_BYTES", required - 1)
    before = inventory(tmp_path)
    with pytest.raises(RunKnowledgeError):
        prepare_run_knowledge(workspace, s.task, s.host, ledger=ledger)
    assert inventory(tmp_path) == before
    assert not (workspace.root / "knowledge").exists()


def test_owner_plus_knowledge_bind_capacity_is_checked_before_any_copy(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    s = inputs(tmp_path)
    owner_bytes = sum(path.stat().st_size for path in (
        s.workspace.root / "workspace.json", s.task / "task.json", s.task / "crash.log"))
    monkeypatch.setattr(applicability, "MAX_CONTEXT_BYTES", owner_bytes + 1)
    before_workspace = inventory(s.workspace.root)
    before_source = inventory(s.source.store.root)
    with pytest.raises(RunKnowledgeError):
        s.prepare()
    assert inventory(s.workspace.root) == before_workspace
    assert inventory(s.source.store.root) == before_source
    assert not (s.workspace.root / "knowledge").exists()


def test_source_change_during_copy_cannot_publish_authoritative_profile(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    s = inputs(tmp_path)
    selected = json.loads(s.source.store.read_entry(s.selected_entry).payload_json)
    source_path = s.source.store.root / "materials" / (selected["source_grants"][0]["sha256"] + ".blob")
    original = KnowledgeStore._publish
    changed = False

    def changing(store: KnowledgeStore, folder: str, filename: str, raw: bytes) -> None:
        nonlocal changed
        original(store, folder, filename, raw)
        if store.root == s.workspace.root / "knowledge" and not changed:
            info = source_path.stat()
            data = source_path.read_bytes()
            source_path.write_bytes(bytes([data[0] ^ 1]) + data[1:])
            os.utime(source_path, ns=(info.st_atime_ns, info.st_mtime_ns))
            changed = True

    monkeypatch.setattr(KnowledgeStore, "_publish", changing)
    with pytest.raises(RunKnowledgeError):
        s.prepare()
    assert changed and not (s.workspace.root / PROFILE_PATH).exists()
    with pytest.raises(RunKnowledgeError):
        s.read(None)


def test_post_copy_source_close_budget_is_checked_before_publication(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    s = inputs(tmp_path)
    reads = knowledge_run._Capture(s.source.store.root)
    contents, projections = s.source._bundle(s.host.approval.snapshot_sha256, s.host.approval.scope, reads)
    s.source._index(contents, projections, s.host.approval.snapshot_sha256, reads)
    reads.finish()
    monkeypatch.setattr(knowledge_run, "MAX_INDEX_READ_CALLS", reads.read_calls + len(reads.receipts) - 1)
    before = inventory(tmp_path)
    with pytest.raises(RunKnowledgeError):
        s.prepare()
    assert inventory(tmp_path) == before
    assert not (s.workspace.root / "knowledge").exists()


def test_existing_successful_run_is_readable_but_prepare_cannot_replace_it(tmp_path: Path) -> None:
    s = inputs(tmp_path)
    profile = s.prepare()
    before = inventory(tmp_path)
    with pytest.raises(RunKnowledgeError):
        s.prepare()
    assert s.read(profile) is not None
    assert inventory(tmp_path) == before
