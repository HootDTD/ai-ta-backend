"""Pure fixture tests for the §8B.4 eight-gate promotion lint.

No DB, no LLM, no mocks, no containers. The POSITIVE fixture is the seeded
bernoulli problem (the problem_01.json shape, inlined so mutations are visible
in-diff). Each adversarial fixture = the positive baseline + EXACTLY ONE
``_mutate`` and asserts ``failed_gate == N`` (the discriminating signal), so the
fixture goes RED iff its target gate is reverted (independent-mutation
discipline). Short-circuit-order tests prove the earliest failing gate wins.
"""

from __future__ import annotations

import copy

from apollo.ontology.graph import KGGraph
from apollo.ontology.nodes import build_node
from apollo.provisioning import PromotionResult, problem_dup_hash, run_promotion_lint
from apollo.provisioning.promotion_lint import _normalize_symbol
from apollo.schemas.problem import Problem


# --------------------------------------------------------------------------- #
# Central fixtures
# --------------------------------------------------------------------------- #


def _bernoulli_graph() -> dict:
    """The FULL annotated bernoulli problem dict (problem_01.json shape).

    Includes per-step ``entity_key`` + top-level ``declared_paths`` so it feeds
    BOTH ``Problem.model_validate`` (gate 1, which drops the extra keys) and
    ``validate_reference_graph`` (gate 2, which requires them). Passes all 8 gates.
    """
    return {
        "id": "bernoulli_horizontal_pipe_find_p2",
        "concept_id": "bernoulli_principle",
        "difficulty": "intro",
        "given_values": {
            "A1": 0.01,
            "A2": 0.005,
            "P1": 200000.0,
            "v1": 2.0,
            "rho": 1000.0,
        },
        "problem_text": (
            "Water flows through a horizontal pipe. At section 1 the area is "
            "0.01 m^2, the pressure is 200000 Pa, and the velocity is 2.0 m/s. "
            "At section 2 the area narrows to 0.005 m^2. Find the pressure P2."
        ),
        "target_unknown": "P2",
        "declared_paths": [
            [
                "continuity",
                "incompressibility",
                "bernoulli",
                "horizontal_simplification",
                "plan_apply_continuity",
                "plan_apply_horizontal_simplification",
                "plan_solve_bernoulli_for_p2",
            ]
        ],
        "reference_solution": [
            {
                "id": "continuity",
                "step": 1,
                "entry_type": "equation",
                "entity_key": "eq.continuity",
                "content": {
                    "label": "Continuity (mass conservation)",
                    "symbolic": "rho*A1*v1 - rho*A2*v2",
                    "variables": ["rho", "A1", "v1", "A2", "v2"],
                },
                "depends_on": [],
            },
            {
                "id": "incompressibility",
                "step": 2,
                "entry_type": "condition",
                "entity_key": "cond.incompressibility",
                "content": {
                    "label": "Incompressibility assumption",
                    "applies_when": "density is constant",
                },
                "depends_on": [],
            },
            {
                "id": "bernoulli",
                "step": 3,
                "entry_type": "equation",
                "entity_key": "eq.bernoulli",
                "content": {
                    "label": "Bernoulli's equation",
                    "symbolic": (
                        "P1 + Rational(1,2)*rho*v1**2 + rho*g*h1 "
                        "- (P2 + Rational(1,2)*rho*v2**2 + rho*g*h2)"
                    ),
                    "variables": ["P1", "rho", "v1", "g", "h1", "P2", "v2", "h2"],
                },
                "depends_on": ["incompressibility"],
            },
            {
                "id": "horizontal_simplification",
                "step": 4,
                "entry_type": "simplification",
                "entity_key": "simp.horizontal_simplification",
                "content": {
                    "applies_when": "h1 == h2",
                    "transformation": "rho*g*h1 and rho*g*h2 cancel",
                },
                "depends_on": ["bernoulli"],
            },
            {
                "id": "plan_apply_continuity",
                "step": 5,
                "entry_type": "procedure_step",
                "entity_key": "proc.plan_apply_continuity",
                "content": {
                    "order": 1,
                    "action": "use continuity with rho, A1, v1, A2 to solve for v2",
                    "purpose": "obtain v2 to plug into bernoulli at section 2",
                    "uses_equations": ["continuity"],
                },
                "depends_on": ["continuity"],
            },
            {
                "id": "plan_apply_horizontal_simplification",
                "step": 6,
                "entry_type": "procedure_step",
                "entity_key": "proc.plan_apply_horizontal_simplification",
                "content": {
                    "order": 2,
                    "action": "set h1 == h2 so the gravitational terms cancel",
                    "purpose": "simplify bernoulli to relate P1, P2, v1, v2",
                    "uses_equations": ["bernoulli"],
                },
                "depends_on": ["bernoulli", "horizontal_simplification"],
            },
            {
                "id": "plan_solve_bernoulli_for_p2",
                "step": 7,
                "entry_type": "procedure_step",
                "entity_key": "proc.plan_solve_bernoulli_for_p2",
                "content": {
                    "order": 3,
                    "action": "substitute v2 and known P1, rho, v1 and solve for P2",
                    "purpose": "produce the numerical answer for P2",
                    "uses_equations": ["bernoulli"],
                },
                "depends_on": [
                    "plan_apply_continuity",
                    "plan_apply_horizontal_simplification",
                ],
            },
        ],
    }


