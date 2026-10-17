"""Small, read-only admission gate for the explicitly registered 18 CNY batch.

Existing per-run SQLite ledgers remain the accounting authority. This module
neither sends requests nor maintains another ledger, scheduler or account key.
"""

from __future__ import annotations

import hashlib
import json
import re
from contextlib import closing
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .evidence_io import open_readonly_database
from .local_execution import _checked
from .model_calls import ModelCallStore, canonical

_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
_AMOUNT = re.compile(r"[0-9]{1,6}(?:\.[0-9]{1,6})?\Z")
_MILLION = Decimal(1_000_000)
_DEEPSEEK_ENDPOINTS = {"https://api.deepseek.com/chat/completions", "https://api.deepseek.com/v1/chat/completions"}
BatchCostMode = Literal["flat8", "deepseek_flash_peak"]


class BatchBudget(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, hide_input_in_errors=True)
    schema_version: Literal[1] = 1
    budget_id: str
    model_id: str = Field(min_length=1, max_length=200)
    credential_file: str = Field(min_length=1)
    max_cny: str = "18"
    ceiling_cny_per_million_tokens: str = "8"
    run_ids: list[str] = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def valid_batch(self) -> BatchBudget:
        if not _NAME.fullmatch(self.budget_id) or any(not _NAME.fullmatch(item) for item in self.run_ids):
            raise ValueError("invalid registered batch or run ID")
        if len({item.casefold() for item in self.run_ids}) != len(self.run_ids):
            raise ValueError("duplicate registered run ID")
        if not _AMOUNT.fullmatch(self.max_cny) or not _AMOUNT.fullmatch(self.ceiling_cny_per_million_tokens):
            raise ValueError("batch money amounts must be finite decimal strings")
        if not Decimal(0) < Decimal(self.max_cny) <= Decimal(18):
            raise ValueError("this batch may allocate at most 18 CNY of the 20 CNY allowance")
        if Decimal(self.ceiling_cny_per_million_tokens) < Decimal(8):
            raise ValueError("this batch must use at least the confirmed 8 CNY per million ceiling")
        return self


def _read_json(path: Path, *, max_bytes: int = 65536) -> tuple[dict[str, Any], bytes]:
    source = _checked(path)
    if not source.is_file() or source.stat().st_size > max_bytes:
        raise ValueError("batch budget input is missing or oversized")
    raw = source.read_bytes()
    if len(raw) > max_bytes:
        raise ValueError("batch budget input is oversized")
    value = json.loads(raw.decode("utf-8-sig"))
    if not isinstance(value, dict):
        # File shape errors share the public invalid-configuration contract.
        raise ValueError("batch budget input must be an object")  # noqa: TRY004
    return value, raw


def _credential_location(repo: Path, value: str) -> Path:
    candidate = Path(value)
    location = _checked(candidate if candidate.is_absolute() else repo / candidate)
    if location.parent != repo / "configs" or not location.name.endswith(".local.json") or not location.is_file():
        raise ValueError("batch credential locator must use configs/*.local.json")
    return location


def _ledger_usage(work: Path, frozen: dict[str, Any], run_id: str, model_id: str) -> dict[str, int]:
    task_id = frozen["task"]["task_id"]
    ledger_path = _checked(work / "model-calls.sqlite")
    with closing(open_readonly_database(ledger_path)) as connection:
        owners = connection.execute("SELECT DISTINCT task_id, run_id FROM model_calls").fetchall()
    if any(row["task_id"] != task_id or row["run_id"] != run_id for row in owners):
        raise ValueError("batch ledger contains a different run owner")
    store = ModelCallStore(ledger_path, read_only=True)
    records = store.records(task_id, run_id)
    if any(record.intent.model_id != model_id for record in records):
        raise ValueError("batch ledger contains a different model")
    return store.usage_summary(task_id, run_id)


def _saved_run(work: Path, run_id: str, binding: dict[str, Any], model_id: str,
               credential_file: Path, repo: Path) -> tuple[dict[str, Any], dict[str, int]]:
    frozen, _ = _read_json(work / "competition-config.json", max_bytes=2_097_152)
    settings = frozen["config"]
    if (frozen.get("batch_budget") != binding or settings.get("model_mode") != "cloud" or
            settings["model"]["model_id"] != model_id or
            _credential_location(repo, settings["provider"]["credential_file"]) != credential_file):
        raise ValueError("registered run belongs to a different batch, model or credential locator")
    cap = settings["max_known_tokens"]
    if type(cap) is not int or cap < 1:
        raise ValueError("registered run token cap is invalid")
    usage = _ledger_usage(work, frozen, run_id, model_id)
    if usage["known_total_tokens"] > cap:
        raise ValueError("registered run exceeded its frozen token cap")
    return frozen, usage


