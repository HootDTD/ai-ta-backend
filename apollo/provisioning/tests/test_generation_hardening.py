"""DAG-3 generation hardening: unified ontology block, defect-retry harness in
``find_or_generate``, and the three new defect classes (dependency-completeness,
semantic entity keys, problem-local symbol table).

Standing rule: every symbolic defect class self-deactivates on prose — the
qualitative fixtures here must produce ZERO defects and ZERO retries.
"""

from __future__ import annotations

import json

import pytest

from apollo.provisioning.authored_sets.graph_derivation import (
    _DERIVATION_SYSTEM_PROMPT,
    ALL_DEFECT_CLASSES,
    GENERATION_DEFECT_CLASSES,
    find_derivation_defects,
)
from apollo.provisioning.generation_contract import ontology_block, ordered_step_ontology_block
from apollo.provisioning.solution import (
    _SOLUTION_GENERATE_AUGMENT_SYSTEM_PROMPT,
    _SOLUTION_GENERATE_SYSTEM_PROMPT,
    _authored_output_contract,
    find_or_generate,
)
from apollo.provisioning.tests.test_solution import (  # reuse the suite's fixtures
    _candidate,
    _retrieve_returning,
)

# --------------------------------------------------------------------------- #
# 1. Contract unification — one ontology block, three consumers
# --------------------------------------------------------------------------- #


def test_ontology_block_is_sourced_by_all_three_prompts():
    """A rule added to the block propagates to EVERY generation prompt."""
    block = ontology_block()
    assert block in _SOLUTION_GENERATE_SYSTEM_PROMPT
    assert block in _SOLUTION_GENERATE_AUGMENT_SYSTEM_PROMPT
    assert block in _DERIVATION_SYSTEM_PROMPT
    assert ordered_step_ontology_block() in _authored_output_contract()
    assert 'Never emit "step", "depends_on"' in _authored_output_contract()


def test_ontology_block_renders_byte_stably():
    assert ontology_block() == ontology_block()


# --------------------------------------------------------------------------- #
# Fixture graphs (calc + qualitative)
# --------------------------------------------------------------------------- #


def _calc_graph(**overrides) -> dict:
    """Bernoulli-shaped quantitative graph: an equation using ``rho`` that is
    bound by a variable_mapping node."""
    graph = {
        "id": "calc-fixture",
        "concept_id": "bernoulli_principle",
        "difficulty": "standard",
        "problem_text": "Find P2 for the pipe.",
        "given_values": {"P1": 101325.0, "v1": 3.0, "v2": 9.0},
        "target_unknown": "P2",
        "reference_solution": [
            {
                "step": 1,
                "entry_type": "variable_mapping",
                "id": "density_symbol",
                "content": {"term": "fluid density", "symbol": "rho"},
                "depends_on": [],
            },
            {
                "step": 2,
                "entry_type": "equation",
                "id": "bernoulli_equation",
                "content": {
                    "label": "Bernoulli",
                    "symbolic": "P1 + 0.5*rho*v1**2 - P2 - 0.5*rho*v2**2",
                },
                "depends_on": ["density_symbol"],
            },
            {
                "step": 3,
                "entry_type": "procedure_step",
                "id": "solve_outlet_pressure",
                "content": {
                    "action": "Solve for P2",
                    "purpose": "isolate the outlet pressure",
                    "order": 1,
                    "uses_equations": ["bernoulli_equation"],
                },
                "depends_on": ["bernoulli_equation"],
            },
        ],
    }
    graph.update(overrides)
    return graph


def _prose_graph() -> dict:
    """MGMT-style qualitative graph: no equations anywhere."""
    return {
        "id": "prose-fixture",
        "concept_id": "future_shock",
        "difficulty": "intro",
        "problem_text": "Explain why future shock occurs.",
        "given_values": {},
        "target_unknown": "future shock",
        "reference_solution": [
            {
                "step": 1,
                "entry_type": "definition",
                "id": "future_shock_definition",
                "content": {
                    "concept": "future shock",
                    "meaning": "Disorientation from too much change in too little time.",
                },
                "depends_on": [],
            },
            {
                "step": 2,
                "entry_type": "procedure_step",
                "id": "explain_adaptation_gap",
                "content": {
                    "action": "Argue that change outpaces human adaptation.",
                    "purpose": "explain the mechanism",
                    "order": 1,
                },
                "depends_on": ["future_shock_definition"],
            },
        ],
    }


def _defects(graph: dict, classes=GENERATION_DEFECT_CLASSES) -> list[str]:
    return find_derivation_defects(
        graph, canonical_symbols={}, normalization_map={}, classes=classes
    )


# --------------------------------------------------------------------------- #
# 2. Semantic entity keys
# --------------------------------------------------------------------------- #


