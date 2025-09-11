from __future__ import annotations

"""Custom chat loader function for AI-use reports (Option B).

Expose `load_chat(chat_id: str) -> dict` and point `CHAT_LOADER_FUNC`
to `backend.chats.loader:load_chat` in your environment.

This implementation reads a transcript JSON from `$CHAT_STORE_DIR/{chat_id}.json`.
It validates the basic shape required by `build_evidence_pack`.
"""

import json
import os
from typing import Any, Dict


def _require_env(name: str) -> str:
    v = os.getenv(name)
    if not v:
        raise ValueError(f"{name} not configured")
    return v


def load_chat(chat_id: str) -> Dict[str, Any]:
    store = _require_env("CHAT_STORE_DIR")
    path = os.path.join(store, f"{chat_id}.json")
    if not os.path.exists(path):
        raise ValueError("chat not found")

    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    # Basic normalization/validation so downstream code is stable
    if not isinstance(data, dict):
        raise ValueError("malformed chat payload")
    turns = data.get("turns")
    if not isinstance(turns, list):
        raise ValueError("chat has no turns")

    # Ensure minimum fields exist
    for i, t in enumerate(turns):
        if not isinstance(t, dict):
            raise ValueError(f"turn {i} malformed")
        t.setdefault("turn_id", str(i + 1))
        t.setdefault("role", "user")
        t.setdefault("content", "")
        t.setdefault("created_at", "")
        t.setdefault("model", None)
        t.setdefault("tool_name", None)
        t.setdefault("attachments", [])

    # Echo original meta if present
    meta = data.get("meta") or {}
    return {
        "chat_id": data.get("chat_id") or chat_id,
        "meta": meta,
        "turns": turns,
    }

