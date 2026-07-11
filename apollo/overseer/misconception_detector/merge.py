"""Severity-weighted merge + anti-dilution ceiling for the misconception detector.

Frozen contract: ``docs/_archive/plans/2026-07-08-apollo-misconception-detector-plan.md``
section 5.6 (T7), amended by A5 (``canonical_key`` rules).

``merge_detections`` is the last PURE stage before ``apply.py`` turns the
outcome into an adjusted composite/rubric. It takes the GATE-cleared findings
(each already docked by ``gate.py``: ``verdict="misconception"``,
``corroborated=True``) plus a per-concept centrality map, and produces a frozen
``MergeOutcome`` with four products:

  * ``misconception_penalty`` — ``min(clamp, Sum severity_i)`` where
    ``severity_i = centrality.get(concept_key, CENTRALITY_W_MIN) * confidence_i``
    over ALL docked findings (bank-keyed AND unkeyed alike). The detector only
    ever subtracts, never adds, so this is always >= 0 and clamped from above.
  * ``ceiling_applied`` — the anti-dilution guard: a CEILING-ELIGIBLE docked
    finding (A12 — a deterministic ``sympy_veto`` dock or a bank-corroborated
    judge dock; NOT a lone-judge penalty-only dock) attacking a
    maximally-central concept must cap the artifact composite below the named
    Strong scorecard band, so a student cannot dilute one load-bearing
    misconception across otherwise-strong coverage. "Central" is DERIVED from
    the centrality map's maximum value (not a hand-authored constant), so it
    scales with each attempt's own reference graph.
  * ``misconceptions`` — the artifact ``misconceptions[]`` ledger rows, emitted
    ONLY for docked findings that carry a bank-keyed signature (``misc.<code>``).
    Per A5 the row's ``canonical_key`` is the BARE ``misc.<code>`` taken verbatim
    from the finding's signature — never re-prefixed, never the ``unkeyed:*``
    placeholder (which would double-prefix downstream in
    ``apollo/emergent/store.py::_signature_for`` and never promote). An
    ``unkeyed:<concept_id>`` docked finding still contributes to the penalty and
    the ceiling above, but is EXCLUDED from this keyed row list.
  * ``ledger_findings`` — every docked finding, now carrying its computed
    ``severity`` (filled via a NEW immutable copy; the input finding is never
    mutated), for the emergent store to persist.

Dedup (2026-07-10 live bug fix, staging session 43 / attempt 44): before any
penalty accumulation, docked findings are deduplicated by ``signature`` (the
artifact's ``canonical_key`` -- see ``_keyed_row``), keeping the MAX-confidence
instance per signature. Production observed the SAME finding
(``misc.forgets_minus_sign``, same ``evidence_span``) appear twice in one
attempt's gated set, doubling its severity into ``SEVERITY_CLAMP`` when a
single dock should have applied. This is a flag-independent behavior change
(one error, one dock) -- no new flag is introduced; see
``docs/superpowers/specs/2026-07-10-apollo-topic-score-design.md`` section 2,
which calls it out explicitly as intentionally not byte-identical to prior
flag-off behavior.

Pure module: no IO, no LLM, no DB. Immutable throughout — returns a new
``MergeOutcome`` and never mutates the input findings or centrality map.
"""

from __future__ import annotations

import dataclasses

from apollo.overseer.misconception_detector.config import (
    CEILING_COMPOSITE,
    CENTRALITY_W_MIN,
    SEVERITY_CLAMP,
)
from apollo.overseer.misconception_detector.types import ConceptFinding, MergeOutcome

# A bank-keyed signature looks like ``misc.<code>``; an un-attributable finding
# carries the ``unkeyed:<concept_id>`` placeholder (see ConceptFinding.signature
# contract in types.py / plan section 2).
_MISC_PREFIX = "misc."
_UNKEYED_PREFIX = "unkeyed:"


def merge_detections(
    gated: tuple[ConceptFinding, ...],
    *,
    centrality: dict[str, float],
    clamp: float = SEVERITY_CLAMP,
    ceiling_composite: float = CEILING_COMPOSITE,
) -> MergeOutcome:
    """Merge gate-cleared docked findings into a frozen ``MergeOutcome``.

    Args:
        gated: docked findings from ``gate.py`` (``verdict="misconception"``,
            ``corroborated=True``). An empty tuple yields the empty outcome.
            Findings sharing a ``signature`` are deduplicated (max-confidence
            wins) before severity/penalty accumulation — see
            ``_dedup_by_signature``.
        centrality: ``{concept_key: 0..1}`` from ``centrality.py``. A finding on
            a concept absent here weights at ``CENTRALITY_W_MIN``.
        clamp: max total penalty (defaults to ``SEVERITY_CLAMP``).
        ceiling_composite: the named-band ceiling constant — accepted for
            signature parity with the plan / downstream ``apply.py`` and to keep
            the ceiling policy configurable, though this pure stage only emits
            the boolean ``ceiling_applied`` flag (``apply.py`` owns the actual
            composite cap). Unused arithmetically here by design.

    Returns:
        A frozen ``MergeOutcome``.
    """
    # Consider only genuinely docked findings. Defensive: gate.py already
    # returns only docked/clarification rows, so a stray non-corroborated or
    # non-misconception row (a needs_clarification downgrade that leaked, or a
    # future caller passing raw findings) must not be treated as a dock.
    docked = tuple(f for f in gated if f.corroborated and f.verdict == "misconception")

    if not docked:
        return MergeOutcome(
            misconception_penalty=0.0,
            misconceptions=(),
            ceiling_applied=False,
            ledger_findings=(),
        )

    deduped = _dedup_by_signature(docked)

    ledger_findings = tuple(_with_severity(f, _severity_for(f, centrality)) for f in deduped)

    total_severity = sum(f.severity for f in ledger_findings)
    penalty = min(clamp, total_severity)

    ceiling_applied = _any_central(ledger_findings, centrality)

    misconceptions = tuple(_keyed_row(f) for f in ledger_findings if _is_bank_keyed(f.signature))

    return MergeOutcome(
        misconception_penalty=penalty,
        misconceptions=misconceptions,
        ceiling_applied=ceiling_applied,
        ledger_findings=ledger_findings,
    )


