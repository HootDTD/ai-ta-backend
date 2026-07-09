"""V3 parser triviality tests.

Covers:
- Deterministic short-circuits (length floor, ACK list, math-chars).
- LLM classifier path (mocked) for prose without math characters.
- The redesign-doc false-negative ("what comes in must equal what goes out")
  and false-positive ("first, hi there!") cases.
"""
from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from apollo.parser.parser_llm import _is_non_trivial
from apollo.subjects import load_concept


@pytest.fixture(scope="module")
def concept():
    return load_concept("fluid_mechanics", "bernoulli_principle")


def test_length_floor_below_ten_is_trivial(concept):
    assert _is_non_trivial("hi", concept) is False
    assert _is_non_trivial("ok sure", concept) is False


def test_trivial_ack_list_is_trivial(concept):
    for ack in ("ok", "okay", "yes", "no", "hmm", "hi", "hey", "thanks", "thx", "ty"):
        assert _is_non_trivial(ack, concept) is False, f"{ack!r} should be trivial"


def test_math_characters_short_circuit_to_non_trivial(concept):
    # No LLM call needed when math chars are present.
    assert _is_non_trivial("A1*v1 = A2*v2", concept) is True
    assert _is_non_trivial("P1 + 0.5*rho*v1**2 = P2", concept) is True


def _patch_classifier(*, is_teaching, confidence):
    return patch(
        "apollo.parser.parser_llm.cheap_chat",
        return_value=json.dumps({
            "is_teaching": is_teaching,
            "confidence": confidence,
            "reason": "test",
        }),
    )


def test_plain_prose_teaching_caught_by_llm(concept):
    """Redesign-doc false-negative: V2 missed this; V3 must catch it."""
    utterance = "what comes in must equal what goes out, that's the rule"
    with _patch_classifier(is_teaching=True, confidence=0.9):
        assert _is_non_trivial(utterance, concept) is True


def test_plain_prose_chitchat_not_teaching(concept):
    """LLM says no teaching => trivial (don't raise)."""
    utterance = "alright, that all sounds good to me, thanks"
    with _patch_classifier(is_teaching=False, confidence=0.9):
        assert _is_non_trivial(utterance, concept) is False


def test_low_confidence_treated_as_trivial(concept):
    """Below confidence threshold => trivial (return empty silently)."""
    utterance = "hmm, I think about it like a balance of stuff but I'm not sure"
    with _patch_classifier(is_teaching=True, confidence=0.4):
        assert _is_non_trivial(utterance, concept) is False


def test_high_confidence_teaching_threshold_boundary(concept):
    """Threshold is 0.6; exactly 0.6 should pass; 0.59 should not."""
    with _patch_classifier(is_teaching=True, confidence=0.6):
        assert _is_non_trivial("a longer prose teaching attempt", concept) is True
    with _patch_classifier(is_teaching=True, confidence=0.59):
        assert _is_non_trivial("a longer prose teaching attempt", concept) is False


def test_llm_failure_falls_open_to_trivial(concept):
    """Network/parse error => classifier returns (False, 0.0) => trivial.
    Better to silently let Apollo respond than to spuriously raise."""
    with patch(
        "apollo.parser.parser_llm.cheap_chat",
        side_effect=RuntimeError("API down"),
    ):
        # Without math chars and with the classifier failing, this falls
        # through to "trivial".
        assert _is_non_trivial("a non-trivial prose utterance", concept) is False


def test_first_hi_there_is_not_treated_as_teaching(concept):
    """V2 false-positive: "first, hi there!" tripped the plan-marker scan
    on the word 'first'. V3 routes this through the LLM classifier, which
    correctly says 'not teaching'."""
    with _patch_classifier(is_teaching=False, confidence=0.9):
        assert _is_non_trivial("first, hi there!", concept) is False


def test_math_chars_dominate_over_classifier(concept):
    """Even if the classifier would say not-teaching, math characters
    short-circuit to non-trivial. This avoids unnecessary LLM calls and
    matches the historical behavior."""
    # No patch — if we hit cheap_chat we'd raise.
    assert _is_non_trivial("here y = 5 + 3 holds", concept) is True


def test_hyphenated_word_does_not_short_circuit(concept):
    """Regression: V2's _EQUATION_LIKE regex included `-`, which made
    "non-trivial" match. V3 drops `-` from the class and routes through
    the LLM classifier."""
    with _patch_classifier(is_teaching=False, confidence=0.9):
        assert _is_non_trivial("a non-trivial prose utterance", concept) is False