def _canonical_symbols() -> set[str]:
    return {"P", "rho", "v", "A", "h", "g", "Q"}


def _normalization_map() -> dict:
    return {
        "pressure": "P",
        "static pressure": "P",
        "density": "rho",
        "fluid density": "rho",
        "velocity": "v",
        "fluid velocity": "v",
        "speed": "v",
        "area": "A",
        "cross-sectional area": "A",
        "height": "h",
        "elevation": "h",
        "gravity": "g",
        "gravitational acceleration": "g",
        "flow rate": "Q",
    }


def _step(graph: dict, step_id: str) -> dict:
    for s in graph["reference_solution"]:
        if s["id"] == step_id:
            return s
    raise KeyError(step_id)


def _lint(graph: dict, *, existing_hashes=None) -> PromotionResult:
    return run_promotion_lint(
        graph,
        canonical_symbols=_canonical_symbols(),
        normalization_map=_normalization_map(),
        existing_problem_hashes=existing_hashes if existing_hashes is not None else set(),
    )


# --------------------------------------------------------------------------- #
# Positive
# --------------------------------------------------------------------------- #


def test_seeded_bernoulli_passes_all_eight_gates():
    result = _lint(_bernoulli_graph())
    assert result == PromotionResult(ok=True, failed_gate=None, diagnostic="")


# --------------------------------------------------------------------------- #
# Adversarial — one per gate, each asserts EXACTLY failed_gate == N
# --------------------------------------------------------------------------- #


def test_gate1_fires_on_uses_equations_pointing_at_non_equation():
    """A procedure_step whose ``uses_equations`` points at a NON-equation
    (``incompressibility`` is a condition) makes ``Problem._resolve_references``
    RAISE inside gate 1's ``model_validate`` -> failed_gate == 1.

    NOTE (test-honesty): this exercises gate 1's SCHEMA-VALIDATION path, not the
    downstream ``to_kg_graph`` forbidden-edge ``except`` (that branch is provably
    unreachable from a validated Problem and is marked ``# pragma: no cover`` —
    see ``run_promotion_lint``). The name reflects the path actually taken."""
    graph = copy.deepcopy(_bernoulli_graph())
    # incompressibility is a condition, not an equation.
    _step(graph, "plan_apply_continuity")["content"]["uses_equations"] = [
        "incompressibility"
    ]
    result = _lint(graph)
    assert result.ok is False
    assert result.failed_gate == 1


