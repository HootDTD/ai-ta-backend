"""P1.2b is no longer inert in transcript replay (P3.2 W2-C, spec §4 trap 2).

``campaign/transcript_replay.py`` used to call ``compute_topic_score`` with
neither ``asked_node_ids`` nor ``evidence_spans``, so the 2026-08-07 bimodal-fix
denominator scoping was silently switched OFF in replay: the offline gate and
production graded the same attempt by different arithmetic, and no test said so.

These tests pin the fix from both sides — the feature is now live in replay AND
a ledger-less fixture still reproduces the pre-fix arithmetic exactly.

No DB, no network: the adjudicator seam is patched with recorded output.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from apollo.handlers import done
from apollo.overseer.topic_score import compute_centrality, compute_topic_score
from apollo.schemas.problem import Problem
from apollo.smart_questions.selection import is_graded
from campaign import transcript_replay
from campaign.transcript_replay import (
    LedgerRow,
    fixture_paths,
    grade_replay,
    ledger_rows,
    replay_fixture,
    run,
)

pytestmark = pytest.mark.unit

TURN_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "turn_replay"
UNENGAGED = "extra_step_6"
TRANSCRIPT = (("student", "I taught the four impairments and how they threaten dignity."),)


def _wide_problem() -> Problem:
    """A real authored problem widened to FOUR graded nodes.

    ``MIN_GRADED_DENOMINATOR = 2`` means P1.2b is structurally inert on a
    two-graded-node rubric (the floor re-admits every dropped node), and every
    committed turn-replay fixture has one or two. Demonstrating the fix needs a
    rubric wide enough for the floor to leave room — so this widens a REAL
    payload with two more procedure steps rather than inventing a problem.
    """
    payload: dict[str, Any] = copy.deepcopy(
        json.loads(
            (TURN_FIXTURES / "attempt_124_conflicting_graded.json").read_text(encoding="utf-8")
        )["problem"]
    )
    template = next(
        step for step in payload["reference_solution"] if step["id"] == "q2_connect_to_dignity"
    )
    for order, step_number in ((2, 5), (3, 6)):
        step = copy.deepcopy(template)
        step["id"] = f"extra_step_{step_number}"
        step["step"] = step_number
        step["content"]["order"] = order
        payload["reference_solution"].append(step)
    return Problem.model_validate(payload)


def _graded_ids(problem: Problem) -> tuple[str, ...]:
    return tuple(
        node.node_id for node in problem.to_kg_graph(attempt_id=-1).nodes if is_graded(node)
    )


def _adjudicator_output(
    problem: Problem, *, span: str | None = None, contradicted: bool = False
) -> dict[str, Any]:
    """Full credit everywhere except :data:`UNENGAGED`, which is a clean zero."""
    return {
        "verdicts": [
            {
                "node_id": node_id,
                "covered": node_id != UNENGAGED,
                "credit": 0.0 if node_id == UNENGAGED else 1.0,
                "confidence": 0.9,
                "evidence_span": None if node_id == UNENGAGED else span,
                "prompted": False,
                "corrected_later": False,
                "contradicted": contradicted,
                "basis": "absent" if node_id == UNENGAGED else "stated",
                "hoot_assisted": False,
            }
            for node_id in _graded_ids(problem)
        ]
    }


def _engaged_rows(problem: Problem) -> list[LedgerRow]:
    rows = ledger_rows(
        [
            {"reference_node_id": node_id, "state": "understood", "times_asked": 1, "evidence": []}
            for node_id in _graded_ids(problem)
            if node_id != UNENGAGED
        ]
    )
    assert rows is not None
    return rows


# --------------------------------------------------------------------------- #
# The fix                                                                      #
# --------------------------------------------------------------------------- #


async def test_asked_node_ids_now_passed_to_compute_topic_score() -> None:
    """The kwargs the old call omitted are both present, from the real producers."""
    problem = _wide_problem()
    with patch(
        "campaign.transcript_replay.compute_topic_score", wraps=compute_topic_score
    ) as scorer:
        await grade_replay(
            problem=problem,
            transcript=TRANSCRIPT,
            adjudicator_output=_adjudicator_output(problem),
            rows=_engaged_rows(problem),
            name="wide",
        )
    kwargs = scorer.call_args.kwargs
    assert isinstance(kwargs["asked_node_ids"], frozenset)
    assert kwargs["asked_node_ids"] == frozenset(
        node_id for node_id in _graded_ids(problem) if node_id != UNENGAGED
    )
    assert "evidence_spans" in kwargs


async def test_wrongness_candidates_ride_through_to_the_corroborator() -> None:
    """Seam S5 stays reachable from replay after the switch to the spans twin.

    ``compute_transcript_coverage`` (numeric-only) documents the candidate kwarg
    as existing "so the offline replay can exercise the same corroboration
    lane". Replay now calls ``compute_transcript_coverage_with_spans`` instead —
    it needs the verdicts for ``evidence_spans`` — so the lane has to be reachable
    through THAT call, and this proves it is.
    """
    problem = _wide_problem()
    graded = _graded_ids(problem)[0]
    with patch(
        "campaign.transcript_replay.compute_topic_score", wraps=compute_topic_score
    ) as scorer:
        await grade_replay(
            problem=problem,
            transcript=TRANSCRIPT,
            adjudicator_output=_adjudicator_output(problem, contradicted=True),
            wrongness_candidates={graded: "I taught the four impairments"},
            name="candidates",
        )
    coverage = scorer.call_args.kwargs["coverage"]
    assert coverage["wrongness"][graded]["contradicted"] is True

    with patch(
        "campaign.transcript_replay.compute_topic_score", wraps=compute_topic_score
    ) as scorer:
        await grade_replay(
            problem=problem,
            transcript=TRANSCRIPT,
            adjudicator_output=_adjudicator_output(problem, contradicted=True),
            name="no-candidates",
        )
    # No candidates ⇒ the corroborator cannot originate a finding, so the key is
    # absent entirely rather than present-and-false.
    assert "wrongness" not in scorer.call_args.kwargs["coverage"]


def test_probed_node_ids_has_exactly_one_producer() -> None:
    """Replay must use ``done._probed_node_ids``, not a private copy of it.

    A bare ledger row is deliberately NOT proof of engagement (degenerate
    fallback turns and bare ``missing`` tally updates both mint rows), and a
    replay-local reimplementation of that rule is how the two lanes drift.
    """
    assert transcript_replay._probed_node_ids is done._probed_node_ids


async def test_p1_2b_no_longer_inert_in_replay() -> None:
    """A graded node the loop never engaged, credited 0, leaves the denominator.

    Same coverage, same verdicts, same transcript — only the ledger differs, and
    the score moves. Before the fix both calls returned the identical number,
    which is exactly what "inert" meant.
    """
    problem = _wide_problem()
    adjudicator_output = _adjudicator_output(problem)

    with_ledger = await grade_replay(
        problem=problem,
        transcript=TRANSCRIPT,
        adjudicator_output=adjudicator_output,
        rows=_engaged_rows(problem),
        name="with",
    )
    without_ledger = await grade_replay(
        problem=problem,
        transcript=TRANSCRIPT,
        adjudicator_output=adjudicator_output,
        rows=None,
        name="without",
    )

    assert with_ledger.score > without_ledger.score
    assert with_ledger.asked_node_ids == tuple(
        sorted(node_id for node_id in _graded_ids(problem) if node_id != UNENGAGED)
    )
    assert without_ledger.asked_node_ids is None
    # The exclusion is one-directional: P1.2b only ever drops zero-credit nodes.
    assert with_ledger.topic_credits[UNENGAGED] == 0.0


async def test_unengaged_zero_credit_node_is_reported_unprobed_with_zero_weight() -> None:
    problem = _wide_problem()
    graph = problem.to_kg_graph(attempt_id=-1)
    recorded = json.dumps(_adjudicator_output(problem))
    with patch("apollo.overseer.transcript_coverage._call_adjudication", return_value=recorded):
        from apollo.overseer.transcript_coverage import compute_transcript_coverage_with_spans

        coverage, spans = await compute_transcript_coverage_with_spans(
            transcript=TRANSCRIPT, reference_graph=graph, problem=problem
        )
    result = compute_topic_score(
        coverage=dict(coverage),
        reference_nodes=graph.nodes,
        centrality=compute_centrality(graph),
        evidence_spans=spans,
        asked_node_ids=done._probed_node_ids(_engaged_rows(problem)),
    )
    unengaged = next(topic for topic in result.topics if topic.canonical_key == UNENGAGED)
    assert unengaged.status == "unprobed"
    assert unengaged.weight == 0.0


async def test_ledger_less_fixture_reproduces_pre_fix_arithmetic() -> None:
    """No ``question_opportunities`` key ⇒ ``asked_node_ids=None`` ⇒ old numbers.

    ``compute_topic_score`` documents ``None`` as reproducing the pre-fix grade
    arithmetic exactly, so every pre-P3.2 transcript-grader fixture is untouched
    by this change.
    """
    problem = _wide_problem()
    graph = problem.to_kg_graph(attempt_id=-1)
    adjudicator_output = _adjudicator_output(problem)
    recorded = json.dumps(adjudicator_output)

    replayed = await grade_replay(
        problem=problem,
        transcript=TRANSCRIPT,
        adjudicator_output=adjudicator_output,
        rows=None,
        name="legacy",
    )
    with patch("apollo.overseer.transcript_coverage._call_adjudication", return_value=recorded):
        from apollo.overseer.transcript_coverage import compute_transcript_coverage

        coverage = await compute_transcript_coverage(TRANSCRIPT, graph, problem)
    pre_fix = compute_topic_score(
        coverage=dict(coverage),
        reference_nodes=graph.nodes,
        centrality=compute_centrality(graph),
    )
    assert (replayed.score, replayed.letter) == (pre_fix.score, pre_fix.letter)
    assert replayed.topic_credits == {topic.canonical_key: topic.credit for topic in pre_fix.topics}


async def test_evidence_spans_come_from_the_narrative_gate() -> None:
    """Spans ride through the SAME gate the Done path uses, not a replay copy.

    A verbatim student span survives onto the topic; anything the student never
    typed is dropped by ``narrative_evidence_spans`` before the scorer sees it.
    """
    problem = _wide_problem()
    verbatim = "I taught the four impairments"
    survived = await grade_replay(
        problem=problem,
        transcript=TRANSCRIPT,
        adjudicator_output=_adjudicator_output(problem, span=verbatim),
        rows=None,
        name="span",
    )
    hallucinated = await grade_replay(
        problem=problem,
        transcript=TRANSCRIPT,
        adjudicator_output=_adjudicator_output(problem, span="words the student never typed"),
        rows=None,
        name="nospan",
    )
    # Spans are display-only: they populate TopicCredit.evidence_span and never
    # move the score, so the two runs agree on every number.
    assert survived.score == hallucinated.score
    with patch(
        "campaign.transcript_replay.compute_topic_score", wraps=compute_topic_score
    ) as scorer:
        await grade_replay(
            problem=problem,
            transcript=TRANSCRIPT,
            adjudicator_output=_adjudicator_output(problem, span=verbatim),
            rows=None,
            name="span",
        )
    assert verbatim in scorer.call_args.kwargs["evidence_spans"].values()

    with patch(
        "campaign.transcript_replay.compute_topic_score", wraps=compute_topic_score
    ) as scorer:
        await grade_replay(
            problem=problem,
            transcript=TRANSCRIPT,
            adjudicator_output=_adjudicator_output(problem, span="words the student never typed"),
            rows=None,
            name="nospan",
        )
    assert scorer.call_args.kwargs["evidence_spans"] == {}


# --------------------------------------------------------------------------- #
# Fixture-directory contract (trap 1)                                          #
# --------------------------------------------------------------------------- #


def test_empty_fixture_directory_fails_loudly(tmp_path: Path) -> None:
    """``run()`` used to fold an empty directory into ``passed=False``.

    That reads as "the fixtures regressed" when in fact none were ever exported
    — and ``campaign/fixtures/transcript_grader/`` has been README-only since it
    was created. The harness defect now names itself.
    """
    empty = tmp_path / "transcript_grader"
    empty.mkdir()
    with pytest.raises(SystemExit) as excinfo:
        fixture_paths(empty)
    assert str(empty) in str(excinfo.value)


async def test_run_on_an_empty_directory_raises_instead_of_returning_not_passed(
    tmp_path: Path,
) -> None:
    empty = tmp_path / "nothing"
    empty.mkdir()
    with pytest.raises(SystemExit):
        await run(empty)


def _write_legacy_fixture(directory: Path, *, with_ledger: bool) -> Path:
    problem = _wide_problem()
    payload: dict[str, Any] = {
        "problem": problem.model_dump(mode="json"),
        "transcript": [{"role": role, "content": content} for role, content in TRANSCRIPT],
        "adjudicator_output": _adjudicator_output(problem),
        "gate": {"min_score": 0, "require_validated_spans": False},
    }
    if with_ledger:
        payload["question_opportunities"] = [
            {"reference_node_id": node_id, "state": "understood", "times_asked": 1, "evidence": []}
            for node_id in _graded_ids(problem)
            if node_id != UNENGAGED
        ]
    path = directory / ("with_ledger.json" if with_ledger else "no_ledger.json")
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


async def test_replay_fixture_reads_the_legacy_schema_and_honours_its_ledger(
    tmp_path: Path,
) -> None:
    """The transcript-grader schema is unchanged; ``question_opportunities`` is
    an ADDITIVE key so a fixture set exported before P3.2 keeps working."""
    with_ledger = await replay_fixture(_write_legacy_fixture(tmp_path, with_ledger=True))
    without = await replay_fixture(_write_legacy_fixture(tmp_path, with_ledger=False))
    assert with_ledger.asked_node_ids is not None
    assert without.asked_node_ids is None
    assert with_ledger.score > without.score


async def test_run_reports_every_fixture_and_its_gate(tmp_path: Path) -> None:
    _write_legacy_fixture(tmp_path, with_ledger=True)
    _write_legacy_fixture(tmp_path, with_ledger=False)
    outcomes, passed = await run(tmp_path)
    assert sorted(outcome.name for outcome in outcomes) == ["no_ledger", "with_ledger"]
    assert passed is True
