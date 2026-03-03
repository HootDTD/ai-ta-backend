from __future__ import annotations

import logging
from typing import Any, Dict

from fastapi import APIRouter, HTTPException

from backend import supabase_client as sb

log = logging.getLogger(__name__)
router = APIRouter()


@router.post("/chats/{chat_id}")
def save_chat(chat_id: str, payload: Dict[str, Any]):
    """Save a chat transcript to Supabase chat_sessions + chat_turns tables."""
    data = dict(payload or {})
    data["chat_id"] = chat_id
    meta = data.get("meta", {})
    turns = data.get("turns", [])

    try:
        # Upsert the session row
        session_rows = sb.upsert(
            "chat_sessions",
            {"chat_id": chat_id, "meta": meta, "updated_at": "now()"},
            on_conflict="chat_id",
        )
        if not session_rows:
            raise HTTPException(status_code=500, detail="failed to upsert chat session")
        session_id = session_rows[0]["id"]

        # Delete old turns for this session, then insert new ones
        sb.delete("chat_turns", {"chat_session_id": f"eq.{session_id}"})

        if turns:
            turn_rows = []
            for turn in turns:
                turn_rows.append({
                    "chat_session_id": session_id,
                    "turn_id": str(turn.get("turn_id", "")),
                    "role": turn.get("role", "user"),
                    "content": turn.get("content", ""),
                    "created_at": turn.get("created_at"),
                    "model": turn.get("model"),
                    "tool_name": turn.get("tool_name"),
                    "attachments": turn.get("attachments", []),
                })
            sb.insert("chat_turns", turn_rows)

    except HTTPException:
        raise
    except Exception as exc:
        log.error("Failed to save chat %s to Supabase: %s", chat_id, exc)
        raise HTTPException(status_code=500, detail="failed to save chat")

    return {"ok": True, "chat_id": chat_id}