def test_gate1_accepts_variable_mapping_after_3b2d_map_extension():
    """variable_mapping is schema-legal AND, as of WU-3B2d's additive extension of
    the frozen ``_ENTRY_TYPE_TO_KIND_PREFIX`` (``variable_mapping -> (variable,
    varmap)``), is in the mint map — so gate-1's mint-map membership sub-check
    ACCEPTS it (it no longer fails CLOSED). Before 3B2d this asserted
    ``failed_gate == 1`` (ADJ #5 defense-in-depth); the extension is the unit that
    flips it. DISCRIMINATING: reverting the additive map key makes gate 1 fire
    again (``ok is False`` / ``failed_gate == 1``), so this RED-flags the revert."""
    graph = copy.deepcopy(_bernoulli_graph())
    step = _step(graph, "incompressibility")
    step["entry_type"] = "variable_mapping"
    step["content"] = {"term": "density", "symbol": "rho"}
    result = _lint(graph)
    # gate 1's mint-map sub-check no longer rejects variable_mapping; the graph is
    # otherwise valid, so the lint passes all 8 gates.
    assert result.failed_gate != 1, result.diagnostic
    assert result.ok is True


def test_gate2_fires_on_missing_entity_link():
    """Deleting one step's entity_key is schema-legal (Problem drops it) but
    closure-illegal -> gate 2."""
    graph = copy.deepcopy(_bernoulli_graph())
    del _step(graph, "bernoulli")["entity_key"]
    result = _lint(graph)
    assert result.ok is False
    assert result.failed_gate == 2


def test_gate3_fires_on_depends_on_cycle():
    """A DEPENDS_ON cycle (both ids exist -> passes Problem's existence check)
    -> topological_order raises -> gate 3."""
    graph = copy.deepcopy(_bernoulli_graph())
    # continuity <-> incompressibility cycle (both equations/conditions exist).
    _step(graph, "continuity")["depends_on"] = ["incompressibility"]
    _step(graph, "incompressibility")["depends_on"] = ["continuity"]
    result = _lint(graph)
    assert result.ok is False
    assert result.failed_gate == 3


def test_gate4_fires_on_foreign_symbol():
    """A foreign symbol x (not canonical, not normalizable) -> gate 4, the SOLE
    foreign-symbol guard."""
    graph = copy.deepcopy(_bernoulli_graph())
    _step(graph, "continuity")["content"]["symbolic"] = "rho*A1*v1 - rho*A2*v2 + x"
    result = _lint(graph)
    assert result.ok is False
    assert result.failed_gate == 4


def test_equation_free_symbols_is_independent_of_global_sympy_cache():
    """REGRESSION (test-honesty HIGH finding): ``_equation_free_symbols`` must
    return the SAME set regardless of SymPy's process-global symbol cache.

    ``parse_zero_form`` -> ``sympy.parse_expr`` auto-creates a foreign symbol from
    that cache; the observed flake was gate 4's foreign-symbol verdict going
    order-dependent (``x`` vanishing from ``free_symbols`` under some interleaving,
    so it slipped to gate 7). ``_equation_free_symbols`` now ``clear_cache()``s
    before every parse, so its output depends ONLY on the equation text. This test
    pins that directly: compute the free symbols, deliberately POISON the global
    cache with assumption-bearing variants of every symbol involved, recompute, and
    assert the two sets are IDENTICAL and still contain the foreign ``x``.

    DISCRIMINATION: with the ``clear_cache()`` line removed this still passes only
    because the harness cannot force the rare poisoning that triggers the drop; the
    load-bearing guarantee is the explicit cache clear, asserted via the
    cache-independence equality below (any future poisoning that DID affect a parse
    would break this equality)."""
    from sympy import Symbol
    from sympy.core.cache import clear_cache

    from apollo.provisioning.promotion_lint import _equation_free_symbols

    problem = Problem.model_validate(_bernoulli_graph())
    step = next(s for s in problem.reference_solution if s.id == "continuity")
    step.content["symbolic"] = "rho*A1*v1 - rho*A2*v2 + x"

    clear_cache()
    first = _equation_free_symbols(step)

    # Poison the global cache with assumption-bearing variants of EVERY symbol the
    # equation parses (name+assumptions are distinct cache keys; this is the closest
    # reproducible analogue of the cross-test leakage that caused the flake).
    poison = [
        Symbol(name, **kw)
        for name in ("x", "rho", "A1", "v1", "A2", "v2")
        for kw in (dict(zero=True), dict(positive=True), dict(real=True))
    ]
    assert poison  # keep references live so the cache stays primed

    second = _equation_free_symbols(step)

    assert first == second  # cache-independent
    assert "x" in second  # the foreign symbol survives -> gate 4 will fire


