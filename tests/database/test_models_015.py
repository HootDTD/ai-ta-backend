"""Unit tests for Migration 015 SQLAlchemy models.

Verifies class metadata only - no DB connection required.
"""

import pytest
from sqlalchemy import select

from database.models import ChatRouterDecision, ChatSession, ChatSessionSnippet


@pytest.mark.unit
def test_chat_session_snippet_columns():
    cols = {c.name for c in ChatSessionSnippet.__table__.columns}
    assert {
        "learning_activity_id",
        "chunk_id",
        "course_id",
        "original_score",
        "first_seen_turn",
        "last_used_turn",
        "snippet_payload",
        "created_at",
    } <= cols


@pytest.mark.unit
def test_chat_models_use_target_schemas():
    assert ChatSession.__table__.schema == "app"
    assert ChatSession.__table__.name == "learning_activities"
    assert ChatSessionSnippet.__table__.schema == "internal"
    assert ChatRouterDecision.__table__.schema == "internal"
    assert ChatRouterDecision.__tablename__ == "chat_routing_decisions"
    assert ChatSession.__mapper__.polymorphic_identity == "chat"
    compiled = str(select(ChatSession).compile(compile_kwargs={"literal_binds": True}))
    assert "learning_activities.modality IN ('chat')" in compiled


@pytest.mark.unit
def test_chat_router_decision_columns():
    cols = {c.name for c in ChatRouterDecision.__table__.columns}
    assert {
        "final_route",
        "retrieval_mode",
        "was_clarified",
        "clarify_cause",
        "course_id",
    } <= cols
