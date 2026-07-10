"""T5 smoke tests — graded edge credit (§5.6) + winning-path aggregation
(§5.7). Deterministic, FakeNLI-injected, no network / model load / DB."""

from __future__ import annotations

import math

import pytest

from apollo.graph_compare.canonical import (
    CanonicalEdge,
    CanonicalGraph,
    CanonicalNode,
    ReferenceGraph,
    ReferencePathView,
)
from apollo.graph_compare.coverage import coverage_result
from apollo.ontology.edges import EdgeType
from apollo.resolution.nli_adjudicator import FakeNLIAdjudicator, NLIResult
from apollo.resolver_v2.aggregate import aggregate
from apollo.resolver_v2.config import ResolverV2Params
from apollo.resolver_v2.edges import score_edges, verbalize_edge
from apollo.resolver_v2.types import EdgeScore, NodeScore, PairScore, Window

_PARAMS = ResolverV2Params()  # t_edge=0.85, edge_pullup_floor=0.6

U = "proc.apply_continuity"
V = "eq.continuity"


# ---------------------------------------------------------------------------
# fixtures / helpers
# ---------------------------------------------------------------------------


def _windows(*texts: str) -> tuple[Window, ...]:
    return tuple(Window(index=i, turn_index=0, text=t) for i, t in enumerate(texts))


def _node(key: str, credit: float, best_window: int | None = None) -> NodeScore:
    best = None
    if best_window is not None:
        best = PairScore(
            window_index=best_window,
            view_index=0,
            lexical=0.5,
            entailment=0.0,
            contradiction=0.0,
            fused=0.5,
        )
    return NodeScore(canonical_key=key, score=credit, credit=credit, source="nli", best=best)


def _edge(edge_type: EdgeType = EdgeType.USES, u: str = U, v: str = V) -> CanonicalEdge:
    return CanonicalEdge(edge_type=edge_type, from_key=u, to_key=v, provenance="explicit")


def _nli(entailment: float) -> NLIResult:
    label = "entailment" if entailment >= 0.5 else "neutral"
    return NLIResult(
        label=label,
        entailment=entailment,
        contradiction=0.01,
        neutral=max(0.0, 1.0 - entailment - 0.01),
        model_name="fake",
    )


def _select_all(windows, view_text, k):
    """SelectFn fake: first-k windows, descending fake lex."""
    return tuple((w.index, 0.5) for w in windows)[:k]


def _select_never(windows, view_text, k):
    raise AssertionError("select_fn must not be called (ENTAIL gate closed / no budget)")


def _score(edges, node_scores, windows, *, nli, select_fn, labels=None,
           explicit=frozenset(), inferred=frozenset(), budget=100, params=_PARAMS):
    return score_edges(
        edges,
        node_scores,
        windows,
        labels=labels if labels is not None else {},
        nli=nli,
        params=params,
        v1_explicit_triples=explicit,
        v1_inferred_triples=inferred,
        select_fn=select_fn,
        pair_budget_left=budget,
    )


# ---------------------------------------------------------------------------
# verbalize_edge — §5.6 templates
# ---------------------------------------------------------------------------


def test_verbalize_edge_templates():
    assert verbalize_edge("USES", "apply continuity", "the continuity equation") == (
        "apply continuity uses the continuity equation."
    )
    assert verbalize_edge("DEPENDS_ON", "A", "B") == "B requires A."
    assert verbalize_edge("PRECEDES", "A", "B") == "A happens before B."
    assert verbalize_edge("SCOPES", "steady flow", "Bernoulli") == (
        "Bernoulli applies when steady flow."
    )
    # defensive fallback for an unknown type — never raises
    assert verbalize_edge("MYSTERY", "A", "B") == "A relates to B."


# ---------------------------------------------------------------------------
# score_edges — ENTAIL tier + pull-up (the card's happy path)
# ---------------------------------------------------------------------------


