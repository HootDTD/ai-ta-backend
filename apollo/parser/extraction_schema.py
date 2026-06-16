"""WU-2A: strict OpenAI `json_schema` for one-call node + typed-edge extraction.

`build_extraction_schema() -> dict` returns the `json_schema` payload for the
parser's single GPT-4o structured-outputs call. Ported from the RQ3 spike's
`RESPONSE_SCHEMA` (`scripts/spikes/rq3_edge_extraction.py` — REFERENCE ONLY,
never imported) with TWO production additions:
  1. a `provenance` enum (`explicit|inferred`) on every edge, and
  2. the schema named/wired for the production `response_format`.

Strict-mode rules this schema MUST honor (OpenAI structured outputs):
`strict: true`, `additionalProperties: false` on every object, and EVERY
property listed in `required`. Optional-by-value fields are expressed as
nullable types (`{"type": ["string", "null"]}`) rather than omitted — this is
why all type-specific entry fields are present-and-nullable. This is a pure
data builder (no LLM, no I/O); it lives in its own module so `parser_llm.py`
does not grow an ~80-line schema literal.
"""
from __future__ import annotations

from apollo.ontology import EdgeType

# Node entry types — the six NodeType values, kept as a list so the enum order
# is stable for the (offline) strict-schema assertion test.
_ENTRY_TYPES = [
    "equation",
    "condition",
    "simplification",
    "definition",
    "variable_mapping",
    "procedure_step",
]

# Edge endpoint refs are "n<i>" (i-th entry of THIS response, 0-based) or an
# existing-graph node id — resolved by `_resolve_typed_edges`.
_EDGE_TYPES = [e.value for e in EdgeType]

_EDGE_PROVENANCE = ["explicit", "inferred"]


def build_extraction_schema() -> dict:
    """Return the strict `json_schema` payload for the one-call parser.

    A fresh dict per call (immutable-style — callers never share/mutate a
    module-global schema object).
    """
    entry_properties: dict = {
        "type": {"type": "string", "enum": list(_ENTRY_TYPES)},
        "confidence": {"type": "number"},
        # existing-graph id this entry refers to (node-dedup hint; nullable).
        "reuse_of": {"type": ["string", "null"]},
        # equation
        "symbolic": {"type": ["string", "null"]},
        "label": {"type": ["string", "null"]},
        "variables": {"type": ["array", "null"], "items": {"type": "string"}},
        # condition / simplification
        "applies_when": {"type": ["string", "null"]},
        "transformation": {"type": ["string", "null"]},
        # definition
        "concept": {"type": ["string", "null"]},
        "meaning": {"type": ["string", "null"]},
        # variable_mapping
        "term": {"type": ["string", "null"]},
        "symbol": {"type": ["string", "null"]},
        # procedure_step
        "action": {"type": ["string", "null"]},
        "purpose": {"type": ["string", "null"]},
        # procedure_step USES self-reference: 0-based indices into THIS
        # response's `entries` naming the equation entries the step applies.
        # Nullable int-array (use [] for none, null when not a step). The
        # deterministic within-turn USES fallback (`_resolve_uses_edges`)
        # reads it when the model emits no typed `edges` and no graph_context
        # is supplied, so it MUST be part of the strict schema (a field absent
        # from the schema can never arrive under additionalProperties:false).
        "uses_equation_ordinals": {
            "type": ["array", "null"],
            "items": {"type": "integer"},
        },
    }

    edge_properties: dict = {
        "edge_type": {"type": "string", "enum": list(_EDGE_TYPES)},
        "from_ref": {"type": "string"},
        "to_ref": {"type": "string"},
        "provenance": {"type": "string", "enum": list(_EDGE_PROVENANCE)},
    }

    return {
        "name": "kg_extraction",
        "strict": True,
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["entries", "edges"],
            "properties": {
                "entries": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": list(entry_properties.keys()),
                        "properties": entry_properties,
                    },
                },
                "edges": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": list(edge_properties.keys()),
                        "properties": edge_properties,
                    },
                },
            },
        },
    }
