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

import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from apollo.ontology.nodes import Node, NodeType
from apollo.resolution.result import ResolutionResult

# A1 iter1 (G1 weekend campaign): dormant flag for the structural-denominator
# unresolved_rate (see :func:`unresolved_rate_of_v2`). Default OFF everywhere
# — repo idiom, mirrors apollo/handlers/*.py's env-flag helpers.
_ABSTENTION_DENOM_V2_FLAG = "APOLLO_ABSTENTION_DENOM_V2"

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
# D5/D6: the misconception bank was empty/absent for this concept. Soundness
# was never checked; soundness_score and contradiction_score are None.
# Layer-3 (coverage) still updates — this is NOT a full abstention.
REASON_MISCONCEPTION_BANK_EMPTY = "misconception_bank_empty"
# Phase 1c (spec §8): the attempt resolved only via weak tiers (low
# normalization_confidence) -> distrust even at low unresolved_rate.
REASON_LOW_NORMALIZATION_CONFIDENCE = "normalization_confidence_below_threshold"

# Event kinds (subset) WU-4B2 may be told to withhold.
_SUPPRESS_MISSING = "missing"
_SUPPRESS_MISCONCEPTION = "misconception"


@dataclass(frozen=True)
class Abstention:
    """The §6.6 abstention outcome (frozen, persisted by WU-4B3)."""

    abstention_reasons: tuple[str, ...]
    abstained: bool
    suppressed_event_kinds: frozenset[str]


def unresolved_rate_of(resolution: ResolutionResult) -> float:
    """``unresolved_count / total`` over ``resolution.resolved`` (count every
    node whose ``resolution != 'resolved'``). ``0.0`` for an empty attempt (an
    empty attempt never false-trips the gate)."""
    total = len(resolution.resolved)
    if total == 0:
        return 0.0
    unresolved = sum(1 for rn in resolution.resolved if rn.resolution != "resolved")
    return unresolved / total


def _abstention_denom_v2_enabled() -> bool:
    """Dormant-flag reader for the structural-denominator unresolved_rate.

    Repo idiom (mirrors ``apollo/handlers/done.py`` etc.): default OFF
    everywhere, including test, unless the env var is explicitly truthy."""
    return os.environ.get(_ABSTENTION_DENOM_V2_FLAG, "").lower() in ("1", "true", "yes")


def unresolved_rate_of_v2(
    resolution: ResolutionResult,
    node_type_by_id: Mapping[str, NodeType],
    candidate_types: frozenset[NodeType],
) -> float:
    """A1 iter1 (G1) — structural-denominator alternative to
    :func:`unresolved_rate_of`. Dormant behind
    :func:`_abstention_denom_v2_enabled` (default OFF); see
    :func:`unresolved_rate_for_abstention` for the flag-gated selector callers
    should actually use.

    Guiding principle: abstention should fire when the grader COULDN'T FOLLOW
    what the student taught, not when the parser was chatty. ``unresolved_rate_of``
    divides by EVERY student node the LLM parser emitted, including
    over-segmented variable-gloss/procedural-filler atoms that have no
    reference-graph counterpart to resolve against by construction
    (``apollo/resolution/candidates.py`` mints candidates only from the
    reference-solution steps + course misconceptions;
    ``resolver.py::type_compatible`` hard-gates a node against candidates of
    its OWN ``node_type`` — a node whose type has zero candidates of that type
    can never leave ``unresolved``). This function restricts the denominator
    to student nodes whose ``node_type`` IS present in ``candidate_types``
    (derived from THIS problem's closed candidate set, per
    ``apollo/resolution/candidates.py::build_candidate_set`` — reference
    nodes + misconceptions) — a type/structure-driven exclusion, never a
    content/correctness one, so it cannot manufacture credit.

    ``node_type_by_id`` missing an entry for a resolved node's id treats that
    node as NOT relevant (defensive: a node this function cannot type is
    excluded rather than assumed countable). Identical to
    :func:`unresolved_rate_of` when every student node's type is in
    ``candidate_types``. ``0.0`` for an empty relevant set (an attempt with no
    structurally-relevant student nodes never false-trips the gate, matching
    ``unresolved_rate_of``'s empty-attempt behavior)."""
    relevant = tuple(
        rn for rn in resolution.resolved if node_type_by_id.get(rn.node_id) in candidate_types
    )
    total = len(relevant)
    if total == 0:
        return 0.0
    unresolved = sum(1 for rn in relevant if rn.resolution != "resolved")
    return unresolved / total


def unresolved_rate_for_abstention(
    resolution: ResolutionResult,
    *,
    node_type_by_id: Mapping[str, NodeType] | None = None,
    candidate_types: frozenset[NodeType] | None = None,
) -> float:
    """Flag-gated selector between :func:`unresolved_rate_of` (v1, the only
    live behavior) and :func:`unresolved_rate_of_v2` (dormant, A1 iter1).

    Flag OFF (default) -> always :func:`unresolved_rate_of(resolution)`,
    byte-identical to pre-A1-iter1 behavior regardless of what
    ``node_type_by_id``/``candidate_types`` a caller passes (defensive: a
    caller threading the v2 inputs never regresses the OFF path). Flag ON
    without BOTH v2 inputs also falls back to v1 (v2 is meaningless without
    them) — only flag-ON + both-inputs-supplied uses
    :func:`unresolved_rate_of_v2`."""
    if (
        _abstention_denom_v2_enabled()
        and node_type_by_id is not None
        and candidate_types is not None
    ):
        return unresolved_rate_of_v2(resolution, node_type_by_id, candidate_types)
    return unresolved_rate_of(resolution)


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
    misconception_bank_empty: bool = False,
    normalization_confidence: float = 1.0,
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
    - ``misconception_bank_empty``    -> REASON_MISCONCEPTION_BANK_EMPTY (D5/D6:
                                         soundness was never checked; coverage still
                                         updates Layer-3 — NOT a full abstention)

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

    if misconception_bank_empty:
        reasons.append(REASON_MISCONCEPTION_BANK_EMPTY)

    return Abstention(
        abstention_reasons=tuple(reasons),
        abstained=abstained,
        suppressed_event_kinds=frozenset(suppressed),
    )
