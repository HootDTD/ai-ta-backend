"""Real-PG cutover test for handle_get_session (WU-3D Task 5).

`handle_get_session` now returns ``concept_id`` (not the dropped
``concept_cluster_id``) and resolves the current problem dict from the DB via
``list_problems_for_concept(db, concept_id=...)``. Neo4j is mocked.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from apollo.conftest import TEST_USER_ID
from apollo.handlers.lifecycle import handle_get_session
from apollo.ontology import KGGraph
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


async def test_get_session_returns_concept_id_and_db_problem(db_session):
    """T5.6 — the response carries concept_id (not concept_cluster_id) and the
    problem dict comes from list_problems_for_concept."""
    sid, cid, codes = await seed_course(
        db_session,
        subject_slug="fluid_mechanics",
        concept_slug="bernoulli_principle",
        problems=_INTRO,
    )
    current_code = codes[0]
    sess = TutoringSession(
        user_id=TEST_USER_ID,
        search_space_id=sid,
        concept_id=cid,
        status=SessionStatus.active.value,
        phase=SessionPhase.TEACHING.value,
        current_problem_id=current_code,
    )
    db_session.add(sess)
    await db_session.flush()
    db_session.add(ProblemAttempt(session_id=sess.id, problem_id=current_code, difficulty="intro"))
    await db_session.flush()

    store = MagicMock()
    store.read_graph = AsyncMock(return_value=KGGraph())
    with patch("apollo.handlers.lifecycle.KGStore", return_value=store):
        out = await handle_get_session(db=db_session, neo=MagicMock(), session_id=sess.id)

    assert "concept_cluster_id" not in out
    assert out["concept_id"] == cid
    assert out["problem"] is not None
    assert out["problem"]["id"] == current_code
