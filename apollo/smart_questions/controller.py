"""I/O orchestration for unified coverage assessment and Apollo questioning."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apollo.persistence.models import ReferenceQuestionOpportunity
from apollo.schemas.problem import Problem
from apollo.smart_questions.unified import evaluate_and_ask


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
    for row in rows:
        if row.state == "asked_waiting":
            row.state = "answered"
            row.answered_turn = turn_index

    asked_node_ids = {row.reference_node_id for row in rows}
    result = await evaluate_and_ask(
        transcript=transcript,
        reference_graph=reference_graph,
        problem=problem,
        already_asked_node_ids=asked_node_ids,
    )
    if result.action == "done":
        return QuestionDecision(action="done")

    target_id = cast(str, result.target_node_id)
    question = cast(str, result.reply)
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
    return QuestionDecision(action="ask", question=question, target_node_id=target_id)