def test_step_position_echo_id_is_a_semantic_key_defect():
    graph = _calc_graph()
    graph["reference_solution"][1]["id"] = "step_2"
    graph["reference_solution"][2]["content"]["uses_equations"] = ["step_2"]
    graph["reference_solution"][2]["depends_on"] = ["step_2"]
    defects = _defects(graph)
    assert any(d.startswith("semantic_key") and "step_2" in d for d in defects)


def test_entity_key_type_echo_is_a_semantic_key_defect():
    graph = _calc_graph()
    graph["reference_solution"][1]["entity_key"] = "eq.step_2"
    defects = _defects(graph)
    assert any(d.startswith("semantic_key") and "eq.step_2" in d for d in defects)


def test_meaningful_ids_and_keys_pass():
    graph = _calc_graph()
    graph["reference_solution"][1]["entity_key"] = "eq.bernoulli"
    assert not [d for d in _defects(graph) if d.startswith("semantic_key")]


# --------------------------------------------------------------------------- #
# 3. Dependency-completeness
# --------------------------------------------------------------------------- #


def test_symbol_bound_outside_the_closure_is_a_dependency_defect():
    graph = _calc_graph()
    graph["reference_solution"][1]["depends_on"] = []  # equation no longer sees rho's binder
    defects = _defects(graph)
    assert any(
        d.startswith("dependency_completeness") and "'rho'" in d and "density_symbol" in d
        for d in defects
    )


def test_symbol_bound_transitively_passes():
    assert not [d for d in _defects(_calc_graph()) if d.startswith("dependency_completeness")]


def test_shared_constants_and_givens_never_fire_dependency_completeness():
    graph = _calc_graph()
    # g is a shared constant; P1/v1/v2 are givens; none demand an upstream node.
    graph["reference_solution"][1]["content"]["symbolic"] = (
        "P1 + 0.5*rho*v1**2 + rho*g - P2 - 0.5*rho*v2**2"
    )
    defects = [d for d in _defects(graph) if d.startswith("dependency_completeness")]
    assert not [d for d in defects if "'g'" in d or "'P1'" in d]


def test_condition_edges_do_not_provide_symbol_coverage():
    """A condition in the closure does NOT bind symbols — coverage must come
    from the variable_mapping/definition binder itself."""
    graph = _calc_graph()
    steps = graph["reference_solution"]
    steps[1]["depends_on"] = ["steady_flow_condition"]
    steps.insert(
        1,
        {
            "step": 2,
            "entry_type": "condition",
            "id": "steady_flow_condition",
            "content": {"applies_when": "steady incompressible flow"},
            "depends_on": [],
        },
    )
    for index, step in enumerate(steps, start=1):
        step["step"] = index
    defects = _defects(graph)
    assert any(d.startswith("dependency_completeness") and "'rho'" in d for d in defects)


# --------------------------------------------------------------------------- #
# 4. Problem-local symbol table
# --------------------------------------------------------------------------- #


def test_absent_symbol_table_is_legacy_and_clean():
    assert not [d for d in _defects(_calc_graph()) if d.startswith("symbol_table")]


def test_symbol_table_missing_entry_is_a_defect():
    graph = _calc_graph()
    graph["symbol_table"] = {
        "P1": {"role": "inlet pressure", "ontology_key": "", "unit": "Pa"},
    }
    defects = [d for d in _defects(graph) if d.startswith("symbol_table")]
    assert any("'rho'" in d for d in defects)


def test_symbol_table_case_variant_is_called_out():
    graph = _calc_graph()
    graph["reference_solution"][1]["content"]["symbolic"] = "M*v1 - P2"
    graph["reference_solution"][1]["depends_on"] = []
    graph["symbol_table"] = {
        "m": {"role": "mass", "ontology_key": "", "unit": "kg"},
        "v1": {"role": "speed", "ontology_key": "", "unit": "m/s"},
    }
    defects = [d for d in _defects(graph) if d.startswith("symbol_table")]
    assert any("case-sensitive" in d and "'M'" in d for d in defects)


def test_symbol_table_must_be_an_object_of_objects():
    graph = _calc_graph()
    graph["symbol_table"] = {"rho": "density"}
    defects = [d for d in _defects(graph) if d.startswith("symbol_table")]
    assert defects


def test_complete_symbol_table_passes():
    graph = _calc_graph()
    graph["symbol_table"] = {
        name: {"role": name.lower(), "ontology_key": "", "unit": None}
        for name in ("P1", "P2", "rho", "v1", "v2")
    }
    assert not [d for d in _defects(graph) if d.startswith("symbol_table")]


# --------------------------------------------------------------------------- #
# 5. Prose self-deactivation (qualitative corpus)
# --------------------------------------------------------------------------- #


def test_prose_graph_produces_zero_defects_under_all_classes():
    assert _defects(_prose_graph(), classes=ALL_DEFECT_CLASSES - {"node_count"}) == []


# --------------------------------------------------------------------------- #
# 6. Defect-retry harness in find_or_generate
# --------------------------------------------------------------------------- #


