"""Explicit offline knowledge declarations and read-only CLI admission.

These fixtures contain host-curated synthetic guidance. They do not measure
model quality, learned Skills, application repair, or real provider behavior.
"""

from __future__ import annotations

import copy
import json
import os
import shutil
import sys
from pathlib import Path

import pytest
from test_agent_loop import task_snapshot
from test_knowledge_retrieval import admit, inventory
from test_offline_script import fixed_document, mock_document, write

from dfx_agent import agent, cli
from dfx_agent.applicability_contracts import TaskKnowledgeBinding
from dfx_agent.applicability_handoff import ApplicabilityPolicy
from dfx_agent.knowledge import KnowledgeStore
from dfx_agent.knowledge_contracts import PermissionScope
from dfx_agent.knowledge_retrieval import KnowledgeRetriever
from dfx_agent.offline_script import load_offline_script
from dfx_agent.static_skills import StaticSkillPolicy
from dfx_agent.task import verify_task


@pytest.fixture
def root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("kc")


def declaration(*, mock: bool = False) -> dict:
    value = mock_document() if mock else fixed_document()
    value["schema_version"] = 6 if mock else 5
    value["working_context"] = {"enabled": False}
    approval = TaskKnowledgeBinding(task_id="task1", run_id="run1", source_snapshot_sha256="1" * 64,
        crash_sha256="2" * 64, snapshot_sha256="3" * 64, scope=PermissionScope(split="development"))
    value["knowledge"] = {"library_path": "approved/library", "approval": approval.model_dump(mode="json")}
    return value


def library(root: Path):
    task = task_snapshot(root)
    source = root / "approved" / "library"
    source.mkdir(parents=True)
    store = KnowledgeStore(source)
    first = admit(store, root / "projection", "width")
    scope = PermissionScope(split="development")
    snapshot = store.freeze_snapshot((first.entry_id,), scope)
    KnowledgeRetriever(store).build_index(snapshot.snapshot_sha256, scope)
    info = verify_task(task)
    approval = TaskKnowledgeBinding(task_id=info["task_id"], run_id="run1",
        source_snapshot_sha256=info["source_snapshot_sha256"], crash_sha256=info["crash_sha256"],
        snapshot_sha256=snapshot.snapshot_sha256, scope=scope)
    return task, source, approval, first.entry_id


def saved_configuration(script) -> dict:
    approval = script.knowledge_approval()
    applicability = script.applicability_policy()
    static = script.static_skill_policy()
    result = {"knowledge_profile": {"approval": approval.model_dump(mode="json")}}
    if applicability is not None:
        result["applicability_profile"] = applicability.model_dump(mode="json")
    if static is not None:
        result["static_skill_profile"] = static.model_dump(mode="json")
    return result


@pytest.mark.parametrize("mock", [False, True])
@pytest.mark.parametrize("mode", ["ordinary", "applicability_on", "applicability_off", "static_on", "static_off"])
def test_new_generations_preserve_explicit_knowledge_and_fresh_policy_objects(root: Path, mock: bool, mode: str) -> None:
    value = declaration(mock=mock)
    knowledge = value["knowledge"]
    if mode != "ordinary":
        knowledge["applicability_policy"] = {"schema_version": 1, "enabled": mode == "applicability_on"}
    if mode.startswith("static"):
        knowledge["static_skill_policy"] = {"schema_version": 1, "enabled": mode == "static_on",
                                            "entry_ids": ["experience-" + "4" * 64]}
    script = load_offline_script(write(root, value))
    assert script.schema_version == (6 if mock else 5)
    assert script.mode == ("mock_chat_completions" if mock else "fixed_response")
    approval = script.knowledge_approval()
    assert approval is not None and approval.scope.excluded_lineage_groups == ()
    assert approval is not script.knowledge_approval()
    assert (script.applicability_policy() is None) is (mode == "ordinary")
    assert (script.static_skill_policy() is None) is (not mode.startswith("static"))
    if mode.startswith("static"):
        assert script.static_skill_policy() == StaticSkillPolicy(enabled=mode == "static_on",
                                                               entry_ids=("experience-" + "4" * 64,))
    value["knowledge"]["approval"]["scope"]["split"] = "confirmation"
    assert script.knowledge_approval().scope.split == "development"
    script.check_resume_knowledge(saved_configuration(script))


