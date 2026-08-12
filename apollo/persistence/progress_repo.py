"""Repository for course-scoped student progress rows.

Two public functions:
  - load_progress: return the user's per-course progress row, creating a default
    (0 XP, level 1) row if missing.
  - apply_xp: add xp_delta ATOMICALLY, recompute level from the returned total,
    stamp last_level_up_at on level change, and return a before/after summary
    suitable for the Done response's `xp_earned` / `level_before` /
    `level_after` / `level_up` fields.

Both functions commit. Callers should not wrap them in a nested
transaction — handle_done commits separately after updating the
problem attempt + session phase.

M2 (P3.4 concurrency memo §7): both functions used to be racy. `apply_xp` was a
read-modify-write in Python (`xp_after = xp_before + xp_delta`) with no lock and
no version column, so two concurrent Dones lost one award; `load_progress` was
check-then-insert, so the FIRST graded attempt in a course raced into an
`IntegrityError` on the composite PK — a 500 AFTER the grade had committed.
`student_progress` has no per-award row and no idempotency key (PK is
`(user_id, course_id)`, a bare mutable counter), and it commits BEFORE the
artifact's unique constraint fires, so that constraint cannot shield it. Both
fixes are local and migration-free."""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Dict

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from apollo.overseer.xp import level_from_xp
from apollo.persistence.models import StudentProgress


def _insert_for(db: AsyncSession):
    """Return the dialect-specific `insert()` construct that supports
    `ON CONFLICT DO NOTHING`.

    Both Postgres (production) and SQLite (the unit suite) accept the same
    `ON CONFLICT (…) DO NOTHING` syntax, but SQLAlchemy exposes it only on the
    per-dialect insert constructs — the generic `sqlalchemy.insert()` has no
    `on_conflict_do_nothing`. Dispatching here keeps ONE code path under test in
    both harnesses instead of a Postgres-only branch the unit suite never runs.
    """
    if db.get_bind().dialect.name == "sqlite":
        from sqlalchemy.dialects.sqlite import insert as _insert
    else:
        from sqlalchemy.dialects.postgresql import insert as _insert
    return _insert


async def _ensure_row(db: AsyncSession, *, user_id: str, course_id: int) -> None:
    """Idempotently materialize the (user, course) progress row.

    `DO NOTHING` rather than check-then-insert: two concurrent first-ever awards
    both see "no row" and both insert, and the loser used to raise
    `IntegrityError` straight out of `apply_xp`. Does NOT commit — the caller
    owns the transaction boundary.
    """
    insert = _insert_for(db)
    await db.execute(
        insert(StudentProgress)
        .values(user_id=user_id, course_id=course_id, xp_total=0, level=1)
        .on_conflict_do_nothing(index_elements=["user_id", "course_id"])
    )


async def load_progress(
    *, db: AsyncSession, user_id: str, course_id: int
) -> StudentProgress:
    row = None
    await _ensure_row(db, user_id=user_id, course_id=course_id)
    await db.commit()
    # `populate_existing` so a row already in this session's identity map (e.g.
    # one `apply_xp` just incremented with a Core UPDATE) is refreshed from the
    # database rather than served stale — `expire_on_commit=False` means a
    # commit never refreshes it on its own.
    row = (
        await db.execute(
            select(StudentProgress)
            .where(
                StudentProgress.user_id == user_id,
                StudentProgress.course_id == course_id,
            )
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    return row


async def apply_xp(
    *,
    db: AsyncSession,
    user_id: str,
    course_id: int,
    xp_delta: int,
) -> Dict[str, Any]:
    if xp_delta < 0:
        raise ValueError(f"xp_delta must be non-negative; got {xp_delta}")

    await _ensure_row(db, user_id=user_id, course_id=course_id)

    # THE atomic award. `xp_after` is the database's post-increment total, so
    # every field below is derived from a value no concurrent writer can have
    # invalidated — nothing is computed from a stale Python read.
    xp_after = int(
        (
            await db.execute(
                update(StudentProgress)
                .where(
                    StudentProgress.user_id == user_id,
                    StudentProgress.course_id == course_id,
                )
                .values(xp_total=StudentProgress.xp_total + xp_delta)
                .returning(StudentProgress.xp_total)
                .execution_options(synchronize_session=False)
            )
        ).scalar_one()
    )
    xp_before = xp_after - xp_delta
    level_before = level_from_xp(xp_before)
    level_after = level_from_xp(xp_after)
    level_up = level_after > level_before

    # `level` is a pure function of `xp_total`, so writing it back is safe as
    # long as it can only RATCHET UP: the `level < :level_after` predicate stops
    # a slower concurrent award (which computed a lower level from a smaller
    # total) from writing the level backwards. XP only grows (`xp_delta >= 0`),
    # so the level is monotone by construction.
    values: dict[str, Any] = {"level": level_after}
    if level_up:
        values["last_level_up_at"] = datetime.now(UTC)
    await db.execute(
        update(StudentProgress)
        .where(
            StudentProgress.user_id == user_id,
            StudentProgress.course_id == course_id,
            StudentProgress.level < level_after,
        )
        .values(**values)
        .execution_options(synchronize_session=False)
    )

    await db.commit()

    return {
        "xp_before": xp_before,
        "xp_after": xp_after,
        "level_before": level_before,
        "level_after": level_after,
        "level_up": level_up,
    }
