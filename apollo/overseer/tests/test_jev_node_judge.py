"""Jev per-turn node judge: prompt shape, label->credit, aggregation, contract.

The client runs on an ``httpx.MockTransport`` scripted per (student turn, node),
so every request the judge builds is observed exactly as Jev would see it."""

from __future__ import annotations

import json

import httpx
import pytest

from apollo.hoot_bridge.reference_answer import ASIDE_MESSAGE_INTENT_TAG
from apollo.ontology import KGGraph, build_node
from apollo.overseer.coverage_contract import JEV_BASIS_VALUES, validate_coverage_verdict
from apollo.overseer.jev_client import JevClient, JevError
from apollo.overseer.jev_node_judge import (
    JEV_CRITERIA,
    JEV_INSTRUCTIONS_TEMPLATE,
    LABEL_CREDIT,
    LABELS,
    JevJudgeResult,
    TurnNodeCell,
    aggregate,
    build_question_payload,
    evidence_spans,
    judge_transcript,
    question_ids,
    student_turns,
)
from apollo.overseer.transcript_coverage import _build_rubric_items, validate_span

pytestmark = pytest.mark.unit


def _graph() -> KGGraph:
    eq = build_node(
        node_type="equation",
        node_id="eq1",
        attempt_id=1,
        source="reference",
        content={"symbolic": "P + 0.5*rho*v**2 = const", "label": "Bernoulli"},
    )
    cond = build_node(
        node_type="condition",
        node_id="c1",
        attempt_id=1,
        source="reference",
        content={"applies_when": "steady flow", "label": "steady"},
    )
    ungraded = build_node(
        node_type="definition",
        node_id="d1",
        attempt_id=1,
        source="reference",
        content={"concept": "pressure", "meaning": "force per area"},
    )
    return KGGraph(nodes=[eq, cond, ungraded], edges=[])


def _scripted_client(script: dict[tuple[str, str], str], requests: list[dict] | None = None):
    """``script[(student_answer, node_id)] = label``; unscripted cells are not_covered."""
    seen = requests if requests is not None else []
    node_by_qid = question_ids(_build_rubric_items(_graph()))

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body)
        answer = body["state"]["student_answer"]
        answers = {}
        for qid in body["questions"]:
            label = script.get((answer, node_by_qid[qid]), "not_covered")
            answers[qid] = {
                "type": "choice",
                "choice": label,
                "confidence": 0.9,
                "probabilities": {name: (0.9 if name == label else 0.1 / 3) for name in LABELS},
            }
        return httpx.Response(
            200,
            json={
                "model": "jev-1.14.0",
                "answers": answers,
                "usage": {"input_tokens": 100, "output_tokens": 2},
            },
        )

    return JevClient(api_key="k", transport=httpx.MockTransport(handler))


_TRANSCRIPT = (
    ("apollo", "What equation do we use?"),
    ("student", "Pressure plus kinetic term is constant."),
    ("apollo", "When does that hold?"),
    ("student", "Only when the flow is steady."),
)


def test_constants_match_the_spec_table():
    assert LABELS == ("covered", "partial", "not_covered", "incorrect")
    assert LABEL_CREDIT == {"covered": 1.0, "partial": 0.6, "not_covered": 0.0, "incorrect": 0.0}
    assert set(JEV_CRITERIA) == set(LABELS)
    assert JEV_BASIS_VALUES == LABELS


async def test_one_request_per_student_turn_with_one_choice_question_per_graded_node():
    requests: list[dict] = []
    client = _scripted_client({}, requests)

    await judge_transcript(_graph(), _TRANSCRIPT, client=client)

    assert len(requests) == 2
    by_answer = {body["state"]["student_answer"]: body for body in requests}
    first = by_answer["Pressure plus kinetic term is constant."]
    second = by_answer["Only when the flow is steady."]
    assert first["model"] == "jev-latest"
    assert first["state"] == {
        "apollo_question": "What equation do we use?",
        "student_answer": "Pressure plus kinetic term is constant.",
        "previous_student_answer": None,
    }
    assert second["state"] == {
        "apollo_question": "When does that hold?",
        "student_answer": "Only when the flow is steady.",
        "previous_student_answer": "Pressure plus kinetic term is constant.",
    }
    # Graded nodes only (the definition is ungraded), in rubric order.
    assert list(first["questions"]) == ["n0", "n1"]
    question = first["questions"]["n0"]
    assert question["type"] == "choice"
    assert question["criteria"] == JEV_CRITERIA
    assert question["instructions"].startswith(JEV_INSTRUCTIONS_TEMPLATE.split("{")[0])
    assert "Reference idea (equation): Bernoulli" in question["instructions"]
    assert "P + 0.5*rho*v**2 = const" in question["instructions"]
    assert first["questions"] == build_question_payload(_build_rubric_items(_graph()))


