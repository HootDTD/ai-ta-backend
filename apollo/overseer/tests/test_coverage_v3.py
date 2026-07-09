"""V3 coverage tests (item #10): retry, batch, confidence, no soft-fail."""
from __future__ import annotations
import asyncio

import json
from unittest.mock import MagicMock, patch

import pytest

from apollo.errors import CoverageGradingError
from apollo.ontology import Edge, EdgeType, KGGraph, build_node
from apollo.overseer.coverage import (
    _RETRY_ATTEMPTS,
    _batch_binary_match,
    compute_coverage,
)


def _eq_node(node_id: str, symbolic: str, label: str = "", *, attempt_id: int = 1):
    return build_node(
        node_type="equation",
        node_id=node_id,
        attempt_id=attempt_id,
        source="parser",
        content={"symbolic": symbolic, "label": label},
    )


def _cond_node(node_id: str, applies_when: str, label: str = "",
               *, attempt_id: int = 1):
    return build_node(
        node_type="condition",
        node_id=node_id,
        attempt_id=attempt_id,
        source="parser",
        content={"applies_when": applies_when, "label": label},
    )


def _proc_node(node_id: str, action: str, *, attempt_id: int = 1):
    return build_node(
        node_type="procedure_step",
        node_id=node_id,
        attempt_id=attempt_id,
        source="parser",
        content={"action": action, "purpose": ""},
    )


def _mock_openai_returning(payloads):
    """Build an OpenAI mock whose chat.completions.create returns each
    payload string in order. Use side_effect with iter() so it raises
    StopIteration on excess calls (catches over-calling bugs)."""
    client = MagicMock()
    it = iter(payloads)
    client.chat.completions.create.side_effect = lambda **kw: MagicMock(
        choices=[MagicMock(message=MagicMock(content=next(it)))]
    )
    return client


def _mock_openai_always(payload: str):
    client = MagicMock()
    client.chat.completions.create.return_value = MagicMock(
        choices=[MagicMock(message=MagicMock(content=payload))]
    )
    return client


# ---- compute_coverage end-to-end ----------------------------------------

@patch("apollo.overseer.coverage.OpenAI")
@patch("apollo.overseer.coverage.time.sleep", lambda *_: None)
def test_compute_coverage_batches_binary_calls(mock_client_cls):
    """One LLM call per binary type, regardless of how many ref nodes."""
    student = KGGraph(nodes=[
        _eq_node("stu_eq1", "A1*v1 - A2*v2"),
        _eq_node("stu_eq2", "P1 + 0.5*rho*v1**2 - P2 - 0.5*rho*v2**2"),
    ])
    reference = KGGraph(nodes=[
        _eq_node("ref_continuity", "rho*A1*v1 - rho*A2*v2", "Continuity",
                 attempt_id=1),
        _eq_node("ref_bernoulli", "P1 + 0.5*rho*v1**2 - P2 - 0.5*rho*v2**2",
                 "Bernoulli", attempt_id=1),
    ])
    payload = json.dumps({"matches": [
        {"ref_id": "ref_continuity", "covered": True, "confidence": 0.9},
        {"ref_id": "ref_bernoulli", "covered": True, "confidence": 0.9},
    ]})
    client = _mock_openai_always(payload)
    mock_client_cls.return_value = client

    result = asyncio.run(compute_coverage(student, reference))

    # Only ONE call (one type, one batch) — vs V2's two calls.
    assert client.chat.completions.create.call_count == 1
    assert result["per_step"]["ref_continuity"] == "covered"
    assert result["per_step"]["ref_bernoulli"] == "covered"
    assert result["confidences"]["ref_continuity"] == 0.9


@patch("apollo.overseer.coverage.OpenAI")
@patch("apollo.overseer.coverage.time.sleep", lambda *_: None)
def test_low_confidence_covered_downgraded_to_missing(mock_client_cls):
    """Below-floor confidence on covered=true => downgraded to missing
    (item #10 confidence gate)."""
    student = KGGraph(nodes=[_eq_node("stu_eq1", "A*v")])
    reference = KGGraph(nodes=[
        _eq_node("ref_eq1", "rho*A*v", "Continuity"),
    ])
    payload = json.dumps({"matches": [
        {"ref_id": "ref_eq1", "covered": True, "confidence": 0.2},
    ]})
    mock_client_cls.return_value = _mock_openai_always(payload)

    result = asyncio.run(compute_coverage(student, reference))
    assert result["per_step"]["ref_eq1"] == "missing"
    # But the confidence is preserved so the diagnostic can hedge.
    assert result["confidences"]["ref_eq1"] == 0.2


@patch("apollo.overseer.coverage.OpenAI")
@patch("apollo.overseer.coverage.time.sleep", lambda *_: None)
def test_empty_student_short_circuits_without_llm(mock_client_cls):
    """No student equations of a type => no LLM call needed; all missing."""
    student = KGGraph(nodes=[])  # empty
    reference = KGGraph(nodes=[
        _eq_node("ref_eq1", "x"),
        _cond_node("ref_c1", "y"),
    ])
    client = MagicMock()
    mock_client_cls.return_value = client

    result = asyncio.run(compute_coverage(student, reference))
    assert result["per_step"]["ref_eq1"] == "missing"
    assert result["per_step"]["ref_c1"] == "missing"
    # Zero LLM calls for either type.
    assert client.chat.completions.create.call_count == 0


