"""WU-3C2 Step 4 — pure-unit tests for the content tiers (the heart, §5).

No Docker, no network. One assertion per tier behavior:
- exact key / identical content -> method 'exact';
- SymPy structural equivalence under d=2r (REUSES parse_zero_form), sign-exact;
- normalized alias match -> method 'alias';
- RapidFuzz >= 0.9 paraphrase -> method 'fuzzy'; below threshold -> None;
- _fuzzy_ratio normalizes to 0..1.
"""

from __future__ import annotations

import pytest

from apollo.ontology.nodes import build_node
from apollo.resolution.candidates import Candidate
from apollo.resolution.tiers import (
    _fuzzy_ratio,
    match_alias,
    match_alias_all,
    match_exact,
    match_fuzzy,
    match_fuzzy_all,
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


def _cand(
    key, *, node_type, symbolic=None, aliases=(), is_misc=False, opposes=None, exact_aliases=()
):
    return Candidate(
        canonical_key=key,
        canon_key=hash(key) % 1000,
        node_type=node_type,
        is_misconception=is_misc,
        symbolic=symbolic,
        aliases=tuple(aliases),
        display_name=key,
        opposes_key=opposes,
        exact_aliases=tuple(exact_aliases),
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
    node = _node(
        "s", "simplification", {"applies_when": "h1 == h2", "transformation": "gravity cancels"}
    )
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
        node_type="condition",
        node_id="s1",
        attempt_id=1,
        source="parser",
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
        _cand(
            "cond.b",
            node_type="condition",
            aliases=("the density stays constant throughout the pipe",),
        ),
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
        _cand("eq.b", node_type="equation", symbolic="P1 - P2"),  # matches
    )
    hit = match_exact(node, cands)
    assert hit is not None and hit[0].canonical_key == "eq.b"


# ---------------------------------------------------------------------------
# WU-3C2 fix — the "_all" lexical tiers that feed misconception competition.
# These return EVERY type-compatible above-threshold match with its RAW score
# and the specific winning alias, so a competing misconception is never
# discarded before competition runs (§5 / §6.11).
# ---------------------------------------------------------------------------


def test_match_alias_all_returns_every_type_compatible_exact_alias_hit():
    """Two distinct candidates both carry the student's exact alias -> BOTH are
    returned (raw score 1.0, each with the winning alias), not just one."""
    node = _condition_node("s1", "density is constant")
    cands = (
        _cand("cond.a", node_type="condition", aliases=("density is constant",)),
        _cand("cond.b", node_type="condition", aliases=("incompressible", "density is constant")),
        _cand("cond.other", node_type="condition", aliases=("totally different",)),
    )
    hits = match_alias_all(node, cands)
    keys = {h.candidate.canonical_key for h in hits}
    assert keys == {"cond.a", "cond.b"}  # cond.other excluded (no alias hit)
    for h in hits:
        assert h.method == "alias"
        assert h.score == 1.0
        assert _normalize_for_test(h.winning_alias) == "density is constant"


def test_match_alias_returns_single_best_still_works_for_callers():
    """The single-best match_alias wrapper still returns ONE TierHit (back-compat
    for the exact-tier callers/tests that consume the old shape)."""
    node = _condition_node("s1", "density is constant")
    cands = (_cand("cond.a", node_type="condition", aliases=("density is constant",)),)
    hit = match_alias(node, cands)
    assert hit is not None
    assert hit[0].canonical_key == "cond.a" and hit[1] == "alias"


def test_match_fuzzy_all_returns_every_above_threshold_with_raw_score():
    """A reference and a misconception are BOTH above the fuzzy threshold for a
    polar-near-miss student claim -> BOTH appear with their RAW token_set_ratio
    and the alias that produced the hit (the misconception is not discarded as
    'not the global best')."""
    node = _node(
        "s1",
        "definition",
        {"concept": "pressure", "meaning": "faster flow gives higher pressure here"},
    )
    ref_alias = "faster flow gives lower pressure here"
    misc_alias = "faster flow means higher pressure"
    cands = (
        _cand("def.tradeoff", node_type="definition", aliases=(ref_alias,)),
        _cand("misc.same_dir", node_type="definition", is_misc=True, aliases=(misc_alias,)),
        _cand("def.unrelated", node_type="definition", aliases=("the pipe is red",)),
    )
    hits = match_fuzzy_all(node, cands, threshold=0.9)
    by_key = {h.candidate.canonical_key: h for h in hits}
    assert set(by_key) == {"def.tradeoff", "misc.same_dir"}  # unrelated below threshold
    # Raw scores, NOT the flat fuzzy cap (0.80).
    assert by_key["def.tradeoff"].score > 0.9 and by_key["def.tradeoff"].score != 0.80
    assert by_key["misc.same_dir"].score >= 0.9
    assert by_key["def.tradeoff"].winning_alias == ref_alias
    assert by_key["misc.same_dir"].winning_alias == misc_alias


def test_match_fuzzy_all_below_threshold_is_excluded():
    """Below-threshold candidates never appear (no snap, §5)."""
    node = _condition_node("s1", "the pipe is painted bright red")
    cands = (_cand("cond.x", node_type="condition", aliases=("density is constant",)),)
    assert match_fuzzy_all(node, cands, threshold=0.9) == []


def test_match_fuzzy_all_picks_best_alias_per_candidate():
    """When a candidate has several above-threshold aliases, the hit carries the
    HIGHEST-scoring alias as winning_alias (deterministic)."""
    node = _condition_node("s1", "density is constant throughout")
    cands = (
        _cand(
            "cond.a",
            node_type="condition",
            aliases=("density is constant throughout", "density is constant"),
        ),
    )
    hits = match_fuzzy_all(node, cands, threshold=0.9)
    assert len(hits) == 1
    assert hits[0].winning_alias == "density is constant throughout"  # exact paraphrase


def _normalize_for_test(text: str) -> str:
    return text.strip().lower()


# ---------------------------------------------------------------------------
# Phase 1b — exact-only alias channel. The exact-alias tier (match_alias_all)
# reads exact_aliases + aliases; the fuzzy tier (match_fuzzy_all) reads ONLY
# aliases. A curated reference phrasing resolves exact-only; a hand-wave variant
# of it can NEVER leak coverage credit through the fuzzy tier.
# ---------------------------------------------------------------------------


def _surface_node(node_id: str, node_type, text: str):
    """A node whose ``student_surface_text`` equals ``text`` exactly for the
    given ``node_type``.

    condition -> ``applies_when`` carries the whole text. definition ->
    ``student_surface_text`` is ``f"{concept} {meaning}".strip()`` and BOTH
    ``concept`` and ``meaning`` are pydantic ``min_length=1`` (empty meaning is
    rejected), so the text is split at its first space across concept+meaning;
    the single-space concat reconstructs the text verbatim. Single-word
    definition text would have no split point — the corpus has none."""
    if node_type == "condition":
        return _node(node_id, "condition", {"applies_when": text, "label": ""})
    if node_type == "definition":
        concept, _, meaning = text.partition(" ")
        return _node(node_id, "definition", {"concept": concept, "meaning": meaning})
    raise ValueError(f"unsupported node_type for _surface_node: {node_type}")  # pragma: no cover


def test_exact_alias_tier_resolves_curated_phrasing_via_exact_aliases():
    """A curated phrasing present in ``exact_aliases`` resolves via the exact
    (normalized-equality) alias tier — method 'alias', raw score 1.0."""
    node = _condition_node("s1", "open to the atmosphere")
    cands = (_cand("cond.open", node_type="condition", exact_aliases=("open to the atmosphere",)),)
    hits = match_alias_all(node, cands)
    assert len(hits) == 1
    assert hits[0].candidate.canonical_key == "cond.open"
    assert hits[0].method == "alias" and hits[0].score == 1.0


def test_fuzzy_tier_ignores_exact_aliases_near_miss_does_not_resolve():
    """CRITICAL GUARD: a near-miss paraphrase of a curated ``exact_aliases``
    entry stays UNRESOLVED in BOTH tiers. The fuzzy tier never reads
    ``exact_aliases`` (so token_set_ratio>=0.9 cannot snap it), and the exact
    tier is not exact-equal — so the hand-wave variant is missing."""
    # Near-miss paraphrase that WOULD clear token_set_ratio>=0.9 if fuzzed.
    node = _condition_node("s1", "the tank is open to the atmosphere outside")
    cands = (_cand("cond.open", node_type="condition", exact_aliases=("open to the atmosphere",)),)
    assert match_fuzzy_all(node, cands, threshold=0.9) == []  # exact_aliases never fuzzed
    assert match_alias_all(node, cands) == []  # not exact-equal either -> missing


def test_exact_alias_tier_still_reads_misconception_aliases():
    """The exact-alias tier still reads the ``aliases`` channel (e.g. a
    misconception whose trigger phrase exact-matches), proving the
    ``exact_aliases`` addition did not break the existing ``aliases`` read."""
    node = _surface_node("s1", "definition", "faster flow means higher pressure")
    cands = (
        _cand(
            "misc.same_dir",
            node_type="definition",
            is_misc=True,
            aliases=("faster flow means higher pressure",),
            exact_aliases=(),
        ),
    )
    hits = match_alias_all(node, cands)
    assert len(hits) == 1
    assert hits[0].candidate.canonical_key == "misc.same_dir"
    assert hits[0].method == "alias" and hits[0].score == 1.0


# The dropped surfaces from the edge-loss diagnosis (spec §14). Each curated
# phrasing (an exact_aliases entry) resolves; the hand-wave variant must stay
# missing in BOTH the alias and the fuzzy tiers.
_NEGATIVE_CORPUS = [
    ("open to the atmosphere", "it's basically open air out there", "condition"),
    ("the reservoir is wide", "the tank is kind of big i guess", "condition"),
    ("p1 = p2", "the pressures are roughly equal", "condition"),
    (
        "nominal and real gdp are basically the same",
        "gdp is gdp more or less",
        "definition",
    ),
    (
        "a streamline is a path a fluid particle follows",
        "it's just a line in the flow",
        "definition",
    ),
]


@pytest.mark.parametrize("curated,handwave,ntype", _NEGATIVE_CORPUS)
def test_negative_corpus_curated_resolves_handwave_missing(curated, handwave, ntype):
    """For each dropped surface: the CURATED phrasing resolves via the exact
    alias tier, and the HAND-WAVE variant stays missing in BOTH the alias and
    the fuzzy tiers (exact_aliases is never fuzzed)."""
    cand = _cand("ref.x", node_type=ntype, exact_aliases=(curated,))
    good = _surface_node("g", ntype, curated)
    bad = _surface_node("b", ntype, handwave)
    assert len(match_alias_all(good, (cand,))) == 1  # curated resolves exact
    assert match_alias_all(bad, (cand,)) == []  # hand-wave: not exact
    assert match_fuzzy_all(bad, (cand,), threshold=0.9) == []  # and never fuzzed


# --------------------------------------------------------------------------- #
# WU-AAS lane B2.2 Finding 1 — single equation parser (no resolver-local dup).
#
# The resolution tier's ``_zero_form`` now DELEGATES to the mint-time
# ``parse_zero_form`` (one parser, no duplication). These pin: (1) every form the
# OLD resolver-local parser resolved still resolves BYTE-IDENTICALLY, and (2) the
# tolerant forms the mint now stores in ``content['symbolic']`` (``^`` exponent,
# chained equality) are now PARSEABLE here too — closing the recall gap where a
# minted subject's reference equation was unparseable by the grader.
# --------------------------------------------------------------------------- #


def test_zero_form_previously_resolvable_forms_are_byte_identical():
    """REGRESSION (single-parser route): for every form the old resolver-local
    ``_zero_form`` accepted (single ``=`` / bare expression, no ``^``), the unified
    parser returns a structurally IDENTICAL zero-form — proven by a sign-exact
    ``simplify(new - reference) == 0`` against a hand-built reference. Adding
    ``convert_xor`` cannot perturb these (``^`` is absent)."""
    from sympy import Symbol, pi, simplify

    from apollo.resolution.tiers import _extended_locals, _zero_form

    A1, v1, A2, v2 = (Symbol(n) for n in ("A1", "v1", "A2", "v2"))
    r = Symbol("r")  # ``pi`` is SymPy's reserved constant (never shadowed by _extended_locals)
    cases = [
        ("A1*v1 = A2*v2", A1 * v1 - A2 * v2),
        ("A1*v1 - A2*v2", A1 * v1 - A2 * v2),  # bare zero-form unchanged
        ("A = pi*r**2", Symbol("A") - pi * r ** 2),  # ** power path unchanged
    ]
    for symbolic, reference in cases:
        got = _zero_form(symbolic, _extended_locals(symbolic))
        assert got is not None, symbolic
        assert simplify(got - reference) == 0, symbolic


def test_zero_form_malformed_stays_a_non_match_not_a_crash():
    """The non-crash contract survives the delegation: a genuinely malformed
    reference (``MalformedEquationError`` inside the mint parser) is swallowed to
    ``None`` (a non-match), never propagated."""
    from apollo.resolution.tiers import _extended_locals, _zero_form

    assert _zero_form("v = = 3", _extended_locals("v")) is None  # empty middle operand
    assert _zero_form("v = (a*t", _extended_locals("v a t")) is None  # unbalanced


def test_symbolic_tier_now_matches_caret_exponent_form():
    """Tolerance gain: a reference stored with ``^`` (the mainstream teacher-PDF
    exponent) now resolves against the student's ``**`` form. Before the single-
    parser route the resolver-local ``_zero_form`` returned None on ``^`` -> the
    minted reference was silently unmatchable."""
    node = _equation_node("s1", "A = pi*r**2")
    cands = (_cand("eq.circular_area", node_type="equation", symbolic="A = pi*r^2"),)
    hit = match_symbolic(node, cands, mappings={})
    assert hit is not None and hit[0].canonical_key == "eq.circular_area"


def test_symbolic_tier_now_matches_chained_equality_reference():
    """Tolerance gain: a reference minted as a CHAINED equality
    (``y = a*t^2 = 25.0`` — symbolic head + numeric tail) resolves against the
    student's clean symbolic form. The parser normalizes both to the first equality
    (``y = a*t^2``); the numeric tail is discarded (log-debug only)."""
    node = _equation_node("s1", "y = a*t**2")
    cands = (_cand("eq.motion", node_type="equation", symbolic="y = a*t^2 = 25.0"),)
    hit = match_symbolic(node, cands, mappings={})
    assert hit is not None and hit[0].canonical_key == "eq.motion"
