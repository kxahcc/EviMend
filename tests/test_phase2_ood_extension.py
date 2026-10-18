"""Freeze additional model OOD tasks before any outcome is available."""

from __future__ import annotations

import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path

import pytest

from dfx_agent.batch_budget import check_batch_budget
from dfx_agent.competition import CompetitionConfig
from dfx_agent.task import digest, prepare_task, verify_task
from dfx_agent.workspace import _copy_tree

ROOT = Path(__file__).resolve().parents[1]
BENIGN = "export function add(left: number, right: number): number { return left + right; }\n"


@pytest.mark.skipif(os.environ.get("DFX_OOD_EXTENSION_PREPARE") != "1", reason="local model protocol preparation not requested")
def test_freeze_six_additional_ood_scenarios() -> None:
    public = ROOT / "runs/exports/phase2"
    protocol_path = ROOT / ".dfx-local/state/phase2-protocol.json"
    protocol = json.loads(protocol_path.read_bytes())
    if any(row["variant"] == "pcm-log-metamorphic" for row in protocol["runs"]):
        for row in protocol["runs"]:
            assert digest((ROOT / row["config"]).read_bytes()) == row["config_sha256"]
            verify_task(ROOT / row["task"])
        return
    assert len(protocol["runs"]) == 9
    template = json.loads((ROOT / "configs/generated/phase2-native-confirmation.local.json").read_bytes())
    cases = [
        ("binary-log", "live1017-retry07", {"src/main.ets": BENIGN}, b"\x00\xff\x80\xfe\x00\x81garbled\x00\xff"),
        ("missing-source", "live1017-retry08", {"src/main.ets": BENIGN},
         b"Error name:TypeError\nError message:cannot read property x of undefined\nStacktrace:\n    at load (entry/src/main/ets/pages/Missing.ets:21:7)\n"),
        ("ambiguous-module", "live1017-retry09", {"moduleA/Index.ets": BENIGN, "moduleB/Index.ets": BENIGN},
         b"Error name:TypeError\nError message:cannot read property x of undefined\nStacktrace:\n    at load (Index.ets:1:7)\n"),
        ("line-version-mismatch", "live1017-retry11", {"src/main.ets": BENIGN},
         b"Error name:TypeError\nError message:cannot read property x of undefined\nStacktrace:\n    at load (src/main.ets:999:7)\n"),
        ("normal-info-log", "live1017-retry13", {"src/main.ets": BENIGN},
         b"2026-10-18 10:00:00 I/App started\n2026-10-18 10:00:01 I/App add(2,3)=5\n2026-10-18 10:00:02 I/App completed normally\n"),
    ]
    (public / "protocol-before-ood-extension.json").write_bytes(protocol_path.read_bytes())
    frozen_at = datetime.now(UTC).isoformat()

    def save(task: Path, name: str, label: str, config: dict, positive: bool = False) -> None:
        for slot in (ROOT / ".dfx-local/work" / ("competition-" + name),
                     ROOT / ".dfx-local/state" / ("competition-" + name + "-approval.json"),
                     ROOT / ".dfx-local/tmp" / ("competition-" + name + "-library")):
            assert not slot.exists(), f"Experimental slot occupied: {slot.name}"
        config["max_known_tokens"] = 1000000 if positive else 600000
        config["max_calls"] = 40 if positive else 24
        config["max_tool_calls"] = 100 if positive else 60
        config["max_seconds"] = 900 if positive else 600
        config["model"]["max_output_tokens"] = 8192
        parsed = CompetitionConfig.model_validate(config)
        assert not parsed.working_context.enabled
        check_batch_budget(project_root=ROOT, managed_root=ROOT / ".dfx-local",
            work=ROOT / ".dfx-local/work" / ("competition-" + name), run_id=name,
            profile_path=config["batch_budget_file"], model_id=config["model"]["model_id"],
            credential_file=config["provider"]["credential_file"], max_known_tokens=config["max_known_tokens"],
            batch_cost_mode=config["batch_cost_mode"], endpoint=config["provider"]["endpoint"],
            charge_unknown_request_caps=tuple(config["charge_unknown_request_caps"]))
        destination = ROOT / "configs/generated" / f"phase2-{name}.local.json"
        destination.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        task_info = verify_task(task)
        protocol["runs"].append({"run_id": name, "category": "ood_positive" if positive else "ood",
            "variant": label, "task_id": task_info["task_id"], "task": task.relative_to(ROOT).as_posix(),
            "source_snapshot_sha256": task_info["source_snapshot_sha256"], "crash_sha256": task_info["crash_sha256"],
            "config": destination.relative_to(ROOT).as_posix(), "config_sha256": digest(destination.read_bytes()),
            "frozen_at_utc": frozen_at, "expectation":
            "Same real PCM fault/source, transformed log only; candidate passes all four admitted source checks" if positive else
            "Explain absent or mismatched source-fault evidence; normally finish without fabricated root, candidate or verification"})

    for label, name, files, log in cases:
        data = ROOT / ".dfx-local/tmp/evaluation-ood" / label
        for relative, content in files.items():
            path = data / "app" / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        (data / "fault.log").write_bytes(log)
        task = ROOT / ".dfx-local/tmp/reviewed" / ("ood-" + label)
        if not task.exists():
            prepare_task(data, {"task_id": "ood-" + label, "app": "app",
                "log": {"path": "fault.log", "sha256": digest(log)}}, task)
        info = verify_task(task)
        assert info["task_id"] == "ood-" + label and info["crash_sha256"] == digest(log)
        actual = {p.relative_to(task / "source").as_posix(): p.read_text(encoding="utf-8")
                  for p in (task / "source").rglob("*") if p.is_file()}
        assert actual == files
        config = json.loads(json.dumps(template))
        config["execution"] = None
        save(task, name, label, config)

    original = ROOT / ".dfx-local/tmp/reviewed/source-b3-021-audioediting1"
    original_info = verify_task(original)
    text = (original / "crash.log").read_text(encoding="utf-8")
    text = re.sub(r"(?m)^([ \t]*)at ", r"\g<1>#123 at ", text)
    text = "I/Host log stream opened\r\n" + text.replace("\n", "\r\n") + "\r\nI/Host stream closed\r\n"
    log = text.encode("utf-16")
    data = ROOT / ".dfx-local/tmp/evaluation-ood/pcm-log-metamorphic"
    data.mkdir(parents=True, exist_ok=True)
    if not (data / "app").exists():
        _copy_tree(original / original_info["source_root"], data / "app")
    logfile = data / "fault.log"
    logfile.write_bytes(log)
    task = ROOT / ".dfx-local/tmp/reviewed/ood-pcm-log-metamorphic"
    prepare_task(data, {"task_id": "ood-pcm-log-metamorphic", "app": "app",
        "log": {"path": "fault.log", "sha256": digest(log)}}, task)
    assert verify_task(task)["source_snapshot_sha256"] == original_info["source_snapshot_sha256"]
    config = json.loads((ROOT / "configs/generated/full-source-b3-021-audioediting1.local.json").read_bytes())
    config["charge_unknown_run_caps"] = []
    config["charge_unknown_request_caps"] = template["charge_unknown_request_caps"]
    save(task, "live1017-retry14", "pcm-log-metamorphic", config, positive=True)
    protocol["ood_extension"] = {"frozen_at_utc": frozen_at,
        "selection": "Five plausible insufficient-evidence cases plus one positive real-fault metamorphic repair control",
        "negative_max_tokens": 600000, "positive_max_tokens": 1000000,
        "unchanged": "Initial nine observations, six comparison budgets, runtime, corpus and original normal-case selections"}
    protocol_path.write_text(json.dumps(protocol, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (public / "protocol.json").write_bytes(protocol_path.read_bytes())
