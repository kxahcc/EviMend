"""The new retry SDK path executes the existing full synthetic repair acceptance."""

from pathlib import Path

import pytest
from test_competition import (
    test_complete_sdk_local_repair_feedback_skills_report_and_resume as _accept_repair,
)


def test_retrying_sdk_private_file_repair_feedback_report_delivery_and_resume(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _accept_repair(tmp_path, monkeypatch, "retrying_private_file")
