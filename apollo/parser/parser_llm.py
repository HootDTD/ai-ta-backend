"""Parser: student utterance → typed Nodes + Edges via GPT-4o JSON mode.

V3 contract:
- Concept-driven system prompt (loaded from the registry, no hardcoded
  fluid-mechanics text in this file)
- Returns (list[Node], list[Edge]) — typed and ready to write
- Procedure-step USES edges are by node_id (resolved from the LLM's
  `uses_equation_ordinals` self-references in the same response)
- Procedure-step PRECEDES edges chain consecutive procedure steps in a
  single utterance

Under no-fallback policy: if the utterance LOOKS like a teaching attempt and
the LLM extracts zero entries, raise ParserCouldNotExtractError. Short
acknowledgements legitimately produce empty extractions and do NOT raise.
"""
from __future__ import annotations

import json
import os
import re
import uuid
from typing import Any

from openai import OpenAI

from apollo.errors import ParserCouldNotExtractError
from apollo.ontology import (
    EDGE_ALLOWED_PAIRS,
    Edge,
    EdgeType,
    Node,
    NodeType,
    build_node,
)
from apollo.parser.prompt_builder import build_system_prompt
from apollo.subjects import ConceptDefinition

_EQUATION_LIKE = re.compile(r"[=*/^+\-]|\d+\.?\d*|\^|\*\*")
_TRIVIAL_ACKS = frozenset({
    "ok", "okay", "yes", "no", "hmm", "hi", "hey", "thanks", "thx", "ty",
})


def _is_non_trivial(utterance: str, concept: ConceptDefinition) -> bool:
    """Concept-aware triviality check.

    Keywords + plan markers come from the concept's solver_hints registry —
    no global fluid-mechanics list. Falls back to math-character heuristic
    for utterances with no domain keywords.
    """
    s = utterance.strip().lower()
    if len(s) < 10:
        return False
    if s in _TRIVIAL_ACKS:
        return False
    if _EQUATION_LIKE.search(utterance):
        return True
    keywords = tuple(concept.solver_hints.non_trivial_keywords)
    if keywords and any(k in s for k in keywords):
        return True
    plan_markers = tuple(concept.solver_hints.plan_markers)
    return bool(plan_markers) and any(m in s for m in plan_markers)


def _entry_to_node(
    entry: dict,
    *,
    attempt_id: int,
    fallback_node_id: str,
) -> Node | None:
    """Convert a single LLM entry to a typed Node, or return None if invalid."""
    t = entry.get("type")
    if t not in {"equation", "condition", "simplification",
                 "definition", "variable_mapping", "procedure_step"}:
        return None
    content = dict(entry.get("content") or {})
    # Drop legacy fields the V3 parser shouldn't emit anymore.
    if t == "procedure_step":
        content.pop("order", None)
        content.pop("uses_equations", None)

    try:
        return build_node(
            node_type=t,  # type: ignore[arg-type]
            node_id=fallback_node_id,
            attempt_id=attempt_id,
            source="parser",
            content=content,
        )
    except Exception:  # noqa: BLE001 - skip malformed entries
        return None


def _resolve_uses_edges(
    raw_entries: list[dict],
    nodes: list[Node],
    *,
    attempt_id: int,
) -> list[Edge]:
    """Resolve `uses_equation_ordinals` ints into USES edges by node_id.

    Drops edges with out-of-range ordinals or non-equation targets.
    """
    edges: list[Edge] = []
    for raw, node in zip(raw_entries, nodes):
        if node.node_type != "procedure_step":
            continue
        ordinals = raw.get("uses_equation_ordinals") or []
        if not isinstance(ordinals, list):
            continue
        for o in ordinals:
            if not isinstance(o, int) or o < 0 or o >= len(nodes):
                continue
            target = nodes[o]
            if target.node_type != "equation":
                continue
            try:
                edges.append(Edge(
                    edge_type=EdgeType.USES,
                    from_node_id=node.node_id,
                    to_node_id=target.node_id,
                    attempt_id=attempt_id,
                    source="parser",
                    from_node_type="procedure_step",
                    to_node_type="equation",
                ))
            except ValueError:
                continue
    return edges


def _build_precedes_chain(
    nodes: list[Node],
    *,
    attempt_id: int,
) -> list[Edge]:
    """Chain consecutive procedure_step nodes in `nodes` order with PRECEDES."""
    proc = [n for n in nodes if n.node_type == "procedure_step"]
    edges: list[Edge] = []
    for prev, nxt in zip(proc, proc[1:]):
        try:
            edges.append(Edge(
                edge_type=EdgeType.PRECEDES,
                from_node_id=prev.node_id,
                to_node_id=nxt.node_id,
                attempt_id=attempt_id,
                source="parser",
                from_node_type="procedure_step",
                to_node_type="procedure_step",
            ))
        except ValueError:
            continue
    return edges


def parse_utterance(
    utterance: str,
    *,
    concept: ConceptDefinition,
    attempt_id: int,
    model: str | None = None,
) -> tuple[list[Node], list[Edge]]:
    """Return (nodes, edges) for a student utterance.

    Raises ParserCouldNotExtractError when a non-trivial utterance yields
    zero extractions.
    """
    model = model or os.getenv("MAIN_MODEL", "gpt-4o")
    system_prompt = build_system_prompt(concept)

    client = OpenAI()
    resp = client.chat.completions.create(
        model=model,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": utterance},
        ],
        temperature=0.0,
    )
    raw = resp.choices[0].message.content or "{}"
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        if _is_non_trivial(utterance, concept):
            raise ParserCouldNotExtractError(utterance=utterance)
        return [], []

    raw_entries = payload.get("entries", [])
    if not isinstance(raw_entries, list):
        raw_entries = []

    nodes: list[Node] = []
    kept_raw: list[dict] = []
    for e in raw_entries:
        if not isinstance(e, dict) or "type" not in e or "content" not in e:
            continue
        node = _entry_to_node(
            e,
            attempt_id=attempt_id,
            fallback_node_id=f"stu_{uuid.uuid4().hex[:12]}",
        )
        if node is None:
            continue
        nodes.append(node)
        kept_raw.append(e)

    if not nodes and _is_non_trivial(utterance, concept):
        raise ParserCouldNotExtractError(utterance=utterance)

    edges: list[Edge] = []
    edges.extend(_resolve_uses_edges(kept_raw, nodes, attempt_id=attempt_id))
    edges.extend(_build_precedes_chain(nodes, attempt_id=attempt_id))

    return nodes, edges
