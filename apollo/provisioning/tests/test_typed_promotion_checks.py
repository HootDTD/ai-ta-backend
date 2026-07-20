"""Typed-path promotion profile: duplicate check + solve-and-check ONLY.

``run_typed_promotion_checks`` is the whole post-confirmation lint surface for a
manual typed problem. It must NOT enter former gates 1-7 (construction owns
those), must run gate 8 (duplicate) BEFORE gate 9 (solve-and-check), and must
preserve gate 9's verified/refuted/unresolved -> held three-way semantics and
result types (the honesty stamp downstream reads ``PromotionVerified``).
"""

from __future__ import annotations

from apollo.provisioning.problem_hash import problem_dup_hash
from apollo.provisioning.promotion_lint import (
    PromotionRefuted,
    PromotionResult,
    PromotionUnresolved,
    PromotionVerified,
    concept_symbol_diagnostic,
    run_typed_promotion_checks,
)
from apollo.schemas.problem import Problem


def _symbolic_graph(*, governing: str, stated: str, givens: dict, target: str = "Q") -> dict:
    """A minimal stated-answer symbolic graph that activates the solve-check layer."""
    return {
        "id": "typed_gate9",
        "concept_id": "provisional.inventory",
        "difficulty": "intro",
        "given_values": givens,
        "problem_text": f"Solve for {target}.",
        "target_unknown": target,
        "declared_paths": [["governing", "answer_key", "solve"]],
        "reference_solution": [
            {
                "id": "governing",
                "step": 1,
                "entry_type": "equation",
                "entity_key": "eq.governing",
                "content": {"label": "Governing equation", "symbolic": governing},
                "depends_on": [],
            },
            {
                "id": "answer_key",
                "step": 2,
                "entry_type": "equation",
                "entity_key": "eq.answer_key",
                "content": {"label": "Stated answer", "symbolic": f"{target} = {stated}"},
                "depends_on": ["governing"],
            },
            {
                "id": "solve",
                "step": 3,
                "entry_type": "procedure_step",
                "entity_key": "proc.solve",
                "content": {
                    "order": 1,
                    "action": f"solve for {target}",
                    "purpose": "obtain the stated answer",
                    "uses_equations": ["answer_key"],
                },
                "depends_on": ["answer_key"],
            },
        ],
    }


def _prose_graph() -> dict:
    """An equation-free argument: the solve-check layer must NOT apply."""
    return {
        "id": "typed_prose",
        "concept_id": "provisional.inventory",
        "difficulty": "intro",
        "given_values": {},
        "problem_text": "Argue whether federalism strengthens accountability.",
        "target_unknown": "whether federalism strengthens accountability",
        "declared_paths": [["federalism_meaning", "conclude"]],
        "reference_solution": [
            {
                "id": "federalism_meaning",
                "step": 1,
                "entry_type": "definition",
                "entity_key": "def.federalism_meaning",
                "content": {"concept": "federalism", "meaning": "divided sovereignty"},
                "depends_on": [],
            },
            {
                "id": "conclude",
                "step": 2,
                "entry_type": "procedure_step",
                "entity_key": "proc.conclude",
                "content": {"order": 1, "action": "weigh veto points", "purpose": "answer"},
                "depends_on": ["federalism_meaning"],
            },
        ],
    }


def _check(graph: dict, *, existing_hashes: set[str] | None = None) -> PromotionResult:
    return run_typed_promotion_checks(
        Problem.model_validate(graph),
        graph,
        normalization_map={},
        existing_problem_hashes=existing_hashes if existing_hashes is not None else set(),
    )


def test_correct_answer_is_verified_and_promotes() -> None:
    graph = _symbolic_graph(governing="Q = A*v", stated="0.06", givens={"A": 0.015, "v": 4.0})
    result = _check(graph)
    assert result.ok is True
    assert isinstance(result, PromotionVerified)  # -> "mechanically_verified" honesty stamp


def test_wrong_answer_is_refuted() -> None:
    graph = _symbolic_graph(governing="Q - A*v", stated="0.07", givens={"A": 0.015, "v": 4.0})
    result = _check(graph)
    assert result.ok is False
    assert result.failed_gate == 9
    assert isinstance(result, PromotionRefuted)
    assert result.verdict == "refuted"


def test_transcendental_system_is_unresolved_and_held() -> None:
    # A transcendental system SymPy cannot solve in closed form (or the 2s solver
    # timeout) yields the third verdict rather than a false refutation.
    graph = _symbolic_graph(governing="Q + cos(Q)", stated="0", givens={})
    result = _check(graph)
    assert result.ok is False
    assert result.failed_gate == 9
    assert isinstance(result, PromotionUnresolved)  # -> PromoteHeldForReview downstream


def test_prose_problem_skips_solve_check_and_promotes() -> None:
    result = _check(_prose_graph())
    assert result.ok is True
    assert type(result) is PromotionResult  # not verified: no mechanical oracle applied


def test_duplicate_is_gate_8_and_precedes_solve_check() -> None:
    # A CORRECT symbolic answer would pass gate 9; a duplicate must still fail
    # FIRST at gate 8, proving the duplicate check runs before solve-and-check.
    graph = _symbolic_graph(governing="Q = A*v", stated="0.06", givens={"A": 0.015, "v": 4.0})
    own_hash = problem_dup_hash(Problem.model_validate(graph))
    result = _check(graph, existing_hashes={own_hash})
    assert result.ok is False
    assert result.failed_gate == 8


def test_concept_symbol_diagnostic_isolates_gate_4_foreign_symbol() -> None:
    # Re-homing's foreign-symbol check (§4 review-flag path), run in isolation
    # from the rest of promotion: a symbol neither given nor canonical/
    # normalizable is flagged.
    graph = _symbolic_graph(governing="Q = A*v", stated="0.06", givens={"A": 0.015, "v": 4.0})
    diagnostic = concept_symbol_diagnostic(
        graph, canonical_symbols={"A", "v"}, normalization_map={}
    )
    assert diagnostic is not None
    assert "Q" in diagnostic


def test_concept_symbol_diagnostic_none_when_all_symbols_ground() -> None:
    graph = _symbolic_graph(governing="Q = A*v", stated="0.06", givens={"A": 0.015, "v": 4.0})
    diagnostic = concept_symbol_diagnostic(
        graph, canonical_symbols={"A", "v", "Q"}, normalization_map={}
    )
    assert diagnostic is None
