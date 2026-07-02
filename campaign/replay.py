"""Task 8 (weekend/g1-replay-benchmark) — the F1c resolver-recall replay harness.

F1c (the frozen 36-attempt fluid_mechanics campaign corpus recorded at
``campaign/out/f1c/attempts.jsonl``) abstained on 31/31 graded attempts with
reason ``unresolved_rate_above_threshold`` — a resolver-recall failure (see
``docs/_archive/handoffs`` / MEMORY.md ``apollo-grader-abstention-defect``).
This module re-runs ONLY the resolution+grading stage of a Done click over
those already-recorded attempts, so a future resolver fix hypothesis can be
scored in seconds instead of re-running the full LLM-student campaign.

Design (why this shape, not a from-scratch reimplementation):

``attempts.jsonl`` records the campaign driver's HTTP transcript PLUS the
already-computed grading artifacts (``artifact_canonical``/``artifact_pair``)
from the ORIGINAL run — it does not carry the raw pre-resolution student
:class:`~apollo.ontology.KGGraph` or the problem's candidate set (those never
leave the server process in the HTTP campaign driver). Both are durable,
however: the student graph is frozen per-attempt in Neo4j
(``KGStore.read_graph``) and the problem/candidates are keyed off
``ProblemAttempt.problem_id`` in Postgres — exactly the state the existing
learner-update RETRY JANITOR (WU-5B3a-0/1, ``apollo/handlers/done_inputs.py``)
already knows how to reconstruct. So this replay harness is a thin driver
over the SAME two functions the live Done path and the retry janitor call:

  * :func:`apollo.handlers.done_inputs.build_rerun_inputs` — reloads
    ``problem_payload`` / ``old_rubric`` / the frozen ``student_graph`` from
    Postgres + Neo4j for one ``(attempt, sess)`` pair.
  * :func:`apollo.handlers.done_grading.run_graph_simulation` — the real
    ``resolve -> RESOLVES_TO -> canonicalize -> grade -> audit -> persist``
    chain (§6.4). This module imports and calls it; it does not reimplement
    any grading/resolution logic.

No live LLM student turn, no live session creation, no HTTP server — only the
DB/Neo4j reads + the deterministic (module-injected NLI aside) grading chain.
Per-attempt DB writes ``run_graph_simulation`` performs (a fresh
``apollo_graph_comparison_runs`` row) are the same idempotent-MERGE writes the
retry janitor produces; they do not touch ``campaign/out/f1c/attempts.jsonl``
or any other frozen fixture.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from apollo.errors import (
    LearnerUpdateUnreconstructableError,
    ResolutionInvalidOutputError,
    ResolutionUnavailableError,
    TranscriptAuditUnavailableError,
)
from apollo.grading.abstention import unresolved_rate_of
from apollo.grading.artifact_build import build_graph_artifact
from apollo.grading.composite import load_weights
from apollo.graph_compare.validator import (
    ReferenceGraphInvalidError,
    StudentGraphInvalidError,
)
from apollo.handlers.done_grading import run_graph_simulation
from apollo.handlers.done_inputs import build_rerun_inputs
from apollo.persistence.models import ApolloSession, ProblemAttempt

_LOG = logging.getLogger(__name__)

# Persona classes whose recorded student teaching is deliberately deficient
# (an unresolved concept, or an outright misconception) — the resolver must
# NEVER grant these full resolution credit, so they are the sanity check for
# a resolver-recall fix that over-corrects into false credit.
CONTROL_PERSONAS: frozenset[str] = frozenset({"misconception", "vague_then_clarifies"})

# Errors the live chain treats as a per-attempt failure (see
# ``done_grading.run_graph_simulation``'s NO-FALLBACK contract + the retry
# janitor's dead-letter). The replay harness catches the SAME set so one bad
# attempt cannot crash the whole batch; each is recorded as a distinct
# abstention-histogram bucket (``replay_error:<ClassName>``) rather than
# silently dropped.
_REPLAYABLE_ERRORS: tuple[type[Exception], ...] = (
    LearnerUpdateUnreconstructableError,
    ResolutionUnavailableError,
    ResolutionInvalidOutputError,
    TranscriptAuditUnavailableError,
    ReferenceGraphInvalidError,
    StudentGraphInvalidError,
)


@dataclass(frozen=True)
class ReplayOutcome:
    """One attempt's replayed resolution+grading result."""

    attempt_id: int
    persona: str
    is_control: bool
    unresolved_rate: float
    abstained: bool
    abstention_reasons: tuple[str, ...]
    graph_composite: float
    actual_credited: tuple[str, ...]
    actual_unresolved: tuple[str, ...]
    actual_misconceptions: tuple[str, ...]
    expected_credited: tuple[str, ...]
    expected_unresolved: tuple[str, ...]
    expected_misconceptions: tuple[str, ...]
    control_credit_leak: bool


