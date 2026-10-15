"""Read-only preflight for device-free source checks, without platform setup.

The author format is the existing LocalCheckConfig, not a second application
framework. Its commands execute the candidate source or a declared local
adapter; validation_scope and mocked_interfaces state what those checks cover.
Preflight checks fixed files, never runs a command or certifies the adapter's
semantic fidelity to a platform API.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .local_execution import STAGES, LocalCheckConfig, _checked
from .task import digest

MAX_SOURCE_PROFILE_BYTES = 4 * 1024 * 1024


def load_source_profile(path: Path) -> LocalCheckConfig:
    source = _checked(path)
    if not source.is_file() or source.stat().st_size > MAX_SOURCE_PROFILE_BYTES:
        raise ValueError("missing or oversized source validation profile")
    with source.open("rb") as stream:
        raw = stream.read(MAX_SOURCE_PROFILE_BYTES + 1)
    if len(raw) > MAX_SOURCE_PROFILE_BYTES:
        raise ValueError("oversized source validation profile")
    profile = LocalCheckConfig.model_validate_json(raw.decode("utf-8-sig"))
    if profile.mode != "source":
        raise ValueError("--validation requires a source-mode validation profile")
    return profile


def source_preflight(profile: LocalCheckConfig) -> dict[str, Any]:
    """Check a fixed local protocol without requiring HarmonyOS or a device."""
    if profile.mode != "source":
        raise ValueError("source preflight requires a source-mode validation profile")
    issues: list[dict[str, str]] = []

    def issue(code: str, item: str, message: str, action: str) -> None:
        issues.append({"code": code, "item": item, "message": message, "action": action})

    def required_path(value: str, kind: str, item: str) -> Path | None:
        path = Path(value)
        if not path.is_absolute():
            issue("absolute_path_required", item, "The declared path is relative or still a placeholder.",
                  "Provide an absolute path to the source project, local runtime or approved check file.")
            return None
        try:
            actual = _checked(path)
            if actual.is_dir() != (kind == "directory") or kind == "file" and not actual.is_file():
                raise ValueError("configured path has the wrong entry type")
        except (OSError, ValueError):
            issue("path_unavailable", item, "The declared path is unavailable or linked.",
                  "Correct this local path and rerun preflight.")
            return None
        return actual

    project = required_path(profile.project_root, "directory", "project_root")
    commands = {**profile.commands}
    if profile.preparation is not None:
        commands["prepare"] = profile.preparation
    for stage, command in commands.items():
        if any(argument.startswith("SET_") for argument in command.argv[1:]):
            issue("command_arguments_required", stage, "The command still contains argument placeholders.",
                  "Provide reviewed arguments that exercise the current project copy.")
        executable = required_path(command.argv[0], "file", stage + ".executable")
        if executable is not None and executable.suffix.casefold() in (".cmd", ".bat", ".ps1"):
            issue("native_executable_required", stage, "The protected executor does not launch shell wrappers.",
                  "Use an explicit Python, Node or another local native executable plus script arguments.")
    for stage, oracle in (profile.independent_oracles or {}).items():
        actual = required_path(oracle.path, "file", stage + ".oracle")
        if actual is None:
            continue
        if project is not None and actual.is_relative_to(project):
            issue("oracle_inside_project", stage, "The independent check is inside the writable source project.",
                  "Keep host-owned check files outside the source project and candidate run tree.")
            continue
        try:
            if actual.stat().st_size > oracle.max_bytes:
                raise ValueError("oversized oracle")
            with actual.open("rb") as stream:
                content = stream.read(oracle.max_bytes + 1)
            if len(content) > oracle.max_bytes or digest(content) != oracle.sha256:
                raise ValueError("changed oracle")
        except (OSError, ValueError):
            issue("oracle_unavailable", stage, "The independent check bytes differ or exceed their read limit.",
                  "Review the check file and update its host-owned protocol before a new run.")
    return {"schema_version": 1, "status": "configured" if not issues else "blocked", "issues": issues,
            "verification_scope": "source_execution", "validation_scope": profile.validation_scope,
            "mocked_interfaces": profile.mocked_interfaces,
            "capabilities": {stage: stage in profile.commands for stage in STAGES},
            "observations": {"source_execution": "not_probed", "source_verification": "not_run",
                             "application_verification": "not_run"},
            "next_action": ("Run the approved local source checks through the managed executor."
                            if not issues else "Resolve the listed local paths or check declarations before execution.")}
