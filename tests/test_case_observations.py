"""Synthetic receiver and evaluator-vector checks; never application evidence."""

from __future__ import annotations

import importlib.util
import json
import struct
from copy import deepcopy
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("case_observation_receiver",
                                            ROOT / "scripts/preparation/check_case_observations.py")
assert SPEC is not None and SPEC.loader is not None
RECEIVER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RECEIVER)

CHECKSUMS = {"order_ascii": "1e09f387", "order_cjk": "2d1dee28",
             "order_supplementary_character": "98d4a482"}
AMPLITUDES = {"mono_exact_second": [0.25], "mono_final_sampled_frame": [0.25, 0.5],
              "stereo_exact_duration": [0.375, 0.5], "stereo_final_sampled_frame": [0.375, 0.5, 0.75]}


def oracles():
    return RECEIVER.load_json(ROOT / "resources/preparation/case-oracles.json")


def synthetic_observations(registry, *, case_id=None):
    """Explicit test doubles, not results calculated from an application."""
    rows = []
    for case in registry["cases"]:
        if case_id is not None and case["case_id"] != case_id:
            continue
        for vector in case["vectors"]:
            vector_id = vector["vector_id"]
            if case["case_id"] == "TC_JSError_021":
                observed = {"completed": True, "amplitudes": list(AMPLITUDES[vector_id])}
            else:
                observed = {"completed": True, "payload_json": vector["input"]["payload_json"],
                            "create_checksum": CHECKSUMS[vector_id], "verify_original": True,
                            "verify_wrong_checksum": False, "verify_tampered_request": False,
                            "pre_order_original": {"status_code": 200, "orders_added": 1},
                            "pre_order_tampered_request": {"status_code": 400, "orders_added": 0,
                                                           "error_message": "订单内容校验失败"}}
            rows.append({"case_id": case["case_id"], "vector_id": vector_id,
                         "input": deepcopy(vector["input"]), "observed": observed})
    return {"schema_version": 1, "scope": "case_observations_only", "oracle_id": registry["oracle_id"],
            "observations": rows}


def row(document, vector_id):
    return next(item for item in document["observations"] if item["vector_id"] == vector_id)


def reference_checksum(payload):
    """Validate fixed evaluator constants via UTF-16 bytes, not candidate code."""
    units = [value[0] for value in struct.iter_unpack("<H", payload.encode("utf-16-le"))]
    modulus = 1 << 32
    polynomial = sum(unit * pow(31, len(units) - index - 1, modulus) for index, unit in enumerate(units))
    return format(polynomial % modulus, "x")


def test_registered_vectors_match_explicit_independent_constants():
    registry = oracles()
    assert registry["scope"] == "case_observations_only"
    assert registry["visibility"] == "evaluator_only"
    assert all(case["admission"] == "pending_host_and_environment_review" for case in registry["cases"])
    pcm_case, order_case = registry["cases"]
    assert [case["case_id"] for case in registry["cases"]] == ["TC_JSError_021", "TC_JSError_022"]
    decoded = {
        "mono_exact_second": [0.25, -0.75],
        "mono_final_sampled_frame": [0.25, -0.75, -0.5],
        "stereo_exact_duration": [-0.5, 0.25, 0.125, -1.0, -0.75, -0.25, 1.0, 0.5],
        "stereo_final_sampled_frame": [-0.5, 0.25, 0.125, -1.0, -0.75, -0.25, 1.0, 0.5, 0.5, -1.0],
    }
    for vector in pcm_case["vectors"]:
        raw = bytes.fromhex(vector["input"]["pcm_hex"])
        assert [value[0] for value in struct.iter_unpack("<f", raw)] == decoded[vector["vector_id"]]
        assert vector["input"]["sample_rate"] == 2
        assert len(raw) % (4 * vector["input"]["channel_count"]) == 0
        assert vector["expected"] == {"completed": True, "amplitudes": AMPLITUDES[vector["vector_id"]]}
    for vector in order_case["vectors"]:
        inputs = vector["input"]
        assert list(inputs["request"]) == inputs["request_key_order"]
        assert list(inputs["tampered_request"]) == inputs["request_key_order"]
        assert json.dumps(inputs["request"], ensure_ascii=False, separators=(",", ":")) == inputs["payload_json"]
        assert json.dumps(inputs["tampered_request"], ensure_ascii=False,
                          separators=(",", ":")) == inputs["tampered_payload_json"]
        assert inputs["valid_checksum"] == CHECKSUMS[vector["vector_id"]] == reference_checksum(inputs["payload_json"])
        assert reference_checksum(inputs["tampered_payload_json"]) != inputs["valid_checksum"]
        assert inputs["request"]["actualPay"] + 1 == inputs["tampered_request"]["actualPay"]
    supplementary = order_case["vectors"][-1]["input"]["payload_json"]
    assert len(supplementary.encode("utf-16-le")) // 2 == len(supplementary) + 1


