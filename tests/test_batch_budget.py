"""No-network tests for the shared batch ceiling over existing model ledgers."""

from __future__ import annotations

import hashlib
import json
from decimal import ROUND_FLOOR, Decimal
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from dfx_agent import competition
from dfx_agent.batch_budget import (
    BatchBudget,
    check_batch_budget,
    deepseek_flash_peak_cost,
    unknown_request_cap_cost,
)
from dfx_agent.competition import CompetitionConfig
from dfx_agent.model_calls import ModelCallIntent, ModelCallStore, canonical, sha256
from dfx_agent.model_provider import (
    CloudChatCompletionsAdapter,
    CloudProviderConfig,
    ProviderSettings,
)


@pytest.fixture
def batch(tmp_path: Path) -> dict[str, Any]:
    repo = tmp_path / "repo"
    (repo / "configs").mkdir(parents=True)
    managed = repo / ".dfx-local"
    (managed / "work").mkdir(parents=True)
    (managed / "state").mkdir()
    credential = repo / "configs/model.local.json"
    # The gate compares this location but must never read its credential bytes.
    credential.write_text("this is deliberately not valid JSON", encoding="utf-8")
    profile = repo / "configs/batch.local.json"
    value = {"schema_version": 1, "budget_id": "first-real-batch", "model_id": "deepseek-flash",
             "credential_file": str(credential), "max_cny": "18", "ceiling_cny_per_million_tokens": "8",
             "run_ids": ["r1", "r2", "r3"]}
    profile.write_text(canonical(value), encoding="utf-8")
    return {"repo": repo, "managed": managed, "credential": credential, "profile": profile, "value": value}


def admit(batch: dict[str, Any], run_id: str = "r1", cap: int = 100000, *,
          resume: bool = False, **changes: Any) -> dict[str, Any]:
    arguments = {"project_root": batch["repo"], "managed_root": batch["managed"],
        "work": batch["managed"] / "work" / ("competition-" + run_id), "run_id": run_id,
        "profile_path": str(batch["profile"]), "model_id": "deepseek-flash",
        "credential_file": str(batch["credential"]), "max_known_tokens": cap, "resume": resume}
    return check_batch_budget(**(arguments | changes))


def saved(batch: dict[str, Any], run_id: str = "r1", *, tokens: int = 250000,
          cap: int = 500000, state: str = "completed", usage: bool = True,
          terminal: bool = True, binding: dict[str, Any] | None = None,
          output_tokens: int = 0, raw_usage: dict[str, Any] | None = None,
          endpoint: str = "https://api.deepseek.com/chat/completions") -> tuple[Path, ModelCallStore]:
    identity = binding if binding is not None else admit(batch, run_id, cap)
    work = batch["managed"] / "work" / ("competition-" + run_id)
    work.mkdir()
    task_id = "task-" + run_id
    config = {"model_mode": "cloud", "model": {"model_id": "deepseek-flash"},
              "provider": {"credential_file": str(batch["credential"]), "endpoint": endpoint}, "max_known_tokens": cap}
    (work / "competition-config.json").write_text(canonical({"config": config,
        "task": {"task_id": task_id}, "execution": None, "batch_budget": identity}), encoding="utf-8")
    store = ModelCallStore(work / "model-calls.sqlite")
    intent = ModelCallIntent(call_id="call1", task_id=task_id, run_id=run_id, kind="solver",
        provider_id="deepseek", model_id="deepseek-flash", prompt_sha256=sha256("prompt"),
        tool_schema_sha256=sha256("tools"), parameters_sha256=sha256("parameters"), tool_names=())
    store.reserve(intent, max_calls=2)
    if state != "reserved":
        store.dispatch("call1")
    if state == "completed":
        store.complete(intent, AIMessage(content="finished", usage_metadata=(
            {"input_tokens": tokens, "output_tokens": output_tokens, "total_tokens": tokens + output_tokens}
            if usage else None), response_metadata={} if raw_usage is None else {"dfx_provider_usage_raw": raw_usage}))
    elif state == "unknown":
        store.mark_unknown("call1", "response_lost")
    if terminal:
        report = {"schema_version": 4, "scope": "competition_task", "task_id": task_id,
                  "run_id": run_id, "cost": store.usage_summary(task_id, run_id), "status": "unresolved"}
        report["report_sha256"] = hashlib.sha256(canonical(report).encode()).hexdigest()
        (work / "competition-report.json").write_text(canonical(report), encoding="utf-8")
    return work, store


