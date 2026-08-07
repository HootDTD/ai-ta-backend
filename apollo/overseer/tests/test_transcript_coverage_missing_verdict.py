"""Abstain-not-zero for omitted adjudicator verdicts (bimodal-fix P0.5).

Defect I5 (2026-08-07 spec): a graded node the adjudicator omitted from
``verdicts[]`` (or misspelled the node_id of) silently scored 0.0 with no log
and no abstention. Policy under test:

* a missing graded node triggers exactly ONE semantic re-adjudication;
* first-call verdicts always win for nodes both calls returned;
* a node still missing after the retry is OMITTED from every coverage map
  (excluded from the topic denominator downstream), never scored 0;
* NO verdict for ANY graded node is an adjudication failure →
  ``CoverageGradingError`` (503 try-again), never a fabricated F(0);
* a retry call that dies on provider errors degrades to exclusion — the
  first call's partial verdicts are real and must not be discarded.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from apollo.errors import CoverageGradingError
from apollo.ontology import KGGraph, build_node
from apollo.overseer.coverage_contract import validate_coverage_verdict
from apollo.overseer.topic_score import compute_topic_score
from apollo.overseer.transcript_coverage import (
    compute_transcript_coverage,
    compute_transcript_coverage_with_spans,
)

pytestmark = pytest.mark.unit


def _two_node_graph():
    return KGGraph(
        nodes=[
            build_node(
                node_type="procedure_step",
                node_id="p1",
                attempt_id=1,
                source="reference",
                content={"action": "Integrate", "purpose": ""},
            ),
            build_node(
                node_type="procedure_step",
                node_id="p2",
                attempt_id=1,
                source="reference",
                content={"action": "Evaluate", "purpose": ""},
            ),
        ]
    )


def _problem():
    return SimpleNamespace(problem_text="Evaluate the integral")


def _item(node_id: str, credit: float) -> dict:
    return {
        "node_id": node_id,
        "covered": credit >= 0.5,
        "credit": credit,
        "confidence": 0.9,
        "evidence_span": "I integrate now",
        "prompted": False,
        "corrected_later": False,
        "basis": "stated",
    }


def _response(payload) -> SimpleNamespace:
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(payload)))]
    )


def _sequenced_client(*outcomes):
    """A client whose successive ``create`` calls yield each outcome in turn
    (a dict payload → JSON response; an Exception instance → raised)."""
    client = MagicMock()
    client.chat.completions.create.side_effect = [
        outcome if isinstance(outcome, Exception) else _response(outcome)
        for outcome in outcomes
    ]
    return client


@pytest.mark.asyncio
async def test_missing_node_triggers_one_readjudication_and_first_call_wins():
    client = _sequenced_client(
        {"verdicts": [_item("p1", 1.0)]},
        {"verdicts": [_item("p1", 0.2), _item("p2", 0.6)]},
    )
    with patch("apollo.overseer.transcript_coverage.bounded_client", return_value=client):
        result = await compute_transcript_coverage(
            [("student", "I integrate now")], _two_node_graph(), _problem()
        )

    validate_coverage_verdict(result)
    assert client.chat.completions.create.call_count == 2
    # p1 keeps the FIRST call's credit; p2 is filled from the retry.
    assert result["procedure_scores"]["p1"] == pytest.approx(1.0)
    assert result["procedure_scores"]["p2"] == pytest.approx(0.6)


@pytest.mark.asyncio
async def test_still_missing_after_retry_is_omitted_not_zeroed():
    client = _sequenced_client(
        {"verdicts": [_item("p1", 1.0)]},
        {"verdicts": [_item("p1", 1.0)]},
    )
    with patch("apollo.overseer.transcript_coverage.bounded_client", return_value=client):
        result = await compute_transcript_coverage(
            [("student", "I integrate now")], _two_node_graph(), _problem()
        )

    validate_coverage_verdict(result)
    assert client.chat.completions.create.call_count == 2
    assert "p2" not in result["procedure_scores"]
    assert "p2" not in result["per_step"]
    assert "p2" not in result["confidences"]
    assert result["procedure_scores"]["p1"] == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_no_graded_verdict_at_all_raises_named_error():
    client = _sequenced_client({"verdicts": []}, {"verdicts": []})
    with patch("apollo.overseer.transcript_coverage.bounded_client", return_value=client):
        with pytest.raises(CoverageGradingError):
            await compute_transcript_coverage(
                [("student", "I integrate now")], _two_node_graph(), _problem()
            )
    assert client.chat.completions.create.call_count == 2


@pytest.mark.asyncio
async def test_retry_provider_failure_degrades_to_exclusion():
    """The semantic retry dying on provider errors (2 attempts) must NOT
    discard the first call's real partial verdicts with a 503."""
    client = _sequenced_client(
        {"verdicts": [_item("p1", 0.85)]},
        RuntimeError("boom"),
        RuntimeError("boom"),
    )
    with patch("apollo.overseer.transcript_coverage.bounded_client", return_value=client):
        result = await compute_transcript_coverage(
            [("student", "I integrate now")], _two_node_graph(), _problem()
        )

    assert client.chat.completions.create.call_count == 3
    assert result["procedure_scores"]["p1"] == pytest.approx(0.85)
    assert "p2" not in result["procedure_scores"]


@pytest.mark.asyncio
async def test_with_spans_lane_shares_the_retry_and_omission_policy():
    client = _sequenced_client(
        {"verdicts": [_item("p1", 0.7)]},
        {"verdicts": [_item("p1", 0.7)]},
    )
    with patch("apollo.overseer.transcript_coverage.bounded_client", return_value=client):
        coverage, spans = await compute_transcript_coverage_with_spans(
            [("student", "I integrate now")], _two_node_graph(), _problem()
        )

    validate_coverage_verdict(coverage)
    assert "p2" not in coverage["procedure_scores"]
    assert spans == {"p1": "I integrate now"}


def test_topic_score_excludes_omitted_node_from_denominator():
    """An omitted node leaves the denominator entirely — weights renormalize
    over the adjudicated set, so 1-of-1-adjudicated full credit is 100, not
    the 50 that zeroing the omitted node would produce."""
    nodes = _two_node_graph().nodes
    result = compute_topic_score(
        coverage={
            "per_step": {"p1": "covered"},
            "procedure_scores": {"p1": 1.0},
        },
        reference_nodes=nodes,
        centrality={"p1": 1.0, "p2": 1.0},
    )

    assert result.score == 100
    assert [topic.canonical_key for topic in result.topics] == ["p1"]


def test_topic_score_all_nodes_omitted_raises_instead_of_silent_f0():
    nodes = _two_node_graph().nodes
    with pytest.raises(ValueError):
        compute_topic_score(coverage={}, reference_nodes=nodes, centrality={})


def test_topic_score_adjudicated_zero_still_counts_as_zero():
    """Exclusion is ONLY for omitted verdicts — an adjudicated node the grader
    scored 0.0 stays in the denominator (a real earned zero)."""
    nodes = _two_node_graph().nodes
    result = compute_topic_score(
        coverage={
            "per_step": {"p1": "covered", "p2": "missing"},
            "procedure_scores": {"p1": 1.0, "p2": 0.0},
        },
        reference_nodes=nodes,
        centrality={"p1": 1.0, "p2": 1.0},
    )

    assert result.score == 50
    assert [topic.canonical_key for topic in result.topics] == ["p1", "p2"]
