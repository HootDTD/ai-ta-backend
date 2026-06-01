"""Coverage: compare a frozen student KGGraph against a reference KGGraph.

V3 contract: both inputs are KGGraph (apollo.ontology.graph). The matcher
walks the reference graph in topological order along PRECEDES (procedure)
and DEPENDS_ON (everything else), so the order of LLM calls follows the
authored teaching plan.

For procedure_step entries, the matcher gets BOTH the action text AND the
USES edges (real equation node_ids on both sides) so it can reward exact
overlap.

Item #10 hardening:
- Retry-with-backoff on transient errors (3 attempts, exponential).
  Terminal failure raises `CoverageGradingError` instead of silently
  downgrading the grade.
- Binary-type matchers are BATCHED: one LLM call per type evaluates all
  reference nodes of that type at once. ~50% fewer calls per Done.
- Per-node confidence scores returned alongside coverage so the
  diagnostic narrative can hedge on mid-band matches.

Return shape:
  {
    "per_step":          {ref_node.node_id: "covered" | "missing"},
    "procedure_scores":  {ref_node.node_id: float in [0, 1]},
    "confidences":       {ref_node.node_id: float in [0, 1]},
  }
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Callable

from openai import OpenAI

from apollo.errors import CoverageGradingError
from apollo.ontology import EdgeType, KGGraph, Node

_LOG = logging.getLogger(__name__)

_RETRY_ATTEMPTS = 3
_RETRY_BACKOFF_S = (0.5, 1.0, 2.0)

# Confidence floor for "covered=true" to be accepted. Below this the match
# is downgraded to "missing" but logged with `covered_uncertain=true` so
# the diagnostic narrative can hedge.
_BINARY_CONFIDENCE_FLOOR: float = 0.5


def _with_retry(
    fn: Callable[[], Any],
    *,
    stage: str,
) -> Any:
    """Run `fn` up to _RETRY_ATTEMPTS times with exponential backoff.

    Raises CoverageGradingError if every attempt fails. Note: this DOES
    raise on terminal failure (no soft-fail) — that's the no-fallback
    contract item #10 introduces.
    """
    last_exc: Exception | None = None
    for i in range(_RETRY_ATTEMPTS):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            _LOG.warning(
                "coverage stage %r retry %d/%d failed: %s",
                stage, i + 1, _RETRY_ATTEMPTS, exc,
            )
            if i < _RETRY_ATTEMPTS - 1:
                time.sleep(_RETRY_BACKOFF_S[i])
    raise CoverageGradingError(
        stage=stage,
        last_error=str(last_exc) if last_exc else "unknown",
    )


_BATCH_BINARY_PROMPT = """You are grading whether the student's
knowledge-graph entries semantically cover each reference entry of a
single type. Students phrase things in their own words, so judge by
meaning, not wording.

You will receive:
- entry_type: one of equation, condition, simplification, definition,
  variable_mapping
- reference_entries: a list of dicts with `ref_id` and `content`
- student_entries: a list of dicts (full content) for entries of the
  same type the student taught

Return ONLY a JSON object of the form:
{"matches": [
   {"ref_id": "<id>", "covered": <bool>, "confidence": <float in [0, 1]>},
   ...
]}

There must be exactly one entry per reference entry, in the same order.

Guidance by entry_type:
- equation: two equations are equivalent if they express the same physical
  relationship. Sign flips and algebraic rearrangements are equivalent.
  A student equation that omits a common non-zero factor still covers.
- condition: any student condition asserting the same physical assumption
  covers the reference.
- simplification: any student simplification performing the same geometric
  or physical reduction covers the reference.
- definition: any student definition of the same concept covers.
- variable_mapping: any student mapping of the same term to the same
  symbol covers.

If a student entry includes a `student_belief` field, the student has
disputed the parser's surface form and supplied their own wording. Grade
against the student's wording — the structural fields are still useful
for the comparison, but the student's belief is what they actually
asserted. This is the Negotiable Open Learner Model contract.

