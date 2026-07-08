"""Corroboration + dual-tau confidence gate.

Contract: ``docs/_archive/plans/2026-07-08-apollo-misconception-detector-plan.md``
section 5.5, amended by A1 (dual-tau selection for judge findings).

``gate_findings`` groups per-concept ``ConceptFinding``s from the three
detector tiers (``sympy_veto``, ``bank_pattern``, ``judge``) and decides, per
``concept_key``, whether the concept:

  * DOCKS — rewritten to ``verdict="misconception"``, ``corroborated=True`` —
    iff a deterministic ``sympy_veto`` finding is present (self-corroborating),
    OR >=2 independent sources agree AND any ``judge`` finding among them
    clears its applicable tau (``tau_fire`` for the token-probability path,
    ``tau_fire_verbalized`` for the verbalized-confidence fallback — A1);
  * is DOWNGRADED to ``needs_clarification`` (never docked) when the only
    signal is a lone ``judge`` finding, regardless of its confidence — a lone
    judge opinion routes to the clarification loop, it never docks by itself;
  * is DROPPED otherwise (e.g. a lone sub-tau judge finding, or a judge
    finding that fails its tau in a would-be corroborating group and leaves
    no other deterministic/agreeing signal).

Pure function: no IO, no LLM, no DB. Returns a NEW tuple; input findings are
never mutated (``ConceptFinding`` is frozen, so this is enforced by the type
system too, but we still always construct new instances for changed rows).

Dual-tau selection (A1): ``ConceptFinding`` carries a
``verdict_token_prob_present`` origin bit, set upstream in
``judge.py::_finding_from_row`` from ``verdict_token_prob is not None``. The
gate routes on that bit — the token-probability path is gated at ``tau_fire``
(0.85), the verbalized-confidence fallback at the stricter ``tau_verbalized``
(0.90). This is a SINGLE comparison against the routed threshold; the earlier
``>= tau_fire OR >= tau_verbalized`` formulation degenerated to the looser
bound and made ``TAU_FIRE_VERBALIZED`` dead code on the default production path
(a verbalized finding at 0.87 wrongly corroborated a dock). The dual constants
remain explicit parameters so a caller can override either threshold for a
specific batch without touching this module's logic.
"""

from __future__ import annotations

from apollo.overseer.misconception_detector.config import TAU_FIRE, TAU_FIRE_VERBALIZED
from apollo.overseer.misconception_detector.types import ConceptFinding

_DETERMINISTIC_SOURCES = frozenset({"sympy_veto"})


def gate_findings(
    findings: tuple[ConceptFinding, ...],
    *,
    tau_fire: float = TAU_FIRE,
    tau_verbalized: float = TAU_FIRE_VERBALIZED,
) -> tuple[ConceptFinding, ...]:
    """Group by concept_key, then dock / downgrade / drop per concept.

    Returns a new tuple of ``ConceptFinding`` — docked findings carry
    ``verdict="misconception"`` and ``corroborated=True``; downgraded
    findings carry ``verdict="needs_clarification"`` and
    ``corroborated=False``; dropped concepts contribute nothing.
    """
    by_concept: dict[str, list[ConceptFinding]] = {}
    for finding in findings:
        by_concept.setdefault(finding.concept_key, []).append(finding)

    gated: list[ConceptFinding] = []
    for concept_key, group in by_concept.items():
        outcome = _gate_one_concept(group, tau_fire=tau_fire, tau_verbalized=tau_verbalized)
        if outcome is not None:
            gated.append(outcome)

    return tuple(gated)


def _gate_one_concept(
    group: list[ConceptFinding],
    *,
    tau_fire: float,
    tau_verbalized: float,
) -> ConceptFinding | None:
    """Decide the fate of ONE concept's findings. Returns the single
    representative finding to keep (docked or downgraded), or None to drop
    the concept entirely."""
    deterministic = [f for f in group if f.source in _DETERMINISTIC_SOURCES]
    if deterministic:
        # Self-corroborating: deterministic veto alone (or with anything
        # else) always docks. Prefer the deterministic finding as the base.
        return _docked(deterministic[0])

    judges = [f for f in group if f.source == "judge"]
    non_judges = [f for f in group if f.source != "judge"]

    judge_clears_tau = any(
        _judge_clears_tau(j, tau_fire=tau_fire, tau_verbalized=tau_verbalized) for j in judges
    )

    independent_sources = {f.source for f in group}
    has_corroboration = len(independent_sources) >= 2

    if has_corroboration:
        if judges:
            # A judge is part of the group: it must clear its tau for the
            # group to dock. Non-judge + non-judge corroboration (no judge
            # present at all) docks regardless (handled by the `else`).
            if judge_clears_tau:
                return _docked(non_judges[0] if non_judges else judges[0])
            # Judge present but fails tau: no valid corroboration from it.
            # Fall through to lone-judge / drop logic using non-judge signals.
        else:
            # >=2 non-judge independent sources agreeing (e.g. bank_pattern
            # appearing from two distinct paths, or any future tier) docks.
            return _docked(non_judges[0])

    # No valid corroboration reached. If there's a lone judge signal (with or
    # without a failed-tau judge alongside), route the single most confident
    # judge finding to needs_clarification rather than dropping silently —
    # but ONLY when there is exactly one independent source overall (a lone
    # judge opinion, unaccompanied by any other tier).
    if judges and not non_judges:
        best_judge = max(judges, key=lambda f: f.confidence)
        if _judge_clears_tau(best_judge, tau_fire=tau_fire, tau_verbalized=tau_verbalized):
            return _needs_clarification(best_judge)
        return None

    # Judge present alongside non-judge signal(s) but failed tau, and no
    # other corroboration path exists -> drop entirely (per contract: a
    # judge below tau is dropped; the lone remaining non-judge signal is
    # insufficient on its own to dock).
    return None


def _judge_clears_tau(finding: ConceptFinding, *, tau_fire: float, tau_verbalized: float) -> bool:
    """A judge finding clears the ONE tau applicable to its origin (A1).

    ``ConceptFinding.verdict_token_prob_present`` (set upstream in
    ``judge.py::_finding_from_row`` from ``verdict_token_prob is not None``)
    selects the threshold: the token-probability path uses ``tau_fire`` (0.85);
    the verbalized-confidence fallback uses the stricter ``tau_verbalized``
    (0.90), because verbalized confidence runs overconfident. This is a SINGLE
    comparison against the routed threshold — the old ``>= tau_fire OR >=
    tau_verbalized`` degenerated to the looser bound and made
    ``TAU_FIRE_VERBALIZED`` dead code on the default production path."""
    tau = tau_fire if finding.verdict_token_prob_present else tau_verbalized
    return finding.confidence >= tau


def _docked(finding: ConceptFinding) -> ConceptFinding:
    return ConceptFinding(
        concept_key=finding.concept_key,
        verdict="misconception",
        confidence=finding.confidence,
        severity=finding.severity,
        evidence_span=finding.evidence_span,
        signature=finding.signature,
        source=finding.source,
        corroborated=True,
        verdict_token_prob_present=finding.verdict_token_prob_present,
    )


def _needs_clarification(finding: ConceptFinding) -> ConceptFinding:
    return ConceptFinding(
        concept_key=finding.concept_key,
        verdict="needs_clarification",
        confidence=finding.confidence,
        severity=finding.severity,
        evidence_span=finding.evidence_span,
        signature=finding.signature,
        source=finding.source,
        corroborated=False,
        verdict_token_prob_present=finding.verdict_token_prob_present,
    )
