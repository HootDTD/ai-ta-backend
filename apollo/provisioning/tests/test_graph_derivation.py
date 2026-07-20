"""Solution-grounded graph derivation — pure validator + injected-chat derivation."""

import json

import pytest

from apollo.provisioning.authored_sets.graph_derivation import (
    DerivationError,
    TypedConstructionDefect,
    build_ordered_problem,
    derive_reference_graph,
    find_derivation_defects,
)
from apollo.provisioning.ingest import AuthoredProblem
from apollo.provisioning.solution import GroundingSpan
from apollo.schemas.problem import Problem

_VOCAB = {
    "symbols": ["x", "u", "v", "du", "dv", "dx", "F", "I", "C"],
    "description": {"x": "integration variable", "F": "the antiderivative"},
}
_NORM = {"antiderivative": "F", "constant of integration": "C"}


def _ordered_authored(*, target_unknown: str = "") -> AuthoredProblem:
    return AuthoredProblem(
        problem_code="authored.ordered",
        concept_slug="provisional.inventory",
        statement="Explain and solve the ordered problem.",
        solution="A worked answer.",
        given_values={"a": 2.0},
        target_unknown=target_unknown,
        completeness="answer_only",
    )


def test_ordered_builder_derives_case_sensitive_symbol_dependencies():
    graph = build_ordered_problem(
        _ordered_authored(target_unknown="z"),
        [
            {
                "entry_type": "variable_mapping",
                "id": "lowercase_mass",
                "content": {"term": "lowercase mass", "symbol": "m"},
            },
            {
                "entry_type": "variable_mapping",
                "id": "uppercase_mass",
                "content": {"term": "uppercase mass", "symbol": "M"},
            },
            {
                "entry_type": "equation",
                "id": "intermediate_value",
                "content": {"symbolic": "x = m + a"},
            },
            {
                "entry_type": "equation",
                "id": "target_value",
                "content": {"symbolic": "z = x + M"},
            },
        ],
    )

    steps = {step["id"]: step for step in graph["reference_solution"]}
    assert steps["intermediate_value"]["depends_on"] == ["lowercase_mass"]
    assert steps["target_value"]["depends_on"] == [
        "uppercase_mass",
        "intermediate_value",
    ]
    assert isinstance(Problem.model_validate(graph), Problem)


def test_ordered_builder_rejects_case_variant_as_undefined():
    with pytest.raises(TypedConstructionDefect, match="case-sensitive.*'M'"):
        build_ordered_problem(
            _ordered_authored(target_unknown="answer"),
            [
                {
                    "entry_type": "variable_mapping",
                    "id": "lowercase_mass",
                    "content": {"term": "mass", "symbol": "m"},
                },
                {
                    "entry_type": "equation",
                    "id": "calculate_answer",
                    "content": {"symbolic": "answer = M + a"},
                },
            ],
        )


def test_ordered_builder_uses_declared_then_adjacent_prose_fallback():
    graph = build_ordered_problem(
        _ordered_authored(),
        [
            {
                "entry_type": "definition",
                "id": "federalism_meaning",
                "content": {"concept": "federalism", "meaning": "divided sovereignty"},
            },
            {
                "entry_type": "condition",
                "id": "divided_authority",
                "content": {"applies_when": "authority is split"},
            },
            {
                "entry_type": "simplification",
                "id": "focus_accountability",
                "content": {
                    "applies_when": "accountability is the question",
                    "transformation": "focus the argument on veto points",
                },
                "references": ["federalism_meaning"],
            },
        ],
    )
    first, second, third = graph["reference_solution"]
    assert first["depends_on"] == []
    assert second["depends_on"] == ["federalism_meaning"]
    assert third["depends_on"] == ["federalism_meaning"]


def test_ordered_builder_rejects_non_list_references():
    with pytest.raises(TypedConstructionDefect, match="references must be an array"):
        build_ordered_problem(
            _ordered_authored(),
            [
                {
                    "entry_type": "definition",
                    "id": "federalism_meaning",
                    "content": {"concept": "federalism", "meaning": "divided sovereignty"},
                },
                {
                    "entry_type": "condition",
                    "id": "divided_authority",
                    "content": {"applies_when": "authority is split"},
                    "references": "federalism_meaning",
                },
            ],
        )


