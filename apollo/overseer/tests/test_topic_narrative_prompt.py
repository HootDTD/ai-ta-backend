"""2026-07-11 feedback spec §2 — the prompt itself must not contain internals.

Root-cause fix: the LLM can't leak canonical keys / credit / weight / dock if
they never reach the prompt. Display names + statuses + whole-number
percentages are the only per-topic data the narrator sees.
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


def test_user_prompt_has_no_canonical_keys_or_decimals():
    _system, user = build_topic_narrative_prompt(
        _result(), problem_text="Explain upstream vs downstream."
    )
    assert "proc_explain_causality" not in user
    assert "misc.wrong_direction" not in user
    assert "credit=" not in user and "weight=" not in user
    assert "0.23" not in user and "0.6359" not in user
    assert "Coverage component" not in user
    assert "Misconception dock" not in user


def test_user_prompt_carries_display_name_status_and_percent():
    _system, user = build_topic_narrative_prompt(_result(), problem_text="P?")
    assert "Explain causality in directional systems" in user
    assert "covered" in user
    assert "90%" in user
    assert "Score:" not in user


def test_misconception_line_keeps_span_and_resolution_only():
    _system, user = build_topic_narrative_prompt(_result(), problem_text="P?")
    assert "downstream changes rewrite the source" in user
    assert "corrected" in user
    assert "0.05" not in user


def test_missing_display_name_falls_back_to_humanized_key():
    _system, user = build_topic_narrative_prompt(_result(display_name=None), problem_text="P?")
    assert "proc_explain_causality" not in user
    assert "explain causality" in user


def test_system_prompt_forbids_internals_and_allows_percentages():
    system, _user = build_topic_narrative_prompt(_result(), problem_text="P?")
    assert "internal identifiers" in system
    assert "percentage" in system.lower()


def test_system_prompt_forbids_third_person_audit_feedback():
    system, _user = build_topic_narrative_prompt(_result(), problem_text="P?")
    lowered = " ".join(system.lower().split())
    assert 'speak to the student as "you" and "your"' in lowered
    assert 'never call them "the student,' in lowered
    assert "say nothing at all about misconceptions" in lowered


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
    assert '2.' not in user
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
