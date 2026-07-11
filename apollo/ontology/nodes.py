"""Apollo V3 KG node taxonomy.

Six node types — one per checklist-1 KG entry kind. Pydantic discriminated
union (absorbs checklist item 8). Each node carries `node_id` (unique within
an attempt subgraph), `attempt_id` (subgraph scoping), `source` (provenance),
and a typed `content` payload.

Procedure-step `order` and `uses_equations` fields are gone — order is
derivable from the PRECEDES edge chain, and equation links are real USES
edges (see apollo.ontology.edges).
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, Field

NodeType = Literal[
    "equation",
    "condition",
    "simplification",
    "definition",
    "variable_mapping",
    "procedure_step",
]

NodeSource = Literal["parser", "reference", "system"]

# Node negotiation status (P3 — Negotiable OLM, migration 021).
#   ACCEPTED  — parser-authored, student has not contested. Default for new
#               nodes; coverage grades the parser's surface form.
#   DISPUTED  — student has flagged the entry as wrong / misheard. Done-gate
#               (P3.6) blocks until at least one negotiation move is logged.
#   DUAL      — system + student each hold a belief (paraphrase or skip).
#               Coverage uses `student_belief` if non-null; else `content`.
NodeStatus = Literal["ACCEPTED", "DISPUTED", "DUAL"]


class _NodeBase(BaseModel):
    node_id: str = Field(min_length=1)
    attempt_id: int
    source: NodeSource
    # LLM-reported self-confidence in the parser's extraction of this node.
    # Range [0, 1]. Default 1.0 — non-parser sources (reference, system) and
    # legacy nodes without the field are treated as authoritative. The P3
    # OLM trigger is `parser_confidence < 0.6`, so the default protects
    # legacy data from false-firing the Done-gate.
    parser_confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    # Negotiable OLM (P3). Default ACCEPTED — pre-P3 nodes round-trip with
    # no behavioral change. DISPUTED / DUAL flip on student moves through
    # apollo_kg_negotiations; coverage and the Done-gate read these fields.
    status: NodeStatus = Field(default="ACCEPTED")
    # The student's preferred surface form for this entry, if they have
    # supplied a paraphrase. None until the student paraphrases. Coverage
    # uses this for DUAL entries; ACCEPTED entries ignore it (the parser's
    # `content` is operative).
    student_belief: str | None = Field(default=None)
    # F-struct: the canonical entity/concept key this node maps to
    # (authoring name `entity_key`, e.g. "def.real_basis"). Populated on
    # REFERENCE nodes only (Problem.to_kg_graph); parser/system/legacy nodes
    # stay None. Lets the structural co-key gate name the misconception a bank
    # entry `opposes` for this node. Default None keeps every non-reference and
    # pre-F-struct node byte-identical.
    entity_key: str | None = Field(default=None)


# --- Per-type content payloads ---------------------------------------------


class EquationContent(BaseModel):
    symbolic: str = Field(min_length=1)
    label: str = Field(default="")
    variables: list[str] = Field(default_factory=list)


class ConditionContent(BaseModel):
    applies_when: str = Field(min_length=1)
    label: str = Field(default="")


class SimplificationContent(BaseModel):
    applies_when: str = Field(min_length=1)
    transformation: str = Field(min_length=1)


class DefinitionContent(BaseModel):
    concept: str = Field(min_length=1)
    meaning: str = Field(min_length=1)


class VariableMappingContent(BaseModel):
    term: str = Field(min_length=1)
    symbol: str = Field(min_length=1)


class ProcedureStepContent(BaseModel):
    action: str = Field(min_length=1)
    purpose: str = Field(default="")


# --- Discriminated node union ----------------------------------------------


class EquationNode(_NodeBase):
    node_type: Literal["equation"] = "equation"
    content: EquationContent


class ConditionNode(_NodeBase):
    node_type: Literal["condition"] = "condition"
    content: ConditionContent


class SimplificationNode(_NodeBase):
    node_type: Literal["simplification"] = "simplification"
    content: SimplificationContent


class DefinitionNode(_NodeBase):
    node_type: Literal["definition"] = "definition"
    content: DefinitionContent


class VariableMappingNode(_NodeBase):
    node_type: Literal["variable_mapping"] = "variable_mapping"
    content: VariableMappingContent


class ProcedureStepNode(_NodeBase):
    node_type: Literal["procedure_step"] = "procedure_step"
    content: ProcedureStepContent


Node = Annotated[
    EquationNode
    | ConditionNode
    | SimplificationNode
    | DefinitionNode
    | VariableMappingNode
    | ProcedureStepNode,
    Field(discriminator="node_type"),
]


# Map node_type string -> Neo4j label string. Application code applies the
# returned label PLUS the secondary :_KGNode label so a single index covers
# all subgraph reads + cleanup.
NODE_LABELS: dict[NodeType, str] = {
    "equation": "Equation",
    "condition": "Condition",
    "simplification": "Simplification",
    "definition": "Definition",
    "variable_mapping": "VariableMapping",
    "procedure_step": "ProcedureStep",
}

# Reverse lookup for read-back.
NODE_LABEL_TO_TYPE: dict[str, NodeType] = {v: k for k, v in NODE_LABELS.items()}

NODE_CONTENT_TYPES: dict[NodeType, type[BaseModel]] = {
    "equation": EquationContent,
    "condition": ConditionContent,
    "simplification": SimplificationContent,
    "definition": DefinitionContent,
    "variable_mapping": VariableMappingContent,
    "procedure_step": ProcedureStepContent,
}


def build_node(
    *,
    node_type: NodeType,
    node_id: str,
    attempt_id: int,
    source: NodeSource,
    content: dict,
    parser_confidence: float = 1.0,
    status: NodeStatus = "ACCEPTED",
    student_belief: str | None = None,
    entity_key: str | None = None,
) -> Node:
    """Construct a typed node from a (type, content_dict) pair."""
    cls_map: dict[NodeType, type[_NodeBase]] = {
        "equation": EquationNode,
        "condition": ConditionNode,
        "simplification": SimplificationNode,
        "definition": DefinitionNode,
        "variable_mapping": VariableMappingNode,
        "procedure_step": ProcedureStepNode,
    }
    cls = cls_map[node_type]
    return cls(
        node_id=node_id,
        attempt_id=attempt_id,
        source=source,
        parser_confidence=parser_confidence,
        status=status,
        student_belief=student_belief,
        entity_key=entity_key,
        content=content,  # type: ignore[arg-type]
    )
