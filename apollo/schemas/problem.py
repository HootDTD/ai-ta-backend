"""Pydantic schema for a problem file with structured reference solution.

A problem is: a text statement, given values, target unknown, and an ordered
list of KG entries (equation | definition | condition | simplification |
variable_mapping | procedure_step) that must be present in the student's KG
for the solver to reach the target.

V3 changes:
- model_validator enforces depends_on resolution
- model_validator enforces procedure_step.uses_equations resolves to real
  equation ids in the same problem (kills checklist item 7's silent typos)
- Procedure-step `order` must form a 1..N contiguous sequence
- to_kg_graph(attempt_id) -> KGGraph derives a typed reference subgraph
  from the validated problem
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from apollo.ontology import (
    Edge,
    EdgeType,
    KGGraph,
    Node,
    NodeType,
    build_node,
)

EntryType = Literal[
    "equation",
    "definition",
    "condition",
    "simplification",
    "variable_mapping",
    "procedure_step",
]
Difficulty = Literal["intro", "standard", "hard"]


class ReferenceStep(BaseModel):
    step: int = Field(ge=1)
    entry_type: EntryType
    id: str = Field(min_length=1)
    content: dict[str, Any]
    depends_on: list[str] = Field(default_factory=list)
    # F-struct: canonical entity key authored per step (e.g. "def.real_basis").
    # Optional so pre-seeded / non-layer1 problems validate unchanged; when
    # present it flows onto the reference Node in to_kg_graph.
    entity_key: str | None = None


class Problem(BaseModel):
    id: str = Field(min_length=1)
    concept_id: str = Field(min_length=1)
    difficulty: Difficulty
    problem_text: str = Field(min_length=1)
    # Subject-fluid Apollo: given_values / target_unknown are OPTIONAL per subject
    # profile. A quantitative_symbolic problem still supplies both (and the
    # promotion-lint gates 4/5 enforce that target_unknown is a real canonical
    # symbol — an empty/omitted target fails gate 4, the sole foreign-symbol
    # guard). A qualitative_argumentative problem may omit given_values (no numeric
    # givens) and carry a PROSE target_unknown or none at all; gates 4/5 are OFF
    # under that profile so the symbol contract is not imposed. Defaults keep every
    # existing fluid problem byte-identical (they always pass both explicitly).
    given_values: dict[str, float] = Field(default_factory=dict)
    target_unknown: str = Field(default="")
    reference_solution: list[ReferenceStep] = Field(min_length=1)

    # ------------------------------------------------------------------ #
    # Cross-reference validators (checklist item 7)                      #
    # ------------------------------------------------------------------ #

    @model_validator(mode="after")
    def _resolve_references(self) -> Problem:
        ids = {step.id for step in self.reference_solution}
        eq_ids = {step.id for step in self.reference_solution if step.entry_type == "equation"}

        for step in self.reference_solution:
            for dep in step.depends_on:
                if dep not in ids:
                    raise ValueError(
                        f"problem {self.id!r} step {step.id!r}: "
                        f"depends_on {dep!r} not found in reference_solution"
                    )

            if step.entry_type == "procedure_step":
                used = step.content.get("uses_equations", []) or []
                for u in used:
                    if u not in eq_ids:
                        raise ValueError(
                            f"problem {self.id!r} procedure_step {step.id!r}: "
                            f"uses_equations {u!r} is not an equation id "
                            f"in this problem (have: {sorted(eq_ids)})"
                        )

        proc_steps = [s for s in self.reference_solution if s.entry_type == "procedure_step"]
        proc_steps.sort(key=lambda s: int(s.content.get("order", 0)))
        for i, step in enumerate(proc_steps, start=1):
            order = step.content.get("order")
            if order != i:
                raise ValueError(
                    f"problem {self.id!r} procedure_step {step.id!r}: "
                    f"order={order!r}, expected {i} "
                    f"(must be 1..N contiguous)"
                )

        return self

    # ------------------------------------------------------------------ #
    # KGGraph derivation (V3 reference graph)                            #
    # ------------------------------------------------------------------ #

    def to_kg_graph(self, attempt_id: int) -> KGGraph:
        """Derive a typed reference subgraph from this problem.

        Edges:
        - DEPENDS_ON for every depends_on entry
        - USES (procedure_step -> equation) for every uses_equations entry
        - PRECEDES chain across procedure_steps in `order`
        """
        nodes: list[Node] = []
        for step in self.reference_solution:
            content = self._strip_legacy_proc_fields(step)
            node = build_node(
                node_type=step.entry_type,  # type: ignore[arg-type]
                node_id=step.id,
                attempt_id=attempt_id,
                source="reference",
                content=content,
                entity_key=step.entity_key,
            )
            nodes.append(node)

        edges: list[Edge] = []
        node_type_lookup: dict[str, NodeType] = {
            s.id: s.entry_type for s in self.reference_solution
        }

        # DEPENDS_ON edges
        for step in self.reference_solution:
            for dep in step.depends_on:
                from_t = node_type_lookup[step.id]
                to_t = node_type_lookup[dep]
                # Ontology forbids same-type self-edge of DEPENDS_ON only when
                # ids match; node-type sameness is allowed.
                if step.id == dep:
                    continue
                edges.append(
                    Edge(
                        edge_type=EdgeType.DEPENDS_ON,
                        from_node_id=step.id,
                        to_node_id=dep,
                        attempt_id=attempt_id,
                        source="reference",
                        from_node_type=from_t,
                        to_node_type=to_t,
                    )
                )

        # USES edges (procedure_step -> equation)
        for step in self.reference_solution:
            if step.entry_type != "procedure_step":
                continue
            for eq_id in step.content.get("uses_equations", []) or []:
                edges.append(
                    Edge(
                        edge_type=EdgeType.USES,
                        from_node_id=step.id,
                        to_node_id=eq_id,
                        attempt_id=attempt_id,
                        source="reference",
                        from_node_type="procedure_step",
                        to_node_type="equation",
                    )
                )

        # PRECEDES chain across procedure steps in order
        proc = sorted(
            (s for s in self.reference_solution if s.entry_type == "procedure_step"),
            key=lambda s: int(s.content["order"]),
        )
        for prev, nxt in zip(proc, proc[1:], strict=False):
            edges.append(
                Edge(
                    edge_type=EdgeType.PRECEDES,
                    from_node_id=prev.id,
                    to_node_id=nxt.id,
                    attempt_id=attempt_id,
                    source="reference",
                    from_node_type="procedure_step",
                    to_node_type="procedure_step",
                )
            )

        return KGGraph(nodes=nodes, edges=edges)

    @staticmethod
    def _strip_legacy_proc_fields(step: ReferenceStep) -> dict[str, Any]:
        """Drop fields that have moved to edges (order, uses_equations).

        ProcedureStepContent only takes action+purpose. order is encoded as
        the PRECEDES chain; uses_equations is encoded as USES edges.
        """
        if step.entry_type != "procedure_step":
            return dict(step.content)
        return {
            "action": step.content.get("action", ""),
            "purpose": step.content.get("purpose", ""),
        }


def load_problem(path: str | Path) -> Problem:
    """Load and validate a problem JSON file."""
    text = Path(path).read_text()
    return Problem.model_validate_json(text)
