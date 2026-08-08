"""Persistence orchestration for Apollo's unified tally and questioning call."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Literal, cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apollo.persistence.models import QuestionOpportunity
from apollo.schemas.problem import Problem
from apollo.smart_questions.selection import build_selection_policy
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
class CoveredTopic:
    """One reference node the student has demonstrated (tally status
    ``understood``). Emitted each turn as part of the current covered snapshot;
    the UI diffs ``node_id``s across turns so each topic celebrates only once."""

    node_id: str
    display_name: str


@dataclass(frozen=True)
class QuestionDecision:
    action: Literal["ask", "done"]
    question: str | None = None
    target_node_id: str | None = None
    covered_topics: tuple[CoveredTopic, ...] = ()
    #: Graded reference nodes on this problem — the ones the grade is computed
    #: over. Served to the student-ui pre-Done coverage meter.
    graded_topic_total: int = 0
    #: Graded nodes whose tally state is not yet ``understood``.
    open_graded_topics: int = 0


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
        status = str(row.state) if row is not None else "missing"
        if status not in _VALID_STATES:
            status = "missing"
        state.append(
            TallyState(
                node_id=node.node_id,
                label=_node_label(node),
                status=cast(Any, status),
                evidence=_evidence_rows(row.evidence) if row is not None else (),
                times_asked=int(row.times_asked) if row is not None else 0,
                last_asked_turn=(
                    int(row.last_asked_turn)
                    if row is not None and row.last_asked_turn is not None
                    else None
                ),
            )
        )
    return tuple(state)


def _new_opportunity_row(
    *, course_id: int, session_id: int, attempt_id: int, node_id: str
) -> QuestionOpportunity:
    return QuestionOpportunity(
        course_id=course_id,
        session_id=session_id,
        attempt_id=attempt_id,
        reference_node_id=node_id,
        state="missing",
        question="",
        evidence=[],
        times_asked=0,
    )


def _apply_tally_updates(
    db: AsyncSession,
    *,
    course_id: int,
    session_id: int,
    attempt_id: int,
    rows: list[Any],
    updates: tuple[TallyUpdate, ...],
) -> dict[str, Any]:
    """Persist the engine's tally updates.

    P2.4: there is no second evidence check here. ``unified._decode_updates``
    already rejected (and logged) any non-``missing`` update whose quote is not
    a normalized verbatim match inside the cited student turn; the old raw
    case/punctuation-sensitive re-check only ever dropped valid updates, which
    left nodes stuck ``missing`` and made Apollo re-probe covered territory.
    """
    by_id = {str(row.reference_node_id): row for row in rows}
    for update in updates:
        row = by_id.get(update.node_id)
        if row is None:
            row = _new_opportunity_row(
                course_id=course_id,
                session_id=session_id,
                attempt_id=attempt_id,
                node_id=update.node_id,
            )
            db.add(row)
            by_id[update.node_id] = row
        row.state = update.status
        if update.evidence is not None:
            evidence = list(row.evidence or [])
            serialized = {
                "turn_id": update.evidence.turn_id,
                "quote": update.evidence.quote,
            }
            if serialized not in evidence:
                evidence.append(serialized)
            row.evidence = evidence
    return by_id


def _covered_topics(reference_graph: Any, tally_by_id: dict[str, Any]) -> tuple[CoveredTopic, ...]:
    """Current covered snapshot: every reference node whose tally status is
    ``understood`` after this turn's updates, with its human label. A node with
    no tally row defaults to ``missing`` and is absent (never celebrated). The
    UI receives this full snapshot each turn and diffs ``node_id``s across
    turns, so each topic celebrates only once per attempt. Grading is untouched
    — this only reads the tally the questioning call already produced."""
    labels = {node.node_id: _node_label(node) for node in reference_graph.nodes}
    return tuple(
        CoveredTopic(node_id=node_id, display_name=labels[node_id])
        for node_id, row in tally_by_id.items()
        if str(row.state) == "understood" and node_id in labels
    )


def _write_opportunity_audit(
    db: AsyncSession,
    *,
    course_id: int,
    attempt_id: int,
    session_id: int,
    rows: dict[str, Any],
    result: UnifiedQuestionResult,
    turn_index: int,
) -> dict[str, Any]:
    """Record question timing without overwriting the merged tally state."""

    if result.action == "done":
        for row in rows.values():
            if row.asked_turn is not None and row.answered_turn is None:
                row.answered_turn = turn_index
        return rows

    target_id = cast(str, result.target_node_id)
    question = cast(str, result.question)
    target_row = rows.get(target_id)
    for row in rows.values():
        if row.asked_turn is not None and row.answered_turn is None and row is not target_row:
            row.answered_turn = turn_index
    if target_row is None:
        target_row = _new_opportunity_row(
            course_id=course_id,
            session_id=session_id,
            attempt_id=attempt_id,
            node_id=target_id,
        )
        db.add(target_row)
        rows[target_id] = target_row
    target_row.question = question
    target_row.asked_turn = turn_index + 1
    target_row.answered_turn = None
    return rows


async def plan_next_question(
    db: AsyncSession,
    *,
    course_id: int,
    attempt_id: int,
    session_id: int,
    problem: Problem,
    transcript: list[tuple[str, str]],
    turn_index: int,
) -> QuestionDecision:
    reference_graph = problem.to_kg_graph(attempt_id)
    opportunity_rows = cast(
        list[Any],
        (
            await db.execute(
                select(QuestionOpportunity).where(
                    QuestionOpportunity.course_id == course_id,
                    QuestionOpportunity.session_id == session_id,
                    QuestionOpportunity.attempt_id == attempt_id,
                )
            )
        )
        .scalars()
        .all(),
    )
    tally_state = _build_tally_state(reference_graph, opportunity_rows)
    budget = QuestionBudget(
        questions_asked=sum(int(row.times_asked) for row in opportunity_rows),
        cap=question_cap(),
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
        course_id=course_id,
        session_id=session_id,
        attempt_id=attempt_id,
        rows=opportunity_rows,
        updates=result.tally_updates,
    )
    covered_topics = _covered_topics(reference_graph, tally_by_id)
    policy = build_selection_policy(
        reference_graph=reference_graph,
        tally_state=_build_tally_state(reference_graph, list(tally_by_id.values())),
        questions_asked=budget.questions_asked,
        cap=budget.cap,
    )

    if result.action == "ask" and result.target_node_id is not None:
        target_row = tally_by_id.get(result.target_node_id)
        if target_row is None:
            target_row = _new_opportunity_row(
                course_id=course_id,
                session_id=session_id,
                attempt_id=attempt_id,
                node_id=result.target_node_id,
            )
            db.add(target_row)
            tally_by_id[result.target_node_id] = target_row
        target_row.times_asked = int(target_row.times_asked) + 1
        target_row.last_asked_turn = turn_index + 1

    _write_opportunity_audit(
        db,
        course_id=course_id,
        attempt_id=attempt_id,
        session_id=session_id,
        rows=tally_by_id,
        result=result,
        turn_index=turn_index,
    )
    if result.action == "done":
        return QuestionDecision(
            action="done",
            covered_topics=covered_topics,
            graded_topic_total=policy.graded_topic_total,
            open_graded_topics=policy.open_graded_topics,
        )
    return QuestionDecision(
        action="ask",
        question=cast(str, result.reply),
        target_node_id=cast(str, result.target_node_id),
        covered_topics=covered_topics,
        graded_topic_total=policy.graded_topic_total,
        open_graded_topics=policy.open_graded_topics,
    )
