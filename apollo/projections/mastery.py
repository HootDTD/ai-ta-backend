"""Campaign-plan Task B2 — mastery projection from the canonical grading
artifact (spec 2026-07-01 section 2/3, "mastery ledger only").

``update_mastery_from_artifact`` is a SEPARATE, simpler write path from the
dormant WU-5A2 Bayesian ``run_learner_update``
(``apollo/handlers/learner_update.py``): where WU-5A folds per-node
likelihoods into a 3-state Bayesian belief filter, this projection is a flat
EWMA of the artifact's ALREADY-COMPUTED composite score (spec Q3: "each
artifact appends a mastery_event and updates learner_state per concept: ...
updated from the composite (EWMA-style; exact rule is an implementation
detail, not campaign-gated)"). It writes NOTHING beyond what the artifact
already carries — no new grading, no new resolution.

Both paths write ``apollo_mastery_events`` / ``apollo_learner_state``, so they
are MUTUALLY EXCLUSIVE at the ``done.py`` call site (guarded on
``_graph_sim_layer3_enabled()`` — see that module's flag docstring): never
both active for the same attempt.

Granularity: the plan text says "one mastery_event per concept"; the schema's
``apollo_mastery_events``/``apollo_learner_state`` are keyed by
``entity_id`` (a ``apollo_kg_entities`` row), one level BELOW the top-level
``apollo_concepts`` scope the artifact records (``GradingArtifact.concept_id``).
Reusing the columns as-is (no new migration), this projection writes one
event/state row per DISTINCT resolvable entity referenced in the artifact's
``node_ledger`` (``credited``/``misconception`` rows only — ``unresolved`` rows
carry no ``canonical_key`` that maps to an entity), via the SAME
``canonical_key -> entity_id`` lookup WU-5A already uses
(``apollo.knowledge_graph.canon_projection.load_entity_specs`` — no new
inference). Every touched entity for a Done click receives the SAME composite
scalar as its new evidence: v1 has no per-node score finer than the composite
the artifact already recorded.

Idempotent per attempt: ``apollo_mastery_events``' existing
``UNIQUE(attempt_id, entity_id, event_kind)`` constraint (migration 026,
``NULLS NOT DISTINCT``) is reused with ``event_kind=EVENT_KIND`` — before
writing anything for an entity this module checks whether an event already
exists for ``(attempt_id, entity_id, EVENT_KIND)`` and, if so, skips that
entity entirely (no double event, no double EWMA application on a retry of
the same attempt).

Belief representation: the shared ``belief`` columns are the WU-5A 3-tuple
``(p_misc, p_shaky, p_mastered)`` (see ``apollo.learner_model.belief``). This
projection has no shaky/misconception-probability signal distinct from the
composite scalar, so it encodes ``belief = (1 - mastery, 0.0, mastery)`` — a
valid simplex for which ``apollo.learner_model.belief.mastery_of(belief) ==
mastery`` exactly.
"""

from __future__ import annotations

import os
from typing import Any, cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apollo.knowledge_graph.canon_projection import load_entity_specs
from apollo.persistence.models import (
    GradingArtifact,
    LearnerState,
    MasteryEvent,
    ProblemAttempt,
    TutoringSession,
)
from apollo.persistence.problem_linkage import resolve_problem_id

# Env var name (Task B2). Default matches the plan's named constant.
_ENV_EWMA_ALPHA = "APOLLO_MASTERY_EWMA_ALPHA"
_DEFAULT_EWMA_ALPHA: float = 0.3

# This projection's event_kind: an OPEN enum value (apollo_mastery_events.
# event_kind carries no SQL CHECK — see models.MASTERY_EVENT_KINDS docstring),
# distinct from WU-5A's covered/missing/partial/misconception/corrected set so
# the two write paths never collide on the same UNIQUE key even if both ran
# for the same attempt (they are still flag-guarded to never both be active).
EVENT_KIND: str = "composite"

# Ledger statuses that resolve to a real entity (see artifact_build.py's
# node_ledger construction): "unresolved" rows carry a student-node id, not a
# reference/misconception canonical_key, so they cannot map to an entity.
_LEDGER_STATUSES_WITH_ENTITY = frozenset({"credited", "misconception"})


