"""Compare evaluator-owned finite expectations with supplied observation JSON.

This receiver never executes or translates ArkTS, reads a candidate, computes an
observed waveform/checksum, or authenticates application evidence. A passing
manually supplied JSON file is still only a case-observation comparison.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any

SCOPE = "case_observations_only"
MAX_JSON_BYTES = 1_048_576
DEFAULT_ORACLES = Path(__file__).resolve().parents[2] / "resources/preparation/case-oracles.json"


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON member")
        result[key] = value
    return result


def _constant(value: str) -> Any:
    raise ValueError("nonfinite JSON number")


def load_json(path: Path) -> dict[str, Any]:
    """Read one bounded UTF-8 object; do not execute or repair its content."""
    with path.open("rb") as source:
        raw = source.read(MAX_JSON_BYTES + 1)
    if len(raw) > MAX_JSON_BYTES:
        raise ValueError("JSON input exceeds receiver capacity")
    value = json.loads(raw.decode("utf-8"), object_pairs_hook=_object, parse_constant=_constant)
    if not isinstance(value, dict):
        raise ValueError("JSON root must be an object")
    return value


def _finite(value: Any) -> bool:
    if isinstance(value, dict):
        return all(isinstance(key, str) and _finite(item) for key, item in value.items())
    if isinstance(value, list):
        return all(_finite(item) for item in value)
    if type(value) is float:
        return math.isfinite(value)
    return value is None or type(value) in (str, int, bool)


def _equal(actual: Any, expected: Any, *, tolerance: float = 0.0) -> bool:
    if isinstance(expected, dict):
        return (isinstance(actual, dict) and actual.keys() == expected.keys() and
                all(_equal(actual[key], value, tolerance=tolerance) for key, value in expected.items()))
    if isinstance(expected, list):
        return (isinstance(actual, list) and len(actual) == len(expected) and
                all(_equal(left, right, tolerance=tolerance) for left, right in zip(actual, expected, strict=True)))
    if type(expected) is float and type(actual) in (int, float):
        return math.isfinite(actual) and math.isclose(actual, expected, rel_tol=0.0, abs_tol=tolerance)
    return type(actual) is type(expected) and actual == expected


def _registered(oracles: dict[str, Any]) -> dict[tuple[str, str], tuple[dict[str, Any], float]]:
    if (oracles.get("schema_version") != 1 or type(oracles.get("schema_version")) is not int or
            oracles.get("scope") != SCOPE or oracles.get("visibility") != "evaluator_only" or
            oracles.get("admission") != "pending_host_and_environment_review" or
            not isinstance(oracles.get("oracle_id"), str) or not oracles["oracle_id"] or
            not isinstance(oracles.get("cases"), list) or not oracles["cases"] or not _finite(oracles)):
        raise ValueError("invalid evaluator oracle document")
    registered: dict[tuple[str, str], tuple[dict[str, Any], float]] = {}
    case_ids: set[str] = set()
    for case in oracles["cases"]:
        if (not isinstance(case, dict) or not isinstance(case.get("case_id"), str) or not case["case_id"] or
                case["case_id"] in case_ids or case.get("admission") != "pending_host_and_environment_review" or
                not isinstance(case.get("contract"), dict) or not isinstance(case.get("vectors"), list) or
                not case["vectors"]):
            raise ValueError("invalid or duplicate registered case")
        case_ids.add(case["case_id"])
        tolerance = case["contract"].get("amplitude_abs_tolerance", 0.0)
        if type(tolerance) not in (int, float) or not math.isfinite(tolerance) or not 0 <= tolerance <= 0.000001:
            raise ValueError("invalid amplitude tolerance")
        for vector in case["vectors"]:
            if (not isinstance(vector, dict) or set(vector) != {"vector_id", "input", "expected"} or
                    not isinstance(vector["vector_id"], str) or not vector["vector_id"] or
                    not isinstance(vector["input"], dict) or not isinstance(vector["expected"], dict) or
                    vector["expected"].get("completed") is not True):
                raise ValueError("invalid registered vector")
            key = case["case_id"], vector["vector_id"]
            if key in registered:
                raise ValueError("duplicate registered vector")
            registered[key] = vector, float(tolerance)
    return registered


def evaluate_observations(oracles: dict[str, Any], observations: dict[str, Any], *,
                          case_id: str | None = None) -> dict[str, Any]:
    """Require all vectors from the selected case, or all cases by default.

    IDs, input echoes and source claims do not prove that an adapter actually ran
    the candidate. Comparison results never confer application admission.
    """
    registered = _registered(oracles)
    if case_id is not None:
        if not isinstance(case_id, str) or not case_id:
            raise ValueError("invalid selected case")
        registered = {key: value for key, value in registered.items() if key[0] == case_id}
        if not registered:
            raise ValueError("selected case is not registered")
    if (set(observations) != {"schema_version", "scope", "oracle_id", "observations"} or
            type(observations.get("schema_version")) is not int or observations["schema_version"] != 1 or
            observations.get("scope") != SCOPE or observations.get("oracle_id") != oracles["oracle_id"] or
            not isinstance(observations.get("observations"), list) or not _finite(observations)):
        raise ValueError("invalid observation document")
    failures: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    matched = 0
    for row in observations["observations"]:
        if (not isinstance(row, dict) or set(row) != {"case_id", "vector_id", "input", "observed"} or
                not isinstance(row["case_id"], str) or not isinstance(row["vector_id"], str) or
                not isinstance(row["input"], dict) or not isinstance(row["observed"], dict)):
            raise ValueError("invalid observation row")
        key = row["case_id"], row["vector_id"]
        if key in seen:
            raise ValueError("duplicate observation")
        seen.add(key)
        reason = None
        if key not in registered:
            reason = "unregistered_observation"
        else:
            vector, tolerance = registered[key]
            if not _equal(row["input"], vector["input"]):
                reason = "input_mismatch"
            elif not _equal(row["observed"], vector["expected"], tolerance=tolerance):
                reason = "observed_mismatch"
            else:
                matched += 1
        if reason is not None:
            failures.append({"case_id": key[0], "vector_id": key[1], "reason": reason})
    for registered_case_id, vector_id in sorted(registered.keys() - seen):
        failures.append({"case_id": registered_case_id, "vector_id": vector_id, "reason": "missing_observation"})
    result = {"schema_version": 1, "scope": SCOPE, "oracle_id": oracles["oracle_id"],
            "status": "fail" if failures else "pass", "required_count": len(registered),
            "matched_count": matched, "failures": failures,
            "application_verification": "not_run", "application_evidence_authenticated": False,
            "accepted": False, "admission": "pending_host_and_environment_review"}
    if case_id is not None:
        result["case_id"] = case_id
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--observations", type=Path, required=True)
    parser.add_argument("--oracles", type=Path, default=DEFAULT_ORACLES)
    parser.add_argument("--case", help="Require every vector for this exact registered case ID; default: all cases")
    args = parser.parse_args(argv)
    try:
        result = evaluate_observations(load_json(args.oracles), load_json(args.observations), case_id=args.case)
    except (OSError, ValueError, TypeError, OverflowError, RecursionError):
        # No expected values, source excerpts or private input appear in feedback.
        result = {"schema_version": 1, "scope": SCOPE, "status": "fail", "reason": "invalid_receiver_input",
                  "application_verification": "not_run", "application_evidence_authenticated": False,
                  "accepted": False, "admission": "pending_host_and_environment_review"}
    print(json.dumps(result, ensure_ascii=True, allow_nan=False, sort_keys=True))
    return 0 if result["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
