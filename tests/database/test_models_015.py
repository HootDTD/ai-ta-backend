"""Unit tests for Migration 015 SQLAlchemy models.

Verifies class metadata only - no DB connection required.
"""
import pytest

from database.models import ChatRouterDecision, ChatSession, ChatSessionSnippet


@pytest.mark.unit
def test_chat_session_snippet_columns():
    cols = {c.name for c in ChatSessionSnippet.__table__.columns}
    assert {
        "chat_session_id",
        "chunk_id",
        "original_score",
        "first_seen_turn",
        "last_used_turn",
        "snippet_payload",
        "created_at",
    } <= cols


@pytest.mark.unit
def test_chat_session_has_centroid_attr():
    assert hasattr(ChatSession, "topic_centroid_vector")


@pytest.mark.unit
def test_chat_router_decision_columns():
    cols = {c.name for c in ChatRouterDecision.__table__.columns}
    assert {
        "final_route",
        "retrieval_mode",
        "was_clarified",
        "clarify_cause",
    } <= cols
