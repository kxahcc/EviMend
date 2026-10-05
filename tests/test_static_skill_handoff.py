"""Static SDK reads keep full originals, costs and actual visibility distinct."""

from __future__ import annotations

import copy
import json
import os
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, SystemMessage
from test_agent_loop import response
from test_knowledge_reporting import open_run
from test_metrics import read_json, run_synthetic
from test_readonly_evidence import tree_bytes
from test_static_skills import prepare, request, scenario

from dfx_agent import static_skill_handoff as handoff
from dfx_agent.applicability_handoff import ApplicabilityPolicy
from dfx_agent.knowledge import KnowledgeStore
from dfx_agent.knowledge_contracts import MAX_CLOSURE_BYTES, encode
from dfx_agent.knowledge_retrieval import KnowledgeRetriever
from dfx_agent.knowledge_run import HostKnowledgeInput
from dfx_agent.model_calls import canonical, sha256
from dfx_agent.reporting import read_completion
from dfx_agent.static_skills import (
    NAMESPACE,
    PROFILE_PATH,
    STATIC_SKILL_MARKER,
    StaticSkillError,
    StaticSkillPolicy,
    prepare_run_static_skills,
    static_skill_section,
)


@pytest.fixture
def root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("sh")


def sdk_run(root: Path, *, enabled: bool = True, scripted=None, max_calls: int = 6, max_tool_calls: int = 12):
    s = scenario(root)
    profile, _service = prepare(s, enabled=enabled)
    skill_id = profile["files"][0]["skill_id"]
    approval = s.store.approval.model_copy(update={"run_id": "run2"})
    work = root / "run2"
    report = run_synthetic(s.task, work, "run2", scripted or [
        response(1, "read_skill", {"skill_id": skill_id}), response(2),
    ], knowledge=HostKnowledgeInput(KnowledgeRetriever(KnowledgeStore(s.library)), approval),
        applicability_policy=ApplicabilityPolicy(enabled=False),
        static_skill_policy=StaticSkillPolicy(enabled=enabled, entry_ids=s.selected),
        max_calls=max_calls, max_tool_calls=max_tool_calls)
    return s, work, report, skill_id


def test_exact_full_result_is_identical_body_in_both_catalog_modes(root: Path) -> None:
    tools = []
    results = []
    for enabled in (True, False):
        s = scenario(root / str(enabled))
        profile, service = prepare(s, enabled=enabled)
        name = profile["files"][0]["skill_id"]
        result = handoff.static_skill_result(service, name)
        assert result["body"].encode() == (s.workspace.root / result["path"]).read_bytes()
        assert "static_skills_enabled" not in result
        assert result["application_verification"] == "not_run"
        assert result["epistemic_status"] == "historical_guidance_not_current_evidence"
        tool = handoff.static_skill_tools(service)[0]
        assert json.loads(tool.invoke({"skill_id": name})) == result
        tools.append((tool.description, tool.args_schema.model_json_schema()))
        results.append(result)
        assert tool.invoke({"skill_id": "unselected"}) == "rejected: static_skill_source_invalid"
    assert tools[0] == tools[1]
    assert results[0] == results[1]


@pytest.mark.parametrize("enabled", [True, False])
def test_live_actual_native_catalog_passes_exact_validation(root: Path, enabled: bool) -> None:
    s = scenario(root)
    profile, service = prepare(s, enabled=enabled)
    middleware = service.middleware()
    state = middleware.before_agent({}, None, {})
    sent = middleware.modify_request(request(state, SystemMessage(content="base prompt")))
    assert sent.system_message is not None
    before = tree_bytes(root)
    handoff.validate_static_skill_messages(service, [sent.system_message, *sent.messages])
    assert tree_bytes(root) == before
    assert service.expected_section() == static_skill_section(profile)


@pytest.mark.parametrize("change", ["missing", "shortened", "duplicate", "moved", "another_prefix"])
def test_modified_on_catalog_is_not_authenticated(root: Path, change: str) -> None:
    s = scenario(root)
    _profile, service = prepare(s)
    middleware = service.middleware()
    sent = middleware.modify_request(request(middleware.before_agent({}, None, {}), SystemMessage(content="base")))
    assert sent.system_message is not None
    blocks = copy.deepcopy(sent.system_message.content_blocks)
    if change == "missing":
        blocks.pop()
    elif change == "shortened":
        blocks[-1]["text"] = blocks[-1]["text"].split("Check their premises", 1)[0]
    elif change == "duplicate":
        blocks.append(copy.deepcopy(blocks[-1]))
    elif change == "moved":
        blocks.append({"type": "text", "text": "later addition"})
    else:
        blocks[-1]["text"] = blocks[-1]["text"].removeprefix("\n\n")
    with pytest.raises(StaticSkillError):
        handoff.validate_static_skill_messages(service, [SystemMessage(content_blocks=blocks), *sent.messages])


