"""Deterministic host summaries; model prose and synthetic checks cannot certify repair."""

from __future__ import annotations

import json
import stat
from contextlib import closing
from pathlib import Path
from typing import Any

from .actions import ToolActionStore
from .conditions import ConditionStore
from .context_compaction import CompactionPolicy, CompactionStore
from .contracts import CandidatePatch
from .decisions import DecisionStore
from .diagnostics import AnalysisStore
from .model_calls import ModelCallConflict, ModelCallStore, canonical, sha256
from .models import context_profile
from .provenance import validate_session_provenance
from .task import digest, snapshot_digest
from .verification import CheckLedger
from .working_context_plan import WorkingContextPolicy, WorkingContextStore
from .workspace import CandidateWorkspace, _manifest


def _owned_file(workspace: CandidateWorkspace, path: Path) -> Path:
    relative = path.relative_to(workspace.root)
    if not relative.parts:
        raise ValueError("report source must be a file within its workspace")
    info = path.lstat()
    current = workspace.root
    for part in relative.parts:
        current /= part
        info = current.lstat()
        if (stat.S_ISLNK(info.st_mode) or
                getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)):
            raise ValueError("report source is linked")
    if not path.is_file() or info.st_nlink != 1:
        raise ValueError("report source is missing or has shared storage")
    if not path.resolve(strict=True).is_relative_to(workspace.root.resolve(strict=True)):
        raise ValueError("report source is outside its workspace")
    return path


def _session(workspace: CandidateWorkspace, ledger: CheckLedger) -> dict[str, Any] | None:
    path = workspace.root / "session.json"
    if not path.exists():
        return None
    value = json.loads(_owned_file(workspace, path).read_text(encoding="utf-8"))
    if (not isinstance(value, dict) or value.get("run_id") != workspace.run_id or
            value.get("protocol_sha256") != ledger.executor.protocol_sha256):
        raise ValueError("report session or verification protocol differs")
    validate_session_provenance(value)
    profile = value.get("context_profile", {})
    if value["schema_version"] == 10:
        policy = WorkingContextPolicy.model_validate(profile["host_working_context"])
        if canonical(profile) != canonical(context_profile(
            CompactionPolicy.model_validate(profile["host_compaction"]), policy
        )):
            raise ValueError("report working context profile differs")
    elif "host_working_context" in profile or "working_context_prompt_sha256" in profile:
        raise ValueError("historical session cannot acquire a working context profile")
    return value


def _model_facts(workspace: CandidateWorkspace, store: ModelCallStore) -> list[dict[str, Any]]:
    _owned_file(workspace, store.path)
    if store.path != workspace.root / "model-calls.sqlite":
        raise ValueError("report model ledger belongs to another workspace")
    with closing(store._connect()) as connection:
        identifiers = [row[0] for row in connection.execute("SELECT call_id FROM model_calls ORDER BY rowid")]
    found = []
    for identifier in identifiers:
        try:
            record = store.get(identifier)
            if (record is None or record.intent.task_id != workspace.task_id or record.intent.run_id != workspace.run_id):
                raise ValueError("report model call belongs to another task or run")
            if record.state == "completed":
                store.replay(record.intent)
        except ModelCallConflict as exc:
            raise ValueError("report model request or receipt changed") from exc
        found.append(record.model_dump(mode="json"))
    return found


def _action_facts(workspace: CandidateWorkspace, actions: ToolActionStore,
                  model_calls: ModelCallStore) -> tuple[list[dict[str, Any]], dict[str, str]]:
    _owned_file(workspace, actions.path)
    if actions.path != workspace.root / "tool-actions.sqlite":
        raise ValueError("report tool ledger belongs to another workspace")
    records = actions.records()
    contents: dict[str, str] = {}
    for item in records:
        parent = model_calls.get(item["model_call_id"])
        if (parent is None or parent.state != "completed" or parent.intent.kind != "solver" or
                parent.intent.task_id != workspace.task_id or parent.intent.run_id != workspace.run_id):
            raise ValueError("report action has no completed model call for this run")
        message, _ = model_calls.replay(parent.intent)
        matching = [call for call in message.tool_calls if call["name"] == item["tool_name"] and
                    "action-" + sha256(canonical([workspace.task_id, workspace.run_id,
                                                   item["model_call_id"], call["id"]])) == item["action_id"]]
        if len(matching) != 1:
            raise ValueError("report action does not match its model call")
        if item["request_sha256"] != sha256(canonical(matching[0])):
            raise ValueError("report tool request fingerprint differs from its model call")
        proposal = item["proposal"]
        if item["authorization"] == "allowed" and proposal is not None and (
                proposal["task_id"] != workspace.task_id or proposal["run_id"] != workspace.run_id or
                proposal["source_snapshot_sha256"] != workspace.source_snapshot_sha256):
            raise ValueError("report action proposal belongs to another task or version")
        if item["state"] == "completed":
            receipt = actions.receipt(item["action_id"])
            if receipt.tool_call_id != matching[0]["id"]:
                raise ValueError("report tool receipt names another model tool call")
            if isinstance(receipt.content, str):
                contents[item["action_id"]] = receipt.content
    return records, contents


