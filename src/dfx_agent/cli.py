"""Local entry points for the first DFX implementation milestone."""

import argparse
import json
import os
import zipfile
from pathlib import Path

from pydantic import ValidationError

from .analysis import analyze_task
from .runtime import doctor
from .task import CaseCatalog, prepare_task


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
    for name in ("repair", "resume"):
        offline = sub.add_parser(name, help="Managed fixed-response or mock-wire demonstration only")
        offline.add_argument("--task", required=True, type=Path)
        offline.add_argument("--work", required=True, type=Path)
        offline.add_argument("--run-id", required=True)
        offline.add_argument("--fixed-script", "--script", dest="fixed_script", required=True, type=Path)
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
        else:
            from .agent import run_fixed_agent, run_provider_agent
            from .model_calls import ModelCallStore
            from .offline_script import load_offline_script
            from .workspace import CandidateWorkspace

            if os.environ.get("DFX_MANAGED_AGENT") != "offline-v1":
                raise ValueError("repair/resume require the managed offline entry point")
            script = load_offline_script(args.fixed_script)
            options = {**script.options(), "resume": args.command == "resume", "policy_features": script.features(),
                       "compaction_policy": script.compaction_policy()}
            if script.mode == "fixed_response":
                result = run_fixed_agent(args.task, args.work, args.run_id, script.fixed_responses(),
                                         script.executor(), compaction_responses=script.compaction_responses(), **options)
            else:
                offset = 0
                if args.command == "resume":
                    workspace = CandidateWorkspace.reopen(args.task, args.work, args.run_id)
                    store_path = workspace.root / "model-calls.sqlite"
                    if not store_path.is_file() or store_path.is_symlink():
                        raise ValueError("resume model ledger is missing or linked")
                    offset = ModelCallStore(store_path).usage_summary(workspace.task_id, args.run_id)["completed_calls"]
                result = run_provider_agent(args.task, args.work, args.run_id, script.adapter(response_offset=offset),
                                            script.executor(), **options)
        if args.command in ("doctor", "analyze") and args.output:
            if args.output.exists():
                raise ValueError("Output already exists; choose a new report path")
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(result, ensure_ascii=True, indent=2))
    except (OSError, ValueError, KeyError, TypeError, ValidationError, zipfile.BadZipFile) as exc:
        parser.error(str(exc))
