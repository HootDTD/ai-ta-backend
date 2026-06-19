"""Unit tests for ``chats.service.append_turn`` keyword threading (no DB).

WU-5B4 adds a backward-compatible, keyword-only ``keywords`` param to
``append_turn`` that writes ``ChatTurn(keywords=keywords or [])`` — the §10 RQ5
write-only hedge. These tests drive ``append_turn`` against a FAKE
``AsyncSession`` (the two internal ``execute`` calls — the FOR-UPDATE session
lock and the ``max(turn_index)`` query — are stubbed) so no real DB is needed;
they capture the ``ChatTurn`` handed to ``db_session.add(...)`` and assert the
keyword field threads through.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from chats.service import append_turn


def _fake_session(lock_id: int = 1, max_idx: int = 0):
    """A fake AsyncSession whose two execute() calls return the lock row id then
    the max turn_index. ``add`` records the built ChatTurn for inspection."""
    lock_result = MagicMock()
    lock_result.scalar_one_or_none.return_value = lock_id
    idx_result = MagicMock()
    idx_result.scalar_one.return_value = max_idx

    session = MagicMock()
    session.execute = AsyncMock(side_effect=[lock_result, idx_result])
    session.added = []
    session.add = lambda obj: session.added.append(obj)
    return session


@pytest.mark.unit
async def test_append_turn_sets_keywords_on_built_turn():
    session = _fake_session()
    turn = await append_turn(
        session,
        chat_session_id=1,
        role="assistant",
        content="x",
        keywords=["energy", "work"],
    )
    assert session.added == [turn]
    assert turn.keywords == ["energy", "work"]


@pytest.mark.unit
async def test_append_turn_keywords_defaults_to_empty_list():
    session = _fake_session()
    turn = await append_turn(
        session,
        chat_session_id=1,
        role="assistant",
        content="x",
    )
    assert turn.keywords == []
    # The default must be a NEW list object, never a shared mutable default.
    turn.keywords.append("leaked")
    other = _fake_session()
    turn2 = await append_turn(
        other,
        chat_session_id=1,
        role="assistant",
        content="y",
    )
    assert turn2.keywords == []


@pytest.mark.unit
async def test_append_turn_keywords_none_coalesces_to_empty():
    session = _fake_session()
    turn = await append_turn(
        session,
        chat_session_id=1,
        role="assistant",
        content="x",
        keywords=None,
    )
    assert turn.keywords == []


@pytest.mark.unit
async def test_append_turn_keywords_does_not_touch_other_columns():
    """The new param is isolated — turns built with and without keywords are
    identical in every OTHER field."""
    s1 = _fake_session()
    with_kw = await append_turn(
        s1,
        chat_session_id=7,
        role="assistant",
        content="answer",
        model="gpt-5",
        citations=[{"label": "Textbook", "page": 1}],
        attachments=[{"name": "img"}],
        keywords=["entropy"],
    )
    s2 = _fake_session()
    without_kw = await append_turn(
        s2,
        chat_session_id=7,
        role="assistant",
        content="answer",
        model="gpt-5",
        citations=[{"label": "Textbook", "page": 1}],
        attachments=[{"name": "img"}],
    )
    for field in ("role", "content", "turn_index", "model", "attachments", "citations"):
        assert getattr(with_kw, field) == getattr(without_kw, field)
    # Only keywords differs.
    assert with_kw.keywords == ["entropy"]
    assert without_kw.keywords == []