def test_off_mode_rejects_injected_catalog_but_allows_full_body_read(root: Path) -> None:
    s = scenario(root)
    profile, service = prepare(s, enabled=False)
    section = static_skill_section({**profile, "enabled": True})
    with pytest.raises(StaticSkillError):
        handoff.validate_static_skill_messages(service, [SystemMessage(content=section or STATIC_SKILL_MARKER)])
    assert handoff.static_skill_result(service, profile["files"][0]["skill_id"])["body"]


@pytest.mark.parametrize("enabled", [True, False])
def test_ascii_report_capacity_gate_precedes_all_static_publication(root: Path, monkeypatch, enabled: bool) -> None:
    s = scenario(root)
    before = tree_bytes(root)
    monkeypatch.setattr(handoff, "MAX_STATIC_SKILL_REPORT_BYTES", 1)
    with pytest.raises(StaticSkillError):
        prepare_run_static_skills(s.store, StaticSkillPolicy(enabled=enabled, entry_ids=s.selected),
                                  max_calls=6, max_tool_calls=12)
    assert tree_bytes(root) == before
    assert not (s.workspace.root / NAMESPACE).exists()
    assert not (s.workspace.root / PROFILE_PATH).exists()


def test_capacity_requires_a_complete_valid_budget_pair(root: Path) -> None:
    s = scenario(root)
    before = tree_bytes(root)
    with pytest.raises(StaticSkillError):
        prepare_run_static_skills(s.store, StaticSkillPolicy(enabled=True, entry_ids=s.selected), max_calls=3)
    assert tree_bytes(root) == before
    profile, _service = prepare(s)
    for value in (True, 0, -1, 1.5):
        with pytest.raises(StaticSkillError):
            handoff.preflight_static_skill_report(profile, static_skill_section(profile), value, 3)
    with pytest.raises(StaticSkillError):
        handoff.preflight_static_skill_report(profile, "wrong", 3, 3)


def test_final_gate_measures_ascii_both_report_locations(root: Path, monkeypatch) -> None:
    s = scenario(root)
    profile, _service = prepare(s)
    facts = {"profile": profile, "unicode": "中文" * 20}
    fragment = {"static_skill_profile": profile, "static_skill_facts": facts,
        "completion": {"static_skill_profile": profile, "static_skill_facts": facts}}
    size = len(json.dumps(fragment, ensure_ascii=True, indent=2).encode("utf-8"))
    monkeypatch.setattr(handoff, "MAX_STATIC_SKILL_REPORT_BYTES", size)
    handoff.validate_static_skill_report(profile, facts)
    monkeypatch.setattr(handoff, "MAX_STATIC_SKILL_REPORT_BYTES", size - 1)
    with pytest.raises(ValueError, match="fragments exceed capacity"):
        handoff.validate_static_skill_report(profile, facts)


@pytest.mark.parametrize("enabled", [True, False])
def test_sdk_read_complete_body_shared_tools_and_accounted_later_visibility(root: Path, enabled: bool) -> None:
    s, work, report, skill_id = sdk_run(root, enabled=enabled)
    assert (read_json(work / "session.json")["schema_version"], report["schema_version"],
            report["completion"]["schema_version"]) == (13, 10, 7)
    assert len(report["offered_tools"]) == 20
    facts = report["static_skill_facts"]
    assert facts == report["completion"]["static_skill_facts"]
    assert facts["unread_skill_ids"] == []
    assert facts["resources"][0]["skill_id"] == skill_id
    assert len(facts["resources"][0]["actions"]) == 1
    assert facts["resources"][0]["actions"][0]["visible_solver_count"] == 1
    assert '"body":' not in encode(facts)
    assert all(item["catalog_entries"] == int(enabled) for item in facts["solver_inputs"])
    assert report["completion"]["cost"]["tool_calls"] == 1
    assert report["completion"]["cost"]["total_tokens"] == 30
    assert report["completion"]["verification"]["accepted"] is False
    opened = open_run(s.task, work)
    before = tree_bytes(root)
    assert read_completion(*opened, task_directory=s.task) == report["completion"]
    handoff.audit_stored_static_skills(opened[0], s.task, opened[1], report["knowledge_profile"],
                                      report["static_skill_profile"])
    assert tree_bytes(root) == before


