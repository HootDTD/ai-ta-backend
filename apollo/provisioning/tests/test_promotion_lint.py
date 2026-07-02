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
import json
import pathlib

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
    _step(graph, "plan_apply_continuity")["content"]["uses_equations"] = ["incompressibility"]
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
    _step(graph, "plan_solve_bernoulli_for_p2")["content"]["uses_equations"] = ["continuity"]
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


def test_gate5_terminal_with_no_equation_passes_structural_half():
    """INVERTED under Option 2: a single linear chain whose terminal step has empty
    uses_equations now PASSES the structural half — the unique terminal sink is the
    kind-agnostic target-reachability property, and there is no parseable terminal
    equation to run the symbolic half on. (Old code hard-rejected at the
    'terminal uses no equation' branch; that branch is gone.)"""
    from apollo.provisioning.promotion_lint import _gate_5

    graph = copy.deepcopy(_bernoulli_graph())
    _step(graph, "plan_solve_bernoulli_for_p2")["content"]["uses_equations"] = []
    problem = Problem.model_validate(graph)
    kg = problem.to_kg_graph(attempt_id=0)
    assert _gate_5(problem, kg) is None  # structural sink present; no symbolic half to run


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


# --------------------------------------------------------------------------- #
# Subject-fluid Apollo — profile-driven active_gates (gates 4/5 OFF for arguments)
# --------------------------------------------------------------------------- #


def _argument_graph() -> dict:
    """A prose ARGUMENT reference graph (qualitative_argumentative node vocab:
    procedure_step / definition / condition; NO equations). Annotated (entity_key
    per step + declared_paths) so it feeds gate 1 (schema, which drops the extras)
    AND gate 2 (closure, which requires them). ``target_unknown`` is PROSE and
    ``given_values`` is empty — exactly what an argument carries."""
    return {
        "id": "polisci_federalism_disperses_power",
        "concept_id": "federalism",
        "difficulty": "standard",
        "given_values": {},
        "problem_text": (
            "Argue whether a federal system strengthens or weakens democratic "
            "accountability."
        ),
        "target_unknown": "whether federalism strengthens accountability",
        "declared_paths": [
            ["def_federalism", "premise_dispersed_power", "step_veto_points", "step_conclusion"]
        ],
        "reference_solution": [
            {
                "id": "def_federalism",
                "step": 1,
                "entry_type": "definition",
                "entity_key": "def.federalism",
                "content": {
                    "concept": "federalism",
                    "meaning": "Sovereignty divided between national and subnational units.",
                },
                "depends_on": [],
            },
            {
                "id": "premise_dispersed_power",
                "step": 2,
                "entry_type": "condition",
                "entity_key": "cond.dispersed_power",
                "content": {
                    "applies_when": "authority is constitutionally split across levels",
                },
                "depends_on": ["def_federalism"],
            },
            {
                "id": "step_veto_points",
                "step": 3,
                "entry_type": "procedure_step",
                "entity_key": "proc.veto_points",
                "content": {
                    "order": 1,
                    "action": "identify the multiple veto points federalism creates",
                    "purpose": "establish that power is checked at several levels",
                },
                "depends_on": ["premise_dispersed_power"],
            },
            {
                "id": "step_conclusion",
                "step": 4,
                "entry_type": "procedure_step",
                "entity_key": "proc.conclusion",
                "content": {
                    "order": 2,
                    "action": "weigh dispersed checks against blurred responsibility",
                    "purpose": "reach a reasoned verdict on accountability",
                },
                "depends_on": ["step_veto_points"],
            },
        ],
    }


def test_argument_graph_promotes_under_content_derived_gates():
    """Subject-agnostic (Option 2): a prose argument graph (no equations) PROMOTES.
    ``content_active_gates`` drops the symbolic gates {4,6,7}, and the prose target
    is no longer treated as a foreign symbol (gate 4's ``target_unknown`` add is
    gone). Under the OLD subject-fluid code the prose target made gate 4 fire — the
    bug a profile had to switch off; it is now fixed STRUCTURALLY, no profile
    needed."""
    from apollo.provisioning.promotion_lint import content_active_gates

    g = _argument_graph()
    result = run_promotion_lint(
        g,
        canonical_symbols=_canonical_symbols(),
        normalization_map=_normalization_map(),
        existing_problem_hashes=set(),
        active_gates=content_active_gates(g),
    )
    assert result.ok is True, result.diagnostic