def test_entail_tier_pulls_up_endpoints_and_grades_credit():
    """Scripted entailment on a verbalized USES edge -> evidence "entail",
    both endpoints floored at 0.6, edge_credit = 1.0 * sqrt(0.6 * 0.6)."""
    windows = _windows("the flow rate stays the same through the pipe", "so A1 v1 = A2 v2")
    labels = {U: "apply continuity", V: "the continuity equation"}
    hypothesis = "apply continuity uses the continuity equation."
    # Endpoint credits 0.0 — the gate opens via best-window co-occurrence.
    nodes = {U: _node(U, 0.0, best_window=0), V: _node(V, 0.0, best_window=1)}
    nli = FakeNLIAdjudicator({(windows[0].text, hypothesis): _nli(0.95)})

    scores, pullups, pairs = _score(
        (_edge(),), nodes, windows, nli=nli, select_fn=_select_all, labels=labels
    )

    assert len(scores) == 1
    assert scores[0].relation_evidence == "entail"
    assert scores[0].credit == pytest.approx(math.sqrt(0.6 * 0.6))  # = 0.6
    assert pullups == {U: 0.6, V: 0.6}
    assert pairs == 1  # early stop on the first qualifying window


def test_entail_boundary_exactly_t_edge_fires():
    windows = _windows("w0 text", "w1 text")
    hypothesis = f"{U} uses {V}."  # labels default to canonical keys
    nodes = {U: _node(U, 0.5, best_window=0), V: _node(V, 0.5, best_window=0)}
    nli = FakeNLIAdjudicator({(windows[0].text, hypothesis): _nli(_PARAMS.t_edge)})

    scores, pullups, pairs = _score((_edge(),), nodes, windows, nli=nli, select_fn=_select_all)

    assert scores[0].relation_evidence == "entail"
    # pull-up floors 0.5 -> 0.6 inside the formula too
    assert scores[0].credit == pytest.approx(1.0 * math.sqrt(0.6 * 0.6))
    assert pullups == {U: 0.6, V: 0.6}
    assert pairs == 1


def test_below_t_edge_falls_to_cooccur_no_pullup():
    windows = _windows("w0 text", "w1 text")
    hypothesis = f"{U} uses {V}."
    nodes = {U: _node(U, 0.5, best_window=0), V: _node(V, 0.5, best_window=1)}
    nli = FakeNLIAdjudicator(
        {
            (windows[0].text, hypothesis): _nli(0.84),  # just under t_edge=0.85
            (windows[1].text, hypothesis): _nli(0.84),
        }
    )

    scores, pullups, pairs = _score((_edge(),), nodes, windows, nli=nli, select_fn=_select_all)

    assert scores[0].relation_evidence == "cooccur"
    assert scores[0].credit == pytest.approx(0.7 * math.sqrt(0.5 * 0.5))  # no floor
    assert pullups == {}
    assert pairs == 2  # both top-2 windows tried, neither entailed


# ---------------------------------------------------------------------------
# score_edges — COOCCUR / ENDPOINTS / none tiers (nli=None degrade)
# ---------------------------------------------------------------------------


def test_nli_none_cooccur_tier_still_applies():
    nodes = {U: _node(U, 0.4, best_window=2), V: _node(V, 0.4, best_window=3)}  # adjacent
    scores, pullups, pairs = _score(
        (_edge(),), nodes, _windows("a", "b", "c", "d"), nli=None, select_fn=_select_never
    )
    assert scores[0].relation_evidence == "cooccur"
    assert scores[0].credit == pytest.approx(0.7 * math.sqrt(0.4 * 0.4))
    assert pullups == {} and pairs == 0


def test_endpoints_tier_boundary():
    # non-adjacent best windows, no NLI -> ENDPOINTS is the only applicable tier
    nodes = {U: _node(U, 0.7, best_window=0), V: _node(V, 0.7, best_window=3)}
    scores, _, _ = _score((_edge(),), nodes, _windows("a", "b", "c", "d"), nli=None,
                          select_fn=_select_never)
    assert scores[0].relation_evidence == "endpoints"
    assert scores[0].credit == pytest.approx(0.4 * math.sqrt(0.7 * 0.7))

    # just below the 0.7 bar -> no tier applies
    nodes_low = {U: _node(U, 0.69, best_window=0), V: _node(V, 0.7, best_window=3)}
    scores_low, pullups_low, _ = _score(
        (_edge(),), nodes_low, _windows("a", "b", "c", "d"), nli=None, select_fn=_select_never
    )
    assert scores_low[0].relation_evidence == "none"
    assert scores_low[0].credit == 0.0
    assert pullups_low == {}


def test_missing_endpoints_score_zero_without_crashing():
    scores, pullups, pairs = _score(
        (_edge(),), {}, _windows("a"), nli=None, select_fn=_select_never
    )
    assert scores[0].credit == 0.0
    assert scores[0].relation_evidence == "none"
    assert not math.isnan(scores[0].credit)
    assert pullups == {} and pairs == 0


