"""Unit coverage for the INTERACTION4 aside path in the chat handler.

The intent LLM and Hoot bridge are mocked, and persistence is observed through
the handler's unit seam. No database, retrieval service, or network is used.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from apollo.handlers.chat import _maybe_intent_confirmation
from apollo.hoot_bridge.reference_answer import (
    ASIDE_MESSAGE_INTENT_TAG,
    MAX_ASIDES_PER_SESSION,
    MESSAGE_KIND_REFERENCE_ASIDE,
    ReferenceAsideResult,
)
from apollo.ontology import KGGraph

pytestmark = pytest.mark.unit

_QUESTION = "Wait, what is a network effect?"


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
    problem = MagicMock(database_id=42)
    return db, sess, store, concept, problem


def _reference_intent_llm():
    return patch(
        "apollo.handlers.intent.cheap_chat",
        return_value=json.dumps(
            {
                "intent": "reference_question",
                "confidence": 0.96,
                "reason": "The student asks for a definition.",
            }
        ),
    )


async def _run_reference_intent(*, db, sess, store, concept, problem):
    return await _maybe_intent_confirmation(
        db=db,
        sess=sess,
        attempt_id=99,
        message=_QUESTION,
        history=[],
        concept=concept,
        store=store,
        problem=problem,
    )


async def test_reference_question_flag_on_returns_aside_and_persona_resume(monkeypatch):
    monkeypatch.setenv("INTERACTION4", "1")
    db, sess, store, concept, problem = _chat_context()
    bridge = AsyncMock(
        return_value=ReferenceAsideResult(
            in_scope=True,
            text="A network effect means a product becomes more valuable as adoption grows.",
            citations=[{"label": "Week 2, p. 4"}],
        )
    )

    with (
        _reference_intent_llm(),
        patch("apollo.handlers.chat.answer_reference_question", new=bridge),
        patch("apollo.handlers.chat._next_turn_index", new=AsyncMock(return_value=8)),
    ):
        response = await _run_reference_intent(
            db=db,
            sess=sess,
            store=store,
            concept=concept,
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


async def test_reference_question_cap_redirects_without_bridge_call(monkeypatch):
    monkeypatch.setenv("INTERACTION4", "1")
    db, sess, store, concept, problem = _chat_context(aside_count=MAX_ASIDES_PER_SESSION)
    bridge = AsyncMock()

    with (
        _reference_intent_llm(),
        patch("apollo.handlers.chat.answer_reference_question", new=bridge),
        patch("apollo.handlers.chat._next_turn_index", new=AsyncMock(return_value=12)),
    ):
        response = await _run_reference_intent(
            db=db,
            sess=sess,
            store=store,
            concept=concept,
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
    db, sess, store, concept, problem = _chat_context()
    bridge = AsyncMock(side_effect=RuntimeError("retrieval unavailable"))

    with (
        _reference_intent_llm(),
        patch("apollo.handlers.chat.answer_reference_question", new=bridge),
        patch("apollo.handlers.chat._next_turn_index", new=AsyncMock(return_value=4)),
    ):
        response = await _run_reference_intent(
            db=db,
            sess=sess,
            store=store,
            concept=concept,
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
