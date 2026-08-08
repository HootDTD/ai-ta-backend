"""P1.2b (never-asked nodes leave the denominator) + the post-grade
``reference_text`` contract (P2.3 / decision D2), both in
``apollo/overseer/topic_score.py``.

2026-08-07 bimodal-fix:

* **P1.2b** — 31% of graded nodes in the pilot had no ``QuestionOpportunity``
  row at all (never asked AND never tally-updated), and those scored ``missing``
  85% of the time. Grading a student on a topic the tutor never raised is the
  single largest contributor to the F pile after the empty-attempt rows. A
  graded node with no ledger row is now weight 0 (out of the denominator) and
  is reported with status ``unprobed`` so the artifact/UI can say "not part of
  this grade" instead of "you missed this".
* **reference_text** — post-grade closure (D2): a topic scoring below 0.6
  carries its reference statement so the UI can render "what full credit looks
  like". Never emitted for a topic that already earned ≥ 0.6, and never a full
  worked solution — one node's statement only.

``asked_node_ids=None`` (the ledger fetch failed / feature not wired) must be
byte-identical to the pre-fix result.
"""

from __future__ import annotations

import pytest

from apollo.ontology import KGGraph, build_node
from apollo.overseer.topic_score import (
    REFERENCE_TEXT_CREDIT_THRESHOLD,
    compute_centrality,
    compute_topic_score,
    reference_statement_for,
)

pytestmark = pytest.mark.unit


def _equation(node_id: str, label: str = "", symbolic: str = "x = y"):
    return build_node(
        node_type="equation",
        node_id=node_id,
        attempt_id=1,
        source="reference",
        content={"symbolic": symbolic, "label": label},
    )


def _procedure(node_id: str, action: str = "do the thing", purpose: str = ""):
    return build_node(
        node_type="procedure_step",
        node_id=node_id,
        attempt_id=1,
        source="reference",
        content={"action": action, "purpose": purpose},
    )


def _condition(node_id: str, applies_when: str = "the flow is steady", label: str = ""):
    return build_node(
        node_type="condition",
        node_id=node_id,
        attempt_id=1,
        source="reference",
        content={"applies_when": applies_when, "label": label},
    )


def _simplification(node_id: str):
    return build_node(
        node_type="simplification",
        node_id=node_id,
        attempt_id=1,
        source="reference",
        content={"applies_when": "friction is negligible", "transformation": "drop the loss term"},
    )


def _definition(node_id: str):
    return build_node(
        node_type="definition",
        node_id=node_id,
        attempt_id=1,
        source="reference",
        content={"concept": "density", "meaning": "mass per unit volume"},
    )


def _coverage(*node_ids: str, scores: dict[str, float] | None = None) -> dict:
    """Every listed node is adjudicated (present in ``per_step``) so the P0.5
    abstain filter is a no-op and these tests isolate P1.2b."""
    per_step = {node_id: "missing" for node_id in node_ids}
    return {"per_step": per_step, "procedure_scores": dict(scores or {})}


# --- P1.2b: never-probed graded nodes leave the denominator -----------------


def test_never_probed_node_is_weight_zero_and_marked_unprobed():
    asked, never_asked = _equation("eq.asked"), _equation("eq.never")
    nodes = [asked, never_asked]

    result = compute_topic_score(
        coverage=_coverage("eq.asked", "eq.never", scores={"eq.asked": 1.0, "eq.never": 0.0}),
        reference_nodes=nodes,
        centrality=compute_centrality(KGGraph(nodes=nodes)),
        asked_node_ids=frozenset({"eq.asked"}),
    )

    by_key = {topic.canonical_key: topic for topic in result.topics}
    assert by_key["eq.never"].weight == 0.0
    assert by_key["eq.never"].status == "unprobed"
    # The probed node now carries the WHOLE denominator: 1.0 credit -> 100.
    assert by_key["eq.asked"].weight == pytest.approx(1.0)
    assert result.score == 100
    assert result.letter == "A+"


def test_unprobed_node_still_appears_in_topics_for_the_artifact():
    nodes = [_equation("eq.asked"), _equation("eq.never")]
    result = compute_topic_score(
        coverage=_coverage("eq.asked", "eq.never", scores={"eq.asked": 1.0}),
        reference_nodes=nodes,
        centrality=compute_centrality(KGGraph(nodes=nodes)),
        asked_node_ids=frozenset({"eq.asked"}),
    )

    assert [topic.canonical_key for topic in result.topics] == ["eq.asked", "eq.never"]


def test_unprobed_node_never_contributes_credit_even_when_adjudicated_positive():
    """A never-asked node cannot raise the grade either — the denominator is
    the probed set, so its credit is reported but unweighted."""
    nodes = [_equation("eq.asked"), _equation("eq.never")]
    result = compute_topic_score(
        coverage=_coverage("eq.asked", "eq.never", scores={"eq.asked": 0.0, "eq.never": 1.0}),
        reference_nodes=nodes,
        centrality=compute_centrality(KGGraph(nodes=nodes)),
        asked_node_ids=frozenset({"eq.asked"}),
    )

    by_key = {topic.canonical_key: topic for topic in result.topics}
    assert by_key["eq.never"].credit == 1.0
    assert by_key["eq.never"].weight == 0.0
    assert result.score == 0


def test_asked_node_ids_none_is_byte_identical_to_the_pre_fix_result():
    nodes = [_equation("eq.one"), _equation("eq.two")]
    coverage = _coverage("eq.one", "eq.two", scores={"eq.one": 1.0, "eq.two": 0.0})
    centrality = compute_centrality(KGGraph(nodes=nodes))

    baseline = compute_topic_score(coverage=coverage, reference_nodes=nodes, centrality=centrality)
    explicit_none = compute_topic_score(
        coverage=coverage,
        reference_nodes=nodes,
        centrality=centrality,
        asked_node_ids=None,
    )

    assert explicit_none == baseline
    assert baseline.score == 50
    assert all(topic.status != "unprobed" for topic in baseline.topics)


