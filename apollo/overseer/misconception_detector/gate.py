"""Corroboration + dual-tau confidence gate.

Contract: ``docs/_archive/plans/2026-07-08-apollo-misconception-detector-plan.md``
section 5.5 (A1 dual-tau selection), amended by
``docs/_archive/specs/2026-07-08-apollo-misconception-corroboration-redesign.md``
§4.0/§4.4/§5 (A9-A13, the "authoritative judge" redesign).

``gate_findings`` groups per-concept ``ConceptFinding``s from the three
detector tiers (``sympy_veto``, ``bank_pattern``, ``judge``) and decides, per
judge/deterministic concept, the outcome per the full §5 truth-table:

  * **Deterministic** (``sympy_veto``) always self-docks, ceiling-eligible.
  * **Judge + bank co-keyed** (same validated ``bank_code``, matched
    GLOBALLY across the whole detection result — NOT by ``concept_key``,
    A13) that clears the judge's routed tau -> DOCK, represented by the
    JUDGE finding (node_id-keyed, so ``centrality`` resolves it),
    ceiling-eligible.
  * **Lone bank-keyed judge >= TAU_SOLO_JUDGE** (no bank corroboration) ->
    DOCK, penalty-only (``ceiling_eligible=False``, A12).
  * **Lone bank-keyed judge, routed-tau-ok but sub-solo-tau**, or **lone
    UNKEYED judge that clears its routed tau** -> ``needs_clarification``
    (never docks).
  * **Lone judge sub-routed-tau**, or **lone bank_pattern finding** (no judge
    ever names its code) -> DROP.

Pure function: no IO, no LLM, no DB. Returns a NEW tuple; input findings are
never mutated (``ConceptFinding`` is frozen). Both dock/clarify builders use
``dataclasses.replace`` (A13/§4.6.1) so no field — in particular
``bank_code``/``bank_match_above_floor``/``signature`` — is ever silently
dropped from a docked/clarified representative.

Cross-namespace keying (A13, §4.0): the judge tier keys each finding by the
reference-graph node's semantic ``node_id`` (a per-node string); the
``bank_pattern`` tier keys every finding by the SESSION-scoped
``str(concept_id)`` (one shared key for the whole attempt's bank — a
different encoding AND a different cardinality). Grouping strictly by
``concept_key`` therefore never lets a bank finding corroborate a judge
finding on the same misconception in production. The fix: ``gate_findings``
builds a ``bank_by_code`` index (every ``bank_pattern`` finding with a
non-None ``bank_code``, keyed by that code) ONCE, and ``_gate_one_concept``
looks up the corroborating bank finding by the judge's OWN validated
``bank_code`` — never by ``concept_key`` equality. The docked representative
of any bank-corroborated dock is ALWAYS the judge (or sympy) finding, never
the bank witness, so ``centrality`` (also node_id-keyed) can resolve it.

Dual-tau selection (A1): ``ConceptFinding`` carries a
``verdict_token_prob_present`` origin bit, set upstream in
``judge.py::_finding_from_row`` from ``verdict_token_prob is not None``. The
gate routes on that bit — the token-probability path is gated at ``tau_fire``
(0.85), the verbalized-confidence fallback at the stricter ``tau_verbalized``
(0.90).
"""

from __future__ import annotations

import dataclasses

from apollo.overseer.misconception_detector.config import (
    TAU_FIRE,
    TAU_FIRE_VERBALIZED,
    TAU_SOLO_JUDGE,
)
from apollo.overseer.misconception_detector.types import ConceptFinding

_DETERMINISTIC_SOURCES = frozenset({"sympy_veto"})
_ANCHOR_SOURCES = _DETERMINISTIC_SOURCES | frozenset({"judge"})


