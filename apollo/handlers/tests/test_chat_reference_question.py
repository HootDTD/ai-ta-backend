"""Unit coverage for the INTERACTION4 aside path in the chat handler.

The Hoot bridge is mocked, and persistence is observed through the handler's
explicit-request seam. No database, retrieval service, or network is used.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from apollo.api import ChatRequest
from apollo.handlers.chat import _maybe_execute_reference_aside, handle_chat
from apollo.hoot_bridge.reference_answer import (
    ASIDE_MESSAGE_INTENT_TAG,
    MAX_ASIDES_PER_SESSION,
    MESSAGE_KIND_REFERENCE_ASIDE,
    ReferenceAsideResult,
)
from apollo.ontology import KGGraph

pytestmark = pytest.mark.unit

_QUESTION = "Wait, what is a network effect?"


def test_chat_request_defaults_ask_hoot_to_false():
    assert ChatRequest(message=_QUESTION).ask_hoot is False
    assert ChatRequest(message=_QUESTION, ask_hoot=True).ask_hoot is True


def _chat_context(*, aside_count: int = 0):
    db = MagicMock()
    db.add = MagicMock()
    db.commit = AsyncMock()
    sess = SimpleNamespace(
        id=11,
        course_id=7,
        metadata_={"reference_question_aside_count": aside_count},
    )
    store = MagicMock()
    store.read_graph = AsyncMock(return_value=KGGraph())
    concept = SimpleNamespace(concept_id="network_effects", subject_id="business")
    problem = MagicMock(database_id=42, concept_id="network_effects")
    return db, sess, store, concept, problem


async def _run_ask_hoot(*, db, sess, store, problem):
    return await _maybe_execute_reference_aside(
        db=db,
        sess=sess,
        attempt_id=99,
        message=_QUESTION,
        store=store,
        problem=problem,
        ask_hoot=True,
    )


@pytest.mark.parametrize("allowlist", [None, "other_concept, NETWORK_EFFECTS"])
async def test_reference_question_allowed_returns_aside_and_persona_resume(monkeypatch, allowlist):
    monkeypatch.setenv("INTERACTION4", "1")
    if allowlist is None:
        monkeypatch.delenv("INTERACTION_CONCEPTS", raising=False)
    else:
        monkeypatch.setenv("INTERACTION_CONCEPTS", allowlist)
    db, sess, store, _, problem = _chat_context()
    bridge = AsyncMock(
        return_value=ReferenceAsideResult(
            in_scope=True,
            text="A network effect means a product becomes more valuable as adoption grows.",
            citations=[{"label": "Week 2, p. 4"}],
        )
    )

    with (
        patch("apollo.handlers.chat.answer_reference_question", new=bridge),
        patch("apollo.handlers.chat._next_turn_index", new=AsyncMock(return_value=8)),
    ):
        response = await _run_ask_hoot(
            db=db,
            sess=sess,
            store=store,
            problem=problem,
        )

    assert response == {
        "apollo_reply": "Okay, so how does that fit into what you were teaching me?",
        "kg_entries_added": 0,
        "kg": {"nodes": [], "edges": []},
        "message_kind": MESSAGE_KIND_REFERENCE_ASIDE,
        "aside": {
            "text": "A network effect means a product becomes more valuable as adoption grows.",
            "citations": [{"label": "Week 2, p. 4"}],
            "in_scope": True,
        },
        "intent_executed": {"intent": "reference_question", "aside_count": 1},
    }
    bridge.assert_awaited_once_with(
        db=db,
        course_id=7,
        question=_QUESTION,
        problem=problem,
    )
    assert sess.metadata_["reference_question_aside_count"] == 1

    persisted = [call.args[0] for call in db.add.call_args_list]
    assert [(row.role, row.content, row.turn_index) for row in persisted] == [
        ("student", _QUESTION, 8),
        (
            "apollo",
            "A network effect means a product becomes more valuable as adoption grows.",
            9,
        ),
        (
            "apollo",
            "Okay, so how does that fit into what you were teaching me?",
            10,
        ),
    ]
    assert persisted[1].intent == ASIDE_MESSAGE_INTENT_TAG
    assert persisted[0].intent is None
    assert persisted[2].intent is None
    # The structured payload persists on the aside row so the session
    # snapshot can replay citations after a reload; the flanking rows
    # carry no aside metadata.
    assert persisted[1].message_metadata == {
        "aside": {"citations": [{"label": "Week 2, p. 4"}], "in_scope": True}
    }
    assert persisted[0].message_metadata == {}
    assert persisted[2].message_metadata == {}


async def test_reference_question_nonmatching_concept_never_executes_aside(monkeypatch):
    monkeypatch.setenv("INTERACTION4", "1")
    monkeypatch.setenv("INTERACTION_CONCEPTS", "bernoulli_principle")
    db, sess, store, _, problem = _chat_context()
    bridge = AsyncMock()

    with patch("apollo.handlers.chat.answer_reference_question", new=bridge):
        response = await _run_ask_hoot(
            db=db,
            sess=sess,
            store=store,
            problem=problem,
        )

    assert response is None
    bridge.assert_not_awaited()
    db.add.assert_not_called()
    db.commit.assert_not_awaited()


async def test_reference_question_cap_redirects_without_bridge_call(monkeypatch):
    monkeypatch.setenv("INTERACTION4", "1")
    monkeypatch.delenv("INTERACTION_CONCEPTS", raising=False)
    db, sess, store, _, problem = _chat_context(aside_count=MAX_ASIDES_PER_SESSION)
    bridge = AsyncMock()

    with (
        patch("apollo.handlers.chat.answer_reference_question", new=bridge),
        patch("apollo.handlers.chat._next_turn_index", new=AsyncMock(return_value=12)),
    ):
        response = await _run_ask_hoot(
            db=db,
            sess=sess,
            store=store,
            problem=problem,
        )

    assert response["apollo_reply"] == (
        "I think I've looked enough things up for this session — let's keep teaching so I can "
        "really learn it. What were you saying?"
    )
    assert set(response) == {"apollo_reply", "kg_entries_added", "kg"}
    bridge.assert_not_awaited()
    assert sess.metadata_["reference_question_aside_count"] == MAX_ASIDES_PER_SESSION

    persisted = [call.args[0] for call in db.add.call_args_list]
    assert [row.role for row in persisted] == ["student", "apollo"]
    assert all(row.intent is None for row in persisted)


async def test_reference_question_bridge_failure_apologizes_as_teaching_turn(monkeypatch):
    monkeypatch.setenv("INTERACTION4", "1")
    monkeypatch.delenv("INTERACTION_CONCEPTS", raising=False)
    db, sess, store, _, problem = _chat_context()
    bridge = AsyncMock(side_effect=RuntimeError("retrieval unavailable"))

    with (
        patch("apollo.handlers.chat.answer_reference_question", new=bridge),
        patch("apollo.handlers.chat._next_turn_index", new=AsyncMock(return_value=4)),
    ):
        response = await _run_ask_hoot(
            db=db,
            sess=sess,
            store=store,
            problem=problem,
        )

    assert response == {
        "apollo_reply": (
            "Hmm, I couldn't look that up right now. Let's keep going — what were you saying?"
        ),
        "kg_entries_added": 0,
        "kg": {"nodes": [], "edges": []},
    }
    bridge.assert_awaited_once()
    assert sess.metadata_["reference_question_aside_count"] == 0

    persisted = [call.args[0] for call in db.add.call_args_list]
    assert [(row.role, row.turn_index) for row in persisted] == [
        ("student", 4),
        ("apollo", 5),
    ]
    assert all(row.intent is None for row in persisted)


async def test_ask_hoot_flag_off_returns_normal_teaching_turn(monkeypatch):
    monkeypatch.delenv("INTERACTION4", raising=False)
    monkeypatch.delenv("INTERACTION_CONCEPTS", raising=False)
    db, sess, store, concept, problem = _chat_context()
    sess.concept_id = 23
    sess.current_problem_id = 42
    sess.pending_intent = None
    attempt = SimpleNamespace(id=99)

    session_result = MagicMock()
    session_result.scalar_one.return_value = sess
    attempt_result = MagicMock()
    attempt_result.scalars.return_value.first.return_value = attempt
    db.execute = AsyncMock(side_effect=[session_result, attempt_result])

    bridge = AsyncMock()
    persist_turn = AsyncMock()
    normal_reply = "How does that idea connect to the problem?"
    decision = SimpleNamespace(
        action="ask",
        question=normal_reply,
        covered_topics=[],
        target_node_id=None,
    )

    with (
        patch("apollo.handlers.chat.KGStore", return_value=store),
        patch(
            "apollo.handlers.chat.load_concept_definition",
            new=AsyncMock(return_value=concept),
        ),
        patch("apollo.handlers.chat._find_problem", new=AsyncMock(return_value=problem)),
        patch("apollo.handlers.chat._load_history", new=AsyncMock(return_value=[])),
        patch(
            "apollo.handlers.chat.classify_intent",
            return_value=SimpleNamespace(intent="teaching", confidence=0.99),
        ),
        patch(
            "apollo.handlers.chat._read_graph_or_empty",
            new=AsyncMock(return_value=KGGraph()),
        ),
        patch("apollo.handlers.chat.parse_utterance", return_value=([], [])),
        patch("apollo.handlers.chat._write_kg_or_skip", new=AsyncMock(return_value=0)),
        patch("apollo.handlers.chat._next_turn_index", new=AsyncMock(return_value=0)),
        patch(
            "apollo.handlers.chat.plan_next_question",
            new=AsyncMock(return_value=decision),
        ),
        patch("apollo.handlers.chat._persist_turn", new=persist_turn),
        patch("apollo.handlers.chat.answer_reference_question", new=bridge),
    ):
        response = await handle_chat(
            db=db,
            neo=None,
            session_id=sess.id,
            message=_QUESTION,
            ask_hoot=True,
        )

    assert response == {
        "apollo_reply": normal_reply,
        "kg_entries_added": 0,
        "kg": {"nodes": [], "edges": []},
        "covered_topics": [],
        "question_target": None,
    }
    bridge.assert_not_awaited()
    persist_turn.assert_awaited_once()
