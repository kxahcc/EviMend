"""Approved static procedures use native SDK loading without new authority.

These are software tests with host-curated fixtures. There is no learned Skill,
model decision, application qualification, or Agent session integration here.
"""

from __future__ import annotations

import asyncio
import copy
import json
import os
import shutil
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from langchain.agents.middleware.types import ModelRequest
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import ValidationError
from test_agent_loop import task_snapshot
from test_knowledge_retrieval import admit, guidance, inventory

from dfx_agent import static_skills as skills
from dfx_agent.applicability_contracts import TaskKnowledgeBinding
from dfx_agent.knowledge import KnowledgeStore
from dfx_agent.knowledge_contracts import (
    PermissionScope,
    SnapshotPayload,
    SolverProjection,
    encode,
)
from dfx_agent.knowledge_retrieval import KnowledgeRetriever
from dfx_agent.knowledge_run import (
    HostKnowledgeInput,
    prepare_run_knowledge,
    read_run_knowledge,
)
from dfx_agent.task import digest, verify_task
from dfx_agent.verification import CheckLedger, FixedTextExecutor
from dfx_agent.workspace import CandidateWorkspace


@pytest.fixture
def root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    # Keep Windows publication paths short, independently of test names.
    return tmp_path_factory.mktemp("ss")


def scenario(root: Path, *, both: bool = False, inference: bool = False) -> SimpleNamespace:
    task = task_snapshot(root)
    workspace = CandidateWorkspace(task, root / "run", "run1")
    ledger = CheckLedger(workspace, FixedTextExecutor("src/main.ets", "value=good\n"))
    library = root / "lib"
    library.mkdir()
    original = KnowledgeStore(library)
    first = admit(original, root / "w", "width", with_history=inference)
    second = admit(original, root / "o", "offset")
    selected = tuple(sorted((first.entry_id, second.entry_id) if both else (first.entry_id,)))
    scope = PermissionScope(split="development")
    snapshot = original.freeze_snapshot(selected, scope)
    retriever = KnowledgeRetriever(original)
    retriever.build_index(snapshot.snapshot_sha256, scope)
    approval = TaskKnowledgeBinding(task_id=workspace.task_id, run_id=workspace.run_id,
        source_snapshot_sha256=workspace.source_snapshot_sha256,
        crash_sha256=verify_task(task)["crash_sha256"], snapshot_sha256=snapshot.snapshot_sha256, scope=scope)
    knowledge = prepare_run_knowledge(workspace, task, HostKnowledgeInput(retriever, approval), ledger=ledger)
    store = read_run_knowledge(workspace, task, knowledge, ledger=ledger)
    assert store is not None
    return SimpleNamespace(root=root, task=task, workspace=workspace, ledger=ledger,
        library=library, store=store, first=first, second=second, selected=selected, snapshot=snapshot)


def prepare(s: SimpleNamespace, *, enabled: bool = True):
    policy = skills.StaticSkillPolicy(enabled=enabled, entry_ids=s.selected)
    profile = skills.prepare_run_static_skills(s.store, policy)
    assert profile is not None
    service = skills.read_run_static_skills(s.store, profile)
    assert service is not None
    return profile, service


def request(state: dict[str, Any], system: SystemMessage | None = None) -> ModelRequest:
    return ModelRequest(model=FakeListChatModel(responses=["unused"]),
        messages=[HumanMessage(content="Investigate a synthetic fault.")],
        system_message=system, tools=[], state=state)