@dataclass(frozen=True)
class ReplayError:
    """An attempt that could not be replayed (records WHY, never crashes the batch)."""

    attempt_id: int
    persona: str
    reason: str


def load_records(
    attempts_path: Path | str, *, personas: Sequence[str] | None = None
) -> list[dict[str, Any]]:
    """Read the frozen JSONL transcript log, keeping only attempts that
    actually reached the grading stage (``status == "ok"`` — the campaign
    driver leaves ``attempt_id`` ``None`` on an HTTP-level failure, so those
    5/36 F1c rows have nothing to replay) and whose recorded ``persona``
    class is in ``personas`` (all classes when ``personas`` is ``None``).

    Read-only: never mutates ``attempts_path``.
    """
    wanted = set(personas) if personas is not None else None
    records: list[dict[str, Any]] = []
    with Path(attempts_path).open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            record = json.loads(line)
            if record.get("status") != "ok" or record.get("attempt_id") is None:
                continue
            if wanted is not None and record.get("persona") not in wanted:
                continue
            records.append(record)
    return records


def _ledger_keys(artifact: Mapping[str, Any], status: str) -> tuple[str, ...]:
    return tuple(
        sorted(
            entry["canonical_key"]
            for entry in artifact.get("node_ledger", [])
            if entry.get("status") == status
        )
    )


def _misconception_keys(artifact: Mapping[str, Any]) -> tuple[str, ...]:
    return tuple(sorted(m.get("canonical_key", "") for m in artifact.get("misconceptions", [])))


async def _load_attempt_and_session(
    db: Any, *, attempt_id: int
) -> tuple[ProblemAttempt, ApolloSession]:
    """Reload the durable ``(ProblemAttempt, ApolloSession)`` pair the F1c run
    left in Postgres — the same rows the live Done path and the retry
    janitor operate on."""
    attempt = await db.get(ProblemAttempt, attempt_id)
    if attempt is None:
        raise LearnerUpdateUnreconstructableError(
            attempt_id=attempt_id, reason="attempt_not_found_in_local_stack"
        )
    sess = await db.get(ApolloSession, int(attempt.session_id))
    if sess is None:
        raise LearnerUpdateUnreconstructableError(
            attempt_id=attempt_id, reason="session_not_found_in_local_stack"
        )
    return attempt, sess


