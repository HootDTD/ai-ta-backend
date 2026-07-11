import math

import pytest

from apollo.overseer.coverage_contract import _validate_score_map, validate_coverage_verdict


def _valid():
    return {
        "per_step": {"n": "covered"},
        "procedure_scores": {"n": 1.0},
        "confidences": {"n": 0.9},
        "negotiation_counts": {"dual": 0, "disputed": 0, "paraphrased": 0, "skipped": 0},
    }


def test_validator_accepts_all_zero_negotiation_counts():
    validate_coverage_verdict(_valid())


@pytest.mark.parametrize("mutation", ["missing", "bad_status", "nan"])
def test_validator_rejects_contract_violations(mutation):
    value = _valid()
    if mutation == "missing":
        del value["negotiation_counts"]
    elif mutation == "bad_status":
        value["per_step"]["n"] = "partial"
    else:
        value["confidences"]["n"] = math.nan
    with pytest.raises(ValueError):
        validate_coverage_verdict(value)


def test_validator_rejects_procedure_scores_not_a_dict():
    value = _valid()
    value["procedure_scores"] = ["not", "a", "dict"]
    with pytest.raises(ValueError, match="procedure_scores must be a dict"):
        validate_coverage_verdict(value)


def test_validator_rejects_score_map_non_numeric_value():
    value = _valid()
    value["procedure_scores"]["n"] = "x"
    with pytest.raises(ValueError, match="must map string node ids to numbers"):
        validate_coverage_verdict(value)


def test_validator_rejects_per_step_not_a_dict():
    value = _valid()
    value["per_step"] = ["not", "a", "dict"]
    with pytest.raises(ValueError, match="per_step must be a dict"):
        validate_coverage_verdict(value)


def test_validator_rejects_negotiation_counts_wrong_key_set():
    value = _valid()
    value["negotiation_counts"] = {"dual": 0, "disputed": 0, "paraphrased": 0}
    with pytest.raises(ValueError, match="negotiation_counts keys must be exactly"):
        validate_coverage_verdict(value)


@pytest.mark.parametrize("bad_value", [-1, True])
def test_validator_rejects_negotiation_counts_negative_or_bool_value(bad_value):
    value = _valid()
    value["negotiation_counts"]["dual"] = bad_value
    with pytest.raises(ValueError, match="negotiation_counts values must be non-negative integers"):
        validate_coverage_verdict(value)


def test_validate_score_map_directly_rejects_non_dict():
    with pytest.raises(ValueError, match="procedure_scores must be a dict"):
        _validate_score_map(["not", "a", "dict"], key="procedure_scores")
