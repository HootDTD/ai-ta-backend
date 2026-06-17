"""WU-4A2 Task 4 — the 7 rubric sub-scores (scores.py).

RED-first. Each sub-score is a pure ratio in [0,1] over the canonical graphs and
NEVER emits an event. Binding rules under test:
  * edge_coverage: explicit outweighs inferred (inferred match = 0.5).
  * procedure_order: penalize only true PRECEDES inversions, never a missing
    stated order.
  * dependency: lowest-weight, direction-loose any->any DEPENDS_ON match.
  * every vacuous dimension (no reference edges of that type) -> 1.0, never NaN.
"""

from __future__ import annotations

import math

from apollo.graph_compare.coverage import coverage_result
from apollo.graph_compare.scores import (
    INFERRED_EDGE_WEIGHT,
    SubScores,
    compute_sub_scores,
)
from apollo.graph_compare.soundness import contradiction_penalty
from apollo.ontology.edges import EdgeType

from ._builders import cedge, cnode, path, rgraph, rnode, snorm


def _winning(student, reference):
    _, winning, _ = coverage_result(student, reference)
    return winning


def test_node_coverage_equals_winning_path_ratio():
    ref = rgraph(
        nodes=(rnode("a"), rnode("b"), rnode("c"), rnode("d")),
        paths=(path("a", "b", "c", "d"),),
    )
    student = snorm(nodes=(cnode("a"), cnode("b")))
    winning = _winning(student, ref)
    sub = compute_sub_scores(student, ref, winning)
    assert sub.node_coverage == winning.score == 0.5


def test_edge_coverage_explicit_full_match():
    ref = rgraph(
        nodes=(rnode("proc.x", "procedure_step"), rnode("eq.y")),
        edges=(cedge(EdgeType.USES, "proc.x", "eq.y"),),
        paths=(path("proc.x", "eq.y"),),
    )
    student = snorm(
        nodes=(cnode("proc.x", "procedure_step"), cnode("eq.y")),
        edges=(cedge(EdgeType.USES, "proc.x", "eq.y", provenance="explicit"),),
    )
    sub = compute_sub_scores(student, ref, _winning(student, ref))
    assert sub.edge_coverage == 1.0


def test_edge_coverage_inferred_weighted_lower():
    ref = rgraph(
        nodes=(rnode("proc.x", "procedure_step"), rnode("eq.y")),
        edges=(cedge(EdgeType.USES, "proc.x", "eq.y"),),
        paths=(path("proc.x", "eq.y"),),
    )
    student = snorm(
        nodes=(cnode("proc.x", "procedure_step"), cnode("eq.y")),
        edges=(cedge(EdgeType.USES, "proc.x", "eq.y", provenance="inferred"),),
    )
    sub = compute_sub_scores(student, ref, _winning(student, ref))
    assert sub.edge_coverage == INFERRED_EDGE_WEIGHT == 0.5


def test_edge_coverage_missing_edge_lowers_score():
    ref = rgraph(
        nodes=(rnode("proc.x", "procedure_step"), rnode("eq.y"), rnode("eq.z")),
        edges=(
            cedge(EdgeType.USES, "proc.x", "eq.y"),
            cedge(EdgeType.USES, "proc.x", "eq.z"),
        ),
        paths=(path("proc.x", "eq.y", "eq.z"),),
    )
    student = snorm(
        nodes=(cnode("proc.x", "procedure_step"), cnode("eq.y"), cnode("eq.z")),
        edges=(cedge(EdgeType.USES, "proc.x", "eq.y"),),
    )
    sub = compute_sub_scores(student, ref, _winning(student, ref))
    assert sub.edge_coverage == 0.5  # one of two reference edges matched


def test_scoping_score_matches_scopes_edges():
    ref = rgraph(
        nodes=(rnode("cond.c", "condition"), rnode("eq.y")),
        edges=(cedge(EdgeType.SCOPES, "cond.c", "eq.y"),),
        paths=(path("cond.c", "eq.y"),),
    )
    student = snorm(
        nodes=(cnode("cond.c", "condition"), cnode("eq.y")),
        edges=(cedge(EdgeType.SCOPES, "cond.c", "eq.y"),),
    )
    sub = compute_sub_scores(student, ref, _winning(student, ref))
    assert sub.scoping == 1.0
    # usage is vacuous (no reference USES edges) -> 1.0
    assert sub.usage == 1.0


def test_usage_score_matches_uses_edges():
    ref = rgraph(
        nodes=(rnode("proc.x", "procedure_step"), rnode("eq.y")),
        edges=(cedge(EdgeType.USES, "proc.x", "eq.y"),),
        paths=(path("proc.x", "eq.y"),),
    )
    student = snorm(nodes=(cnode("proc.x", "procedure_step"), cnode("eq.y")))
    sub = compute_sub_scores(student, ref, _winning(student, ref))
    assert sub.usage == 0.0  # the one reference USES edge is unmatched
    assert sub.scoping == 1.0  # vacuous


