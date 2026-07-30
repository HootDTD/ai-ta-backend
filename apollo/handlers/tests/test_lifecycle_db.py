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
from apollo.hoot_bridge.reference_answer import ASIDE_MESSAGE_INTENT_TAG
from apollo.ontology import KGGraph
from apollo.persistence.models import (
    ProblemAttempt,
    SessionPhase,
    SessionStatus,
    TutoringMessage,
    TutoringSession,
)
from apollo.subjects.tests._curriculum_fixtures import (
    load_bernoulli_problem_payloads,
    problem_database_id,
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
    current_problem_id = await problem_database_id(
        db_session, concept_id=cid, problem_code=current_code
    )
    sess = TutoringSession(
        user_id=TEST_USER_ID,
        search_space_id=sid,
        concept_id=cid,
        status=SessionStatus.active.value,
        phase=SessionPhase.TEACHING.value,
        current_problem_id=current_problem_id,
    )
    db_session.add(sess)
    await db_session.flush()
    db_session.add(
        ProblemAttempt(
            session_id=sess.id,
            problem_id=current_problem_id,
            difficulty="intro",
            user_id=sess.user_id,
            course_id=sess.course_id,
        )
    )
    await db_session.flush()

    store = MagicMock()
    store.read_graph = AsyncMock(return_value=KGGraph())
    with patch("apollo.handlers.lifecycle.KGStore", return_value=store):
        out = await handle_get_session(db=db_session, neo=MagicMock(), session_id=sess.id)

    assert "concept_cluster_id" not in out
    assert out["concept_id"] == cid
    assert out["problem"] is not None
    assert out["problem"]["id"] == current_code


@pytest.mark.parametrize(
    ("interaction4", "concepts_allowlist", "expected"),
    [
        ("true", "bernoulli_principle", True),
        ("true", "ethics", False),
        ("false", "bernoulli_principle", False),
        ("true", "", True),  # empty allowlist = unrestricted
    ],
)
async def test_get_session_ask_hoot_available_gates_on_flag_and_concept(
    db_session, monkeypatch, interaction4, concepts_allowlist, expected
):
    """ask_hoot_available mirrors the chat-handler aside gate (INTERACTION4 +
    concept allowlist) so the UI only renders the Ask Hoot button where the
    lane can actually run."""
    monkeypatch.setenv("INTERACTION4", interaction4)
    monkeypatch.setenv("INTERACTION_CONCEPTS", concepts_allowlist)
    sid, cid, codes = await seed_course(
        db_session,
        subject_slug="fluid_mechanics",
        concept_slug="bernoulli_principle",
        problems=_INTRO,
    )
    current_problem_id = await problem_database_id(
        db_session, concept_id=cid, problem_code=codes[0]
    )
    sess = TutoringSession(
        user_id=TEST_USER_ID,
        search_space_id=sid,
        concept_id=cid,
        status=SessionStatus.active.value,
        phase=SessionPhase.TEACHING.value,
        current_problem_id=current_problem_id,
    )
    db_session.add(sess)
    await db_session.flush()

    store = MagicMock()
    store.read_graph = AsyncMock(return_value=KGGraph())
    with patch("apollo.handlers.lifecycle.KGStore", return_value=store):
        out = await handle_get_session(db=db_session, neo=MagicMock(), session_id=sess.id)

    assert out["ask_hoot_available"] is expected


async def test_get_session_replays_aside_intent_and_citations(db_session):
    """Aside rows come back with the WIRE intent tag ("reference_aside", the
    value the student UI keys on — not the stored "reference_question_aside")
    plus the aside payload reconstructed from row metadata; pre-metadata rows
    keep the intent but omit the payload; internal intents on other rows are
    not exposed."""
    sid, cid, codes = await seed_course(
        db_session,
        subject_slug="fluid_mechanics",
        concept_slug="bernoulli_principle",
        problems=_INTRO,
    )
    current_problem_id = await problem_database_id(
        db_session, concept_id=cid, problem_code=codes[0]
    )
    sess = TutoringSession(
        user_id=TEST_USER_ID,
        search_space_id=sid,
        concept_id=cid,
        status=SessionStatus.active.value,
        phase=SessionPhase.TEACHING.value,
        current_problem_id=current_problem_id,
    )
    db_session.add(sess)
    await db_session.flush()
    attempt = ProblemAttempt(
        session_id=sess.id,
        problem_id=current_problem_id,
        difficulty="intro",
        user_id=sess.user_id,
        course_id=sess.course_id,
    )
    db_session.add(attempt)
    await db_session.flush()

    citations = [{"label": "[Week 2, p. 4]", "page": 4}]
    rows = [
        ("student", "What is Bernoulli's principle?", 0, None, None),
        ("apollo", "Bernoulli's principle relates pressure and speed.", 1,
         ASIDE_MESSAGE_INTENT_TAG, {"aside": {"citations": citations, "in_scope": True}}),
        ("apollo", "Okay, back to your teaching.", 2, None, None),
        # A row persisted before the metadata existed: intent but no payload.
        ("apollo", "An older aside answer.", 3, ASIDE_MESSAGE_INTENT_TAG, None),
    ]
    for role, content, idx, intent, meta in rows:
        db_session.add(
            TutoringMessage(
                session_id=sess.id,
                course_id=sess.course_id,
                attempt_id=attempt.id,
                role=role,
                content=content,
                turn_index=idx,
                intent=intent,
                message_metadata=meta or {},
            )
        )
    await db_session.flush()

    store = MagicMock()
    store.read_graph = AsyncMock(return_value=KGGraph())
    with patch("apollo.handlers.lifecycle.KGStore", return_value=store):
        out = await handle_get_session(db=db_session, neo=MagicMock(), session_id=sess.id)

    messages = out["messages"]
    assert [m["turn_index"] for m in messages] == [0, 1, 2, 3]
    assert "intent" not in messages[0] and "intent" not in messages[2]
    assert messages[1]["intent"] == "reference_aside"
    assert messages[1]["aside"] == {
        "text": "Bernoulli's principle relates pressure and speed.",
        "citations": citations,
        "in_scope": True,
    }
    assert messages[3]["intent"] == "reference_aside"
    assert "aside" not in messages[3]


async def test_get_session_ask_hoot_unavailable_without_problem(db_session, monkeypatch):
    """No current problem -> no aside lane -> button hidden."""
    monkeypatch.setenv("INTERACTION4", "true")
    monkeypatch.setenv("INTERACTION_CONCEPTS", "")
    sid, cid, _codes = await seed_course(
        db_session,
        subject_slug="fluid_mechanics",
        concept_slug="bernoulli_principle",
        problems=_INTRO,
    )
    sess = TutoringSession(
        user_id=TEST_USER_ID,
        search_space_id=sid,
        concept_id=cid,
        status=SessionStatus.active.value,
        phase=SessionPhase.TEACHING.value,
    )
    db_session.add(sess)
    await db_session.flush()

    out = await handle_get_session(db=db_session, neo=MagicMock(), session_id=sess.id)

    assert out["ask_hoot_available"] is False
