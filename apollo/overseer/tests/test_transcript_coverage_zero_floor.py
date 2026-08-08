"""The 0.6 content floor (bimodal-fix P1.1 hardening, 2026-08-08).

Wave-3 replay + adversarial audit finding (`replay/run-p1/report.md` §7): P1.1
successfully un-collapsed the credit distribution, but it bought the middle of
the scale too cheaply. ALL 25 of the replay's C(60) rows were the same degenerate
cell — every graded credit exactly 0.6 — and all 25 came from a prod F. Reading
the transcripts behind them, three real over-credit patterns account for the
worst of it:

* an attempt whose entire student contribution was an eight-character admission
  of not knowing, credited 0.6/0.6 -> C(60);
* an attempt whose entire contribution was a 27-character sentence opener
  announcing a list and naming none of it — and that fragment WAS the recorded
  evidence span;
* an attempt where the student only asked Apollo questions and taught nothing.

The correction is a prompt-level floor, deliberately NOT a character-count gate
in code: 0.6 is the lowest credit that asserts the student taught something, so
it requires the student's own words to carry part of the item's substance.
Contentless, sub-sentence, question-only and category-without-content
contributions are 0.

The floor must not over-tighten in the other direction. The same audit found
attempt 189 under-credited: the student mapped three of five guarantees to their
indignities in their own words and scored 0.0. Partial enumeration in the
student's own words is real content and is pinned here at 0.6.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from apollo.ontology import KGGraph, build_node
from apollo.overseer.transcript_coverage import (
    build_system_prompt,
    compute_transcript_coverage_with_spans,
)

pytestmark = pytest.mark.unit


def _problem() -> SimpleNamespace:
    return SimpleNamespace(problem_text="Explain the framework")


def _items() -> list[dict]:
    return [{"id": "p1", "type": "procedure_step", "display_name": "Name the issues"}]


# --------------------------------------------------------------------------- #
# The floor rail
# --------------------------------------------------------------------------- #
def test_zero_floor_rail_is_stated_before_the_calibration_examples():
    """Rule first, then the worked examples that illustrate it — the exemplars
    are calibration for a bar the prompt has already set, not the bar itself."""
    prompt = build_system_prompt(_problem())
    assert "THE 0.6 FLOOR" in prompt
    assert prompt.index("THE 0.6 FLOOR") < prompt.index("CALIBRATION EXAMPLES")


def test_floor_requires_the_students_own_words_to_carry_item_substance():
    prompt = build_system_prompt(_problem())
    assert "0.6 is not a participation score" in prompt
    assert "lowest credit that still asserts the student taught something" in prompt
    assert "part of this item's mechanism, definition, or claim" in prompt
    # Graded-right-or-wrong is the operational test the grader can actually apply.
    assert "could mark it right or wrong" in prompt


def test_floor_names_the_contentless_patterns_that_must_score_zero():
    """The four real over-credit shapes, spelled out so the model does not have
    to infer them from the anchor adjectives alone."""
    prompt = build_system_prompt(_problem())
    assert "an admission that they do not know it" in prompt
    assert "announces an answer and stops before delivering it" in prompt
    assert "category, title, or label with none of its content" in prompt
    assert "a question put to Apollo" in prompt


def test_floor_makes_the_evidence_span_the_operational_test():
    """Attempt 174's 0.6 was anchored on an evidence span that stated nothing.
    A span that is not itself a claim about the item is not evidence."""
    prompt = build_system_prompt(_problem())
    assert "The evidence_span is the test" in prompt
    assert "not itself a claim about this item's substance, the credit is 0" in prompt


def test_zero_anchor_is_defined_by_the_students_words_not_the_dialogue():
    """Pre-hardening the 0 anchor read 'the dialogue shows nothing that bears on
    the item' — which an Apollo turn that explains the item satisfies. The
    subject of the sentence has to be the student."""
    prompt = build_system_prompt(_problem())
    assert "0 means the student's own words carry none of the item's substance" in prompt
    assert "0 means the dialogue shows nothing that bears on the item" not in prompt


# --------------------------------------------------------------------------- #
# The exemplars
# --------------------------------------------------------------------------- #
def test_zero_anchor_gains_exemplars_for_the_three_real_overcredit_patterns():
    prompt = build_system_prompt(_problem())
    # The leading space keeps " 0 —" from matching inside " 1.0 —".
    assert prompt.count(" 0 —") >= 5
    assert "entire contribution is that they do not know" in prompt
    assert "only the opening of a list" in prompt
    assert "only asks Apollo about the item" in prompt


def test_negative_exemplars_say_zero_not_zero_point_six_explicitly():
    """Each negative exemplar has to close the specific mistake it describes —
    'that is 0, not 0.6' — because 0.6 is exactly where these landed."""
    prompt = build_system_prompt(_problem())
    floor_block = prompt[prompt.index("CALIBRATION EXAMPLES") :]
    assert floor_block.count("not 0.6") >= 2


def test_partial_enumeration_in_the_students_own_words_stays_worth_zero_point_six():
    """The counter-exemplar that stops the floor over-tightening (attempt 189):
    three of five mapped correctly in the student's own words is thin, not
    absent."""
    prompt = build_system_prompt(_problem())
    assert "Partial enumeration in the student's own words is real content" in prompt
    assert "that is 0.6, not 0." in prompt


def test_the_existing_anchor_exemplars_are_not_displaced():
    """The hardening is additive: the 1.0 / 0.85 / 0.6 calibration that the live
    arm measured at a 59.7% mid-anchor share must survive it."""
    prompt = build_system_prompt(_problem())
    assert "three of the four" in prompt
    assert "directionally right but thin" in prompt
    for anchor in (" 1.0 —", " 0.85 —", " 0.6 —"):
        assert prompt.count(anchor) >= 2, f"anchor {anchor!r} needs >= 2 exemplars"


def test_floor_rail_survives_every_optional_prompt_frame():
    """Course evidence / Hoot asides / the live tally append AFTER the floor;
    none of them may displace it."""
    for kwargs in (
        {"course_evidence": "[1] excerpt"},
        {"hoot_asides": ("Hoot said this",)},
        {
            "tally_context": [{"node_id": "p1", "state": "understood", "times_asked": 1}],
            "reference_items": _items(),
        },
    ):
        prompt = build_system_prompt(_problem(), **kwargs)  # type: ignore[arg-type]
        assert "THE 0.6 FLOOR" in prompt, kwargs


def test_floor_does_not_contradict_the_tally_burden_of_proof_rule():
    """P1.3's rule raises the bar for scoring a tally-understood node BELOW 0.85;
    the floor raises the bar for scoring anything ABOVE 0. Both must be present
    and neither may be phrased as a cap on the other."""
    prompt = build_system_prompt(
        _problem(),
        tally_context=[{"node_id": "p1", "state": "understood", "student_quote": "I said it"}],
        reference_items=_items(),
    )
    assert "THE 0.6 FLOOR" in prompt
    assert "needs an explicit reason you can cite from the dialogue" in prompt
    assert prompt.index("THE 0.6 FLOOR") < prompt.index("LIVE TUTOR TALLY")


def test_prompt_paraphrases_the_real_transcripts_and_quotes_no_student_verbatim():
    """The exemplars are drawn from real prod attempts (80, 158, 173, 174, 35,
    189) but must never carry a student's actual words into every future grading
    call. The last entry is attempt 158's clause, which the P1.1 build DID quote
    verbatim until this rail was added."""
    prompt = build_system_prompt(_problem()).lower()
    for verbatim in (
        "not sure",
        "the four ethical issues are",
        "who is picasso",
        "the gap between the informed and uninformed widens",
    ):
        assert verbatim not in prompt, verbatim


# --------------------------------------------------------------------------- #
# It is a PROMPT floor — there is no length gate in code
# --------------------------------------------------------------------------- #
def _graph() -> KGGraph:
    return KGGraph(
        nodes=[
            build_node(
                node_type="procedure_step",
                node_id="p1",
                attempt_id=1,
                source="reference",
                content={"action": "Name the issues", "purpose": ""},
            )
        ]
    )


def _client(payload: dict) -> MagicMock:
    client = MagicMock()
    client.chat.completions.create.return_value = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(payload)))]
    )
    return client


@pytest.mark.asyncio
async def test_a_short_span_still_earns_the_credit_the_adjudicator_judged():
    """No character-count gate: the adjudicator is the grader of record, and a
    terse-but-substantive answer ("PAPA is privacy, accuracy, property,
    access") must keep its credit. The floor lives in the prompt precisely so
    code never has to guess at length."""
    payload = {
        "verdicts": [
            {
                "node_id": "p1",
                "covered": True,
                "credit": 0.6,
                "confidence": 0.8,
                "evidence_span": "privacy, accuracy, property, access",
                "prompted": False,
                "corrected_later": False,
                "basis": "stated",
            }
        ]
    }
    transcript = [("student", "privacy, accuracy, property, access")]
    with patch("apollo.overseer.transcript_coverage.bounded_client", return_value=_client(payload)):
        coverage, spans = await compute_transcript_coverage_with_spans(
            transcript, _graph(), _problem()
        )
    assert coverage["procedure_scores"]["p1"] == pytest.approx(0.6)
    assert spans == {"p1": "privacy, accuracy, property, access"}
