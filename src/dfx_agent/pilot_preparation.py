"""Prepare one reviewed source/log snapshot and admit its original replay only."""

from __future__ import annotations

import json
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .evidence_io import local_evidence_path
from .local_execution import (
    STAGES,
    IndependentOracle,
    LocalCheckConfig,
    LocalCommand,
    _checked,
)
from .task import (
    CaseSelection,
    digest,
    prepare_task,
    require_relative_reference,
    verify_task,
)
from .verification import _atomic_json

MAX_CONFIG_BYTES = 1048576
MAX_OUTPUT_BYTES = 65536
SOURCE = "commons/lib_foundation/src/main/ets/http/OrderRequestChecksum.ets"
ORACLE = "resources/source-validation/household-checksum/oracle.cjs"
MARKER = "DFX022_TARGET_RANGE_ERROR"
SCOPE = (
    "Execute actual OrderRequestChecksum.create/verify after Node stripTypeScriptTypes, "
    "erased IRequest interface import and local export binding; fixed ASCII/CJK/emoji "
    "requests, tampering and repeated/frozen inputs; no PRE_ORDER, UI, device or server coverage"
)
REVIEWED_ORACLES = {
    ORACLE: (SOURCE, MARKER, SCOPE),
    "resources/source-validation/tourist-phone/oracle.cjs": (
        "components/module_base/src/main/ets/util/CommonUtil.ets",
        "DFX016_TARGET_REGEX_SYNTAX_ERROR",
        ("Execute actual module_base CommonUtil after Node stripTypeScriptTypes; "
         "fixed phone validation, padWithZeros and CardNo cases; no UI or caller-substitution coverage"),
    ),
}


def _read(path: Path, cap: int) -> bytes:
    source = _checked(path)
    io_source = local_evidence_path(source)
    if not io_source.is_file() or io_source.stat().st_size > cap:
        raise ValueError("preparation input is missing or oversized")
    with io_source.open("rb") as stream:
        content = stream.read(cap + 1)
    if len(content) > cap:
        raise ValueError("preparation input is oversized")
    return content


def _owned(root: Path, value: str, owner: str, *, exists: bool = False) -> Path:
    require_relative_reference(value)
    result = _checked(root / value, exists=exists)
    boundary = _checked(root / owner)
    if result == boundary or not result.is_relative_to(boundary):
        raise ValueError("preparation path is outside its declared owner")
    return result


def _write_new(path: Path, value: dict[str, Any]) -> None:
    _checked(path, exists=False)
    local_evidence_path(path.parent).mkdir(parents=True, exist_ok=True)
    with local_evidence_path(path).open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(value, ensure_ascii=True, indent=2) + "\n")


def _baseline_stage(node: Path, oracle: Path, project: Path, stage: str) -> dict[str, Any]:
    # This is the fixed host oracle and original reviewed source, not a model
    # command or candidate. It inherits the verified managed Job. NODE_OPTIONS
    # and model credentials are not inherited into the native runtime.
    environment = {name: value for name, value in os.environ.items()
                   if name.casefold() in {"systemroot", "windir", "temp", "tmp", "path"}}
    try:
        completed = subprocess.run([str(node), str(oracle), stage], cwd=project,
            env=environment, stdin=subprocess.DEVNULL, capture_output=True,
            timeout=10, check=False, shell=False)
    except subprocess.TimeoutExpired:
        raise ValueError("original replay exceeded its ten-second limit") from None
    if len(completed.stdout) > MAX_OUTPUT_BYTES or len(completed.stderr) > MAX_OUTPUT_BYTES:
        raise ValueError("original replay exceeded its output limit")
    return {"stage": stage, "exit_code": completed.returncode,
        "stdout": completed.stdout.decode("utf-8", errors="replace"),
        "stderr": completed.stderr.decode("utf-8", errors="replace"),
        "stdout_sha256": digest(completed.stdout), "stderr_sha256": digest(completed.stderr)}