@pytest.mark.parametrize("schema", [1, 2, 3, 4])
def test_old_generations_cannot_acquire_implicit_or_explicit_knowledge(root: Path, schema: int) -> None:
    value = fixed_document() if schema % 2 else mock_document()
    value["schema_version"] = schema
    if schema > 2:
        value["working_context"] = {"enabled": False}
    script = load_offline_script(write(root, value))
    assert script.knowledge_approval() is script.applicability_policy() is script.static_skill_policy() is None
    assert script.validate_knowledge_paths(None, root / "task", root / "work") is None
    assert script.knowledge_input(None, root / "task", root / "work", "run1") is None
    value["knowledge"] = declaration()["knowledge"]
    with pytest.raises(ValueError, match="fields"):
        load_offline_script(write(root, value))


@pytest.mark.parametrize("change", ["missing_knowledge", "missing_context", "null", "array", "extra", "missing_path",
                                   "missing_approval", "approval_extra", "approval_missing", "scope_missing",
                                   "scope_extra", "scope_learn", "bad_hash", "bad_run", "bad_applicability",
                                   "approval_boolean_schema", "approval_float_schema", "scope_boolean_schema", "scope_float_schema",
                                   "applicability_missing_enabled", "applicability_boolean_schema", "static_without_policy",
                                   "static_empty", "static_unsorted", "static_missing_enabled", "static_extra"])
def test_new_fields_require_complete_independent_host_authority(root: Path, change: str) -> None:
    value = declaration()
    knowledge = value["knowledge"]
    if change == "missing_knowledge":
        del value["knowledge"]
    elif change == "missing_context":
        del value["working_context"]
    elif change in ("null", "array"):
        value["knowledge"] = None if change == "null" else []
    elif change == "extra":
        knowledge["discover_skills"] = True
    elif change == "missing_path":
        del knowledge["library_path"]
    elif change == "missing_approval":
        del knowledge["approval"]
    elif change == "approval_extra":
        knowledge["approval"]["approve_from_session"] = True
    elif change == "approval_missing":
        del knowledge["approval"]["run_id"]
    elif change == "scope_missing":
        del knowledge["approval"]["scope"]["use"]
    elif change == "scope_extra":
        knowledge["approval"]["scope"]["directory"] = "data_B"
    elif change == "scope_learn":
        knowledge["approval"]["scope"]["use"] = "learn"
    elif change == "bad_hash":
        knowledge["approval"]["snapshot_sha256"] = "bad"
    elif change == "bad_run":
        knowledge["approval"]["run_id"] = "../run"
    elif change in ("approval_boolean_schema", "approval_float_schema"):
        knowledge["approval"]["schema_version"] = True if change == "approval_boolean_schema" else 1.0
    elif change in ("scope_boolean_schema", "scope_float_schema"):
        knowledge["approval"]["scope"]["schema_version"] = True if change == "scope_boolean_schema" else 1.0
    elif change == "bad_applicability":
        knowledge["applicability_policy"] = {"schema_version": 1, "enabled": "true"}
    elif change == "applicability_missing_enabled":
        knowledge["applicability_policy"] = {"schema_version": 1}
    elif change == "applicability_boolean_schema":
        knowledge["applicability_policy"] = {"schema_version": True, "enabled": False}
    else:
        knowledge["static_skill_policy"] = {"schema_version": 1, "enabled": True,
                                            "entry_ids": ["experience-" + "4" * 64]}
        if change != "static_without_policy":
            knowledge["applicability_policy"] = {"schema_version": 1, "enabled": False}
        if change == "static_empty":
            knowledge["static_skill_policy"]["entry_ids"] = []
        elif change == "static_unsorted":
            knowledge["static_skill_policy"]["entry_ids"] = ["experience-" + "5" * 64, "experience-" + "4" * 64]
        elif change == "static_missing_enabled":
            del knowledge["static_skill_policy"]["enabled"]
        elif change == "static_extra":
            knowledge["static_skill_policy"]["shell"] = True
    with pytest.raises(ValueError):
        load_offline_script(write(root, value))


@pytest.mark.parametrize("path", ["", ".", "..", "../lib", "/library", "C:/library", "approved\\library",
                                 "approved//library", "approved/./library", "data_B/lib", "approved/Data_B/lib",
                                 "archive/old/lib", "approved/.git/lib", "approved/lib.", "approved/lib ",
                                 "approved/NUL.json", "approved/COM1", "approved/lib:stream", "approved/?",
                                 "approved/\u0000library"])
