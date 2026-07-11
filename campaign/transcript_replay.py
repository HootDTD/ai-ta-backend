"""Offline replay gate for recorded Apollo transcript-grader fixtures.

Fixtures contain transcript, authored ``Problem`` payload, and a human-reviewed
structured adjudicator response. No network or database service is contacted.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from unittest.mock import patch

from apollo.overseer.misconception_detector.centrality import compute_centrality
from apollo.overseer.topic_score import compute_topic_score
from apollo.overseer.transcript_coverage import compute_transcript_coverage
from apollo.schemas.problem import Problem


@dataclass(frozen=True)
class ReplayOutcome:
    name: str
    score: int
    letter: str
    credited_topics: int
    validated_spans: bool


async def replay_fixture(path: Path) -> ReplayOutcome:
    fixture = json.loads(path.read_text(encoding="utf-8"))
    problem = Problem.model_validate(fixture["problem"])
    graph = problem.to_kg_graph(attempt_id=-1)
    transcript = tuple((m["role"], m["content"]) for m in fixture["transcript"])
    recorded = json.dumps(fixture["adjudicator_output"])
    with patch("apollo.overseer.transcript_coverage._call_adjudication", return_value=recorded):
        coverage = await compute_transcript_coverage(transcript, graph, problem)
    topic_score = compute_topic_score(
        coverage=coverage,
        reference_nodes=graph.nodes,
        centrality=compute_centrality(graph),
        detection_outcome=None,
    )
    credited = sum(topic.credit > 0 for topic in topic_score.topics)
    student_text = " ".join(content for role, content in transcript if role == "student")
    spans = [v.get("evidence_span") for v in fixture["adjudicator_output"]["verdicts"] if v.get("credit", 0) > 0]
    validated = all(span and " ".join(span.split()) in " ".join(student_text.split()) for span in spans)
    return ReplayOutcome(path.stem, topic_score.score, topic_score.letter, credited, validated)


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
    for path in sorted(fixtures.glob("*.json")):
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