def _dedup_by_signature(
    findings: tuple[ConceptFinding, ...],
) -> tuple[ConceptFinding, ...]:
    """Collapse docked findings that share a ``signature`` to ONE instance,
    keeping the max-confidence one (2026-07-10 live bug fix).

    ``signature`` is the same field ``_keyed_row`` copies verbatim into the
    artifact row's ``canonical_key`` (bare ``misc.<code>``) or the
    ``unkeyed:<concept_id>`` placeholder — the identity a repeated detector
    finding shares when it names the same misconception twice in one attempt
    (production case: two ``misc.forgets_minus_sign`` rows, same
    ``evidence_span``, doubling the dock). Order of first appearance is
    preserved for determinism; does not mutate any input finding.
    """
    best_by_signature: dict[str, ConceptFinding] = {}
    for finding in findings:
        current = best_by_signature.get(finding.signature)
        if current is None or finding.confidence > current.confidence:
            # dict insertion order tracks FIRST assignment of a key, so a
            # later same-signature finding (even the winning higher-
            # confidence one) does not reorder its slot — first-appearance
            # order is preserved without a second pass.
            best_by_signature[finding.signature] = finding
    return tuple(best_by_signature.values())


def _severity_for(finding: ConceptFinding, centrality: dict[str, float]) -> float:
    """severity = centrality(concept) * confidence, with the peripheral floor."""
    weight = centrality.get(finding.concept_key, CENTRALITY_W_MIN)
    return weight * finding.confidence


def _with_severity(finding: ConceptFinding, severity: float) -> ConceptFinding:
    """Return a NEW finding with ``severity`` filled — never mutate the input."""
    return dataclasses.replace(finding, severity=severity)


def _any_central(findings: tuple[ConceptFinding, ...], centrality: dict[str, float]) -> bool:
    """True iff any CEILING-ELIGIBLE docked finding attaches to a
    maximally-central concept (A12, corroboration/keying redesign spec
    §4.7/§8).

    "Central" is DERIVED from the centrality map's own maximum value (not a
    hand-authored constant) so the ceiling scales with each attempt's reference
    graph. When the map is empty (every finding fell back to CENTRALITY_W_MIN),
    the max collapses to that floor and any ceiling-eligible docked finding
    trips the ceiling — a conservative choice: with no graph structure to
    rank concepts, a corroborated misconception is treated as central.

    ``ceiling_eligible`` is stamped by ``gate.py`` per the §5 truth-table: a
    deterministic (``sympy_veto``) dock or a bank-corroborated judge dock is
    eligible; a lone-judge (penalty-only) dock is NOT — it may still subtract
    from the penalty above, but a single uncorroborated LLM opinion can never
    by itself cap the band (A12/L2, the severity gradient / anti-dilution
    ceiling policy).
    """
    max_centrality = max(centrality.values(), default=CENTRALITY_W_MIN)
    return any(
        f.ceiling_eligible and centrality.get(f.concept_key, CENTRALITY_W_MIN) >= max_centrality
        for f in findings
    )


def _is_bank_keyed(signature: str) -> bool:
    """A signature is bank-keyed iff it is a bare ``misc.<code>`` (A5).

    An ``unkeyed:<concept_id>`` placeholder is NOT bank-keyed and never becomes
    a keyed ledger row.
    """
    return signature.startswith(_MISC_PREFIX) and not signature.startswith(_UNKEYED_PREFIX)


def _keyed_row(finding: ConceptFinding) -> dict:
    """Build one artifact ``misconceptions[]`` row from a bank-keyed finding.

    ``canonical_key`` is the BARE ``misc.<code>`` taken verbatim from the
    finding's signature (A5) — no re-prefixing, no ``unkeyed:*``. ``opposes`` is
    left ``None`` here; the emergent store re-derives opposition/signature
    downstream from the persisted bank entry.
    """
    return {
        "canonical_key": finding.signature,
        "evidence_span": finding.evidence_span,
        "confidence": finding.confidence,
        "opposes": None,
    }
