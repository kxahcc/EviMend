"""Preflight verifies declared local conditions; it never probes a real app."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from dfx_agent.application_profile import (
    ApplicationProfile,
    application_preflight,
    load_application_profile,
)
from dfx_agent.local_execution import STAGES
from dfx_agent.task import digest


def profile(tmp_path: Path) -> ApplicationProfile:
    project = tmp_path / "full-project"
    project.mkdir()
    for name in ("build-profile.json5", "oh-package.json5", "hvigorfile.ts"):
        (project / name).write_text("{}", encoding="utf-8")
    sdk = tmp_path / "sdk"
    sdk.mkdir()
    oracle = tmp_path / "approved-check.py"
    oracle.write_bytes(b"independent host check\n")
    signing = tmp_path / "signing.profile"
    signing.write_bytes(b"host signing material; existence is not validity")
    sha256 = digest(oracle.read_bytes())
    return ApplicationProfile.model_validate({
        "profile_id": "offline-profile",
        "execution": {"mode": "application", "project_root": str(project),
                      "preparation": {"argv": [sys.executable, "prepare.py"]},
                      "commands": {stage: {"argv": [sys.executable, str(oracle), stage],
                                           "oracle_sha256": sha256} for stage in STAGES},
                      "baseline_target_exit_codes": [9], "baseline_fault_pattern": "OriginalFault"},
        "prerequisites": [{"name": "sdk", "path": str(sdk), "kind": "directory", "purpose": "SDK directory"}],
        "independent_oracles": {stage: {"path": str(oracle), "sha256": sha256} for stage in STAGES},
        "device_id": "explicit-paired-device", "signing_paths": [str(signing)]})


def changed(original: ApplicationProfile, **values: Any) -> ApplicationProfile:
    return ApplicationProfile.model_validate({**original.model_dump(), **values})


def test_configured_preflight_is_read_only_and_not_application_acceptance(tmp_path: Path,
                                                                       monkeypatch: pytest.MonkeyPatch) -> None:
    configured = profile(tmp_path)
    before = {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    monkeypatch.setattr(subprocess, "Popen", lambda *_args, **_kwargs: pytest.fail("preflight spawned a process"))
    result = application_preflight(configured)
    assert result["status"] == "configured" and result["issues"] == []
    assert result["capabilities"]["project_dependency_preparation"] is True
    assert result["observations"] == {"device_connection": "not_probed", "sdk_compatibility": "not_probed",
                                      "signing_validity": "not_probed", "application_verification": "not_run"}
    assert before == {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}


def test_missing_sdk_signing_and_project_files_are_actionable(tmp_path: Path) -> None:
    original = profile(tmp_path)
    (Path(original.execution.project_root) / "hvigorfile.ts").unlink()
    Path(original.signing_paths[0]).unlink()
    result = application_preflight(changed(original, prerequisites=[{
        "name": "sdk", "path": str(tmp_path / "missing-sdk"), "kind": "directory", "purpose": "SDK"}]))
    assert result["status"] == "blocked"
    assert {row["item"] for row in result["issues"]} == {"sdk", "signing_file_1", "hvigorfile.ts"}
    assert all(row["action"] for row in result["issues"])


def test_all_four_independent_oracles_and_matching_hashes_are_required(tmp_path: Path) -> None:
    original = profile(tmp_path)
    value = original.model_dump()
    del value["independent_oracles"]["regression"]
    with pytest.raises(ValidationError, match="all four"):
        ApplicationProfile.model_validate(value)
    value = original.model_dump()
    value["independent_oracles"]["target"]["sha256"] = "1" * 64
    with pytest.raises(ValidationError, match="hashes disagree"):
        ApplicationProfile.model_validate(value)


def test_oracle_change_is_not_silently_reapproved(tmp_path: Path) -> None:
    original = profile(tmp_path)
    Path(original.independent_oracles["target"].path).write_bytes(b"changed check")
    result = application_preflight(original)
    assert result["status"] == "blocked"
    assert {row["code"] for row in result["issues"]} == {"oracle_unavailable"}


def test_bound_execution_retains_the_reviewed_oracle_files(tmp_path: Path) -> None:
    original = profile(tmp_path)
    assert original.execution.independent_oracles is None
    execution = original.bound_execution()
    assert execution.independent_oracles == original.independent_oracles
    assert execution.model_dump()["independent_oracles"] == original.model_dump()["independent_oracles"]
    # Changing an external file does not silently update the frozen digest.
    oracle = original.independent_oracles["target"]
    Path(oracle.path).write_bytes(b"replacement acceptance check")
    assert execution.independent_oracles is not None
    assert execution.independent_oracles["target"].sha256 == oracle.sha256
    assert digest(Path(oracle.path).read_bytes()) != oracle.sha256


def test_profile_cannot_supply_conflicting_execution_oracle_bindings(tmp_path: Path) -> None:
    original = profile(tmp_path)
    execution = original.bound_execution().model_dump()
    execution["independent_oracles"]["target"]["path"] = str(tmp_path / "other-check.py")
    with pytest.raises(ValidationError, match="bindings differ"):
        changed(original, execution=execution)


def test_oracle_in_writable_project_is_rejected(tmp_path: Path) -> None:
    original = profile(tmp_path)
    oracle = Path(original.execution.project_root) / "self-assertion.json"
    oracle.write_bytes(b"independent host check\n")
    rows = original.model_dump()["independent_oracles"]
    rows["behavior"]["path"] = str(oracle)
    result = application_preflight(changed(original, independent_oracles=rows))
    assert result["status"] == "blocked"
    assert result["issues"][0]["code"] == "oracle_inside_project"


def test_shell_wrapper_needs_explicit_native_tool_argv(tmp_path: Path) -> None:
    original = profile(tmp_path)
    wrapper = tmp_path / "hvigor.cmd"
    wrapper.write_bytes(b"@echo not executed\r\n")
    execution = original.execution.model_dump()
    execution["commands"]["build"]["argv"][0] = str(wrapper)
    result = application_preflight(changed(original, execution=execution))
    assert result["status"] == "blocked"
    issue = result["issues"][0]
    assert issue["code"] == "native_executable_required" and "node.exe" in issue["action"]


def test_physical_only_case_rejects_emulator(tmp_path: Path) -> None:
    original = profile(tmp_path)
    result = application_preflight(changed(original, device_kind="emulator"))
    assert result["status"] == "blocked" and result["issues"][0]["code"] == "physical_device_required"


def test_relative_executable_is_not_resolved_from_developer_path(tmp_path: Path) -> None:
    original = profile(tmp_path)
    execution = original.execution.model_dump()
    execution["commands"]["build"]["argv"][0] = "node.exe"
    result = application_preflight(changed(original, execution=execution))
    assert result["status"] == "blocked" and result["issues"][0]["code"] == "absolute_path_required"


def test_template_loads_and_lists_missing_conditions_without_installing() -> None:
    template = Path(__file__).resolve().parents[1] / "configs/harmonyos-app.template.json"
    result = application_preflight(load_application_profile(template))
    assert result["status"] == "blocked" and len(result["issues"]) > 10
    assert any(row["code"] == "device_id_required" for row in result["issues"])


def test_profile_loading_does_not_interpret_arbitrary_extra_fields(tmp_path: Path) -> None:
    original = profile(tmp_path)
    filename = tmp_path / "profile.json"
    value = original.model_dump()
    value["install_sdk"] = True
    filename.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(ValidationError, match="Extra inputs"):
        load_application_profile(filename)
