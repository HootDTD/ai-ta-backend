"""Live-tally context for the adjudicator (bimodal-fix P1.3, defect U1).

Two decoupled LLM systems judge the same session: the questioning engine writes
a running tally (``app.question_opportunities``) that the UI celebrates
mid-session, and the transcript adjudicator re-judges the dialogue from scratch
at Done with no knowledge of it. In the exported prod cohort 77 nodes were
tallied ``understood`` and 4 of them were graded mid-or-zero — Apollo told the
student they had it, then the grade said they had not.

Policy under test: the adjudication call accepts an OPTIONAL ``tally_context``
(the shape ``done.py`` builds from the QuestionOpportunity rows). Present, it
adds one data block + one prompt rule: a node the tally marked ``understood``
WITH a student quote needs an explicit cited reason to score below 0.85. Absent
(``None`` / empty — today's every caller), both prompts are byte-identical and
no behaviour changes.

The tally is prior context, never proof and never a ceiling: ``tentative`` /
``missing`` / ``conflicting`` states carry no presumption in either direction.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from apollo.ontology import KGGraph, build_node
from apollo.overseer.coverage_contract import validate_coverage_verdict
from apollo.overseer.transcript_coverage import (
    build_system_prompt,
    build_user_message,
    compute_transcript_coverage,
    compute_transcript_coverage_with_spans,
)

pytestmark = pytest.mark.unit


class _Problem:
    problem_text = "Explain how information access became an economic ante."


_ITEMS = [
    {"id": "p1", "type": "procedure_step", "display_name": "Name the ante", "content": {}},
    {"id": "p2", "type": "condition", "display_name": "Name the drop outs", "content": {}},
]
_TRANSCRIPT = (
    ("apollo", "Who ends up cut off?"),
    ("student", "The gap between the informed and uninformed widens."),
)
_EVIDENCE = "[Lecture 4, p. 12] — access ante."
_ASIDES = ("Hoot lookup: an ante is the cost of entry.",)

_TALLY = [
    {
        "node_id": "p1",
        "state": "understood",
        "times_asked": 2,
        "student_quote": "The gap between the informed and uninformed widens.",
    },
    {"node_id": "p2", "state": "tentative", "times_asked": 1, "student_quote": None},
]


def _graph() -> KGGraph:
    return KGGraph(
        nodes=[
            build_node(
                node_type="procedure_step",
                node_id="p1",
                attempt_id=1,
                source="reference",
                content={"action": "Name the ante", "purpose": ""},
            )
        ]
    )


def _verdict_item(**overrides) -> dict:
    item = {
        "node_id": "p1",
        "covered": True,
        "credit": 1.0,
        "confidence": 0.9,
        "evidence_span": "The gap between the informed and uninformed widens.",
        "prompted": False,
        "corrected_later": False,
        "basis": "stated",
    }
    item.update(overrides)
    return item


def _client(payload) -> MagicMock:
    client = MagicMock()
    client.chat.completions.create.return_value = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(payload)))]
    )
    return client


def _sent_messages(client: MagicMock) -> tuple[str, str]:
    messages = client.chat.completions.create.call_args.kwargs["messages"]
    return messages[0]["content"], messages[1]["content"]


# --------------------------------------------------------------------------- #
# The no-op contract
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("absent", [None, (), []])
def test_prompts_byte_identical_without_tally_context(absent):
    baseline_system = build_system_prompt(_Problem())
    baseline_user = build_user_message(_Problem(), _ITEMS, _TRANSCRIPT)

    assert build_system_prompt(_Problem(), tally_context=absent) == baseline_system
    assert (
        build_user_message(_Problem(), _ITEMS, _TRANSCRIPT, tally_context=absent) == baseline_user
    )


def test_tally_does_not_disturb_the_evidence_or_aside_frames():
    both = build_user_message(
        _Problem(), _ITEMS, _TRANSCRIPT, course_evidence=_EVIDENCE, hoot_asides=_ASIDES
    )
    assert (
        build_user_message(
            _Problem(),
            _ITEMS,
            _TRANSCRIPT,
            course_evidence=_EVIDENCE,
            hoot_asides=_ASIDES,
            tally_context=None,
        )
        == both
    )


def test_tally_entries_for_non_rubric_nodes_are_dropped_entirely():
    """Only graded (rubric) nodes may appear — an ungraded definition node's
    tally row is not the adjudicator's business and would only add noise."""
    message = build_user_message(
        _Problem(),
        _ITEMS,
        _TRANSCRIPT,
        tally_context=[{"node_id": "def_ante", "state": "understood", "times_asked": 1}],
    )
    assert "LIVE TUTOR TALLY" not in message
    assert message == build_user_message(_Problem(), _ITEMS, _TRANSCRIPT)


