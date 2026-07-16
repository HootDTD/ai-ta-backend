"""WU-3C2 Step 9 — pure-unit tests for resolve_attempt orchestration.

No live OpenAI call ever fires: the §6.9 worked example resolves on content
tiers alone (llm_calls == 0). The LLM adjudication path has been REMOVED
(Task 4): unmatched post-tier nodes stay unresolved, llm_calls is always 0.
Pin (§5):
- the §6.9 worked example end-to-end (the five mappings);
- each resolved node's confidence == METHOD_CONFIDENCE_CAP[method];
- a below-threshold node -> unresolved / 0.0 / no edge (DATA, no exception);
- the resolver is pure (same input -> same output);
- over the cap the whole attempt abstains (llm_calls == 0);
- an empty candidate set -> every node unresolved, no edges.
"""

from __future__ import annotations

import inspect

from apollo.grading.abstention import unresolved_rate_of
from apollo.ontology.graph import KGGraph
from apollo.ontology.nodes import build_node
from apollo.resolution import resolve_attempt, resolver
from apollo.resolution.candidates import METHOD_CONFIDENCE_CAP, Candidate
from apollo.resolution.competition import (
    apply_misconception_competition,
    polarity_screen,
)
from apollo.resolution.resolver import _content_match
from apollo.resolution.structural import ScoredMatch
from apollo.resolution.tiers import (
    match_alias_all,
    match_fuzzy,
    match_fuzzy_all,
    student_surface_text,
)

# ---------------------------------------------------------------------------
# Task 4 guard tests — must fail RED until the adjudicator is removed.
# ---------------------------------------------------------------------------


def test_resolver_has_no_llm_adjudicator():
    sig = inspect.signature(resolver.resolve_attempt)
    assert "llm_adjudicator" not in sig.parameters


def test_unmatched_node_stays_unresolved_no_llm():
    graph = _graph(
        [_node("s1", "condition", {"applies_when": "totally unrelated prose", "label": ""})]
    )
    cands = _cands_with("cond.bernoulli", node_type="condition")
    result = resolve_attempt(graph, cands)
    rn = {r.node_id: r for r in result.resolved}["s1"]
    assert rn.resolution == "unresolved"
    assert result.llm_calls == 0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _cand(
    key,
    *,
    node_type,
    symbolic=None,
    aliases=(),
    is_misc=False,
    opposes=None,
    canon=None,
    exact_aliases=(),
):
    return Candidate(
        canonical_key=key,
        canon_key=canon if canon is not None else (abs(hash(key)) % 1000) + 1,
        node_type=node_type,
        is_misconception=is_misc,
        symbolic=symbolic,
        aliases=tuple(aliases),
        display_name=key,
        opposes_key=opposes,
        exact_aliases=tuple(exact_aliases),
    )


def _node(node_id, node_type, content):
    return build_node(
        node_type=node_type, node_id=node_id, attempt_id=1, source="parser", content=content
    )


def _graph(nodes):
    """Wrap a list of nodes in a KGGraph (shorthand for confirmed_resolution tests)."""
    return KGGraph(nodes=nodes)


def _cands_with(key, *, node_type, aliases=()):
    """Single-candidate tuple with the given key, node_type, and optional aliases."""
    return (_cand(key, node_type=node_type, aliases=aliases),)


def _worked_example_candidates():
    """The §6.9 candidate set: bernoulli reference + authored def + misc."""
    return (
        _cand(
            "cond.incompressibility",
            node_type="condition",
            aliases=("density is constant",),
            canon=2,
        ),
        _cand("eq.circular_area", node_type="equation", symbolic="A = pi*d**2/4", canon=20),
        _cand(
            "eq.bernoulli",
            node_type="equation",
            aliases=("use bernoulli",),
            symbolic="P1 = P2",
            canon=3,
        ),
        _cand(
            "proc.compute_v2",
            node_type="procedure_step",
            aliases=("four times the speed",),
            canon=30,
        ),
        _cand(
            "def.pressure_velocity_tradeoff",
            node_type="definition",
            aliases=("pressure is lower at the narrow part",),
            canon=8,
        ),
        _cand(
            "misc.pressure_velocity_same_direction",
            node_type="definition",
            is_misc=True,
            aliases=("faster flow means higher pressure",),
            opposes="def.pressure_velocity_tradeoff",
            canon=9,
        ),
    )