def test_ordered_builder_rejects_empty_reference_ids():
    with pytest.raises(TypedConstructionDefect, match="references must contain non-empty ids"):
        build_ordered_problem(
            _ordered_authored(),
            [
                {
                    "entry_type": "definition",
                    "id": "federalism_meaning",
                    "content": {"concept": "federalism", "meaning": "divided sovereignty"},
                },
                {
                    "entry_type": "condition",
                    "id": "divided_authority",
                    "content": {"applies_when": "authority is split"},
                    "references": [""],
                },
            ],
        )


def test_ordered_builder_adds_unbroken_procedure_chain_and_contiguous_order():
    graph = build_ordered_problem(
        _ordered_authored(),
        [
            {
                "entry_type": "procedure_step",
                "id": "identify_evidence",
                "content": {"action": "identify evidence", "purpose": "ground the claim"},
            },
            {
                "entry_type": "definition",
                "id": "accountability_meaning",
                "content": {"concept": "accountability", "meaning": "answerability"},
            },
            {
                "entry_type": "procedure_step",
                "id": "weigh_evidence",
                "content": {"action": "weigh evidence", "purpose": "reach a conclusion"},
            },
            {
                "entry_type": "procedure_step",
                "id": "state_conclusion",
                "content": {"action": "state conclusion", "purpose": "answer"},
            },
        ],
    )
    problem = Problem.model_validate(graph)
    procedures = [
        step for step in problem.reference_solution if step.entry_type == "procedure_step"
    ]
    assert [step.content["order"] for step in procedures] == [1, 2, 3]
    precedes = [
        (edge.from_node_id, edge.to_node_id)
        for edge in problem.to_kg_graph(0).edges
        if edge.edge_type == "PRECEDES"
    ]
    assert precedes == [
        ("identify_evidence", "weigh_evidence"),
        ("weigh_evidence", "state_conclusion"),
    ]


def test_ordered_builder_makes_cycles_unresolved_edges_and_bad_order_unreachable():
    graph = build_ordered_problem(
        _ordered_authored(),
        [
            {
                "entry_type": "definition",
                "id": "first_concept",
                "content": {"concept": "first", "meaning": "first meaning"},
            },
            {
                "entry_type": "procedure_step",
                "id": "first_action",
                "content": {"order": 99, "action": "act", "purpose": "advance"},
                "references": ["first_concept"],
            },
            {
                "entry_type": "procedure_step",
                "id": "second_action",
                "content": {"order": -4, "action": "finish", "purpose": "answer"},
            },
        ],
    )
    problem = Problem.model_validate(graph)
    ids = [step.id for step in problem.reference_solution]
    for index, step in enumerate(problem.reference_solution):
        assert all(ids.index(dependency) < index for dependency in step.depends_on)
    assert [
        step.content["order"]
        for step in problem.reference_solution
        if step.entry_type == "procedure_step"
    ] == [1, 2]
    problem.to_kg_graph(0).topological_order("DEPENDS_ON")


def test_ordered_builder_rejects_non_sequence_and_empty_steps():
    with pytest.raises(TypedConstructionDefect, match="non-empty array"):
        build_ordered_problem(_ordered_authored(), {"not": "a sequence"})
    with pytest.raises(TypedConstructionDefect, match="non-empty array"):
        build_ordered_problem(_ordered_authored(), [])


def test_ordered_builder_rejects_malformed_symbol_table():
    steps = [
        {
            "entry_type": "definition",
            "id": "federalism_meaning",
            "content": {"concept": "federalism", "meaning": "divided sovereignty"},
        }
    ]
    with pytest.raises(TypedConstructionDefect, match="symbol_table"):
        build_ordered_problem(_ordered_authored(), steps, symbol_table="not-a-dict")
    with pytest.raises(TypedConstructionDefect, match="symbol_table"):
        build_ordered_problem(_ordered_authored(), steps, symbol_table={"m": "not-a-dict"})


