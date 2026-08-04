"""INTERACTION5 — Hoot-aside grading-cap adjudicator seam.

Mirrors ``test_grounded_prompts.py``'s byte-identical contract: with no asides
the system prompt, user message, and JSON schema are character-for-character the
pre-feature build, so an off flag cannot move a single grade. With asides the
grader gains an untrusted HOOT LOOKUP ANSWERS block, a matching instruction, a
``hoot_assisted`` schema property, and carries per-node assist flags back on the
coverage dict for the downstream cap pass.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from apollo.errors import CoverageGradingError
from apollo.ontology import KGGraph, build_node
from apollo.overseer.coverage_contract import validate_coverage_verdict
from apollo.overseer.transcript_coverage import (
    build_system_prompt,
    build_transcript_grader_schema,
    build_user_message,
    compute_transcript_coverage_with_spans,
)

pytestmark = pytest.mark.unit


class _Problem:
    problem_text = "A pipe narrows from 10 cm to 5 cm. Explain what happens to the pressure."


_ITEMS = [{"id": "def_head", "type": "Definition", "display_name": "Head", "content": {}}]
_TRANSCRIPT = (
    ("apollo", "Where do we start?"),
    ("student", "Total head stays constant along the streamline."),
)
_EVIDENCE = "[Lecture 4, p. 12] — head form p/(rho g) + v^2/(2g) + z = H."
_ASIDES = ("Bernoulli's principle: faster flow means lower static pressure [Lecture 4].",)


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


def _verdict_item(**overrides) -> dict:
    item = {
        "node_id": "p1",
        "covered": True,
        "credit": 0.9,
        "confidence": 0.9,
        "evidence_span": "Total head stays constant along the streamline.",
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


def _schema_props_from_call(client: MagicMock) -> dict:
    call = client.chat.completions.create.call_args
    schema = call.kwargs["response_format"]["json_schema"]
    return schema["schema"]["properties"]["verdicts"]["items"]["properties"]


# --------------------------------------------------------------------------- #
# The no-op contract — byte-identical when asides are absent
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("absent", [(), []])
def test_prompts_and_schema_byte_identical_without_asides(absent):
    baseline_system = build_system_prompt(_Problem())
    baseline_user = build_user_message(_Problem(), _ITEMS, _TRANSCRIPT)
    baseline_schema = build_transcript_grader_schema()

    assert build_system_prompt(_Problem(), hoot_asides=absent) == baseline_system
    assert build_user_message(_Problem(), _ITEMS, _TRANSCRIPT, hoot_asides=absent) == baseline_user
    # Default arg and explicit-False both reproduce the pre-feature schema.
    assert build_transcript_grader_schema(False) == baseline_schema
    assert (
        "hoot_assisted"
        not in baseline_schema["schema"]["properties"]["verdicts"]["items"]["properties"]
    )


def test_asides_do_not_disturb_the_course_evidence_frame():
    """With evidence present but no asides, the message equals the evidence-only
    build — the aside plumbing adds nothing."""
    evidence_only = build_user_message(_Problem(), _ITEMS, _TRANSCRIPT, course_evidence=_EVIDENCE)
    assert (
        build_user_message(
            _Problem(), _ITEMS, _TRANSCRIPT, course_evidence=_EVIDENCE, hoot_asides=()
        )
        == evidence_only
    )


# --------------------------------------------------------------------------- #
# The grounded build — prompt + user message + schema when asides present
# --------------------------------------------------------------------------- #
def test_system_prompt_gains_hoot_aside_instruction_when_present():
    grounded = build_system_prompt(_Problem(), hoot_asides=_ASIDES)
    baseline = build_system_prompt(_Problem())

    assert grounded.startswith(baseline)
    assert "HOOT LOOKUP ANSWERS" in grounded
    assert "NOT the student's teaching" in grounded
    assert "NEVER itself evidence that the student" in grounded
    assert "evidence_span must always quote the STUDENT" in grounded
    assert "set hoot_assisted to true if and only if" in grounded
    # Flat cap / no earn-back is spelled out.
    assert "even if the student later teaches it well" in grounded


def test_system_prompt_appends_both_evidence_and_aside_frames_in_order():
    both = build_system_prompt(_Problem(), course_evidence=_EVIDENCE, hoot_asides=_ASIDES)
    assert "COURSE EVIDENCE" in both
    assert both.index("COURSE EVIDENCE") < both.index("HOOT LOOKUP ANSWERS")


def test_user_message_gains_hoot_lookup_block_before_dialogue():
    message = build_user_message(_Problem(), _ITEMS, _TRANSCRIPT, hoot_asides=_ASIDES)

    assert "HOOT LOOKUP ANSWERS" in message
    assert _ASIDES[0] in message
    assert "[Hoot lookup answer 1]" in message
    assert message.index("RUBRIC ITEMS") < message.index("HOOT LOOKUP ANSWERS")
    assert message.index("HOOT LOOKUP ANSWERS") < message.index("DIALOGUE (untrusted")
    assert "NOT the student's teaching" in message
    # Transcript still lands last.
    assert message.endswith(f"student: {_TRANSCRIPT[-1][1]}")


def test_user_message_numbers_multiple_asides_and_orders_evidence_first():
    asides = ("first lookup answer", "second lookup answer")
    message = build_user_message(
        _Problem(), _ITEMS, _TRANSCRIPT, course_evidence=_EVIDENCE, hoot_asides=asides
    )
    assert "[Hoot lookup answer 1]" in message
    assert "[Hoot lookup answer 2]" in message
    assert message.index("COURSE EVIDENCE") < message.index("HOOT LOOKUP ANSWERS")
    assert message.index("HOOT LOOKUP ANSWERS") < message.index("DIALOGUE (untrusted")


def test_schema_gains_hoot_assisted_only_when_included():
    with_flag = build_transcript_grader_schema(True)
    props = with_flag["schema"]["properties"]["verdicts"]["items"]
    assert props["properties"]["hoot_assisted"] == {"type": "boolean"}
    # Strict schema keeps `required` == all properties.
    assert "hoot_assisted" in props["required"]
    assert props["required"] == list(props["properties"])


# --------------------------------------------------------------------------- #
# End-to-end: coverage carries the flag; schema variant is actually used
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_asides_carry_hoot_assisted_true_through_coverage():
    payload = {"verdicts": [_verdict_item(hoot_assisted=True)]}
    client = _client(payload)
    with patch("apollo.overseer.transcript_coverage.bounded_client", return_value=client):
        coverage, spans = await compute_transcript_coverage_with_spans(
            list(_TRANSCRIPT), _graph(), _Problem(), hoot_asides=_ASIDES
        )
    validate_coverage_verdict(coverage)
    assert coverage["hoot_assisted"] == {"p1": True}
    # The include-variant schema (with the boolean property) was sent.
    assert "hoot_assisted" in _schema_props_from_call(client)
    client.chat.completions.create.assert_called_once()


@pytest.mark.asyncio
async def test_hoot_assisted_defaults_false_when_absent_in_response():
    """Under the asides (include) path, a verdict that omits the field defaults
    to False rather than raising."""
    payload = {"verdicts": [_verdict_item()]}  # no hoot_assisted key
    with patch("apollo.overseer.transcript_coverage.bounded_client", return_value=_client(payload)):
        coverage, _ = await compute_transcript_coverage_with_spans(
            list(_TRANSCRIPT), _graph(), _Problem(), hoot_asides=_ASIDES
        )
    assert coverage["hoot_assisted"] == {"p1": False}


@pytest.mark.asyncio
async def test_non_bool_hoot_assisted_raises_named_error():
    payload = {"verdicts": [_verdict_item(hoot_assisted="yes")]}
    with patch("apollo.overseer.transcript_coverage.bounded_client", return_value=_client(payload)):
        with pytest.raises(CoverageGradingError):
            await compute_transcript_coverage_with_spans(
                list(_TRANSCRIPT), _graph(), _Problem(), hoot_asides=_ASIDES
            )


@pytest.mark.asyncio
async def test_no_asides_coverage_has_no_hoot_assisted_key_and_uses_base_schema():
    payload = {"verdicts": [_verdict_item(credit=0.7)]}
    client = _client(payload)
    with patch("apollo.overseer.transcript_coverage.bounded_client", return_value=client):
        coverage, spans = await compute_transcript_coverage_with_spans(
            list(_TRANSCRIPT), _graph(), _Problem()
        )
    validate_coverage_verdict(coverage)
    assert "hoot_assisted" not in coverage
    assert "hoot_assisted" not in _schema_props_from_call(client)
    assert spans == {"p1": "Total head stays constant along the streamline."}
