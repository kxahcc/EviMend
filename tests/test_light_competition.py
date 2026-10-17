"""Full SDK/software wiring with mock cloud I/O, not model or ArkTS efficacy."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import httpx
import pytest
from test_source_execution import source_config as _source_config

from dfx_agent import competition
from dfx_agent.competition import run_competition, verify_private_delivery
from dfx_agent.model_calls import ModelCallStore, canonical
from dfx_agent.model_provider import CloudChatCompletionsAdapter
from dfx_agent.task import PROFILE, digest, snapshot_digest
from dfx_agent.working_context_plan import WORKING_CONTEXT_PROMPT

REPOSITORY = Path(__file__).resolve().parents[1]
KEY_NAME = "DFX_LIGHT_COMPETITION_MOCK_KEY"


class _RepairWire:
    """Deterministic responses exercise real SDK tools and preserve diagnosis gates."""

    def __init__(self, *, first_bad: bool = False) -> None:
        self.requests: list[dict[str, Any]] = []
        self.candidate = ""
        self.first_bad = first_bad
        self.candidate_ids: list[str] = []
        self.feedback_id = ""
        self.seen_feedback: dict[str, Any] | None = None
        self.seen_inspection: dict[str, Any] | None = None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.requests.append(body)
        last = body["messages"][-1]
        if last["role"] == "tool":
            content = last["content"]
            assert not content.startswith("rejected:"), content
            try:
                receipt = json.loads(content)
            except ValueError:
                receipt = None
            if isinstance(receipt, dict) and "candidate_id" in receipt and "ordinal" in receipt:
                self.candidate = receipt["candidate_id"]
                self.candidate_ids.append(self.candidate)
            if isinstance(receipt, dict) and "feedback_id" in receipt:
                self.feedback_id = receipt["feedback_id"]
        steps: list[tuple[str, dict[str, Any]]] = [
            ("read_log", {}),
            ("read_source", {"path": "calc.py"}),
            ("record_diagnosis", {"path": "calc.py", "line": 2,
                                  "reason": "The arithmetic decreases the value rather than preserving the expected increment."}),
            ("edit_source", {"path": "calc.py", "old": "return n - 1", "new": "return n + 1"}),
            ("register_candidate", {"diagnosis_id": "diagnosis-1"}),
            ("run_check", {"candidate_id": self.candidate}),
        ]
        position = len(self.requests) - 1
        if self.first_bad:
            steps[3:] = [
                ("record_obligation", {"obligation_id": "preserve-increment",
                    "statement": "Preserve the increment behavior on normal and boundary inputs.",
                    "evidence_ids": ["source-1"]}),
                ("edit_source", {"path": "calc.py", "old": "return n - 1", "new": "return 3"}),
                ("register_candidate", {"diagnosis_id": "diagnosis-1", "preserves": ["preserve-increment"]}),
                ("run_check", {"candidate_id": self.candidate}),
                ("inspect_analysis", {"validation_candidate_id": self.candidate}),
                ("link_failed_check", {"candidate_id": self.candidate,
                                       "violated_obligation_ids": ["preserve-increment"]}),
                ("edit_source", {"path": "calc.py", "old": "return n - 1", "new": "return n + 1"}),
                ("register_candidate", {"diagnosis_id": "diagnosis-1", "preserves": ["preserve-increment"]}),
                ("run_check", {"candidate_id": self.candidate}),
            ]
            if position == 8:
                inspection = json.loads(last["content"])["validation"]
                failure = inspection["saved_check"]["failed_stage"]
                assert failure["role"] == "candidate" and failure["stage"] == "behavior"
                assert "normal input behavior changed" in failure["output"]["stderr"]["text"]
                assert inspection["autonomous_test_generation"] is False
                self.seen_inspection = inspection
            if position == 9:
                # Require the actual next provider request to contain the saved
                # source failure before supplying the second scripted edit.
                # The current context panel names required groups by reference;
                # the full latest tool group is sent as ordinary wire messages.
                # Inspect those actual bytes, never reconstruct them from a
                # panel reference or assume the old inline-panel encoding.
                names = {call["id"]: call["function"]["name"] for message in body["messages"]
                         for call in message.get("tool_calls", [])}
                assert last["role"] == "tool" and names[last["tool_call_id"]] == "link_failed_check"
                feedback = json.loads(last["content"])
                assert feedback["feedback_id"] == self.feedback_id
                assert feedback["candidate_id"] == self.candidate_ids[0]
                assert feedback["scope"] == "source" and feedback["evidence_kind"] == "source_execution"
                assert "normal input behavior changed" in feedback["observation"]
                self.seen_feedback = feedback
        calls = []
        if position < len(steps):
            name, arguments = steps[position]
            calls = [{"id": f"light-tool-{position + 1}", "type": "function",
                      "function": {"name": name, "arguments": canonical(arguments)}}]
        return httpx.Response(200, json={"id": f"light-response-{position + 1}", "model": body["model"],
            "choices": [{"index": 0, "finish_reason": "tool_calls" if calls else "stop",
                         "message": {"role": "assistant", "content": "" if calls else "Software fixture completed.",
                                     "tool_calls": calls}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}})


@pytest.fixture
def managed(monkeypatch: pytest.MonkeyPatch) -> Path:
    root = REPOSITORY / ".dfx-local"
    if os.name != "nt" or not (root / "state/active.json").exists():
        pytest.skip("competition software checks require the existing managed Windows test Job")
    lease = json.loads((root / "state/active.json").read_text(encoding="utf-8-sig"))
    assert lease["task"] == "test"
    monkeypatch.setenv("DFX_MANAGED_COMPETITION", "local-v1")
    monkeypatch.setenv(KEY_NAME, "synthetic-never-real-light-key")
    return root


def _prepare(tmp_path: Path, *, source_checks: bool, first_bad: bool = False) -> tuple[Path, Path]:
    inputs = tmp_path / "inputs"
    project = inputs / "project"
    project.mkdir(parents=True)
    (project / "calc.py").write_text("def transform(n):\n    return n - 1\n", encoding="utf-8")
    log = inputs / "fault.log"
    log.write_text("ReplayTargetFault at calc.py:2\n", encoding="utf-8")
    task = tmp_path / "task"
    # This approved Python software fixture is not an ArkTS application or the
    # competition-data importer. Independent expectations stay outside it.
    (task / "source").mkdir(parents=True)
    original = (project / "calc.py").read_bytes()
    (task / "source/calc.py").write_bytes(original)
    manifest = [{"path": "calc.py", "sha256": digest(original)}]
    (task / "source-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (task / "crash.log").write_bytes(log.read_bytes())
    (task / "task.json").write_text(json.dumps({"schema_version": 1, "profile": PROFILE,
        "task_id": "light-competition-task", "source_root": "source", "source_manifest": "source-manifest.json",
        "source_snapshot_sha256": snapshot_digest(manifest), "crash_log": "crash.log",
        "crash_sha256": digest(log.read_bytes())}), encoding="utf-8")
    execution = _source_config(tmp_path, project).model_dump(mode="json") if source_checks else None
    value = json.loads((REPOSITORY / "configs/competition-demo.json").read_bytes())
    value.update(model_mode="cloud", execution=execution,
        provider={"endpoint": "https://light-model.invalid/chat/completions", "credential_env": KEY_NAME,
                  "network": {"max_retries": 2, "request_timeout_seconds": 30.0, "connect_timeout_seconds": 10.0}},
        model={"provider_id": "compatible", "model_id": "light-scripted-wire", "max_output_tokens": 1024,
               "timeout_seconds": 30.0},
        policy_features={"rule_guidance": False, "obligation_tracking": first_bad, "decision_checks": False},
        working_context={"enabled": True, "history_selection": "repair_frontier", "context_layout": "decision_frontier"},
        applicability_enabled=False, static_skills_enabled=False,
        max_calls=16 if first_bad else 10, max_tool_calls=20, max_seconds=300)
    settings = tmp_path / "run-config.json"
    settings.write_text(json.dumps(value), encoding="utf-8")
    return task, settings


def _mock_cloud(monkeypatch: pytest.MonkeyPatch, wire: _RepairWire) -> tuple[Path, bytes, Any]:
    policy_path = REPOSITORY / "configs/local-runtime.json"
    original_read = Path.read_bytes
    original_policy = original_read(policy_path)

    def read_bytes(path: Path) -> bytes:
        raw = original_read(path)
        if path == policy_path:
            value = json.loads(raw)
            value["execution"]["allow_model_experiments"] = True
            return json.dumps(value).encode("utf-8")
        return raw

    def adapter(settings: Any, provider: Any, *, allow_network: bool,
                transport: httpx.MockTransport | None) -> CloudChatCompletionsAdapter:
        assert allow_network is True and transport is None
        # Retain the official SDK and async retry path. Only HTTP transport is
        # a MockTransport; disable network admission as an additional guard.
        return CloudChatCompletionsAdapter(settings, provider, allow_network=False, transport=httpx.MockTransport(wire))

    monkeypatch.setattr(Path, "read_bytes", read_bytes)
    monkeypatch.setattr(competition, "CloudChatCompletionsAdapter", adapter)
    return policy_path, original_policy, original_read


@pytest.mark.parametrize("source_checks", [True, False], ids=["source-verification", "analysis-only"])
def test_light_competition_sdk_async_wire_candidate_delivery_and_completed_resume(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, managed: Path, source_checks: bool) -> None:
    assert tmp_path.is_relative_to(managed / "tmp")
    task, settings = _prepare(tmp_path, source_checks=source_checks)
    wire = _RepairWire()
    policy_path, original_policy, original_read = _mock_cloud(monkeypatch, wire)
    work = tmp_path / "run"
    run_id = "light-source" if source_checks else "light-analysis"
    result = run_competition(task, work, run_id, settings, REPOSITORY)
    report = json.loads((work / "competition-report.json").read_bytes())
    solver = json.loads((work / "agent-report.json").read_bytes())
    assert result["status"] == ("source_verified" if source_checks else "patch_proposed"), solver
    assert result["accepted"] is source_checks
    assert report["schema_version"] == 4 and len(report["attempts"]) == 1
    assert report["application_verification"] == result["application_verification"] == "not_run"
    assert report["localization_result"]["final"]["complete"] is True
    candidate = report["attempts"][0]
    assert candidate["candidate"]["diagnosis_id"] == "diagnosis-1"
    assert candidate["check"]["level"] == ("source_execution" if source_checks else "not_run")
    assert report["source_verification"] == ("pass" if source_checks else "not_run")
    patch = (work / candidate["patch_path"]).read_text(encoding="utf-8")
    assert "return n - 1" in patch and "return n + 1" in patch
    if source_checks:
        execution = candidate["execution"]
        assert execution["baseline"]["fault_reproduced"] is True
        assert [stage["stage"] for stage in execution["stages"]] == ["build", "target", "behavior", "regression"]
        assert all(stage["status"] == "pass" for stage in execution["stages"])
        assert execution["application_success"] is False and execution["application_verification"] == "not_run"
        assert report["validation_scope"] == execution["validation_scope"]
        assert report["mocked_interfaces"] == execution["mocked_interfaces"] == []
    else:
        assert candidate["execution"] is None and candidate["check"]["status"] == "inconclusive"
        assert report["validation_scope"] == "analysis_only"
        assert not (work / "local-execution").exists()
    session = json.loads((work / "session.json").read_bytes())
    assert session["model_profile"]["sdk"]["api"] == "chat.completions.async"
    assert solver["rejected_tool_calls"] == solver["uncertain_tool_calls"] == solver["uncertain_model_calls"] == 0
    assert len(wire.requests) == result["model_calls"] == 7
    assert len(solver["working_context_plans"]) == 7
    plans = [json.loads(path.read_bytes()) for path in (work / "working-context").glob("*.plan.json")]
    assert len(plans) == 7 and all(plan["policy"]["context_layout"] == "decision_frontier" and
                                  plan["policy"]["history_selection"] == "repair_frontier" for plan in plans)
    startup = canonical(wire.requests[0]["messages"])
    assert ("source_execution" if source_checks else "analysis_only") in startup
    if source_checks:
        assert report["validation_scope"] in startup and "mocked_interfaces" in startup
    calls = ModelCallStore(work / "model-calls.sqlite", read_only=True)
    records = calls.records("light-competition-task", run_id)
    assert len(records) == 7 and all(record.state == "completed" for record in records)
    assert all(calls.replay(record.intent)[0].response_metadata["dfx_network"]["network_attempts"] == 1 for record in records)
    manifest = verify_private_delivery(work / "delivery.zip", 268435456)
    assert any(item["path"].endswith("patch.diff") for item in manifest["files"])
    saved_report = (work / "competition-report.json").read_bytes()
    saved_checks = {path.name: path.read_bytes() for path in (work / "checks").glob("*.json")}
    resumed = run_competition(task, work, run_id, settings, REPOSITORY, resume=True)
    assert resumed["status"] == result["status"] and resumed["accepted"] is source_checks
    assert len(wire.requests) == resumed["model_calls"] == 7
    assert (work / "competition-report.json").read_bytes() == saved_report
    assert {path.name: path.read_bytes() for path in (work / "checks").glob("*.json")} == saved_checks
    assert original_read(policy_path) == original_policy


def test_source_business_failure_reaches_actual_wire_then_second_candidate_passes_and_resumes(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, managed: Path) -> None:
    assert tmp_path.is_relative_to(managed / "tmp")
    task, settings = _prepare(tmp_path, source_checks=True, first_bad=True)
    wire = _RepairWire(first_bad=True)
    policy_path, original_policy, original_read = _mock_cloud(monkeypatch, wire)
    work = tmp_path / "run"
    run_id = "light-source-feedback"
    result = run_competition(task, work, run_id, settings, REPOSITORY)
    report = json.loads((work / "competition-report.json").read_bytes())
    solver = json.loads((work / "agent-report.json").read_bytes())
    assert result["status"] == "source_verified" and result["accepted"] is True, {
        "stopped_reason": solver["stopped_reason"], "adapter_error_reason": solver["adapter_error_reason"],
        "model_calls": solver["model_calls"], "rejected_tool_calls": solver["rejected_tool_calls"],
        "uncertain_model_calls": solver["uncertain_model_calls"], "attempts": len(report["attempts"]),
        "last_sent": wire.requests[-1]["messages"][-1] if wire.requests else None,
        "last_check": report["attempts"][-1]["check"] if report["attempts"] else None,
    }
    assert report["schema_version"] == 4 and report["source_verification"] == "pass"
    assert report["application_verification"] == "not_run" and report["mocked_interfaces"] == []
    assert len(report["attempts"]) == 2 and wire.candidate_ids[0] != wire.candidate_ids[1]
    first, final = report["attempts"]
    assert [attempt["check"]["status"] for attempt in report["attempts"]] == ["fail", "pass"]
    assert all(attempt["candidate"]["diagnosis_id"] == "diagnosis-1" and
               attempt["candidate"]["preserves"] == ["preserve-increment"] and
               attempt["candidate"]["decision_id"] is None for attempt in report["attempts"])
    assert [stage["status"] for stage in first["execution"]["stages"]] == ["pass", "pass", "fail"]
    assert first["execution"]["stages"][-1]["stage"] == "behavior"
    assert "normal input behavior changed" in first["check"]["detail"]
    assert [stage["stage"] for stage in final["execution"]["stages"]] == ["build", "target", "behavior", "regression"]
    assert all(stage["status"] == "pass" for stage in final["execution"]["stages"])
    assert all(attempt["check"]["level"] == "source_execution" and
               attempt["execution"]["baseline"]["fault_reproduced"] is True and
               attempt["execution"]["application_verification"] == "not_run" and
               attempt["execution"]["application_success"] is False for attempt in report["attempts"])
    assert report["localization_result"]["final"]["complete"] is True
    assert report["metrics"]["first_attempt_pass"] is False
    assert report["metrics"]["source_repair_success"] is True
    assert report["metrics"]["application_repair_success"] is None
    feedback = json.loads((work / "feedback" / (wire.feedback_id + ".json")).read_bytes())
    assert wire.seen_feedback is not None
    assert wire.seen_inspection is not None
    assert {key: value for key, value in wire.seen_feedback.items() if key != "feedback_id"} == feedback
    assert feedback["candidate_id"] == first["candidate"]["candidate_id"]
    assert feedback["execution_id"] == first["check"]["idempotency_key"]
    assert feedback["scope"] == "source" and feedback["evidence_kind"] == "source_execution"
    assert feedback["violated_obligation_ids"] == ["preserve-increment"]
    assert len(wire.requests) == result["model_calls"] == 13
    panels = [json.loads(body["messages"][1]["content"].split(WORKING_CONTEXT_PROMPT + "\n", 1)[1])
              for body in wire.requests]
    failures = [panel["decision_context"]["revision"]["latest_failure"] for panel in panels[9:12]]
    assert all(failure is not None and failure["candidate_id"] == first["candidate"]["candidate_id"] and
               failure["check_result"]["status"] == "fail" and
               failure["check_result"]["level"] == "source_execution" for failure in failures)
    assert panels[-1]["decision_context"]["revision"]["latest_failure"] is None
    assert len(solver["working_context_plans"]) == 13
    assert solver["rejected_tool_calls"] == solver["uncertain_tool_calls"] == solver["uncertain_model_calls"] == 0
    calls = ModelCallStore(work / "model-calls.sqlite", read_only=True)
    records = calls.records("light-competition-task", run_id)
    assert len(records) == 13 and all(record.state == "completed" for record in records)
    assert all(calls.replay(record.intent)[0].response_metadata["dfx_network"]["network_attempts"] == 1 for record in records)
    assert (work / first["patch_path"]).read_bytes() != (work / final["patch_path"]).read_bytes()
    saved_report = (work / "competition-report.json").read_bytes()
    saved_checks = {path.name: path.read_bytes() for path in (work / "checks").glob("*.json")}
    saved_feedback = {path.name: path.read_bytes() for path in (work / "feedback").glob("*.json")}
    verify_private_delivery(work / "delivery.zip", 268435456)
    resumed = run_competition(task, work, run_id, settings, REPOSITORY, resume=True)
    assert resumed["status"] == "source_verified" and resumed["accepted"] is True
    assert len(wire.requests) == resumed["model_calls"] == 13
    assert (work / "competition-report.json").read_bytes() == saved_report
    assert {path.name: path.read_bytes() for path in (work / "checks").glob("*.json")} == saved_checks
    assert {path.name: path.read_bytes() for path in (work / "feedback").glob("*.json")} == saved_feedback
    assert original_read(policy_path) == original_policy
