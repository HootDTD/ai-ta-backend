"""Next-turn re-scoring: the student's reply is judged against each pending
clarification's target idea and the outcome recorded (spec §7). A refuted row is
the misconception evidence record (spec §8). Fail-safe: a judge failure leaves
the row asked_waiting (never credit on failure)."""

from __future__ import annotations

import logging

from apollo.clarification.rescorer import ClarificationJudge, rescore_clarification
from apollo.clarification.store import load_asked_waiting, record_outcome
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
) -> None:
    display_by_key = {c.canonical_key: c.display_name for c in candidates}
    for row in await load_asked_waiting(db, attempt_id=attempt_id):
        try:
            outcome = rescore_clarification(
                original_statement=row.original_statement,
                clarification_text=student_message,
                candidate_display=display_by_key.get(row.candidate_key, row.candidate_key),
                judge=judge,
            )
        except Exception as exc:  # noqa: BLE001 - leave asked_waiting, never credit on failure
            _LOG.warning("clarification_rescore_failed id=%s error=%s", row.id, exc)
            continue
        await record_outcome(
            db,
            clarification_id=row.id,
            state=outcome,
            clarification_text=student_message,
            answered_turn=answered_turn,
        )