def _localization(bundle: dict[str, Any], action_id: str | None) -> dict[str, Any]:
    diagnosis = bundle["diagnosis"]
    if any(hypothesis.support_status != "unknown" for hypothesis in diagnosis.hypotheses):
        raise ValueError("offline report cannot confirm a model diagnosis")
    selected = diagnosis.selected_hypothesis_id
    hypotheses = sorted(diagnosis.hypotheses, key=lambda item: item.hypothesis_id != selected)
    locations = []
    seen = set()
    for hypothesis in hypotheses:
        for location in hypothesis.root_cause_locations:
            key = canonical(location.model_dump(mode="json"))
            if key not in seen:
                locations.append(location.model_dump(mode="json"))
                seen.add(key)
    return {"diagnosis_id": diagnosis.record_id, "action_id": action_id,
            "root_cause_locations": locations, "authority": "model_inference",
            "root_cause_confirmation": "unknown", "selected_hypothesis_id": selected,
            "diagnosis": diagnosis.model_dump(mode="json"),
            "evidence": [item.model_dump(mode="json") for item in bundle["evidence"]],
            "obligations": [item.model_dump(mode="json") for item in bundle["obligations"]]}


def _candidate_facts(workspace: CandidateWorkspace, ledger: CheckLedger,
                     bundles: dict[str, Any], obligations: dict[str, Any]) -> list[dict[str, Any]]:
    if not workspace.candidates.is_dir() or workspace.candidates.is_symlink():
        raise ValueError("report candidates directory is missing or linked")
    found = []
    for path in workspace.candidates.iterdir():
        if not path.is_dir() or path.is_symlink():
            raise ValueError("report candidate directory has an unsafe entry")
        record = CandidatePatch.model_validate_json(_owned_file(workspace, path / "candidate.json").read_bytes())
        patch = _owned_file(workspace, path / "patch.diff")
        manifest = _owned_file(workspace, path / "manifest.json")
        workspace.require_registered(path.name)
        if record.diagnosis_id is not None and record.diagnosis_id not in bundles:
            raise ValueError("report candidate refers to an unregistered diagnosis")
        if any(identifier not in obligations for identifier in (*record.restores, *record.preserves)):
            raise ValueError("report candidate refers to an unregistered obligation")
        check_key = digest(f"{workspace.run_id}:{record.candidate_id}:offline:{ledger.executor.protocol_sha256}".encode())
        check_path = ledger.root / f"{check_key}.json"
        if check_path.exists():
            _owned_file(workspace, check_path)
        result = ledger.completed(record.candidate_id)
        state = "not_requested"
        if check_path.exists():
            intent = json.loads(_owned_file(workspace, check_path).read_text(encoding="utf-8"))
            state = "completed" if result is not None else "pending"
            if result is None and intent.get("state") != "pending":
                raise ValueError("report verification intent has no valid result")
        found.append({"candidate": record.model_dump(mode="json"),
                      "patch_path": patch.relative_to(workspace.root).as_posix(),
                      "patch_sha256": digest(patch.read_bytes()), "manifest_sha256": digest(manifest.read_bytes()),
                      "check_state": state, "check": None if result is None else result.model_dump(mode="json")})
    found.sort(key=lambda item: item["candidate"]["ordinal"])
    if len({item["candidate"]["ordinal"] for item in found}) != len(found):
        raise ValueError("report candidates have duplicate ordinals")
    return found


