"""GEN-3 verifier tests: real SymPy and stubbed qualitative judging."""

from __future__ import annotations

import json

from apollo.provisioning.problem_generation.verifiers import (
    qualitative_rubric,
    round_trip_check,
)
from apollo.schemas.problem import Problem


def _quantitative_problem(*, governing: str, stated: str) -> Problem:
    return Problem.model_validate(
        {
            "id": "generated-flow-rate",
            "concept_id": "flow_rate",
            "difficulty": "standard",
            "problem_text": "Water crosses a 0.015 m^2 section at 4 m/s. Find Q.",
            "given_values": {"A": 0.015, "v": 4.0},
            "target_unknown": "Q",
            "reference_solution": [
                {
                    "step": 1,
                    "entry_type": "equation",
                    "id": "governing",
                    "content": {"label": "Flow relation", "symbolic": governing},
                    "depends_on": [],
                },
                {
                    "step": 2,
                    "entry_type": "equation",
                    "id": "answer_key",
                    "content": {"label": "Stated answer", "symbolic": f"Q = {stated}"},
                    "depends_on": ["governing"],
                },
            ],
        }
    )


def _prose_problem() -> Problem:
    return Problem.model_validate(
        {
            "id": "generated-trust",
            "concept_id": "institutional_trust",
            "difficulty": "standard",
            "problem_text": (
                "A manager publishes decision criteria and invites staff questions. "
                "Explain how these practices may support institutional trust."
            ),
            "given_values": {},
            "target_unknown": "institutional trust",
            "reference_solution": [
                {
                    "step": 1,
                    "entry_type": "definition",
                    "id": "trust_claims",
                    "content": {
                        "term": "institutional trust",
                        "definition": "Transparency supports trust and guarantees agreement.",
                    },
                    "depends_on": [],
                }
            ],
        }
    )


def test_round_trip_correct_generated_problem_is_verified():
    result = round_trip_check(_quantitative_problem(governing="Q = A*v", stated="0.06"))
    assert result.verdict == "verified"
    assert "all solution branches match" in result.diagnostic


def test_round_trip_wrong_stated_answer_is_refuted():
    result = round_trip_check(_quantitative_problem(governing="Q - A*v", stated="0.07"))
    assert result.verdict == "refuted"
    assert "solved='0.0600000000000000'" in result.diagnostic
    assert "stated='0.0700000000000000'" in result.diagnostic


def test_round_trip_transcendental_system_is_unresolved():
    result = round_trip_check(_quantitative_problem(governing="Q + cos(Q)", stated="0"))
    assert result.verdict == "unresolved"
    assert "NotImplementedError" in result.diagnostic or "timeout" in result.diagnostic


def test_round_trip_prose_problem_is_inapplicable():
    result = round_trip_check(_prose_problem())
    assert result.verdict == "inapplicable"
    assert "no distinct governing system" in result.diagnostic


def test_qualitative_rubric_decomposes_claims_and_pins_ceiling():
    calls = []

    def chat_fn(**kwargs):
        calls.append(kwargs)
        return json.dumps(
            {
                "claims": [
                    {
                        "claim": "The manager publishes decision criteria.",
                        "supported": True,
                        "note": "The statement says this directly.",
                    },
                    {
                        "claim": "All staff agree with every decision.",
                        "supported": False,
                        "note": "The statement does not claim unanimous agreement.",
                    },
                ]
            }
        )

    report = qualitative_rubric(_prose_problem(), chat_fn=chat_fn)

    assert report is not None
    assert report.unsupported_count == 1
    assert report.ceiling == "faithfulness_only"
    assert report.claims[1].supported is False
    assert calls[0]["purpose"] == "problem_generation_qualitative_rubric"
    assert calls[0]["temperature"] == 0.0
    assert calls[0]["response_format"]["type"] == "json_schema"
    assert calls[0]["response_format"]["json_schema"]["strict"] is True
    assert "faithfulness_only" not in calls[0]["messages"][1]["content"]


def test_qualitative_rubric_malformed_output_fails_open(caplog):
    report = qualitative_rubric(_prose_problem(), chat_fn=lambda **_kwargs: "not-json")
    assert report is None
    assert "apollo_problem_generation_qualitative_rubric_failed" in caplog.text
