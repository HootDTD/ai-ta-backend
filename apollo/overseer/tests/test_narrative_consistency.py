"""P2.1 (2026-08-07 bimodal-fix spec §5) — the deterministic narrative/verdict
consistency gate.

Defect U2: the narrator praised content the coverage verdict zeroed (attempt
154, F/28: "You clearly contrasted democratization and centralization… gave
concrete examples" — that exact node graded ``missing``). The gate is pure,
post-generation, and string/structural: no second LLM call.

Contract under test:

* a PURE-praise sentence about a topic below :data:`PRAISE_FLOOR` is stripped;
* a topic at credit 0 always ends up with its gap named (deterministic sentence
  appended when the model named none);
* mixed praise+gap sentences survive — they already name the gap;
* topics at or above the floor are untouched (byte-identical);
* the pass is pure, total, and idempotent.
"""

from __future__ import annotations

from typing import Any

import pytest

from apollo.overseer.narrative_consistency import (
    FALLBACK_HEADLINE,
    PRAISE_FLOOR,
    enforce_narrative_consistency,
)
from apollo.overseer.topic_score import TopicCredit

pytestmark = pytest.mark.unit


def _topic(
    *,
    key: str = "t1",
    credit: float,
    display_name: str | None = "apply continuity",
) -> TopicCredit:
    status = "covered" if credit >= 1.0 else ("missing" if credit <= 0.0 else "partial")
    return TopicCredit(
        canonical_key=key,
        display_name=display_name,
        credit=credit,
        status=status,  # type: ignore[arg-type]
        weight=1.0,
        misconceptions=(),
    )


def _feedback(
    note: str,
    *,
    key: str = "t1",
    headline: str = "Here is the summary.",
    next_step: str = "Show how continuity connects the two states.",
) -> dict[str, Any]:
    return {
        "headline": headline,
        "topic_feedback": [
            {"canonical_key": key, "note": note, "quote": None, "hoot_assisted": False}
        ],
        "recap": [],
        "next_step": next_step,
    }


def _note(result: dict[str, Any]) -> str:
    note = result["topic_feedback"][0]["note"]
    assert isinstance(note, str)
    return note


# --------------------------------------------------------------------------
# Per-topic notes.
# --------------------------------------------------------------------------


def test_pure_praise_on_a_zeroed_topic_is_replaced_by_a_named_gap() -> None:
    """The attempt-154 shape: praise-only prose for a node graded missing."""
    feedback = _feedback(
        "You clearly contrasted democratization and centralization and gave concrete examples."
    )

    result = enforce_narrative_consistency(feedback, topics=[_topic(credit=0.0)])

    note = _note(result)
    assert "clearly contrasted" not in note
    assert "apply continuity" in note
    assert note.endswith(".")


def test_zeroed_topic_without_a_gap_sentence_gets_one_appended() -> None:
    """Neutral prose is kept, but credit 0 must still name what Apollo missed."""
    feedback = _feedback("Continuity is the bridge between the two states.")

    result = enforce_narrative_consistency(feedback, topics=[_topic(credit=0.0)])

    note = _note(result)
    assert note.startswith("Continuity is the bridge between the two states.")
    assert "apply continuity" in note


def test_imperative_instruction_already_names_the_gap() -> None:
    """An imperative revision instruction counts as naming the gap — no append."""
    feedback = _feedback("Make the continuity step explicit.")

    result = enforce_narrative_consistency(feedback, topics=[_topic(credit=0.0)])

    assert _note(result) == "Make the continuity step explicit."


def test_stripping_praise_keeps_a_surviving_gap_sentence_as_is() -> None:
    feedback = _feedback("You clearly nailed it. But the mechanism never came through.")

    result = enforce_narrative_consistency(feedback, topics=[_topic(credit=0.0)])

    assert _note(result) == "But the mechanism never came through."


def test_mixed_praise_and_gap_sentence_survives() -> None:
    feedback = _feedback("You named the two states, but you never connected them.")

    result = enforce_narrative_consistency(feedback, topics=[_topic(credit=0.0)])

    assert _note(result) == "You named the two states, but you never connected them."


def test_partial_credit_below_the_floor_strips_praise_and_names_the_gap() -> None:
    feedback = _feedback("You explained continuity nicely.")

    result = enforce_narrative_consistency(feedback, topics=[_topic(credit=0.5)])

    note = _note(result)
    assert "nicely" not in note
    assert "apply continuity" in note


def test_topic_at_the_floor_keeps_its_praise_untouched() -> None:
    feedback = _feedback("You explained continuity nicely.")

    result = enforce_narrative_consistency(feedback, topics=[_topic(credit=PRAISE_FLOOR)])

    assert result == feedback


def test_credited_topic_is_byte_identical() -> None:
    feedback = _feedback(
        "You clearly contrasted the two states and gave concrete examples.",
        headline="You clearly nailed apply continuity.",
        next_step="You showed continuity well, so extend it to the next state.",
    )

    result = enforce_narrative_consistency(feedback, topics=[_topic(credit=1.0)])

    assert result == feedback


