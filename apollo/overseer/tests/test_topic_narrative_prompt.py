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
    assert "Score: 64 (C)" in user


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
