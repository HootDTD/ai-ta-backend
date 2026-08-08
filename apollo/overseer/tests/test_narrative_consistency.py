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
    MAX_REFERENCE_NAME_QUOTES,
    PRAISE_FLOOR,
    enforce_narrative_consistency,
)
from apollo.overseer.topic_score import MAX_REFERENCE_TEXT_REVEALS, TopicCredit

pytestmark = pytest.mark.unit


def _topic(
    *,
    key: str = "t1",
    credit: float,
    display_name: str | None = "apply continuity",
    weight: float = 1.0,
    hoot_assisted: bool = False,
) -> TopicCredit:
    status = "covered" if credit >= 1.0 else ("missing" if credit <= 0.0 else "partial")
    return TopicCredit(
        canonical_key=key,
        display_name=display_name,
        credit=credit,
        status=status,  # type: ignore[arg-type]
        weight=weight,
        misconceptions=(),
        hoot_assisted=hoot_assisted,
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
# Headline false-strip protection (review finding 1). Real prod problem 453
# (Mason PAPA privacy): the graded reference wording of one credited node and
# four uncredited ones share ordinary domain vocabulary, so a one-shared-word
# rule deletes accurate praise and collapses the whole headline to a canned
# line. 240 ledger-supported praise headlines built from the 14 exported prod
# problems: 139 false strips (57.9%) under one-word, 0 under the rule below.
# --------------------------------------------------------------------------

_P453_CREDITED = (
    "Two forces threaten privacy: the growth of information technology (enhanced capacity for "
    "surveillance, communication, computation, storage, and retrieval) and, more insidiously, "
    "the increased value of information in decision-making."
)
_P453_UNCREDITED = (
    "Information is increasingly valuable to policy makers and decision makers; they covet it "
    "even if acquiring it invades another's privacy."
)


def _p453_topics() -> list[TopicCredit]:
    return [
        _topic(key="q5_two_forces", credit=1.0, display_name=_P453_CREDITED),
        _topic(key="value_over_privacy", credit=0.0, display_name=_P453_UNCREDITED),
    ]


@pytest.mark.parametrize(
    "headline",
    [
        "You clearly explained how information technology expanded the capacity for surveillance.",
        "You gave Apollo a sharp account of the two forces behind privacy erosion.",
        "Strong work on the structural drivers of privacy invasion.",
    ],
)
def test_praise_for_a_credited_topic_survives_next_to_uncredited_ones(headline: str) -> None:
    """A single shared domain word must never delete ledger-supported praise."""
    feedback = _feedback(
        "Make the value-of-information point explicit.",
        key="value_over_privacy",
        headline=headline,
    )

    result = enforce_narrative_consistency(feedback, topics=_p453_topics())

    assert result["headline"] == headline


def test_praise_aimed_at_the_uncredited_topic_is_still_stripped() -> None:
    """Recall is preserved: the attempt-154 defect shape still loses its praise."""
    feedback = _feedback(
        "Make the value-of-information point explicit.",
        key="value_over_privacy",
        headline=(
            "You clearly showed how decision makers covet information even when acquiring it "
            "invades someone else's privacy."
        ),
    )

    result = enforce_narrative_consistency(feedback, topics=_p453_topics())

    assert result["headline"] == FALLBACK_HEADLINE


def test_a_sentence_leaning_on_credited_wording_is_never_deleted() -> None:
    """Guard 2: overlap with the uncredited topic must beat every credited one."""
    feedback = _feedback(
        "Make the value-of-information point explicit.",
        key="value_over_privacy",
        headline=(
            "You clearly laid out the growth of information technology and its capacity for "
            "surveillance, communication, and storage."
        ),
    )

    result = enforce_narrative_consistency(feedback, topics=_p453_topics())

    assert result["headline"] == feedback["headline"]


# --------------------------------------------------------------------------
# Hoot-assisted topics (review finding 2). INTERACTION5 caps an assisted node
# at a flat 0.5 — always under the floor — so credit there is a policy penalty,
# not an absence of evidence.
# --------------------------------------------------------------------------


def test_hoot_capped_topic_keeps_its_credit_sentence_and_gains_no_gap() -> None:
    """A node graded `covered` then capped to 0.5 is not a teaching gap."""
    note = (
        "You explained both forces in your own words. You looked this up with Hoot, so it "
        "counted for less."
    )
    feedback = _feedback(note)

    result = enforce_narrative_consistency(
        feedback, topics=[_topic(credit=0.5, hoot_assisted=True)]
    )

    assert result == feedback


def test_hoot_assisted_topic_at_zero_is_still_uncredited() -> None:
    """The cap is min(evidence, 0.5): exactly 0 can only come from a pre-cap 0."""
    feedback = _feedback("You clearly explained continuity.")

    result = enforce_narrative_consistency(
        feedback, topics=[_topic(credit=0.0, hoot_assisted=True)]
    )

    note = _note(result)
    assert "clearly explained" not in note
    assert "apply continuity" in note


# --------------------------------------------------------------------------
# Reference-wording budget. The gate's own gap sentences quote the topic's
# display name — the reference solution's own wording — so they are the same
# reveal channel as D2's `topics[].reference_text` and share its per-attempt
# cap. Without it a wholly-failed attempt got one quoted clause per zeroed node
# in the narrative while `topics[]` was capped at two: one payload, two answers
# to "how much of the reference may this student see", and (restart_problem is
# reachable from REPORT, browse is best-grade-wins) a recitable A+.
# --------------------------------------------------------------------------


def _multi_topic_feedback(keys: list[str]) -> dict[str, Any]:
    return {
        "headline": "Here is the summary.",
        "topic_feedback": [
            {"canonical_key": key, "note": "You clearly nailed it.", "quote": None} for key in keys
        ],
        "recap": [],
        "next_step": "Keep going.",
    }


def _notes(result: dict[str, Any]) -> list[str]:
    return [item["note"] for item in result["topic_feedback"]]


def test_the_narrative_quote_budget_is_the_scorers_reveal_cap() -> None:
    """One constant, imported — the two surfaces cannot drift apart."""
    assert MAX_REFERENCE_NAME_QUOTES == MAX_REFERENCE_TEXT_REVEALS


def test_a_wholly_failed_attempt_quotes_at_most_two_reference_statements() -> None:
    """The prod shape (attempts 154/75/79/151/177): pure praise per zeroed
    topic, all five stripped, all five needing a gap sentence."""
    keys = [f"t{index}" for index in range(5)]
    topics = [_topic(key=key, credit=0.0, display_name=f"reference clause {key}") for key in keys]

    result = enforce_narrative_consistency(_multi_topic_feedback(keys), topics=topics)

    quoted = [note for note in _notes(result) if "reference clause" in note]
    assert len(quoted) == MAX_REFERENCE_NAME_QUOTES
    # Every zeroed topic still gets its gap named — only the wording is budgeted.
    assert all("next time" in note for note in _notes(result))
    assert sum(note.count("reference clause") for note in _notes(result)) == 2


def test_budgeted_out_topics_still_name_the_gap_without_the_wording() -> None:
    keys = [f"t{index}" for index in range(3)]
    topics = [_topic(key=key, credit=0.0, display_name=f"secret wording {key}") for key in keys]

    notes = _notes(enforce_narrative_consistency(_multi_topic_feedback(keys), topics=topics))

    unquoted = [note for note in notes if "secret wording" not in note]
    assert len(unquoted) == 1
    assert unquoted[0] == (
        "Apollo never got this idea from your teaching — walk through it explicitly next time."
    )


def test_the_quote_budget_goes_to_the_lowest_credit_topics_first() -> None:
    """Same ordering key as `topic_score._reveal_reference_text`, so the
    narrative names the same nodes `reference_text` reveals."""
    topics = [
        _topic(key="t_mid", credit=0.4, display_name="wording mid"),
        _topic(key="t_low", credit=0.0, display_name="wording low"),
        _topic(key="t_high", credit=0.5, display_name="wording high"),
    ]

    notes = " ".join(
        _notes(
            enforce_narrative_consistency(
                _multi_topic_feedback(["t_mid", "t_low", "t_high"]), topics=topics
            )
        )
    )

    assert "wording low" in notes and "wording mid" in notes
    assert "wording high" not in notes


def test_a_partial_topic_past_the_budget_uses_the_partial_wordless_line() -> None:
    topics = [
        _topic(key="t0", credit=0.0, display_name="wording zero"),
        _topic(key="t1", credit=0.1, display_name="wording one"),
        _topic(key="t2", credit=0.5, display_name="wording two"),
    ]

    notes = _notes(
        enforce_narrative_consistency(_multi_topic_feedback(["t0", "t1", "t2"]), topics=topics)
    )

    assert notes[2] == "Only part of this landed — make the rest explicit next time."


def test_the_next_step_never_spends_a_quote_the_budget_did_not_grant() -> None:
    """The next-step fallback quotes the subject's wording, so it obeys the same
    budget: with three zeroed topics the two notes exhaust it and the next step
    falls back to the wording-free move."""
    keys = [f"t{index}" for index in range(3)]
    topics = [_topic(key=key, credit=0.0, display_name=f"clause {key}") for key in keys]
    feedback = {**_multi_topic_feedback(keys), "next_step": "You clearly nailed clause t2."}

    result = enforce_narrative_consistency(feedback, topics=topics)

    assert "clause" not in result["next_step"]
    assert result["next_step"]


# --------------------------------------------------------------------------
# Zero-weight topics (review finding 3). A graded node excluded from the
# denominator (P1.2b `unprobed`: Apollo never asked) did not count toward the
# grade, so the student may not be told they failed to teach it.
# --------------------------------------------------------------------------


def test_unscored_topic_is_never_blamed_on_the_student() -> None:
    feedback = _feedback("This one stayed in the background.")

    result = enforce_narrative_consistency(feedback, topics=[_topic(credit=0.0, weight=0.0)])

    assert _note(result) == "This one stayed in the background."


def test_unscored_topic_emptied_by_praise_stripping_gets_a_neutral_note() -> None:
    feedback = _feedback("You clearly explained continuity throughout.")

    result = enforce_narrative_consistency(feedback, topics=[_topic(credit=0.0, weight=0.0)])

    note = _note(result)
    assert "clearly explained" not in note
    assert "did not count toward your grade" in note
    assert "never got this from your teaching" not in note


def test_next_step_subject_prefers_a_topic_that_counted() -> None:
    feedback = _feedback(
        "Make the continuity step explicit.",
        key="z_scored",
        next_step="",
    )

    result = enforce_narrative_consistency(
        feedback,
        topics=[
            _topic(key="a_unprobed", credit=0.0, weight=0.0, display_name="never asked topic"),
            _topic(key="z_scored", credit=0.0, weight=1.0, display_name="apply continuity"),
        ],
    )

    assert "apply continuity" in result["next_step"]
    assert "never asked topic" not in result["next_step"]


# --------------------------------------------------------------------------
# The quoted reference span (review findings 4 and 5).
# --------------------------------------------------------------------------


def test_clipped_reference_wording_never_leaves_an_unclosed_bracket() -> None:
    """67% of real prod graded names exceed the quote budget and 453's clips
    mid-parenthetical — the student must not read a bracket that never closes."""
    feedback = _feedback("You clearly explained the whole thing.")

    result = enforce_narrative_consistency(
        feedback, topics=[_topic(credit=0.0, display_name=_P453_CREDITED)]
    )

    note = _note(result)
    assert note.count("(") == note.count(")") == 0
    assert 'teaching: "Two forces threaten privacy: the growth of information technology…"' in note


def test_embedded_double_quotes_never_break_the_quoted_span() -> None:
    feedback = _feedback("You clearly explained the whole thing.")

    result = enforce_narrative_consistency(
        feedback,
        topics=[_topic(credit=0.0, display_name='The so-called "network effect" compounds.')],
    )

    note = _note(result)
    assert note.count('"') == 2
    assert "'network effect'" in note


def test_reference_wording_is_never_quoted_twice_in_one_payload() -> None:
    """The note's appended gap sentence already carries the clipped clause, so
    the next-step fallback may not repeat it."""
    feedback = _feedback(
        "You clearly explained the whole thing.",
        next_step="You clearly showed how decision makers covet valuable information.",
    )

    result = enforce_narrative_consistency(
        feedback, topics=[_topic(credit=0.0, display_name=_P453_UNCREDITED)]
    )

    clipped = "Information is increasingly valuable to policy makers and decision makers"
    assert clipped in _note(result)
    assert clipped not in result["next_step"]
    assert "covet" not in result["next_step"]
    assert result["next_step"]


def test_all_topics_unscored_still_yields_a_next_step() -> None:
    """Degenerate ledger — every graded node left the denominator."""
    feedback = _feedback("You clearly explained everything.", next_step="")

    result = enforce_narrative_consistency(feedback, topics=[_topic(credit=0.0, weight=0.0)])

    assert "apply continuity" in result["next_step"]


def test_a_closed_bracket_pair_is_kept_intact() -> None:
    """Balancing only removes brackets the clip left OPEN."""
    feedback = _feedback("You clearly explained the whole thing.")

    result = enforce_narrative_consistency(
        feedback, topics=[_topic(credit=0.0, display_name="Bernoulli (steady, inviscid) applies.")]
    )

    assert "Bernoulli (steady, inviscid) applies" in _note(result)


def test_a_name_that_opens_with_a_bracket_is_still_quotable() -> None:
    """Balancing may never empty the span — something is always quoted."""
    feedback = _feedback("You clearly explained the whole thing.")

    result = enforce_narrative_consistency(
        feedback, topics=[_topic(credit=0.0, display_name="(unclosed label")]
    )

    assert "(unclosed label" in _note(result)


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


def test_non_list_topic_feedback_passes_through() -> None:
    feedback: dict[str, Any] = {
        "headline": "Here is the summary.",
        "topic_feedback": {"canonical_key": "t1"},
        "recap": [],
        "next_step": "Show the continuity step.",
    }

    result = enforce_narrative_consistency(feedback, topics=[_topic(credit=0.0)])

    assert result["topic_feedback"] == {"canonical_key": "t1"}


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
