"""Read-only, explicit-matrix aggregation of stopped synthetic agent evidence."""

from __future__ import annotations

import hashlib
import itertools
import json
import math
import os
import stat
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from langchain_core.utils.function_calling import convert_to_openai_tool
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .actions import OFFLINE_CAPABILITIES, TOOL_ACTIONS, ToolActionStore
from .agent import _tools
from .conditions import ConditionStore
from .context_compaction import CompactionPolicy
from .contracts import IDENTIFIER, require_hash
from .evidence_io import local_evidence_path
from .model_calls import ModelCallStore, canonical, sha256
from .model_provider import _tool_descriptors
from .models import context_profile
from .policies import PolicyFeatures
from .reporting import read_completion
from .source_search import search_limits
from .task import CaseSelection, digest, require_relative_reference, verify_task
from .task_strategy import build_system_prompt, build_task_message
from .verification import CheckLedger, ReplayOnlyExecutor
from .workspace import CandidateWorkspace

MAX_MANIFEST_BYTES = 1_048_576
MAX_JSON_BYTES = 16_777_216
MAX_CELLS = 4096
MAX_TREE_FILES = 100_000
_FORBIDDEN = frozenset({"data_b", "archive", ".git", ".agents", ".codex"})


def _identifier(value: str) -> None:
    if not IDENTIFIER.fullmatch(value):
        raise ValueError("invalid evaluation identity")


def _reference(value: str) -> None:
    require_relative_reference(value)
    if (":" in value or any(ord(char) < 32 for char in value) or
            any(part.casefold() in _FORBIDDEN or part.endswith((".", " ")) for part in value.split("/"))):
        raise ValueError("unsafe evaluation reference")


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class RunConfiguration(_Frozen):
    model_profile_sha256: str
    context_profile_sha256: str
    tool_schema_sha256: str
    search_limits_sha256: str
    capabilities: tuple[str, ...]
    max_calls: int = Field(gt=0)
    max_known_tokens: int = Field(gt=0)
    max_seconds: float = Field(gt=0)
    max_candidates: int = Field(gt=0)
    max_tool_calls: int = Field(gt=0)

    @model_validator(mode="after")
    def valid(self) -> RunConfiguration:
        for name in ("model_profile_sha256", "context_profile_sha256", "tool_schema_sha256", "search_limits_sha256"):
            require_hash(getattr(self, name))
        if (not math.isfinite(self.max_seconds) or tuple(sorted(set(self.capabilities))) != self.capabilities or
                not set(self.capabilities) <= OFFLINE_CAPABILITIES):
            raise ValueError("invalid evaluation budget or capabilities")
        return self


class EvaluationTask(_Frozen):
    task_id: str
    case_key: str
    lineage_group_id: str
    fault_variant_id: str
    task_path: str
    task_sha256: str
    source_snapshot_sha256: str
    crash_sha256: str
    protocol_sha256: str
    observations_sha256: str
    selection: CaseSelection | None = None

    @model_validator(mode="after")
    def valid(self) -> EvaluationTask:
        for value in (self.task_id, self.lineage_group_id, self.fault_variant_id):
            _identifier(value)
        _reference(self.case_key)
        _reference(self.task_path)
        for name in ("task_sha256", "source_snapshot_sha256", "crash_sha256", "protocol_sha256", "observations_sha256"):
            require_hash(getattr(self, name))
        if self.selection is not None and any(getattr(self.selection, name) != getattr(self, name) for name in
                                             ("task_id", "case_key", "lineage_group_id", "fault_variant_id")):
            raise ValueError("evaluation task differs from its case selection")
        return self


class EvaluationVariant(_Frozen):
    variant_id: str
    policy_features: PolicyFeatures

    @model_validator(mode="after")
    def valid(self) -> EvaluationVariant:
        _identifier(self.variant_id)
        return self


class EvaluationCell(_Frozen):
    task_id: str
    variant_id: str
    repeat_id: str
    run_id: str
    work_path: str | None

    @model_validator(mode="after")
    def valid(self) -> EvaluationCell:
        for value in (self.task_id, self.variant_id, self.repeat_id, self.run_id):
            _identifier(value)
        if self.work_path is not None:
            _reference(self.work_path)
        return self


