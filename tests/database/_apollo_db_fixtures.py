"""Shared real-PG seed helpers for Apollo database tests.

Provides ``seed_attempt_chain``: inserts the minimal FK parent rows needed by
Apollo tables that reference ``apollo_problem_attempts`` (and transitively
``apollo_sessions`` / ``aita_search_spaces``). Returns a frozen dataclass so
callers can access ``attempt_id``, ``session_id``, ``user_id``,
``search_space_id``, and ``concept_id`` by name.

Extracted from the inline seeding pattern in
``tests/database/test_apollo_comparison_run_persistence.py``; kept here so
multiple test modules can share the chain without duplicating code.
"""

from __future__ import annotations

import dataclasses
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from apollo.persistence.models import TutoringSession, ProblemAttempt
from database.models import Course


@dataclasses.dataclass(frozen=True)
class AttemptChain:
    """Ids of the parent rows created by ``seed_attempt_chain``."""

    attempt_id: int
    session_id: int
    user_id: str
    search_space_id: int
    concept_id: int | None  # None when no concept is seeded


async def seed_attempt_chain(db: AsyncSession) -> AttemptChain:
    """Create Course -> TutoringSession -> ProblemAttempt; return ids.

    ``user_id`` is a fresh UUID per call so the unique-active-session index
    (``ix_apollo_sessions_unique_active_per_user``) never collides across tests.
    ``concept_id`` is ``None`` because no Subject/Concept is seeded here; the
    FK on ``apollo_clarifications.concept_id`` is nullable (ON DELETE SET NULL),
    so callers can pass ``None`` freely.
    """
    user_id = str(uuid.uuid4())
    slug = f"course-{uuid.uuid4().hex[:8]}"

    space = Course(name=f"Course {slug}", slug=slug, subject_name="Physics")
    db.add(space)
    await db.flush()

    session = TutoringSession(user_id=user_id, search_space_id=space.id)
    db.add(session)
    await db.flush()

    attempt = ProblemAttempt(session_id=session.id, problem_id="p1", difficulty="easy")
    db.add(attempt)
    await db.flush()

    return AttemptChain(
        attempt_id=int(attempt.id),
        session_id=int(session.id),
        user_id=user_id,
        search_space_id=int(space.id),
        concept_id=None,
    )