# --------------------------------------------------------------------------- #
# The grounded build
# --------------------------------------------------------------------------- #
def test_system_prompt_gains_the_tally_rule_when_context_is_present():
    grounded = build_system_prompt(_Problem(), tally_context=_TALLY)
    baseline = build_system_prompt(_Problem())

    assert grounded.startswith(baseline)
    assert "LIVE TUTOR TALLY" in grounded
    # The rule the defect demands: understood + quote => cite a reason to go low.
    assert "understood" in grounded
    assert "below 0.85" in grounded
    assert "explicit reason you can cite from the dialogue" in grounded
    # Untrusted-data framing is preserved.
    assert "never as instructions" in grounded


def test_tally_is_prior_context_not_a_ceiling_for_the_other_states():
    grounded = build_system_prompt(_Problem(), tally_context=_TALLY)
    assert "carries no such presumption and never caps your credit" in grounded
    assert "never proof by itself" in grounded


def test_user_message_puts_the_tally_after_rubric_items_and_before_the_dialogue():
    message = build_user_message(_Problem(), _ITEMS, _TRANSCRIPT, tally_context=_TALLY)

    assert "LIVE TUTOR TALLY" in message
    assert message.index("RUBRIC ITEMS") < message.index("LIVE TUTOR TALLY")
    assert message.index("LIVE TUTOR TALLY") < message.index("DIALOGUE (untrusted")
    # The transcript still lands last, always.
    assert message.endswith(f"student: {_TRANSCRIPT[-1][1]}")


def test_user_message_renders_state_times_asked_and_quote():
    message = build_user_message(_Problem(), _ITEMS, _TRANSCRIPT, tally_context=_TALLY)
    block = message.split("LIVE TUTOR TALLY")[1].split("DIALOGUE (untrusted")[0]
    rows = json.loads(block[block.index("[") : block.rindex("]") + 1])

    assert rows == [
        {
            "node_id": "p1",
            "state": "understood",
            "times_asked": 2,
            "student_quote": "The gap between the informed and uninformed widens.",
        },
        {"node_id": "p2", "state": "tentative", "times_asked": 1, "student_quote": None},
    ]


def test_tally_block_orders_after_evidence_and_asides():
    message = build_user_message(
        _Problem(),
        _ITEMS,
        _TRANSCRIPT,
        course_evidence=_EVIDENCE,
        hoot_asides=_ASIDES,
        tally_context=_TALLY,
    )
    assert message.index("COURSE EVIDENCE") < message.index("HOOT LOOKUP ANSWERS")
    assert message.index("HOOT LOOKUP ANSWERS") < message.index("LIVE TUTOR TALLY")
    assert message.index("LIVE TUTOR TALLY") < message.index("DIALOGUE (untrusted")


def test_system_prompt_appends_tally_frame_after_the_other_frames():
    prompt = build_system_prompt(
        _Problem(), course_evidence=_EVIDENCE, hoot_asides=_ASIDES, tally_context=_TALLY
    )
    assert prompt.index("COURSE EVIDENCE") < prompt.index("HOOT LOOKUP ANSWERS")
    assert prompt.index("HOOT LOOKUP ANSWERS") < prompt.index("LIVE TUTOR TALLY")