def _overlap(left: Path, right: Path) -> bool:
    return left == right or left.is_relative_to(right) or right.is_relative_to(left)


class EvaluationManifest(_Frozen):
    schema_version: Literal[1] = 1
    evaluation_id: str
    scope: Literal["offline_synthetic"]
    configuration: RunConfiguration
    tasks: tuple[EvaluationTask, ...] = Field(min_length=1)
    variants: tuple[EvaluationVariant, ...] = Field(min_length=1)
    repeat_ids: tuple[str, ...] = Field(min_length=1)
    runs: tuple[EvaluationCell, ...] = Field(min_length=1, max_length=MAX_CELLS)

    @model_validator(mode="after")
    def valid(self) -> EvaluationManifest:
        _identifier(self.evaluation_id)
        if len(self.tasks) * len(self.variants) * len(self.repeat_ids) > MAX_CELLS:
            raise ValueError("evaluation matrix exceeds format cell limit")
        for value in self.repeat_ids:
            _identifier(value)
        for collection in ([task.task_id for task in self.tasks], [task.case_key for task in self.tasks],
                           [task.crash_sha256 for task in self.tasks], [task.task_path.casefold() for task in self.tasks],
                           [variant.variant_id for variant in self.variants], list(self.repeat_ids),
                           [cell.run_id for cell in self.runs]):
            if len(collection) != len(set(collection)):
                raise ValueError("duplicate evaluation identity, log bytes or input path")
        expected = set(itertools.product((task.task_id for task in self.tasks),
                                        (variant.variant_id for variant in self.variants), self.repeat_ids))
        actual = [(cell.task_id, cell.variant_id, cell.repeat_id) for cell in self.runs]
        if len(actual) != len(set(actual)) or set(actual) != expected:
            raise ValueError("evaluation must declare the exact task/variant/repeat matrix")
        paths = [Path(task.task_path.casefold()) for task in self.tasks]
        paths.extend(Path(cell.work_path.casefold()) for cell in self.runs if cell.work_path is not None)
        if any(_overlap(left, right) for left, right in itertools.combinations(paths, 2)):
            raise ValueError("evaluation task and work paths overlap")
        return self


def _guard(path: Path, *, missing: bool = False) -> None:
    """Inspect before resolve; no shared files, reparse points or special nodes."""
    if ".." in path.parts:
        raise ValueError("evidence path is not canonical")
    for component in (*reversed(path.parents), path):
        try:
            info = component.lstat()
        except FileNotFoundError:
            if missing:
                continue
            raise ValueError("evidence path is missing") from None
        if (stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) &
                getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)):
            raise ValueError("evidence path is linked")
        if stat.S_ISREG(info.st_mode):
            if component != path or info.st_nlink != 1:
                raise ValueError("evidence file is shared or used as a directory")
        elif not stat.S_ISDIR(info.st_mode):
            raise ValueError("evidence contains a nonregular node")


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    found: dict[str, Any] = {}
    for key, value in pairs:
        if key in found:
            raise ValueError("duplicate JSON key in evidence")
        found[key] = value
    return found


def _nonfinite(value: str) -> Any:
    raise ValueError("nonfinite JSON number in evidence: " + value)


def _finite_float(value: str) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("nonfinite JSON number in evidence")
    return result


def _read_bytes(path: Path, limit: int) -> bytes:
    _guard(path)
    if not path.is_file() or path.stat().st_size > limit:
        raise ValueError("JSON evidence is missing or exceeds format limit")
    with path.open("rb") as source:
        raw = source.read(limit + 1)
    if len(raw) > limit:
        raise ValueError("JSON evidence exceeds format limit")
    return raw


def _decode(raw: bytes) -> Any:
    try:
        return json.loads(raw.decode("utf-8-sig"), object_pairs_hook=_pairs,
                          parse_constant=_nonfinite, parse_float=_finite_float)
    except (UnicodeError, RecursionError, OverflowError) as exc:
        raise ValueError("invalid JSON evidence encoding or nesting") from exc


