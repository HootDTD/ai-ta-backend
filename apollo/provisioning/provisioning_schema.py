"""WU-3B2x — strict/declared ``json_schema`` builders for the provisioning LLM call-sites.

These builders derive every declared field-name set FROM the Pydantic models so
the prompt↔parser contract can never drift (mirrors
``apollo/parser/extraction_schema.py``). Pure data builders — no LLM, no I/O.

Single source of truth:
  * Stage-2 (``find_or_generate``) outer envelope keys come from
    ``apollo.schemas.problem.ReferenceStep.model_fields`` (``REFERENCE_STEP_FIELDS``).
  * The per-``entry_type`` ``content`` field hints come from
    ``apollo.ontology.nodes.NODE_CONTENT_TYPES`` (the same source the ontology uses).
  * The ``entry_type`` enum is ``tuple(NODE_CONTENT_TYPES.keys())`` (the six
    ``EntryType`` literal values).

Strict-mode nuance (Decision #2): ``ReferenceStep.content`` is an OPEN per-type
dict (and ``Problem.given_values`` keys are arbitrary symbols), so the Stage-2
solution schema CANNOT be fully strict-closed — it declares ``content`` as a
permissive object and runs with ``strict=False``. ``Problem.model_validate``
remains the hard post-parse enforcer of the inner per-type dicts. The Stage-4 tag
schema has no open dicts and therefore CAN be strict (``strict=True``).
"""

from __future__ import annotations

from apollo.ontology.nodes import NODE_CONTENT_TYPES
from apollo.schemas.problem import ReferenceStep

__all__ = [
    "REFERENCE_STEP_FIELDS",
    "ENTRY_TYPES",
    "build_solution_schema",
    "build_tag_schema",
    "build_pairing_phase_a_schema",
    "build_pairing_phase_b_schema",
    "solution_content_field_hints",
]

# Single source of truth — NEVER hand-type these lists. They are derived from the
# Pydantic models so adding/removing a model field RED-flags the contract tests.
REFERENCE_STEP_FIELDS: tuple[str, ...] = tuple(ReferenceStep.model_fields.keys())
# The six ontology entry types (== the EntryType literal set).
ENTRY_TYPES: tuple[str, ...] = tuple(NODE_CONTENT_TYPES.keys())

# The two raw-dict extras a procedure_step's ``content`` carries that
# ``ProcedureStepContent`` does NOT declare — they live in the raw ``content``
# dict and are read by ``Problem._resolve_references`` (stripped at
# ``to_kg_graph`` time by ``_strip_legacy_proc_fields``). Surfaced in the prose
# hints so the model fills them.
_PROCEDURE_STEP_RAW_EXTRAS: tuple[str, ...] = ("order", "uses_equations")


def build_solution_schema() -> dict:
    """Return the Stage-2 ``json_schema`` payload (a FRESH dict per call).

    The OUTER envelope ``{reference_solution: [ <ReferenceStep>... ]}`` is pinned
    so the model emits the right top-level key + step skeleton; the per-type
    ``content`` is an OPEN object (``Problem.model_validate`` enforces it). NOT
    strict (``strict=False``) because of the open ``content`` dict (Decision #2).
    The ``items`` ``required``/``properties`` key set == ``REFERENCE_STEP_FIELDS``
    (the contract test pins this).
    """
    return {
        "name": "reference_solution",
        # content is an open per-type dict; cannot be strict-closed (Decision #2).
        "strict": False,
        "schema": {
            "type": "object",
            "properties": {
                "reference_solution": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": list(REFERENCE_STEP_FIELDS),
                        "properties": {
                            "step": {"type": "integer"},
                            "entry_type": {"type": "string", "enum": list(ENTRY_TYPES)},
                            "id": {"type": "string"},
                            # open per-type dict (Pydantic validates the inner shape).
                            "content": {"type": "object"},
                            "depends_on": {"type": "array", "items": {"type": "string"}},
                            # F-struct: authored canonical entity key (nullable —
                            # ReferenceStep.entity_key defaults to None).
                            "entity_key": {"type": ["string", "null"]},
                        },
                    },
                },
            },
            "required": ["reference_solution"],
        },
    }


def build_tag_schema() -> dict:
    """Return the Stage-4 concept-tag ``json_schema`` payload (a FRESH dict per call).

    Strict-capable (no open dicts) → ``strict=True``. ``_parse_tag`` only HARD-
    requires ``concept_slug``; ``display_name``/``prereqs`` are read with defaults
    (``tag.get(...)``). OpenAI strict mode requires EVERY property in ``required``,
    so all three are required in the SCHEMA — an empty ``prereqs: []`` and a
    ``display_name`` equal to the slug satisfy BOTH the strict schema and the
    lenient parser.
    """
    return {
        "name": "concept_tag",
        "strict": True,
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["concept_slug", "display_name", "prereqs"],
            "properties": {
                "concept_slug": {"type": "string"},
                "display_name": {"type": "string"},
                "prereqs": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["from", "to"],
                        "properties": {
                            "from": {"type": "string"},
                            "to": {"type": "string"},
                        },
                    },
                },
            },
        },
    }


def build_pairing_phase_a_schema() -> dict:
    """Return the Stage-3 Phase-A (pairing / answer-relevance) ``json_schema``
    payload (a FRESH dict per call).

    Strict-capable (no open dicts) → ``strict=True``. The keys mirror exactly what
    ``pairing_gate.validate_pair`` reads from the Phase-A response
    (``phase_a.get("paired")`` and ``phase_a.get("confidence")``); the contract
    test pins ``required`` == those reads.
    """
    return {
        "name": "pairing_phase_a",
        "strict": True,
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["paired", "confidence"],
            "properties": {
                "paired": {"type": "boolean"},
                "confidence": {"type": "number"},
            },
        },
    }


def build_pairing_phase_b_schema() -> dict:
    """Return the Stage-3 Phase-B (claim-decomposed faithfulness) ``json_schema``
    payload (a FRESH dict per call).

    Strict-capable → ``strict=True``. The shape mirrors exactly what
    ``pairing_gate.validate_pair`` reads from the Phase-B response: a ``claims``
    array whose entries each carry ``claim`` (string) and ``entailed`` (bool)
    (``c.get("claim")`` / ``c.get("entailed")``). The contract test pins these.
    """
    return {
        "name": "pairing_phase_b",
        "strict": True,
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["claims"],
            "properties": {
                "claims": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["claim", "entailed"],
                        "properties": {
                            "claim": {"type": "string"},
                            "entailed": {"type": "boolean"},
                        },
                    },
                },
            },
        },
    }


def solution_content_field_hints() -> str:
    """Build a human-readable per-``entry_type`` content-field listing from
    ``NODE_CONTENT_TYPES`` for the Stage-2 prose prompt.

    Each entry_type names its declared content fields (sourced from the model's
    ``model_fields``); ``procedure_step`` additionally names the two raw-dict
    extras (``order``, ``uses_equations``) that ``Problem._resolve_references``
    requires. Keeps the prose honest with the ontology — never a hand-typed list.
    """
    lines: list[str] = []
    for entry_type, model in NODE_CONTENT_TYPES.items():
        fields = list(model.model_fields.keys())
        if entry_type == "procedure_step":
            fields = [*fields, *_PROCEDURE_STEP_RAW_EXTRAS]
        lines.append(f"{entry_type} -> {{{', '.join(fields)}}}")
    return "; ".join(lines)
