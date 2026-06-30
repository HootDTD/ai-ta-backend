"""Answer-blind probe-hint construction tests. The core guarantee: hints reveal
DIMENSION only, never candidate values/claims/symbols."""

from apollo.clarification.probe import build_probe_hint
from apollo.resolution.candidates import Candidate
from apollo.resolution.tests.test_resolver import _node


def _cand(node_type, display, aliases=(), symbolic=None):
    return Candidate(
        canonical_key="cond.k",
        canon_key=1,
        node_type=node_type,
        is_misconception=False,
        symbolic=symbolic,
        aliases=aliases,
        display_name=display,
        opposes_key=None,
        exact_aliases=(),
    )


def test_hint_never_leaks_candidate_value():
    """Core guarantee: hint must not leak any candidate property."""
    cand = _cand(
        "condition",
        "pressure is LOWER where flow is faster",
        aliases=("inverse pressure-velocity",),
        symbolic="P+0.5*rho*v^2=const",
    )
    node = _node("s1", "condition", {"applies_when": "pressure and speed are related"})
    hint = build_probe_hint(node, cand)

    # Collect all potentially leaky tokens
    leaky = [
        "LOWER",
        "lower",
        "inverse pressure-velocity",
        "P+0.5",
        cand.display_name,
        *cand.aliases,
    ]

    # No leaky token may appear in the hint
    for token in leaky:
        assert token not in hint, f"Hint leaked candidate token: {token}"

    # Hint must be non-empty steering
    assert hint


def test_hint_names_the_dimension_per_node_type():
    """Hint should name the dimension to pin down, derived from node type."""
    # Condition: should mention direction/relationship
    hint_cond = build_probe_hint(
        _node("s1", "condition", {"applies_when": "x"}), _cand("condition", "d")
    )
    assert "direction" in hint_cond.lower()

    # Equation: should mention variable
    hint_eq = build_probe_hint(_node("s2", "equation", {"symbolic": "x"}), _cand("equation", "d"))
    assert "variable" in hint_eq.lower()

    # Definition: should mention define
    hint_def = build_probe_hint(
        _node("s3", "definition", {"concept": "x", "meaning": "y"}),
        _cand("definition", "d"),
    )
    assert "define" in hint_def.lower()


def test_hint_fallback_for_unknown_node_type():
    """Coverage: unknown node types fall back to a generic steering hint.

    Since all 6 valid NodeTypes are in _HINT_BY_TYPE, test the fallback by
    temporarily modifying the hint map to exclude a type, then restore it."""
    from apollo.clarification import probe

    # Save original map
    original_map = probe._HINT_BY_TYPE.copy()

    try:
        # Remove one type to trigger fallback
        del probe._HINT_BY_TYPE["procedure_step"]

        hint = build_probe_hint(
            _node("s4", "procedure_step", {"action": "something"}),
            _cand("procedure_step", "d"),
        )

        # Fallback should be returned
        assert hint == probe._FALLBACK
        assert "precise" in hint.lower() and "claim" in hint.lower()
    finally:
        # Restore original map
        probe._HINT_BY_TYPE = original_map