def test_gate4_fires_on_foreign_symbol_under_poisoned_cache():
    """End-to-end companion to the cache-independence test: even with the global
    SymPy cache poisoned BEFORE the lint runs, gate 4 (the sole foreign-symbol
    guard) still fires on a foreign ``x`` -> ``failed_gate == 4`` (NOT 7)."""
    from sympy import Symbol

    poison = [Symbol("x", zero=True), Symbol("x", positive=True), Symbol("x")]
    assert poison  # prime the cache before the lint

    graph = copy.deepcopy(_bernoulli_graph())
    _step(graph, "continuity")["content"]["symbolic"] = "rho*A1*v1 - rho*A2*v2 + x"
    result = _lint(graph)
    assert result.ok is False
    assert result.failed_gate == 4  # determinism pin holds: 4, never 7


def test_gate5_fires_on_terminal_not_computing_target():
    """Terminal procedure step uses ONLY continuity (which lacks P2==target)
    -> gate 5 terminal-computes-target sub-clause."""
    graph = copy.deepcopy(_bernoulli_graph())
    _step(graph, "plan_solve_bernoulli_for_p2")["content"]["uses_equations"] = [
        "continuity"
    ]
    result = _lint(graph)
    assert result.ok is False
    assert result.failed_gate == 5


def test_gate6_fires_on_malformed_equation():
    """A dangling operator -> parse_zero_form raises MalformedEquationError
    -> gate 6. (gate 4 SKIPS malformed equations so it must NOT steal this.)"""
    graph = copy.deepcopy(_bernoulli_graph())
    _step(graph, "continuity")["content"]["symbolic"] = "rho*A1*v1 - = P2"
    result = _lint(graph)
    assert result.ok is False
    assert result.failed_gate == 6


def test_gate7_fires_on_unclosed_system():
    """A free symbol Q that IS canonical (gate 4 passes) but has no
    given/target/intermediate/cancellation path -> gate 7 paper-closure check."""
    graph = copy.deepcopy(_bernoulli_graph())
    _step(graph, "continuity")["content"]["symbolic"] = "rho*A1*v1 - rho*A2*v2 + Q"
    result = _lint(graph)
    assert result.ok is False
    assert result.failed_gate == 7


def test_gate8_fires_on_duplicate():
    """The only gate reading existing_problem_hashes."""
    graph = _bernoulli_graph()
    dup_hash = problem_dup_hash(Problem.model_validate(graph))
    result = _lint(graph, existing_hashes={dup_hash})
    assert result.ok is False
    assert result.failed_gate == 8


# --------------------------------------------------------------------------- #
# Short-circuit ORDER (first failing gate wins)
# --------------------------------------------------------------------------- #


def test_short_circuit_reports_earliest_gate():
    """A problem failing BOTH gate 3 (cycle) AND gate 8 (dup) reports 3."""
    graph = copy.deepcopy(_bernoulli_graph())
    _step(graph, "continuity")["depends_on"] = ["incompressibility"]
    _step(graph, "incompressibility")["depends_on"] = ["continuity"]
    dup_hash = problem_dup_hash(Problem.model_validate(_bernoulli_graph()))
    result = _lint(graph, existing_hashes={dup_hash})
    assert result.ok is False
    assert result.failed_gate == 3


