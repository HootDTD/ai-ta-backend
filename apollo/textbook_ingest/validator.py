# apollo/textbook_ingest/validator.py
"""Strict 8-gate validator. This task ships Gate 1 (schema) only; later tasks
fill gates 2-8. Short-circuits on first failing gate. See spec section 6."""
from __future__ import annotations

from dataclasses import dataclass

from apollo.ontology.nodes import build_node
from apollo.subjects import ConceptDefinition
from apollo.textbook_ingest.kg_convert import REFERENCE_ATTEMPT_ID
from apollo.textbook_ingest.types import ExtractedProblem


@dataclass
class ValidationResult:
    ok: bool
    gate_failed: str | None = None
    diagnostic: str = ""


def _gate1_schema(p: ExtractedProblem, concept: ConceptDefinition) -> ValidationResult:
    # ExtractedProblem itself is already Pydantic-validated; re-validate each
    # reference node's typed content via the ontology builder.
    for step in p.reference_solution:
        try:
            build_node(node_type=step.entry_type, node_id=step.id,
                       attempt_id=REFERENCE_ATTEMPT_ID, source="reference",
                       content=step.content)
        except Exception as exc:  # noqa: BLE001 - surface as a gate diagnostic
            return ValidationResult(False, "schema", f"node {step.id!r}: {exc}")
    return ValidationResult(True)


# Gate registry. Later tasks append gates 2-8 in order.
_GATES = [_gate1_schema]


def validate_problem(p: ExtractedProblem, concept: ConceptDefinition) -> ValidationResult:
    for gate in _GATES:
        res = gate(p, concept)
        if not res.ok:
            return res
    return ValidationResult(True)
