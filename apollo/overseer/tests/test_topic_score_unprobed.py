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
  worked solution — one node's statement only, and at most
  ``MAX_REFERENCE_TEXT_REVEALS`` statements per attempt.

``asked_node_ids=None`` (the ledger fetch failed / feature not wired) leaves the
grade arithmetic byte-identical to the pre-fix result.
"""

from __future__ import annotations

import dataclasses

import pytest

from apollo.ontology import KGGraph, build_node
from apollo.overseer.topic_score import (
    MAX_REFERENCE_TEXT_REVEALS,
    MIN_GRADED_DENOMINATOR,
    REFERENCE_TEXT_CREDIT_THRESHOLD,
    TopicCredit,
    _reveal_reference_text,
    compute_centrality,
    compute_topic_score,
    graded_topics_only,
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
    """4 graded nodes, 3 probed — comfortably over the floor, so the never-probed
    node leaves the denominator and the probed three renormalize to 1.0."""
    nodes = [_equation(f"eq.{i}") for i in range(3)] + [_equation("eq.never")]
    result = compute_topic_score(
        coverage=_coverage(
            "eq.0",
            "eq.1",
            "eq.2",
            "eq.never",
            scores={"eq.0": 1.0, "eq.1": 1.0, "eq.2": 1.0, "eq.never": 0.0},
        ),
        reference_nodes=nodes,
        centrality=compute_centrality(KGGraph(nodes=nodes)),
        asked_node_ids=frozenset({"eq.0", "eq.1", "eq.2"}),
    )

    by_key = {topic.canonical_key: topic for topic in result.topics}
    assert by_key["eq.never"].weight == 0.0
    assert by_key["eq.never"].status == "unprobed"
    assert sum(topic.weight for topic in result.topics) == pytest.approx(1.0)
    assert result.score == 100
    # Pre-fix the same attempt scored 75 (the never-raised node dragged it down).


def test_unprobed_node_still_appears_in_topics_for_the_artifact():
    nodes = [_equation("eq.0"), _equation("eq.1"), _equation("eq.never")]
    result = compute_topic_score(
        coverage=_coverage("eq.0", "eq.1", "eq.never", scores={"eq.0": 1.0, "eq.1": 1.0}),
        reference_nodes=nodes,
        centrality=compute_centrality(KGGraph(nodes=nodes)),
        asked_node_ids=frozenset({"eq.0", "eq.1"}),
    )

    assert [topic.canonical_key for topic in result.topics] == ["eq.0", "eq.1", "eq.never"]


def test_unprobed_node_the_adjudicator_credited_is_kept_not_confiscated():
    """The exclusion is ASYMMETRIC (review fix): a node with no ledger row is
    dropped only when the adjudicator ALSO found no credit for it. The tally is
    a lossy record of a spontaneously-taught node, so zeroing its weight would
    confiscate work the student demonstrably did. Full contract in
    ``test_topic_score_denominator.py``."""
    nodes = [_equation("eq.0"), _equation("eq.1"), _equation("eq.never")]
    result = compute_topic_score(
        coverage=_coverage(
            "eq.0", "eq.1", "eq.never", scores={"eq.0": 0.0, "eq.1": 0.0, "eq.never": 1.0}
        ),
        reference_nodes=nodes,
        centrality=compute_centrality(KGGraph(nodes=nodes)),
        asked_node_ids=frozenset({"eq.0", "eq.1"}),
    )

    by_key = {topic.canonical_key: topic for topic in result.topics}
    assert by_key["eq.never"].credit == 1.0
    assert by_key["eq.never"].weight > 0.0
    assert by_key["eq.never"].status != "unprobed"
    assert result.score == 33


def test_asked_node_ids_none_leaves_the_grade_arithmetic_unchanged():
    """``None`` == omitted == the pre-fix denominator.

    NOT a claim of byte-identical PAYLOAD: `TopicCredit` gained the additive
    `reference_text`, which is populated from the credit alone and so appears in
    both of these results. What is pinned here is the grade arithmetic — score,
    letter, per-topic weight and status — plus the absence of any `unprobed`
    row, which is what a replay diff against the pre-fix arm must match.
    """
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
    # The additive field IS present on the weak topic — the pre-fix payload had
    # no such key at all, so "byte-identical" is true of the arithmetic only.
    by_key = {topic.canonical_key: topic for topic in baseline.topics}
    assert by_key["eq.two"].reference_text is not None
    assert dataclasses.replace(by_key["eq.two"], reference_text=None).reference_text is None


# --- P1.2b floor: a collapsed denominator is never a free A+ ----------------


def test_single_probed_node_does_not_collapse_the_denominator(caplog):
    """The exploit the floor exists to close.

    A student who explains ONE of five graded nodes and stops (or whose
    auto-done fires before the loop mints any other ledger row) must not be
    graded on that one node alone — that would make bailing out early the
    highest-scoring strategy. The denominator is widened back to the floor
    (three of five), so the attempt scores 33 (D), not 100 (A+).
    """
    nodes = [_equation(f"eq.{i}") for i in range(5)]
    with caplog.at_level("WARNING"):
        result = compute_topic_score(
            coverage=_coverage(
                *[f"eq.{i}" for i in range(5)],
                scores={"eq.0": 1.0, "eq.1": 0.0, "eq.2": 0.0, "eq.3": 0.0, "eq.4": 0.0},
            ),
            reference_nodes=nodes,
            centrality=compute_centrality(KGGraph(nodes=nodes)),
            asked_node_ids=frozenset({"eq.0"}),
        )

    assert result.score == 33
    assert result.letter == "D"
    assert sum(1 for topic in result.topics if topic.status == "unprobed") == 2
    assert "apollo_topic_score_denominator_widened" in caplog.text
    assert "kept=1 required=3" in caplog.text


def test_floor_is_half_the_graded_nodes_rounded_up():
    """3 graded / 2 probed clears `ceil(3/2) == 2`, so P1.2b applies."""
    nodes = [_equation("eq.0"), _equation("eq.1"), _equation("eq.never")]
    result = compute_topic_score(
        coverage=_coverage("eq.0", "eq.1", "eq.never", scores={"eq.0": 1.0, "eq.1": 1.0}),
        reference_nodes=nodes,
        centrality=compute_centrality(KGGraph(nodes=nodes)),
        asked_node_ids=frozenset({"eq.0", "eq.1"}),
    )

    assert result.score == 100
    assert {t.canonical_key for t in result.topics if t.status == "unprobed"} == {"eq.never"}


def test_floor_never_drops_below_two_graded_nodes():
    """2 graded / 1 probed fails the absolute floor even though it is half the
    nodes: a single node is too thin a denominator to carry a whole grade, so
    the never-probed one is widened back in."""
    nodes = [_equation("eq.0"), _equation("eq.never")]
    result = compute_topic_score(
        coverage=_coverage("eq.0", "eq.never", scores={"eq.0": 1.0}),
        reference_nodes=nodes,
        centrality=compute_centrality(KGGraph(nodes=nodes)),
        asked_node_ids=frozenset({"eq.0"}),
    )

    assert MIN_GRADED_DENOMINATOR == 2
    assert result.score == 50
    assert all(topic.status != "unprobed" for topic in result.topics)


def test_a_one_graded_node_problem_is_never_blocked_by_the_absolute_floor():
    """The floor is capped by the graded count, so a 1-node rubric whose single
    node WAS probed still passes (the filter is a no-op there anyway)."""
    nodes = [_equation("eq.only")]
    result = compute_topic_score(
        coverage=_coverage("eq.only", scores={"eq.only": 1.0}),
        reference_nodes=nodes,
        centrality=compute_centrality(KGGraph(nodes=nodes)),
        asked_node_ids=frozenset({"eq.only"}),
    )

    assert result.score == 100
    assert result.topics[0].weight == pytest.approx(1.0)
    assert result.topics[0].status != "unprobed"


def test_no_graded_node_probed_falls_back_to_grading_all_of_them(caplog):
    """Degenerate safety valve: if the ledger names none of the graded nodes,
    excluding every uncredited one would leave a sub-floor denominator. The
    widening restores it and logs — the safety net must never make a Done
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
    assert "apollo_topic_score_denominator_widened" in caplog.text
    assert "kept=1 required=2" in caplog.text


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


