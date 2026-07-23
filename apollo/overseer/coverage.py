"""Coverage: compare a frozen student KGGraph against a reference KGGraph.

V3 contract: both inputs are KGGraph (apollo.ontology.graph). The concurrent
matcher is DEPENDS_ON-direction-invariant: it compares reference/student nodes
by content and consults only outgoing USES neighbors for procedure evidence.
Procedure scheduling uses PRECEDES; no DEPENDS_ON traversal or order is
consumed.

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

import asyncio
import json
import logging
import time
from collections.abc import Callable
from typing import Any

from openai import OpenAI
from sympy import simplify
from sympy.parsing.sympy_parser import (
    convert_xor,
    parse_expr,
    standard_transformations,
)

from apollo.errors import CoverageGradingError
from apollo.ontology import EdgeType, KGGraph, Node
from apollo.resolution.tiers import (
    _extended_locals,
    _symbolic_equiv,
    _zero_form,
    student_surface_text,
)
from config.models import MAIN_MODEL

# Same transformation set the single mint/resolution parser uses so ``^`` and
# chained equalities are handled identically here.
_SIGN_TRANSFORMATIONS = standard_transformations + (convert_xor,)

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
                stage,
                i + 1,
                _RETRY_ATTEMPTS,
                exc,
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
  relationship. Algebraic rearrangements (same sign, reshuffled terms) are
  equivalent, but a SIGN FLIP changes the relationship and is NOT
  equivalent — e.g. "NX = X - M" and "NX = M - X" are different, not
  interchangeable rearrangements, even though they look similar.
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
        return {n.node_id: {"covered": False, "confidence": 1.0} for n in reference_nodes}

    used_model = model or MAIN_MODEL

    payload = {
        "entry_type": entry_type,
        "reference_entries": [
            {"ref_id": n.node_id, "content": n.content.model_dump()} for n in reference_nodes
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
                n.node_id,
                {"covered": False, "confidence": 0.0},
            )
        # T-W5a (P3) — grader positive-focus. Default OFF: the sign pre-gate
        # runs exactly as before (byte-identical). When ON, skip it — the
        # coverage grader credits the produced equation and stops
        # credit-denying on wrongness; the detector's sympy_veto tier still
        # names the sign error in the feedback channel independently.
        return out

    return _with_retry(_call, stage=f"binary_match:{entry_type}")


def _sign_reversed_zero_form(symbolic: str, local_dict: dict):
    """Zero-form of the SIGN-REVERSED equation for ``symbolic``.

    The relationship-level sign flip is NOT ``-1 *`` the whole zero-form once an
    equation carries an '=' — only the RHS flips. For ``LHS = RHS`` the reversed
    relationship is ``LHS = -RHS``, whose zero-form is ``LHS - (-RHS) = LHS +
    RHS``. For a bare expression ``E`` (implicitly ``E = 0``) the reversed form
    is ``-E = 0``, zero-form ``-E`` — the same result the (now-removed) ``-1*``
    wrap produced for the no-'=' fixtures, so the bare-expression fixtures are
    unaffected. Chained equalities normalize to the FIRST equality, matching
    ``parse_zero_form``. Returns ``None`` on any parse failure (a non-parse is a
    non-match, never a crash)."""
    s = symbolic.strip()
    if "=" in s:
        # Chained (A = B = C = ...): keep the first equality, mirroring
        # apollo.solver.sympy_exec.parse_zero_form.
        parts = s.split("=")
        lhs, rhs = parts[0], parts[1]
        try:
            l_expr = parse_expr(
                lhs.strip(),
                local_dict=local_dict,
                transformations=_SIGN_TRANSFORMATIONS,
            )
            r_expr = parse_expr(
                rhs.strip(),
                local_dict=local_dict,
                transformations=_SIGN_TRANSFORMATIONS,
            )
        except Exception:  # noqa: BLE001 - a non-parse is a non-match, never a crash
            return None
        return simplify(l_expr + r_expr)
    zf = _zero_form(s, local_dict)
    return None if zf is None else simplify(-zf)


def _sign_gate_equation_verdicts(
    *,
    verdicts: dict[str, dict[str, Any]],
    reference_nodes: list[Node],
    student_nodes: list[Node],
) -> dict[str, dict[str, Any]]:
    """D4 fix (T10): SymPy sign pre-gate over LLM equation verdicts.

    Flag-gated (``detector_enabled()``) and equation-only — the caller
    already checked both before invoking this. For every reference the
    LLM marked ``covered=True``, re-check sign-exactness with
    ``apollo.resolution.tiers._symbolic_equiv``: if NO student equation is
    sign-exact equivalent to the reference, but at least one student
    equation IS sign-exact equivalent to the reference's negation, the
    LLM's "covered" is a false positive on a sign-reversed mutant — force
    it to ``covered=False``. This is a DOWNGRADE-ONLY gate: it never
    upgrades an LLM ``covered=False`` verdict (no over-correction), and a
    genuine sign-preserving rearrangement stays sign-exact equivalent to
    the reference itself so it is left untouched.

    Returns a NEW dict (immutable-value-object convention) — the input
    ``verdicts`` mapping is never mutated in place.
    """
    ref_by_id = {n.node_id: n for n in reference_nodes}
    student_texts = [text for text in (student_surface_text(n) for n in student_nodes) if text]
    if not student_texts:
        return verdicts

    gated: dict[str, dict[str, Any]] = dict(verdicts)
    for ref_id, verdict in verdicts.items():
        if not verdict.get("covered"):
            continue
        ref_node = ref_by_id.get(ref_id)
        if ref_node is None:
            continue
        ref_symbolic = student_surface_text(ref_node)
        if not ref_symbolic:
            continue

        sign_exact_match = any(
            _symbolic_equiv(student_text, ref_symbolic, mappings={})
            for student_text in student_texts
        )
        if sign_exact_match:
            continue  # genuine (sign-preserving) match — leave as covered.

        # Detect a sign-reversed relationship on PARSED zero-forms rather than
        # round-tripping a synthetic "-1*(...)" string through the '='-splitting
        # parser (which mangled real '='-bearing references such as
        # ``NX = X - M`` into garbage). Compare each student's zero-form against
        # the reference's SIGN-REVERSED zero-form (RHS flipped, not the whole
        # zero-form negated — those differ once an '=' is present).
        sign_reversed_match = False
        for student_text in student_texts:
            local_dict = _extended_locals(student_text, ref_symbolic)
            student_zf = _zero_form(student_text, local_dict)
            reversed_ref_zf = _sign_reversed_zero_form(ref_symbolic, local_dict)
            if student_zf is None or reversed_ref_zf is None:
                continue
            try:
                if bool(simplify(student_zf - reversed_ref_zf) == 0):
                    sign_reversed_match = True
                    break
            except Exception:  # noqa: BLE001 - comparison failure is a non-match  # pragma: no cover - defensive
                continue
        if sign_reversed_match:
            gated[ref_id] = {**verdict, "covered": False}

    return gated


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
    used_model = model or MAIN_MODEL

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
                "uses_equations": [n.node_id for n in student_uses_per_node.get(s.node_id, [])],
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


async def compute_coverage(
    student_graph: KGGraph,
    reference_graph: KGGraph,
) -> dict[str, Any]:
    """Walk the reference graph; produce per-node coverage + procedure scores.

    v1: all matcher calls (one per procedure step + one batch per binary
    type) run CONCURRENTLY via asyncio.to_thread. The per-node verdict map
    is order-independent, so dropping the sequential topological walk does
    not change results — only latency. NO-FALLBACK preserved: a
    CoverageGradingError raised inside any task propagates out of gather.
    """
    per_step: dict[str, str] = {}
    procedure_scores: dict[str, float] = {}
    confidences: dict[str, float] = {}

    student_proc = student_graph.by_type("procedure_step")
    student_uses_per_node: dict[str, list[Node]] = {
        s.node_id: student_graph.neighbors(s.node_id, EdgeType.USES) for s in student_proc
    }

    try:
        proc_order = reference_graph.topological_order(
            EdgeType.PRECEDES,
            node_type="procedure_step",
        )
    except ValueError:
        proc_order = reference_graph.by_type("procedure_step")

    async def _proc_task(ref_node: Node) -> tuple[Node, float, float]:
        ref_uses = reference_graph.neighbors(ref_node.node_id, EdgeType.USES)
        score, confidence = await asyncio.to_thread(
            _procedure_match_score,
            ref_node,
            student_proc,
            ref_uses=ref_uses,
            student_uses_per_node=student_uses_per_node,
        )
        return ref_node, score, confidence

    async def _binary_task(entry_type: str, ref_nodes: list[Node]):
        student_pool = student_graph.by_type(entry_type)
        verdicts = await asyncio.to_thread(
            _batch_binary_match,
            entry_type=entry_type,
            reference_nodes=ref_nodes,
            student_nodes=student_pool,
        )
        return entry_type, ref_nodes, verdicts

    tasks: list = [_proc_task(n) for n in proc_order]
    binary_groups: dict[str, list[Node]] = {}
    for entry_type in _BINARY_TYPES:
        rn = [n for n in reference_graph.nodes if n.node_type == entry_type]
        if rn:
            binary_groups[entry_type] = rn
            tasks.append(_binary_task(entry_type, rn))

    # NO return_exceptions: a CoverageGradingError must propagate (no-fallback).
    results = await asyncio.gather(*tasks)

    for res in results:
        # _binary_task returns (str, list, dict); _proc_task returns
        # (Node, float, float). Node is a discriminated-union alias so it
        # can't be used in isinstance — discriminate on the str entry_type.
        if isinstance(res[0], str):
            entry_type, ref_nodes, verdicts = res
            for ref in ref_nodes:
                v = verdicts.get(ref.node_id, {"covered": False, "confidence": 0.0})
                covered = bool(v["covered"])
                conf = float(v["confidence"])
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
        else:
            ref_node, score, confidence = res
            procedure_scores[ref_node.node_id] = score
            confidences[ref_node.node_id] = confidence
            per_step[ref_node.node_id] = "covered" if score >= 0.5 else "missing"

    # P3.4: surface the negotiation state of the student graph so the
    # diagnostic narration (P3.11) and tests can attest "you negotiated
    # N entries" without re-walking the graph downstream.
    dual_count = sum(1 for n in student_graph.nodes if n.status == "DUAL")
    disputed_count = sum(1 for n in student_graph.nodes if n.status == "DISPUTED")
    paraphrased_count = sum(
        1 for n in student_graph.nodes if n.status == "DUAL" and n.student_belief
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