def _load(path: Path, limit: int = MAX_JSON_BYTES) -> Any:
    return _decode(_read_bytes(path, limit))


def load_evaluation_manifest(path: Path) -> EvaluationManifest:
    value = _load(path.absolute(), MAX_MANIFEST_BYTES)
    return EvaluationManifest.model_validate_json(canonical(value))


def _tree_snapshot(root: Path) -> dict[str, tuple[int, int, int, int, str]]:
    _guard(root)
    if not root.is_dir():
        raise ValueError("evidence tree must be a directory")
    found: dict[str, tuple[int, int, int, int, str]] = {}
    pending = [root]
    while pending:
        path = pending.pop()
        _guard(path)
        info = path.lstat()
        relative = path.relative_to(root).as_posix()
        if path.is_dir():
            found[relative] = (0, info.st_mtime_ns, info.st_ino, info.st_dev, "directory")
            pending.extend(path.iterdir())
        else:
            checksum = hashlib.sha256()
            with path.open("rb") as source:
                while chunk := source.read(1_048_576):
                    checksum.update(chunk)
            found[relative] = (info.st_size, info.st_mtime_ns, info.st_ino, info.st_dev, checksum.hexdigest())
            parts = path.relative_to(root).parts
            is_source = parts[0] in {"source", "base", "draft"} or (
                len(parts) > 2 and parts[0] == "candidates" and parts[2] == "source")
            if path.suffix.casefold() == ".json" and not is_source:
                _load(path)
        if len(found) > MAX_TREE_FILES:
            raise ValueError("evidence tree exceeds format entry limit")
    return found


def _input_paths(manifest: EvaluationManifest, root: Path) -> list[Path]:
    root = root.absolute()
    _guard(root)
    paths = [root / task.task_path for task in manifest.tasks]
    paths.extend(root / cell.work_path for cell in manifest.runs if cell.work_path is not None)
    for path in paths:
        _guard(path, missing=True)
    if any(_overlap(left.resolve(), right.resolve()) for left, right in itertools.combinations(paths, 2)):
        raise ValueError("resolved evaluation input paths overlap")
    return paths


def _session(task: EvaluationTask, cell: EvaluationCell, variant: EvaluationVariant,
             configuration: RunConfiguration, workspace: CandidateWorkspace) -> dict[str, Any]:
    value = _load(workspace.root / "session.json")
    if not isinstance(value, dict):
        raise ValueError("session must be a JSON object")  # noqa: TRY004 -- Evidence rejection uses ValueError.
    required = {"schema_version", "run_id", "started_at", "max_calls", "max_known_tokens", "max_seconds",
                "max_candidates", "max_tool_calls", "capabilities", "protocol_sha256", "observations_sha256",
                "policy_features", "model_profile", "context_profile", "system_prompt_sha256",
                "task_message_sha256", "tool_schema_sha256", "search_limits"}
    if set(value) != required or value["schema_version"] != 8 or value["run_id"] != cell.run_id:
        raise ValueError("session schema or identity differs")
    if not isinstance(value["started_at"], str) or datetime.fromisoformat(value["started_at"]).tzinfo is None:
        raise ValueError("session time must have a timezone")
    for name in ("max_calls", "max_known_tokens", "max_seconds", "max_candidates", "max_tool_calls", "capabilities"):
        # Integer and float seconds are equivalent in the actual agent API.
        equivalent_seconds = (name == "max_seconds" and type(value[name]) in (int, float) and
                              value[name] == configuration.max_seconds)
        if canonical(value[name]) != canonical(configuration.model_dump(mode="json")[name]) and not equivalent_seconds:
            raise ValueError("session budgets or capabilities differ from evaluation")
    for name in ("model_profile", "context_profile", "search_limits"):
        if sha256(canonical(value[name])) != getattr(configuration, name + "_sha256"):
            raise ValueError("session configuration hash differs from evaluation")
    expected = {"protocol_sha256": task.protocol_sha256, "observations_sha256": task.observations_sha256,
                "tool_schema_sha256": configuration.tool_schema_sha256,
                "policy_features": variant.policy_features.model_dump(mode="json"), "search_limits": search_limits(),
                "system_prompt_sha256": digest(build_system_prompt(variant.policy_features).encode()),
                "task_message_sha256": digest(build_task_message(task_id=task.task_id, run_id=cell.run_id,
                    source_snapshot_sha256=task.source_snapshot_sha256,
                    capabilities=frozenset(configuration.capabilities), resume=False).encode())}
    if any(canonical(value[name]) != canonical(expected_value) for name, expected_value in expected.items()):
        raise ValueError("session prompts, task protocol or method features differ")
    profile = value["context_profile"]
    if not isinstance(profile, dict) or "host_compaction" not in profile:
        raise ValueError("missing context profile")
    policy = CompactionPolicy.model_validate(profile["host_compaction"])
    if canonical(profile) != canonical(context_profile(policy)):
        raise ValueError("unsupported context profile")
    return value


