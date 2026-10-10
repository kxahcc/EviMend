"""Competition product entry: existing SDK loop, curated procedures and local checks.

The scripted demo is solely a software acceptance fixture. Cloud mode never
substitutes scripted answers; its network and application permissions are fixed
by the project owner's runtime policy before any writable execution starts.
"""

from __future__ import annotations

import json
import os
import sys
import zipfile
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from .actions import ToolActionStore
from .agent import ResumeAdmissionRejected, preflight_resume, run_provider_agent
from .applicability_contracts import TaskKnowledgeBinding
from .applicability_handoff import ApplicabilityPolicy
from .contracts import SourceLocation
from .domain_skills import prepare_domain_knowledge
from .evidence_io import local_evidence_path
from .local_execution import LocalApplicationExecutor, LocalCheckConfig, _checked
from .model_calls import ModelCallStore, canonical
from .model_provider import (
    CloudChatCompletionsAdapter,
    CloudProviderConfig,
    ProviderSettings,
)
from .policies import PolicyFeatures
from .progress import emit_progress, progress_scope
from .static_skills import StaticSkillPolicy
from .task import digest, prepare_task, verify_task
from .verification import _atomic_json
from .working_context_plan import WorkingContextPolicy
from .workspace import CandidateWorkspace


class CompetitionConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, hide_input_in_errors=True)
    schema_version: Literal[1] = 1
    model_mode: Literal["cloud", "mock_demo"]
    provider: CloudProviderConfig
    model: ProviderSettings
    execution: LocalCheckConfig | None = None
    policy_features: PolicyFeatures = PolicyFeatures(rule_guidance=True, obligation_tracking=True, decision_checks=True)
    working_context: WorkingContextPolicy = WorkingContextPolicy(enabled=True)
    applicability_enabled: bool = True
    static_skills_enabled: bool = True
    max_calls: int = Field(default=40, ge=1, le=200)
    max_known_tokens: int = Field(default=256000, ge=1)
    max_seconds: int = Field(default=900, ge=1, le=1800)
    max_candidates: int = Field(default=4, ge=1, le=20)
    max_tool_calls: int = Field(default=100, ge=1, le=500)

    @model_validator(mode="after")
    def valid_mode(self) -> CompetitionConfig:
        if self.model_mode == "cloud" and self.execution is None:
            raise ValueError("cloud mode requires a reviewed full project and all four check commands")
        if self.model_mode == "mock_demo" and self.execution is not None:
            raise ValueError("mock_demo uses only its declared synthetic project")
        return self


def prepare_demo(root: Path, *, resume: bool = False) -> tuple[Path, LocalCheckConfig]:
    """Build a toy fault and independent oracle, outside any competition dataset."""
    if not root.exists():
        if resume:
            raise ValueError("original demo input is missing; resume never recreates it")
        project = root / "project"
        (project / "src").mkdir(parents=True)
        (project / "src/main.ets").write_text("value=bad\n", encoding="utf-8")
        (project / "asset.bin").write_bytes(b"demo resource excluded from analysis")
        (project / "oracle.py").write_text(
            "from pathlib import Path\nimport sys\n"
            "stage=sys.argv[1]\nvalue=Path('src/main.ets').read_text().strip()\n"
            "if stage=='build':\n assert Path('asset.bin').read_bytes().startswith(b'demo')\n"
            "elif stage=='target' and value=='value=bad':\n print('DEMO_FAULT');sys.exit(7)\n"
            "elif stage in ('behavior','regression') and value!='value=good':\n"
            " print('required value semantics lost');sys.exit(9)\n", encoding="utf-8")
        log = root / "fault.log"
        log.write_text("DEMO_FAULT at src/main.ets:1\n", encoding="utf-8")
        prepare_task(root, {"task_id": "competition-demo", "app": "project",
            "log": {"path": "fault.log", "sha256": digest(log.read_bytes())}}, root.parent / (root.name + "-task"))
    task = root.parent / (root.name + "-task")
    project = root / "project"
    return task, LocalCheckConfig.model_validate_json(canonical({"project_root": str(project), "mode": "synthetic",
        "commands": {stage: {"argv": [sys.executable, "oracle.py", stage], "timeout_seconds": 10}
                     for stage in ("build", "target", "behavior", "regression")},
        "baseline_target_exit_codes": [7], "baseline_fault_pattern": "DEMO_FAULT", "max_check_seconds": 90}))


