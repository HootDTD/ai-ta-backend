"""Real-PG ORM round-trip tests for ``chat_turns.keywords`` (WU-5B4).

Runs on the ``db_session`` fixture (real pgvector container, ``Base.metadata.
create_all`` picks up the new column, savepoint rollback per test). Proves the
write-only JSONB column persists a <=8-term concept list, defaults to ``[]``
when omitted, and round-trips through the ``append_turn`` SERVICE write-API
end-to-end. All real PG — no mocks.
"""

import pytest
from sqlalchemy import select

from chats.service import append_turn
from database.models import ChatSession, ChatTurn, SearchSpace

pytestmark = pytest.mark.integration

_USER = "00000000-0000-0000-0000-0000000000ab"


async def _seed_session(db_session) -> ChatSession:
    space = SearchSpace(
        name="kw roundtrip space",
        slug=f"kw-roundtrip-{id(db_session)}",
        subject_name="Physics",
    )
    db_session.add(space)
    await db_session.flush()
    session = ChatSession(
        chat_id=f"kw-chat-{id(db_session)}",
        user_id=_USER,
        search_space_id=space.id,
        meta={},
        memory_summary="",
    )
    db_session.add(session)
    await db_session.flush()
    return session


async def _read_turn(db_session, turn_id: int) -> ChatTurn:
    return (
        await db_session.execute(
            select(ChatTurn)
            .where(ChatTurn.id == turn_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()


async def test_keywords_roundtrip_list_le_8(db_session):
    session = await _seed_session(db_session)
    terms = ["a", "b", "c", "d", "e", "f", "g", "h"]  # exactly 8
    turn = ChatTurn(
        chat_session_id=session.id,
        turn_index=1,
        turn_id="1",
        role="assistant",
        content="x",
        keywords=terms,
    )
    db_session.add(turn)
    await db_session.commit()

    read = await _read_turn(db_session, turn.id)
    assert read.keywords == terms  # exact list, in order


async def test_keywords_default_empty_when_absent(db_session):
    session = await _seed_session(db_session)
    turn = ChatTurn(
        chat_session_id=session.id,
        turn_index=1,
        turn_id="1",
        role="assistant",
        content="x",
    )
    db_session.add(turn)
    await db_session.commit()

    read = await _read_turn(db_session, turn.id)
    assert read.keywords == []


async def test_keywords_via_append_turn_service(db_session):
    session = await _seed_session(db_session)
    turn = await append_turn(
        db_session,
        chat_session_id=session.id,
        role="assistant",
        content="x",
        keywords=["entropy", "heat"],
    )
    await db_session.commit()

    read = (
        await db_session.execute(
            select(ChatTurn)
            .where(ChatTurn.chat_session_id == session.id)
            .where(ChatTurn.turn_index == turn.turn_index)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    assert read.keywords == ["entropy", "heat"]


async def test_keywords_unicode_and_multiword_terms(db_session):
    session = await _seed_session(db_session)
    terms = ["mécanique", "angular momentum", "résistance de l'air"]
    turn = await append_turn(
        db_session,
        chat_session_id=session.id,
        role="assistant",
        content="x",
        keywords=terms,
    )
    await db_session.commit()

    read = await _read_turn(db_session, turn.id)
    assert read.keywords == terms