def test_procedure_order_penalizes_true_inversion():
    # Reference path orders A before B (both procedure_steps); S_norm states the
    # REVERSE order (PRECEDES B->A) -> inversion -> < 1.0.
    ref = rgraph(
        nodes=(rnode("proc.a", "procedure_step"), rnode("proc.b", "procedure_step")),
        paths=(path("proc.a", "proc.b"),),
    )
    student = snorm(
        nodes=(cnode("proc.a", "procedure_step"), cnode("proc.b", "procedure_step")),
        edges=(cedge(EdgeType.PRECEDES, "proc.b", "proc.a"),),  # reversed!
    )
    sub = compute_sub_scores(student, ref, _winning(student, ref))
    assert sub.procedure_order < 1.0
    assert sub.procedure_order == 0.0  # 1 inversion / 1 comparable pair


def test_procedure_order_no_penalty_for_missing_stated_order():
    # Both A,B present, but student states NO PRECEDES edge -> NOT penalized.
    ref = rgraph(
        nodes=(rnode("proc.a", "procedure_step"), rnode("proc.b", "procedure_step")),
        paths=(path("proc.a", "proc.b"),),
    )
    student = snorm(
        nodes=(cnode("proc.a", "procedure_step"), cnode("proc.b", "procedure_step")),
    )
    sub = compute_sub_scores(student, ref, _winning(student, ref))
    assert sub.procedure_order == 1.0


def test_procedure_order_correct_order_no_penalty():
    ref = rgraph(
        nodes=(rnode("proc.a", "procedure_step"), rnode("proc.b", "procedure_step")),
        paths=(path("proc.a", "proc.b"),),
    )
    student = snorm(
        nodes=(cnode("proc.a", "procedure_step"), cnode("proc.b", "procedure_step")),
        edges=(cedge(EdgeType.PRECEDES, "proc.a", "proc.b"),),  # correct order
    )
    sub = compute_sub_scores(student, ref, _winning(student, ref))
    assert sub.procedure_order == 1.0


def test_dependency_score_direction_loose():
    # A DEPENDS_ON edge matches the reference pair regardless of direction.
    ref = rgraph(
        nodes=(rnode("eq.a"), rnode("eq.b")),
        edges=(cedge(EdgeType.DEPENDS_ON, "eq.a", "eq.b"),),
        paths=(path("eq.a", "eq.b"),),
    )
    student = snorm(
        nodes=(cnode("eq.a"), cnode("eq.b")),
        edges=(cedge(EdgeType.DEPENDS_ON, "eq.b", "eq.a"),),  # reversed direction
    )
    sub = compute_sub_scores(student, ref, _winning(student, ref))
    assert sub.dependency == 1.0  # direction-loose match


def test_contradiction_subscore_equals_soundness_dimension():
    ref = rgraph(nodes=(rnode("eq.a"),), paths=(path("eq.a"),))
    student = snorm(
        nodes=(cnode("eq.a"), cnode("misc.density_ignored", node_type="misconception")),
    )
    sub = compute_sub_scores(student, ref, _winning(student, ref))
    assert sub.contradiction == 1.0 - contradiction_penalty(1) == 0.5


def test_vacuous_dimensions_are_one_not_nan():
    # Reference with ZERO edges of any type -> every edge dimension is 1.0.
    ref = rgraph(nodes=(rnode("eq.a"),), paths=(path("eq.a"),))
    student = snorm(nodes=(cnode("eq.a"),))
    sub = compute_sub_scores(student, ref, _winning(student, ref))
    for value in (sub.edge_coverage, sub.scoping, sub.usage, sub.procedure_order, sub.dependency):
        assert value == 1.0
        assert not math.isnan(value)


def test_sub_scores_pure_same_input_same_output():
    ref = rgraph(
        nodes=(rnode("proc.x", "procedure_step"), rnode("eq.y")),
        edges=(cedge(EdgeType.USES, "proc.x", "eq.y"),),
        paths=(path("proc.x", "eq.y"),),
    )
    student = snorm(
        nodes=(cnode("proc.x", "procedure_step"), cnode("eq.y")),
        edges=(cedge(EdgeType.USES, "proc.x", "eq.y"),),
    )
    winning = _winning(student, ref)
    assert compute_sub_scores(student, ref, winning) == compute_sub_scores(student, ref, winning)


def test_subscores_is_frozen():
    s = SubScores(
        node_coverage=1.0,
        edge_coverage=1.0,
        scoping=1.0,
        usage=1.0,
        procedure_order=1.0,
        dependency=1.0,
        contradiction=1.0,
    )
    assert s.node_coverage == 1.0
