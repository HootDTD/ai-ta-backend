"""Next-turn re-scoring: the student's reply is judged against each pending
clarification's target idea and the outcome recorded (spec §7). A refuted row is
the misconception evidence record (spec §8). Fail-safe: a judge failure leaves
the row asked_waiting (never credit on failure).

2026-07-10 emergent misconception map plan (T3, spec §5.3.2, plan risk R2): on
a ``refuted`` outcome, this module also feeds the emergent map's capture seam
2 (``clarification_refuted``) when ``APOLLO_EMERGENT_MAP_CAPTURE`` is ON. The
emergent signature is ALWAYS ``emergent.<entity_key of the opposed reference
node>`` — resolved from the refuted CANDIDATE, never invented by the judge:

  * a reference-node candidate's own ``canonical_key`` IS its entity_key
    (``candidates_from_reference_solution`` sets ``canonical_key = step["entity_key"]``);
  * a misconception candidate's ``opposes_key`` names the entity_key of the
    node it opposes (``candidates_from_misconceptions``).

A candidate with neither (a misconception with ``opposes_key=None``, or a
reference-node candidate with an empty ``canonical_key``) has no resolvable
entity_key and is NOT captured — the scope boundary spec §5.2 already applies
to the detector-birth seam.

The capture write runs inside its own ``db.begin_nested()`` savepoint (the
clarification loop's own precedent — ``apollo/handlers/chat.py`` wraps this
whole function in a nested transaction) with its own try/except, so a capture
failure can never poison the resolution transaction: isolation holds via
savepoint STACKING, not an early commit — ``record_outcome`` only mutates
the ORM row (no commit of its own), ``_capture_refuted``'s
``db.begin_nested()`` opens a savepoint INSIDE ``chat.py``'s own outer
``begin_nested()``, so a capture rollback releases only the inner savepoint
and leaves the outer transaction (and ``record_outcome``'s mutation within
it) intact; the exception is swallowed + logged, never re-raised.

2026-07-10 plan T7 (spec §5.5 Q3): after the observation write succeeds,
this module also eagerly materializes the signature's :Canon opposes-entity
via ``apollo.emergent.materialize.materialize_if_promotable`` — same
try/except, same nested savepoint, so a materialization failure is likewise
swallowed and never poisons the resolution transaction. ``resolve_pending_
clarifications`` accepts an optional ``neo`` client (default ``None``,
fully backward-compatible with every existing caller/test that omits it);
when provided (threaded from ``apollo/handlers/chat.py::handle_chat``, which
already receives a ``neo`` client for its own KGStore), the :Canon projection
runs eagerly from this seam too. When ``neo`` is ``None`` the entity +
opposes link still materialize in Postgres — only the Neo4j projection step
is skipped (logged, non-fatal) — so the eventual-consistency fallback is:
the emergent entity is durable and :Canon will pick it up at the next
projection run for that concept (e.g. the next detector-birth capture on the
same concept, or any future explicit re-projection).
"""

from __future__ import annotations

import logging

from apollo.clarification.rescorer import ClarificationJudge, rescore_clarification
from apollo.clarification.store import load_asked_waiting, record_outcome
from apollo.emergent.capture import record_clarification_refuted
from apollo.emergent.config import emergent_map_capture_enabled
from apollo.emergent.materialize import materialize_if_promotable
from apollo.persistence.neo4j_client import Neo4jClient
from apollo.resolution.candidates import Candidate

_LOG = logging.getLogger(__name__)


def _refuted_signature(candidate: Candidate | None) -> tuple[str, str] | None:
    """Resolve ``(signature, opposes)`` for a refuted candidate, or ``None``
    when no stable entity_key can be resolved (scope boundary). See the
    module docstring / plan R2 for the two reachable cases."""
    if candidate is None:
        return None
    entity_key = candidate.opposes_key if candidate.is_misconception else candidate.canonical_key
    if not entity_key:
        return None
    return f"emergent.{entity_key}", entity_key


async def _capture_refuted(
    db,
    *,
    row,
    attempt_id: int,
    candidate: Candidate | None,
    student_message: str,
    confidence: float,
    neo: Neo4jClient | None,
) -> None:
    """Own failure domain (T3 + T7): resolve the signature, then write inside
    a nested savepoint with its own try/except — a capture OR materialization
    failure is logged and swallowed, never propagated to the caller. T7:
    right after the observation write succeeds, eagerly materialize the
    signature's :Canon opposes-entity (Q3) — same savepoint, same except."""
    resolved = _refuted_signature(candidate)
    if resolved is None:
        return
    signature, opposes = resolved
    try:
        async with db.begin_nested():
            await record_clarification_refuted(
                db,
                search_space_id=int(row.search_space_id),
                concept_id=row.concept_id,
                user_id=str(row.user_id),
                attempt_id=int(attempt_id),
                signature=signature,
                opposes=opposes,
                confidence=confidence,
                evidence_span=student_message or None,
            )
            await materialize_if_promotable(
                db,
                neo,
                search_space_id=int(row.search_space_id),
                concept_id=row.concept_id,
                signature=signature,
                opposes_entity_key=opposes,
            )
    except Exception as exc:  # noqa: BLE001 - own failure domain, never poison resolution
        _LOG.warning(
            "emergent_refuted_capture_failed clarification_id=%s error=%s", row.id, exc
        )


async def resolve_pending_clarifications(
    *,
    db,
    attempt_id: int,
    student_message: str,
    candidates: tuple[Candidate, ...],
    judge: ClarificationJudge,
    answered_turn: int,
    neo: Neo4jClient | None = None,
) -> None:
    display_by_key = {c.canonical_key: c.display_name for c in candidates}
    candidate_by_key = {c.canonical_key: c for c in candidates}
    for row in await load_asked_waiting(db, attempt_id=attempt_id):
        candidate_key = str(row.candidate_key)
        try:
            result = rescore_clarification(
                original_statement=str(row.original_statement),
                clarification_text=student_message,
                candidate_display=display_by_key.get(candidate_key, candidate_key),
                judge=judge,
            )
        except Exception as exc:  # noqa: BLE001 - leave asked_waiting, never credit on failure
            _LOG.warning("clarification_rescore_failed id=%s error=%s", row.id, exc)
            continue
        await record_outcome(
            db,
            clarification_id=int(row.id),
            state=result.outcome,
            clarification_text=student_message,
            answered_turn=answered_turn,
        )
        if result.outcome == "refuted" and emergent_map_capture_enabled():
            await _capture_refuted(
                db,
                row=row,
                attempt_id=attempt_id,
                candidate=candidate_by_key.get(candidate_key),
                student_message=student_message,
                confidence=result.confidence,
                neo=neo,
            )
