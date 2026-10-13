"""Product command behavior without launching models or applications."""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

import pytest

from dfx_agent import product_cli as cli


def _event(sequence: int = 1) -> dict[str, Any]:
    return {"schema_version": 1, "sequence": sequence, "observed_at": "2026-10-08T00:00:00+00:00",
            "run_id": "sample", "event": "model_completed", "call_id": "call1"}


def _observation(events: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    values = [_event()] if events is None else events
    return {"tracking": "available", "events": values,
            "next_sequence": values[-1]["sequence"] if values else 0,
            "last_sequence": values[-1]["sequence"] if values else 0,
            "partial_line": False, "size_bytes": 100, "capacity_bytes": 131072}


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    work = tmp_path / ".dfx-local/work/competition-sample"
    work.mkdir(parents=True)
    config = tmp_path / "configs"
    config.mkdir()
    (tmp_path / "src").mkdir()
    (config / "competition-demo.json").write_text('{"model_mode":"mock_demo"}', encoding="utf-8")
    monkeypatch.setattr(cli, "_workspace", lambda _: tmp_path)
    return tmp_path


@pytest.mark.parametrize("flag", ["--help", "--version"])
def test_help_and_version_do_not_resolve_workspace(flag: str, monkeypatch: pytest.MonkeyPatch) -> None:
    def reject(_: Any) -> Path:
        pytest.fail("help/version must not access a deployment or start its backend")

    monkeypatch.setattr(cli, "_workspace", reject)
    with pytest.raises(SystemExit) as raised:
        cli.main([flag])
    assert raised.value.code == 0


@pytest.mark.parametrize("name", ["../sample", "", "-switch", "a/b", "a\\b", "a\nsecret", "a" * 65])
def test_launch_name_rejects_paths_and_option_injection(name: str) -> None:
    with pytest.raises(SystemExit) as raised:
        cli.parser().parse_args(["--demo", "--name", name])
    assert raised.value.code == 2


def test_demo_dispatches_explicit_configuration_without_launching_backend(
    workspace: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = []
    monkeypatch.setattr(cli, "_run", lambda *args: calls.append(args) or 2)
    assert cli.main(["--demo", "--name", "sample", "--output-format", "json"]) == 2
    assert calls == [(workspace, "", "configs/competition-demo.json", "sample", False, "json")]


def test_resume_uses_saved_inputs_and_rejects_replacement_profile(
    workspace: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = []
    monkeypatch.setattr(cli, "_resume_inputs", lambda root, name: ("reviewed-task", "saved-profile.json"))
    monkeypatch.setattr(cli, "_run", lambda *args: calls.append(args) or 0)
    assert cli.main(["--resume", "sample"]) == 0
    assert calls == [(workspace, "reviewed-task", "saved-profile.json", "sample", True, "text")]
    with pytest.raises(SystemExit) as raised:
        cli.main(["--resume", "sample", "--config", "new-profile.json"])
    assert raised.value.code == 2
    assert len(calls) == 1


def test_named_status_is_management_and_displays_latest_saved_event(
    workspace: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    def saved(_: Path, **options: Any) -> dict[str, Any]:
        events = [_event(number) for number in range(1, 102)]
        selected = events[:options.get("max_events", 100)]
        return {**_observation(selected), "last_sequence": 101}

    monkeypatch.setattr(cli, "read_progress", saved)
    monkeypatch.setattr(cli, "_active", lambda _: None)
    assert cli.main(["status", "sample", "--output-format", "json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["event"] == "status"
    assert result["name"] == "sample"
    assert result["latest_event"][-1]["sequence"] == 101


def test_named_report_does_not_treat_session_name_as_launch_flag(
    workspace: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(cli, "_summary", lambda root, name: {"event": "result", "name": name, "accepted": False})
    assert cli.main(["report", "sample", "--output-format", "json"]) == 0
    assert json.loads(capsys.readouterr().out) == {"event": "result", "name": "sample", "accepted": False}


def test_named_stop_uses_management_dispatch(
    workspace: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    calls = []
    monkeypatch.setattr(cli, "_stop", lambda root, name: calls.append((root, name)) or
                        {"name": name, "stopped": True, "active_run": False})
    assert cli.main(["stop", "sample", "--output-format", "json"]) == 0
    assert calls == [(workspace, "sample")]
    assert json.loads(capsys.readouterr().out)["stopped"] is True


def test_json_logs_produce_one_structured_observation(
    workspace: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(cli, "read_progress", lambda *args, **options: _observation())
    monkeypatch.setattr(cli, "_active", lambda _: None)
    assert cli.main(["logs", "sample", "--output-format", "json"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 1
    result = json.loads(lines[0])
    assert result["events"] == [_event()]
    assert result["tracking"] == "available"


def test_stream_logs_preserve_requested_cursor(
    workspace: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    cursors = []

    def saved(_: Path, **options: Any) -> dict[str, Any]:
        cursors.append(options["after_sequence"])
        return _observation([_event(5)])

    monkeypatch.setattr(cli, "read_progress", saved)
    monkeypatch.setattr(cli, "_active", lambda _: None)
    assert cli.main(["logs", "sample", "--after", "4", "--output-format", "stream-json"]) == 0
    assert cursors[0] == 4
    assert json.loads(capsys.readouterr().out)["sequence"] == 5


def test_launch_flags_cannot_be_combined_with_management(workspace: Path) -> None:
    with pytest.raises(SystemExit) as raised:
        cli.main(["--demo", "status", "sample"])
    assert raised.value.code == 2


def test_offline_environment_sync_stays_on_managed_backend(
    workspace: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    calls = []
    monkeypatch.setattr(cli, "_invoke", lambda root, args: calls.append((root, args)) or (0, {"status": "finished"}))
    assert cli.main(["--output-format", "json", "env", "sync", "--offline"]) == 0
    assert calls == [(workspace, ["Init", "-OfflineSync"])]
    assert json.loads(capsys.readouterr().out)["status"] == "finished"


def test_cleanup_requires_explicit_plan_before_apply(
    workspace: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = []
    monkeypatch.setattr(cli, "_invoke", lambda root, args: calls.append(args) or (0, {"status": "preview"}))
    assert cli.main(["env", "clean", "--scope", "cache"]) == 0
    assert calls == [["Clean", "-Scope", "cache"]]
    with pytest.raises(SystemExit) as raised:
        cli.main(["env", "clean", "--apply"])
    assert raised.value.code == 2
    assert len(calls) == 1


@pytest.mark.parametrize("active", [
    {"task": "test", "competition_work_id": "sample", "run_id": "original"},
    {"task": "competition", "competition_work_id": "another", "run_id": "original"},
    {"task": "competition", "competition_work_id": "sample", "run_id": "replacement"},
])
def test_exact_stop_refuses_other_tasks_or_replaced_manager(
    workspace: Path, monkeypatch: pytest.MonkeyPatch, active: dict[str, str],
) -> None:
    monkeypatch.setattr(cli, "_active", lambda _: active)
    monkeypatch.setattr(cli, "_invoke", lambda *args: pytest.fail("unrelated manager must not receive Stop"))
    with pytest.raises(ValueError):
        cli._stop(workspace, "sample", "original")


def test_stop_passes_original_manager_guid_to_guarded_backend(
    workspace: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = "00000000-0000-4000-8000-000000000001"
    calls = []
    monkeypatch.setattr(cli, "_active", lambda _: {"task": "competition", "competition_work_id": "sample", "run_id": manager})
    monkeypatch.setattr(cli, "_invoke", lambda root, args: calls.append(args) or
                        (0, {"stopped": True, "active_run": False}))
    assert cli._stop(workspace, "sample", manager)["stopped"] is True
    assert calls == [["Stop", "-ExpectedRunId", manager]]


def test_non_json_backend_error_does_not_expose_private_diagnostics(
    workspace: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    def failed(*args: Any) -> Any:
        raise ValueError("PRIVATE_API_TOKEN_and_source_body")

    monkeypatch.setattr(cli, "_invoke", failed)
    assert cli.main(["status", "--output-format", "json"]) == 1
    captured = capsys.readouterr()
    assert "PRIVATE_API_TOKEN" not in captured.out + captured.err
    assert json.loads(captured.out)["event"] == "error"


def test_capture_drains_oversized_output_without_retaining_it() -> None:
    capture = cli._Capture(io.BytesIO(b"x" * (cli._CAPTURE_BYTES + 4096)))
    with pytest.raises(ValueError):
        capture.finish()
    assert len(capture.data) == cli._CAPTURE_BYTES
    assert capture.overflow


@pytest.mark.parametrize("raw", [b"private non-json failure", b"[]", b"\xff"])
def test_capture_rejects_non_object_or_invalid_backend_output(raw: bytes) -> None:
    capture = cli._Capture(io.BytesIO(raw))
    with pytest.raises((ValueError, TypeError, UnicodeError)):
        capture.finish()


class _Process:
    def __init__(self, receipt: dict[str, Any]):
        self.pid = 4242
        self.stdout = io.BytesIO(json.dumps(receipt).encode())
        self.stderr = io.BytesIO(b"")
        self.returncode: int | None = None
        self.polls = 0

    def poll(self) -> int | None:
        self.polls += 1
        if self.polls > 2:
            self.returncode = 0
        return self.returncode


def test_interrupt_before_lease_publication_stops_once_when_own_lease_appears(
    workspace: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _Process({"status": "canceled"})
    manager = "00000000-0000-4000-8000-000000000001"
    calls = []
    sleep_calls = []
    observations = iter([None, {"manager_pid": process.pid, "competition_work_id": "sample",
                               "task": "competition", "run_id": manager}])

    def sleep(_: float) -> None:
        sleep_calls.append(True)
        if len(sleep_calls) == 1:
            raise KeyboardInterrupt

    monkeypatch.setattr(cli, "_backend", lambda *args: ["fixed-manager"])
    monkeypatch.setattr(cli.subprocess, "Popen", lambda *args, **options: process)
    monkeypatch.setattr(cli, "_active", lambda _: next(observations))
    monkeypatch.setattr(cli, "read_progress", lambda *args, **options: _observation([]))
    monkeypatch.setattr(cli.time, "sleep", sleep)
    monkeypatch.setattr(cli, "_stop", lambda root, name, expected: calls.append((name, expected)) or
                        {"stopped": True, "active_run": False})
    assert cli._run(workspace, "", "configs/competition-demo.json", "sample", False, "json") == 130
    assert calls == [("sample", manager)]


def test_json_run_emits_only_terminal_result_and_never_invokes_shell(
    workspace: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    process = _Process({"status": "finished"})
    launches = []

    def launch(arguments: list[str], **options: Any) -> _Process:
        launches.append((arguments, options))
        return process

    monkeypatch.setattr(cli, "_backend", lambda root, arguments: ["fixed-manager", *arguments])
    monkeypatch.setattr(cli.subprocess, "Popen", launch)
    monkeypatch.setattr(cli, "_active", lambda _: None)
    monkeypatch.setattr(cli, "read_progress", lambda *args, **options: _observation())
    monkeypatch.setattr(cli.time, "sleep", lambda _: None)
    monkeypatch.setattr(cli, "_summary", lambda root, name: {"event": "result", "name": name, "accepted": False})
    assert cli._run(workspace, "", "configs/competition-demo.json", "sample", False, "json") == 2
    assert json.loads(capsys.readouterr().out) == {"event": "result", "name": "sample", "accepted": False,
                                               "tracking": "available"}
    assert len(launches) == 1
    assert launches[0][1]["shell"] is False
    assert "-CompetitionWorkId" in launches[0][0]


@pytest.fixture
def application_template(workspace: Path) -> Path:
    source = Path(__file__).parents[1] / "configs/harmonyos-app.template.json"
    target = workspace / "configs/application.json"
    target.write_bytes(source.read_bytes())
    return target


def _configure_arguments(destination: str = "configs/cloud.json", *options: str) -> list[str]:
    return ["--output-format", "json", "configure", "--provider", "deepseek", "--model", "chosen-model",
            "--application", "configs/application.json", "--save", destination, *options]


def _reject_external_actions(monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx

    def reject(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("configuration/preflight must not start a process or send a network request")

    monkeypatch.setattr(cli, "_invoke", reject)
    # Keep Popen's type intact: asyncio and SDK modules may import it lazily
    # for runtime typing/subclass declarations. Only actual construction fails.
    monkeypatch.setattr(cli.subprocess.Popen, "__init__", reject)
    monkeypatch.setattr(httpx.Client, "request", reject)
    monkeypatch.setattr(httpx.Client, "send", reject)
    monkeypatch.setattr(httpx.AsyncClient, "request", reject)
    monkeypatch.setattr(httpx.AsyncClient, "send", reject)


def test_cloud_configuration_saves_unready_application_without_network_or_reading_key(
    workspace: Path, application_template: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _reject_external_actions(monkeypatch)
    private = "PRIVATE_SECRET_NEVER_READ_OR_SAVED"
    environment_name = "DFX_TEST_PROVIDER_SECRET"
    monkeypatch.setenv(environment_name, private)
    original = type(cli.os.environ).__getitem__

    def guarded_key(environment: Any, key: str) -> str:
        if key == environment_name:
            pytest.fail("configure must save only the credential environment name")
        return original(environment, key)

    monkeypatch.setattr(type(cli.os.environ), "__getitem__", guarded_key)
    original_application = application_template.read_bytes()
    assert cli.main(_configure_arguments("configs/cloud.json", "--key-env", environment_name)) == 0
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["event"] == "configuration_created"
    assert receipt["application_prerequisites"] == "blocked"
    assert receipt["model_verification"] == "not_run"
    assert receipt["experiments_enabled"] is False
    assert receipt["credential_env"] == environment_name
    saved_bytes = (workspace / "configs/cloud.json").read_bytes()
    assert private.encode() not in saved_bytes
    profile = json.loads(saved_bytes)
    assert profile["provider"] == {"endpoint": "https://api.deepseek.com/chat/completions",
                                   "credential_env": environment_name}
    assert profile["model"]["thinking_mode"] == "disabled"
    assert profile["working_context"]["enabled"] is True
    assert profile["working_context"]["history_selection"] == "repair_frontier"
    assert profile["working_context"]["context_layout"] == "decision_frontier"
    assert profile["execution"]["project_root"] == "SET_ABSOLUTE_FULL_PROJECT_ROOT"
    assert application_template.read_bytes() == original_application


def test_explicit_deepseek_thinking_keeps_full_history_without_working_context_selection(
    workspace: Path, application_template: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _reject_external_actions(monkeypatch)
    assert cli.main(_configure_arguments("configs/thinking.json", "--thinking", "enabled")) == 0
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["model_verification"] == "not_run"
    saved = json.loads((workspace / "configs/thinking.json").read_bytes())
    assert saved["model"]["thinking_mode"] == "enabled"
    assert saved["working_context"]["enabled"] is False


@pytest.mark.parametrize("destination", ["data_B/cloud.json", "archive/cloud.json", ".git/cloud.json", "../outside.json"])
def test_cloud_configuration_refuses_original_data_archives_and_external_output(
    workspace: Path, application_template: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str], destination: str,
) -> None:
    _reject_external_actions(monkeypatch)
    target = workspace / destination
    target.parent.mkdir(parents=True, exist_ok=True)
    assert cli.main(_configure_arguments(destination)) == 1
    assert json.loads(capsys.readouterr().out)["event"] == "error"
    assert not target.exists()


def test_cloud_configuration_never_overwrites_reviewed_output(
    workspace: Path, application_template: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _reject_external_actions(monkeypatch)
    saved = workspace / "configs/cloud.json"
    original = b"previous approved configuration\n"
    saved.write_bytes(original)
    assert cli.main(_configure_arguments()) == 1
    assert json.loads(capsys.readouterr().out)["event"] == "error"
    assert saved.read_bytes() == original


def test_application_doctor_is_read_only_and_does_not_probe_devices_or_model(
    workspace: Path, application_template: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _reject_external_actions(monkeypatch)
    before = {str(path.relative_to(workspace)): path.read_bytes() for path in workspace.rglob("*") if path.is_file()}
    assert cli.main(["--output-format", "json", "doctor", "--application", "configs/application.json"]) == 1
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["event"] == "preflight"
    assert receipt["status"] == "blocked"
    assert receipt["observations"] == {"device_connection": "not_probed", "sdk_compatibility": "not_probed",
                                        "signing_validity": "not_probed", "application_verification": "not_run"}
    assert receipt["issues"]
    after = {str(path.relative_to(workspace)): path.read_bytes() for path in workspace.rglob("*") if path.is_file()}
    assert after == before


@pytest.mark.parametrize("scope", ["env", "all"])
@pytest.mark.parametrize("discard", [False, True])
def test_environment_deletion_is_rejected_before_invoke_and_points_to_external_setup(
    workspace: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    scope: str, discard: bool,
) -> None:
    monkeypatch.setattr(cli, "_invoke", lambda *args: pytest.fail("running environment deletion must not invoke a manager"))
    arguments = ["--output-format", "json", "env", "clean", "--scope", scope, "--apply", "--plan-id", "reviewed-plan"]
    if discard:
        arguments += ["--discard-evidence"]
    assert cli.main(arguments) == 1
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["event"] == "external_cleanup_required"
    assert receipt["scope"] == scope
    assert receipt["command"] == f"setup.cmd --clean --scope {scope} --apply --plan-id reviewed-plan" + (
        " --discard-evidence" if discard else "")
