"""Local entry points for the first DFX implementation milestone."""

import argparse
import json
from pathlib import Path
import zipfile

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
        else:
            if args.output.resolve().is_relative_to(args.task.resolve()):
                raise ValueError("Analysis output must be outside the fixed task input")
            result = analyze_task(args.task, args.max_anchors)
        if args.command != "prepare" and args.output:
            if args.output.exists():
                raise ValueError("Output already exists; choose a new report path")
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(result, ensure_ascii=True, indent=2))
    except (OSError, ValueError, KeyError, TypeError, zipfile.BadZipFile) as exc:
        parser.error(str(exc))
