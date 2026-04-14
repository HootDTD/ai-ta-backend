from unittest.mock import patch

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from apollo.errors import FilterRejectedError, ParserCouldNotExtractError
from apollo.handlers.chat import handle_chat
from apollo.persistence.models import ApolloSession, KGEntry, Message, ProblemAttempt, SessionPhase, SessionStatus
from database.models import Base


@pytest_asyncio.fixture
async def db_with_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    apollo_tables = [
        ApolloSession.__table__,
        KGEntry.__table__,
        Message.__table__,
        ProblemAttempt.__table__,
    ]
    async with engine.begin() as conn:
        await conn.run_sync(lambda sync_conn: Base.metadata.create_all(sync_conn, tables=apollo_tables))
    Session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with Session() as s:
        sess = ApolloSession(
            student_id="stu-1",
            concept_cluster_id="fluid_mechanics",
            status=SessionStatus.active.value,
            phase=SessionPhase.TEACHING.value,
            current_problem_id="bernoulli_horizontal_pipe_find_p2",
        )
        s.add(sess)
        await s.commit()
        await s.refresh(sess)
        yield s, sess.id
    await engine.dispose()


@pytest.mark.asyncio
@patch("apollo.handlers.chat.draft_reply")
@patch("apollo.handlers.chat.parse_utterance")
async def test_chat_happy_path(mock_parse, mock_draft, db_with_session):
    db, session_id = db_with_session
    mock_parse.return_value = [
        {"type": "equation", "content": {"symbolic": "A1*v1 - A2*v2", "label": "Continuity"}},
    ]
    mock_draft.return_value = "Okay — so when density is constant, A1*v1 equals A2*v2. Why is that?"

    result = await handle_chat(
        db=db,
        session_id=session_id,
        message="For incompressible flow, A1*v1 = A2*v2.",
    )

    assert "A1" in result["apollo_reply"] or "density" in result["apollo_reply"]
    assert result["kg_entries_added"] == 1
    assert "equation" in result["kg"]
    assert len(result["kg"]["equation"]) == 1


@pytest.mark.asyncio
@patch("apollo.handlers.chat.parse_utterance")
async def test_chat_propagates_parser_error(mock_parse, db_with_session):
    db, session_id = db_with_session
    mock_parse.side_effect = ParserCouldNotExtractError(utterance="garbled teaching attempt")

    with pytest.raises(ParserCouldNotExtractError):
        await handle_chat(db=db, session_id=session_id, message="garbled teaching attempt")


@pytest.mark.asyncio
@patch("apollo.handlers.chat.draft_reply")
@patch("apollo.handlers.chat.parse_utterance")
async def test_chat_propagates_filter_rejection(mock_parse, mock_draft, db_with_session):
    db, session_id = db_with_session
    mock_parse.return_value = []
    mock_draft.return_value = "That's the continuity equation at work."

    with pytest.raises(FilterRejectedError):
        await handle_chat(db=db, session_id=session_id, message="ok")


@pytest.mark.asyncio
@patch("apollo.handlers.chat.draft_reply")
@patch("apollo.handlers.chat.parse_utterance")
async def test_chat_persists_messages(mock_parse, mock_draft, db_with_session):
    db, session_id = db_with_session
    mock_parse.return_value = []
    mock_draft.return_value = "tell me more about that"

    await handle_chat(db=db, session_id=session_id, message="ok")

    from sqlalchemy import select
    msgs = (await db.execute(select(Message).where(Message.session_id == session_id).order_by(Message.turn_index))).scalars().all()
    assert [m.role for m in msgs] == ["student", "apollo"]
    assert msgs[0].content == "ok"
    assert msgs[1].content == "tell me more about that"
