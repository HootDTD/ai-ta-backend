import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from apollo.errors import CoverageGradingError
from apollo.ontology import KGGraph, build_node
from apollo.overseer.coverage_contract import validate_coverage_verdict
from apollo.overseer.transcript_coverage import (
    _quantize_credit,
    build_transcript_grader_schema,
    compute_transcript_coverage,
    validate_span,
)


def _graph():
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


def _problem():
    return SimpleNamespace(problem_text="Evaluate the integral")


def _client(payload):
    client = MagicMock()
    client.chat.completions.create.return_value = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(payload)))]
    )
    return client


def test_schema_fresh_strict_and_quantization_boundary():
    one = build_transcript_grader_schema()
    two = build_transcript_grader_schema()
    assert one is not two
    assert one["strict"] is True
    assert one["schema"]["additionalProperties"] is False
    assert _quantize_credit(0.55) == 0.4
    assert _quantize_credit(0.9) == 1.0


def test_span_validation_is_student_only_and_normalizes_whitespace():
    assert validate_span("a  b\nc", ["a b c"])
    assert not validate_span("Apollo quote", ["student quote"])
    assert not validate_span(None, ["student quote"])


@pytest.mark.asyncio
async def test_full_credit_maps_to_contract_and_calls_once():
    payload = {
        "verdicts": [
            {
                "node_id": "p1",
                "covered": True,
                "credit": 1.0,
                "confidence": 0.9,
                "evidence_span": "I integrate",
                "prompted": False,
                "corrected_later": False,
            }
        ]
    }
    client = _client(payload)
    with patch("apollo.overseer.transcript_coverage.OpenAI", return_value=client):
        result = await compute_transcript_coverage(
            [("student", "I integrate now")], _graph(), _problem()
        )
    validate_coverage_verdict(result)
    assert result["per_step"]["p1"] == "covered"
    assert result["procedure_scores"]["p1"] == 1.0
    assert not any(result["negotiation_counts"].values())
    client.chat.completions.create.assert_called_once()


@pytest.mark.asyncio
async def test_unverified_span_downgrades_partial_to_zero():
    payload = {
        "verdicts": [
            {
                "node_id": "p1",
                "covered": False,
                "credit": 0.7,
                "confidence": 0.8,
                "evidence_span": "Apollo only",
                "prompted": True,
                "corrected_later": False,
            }
        ]
    }
    with patch("apollo.overseer.transcript_coverage.OpenAI", return_value=_client(payload)):
        result = await compute_transcript_coverage(
            [("apollo", "Apollo only"), ("student", "no")], _graph(), _problem()
        )
    assert result["procedure_scores"]["p1"] == 0.0
    assert result["per_step"]["p1"] == "missing"


@pytest.mark.asyncio
async def test_empty_output_raises_named_error():
    with patch("apollo.overseer.transcript_coverage.OpenAI", return_value=_client({})):
        with pytest.raises(CoverageGradingError):
            await compute_transcript_coverage([], _graph(), _problem())


@pytest.mark.asyncio
async def test_verdicts_not_a_list_raises_named_error():
    with patch(
        "apollo.overseer.transcript_coverage.OpenAI", return_value=_client({"verdicts": {}})
    ):
        with pytest.raises(CoverageGradingError):
            await compute_transcript_coverage([], _graph(), _problem())


def _raw_client(raw_content):
    client = MagicMock()
    client.chat.completions.create.return_value = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=raw_content))]
    )
    return client


@pytest.mark.asyncio
async def test_nan_credit_raises_named_error_via_finite01_guard():
    raw = '{"verdicts": [{"node_id": "p1", "covered": true, "credit": NaN, "confidence": 0.9, "evidence_span": "I integrate", "prompted": false, "corrected_later": false}]}'
    with patch("apollo.overseer.transcript_coverage.OpenAI", return_value=_raw_client(raw)):
        with pytest.raises(CoverageGradingError):
            await compute_transcript_coverage(
                [("student", "I integrate now")], _graph(), _problem()
            )


@pytest.mark.asyncio
async def test_retry_path_succeeds_after_first_call_raises():
    payload = {
        "verdicts": [
            {
                "node_id": "p1",
                "covered": True,
                "credit": 1.0,
                "confidence": 0.9,
                "evidence_span": "I integrate",
                "prompted": False,
                "corrected_later": False,
            }
        ]
    }
    client = _client(payload)
    client.chat.completions.create.side_effect = [
        RuntimeError("boom"),
        client.chat.completions.create.return_value,
    ]
    with patch("apollo.overseer.transcript_coverage.OpenAI", return_value=client):
        result = await compute_transcript_coverage(
            [("student", "I integrate now")], _graph(), _problem()
        )
    assert result["per_step"]["p1"] == "covered"
    assert client.chat.completions.create.call_count == 2


@pytest.mark.asyncio
async def test_retry_path_exhausted_raises_named_error_after_two_attempts():
    client = MagicMock()
    client.chat.completions.create.side_effect = RuntimeError("boom")
    with patch("apollo.overseer.transcript_coverage.OpenAI", return_value=client):
        with pytest.raises(CoverageGradingError):
            await compute_transcript_coverage(
                [("student", "I integrate now")], _graph(), _problem()
            )
    assert client.chat.completions.create.call_count == 2


def test_validate_span_true_within_one_student_message_with_whitespace_differences():
    assert validate_span("I   integrate\nnow", ["I integrate now"])


def test_validate_span_false_when_stitched_across_student_message_boundary():
    student_messages = ["I integrate the function", "now I evaluate the bounds"]
    stitched_span = "the function now I evaluate"
    assert not validate_span(stitched_span, student_messages)
