"""WU-6A1 — the learner-profile READ path over ``apollo_learner_state``.

The genuinely-first read over the Layer-3 belief table. The only pre-existing
read, ``apollo/learner_model/persistence.py:_lock_prior_state``, is a
``SELECT ... FOR UPDATE`` write-lock scoped to a single ``entity_id`` and is NOT
reused here. ``read_learner_profile`` is a pure, no-lock, course-scoped read:

* It loads this concept's ``apollo_kg_entities`` (the id<->canonical_key maps).
* It loads the learner's ``apollo_learner_state`` rows scoped by BOTH
  ``user_id`` AND ``search_space_id`` AND ``entity_id IN (concept entities)`` —
  the spec §1.4 per-classroom isolation invariant: mastery never crosses
  courses.
* It loads the WITHIN-CONCEPT ``apollo_entity_prereqs`` edges (BOTH endpoints
  among this concept's entities), so ``prereq_edges`` only ever references
  entities present in the id<->key maps — a cross-concept edge is excluded.

Columns are returned VERBATIM (no belief recompute — ``belief.py`` is NOT
imported). ``is_empty`` reflects the PROD cold-start path: ``apollo_learner_state``
is populated only while ``APOLLO_GRAPH_SIM_LAYER3_ENABLED`` is ON (OFF in prod),
so the table is empty in prod; on cold-start only ``by_canonical_key`` is empty —
the structural maps/edges still populate.

This module computes NO scoring threshold and NO ``prereqs_mastered`` semantic
(those are WU-6A2's ``personalization_select.py``); it imports NOTHING from any
``personalization_select`` module (one-way dependency arrow, no cycle). At most
three scoped queries, none inside a loop (no N+1). NO migration, NO Neo4j, NO LLM.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apollo.persistence.models import EntityPrereq, KGEntity, LearnerState

__all__ = ["EntityProfile", "LearnerProfile", "read_learner_profile"]


@dataclass(frozen=True)
class EntityProfile:
    """RAW stored columns for one (user, course, entity) learner-state row.

    Read VERBATIM — NO belief recompute (``belief.py`` is NOT imported).
    """

    entity_id: int
    canonical_key: str
    mastery: float
    confidence: float
    misconception_code: str | None


@dataclass(frozen=True)
class LearnerProfile:
    """Immutable snapshot of one learner's profile for one concept in one course.

    ``by_canonical_key`` holds ONLY the entities with a present learner_state row
    (absence means "cold", never zero-filled). ``prereq_edges`` are RAW
    ``(from_entity_id, to_entity_id)`` tuples, deterministically sorted, and
    WITHIN-CONCEPT — every endpoint is one of this concept's entities, so each
    resolves through the id<->key maps (a cross-concept edge is excluded). The two
    id<->key maps cover this concept's full entity inventory. ``is_empty`` is True
    iff ZERO learner_state rows matched (the PROD cold-start path).
    """

    by_canonical_key: Mapping[str, EntityProfile]
    prereq_edges: tuple[tuple[int, int], ...]
    entity_id_by_key: Mapping[str, int]
    key_by_entity_id: Mapping[int, str]
    is_empty: bool


async def read_learner_profile(
    db: AsyncSession,
    *,
    user_id: str,
    search_space_id: int,
    concept_id: int,
) -> LearnerProfile:
    """Read one learner's course-scoped profile for ``concept_id``.

    Pure read: no lock, no commit, no flush, no mutation of ``db`` or inputs.
    Returns a fully-immutable ``LearnerProfile``. On cold-start (no state rows)
    ``by_canonical_key`` is empty but the maps/edges still populate; on an unknown
    concept (no entities) every field is empty and ``is_empty`` is True.
    """
    # 1. This concept's entity inventory (1 query) -> id<->key maps.
    entity_rows = (
        await db.execute(
            select(KGEntity.id, KGEntity.canonical_key).where(KGEntity.concept_id == concept_id)
        )
    ).all()

    entity_id_by_key: dict[str, int] = {key: ent_id for ent_id, key in entity_rows}
    key_by_entity_id: dict[int, str] = {ent_id: key for ent_id, key in entity_rows}
    concept_entity_ids = list(key_by_entity_id)

    # Unknown concept: no entities -> short-circuit before the degenerate
    # ``IN ()`` state/prereq queries.
    if not concept_entity_ids:
        return LearnerProfile(
            by_canonical_key={},
            prereq_edges=(),
            entity_id_by_key={},
            key_by_entity_id={},
            is_empty=True,
        )

    # 2. Course-scoped learner_state rows (1 query). Belt-and-suspenders scoping:
    #    user_id AND search_space_id AND entity_id IN (this concept's entities).
    state_rows = (
        await db.execute(
            select(
                LearnerState.entity_id,
                LearnerState.mastery,
                LearnerState.confidence,
                LearnerState.misconception_code,
            ).where(
                LearnerState.user_id == user_id,
                LearnerState.search_space_id == search_space_id,
                LearnerState.entity_id.in_(concept_entity_ids),
            )
        )
    ).all()

    by_canonical_key: dict[str, EntityProfile] = {}
    for entity_id, mastery, confidence, misconception_code in state_rows:
        canonical_key = key_by_entity_id[entity_id]
        by_canonical_key[canonical_key] = EntityProfile(
            entity_id=entity_id,
            canonical_key=canonical_key,
            mastery=mastery,
            confidence=confidence,
            misconception_code=misconception_code,
        )

    # 3. WITHIN-CONCEPT prereq edges (1 query): BOTH endpoints among this concept's
    #    entities, so ``prereq_edges`` is self-contained — every id resolves through
    #    the id<->key maps. A cross-concept edge (one foreign endpoint) is excluded:
    #    the within-concept wedge does not gate on out-of-concept prerequisites, and
    #    the consumer (WU-6A2) can resolve every endpoint it receives. The two
    #    ``.in_`` predicates are ANDed by the multi-arg ``.where``.
    prereq_rows = (
        await db.execute(
            select(EntityPrereq.from_entity_id, EntityPrereq.to_entity_id).where(
                EntityPrereq.from_entity_id.in_(concept_entity_ids),
                EntityPrereq.to_entity_id.in_(concept_entity_ids),
            )
        )
    ).all()

    prereq_edges = tuple(sorted((from_id, to_id) for from_id, to_id in prereq_rows))

    return LearnerProfile(
        by_canonical_key=by_canonical_key,
        prereq_edges=prereq_edges,
        entity_id_by_key=entity_id_by_key,
        key_by_entity_id=key_by_entity_id,
        is_empty=(len(state_rows) == 0),
    )
