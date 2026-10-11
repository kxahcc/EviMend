"""One-time installer launcher; Python 3.11+ and Windows, no PowerShell."""

from __future__ import annotations

import sys
import argparse
from pathlib import Path


def main() -> int:
    if sys.version_info < (3, 11):
        print("DFX setup needs an existing Python 3.11+ launcher.", file=sys.stderr)
        return 1
    root = Path(__file__).resolve().parents[1]
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(root / "src"))
    from dfx_agent.managed_runtime import main as manage

    arguments = sys.argv[1:]
    if arguments and arguments[0] == "--clean":
        parser = argparse.ArgumentParser(description="Release an idle project using an independent Python")
        parser.add_argument("--scope", choices=("env", "all"), required=True)
        parser.add_argument("--apply", action="store_true")
        parser.add_argument("--plan-id")
        parser.add_argument("--discard-evidence", action="store_true")
        args = parser.parse_args(arguments[1:])
        if args.apply != bool(args.plan_id):
            parser.error("apply requires the reviewed plan ID")
        command = ["--workspace", str(root), "Clean", "-Scope", args.scope]
        if args.apply:
            command += ["-Apply", "-PlanId", args.plan_id]
        if args.discard_evidence:
            command += ["-DiscardEvidence"]
        return manage(command)
    if arguments not in ([], ["--offline"]):
        print("Usage: setup.cmd [--offline] | setup.cmd --clean --scope env|all [--apply --plan-id ID]", file=sys.stderr)
        return 1
    code = manage(["--workspace", str(root), "Prepare"])
    if code:
        return code
    return manage(["--workspace", str(root), "Init", *(["-OfflineSync"] if arguments else [])])


if __name__ == "__main__":
    raise SystemExit(main())
