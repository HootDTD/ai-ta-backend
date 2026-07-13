"""DAG-5 local replay: compare contraction grading OFF versus ON.

Runs the frozen campaign corpus through the real shadow grading chain twice.
Requires the local Docker-backed Postgres and Neo4j state that produced the
recorded attempts; it is a manual evaluation harness and never runs in CI.

Usage:
    python scripts/dag5_contraction_replay.py
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from apollo.grading.events import convert_findings_to_events  # noqa: E402
from apollo.graph_compare.findings import FindingKind  # noqa: E402
from apollo.handlers.done_grading import run_graph_simulation  # noqa: E402
from apollo.handlers.done_inputs import build_rerun_inputs  # noqa: E402
from apollo.persistence.models import ApolloSession, ProblemAttempt  # noqa: E402
from campaign.replay import load_records  # noqa: E402

_DEFAULT_CORPUS = Path("campaign/out/f1c/attempts.jsonl")


def _event_dict(event) -> dict[str, Any]:
    return {
        "canonical_key": event.canonical_key,
        "event_kind": event.event_kind.value,
        "score": event.score,
        "reference_step_id": event.reference_step_id,
    }


def _events(shadow) -> list[dict[str, Any]]:
    return [
        _event_dict(event)
        for event in convert_findings_to_events(
            shadow.audited,
            opposes_map=shadow.opposes_map,
            turn_order=shadow.turn_order,
        )
    ]


def _count(shadow, kind: FindingKind) -> int:
    return sum(finding.kind == kind for finding in shadow.audited.findings)


def _removed_missing_zero(
    off_events: list[dict[str, Any]], on_events: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    on_signatures = {
        (event["canonical_key"], event["event_kind"], event["score"]) for event in on_events
    }
    return [
        event
        for event in off_events
        if event["event_kind"] == "missing"
        and event["score"] == 0.0
        and (event["canonical_key"], event["event_kind"], event["score"]) not in on_signatures
    ]


async def _load_attempt_and_session(db: Any, attempt_id: int):
    attempt = await db.get(ProblemAttempt, attempt_id)
    if attempt is None:
        raise RuntimeError(f"attempt {attempt_id} is absent from local Postgres")
    sess = await db.get(ApolloSession, int(attempt.session_id))
    if sess is None:
        raise RuntimeError(f"session for attempt {attempt_id} is absent from local Postgres")
    return attempt, sess


async def replay_one(db: Any, neo: Any, record: Mapping[str, Any]) -> dict[str, Any]:
    attempt_id = int(record["attempt_id"])
    attempt, sess = await _load_attempt_and_session(db, attempt_id)
    rerun = await build_rerun_inputs(db, neo, attempt=attempt, sess=sess)
    common = {
        "attempt": attempt,
        "sess": sess,
        "student_graph": rerun.student_graph,
        "problem_payload": rerun.problem_payload,
        "old_rubric": rerun.old_rubric,
    }
    off = await run_graph_simulation(db, neo, **common, contraction_enabled=False)
    on = await run_graph_simulation(db, neo, **common, contraction_enabled=True)
    if off is None or on is None:
        raise RuntimeError(f"attempt {attempt_id} returned no shadow result")

    off_events = _events(off)
    on_events = _events(on)
    return {
        "attempt_id": attempt_id,
        "persona": record.get("persona"),
        "node_coverage": {
            "off": off.grade.node_coverage_score,
            "on": on.grade.node_coverage_score,
            "delta": on.grade.node_coverage_score - off.grade.node_coverage_score,
        },
        "covered_by_contraction": _count(on, FindingKind.COVERED_BY_CONTRACTION),
        "not_demonstrated": _count(on, FindingKind.NOT_DEMONSTRATED),
        "mastery_events": {
            "off": off_events,
            "on": on_events,
            "removed_s0_missing": _removed_missing_zero(off_events, on_events),
        },
    }


async def run(corpus: Path, db: Any, neo: Any) -> dict[str, Any]:
    records = load_records(corpus)
    attempts: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for record in records:
        try:
            attempts.append(await replay_one(db, neo, record))
        except Exception as exc:  # manual harness: retain the rest of the corpus
            cause = exc.__cause__ or exc.__context__
            errors.append(
                {
                    "attempt_id": record.get("attempt_id"),
                    "error": f"{type(exc).__name__}: {exc}",
                    "cause": f"{type(cause).__name__}: {cause}" if cause else None,
                }
            )
            # A flush failure poisons the shared session; reset it so one bad
            # attempt cannot cascade PendingRollbackError over the corpus.
            try:
                await db.rollback()
            except Exception:  # pragma: no cover - best-effort reset
                pass
    return {
        "corpus": str(corpus),
        "attempts": attempts,
        "totals": {
            "attempts": len(attempts),
            "errors": len(errors),
            "covered_by_contraction": sum(row["covered_by_contraction"] for row in attempts),
            "not_demonstrated": sum(row["not_demonstrated"] for row in attempts),
            "removed_s0_missing": sum(
                len(row["mastery_events"]["removed_s0_missing"]) for row in attempts
            ),
        },
        "errors": errors,
    }


async def _amain(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, default=_DEFAULT_CORPUS)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)

    from apollo.persistence.neo4j_client import Neo4jClient
    from database.session import get_db_session

    neo = Neo4jClient.from_env()
    try:
        async for db in get_db_session():
            payload = await run(args.corpus, db, neo)
            break
    finally:
        await neo.close()

    rendered = json.dumps(payload, indent=2, sort_keys=True)
    if args.out is None:
        print(rendered)  # noqa: T201 - CLI output is the product
    else:
        args.out.write_text(rendered + "\n", encoding="utf-8")


def main(argv: Sequence[str] | None = None) -> None:
    asyncio.run(_amain(argv))


if __name__ == "__main__":
    main()
