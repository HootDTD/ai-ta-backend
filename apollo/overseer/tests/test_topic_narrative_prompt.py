"""The structured topic-feedback prompt contains only required internals.

Canonical keys are supplied for the response mapping and may appear only in
``canonical_key`` fields. Decimal credit / weight / dock values remain absent.
"""

from __future__ import annotations

import pytest

from apollo.overseer.topic_narrative import build_topic_narrative_prompt
from apollo.overseer.topic_score import TopicCredit, TopicMisconception, TopicScoreResult

pytestmark = pytest.mark.unit


def _result(
    display_name: str | None = "Explain causality in directional systems",
) -> TopicScoreResult:
    return TopicScoreResult(
        score=64,
        letter="C",
        coverage_component=0.6359,
        misconception_dock=0.0,
        topics=(
            TopicCredit(
                canonical_key="proc_explain_causality",
                display_name=display_name,
                credit=0.9,
                status="covered",
                weight=0.23,
                misconceptions=(
                    TopicMisconception(
                        canonical_key="misc.wrong_direction",
                        resolved=True,
                        dock_points=0.05,
                        evidence_span="downstream changes rewrite the source",
                    ),
                ),
            ),
        ),
    )


def test_user_prompt_has_topic_canonical_key_but_no_scoring_decimals():
    _system, user = build_topic_narrative_prompt(
        _result(), problem_text="Explain upstream vs downstream."
    )
    assert 'canonical_key="proc_explain_causality"' in user
    assert "misc.wrong_direction" not in user
    assert "credit=" not in user and "weight=" not in user
    assert "0.23" not in user and "0.6359" not in user
    assert "Coverage component" not in user
    assert "Misconception dock" not in user


def test_user_prompt_carries_display_name_and_status_word_but_no_percent():
    """Study-prep 2026-08-23: the topic line used to end ``— 90%``, and that
    number is where "you scored 72%" in the prose came from. The status word is
    all the narrator gets now."""
    _system, user = build_topic_narrative_prompt(_result(), problem_text="P?")
    assert "Explain causality in directional systems" in user
    assert "covered" in user
    assert "90%" not in user
    assert "%" not in user
    assert "Score:" not in user


def test_misconception_line_keeps_span_and_resolution_only():
    _system, user = build_topic_narrative_prompt(_result(), problem_text="P?")
    assert "downstream changes rewrite the source" in user
    assert "corrected" in user
    assert "0.05" not in user


def test_missing_display_name_falls_back_to_humanized_key():
    _system, user = build_topic_narrative_prompt(_result(display_name=None), problem_text="P?")
    assert 'canonical_key="proc_explain_causality"' in user
    assert "explain causality" in user


def test_system_prompt_forbids_internals_and_every_numeric_grade():
    """Percentages used to be explicitly ALLOWED here ("available for
    prioritization"). Study-prep 2026-08-23 inverted that: no score, no
    percentage, no points, no letter — while subject-matter numbers stay
    welcome, so the rule cannot be read as "avoid numbers"."""
    system, _user = build_topic_narrative_prompt(_result(), problem_text="P?")
    assert "internal identifiers" in system
    assert "outside the canonical_key JSON fields" in system
    lowered = " ".join(system.lower().split())
    assert "never put a grade into words as a number" in lowered
    assert "no score, no percentage, no points" in lowered
    assert "numbers that belong to the subject matter" in lowered


def test_system_prompt_requires_exact_json_shape_and_gated_quotes():
    system, _user = build_topic_narrative_prompt(_result(), problem_text="P?")
    assert '"headline"' in system
    assert '"topic_feedback"' in system
    assert '"next_step"' in system
    assert "entire span exactly, character for character" in system
    assert "quote must be null" in system


def test_system_prompt_forbids_third_person_audit_feedback():
    system, _user = build_topic_narrative_prompt(_result(), problem_text="P?")
    lowered = " ".join(system.lower().split())
    assert 'speak to the student as "you" and "your"' in lowered
    assert 'never call them "the student,' in lowered
    assert "say nothing at all about misconceptions" in lowered


# ── 2026-08-07 P2.1: the narrative is written FROM the per-node verdicts ──