def _model_identity(session: dict[str, Any], store: ModelCallStore, workspace: CandidateWorkspace,
                    ledger: CheckLedger, task_directory: Path, crash_sha256: str) -> list[str]:
    # Recreate the installed tool definitions without invoking any closure. An
    # explicit readback ConditionStore avoids _tools' registry-creation fallback.
    tools = _tools(workspace, ledger, session["max_candidates"], task_directory,
                   "crash.log", crash_sha256, PolicyFeatures.model_validate(session["policy_features"]),
                   frozenset(session["capabilities"]), ConditionStore(workspace))
    descriptors = [{"name": item.name, "description": item.description,
                    "schema": item.args_schema.model_json_schema()} for item in tools]
    if session["tool_schema_sha256"] != sha256(canonical(descriptors)):
        raise ValueError("session tool schema differs from installed tool definitions")
    # CompactionStore.audit, called by read_completion, also checks the entire profile.
    profile = session["model_profile"]
    if profile["transport"] == "fixed_response":
        wire_descriptors = [{"name": item.name, "description": item.description, "args": item.args} for item in tools]
    else:
        wire_descriptors = _tool_descriptors([convert_to_openai_tool(item) for item in tools])
    expected_schema = sha256(canonical(wire_descriptors))
    expected_names = tuple(item.name for item in tools)
    settings = profile.get("settings", profile)
    expected = (settings.get("provider_id"), settings.get("model_id"))
    schemas: set[str] = set()
    for record in store.records(workspace.task_id, workspace.run_id):
        intent = record.intent
        if (intent.provider_id, intent.model_id) != expected or intent.kind not in ("solver", "context_compaction"):
            raise ValueError("model ledger differs from supported session identity")
        if intent.kind == "solver":
            if intent.tool_names != expected_names or set(intent.tool_names) != set(TOOL_ACTIONS):
                raise ValueError("solver did not share the complete offline tool set")
            if intent.tool_schema_sha256 != expected_schema:
                raise ValueError("solver wire tool schema differs from installed tool definitions")
            schemas.add(intent.tool_schema_sha256)
        elif intent.tool_names:
            raise ValueError("summary calls must not have tools")
    if len(schemas) > 1:
        raise ValueError("solver tool schemas changed within a run")
    return sorted(schemas)


def _outcome(completion: dict[str, Any]) -> str:
    if completion["suggested_status"] == "offline_checked" and completion["latest_candidate_check_status"] == "pass":
        return "pass"
    uncertain = {"model_result_unknown", "action_result_unknown", "check_result_unknown", "model_request_not_dispatched",
                 "provider_format_error", "provider_request_unsupported", "context_limit", "context_format_unsupported",
                 "context_summary_invalid", "context_compaction_input_limit", "fixed_compaction_responses_exhausted"}
    if (completion["latest_candidate_check_status"] == "fail" and
            not uncertain.intersection(completion["unresolved_reasons"])):
        return "fail"
    return "unknown"