def test_library_path_has_no_implicit_discovery_or_forbidden_material_access(root: Path, path: str) -> None:
    value = declaration()
    value["knowledge"]["library_path"] = path
    with pytest.raises(ValueError, match="library path"):
        load_offline_script(write(root, value))


def test_initial_admission_authenticates_full_originals_and_index_without_writes(root: Path) -> None:
    task, source, approval, _entry = library(root)
    value = declaration()
    value["knowledge"]["approval"] = approval.model_dump(mode="json")
    script = load_offline_script(write(root, value))
    before = inventory(root)
    host = script.knowledge_input(root, task, root / "new-work", "run1")
    assert host is not None and host.approval == approval
    assert host.retriever.store.root == source
    assert inventory(root) == before
    assert not (root / "new-work").exists()
    assert not (source / "retrievals").exists()


@pytest.mark.parametrize("defect", ["unindexed", "index_changed", "snapshot_missing", "wrong_task", "wrong_run",
                                   "wrong_source", "wrong_log", "wrong_scope"])
def test_startup_rejects_original_or_authority_drift_before_workspace(root: Path, defect: str) -> None:
    task, source, approval, _entry = library(root)
    value = declaration()
    value["knowledge"]["approval"] = approval.model_dump(mode="json")
    if defect == "unindexed":
        shutil.rmtree(source / "indexes")
    elif defect == "index_changed":
        (source / "indexes" / (approval.snapshot_sha256 + ".sqlite")).write_bytes(b"invalid index")
    elif defect == "snapshot_missing":
        (source / "snapshots" / (approval.snapshot_sha256 + ".json")).unlink()
    else:
        fields = {"wrong_task": ("task_id", "another"), "wrong_run": ("run_id", "other"),
                  "wrong_source": ("source_snapshot_sha256", "4" * 64), "wrong_log": ("crash_sha256", "5" * 64)}
        if defect == "wrong_scope":
            value["knowledge"]["approval"]["scope"]["excluded_lineage_groups"] = ["hidden-family"]
        else:
            key, replacement = fields[defect]
            value["knowledge"]["approval"][key] = replacement
    script = load_offline_script(write(root, value))
    before = inventory(root)
    with pytest.raises(ValueError, match="originals, index or approval"):
        script.knowledge_input(root, task, root / "new-work", "run1")
    assert inventory(root) == before


@pytest.mark.parametrize("root_mode", ["missing", "relative", "noncanonical"])
def test_project_authority_must_be_explicit_and_canonical(root: Path, root_mode: str) -> None:
    script = load_offline_script(write(root, declaration()))
    project_root = None if root_mode == "missing" else Path("relative") if root_mode == "relative" else root / "x" / ".."
    with pytest.raises(ValueError, match="project root"):
        script.validate_knowledge_paths(project_root, root / "task", root / "work")


@pytest.mark.parametrize("relative", ["input", "input/source", "work", "work/nested", "approved"])
def test_library_cannot_overlap_current_task_or_work(root: Path, relative: str) -> None:
    value = declaration()
    value["knowledge"]["library_path"] = relative
    script = load_offline_script(write(root, value))
    work = root / "approved" / "work" if relative == "approved" else root / "work"
    with pytest.raises(ValueError, match="separate"):
        script.validate_knowledge_paths(root, root / "input", work)


def test_resume_config_uses_external_approval_and_never_opens_missing_library(root: Path) -> None:
    value = declaration()
    value["knowledge"]["applicability_policy"] = {"schema_version": 1, "enabled": False}
    value["knowledge"]["static_skill_policy"] = {"schema_version": 1, "enabled": True,
                                                "entry_ids": ["experience-" + "4" * 64]}
    script = load_offline_script(write(root, value))
    before = inventory(root)
    assert script.validate_knowledge_paths(root, root / "task", root / "work") == root / "approved" / "library"
    script.check_resume_knowledge(saved_configuration(script))
    assert inventory(root) == before and not (root / "approved").exists()