def test_argument_graph_promotes_under_qualitative_active_gates():
    """The SAME graph passes when the qualitative_argumentative profile turns gates
    4/5 off (active_gates={1,2,3,8}): gates 1 (schema+mint map, where
    definition/condition/procedure_step are all in the map), 2 (closure), 3 (DAG)
    and 8 (dedup) all pass on a prose argument graph. DISCRIMINATING: drop the
    ``if number not in active_gates`` skip and gate 4 fires again -> RED."""
    result = run_promotion_lint(
        _argument_graph(),
        canonical_symbols=_canonical_symbols(),
        normalization_map=_normalization_map(),
        existing_problem_hashes=set(),
        active_gates=frozenset({1, 2, 3, 8}),
    )
    assert result.ok is True, result.diagnostic
    assert result.failed_gate is None


def test_default_active_gates_is_all_eight_back_compat():
    """Explicitly passing ALL_PROMOTION_GATES is identical to omitting active_gates
    (the back-compat contract): the seeded bernoulli still passes all eight."""
    from apollo.provisioning.promotion_lint import ALL_PROMOTION_GATES

    result = run_promotion_lint(
        _bernoulli_graph(),
        canonical_symbols=_canonical_symbols(),
        normalization_map=_normalization_map(),
        existing_problem_hashes=set(),
        active_gates=ALL_PROMOTION_GATES,
    )
    assert result == PromotionResult(ok=True, failed_gate=None, diagnostic="")


def test_qualitative_gates_still_catch_structural_failures():
    """Turning 4/5 off must NOT make the qualitative profile a rubber stamp: a
    DEPENDS_ON cycle still fails gate 3, and a missing entity_key still fails gate
    2, under active_gates={1,2,3,8}."""
    cyclic = copy.deepcopy(_argument_graph())
    _step(cyclic, "def_federalism")["depends_on"] = ["step_conclusion"]
    _step(cyclic, "step_conclusion")["depends_on"] = ["def_federalism"]
    r_cycle = run_promotion_lint(
        cyclic,
        canonical_symbols=set(),
        normalization_map={},
        existing_problem_hashes=set(),
        active_gates=frozenset({1, 2, 3, 8}),
    )
    assert r_cycle.ok is False
    assert r_cycle.failed_gate == 3

    no_link = copy.deepcopy(_argument_graph())
    del _step(no_link, "premise_dispersed_power")["entity_key"]
    r_link = run_promotion_lint(
        no_link,
        canonical_symbols=set(),
        normalization_map={},
        existing_problem_hashes=set(),
        active_gates=frozenset({1, 2, 3, 8}),
    )
    assert r_link.ok is False
    assert r_link.failed_gate == 2


# --------------------------------------------------------------------------- #
# WU-3B2b-SA Phase 1 — characterization / differential oracle (subject-agnostic)
#
# The regression oracle for the subject-agnostic gate change. Two corpora:
#   * BACK-COMPAT ANCHOR — the in-repo stand-in for the "41 seeded ss=2 :Canon"
#     (they were seeded FROM this repo content): the inlined bernoulli fixture +
#     the 10 seed JSONs under apollo/subjects/*/concepts/*/problems/. The
#     differential test locks old==new so the behavior change never moves them.
#   * AAE 333 FORWARD FIXTURES — apollo/provisioning/tests/fixtures/aae333_0*.json.
#     Real Purdue AAE 333 problem statements/targets/givens pulled read-only from
#     staging (ss=4/doc=6/run=2); the reference SOLUTIONS are reconstructed (the
#     live ones were never persisted — rejected_problems carry payload={}), each a
#     well-formed symbolic system with a single graph-derived answer, calibrated to
#     reproduce the documented live reject (5x gate5, 1x gate4). Phase 2 inverts the
#     snapshot test to assert these PROMOTE.
# --------------------------------------------------------------------------- #

_SEED_ROOT = pathlib.Path(__file__).resolve().parents[2] / "subjects"
_FIXTURE_DIR = pathlib.Path(__file__).resolve().parent / "fixtures"