def test_saved_resource_is_not_a_read_or_model_use(root: Path) -> None:
    _s, _work, report, skill_id = sdk_run(root, scripted=[response(1)])
    facts = report["static_skill_facts"]
    assert facts["actions"] == facts["resources"][0]["actions"] == []
    assert facts["unread_skill_ids"] == [skill_id]


def test_final_successful_read_without_later_solver_is_not_counted_visible(root: Path) -> None:
    _s, _work, report, _skill_id = sdk_run(root, max_calls=1)
    facts = report["static_skill_facts"]
    assert facts["unread_skill_ids"] == []
    assert facts["resources"][0]["actions"][0]["visible_solver_count"] == 0


def test_rejected_unselected_read_does_not_become_a_success(root: Path) -> None:
    _s, _work, report, skill_id = sdk_run(root, scripted=[
        response(1, "read_skill", {"skill_id": "dfx-" + "f" * 56}), response(2),
    ])
    facts = report["static_skill_facts"]
    assert len(facts["actions"]) == 1
    assert facts["resources"][0]["actions"] == []
    assert facts["unread_skill_ids"] == [skill_id]


def test_budget_rejected_siblings_are_authenticated_compactly(root: Path) -> None:
    s = scenario(root)
    profile, _service = prepare(s)
    skill_id = profile["files"][0]["skill_id"]
    siblings = AIMessage(content="", tool_calls=[
        {"name": "read_skill", "args": {"skill_id": skill_id}, "id": f"call-{index}"} for index in range(5)
    ], usage_metadata={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15})
    work = root / "run2"
    report = run_synthetic(s.task, work, "run2", [siblings, response(2)],
        knowledge=HostKnowledgeInput(KnowledgeRetriever(KnowledgeStore(s.library)),
            s.store.approval.model_copy(update={"run_id": "run2"})),
        applicability_policy=ApplicabilityPolicy(enabled=False),
        static_skill_policy=StaticSkillPolicy(enabled=True, entry_ids=s.selected), max_tool_calls=1)
    facts = report["static_skill_facts"]
    assert len(facts["actions"]) == len(facts["resources"][0]["actions"]) == 1
    assert len(facts["budget_rejections"]) == 1
    assert facts["budget_rejections"][0]["count"] == facts["budget_rejections"][0]["completed_count"] == 4
    assert facts["budget_rejections"][0]["unknown_count"] == 0
    assert report["completion"]["cost"]["tool_calls"] == 5


def test_rehashed_shortened_receipt_cannot_replace_full_skill_body(root: Path) -> None:
    s, work, report, _skill_id = sdk_run(root, max_calls=1)
    opened = open_run(s.task, work)
    action = opened[3].records()[0]
    with closing(sqlite3.connect(work / "tool-actions.sqlite")) as connection, connection:
        raw = connection.execute("SELECT message_json FROM tool_actions WHERE action_id=?",
                                 (action["action_id"],)).fetchone()[0]
        message = json.loads(raw)
        answer = json.loads(message["content"])
        answer["body"] = "short summary"
        message["content"] = encode(answer)
        payload = canonical(message)
        connection.execute("UPDATE tool_actions SET message_json=?,message_sha256=? WHERE action_id=?",
            (payload, sha256(payload), action["action_id"]))
    opened = open_run(s.task, work)
    before = tree_bytes(root)
    with pytest.raises(ValueError, match="complete frozen guidance"):
        handoff.audit_run_static_skills(opened[0], s.task, opened[1], report["knowledge_profile"],
            report["static_skill_profile"], opened[2], opened[3])
    assert tree_bytes(root) == before


def test_old_run_cannot_acquire_a_static_namespace_without_a_profile(root: Path) -> None:
    s = scenario(root)
    (s.workspace.root / NAMESPACE).mkdir()
    before = tree_bytes(root)
    with pytest.raises(ValueError, match="cannot acquire static"):
        handoff.audit_stored_static_skills(s.workspace, s.task, s.ledger, None, None)
    assert tree_bytes(root) == before


