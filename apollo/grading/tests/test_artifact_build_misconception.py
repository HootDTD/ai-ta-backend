"""T11 — wire ``build_llm_artifact`` to the misconception-detector's
``MergeOutcome`` (penalty / misconceptions / composite).

Frozen contract: ``docs/_archive/plans/2026-07-08-apollo-misconception-detector-plan.md``
section 7 (T11), section 8's T11 RED assertions.

``build_llm_artifact`` grows one new keyword-only parameter,
``detection_outcome: MergeOutcome | None = None``:

  * ``None`` (the default) or an EMPTY outcome (``misconception_penalty == 0.0``
    and ``misconceptions == ()``) -> the artifact payload is BYTE-IDENTICAL to
    today (design invariant #1 — the hard regression guard). This covers both
    the flag-OFF path (caller never threads an outcome) and a flag-ON call
    where the detector found nothing for this attempt.
  * A non-empty outcome -> ``scores.misconception_penalty`` becomes
    ``outcome.misconception_penalty`` (no longer hardcoded ``0.0``),
    ``misconceptions`` becomes ``list(outcome.misconceptions)`` (no longer the
    hardcoded ``[]``), and ``scores.composite`` is recomputed via
    ``apply.apply_penalty(composite=<today's composite>, outcome=outcome)`` —
    i.e. the detector only ever SUBTRACTS from (or ceilings) the already-
    computed LLM-path composite; it never changes ``node_coverage``/
    ``edge_coverage`` or any other input.

Pure module: no IO, no LLM, no DB. Every assertion below is offline.
"""

from __future__ import annotations

from typing import Any

import pytest

from apollo.grading.artifact_build import build_llm_artifact
from apollo.grading.composite import load_weights
from apollo.overseer.misconception_detector.apply import apply_penalty
from apollo.overseer.misconception_detector.types import ConceptFinding, MergeOutcome

_COVERAGE = {
    "per_step": {"k1": "covered", "k2": "missing"},
    "confidences": {"k1": 0.9, "k2": 0.0},
}
_RUBRIC = {"overall": {"score": 71}}


def _base_kwargs(**overrides) -> dict:
    kwargs = dict(
        coverage=_COVERAGE,
        rubric=_RUBRIC,
        weights=load_weights(),
        graph_failure=None,
        latency_ms=5,
        clarification_trace=[],
    )
    kwargs.update(overrides)
    return kwargs


def _docked_finding(
    *,
    concept_key: str = "concept.gdp_identity",
    confidence: float = 0.9,
    severity: float = 0.27,
    evidence_span: str = "transfers are part of GDP",
    signature: str = "misc.includes_transfers",
) -> ConceptFinding:
    return ConceptFinding(
        concept_key=concept_key,
        verdict="misconception",
        confidence=confidence,
        severity=severity,
        evidence_span=evidence_span,
        signature=signature,
        source="judge",
        corroborated=True,
    )


def _non_empty_outcome(**overrides: Any) -> MergeOutcome:
    finding = _docked_finding()
    kwargs: dict[str, Any] = dict(
        misconception_penalty=0.27,
        misconceptions=(
            {
                "canonical_key": "misc.includes_transfers",
                "evidence_span": "transfers are part of GDP",
                "confidence": 0.9,
                "opposes": None,
            },
        ),
        ceiling_applied=False,
        ledger_findings=(finding,),
    )
    kwargs.update(overrides)
    return MergeOutcome(**kwargs)


def _empty_outcome() -> MergeOutcome:
    return MergeOutcome(
        misconception_penalty=0.0,
        misconceptions=(),
        ceiling_applied=False,
        ledger_findings=(),
    )


# --------------------------------------------------------------------------- #
# Default-None guard (design invariant #1 — byte-identical to today)
# --------------------------------------------------------------------------- #
def test_default_none_is_byte_identical_to_no_param_call():
    """Omitting ``detection_outcome`` entirely must produce the exact same
    payload as passing it explicitly as ``None`` — the parameter is additive
    and opt-in only."""
    kwargs = _base_kwargs()
    without_param = build_llm_artifact(**kwargs)
    with_none = build_llm_artifact(**kwargs, detection_outcome=None)

    assert without_param == with_none


def test_none_outcome_keeps_zero_penalty_and_empty_misconceptions():
    art = build_llm_artifact(**_base_kwargs(), detection_outcome=None)

    assert art["scores"]["misconception_penalty"] == 0.0
    assert art["misconceptions"] == []


def test_none_outcome_composite_unchanged_from_todays_mapping():
    """With no outcome, composite stays the plain rubric-score renormalization
    (spec §1/§3) — no ``apply_penalty`` call happens at all."""
    art = build_llm_artifact(**_base_kwargs(), detection_outcome=None)

    assert art["scores"]["composite"] == pytest.approx(0.71)


def test_empty_outcome_is_also_byte_identical_to_none():
    """An outcome object that carries zero penalty and no misconceptions
    (the flag-ON-but-detector-found-nothing case) must produce a payload
    identical to the ``None`` default — the detector never adds a phantom
    penalty/ceiling for an empty result."""
    none_art = build_llm_artifact(**_base_kwargs(), detection_outcome=None)
    empty_art = build_llm_artifact(**_base_kwargs(), detection_outcome=_empty_outcome())

    assert none_art == empty_art


