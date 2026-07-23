"""Overseer.problem_selector — pick a problem from the DB problem bank.

Problems are loaded from ``app.problems`` by course and concept. Promoted
columns are reassembled and validated through the public Pydantic schema.

Deterministic: sorted by ``Problem.id`` (== ``payload['id']`` == ``problem_code``).
Refresh on every call (no caching). Raises ``PoolExhaustedError`` if no
unattempted problem at the requested difficulty remains.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apollo.errors import PoolExhaustedError
from apollo.learner_model.personalization_read import read_learner_profile
from apollo.learner_model.personalization_select import (
    personalize_selection,
    weak_teachable,
)
from apollo.overseer import personalization_flag
from apollo.persistence.models import Concept, Problem
from apollo.schemas.problem import Problem as ProblemSchema

_LOG = logging.getLogger(__name__)


async def list_problems_for_concept(
    db: AsyncSession, *, concept_id: int, search_space_id: int
) -> list[ProblemSchema]:
    """Load every teachable problem within the requested course and concept.

    WU-3B2a (§8B auto-provisioning): ONLY Tier-2 problems are returned. Tier-1 is
    auto-provisioned inventory (not yet teachable) and is excluded. This single
    predicate is the SOLE chokepoint, so it gates BOTH ``select_problem`` and
    ``select_problem_personalized`` with no separate edit.

    WU-3B2h (§8B.3 anomaly quarantine): the additional ``quarantined_at IS NULL``
    predicate excludes a problem the ``apollo.provisioning.quarantine`` sweep has
    pulled from the selectable pool (a wrong/mispaired reference solution whose
    class-wide misses concentrate on one node). The column ships in migration 030;
    this is where its filter is applied. Reversible: the sweep clears
    ``quarantined_at`` and the problem becomes selectable again."""
    rows = (
        await db.execute(
            select(Problem, Concept.slug)
            .join(Concept, Concept.id == Problem.concept_id)
            .where(
                Problem.course_id == search_space_id,
                Concept.course_id == search_space_id,
                Problem.concept_id == concept_id,
                Problem.tier == 2,
                Problem.quarantined_at.is_(None),
            )
        )
    ).all()
    problems: list[ProblemSchema] = []
    for row, concept_slug in rows:
        try:
            problems.append(
                ProblemSchema.model_validate(
                    {**row.to_pydantic_payload(concept_slug=concept_slug), "database_id": row.id}
                )
            )
        except ValidationError as exc:
            first_error = exc.errors(include_input=False)[0]
            location = ".".join(str(part) for part in first_error["loc"])
            summary = (
                f"{exc.error_count()} validation error(s); first={location}: "
                f"{first_error['msg']} [{first_error['type']}]"
            )
            _LOG.warning(
                "apollo_problem_selector_invalid_payload_skipped",
                extra={
                    "event": "apollo_problem_selector_invalid_payload_skipped",
                    "concept_id": concept_id,
                    "problem_tier": row.tier,
                    "problem_id": row.id,
                    "validation_error": summary,
                },
            )
    return sorted(problems, key=lambda p: p.id)


async def select_problem(
    db: AsyncSession,
    *,
    concept_id: int,
    search_space_id: int,
    difficulty: str,
    attempted_ids: Sequence[str | int],
) -> ProblemSchema:
    """Pick the first unattempted ``Problem`` at ``difficulty`` for ``concept_id``.

    Raises ``PoolExhaustedError`` (with ``concept_cluster_id=str(concept_id)`` for
    API back-compat) when none remain.
    """
    pool = await list_problems_for_concept(
        db, concept_id=concept_id, search_space_id=search_space_id
    )
    attempted = set(attempted_ids)
    candidates = [
        p
        for p in pool
        if p.difficulty == difficulty and p.id not in attempted and p.database_id not in attempted
    ]
    if not candidates:
        raise PoolExhaustedError(concept_cluster_id=str(concept_id), difficulty=difficulty)
    return candidates[0]


async def select_problem_personalized(
    db: AsyncSession,
    *,
    user_id: str,
    search_space_id: int,
    concept_id: int,
    difficulty: str,
    attempted_ids: Sequence[str | int],
) -> ProblemSchema:
    """The v1 session-personalization wedge (WU-6A3) wrapped around the untouched
    ``select_problem``.

    FLAG-OFF (default, incl. prod): the EXACT old path — delegates to
    ``select_problem`` with NO profile read and NO log, byte-identical to today.

    FLAG-ON: reads the candidate pool once AND the learner profile once at the
    selection seam (not per-turn, not in a candidate loop — no N+1), delegates the
    scoring + cold-start branch to the frozen WU-6A2 ``personalize_selection``, and
    emits exactly ONE structured ``event=personalized_selection`` observability log
    (the only runtime signal the wedge engaged vs degraded). On the prod cold-start
    path (empty ``apollo_learner_state``) the profile is empty and the choice is
    byte-identical to flag-OFF (``candidates[0]``). A ``PoolExhaustedError`` is
    raised byte-identically BEFORE the log (the raise precedes it).
    """
    if not personalization_flag.is_enabled():
        return await select_problem(
            db,
            concept_id=concept_id,
            search_space_id=search_space_id,
            difficulty=difficulty,
            attempted_ids=attempted_ids,
        )

    pool = await list_problems_for_concept(
        db, concept_id=concept_id, search_space_id=search_space_id
    )
    profile = await read_learner_profile(
        db,
        user_id=user_id,
        search_space_id=search_space_id,
        concept_id=concept_id,
    )
    chosen = personalize_selection(
        profile,
        pool,
        concept_id=concept_id,
        difficulty=difficulty,
        attempted_ids=attempted_ids,
    )

    # ONE structured observability log. ``weak_teachable`` is recomputed here PURELY
    # for the log's n_weak_entities + fallback_fired fields; it is the same pure,
    # in-memory function ``personalize_selection`` used internally (cheap, no IO).
    weak = weak_teachable(profile)
    _LOG.info(
        "apollo_select_problem_personalized",
        extra={
            "event": "personalized_selection",
            "personalization_enabled": True,
            "profile_is_empty": profile.is_empty,
            "n_weak_entities": len(weak),
            "chosen_problem_id": chosen.id,
            "fallback_fired": profile.is_empty or not weak,
        },
    )
    return chosen
