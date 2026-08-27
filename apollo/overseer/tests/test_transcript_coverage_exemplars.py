"""The calibration exemplars: paraphrase-only, and no code gate under them.

These are the two rails that survive the reverted 0.6 content floor (see
`transcript-coverage.md`, "the floor was tried and measured harmful"):

* **Paraphrase, never quote.** Every exemplar is drawn from a real Week-4 prod
  transcript, so an exemplar that copies a student's clause would ride that
  pilot student's own words along in every adjudication call this service ever
  makes. The P1.1 build did exactly that with attempt 158's clause until
  2026-08-08. This is pinned, not asserted in a comment.
* **The adjudicator is the grader of record.** The bar lives in the prompt; no
  length rail, no character count, no code-side clamp re-judges the credit the
  model returned. A rail would fail a terse-but-correct
  "privacy, accuracy, property, access" and pass a long empty paragraph.
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


# --------------------------------------------------------------------------- #
# Paraphrase, never quote
# --------------------------------------------------------------------------- #
def test_prompt_paraphrases_the_real_transcripts_and_quotes_no_student_verbatim():
    """The exemplars are drawn from real prod attempts (80, 158) but must never
    carry a student's actual words into every future grading call.

    The 158 clause is the live entry — the P1.1 build DID quote it verbatim
    until this rail was added. The other fragments are the transcripts the
    reverted floor's negative exemplars were drawn from (attempts 173, 174, 35);
    they are kept on the blocklist so a future floor attempt cannot reintroduce
    a verbatim quote without reddening CI.
    """
    prompt = build_system_prompt(_problem()).lower()
    for verbatim in (
        "the gap between the informed and uninformed widens",
        "not sure",
        "the four ethical issues are",
        "who is picasso",
    ):
        assert verbatim not in prompt, verbatim


def test_every_exemplified_anchor_keeps_at_least_two_worked_exemplars():
    """The anchor scale is calibrated by example, not by adjectives alone: the
    live calibration arm measured a 59.7% mid-anchor share against exactly this
    exemplar block, so the block is FROZEN — an anchor listed here must keep its
    >= 2 or that measurement no longer describes the served prompt.

    The 0.3 anchor added 2026-08-24 is deliberately absent from this list: it is
    defined by one prose line in `build_system_prompt` and by nothing here, so
    the exemplar block stayed byte-identical across the arm that cleared it.
    Giving 0.3 exemplars is a separate calibration change and needs its own arm —
    it is not a gap this test should paper over by asserting a fifth anchor that
    the prompt does not, and must not yet, carry."""
    prompt = build_system_prompt(_problem())
    assert "CALIBRATION EXAMPLES" in prompt
    assert "three of the four" in prompt
    assert "directionally right but thin" in prompt
    for anchor in (" 1.0 —", " 0.85 —", " 0.6 —", " 0 —"):
        assert prompt.count(anchor) >= 2, f"anchor {anchor!r} needs >= 2 exemplars"


def test_exemplars_survive_every_optional_prompt_frame():
    """Course evidence / Hoot asides / the live tally append AFTER the
    exemplars; none of them may displace the calibration block."""
    for kwargs in (
        {"course_evidence": "[1] excerpt"},
        {"hoot_asides": ("Hoot said this",)},
        {
            "tally_context": [{"node_id": "p1", "state": "understood", "times_asked": 1}],
            "reference_items": [
                {"id": "p1", "type": "procedure_step", "display_name": "Name the issues"}
            ],
        },
    ):
        prompt = build_system_prompt(_problem(), **kwargs)  # type: ignore[arg-type]
        assert "CALIBRATION EXAMPLES" in prompt, kwargs


# --------------------------------------------------------------------------- #
# There is no length gate — and no credit clamp — in code
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
    """No character-count gate: a terse-but-substantive answer ("PAPA is
    privacy, accuracy, property, access") keeps its credit."""
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


@pytest.mark.asyncio
async def test_credit_is_not_clamped_when_the_model_says_not_fully_covered():
    """`covered` in this adjudicator means FULLY covered, so `covered=False`
    with a mid credit is the NORMAL shape of a genuine partial — 104 of the 114
    mid credits in the 2026-08-08 replay carried it. A clamp keyed on `covered`
    was simulated over the 106-attempt replay and would have re-bimodalized the
    distribution (B band 16 -> 3, median 72 -> 49); it must never ship."""
    payload = {
        "verdicts": [
            {
                "node_id": "p1",
                "covered": False,
                "credit": 0.6,
                "confidence": 0.7,
                "evidence_span": "the people priced out lose access",
                "prompted": False,
                "corrected_later": False,
                "basis": "implied",
            }
        ]
    }
    transcript = [("student", "the people priced out lose access")]
    with patch("apollo.overseer.transcript_coverage.bounded_client", return_value=_client(payload)):
        coverage, _ = await compute_transcript_coverage_with_spans(transcript, _graph(), _problem())
    assert coverage["procedure_scores"]["p1"] == pytest.approx(0.6)
    # `per_step` still reports "missing" (covered AND credit >= 0.5), which is a
    # DIAGNOSTIC label — it never feeds back into the credit.
    assert coverage["per_step"]["p1"] == "missing"