def test_round_trip_keeps_complete_guidance_and_original_permissions(root: Path) -> None:
    s = scenario(root, both=True)
    before = inventory(s.library)
    profile, service = prepare(s)
    assert inventory(s.library) == before
    assert skills.normalize_static_skill_profile(profile) == profile
    assert json.loads((s.workspace.root / skills.PROFILE_PATH).read_bytes()) == profile
    assert SnapshotPayload.model_validate_json(s.snapshot.payload_json).skill_files == ()
    expected_guidance = {encode(guidance("width")), encode(guidance("offset"))}
    observed_guidance = set()
    for file in profile["files"]:
        body = service.body(file["skill_id"])
        raw = (s.workspace.root / file["path"]).read_bytes()
        assert body.encode() == raw and digest(raw) == file["sha256"] and len(raw) == file["bytes"]
        projection = json.loads(body.split("## Complete approved projection\n", 1)[1])
        observed_guidance.add(encode(projection["guidance"]))
        assert projection["derivation_authority"] == "host_curated"
        assert projection["epistemic_status"] == "historical_guidance_not_current_evidence"
        assert projection["validation_scopes"] == []
        for heading in ("Preconditions and original unknown questions", "Procedure and observation branches",
                        "Behaviors to preserve", "Counterexamples", "Exit conditions"):
            assert "## " + heading in body
        assert file["metadata"]["allowed_tools"] == []
        assert file["metadata"]["metadata"]["application_verification"] == "not_run"
        assert "inference_projection" not in body
    assert observed_guidance == expected_guidance
    before = inventory(root)
    assert service.catalog() == service.expected_metadata()
    reopened = skills.read_run_static_skills(s.store, copy.deepcopy(profile))
    assert reopened is not None and reopened.catalog() == service.catalog()
    assert inventory(root) == before


def test_native_on_off_load_same_catalog_and_allow_full_read(root: Path) -> None:
    a = scenario(root / "a", both=True)
    b = scenario(root / "b", both=True)
    on, on_service = prepare(a)
    off, off_service = prepare(b, enabled=False)
    assert skills.shared_static_skill_profile(on) == skills.shared_static_skill_profile(off)
    assert on_service.catalog() == off_service.catalog()
    assert not hasattr(on_service.middleware(), "tools")
    assert not hasattr(off_service.middleware(), "tools")
    for left, right in zip(on["files"], off["files"], strict=True):
        assert on_service.body(left["skill_id"]) == off_service.body(right["skill_id"])
    system = SystemMessage(content=[{"type": "text", "text": "Shared base prompt."}])
    for service, enabled in ((on_service, True), (off_service, False)):
        middleware = service.middleware()
        state = middleware.before_agent({}, None, {})
        assert state["skills_metadata"] == service.expected_metadata()
        assert state["skills_load_errors"] == []
        before = request(state, system)
        sent = middleware.modify_request(before)
        if enabled:
            assert sent is not before
            assert sent.system_message.content_blocks[-1] == {
                "type": "text", "text": "\n\n" + service.expected_section()}
            assert skills.STATIC_SKILL_MARKER in sent.system_message.text
            assert "read_skill(skill_id=" in sent.system_message.text
        else:
            assert sent is before and service.expected_section() is None
            assert skills.STATIC_SKILL_MARKER not in sent.system_message.text
        assert system.text == "Shared base prompt."
        assert sent.messages == before.messages and sent.tools == before.tools == []
        assert middleware.before_agent(state, None, {}) is None


def test_native_async_load_and_empty_system_prompt(root: Path) -> None:
    _, service = prepare(scenario(root))
    middleware = service.middleware()
    state = asyncio.run(middleware.abefore_agent({}, None, {}))
    sent = middleware.modify_request(request(state))
    assert sent.system_message.content_blocks == [{"type": "text", "text": service.expected_section()}]


@pytest.mark.parametrize("mutation", ["missing", "empty", "description", "path", "tools", "annotation", "errors"])
@pytest.mark.parametrize("enabled", [True, False])
def test_cache_is_not_an_approval_source(root: Path, mutation: str, enabled: bool) -> None:
    _, service = prepare(scenario(root), enabled=enabled)
    middleware = service.middleware()
    good = middleware.before_agent({}, None, {})
    state = copy.deepcopy(good)
    if mutation == "missing":
        state.pop("skills_metadata")
    elif mutation == "empty":
        state["skills_metadata"] = []
    elif mutation == "errors":
        state["skills_load_errors"] = ["incomplete source"]
    elif mutation == "tools":
        state["skills_metadata"][0]["allowed_tools"] = ["execute"]
    elif mutation == "annotation":
        state["skills_metadata"][0]["metadata"]["application_verification"] = "passed"
    else:
        state["skills_metadata"][0][mutation] = "unapproved value"
    before = inventory(root)
    with pytest.raises(skills.StaticSkillError):
        middleware.modify_request(request(state))
    if mutation != "missing":
        with pytest.raises(skills.StaticSkillError):
            middleware.before_agent(state, None, {})
    else:
        assert middleware.before_agent(state, None, {}) == good
    assert inventory(root) == before


