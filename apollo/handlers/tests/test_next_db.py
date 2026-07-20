"""Real-PG cutover tests for handle_next (WU-3D Task 5).

`handle_next` now selects the next problem by ``sess.concept_id`` from the DB
(no legacy cluster map). These isolate the concept-resolution path; the grading
machinery is untouched and not re-tested here.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from apollo.conftest import TEST_USER_ID
from apollo.errors import PoolExhaustedError
from apollo.handlers.next import handle_next
from apollo.persistence.models import (
    TutoringSession,
    ProblemAttempt,
    SessionPhase,
    SessionStatus,
)
from apollo.subjects.tests._curriculum_fixtures import (
    load_bernoulli_problem_payloads,
    seed_course,
)

pytestmark = pytest.mark.integration

_INTRO = [p for p in load_bernoulli_problem_payloads() if p["difficulty"] == "intro"]


async def _seed_report_session(db, *, search_space_id, concept_id, current_problem_id):
    """A REPORT-phase session with one graded attempt on current_problem_id."""
    sess = TutoringSession(
        user_id=TEST_USER_ID,
        search_space_id=search_space_id,
        concept_id=concept_id,
        status=SessionStatus.active.value,
        phase=SessionPhase.REPORT.value,
        current_problem_id=current_problem_id,
    )
    db.add(sess)
    await db.flush()
    db.add(
        ProblemAttempt(
            session_id=sess.id,
            problem_id=current_problem_id,
            difficulty="intro",
            result="graded",
        )
    )
    await db.flush()
    return sess.id


async def test_next_selects_problem_by_session_concept_id(db_session):
    """T5.1 — handle_next advances to a problem belonging to the session's
    concept_id (resolved from the DB, not a cluster map)."""
    sid, cid, codes = await seed_course(
        db_session,
        subject_slug="fluid_mechanics",
        concept_slug="bernoulli_principle",
        problems=_INTRO,
    )
    first_code = codes[0]
    session_id = await _seed_report_session(
        db_session,
        search_space_id=sid,
        concept_id=cid,
        current_problem_id=first_code,
    )

    result = await handle_next(db=db_session, session_id=session_id, difficulty="intro")

    assert result["problem"]["id"] in set(codes)
    assert result["problem"]["id"] != first_code  # excludes the attempted one
    sess = (
        await db_session.execute(select(TutoringSession).where(TutoringSession.id == session_id))
    ).scalar_one()
    assert sess.current_problem_id == result["problem"]["id"]
    assert sess.phase == SessionPhase.TEACHING.value


async def test_next_pool_exhausted_uses_concept_id(db_session, monkeypatch):
    """T5.2 — when select_problem raises PoolExhaustedError it propagates
    (unchanged 409 contract)."""
    sid, cid, codes = await seed_course(
        db_session,
        subject_slug="fluid_mechanics",
        concept_slug="bernoulli_principle",
        problems=_INTRO,
    )
    session_id = await _seed_report_session(
        db_session,
        search_space_id=sid,
        concept_id=cid,
        current_problem_id=codes[0],
    )

    # WU-6A3: the route now calls select_problem_personalized (flag-OFF delegates
    # byte-identically to select_problem); stub that entry point. Its signature adds
    # user_id + search_space_id, so the stub must accept them.
    async def _boom(db, *, user_id, search_space_id, concept_id, difficulty, attempted_ids):
        raise PoolExhaustedError(concept_cluster_id=str(concept_id), difficulty=difficulty)

    monkeypatch.setattr("apollo.handlers.next.select_problem_personalized", _boom)

    with pytest.raises(PoolExhaustedError) as exc:
        await handle_next(db=db_session, session_id=session_id, difficulty="hard")
    assert exc.value.concept_cluster_id == str(cid)