def test_short_circuit_gate1_precedes_all():
    """A schema-broken problem that would ALSO fail later gates reports 1."""
    graph = copy.deepcopy(_bernoulli_graph())
    # Break schema (empty problem_text -> ValidationError) AND seed a dup.
    graph["problem_text"] = ""
    dup_hash = problem_dup_hash(Problem.model_validate(_bernoulli_graph()))
    result = _lint(graph, existing_hashes={dup_hash})
    assert result.ok is False
    assert result.failed_gate == 1


# --------------------------------------------------------------------------- #
# White-box helper tests (keep pure branches covered)
# --------------------------------------------------------------------------- #


def test_normalize_symbol_accepts_subscripted_base():
    assert _normalize_symbol("P2", {"P", "v"}, {}) == "P"
    assert _normalize_symbol("v1", {"P", "v"}, {}) == "v"
    assert _normalize_symbol("h12", {"h"}, {}) == "h"
    assert _normalize_symbol("x", {"P"}, {}) is None


def test_normalize_symbol_uses_normalization_map():
    assert _normalize_symbol("static pressure", set(), {"static pressure": "P"}) == "P"


def _forked_chain_graph() -> KGGraph:
    """Two procedure_steps with NO incoming PRECEDES = two heads. This shape
    cannot arise from a schema-valid Problem (to_kg_graph always builds one
    linear chain), so the single-chain-head branch is covered white-box."""
    nodes = [
        build_node(
            node_type="procedure_step",
            node_id="p_a",
            attempt_id=0,
            source="reference",
            content={"action": "a", "purpose": ""},
        ),
        build_node(
            node_type="procedure_step",
            node_id="p_b",
            attempt_id=0,
            source="reference",
            content={"action": "b", "purpose": ""},
        ),
    ]
    # No PRECEDES edges at all -> both nodes are heads.
    return KGGraph(nodes=nodes, edges=[])


def test_gate5_chain_helper_rejects_forked_chain():
    from apollo.provisioning.promotion_lint import _gate_5

    forked = _forked_chain_graph()
    problem = Problem.model_validate(_bernoulli_graph())
    diag = _gate_5(problem, forked)
    assert diag is not None  # two heads -> single-chain-head branch fires


def _proc_node(node_id: str):
    return build_node(
        node_type="procedure_step",
        node_id=node_id,
        attempt_id=0,
        source="reference",
        content={"action": node_id, "purpose": ""},
    )


def _precedes_edge(from_id: str, to_id: str):
    from apollo.ontology.edges import Edge, EdgeType

    return Edge(
        edge_type=EdgeType.PRECEDES,
        from_node_id=from_id,
        to_node_id=to_id,
        attempt_id=0,
        source="reference",
        from_node_type="procedure_step",
        to_node_type="procedure_step",
    )


def test_gate5_chain_helper_rejects_incomplete_coverage():
    """Exactly ONE head (p_a) but a second component (p_c<->p_d cycle, both with
    incoming PRECEDES) the walk never reaches -> the chain-coverage branch fires
    (distinct from the head-count branch)."""
    from apollo.provisioning.promotion_lint import _gate_5

    nodes = [_proc_node("p_a"), _proc_node("p_b"), _proc_node("p_c"), _proc_node("p_d")]
    edges = [
        _precedes_edge("p_a", "p_b"),  # head walks p_a -> p_b (len 2)
        _precedes_edge("p_c", "p_d"),  # p_c, p_d each have incoming -> not heads
        _precedes_edge("p_d", "p_c"),
    ]
    kg = KGGraph(nodes=nodes, edges=edges)
    problem = Problem.model_validate(_bernoulli_graph())
    diag = _gate_5(problem, kg)
    assert diag is not None
    assert "chain covers" in diag