def _worked_example_graph():
    return KGGraph(
        nodes=[
            _node("d1", "condition", {"applies_when": "density is constant", "label": ""}),
            _node("a1", "equation", {"symbolic": "A = pi*r**2", "label": "", "variables": []}),
            _node("b1", "equation", {"symbolic": "use bernoulli", "label": "", "variables": []}),
            _node("p1", "procedure_step", {"action": "four times the speed", "purpose": ""}),
            _node(
                "t1",
                "definition",
                {"concept": "narrow part", "meaning": "pressure is lower at the narrow part"},
            ),
        ]
    )


def test_worked_example_6_9_end_to_end():
    # §6.9 declares the per-problem d=2r mapping; the resolver applies it only
    # when the problem table supplies it (no global default — Defect C).
    result = resolve_attempt(
        _worked_example_graph(),
        _worked_example_candidates(),
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
        _worked_example_graph(),
        _worked_example_candidates(),
        symbolic_mappings={"d": "2*r"},
    )
    for rn in result.resolved:
        if rn.resolution == "resolved":
            assert rn.confidence == METHOD_CONFIDENCE_CAP[rn.method]


def test_below_threshold_node_is_unresolved_data_no_edge():
    graph = KGGraph(
        nodes=[
            _node("v1", "condition", {"applies_when": "the pipe is painted red", "label": ""}),
        ]
    )
    cands = (
        _cand("cond.incompressibility", node_type="condition", aliases=("density is constant",)),
    )
    result = resolve_attempt(graph, cands)
    rn = result.resolved[0]
    assert rn.resolution == "unresolved"
    assert rn.method == "unresolved"
    assert rn.confidence == 0.0
    assert result.resolved_edges() == ()


def test_result_llm_calls_always_zero():
    """Without an LLM adjudicator, a node that no content tier can place stays
    unresolved and llm_calls is always 0 (the silent adjudication path is gone)."""
    graph = KGGraph(
        nodes=[
            _node(
                "x1", "condition", {"applies_when": "an utterly ambiguous phrase zzz", "label": ""}
            ),
        ]
    )
    cands = (_cand("cond.incompressibility", node_type="condition", aliases=()),)

    result = resolve_attempt(graph, cands)
    assert result.llm_calls == 0
    assert result.resolved[0].resolution == "unresolved"


def test_resolver_is_pure_same_input_same_output():
    g = _worked_example_graph()
    c = _worked_example_candidates()
    maps = {"d": "2*r"}
    a = resolve_attempt(g, c, symbolic_mappings=maps)
    b = resolve_attempt(g, c, symbolic_mappings=maps)
    assert [(r.node_id, r.resolved_key, r.method, r.confidence) for r in a.resolved] == [
        (r.node_id, r.resolved_key, r.method, r.confidence) for r in b.resolved
    ]


def test_resolver_over_cap_abstains():
    graph = KGGraph(
        nodes=[
            _node(f"s{i}", "condition", {"applies_when": "density is constant", "label": ""})
            for i in range(151)
        ]
    )
    cands = (
        _cand("cond.incompressibility", node_type="condition", aliases=("density is constant",)),
    )
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
    graph = KGGraph(
        nodes=[
            _node(
                "c1",
                "condition",
                {"applies_when": "density is constant", "label": "cond.incompressibility"},
            ),
        ]
    )
    cands = (_cand("cond.incompressibility", node_type="condition"),)
    result = resolve_attempt(graph, cands)
    rn = result.resolved[0]
    assert rn.method == "exact"
    assert rn.confidence == 1.0
    assert rn.resolved_key == "cond.incompressibility"


