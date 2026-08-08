"""Anchored partial credit for the adjudicator (bimodal-fix P1.1).

Root cause (2026-08-07 spec §1.2): the prompt offered anchors 1.0 / 0.85 / 0.6 /
0 as *guidelines* and allowed "any value in [0, 1]", and the served credit
collapsed to the extremes — 129 of 259 prod topic credits were exactly 0 and 114
were >= 0.9, leaving 8 genuinely mid ones. With 1-3 graded nodes per problem the
reachable score set is quantized and a B is arithmetically impossible.

Policy under test:

* the STRUCTURED-OUTPUT schema constrains ``credit`` to the anchor enum
  {0, 0.6, 0.85, 1.0} — not just prose;
* the system prompt carries calibration exemplars (>= 2 per anchor) drawn from
  the real Week-4 transcript patterns;
* a credit the model still returns off-anchor is SNAPPED to the nearest anchor
  (ties resolve DOWN — never invent credit the model did not judge) and logged;
* the enum can never become a new hard-failure mode: if the first provider call
  fails, the retry drops the enum and the code snap alone enforces the anchors.
"""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from apollo.ontology import KGGraph, build_node
from apollo.overseer.coverage_contract import validate_coverage_verdict
from apollo.overseer.transcript_coverage import (
    CREDIT_ANCHORS,
    build_system_prompt,
    build_transcript_grader_schema,
    compute_transcript_coverage,
    compute_transcript_coverage_with_spans,
)

pytestmark = pytest.mark.unit


def _graph() -> KGGraph:
    return KGGraph(
        nodes=[
            build_node(
                node_type="procedure_step",
                node_id="p1",
                attempt_id=1,
                source="reference",
                content={"action": "Integrate", "purpose": ""},
            )
        ]
    )


def _problem() -> SimpleNamespace:
    return SimpleNamespace(problem_text="Evaluate the integral")


def _item(credit: float, **overrides) -> dict:
    item = {
        "node_id": "p1",
        "covered": True,
        "credit": credit,
        "confidence": 0.9,
        "evidence_span": "I integrate now",
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


def _credit_schema(client: MagicMock, call_index: int = 0) -> dict:
    call = client.chat.completions.create.call_args_list[call_index]
    schema = call.kwargs["response_format"]["json_schema"]
    return schema["schema"]["properties"]["verdicts"]["items"]["properties"]["credit"]


# --------------------------------------------------------------------------- #
# The schema constraint
# --------------------------------------------------------------------------- #
def test_anchor_enum_is_the_four_documented_values():
    assert CREDIT_ANCHORS == (0.0, 0.6, 0.85, 1.0)


def test_schema_constrains_credit_to_the_anchor_enum_by_default():
    schema = build_transcript_grader_schema()
    credit = schema["schema"]["properties"]["verdicts"]["items"]["properties"]["credit"]
    assert credit["type"] == "number"
    assert credit["enum"] == list(CREDIT_ANCHORS)


def test_schema_enum_can_be_dropped_for_the_downgrade_retry():
    """The enum-free build is exactly the pre-P1.1 credit property — it is the
    fallback used when a provider rejects a numeric enum, so it must stay a
    plain number and must not disturb any other property."""
    enumless = build_transcript_grader_schema(credit_enum=False)
    props = enumless["schema"]["properties"]["verdicts"]["items"]["properties"]
    assert props["credit"] == {"type": "number"}
    assert props["basis"]["enum"] == ["stated", "used", "implied", "absent"]
    assert enumless["strict"] is True


def test_schema_enum_composes_with_the_hoot_assisted_variant():
    schema = build_transcript_grader_schema(True)
    items = schema["schema"]["properties"]["verdicts"]["items"]
    assert items["properties"]["credit"]["enum"] == list(CREDIT_ANCHORS)
    assert items["required"] == list(items["properties"])


# --------------------------------------------------------------------------- #
# The prompt: hard anchors + calibration exemplars
# --------------------------------------------------------------------------- #
def test_prompt_states_the_anchors_are_the_only_allowed_values():
    prompt = build_system_prompt(_problem())
    assert "exactly one of these four values" in prompt
    assert "0, 0.6, 0.85, or 1.0" in prompt
    # The old "any value in [0, 1] is allowed" licence is gone.
    assert "Any value in [0, 1] is allowed" not in prompt
    assert "As guidelines, not strict rules" not in prompt


def test_prompt_carries_at_least_two_calibration_exemplars_per_anchor():
    prompt = build_system_prompt(_problem())
    assert "CALIBRATION EXAMPLES" in prompt
    # The leading space keeps " 0 —" from matching inside " 1.0 —".
    for anchor in (" 1.0 —", " 0.85 —", " 0.6 —", " 0 —"):
        assert prompt.count(anchor) >= 2, f"anchor {anchor!r} needs >= 2 exemplars"


def test_prompt_exemplars_encode_the_real_week4_patterns():
    """Grounded in the exported prod transcripts, not invented: the 0.85 case is
    the student who names three of a four-item list in their own words
    (attempt 80); the 0.6 case is the directionally-right but thin answer
    (attempt 158, 'the gap between informed and uninformed widens'); the 0 case
    includes bare agreement with Apollo's own statement."""
    prompt = build_system_prompt(_problem())
    assert "three of the four" in prompt
    assert "directionally right but thin" in prompt
    assert "only agrees with Apollo" in prompt


def test_prompt_keeps_the_loosened_calibration_and_anti_gaming_rails():
    prompt = build_system_prompt(_problem())
    assert "Lean toward crediting genuine understanding" in prompt
    assert "confirms, corrects, or builds on" in prompt
    assert "NOT evidence on their own" in prompt
    assert "grader of record" in prompt


# --------------------------------------------------------------------------- #
# Snapping
# --------------------------------------------------------------------------- #
async def _credit_for(raw_credit: float) -> float:
    payload = {"verdicts": [_item(raw_credit)]}
    with patch("apollo.overseer.transcript_coverage.bounded_client", return_value=_client(payload)):
        result = await compute_transcript_coverage(
            [("student", "I integrate now")], _graph(), _problem()
        )
    validate_coverage_verdict(result)
    return result["procedure_scores"]["p1"]


@pytest.mark.asyncio
@pytest.mark.parametrize("anchor", [0.0, 0.6, 0.85, 1.0])
async def test_anchor_values_pass_through_untouched(anchor):
    assert await _credit_for(anchor) == pytest.approx(anchor)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("raw", "snapped"),
    [
        (0.79, 0.85),
        (0.9, 0.85),
        (0.95, 1.0),
        (0.2, 0.0),
        (0.5, 0.6),
        (0.7, 0.6),
        (0.73, 0.85),
    ],
)
async def test_off_anchor_credit_snaps_to_the_nearest_anchor(raw, snapped):
    assert await _credit_for(raw) == pytest.approx(snapped)


