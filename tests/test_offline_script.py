"""Strict CLI fixture loading and isolated mock HTTP response consumption."""

from __future__ import annotations

import json
import os
import sys
from dataclasses import FrozenInstanceError
from pathlib import Path

import httpx
import pytest
from langchain_core.messages import AIMessage, HumanMessage
from test_model_provider import descriptors, wire_response

from dfx_agent import cli
from dfx_agent.context_compaction import CompactionPolicy
from dfx_agent.model_provider import (
    MOCK_ENDPOINT,
    AdapterResponseError,
    AdapterTransportError,
)
from dfx_agent.offline_script import MAX_SCRIPT_BYTES, load_offline_script


def fixed_document() -> dict:
    return {"schema_version": 1, "responses": [
        {"tool": "read_source", "args": {"path": "src/main.ets"},
         "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}},
        {"tool": None, "args": {}, "usage": None}],
        "expected": {"path": "src/main.ets", "text": "synthetic expectation\n"}}


def mock_document() -> dict:
    return {"schema_version": 2, "model": {
        "mode": "mock_chat_completions", "settings": {
            "provider_id": "configured-provider", "model_id": "configured/model"},
        "responses": [wire_response()]},
        "expected": {"path": "src/main.ets", "text": "synthetic expectation\n"}}


def write(tmp_path: Path, value: dict, *, name: str = "offline.json") -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    return path


def test_schema_one_preserves_legacy_messages_and_synthetic_expectation(tmp_path: Path) -> None:
    script = load_offline_script(write(tmp_path, fixed_document()))
    assert script.mode == "fixed_response"
    messages = script.fixed_responses()
    assert all(isinstance(message, AIMessage) for message in messages)
    assert messages[0].content == ""
    assert messages[0].tool_calls == [{"name": "read_source", "args": {"path": "src/main.ets"},
                                      "id": "call-1", "type": "tool_call"}]
    assert messages[0].usage_metadata == {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}
    assert messages[1].content == "done" and messages[1].tool_calls == [] and messages[1].usage_metadata is None
    assert script.executor().path == "src/main.ets"
    assert script.executor().expected_text == "synthetic expectation\n"
    assert script.options() == {}
    assert script.features().model_dump() == {"rule_guidance": False, "obligation_tracking": False, "decision_checks": False}
    with pytest.raises(ValueError, match="mode"):
        script.adapter()


@pytest.mark.parametrize("document", [fixed_document, mock_document])
def test_limits_are_explicit_only_and_flags_are_shared(tmp_path: Path, document) -> None:
    value = document()
    value["limits"] = {"max_seconds": 2.5, "max_candidates": 1}
    value["policy_features"] = {"rule_guidance": True, "decision_checks": True}
    script = load_offline_script(write(tmp_path, value))
    assert script.options() == {"max_seconds": 2.5, "max_candidates": 1}
    assert script.features().rule_guidance is True
    assert script.features().obligation_tracking is False
    assert script.features().decision_checks is True


def test_schema_two_uses_one_mock_send_and_keeps_fixtures_out_of_context(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    value = mock_document()
    value["expected"]["text"] = "PRIVATE_SYNTHETIC_ORACLE"
    value["model"]["responses"] = [wire_response(content="FIRST_FIXTURE"), wire_response(content="FUTURE_FIXTURE")]
    script = load_offline_script(write(tmp_path, value))
    adapter = script.adapter()
    outgoing = []
    original_send = httpx.Client.send

    def send(self, request, **kwargs):
        outgoing.append(request)
        return original_send(self, request, **kwargs)

    monkeypatch.setattr(httpx.Client, "send", send)
    request = adapter.prepare([HumanMessage(content="Inspect source.")], descriptors())
    assert "PRIVATE_SYNTHETIC_ORACLE" not in request.body and "FUTURE_FIXTURE" not in request.body
    assert "responses" not in adapter.profile()
    assert adapter.invoke(request).content == "FIRST_FIXTURE"
    assert len(outgoing) == 1
    assert str(outgoing[0].url) == MOCK_ENDPOINT
    assert "authorization" not in outgoing[0].headers
    assert outgoing[0].content.decode("utf-8") == request.body
    assert adapter.invoke(request).content == "FUTURE_FIXTURE"
    assert len(outgoing) == 2
    with pytest.raises(AdapterTransportError, match="^mock_responses_exhausted$"):
        adapter.invoke(request)
    assert len(outgoing) == 3
    assert adapter.profile()["automatic_retries"] == 0
    assert script.mode == "mock_chat_completions"
    with pytest.raises(ValueError, match="mode"):
        script.fixed_responses()


def test_mock_streams_are_separate_and_mutating_returned_inputs_cannot_change_script(tmp_path: Path) -> None:
    value = mock_document()
    path = write(tmp_path, value)
    script = load_offline_script(path)
    value["model"]["responses"][0]["choices"][0]["message"]["content"] = "changed"
    value["expected"]["text"] = "changed"
    write(tmp_path, value)
    adapter1, adapter2 = script.adapter(), script.adapter()
    request1 = adapter1.prepare([HumanMessage(content="Inspect.")], [])
    request2 = adapter2.prepare([HumanMessage(content="Inspect.")], [])
    assert adapter1.invoke(request1).content == adapter2.invoke(request2).content == "done"
    assert script.executor().expected_text == "synthetic expectation\n"
    with pytest.raises(AdapterTransportError, match="mock_responses_exhausted"):
        adapter1.invoke(request1)
    assert script.adapter().invoke(script.adapter().prepare([HumanMessage(content="Inspect.")], [])).content == "done"
    with pytest.raises(FrozenInstanceError):
        script.mode = "fixed_response"


def test_host_supplied_resume_offset_skips_completed_replay_without_inspecting_messages(tmp_path: Path) -> None:
    value = mock_document()
    value["model"]["responses"] = [wire_response(content="COMPLETED"), wire_response(content="NEW_REQUEST")]
    script = load_offline_script(write(tmp_path, value))
    adapter = script.adapter(response_offset=1)
    request = adapter.prepare([HumanMessage(content="No turn count in this message.")], [])
    assert adapter.invoke(request).content == "NEW_REQUEST"
    with pytest.raises(AdapterTransportError, match="mock_responses_exhausted"):
        adapter.invoke(request)
    assert script.adapter().invoke(request).content == "COMPLETED"
    assert script.adapter(response_offset=1).profile() == script.adapter().profile()


@pytest.mark.parametrize("offset", [2, 3])
def test_offset_at_or_after_end_produces_safe_exhaustion(tmp_path: Path, offset: int) -> None:
    value = mock_document()
    value["model"]["responses"] = [wire_response(), wire_response()]
    adapter = load_offline_script(write(tmp_path, value)).adapter(response_offset=offset)
    with pytest.raises(AdapterTransportError, match="^mock_responses_exhausted$"):
        adapter.invoke(adapter.prepare([HumanMessage(content="Inspect.")], []))


@pytest.mark.parametrize("offset", [-1, True, False, 1.0, "1", None])
def test_offset_is_a_strict_nonnegative_host_integer(tmp_path: Path, offset: object) -> None:
    script = load_offline_script(write(tmp_path, mock_document()))
    with pytest.raises(ValueError, match="response offset"):
        script.adapter(response_offset=offset)


def test_legacy_messages_options_and_executors_have_no_shared_mutable_state(tmp_path: Path) -> None:
    value = fixed_document()
    value["limits"] = {"max_calls": 5}
    script = load_offline_script(write(tmp_path, value))
    messages = script.fixed_responses()
    messages[0].tool_calls[0]["args"]["path"] = "changed"
    messages[0].usage_metadata["total_tokens"] = 100
    messages.clear()
    options = script.options()
    options["max_calls"] = 100
    executor = script.executor()
    executor.expected_text = "changed"
    assert script.fixed_responses()[0].tool_calls[0]["args"]["path"] == "src/main.ets"
    assert script.fixed_responses()[0].usage_metadata["total_tokens"] == 15
    assert script.options() == {"max_calls": 5}
    assert script.executor().expected_text == "synthetic expectation\n"


@pytest.mark.parametrize("schema", [1, 2])
def test_empty_response_streams_are_valid_and_do_not_make_network_requests(tmp_path: Path, schema: int) -> None:
    value = fixed_document() if schema == 1 else mock_document()
    if schema == 1:
        value["responses"] = []
    else:
        value["model"]["responses"] = []
    script = load_offline_script(write(tmp_path, value))
    if schema == 1:
        assert script.fixed_responses() == []
    else:
        adapter = script.adapter()
        with pytest.raises(AdapterTransportError, match="mock_responses_exhausted"):
            adapter.invoke(adapter.prepare([HumanMessage(content="Inspect.")], []))


def test_wire_usage_is_not_silently_fixed_and_malformed_response_is_left_to_adapter(tmp_path: Path) -> None:
    value = mock_document()
    raw_usage = {"prompt_tokens": "ten", "completion_tokens": -1, "total_tokens": None}
    value["model"]["responses"][0]["usage"] = raw_usage
    adapter = load_offline_script(write(tmp_path, value)).adapter()
    message = adapter.invoke(adapter.prepare([HumanMessage(content="Inspect.")], []))
    assert message.usage_metadata is None
    assert message.response_metadata["dfx_provider_usage_raw"] == raw_usage
    value["model"]["responses"] = [{"bad_response": True}]
    adapter = load_offline_script(write(tmp_path, value)).adapter()
    with pytest.raises(AdapterResponseError, match="invalid_response_identity"):
        adapter.invoke(adapter.prepare([HumanMessage(content="Inspect.")], []))


@pytest.mark.parametrize("usage", [False, 15, [], {},
                                   {"input_tokens": "10", "output_tokens": 5, "total_tokens": 15},
                                   {"input_tokens": True, "output_tokens": 5, "total_tokens": 6},
                                   {"input_tokens": -1, "output_tokens": 5, "total_tokens": 4},
                                   {"input_tokens": 10.0, "output_tokens": 5, "total_tokens": 15},
                                   {"input_tokens": 10, "output_tokens": 5, "total_tokens": 99},
                                   {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15, "extra": 0}])
def test_fixed_usage_is_strict_before_langchain_conversion(tmp_path: Path, usage: object) -> None:
    value = fixed_document()
    value["responses"][0]["usage"] = usage
    with pytest.raises(ValueError, match="fixed-response usage"):
        load_offline_script(write(tmp_path, value))


@pytest.mark.parametrize("limits", [None, [], {"max_calls": 0}, {"max_calls": -1}, {"max_calls": True},
                                    {"max_calls": 1.0}, {"max_candidates": "2"}, {"max_seconds": False},
                                    {"max_seconds": 0}, {"max_seconds": -0.1}, {"unknown": 1}])
def test_invalid_limits_are_not_coerced_or_given_another_meaning(tmp_path: Path, limits: object) -> None:
    value = fixed_document()
    value["limits"] = limits
    with pytest.raises(ValueError, match="limits"):
        load_offline_script(write(tmp_path, value))


@pytest.mark.parametrize("tool,args", [("execute", {}), ("unknown_tool", {}), (False, {}), ("", {}),
                                      ("read_log", []), ("read_log", None), (None, {"ignored": True})])
def test_invalid_legacy_tool_or_arguments_are_rejected(tmp_path: Path, tool: object, args: object) -> None:
    value = fixed_document()
    value["responses"][0].update(tool=tool, args=args)
    with pytest.raises(ValueError, match="fixed-response"):
        load_offline_script(write(tmp_path, value))


@pytest.mark.parametrize("path", ["../outside.ets", "/outside.ets", "C:/outside.ets", "src\\main.ets",
                                 "src//main.ets", "src/./main.ets", "", "src/\u0000main.ets"])
def test_expectation_path_must_be_a_safe_relative_source_path(tmp_path: Path, path: str) -> None:
    value = fixed_document()
    value["expected"]["path"] = path
    with pytest.raises(ValueError, match="expectation path"):
        load_offline_script(write(tmp_path, value))


@pytest.mark.parametrize("location,key", [("top", "endpoint"), ("top", "api_key"), ("model", "endpoint"),
                                        ("model", "credentials"), ("settings", "base_url"), ("settings", "api_key")])
def test_endpoints_and_credentials_are_rejected_without_exposing_values(tmp_path: Path, location: str, key: str) -> None:
    value = mock_document()
    destination = value if location == "top" else value["model"] if location == "model" else value["model"]["settings"]
    destination[key] = "SENSITIVE_CONFIGURATION_VALUE"
    with pytest.raises(ValueError) as caught:
        load_offline_script(write(tmp_path, value))
    assert "SENSITIVE_CONFIGURATION_VALUE" not in str(caught.value)


@pytest.mark.parametrize("change", ["schema_bool", "schema_unknown", "extra_top", "extra_expected", "extra_response",
                                   "wrong_text", "wrong_responses", "wrong_flags", "extra_flags", "wire_mode", "wire_array"])
def test_closed_schema_fields_and_feature_types(tmp_path: Path, change: str) -> None:
    value = mock_document() if change.startswith("wire") else fixed_document()
    if change == "schema_bool":
        value["schema_version"] = True
    elif change == "schema_unknown":
        value["schema_version"] = 3
    elif change == "extra_top":
        value["unused"] = True
    elif change == "extra_expected":
        value["expected"]["unused"] = True
    elif change == "extra_response":
        value["responses"][0]["unused"] = True
    elif change == "wrong_text":
        value["expected"]["text"] = 1
    elif change == "wrong_responses":
        value["responses"] = {}
    elif change == "wrong_flags":
        value["policy_features"] = {"rule_guidance": "yes"}
    elif change == "extra_flags":
        value["policy_features"] = {"unused": True}
    elif change == "wire_mode":
        value["model"]["mode"] = "cloud"
    else:
        value["model"]["responses"] = [[]]
    with pytest.raises(ValueError):
        load_offline_script(write(tmp_path, value))


@pytest.mark.parametrize("raw", [b"\xff", b"[]", b"{}", b'{"schema_version":1,"schema_version":2}',
                               b'{"number":NaN}', b'{"number":Infinity}', b'{"number":1e999}',
                               b'{"text":"\\ud800"}', b'{"nested":{"x":1,"x":2}}'])
def test_invalid_utf8_json_duplicate_keys_and_nonfinite_values(tmp_path: Path, raw: bytes) -> None:
    path = tmp_path / "invalid.json"
    path.write_bytes(raw)
    with pytest.raises(ValueError):
        load_offline_script(path)


def test_script_size_is_bounded_before_decoding(tmp_path: Path) -> None:
    path = tmp_path / "large.json"
    path.write_bytes(b" " * (MAX_SCRIPT_BYTES + 1))
    with pytest.raises(ValueError, match="1 MiB"):
        load_offline_script(path)


def test_hard_link_script_is_refused(tmp_path: Path) -> None:
    original = write(tmp_path, fixed_document())
    linked = tmp_path / "linked.json"
    try:
        os.link(original, linked)
    except OSError as exc:
        pytest.skip(f"Hard links unavailable: {exc}")
    with pytest.raises(ValueError, match="unlinked"):
        load_offline_script(linked)


def test_symbolic_link_script_is_refused(tmp_path: Path) -> None:
    original = write(tmp_path, fixed_document())
    linked = tmp_path / "linked.json"
    try:
        linked.symlink_to(original)
    except OSError as exc:
        pytest.skip(f"Symbolic links unavailable: {exc}")
    with pytest.raises(ValueError, match="linked"):
        load_offline_script(linked)


@pytest.mark.parametrize("document", [fixed_document, mock_document])
def test_context_compaction_is_disabled_by_default(tmp_path: Path, document) -> None:
    script = load_offline_script(write(tmp_path, document()))
    assert script.compaction_policy() == CompactionPolicy()
    assert script.compaction_policy().enabled is False
    assert script.compaction_responses() == []


def test_fixed_compaction_stream_is_independent_fresh_and_text_only(tmp_path: Path) -> None:
    value = fixed_document()
    value["context_compaction"] = {
        "policy": {"enabled": True, "max_summary_bytes": 8},
        "responses": [{"text": "单独的摘要", "usage": {"input_tokens": 5, "output_tokens": 2, "total_tokens": 7}},
                      {"text": "", "usage": None}]}
    script = load_offline_script(write(tmp_path, value))
    policy = script.compaction_policy()
    assert policy.enabled is True and policy.max_summary_bytes == 8
    assert policy is not script.compaction_policy()
    summaries = script.compaction_responses()
    assert [item.content for item in summaries] == ["单独的摘要", ""]
    assert all(not item.tool_calls and not item.invalid_tool_calls for item in summaries)
    assert summaries[0].usage_metadata == {"input_tokens": 5, "output_tokens": 2, "total_tokens": 7}
    assert summaries[1].usage_metadata is None
    summaries[0].content = "mutated"
    summaries[0].usage_metadata["total_tokens"] = 100
    summaries.clear()
    assert script.compaction_responses()[0].content == "单独的摘要"
    assert script.compaction_responses()[0].usage_metadata["total_tokens"] == 7
    assert len(script.fixed_responses()) == 2
    assert script.fixed_responses()[0].tool_calls[0]["id"] == "call-1"
    assert script.fixed_responses()[1].content == "done"


@pytest.mark.parametrize("document", [fixed_document, mock_document])
def test_compaction_policy_without_a_fixture_stream_is_valid(tmp_path: Path, document) -> None:
    value = document()
    value["context_compaction"] = {"policy": {"enabled": True}}
    script = load_offline_script(write(tmp_path, value))
    assert script.compaction_policy().enabled is True
    assert script.compaction_responses() == []


def test_mock_summary_and_solver_use_one_wire_stream_and_total_completed_offset(tmp_path: Path) -> None:
    value = mock_document()
    value["context_compaction"] = {"policy": {"enabled": True}}
    value["model"]["responses"] = [wire_response(content="summary"), wire_response(content="solver")]
    script = load_offline_script(write(tmp_path, value))
    assert script.compaction_responses() == []
    adapter = script.adapter()
    request = adapter.prepare([HumanMessage(content="No fixtures in this context.")], [])
    assert "summary" not in request.body and "solver" not in request.body
    assert adapter.invoke(request).content == "summary"
    assert adapter.invoke(request).content == "solver"
    resumed = script.adapter(response_offset=1)
    assert resumed.invoke(request).content == "solver"


@pytest.mark.parametrize("responses", [[], [{"text": "", "usage": None}],
                                     [{"text": "x" * 65_537, "usage": None}]])
def test_loader_preserves_empty_and_oversized_summary_outputs_for_runtime_validation(
        tmp_path: Path, responses: list) -> None:
    value = fixed_document()
    value["context_compaction"] = {"policy": {"enabled": True, "max_summary_bytes": 1}, "responses": responses}
    script = load_offline_script(write(tmp_path, value))
    assert [item.content for item in script.compaction_responses()] == [item["text"] for item in responses]


@pytest.mark.parametrize("compaction", [None, [], {}, {"policy": {}, "extra": True},
                                      {"policy": {}, "responses": None}, {"policy": {}, "responses": {}},
                                      {"policy": {}, "responses": [1]},
                                      {"policy": {}, "responses": [{"text": "summary"}]},
                                      {"policy": {}, "responses": [{"usage": None}]},
                                      {"policy": {}, "responses": [{"text": 1, "usage": None}]},
                                      {"policy": {}, "responses": [{"text": "summary", "usage": None, "tool": "edit_source"}]}])
def test_compaction_configuration_and_response_fields_are_closed(tmp_path: Path, compaction: object) -> None:
    value = fixed_document()
    value["context_compaction"] = compaction
    with pytest.raises(ValueError, match="compaction"):
        load_offline_script(write(tmp_path, value))


@pytest.mark.parametrize("policy", [None, [], {"unknown": 1}, {"enabled": "true"}, {"enabled": 1},
                                  {"trigger_bytes": 127}, {"trigger_bytes": 786_433}, {"retain_groups": 0},
                                  {"retain_groups": 21}, {"max_summary_input_bytes": 127},
                                  {"max_summary_input_bytes": 900_001}, {"max_summary_bytes": 0},
                                  {"max_summary_bytes": 65_537}, {"max_summary_bytes": True}])
def test_compaction_policy_uses_strict_host_contract(tmp_path: Path, policy: object) -> None:
    value = fixed_document()
    value["context_compaction"] = {"policy": policy}
    with pytest.raises(ValueError, match="compaction policy"):
        load_offline_script(write(tmp_path, value))


@pytest.mark.parametrize("usage", [{}, {"input_tokens": True, "output_tokens": 1, "total_tokens": 2},
                                 {"input_tokens": 1, "output_tokens": -1, "total_tokens": 0},
                                 {"input_tokens": 1, "output_tokens": 1, "total_tokens": 3},
                                 {"input_tokens": 1.0, "output_tokens": 1, "total_tokens": 2}])
def test_compaction_usage_keeps_existing_fixed_response_rules(tmp_path: Path, usage: object) -> None:
    value = fixed_document()
    value["context_compaction"] = {"policy": {"enabled": True}, "responses": [{"text": "summary", "usage": usage}]}
    with pytest.raises(ValueError, match="fixed-response usage"):
        load_offline_script(write(tmp_path, value))


@pytest.mark.parametrize("responses", [[], [{"text": "summary", "usage": None}]])
def test_mock_mode_rejects_a_separate_compaction_response_stream(tmp_path: Path, responses: list) -> None:
    value = mock_document()
    value["context_compaction"] = {"policy": {"enabled": True}, "responses": responses}
    with pytest.raises(ValueError, match="shared wire stream"):
        load_offline_script(write(tmp_path, value))


def test_compaction_text_rejects_invalid_utf8_scalar(tmp_path: Path) -> None:
    value = fixed_document()
    value["context_compaction"] = {"policy": {"enabled": True}, "responses": [{"text": "\ud800", "usage": None}]}
    path = tmp_path / "invalid-summary.json"
    path.write_text(json.dumps(value, ensure_ascii=True), encoding="utf-8")
    with pytest.raises(ValueError):
        load_offline_script(path)


@pytest.mark.parametrize("mock_mode", [False, True])
@pytest.mark.parametrize("command", ["repair", "resume"])
def test_cli_forwards_explicit_compaction_policy_and_only_fixed_summary_fixtures(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture,
        mock_mode: bool, command: str) -> None:
    from dfx_agent import agent

    value = mock_document() if mock_mode else fixed_document()
    value["context_compaction"] = {"policy": {"enabled": True, "max_summary_bytes": 20}}
    if not mock_mode:
        value["context_compaction"]["responses"] = [{"text": "summary fixture", "usage": None}]
    path = write(tmp_path, value)
    seen = {}
    admission = []
    # This isolates CLI option forwarding, using mocked Agent execution.
    # Real read-only admission and drift rejection have integration tests.
    if command == "resume":
        monkeypatch.setattr(agent, "preflight_resume", lambda *args: admission.append(args))

    def run(*args, **options):
        seen.update(options)
        return {"synthetic_handoff": True}

    monkeypatch.setattr(agent, "run_provider_agent" if mock_mode else "run_fixed_agent", run)
    if mock_mode and command == "resume":
        from test_agent_loop import task_snapshot

        from dfx_agent.model_calls import ModelCallIntent, ModelCallStore
        from dfx_agent.task import digest
        from dfx_agent.workspace import CandidateWorkspace

        task = task_snapshot(tmp_path)
        workspace = CandidateWorkspace(task, tmp_path / "run", "run1")
        store = ModelCallStore(workspace.root / "model-calls.sqlite")
        for index, kind in enumerate(("context_compaction", "solver")):
            intent = ModelCallIntent.model_validate({
                "call_id": f"saved-{index}", "task_id": "task1", "run_id": "run1", "kind": kind,
                "provider_id": "synthetic", "model_id": "offline", "prompt_sha256": digest(b"prompt"),
                "tool_schema_sha256": digest(b"tools"), "parameters_sha256": digest(b"parameters"), "tool_names": ()})
            store.reserve(intent, max_calls=3)
            store.dispatch(intent.call_id)
            store.complete(intent, AIMessage(content="saved"))
        value["model"]["responses"] = [wire_response(content="saved summary"), wire_response(content="saved solver"),
                                       wire_response(content="new response")]
        path = write(tmp_path, value)

        def run_after_offset(*args, **options):
            seen.update(options)
            adapter = args[3]
            assert adapter.invoke(adapter.prepare([HumanMessage(content="next request")], [])).content == "new response"
            return {"synthetic_handoff": True}

        monkeypatch.setattr(agent, "run_provider_agent", run_after_offset)
    else:
        task = tmp_path / "task"
    monkeypatch.setenv("DFX_MANAGED_AGENT", "offline-v1")
    monkeypatch.setattr(sys, "argv", ["dfx", command, "--task", str(task), "--work", str(tmp_path / "run"),
                                     "--run-id", "run1", "--script", str(path)])
    cli.main()
    assert admission == ([(task, tmp_path / "run", "run1")] if command == "resume" else [])
    assert json.loads(capsys.readouterr().out) == {"synthetic_handoff": True}
    assert seen["compaction_policy"] == CompactionPolicy(enabled=True, max_summary_bytes=20)
    assert seen["resume"] is (command == "resume")
    if mock_mode:
        assert "compaction_responses" not in seen
    else:
        assert len(seen["compaction_responses"]) == 1
        assert seen["compaction_responses"][0].content == "summary fixture"
