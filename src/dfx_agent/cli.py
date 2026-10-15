"""Local entry points for the first DFX implementation milestone."""

import argparse
import json
import os
import sqlite3
import zipfile
from pathlib import Path

from pydantic import ValidationError

from .analysis import analyze_task
from .runtime import doctor
from .task import CaseCatalog, prepare_task

_EXPORT_SAFE_REASONS = {
    "another active task blocks evidence export": "activity lease mismatch",
    "export owner differs from this prepared project": "project owner mismatch",
    "managed export directories are not prepared": "export directories missing",
    "export requires the current managed runtime policy": "runtime policy mismatch",
    "resource accounting found an external or export link": "storage contains an unsupported link",
    "export would exceed shared storage budget or free disk reserve": "storage budget or reserve exhausted",
    "evidence tree has an unregistered top-level entry": "unregistered evidence entry",
}


def _competition_receipt(result: dict) -> dict:
    """Keep the console receipt separate from full private Agent evidence."""
    fields = ("schema_version", "status", "accepted", "application_verification", "report_path",
              "candidate_count", "model_calls")
    receipt = {key: result[key] for key in fields}
    if "source_verification" in result:
        if (result["source_verification"] not in {"pass", "fail", "inconclusive", "not_run"} or
                result.get("verification_level") not in {"not_run", "source_execution", "application_execution"}):
            raise ValueError("invalid source verification console receipt")
        receipt["source_verification"] = result["source_verification"]
        receipt["verification_level"] = result["verification_level"]
    if (type(receipt["schema_version"]) is not int or receipt["schema_version"] < 1 or
            type(receipt["accepted"]) is not bool or
            any(type(receipt[key]) is not int or receipt[key] < 0 for key in ("candidate_count", "model_calls")) or
            any(type(receipt[key]) is not str or not receipt[key] or len(receipt[key]) > 128 or
                any(ord(character) < 32 for character in receipt[key])
                for key in ("status", "application_verification")) or
            type(receipt["report_path"]) is not str or not receipt["report_path"] or
            len(receipt["report_path"]) > 2048 or
            any(ord(character) < 32 for character in receipt["report_path"])):
        raise ValueError("invalid competition console receipt")
    if len(json.dumps(receipt, ensure_ascii=True, indent=2)) > 4096:
        raise ValueError("competition console receipt exceeds bound")
    return receipt


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    ready = sub.add_parser("doctor")
    ready.add_argument("--output", type=Path)
    prepare = sub.add_parser("prepare")
    prepare.add_argument("--catalog", required=True, type=Path, help="Explicitly reviewed task catalog; no implicit case selection")
    prepare.add_argument("--data-root", type=Path, default=Path("data_B"))
    prepare.add_argument("--case", required=True)
    prepare.add_argument("--output", required=True, type=Path)
    analyze = sub.add_parser("analyze")
    analyze.add_argument("--task", required=True, type=Path)
    analyze.add_argument("--output", required=True, type=Path)
    analyze.add_argument("--max-anchors", type=int, default=8)
    aggregate = sub.add_parser("aggregate", help="Managed, read-only aggregation of saved synthetic evidence")
    aggregate.add_argument("--manifest", required=True, type=Path)
    aggregate.add_argument("--root", required=True, type=Path)
    aggregate.add_argument("--output", required=True, type=Path)
    export = sub.add_parser("export", help="Managed preview or application of a private local evidence export")
    export.add_argument("--manifest", required=True, type=Path)
    export.add_argument("--root", required=True, type=Path)
    export.add_argument("--export-id", required=True)
    export.add_argument("--owner-id", required=True)
    export.add_argument("--plan", required=True, type=Path)
    export.add_argument("--apply", action="store_true")
    export.add_argument("--plan-id")
    verify_export = sub.add_parser("verify-export", help="Managed read-only verification of a saved evidence package")
    verify_export.add_argument("--package", required=True, type=Path)
    verify_export.add_argument("--owner-id", required=True)
    for name in ("repair", "resume"):
        offline = sub.add_parser(name, help="Managed fixed-response or mock-wire demonstration only")
        offline.add_argument("--task", required=True, type=Path)
        offline.add_argument("--work", required=True, type=Path)
        offline.add_argument("--run-id", required=True)
        offline.add_argument("--fixed-script", "--script", dest="fixed_script", required=True, type=Path)
        offline.add_argument("--project-root", type=Path, help="Explicit managed project authority for knowledge scripts")
    competition = sub.add_parser("competition", help="Managed competition task with explicit frozen run configuration")
    competition.add_argument("--task", required=True, type=Path)
    competition.add_argument("--work", required=True, type=Path)
    competition.add_argument("--run-id", required=True)
    competition.add_argument("--config", required=True, type=Path)
    competition.add_argument("--project-root", required=True, type=Path)
    competition.add_argument("--resume", action="store_true")
    args = None
    resume_rejection_type = None
    try:
        args = parser.parse_args()
        if args.command == "doctor":
            result = doctor()
        elif args.command == "prepare":
            catalog = CaseCatalog.model_validate_json(args.catalog.read_text(encoding="utf-8"))
            result = prepare_task(args.data_root, catalog.select(args.case).model_dump(exclude_none=True), args.output)
        elif args.command == "analyze":
            if args.output.resolve().is_relative_to(args.task.resolve()):
                raise ValueError("Analysis output must be outside the fixed task input")
            result = analyze_task(args.task, args.max_anchors)
        elif args.command == "aggregate":
            if os.environ.get("DFX_MANAGED_METRICS") != "offline-v1":
                raise ValueError("aggregate requires the managed offline metrics entry point")
            from .metrics import write_evaluation_report

            report = write_evaluation_report(args.manifest, args.root, args.output)
            result = {"schema_version": report["schema_version"], "scope": report["scope"],
                      "evaluation_id": report["evaluation_id"], "output": str(args.output),
                      "row_count": len(report["rows"])}
        elif args.command in ("export", "verify-export"):
            if os.environ.get("DFX_MANAGED_EXPORT") != "offline-v1":
                parser.error("export/verify-export require the managed offline export entry point")
            from .exporting import apply_export, preview_export, save_export_plan
            from .exporting import verify_export as verify_package

            if args.command == "verify-export":
                receipt = verify_package(args.package)
                if receipt.get("owner_id") != args.owner_id:
                    parser.error("Export package owner does not match the expected package origin")
            elif args.apply:
                if not args.plan_id:
                    parser.error("Applying an export requires the reviewed plan ID")
                receipt = apply_export(args.manifest, args.root, args.export_id, args.owner_id, args.plan, args.plan_id)
            else:
                if args.plan_id:
                    parser.error("Plan ID requires --apply")
                receipt = preview_export(args.manifest, args.root, args.export_id, args.owner_id)
                save_export_plan(receipt, args.plan)
            result = {key: receipt[key] for key in ("schema_version", "scope", "status", "export_id", "owner_id", "plan_id",
                                                   "estimated_bytes", "privacy", "output", "package_path", "package_sha256",
                                                   "aggregate_sha256") if key in receipt}
            if args.command == "export" and not args.apply:
                result["status"] = "preview"
                result["output"] = str(args.root / "runs" / "exports" / args.export_id)
            elif "package_path" in result:
                result["output"] = result["package_path"]
            if len(json.dumps(result, ensure_ascii=True, indent=2)) > 4096:
                parser.error("Export receipt exceeds the bounded console response")
        elif args.command == "competition":
            if os.environ.get("DFX_MANAGED_COMPETITION") != "local-v1":
                raise ValueError("competition requires the managed local entry point")
            from .agent import ResumeAdmissionRejected
            from .competition import run_competition

            resume_rejection_type = ResumeAdmissionRejected
            result = _competition_receipt(run_competition(
                args.task, args.work, args.run_id, args.config, args.project_root, resume=args.resume))
        else:
            from .agent import (
                ResumeAdmissionRejected,
                preflight_resume,
                run_fixed_agent,
                run_provider_agent,
            )
            from .model_calls import ModelCallConflict, ModelCallStore
            from .offline_script import load_offline_script
            from .workspace import CandidateWorkspace

            if os.environ.get("DFX_MANAGED_AGENT") != "offline-v1":
                raise ValueError("repair/resume require the managed offline entry point")
            resume_rejection_type = ResumeAdmissionRejected
            try:
                script = load_offline_script(args.fixed_script)
                script.validate_knowledge_paths(args.project_root, args.task, args.work)
                approval = script.knowledge_approval()
                if args.command == "resume":
                    session = (preflight_resume(args.task, args.work, args.run_id) if approval is None else
                        preflight_resume(args.task, args.work, args.run_id, knowledge_approval=approval))
                    # Legacy no-knowledge scripts keep their original execution path.
                    # New knowledge declarations are checked before any writable Agent store.
                    if script.schema_version in (5, 6):
                        script.check_resume_knowledge(session)
            except (OSError, ValueError, TypeError, KeyError) as exc:
                if args.command == "resume":
                    raise ResumeAdmissionRejected("resume offline script or host knowledge configuration rejected") from exc
                raise
            options = {**script.options(), "resume": args.command == "resume", "policy_features": script.features(),
                       "compaction_policy": script.compaction_policy(),
                       "working_context_policy": script.working_context_policy()}
            if script.schema_version in (5, 6):
                options.update({"applicability_policy": script.applicability_policy(),
                                "static_skill_policy": script.static_skill_policy()})
                if args.command == "resume":
                    options["knowledge_approval"] = approval
                else:
                    options["knowledge"] = script.knowledge_input(args.project_root, args.task, args.work, args.run_id)
            if script.mode == "fixed_response":
                result = run_fixed_agent(args.task, args.work, args.run_id, script.fixed_responses(),
                                         script.executor(), compaction_responses=script.compaction_responses(), **options)
            else:
                offset = 0
                if args.command == "resume":
                    try:
                        workspace = CandidateWorkspace.reopen(args.task, args.work, args.run_id)
                        store_path = workspace.root / "model-calls.sqlite"
                        if not store_path.is_file() or store_path.is_symlink():
                            raise ValueError("resume model ledger is missing or linked")
                        offset = ModelCallStore(store_path, read_only=True).usage_summary(
                            workspace.task_id, args.run_id)["completed_calls"]
                    except (OSError, ValueError, TypeError, KeyError, sqlite3.Error, ModelCallConflict) as exc:
                        raise ResumeAdmissionRejected("resume original model ledger rejected before execution") from exc
                result = run_provider_agent(args.task, args.work, args.run_id, script.adapter(response_offset=offset),
                                            script.executor(), **options)
        if args.command in ("doctor", "analyze") and args.output:
            if args.output.exists():
                raise ValueError("Output already exists; choose a new report path")
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(result, ensure_ascii=True, indent=2))
    except Exception as exc:
        if args is not None and args.command == "competition":
            if args.resume and resume_rejection_type is not None and isinstance(exc, resume_rejection_type):
                parser.exit(3, "Resume rejected before execution: competition configuration or saved evidence rejected; "
                            "private input details omitted from the console\n")
            parser.error("Competition entry rejected; private input details omitted from the console")
        if args is not None and args.command == "resume" and resume_rejection_type is not None and isinstance(exc, resume_rejection_type):
            parser.exit(3, "Resume rejected before execution: " + str(exc) + "\n")
        if args is not None and args.command in ("export", "verify-export"):
            reason = _EXPORT_SAFE_REASONS.get(str(exc), "evidence validation failed")
            parser.error("Export evidence rejected: " + reason + "; private input details omitted from the console")
        if not isinstance(exc, (OSError, ValueError, KeyError, TypeError, ValidationError, zipfile.BadZipFile)):
            raise
        parser.error(str(exc))