class _DemoModel:
    """Use real tool receipts for IDs; never send this fixture in cloud mode."""

    def __init__(self, task: Path, run_id: str):
        self.task = verify_task(task)
        self.run_id = run_id
        self.position = 0
        self.candidate = ""
        self.decision = ""
        self.feedback = ""

    def restore(self, work: Path) -> None:
        calls = ModelCallStore(work / "model-calls.sqlite", read_only=True)
        for record in calls.records(self.task["task_id"], self.run_id, kind="solver"):
            if record.state == "completed":
                message, _ = calls.replay(record.intent)
                if isinstance(message.id, str) and message.id.startswith("demo-response-"):
                    self.position = max(self.position, int(message.id.rsplit("-", 1)[1]))
        actions = ToolActionStore(work / "tool-actions.sqlite", read_only=True)
        for action in actions.records():
            if action["state"] == "completed":
                content = actions.receipt(action["action_id"]).content
                if isinstance(content, str):
                    self._receipt(content)

    def _receipt(self, text: str) -> None:
        if text.startswith("rejected:"):
            raise RuntimeError("demo tool rejected")
        try:
            receipt = json.loads(text)
        except ValueError:
            return
        if isinstance(receipt, dict):
            if "candidate_id" in receipt and "ordinal" in receipt:
                self.candidate = receipt["candidate_id"]
            if "next_action" in receipt:
                self.decision = receipt["record_id"]
            if "feedback_id" in receipt:
                self.feedback = receipt["feedback_id"]

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        messages = body["messages"]
        if messages[-1]["role"] == "tool":
            self._receipt(messages[-1]["content"])
        import re

        skill = re.search(r"dfx-[a-f0-9]{56}", canonical(messages))
        source_sha = self.task["source_snapshot_sha256"]
        plan: dict[str, Any] = {"task_id": self.task["task_id"], "run_id": self.run_id,
            "diagnosis_id": "diagnosis-1", "plan": {"source_snapshot_sha256": source_sha,
                "directions": [{"direction_id": "preserve-direction", "repair_key": "restore-value",
                    "acceptance_key": "preserve-value", "support_evidence_ids": ["source-1"],
                    "obligation_ids": ["preserve-value"], "hypothesis_ids": ["hypothesis-1"]}],
                "selected_direction_id": "preserve-direction", "remaining_probe_budget": 0}}
        if self.feedback:
            plan.update(candidate_id=self.candidate, feedback_id=self.feedback)
        steps: list[tuple[str, dict[str, Any]]] = [
            ("read_log", {}), ("read_source", {"path": "src/main.ets"}),
            ("record_diagnosis", {"path": "src/main.ets", "line": 1, "reason": "invalid toy value violates required output"}),
            ("read_skill", {"skill_id": skill.group() if skill else "missing"}),
            ("record_obligation", {"obligation_id": "preserve-value", "statement": "preserve required value semantics",
                                   "evidence_ids": ["source-1"]}),
            ("plan_repair_action", {"plan_json": canonical(plan)}),
            ("edit_source", {"path": "src/main.ets", "old": "bad", "new": "almost"}),
            ("register_candidate", {"diagnosis_id": "diagnosis-1", "preserves": ["preserve-value"],
                                    "decision_id": self.decision, "direction_id": "preserve-direction"}),
            ("run_check", {"candidate_id": self.candidate}),
            ("link_failed_check", {"candidate_id": self.candidate, "violated_obligation_ids": ["preserve-value"]}),
            ("plan_repair_action", {"plan_json": canonical(plan)}),
            ("edit_source", {"path": "src/main.ets", "old": "bad", "new": "good"}),
            ("register_candidate", {"diagnosis_id": "diagnosis-1", "preserves": ["preserve-value"],
                                    "decision_id": self.decision, "direction_id": "preserve-direction"}),
            ("run_check", {"candidate_id": self.candidate}),
        ]
        calls = []
        if self.position < len(steps):
            name, args = steps[self.position]
            calls = [{"id": f"demo-{self.position + 1}", "type": "function",
                      "function": {"name": name, "arguments": canonical(args)}}]
        self.position += 1
        return httpx.Response(200, json={"id": f"demo-response-{self.position}", "model": body["model"],
            "choices": [{"index": 0, "finish_reason": "tool_calls" if calls else "stop",
                "message": {"role": "assistant", "content": "" if calls else "Synthetic fixture finished.", "tool_calls": calls}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}})


def _previous_report(work: Path, task: dict[str, Any], run_id: str) -> dict[str, Any] | None:
    """Read the original report before writable resume; hashes are not signatures."""
    path = work / "competition-report.json"
    if not path.exists():
        return None
    value = json.loads(_checked(path).read_bytes())
    original = {key: item for key, item in value.items() if key != "report_sha256"}
    if (value.get("schema_version") not in (1, 2, 3) or value.get("scope") != "competition_task" or
            value.get("task_id") != task["task_id"] or value.get("run_id") != run_id or
            value.get("source_snapshot_sha256") != task["source_snapshot_sha256"] or
            value.get("report_sha256") != digest(canonical(original).encode())):
        raise ResumeAdmissionRejected("competition report source or integrity differs")
    if value["schema_version"] in (2, 3):
        start = datetime.fromisoformat(json.loads(_checked(work / "session.json").read_bytes())["started_at"])
        _validate_report_timing(value, start)
    return value


def _terminal_facts(facts: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in facts.items() if key not in {"timing", "report_sha256"}}


def _progress_facts(facts: dict[str, Any]) -> dict[str, Any]:
    """Comparable legacy watermarks; derived execution labels are not progress."""
    return {key: facts[key] for key in ("task_id", "run_id", "source_snapshot_sha256", "method",
                                      "execution_provenance", "cost", "localization", "obligations", "decisions")} | {
        "attempts": [{key: attempt[key] for key in ("candidate", "patch_path", "patch_sha256", "check")}
                     for attempt in facts["attempts"]]}


def _validate_report_timing(facts: dict[str, Any], start: datetime) -> dict[str, Any]:
    timing = facts["timing"]
    if (start.tzinfo is None or timing.get("terminal_facts_sha256") !=
            digest(canonical(_terminal_facts(facts)).encode())):
        raise ResumeAdmissionRejected("competition terminal timing source differs")
    if timing.get("finished_at") is not None:
        finish = datetime.fromisoformat(timing["finished_at"])
        if finish.tzinfo is None:
            raise ResumeAdmissionRejected("competition terminal timing has no timezone")
        measured = (finish - start).total_seconds() * 1000
        if measured < 0 or timing.get("agent_wall_ms") != measured:
            raise ResumeAdmissionRejected("competition terminal timing differs")
    elif timing.get("agent_wall_ms") is not None or timing.get("reason") != "legacy_terminal_time_unknown":
        raise ResumeAdmissionRejected("competition unknown terminal timing differs")
    return timing


def _report_timing(work: Path, facts: dict[str, Any], previous: dict[str, Any] | None,
                   observed_at: datetime) -> dict[str, Any]:
    start = datetime.fromisoformat(json.loads(_checked(work / "session.json").read_bytes())["started_at"])
    if start.tzinfo is None or observed_at.tzinfo is None:
        raise ValueError("competition timing requires timezone-aware host observations")
    terminal_sha = digest(canonical(_terminal_facts(facts)).encode())
    if previous is not None:
        if previous["schema_version"] in (2, 3):
            timing = _validate_report_timing(previous, start)
            if timing["terminal_facts_sha256"] == terminal_sha:
                return timing
            if previous["schema_version"] == 2 and _progress_facts(previous) == _progress_facts(facts):
                # A stricter derived report is not new model/tool/application work.
                # Keep the original observation while binding it to the new facts.
                return {**timing, "terminal_facts_sha256": terminal_sha}
        elif _progress_facts(previous) == _progress_facts(facts):
            return {"agent_wall_ms": None, "finished_at": None, "terminal_facts_sha256": terminal_sha,
                "authority": "host_report_observation", "scope": "session_start_to_reported_terminal",
                "reason": "legacy_terminal_time_unknown"}
    elapsed = (observed_at - start).total_seconds() * 1000
    if elapsed < 0:
        raise ValueError("competition finish predates its original session")
    return {"agent_wall_ms": elapsed, "finished_at": observed_at.isoformat(), "terminal_facts_sha256": terminal_sha,
            "authority": "host_report_observation", "scope": "session_start_to_reported_terminal", "reason": None}


def _localization_projection(localized: Any, source_sha256: str) -> dict[str, Any]:
    """Derive output completeness without changing the saved SDK localization."""
    result: dict[str, Any] = {"diagnosis_id": None, "selected": None, "alternatives": [],
        "complete": False, "incomplete_reasons": [], "authority": "model_inference",
        "root_cause_confirmation": "unknown"}
    reasons = result["incomplete_reasons"]
    if not isinstance(localized, dict) or not isinstance(localized.get("diagnosis"), dict):
        reasons.append("diagnosis_missing")
        return result
    diagnosis = localized["diagnosis"]
    result["diagnosis_id"] = localized.get("diagnosis_id")
    if (not isinstance(result["diagnosis_id"], str) or not result["diagnosis_id"] or
            diagnosis.get("record_id") != result["diagnosis_id"]):
        reasons.append("diagnosis_identity_invalid")
    if diagnosis.get("source_snapshot_sha256") != source_sha256:
        reasons.append("diagnosis_source_mismatch")
    hypotheses = diagnosis.get("hypotheses")
    if not isinstance(hypotheses, list) or any(
        not isinstance(item, dict) or not isinstance(item.get("hypothesis_id"), str) for item in hypotheses
    ) or len({item["hypothesis_id"] for item in hypotheses}) != len(hypotheses):
        reasons.append("hypotheses_invalid")
        return result
    selected_id = diagnosis.get("selected_hypothesis_id")
    result["alternatives"] = deepcopy([item for item in hypotheses if item["hypothesis_id"] != selected_id])
    if not isinstance(selected_id, str) or not selected_id:
        reasons.append("selected_hypothesis_missing")
        return result
    selected = next((item for item in hypotheses if item["hypothesis_id"] == selected_id), None)
    if selected is None:
        reasons.append("selected_hypothesis_not_found")
        return result
    result["selected"] = deepcopy(selected)
    locations = selected.get("root_cause_locations")
    if not isinstance(locations, list) or not locations:
        reasons.append("selected_root_cause_locations_missing")
        return result
    try:
        for location in locations:
            if SourceLocation.model_validate(location).snapshot_sha256 != source_sha256:
                raise ValueError("root cause uses another source")
    except (ValueError, ValidationError):
        reasons.append("selected_root_cause_locations_invalid")
    result["complete"] = not reasons
    return result


def _localization_result(localization: dict[str, Any], latest: dict[str, Any] | None,
                         source_sha256: str) -> dict[str, Any]:
    result = {key: _localization_projection(localization.get(key), source_sha256)
              for key in ("initial", "latest", "candidate_linked")}
    final = result.pop("candidate_linked")
    final["candidate_id"] = latest["candidate"].get("candidate_id") if latest else None
    candidate_diagnosis = latest["candidate"].get("diagnosis_id") if latest else None
    if latest is None:
        final["incomplete_reasons"].append("candidate_missing")
    elif not candidate_diagnosis:
        final["incomplete_reasons"].append("candidate_diagnosis_missing")
    elif candidate_diagnosis != final["diagnosis_id"]:
        final["incomplete_reasons"].append("candidate_diagnosis_mismatch")
    final["complete"] = final["complete"] and not final["incomplete_reasons"]
    return {**result, "final": final}


def _location_lines(hypothesis: dict[str, Any]) -> list[str]:
    locations = hypothesis.get("root_cause_locations")
    if not isinstance(locations, list):
        return []
    lines = []
    for raw in locations:
        try:
            location = SourceLocation.model_validate(raw)
        except (ValueError, ValidationError):
            continue
        lines.append(f"- {location.path}:{location.start_line}-{location.end_line}")
    return lines


def _report(work: Path, solver: dict[str, Any], executor: LocalApplicationExecutor,
            config: CompetitionConfig, *, previous_report: dict[str, Any] | None = None,
            observed_at: datetime | None = None) -> dict[str, Any]:
    completion = solver["completion"]
    attempts = []
    for artifact in completion["candidate_artifacts"]:
        result = artifact["check"]
        evidence = None if result is None else executor.evidence(result["idempotency_key"])
        attempts.append({"candidate": artifact["candidate"], "patch_path": artifact["patch_path"],
                         "patch_sha256": artifact["patch_sha256"], "check": result, "execution": evidence})
    latest = attempts[-1] if attempts else None
    first_check = attempts[0]["check"] if attempts else None
    application_state = (latest["execution"]["application_verification"]
                         if latest and latest["execution"] else "not_run")
    level = (latest["execution"]["verification_level"] if latest and latest["execution"] else
             latest["check"]["level"] if latest and latest["check"] else "not_run")
    localization_result = _localization_result(completion["localization"], latest, solver["source_snapshot_sha256"])
    accepted = bool(latest and latest["execution"] and application_state == "pass" and latest["execution"]["accepted"] and
                    localization_result["final"]["complete"])
    software_pass = bool(latest and latest["check"] and latest["check"]["status"] == "pass")
    outcome = ("application_verified" if accepted else "synthetic_checked"
               if software_pass and level == "offline_synthetic" else "unresolved")
    # A later failed or unknown candidate must never inherit an older pass.
    if solver["stopped_reason"] or solver["uncertain_model_calls"] or solver["uncertain_tool_calls"]:
        accepted = False
        outcome = "unresolved"
    session = completion["execution_provenance"]
    actions = solver["actions"]
    diagnosis = completion["localization"]
    facts = {"schema_version": 3, "scope": "competition_task", "task_id": solver["task_id"], "run_id": solver["run_id"],
        "status": outcome, "accepted": accepted, "application_verification": application_state, "verification_level": level,
        "root_cause_authority": "model_inference", "localization": diagnosis, "attempts": attempts,
        "localization_result": localization_result,
        "obligations": {path.stem: json.loads(path.read_text(encoding="utf-8"))
                        for path in sorted((work / "obligations").glob("*.json"))},
        "decisions": solver["decisions"], "stopped_reason": solver["stopped_reason"],
        "adapter_error_reason": solver["adapter_error_reason"], "method": config.model_dump(mode="json", exclude={"execution"}),
        "source_snapshot_sha256": solver["source_snapshot_sha256"], "execution_provenance": session,
        "cost": completion["cost"],
        "metrics": {"candidate_count": len(attempts), "repair_rounds": len(attempts), "agent_chain_length": len(actions),
            "first_attempt_pass": (first_check["status"] == "pass" if first_check and
                                   first_check["status"] in ("pass", "fail") else None),
            "root_cause_accuracy": None, "functional_correctness": True if accepted else None,
            "repair_success": True if accepted else None, "stability": None},
        "solver_trace": "agent-report.json", "privacy": "private source, logs, model messages and test output; review before sharing",
        "limitations": ["Stage success covers only host-specified tests, not all possible behavior.",
            ("Synthetic model usage is fixture data, not measured cloud consumption." if config.model_mode == "mock_demo"
             else "Provider-reported usage is recorded; missing usage and monetary cost remain unknown."),
            "Curated skills and obligation links are guidance; application effect and comparative gains require experiments."]}
    facts["timing"] = _report_timing(work, facts, previous_report, observed_at or datetime.now(UTC))
    facts["report_sha256"] = digest(canonical(facts).encode())
    _atomic_json(work / "competition-report.json", facts)
    lines = ["# 故障修复结果", "", f"状态：{outcome}；应用接受：{accepted}",
        f"检查层级：{level}；实际应用检查：{application_state}", "",
        "定位依据和根因解释由模型提出；准确率需独立标签评测。", ""]
    for label, title in (("initial", "初次定位"), ("latest", "最新定位"), ("final", "最新候选关联的最终定位")):
        projected = localization_result[label]
        if label != "final" and projected["diagnosis_id"] is None:
            continue
        lines.append(title + "：")
        if label == "final":
            lines.append(f"候选：{projected['candidate_id'] or '无'}；诊断：{projected['diagnosis_id'] or '无'}")
        selected = projected["selected"]
        if selected is not None:
            lines.append(f"所选假设：{selected['hypothesis_id']}")
            lines.extend(_location_lines(selected))
        else:
            lines.append("尚未选定根因假设。")
        for alternative in projected["alternatives"]:
            lines.append(f"备选假设（未作为所选根因）：{alternative['hypothesis_id']}")
            lines.extend(_location_lines(alternative))
        if not projected["complete"]:
            lines.append("定位输出不完整：" + ", ".join(projected["incomplete_reasons"]))
        localized = diagnosis["candidate_linked" if label == "final" else label]
        if isinstance(localized, dict) and localized.get("evidence"):
            lines.append("登记证据（包含所选与备选的依据）：")
            lines.extend(f"- {item['kind']}：{item['statement']}" for item in localized["evidence"])
        lines.append("")
    lines += ["| 尝试 | 检查 | 补丁 |", "|---|---|---|"]
    for attempt in attempts:
        lines.append(f"| {attempt['candidate']['ordinal']} | {(attempt['check'] or {}).get('status', 'not_run')} | [{attempt['patch_path']}]({attempt['patch_path']}) |")
    for attempt in attempts:
        lines += ["", f"尝试 {attempt['candidate']['ordinal']}：{(attempt['check'] or {}).get('detail', '未运行检查')}"]
        if attempt["execution"]:
            lines.append(f"原故障复现：{(attempt['execution'].get('baseline') or {}).get('fault_reproduced', '未知')}")
            lines.extend(f"- {stage['stage']}：{stage['status']}（{stage['reason']}）"
                         for stage in attempt["execution"]["stages"])
    lines += ["", f"停止原因：{solver['stopped_reason'] or '无'}", "",
        f"模型调用：{solver['model_calls']}；已知 Token：{solver['known_tokens']}；缺用量调用：{solver['missing_usage_calls']}", "",
        "完整定位、修复义务、决策、阶段回执和未决项见 competition-report.json；SDK原始轨迹见 agent-report.json。"]
    (work / "result.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    emit_progress("report_saved", status=outcome, accepted=accepted, verification_level=level,
                  candidate_count=len(attempts), model_calls=solver["model_calls"])
    return facts


def private_delivery(work: Path, task: Path, max_bytes: int) -> Path:
    """Bounded private result bundle; its manifest verifies bytes, not causal truth."""
    target = work / "delivery.zip"
    files: list[tuple[str, Path]] = []
    for prefix, source in (("task", task), ("run", work)):
        for path in source.rglob("*"):
            if path.is_symlink():
                raise ValueError("private delivery contains a link")
            relative = path.relative_to(source).as_posix()
            if path.is_file() and path != target and not relative.endswith((".sqlite-wal", ".sqlite-shm")):
                # Execution work copies are mutable build products. The frozen
                # project-base, source manifests, patches and stage originals suffice.
                if prefix == "run" and relative.startswith("local-execution/") and any(
                        part.startswith(("b-", "c-")) for part in path.relative_to(source).parts[1:2]):
                    continue
                files.append((prefix + "/" + relative, path))
    if sum(path.stat().st_size for _, path in files) > max_bytes:
        raise ValueError("private delivery exceeds the export byte limit")
    originals = {name: path.read_bytes() for name, path in files}
    manifest = {"schema_version": 1, "scope": "private_competition_delivery",
        "verification": "content hashes only; not an independent effect evaluation",
        "files": [{"path": name, "sha256": digest(raw), "bytes": len(raw)} for name, raw in originals.items()]}
    temporary = local_evidence_path(target.with_suffix(".new"))
    with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, raw in originals.items():
            archive.writestr(name, raw)
        archive.writestr("manifest.json", canonical(manifest))
    if temporary.stat().st_size > max_bytes:
        temporary.unlink()
        raise ValueError("private delivery serialized size exceeds the export byte limit")
    verify_private_delivery(temporary, max_bytes)
    temporary.replace(local_evidence_path(target))
    return target


def verify_private_delivery(package: Path, max_bytes: int) -> dict[str, Any]:
    if package.stat().st_size > max_bytes:
        raise ValueError("private delivery exceeds byte limit")
    with zipfile.ZipFile(local_evidence_path(package)) as archive:
        if sum(item.file_size for item in archive.infolist()) > max_bytes:
            raise ValueError("private delivery expanded content exceeds byte limit")
        manifest = json.loads(archive.read("manifest.json"))
        expected = [item["path"] for item in manifest["files"]] + ["manifest.json"]
        if len(set(expected)) != len(expected) or sorted(archive.namelist()) != sorted(expected):
            raise ValueError("private delivery manifest differs")
        for item in manifest["files"]:
            raw = archive.read(item["path"])
            if len(raw) != item["bytes"] or digest(raw) != item["sha256"]:
                raise ValueError("private delivery content changed")
    return manifest


def _run_competition(task: Path, work: Path, run_id: str, config_path: Path,
                    project_root: Path, *, resume: bool = False) -> dict[str, Any]:
    """One managed command; cloud dispatch is opt-in by host policy, never by model."""
    if os.environ.get("DFX_MANAGED_COMPETITION") != "local-v1":
        raise ValueError("competition requires its managed entry point")
    repo = _checked(project_root)
    managed = _checked(repo / ".dfx-local")
    work = _checked(work, exists=resume)
    lease = json.loads(_checked(managed / "state/active.json").read_text(encoding="utf-8-sig"))
    test_scope = lease.get("task") == "test" and work.is_relative_to(managed / "tmp")
    if not work.is_relative_to(managed / "work") and not test_scope:
        raise ValueError("competition work is outside the managed work directory")
    config = CompetitionConfig.model_validate_json(_checked(config_path).read_bytes())
    policy = json.loads(_checked(repo / "configs/local-runtime.json").read_bytes())
    if config.model_mode == "cloud" and policy["execution"]["allow_model_experiments"] is not True:
        raise ValueError("real model experiments are disabled")
    if config.execution and config.execution.mode == "application" and policy["execution"]["allow_application_experiments"] is not True:
        raise ValueError("real application experiments are disabled")
    auxiliary = work.parent if test_scope else managed / "tmp"
    library = auxiliary / ("competition-" + run_id + "-library")
    approval_path = ((auxiliary if test_scope else managed / "state") /
                     ("competition-" + run_id + "-approval.json"))
    mock = config.model_mode == "mock_demo"
    if mock:
        task, execution = prepare_demo(auxiliary / ("competition-" + run_id + "-demo"), resume=resume)
    else:
        task = _checked(task)
        assert config.execution is not None
        execution = config.execution
    frozen = {"config": config.model_dump(mode="json"), "task": verify_task(task), "execution": execution.model_dump(mode="json")}
    approval = None
    knowledge = None
    if resume:
        if json.loads(_checked(work / "competition-config.json").read_bytes()) != frozen:
            raise ResumeAdmissionRejected("competition configuration or task differs from the original run")
        saved = json.loads(_checked(work / "session.json").read_bytes())
        approval = TaskKnowledgeBinding.model_validate_json(_checked(approval_path).read_bytes())
        preflight_resume(task, work, run_id, knowledge_approval=approval)
        previous_report = _previous_report(work, frozen["task"], run_id)
        entry_ids = tuple(saved["static_skill_profile"]["entry_ids"])
    else:
        previous_report = None
        if approval_path.exists():
            raise ValueError("competition approval already exists; choose a new work ID")
        knowledge, entry_ids = prepare_domain_knowledge(repo / "resources/skills", library, task, run_id)
        _atomic_json(approval_path, knowledge.approval.model_dump(mode="json"))
    executor = LocalApplicationExecutor(execution, managed_root=managed,
        allow_application=policy["execution"]["allow_application_experiments"],
        excluded_env_names=(config.provider.credential_env,))
    demo_model = _DemoModel(task, run_id) if mock else None
    if demo_model is not None and resume:
        demo_model.restore(work)
    transport = httpx.MockTransport(demo_model) if demo_model is not None else None
    adapter = CloudChatCompletionsAdapter(config.model, config.provider, allow_network=not mock, transport=transport)
    saved_key = os.environ.get(config.provider.credential_env)
    if mock:
        os.environ[config.provider.credential_env] = "dfx-synthetic-credential-never-real"
    def startup(workspace: CandidateWorkspace) -> None:
        _atomic_json(workspace.root / "competition-config.json", frozen)
        # Position metadata simplifies resume; the original competition/session/
        # approval checks remain the execution authority.
        profile = workspace.root / "product-config.json"
        _atomic_json(profile, config.model_dump(mode="json"))
        _atomic_json(workspace.root / "product-launch.json", {"schema_version": 1,
            "run_id": run_id, "task": str(task), "config_sha256": digest(profile.read_bytes())})
        emit_progress("run_started")

    try:
        solver = run_provider_agent(task, work, run_id, adapter, executor, resume=resume,
            max_calls=config.max_calls, max_known_tokens=config.max_known_tokens, max_seconds=config.max_seconds,
            max_candidates=config.max_candidates, max_tool_calls=config.max_tool_calls,
            policy_features=config.policy_features, working_context_policy=config.working_context,
            knowledge=knowledge, knowledge_approval=approval,
            applicability_policy=ApplicabilityPolicy(enabled=config.applicability_enabled),
            static_skill_policy=StaticSkillPolicy(enabled=config.static_skills_enabled, entry_ids=entry_ids),
            startup_hook=None if resume else startup)
    finally:
        if mock:
            if saved_key is None:
                os.environ.pop(config.provider.credential_env, None)
            else:
                os.environ[config.provider.credential_env] = saved_key
    report = _report(work, solver, executor, config, previous_report=previous_report)
    private_delivery(work, task, policy["limits"]["max_export_package_bytes"])
    emit_progress("run_finished", status=report["status"], accepted=report["accepted"])
    return {"schema_version": 1, "status": report["status"], "accepted": report["accepted"],
        "application_verification": report["application_verification"], "report_path": str(work / "result.md"),
        "candidate_count": len(report["attempts"]), "model_calls": solver["model_calls"]}


def run_competition(task: Path, work: Path, run_id: str, config_path: Path,
                    project_root: Path, *, resume: bool = False) -> dict[str, Any]:
    try:
        with progress_scope(work, run_id):
            return _run_competition(task, work, run_id, config_path, project_root, resume=resume)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        if resume:
            raise ResumeAdmissionRejected("competition resume input or saved evidence is unavailable or incompatible") from exc
        raise
