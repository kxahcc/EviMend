"""Real slider callback execution plus negative repair controls; no UI/device claims."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from test_enterprise_source_replay import check_exported_candidate
from test_office_source_replay import managed as managed  # noqa: PLC0414
from test_office_source_replay import ordinary_node as ordinary_node  # noqa: PLC0414

from dfx_agent.local_execution import (
    STAGES,
    IndependentOracle,
    LocalApplicationExecutor,
    LocalCheckConfig,
    LocalCommand,
)
from dfx_agent.pilot_preparation import REVIEWED_MOCKS, REVIEWED_ORACLES
from dfx_agent.task import digest, prepare_task
from dfx_agent.verification import CheckLedger
from dfx_agent.workspace import CandidateWorkspace, TextEdit

ROOT = Path(__file__).resolve().parents[1]
FONT = "resources/source-validation/teleprompter-font/oracle.cjs"
MUSIC = "resources/source-validation/music-slider/oracle.cjs"
OPTIONS = "components/teleprompter/src/main/ets/model/PrompterConfigModel.ets"
BAD_MARKER = "const marker = '#'.repeat(Math.floor(Math.log10(this.musicModel.progressMax / remainMs)));"
GOOD_MARKER = "const marker = '#'.repeat(remainMs > 0 ? Math.max(0, Math.floor(Math.log10(this.musicModel.progressMax / remainMs))) : 0);"
FONT_SOURCE = """sliderBuilder({
            sliderValue: this.config.fontSize,
            changeValue: (value) => {
              let level = value / (this.configList.fontSizeList[1] - this.configList.fontSizeList[0]) * 100;
              hilog.error(0, 'ConfigSheet', 'fontSize' + value.toFixed(level));
              this.config.fontSize = value;
            }
          })
"""
MUSIC_SOURCE = """Slider({})
          .onChange((value: number, mode: SliderChangeMode) => {
            if (mode === SliderChangeMode.End || mode === SliderChangeMode.Begin) {
              if (this.musicModel.isPlay) {
                const remainMs = this.musicModel.progressMax - value;
                const marker = '#'.repeat(Math.floor(Math.log10(this.musicModel.progressMax / remainMs)));
                Logger.info('PlayerControlArea', `marker: ${marker}`);
              }
              MediaService.getInstance().seek(value);
            }
          })
          .height(10)
"""


@pytest.mark.parametrize("oracle,mutation,expected,failed_stage", [
    (FONT, "good", "pass", None),
    (FONT, "no_update", "fail", "target"),
    (FONT, "clamp", "fail", "target"),
    (FONT, "wrong_normal", "fail", "behavior"),
    (MUSIC, "good", "pass", None),
    (MUSIC, "no_seek", "fail", "target"),
    (MUSIC, "wrong_marker", "fail", "behavior"),
    (MUSIC, "still_bad", "fail", "target"),
])
def test_slider_checks_preserve_function_after_removing_exception(
        tmp_path: Path, ordinary_node: Path, managed: Path,
        oracle: str, mutation: str, expected: str, failed_stage: str | None) -> None:
    relative, marker, scope = REVIEWED_ORACLES[oracle]
    data, task = tmp_path / "data", tmp_path / "task"
    source = data / "app" / relative
    source.parent.mkdir(parents=True)
    original = FONT_SOURCE if oracle == FONT else MUSIC_SOURCE
    source.write_text(original, encoding="utf-8")
    if oracle == FONT:
        options = data / "app" / OPTIONS
        options.parent.mkdir(parents=True)
        options.write_text("class Options { fontSizeList: number[] = [10, 100]; }", encoding="utf-8")
        repaired = original.replace("value.toFixed(level)", "String(value)")
        update = "this.config.fontSize = value;"
        if mutation == "no_update":
            repaired = repaired.replace(update, "/* lost font-size update */")
        elif mutation == "clamp":
            repaired = repaired.replace(update, "this.config.fontSize = Math.min(value, 90);")
        elif mutation == "wrong_normal":
            repaired = repaired.replace(update, "this.config.fontSize = value === 100 ? value : 99;")
    else:
        repaired = original.replace(BAD_MARKER, GOOD_MARKER)
        if mutation == "no_seek":
            repaired = repaired.replace("MediaService.getInstance().seek(value);", "/* lost seek */")
        elif mutation == "wrong_marker":
            repaired = repaired.replace(GOOD_MARKER, "const marker = '';")
        elif mutation == "still_bad":
            repaired = original.replace(BAD_MARKER, BAD_MARKER + " /* not fixed */")
    log = data / "fault.log"
    log.write_bytes(b"RangeError: synthetic slider callback\n")
    prepare_task(data, {"task_id": "synthetic-slider", "app": "app",
        "log": {"path": "fault.log", "sha256": digest(log.read_bytes())}}, task)
    workspace = CandidateWorkspace(task, tmp_path / "work", "slider-check")
    workspace.start_draft()
    workspace.apply_text_edits([TextEdit(path=relative, old=original, new=repaired)])
    candidate = workspace.register_candidate(1)
    adapter = ROOT / oracle
    sha = digest(adapter.read_bytes())
    config = LocalCheckConfig(mode="source", project_root=str(data / "app"), validation_scope=scope,
        mocked_interfaces=REVIEWED_MOCKS[oracle], commands={stage: LocalCommand(
            argv=[str(ordinary_node), str(adapter), stage], timeout_seconds=10, oracle_sha256=sha) for stage in STAGES},
        independent_oracles={stage: IndependentOracle(path=str(adapter), sha256=sha) for stage in STAGES},
        baseline_target_exit_codes=[9], baseline_fault_pattern=marker, max_check_seconds=90)
    executor = LocalApplicationExecutor(config, workspace, managed_root=managed)
    checked = CheckLedger(workspace, executor).run(candidate.candidate_id)
    assert checked.status == expected and checked.level == "source_execution"
    receipt = executor.evidence(checked.idempotency_key)
    assert receipt["baseline"]["fault_reproduced"] is True
    if failed_stage is not None:
        assert receipt["stages"][-1]["stage"] == failed_stage
    assert receipt["application_verification"] == "not_run"


@pytest.mark.skipif(os.environ.get("DFX_REAL_EXPORT_ACCEPTANCE") != "1", reason="local exported candidates not requested")
@pytest.mark.parametrize("oracle,relative,protocol", [
    (FONT, "b1-026-teleprompter1/live1017-case10", "teleprompter-font"),
    (MUSIC, "b1-025-music1/live1017-case08", "music-slider"),
])
def test_existing_slider_candidates_with_actual_source_and_exported_receipts(
        tmp_path: Path, ordinary_node: Path, managed: Path, oracle: str, relative: str, protocol: str) -> None:
    _, marker, scope = REVIEWED_ORACLES[oracle]
    check_exported_candidate(tmp_path, ordinary_node, managed, ROOT / "runs/exports/cases" / relative,
        ROOT / oracle, protocol, scope, marker, REVIEWED_MOCKS[oracle])