def test_unprojected_candidate_resolves_without_canon_key():
    """A resolved node whose candidate has no :Canon key (canon_key < 0) is
    'resolved' but emits NO edge (resolved_canon_key None)."""
    graph = KGGraph(
        nodes=[
            _node("c1", "condition", {"applies_when": "density is constant", "label": ""}),
        ]
    )
    cand = _cand("cond.x", node_type="condition", aliases=("density is constant",), canon=-1)
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
    """The genuine §6.11 out-competition proof: BOTH the reference and the
    misconception SURVIVE the polarity screen and ENTER competition; the
    reference is the lexically-CLOSER fuzzy hit (higher raw token_set_ratio) yet
    a within-0.05-margin misconception STILL WINS via the wired resolve_attempt.

    This exercises ``apply_misconception_competition``'s
    ``best_misc.score + _MISCONCEPTION_MARGIN >= best_non_misc_score`` branch
    with a SURVIVING reference (NOT the polarity-screen-out shortcut where the
    reference is dropped before competition — that is pinned separately by
    ``test_inverted_reference_is_polarity_screened_then_misconception_resolves``).

    Measured raw token_set_ratio (no antonym pair present -> both survive
    polarity):
      student surface = 'pressure the flow goes through the pipe at the narrow
        nozzle section'
      reference alias '...section now'  -> raw 0.9623  (lexically CLOSER)
      misconception   '...nozzle area'  -> raw 0.9451  (within 0.05 margin)
    """
    # student_surface_text(definition) == concept + ' ' + meaning.
    meaning = "the flow goes through the pipe at the narrow nozzle section"
    graph = KGGraph(
        nodes=[
            _node("s1", "definition", {"concept": "pressure", "meaning": meaning}),
        ]
    )
    ref = _cand(
        "def.pressure_velocity_tradeoff",
        node_type="definition",
        aliases=("the flow goes through the pipe at the narrow nozzle section now",),
    )
    misc = _cand(
        "misc.pressure_velocity_same_direction",
        node_type="definition",
        is_misc=True,
        aliases=("the flow goes through the pipe at the narrow nozzle area",),
        opposes="def.pressure_velocity_tradeoff",
    )
    cands = (ref, misc)

    # --- BOTH candidates enter competition (neither is polarity-screened out) ---
    # The reference SURVIVES the polarity screen: same-direction wording, no
    # antonym pair trips. Proof 1: the reference resolves when it is the SOLE
    # candidate (so it is not silently dropped before competition).
    ref_alone = resolve_attempt(
        KGGraph(nodes=[_node("s1", "definition", {"concept": "pressure", "meaning": meaning})]),
        (ref,),
    )
    assert ref_alone.resolved[0].resolved_key == "def.pressure_velocity_tradeoff"
    assert ref_alone.resolved[0].method == "fuzzy"

    # Proof 2 (instrument the seam): exactly TWO type-compatible above-threshold
    # lexical candidates survive into competition (the reference + the misc).
    surviving = match_fuzzy_all(graph.nodes[0], cands, threshold=0.9)
    surviving_keys = {h.candidate.canonical_key for h in surviving}
    assert surviving_keys == {
        "def.pressure_velocity_tradeoff",
        "misc.pressure_velocity_same_direction",
    }
    assert len(surviving) == 2
    # The reference is the lexically-CLOSER hit (higher raw token_set_ratio).
    by_key = {h.candidate.canonical_key: h.score for h in surviving}
    assert (
        by_key["def.pressure_velocity_tradeoff"] > by_key["misc.pressure_velocity_same_direction"]
    )

    # --- The within-margin misconception STILL wins through resolve_attempt ---
    result = resolve_attempt(graph, cands)
    rn = result.resolved[0]
    assert rn.resolved_key == "misc.pressure_velocity_same_direction"
    assert rn.method == "fuzzy"
    assert rn.confidence == METHOD_CONFIDENCE_CAP["fuzzy"]


def test_polar_near_miss_competition_discriminates_vs_single_global_best():
    """DISCRIMINATION PROOF for the test above: the misconception wins ONLY
    because multi-candidate competition runs. Under single-global-best fuzzy
    selection the lexically-CLOSER reference (raw 0.9623) would win outright.

    ``match_fuzzy`` is the single-best wrapper (the pre-competition selection an
    implementation WITHOUT competition would use); it returns the REFERENCE. The
    wired ``resolve_attempt`` returns the MISCONCEPTION. The two disagree -> the
    test genuinely pins the competition path, not just thresholding."""
    meaning = "the flow goes through the pipe at the narrow nozzle section"
    node = _node("s1", "definition", {"concept": "pressure", "meaning": meaning})
    ref = _cand(
        "def.pressure_velocity_tradeoff",
        node_type="definition",
        aliases=("the flow goes through the pipe at the narrow nozzle section now",),
    )
    misc = _cand(
        "misc.pressure_velocity_same_direction",
        node_type="definition",
        is_misc=True,
        aliases=("the flow goes through the pipe at the narrow nozzle area",),
        opposes="def.pressure_velocity_tradeoff",
    )
    cands = (ref, misc)

    # Single-global-best fuzzy (competition BYPASSED) -> the closer REFERENCE.
    single_best = match_fuzzy(node, cands, threshold=0.9)
    assert single_best is not None
    assert single_best[0].canonical_key == "def.pressure_velocity_tradeoff"
    assert round(single_best[2], 4) == 0.9623

    # The wired path (competition RUNS) -> the within-margin MISCONCEPTION.
    result = resolve_attempt(KGGraph(nodes=[node]), cands)
    assert result.resolved[0].resolved_key == "misc.pressure_velocity_same_direction"
    # The two selection strategies disagree: that disagreement IS the proof.
    assert single_best[0].canonical_key != result.resolved[0].resolved_key


