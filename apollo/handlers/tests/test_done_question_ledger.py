"""The Done path reads the question ledger once and feeds BOTH consumers.

2026-08-07 bimodal-fix wiring in ``apollo/handlers/done.py``:

* **P1.2b** — ``asked_node_ids`` (the reference nodes with a
  ``QuestionOpportunity`` row this attempt) goes to ``compute_topic_score`` so
  graded nodes the tutor never raised leave the denominator.
* **P1.3** — ``tally_context`` (per-node live state + the student's verbatim
  answer quote) goes to the adjudicator, which today re-judges the transcript
  with no knowledge of what the questioning engine already concluded (defect
  U1: identical live tallies produced F(0) and A+(100)).

ONE query serves both. It owns its failure domain: a ledger read that raises
degrades to ``None``/``None``, which reproduces the pre-fix grade exactly —
grading must never break because a telemetry-adjacent read failed.

Unit harness = ``_done_fixtures._old_path_patches`` (no Docker); the real query
is exercised against SQLite.
"""

from __future__ import annotations

from contextlib import ExitStack
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from apollo.conftest import TEST_SPACE_ID, TEST_USER_ID
from apollo.handlers.done import _question_ledger, _tally_context
from apollo.handlers.tests._done_fixtures import _old_path_patches
from apollo.persistence.models import (
    ProblemAttempt,
    QuestionOpportunity,
    SessionPhase,
    SessionStatus,
    TutoringSession,
)
from database.models import Base

pytestmark = pytest.mark.unit


class _Row:
    """Minimal stand-in for a ``QuestionOpportunity`` ORM row."""

    def __init__(
        self,
        node_id: str,
        *,
        state: str = "understood",
        times_asked: int = 1,
        evidence: Any = None,
    ) -> None:
        self.reference_node_id = node_id
        self.state = state
        self.times_asked = times_asked
        self.evidence = evidence


def _drop(patches, *attributes):
    return [p for p in patches if getattr(p, "attribute", None) not in attributes]


async def _run(*, ledger: object = None):
    """Drive ``handle_done`` with a stubbed ledger read. ``ledger`` is the
    return value of ``_question_ledger`` (``None`` models a failed read)."""
    db, sess, attempt, patches = _old_path_patches()
    patches = _drop(patches, "_question_ledger")
    patches.append(
        patch("apollo.handlers.done._question_ledger", new=AsyncMock(return_value=ledger))
    )

    from apollo.handlers.done import handle_done

    started: dict[str, Any] = {}
    with ExitStack() as stack:
        for p in patches:
            started[getattr(p, "attribute", repr(p))] = stack.enter_context(p)
        await handle_done(db=db, neo=MagicMock(), session_id=11)
    return started


# --- pure helpers ----------------------------------------------------------


def test_tally_context_shape_is_the_pinned_cross_slice_contract():
    rows = [
        _Row(
            "q1",
            state="understood",
            times_asked=2,
            evidence=[
                {"turn_id": 0, "quote": "early words"},
                {"turn_id": 4, "quote": "best words"},
            ],
        ),
        _Row("q2", state="missing", times_asked=0, evidence=[]),
    ]

    assert _tally_context(rows) == [
        {
            "node_id": "q1",
            "state": "understood",
            "times_asked": 2,
            # The LATEST quote — the student's most recent demonstration.
            "student_quote": "best words",
        },
        {"node_id": "q2", "state": "missing", "times_asked": 0, "student_quote": None},
    ]


@pytest.mark.parametrize(
    "evidence",
    [
        None,
        [],
        "not a list",
        [{"turn_id": 1}],
        [{"turn_id": 1, "quote": ""}],
        [{"turn_id": 1, "quote": 7}],
        ["not a dict"],
    ],
)
def test_tally_context_quote_is_none_for_unusable_evidence(evidence):
    assert _tally_context([_Row("q1", evidence=evidence)])[0]["student_quote"] is None


def test_tally_context_falls_back_to_the_last_usable_quote():
    rows = [_Row("q1", evidence=[{"turn_id": 0, "quote": "good"}, {"turn_id": 1, "quote": ""}])]
    assert _tally_context(rows)[0]["student_quote"] == "good"


