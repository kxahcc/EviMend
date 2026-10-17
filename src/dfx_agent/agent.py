"""Offline Deep Agents loop with host-owned candidate and check decisions."""

from __future__ import annotations

import json
import sqlite3
import stat
import time
from collections.abc import Callable
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from deepagents import (
    GeneralPurposeSubagentProfile,
    HarnessProfile,
    create_deep_agent,
    register_harness_profile,
)
from deepagents.backends import FilesystemBackend
from deepagents.middleware.filesystem import FilesystemMiddleware
from deepagents.middleware.summarization import SummarizationMiddleware
from langchain.agents.middleware.types import AgentMiddleware
from langchain_core.messages import AIMessage
from langchain_core.runnables.config import RunnableConfig
from langchain_core.tools import tool
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.checkpoint.sqlite import SqliteSaver
from pydantic import ValidationError

from .actions import (
    OFFLINE_CAPABILITIES,
    ToolActionStore,
    ToolActionUncertain,
    ToolAuthorizationMiddleware,
)
from .applicability import ApplicabilityStore
from .applicability_contracts import TaskKnowledgeBinding
from .applicability_handoff import (
    ApplicabilityPolicy,
    applicability_tools,
    audit_run_applicability,
    audit_stored_applicability,
    normalize_applicability_profile,
)
from .conditions import ConditionRequest, ConditionStore, SyntheticObservations
from .context_compaction import CompactionPolicy, CompactionStore
from .contracts import (
    IDENTIFIER,
    Diagnosis,
    Hypothesis,
    RepairObligation,
    SourceLocation,
    admit_evidence,
    execution_evidence_kind,
    require_path,
)
from .decisions import DecisionRequest, DecisionStore, feedback_key
from .diagnostics import AnalysisStore, DiagnosisSubmission
from .evidence_io import local_evidence_path, open_readonly_database
from .knowledge_handoff import (
    audit_run_knowledge,
    audit_stored_queries,
    experience_tools,
)
from .knowledge_run import (
    HostKnowledgeInput,
    normalize_run_knowledge_profile,
    prepare_run_knowledge,
    read_run_knowledge,
)
from .logs import MAX_LOG_BYTES, decode_text
from .model_calls import (
    ModelCallConflict,
    ModelCallStore,
    ModelCallUncertain,
    canonical,
)
from .model_provider import ChatCompletionsAdapter
from .models import (
    AccountedProviderModel,
    FixedResponseModel,
    ModelLimit,
    context_profile,
)
from .policies import (
    Counterexample,
    PolicyFeatures,
    rule_guidance,
)
from .provenance import (
    capture_execution_provenance,
    ensure_execution_compatible,
    validate_session_provenance,
)
from .reporting import _owned_file, build_completion
from .solver_visibility import SolverVisibilityStore
from .source_search import SourceSearch, search_limits
from .static_skill_handoff import (
    audit_run_static_skills,
    audit_stored_static_skills,
    static_skill_tools,
)
from .static_skills import (
    StaticSkillPolicy,
    StaticSkillService,
    normalize_static_skill_profile,
    prepare_run_static_skills,
    read_run_static_skills,
)
from .task import digest, verify_task
from .task_strategy import build_system_prompt, build_task_message
from .verification import (
    AnalysisOnlyExecutor,
    CheckLedger,
    OfflineExecutor,
    _atomic_json,
)
from .working_context_plan import WorkingContextPolicy, WorkingContextStore
from .workspace import CandidateWorkspace, TextEdit, _files


def _safe_file(root: Path, relative: str) -> Path:
    require_path(relative)
    path = local_evidence_path(root / relative)
    if (not path.is_file() or path.is_symlink() or
            not path.resolve(strict=True).is_relative_to(local_evidence_path(root).resolve(strict=True))):
        raise ValueError("source path is missing or linked")
    return path


def _current_candidates(workspace: CandidateWorkspace) -> list[str]:
    found: list[tuple[int, str]] = []
    for path in workspace.candidates.iterdir():
        if not path.is_dir() or path.is_symlink():
            raise ValueError("candidate directory has an unsafe entry")
        record = json.loads((path / "candidate.json").read_text(encoding="utf-8"))
        workspace.require_registered(path.name)
        found.append((record["ordinal"], path.name))
    if len({ordinal for ordinal, _ in found}) != len(found):
        raise ValueError("duplicate candidate ordinal")
    return [candidate_id for _, candidate_id in sorted(found)]


def _known_obligations(workspace: CandidateWorkspace) -> dict[str, RepairObligation]:
    return AnalysisStore(workspace).obligations()


