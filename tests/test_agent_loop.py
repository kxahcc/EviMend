"""Fixed-response acceptance tests for the offline controller boundary."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage

from dfx_agent.agent import run_fixed_agent
from dfx_agent.task import PROFILE, digest, snapshot_digest
from dfx_agent.verification import CheckLedger, FixedTextExecutor
from dfx_agent.workspace import CandidateWorkspace, TextEdit


def task_snapshot(root: Path) -> Path:
    task = root / "input"
    source = task / "source/src"
    source.mkdir(parents=True)
    (source / "main.ets").write_bytes(b"value=bad\n")
    manifest = [{"path": "src/main.ets", "sha256": digest(b"value=bad\n")}]
    (task / "source-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (task / "crash.log").write_text("synthetic crash", encoding="utf-8")
    (task / "task.json").write_text(json.dumps({
        "schema_version": 1, "profile": PROFILE, "task_id": "task1",
        "source_root": "source", "source_manifest": "source-manifest.json",
        "source_snapshot_sha256": snapshot_digest(manifest),
        "crash_log": "crash.log", "crash_sha256": digest(b"synthetic crash"),
    }), encoding="utf-8")
    return task


def candidate_id(task: Path, root: Path, replacement: str) -> str:
    workspace = CandidateWorkspace(task, root, "derive")
    workspace.start_draft()
    workspace.apply_text_edits([TextEdit(path="src/main.ets", old="bad", new=replacement)])
    return workspace.register_candidate(1).candidate_id


def response(index: int, name: str | None = None, args: dict | None = None,
             *, usage: bool = True) -> AIMessage:
    return AIMessage(
        content="done" if name is None else "",
        tool_calls=[] if name is None else [{"name": name, "args": args or {}, "id": f"call-{index}"}],
        usage_metadata={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15} if usage else None,
    )


def test_direct_candidate_is_only_offline_checked(tmp_path: Path) -> None:
    task = task_snapshot(tmp_path)
    good = candidate_id(task, tmp_path / "derive", "good")
    scripted = [
        response(1, "read_log"),
        response(2, "record_diagnosis", {"path": "src/main.ets", "line": 1, "reason": "bad value"}),
        response(3, "edit_source", {"path": "src/main.ets", "old": "bad", "new": "good"}),
        response(4, "register_candidate", {"diagnosis_id": "diagnosis-1"}),
        response(5, "run_check", {"candidate_id": good}), response(6),
    ]
    executor = FixedTextExecutor("src/main.ets", "value=good\n")
    report = run_fixed_agent(task, tmp_path / "run", "run1", scripted, executor)
    assert report["status"] == "offline_checked"
    assert report["application_verification"] == "not_run"
    assert report["candidates"] == [good]
    assert report["checks"][0]["status"] == "pass"
    assert report["model_calls_this_turn"] == 6
    assert report["known_tokens_this_turn"] == 90
    assert {"read_log", "read_source", "edit_source", "register_candidate", "run_check"} <= set(report["offered_tools"])
    assert not {"execute", "write_file", "edit_file", "task"} & set(report["offered_tools"])
    assert len(executor.calls) == 1
    assert json.loads((tmp_path / "run/diagnoses/diagnosis-1.json").read_text())["diagnosis"]["hypotheses"][0]["support_status"] == "unknown"


def test_failed_first_candidate_revised_from_base(tmp_path: Path) -> None:
    task = task_snapshot(tmp_path)
    almost = candidate_id(task, tmp_path / "derive1", "almost")
    good = candidate_id(task, tmp_path / "derive2", "good")
    scripted = [
        response(1, "edit_source", {"path": "src/main.ets", "old": "bad", "new": "almost"}),
        response(2, "register_candidate"), response(3, "run_check", {"candidate_id": almost}),
        response(4, "edit_source", {"path": "src/main.ets", "old": "bad", "new": "good"}),
        response(5, "register_candidate"), response(6, "run_check", {"candidate_id": good}), response(7),
    ]
    report = run_fixed_agent(task, tmp_path / "run", "run1", scripted,
                             FixedTextExecutor("src/main.ets", "value=good\n",
                                               failure_detail="synthetic compiler rejected first candidate"))
    assert report["candidates"] == [almost, good]
    assert [item["status"] for item in report["checks"]] == ["fail", "pass"]
    assert "compiler" in report["checks"][0]["detail"]
    assert report["status"] == "offline_checked"


def test_no_candidate_and_usage_unknown(tmp_path: Path) -> None:
    task = task_snapshot(tmp_path)
    report = run_fixed_agent(task, tmp_path / "run", "run1", [response(1, usage=False)],
                             FixedTextExecutor("src/main.ets", "value=good\n"))
    assert report["status"] == "unresolved"
    assert report["candidates"] == []
    assert report["known_tokens_this_turn"] == 0
    assert report["missing_usage_calls_this_turn"] == 1


def test_format_retry_and_call_budget(tmp_path: Path) -> None:
    task = task_snapshot(tmp_path)
    report = run_fixed_agent(task, tmp_path / "run", "run1", [
        response(1, "edit_source", {"path": "src/main.ets", "old": "missing", "new": "good"}),
        response(2, "edit_source", {"path": "src/main.ets", "old": "bad", "new": "good"}),
        response(3, "register_candidate"), response(4),
    ], FixedTextExecutor("src/main.ets", "value=good\n"), max_calls=2)
    assert report["status"] == "budget_exhausted"
    assert report["model_calls_this_turn"] == 2
    assert report["candidates"] == []
    assert (tmp_path / "run/draft/src/main.ets").read_text() == "value=good\n"


def test_timeout_before_first_model_call(tmp_path: Path) -> None:
    task = task_snapshot(tmp_path)
    report = run_fixed_agent(task, tmp_path / "run", "run1", [response(1)],
                             FixedTextExecutor("src/main.ets", "value=good\n"), max_seconds=0.000001)
    assert report["status"] == "budget_exhausted"
    assert report["stopped_reason"] == "wall_time"


def test_invalid_tool_arguments_require_a_new_model_call(tmp_path: Path) -> None:
    task = task_snapshot(tmp_path)
    report = run_fixed_agent(task, tmp_path / "run", "run1", [
        response(1, "record_diagnosis", {"path": "src/main.ets", "line": "wrong", "reason": "bad value"}),
        response(2, "record_diagnosis", {"path": "src/main.ets", "line": 1, "reason": "bad value"}),
        response(3),
    ], FixedTextExecutor("src/main.ets", "value=good\n"))
    assert report["model_calls"] == 3
    assert (tmp_path / "run/diagnoses/diagnosis-1.json").is_file()
    assert report["status"] == "unresolved"


def test_resume_cannot_reset_model_budget(tmp_path: Path) -> None:
    task = task_snapshot(tmp_path)
    executor = FixedTextExecutor("src/main.ets", "value=good\n")
    first = run_fixed_agent(task, tmp_path / "run", "run1", [response(1)], executor, max_calls=1)
    assert first["model_calls"] == 1
    second = run_fixed_agent(task, tmp_path / "run", "run1", [response(1)], executor,
                             resume=True, max_calls=1)
    assert second["status"] == "budget_exhausted"
    assert second["model_calls"] == 1
    assert second["model_calls_this_turn"] == 0


def test_resume_rejects_changed_check_protocol(tmp_path: Path) -> None:
    task = task_snapshot(tmp_path)
    run_fixed_agent(task, tmp_path / "run", "run1", [response(1)],
                    FixedTextExecutor("src/main.ets", "value=good\n"))
    with pytest.raises(ValueError, match="session identity"):
        run_fixed_agent(task, tmp_path / "run", "run1", [response(1)],
                        FixedTextExecutor("src/main.ets", "value=different\n"), resume=True)


def test_known_token_budget_stops_next_call(tmp_path: Path) -> None:
    task = task_snapshot(tmp_path)
    report = run_fixed_agent(task, tmp_path / "run", "run1", [response(1, "list_source"), response(2)],
                             FixedTextExecutor("src/main.ets", "value=good\n"), max_known_tokens=15)
    assert report["status"] == "budget_exhausted"
    assert report["model_calls"] == 1 and report["known_tokens"] == 15


def test_pending_check_reconciles_with_same_key(tmp_path: Path) -> None:
    task = task_snapshot(tmp_path)
    workspace = CandidateWorkspace(task, tmp_path / "run", "run1")
    workspace.start_draft()
    workspace.apply_text_edits([TextEdit(path="src/main.ets", old="bad", new="good")])
    candidate = workspace.register_candidate(1)
    executor = FixedTextExecutor("src/main.ets", "value=good\n")
    ledger = CheckLedger(workspace, executor)
    key = digest(f"run1:{candidate.candidate_id}:offline:{executor.protocol_sha256}".encode())
    (ledger.root / f"{key}.json").write_text(json.dumps({"state": "pending", "idempotency_key": key,
        "candidate_id": candidate.candidate_id, "check_id": "offline",
        "protocol_sha256": executor.protocol_sha256}), encoding="utf-8")
    assert executor.check(key=key, source=workspace.require_registered(candidate.candidate_id))[0] == "pass"
    reopened = CandidateWorkspace.reopen(task, tmp_path / "run", "run1")
    result = CheckLedger(reopened, executor).run(candidate.candidate_id)
    assert result.status == "pass" and result.idempotency_key == key
    assert CheckLedger(reopened, executor).run(candidate.candidate_id) == result
    assert executor.calls == [key]
    assert executor.requests == [key, key]