def test_first_admission_is_read_only_and_does_not_read_key(batch: dict[str, Any]) -> None:
    before = sorted(path.relative_to(batch["repo"]).as_posix() for path in batch["repo"].rglob("*"))
    identity = admit(batch)
    assert identity["max_cny"] == "18" and identity["ceiling_cny_per_million_tokens"] == "8"
    assert identity["profile_sha256"] == hashlib.sha256(batch["profile"].read_bytes()).hexdigest()
    assert sorted(path.relative_to(batch["repo"]).as_posix() for path in batch["repo"].rglob("*")) == before
    assert "credential" not in canonical(identity)


def test_exact_monetary_boundary_uses_decimal_without_rounding_up(batch: dict[str, Any]) -> None:
    admit(batch, cap=2250000)
    with pytest.raises(ValueError, match="monetary ceiling"):
        admit(batch, cap=2250001)


def test_reopened_ledger_charges_actual_other_usage_not_other_full_cap(batch: dict[str, Any]) -> None:
    work, _ = saved(batch)
    before = (work / "model-calls.sqlite").read_bytes()
    admit(batch, "r2", 2000000)  # 250000 + 2000000 at 8 CNY/M = exactly 18 CNY.
    assert (work / "model-calls.sqlite").read_bytes() == before
    with pytest.raises(ValueError, match="monetary ceiling"):
        admit(batch, "r2", 2000001)


@pytest.mark.parametrize("state,usage,terminal", [("reserved", True, True), ("dispatched", True, True),
    ("unknown", True, True), ("completed", False, True), ("completed", True, False)])
def test_unfinished_or_unknown_other_run_blocks_next(batch: dict[str, Any], state: str,
                                                    usage: bool, terminal: bool) -> None:
    saved(batch, state=state, usage=usage, terminal=terminal)
    with pytest.raises(ValueError):
        admit(batch, "r2")
    assert not (batch["managed"] / "work/competition-r2").exists()


def test_written_work_without_original_ledger_blocks_next(batch: dict[str, Any]) -> None:
    (batch["managed"] / "work/competition-r1").mkdir()
    with pytest.raises(ValueError):
        admit(batch, "r2")


def test_explicit_unknown_acceptance_charges_full_original_cap_without_rewriting(batch: dict[str, Any]) -> None:
    work, _ = saved(batch, state="unknown", cap=500000)
    before = {path.name: path.read_bytes() for path in work.iterdir()}
    admit(batch, "r2", cap=1750000, charge_unknown_run_caps=("r1",))
    with pytest.raises(ValueError, match="monetary ceiling"):
        admit(batch, "r2", cap=1750001, charge_unknown_run_caps=("r1",))
    assert {path.name: path.read_bytes() for path in work.iterdir()} == before
    with pytest.raises(ValueError, match="unknown model cost"):
        admit(batch, "r2")


@pytest.mark.parametrize("approved", [("r2",), ("outside",), ("r1", "r1"), ("r3",)])
def test_unknown_acceptance_cannot_name_current_duplicate_or_missing_runs(
        batch: dict[str, Any], approved: tuple[str, ...]) -> None:
    saved(batch, state="unknown")
    with pytest.raises(ValueError):
        admit(batch, "r2", charge_unknown_run_caps=approved)


def test_unknown_acceptance_cannot_replace_a_missing_terminal_report(batch: dict[str, Any]) -> None:
    saved(batch, state="unknown", terminal=False)
    with pytest.raises(ValueError):
        admit(batch, "r2", charge_unknown_run_caps=("r1",))


def test_resume_keeps_original_cap_without_double_charging_current(batch: dict[str, Any]) -> None:
    saved(batch, tokens=2100000, cap=2250000)
    admit(batch, cap=2250000, resume=True)
    with pytest.raises(ValueError, match="resume token cap"):
        admit(batch, cap=2249999, resume=True)
    with pytest.raises(ValueError, match="already exists"):
        admit(batch)


def test_current_unknown_can_reach_original_resume_engine_but_blocks_other_runs(batch: dict[str, Any]) -> None:
    saved(batch, state="unknown", terminal=False)
    admit(batch, cap=500000, resume=True)
    with pytest.raises(ValueError, match="unknown model cost"):
        admit(batch, "r2")


