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
  * **ONE belief update per entity per Done (§3 steps 1-3, the SINGLE
    combined-likelihood update).** Events are GROUPED by ``entity_id`` first;
    within a group the §3 update is computed ONCE: (step 1) multiply the per-event
    RAW ``likelihood_for_event`` vectors into ONE combined ``L``; (step 2) ``damp``
    the COMBINED ``L`` ONCE with ``q = parser_confidence · grader_confidence``;
    (step 3) ONE ``bayes_update`` over the recompute base. This is NOT a per-event
    ``apply_event`` chain: the §3.1 affine damper (``q·L + (1-q)·[1,1,1]``) is NOT a
    multiplicative homomorphism, so ``damp(L1)·damp(L2) ≠ damp(L1·L2)`` for ``q<1``
    — chaining ``apply_event`` (which damps + renormalizes per event) would diverge
    from the spec. Each event still appends its OWN ``apollo_mastery_events`` row
    (the event log; the belief columns record the single base->combined transition
    that actually occurred), but ``apollo_learner_state`` is upserted ONCE per
    entity — the §3 "fires once per Done episode, never per turn" invariant.
    (Reachable today: with the structurally-empty ``opposes_map`` a CONTRADICTION +
    a COVERED on the same ``canonical_key`` both surface as standalone events for
    one entity.) The single-event case is numerically identical to ``apply_event``.
  * **SELECT ... FOR UPDATE** the prior learner-state rows before the
    read-modify-write (a janitor retry racing a live Done must not clobber a
    posterior).
  * **evidence_count INCREMENTS once per Done; 1 only on a fresh insert** (the
    increment is per entity per Done, NOT per event).
  * **last_evidence_at / updated_at = done_ts** (the SAME instant Neo4j
    ``graded_at`` carries via ``stamp_graded_at(ts=done_ts)``).
  * **prior_last_evidence_at** anchors the recorded-but-not-applied
    ``dt_days_since_last`` only; v1 applies NO decay (deferred to WU-5B), so the
    posterior is independent of this anchor — it is read off the locked prior
    state row for the v1 record.

Builds NEW value objects, never mutates inputs.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime

from sqlalchemy import delete, desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from apollo.grading.events import convert_findings_to_events
from apollo.handlers.done_grading import ShadowGradeResult
from apollo.learner_model.belief import (
    COLD_START_PRIOR,
    NO_OP_LIKELIHOOD,
    bayes_update,
    confidence_of,
    damp,
    likelihood_for_event,
    mastery_of,
    misconception_code_of,
)
from apollo.learner_model.state_model import BeliefUpdate
from apollo.learner_model.update import event_to_row_specs
from apollo.persistence.models import (
    ApolloSession,
    LearnerState,
    MasteryEvent,
    ProblemAttempt,
)
from apollo.persistence.problem_linkage import resolve_concept_problem_id

# v1 comparison_confidence (§3 line 432-435): grader_confidence is the shadow
# normalization_confidence scaled by 1.0 (the per-comparison confidence is a
# later refinement; folding it now would double-discount the §3 damper).
_COMPARISON_CONFIDENCE: float = 1.0

# WU-5B1 §3 Step 0 — the between-session decay seam (adjudication #1). A
# keyword-only ``prior_transform`` threaded through the fold; when provided it
# decays the recompute-base prior toward COLD_START_PRIOR BEFORE damp/bayes.
# Signature: ``(base_belief, dt_days) -> decayed_belief`` where ``dt_days`` is
# the integer ``dt_days_since_last`` (or ``None`` -> the transform returns the
# prior unchanged). Default ``None`` = identity = byte-identical to WU-5A2.
PriorTransform = Callable[[tuple[float, float, float], int | None], tuple[float, float, float]]

# WU-5B2 §3 negotiation multiplier seam (adjudication #4). Two identity-default
# keyword-only callables threaded through the fold (alongside ``prior_transform``):
#   - ``likelihood_multiplier_for_entity(entity_events) -> (m_misc, m_shaky, m_mastered)``
#       multiplies the COMBINED likelihood ONCE per entity BEFORE damp; identity
#       (1,1,1) when no qualifying move OR when the entity carries a misconception
#       (the RATIFIED suppression). Default ``None`` = no-op (NO_OP_LIKELIHOOD).
#   - ``negotiation_move_for_entity(entity_events) -> str | None``
#       the representative move string persisted onto each event row (OVERRIDES the
#       frozen ``MasteryEventRowSpec.negotiation_move=None``). Default ``None``.
# DECOUPLED on purpose: suppression mutes the MULTIPLIER but the move STRING is
# still persisted (so the refit corpus can later re-fit the sign).
LikelihoodMultiplierForEntity = Callable[[list], tuple[float, float, float]]
NegotiationMoveForEntity = Callable[[list], "str | None"]


