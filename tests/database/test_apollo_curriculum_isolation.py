"""The §8A.6 course-isolation contract for the DB-resolved curriculum (WU-3D Task 8).

Two independent courses are built with the SAME physics topic ("bernoulli") via
DATA INSERTS ONLY (no per-course code). A student in course X is offered ONLY
course X's concepts and problems; every curriculum read filters by
``search_space_id`` (concept resolution is the only entry point; problem and
misconception reads inherit scope through ``concept_id`` FKs).

Criteria: #3 (two courses same physics, student sees only own), #4 (session_init
populates concept_id), #5 (new course wired with zero code), #6 (every curriculum
read filters by search_space_id). Runs on real Postgres; the LLM hop is mocked.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from sqlalchemy import select

from apollo.conftest import TEST_USER_ID, TEST_USER_ID_2
from apollo.hoot_bridge.session_init import init_session_from_hoot
from apollo.overseer.problem_selector import list_problems_for_concept
from apollo.persistence.models import TutoringSession
from apollo.subjects.curriculum_db import list_course_concepts
from apollo.subjects.tests._curriculum_fixtures import (
    load_bernoulli_problem_payloads,
    seed_course,
)

pytestmark = pytest.mark.integration

_ALL = load_bernoulli_problem_payloads()
# Two DIFFERENT problem subsets so the courses' problem_codes are distinct,
# even though both teach "bernoulli".
_X_PROBLEMS = _ALL[:2]
_Y_PROBLEMS = _ALL[2:4]


async def _seed_two_courses(db):
    """Course X and course Y, each: subject -> concept 'bernoulli' -> 2 problems.
    Data-only; no per-course code path. Returns the two (sid, cid, codes) tuples."""
    x = await seed_course(
        db,
        subject_slug="physics_x",
        concept_slug="bernoulli",
        problems=_X_PROBLEMS,
    )
    y = await seed_course(
        db,
        subject_slug="physics_y",
        concept_slug="bernoulli",
        problems=_Y_PROBLEMS,
    )
    return x, y


async def test_student_in_course_x_offered_only_x_concepts(db_session):
    """T8.1 (criterion #3) — list_course_concepts(X) returns only X's concept_id;
    Y's is absent. Symmetric for Y."""
    (sid_x, cid_x, _cx), (sid_y, cid_y, _cy) = await _seed_two_courses(db_session)

    x_concepts = {
        c.concept_id for c in await list_course_concepts(db_session, search_space_id=sid_x)
    }
    y_concepts = {
        c.concept_id for c in await list_course_concepts(db_session, search_space_id=sid_y)
    }

    assert x_concepts == {cid_x}
    assert y_concepts == {cid_y}
    assert cid_y not in x_concepts
    assert cid_x not in y_concepts


async def test_problems_scoped_to_course_concept(db_session):
    """T8.2 (criterion #3/#6) — list_problems_for_concept(X_cid) returns only X's
    problem_codes; none of Y's."""
    (sid_x, cid_x, codes_x), (sid_y, cid_y, codes_y) = await _seed_two_courses(db_session)

    x_problem_codes = {
        p.id
        for p in await list_problems_for_concept(
            db_session, concept_id=cid_x, search_space_id=sid_x
        )
    }
    assert x_problem_codes == set(codes_x)
    assert x_problem_codes.isdisjoint(set(codes_y))


async def test_session_init_for_course_x_picks_x_problem(db_session):
    """T8.3 (criterion #3/#4) — init for course X creates a session whose
    concept_id == X's concept and whose problem is one of X's problems."""
    (sid_x, cid_x, codes_x), _y = await _seed_two_courses(db_session)

    with patch("apollo.hoot_bridge.session_init.infer_concept_id", return_value=cid_x):
        result = await init_session_from_hoot(
            db=db_session,
            user_id=TEST_USER_ID,
            search_space_id=sid_x,
            hoot_transcript="bernoulli",
            difficulty="intro",
        )

    sess = (
        await db_session.execute(
            select(TutoringSession).where(TutoringSession.id == result["session_id"])
        )
    ).scalar_one()
    assert sess.concept_id == cid_x
    assert result["problem"]["id"] in set(codes_x)


async def test_new_course_wired_with_zero_code(db_session):
    """T8.4 (criterion #5) — course Y is added via INSERTs only and a full
    init_session_from_hoot(search_space_id=Y) succeeds end-to-end (LLM mocked).
    The same code that serves X serves Y — no code path references Y by name."""
    _x, (sid_y, cid_y, codes_y) = await _seed_two_courses(db_session)

    candidates = await list_course_concepts(db_session, search_space_id=sid_y)

    with patch("apollo.hoot_bridge.session_init.infer_concept_id", return_value=cid_y) as spy:
        result = await init_session_from_hoot(
            db=db_session,
            user_id=TEST_USER_ID_2,
            search_space_id=sid_y,
            hoot_transcript="bernoulli",
            difficulty="intro",
        )

    # The candidates the (course-agnostic) code passed to the LLM were Y's only.
    assert spy.call_args.kwargs["candidates"] == candidates
    assert [c.concept_id for c in spy.call_args.kwargs["candidates"]] == [cid_y]
    sess = (
        await db_session.execute(
            select(TutoringSession).where(TutoringSession.id == result["session_id"])
        )
    ).scalar_one()
    assert sess.concept_id == cid_y
    assert result["problem"]["id"] in set(codes_y)


async def test_every_curriculum_read_filters_by_search_space(db_session):
    """T8.5 (criterion #6) — the concept sets for X and Y are disjoint and a
    course-Y concept is never returned for a course-X query. Documents that the
    only curriculum entry point (concept resolution) is course-scoped."""
    (sid_x, cid_x, _cx), (sid_y, cid_y, _cy) = await _seed_two_courses(db_session)

    x_ids = {c.concept_id for c in await list_course_concepts(db_session, search_space_id=sid_x)}
    y_ids = {c.concept_id for c in await list_course_concepts(db_session, search_space_id=sid_y)}

    assert x_ids.isdisjoint(y_ids)
    assert cid_y not in x_ids
    assert cid_x not in y_ids