def _env_float(name: str, default: float) -> float:
    """Read ``name`` from the environment as a float; fall back to ``default``
    on missing or malformed (mirrors ``apollo.grading.composite._env_float``)."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def ewma_alpha() -> float:
    """The EWMA smoothing weight, read fresh on every call (campaign tuning
    runs can retune it between attempts without a process restart)."""
    return _env_float(_ENV_EWMA_ALPHA, _DEFAULT_EWMA_ALPHA)


def ewma_mastery(*, composite: float, prior_mastery: float, alpha: float) -> float:
    """``alpha*composite + (1-alpha)*prior_mastery``, clamped to ``[0, 1]``.
    Pure arithmetic (mirrors ``apollo.grading.composite.composite_score``'s
    clamp-and-round convention, no rounding here — belief-array columns keep
    full float precision)."""
    raw = alpha * composite + (1 - alpha) * prior_mastery
    return max(0.0, min(1.0, raw))


def _ledger_entity_keys(artifact_row: GradingArtifact) -> list[str]:
    """Distinct ``canonical_key``s from the artifact's ``node_ledger`` that
    could map to a real entity (credited or misconception rows), in ledger
    order, de-duplicated."""
    seen: set[str] = set()
    keys: list[str] = []
    node_ledger = cast("list[dict[str, Any]]", artifact_row.node_ledger) or []
    for row in node_ledger:
        if row.get("status") not in _LEDGER_STATUSES_WITH_ENTITY:
            continue
        key = row.get("canonical_key")
        if not key or key in seen:
            continue
        seen.add(key)
        keys.append(key)
    return keys


def _entity_id_lookups(specs: list[Any]) -> tuple[dict[str, int], dict[str, int]]:
    """Two ``ledger canonical_key -> entity_id`` lookups for a concept's specs:
    an EXACT ``canonical_key`` map and a bare-SUFFIX map.

    WHY two: GRAPH artifacts (the shadow chain) carry TRUE namespaced canonical
    keys (``eq.bernoulli``, ``proc.plan_solve_...``) that hit the exact map.
    LLM-FALLBACK artifacts (``apollo.grading.artifact_build.build_llm_artifact``
    — the ONLY served path when the shadow chain is off) instead put BARE
    reference-node ids (``bernoulli``, ``plan_invoke_...``) in ``canonical_key``,
    because they come straight off ``compute_coverage``'s per-step map with no
    kind prefix. Entity keys are namespaced with a kind prefix (``eq.``,
    ``proc.``, ``var.``, ``concept.``, ``cond.``, ``simp.``, ``def.``,
    ``misc.``), so a bare ledger key never hits the exact map. The suffix map
    (the part after the first ``.``) lets those bare keys still resolve.

    AMBIGUOUS suffixes are dropped: if two specs share the same post-prefix
    suffix (``eq.foo`` and ``proc.foo``), a bare ``foo`` cannot be attributed to
    one entity without guessing, so it resolves to NEITHER (no event) rather
    than crediting the wrong entity."""
    exact: dict[str, int] = {}
    suffix: dict[str, int] = {}
    ambiguous: set[str] = set()
    for spec in specs:
        exact[spec.canonical_key] = spec.key
        if "." in spec.canonical_key:
            suf = spec.canonical_key.split(".", 1)[1]
            if suf in suffix:
                ambiguous.add(suf)
            else:
                suffix[suf] = spec.key
    for suf in ambiguous:
        del suffix[suf]
    return exact, suffix


def _normalization_confidence(artifact_row: GradingArtifact) -> float:
    """The artifact's normalization confidence (``abstention.
    normalization_confidence`` — see ``artifact_build.py``'s ``abstention``
    block). Defaults to ``1.0`` when absent (the LLM-fallback path records no
    such signal; treating it as fully confident matches the pre-artifact
    behavior of trusting the served grade outright)."""
    abstention: dict[str, Any] = cast("dict[str, Any] | None", artifact_row.abstention) or {}
    value = abstention.get("normalization_confidence")
    return float(value) if value is not None else 1.0


async def _existing_event(
    db: AsyncSession, *, attempt_id: int, entity_id: int
) -> MasteryEvent | None:
    return (
        await db.execute(
            select(MasteryEvent).where(
                MasteryEvent.attempt_id == attempt_id,
                MasteryEvent.entity_id == entity_id,
                MasteryEvent.event_kind == EVENT_KIND,
            )
        )
    ).scalar_one_or_none()


async def _prior_state(
    db: AsyncSession, *, user_id: str, search_space_id: int, entity_id: int
) -> LearnerState | None:
    return (
        await db.execute(
            select(LearnerState).where(
                LearnerState.user_id == user_id,
                LearnerState.search_space_id == search_space_id,
                LearnerState.entity_id == entity_id,
            )
        )
    ).scalar_one_or_none()


def _belief_for(mastery: float) -> list[float]:
    """``(p_misc, p_shaky, p_mastered)`` with all mass on misc/mastered so
    ``mastery_of(belief) == mastery`` exactly (0.5*0 + mastery)."""
    return [max(0.0, min(1.0, 1.0 - mastery)), 0.0, max(0.0, min(1.0, mastery))]


async def update_mastery_from_artifact(db: AsyncSession, *, artifact_row: GradingArtifact) -> None:
    """Append a ``composite`` ``apollo_mastery_events`` row and EWMA-upsert
    ``apollo_learner_state`` for every distinct entity the artifact's ledger
    credits or flags a misconception on. FLUSH-ONLY: mirrors
    ``apollo.learner_model.persistence.persist_learner_update``'s contract —
    the caller owns the transaction boundary (commit/rollback), so a failure
    here composes with whatever else the caller is doing in the same txn.

    A no-op when ``artifact_row.concept_id`` is ``None`` (no scope to resolve
    entities against) or when the ledger names no credited/misconception key
    (nothing to project)."""
    if artifact_row.concept_id is None:
        return
    keys = _ledger_entity_keys(artifact_row)
    if not keys:
        return

    specs = await load_entity_specs(db, concept_id=int(artifact_row.concept_id))
    exact_by_key, suffix_by_key = _entity_id_lookups(specs)

    scores = cast("dict[str, Any]", artifact_row.scores) or {}
    composite = float(scores.get("composite", 0.0))
    confidence = _normalization_confidence(artifact_row)
    alpha = ewma_alpha()
    attempt_id = int(artifact_row.attempt_id)
    user_id = str(artifact_row.user_id)
    search_space_id = int(artifact_row.search_space_id)

    # GEN-5: recover the durable problem-bank key from the attempt's session.
    # Missing legacy rows/codes deliberately produce NULL without suppressing
    # the composite event or its learner-state update.
    linkage_result = await db.execute(
        select(TutoringSession.concept_id, ProblemAttempt.problem_id)
        .select_from(ProblemAttempt)
        .join(TutoringSession, TutoringSession.id == ProblemAttempt.session_id)
        .where(ProblemAttempt.id == attempt_id)
    )
    # The existing pure projection tests use a deliberately minimal result
    # duck that represents every lookup as a miss. Preserve that contract while
    # real SQLAlchemy results take the joined-row path.
    attempt_linkage = (
        linkage_result.one_or_none() if hasattr(linkage_result, "one_or_none") else None
    )
    concept_problem_id = None
    if attempt_linkage is not None and attempt_linkage.concept_id is not None:
        concept_problem_id = await resolve_problem_id(
            db,
            concept_id=int(attempt_linkage.concept_id),
            course_id=search_space_id,
            problem_identity=attempt_linkage.problem_id,
        )

    for key in keys:
        # Exact namespaced match first (graph artifacts); fall back to the bare
        # suffix map (llm_fallback artifacts carry unprefixed reference ids).
        entity_id = exact_by_key.get(key)
        if entity_id is None:
            entity_id = suffix_by_key.get(key)
        if entity_id is None:
            continue
        if await _existing_event(db, attempt_id=attempt_id, entity_id=entity_id) is not None:
            continue  # idempotent: this attempt already projected this entity

        prior_state = await _prior_state(
            db, user_id=user_id, search_space_id=search_space_id, entity_id=entity_id
        )
        prior_mastery = cast(float, prior_state.mastery) if prior_state is not None else composite
        new_mastery = ewma_mastery(composite=composite, prior_mastery=prior_mastery, alpha=alpha)
        prior_belief = _belief_for(prior_mastery)
        posterior_belief = _belief_for(new_mastery)

        db.add(
            MasteryEvent(
                user_id=user_id,
                search_space_id=search_space_id,
                entity_id=entity_id,
                attempt_id=attempt_id,
                concept_problem_id=concept_problem_id,
                event_kind=EVENT_KIND,
                score=composite,
                misconception_code=None,
                parser_confidence=None,
                grader_confidence=confidence,
                negotiation_move=None,
                reference_step_id=None,
                prior_belief=prior_belief,
                posterior_belief=posterior_belief,
                mastery_after=new_mastery,
                dt_days_since_last=None,
                evidence_node_ids=[],
            )
        )

        if prior_state is None:
            db.add(
                LearnerState(
                    user_id=user_id,
                    search_space_id=search_space_id,
                    entity_id=entity_id,
                    belief=posterior_belief,
                    mastery=new_mastery,
                    confidence=confidence,
                    misconception_code=None,
                    evidence_count=1,
                    last_evidence_at=artifact_row.created_at,
                    updated_at=artifact_row.created_at,
                )
            )
        else:
            prior_state.belief = posterior_belief  # type: ignore[assignment]
            prior_state.mastery = new_mastery  # type: ignore[assignment]
            prior_state.confidence = confidence  # type: ignore[assignment]
            prior_state.evidence_count = prior_state.evidence_count + 1  # type: ignore[assignment]
            prior_state.last_evidence_at = artifact_row.created_at  # type: ignore[assignment]
            prior_state.updated_at = artifact_row.created_at  # type: ignore[assignment]

    await db.flush()
