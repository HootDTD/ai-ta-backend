"""WU-4C1 — the Done SHADOW graph-simulation chain (§6.4 steps 1-13/15-call).

Wires the already-built grading chain
``resolve -> RESOLVES_TO -> canonicalize -> grade -> audit -> persist run+findings``
into a single ``run_graph_simulation`` so ``done.py`` stays focused and under 800
lines. EVERY callee here is DONE and frozen — this module only CALLS them. It
writes NO new grading algorithm.

Binding contract (§6.4 staged transaction + NO-FALLBACK):
  * The OLD student-facing grade/XP are already committed by ``done.py`` BEFORE
    this runs, so re-raising a named error here NEVER voids the grade.
  * Steps 4 gate (``validate_student_graph``) and ``build_reference_canonical``
    validation raise BEFORE any cross-store write — they BLOCK only the shadow run
    and DO NOT set ``learner_update_pending`` (nothing to retry until the bad
    graph/reference is fixed). They surface as the right HTTP status (422 / 409).
  * Any step-5+ infra failure (``ResolutionUnavailableError`` /
    ``ResolutionInvalidOutputError`` / ``TranscriptAuditUnavailableError`` /
    ``CanonProjectionError`` / unexpected ``Exception`` in the cross-store window)
    sets ``attempt.learner_update_pending=True``, commits that flag, and RE-RAISES
    the original error. The retry re-runs FROM resolution idempotently (RESOLVES_TO
    MERGE + persist supersede).

WU-4C1 does NOT call ``convert_findings_to_events`` and does NOT write
``apollo_mastery_events`` / ``apollo_learner_state`` (those are WU-5A). It carries
``opposes_map`` + ``turn_order`` in the frozen ``ShadowGradeResult`` so WU-5A can
consume them. It does NOT promote the shadow grade to student-facing (WU-4C2).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apollo.errors import (
    ResolutionInvalidOutputError,
    ResolutionUnavailableError,
    TranscriptAuditUnavailableError,
)
from apollo.grading.audited_grade import AuditedGrade, build_audited_grade
from apollo.grading.normalization_confidence import compute_normalization_confidence
from apollo.grading.opposes import build_opposes_map
from apollo.grading.persistence import persist_comparison_run
from apollo.grading.reference_hash import reference_graph_hash
from apollo.grading.transcript_audit import main_chat_auditor
from apollo.graph_compare.canonical import (
    build_reference_canonical,
    build_student_canonical,
)
from apollo.graph_compare.core import GradeResult, grade_attempt
from apollo.graph_compare.problem_inputs import build_problem_candidates
from apollo.graph_compare.validator import (
    ReferenceGraphInvalidError,
    StudentGraphInvalidError,
    validate_student_graph,
)
from apollo.handlers.done_turn_order import build_turn_order
from apollo.knowledge_graph.canon_projection import load_entity_specs
from apollo.knowledge_graph.resolution_store import write_resolution
from apollo.ontology import KGGraph
from apollo.overseer.misconception_bank import load_for_concept
from apollo.persistence.models import ApolloSession, Message, ProblemAttempt
from apollo.persistence.neo4j_client import Neo4jClient
from apollo.resolution import resolve_attempt
from apollo.resolution.adjudication import main_chat_adjudicator

# The named infra errors that surface in the cross-store window (step 5+). These
# DO set learner_update_pending (the grade is already committed; the retry re-runs
# resolution idempotently). StudentGraph/Reference invalid are handled separately
# (raised BEFORE any cross-store write -> do NOT set pending).
_PENDING_ON_ERRORS = (
    ResolutionUnavailableError,
    ResolutionInvalidOutputError,
    TranscriptAuditUnavailableError,
)


@dataclass(frozen=True)
class ShadowGradeResult:
    """The frozen handoff WU-4C2 (calibration metrics) and WU-5A (belief update)
    both read. Carries everything the live chain computed for the shadow run."""

    run_id: int
    grade: GradeResult
    audited: AuditedGrade
    normalization_confidence: float
    reference_graph_hash: str
    opposes_map: Mapping[str, str]
    turn_order: Mapping[str, int]


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _misconceptions_dict(entries: list) -> dict:
    """Map ``MisconceptionEntry`` rows onto the dict shape
    ``candidates_from_misconceptions`` reads: ``{"misconceptions": [{key,
    trigger_phrases, opposes, display_name}, ...]}``.

    Field translation (§3.1 step 1 / Risk #2): ``code -> key``,
    ``description -> display_name``. The bank carries no ``opposes`` column today,
    so ``opposes`` is ``None`` (a missing opposes-link just disables conflict-pair
    detection for that misconception — tolerated; WU-4C1 writes no events anyway).
    """
    return {
        "misconceptions": [
            {
                "key": e.code,
                "trigger_phrases": list(e.trigger_phrases),
                "opposes": None,
                "display_name": e.description,
            }
            for e in entries
        ]
    }


async def _read_transcript(db: AsyncSession, *, attempt_id: int) -> str:
    """Join the attempt's messages (ordered by turn) into one transcript string
    for the §6.4 step-12 transcript audit."""
    rows = (
        await db.execute(
            select(Message.content)
            .where(Message.attempt_id == attempt_id)
            .order_by(Message.turn_index)
        )
    ).scalars().all()
    return "\n".join(rows)


async def _set_pending_and_commit(
    db: AsyncSession, attempt: ProblemAttempt
) -> None:
    """NO-FALLBACK: flag the attempt for a learner-model retry and commit ONLY
    that flag (the grade/XP are already durable from the OLD path)."""
    attempt.learner_update_pending = True
    await db.commit()


async def run_graph_simulation(
    db: AsyncSession,
    neo: Neo4jClient,
    *,
    attempt: ProblemAttempt,
    sess: ApolloSession,
    student_graph: KGGraph,
    problem_payload: dict,
) -> ShadowGradeResult | None:
    """Run the full §6.4 chain in SHADOW and persist the comparison run + findings.

    Returns the ``ShadowGradeResult`` on success. On a step-5+ infra failure it
    sets ``attempt.learner_update_pending=True``, commits that flag, and RE-RAISES
    the named error (never a partial result, never voids the grade). A pure
    validation failure (student-graph 422 / reference 409) re-raises WITHOUT
    setting pending (nothing cross-store was written).
    """
    # ---- Steps 1-4: assemble inputs + the step-4 raw-graph gate (no writes) ----
    entries = await load_for_concept(db, concept_id=sess.concept_id)
    misconceptions = _misconceptions_dict(entries)

    specs = await load_entity_specs(
        db, search_space_id=sess.search_space_id, concept_id=sess.concept_id
    )
    canon_key_by_canonical_key = {spec.canonical_key: spec.key for spec in specs}

    inputs = build_problem_candidates(
        problem_payload,
        misconceptions,
        canon_key_by_canonical_key=canon_key_by_canonical_key,
    )

    # Step 4 gate — runs on the RAW student graph BEFORE resolution. A failure
    # here is a 422 and has written NOTHING cross-store -> do NOT set pending.
    validate_student_graph(student_graph)

    # ---- Steps 5+ : the cross-store window (NO-FALLBACK on infra failure) ----
    try:
        # Step 5 — resolve (the one LLM adjudication call, live default).
        resolution = resolve_attempt(
            student_graph,
            inputs.candidates,
            llm_adjudicator=main_chat_adjudicator,
            fuzzy_threshold=0.9,
            symbolic_mappings=inputs.symbolic_mappings,
        )
        # Step 6 — RESOLVES_TO + resolution fields (idempotent MERGE).
        await write_resolution(neo, attempt.id, resolution, resolved_at=_now_iso())

        # Step 7 — canonicalize both sides. build_reference_canonical validates
        # the reference FIRST (raises ReferenceGraphInvalidError = 409); it runs
        # AFTER resolution here, so on that 409 a RESOLVES_TO MERGE may exist, but
        # it is idempotent and harmless — the reference is bad, not the student's
        # graph, so we still surface the 409 WITHOUT setting pending.
        student_canonical = build_student_canonical(student_graph, resolution)
        reference_graph = build_reference_canonical(problem_payload)

        # Step 8 — grade (pure).
        grade = grade_attempt(student_canonical, reference_graph)

        # Step 9 — transcript audit (live auditor; suppress-all-missing on infra
        # failure is handled INSIDE build_audited_grade).
        transcript = await _read_transcript(db, attempt_id=attempt.id)
        student_nodes = tuple(student_graph.nodes)
        audited = build_audited_grade(
            grade,
            transcript=transcript,
            resolution=resolution,
            student_nodes=student_nodes,
            candidates=inputs.candidates,
            reference_invalid=False,
            audit_fn=main_chat_auditor,
        )

        # Step 10 — confidence + reference hash.
        normalization_confidence = compute_normalization_confidence(audited, resolution)
        ref_hash = reference_graph_hash(reference_graph)

        # Step 11 — persist run + findings (flush only) then commit (WU-4C1 owns
        # the run-txn boundary).
        run_id = await persist_comparison_run(
            db,
            attempt_id=attempt.id,
            user_id=sess.user_id,
            search_space_id=sess.search_space_id,
            grade=grade,
            audited=audited,
            normalization_confidence=normalization_confidence,
            reference_graph_hash=ref_hash,
        )
        await db.commit()

        # Step 12 — opposes map + turn order (WU-5A handoff signals).
        opposes_map = build_opposes_map(inputs.candidates)
        turn_order = await build_turn_order(
            db, neo, attempt_id=attempt.id, student_graph=student_graph
        )

        # Step 13 — the frozen handoff.
        return ShadowGradeResult(
            run_id=run_id,
            grade=grade,
            audited=audited,
            normalization_confidence=normalization_confidence,
            reference_graph_hash=ref_hash,
            opposes_map=opposes_map,
            turn_order=turn_order,
        )
    except ReferenceGraphInvalidError:
        # §6.6 in SHADOW v1: a bad reference blocks only the shadow run (the OLD
        # path already graded the student). Surface as 409; nothing the retry can
        # fix until the reference is corrected -> do NOT set pending.
        raise
    except StudentGraphInvalidError:
        # Belt-and-suspenders: validate_student_graph already ran above, but keep
        # the 422 visible without setting pending if it ever surfaces here.
        raise
    except _PENDING_ON_ERRORS:
        # Cross-store infra failure -> flag for retry, commit, re-raise (the grade
        # is already committed; never voided).
        await _set_pending_and_commit(db, attempt)
        raise
    except Exception:
        # Any unexpected failure in the cross-store window is still NO-FALLBACK:
        # flag for retry, commit, re-raise (e.g. CanonProjectionError, Risk #5).
        await _set_pending_and_commit(db, attempt)
        raise
