"""Tests for bounded chat-history loader (item #2).

Uses an in-memory SQLite database. The summarizer is patched — we test
the windowing logic, not the LLM."""
from __future__ import annotations

import json
from unittest.mock import patch

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from apollo.conftest import TEST_SPACE_ID, TEST_USER_ID
from apollo.handlers.chat import _load_history
from apollo.handlers.history import (
    RAW_WINDOW_TURNS,
    REFRESH_EVERY_K_TURNS,
    load_windowed_history,
)
from apollo.persistence.models import (
    ApolloSession,
    Message,
    ProblemAttempt,
    SessionPhase,
    SessionStatus,
)
from database.models import Base


@pytest_asyncio.fixture
async def db_with_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    apollo_tables = [
        ApolloSession.__table__,
        Message.__table__,
        ProblemAttempt.__table__,
    ]
    async with engine.begin() as conn:
        await conn.run_sync(
            lambda sync_conn: Base.metadata.create_all(sync_conn, tables=apollo_tables)
        )
    Session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with Session() as s:
        sess = ApolloSession(
            user_id=TEST_USER_ID,
            search_space_id=TEST_SPACE_ID,
            concept_id=1,
            status=SessionStatus.active.value,
            phase=SessionPhase.TEACHING.value,
            current_problem_id="p1",
        )
        s.add(sess)
        await s.commit()
        await s.refresh(sess)
        yield s, sess
    await engine.dispose()


async def _seed_messages(s: AsyncSession, session_id: int, n: int) -> None:
    for i in range(n):
        role = "student" if i % 2 == 0 else "apollo"
        s.add(Message(
            session_id=session_id,
            attempt_id=None,
            role=role,
            content=f"turn {i}",
            turn_index=i,
        ))
    await s.commit()


@pytest.mark.asyncio
async def test_short_session_returns_no_summary(db_with_session):
    """Below the window size => no summary, all messages returned raw."""
    s, sess = db_with_session
    await _seed_messages(s, sess.id, 5)

    summary, window = await load_windowed_history(db=s, session=sess, attempt_id=None)

    assert summary is None
    assert len(window) == 5
    assert window[0]["content"] == "turn 0"


@pytest.mark.asyncio
async def test_at_window_size_returns_no_summary(db_with_session):
    """Exactly at window size => still no older turns to summarize."""
    s, sess = db_with_session
    await _seed_messages(s, sess.id, RAW_WINDOW_TURNS)

    summary, window = await load_windowed_history(db=s, session=sess, attempt_id=None)
    assert summary is None
    assert len(window) == RAW_WINDOW_TURNS


@pytest.mark.asyncio
@patch("apollo.handlers.history.cheap_chat",
       return_value=json.dumps({"summary": "early turns happened"}))
async def test_above_window_size_triggers_summary(mock_chat, db_with_session):
    """One turn over window size triggers a summary refresh."""
    s, sess = db_with_session
    n = RAW_WINDOW_TURNS + 5
    await _seed_messages(s, sess.id, n)

    summary, window = await load_windowed_history(db=s, session=sess, attempt_id=None)

    assert summary == "early turns happened"
    # Window should be exactly RAW_WINDOW_TURNS most recent turns.
    assert len(window) == RAW_WINDOW_TURNS
    # Older turns indices 0..n-RAW-1 are summarized.
    # Verify the persisted "covered through" pointer.
    assert sess.history_summary == "early turns happened"
    assert sess.history_summary_up_to_turn == n - RAW_WINDOW_TURNS - 1
    assert mock_chat.call_count == 1


@pytest.mark.asyncio
@patch("apollo.handlers.history.cheap_chat",
       return_value=json.dumps({"summary": "summary"}))
