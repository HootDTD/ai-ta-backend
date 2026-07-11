import math

import pytest

from apollo.overseer.coverage_contract import validate_coverage_verdict


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
