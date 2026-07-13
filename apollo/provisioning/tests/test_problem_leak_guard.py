import json
from pathlib import Path
from typing import Any

import pytest

from apollo.provisioning.problem_leak_guard import (
    CONFIDENCE_THRESHOLD,
    check_problem_leak,
)
from apollo.schemas.problem import Problem

_BERNOULLI_FIXTURE = (
    Path(__file__).parents[2]
    / "subjects/fluid_mechanics/concepts/bernoulli_principle/problems/problem_03.json"
)


def _calc_problem(problem_text: str, *, given_values: dict[str, float] | None = None) -> Problem:
    return Problem.model_validate(
        {
            "id": "calc-leak-fixture",
            "concept_id": "continuity_equation",
            "difficulty": "standard",
            "problem_text": problem_text,
            "given_values": given_values or {"A1": 0.02, "A2": 0.008, "v1": 3.0},
            "target_unknown": "v2",
            "reference_solution": [
                {
                    "step": 1,
                    "entry_type": "equation",
                    "id": "continuity",
                    "content": {"symbolic": "A1*v1 = A2*v2"},
                },
                {
                    "step": 2,
                    "entry_type": "procedure_step",
                    "id": "solve-v2",
                    "content": {
                        "order": 1,
                        "action": "Substitute the givens and solve v2 = 44.29 m/s",
                        "purpose": "Obtain the outlet velocity",
                        "uses_equations": ["continuity"],
                    },
                    "depends_on": ["continuity"],
                },
            ],
        }
    )


@pytest.mark.parametrize(
    "problem_text",
    [
        "Given the inlet conditions, find v2. The answer is 44.29 m/s.",
        "Given the inlet conditions, find v2 = 44.3 m/s.",
        "The outlet velocity will be 4.429e1 m/s; explain the calculation.",
    ],
)
def test_calc_answer_leaks_are_caught_deterministically(problem_text: str):
    verdict = check_problem_leak(_calc_problem(problem_text))

    assert verdict.leaked is True
    assert verdict.method == "deterministic"
    assert verdict.confidence == 1.0


def test_clean_bernoulli_fixture_with_only_given_numbers_passes_deterministically():
    raw = json.loads(_BERNOULLI_FIXTURE.read_text())
    raw["reference_solution"][-1]["content"]["action"] += ", obtaining v2 = 7.5"
    problem = Problem.model_validate(raw)

    verdict = check_problem_leak(problem, chat_fn=lambda **_: pytest.fail("judge called"))

    assert verdict.leaked is False
    assert verdict.method == "deterministic"


def test_given_values_are_never_mistaken_for_the_answer():
    problem = _calc_problem(
        "A calibration value is 44.29. Given the inlet conditions, find v2.",
        given_values={"calibration": 44.29, "same_digits": 4.429},
    )

    verdict = check_problem_leak(problem)

    assert verdict.leaked is False
    assert verdict.method == "deterministic"


def test_target_equation_is_a_leak_even_when_the_scalar_is_also_given():
    problem = _calc_problem(
        "A calibration value is 44.29, and the solved relation is v2 = 44.29.",
        given_values={"calibration": 44.29},
    )

    assert check_problem_leak(problem).leaked is True


def test_same_digits_as_answer_do_not_match_but_answer_itself_does():
    clean = _calc_problem("Use the scale factor 4.429 to find v2.", given_values={"scale": 4.429})
    leaking = clean.model_copy(update={"problem_text": "Use the scale factor 4.429; v2 is 44.3."})

    assert check_problem_leak(clean).leaked is False
    assert check_problem_leak(leaking).leaked is True


