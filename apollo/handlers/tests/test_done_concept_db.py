"""Real-PG cutover test for handle_done problem resolution (WU-3D Task 5).

`handle_done` now resolves the current problem from the DB via
``_find_problem(db, sess.concept_id, sess.current_problem_id)`` and builds the
reference graph from that DB problem's payload. The grading collaborators
(coverage / rubric / diagnostic / xp / retention) are mocked deterministically;
Neo4j is mocked. This isolates the concept-resolution cutover.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from apollo.conftest import TEST_USER_ID
from apollo.handlers.done import _find_problem, handle_done
from apollo.ontology import KGGraph
from apollo.persistence.models import (
    ApolloSession,
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


async def test_done_resolves_problem_from_db_concept_id(db_session):
    """T5.5 — handle_done builds the reference graph from the DB problem and
    returns a rubric. The reference graph is derived from the DB payload (its
    nodes are non-empty), proving the §6 grading core reads the DB problem."""
    sid, cid, codes = await seed_course(
        db_session,
        subject_slug="fluid_mechanics",
        concept_slug="bernoulli_principle",
        problems=_INTRO,
    )
    current_code = codes[0]
    sess = ApolloSession(
        user_id=TEST_USER_ID,
        search_space_id=sid,
        concept_id=cid,
        status=SessionStatus.active.value,
        phase=SessionPhase.SOLVING.value,
        current_problem_id=current_code,
    )
    db_session.add(sess)
    await db_session.flush()
    attempt = ProblemAttempt(session_id=sess.id, problem_id=current_code, difficulty="intro")
    db_session.add(attempt)
    await db_session.flush()

    envelope = MagicMock(
        xp_earned=10,
        xp_before=0,
        xp_after=10,
        level_before=1,
        level_after=1,
        level_up=False,
        title_after="Novice",
        level_progress_pct=0.1,
        xp_to_next_level=90,
    )

    captured = {}

    async def _coverage(student, reference):
        captured["reference_nodes"] = list(reference.nodes)
        return {}

    with (
        patch("apollo.handlers.done.KGStore.read_graph", new=AsyncMock(return_value=KGGraph())),
        patch("apollo.handlers.done.KGStore.freeze", new=AsyncMock()),
        patch("apollo.handlers.done.KGStore.stamp_graded_at", new=AsyncMock()),
        patch("apollo.handlers.done.compute_coverage", new=AsyncMock(side_effect=_coverage)),
        patch("apollo.handlers.done._attempt_misconception_scores", new=AsyncMock(return_value={})),
        patch("apollo.handlers.done.compute_rubric", return_value={"overall": {"score": 0.5}}),
        patch("apollo.handlers.done.generate_diagnostic", return_value="narrative"),
        patch("apollo.handlers.done.has_prior_graded_attempt", new=AsyncMock(return_value=False)),
        patch("apollo.handlers.done.compute_xp_earned", return_value=10),
        patch(
            "apollo.handlers.done.apply_xp",
            new=AsyncMock(return_value={"xp_before": 0, "xp_after": 10}),
        ),
        patch("apollo.handlers.done.compute_progress_envelope", return_value=envelope),
    ):
        out = await handle_done(db=db_session, neo=MagicMock(), session_id=sess.id)

    assert out["rubric"] == {"overall": {"score": 0.5}}
    # The reference graph passed to coverage came from the DB problem payload.
    assert captured["reference_nodes"]


async def test_done_find_problem_raises_when_code_absent(db_session):
    """T5.x — _find_problem raises RuntimeError when the problem_code has no DB
    row for the concept (the NO-FALLBACK not-found branch)."""
    sid, cid, _codes = await seed_course(
        db_session,
        subject_slug="fluid_mechanics",
        concept_slug="bernoulli_principle",
        problems=_INTRO,
    )
    with pytest.raises(RuntimeError):
        await _find_problem(db_session, cid, "nonexistent_problem_code")
