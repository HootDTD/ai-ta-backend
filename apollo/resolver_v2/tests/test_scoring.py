"""T4 smoke tests: views loader, NLI provider, fusion + graded credit + max-aggregation.

Prototype gate per the design card: fusion math, max-aggregation over
windows x views, credit tiers, contradiction veto, polarity screen, v1 floor,
pair-budget + memo cache. Every test injects a FakeNLIAdjudicator and a fake
SelectFn — ZERO model loads, zero real prefilter (T3 builds it in parallel).
The 95% patch gate is explicitly deferred for this branch.
"""

from __future__ import annotations

import json
import logging

import pytest

from apollo.graph_compare.canonical import (
    CanonicalNode,
    ReferenceGraph,
    ReferencePathView,
)
from apollo.resolution.nli_adjudicator import (
    FakeNLIAdjudicator,
    NLIResult,
    TransformersNLIAdjudicator,
)
from apollo.resolver_v2 import nli_provider
from apollo.resolver_v2 import views as views_mod
from apollo.resolver_v2.config import ResolverV2Params
from apollo.resolver_v2.scoring import credit_for_score, fuse, score_nodes
from apollo.resolver_v2.types import RefNode, SelectFn, Window
from apollo.resolver_v2.views import build_ref_nodes, load_views

_PARAMS = ResolverV2Params()

_W0 = Window(index=0, turn_index=0, text="The student explains conservation of mass in the pipe.")
_W1 = Window(index=1, turn_index=1, text="The flow rate stays equal at both sections of the pipe.")


def _nli(entailment: float = 0.0, contradiction: float = 0.0) -> NLIResult:
    neutral = max(0.0, 1.0 - entailment - contradiction)
    label = max(
        ("entailment", entailment), ("neutral", neutral), ("contradiction", contradiction),
        key=lambda kv: kv[1],
    )[0]
    return NLIResult(label, entailment, contradiction, neutral, "fake-nli")


def _script_all(
    windows: tuple[Window, ...], views: tuple[str, ...]
) -> dict[tuple[str, str], NLIResult]:
    """Script EVERY (window, view) pair with a low-signal default; tests
    override the interesting keys."""
    return {(w.text, v): _nli(0.10, 0.02) for w in windows for v in views}


class CountingNLI:
    """FakeNLIAdjudicator wrapper that counts real classify calls (the unit
    the §5.4 budget meters)."""

    def __init__(self, scripted: dict[tuple[str, str], NLIResult]):
        self._fake = FakeNLIAdjudicator(scripted)
        self.calls = 0

    def classify(self, premise: str, hypothesis: str) -> NLIResult:
        self.calls += 1
        return self._fake.classify(premise, hypothesis)


def _select_by_lex(lex_by_window: dict[int, float]) -> SelectFn:
    """Fake SelectFn honoring the T3 contract: top-k (window_index, lex),
    sorted by (-lex, index)."""

    def select(windows, view_text, k):  # noqa: ANN001 - SelectFn shape
        ranked = sorted(
            ((w.index, lex_by_window.get(w.index, 0.0)) for w in windows),
            key=lambda pair: (-pair[1], pair[0]),
        )
        return tuple(ranked[:k])

    return select


def _node(key: str, *views: str) -> RefNode:
    return RefNode(canonical_key=key, node_type="equation", label=views[0], views=tuple(views))


# --- fusion math ---------------------------------------------------------------


def test_fuse_linear_interpolation():
    assert fuse(0.95, 0.4, _PARAMS) == pytest.approx(0.85 * 0.95 + 0.15 * 0.4)
    assert fuse(0.95, 0.4, ResolverV2Params(alpha=1.0)) == pytest.approx(0.95)
    assert fuse(0.0, 0.0, _PARAMS) == 0.0


# --- credit tiers ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("score", "expected_credit", "expected_gray"),
    [
        (0.95, 1.0, False),
        (0.90, 1.0, False),  # t_high boundary is inclusive
        (0.89, 0.7, False),
        (0.75, 0.7, False),  # t_mid boundary is inclusive
        (0.74, 0.3, True),
        (0.40, 0.3, True),  # t_low boundary is inclusive
        (0.39, 0.0, False),
        (0.0, 0.0, False),
    ],
)
def test_credit_tiers(score: float, expected_credit: float, expected_gray: bool):
    assert credit_for_score(score, _PARAMS) == (expected_credit, expected_gray)


# --- score_nodes: happy path + max-aggregation --------------------------------


