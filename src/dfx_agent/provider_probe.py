"""Read-only provider admission through the official SDK; no inference request."""

from __future__ import annotations

import json
import math
import shutil
import subprocess
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from openai import OpenAI

from .model_profile import load_model_profile
from .verification import _atomic_json


def cny_balance(response: Any) -> str:
    if not isinstance(response, dict) or response.get("is_available") is not True:
        raise ValueError("provider_balance_unavailable")
    rows = response.get("balance_infos")
    if not isinstance(rows, list):
        raise ValueError("provider_balance_invalid")  # noqa: TRY004 -- malformed external JSON, not caller type misuse
    matches = [row for row in rows if isinstance(row, dict) and row.get("currency") == "CNY"]
    if len(matches) != 1 or not isinstance(matches[0].get("total_balance"), str):
        raise ValueError("provider_cny_balance_missing")
    try:
        value = Decimal(matches[0]["total_balance"])
    except InvalidOperation:
        raise ValueError("provider_balance_invalid") from None
    if not value.is_finite() or value < 0:
        raise ValueError("provider_balance_invalid")
    return str(value)


def node_capability() -> dict[str, Any]:
    command = shutil.which("node")
    if command is None:
        return {"status": "missing", "strip_types": False}
    executable = Path(command).resolve(strict=True)
    if ".codex" in {part.casefold() for part in executable.parts} or executable.suffix.lower() != ".exe":
        return {"status": "unsupported_runtime", "strip_types": False}
    code = "process.stdout.write(JSON.stringify({version:process.version,strip_types:typeof require('node:module').stripTypeScriptTypes==='function'}))"
    result = subprocess.run([str(executable), "-e", code], capture_output=True, timeout=10, check=False)
    if result.returncode or len(result.stdout) > 4096:
        raise ValueError("node_probe_failed")
    value = json.loads(result.stdout)
    if not isinstance(value.get("version"), str) or type(value.get("strip_types")) is not bool:
        raise ValueError("node_probe_invalid")
    return {"status": "available", "executable": str(executable), **value}


def run_probe(root: Path) -> dict[str, Any]:
    from .managed_runtime import require_managed_worker

    require_managed_worker(root, "provider")
    policy = json.loads((root / "configs/local-runtime.json").read_bytes())
    if policy["execution"]["allow_model_experiments"] is not True:
        raise ValueError("provider_probe_not_authorized")
    profile = load_model_profile(root / "configs/model.local.json")
    if profile.provider != "deepseek" or profile.base_url not in (None, "https://api.deepseek.com", "https://api.deepseek.com/"):
        raise ValueError("provider_probe_requires_official_deepseek")
    if not profile.api_key.get_secret_value():
        raise ValueError("provider_credential_missing")
    try:
        with OpenAI(api_key=profile.api_key.get_secret_value(), base_url="https://api.deepseek.com",
                    max_retries=0, timeout=15.0) as client:
            models = [item.id for item in client.models.list().data]
            balance = cny_balance(client.get("/user/balance", cast_to=dict[str, Any]))
    except Exception as error:  # noqa: BLE001 -- redact every SDK exception at the private credential boundary
        # Do not serialize SDK responses or exception messages, which may echo
        # private input. The caller receives only a bounded category.
        raise ValueError("provider_probe_failed:" + type(error).__name__) from None
    result = {"schema_version": 1, "observed_at": datetime.now(UTC).isoformat(),
        "model": profile.model, "model_available": profile.model in models,
        "balance_cny": balance, "inference_requests": 0, "thinking_mode": profile.thinking_mode,
        "node": node_capability()}
    if not math.isfinite(float(balance)):
        raise ValueError("provider_balance_invalid")
    _atomic_json(root / ".dfx-local/state/provider-admission.json", result)
    return result
