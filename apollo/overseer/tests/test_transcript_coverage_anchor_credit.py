"""Anchored partial credit for the adjudicator (bimodal-fix P1.1).

Root cause (2026-08-07 spec §1.2): the prompt offered anchors 1.0 / 0.85 / 0.6 /
0 as *guidelines* and allowed "any value in [0, 1]", and the served credit
collapsed to the extremes — 129 of 259 prod topic credits were exactly 0 and 114
were >= 0.9, leaving 8 genuinely mid ones. With 1-3 graded nodes per problem the
reachable score set is quantized and a B is arithmetically impossible.

Policy under test:

* the STRUCTURED-OUTPUT schema constrains ``credit`` to the anchor enum
  {0, 0.3, 0.6, 0.85, 1.0} — not just prose;
* the system prompt carries calibration exemplars (>= 2 per anchor) drawn from
  the real Week-4 transcript patterns;
* a credit the model still returns off-anchor is SNAPPED to the nearest anchor
  (ties resolve DOWN — never invent credit the model did not judge) and logged;
* the enum can never become a new hard-failure mode: an error that REJECTS the
  schema drops the enum (and latches it off for the process) while the code snap
  alone enforces the anchors — but a transient 429/timeout is retried
  like-for-like, so a rate limit can neither degrade one grade to the pre-P1.1
  schema nor forge the "enum unsupported" signal the calibration arm reads.

The 0.3 anchor (2026-08-24) closes the "phantom 0.6" defect: 0.6 was the lowest
non-zero anchor, so the adjudicator's hedge on a topic it simultaneously reported
as missing/``basis="absent"`` with no evidence span still landed on the lowest
anchor that means "landed". 0.3 gives that hedge a cheaper landing spot below
``topic_narrative.PRAISE_FLOOR`` (0.6, deliberately unmoved) and below the
``per_step`` covered threshold (0.5), so a hedged topic reads as uncredited
everywhere while its credit stops inflating the topic score.
"""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from apollo.errors import CoverageGradingError
from apollo.ontology import KGGraph, build_node
from apollo.overseer.coverage_contract import validate_coverage_verdict
from apollo.overseer.topic_narrative import PRAISE_FLOOR
from apollo.overseer.transcript_coverage import (
    CREDIT_ANCHORS,
    build_system_prompt,
    build_transcript_grader_schema,
    compute_transcript_coverage,
    compute_transcript_coverage_with_spans,
    credit_enum_supported,
    reset_credit_enum_support,
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
def test_anchor_enum_is_the_five_documented_values():
    assert CREDIT_ANCHORS == (0.0, 0.3, 0.6, 0.85, 1.0)


def test_anchor_enum_is_ascending_which_is_what_makes_ties_snap_down():
    """`_snap_credit` breaks a tie by taking the FIRST minimal distance, so the
    ascending order is load-bearing, not cosmetic — inserting 0.3 out of order
    would silently turn every midpoint tie below it into a snap UP."""
    assert list(CREDIT_ANCHORS) == sorted(CREDIT_ANCHORS)
    assert len(set(CREDIT_ANCHORS)) == len(CREDIT_ANCHORS)


def test_schema_constrains_credit_to_the_anchor_enum_by_default():
    schema = build_transcript_grader_schema()
    credit = schema["schema"]["properties"]["verdicts"]["items"]["properties"]["credit"]
    assert credit["type"] == "number"
    assert credit["enum"] == list(CREDIT_ANCHORS)
    # The 0.3 anchor is only real if the model is actually ALLOWED to return it:
    # the snap alone cannot produce a value the structured output forbids.
    assert 0.3 in credit["enum"]


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
    assert 0.3 in items["properties"]["credit"]["enum"]
    assert items["required"] == list(items["properties"])


# --------------------------------------------------------------------------- #
# The prompt: hard anchors + calibration exemplars
# --------------------------------------------------------------------------- #
def test_prompt_states_the_anchors_are_the_only_allowed_values():
    prompt = build_system_prompt(_problem())
    assert "exactly one of these five values" in prompt
    assert "0, 0.3, 0.6, 0.85, or 1.0" in prompt
    # The old "any value in [0, 1] is allowed" licence is gone.
    assert "Any value in [0, 1] is allowed" not in prompt
    assert "As guidelines, not strict rules" not in prompt


def test_prompt_gives_the_new_03_anchor_its_own_semantics_and_invites_it():
    """A schema enum value the prose never explains is a value the model will not
    reach for — the whole point of 0.3 is that the hedge lands THERE instead of
    on 0.6, so the prompt has to say what it means AND list it among the values
    worth reaching for. Exemplars are deliberately NOT added (that is a separate
    calibration arm); this one line is 0.3's entire definition."""
    prompt = build_system_prompt(_problem())
    assert "0.3 means a bare or tangential mention" in prompt
    assert "reach for 0.85, 0.6 and 0.3" in prompt
    # 0.6 keeps its own distinct, unweakened meaning — 0.3 sits BELOW it, it does
    # not redefine it.
    assert "0.6 means on track but thin, ambiguous, or unconnected" in prompt


def test_prompt_carries_at_least_two_calibration_exemplars_per_exemplified_anchor():
    prompt = build_system_prompt(_problem())
    assert "CALIBRATION EXAMPLES" in prompt
    # The leading space keeps " 0 —" from matching inside " 1.0 —".
    for anchor in (" 1.0 —", " 0.85 —", " 0.6 —", " 0 —"):
        assert prompt.count(anchor) >= 2, f"anchor {anchor!r} needs >= 2 exemplars"


def test_the_new_anchor_deliberately_has_no_calibration_exemplar_yet():
    """Pinned so it stays a DECISION rather than an oversight. The exemplar block
    is the calibration instrument: the 5-transcript x 2-arm x 4-sample experiment
    that cleared 0.3 held it byte-identical, so adding a 0.3 exemplar here would
    silently invalidate that measurement. Whoever writes one must run a new arm —
    and will land on this test first."""
    prompt = build_system_prompt(_problem())
    assert " 0.3 —" not in prompt


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
@pytest.mark.parametrize("anchor", [0.0, 0.3, 0.6, 0.85, 1.0])
async def test_anchor_values_pass_through_untouched(anchor):
    assert await _credit_for(anchor) == pytest.approx(anchor)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("raw", "snapped"),
    [
        (0.79, 0.85),
        (0.9, 0.85),
        (0.95, 1.0),
        (0.1, 0.0),
        (0.2, 0.3),
        (0.4, 0.3),
        (0.5, 0.6),
        (0.7, 0.6),
        (0.73, 0.85),
    ],
)
async def test_off_anchor_credit_snaps_to_the_nearest_anchor(raw, snapped):
    """0.2 and 0.4 used to collapse to 0.0 and 0.6 respectively — the whole
    [0.15, 0.45] band now has its own anchor, which is exactly the band the
    phantom-0.6 hedge was being rounded out of."""
    assert await _credit_for(raw) == pytest.approx(snapped)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("raw", "snapped"), [(0.15, 0.0), (0.45, 0.3), (0.725, 0.6), (0.925, 0.85)]
)
async def test_exact_midpoints_snap_down_never_up(raw, snapped):
    """A tie must never manufacture credit the adjudicator did not judge. The new
    anchor adds two midpoints (0.15 between 0 and 0.3, 0.45 between 0.3 and 0.6)
    and both resolve downward like every pre-existing one."""
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
    """Snapping happens BEFORE every consumer: a 0.1 verdict becomes a real 0,
    so per_step is missing and the narrative loses the quote it would otherwise
    have used to praise the node. (Pre-0.3-anchor this case was 0.2, which now
    has a nearer home — 0.1 is the value that still rounds all the way down.)"""
    payload = {"verdicts": [_item(0.1)]}
    with patch("apollo.overseer.transcript_coverage.bounded_client", return_value=_client(payload)):
        coverage, spans = await compute_transcript_coverage_with_spans(
            [("student", "I integrate now")], _graph(), _problem()
        )
    assert coverage["procedure_scores"]["p1"] == pytest.approx(0.0)
    assert coverage["per_step"]["p1"] == "missing"
    assert spans == {}