def test_ordered_builder_accumulates_first_pass_schema_diagnostics():
    """Every step-schema defect class raises together in one pass: a non-dict
    step, a forbidden ``depends_on`` key, an unsupported extra key, a missing
    id, a duplicate id, an opaque id, and non-object ``content``."""
    steps = [
        "not a dict",
        {
            "entry_type": "definition",
            "id": "shared_definition",
            "content": {"concept": "a", "meaning": "b"},
            "depends_on": [],
        },
        {
            "entry_type": "definition",
            "id": "extra_field_definition",
            "content": {"concept": "a", "meaning": "b"},
            "unexpected_field": True,
        },
        {
            "entry_type": "definition",
            "content": {"concept": "a", "meaning": "b"},
        },
        {
            "entry_type": "definition",
            "id": "shared_definition",
            "content": {"concept": "c", "meaning": "d"},
        },
        {
            "entry_type": "definition",
            "id": "123",
            "content": {"concept": "e", "meaning": "f"},
        },
        {
            "entry_type": "definition",
            "id": "bad_content_definition",
            "content": "not-a-dict",
        },
    ]
    with pytest.raises(TypedConstructionDefect) as excinfo:
        build_ordered_problem(_ordered_authored(), steps)
    diagnostics = "\n".join(excinfo.value.diagnostics)
    assert "step 1 must be an object" in diagnostics
    assert "must not declare depends_on" in diagnostics
    assert "unsupported keys" in diagnostics
    assert "has no id" in diagnostics
    assert "duplicate_id: shared_definition" in diagnostics
    assert "opaque_id: '123'" in diagnostics
    assert "content must be an object" in diagnostics


def test_ordered_builder_rejects_non_array_uses_equations():
    steps = [
        {
            "entry_type": "procedure_step",
            "id": "identify_evidence",
            "content": {
                "action": "identify evidence",
                "purpose": "ground the claim",
                "uses_equations": "not-a-list",
            },
        },
    ]
    with pytest.raises(TypedConstructionDefect, match="uses_equations must be an array"):
        build_ordered_problem(_ordered_authored(), steps)


def test_ordered_builder_rejects_procedure_uses_equations_referencing_non_prior_equation():
    steps = [
        {
            "entry_type": "procedure_step",
            "id": "apply_formula",
            "content": {
                "action": "apply the formula",
                "purpose": "answer",
                "uses_equations": ["never_declared_equation"],
            },
        },
    ]
    with pytest.raises(TypedConstructionDefect, match="non-prior equations"):
        build_ordered_problem(_ordered_authored(), steps)


def test_ordered_builder_rejects_rhs_sympify_double_parse_failure(monkeypatch):
    """The RHS double-parse (line 185) is defense-in-depth beyond
    ``_equation_parse_defect``'s own double-parse: let the first two
    sympify calls (LHS/RHS inside the defect pre-check) succeed for real,
    then force the third (this function's own RHS re-parse) to blow up."""
    import apollo.provisioning.authored_sets.graph_derivation as gd

    real_sympify = gd.sympy.sympify
    calls = {"n": 0}

    def _flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] > 2:
            raise TypeError("forced rhs sympify failure")
        return real_sympify(*args, **kwargs)

    monkeypatch.setattr(gd.sympy, "sympify", _flaky)
    with pytest.raises(TypedConstructionDefect, match="sympify failed"):
        build_ordered_problem(
            _ordered_authored(target_unknown="z"),
            [
                {
                    "entry_type": "variable_mapping",
                    "id": "lowercase_mass",
                    "content": {"term": "mass", "symbol": "m"},
                },
                {
                    "entry_type": "equation",
                    "id": "target_value",
                    "content": {"symbolic": "z = m + a"},
                },
            ],
        )


def test_ordered_builder_wraps_final_schema_validation_failure(monkeypatch):
    """``Problem.model_validate``/``to_kg_graph`` failures that survive every
    upstream diagnostic (the final construction surface) are wrapped in a
    ``graph_derivation:``-prefixed ``TypedConstructionDefect``."""
    import apollo.provisioning.authored_sets.graph_derivation as gd

    def _boom(*_args, **_kwargs):
        raise ValueError("forced final validation failure")

    monkeypatch.setattr(gd.Problem, "model_validate", _boom)
    with pytest.raises(TypedConstructionDefect, match="graph_derivation:"):
        build_ordered_problem(
            _ordered_authored(),
            [
                {
                    "entry_type": "definition",
                    "id": "federalism_meaning",
                    "content": {"concept": "federalism", "meaning": "divided sovereignty"},
                },
            ],
        )