@pytest.mark.parametrize("mutation", ["body", "profile", "missing", "extra_file", "extra_dir", "approved_source"])
def test_resource_drift_is_rejected_even_with_matching_cached_metadata(root: Path, mutation: str) -> None:
    s = scenario(root)
    profile, service = prepare(s)
    middleware = service.middleware()
    state = middleware.before_agent({}, None, {})
    body = s.workspace.root / profile["files"][0]["path"]
    if mutation == "body":
        old = body.stat()
        raw = body.read_bytes()
        body.write_bytes(raw.replace(b"Historical", b"Xistorical", 1))
        os.utime(body, ns=(old.st_atime_ns, old.st_mtime_ns))
    elif mutation == "profile":
        changed = copy.deepcopy(profile)
        changed["enabled"] = False
        (s.workspace.root / skills.PROFILE_PATH).write_bytes(encode(changed).encode())
    elif mutation == "missing":
        body.unlink()
    elif mutation == "extra_file":
        (body.parent / "helper.py").write_bytes(b"not allowed")
    elif mutation == "extra_dir":
        (body.parent.parent / "extra").mkdir()
    else:
        original = s.store.retriever.store.root / "entries" / (s.first.entry_id + ".json")
        original.write_bytes(original.read_bytes() + b" ")
    before = inventory(root)
    for action in (lambda: service.body(profile["files"][0]["skill_id"]),
                   lambda: middleware.before_agent(state, None, {}),
                   lambda: middleware.modify_request(request(state)),
                   lambda: skills.read_run_static_skills(s.store, profile)):
        with pytest.raises(skills.StaticSkillError):
            action()
    assert inventory(root) == before


def test_reopening_uses_run_originals_after_external_library_is_removed(root: Path) -> None:
    s = scenario(root)
    profile, service = prepare(s)
    expected = service.body(profile["files"][0]["skill_id"])
    shutil.rmtree(s.library)
    before = inventory(root)
    reopened = skills.read_run_static_skills(s.store, profile)
    assert reopened is not None and reopened.body(profile["files"][0]["skill_id"]) == expected
    assert inventory(root) == before


def test_disabled_none_creates_nothing_and_rejects_residual_resources(root: Path) -> None:
    s = scenario(root)
    before = inventory(root)
    assert skills.prepare_run_static_skills(s.workspace, None) is None
    assert skills.read_run_static_skills(s.workspace, None) is None
    assert inventory(root) == before
    prepare(s, enabled=False)
    before = inventory(root)
    for action in (lambda: skills.prepare_run_static_skills(s.workspace, None),
                   lambda: skills.read_run_static_skills(s.workspace, None),
                   lambda: skills.prepare_run_static_skills(s.store,
                       skills.StaticSkillPolicy(enabled=False, entry_ids=s.selected))):
        with pytest.raises(skills.StaticSkillError):
            action()
    assert inventory(root) == before


@pytest.mark.parametrize("case", ["unselected", "inference", "foreign_run", "profile_only", "limit", "native_skip"])
def test_publication_rejects_unapproved_or_unreadable_resources_before_writing(
        root: Path, monkeypatch: pytest.MonkeyPatch, case: str) -> None:
    s = scenario(root, inference=case == "inference")
    selected = (s.second.entry_id,) if case == "unselected" else s.selected
    policy = skills.StaticSkillPolicy(enabled=True, entry_ids=selected)
    if case == "foreign_run":
        s.store.approval = s.store.approval.model_copy(update={"run_id": "other"})
    elif case == "profile_only":
        s.store.retriever.store = KnowledgeStore(s.library)
    elif case == "limit":
        monkeypatch.setattr(skills, "MAX_SKILL_FILE_BYTES", 32)
    elif case == "native_skip":
        monkeypatch.setattr(skills, "_native_catalog", lambda backend: [])
    before = inventory(root)
    with pytest.raises(skills.StaticSkillError):
        skills.prepare_run_static_skills(s.store, policy)
    assert inventory(root) == before
    assert not (s.workspace.root / skills.NAMESPACE).exists()


