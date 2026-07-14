import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from apollo.errors import CoverageGradingError
from apollo.ontology import KGGraph, build_node
from apollo.overseer.coverage_contract import validate_coverage_verdict
from apollo.overseer.transcript_coverage import (
    NodeVerdict,
    build_system_prompt,
    build_transcript_grader_schema,
    compute_transcript_coverage,
    compute_transcript_coverage_with_spans,
    narrative_evidence_spans,
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


def test_schema_fresh_strict_and_basis_enum():
    one = build_transcript_grader_schema()
    two = build_transcript_grader_schema()
    assert one is not two
    assert one["strict"] is True
    assert one["schema"]["additionalProperties"] is False
    verdict_schema = one["schema"]["properties"]["verdicts"]["items"]
    assert verdict_schema["properties"]["basis"] == {
        "type": "string",
        "enum": ["stated", "used", "implied", "absent"],
    }
    assert "basis" in verdict_schema["required"]


def test_prompt_uses_knowledge_demonstration_basis_and_preserves_evidence_rails():
    prompt = build_system_prompt(_problem())
    assert "explicitly stated, correctly used in their reasoning, or clearly implied" in prompt
    assert 'Set basis to "stated"' in prompt
    assert "Apollo's restatements, completions, and corrections are NOT evidence" in prompt


def test_prompt_states_grader_of_record_and_guidance_only_no_verbatim_requirement():
    prompt = build_system_prompt(_problem())
    assert "grader of record" in prompt
    assert "guidelines, not" in prompt
    assert "must quote a verbatim evidence span" not in prompt


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
                "basis": "stated",
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
async def test_span_mismatch_does_not_zero_credit():
    """Span validation is diagnostic-only now: a positive credit whose span
    isn't a verbatim student quote still flows through to procedure_scores
    untouched."""
    payload = {
        "verdicts": [
            {
                "node_id": "p1",
                "covered": True,
                "credit": 0.9,
                "confidence": 0.8,
                "evidence_span": "Apollo only",
                "prompted": True,
                "corrected_later": False,
                "basis": "stated",
            }
        ]
    }
    with patch("apollo.overseer.transcript_coverage.OpenAI", return_value=_client(payload)):
        result = await compute_transcript_coverage(
            [("apollo", "Apollo only"), ("student", "no")], _graph(), _problem()
        )
    assert result["procedure_scores"]["p1"] == pytest.approx(0.9)
    assert result["per_step"]["p1"] == "covered"


@pytest.mark.asyncio
async def test_empty_output_raises_named_error():
    with patch("apollo.overseer.transcript_coverage.OpenAI", return_value=_client({})):
        with pytest.raises(CoverageGradingError):
            await compute_transcript_coverage([], _graph(), _problem())


@pytest.mark.asyncio
async def test_continuous_credit_passes_through_untouched():
    """The LLM's continuous credit in [0, 1] flows through to procedure_scores
    unmodified — no quantization to {0, 0.4, 0.7, 1.0}."""
    payload = {
        "verdicts": [
            {
                "node_id": "p1",
                "covered": True,
                "credit": 0.79,
                "confidence": 0.9,
                "evidence_span": "I integrate both sides",
                "prompted": False,
                "corrected_later": False,
                "basis": "implied",
            }
        ]
    }
    with patch("apollo.overseer.transcript_coverage.OpenAI", return_value=_client(payload)):
        result = await compute_transcript_coverage(
            [("student", "I integrate both sides")], _graph(), _problem()
        )
    assert result["procedure_scores"]["p1"] == pytest.approx(0.79)
    assert result["per_step"]["p1"] == "covered"


@pytest.mark.asyncio
async def test_basis_no_longer_overrides_credit():
    """basis == "absent" no longer force-zeroes a positive credit — basis is
    provenance/logging only, never a code rule."""
    payload = {
        "verdicts": [
            {
                "node_id": "p1",
                "covered": True,
                "credit": 0.3,
                "confidence": 0.9,
                "evidence_span": "I integrate",
                "prompted": False,
                "corrected_later": False,
                "basis": "absent",
            }
        ]
    }
    with patch("apollo.overseer.transcript_coverage.OpenAI", return_value=_client(payload)):
        result = await compute_transcript_coverage(
            [("student", "I integrate")], _graph(), _problem()
        )
    assert result["procedure_scores"]["p1"] == pytest.approx(0.3)


@pytest.mark.asyncio
async def test_sub_half_credit_is_missing_in_per_step_but_raw_in_procedure_scores():
    payload = {
        "verdicts": [
            {
                "node_id": "p1",
                "covered": True,
                "credit": 0.3,
                "confidence": 0.9,
                "evidence_span": "I integrate",
                "prompted": False,
                "corrected_later": False,
                "basis": "implied",
            }
        ]
    }
    with patch("apollo.overseer.transcript_coverage.OpenAI", return_value=_client(payload)):
        result = await compute_transcript_coverage(
            [("student", "I integrate")], _graph(), _problem()
        )
    assert result["per_step"]["p1"] == "missing"
    assert result["procedure_scores"]["p1"] == pytest.approx(0.3)


@pytest.mark.asyncio
async def test_missing_basis_raises_named_error():
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
    with patch("apollo.overseer.transcript_coverage.OpenAI", return_value=_client(payload)):
        with pytest.raises(CoverageGradingError):
            await compute_transcript_coverage([("student", "I integrate")], _graph(), _problem())


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
    raw = '{"verdicts": [{"node_id": "p1", "covered": true, "credit": NaN, "confidence": 0.9, "evidence_span": "I integrate", "prompted": false, "corrected_later": false, "basis": "stated"}]}'
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
                "basis": "stated",
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


# --------------------------------------------------------------------------- #
# Per-session narrative evidence (diagnostic-narrative grounding)
# --------------------------------------------------------------------------- #
def _verdict(
    node_id: str,
    *,
    credit: float = 0.7,
    evidence_span: str | None = "I integrate now",
) -> NodeVerdict:
    return NodeVerdict(
        node_id=node_id,
        covered=credit >= 0.5,
        credit=credit,
        confidence=0.9,
        evidence_span=evidence_span,
        prompted=False,
        corrected_later=False,
        basis="stated",
    )


def test_narrative_evidence_spans_keeps_only_verbatim_student_quotes():
    """The narrative lane must only ever quote words the student actually
    typed THIS attempt — an adjudicator span that fails the verbatim check
    (hallucinated, Apollo-sourced, or stitched) is dropped, not served."""
    transcript = [("student", "I integrate now"), ("apollo", "Alvin Toffler wrote it in 1970")]
    verdicts = [
        _verdict("p1", evidence_span="I integrate now"),
        _verdict("p2", evidence_span="Alvin Toffler wrote it in 1970"),
        _verdict("p3", evidence_span=None),
    ]
    spans = narrative_evidence_spans(verdicts, transcript)
    assert spans == {"p1": "I integrate now"}


def test_narrative_evidence_spans_drops_zero_credit_verdicts():
    transcript = [("student", "I integrate now")]
    verdicts = [_verdict("p1", credit=0.0, evidence_span="I integrate now")]
    assert narrative_evidence_spans(verdicts, transcript) == {}


@pytest.mark.asyncio
async def test_with_spans_returns_contract_coverage_plus_gated_spans():
    """One adjudication call yields BOTH the frozen coverage verdict and the
    per-attempt narrative spans — no second LLM call, no extra coverage key."""
    payload = {
        "verdicts": [
            {
                "node_id": "p1",
                "covered": True,
                "credit": 0.7,
                "confidence": 0.9,
                "evidence_span": "I integrate now",
                "prompted": False,
                "corrected_later": False,
                "basis": "stated",
            }
        ]
    }
    client = _client(payload)
    with patch("apollo.overseer.transcript_coverage.OpenAI", return_value=client):
        coverage, spans = await compute_transcript_coverage_with_spans(
            [("student", "I integrate now")], _graph(), _problem()
        )
    validate_coverage_verdict(coverage)
    assert coverage["procedure_scores"]["p1"] == pytest.approx(0.7)
    assert spans == {"p1": "I integrate now"}
    client.chat.completions.create.assert_called_once()