def _good_graph() -> dict:
    return {
        "reference_solution": [
            {
                "step": 1,
                "entry_type": "equation",
                "id": "ibp_formula",
                "content": {
                    "label": "Integration by parts",
                    "symbolic": "integral u dv = u*v - integral v du",
                    "display": True,
                    "variables": ["u", "v"],
                },
                "depends_on": [],
            },
            {
                "step": 2,
                "entry_type": "definition",
                "id": "parts_assignment",
                "content": {
                    "concept": "u = x, dv = e^x dx",
                    "meaning": "differentiate x, integrate e^x",
                },
                "depends_on": [],
            },
            {
                "step": 3,
                "entry_type": "equation",
                "id": "du_v_computed",
                "content": {
                    "label": "Differentials",
                    "symbolic": "du = 1*dx",
                    "variables": ["du", "dx"],
                },
                "depends_on": ["parts_assignment"],
            },
            {
                "step": 4,
                "entry_type": "procedure_step",
                "id": "apply_parts",
                "content": {
                    "label": "Apply parts",
                    "order": 1,
                    "action": "apply the parts formula with u = x, dv = e^x dx",
                    "purpose": "reduce to integral of e^x",
                    "uses_equations": ["ibp_formula"],
                },
                "depends_on": ["ibp_formula", "parts_assignment"],
            },
            {
                "step": 5,
                "entry_type": "procedure_step",
                "id": "integrate_remainder",
                "content": {
                    "label": "Integrate the remainder",
                    "order": 2,
                    "action": "integrate e^x to finish: F = x*e^x - e^x + C",
                    "purpose": "produce the antiderivative",
                    "uses_equations": [],
                },
                "depends_on": ["apply_parts"],
            },
        ],
        "target_unknown": "F",
        "symbolic_mappings": {"u": "x"},
        "bound_variables": ["x"],
        # find_derivation_defects consumes a Problem-shaped dict:
        "id": "t",
        "concept_id": "integration-by-parts",
        "difficulty": "standard",
        "problem_text": "Evaluate integral x e^x dx.",
        "given_values": {},
    }


class TestValidator:
    def test_clean_graph_has_no_defects(self) -> None:
        assert (
            find_derivation_defects(
                _good_graph(), canonical_symbols=_VOCAB, normalization_map=_NORM
            )
            == []
        )

    def test_node_count_bounds(self) -> None:
        g = _good_graph()
        g["reference_solution"] = g["reference_solution"][:2]
        for s in g["reference_solution"]:
            s["depends_on"] = []
        defects = find_derivation_defects(g, canonical_symbols=_VOCAB, normalization_map=_NORM)
        assert any(d.startswith("node_count") for d in defects)

    def test_opaque_id_rejected(self) -> None:
        g = _good_graph()
        g["reference_solution"][2]["id"] = "vm_a"
        defects = find_derivation_defects(g, canonical_symbols=_VOCAB, normalization_map=_NORM)
        assert any(d.startswith("opaque_id") for d in defects)

    def test_unparseable_concrete_equation_rejected(self) -> None:
        g = _good_graph()
        # x(x+1): silent function-call misparse — must be caught (explicit * required)
        g["reference_solution"][2]["content"]["symbolic"] = "du = x(x+1)"
        defects = find_derivation_defects(g, canonical_symbols=_VOCAB, normalization_map=_NORM)
        assert any(d.startswith("equation_parse") for d in defects)

    def test_display_identity_is_not_parse_checked(self) -> None:
        # the ibp_formula display node is unparseable by design — no defect
        assert not any(
            "ibp_formula" in d
            for d in find_derivation_defects(
                _good_graph(), canonical_symbols=_VOCAB, normalization_map=_NORM
            )
        )

    def test_reserved_name_I_parses_via_local_dict(self) -> None:
        g = _good_graph()
        g["reference_solution"][2]["content"]["symbolic"] = "2*I = x - C"
        defects = find_derivation_defects(g, canonical_symbols=_VOCAB, normalization_map=_NORM)
        assert not any(d.startswith("equation_parse") for d in defects)

    def test_depends_on_cycle_rejected(self) -> None:
        g = _good_graph()
        # close a cycle: parts_assignment -> apply_parts -> parts_assignment.
        # (Problem.model_validate only checks depends_on ids RESOLVE, not order.)
        g["reference_solution"][1]["depends_on"] = ["apply_parts"]
        defects = find_derivation_defects(g, canonical_symbols=_VOCAB, normalization_map=_NORM)
        assert any(d.startswith("cycle") for d in defects)

    def test_duplicate_variable_fragmentation_rejected(self) -> None:
        g = _good_graph()
        extra = [
            {
                "step": 1,
                "entry_type": "variable_mapping",
                "id": "antiderivative_symbol",
                "content": {"term": "antiderivative", "symbol": "F"},
                "depends_on": [],
            },
            {
                "step": 2,
                "entry_type": "variable_mapping",
                "id": "resulting_integral",
                "content": {"term": "the antiderivative", "symbol": "F"},
                "depends_on": [],
            },
        ]
        g["reference_solution"] = extra + g["reference_solution"]
        for i, s in enumerate(g["reference_solution"], start=1):
            s["step"] = i
        defects = find_derivation_defects(g, canonical_symbols=_VOCAB, normalization_map=_NORM)
        assert any(d.startswith("fragmentation") for d in defects)

    def test_duplicate_equation_fragmentation_rejected(self) -> None:
        g = _good_graph()
        g["reference_solution"].append(
            {
                "step": 6,
                "entry_type": "equation",
                "id": "differentials_again",
                "content": {"label": "Differentials restated", "symbolic": "1*dx = du"},
                "depends_on": [],
            }
        )
        defects = find_derivation_defects(g, canonical_symbols=_VOCAB, normalization_map=_NORM)
        assert any(d.startswith("fragmentation") for d in defects)