# ---------------------------------------------------------------------------
# score_edges — ENTAIL gate + budget guard
# ---------------------------------------------------------------------------


def test_entail_gate_closed_runs_no_nli():
    """min credit < 0.3 and non-adjacent best windows -> the NLI tier never
    runs (FakeNLI({}) would KeyError, _select_never would raise)."""
    nodes = {U: _node(U, 0.2, best_window=0), V: _node(V, 0.2, best_window=3)}
    scores, pullups, pairs = _score(
        (_edge(),), nodes, _windows("a", "b", "c", "d"),
        nli=FakeNLIAdjudicator({}), select_fn=_select_never,
    )
    assert pairs == 0
    assert scores[0].relation_evidence == "none"
    assert scores[0].credit == 0.0
    assert pullups == {}


def test_pair_budget_caps_edge_nli():
    """budget=1 stops before the second (entailing) window; budget=2 reaches
    it. budget=0 -> no NLI at all."""
    windows = _windows("w0 text", "w1 text")
    hypothesis = f"{U} uses {V}."
    # gate open via min credit >= 0.3; NOT co-occurring (so no cooccur rescue)
    nodes = {U: _node(U, 0.5, best_window=0), V: _node(V, 0.5, best_window=3)}
    scripted = {
        (windows[0].text, hypothesis): _nli(0.10),
        (windows[1].text, hypothesis): _nli(0.95),
    }

    scores_1, pullups_1, pairs_1 = _score(
        (_edge(),), nodes, windows, nli=FakeNLIAdjudicator(scripted),
        select_fn=_select_all, budget=1,
    )
    assert pairs_1 == 1
    assert scores_1[0].relation_evidence == "none"  # 0.5 < endpoints bar, no cooccur
    assert pullups_1 == {}

    scores_2, pullups_2, pairs_2 = _score(
        (_edge(),), nodes, windows, nli=FakeNLIAdjudicator(scripted),
        select_fn=_select_all, budget=2,
    )
    assert pairs_2 == 2
    assert scores_2[0].relation_evidence == "entail"
    assert scores_2[0].credit == pytest.approx(math.sqrt(0.6 * 0.6))
    assert pullups_2 == {U: 0.6, V: 0.6}

    _, _, pairs_0 = _score(
        (_edge(),), nodes, windows, nli=FakeNLIAdjudicator({}),
        select_fn=_select_never, budget=0,
    )
    assert pairs_0 == 0


def test_budget_is_shared_across_edges():
    windows = _windows("w0 text", "w1 text")
    u2, v2 = "proc.second", "eq.second"
    nodes = {
        U: _node(U, 0.5, best_window=0),
        V: _node(V, 0.5, best_window=3),
        u2: _node(u2, 0.5, best_window=0),
        v2: _node(v2, 0.5, best_window=3),
    }
    hyp_1 = f"{U} uses {V}."
    scripted = {
        (windows[0].text, hyp_1): _nli(0.10),
        (windows[1].text, hyp_1): _nli(0.10),
        # the second edge's hypothesis is deliberately NOT scripted — if the
        # exhausted budget failed to gate it, FakeNLI would KeyError
    }
    edges = (_edge(), _edge(u=u2, v=v2))
    _, _, pairs = _score(
        edges, nodes, windows, nli=FakeNLIAdjudicator(scripted),
        select_fn=_select_all, budget=2,
    )
    assert pairs == 2  # edge 1 consumed the whole budget; edge 2 ran no NLI


# ---------------------------------------------------------------------------
# score_edges — v1 floors
# ---------------------------------------------------------------------------


def test_v1_explicit_floor_wins_over_zero_graded_credit():
    triple = ("USES", U, V)  # plain strings interop with the EdgeType StrEnum
    scores, pullups, _ = _score(
        (_edge(),), {}, (), nli=None, select_fn=_select_never,
        explicit=frozenset({triple}),
    )
    assert scores[0].credit == 1.0
    assert scores[0].relation_evidence == "v1_explicit"
    assert pullups == {}  # v1 floor is not relation entailment — no pull-up