@pytest.mark.asyncio
async def test_the_new_anchor_is_sub_threshold_for_every_binary_consumer():
    """0.3 is the first non-zero anchor that is NOT "landed": it sits below the
    0.5 `per_step` covered threshold and below `topic_narrative.PRAISE_FLOOR`
    (0.6), so a hedged topic reads as uncredited to the rubric axes and to the
    narrative gate while still carrying its fractional credit into the topic
    lane. It keeps its evidence quote, because the span gate keys on credit > 0
    and 0.3 IS credit the adjudicator judged."""
    payload = {"verdicts": [_item(0.3)]}
    with patch("apollo.overseer.transcript_coverage.bounded_client", return_value=_client(payload)):
        coverage, spans = await compute_transcript_coverage_with_spans(
            [("student", "I integrate now")], _graph(), _problem()
        )
    assert coverage["procedure_scores"]["p1"] == pytest.approx(0.3)
    assert coverage["per_step"]["p1"] == "missing"
    assert 0.3 < PRAISE_FLOOR
    assert spans == {"p1": "I integrate now"}


@pytest.mark.asyncio
async def test_the_phantom_hedge_shape_no_longer_reaches_the_landed_anchor():
    """The defect, end to end: the adjudicator reports the topic as NOT covered,
    `basis="absent"`, no evidence span — and still credits it. Pre-fix the only
    non-zero place that hedge could land was 0.6, the lowest anchor that means
    "landed", so a contentless topic scored as a thin-but-real one. With 0.3
    available the same shape lands there instead: still uncredited to every
    binary consumer, still unquotable, and worth half as much to the score."""
    payload = {"verdicts": [_item(0.3, covered=False, basis="absent", evidence_span=None)]}
    with patch("apollo.overseer.transcript_coverage.bounded_client", return_value=_client(payload)):
        coverage, spans = await compute_transcript_coverage_with_spans(
            [("student", "I integrate now")], _graph(), _problem()
        )
    assert coverage["procedure_scores"]["p1"] == pytest.approx(0.3)
    assert coverage["per_step"]["p1"] == "missing"
    assert coverage["basis"]["p1"] == "absent"
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
# The enum is never a new failure mode — and never downgraded on a transient one
# --------------------------------------------------------------------------- #
_SCHEMA_REJECTION = RuntimeError(
    "Error code: 400 - Invalid schema for response_format 'apollo_transcript_coverage': "
    "'enum' is not permitted for 'number'"
)
_TRANSIENT = RuntimeError("Error code: 429 - Rate limit reached for gpt-5.1")