@pytest.mark.parametrize("field,value", [("entry_ids", []), ("entry_ids", ["experience-" + "g" * 64]),
    ("entry_ids", ["experience-" + "b" * 64, "experience-" + "a" * 64]),
    ("entry_ids", ["experience-" + "a" * 64] * 2), ("enabled", 1), ("enabled", "true"),
    ("schema_version", True), ("schema_version", 2), ("extra", "value")])
def test_policy_requires_explicit_strict_selection(field: str, value: Any) -> None:
    raw = {"schema_version": 1, "enabled": True, "entry_ids": ["experience-" + "a" * 64]}
    raw[field] = value
    with pytest.raises(ValidationError):
        skills.StaticSkillPolicy.model_validate_json(encode(raw))


@pytest.mark.parametrize("case", ["missing", "extra", "hash", "bytes", "path", "tools", "bool_schema"])
def test_pure_profile_normalizer_rejects_noncanonical_or_inconsistent_fields(root: Path, case: str) -> None:
    profile, _ = prepare(scenario(root))
    changed = copy.deepcopy(profile)
    if case == "missing":
        del changed["files"][0]["metadata"]["license"]
    elif case == "extra":
        changed["unexpected"] = True
    elif case == "hash":
        changed["prompt_sha256"] = "a" * 64
    elif case == "bytes":
        changed["files"][0]["bytes"] = 0
    elif case == "path":
        changed["files"][0]["virtual_path"] = "/private/SKILL.md"
    elif case == "tools":
        changed["files"][0]["metadata"]["allowed_tools"] = ["execute"]
    else:
        changed["schema_version"] = True
    before = inventory(root)
    with pytest.raises(skills.StaticSkillError):
        skills.normalize_static_skill_profile(changed)
    assert inventory(root) == before


def test_manifest_backend_denies_directory_expansion_and_execution(root: Path) -> None:
    profile, service = prepare(scenario(root))
    backend = service.middleware()._backend
    expected_path = profile["files"][0]["virtual_path"]
    assert backend.ls(skills.VIRTUAL_ROOT).entries == [{
        "path": expected_path.removesuffix("/SKILL.md"), "is_dir": True}]
    assert backend.download_files([expected_path])[0].content.decode() == service.body(profile["files"][0]["skill_id"])
    assert asyncio.run(backend.adownload_files([expected_path]))[0].content is not None
    for action in (lambda: backend.ls("/"), lambda: backend.ls(expected_path),
                   lambda: backend.download_files([expected_path, "/private/SKILL.md"]),
                   lambda: backend.download_files([expected_path] * 65),
                   lambda: backend.read(expected_path), lambda: backend.write(expected_path, "new"),
                   lambda: backend.execute("anything"), lambda: backend.grep("secret"),
                   lambda: backend.glob("*"), lambda: backend.edit(expected_path, "a", "b"),
                   lambda: backend.delete(expected_path), lambda: backend.upload_files([]),
                   lambda: asyncio.run(backend.aread(expected_path))):
        with pytest.raises(skills.StaticSkillError):
            action()


def test_foreign_skill_id_does_not_create_or_read_paths(root: Path) -> None:
    _, service = prepare(scenario(root))
    before = inventory(root)
    for value in ("dfx-" + "f" * 56, "../SKILL.md", "/private/SKILL.md", 123):
        with pytest.raises(skills.StaticSkillError):
            service.body(value)
    assert inventory(root) == before


