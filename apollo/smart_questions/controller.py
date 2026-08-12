"""Persistence orchestration for Apollo's unified tally and questioning call."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, cast

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import set_committed_value

from apollo.overseer.wrongness import (
    LEVEL_PRODUCE,
    LEVEL_SCHEDULE,
    WRONGNESS_NONE,
    candidate_quotes,
    effective_wrongness_level,
    ledger_findings,
)
from apollo.persistence.attempt_history import prior_wrongness_findings
from apollo.persistence.models import QuestionOpportunity
from apollo.schemas.problem import Problem
from apollo.smart_questions.challenge import clean_quote
from apollo.smart_questions.selection import SelectionPolicy, build_selection_policy
from apollo.smart_questions.unified import (
    CarriedChallenge,
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


async def _bump_times_asked(db: AsyncSession, *, row: Any) -> None:
    """Atomically increment ONE ledger row's ``times_asked`` (M5, P3.4).

    Replaces the Python read-modify-write that lost an increment whenever two
    turns of the same attempt overlapped (a double-send): both read N, both
    wrote N+1. A lost increment is GRADE-VISIBLE — ``done._probed_node_ids``
    reads ``times_asked > 0`` as engagement, so it mis-scopes the P1.2b
    denominator, and the per-node ask cap stops binding.

    ``flush()`` first because the row may have been minted THIS turn by
    ``_apply_tally_updates`` and not exist in the database yet. The returned
    value is written back with ``set_committed_value`` and NEVER by plain
    assignment: assigning would mark the attribute dirty and re-emit a blind
    ``SET times_asked = <our value>`` at the next flush, reintroducing the exact
    lost update this exists to remove.
    """
    await db.flush()
    new_value = (
        await db.execute(
            update(QuestionOpportunity)
            .where(QuestionOpportunity.id == row.id)
            .values(times_asked=QuestionOpportunity.times_asked + 1)
            .returning(QuestionOpportunity.times_asked)
            .execution_options(synchronize_session=False)
        )
    ).scalar_one()
    set_committed_value(row, "times_asked", int(new_value))


def _evidence_entry(update: TallyUpdate) -> dict[str, Any]:
    """One entry for the free-form ``question_opportunities.evidence`` array.

    Two shapes, and the boundary between them is a contract (P3.2 seam S2):

    * ``wrongness == "none"`` -> exactly ``{"turn_id", "quote"}``, BYTE-IDENTICAL
      to every entry ever written before P3.2. Level 0 therefore writes the same
      bytes it always has, and the dedup rule (``if serialized not in evidence``)
      keeps matching historical rows.
    * otherwise -> the tagged entry
      ``{"turn_id", "quote", "wrongness", "contradicts", "kind"}``.

    No migration: the column is free-form JSONB (`__evidence__array_check` only
    asserts ``jsonb_typeof = 'array'``). Both shapes must keep reading correctly
    in `done._latest_student_quote` and `done._probed_node_ids`, which only ever
    look at ``quote`` — pinned by test.
    """
    evidence = cast(EvidenceQuote, update.evidence)
    entry: dict[str, Any] = {"turn_id": evidence.turn_id, "quote": evidence.quote}
    if update.wrongness == "none" or update.contradiction is None:
        return entry
    return {
        **entry,
        "wrongness": update.wrongness,
        "contradicts": update.contradiction.reference_clause,
        "kind": update.contradiction.kind,
    }


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
    # `tally_update`, not `update`: the loop name used to shadow the SQLAlchemy
    # `update()` this module calls in `_bump_times_asked` (ruff F402).
    for tally_update in updates:
        row = by_id.get(tally_update.node_id)
        if row is None:
            row = _new_opportunity_row(
                course_id=course_id,
                session_id=session_id,
                attempt_id=attempt_id,
                node_id=tally_update.node_id,
            )
            db.add(row)
            by_id[tally_update.node_id] = row
        row.state = tally_update.status
        if tally_update.evidence is not None:
            evidence = list(row.evidence or [])
            serialized = _evidence_entry(tally_update)
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


@dataclass(frozen=True)
class _LadderInputs:
    """Everything `APOLLO_WRONGNESS_LEVEL` changes about ONE questioning call.

    Resolved once per turn from a SINGLE ledger read, so the level is decided in
    exactly one place and each rung's inputs are inert below it:

    * level >= 1 — ``wrongness`` turns the producer on (schema + prompt block).
    * level >= 2 — ``contested_ids`` reorders the askable set (L2a),
      ``contested_quotes`` names each graded node's latest material
      contradiction, ``challenge_gate`` arms the done-gate (L2b), and
      ``carried_challenges`` carries ONE earlier-attempt claim forward (L2c).

    Level 0 is the all-defaults instance, which is byte-identical to pre-P3.2.
    """

    wrongness: bool = False
    challenge_gate: bool = False
    contested_ids: tuple[str, ...] = ()
    contested_quotes: dict[str, str] = field(default_factory=dict)
    carried_challenges: tuple[CarriedChallenge, ...] = ()


def _select_carried(
    prior: Sequence[dict[str, Any]],
    *,
    policy: SelectionPolicy,
    tally_state: Sequence[TallyState],
) -> tuple[CarriedChallenge, ...]:
    """At most ONE carried challenge (decision D4), newest-first.

    The newest UNRESOLVED prior finding whose node is still askable this attempt
    and has not been probed yet. One is the cap on purpose: the point is a single
    continuity question, not a rap sheet — and the consequence is always earned
    inside the current attempt.
    """
    askable = set(policy.askable_ids)
    asked = {item.node_id: item.times_asked for item in tally_state}
    for finding in prior:
        node_id = finding.get("canonical_key")
        if not isinstance(node_id, str) or node_id not in askable:
            continue
        if finding.get("resolved") or asked.get(node_id, 0) != 0:
            continue
        quote = clean_quote(finding.get("evidence_span"))
        if quote:
            return (CarriedChallenge(node_id=node_id, prior_quote=quote),)
    return ()


async def _ladder_inputs(
    db: AsyncSession,
    *,
    problem: Problem,
    attempt_id: int,
    course_id: int,
    reference_graph: Any,
    rows: list[Any],
    tally_state: Sequence[TallyState],
    questions_asked: int,
    cap: int,
) -> _LadderInputs:
    """Resolve this turn's ladder inputs. One ledger pass, one DB read at >= 2."""
    level = effective_wrongness_level(problem.concept_id)
    if level < LEVEL_SCHEDULE:
        return _LadderInputs(wrongness=level >= LEVEL_PRODUCE)

    findings = ledger_findings(rows)
    contested_ids = tuple(
        dict.fromkeys(
            finding.node_id
            for finding in findings
            if finding.wrongness != WRONGNESS_NONE and finding.is_latest_evidence
        )
    )
    policy = build_selection_policy(
        reference_graph=reference_graph,
        tally_state=tally_state,
        questions_asked=questions_asked,
        cap=cap,
        contested_ids=contested_ids,
    )
    prior = (
        await prior_wrongness_findings(
            db,
            attempt_id=attempt_id,
            problem_id=problem.database_id,
            course_id=course_id,
        )
        if problem.database_id is not None
        else ()
    )
    return _LadderInputs(
        wrongness=True,
        challenge_gate=True,
        contested_ids=contested_ids,
        # `candidate_quotes` is the same graded + material + latest-evidence
        # filter the at-Done corroborator uses, so the node the gate challenges
        # is exactly the node that could later be corroborated.
        contested_quotes=candidate_quotes(findings, graded_node_ids=policy.graded_ids),
        carried_challenges=_select_carried(prior, policy=policy, tally_state=tally_state),
    )


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
    questions_asked = sum(int(row.times_asked) for row in opportunity_rows)
    cap = question_cap()
    ladder = await _ladder_inputs(
        db,
        problem=problem,
        attempt_id=attempt_id,
        course_id=course_id,
        reference_graph=reference_graph,
        rows=opportunity_rows,
        tally_state=tally_state,
        questions_asked=questions_asked,
        cap=cap,
    )
    budget = QuestionBudget(
        questions_asked=questions_asked,
        cap=cap,
        carried_challenges=ladder.carried_challenges,
    )
    result = await evaluate_and_ask(
        transcript=transcript,
        reference_graph=reference_graph,
        problem=problem,
        tally_state=tally_state,
        budget=budget,
        wrongness=ladder.wrongness,
        contested_ids=ladder.contested_ids,
        contested_quotes=ladder.contested_quotes,
        challenge_gate=ladder.challenge_gate,
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
        contested_ids=ladder.contested_ids,
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
        if not result.fallback_served or target_row.asked_turn is not None:
            # A degenerate fallback reply (a verbatim public clause, served when the
            # engine burned its regenerate) never actually probed this node, so the
            # FIRST time Apollo serves a node it must not spend one of its
            # MAX_ASKS_PER_NODE probes: doing so used to exhaust a thin rubric's
            # only graded node, empty `askable_ids`, force `done`, and auto-grade
            # an unprobed topic as 0 (the bimodal-F mode).
            #
            # Every LATER serve on the same node (`asked_turn` already stamped)
            # charges, degenerate or not. Without that the counter never moves:
            # `budget.questions_asked` is `sum(times_asked)`, so a model that keeps
            # going off-policy on the same node re-serves the same clipped clause
            # forever and `budget_exhausted` never gets any closer. The free pass
            # is per node and once, which is all the original harm needs — a node
            # already probed once is not "never actually probed".
            await _bump_times_asked(db, row=target_row)
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
