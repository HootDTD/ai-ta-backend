"""WU-5A2 §6.4 step 16/17 — the Layer-3 transaction owner + NO-FALLBACK fork.

The thin handler half of the all-or-nothing belief write. It re-derives the
``canonical_key -> entity_id`` map (course-scoped), calls the FLUSH-ONLY
``persist_learner_update``, and OWNS the single ``commit()`` — the atomic
boundary for the ``apollo_mastery_events`` appends + ``apollo_learner_state``
upserts (a crash between them rolls back BOTH tables).

NO-FALLBACK (mirrors ``done_grading._set_pending_and_commit``): the OLD grade/XP
and the shadow run/findings are ALREADY committed before this runs, so on ANY
exception inside the Layer-3 txn we roll back the Layer-3 work, set
``attempt.learner_update_pending = True``, commit THAT flag in a separate tiny
txn, and RE-RAISE the original error. The student grade (``attempt.result ==
"graded"``) is NEVER voided.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from apollo.handlers.done_grading import ShadowGradeResult
from apollo.knowledge_graph.canon_projection import load_entity_specs
from apollo.learner_model.persistence import (
    LearnerUpdateResult,
    persist_learner_update,
)
from apollo.persistence.models import ApolloSession, ProblemAttempt


async def _set_pending_and_commit(
    db: AsyncSession, attempt: ProblemAttempt
) -> None:
    """NO-FALLBACK: flag the attempt for a Layer-3 retry and commit ONLY that
    flag (the grade/XP + shadow run are already durable)."""
    attempt.learner_update_pending = True
    await db.commit()


async def run_learner_update(
    db: AsyncSession,
    *,
    sess: ApolloSession,
    attempt: ProblemAttempt,
    shadow: ShadowGradeResult,
    done_ts: datetime,
    parser_confidence: float,
) -> LearnerUpdateResult | None:
    """Re-derive the entity map, persist the §3 belief update (flush-only), and
    commit — the all-or-nothing boundary. On ANY failure inside the txn: roll
    back, set ``attempt.learner_update_pending = True``, commit THAT flag in a
    separate txn, and RE-RAISE the original error (NEVER voids the grade)."""
    try:
        # Course-scoped entity map: spec.canonical_key -> spec.key (the surrogate
        # apollo_kg_entities.id). concept_id is the precise scope; load_entity_specs
        # falls back to search_space_id-only when concept_id is None (pre-cutover).
        specs = await load_entity_specs(
            db,
            search_space_id=sess.search_space_id,
            concept_id=sess.concept_id,
        )
        canon_key_by_canonical_key = {spec.canonical_key: spec.key for spec in specs}

        result = await persist_learner_update(
            db,
            sess=sess,
            attempt=attempt,
            shadow=shadow,
            done_ts=done_ts,
            parser_confidence=parser_confidence,
            canon_key_by_canonical_key=canon_key_by_canonical_key,
        )
        await db.commit()  # the single all-or-nothing boundary
        return result
    except Exception:
        # Roll back the partial Layer-3 work, then flag for retry in a SEPARATE
        # txn (the grade/XP + shadow run stay committed) and re-raise.
        await db.rollback()
        await _set_pending_and_commit(db, attempt)
        raise
