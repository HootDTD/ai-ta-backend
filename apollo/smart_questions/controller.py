"""I/O orchestration for reference coverage, opportunity state, and wording."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Literal, cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apollo.persistence.models import ReferenceQuestionOpportunity
from apollo.schemas.problem import Problem
from apollo.smart_questions.evaluator import evaluate_reference_coverage
from apollo.smart_questions.planner import choose_target
from apollo.smart_questions.writer import write_question


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

    coverage = await evaluate_reference_coverage(
        transcript=transcript, reference_graph=reference_graph, problem=problem
    )
    target_id = choose_target(reference_graph, coverage, {row.reference_node_id for row in rows})
    if target_id is None:
        return QuestionDecision(action="done")

    target = next(node for node in reference_graph.nodes if node.node_id == target_id)
    question = await asyncio.to_thread(write_question, node=target, transcript=transcript)
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
