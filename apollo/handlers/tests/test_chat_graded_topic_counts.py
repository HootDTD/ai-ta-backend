"""Pre-Done coverage meter counts on the chat response (P2.2 contract).

2026-08-07 bimodal-fix P2.2: the student had no way to know how much of the
problem was still unaddressed before clicking Done, so Done was routinely
clicked mid-topic (and the auto-done path grades without consent at all). The
chat response now carries, alongside the existing `covered_topics` snapshot:

* ``graded_topic_total`` — how many reference nodes are of a GRADED type
  (equation / condition / simplification / procedure_step). `definition` and
  `variable_mapping` nodes are never graded, so counting them would show the
  student a denominator that has nothing to do with their grade.
* ``open_graded_topics`` — graded nodes the live tally has NOT marked
  ``understood`` yet. The student-UI shows the count and warns on Done.

Both are derived from data the turn already has (the problem's reference
solution + the covered snapshot the questioning controller returns) — no extra
DB read, no extra LLM call. They are display-only, so the derivation soft-fails
to ``0/0`` rather than breaking a teaching turn.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from apollo.conftest import TEST_SPACE_ID, TEST_USER_ID
from apollo.handlers.chat import _graded_topic_counts
from apollo.handlers.intent import IntentVerdict
from apollo.knowledge_graph.store import WriteEdgesResult
from apollo.ontology import KGGraph
from apollo.persistence.models import (
    ProblemAttempt,
    SessionPhase,
    SessionStatus,
    TutoringMessage,
    TutoringSession,
)
from apollo.smart_questions import QuestionDecision
from apollo.smart_questions.controller import CoveredTopic
from database.models import Base

pytestmark = pytest.mark.unit


def _step(step_id: str, entry_type: str) -> SimpleNamespace:
    return SimpleNamespace(id=step_id, entry_type=entry_type)


def _problem(*steps: SimpleNamespace) -> MagicMock:
    problem = MagicMock(problem_text="teach me")
    problem.reference_solution = list(steps)
    return problem


# --- pure derivation -------------------------------------------------------


def test_counts_only_graded_node_types():
    problem = _problem(
        _step("eq1", "equation"),
        _step("c1", "condition"),
        _step("s1", "simplification"),
        _step("p1", "procedure_step"),
        _step("d1", "definition"),
        _step("v1", "variable_mapping"),
    )

    assert _graded_topic_counts(problem, ()) == (4, 4)


def test_understood_graded_nodes_close_the_open_count():
    problem = _problem(_step("eq1", "equation"), _step("c1", "condition"))
    covered = (CoveredTopic("eq1", "Bernoulli"),)

    assert _graded_topic_counts(problem, covered) == (2, 1)


def test_covered_ungraded_nodes_never_change_either_count():
    """A celebrated `definition` topic must not make the meter read 'done' —
    it was never in the graded denominator."""
    problem = _problem(_step("eq1", "equation"), _step("d1", "definition"))
    covered = (CoveredTopic("d1", "Density"), CoveredTopic("stale_node", "Gone"))

    assert _graded_topic_counts(problem, covered) == (1, 1)


def test_every_graded_node_understood_leaves_zero_open():
    problem = _problem(_step("eq1", "equation"), _step("c1", "condition"))
    covered = (CoveredTopic("eq1", "A"), CoveredTopic("c1", "B"))

    assert _graded_topic_counts(problem, covered) == (2, 0)


def test_problem_with_no_graded_nodes_reports_zero_total():
    assert _graded_topic_counts(_problem(_step("d1", "definition")), ()) == (0, 0)


def test_derivation_soft_fails_to_zero(caplog):
    """Display-only telemetry must never break a teaching turn — a problem
    object whose reference solution cannot be walked degrades to 0/0."""
    broken = SimpleNamespace(id="p_code")  # no reference_solution at all

    with caplog.at_level("WARNING"):
        assert _graded_topic_counts(broken, ()) == (0, 0)

    assert "apollo_graded_topic_counts_failed" in caplog.text


def test_malformed_reference_step_soft_fails_to_zero(caplog):
    malformed = SimpleNamespace(id="p_code", reference_solution=[object()])

    with caplog.at_level("WARNING"):
        assert _graded_topic_counts(malformed, ()) == (0, 0)

    assert "apollo_graded_topic_counts_failed" in caplog.text


def test_malformed_covered_topic_soft_fails_to_zero(caplog):
    """The covered-topics walk is inside the guard too. A `QuestionDecision`
    carrying a topic without `node_id` (test double, rolling-deploy shape
    change, controller regression) must degrade the meter, not 500 the turn."""
    problem = _problem(_step("eq1", "equation"))

    with caplog.at_level("WARNING"):
        assert _graded_topic_counts(problem, (object(),)) == (0, 0)

    assert "apollo_graded_topic_counts_failed" in caplog.text


# --- the chat response -----------------------------------------------------


@pytest_asyncio.fixture
async def db_session_attempt():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        execution_options={"schema_translate_map": {"app": None, "internal": None}},
    )
    tables = [
        TutoringSession.__table__,
        ProblemAttempt.__table__,
        TutoringMessage.__table__,
    ]
    async with engine.begin() as conn:
        await conn.run_sync(lambda sc: Base.metadata.create_all(sc, tables=tables))
    Session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with Session() as s:
        sess = TutoringSession(
            user_id=TEST_USER_ID,
            search_space_id=TEST_SPACE_ID,
            concept_id=1,
            status=SessionStatus.active.value,
            phase=SessionPhase.TEACHING.value,
            current_problem_id=1,
            pending_intent=None,
        )
        s.add(sess)
        await s.commit()
        await s.refresh(sess)
        attempt = ProblemAttempt(
            session_id=sess.id,
            problem_id=1,
            difficulty="intro",
            user_id=sess.user_id,
            course_id=sess.course_id,
        )
        s.add(attempt)
        await s.commit()
        await s.refresh(attempt)
        yield s, sess.id
    await engine.dispose()


def _fake_store():
    store = MagicMock()
    store.read_graph = AsyncMock(return_value=KGGraph())
    store.write_nodes = AsyncMock(return_value=0)
    store.write_edges = AsyncMock(return_value=WriteEdgesResult(written=0))
    return store


def _patches(decision: QuestionDecision):
    problem = _problem(
        _step("eq1", "equation"),
        _step("c1", "condition"),
        _step("d1", "definition"),
    )
    return [
        patch("apollo.handlers.chat.KGStore", return_value=_fake_store()),
        patch("apollo.handlers.chat.parse_utterance", return_value=([], [])),
        patch("apollo.handlers.chat.plan_next_question", new=AsyncMock(return_value=decision)),
        patch(
            "apollo.handlers.chat.classify_intent",
            return_value=IntentVerdict(intent="teaching", confidence=1.0, reason=""),
        ),
        patch("apollo.handlers.chat._find_problem", new=AsyncMock(return_value=problem)),
        patch(
            "apollo.handlers.chat.load_concept_definition", new=AsyncMock(return_value=MagicMock())
        ),
    ]


@pytest.mark.asyncio
async def test_ask_turn_carries_the_meter_counts(db_session_attempt):
    db, session_id = db_session_attempt
    decision = QuestionDecision(
        action="ask",
        question="what next?",
        target_node_id="c1",
        covered_topics=(CoveredTopic("eq1", "Bernoulli"),),
    )
    from apollo.handlers.chat import handle_chat

    ps = _patches(decision)
    with ps[0], ps[1], ps[2], ps[3], ps[4], ps[5]:
        result = await handle_chat(db=db, neo=MagicMock(), session_id=session_id, message="hi")

    assert result["graded_topic_total"] == 2
    assert result["open_graded_topics"] == 1
    # The pre-existing snapshot is untouched.
    assert result["covered_topics"] == [{"node_id": "eq1", "display_name": "Bernoulli"}]


@pytest.mark.asyncio
async def test_auto_done_turn_carries_the_meter_counts_too(db_session_attempt):
    """The auto-done branch returns a different dict — it must not silently
    drop the contract the UI types against."""
    db, session_id = db_session_attempt
    decision = QuestionDecision(action="done", covered_topics=(CoveredTopic("eq1", "Bernoulli"),))
    from apollo.handlers.chat import handle_chat

    ps = _patches(decision)
    done_patch = patch("apollo.handlers.done.handle_done", new=AsyncMock(return_value={}))
    with ps[0], ps[1], ps[2], ps[3], ps[4], ps[5], done_patch:
        result = await handle_chat(db=db, neo=MagicMock(), session_id=session_id, message="done")

    assert result["graded_topic_total"] == 2
    assert result["open_graded_topics"] == 1
