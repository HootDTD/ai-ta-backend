"""The SINGLE generation ontology block every reference-solution prompt sources.

DAG-3 (problem-generation master plan, Track 0.4): the per-step contract used to
be authored three separate times (``solution.py`` extract/generate,
``authored_sets/graph_derivation.py`` derivation, ``solution.py`` authored
construction) — a rule added to one did not propagate. This module is now the
only place the shared step contract lives; each prompt keeps its own FRAMING
(what the model converts, which extra top-level keys it emits) and appends
``ontology_block()``.

The block renders byte-stably from the ontology (`solution_content_field_hints`
sources ``NODE_CONTENT_TYPES``) so prompt-hash-style tests are possible and the
prose can never drift from the schema.
"""

from __future__ import annotations

from apollo.provisioning.provisioning_schema import solution_content_field_hints

__all__ = ["ontology_block", "SHARED_CONSTANT_SYMBOLS", "GENERIC_ID_TOKENS"]


# Symbols that are ambient mathematical/physical constants: an equation may use
# them without an upstream variable_mapping/definition node, and they never
# create a dependency edge by themselves (the dependency-completeness defect
# class whitelists them — a shared constant must not fabricate a false dep).
SHARED_CONSTANT_SYMBOLS: frozenset[str] = frozenset({"pi", "e", "g", "c", "R", "k_B", "N_A"})

# Tokens that carry no meaning on their own inside a step id / entity key —
# an id whose every alphabetic token is generic (``step_2``, ``eq1``,
# ``vm_a``, ``node_3``) is a semantic-entity-key defect: entity keys derive
# from these ids at mint time, and an opaque id yields an opaque key that
# defeats cross-problem entity resolution.
GENERIC_ID_TOKENS: frozenset[str] = frozenset(
    {"step", "eq", "eqn", "equation", "node", "item", "vm", "ps", "var", "sym", "entry", "s"}
)


def ontology_block() -> str:
    """The shared per-step output contract (framing-free, envelope-free).

    Every reference-solution prompt appends this VERBATIM after its own framing
    and top-level-key envelope. Rules stated here are enforced by a mechanical
    validator (``find_derivation_defects`` at generation time, the promotion
    lint after) — the prompt and the validators must never disagree.
    """
    return (
        'Each step object has EXACTLY these keys: "step" (integer >= 1, 1-based '
        'position), "entry_type", "id", "content" (object), "depends_on" (array '
        'of step "id" strings, [] if none).\n'
        '"entry_type" is exactly one of "equation", "condition", '
        '"simplification", "definition", "variable_mapping", "procedure_step"; '
        f'its "content" fields are {solution_content_field_hints()}.\n'
        "Cross-step rules the validator enforces:\n"
        '- Every "depends_on" id must be a real step "id" in this solution; the '
        "dependency graph must be acyclic.\n"
        "- A procedure_step's content.uses_equations must list real equation "
        'step "id"s; procedure_step content.order values must be 1..N '
        "contiguous across the procedure_steps.\n"
        "- IDs are meaningful snake_case English naming the step's CONTENT "
        '(e.g. "ibp_formula", "equal_pressure_simplification"). NEVER opaque or '
        'mechanical ids like "step_2", "eq1", "vm_a", "node_3" — entity keys '
        "derive from these ids, and an opaque id defeats entity resolution.\n"
        "- Every symbol an equation uses must be defined for the reader: a "
        "problem given, a declared bound variable, the target, a shared "
        "mathematical constant, or bound by a variable_mapping/definition step "
        'that the equation (transitively) "depends_on".\n'
        'OPTIONALLY include a top-level "symbol_table" object mapping EVERY '
        "symbol your equations use to "
        '{"role": short lowercase role id, "ontology_key": the concept-'
        'vocabulary key it instantiates ("" if none), "unit": its unit string '
        "or null}. Symbols are case-sensitive: m and M are DIFFERENT quantities "
        "and need two entries; never reuse one casing for both.\n"
        "Return the JSON object ONLY — no prose, no explanation, no markdown "
        "code fences."
    )