If no student entry expresses a reference's meaning, set covered=false
with the appropriate confidence (1.0 if you're sure nothing matches,
lower if there might be a tangential match).
"""


def _batch_binary_match(
    *,
    entry_type: str,
    reference_nodes: list[Node],
    student_nodes: list[Node],
    model: str | None = None,
) -> dict[str, dict[str, Any]]:
    """One LLM call per type → per-ref verdict map.

    Return: {ref_node_id: {"covered": bool, "confidence": float}}
    """
    if not reference_nodes:
        return {}
    if not student_nodes:
        return {
            n.node_id: {"covered": False, "confidence": 1.0}
            for n in reference_nodes
        }

    import os
    used_model = model or os.getenv("MAIN_MODEL", "gpt-4o")

    payload = {
        "entry_type": entry_type,
        "reference_entries": [
            {"ref_id": n.node_id, "content": n.content.model_dump()}
            for n in reference_nodes
        ],
        # P3.4: DUAL entries with a student_belief carry that wording into
        # the LLM payload via _student_payload.
        "student_entries": [_student_payload(n) for n in student_nodes],
    }

    def _call() -> dict[str, dict[str, Any]]:
        client = OpenAI()
        resp = client.chat.completions.create(
            model=used_model,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": _BATCH_BINARY_PROMPT},
                {"role": "user", "content": json.dumps(payload)},
            ],
            temperature=0.0,
        )
        raw = resp.choices[0].message.content or "{}"
        parsed = json.loads(raw)
        matches = parsed.get("matches", [])
        if not isinstance(matches, list):
            raise ValueError(f"matches not a list: {type(matches)}")
        out: dict[str, dict[str, Any]] = {}
        for m in matches:
            rid = m.get("ref_id")
            if not isinstance(rid, str):
                continue
            try:
                conf = float(m.get("confidence", 0.0))
            except (TypeError, ValueError):
                conf = 0.0
            out[rid] = {
                "covered": bool(m.get("covered", False)),
                "confidence": max(0.0, min(1.0, conf)),
            }
        # Fill any missing ref_ids with "not covered, low confidence"
        # rather than dropping them — the orchestrator must have a verdict
        # for every reference entry.
        for n in reference_nodes:
            out.setdefault(
                n.node_id, {"covered": False, "confidence": 0.0},
            )
        return out

    return _with_retry(_call, stage=f"binary_match:{entry_type}")


_PROCEDURE_MATCHER_PROMPT = """You are grading whether a student's procedure step
covers a reference procedure step. Score how well the student's action matches
the reference's action, with partial credit, and return your confidence.