def test_no_graded_node_probed_falls_back_to_grading_all_of_them(caplog):
    """Degenerate safety valve: if the ledger names none of the graded nodes,
    excluding them all would leave an empty denominator. Grade the adjudicated
    set exactly as before and log — the safety net must never make a Done
    ungradeable."""
    nodes = [_equation("eq.one"), _equation("eq.two")]
    with caplog.at_level("WARNING"):
        result = compute_topic_score(
            coverage=_coverage("eq.one", "eq.two", scores={"eq.one": 1.0, "eq.two": 0.0}),
            reference_nodes=nodes,
            centrality=compute_centrality(KGGraph(nodes=nodes)),
            asked_node_ids=frozenset({"def.unrelated"}),
        )

    assert result.score == 50
    assert all(topic.status != "unprobed" for topic in result.topics)
    assert "apollo_topic_score_no_probed_graded_node" in caplog.text


def test_ungraded_types_are_untouched_by_the_probe_filter():
    """`definition` nodes never entered the denominator to begin with, so the
    probe filter must not resurrect them as `unprobed` rows."""
    nodes = [_equation("eq.asked"), _definition("def.x")]
    result = compute_topic_score(
        coverage=_coverage("eq.asked", scores={"eq.asked": 1.0}),
        reference_nodes=nodes,
        centrality=compute_centrality(KGGraph(nodes=nodes)),
        asked_node_ids=frozenset({"eq.asked"}),
    )

    assert [topic.canonical_key for topic in result.topics] == ["eq.asked"]


def test_unadjudicated_node_is_dropped_before_the_probe_filter_runs():
    """P0.5 (abstain-not-zero) still wins: a node the adjudicator omitted is
    absent from topics[] entirely, not reported as `unprobed`."""
    nodes = [_equation("eq.asked"), _equation("eq.omitted")]
    result = compute_topic_score(
        coverage=_coverage("eq.asked", scores={"eq.asked": 1.0}),
        reference_nodes=nodes,
        centrality=compute_centrality(KGGraph(nodes=nodes)),
        asked_node_ids=frozenset({"eq.asked", "eq.omitted"}),
    )

    assert [topic.canonical_key for topic in result.topics] == ["eq.asked"]


# --- reference_text (D2 post-grade closure) --------------------------------


def test_reference_text_present_only_below_the_credit_threshold():
    strong, weak = _equation("eq.strong", label="Bernoulli"), _equation("eq.weak", label="Momentum")
    nodes = [strong, weak]
    result = compute_topic_score(
        coverage=_coverage("eq.strong", "eq.weak", scores={"eq.strong": 1.0, "eq.weak": 0.0}),
        reference_nodes=nodes,
        centrality=compute_centrality(KGGraph(nodes=nodes)),
    )

    by_key = {topic.canonical_key: topic for topic in result.topics}
    assert by_key["eq.strong"].reference_text is None
    assert by_key["eq.weak"].reference_text == "Momentum — x = y"


def test_reference_text_boundary_is_exclusive_at_the_threshold():
    at_threshold = _equation("eq.at", label="At")
    below = _equation("eq.below", label="Below")
    nodes = [at_threshold, below]
    result = compute_topic_score(
        coverage=_coverage(
            "eq.at",
            "eq.below",
            scores={
                "eq.at": REFERENCE_TEXT_CREDIT_THRESHOLD,
                "eq.below": REFERENCE_TEXT_CREDIT_THRESHOLD - 0.01,
            },
        ),
        reference_nodes=nodes,
        centrality=compute_centrality(KGGraph(nodes=nodes)),
    )

    by_key = {topic.canonical_key: topic for topic in result.topics}
    assert by_key["eq.at"].reference_text is None
    assert by_key["eq.below"].reference_text is not None


def test_unprobed_topic_also_exposes_its_reference_statement():
    nodes = [_equation("eq.asked"), _procedure("p.never", action="Balance the two sides")]
    result = compute_topic_score(
        coverage=_coverage("eq.asked", "p.never", scores={"eq.asked": 1.0}),
        reference_nodes=nodes,
        centrality=compute_centrality(KGGraph(nodes=nodes)),
        asked_node_ids=frozenset({"eq.asked"}),
    )

    by_key = {topic.canonical_key: topic for topic in result.topics}
    assert by_key["p.never"].status == "unprobed"
    assert by_key["p.never"].reference_text == "Balance the two sides"


@pytest.mark.parametrize(
    ("node", "expected"),
    [
        (_equation("eq.a", label="Bernoulli", symbolic="p + q = c"), "Bernoulli — p + q = c"),
        (_equation("eq.b", label="", symbolic="p + q = c"), "p + q = c"),
        (_condition("c.a", applies_when="the flow is steady"), "the flow is steady"),
        (_condition("c.b", applies_when="steady flow", label="Steady"), "Steady — steady flow"),
        (_simplification("s.a"), "friction is negligible — drop the loss term"),
        (
            _procedure("p.a", action="Solve for v", purpose="isolate the unknown"),
            "Solve for v — isolate the unknown",
        ),
        (_definition("d.a"), "density — mass per unit volume"),
    ],
)
def test_reference_statement_renders_every_node_type(node, expected: str):
    assert reference_statement_for(node) == expected


def test_reference_statement_is_none_when_the_node_carries_no_renderable_text():
    class _Blank:
        node_id = "x"

        class content:  # noqa: N801 - test stub
            pass

    assert reference_statement_for(_Blank()) is None
