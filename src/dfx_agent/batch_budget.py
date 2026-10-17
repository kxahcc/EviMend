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


def check_batch_budget(*, project_root: Path, managed_root: Path, work: Path, run_id: str,
                       profile_path: str, model_id: str, credential_file: str | None,
                       max_known_tokens: int, resume: bool = False) -> dict[str, Any]:
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
    if model_id != batch.model_id or credential_file is None:
        raise ValueError("batch model or credential locator differs")
    private = _credential_location(repo, credential_file)
    if _credential_location(repo, batch.credential_file) != private or source == private:
        raise ValueError("batch credential locator differs")
    if type(max_known_tokens) is not int or max_known_tokens < 1:
        raise ValueError("invalid batch run token cap")
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

    known_other_tokens = 0
    for identifier in batch.run_ids:
        if identifier == run_id:
            continue
        previous_work = _checked(managed / "work" / ("competition-" + identifier), exists=False)
        if not previous_work.exists():
            continue
        previous, usage = _saved_run(previous_work, identifier, binding, model_id, private, repo)
        if (usage["uncertain_calls"] or usage["missing_usage_calls"] or usage["invalid_usage_calls"] or
                usage["reserved_calls"] != usage["completed_calls"]):
            raise ValueError("another registered run has incomplete or unknown model cost")
        report, _ = _read_json(previous_work / "competition-report.json", max_bytes=33_554_432)
        unhashed = {key: item for key, item in report.items() if key != "report_sha256"}
        if (report.get("run_id") != identifier or report.get("task_id") != previous["task"]["task_id"] or
                report.get("report_sha256") != hashlib.sha256(canonical(unhashed).encode()).hexdigest() or
                any(report.get("cost", {}).get(key) != count for key, count in usage.items())):
            raise ValueError("another registered run has no current terminal cost report")
        known_other_tokens += usage["known_total_tokens"]
    reserved_cny = (Decimal(known_other_tokens + max_known_tokens) *
                    Decimal(batch.ceiling_cny_per_million_tokens) / _MILLION)
    if reserved_cny > Decimal(batch.max_cny):
        raise ValueError("batch monetary ceiling would be exceeded by this run reservation")
    return binding