def _seed_anchor_inputs() -> list[tuple[dict, set, dict]]:
    """(graph, canonical_symbols, normalization_map) for each in-repo seed JSON.

    The per-concept symbol table is loaded from the seed concept dir so gate 4 is
    non-vacuous (the SAME table old and new read — the differential holds for any
    table, but the real table exercises the symbolic path)."""
    out: list[tuple[dict, set, dict]] = []
    for pj in sorted(_SEED_ROOT.glob("*/concepts/*/problems/problem_*.json")):
        g = json.loads(pj.read_text())
        cdir = pj.parents[1]
        cs_f, nm_f = cdir / "canonical_symbols.json", cdir / "normalization_map.json"
        cs = json.loads(cs_f.read_text()) if cs_f.exists() else {}
        canon = set(cs.get("symbols") or []) if isinstance(cs, dict) else set(cs)
        nm = json.loads(nm_f.read_text()) if nm_f.exists() else {}
        out.append((g, canon, nm if isinstance(nm, dict) else {}))
    return out


def _anchor_inputs() -> list[tuple[dict, set, dict]]:
    return [(_bernoulli_graph(), _canonical_symbols(), _normalization_map())] + _seed_anchor_inputs()


def _old_lint(g: dict, canon: set, norm: dict) -> PromotionResult:
    """The pre-change pipeline: all eight gates (the default active set)."""
    return run_promotion_lint(
        g, canonical_symbols=canon, normalization_map=norm, existing_problem_hashes=set()
    )


def _new_lint(g: dict, canon: set, norm: dict) -> PromotionResult:
    """The post-change pipeline: the caller computes ``content_active_gates(g)`` and
    passes it as the active set (exactly what ``promote`` does in Step 2.5). Every
    anchor problem carries equations, so content-derived applicability self-activates
    all eight gates — this differential then proves the content-derivation does not
    move a single anchor verdict (the §5 back-compat anchor; asymmetric safety: a
    false-GREEN must never ship)."""
    from apollo.provisioning.promotion_lint import content_active_gates

    return run_promotion_lint(
        g,
        canonical_symbols=canon,
        normalization_map=norm,
        existing_problem_hashes=set(),
        active_gates=content_active_gates(g),
    )


def test_new_pipeline_equals_old_on_back_compat_anchor():
    """DIFFERENTIAL LOCK: for every anchor input, the new pipeline returns the
    IDENTICAL (ok, failed_gate) as the old. Subject-neutral — bakes in zero subject
    assumptions; it only asserts new == old wherever the content path applies."""
    inputs = _anchor_inputs()
    assert len(inputs) == 11  # inlined bernoulli + 10 seed JSONs
    for g, canon, norm in inputs:
        old = _old_lint(g, canon, norm)
        new = _new_lint(g, canon, norm)
        assert (old.ok, old.failed_gate) == (new.ok, new.failed_gate), g.get("id")


def test_back_compat_anchor_all_promote_today():
    """ABSOLUTE snapshot: every anchor input PASSES all eight gates on current code.
    Pins the concrete verdict so a regression that moves old AND new together (which
    the differential alone would miss) is still caught."""
    for g, canon, norm in _anchor_inputs():
        r = _old_lint(g, canon, norm)
        assert r.ok is True, (g.get("id"), r.diagnostic)


def _load_aae333() -> list[tuple[str, dict, set, dict, int]]:
    expected = json.loads((_FIXTURE_DIR / "aae333_expected.json").read_text())
    out: list[tuple[str, dict, set, dict, int]] = []
    for name, meta in sorted(expected.items()):
        g = json.loads((_FIXTURE_DIR / f"{name}.json").read_text())
        out.append((name, g, set(meta["canonical_symbols"]), meta["normalization_map"],
                    int(meta["failed_gate"])))
    return out


def test_aae333_now_promotes_under_content_derived_gates():
    """FORWARD PROOF (spec §1 fix): the 6 reconstructed AAE 333 fixtures that the
    live E2E rejected 0/6 (5×gate5, 1×gate4 — see ``aae333_expected.json``) now ALL
    PROMOTE under the subject-agnostic gates. The 5 covering-table cases pass via the
    graph-derived symbolic answer (gate 5 symbolic half + gate 7 closure on a single
    unknown); ``aae333_06`` (empty table) passes via internal symbol grounding
    (table-less gate 4). The active set is content-derived, exactly as ``promote``
    computes it."""
    from apollo.provisioning.promotion_lint import content_active_gates

    rows = _load_aae333()
    assert len(rows) == 6
    for name, g, canon, norm, _historical_gate in rows:
        r = run_promotion_lint(
            g,
            canonical_symbols=canon,
            normalization_map=norm,
            existing_problem_hashes=set(),
            active_gates=content_active_gates(g),
        )
        assert r.ok is True, (name, r.failed_gate, r.diagnostic)


