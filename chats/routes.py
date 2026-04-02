from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import delete, func, select

from auth import resolve_auth_context
from chats.service import (
    append_turn,
    get_chat_session_for_user,
    get_or_create_chat_session_for_user,
    refresh_memory_summary,
    serialize_chat_session,
)
from database.models import ChatSession, ChatTurn
from database.session import get_async_session, run_async

log = logging.getLogger(__name__)
router = APIRouter()


def _coerce_search_space_id(payload: Dict[str, Any]) -> int:
    raw = payload.get("search_space_id")
    if raw is None:
        meta = payload.get("meta")
        if isinstance(meta, dict):
            raw = meta.get("search_space_id")
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = 0
    return value


@router.get("/chats")
def list_chats(request: Request):
    """Return chat sessions for the authenticated user, optionally filtered by search_space_id."""
    from fastapi import Query as FQuery

    auth = resolve_auth_context(request)
    raw_ssid = request.query_params.get("search_space_id")
    search_space_id = int(raw_ssid) if raw_ssid and raw_ssid.isdigit() else None

    async def _run() -> list:
        async with get_async_session() as db_session:
            stmt = (
                select(ChatSession)
                .where(ChatSession.user_id == auth.user_id)
            )
            if search_space_id:
                stmt = stmt.where(ChatSession.search_space_id == search_space_id)
            stmt = stmt.order_by(ChatSession.updated_at.desc())

            result = await db_session.execute(stmt)
            sessions = result.scalars().all()

            out = []
            for s in sessions:
                # Get the first user turn as a title preview
                first_turn_result = await db_session.execute(
                    select(ChatTurn)
                    .where(
                        ChatTurn.chat_session_id == s.id,
                        ChatTurn.role == "user",
                    )
                    .order_by(ChatTurn.turn_index.asc())
                    .limit(1)
                )
                first_turn = first_turn_result.scalars().first()

                # Get total turn count
                count_result = await db_session.execute(
                    select(func.count(ChatTurn.id))
                    .where(ChatTurn.chat_session_id == s.id)
                )
                turn_count = int(count_result.scalar_one() or 0)

                title = ""
                if first_turn and first_turn.content:
                    title = first_turn.content.strip()[:120]

                out.append({
                    "chat_id": s.chat_id,
                    "search_space_id": s.search_space_id,
                    "title": title,
                    "turn_count": turn_count,
                    "created_at": s.created_at.isoformat() if s.created_at else None,
                    "updated_at": s.updated_at.isoformat() if s.updated_at else None,
                })
            return out

    try:
        return run_async(_run())
    except HTTPException:
        raise
    except Exception as exc:
        log.error("Failed to list chats: %s", exc)
        raise HTTPException(status_code=500, detail="failed to list chats")


@router.post("/chats/{chat_id}")
def save_chat(chat_id: str, payload: Dict[str, Any], request: Request):
    """Import/upsert chat transcript for the authenticated user."""
    auth = resolve_auth_context(request)
    data = dict(payload or {})
    turns = data.get("turns", []) or []
    meta = data.get("meta", {}) or {}
    search_space_id = _coerce_search_space_id(data)

    async def _run() -> dict:
        async with get_async_session() as db_session:
            existing = await get_chat_session_for_user(
                db_session,
                chat_id=chat_id,
                user_id=auth.user_id,
            )
            if existing is None and search_space_id <= 0:
                raise HTTPException(status_code=400, detail="search_space_id is required for new chats")

            session = existing or await get_or_create_chat_session_for_user(
                db_session,
                chat_id=chat_id,
                user_id=auth.user_id,
                search_space_id=search_space_id,
                meta=meta,
            )

            if search_space_id > 0 and int(session.search_space_id) != search_space_id:
                raise HTTPException(status_code=400, detail="search_space_id mismatch for chat")

            session.meta = meta
            session.updated_at = datetime.now(UTC)

            # Serialize full import/upsert writes for this chat session.
            await db_session.execute(
                select(ChatSession.id)
                .where(ChatSession.id == session.id)
                .with_for_update()
            )

            await db_session.execute(
                delete(ChatTurn).where(ChatTurn.chat_session_id == session.id)
            )

            for turn in turns:
                turn_dt = turn.get("created_at")
                await append_turn(
                    db_session,
                    chat_session_id=int(session.id),
                    role=str(turn.get("role", "user")),
                    content=str(turn.get("content", "")),
                    created_at=turn_dt,
                    model=turn.get("model"),
                    tool_name=turn.get("tool_name"),
                    tool_inputs=turn.get("tool_inputs"),
                    attachments=turn.get("attachments") or [],
                )

            await refresh_memory_summary(db_session, chat_session=session)
            await db_session.commit()
            return {"ok": True, "chat_id": chat_id}

    try:
        return run_async(_run())
    except HTTPException:
        raise
    except Exception as exc:
        log.error("Failed to save chat %s: %s", chat_id, exc)
        raise HTTPException(status_code=500, detail="failed to save chat")


@router.delete("/chats/{chat_id}", status_code=204)
def delete_chat(chat_id: str, request: Request):
    """Delete a chat session and all its turns. Owner only."""
    auth = resolve_auth_context(request)

    async def _run():
        async with get_async_session() as db_session:
            session = await get_chat_session_for_user(
                db_session,
                chat_id=chat_id,
                user_id=auth.user_id,
            )
            if session is None:
                raise HTTPException(status_code=404, detail="chat not found")
            await db_session.execute(
                delete(ChatTurn).where(ChatTurn.chat_session_id == session.id)
            )
            await db_session.execute(
                delete(ChatSession).where(ChatSession.id == session.id)
            )
            await db_session.commit()

    try:
        run_async(_run())
    except HTTPException:
        raise
    except Exception as exc:
        log.error("Failed to delete chat %s: %s", chat_id, exc)
        raise HTTPException(status_code=500, detail="failed to delete chat")
    return None


@router.get("/chats/{chat_id}")
def get_chat(chat_id: str, request: Request):
    """Return chat transcript for the authenticated owner."""
    auth = resolve_auth_context(request)

    async def _run() -> dict:
        async with get_async_session() as db_session:
            try:
                return await serialize_chat_session(
                    db_session,
                    chat_id=chat_id,
                    user_id=auth.user_id,
                )
            except ValueError:
                raise HTTPException(status_code=404, detail="chat not found")

    try:
        return run_async(_run())
    except HTTPException:
        raise
    except Exception as exc:
        log.error("Failed to load chat %s: %s", chat_id, exc)
        raise HTTPException(status_code=500, detail="failed to load chat")
