"""Unit tests for the target ``app.chat_messages`` ORM mapping (no DB)."""

import pytest
from sqlalchemy.dialects.postgresql import ARRAY

from database.models import ChatMessage


@pytest.mark.unit
def test_chat_message_target_shape():
    cols = {c.name for c in ChatMessage.__table__.columns}
    assert {"course_id", "chat_session_id", "external_id", "keywords"} <= cols
    assert "turn_id" not in cols
    assert ChatMessage.__table__.schema == "app"


@pytest.mark.unit
def test_chat_message_keywords_column_shape():
    col = ChatMessage.__table__.columns["keywords"]
    assert col.nullable is False
    assert isinstance(col.type, ARRAY)
    assert col.type.item_type.python_type is str
    assert col.default is not None
    assert col.default.is_callable
    assert col.default.arg(None) == []
    assert col.server_default is not None
    assert "'{}'::text[]" in str(col.server_default.arg)


@pytest.mark.unit
def test_chat_message_uniques_are_session_scoped():
    unique_columns = {
        tuple(column.name for column in constraint.columns)
        for constraint in ChatMessage.__table__.constraints
        if constraint.__class__.__name__ == "UniqueConstraint"
    }
    assert ("chat_session_id", "external_id") in unique_columns
    assert ("chat_session_id", "turn_index") in unique_columns
