"""Unit tests for migration 029 SQLAlchemy ORM mapping (no DB).

WU-5B4 adds ONE write-only JSONB column ``chat_turns.keywords`` (§10 RQ5 hedge:
persist the per-/ask ``extract_and_filter_keywords`` output for offline
class-level backfill). These tests assert the ORM ``ChatTurn`` class metadata
mirrors the sibling ``attachments``/``citations`` JSONB-array shape EXACTLY —
class metadata only, no DB connection required.
"""

import pytest
from sqlalchemy.dialects.postgresql import JSONB

from database.models import ChatTurn


@pytest.mark.unit
def test_chat_turn_has_keywords_column():
    cols = {c.name for c in ChatTurn.__table__.columns}
    assert "keywords" in cols


@pytest.mark.unit
def test_chat_turn_keywords_column_shape():
    col = ChatTurn.__table__.columns["keywords"]
    # NOT NULL, mirroring attachments/citations.
    assert col.nullable is False
    # JSONB type instance.
    assert isinstance(col.type, JSONB)
    # ORM-side default produces a fresh empty list (so omitted inserts default
    # to []). SQLAlchemy wraps the `default=list` callable, so assert behavior
    # (it is callable and yields []) rather than object identity.
    assert col.default is not None
    assert col.default.is_callable
    assert col.default.arg(None) == []
    # Server default is the SQL literal '[]'::jsonb (so raw/legacy inserts are safe).
    assert col.server_default is not None
    assert "'[]'::jsonb" in str(col.server_default.arg)


@pytest.mark.unit
def test_chat_turn_keywords_matches_citations_shape():
    """keywords copies the citations/attachments JSONB-array convention exactly."""
    keywords = ChatTurn.__table__.columns["keywords"]
    citations = ChatTurn.__table__.columns["citations"]
    assert keywords.nullable == citations.nullable
    assert type(keywords.type) is type(citations.type)
    # Both wrap the same `default=list` convention (callable yielding []).
    assert keywords.default.is_callable == citations.default.is_callable
    assert keywords.default.arg(None) == citations.default.arg(None) == []
    assert str(keywords.server_default.arg) == str(citations.server_default.arg)
