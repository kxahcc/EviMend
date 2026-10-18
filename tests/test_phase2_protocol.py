"""Freeze the small OOD and method-comparison protocol before model outcomes."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from dfx_agent.competition import CompetitionConfig
from dfx_agent.task import digest, prepare_task, verify_task

ROOT = Path(__file__).resolve().parents[1]
UNKNOWN = ["live1017-022b", "live1017-case17", "live1017-case12", "live1017-retry20"]


@pytest.mark.skipif(os.environ.get("DFX_PHASE2_PREPARE") != "1", reason="local protocol freezing not requested")
def test_freeze_small_model_protocol_before_outcomes() -> None:
    protocol_path = ROOT / ".dfx-local/state/phase2-protocol.json"
    if protocol_path.exists():
        protocol = json.loads(protocol_path.read_bytes())
        for row in protocol["runs"]:
            assert digest((ROOT / row["config"]).read_bytes()) == row["config_sha256"]
            assert verify_task(ROOT / row["task"])["source_snapshot_sha256"] == row["source_snapshot_sha256"]
        return
    cases = json.loads((ROOT / ".dfx-local/state/full-run-map.json").read_bytes())["entries"]
    rows = []

    def save(config: dict, task: Path, name: str, category: str, variant: str, expectation: str) -> None:
        assert not (ROOT / ".dfx-local/work" / ("competition-" + name)).exists()
        config["charge_unknown_run_caps"] = []
        config["charge_unknown_request_caps"] = UNKNOWN
        config["model"]["max_output_tokens"] = 8192
        config["max_known_tokens"] = 100000 if category == "ood" else 600000
        config["max_calls"] = 8 if category == "ood" else 40
        config["max_tool_calls"] = 20 if category == "ood" else 100
        config["max_seconds"] = 180 if category == "ood" else 900
        assert config["working_context"]["enabled"] is False
        CompetitionConfig.model_validate(config)
        destination = ROOT / "configs/generated" / f"phase2-{name}.local.json"
        destination.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        task_info = verify_task(task)
        rows.append({"run_id": name, "category": category, "variant": variant,
            "task_id": task_info["task_id"], "task": task.relative_to(ROOT).as_posix(),
            "source_snapshot_sha256": task_info["source_snapshot_sha256"],
            "crash_sha256": task_info["crash_sha256"], "config": destination.relative_to(ROOT).as_posix(),
            "config_sha256": digest(destination.read_bytes()), "expectation": expectation})

    template = json.loads((ROOT / cases[-1]["config"]).read_bytes())
    for label, name, log in [("empty-log", "live1017-021a", b""),
                             ("native-only", "live1017-022a", b"Error name:SIGSEGV\n#00 pc 0x123 libnative.so\n")]:
        data = ROOT / ".dfx-local/tmp/evaluation-ood" / label
        (data / "app/src").mkdir(parents=True, exist_ok=True)
        (data / "app/src/main.ets").write_text(
            "export function add(left: number, right: number): number { return left + right; }\n", encoding="utf-8")
        (data / "fault.log").write_bytes(log)
        task = ROOT / ".dfx-local/tmp/reviewed" / ("ood-" + label)
        prepare_task(data, {"task_id": "ood-" + label, "app": "app",
            "log": {"path": "fault.log", "sha256": digest(log)}}, task)
        config = json.loads(json.dumps(template))
        config["execution"] = None
        save(config, task, name, "ood", label,
             "No supported source root cause, no patch and no claimed verification; explain missing source evidence")

    variants = [("general", False, False), ("guided", True, True), ("skill", True, False)]
    run_names = iter(["live1017-case27", "live1017-case28", "live1017-retry21",
                      "live1017-retry22", "live1017-retry23", "live1017-retry24"])
    for case_id in ("b3-019-exam1", "b3-021-audioediting1"):
        entry = next(row for row in cases if row["case_id"] == case_id)
        group = []
        for variant, skills, guidance in variants:
            config = json.loads((ROOT / entry["config"]).read_bytes())
            config["static_skills_enabled"] = skills
            config["policy_features"] = dict.fromkeys(("rule_guidance", "obligation_tracking", "decision_checks"), guidance)
            save(config, ROOT / entry["task"], next(run_names), "ablation", variant,
                 "Same fixed source protocol; count every completed, failed or limited run; no post-outcome selection")
            comparable = json.loads(json.dumps(config))
            comparable.pop("static_skills_enabled")
            comparable.pop("policy_features")
            group.append(comparable)
        assert group[0] == group[1] == group[2], "unexpected method/configuration difference"
    protocol = {"schema_version": 1, "frozen_at_utc": datetime.now(UTC).isoformat(),
        "purpose": "Small competition pilot, not significant or generalizable paper evidence",
        "order": "Two model OOD tasks before two cases x general/guided/skill; shared per-case budgets",
        "selection": "Recursion termination/order and typed-array PCM bounds: distinct fault families with admitted protocols",
        "knowledge": "Same domain corpus, tools and full Skill bodies accessible; only catalog injection and optional domain policy differ",
        "history": "Complete thinking history in every group; working_context disabled; no frontier/context-pruning claim",
        "metrics": ["source pass", "candidate attempts", "model/tool calls", "cache-hit/miss input", "output tokens", "estimated CNY", "wall time", "actual mechanism consumption"],
        "stop": "Existing total monetary gate, unknown response gate, resource/time limits; never silently retry or relax",
        "runtime_sources": {p.relative_to(ROOT).as_posix(): digest(p.read_bytes())
            for folder in ("src/dfx_agent", "skill", "resources/source-validation")
            for p in sorted((ROOT / folder).rglob("*")) if p.is_file() and "__pycache__" not in p.parts},
        "runs": rows}
    protocol_path.write_text(json.dumps(protocol, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