async def _run(client: MagicMock):
    with patch("apollo.overseer.transcript_coverage.bounded_client", return_value=client):
        return await compute_transcript_coverage(
            [("student", "I integrate now")], _graph(), _problem()
        )


@pytest.mark.asyncio
async def test_schema_rejection_retries_without_the_credit_enum():
    """A provider that rejects a numeric enum must not take grading down: the
    next attempt sends the pre-P1.1 schema and the code snap still enforces
    the anchors."""
    client = _client({"verdicts": [_item(0.79)]})
    good = client.chat.completions.create.return_value
    client.chat.completions.create.side_effect = [_SCHEMA_REJECTION, good]
    result = await _run(client)
    assert client.chat.completions.create.call_count == 2
    assert "enum" in _credit_schema(client, 0)
    assert "enum" not in _credit_schema(client, 1)
    # The snap still delivers an anchored credit from the enum-free call.
    assert result["procedure_scores"]["p1"] == pytest.approx(0.85)


@pytest.mark.asyncio
async def test_schema_rejection_downgrade_is_logged(caplog):
    client = _client({"verdicts": [_item(1.0)]})
    good = client.chat.completions.create.return_value
    client.chat.completions.create.side_effect = [_SCHEMA_REJECTION, good]
    with caplog.at_level(logging.WARNING, logger="apollo.overseer.transcript_coverage"):
        await _run(client)
    assert any(
        "transcript_coverage_credit_enum_downgraded" in record.getMessage()
        for record in caplog.records
    )