@pytest.mark.asyncio
@pytest.mark.parametrize(("raw", "snapped"), [(0.3, 0.0), (0.725, 0.6), (0.925, 0.85)])
async def test_exact_midpoints_snap_down_never_up(raw, snapped):
    """A tie must never manufacture credit the adjudicator did not judge."""
    assert await _credit_for(raw) == pytest.approx(snapped)


@pytest.mark.asyncio
async def test_snapping_is_logged_with_both_values(caplog):
    with caplog.at_level(logging.INFO, logger="apollo.overseer.transcript_coverage"):
        await _credit_for(0.79)
    messages = [record.getMessage() for record in caplog.records]
    assert any(
        "transcript_coverage_credit_snapped" in message
        and "raw=0.79" in message
        and "snapped=0.85" in message
        for message in messages
    )


@pytest.mark.asyncio
async def test_on_anchor_credit_is_not_logged_as_snapped(caplog):
    with caplog.at_level(logging.INFO, logger="apollo.overseer.transcript_coverage"):
        await _credit_for(0.85)
    assert not any(
        "transcript_coverage_credit_snapped" in record.getMessage() for record in caplog.records
    )


@pytest.mark.asyncio
async def test_snapped_credit_drives_per_step_and_the_narrative_span_gate():
    """Snapping happens BEFORE every consumer: a 0.2 verdict becomes a real 0,
    so per_step is missing and the narrative loses the quote it would otherwise
    have used to praise the node."""
    payload = {"verdicts": [_item(0.2)]}
    with patch("apollo.overseer.transcript_coverage.bounded_client", return_value=_client(payload)):
        coverage, spans = await compute_transcript_coverage_with_spans(
            [("student", "I integrate now")], _graph(), _problem()
        )
    assert coverage["procedure_scores"]["p1"] == pytest.approx(0.0)
    assert coverage["per_step"]["p1"] == "missing"
    assert spans == {}


@pytest.mark.asyncio
async def test_credit_just_over_half_snaps_up_to_partial_and_stays_covered():
    payload = {"verdicts": [_item(0.55)]}
    with patch("apollo.overseer.transcript_coverage.bounded_client", return_value=_client(payload)):
        coverage, spans = await compute_transcript_coverage_with_spans(
            [("student", "I integrate now")], _graph(), _problem()
        )
    assert coverage["procedure_scores"]["p1"] == pytest.approx(0.6)
    assert coverage["per_step"]["p1"] == "covered"
    assert spans == {"p1": "I integrate now"}


# --------------------------------------------------------------------------- #
# The enum is never a new failure mode
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_provider_failure_retries_without_the_credit_enum():
    """A provider that rejects a numeric enum must not take grading down: the
    second attempt sends the pre-P1.1 schema and the code snap still enforces
    the anchors."""
    client = _client({"verdicts": [_item(0.79)]})
    good = client.chat.completions.create.return_value
    client.chat.completions.create.side_effect = [
        RuntimeError("400 Invalid schema for response_format"),
        good,
    ]
    with patch("apollo.overseer.transcript_coverage.bounded_client", return_value=client):
        result = await compute_transcript_coverage(
            [("student", "I integrate now")], _graph(), _problem()
        )
    assert client.chat.completions.create.call_count == 2
    assert "enum" in _credit_schema(client, 0)
    assert "enum" not in _credit_schema(client, 1)
    # The snap still delivers an anchored credit from the enum-free call.
    assert result["procedure_scores"]["p1"] == pytest.approx(0.85)


@pytest.mark.asyncio
async def test_downgrade_is_logged(caplog):
    client = _client({"verdicts": [_item(1.0)]})
    good = client.chat.completions.create.return_value
    client.chat.completions.create.side_effect = [RuntimeError("boom"), good]
    with caplog.at_level(logging.WARNING, logger="apollo.overseer.transcript_coverage"):
        with patch("apollo.overseer.transcript_coverage.bounded_client", return_value=client):
            await compute_transcript_coverage(
                [("student", "I integrate now")], _graph(), _problem()
            )
    assert any(
        "transcript_coverage_credit_enum_downgraded" in record.getMessage()
        for record in caplog.records
    )
