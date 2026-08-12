"""Offline replay gate for recorded Apollo transcript-grader fixtures.

Fixtures contain transcript, authored ``Problem`` payload, and a human-reviewed
structured adjudicator response. No network or database service is contacted:
the adjudicator seam (``transcript_coverage._call_adjudication``) is patched
with the fixture's recorded output.

Two entry points share ONE grading core:

* :func:`replay_fixture` — the permanent transcript-grader gate over
  ``campaign/fixtures/transcript_grader/`` (schema
  ``{problem, transcript, adjudicator_output, gate}``).
* :func:`grade_replay` — that core, reused verbatim by
  ``campaign/turn_replay.py`` for the at-Done half of a turn-level replay, so
  the two harnesses can never grade differently.

**P1.2b is no longer inert here (P3.2 W2-C).** This module used to call
``compute_topic_score`` with neither ``asked_node_ids`` nor ``evidence_spans``,
so the 2026-08-07 bimodal-fix denominator scoping was silently switched OFF in
replay and replay disagreed with production on exactly the arithmetic the fix
introduced. Both are now supplied from the SAME producers production uses —
``done._probed_node_ids`` and ``transcript_coverage.narrative_evidence_spans``.
A fixture with no ``question_opportunities`` key still yields
``asked_node_ids=None``, which ``compute_topic_score`` documents as reproducing
the pre-fix grade arithmetic exactly, so every pre-P3.2 fixture is unaffected.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

from apollo.handlers.done import _probed_node_ids
from apollo.overseer.topic_score import compute_centrality, compute_topic_score
from apollo.overseer.transcript_coverage import (
    compute_transcript_coverage_with_spans,
    validate_span,
)
from apollo.schemas.problem import Problem


@dataclass
class LedgerRow:
    """A ``QuestionOpportunity`` stand-in for offline replay.

    Duck-typed against the three production readers that matter offline —
    ``done._probed_node_ids``, ``done._latest_student_quote`` and
    ``smart_questions/controller._build_tally_state`` — none of which touch the
    ORM, all of which read these attribute names.

    **Mutable on purpose**, unlike everything else the replay holds. The
    production writer ``controller._apply_tally_updates`` assigns ``row.state``
    and ``row.evidence`` in place; a frozen row would force the harness to
    reimplement the one function it exists to exercise. Each replay run builds
    its rows fresh from the fixture payload (:func:`ledger_rows`), so a run
    never mutates the loaded fixture.

    ``evidence`` is a ``list``, never a tuple: both
    ``done._latest_student_quote`` and ``controller._evidence_rows`` gate on
    ``isinstance(value, list)`` and silently yield nothing for any other
    sequence — a tuple here would make the ledger look empty instead of failing.
    """

    reference_node_id: str
    state: str = "missing"
    times_asked: int = 0
    last_asked_turn: int | None = None
    asked_turn: int | None = None
    answered_turn: int | None = None
    question: str = ""
    evidence: list[dict[str, Any]] = field(default_factory=list)


def ledger_rows(payload: Iterable[dict[str, Any]] | None) -> list[LedgerRow] | None:
    """Fresh :class:`LedgerRow` objects from a fixture's ``question_opportunities``.

    ``None`` in, ``None`` out — the fixture carries no ledger, which is the
    pre-P3.2 transcript-grader schema, and ``asked_node_ids=None`` reproduces
    the pre-P1.2b grade arithmetic exactly. Evidence entries are deep-copied so
    a replay can append to them without touching the caller's payload.
    """
    if payload is None:
        return None
    return [
        LedgerRow(
            reference_node_id=str(row["reference_node_id"]),
            state=str(row.get("state", "missing")),
            times_asked=int(row.get("times_asked") or 0),
            last_asked_turn=row.get("last_asked_turn"),
            asked_turn=row.get("asked_turn"),
            answered_turn=row.get("answered_turn"),
            question=str(row.get("question") or ""),
            evidence=copy.deepcopy(list(row.get("evidence") or [])),
        )
        for row in payload
    ]


@dataclass(frozen=True)
class ReplayOutcome:
    name: str
    score: int
    letter: str
    credited_topics: int
    validated_spans: bool
    #: The P1.2b engagement scope this replay actually applied, or ``None`` when
    #: the fixture carried no ledger. Reported so a replay can never again claim
    #: to agree with production while silently running the feature OFF.
    asked_node_ids: tuple[str, ...] | None = None
    topic_credits: dict[str, float] = field(default_factory=dict)


def fixture_paths(directory: Path) -> tuple[Path, ...]:
    """Every ``*.json`` fixture in ``directory``, or a loud failure.

    An empty (or missing) fixture directory is a HARNESS defect, not a failed
    gate: ``run()`` used to fold it into ``passed=False``, which reads as "the
    fixtures regressed" when in fact none were ever exported. Raises
    ``SystemExit`` naming the directory instead.
    """
    paths = tuple(sorted(directory.glob("*.json"))) if directory.is_dir() else ()
    if not paths:
        raise SystemExit(
            f"no *.json fixtures found in {directory} — "
            "export the fixture set before running this gate "
            "(an empty directory is a harness defect, not a failing gate)"
        )
    return paths


async def grade_replay(
    *,
    problem: Problem,
    transcript: Sequence[tuple[str, str]],
    adjudicator_output: dict[str, Any],
    rows: Sequence[Any] | None = None,
    wrongness_candidates: Mapping[str, str] | None = None,
    name: str = "",
) -> ReplayOutcome:
    """Grade one recorded attempt exactly as the Done path grades it.

    ``rows`` are the attempt's question-opportunity rows (any object exposing
    the ``QuestionOpportunity`` attribute names — :class:`LedgerRow` is the
    offline one). ``None`` means "this fixture has no ledger", which
    ``compute_topic_score`` reproduces as the pre-P1.2b arithmetic.

    Uses ``compute_transcript_coverage_with_spans`` — the same call
    ``handlers/done`` makes — so the coverage dict and the narrative spans come
    from ONE adjudication, and ``evidence_spans`` is the real
    ``narrative_evidence_spans`` gate rather than a replay-local approximation.
    The numeric-only twin ``compute_transcript_coverage`` cannot serve here: it
    returns no verdicts, so the spans would need either a second adjudication or
    a private copy of the gate.

    ``wrongness_candidates`` (P3.2 seam S5) rides straight through, so a
    level->=1 campaign arm exercises the corroboration lane offline exactly as
    production does. ``None``/``{}`` reproduces today's prompts and verdict.
    """
    graph = problem.to_kg_graph(attempt_id=-1)
    recorded = json.dumps(adjudicator_output)
    with patch("apollo.overseer.transcript_coverage._call_adjudication", return_value=recorded):
        coverage, narrative_spans = await compute_transcript_coverage_with_spans(
            transcript=transcript,
            reference_graph=graph,
            problem=problem,
            wrongness_candidates=wrongness_candidates,
        )
    asked_node_ids = None if rows is None else _probed_node_ids(rows)
    topic_score = compute_topic_score(
        coverage=cast(dict, coverage),
        reference_nodes=graph.nodes,
        centrality=compute_centrality(graph),
        evidence_spans=narrative_spans,
        asked_node_ids=asked_node_ids,
    )
    credited = sum(topic.credit > 0 for topic in topic_score.topics)
    student_messages = [content for role, content in transcript if role == "student"]
    spans = [
        v.get("evidence_span") for v in adjudicator_output["verdicts"] if v.get("credit", 0) > 0
    ]
    # Reuse the serving-lane rail — a private reimplementation here would
    # silently drift from the per-message check it is supposed to mirror.
    validated = all(validate_span(span, student_messages) for span in spans)
    return ReplayOutcome(
        name=name,
        score=topic_score.score,
        letter=topic_score.letter,
        credited_topics=credited,
        validated_spans=validated,
        asked_node_ids=None if asked_node_ids is None else tuple(sorted(asked_node_ids)),
        topic_credits={topic.canonical_key: topic.credit for topic in topic_score.topics},
    )


async def replay_fixture(path: Path) -> ReplayOutcome:
    fixture = json.loads(path.read_text(encoding="utf-8"))
    problem = Problem.model_validate(fixture["problem"])
    transcript = tuple((m["role"], m["content"]) for m in fixture["transcript"])
    return await grade_replay(
        problem=problem,
        transcript=transcript,
        adjudicator_output=fixture["adjudicator_output"],
        rows=ledger_rows(fixture.get("question_opportunities")),
        name=path.stem,
    )


def _passes_gate(outcome: ReplayOutcome, fixture: dict) -> bool:
    gate = fixture.get("gate", {})
    return (
        outcome.score >= gate.get("min_score", 0)
        and outcome.score < gate.get("max_score_exclusive", 101)
        and outcome.credited_topics <= gate.get("max_credited_topics", 10**9)
        and (not gate.get("require_validated_spans", True) or outcome.validated_spans)
    )


async def run(fixtures: Path) -> tuple[list[ReplayOutcome], bool]:
    outcomes: list[ReplayOutcome] = []
    passed = True
    for path in fixture_paths(fixtures):
        try:
            outcome = await replay_fixture(path)
            outcomes.append(outcome)
            passed = passed and _passes_gate(outcome, json.loads(path.read_text(encoding="utf-8")))
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            passed = False
    return outcomes, passed and bool(outcomes)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixtures", type=Path, required=True)
    args = parser.parse_args()
    outcomes, passed = asyncio.run(run(args.fixtures))
    for outcome in outcomes:
        print(json.dumps(asdict(outcome), sort_keys=True))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