# --------------------------------------------------------------------------- #
# WU-3B2b-SA Phase 2 — content-derived applicability (Step 2.1)
#
# The active-gate set is no longer a stored subject profile: it is DERIVED from
# the problem's own content. Structural gates {1,2,3,5,8} ALWAYS apply; the
# symbolic rigor gates {4,6,7} self-activate ONLY when a parseable equation step
# is present (spec §4 tier 2 "self-activating deterministic rigor"). Gate 5 is
# SPLIT (structural half always-on, symbolic half self-activates inside the gate),
# so it stays in the always-on set.
# --------------------------------------------------------------------------- #


def test_content_active_gates_equationless_graph_drops_symbolic():
    """A prose argument graph (no parseable equations) -> only the structural
    gates {1,2,3,5,8} apply; the symbolic rigor gates {4,6,7} are NOT in the
    content-derived active set, so a rigor gate can never reject content it does
    not apply to (spec §4 the additive-oracle safety property)."""
    from apollo.provisioning.promotion_lint import content_active_gates

    assert content_active_gates(_argument_graph()) == frozenset({1, 2, 3, 5, 8})


def test_content_active_gates_symbolic_graph_keeps_all_eight():
    """A symbolic graph (>=1 parseable equation) self-activates the symbolic rigor
    gates {4,6,7}, so the content-derived active set is all eight — byte-identical
    to today's default for the back-compat anchor."""
    from apollo.provisioning.promotion_lint import content_active_gates

    assert content_active_gates(_bernoulli_graph()) == frozenset({1, 2, 3, 4, 5, 6, 7, 8})


def test_content_active_gates_schema_broken_graph_keeps_all_gates():
    """A schema-broken graph (cannot even validate as a Problem) keeps ALL gates
    active so gate 1 still fires on it — fail-closed, never fail-open."""
    from apollo.provisioning.promotion_lint import content_active_gates

    broken = copy.deepcopy(_bernoulli_graph())
    broken["reference_solution"] = "not a list"  # ValidationError on model_validate
    assert content_active_gates(broken) == frozenset({1, 2, 3, 4, 5, 6, 7, 8})


# --------------------------------------------------------------------------- #
# WU-3B2b-SA Phase 3 — Direction-B named tiers (structural core + rigor layers)
#
# The lint's three tiers are NAMED in code: a STRUCTURAL CORE (always-on floor) +
# a list of RIGOR LAYERS (each a pair (applies?, gate_numbers); v1 ships exactly
# one — the symbolic layer) + the faithfulness oracle (pairing_gate, run by the
# orchestrator). content_active_gates is DRIVEN by this surface, so the safety
# property — a rigor layer's gates activate ONLY when it applies — is structurally
# enforced (a layer physically cannot block content it does not apply to).
# --------------------------------------------------------------------------- #


def test_direction_b_named_tiers_exist_and_symbolic_layer_is_the_only_one():
    from apollo.provisioning.promotion_lint import RIGOR_LAYERS, STRUCTURAL_CORE_GATES

    assert STRUCTURAL_CORE_GATES == frozenset({1, 2, 3, 5, 8})
    assert len(RIGOR_LAYERS) == 1  # v1 ships exactly the symbolic layer
    applies, gates = RIGOR_LAYERS[0]
    assert set(gates) == {4, 6, 7}
    # the symbolic layer APPLIES iff parseable equations are present
    assert applies(_bernoulli_graph()) is True
    assert applies(_argument_graph()) is False


def test_rigor_layer_gates_activate_only_when_applicable():
    """The STRUCTURAL safety property: a layer that does not apply contributes NO
    gates to the active set, so it can only ever ADD a rejection to content it
    applies to. content_active_gates is the structural enforcement point."""
    from apollo.provisioning.promotion_lint import (
        RIGOR_LAYERS,
        STRUCTURAL_CORE_GATES,
        content_active_gates,
    )

    _applies, gates = RIGOR_LAYERS[0]
    # equation-less graph: the symbolic layer does NOT apply -> only the core runs
    assert content_active_gates(_argument_graph()) == STRUCTURAL_CORE_GATES
    # symbolic graph: the layer applies -> its gates join the core
    assert content_active_gates(_bernoulli_graph()) == STRUCTURAL_CORE_GATES | set(gates)


