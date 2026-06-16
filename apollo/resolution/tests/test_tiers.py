"""WU-3C2 Step 4 — pure-unit tests for the content tiers (the heart, §5).

No Docker, no network. One assertion per tier behavior:
- exact key / identical content -> method 'exact';
- SymPy structural equivalence under d=2r (REUSES parse_zero_form), sign-exact;
- normalized alias match -> method 'alias';
- RapidFuzz >= 0.9 paraphrase -> method 'fuzzy'; below threshold -> None;
- _fuzzy_ratio normalizes to 0..1.
"""

from __future__ import annotations

from apollo.ontology.nodes import build_node
from apollo.resolution.candidates import Candidate
from apollo.resolution.tiers import (
    _fuzzy_ratio,
    match_alias,
    match_exact,
    match_fuzzy,
    match_symbolic,
    student_surface_text,
)


def _equation_node(node_id: str, symbolic: str, *, label: str = ""):
    return build_node(
        node_type="equation",
        node_id=node_id,
        attempt_id=1,
        source="parser",
        content={"symbolic": symbolic, "label": label, "variables": []},
    )


def _condition_node(node_id: str, applies_when: str):
    return build_node(
        node_type="condition",
        node_id=node_id,
        attempt_id=1,
        source="parser",
        content={"applies_when": applies_when, "label": ""},
    )


def _cand(key, *, node_type, symbolic=None, aliases=(), is_misc=False, opposes=None):
    return Candidate(
        canonical_key=key,
        canon_key=hash(key) % 1000,
        node_type=node_type,
        is_misconception=is_misc,
        symbolic=symbolic,
        aliases=tuple(aliases),
        display_name=key,
        opposes_key=opposes,
    )


def test_exact_tier_matches_identical_key():
    node = _equation_node("s1", "A = pi*r**2", label="eq.circular_area")
    cands = (_cand("eq.circular_area", node_type="equation", symbolic="A = pi*r**2"),)
    hit = match_exact(node, cands)
    assert hit is not None
    cand, method, score = hit
    assert cand.canonical_key == "eq.circular_area"
    assert method == "exact"


def test_symbolic_tier_circular_area_with_d_eq_2r():
    node = _equation_node("s1", "A = pi*r**2")
    cands = (_cand("eq.circular_area", node_type="equation", symbolic="A = pi*d**2/4"),)
    hit = match_symbolic(node, cands, mappings={"d": "2*r"})
    assert hit is not None
    cand, method, _ = hit
    assert cand.canonical_key == "eq.circular_area"
    assert method == "symbolic"


def test_symbolic_tier_sign_exact_rejects_inverted():
    node = _equation_node("s1", "A = -pi*r**2")
    cands = (_cand("eq.circular_area", node_type="equation", symbolic="A = pi*d**2/4"),)
    hit = match_symbolic(node, cands, mappings={"d": "2*r"})
    assert hit is None  # sign-exact: -A does not match +A


def test_alias_tier_density_is_constant_to_incompressibility():
    node = _condition_node("s1", "density is constant")
    cands = (
        _cand(
            "cond.incompressibility",
            node_type="condition",
            aliases=("density is constant", "incompressible flow"),
        ),
    )
    hit = match_alias(node, cands)
    assert hit is not None
    cand, method, _ = hit
    assert cand.canonical_key == "cond.incompressibility"
    assert method == "alias"


def test_fuzzy_tier_above_threshold():
    node = _condition_node("s1", "the density stays constant throughout")
    cands = (
        _cand(
            "cond.incompressibility",
            node_type="condition",
            aliases=("density is constant",),
        ),
    )
    hit = match_fuzzy(node, cands, threshold=0.9)
    assert hit is not None
    _, method, score = hit
    assert method == "fuzzy"
    assert score >= 0.9


def test_fuzzy_tier_below_threshold_returns_none():
    node = _condition_node("s1", "the pipe is painted bright red")
    cands = (
        _cand(
            "cond.incompressibility",
            node_type="condition",
            aliases=("density is constant",),
        ),
    )
    hit = match_fuzzy(node, cands, threshold=0.9)
    assert hit is None  # no snap below threshold


def test_fuzzy_ratio_normalized_0_to_1():
    assert _fuzzy_ratio("abc", "abc") == 1.0
    assert _fuzzy_ratio("aaaa bbbb", "zzzz yyyy") < 0.9


def test_student_surface_text_per_node_type():
    eq = _equation_node("e", "P1 = P2")
    cond = _condition_node("c", "density is constant")
    assert "P1 = P2" in student_surface_text(eq) or "P1=P2" in student_surface_text(eq)
    assert "density is constant" in student_surface_text(cond)


def _node(node_id, node_type, content):
    return build_node(
        node_type=node_type, node_id=node_id, attempt_id=1, source="parser", content=content
    )


def test_student_surface_text_simplification_concats_applies_and_transform():
    node = _node("s", "simplification",
                 {"applies_when": "h1 == h2", "transformation": "gravity cancels"})
    text = student_surface_text(node)
    assert "h1 == h2" in text and "gravity cancels" in text


def test_student_surface_text_definition_concats_concept_and_meaning():
    node = _node("d", "definition", {"concept": "tradeoff", "meaning": "pressure drops"})
    text = student_surface_text(node)
    assert "tradeoff" in text and "pressure drops" in text


def test_student_surface_text_procedure_step_uses_action():
    node = _node("p", "procedure_step", {"action": "apply continuity", "purpose": ""})
    assert student_surface_text(node) == "apply continuity"


def test_student_surface_text_variable_mapping_uses_term():
    node = _node("v", "variable_mapping", {"term": "density", "symbol": "rho"})
    assert student_surface_text(node) == "density"