def test_resume_library_locator_can_move_while_original_authority_is_unchanged(root: Path) -> None:
    original = declaration()
    script = load_offline_script(write(root, original))
    session = saved_configuration(script)
    moved = copy.deepcopy(original)
    moved["knowledge"]["library_path"] = "other/nonexistent-library"
    restored = load_offline_script(write(root, moved, name="moved.json"))
    before = inventory(root)
    restored.validate_knowledge_paths(root, root / "task", root / "work")
    restored.check_resume_knowledge(session)
    assert restored.knowledge_approval() == script.knowledge_approval()
    assert inventory(root) == before


def test_startup_rejects_hardlinked_original_material_without_creating_workspace(root: Path) -> None:
    task, source, approval, _entry = library(root)
    value = declaration()
    value["knowledge"]["approval"] = approval.model_dump(mode="json")
    script = load_offline_script(write(root, value))
    original = next((source / "materials").glob("*.blob"))
    try:
        os.link(original, root / "shared.blob")
    except OSError as exc:
        pytest.skip(f"Hard links unavailable: {exc}")
    before = inventory(root)
    with pytest.raises(ValueError, match="originals, index or approval"):
        script.knowledge_input(root, task, root / "new-work", "run1")
    assert inventory(root) == before


@pytest.mark.parametrize("field", ["approval", "missing_knowledge", "applicability", "missing_applicability",
                                  "static_enabled", "static_entries", "missing_static"])
def test_resume_saved_configuration_cannot_override_external_host_choices(root: Path, field: str) -> None:
    value = declaration()
    value["knowledge"]["applicability_policy"] = {"schema_version": 1, "enabled": False}
    value["knowledge"]["static_skill_policy"] = {"schema_version": 1, "enabled": True,
                                                "entry_ids": ["experience-" + "4" * 64]}
    script = load_offline_script(write(root, value))
    session = saved_configuration(script)
    if field == "approval":
        session["knowledge_profile"]["approval"]["run_id"] = "other"
    elif field == "missing_knowledge":
        del session["knowledge_profile"]
    elif field == "applicability":
        session["applicability_profile"]["enabled"] = True
    elif field == "missing_applicability":
        del session["applicability_profile"]
    elif field == "static_enabled":
        session["static_skill_profile"]["enabled"] = False
    elif field == "static_entries":
        session["static_skill_profile"]["entry_ids"] = ["experience-" + "5" * 64]
    else:
        del session["static_skill_profile"]
    with pytest.raises(ValueError, match="differs"):
        script.check_resume_knowledge(session)


@pytest.mark.parametrize("mock", [False, True])
@pytest.mark.parametrize("command", ["repair", "resume"])
def test_cli_forwards_explicit_authority_and_never_reads_original_library_on_resume(
        root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture,
        mock: bool, command: str) -> None:
    task, source, approval, entry = library(root)
    value = declaration(mock=mock)
    value["knowledge"]["approval"] = approval.model_dump(mode="json")
    value["knowledge"]["applicability_policy"] = {"schema_version": 1, "enabled": False}
    value["knowledge"]["static_skill_policy"] = {"schema_version": 1, "enabled": True, "entry_ids": [entry]}
    path = write(root, value)
    script = load_offline_script(path)
    seen = {}
    admissions = []

    def preflight(*args, **options):
        admissions.append((args, options))
        return saved_configuration(script)

    def run(*args, **options):
        seen.update(options)
        return {"synthetic_handoff": True}

    monkeypatch.setattr(agent, "preflight_resume", preflight)
    monkeypatch.setattr(agent, "run_provider_agent" if mock else "run_fixed_agent", run)
    if command == "resume":
        shutil.rmtree(source.parent)
        if mock:
            from dfx_agent.model_calls import ModelCallStore
            from dfx_agent.workspace import CandidateWorkspace

            workspace = CandidateWorkspace(task, root / "work", "run1")
            ModelCallStore(workspace.root / "model-calls.sqlite")
    before = inventory(root)
    monkeypatch.setenv("DFX_MANAGED_AGENT", "offline-v1")
    monkeypatch.setattr(sys, "argv", ["dfx", command, "--task", str(task), "--work", str(root / "work"),
        "--run-id", "run1", "--script", str(path), "--project-root", str(root)])
    cli.main()
    assert json.loads(capsys.readouterr().out) == {"synthetic_handoff": True}
    assert seen["applicability_policy"] == ApplicabilityPolicy(enabled=False)
    assert seen["static_skill_policy"] == StaticSkillPolicy(enabled=True, entry_ids=(entry,))
    assert inventory(root) == before
    if command == "resume":
        assert seen["knowledge_approval"] == approval and "knowledge" not in seen
        assert admissions == [((task, root / "work", "run1"), {"knowledge_approval": approval})]
    else:
        assert seen["knowledge"].approval == approval and "knowledge_approval" not in seen
        assert admissions == []