async def test_label_to_credit_and_contract():
    client = _scripted_client(
        {
            ("Pressure plus kinetic term is constant.", "eq1"): "covered",
            ("Only when the flow is steady.", "c1"): "partial",
        }
    )
    result = await judge_transcript(_graph(), _TRANSCRIPT, client=client)

    verdict = result.verdict
    validate_coverage_verdict(verdict)
    assert verdict["procedure_scores"] == {"eq1": 1.0, "c1": 0.6}
    assert verdict["per_step"] == {"eq1": "covered", "c1": "covered"}
    assert verdict["basis"] == {"eq1": "covered", "c1": "partial"}
    assert verdict["confidences"] == {"eq1": 0.9, "c1": 0.9}
    assert verdict["negotiation_counts"] == {
        "dual": 0,
        "disputed": 0,
        "paraphrased": 0,
        "skipped": 0,
    }
    assert result.winning_turn == {"eq1": 0, "c1": 1}
    assert len(result.cells) == 4
    assert result.usage == {"input_tokens": 200, "output_tokens": 4}
    cell = next(c for c in result.cells if c.turn_index == 0 and c.node_id == "eq1")
    assert cell.label == "covered"
    assert cell.probabilities["covered"] == pytest.approx(0.9)


async def test_incorrect_scores_zero_and_is_kept_as_the_feedback_label():
    client = _scripted_client({("Pressure plus kinetic term is constant.", "eq1"): "incorrect"})
    result = await judge_transcript(_graph(), _TRANSCRIPT, client=client)
    assert result.verdict["procedure_scores"]["eq1"] == 0.0
    assert result.verdict["per_step"]["eq1"] == "missing"
    assert result.verdict["basis"]["eq1"] == "incorrect"
    assert result.winning_turn["eq1"] == 0


async def test_later_correction_overrides_earlier_incorrect():
    client = _scripted_client(
        {
            ("Pressure plus kinetic term is constant.", "eq1"): "incorrect",
            ("Only when the flow is steady.", "eq1"): "covered",
        }
    )
    result = await judge_transcript(_graph(), _TRANSCRIPT, client=client)
    assert result.verdict["basis"]["eq1"] == "covered"
    assert result.verdict["procedure_scores"]["eq1"] == 1.0
    assert result.winning_turn["eq1"] == 1


async def test_best_label_across_turns_wins_even_if_a_later_turn_is_worse():
    client = _scripted_client(
        {
            ("Pressure plus kinetic term is constant.", "eq1"): "partial",
            ("Only when the flow is steady.", "eq1"): "incorrect",
        }
    )
    result = await judge_transcript(_graph(), _TRANSCRIPT, client=client)
    assert result.verdict["basis"]["eq1"] == "partial"
    assert result.winning_turn["eq1"] == 0


def _cell(turn: int, label: str, confidence: float = 0.5) -> TurnNodeCell:
    return TurnNodeCell(turn, "eq1", label, {label: confidence}, confidence, 10.0)


def test_aggregate_rank_and_earliest_tie_break():
    cells = [_cell(0, "not_covered"), _cell(1, "partial", 0.7), _cell(2, "partial", 0.95)]
    verdict, winning = aggregate(["eq1"], cells)
    assert verdict["basis"] == {"eq1": "partial"}
    assert verdict["confidences"] == {"eq1": 0.7}
    assert winning == {"eq1": 1}

    verdict, winning = aggregate(["eq1"], [_cell(0, "not_covered"), _cell(1, "incorrect")])
    assert verdict["basis"] == {"eq1": "incorrect"}
    assert winning == {"eq1": 1}


