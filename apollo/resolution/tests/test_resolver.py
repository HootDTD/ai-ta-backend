"""WU-3C2 Step 9 — pure-unit tests for resolve_attempt orchestration (mocked LLM).

No live OpenAI call ever fires: the §6.9 worked example resolves on content
tiers alone (llm_calls == 0); the LLM-needing tests inject a deterministic
``llm_adjudicator`` stub. Pin (§5):
- the §6.9 worked example end-to-end (the five mappings);
- each resolved node's confidence == METHOD_CONFIDENCE_CAP[method];
- a below-threshold node -> unresolved / 0.0 / no edge (DATA, no exception);
- result.llm_calls <= 1; one LLM call when adjudication is needed;
- the resolver is pure (same input -> same output);
- over the cap the whole attempt abstains (llm_calls == 0);
- an empty candidate set -> every node unresolved, no edges.
"""

from __future__ import annotations

from apollo.ontology.nodes import build_node
from apollo.resolution import resolve_attempt
from apollo.resolution.candidates import METHOD_CONFIDENCE_CAP, Candidate
from apollo.ontology.graph import KGGraph


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _cand(key, *, node_type, symbolic=None, aliases=(), is_misc=False, opposes=None, canon=None):
    return Candidate(
        canonical_key=key,
        canon_key=canon if canon is not None else (abs(hash(key)) % 1000) + 1,
        node_type=node_type,
        is_misconception=is_misc,
        symbolic=symbolic,
        aliases=tuple(aliases),
        display_name=key,
        opposes_key=opposes,
    )


def _node(node_id, node_type, content):
    return build_node(
        node_type=node_type, node_id=node_id, attempt_id=1, source="parser", content=content
    )


def _worked_example_candidates():
    """The §6.9 candidate set: bernoulli reference + authored def + misc."""
    return (
        _cand("cond.incompressibility", node_type="condition",
              aliases=("density is constant",), canon=2),
        _cand("eq.circular_area", node_type="equation", symbolic="A = pi*d**2/4", canon=20),
        _cand("eq.bernoulli", node_type="equation",
              aliases=("use bernoulli",), symbolic="P1 = P2", canon=3),
        _cand("proc.compute_v2", node_type="procedure_step",
              aliases=("four times the speed",), canon=30),
        _cand("def.pressure_velocity_tradeoff", node_type="definition",
              aliases=("pressure is lower at the narrow part",), canon=8),
        _cand("misc.pressure_velocity_same_direction", node_type="definition",
              is_misc=True, aliases=("faster flow means higher pressure",),
              opposes="def.pressure_velocity_tradeoff", canon=9),
    )


def _worked_example_graph():
    return KGGraph(nodes=[
        _node("d1", "condition", {"applies_when": "density is constant", "label": ""}),
        _node("a1", "equation", {"symbolic": "A = pi*r**2", "label": "", "variables": []}),
        _node("b1", "equation", {"symbolic": "use bernoulli", "label": "", "variables": []}),
        _node("p1", "procedure_step", {"action": "four times the speed", "purpose": ""}),
        _node("t1", "definition",
              {"concept": "narrow part", "meaning": "pressure is lower at the narrow part"}),
    ])


def test_worked_example_6_9_end_to_end():
    result = resolve_attempt(
        _worked_example_graph(), _worked_example_candidates()
    )
    keyed = {rn.node_id: rn.resolved_key for rn in result.resolved}
    assert keyed["d1"] == "cond.incompressibility"
    assert keyed["a1"] == "eq.circular_area"
    assert keyed["b1"] == "eq.bernoulli"
    assert keyed["p1"] == "proc.compute_v2"
    assert keyed["t1"] == "def.pressure_velocity_tradeoff"
    assert result.llm_calls == 0  # content tiers alone resolve it


def test_confidence_equals_method_cap_per_node():
    result = resolve_attempt(
        _worked_example_graph(), _worked_example_candidates()
    )
    for rn in result.resolved:
        if rn.resolution == "resolved":
            assert rn.confidence == METHOD_CONFIDENCE_CAP[rn.method]


def test_below_threshold_node_is_unresolved_data_no_edge():
    graph = KGGraph(nodes=[
        _node("v1", "condition", {"applies_when": "the pipe is painted red", "label": ""}),
    ])
    cands = (_cand("cond.incompressibility", node_type="condition",
                   aliases=("density is constant",)),)
    result = resolve_attempt(graph, cands)
    rn = result.resolved[0]
    assert rn.resolution == "unresolved"
    assert rn.method == "unresolved"
    assert rn.confidence == 0.0
    assert result.resolved_edges() == ()


def test_result_llm_calls_at_most_one():
    """A node only an LLM can place triggers exactly one adjudication call."""
    graph = KGGraph(nodes=[
        _node("x1", "condition", {"applies_when": "an utterly ambiguous phrase zzz", "label": ""}),
    ])
    cands = (_cand("cond.incompressibility", node_type="condition", aliases=()),)

    def _stub(request):
        return {"x1": "cond.incompressibility"}

    result = resolve_attempt(graph, cands, llm_adjudicator=_stub)
    assert result.llm_calls == 1
    assert result.resolved[0].method == "llm"
    assert result.resolved[0].confidence == 0.75


def test_resolver_is_pure_same_input_same_output():
    g = _worked_example_graph()
    c = _worked_example_candidates()
    a = resolve_attempt(g, c)
    b = resolve_attempt(g, c)
    assert [(r.node_id, r.resolved_key, r.method, r.confidence) for r in a.resolved] == \
           [(r.node_id, r.resolved_key, r.method, r.confidence) for r in b.resolved]


def test_resolver_over_cap_abstains():
    graph = KGGraph(nodes=[
        _node(f"s{i}", "condition", {"applies_when": "density is constant", "label": ""})
        for i in range(151)
    ])
    cands = (_cand("cond.incompressibility", node_type="condition",
                   aliases=("density is constant",)),)
    result = resolve_attempt(graph, cands)
    assert all(r.resolution == "unresolved" for r in result.resolved)
    assert result.llm_calls == 0


def test_empty_candidate_set_all_unresolved():
    result = resolve_attempt(_worked_example_graph(), ())
    assert all(r.resolution == "unresolved" for r in result.resolved)
    assert result.resolved_edges() == ()


def test_exact_tier_node_resolves_with_exact_method_and_cap():
    """A node whose label equals a candidate key resolves via the EXACT tier
    (method 'exact', confidence 1.0)."""
    graph = KGGraph(nodes=[
        _node("c1", "condition",
              {"applies_when": "density is constant",
               "label": "cond.incompressibility"}),
    ])
    cands = (_cand("cond.incompressibility", node_type="condition"),)
    result = resolve_attempt(graph, cands)
    rn = result.resolved[0]
    assert rn.method == "exact"
    assert rn.confidence == 1.0
    assert rn.resolved_key == "cond.incompressibility"


def test_unprojected_candidate_resolves_without_canon_key():
    """A resolved node whose candidate has no :Canon key (canon_key < 0) is
    'resolved' but emits NO edge (resolved_canon_key None)."""
    graph = KGGraph(nodes=[
        _node("c1", "condition", {"applies_when": "density is constant", "label": ""}),
    ])
    cand = _cand("cond.x", node_type="condition",
                 aliases=("density is constant",), canon=-1)
    result = resolve_attempt(graph, (cand,))
    rn = result.resolved[0]
    assert rn.resolution == "resolved"
    assert rn.resolved_canon_key is None
    assert result.resolved_edges() == ()