def test_exact_tier_matches_canonical_key_via_label():
    """The exact tier fires when the node's label equals the candidate key."""
    node = _condition_node("s1", "anything")
    node = build_node(
        node_type="condition", node_id="s1", attempt_id=1, source="parser",
        content={"applies_when": "anything", "label": "cond.incompressibility"},
    )
    cands = (_cand("cond.incompressibility", node_type="condition"),)
    hit = match_exact(node, cands)
    assert hit is not None and hit[1] == "exact"


def test_exact_tier_skips_type_incompatible_candidate():
    """A type-mismatched candidate is skipped by the exact tier (no match)."""
    node = _equation_node("s1", "A = pi*r**2", label="A = pi*r**2")
    cands = (_cand("cond.x", node_type="condition", aliases=()),)  # wrong type
    assert match_exact(node, cands) is None


def test_symbolic_tier_unparseable_is_non_match():
    """An unparseable equation is a non-match (None), never a crash."""
    node = _equation_node("s1", "= = =")  # not parseable as LHS=RHS
    cands = (_cand("eq.x", node_type="equation", symbolic="A = pi*d**2/4"),)
    assert match_symbolic(node, cands, mappings={}) is None


def test_symbolic_tier_skips_non_equation_node():
    node = _condition_node("s1", "density is constant")
    cands = (_cand("eq.x", node_type="equation", symbolic="A = pi*r**2"),)
    assert match_symbolic(node, cands) is None


def test_alias_tier_skips_type_incompatible_and_empty_surface():
    # type-incompatible candidate is skipped
    node = _condition_node("s1", "density is constant")
    wrong_type = (_cand("eq.x", node_type="equation", aliases=("density is constant",)),)
    assert match_alias(node, wrong_type) is None


def test_fuzzy_tier_skips_type_incompatible_candidate():
    node = _condition_node("s1", "the density stays constant throughout")
    wrong_type = (_cand("eq.x", node_type="equation", aliases=("density is constant",)),)
    assert match_fuzzy(node, wrong_type, threshold=0.9) is None


def test_exact_tier_matches_identical_symbolic_without_label():
    """The exact tier also fires when the equation symbolic matches verbatim
    (no label needed) — the surface==symbolic branch."""
    node = _equation_node("s1", "P1 - P2")  # no label set
    cands = (_cand("eq.b", node_type="equation", symbolic="P1 - P2"),)
    hit = match_exact(node, cands)
    assert hit is not None and hit[0].canonical_key == "eq.b" and hit[1] == "exact"


def test_symbolic_tier_candidate_without_symbolic_is_skipped():
    """An equation candidate with no symbolic is skipped by the symbolic tier."""
    node = _equation_node("s1", "A = pi*r**2")
    cands = (_cand("eq.x", node_type="equation", symbolic=None),)
    assert match_symbolic(node, cands, mappings={}) is None


def test_symbolic_tier_unparseable_candidate_is_non_match():
    """A candidate whose symbolic does not parse is a non-match, not a crash."""
    node = _equation_node("s1", "A = pi*r**2")
    cands = (_cand("eq.x", node_type="equation", symbolic="@@@"),)
    assert match_symbolic(node, cands, mappings={}) is None


def test_symbolic_tier_ignores_unparseable_mapping_value():
    """A mapping whose replacement value fails to parse is skipped (continue),
    and a still-equivalent pair matches."""
    node = _equation_node("s1", "A = pi*r**2")
    cands = (_cand("eq.x", node_type="equation", symbolic="A = pi*r**2"),)
    hit = match_symbolic(node, cands, mappings={"d": "@@@"})
    assert hit is not None  # base equivalence holds; bad mapping value ignored


def test_alias_tier_no_matching_alias_returns_none():
    """A candidate WITH aliases but none matching -> None (alias loop exhausts)."""
    node = _condition_node("s1", "density is constant")
    cands = (_cand("cond.x", node_type="condition", aliases=("totally different",)),)
    assert match_alias(node, cands) is None


def test_fuzzy_tier_picks_highest_above_threshold():
    """With two above-threshold aliases the higher score wins (the best-update
    branch)."""
    node = _condition_node("s1", "the density stays constant throughout")
    cands = (
        _cand("cond.a", node_type="condition", aliases=("density is constant",)),
        _cand("cond.b", node_type="condition",
              aliases=("the density stays constant throughout the pipe",)),
    )
    hit = match_fuzzy(node, cands, threshold=0.9)
    assert hit is not None
    assert hit[0].canonical_key == "cond.b"  # closer paraphrase wins


def test_fuzzy_tier_keeps_best_when_later_candidate_is_worse():
    """A later, lower-scoring above-threshold candidate does NOT replace the
    incumbent best (the not-better branch of the best-update)."""
    node = _condition_node("s1", "density is constant exactly")
    cands = (
        _cand("cond.a", node_type="condition", aliases=("density is constant exactly",)),
        _cand("cond.b", node_type="condition", aliases=("density is constant roughly",)),
    )
    hit = match_fuzzy(node, cands, threshold=0.9)
    assert hit is not None
    assert hit[0].canonical_key == "cond.a"  # exact-ish first alias stays best


def test_exact_tier_scans_past_non_matching_first_candidate():
    """The exact tier loops past a first candidate that doesn't match and finds
    the matching one (the symbolic-branch False -> next-iteration edge)."""
    node = _equation_node("s1", "P1 - P2")  # no label
    cands = (
        _cand("eq.other", node_type="equation", symbolic="X - Y"),  # no match
        _cand("eq.b", node_type="equation", symbolic="P1 - P2"),    # matches
    )
    hit = match_exact(node, cands)
    assert hit is not None and hit[0].canonical_key == "eq.b"