def gate_findings(
    findings: tuple[ConceptFinding, ...],
    *,
    opposes_index: dict[str, str] | None = None,
    tau_fire: float = TAU_FIRE,
    tau_verbalized: float = TAU_FIRE_VERBALIZED,
    tau_solo: float = TAU_SOLO_JUDGE,
) -> tuple[ConceptFinding, ...]:
    """Group deterministic/judge findings per concept_key, index bank
    findings by validated bank_code, then dock / downgrade / drop per §5.

    Returns a new tuple of ``ConceptFinding`` — docked findings carry
    ``verdict="misconception"``, ``corroborated=True``, and an explicit
    ``ceiling_eligible`` per the truth-table row that fired; downgraded
    findings carry ``verdict="needs_clarification"``,
    ``corroborated=False``; dropped concepts contribute nothing.

    A lone ``bank_pattern`` finding (row 9 — no judge/sympy concept ever
    names its ``bank_code``) never anchors its own group: it is a corroboration
    witness only, indexed in ``bank_by_code``, and is dropped by construction
    if no anchor concept looks it up.

    ``opposes_index`` (F-struct, default empty) maps a reference node's
    ``concept_key`` (``node_id``) to the ``bank_code`` a bank entry ``opposes``
    for that node. When non-empty (only under the ``APOLLO_MISC_STRUCT_COKEY``
    sub-flag; the caller passes ``{}`` otherwise), a confident
    ``wrong``/``misconception`` judge verdict at a node the judge could NOT
    name (``bank_code is None``) docks structurally — the graph names the
    misconception the judge only localized. An EMPTY index is byte-identical
    to pre-F-struct behavior.
    """
    opposes = opposes_index or {}
    bank_by_code: dict[str, list[ConceptFinding]] = {}
    anchors: dict[str, list[ConceptFinding]] = {}
    for finding in findings:
        if finding.source == "bank_pattern":
            if finding.bank_code is not None:
                bank_by_code.setdefault(finding.bank_code, []).append(finding)
            continue
        if finding.source in _ANCHOR_SOURCES:
            anchors.setdefault(finding.concept_key, []).append(finding)

    gated: list[ConceptFinding] = []
    for group in anchors.values():
        outcome = _gate_one_concept(
            group,
            bank_by_code=bank_by_code,
            opposes_index=opposes,
            tau_fire=tau_fire,
            tau_verbalized=tau_verbalized,
            tau_solo=tau_solo,
        )
        if outcome is not None:
            gated.append(outcome)

    return tuple(gated)


def _gate_one_concept(
    group: list[ConceptFinding],
    *,
    bank_by_code: dict[str, list[ConceptFinding]],
    opposes_index: dict[str, str] | None = None,
    tau_fire: float,
    tau_verbalized: float,
    tau_solo: float,
) -> ConceptFinding | None:
    """Decide the fate of ONE judge/deterministic concept. Returns the single
    representative finding to keep (docked or downgraded), or None to drop
    the concept entirely. Implements the §5 truth-table exactly, plus the
    F-struct structural co-key branch (default-empty ``opposes_index`` ⇒
    byte-identical to the §5 truth-table)."""
    opposes = opposes_index or {}
    deterministic = [f for f in group if f.source in _DETERMINISTIC_SOURCES]
    if deterministic:
        # Rows 1/2: self-corroborating, always ceiling-eligible. Prefer the
        # deterministic finding as the representative.
        return _docked(deterministic[0], ceiling_eligible=True)

    judges = [f for f in group if f.source == "judge"]
    if not judges:
        return None

    best_judge = max(judges, key=lambda f: f.confidence)
    routed_ok = _judge_clears_tau(best_judge, tau_fire=tau_fire, tau_verbalized=tau_verbalized)
    solo_ok = routed_ok and best_judge.confidence >= tau_solo

    corroborating_bank = None
    if best_judge.bank_code is not None:
        candidates = bank_by_code.get(best_judge.bank_code)
        if candidates:
            corroborating_bank = max(candidates, key=lambda f: f.confidence)

    if corroborating_bank is not None:
        # Rows 3 / 3b: judge + bank agree on the same validated bank_code
        # (floor-free — B's bank_match_above_floor is deliberately ignored).
        if routed_ok:
            return _docked(best_judge, ceiling_eligible=True)
        return _needs_clarification(best_judge)

    # No bank corroboration available for this judge's named code.
    if best_judge.bank_code is not None:
        # Row 5: lone bank-keyed judge clearing BOTH routed tau AND the
        # stricter solo tau -> dock, penalty-only.
        if solo_ok:
            return _docked(best_judge, ceiling_eligible=False)
        # Row 6: keyed but sub-solo-tau (still clears routed tau) -> clarify.
        if routed_ok:
            return _needs_clarification(best_judge)
        # Row 8: keyed but sub-routed-tau -> drop.
        return None

    # F-struct structural co-key (D4 mutual exclusion): this branch is reached
    # ONLY when ``best_judge.bank_code is None`` — the judge LOCALIZED an error
    # to this node (``wrong``/``misconception``) but named NO validated code.
    # If a bank entry opposes this node's entity_key (resolved into
    # ``opposes_index`` by the caller), the GRAPH names it and we dock via the
    # existing co-key semantics. Control-safe: ``clear``/``needs_clarification``
    # are excluded by the explicit verdict guard (and controls only ever return
    # ``clear``). Confidence floor (D2): gated on ``routed_ok`` — the SAME dual-
    # tau routing used everywhere — so a hedged low-confidence localization can
    # never dock. Empty ``opposes_index`` ⇒ this lookup misses and prior
    # behavior (rows 7/8 below) is byte-identical.
    struct_code = opposes.get(best_judge.concept_key)
    if struct_code is not None and routed_ok and best_judge.verdict in ("wrong", "misconception"):
        return _struct_docked(best_judge, bank_code=struct_code)

    # Row 7: lone UNKEYED judge clearing routed tau -> clarify, never docks.
    if routed_ok:
        return _needs_clarification(best_judge)
    # Row 8: lone UNKEYED judge sub-routed-tau -> drop.
    return None


