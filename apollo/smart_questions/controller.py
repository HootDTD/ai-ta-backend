"""Persistence orchestration for Apollo's unified tally and questioning call."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Literal, cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apollo.persistence.models import QuestionTally, ReferenceQuestionOpportunity
from apollo.schemas.problem import Problem
from apollo.smart_questions.unified import (
    EvidenceQuote,
    QuestionBudget,
    TallyState,
    TallyUpdate,
    UnifiedQuestionResult,
    evaluate_and_ask,
    question_cap,
)

_LOG = logging.getLogger(__name__)
_VALID_STATES = {"understood", "tentative", "missing", "conflicting"}


@dataclass(frozen=True)
class QuestionDecision:
    action: Literal["ask", "done"]
    question: str | None = None
    target_node_id: str | None = None


def _node_label(node: Any) -> str:
    content = node.content.model_dump(mode="json")
    for key in (
        "label",
        "concept",
        "action",
        "term",
        "symbolic",
        "applies_when",
        "transformation",
        "meaning",
        "purpose",
    ):
        value = content.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return str(node.node_id)


def _evidence_rows(value: Any) -> tuple[EvidenceQuote, ...]:
    if not isinstance(value, list):
        return ()
    evidence: list[EvidenceQuote] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        turn_id, quote = item.get("turn_id"), item.get("quote")
        if isinstance(turn_id, int) and not isinstance(turn_id, bool) and isinstance(quote, str):
            evidence.append(EvidenceQuote(turn_id=turn_id, quote=quote))
    return tuple(evidence)


def _build_tally_state(reference_graph: Any, rows: list[Any]) -> tuple[TallyState, ...]:
    by_id = {str(row.reference_node_id): row for row in rows}
    state: list[TallyState] = []
    for node in reference_graph.nodes:
        row = by_id.get(node.node_id)
        status = str(row.status) if row is not None else "missing"
        if status not in _VALID_STATES:
            status = "missing"
        state.append(
            TallyState(
                node_id=node.node_id,
                label=_node_label(node),
                status=cast(Any, status),
                evidence=_evidence_rows(row.evidence) if row is not None else (),
                student_declined=bool(row.student_declined) if row is not None else False,
                times_asked=int(row.times_asked) if row is not None else 0,
                last_asked_turn=(
                    int(row.last_asked_turn)
                    if row is not None and row.last_asked_turn is not None
                    else None
                ),
            )
        )
    return tuple(state)


def _valid_update_evidence(update: TallyUpdate, transcript: list[tuple[str, str]]) -> bool:
    if update.status == "missing" and update.evidence is None:
        return True
    if update.evidence is None or not 0 <= update.evidence.turn_id < len(transcript):
        return False
    role, content = transcript[update.evidence.turn_id]
    return role == "student" and update.evidence.quote in content


def _new_tally_row(*, attempt_id: int, node_id: str) -> QuestionTally:
    return QuestionTally(
        attempt_id=attempt_id,
        reference_node_id=node_id,
        status="missing",
        evidence=[],
        student_declined=False,
        times_asked=0,
    )


def _apply_tally_updates(
    db: AsyncSession,
    *,
    attempt_id: int,
    rows: list[Any],
    updates: tuple[TallyUpdate, ...],
    transcript: list[tuple[str, str]],
) -> dict[str, Any]:
    by_id = {str(row.reference_node_id): row for row in rows}
    for update in updates:
        if not _valid_update_evidence(update, transcript):
            _LOG.warning(
                "apollo_question_tally_invalid_evidence attempt_id=%s node_id=%s turn_id=%s",
                attempt_id,
                update.node_id,
                update.evidence.turn_id if update.evidence is not None else None,
            )
            continue
        row = by_id.get(update.node_id)
        if row is None:
            row = _new_tally_row(attempt_id=attempt_id, node_id=update.node_id)
            db.add(row)
            by_id[update.node_id] = row
        row.status = update.status
        if update.evidence is not None:
            evidence = list(row.evidence or [])
            serialized = {
                "turn_id": update.evidence.turn_id,
                "quote": update.evidence.quote,
            }
            if serialized not in evidence:
                evidence.append(serialized)
            row.evidence = evidence
        if update.student_declined is not None:
            row.student_declined = update.student_declined
    return by_id


async def _write_opportunity_audit(
    db: AsyncSession,
    *,
    attempt_id: int,
    session_id: int,
    result: UnifiedQuestionResult,
    turn_index: int,
) -> None:
    rows = cast(
        list[Any],
        (
            await db.execute(
                select(ReferenceQuestionOpportunity).where(
                    ReferenceQuestionOpportunity.attempt_id == attempt_id
                )
            )
        )
        .scalars()
        .all(),
    )
    if result.action == "done":
        for row in rows:
            if row.state == "asked_waiting":
                row.state = "answered"
                row.answered_turn = turn_index
        return

    target_id = cast(str, result.target_node_id)
    question = cast(str, result.question)
    target_row = next((row for row in rows if row.reference_node_id == target_id), None)
    for row in rows:
        if row.state == "asked_waiting" and row is not target_row:
            row.state = "answered"
            row.answered_turn = turn_index
    if target_row is None:
        db.add(
            ReferenceQuestionOpportunity(
                attempt_id=attempt_id,
                session_id=session_id,
                reference_node_id=target_id,
                state="asked_waiting",
                question=question,
                asked_turn=turn_index + 1,
            )
        )
    else:
        target_row.state = "asked_waiting"
        target_row.question = question
        target_row.asked_turn = turn_index + 1
        target_row.answered_turn = None


async def plan_next_question(
    db: AsyncSession,
    *,
    attempt_id: int,
    session_id: int,
    problem: Problem,
    transcript: list[tuple[str, str]],
    turn_index: int,
) -> QuestionDecision:
    reference_graph = problem.to_kg_graph(attempt_id)
    tally_rows = cast(
        list[Any],
        (await db.execute(select(QuestionTally).where(QuestionTally.attempt_id == attempt_id)))
        .scalars()
        .all(),
    )
    tally_state = _build_tally_state(reference_graph, tally_rows)
    budget = QuestionBudget(
        questions_asked=sum(int(row.times_asked) for row in tally_rows), cap=question_cap()
    )
    result = await evaluate_and_ask(
        transcript=transcript,
        reference_graph=reference_graph,
        problem=problem,
        tally_state=tally_state,
        budget=budget,
    )
    tally_by_id = _apply_tally_updates(
        db,
        attempt_id=attempt_id,
        rows=tally_rows,
        updates=result.tally_updates,
        transcript=transcript,
    )

    if result.action == "ask" and result.target_node_id is not None:
        target_row = tally_by_id.get(result.target_node_id)
        if target_row is None:
            target_row = _new_tally_row(attempt_id=attempt_id, node_id=result.target_node_id)
            db.add(target_row)
            tally_by_id[result.target_node_id] = target_row
        target_row.times_asked = int(target_row.times_asked) + 1
        target_row.last_asked_turn = turn_index + 1

    # Legacy ledger remains write-only audit continuity; none of its state reaches the decision.
    await _write_opportunity_audit(
        db,
        attempt_id=attempt_id,
        session_id=session_id,
        result=result,
        turn_index=turn_index,
    )
    if result.action == "done":
        return QuestionDecision(action="done")
    return QuestionDecision(
        action="ask",
        question=cast(str, result.reply),
        target_node_id=cast(str, result.target_node_id),
    )
