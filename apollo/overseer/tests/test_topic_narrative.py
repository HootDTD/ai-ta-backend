"""2026-07-10 topic-score design spec §4 — the ledger-grounded narrative
prompt. Pure module (no IO): ``build_topic_narrative_prompt`` must assemble a
prompt that names every topic + misconception in the ledger, with the exact
evidence spans, `$...$`-only math delimiters, and nothing else score-related
invented.

2026-07-11 feedback spec §2 updated the contract: canonical keys and decimal
credit/weight/dock values are internals and must NOT reach the prompt (see
``test_topic_narrative_prompt.py` for the dedicated internals-leak suite).
This file keeps the broader structural/shape assertions (problem text,
score/letter, math delimiters, next-step line, degrade-gracefully paths)."""

from __future__ import annotations

import dataclasses

from apollo.overseer.topic_narrative import build_topic_narrative_prompt
from apollo.overseer.topic_score import TopicCredit, TopicMisconception, TopicScoreResult


def _result(**overrides: object) -> TopicScoreResult:
    base = TopicScoreResult(
        score=72,
        letter="B-",
        coverage_component=0.8,
        misconception_dock=0.08,
        topics=(
            TopicCredit(
                canonical_key="eq1",
                display_name="Bernoulli equation",
                credit=1.0,
                status="covered",
                weight=0.5,
                misconceptions=(
                    TopicMisconception(
                        canonical_key="misc.sign_flip",
                        resolved=False,
                        dock_points=0.08,
                        evidence_span="pressure always increases downstream",
                    ),
                ),
            ),
            TopicCredit(
                canonical_key="c1",
                display_name=None,
                credit=0.0,
                status="missing",
                weight=0.3,
                misconceptions=(),
            ),
            TopicCredit(
                canonical_key="p1",
                display_name="apply continuity",
                credit=0.5,
                status="partial",
                weight=0.2,
                misconceptions=(),
            ),
        ),
    )
    return dataclasses.replace(base, **overrides)  # type: ignore[arg-type]


def test_prompt_contains_every_topic_display_name_or_humanized_fallback():
    # 2026-07-11 feedback spec §2: canonical keys never reach the prompt as
    # ledger internals — display name (or its humanized fallback) is the
    # topic's identity. `_humanize_key` degrades snake_case/prefixed keys to
    # readable phrases; a bare short key like "c1" has no prefix/underscore
    # to strip and so round-trips unchanged, which is fine since it no
    # longer reads as an internal identifier in that form.
    result = _result()
    _system, user = build_topic_narrative_prompt(result, problem_text="Water flows...")

    assert "Bernoulli equation" in user
    assert "apply continuity" in user
    # The prefixed/underscored keys must never appear verbatim.
    assert "eq1" not in user
    assert "p1" not in user


def test_prompt_contains_display_name_when_present():
    result = _result()
    _system, user = build_topic_narrative_prompt(result, problem_text="p")
    assert "Bernoulli equation" in user


def test_prompt_contains_every_misconception_evidence_span_but_not_key_or_dock():
    # 2026-07-11 feedback spec §2: evidence span survives; canonical key and
    # decimal dock points are internals and must not reach the prompt.
    result = _result()
    _system, user = build_topic_narrative_prompt(result, problem_text="p")

    assert "pressure always increases downstream" in user
    assert "misc.sign_flip" not in user
    assert "0.08" not in user  # dock_points no longer formatted into the prompt


def test_prompt_marks_resolved_vs_uncorrected():
    resolved_result = _result(
        topics=(
            TopicCredit(
                canonical_key="eq1",
                display_name=None,
                credit=1.0,
                status="covered",
                weight=1.0,
                misconceptions=(
                    TopicMisconception(
                        canonical_key="misc.x",
                        resolved=True,
                        dock_points=0.0,
                        evidence_span="corrected statement",
                    ),
                ),
            ),
        ),
    )
    _system, user = build_topic_narrative_prompt(resolved_result, problem_text="p")
    assert "corrected" in user
    assert "uncorrected" not in user.split("corrected")[0] or "corrected" in user


def test_prompt_includes_problem_text():
    result = _result()
    _system, user = build_topic_narrative_prompt(result, problem_text="SENTINEL_PROBLEM")
    assert "SENTINEL_PROBLEM" in user


def test_prompt_includes_score_and_letter_but_not_component_decimals():
    # 2026-07-11 feedback spec §2: coverage_component / misconception_dock
    # decimals are internals and no longer appear in the prompt.
    result = _result()
    _system, user = build_topic_narrative_prompt(result, problem_text="p")
    assert "72" in user
    assert "B-" in user
    assert "Coverage component" not in user
    assert "Misconception dock" not in user


def test_system_prompt_instructs_dollar_delimiter_only():
    result = _result()
    system, _user = build_topic_narrative_prompt(result, problem_text="p")
    assert "$...$" in system
    # The prompt explicitly forbids the axis path's other delimiters — they
    # may appear ONLY inside the "never" prohibition, never as the
    # recommended form.
    assert r"never `\( \)`" in system or r"never" in system.lower()


def test_system_prompt_forbids_claims_beyond_ledger():
    result = _result()
    system, _user = build_topic_narrative_prompt(result, problem_text="p")
    sys_lower = system.lower()
    assert "ledger" in sys_lower
    assert "not" in sys_lower and "claim" in sys_lower


def test_system_prompt_instructs_next_step_line():
    result = _result()
    system, _user = build_topic_narrative_prompt(result, problem_text="p")
    assert "Next step:" in system


def test_no_topics_degrades_gracefully():
    empty_result = TopicScoreResult(
        score=0,
        letter="F",
        coverage_component=0.0,
        misconception_dock=0.0,
        topics=(),
    )
    _system, user = build_topic_narrative_prompt(empty_result, problem_text="p")
    assert "no topics graded" in user


def test_evidence_span_none_renders_placeholder_not_crash():
    result = TopicScoreResult(
        score=50,
        letter="D",
        coverage_component=0.5,
        misconception_dock=0.0,
        topics=(
            TopicCredit(
                canonical_key="eq1",
                display_name=None,
                credit=1.0,
                status="covered",
                weight=1.0,
                misconceptions=(
                    TopicMisconception(
                        canonical_key="misc.x",
                        resolved=False,
                        dock_points=0.0,
                        evidence_span=None,
                    ),
                ),
            ),
        ),
    )
    _system, user = build_topic_narrative_prompt(result, problem_text="p")
    assert "(no evidence span)" in user


def test_returns_tuple_of_two_strings():
    result = _result()
    out = build_topic_narrative_prompt(result, problem_text="p")
    assert isinstance(out, tuple)
    assert len(out) == 2
    system, user = out
    assert isinstance(system, str)
    assert isinstance(user, str)