def test_complete_synthetic_match_does_not_authenticate_application():
    registry = oracles()
    result = RECEIVER.evaluate_observations(registry, synthetic_observations(registry))
    assert result["status"] == "pass"
    assert result["matched_count"] == result["required_count"] == 7
    assert result["failures"] == []
    assert result["scope"] == "case_observations_only"
    assert result["accepted"] is False
    assert result["application_evidence_authenticated"] is False
    assert result["application_verification"] == "not_run"
    assert "case_id" not in result


@pytest.mark.parametrize("case_id,required_count", [("TC_JSError_021", 4), ("TC_JSError_022", 3)])
def test_selected_case_requires_its_complete_vectors_without_application_authority(case_id, required_count):
    registry = oracles()
    result = RECEIVER.evaluate_observations(registry, synthetic_observations(registry, case_id=case_id),
                                            case_id=case_id)
    assert result["status"] == "pass" and result["failures"] == []
    assert result["case_id"] == case_id
    assert result["required_count"] == result["matched_count"] == required_count
    assert result["scope"] == "case_observations_only"
    assert result["accepted"] is False and result["application_evidence_authenticated"] is False
    assert result["application_verification"] == "not_run"
    assert result["admission"] == "pending_host_and_environment_review"


def test_selected_audio_case_rejects_missing_final_vector():
    registry = oracles()
    document = synthetic_observations(registry, case_id="TC_JSError_021")
    document["observations"].pop()
    result = RECEIVER.evaluate_observations(registry, document, case_id="TC_JSError_021")
    assert result["status"] == "fail" and result["required_count"] == 4 and result["matched_count"] == 3
    assert result["failures"] == [{"case_id": "TC_JSError_021", "vector_id": "stereo_final_sampled_frame",
                                  "reason": "missing_observation"}]


def test_selected_audio_case_rejects_foreign_order_observation():
    registry = oracles()
    document = synthetic_observations(registry, case_id="TC_JSError_021")
    document["observations"].append(row(synthetic_observations(registry), "order_ascii"))
    result = RECEIVER.evaluate_observations(registry, document, case_id="TC_JSError_021")
    assert result["status"] == "fail" and result["required_count"] == result["matched_count"] == 4
    assert result["failures"] == [{"case_id": "TC_JSError_022", "vector_id": "order_ascii",
                                  "reason": "unregistered_observation"}]


def test_selected_audio_case_rejects_wrong_final_frame():
    registry = oracles()
    document = synthetic_observations(registry, case_id="TC_JSError_021")
    row(document, "stereo_final_sampled_frame")["observed"]["amplitudes"] = [0.375, 0.5]
    result = RECEIVER.evaluate_observations(registry, document, case_id="TC_JSError_021")
    assert result["status"] == "fail" and result["required_count"] == 4 and result["matched_count"] == 3
    assert result["failures"] == [{"case_id": "TC_JSError_021", "vector_id": "stereo_final_sampled_frame",
                                  "reason": "observed_mismatch"}]


@pytest.mark.parametrize("case_id", ["TC_JSError_999", "021", ""])
def test_unsupported_case_selection_is_rejected(case_id):
    registry = oracles()
    with pytest.raises(ValueError, match="selected case"):
        RECEIVER.evaluate_observations(registry, synthetic_observations(registry), case_id=case_id)


@pytest.mark.parametrize("wrong", [[], [0.0, 0.0, 0.0], [0.375, 0.5], [0.5, 0.75, 0.5],
                                 [-0.125, -0.5, -0.25], [0.375, 0.5, 0.75, 0.0], [True, 0.5, 0.75]])
def test_empty_truncated_signed_one_channel_extra_frame_or_boolean_waveform_fails(wrong):
    registry = oracles()
    document = synthetic_observations(registry)
    row(document, "stereo_final_sampled_frame")["observed"]["amplitudes"] = wrong
    result = RECEIVER.evaluate_observations(registry, document)
    assert result["status"] == "fail"
    assert result["failures"] == [{"case_id": "TC_JSError_021", "vector_id": "stereo_final_sampled_frame",
                                  "reason": "observed_mismatch"}]


def test_exact_duration_exception_and_lost_final_mono_sample_fail():
    registry = oracles()
    document = synthetic_observations(registry)
    row(document, "mono_exact_second")["observed"] = {"completed": False, "error": "RangeError"}
    row(document, "mono_final_sampled_frame")["observed"]["amplitudes"] = [0.25]
    result = RECEIVER.evaluate_observations(registry, document)
    assert {item["vector_id"] for item in result["failures"]} == {"mono_exact_second", "mono_final_sampled_frame"}


@pytest.mark.parametrize("field,value", [("create_checksum", "0"), ("verify_original", False),
                                        ("verify_wrong_checksum", True), ("verify_tampered_request", True),
                                        ("pre_order_original", {"status_code": 200, "orders_added": 0}),
                                        ("pre_order_tampered_request", {"status_code": 200, "orders_added": 1})])
