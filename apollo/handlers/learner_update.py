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

import os
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from apollo.handlers.done_grading import ShadowGradeResult
from apollo.knowledge_graph.canon_projection import load_entity_specs
from apollo.learner_model.belief import COLD_START_PRIOR, NO_OP_LIKELIHOOD
from apollo.learner_model.decay import decay_toward_prior
from apollo.learner_model.negotiation import (
    entity_negotiation_move,
    load_student_moves,
    negotiation_multiplier,
    suppresses_hedge,
)
from apollo.learner_model.persistence import (
    LearnerUpdateResult,
    persist_learner_update,
)
from apollo.persistence.models import ApolloSession, ProblemAttempt

# WU-5B1 §3 Step 0 — the between-session decay gate. Default OFF EVERYWHERE
# (mirrors done.py's flag-helper shape). When ON, run_learner_update builds the
# decay prior_transform closure and threads it into persist_learner_update; when
# OFF it passes None (byte-identical to WU-5A2). The flag is read INTERNALLY so
# run_learner_update's public signature stays unchanged (the done.py call site is
# byte-identical).
_LEARNER_DECAY_FLAG: str = "APOLLO_LEARNER_DECAY_ENABLED"


def _learner_decay_enabled() -> bool:
    return os.environ.get(_LEARNER_DECAY_FLAG, "").lower() in ("1", "true", "yes")


# WU-5B2 §3 — the negotiation-multiplier gate. Default OFF EVERYWHERE (mirrors
# _learner_decay_enabled). When ON, run_learner_update reads the student moves
# ONCE + builds the per-entity multiplier/move closures and threads them into
# persist_learner_update; when OFF it passes None (identity -> byte-identical to
# WU-5B1). Read INTERNALLY so run_learner_update's public signature is unchanged.
_LEARNER_NEGOTIATION_FLAG: str = "APOLLO_LEARNER_NEGOTIATION_ENABLED"


def _learner_negotiation_enabled() -> bool:
    return os.environ.get(_LEARNER_NEGOTIATION_FLAG, "").lower() in ("1", "true", "yes")


async def _set_pending_and_commit(db: AsyncSession, attempt: ProblemAttempt) -> None:
    """NO-FALLBACK: flag the attempt for a Layer-3 retry and commit ONLY that
    flag (the grade/XP + shadow run are already durable)."""
    attempt.learner_update_pending = True  # type: ignore[assignment]
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
            search_space_id=int(sess.search_space_id),
            concept_id=sess.concept_id,  # type: ignore[arg-type]  # nullable col
        )
        canon_key_by_canonical_key = {spec.canonical_key: spec.key for spec in specs}

        # WU-5B1 §3 Step 0: when the decay flag is ON, build the prior_transform
        # closure that decays the recompute-base prior toward COLD_START_PRIOR by
        # the integer dt_days_since_last (the dt is supplied by the persistence
        # fold — anchored to the frozen done_ts, NEVER now()). A None dt
        # (cold-start / no prior anchor) returns the prior unchanged (defensive
        # symmetry with the decay_weight clamp). OFF -> None -> byte-identical 5A2.
        prior_transform = None
        if _learner_decay_enabled():

            def prior_transform(belief, dt_days):
                if dt_days is None:
                    return belief
                return decay_toward_prior(belief, COLD_START_PRIOR, dt_days)

        # WU-5B2 §3: when the negotiation flag is ON, read this attempt's student
        # moves ONCE (actor='student', latest-wins) and build the per-entity
        # closures: the representative move (persisted on each event row) and the
        # likelihood multiplier (RATIFIED suppression -> identity on a
        # misconception entity). OFF -> both None -> identity -> byte-identical 5B1.
        likelihood_multiplier_for_entity = None
        negotiation_move_for_entity = None
        if _learner_negotiation_enabled():
            moves = await load_student_moves(db, attempt_id=int(attempt.id))

            def negotiation_move_for_entity(entity_events):
                return entity_negotiation_move(entity_events, moves)

            def likelihood_multiplier_for_entity(entity_events):
                # RATIFIED suppression (#4b): a misconception entity forces identity
                # x1.0 — a negotiation move must NOT dilute a misconception.
                if suppresses_hedge(entity_events):
                    return NO_OP_LIKELIHOOD
                return negotiation_multiplier(entity_negotiation_move(entity_events, moves))

        result = await persist_learner_update(
            db,
            sess=sess,
            attempt=attempt,
            shadow=shadow,
            done_ts=done_ts,
            parser_confidence=parser_confidence,
            canon_key_by_canonical_key=canon_key_by_canonical_key,
            prior_transform=prior_transform,
            likelihood_multiplier_for_entity=likelihood_multiplier_for_entity,
            negotiation_move_for_entity=negotiation_move_for_entity,
        )
        await db.commit()  # the single all-or-nothing boundary
        return result
    except Exception:
        # Roll back the partial Layer-3 work, then flag for retry in a SEPARATE
        # txn (the grade/XP + shadow run stay committed) and re-raise.
        await db.rollback()
        await _set_pending_and_commit(db, attempt)
        raise