def test_long_reference_wording_is_quoted_and_shortened() -> None:
    """Prod display names are sentence-shaped reference wording (median 220
    chars, always ending in a period) — the gap sentence must quote a clipped
    clause, never swallow a second sentence."""
    display_name = (
        "The defining feature of a direct network effect is that the benefit flows between "
        "members of a single user group, without requiring a second group to participate."
    )
    feedback = _feedback("You clearly explained the whole thing.")

    result = enforce_narrative_consistency(
        feedback, topics=[_topic(credit=0.0, display_name=display_name)]
    )

    note = _note(result)
    assert note.startswith('Apollo never got this from your teaching: "The defining feature')
    assert note.endswith("walk through it explicitly next time.")
    assert "…" in note
    assert note.count(".") == 1


def test_topic_without_a_display_name_falls_back_to_the_humanized_key() -> None:
    feedback = _feedback("You clearly showed the whole derivation.", key="eq_mass_balance")

    result = enforce_narrative_consistency(
        feedback,
        topics=[_topic(key="eq_mass_balance", credit=0.0, display_name=None)],
    )

    note = _note(result)
    assert "eq_mass_balance" not in note
    assert "mass balance" in note


# --------------------------------------------------------------------------
# Headline and next step.
# --------------------------------------------------------------------------


def test_headline_praising_an_uncredited_topic_is_replaced() -> None:
    feedback = _feedback(
        "Make the continuity step explicit.",
        headline="You clearly applied continuity throughout.",
    )

    result = enforce_narrative_consistency(feedback, topics=[_topic(credit=0.0)])

    assert "clearly applied continuity" not in result["headline"]
    assert result["headline"]


def test_generic_headline_praise_that_names_no_topic_survives() -> None:
    """Praise the ledger cannot contradict is left alone — this gate is not a
    tone police."""
    feedback = _feedback(
        "Make the continuity step explicit.",
        headline="You clearly put real work into this.",
    )

    result = enforce_narrative_consistency(feedback, topics=[_topic(credit=0.0)])

    assert result["headline"] == "You clearly put real work into this."


def test_headline_is_matched_against_every_uncredited_topic() -> None:
    feedback = _feedback(
        "Make the continuity step explicit.",
        headline="You clearly explained adoption of the network effect.",
    )

    result = enforce_narrative_consistency(
        feedback,
        topics=[
            _topic(credit=0.0),
            _topic(key="t2", credit=0.0, display_name="network effect adoption"),
        ],
    )

    assert result["headline"] == FALLBACK_HEADLINE


def test_next_step_praising_an_uncredited_topic_falls_back_to_a_teach_it_move() -> None:
    feedback = _feedback(
        "Make the continuity step explicit.",
        next_step="You explained apply continuity well.",
    )

    result = enforce_narrative_consistency(feedback, topics=[_topic(credit=0.0)])

    assert "explained apply continuity" not in result["next_step"]
    assert "apply continuity" in result["next_step"]


# --------------------------------------------------------------------------
# Totality: never raise, never mutate, idempotent.
# --------------------------------------------------------------------------


def test_unknown_canonical_key_is_left_alone() -> None:
    feedback = _feedback("You clearly explained everything.", key="ghost")

    result = enforce_narrative_consistency(feedback, topics=[_topic(credit=0.0)])

    assert _note(result) == "You clearly explained everything."


def test_abbreviation_is_not_a_sentence_boundary() -> None:
    """ "e.g." must not split praise into a praise half and a stranded half."""
    feedback = _feedback("You clearly explained it, e.g. with the pipe example.")

    result = enforce_narrative_consistency(feedback, topics=[_topic(credit=0.0)])

    assert "pipe example" not in _note(result)


def test_malformed_payload_fields_pass_through_untouched() -> None:
    """The gate is total: a shape it does not understand is never rewritten."""
    feedback: dict[str, Any] = {
        "headline": None,
        "topic_feedback": ["not a dict", {"canonical_key": "t1"}],
        "recap": [],
        "next_step": 7,
    }

    result = enforce_narrative_consistency(feedback, topics=[_topic(credit=0.0)])

    assert result["headline"] is None
    assert result["next_step"] == 7
    assert result["topic_feedback"] == ["not a dict", {"canonical_key": "t1"}]


def test_no_topics_returns_an_equal_payload() -> None:
    feedback = _feedback("You clearly explained everything.")

    assert enforce_narrative_consistency(feedback, topics=[]) == feedback


def test_input_payload_is_never_mutated() -> None:
    feedback = _feedback("You clearly explained everything.")
    before = str(feedback)

    enforce_narrative_consistency(feedback, topics=[_topic(credit=0.0)])

    assert str(feedback) == before


def test_gate_is_idempotent() -> None:
    feedback = _feedback(
        "You clearly contrasted the two states.",
        headline="You clearly applied continuity throughout.",
        next_step="You explained apply continuity well.",
    )
    topics = [_topic(credit=0.0)]

    once = enforce_narrative_consistency(feedback, topics=topics)
    twice = enforce_narrative_consistency(once, topics=topics)

    assert twice == once


def test_empty_prose_fields_get_deterministic_replacements() -> None:
    feedback = _feedback("", headline="", next_step="")

    result = enforce_narrative_consistency(feedback, topics=[_topic(credit=0.0)])

    assert result["headline"]
    assert result["next_step"]
    assert "apply continuity" in _note(result)
