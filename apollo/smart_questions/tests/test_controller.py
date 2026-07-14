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


@pytest.mark.asyncio
async def test_controller_records_one_selected_opportunity(monkeypatch):
    db = _DB([])

    async def evaluate(**kwargs):
        return UnifiedQuestionResult(
            coverage=(NodeCoverage("def_x", "missing", 0.0),),
            action="ask",
            target_node_id="def_x",
            reply="What do you mean by that?",
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
    assert result.action == "ask"
    assert result.target_node_id == "def_x"
    assert len(db.added) == 1
    assert db.added[0].asked_turn == 5


@pytest.mark.asyncio
async def test_controller_closes_answered_gap_then_stops(monkeypatch):
    row = SimpleNamespace(state="asked_waiting", answered_turn=None, reference_node_id="def_x")
    db = _DB([row])

    async def evaluate(**kwargs):
        assert kwargs["already_asked_node_ids"] == {"def_x"}
        return UnifiedQuestionResult(
            coverage=(NodeCoverage("def_x", "missing", 0.0),),
            action="done",
            target_node_id=None,
            reply=None,
        )

    monkeypatch.setattr(controller, "evaluate_and_ask", evaluate)
    result = await controller.plan_next_question(
        db,
        attempt_id=2,
        session_id=3,
        problem=_problem(),
        transcript=[("student", "I still do not know")],
        turn_index=6,
    )
    assert result.action == "done"
    assert row.state == "answered"
    assert row.answered_turn == 6
    assert db.added == []
