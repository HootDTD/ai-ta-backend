"""Persistence for the clarification loop. Idempotent asked_waiting writes (one
follow-up per idea via the UNIQUE(attempt_id, node_id) constraint); terminal
outcome recording; confirmed-resolution loading for grading."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from apollo.persistence.models import Clarification


async def write_asked_waiting(
    db: AsyncSession,
    *,
    attempt_id: int,
    session_id: int,
    user_id: str,
    search_space_id: int,
    concept_id: int | None,
    node_id: str,
    candidate_key: str,
    probe_question: str,
    original_statement: str,
    asked_turn: int,
) -> None:
    """Insert an asked_waiting row; no-op if this (attempt, node) already has one."""
    stmt = (
        pg_insert(Clarification)
        .values(
            attempt_id=attempt_id,
            session_id=session_id,
            user_id=user_id,
            search_space_id=search_space_id,
            concept_id=concept_id,
            node_id=node_id,
            candidate_key=candidate_key,
            state="asked_waiting",
            probe_question=probe_question,
            original_statement=original_statement,
            asked_turn=asked_turn,
        )
        .on_conflict_do_nothing(constraint="apollo_clarifications_attempt_node_uniq")
    )
    await db.execute(stmt)


async def load_asked_waiting(db: AsyncSession, *, attempt_id: int) -> list[Clarification]:
    rows = await db.execute(
        select(Clarification).where(
            Clarification.attempt_id == attempt_id,
            Clarification.state == "asked_waiting",
        )
    )
    return list(rows.scalars().all())


async def record_outcome(
    db: AsyncSession,
    *,
    clarification_id: int,
    state: str,
    clarification_text: str | None,
    answered_turn: int,
) -> None:
    row = (
        await db.execute(select(Clarification).where(Clarification.id == clarification_id))
    ).scalar_one()
    row.state = state
    row.clarification_text = clarification_text
    row.answered_turn = answered_turn
    row.updated_at = datetime.now(UTC)


async def load_confirmed_resolutions(db: AsyncSession, *, attempt_id: int) -> dict[str, str]:
    rows = await db.execute(
        select(Clarification.node_id, Clarification.candidate_key).where(
            Clarification.attempt_id == attempt_id,
            Clarification.state == "confirmed",
        )
    )
    return {node_id: candidate_key for node_id, candidate_key in rows.all()}