def test_inverted_reference_is_polarity_screened_then_misconception_resolves():
    """The SEPARATE polarity-screen-out path (NOT competition): a
    direction-INVERTED reference alias ('higher' vs student 'lower') is dropped
    by the polarity screen BEFORE competition, so the misconception then wins as
    the SOLE surviving lexical candidate.

    Honestly named: this proves the polarity screen rejects an inverted
    reference, it does NOT exercise the within-margin out-competition branch
    (that is pinned by
    ``test_polar_near_miss_resolves_to_misconception_through_resolve_attempt``)."""
    meaning = "faster flow gives lower pressure here"  # student says 'lower'
    graph = KGGraph(
        nodes=[
            _node("s1", "definition", {"concept": "pressure", "meaning": meaning}),
        ]
    )
    inverted_ref = _cand(
        "def.pressure_velocity_tradeoff",
        node_type="definition",
        aliases=("faster flow gives higher pressure here",),  # 'higher' inverts 'lower'
    )
    misc = _cand(
        "misc.pressure_velocity_same_direction",
        node_type="definition",
        is_misc=True,
        # Same WRONG ('higher') direction, lexically close enough to clear 0.9.
        aliases=("pressure faster flow gives higher pressure here",),
        opposes="def.pressure_velocity_tradeoff",
    )
    cands = (inverted_ref, misc)

    # The inverted reference is polarity-screened OUT (a fuzzy hit that the
    # higher/lower antonym pair rejects), so it never reaches competition.
    surface = student_surface_text(graph.nodes[0])
    ref_fuzzy = match_fuzzy_all(graph.nodes[0], (inverted_ref,), threshold=0.9)
    assert len(ref_fuzzy) == 1  # it IS an above-threshold fuzzy hit ...
    assert not polarity_screen(surface, ref_fuzzy[0].winning_alias)  # ... but inverted

    result = resolve_attempt(graph, cands)
    rn = result.resolved[0]
    # Misconception wins as the SOLE surviving lexical candidate.
    assert rn.resolved_key == "misc.pressure_velocity_same_direction"
    assert rn.method == "fuzzy"


def test_polarity_screen_only_rejects_the_winning_alias_not_unrelated_aliases():
    """A valid same-direction reference match is KEPT even when the SAME
    candidate carries an UNRELATED, direction-inverted alias that IS a registered
    antonym of the student text. Screening EVERY alias (old bug) false-rejected
    the match; per-winning-alias screening keeps it.

    Discriminating fixture (unlike the old one):
      * the WINNING hit is FUZZY (the student surface is NOT a normalized-exact
        match for any alias) -> the polarity screen actually applies to it;
      * the unrelated NON-winning alias 'pressure decreases here' IS a registered
        direction-antonym of the student ('increases' <-> 'decreases', a
        ``competition._DIRECTION_ANTONYMS`` pair).

    So the OLD ``all(polarity_screen(...) for a in cand.aliases)`` predicate
    yields keep=False (node UNRESOLVED) while the NEW winning-only screen yields
    keep=True (node RESOLVED). The discrimination is proven below by running the
    reconstructed OLD predicate on this exact fixture."""
    # student_surface_text(definition) == concept + ' ' + meaning, so the surface
    # is 'pressure the pressure increases as the pipe widens downstream' — NOT a
    # normalized-exact match for the winning alias, so the WINNING hit is FUZZY.
    meaning = "the pressure increases as the pipe widens downstream"
    graph = KGGraph(
        nodes=[
            _node("s1", "definition", {"concept": "pressure", "meaning": meaning}),
        ]
    )
    winning_alias = "the pressure increases as the pipe widens further downstream"
    unrelated_antonym = "pressure decreases here"  # decreases <-> increases: registered
    cand = _cand(
        "def.pressure_velocity_tradeoff",
        node_type="definition",
        aliases=(winning_alias, unrelated_antonym),
    )

    surface = student_surface_text(graph.nodes[0])

    # The winning hit is FUZZY (no normalized-exact alias match on the winner) and
    # the screen passes for it (same direction: 'increases' both sides).
    fuzzy_hits = match_fuzzy_all(graph.nodes[0], (cand,), threshold=0.9)
    assert len(fuzzy_hits) == 1
    assert fuzzy_hits[0].winning_alias == winning_alias
    assert polarity_screen(surface, winning_alias) is True

    # The unrelated alias IS a genuine registered antonym of the student text.
    assert polarity_screen(surface, unrelated_antonym) is False

    # --- DISCRIMINATION PROOF ---
    # OLD all()-screen over EVERY alias FAILS this fixture (keep=False).
    old_keep = all(polarity_screen(surface, a) for a in cand.aliases)
    assert old_keep is False  # OLD logic -> node would be UNRESOLVED
    # NEW per-winning-alias screen PASSES (keep=True).
    new_keep = polarity_screen(surface, fuzzy_hits[0].winning_alias)
    assert new_keep is True  # NEW logic -> node RESOLVED

    # End-to-end: the live (NEW) resolver resolves the node.
    result = resolve_attempt(graph, (cand,))
    rn = result.resolved[0]
    assert rn.resolution == "resolved"
    assert rn.resolved_key == "def.pressure_velocity_tradeoff"
    assert rn.method == "fuzzy"


