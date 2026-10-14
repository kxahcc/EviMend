"""Local-only model settings; credentials never enter run snapshots or reports."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr

from .local_execution import _checked

DEFAULT_MODEL_PROFILE = "configs/model.local.json"
MAX_PROFILE_BYTES = 65536


class ModelBudget(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, hide_input_in_errors=True)
    max_calls: int = Field(default=40, ge=1, le=200)
    max_known_tokens: int = Field(default=256000, ge=1)
    max_output_tokens: int = Field(default=4096, ge=1, le=131072)
    max_seconds: int = Field(default=900, ge=1, le=1800)
    max_candidates: int = Field(default=4, ge=1, le=20)
    max_tool_calls: int = Field(default=100, ge=1, le=500)


class PrivateModelProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, hide_input_in_errors=True)
    schema_version: Literal[1] = 1
    provider: Literal["openai", "deepseek", "compatible"]
    model: str = Field(min_length=1, max_length=200)
    base_url: str | None = None
    api_key: SecretStr = Field(default_factory=lambda: SecretStr(""), exclude=True, repr=False)
    thinking_mode: Literal["enabled", "disabled"] | None = None
    output_parameter: Literal["max_tokens", "max_completion_tokens"] | None = None
    budget: ModelBudget = Field(default_factory=ModelBudget)


def load_model_profile(path: Path) -> PrivateModelProfile:
    """Reject invalid private input without including its bytes in an exception."""
    try:
        source = _checked(path)
        if not source.is_file() or source.stat().st_size > MAX_PROFILE_BYTES:
            raise ValueError("invalid file")
        with source.open("rb") as stream:
            raw = stream.read(MAX_PROFILE_BYTES + 1)
        if len(raw) > MAX_PROFILE_BYTES:
            raise ValueError("oversized file")
        return PrivateModelProfile.model_validate_json(raw.decode("utf-8-sig"))
    except (OSError, ValueError, TypeError, RecursionError):
        raise ValueError("private model configuration is missing or invalid") from None


def read_profile_credential(path: Path, *, expected_endpoint: str | None = None) -> str:
    private = load_model_profile(path)
    if expected_endpoint is not None:
        from .model_provider import provider_connection

        connection, _ = provider_connection(private.provider, private.model, base_url=private.base_url)
        if connection.endpoint != expected_endpoint:
            raise ValueError("provider_credential_endpoint_changed")
    value = private.api_key.get_secret_value()
    if not value:
        raise ValueError("provider_credential_missing")
    if len(value) > 8192 or any(ord(character) <= 32 or ord(character) >= 127 for character in value):
        raise ValueError("provider_credential_invalid")
    return value