def test_score_nodes_high_entailment_credits_full():
    """Scripted entailment 0.95 on the right (window, view) -> credit 1.0,
    ``best`` records exactly that pair."""
    windows = (_W0, _W1)
    node = _node(
        "eq.continuity",
        "Continuity (mass conservation)",
        "The product of area and velocity stays equal at both sections.",
    )
    scripted = _script_all(windows, node.views)
    scripted[(_W1.text, node.views[0])] = _nli(0.95, 0.01)
    scores, pair_count = score_nodes(
        windows, (node,),
        nli=FakeNLIAdjudicator(scripted), params=_PARAMS,
        v1_resolved_keys=frozenset(), select_fn=_select_by_lex({0: 0.2, 1: 0.7}),
    )
    (result,) = scores
    assert result.credit == 1.0
    assert result.source == "nli"
    assert result.score == pytest.approx(0.85 * 0.95 + 0.15 * 0.7)
    assert result.best is not None
    assert (result.best.window_index, result.best.view_index) == (1, 0)
    assert pair_count == 4  # 2 windows x 2 views, all really classified


def test_score_nodes_max_over_views():
    """Multi-view: only view index 2 entails -> the node is still credited
    (max-aggregation over views)."""
    windows = (_W0,)
    node = _node(
        "eq.continuity",
        "Continuity of the volumetric flow in a pipe",
        "Mass flow is the same everywhere along the pipe.",
        "The area velocity product is equal at the two sections.",
    )
    scripted = _script_all(windows, node.views)
    scripted[(_W0.text, node.views[2])] = _nli(0.95, 0.01)
    scores, pair_count = score_nodes(
        windows, (node,),
        nli=FakeNLIAdjudicator(scripted), params=_PARAMS,
        v1_resolved_keys=frozenset(), select_fn=_select_by_lex({0: 0.7}),
    )
    (result,) = scores
    assert result.credit == 1.0
    assert result.best is not None and result.best.view_index == 2
    assert pair_count == 3


def test_score_nodes_gray_band_gets_deterministic_default():
    """A mid fused score lands in the gray band -> credit 0.3, source 'nli'
    (grayzone upgrade is the engine's job, T7)."""
    windows = (_W0,)
    node = _node("eq.continuity", "Continuity (mass conservation)")
    scripted = {(_W0.text, node.views[0]): _nli(0.50, 0.02)}
    scores, _ = score_nodes(
        windows, (node,),
        nli=FakeNLIAdjudicator(scripted), params=_PARAMS,
        v1_resolved_keys=frozenset(), select_fn=_select_by_lex({0: 0.5}),
    )
    assert scores[0].score == pytest.approx(0.85 * 0.50 + 0.15 * 0.5)
    assert scores[0].credit == 0.3
    assert scores[0].source == "nli"


# --- screens ------------------------------------------------------------------


def test_contradiction_veto_zeroes_pair():
    """P(contradiction) 0.9 > max_contradiction -> pair vetoed even with high
    entailment; raw NLI numbers stay on the PairScore for the trace."""
    windows = (_W0,)
    node = _node("eq.continuity", "Continuity (mass conservation)")
    scripted = {(_W0.text, node.views[0]): _nli(0.90, 0.90)}
    scores, pair_count = score_nodes(
        windows, (node,),
        nli=FakeNLIAdjudicator(scripted), params=_PARAMS,
        v1_resolved_keys=frozenset(), select_fn=_select_by_lex({0: 0.5}),
    )
    (result,) = scores
    assert result.credit == 0.0
    assert result.best is not None
    assert result.best.fused == 0.0
    assert result.best.entailment == pytest.approx(0.90)
    assert result.best.contradiction == pytest.approx(0.90)
    assert pair_count == 1


def test_polarity_screen_blocks_nli_entirely():
    """Negation XOR (window negated, affirmative view) -> pair contributes 0
    WITHOUT any NLI call (an empty script would KeyError if reached)."""
    window = Window(index=0, turn_index=0, text="The flow is not steady in the pipe section.")
    node = _node("cond.steady_flow", "The flow is steady in the pipe section.")
    scores, pair_count = score_nodes(
        (window,), (node,),
        nli=FakeNLIAdjudicator({}), params=_PARAMS,
        v1_resolved_keys=frozenset(), select_fn=_select_by_lex({0: 0.5}),
    )
    (result,) = scores
    assert result.credit == 0.0
    assert result.best is not None
    assert result.best.fused == 0.0
    assert result.best.entailment == 0.0  # NLI skipped
    assert pair_count == 0


# --- floors, degrades, budget ---------------------------------------------------


def test_v1_floor_lifts_otherwise_zero_node():
    node_floored = _node("eq.continuity", "Continuity (mass conservation)")
    node_plain = _node("eq.bernoulli", "Bernoulli's equation relates pressure and velocity.")
    scores, pair_count = score_nodes(
        (_W0,), (node_floored, node_plain),
        nli=None, params=_PARAMS,
        v1_resolved_keys=frozenset({"eq.continuity"}), select_fn=_select_by_lex({0: 0.0}),
    )
    floored, plain = scores
    assert (floored.credit, floored.source) == (1.0, "v1_floor")
    assert floored.score == 0.0  # the floor lifts credit, not the raw score
    assert (plain.credit, plain.source) == (0.0, "lexical_skip")
    assert pair_count == 0