def _chat(responses: list[str]):
    calls: list[dict] = []

    def chat_fn(**kwargs) -> str:
        calls.append(kwargs)
        return responses[min(len(calls) - 1, len(responses) - 1)]

    chat_fn.calls = calls  # type: ignore[attr-defined]
    return chat_fn


class _Candidate:
    problem_text = "Evaluate integral x e^x dx."
    given_values: dict = {}
    target_unknown = "F"
    difficulty = "standard"
    chunk_content_hash = "abc"


def _llm_payload(graph: dict) -> str:
    return json.dumps(
        {
            k: graph[k]
            for k in (
                "reference_solution",
                "target_unknown",
                "symbolic_mappings",
                "bound_variables",
            )
        }
    )


_SPANS = (
    GroundingSpan(text="Let u = x, dv = e^x dx ... ANSWER: x e^x - e^x + C", carries_solution=True),
)


@pytest.mark.asyncio
async def test_derive_returns_clean_graph_first_pass() -> None:
    chat_fn = _chat([_llm_payload(_good_graph())])
    out = await derive_reference_graph(
        _Candidate(),
        _SPANS,
        concept_slug="integration-by-parts",
        concept_display_name="Integration by Parts",
        canonical_symbols=_VOCAB,
        normalization_map=_NORM,
        chat_fn=chat_fn,
    )
    assert len(out.reference_solution) == 5 and not out.retried
    assert out.bound_variables == ["x"]
    # leak guard: the prompt context is the SOLUTION spans (plus problem/vocab)
    user = chat_fn.calls[0]["messages"][1]["content"]
    assert "ANSWER: x e^x" in user


@pytest.mark.asyncio
async def test_derive_retries_with_defect_feedback_then_succeeds() -> None:
    bad = _good_graph()
    bad["reference_solution"][2]["id"] = "vm_a"
    chat_fn = _chat([_llm_payload(bad), _llm_payload(_good_graph())])
    out = await derive_reference_graph(
        _Candidate(),
        _SPANS,
        concept_slug="integration-by-parts",
        concept_display_name="Integration by Parts",
        canonical_symbols=_VOCAB,
        normalization_map=_NORM,
        chat_fn=chat_fn,
    )
    assert out.retried
    retry_user = chat_fn.calls[1]["messages"][1]["content"]
    assert "opaque_id" in retry_user  # defects fed back
    assert chat_fn.calls[1]["reasoning_effort"] == "high"


@pytest.mark.asyncio
async def test_derive_fails_closed_after_retry() -> None:
    bad = _good_graph()
    bad["reference_solution"][2]["id"] = "vm_a"
    chat_fn = _chat([_llm_payload(bad), _llm_payload(bad)])
    with pytest.raises(DerivationError):
        await derive_reference_graph(
            _Candidate(),
            _SPANS,
            concept_slug="integration-by-parts",
            concept_display_name="Integration by Parts",
            canonical_symbols=_VOCAB,
            normalization_map=_NORM,
            chat_fn=chat_fn,
        )


@pytest.mark.asyncio
async def test_derive_requires_solution_spans() -> None:
    with pytest.raises(DerivationError):
        await derive_reference_graph(
            _Candidate(),
            (),
            concept_slug="integration-by-parts",
            concept_display_name="Integration by Parts",
            canonical_symbols=_VOCAB,
            normalization_map=_NORM,
            chat_fn=_chat(["{}"]),
        )