def test_final_step_symbolic_result_is_caught_deterministically():
    problem = Problem.model_validate(
        {
            "id": "calc-symbolic-leak",
            "concept_id": "trigonometric_integrals",
            "difficulty": "standard",
            "problem_text": "Evaluate the integral. Its value is pi / 16.",
            "target_unknown": "I",
            "reference_solution": [
                {
                    "step": 1,
                    "entry_type": "procedure_step",
                    "id": "integrate",
                    "content": {
                        "order": 1,
                        "action": "Integrate and evaluate I = pi/16",
                        "purpose": "obtain the definite integral",
                        "uses_equations": [],
                    },
                }
            ],
        }
    )

    verdict = check_problem_leak(problem)

    assert verdict.leaked is True
    assert verdict.method == "deterministic"


def test_symbolic_result_is_not_matched_as_a_substring_of_another_value():
    problem = Problem.model_validate(
        {
            "id": "calc-symbolic-clean",
            "concept_id": "trigonometric_integrals",
            "difficulty": "standard",
            "problem_text": "Evaluate the integral over the interval ending at pi/160.",
            "target_unknown": "I",
            "reference_solution": [
                {
                    "step": 1,
                    "entry_type": "procedure_step",
                    "id": "integrate",
                    "content": {
                        "order": 1,
                        "action": "Integrate and evaluate I = pi/16",
                        "purpose": "obtain the definite integral",
                        "uses_equations": [],
                    },
                }
            ],
        }
    )

    assert check_problem_leak(problem).leaked is False


def _qualitative_problem() -> Problem:
    return Problem.model_validate(
        {
            "id": "mgmt-qualitative-fixture",
            "concept_id": "organizational_change",
            "difficulty": "standard",
            "problem_text": (
                "A growing firm replaces informal decision-making with formal reporting lines. "
                "Explain how employees and managers may respond to the transition."
            ),
            "reference_solution": [
                {
                    "step": 1,
                    "entry_type": "definition",
                    "id": "role-clarity",
                    "content": {
                        "label": "role clarity",
                        "definition": "formal reporting can clarify authority while disrupting norms",
                    },
                },
                {
                    "step": 2,
                    "entry_type": "procedure_step",
                    "id": "weigh-effects",
                    "content": {
                        "order": 1,
                        "action": "weigh clearer authority against resistance and disorientation",
                        "purpose": "form a balanced qualitative argument",
                        "uses_equations": [],
                    },
                    "depends_on": ["role-clarity"],
                },
            ],
        }
    )


def _judge_stub(*, leaked: bool, confidence: float, quoted_span: str | None):
    calls: list[dict[str, Any]] = []

    def chat_fn(**kwargs: Any) -> str:
        calls.append(kwargs)
        return json.dumps({"leaked": leaked, "confidence": confidence, "quoted_span": quoted_span})

    chat_fn.calls = calls  # type: ignore[attr-defined]
    return chat_fn


def test_qualitative_problem_abstains_without_a_judge():
    verdict = check_problem_leak(_qualitative_problem())

    assert verdict.leaked is False
    assert verdict.confidence == 0.0
    assert verdict.method == "deterministic"
    assert verdict.reasons == ["no extractable answer"]


@pytest.mark.parametrize(
    ("leaked", "confidence", "expected_leaked"),
    [
        (True, CONFIDENCE_THRESHOLD + 0.2, True),
        (True, CONFIDENCE_THRESHOLD - 0.01, False),
        (False, 0.95, False),
    ],
)
def test_qualitative_judge_confidence_gate(leaked: bool, confidence: float, expected_leaked: bool):
    chat_fn = _judge_stub(
        leaked=leaked,
        confidence=confidence,
        quoted_span="clarify authority" if leaked else None,
    )

    verdict = check_problem_leak(_qualitative_problem(), chat_fn=chat_fn)

    assert verdict.leaked is expected_leaked
    assert verdict.method == "judge"
    assert verdict.confidence == confidence
    call = chat_fn.calls[0]  # type: ignore[attr-defined]
    assert call["purpose"] == "problem_leak_judge"
    assert call["temperature"] == 0.0
    assert call["response_format"]["type"] == "json_schema"
    assert call["response_format"]["json_schema"]["strict"] is True