def _question():
    return _candidate(
        problem_text="Find P2 for the pipe.",
        given_values={"P1": 101325.0, "v1": 3.0, "v2": 9.0},
        target_unknown="P2",
        concept_slug="bernoulli_principle",
    )


def _sequenced_chat(responses: list[dict]):
    calls: list[dict] = []

    def _chat(**kwargs):
        calls.append(kwargs)
        payload = responses[min(len(calls), len(responses)) - 1]
        return json.dumps(payload)

    _chat.calls = calls  # type: ignore[attr-defined]
    return _chat


@pytest.mark.asyncio
async def test_defective_draft_is_retried_with_localized_feedback():
    defective = {"reference_solution": _calc_graph()["reference_solution"]}
    defective["reference_solution"] = json.loads(json.dumps(defective["reference_solution"]))
    defective["reference_solution"][1]["depends_on"] = []  # dependency_completeness
    clean = {"reference_solution": _calc_graph()["reference_solution"]}
    chat = _sequenced_chat([defective, clean])

    draft = await find_or_generate(
        None, _question(), retrieve_fn=_retrieve_returning([]), chat_fn=chat
    )

    assert len(chat.calls) == 2  # type: ignore[attr-defined]
    retry_messages = chat.calls[1]["messages"]  # type: ignore[attr-defined]
    feedback = retry_messages[-1]["content"]
    assert "dependency_completeness" in feedback
    assert "'rho'" in feedback and "density_symbol" in feedback  # localized, actionable
    assert retry_messages[-2]["role"] == "assistant"  # the model sees its own draft
    assert "generation_defects" not in draft.provenance


@pytest.mark.asyncio
async def test_exhausted_retries_flag_the_draft_instead_of_crashing():
    defective = {"reference_solution": json.loads(json.dumps(_calc_graph()["reference_solution"]))}
    defective["reference_solution"][1]["depends_on"] = []
    chat = _sequenced_chat([defective, defective, defective])

    draft = await find_or_generate(
        None, _question(), retrieve_fn=_retrieve_returning([]), chat_fn=chat
    )

    assert len(chat.calls) == 3  # initial + 2 retries  # type: ignore[attr-defined]
    assert any(
        d.startswith("dependency_completeness") for d in draft.provenance["generation_defects"]
    )


@pytest.mark.asyncio
async def test_prose_draft_never_retries_and_never_flags():
    prose = {"reference_solution": _prose_graph()["reference_solution"]}
    chat = _sequenced_chat([prose])

    draft = await find_or_generate(
        None,
        _candidate(
            problem_text="Explain why future shock occurs.",
            given_values={},
            target_unknown="future shock",
            concept_slug="future_shock",
        ),
        retrieve_fn=_retrieve_returning([]),
        chat_fn=chat,
    )

    assert len(chat.calls) == 1  # type: ignore[attr-defined]
    assert "generation_defects" not in draft.provenance


@pytest.mark.asyncio
async def test_symbol_table_is_captured_into_provenance():
    payload = {
        "reference_solution": _calc_graph()["reference_solution"],
        "symbol_table": {
            name: {"role": name.lower(), "ontology_key": "", "unit": None}
            for name in ("P1", "P2", "rho", "v1", "v2")
        },
    }
    chat = _sequenced_chat([payload])

    draft = await find_or_generate(
        None, _question(), retrieve_fn=_retrieve_returning([]), chat_fn=chat
    )

    assert len(chat.calls) == 1  # type: ignore[attr-defined]
    assert draft.provenance["symbol_table"]["rho"]["role"] == "rho"


def test_parse_symbol_table_tolerates_junk():
    from apollo.provisioning.solution import _parse_symbol_table

    assert _parse_symbol_table("not json") is None
    assert _parse_symbol_table(json.dumps({"symbol_table": ["not", "a", "dict"]})) is None
    assert _parse_symbol_table(json.dumps({"symbol_table": {"m": {"role": "mass"}}})) == {
        "m": {"role": "mass"}
    }


@pytest.mark.asyncio
async def test_augment_path_defect_retry_adopts_the_fixed_draft():
    """The harness retry parses the AUGMENT envelope (three-key) on that path."""
    defective_steps = json.loads(json.dumps(_calc_graph()["reference_solution"]))
    defective_steps[1]["depends_on"] = []  # dependency_completeness
    defective = {
        "reference_solution": defective_steps,
        "augmented_problem_text": "Find P2 and explain why.",
        "augmented_target_unknown": "P2",
    }
    clean = {
        "reference_solution": _calc_graph()["reference_solution"],
        "augmented_problem_text": "Find P2 and explain why.",
        "augmented_target_unknown": "P2",
    }
    chat = _sequenced_chat([defective, clean])

    draft = await find_or_generate(
        None,
        _question(),
        retrieve_fn=_retrieve_returning([]),
        chat_fn=chat,
        augment_recall=True,
    )

    assert len(chat.calls) == 2  # type: ignore[attr-defined]
    assert "generation_defects" not in draft.provenance
    assert draft.augmented_problem_text == "Find P2 and explain why."
