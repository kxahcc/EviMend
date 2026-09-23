"""Reject invalid evidence provenance, references and source coordinates."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from dfx_agent.contracts import (
    Diagnosis,
    Evidence,
    Hypothesis,
    SourceLocation,
    admit_evidence,
    validate_references,
)


def sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


SNAPSHOT = sha("snapshot")
COMMON = {
    "task_id": "task1", "run_id": "run1", "created_at": datetime.now(UTC),
    "source_snapshot_sha256": SNAPSHOT,
}


def location(**changes: object) -> SourceLocation:
    values = {"snapshot_sha256": SNAPSHOT, "path": "src/main.ets", "start_line": 2, "end_line": 2}
    values.update(changes)
    return SourceLocation.model_validate(values)


def evidence(**changes: object) -> Evidence:
    values = {
        **COMMON, "record_id": "e1", "kind": "source_fact", "producer": "source_reader",
        "artifact_sha256": sha("source"), "statement": "assignment", "location": location(),
    }
    values.update(changes)
    return Evidence.model_validate(values)


def diagnosis(**changes: object) -> Diagnosis:
    values = {
        **COMMON, "record_id": "d1", "exception_locations": [location()],
        "hypotheses": [Hypothesis(hypothesis_id="h1", root_cause_locations=[location()],
                                  support_evidence_ids=["e1"], support_status="supported")],
        "selected_hypothesis_id": "h1",
    }
    values.update(changes)
    return Diagnosis.model_validate(values)


def test_valid_references_stay_bound_to_source() -> None:
    validate_references(diagnosis(), [evidence()], {"src/main.ets": 3})
    pure_log = evidence(record_id="log1", kind="log_observation", producer="input",
                        source_snapshot_sha256=None, location=None, log_line=1)
    validate_references(diagnosis(), [evidence(), pure_log], {"src/main.ets": 3})


def test_bad_line_and_missing_evidence_are_rejected() -> None:
    with pytest.raises(ValidationError):
        location(start_line=0)
    with pytest.raises(ValueError, match="outside"):
        validate_references(diagnosis(), [evidence()], {"src/main.ets": 1})
    with pytest.raises(ValueError, match="missing evidence"):
        validate_references(diagnosis(), [], {"src/main.ets": 3})


def test_wrong_snapshot_and_run_cannot_support_diagnosis() -> None:
    with pytest.raises(ValueError, match="another task, run or snapshot"):
        validate_references(diagnosis(), [evidence(run_id="other")], {"src/main.ets": 3})
    with pytest.raises(ValueError, match="another snapshot"):
        validate_references(diagnosis(exception_locations=[location(snapshot_sha256=sha("other"))]),
                            [evidence()], {"src/main.ets": 3})


def test_model_inference_cannot_masquerade_as_execution() -> None:
    with pytest.raises(ValidationError, match="producer"):
        evidence(kind="application_execution", producer="model", execution_id="job1")
    with pytest.raises(ValidationError, match="execution_id"):
        evidence(kind="local_execution", producer="execution_backend", location=None)
    forged = evidence(kind="application_execution", producer="execution_backend", execution_id="job1", location=None)
    with pytest.raises(ValueError, match="trusted channel"):
        admit_evidence(forged.model_dump(), channel="model")


def test_strict_fields_and_paths() -> None:
    with pytest.raises(ValidationError):
        location(path="../outside.ets")
    with pytest.raises(ValidationError):
        location(start_line="2")
    with pytest.raises(ValidationError):
        evidence(unexpected="ignored")