def test_static_solver_closure_above_knowledge_limit_closes_both_full_reads(root: Path) -> None:
    # The actual solver domain permits multiple originals over the knowledge
    # domain's unchanged aggregate. This uses real bytes, not a larger config.
    size = MAX_CLOSURE_BYTES // 2 + 1
    paths = [root / "first.json", root / "second.json"]
    raw = b"x" * size
    for path in paths:
        path.write_bytes(raw)
    reads = handoff._StaticSolverReadSet(root, 2)
    for path in paths:
        assert reads.read(path) == raw
    assert reads.byte_count == 2 * size > MAX_CLOSURE_BYTES
    assert reads.read_calls == 2 and reads.read_bytes == 2 * size
    reads.finish()
    assert reads.read_calls == 4 and reads.read_bytes == 4 * size
    assert reads.max_read_bytes == 2 * handoff.MAX_SOLVER_INPUT_TOTAL_BYTES
    assert reads.max_read_calls == 4


def test_static_solver_shared_single_and_total_limits_remain_exact(root: Path) -> None:
    reads = handoff._StaticSolverReadSet(root, 8)
    single = handoff.MAX_SOLVER_INPUT_BYTES
    total = handoff.MAX_SOLVER_INPUT_TOTAL_BYTES
    assert single == MAX_CLOSURE_BYTES and total == 8 * single
    for number in range(8):
        reads.reserve(root / f"model-{number + 1}.json", single)
    assert reads.byte_count == total and len(reads.sizes) == 8
    reads.reserve(root / "model-1.json", single)
    assert reads.byte_count == total
    with pytest.raises(ValueError, match="shared total limit"):
        reads.reserve(root / "model-9.json", 1)
    with pytest.raises(ValueError, match="per-file limit"):
        handoff._StaticSolverReadSet(root, 1).reserve(root / "too-large.json", single + 1)
    with pytest.raises(ValueError, match="size changed"):
        reads.reserve(root / "model-1.json", single - 1)


def test_static_solver_rejects_real_oversize_before_reading(root: Path) -> None:
    path = root / "too-large.json"
    path.write_bytes(b"x" * (handoff.MAX_SOLVER_INPUT_BYTES + 1))
    reads = handoff._StaticSolverReadSet(root, 1)
    with pytest.raises(ValueError, match="per-file limit"):
        reads.read(path)
    assert reads.read_calls == reads.read_bytes == reads.byte_count == 0


def test_static_solver_finish_rereads_count_against_exact_cumulative_call_budget(root: Path) -> None:
    path = root / "one.json"
    path.write_bytes(b"same original")
    reads = handoff._StaticSolverReadSet(root, 1)
    raw = reads.read(path)
    reads.finish()
    assert reads.read_calls == 2 and reads.read_bytes == 2 * len(raw)
    with pytest.raises(ValueError, match="cumulative read budget"):
        reads.finish()
    assert reads.read_calls == 2 and reads.read_bytes == 2 * len(raw)


def test_static_solver_finish_rereads_count_against_exact_cumulative_byte_budget(root: Path, monkeypatch) -> None:
    # Reduce only this unit-test cap to exercise its boundary cheaply; the
    # production constants and actual >8 MiB closure test above stay intact.
    monkeypatch.setattr(handoff, "MAX_SOLVER_INPUT_TOTAL_BYTES", 8)
    path = root / "one.json"
    path.write_bytes(b"12345678")
    reads = handoff._StaticSolverReadSet(root, 3)
    reads.read(path)
    reads.finish()
    assert reads.read_calls == 2 and reads.read_bytes == reads.max_read_bytes == 16
    with pytest.raises(ValueError, match="cumulative read budget"):
        reads.read(path)
    assert reads.read_calls == 2 and reads.read_bytes == 16


def test_static_solver_finish_rejects_same_length_restored_mtime_change(root: Path) -> None:
    path = root / "one.json"
    path.write_bytes(b"original")
    reads = handoff._StaticSolverReadSet(root, 1)
    reads.read(path)
    info = path.stat()
    path.write_bytes(b"modified")
    os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns))
    with pytest.raises(ValueError):
        reads.finish()
    assert reads.read_calls == 2 and reads.read_bytes == 16


@pytest.mark.parametrize("max_calls", [True, 0, -1, 1.5])
def test_static_solver_read_set_requires_explicit_positive_call_budget(root: Path, max_calls) -> None:
    with pytest.raises(ValueError, match="read budget is invalid"):
        handoff._StaticSolverReadSet(root, max_calls)