def _row(task: EvaluationTask, cell: EvaluationCell, variant: EvaluationVariant,
         configuration: RunConfiguration, root: Path) -> dict[str, Any]:
    value: dict[str, Any] = {**task.model_dump(mode="json", exclude={"selection"}),
                             **cell.model_dump(mode="json"), "record_status": "not_run", "status": "not_run",
                             "missing_reason": "explicit_not_run", "terminal_status": None, "cost": None,
                             "localization": {"initial": None, "latest": None, "candidate_linked": None},
                             "candidate_artifacts": [], "candidate_count": None, "first_candidate_id": None,
                             "first_candidate_check_status": "not_run", "latest_candidate_id": None,
                             "latest_candidate_check_status": "not_run", "context_compactions": [],
                             "facts_sha256": None, "summary_sha256": None, "solver_tool_schema_sha256": [],
                             "synthetic_outcome": "unknown", "timing": {"localization_ms": None, "repair_ms": None},
                             "verification": {"application_verification": "not_run", "accepted": False},
                             "identity_provenance": "manifest_declaration" if task.selection is None else "selection_hash_bound",
                             "application_admission": "not_verified",
                             "unresolved_reasons": []}
    if cell.work_path is None:
        return value
    work = root / cell.work_path
    value.update(record_status="missing", status="missing", missing_reason="work_missing")
    if not work.exists():
        return value
    state = _load(work / "run-state.json", MAX_MANIFEST_BYTES)
    if (not isinstance(state, dict) or type(state.get("schema_version")) is not int or state["schema_version"] != 1 or
            state.get("task") != "offline" or state.get("run_id") != cell.run_id or
            state.get("status") not in ("finished", "failed", "canceled")):
        raise ValueError("work item has no matching terminal offline state")
    value["terminal_status"] = state["status"]
    if not (work / "agent-report.json").exists():
        value["missing_reason"] = "completion_missing"
        return value
    workspace = CandidateWorkspace.reopen(root / task.task_path, work, cell.run_id)
    session = _session(task, cell, variant, configuration, workspace)
    calls = ModelCallStore(work / "model-calls.sqlite", read_only=True)
    actions = ToolActionStore(work / "tool-actions.sqlite", read_only=True)
    ledger = CheckLedger(workspace, ReplayOnlyExecutor(task.protocol_sha256), read_only=True)
    completion = read_completion(workspace, ledger, calls, actions)
    schemas = _model_identity(session, calls, workspace, ledger, root / task.task_path, task.crash_sha256)
    artifacts = completion["candidate_artifacts"]
    first = artifacts[0] if artifacts else None
    value.update(record_status="verified", status=completion["suggested_status"], missing_reason=None,
                 localization=completion["localization"], candidate_artifacts=artifacts, candidate_count=len(artifacts),
                 first_candidate_id=None if first is None else first["candidate"]["candidate_id"],
                 first_candidate_check_status="not_run" if first is None or first["check"] is None else first["check"]["status"],
                 solver_tool_schema_sha256=schemas, synthetic_outcome=_outcome(completion))
    for name in ("cost", "latest_candidate_id", "latest_candidate_check_status", "context_compactions",
                 "verification", "timing", "facts_sha256", "summary_sha256", "unresolved_reasons"):
        value[name] = completion[name]
    return value