async def replay_attempt(
    db: Any, neo: Any, record: Mapping[str, Any]
) -> ReplayOutcome | ReplayError:
    """Re-run resolution+grading for one recorded F1c attempt.

    Calls ONLY :func:`build_rerun_inputs` + :func:`run_graph_simulation` — the
    real chain the retry janitor and the live Done path both use. Any of the
    named ``_REPLAYABLE_ERRORS`` is caught and returned as a :class:`ReplayError`
    (never raised) so one bad attempt cannot abort the batch.
    """
    attempt_id = int(record["attempt_id"])
    persona = str(record.get("persona", ""))
    try:
        attempt, sess = await _load_attempt_and_session(db, attempt_id=attempt_id)
        inputs = await build_rerun_inputs(db, neo, attempt=attempt, sess=sess)
        shadow = await run_graph_simulation(
            db,
            neo,
            attempt=attempt,
            sess=sess,
            student_graph=inputs.student_graph,
            problem_payload=inputs.problem_payload,
            old_rubric=inputs.old_rubric,
        )
    except _REPLAYABLE_ERRORS as exc:
        _LOG.warning(
            "replay_attempt_failed attempt_id=%s persona=%s error=%s", attempt_id, persona, exc
        )
        return ReplayError(attempt_id=attempt_id, persona=persona, reason=type(exc).__name__)

    if shadow is None:  # pragma: no cover - defensive: run_graph_simulation's happy path
        # never returns None (it either returns a ShadowGradeResult or raises
        # one of the named _REPLAYABLE_ERRORS above); this guard only narrows
        # the declared `ShadowGradeResult | None` type for mypy.
        return ReplayError(
            attempt_id=attempt_id, persona=persona, reason="ShadowGradeResultMissing"
        )

    artifact = build_graph_artifact(
        shadow=shadow,
        weights=load_weights(),
        clarification_trace=[],
        latency_ms=None,
    )
    expected = record.get("expected") or {}
    is_control = persona in CONTROL_PERSONAS
    actual_credited = _ledger_keys(artifact, "credited")
    expected_credited = tuple(sorted(expected.get("credited", [])))
    # A control persona (misconception / vague) must not gain resolution
    # credit for concepts it never taught correctly — any actual credit key
    # absent from the expected credited set on a control attempt is a leak.
    control_credit_leak = is_control and bool(set(actual_credited) - set(expected_credited))

    return ReplayOutcome(
        attempt_id=attempt_id,
        persona=persona,
        is_control=is_control,
        unresolved_rate=unresolved_rate_of(shadow.resolution),
        abstained=bool(shadow.audited.abstained),
        abstention_reasons=tuple(shadow.audited.abstention_reasons),
        graph_composite=float(artifact["scores"]["composite"]),
        actual_credited=actual_credited,
        actual_unresolved=_ledger_keys(artifact, "unresolved"),
        actual_misconceptions=_misconception_keys(artifact),
        expected_credited=expected_credited,
        expected_unresolved=tuple(sorted(expected.get("unresolved", []))),
        expected_misconceptions=tuple(sorted(expected.get("misconceptions", []))),
        control_credit_leak=control_credit_leak,
    )


def _mean(values: Iterable[float]) -> float | None:
    values = list(values)
    if not values:
        return None
    return sum(values) / len(values)


@dataclass(frozen=True)
class ReplayMetrics:
    """The four required per-run metric keys (Task 8 brief), plus the raw
    per-attempt outcomes/errors for debugging. ``as_dict`` is the JSON-
    serializable shape written to ``replay-baseline-*.json``."""

    unresolved_rate: dict[str, dict[str, Any]]
    abstention_reasons: dict[str, int]
    graph_composite: dict[str, dict[str, Any]]
    band_vs_expected: list[dict[str, Any]]
    errors: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "unresolved_rate": self.unresolved_rate,
            "abstention_reasons": self.abstention_reasons,
            "graph_composite": self.graph_composite,
            "band_vs_expected": self.band_vs_expected,
            "errors": self.errors,
        }