def build_completion(workspace: CandidateWorkspace, ledger: CheckLedger, model_calls: ModelCallStore,
                     actions: ToolActionStore, *, stopped_reason: str | None) -> dict[str, Any]:
    """Summarize persisted host facts; no provider, tool or application is executed."""
    if ledger.workspace is not workspace or ledger.root != workspace.root / "checks":
        raise ValueError("report verification ledger belongs to another workspace")
    if stopped_reason is not None and (not isinstance(stopped_reason, str) or not stopped_reason):
        raise ValueError("report stopped reason must be a nonempty host reason or null")
    if snapshot_digest(_manifest(workspace.base)) != workspace.source_snapshot_sha256:
        raise ValueError("report baseline changed")
    session = _session(workspace, ledger)
    provenance = validate_session_provenance(session) if session is not None else None
    model_records = _model_facts(workspace, model_calls)
    context = session.get("context_profile", {}) if session is not None else {}
    compaction_policy = CompactionPolicy.model_validate(context.get("host_compaction", {}))
    model_profile = (session["model_profile"] if session is not None else
                     {"transport": "fixed_response", "provider_id": "dfxfixed", "model_id": "offline"})
    compactions = CompactionStore(workspace.root, workspace.task_id, workspace.run_id,
                                 compaction_policy, model_profile).audit(model_calls)
    current_session = session is not None and session["schema_version"] == 10
    working_plans = (WorkingContextStore(workspace, None, None,
        WorkingContextPolicy.model_validate(context["host_working_context"]), model_profile).audit(
            model_calls, actions, compactions) if current_session else None)
    action_records, receipts = _action_facts(workspace, actions, model_calls)
    decisions = [record.model_dump(mode="json") for record in DecisionStore(workspace).records().values()]
    conditions = ConditionStore(workspace)
    observations_sha256 = conditions.observations_sha256
    if session is not None and observations_sha256 != session["observations_sha256"]:
        raise ValueError("report condition inputs differ from its session")
    condition_results = [record.model_dump(mode="json") for record in conditions.records().values()]
    policy_features = session["policy_features"] if session is not None else None
    offered_tools = sorted({name for record in model_records for name in record["intent"]["tool_names"]})
    analysis = AnalysisStore(workspace)
    bundles, obligations = analysis.bundles(), analysis.obligations()
    candidate_artifacts = _candidate_facts(workspace, ledger, bundles, obligations)
    first_edit = next((item["action_id"] for item in action_records
                       if item["tool_name"] == "edit_source" and item["authorization"] == "allowed"), None)
    diagnosis_order: list[str] = []
    diagnosis_actions: dict[str, str] = {}
    initial_id = None
    before_edit = True
    for item in action_records:
        if item["action_id"] == first_edit:
            before_edit = False
        if (item["tool_name"] != "record_diagnosis" or item["authorization"] != "allowed" or
                item["state"] != "completed"):
            continue
        try:
            payload = json.loads(receipts.get(item["action_id"], ""))
        except (TypeError, ValueError):
            continue
        if not isinstance(payload, dict) or not isinstance(payload.get("diagnosis_id"), str):
            continue
        identifier = payload["diagnosis_id"]
        if identifier not in bundles:
            raise ValueError("report diagnosis receipt refers to a missing registered record")
        if identifier not in diagnosis_actions:
            diagnosis_order.append(identifier)
            diagnosis_actions[identifier] = item["action_id"]
            if initial_id is None and before_edit:
                initial_id = identifier
    latest_id = diagnosis_order[-1] if diagnosis_order else None
    latest = candidate_artifacts[-1] if candidate_artifacts else None
    linked_id = latest["candidate"]["diagnosis_id"] if latest is not None else None
    latest_status = latest["check"]["status"] if latest is not None and latest["check"] is not None else "not_run"
    usage = model_calls.usage_summary(workspace.task_id, workspace.run_id)
    unknown_tools = sum(item["state"] != "completed" for item in action_records)
    pending_checks = sum(item["check_state"] == "pending" for item in candidate_artifacts)
    undispatched_models = sum(item["state"] == "reserved" for item in model_records)
    rejected_tools = sum(item["authorization"] == "rejected" for item in action_records)
    budget_rejected = any(item["rejection"] == "budget_exhausted" for item in action_records)
    if stopped_reason == "model_result_unknown" and not usage["uncertain_calls"]:
        raise ValueError("report claims an unknown model result without ledger evidence")
    if stopped_reason == "action_result_unknown" and not unknown_tools:
        raise ValueError("report claims an unknown tool result without ledger evidence")
    if stopped_reason == "action_budget" and not budget_rejected:
        raise ValueError("report claims an exhausted tool budget without ledger evidence")
    if (stopped_reason == "model_budget" and session is not None and
            usage["reserved_calls"] < session["max_calls"] and
            usage["known_total_tokens"] < session["max_known_tokens"]):
        raise ValueError("report claims an exhausted model budget without ledger evidence")
    budget_exhausted = budget_rejected or stopped_reason in ("model_budget", "action_budget", "wall_time")
    unknown = bool(usage["uncertain_calls"] or unknown_tools or pending_checks or undispatched_models)
    provider_failure = stopped_reason in ("provider_format_error", "provider_request_unsupported",
                                         "context_limit", "context_format_unsupported", "context_summary_invalid",
                                         "context_compaction_input_limit", "fixed_compaction_responses_exhausted")
    provider_failure = provider_failure or any(item["outcome"] == "invalid" for item in compactions)
    if current_session and stopped_reason is not None and stopped_reason.startswith("working_context_"):
        provider_failure = True
    suggested_status = ("unresolved" if unknown or provider_failure else "budget_exhausted" if budget_exhausted else
                        "offline_checked" if latest_status == "pass" else "unresolved")
    unsequenced = sorted(set(bundles) - set(diagnosis_order))
    reasons = ["application_verification_not_run"]
    if initial_id is None:
        reasons.append("initial_localization_missing")
    if latest_id is None:
        reasons.append("localization_missing")
    else:
        reasons.append("root_cause_unconfirmed")
    if unsequenced:
        reasons.append("diagnosis_order_unknown")
    if latest is None:
        reasons.append("candidate_missing")
    else:
        if linked_id is None:
            reasons.append("candidate_diagnosis_missing")
        elif linked_id != latest_id:
            reasons.append("latest_diagnosis_not_linked_to_latest_candidate")
        if latest_status != "pass":
            reasons.append({"not_run": "latest_candidate_check_missing", "fail": "latest_candidate_check_failed",
                            "inconclusive": "latest_candidate_check_inconclusive"}[latest_status])
    if usage["uncertain_calls"]:
        reasons.append("model_result_unknown")
    if unknown_tools:
        reasons.append("action_result_unknown")
    if pending_checks:
        reasons.append("check_result_unknown")
    if undispatched_models:
        reasons.append("model_request_not_dispatched")
    if budget_exhausted:
        reasons.append("budget_exhausted")
    if stopped_reason is not None and stopped_reason not in reasons:
        reasons.append(stopped_reason)
    usage_complete = not (usage["missing_usage_calls"] or usage["invalid_usage_calls"] or
                          usage["reserved_calls"] != usage["completed_calls"])
    facts = {"analysis": {identifier: _localization(bundle, diagnosis_actions.get(identifier))
                          for identifier, bundle in sorted(bundles.items())},
             "obligations": {identifier: item.model_dump(mode="json") for identifier, item in sorted(obligations.items())},
             "candidate_artifacts": candidate_artifacts, "actions": action_records, "model_calls": model_records,
             "context_compactions": compactions,
             "decisions": decisions, "condition_results": condition_results,
             "observations_sha256": observations_sha256,
             "session": session}
    if current_session:
        facts["working_context_plans"] = working_plans
    verified_fields = ["schema_version", "task_id", "run_id", "source_snapshot_sha256", "status",
                       "application_verification", "completion", "candidates", "checks", "model_calls",
                       "known_tokens", "missing_usage_calls", "uncertain_model_calls", "tool_calls",
                       "rejected_tool_calls", "uncertain_tool_calls", "actions", "decisions", "condition_results"]
    unverified_fields = ["adapter_error_reason", "model_calls_this_turn", "known_tokens_this_turn",
                         "missing_usage_calls_this_turn"]
    (verified_fields if session is not None else unverified_fields).append("policy_features")
    (verified_fields if model_records else unverified_fields).append("offered_tools")
    if provenance is not None:
        verified_fields.append("execution_provenance")
    if current_session:
        verified_fields.append("working_context_plans")
    value = {"schema_version": 4 if current_session else (2 if provenance is None else 3),
             "task_id": workspace.task_id, "run_id": workspace.run_id,
             "source_snapshot_sha256": workspace.source_snapshot_sha256,
             "authority": "host_summary", "suggested_status": suggested_status, "stopped_reason": stopped_reason,
             "localization": {"initial": None if initial_id is None else facts["analysis"][initial_id],
                              "latest": None if latest_id is None else facts["analysis"][latest_id],
                              "candidate_linked": None if linked_id is None else facts["analysis"][linked_id],
                              "initial_cutoff_action_id": first_edit, "ordering": "tool_action_ledger",
                              "diagnosis_order": diagnosis_order, "unsequenced_diagnosis_ids": unsequenced},
             "candidate_artifacts": candidate_artifacts,
             "context_compactions": compactions,
             "latest_candidate_id": None if latest is None else latest["candidate"]["candidate_id"],
             "latest_candidate_check_status": latest_status,
             "historical_passed_candidate_ids": [item["candidate"]["candidate_id"] for item in candidate_artifacts
                                                  if item["check"] is not None and item["check"]["status"] == "pass"],
             "execution_facts": {"actions": action_records, "decisions": decisions,
                                 "condition_results": condition_results, "policy_features": policy_features,
                                 "offered_tools": offered_tools if model_records else None},
             "report_validation": {"verified_top_level_fields": sorted(verified_fields),
                                   "unverified_top_level_fields": sorted(unverified_fields),
                                   "recomputation_input_field": "stopped_reason"},
             "verification": {"level": "offline_synthetic" if any(item["check"] is not None
                                                                         for item in candidate_artifacts) else "not_run",
                              "application_verification": "not_run", "accepted": False,
                              "protocol_sha256": ledger.executor.protocol_sha256,
                              "pending_checks": pending_checks},
             "unresolved_reasons": reasons,
             "cost": {**usage, "usage_complete": usage_complete,
                      "calls_by_kind": {kind: sum(record["intent"]["kind"] == kind for record in model_records)
                                        for kind in ("solver", "context_compaction", "format_repair", "sdk_retry")},
                      "total_tokens": usage["known_total_tokens"] if usage_complete else None,
                      "monetary_cost": None, "tool_calls": len(action_records), "rejected_tool_calls": rejected_tools,
                      "uncertain_tool_calls": unknown_tools, "undispatched_model_calls": undispatched_models,
                      "check_requests": sum(item["check_state"] != "not_requested" for item in candidate_artifacts)},
             "timing": {"localization_ms": None, "repair_ms": None}, "facts_sha256": sha256(canonical(facts))}
    if provenance is not None:
        value["execution_provenance"] = provenance
    if current_session:
        value["working_context_plans"] = working_plans
    return {**value, "summary_sha256": sha256(canonical(value))}


