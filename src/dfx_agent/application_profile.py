"""Explicit application prerequisites and a read-only deployment preflight.

Platform SDK, device pairing and signing are operator-provisioned once. The
Agent's existing protected executor can then prepare each project copy, build,
replay and run independent checks. This module never installs tools or probes
devices, and file existence must not be reported as successful application use.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .contracts import require_hash
from .local_execution import STAGES, LocalCheckConfig, _checked
from .task import digest


class ApplicationPrerequisite(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    name: str = Field(min_length=1, max_length=128)
    path: str = Field(min_length=1, max_length=32768)
    kind: Literal["file", "directory"] = "file"
    purpose: str = Field(min_length=1, max_length=512)


class IndependentOracle(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    path: str = Field(min_length=1, max_length=32768)
    sha256: str
    max_bytes: int = Field(default=1048576, ge=1, le=16777216)

    @model_validator(mode="after")
    def valid_hash(self) -> IndependentOracle:
        require_hash(self.sha256)
        return self


class ApplicationProfile(BaseModel):
    """Host-owned input, never an Agent-generated acceptance protocol.

    Commands contain native executable argv; use node.exe plus an explicit
    JavaScript entry for tooling distributed as .cmd wrappers. Dependency
    preparation belongs to LocalCheckConfig.preparation, not to an SDK install.
    Independent oracle files must remain outside the writable project tree.
    """

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    schema_version: Literal[1] = 1
    platform: Literal["harmonyos"] = "harmonyos"
    profile_id: str = Field(min_length=1, max_length=128)
    execution: LocalCheckConfig
    prerequisites: list[ApplicationPrerequisite] = Field(min_length=1, max_length=32)
    independent_oracles: dict[str, IndependentOracle]
    device_id: str = Field(min_length=1, max_length=256)
    device_kind: Literal["physical", "emulator"] = "physical"
    physical_device_required: bool = True
    signing_paths: list[str] = Field(default_factory=list, max_length=16)

    @model_validator(mode="after")
    def valid_profile(self) -> ApplicationProfile:
        if self.execution.mode != "application":
            raise ValueError("application profiles require application-mode checks")
        if set(self.independent_oracles) != set(STAGES):
            raise ValueError("independent oracle files are required for all four check stages")
        if any(self.execution.commands[stage].oracle_sha256 != oracle.sha256
               for stage, oracle in self.independent_oracles.items()):
            raise ValueError("oracle file and execution command hashes disagree")
        names = [item.name.casefold() for item in self.prerequisites]
        if len(names) != len(set(names)):
            raise ValueError("prerequisite names must be unique")
        return self


def load_application_profile(path: Path) -> ApplicationProfile:
    """Read an explicit profile without resolving relative paths through cwd."""
    return ApplicationProfile.model_validate_json(_checked(path).read_bytes())


def application_preflight(profile: ApplicationProfile) -> dict[str, Any]:
    """Return actionable missing conditions without spawning or changing files.

    ``configured`` only says the declared local files are accessible and match
    their fixed protocol. Device availability, SDK versions, signing validity,
    buildability and business behavior always remain ``not_probed`` here.
    """
    issues: list[dict[str, str]] = []

    def issue(code: str, item: str, message: str, action: str) -> None:
        issues.append({"code": code, "item": item, "message": message, "action": action})

    def required_path(value: str, kind: str, item: str) -> Path | None:
        path = Path(value)
        if not path.is_absolute():
            issue("absolute_path_required", item, "A configured path is relative or still a placeholder.",
                  "Provide an absolute path from the installed platform toolchain or approved check package.")
            return None
        try:
            actual = _checked(path)
            if (kind == "file" and not actual.is_file()) or (kind == "directory" and not actual.is_dir()):
                raise ValueError("configured path has the wrong entry type")
        except (OSError, ValueError) as exc:
            issue("path_unavailable", item, str(exc), "Provision this prerequisite, then rerun the read-only preflight.")
            return None
        return actual

    project = required_path(profile.execution.project_root, "directory", "project_root")
    if project is not None:
        for marker in ("build-profile.json5", "oh-package.json5", "hvigorfile.ts"):
            try:
                present = _checked(project / marker).is_file()
            except (OSError, ValueError):
                present = False
            if not present:
                issue("project_file_missing", marker, "The full HarmonyOS project is missing a required build file.",
                      "Select the complete application project, rather than the analysis-only source snapshot.")
    for prerequisite in profile.prerequisites:
        required_path(prerequisite.path, prerequisite.kind, prerequisite.name)
    commands = {**profile.execution.commands}
    if profile.execution.preparation is not None:
        commands["prepare"] = profile.execution.preparation
    for stage, command in commands.items():
        if any(argument.startswith("SET_") for argument in command.argv[1:]):
            issue("command_arguments_required", stage, "The command still contains unreviewed argument placeholders.",
                  "Provide reviewed arguments, including project-scoped cache configuration for dependency/build tools.")
        executable = required_path(command.argv[0], "file", stage + ".executable")
        if executable is not None and executable.suffix.casefold() in (".cmd", ".bat", ".ps1"):
            issue("native_executable_required", stage, "The protected application executor does not launch shell wrappers.",
                  "Use an explicit node.exe + JavaScript entry argv, or another reviewed native executable.")
    for stage, oracle in profile.independent_oracles.items():
        actual = required_path(oracle.path, "file", stage + ".oracle")
        if actual is None:
            continue
        if project is not None and actual.is_relative_to(project):
            issue("oracle_inside_project", stage, "The independent check definition is inside the writable project.",
                  "Place the host-owned oracle in a separate approved check package.")
            continue
        try:
            if actual.stat().st_size > oracle.max_bytes:
                raise ValueError("oracle file exceeds its bounded read size")
            with actual.open("rb") as stream:
                content = stream.read(oracle.max_bytes + 1)
            if len(content) > oracle.max_bytes or digest(content) != oracle.sha256:
                raise ValueError("oracle bytes differ from the approved hash")
        except (OSError, ValueError) as exc:
            issue("oracle_unavailable", stage, str(exc), "Review the independent check file and update its host-owned protocol.")
    for index, path in enumerate(profile.signing_paths):
        required_path(path, "file", f"signing_file_{index + 1}")
    if profile.device_id.startswith("SET_"):
        issue("device_id_required", "device", "A device serial has not been configured.",
              "Pair the approved device and configure its explicit serial before replay.")
    if profile.physical_device_required and profile.device_kind != "physical":
        issue("physical_device_required", "device", "This application requires a physical HarmonyOS device.",
              "Provide a paired supported phone; do not substitute an emulator for this case.")
    return {"schema_version": 1, "profile_id": profile.profile_id,
            "status": "configured" if not issues else "blocked", "issues": issues,
            "capabilities": {"project_dependency_preparation": profile.execution.preparation is not None,
                             "build": True, "target": True, "behavior": True, "regression": True},
            "observations": {"device_connection": "not_probed", "sdk_compatibility": "not_probed",
                             "signing_validity": "not_probed", "application_verification": "not_run"},
            "next_action": ("Run the approved commands through the managed application executor once experiments are enabled."
                            if not issues else "Resolve the listed prerequisites before starting application execution.")}
