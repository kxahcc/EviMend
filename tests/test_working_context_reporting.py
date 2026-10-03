"""New send-plan evidence is explicit; historical summaries remain untouched."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from test_agent_loop import response
from test_provenance_reporting import _open, _save
from test_readonly_evidence import tree_bytes
from test_reporting import run_case

from dfx_agent.model_calls import canonical, sha256
from dfx_agent.reporting import build_completion, read_completion
from dfx_agent.working_context_plan import WorkingContextPolicy, WorkingContextStore


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture
def current_off(tmp_path: Path):
    run_case(tmp_path, [response(1)])
    return tmp_path


def test_new_generation_freezes_disabled_policy_without_backfilling_historical_keys(current_off) -> None:
    report = read_json(current_off / "run/agent-report.json")
    session = read_json(current_off / "run/session.json")
    assert (session["schema_version"], report["schema_version"], report["completion"]["schema_version"]) == (10, 7, 4)
    assert session["context_profile"]["host_working_context"]["enabled"] is False
    assert report["working_context_plans"] == report["completion"]["working_context_plans"] == []
    assert "working_context_plans" in report["completion"]["report_validation"]["verified_top_level_fields"]
    before = tree_bytes(current_off)
    assert read_completion(*_open(current_off)) == report["completion"]
    assert tree_bytes(current_off) == before


@pytest.mark.parametrize("schema", [8, 9])
def test_historical_generation_keeps_exact_canonical_shape_and_skips_new_audit(
    current_off, monkeypatch: pytest.MonkeyPatch, schema: int,
) -> None:
    session_path = current_off / "run/session.json"
    session = read_json(session_path)
    session["schema_version"] = schema
    if schema == 8:
        session.pop("execution_provenance")
    session["context_profile"].pop("host_working_context")
    session["context_profile"].pop("working_context_prompt_sha256")
    session["context_profile"].pop("working_context_format", None)
    _save(session_path, session)
    report_path = current_off / "run/agent-report.json"
    report = read_json(report_path)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("historical report cannot run a new selector audit")

    monkeypatch.setattr(WorkingContextStore, "audit", forbidden)
    expected = build_completion(*_open(current_off), stopped_reason=report["stopped_reason"])
    assert expected["schema_version"] == (2 if schema == 8 else 3)
    assert "working_context_plans" not in expected
    assert "working_context_plans" not in expected["report_validation"]["verified_top_level_fields"]
    report.update(schema_version=5 if schema == 8 else 6, completion=expected)
    report.pop("working_context_plans")
    if schema == 8:
        report.pop("execution_provenance")
    _save(report_path, report)
    frozen, before = canonical(expected), tree_bytes(current_off)
    assert canonical(read_completion(*_open(current_off))) == frozen
    assert tree_bytes(current_off) == before
    report["working_context_plans"] = []
    _save(report_path, report)
    with pytest.raises(ValueError, match="historical report"):
        read_completion(*_open(current_off))


@pytest.mark.parametrize("tamper", ["missing_top", "changed_top", "changed_summary"])
def test_new_report_cannot_forge_plan_facts_even_with_a_rehashed_summary(current_off, tamper: str) -> None:
    path = current_off / "run/agent-report.json"
    report = read_json(path)
    if tamper == "missing_top":
        report.pop("working_context_plans")
    elif tamper == "changed_top":
        report["working_context_plans"] = [{"invented": True}]
    else:
        report["completion"]["working_context_plans"] = [{"invented": True}]
        report["completion"]["summary_sha256"] = sha256(canonical({
            key: value for key, value in report["completion"].items() if key != "summary_sha256"
        }))
    _save(path, report)
    with pytest.raises(ValueError):
        read_completion(*_open(current_off))


def test_enabled_plan_audit_is_read_only_and_returns_independent_materialized_facts(tmp_path: Path) -> None:
    run_case(tmp_path, [response(1)], working_context_policy=WorkingContextPolicy(enabled=True))
    workspace, ledger, calls, actions = _open(tmp_path)
    session = read_json(tmp_path / "run/session.json")
    store = WorkingContextStore(workspace, None, None,
        WorkingContextPolicy.model_validate(session["context_profile"]["host_working_context"]), session["model_profile"])
    before = tree_bytes(tmp_path)
    result = store.audit(calls, actions, [])
    original = copy.deepcopy(result)
    assert len(result) == 1
    result[0].clear()
    assert store.audit(calls, actions, []) == original
    assert read_completion(workspace, ledger, calls, actions)["working_context_plans"] == original
    assert tree_bytes(tmp_path) == before
