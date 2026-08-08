"""Review fixes for the P1.2b denominator (``apollo/overseer/topic_score.py``).

Two defects found in the first P1.2b build, both about WHICH graded nodes the
grade is divided by:

* **Symmetric exclusion confiscated demonstrated work.** The first build zeroed
  the weight of every graded node without a ``QuestionOpportunity`` row
  regardless of the credit the adjudicator awarded it. The adjudicator reads the
  transcript, not the ledger, so a node the student taught spontaneously — one
  the tally simply failed to record (in the pilot ~15% of ledger-less graded
  nodes were NOT ``missing``) — silently lost its whole contribution to the
  numerator, and ``graded_topics_only`` then hid it from the narrative and from
  remediation too. The exclusion is now ASYMMETRIC: a ledger-less node is
  dropped only when the adjudicator ALSO found no credit for it.
* **The floor was an on/off gate that fired hardest on the target population.**
  Below the floor the first build restored the FULL adjudicated denominator, so
  the budget-starved sessions P1.2b exists for got no relief at all. The floor
  is now a MINIMUM WIDTH: the denominator is widened back only to the floor,
  highest-credit dropped node first.

Together these keep the anti-gaming property the spec deviation was introduced
for — "answer one of five graded nodes and click Done" can never renormalize
that node into an A+ — without paying for it with confiscated credit.
"""

from __future__ import annotations

import pytest

from apollo.ontology import KGGraph, build_node
from apollo.overseer.topic_score import (
    MIN_GRADED_DENOMINATOR,
    UNPROBED_CREDIT_KEEP_THRESHOLD,
    _weights_for,
    compute_centrality,
    compute_topic_score,
    graded_topics_only,
)

pytestmark = pytest.mark.unit


def _equation(node_id: str, label: str = ""):
    return build_node(
        node_type="equation",
        node_id=node_id,
        attempt_id=1,
        source="reference",
        content={"symbolic": "x = y", "label": label},
    )


def _score(
    *,
    credits: dict[str, float],
    asked: frozenset[str] | None,
):
    """Grade a flat all-equation reference graph (equal centrality) so the
    arithmetic below is exactly ``mean(credit over the denominator)``."""
    nodes = [_equation(node_id) for node_id in credits]
    return compute_topic_score(
        coverage={
            "per_step": {node_id: "missing" for node_id in credits},
            "procedure_scores": dict(credits),
        },
        reference_nodes=nodes,
        centrality=compute_centrality(KGGraph(nodes=nodes)),
        asked_node_ids=asked,
    )


# --- asymmetric exclusion: never confiscate credit the adjudicator awarded ---


def test_credited_node_without_a_ledger_row_keeps_its_full_weight():
    """The confiscation defect.

    G1/G2 were probed and scored 0.6; G3/G4 have no ledger row (the student
    taught them spontaneously and the tally never recorded it) and the
    adjudicator scored both 1.0. The first build shrank the denominator to
    {G1, G2} and served 60 (C) for work that averages 80 (B+).
    """
    result = _score(
        credits={"eq.1": 0.6, "eq.2": 0.6, "eq.3": 1.0, "eq.4": 1.0},
        asked=frozenset({"eq.1", "eq.2"}),
    )

    assert result.score == 80
    assert all(topic.status != "unprobed" for topic in result.topics)
    assert all(topic.weight > 0.0 for topic in result.topics)


def test_uncredited_node_without_a_ledger_row_still_leaves_the_denominator():
    """The asymmetry cuts one way only — the original P1.2b relief is intact."""
    result = _score(
        credits={"eq.1": 1.0, "eq.2": 1.0, "eq.3": 0.0, "eq.4": 0.0},
        asked=frozenset({"eq.1", "eq.2"}),
    )

    by_key = {topic.canonical_key: topic for topic in result.topics}
    assert result.score == 100
    assert by_key["eq.3"].status == "unprobed" and by_key["eq.3"].weight == 0.0
    assert by_key["eq.4"].status == "unprobed" and by_key["eq.4"].weight == 0.0


def test_keep_threshold_is_inclusive_at_the_adjudication_anchor():
    """``UNPROBED_CREDIT_KEEP_THRESHOLD`` is the lowest P1.1 anchor that means
    "the student landed this" — at it the node is kept, a hair below it is not."""
    result = _score(
        credits={
            "eq.1": 1.0,
            "eq.2": 1.0,
            "eq.at": UNPROBED_CREDIT_KEEP_THRESHOLD,
            "eq.below": UNPROBED_CREDIT_KEEP_THRESHOLD - 0.01,
        },
        asked=frozenset({"eq.1", "eq.2"}),
    )

    by_key = {topic.canonical_key: topic for topic in result.topics}
    assert by_key["eq.at"].status != "unprobed"
    assert by_key["eq.below"].status == "unprobed"