def run_preparation(root: Path, config_path: Path | str) -> dict[str, Any]:
    from .managed_runtime import require_managed_worker

    root = _checked(root)
    require_managed_worker(root, "prepare")
    config = Path(config_path)
    config = _checked(config if config.is_absolute() else root / config)
    state = _checked(root / ".dfx-local/state")
    if not config.is_relative_to(state) or config.suffix != ".json":
        raise ValueError("preparation configuration must be a host state JSON")
    raw = json.loads(_read(config, MAX_CONFIG_BYTES).decode("utf-8-sig"))
    expected_fields = {"case", "data_root", "output", "validation_profile", "node", "oracle", "record"}
    if not isinstance(raw, dict) or set(raw) != expected_fields:
        raise ValueError("preparation configuration has unexpected fields")
    case = CaseSelection.model_validate(raw["case"])
    if any(not isinstance(raw[key], str) for key in expected_fields - {"case"}):
        raise ValueError("preparation paths must be strings")
    require_relative_reference(raw["data_root"])
    if raw["data_root"] != "data_B":
        raise ValueError("pilot preparation only admits the reviewed data_B root")
    data = _checked(root / raw["data_root"])
    if not data.is_relative_to(root) or not data.is_dir():
        raise ValueError("dataset must remain inside the workspace")
    output = _owned(root, raw["output"], ".dfx-local/tmp")
    profile_path = _owned(root, raw["validation_profile"], ".dfx-local/state")
    record_path = _owned(root, raw["record"], ".dfx-local/state")
    if (profile_path.suffix != ".json" or record_path.suffix != ".json" or
            len({config, profile_path, record_path}) != 3 or
            any(local_evidence_path(path).exists() for path in (output, profile_path, record_path))):
        raise ValueError("preparation refuses existing or overlapping outputs")
    if output.is_relative_to(data) or data.is_relative_to(output):
        raise ValueError("preparation output overlaps dataset")
    if raw["oracle"] not in REVIEWED_ORACLES:
        raise ValueError("only reviewed source oracles are admitted")
    selected_source, selected_marker, selected_scope = REVIEWED_ORACLES[raw["oracle"]]
    oracle = _checked(root / raw["oracle"])
    oracle_sha = digest(_read(oracle, MAX_CONFIG_BYTES))
    node = Path(raw["node"])
    if not node.is_absolute():
        raise ValueError("Node must be an absolute native executable")
    node = _checked(node)
    if not node.is_file() or node.suffix.casefold() != ".exe" or ".codex" in {
            part.casefold() for part in node.parts}:
        raise ValueError("Node must be an existing ordinary native executable")
    record: dict[str, Any] = {"schema_version": 1, "state": "pending", "status": "preparing",
        "observed_at": datetime.now(UTC).isoformat(), "case_identity_sha256": case.sha256(),
        "task_id": case.task_id, "task": str(output), "validation_profile": str(profile_path),
        "oracle": str(oracle), "oracle_sha256": oracle_sha, "node": str(node),
        "validation_scope": selected_scope, "mocked_interfaces": [], "stages": [],
        "source_verification": "not_run", "application_verification": "not_run",
        "repair_success": None, "candidate_created": False, "model_calls": 0}
    _write_new(record_path, record)
    try:
        snapshot = prepare_task(data, case.model_dump(exclude_none=True), output)
        task = verify_task(output)
        source = output / "source"
        source_sha = digest(_read(source / selected_source, MAX_CONFIG_BYTES))
        record.update(source_snapshot_sha256=snapshot["source_snapshot_sha256"],
            source_files=snapshot["source_files"], original_source_sha256=source_sha,
            source_log_alignment=task["source_log_alignment"])
        for stage in ("build", "target"):
            verify_task(output)
            if digest(_read(oracle, MAX_CONFIG_BYTES)) != oracle_sha:
                raise ValueError("oracle changed before original replay")
            observed = _baseline_stage(node, oracle, source, stage)
            record["stages"].append(observed)
            _atomic_json(record_path, record)
            verify_task(output)
            if digest(_read(oracle, MAX_CONFIG_BYTES)) != oracle_sha:
                raise ValueError("oracle changed during original replay")
            try:
                header = json.loads(observed["stdout"].splitlines()[0])
            except (IndexError, TypeError, json.JSONDecodeError):
                raise ValueError("original replay did not identify its source bytes") from None
            if (not isinstance(header, dict) or header.get("stage") != stage or
                    header.get("source_sha256") != source_sha):
                raise ValueError("original replay source identity differs")
            if stage == "build" and observed["exit_code"] != 0:
                raise ValueError("original source could not be loaded")
            if stage == "target" and (observed["exit_code"] != 9 or
                    selected_marker not in observed["stdout"] + observed["stderr"]):
                raise ValueError("original did not reproduce the reviewed fault")
        profile = LocalCheckConfig(mode="source", project_root=str(source),
            validation_scope=selected_scope, mocked_interfaces=[], commands={stage: LocalCommand(
                argv=[str(node), str(oracle), stage], oracle_sha256=oracle_sha, timeout_seconds=10)
                for stage in STAGES}, independent_oracles={stage: IndependentOracle(
                    path=str(oracle), sha256=oracle_sha) for stage in STAGES},
            baseline_target_exit_codes=[9], baseline_fault_pattern=selected_marker, max_check_seconds=90)
        _write_new(profile_path, profile.model_dump(mode="json"))
        record.update(state="done", status="source_baseline_admitted", fault_reproduced=True,
            baseline_verification="reproduced", source_verification="not_run",
            profile_sha256=digest(_read(profile_path, MAX_CONFIG_BYTES)))
    except Exception as error:
        record.update(state="done", status="blocked", fault_reproduced=False,
            error_type=type(error).__name__, error_detail=str(error)[:1024])
        _checked(record_path)
        _atomic_json(record_path, record)
        raise
    _checked(record_path)
    _atomic_json(record_path, record)
    return record
