"""Coverage: compare a frozen student KGGraph against a reference KGGraph.

V3 contract: both inputs are KGGraph (apollo.ontology.graph). The matcher
walks the reference graph in topological order along PRECEDES (procedure)
and DEPENDS_ON (everything else), so the order of LLM calls follows the
authored teaching plan.

For procedure_step entries, the matcher gets BOTH the action text AND the
USES edges (real equation node_ids on both sides) so it can reward exact
overlap. This makes USES grading-meaningful for the first time.

Soft-fails: equation/condition/simplification matches default to "missing"
on LLM exception; procedure scores default to 0.0. (Retry/cache/batch
beyond this is checklist item 10, deferred.)

Return shape:
  {
    "per_step":          {ref_node.node_id: "covered" | "missing"},
    "procedure_scores":  {ref_node.node_id: float in [0, 1]},
  }
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any

from openai import OpenAI

from apollo.ontology import EdgeType, KGGraph, Node

_LOG = logging.getLogger(__name__)


_BINARY_MATCHER_PROMPT = """You are grading whether any student-taught knowledge-graph
entry semantically covers a single reference entry. Students phrase things in their own
words, so judge by meaning, not wording.

Return ONLY a JSON object of the form: {"covered": true} or {"covered": false}.

Guidance by entry_type:
- equation: two equations are equivalent if they express the same physical relationship.
  A student equation that omits a common non-zero factor the reference keeps still covers
  the reference. Sign flips and algebraic rearrangements of the same equation are equivalent.
- condition: any student condition asserting the same physical assumption covers the reference.
- simplification: any student simplification performing the same geometric or physical
  reduction covers the reference.
- definition: any student definition of the same concept covers the reference.
- variable_mapping: any student mapping of the same term to the same symbol covers the reference.

If no student entry expresses the reference's meaning, return {"covered": false}.
"""


def _binary_match(
    ref_node: Node,
    student_pool: list[Node],
    *,
    model: str | None = None,
) -> bool:
    """LLM-based semantic coverage check. Soft-fails to False on exception."""
    if not student_pool:
        return False
    model = model or os.getenv("MAIN_MODEL", "gpt-4o")
    try:
        client = OpenAI()
        payload = {
            "entry_type": ref_node.node_type,
            "reference_entry": ref_node.content.model_dump(),
            "student_entries": [n.content.model_dump() for n in student_pool],
        }
        resp = client.chat.completions.create(
            model=model,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": _BINARY_MATCHER_PROMPT},
                {"role": "user", "content": json.dumps(payload)},
            ],
            temperature=0.0,
        )
        raw = resp.choices[0].message.content or "{}"
        parsed = json.loads(raw)
        return bool(parsed.get("covered", False))
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("binary matcher soft-fail for %s: %s", ref_node.node_type, exc)
        return False


_PROCEDURE_MATCHER_PROMPT = """You are grading whether a student's procedure step
covers a reference procedure step. Score how well the student's action matches
the reference's action, with partial credit.

Return ONLY a JSON object of the form: {"score": <float in [0.0, 1.0]>}

Scoring guide:
- 1.0: student describes the same action with the same ordering/intent AND the
  set of equations the student linked via USES matches or contains the reference's
  USES set.
- 0.7-0.9: same action; USES partially overlaps OR is missing.
- 0.4-0.6: partial overlap on action; USES does not help.
- 0.1-0.3: tangentially related.
- 0.0: no match — student did not describe this step.

Consider the student's action semantically, not word-for-word. Ordering matters
when the reference step has a position in a chain; if so, the student step at
the same chain position is the best candidate but not required.

