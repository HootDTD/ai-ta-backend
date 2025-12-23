from __future__ import annotations

import json
import os
from typing import Any, Dict

from fastapi import APIRouter, HTTPException

from ..config import get_runtime_dir

router = APIRouter()


@router.post("/chats/{chat_id}")
def save_chat(chat_id: str, payload: Dict[str, Any]):
    """Save a chat transcript to $CHAT_STORE_DIR/{chat_id}.json.

    Payload shape: {"chat_id": str, "meta": {...}, "turns": [...]}.
    """
    store = os.getenv("CHAT_STORE_DIR")
    if not store:
        store = str(get_runtime_dir() / "chats")
    os.makedirs(store, exist_ok=True)
    path = os.path.join(store, f"{chat_id}.json")

    data = dict(payload or {})
    data["chat_id"] = chat_id
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
    except Exception:
        raise HTTPException(status_code=500, detail="failed to save chat")
    return {"ok": True, "chat_id": chat_id}
