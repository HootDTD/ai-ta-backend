"""I/O orchestration for unified R-graph learner tally and questioning."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apollo.persistence.models import ReferenceQuestionOpportunity
from apollo.schemas.problem import Problem
from apollo.smart_questions.unified import QuestionHistory, evaluate_and_ask


@dataclass(frozen=True)
class QuestionDecision:
    action: Literal["ask", "done"]
    question: str | None = None
    target_node_id: str | None = None


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
    result = await evaluate_and_ask(
        transcript=transcript,
        reference_graph=reference_graph,
        problem=problem,
        question_history=tuple(
            QuestionHistory(
                node_id=str(row.reference_node_id),
                question=str(row.question),
                state=str(row.state),
            )
            for row in rows
        ),
    )
    coverage_by_id = {item.node_id: item for item in result.coverage}

    if result.action == "done":
        for row in rows:
            if row.state == "asked_waiting":
                row.state = "answered"
                row.answered_turn = turn_index
        return QuestionDecision(action="done")

    target_id = cast(str, result.target_node_id)
    question = cast(str, result.reply)
    target_row = next((row for row in rows if row.reference_node_id == target_id), None)
    for row in rows:
        if row.state != "asked_waiting" or row is target_row:
            continue
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
        # The row is a per-node latest-question ledger, not proof that the node
        # was learned. Reuse it when the fresh tally remains tentative/missing/
        # conflicting so an insufficient response can receive a better probe.
        target_row.state = "asked_waiting"
        target_row.question = question
        target_row.asked_turn = turn_index + 1
        target_row.answered_turn = None
        assert coverage_by_id[target_id].state != "understood"

    return QuestionDecision(action="ask", question=question, target_node_id=target_id)
