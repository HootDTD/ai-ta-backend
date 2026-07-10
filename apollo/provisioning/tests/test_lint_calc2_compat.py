"""The authored calc-2 gold standard must pass the promotion lint once the
graphs carry the annotations the derivation emits natively:

  - ``display: true`` on operator-identity equation steps (auto-detectable:
    the symbolic fails ``parse_zero_form``)
  - ``bound_variables``: the per-problem bound symbols (integration variable /
    series index / sample points and the opaque integrand symbol of numerical
    rules)

This test annotates the committed graphs mechanically the same way the
derivation LLM is instructed to, then asserts 60/60 lint PASS — the
product-path compatibility pin for the reversed-provisioning standard.
Verified pre-change baseline (2026-07-08): 37 PASS, 5 gate-6, 18 gate-7.
"""

import json
from pathlib import Path

import pytest

from apollo.persistence.learner_model_seed import (
    _entity_key_for_step,
    annotate_reference_solution,
)
from apollo.provisioning.promotion_lint import content_active_gates, run_promotion_lint
from apollo.solver.sympy_exec import MalformedEquationError, parse_zero_form

_ROOT = Path(__file__).resolve().parents[2] / "subjects" / "calculus_2" / "concepts"

# The bound symbols the derivation would declare per concept (integration
# variable, series index, endpoint/sample symbols of numerical rules, the
# opaque integrand symbol f). Every symbol class here is reflected in
# graph_derivation._DERIVATION_SYSTEM_PROMPT's bound_variables instruction.
_BOUND: dict[str, list[str]] = {
    "improper_integrals": ["x", "t", "u", "R"],
    "alternating_series": ["n", "k", "Ncut"],
    "ratio_root_tests": ["n", "k"],
    "comparison_tests": ["n", "k"],
    "numerical_integration": [
        "x",
        "f",
        "fpp",
        "f4",
        "x0",
        "x1",
        "x2",
        "x3",
        "x4",
        "x5",
        "x6",
        "xi",
        "c",
        # sampled integrand values f(x_i) / midpoint values f(m_i) written
        # f0..f6 / fm1..fm6 in the gold graphs
        "f0",
        "f1",
        "f2",
        "f3",
        "f4",
        "f5",
        "f6",
        "fm1",
        "fm2",
        "fm3",
        "fm4",
        "fm5",
        "fm6",
        # error-bound method symbols: the error term and the |f''| bound K
        "Et",
        "Em",
        "Es",
        "K",
    ],
    "integration_by_parts": ["x"],
    # undetermined coefficients: the decomposition template introduces A/B/C
    # and the PROCEDURE determines them (clearing denominators happens in
    # prose, so the paper check cannot see the determining equations)
    "partial_fractions": ["x", "A", "B", "C"],
    "trigonometric_integrals": ["x"],
    "trigonometric_substitution": ["x", "theta"],
    "u_substitution": ["x", "u"],
}


def _mark_display(problem: dict) -> dict:
    steps = []
    for raw in problem["reference_solution"]:
        s = dict(raw)
        content = dict(s.get("content") or {})
        if s.get("entry_type") == "equation" and content.get("symbolic"):
            try:
                parse_zero_form(str(content["symbolic"]), entry_id=str(s["id"]))
            except MalformedEquationError:
                content["display"] = True
        s["content"] = content
        steps.append(s)
    return {**problem, "reference_solution": steps}


@pytest.mark.parametrize(
    "path",
    sorted(_ROOT.glob("*/problems/problem_*.json")),
    ids=lambda p: p.parent.parent.name + "/" + p.name,
)
def test_gold_graph_promotes(path: Path) -> None:
    concept_dir = path.parent.parent
    sym = json.loads((concept_dir / "canonical_symbols.json").read_text())
    norm = json.loads((concept_dir / "normalization_map.json").read_text())
    problem = _mark_display(json.loads(path.read_text()))
    problem["bound_variables"] = _BOUND[concept_dir.name]
    steps = {s["id"]: s for s in problem["reference_solution"]}
    annotated = annotate_reference_solution(problem, lambda nid: _entity_key_for_step(steps[nid]))
    result = run_promotion_lint(
        annotated,
        canonical_symbols=set(sym["symbols"]),
        normalization_map=norm,
        existing_problem_hashes=set(),
        active_gates=content_active_gates(annotated),
    )
    assert result.ok, f"{path.name}: gate {result.failed_gate}: {result.diagnostic}"


# --------------------------------------------------------------------------- #
# Targeted gate-change pins (additive semantics; the fluid+macro anchor in
# test_promotion_lint.py stays byte-untouched).
# --------------------------------------------------------------------------- #


def _mini_problem(*, display: bool, bound: list[str] | None) -> dict:
    """A minimal annotated graph: one display identity + one concrete equation
    whose answer is function-valued (F and x remain free)."""
    problem: dict = {
        "id": "mini",
        "concept_id": "integration-by-parts",
        "difficulty": "standard",
        "problem_text": "Evaluate integral x e^x dx.",
        "given_values": {},
        "target_unknown": "F",
        "reference_solution": [
            {
                "step": 1,
                "entry_type": "equation",
                "id": "ibp_formula",
                "content": {
                    "label": "Integration by parts",
                    "symbolic": "integral u dv = u*v - integral v du",
                    **({"display": True} if display else {}),
                },
                "depends_on": [],
            },
            {
                "step": 2,
                "entry_type": "equation",
                "id": "antiderivative_result",
                "content": {
                    "label": "Result",
                    "symbolic": "F = x*exp(x) - exp(x) + C",
                },
                "depends_on": [],
            },
            {
                "step": 3,
                "entry_type": "procedure_step",
                "id": "apply_parts",
                "content": {
                    "order": 1,
                    "action": "apply parts and integrate the remainder",
                    "purpose": "produce F",
                    "uses_equations": ["antiderivative_result"],
                },
                "depends_on": ["ibp_formula"],
            },
        ],
    }
    if bound is not None:
        problem["bound_variables"] = bound
    steps = {s["id"]: s for s in problem["reference_solution"]}
    return annotate_reference_solution(problem, lambda nid: _entity_key_for_step(steps[nid]))


_SYMS = {"x", "u", "v", "F", "C"}


def _lint(annotated: dict):
    return run_promotion_lint(
        annotated,
        canonical_symbols=_SYMS,
        normalization_map={},
        existing_problem_hashes=set(),
        active_gates=content_active_gates(annotated),
    )


def test_gate_6_skips_display_marked_identity() -> None:
    result = _lint(_mini_problem(display=True, bound=["x", "C"]))
    assert result.ok, f"gate {result.failed_gate}: {result.diagnostic}"


def test_gate_6_still_rejects_unmarked_malformed_equation() -> None:
    result = _lint(_mini_problem(display=False, bound=["x", "C"]))
    assert not result.ok and result.failed_gate == 6


def test_gate_7_without_bound_variables_unchanged() -> None:
    result = _lint(_mini_problem(display=True, bound=None))
    assert not result.ok and result.failed_gate == 7
