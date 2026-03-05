from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import delete, select

from backend.auth import resolve_auth_context
from backend.chats.service import (
    append_turn,
    get_chat_session_for_user,
    get_or_create_chat_session_for_user,
    refresh_memory_summary,
    serialize_chat_session,
)
from backend.database.models import ChatSession, ChatTurn
from backend.database.session import get_async_session, run_async

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