def _variant_summary(variant: EvaluationVariant, rows: list[dict[str, Any]]) -> dict[str, Any]:
    costs = [row["cost"] for row in rows if row["cost"] is not None]
    unknown = sum(row["cost"] is None or not row["cost"]["usage_complete"] for row in rows)
    known_tokens = sum(cost["known_total_tokens"] for cost in costs)
    known_calls = sum(cost["reserved_calls"] for cost in costs)
    latest = sum(row["synthetic_outcome"] == "pass" for row in rows)
    first = sum(row["record_status"] == "verified" and row["first_candidate_check_status"] == "pass" for row in rows)
    return {"variant_id": variant.variant_id, "policy_features": variant.policy_features.model_dump(mode="json"),
            "task_count": len({row["task_id"] for row in rows}), "expected_runs": len(rows),
            "verified_runs": sum(row["record_status"] == "verified" for row in rows),
            "record_status_counts": dict(sorted(Counter(row["record_status"] for row in rows).items())),
            "status_counts": dict(sorted(Counter(row["status"] for row in rows).items())),
            "synthetic_latest_pass": {"passed": latest, "total": len(rows), "rate": latest / len(rows)},
            "synthetic_first_candidate_pass": {"passed": first, "total": len(rows), "rate": first / len(rows)},
            "cost": {"known_total_tokens_lower_bound": known_tokens,
                     "total_tokens": None if unknown else known_tokens, "unknown_cost_rows": unknown,
                     "known_reserved_calls_lower_bound": known_calls,
                     "reserved_calls": None if len(costs) != len(rows) else known_calls,
                     "calls_by_kind": {kind: sum(cost["calls_by_kind"][kind] for cost in costs) for kind in
                                       ("solver", "context_compaction", "format_repair", "sdk_retry")},
                     "calls_by_kind_complete": len(costs) == len(rows), "monetary_cost": None}}


