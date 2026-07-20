"""Integration tests for migration 015 (RAG orchestrator schema).

These tests assert that migration 015 has been applied to the test database.
They require:
  - TEST_DATABASE_URL pointing at a Postgres instance with pgvector
  - A `db_session` async fixture that yields a SQLAlchemy AsyncSession
"""

import pytest
from sqlalchemy import text


@pytest.mark.integration
async def test_chat_session_snippets_table_exists(db_session):
    result = await db_session.execute(text(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = 'chat_session_snippets' ORDER BY column_name"
    ))
    cols = {row[0] for row in result}
    expected = {
        "chat_session_id",
        "chunk_id",
        "original_score",
        "first_seen_turn",
        "last_used_turn",
        "snippet_payload",
        "created_at",
    }
    assert expected <= cols


@pytest.mark.integration
async def test_app_chat_sessions_has_target_columns(db_session):
    result = await db_session.execute(text(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = 'app' AND table_name = 'chat_sessions'"
    ))
    cols = {row[0] for row in result}
    assert cols == {
        "id",
        "external_id",
        "user_id",
        "course_id",
        "metadata",
        "memory_summary",
        "created_at",
        "updated_at",
    }


@pytest.mark.integration
async def test_internal_chat_routing_decisions_table_exists(db_session):
    result = await db_session.execute(text(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = 'internal' AND table_name = 'chat_routing_decisions'"
    ))
    cols = {row[0] for row in result}
    assert "final_route" in cols and "retrieval_mode" in cols
