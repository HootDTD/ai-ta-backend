"""Every `node_ledger` row records the basis the adjudicator declared.

`basis ∈ {stated, used, implied, absent}` is the model's own answer to "why does
this credit exist" — and until 2026-08-08 it only ever reached a log line, which
is why the 2026-08-08 replay could not size the `absent | 0.6` cell per attempt.
The ledger is the permanent per-node record, so that is where it belongs.

It goes in its OWN key, not into `method`: `method` is the graph lane's resolver
vocabulary (`exact` / `fuzzy` / `semantic` / `nli` / `clarification`, per
migration 034's row comment) and conflating two enums in one column would make
both unreadable. `basis` is additive and absent-safe — an artifact written before
today, or by the dormant graph lane, simply has `None`.
"""

from __future__ import annotations

import pytest

from apollo.grading.artifact_build import LEDGER_STATUS_UNPROBED, build_llm_artifact
from apollo.overseer.topic_score import TopicCredit, TopicScoreResult

pytestmark = pytest.mark.unit


def _rubric() -> dict:
    return {"overall": {"score": 60.0}}


def _coverage(**extra) -> dict:
    base = {
        "per_step": {"p1": "covered", "p2": "missing"},
        "procedure_scores": {"p1": 1.0, "p2": 0.6},
        "confidences": {"p1": 0.9, "p2": 0.7},
        "negotiation_counts": {"dual": 0, "disputed": 0, "paraphrased": 0, "skipped": 0},
    }
    base.update(extra)
    return base


def _by_key(artifact: dict) -> dict[str, dict]:
    return {row["canonical_key"]: row for row in artifact["node_ledger"]}


def _build(coverage: dict, topic_score=None) -> dict:
    return build_llm_artifact(
        coverage=coverage,
        rubric=_rubric(),
        latency_ms=12,
        clarification_trace=[],
        topic_score=topic_score,
    )


def test_credited_and_unresolved_rows_both_carry_their_basis():
    artifact = _build(_coverage(basis={"p1": "stated", "p2": "absent"}))
    rows = _by_key(artifact)
    assert rows["p1"]["status"] == "credited"
    assert rows["p1"]["basis"] == "stated"
    assert rows["p2"]["status"] == "unresolved"
    assert rows["p2"]["basis"] == "absent"


def test_the_absent_yet_credited_cell_is_legible_from_the_artifact_alone():
    """The whole point: a row can now be read as "the model said there was no
    evidence AND gave it 0.6" without correlating a log stream."""
    coverage = _coverage(
        per_step={"p1": "missing"},
        procedure_scores={"p1": 0.6},
        confidences={"p1": 0.7},
        basis={"p1": "absent"},
    )
    row = _by_key(_build(coverage))["p1"]
    assert (row["basis"], row["status"]) == ("absent", "unresolved")


def _topic(key: str, status: str) -> TopicCredit:
    return TopicCredit(
        canonical_key=key,
        display_name=key,
        credit=1.0,
        status=status,  # type: ignore[arg-type]
        weight=1.0,
        misconceptions=(),
    )


def test_unprobed_rows_carry_basis_too():
    """A P1.2b-excluded node still got a verdict, so it still has a basis — the
    exclusion is about the denominator, not about the evidence."""
    topic_score = TopicScoreResult(
        score=60,
        letter="C",
        coverage_component=0.6,
        misconception_dock=0.0,
        topics=(_topic("p2", "unprobed"), _topic("p1", "graded")),
    )
    artifact = build_llm_artifact(
        coverage=_coverage(basis={"p1": "stated", "p2": "implied"}),
        rubric=_rubric(),
        latency_ms=12,
        clarification_trace=[],
        topic_score=topic_score,
    )
    rows = _by_key(artifact)
    assert rows["p2"]["status"] == LEDGER_STATUS_UNPROBED
    assert rows["p2"]["basis"] == "implied"


def test_basis_does_not_squat_on_method():
    """`method` is the graph lane's resolver vocabulary. Two enums in one field
    would make both unreadable, so it stays None on this path."""
    rows = _by_key(_build(_coverage(basis={"p1": "stated", "p2": "absent"})))
    assert [row["method"] for row in rows.values()] == [None, None]


def test_a_coverage_verdict_without_basis_still_builds_the_old_shape():
    """Absent-safe both ways: the dormant graph lane emits no `basis`, and the
    row must still be built (with None) rather than raising or dropping keys."""
    artifact = _build(_coverage())
    rows = _by_key(artifact)
    assert rows["p1"]["basis"] is None and rows["p2"]["basis"] is None
    assert set(rows["p1"]) == {
        "canonical_key",
        "status",
        "method",
        "basis",
        "confidence",
        "evidence_span",
    }


def test_a_node_missing_from_the_basis_map_is_none_not_a_crash():
    """Partial maps happen (an omitted verdict, an older mixed payload). Read it
    defensively — a ledger row is a record, and a missing basis is not a reason
    to fail a write."""
    rows = _by_key(_build(_coverage(basis={"p1": "used"})))
    assert rows["p1"]["basis"] == "used"
    assert rows["p2"]["basis"] is None


def test_basis_does_not_disturb_the_rest_of_the_payload():
    """Additive means additive: adding the key changes nothing else about the
    artifact, so a consumer that ignores it sees the pre-2026-08-08 payload."""
    with_basis = _build(_coverage(basis={"p1": "stated", "p2": "absent"}))
    without = _build(_coverage())
    for row in with_basis["node_ledger"]:
        row.pop("basis")
    for row in without["node_ledger"]:
        row.pop("basis")
    assert with_basis == without
