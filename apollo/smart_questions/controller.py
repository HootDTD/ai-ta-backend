"""I/O orchestration for reference coverage, opportunity state, and wording.

The answer/student boundary lives here: the evaluator and planner are
answer-side, the writer is answer-blind (nudge + public surface only), and
the deterministic leak guard runs twice — on the evaluator's hint before it
reaches the writer, and on the writer's question before it reaches the
student.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Literal, cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apollo.persistence.models import ReferenceQuestionOpportunity
from apollo.schemas.problem import Problem
from apollo.smart_questions.evaluator import evaluate_reference_coverage
from apollo.smart_questions.leak_guard import leaks_private_content
from apollo.smart_questions.planner import choose_target
from apollo.smart_questions.writer import SAFE_FALLBACK as _SAFE_FALLBACK
from apollo.smart_questions.writer import write_question

_LOG = logging.getLogger(__name__)

_GENERIC_NUDGE = "ask the student to explain the part of the problem they have not covered yet"


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
    problem_text = str(problem.problem_text)
    student_messages = [content for role, content in transcript if role == "student"]

    # The hint crosses from the answer-aware evaluator to the answer-blind
    # writer; guard it so a hint that parrots node content never steers the
    # question. The node itself stays controller-side for guarding only.
    hint = next(item.ask_hint for item in coverage if item.node_id == target_id).strip()
    nudge = hint or _GENERIC_NUDGE
    if hint and leaks_private_content(
        hint, node=target, problem_text=problem_text, student_messages=student_messages
    ):
        _LOG.warning(
            "smart_question_hint_leak_blocked attempt_id=%s node_id=%s", attempt_id, target_id
        )
        nudge = _GENERIC_NUDGE

    question = await asyncio.to_thread(
        write_question, nudge=nudge, problem_text=problem_text, transcript=transcript
    )
    if question != _SAFE_FALLBACK and leaks_private_content(
        question, node=target, problem_text=problem_text, student_messages=student_messages
    ):
        _LOG.warning("smart_question_leak_blocked attempt_id=%s node_id=%s", attempt_id, target_id)
        question = _SAFE_FALLBACK
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