def test_nli_none_degrades_lexical_only():
    nodes = (
        _node("eq.continuity", "Continuity (mass conservation)"),
        _node("eq.bernoulli", "Bernoulli's equation relates pressure and velocity."),
    )
    scores, pair_count = score_nodes(
        (_W0, _W1), nodes,
        nli=None, params=_PARAMS,
        v1_resolved_keys=frozenset(), select_fn=_select_by_lex({0: 0.3, 1: 0.2}),
    )
    assert [s.source for s in scores] == ["lexical_skip", "lexical_skip"]
    assert all(s.best is None for s in scores)
    assert scores[0].score == pytest.approx(0.3)  # score = max lex
    assert pair_count == 0


def test_lexical_skip_below_floor_runs_no_nli():
    node = _node("eq.continuity", "Continuity (mass conservation)")
    scores, pair_count = score_nodes(
        (_W0,), (node,),
        nli=FakeNLIAdjudicator({}),  # would KeyError if NLI ran
        params=_PARAMS,
        v1_resolved_keys=frozenset(), select_fn=_select_by_lex({0: 0.05}),
    )
    (result,) = scores
    assert (result.source, result.best) == ("lexical_skip", None)
    assert result.score == pytest.approx(0.05)
    assert result.credit == 0.0
    assert pair_count == 0


def test_pair_budget_respected_second_node_degrades():
    """budget=1: node 1 consumes the only NLI pair; node 2 falls back to
    lexical_skip without touching the adjudicator."""
    params = ResolverV2Params(max_nli_pairs=1, top_k_windows=1)
    node1 = _node("eq.continuity", "Continuity (mass conservation)")
    node2 = _node("eq.bernoulli", "Bernoulli's equation relates pressure and velocity.")
    counting = CountingNLI({(_W0.text, node1.views[0]): _nli(0.20, 0.02)})
    scores, pair_count = score_nodes(
        (_W0,), (node1, node2),
        nli=counting, params=params,
        v1_resolved_keys=frozenset(), select_fn=_select_by_lex({0: 0.30}),
    )
    first, second = scores
    assert first.source == "nli"
    assert (second.source, second.best) == ("lexical_skip", None)
    assert second.score == pytest.approx(0.30)
    assert pair_count == 1
    assert counting.calls == 1


def test_pair_memo_cache_shared_view_scored_once():
    """A view shared across nodes is never re-scored: one real NLI call, both
    nodes credited from the cached result."""
    shared = "The pressure difference drives the flow through the pipe."
    node_a = RefNode("proc.a", "procedure_step", shared, (shared,))
    node_b = RefNode("proc.b", "procedure_step", shared, (shared,))
    counting = CountingNLI({(_W0.text, shared): _nli(0.80, 0.02)})
    scores, pair_count = score_nodes(
        (_W0,), (node_a, node_b),
        nli=counting, params=_PARAMS,
        v1_resolved_keys=frozenset(), select_fn=_select_by_lex({0: 0.5}),
    )
    expected = 0.85 * 0.80 + 0.15 * 0.5  # 0.755 >= t_mid -> credit 0.7
    assert [s.credit for s in scores] == [0.7, 0.7]
    assert scores[0].score == pytest.approx(expected)
    assert scores[1].score == pytest.approx(expected)
    assert pair_count == 1
    assert counting.calls == 1


def test_empty_windows_yield_zero_source():
    node = _node("eq.continuity", "Continuity (mass conservation)")
    node_v1 = _node("eq.bernoulli", "Bernoulli's equation relates pressure and velocity.")
    scores, pair_count = score_nodes(
        (), (node, node_v1),
        nli=FakeNLIAdjudicator({}), params=_PARAMS,
        v1_resolved_keys=frozenset({"eq.bernoulli"}),
        select_fn=lambda windows, view_text, k: (),
    )
    assert (scores[0].source, scores[0].score, scores[0].credit) == ("zero", 0.0, 0.0)
    assert (scores[1].source, scores[1].credit) == ("v1_floor", 1.0)  # floor applies everywhere
    assert pair_count == 0


# --- views loader ----------------------------------------------------------------


_FIXTURE_CACHE = {
    "_meta": {"model": "test-model", "date": "2026-07-07"},
    "bernoulli_principle/problem_01": {
        "eq.continuity": [
            "Mass flow is conserved along the pipe.",
            "   ",  # blank -> dropped
            42,  # non-string -> dropped
            "The product of area and velocity is the same at both sections.",
        ],
        "cond.incompressibility": "not-a-list",  # malformed entry -> dropped
    },
}


