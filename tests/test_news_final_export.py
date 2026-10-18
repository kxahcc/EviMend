"""Independently recheck the frozen news repair, without another cloud request."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from test_enterprise_source_replay import check_exported_candidate
from test_office_source_replay import managed as managed  # noqa: PLC0414
from test_office_source_replay import ordinary_node as ordinary_node  # noqa: PLC0414

from dfx_agent.pilot_preparation import REVIEWED_MOCKS, REVIEWED_ORACLES

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(os.environ.get("DFX_REAL_EXPORT_ACCEPTANCE") != "1",
                    reason="local frozen candidate validation not requested")
def test_news_candidate_actual_source_and_independent_behavior(
        tmp_path: Path, ordinary_node: Path, managed: Path) -> None:
    result = ROOT / "runs/exports/cases/b2-002-comprehensivenews1/live1017-retry18"
    if not result.is_dir():
        pytest.skip("optional private candidate export is unavailable")
    oracle = "resources/source-validation/news-author/oracle.cjs"
    _, marker, scope = REVIEWED_ORACLES[oracle]
    check_exported_candidate(tmp_path, ordinary_node, managed, result,
                             ROOT / oracle, "news-author", scope, marker,
                             REVIEWED_MOCKS[oracle])