def read_completion(workspace: CandidateWorkspace, ledger: CheckLedger, model_calls: ModelCallStore,
                    actions: ToolActionStore) -> dict[str, Any]:
    """Recompute a report from saved facts; historical provenance never probes this host."""
    report = json.loads(_owned_file(workspace, workspace.root / "agent-report.json").read_text(encoding="utf-8"))
    if (not isinstance(report, dict) or type(report.get("schema_version")) is not int or
            report["schema_version"] not in (5, 6, 7) or report.get("task_id") != workspace.task_id or
            report.get("run_id") != workspace.run_id or
            report.get("source_snapshot_sha256") != workspace.source_snapshot_sha256):
        raise ValueError("completion report schema, task, run or snapshot differs")
    if report.get("application_verification") != "not_run" or "stopped_reason" not in report:
        raise ValueError("completion report application claim or stop identity differs")
    value = build_completion(workspace, ledger, model_calls, actions, stopped_reason=report["stopped_reason"])
    expected_schema = {2: 5, 3: 6, 4: 7}[value["schema_version"]]
    if report["schema_version"] != expected_schema:
        raise ValueError("completion report and session schema differ")
    if expected_schema == 5 and "execution_provenance" in report:
        raise ValueError("legacy completion report cannot claim recorded execution provenance")
    if report.get("status") != value["suggested_status"]:
        raise ValueError("completion report status differs from host facts")
    if canonical(report.get("completion")) != canonical(value):
        raise ValueError("completion report differs from current host facts")
    artifacts = value["candidate_artifacts"]
    expected = {"candidates": [item["candidate"]["candidate_id"] for item in artifacts],
                "checks": [item["check"] for item in artifacts if item["check"] is not None],
                "model_calls": value["cost"]["reserved_calls"],
                "known_tokens": value["cost"]["known_total_tokens"],
                "missing_usage_calls": value["cost"]["missing_usage_calls"] + value["cost"]["invalid_usage_calls"],
                "uncertain_model_calls": value["cost"]["uncertain_calls"],
                "tool_calls": value["cost"]["tool_calls"],
                "rejected_tool_calls": value["cost"]["rejected_tool_calls"],
                "uncertain_tool_calls": value["cost"]["uncertain_tool_calls"]}
    if "execution_provenance" in value:
        expected["execution_provenance"] = value["execution_provenance"]
    if "working_context_plans" in value:
        expected["working_context_plans"] = value["working_context_plans"]
    elif "working_context_plans" in report:
        raise ValueError("historical report cannot acquire working context plans")
    execution = value["execution_facts"]
    for key in ("actions", "decisions", "condition_results", "policy_features", "offered_tools"):
        if key in value["report_validation"]["verified_top_level_fields"]:
            expected[key] = execution[key]
    if any(canonical(report.get(key)) != canonical(item) for key, item in expected.items()):
        raise ValueError("completion report top-level facts differ from its host summary")
    return value