def _write_cache(tmp_path, payload):
    path = tmp_path / "views_cache.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_load_views_happy_path(monkeypatch, tmp_path):
    monkeypatch.setattr(views_mod, "VIEWS_CACHE_PATH", _write_cache(tmp_path, _FIXTURE_CACHE))
    result = load_views("bernoulli_principle", "problem_01")
    assert result == {
        "eq.continuity": (
            "Mass flow is conserved along the pipe.",
            "The product of area and velocity is the same at both sections.",
        )
    }


def test_load_views_missing_problem_and_file_degrade(monkeypatch, tmp_path):
    """Missing problem key, missing file, and unparseable JSON all degrade to
    {} without raising (§5.2)."""
    monkeypatch.setattr(views_mod, "VIEWS_CACHE_PATH", _write_cache(tmp_path, _FIXTURE_CACHE))
    assert load_views("bernoulli_principle", "problem_99") == {}
    monkeypatch.setattr(views_mod, "VIEWS_CACHE_PATH", tmp_path / "missing.json")
    assert load_views("bernoulli_principle", "problem_01") == {}
    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(views_mod, "VIEWS_CACHE_PATH", broken)
    assert load_views("bernoulli_principle", "problem_01") == {}


# --- build_ref_nodes ----------------------------------------------------------


def _reference_graph() -> ReferenceGraph:
    nodes = (
        CanonicalNode("eq.continuity", "equation", ("continuity",), ()),
        CanonicalNode("cond.incompressibility", "condition", ("incompressibility",), ()),
    )
    paths = (
        ReferencePathView(("eq.continuity", "cond.incompressibility")),
        ReferencePathView(("eq.continuity", "def.mystery")),  # key with no ref node
    )
    return ReferenceGraph(nodes=nodes, edges=(), paths=paths)


def test_build_ref_nodes_labels_views_and_order():
    payload = {
        "reference_solution": [
            {"entity_key": "eq.continuity", "content": {"label": "Continuity (mass conservation)"}},
            {"entity_key": "eq.unused", "content": {"label": "Never on a path"}},
            "malformed-step",
        ]
    }
    views_by_key = {
        "eq.continuity": (
            "The product of area and velocity is the same at both sections.",
            "Continuity (mass conservation)",  # duplicate of the label -> deduped
        )
    }
    ref_nodes = build_ref_nodes(_reference_graph(), payload, views_by_key)
    # union of path keys, key-sorted; eq.unused is NOT on any path
    assert [n.canonical_key for n in ref_nodes] == [
        "cond.incompressibility", "def.mystery", "eq.continuity",
    ]
    continuity = ref_nodes[2]
    assert continuity.label == "Continuity (mass conservation)"
    assert continuity.views == (
        "Continuity (mass conservation)",
        "The product of area and velocity is the same at both sections.",
    )
    assert continuity.node_type == "equation"
    # no payload label / no cached views -> label falls back to the key
    incompressibility = ref_nodes[0]
    assert incompressibility.label == "cond.incompressibility"
    assert incompressibility.views == ("cond.incompressibility",)
    assert incompressibility.node_type == "condition"
    # path key without a reference node degrades, never raises
    assert ref_nodes[1].node_type == "unknown"
    assert ref_nodes[1].views == ("def.mystery",)


# --- nli_provider ---------------------------------------------------------------


def test_get_adjudicator_is_lazy_singleton(monkeypatch):
    monkeypatch.setattr(nli_provider, "_ADJUDICATOR", None)
    monkeypatch.setattr(nli_provider, "_transformers_available", lambda: True)
    adjudicator = nli_provider.get_adjudicator()
    assert isinstance(adjudicator, TransformersNLIAdjudicator)
    assert adjudicator._pipe is None  # constructed lazily — NO model load
    assert nli_provider.get_adjudicator() is adjudicator  # process singleton


def test_get_adjudicator_degrades_without_transformers(monkeypatch, caplog):
    monkeypatch.setattr(nli_provider, "_ADJUDICATOR", None)
    monkeypatch.setattr(nli_provider, "_IMPORT_FAILURE_LOGGED", False)
    monkeypatch.setattr(nli_provider, "_transformers_available", lambda: False)
    with caplog.at_level(logging.WARNING, logger="apollo.resolver_v2.nli_provider"):
        assert nli_provider.get_adjudicator() is None
        assert nli_provider.get_adjudicator() is None
    warnings = [
        record for record in caplog.records
        if "resolver_v2_nli_transformers_unavailable" in record.getMessage()
    ]
    assert len(warnings) == 1  # logged once per process, not per call