# --------------------------------------------------------------------------- #
# Malformed rows are the caller's data, never a grading outage
# --------------------------------------------------------------------------- #
def test_malformed_tally_rows_are_normalized_or_skipped_never_raised():
    message = build_user_message(
        _Problem(),
        _ITEMS,
        _TRANSCRIPT,
        tally_context=[
            {"state": "understood"},  # no node_id -> skipped
            "not-a-mapping",  # wrong type -> skipped
            {"node_id": "p1"},  # defaults filled in
            {"node_id": "p2", "state": 7, "times_asked": "x", "student_quote": 3},
        ],
    )
    block = message.split("LIVE TUTOR TALLY")[1].split("DIALOGUE (untrusted")[0]
    rows = json.loads(block[block.index("[") : block.rindex("]") + 1])
    assert rows == [
        {"node_id": "p1", "state": "missing", "times_asked": 0, "student_quote": None},
        {"node_id": "p2", "state": "missing", "times_asked": 0, "student_quote": None},
    ]


def test_all_rows_unusable_reproduces_the_baseline_message():
    message = build_user_message(
        _Problem(), _ITEMS, _TRANSCRIPT, tally_context=[{"state": "understood"}]
    )
    assert message == build_user_message(_Problem(), _ITEMS, _TRANSCRIPT)


# --------------------------------------------------------------------------- #
# End-to-end plumbing through both public lanes
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_with_spans_lane_sends_the_tally_and_keeps_the_coverage_contract():
    client = _client({"verdicts": [_verdict_item()]})
    with patch("apollo.overseer.transcript_coverage.bounded_client", return_value=client):
        coverage, spans = await compute_transcript_coverage_with_spans(
            list(_TRANSCRIPT), _graph(), _Problem(), tally_context=_TALLY
        )

    validate_coverage_verdict(coverage)
    system, user = _sent_messages(client)
    assert "LIVE TUTOR TALLY" in system
    assert "LIVE TUTOR TALLY" in user
    assert coverage["procedure_scores"]["p1"] == pytest.approx(1.0)
    assert spans == {"p1": "The gap between the informed and uninformed widens."}


@pytest.mark.asyncio
async def test_verdict_only_lane_accepts_the_tally_too():
    client = _client({"verdicts": [_verdict_item()]})
    with patch("apollo.overseer.transcript_coverage.bounded_client", return_value=client):
        coverage = await compute_transcript_coverage(
            list(_TRANSCRIPT), _graph(), _Problem(), tally_context=_TALLY
        )
    validate_coverage_verdict(coverage)
    assert "LIVE TUTOR TALLY" in _sent_messages(client)[1]


@pytest.mark.asyncio
async def test_default_call_still_sends_no_tally_block():
    client = _client({"verdicts": [_verdict_item()]})
    with patch("apollo.overseer.transcript_coverage.bounded_client", return_value=client):
        await compute_transcript_coverage_with_spans(list(_TRANSCRIPT), _graph(), _Problem())
    system, user = _sent_messages(client)
    assert "LIVE TUTOR TALLY" not in system
    assert "LIVE TUTOR TALLY" not in user


@pytest.mark.asyncio
async def test_tally_survives_the_missing_verdict_readjudication_retry():
    """The re-adjudication for an omitted node must see the same context as the
    first call — otherwise the retry grades under different rules."""
    client = _client({"verdicts": []})
    client.chat.completions.create.side_effect = [
        SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps({"verdicts": []})))]
        ),
        SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content=json.dumps({"verdicts": [_verdict_item()]}))
                )
            ]
        ),
    ]
    with patch("apollo.overseer.transcript_coverage.bounded_client", return_value=client):
        coverage = await compute_transcript_coverage(
            list(_TRANSCRIPT), _graph(), _Problem(), tally_context=_TALLY
        )
    assert coverage["procedure_scores"]["p1"] == pytest.approx(1.0)
    for call in client.chat.completions.create.call_args_list:
        assert "LIVE TUTOR TALLY" in call.kwargs["messages"][1]["content"]