def test_profile_change_cannot_rebind_existing_runs_or_resume(batch: dict[str, Any]) -> None:
    saved(batch)
    batch["value"]["max_cny"] = "17"
    batch["profile"].write_text(canonical(batch["value"]), encoding="utf-8")
    with pytest.raises(ValueError, match="different batch"):
        admit(batch, "r2")
    with pytest.raises(ValueError, match="different batch"):
        admit(batch, cap=500000, resume=True)


@pytest.mark.parametrize("changes", [{"model_id": "other"}, {"credential_file": None},
    {"work": Path("somewhere-else")}, {"max_known_tokens": True}])
def test_current_identity_or_cap_mismatch_rejects(batch: dict[str, Any], changes: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        admit(batch, **changes)


def test_unregistered_and_case_changed_ids_reject(batch: dict[str, Any]) -> None:
    for identifier in ("outside", "R1"):
        with pytest.raises(ValueError, match="not registered"):
            admit(batch, identifier)


@pytest.mark.parametrize("changes", [{"max_cny": "20"}, {"max_cny": "0"},
    {"ceiling_cny_per_million_tokens": "7.999"}, {"max_cny": "NaN"},
    {"run_ids": ["r1", "R1"]}, {"run_ids": ["../outside"]}, {"api_key": "not-allowed"}])
def test_profile_cannot_raise_allowance_lower_price_or_acquire_key(batch: dict[str, Any],
                                                               changes: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        BatchBudget.model_validate(batch["value"] | changes)


def test_stale_report_does_not_hide_newer_ledger_usage(batch: dict[str, Any]) -> None:
    _, store = saved(batch)
    second = ModelCallIntent(call_id="call2", task_id="task-r1", run_id="r1", kind="solver",
        provider_id="deepseek", model_id="deepseek-flash", prompt_sha256=sha256("p2"),
        tool_schema_sha256=sha256("t2"), parameters_sha256=sha256("a2"), tool_names=())
    store.reserve(second, max_calls=2)
    store.dispatch("call2")
    store.complete(second, AIMessage(content="second", usage_metadata={
        "input_tokens": 100, "output_tokens": 0, "total_tokens": 100}))
    with pytest.raises(ValueError, match="current terminal cost report"):
        admit(batch, "r2")


def test_saved_run_cannot_hide_foreign_owner_or_model(batch: dict[str, Any]) -> None:
    _, store = saved(batch)
    foreign = ModelCallIntent(call_id="foreign", task_id="different-task", run_id="different-run", kind="solver",
        provider_id="deepseek", model_id="other-model", prompt_sha256=sha256("p"),
        tool_schema_sha256=sha256("t"), parameters_sha256=sha256("a"), tool_names=())
    store.reserve(foreign, max_calls=1)
    with pytest.raises(ValueError, match="different run owner"):
        admit(batch, "r2")


def test_batch_file_must_use_local_config_or_managed_state(batch: dict[str, Any]) -> None:
    public = batch["repo"] / "configs/public.json"
    public.write_text(canonical(batch["value"]), encoding="utf-8")
    with pytest.raises(ValueError, match="local config or managed state"):
        admit(batch, profile_path=str(public))
    state = batch["managed"] / "state/batch.json"
    state.write_text(canonical(batch["value"]), encoding="utf-8")
    assert admit(batch, profile_path=str(state))["budget_id"] == "first-real-batch"


def test_old_competition_dump_keeps_optional_budget_field_absent() -> None:
    root = Path(__file__).resolve().parents[1]
    config = CompetitionConfig.model_validate_json((root / "configs/competition-cloud.template.json").read_bytes())
    assert "batch_budget_file" not in config.model_dump(mode="json")
    assert "batch_cost_mode" not in config.model_dump(mode="json")
    loaded = CompetitionConfig.model_validate(config.model_dump(mode="json") | {"batch_budget_file": "configs/b.local.json"})
    assert loaded.model_dump(mode="json")["batch_budget_file"] == "configs/b.local.json"


def test_competition_rejects_over_budget_before_creating_run_or_knowledge(
        batch: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    repo = Path(__file__).resolve().parents[1]
    template = CompetitionConfig.model_validate_json((repo / "configs/competition-cloud.template.json").read_bytes())
    config = template.model_dump(mode="json")
    config["provider"]["credential_file"] = str(batch["credential"])
    config["model"]["model_id"] = "deepseek-flash"
    config["batch_budget_file"] = str(batch["profile"])
    config["max_known_tokens"] = 2250001
    selected = batch["repo"] / "configs/run.json"
    selected.write_text(canonical(config), encoding="utf-8")
    (batch["repo"] / "configs/local-runtime.json").write_text(canonical({"execution": {
        "allow_model_experiments": True, "allow_application_experiments": False}}), encoding="utf-8")
    (batch["managed"] / "state/active.json").write_text(canonical({"task": "competition"}), encoding="utf-8")
    monkeypatch.setenv("DFX_MANAGED_COMPETITION", "local-v1")

    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("no execution or knowledge initialization may precede budget admission")

    monkeypatch.setattr(competition, "prepare_domain_knowledge", forbidden)
    monkeypatch.setattr(competition, "AnalysisOnlyExecutor", forbidden)
    work = batch["managed"] / "work/competition-r1"
    with pytest.raises(ValueError, match="monetary ceiling"):
        competition._run_competition(batch["repo"] / "not-yet-needed-task", work, "r1", selected, batch["repo"])
    assert not work.exists()
    assert sorted(path.name for path in (batch["managed"] / "state").iterdir()) == ["active.json"]


def peak(work: Path, **changes: Any) -> dict[str, Any]:
    return deepseek_flash_peak_cost(work, **({"task_id": "task-r1", "run_id": "r1",
        "model_id": "deepseek-flash", "endpoint": "https://api.deepseek.com/chat/completions"} | changes))


def test_existing_evaluation_budget_cannot_be_omitted_from_new_cloud_configuration(
        batch: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    repo = Path(__file__).resolve().parents[1]
    raw = CompetitionConfig.model_validate_json((repo / "configs/competition-cloud.template.json").read_bytes()).model_dump(mode="json")
    raw["provider"]["credential_file"] = str(batch["credential"])
    selected = batch["repo"] / "configs/run.json"
    selected.write_text(canonical(raw), encoding="utf-8")
    (batch["repo"] / "configs/evaluation.local.json").write_bytes(Path(batch["profile"]).read_bytes())
    (batch["repo"] / "configs/local-runtime.json").write_text(canonical({"execution": {
        "allow_model_experiments": True, "allow_application_experiments": False}}), encoding="utf-8")
    (batch["managed"] / "state/active.json").write_text(canonical({"task": "competition"}), encoding="utf-8")
    monkeypatch.setenv("DFX_MANAGED_COMPETITION", "local-v1")
    work = batch["managed"] / "work/competition-r1"
    with pytest.raises(ValueError, match="shared batch monetary budget"):
        competition._run_competition(batch["repo"] / "unused-task", work, "r1", selected, batch["repo"])
    assert not work.exists()


def test_deepseek_peak_helper_reads_original_cache_usage_and_does_not_double_count_reasoning(
        batch: dict[str, Any]) -> None:
    raw = {"prompt_tokens": 5602, "completion_tokens": 81, "total_tokens": 5683,
           "prompt_cache_hit_tokens": 256, "prompt_cache_miss_tokens": 5346,
           "prompt_tokens_details": {"cached_tokens": 256}, "completion_tokens_details": {"reasoning_tokens": 40}}
    work, _ = saved(batch, tokens=5602, output_tokens=81, raw_usage=raw)
    before = (work / "model-calls.sqlite").read_bytes()
    priced = peak(work)
    assert priced["known_input_tokens"] == 5602 and priced["known_output_tokens"] == 81
    assert priced["cache_hit_tokens"] == 256 and priced["cache_miss_tokens"] == 5346
    assert priced["cache_fallback_calls"] == 0 and priced["cache_usage_complete"] is True
    assert priced["known_reasoning_tokens"] == 40 and priced["reasoning_tokens"] == 40
    assert Decimal(priced["estimated_ceiling_cny"]) == Decimal("0.01135024")
    assert (work / "model-calls.sqlite").read_bytes() == before
    assert peak(work) == priced  # Reopen does not write or charge a second time.


@pytest.mark.parametrize("cache", [{}, {"prompt_cache_hit_tokens": 256}, {"prompt_cache_miss_tokens": 5346},
    {"prompt_cache_hit_tokens": None, "prompt_cache_miss_tokens": 5346},
    {"prompt_cache_hit_tokens": True, "prompt_cache_miss_tokens": 5346},
    {"prompt_cache_hit_tokens": -1, "prompt_cache_miss_tokens": 5603},
    {"prompt_cache_hit_tokens": "256", "prompt_cache_miss_tokens": 5346},
    {"prompt_cache_hit_tokens": 256, "prompt_cache_miss_tokens": 5345},
    {"prompt_cache_hit_tokens": 256, "prompt_cache_miss_tokens": 5346,
     "prompt_tokens_details": {"cached_tokens": 255}},
    {"prompt_cache_hit_tokens": 256, "prompt_cache_miss_tokens": 5346,
     "prompt_tokens_details": {"cached_tokens": True}},
    {"prompt_cache_hit_tokens": 256, "prompt_cache_miss_tokens": 5346, "prompt_tokens_details": "unsupported"}])
def test_missing_invalid_or_conflicting_cache_metadata_is_all_input_miss(
        batch: dict[str, Any], cache: dict[str, Any]) -> None:
    raw = {"prompt_tokens": 5602, "completion_tokens": 81, "total_tokens": 5683} | cache
    work, _ = saved(batch, tokens=5602, output_tokens=81, raw_usage=raw)
    priced = peak(work)
    assert priced["cache_hit_tokens"] == 0 and priced["cache_miss_tokens"] == 5602
    assert priced["cache_fallback_calls"] == 1 and priced["cache_usage_complete"] is False
    assert priced["reasoning_tokens"] is None and priced["reasoning_unknown_calls"] == 1
    assert Decimal(priced["estimated_ceiling_cny"]) == Decimal("0.011852")


def test_new_mode_reprices_old_complete_run_without_changing_original_batch_profile(batch: dict[str, Any]) -> None:
    raw = {"prompt_tokens": 250000, "completion_tokens": 0, "total_tokens": 250000,
           "prompt_cache_hit_tokens": 250000, "prompt_cache_miss_tokens": 0,
           "prompt_tokens_details": {"cached_tokens": 250000}}
    work, _ = saved(batch, raw_usage=raw)
    original = batch["profile"].read_bytes()
    frozen = (work / "competition-config.json").read_bytes()
    with pytest.raises(ValueError, match="monetary ceiling"):
        admit(batch, "r2", 2248750)  # Historical flat pricing remains the default.
    parameters = {"batch_cost_mode": "deepseek_flash_peak", "endpoint": "https://api.deepseek.com/chat/completions"}
    identity = admit(batch, "r2", 2248750, **parameters)  # 0.01 + 17.99 = exactly 18 CNY.
    assert identity["profile_sha256"] == hashlib.sha256(original).hexdigest()
    with pytest.raises(ValueError, match="monetary ceiling"):
        admit(batch, "r2", 2248751, **parameters)
    assert batch["profile"].read_bytes() == original
    assert (work / "competition-config.json").read_bytes() == frozen


def test_peak_mode_without_cache_details_charges_input_miss_not_zero(batch: dict[str, Any]) -> None:
    work, _ = saved(batch)
    parameters = {"batch_cost_mode": "deepseek_flash_peak", "endpoint": "https://api.deepseek.com/chat/completions"}
    assert Decimal(peak(work)["estimated_ceiling_cny"]) == Decimal("0.5")
    admit(batch, "r2", 2187500, **parameters)  # 0.5 + 17.5 = exactly 18 CNY.
    with pytest.raises(ValueError, match="monetary ceiling"):
        admit(batch, "r2", 2187501, **parameters)


def test_peak_mode_still_reserves_full_current_and_explicitly_approved_unknown_caps(batch: dict[str, Any]) -> None:
    saved(batch, state="unknown", cap=250000)
    parameters = {"batch_cost_mode": "deepseek_flash_peak", "endpoint": "https://api.deepseek.com/chat/completions"}
    with pytest.raises(ValueError, match="unknown model cost"):
        admit(batch, "r2", 2000000, **parameters)
    admit(batch, "r2", 2000000, charge_unknown_run_caps=("r1",), **parameters)
    with pytest.raises(ValueError, match="monetary ceiling"):
        admit(batch, "r2", 2000001, charge_unknown_run_caps=("r1",), **parameters)


@pytest.mark.parametrize("state,has_usage", [("reserved", True), ("dispatched", True), ("unknown", True),
                                            ("completed", False)])
def test_peak_helper_rejects_incomplete_or_unknown_ledger(batch: dict[str, Any], state: str, has_usage: bool) -> None:
    work, _ = saved(batch, state=state, usage=has_usage)
    with pytest.raises(ValueError, match="complete known-usage ledger"):
        peak(work)


@pytest.mark.parametrize("changes", [{"endpoint": "https://other.invalid/chat/completions"},
    {"endpoint": "https://api.deepseek.com.attacker.invalid/chat/completions"},
    {"endpoint": None}, {"model_id": "deepseek-reasoner"}])
def test_peak_price_is_not_applied_to_unconfirmed_endpoint_or_model(batch: dict[str, Any], changes: dict[str, Any]) -> None:
    work, _ = saved(batch)
    with pytest.raises(ValueError, match="official endpoint"):
        peak(work, **changes)
    with pytest.raises(ValueError):
        admit(batch, "r2", batch_cost_mode="deepseek_flash_peak", **changes)


def test_peak_mode_rejects_historical_nonofficial_endpoint(batch: dict[str, Any]) -> None:
    saved(batch, endpoint="https://different-provider.invalid/chat/completions")
    with pytest.raises(ValueError, match="official endpoint"):
        admit(batch, "r2", batch_cost_mode="deepseek_flash_peak", endpoint="https://api.deepseek.com/chat/completions")


def test_competition_cache_pricing_is_explicit_and_requires_cloud_batch() -> None:
    root = Path(__file__).resolve().parents[1]
    original = CompetitionConfig.model_validate_json((root / "configs/competition-cloud.template.json").read_bytes())
    assert "batch_cost_mode" not in original.model_dump(mode="json")
    config = original.model_dump(mode="json") | {"batch_budget_file": "configs/b.local.json",
                                                "batch_cost_mode": "deepseek_flash_peak"}
    selected = CompetitionConfig.model_validate(config)
    assert selected.model_dump(mode="json")["batch_cost_mode"] == "deepseek_flash_peak"
    del config["batch_budget_file"]
    with pytest.raises(ValueError, match="explicit cloud batch"):
        CompetitionConfig.model_validate(config)

def _saved_request_unknown(batch: dict[str, Any]) -> tuple[Path, dict[str, Any]]:
    work, store = saved(batch, tokens=1000, output_tokens=10,
        raw_usage={"prompt_tokens": 1000, "completion_tokens": 10, "total_tokens": 1010,
                   "prompt_cache_hit_tokens": 100, "prompt_cache_miss_tokens": 900})
    model = ProviderSettings(provider_id="deepseek", model_id="deepseek-flash", max_output_tokens=4096,
                             thinking_mode="enabled", timeout_seconds=60.0)
    provider = CloudProviderConfig(endpoint="https://api.deepseek.com/chat/completions",
        credential_env="DEEPSEEK_API_KEY", credential_file=str(batch["credential"]))
    adapter = CloudChatCompletionsAdapter(model, provider, allow_network=False)
    request = adapter.prepare([HumanMessage(content="Review the supplied source")], [])
    intent = ModelCallIntent(call_id="call2", task_id="task-r1", run_id="r1", kind="solver",
        provider_id="deepseek", model_id="deepseek-flash", prompt_sha256=sha256(canonical(request.messages)),
        tool_schema_sha256=sha256(canonical(request.tools)), parameters_sha256=sha256(canonical({
            "wire_parameters": request.parameters, "body_sha256": request.body_sha256,
            "profile_sha256": request.profile_sha256})), tool_names=())
    store.reserve(intent, max_calls=2)
    store.dispatch("call2")
    store.mark_unknown("call2", "response_lost")
    frozen = json.loads((work / "competition-config.json").read_bytes())
    frozen["config"]["model"] = model.model_dump(mode="json")
    frozen["task"]["source_snapshot_sha256"] = sha256("original source")
    (work / "competition-config.json").write_text(canonical(frozen), encoding="utf-8")
    inputs = work / "solver-inputs"
    inputs.mkdir()
    (inputs / "call2.json").write_text(canonical({"schema_version": 1, "task_id": "task-r1", "run_id": "r1",
        "source_snapshot_sha256": frozen["task"]["source_snapshot_sha256"], "parent_call_id": "call2",
        "solver_intent": intent.model_dump(mode="json"), "model_profile_json": canonical(adapter.profile()),
        "prepared_request": request.model_dump(mode="json")}), encoding="utf-8")
    report = {"run_id": "r1", "task_id": "task-r1", "cost": store.usage_summary("task-r1", "r1"), "status": "unresolved"}
    report["report_sha256"] = sha256(canonical(report))
    (work / "competition-report.json").write_text(canonical(report), encoding="utf-8")
    (work / "run-state.json").write_text(canonical({"task": "competition", "run_id": "r1", "status": "finished",
        "finished_at": "2026-10-18T04:22:26+00:00"}), encoding="utf-8")
    return work, frozen


def test_explicit_request_cap_charges_known_usage_and_original_reservation_readonly(batch: dict[str, Any]) -> None:
    work, frozen = _saved_request_unknown(batch)
    before = {path.relative_to(work).as_posix(): path.read_bytes() for path in work.rglob("*") if path.is_file()}
    charge = unknown_request_cap_cost(work, frozen, run_id="r1", reservation_rate=Decimal(8))
    assert charge["known_cost"]["estimated_ceiling_cny"] == "0.001884"
    assert charge["usage_complete"] is False
    assert charge["unknown_request_reservations"][0]["call_id"] == "call2"
    assert Decimal(charge["charged_cny"]) < Decimal(4)
    limit = int(((Decimal(18) - Decimal(charge["charged_cny"])) * 1000000 / 8).to_integral_value(rounding=ROUND_FLOOR))
    flags = {"charge_unknown_request_caps": ("r1",), "batch_cost_mode": "deepseek_flash_peak",
             "endpoint": "https://api.deepseek.com/chat/completions"}
    admit(batch, "r2", cap=limit, **flags)
    with pytest.raises(ValueError, match="monetary ceiling"):
        admit(batch, "r2", cap=limit + 1, **flags)
    with pytest.raises(ValueError, match="unknown model cost"):
        admit(batch, "r2")
    assert before == {path.relative_to(work).as_posix(): path.read_bytes() for path in work.rglob("*") if path.is_file()}


@pytest.mark.parametrize("failure", ["missing_request", "wrong_owner", "wrong_model", "changed_body",
    "changed_intent", "running", "active", "missing_terminal", "source", "original_cap"])
def test_request_cap_requires_closed_original_bound_request(batch: dict[str, Any], failure: str) -> None:
    work, frozen = _saved_request_unknown(batch)
    assert unknown_request_cap_cost(work, frozen, run_id="r1", reservation_rate=Decimal(8))["usage_complete"] is False
    path = work / "solver-inputs/call2.json"
    value = json.loads(path.read_bytes())
    if failure == "missing_request":
        path.unlink()
    elif failure == "wrong_owner":
        value["run_id"] = "r3"
    elif failure == "wrong_model":
        frozen["config"]["model"]["max_output_tokens"] = 8192
    elif failure == "changed_body":
        value["prepared_request"]["body"] += " "
    elif failure == "changed_intent":
        value["solver_intent"]["prompt_sha256"] = sha256("other prompt")
    elif failure == "running":
        (work / "run-state.json").write_text(canonical({"status": "running", "run_id": "r1"}), encoding="utf-8")
    elif failure == "active":
        (batch["managed"] / "state/active.json").write_text(canonical({"competition_work_id": "r1"}), encoding="utf-8")
    elif failure == "missing_terminal":
        (work / "run-state.json").unlink()
    elif failure == "source":
        value["source_snapshot_sha256"] = sha256("other source")
    else:
        frozen["config"]["max_known_tokens"] = 1
    if failure != "missing_request":
        path.write_text(canonical(value), encoding="utf-8")
    with pytest.raises((ValueError, OSError)):
        unknown_request_cap_cost(work, frozen, run_id="r1", reservation_rate=Decimal(8))


@pytest.mark.parametrize("request_caps,full_caps,mode", [(("r1",), (), "flat8"), (("r1",), ("r1",), "deepseek_flash_peak"),
    (("r2",), (), "deepseek_flash_peak"), (("r1", "r1"), (), "deepseek_flash_peak"), (("outside",), (), "deepseek_flash_peak")])
def test_request_acceptance_scope_and_pricing_are_explicit(batch: dict[str, Any], request_caps, full_caps, mode) -> None:
    _saved_request_unknown(batch)
    with pytest.raises(ValueError):
        admit(batch, "r2", charge_unknown_request_caps=request_caps, charge_unknown_run_caps=full_caps,
              batch_cost_mode=mode, endpoint="https://api.deepseek.com/chat/completions")
