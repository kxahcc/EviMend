"""Private, stopped synthetic evidence packages; never execute saved messages."""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
from contextlib import closing
from pathlib import Path
from typing import Any
from uuid import UUID

from .contracts import require_hash
from .evidence_io import local_evidence_path, open_readonly_database
from .metrics import (
    MAX_JSON_BYTES,
    MAX_MANIFEST_BYTES,
    MAX_TREE_FILES,
    AnyEvaluationManifest,
    _decode,
    _guard,
    _identifier,
    _input_paths,
    _load,
    _overlap,
    _read_bytes,
    _reference,
    _tree_snapshot,
    aggregate_evaluation,
    parse_evaluation_manifest,
)
from .model_calls import canonical, sha256
from .task import digest

_TASK_ENTRIES = frozenset({"task.json", "source-manifest.json", "crash.log", "source"})
_DATABASES = frozenset({"checkpoints.sqlite", "model-calls.sqlite", "tool-actions.sqlite"})
_WORK_ENTRIES = frozenset({
    "run-state.json", "workspace.json", "session.json", "agent-report.json", "condition-inputs.json",
    "base", "draft", "candidates", "checks", "diagnoses", "obligations", "feedback", "decisions",
    "conditions", "compactions", "value-sources", "working-context",
    "knowledge-profile.json", "knowledge", "experience-bindings", "experience-queries",
    "applicability-contexts", "applicability-reviews", "applicability-materials", "solver-inputs",
}) | _DATABASES | frozenset(name + suffix for name in _DATABASES for suffix in ("-wal", "-shm", "-journal"))
_PRIVACY = {
    "local_only": True, "redacted": False, "share_ready": False,
    "contents": ["source_and_candidates", "crash_logs", "raw_model_messages", "tool_arguments_and_results",
                 "graph_checkpoints", "failed_attempts_and_costs"],
}


def _encoded(value: Any) -> bytes:
    return canonical(value).encode("utf-8")


def _long_path(path: Path) -> Path:
    """Use Windows long-path APIs locally without changing registry or user PATH."""
    return local_evidence_path(path)


def _exists(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    return True


def _worker_ancestor(worker_pid: Any) -> bool:
    """Windows venv launchers may add a process between Python and the worker."""
    if type(worker_pid) is not int or worker_pid <= 0:
        return False
    if worker_pid == os.getppid():
        return True
    if os.name != "nt":
        return False
    import ctypes
    from ctypes import wintypes

    class ProcessEntry(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("usage", wintypes.DWORD), ("pid", wintypes.DWORD),
                    ("heap", ctypes.c_size_t), ("module", wintypes.DWORD), ("threads", wintypes.DWORD),
                    ("parent", wintypes.DWORD), ("priority", wintypes.LONG), ("flags", wintypes.DWORD),
                    ("executable", wintypes.WCHAR * 260)]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    snapshot = kernel.CreateToolhelp32Snapshot
    snapshot.argtypes, snapshot.restype = [wintypes.DWORD, wintypes.DWORD], wintypes.HANDLE
    first, following = kernel.Process32FirstW, kernel.Process32NextW
    for function in (first, following):
        function.argtypes, function.restype = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry)], wintypes.BOOL
    kernel.CloseHandle.argtypes, kernel.CloseHandle.restype = [wintypes.HANDLE], wintypes.BOOL
    handle = snapshot(2, 0)
    if handle in (None, ctypes.c_void_p(-1).value):
        raise ValueError("worker process ancestry is unavailable")
    parents: dict[int, int] = {}
    try:
        entry = ProcessEntry()
        entry.size = ctypes.sizeof(entry)
        more = first(handle, ctypes.byref(entry))
        while more:
            parents[entry.pid] = entry.parent
            more = following(handle, ctypes.byref(entry))
    finally:
        kernel.CloseHandle(handle)
    current, visited = os.getpid(), set()
    while current in parents and current not in visited:
        visited.add(current)
        current = parents[current]
        if current == worker_pid:
            return True
    return False