def test_unprobed_topic_does_not_expose_its_reference_statement():
    """An `unprobed` topic is explicitly "not part of this grade", so revealing
    its answer is pure leakage with no diagnostic value — and it would widen the
    D2 reveal beyond the nodes the student was actually assessed on."""
    nodes = [
        _equation("eq.0"),
        _equation("eq.1"),
        _procedure("p.never", action="Balance the two sides"),
    ]
    result = compute_topic_score(
        coverage=_coverage("eq.0", "eq.1", "p.never", scores={"eq.0": 1.0, "eq.1": 1.0}),
        reference_nodes=nodes,
        centrality=compute_centrality(KGGraph(nodes=nodes)),
        asked_node_ids=frozenset({"eq.0", "eq.1"}),
    )

    by_key = {topic.canonical_key: topic for topic in result.topics}
    assert by_key["p.never"].status == "unprobed"
    assert by_key["p.never"].credit == 0.0
    assert by_key["p.never"].reference_text is None


def test_reveal_is_capped_so_the_union_is_never_the_worked_solution():
    """On a wholly-failed attempt EVERY graded topic is below the threshold, so
    an uncapped per-topic reveal would hand back the entire graded reference
    solution — convertible into a grade via restart (best-grade-wins). At most
    `MAX_REFERENCE_TEXT_REVEALS` statements ship, chosen lowest-credit first."""
    nodes = [_equation(f"eq.{i}", label=f"L{i}") for i in range(5)]
    result = compute_topic_score(
        coverage=_coverage(
            *[f"eq.{i}" for i in range(5)],
            scores={"eq.0": 0.0, "eq.1": 0.1, "eq.2": 0.2, "eq.3": 0.3, "eq.4": 0.4},
        ),
        reference_nodes=nodes,
        centrality=compute_centrality(KGGraph(nodes=nodes)),
    )

    revealed = [t.canonical_key for t in result.topics if t.reference_text is not None]
    assert MAX_REFERENCE_TEXT_REVEALS == 2
    assert len(revealed) == MAX_REFERENCE_TEXT_REVEALS
    assert revealed == ["eq.0", "eq.1"]  # lowest credit first, reference order on ties


