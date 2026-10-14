"""Private file parsing and redaction, without external requests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from dfx_agent.model_profile import (
    MAX_PROFILE_BYTES,
    load_model_profile,
    read_profile_credential,
)

SECRET = "synthetic-private-key-never-export"


def profile(tmp_path: Path, **changes: object) -> Path:
    source = tmp_path / "model.local.json"
    source.write_text(json.dumps({"provider": "deepseek", "model": "chosen-model", "api_key": SECRET,
                                  **changes}), encoding="utf-8-sig")
    return source


def test_private_model_and_budget_parse_but_key_is_excluded(tmp_path: Path) -> None:
    source = profile(tmp_path, budget={"max_calls": 7, "max_known_tokens": 50000})
    parsed = load_model_profile(source)
    assert parsed.budget.max_calls == 7 and parsed.budget.max_known_tokens == 50000
    assert read_profile_credential(source) == SECRET
    assert "api_key" not in parsed.model_dump()
    assert SECRET not in parsed.model_dump_json() and SECRET not in repr(parsed)


def test_private_network_defaults_and_explicit_limits(tmp_path: Path) -> None:
    assert load_model_profile(profile(tmp_path)).network.max_retries == 2
    parsed = load_model_profile(profile(tmp_path, network={"max_retries": 5,
        "request_timeout_seconds": 90.0, "connect_timeout_seconds": 10.0}))
    assert parsed.network.max_retries == 5 and parsed.network.request_timeout_seconds == 90.0
    assert SECRET not in parsed.model_dump_json()


@pytest.mark.parametrize("changes", [
    {"provider": "unknown"}, {"budget": {"max_calls": 0}}, {"budget": {"max_seconds": 1801}},
    {"budget": {"max_tool_calls": 501}}, {"api_key": {"private": SECRET}}, {"unsupported": SECRET},
    {"network": {"max_retries": 6}}, {"network": {"max_retries": -1}},
    {"network": {"request_timeout_seconds": 5.0, "connect_timeout_seconds": 10.0}},
    {"network": {"request_timeout_seconds": float("nan")}},
])
def test_invalid_private_input_never_echoes_values(tmp_path: Path, changes: dict[str, object]) -> None:
    with pytest.raises(ValueError) as failure:
        load_model_profile(profile(tmp_path, **changes))
    assert SECRET not in str(failure.value)


@pytest.mark.parametrize("value,reason", [("", "missing"), ("key\nvalue", "invalid"), ("密钥", "invalid")])
def test_credential_is_required_and_bounded(tmp_path: Path, value: str, reason: str) -> None:
    with pytest.raises(ValueError, match="provider_credential_" + reason):
        read_profile_credential(profile(tmp_path, api_key=value))


def test_oversized_missing_and_malformed_files_are_sanitized(tmp_path: Path) -> None:
    path = tmp_path / "model.local.json"
    for raw in (None, b"x" * (MAX_PROFILE_BYTES + 1), b'{"api_key":"' + SECRET.encode()):
        if raw is not None:
            path.write_bytes(raw)
        with pytest.raises(ValueError, match="missing or invalid") as failure:
            load_model_profile(path)
        assert SECRET not in str(failure.value)