def test_renderer_rejects_native_truncation_and_retains_declared_scope() -> None:
    projection = SolverProjection(guidance=guidance("width"), derivation_authority="host_curated")
    long = projection.model_dump(mode="json")
    long["guidance"]["trigger"] = "x" * 1025
    with pytest.raises(ValueError):
        skills._render("experience-" + "a" * 64, "a" * 64,
                       SolverProjection.model_validate_json(encode(long)))
    file, raw = skills._render("experience-" + "a" * 64, "a" * 64, projection)
    assert file["metadata"]["description"] == projection.guidance.trigger
    assert encode(projection).encode() in raw


def test_body_hardlink_is_not_admitted_as_frozen_original(root: Path) -> None:
    s = scenario(root)
    profile, service = prepare(s)
    path = s.workspace.root / profile["files"][0]["path"]
    alias = root / "alias"
    os.link(path, alias)
    before = inventory(root)
    with pytest.raises(skills.StaticSkillError):
        service.catalog()
    assert inventory(root) == before


def test_rehashed_local_manifest_cannot_replace_approved_rendering(root: Path) -> None:
    s = scenario(root)
    profile, _ = prepare(s)
    forged = copy.deepcopy(profile)
    path = s.workspace.root / forged["files"][0]["path"]
    raw = path.read_bytes().replace(b"Historical", b"Xistorical", 1)
    path.write_bytes(raw)
    forged["files"][0]["sha256"] = digest(raw)
    (s.workspace.root / skills.PROFILE_PATH).write_bytes(encode(forged).encode())
    assert skills.normalize_static_skill_profile(forged) == forged
    before = inventory(root)
    with pytest.raises(skills.StaticSkillError):
        skills.read_run_static_skills(s.store, forged)
    assert inventory(root) == before


def test_profile_copies_cannot_mutate_service_identity(root: Path) -> None:
    profile, service = prepare(scenario(root))
    returned = service.profile
    returned["enabled"] = False
    returned["files"][0]["sha256"] = "a" * 64
    assert service.profile == profile
    assert service.expected_section() is not None


@pytest.mark.parametrize("limit", ["MAX_STATIC_SKILL_BYTES", "MAX_INDEX_READ_BYTES", "MAX_INDEX_READ_CALLS"])
def test_aggregate_and_source_closure_capacity_reject_before_publication(
        root: Path, monkeypatch: pytest.MonkeyPatch, limit: str) -> None:
    s = scenario(root)
    monkeypatch.setattr(skills, limit, 1)
    before = inventory(root)
    with pytest.raises(skills.StaticSkillError):
        skills.prepare_run_static_skills(s.store, skills.StaticSkillPolicy(enabled=True, entry_ids=s.selected))
    assert inventory(root) == before


def test_partial_publication_is_not_silently_repaired_or_used(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    s = scenario(root)
    policy = skills.StaticSkillPolicy(enabled=True, entry_ids=s.selected)
    publish = s.store.io._publish

    def interrupted(folder: str, filename: str, raw: bytes) -> None:
        if filename == skills.PROFILE_PATH:
            raise OSError("synthetic interruption")
        publish(folder, filename, raw)

    monkeypatch.setattr(s.store.io, "_publish", interrupted)
    with pytest.raises(skills.StaticSkillError):
        skills.prepare_run_static_skills(s.store, policy)
    namespace = s.workspace.root / skills.NAMESPACE
    assert list(namespace.glob("*/SKILL.md"))
    assert not (s.workspace.root / skills.PROFILE_PATH).exists()
    before = inventory(root)
    with pytest.raises(skills.StaticSkillError):
        skills.prepare_run_static_skills(s.store, policy)
    with pytest.raises(skills.StaticSkillError):
        skills.read_run_static_skills(s.store, None)
    assert inventory(root) == before


def test_policy_entry_count_is_bounded_before_any_io() -> None:
    raw = {"schema_version": 1, "enabled": True,
           "entry_ids": ["experience-" + f"{index:064x}" for index in range(65)]}
    with pytest.raises(ValidationError):
        skills.StaticSkillPolicy.model_validate_json(encode(raw))