def test_tally_context_coerces_a_null_times_asked():
    assert _tally_context([_Row("q1", times_asked=None)])[0]["times_asked"] == 0


def test_tally_context_of_an_empty_ledger_is_empty():
    assert _tally_context([]) == []


# --- handle_done wiring ----------------------------------------------------


async def test_ledger_feeds_both_the_adjudicator_and_the_scorer():
    started = await _run(ledger=(_Row("q1", times_asked=2), _Row("q2", state="missing")))

    coverage_kwargs = started["compute_transcript_coverage_with_spans"].await_args.kwargs
    assert coverage_kwargs["tally_context"] == [
        {"node_id": "q1", "state": "understood", "times_asked": 2, "student_quote": None},
        {"node_id": "q2", "state": "missing", "times_asked": 1, "student_quote": None},
    ]
    score_kwargs = started["compute_topic_score"].call_args.kwargs
    assert score_kwargs["asked_node_ids"] == frozenset({"q1", "q2"})


async def test_empty_ledger_passes_an_empty_probe_set_not_none():
    """An attempt whose questioning loop engaged nothing is a real signal (the
    auto-done / restart-orphan pathologies) — it must reach the scorer as an
    empty set, which its own degenerate-case guard then handles."""
    started = await _run(ledger=())

    assert started["compute_topic_score"].call_args.kwargs["asked_node_ids"] == frozenset()
    assert (
        started["compute_transcript_coverage_with_spans"].await_args.kwargs["tally_context"] == []
    )


async def test_failed_ledger_read_degrades_to_the_pre_fix_grade():
    started = await _run(ledger=None)

    assert started["compute_topic_score"].call_args.kwargs["asked_node_ids"] is None
    assert (
        started["compute_transcript_coverage_with_spans"].await_args.kwargs["tally_context"] is None
    )


# --- the real query --------------------------------------------------------


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        execution_options={"schema_translate_map": {"app": None, "internal": None}},
    )
    tables = [
        TutoringSession.__table__,
        ProblemAttempt.__table__,
        QuestionOpportunity.__table__,
    ]
    async with engine.begin() as conn:
        await conn.run_sync(lambda sc: Base.metadata.create_all(sc, tables=tables))
    Session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with Session() as s:
        yield s
    await engine.dispose()


@pytest.mark.asyncio
async def test_question_ledger_reads_this_attempt_only_in_stable_order(db: AsyncSession):
    sess = TutoringSession(
        user_id=TEST_USER_ID,
        search_space_id=TEST_SPACE_ID,
        concept_id=1,
        status=SessionStatus.active.value,
        phase=SessionPhase.TEACHING.value,
        current_problem_id=1,
    )
    db.add(sess)
    await db.flush()
    attempts = []
    for _ in range(2):
        attempt = ProblemAttempt(
            session_id=sess.id,
            problem_id=1,
            difficulty="intro",
            user_id=sess.user_id,
            course_id=sess.course_id,
        )
        db.add(attempt)
        await db.flush()
        attempts.append(attempt)
    mine, other = attempts
    for node_id in ("q2_second", "q1_first"):
        db.add(
            QuestionOpportunity(
                course_id=sess.course_id,
                session_id=sess.id,
                attempt_id=mine.id,
                reference_node_id=node_id,
                state="understood",
                question="?",
                times_asked=1,
            )
        )
    db.add(
        QuestionOpportunity(
            course_id=sess.course_id,
            session_id=sess.id,
            attempt_id=other.id,
            reference_node_id="q_other",
            state="missing",
            question="?",
            times_asked=1,
        )
    )
    await db.commit()

    rows = await _question_ledger(db, attempt_id=int(mine.id))

    assert rows is not None
    # Insertion order (id), never node-id order — the tally context the
    # adjudicator sees must be reproducible.
    assert [row.reference_node_id for row in rows] == ["q2_second", "q1_first"]


@pytest.mark.asyncio
async def test_question_ledger_soft_fails_to_none(caplog):
    broken = MagicMock()
    broken.execute = AsyncMock(side_effect=RuntimeError("connection gone"))

    with caplog.at_level("ERROR"):
        assert await _question_ledger(broken, attempt_id=99) is None

    assert "apollo_question_ledger_fetch_failed" in caplog.text
