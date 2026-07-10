"""Phase-1 diagnostic trace for the Apollo misconception detector.

Design spec: ``docs/_archive/specs/2026-07-09-apollo-misconception-trace-and-
tau-calibration-design.md`` (Phase 1). Recall-gap handoff:
``docs/_archive/handoffs/2026-07-08-apollo-misconception-recall-gap-handoff.md``.

**Instrumentation only — this module changes NO scoring, tau, gate row, or
threshold.** It is imported and invoked ONLY when ``config.trace_enabled()``
(``APOLLO_MISC_TRACE``, default OFF) is true; with the flag OFF nothing here
runs and the detector's behaviour/output is byte-identical to today.

Why a separate replay module (not inline hooks in gate/merge)? The gate
collapses each per-concept anchor group into ONE representative finding, so an
inline hook cannot cheaply attribute a decision back to every reference-graph
node WITHOUT changing the hot path's control flow. Instead this module takes
the SAME artifacts the live chain already produced — the raw
``DetectionResult`` (``detect_misconceptions``), the ``gate_findings`` output,
the ``MergeOutcome`` (``merge_detections``), the centrality map, and the
reference graph — and RE-DERIVES, per node, read-only, what the judge said and
which §5 truth-table row fired. It never mutates its inputs and never calls the
gate/merge internals, so it cannot perturb the grade it is observing.

Output: one JSON object per reference-graph concept node, appended as a line to
``config.trace_path()`` (``APOLLO_MISC_TRACE_PATH``, default
``campaign/out/misconception_trace.jsonl``). Machine-parseable (``json.loads``
each line). Attempt-agnostic: controls are traced identically to misconception
attempts. Any emit failure is swallowed (soft-fail — a trace defect must never
break a grade).
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from apollo.ontology import KGGraph
from apollo.overseer.misconception_detector.config import trace_path
from apollo.overseer.misconception_detector.types import (
    ConceptFinding,
    DetectionResult,
    MergeOutcome,
)

_LOG = logging.getLogger(__name__)

# Named-band label for the "Strong" scorecard band, matched case-insensitively
# so a false-Strong roll-up flag is robust to the caller's band casing.
_STRONG_BAND = "strong"


def _gate_row_label(
    *,
    has_sympy: bool,
    best_judge: ConceptFinding | None,
    corroborating_bank: ConceptFinding | None,
    gated_verdict: str | None,
) -> str:
    """Human-readable §5 truth-table row label for THIS node, re-derived
    read-only from the same signals ``gate._gate_one_concept`` uses.

    This is a diagnostic annotation, not a re-implementation of the gate: the
    actual dock/clarify/drop outcome is read from ``gated_verdict`` (the real
    gate output joined by ``concept_key``), and this label only NAMES which
    branch produced it so a reader can see co-key (row 3) vs lone-solo (row 5)
    vs else at a glance.
    """
    if has_sympy:
        return "row1_2_sympy"
    if best_judge is None:
        return "no_judge"
    if corroborating_bank is not None:
        return "row3_cokey_dock" if gated_verdict == "misconception" else "row3b_cokey_clarify"
    if best_judge.bank_code is not None:
        if gated_verdict == "misconception":
            return "row5_lone_solo_dock"
        if gated_verdict == "needs_clarification":
            return "row6_keyed_sub_solo_clarify"
        return "row8_keyed_sub_routed_drop"
    if gated_verdict == "needs_clarification":
        return "row7_unkeyed_clarify"
    return "row8_unkeyed_drop"


def _gate_decision(gated_verdict: str | None) -> str:
    """Collapse the joined gate verdict into the trace's decision enum:
    ``dock`` / ``needs_clarification`` / ``drop``."""
    if gated_verdict == "misconception":
        return "dock"
    if gated_verdict == "needs_clarification":
        return "needs_clarification"
    return "drop"


def _best_bank_top1(bank_findings: tuple[ConceptFinding, ...]) -> ConceptFinding | None:
    """The single highest-confidence ``bank_pattern`` finding across the whole
    detection result (the below-floor best match the recall-gap handoff §3
    calls out — e.g. ``nominal_for_real``@0.582). ``None`` when no bank tier
    fired at all."""
    if not bank_findings:
        return None
    return max(bank_findings, key=lambda f: f.confidence)


def build_node_traces(
    *,
    attempt_id: int,
    reference_graph: KGGraph,
    detection: DetectionResult,
    gated: tuple[ConceptFinding, ...],
    outcome: MergeOutcome,
    centrality: dict[str, float],
    final_band: str | None,
    is_false_strong: bool,
) -> tuple[dict[str, Any], ...]:
    """Pure. Build one trace row per reference-graph concept node.

    Joins the raw per-tier findings (``detection.per_concept``) to the real
    gate output (``gated``) by ``concept_key`` (== ``node_id``, the anchor key
    both the gate and ``centrality`` use), so every row reports exactly what
    the judge returned for that node AND how the gate decided — without
    re-running or perturbing either. Rows are emitted for EVERY node (controls
    included), even nodes the judge/gate produced nothing for (decision
    ``drop``), so a reader sees the full node inventory of an attempt.

    Never mutates any input (``ConceptFinding`` is frozen; the returned dicts
    are fresh)."""
    per_concept = detection.per_concept
    judges_by_key: dict[str, list[ConceptFinding]] = {}
    bank_findings: list[ConceptFinding] = []
    sympy_keys: set[str] = set()
    for finding in per_concept:
        if finding.source == "judge":
            judges_by_key.setdefault(finding.concept_key, []).append(finding)
        elif finding.source == "bank_pattern":
            bank_findings.append(finding)
        elif finding.source == "sympy_veto":
            sympy_keys.add(finding.concept_key)

    bank_by_code: dict[str, list[ConceptFinding]] = {}
    for finding in bank_findings:
        if finding.bank_code is not None:
            bank_by_code.setdefault(finding.bank_code, []).append(finding)

    gated_by_key = {f.concept_key: f for f in gated}
    bank_top1 = _best_bank_top1(tuple(bank_findings))

    rows: list[dict[str, Any]] = []
    for node in reference_graph.nodes:
        key = node.node_id
        node_judges = judges_by_key.get(key, [])
        best_judge = max(node_judges, key=lambda f: f.confidence) if node_judges else None
        has_sympy = key in sympy_keys

        corroborating_bank: ConceptFinding | None = None
        if best_judge is not None and best_judge.bank_code is not None:
            candidates = bank_by_code.get(best_judge.bank_code)
            if candidates:
                corroborating_bank = max(candidates, key=lambda f: f.confidence)

        gated_rep = gated_by_key.get(key)
        gated_verdict = gated_rep.verdict if gated_rep is not None else None

        row_label = _gate_row_label(
            has_sympy=has_sympy,
            best_judge=best_judge,
            corroborating_bank=corroborating_bank,
            gated_verdict=gated_verdict,
        )
        rows.append(
            {
                "attempt_id": attempt_id,
                "node_id": key,
                "node_type": node.node_type,
                "judge": _judge_payload(best_judge),
                "finding_signature": (best_judge.signature if best_judge is not None else None),
                "bank_code": best_judge.bank_code if best_judge is not None else None,
                "bank_pattern_top1": _bank_top1_payload(bank_top1),
                "cokey_bank_code": (
                    corroborating_bank.bank_code if corroborating_bank is not None else None
                ),
                "centrality": centrality.get(key),
                "gate_decision": _gate_decision(gated_verdict),
                "gate_row": row_label,
                "ceiling_eligible": (gated_rep.ceiling_eligible if gated_rep is not None else None),
                "final_band": final_band,
                "misconception_penalty": outcome.misconception_penalty,
                "ceiling_applied": outcome.ceiling_applied,
                "is_false_strong": is_false_strong,
            }
        )
    return tuple(rows)


def _judge_payload(finding: ConceptFinding | None) -> dict[str, Any] | None:
    """The judge sub-object: verdict, named code, confidence, and whether the
    confidence came from a real verdict-token probability (vs the verbalized
    fallback) — the T1/T3 diagnostic bit from the recall-gap handoff §5.
    ``None`` when the judge tier produced nothing for this node."""
    if finding is None:
        return None
    return {
        "verdict": finding.verdict,
        "misconception_code": finding.bank_code,
        "confidence": finding.confidence,
        "verdict_token_prob_present": finding.verdict_token_prob_present,
    }


def _bank_top1_payload(finding: ConceptFinding | None) -> dict[str, Any] | None:
    """The below-floor best bank match (handoff §3): its code + similarity +
    whether it cleared ``BANK_SIM_FLOOR``. ``None`` when no bank tier fired."""
    if finding is None:
        return None
    return {
        "bank_code": finding.bank_code,
        "similarity": finding.confidence,
        "above_floor": finding.bank_match_above_floor,
    }


def is_false_strong(*, is_control: bool, final_band: str | None) -> bool:
    """A per-attempt roll-up flag: a MISCONCEPTION-class attempt (not a
    control) that still lands in the Strong band is a residual false-Strong —
    the exact miss the recall gap targets (handoff §2). Controls are never
    false-Strong by definition."""
    if is_control:
        return False
    return (final_band or "").strip().lower() == _STRONG_BAND


def emit_traces(rows: tuple[dict[str, Any], ...], *, path: str | None = None) -> None:
    """Append each row as one JSON line to ``path`` (default
    ``config.trace_path()``). Creates parent dirs as needed. Soft-fail: any IO
    / serialization error is logged and swallowed — a trace defect must never
    break a grade (the detector's own soft-fail contract, extended here)."""
    if not rows:
        return
    target = path if path is not None else trace_path()
    try:
        parent = os.path.dirname(target)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(target, "a", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row, sort_keys=True, default=str))
                handle.write("\n")
    except Exception:  # noqa: BLE001
        _LOG.exception("misconception_trace_emit_failed path=%s", target)


def trace_attempt(
    *,
    attempt_id: int,
    reference_graph: KGGraph,
    detection: DetectionResult,
    gated: tuple[ConceptFinding, ...],
    outcome: MergeOutcome,
    centrality: dict[str, float],
    final_band: str | None,
    is_control: bool,
    path: str | None = None,
) -> tuple[dict[str, Any], ...]:
    """Convenience seam: compute the per-attempt false-Strong roll-up, build
    every node row, emit them, and return the rows (so a harness can also
    aggregate them in-process). Pure except for the ``emit_traces`` append.

    Callers gate this behind ``config.trace_enabled()`` so it is never on the
    hot path when the flag is OFF."""
    false_strong = is_false_strong(is_control=is_control, final_band=final_band)
    rows = build_node_traces(
        attempt_id=attempt_id,
        reference_graph=reference_graph,
        detection=detection,
        gated=gated,
        outcome=outcome,
        centrality=centrality,
        final_band=final_band,
        is_false_strong=false_strong,
    )
    emit_traces(rows, path=path)
    return rows


__all__ = [
    "build_node_traces",
    "emit_traces",
    "is_false_strong",
    "trace_attempt",
]