def test_a_credited_unprobed_topic_reaches_the_narrative():
    """``graded_topics_only`` strips exactly the excluded topics, so keeping a
    demonstrated node in the grade also puts it back in the narrative and in
    remediation — the first build dropped it from all three at once."""
    result = _score(
        credits={"eq.1": 0.0, "eq.2": 0.0, "eq.taught": 1.0},
        asked=frozenset({"eq.1", "eq.2"}),
    )

    assert [topic.canonical_key for topic in graded_topics_only(result).topics] == [
        "eq.1",
        "eq.2",
        "eq.taught",
    ]


# --- the floor is a minimum WIDTH, not an on/off gate ------------------------


def test_below_the_floor_the_denominator_is_widened_not_fully_restored(caplog):
    """The early-Done cohort the spec named as P1.2b's target.

    Five graded nodes, Apollo asked about two, the student answered both fully.
    The first build restored all five and served 40 (D). The floor is
    ``ceil(5/2) == 3``, so the denominator is widened to three — not five — and
    the student is graded 67 for two-of-three, still nowhere near the A+ that a
    one-node denominator would produce.
    """
    with caplog.at_level("WARNING"):
        result = _score(
            credits={"eq.1": 1.0, "eq.2": 1.0, "eq.3": 0.0, "eq.4": 0.0, "eq.5": 0.0},
            asked=frozenset({"eq.1", "eq.2"}),
        )

    assert result.score == 67
    assert sum(1 for topic in result.topics if topic.status == "unprobed") == 2
    assert "apollo_topic_score_denominator_widened" in caplog.text
    assert "kept=2 required=3" in caplog.text


def test_one_probed_node_can_never_renormalize_into_an_a_plus():
    """The exploit the floor exists to close, re-pinned under the new shape:
    one probed node out of five is still divided by the floor, not by one."""
    result = _score(
        credits={"eq.1": 1.0, "eq.2": 0.0, "eq.3": 0.0, "eq.4": 0.0, "eq.5": 0.0},
        asked=frozenset({"eq.1"}),
    )

    assert result.score == 33
    assert result.letter == "D"


def test_widening_takes_the_highest_credit_dropped_node_first():
    """Widening the denominator is OUR policy choice, not the student's failure,
    so it costs them as little as possible: the dropped node with the most
    credit comes back first (ties broken in reference order, so the grade is
    reproducible)."""
    result = _score(
        credits={"eq.1": 1.0, "eq.2": 0.3, "eq.3": 0.0, "eq.4": 0.0},
        asked=frozenset({"eq.1"}),
    )

    by_key = {topic.canonical_key: topic for topic in result.topics}
    assert MIN_GRADED_DENOMINATOR == 2
    assert by_key["eq.2"].status != "unprobed"  # widened back in
    assert by_key["eq.3"].status == "unprobed"
    assert by_key["eq.4"].status == "unprobed"
    assert result.score == 65  # (1.0 + 0.3) / 2


def test_widening_never_exceeds_the_graded_node_count():
    """A 1-node rubric has an unreachable floor by construction; capping it at
    the graded count keeps the filter a no-op rather than a hard error."""
    result = _score(credits={"eq.only": 1.0}, asked=frozenset({"eq.only"}))

    assert result.score == 100
    assert result.topics[0].weight == pytest.approx(1.0)


def test_weights_of_an_empty_denominator_are_empty():
    """`_denominator` can no longer return an empty list (the floor is >= 1 and
    `graded_nodes` is non-empty by this point), so this defensive branch is
    unreachable through `compute_topic_score` and is pinned directly."""
    assert _weights_for([], {"eq.1": 1.0}) == {}


def test_an_empty_ledger_still_grades_the_credited_nodes(caplog):
    """Ledger read returned rows for nothing: the credited nodes still carry the
    grade (they are kept by credit, not by probe), and the floor widens the rest
    back in — the safety net can never make a Done ungradeable."""
    with caplog.at_level("WARNING"):
        result = _score(credits={"eq.1": 1.0, "eq.2": 0.0}, asked=frozenset())

    assert result.score == 50
    assert all(topic.status != "unprobed" for topic in result.topics)