async def test_subsequent_turns_within_K_dont_resummarize(
    mock_chat, db_with_session,
):
    """After a summary is computed, calling load again before K new turns
    have arrived should NOT regenerate the summary (cost control)."""
    s, sess = db_with_session
    n = RAW_WINDOW_TURNS + 5
    await _seed_messages(s, sess.id, n)

    # First call — produces summary.
    await load_windowed_history(db=s, session=sess, attempt_id=None)
    assert mock_chat.call_count == 1

    # Add 2 more turns (under K threshold of 8) and call again.
    s.add(Message(
        session_id=sess.id, attempt_id=None,
        role="student", content="more", turn_index=n,
    ))
    s.add(Message(
        session_id=sess.id, attempt_id=None,
        role="apollo", content="ok", turn_index=n + 1,
    ))
    await s.commit()

    await load_windowed_history(db=s, session=sess, attempt_id=None)
    assert mock_chat.call_count == 1, "should NOT re-summarize within K"


@pytest.mark.asyncio
@patch("apollo.handlers.history.cheap_chat",
       return_value=json.dumps({"summary": "fresh"}))
async def test_summary_refreshes_after_K_new_older_turns(
    mock_chat, db_with_session,
):
    """When the older tail grows by K turns since the last refresh, the
    summarizer fires again."""
    s, sess = db_with_session
    n = RAW_WINDOW_TURNS + 5
    await _seed_messages(s, sess.id, n)

    await load_windowed_history(db=s, session=sess, attempt_id=None)
    first_covered = sess.history_summary_up_to_turn
    assert mock_chat.call_count == 1

    # Add K new turns. Half become "older" (the new ones push older turns
    # out of the window), so the older-tail grows by K.
    for i in range(REFRESH_EVERY_K_TURNS + 2):
        s.add(Message(
            session_id=sess.id, attempt_id=None,
            role="student" if i % 2 == 0 else "apollo",
            content=f"new {i}", turn_index=n + i,
        ))
    await s.commit()

    await load_windowed_history(db=s, session=sess, attempt_id=None)
    assert mock_chat.call_count == 2
    assert sess.history_summary_up_to_turn > first_covered


@pytest.mark.asyncio
@patch("apollo.handlers.history.cheap_chat",
       side_effect=RuntimeError("API down"))
async def test_summarizer_failure_falls_back_to_raw_window(
    mock_chat, db_with_session,
):
    """LLM error => no summary, but raw window still returned."""
    s, sess = db_with_session
    n = RAW_WINDOW_TURNS + 3
    await _seed_messages(s, sess.id, n)

    summary, window = await load_windowed_history(db=s, session=sess, attempt_id=None)
    assert summary is None  # never set
    assert len(window) == RAW_WINDOW_TURNS


@pytest.mark.asyncio
@patch("apollo.handlers.history.cheap_chat",
       return_value="not json at all")
async def test_summarizer_malformed_json_falls_back(
    mock_chat, db_with_session,
):
    s, sess = db_with_session
    await _seed_messages(s, sess.id, RAW_WINDOW_TURNS + 3)
    summary, window = await load_windowed_history(db=s, session=sess, attempt_id=None)
    assert summary is None


@pytest.mark.asyncio
async def test_history_is_scoped_to_attempt(db_with_session):
    s, sess = db_with_session
    first = ProblemAttempt(session_id=sess.id, problem_id="p1", difficulty="intro")
    second = ProblemAttempt(session_id=sess.id, problem_id="p1", difficulty="intro")
    s.add_all([first, second])
    await s.flush()
    s.add_all([
        Message(
            session_id=sess.id, attempt_id=first.id, role="student",
            content="old attempt secret", turn_index=0,
        ),
        Message(
            session_id=sess.id, attempt_id=second.id, role="student",
            content="new attempt only", turn_index=1,
        ),
    ])
    await s.commit()

    summary, window = await load_windowed_history(
        db=s, session=sess, attempt_id=second.id,
    )

    assert summary is None
    assert [turn["content"] for turn in window] == ["new attempt only"]
    assert await _load_history(s, sess.id, second.id) == [
        {"role": "user", "content": "new attempt only"},
    ]
