"""Repository for apollo_student_progress rows.

Two public functions:
  - load_progress: return the user's progress row, creating a default
    (0 XP, level 1) row if missing.
  - apply_xp: add xp_delta, recompute level, stamp last_level_up_at on
    level change, and return a before/after summary suitable for the
    Done response's `xp_earned` / `level_before` / `level_after` /
    `level_up` fields.

Both functions commit. Callers should not wrap them in a nested
transaction — handle_done commits separately after updating the
problem attempt + session phase."""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Dict

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apollo.overseer.xp import level_from_xp
from apollo.persistence.models import StudentProgress


async def load_progress(*, db: AsyncSession, user_id: str) -> StudentProgress:
    row = (await db.execute(
        select(StudentProgress).where(StudentProgress.user_id == user_id)
    )).scalar_one_or_none()
    if row is not None:
        return row
    row = StudentProgress(user_id=user_id, xp_total=0, level=1)
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


async def apply_xp(
    *,
    db: AsyncSession,
    user_id: str,
    xp_delta: int,
) -> Dict[str, Any]:
    if xp_delta < 0:
        raise ValueError(f"xp_delta must be non-negative; got {xp_delta}")

    row = await load_progress(db=db, user_id=user_id)
    xp_before = row.xp_total
    level_before = row.level

    xp_after = xp_before + xp_delta
    level_after = level_from_xp(xp_after)
    level_up = level_after > level_before

    row.xp_total = xp_after
    row.level = level_after
    if level_up:
        row.last_level_up_at = datetime.now(UTC)

    await db.commit()

    return {
        "xp_before": xp_before,
        "xp_after": xp_after,
        "level_before": level_before,
        "level_after": level_after,
        "level_up": level_up,
    }
