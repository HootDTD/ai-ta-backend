"""Tests for the intent classifier and confirmation detector (item #5).

These cover `apollo.handlers.intent` in isolation. Integration with the
full chat handler lives in test_chat.py once the legacy V2 tests there
are rewritten for V3 — out of scope for item #5."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from apollo.handlers.intent import (
    _CLASSIFIER_PROMPT,
    INTENT_CONFIDENCE_THRESHOLD,
    _classifier_prompt,
    classify_intent,
    confirmation_prompt_for,
    detect_confirmation,
)
from apollo.subjects import load_concept


@pytest.fixture(scope="module")
def concept():
    return load_concept("fluid_mechanics", "bernoulli_principle")


# ---- detect_confirmation -------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "yes",
        "yep",
        "Yeah grade me",
        "sure, go ahead",
        "ok",
        "Sounds good!",
        "let's do it",
        "please",
        "okay grade me",
    ],
)
def test_detect_confirmation_affirms(text):
    v = detect_confirmation(text)
    assert v.affirmed is True and v.rejected is False


@pytest.mark.parametrize(
    "text",
    [
        "no",
        "nope",
        "not yet",
        "wait, give me a sec",
        "cancel",
        "never mind",
        "nevermind",
        "hold on",
    ],
)
def test_detect_confirmation_rejects(text):
    v = detect_confirmation(text)
    assert v.affirmed is False and v.rejected is True


@pytest.mark.parametrize(
    "text",
    [
        "I want to add one more equation: A1*v1 = A2*v2",
        "actually, let me explain density first",
        "",
        "the velocity is constant",
    ],
)
def test_detect_confirmation_ambiguous(text):
    v = detect_confirmation(text)
    assert v.affirmed is False
    assert v.rejected is False
    assert v.ambiguous is True


def test_detect_confirmation_mixed_signal_falls_through():
    """If both yes and no patterns hit, treat as ambiguous (fall through)."""
    v = detect_confirmation("yes but actually no, wait")
    assert v.ambiguous is True


# ---- classify_intent ----------------------------------------------------


def _patch_classifier(*, intent: str, confidence: float, reason: str = "test"):
    return patch(
        "apollo.handlers.intent.cheap_chat",
        return_value=json.dumps(
            {
                "intent": intent,
                "confidence": confidence,
                "reason": reason,
            }
        ),
    )


def test_classify_returns_verdict_for_done(concept):
    with _patch_classifier(intent="done", confidence=0.92):
        v = classify_intent(
            utterance="ok I'm done teaching, your turn",
            history=[],
            concept=concept,
        )
    assert v.intent == "done"
    assert v.confidence == 0.92
    assert v.confidence >= INTENT_CONFIDENCE_THRESHOLD


def test_classify_returns_verdict_for_teaching(concept):
    with _patch_classifier(intent="teaching", confidence=0.85):
        v = classify_intent(
            utterance="for incompressible flow, A1*v1 = A2*v2",
            history=[],
            concept=concept,
        )
    assert v.intent == "teaching"


def test_classify_low_confidence_done(concept):
    """Low-confidence done should still return done as intent — caller
    decides whether to act on it."""
    with _patch_classifier(intent="done", confidence=0.4):
        v = classify_intent(
            utterance="hmm I think that's all maybe",
            history=[],
            concept=concept,
        )
    assert v.intent == "done"
    assert v.confidence < INTENT_CONFIDENCE_THRESHOLD


def test_classify_unknown_intent_defaults_to_teaching(concept):
    """If the LLM returns a string we don't recognize, fall back to
    teaching (conservative)."""
    with _patch_classifier(intent="argue_with_apollo", confidence=0.9):
        v = classify_intent(
            utterance="actually you're wrong",
            history=[],
            concept=concept,
        )
    assert v.intent == "teaching"


def test_classify_non_numeric_confidence_safe(concept):
    with patch(
        "apollo.handlers.intent.cheap_chat",
        return_value=json.dumps({"intent": "done", "confidence": "very high"}),
    ):
        v = classify_intent(
            utterance="ok done",
            history=[],
            concept=concept,
        )
    assert v.intent == "done"
    assert v.confidence == 0.0


def test_classify_confidence_clamped(concept):
    with _patch_classifier(intent="done", confidence=2.5):
        v = classify_intent(
            utterance="grade me",
            history=[],
            concept=concept,
        )
    assert v.confidence == 1.0


def test_classify_soft_fails_to_teaching_on_exception(concept):
    """Network/parse error => default to teaching (don't hijack the turn)."""
    with patch(
        "apollo.handlers.intent.cheap_chat",
        side_effect=RuntimeError("API down"),
    ):
        v = classify_intent(
            utterance="ok done",
            history=[],
            concept=concept,
        )
    assert v.intent == "teaching"
    assert v.confidence == 0.0


def test_classify_soft_fails_to_teaching_on_malformed_json(concept):
    with patch(
        "apollo.handlers.intent.cheap_chat",
        return_value="not json",
    ):
        v = classify_intent(
            utterance="ok done",
            history=[],
            concept=concept,
        )
    assert v.intent == "teaching"


def test_classify_history_tail_is_passed(concept):
    """The classifier should receive the tail of the history; verify
    payload shape via the patched call args."""
    long_history = [{"role": "user", "content": f"turn {i}"} for i in range(10)]
    with patch(
        "apollo.handlers.intent.cheap_chat",
        return_value=json.dumps({"intent": "teaching", "confidence": 0.5}),
    ) as mock_chat:
        classify_intent(
            utterance="latest",
            history=long_history,
            concept=concept,
        )
        # Inspect the user-message payload — should contain at most the
        # last _HISTORY_TAIL_LEN turns.
        args, kwargs = mock_chat.call_args
        user_msg = kwargs["messages"][-1]["content"]
        payload = json.loads(user_msg)
        assert len(payload["history_tail"]) <= 4  # _HISTORY_TAIL_LEN


# ---- confirmation_prompt_for --------------------------------------------


@pytest.mark.parametrize(
    "intent",
    [
        "done",
        "restart",
        "next",
        "return_to_hoot",
        "help",
    ],
)
def test_confirmation_prompt_exists_for_all_gated_intents(intent):
    prompt = confirmation_prompt_for(intent)
    assert prompt
    assert isinstance(prompt, str)
    assert len(prompt) > 10


@pytest.mark.parametrize("intent", ["teaching", "off_topic"])
def test_confirmation_prompt_is_empty_for_fallthrough_intents(intent):
    assert confirmation_prompt_for(intent) == ""


def test_confirmation_prompt_empty_for_reference_question():
    """reference_question direct-executes (handlers/chat.py) rather than
    confirmation-gating, so it must never carry a confirmation prompt."""
    assert confirmation_prompt_for("reference_question") == ""


# ---- INTERACTION4 flag gating (brief: "the reference_question label enters
# the intent-classifier prompt ONLY when INTERACTION4 is on") -------------


def test_classifier_prompt_byte_identical_when_flag_off(monkeypatch):
    """HARD REQUIREMENT: flag off => classifier prompt is exactly today's
    prompt, not just "doesn't mention reference_question"."""
    monkeypatch.delenv("INTERACTION4", raising=False)
    monkeypatch.delenv("INTERACTION_CONCEPTS", raising=False)
    assert _classifier_prompt("bernoulli_principle") == _CLASSIFIER_PROMPT


def test_classifier_prompt_gains_reference_question_when_flag_on_allowlist_unset(monkeypatch):
    monkeypatch.setenv("INTERACTION4", "1")
    monkeypatch.delenv("INTERACTION_CONCEPTS", raising=False)
    prompt = _classifier_prompt("bernoulli_principle")
    assert "reference_question" in prompt
    assert prompt != _CLASSIFIER_PROMPT


def test_classifier_prompt_gains_reference_question_for_matching_concept(monkeypatch):
    monkeypatch.setenv("INTERACTION4", "1")
    monkeypatch.setenv("INTERACTION_CONCEPTS", "network_effects, BERNOULLI_PRINCIPLE")
    prompt = _classifier_prompt("bernoulli_principle")
    assert "reference_question" in prompt
    assert prompt != _CLASSIFIER_PROMPT


def test_classifier_prompt_omits_reference_question_for_nonmatching_concept(monkeypatch):
    monkeypatch.setenv("INTERACTION4", "1")
    monkeypatch.setenv("INTERACTION_CONCEPTS", "network_effects")
    assert _classifier_prompt("bernoulli_principle") == _CLASSIFIER_PROMPT


def test_classify_intent_reference_question_when_flag_on(monkeypatch, concept):
    monkeypatch.delenv("INTERACTION_CONCEPTS", raising=False)
    with (
        patch("apollo.handlers.intent._interaction4_enabled", return_value=True),
        _patch_classifier(intent="reference_question", confidence=0.9),
    ):
        v = classify_intent(
            utterance="wait, what IS a network effect?",
            history=[],
            concept=concept,
        )
    assert v.intent == "reference_question"


def test_classify_intent_reference_question_downgraded_when_flag_off(concept):
    """Defense in depth: even if the model somehow emits reference_question
    with the flag off (it shouldn't see the label at all), the verdict is
    forced back to teaching so flag-off behavior never surfaces the intent."""
    with (
        patch("apollo.handlers.intent._interaction4_enabled", return_value=False),
        _patch_classifier(intent="reference_question", confidence=0.95),
    ):
        v = classify_intent(
            utterance="wait, what IS a network effect?",
            history=[],
            concept=concept,
        )
    assert v.intent == "teaching"


def test_classify_intent_reference_question_downgraded_for_nonmatching_concept(
    monkeypatch, concept
):
    monkeypatch.setenv("INTERACTION4", "1")
    monkeypatch.setenv("INTERACTION_CONCEPTS", "network_effects")
    with _patch_classifier(intent="reference_question", confidence=0.95):
        v = classify_intent(
            utterance="wait, what IS Bernoulli's principle?",
            history=[],
            concept=concept,
        )
    assert v.intent == "teaching"


def test_classify_intent_ambiguous_defaults_to_teaching_with_flag_on(concept):
    """Conservative-bias invariant regression: adding the new label must not
    weaken the ambiguous-case fallback to teaching, flag on or off."""
    with (
        patch("apollo.handlers.intent._interaction4_enabled", return_value=True),
        _patch_classifier(intent="teaching", confidence=0.3),
    ):
        v = classify_intent(
            utterance="hmm, not sure what to say",
            history=[],
            concept=concept,
        )
    assert v.intent == "teaching"
