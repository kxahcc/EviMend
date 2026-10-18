"""Read-only inspection of supplied material anomalies, separate from solver OOD runs."""

from __future__ import annotations

import hashlib
import json
import time
import zipfile
from datetime import UTC, datetime
from pathlib import Path

from dfx_agent.logs import MAX_LOG_BYTES, parse_crash


def inspect(root: Path) -> dict:
    started = time.perf_counter_ns()
    data = root / "data_B"
    b3 = data / "data_B3/benchmarkfordfx-better"
    files = sorted(p for p in data.rglob("*") if p.is_file())
    def relative(path: Path) -> str:
        return path.relative_to(root).as_posix()
    archives = []
    case013_members = []
    for path in files:
        if path.suffix.lower() != ".zip":
            continue
        archives.append(relative(path))
        with zipfile.ZipFile(path) as archive:
            case013_members.extend({"archive": relative(path), "member": m.filename}
                for m in archive.infolist() if "tc_jserror_013" in m.filename.casefold())
    school = data / "data_B1/SchoolLife1"
    school_logs = [relative(p) for p in files if p.is_relative_to(school) and
                   (p.name.casefold().startswith("jscrash") or p.suffix.casefold() == ".jscrash")]
    case013_files = [relative(p) for p in files if "tc_jserror_013" in p.name.casefold()]
    readme = b3 / "README.md"
    declarations = {name: [line for line in readme.read_text(encoding="utf-8-sig").splitlines()
                           if "| " + name + " |" in line] for name in ("TC_JSError_013", "TC_JSError_019")}
    path018 = b3 / "Logs/故障现场/TC_JSError_018-ImageProcessing1.zip"
    with zipfile.ZipFile(path018) as archive:
        members = [{"name": m.filename, "bytes": m.file_size} for m in archive.infolist() if not m.is_dir()]
        source_only = bool(members) and all(Path(m["name"]).suffix.casefold() == ".ets" for m in members)
        parser_observations = []
        if source_only:
            for member in members:
                if member["bytes"] > MAX_LOG_BYTES:
                    raise ValueError("Unexpected oversized source-only material")
                parsed = parse_crash(archive.read(member["name"]))
                parser_observations.append({"member": member["name"], "error_name": parsed.error_name,
                                            "stack_frame_count": len(parsed.frames), "warnings": parsed.warnings})
            source_only = not any(item["error_name"] or item["stack_frame_count"] for item in parser_observations)
    school_missing = school.is_dir() and not school_logs
    missing013 = (b3 / "App/Exam1").is_dir() and bool(declarations["TC_JSError_013"]) and not case013_files and not case013_members
    rows = [
        {"case": "SchoolLife1", "category": "missing_material", "source_present": school.is_dir(),
         "jscrash_files": school_logs, "issue": "有应用源码，没有对应jscrash故障输入", "expected_anomaly_confirmed": school_missing},
        {"case": "B3-013", "category": "missing_material", "source_present": (b3 / "App/Exam1").is_dir(),
         "source_alignment": "not_verified_for_013",
         "field_archives": [p for p in case013_files if p.lower().endswith(".zip")], "case_files": case013_files,
         "archive_member_matches": case013_members, "readme_declaration": declarations["TC_JSError_013"],
         "related_case": {"case": "B3-019", "readme_declaration": declarations["TC_JSError_019"],
                          "field_archive_present": (b3 / "Logs/故障现场/TC_JSError_019-Exam1.zip").is_file()},
         "issue": "013仅声明模块导出缺失，实际模块/导出名仍是占位符；缺对应日志、脚本与基线材料",
         "expected_anomaly_confirmed": missing013},
        {"case": "B3-018", "category": "wrong_material_type", "archive": relative(path018),
         "archive_sha256": hashlib.sha256(path018.read_bytes()).hexdigest(), "members": members,
         "parser_observations": parser_observations, "issue": "标为故障现场的ZIP仅含源码，不含崩溃日志",
         "expected_anomaly_confirmed": source_only},
    ]
    for row in rows:
        if not row["expected_anomaly_confirmed"]:
            row["category"] = "requires_review"
            row["issue"] = "原异常条件已变化，需重新核对实际输入；不能沿用旧缺失结论"
        row["system_handling"] = {
            "layer": "dataset_material_preflight_and_batch_selection",
            "decision": "excluded_as_material_ood" if row["expected_anomaly_confirmed"] else "materials_changed_review_required",
            "current_evaluation_closed": bool(row["expected_anomaly_confirmed"]),
            "admitted_to_current_repair_benchmark": False, "model_invoked": False, "patch_generated": False,
            "source_verification": "not_run", "repairability": "undetermined",
            "self_constructed_materials": False,
        }
        row["disposition"] = "本轮作为原始数据材料异常/OOD单列；不补造输入、不生成补丁，不计入正常修复或模型OOD通过率"
    return {"checked_at_utc": datetime.now(UTC).isoformat(),
            "scope": "Actual dataset material preflight and batch disposition; no model/solver run, fault injection or candidate execution",
            "model_calls": 0, "model_tokens": 0, "model_cost_cny": "0",
            "audit_elapsed_ms": (time.perf_counter_ns() - started) / 1_000_000,
            "scanned_archives": archives, "rows": rows}


def publish(report: dict, output: Path) -> None:
    output.mkdir(parents=True, exist_ok=True)
    (output / "material-gaps.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    lines = ["# 原始数据材料异常与系统处理", "", "证据层级：实际数据的输入材料预检与批次筛选；未调用模型、未运行候选源码。", "",
             "采集时间（UTC）：" + report["checked_at_utc"] + "。", "",
             "| 对象 | 识别依据 | 系统处理 |", "|---|---|---|"]
    for row in report["rows"]:
        decision = "材料异常/OOD，未进入修复" if row["expected_anomaly_confirmed"] else "材料已变化，需重新审核"
        lines.append(f"| {row['case']} | {row['issue']} | {decision} |")
    if next(row for row in report["rows"] if row["case"] == "B3-013")["expected_anomaly_confirmed"]:
        lines += ["", "B3-013在README中声明SyntaxError（模块未导出指定名称），但模块名、导出名、导入方仍是占位符。"
              f"检查本地文件及{len(report['scanned_archives'])}个ZIP成员后，未找到对应日志、脚本或基线材料。"
              "同一Exam1源码树的019则声明RangeError / Stack overflow，不能用019日志或修复结果代替013。", "",
              "因此013的状态是缺少可验证故障输入、修复能力未知，不是代码被证明无法修复。"
              "本轮将其与018、SchoolLife1一起作为原始数据材料异常/OOD记录完成，不安排补造日志或故障修复。"]
    lines += ["",
              "这三项不参与正常修复率或模型OOD通过率；它们单独证明本次材料筛选识别了缺失/错型输入并避免无依据修复。"
              "SchoolLife1是额外源码资源，不能把三条异常直接加到26例上作为独立故障总数。", "",
              "[原始检查记录](material-gaps.json) · [OOD总入口](README.md)", ""]
    (output / "material-gaps.md").write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    workspace = Path(__file__).resolve().parents[2]
    observation = inspect(workspace)
    publish(observation, workspace / "runs/exports/robustness")
    print(json.dumps({"materials": len(observation["rows"]), "model_calls": 0}))