async def test_aside_rows_are_excluded():
    requests: list[dict] = []
    transcript = [
        {"role": "apollo", "content": "What equation?", "intent": None},
        {"role": "student", "content": "What is Bernoulli?"},
        {"role": "apollo", "content": "Hoot says: P + q = c", "intent": ASIDE_MESSAGE_INTENT_TAG},
        {"role": "student", "content": "So pressure plus q is constant."},
    ]
    await judge_transcript(_graph(), transcript, client=_scripted_client({}, requests))

    states = sorted((body["state"] for body in requests), key=lambda s: s["student_answer"])
    assert [s["student_answer"] for s in states] == [
        "So pressure plus q is constant.",
        "What is Bernoulli?",
    ]
    # The aside never becomes the question a student turn answers.
    assert states[0]["apollo_question"] == "What equation?"
    assert all("Hoot says" not in json.dumps(body) for body in requests)


async def test_empty_transcript_is_all_not_covered_without_any_request():
    requests: list[dict] = []
    result = await judge_transcript(
        _graph(), (("apollo", "Hi, teach me!"),), client=_scripted_client({}, requests)
    )
    assert requests == []
    validate_coverage_verdict(result.verdict)
    assert result.verdict["procedure_scores"] == {"eq1": 0.0, "c1": 0.0}
    assert result.verdict["basis"] == {"eq1": "not_covered", "c1": "not_covered"}
    assert result.verdict["confidences"] == {"eq1": 1.0, "c1": 1.0}
    assert result.winning_turn == {"eq1": None, "c1": None}
    assert result.cells == []
    assert result.usage == {}


async def test_default_client_is_built_from_env_when_none_passed(monkeypatch):
    monkeypatch.delenv("JEV_API_KEY", raising=False)
    with pytest.raises(JevError, match="JEV_API_KEY"):
        await judge_transcript(_graph(), _TRANSCRIPT)


async def test_missing_answer_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"model": "m", "answers": {}})

    client = JevClient(api_key="k", transport=httpx.MockTransport(handler))
    with pytest.raises(JevError, match="no answer for n0"):
        await judge_transcript(_graph(), _TRANSCRIPT, client=client)


async def test_off_label_choice_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        answers = {
            qid: {"choice": "maybe", "confidence": 0.5, "probabilities": {}}
            for qid in body["questions"]
        }
        return httpx.Response(200, json={"model": "m", "answers": answers})

    client = JevClient(api_key="k", transport=httpx.MockTransport(handler))
    with pytest.raises(JevError, match="off-label"):
        await judge_transcript(_graph(), _TRANSCRIPT, client=client)


def test_student_turns_accepts_tuples_and_null_content():
    turns = student_turns([("student", None), ("apollo", "q"), ("student", "a")])
    assert [t.student_answer for t in turns] == ["", "a"]
    assert turns[1].previous_student_answer == ""
    assert turns[0].apollo_question is None


def test_node_text_falls_back_to_content_without_display_name():
    payload = build_question_payload([{"id": "x", "type": "simplification", "content": {"a": 1}}])
    assert payload["n0"]["instructions"].endswith('Reference idea (simplification): {"a": 1}')


async def test_evidence_spans_are_the_winning_student_turn_verbatim():
    client = _scripted_client({("Only when the flow is steady.", "c1"): "partial"})
    result = await judge_transcript(_graph(), _TRANSCRIPT, client=client)
    spans = evidence_spans(result, _TRANSCRIPT)
    assert spans == {"c1": "Only when the flow is steady."}
    students = [content for role, content in _TRANSCRIPT if role == "student"]
    assert all(validate_span(span, students) for span in spans.values())


def test_evidence_spans_skip_blank_turns():
    verdict, winning = aggregate(["eq1"], [_cell(0, "covered")])
    result = JevJudgeResult(verdict=verdict, cells=[], winning_turn=winning, usage={})
    assert evidence_spans(result, [("student", "   ")]) == {}


@pytest.mark.parametrize("label", LABELS)
def test_contract_accepts_every_jev_label_as_basis(label):
    verdict, _ = aggregate(["eq1"], [_cell(0, label)])
    validate_coverage_verdict(verdict)
