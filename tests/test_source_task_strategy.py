"""Source replay and unexecuted proposals have explicit, frozen model context."""

from __future__ import annotations

import json

import pytest

from dfx_agent.policies import PolicyFeatures
from dfx_agent.task import digest
from dfx_agent.task_strategy import build_system_prompt, build_task_message

SOURCE = digest(b"source-snapshot")
CAPABILITIES = frozenset({"read_log", "read_source", "edit_source", "verify_candidate"})


def task_context(**changes: object) -> str:
    arguments: dict[str, object] = {
        "task_id": "source-task", "run_id": "source-run", "source_snapshot_sha256": SOURCE,
        "capabilities": CAPABILITIES, "resume": False,
    }
    arguments.update(changes)
    return build_task_message(**arguments)  # type: ignore[arg-type]


def test_legacy_task_bytes_do_not_acquire_new_scope_fields() -> None:
    expected = {"task_id": "source-task", "run_id": "source-run", "source_snapshot_sha256": SOURCE,
        "capabilities": sorted(CAPABILITIES), "resume": False, "verification_level": "offline_synthetic",
        "application_verification": "not_run", "request": (
            "Analyze the authorized snapshot and crash log, locate the cause, prepare a behavior-preserving candidate, "
            "and request the available host-fixed check.")}
    assert task_context() == json.dumps(expected, ensure_ascii=False, sort_keys=True)
    assert task_context(source_checks=False, analysis_only=False) == task_context()
    old_prompt = build_system_prompt(PolicyFeatures(), application_checks=True)
    assert old_prompt == build_system_prompt(PolicyFeatures(), application_checks=True,
                                             source_checks=False, analysis_only=False)
    assert "host-fixed local checks build the full project" in old_prompt


def test_source_prompt_and_startup_freeze_scope_without_device_requirement() -> None:
    features = PolicyFeatures(obligation_tracking=True)
    prompt = build_system_prompt(features, source_checks=True, require_candidate_diagnosis=True)
    assert "source_execution" in prompt and "declared validation_scope" in prompt
    assert "do not fabricate compiler" not in prompt
    assert "current offline check supplies synthetic evidence only" not in prompt
    assert "observation is real within its recorded source-execution scope" in prompt
    assert "does not establish full application" in prompt
    assert "Do not request a device, SDK, signing or emulator" in prompt
    assert "requires complete localization before candidate freezing" in prompt
    message = json.loads(task_context(source_checks=True, validation_scope="typed-array target and boundaries",
                                     mocked_interfaces=["audio I/O"]))
    assert message["verification_level"] == "source_execution"
    assert message["source_verification"] == message["application_verification"] == "not_run"
    assert message["validation_scope"] == "typed-array target and boundaries"
    assert message["mocked_interfaces"] == ["audio I/O"]
    assert task_context(source_checks=True, validation_scope="different boundary", mocked_interfaces=[]) != (
        task_context(source_checks=True, validation_scope="typed-array target and boundaries",
                     mocked_interfaces=["audio I/O"]))


def test_analysis_only_model_receives_no_execution_obligation() -> None:
    prompt = build_system_prompt(PolicyFeatures(), analysis_only=True)
    assert "No executable verification is configured" in prompt
    assert "do not request or repeatedly call run_check" in prompt
    assert "Verification remains not_run" in prompt and "unverified proposal" in prompt
    assert "current offline check supplies synthetic evidence only" not in prompt
    for resumed in (False, True):
        message = json.loads(task_context(analysis_only=True, resume=resumed))
        assert message["verification_level"] == "not_run"
        assert message["validation_scope"] == "analysis_only"
        assert message["source_verification"] == message["application_verification"] == "not_run"
        assert "return an unverified proposal without waiting for runtime access" in message["request"]
        assert "request the available host-fixed check" not in message["request"]
        assert "mocked_interfaces" not in message


@pytest.mark.parametrize("modes", [
    {"application_checks": True, "source_checks": True},
    {"source_checks": True, "analysis_only": True},
    {"application_checks": True, "analysis_only": True},
])
def test_incompatible_modes_cannot_disguise_verification_scope(modes: dict[str, bool]) -> None:
    with pytest.raises(ValueError, match="mutually exclusive"):
        build_system_prompt(PolicyFeatures(), **modes)
    with pytest.raises(ValueError, match="mutually exclusive"):
        task_context(**modes)


@pytest.mark.parametrize("scope,mocks", [(None, []), (" ", []), ("local replay", None)])
def test_source_startup_cannot_omit_declared_scope(scope: str | None, mocks: list[str] | None) -> None:
    with pytest.raises(ValueError, match="requires validation scope"):
        task_context(source_checks=True, validation_scope=scope, mocked_interfaces=mocks)
