from types import SimpleNamespace

import pytest

from apollo.schemas.problem import Problem
from apollo.smart_questions import controller
from apollo.smart_questions.planner import NodeCoverage


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


def _patch_writer(monkeypatch, captured: dict, reply: str = "What do you mean by that?"):
    def fake_write_question(**kwargs):
        captured.update(kwargs)
        return reply

    async def run_sync(fn, **kwargs):
        return fn(**kwargs)

    monkeypatch.setattr(controller, "write_question", fake_write_question)
    monkeypatch.setattr(controller.asyncio, "to_thread", run_sync)


@pytest.mark.asyncio
async def test_controller_records_one_selected_opportunity(monkeypatch):
    db = _DB([])
    captured: dict = {}

    async def evaluate(**kwargs):
        return [NodeCoverage("def_x", "missing", 0.0, "ask what x is for")]

    monkeypatch.setattr(controller, "evaluate_reference_coverage", evaluate)
    _patch_writer(monkeypatch, captured)
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
async def test_controller_passes_nudge_and_public_context_only(monkeypatch):
    db = _DB([])
    captured: dict = {}

    async def evaluate(**kwargs):
        return [NodeCoverage("def_x", "missing", 0.0, "ask what x is for")]

    monkeypatch.setattr(controller, "evaluate_reference_coverage", evaluate)
    _patch_writer(monkeypatch, captured)
    await controller.plan_next_question(
        db,
        attempt_id=2,
        session_id=3,
        problem=_problem(),
        transcript=[("student", "x matters")],
        turn_index=4,
    )
    assert captured == {
        "nudge": "ask what x is for",
        "problem_text": "Explain x.",
        "transcript": [("student", "x matters")],
    }


@pytest.mark.asyncio
async def test_leaking_hint_is_replaced_by_generic_nudge(monkeypatch):
    db = _DB([])
    captured: dict = {}

    async def evaluate(**kwargs):
        # Hint parrots the node's private meaning — must never reach the writer.
        return [NodeCoverage("def_x", "missing", 0.0, "ask about the private meaning")]

    monkeypatch.setattr(controller, "evaluate_reference_coverage", evaluate)
    _patch_writer(monkeypatch, captured)
    result = await controller.plan_next_question(
        db,
        attempt_id=2,
        session_id=3,
        problem=_problem(),
        transcript=[("student", "x matters")],
        turn_index=4,
    )
    assert result.action == "ask"
    assert captured["nudge"] == controller._GENERIC_NUDGE
    assert "private" not in captured["nudge"]


@pytest.mark.asyncio
async def test_empty_hint_uses_generic_nudge(monkeypatch):
    db = _DB([])
    captured: dict = {}

    async def evaluate(**kwargs):
        return [NodeCoverage("def_x", "missing", 0.0)]

    monkeypatch.setattr(controller, "evaluate_reference_coverage", evaluate)
    _patch_writer(monkeypatch, captured)
    await controller.plan_next_question(
        db,
        attempt_id=2,
        session_id=3,
        problem=_problem(),
        transcript=[("student", "x matters")],
        turn_index=4,
    )
    assert captured["nudge"] == controller._GENERIC_NUDGE


@pytest.mark.asyncio
async def test_leaking_question_falls_back_to_safe_question(monkeypatch):
    db = _DB([])
    captured: dict = {}

    async def evaluate(**kwargs):
        return [NodeCoverage("def_x", "missing", 0.0, "ask what x is for")]

    monkeypatch.setattr(controller, "evaluate_reference_coverage", evaluate)
    _patch_writer(monkeypatch, captured, reply="Is it about the private meaning of x?")
    result = await controller.plan_next_question(
        db,
        attempt_id=2,
        session_id=3,
        problem=_problem(),
        transcript=[("student", "x matters")],
        turn_index=4,
    )
    assert result.action == "ask"
    assert result.question == controller._SAFE_FALLBACK
    assert len(db.added) == 1  # the opportunity is still spent


@pytest.mark.asyncio
async def test_controller_closes_answered_gap_then_stops(monkeypatch):
    row = SimpleNamespace(state="asked_waiting", answered_turn=None, reference_node_id="def_x")
    db = _DB([row])

    async def evaluate(**kwargs):
        return [NodeCoverage("def_x", "missing", 0.0)]

    monkeypatch.setattr(controller, "evaluate_reference_coverage", evaluate)
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
