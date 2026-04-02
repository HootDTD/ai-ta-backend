from __future__ import annotations

import contextlib
import os
from datetime import UTC, datetime
from typing import Any, Dict, List

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import ChatSession, ChatTurn

MEMORY_WINDOW_TURNS = int(os.getenv("CHAT_MEMORY_WINDOW_TURNS", "8"))
MEMORY_SUMMARY_MAX_CHARS = int(os.getenv("CHAT_MEMORY_SUMMARY_MAX_CHARS", "3000"))
MEMORY_SUMMARY_TRIGGER_TURNS = int(os.getenv("CHAT_MEMORY_SUMMARY_TRIGGER_TURNS", "12"))


def build_memory_context(summary: str, turns: List[ChatTurn]) -> str:
    lines: List[str] = []
    if summary.strip():
        lines.append("Conversation summary:")
        lines.append(summary.strip())
        lines.append("")
    if turns:
        lines.append("Recent conversation turns:")
        for turn in turns:
            role = (turn.role or "user").strip().lower()
            prefix = "User" if role == "user" else "Assistant"
            content = (turn.content or "").strip()
            if not content:
                continue
            lines.append(f"{prefix}: {content}")
    return "\n".join(lines).strip()


async def get_chat_session_for_user(
    db_session: AsyncSession,
    *,
    chat_id: str,
    user_id: str,
) -> ChatSession | None:
    result = await db_session.execute(
        select(ChatSession).where(
            ChatSession.chat_id == chat_id,
            ChatSession.user_id == user_id,
        )
    )
    return result.scalars().first()


async def get_or_create_chat_session_for_user(
    db_session: AsyncSession,
    *,
    chat_id: str,
    user_id: str,
    search_space_id: int,
    meta: Dict[str, Any] | None = None,
) -> ChatSession:
    existing = await get_chat_session_for_user(
        db_session,
        chat_id=chat_id,
        user_id=user_id,
    )
    if existing is not None:
        return existing

    session = ChatSession(
        chat_id=chat_id,
        user_id=user_id,
        search_space_id=search_space_id,
        meta=meta or {},
        memory_summary="",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    db_session.add(session)
    await db_session.flush()
    return session


async def list_recent_turns(
    db_session: AsyncSession,
    *,
    chat_session_id: int,
    limit: int = MEMORY_WINDOW_TURNS,
) -> List[ChatTurn]:
    result = await db_session.execute(
        select(ChatTurn)
        .where(ChatTurn.chat_session_id == chat_session_id)
        .order_by(ChatTurn.turn_index.desc())
        .limit(limit)
    )
    rows = result.scalars().all()
    rows.reverse()
    return rows


async def append_turn(
    db_session: AsyncSession,
    *,
    chat_session_id: int,
    role: str,
    content: str,
    created_at: str | None = None,
    model: str | None = None,
    tool_name: str | None = None,
    tool_inputs: Dict[str, Any] | None = None,
    attachments: List[Dict[str, Any]] | None = None,
) -> ChatTurn:
    # Lock the parent chat session row so concurrent writers serialize turn indexes.
    lock_result = await db_session.execute(
        select(ChatSession.id)
        .where(ChatSession.id == chat_session_id)
        .with_for_update()
    )
    if lock_result.scalar_one_or_none() is None:
        raise ValueError(f"chat_session not found: {chat_session_id}")

    idx_result = await db_session.execute(
        select(func.coalesce(func.max(ChatTurn.turn_index), 0)).where(
            ChatTurn.chat_session_id == chat_session_id
        )
    )
    max_idx = int(idx_result.scalar_one() or 0)
    next_index = max_idx + 1
    dt = datetime.now(UTC)
    if created_at:
        with contextlib.suppress(Exception):
            parsed = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
            dt = parsed

    turn = ChatTurn(
        chat_session_id=chat_session_id,
        turn_index=next_index,
        turn_id=str(next_index),
        role=role,
        content=content or "",
        created_at=dt,
        model=model,
        tool_name=tool_name,
        tool_inputs=tool_inputs,
        attachments=attachments or [],
    )
    db_session.add(turn)
    return turn


def _summarize_turns_for_memory(turns: List[ChatTurn]) -> str:
    snippets: List[str] = []
    for turn in turns:
        txt = (turn.content or "").strip()
        if not txt:
            continue
        role = (turn.role or "user").strip().lower()
        prefix = "U" if role == "user" else "A"
        snippets.append(f"{prefix}: {txt[:220]}")
    summary = " | ".join(snippets)
    if len(summary) > MEMORY_SUMMARY_MAX_CHARS:
        summary = summary[: MEMORY_SUMMARY_MAX_CHARS - 3].rstrip() + "..."
    return summary


async def refresh_memory_summary(
    db_session: AsyncSession,
    *,
    chat_session: ChatSession,
) -> None:
    all_turns_result = await db_session.execute(
        select(ChatTurn)
        .where(ChatTurn.chat_session_id == chat_session.id)
        .order_by(ChatTurn.turn_index.asc())
    )
    all_turns = all_turns_result.scalars().all()
    if len(all_turns) <= MEMORY_SUMMARY_TRIGGER_TURNS:
        chat_session.memory_summary = _summarize_turns_for_memory(all_turns[:-MEMORY_WINDOW_TURNS] if len(all_turns) > MEMORY_WINDOW_TURNS else [])
        chat_session.updated_at = datetime.now(UTC)
        return

    older = all_turns[:-MEMORY_WINDOW_TURNS]
    chat_session.memory_summary = _summarize_turns_for_memory(older)
    chat_session.updated_at = datetime.now(UTC)


async def serialize_chat_session(
    db_session: AsyncSession,
    *,
    chat_id: str,
    user_id: str,
) -> dict:
    session = await get_chat_session_for_user(db_session, chat_id=chat_id, user_id=user_id)
    if session is None:
        raise ValueError("chat not found")
    turns_result = await db_session.execute(
        select(ChatTurn)
        .where(ChatTurn.chat_session_id == session.id)
        .order_by(ChatTurn.turn_index.asc())
    )
    turns = turns_result.scalars().all()
    return {
        "chat_id": session.chat_id,
        "meta": session.meta or {},
        "memory_summary": session.memory_summary or "",
        "turns": [
            {
                "turn_id": t.turn_id,
                "role": t.role,
                "content": t.content,
                "created_at": t.created_at.isoformat() if t.created_at else None,
                "model": t.model,
                "tool_name": t.tool_name,
                "tool_inputs": t.tool_inputs,
                "attachments": t.attachments or [],
            }
            for t in turns
        ],
    }