def summarize(results: Sequence[ReplayOutcome | ReplayError]) -> ReplayMetrics:
    """Aggregate replayed per-attempt outcomes into the four required metrics."""
    outcomes = [r for r in results if isinstance(r, ReplayOutcome)]
    errors = [r for r in results if isinstance(r, ReplayError)]

    personas = sorted({o.persona for o in outcomes})

    unresolved_rate: dict[str, dict[str, Any]] = {}
    graph_composite: dict[str, dict[str, Any]] = {}
    for persona in personas:
        rows = [o for o in outcomes if o.persona == persona]
        unresolved_rate[persona] = {
            "mean": _mean(o.unresolved_rate for o in rows),
            "n": len(rows),
            "values": [o.unresolved_rate for o in rows],
        }
        graph_composite[persona] = {
            "mean": _mean(o.graph_composite for o in rows),
            "n": len(rows),
            "values": [o.graph_composite for o in rows],
        }

    abstention_reasons: dict[str, int] = {}
    for o in outcomes:
        if not o.abstention_reasons:
            abstention_reasons["<none>"] = abstention_reasons.get("<none>", 0) + 1
            continue
        for reason in o.abstention_reasons:
            abstention_reasons[reason] = abstention_reasons.get(reason, 0) + 1
    for e in errors:
        bucket = f"replay_error:{e.reason}"
        abstention_reasons[bucket] = abstention_reasons.get(bucket, 0) + 1

    band_vs_expected = [
        {
            "attempt_id": o.attempt_id,
            "persona": o.persona,
            "is_control": o.is_control,
            "abstained": o.abstained,
            "expected_credited": list(o.expected_credited),
            "actual_credited": list(o.actual_credited),
            "expected_unresolved": list(o.expected_unresolved),
            "actual_unresolved": list(o.actual_unresolved),
            "expected_misconceptions": list(o.expected_misconceptions),
            "actual_misconceptions": list(o.actual_misconceptions),
            "control_credit_leak": o.control_credit_leak,
        }
        for o in outcomes
    ]

    return ReplayMetrics(
        unresolved_rate=unresolved_rate,
        abstention_reasons=abstention_reasons,
        graph_composite=graph_composite,
        band_vs_expected=band_vs_expected,
        errors=[
            {"attempt_id": e.attempt_id, "persona": e.persona, "reason": e.reason} for e in errors
        ],
    )


async def run_replay(
    *, run_dir: Path | str, personas: Sequence[str] | None, db: Any, neo: Any
) -> ReplayMetrics:
    """Replay every matching, gradeable attempt under ``run_dir/attempts.jsonl``
    and return the aggregated :class:`ReplayMetrics`."""
    attempts_path = Path(run_dir) / "attempts.jsonl"
    records = load_records(attempts_path, personas=personas)
    results: list[ReplayOutcome | ReplayError] = []
    for record in records:
        results.append(await replay_attempt(db, neo, record))
    return summarize(results)


async def _amain(argv: Sequence[str] | None = None) -> ReplayMetrics:
    parser = argparse.ArgumentParser(
        prog="python -m campaign.replay",
        description=(
            "Replay ONLY the resolution+grading stage over a frozen campaign "
            "attempts.jsonl (no live LLM student, no HTTP)."
        ),
    )
    parser.add_argument(
        "--run-dir",
        required=True,
        help="Campaign run directory containing attempts.jsonl (e.g. campaign/out/f1c)",
    )
    parser.add_argument(
        "--personas",
        default=None,
        help="Comma-separated persona classes to replay (default: all recorded classes)",
    )
    parser.add_argument(
        "--out",
        default=None,
        help="Optional path to write the metrics JSON (default: print to stdout)",
    )
    args = parser.parse_args(argv)
    personas = [p.strip() for p in args.personas.split(",") if p.strip()] if args.personas else None

    # Imported lazily so unit tests that never touch --run-dir don't need a
    # live DB/Neo4j configured just to import this module.
    from apollo.persistence.neo4j_client import Neo4jClient
    from database.session import get_db_session

    neo = Neo4jClient.from_env()
    try:
        async for db in get_db_session():
            metrics = await run_replay(run_dir=args.run_dir, personas=personas, db=db, neo=neo)
            break
    finally:
        await neo.close()

    payload = json.dumps(metrics.as_dict(), indent=2, sort_keys=True)
    if args.out:
        Path(args.out).write_text(payload, encoding="utf-8")
    else:
        print(payload)  # noqa: T201 - CLI output is the product
    return metrics


def main(argv: Sequence[str] | None = None) -> None:
    asyncio.run(_amain(argv))


if __name__ == "__main__":  # pragma: no cover - thin CLI entrypoint
    main()