# --------------------------------------------------------------------------- #
# WU-3B2b-SA Phase 2 — graph-derived symbolic answer + gate 5 (Step 2.2, Option 2)
#
# The symbolic answer is DERIVED FROM THE GRAPH, not read from the prose
# ``target_unknown`` (spec §4.1 "the node carrying the answer is what the chain
# terminates in"). ``_derive_symbolic_answer`` = (all equation free symbols) -
# givens - intermediates - cancelled, reusing gate 7's existing intermediate /
# cancellation definitions. For a closed system it is size 0 or 1; the single
# element is the answer. Gate 5's symbolic half checks the terminal equation
# computes THAT graph-derived answer (kind-agnostic: a prose target no longer
# blocks a symbolic system that does close to a single unknown).
# --------------------------------------------------------------------------- #


def test_derive_symbolic_answer_is_the_lone_unknown_for_bernoulli():
    """The graph-derived answer for the seeded bernoulli system is exactly {P2} —
    every other free symbol is a given (A1/A2/P1/v1/rho), an intermediate
    (rho/v1/v2 coupling continuity<->bernoulli), or cancelled (g/h1/h2 via the
    horizontal simplification). The lone remaining symbol IS the target."""
    from apollo.provisioning.promotion_lint import _derive_symbolic_answer

    problem = Problem.model_validate(_bernoulli_graph())
    assert _derive_symbolic_answer(problem) == {"P2"}


def test_derive_symbolic_answer_is_two_when_a_second_unknown_is_added():
    """Adding an ungrounded canonical symbol Q to a non-terminal equation makes the
    system under-determined: the graph-derived answer becomes {P2, Q} (size 2).
    This is the signal gate 7 (Option 2) rejects on."""
    from apollo.provisioning.promotion_lint import _derive_symbolic_answer

    graph = copy.deepcopy(_bernoulli_graph())
    _step(graph, "continuity")["content"]["symbolic"] = "rho*A1*v1 - rho*A2*v2 + Q"
    problem = Problem.model_validate(graph)
    assert _derive_symbolic_answer(problem) == {"P2", "Q"}


def test_gate5_structural_half_passes_equationless_argument_graph():
    """An equation-less argument graph has a unique terminal sink + full chain
    coverage but NO equations: the structural half passes and the symbolic half is
    SKIPPED (no parseable terminal equation), so gate 5 returns None. Under the old
    code this rejected at the 'terminal uses no equation' branch."""
    from apollo.provisioning.promotion_lint import _gate_5

    problem = Problem.model_validate(_argument_graph())
    kg = problem.to_kg_graph(attempt_id=0)
    assert _gate_5(problem, kg) is None


def test_gate5_symbolic_half_rejects_terminal_missing_the_graph_answer():
    """A terminal step that references a parseable equation NOT containing the
    single graph-derived answer (P2) fails the symbolic half — keyed off the
    graph-derived answer, NOT the prose target_unknown. The diagnostic names the
    answer it failed to compute."""
    from apollo.provisioning.promotion_lint import _gate_5

    graph = copy.deepcopy(_bernoulli_graph())
    # Terminal now uses continuity (lacks P2) instead of bernoulli.
    _step(graph, "plan_solve_bernoulli_for_p2")["content"]["uses_equations"] = ["continuity"]
    problem = Problem.model_validate(graph)
    kg = problem.to_kg_graph(attempt_id=0)
    diag = _gate_5(problem, kg)
    assert diag is not None
    assert "answer" in diag and "P2" in diag


# --------------------------------------------------------------------------- #
# WU-3B2b-SA Phase 2 — internal symbol grounding (Step 2.3, Option 2)
#
# Gate 4 DROPS the explicit target_unknown add (the prose label is no longer a
# symbol). When a canonical_symbols table EXISTS the seeded path is byte-identical
# to today. When the concept is TABLE-LESS (a fresh auto-minted concept — the AAE
# 333 gate-4 failure) a symbol is grounded against the problem's OWN closure:
# givens U definition/variable_mapping symbols U coupling intermediates U cancelled
# U {the graph-derived answer}. A foreign/unexplained symbol becomes an extra
# free unknown, so it is caught by gate 7 (under-determination), never silently
# promoted (asymmetric safety).
# --------------------------------------------------------------------------- #