def test_system_prompt_bans_praise_below_the_credit_floor():
    """A topic the ledger did not credit must not be credited in prose (defect
    U2). Study-prep 2026-08-23 moved the currency from the percentage to the
    status word — the RULE is unchanged, so the block still has to name every
    uncredited status and still has to bind the headline and the next step."""
    system, _user = build_topic_narrative_prompt(_result(), problem_text="P?")
    lowered = " ".join(system.lower().split())
    assert "credit consistency" in lowered
    assert '"covered" is the only status that was credited' in lowered
    assert '"partially covered" was not credited' in lowered
    assert '"missing" was not credited at all' in lowered
    assert "the headline and the next step follow the same rule" in lowered
    # And the currency itself is gone: no percentage threshold survives.
    assert "60%" not in lowered and "0%" not in lowered


# ── 2026-07-14 narrative grounding: verbatim student transcript in the prompt ──


def test_user_prompt_includes_student_transcript_when_provided():
    _system, user = build_topic_narrative_prompt(
        _result(),
        problem_text="P?",
        student_utterances=("future shock is rapid change", "it disrupts social norms"),
    )
    assert "What the student actually said" in user
    assert '1. "future shock is rapid change"' in user
    assert '2. "it disrupts social norms"' in user


def test_user_prompt_omits_transcript_block_by_default():
    _system, user = build_topic_narrative_prompt(_result(), problem_text="P?")
    assert "What the student actually said" not in user


def test_blank_utterances_are_dropped_and_all_blank_omits_block():
    _system, user = build_topic_narrative_prompt(
        _result(), problem_text="P?", student_utterances=("", "  ", "real words")
    )
    assert '1. "real words"' in user
    assert "2." not in user
    _system, user2 = build_topic_narrative_prompt(
        _result(), problem_text="P?", student_utterances=("", "   ")
    )
    assert "What the student actually said" not in user2


def test_system_prompt_forbids_overstating_credited_topics():
    system, _user = build_topic_narrative_prompt(_result(), problem_text="P?")
    lower = system.lower()
    assert "what the student actually said" in lower
    assert "never expand a topic's name" in lower
    # Transcript must not re-grade: ledger stays authoritative.
    assert "authoritative" in lower


# ── 2026-07-14 per-session grounding: reference text is never the student's words ──


def _result_with_evidence(evidence_span: str | None) -> TopicScoreResult:
    return TopicScoreResult(
        score=70,
        letter="B",
        coverage_component=0.7,
        misconception_dock=0.0,
        topics=(
            TopicCredit(
                canonical_key="proc_when_started",
                display_name=(
                    "State when future shock was identified: Alvin Toffler named it "
                    "in his 1970 book"
                ),
                credit=0.7,
                status="covered",
                weight=1.0,
                misconceptions=(),
                evidence_span=evidence_span,
            ),
        ),
    )


def test_user_prompt_marks_topic_text_as_reference_wording():
    """The narrator must be able to tell reference wording apart from student
    speech — the evidence header says the topic descriptions are the
    reference solution's own words."""
    _system, user = build_topic_narrative_prompt(_result_with_evidence(None), problem_text="P?")
    assert "reference solution's own wording" in user


def test_user_prompt_quotes_student_evidence_when_present():
    _system, user = build_topic_narrative_prompt(
        _result_with_evidence("it started in 1970"), problem_text="P?"
    )
    assert 'You said: "it started in 1970"' in user


def test_user_prompt_has_no_you_said_line_without_evidence():
    _system, user = build_topic_narrative_prompt(_result_with_evidence(None), problem_text="P?")
    assert "You said:" not in user


def test_system_prompt_forbids_attributing_reference_content_to_student():
    """The exact failure this guards: the narrative told a student who wrote
    only '1970' that they 'referenced Alvin Toffler's 1970 book and the
    post-World-War-II era' — reference wording presented as the student's own
    statement."""
    system, _user = build_topic_narrative_prompt(_result_with_evidence(None), problem_text="P?")
    lowered = " ".join(system.lower().split())
    assert "not what the student said" in lowered
    assert 'quoted "you said"' in lowered