# ---------------------------------------------------------------------------
# NIT (§5) — a candidate that exact-alias-matches (raw 1.0) ALSO fuzzy-matches
# itself (token_set_ratio of identical strings == 1.0). It must enter the lexical
# competition ONCE, via the higher (alias) tier, so the reported method does not
# depend on enqueue order or max()'s tie-stability.
# ---------------------------------------------------------------------------


def test_exact_alias_and_fuzzy_dual_hit_resolves_via_alias_not_fuzzy():
    """A candidate whose alias EXACTLY equals the student surface is both an
    exact-alias hit (raw 1.0) AND a fuzzy hit (identical strings -> ratio 1.0).
    It must resolve via the HIGHER tier ('alias' / 0.92), never the lower one
    ('fuzzy' / 0.80)."""
    graph = KGGraph(
        nodes=[
            _node("s1", "definition", {"concept": "pressure", "meaning": "density is constant"}),
        ]
    )
    # surface == 'pressure density is constant'; the alias matches it verbatim.
    cand = _cand("def.x", node_type="definition", aliases=("pressure density is constant",))

    # The candidate IS a dual hit: it appears in BOTH the alias and fuzzy tiers
    # at raw 1.0 (this is the double-enqueue the NIT dedupe collapses).
    node = graph.nodes[0]
    alias_hits = match_alias_all(node, (cand,))
    fuzzy_hits = match_fuzzy_all(node, (cand,), threshold=0.9)
    assert [h.candidate.canonical_key for h in alias_hits] == ["def.x"]
    assert [h.candidate.canonical_key for h in fuzzy_hits] == ["def.x"]
    assert alias_hits[0].score == 1.0 and fuzzy_hits[0].score == 1.0

    result = resolve_attempt(graph, (cand,))
    rn = result.resolved[0]
    assert rn.method == "alias"
    assert rn.confidence == METHOD_CONFIDENCE_CAP["alias"]  # 0.92, not fuzzy 0.80
    assert rn.resolved_key == "def.x"


def test_dual_hit_alias_preference_is_order_independent():
    """The alias>fuzzy preference for a dual-hit candidate is EXPLICIT, not a
    side effect of enqueue order or max() tie-stability: _content_match collapses
    the double-enqueue to ONE alias entry regardless of how the matchers order
    their hits.

    Discrimination: the OLD un-deduped lexical list IS order-sensitive — feeding
    the same two raw-1.0 ScoredMatches to ``apply_misconception_competition``
    fuzzy-first flips the method to 'fuzzy'. _content_match must NOT have that
    sensitivity."""
    surface = "pressure density is constant"
    cand = _cand("def.x", node_type="definition", aliases=(surface,))

    # OLD list-based behavior is order-sensitive (the bug being fixed):
    alias_sm = ScoredMatch("s1", cand, "alias", 1.0)
    fuzzy_sm = ScoredMatch("s1", cand, "fuzzy", 1.0)
    assert apply_misconception_competition(surface, [alias_sm, fuzzy_sm]).method == "alias"
    assert apply_misconception_competition(surface, [fuzzy_sm, alias_sm]).method == "fuzzy"

    # NEW _content_match always yields the alias tier (dedupe by candidate). The
    # student surface ('pressure density is constant') is built from the node, so
    # the dual hit is produced internally; the result is stable.
    node = _node("s1", "definition", {"concept": "pressure", "meaning": "density is constant"})
    match = _content_match(node, (cand,), fuzzy_threshold=0.9, symbolic_mappings={})
    assert match is not None
    assert match.method == "alias"


