"""Production domain procedures reach SDK inputs; scripted reads do not test reasoning."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from test_agent_loop import task_snapshot
from test_provider_loop import scripted_adapter, wire_response
from test_readonly_evidence import tree_bytes

from dfx_agent.agent import run_provider_agent
from dfx_agent.applicability_handoff import ApplicabilityPolicy
from dfx_agent.domain_skills import FAMILIES, prepare_domain_knowledge
from dfx_agent.skill_packages import package_metadata
from dfx_agent.static_skills import STATIC_SKILL_MARKER, StaticSkillPolicy
from dfx_agent.verification import FixedTextExecutor
from dfx_agent.working_context_plan import WorkingContextPolicy

RESOURCES = Path(__file__).resolve().parents[1] / "skill"


@pytest.fixture
def root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("ds")


@pytest.mark.parametrize("family", FAMILIES)
def test_production_domain_skill_full_sdk_read_and_completed_restore(root: Path, family: str) -> None:
    task, work = task_snapshot(root), root / "r"
    host, entry_ids = prepare_domain_knowledge(RESOURCES, root / "k", task, "run1")
    original = (RESOURCES / family / "SKILL.md").read_bytes()
    metadata = package_metadata(original.decode("utf-8"), family, f"/approved-skills/{family}/SKILL.md")
    skill_id = family
    policy = StaticSkillPolicy(enabled=True, entry_ids=entry_ids)
    context = WorkingContextPolicy(enabled=True, history_selection="repair_frontier",
                                   context_layout="decision_frontier")
    requests: list[dict] = []
    adapter = scripted_adapter([
        wire_response(1, [("read_skill", {"skill_id": skill_id})]),
        wire_response(2),
    ], requests)
    executor = FixedTextExecutor("src/main.ets", "value=good\n")
    report = run_provider_agent(task, work, "run1", adapter, executor, knowledge=host,
        applicability_policy=ApplicabilityPolicy(enabled=True), static_skill_policy=policy,
        working_context_policy=context, max_calls=3, max_tool_calls=3)

    assert len(requests) == report["model_calls"] == 2
    assert report["tool_calls"] == 1 and report["rejected_tool_calls"] == report["uncertain_tool_calls"] == 0
    assert report["application_verification"] == "not_run"
    assert report["completion"]["verification"]["accepted"] is False
    assert executor.calls == []
    files = report["static_skill_profile"]["files"]
    assert len(files) == len(FAMILIES) == 4
    catalog = "\n".join(message["content"] for message in requests[0]["messages"] if message["role"] == "system")
    assert STATIC_SKILL_MARKER in catalog
    assert all(file["skill_id"] in catalog and file["metadata"]["description"] in catalog for file in files)
    selected = next(file for file in files if file["skill_id"] == skill_id)
    assert selected["metadata"] == metadata

    receipt = next(message for message in requests[1]["messages"] if message["role"] == "tool"
                   and message["tool_call_id"] == "call-1-0")
    result = json.loads(receipt["content"])
    assert result["skill_id"] == skill_id
    assert result["body"].encode("utf-8") == (work / selected["path"]).read_bytes()
    assert result["body"].encode("utf-8") == original
    assert "## Complete approved projection" not in result["body"]
    assert report["static_skill_profile"]["renderer"] == "standard_skill_v1"
    assert result["application_verification"] == "not_run"
    facts = report["static_skill_facts"]
    resources = {resource["skill_id"]: resource for resource in facts["resources"]}
    assert len(resources[skill_id]["actions"]) == 1
    assert resources[skill_id]["actions"][0]["visible_solver_count"] == 1
    unread = sorted(set(resources) - {skill_id})
    assert facts["unread_skill_ids"] == unread
    assert all(resources[identifier]["actions"] == [] for identifier in unread)

    originals = tree_bytes(work / "static-skills")
    saved_profile = (work / "static-skills-profile.json").read_bytes()
    restored = run_provider_agent(task, work, "run1", adapter, executor, resume=True,
        knowledge_approval=host.approval, applicability_policy=ApplicabilityPolicy(enabled=True),
        static_skill_policy=policy, working_context_policy=context, max_calls=3, max_tool_calls=3)
    assert len(requests) == 2 and restored["model_calls_this_turn"] == 0
    assert restored["model_calls"] == report["model_calls"] and restored["tool_calls"] == 1
    assert restored["static_skill_facts"] == facts
    assert restored["application_verification"] == "not_run"
    assert restored["completion"]["verification"]["accepted"] is False
    assert tree_bytes(work / "static-skills") == originals
    assert (work / "static-skills-profile.json").read_bytes() == saved_profile
    assert executor.calls == []