def _active(root: Path, owner_id: str) -> None:
    state = root / ".dfx-local/state"
    if _exists(state / "remote-jobs.json"):
        raise ValueError("unresolved remote activity blocks evidence export")
    if _exists(state / "active.json"):
        active = _load(state / "active.json")
        if not (isinstance(active, dict) and os.environ.get("DFX_EXPORT_RUN_ID") and
                active.get("task") == "export" and active.get("run_id") == os.environ["DFX_EXPORT_RUN_ID"] and
                active.get("project_instance_id") == owner_id and
                _worker_ancestor(active.get("child_pid"))):
            raise ValueError("another active task blocks evidence export")


def _context(root: Path, export_id: str, owner_id: str) -> tuple[dict[str, Any], str, str]:
    _identifier(export_id)
    UUID(owner_id)
    _guard(root)
    for path in (root / ".dfx-local/state", root / ".dfx-local/tmp", root / "runs/exports"):
        _guard(path)
        if not path.is_dir():
            raise ValueError("managed export directories are not prepared")
    owner_path = root / ".dfx-local/owner.json"
    owner_raw = _read_bytes(owner_path, MAX_JSON_BYTES)
    owner = _decode(owner_raw)
    if not (isinstance(owner, dict) and owner.get("schema_version") == 1 and
            owner.get("project_instance_id") == owner_id and owner.get("repo_real_path") == str(root.resolve()) and
            owner.get("managed_real_path") == str((root / ".dfx-local").resolve())):
        raise ValueError("export owner differs from this prepared project")
    config_raw = _read_bytes(root / "configs/local-runtime.json", MAX_JSON_BYTES)
    config = _decode(config_raw)
    if not (isinstance(config, dict) and config.get("schema_version") == 1 and
            config.get("enforcement_status") == "offline_agent_guarded" and
            config.get("paths") == {"managed_root": ".dfx-local", "export_root": "runs/exports"}):
        raise ValueError("export requires the current managed runtime policy")
    limits = config.get("limits")
    if not isinstance(limits, dict):
        raise ValueError("missing export resource limits")  # noqa: TRY004 -- Rejected evidence uses ValueError.
    for name in ("max_export_package_bytes", "managed_disk_budget_bytes", "min_free_disk_bytes"):
        if type(limits.get(name)) is not int or limits[name] <= 0:
            raise ValueError("invalid export resource limit")
    fraction = limits.get("min_free_disk_fraction")
    if not isinstance(fraction, (int, float)) or isinstance(fraction, bool) or not 0 < fraction < 1:
        raise ValueError("invalid disk reserve fraction")
    _active(root, owner_id)
    return limits, digest(owner_raw), digest(config_raw)