The `uses_equations` field on each side lists the equation IDENTIFIERS that step
links to. Equation identifiers across the two sides are NOT directly comparable
(reference uses authored ids; student uses extraction ids), but the size + the
provided `equation_summaries` lookup let you tell whether the same set of
underlying equations is referenced.
"""


def _procedure_match_score(
    ref_node: Node,
    student_pool: list[Node],
    *,
    ref_uses: list[Node],
    student_uses_per_node: dict[str, list[Node]],
    model: str | None = None,
) -> float:
    """LLM-based partial-credit match score in [0, 1]. Soft-fails to 0.0."""
    if not student_pool:
        return 0.0
    model = model or os.getenv("MAIN_MODEL", "gpt-4o")
    try:
        client = OpenAI()
        # Build a small lookup so the LLM can compare USES sets across the
        # two id namespaces by underlying equation content, not by id.
        equation_summaries: dict[str, str] = {}
        for eq in ref_uses:
            equation_summaries[eq.node_id] = eq.content.model_dump().get("symbolic", "")
        for steps in student_uses_per_node.values():
            for eq in steps:
                equation_summaries[eq.node_id] = eq.content.model_dump().get("symbolic", "")

        payload = {
            "reference_step": {
                "action": ref_node.content.model_dump().get("action"),
                "purpose": ref_node.content.model_dump().get("purpose"),
                "uses_equations": [n.node_id for n in ref_uses],
            },
            "student_steps": [
                {
                    "action": s.content.model_dump().get("action"),
                    "purpose": s.content.model_dump().get("purpose"),
                    "uses_equations": [
                        n.node_id for n in student_uses_per_node.get(s.node_id, [])
                    ],
                }
                for s in student_pool
            ],
            "equation_summaries": equation_summaries,
        }
        resp = client.chat.completions.create(
            model=model,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": _PROCEDURE_MATCHER_PROMPT},
                {"role": "user", "content": json.dumps(payload)},
            ],
            temperature=0.0,
        )
        raw = resp.choices[0].message.content or "{}"
        parsed = json.loads(raw)
        score = float(parsed.get("score", 0.0))
        return max(0.0, min(1.0, score))
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("procedure matcher soft-fail: %s", exc)
        return 0.0


_BINARY_TYPES = ("equation", "condition", "simplification")


def compute_coverage(
    student_graph: KGGraph,
    reference_graph: KGGraph,
) -> dict[str, Any]:
    """Walk the reference graph; produce per-node coverage + procedure scores.

    Walk order:
    - For procedure_step refs: PRECEDES topological order (so earlier steps
      get matched first; downstream concerns can short-circuit if a
      prerequisite is uncovered, though we don't currently exploit that).
    - For binary types: just iterate (order doesn't affect correctness).
    """
    per_step: dict[str, str] = {}
    procedure_scores: dict[str, float] = {}

    student_proc = student_graph.by_type("procedure_step")
    student_uses_per_node: dict[str, list[Node]] = {
        s.node_id: student_graph.neighbors(s.node_id, EdgeType.USES)
        for s in student_proc
    }

    # Procedure steps in topological PRECEDES order
    try:
        proc_order = reference_graph.topological_order(
            EdgeType.PRECEDES, node_type="procedure_step",
        )
    except ValueError:
        # Cycle or disconnect — fall back to insertion order
        proc_order = reference_graph.by_type("procedure_step")

    for ref_node in proc_order:
        ref_uses = reference_graph.neighbors(ref_node.node_id, EdgeType.USES)
        score = _procedure_match_score(
            ref_node,
            student_proc,
            ref_uses=ref_uses,
            student_uses_per_node=student_uses_per_node,
        )
        procedure_scores[ref_node.node_id] = score
        per_step[ref_node.node_id] = "covered" if score >= 0.5 else "missing"

    # Binary types
    for ref_node in reference_graph.nodes:
        if ref_node.node_type not in _BINARY_TYPES:
            continue
        student_pool = student_graph.by_type(ref_node.node_type)
        covered = _binary_match(ref_node, student_pool)
        per_step[ref_node.node_id] = "covered" if covered else "missing"

    return {"per_step": per_step, "procedure_scores": procedure_scores}