# --------------------------------------------------------------------------- #
# Non-empty outcome wires penalty / misconceptions / composite
# --------------------------------------------------------------------------- #
def test_non_empty_outcome_sets_misconception_penalty():
    outcome = _non_empty_outcome(misconception_penalty=0.27)

    art = build_llm_artifact(**_base_kwargs(), detection_outcome=outcome)

    assert art["scores"]["misconception_penalty"] == pytest.approx(0.27)
    assert art["scores"]["misconception_penalty"] != 0.0


def test_non_empty_outcome_sets_misconceptions_rows():
    outcome = _non_empty_outcome()

    art = build_llm_artifact(**_base_kwargs(), detection_outcome=outcome)

    assert art["misconceptions"] == list(outcome.misconceptions)
    assert art["misconceptions"] != []
    assert isinstance(art["misconceptions"], list)  # never the raw tuple


def test_non_empty_outcome_reduces_composite_via_apply_penalty():
    """composite becomes exactly apply_penalty(composite=<today's value>,
    outcome=outcome) — the detector only ever subtracts/ceilings, never
    recomputes node_coverage/edge_coverage."""
    outcome = _non_empty_outcome(misconception_penalty=0.27, ceiling_applied=False)

    art = build_llm_artifact(**_base_kwargs(), detection_outcome=outcome)

    today_composite = 0.71  # rubric overall.score / 100, pre-penalty
    expected = apply_penalty(composite=today_composite, outcome=outcome)
    assert art["scores"]["composite"] == pytest.approx(expected)
    assert art["scores"]["composite"] < today_composite


def test_ceiling_applied_caps_composite_below_named_band():
    """A ceiling-tripping outcome caps composite at CEILING_COMPOSITE even
    when the raw rubric score would otherwise sit above it."""
    high_rubric = {"overall": {"score": 95}}
    outcome = _non_empty_outcome(misconception_penalty=0.05, ceiling_applied=True)

    art = build_llm_artifact(**_base_kwargs(rubric=high_rubric), detection_outcome=outcome)

    expected = apply_penalty(composite=0.95, outcome=outcome)
    assert art["scores"]["composite"] == pytest.approx(expected)
    assert art["scores"]["composite"] <= 0.84 + 1e-9


def test_ceiling_only_zero_penalty_outcome_still_applies_ceiling():
    """A ceiling-tripping outcome with ZERO penalty and NO keyed rows must
    still flow through ``apply_penalty`` and cap the composite — the
    ``has_detection`` guard is aligned with merge.py's ceiling predicate
    (review LOW finding). Without the ceiling term in the guard this outcome
    would be treated as an empty no-op and the ceiling silently dropped."""
    high_rubric = {"overall": {"score": 95}}
    outcome = MergeOutcome(
        misconception_penalty=0.0,
        misconceptions=(),
        ceiling_applied=True,
        ledger_findings=(),
    )

    art = build_llm_artifact(**_base_kwargs(rubric=high_rubric), detection_outcome=outcome)

    # ceiling honored: composite capped at CEILING_COMPOSITE, not the raw 0.95
    assert art["scores"]["composite"] <= 0.84 + 1e-9
    assert art["scores"]["composite"] != pytest.approx(0.95)


def test_non_empty_outcome_does_not_change_node_or_edge_coverage():
    """The detector only touches misconception_penalty/misconceptions/
    composite — node_coverage and edge_coverage are untouched inputs."""
    outcome = _non_empty_outcome()

    without = build_llm_artifact(**_base_kwargs())
    with_outcome = build_llm_artifact(**_base_kwargs(), detection_outcome=outcome)

    assert with_outcome["scores"]["node_coverage"] == without["scores"]["node_coverage"]
    assert with_outcome["scores"]["edge_coverage"] == without["scores"]["edge_coverage"]


def test_non_empty_outcome_does_not_mutate_node_ledger_or_other_keys():
    """Only scores.misconception_penalty, scores.composite, and top-level
    misconceptions change — everything else is untouched."""
    outcome = _non_empty_outcome()

    without = build_llm_artifact(**_base_kwargs())
    with_outcome = build_llm_artifact(**_base_kwargs(), detection_outcome=outcome)

    assert with_outcome["node_ledger"] == without["node_ledger"]
    assert with_outcome["edge_ledger"] == without["edge_ledger"]
    assert with_outcome["grader_used"] == without["grader_used"]
    assert with_outcome["abstention"] == without["abstention"]
    assert with_outcome["clarification_trace"] == without["clarification_trace"]


def test_outcome_input_object_is_not_mutated():
    """Immutability: the caller's MergeOutcome/tuple is never touched — the
    artifact stores a NEW list built from it, not a mutated version of it."""
    outcome = _non_empty_outcome()
    original_misconceptions = outcome.misconceptions
    original_penalty = outcome.misconception_penalty

    build_llm_artifact(**_base_kwargs(), detection_outcome=outcome)

    assert outcome.misconceptions == original_misconceptions
    assert outcome.misconception_penalty == original_penalty