@patch("apollo.overseer.coverage.OpenAI")
@patch("apollo.overseer.coverage.time.sleep", lambda *_: None)
def test_retry_on_transient_then_succeeds(mock_client_cls):
    """First call raises; second succeeds. Final result is the success."""
    payload = json.dumps({"matches": [
        {"ref_id": "ref_eq1", "covered": True, "confidence": 0.9},
    ]})
    success = MagicMock(choices=[MagicMock(message=MagicMock(content=payload))])
    client = MagicMock()
    client.chat.completions.create.side_effect = [
        RuntimeError("transient"),
        success,
    ]
    mock_client_cls.return_value = client

    student = KGGraph(nodes=[_eq_node("stu", "x")])
    reference = KGGraph(nodes=[_eq_node("ref_eq1", "x")])

    result = asyncio.run(compute_coverage(student, reference))
    assert result["per_step"]["ref_eq1"] == "covered"
    assert client.chat.completions.create.call_count == 2  # retry once


@patch("apollo.overseer.coverage.OpenAI")
@patch("apollo.overseer.coverage.time.sleep", lambda *_: None)
def test_retry_exhausted_raises_coverage_grading_error(mock_client_cls):
    """All N attempts fail => CoverageGradingError, no silent downgrade."""
    client = MagicMock()
    client.chat.completions.create.side_effect = RuntimeError("API down")
    mock_client_cls.return_value = client

    student = KGGraph(nodes=[_eq_node("stu", "x")])
    reference = KGGraph(nodes=[_eq_node("ref", "x")])

    with pytest.raises(CoverageGradingError) as exc_info:
        asyncio.run(compute_coverage(student, reference))

    assert exc_info.value.stage.startswith("binary_match")
    # Should have tried _RETRY_ATTEMPTS times before giving up.
    assert client.chat.completions.create.call_count == _RETRY_ATTEMPTS


# ---- _batch_binary_match unit tests -------------------------------------

@patch("apollo.overseer.coverage.OpenAI")
@patch("apollo.overseer.coverage.time.sleep", lambda *_: None)
def test_batch_returns_verdict_per_ref_id(mock_client_cls):
    payload = json.dumps({"matches": [
        {"ref_id": "a", "covered": True, "confidence": 0.8},
        {"ref_id": "b", "covered": False, "confidence": 0.9},
    ]})
    mock_client_cls.return_value = _mock_openai_always(payload)
    refs = [_eq_node("a", "x"), _eq_node("b", "y")]
    students = [_eq_node("s1", "x")]

    result = _batch_binary_match(
        entry_type="equation",
        reference_nodes=refs,
        student_nodes=students,
    )
    assert result["a"]["covered"] is True
    assert result["a"]["confidence"] == 0.8
    assert result["b"]["covered"] is False


@patch("apollo.overseer.coverage.OpenAI")
@patch("apollo.overseer.coverage.time.sleep", lambda *_: None)
def test_batch_fills_missing_ref_with_not_covered(mock_client_cls):
    """If LLM returns matches for fewer ref_ids than asked, fill the
    remainder with covered=false at confidence 0 — orchestrator must
    have a verdict for every ref."""
    payload = json.dumps({"matches": [
        {"ref_id": "a", "covered": True, "confidence": 0.9},
    ]})  # missing "b"
    mock_client_cls.return_value = _mock_openai_always(payload)
    refs = [_eq_node("a", "x"), _eq_node("b", "y")]
    students = [_eq_node("s1", "x")]

    result = _batch_binary_match(
        entry_type="equation",
        reference_nodes=refs,
        student_nodes=students,
    )
    assert "a" in result and "b" in result
    assert result["b"]["covered"] is False


def test_batch_with_no_reference_short_circuits():
    result = _batch_binary_match(
        entry_type="equation",
        reference_nodes=[],
        student_nodes=[_eq_node("s", "x")],
    )
    assert result == {}


def test_batch_with_no_student_short_circuits_to_all_missing():
    refs = [_eq_node("a", "x"), _eq_node("b", "y")]
    result = _batch_binary_match(
        entry_type="equation",
        reference_nodes=refs,
        student_nodes=[],
    )
    assert result["a"]["covered"] is False
    assert result["a"]["confidence"] == 1.0
    assert result["b"]["covered"] is False


# ---- procedure scoring with retry / confidence --------------------------

@patch("apollo.overseer.coverage.OpenAI")
@patch("apollo.overseer.coverage.time.sleep", lambda *_: None)
def test_procedure_score_with_confidence(mock_client_cls):
    payload = json.dumps({"score": 0.8, "confidence": 0.9})
    mock_client_cls.return_value = _mock_openai_always(payload)

    student = KGGraph(nodes=[_proc_node("s1", "use continuity to find v2")])
    reference = KGGraph(nodes=[_proc_node("r1", "apply continuity to get v2")])

    result = asyncio.run(compute_coverage(student, reference))
    assert result["procedure_scores"]["r1"] == 0.8
    assert result["confidences"]["r1"] == 0.9
    assert result["per_step"]["r1"] == "covered"


@patch("apollo.overseer.coverage.OpenAI")
@patch("apollo.overseer.coverage.time.sleep", lambda *_: None)
def test_procedure_retry_exhausted_raises(mock_client_cls):
    client = MagicMock()
    client.chat.completions.create.side_effect = RuntimeError("boom")
    mock_client_cls.return_value = client

    student = KGGraph(nodes=[_proc_node("s1", "x")])
    reference = KGGraph(nodes=[_proc_node("r1", "y")])

    with pytest.raises(CoverageGradingError) as exc_info:
        asyncio.run(compute_coverage(student, reference))
    assert exc_info.value.stage == "procedure_match"
