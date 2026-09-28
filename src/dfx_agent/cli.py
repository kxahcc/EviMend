"""Local entry points for the first DFX implementation milestone."""

import argparse
import json
import os
import zipfile
from pathlib import Path

from .analysis import analyze_task
from .runtime import doctor
from .task import prepare_task


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
        offline = sub.add_parser(name, help="Managed fixed-response offline demonstration only")
        offline.add_argument("--task", required=True, type=Path)
        offline.add_argument("--work", required=True, type=Path)
        offline.add_argument("--run-id", required=True)
        offline.add_argument("--fixed-script", required=True, type=Path)
    try:
        args = parser.parse_args()
        if args.command == "doctor":
            result = doctor()
        elif args.command == "prepare":
            catalog = json.loads(args.catalog.read_text(encoding="utf-8"))
            if catalog.get("schema_version") != 1:
                raise ValueError("Unsupported catalog schema")
            matches = [c for c in catalog["cases"] if c["task_id"] == args.case]
            if len(matches) != 1:
                raise ValueError("Expected exactly one catalog case matching --case")
            result = prepare_task(args.data_root, matches[0], args.output)
        elif args.command == "analyze":
            if args.output.resolve().is_relative_to(args.task.resolve()):
                raise ValueError("Analysis output must be outside the fixed task input")
            result = analyze_task(args.task, args.max_anchors)
        else:
            from langchain_core.messages import AIMessage

            from .agent import run_fixed_agent
            from .verification import FixedTextExecutor

            if os.environ.get("DFX_MANAGED_AGENT") != "offline-v1":
                raise ValueError("repair/resume require the managed offline entry point")
            if args.fixed_script.stat().st_size > 1_048_576:
                raise ValueError("fixed-response script exceeds 1 MiB")
            script = json.loads(args.fixed_script.read_text(encoding="utf-8"))
            if script.get("schema_version") != 1 or set(script) != {"schema_version", "responses", "expected"}:
                raise ValueError("invalid fixed-response script")
            responses = []
            for index, item in enumerate(script["responses"], start=1):
                if set(item) != {"tool", "args", "usage"}:
                    raise ValueError("invalid fixed response")
                tool_call = [] if item["tool"] is None else [{"name": item["tool"], "args": item["args"], "id": f"call-{index}"}]
                responses.append(AIMessage(content="done" if not tool_call else "", tool_calls=tool_call,
                                           usage_metadata=item["usage"]))
            expected = script["expected"]
            if set(expected) != {"path", "text"}:
                raise ValueError("invalid synthetic expectation")
            result = run_fixed_agent(args.task, args.work, args.run_id, responses,
                                     FixedTextExecutor(expected["path"], expected["text"]),
                                     resume=args.command == "resume")
        if args.command in ("doctor", "analyze") and args.output:
            if args.output.exists():
                raise ValueError("Output already exists; choose a new report path")
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(result, ensure_ascii=True, indent=2))
    except (OSError, ValueError, KeyError, TypeError, zipfile.BadZipFile) as exc:
        parser.error(str(exc))