class TestForeignSymbols:
    def test_foreign_symbol_in_concrete_equation_rejected(self) -> None:
        g = _good_graph()
        # dtheta is not in the vocabulary/givens/bound/target
        g["reference_solution"][2]["content"]["symbolic"] = "du = 2*dtheta"
        defects = find_derivation_defects(g, canonical_symbols=_VOCAB, normalization_map=_NORM)
        assert any(d.startswith("foreign_symbol") and "dtheta" in d for d in defects)

    def test_vocabulary_and_bound_symbols_allowed(self) -> None:
        # du, dv are vocabulary; x is bound -> clean
        g = _good_graph()
        g["reference_solution"][2]["content"]["symbolic"] = "du = 2*x*dv"
        defects = find_derivation_defects(g, canonical_symbols=_VOCAB, normalization_map=_NORM)
        assert not any(d.startswith("foreign_symbol") for d in defects)

    def test_display_formula_exempt_from_symbol_check(self) -> None:
        # the ibp_formula display identity carries non-vocabulary tokens freely
        defects = find_derivation_defects(
            _good_graph(), canonical_symbols=_VOCAB, normalization_map=_NORM
        )
        assert not any(d.startswith("foreign_symbol") for d in defects)


class TestValidatorBranches:
    def test_trig_textbook_notation_is_display(self) -> None:
        g = _good_graph()
        g["reference_solution"][2]["content"]["symbolic"] = "sin^2 x = 1 - cos^2 x"
        defects = find_derivation_defects(g, canonical_symbols=_VOCAB, normalization_map=_NORM)
        assert not any(d.startswith("equation_parse") for d in defects)

    def test_empty_symbolic_rejected(self) -> None:
        g = _good_graph()
        g["reference_solution"][2]["content"]["symbolic"] = ""
        defects = find_derivation_defects(g, canonical_symbols=_VOCAB, normalization_map=_NORM)
        assert any("empty symbolic" in d for d in defects)

    def test_sympify_side_failure_rejected(self, monkeypatch) -> None:
        # the sympify double-parse is defense-in-depth: force its failure to
        # pin the defect wording and the continue path
        import apollo.provisioning.authored_sets.graph_derivation as gd

        def _boom(*_a, **_k):
            raise TypeError("forced sympify failure")

        monkeypatch.setattr(gd.sympy, "sympify", _boom)
        defects = find_derivation_defects(
            _good_graph(), canonical_symbols=_VOCAB, normalization_map=_NORM
        )
        assert any("sympify failed" in d for d in defects)

    def test_non_snake_case_id_rejected(self) -> None:
        g = _good_graph()
        g["reference_solution"][2]["id"] = "Vm A"
        defects = find_derivation_defects(g, canonical_symbols=_VOCAB, normalization_map=_NORM)
        assert any("is not snake_case" in d for d in defects)

    def test_duplicate_id_rejected(self) -> None:
        g = _good_graph()
        g["reference_solution"][2]["id"] = "parts_assignment"
        defects = find_derivation_defects(g, canonical_symbols=_VOCAB, normalization_map=_NORM)
        assert any(d.startswith("duplicate_id") for d in defects)

    def test_schema_failure_short_circuits(self) -> None:
        g = _good_graph()
        del g["reference_solution"][3]["content"]["order"]  # breaks order contiguity
        defects = find_derivation_defects(g, canonical_symbols=_VOCAB, normalization_map=_NORM)
        assert len(defects) == 1 and defects[0].startswith("schema:")


@pytest.mark.asyncio
async def test_derive_unparseable_first_pass_retries_then_succeeds() -> None:
    chat_fn = _chat(["not json at all", _llm_payload(_good_graph())])
    out = await derive_reference_graph(
        _Candidate(),
        _SPANS,
        concept_slug="integration-by-parts",
        concept_display_name="Integration by Parts",
        canonical_symbols=_VOCAB,
        normalization_map=_NORM,
        chat_fn=chat_fn,
    )
    assert out.retried
    assert "unparseable derivation response" in chat_fn.calls[1]["messages"][1]["content"]


@pytest.mark.asyncio
async def test_derive_non_dict_and_missing_list_responses_fail_closed() -> None:
    chat_fn = _chat(['["a list"]', '{"reference_solution": "not a list"}'])
    with pytest.raises(DerivationError):
        await derive_reference_graph(
            _Candidate(),
            _SPANS,
            concept_slug="integration-by-parts",
            concept_display_name="Integration by Parts",
            canonical_symbols=_VOCAB,
            normalization_map=_NORM,
            chat_fn=chat_fn,
        )