# ---------------------------------------------------------------------------
# DEFECT C (§5) — symbolic mappings are per-problem declared data, NOT a global
# default. With NO mappings passed, a d=2r-dependent equivalence must NOT match;
# with the per-problem mapping passed explicitly, the §6.9 case DOES match.
# ---------------------------------------------------------------------------


def test_default_no_symbolic_mapping_does_not_false_match_diameter_cube():
    """resolve_attempt with the DEFAULT (no symbolic_mappings) must NOT treat
    'V = d**3' as equivalent to 'V = 8*r**3' — the global d=2r default that
    produced that false symbolic match is gone."""
    graph = KGGraph(
        nodes=[
            _node("v1", "equation", {"symbolic": "V = d**3", "label": "", "variables": []}),
        ]
    )
    cands = (_cand("eq.sphere_ish", node_type="equation", symbolic="V = 8*r**3"),)
    result = resolve_attempt(graph, cands)
    rn = result.resolved[0]
    # No mapping -> d and r are independent symbols -> not equivalent -> unresolved.
    assert rn.resolution == "unresolved"
    assert rn.resolved_key is None


def test_explicit_per_problem_mapping_resolves_circular_area():
    """With the per-problem mapping {'d': '2*r'} passed explicitly, the §6.9
    circular-area case (A = pi*r**2 <-> A = pi*d**2/4) resolves via symbolic."""
    graph = KGGraph(
        nodes=[
            _node("a1", "equation", {"symbolic": "A = pi*r**2", "label": "", "variables": []}),
        ]
    )
    cands = (_cand("eq.circular_area", node_type="equation", symbolic="A = pi*d**2/4"),)
    result = resolve_attempt(graph, cands, symbolic_mappings={"d": "2*r"})
    rn = result.resolved[0]
    assert rn.resolved_key == "eq.circular_area"
    assert rn.method == "symbolic"


def test_default_no_mapping_circular_area_stays_unresolved():
    """Without the explicit mapping the circular-area pair does NOT symbolically
    match (d/r independent) — confirms the §6.9 match depended on the per-problem
    mapping, not a global default."""
    graph = KGGraph(
        nodes=[
            _node("a1", "equation", {"symbolic": "A = pi*r**2", "label": "", "variables": []}),
        ]
    )
    cands = (_cand("eq.circular_area", node_type="equation", symbolic="A = pi*d**2/4"),)
    result = resolve_attempt(graph, cands)
    assert result.resolved[0].resolution == "unresolved"


def test_worked_example_6_9_requires_explicit_mapping_for_area():
    """The §6.9 worked example resolves end-to-end ONLY when the per-problem
    d=2r mapping is supplied; the circular-area node is the mapping-dependent
    one."""
    result = resolve_attempt(
        _worked_example_graph(),
        _worked_example_candidates(),
        symbolic_mappings={"d": "2*r"},
    )
    keyed = {rn.node_id: rn.resolved_key for rn in result.resolved}
    assert keyed["d1"] == "cond.incompressibility"
    assert keyed["a1"] == "eq.circular_area"
    assert keyed["b1"] == "eq.bernoulli"
    assert keyed["p1"] == "proc.compute_v2"
    assert keyed["t1"] == "def.pressure_velocity_tradeoff"
    assert result.llm_calls == 0


# ---------------------------------------------------------------------------
# PHASE 0 — post-tier unresolved behaviour (the LLM adjudication path is gone).
# A node that no content tier can place stays unresolved regardless of whether
# the candidate set has a type-compatible key or not.
# ---------------------------------------------------------------------------


