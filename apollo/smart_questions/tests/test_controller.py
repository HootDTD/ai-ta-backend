from types import SimpleNamespace

import pytest

from apollo.persistence.models import QuestionOpportunity
from apollo.schemas.problem import Problem
from apollo.smart_questions import controller
from apollo.smart_questions.unified import EvidenceQuote, TallyUpdate, UnifiedQuestionResult


def _problem() -> Problem:
    return Problem.model_validate(
        {
            "id": "p1",
            "concept_id": "c1",
            "difficulty": "intro",
            "problem_text": "Explain x?",
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
    def __init__(self, *results):
        self.results = list(results)
        self.added = []
        self.statements = []

    async def execute(self, statement):
        self.statements.append(statement)
        return _Result(self.results.pop(0))

    def add(self, row):
        self.added.append(row)


def _ask(*updates):
    return UnifiedQuestionResult(
        tally_updates=tuple(updates),
        action="ask",
        target_node_id="def_x",
        reply="That helps. What do you mean by x?",
        question="What do you mean by x?",
    )


@pytest.mark.asyncio
async def test_absent_rows_default_missing_and_ask_persists_tally_and_audit(monkeypatch):
    db = _DB([])

    async def evaluate(**kwargs):
        assert kwargs["tally_state"][0].status == "missing"
        assert kwargs["tally_state"][0].times_asked == 0
        assert kwargs["budget"].questions_asked == 0
        return _ask(TallyUpdate("def_x", "tentative", EvidenceQuote(0, "x matters"), False))

    monkeypatch.setattr(controller, "evaluate_and_ask", evaluate)
    result = await controller.plan_next_question(
        db,
        course_id=11,
        attempt_id=2,
        session_id=3,
        problem=_problem(),
        transcript=[("student", "x matters")],
        turn_index=0,
    )
    opportunity = next(row for row in db.added if isinstance(row, QuestionOpportunity))
    assert result.question == "That helps. What do you mean by x?"
    assert opportunity.course_id == 11
    assert opportunity.session_id == 3
    assert opportunity.state == "tentative"
    assert opportunity.evidence == [{"turn_id": 0, "quote": "x matters"}]
    assert opportunity.student_declined is False
    assert opportunity.times_asked == 1
    assert opportunity.last_asked_turn == 1
    assert opportunity.question == "What do you mean by x?"


@pytest.mark.asyncio
async def test_confirm_once_round_trip_increments_target_to_two(monkeypatch):
    target = SimpleNamespace(
        reference_node_id="def_x",
        state="missing",
        evidence=[],
        student_declined=True,
        times_asked=1,
        last_asked_turn=3,
        question="Prior question?",
        asked_turn=3,
        answered_turn=4,
    )
    db = _DB([target])

    async def evaluate(**kwargs):
        state = kwargs["tally_state"][0]
        assert state.student_declined is True
        assert state.times_asked == 1
        assert kwargs["budget"].questions_asked == 1
        return _ask(TallyUpdate("def_x", "understood", EvidenceQuote(0, "x matters"), False))

    monkeypatch.setattr(controller, "evaluate_and_ask", evaluate)
    await controller.plan_next_question(
        db,
        course_id=11,
        attempt_id=2,
        session_id=3,
        problem=_problem(),
        transcript=[("student", "x matters")],
        turn_index=4,
    )
    assert target.state == "understood"
    assert target.student_declined is False
    assert target.times_asked == 2
    assert target.last_asked_turn == 5


@pytest.mark.asyncio
async def test_invalid_evidence_drops_update_and_preserves_prior(monkeypatch, caplog):
    row = SimpleNamespace(
        reference_node_id="def_x",
        state="tentative",
        evidence=[{"turn_id": 0, "quote": "old quote"}],
        student_declined=False,
        times_asked=1,
        last_asked_turn=1,
        question="Prior question?",
        asked_turn=1,
        answered_turn=2,
    )
    db = _DB([row])
    monkeypatch.setattr(
        controller,
        "evaluate_and_ask",
        lambda **kwargs: _async_result(
            UnifiedQuestionResult(
                (TallyUpdate("def_x", "understood", EvidenceQuote(0, "invented"), True),),
                "done",
                None,
                None,
                None,
            )
        ),
    )
    with caplog.at_level("WARNING"):
        result = await controller.plan_next_question(
            db,
            course_id=11,
            attempt_id=2,
            session_id=3,
            problem=_problem(),
            transcript=[("student", "new words")],
            turn_index=2,
        )
    assert result.action == "done"
    assert row.state == "tentative"
    assert row.evidence == [{"turn_id": 0, "quote": "old quote"}]
    assert row.student_declined is False
    assert "apollo_question_opportunity_invalid_evidence" in caplog.text


async def _async_result(value):
    return value


@pytest.mark.asyncio
async def test_reference_opportunity_state_is_not_input_but_still_written(monkeypatch):
    audit = SimpleNamespace(
        reference_node_id="def_x",
        state="asked_waiting",
        question="Old question?",
        asked_turn=1,
        answered_turn=None,
        evidence=[],
        student_declined=False,
        times_asked=1,
        last_asked_turn=1,
    )
    db = _DB([audit])

    async def evaluate(**kwargs):
        assert "question_history" not in kwargs
        return _ask()

    monkeypatch.setattr(controller, "evaluate_and_ask", evaluate)
    await controller.plan_next_question(
        db,
        course_id=11,
        attempt_id=2,
        session_id=3,
        problem=_problem(),
        transcript=[("student", "x")],
        turn_index=2,
    )
    assert audit.question == "What do you mean by x?"
    assert audit.asked_turn == 3
    assert audit.state == "asked_waiting"


@pytest.mark.asyncio
async def test_done_closes_pending_question_without_overwriting_tally_state(monkeypatch):
    audit = SimpleNamespace(
        reference_node_id="def_x",
        state="asked_waiting",
        question="Old question?",
        asked_turn=1,
        answered_turn=None,
        evidence=[],
        student_declined=False,
        times_asked=1,
        last_asked_turn=1,
    )
    db = _DB([audit])

    async def evaluate(**kwargs):
        return UnifiedQuestionResult((), "done", None, None, None)

    monkeypatch.setattr(controller, "evaluate_and_ask", evaluate)
    result = await controller.plan_next_question(
        db,
        course_id=11,
        attempt_id=2,
        session_id=3,
        problem=_problem(),
        transcript=[("student", "x")],
        turn_index=2,
    )
    assert result.action == "done"
    assert audit.state == "asked_waiting"
    assert audit.answered_turn == 2


def test_controller_defensive_tally_decoders_and_validation():
    node = SimpleNamespace(
        node_id="fallback",
        content=SimpleNamespace(model_dump=lambda **kwargs: {}),
    )
    assert controller._node_label(node) == "fallback"
    assert controller._evidence_rows(None) == ()
    assert controller._evidence_rows([None, {"turn_id": 1, "quote": "yes"}]) == (
        EvidenceQuote(1, "yes"),
    )
    row = SimpleNamespace(
        reference_node_id="fallback",
        state="invalid",
        evidence=[],
        student_declined=False,
        times_asked=0,
        last_asked_turn=None,
        question="",
        asked_turn=None,
        answered_turn=None,
    )
    assert (
        controller._build_tally_state(SimpleNamespace(nodes=[node]), [row])[0].status == "missing"
    )
    assert controller._valid_update_evidence(TallyUpdate("fallback", "missing"), [])
    assert not controller._valid_update_evidence(TallyUpdate("fallback", "understood"), [])


@pytest.mark.asyncio
async def test_advancing_target_closes_previous_question(monkeypatch):
    previous = SimpleNamespace(
        reference_node_id="old",
        state="asked_waiting",
        question="Old question?",
        asked_turn=1,
        answered_turn=None,
        evidence=[],
        student_declined=False,
        times_asked=1,
        last_asked_turn=1,
    )
    db = _DB([previous])

    async def evaluate(**kwargs):
        return _ask()

    monkeypatch.setattr(controller, "evaluate_and_ask", evaluate)
    await controller.plan_next_question(
        db,
        course_id=11,
        attempt_id=2,
        session_id=3,
        problem=_problem(),
        transcript=[("student", "x")],
        turn_index=2,
    )
    assert previous.state == "asked_waiting"
    assert previous.answered_turn == 2


@pytest.mark.asyncio
async def test_covered_topics_snapshot_includes_node_understood_this_turn(monkeypatch):
    """A node pushed to ``understood`` this turn appears in the covered snapshot
    with its human label — on the done turn too, so the last topic still
    celebrates before the report."""
    row = SimpleNamespace(
        reference_node_id="def_x",
        state="tentative",
        evidence=[],
        student_declined=False,
        times_asked=1,
        last_asked_turn=1,
        question="Prior question?",
        asked_turn=1,
        answered_turn=2,
    )
    db = _DB([row])

    async def evaluate(**kwargs):
        return UnifiedQuestionResult(
            (TallyUpdate("def_x", "understood", EvidenceQuote(0, "x matters"), False),),
            "done",
            None,
            None,
            None,
        )

    monkeypatch.setattr(controller, "evaluate_and_ask", evaluate)
    result = await controller.plan_next_question(
        db,
        course_id=11,
        attempt_id=2,
        session_id=3,
        problem=_problem(),
        transcript=[("student", "x matters")],
        turn_index=0,
    )
    assert result.action == "done"
    assert result.covered_topics == (controller.CoveredTopic("def_x", "x"),)


@pytest.mark.asyncio
async def test_covered_topics_excludes_non_understood_nodes(monkeypatch):
    """A node that is only ``tentative`` (or ``missing``) is never celebrated."""
    db = _DB([])

    async def evaluate(**kwargs):
        return _ask(TallyUpdate("def_x", "tentative", EvidenceQuote(0, "x matters"), False))

    monkeypatch.setattr(controller, "evaluate_and_ask", evaluate)
    result = await controller.plan_next_question(
        db,
        course_id=11,
        attempt_id=2,
        session_id=3,
        problem=_problem(),
        transcript=[("student", "x matters")],
        turn_index=0,
    )
    assert result.action == "ask"
    assert result.covered_topics == ()


@pytest.mark.asyncio
async def test_covered_topics_is_a_cumulative_snapshot_not_just_this_turn(monkeypatch):
    """A node already ``understood`` from a prior turn stays in the snapshot even
    with no new update, so the backend sends the full covered set each turn and
    the UI diffs it."""
    prior = SimpleNamespace(
        reference_node_id="def_x",
        state="understood",
        evidence=[{"turn_id": 0, "quote": "x matters"}],
        student_declined=False,
        times_asked=1,
        last_asked_turn=1,
        question="Prior question?",
        asked_turn=1,
        answered_turn=2,
    )
    db = _DB([prior])

    async def evaluate(**kwargs):
        return _ask()

    monkeypatch.setattr(controller, "evaluate_and_ask", evaluate)
    result = await controller.plan_next_question(
        db,
        course_id=11,
        attempt_id=2,
        session_id=3,
        problem=_problem(),
        transcript=[("student", "x")],
        turn_index=2,
    )
    assert controller.CoveredTopic("def_x", "x") in result.covered_topics
