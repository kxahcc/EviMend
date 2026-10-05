"""Native Skill cache drift is rejected before opening writable run ledgers."""

from __future__ import annotations

import os
import sqlite3
import sys
from contextlib import closing
from pathlib import Path
from types import ModuleType

import ormsgpack
import pytest
from langgraph.checkpoint.sqlite import SqliteSaver
from test_agent_loop import response
from test_metrics import run_synthetic
from test_readonly_evidence import tree_bytes
from test_static_skill_handoff import sdk_run

from dfx_agent.agent import ResumeAdmissionRejected
from dfx_agent.applicability_handoff import ApplicabilityPolicy
from dfx_agent.static_skills import StaticSkillPolicy


@pytest.fixture
def root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("sc")


def _resume(s, work: Path, skill_id: str):
    return run_synthetic(s.task, work, "run2", [response(1, "read_skill", {"skill_id": skill_id}), response(2)],
        resume=True, knowledge_approval=s.store.approval.model_copy(update={"run_id": "run2"}),
        applicability_policy=ApplicabilityPolicy(enabled=False),
        static_skill_policy=StaticSkillPolicy(enabled=True, entry_ids=s.selected), max_calls=6, max_tool_calls=12)


@pytest.mark.parametrize("place", ["checkpoint", "pending"])
@pytest.mark.parametrize("channel", ["skills_metadata", "skills_load_errors"])
def test_changed_native_cache_rejected_without_run_writes(root: Path, place: str, channel: str) -> None:
    s, work, _report, skill_id = sdk_run(root)
    with closing(sqlite3.connect(work / "checkpoints.sqlite")) as connection, connection:
        saver = SqliteSaver(connection)
        identifier, kind, raw = connection.execute("SELECT checkpoint_id,type,checkpoint FROM checkpoints "
            "WHERE thread_id='run2' AND checkpoint_ns='' ORDER BY checkpoint_id DESC LIMIT 1").fetchone()
        changed = [] if channel == "skills_metadata" else ["synthetic cache drift"]
        if place == "checkpoint":
            checkpoint = saver.serde.loads_typed((kind, raw))
            assert channel in checkpoint["channel_values"]
            checkpoint["channel_values"][channel] = changed
            kind, raw = saver.serde.dumps_typed(checkpoint)
            connection.execute("UPDATE checkpoints SET type=?,checkpoint=? WHERE thread_id='run2' "
                "AND checkpoint_ns='' AND checkpoint_id=?", (kind, raw, identifier))
        else:
            kind, raw = saver.serde.dumps_typed(changed)
            connection.execute("INSERT INTO writes (thread_id,checkpoint_ns,checkpoint_id,task_id,idx,channel,type,value) "
                "VALUES ('run2','',?,'synthetic-cache',0,?,?,?)", (identifier, channel, kind, raw))
    before = tree_bytes(root)
    with pytest.raises(ResumeAdmissionRejected):
        _resume(s, work, skill_id)
    assert tree_bytes(root) == before


def test_valid_native_cache_resumes_without_another_model_or_skill_read(root: Path) -> None:
    s, work, report, skill_id = sdk_run(root)
    resumed = _resume(s, work, skill_id)
    assert resumed["model_calls_this_turn"] == resumed["known_tokens_this_turn"] == 0
    assert resumed["static_skill_facts"] == report["static_skill_facts"]
    assert resumed["tool_calls"] == 1


def test_unmerged_checkpoint_journal_is_rejected_without_cleanup(root: Path) -> None:
    s, work, _report, skill_id = sdk_run(root)
    (work / "checkpoints.sqlite-wal").write_bytes(b"synthetic unmerged journal")
    before = tree_bytes(root)
    with pytest.raises(ResumeAdmissionRejected):
        _resume(s, work, skill_id)
    assert tree_bytes(root) == before


def test_unregistered_native_cache_constructor_is_never_called(root: Path, monkeypatch) -> None:
    s, work, _report, skill_id = sdk_run(root)
    called = []
    module = ModuleType("dfx_synthetic_cache_constructor")

    def construct(argument):
        called.append(argument)
        return argument

    monkeypatch.setattr(module, "construct", construct, raising=False)
    monkeypatch.setitem(sys.modules, module.__name__, module)
    # Constructor single-arg extension in the locked SDK. The test replaces
    # only a stopped synthetic pending cache value, never executes the extension.
    wrapped = ormsgpack.Ext(0, ormsgpack.packb((module.__name__, "construct", [])))
    raw = ormsgpack.packb(wrapped)
    with closing(sqlite3.connect(work / "checkpoints.sqlite")) as connection, connection:
        identifier = connection.execute("SELECT checkpoint_id FROM checkpoints WHERE thread_id='run2' "
            "AND checkpoint_ns='' ORDER BY checkpoint_id DESC LIMIT 1").fetchone()[0]
        connection.execute("INSERT INTO writes (thread_id,checkpoint_ns,checkpoint_id,task_id,idx,channel,type,value) "
            "VALUES ('run2','',?,'synthetic-constructor',0,'skills_metadata','msgpack',?)", (identifier, raw))
    before = tree_bytes(root)
    with pytest.raises(ResumeAdmissionRejected):
        _resume(s, work, skill_id)
    assert called == [] and tree_bytes(root) == before


def test_missing_final_catalog_stops_before_dispatch_and_never_counts_skill_use(root: Path, monkeypatch) -> None:
    from dfx_agent import models

    original = models.FixedResponseModel._view

    def shortened(self, messages):
        view = original(self, messages)
        # Simulate a late integration failure after native middleware loading.
        view[0] = view[0].model_copy(update={"content": view[0].content_blocks[:-1]})
        self.active_view = [message.model_copy(deep=True) for message in view]
        return view

    monkeypatch.setattr(models.FixedResponseModel, "_view", shortened)
    _s, _work, report, skill_id = sdk_run(root)
    assert report["stopped_reason"] == "static_skill_input_invalid"
    assert report["tool_calls"] == 0 and report["model_calls"] == 1
    assert report["completion"]["cost"]["completed_calls"] == 0
    assert report["static_skill_facts"]["unread_skill_ids"] == [skill_id]
    assert report["static_skill_facts"]["solver_inputs"][0]["input_saved"] is False


def test_shared_checkpoint_file_is_rejected_before_writable_resume(root: Path) -> None:
    s, work, _report, skill_id = sdk_run(root)
    os.link(work / "checkpoints.sqlite", root / "shared-checkpoint.sqlite")
    before = tree_bytes(root)
    with pytest.raises(ResumeAdmissionRejected):
        _resume(s, work, skill_id)
    assert tree_bytes(root) == before


def test_checkpoint_entry_cannot_be_hidden_by_an_exists_false_result(root: Path, monkeypatch) -> None:
    from dfx_agent import agent

    s, work, _report, skill_id = sdk_run(root)
    original_exists, original_owned = Path.exists, agent._owned_file
    checked = []

    def exists(path):
        return False if path == work / "checkpoints.sqlite" else original_exists(path)

    def owned(workspace, path):
        if path == work / "checkpoints.sqlite":
            checked.append(path)
            raise ValueError("synthetic unsafe checkpoint entry")
        return original_owned(workspace, path)

    monkeypatch.setattr(Path, "exists", exists)
    monkeypatch.setattr(agent, "_owned_file", owned)
    before = tree_bytes(root)
    with pytest.raises(ResumeAdmissionRejected):
        _resume(s, work, skill_id)
    assert checked == [work / "checkpoints.sqlite"] and tree_bytes(root) == before
