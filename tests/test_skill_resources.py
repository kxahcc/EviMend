"""Standard packages reach actual SDK requests without granting execution."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from test_agent_loop import task_snapshot
from test_provider_loop import scripted_adapter, wire_response

from dfx_agent.agent import run_provider_agent
from dfx_agent.applicability_handoff import ApplicabilityPolicy
from dfx_agent.domain_skills import prepare_domain_knowledge
from dfx_agent.static_skills import StaticSkillPolicy
from dfx_agent.verification import FixedTextExecutor
from dfx_agent.working_context_plan import WorkingContextPolicy


def package(root: Path, *, always: bool = False) -> tuple[Path, str, str]:
    source = root / "skill" / "external-check"
    (source / "references").mkdir(parents=True)
    (source / "scripts").mkdir()
    front = 'metadata:\n  dfx-load: always\n' if always else ''
    body = ("---\nname: external-check\ndescription: Inspect external prerequisite observations.\n" +
            front + "---\n\n# EXTERNAL_PROCEDURE\n"
            "Read [details](references/details.md) when the API semantics are unclear.\n")
    reference = "REFERENCE_FULL_TEXT\r\nCheck the current API contract before selecting a repair.\r\n"
    (source / "SKILL.md").write_bytes(body.encode())
    (source / "references/details.md").write_bytes(reference.encode())
    (source / "scripts/helper.py").write_text(
        "from pathlib import Path\nPath('must-not-execute').write_text('wrong')\n", encoding="utf-8")
    return source, body, reference


def run(root: Path, calls: list[dict], *, always: bool = False):
    source, body, reference = package(root, always=always)
    task = task_snapshot(root)
    host, entries = prepare_domain_knowledge(source.parent, root / "k", task, "run1")
    requests: list[dict] = []
    policy = StaticSkillPolicy(enabled=True, entry_ids=entries)
    context = WorkingContextPolicy(enabled=True, history_selection="repair_frontier",
                                   context_layout="decision_frontier")
    adapter = scripted_adapter([wire_response(index, [("read_skill", args)])
                                for index, args in enumerate(calls, 1)] +
                               [wire_response(len(calls) + 1)], requests)
    executor = FixedTextExecutor("src/main.ets", "value=good\n")
    report = run_provider_agent(task, root / "r", "run1", adapter, executor, knowledge=host,
        applicability_policy=ApplicabilityPolicy(enabled=True), static_skill_policy=policy,
        working_context_policy=context, max_calls=6, max_tool_calls=5)
    return report, requests, (task, host.approval, policy, context, executor), (source, body, reference)


def result(request: dict) -> dict:
    return json.loads(next(message["content"] for message in reversed(request["messages"])
                           if message["role"] == "tool"))


def test_reference_only_read_is_visible_but_skill_body_stays_unread_and_resume_frozen(tmp_path: Path) -> None:
    report, requests, args, originals = run(tmp_path, [
        {"skill_id": "external-check", "resource_path": "references/details.md"}])
    source, body, reference = originals
    assert len(requests) == 2
    assert body not in json.dumps(requests[0], ensure_ascii=False)
    assert "REFERENCE_FULL_TEXT" not in json.dumps(requests[0], ensure_ascii=False)
    assert result(requests[1])["body"] == reference
    assert result(requests[1])["resource_path"] == "references/details.md"
    facts = report["static_skill_facts"]
    assert facts["unread_skill_ids"] == ["external-check"]
    assert facts["resources"][0]["actions"] == []
    assert facts["resources"][0]["resource_actions"][0]["visible_solver_count"] == 1
    assert report["application_verification"] == "not_run"
    assert report["completion"]["verification"]["accepted"] is False
    # A later author edit does not replace the saved package of this task.
    (source / "references/details.md").write_text("new author revision", encoding="utf-8")
    (source / "SKILL.md").write_text("new author revision", encoding="utf-8")
    task, approval, policy, context, executor = args
    before = {path.relative_to(tmp_path / "r").as_posix(): path.read_bytes()
              for path in (tmp_path / "r").rglob("*") if path.is_file() and path.name != "agent-report.json"}
    restore_requests: list[dict] = []
    resumed = run_provider_agent(task, tmp_path / "r", "run1", scripted_adapter([], restore_requests), executor,
        resume=True, knowledge_approval=approval, applicability_policy=ApplicabilityPolicy(enabled=True),
        static_skill_policy=policy, working_context_policy=context, max_calls=6, max_tool_calls=5)
    assert resumed["static_skill_facts"] == facts
    after = {path.relative_to(tmp_path / "r").as_posix(): path.read_bytes()
             for path in (tmp_path / "r").rglob("*") if path.is_file() and path.name != "agent-report.json"}
    # The report refreshes per-turn counters; frozen sources and call/action
    # ledgers remain byte-identical and no additional model call is made.
    assert resumed["model_calls_this_turn"] == resumed["known_tokens_this_turn"] == 0
    assert restore_requests == [] and after == before


def test_body_and_reference_both_survive_context_selection_as_distinct_reads(tmp_path: Path) -> None:
    report, requests, _args, originals = run(tmp_path, [{"skill_id": "external-check"},
        {"skill_id": "external-check", "resource_path": "references/details.md"}])
    _, body, reference = originals
    answers = [json.loads(message["content"]) for message in requests[-1]["messages"]
               if message["role"] == "tool"]
    assert {answer["body"] for answer in answers} == {body, reference}
    facts = report["static_skill_facts"]
    assert facts["unread_skill_ids"] == []
    assert len(facts["resources"][0]["actions"]) == len(facts["resources"][0]["resource_actions"]) == 1


@pytest.mark.parametrize("path", ["../SKILL.md", "/outside.md", "references/missing.md"])
def test_unapproved_reference_does_not_acquire_file_access(tmp_path: Path, path: str) -> None:
    report, requests, _args, _originals = run(tmp_path, [{"skill_id": "external-check", "resource_path": path}])
    assert any(message["content"] == "rejected: static_skill_source_invalid"
               for message in requests[-1]["messages"] if message["role"] == "tool")
    facts = report["static_skill_facts"]
    assert facts["unread_skill_ids"] == ["external-check"]
    assert facts["resources"][0]["resource_actions"] == []


def test_reading_a_bundled_script_returns_text_without_executing(tmp_path: Path) -> None:
    report, requests, _args, originals = run(tmp_path, [
        {"skill_id": "external-check", "resource_path": "scripts/helper.py"}])
    source, _body, _reference = originals
    assert result(requests[-1])["body"] == (source / "scripts/helper.py").read_bytes().decode("utf-8")
    assert not list(tmp_path.rglob("must-not-execute"))
    assert report["static_skill_facts"]["unread_skill_ids"] == ["external-check"]


def test_explicit_always_loading_sends_body_but_not_support_text_before_tools(tmp_path: Path) -> None:
    report, requests, _args, originals = run(tmp_path, [], always=True)
    _source, body, _reference = originals
    system = "\n".join(message["content"] for message in requests[0]["messages"] if message["role"] == "system")
    assert body in system and "REFERENCE_FULL_TEXT" not in system
    assert report["model_calls"] == 1 and report["tool_calls"] == 0
    assert report["static_skill_facts"]["always_loaded_skill_ids"] == ["external-check"]
    assert report["static_skill_facts"]["resources"][0]["actions"] == []
