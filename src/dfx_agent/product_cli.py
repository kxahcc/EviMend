"""User-facing DFX commands; execution stays inside the managed Windows backend."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import __version__
from .progress import read_progress

_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
_CAPTURE_BYTES = 32768


def _name(value: str) -> str:
    if not _NAME.fullmatch(value):
        raise argparse.ArgumentTypeError("use 1-64 letters, digits, underscores or hyphens")
    return value


def _checked(path: Path, *, exists: bool = True) -> Path:
    path = path.absolute()
    if any(ord(c) < 32 or ord(c) == 127 for c in str(path)):
        raise ValueError("path contains control characters")
    for part in (path, *path.parents):
        try:
            info = part.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError("linked paths are not supported")
    return path.resolve(strict=exists)


def _json(path: Path, limit: int = 4 * 1024 * 1024) -> dict[str, Any]:
    path = _checked(path)
    if not path.is_file() or path.stat().st_size > limit:
        raise ValueError("missing or oversized metadata")
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise TypeError("metadata must be an object")
    return value


def _workspace(value: Path | None) -> Path:
    if value is None and os.environ.get("DFX_PROJECT_ROOT"):
        value = Path(os.environ["DFX_PROJECT_ROOT"])
    if value is None:
        for candidate in (Path.cwd(), *Path.cwd().parents):
            if (candidate / "configs/local-runtime.json").is_file() and (candidate / "pyproject.toml").is_file():
                value = candidate
                break
    if value is None:
        raise ValueError("specify --workspace with the deployed DFX project directory")
    root = _checked(value)
    for relative in ("configs/local-runtime.json", "pyproject.toml"):
        _checked(root / relative)
    return root


def _relative(root: Path, value: Path, *, directory: bool = False) -> str:
    path = _checked(value if value.is_absolute() else root / value)
    if not path.is_relative_to(root) or path == root:
        raise ValueError("inputs must be inside the deployed project")
    parts = path.relative_to(root).parts
    if any(p.casefold() in {"data_b", "archive", ".git", ".agents", ".codex"} for p in parts):
        raise ValueError("use a reviewed task snapshot outside original data and archives")
    if path.is_dir() != directory:
        raise ValueError("input type differs from the requested file or directory")
    return path.relative_to(root).as_posix()


def _work(root: Path, name: str) -> Path:
    return _checked(root / ".dfx-local/work" / ("competition-" + _name(name)), exists=False)


def _backend(root: Path, arguments: list[str]) -> list[str]:
    if os.name != "nt":
        raise ValueError("this deployment currently requires Windows")
    # Launch the actual Python image rather than the Windows venv redirector,
    # so the process observed by this frontend is the exact lease manager.
    executable = Path(getattr(sys, "_base_executable", sys.executable))
    return [str(_checked(executable)), "-B", "-m", "dfx_agent.managed_runtime", "--workspace", str(root), *arguments]


def _backend_environment(root: Path) -> dict[str, str]:
    environment = os.environ.copy()
    # The independent setup interpreter need not have Agent dependencies or
    # this package installed; its manager uses only the distributed stdlib code.
    environment["PYTHONPATH"] = str(_checked(root / "src"))
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    return environment


class _Capture:
    """Drain a backend pipe without retaining private, unbounded output."""

    def __init__(self, pipe: Any):
        self.data = bytearray()
        self.overflow = False
        self.thread = threading.Thread(target=self._read, args=(pipe,), daemon=True)
        self.thread.start()

    def _read(self, pipe: Any) -> None:
        try:
            while chunk := pipe.read(4096):
                room = _CAPTURE_BYTES - len(self.data)
                self.data.extend(chunk[:room])
                self.overflow |= len(chunk) > room
        except (OSError, ValueError):
            self.overflow = True
        finally:
            try:
                pipe.close()
            except (OSError, ValueError):
                self.overflow = True

    def finish(self) -> dict[str, Any]:
        self.thread.join(timeout=2)
        if self.thread.is_alive() or self.overflow:
            raise ValueError("backend response unavailable or exceeds limit")
        value = json.loads(self.data.decode("utf-8-sig"))
        if not isinstance(value, dict):
            raise TypeError("backend response is not an object")
        return value


def _active(root: Path) -> dict[str, Any] | None:
    try:
        return _json(root / ".dfx-local/state/active.json", 16384)
    except FileNotFoundError:
        return None


def _invoke(root: Path, arguments: list[str]) -> tuple[int, dict[str, Any]]:
    process = subprocess.Popen(_backend(root, arguments), cwd=root, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, shell=False,
                               env=_backend_environment(root),
                               creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    output, errors = _Capture(process.stdout), _Capture(process.stderr)
    interrupted = False
    deadline = time.monotonic() + 1920
    while process.poll() is None:
        try:
            if time.monotonic() >= deadline:
                if not _stop_own_manager(root, process.pid):
                    raise subprocess.TimeoutExpired("managed backend", 1920)
                interrupted = True
                deadline = time.monotonic() + 30
            if interrupted:
                _stop_own_manager(root, process.pid)
            time.sleep(0.2)
        except KeyboardInterrupt:
            interrupted = True
    errors.thread.join(timeout=2)
    if interrupted:
        raise KeyboardInterrupt
    assert process.returncode is not None
    return process.returncode, output.finish()


def _stop_own_manager(root: Path, pid: int) -> bool:
    active = _active(root)
    if not active or active.get("manager_pid") != pid:
        return False
    # The exact-run backend rechecks GUID and process identity. A delayed stop
    # is written only to that manager's private request channel.
    process = subprocess.Popen(_backend(root, ["Stop", "-ExpectedRunId", active["run_id"]]),
                               cwd=root, stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=False,
                               env=_backend_environment(root),
                               creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    output, errors = _Capture(process.stdout), _Capture(process.stderr)
    process.wait(timeout=30)
    errors.thread.join(timeout=2)
    return process.returncode == 0 and output.finish().get("stopped") is True


def _stop(root: Path, name: str, expected: str | None = None) -> dict[str, Any]:
    active = _active(root)
    if active is None:
        return {"stopped": True, "active_run": False, "name": name}
    if active.get("task") != "competition" or active.get("competition_work_id") != name:
        raise ValueError("the active task belongs to another session")
    if expected is not None and active.get("run_id") != expected:
        raise ValueError("the active manager changed; no stop was sent")
    code, receipt = _invoke(root, ["Stop", "-ExpectedRunId", active["run_id"]])
    if code:
        raise ValueError("managed stop could not be confirmed")
    return {"name": name, "stopped": receipt.get("stopped") is True,
            "active_run": receipt.get("active_run")}


def _emit(value: dict[str, Any], mode: str) -> None:
    if mode != "text":
        print(json.dumps(value, ensure_ascii=True, separators=(",", ":")), flush=True)
        return
    event = value.get("event", "status")
    details = " | ".join(f"{key}={item}" for key, item in value.items()
                         if key not in {"event", "schema_version", "run_id", "sequence", "observed_at"})
    print(f"[{event}] {details}", flush=True)


def _events(work: Path, after: int, mode: str) -> int:
    observation = read_progress(work, after_sequence=after, max_events=100)
    for event in observation["events"]:
        if mode != "json":
            _emit(event, mode)
    return observation["next_sequence"]


def _all_events(work: Path, after: int) -> dict[str, Any]:
    """Drain saved pages without claiming that a partial sidecar is complete."""
    events: list[dict[str, Any]] = []
    observation = read_progress(work, after_sequence=after, max_events=1000)
    while observation["events"]:
        events.extend(observation["events"])
        after = observation["next_sequence"]
        if after >= observation["last_sequence"]:
            break
        observation = read_progress(work, after_sequence=after, max_events=1000)
    return {**observation, "events": events, "next_sequence": after}


def _summary(root: Path, name: str) -> dict[str, Any]:
    work = _work(root, name)
    report = _json(work / "competition-report.json", 32 * 1024 * 1024)
    if report.get("run_id") != name:
        raise ValueError("saved report belongs to another session")
    # This is an operational view. Cryptographic/semantic evidence verification
    # remains with existing independent report/export readers.
    cost = report.get("cost", {})
    if not isinstance(cost, dict):
        raise TypeError("invalid saved cost view")
    status, accepted = report["status"], report["accepted"]
    application, level = report["application_verification"], report.get("verification_level", "unknown")
    calls, tokens = cost.get("reserved_calls"), cost.get("known_total_tokens")
    if (status not in {"application_verified", "synthetic_checked", "unresolved"} or
            type(accepted) is not bool or application not in {"pass", "fail", "inconclusive", "not_run"} or
            level not in {"unknown", "not_run", "offline_synthetic", "application_execution"} or
            not isinstance(report.get("attempts"), list) or
            any(item is not None and (type(item) is not int or item < 0) for item in (calls, tokens)) or
            accepted and (status != "application_verified" or application != "pass" or level != "application_execution")):
        raise ValueError("saved summary fields are invalid")
    return {"event": "result", "name": name, "status": report["status"],
            "accepted": report["accepted"], "application_verification": report["application_verification"],
            "verification_level": report.get("verification_level", "unknown"),
            "candidate_count": len(report.get("attempts", [])),
            "model_calls": calls, "known_tokens": tokens,
            "report": str(work / "result.md"), "evidence": str(work / "competition-report.json"),
            "delivery": str(work / "delivery.zip"), "view_authority": "saved_operational_view"}


def _resume_inputs(root: Path, name: str) -> tuple[str, str]:
    work = _work(root, name)
    launch = _json(work / "product-launch.json", 8192)
    config = _checked(work / "product-config.json")
    if (launch.get("schema_version") != 1 or launch.get("run_id") != name or
            hashlib.sha256(config.read_bytes()).hexdigest() != launch.get("config_sha256")):
        raise ValueError("saved launch inputs changed")
    raw = _json(config)
    task = "" if raw.get("model_mode") == "mock_demo" else _relative(root, Path(launch["task"]), directory=True)
    return task, _relative(root, config)


def _run(root: Path, task: str, config: str, name: str, resume: bool, mode: str) -> int:
    work = _work(root, name)
    args = ["Run", "-Task", "competition", "-CompetitionConfigPath", config, "-CompetitionWorkId", name]
    if task:
        args += ["-CompetitionTaskPath", task]
    if resume:
        args += ["-Resume"]
    process = subprocess.Popen(_backend(root, args), cwd=root, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, shell=False,
                               env=_backend_environment(root),
                               creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    output, errors = _Capture(process.stdout), _Capture(process.stderr)
    if mode != "json":
        _emit({"event": "launch", "name": name, "mode": "resume" if resume else "new",
               "work": str(work), "feedback": "host activity; acceptance comes from the final report"}, mode)
    after = 0
    manager = None
    started = time.monotonic()
    heartbeat = started
    interrupted = False
    stop_unconfirmed = False
    while process.poll() is None:
        try:
            try:
                active = _active(root)
            except (OSError, ValueError, TypeError):
                active = None
            if (active and active.get("manager_pid") == process.pid and
                    active.get("competition_work_id") == name):
                manager = active["run_id"]
            if interrupted and manager is not None:
                try:
                    _stop(root, name, manager)
                except (OSError, ValueError, TypeError, subprocess.SubprocessError):
                    stop_unconfirmed = True
                    if mode != "json":
                        _emit({"event": "stop_unconfirmed", "name": name,
                               "note": "waiting for this manager to exit; saved work is retained"}, mode)
                manager = None
            after = _events(work, after, mode)
            if mode != "json" and time.monotonic() - heartbeat >= 10:
                _emit({"event": "waiting", "name": name, "elapsed_seconds": round(time.monotonic() - started),
                       "note": "waiting for the next saved activity"}, mode)
                heartbeat = time.monotonic()
            time.sleep(0.2)
        except KeyboardInterrupt:
            interrupted = True
    # Drain all remaining pages even if the manager exited between observations.
    remaining = _all_events(work, after)
    if mode != "json":
        for event in remaining["events"]:
            _emit(event, mode)
    errors.thread.join(timeout=2)
    receipt = output.finish()
    if interrupted:
        _emit({"event": "interrupted", "name": name, "manager_status": receipt.get("status"),
               "stop_confirmation": "unknown" if stop_unconfirmed else "manager_exited",
               "note": "resume rechecks original budgets and unknown calls"}, mode)
        return 130
    if process.returncode:
        _emit({"event": "failed", "name": name, "manager_status": receipt.get("status"),
               "note": "inspect the private manager log; no application success inferred"}, mode)
        return 1
    result = _summary(root, name)
    result = {**result, "tracking": remaining["tracking"]}
    _emit(result, mode)
    return 0 if result["accepted"] else 2


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="dfx", description="DFX: logs to localization, patch and verification.",
        epilog="Example: dfx --demo --name trial01 | dfx --target reviewed-task --config profile.json")
    result.add_argument("--version", action="version", version=f"dfx {__version__}")
    result.add_argument("--workspace", type=Path, help="deployed project directory (default: discover from current directory)")
    result.add_argument("--output-format", choices=("text", "json", "stream-json"), default="text")
    selection = result.add_mutually_exclusive_group()
    selection.add_argument("-t", "--target", type=Path, help="reviewed task snapshot containing source and crash log")
    selection.add_argument("--demo", action="store_true", help="synthetic end-to-end demo, without network or HarmonyOS")
    selection.add_argument("--resume", type=_name, metavar="NAME", help="resume using saved original launch inputs")
    result.add_argument("--config", type=Path, help="provider, verification commands and budget profile")
    result.add_argument("--name", type=_name, help="session name (auto-generated for a new run)")
    sub = result.add_subparsers(dest="command")
    for command in ("status", "logs", "stop", "report"):
        entry = sub.add_parser(command)
        entry.add_argument("session", type=_name, nargs="?" if command == "status" else None)
        entry.add_argument("--output-format", choices=("text", "json", "stream-json"), default=argparse.SUPPRESS)
        if command == "logs":
            entry.add_argument("--follow", action="store_true")
            entry.add_argument("--after", type=int, default=0, help="last seen event sequence")
    env = sub.add_parser("env", help="project-scoped installation and resource management")
    actions = env.add_subparsers(dest="action", required=True)
    for action in ("status", "prepare", "sync"):
        entry = actions.add_parser(action)
        if action == "sync":
            entry.add_argument("--offline", action="store_true", help="use existing project Python and cached packages only")
    clean = actions.add_parser("clean")
    clean.add_argument("--scope", choices=("cache", "testcache", "tmp", "work", "env", "all"), default="cache")
    clean.add_argument("--apply", action="store_true")
    clean.add_argument("--plan-id")
    clean.add_argument("--discard-evidence", action="store_true")
    configure = sub.add_parser("configure", help="create a cloud/application profile without sending a model request")
    configure.add_argument("--provider", choices=("openai", "deepseek", "compatible"), required=True)
    configure.add_argument("--model", required=True)
    configure.add_argument("--base-url", help="Chat API base URL for a compatible service")
    configure.add_argument("--key-env", help="name of the environment variable containing the key")
    configure.add_argument("--application", type=Path, required=True, help="host-owned application profile")
    configure.add_argument("--save", type=Path, required=True, help="new profile inside the project")
    configure.add_argument("--thinking", choices=("enabled", "disabled"))
    configure.add_argument("--max-calls", type=int, default=40)
    configure.add_argument("--max-tokens", type=int, default=256000)
    configure.add_argument("--max-output-tokens", type=int, default=4096)
    configure.add_argument("--max-seconds", type=int, default=900)
    configure.add_argument("--output-parameter", choices=("max_tokens", "max_completion_tokens"))
    doctor = sub.add_parser("doctor", help="check declared local prerequisites without a model/device call")
    doctor.add_argument("--application", type=Path, required=True)
    return result


def main(argv: list[str] | None = None) -> int:
    cli = parser()
    args = cli.parse_args(argv)
    try:
        root = _workspace(args.workspace)
        if args.command:
            if args.target or args.demo or args.resume or args.config or args.name:
                cli.error("launch flags cannot be combined with a management command")
            if args.command in {"configure", "doctor"}:
                from .application_profile import (
                    application_preflight,
                    load_application_profile,
                )
                application = load_application_profile(root / _relative(root, args.application))
                observation = application_preflight(application)
                if args.command == "doctor":
                    _emit({"event": "preflight", **observation}, args.output_format)
                    return 0 if observation["status"] == "configured" else 1
                from .competition import CompetitionConfig
                from .model_provider import provider_connection
                from .working_context_plan import WorkingContextPolicy
                settings: dict[str, Any] = {"max_output_tokens": args.max_output_tokens}
                if args.output_parameter:
                    settings["max_output_parameter"] = args.output_parameter
                if args.thinking is not None:
                    settings["thinking_mode"] = args.thinking
                elif args.provider == "deepseek":
                    settings["thinking_mode"] = "disabled"
                connection, model = provider_connection(args.provider, args.model,
                    base_url=args.base_url, credential_env=args.key_env, **settings)
                profile = CompetitionConfig(provider=connection, model=model, model_mode="cloud",
                    execution=application.execution, max_calls=args.max_calls,
                    max_known_tokens=args.max_tokens, max_seconds=args.max_seconds,
                    working_context=WorkingContextPolicy(enabled=False) if args.thinking == "enabled"
                        else WorkingContextPolicy(enabled=True, history_selection="repair_frontier",
                                                  context_layout="decision_layers"))
                destination = _checked(args.save if args.save.is_absolute() else root / args.save, exists=False)
                if (not destination.is_relative_to(root) or destination == root or
                        any(part.casefold() in {"data_b", "archive", ".git", ".agents", ".codex"}
                            for part in destination.relative_to(root).parts)):
                    raise ValueError("configuration output must be inside the deployed project")
                # Exclusive creation keeps an existing reviewed profile intact.
                with destination.open("x", encoding="utf-8") as output:
                    output.write(profile.model_dump_json(indent=2) + "\n")
                _emit({"event": "configuration_created", "path": str(destination),
                    "provider": model.provider_id, "model": model.model_id,
                    "credential_env": connection.credential_env,
                    "application_prerequisites": observation["status"],
                    "model_verification": "not_run", "experiments_enabled": False}, args.output_format)
                return 0
            if args.command == "env":
                backend = [{"status": "Status", "prepare": "Prepare", "sync": "Init", "clean": "Clean"}[args.action]]
                if args.action == "sync" and args.offline:
                    backend += ["-OfflineSync"]
                if args.action == "clean":
                    if args.apply and args.scope in {"env", "all"}:
                        _emit({"event": "external_cleanup_required", "scope": args.scope,
                            "note": "DFX cannot delete its running interpreter. Exit DFX, then use the independent Python setup launcher.",
                            "command": f"setup.cmd --clean --scope {args.scope} --apply --plan-id {args.plan_id or 'PREVIEW_PLAN_ID'}"
                                + (" --discard-evidence" if args.discard_evidence else "")}, args.output_format)
                        return 1
                    backend += ["-Scope", args.scope]
                    if args.apply:
                        if not args.plan_id:
                            cli.error("clean --apply requires the preview plan ID")
                        backend += ["-Apply", "-PlanId", args.plan_id]
                    elif args.plan_id:
                        cli.error("--plan-id requires --apply")
                    if args.discard_evidence:
                        backend += ["-DiscardEvidence"]
                code, receipt = _invoke(root, backend)
                _emit(receipt, args.output_format)
                return 0 if code == 0 else 1
            if args.command == "stop":
                _emit(_stop(root, args.session), args.output_format)
                return 0
            if args.command == "status" and args.session is None:
                code, receipt = _invoke(root, ["Status"])
                _emit(receipt, args.output_format)
                return 0 if code == 0 else 1
            work = _work(root, args.session)
            if not work.is_dir():
                raise ValueError("session does not exist")
            if args.command == "report":
                _emit(_summary(root, args.session), args.output_format)
            elif args.command == "status":
                observation = _all_events(work, 0)
                active = _active(root)
                _emit({"event": "status", "name": args.session,
                       "activity": "lease_present" if active and active.get("competition_work_id") == args.session else "no_active_lease",
                       "tracking": observation["tracking"], "latest_event": observation["events"][-1:]}, args.output_format)
            else:
                if args.after < 0:
                    cli.error("--after must be nonnegative")
                if args.follow and args.output_format == "json":
                    cli.error("logs --follow requires text or stream-json output")
                after = args.after
                while True:
                    observation = _all_events(work, after)
                    after = observation["next_sequence"]
                    if args.output_format == "json":
                        _emit(observation, args.output_format)
                    else:
                        for event in observation["events"]:
                            _emit(event, args.output_format)
                    active = _active(root)
                    if not args.follow or not active or active.get("competition_work_id") != args.session:
                        break
                    time.sleep(0.2)
            return 0
        if args.resume:
            if args.config or args.name:
                cli.error("resume uses the saved profile and name; do not replace them")
            task, config = _resume_inputs(root, args.resume)
            return _run(root, task, config, args.resume, True, args.output_format)
        if not args.demo and args.target is None:
            cli.error("provide --target, --demo or --resume")
        config = _relative(root, args.config or Path("configs/competition-demo.json"))
        profile = _json(root / config)
        if args.demo != (profile.get("model_mode") == "mock_demo"):
            cli.error("--demo requires a demo profile; --target requires a real-model profile")
        if args.target is not None and args.config is None:
            cli.error("--target requires an explicit --config profile")
        task = _relative(root, args.target, directory=True) if args.target else ""
        name = args.name or datetime.now(UTC).strftime("run-%Y%m%d-%H%M%S-") + os.urandom(3).hex()
        return _run(root, task, config, name, False, args.output_format)
    except KeyboardInterrupt:
        _emit({"event": "interrupted", "note": "viewer stopped; use dfx stop NAME to stop an active session"}, args.output_format)
        return 130
    except subprocess.TimeoutExpired:
        _emit({"event": "manager_unknown", "note": "backend exit or stop could not be confirmed; inspect managed status before starting another task"}, args.output_format)
        return 1
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        _emit({"event": "error", "note": "command rejected or backend unavailable; check workspace, input paths, environment and saved state"}, args.output_format)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
