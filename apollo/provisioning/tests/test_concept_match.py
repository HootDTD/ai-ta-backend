"""Closed-list concept matcher (reversed provisioning) — injected chat_fn, no network."""

import json

import pytest

from apollo.provisioning.concept_match import (
    _NO_MATCH,
    build_match_schema,
    match_concept,
)
from apollo.subjects.curriculum_db import RegisteredConcept

_CONCEPTS = [
    RegisteredConcept(
        concept_id=1,
        slug="integration-by-parts",
        display_name="Integration by Parts",
        description="u dv = uv - v du",
    ),
    RegisteredConcept(
        concept_id=2,
        slug="u_substitution",
        display_name="Substitution",
        description="change of variables",
    ),
]


def _chat(responses: list[str]):
    calls: list[dict] = []

    def chat_fn(**kwargs) -> str:
        calls.append(kwargs)
        return responses[min(len(calls) - 1, len(responses) - 1)]

    chat_fn.calls = calls  # type: ignore[attr-defined]
    return chat_fn


@pytest.mark.asyncio
async def test_match_primary_resolves_concept_id() -> None:
    chat_fn = _chat(
        [
            json.dumps(
                {
                    "primary": "integration-by-parts",
                    "secondary": ["u_substitution"],
                    "confidence": 0.97,
                    "rationale": "product of x and e^x",
                }
            )
        ]
    )
    m = await match_concept("Evaluate integral x e^x dx.", _CONCEPTS, chat_fn=chat_fn)
    assert m.concept_id == 1 and m.slug == "integration-by-parts"
    assert not m.no_match and not m.retried
    # pass 1 runs at low effort
    assert chat_fn.calls[0]["reasoning_effort"] == "low"
    # the model is BLIND to provenance: the prompt carries only problem_text + list
    user = chat_fn.calls[0]["messages"][1]["content"]
    assert "integration-by-parts" in user and "Evaluate integral" in user


@pytest.mark.asyncio
async def test_no_match_is_held_not_forced() -> None:
    chat_fn = _chat(
        [
            json.dumps(
                {
                    "primary": _NO_MATCH,
                    "secondary": [],
                    "confidence": 0.9,
                    "rationale": "a statistics question, nothing here fits",
                }
            )
        ]
    )
    m = await match_concept("Compute the median of ...", _CONCEPTS, chat_fn=chat_fn)
    assert m.no_match and m.concept_id is None and not m.retried


@pytest.mark.asyncio
async def test_self_contradiction_retries_once_at_medium() -> None:
    contradiction = json.dumps(
        {
            "primary": _NO_MATCH,
            "secondary": [],
            "confidence": 0.6,
            "rationale": "standard integration by parts, so no listed concept applies",
        }
    )
    fixed = json.dumps(
        {
            "primary": "integration-by-parts",
            "secondary": [],
            "confidence": 0.95,
            "rationale": "product form",
        }
    )
    chat_fn = _chat([contradiction, fixed])
    m = await match_concept("Evaluate integral ln(x) dx.", _CONCEPTS, chat_fn=chat_fn)
    assert m.retried and m.concept_id == 1
    assert chat_fn.calls[1]["reasoning_effort"] == "medium"


@pytest.mark.asyncio
async def test_persistent_no_match_after_retry_stays_no_match() -> None:
    contradiction = json.dumps(
        {
            "primary": _NO_MATCH,
            "secondary": [],
            "confidence": 0.6,
            "rationale": "maybe integration by parts but unsure",
        }
    )
    chat_fn = _chat([contradiction, contradiction])
    m = await match_concept("Evaluate something.", _CONCEPTS, chat_fn=chat_fn)
    assert m.no_match and m.retried and len(chat_fn.calls) == 2


@pytest.mark.asyncio
async def test_hallucinated_slug_retries_then_no_match() -> None:
    bad = json.dumps(
        {"primary": "made-up-concept", "secondary": [], "confidence": 0.8, "rationale": "x"}
    )
    chat_fn = _chat([bad, bad])
    m = await match_concept("Evaluate.", _CONCEPTS, chat_fn=chat_fn)
    assert m.no_match and m.retried


@pytest.mark.asyncio
async def test_unparseable_response_retries_then_no_match() -> None:
    chat_fn = _chat(["not json", "still not json"])
    m = await match_concept("Evaluate.", _CONCEPTS, chat_fn=chat_fn)
    assert m.no_match and m.retried and len(chat_fn.calls) == 2


@pytest.mark.asyncio
async def test_slug_match_is_normalization_insensitive() -> None:
    # model echoes hyphens for an underscore-registered slug
    chat_fn = _chat(
        [
            json.dumps(
                {
                    "primary": "u-substitution",
                    "secondary": [],
                    "confidence": 0.9,
                    "rationale": "sub",
                }
            )
        ]
    )
    m = await match_concept("integral 2x cos(x^2) dx", _CONCEPTS, chat_fn=chat_fn)
    assert m.concept_id == 2 and m.slug == "u_substitution"


def test_match_schema_is_strict_json_schema() -> None:
    schema = build_match_schema()
    assert schema["name"] and schema["strict"] is True
    assert set(schema["schema"]["required"]) == {"primary", "secondary", "confidence", "rationale"}


@pytest.mark.asyncio
async def test_non_dict_response_retries(chat_list=None) -> None:
    chat_fn = _chat(
        [
            '["a list"]',
            '{"primary": "u_substitution", "secondary": [], "confidence": 0.9, "rationale": "sub"}',
        ]
    )
    m = await match_concept("integral 2x cos(x^2) dx", _CONCEPTS, chat_fn=chat_fn)
    assert m.retried and m.concept_id == 2


@pytest.mark.asyncio
async def test_non_numeric_confidence_degrades_to_zero() -> None:
    chat_fn = _chat(
        [
            json.dumps(
                {
                    "primary": "u_substitution",
                    "secondary": [],
                    "confidence": "high",
                    "rationale": "sub",
                }
            )
        ]
    )
    m = await match_concept("integral", _CONCEPTS, chat_fn=chat_fn)
    assert m.concept_id == 2 and m.confidence == 0.0
