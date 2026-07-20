"""Real-PG cutover tests for handle_chat concept resolution (WU-3D Task 5).

`handle_chat` now resolves the concept via ``load_concept_definition(db,
concept_id=sess.concept_id)`` and finds the current problem via the DB
``_find_problem(db, concept_id, problem_code)``. These tests let those two DB
paths run for real (the cutover) and mock the heavy parser/reply/Neo4j
collaborators around them.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from apollo.conftest import TEST_USER_ID
from apollo.handlers.chat import _find_problem, handle_chat
from apollo.handlers.intent import IntentVerdict
from apollo.knowledge_graph.store import WriteEdgesResult
from apollo.ontology import KGGraph
from apollo.persistence.models import (
    TutoringSession,
    ProblemAttempt,
    SessionPhase,
    SessionStatus,
)
from apollo.smart_questions import QuestionDecision
from apollo.subjects.tests._curriculum_fixtures import (
    load_bernoulli_concept_payloads,
    load_bernoulli_problem_payloads,
    seed_course,
)

pytestmark = pytest.mark.integration

_INTRO = [p for p in load_bernoulli_problem_payloads() if p["difficulty"] == "intro"]


async def _seed_session_with_attempt(db, *, concept_payloads=None):
    sid, cid, codes = await seed_course(
        db,
        subject_slug="fluid_mechanics",
        concept_slug="bernoulli_principle",
        problems=_INTRO,
        concept_payloads=concept_payloads,
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
    db.add(sess)
    await db.flush()
    attempt = ProblemAttempt(session_id=sess.id, problem_id=current_code, difficulty="intro")
    db.add(attempt)
    await db.flush()
    return sess.id, cid, current_code


def _fake_store():
    store = MagicMock()
    store.read_graph = AsyncMock(return_value=KGGraph())
    store.write_nodes = AsyncMock(return_value=0)
    store.write_edges = AsyncMock(return_value=WriteEdgesResult(written=0))
    store.summarize_for_apollo = AsyncMock(return_value="(summary)")
    return store


async def test_chat_loads_concept_definition_from_db(db_session):
    """T5.3 — parse_utterance receives a concept whose parser_prompt_template
    matches the seeded apollo_concepts row (proves DB concept resolution)."""
    payloads = load_bernoulli_concept_payloads()
    session_id, _cid, _code = await _seed_session_with_attempt(
        db_session, concept_payloads=payloads
    )

    store = _fake_store()
    with (
        patch("apollo.handlers.chat.KGStore", return_value=store),
        patch("apollo.handlers.chat.parse_utterance", return_value=([], [])) as parse,
        patch(
            "apollo.handlers.chat.plan_next_question",
            new=AsyncMock(
                return_value=QuestionDecision(action="ask", question="ok", target_node_id="eq.a")
            ),
        ),
        patch(
            "apollo.handlers.chat.classify_intent",
            return_value=IntentVerdict(intent="teaching", confidence=1.0, reason=""),
        ),
        patch(
            "apollo.handlers.chat._unified_questioning_enabled",
            return_value=True,
        ),
    ):
        await handle_chat(db=db_session, neo=MagicMock(), session_id=session_id, message="hi")

    assert parse.called
    concept = parse.call_args.kwargs["concept"]
    assert concept.parser_prompt_template == payloads["parser_prompt_template"]


async def test_chat_find_problem_matches_problem_code(db_session):
    """T5.4 — the unified planner is given the DB problem (proves _find_problem
    reads the DB by concept_id + problem_code)."""
    session_id, _cid, current_code = await _seed_session_with_attempt(db_session)
    expected_text = next(p for p in _INTRO if p["id"] == current_code)["problem_text"]

    store = _fake_store()
    with (
        patch("apollo.handlers.chat.KGStore", return_value=store),
        patch("apollo.handlers.chat.parse_utterance", return_value=([], [])),
        patch(
            "apollo.handlers.chat.plan_next_question",
            new=AsyncMock(
                return_value=QuestionDecision(action="ask", question="ok", target_node_id="eq.a")
            ),
        ) as planner,
        patch(
            "apollo.handlers.chat.classify_intent",
            return_value=IntentVerdict(intent="teaching", confidence=1.0, reason=""),
        ),
        patch(
            "apollo.handlers.chat._unified_questioning_enabled",
            return_value=True,
        ),
    ):
        await handle_chat(db=db_session, neo=MagicMock(), session_id=session_id, message="hi")

    assert planner.await_args.kwargs["problem"].problem_text == expected_text


async def test_chat_find_problem_raises_when_code_absent(db_session):
    """T5.x — _find_problem raises RuntimeError when the problem_code has no DB
    row for the concept (the NO-FALLBACK not-found branch)."""
    session_id, cid, _code = await _seed_session_with_attempt(db_session)
    with pytest.raises(RuntimeError):
        await _find_problem(db_session, cid, "nonexistent_problem_code")