@pytest.mark.asyncio
async def test_transient_error_retries_with_the_enum_intact(caplog):
    """A 429/timeout/5xx says nothing about the schema. Downgrading on it would
    (a) silently grade that attempt under the unconstrained pre-P1.1 schema, so
    the model re-emits the 0.9/0.95/0 distribution P1.1 exists to break, and
    (b) fire the log line the calibration arm reads as proof the enum is
    unsupported — dropping the enum repo-wide on one rate limit."""
    client = _client({"verdicts": [_item(0.79)]})
    good = client.chat.completions.create.return_value
    client.chat.completions.create.side_effect = [_TRANSIENT, good]
    with caplog.at_level(logging.WARNING, logger="apollo.overseer.transcript_coverage"):
        result = await _run(client)
    assert client.chat.completions.create.call_count == 2
    assert "enum" in _credit_schema(client, 0)
    assert "enum" in _credit_schema(client, 1)
    assert not any(
        "transcript_coverage_credit_enum_downgraded" in record.getMessage()
        for record in caplog.records
    )
    assert result["procedure_scores"]["p1"] == pytest.approx(0.85)


@pytest.mark.asyncio
async def test_two_transient_errors_still_fail_closed():
    """The like-for-like retry budget is unchanged by P1.1: two transient
    failures raise the structured grading error, never a fabricated grade."""
    client = _client({"verdicts": [_item(1.0)]})
    client.chat.completions.create.side_effect = [_TRANSIENT, _TRANSIENT]
    with pytest.raises(CoverageGradingError):
        await _run(client)
    assert client.chat.completions.create.call_count == 2


@pytest.mark.asyncio
async def test_schema_downgrade_does_not_consume_the_transient_retry():
    """Dropping the enum is a schema change, not a fault, so it earns its own
    attempt: a rejection followed by a 429 still gets one like-for-like retry."""
    client = _client({"verdicts": [_item(0.95)]})
    good = client.chat.completions.create.return_value
    client.chat.completions.create.side_effect = [_SCHEMA_REJECTION, _TRANSIENT, good]
    result = await _run(client)
    assert client.chat.completions.create.call_count == 3
    assert "enum" in _credit_schema(client, 0)
    assert "enum" not in _credit_schema(client, 1)
    assert "enum" not in _credit_schema(client, 2)
    assert result["procedure_scores"]["p1"] == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_schema_rejection_latches_the_enum_off_for_the_process():
    """A genuine rejection is deterministic — re-sending the enum on every later
    Done would cost a wasted full-prompt adjudication call per grade forever."""
    first = _client({"verdicts": [_item(1.0)]})
    first.chat.completions.create.side_effect = [
        _SCHEMA_REJECTION,
        first.chat.completions.create.return_value,
    ]
    await _run(first)
    assert credit_enum_supported() is False

    later = _client({"verdicts": [_item(1.0)]})
    await _run(later)
    later.chat.completions.create.assert_called_once()
    assert "enum" not in _credit_schema(later, 0)


@pytest.mark.asyncio
async def test_a_transient_failure_never_latches_the_enum_off():
    client = _client({"verdicts": [_item(1.0)]})
    good = client.chat.completions.create.return_value
    client.chat.completions.create.side_effect = [_TRANSIENT, good]
    await _run(client)
    assert credit_enum_supported() is True


def test_the_latch_starts_armed_and_can_be_re_armed():
    """The conftest fixture re-arms it around every test; a deploy re-arms it in
    production, which is exactly when a provider-side fix would land."""
    assert credit_enum_supported() is True
    reset_credit_enum_support()
    assert credit_enum_supported() is True