def test_internal_grounded_symbols_covers_cancelled_and_the_answer():
    """The table-less grounded closure includes the SIMPLIFICATION-cancelled
    gravity terms (g/h1/h2) and the lone graph-derived answer (P2) — not just the
    givens. Pinned directly so the closure does not silently shrink."""
    from apollo.provisioning.promotion_lint import _internal_grounded_symbols

    problem = Problem.model_validate(_bernoulli_graph())
    grounded = _internal_grounded_symbols(problem)
    assert {"g", "h1", "h2"} <= grounded  # cancelled by the horizontal simplification
    assert "P2" in grounded  # the lone graph-derived answer


def test_defined_symbols_reads_definition_and_variable_mapping_steps():
    """``_defined_symbols`` harvests a variable_mapping / definition step's
    ``symbol`` / ``term`` values and tokenizes its ``meaning`` / ``definition`` prose
    (lenient superset). Pinned directly because the back-compat anchor carries no
    such step, so this branch needs its own fixture."""
    from apollo.provisioning.promotion_lint import _defined_symbols

    graph = copy.deepcopy(_bernoulli_graph())
    vm = _step(graph, "incompressibility")  # repurpose as a variable_mapping step
    vm["entry_type"] = "variable_mapping"
    vm["content"] = {"term": "bulk modulus", "symbol": "kappa", "meaning": "stiffness K"}
    problem = Problem.model_validate(graph)
    defined = _defined_symbols(problem)
    assert "kappa" in defined  # content['symbol']
    assert "bulk modulus" in defined  # content['term'] (whole value)
    assert "K" in defined  # tokenized out of content['meaning']

    # A ``definition`` step WITHOUT 'symbol'/'term' (only prose 'meaning') exercises
    # the skip arm — tokens come solely from the prose.
    arg_defined = _defined_symbols(Problem.model_validate(_argument_graph()))
    assert "Sovereignty" in arg_defined  # tokenized out of def_federalism['meaning']


def test_gate4_table_less_promotes_a_self_grounded_system():
    """A fresh concept (EMPTY canonical_symbols) whose every symbol is given,
    computed, cancelled, or the lone answer PASSES — internal grounding, no seeded
    table needed. The old code rejected at gate 4 (every symbol foreign vs the
    empty table); the bug that blocked auto-minted concepts is fixed."""
    graph = copy.deepcopy(_bernoulli_graph())
    r = run_promotion_lint(
        graph, canonical_symbols=set(), normalization_map={}, existing_problem_hashes=set()
    )
    assert r.ok is True, r.diagnostic


def test_gate4_seeded_table_path_is_byte_identical_to_today():
    """When a table EXISTS the internal path must NOT run: a foreign symbol x still
    rejects at gate 4 exactly as today (superset-safety — the 41 ride this)."""
    graph = copy.deepcopy(_bernoulli_graph())
    _step(graph, "continuity")["content"]["symbolic"] = "rho*A1*v1 - rho*A2*v2 + x"
    r = run_promotion_lint(
        graph,
        canonical_symbols=_canonical_symbols(),
        normalization_map=_normalization_map(),
        existing_problem_hashes=set(),
    )
    assert r.failed_gate == 4


def test_table_less_unexplained_symbol_is_rejected_as_underdetermined():
    """A truly foreign symbol zzz in a table-less concept is given/defined/computed
    by nothing -> it becomes a SECOND free unknown alongside the real answer, so the
    system is under-determined and gate 7 rejects it. Never a false-GREEN."""
    graph = copy.deepcopy(_bernoulli_graph())
    _step(graph, "continuity")["content"]["symbolic"] = "rho*A1*v1 - rho*A2*v2 + zzz"
    r = run_promotion_lint(
        graph, canonical_symbols=set(), normalization_map={}, existing_problem_hashes=set()
    )
    assert r.ok is False
    assert r.failed_gate == 7  # zzz + P2 = two free unknowns


# --------------------------------------------------------------------------- #
# WU-3B2b-SA Phase 2 — gate 7 under-determination (Step 2.4, Option 2)
#
# Gate 7 becomes: FAIL iff the graph-derived answer has MORE THAN ONE free
# unknown (the system is under-determined). It keys off the GRAPH, not the prose
# target_unknown — a closed symbolic system with a PROSE target (the live AAE 333
# shape) passes; a system with two independent unknowns fails. Byte-identical to
# the old paper-closure check on the anchor (there the lone remaining symbol IS
# the target, so |answer| == 1 == "closed").
# --------------------------------------------------------------------------- #


