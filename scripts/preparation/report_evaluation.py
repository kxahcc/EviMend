"""Summarize completed OOD and ablation exports without model calls or old work."""

from __future__ import annotations

import collections
import csv
import hashlib
import io
import json
import sqlite3
import tempfile
import zipfile
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
EXPORTS = ROOT / "runs/exports"


def write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_csv(path: Path, rows: list[dict]) -> None:
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=sorted({key for row in rows for key in row}))
    writer.writeheader()
    writer.writerows(rows)
    path.write_text(stream.getvalue(), encoding="utf-8-sig", newline="")


def totals(rows: list[dict]) -> dict:
    return {"completed_runs": len(rows),
        "estimated_cny": str(sum((Decimal(row["estimated_cny"]) for row in rows), Decimal(0))),
        "model_calls": sum(row["model_calls"] for row in rows),
        "cache_hit_input_tokens": sum(row["cache_hit_input_tokens"] for row in rows),
        "cache_miss_input_tokens": sum(row["cache_miss_input_tokens"] for row in rows),
        "output_tokens": sum(row["output_tokens"] for row in rows),
        "total_tokens": sum(row["total_tokens"] for row in rows)}


def main() -> None:
    record = json.loads((EXPORTS / "evaluation-runs.json").read_bytes())
    rows = []
    comparisons = collections.defaultdict(list)
    for entry in record["results"]:
        folder = EXPORTS / entry["result"]
        assert folder.resolve().is_relative_to(EXPORTS.resolve())
        summary = json.loads((folder / "summary.json").read_bytes())
        assert summary["report_sha256"] == entry["report_sha256"]
        assert summary["usage"]["usage_complete"] and not entry["stopped_reason"]
        with zipfile.ZipFile(folder / "evidence.zip") as archive:
            agent = json.loads(archive.read("run/agent-report.json"))
            report = json.loads(archive.read("run/competition-report.json"))
            profile = json.loads(archive.read("run/competition-config.json"))
            ledger_bytes = archive.read("run/model-calls.sqlite") if entry["category"] == "ood" else None
        actions = collections.Counter(a["tool_name"] for a in agent["actions"] if a["state"] == "completed")
        row = {k: entry[k] for k in ("run_id", "task_id", "category", "variant", "status", "candidate_count", "result")}
        usage = summary["usage"]
        row.update(source_pass=summary["status"] == "source_verified" and summary["accepted"],
            model_calls=usage["completed_calls"], tool_calls=summary["metrics"]["agent_chain_length"],
            cache_hit_input_tokens=usage["cache_hit_tokens"], cache_miss_input_tokens=usage["cache_miss_tokens"],
            output_tokens=usage["known_output_tokens"], total_tokens=usage["known_total_tokens"],
            estimated_cny=summary["model_cost"]["estimated_ceiling_cny"], agent_wall_ms=summary["timing"]["agent_wall_ms"],
            cli_wall_ms=entry.get("cli_wall_ms"), first_candidate_pass=summary["metrics"]["first_attempt_pass"],
            skill_reads=actions["read_skill"], rule_queries=actions["consult_rule"], draft_edits=actions["edit_source"],
            obligations=len(report["obligations"]), decisions=len(report["decisions"]),
            context_plans=len(agent["working_context_plans"]), usage_complete=True)
        if entry["category"] == "ood":
            # Open only the terminal frozen main image; never an active ledger.
            assert ledger_bytes is not None
            with tempfile.TemporaryDirectory(prefix="evaluation-ledger-", dir=ROOT / ".dfx-local/tmp") as temporary:
                ledger = Path(temporary) / "model-calls.sqlite"
                ledger.write_bytes(ledger_bytes)
                db = sqlite3.connect(ledger.as_uri() + "?mode=ro&immutable=1", uri=True)
                try:
                    raw = db.execute("SELECT message_json FROM model_calls WHERE call_id=?",
                                     (f"model-{usage['completed_calls']}",)).fetchone()
                finally:
                    db.close()
            message = json.loads(raw[0]) if raw and raw[0] else {}
            data = message.get("data", message)
            content = data.get("content", data.get("text", ""))
            if not isinstance(content, str):
                content = json.dumps(content, ensure_ascii=False)
            row["no_fabricated_patch_or_root"] = bool(entry["expectation_met"] and not row["draft_edits"])
            row["autonomous_abstention_complete"] = row["no_fabricated_patch_or_root"] and bool(content.strip())
            if content.strip():
                (folder / "MODEL-RESPONSE.md").write_text(content + "\n", encoding="utf-8")
        elif entry["category"] == "ablation":
            same = json.loads(json.dumps(profile["config"]))
            same.pop("static_skills_enabled")
            same.pop("policy_features")
            cap = same.pop("max_known_tokens")
            # Each group's actual original cap remains explicit. A replacement
            # with a larger cap is descriptive, not an identical-cap rate trial.
            same.pop("charge_unknown_request_caps")
            comparisons[entry["task_id"]].append({"variant": entry["variant"], "token_cap": cap,
                "common_config": same, "source_snapshot_sha256": summary["source_snapshot_sha256"],
                "crash_sha256": profile["task"]["crash_sha256"],
                "corpus_sha256": agent["knowledge_profile"]["approval"]["snapshot_sha256"],
                "offered_tools_sha256": hashlib.sha256(json.dumps(agent["offered_tools"], sort_keys=True).encode()).hexdigest()})
        rows.append(row)
        outcome = ("证据不足，未改源码；正常结束" if row.get("autonomous_abstention_complete") else
                   "源码协议通过" if row["source_pass"] else "未满足预期，见原件")
        text = ["# 本次有效测试结果", "", f"任务：{row['task_id']}；组：{row['variant']}；运行：{row['run_id']}。", "",
            f"结果：{outcome}。", "",
            f"模型调用{row['model_calls']}，工具调用{row['tool_calls']}；缓存命中输入{row['cache_hit_input_tokens']}、未命中输入{row['cache_miss_input_tokens']}、输出{row['output_tokens']} Token。", "",
            f"费用估算{row['estimated_cny']}元；Agent耗时{row['agent_wall_ms']}ms；包含启动/导出的CLI耗时{row['cli_wall_ms']}ms。", "",
            "[原报告与证据](README.md) · [根因记录](root-cause.json) · [检查记录](verification.json)", "",
            "完整HarmonyOS应用未执行。", ""]
        (folder / "RESULT.md").write_text("\n".join(text), encoding="utf-8")

    checks = []
    for task, values in comparisons.items():
        complete = len(values) == 3 and {item["variant"] for item in values} == {"general", "guided", "skill"}
        common = [{k: v for k, v in item.items() if k not in {"variant", "token_cap"}} for item in values]
        equal = all(item == common[0] for item in common[1:])
        if not equal:
            raise ValueError(f"Actual comparison identity mismatch: {task}")
        checks.append({"task_id": task, "complete_three_variants": complete,
            "same_model_tools_corpus_inputs_and_other_limits": equal,
            "same_token_cap": len({item["token_cap"] for item in values}) == 1,
            "original_token_caps": {item["variant"]: item["token_cap"] for item in values},
            "scope": "Small completed-process pilot; no statistical significance or generalization claim"})

    generated = datetime.now(UTC).isoformat()
    negative = [row for row in rows if row["category"] == "ood"]
    positive = [row for row in rows if row["category"] == "ood_positive"]
    ood_rows = negative + positive
    ablations = [row for row in rows if row["category"] == "ablation"]
    ablations.sort(key=lambda row: (row["task_id"], {"general": 0, "guided": 1, "skill": 2}[row["variant"]]))
    ood_stats = {**totals(ood_rows), "negative_scenes": len(negative),
        "complete_correct_abstentions": sum(row["autonomous_abstention_complete"] for row in negative),
        "positive_controls": len(positive), "positive_source_passes": sum(row["source_pass"] for row in positive)}
    ablation_stats = {**totals(ablations), "source_passes": sum(row["source_pass"] for row in ablations)}
    write_json(EXPORTS / "robustness/model-index.json", {"generated_at_utc": generated, "totals": ood_stats, "results": ood_rows})
    write_csv(EXPORTS / "robustness/model-results.csv", ood_rows)
    write_json(EXPORTS / "ablations/index.json", {"generated_at_utc": generated, "totals": ablation_stats,
        "actual_comparison_checks": checks, "results": ablations})
    write_csv(EXPORTS / "ablations/results.csv", ablations)
    write_json(EXPORTS / "evaluation-summary.json", {"generated_at_utc": generated, "ood": ood_stats, "ablations": ablation_stats})

    for folder_name, title, selected in [("robustness", "鲁棒性与OOD", ood_rows), ("ablations", "必要消融", ablations)]:
        folder = EXPORTS / folder_name
        lines = ["# " + title, "", "按最后一次有效完成的测试汇总，原件与统计分别保留。", ""]
        if folder_name == "robustness":
            lines += ["[解析矩阵](parser-matrix.json) · [源码证据矩阵](evidence-matrix.json) · [材料异常](material-gaps.json)", "",
                "[可选完整应用环境评估](application-feasibility.json)", "",
                "软件测试覆盖23种解析输入和17种源码证据场景；这些是软件处理证据，与下面的真实模型OOD分别统计。", "",
                f"模型证据不足场景：{ood_stats['complete_correct_abstentions']}/{len(negative)}符合预期。",
                f"真实故障日志变形对照：{ood_stats['positive_source_passes']}/{len(positive)}源码验证通过。" if positive else "真实故障日志变形对照待完成。", "",
                "日志变形保持原工程源码与故障不变，检验表示变化下的修复稳定性；这些不是新增独立故障。证据不足的7个场景主要采用受控小程序，不作广泛跨应用拒修泛化声明。", "",
                "[模型统计](model-index.json) · [模型CSV](model-results.csv)", ""]
            if (folder / "application-demo/README.md").is_file():
                lines += ["[办公考勤单应用演示准备](application-demo/README.md)：准备状态与实际运行证据分别标注。", ""]
        else:
            lines += ["[逐次统计与共同条件](index.json) · [CSV](results.csv)", "",
                "只比较已完成的过程结果；实际Token上限逐组保留。少量案例不构成显著性或泛化证明，未启用working_context，不报告上下文剪枝或自动学习收益。", ""]
        lines += ["| 任务 | 组 | 结果 | 调用 | 命中输入 | 未命中输入 | 输出 | 估算元 | 产物 |",
                  "|---|---|---|---:|---:|---:|---:|---:|---|"]
        for row in selected:
            result = "依据不足，未修改" if row.get("autonomous_abstention_complete") else "源码通过" if row["source_pass"] else "未通过"
            relative = Path(row["result"]).relative_to(folder_name).as_posix()
            lines.append(f"| {row['task_id']} | {row['variant']} | {result} | {row['model_calls']} | {row['cache_hit_input_tokens']} | {row['cache_miss_input_tokens']} | {row['output_tokens']} | {row['estimated_cny']} | [打开]({relative}/RESULT.md) |")
        lines += ["", "费用按命中、未命中输入和输出的冻结费率估算；不是账户账单。完整HarmonyOS应用未执行。", ""]
        (folder / "README.md").write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps({"ood": ood_stats, "ablations": ablation_stats}, ensure_ascii=False))


if __name__ == "__main__":
    main()
