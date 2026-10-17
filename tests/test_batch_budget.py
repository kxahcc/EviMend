"""No-network tests for the shared batch ceiling over existing model ledgers."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessage

from dfx_agent import competition
from dfx_agent.batch_budget import BatchBudget, check_batch_budget
from dfx_agent.competition import CompetitionConfig
from dfx_agent.model_calls import ModelCallIntent, ModelCallStore, canonical, sha256


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
          terminal: bool = True, binding: dict[str, Any] | None = None) -> tuple[Path, ModelCallStore]:
    identity = binding if binding is not None else admit(batch, run_id, cap)
    work = batch["managed"] / "work" / ("competition-" + run_id)
    work.mkdir()
    task_id = "task-" + run_id
    config = {"model_mode": "cloud", "model": {"model_id": "deepseek-flash"},
              "provider": {"credential_file": str(batch["credential"])}, "max_known_tokens": cap}
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
            {"input_tokens": tokens, "output_tokens": 0, "total_tokens": tokens} if usage else None)))
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