def test_gate7_passes_single_answer_even_when_target_is_prose():
    """Gate 7 keys off the GRAPH-derived answer, not target_unknown. A bernoulli
    system closing to the single unknown P2 PASSES even when target_unknown is a
    PROSE label — the live AAE 333 shape. Old code rejected it (P2 'unclosed'
    because it was not the prose target)."""
    graph = copy.deepcopy(_bernoulli_graph())
    graph["target_unknown"] = "the downstream pressure"  # prose, not the symbol 'P2'
    r = run_promotion_lint(
        graph,
        canonical_symbols=_canonical_symbols(),
        normalization_map=_normalization_map(),
        existing_problem_hashes=set(),
    )
    assert r.ok is True, r.diagnostic


def test_gate7_rejects_two_independent_unknowns_via_derive():
    """White-box: the Option-2 gate keys off ``_derive_symbolic_answer``. Two
    independent free unknowns (P2 plus an ungrounded canonical Q) make it size 2,
    so gate 7 fires."""
    from apollo.provisioning.promotion_lint import _derive_symbolic_answer, _gate_7

    graph = copy.deepcopy(_bernoulli_graph())
    _step(graph, "continuity")["content"]["symbolic"] = "rho*A1*v1 - rho*A2*v2 + Q"
    problem = Problem.model_validate(graph)
    assert _derive_symbolic_answer(problem) == {"P2", "Q"}
    assert _gate_7(problem) is not None


# --------------------------------------------------------------------------- #
# WU-AAS lane B2.2 / G4.2 — mint-time equation parser tolerance
#
# The F1a provisioning-notes.md rejection log is the regression corpus:
#   * gate 6 reject #1: ``v = v0 + a*t = 3 + 2*5`` — chained equality.
#   * gate 6 reject #2: ``x = v0*t + (1/2)*a*t^2 = 0 + 0.5*(2.0)*(5.0)^2 = 25.0 m``
#                       — chained equality + ``^`` + a unit-bearing numeric tail.
#   * gate 4 reject:    ``x`` (position) rejected as foreign on the seeded-table
#                       path even though the problem's own varmap.var_x grounds it.
# --------------------------------------------------------------------------- #


def test_gate6_tolerates_chained_equality_and_caret():
    """WU-AAS G4.2: the two EXACT F1a-logged gate-6 rejects no longer fire. A chained
    equality (``=`` used ``symbolic = numeric = final``) and ``^`` exponent notation
    are normalized (to the first equality / to ``**``) rather than rejected as
    malformed. White-box on ``_gate_6`` so the verdict is isolated from the other
    gates."""
    from apollo.provisioning.promotion_lint import _gate_6

    for symbolic in (
        "v = v0 + a*t = 3 + 2*5",
        "x = v0*t + (1/2)*a*t^2 = 0 + 0.5*(2.0)*(5.0)^2 = 25.0 m",
    ):
        graph = copy.deepcopy(_bernoulli_graph())
        _step(graph, "continuity")["content"]["symbolic"] = symbolic
        problem = Problem.model_validate(graph)
        assert _gate_6(problem) is None, symbolic


def test_gate6_still_rejects_genuinely_malformed_equation():
    """Counter-test: the tolerance loosening does NOT neuter gate 6 — an unbalanced
    paren still raises ``MalformedEquationError`` -> gate 6 fires."""
    from apollo.provisioning.promotion_lint import _gate_6

    graph = copy.deepcopy(_bernoulli_graph())
    _step(graph, "continuity")["content"]["symbolic"] = "v = v0 + (a*t"
    problem = Problem.model_validate(graph)
    assert _gate_6(problem) is not None


def test_gate4_seeded_table_accepts_a_problem_given_symbol():
    """WU-AAS G4.2 (F1a Finding B, root cause): on the SEEDED-table path a symbol the
    problem itself GIVES a value for must not be rejected as foreign. Here ``k`` is
    absent from the fluid table but is a given value — the old seeded path ran ONLY
    ``_normalize_symbol`` (table lookup) and rejected it; now the problem's own
    grounding is consulted too. Purely additive: a table symbol still normalizes."""
    graph = copy.deepcopy(_bernoulli_graph())
    graph["given_values"] = {**graph["given_values"], "k": 1.0}
    result = _lint(graph)
    assert result.failed_gate != 4, result.diagnostic
    assert result.ok is True, result.diagnostic