def test_reveal_skips_nodes_that_render_no_statement_and_takes_the_next():
    """The cap counts STATEMENTS, not candidates: a prose-less node must not
    consume one of the two slots.

    Every graded content model requires at least one non-empty prose field, so a
    statement-less node is unreachable through `build_node` — the helper is
    exercised directly with a stub to pin the defensive branch.
    """

    class _Blank:
        class content:  # noqa: N801 - test stub
            pass

    def _topic(key: str, credit: float) -> TopicCredit:
        return TopicCredit(
            canonical_key=key,
            display_name=key,
            credit=credit,
            status="missing",
            weight=0.5,
            misconceptions=(),
        )

    topics = [_topic("eq.blank", 0.0), _topic("eq.a", 0.1), _topic("eq.b", 0.2)]
    nodes_by_id = {
        "eq.blank": _Blank(),
        "eq.a": _equation("eq.a", label="A"),
        "eq.b": _equation("eq.b", label="B"),
    }

    revealed = [
        topic.canonical_key
        for topic in _reveal_reference_text(topics, nodes_by_id)
        if topic.reference_text is not None
    ]
    assert revealed == ["eq.a", "eq.b"]


# --- graded_topics_only: the narrative/feedback view ------------------------


def test_graded_topics_only_drops_unprobed_rows():
    """P2.1 consistency: the narrator and the structured feedback must never be
    handed a topic the payload simultaneously labels "not part of this grade"."""
    nodes = [_equation("eq.0"), _equation("eq.1"), _equation("eq.never")]
    result = compute_topic_score(
        coverage=_coverage("eq.0", "eq.1", "eq.never", scores={"eq.0": 1.0, "eq.1": 1.0}),
        reference_nodes=nodes,
        centrality=compute_centrality(KGGraph(nodes=nodes)),
        asked_node_ids=frozenset({"eq.0", "eq.1"}),
    )

    narrated = graded_topics_only(result)
    assert [t.canonical_key for t in narrated.topics] == ["eq.0", "eq.1"]
    # Score/letter are the already-computed grade — never recomputed here.
    assert (narrated.score, narrated.letter) == (result.score, result.letter)
    assert result.topics[2].status == "unprobed"  # the input is not mutated


def test_graded_topics_only_is_identity_without_unprobed_rows():
    nodes = [_equation("eq.0"), _equation("eq.1")]
    result = compute_topic_score(
        coverage=_coverage("eq.0", "eq.1", scores={"eq.0": 1.0, "eq.1": 0.0}),
        reference_nodes=nodes,
        centrality=compute_centrality(KGGraph(nodes=nodes)),
    )

    assert graded_topics_only(result) == result


def test_graded_topics_only_tolerates_a_none_result():
    assert graded_topics_only(None) is None


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