def _usage(path: Path, *, internal_links: bool = False) -> int:
    """No following links; only pre-existing internal runtime links are tolerated."""
    path = _long_path(path)
    _guard(path, missing=True)
    if not _exists(path):
        return 0
    pending = [path]
    size = 0
    while pending:
        current = pending.pop()
        info = current.lstat()
        linked = stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & getattr(
            stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        if linked:
            if not internal_links or not current.resolve().is_relative_to(path.resolve()):
                raise ValueError("resource accounting found an external or export link")
            continue
        if stat.S_ISDIR(info.st_mode):
            pending.extend(current.iterdir())
        elif stat.S_ISREG(info.st_mode):
            # Runtime uv hardlinks are counted conservatively; export hardlinks are refused.
            if not internal_links and info.st_nlink != 1:
                raise ValueError("resource accounting found a shared export file")
            size += info.st_size
        else:
            raise ValueError("resource accounting found a nonregular entry")
    return size


def _capacity(root: Path, limits: dict[str, Any], remaining: int, accounted_total: int | None = None) -> int:
    total = (accounted_total if accounted_total is not None else
             _usage(root / ".dfx-local", internal_links=True) + _usage(root / "runs/exports"))
    disk = shutil.disk_usage(root)
    reserve = max(limits["min_free_disk_bytes"], int(disk.total * limits["min_free_disk_fraction"]))
    if total + remaining > limits["managed_disk_budget_bytes"] or disk.free - remaining < reserve:
        raise ValueError("export would exceed shared storage budget or free disk reserve")
    return total


def _validate_tree(path: Path, allowed: frozenset[str], maximum: int) -> None:
    _guard(path)
    if not path.is_dir() or any(item.name not in allowed for item in path.iterdir()):
        raise ValueError("evidence tree has an unregistered top-level entry")
    pending = [path]
    size, count = 0, 0
    folded: set[str] = set()
    while pending:
        current = pending.pop()
        _guard(current)
        relative = current.relative_to(path).as_posix()
        if relative != ".":
            _reference(relative)
        if relative.casefold() in folded:
            raise ValueError("case-insensitive evidence path collision")
        folded.add(relative.casefold())
        count += 1
        if current.is_dir():
            pending.extend(current.iterdir())
        else:
            size += current.stat().st_size
            if current.name.casefold().startswith(".env") or current.name.casefold() in {
                    "credentials.json", "credentials", "id_rsa", "id_ed25519"} or (
                    current.suffix.casefold() in {".pem", ".key", ".pfx", ".p12"}):
                raise ValueError("credential-like file is not eligible for a raw evidence export")
            if allowed == _WORK_ENTRIES and current.parent == path and current.name in _WORK_ENTRIES:
                for database in _DATABASES:
                    if (current.name in {database + suffix for suffix in ("-wal", "-journal", "-shm")} and
                            (not _exists(path / database) or (not current.name.endswith("-shm") and current.stat().st_size))):
                        raise ValueError("database sidecar is orphaned or unmerged")
            nested_index = (current.relative_to(path).parts[:2] == ("knowledge", "indexes") and
                            current.suffix == ".sqlite")
            if allowed == _WORK_ENTRIES and ((current.parent == path and current.name in _DATABASES) or nested_index):
                with closing(open_readonly_database(current)) as connection:
                    if [row[0] for row in connection.execute("PRAGMA quick_check")] != ["ok"]:
                        raise ValueError("database evidence failed read-only integrity checking")
        if size > maximum or count > MAX_TREE_FILES:
            raise ValueError("evidence tree exceeds export capacity or format entry limit")


def _snapshot(manifest: AnyEvaluationManifest, root: Path, maximum: int) -> dict[str, Any]:
    trees: dict[str, Any] = {}
    tasks = {task.task_path for task in manifest.tasks}
    for path in _input_paths(manifest, root):
        relative = path.relative_to(root).as_posix()
        if not _exists(path):
            trees[relative] = None
            continue
        _validate_tree(path, _TASK_ENTRIES if relative in tasks else _WORK_ENTRIES, maximum)
        trees[relative] = {key: list(value) for key, value in sorted(_tree_snapshot(path).items())}
    return trees


def _payload_inventory(trees: dict[str, Any]) -> dict[str, Any]:
    inventory: dict[str, Any] = {"payload": {"kind": "directory"}}
    for relative, snapshot in trees.items():
        if snapshot is None:
            continue
        for child, value in snapshot.items():
            path = Path("payload") / relative
            if child != ".":
                path /= child
            key = path.as_posix()
            inventory[key] = ({"kind": "directory"} if value[4] == "directory" else
                              {"kind": "file", "size": value[0], "sha256": value[4]})
            for parent in path.parents:
                if parent.as_posix() != ".":
                    inventory.setdefault(parent.as_posix(), {"kind": "directory"})
    return dict(sorted(inventory.items()))


def _byte_snapshot(root: Path) -> dict[str, tuple[int, int, int, int, str]]:
    """Package inventory is bytes, not a JSON parser for application resources."""
    _guard(root)
    found: dict[str, tuple[int, int, int, int, str]] = {}
    pending = [root]
    while pending:
        path = pending.pop()
        _guard(path)
        info = path.lstat()
        if path.is_dir():
            checksum = "directory"
            pending.extend(path.iterdir())
        else:
            hashed = hashlib.sha256()
            with path.open("rb") as source:
                while chunk := source.read(1_048_576):
                    hashed.update(chunk)
            checksum = hashed.hexdigest()
        found[path.relative_to(root).as_posix()] = (
            0 if path.is_dir() else info.st_size, info.st_mtime_ns, info.st_ino, info.st_dev, checksum)
        if len(found) > MAX_TREE_FILES:
            raise ValueError("export package exceeds format entry limit")
    return found


def _package_value(plan: dict[str, Any]) -> dict[str, Any]:
    value = {
        "schema_version": 1, "scope": "offline_synthetic", "status": "verified",
        "export_id": plan["export_id"], "owner_id": plan["owner_id"], "plan_id": plan["plan_id"],
        "manifest_sha256": plan["manifest_sha256"], "aggregate_sha256": plan["aggregate_sha256"],
        "privacy": _PRIVACY, "plan": plan,
        "validator_provenance": {"module": "dfx_agent.exporting", "schema_version": 1,
                                 "execution_versions": "not_reconstructed"},
    }
    value["package_sha256"] = sha256(canonical(value))
    return value


def _preview_export(manifest_path: Path, root: Path, export_id: str, owner_id: str,
                    *, reserve_new: bool) -> dict[str, Any]:
    """Inspect only; the caller explicitly saves the resulting reviewable plan."""
    root, manifest_path = root.absolute(), manifest_path.absolute()
    limits, owner_sha, config_sha = _context(root, export_id, owner_id)
    target = root / "runs/exports" / export_id
    _guard(target, missing=True)
    if _exists(target):
        raise ValueError("export target already exists")
    try:
        relative = manifest_path.relative_to(root).as_posix()
    except ValueError:
        raise ValueError("manifest must be within this project") from None
    _reference(relative)
    fixed_plan = root / ".dfx-local/state" / ("export-plan-" + export_id + ".json")
    if manifest_path in {fixed_plan, fixed_plan.with_name(fixed_plan.name + ".new")}:
        raise ValueError("manifest overlaps the export plan output")
    if manifest_path.is_relative_to(root / "runs/exports"):
        raise ValueError("an export package is not an original manifest input")
    raw = _read_bytes(manifest_path, MAX_MANIFEST_BYTES)
    manifest = parse_evaluation_manifest(_decode(raw))
    paths = _input_paths(manifest, root)
    if any(_overlap(manifest_path, path) or _overlap(root / "runs/exports", path) or
           _overlap(root / ".dfx-local/state", path) or path == root / ".dfx-local/tmp" for path in paths):
        raise ValueError("manifest or export management paths overlap input trees")
    trees = _snapshot(manifest, root, limits["max_export_package_bytes"])
    report = aggregate_evaluation(manifest, root)
    report_raw = _encoded(report)
    inventory = _payload_inventory(trees)
    inventory["evaluation.json"] = {"kind": "file", "size": len(raw), "sha256": digest(raw)}
    inventory["aggregate-report.json"] = {"kind": "file", "size": len(report_raw), "sha256": digest(report_raw)}
    plan: dict[str, Any] = {
        "schema_version": 1, "scope": "offline_synthetic", "export_id": export_id, "owner_id": owner_id,
        "manifest_path": relative, "manifest_sha256": digest(raw), "aggregate_sha256": report["report_sha256"],
        "owner_sha256": owner_sha, "config_sha256": config_sha, "privacy": _PRIVACY,
        "source_snapshot": trees, "inventory": dict(sorted(inventory.items())), "estimated_bytes": 0,
    }
    # All hashes have a fixed width; reach a fixed point for the size's decimal width.
    for _ in range(8):
        plan["plan_id"] = sha256(canonical({key: value for key, value in plan.items() if key != "plan_id"}))
        metadata_raw = _encoded(_package_value(plan))
        if len(metadata_raw) > MAX_JSON_BYTES:
            raise ValueError("export metadata exceeds format size limit")
        estimated = sum(value.get("size", 0) for value in inventory.values()) + len(metadata_raw)
        if estimated == plan["estimated_bytes"]:
            break
        plan["estimated_bytes"] = estimated
    else:
        raise ValueError("export size calculation did not converge")
    if plan["estimated_bytes"] > limits["max_export_package_bytes"]:
        raise ValueError("export package exceeds configured capacity")
    _capacity(root, limits, plan["estimated_bytes"] if reserve_new else 0)
    if (trees != _snapshot(manifest, root, limits["max_export_package_bytes"]) or
            raw != _read_bytes(manifest_path, MAX_MANIFEST_BYTES) or
            (limits, owner_sha, config_sha) != _context(root, export_id, owner_id)):
        raise ValueError("export inputs changed during preview")
    return plan


def preview_export(manifest_path: Path, root: Path, export_id: str, owner_id: str) -> dict[str, Any]:
    return _preview_export(manifest_path, root, export_id, owner_id, reserve_new=True)


def save_export_plan(plan: dict[str, Any], path: Path) -> None:
    """Atomically replace a preview, never a package or source input."""
    _check_plan(plan)
    path = path.absolute()
    _guard(path, missing=True)
    _guard(path.parent)
    if (path.parent.name != "state" or path.parent.parent.name != ".dfx-local" or
            path.name != "export-plan-" + plan["export_id"] + ".json"):
        raise ValueError("export plan must use its fixed managed state filename")
    if _exists(path):
        previous = _load(path)
        _check_plan(previous)
        if previous["owner_id"] != plan["owner_id"] or previous["export_id"] != plan["export_id"]:
            raise ValueError("existing export plan belongs to another identity")
    temporary = path.with_name(path.name + ".new")
    _guard(temporary, missing=True)
    if _exists(temporary):
        raise ValueError("unfinished export plan exists")
    root = path.parent.parent.parent
    limits, _, _ = _context(root, plan["export_id"], plan["owner_id"])
    _capacity(root, limits, len(_encoded(plan)))
    with temporary.open("xb") as output:
        output.write(_encoded(plan))
        output.flush()
        os.fsync(output.fileno())
    _guard(path, missing=True)
    temporary.replace(path)


def _check_plan(plan: Any) -> dict[str, Any]:
    required = {"schema_version", "scope", "export_id", "owner_id", "manifest_path", "manifest_sha256",
                "aggregate_sha256", "owner_sha256", "config_sha256", "privacy", "source_snapshot", "inventory",
                "estimated_bytes", "plan_id"}
    if not (isinstance(plan, dict) and set(plan) == required and type(plan.get("schema_version")) is int and
            plan.get("schema_version") == 1 and plan.get("scope") == "offline_synthetic"
            and plan.get("privacy") == _PRIVACY and isinstance(plan.get("plan_id"), str) and
            sha256(canonical({key: value for key, value in plan.items() if key != "plan_id"})) == plan["plan_id"]):
        raise ValueError("export plan content or hash differs")
    try:
        _identifier(plan["export_id"])
        UUID(plan["owner_id"])
        _reference(plan["manifest_path"])
        for name in ("manifest_sha256", "aggregate_sha256", "owner_sha256", "config_sha256", "plan_id"):
            require_hash(plan[name])
        if (type(plan["estimated_bytes"]) is not int or plan["estimated_bytes"] <= 0 or
                not isinstance(plan["source_snapshot"], dict) or not isinstance(plan["inventory"], dict) or
                len(plan["inventory"]) > MAX_TREE_FILES):
            raise ValueError("invalid export plan inventory or size")
        folded: set[str] = set()
        for relative, entry in plan["inventory"].items():
            _reference(relative)
            if relative.casefold() in folded or not isinstance(entry, dict):
                raise ValueError("invalid export plan entry")
            folded.add(relative.casefold())
            if entry.get("kind") == "directory":
                if set(entry) != {"kind"}:
                    raise ValueError("invalid directory entry")
            elif (set(entry) == {"kind", "size", "sha256"} and entry.get("kind") == "file" and
                  type(entry["size"]) is int and entry["size"] >= 0):
                require_hash(entry["sha256"])
            else:
                raise ValueError("invalid file entry")
        roots: list[Path] = []
        for relative, snapshot in plan["source_snapshot"].items():
            _reference(relative)
            roots.append(Path(relative.casefold()))
            if snapshot is None:
                continue
            if not isinstance(snapshot, dict) or not snapshot or len(snapshot) > MAX_TREE_FILES:
                raise ValueError("invalid source snapshot")
            if snapshot.get(".") is None or snapshot["."][4] != "directory":
                raise ValueError("source snapshot has no root directory")
            children: set[str] = set()
            for child, identity in snapshot.items():
                if child != ".":
                    _reference(child)
                if child.casefold() in children or not (isinstance(identity, list) and len(identity) == 5 and
                        all(type(number) is int and number >= 0 for number in identity[:4])):
                    raise ValueError("invalid source file identity")
                children.add(child.casefold())
                if identity[4] == "directory":
                    if identity[0] != 0:
                        raise ValueError("directory has a file size")
                else:
                    require_hash(identity[4])
        if any(_overlap(left, right) for index, left in enumerate(roots) for right in roots[index + 1:]):
            raise ValueError("overlapping export source snapshots")
        payload = {key: entry for key, entry in plan["inventory"].items() if key.startswith("payload/") or key == "payload"}
        if _payload_inventory(plan["source_snapshot"]) != payload or set(plan["inventory"]) - set(payload) != {
                "evaluation.json", "aggregate-report.json"}:
            raise ValueError("export inventory differs from source snapshot")
    except (TypeError, AttributeError, KeyError, IndexError) as exc:
        raise ValueError("invalid export plan field types") from exc
    return plan


def _copy_file(source: Path, destination: Path, expected: dict[str, Any], root: Path,
               limits: dict[str, Any], remaining: list[int]) -> None:
    source = _long_path(source)
    _guard(source)
    _guard(destination, missing=True)
    checksum = hashlib.sha256()
    copied = 0
    with source.open("rb") as input_file, destination.open("xb") as output:
        while chunk := input_file.read(1_048_576):
            copied += len(chunk)
            if copied > expected["size"]:
                raise ValueError("source grew during export")
            _capacity(root, limits, remaining[0], remaining[1] + remaining[2] - remaining[0])
            output.write(chunk)
            checksum.update(chunk)
            remaining[0] -= len(chunk)
        output.flush()
        os.fsync(output.fileno())
    if copied != expected["size"] or checksum.hexdigest() != expected["sha256"]:
        raise ValueError("source changed during export")


def apply_export(manifest_path: Path, root: Path, export_id: str, owner_id: str,
                 plan_path: Path, plan_id: str) -> dict[str, Any]:
    """Recheck the reviewed inputs, copy privately, verify, then publish a new ID."""
    root = root.absolute()
    saved = _load(plan_path.absolute())
    _check_plan(saved)
    expected_plan = root / ".dfx-local/state" / ("export-plan-" + export_id + ".json")
    if plan_path.absolute() != expected_plan:
        raise ValueError("export plan is outside its fixed managed filename")
    fresh = preview_export(manifest_path, root, export_id, owner_id)
    if saved != fresh or fresh["plan_id"] != plan_id:
        raise ValueError("reviewed export plan no longer matches current inputs")
    limits, _, _ = _context(root, export_id, owner_id)
    stage = root / ".dfx-local/tmp" / ("ex-" + plan_id[:16])
    target = root / "runs/exports" / export_id
    _guard(stage, missing=True)
    if _exists(stage):
        raise ValueError("unfinished export stage exists; inspect it before cleanup")
    raw_manifest = _read_bytes(manifest_path.absolute(), MAX_MANIFEST_BYTES)
    frozen_manifest = parse_evaluation_manifest(_decode(raw_manifest))
    if any(_overlap(stage, path) for path in _input_paths(frozen_manifest, root)):
        raise ValueError("export stage overlaps source evidence")
    base_usage = _capacity(root, limits, fresh["estimated_bytes"])
    stage = _long_path(stage)
    stage.mkdir()
    # A failure leaves an unpublished, owned stage for explicit cleanup; no source deletion.
    remaining = [fresh["estimated_bytes"], base_usage, fresh["estimated_bytes"]]
    for relative, entry in fresh["inventory"].items():
        destination = stage / relative
        _guard(destination, missing=True)
        if entry["kind"] == "directory":
            destination.mkdir(parents=True, exist_ok=True)
        elif relative.startswith("payload/"):
            _copy_file(root / relative.removeprefix("payload/"), destination, entry, root, limits, remaining)
    manifest_raw = _read_bytes(manifest_path.absolute(), MAX_MANIFEST_BYTES)
    manifest_entry = fresh["inventory"]["evaluation.json"]
    if len(manifest_raw) != manifest_entry["size"] or digest(manifest_raw) != manifest_entry["sha256"]:
        raise ValueError("manifest changed during export")
    manifest = parse_evaluation_manifest(_decode(manifest_raw))
    report = aggregate_evaluation(manifest, root)
    report_raw = _encoded(report)
    report_entry = fresh["inventory"]["aggregate-report.json"]
    if len(report_raw) != report_entry["size"] or digest(report_raw) != report_entry["sha256"]:
        raise ValueError("aggregate changed during export")
    metadata = (("evaluation.json", manifest_raw), ("aggregate-report.json", report_raw),
                ("package.json", _encoded(_package_value(fresh))))
    if sum(len(raw) for _, raw in metadata) != remaining[0]:
        raise ValueError("export size differs from reviewed plan")
    for name, raw in metadata:
        _capacity(root, limits, remaining[0], remaining[1] + remaining[2] - remaining[0])
        with (stage / name).open("xb") as output:
            output.write(raw)
            output.flush()
            os.fsync(output.fileno())
        remaining[0] -= len(raw)
    if remaining[0] != 0:
        raise ValueError("export size differs from reviewed plan")
    verify_export(stage)
    if fresh != _preview_export(manifest_path, root, export_id, owner_id, reserve_new=False):
        raise ValueError("source or managed ownership changed while exporting")
    _guard(target, missing=True)
    if _exists(target):
        raise ValueError("export target appeared before publication")
    # Same project volume, new destination only. Windows rename refuses an existing target.
    stage.rename(_long_path(target))
    return verify_export(target)


def verify_export(package: Path) -> dict[str, Any]:
    """Check only packaged bytes; graph integrity is checked without deserialization."""
    package = package.absolute()
    display_path = str(package)
    package = _long_path(package)
    _guard(package)
    before = _byte_snapshot(package)
    value = _load(package / "package.json")
    if not isinstance(value, dict):
        raise ValueError("invalid export package metadata")  # noqa: TRY004 -- Rejected evidence uses ValueError.
    package_sha = value.get("package_sha256")
    if sha256(canonical({key: item for key, item in value.items() if key != "package_sha256"})) != package_sha:
        raise ValueError("export metadata hash differs")
    plan = _check_plan(value.get("plan"))
    if value != _package_value(plan):
        raise ValueError("export metadata differs from its reviewed binding")
    actual = {name: ({"kind": "directory"} if entry[4] == "directory" else
                     {"kind": "file", "size": entry[0], "sha256": entry[4]})
              for name, entry in before.items() if name not in {".", "package.json"}}
    if actual != plan["inventory"]:
        raise ValueError("export contains altered, missing or undeclared entries")
    if sum(entry[0] for entry in before.values()) != plan["estimated_bytes"]:
        raise ValueError("export size differs from reviewed plan")
    raw = _read_bytes(package / "evaluation.json", MAX_MANIFEST_BYTES)
    if digest(raw) != value["manifest_sha256"]:
        raise ValueError("export manifest hash differs")
    manifest = parse_evaluation_manifest(_decode(raw))
    payload = package / "payload"
    expected_roots = {task.task_path for task in manifest.tasks} | {
        cell.work_path for cell in manifest.runs if cell.work_path is not None}
    if set(plan["source_snapshot"]) != expected_roots:
        raise ValueError("export input inventory differs from the original matrix")
    for path in _input_paths(manifest, payload):
        if (plan["source_snapshot"][path.relative_to(payload).as_posix()] is None) == _exists(path):
            raise ValueError("missing evidence state differs from reviewed inventory")
        if _exists(path):
            allowed = _TASK_ENTRIES if path.relative_to(payload).as_posix() in {
                task.task_path for task in manifest.tasks} else _WORK_ENTRIES
            _validate_tree(path, allowed, plan["estimated_bytes"])
    report = aggregate_evaluation(manifest, payload)
    if report != _load(package / "aggregate-report.json") or report["report_sha256"] != value["aggregate_sha256"]:
        raise ValueError("exported evidence does not reconstruct its aggregate")
    if before != _byte_snapshot(package):
        raise ValueError("export changed during read-only verification")
    return {"schema_version": 1, "scope": "offline_synthetic", "status": "verified",
            "export_id": value["export_id"], "owner_id": value["owner_id"], "plan_id": value["plan_id"],
            "package_path": display_path, "package_sha256": package_sha,
            "aggregate_sha256": value["aggregate_sha256"], "estimated_bytes": plan["estimated_bytes"],
            "privacy": _PRIVACY}