def test_unmatched_definition_with_incompatible_candidate_stays_unresolved():
    """A definition node whose surface matches NO content tier stays unresolved
    when the only candidate is type-INCOMPATIBLE (simplification). This is now
    purely the content-tier gate — no adjudicator involved."""
    graph = KGGraph(
        nodes=[
            _node(
                "s1",
                "definition",
                {"concept": "wholly", "meaning": "unrelated ambiguous phrase qqq"},
            ),
        ]
    )
    simp_cand = _cand("simp.drop_friction", node_type="simplification", aliases=())
    cands = (simp_cand,)

    result = resolve_attempt(graph, cands)
    rn = result.resolved[0]
    assert rn.resolution == "unresolved"
    assert rn.method == "unresolved"
    assert rn.resolved_key is None
    assert result.llm_calls == 0


def test_unmatched_definition_with_compatible_candidate_stays_unresolved():
    """A definition node whose surface matches NO content tier stays unresolved
    even when a type-COMPATIBLE candidate exists — no LLM guess is made."""
    graph = KGGraph(
        nodes=[
            _node(
                "s1",
                "definition",
                {"concept": "wholly", "meaning": "unrelated ambiguous phrase qqq"},
            ),
        ]
    )
    def_cand = _cand("def.some_concept", node_type="definition", aliases=())
    cands = (def_cand,)

    result = resolve_attempt(graph, cands)
    rn = result.resolved[0]
    assert rn.resolution == "unresolved"
    assert rn.method == "unresolved"
    assert rn.resolved_key is None
    assert result.llm_calls == 0


def test_tier_counts_pinned_on_real_6_9_output():
    """NIT (test-honesty): pin _histogram's REAL output on the §6.9 graph — the
    per-method histogram from actual resolution, not a hand-built dict."""
    result = resolve_attempt(
        _worked_example_graph(),
        _worked_example_candidates(),
        symbolic_mappings={"d": "2*r"},
    )
    # Real per-method histogram (pinned, not hand-built): d1 alias, b1 alias,
    # p1 alias (3 exact-alias hits), a1 symbolic (d=2r), t1 fuzzy (the
    # definition's concept+meaning surface paraphrases the bare alias).
    assert dict(result.tier_counts) == {"alias": 3, "symbolic": 1, "fuzzy": 1}


# ---------------------------------------------------------------------------
# Phase 1b — exact-only alias channel end-to-end. A reference candidate's
# curated `exact_aliases` entry resolves through the wired resolve_attempt via
# the alias tier (method 'alias', cap 0.92). With NO curated alias set, the
# §6.9 worked example resolves byte-identically to today (default exact_aliases
# = () ⇒ no behavior change).
# ---------------------------------------------------------------------------


def test_exact_alias_resolves_end_to_end_via_resolve_attempt():
    """A condition node whose surface == a candidate's ``exact_aliases`` entry
    resolves end-to-end through ``resolve_attempt``: method 'alias', confidence
    METHOD_CONFIDENCE_CAP['alias'] (0.92), ``resolved_key`` == the candidate
    key. Proves the exact-alias channel reaches the live resolver path."""
    graph = KGGraph(
        nodes=[
            _node("c1", "condition", {"applies_when": "open to the atmosphere", "label": ""}),
        ]
    )
    cand = _cand(
        "cond.open_tank",
        node_type="condition",
        exact_aliases=("open to the atmosphere",),
    )
    result = resolve_attempt(graph, (cand,))
    rn = result.resolved[0]
    assert rn.resolution == "resolved"
    assert rn.method == "alias"
    assert rn.confidence == METHOD_CONFIDENCE_CAP["alias"]  # 0.92
    assert rn.resolved_key == "cond.open_tank"


def test_empty_exact_aliases_resolution_unchanged():
    """Byte-identical regression: every worked-example candidate carries the
    default ``exact_aliases == ()``, so the full §6.9 resolution (per-node
    resolved_key + method + confidence, tier histogram, llm_calls) is IDENTICAL
    to the pre-Phase-1b expected — no curated alias ⇒ no behavior change."""
    result = resolve_attempt(
        _worked_example_graph(),
        _worked_example_candidates(),
        symbolic_mappings={"d": "2*r"},
    )
    keyed = {rn.node_id: rn.resolved_key for rn in result.resolved}
    assert keyed == {
        "d1": "cond.incompressibility",
        "a1": "eq.circular_area",
        "b1": "eq.bernoulli",
        "p1": "proc.compute_v2",
        "t1": "def.pressure_velocity_tradeoff",
    }
    methods = {rn.node_id: rn.method for rn in result.resolved}
    assert methods == {
        "d1": "alias",
        "a1": "symbolic",
        "b1": "alias",
        "p1": "alias",
        "t1": "fuzzy",
    }
    for rn in result.resolved:
        assert rn.confidence == METHOD_CONFIDENCE_CAP[rn.method]
    assert dict(result.tier_counts) == {"alias": 3, "symbolic": 1, "fuzzy": 1}
    assert result.llm_calls == 0


