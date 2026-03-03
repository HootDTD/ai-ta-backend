from __future__ import annotations

import uuid as uuid_pkg
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from backend.vendors import supabase_client as sb


# -------------------- Pydantic Schemas --------------------

class ToolCallMeta(BaseModel):
    name: str
    inputs_summary: str


class AIUseReport(BaseModel):
    id: str
    chat_id: str
    created_at: datetime
    style: Optional[str] = None
    length: Optional[str] = None
    markdown: Optional[str] = None
    jsonld: Optional[Any] = None
    model_fingerprint: Optional[str] = None
    tool_calls: list[ToolCallMeta] = Field(default_factory=list)
    prompt_hashes: list[str] = Field(default_factory=list)


# -------------------- Supabase CRUD --------------------

TABLE = "ai_use_reports"


def create_report(
    *,
    chat_id: str,
    style: Optional[str] = None,
    length: Optional[str] = None,
    markdown: Optional[str] = None,
    jsonld: Optional[Any] = None,
    model_fingerprint: Optional[str] = None,
    tool_calls: Optional[Any] = None,
    prompt_hashes: Optional[Any] = None,
) -> Dict[str, Any]:
    """Insert a new AI use report into Supabase and return it."""
    report_id = str(uuid_pkg.uuid4())
    data = {
        "id": report_id,
        "chat_id": chat_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "style": style,
        "length": length,
        "markdown": markdown,
        "jsonld": jsonld,
        "model_fingerprint": model_fingerprint,
        "tool_calls": tool_calls,
        "prompt_hashes": prompt_hashes,
    }
    rows = sb.insert(TABLE, data)
    return rows[0] if rows else data


def get_report(report_id: str) -> Optional[Dict[str, Any]]:
    """Fetch a single report by ID."""
    return sb.select_one(TABLE, {"id": f"eq.{report_id}"})


def list_reports(*, limit: int = 10) -> List[Dict[str, Any]]:
    """List recent reports, newest first."""
    clamped = max(1, min(limit, 100))
    return sb.select(TABLE, {
        "select": "id,chat_id,created_at,style,length",
        "order": "created_at.desc",
        "limit": str(clamped),
    })
