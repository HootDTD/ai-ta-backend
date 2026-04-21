"""GET /apollo/progress/{student_id} — surface XP + level for the UI."""
from __future__ import annotations

from typing import Any, Dict

from sqlalchemy.ext.asyncio import AsyncSession

from apollo.overseer.xp import next_tier_threshold, title_for_level
from apollo.persistence.progress_repo import load_progress


async def handle_get_progress(
    *,
    db: AsyncSession,
    student_id: str,
) -> Dict[str, Any]:
    row = await load_progress(db=db, student_id=student_id)
    return {
        "student_id": row.student_id,
        "xp_total": row.xp_total,
        "level": row.level,
        "title": title_for_level(row.level),
        "next_tier_threshold": next_tier_threshold(row.level),
    }
