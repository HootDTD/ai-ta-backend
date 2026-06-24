"""WU-3C2 Step 2 — pure-unit tests for the closed candidate set + adapters.

No Docker, no network. These pin:
- ``METHOD_CONFIDENCE_CAP`` matches the §3 damper table exactly;
- ``build_candidate_set`` always appends the course misconceptions so they
  compete in every resolution (§5 guardrail);
- ``candidates_from_reference_solution`` yields one Candidate per reference
  step with the right ``node_type`` / ``canonical_key`` / ``symbolic``;
- ``candidates_from_misconceptions`` carries ``trigger_phrases`` as aliases
  and the ``opposes_key``.

The reference + misconception fixtures are the REAL hand-authored bernoulli
source files (problem_01.json / misconceptions.json), loaded from disk.
"""

from __future__ import annotations

import json
from pathlib import Path

from apollo.resolution.candidates import (
    METHOD_CONFIDENCE_CAP,
    RESOLUTION_METHODS,
    Candidate,
    build_candidate_set,
    candidates_from_misconceptions,
    candidates_from_reference_solution,
)

_BERNOULLI = (
    Path(__file__).resolve().parents[2]
    / "subjects"
    / "fluid_mechanics"
    / "concepts"
    / "bernoulli_principle"
)


def _load(name: str) -> dict:
    if name == "problem_01":
        return json.loads((_BERNOULLI / "problems" / "problem_01.json").read_text())
    return json.loads((_BERNOULLI / f"{name}.json").read_text())


def _canon_key_map() -> dict[str, int]:
    """Deterministic canonical_key -> :Canon surrogate key for the fixtures."""
    keys = [
        "eq.continuity",
        "cond.incompressibility",
        "eq.bernoulli",
        "simp.horizontal_simplification",
        "proc.plan_apply_continuity",
        "proc.plan_apply_horizontal_simplification",
        "proc.plan_solve_bernoulli_for_p2",
        "def.pressure_velocity_tradeoff",
        "misc.pressure_velocity_same_direction",
        "misc.density_ignored",
    ]
    return {k: i + 1 for i, k in enumerate(keys)}


def test_method_confidence_caps_match_spec():
    assert METHOD_CONFIDENCE_CAP == {
        "exact": 1.00,
        "symbolic": 0.98,
        "derived": 0.95,
        "alias": 0.92,
        "fuzzy": 0.80,
        "llm": 0.75,
        "unresolved": 0.00,
    }
    assert set(RESOLUTION_METHODS) == set(METHOD_CONFIDENCE_CAP)


def test_candidates_from_reference_solution_problem01():
    cands = candidates_from_reference_solution(
        _load("problem_01"), canon_key_by_canonical_key=_canon_key_map()
    )
    by_key = {c.canonical_key: c for c in cands}
    assert len(cands) == 7
    assert by_key["eq.bernoulli"].node_type == "equation"
    assert by_key["eq.bernoulli"].symbolic is not None
    assert by_key["cond.incompressibility"].node_type == "condition"
    assert by_key["simp.horizontal_simplification"].node_type == "simplification"
    assert by_key["proc.plan_apply_continuity"].node_type == "procedure_step"


def test_reference_variants_stay_distinct_candidates():
    """Over-normalization guardrail (§5): each reference step is its own
    candidate keyed by its own canonical_key — the resolver never merges
    candidates."""
    cands = candidates_from_reference_solution(
        _load("problem_01"), canon_key_by_canonical_key=_canon_key_map()
    )
    keys = [c.canonical_key for c in cands]
    assert len(keys) == len(set(keys))  # no two candidates share a key


def test_candidates_from_misconceptions_carry_aliases_and_opposes():
    cands = candidates_from_misconceptions(
        _load("misconceptions"), canon_key_by_canonical_key=_canon_key_map()
    )
    by_key = {c.canonical_key: c for c in cands}
    pvd = by_key["misc.pressure_velocity_same_direction"]
    assert pvd.is_misconception is True
    assert pvd.opposes_key == "def.pressure_velocity_tradeoff"
    assert "faster flow means higher pressure" in pvd.aliases


def test_build_candidate_set_appends_misconceptions():
    refs = candidates_from_reference_solution(
        _load("problem_01"), canon_key_by_canonical_key=_canon_key_map()
    )
    miscs = candidates_from_misconceptions(
        _load("misconceptions"), canon_key_by_canonical_key=_canon_key_map()
    )
    closed = build_candidate_set(reference_nodes=refs, misconception_entities=miscs)
    keys = {c.canonical_key for c in closed}
    # every misconception present so they compete in every resolution
    assert "misc.pressure_velocity_same_direction" in keys
    assert "misc.density_ignored" in keys
    # immutable tuple
    assert isinstance(closed, tuple)


def test_candidate_is_frozen_immutable():
    c = Candidate(
        canonical_key="eq.bernoulli",
        canon_key=3,
        node_type="equation",
        is_misconception=False,
        symbolic="P1 - P2",
        aliases=(),
        display_name="Bernoulli",
        opposes_key=None,
    )
    import dataclasses

    with __import__("pytest").raises(dataclasses.FrozenInstanceError):
        c.canonical_key = "x"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Phase 1b — Candidate.exact_aliases: curated reference phrasings matched
# EXACT-only (never fuzzed). The field defaults to () so every existing
# kwargs-based construction stays valid.
# ---------------------------------------------------------------------------


def test_candidate_exact_aliases_defaults_empty_and_is_frozen():
    c = Candidate(
        canonical_key="cond.x",
        canon_key=1,
        node_type="condition",
        is_misconception=False,
        symbolic=None,
        aliases=(),
        display_name="x",
        opposes_key=None,
    )
    assert c.exact_aliases == ()  # defaulted, backward-compatible
    c2 = Candidate(
        canonical_key="cond.y",
        canon_key=2,
        node_type="condition",
        is_misconception=False,
        symbolic=None,
        aliases=(),
        display_name="y",
        opposes_key=None,
        exact_aliases=("open to the atmosphere",),
    )
    assert c2.exact_aliases == ("open to the atmosphere",)
    import dataclasses

    with __import__("pytest").raises(dataclasses.FrozenInstanceError):
        c2.exact_aliases = ()  # type: ignore[misc]


def test_reference_aliases_flow_into_exact_aliases_not_aliases():
    """A reference-solution step's ``content.aliases`` lands in ``exact_aliases``
    (the EXACT-only channel) while ``aliases`` (the fuzzy channel) stays empty."""
    problem = {
        "reference_solution": [
            {
                "entry_type": "condition",
                "entity_key": "cond.open_tank",
                "content": {
                    "label": "Open tank",
                    "aliases": ["open to the atmosphere", "vented tank"],
                },
            }
        ]
    }
    cands = candidates_from_reference_solution(problem, canon_key_by_canonical_key={})
    c = cands[0]
    assert c.exact_aliases == ("open to the atmosphere", "vented tank")
    assert c.aliases == ()  # reference fuzzy channel stays empty


def test_reference_without_aliases_has_empty_exact_aliases():
    """No-aliases regression: a step WITHOUT ``content.aliases`` keeps
    ``exact_aliases == ()`` (the default path)."""
    problem = {
        "reference_solution": [
            {
                "entry_type": "condition",
                "entity_key": "cond.no_alias",
                "content": {"label": "No alias"},
            }
        ]
    }
    cands = candidates_from_reference_solution(problem, canon_key_by_canonical_key={})
    assert cands[0].exact_aliases == ()
