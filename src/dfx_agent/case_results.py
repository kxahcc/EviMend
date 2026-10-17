"""Export one stopped competition result for browsing and a separate rerun.

The existing report, frozen workspace and private delivery remain the evidence
authorities. This operation copies them; it never executes a project or model.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

from .batch_budget import deepseek_flash_peak_cost
from .competition import verify_private_delivery
from .evidence_io import local_evidence_path
from .local_execution import _checked
from .model_calls import ModelCallConflict, ModelCallUncertain, canonical
from .task import EXCLUDED_FILES, EXCLUDED_PARTS, digest, include_file, snapshot_digest
from .workspace import CandidateWorkspace, _files, _rename_owned

_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")


def _read(path: Path, cap: int) -> bytes:
    actual = _checked(local_evidence_path(path))
    info = actual.stat()
    if not actual.is_file() or info.st_nlink != 1 or info.st_size > cap:
        raise ValueError("case result input is shared, missing or oversized")
    raw = actual.read_bytes()
    if len(raw) != info.st_size:
        raise ValueError("case result input changed during read")
    return raw


def _json(path: Path, cap: int) -> tuple[dict[str, Any], bytes]:
    raw = _read(path, cap)
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise TypeError("case result input must be an object")
    return value, raw


def _project_file(relative: str) -> bool:
    parts = [part.casefold() for part in Path(relative).parts]
    name = parts[-1]
    return not (any(part in EXCLUDED_PARTS or part in {".dfx-local", "archive", "runs", "faults", "fault_injection", "故障注入"} or
                        part.startswith("tc_") for part in parts) or
                name in EXCLUDED_FILES or name.startswith(("jscrash", ".env", "prompt")) or
                name.endswith((".local.json", ".md", ".p12", ".pfx", ".pem", ".key")))


class _Output:
    def __init__(self, stage: Path, cap: int):
        self.stage, self.cap, self.used = stage, cap, 0
        self.files: dict[str, dict[str, Any]] = {}

    def write(self, relative: str, raw: bytes) -> None:
        if self.used + len(raw) > self.cap:
            raise ValueError("case result exceeds the configured export byte limit")
        target = local_evidence_path(_checked(self.stage / relative, exists=False))
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("xb") as stream:
            stream.write(raw)
        self.used += len(raw)
        self.files[relative] = {"bytes": len(raw), "sha256": digest(raw)}

    def copy(self, relative: str, source: Path) -> None:
        raw = _read(source, self.cap - self.used)
        self.write(relative, raw)
        target = local_evidence_path(self.stage / relative)
        if os.path.samefile(local_evidence_path(source), target):
            raise ValueError("case result copy shares a writable inode")

    def tree(self, prefix: str, source: Path) -> None:
        for relative, path in sorted(_files(source).items()):
            if relative.casefold().endswith(".local.json"):
                raise ValueError("private configuration cannot enter a result source tree")
            self.copy(prefix + "/" + relative, path)


def _counter(value: Any) -> int | None:
    return value if type(value) is int and value >= 0 else None


def _phase_timing(report: dict[str, Any]) -> dict[str, Any]:
    def stamp(value: Any) -> datetime | None:
        if not isinstance(value, str):
            return None
        try:
            parsed = datetime.fromisoformat(value)
            return parsed if parsed.tzinfo is not None else None
        except ValueError:
            return None

    def elapsed(start: datetime | None, end: datetime | None) -> float | None:
        return round((end - start).total_seconds() * 1000, 3) if start and end and end >= start else None

    timing = report.get("timing")
    timing = timing if isinstance(timing, dict) else {}
    finish, wall = stamp(timing.get("finished_at")), timing.get("agent_wall_ms")
    start = (finish - timedelta(milliseconds=wall) if finish is not None and
             isinstance(wall, (int, float)) and not isinstance(wall, bool) and 0 <= wall <= 31_536_000_000 else None)
    localized = report.get("localization")
    first = localized.get("initial") if isinstance(localized, dict) else None
    diagnosis = first.get("diagnosis") if isinstance(first, dict) else None
    located = stamp(diagnosis.get("created_at")) if isinstance(diagnosis, dict) else None
    attempts = report.get("attempts", [])
    candidate = attempts[0].get("candidate") if attempts and isinstance(attempts[0], dict) else None
    patched = stamp(candidate.get("created_at")) if isinstance(candidate, dict) else None
    checks = [item.get("execution") for item in attempts if isinstance(item, dict) and item.get("execution")]
    durations = [elapsed(stamp(item.get("started_at")), stamp(item.get("finished_at")))
                 if isinstance(item, dict) else None for item in checks]
    verification = (round(sum(value for value in durations if value is not None), 3)
                    if durations and all(value is not None for value in durations) else None)
    return {"authority": "derived_from_saved_host_timestamps", "unit": "ms",
        "localization_ms": elapsed(start, located), "repair_ms": elapsed(located, patched),
        "time_to_first_candidate_ms": elapsed(start, patched), "verification_ms": verification,
        "definitions": {"localization_ms": "session start to first saved diagnosis; not proof of correct localization",
            "repair_ms": "first saved diagnosis to first frozen candidate; excludes later revisions",
            "verification_ms": "sum of saved execution start-to-finish intervals, including baseline and candidate checks"}}


def case_evaluation(work: Path, run_id: str, task_id: str, frozen: dict[str, Any],
                report: dict[str, Any], terminal: dict[str, Any]) -> dict[str, Any]:
    """Copy report facts and add a read-only, explicitly estimated fee view."""
    cost = report["cost"]
    usage = {key: _counter(cost.get(key)) for key in ("reserved_calls", "completed_calls", "known_input_tokens",
        "known_output_tokens", "known_total_tokens", "total_tokens", "uncertain_calls", "missing_usage_calls",
        "invalid_usage_calls")}
    usage["usage_complete"] = cost.get("usage_complete") is True and usage["total_tokens"] is not None
    usage["cache_hit_tokens"] = None
    usage["cache_miss_tokens"] = None
    fee: dict[str, Any] = {"status": "unavailable", "estimated_ceiling_cny": None,
        "authority": "provider_usage_and_published_peak_rates_estimate_not_account_bill",
        "reason": "missing_frozen_model_identity", "original_run_token_cap": None,
        "conservative_reservation_cny": None}
    config = frozen.get("config")
    model = config.get("model") if isinstance(config, dict) else None
    provider = config.get("provider") if isinstance(config, dict) else None
    if isinstance(config, dict) and isinstance(model, dict) and isinstance(provider, dict):
        model_id, endpoint = model.get("model_id"), provider.get("endpoint")
        if (config.get("model_mode") != "cloud" or model_id != "deepseek-flash" or endpoint not in {
                "https://api.deepseek.com/chat/completions", "https://api.deepseek.com/v1/chat/completions"}):
            fee["reason"] = "unsupported_model_or_endpoint"
        else:
            cap = _counter(config.get("max_known_tokens"))
            fee["original_run_token_cap"] = cap
            try:
                if usage["usage_complete"] is not True:
                    raise ValueError("original report retains unknown model usage")
                priced = deepseek_flash_peak_cost(work, task_id=task_id, run_id=run_id,
                    model_id=model_id, endpoint=endpoint)
                if (any(usage[key] is not None and usage[key] != priced[key]
                        for key in ("known_input_tokens", "known_output_tokens", "known_total_tokens")) or
                        usage["completed_calls"] is not None and usage["completed_calls"] != priced["logical_calls"]):
                    raise ValueError("model pricing ledger differs from the original report usage")
            except (OSError, ValueError, TypeError, KeyError, sqlite3.Error, ModelCallConflict, ModelCallUncertain):
                unknown_usage = (usage["usage_complete"] is not True or
                    any(usage[key] for key in ("uncertain_calls", "missing_usage_calls", "invalid_usage_calls")))
                fee.update(status="unknown" if unknown_usage else "unavailable",
                           reason="model_usage_unknown" if unknown_usage else "model_ledger_unavailable_or_invalid")
                if unknown_usage and cap is not None:
                    fee["conservative_reservation_cny"] = format(Decimal(cap) * Decimal(8) / Decimal(1_000_000), "f")
                    fee["reservation_authority"] = "frozen_token_cap_times_peak_8_not_measured_spend"
            else:
                fee.update(priced, status="estimated", reason=None)
                for key in ("known_input_tokens", "known_output_tokens", "known_total_tokens"):
                    usage[key] = priced[key]
                if priced["cache_usage_complete"]:
                    usage["cache_hit_tokens"] = priced["cache_hit_tokens"]
                    usage["cache_miss_tokens"] = priced["cache_miss_tokens"]
    return {"metrics": report["metrics"], "cost": cost, "timing": report.get("timing"),
        "phase_timing": _phase_timing(report),
        "manager_timing": {key: terminal.get(key) for key in ("started_at", "finished_at")},
        "usage": usage, "model_cost": fee}


def _evaluation_lines(evaluation: dict[str, Any]) -> list[str]:
    def display(value: Any) -> str:
        return "未知" if value is None else str(value)

    usage, fee = evaluation["usage"], evaluation["model_cost"]
    timing = evaluation["timing"] if isinstance(evaluation["timing"], dict) else {}
    phases = evaluation["phase_timing"]
    total = display(usage["total_tokens"]) if usage["usage_complete"] else "未知（已知下界 " + display(usage["known_total_tokens"]) + "）"
    lines = ["## 时间、Token 与模型费用", "",
        f"Agent 总耗时：{display(timing.get('agent_wall_ms'))} ms；首次定位记录：{display(phases['localization_ms'])} ms；首个补丁生成：{display(phases['repair_ms'])} ms；执行验证：{display(phases['verification_ms'])} ms。",
        f"模型调用：{display(usage['reserved_calls'])}；输入：{display(usage['known_input_tokens'])}；输出：{display(usage['known_output_tokens'])}；总 Token：{total}。",
        f"缓存命中 Token：{display(usage['cache_hit_tokens'])}；未命中：{display(usage['cache_miss_tokens'])}。"]
    if fee["status"] == "estimated":
        lines.append(f"模型费用上界估算：{fee['estimated_ceiling_cny']} 元（{fee['pricing_as_of']} 官方峰值单价，非账户实账）。")
        lines.append(f"单价/百万 Token：缓存命中 0.04 元、未命中 2 元、输出 8 元；缓存字段回退调用：{fee['cache_fallback_calls']}。")
    else:
        reasons = {"missing_frozen_model_identity": "缺少冻结的模型身份", "unsupported_model_or_endpoint": "该模型或地址尚无可用计费规则",
                   "model_usage_unknown": "存在未知或缺失用量", "model_ledger_unavailable_or_invalid": "原调用账本不可用或未通过核对"}
        lines.append("模型费用：未知（" + reasons[fee["reason"]] + "）；不补写为 0 元。")
        if fee["conservative_reservation_cny"] is not None:
            lines.append(f"原任务 Token 上限：{fee['original_run_token_cap']}；保守预算占用：{fee['conservative_reservation_cny']} 元，属于预留上限，非实际消费。")
    return lines + ["", "原始 cost、metrics、timing 与逐项计费依据保留在 summary.json，未知耗时与费用保持未知。", ""]


def export_case_result(root: Path, run_id: str, *, project: Path | None = None) -> dict[str, Any]:
    from .managed_runtime import require_managed_worker

    repo = _checked(root)
    require_managed_worker(repo, "case-export")
    if not _NAME.fullmatch(run_id):
        raise ValueError("invalid case result run ID")
    policy, _ = _json(repo / "configs/local-runtime.json", 1048576)
    cap = policy["limits"]["max_export_package_bytes"]
    if type(cap) is not int or cap < 1:
        raise ValueError("invalid case result export byte limit")
    work = _checked(repo / ".dfx-local/work" / ("competition-" + run_id))
    terminal, _ = _json(work / "run-state.json", cap)
    if (terminal.get("task") != "competition" or terminal.get("run_id") != run_id or
            terminal.get("status") not in {"finished", "failed", "canceled"} or not terminal.get("finished_at")):
        raise ValueError("case result requires a stopped competition run")
    launch, _ = _json(work / "product-launch.json", cap)
    task = _checked(Path(launch["task"]))
    if launch.get("run_id") != run_id or not task.is_relative_to(repo) or task.is_relative_to(work):
        raise ValueError("case result task belongs to another deployment or run")
    workspace = CandidateWorkspace.reopen(task, work, run_id)
    task_id = workspace.task_id
    if not _NAME.fullmatch(task_id):
        raise ValueError("invalid case result task ID")
    frozen, frozen_raw = _json(work / "competition-config.json", cap)
    task_record, task_raw = _json(task / "task.json", cap)
    report, report_raw = _json(work / "competition-report.json", cap)
    required = {"scope", "task_id", "run_id", "status", "accepted", "verification_level",
                "localization", "localization_result", "attempts", "cost", "metrics", "report_sha256"}
    if (not required <= report.keys() or report.get("schema_version") not in {3, 4} or
            report["scope"] != "competition_task" or report["run_id"] != run_id or report["task_id"] != task_id or
            report.get("source_snapshot_sha256") != workspace.source_snapshot_sha256 or
            frozen.get("task") != task_record or
            report["report_sha256"] != digest(canonical({k: v for k, v in report.items() if k != "report_sha256"}).encode())):
        raise ValueError("case result report identity or bytes differ")
    if not isinstance(report["attempts"], list) or not isinstance(report["cost"], dict) or not isinstance(report["metrics"], dict):
        raise TypeError("case result attempts, cost or metrics are invalid")
    evaluation = case_evaluation(work, run_id, task_id, frozen, report, terminal)
    selected = workspace.base
    candidate: dict[str, Any] | None = None
    patch: Path | None = None
    if report["attempts"]:
        latest = report["attempts"][-1]
        if not isinstance(latest, dict) or not isinstance(latest.get("candidate"), dict):
            raise ValueError("case result latest candidate is invalid")
        candidate = latest["candidate"]
        assert candidate is not None
        selected = workspace.require_registered(candidate["candidate_id"])
        saved, _ = _json(selected.parent / "candidate.json", cap)
        patch = selected.parent / "patch.diff"
        if saved != candidate or digest(_read(patch, cap)) != latest["patch_sha256"]:
            raise ValueError("case result latest candidate differs from its report")
    delivery = work / "delivery.zip"
    evidence = verify_private_delivery(delivery, cap)
    evidence_files = {item["path"]: item for item in evidence["files"]}
    for name, expected in (("run/competition-report.json", digest(report_raw)),
                           ("run/competition-config.json", digest(frozen_raw)),
                           ("task/task.json", digest(task_raw))):
        if evidence_files.get(name, {}).get("sha256") != expected:
            raise ValueError("case result delivery differs from its original report or task")
    explanation = _read(work / "result.md", cap)
    if evidence_files.get("run/result.md", {}).get("sha256") != digest(explanation):
        raise ValueError("case result explanation differs from the private delivery")
    project_root: Path | None = None
    project_files: dict[str, Path] = {}
    if project is not None:
        project_root = _checked(project if project.is_absolute() else repo / project)
        if (project_root == repo or not project_root.is_relative_to(repo) or
                any(project_root.is_relative_to(repo / part) for part in (".dfx-local", "runs", "configs", ".git"))):
            raise ValueError("case result project must be a separate original application inside the deployment")
        project_files = _files(project_root)
        original = [{"path": relative, "sha256": digest(_read(path, cap))}
                    for relative, path in sorted(project_files.items()) if include_file(Path(relative))]
        if snapshot_digest(original) != workspace.source_snapshot_sha256:
            raise ValueError("original project does not match the task baseline")
    export_root = _checked(repo / "runs/exports", exists=False)
    target = _checked(export_root / "cases" / task_id / run_id, exists=False)
    if local_evidence_path(target).exists():
        raise ValueError("case result already exists; do not overwrite a previous export")
    local_evidence_path(export_root).mkdir(parents=True, exist_ok=True)
    local_evidence_path(target.parent).mkdir(parents=True, exist_ok=True)
    stage = target.parent / (".case-result-" + uuid4().hex)
    local_evidence_path(stage).mkdir()
    try:
        output = _Output(stage, cap)
        output.tree("input-task", task)
        output.tree("repaired-source", selected)
        output.write("explanation.md", explanation)
        if patch is not None:
            output.copy("patch.diff", patch)
        if project_root is not None:
            source_files = _files(selected)
            baseline = set(_files(workspace.base))
            for relative, path in sorted(project_files.items()):
                if not _project_file(relative) or relative in baseline and relative not in source_files:
                    continue
                output.copy("repaired-project/" + relative, source_files.get(relative, path))
            for relative in sorted(source_files.keys() - project_files.keys()):
                if not _project_file(relative):
                    raise ValueError("candidate addition is excluded from the repaired project")
                output.copy("repaired-project/" + relative, source_files[relative])
        output.copy("evidence.zip", delivery)
        root_cause = {"authority": report.get("root_cause_authority", "model_inference"),
                      "localization": report["localization"], "localization_result": report["localization_result"]}
        verification = {key: report.get(key) for key in ("status", "accepted", "verification_level",
            "source_verification", "application_verification", "validation_scope", "mocked_interfaces",
            "acceptance_scope", "attempts", "stopped_reason", "adapter_error_reason", "cost", "metrics", "limitations")}
        for name, value in (("root-cause.json", root_cause), ("verification.json", verification)):
            output.write(name, (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode())
        lines = ["# 单例修复交付", "", f"任务：{task_id}；原运行：{run_id}",
            f"结果：{report['status']}；当前层级接受：{report['accepted']}",
            f"验证层级：{report['verification_level']}；完整应用：{report.get('application_verification', 'not_run')}",
            f"范围：{report.get('validation_scope', '仅分析，未进行可执行验证')}", "",
            "repaired-source 是冻结的完整筛选源码快照，不是完整应用工程。",
            "有候选时 patch.diff 是对应补丁；没有候选时源码仍是原版，不能称为已修复。",
            "先读 explanation.md 查看原运行的定位、依据和修复说明；结构化根因见 root-cause.json。",
            "候选、检查、失败与未知成本见 verification.json；原说明中的相对链接在 evidence.zip 的 run/ 内可回读。",
            "源码级通过仅覆盖声明的宿主协议，不等于 HarmonyOS 整应用、设备或构建通过。",
            "evidence.zip 原样保留已有私有证据包，含原源码/日志/模型消息及去 Key 的冻结参数；分享前须审查。",
            "本目录不复制 configs/*.local.json 原文件；模型凭据仍只留原部署本机。", ""]
        lines += _evaluation_lines(evaluation)
        lines += ["## 单独重测", "", "在同一完整部署项目中使用 input-task 作为输入，配置本机模型；",
            "需要源码验证时由已接纳运行器为该快照重新生成验证配置，再 configure。",
            "批次预算须在开始前登记新 ID 并留足费用；不要修改已冻结的旧批次或覆盖原 run。",
            "下面命令的配置路径与新 ID 须替换为新准备并获准的实际值：", "", "```text",
            f'dfx.cmd --target "runs/exports/cases/{task_id}/{run_id}/input-task" --config configs/NEW_RUN_CONFIG.json --name NEW_RUN_ID',
            "```", "", "它启动新的模型修复任务，可能收费；本次导出没有调用模型或执行项目。"]
        if project_root is not None:
            lines += ["", "repaired-project 保留原工程的资源与构建配置并覆盖候选增删；",
                      "已移除依赖缓存、构建产物、评测/指令资料和私有配置，不保证平台可构建或可运行。"]
        output.write("README.md", ("\n".join(lines) + "\n").encode())
        summary = {"task_id": task_id, "run_id": run_id, "status": report["status"],
            "accepted": report["accepted"], "verification_level": report["verification_level"],
            "source_snapshot_sha256": workspace.source_snapshot_sha256,
            "candidate_id": None if candidate is None else candidate["candidate_id"],
            "candidate_snapshot_sha256": None if candidate is None else candidate["candidate_snapshot_sha256"],
            "report_sha256": report["report_sha256"], "original_project_included": project_root is not None,
            **evaluation,
            "files": dict(output.files), "privacy": "private; review before sharing"}
        output.write("summary.json", (json.dumps(summary, ensure_ascii=False, indent=2) + "\n").encode())
        # Recheck only the authoritative small input identities before publish.
        if _read(work / "competition-report.json", cap) != report_raw or _read(work / "competition-config.json", cap) != frozen_raw:
            raise ValueError("case result source report changed during export")
        local_evidence_path(target.parent).mkdir(parents=True, exist_ok=True)
        _checked(target, exists=False)
        if local_evidence_path(target).exists():
            raise ValueError("case result destination appeared during export")
        _rename_owned(stage, target)
        return {"status": "exported", "task_id": task_id, "run_id": run_id, "path": str(target),
                "candidate_id": summary["candidate_id"], "verification_level": report["verification_level"],
                "accepted": report["accepted"], "bytes": output.used}
    finally:
        if local_evidence_path(stage).exists():
            _files(stage)
            shutil.rmtree(local_evidence_path(stage))