# ---------------------------------------------------------------------------
# Task 3 — confirmed_resolutions: authoritative clarification at 0.90.
# ---------------------------------------------------------------------------


def test_confirmed_resolution_resolves_via_clarification_at_0_90():
    """A node in confirmed_resolutions with a type-compatible key resolves via
    method 'clarification' at confidence 0.90 and drops out of unresolved_rate."""
    graph = _graph([_node("s1", "condition", {"applies_when": "pressure changes", "label": ""})])
    cands = _cands_with("cond.bernoulli", node_type="condition")
    result = resolve_attempt(graph, cands, confirmed_resolutions={"s1": "cond.bernoulli"})
    rn = {r.node_id: r for r in result.resolved}["s1"]
    assert rn.resolution == "resolved"
    assert rn.method == "clarification"
    assert rn.confidence == 0.90
    assert rn.resolved_key == "cond.bernoulli"
    # The whole point: a confirmed node drops OUT of unresolved_rate.
    assert unresolved_rate_of(result) == 0.0


def test_confirmed_resolution_overrides_a_weaker_tier_hit():
    """Even if a content tier (alias/fuzzy) would have matched, clarification is
    authoritative and wins regardless of any lower-tier hit."""
    graph = _graph([_node("s1", "condition", {"applies_when": "slower means higher", "label": ""})])
    cands = _cands_with("cond.bernoulli", node_type="condition", aliases=("slower means higher",))
    result = resolve_attempt(graph, cands, confirmed_resolutions={"s1": "cond.bernoulli"})
    rn = {r.node_id: r for r in result.resolved}["s1"]
    assert rn.method == "clarification"


def test_confirmed_resolution_ignored_when_type_incompatible_or_unknown_key():
    """A type-incompatible mapping or an unknown candidate key in
    confirmed_resolutions is silently ignored; the node follows the normal path
    (unresolved when no tier matches)."""
    graph = _graph([_node("s1", "condition", {"applies_when": "x", "label": ""})])
    cands = _cands_with("eq.mass", node_type="equation")  # wrong type
    result = resolve_attempt(graph, cands, confirmed_resolutions={"s1": "eq.mass", "s2": "nope"})
    rn = {r.node_id: r for r in result.resolved}["s1"]
    assert rn.resolution == "unresolved"  # type-incompatible -> not credited


def test_clarification_confidence_clears_normalization_floor():
    """Spec §13 / brainstorm §5.3 hard constraint: a confirmed conceptual node's
    0.90 cap normalizes to 1.0 against the 0.75 prose ceiling, comfortably
    clearing the 0.85 abstention floor — so a confirmed node never triggers the
    normalization_confidence gate."""
    from apollo.grading.abstention import ABSTENTION_THRESHOLDS
    from apollo.grading.normalization_confidence import (
        RESOLUTION_CEILING_DEFAULT,
        _type_normalized_confidence,
    )

    cap = 0.90  # clarification cap (METHOD_CONFIDENCE_CAP["clarification"])
    # Conceptual node: ceiling 0.75 -> type-normalized = min(1.0, 0.90/0.75) = 1.0.
    nc = _type_normalized_confidence("condition", cap)
    assert nc == 1.0
    assert nc >= ABSTENTION_THRESHOLDS["min_normalization_confidence"]  # 0.85
    assert RESOLUTION_CEILING_DEFAULT == 0.75


# ---------------------------------------------------------------------------
# Task 6 — find_residual_nodes: the public clarification-detector seam.
# ---------------------------------------------------------------------------


def test_find_residual_nodes_returns_only_unmatched():
    from apollo.resolution.resolver import find_residual_nodes

    # "matched" node: its surface text IS an alias of the candidate → _content_match
    # returns a ScoredMatch, so it is NOT residual.
    matched = _node("s1", "condition", {"applies_when": "exact alias text", "label": ""})
    # "residual" node: its surface text does not match any tier → _content_match None.
    residual = _node("s2", "condition", {"applies_when": "some paraphrase", "label": ""})
    cands = _cands_with("cond.k", node_type="condition", aliases=("exact alias text",))
    out = find_residual_nodes([matched, residual], cands)
    assert [n.node_id for n in out] == ["s2"]