@dataclass(frozen=True)
class LearnerUpdateResult:
    """Immutable summary of one Layer-3 persist. ``abstained`` is True ONLY when
    ``convert_findings_to_events`` returned ``()`` (no events to write at all)."""

    events_written: int  # rows appended to apollo_mastery_events
    states_upserted: int  # distinct (user, search_space, entity) upserts
    skipped_unmapped: tuple[str, ...]  # canonical_keys with no entity_id (event skipped)
    abstained: bool  # True when convert_findings_to_events() == ()


def _mastery_event_orm_from_spec(spec, *, concept_problem_id: int | None) -> MasteryEvent:
    """Build the ``apollo_mastery_events`` ORM row from a ``MasteryEventRowSpec``
    (belief tuples list-ified for the ``REAL[]`` columns; node-id tuple list-ified
    for the JSONB column)."""
    return MasteryEvent(
        user_id=spec.user_id,
        search_space_id=spec.search_space_id,
        entity_id=spec.entity_id,
        attempt_id=spec.attempt_id,
        concept_problem_id=concept_problem_id,
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
    a, b, c = (float(x) for x in row)
    return a, b, c


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
    prior_state.belief = list(state_spec.belief)  # type: ignore[assignment]
    prior_state.mastery = state_spec.mastery
    prior_state.confidence = state_spec.confidence
    prior_state.misconception_code = state_spec.misconception_code
    prior_state.evidence_count = prior_state.evidence_count + 1  # type: ignore[assignment]
    prior_state.last_evidence_at = done_ts  # type: ignore[assignment]
    prior_state.updated_at = done_ts  # type: ignore[assignment]


async def persist_learner_update(
    db: AsyncSession,
    *,
    sess: ApolloSession,
    attempt: ProblemAttempt,
    shadow: ShadowGradeResult,
    done_ts: datetime,
    parser_confidence: float,
    canon_key_by_canonical_key: Mapping[str, int],
    prior_transform: PriorTransform | None = None,
    likelihood_multiplier_for_entity: LikelihoodMultiplierForEntity | None = None,
    negotiation_move_for_entity: NegotiationMoveForEntity | None = None,
) -> LearnerUpdateResult:
    """Persist the §3 belief update for this attempt, FLUSH-ONLY (the caller owns
    the txn boundary). Steps (§6.4 step 16 + step 17):

    1. ``convert_findings_to_events`` (step 16). ``()`` -> write nothing,
       ``LearnerUpdateResult(0, 0, (), abstained=True)``.
    2. Attempt-wide supersede: ``DELETE FROM apollo_mastery_events WHERE
       attempt_id = :attempt_id`` (covers a misconception->corrected kind change
       across runs). Same txn.
    3. Resolve each event's ``canonical_key -> entity_id`` (unmapped -> SKIP,
       recorded in ``skipped_unmapped``) and GROUP the events by ``entity_id``
       (preserving the converter's deterministic order).
    4. Per entity group: ``SELECT ... FOR UPDATE`` the prior state ONCE; recompute
       the base from the EVENT LOG (NOT the state row); compute the §3 SINGLE
       combined-likelihood update (multiply the per-event raw likelihoods -> damp
       the COMBINED L ONCE -> one ``bayes_update`` over the base); append one
       ``MasteryEvent`` row per event (the event log records the single
       base->combined transition); upsert the ``LearnerState`` ONCE with the
       combined posterior (evidence_count INCREMENT once per Done,
       last_evidence_at = done_ts).
    5. ``db.flush()`` (no commit).
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

    # GEN-5: one problem-bank lookup per update, shared by every event row.
    # Legacy sessions/attempts can have no resolvable bank row; NULL linkage is
    # additive and must never suppress their otherwise-valid mastery events.
    concept_problem_id = None
    if sess.concept_id is not None:
        concept_problem_id = await resolve_concept_problem_id(
            db,
            concept_id=int(sess.concept_id),
            problem_code=str(attempt.problem_id),
        )

    # Attempt-wide supersede FIRST: a retry of this attempt drops ALL its prior
    # events (every entity/kind) so a kind change (misconception->corrected) does
    # not leave residue. Same txn as the re-inserts below -> atomic.
    await db.execute(delete(MasteryEvent).where(MasteryEvent.attempt_id == attempt.id))

    # Resolve canonical_key -> entity_id and GROUP events by entity (preserving
    # the converter's deterministic order). One entity may carry >1 event in a
    # single Done (e.g. a CONTRADICTION + a COVERED on the same key with the
    # empty opposes_map) — §3 step 1 folds them into ONE belief update.
    skipped_unmapped: list[str] = []
    events_by_entity: dict[int, list] = {}
    for event in events:
        entity_id = canon_key_by_canonical_key.get(event.canonical_key)
        if entity_id is None:
            # Unmapped (e.g. empty pre-promotion specs): SKIP — both entity_id
            # columns are NOT NULL FKs, never insert a stub.
            skipped_unmapped.append(event.canonical_key)
            continue
        events_by_entity.setdefault(entity_id, []).append(event)

    events_written = 0
    for entity_id, entity_events in events_by_entity.items():
        events_written += await _persist_entity_group(
            db,
            sess=sess,
            attempt=attempt,
            concept_problem_id=concept_problem_id,
            entity_id=entity_id,
            entity_events=entity_events,
            done_ts=done_ts,
            parser_confidence=parser_confidence,
            grader_confidence=grader_confidence,
            prior_transform=prior_transform,
            likelihood_multiplier_for_entity=likelihood_multiplier_for_entity,
            negotiation_move_for_entity=negotiation_move_for_entity,
        )

    await db.flush()

    return LearnerUpdateResult(
        events_written=events_written,
        states_upserted=len(events_by_entity),
        skipped_unmapped=tuple(skipped_unmapped),
        abstained=False,
    )


def _combined_belief_update(
    entity_events: list,
    *,
    prior_belief: tuple[float, float, float] | None,
    prior_last_evidence_at: datetime | None,
    parser_confidence: float,
    grader_confidence: float,
    done_ts: datetime,
    prior_transform: PriorTransform | None = None,
    likelihood_multiplier: tuple[float, float, float] = NO_OP_LIKELIHOOD,
) -> BeliefUpdate:
    """The §3 SINGLE combined-likelihood update for ALL of one entity's events in
    one Done (steps 1-3). Step 0 (WU-5B1, when ``prior_transform`` is provided):
    decay the recompute-base prior toward COLD_START_PRIOR by the integer
    ``dt_days_since_last`` BEFORE damp/bayes. Step 1: multiply the per-event RAW
    ``likelihood_for_event`` vectors into ONE ``L`` (start ``[1,1,1]``). Step 2:
    ``damp`` the COMBINED ``L`` ONCE with ``q = parser_confidence ·
    grader_confidence``. Step 3: ONE ``bayes_update`` over the (cold-start-defaulted,
    optionally decayed) base. Returns a frozen :class:`BeliefUpdate` carrying the
    single transition.

    This deliberately does NOT chain ``apply_event``: the §3.1 affine damper is not
    multiplicative-homomorphic, so per-event damp+bayes diverges from the spec for
    ``q < 1``. For a single event the result is identical to ``apply_event``.

    The misconception code surfaces against the COMBINED posterior — the last event
    (deterministic converter order) whose code clears the §3 two-step flag wins
    (``None`` when none do)."""
    # WU-5B1 hoist (adjudication #1): ``dt_days_since_last`` is computed at the TOP
    # (it feeds only the readout AND the Step-0 decay transform). A pure reordering
    # — both inputs are immutable + unmutated, so the VALUE is unchanged vs WU-5A2.
    dt_days_since_last = (
        (done_ts - prior_last_evidence_at).days
        if prior_last_evidence_at is not None
        else None
    )
    prior = prior_belief if prior_belief is not None else COLD_START_PRIOR
    if prior_transform is not None:
        # §3 Step 0 — decay the base toward COLD_START_PRIOR BEFORE damp/bayes.
        # ``BeliefUpdate.prior_belief`` then records the DECAYED prior (the
        # intended Step-0 -> Step-3 chain). With ``None`` this line is skipped ->
        # byte-identical to WU-5A2.
        prior = prior_transform(prior, dt_days_since_last)
    q = parser_confidence * grader_confidence
    combined_likelihood = NO_OP_LIKELIHOOD
    for event in entity_events:
        likelihood = likelihood_for_event(event)
        x, y, z = (cl * li for cl, li in zip(combined_likelihood, likelihood, strict=True))
        combined_likelihood = (x, y, z)
    # WU-5B2 §3 L428 — fold the negotiation multiplier into the COMBINED
    # likelihood ONCE per entity, AFTER the per-event product and BEFORE damp (so
    # a low-q attempt mutes the metacognitive bump; ``q`` stays parser·grader).
    # The default ``NO_OP_LIKELIHOOD`` makes this an identity multiply (a*1.0=a) ->
    # byte-identical to WU-5B1.
    m0, m1, m2 = (a * m for a, m in zip(combined_likelihood, likelihood_multiplier, strict=True))
    combined_likelihood = (m0, m1, m2)
    damped = damp(combined_likelihood, q)
    posterior = bayes_update(prior, damped)
    misconception_code = None
    for event in entity_events:
        code = misconception_code_of(posterior, event)
        if code is not None:
            misconception_code = code
    return BeliefUpdate(
        prior_belief=prior,
        posterior_belief=posterior,
        mastery_after=mastery_of(posterior),
        confidence_after=confidence_of(posterior),
        misconception_code=misconception_code,
        parser_confidence=parser_confidence,
        grader_confidence=grader_confidence,
        dt_days_since_last=dt_days_since_last,
    )


async def _persist_entity_group(
    db: AsyncSession,
    *,
    sess: ApolloSession,
    attempt: ProblemAttempt,
    concept_problem_id: int | None,
    entity_id: int,
    entity_events: list,
    done_ts: datetime,
    parser_confidence: float,
    grader_confidence: float,
    prior_transform: PriorTransform | None = None,
    likelihood_multiplier_for_entity: LikelihoodMultiplierForEntity | None = None,
    negotiation_move_for_entity: NegotiationMoveForEntity | None = None,
) -> int:
    """Fold ALL of one entity's events for this Done into ONE belief update and
    a SINGLE ``LearnerState`` upsert (§3 steps 1-3 / 'fires once per Done
    episode'). The combined-likelihood update is computed ONCE
    (:func:`_combined_belief_update`); each event still appends its OWN
    ``apollo_mastery_events`` row (the event log records the single
    base->combined transition). Returns the count of event rows appended."""
    prior_state = await _lock_prior_state(
        db,
        user_id=str(sess.user_id),
        search_space_id=int(sess.search_space_id),
        entity_id=entity_id,
    )
    # Recompute base = the EVENT LOG (prior-ATTEMPT posterior), never the
    # self-mutated state row. prior_last_evidence_at anchors the recorded-only
    # dt_days_since_last (no v1 decay) and is read off the locked prior state.
    base_belief = await _recompute_base_for_entity(
        db,
        user_id=str(sess.user_id),
        search_space_id=int(sess.search_space_id),
        entity_id=entity_id,
        attempt_id=int(attempt.id),
    )
    prior_last_evidence_at = prior_state.last_evidence_at if prior_state is not None else None

    # WU-5B2 §3 — resolve the per-entity negotiation multiplier + representative
    # move from the injected maps (identity-default when not provided). The
    # multiplier folds into the COMBINED likelihood BEFORE damp; ``move`` overrides
    # the frozen MasteryEventRowSpec.negotiation_move at the ORM-build site. The two
    # are DECOUPLED — the caller applies the suppression inside the multiplier
    # closure, but the move string is recorded unconditionally.
    entity_multiplier = (
        likelihood_multiplier_for_entity(entity_events)
        if likelihood_multiplier_for_entity is not None
        else NO_OP_LIKELIHOOD
    )
    negotiation_move = (
        negotiation_move_for_entity(entity_events)
        if negotiation_move_for_entity is not None
        else None
    )

    update = _combined_belief_update(
        entity_events,
        prior_belief=base_belief,
        prior_last_evidence_at=prior_last_evidence_at,  # type: ignore[arg-type]
        parser_confidence=parser_confidence,
        grader_confidence=grader_confidence,
        done_ts=done_ts,
        prior_transform=prior_transform,
        likelihood_multiplier=entity_multiplier,
    )

    state_spec = None
    for event in entity_events:
        # Each event-log row carries its OWN per-event detail (kind/score/
        # confidences/reference_step_id/node_ids) but the SAME single belief
        # transition (base -> combined posterior) that actually occurred.
        mastery_spec, state_spec = event_to_row_specs(
            event,
            update,
            user_id=str(sess.user_id),
            search_space_id=int(sess.search_space_id),
            entity_id=entity_id,
            attempt_id=int(attempt.id),
        )
        # OVERRIDE the frozen spec's negotiation_move (None in v1) at the ORM-build
        # site — immutable via dataclasses.replace. ``None`` reproduces the
        # existing None -> byte-identical to WU-5B1.
        mastery_spec = replace(mastery_spec, negotiation_move=negotiation_move)
        db.add(
            _mastery_event_orm_from_spec(
                mastery_spec,
                concept_problem_id=concept_problem_id,
            )
        )

    # state_spec carries the combined posterior — upsert the state ONCE.
    _upsert_learner_state(
        db,
        prior_state=prior_state,
        state_spec=state_spec,
        user_id=str(sess.user_id),
        search_space_id=int(sess.search_space_id),
        entity_id=entity_id,
        done_ts=done_ts,
    )
    return len(entity_events)
