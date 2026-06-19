"""WU-5A2 §6.4 step 16/17 — the all-or-nothing Layer-3 belief write seam.

The PERSISTENCE half of the §3 Bayesian belief update. It maps the frozen
WU-5A1 pure core (``apply_event`` / ``event_to_row_specs``) + the WU-4B2 event
converter (``convert_findings_to_events``) onto the two Layer-3 tables
(``apollo_mastery_events`` append + ``apollo_learner_state`` upsert), FLUSH-ONLY
(the caller — ``run_learner_update`` — owns the single ``commit()`` and the
all-or-nothing boundary).

Mirrors ``apollo/grading/persistence.py`` (WU-4B3): PURE ``*_orm_from_spec``
helpers + a thin async write seam, supersede-by-DELETE, ``db.flush()`` (no
commit). The load-bearing differences vs the runs/findings persist:

  * **convert_findings_to_events is called HERE (and ONLY here)** — step 16. The
    WU-4C1 ``done_grading.py`` tripwire (``test_no_mastery_events_written``)
    stays green. An abstained / empty conversion writes NOTHING.
  * **canonical_key -> entity_id JOIN** via the injected
    ``canon_key_by_canonical_key`` map (``{spec.canonical_key: spec.key}``). An
    event whose key is unmapped is SKIPPED (recorded in ``skipped_unmapped``),
    NEVER inserted with a NULL ``entity_id`` (both columns are NOT NULL FKs).
  * **SUPERSEDE recompute base = the EVENT LOG, never the self-mutated state
    row** (finding #6). On a retry of the SAME attempt the whole attempt's events
    are DELETEd first, then each affected entity's recompute base is the
    ``posterior_belief`` of the latest event row from a DIFFERENT attempt (else
    cold-start). Reading the base off ``LearnerState.belief`` would double-count
    this attempt's first-run mutation.
  * **SELECT ... FOR UPDATE** the prior learner-state rows before the
    read-modify-write (a janitor retry racing a live Done must not clobber a
    posterior).
  * **evidence_count INCREMENTS on upsert; 1 only on a fresh insert.**
  * **last_evidence_at / updated_at = done_ts** (the SAME instant Neo4j
    ``graded_at`` carries via ``stamp_graded_at(ts=done_ts)``).

Builds NEW value objects, never mutates inputs.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import delete, desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from apollo.grading.events import convert_findings_to_events
from apollo.handlers.done_grading import ShadowGradeResult
from apollo.learner_model.update import apply_event, event_to_row_specs
from apollo.persistence.models import (
    ApolloSession,
    LearnerState,
    MasteryEvent,
    ProblemAttempt,
)

# v1 comparison_confidence (§3 line 432-435): grader_confidence is the shadow
# normalization_confidence scaled by 1.0 (the per-comparison confidence is a
# later refinement; folding it now would double-discount the §3 damper).
_COMPARISON_CONFIDENCE: float = 1.0


@dataclass(frozen=True)
class LearnerUpdateResult:
    """Immutable summary of one Layer-3 persist. ``abstained`` is True ONLY when
    ``convert_findings_to_events`` returned ``()`` (no events to write at all)."""

    events_written: int                 # rows appended to apollo_mastery_events
    states_upserted: int                # distinct (user, search_space, entity) upserts
    skipped_unmapped: tuple[str, ...]   # canonical_keys with no entity_id (event skipped)
    abstained: bool                     # True when convert_findings_to_events() == ()


def _mastery_event_orm_from_spec(spec) -> MasteryEvent:
    """Build the ``apollo_mastery_events`` ORM row from a ``MasteryEventRowSpec``
    (belief tuples list-ified for the ``REAL[]`` columns; node-id tuple list-ified
    for the JSONB column)."""
    return MasteryEvent(
        user_id=spec.user_id,
        search_space_id=spec.search_space_id,
        entity_id=spec.entity_id,
        attempt_id=spec.attempt_id,
        event_kind=spec.event_kind,
        score=spec.score,
        misconception_code=spec.misconception_code,
        parser_confidence=spec.parser_confidence,
        grader_confidence=spec.grader_confidence,
        negotiation_move=spec.negotiation_move,
        reference_step_id=spec.reference_step_id,
        prior_belief=list(spec.prior_belief),
        posterior_belief=list(spec.posterior_belief),
        mastery_after=spec.mastery_after,
        dt_days_since_last=spec.dt_days_since_last,
        evidence_node_ids=list(spec.evidence_node_ids),
    )


async def _recompute_base_for_entity(
    db: AsyncSession,
    *,
    user_id: str,
    search_space_id: int,
    entity_id: int,
    attempt_id: int,
) -> tuple[float, float, float] | None:
    """The recompute base for ``entity_id`` = the ``posterior_belief`` of the
    latest ``apollo_mastery_events`` row for this (user, entity) from a DIFFERENT
    attempt (``attempt_id IS DISTINCT FROM`` this one). ``None`` when there is no
    prior-attempt event (cold-start — ``apply_event`` falls back to
    ``COLD_START_PRIOR``).

    NEVER reads the base off ``LearnerState.belief``: on a retry that row may
    already carry THIS attempt's first-run mutation, and reusing it would
    double-count (finding #6 / the HIGH supersede-double-count risk)."""
    row = (
        await db.execute(
            select(MasteryEvent.posterior_belief)
            .where(
                MasteryEvent.user_id == user_id,
                MasteryEvent.search_space_id == search_space_id,
                MasteryEvent.entity_id == entity_id,
                MasteryEvent.attempt_id.is_distinct_from(attempt_id),
            )
            .order_by(desc(MasteryEvent.created_at), desc(MasteryEvent.id))
            .limit(1)
        )
    ).scalar_one_or_none()
    if row is None:
        return None
    return tuple(float(x) for x in row)


async def _lock_prior_state(
    db: AsyncSession,
    *,
    user_id: str,
    search_space_id: int,
    entity_id: int,
) -> LearnerState | None:
    """``SELECT ... FOR UPDATE`` the prior ``LearnerState`` row for this
    (user, course, entity), or ``None`` when no row exists yet."""
    return (
        await db.execute(
            select(LearnerState)
            .where(
                LearnerState.user_id == user_id,
                LearnerState.search_space_id == search_space_id,
                LearnerState.entity_id == entity_id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()


def _upsert_learner_state(
    db: AsyncSession,
    *,
    prior_state: LearnerState | None,
    state_spec,
    user_id: str,
    search_space_id: int,
    entity_id: int,
    done_ts: datetime,
) -> None:
    """Insert a fresh ``LearnerState`` (evidence_count=1) or upsert the locked
    prior row (evidence_count INCREMENT). Runs AFTER the ``MasteryEvent`` append,
    inside the SAME txn — the all-or-nothing boundary (the caller commits)."""
    if prior_state is None:
        db.add(
            LearnerState(
                user_id=user_id,
                search_space_id=search_space_id,
                entity_id=entity_id,
                belief=list(state_spec.belief),
                mastery=state_spec.mastery,
                confidence=state_spec.confidence,
                misconception_code=state_spec.misconception_code,
                evidence_count=state_spec.evidence_count,  # fresh insert -> 1
                last_evidence_at=done_ts,
                updated_at=done_ts,
            )
        )
        return
    # Upsert: INCREMENT evidence_count (never blindly write the spec default
    # literal 1 onto an existing row).
    prior_state.belief = list(state_spec.belief)
    prior_state.mastery = state_spec.mastery
    prior_state.confidence = state_spec.confidence
    prior_state.misconception_code = state_spec.misconception_code
    prior_state.evidence_count = prior_state.evidence_count + 1
    prior_state.last_evidence_at = done_ts
    prior_state.updated_at = done_ts


async def persist_learner_update(
    db: AsyncSession,
    *,
    sess: ApolloSession,
    attempt: ProblemAttempt,
    shadow: ShadowGradeResult,
    done_ts: datetime,
    parser_confidence: float,
    canon_key_by_canonical_key: Mapping[str, int],
) -> LearnerUpdateResult:
    """Persist the §3 belief update for this attempt, FLUSH-ONLY (the caller owns
    the txn boundary). Steps (§6.4 step 16 + step 17):

    1. ``convert_findings_to_events`` (step 16). ``()`` -> write nothing,
       ``LearnerUpdateResult(0, 0, (), abstained=True)``.
    2. Attempt-wide supersede: ``DELETE FROM apollo_mastery_events WHERE
       attempt_id = :attempt_id`` (covers a misconception->corrected kind change
       across runs). Same txn.
    3. Per event: resolve ``canonical_key -> entity_id`` (unmapped -> SKIP,
       recorded in ``skipped_unmapped``); ``SELECT ... FOR UPDATE`` the prior
       state; recompute the base from the EVENT LOG (NOT the state row); WU-5A1
       ``apply_event``; ``event_to_row_specs`` with the resolved ids; append the
       ``MasteryEvent`` row + upsert the ``LearnerState`` (evidence_count
       INCREMENT, last_evidence_at = done_ts).
    4. ``db.flush()`` (no commit).
    """
    events = convert_findings_to_events(
        shadow.audited,
        opposes_map=shadow.opposes_map,
        turn_order=shadow.turn_order,
    )
    if not events:
        return LearnerUpdateResult(
            events_written=0, states_upserted=0, skipped_unmapped=(), abstained=True
        )

    grader_confidence = shadow.normalization_confidence * _COMPARISON_CONFIDENCE

    # Attempt-wide supersede FIRST: a retry of this attempt drops ALL its prior
    # events (every entity/kind) so a kind change (misconception->corrected) does
    # not leave residue. Same txn as the re-inserts below -> atomic.
    await db.execute(
        delete(MasteryEvent).where(MasteryEvent.attempt_id == attempt.id)
    )

    skipped_unmapped: list[str] = []
    states_upserted = 0
    events_written = 0

    for event in events:
        entity_id = canon_key_by_canonical_key.get(event.canonical_key)
        if entity_id is None:
            # Unmapped (e.g. empty pre-promotion specs): SKIP — both entity_id
            # columns are NOT NULL FKs, never insert a stub.
            skipped_unmapped.append(event.canonical_key)
            continue

        prior_state = await _lock_prior_state(
            db,
            user_id=sess.user_id,
            search_space_id=sess.search_space_id,
            entity_id=entity_id,
        )
        prior_belief = await _recompute_base_for_entity(
            db,
            user_id=sess.user_id,
            search_space_id=sess.search_space_id,
            entity_id=entity_id,
            attempt_id=attempt.id,
        )
        prior_last_evidence_at = (
            prior_state.last_evidence_at if prior_state is not None else None
        )

        update = apply_event(
            event,
            prior_belief=prior_belief,
            prior_last_evidence_at=prior_last_evidence_at,
            parser_confidence=parser_confidence,
            grader_confidence=grader_confidence,
            done_ts=done_ts,
        )
        mastery_spec, state_spec = event_to_row_specs(
            event,
            update,
            user_id=sess.user_id,
            search_space_id=sess.search_space_id,
            entity_id=entity_id,
            attempt_id=attempt.id,
        )

        db.add(_mastery_event_orm_from_spec(mastery_spec))
        events_written += 1

        _upsert_learner_state(
            db,
            prior_state=prior_state,
            state_spec=state_spec,
            user_id=sess.user_id,
            search_space_id=sess.search_space_id,
            entity_id=entity_id,
            done_ts=done_ts,
        )
        states_upserted += 1

    await db.flush()

    return LearnerUpdateResult(
        events_written=events_written,
        states_upserted=states_upserted,
        skipped_unmapped=tuple(skipped_unmapped),
        abstained=False,
    )