@pytest.mark.parametrize("defect", ["missing_root", "policy_drift", "approval_drift", "invalid_script"])
def test_cli_resume_config_rejection_is_read_only_and_uses_refusal_exit_code(
        root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, defect: str) -> None:
    value = declaration()
    value["knowledge"]["applicability_policy"] = {"schema_version": 1, "enabled": True}
    path = write(root, value)
    session = saved_configuration(load_offline_script(path))
    if defect == "policy_drift":
        session["applicability_profile"]["enabled"] = False
    elif defect == "approval_drift":
        session["knowledge_profile"]["approval"]["run_id"] = "other"
    elif defect == "invalid_script":
        path.write_bytes(b"invalid json")
    invoked = []
    monkeypatch.setattr(agent, "preflight_resume", lambda *args, **kwargs: copy.deepcopy(session))
    monkeypatch.setattr(agent, "run_fixed_agent", lambda *args, **kwargs: invoked.append(True))
    monkeypatch.setenv("DFX_MANAGED_AGENT", "offline-v1")
    arguments = ["dfx", "resume", "--task", str(root / "task"), "--work", str(root / "work"),
                 "--run-id", "run1", "--script", str(path)]
    if defect != "missing_root":
        arguments.extend(("--project-root", str(root)))
    monkeypatch.setattr(sys, "argv", arguments)
    before = inventory(root)
    with pytest.raises(SystemExit) as caught:
        cli.main()
    assert caught.value.code == 3 and not invoked
    assert inventory(root) == before
    assert "Resume rejected before execution" in capsys.readouterr().err


@pytest.mark.parametrize("defect", ["missing", "corrupt", "usage_error", "identity_drift"])
def test_mock_resume_original_ledger_errors_refuse_before_execution_or_writes(
        root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, defect: str) -> None:
    import sqlite3

    from dfx_agent.model_calls import ModelCallIntent, ModelCallStore
    from dfx_agent.task import digest
    from dfx_agent.workspace import CandidateWorkspace

    task = task_snapshot(root)
    value = declaration(mock=True)
    path = write(root, value)
    script = load_offline_script(path)
    workspace = CandidateWorkspace(task, root / "work", "run1")
    ledger = workspace.root / "model-calls.sqlite"
    if defect == "corrupt":
        ledger.write_bytes(b"not a SQLite database")
    elif defect == "usage_error":
        ModelCallStore(ledger)

        def corrupted_query(*args, **kwargs):
            raise sqlite3.DatabaseError("synthetic original ledger query failure")

        monkeypatch.setattr(ModelCallStore, "usage_summary", corrupted_query)
    elif defect == "identity_drift":
        store = ModelCallStore(ledger)
        intent = ModelCallIntent(call_id="model-1", task_id=workspace.task_id, run_id=workspace.run_id,
            kind="solver", provider_id="configured-provider", model_id="configured/model",
            prompt_sha256=digest(b"prompt"), tool_schema_sha256=digest(b"tools"),
            parameters_sha256=digest(b"parameters"), tool_names=())
        store.reserve(intent, max_calls=3)
        with sqlite3.connect(ledger) as connection:
            connection.execute("UPDATE model_calls SET request_sha256=?", ("6" * 64,))
    invoked = []
    monkeypatch.setattr(agent, "preflight_resume", lambda *args, **kwargs: saved_configuration(script))
    monkeypatch.setattr(agent, "run_provider_agent", lambda *args, **kwargs: invoked.append(True))
    monkeypatch.setenv("DFX_MANAGED_AGENT", "offline-v1")
    monkeypatch.setattr(sys, "argv", ["dfx", "resume", "--task", str(task), "--work", str(workspace.root),
        "--run-id", "run1", "--script", str(path), "--project-root", str(root)])
    before = inventory(root)
    with pytest.raises(SystemExit) as caught:
        cli.main()
    assert caught.value.code == 3 and not invoked
    assert inventory(root) == before
    assert "original model ledger rejected before execution" in capsys.readouterr().err
