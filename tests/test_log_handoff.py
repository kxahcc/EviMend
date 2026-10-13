"""Encoded logs retain raw bytes and line anchors through the repair SDK."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from test_agent_loop import response, task_snapshot
from test_decisions import FULL, diagnosis_submission
from test_working_context_handoff import checked_completion
from test_working_context_plan import SyntheticHost

from dfx_agent import agent, diagnostics
from dfx_agent.agent import run_fixed_agent
from dfx_agent.logs import decode_text, parse_crash, saved_log_excerpt_matches
from dfx_agent.task import digest, verify_task
from dfx_agent.verification import FixedTextExecutor
from dfx_agent.working_context_plan import WorkingContextPolicy

LINES = ["系统日志", "Error name: RangeError", "at consume (src/main.ets:1:1)", "操作完成"]
TEXT = "\r\n".join(LINES) + "\r\n"


def encoded_log(kind: str) -> tuple[bytes, str, list[str]]:
    if kind == "utf16":
        return TEXT.encode("utf-16"), "utf-16", []
    if kind == "utf16be":
        return b"\xfe\xff" + TEXT.encode("utf-16-be"), "utf-16", []
    if kind == "utf8bom":
        return b"\xef\xbb\xbf" + TEXT.encode(), "utf-8-sig", []
    if kind == "gb18030":
        return TEXT.encode("gb18030"), "gb18030", []
    if kind == "broken_utf16":
        return TEXT.encode("utf-16") + b"\x00", "utf-16", ["invalid_utf16_bytes"]
    if kind == "broken_utf8":
        return b"\xff\r\n" + ("\r\n".join(LINES[1:]) + "\r\n").encode(), "utf-8", ["undecodable_bytes_replaced"]
    raise ValueError("unknown synthetic encoding")


def log_submission(task: Path) -> dict:
    raw = diagnosis_submission(task)
    raw["log_citations"] = [{"evidence_id": "log1", "start_line": 2, "end_line": 3}]
    raw["diagnosis"]["hypotheses"][0]["support_evidence_ids"].append("log1")
    return raw


@pytest.mark.parametrize("kind", ["utf16", "utf16be", "utf8bom", "gb18030", "broken_utf16", "broken_utf8"])
def test_tool_admission_and_frozen_context_use_the_same_decoded_line_anchors(tmp_path: Path, kind: str) -> None:
    data, encoding, warnings = encoded_log(kind)
    host = SyntheticHost(tmp_path, crash_bytes=data, history_selection="repair_frontier", context_layout="decision_frontier")
    first = host.step("read_log", {"start_line": 2, "end_line": 2})
    second = host.step("read_log", {"start_line": 3, "end_line": 3})
    assert first == {"start_line": 2, "lines": [LINES[1]], "encoding": encoding, "warnings": warnings}
    assert second["lines"] == [LINES[2]] and second["warnings"] == warnings
    crash = parse_crash(data)
    assert crash.sha256 == digest(data) and crash.encoding == encoding
    assert crash.error_name == "RangeError" and crash.frames[0].log_line == 3
    host.step("read_source", {"path": "src/main.ets"})
    registered = host.step("record_diagnosis", {"diagnosis_json": json.dumps(log_submission(host.task))})
    assert registered["diagnosis_id"] == "diagnosis1"
    host.step(None, text="Retain the uncertain hypothesis and original log scope.")
    selected = host.plan()
    fact = json.loads(selected.packet_json)["canonical_payload_json"]
    fact = json.loads(fact)["evidence"]["log1"]
    assert fact["statement"] == "\n".join(LINES[1:3])
    assert fact["log_line"] == 2 and fact["artifact_sha256"] == digest(data)
    frozen_path = host.store.directory / f"{selected.parent_call_id}.plan.json"
    before = frozen_path.read_bytes()
    assert host.store._read_plan(frozen_path) == selected
    assert frozen_path.read_bytes() == before and (host.task / "crash.log").read_bytes() == data


@pytest.mark.parametrize("data", [b"\xef\xbb\xbfheader\nsecond", b"header\n\xd6\xd0\nend"])
def test_saved_legacy_decoding_is_read_only_and_does_not_rewrite_evidence(tmp_path: Path, monkeypatch, data: bytes) -> None:
    host = SyntheticHost(tmp_path, crash_bytes=data, history_selection="repair_frontier")

    def legacy(raw: bytes) -> tuple[str, str, list[str]]:
        return raw.decode("utf-8", errors="replace"), "utf-8", []

    with monkeypatch.context() as patch:
        patch.setattr(agent, "decode_text", legacy)
        patch.setattr(diagnostics, "decode_text", legacy)
        host.step("read_log", {})
        host.step("read_source", {"path": "src/main.ets"})
        raw = diagnosis_submission(host.task)
        raw["log_citations"] = [{"evidence_id": "log1", "start_line": 1, "end_line": 2}]
        raw["diagnosis"]["hypotheses"][0]["support_evidence_ids"].append("log1")
        host.step("record_diagnosis", {"diagnosis_json": json.dumps(raw)})
        selected = host.plan()
    saved = host.store.directory / f"{selected.parent_call_id}.plan.json"
    before = saved.read_bytes()
    original = "\n".join(data.decode("utf-8", errors="replace").splitlines()[:2])
    assert original != "\n".join(decode_text(data)[0].splitlines()[:2])
    reread = host.store._read_plan(saved)
    assert reread == selected and saved.read_bytes() == before
    assert json.loads(json.loads(reread.packet_json)["canonical_payload_json"])["evidence"]["log1"]["statement"] == original
    assert (host.task / "crash.log").read_bytes() == data


@pytest.mark.parametrize("kind", ["utf16", "gb18030"])
def test_real_sdk_encoded_log_diagnosis_completion_and_resume(tmp_path: Path, kind: str) -> None:
    task = task_snapshot(tmp_path)
    data, _, _ = encoded_log(kind)
    (task / "crash.log").write_bytes(data)
    metadata = json.loads((task / "task.json").read_bytes())
    metadata["crash_sha256"] = digest(data)
    (task / "task.json").write_text(json.dumps(metadata), encoding="utf-8")
    verify_task(task)
    executor = FixedTextExecutor("src/main.ets", "value=good\n")
    policy = WorkingContextPolicy(enabled=True, history_selection="repair_frontier", context_layout="decision_frontier")
    replies = [response(1, "read_log", {"start_line": 2, "end_line": 3}),
               response(2, "read_source", {"path": "src/main.ets"}),
               response(3, "record_diagnosis", {"diagnosis_json": json.dumps(log_submission(task))}),
               response(4, "inspect_analysis", {"diagnosis_id": "diagnosis1"}), response(5)]
    work = tmp_path / "run"
    report = run_fixed_agent(task, work, "run1", replies, executor,
                             policy_features=FULL, working_context_policy=policy)
    assert report["model_calls_this_turn"] == 5 and report["stopped_reason"] is None
    assert report["application_verification"] == "not_run" and not executor.calls
    assert checked_completion(task, work) == report["completion"]
    saved = (work / "model-calls.sqlite").read_bytes()
    resumed = run_fixed_agent(task, work, "run1", replies, executor, resume=True,
                              policy_features=FULL, working_context_policy=policy)
    assert resumed["model_calls_this_turn"] == 0 and resumed["completion"] == report["completion"]
    assert (work / "model-calls.sqlite").read_bytes() == saved and (task / "crash.log").read_bytes() == data


@pytest.mark.parametrize(("start", "statement"), [(0, "header"), (3, "header"), (1, "not the header"), (1, "\n" * 200)])
def test_saved_excerpt_rejects_wrong_anchors_text_and_oversized_spans(start: int, statement: str) -> None:
    assert not saved_log_excerpt_matches(b"header\nsecond", start, statement)