Return ONLY a JSON object of the form:
{"score": <float in [0, 1]>, "confidence": <float in [0, 1]>}

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
) -> tuple[float, float]:
    """LLM-based partial-credit match score in [0, 1] + confidence in [0, 1].

    Raises CoverageGradingError if all retries fail (no soft-fail).
    """
    if not student_pool:
        return 0.0, 1.0
    import os
    used_model = model or os.getenv("MAIN_MODEL", "gpt-4o")

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
                # P3.4: DUAL steps with a student_belief surface the
                # student's wording as `action` (and also as the explicit
                # `student_belief` field). _student_payload encapsulates
                # the substitution rule — see its docstring.
                "action": _student_payload(s).get("action"),
                "purpose": s.content.model_dump().get("purpose"),
                "uses_equations": [
                    n.node_id for n in student_uses_per_node.get(s.node_id, [])
                ],
                **(
                    {"student_belief": s.student_belief}
                    if s.status == "DUAL" and s.student_belief
                    else {}
                ),
            }
            for s in student_pool
        ],
        "equation_summaries": equation_summaries,
    }

    def _call() -> tuple[float, float]:
        client = OpenAI()
        resp = client.chat.completions.create(
            model=used_model,
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
        try:
            confidence = float(parsed.get("confidence", 0.7))
        except (TypeError, ValueError):
            confidence = 0.7
        return (
            max(0.0, min(1.0, score)),
            max(0.0, min(1.0, confidence)),
        )

    return _with_retry(_call, stage="procedure_match")


_BINARY_TYPES = ("equation", "condition", "simplification")


def _student_payload(node: Node) -> dict[str, Any]:
    """Serialize a student node for the coverage LLM. P3.4 (Negotiable
    OLM) hook: DUAL entries with a `student_belief` surface that wording
    to the LLM as an explicit field — the prompt directs the grader to
    weigh it. ACCEPTED nodes and DUAL-skip nodes (no belief) pass content
    through unchanged, so the pre-P3 grading path is byte-identical.

    For procedure_step we also substitute the `action` text with the
    student's belief because the procedure-step matcher reads
    `action` as the primary semantic field — adding `student_belief`
    alongside isn't enough to shift the match.
    """
    payload = node.content.model_dump()
    if node.status == "DUAL" and node.student_belief:
        payload["student_belief"] = node.student_belief
        if node.node_type == "procedure_step":
            # `action` is the matcher's primary field for procedure steps;
            # surface the student's wording in place of the parser's so
            # the substitution is load-bearing for the score.
            payload["action"] = node.student_belief
    return payload


def compute_coverage(
    student_graph: KGGraph,
    reference_graph: KGGraph,
) -> dict[str, Any]:
    """Walk the reference graph; produce per-node coverage + procedure scores.

    Walk order:
    - For procedure_step refs: PRECEDES topological order.
    - For binary types: ONE batched LLM call per type (vs N calls in V2).

    Return shape adds `confidences` so callers can hedge on mid-band matches.
    Raises CoverageGradingError when LLM retries are exhausted at any stage.
    """
    per_step: dict[str, str] = {}
    procedure_scores: dict[str, float] = {}
    confidences: dict[str, float] = {}

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
        score, confidence = _procedure_match_score(
            ref_node,
            student_proc,
            ref_uses=ref_uses,
            student_uses_per_node=student_uses_per_node,
        )
        procedure_scores[ref_node.node_id] = score
        confidences[ref_node.node_id] = confidence
        per_step[ref_node.node_id] = "covered" if score >= 0.5 else "missing"

    # Binary types — one batched call per type.
    for entry_type in _BINARY_TYPES:
        ref_nodes = [n for n in reference_graph.nodes if n.node_type == entry_type]
        if not ref_nodes:
            continue
        student_pool = student_graph.by_type(entry_type)
        verdicts = _batch_binary_match(
            entry_type=entry_type,
            reference_nodes=ref_nodes,
            student_nodes=student_pool,
        )
        for ref in ref_nodes:
            v = verdicts.get(ref.node_id, {"covered": False, "confidence": 0.0})
            covered = bool(v["covered"])
            conf = float(v["confidence"])
            # Below-floor "covered=true" gets downgraded — but we keep
            # the confidence for the diagnostic to surface uncertainty.
            if covered and conf < _BINARY_CONFIDENCE_FLOOR:
                _LOG.info(
                    "coverage_uncertain",
                    extra={
                        "event": "coverage_uncertain",
                        "ref_id": ref.node_id,
                        "entry_type": entry_type,
                        "confidence": conf,
                    },
                )
                covered = False
            per_step[ref.node_id] = "covered" if covered else "missing"
            confidences[ref.node_id] = conf

    # P3.4: surface the negotiation state of the student graph so the
    # diagnostic narration (P3.11) and tests can attest "you negotiated
    # N entries" without re-walking the graph downstream.
    dual_count = sum(1 for n in student_graph.nodes if n.status == "DUAL")
    disputed_count = sum(1 for n in student_graph.nodes if n.status == "DISPUTED")
    paraphrased_count = sum(
        1 for n in student_graph.nodes
        if n.status == "DUAL" and n.student_belief
    )
    skipped_count = dual_count - paraphrased_count

    return {
        "per_step": per_step,
        "procedure_scores": procedure_scores,
        "confidences": confidences,
        "negotiation_counts": {
            "dual": dual_count,
            "disputed": disputed_count,
            "paraphrased": paraphrased_count,
            "skipped": skipped_count,
        },
    }