def _tools(workspace: CandidateWorkspace, ledger: CheckLedger, max_candidates: int,
           task_directory: Path, crash_log: str, crash_sha256: str,
           policy_features: PolicyFeatures, host_capabilities: frozenset[str] | None = None,
           condition_store: ConditionStore | None = None, *,
           knowledge_store: ApplicabilityStore | None = None,
           applicability_policy: ApplicabilityPolicy | None = None,
           solver_visibility: SolverVisibilityStore | None = None,
           model_calls: ModelCallStore | None = None,
           actions: ToolActionStore | None = None,
           static_skill_service: StaticSkillService | None = None,
           require_candidate_diagnosis: bool = False) -> list[Any]:
    analysis = AnalysisStore(workspace)
    decisions = DecisionStore(workspace)
    conditions = condition_store or (ConditionStore(workspace) if (workspace.root / "condition-inputs.json").exists()
                                    else ConditionStore(workspace, SyntheticObservations([])))
    host_capabilities = OFFLINE_CAPABILITIES if host_capabilities is None else host_capabilities

    @tool
    def inspect_analysis(diagnosis_id: str | None = None, include_schema: bool = False, decision_id: str | None = None,
                         condition_result_id: str | None = None,
                         proposal_json: str | None = None) -> str:
        """Read registered analysis, scoped synthetic condition results, host fixtures and request schemas."""
        try:
            bundles = analysis.bundles()
            value: dict[str, Any] = {"task_id": workspace.task_id, "run_id": workspace.run_id,
                                     "source_snapshot_sha256": workspace.source_snapshot_sha256,
                                     "diagnosis_ids": sorted(bundles),
                                     "obligation_ids": sorted(analysis.obligations()),
                                     "decision_ids": sorted(decisions.records()),
                                     "feedback_ids": decisions.feedback_ids(),
                                     "condition_result_ids": sorted(conditions.records()),
                                     "synthetic_observations": conditions.registry.descriptions(),
                                     "obligation_tracking": policy_features.obligation_tracking}
            if diagnosis_id is not None:
                if diagnosis_id not in bundles:
                    raise ValueError("diagnosis is not registered")
                bundle = bundles[diagnosis_id]
                value["record"] = {"diagnosis": bundle["diagnosis"].model_dump(mode="json"),
                                   "evidence": [item.model_dump(mode="json") for item in bundle["evidence"]],
                                   "obligations": [item.model_dump(mode="json") for item in bundle["obligations"]]}
            if include_schema:
                value["submission_schema"] = DiagnosisSubmission.model_json_schema()
                value["decision_request_schema"] = DecisionRequest.model_json_schema()
                value["condition_request_schema"] = ConditionRequest.model_json_schema()
                value["admission_rules"] = ["All model hypotheses and obligations retain unknown status.",
                    "All model causal relations and obligations retain model_inference authority.",
                    "Source/log citations are read by the host; feedback must match a saved failed check.",
                    "Condition inputs are host-fixed synthetic fixtures; calculations cannot confirm root cause or application success."]
            if decision_id is not None:
                records = decisions.records()
                if decision_id not in records:
                    raise ValueError("decision is not registered")
                value["decision"] = records[decision_id].model_dump(mode="json")
            if condition_result_id is not None:
                records = conditions.records()
                if condition_result_id not in records:
                    raise ValueError("condition result is not registered")
                value["condition_result"] = records[condition_result_id].model_dump(mode="json")
            return json.dumps(value)
        except (OSError, ValueError, ValidationError, KeyError) as exc:
            return f"rejected: {exc}"

    @tool
    def read_log(start_line: int = 1, end_line: int = 100, proposal_json: str | None = None) -> str:
        """Read a bounded line range from the hash-checked input crash log."""
        try:
            if start_line < 1 or end_line < start_line or end_line - start_line > 199:
                raise ValueError("invalid or oversized line range")
            path = _safe_file(task_directory, crash_log)
            if path.stat().st_size > MAX_LOG_BYTES:
                raise ValueError("crash log exceeds input limit")
            raw = path.read_bytes()
            if digest(raw) != crash_sha256:
                raise ValueError("crash log changed after task verification")
            text, encoding, warnings = decode_text(raw)
            lines = text.splitlines()
            return json.dumps({"start_line": start_line, "lines": lines[start_line - 1:end_line],
                               "encoding": encoding, "warnings": warnings},
                              ensure_ascii=False)
        except (OSError, ValueError) as exc:
            return f"rejected: {exc}"

    @tool
    def list_source(proposal_json: str | None = None) -> str:
        """List source files in the task snapshot (not the original dataset)."""
        return json.dumps(sorted(_files(workspace.base)))

    @tool
    def read_source(path: str, start_line: int = 1, end_line: int = 100, draft: bool = False,
                    proposal_json: str | None = None, candidate_id: str | None = None) -> str:
        """Read baseline, current draft, or a registered candidate; draft and candidate_id are exclusive.

        Candidate reads return their immutable candidate/snapshot scope. They
        are not observations of the baseline or another candidate.
        """
        try:
            if start_line < 1 or end_line < start_line or end_line - start_line > 199:
                raise ValueError("invalid or oversized line range")
            if draft and candidate_id is not None:
                raise ValueError("choose either draft or candidate_id")
            source = (workspace.require_registered(candidate_id) if candidate_id is not None else
                      workspace.draft if draft else workspace.base)
            lines = _safe_file(source, path).read_text(encoding="utf-8").splitlines()
            answer: dict[str, Any] = {"path": path, "start_line": start_line,
                                      "lines": lines[start_line - 1:end_line]}
            if candidate_id is not None:
                record = json.loads(_safe_file(source.parent, "candidate.json").read_text(encoding="utf-8"))
                # Recheck after reading so the receipt cannot report a frozen
                # version whose registered content drifted during the read.
                workspace.require_registered(candidate_id)
                answer.update(scope="candidate", candidate_id=candidate_id,
                              snapshot_sha256=record["candidate_snapshot_sha256"])
            return json.dumps(answer, ensure_ascii=False)
        except (OSError, ValueError) as exc:
            return f"rejected: {exc}"

    @tool
    def search_source(query: str, path_prefix: str | None = None, candidate_id: str | None = None,
                      case_sensitive: bool = True, offset: int = 0, limit: int = 30,
                      proposal_json: str | None = None) -> str:
        """Search a single-line literal across baseline or a registered candidate; return versioned line hits.

        path_prefix is an exact relative file/directory prefix, not a glob. Pages contain one hit per line.
        Check complete/skipped and has_more/next_offset before concluding absence; drafts are not searched.
        """
        try:
            return json.dumps(SourceSearch(workspace).search(
                query, path_prefix=path_prefix, candidate_id=candidate_id,
                case_sensitive=case_sensitive, offset=offset, limit=limit), ensure_ascii=False)
        except (OSError, ValueError) as exc:
            return f"rejected: {exc}"

    @tool
    def record_diagnosis(path: str | None = None, line: int | None = None, reason: str | None = None,
                         diagnosis_json: str | None = None, proposal_json: str | None = None) -> str:
        """Record an unknown hypothesis or diagnosis_json; inspect_analysis(include_schema=True) provides its schema."""
        try:
            if diagnosis_json is not None:
                if any(value is not None for value in (path, line, reason)):
                    raise ValueError("choose either a simple location or a structured diagnosis")
                submission = DiagnosisSubmission.model_validate_json(diagnosis_json)
                value = analysis.admit(submission, task=task_directory, log_path=crash_log,
                                       log_sha256=crash_sha256, ledger=ledger,
                                       allow_obligations=policy_features.obligation_tracking)
                return json.dumps({"diagnosis_id": value["diagnosis"]["record_id"],
                                   "support_status": "unknown", "record": value})
            if path is None or line is None or reason is None:
                raise ValueError("simple diagnosis needs path, line and reason")
            lines = _safe_file(workspace.base, path).read_text(encoding="utf-8").splitlines()
            if line < 1 or line > len(lines) or not reason.strip():
                raise ValueError("diagnosis line or reason is invalid")
            number = len(list((workspace.root / "diagnoses").glob("diagnosis-*.json"))) + 1
            location = SourceLocation(snapshot_sha256=workspace.source_snapshot_sha256,
                                      path=path, start_line=line, end_line=line)
            now = datetime.now(UTC)
            identity = {"task_id": workspace.task_id, "run_id": workspace.run_id,
                        "created_at": now, "source_snapshot_sha256": workspace.source_snapshot_sha256}
            fact = admit_evidence({**identity, "record_id": f"source-{number}", "kind": "source_fact",
                                   "artifact_sha256": digest(lines[line - 1].encode()), "statement": lines[line - 1],
                                   "location": location}, channel="source_reader")
            inference = admit_evidence({**identity, "record_id": f"inference-{number}", "kind": "model_inference",
                                        "artifact_sha256": digest(reason.encode()), "statement": reason,
                                        "basis_ids": [fact.record_id], "location": location}, channel="model")
            diagnosis = Diagnosis(**identity, record_id=f"diagnosis-{number}",
                                  hypotheses=[Hypothesis(hypothesis_id=f"hypothesis-{number}",
                                                         root_cause_locations=[location],
                                                         support_evidence_ids=[inference.record_id],
                                                         support_status="unknown")],
                                  selected_hypothesis_id=f"hypothesis-{number}",
                                  unknowns=["model hypothesis has no independent root-cause confirmation"])
            analysis.put_bundle(diagnosis, [fact, inference], [])
            return json.dumps({"diagnosis_id": diagnosis.record_id, "support_status": "unknown"})
        except (OSError, ValueError, ValidationError) as exc:
            return f"rejected: {exc}"

    @tool
    def edit_source(path: str, old: str, new: str, proposal_json: str | None = None) -> str:
        """Apply one exact text replacement to the independent draft."""
        try:
            if not workspace.draft.exists():
                workspace.start_draft()
            workspace.apply_text_edits([TextEdit(path=path, old=old, new=new)])
            return "draft updated; register a candidate before any check"
        except (OSError, ValueError, ValidationError) as exc:
            return f"rejected: {exc}"

    @tool
    def record_obligation(obligation_id: str, statement: str, evidence_ids: list[str],
                          proposal_json: str | None = None) -> str:
        """Record a model-inferred repair obligation tied to registered evidence."""
        try:
            if not policy_features.obligation_tracking:
                raise ValueError("obligation tracking is disabled")
            if not IDENTIFIER.fullmatch(obligation_id):
                raise ValueError("invalid obligation identifier")
            claim = RepairObligation(task_id=workspace.task_id, run_id=workspace.run_id,
                                     record_id=obligation_id, created_at=datetime.now(UTC),
                                     source_snapshot_sha256=workspace.source_snapshot_sha256,
                                     kind="business_behavior", authority="model_inference",
                                     source_evidence_ids=evidence_ids, statement=statement)
            existing = _known_obligations(workspace).get(obligation_id)
            if existing is not None:
                if existing.model_dump(exclude={"created_at"}) != claim.model_dump(exclude={"created_at"}):
                    raise ValueError("obligation identity changed")
                return existing.model_dump_json()
            analysis.put_obligation(claim)
            return claim.model_dump_json()
        except (OSError, ValueError, ValidationError, KeyError) as exc:
            return f"rejected: {exc}"

    @tool
    def register_candidate(diagnosis_id: str = "", restores: list[str] | None = None,
                           preserves: list[str] | None = None, decision_id: str | None = None,
                           direction_id: str | None = None, proposal_json: str | None = None) -> str:
        """Freeze a complete draft diff and return its immutable candidate ID."""
        try:
            candidates = _current_candidates(workspace)
            if len(candidates) >= max_candidates:
                return "rejected: candidate budget exhausted"
            if require_candidate_diagnosis and not diagnosis_id:
                raise ValueError("candidate needs diagnosis_id from record_diagnosis; register again with that ID; the draft is unchanged")
            if diagnosis_id:
                if not IDENTIFIER.fullmatch(diagnosis_id):
                    raise ValueError("invalid diagnosis identifier")
                path = workspace.root / "diagnoses" / f"{diagnosis_id}.json"
                if not path.is_file() or path.is_symlink():
                    raise ValueError("diagnosis is not registered")
                if require_candidate_diagnosis:
                    diagnosis = analysis.bundles()[diagnosis_id]["diagnosis"]
                    selected = next((hypothesis for hypothesis in diagnosis.hypotheses
                                     if hypothesis.hypothesis_id == diagnosis.selected_hypothesis_id), None)
                    if selected is None or not selected.root_cause_locations:
                        raise ValueError("candidate diagnosis needs a selected hypothesis with original root-cause lines; "
                                         "record a complete diagnosis, then register this unchanged draft with its diagnosis_id")
            restores, preserves = restores or [], preserves or []
            if (decision_id is None) != (direction_id is None):
                raise ValueError("candidate decision and direction must be supplied together")
            if decision_id is not None and direction_id is not None:
                decisions.candidate_link(decision_id, direction_id, diagnosis_id, restores + preserves)
            if restores or preserves:
                if not policy_features.obligation_tracking:
                    raise ValueError("obligation tracking is disabled")
                known = _known_obligations(workspace)
                if any(item not in known for item in (*restores, *preserves)):
                    raise ValueError("candidate refers to an unknown obligation")
            candidate = workspace.register_candidate(len(candidates) + 1, diagnosis_id or None,
                                                     restores=restores, preserves=preserves,
                                                     decision_id=decision_id, direction_id=direction_id)
            return json.dumps({"candidate_id": candidate.candidate_id, "ordinal": candidate.ordinal})
        except (OSError, ValueError, ValidationError) as exc:
            return f"rejected: {exc}"

    @tool
    def run_check(candidate_id: str, proposal_json: str | None = None) -> str:
        """Run the host-fixed checks for a frozen candidate; inspect the returned evidence level."""
        try:
            result = ledger.run(candidate_id)
            return result.model_dump_json()
        except (OSError, ValueError, ValidationError) as exc:
            return f"rejected: {exc}"

    @tool
    def link_failed_check(candidate_id: str, violated_obligation_ids: list[str],
                          proposal_json: str | None = None) -> str:
        """Link an actual saved failed check to model-inferred obligations, preserving its scope."""
        try:
            if not policy_features.obligation_tracking:
                raise ValueError("obligation tracking is disabled")
            result = ledger.completed(candidate_id)
            if result is None or result.status != "fail":
                raise ValueError("no failed registered check exists")
            scope = result.execution_scope
            if scope is None:
                raise ValueError("an unexecuted check has no execution feedback")
            record = json.loads((workspace.candidates / candidate_id / "candidate.json").read_text(encoding="utf-8"))
            allowed = set(record["restores"] + record["preserves"])
            if (not violated_obligation_ids or len(set(violated_obligation_ids)) != len(violated_obligation_ids) or
                    any(item not in allowed for item in violated_obligation_ids)):
                raise ValueError("feedback obligation was not linked to this candidate")
            feedback = Counterexample(candidate_id=candidate_id,
                                      candidate_snapshot_sha256=record["candidate_snapshot_sha256"],
                                      execution_id=result.idempotency_key,
                                      evidence_kind=execution_evidence_kind(scope),
                                      scope=scope, link_authority="model_inference",
                                      violated_obligation_ids=violated_obligation_ids,
                                      observation=result.detail)
            folder = workspace.root / "feedback"
            folder.mkdir(exist_ok=True)
            if folder.is_symlink():
                raise ValueError("feedback directory is linked")
            key = feedback_key(feedback)
            path = folder / f"{key}.json"
            if path.exists():
                if path.is_symlink() or Counterexample.model_validate_json(path.read_text(encoding="utf-8")) != feedback:
                    raise ValueError("feedback identity changed")
            else:
                _atomic_json(path, feedback.model_dump())
            return json.dumps({**feedback.model_dump(), "feedback_id": key})
        except (OSError, ValueError, ValidationError, KeyError) as exc:
            return f"rejected: {exc}"

    @tool
    def consult_rule(family: str, proposal_json: str | None = None) -> str:
        """Read optional generic fault-family guidance; unknown families stay generic."""
        card = rule_guidance(family, policy_features)
        return card.model_dump_json() if card is not None else json.dumps({"family": family, "guidance": "generic"})

    @tool
    def plan_repair_action(plan_json: str, proposal_json: str | None = None) -> str:
        """Submit DecisionRequest bound to a diagnosis; inspect_analysis(include_schema=True) provides the schema."""
        try:
            request = DecisionRequest.model_validate_json(plan_json)
            return decisions.decide(request, ledger=ledger, features=policy_features,
                                    capabilities=host_capabilities).model_dump_json()
        except (OSError, ValueError, ValidationError, KeyError) as exc:
            return f"rejected: {exc}"

    @tool
    def evaluate_condition(request_json: str, observation_id: str | None = None,
                           proposal_json: str | None = None) -> str:
        """Calculate a registered finite condition using a host fixture ID; never accept model-supplied values."""
        try:
            request = ConditionRequest.model_validate_json(request_json)
            return conditions.evaluate(request, observation_id).model_dump_json()
        except (OSError, ValueError, ValidationError, KeyError) as exc:
            return f"rejected: {exc}"

    if require_candidate_diagnosis:
        register_candidate.description += (" This run requires diagnosis_id from record_diagnosis, with a selected "
            "hypothesis and original root-cause lines. Missing localization rejects before freezing and preserves the draft.")
    tools = [read_log, list_source, read_source, search_source, inspect_analysis, record_diagnosis, edit_source,
            register_candidate, run_check, consult_rule, plan_repair_action,
            record_obligation, link_failed_check, evaluate_condition]
    if knowledge_store is not None:
        tools.extend(experience_tools(knowledge_store))
    if applicability_policy is not None:
        if knowledge_store is None or solver_visibility is None:
            raise ValueError("applicability requires approved knowledge and frozen solver inputs")
        tools.extend(applicability_tools(knowledge_store, solver_visibility, model_calls, actions, applicability_policy))
    if static_skill_service is not None:
        if knowledge_store is None or applicability_policy is None or solver_visibility is None:
            raise ValueError("static skills require approved knowledge and explicit applicability configuration")
        tools.extend(static_skill_tools(static_skill_service))
    return tools


