"""Two-user isolation for chat repositories on the service-role engine."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from chats.service import (
    append_turn,
    delete_chat_session_for_user,
    get_chat_session_for_user,
    list_recent_turns,
    serialize_chat_session,
)
from database.models import ChatMessage, ChatSession, Course

pytestmark = pytest.mark.integration

USER_A = "00000000-0000-0000-0000-00000000008a"
USER_B = "00000000-0000-0000-0000-00000000008b"


async def _seed_owned_chat(db_session) -> tuple[Course, ChatSession, ChatMessage]:
    course = Course(
        name="DB-08a isolation",
        slug=f"db08a-isolation-{id(db_session)}",
        subject_name="Security",
    )
    db_session.add(course)
    await db_session.flush()

    chat = ChatSession(
        external_id=f"db08a-chat-{id(db_session)}",
        user_id=USER_A,
        course_id=course.id,
        metadata_={},
        memory_summary="",
    )
    db_session.add(chat)
    await db_session.flush()
    message = await append_turn(
        db_session,
        chat_session_id=int(chat.id),
        user_id=USER_A,
        course_id=int(course.id),
        role="user",
        content="owner-only message",
    )
    await db_session.flush()
    return course, chat, message


async def test_service_role_repository_hides_other_users_session_and_messages(db_session):
    course, chat, _ = await _seed_owned_chat(db_session)

    assert (
        await get_chat_session_for_user(
            db_session,
            chat_id=chat.external_id,
            user_id=USER_B,
            course_id=int(course.id),
        )
        is None
    )
    assert (
        await list_recent_turns(
            db_session,
            chat_session_id=int(chat.id),
            user_id=USER_B,
            course_id=int(course.id),
        )
        == []
    )
    with pytest.raises(ValueError, match="chat not found"):
        await serialize_chat_session(db_session, chat_id=chat.external_id, user_id=USER_B)


async def test_service_role_repository_cannot_delete_other_users_chat(db_session):
    course, chat, message = await _seed_owned_chat(db_session)

    deleted = await delete_chat_session_for_user(
        db_session,
        chat_id=chat.external_id,
        user_id=USER_B,
        course_id=int(course.id),
    )
    assert deleted is False
    assert await db_session.scalar(select(ChatSession).where(ChatSession.id == chat.id)) is chat
    assert (
        await db_session.scalar(select(ChatMessage).where(ChatMessage.id == message.id)) is message
    )

    assert await delete_chat_session_for_user(
        db_session,
        chat_id=chat.external_id,
        user_id=USER_A,
        course_id=int(course.id),
    )
    await db_session.flush()
    assert await db_session.scalar(select(ChatSession).where(ChatSession.id == chat.id)) is None
    assert await db_session.scalar(select(ChatMessage).where(ChatMessage.id == message.id)) is None
