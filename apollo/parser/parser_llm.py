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
import os
import re
import uuid
from typing import Any

from openai import OpenAI

from apollo.agent._llm import cheap_chat
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