class ResumeAdmissionRejected(ValueError):
    """The saved work has not been admitted for any new execution."""


def _static_checkpoint_admission(workspace: CandidateWorkspace, service: StaticSkillService) -> None:
    """Inspect the native cache without SqliteSaver setup, DDL or WAL changes.

    This is scoped to the locked SQLite saver layout and selected Skill channels.
    The graph and solver/action ledger checks still own other checkpoint facts.
    """
    path = workspace.root / "checkpoints.sqlite"
    try:
        path.lstat()
    except FileNotFoundError:
        return
    _owned_file(workspace, path)
    limit = 8_388_608
    expected = {"skills_metadata": service.expected_metadata(), "skills_load_errors": []}

    def check(channel: str, value: Any) -> None:
        if canonical(value) != canonical(expected[channel]):
            raise ValueError("resume static skill graph cache differs")

    try:
        with closing(open_readonly_database(path)) as connection:
            # Instantiate only to use the SDK's serializer. get_tuple/setup
            # would create tables or change journal mode, so neither is called.
            saver = SqliteSaver(connection, serde=JsonPlusSerializer(allowed_msgpack_modules=None))
            row = connection.execute(
                "SELECT checkpoint_id,type,length(checkpoint) FROM checkpoints "
                "WHERE thread_id=? AND checkpoint_ns='' ORDER BY checkpoint_id DESC LIMIT 1",
                (workspace.run_id,)).fetchone()
            if row is None:
                return
            identifier, kind, size = row
            if kind not in ("msgpack", "json") or type(size) is not int or not 0 < size <= limit:
                raise ValueError("resume static skill checkpoint exceeds cache admission limit")
            raw = connection.execute("SELECT checkpoint FROM checkpoints WHERE thread_id=? "
                "AND checkpoint_ns='' AND checkpoint_id=?", (workspace.run_id, identifier)).fetchone()[0]
            checkpoint = saver.serde.loads_typed((kind, raw))
            values = checkpoint["channel_values"]
            for channel in expected:
                if channel in values:
                    check(channel, values[channel])
            rows = connection.execute("SELECT task_id,idx,channel,type,length(value) FROM writes "
                "WHERE thread_id=? AND checkpoint_ns='' AND checkpoint_id=? "
                "AND channel IN ('skills_metadata','skills_load_errors') ORDER BY task_id,idx",
                (workspace.run_id, identifier))
            total = size
            for number, (task_id, index, channel, value_kind, value_size) in enumerate(rows, start=1):
                if (value_kind not in ("msgpack", "json") or number > 128 or type(value_size) is not int or
                        value_size <= 0 or total + value_size > limit):
                    raise ValueError("resume static skill pending cache exceeds admission limit")
                total += value_size
                value = connection.execute("SELECT value FROM writes WHERE thread_id=? AND checkpoint_ns='' "
                    "AND checkpoint_id=? AND task_id=? AND idx=?",
                    (workspace.run_id, identifier, task_id, index)).fetchone()[0]
                check(channel, saver.serde.loads_typed((value_kind, value)))
    except (sqlite3.Error, KeyError, TypeError) as exc:
        raise ValueError("resume static skill checkpoint is invalid") from exc


