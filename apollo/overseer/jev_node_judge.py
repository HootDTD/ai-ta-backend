"""Per-turn Apollo node judge on TypeSafe Jev (spec 2026-10-29, experimental).

Instead of ONE gpt-5.1 call over the whole transcript, ask a smaller question
many times: for each student turn, for each graded reference node, which of
:data:`LABELS` applies? One Jev request per student turn carries one ``choice``
question per graded node. Aggregation is deterministic: per node the best label
across turns wins (``covered > partial > incorrect > not_covered``), so a later
correction overrides an earlier mistake, and the result is a standard
:class:`CoverageVerdict` the rest of the grading path consumes unchanged.

The graded-node selection and the node text are the GPT adjudicator's own
(``transcript_coverage._build_rubric_items``), so the two judges grade the same
rubric. Behind ``APOLLO_NODE_JUDGE=jev`` only; ``handlers/done`` falls back to
the GPT adjudicator on any exception raised here.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from apollo.hoot_bridge.reference_answer import ASIDE_MESSAGE_INTENT_TAG
from apollo.ontology import KGGraph
from apollo.overseer.coverage_contract import CoverageVerdict, validate_coverage_verdict
from apollo.overseer.jev_client import JevClient, JevError
from apollo.overseer.transcript_coverage import _build_rubric_items

_LOG = logging.getLogger(__name__)

LABELS: tuple[str, ...] = ("covered", "partial", "not_covered", "incorrect")
# `partial` deliberately equals PRAISE_FLOOR / REFERENCE_TEXT_CREDIT_THRESHOLD
# (0.6), so every downstream split keeps its meaning. One named constant; tuning
# it is a separate pre-registered experiment, never a tweak against the corpus.
PARTIAL_CREDIT = 0.6
LABEL_CREDIT: dict[str, float] = {
    "covered": 1.0,
    "partial": PARTIAL_CREDIT,
    "not_covered": 0.0,
    "incorrect": 0.0,
}
# Aggregation order across turns. `incorrect` outranks `not_covered` only so the
# feedback can say "you got X wrong" rather than "you didn't mention X"; both
# score 0.
LABEL_RANK: dict[str, int] = {"covered": 3, "partial": 2, "incorrect": 1, "not_covered": 0}
# Same threshold the GPT lane uses for `per_step` (transcript_coverage: credit >= 0.5).
_PER_STEP_COVERED_AT = 0.5
# Turns judged concurrently (one HTTP call each).
MAX_CONCURRENT_TURNS = 8

_STUDENT_ROLE = "student"
_APOLLO_ROLE = "apollo"

# --- The exact wording Jev is given (recorded verbatim by the benchmark) ---------
JEV_INSTRUCTIONS_TEMPLATE = (
    "A student is teaching a learner. In the state, student_answer is the student's "
    "message being judged, apollo_question is the learner's message it replies to, and "
    "previous_student_answer is the student's message before it (context only, to resolve "
    "words like 'it' or 'that'). Judge ONLY student_answer against this reference idea "
    "from the problem's worked solution.\n"
    "Reference idea ({node_type}): {node_text}"
)
JEV_CRITERIA: dict[str, str] = {
    "covered": "The student answer expresses this idea correctly and essentially completely.",
    "partial": (
        "The student answer expresses part of this idea, or gestures at it without the key content."
    ),
    "not_covered": "The student answer does not address this idea.",
    "incorrect": "The student answer addresses this idea but says something wrong about it.",
}


@dataclass
class TurnNodeCell:
    turn_index: int
    node_id: str
    label: str
    probabilities: dict[str, float]
    confidence: float
    latency_ms: float


@dataclass
class JevJudgeResult:
    verdict: CoverageVerdict
    cells: list[TurnNodeCell]
    # Student-turn ordinal (0-based, aside rows excluded) the winning label came
    # from; None only when there were no student turns.
    winning_turn: dict[str, int | None]
    usage: dict[str, int]


@dataclass(frozen=True)
class StudentTurn:
    index: int
    apollo_question: str | None
    student_answer: str
    previous_student_answer: str | None


def node_text(item: Mapping[str, Any]) -> str:
    """One rubric item rendered as the reference idea Jev judges against."""
    content = json.dumps(item.get("content") or {}, ensure_ascii=False, sort_keys=True)
    name = item.get("display_name")
    return f"{name} — {content}" if name else content


def build_question(item: Mapping[str, Any]) -> dict[str, Any]:
    """The ``choice`` question for one graded node (lowercase types only)."""
    return {
        "type": "choice",
        "instructions": JEV_INSTRUCTIONS_TEMPLATE.format(
            node_type=item.get("type"), node_text=node_text(item)
        ),
        "criteria": dict(JEV_CRITERIA),
    }


def question_ids(rubric_items: Sequence[Mapping[str, Any]]) -> dict[str, str]:
    """``{qid: node_id}``. Positional qids keep arbitrary node ids off the wire keys."""
    return {f"n{i}": str(item["id"]) for i, item in enumerate(rubric_items)}


def build_question_payload(rubric_items: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """The ``questions`` object for one turn, keyed like :func:`question_ids`."""
    return {f"n{i}": build_question(item) for i, item in enumerate(rubric_items)}


def build_state(turn: StudentTurn) -> dict[str, str | None]:
    return {
        "apollo_question": turn.apollo_question,
        "student_answer": turn.student_answer,
        "previous_student_answer": turn.previous_student_answer,
    }


def _rows(transcript_messages: Sequence[Any]) -> list[tuple[str, str]]:
    """``(role, content)`` rows with aside-tagged rows dropped.

    Accepts the Done path's ``(role, content)`` tuples (already aside-filtered by
    ``done._full_transcript``) or mappings carrying ``role``/``content``/``intent``
    (e.g. exported message rows), which are filtered here the same way."""
    rows: list[tuple[str, str]] = []
    for message in transcript_messages:
        if isinstance(message, Mapping):
            if message.get("intent") == ASIDE_MESSAGE_INTENT_TAG:
                continue
            role, content = message.get("role"), message.get("content")
        else:
            role, content = message
        rows.append((str(role), "" if content is None else str(content)))
    return rows


def student_turns(transcript_messages: Sequence[Any]) -> list[StudentTurn]:
    """Every student message with the Apollo message it answers and the prior student turn."""
    turns: list[StudentTurn] = []
    last_apollo: str | None = None
    previous: str | None = None
    for role, content in _rows(transcript_messages):
        if role == _APOLLO_ROLE:
            last_apollo = content
        elif role == _STUDENT_ROLE:
            turns.append(
                StudentTurn(
                    index=len(turns),
                    apollo_question=last_apollo,
                    student_answer=content,
                    previous_student_answer=previous,
                )
            )
            previous = content
    return turns


async def _judge_turn(
    client: JevClient,
    turn: StudentTurn,
    questions: Mapping[str, Any],
    qid_to_node: Mapping[str, str],
    semaphore: asyncio.Semaphore,
) -> tuple[list[TurnNodeCell], dict[str, int]]:
    async with semaphore:
        response = await client.evaluate(build_state(turn), questions)
    cells: list[TurnNodeCell] = []
    for qid, node_id in qid_to_node.items():
        answer = response.answers.get(qid)
        if answer is None:
            raise JevError(f"jev returned no answer for {qid} (node {node_id})")
        if answer.choice not in LABEL_CREDIT:
            raise JevError(f"jev returned off-label choice {answer.choice!r} for node {node_id}")
        cells.append(
            TurnNodeCell(
                turn_index=turn.index,
                node_id=node_id,
                label=answer.choice,
                probabilities=dict(answer.probabilities),
                confidence=answer.confidence,
                latency_ms=response.latency_ms,
            )
        )
    return cells, response.usage


def _best_cell(cells: Sequence[TurnNodeCell]) -> TurnNodeCell:
    """Highest-ranked label; among equals the EARLIEST turn (stable evidence)."""
    return max(cells, key=lambda cell: (LABEL_RANK[cell.label], -cell.turn_index))


def aggregate(
    node_ids: Sequence[str], cells: Sequence[TurnNodeCell]
) -> tuple[CoverageVerdict, dict[str, int | None]]:
    """Reduce per-turn cells to the frozen coverage contract (pure, no IO)."""
    verdict: CoverageVerdict = {
        "per_step": {},
        "procedure_scores": {},
        "confidences": {},
        "negotiation_counts": {"dual": 0, "disputed": 0, "paraphrased": 0, "skipped": 0},
        "basis": {},
    }
    winning_turn: dict[str, int | None] = {}
    by_node: dict[str, list[TurnNodeCell]] = {node_id: [] for node_id in node_ids}
    for cell in cells:
        by_node.setdefault(cell.node_id, []).append(cell)
    for node_id in node_ids:
        node_cells = by_node[node_id]
        if node_cells:
            best = _best_cell(node_cells)
            label, confidence, turn = best.label, best.confidence, best.turn_index
        else:
            # Nothing was said: certainly not covered, and no turn to point at.
            label, confidence, turn = "not_covered", 1.0, None
        credit = LABEL_CREDIT[label]
        verdict["per_step"][node_id] = "covered" if credit >= _PER_STEP_COVERED_AT else "missing"
        verdict["procedure_scores"][node_id] = credit
        verdict["confidences"][node_id] = min(1.0, max(0.0, confidence))
        verdict["basis"][node_id] = label
        winning_turn[node_id] = turn
    validate_coverage_verdict(verdict)
    return verdict, winning_turn


async def judge_transcript(
    reference_graph: KGGraph,
    transcript_messages: Sequence[Any],
    *,
    client: JevClient | None = None,
) -> JevJudgeResult:
    """Judge every graded node of ``reference_graph`` against every student turn.

    Raises :class:`JevError` (or whatever the client raises) on any failure; the
    caller owns the fallback."""
    rubric_items = _build_rubric_items(reference_graph)
    node_ids = [str(item["id"]) for item in rubric_items]
    turns = student_turns(transcript_messages)
    cells: list[TurnNodeCell] = []
    usage: dict[str, int] = {}
    if node_ids and turns:
        jev = client or JevClient()
        questions = build_question_payload(rubric_items)
        qid_to_node = question_ids(rubric_items)
        semaphore = asyncio.Semaphore(MAX_CONCURRENT_TURNS)
        results = await asyncio.gather(
            *(_judge_turn(jev, turn, questions, qid_to_node, semaphore) for turn in turns)
        )
        for turn_cells, turn_usage in results:
            cells.extend(turn_cells)
            for key, value in turn_usage.items():
                usage[key] = usage.get(key, 0) + value
    verdict, winning_turn = aggregate(node_ids, cells)
    _LOG.info(
        "jev_node_judge_done turns=%d nodes=%d cells=%d usage=%s",
        len(turns),
        len(node_ids),
        len(cells),
        usage,
    )
    return JevJudgeResult(verdict=verdict, cells=cells, winning_turn=winning_turn, usage=usage)


def evidence_spans(result: JevJudgeResult, transcript_messages: Sequence[Any]) -> dict[str, str]:
    """Narrative spans for credited nodes: the winning student turn, verbatim.

    The evidence IS the turn, so it cannot be fabricated; it is one whole student
    message, so it passes ``transcript_coverage.validate_span``."""
    answers = [turn.student_answer for turn in student_turns(transcript_messages)]
    spans: dict[str, str] = {}
    for node_id, turn in result.winning_turn.items():
        credited = result.verdict["procedure_scores"].get(node_id, 0.0) > 0.0
        if credited and turn is not None and answers[turn].strip():
            spans[node_id] = answers[turn]
    return spans


__all__ = [
    "JEV_CRITERIA",
    "JEV_INSTRUCTIONS_TEMPLATE",
    "LABELS",
    "LABEL_CREDIT",
    "LABEL_RANK",
    "MAX_CONCURRENT_TURNS",
    "PARTIAL_CREDIT",
    "JevJudgeResult",
    "StudentTurn",
    "TurnNodeCell",
    "aggregate",
    "build_question",
    "build_question_payload",
    "build_state",
    "evidence_spans",
    "judge_transcript",
    "node_text",
    "question_ids",
    "student_turns",
]
