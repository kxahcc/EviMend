"""A named selector factor cannot relax model, resource or compaction equality."""

from __future__ import annotations

import copy
import shutil
from pathlib import Path

import pytest
from pydantic import ValidationError
from test_agent_loop import response
from test_exporting import managed_inputs
from test_metrics import (
    configuration,
    make_task,
    one_manifest,
    read_json,
    run_synthetic,
    task_record,
    tree_bytes,
)

from dfx_agent import exporting, metrics
from dfx_agent.metrics import (
    aggregate_evaluation,
    parse_evaluation_manifest,
    shared_context_profile,
)
from dfx_agent.model_calls import canonical, sha256
from dfx_agent.working_context_plan import WorkingContextPolicy


def v2_configuration(session):
    result = configuration(session)
    result["context_profile_sha256"] = sha256(canonical(shared_context_profile(session["context_profile"])))
    result["working_context"] = {key: value for key, value in session["context_profile"]["host_working_context"].items()
                                 if key != "enabled"}
    return result


@pytest.fixture(scope="module")
def paired_context(tmp_path_factory):
    root = tmp_path_factory.mktemp("working-context-metrics")
    task = make_task(root / "tasks/task1")
    for enabled in (False, True):
        run_id = "on" if enabled else "off"
        run_synthetic(task, root / "work" / run_id, run_id, [response(1)],
                      working_context_policy=WorkingContextPolicy(enabled=enabled))
    session = read_json(root / "work/off/session.json")
    raw = {"schema_version": 2, "evaluation_id": "working-context-pair", "scope": "offline_synthetic",
        "configuration": v2_configuration(session), "tasks": [task_record(root, task, session)],
        "variants": [{"variant_id": run_id, "policy_features": session["policy_features"],
                      "working_context_enabled": enabled} for run_id, enabled in (("off", False), ("on", True))],
        "repeat_ids": ["repeat1"],
        "runs": [{"task_id": "task1", "variant_id": run_id, "repeat_id": "repeat1", "run_id": run_id,
                  "work_path": "work/" + run_id} for run_id in ("off", "on")]}
    return root, raw


def test_explicit_context_factor_comparison_keeps_complete_evidence_and_cost(paired_context) -> None:
    root, raw = paired_context
    before = tree_bytes(root)
    result = aggregate_evaluation(parse_evaluation_manifest(raw), root)
    assert result["schema_version"] == 2
    rows = {item["variant_id"]: item for item in result["rows"]}
    assert rows["off"]["working_context_enabled"] is False
    assert rows["on"]["working_context_enabled"] is True
    assert rows["off"]["working_context_plans"] == []
    assert len(rows["on"]["working_context_plans"]) == 1
    assert rows["off"]["cost"]["reserved_calls"] == rows["on"]["cost"]["reserved_calls"] == 1
    assert rows["off"]["solver_tool_schema_sha256"] == rows["on"]["solver_tool_schema_sha256"]
    assert all(value is None for value in result["application_metrics"].values())
    assert tree_bytes(root) == before


@pytest.mark.parametrize("field", ["max_packet_bytes", "max_view_bytes", "retain_groups"])
def test_variant_cannot_supply_its_own_context_resource_parameters(paired_context, field: str) -> None:
    _root, raw = paired_context
    changed = copy.deepcopy(raw)
    changed["variants"][1][field] = changed["configuration"]["working_context"][field]
    with pytest.raises(ValidationError):
        parse_evaluation_manifest(changed)


@pytest.mark.parametrize("change", ["factor", "parameters", "model", "compaction", "budget", "tools", "search"])
def test_explicit_factor_does_not_relax_any_other_fair_comparison_gate(
    paired_context, monkeypatch: pytest.MonkeyPatch, change: str,
) -> None:
    root, raw = paired_context
    original = metrics._load

    def modified(path, *args, **kwargs):
        value = original(path, *args, **kwargs)
        # Evidence I/O uses a Windows long-path prefix; compare the frozen
        # relative suffix so the mutation actually reaches the intended cell.
        if path.parts[-3:] == ("work", "on", "session.json"):
            value = copy.deepcopy(value)
            if change == "factor":
                value["context_profile"]["host_working_context"]["enabled"] = False
            elif change == "parameters":
                value["context_profile"]["host_working_context"]["retain_groups"] += 1
            elif change == "model":
                value["model_profile"]["model_id"] = "different"
            elif change == "compaction":
                value["context_profile"]["host_compaction"]["enabled"] = True
            elif change == "budget":
                value["max_calls"] += 1
            elif change == "tools":
                value["tool_schema_sha256"] = "0" * 64
            else:
                value["search_limits"]["max_matches"] = 999
        return value

    monkeypatch.setattr(metrics, "_load", modified)
    with pytest.raises(ValueError):
        aggregate_evaluation(parse_evaluation_manifest(raw), root)


def test_schema1_current_off_run_remains_supported_without_new_variant_defaults(paired_context) -> None:
    root, _raw = paired_context
    task = root / "tasks/task1/input"
    raw = one_manifest(root, task, root / "work/off", "off")
    model = parse_evaluation_manifest(raw)
    frozen = canonical(model.model_dump(mode="json"))
    assert "working_context_enabled" not in frozen and '"working_context":' not in frozen
    result = aggregate_evaluation(model, root)
    assert result["schema_version"] == 1 and "working_context_enabled" not in result["rows"][0]


def test_schema1_cannot_enable_unregistered_context_factor(paired_context) -> None:
    root, _raw = paired_context
    task = root / "tasks/task1/input"
    raw = one_manifest(root, task, root / "work/on", "on")
    with pytest.raises(ValueError, match="cannot enable"):
        aggregate_evaluation(parse_evaluation_manifest(raw), root)


@pytest.mark.parametrize("missing", ["working_context", "working_context_enabled"])
def test_schema2_requires_explicit_shared_parameters_and_variant_factor(paired_context, missing: str) -> None:
    _root, raw = paired_context
    changed = copy.deepcopy(raw)
    target = changed["configuration"] if missing == "working_context" else changed["variants"][0]
    target.pop(missing)
    with pytest.raises(ValidationError):
        parse_evaluation_manifest(changed)


def test_shared_profile_normalizes_exactly_one_boolean(paired_context) -> None:
    root, _raw = paired_context
    session = read_json(root / "work/on/session.json")
    before = copy.deepcopy(session["context_profile"])
    normalized = shared_context_profile(session["context_profile"])
    expected = copy.deepcopy(before)
    expected["host_working_context"]["enabled"] = False
    assert normalized == expected and session["context_profile"] == before


def test_version2_export_keeps_selected_send_plan_material_and_verifies_portably(
    paired_context, tmp_path: Path,
) -> None:
    root, raw = paired_context
    target = tmp_path / "export-source"
    shutil.copytree(root, target)
    inputs = managed_inputs(target, copy.deepcopy(raw))
    before = tree_bytes(target / "work/on")
    plan = inputs.freeze()
    receipt = inputs.apply(plan)
    verified = exporting.verify_export(Path(receipt["package_path"]))
    assert verified["aggregate_sha256"] == receipt["aggregate_sha256"]
    assert (Path(receipt["package_path"]) / "payload/work/on/working-context").is_dir()
    assert tree_bytes(target / "work/on") == before


@pytest.mark.parametrize("version", [True, 0, 3, "2"])
def test_schema_dispatch_rejects_unsupported_versions(paired_context, version) -> None:
    _root, raw = paired_context
    changed = copy.deepcopy(raw)
    changed["schema_version"] = version
    with pytest.raises(ValueError):
        parse_evaluation_manifest(changed)