def _resume_session(task: Path, work: Path, run_id: str,
                    knowledge_approval: TaskKnowledgeBinding | None = None,
                    version_skill_approval: Any = None) -> dict[str, Any]:
    absolute = work.absolute()
    for path in reversed((absolute, *absolute.parents)):
        info = path.lstat()
        if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode) or
                getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)):
            raise ValueError("resume work path is not an ordinary directory chain")
    workspace = CandidateWorkspace.reopen(task, work, run_id)
    path = _owned_file(workspace, workspace.root / "session.json")
    if path.stat().st_size > 16_777_216:
        raise ValueError("resume session exceeds the evidence format limit")
    session = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(session, dict) or session.get("run_id") != run_id:
        raise ValueError("resume session identity differs")
    if session.get("schema_version") not in (10, 11, 12, 13, 14):
        raise ValueError("historical sessions are read-only")
    if session["schema_version"] in (11, 12, 13, 14):
        profile = normalize_run_knowledge_profile(session["knowledge_profile"])
        if knowledge_approval is None or knowledge_approval.model_dump(mode="json") != profile["approval"]:
            raise ValueError("knowledge resume requires the original external host approval")
    elif "knowledge_profile" in session or knowledge_approval is not None:
        raise ValueError("a session without knowledge cannot acquire approval")
    if session["schema_version"] in (12, 13, 14):
        normalize_applicability_profile(session["applicability_profile"])
    elif "applicability_profile" in session:
        raise ValueError("historical session cannot acquire applicability configuration")
    if session["schema_version"] == 13:
        normalize_static_skill_profile(session["static_skill_profile"])
    elif "static_skill_profile" in session or "static_skill_facts" in session:
        raise ValueError("historical session cannot acquire static skill configuration")
    if session["schema_version"] == 14:
        from .version_skill_contracts import (
            TaskVersionSkillBinding,
            normalize_version_skill_profile,
        )

        profile = normalize_version_skill_profile(session["version_skill_profile"])
        if (type(version_skill_approval) is not TaskVersionSkillBinding or
                version_skill_approval.model_dump(mode="json") != profile["approval"]):
            raise ValueError("version resume requires the complete original external task approval")
    elif version_skill_approval is not None or any(key in session for key in ("version_skill_profile", "version_skill_facts")):
        raise ValueError("historical session cannot acquire version Skill configuration")
    saved = validate_session_provenance(session)
    if saved is None:
        raise ValueError("legacy execution provenance is unknown; session is read-only")
    ensure_execution_compatible(saved)
    return session