def _paired(manifest: EvaluationManifest, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    indexed = {(row["task_id"], row["variant_id"], row["repeat_id"]): row for row in rows}
    found = []
    for base, compared in itertools.combinations(manifest.variants, 2):
        per_task = []
        for task in manifest.tasks:
            counts: Counter[str] = Counter()
            for repeat in manifest.repeat_ids:
                left = indexed[task.task_id, base.variant_id, repeat]["synthetic_outcome"]
                right = indexed[task.task_id, compared.variant_id, repeat]["synthetic_outcome"]
                key = ("unknown_pairs" if "unknown" in (left, right) else
                       "synthetic_gain_count" if (left, right) == ("fail", "pass") else
                       "synthetic_loss_count" if (left, right) == ("pass", "fail") else
                       "same_pass_count" if left == "pass" else "same_fail_count")
                counts[key] += 1
            per_task.append({"task_id": task.task_id, "lineage_group_id": task.lineage_group_id,
                             "expected_pairs": len(manifest.repeat_ids),
                             **{key: counts[key] for key in ("unknown_pairs", "synthetic_gain_count",
                                                            "synthetic_loss_count", "same_pass_count", "same_fail_count")}})
        count_keys = ("expected_pairs", "unknown_pairs", "synthetic_gain_count", "synthetic_loss_count",
                      "same_pass_count", "same_fail_count")
        total = {key: sum(item[key] for item in per_task) for key in count_keys}
        found.append({"base_variant_id": base.variant_id, "compared_variant_id": compared.variant_id,
                      **total, "eligible_pairs": total["expected_pairs"] - total["unknown_pairs"],
                      "task_count": len(manifest.tasks),
                      "lineage_group_count": len({task.lineage_group_id for task in manifest.tasks}), "by_task": per_task})
    return found


def aggregate_evaluation(manifest: EvaluationManifest, root: Path) -> dict[str, Any]:
    """Validate all declared inputs; missing cells remain unknown in the denominator."""
    manifest = EvaluationManifest.model_validate_json(canonical(manifest.model_dump(mode="json")))
    root = local_evidence_path(root)
    paths = _input_paths(manifest, root)
    snapshots = {path: _tree_snapshot(path) for path in paths if path.exists()}
    for task in manifest.tasks:
        path = root / task.task_path
        _guard(path)
        declared = _load(path / "task.json", MAX_MANIFEST_BYTES)
        if (not isinstance(declared, dict) or declared.get("source_root") != "source" or
                declared.get("source_manifest") != "source-manifest.json" or declared.get("crash_log") != "crash.log"):
            raise ValueError("evaluation task uses unsupported source references")
        actual = verify_task(path)
        if (digest((path / "task.json").read_bytes()) != task.task_sha256 or any(actual.get(name) != getattr(task, name)
                for name in ("task_id", "source_snapshot_sha256", "crash_sha256")) or
                actual.get("source_root") != "source" or actual.get("source_manifest") != "source-manifest.json" or
                actual.get("crash_log") != "crash.log"):
            raise ValueError("evaluation task differs from its frozen identity")
        if actual["schema_version"] == 2:
            if (task.selection is None or actual["case_identity_sha256"] != task.selection.sha256() or
                    task.selection.log.sha256 != task.crash_sha256):
                raise ValueError("schema-2 task must match its exact catalog selection")
        elif task.selection is not None:
            raise ValueError("schema-1 synthetic task cannot claim a catalog selection")
    tasks = {task.task_id: task for task in manifest.tasks}
    variants = {variant.variant_id: variant for variant in manifest.variants}
    rows = [_row(tasks[cell.task_id], cell, variants[cell.variant_id], manifest.configuration, root)
            for cell in sorted(manifest.runs, key=lambda cell: (cell.task_id, cell.variant_id, cell.repeat_id))]
    schemas = {schema for row in rows for schema in row["solver_tool_schema_sha256"]}
    if len(schemas) > 1:
        raise ValueError("solver wire tool schemas differ between evaluation cells")
    if any(path.exists() != (path in snapshots) or (path in snapshots and _tree_snapshot(path) != snapshots[path])
           for path in paths):
        raise ValueError("evaluation evidence changed during aggregation")
    value = {"schema_version": 1, "scope": "offline_synthetic", "evaluation_id": manifest.evaluation_id,
             "manifest_sha256": sha256(canonical(manifest.model_dump(mode="json"))),
             "configuration": manifest.configuration.model_dump(mode="json"),
             "task_count": len(manifest.tasks), "expected_runs": len(manifest.runs),
             "lineage_group_count": len({task.lineage_group_id for task in manifest.tasks}), "rows": rows,
             "variants": [_variant_summary(variant, [row for row in rows if row["variant_id"] == variant.variant_id])
                          for variant in manifest.variants], "paired_comparisons": _paired(manifest, rows),
             "execution_provenance": {"git_commit": None, "lock_sha256": None, "sdk_version": None,
                                      "status": "not_recorded_in_source_session"},
             "application_metrics": {name: None for name in ("root_cause_hit_at_1", "root_cause_hit_at_3",
                 "line_label_coverage", "repair_success_rate", "functional_correctness", "regression_rate",
                 "first_candidate_pass_rate", "monetary_cost", "localization_ms", "repair_ms")}}
    return {**value, "report_sha256": sha256(canonical(value))}


def write_evaluation_report(manifest_path: Path, root: Path, output: Path) -> dict[str, Any]:
    """Write a new report only, outside the source manifest/task/work trees."""
    manifest_path, root, output = manifest_path.absolute(), root.absolute(), output.absolute()
    _guard(manifest_path)
    _guard(output, missing=True)
    if any(part.casefold() in _FORBIDDEN for part in output.parts):
        raise ValueError("evaluation output uses a forbidden location")
    manifest_bytes = _read_bytes(manifest_path, MAX_MANIFEST_BYTES)
    manifest = EvaluationManifest.model_validate_json(canonical(_decode(manifest_bytes)))
    paths = _input_paths(manifest, root)
    if output.exists() or _overlap(output, manifest_path) or any(_overlap(output, path) for path in paths):
        raise ValueError("evaluation output exists or overlaps its inputs")
    value = aggregate_evaluation(manifest, root)
    if manifest_path.read_bytes() != manifest_bytes:
        raise ValueError("evaluation manifest changed during aggregation")
    raw = (canonical(value) + "\n").encode("utf-8")
    if len(raw) > MAX_JSON_BYTES:
        raise ValueError("evaluation report exceeds format limit")
    _guard(output, missing=True)
    output.parent.mkdir(parents=True, exist_ok=True)
    _guard(output.parent)
    created = False
    try:
        with output.open("xb") as target:
            created = True
            target.write(raw)
            target.flush()
            os.fsync(target.fileno())
    except BaseException:
        if created:
            output.unlink()
        raise
    return value
