from types import SimpleNamespace

import pytest

from apollo.schemas.problem import Problem
from apollo.smart_questions import controller
from apollo.smart_questions.unified import NodeCoverage, UnifiedQuestionResult


def _problem() -> Problem:
    return Problem.model_validate(
        {
            "id": "p1",
            "concept_id": "c1",
            "difficulty": "intro",
            "problem_text": "Explain x.",
            "reference_solution": [
                {
                    "step": 1,
                    "entry_type": "definition",
                    "id": "def_x",
                    "content": {"concept": "x", "meaning": "the private meaning"},
                }
            ],
        }
    )


class _Scalars:
    def __init__(self, rows):
        self.rows = rows

    def all(self):
        return self.rows


class _Result:
    def __init__(self, rows):
        self.rows = rows

    def scalars(self):
        return _Scalars(self.rows)


class _DB:
    def __init__(self, rows):
        self.rows = rows
        self.added = []

    async def execute(self, statement):
        return _Result(self.rows)

    def add(self, row):
        self.added.append(row)


def _ask(state="missing"):
    return UnifiedQuestionResult(
        coverage=(NodeCoverage("def_x", state, 0.2, "x matters"),),
        action="ask",
        target_node_id="def_x",
        reply="What do you mean by x?",
        question="What do you mean by x?",
    )


@pytest.mark.asyncio
async def test_controller_records_first_selected_question(monkeypatch):
    db = _DB([])

    async def evaluate(**kwargs):
        assert kwargs["question_history"] == ()
        return _ask()

    monkeypatch.setattr(controller, "evaluate_and_ask", evaluate)
    result = await controller.plan_next_question(
        db,
        attempt_id=2,
        session_id=3,
        problem=_problem(),
        transcript=[("student", "x matters")],
        turn_index=4,
    )
    assert result.target_node_id == "def_x"
    assert len(db.added) == 1
    assert db.added[0].asked_turn == 5


@pytest.mark.asyncio
async def test_controller_ledger_stores_bare_question_not_acknowledgement(monkeypatch):
    db = _DB([])

    async def evaluate(**kwargs):
        return UnifiedQuestionResult(
            coverage=(NodeCoverage("def_x", "missing", 0.0, None),),
            action="ask",
            target_node_id="def_x",
            reply="I understand your first point. What do you mean by x?",
            question="What do you mean by x?",
        )

    monkeypatch.setattr(controller, "evaluate_and_ask", evaluate)
    result = await controller.plan_next_question(
        db,
        attempt_id=2,
        session_id=3,
        problem=_problem(),
        transcript=[("student", "x matters")],
        turn_index=4,
    )

    assert result.question == "I understand your first point. What do you mean by x?"
    assert db.added[0].question == "What do you mean by x?"


@pytest.mark.asyncio
async def test_controller_reuses_row_when_latest_answer_is_insufficient(monkeypatch):
    row = SimpleNamespace(
        state="asked_waiting",
        answered_turn=None,
        reference_node_id="def_x",
        question="What is x?",
        asked_turn=5,
    )
    db = _DB([row])

    async def evaluate(**kwargs):
        history = kwargs["question_history"]
        assert history[0].question == "What is x?"
        assert history[0].state == "asked_waiting"
        return _ask("tentative")

    monkeypatch.setattr(controller, "evaluate_and_ask", evaluate)
    result = await controller.plan_next_question(
        db,
        attempt_id=2,
        session_id=3,
        problem=_problem(),
        transcript=[("student", "x matters")],
        turn_index=6,
    )
    assert result.action == "ask"
    assert db.added == []
    assert row.question == "What do you mean by x?"
    assert row.asked_turn == 7
    assert row.state == "asked_waiting"
    assert row.answered_turn is None


@pytest.mark.asyncio
async def test_controller_closes_previous_target_when_advancing(monkeypatch):
    previous = SimpleNamespace(
        state="asked_waiting",
        answered_turn=None,
        reference_node_id="old",
        question="Old question?",
        asked_turn=3,
    )
    db = _DB([previous])
    monkeypatch.setattr(controller, "evaluate_and_ask", lambda **kwargs: None)

    async def evaluate(**kwargs):
        return _ask()

    monkeypatch.setattr(controller, "evaluate_and_ask", evaluate)
    await controller.plan_next_question(
        db,
        attempt_id=2,
        session_id=3,
        problem=_problem(),
        transcript=[("student", "x matters")],
        turn_index=4,
    )
    assert previous.state == "answered"
    assert previous.answered_turn == 4
    assert len(db.added) == 1


@pytest.mark.asyncio
async def test_controller_marks_waiting_row_answered_on_done(monkeypatch):
    row = SimpleNamespace(
        state="asked_waiting",
        answered_turn=None,
        reference_node_id="def_x",
        question="What is x?",
        asked_turn=5,
    )
    already_answered = SimpleNamespace(
        state="answered",
        answered_turn=2,
        reference_node_id="old",
        question="Old?",
        asked_turn=1,
    )
    db = _DB([row, already_answered])

    async def evaluate(**kwargs):
        return UnifiedQuestionResult(
            coverage=(NodeCoverage("def_x", "understood", 1, "x"),),
            action="done",
            target_node_id=None,
            reply=None,
            question=None,
        )

    monkeypatch.setattr(controller, "evaluate_and_ask", evaluate)
    result = await controller.plan_next_question(
        db,
        attempt_id=2,
        session_id=3,
        problem=_problem(),
        transcript=[("student", "x")],
        turn_index=6,
    )
    assert result.action == "done"
    assert row.state == "answered"
    assert row.answered_turn == 6
    assert already_answered.answered_turn == 2
    assert db.added == []