def preflight_resume(task: Path, work: Path, run_id: str, *,
                     knowledge_approval: TaskKnowledgeBinding | None = None,
                     version_skill_approval: Any = None) -> dict[str, Any]:
    """Reject incompatible saved execution facts before any writable run resource."""
    try:
        return _resume_session(task, work, run_id, knowledge_approval, version_skill_approval)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ResumeAdmissionRejected(
            "resume execution provenance or session identity differs; legacy sessions are read-only") from exc


def _run_agent(task: Path, work: Path, run_id: str, responses: list[AIMessage],
                    executor: OfflineExecutor, *, resume: bool = False, max_calls: int = 20,
                    max_known_tokens: int = 4000, max_seconds: float = 60,
                    max_candidates: int = 3, max_tool_calls: int = 60,
                    capabilities: frozenset[str] | None = None,
                    policy_features: PolicyFeatures | None = None,
                    observations: SyntheticObservations | None = None,
                    provider_adapter: ChatCompletionsAdapter | None = None,
                    compaction_policy: CompactionPolicy | None = None,
                    working_context_policy: WorkingContextPolicy | None = None,
                    knowledge: HostKnowledgeInput | None = None,
                    knowledge_approval: TaskKnowledgeBinding | None = None,
                    applicability_policy: ApplicabilityPolicy | None = None,
                    static_skill_policy: StaticSkillPolicy | None = None,
                    version_skills: Any = None,
                    version_skill_approval: Any = None,
                    version_skills_enabled: bool | None = None,
                    compaction_responses: list[AIMessage] | None = None,
                    require_candidate_diagnosis: bool = False,
                    startup_hook: Callable[[CandidateWorkspace], None] | None = None) -> dict[str, Any]:
    """Run one bounded offline turn; a pass is never application verification."""
    if max_calls < 1 or max_known_tokens < 1 or max_seconds <= 0 or max_candidates < 1 or max_tool_calls < 1:
        raise ValueError("budgets must be positive")
    if type(require_candidate_diagnosis) is not bool:
        raise TypeError("require_candidate_diagnosis must be a boolean")
    capabilities = OFFLINE_CAPABILITIES if capabilities is None else capabilities
    if not capabilities <= OFFLINE_CAPABILITIES:
        raise ValueError("unsupported offline capability")
    policy_features = policy_features or PolicyFeatures()
    compaction_policy = compaction_policy or CompactionPolicy()
    working_context_policy = working_context_policy or WorkingContextPolicy()
    applicability_profile = (None if applicability_policy is None else
        normalize_applicability_profile(applicability_policy.model_dump(mode="json")))
    if applicability_profile is not None:
        applicability_policy = ApplicabilityPolicy.model_validate_json(canonical(applicability_profile))
    if static_skill_policy is not None:
        static_skill_policy = StaticSkillPolicy.model_validate_json(static_skill_policy.model_dump_json())
        if applicability_policy is None:
            raise ValueError("static skills require explicit applicability configuration")
    if (resume and knowledge is not None) or (not resume and knowledge_approval is not None):
        raise ValueError("knowledge input is startup-only; resume accepts the original approval")
    if (resume and version_skills is not None) or (not resume and version_skill_approval is not None):
        raise ValueError("version source is startup-only; resume needs original external approval")
    if (version_skills is not None or version_skill_approval is not None) and (
            static_skill_policy is not None or applicability_profile is None):
        raise ValueError("version Skills need explicit applicability configuration and their own resource protocol")
    if version_skills is not None and version_skills_enabled is not None and version_skills.enabled != version_skills_enabled:
        raise ValueError("version Skill injection switch differs from explicit startup input")
    session = preflight_resume(task, work, run_id, knowledge_approval=knowledge_approval,
                              version_skill_approval=version_skill_approval) if resume else {}
    execution_provenance = session["execution_provenance"] if resume else capture_execution_provenance()
    try:
        task_info = verify_task(task)
        workspace = (CandidateWorkspace.reopen(task, work, run_id) if resume
                     else CandidateWorkspace(task, work, run_id))
        from .local_execution import LocalApplicationExecutor

        if startup_hook is not None:
            if resume:
                raise ValueError("host startup hook cannot modify resumed evidence")
            startup_hook(workspace)
        if isinstance(executor, LocalApplicationExecutor):
            executor.bind_workspace(workspace)
        ledger = CheckLedger(workspace, executor, read_only=resume)
        session_path = workspace.root / "session.json"
        if resume:
            conditions = ConditionStore(workspace)
            if observations is not None and observations.fingerprint() != conditions.observations_sha256:
                raise ResumeAdmissionRejected("resume condition inputs differ")
            started_at = datetime.fromisoformat(session["started_at"])
            if started_at.tzinfo is None:
                raise ResumeAdmissionRejected("session start time has no timezone")
        else:
            conditions = ConditionStore(workspace, observations or SyntheticObservations([]))
            started_at = datetime.now(UTC)
        knowledge_profile = (session.get("knowledge_profile") if resume else
            prepare_run_knowledge(workspace, task, knowledge, ledger=ledger))
        knowledge_store = read_run_knowledge(workspace, task, knowledge_profile, ledger=ledger)
        if applicability_profile is not None and knowledge_store is None:
            raise ValueError("applicability requires explicit approved knowledge")
        if resume and session.get("applicability_profile") != applicability_profile:
            raise ValueError("resume applicability configuration differs")
        static_owner = knowledge_store if knowledge_store is not None else workspace
        static_profile = (session.get("static_skill_profile") if resume else
            prepare_run_static_skills(static_owner, static_skill_policy,
                                      max_calls=max_calls, max_tool_calls=max_tool_calls))
        if resume:
            if (static_profile is None) != (static_skill_policy is None):
                raise ValueError("resume static skill configuration differs")
            if static_skill_policy is not None and static_profile is not None and (
                    static_profile["enabled"] != static_skill_policy.enabled or
                    static_profile["entry_ids"] != list(static_skill_policy.entry_ids)):
                raise ValueError("resume static skill selection differs")
        static_service = read_run_static_skills(static_owner, static_profile)
        from .version_skills import prepare_run_version_skills, read_run_version_skills

        version_profile = (session.get("version_skill_profile") if resume else
            prepare_run_version_skills(workspace, task, version_skills, ledger=ledger,
                                      max_calls=max_calls, max_tool_calls=max_tool_calls))
        if version_profile is not None:
            if knowledge_store is None or static_profile is not None:
                raise ValueError("version Skills need common approved knowledge and a distinct source protocol")
            if version_skills_enabled is not None and version_profile["enabled"] != version_skills_enabled:
                raise ValueError("version Skill injection switch differs from explicit run configuration")
            static_service = read_run_version_skills(workspace, task, version_profile, ledger=ledger)
        effective_skill_profile = version_profile if version_profile is not None else static_profile
        if resume and static_service is not None:
            _static_checkpoint_admission(workspace, static_service)
        if resume and knowledge_profile is not None:
            model_path, action_path = workspace.root / "model-calls.sqlite", workspace.root / "tool-actions.sqlite"
            if model_path.exists() and action_path.exists():
                audit_run_knowledge(workspace, task, ledger, knowledge_profile,
                    ModelCallStore(model_path, read_only=True), ToolActionStore(action_path, read_only=True))
            else:
                audit_stored_queries(workspace, task, ledger, knowledge_profile)
                if (model_path.exists() or action_path.exists() or
                        (workspace.root / "checkpoints.sqlite").exists() or
                        (workspace.root / "agent-report.json").exists()):
                    raise ValueError("knowledge restoration is missing an initialized ledger")
                for folder in (workspace.root / "experience-queries", workspace.root / "knowledge/retrievals"):
                    if folder.exists() and any(folder.iterdir()):
                        raise ValueError("knowledge restoration has records but missing accounted ledgers")
        model_profile = (provider_adapter.profile() if provider_adapter is not None else
                         {"transport": "fixed_response", "provider_id": "dfxfixed", "model_id": "offline"})
        working_context_store = WorkingContextStore(workspace, task, ledger, working_context_policy, model_profile)
        solver_visibility = (None if applicability_profile is None else
            SolverVisibilityStore(workspace, task, ledger, model_profile, working_context_store=working_context_store,
                                  static_skill_service=static_service))
        audit_stored_applicability(workspace, task, ledger, knowledge_profile, applicability_profile)
        if resume:
            audit_stored_static_skills(workspace, task, ledger, knowledge_profile, effective_skill_profile)
        if resume and applicability_profile is not None:
            model_path, action_path = workspace.root / "model-calls.sqlite", workspace.root / "tool-actions.sqlite"
            if model_path.exists() and action_path.exists():
                audit_run_applicability(workspace, task, ledger, knowledge_profile, applicability_profile,
                    ModelCallStore(model_path, read_only=True), ToolActionStore(action_path, read_only=True))
                audit_run_static_skills(workspace, task, ledger, knowledge_profile, effective_skill_profile,
                    ModelCallStore(model_path, read_only=True), ToolActionStore(action_path, read_only=True))
            elif (workspace.root / "solver-inputs").exists() and any((workspace.root / "solver-inputs").iterdir()):
                raise ValueError("applicability restoration lacks accounted ledgers")
        tools = _tools(workspace, ledger, max_candidates, task, task_info["crash_log"],
                       task_info["crash_sha256"], policy_features, capabilities, conditions,
                       knowledge_store=knowledge_store, applicability_policy=applicability_policy,
                       solver_visibility=solver_visibility, static_skill_service=static_service,
                       require_candidate_diagnosis=require_candidate_diagnosis)
        local_config = executor.config if isinstance(executor, LocalApplicationExecutor) else None
        application_checks = local_config is not None and local_config.mode == "application"
        source_checks = local_config is not None and local_config.mode == "source"
        analysis_only = isinstance(executor, AnalysisOnlyExecutor)
        validation_scope = local_config.validation_scope if local_config is not None else None
        mocked_interfaces = local_config.mocked_interfaces if local_config is not None else None
        system_prompt = build_system_prompt(policy_features, application_checks=application_checks,
                                           source_checks=source_checks, analysis_only=analysis_only,
                                           require_candidate_diagnosis=require_candidate_diagnosis)
        session_identity = {"schema_version": (14 if version_profile is not None else 13 if static_profile is not None else
                            12 if applicability_profile is not None else (10 if knowledge_profile is None else 11)),
                            "run_id": run_id, "started_at": started_at.isoformat(),
                            "execution_provenance": execution_provenance,
                            "max_calls": max_calls, "max_known_tokens": max_known_tokens,
                            "max_seconds": max_seconds, "max_candidates": max_candidates,
                            "max_tool_calls": max_tool_calls, "capabilities": sorted(capabilities),
                            "protocol_sha256": executor.protocol_sha256,
                            "observations_sha256": conditions.observations_sha256,
                            "policy_features": policy_features.model_dump(), "model_profile": model_profile,
                            "context_profile": context_profile(compaction_policy, working_context_policy),
                            "system_prompt_sha256": digest(system_prompt.encode("utf-8")),
                            "task_message_sha256": digest(build_task_message(
                                task_id=workspace.task_id, run_id=run_id,
                                source_snapshot_sha256=workspace.source_snapshot_sha256,
                                capabilities=capabilities, resume=False,
                                application_checks=application_checks,
                                source_checks=source_checks, analysis_only=analysis_only,
                                validation_scope=validation_scope, mocked_interfaces=mocked_interfaces,
                                require_candidate_diagnosis=require_candidate_diagnosis).encode("utf-8")),
                            "tool_schema_sha256": digest(canonical([
                                {"name": item.name, "description": item.description,
                                 "schema": item.args_schema.model_json_schema()} for item in tools
                            ]).encode("utf-8")), "search_limits": search_limits()}
        if require_candidate_diagnosis:
            session_identity["require_candidate_diagnosis"] = True
        if knowledge_profile is not None:
            session_identity["knowledge_profile"] = knowledge_profile
        if applicability_profile is not None:
            session_identity["applicability_profile"] = applicability_profile
        if static_profile is not None:
            session_identity["static_skill_profile"] = static_profile
        if version_profile is not None:
            session_identity["version_skill_profile"] = version_profile
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error, ModelCallConflict) as exc:
        if resume:
            raise ResumeAdmissionRejected("resume saved facts or configuration differ") from exc
        raise
    if resume:
        if session != session_identity:
            raise ResumeAdmissionRejected("resume budget or session identity differs")
        ledger.read_only = False
    else:
        _atomic_json(session_path, session_identity)
    remaining_seconds = (started_at + timedelta(seconds=max_seconds) - datetime.now(UTC)).total_seconds()
    if isinstance(executor, LocalApplicationExecutor):
        executor.set_deadline(time.monotonic() + max(0, remaining_seconds))
    model_calls = ModelCallStore(workspace.root / "model-calls.sqlite")
    actions = ToolActionStore(workspace.root / "tool-actions.sqlite")
    if applicability_policy is not None:
        tools = _tools(workspace, ledger, max_candidates, task, task_info["crash_log"],
            task_info["crash_sha256"], policy_features, capabilities, conditions,
            knowledge_store=knowledge_store, applicability_policy=applicability_policy,
            solver_visibility=solver_visibility, model_calls=model_calls, actions=actions,
            static_skill_service=static_service, require_candidate_diagnosis=require_candidate_diagnosis)
    model_type = AccountedProviderModel if provider_adapter is not None else FixedResponseModel
    provider_arguments: dict[str, Any] = {"adapter": provider_adapter} if provider_adapter is not None else {}
    model = model_type(responses=responses, max_calls=max_calls,
                               max_known_tokens=max_known_tokens,
                               deadline=time.monotonic() + max(0, remaining_seconds),
                               call_store=model_calls, task_id=workspace.task_id, run_id=run_id,
                               compactor=CompactionStore(workspace.root, workspace.task_id, run_id,
                                                         compaction_policy, model_profile),
                               compaction_responses=compaction_responses or [],
                               working_context_store=working_context_store,
                               action_store=actions,
                               solver_visibility_store=solver_visibility,
                               **provider_arguments)
    register_harness_profile(model._llm_type, HarnessProfile(
        excluded_tools=frozenset({"ls", "read_file", "write_file", "edit_file", "glob", "grep", "execute", "task",
                                 "write_todos", "delete"}),
        excluded_middleware=frozenset({SummarizationMiddleware}),
        general_purpose_subagent=GeneralPurposeSubagentProfile(enabled=False),
    ))
    config: RunnableConfig = {"configurable": {"thread_id": run_id}, "max_concurrency": 1,
                              "recursion_limit": max(30, max_calls * 3 + 5)}
    error: str | None = None
    status = "unresolved"
    with SqliteSaver.from_conn_string(str(workspace.root / "checkpoints.sqlite")) as saver:
        if static_service is not None:
            saver.serde = JsonPlusSerializer(allowed_msgpack_modules=None)
        filesystem_backend = FilesystemBackend(root_dir=workspace.base, virtual_mode=True)
        middleware: list[AgentMiddleware[Any, Any, Any]] = [
            FilesystemMiddleware(backend=filesystem_backend, tool_token_limit_before_evict=None,
                                 human_message_token_limit_before_evict=None),
            ToolAuthorizationMiddleware(
                workspace, model_calls, actions, capabilities=capabilities, max_calls=max_calls,
                max_candidates=max_candidates, max_tool_calls=max_tool_calls, deadline=model.deadline,
                applicability_enabled=applicability_policy is not None and applicability_policy.enabled),
        ]
        if static_service is not None:
            middleware.append(static_service.middleware())
        graph = create_deep_agent(
            model=model, tools=tools, system_prompt=system_prompt,
            backend=filesystem_backend,
            checkpointer=saver, subagents=[],
            middleware=middleware,
        )
        previous: list[AIMessage] = []
        if resume:
            checkpoint = graph.get_state(config)
            pending_graph = bool(checkpoint.next)
            previous = [message for message in checkpoint.values.get("messages", [])
                        if isinstance(message, AIMessage)]
            previous_count = len(previous)
        else:
            previous_count = 0
            pending_graph = False
        summary = model_calls.usage_summary(workspace.task_id, run_id)
        solver_records = model_calls.records(workspace.task_id, run_id, kind="solver")
        if [item.intent.call_id for item in solver_records] != [f"model-{index + 1}" for index in range(len(solver_records))]:
            raise ValueError("solver ledger sequence differs")
        if previous_count > len(solver_records):
            raise ValueError("model ledger is missing checkpointed responses")
        if resume:
            for index, checkpoint_message in enumerate(previous):
                record = solver_records[index]
                if (record.state != "completed" or
                        checkpoint_message.response_metadata.get("dfx_model_call_id") != record.intent.call_id):
                    raise ValueError("checkpoint model identity differs from solver ledger")
                saved, _ = model_calls.replay(record.intent)
                checkpoint_value, saved_value = checkpoint_message.model_dump(mode="json"), saved.model_dump(mode="json")
                if saved.id is None:
                    # LangChain adds a response ID after our durable provider boundary.
                    checkpoint_value["id"] = None
                if canonical(checkpoint_value) != canonical(saved_value):
                    raise ValueError("checkpoint response differs from solver receipt")
        model.turn_calls = previous_count
        model.calls = summary["reserved_calls"]
        model.known_tokens = summary["known_total_tokens"]
        model.missing_usage_calls = summary["missing_usage_calls"] + summary["invalid_usage_calls"]
        pending_model_call = len(solver_records) > previous_count
        prior_calls, prior_tokens, prior_missing = model.calls, model.known_tokens, model.missing_usage_calls
        try:
            if resume and (pending_model_call or pending_graph):
                graph.invoke(None, config)
            elif resume and previous_count:
                # Completed reconciliation needs no remaining budget. Pending work above
                # still uses the original ledger limits and deadline at execution boundaries.
                pass
            else:
                graph.invoke({"messages": [{"role": "user", "content": build_task_message(
                    task_id=workspace.task_id, run_id=run_id,
                    source_snapshot_sha256=workspace.source_snapshot_sha256,
                    capabilities=capabilities, resume=resume, application_checks=application_checks,
                    source_checks=source_checks, analysis_only=analysis_only,
                    validation_scope=validation_scope, mocked_interfaces=mocked_interfaces,
                    require_candidate_diagnosis=require_candidate_diagnosis)}]}, config)
        except ModelLimit as exc:
            error = str(exc)
            status = "budget_exhausted" if error in ("model_budget", "wall_time") else "unresolved"
        except ModelCallUncertain:
            error = "model_result_unknown"
            status = "unresolved"
        except ToolActionUncertain:
            error = "action_result_unknown"
            status = "unresolved"
    candidates = _current_candidates(workspace)
    checks = []
    for candidate_id in candidates:
        completed = ledger.completed(candidate_id)
        if completed is not None:
            checks.append(completed.model_dump())
    action_records = actions.records()
    uncertain_actions = sum(item["state"] != "completed" for item in action_records)
    if uncertain_actions:
        status, error = "unresolved", "action_result_unknown"
    elif any(item["rejection"] == "budget_exhausted" for item in action_records):
        status, error = "budget_exhausted", error or "action_budget"
    ensure_execution_compatible(execution_provenance)
    completion = build_completion(workspace, ledger, model_calls, actions, stopped_reason=error,
                                  task_directory=task)
    status = completion["suggested_status"]
    report: dict[str, Any] = {
        "schema_version": (11 if version_profile is not None else 10 if static_profile is not None else
                           9 if applicability_profile is not None else (7 if knowledge_profile is None else 8)),
        "task_id": workspace.task_id, "run_id": run_id,
        "execution_provenance": execution_provenance,
        "source_snapshot_sha256": workspace.source_snapshot_sha256,
        "status": status, "application_verification": "not_run",
        "candidates": candidates, "checks": checks,
        "model_calls": model.calls, "known_tokens": model.known_tokens,
        "missing_usage_calls": model.missing_usage_calls,
        "uncertain_model_calls": model_calls.usage_summary(workspace.task_id, run_id)["uncertain_calls"],
        "actions": action_records, "tool_calls": len(action_records),
        "decisions": [record.model_dump(mode="json") for record in DecisionStore(workspace).records().values()],
        "condition_results": [record.model_dump(mode="json") for record in conditions.records().values()],
        "rejected_tool_calls": sum(item["rejection"] is not None for item in action_records),
        "uncertain_tool_calls": uncertain_actions,
        "model_calls_this_turn": model.calls - prior_calls,
        "known_tokens_this_turn": model.known_tokens - prior_tokens,
        "missing_usage_calls_this_turn": model.missing_usage_calls - prior_missing,
        "policy_features": policy_features.model_dump(),
        "offered_tools": (completion["execution_facts"]["offered_tools"]
                          if completion["execution_facts"]["offered_tools"] is not None
                          else sorted(set(model.offered_tools))),
        "stopped_reason": error, "completion": completion,
        "working_context_plans": completion["working_context_plans"],
        "adapter_error_reason": getattr(model, "request_error_reason", None),
    }
    if knowledge_profile is not None:
        report["knowledge_profile"] = knowledge_profile
        report["knowledge_facts"] = completion["knowledge_facts"]
    if applicability_profile is not None:
        report["applicability_profile"] = applicability_profile
        report["applicability_facts"] = completion["applicability_facts"]
    if static_profile is not None:
        report["static_skill_profile"] = static_profile
        report["static_skill_facts"] = completion["static_skill_facts"]
    if version_profile is not None:
        report["version_skill_profile"] = version_profile
        report["version_skill_facts"] = completion["version_skill_facts"]
    _atomic_json(workspace.root / "agent-report.json", report)
    return report


def run_fixed_agent(task: Path, work: Path, run_id: str, responses: list[AIMessage],
                    executor: OfflineExecutor, **options: Any) -> dict[str, Any]:
    """Run the shared repair loop using deterministic offline messages."""
    return _run_agent(task, work, run_id, responses, executor, **options)


def run_provider_agent(task: Path, work: Path, run_id: str, adapter: ChatCompletionsAdapter,
                       executor: OfflineExecutor, **options: Any) -> dict[str, Any]:
    """Run the same loop through the mock-only wire boundary; never a cloud experiment."""
    return _run_agent(task, work, run_id, [], executor, provider_adapter=adapter, **options)