def _require_deepseek_peak_identity(model_id: str, endpoint: str | None) -> None:
    if model_id != "deepseek-flash" or endpoint not in _DEEPSEEK_ENDPOINTS:
        raise ValueError("DeepSeek peak pricing requires the official endpoint and deepseek-flash model")


def deepseek_flash_peak_cost(work: Path, *, task_id: str, run_id: str,
                             model_id: str, endpoint: str | None) -> dict[str, Any]:
    """Reprice complete saved usage read-only; never claim an actual account bill.

    Confirmed peak rates on 2026-10-17 are 0.04/2/8 CNY per million
    cached-input/uncached-input/output tokens. Missing or conflicting cache
    details receive no cache discount. Reasoning is already part of output.
    """
    _require_deepseek_peak_identity(model_id, endpoint)
    usage = _ledger_usage(work, {"task": {"task_id": task_id}}, run_id, model_id)
    if (usage["uncertain_calls"] or usage["missing_usage_calls"] or usage["invalid_usage_calls"] or
            usage["reserved_calls"] != usage["completed_calls"]):
        raise ValueError("DeepSeek peak pricing requires a complete known-usage ledger")
    store = ModelCallStore(_checked(work / "model-calls.sqlite"), read_only=True)
    hits = misses = fallback_calls = known_reasoning = reasoning_unknown_calls = 0
    for record in store.records(task_id, run_id):
        message, observation = store.replay(record.intent)
        counts = observation.usage
        if counts is None:
            raise ValueError("DeepSeek peak pricing requires known normalized usage")
        raw = message.response_metadata.get("dfx_provider_usage_raw")
        raw = raw if isinstance(raw, dict) else {}
        hit, miss = raw.get("prompt_cache_hit_tokens"), raw.get("prompt_cache_miss_tokens")
        valid_cache = (type(hit) is int and type(miss) is int and hit >= 0 and miss >= 0 and
                       hit + miss == counts.input_tokens)
        details = raw.get("prompt_tokens_details")
        if details is not None:
            if not isinstance(details, dict):
                valid_cache = False
            elif "cached_tokens" in details:
                cached = details["cached_tokens"]
                if type(cached) is not int or cached < 0 or cached != hit:
                    valid_cache = False
        if valid_cache:
            hits += hit
            misses += miss
        else:
            misses += counts.input_tokens
            fallback_calls += 1
        output_details = raw.get("completion_tokens_details")
        reasoning = output_details.get("reasoning_tokens") if isinstance(output_details, dict) else None
        if type(reasoning) is int and 0 <= reasoning <= counts.output_tokens:
            known_reasoning += reasoning
        else:
            reasoning_unknown_calls += 1
    input_tokens, output_tokens = usage["known_input_tokens"], usage["known_output_tokens"]
    if hits + misses != input_tokens:
        raise ValueError("DeepSeek cache accounting differs from normalized input usage")
    ceiling = (Decimal(hits) * Decimal("0.04") + Decimal(misses) * Decimal(2) +
               Decimal(output_tokens) * Decimal(8)) / _MILLION
    return {"cost_mode": "deepseek_flash_peak", "pricing_as_of": "2026-10-17",
            "logical_calls": usage["completed_calls"], "known_input_tokens": input_tokens,
            "cache_hit_tokens": hits, "cache_miss_tokens": misses, "known_output_tokens": output_tokens,
            "known_total_tokens": usage["known_total_tokens"], "cache_fallback_calls": fallback_calls,
            "cache_usage_complete": fallback_calls == 0, "known_reasoning_tokens": known_reasoning,
            "reasoning_tokens": known_reasoning if reasoning_unknown_calls == 0 else None,
            "reasoning_unknown_calls": reasoning_unknown_calls,
            "rates_cny_per_million_tokens": {"cache_hit": "0.04", "cache_miss": "2", "output": "8"},
            "estimated_ceiling_cny": format(ceiling, "f")}


