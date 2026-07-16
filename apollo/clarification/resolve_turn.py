"""Next-turn clarification re-scoring and outcome persistence."""

from __future__ import annotations

import logging

from apollo.clarification.rescorer import ClarificationJudge, rescore_clarification
from apollo.clarification.store import load_asked_waiting, record_outcome
from apollo.persistence.neo4j_client import Neo4jClient
from apollo.resolution.candidates import Candidate

_LOG = logging.getLogger(__name__)


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
    """Resolve pending rows; the retired emergent-map side effect is absent."""
    del neo
    display_by_key = {candidate.canonical_key: candidate.display_name for candidate in candidates}
    for row in await load_asked_waiting(db, attempt_id=attempt_id):
        candidate_key = str(row.candidate_key)
        try:
            result = rescore_clarification(
                original_statement=str(row.original_statement),
                clarification_text=student_message,
                candidate_display=display_by_key.get(candidate_key, candidate_key),
                judge=judge,
            )
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("clarification_rescore_failed id=%s error=%s", row.id, exc)
            continue
        await record_outcome(
            db,
            clarification_id=int(row.id),
            state=result.outcome,
            clarification_text=student_message,
            answered_turn=answered_turn,
        )


__all__ = ["resolve_pending_clarifications"]
