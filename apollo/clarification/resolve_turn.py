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
) -> tuple[tuple[str, str], ...]:
    """Returns the ``(candidate_key, outcome)`` pairs actually recorded this
    call (a judge failure records nothing and leaves that row
    ``asked_waiting``). Callers (chat.py, integration spec §6) use this to
    seed the V2 incremental view for content-verified ``confirmed`` outcomes
    only -- see ``rescorer.default_clarification_judge`` for the
    content-adjudication precondition that ``outcome`` depends on."""
    display_by_key = {c.canonical_key: c.display_name for c in candidates}
    recorded: list[tuple[str, str]] = []
    for row in await load_asked_waiting(db, attempt_id=attempt_id):
        candidate_key = str(row.candidate_key)
        try:
            outcome = rescore_clarification(
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
            state=outcome,
            clarification_text=student_message,
            answered_turn=answered_turn,
        )
        recorded.append((candidate_key, outcome))
    return tuple(recorded)