def check_batch_budget(*, project_root: Path, managed_root: Path, work: Path, run_id: str,
                       profile_path: str, model_id: str, credential_file: str | None,
                       max_known_tokens: int, resume: bool = False,
                       charge_unknown_run_caps: tuple[str, ...] = (), batch_cost_mode: BatchCostMode = "flat8",
                       endpoint: str | None = None) -> dict[str, Any]:
    """Check all registered runs before any mutable run initialization.

    Reserve the current run's full cap, counting other terminal runs from their
    recorded usage. A lost response or interrupted other run blocks admission;
    the current run's recovery remains governed by its original call ledger.
    The returned immutable identity is frozen with the competition config.
    """
    repo, managed = _checked(project_root), _checked(managed_root)
    selected = Path(profile_path)
    source = _checked(selected if selected.is_absolute() else repo / selected)
    if not ((source.parent == repo / "configs" and source.name.endswith(".local.json")) or
            source.parent == managed / "state"):
        raise ValueError("batch budget file must be a local config or managed state file")
    value, raw = _read_json(source)
    batch = BatchBudget.model_validate(value)
    if run_id not in batch.run_ids:
        raise ValueError("run is not registered in the batch budget")
    approved = set(charge_unknown_run_caps)
    if (len(approved) != len(charge_unknown_run_caps) or run_id in approved or
            not approved <= set(batch.run_ids)):
        raise ValueError("unknown-cost acceptance must name unique other registered runs")
    if model_id != batch.model_id or credential_file is None:
        raise ValueError("batch model or credential locator differs")
    private = _credential_location(repo, credential_file)
    if _credential_location(repo, batch.credential_file) != private or source == private:
        raise ValueError("batch credential locator differs")
    if type(max_known_tokens) is not int or max_known_tokens < 1:
        raise ValueError("invalid batch run token cap")
    if batch_cost_mode not in ("flat8", "deepseek_flash_peak"):
        raise ValueError("unknown batch cost mode")
    if batch_cost_mode == "deepseek_flash_peak":
        _require_deepseek_peak_identity(model_id, endpoint)
    expected_work = _checked(managed / "work" / ("competition-" + run_id), exists=False)
    if _checked(work, exists=resume) != expected_work:
        raise ValueError("batch run must use its registered managed work directory")
    binding = {"schema_version": 1, "budget_id": batch.budget_id, "profile_path": str(source),
               "profile_sha256": hashlib.sha256(raw).hexdigest(), "max_cny": batch.max_cny,
               "ceiling_cny_per_million_tokens": batch.ceiling_cny_per_million_tokens}
    if resume:
        current, _ = _saved_run(expected_work, run_id, binding, model_id, private, repo)
        if current["config"]["max_known_tokens"] != max_known_tokens:
            raise ValueError("batch resume token cap differs from the original run")
    elif expected_work.exists():
        raise ValueError("registered run already exists; resume its original budget")

    charged_other_cny = Decimal(0)
    reservation_rate = Decimal(batch.ceiling_cny_per_million_tokens)
    used_approvals: set[str] = set()
    for identifier in batch.run_ids:
        if identifier == run_id:
            continue
        previous_work = _checked(managed / "work" / ("competition-" + identifier), exists=False)
        if not previous_work.exists():
            continue
        previous, usage = _saved_run(previous_work, identifier, binding, model_id, private, repo)
        if batch_cost_mode == "deepseek_flash_peak":
            _require_deepseek_peak_identity(model_id, previous["config"]["provider"].get("endpoint"))
        unknown = bool(usage["uncertain_calls"] or usage["missing_usage_calls"] or usage["invalid_usage_calls"] or
                       usage["reserved_calls"] != usage["completed_calls"])
        if unknown and identifier not in approved:
            raise ValueError("another registered run has incomplete or unknown model cost")
        report, _ = _read_json(previous_work / "competition-report.json", max_bytes=33_554_432)
        unhashed = {key: item for key, item in report.items() if key != "report_sha256"}
        if (report.get("run_id") != identifier or report.get("task_id") != previous["task"]["task_id"] or
                report.get("report_sha256") != hashlib.sha256(canonical(unhashed).encode()).hexdigest() or
                any(report.get("cost", {}).get(key) != count for key, count in usage.items())):
            raise ValueError("another registered run has no current terminal cost report")
        if unknown:
            # Explicit host acceptance occupies the entire original run cap.
            # It never repairs a ledger, invents usage or resends its request.
            charged_other_cny += Decimal(previous["config"]["max_known_tokens"]) * reservation_rate / _MILLION
            used_approvals.add(identifier)
        elif batch_cost_mode == "deepseek_flash_peak":
            priced = deepseek_flash_peak_cost(previous_work, task_id=previous["task"]["task_id"], run_id=identifier,
                model_id=model_id, endpoint=previous["config"]["provider"]["endpoint"])
            charged_other_cny += Decimal(priced["estimated_ceiling_cny"])
        else:
            charged_other_cny += Decimal(usage["known_total_tokens"]) * reservation_rate / _MILLION
    if used_approvals != approved:
        raise ValueError("unknown-cost acceptance has no matching terminal unknown run")
    reserved_cny = charged_other_cny + Decimal(max_known_tokens) * reservation_rate / _MILLION
    if reserved_cny > Decimal(batch.max_cny):
        raise ValueError("batch monetary ceiling would be exceeded by this run reservation")
    return binding
