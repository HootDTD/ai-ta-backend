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
    # §6.9 declares the per-problem d=2r mapping; the resolver applies it only
    # when the problem table supplies it (no global default — Defect C).
    result = resolve_attempt(
        _worked_example_graph(), _worked_example_candidates(),
        symbolic_mappings={"d": "2*r"},
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
        _worked_example_graph(), _worked_example_candidates(),
        symbolic_mappings={"d": "2*r"},
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
    maps = {"d": "2*r"}
    a = resolve_attempt(g, c, symbolic_mappings=maps)
    b = resolve_attempt(g, c, symbolic_mappings=maps)
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


# ---------------------------------------------------------------------------
# DEFECT A (§6.11) — a polar near-miss resolves to the misconception ON THE
# WIRED resolve_attempt PATH, even when a lexically-CLOSER reference is the
# global-best fuzzy hit. This is the genuine §6.11 proof the unit tests missed
# (the unit competition test fed hand-built ScoredMatches; this drives the real
# tier->competition pipeline).
# ---------------------------------------------------------------------------

def test_polar_near_miss_resolves_to_misconception_through_resolve_attempt():
    """Student states the WRONG polar claim. The reference alias is lexically
    CLOSER (higher raw token_set_ratio) than the misconception alias, but the
    misconception is within the 0.05 competition margin -> it WINS, and the
    polarity screen does NOT false-reject the misconception."""
    # ref raw ~0.91, misc raw ~0.90 -> within margin; misconception must win.
    student = "faster flow gives higher pressure here"
    graph = KGGraph(nodes=[
        _node("s1", "definition",
              {"concept": "pressure", "meaning": student}),
    ])
    cands = (
        _cand("def.pressure_velocity_tradeoff", node_type="definition",
              aliases=("faster flow gives lower pressure here",)),
        _cand("misc.pressure_velocity_same_direction", node_type="definition",
              is_misc=True, aliases=("faster flow means higher pressure",),
              opposes="def.pressure_velocity_tradeoff"),
    )
    result = resolve_attempt(graph, cands)
    rn = result.resolved[0]
    assert rn.resolved_key == "misc.pressure_velocity_same_direction"
    assert rn.method == "fuzzy"
    assert rn.confidence == METHOD_CONFIDENCE_CAP["fuzzy"]


def test_polarity_screen_only_rejects_the_winning_alias_not_unrelated_aliases():
    """A valid same-direction reference match is KEPT even when the SAME
    candidate carries an UNRELATED, direction-inverted alias. Screening every
    alias (old bug) false-rejected the match; per-winning-alias screening keeps
    it."""
    # Student phrase aligns (same direction) with the FIRST alias; the candidate
    # also carries an unrelated alias with the opposite direction word.
    student = "the pressure drops at the narrow section of pipe"
    graph = KGGraph(nodes=[
        _node("s1", "definition", {"concept": "pressure", "meaning": student}),
    ])
    cand = _cand(
        "def.pressure_velocity_tradeoff", node_type="definition",
        aliases=(
            "the pressure drops at the narrow section of pipe",  # winning, same dir
            "temperature increases downstream",                  # unrelated, inverted word
        ),
    )
    result = resolve_attempt(graph, (cand,))
    rn = result.resolved[0]
    assert rn.resolved_key == "def.pressure_velocity_tradeoff"
    assert rn.method in ("alias", "fuzzy")


# ---------------------------------------------------------------------------
# DEFECT C (§5) — symbolic mappings are per-problem declared data, NOT a global
# default. With NO mappings passed, a d=2r-dependent equivalence must NOT match;
# with the per-problem mapping passed explicitly, the §6.9 case DOES match.
# ---------------------------------------------------------------------------

def test_default_no_symbolic_mapping_does_not_false_match_diameter_cube():
    """resolve_attempt with the DEFAULT (no symbolic_mappings) must NOT treat
    'V = d**3' as equivalent to 'V = 8*r**3' — the global d=2r default that
    produced that false symbolic match is gone."""
    graph = KGGraph(nodes=[
        _node("v1", "equation", {"symbolic": "V = d**3", "label": "", "variables": []}),
    ])
    cands = (_cand("eq.sphere_ish", node_type="equation", symbolic="V = 8*r**3"),)
    result = resolve_attempt(graph, cands)
    rn = result.resolved[0]
    # No mapping -> d and r are independent symbols -> not equivalent -> unresolved.
    assert rn.resolution == "unresolved"
    assert rn.resolved_key is None


def test_explicit_per_problem_mapping_resolves_circular_area():
    """With the per-problem mapping {'d': '2*r'} passed explicitly, the §6.9
    circular-area case (A = pi*r**2 <-> A = pi*d**2/4) resolves via symbolic."""
    graph = KGGraph(nodes=[
        _node("a1", "equation", {"symbolic": "A = pi*r**2", "label": "", "variables": []}),
    ])
    cands = (_cand("eq.circular_area", node_type="equation", symbolic="A = pi*d**2/4"),)
    result = resolve_attempt(graph, cands, symbolic_mappings={"d": "2*r"})
    rn = result.resolved[0]
    assert rn.resolved_key == "eq.circular_area"
    assert rn.method == "symbolic"


def test_default_no_mapping_circular_area_stays_unresolved():
    """Without the explicit mapping the circular-area pair does NOT symbolically
    match (d/r independent) — confirms the §6.9 match depended on the per-problem
    mapping, not a global default."""
    graph = KGGraph(nodes=[
        _node("a1", "equation", {"symbolic": "A = pi*r**2", "label": "", "variables": []}),
    ])
    cands = (_cand("eq.circular_area", node_type="equation", symbolic="A = pi*d**2/4"),)
    result = resolve_attempt(graph, cands)
    assert result.resolved[0].resolution == "unresolved"


def test_worked_example_6_9_requires_explicit_mapping_for_area():
    """The §6.9 worked example resolves end-to-end ONLY when the per-problem
    d=2r mapping is supplied; the circular-area node is the mapping-dependent
    one."""
    result = resolve_attempt(
        _worked_example_graph(), _worked_example_candidates(),
        symbolic_mappings={"d": "2*r"},
    )
    keyed = {rn.node_id: rn.resolved_key for rn in result.resolved}
    assert keyed["d1"] == "cond.incompressibility"
    assert keyed["a1"] == "eq.circular_area"
    assert keyed["b1"] == "eq.bernoulli"
    assert keyed["p1"] == "proc.compute_v2"
    assert keyed["t1"] == "def.pressure_velocity_tradeoff"
    assert result.llm_calls == 0


def test_tier_counts_pinned_on_real_6_9_output():
    """NIT (test-honesty): pin _histogram's REAL output on the §6.9 graph — the
    per-method histogram from actual resolution, not a hand-built dict."""
    result = resolve_attempt(
        _worked_example_graph(), _worked_example_candidates(),
        symbolic_mappings={"d": "2*r"},
    )
    # Real per-method histogram (pinned, not hand-built): d1 alias, b1 alias,
    # p1 alias (3 exact-alias hits), a1 symbolic (d=2r), t1 fuzzy (the
    # definition's concept+meaning surface paraphrases the bare alias).
    assert dict(result.tier_counts) == {"alias": 3, "symbolic": 1, "fuzzy": 1}