def test_gate5_chain_helper_rejects_terminal_with_no_equation():
    """A single linear chain whose terminal step has empty uses_equations hits
    the terminal-uses-no-equation branch."""
    from apollo.provisioning.promotion_lint import _gate_5

    graph = copy.deepcopy(_bernoulli_graph())
    _step(graph, "plan_solve_bernoulli_for_p2")["content"]["uses_equations"] = []
    problem = Problem.model_validate(graph)
    kg = problem.to_kg_graph(attempt_id=0)
    diag = _gate_5(problem, kg)
    assert diag is not None
    assert "uses no equation" in diag


def test_gate6_skips_equation_without_symbolic():
    """An equation step lacking a ``symbolic`` is gate-1's structural concern;
    gate 6 defensively continues past it."""
    from apollo.provisioning.promotion_lint import _gate_6

    problem = Problem.model_validate(_bernoulli_graph())
    # Blank out one equation's symbolic on the typed view (gate 6 reads content).
    for step in problem.reference_solution:
        if step.id == "continuity":
            step.content.pop("symbolic", None)
    assert _gate_6(problem) is None


def test_equation_free_symbols_empty_when_symbolic_absent():
    from apollo.provisioning.promotion_lint import _equation_free_symbols

    problem = Problem.model_validate(_bernoulli_graph())
    step = next(s for s in problem.reference_solution if s.id == "continuity")
    step.content.pop("symbolic", None)
    assert _equation_free_symbols(step) == set()


def test_cancelled_symbols_reads_variables_list():
    """The cancellation set also picks up a simplification's content.variables."""
    from apollo.provisioning.promotion_lint import _cancelled_symbols

    problem = Problem.model_validate(_bernoulli_graph())
    for step in problem.reference_solution:
        if step.entry_type == "simplification":
            step.content["variables"] = ["zeta"]
    assert "zeta" in _cancelled_symbols(problem)


def test_gate7_skips_non_equation_id_in_nonterminal_uses_equations():
    """White-box (test-honesty LOW finding): exercise the ``if u in free_by_eq``
    FALSE arm of ``_gate_7``'s intermediate-collection loop.

    A validated ``Problem`` forbids ``uses_equations`` pointing at a non-equation
    (``_resolve_references`` raises), so this path is only reachable by mutating
    the typed view post-validation. We point a NON-terminal procedure step's
    ``uses_equations`` at ``incompressibility`` (a CONDITION id, NOT in
    ``free_by_eq``). The false arm must simply SKIP that id without crashing
    (gate 7 collects intermediates only from real equation ids)."""
    from apollo.provisioning.promotion_lint import (
        _equation_free_symbols,
        _equation_steps,
        _gate_7,
    )

    problem = Problem.model_validate(_bernoulli_graph())
    free_by_eq = {s.id: _equation_free_symbols(s) for s in _equation_steps(problem)}
    assert "incompressibility" not in free_by_eq  # the non-equation id we point at

    for step in problem.reference_solution:
        if step.id == "plan_apply_continuity":  # a NON-terminal procedure step
            step.content["uses_equations"] = ["incompressibility"]

    # The false arm skips the non-equation id without raising (KeyError would mean
    # the guard is missing). Behavior is well-defined: gate 7 returns a verdict.
    diag = _gate_7(problem)
    assert diag is None or isinstance(diag, str)


def test_gate5_passes_on_real_bernoulli():
    """Sanity: the white-box gate helper passes the real graph (so the helper
    test above discriminates the fork, not a blanket fail)."""
    from apollo.provisioning.promotion_lint import _gate_5

    problem = Problem.model_validate(_bernoulli_graph())
    kg = problem.to_kg_graph(attempt_id=0)
    assert _gate_5(problem, kg) is None