def test_gate4_seeded_table_accepts_variable_mapping_grounded_symbol():
    """WU-AAS G4.2 (F1a Finding B): a symbol the problem grounds via a
    ``variable_mapping`` step (the ``varmap.var_x`` shape a prose mint emits for
    ``x``) survives the seeded-table gate 4 even though ``x`` is not in the fluid
    canonical table. White-box on ``_gate_4``."""
    from apollo.provisioning.promotion_lint import _gate_4

    graph = copy.deepcopy(_bernoulli_graph())
    vm = _step(graph, "incompressibility")  # repurpose as the var-mapping for x
    vm["entry_type"] = "variable_mapping"
    vm["content"] = {"term": "position", "symbol": "x"}
    # Put x in an equation so it enters the gate-4 symbol set.
    _step(graph, "continuity")["content"]["symbolic"] = "rho*A1*v1 - rho*A2*v2 - x"
    problem = Problem.model_validate(graph)
    assert _gate_4(problem, _canonical_symbols(), _normalization_map()) is None


def test_gate4_seeded_table_still_rejects_ungrounded_foreign_symbol():
    """Counter-test (the sole foreign-symbol guard survives): an ungrounded ``x``
    injected into a seeded-table concept's equation — not given, not defined, not
    computed, not cancelled — STILL rejects at gate 4, exactly as before. The
    loosening only admits symbols the problem itself grounds."""
    graph = copy.deepcopy(_bernoulli_graph())
    _step(graph, "continuity")["content"]["symbolic"] = "rho*A1*v1 - rho*A2*v2 + x"
    result = _lint(graph)
    assert result.ok is False
    assert result.failed_gate == 4


def _linear_motion_graph() -> dict:
    """The EXACT F1a linear_motion problem 1(b) shape, mis-filed under a fluid
    (seeded) canonical table — the WU-AAS mint case that reproduced all three G4.2
    rejects. ``x = v0*t + (1/2)*a*t^2 = ... = 25.0 m`` carries a chained equality +
    ``^`` (gate 6) and ``x`` grounded only by its ``varmap.var_x`` step (gate 4).
    Pre-annotated (entity_key + declared_paths) so it feeds ``run_promotion_lint``
    directly, like ``_bernoulli_graph``."""
    return {
        "id": "linear_motion_find_position",
        "concept_id": "linear_motion",
        "difficulty": "intro",
        "given_values": {"v0": 0.0, "a": 2.0, "t": 5.0},
        "problem_text": (
            "A body starts from rest and accelerates at 2.0 m/s^2 for 5.0 s. "
            "Find the distance x travelled."
        ),
        "target_unknown": "x",
        "declared_paths": [["motion", "var_x", "compute_x"]],
        "reference_solution": [
            {
                "id": "motion",
                "step": 1,
                "entry_type": "equation",
                "entity_key": "eq.motion",
                "content": {
                    "label": "Kinematic position equation",
                    # chained equality + caret + unit-bearing numeric tail (all 3 G4.2 classes)
                    "symbolic": "x = v0*t + (1/2)*a*t^2 = 0 + 0.5*(2.0)*(5.0)^2 = 25.0 m",
                    "variables": ["x", "v0", "a", "t"],
                },
                "depends_on": [],
            },
            {
                "id": "var_x",
                "step": 2,
                "entry_type": "variable_mapping",
                "entity_key": "varmap.var_x",
                "content": {"term": "position", "symbol": "x"},
                "depends_on": [],
            },
            {
                "id": "compute_x",
                "step": 3,
                "entry_type": "procedure_step",
                "entity_key": "proc.compute_x",
                "content": {
                    "order": 1,
                    "action": "substitute v0, a, t into the kinematic equation and solve for x",
                    "purpose": "produce the distance travelled",
                    "uses_equations": ["motion"],
                },
                "depends_on": ["motion", "var_x"],
            },
        ],
    }


def test_linear_motion_authored_set_promotes_end_to_end():
    """WU-AAS lane B2.2 / G4.2 acceptance: the real F1a linear_motion problem 1(b),
    mis-filed under a fluid seeded canonical table, PROMOTES through all applicable
    gates — the chained-equality + ``^`` equation parses (gates 4/5/6/7 via
    ``parse_zero_form``) and ``x`` is grounded by its ``varmap.var_x`` step (gate 4).
    Before this lane it was rejected 3 ways (gate 6 twice, then gate 4 on ``x``)."""
    result = _lint(_linear_motion_graph())
    assert result.ok is True, result.diagnostic
