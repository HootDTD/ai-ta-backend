"""WU-4B1 §6.6 — the hard abstention gates.

:func:`apply_abstention` turns the per-attempt signals (``unresolved_rate``,
the MIN parser confidence over turns, misconception resolution confidences, the
upstream reference-validation failure, and the transcript-audit failure) into a
deterministic abstention outcome: a reason list, the ``abstained`` flag, and a
per-event-kind suppression set (the kinds WU-4B2 must withhold).

Binding semantics (§6.6):
- ``abstained=True`` is set by TWO gates: the ``unresolved_rate`` gate (resolution
  *success*) and the Phase 1c ``min_normalization_confidence`` gate (resolution
  *quality* — the weakest-link nc of the evidence that backed a scored finding).
  Either one means a *no-Layer-3-update* run. The ``missing`` / ``misconception``
  suppressions are *partial*: they withhold specific event kinds (and record a
  reason) but the run still updates Layer-3 for the rest, so they do NOT set
  ``abstained``.
- Reason ordering is deterministic (gate-declaration order) so two calls with
  the same inputs return an identical ``abstention_reasons`` tuple.

Pure: no IO, no mutation of inputs.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from apollo.ontology.nodes import Node
from apollo.resolution.result import ResolutionResult

# §6.6 gate thresholds. Named so WU-4B2/4B3 match against the symbol, not a
# magic literal.
ABSTENTION_THRESHOLDS: dict[str, float] = {
    "unresolved_rate": 0.35,  # > 0.35 -> no Layer-3 update (diagnostic-only run)
    "min_parser_confidence": 0.6,  # MIN over turns < 0.6 -> suppress 'missing'
    "misconception_confidence": 0.8,  # < 0.8 -> withhold 'misconception'
    "min_normalization_confidence": 0.85,  # nc < 0.85 -> abstained=True (Phase 1c; calibration-owned per spec §10)
}

# Reason strings persisted verbatim into apollo_graph_comparison_runs
# .abstention_reasons (WU-4B3). Named constants so WU-4B2/4B3 match symbols.
REASON_HIGH_UNRESOLVED = "unresolved_rate_above_threshold"
REASON_LOW_PARSER_CONFIDENCE = "min_parser_confidence_below_threshold"
REASON_LOW_MISCONCEPTION_CONFIDENCE = "misconception_confidence_below_threshold"
REASON_REFERENCE_INVALID = "reference_graph_invalid"
REASON_TRANSCRIPT_AUDIT_FAILED = "transcript_audit_unavailable"
# Lane B3a/D1 (2026-07-02): the ``misconception_bank_empty`` reason string was
# REMOVED here. Under the emergent-misconception design an empty
# ``apollo_misconceptions`` bank is the NORMAL cold-start state of every class,
# not a reason to abstain. Coverage grading and misconception detection are
# separable signals: an empty bank must NOT gate coverage grading and must NOT
# emit an abstention reason (the reason polluted the harness abstention
# histograms). The bank-empty fact now rides on ``GradeResult.soundness_applicable``
# (D5/D6: soundness_score/contradiction_score are None) and surfaces as an
# explicit machine-readable marker in the graph artifact's misconception section
# (``artifact_build.build_graph_artifact``) — never as an abstention reason.
# Phase 1c (spec §8): the attempt resolved only via weak tiers (low
# normalization_confidence) -> distrust even at low unresolved_rate.
REASON_LOW_NORMALIZATION_CONFIDENCE = "normalization_confidence_below_threshold"
# §10 composite gate (APOLLO_ABSTENTION_COMPOSITE, default OFF; decision memo
# docs/_archive/design/2026-07-06-abstention-signal-decision-memo.md option d):
# the resolver credited less than the required fraction of the problem's
# expected reference set (a CONTENT signal, replacing the unresolved_rate
# volume signal). Only ever appended when the composite gate is enabled; the
# flag-OFF path never emits this reason (byte-identical to pre-§10 behavior).
REASON_COMPOSITE_LOW_COVERAGE = "composite_low_coverage"

# §10 composite gate default coverage threshold (APOLLO_COMPOSITE_COVERAGE_MIN).
COMPOSITE_DEFAULT_COVERAGE_MIN: float = 0.6

# Event kinds (subset) WU-4B2 may be told to withhold.
_SUPPRESS_MISSING = "missing"
_SUPPRESS_MISCONCEPTION = "misconception"


@dataclass(frozen=True)
class Abstention:
    """The §6.6 abstention outcome (frozen, persisted by WU-4B3)."""

    abstention_reasons: tuple[str, ...]
    abstained: bool
    suppressed_event_kinds: frozenset[str]
    # §10 composite gate audit metadata (``None`` unless ``composite_enabled``):
    # {"coverage": float, "contradictions": int, "coverage_min": float,
    #  "decision": "grade" | "abstain"}. Nested verbatim under the artifact's
    # ``abstention.composite`` key by ``artifact_build.build_graph_artifact`` so
    # the composite decision is always auditable when the flag is on.
    composite: dict | None = None


def unresolved_rate_of(resolution: ResolutionResult) -> float:
    """``unresolved_count / total`` over ``resolution.resolved`` (count every
    node whose ``resolution != 'resolved'``). ``0.0`` for an empty attempt (an
    empty attempt never false-trips the gate)."""
    total = len(resolution.resolved)
    if total == 0:
        return 0.0
    unresolved = sum(1 for rn in resolution.resolved if rn.resolution != "resolved")
    return unresolved / total


def min_parser_confidence_of(nodes: Iterable[Node]) -> float:
    """``min(n.parser_confidence ...)``; ``1.0`` for an empty iterable so an
    empty attempt never false-trips the gate. MIN, NEVER mean (§6.6 binding)."""
    confidences = [n.parser_confidence for n in nodes]
    if not confidences:
        return 1.0
    return min(confidences)


def apply_abstention(
    *,
    unresolved_rate: float,
    min_parser_confidence: float,
    misconception_confidences: tuple[float, ...] = (),
    transcript_audit_failed: bool = False,
    reference_invalid: bool = False,
    normalization_confidence: float = 1.0,
    composite_enabled: bool = False,
    node_coverage: float = 0.0,
    contradiction_count: int = 0,
    coverage_min: float = COMPOSITE_DEFAULT_COVERAGE_MIN,
) -> Abstention:
    """Apply the §6.6 gates and return the reasons + flags + suppression set.

    - ``unresolved_rate > 0.35``      -> ``abstained=True``, REASON_HIGH_UNRESOLVED
                                         (no Layer-3 update; diagnostic-only run)
    - ``normalization_confidence < 0.85`` -> ``abstained=True`` (Phase 1c),
                                         REASON_LOW_NORMALIZATION_CONFIDENCE (the
                                         attempt resolved only via weak tiers — a
                                         second no-Layer-3-update trigger besides
                                         ``unresolved_rate``)
    - ``min_parser_confidence < 0.6`` -> suppress ``missing``,
                                         REASON_LOW_PARSER_CONFIDENCE
    - ``transcript_audit_failed``     -> suppress ``missing``,
                                         REASON_TRANSCRIPT_AUDIT_FAILED
    - any ``misconception_confidence < 0.8`` -> suppress ``misconception``,
                                         REASON_LOW_MISCONCEPTION_CONFIDENCE
                                         (the finding still persists for review)
    - ``reference_invalid``           -> REASON_REFERENCE_INVALID (grading already
                                         blocked upstream; surfaced here, not
                                         re-raised)

    An empty misconception bank is deliberately NOT a gate here (lane B3a/D1): it
    is the normal cold-start state and never abstains — the fact is carried on
    ``GradeResult.soundness_applicable`` + the artifact marker instead.

    §10 composite gate (``composite_enabled``, default False — byte-identical
    when omitted): when True, ``abstained`` is decided SOLELY by
    ``node_coverage >= coverage_min`` — overriding whatever the unresolved_rate/
    normalization_confidence gates computed above (those reasons still get
    RECORDED for audit, they just no longer drive ``abstained``).
    ``coverage < coverage_min`` appends REASON_COMPOSITE_LOW_COVERAGE.
    ``contradiction_count`` is recorded in ``Abstention.composite`` for audit
    but never forces abstention on its own (a detected misconception is
    informative feedback, not grading uncertainty).

    Pure + deterministic reason ordering (gate-declaration order)."""
    reasons: list[str] = []
    suppressed: set[str] = set()

    abstained = unresolved_rate > ABSTENTION_THRESHOLDS["unresolved_rate"]
    if abstained:
        reasons.append(REASON_HIGH_UNRESOLVED)

    # Phase 1c quality brake: a low weakest-link normalization_confidence means the
    # attempt resolved only via weak tiers -> distrust even at low unresolved_rate.
    # Plain assignment (NOT `or`): this gate sets abstained independently; the
    # reason list still records BOTH reasons when both gates fire.
    if normalization_confidence < ABSTENTION_THRESHOLDS["min_normalization_confidence"]:
        abstained = True
        reasons.append(REASON_LOW_NORMALIZATION_CONFIDENCE)

    if min_parser_confidence < ABSTENTION_THRESHOLDS["min_parser_confidence"]:
        reasons.append(REASON_LOW_PARSER_CONFIDENCE)
        suppressed.add(_SUPPRESS_MISSING)

    if transcript_audit_failed:
        reasons.append(REASON_TRANSCRIPT_AUDIT_FAILED)
        suppressed.add(_SUPPRESS_MISSING)

    if any(
        c < ABSTENTION_THRESHOLDS["misconception_confidence"] for c in misconception_confidences
    ):
        reasons.append(REASON_LOW_MISCONCEPTION_CONFIDENCE)
        suppressed.add(_SUPPRESS_MISCONCEPTION)

    if reference_invalid:
        reasons.append(REASON_REFERENCE_INVALID)

    composite: dict | None = None
    if composite_enabled:
        decision = "grade" if node_coverage >= coverage_min else "abstain"
        abstained = decision == "abstain"
        if abstained:
            reasons.append(REASON_COMPOSITE_LOW_COVERAGE)
        composite = {
            "coverage": node_coverage,
            "contradictions": contradiction_count,
            "coverage_min": coverage_min,
            "decision": decision,
        }

    return Abstention(
        abstention_reasons=tuple(reasons),
        abstained=abstained,
        suppressed_event_kinds=frozenset(suppressed),
        composite=composite,
    )