def _judge_clears_tau(finding: ConceptFinding, *, tau_fire: float, tau_verbalized: float) -> bool:
    """A judge finding clears the ONE tau applicable to its origin (A1).

    ``ConceptFinding.verdict_token_prob_present`` selects the threshold: the
    token-probability path uses ``tau_fire`` (0.85); the
    verbalized-confidence fallback uses the stricter ``tau_verbalized``
    (0.90)."""
    tau = tau_fire if finding.verdict_token_prob_present else tau_verbalized
    return finding.confidence >= tau


def _docked(finding: ConceptFinding, *, ceiling_eligible: bool) -> ConceptFinding:
    """Build the docked representative via ``dataclasses.replace`` (A13,
    §4.6.1) so every field NOT explicitly named here — in particular
    ``bank_code``, ``bank_match_above_floor``, ``signature``,
    ``concept_key`` — is inherited verbatim from the incoming finding. A
    forgotten ``ceiling_eligible`` at a call site is a compile-time error
    (no default), per the branch's row in the §5 truth-table."""
    return dataclasses.replace(
        finding,
        verdict="misconception",
        corroborated=True,
        ceiling_eligible=ceiling_eligible,
    )


def _struct_docked(finding: ConceptFinding, *, bank_code: str) -> ConceptFinding:
    """Structural co-key dock (F-struct): the judge localized the error to this
    node and the graph named it via a bank entry's ``opposes``. Reuses row-3
    co-key semantics — ceiling-eligible + bank-keyed so merge emits the artifact
    ``misconceptions[]`` row. Sets ``bank_code`` + ``signature`` so
    ``merge._is_bank_keyed`` picks it up (the exact shape ``merge._keyed_row``
    and the ``docked`` filter already consume — no merge change needed)."""
    return dataclasses.replace(
        finding,
        verdict="misconception",
        corroborated=True,
        ceiling_eligible=True,
        bank_code=bank_code,
        signature=f"misc.{bank_code}",
    )


def _needs_clarification(finding: ConceptFinding) -> ConceptFinding:
    """``ceiling_eligible`` is NOT overridden here — it inherits the incoming
    finding's value, which is always False for a pre-gate tier finding (no
    tier ever sets it True), so a clarification row never becomes
    ceiling-eligible."""
    return dataclasses.replace(
        finding,
        verdict="needs_clarification",
        corroborated=False,
    )