def test_agreeing_but_wrong_checksum_and_route_observations_fail(field, value):
    registry = oracles()
    document = synthetic_observations(registry)
    row(document, "order_supplementary_character")["observed"][field] = value
    assert RECEIVER.evaluate_observations(registry, document)["status"] == "fail"


def test_unicode_serialization_and_input_echo_drift_fail():
    registry = oracles()
    document = synthetic_observations(registry)
    target = row(document, "order_cjk")
    target["observed"]["payload_json"] = json.dumps(target["input"]["request"], separators=(",", ":"))
    row(document, "mono_exact_second")["input"]["sample_rate"] = 3
    result = RECEIVER.evaluate_observations(registry, document)
    assert {item["reason"] for item in result["failures"]} == {"input_mismatch", "observed_mismatch"}


def test_all_registered_observations_required_and_extra_vector_fails():
    registry = oracles()
    document = synthetic_observations(registry)
    document["observations"].pop()
    extra = deepcopy(document["observations"][0])
    extra["vector_id"] = "unregistered"
    document["observations"].append(extra)
    result = RECEIVER.evaluate_observations(registry, document)
    assert {item["reason"] for item in result["failures"]} == {"missing_observation", "unregistered_observation"}
    assert result["required_count"] == 7 and result["matched_count"] == 6
    assert "case_id" not in result


@pytest.mark.parametrize("mutation", ["duplicate", "extra_field", "missing_field", "claim_application",
                                     "boolean_version", "nonfinite", "wrong_oracle"])
def test_malformed_or_self_promoted_document_rejects(mutation):
    registry = oracles()
    document = synthetic_observations(registry)
    if mutation == "duplicate":
        document["observations"].append(deepcopy(document["observations"][0]))
    elif mutation == "extra_field":
        document["observations"][0]["accepted"] = True
    elif mutation == "missing_field":
        del document["observations"][0]["observed"]
    elif mutation == "claim_application":
        document["scope"] = "application"
    elif mutation == "boolean_version":
        document["schema_version"] = True
    elif mutation == "nonfinite":
        document["observations"][0]["observed"]["amplitudes"] = [float("nan")]
    else:
        document["oracle_id"] = "other"
    with pytest.raises(ValueError):
        RECEIVER.evaluate_observations(registry, document)


@pytest.mark.parametrize("raw", ['{"x":1,"x":2}', '{"x":NaN}', '{"x":Infinity}', '[]'])
def test_json_reader_rejects_ambiguous_or_nonfinite_inputs(tmp_path, raw):
    source = tmp_path / "observations.json"
    source.write_text(raw, encoding="utf-8")
    with pytest.raises(ValueError):
        RECEIVER.load_json(source)


def test_cli_missing_output_fails_without_disclosing_expected_values(tmp_path, capsys):
    registry = oracles()
    document = synthetic_observations(registry)
    row(document, "order_cjk")["observed"].pop("create_checksum")
    source = tmp_path / "observations.json"
    source.write_text(json.dumps(document), encoding="utf-8")
    assert RECEIVER.main(["--observations", str(source)]) == 1
    output = capsys.readouterr().out
    report = json.loads(output)
    assert report["scope"] == "case_observations_only" and report["status"] == "fail"
    assert not any(value in output for value in CHECKSUMS.values())
    assert "actualPay" not in output and "amplitudes" not in output


def test_cli_matching_test_double_returns_zero_with_no_application_authority(tmp_path, capsys):
    registry = oracles()
    source = tmp_path / "observations.json"
    source.write_text(json.dumps(synthetic_observations(registry)), encoding="utf-8")
    assert RECEIVER.main(["--observations", str(source)]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["accepted"] is False and report["application_verification"] == "not_run"


def test_cli_selected_audio_case_uses_existing_joint_oracle_without_application_authority(tmp_path, capsys):
    registry = oracles()
    source = tmp_path / "observations.json"
    source.write_text(json.dumps(synthetic_observations(registry, case_id="TC_JSError_021")), encoding="utf-8")
    assert RECEIVER.main(["--observations", str(source), "--case", "TC_JSError_021"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["case_id"] == "TC_JSError_021" and report["required_count"] == report["matched_count"] == 4
    assert report["accepted"] is False and report["application_verification"] == "not_run"
    assert report["application_evidence_authenticated"] is False
    assert report["admission"] == "pending_host_and_environment_review"


def test_cli_unsupported_case_fails_without_disclosing_expected_values(tmp_path, capsys):
    registry = oracles()
    source = tmp_path / "observations.json"
    source.write_text(json.dumps(synthetic_observations(registry)), encoding="utf-8")
    assert RECEIVER.main(["--observations", str(source), "--case", "TC_JSError_999"]) == 1
    output = capsys.readouterr().out
    report = json.loads(output)
    assert report["reason"] == "invalid_receiver_input" and report["status"] == "fail"
    assert report["accepted"] is False and report["application_verification"] == "not_run"
    assert not any(value in output for value in CHECKSUMS.values())
    assert "actualPay" not in output and "amplitudes" not in output