def test_low_confidence_judge_leak_is_advisory():
    verdict = check_problem_leak(
        _qualitative_problem(),
        chat_fn=_judge_stub(leaked=True, confidence=0.2, quoted_span="formal reporting"),
    )

    assert verdict.leaked is False
    assert "advisory" in verdict.reasons[0]


def test_qualitative_judge_parse_failure_passes_open():
    verdict = check_problem_leak(_qualitative_problem(), chat_fn=lambda **_: "not-json")

    assert verdict.leaked is False
    assert verdict.method == "judge"
    assert verdict.confidence == 0.0
    assert "judge unavailable (advisory)" in verdict.reasons


# ---------------------------------------------------------------------------
# Defensive-branch coverage: the guard's edge paths are part of its contract
# (never crash, never false-positive on junk inputs), so pin them explicitly.
# ---------------------------------------------------------------------------


def _symbolic_problem(problem_text: str) -> Problem:
    """Problem whose reference answer is symbolic (no numeric value)."""
    return Problem.model_validate(
        {
            "id": "symbolic-leak-fixture",
            "concept_id": "continuity_equation",
            "difficulty": "standard",
            "problem_text": problem_text,
            "given_values": {},
            "target_unknown": "v2",
            "reference_solution": [
                {
                    "step": 1,
                    "entry_type": "equation",
                    "id": "continuity",
                    "content": {"symbolic": "A1*v1 = A2*v2"},
                },
                {
                    "step": 2,
                    "entry_type": "equation",
                    "id": "isolated-answer",
                    "content": {"symbolic": "v2 = A1*v1/A2"},
                    "depends_on": ["continuity"],
                },
            ],
        }
    )


def test_symbolic_answer_expression_in_statement_is_a_leak():
    verdict = check_problem_leak(
        _symbolic_problem("Show that v2 = A1*v1/A2 for the nozzle, then find v2.")
    )

    assert verdict.leaked is True
    assert verdict.method == "deterministic"


def test_result_word_phrasing_in_final_step_is_extracted():
    problem = _calc_problem("Given the inlet conditions, the result is 44.29 m/s. Find v2.")

    verdict = check_problem_leak(problem)

    assert verdict.leaked is True
    assert verdict.method == "deterministic"


def test_overlong_and_prose_answer_candidates_are_ignored():
    from apollo.provisioning.problem_leak_guard import _clean_symbolic

    assert _clean_symbolic("v " * 90) is None  # >120 chars of non-math prose
    assert _clean_symbolic("downstream") is None  # ordinary English word
    assert _clean_symbolic("multi\nline = 3") is None


def test_number_parsing_rejects_overflow_and_junk():
    from apollo.provisioning.problem_leak_guard import _as_number, _numeric_result

    assert _as_number("1e999") is None  # parses but is not finite
    assert _as_number("no digits") is None
    assert _numeric_result("44.29 m/s and then some very long trailing prose" + " x" * 40) is None


def test_contains_symbolic_handles_empty_answers():
    from apollo.provisioning.problem_leak_guard import _contains_symbolic

    assert _contains_symbolic("any statement", "   ") is False


def test_target_equation_with_unparseable_rhs_is_skipped():
    problem = _calc_problem(
        "Find v2 = " + "definitely not an answer just words " * 5 + ". Use the given areas."
    )

    verdict = check_problem_leak(problem)

    assert verdict.leaked is False


def test_judge_confidence_clamping_is_robust():
    from apollo.provisioning.problem_leak_guard import _clamp_confidence

    assert _clamp_confidence("not a float") == 0.0
    assert _clamp_confidence(float("inf")) == 0.0
    assert _clamp_confidence(-3.0) == 0.0
    assert _clamp_confidence(7.0) == 1.0


def test_judge_non_object_json_fails_open():
    verdict = check_problem_leak(_qualitative_problem(), chat_fn=lambda **_: "[1, 2, 3]")

    assert verdict.leaked is False
    assert "judge unavailable (advisory)" in verdict.reasons
