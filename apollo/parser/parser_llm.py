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

V3 triviality detection (item #4): replaced the FM-only keyword/plan-marker
scan with a cheap LLM classifier. Keeps the deterministic short-circuits
(length floor, ACK list, math-character heuristic) so the LLM is only
consulted on ambiguous prose.
"""
from __future__ import annotations

import json
import logging
import re
import uuid

from openai import OpenAI

from apollo.agent._llm import cheap_chat
from apollo.errors import ParserCouldNotExtractError
from apollo.ontology import (
    NODE_CONTENT_TYPES,
    Edge,
    EdgeType,
    Node,
    NodeType,
    build_node,
)
from apollo.parser.edge_resolver import resolve_typed_edges
from apollo.parser.extraction_schema import build_extraction_schema
from apollo.parser.graph_context import GraphContext
from apollo.parser.prompt_builder import build_system_prompt
from apollo.subjects import ConceptDefinition
from config import models

_LOG = logging.getLogger(__name__)

# Equation-like signals: `=`, `*`, `+`, `^`, `/`, digits. NOT `-` —
# hyphens appear in compound words ("non-trivial") and would false-positive.
_EQUATION_LIKE = re.compile(r"[=*/^+]|\d+\.?\d*|\^|\*\*")
_TRIVIAL_ACKS = frozenset({
    "ok", "okay", "yes", "no", "hmm", "hi", "hey", "thanks", "thx", "ty",
})

# Confidence threshold above which the LLM classifier's "is_teaching=true"
# decision is acted on. Below threshold => treat as trivial (return empty
# silently). Same threshold as the leakage judge (item #3) for symmetry.
_TEACHING_CONFIDENCE_THRESHOLD: float = 0.6

_TRIVIALITY_CLASSIFIER_PROMPT = """You decide whether a student utterance
is an attempt to TEACH a concept (explain it, write equations, describe a
procedure, define terms) or just conversational filler / a question / an
acknowledgement.

You will be given:
- the concept the student is supposed to be teaching about,
- the student's utterance.

Return ONLY a JSON object:
{"is_teaching": <bool>, "confidence": <float in [0, 1]>,
 "reason": <one short sentence>}

Heuristics:
- Equations, formulas, conditional rules, procedural steps => teaching.
- Plain prose that describes how something works, even without symbols,
  IS teaching when it conveys a domain claim — e.g.
  "what comes in must equal what goes out" => teaching (continuity).
- Greetings, "ok", "first hi there", "thanks" => not teaching.
- Questions BACK to the assistant ("what should I do next?") => not teaching.
"""


def _classify_teaching(
    utterance: str,
    concept: ConceptDefinition,
) -> tuple[bool, float]:
    """LLM-driven teaching-intent classifier.

    Soft-fails to (False, 0.0) on parse/network errors — i.e. errs toward
    "treat as trivial" so a transient hiccup never produces a spurious
    ParserCouldNotExtractError. The downstream parser still has to extract
    real entries from genuinely teaching utterances; if the classifier
    misses one, the parser just returns empty and Apollo proceeds.
    """
    payload = {
        "concept_id": concept.concept_id,
        "subject_id": concept.subject_id,
        "utterance": utterance,
    }
    try:
        raw = cheap_chat(
            purpose="parser_triviality",
            messages=[
                {"role": "system", "content": _TRIVIALITY_CLASSIFIER_PROMPT},
                {"role": "user", "content": json.dumps(payload)},
            ],
            response_format={"type": "json_object"},
            temperature=0.0,
        )
        parsed = json.loads(raw)
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("triviality classifier soft-fail: %s", exc)
        return False, 0.0

    is_teaching = bool(parsed.get("is_teaching", False))
    try:
        confidence = float(parsed.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    return is_teaching, max(0.0, min(1.0, confidence))


def _is_non_trivial(utterance: str, concept: ConceptDefinition) -> bool:
    """Decide whether a parser-empty result should raise.

    Order:
    1. Length floor < 10 chars => trivial.
    2. Exact match in _TRIVIAL_ACKS => trivial.
    3. Math characters present => non-trivial (no LLM needed).
    4. Otherwise: LLM classifier decides; non-trivial only if confidence
       >= _TEACHING_CONFIDENCE_THRESHOLD.
    """
    s = utterance.strip().lower()
    if len(s) < 10:
        return False
    if s in _TRIVIAL_ACKS:
        return False
    if _EQUATION_LIKE.search(utterance):
        return True
    is_teaching, confidence = _classify_teaching(utterance, concept)
    return is_teaching and confidence >= _TEACHING_CONFIDENCE_THRESHOLD


# Per-type content field names, derived from the Pydantic content models so
# the flat->nested adapter stays in lock-step with the ontology (one source of
# truth — no hand-maintained second list). Each strict-schema entry carries
# these fields FLAT (present-and-nullable); `_flat_content` lifts the ones that
# belong to the entry's type into the nested `content` dict `build_node` wants.
_CONTENT_FIELDS: dict[NodeType, tuple[str, ...]] = {
    t: tuple(model.model_fields.keys())
    for t, model in NODE_CONTENT_TYPES.items()
}


def _flat_content(entry: dict, node_type: NodeType) -> dict:
    """Lift the FLAT type-specific fields for `node_type` into a content dict.

    The strict schema is flat (every node field sits directly on the entry,
    present-and-nullable), but the ontology's `build_node` expects a nested
    per-type `content` payload. This adapter selects only the fields the type's
    content model declares and drops null values so the content model's own
    defaults/required-field validation applies unchanged.
    """
    return {
        key: entry[key]
        for key in _CONTENT_FIELDS[node_type]
        if entry.get(key) is not None
    }


def _entry_to_node(
    entry: dict,
    *,
    attempt_id: int,
    fallback_node_id: str,
) -> Node | None:
    """Convert a single FLAT LLM entry to a typed Node, or None if invalid."""
    t = entry.get("type")
    if t not in NODE_CONTENT_TYPES:
        return None
    content = _flat_content(entry, t)

    # LLM self-reported confidence for this entry. Falls back to 1.0 if the
    # field is absent (legacy prompt) or malformed — keeps behavior identical
    # to pre-P1 for nodes the LLM didn't tag.
    raw_conf = entry.get("confidence", 1.0)
    try:
        confidence = float(raw_conf)
    except (TypeError, ValueError):
        confidence = 1.0
    confidence = max(0.0, min(1.0, confidence))

    try:
        return build_node(
            node_type=t,  # type: ignore[arg-type]
            node_id=fallback_node_id,
            attempt_id=attempt_id,
            source="parser",
            content=content,
            parser_confidence=confidence,
        )
    except Exception:  # noqa: BLE001 - skip malformed entries
        return None


def _resolve_uses_edges(
    raw_entries: list[dict],
    index_to_node: dict[int, Node],
    *,
    attempt_id: int,
) -> list[Edge]:
    """Resolve `uses_equation_ordinals` ints into USES edges by node_id.

    Both the procedure-step source and its equation target are resolved
    through `index_to_node` (ORIGINAL entry index -> built node), exactly like
    the typed-edge path (`edge_resolver`). This keeps the fallback robust to
    malformed entries dropped by `_build_nodes`: a skipped entry never shifts
    the ordinal mapping (WU-2B nit-1; pre-WU-2B indexed the COMPACTED nodes
    list, which mis-targeted on a skip).

    Drops edges with unresolvable/non-equation ordinals.
    """
    edges: list[Edge] = []
    for i, raw in enumerate(raw_entries):
        node = index_to_node.get(i)
        if node is None or node.node_type != "procedure_step":
            continue
        ordinals = raw.get("uses_equation_ordinals") or []
        if not isinstance(ordinals, list):
            continue
        for o in ordinals:
            if not isinstance(o, int):
                continue
            target = index_to_node.get(o)
            if target is None or target.node_type != "equation":
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


def _as_list(value: object) -> list:
    """Return `value` if it is a list, else `[]` (defensive payload coercion)."""
    return value if isinstance(value, list) else []


def _render_graph_context(graph_context: GraphContext | None) -> str:
    """Render the EXISTING GRAPH block for the user message.

    None or empty context yields a single "(empty)" line so the call is
    byte-identical to the no-context path. A populated context yields one
    line per node: `<id> [<type>] <label>` (the spike's `_entry_summary`
    shape), so the model can reference prior-turn ids in cross-turn edges.
    """
    if graph_context is None or graph_context.is_empty():
        return "EXISTING GRAPH: (empty)"
    lines = [
        f"{n.node_id} [{n.node_type}] {n.label[:60]}"
        for n in graph_context.nodes
    ]
    return "EXISTING GRAPH:\n" + "\n".join(lines)


def _call_extraction(
    utterance: str,
    *,
    concept: ConceptDefinition,
    graph_context: GraphContext | None,
    model: str,
) -> str:
    """Make the one strict-`json_schema` GPT-4o call; return the raw content."""
    context_block = _render_graph_context(graph_context)
    client = OpenAI()
    resp = client.chat.completions.create(
        model=model,
        response_format={"type": "json_schema", "json_schema": build_extraction_schema()},
        messages=[
            {"role": "system", "content": build_system_prompt(concept)},
            {"role": "user", "content": f"{context_block}\n\nCURRENT MESSAGE:\n{utterance}"},
        ],
        temperature=0.0,
    )
    return resp.choices[0].message.content or "{}"


def _build_nodes(
    raw_entries: list, *, attempt_id: int,
) -> tuple[list[Node], list[dict], dict[int, Node]]:
    """Build typed nodes from LLM entries.

    Returns (nodes, kept_raw, index_to_node) where `index_to_node` maps the
    ORIGINAL entry index -> built node so "n<i>" edge refs survive skipped
    (malformed) entries — the LLM numbers refs against its own entry list.
    """
    nodes: list[Node] = []
    kept_raw: list[dict] = []
    index_to_node: dict[int, Node] = {}
    for i, e in enumerate(raw_entries):
        if not isinstance(e, dict) or "type" not in e:
            continue
        node = _entry_to_node(
            e, attempt_id=attempt_id, fallback_node_id=f"stu_{uuid.uuid4().hex[:12]}",
        )
        if node is None:
            continue
        nodes.append(node)
        kept_raw.append(e)
        index_to_node[i] = node
    return nodes, kept_raw, index_to_node


def parse_utterance(
    utterance: str,
    *,
    concept: ConceptDefinition,
    attempt_id: int,
    graph_context: GraphContext | None = None,
    model: str | None = None,
) -> tuple[list[Node], list[Edge]]:
    """Return (nodes, edges) for a student utterance.

    One strict-`json_schema` GPT-4o call emits typed nodes AND all four typed
    edges with explicit/inferred provenance. Optional `graph_context` links
    edges across turns; when omitted (default) the call behaves like today's
    within-turn-only parser, and if the model emits no edges the deterministic
    USES/PRECEDES fallback runs. Raises ParserCouldNotExtractError when a
    non-trivial utterance yields zero extractions.
    """
    model = model or models.MAIN_MODEL
    raw = _call_extraction(
        utterance, concept=concept, graph_context=graph_context, model=model,
    )
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        if _is_non_trivial(utterance, concept):
            raise ParserCouldNotExtractError(utterance=utterance)
        return [], []

    raw_entries = _as_list(payload.get("entries"))
    nodes, _kept_raw, index_to_node = _build_nodes(
        raw_entries, attempt_id=attempt_id,
    )
    if not nodes and _is_non_trivial(utterance, concept):
        raise ParserCouldNotExtractError(utterance=utterance)

    edges = resolve_typed_edges(
        _as_list(payload.get("edges")), index_to_node=index_to_node,
        graph_context=graph_context, attempt_id=attempt_id,
    )
    # No-context fallback: today's deterministic within-turn edges, only when
    # no context was supplied AND the model emitted no usable edges. The USES
    # fallback resolves ordinals through `index_to_node` (ORIGINAL entry index)
    # so it must receive the ORIGINAL entries list, not the compacted one.
    if not edges and graph_context is None:
        edges.extend(_resolve_uses_edges(
            raw_entries, index_to_node, attempt_id=attempt_id,
        ))
        edges.extend(_build_precedes_chain(nodes, attempt_id=attempt_id))

    return nodes, edges
