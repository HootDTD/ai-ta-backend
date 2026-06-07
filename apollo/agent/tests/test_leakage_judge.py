"""LLM-judge contract tests.

The default judge (`llm_leakage_judge`) calls a real OpenAI model so its
contract is checked indirectly via:
  1. Verdict shape — `JudgeVerdict` parses well-formed and malformed JSON.
  2. Soft-fail-open semantics — parse errors return `leaks=False`.
  3. Confidence clamping — values outside [0, 1] are clamped.

These tests do NOT make network calls; they patch `cheap_chat` instead.
"""
from __future__ import annotations

import json
from unittest.mock import patch

import pytest
import pytest_asyncio

from apollo.agent.leakage_judge import (
    CONFIDENCE_THRESHOLD,
    JudgeVerdict,
    llm_leakage_judge,
)
from apollo.subjects.tests.seed_helpers import seed_bernoulli_concept


@pytest_asyncio.fixture
async def concept(neo4j_test):
    return await seed_bernoulli_concept(neo4j_test)


def _patch_cheap_chat(payload):
    """Helper: patch cheap_chat to return a fixed JSON string."""
    return patch(
        "apollo.agent.leakage_judge.cheap_chat",
        return_value=json.dumps(payload),
    )


@pytest.mark.asyncio
async def test_well_formed_clean_response(concept):
    with _patch_cheap_chat({"leaks": False, "offending_phrase": None,
                            "reason": None, "confidence": 0.1}):
        verdict = llm_leakage_judge(
            draft="Sure, that makes sense to me.",
            concept=concept,
            history=[],
            kg_summary="",
        )
    assert verdict.leaks is False
    assert verdict.confidence == 0.1


@pytest.mark.asyncio
async def test_well_formed_leak_response(concept):
    with _patch_cheap_chat({
        "leaks": True,
        "offending_phrase": "energy conservation",
        "reason": "named the principle",
        "confidence": 0.85,
    }):
        verdict = llm_leakage_judge(
            draft="That's energy conservation.",
            concept=concept,
            history=[],
            kg_summary="",
        )
    assert verdict.leaks is True
    assert verdict.confidence == 0.85
    assert verdict.offending_phrase == "energy conservation"


@pytest.mark.asyncio
async def test_confidence_clamped_above_one(concept):
    with _patch_cheap_chat({"leaks": False, "confidence": 5.7}):
        verdict = llm_leakage_judge(
            draft="ok", concept=concept, history=[], kg_summary="",
        )
    assert verdict.confidence == 1.0


@pytest.mark.asyncio
async def test_confidence_clamped_below_zero(concept):
    with _patch_cheap_chat({"leaks": False, "confidence": -0.4}):
        verdict = llm_leakage_judge(
            draft="ok", concept=concept, history=[], kg_summary="",
        )
    assert verdict.confidence == 0.0


@pytest.mark.asyncio
async def test_malformed_json_soft_fails_open(concept):
    """Parse error => leaks=false at confidence 0. The deterministic
    pre-filter is the safety net for unambiguous violations."""
    with patch(
        "apollo.agent.leakage_judge.cheap_chat",
        return_value="this is not json",
    ):
        verdict = llm_leakage_judge(
            draft="hello", concept=concept, history=[], kg_summary="",
        )
    assert verdict == JudgeVerdict(
        leaks=False, offending_phrase=None, reason=None, confidence=0.0,
    )


@pytest.mark.asyncio
async def test_missing_fields_default_to_clean(concept):
    """Empty JSON object should not crash; falls through to clean."""
    with _patch_cheap_chat({}):
        verdict = llm_leakage_judge(
            draft="ok", concept=concept, history=[], kg_summary="",
        )
    assert verdict.leaks is False
    assert verdict.confidence == 0.0


@pytest.mark.asyncio
async def test_non_numeric_confidence_safe(concept):
    """If the model returns confidence as a string, we coerce to 0."""
    with _patch_cheap_chat({"leaks": True, "confidence": "very high"}):
        verdict = llm_leakage_judge(
            draft="ok", concept=concept, history=[], kg_summary="",
        )
    # leaks=true but confidence=0 => below threshold, won't trigger reject
    assert verdict.leaks is True
    assert verdict.confidence == 0.0
    assert verdict.confidence < CONFIDENCE_THRESHOLD


@pytest.mark.asyncio
async def test_openai_exception_soft_fails_open(concept):
    """Network/API error => leaks=false at confidence 0."""
    with patch(
        "apollo.agent.leakage_judge.cheap_chat",
        side_effect=RuntimeError("API down"),
    ):
        verdict = llm_leakage_judge(
            draft="ok", concept=concept, history=[], kg_summary="",
        )
    assert verdict.leaks is False
    assert verdict.confidence == 0.0