def test_v1_inferred_floor_and_graded_credit_beats_it():
    triple = ("USES", U, V)
    # graded 0.0 -> inferred floor 0.5 wins
    scores_zero, _, _ = _score(
        (_edge(),), {}, (), nli=None, select_fn=_select_never,
        inferred=frozenset({triple}),
    )
    assert scores_zero[0].credit == 0.5
    assert scores_zero[0].relation_evidence == "v1_inferred"

    # graded cooccur credit 0.7*sqrt(0.81) = 0.63 > 0.5 -> graded wins, keeps tier
    nodes = {U: _node(U, 0.9, best_window=0), V: _node(V, 0.9, best_window=1)}
    scores_graded, _, _ = _score(
        (_edge(),), nodes, _windows("a", "b"), nli=None, select_fn=_select_never,
        inferred=frozenset({triple}),
    )
    assert scores_graded[0].credit == pytest.approx(0.7 * 0.9)
    assert scores_graded[0].relation_evidence == "cooccur"


# ---------------------------------------------------------------------------
# aggregate — winning-path selection (§5.7)
# ---------------------------------------------------------------------------


def _reference(*paths: tuple[str, ...]) -> ReferenceGraph:
    return ReferenceGraph(
        nodes=(), edges=(), paths=tuple(ReferencePathView(canonical_keys=p) for p in paths)
    )


def _edge_score(credit: float) -> EdgeScore:
    return EdgeScore(
        edge_type="USES", from_key=U, to_key=V, credit=credit, relation_evidence="entail"
    )


def test_aggregate_picks_highest_graded_path_and_means_edges():
    nodes = {"a": _node("a", 1.0), "b": _node("b", 0.6), "c": _node("c", 0.0)}
    reference = _reference(("a", "b", "c"), ("a", "b"))
    edge_scores = (_edge_score(1.0), _edge_score(0.5), _edge_score(0.0))

    node_cov, edge_cov, winning = aggregate(reference, nodes, edge_scores)

    assert node_cov == pytest.approx(0.8)  # path 1: (1.0 + 0.6) / 2
    assert winning == 1
    assert edge_cov == pytest.approx(0.5)  # graded mean over ALL ref edges


def test_aggregate_tie_breaks_to_lowest_path_index():
    nodes = {"a": _node("a", 1.0), "b": _node("b", 0.6)}
    reference = _reference(("a", "b"), ("b", "a"))  # both mean 0.8
    node_cov, _, winning = aggregate(reference, nodes, ())
    assert node_cov == pytest.approx(0.8)
    assert winning == 0


def test_aggregate_vacuous_cases_never_nan():
    # empty path scores vacuous 1.0 (v1 parity) and wins
    node_cov, edge_cov, winning = aggregate(_reference((), ("a",)), {}, ())
    assert node_cov == 1.0 and winning == 0
    # zero reference edges -> edge_coverage vacuous 1.0 (v1 parity)
    assert edge_cov == 1.0
    # no paths at all (defensive; upstream guarantees >= 1)
    node_cov_np, edge_cov_np, winning_np = aggregate(_reference(), {}, ())
    assert (node_cov_np, edge_cov_np, winning_np) == (1.0, 1.0, 0)
    for value in (node_cov, edge_cov, node_cov_np, edge_cov_np):
        assert not math.isnan(value)


def test_aggregate_missing_node_key_counts_zero():
    reference = _reference(("a", "ghost"))
    node_cov, _, _ = aggregate(reference, {"a": _node("a", 1.0)}, ())
    assert node_cov == pytest.approx(0.5)


def test_aggregate_binary_credit_reproduces_v1_coverage_semantics():
    """DONE-gate: on credit ∈ {0,1}, aggregate == v1 set-membership coverage
    (same winner, same score), incl. the tie rule."""
    student = CanonicalGraph(
        nodes=(
            CanonicalNode(
                canonical_key="a", node_type="equation",
                source_node_ids=("n1",), evidence_spans=("a text",),
            ),
            CanonicalNode(
                canonical_key="b", node_type="procedure_step",
                source_node_ids=("n2",), evidence_spans=("b text",),
            ),
        ),
        edges=(), unresolved_nodes=(), dropped_edge_count=0,
    )
    for paths in (
        (("a", "b", "c"), ("b", "c")),  # distinct scores
        (("a", "x"), ("b", "y")),  # tie -> lowest index
    ):
        reference = _reference(*paths)
        v1_score, v1_winning, _ = coverage_result(student, reference)
        student_keys = {n.canonical_key for n in student.nodes}
        binary = {
            key: _node(key, 1.0 if key in student_keys else 0.0)
            for path in paths
            for key in path
        }
        node_cov, _, winning = aggregate(reference, binary, ())
        assert node_cov == pytest.approx(v1_score)
        assert winning == v1_winning.path_index
