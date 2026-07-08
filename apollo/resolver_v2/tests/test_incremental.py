"""Tests for T3: the incremental per-turn scorer (design §5.3-§5.4).

Acceptance criteria under test:

* incremental == batch on the pure-ladder monotone fixture (no grayzone, no
  v1 edge floors, no budget truncation);
* conservative incremental_cov <= batch_cov on the general fixture
  (A-MAJOR-1/3/4);
* global window index/turn_index continue across turns (A-MAJOR-2);
* edge coverage monotone non-decreasing across turns;
* unresolved-only rescoring: a resolved node spends 0 pairs on later turns;
* budget truncation sets ``budget_truncated``;
* overlap seam is correct (windows never invented/duplicated at the turn
  boundary).
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from apollo.graph_compare.canonical import (
    CanonicalEdge,
    CanonicalNode,
    ReferenceGraph,
    ReferencePathView,
)
from apollo.ontology.edges import EdgeType
from apollo.resolution.nli_adjudicator import NLIResult
from apollo.resolver_v2 import incremental
from apollo.resolver_v2.config import ResolverV2Params
from apollo.resolver_v2.engine import run_resolver_v2
from apollo.resolver_v2.incremental_types import IncrementalState
from apollo.resolver_v2.types import RefNode, SelectFn, Window

_PARAMS = ResolverV2Params()

_TURN_0 = "The pipe narrows and water speeds up."
_TURN_1 = "Mass is conserved in the pipe."
_TURN_2 = "The flow rate is equal at both sections."

# Labels chosen to equal a transcript turn verbatim so the REAL lexical
# prefilter (`prefilter.select_windows`) gives them a near-1.0 lexical score
# against their own turn -- letting an NLI-entailment override cross the
# t_high tier (fused = alpha*entail + (1-alpha)*lex needs lex high too).
_LABEL_A = _TURN_0
_LABEL_B = _TURN_2


def _empty_state() -> IncrementalState:
    return IncrementalState(
        window_cursor=0,
        global_window_count=0,
        running_node_max={},
        node_source={},
        running_edge_evidence={},
        seeded_keys=frozenset(),
        pair_count_total=0,
    )


def _reference(with_edge: bool = True) -> ReferenceGraph:
    return ReferenceGraph(
        nodes=(
            CanonicalNode(
                canonical_key="concept.a",
                node_type="definition",
                source_node_ids=("r1",),
                evidence_spans=(),
            ),
            CanonicalNode(
                canonical_key="concept.b",
                node_type="definition",
                source_node_ids=("r2",),
                evidence_spans=(),
            ),
        ),
        edges=(
            (
                CanonicalEdge(
                    edge_type=EdgeType.USES,
                    from_key="concept.a",
                    to_key="concept.b",
                    provenance="explicit",
                ),
            )
            if with_edge
            else ()
        ),
        paths=(ReferencePathView(canonical_keys=("concept.a", "concept.b")),),
    )


def _payload() -> dict:
    # concept_id/id NOT in the committed views cache -> label-only views (the
    # documented degrade), so batch's build_ref_nodes matches _ref_nodes().
    return {
        "concept_id": "t3_incremental_test",
        "id": "t3_problem",
        "reference_solution": [
            {"id": "s1", "entity_key": "concept.a", "content": {"label": _LABEL_A}},
            {"id": "s2", "entity_key": "concept.b", "content": {"label": _LABEL_B}},
        ],
    }


def _ref_nodes() -> tuple[RefNode, ...]:
    return (
        RefNode(canonical_key="concept.a", node_type="definition", label=_LABEL_A, views=(_LABEL_A,)),
        RefNode(canonical_key="concept.b", node_type="definition", label=_LABEL_B, views=(_LABEL_B,)),
    )


def _nli(entailment: float, contradiction: float = 0.0) -> NLIResult:
    neutral = max(0.0, 1.0 - entailment - contradiction)
    label = max(
        ("entailment", entailment),
        ("neutral", neutral),
        ("contradiction", contradiction),
        key=lambda kv: kv[1],
    )[0]
    return NLIResult(label, entailment, contradiction, neutral, "fake-nli")


class PermissiveFakeNLI:
    """Stub adjudicator: low-signal default, exact-pair overrides, call count."""

    def __init__(self, overrides: dict[tuple[str, str], NLIResult]):
        self._overrides = overrides
        self.calls = 0

    def classify(self, premise: str, hypothesis: str) -> NLIResult:
        self.calls += 1
        return self._overrides.get((premise, hypothesis), _nli(0.05))


def _lexical_select() -> SelectFn:
    """The real T3 lexical prefilter -- deterministic, and gives an exact
    transcript sentence a high (near-1.0) lexical score against its own
    label so NLI-override fixtures can cross the t_high tier."""
    from apollo.resolver_v2.prefilter import select_windows

    return select_windows


# ---------------------------------------------------------------------------
# 1. Incremental == batch on the pure-ladder monotone fixture
# ---------------------------------------------------------------------------


def test_incremental_equals_batch_on_pure_ladder_fixture():
    turns = (_TURN_0, _TURN_1, _TURN_2)
    overrides = {
        (_TURN_0, _LABEL_A): _nli(0.95),
        (_TURN_1, _LABEL_A): _nli(0.95),
        (_TURN_2, _LABEL_B): _nli(0.95),
    }
    select_fn = _lexical_select()
    reference = _reference(with_edge=True)
    ref_nodes = _ref_nodes()

    batch = run_resolver_v2(
        student_turns=turns,
        reference_graph=reference,
        problem_payload=_payload(),
        v1_resolved_keys=frozenset(),
        v1_explicit_triples=frozenset(),  # pure ladder: no v1 edge floors
        v1_inferred_triples=frozenset(),
        nli=PermissiveFakeNLI(overrides),
        grayzone_fn=None,  # pure ladder: no grayzone upgrade
        params=_PARAMS,
        select_fn=select_fn,
    )

    state = _empty_state()
    nli = PermissiveFakeNLI(overrides)
    snapshot = None
    for cursor in range(1, len(turns) + 1):
        state, snapshot = incremental.score_turn(
            state,
            all_student_turns=turns[:cursor],
            reference_graph=reference,
            problem_payload={},
            v1_resolved_keys=frozenset(),
            nli=nli,
            grayzone_fn=None,
            select_fn=select_fn,
            params=_PARAMS,
            ref_nodes=ref_nodes,
        )

    assert snapshot.node_credits == {
        n.canonical_key: pytest.approx(batch_credit)
        for n, batch_credit in zip(
            ref_nodes, (nc.credit for nc in _by_key(batch.node_scores, ref_nodes))
        )
    }
    assert snapshot.node_cov == pytest.approx(batch.node_coverage)
    assert snapshot.edge_cov == pytest.approx(batch.edge_coverage)
    assert snapshot.winning_path_index == batch.winning_path_index
    batch_edge_by_key = {(e.edge_type, e.from_key, e.to_key): e for e in batch.edge_scores}
    for edge in snapshot.edge_scores:
        b = batch_edge_by_key[(edge.edge_type, edge.from_key, edge.to_key)]
        assert edge.credit == pytest.approx(b.credit)
        assert edge.relation_evidence == b.relation_evidence


def _by_key(node_scores, ref_nodes):
    by_key = {n.canonical_key: n for n in node_scores}
    return [by_key[n.canonical_key] for n in ref_nodes]


# ---------------------------------------------------------------------------
# 2. Conservative incremental_cov <= batch_cov on the general fixture
# ---------------------------------------------------------------------------


def test_incremental_is_conservative_lower_bound_on_general_fixture():
    turns = (_TURN_0, _TURN_1, _TURN_2)
    overrides = {
        (_TURN_0, _LABEL_A): _nli(0.95),
        (_TURN_2, _LABEL_B): _nli(0.5),  # gray band without grayzone
    }
    select_fn = _lexical_select()
    reference = _reference(with_edge=True)
    ref_nodes = _ref_nodes()

    def grayzone_fn(queries, transcript):
        from apollo.resolver_v2.grayzone import GrayzoneVerdict

        return tuple(
            GrayzoneVerdict(
                canonical_key=q.canonical_key,
                taught=True,
                quote="The flow rate is equal at both sections",
                verified=True,
            )
            for q in queries
        )

    batch = run_resolver_v2(
        student_turns=turns,
        reference_graph=reference,
        problem_payload=_payload(),
        v1_resolved_keys=frozenset(),
        v1_explicit_triples=frozenset({("USES", "concept.a", "concept.b")}),
        v1_inferred_triples=frozenset(),
        nli=PermissiveFakeNLI(overrides),
        grayzone_fn=grayzone_fn,  # batch runs grayzone -> concept.b upgraded
        params=_PARAMS,
        select_fn=select_fn,
    )

    state = _empty_state()
    nli = PermissiveFakeNLI(overrides)
    snapshot = None
    for cursor in range(1, len(turns) + 1):
        state, snapshot = incremental.score_turn(
            state,
            all_student_turns=turns[:cursor],
            reference_graph=reference,
            problem_payload={},
            v1_resolved_keys=frozenset(),
            nli=nli,
            grayzone_fn=None,  # hot path: no per-turn LLM call (conservative)
            select_fn=select_fn,
            params=_PARAMS,
            ref_nodes=ref_nodes,
            v1_explicit_triples=frozenset(),  # A-MAJOR-4: omitted incrementally
        )

    assert snapshot.node_cov <= batch.node_coverage + 1e-9
    assert snapshot.edge_cov <= batch.edge_coverage + 1e-9
    for n in ref_nodes:
        assert snapshot.node_credits[n.canonical_key] <= (
            _by_key(batch.node_scores, [n])[0].credit + 1e-9
        )


# ---------------------------------------------------------------------------
# 3. Global window index/turn_index continuity (A-MAJOR-2)
# ---------------------------------------------------------------------------


def test_global_window_and_turn_index_continue_across_turns():
    turns = (_TURN_0, _TURN_1, _TURN_2)
    select_fn = _lexical_select()
    reference = _reference(with_edge=False)
    ref_nodes = _ref_nodes()
    nli = PermissiveFakeNLI({})

    state = _empty_state()
    seen_global_counts = []
    for cursor in range(1, len(turns) + 1):
        prior_cursor = state.window_cursor
        state, _snapshot = incremental.score_turn(
            state,
            all_student_turns=turns[:cursor],
            reference_graph=reference,
            problem_payload={},
            v1_resolved_keys=frozenset(),
            nli=nli,
            grayzone_fn=None,
            select_fn=select_fn,
            params=_PARAMS,
            ref_nodes=ref_nodes,
        )
        assert state.window_cursor == cursor
        assert prior_cursor < state.window_cursor
        seen_global_counts.append(state.global_window_count)

    # One window per turn in this fixture (single short sentence each) -> the
    # running global_window_count strictly increases and matches a from-
    # scratch build over the full transcript.
    from apollo.resolver_v2.windows import build_windows

    full = build_windows(turns, _PARAMS)
    assert seen_global_counts[-1] == len(full)
    assert seen_global_counts == sorted(seen_global_counts)
    assert seen_global_counts == list(range(1, len(turns) + 1))


def test_offset_windows_rewrites_index_and_turn_index():
    local = (
        Window(index=0, turn_index=0, text="a"),
        Window(index=1, turn_index=0, text="b"),
        Window(index=2, turn_index=1, text="c"),
    )
    offset = incremental._offset_windows(local, global_window_count=5, turn_offset=3)
    assert [w.index for w in offset] == [5, 6, 7]
    assert [w.turn_index for w in offset] == [3, 3, 4]
    assert [w.text for w in offset] == ["a", "b", "c"]


# ---------------------------------------------------------------------------
# 4. Edge coverage monotone non-decreasing across turns
# ---------------------------------------------------------------------------


def test_edge_coverage_is_monotone_non_decreasing():
    turns = (_TURN_0, _TURN_1, _TURN_2)
    overrides = {
        (_TURN_0, _LABEL_A): _nli(0.95),
        (_TURN_2, _LABEL_B): _nli(0.95),
        (_TURN_2, f"{_LABEL_A} uses {_LABEL_B}."): _nli(0.9),
    }
    select_fn = _lexical_select()
    reference = _reference(with_edge=True)
    ref_nodes = _ref_nodes()
    nli = PermissiveFakeNLI(overrides)

    state = _empty_state()
    edge_covs = []
    for cursor in range(1, len(turns) + 1):
        state, snapshot = incremental.score_turn(
            state,
            all_student_turns=turns[:cursor],
            reference_graph=reference,
            problem_payload={},
            v1_resolved_keys=frozenset(),
            nli=nli,
            grayzone_fn=None,
            select_fn=select_fn,
            params=_PARAMS,
            ref_nodes=ref_nodes,
        )
        edge_covs.append(snapshot.edge_cov)

    assert edge_covs == sorted(edge_covs)


# ---------------------------------------------------------------------------
# 5. Unresolved-only rescoring: a resolved node spends 0 pairs later
# ---------------------------------------------------------------------------


def test_resolved_node_spends_zero_pairs_on_later_turns():
    turns = (_TURN_0, _TURN_1)
    # Both labels equal the FIRST turn verbatim (real lexical prefilter ->
    # lex ~1.0) so both nodes fully resolve off turn 1 alone.
    ref_nodes = (
        RefNode(canonical_key="concept.a", node_type="definition", label=_TURN_0, views=(_TURN_0,)),
        RefNode(canonical_key="concept.b", node_type="definition", label=_TURN_0, views=(_TURN_0,)),
    )
    overrides = {
        (_TURN_0, _TURN_0): _nli(0.95),
    }
    select_fn = _lexical_select()
    reference = _reference(with_edge=False)  # isolate node pairs only
    nli = PermissiveFakeNLI(overrides)

    state = _empty_state()
    state, snapshot_1 = incremental.score_turn(
        state,
        all_student_turns=turns[:1],
        reference_graph=reference,
        problem_payload={},
        v1_resolved_keys=frozenset(),
        nli=nli,
        grayzone_fn=None,
        select_fn=select_fn,
        params=_PARAMS,
        ref_nodes=ref_nodes,
    )
    # Both nodes resolved to full credit on turn 1.
    assert snapshot_1.node_credits["concept.a"] == pytest.approx(1.0)
    assert snapshot_1.node_credits["concept.b"] == pytest.approx(1.0)

    calls_before = nli.calls
    state, snapshot_2 = incremental.score_turn(
        state,
        all_student_turns=turns,
        reference_graph=reference,
        problem_payload={},
        v1_resolved_keys=frozenset(),
        nli=nli,
        grayzone_fn=None,
        select_fn=select_fn,
        params=_PARAMS,
        ref_nodes=ref_nodes,
    )
    assert snapshot_2.pair_count_this_turn == 0
    assert nli.calls == calls_before  # no new NLI calls spent on resolved nodes


# ---------------------------------------------------------------------------
# 6. Budget truncation sets budget_truncated
# ---------------------------------------------------------------------------


def test_budget_truncation_sets_flag():
    turns = (_TURN_0, _TURN_1)
    select_fn = _lexical_select()
    reference = _reference(with_edge=False)
    ref_nodes = _ref_nodes()
    nli = PermissiveFakeNLI({})  # everything low-signal -> both nodes stay unresolved
    tiny_budget = replace(_PARAMS, max_nli_pairs=1)

    state = _empty_state()
    state, snapshot = incremental.score_turn(
        state,
        all_student_turns=turns,
        reference_graph=reference,
        problem_payload={},
        v1_resolved_keys=frozenset(),
        nli=nli,
        grayzone_fn=None,
        select_fn=select_fn,
        params=tiny_budget,
        ref_nodes=ref_nodes,
    )
    assert snapshot.budget_truncated is True
    assert snapshot.pair_count_this_turn <= 1
    assert state.pair_count_total <= 1


def test_no_budget_truncation_when_plenty_of_budget():
    turns = (_TURN_0,)
    select_fn = _lexical_select()
    reference = _reference(with_edge=False)
    ref_nodes = _ref_nodes()
    nli = PermissiveFakeNLI({(_TURN_0, _LABEL_A): _nli(0.95)})

    state = _empty_state()
    state, snapshot = incremental.score_turn(
        state,
        all_student_turns=turns,
        reference_graph=reference,
        problem_payload={},
        v1_resolved_keys=frozenset(),
        nli=nli,
        grayzone_fn=None,
        select_fn=select_fn,
        params=_PARAMS,
        ref_nodes=ref_nodes,
    )
    assert snapshot.budget_truncated is False


# ---------------------------------------------------------------------------
# 7. Overlap seam correctness -- no window invented/duplicated at the seam
# ---------------------------------------------------------------------------


def test_overlap_seam_matches_batch_window_texts():
    """Windows never cross a turn boundary (windows.py), so incrementally
    windowing only the NEW suffix turns must reproduce exactly the same
    window texts (and the same total count) as a from-scratch batch build
    over the full transcript -- no window is invented, dropped, or
    duplicated at the turn seam."""
    from apollo.resolver_v2.windows import build_windows

    turns = (_TURN_0, _TURN_1, _TURN_2)
    select_fn = _lexical_select()
    reference = _reference(with_edge=False)
    ref_nodes = _ref_nodes()
    nli = PermissiveFakeNLI({})

    state = _empty_state()
    all_texts: list[str] = []
    for cursor in range(1, len(turns) + 1):
        new_turns = turns[state.window_cursor : cursor]
        local = build_windows(list(new_turns), _PARAMS)
        offset = incremental._offset_windows(
            local, global_window_count=state.global_window_count, turn_offset=state.window_cursor
        )
        all_texts.extend(w.text for w in offset)
        state, _snapshot = incremental.score_turn(
            state,
            all_student_turns=turns[:cursor],
            reference_graph=reference,
            problem_payload={},
            v1_resolved_keys=frozenset(),
            nli=nli,
            grayzone_fn=None,
            select_fn=select_fn,
            params=_PARAMS,
            ref_nodes=ref_nodes,
        )

    full = build_windows(turns, _PARAMS)
    assert all_texts == [w.text for w in full]
