"""Real-PG tests for the DB-resolved Hoot->Apollo session init (WU-3D Task 4).

`init_session_from_hoot` now builds the candidate concept set from the course's
``apollo_concepts`` rows (scoped via ``search_space_id``), infers a ``concept_id``
via the LLM hop (mocked), selects a problem from the DB, and populates
the tutoring activity's ``concept_id`` without writing ``concept_cluster_id``.

The legacy ``test_session_init.py`` is module-skipped (references the deleted
``KGEntry``); these are the new behavioral tests on real PG.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from sqlalchemy import select, text

from apollo.conftest import TEST_USER_ID
from apollo.hoot_bridge.session_init import init_session_from_hoot
from apollo.overseer.problem_selector import list_problems_for_concept
from apollo.persistence.models import ProblemAttempt, SessionStatus, TutoringSession
from apollo.subjects.curriculum_db import list_course_concepts
from apollo.subjects.tests._curriculum_fixtures import (
    load_bernoulli_problem_payloads,
    problem_database_id,
    seed_course,
)

pytestmark = pytest.mark.integration

_INTRO = [p for p in load_bernoulli_problem_payloads() if p["difficulty"] == "intro"]


async def _seed_bernoulli_course(db, *, search_space_id=None):
    return await seed_course(
        db,
        subject_slug="fluid_mechanics",
        concept_slug="bernoulli_principle",
        problems=_INTRO,
        search_space_id=search_space_id,
    )


async def test_init_session_populates_concept_id(db_session):
    """T4.1 (criterion #4) — the persisted session's concept_id == inferred id."""
    sid, cid, _codes = await _seed_bernoulli_course(db_session)

    with patch("apollo.hoot_bridge.session_init.infer_concept_id", return_value=cid):
        result = await init_session_from_hoot(
            db=db_session,
            user_id=TEST_USER_ID,
            search_space_id=sid,
            hoot_transcript="bernoulli stuff",
            difficulty="intro",
        )

    sess = (
        await db_session.execute(
            select(TutoringSession).where(TutoringSession.id == result["session_id"])
        )
    ).scalar_one()
    assert sess.concept_id == cid


async def test_init_session_does_not_write_concept_cluster_id(db_session):
    """T4.2 (criterion #4) — the legacy concept_cluster_id column was dropped in
    migration 027 and the ORM no longer carries it, so the cutover cannot write
    it; only concept_id is set. (Pre-027 this asserted ``concept_cluster_id IS
    NULL``; the column no longer exists.)"""
    sid, cid, _codes = await _seed_bernoulli_course(db_session)

    with patch("apollo.hoot_bridge.session_init.infer_concept_id", return_value=cid):
        result = await init_session_from_hoot(
            db=db_session,
            user_id=TEST_USER_ID,
            search_space_id=sid,
            hoot_transcript="t",
            difficulty="intro",
        )

    assert "concept_cluster_id" not in TutoringSession.__table__.columns
    concept_val = (
        await db_session.execute(
            text("SELECT concept_id FROM learning_activities WHERE id = :sid"),
            {"sid": result["session_id"]},
        )
    ).scalar_one()
    assert concept_val == cid


async def test_init_session_creates_first_attempt(db_session):
    """T4.3 — a ProblemAttempt row exists at intro difficulty for the chosen
    problem."""
    sid, cid, _codes = await _seed_bernoulli_course(db_session)

    with patch("apollo.hoot_bridge.session_init.infer_concept_id", return_value=cid):
        result = await init_session_from_hoot(
            db=db_session,
            user_id=TEST_USER_ID,
            search_space_id=sid,
            hoot_transcript="t",
            difficulty="intro",
        )

    attempt = (
        await db_session.execute(
            select(ProblemAttempt).where(ProblemAttempt.session_id == result["session_id"])
        )
    ).scalar_one()
    assert attempt.difficulty == "intro"
    assert attempt.problem_id == await problem_database_id(
        db_session,
        concept_id=cid,
        problem_code=result["problem"]["id"],
    )


async def test_init_session_ends_stale_active_session(db_session):
    """T4.4 — a second init for the same user ends the first session; only the
    second is active."""
    sid, cid, _codes = await _seed_bernoulli_course(db_session)

    with patch("apollo.hoot_bridge.session_init.infer_concept_id", return_value=cid):
        first = await init_session_from_hoot(
            db=db_session,
            user_id=TEST_USER_ID,
            search_space_id=sid,
            hoot_transcript="t1",
            difficulty="intro",
        )
        second = await init_session_from_hoot(
            db=db_session,
            user_id=TEST_USER_ID,
            search_space_id=sid,
            hoot_transcript="t2",
            difficulty="intro",
        )

    first_sess = (
        await db_session.execute(
            select(TutoringSession).where(TutoringSession.id == first["session_id"])
        )
    ).scalar_one()
    second_sess = (
        await db_session.execute(
            select(TutoringSession).where(TutoringSession.id == second["session_id"])
        )
    ).scalar_one()
    assert first_sess.status == SessionStatus.ended.value
    assert second_sess.status == SessionStatus.active.value


async def test_init_session_candidates_passed_to_infer_are_course_scoped(db_session):
    """T4.5 — the candidates threaded to infer_concept_id are exactly this
    course's concepts (list_course_concepts output)."""
    sid, cid, _codes = await _seed_bernoulli_course(db_session)
    expected = await list_course_concepts(db_session, search_space_id=sid)

    with patch("apollo.hoot_bridge.session_init.infer_concept_id", return_value=cid) as spy:
        await init_session_from_hoot(
            db=db_session,
            user_id=TEST_USER_ID,
            search_space_id=sid,
            hoot_transcript="t",
            difficulty="intro",
        )

    _, kwargs = spy.call_args
    assert kwargs["candidates"] == expected
    assert [c.concept_id for c in kwargs["candidates"]] == [cid]


async def test_init_session_rejects_unknown_difficulty(db_session):
    """T4.6 — an unknown difficulty raises ValueError before any LLM call."""
    sid, cid, _codes = await _seed_bernoulli_course(db_session)

    with patch("apollo.hoot_bridge.session_init.infer_concept_id") as spy:
        with pytest.raises(ValueError):
            await init_session_from_hoot(
                db=db_session,
                user_id=TEST_USER_ID,
                search_space_id=sid,
                hoot_transcript="t",
                difficulty="impossible",
            )
    spy.assert_not_called()


async def test_init_session_problem_belongs_to_concept(db_session):
    """T4.x — the chosen problem is one of the concept's DB problems."""
    sid, cid, _codes = await _seed_bernoulli_course(db_session)

    with patch("apollo.hoot_bridge.session_init.infer_concept_id", return_value=cid):
        result = await init_session_from_hoot(
            db=db_session,
            user_id=TEST_USER_ID,
            search_space_id=sid,
            hoot_transcript="t",
            difficulty="intro",
        )

    concept_problem_ids = {
        p.id for p in await list_problems_for_concept(db_session, concept_id=cid)
    }
    assert result["problem"]["id"] in concept_problem_ids
