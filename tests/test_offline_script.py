"""Strict CLI fixture loading and isolated mock HTTP response consumption."""

from __future__ import annotations

import json
import os
from dataclasses import FrozenInstanceError
from pathlib import Path

import httpx
import pytest
from langchain_core.messages import AIMessage, HumanMessage
from test_model_provider import descriptors, wire_response

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
