"""INTERACTION5 — the Hoot-assist narrative surfacing (Agent D).

When one or more topics carry the ledger's ``hoot_assisted`` flag, the
ledger-grounded narrative prompt gains a ``Topics Hoot answered for you`` labeled
data block (naming ONLY the assisted topics) plus a ``HOOT-ASSISTED TOPICS``
system rule. With NO assisted topic, BOTH messages must stay byte-identical to
the pre-INTERACTION5 build — this is the same house pattern the course-evidence
(INTERACTION2) addition follows.
"""

from __future__ import annotations

import pytest

from apollo.overseer.topic_narrative import build_topic_narrative_prompt
from apollo.overseer.topic_score import TopicCredit, TopicScoreResult

pytestmark = pytest.mark.unit

_PROBLEM = "A pipe narrows. Explain what happens to the pressure."
_EVIDENCE = "[Lecture 4, p. 12] — 2.1 Streamlines\nHead form: p/(rho g) + v^2/(2g) + z = H."
_UTTERANCES = ("Total head stays constant along the streamline.",)


def _topic(
    key: str,
    *,
    display_name: str | None,
    hoot_assisted: bool = False,
) -> TopicCredit:
    return TopicCredit(
        canonical_key=key,
        display_name=display_name,
        credit=0.4,
        status="partial",
        weight=0.5,
        misconceptions=(),
        evidence_span=None,
        hoot_assisted=hoot_assisted,
    )


def _result(*topics: TopicCredit) -> TopicScoreResult:
    return TopicScoreResult(
        score=50,
        letter="F",
        coverage_component=0.5,
        misconception_dock=0.0,
        topics=topics,
    )


# --------------------------------------------------------------------------- #
# The no-op contract: no assisted topic => byte-identical to today
# --------------------------------------------------------------------------- #
def test_prompts_byte_identical_when_no_topic_is_assisted():
    """A ledger where every ``hoot_assisted`` is False must build both messages
    exactly as the pre-INTERACTION5 code did (no data block, no system rule)."""
    unassisted = _result(
        _topic("eq1", display_name="Bernoulli"),
        _topic("c1", display_name="Steady flow"),
    )
    system, user = build_topic_narrative_prompt(
        unassisted, problem_text=_PROBLEM, student_utterances=_UTTERANCES
    )

    assert "HOOT-ASSISTED TOPICS" not in system
    assert "Topics Hoot answered for you" not in user


def test_no_op_contract_holds_with_course_evidence_but_no_assist():
    """course_evidence present but no assisted topic => still no Hoot rule/block,
    and the system prompt is exactly the course-only build."""
    unassisted = _result(_topic("eq1", display_name="Bernoulli"))
    course_only_system, _ = build_topic_narrative_prompt(
        unassisted, problem_text=_PROBLEM, course_evidence=_EVIDENCE
    )

    assert "COURSE MATERIALS" in course_only_system
    assert "HOOT-ASSISTED TOPICS" not in course_only_system


# --------------------------------------------------------------------------- #
# The assisted build
# --------------------------------------------------------------------------- #
def test_system_prompt_gains_hoot_rule_only_when_assisted():
    assisted = _result(_topic("eq1", display_name="Bernoulli", hoot_assisted=True))
    baseline = _result(_topic("eq1", display_name="Bernoulli"))

    grounded_system, _ = build_topic_narrative_prompt(assisted, problem_text=_PROBLEM)
    baseline_system, _ = build_topic_narrative_prompt(baseline, problem_text=_PROBLEM)

    assert grounded_system.startswith(baseline_system)
    assert "HOOT-ASSISTED TOPICS" in grounded_system
    # Normalize wrapped whitespace (phrases straddle line breaks in the prompt).
    normalized = " ".join(grounded_system.split())
    # The encouraging, student-voiced clause + the never-as-understanding guard.
    assert "next time try teaching it yourself" in normalized
    assert "NEVER the student's understanding" in normalized
    assert 'never place it in a "quote" field' in normalized


def test_user_message_lists_only_the_assisted_topics():
    result = _result(
        _topic("eq1", display_name="Bernoulli", hoot_assisted=True),
        _topic("c1", display_name="Steady flow"),  # NOT assisted
    )
    _system, user = build_topic_narrative_prompt(result, problem_text=_PROBLEM)

    assert "Topics Hoot answered for you" in user
    # The assisted topic is named in the block...
    assert 'canonical_key="eq1", name="Bernoulli"' in user
    # ...and the unassisted topic never appears inside the assist block.
    block = user.split("Topics Hoot answered for you", 1)[1]
    assert "c1" not in block
    assert "Steady flow" not in block


def test_assist_block_precedes_the_student_transcript():
    """The student's own words stay last and most salient."""
    result = _result(_topic("eq1", display_name="Bernoulli", hoot_assisted=True))
    _system, user = build_topic_narrative_prompt(
        result, problem_text=_PROBLEM, student_utterances=_UTTERANCES
    )
    assert user.index("Topics Hoot answered for you") < user.index("What the student actually said")


def test_assisted_line_falls_back_to_humanized_key_without_display_name():
    result = _result(_topic("proc_apply_continuity", display_name=None, hoot_assisted=True))
    _system, user = build_topic_narrative_prompt(result, problem_text=_PROBLEM)

    assert 'canonical_key="proc_apply_continuity"' in user
    # snake_case key degraded to a readable phrase, never the raw key as the name.
    assert 'name="apply continuity"' in user


def test_course_evidence_and_hoot_assist_stack_in_order():
    """Both flags on: system stacks course rules then Hoot rules; the user
    message keeps course materials before the assist block before the transcript."""
    result = _result(_topic("eq1", display_name="Bernoulli", hoot_assisted=True))
    system, user = build_topic_narrative_prompt(
        result,
        problem_text=_PROBLEM,
        student_utterances=_UTTERANCES,
        course_evidence=_EVIDENCE,
    )

    assert system.index("COURSE MATERIALS") < system.index("HOOT-ASSISTED TOPICS")
    assert (
        user.index("Course materials")
        < user.index("Topics Hoot answered for you")
        < user.index("What the student actually said")
    )
