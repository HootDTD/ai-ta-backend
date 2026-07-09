"""RED->GREEN tests for the severity-weighted merge stage (T7).

Frozen contract: ``docs/_archive/plans/2026-07-08-apollo-misconception-detector-plan.md``
section 5.6 (T7 assertions), amended by A5 (canonical_key is the BARE
``misc.<code>``; an ``unkeyed:*`` finding is docked/penalized but NEVER emitted
as a keyed ledger row).

``merge_detections`` takes the GATE-cleared findings (each already docked:
``verdict="misconception"``, ``corroborated=True``) plus a per-concept
centrality map, and produces the ``MergeOutcome``:

  * ``misconception_penalty`` — ``min(clamp, Sum severity_i)`` where
    ``severity_i = centrality.get(concept_key, CENTRALITY_W_MIN) * confidence_i``
    over ALL docked findings (keyed AND unkeyed);
  * ``ceiling_applied`` — True iff any docked finding sits on a maximally
    central node (derived threshold, not hand-authored);
  * ``misconceptions`` — one row per docked *bank-keyed* finding only
    (``{canonical_key: bare 'misc.<code>', evidence_span, confidence,
    opposes: None}``); unkeyed findings are excluded from this list;
  * ``ledger_findings`` — the docked findings with ``severity`` now filled in
    (new immutable copies), for the emergent store.

Pure module: no IO, no LLM, no DB. Every assertion below is offline.
"""

from __future__ import annotations

import dataclasses

import pytest

from apollo.overseer.misconception_detector.config import (
    CENTRALITY_W_MIN,
    SEVERITY_CLAMP,
)
from apollo.overseer.misconception_detector.merge import merge_detections
from apollo.overseer.misconception_detector.types import (
    ConceptFinding,
    MergeOutcome,
)


def _docked(
    *,
    concept_key: str = "concept.gdp_identity",
    confidence: float = 0.9,
    severity: float = 0.0,
    evidence_span: str = "student said X",
    signature: str = "misc.includes_transfers",
    source: str = "judge",
    ceiling_eligible: bool = False,
) -> ConceptFinding:
    """A gate-cleared (docked) finding — the shape merge_detections consumes."""
    return ConceptFinding(
        concept_key=concept_key,
        verdict="misconception",
        confidence=confidence,
        severity=severity,
        evidence_span=evidence_span,
        signature=signature,
        source=source,
        corroborated=True,
        ceiling_eligible=ceiling_eligible,
    )


# --------------------------------------------------------------------------- #
# severity = centrality * confidence
# --------------------------------------------------------------------------- #
def test_severity_is_centrality_times_confidence():
    """severity_i = centrality[concept] * confidence_i, filled on ledger_findings.

    Chosen so the single severity (0.5 * 0.5 = 0.25) stays UNDER SEVERITY_CLAMP
    (0.30), isolating the severity=centrality*confidence law from the clamp.
    """
    finding = _docked(concept_key="concept.a", confidence=0.5)
    centrality = {"concept.a": 0.5}

    outcome = merge_detections((finding,), centrality=centrality)

    assert len(outcome.ledger_findings) == 1
    ledger = outcome.ledger_findings[0]
    assert ledger.severity == pytest.approx(0.5 * 0.5)
    # Single unclamped severity flows straight through to the penalty.
    assert outcome.misconception_penalty == pytest.approx(0.25)


def test_missing_centrality_key_falls_back_to_centrality_w_min():
    """A concept absent from the centrality map weights at CENTRALITY_W_MIN."""
    finding = _docked(concept_key="concept.orphan", confidence=1.0)

    outcome = merge_detections((finding,), centrality={})

    assert outcome.ledger_findings[0].severity == pytest.approx(CENTRALITY_W_MIN)
    assert outcome.misconception_penalty == pytest.approx(CENTRALITY_W_MIN)


# --------------------------------------------------------------------------- #
# penalty = min(clamp, Sum severity)
# --------------------------------------------------------------------------- #
def test_penalty_is_sum_of_severities():
    a = _docked(concept_key="concept.a", confidence=0.5, signature="misc.a")
    b = _docked(concept_key="concept.b", confidence=0.5, signature="misc.b")
    centrality = {"concept.a": 0.4, "concept.b": 0.2}

    outcome = merge_detections((a, b), centrality=centrality)

    # 0.4*0.5 + 0.2*0.5 = 0.20 + 0.10 = 0.30 (== SEVERITY_CLAMP but exactly at it)
    assert outcome.misconception_penalty == pytest.approx(0.30)


def test_penalty_clamped_at_severity_clamp():
    """Sum severity exceeding the clamp is capped at SEVERITY_CLAMP."""
    # Three maximally-central, high-confidence docked findings would sum to
    # 3 * (1.0 * 1.0) = 3.0 without the clamp.
    findings = tuple(
        _docked(concept_key=f"concept.{i}", confidence=1.0, signature=f"misc.{i}")
        for i in range(3)
    )
    centrality = {f"concept.{i}": 1.0 for i in range(3)}

    outcome = merge_detections(findings, centrality=centrality)

    assert outcome.misconception_penalty == pytest.approx(SEVERITY_CLAMP)


def test_penalty_clamp_is_parameterizable():
    finding = _docked(concept_key="concept.a", confidence=1.0)
    centrality = {"concept.a": 1.0}

    outcome = merge_detections((finding,), centrality=centrality, clamp=0.10)

    assert outcome.misconception_penalty == pytest.approx(0.10)


def test_empty_input_yields_empty_outcome():
    outcome = merge_detections((), centrality={"concept.a": 1.0})

    assert outcome == MergeOutcome(
        misconception_penalty=0.0,
        misconceptions=(),
        ceiling_applied=False,
        ledger_findings=(),
    )


# --------------------------------------------------------------------------- #
# ceiling_applied: central docked finding -> True; peripheral-only -> False
# --------------------------------------------------------------------------- #
def test_central_docked_finding_sets_ceiling_applied():
    """A docked finding on the most-central node trips the anti-dilution
    ceiling — but ONLY when it is ceiling_eligible (A12): this finding models
    a corroborated/deterministic central dock (row 1/2/3 of the gate
    truth-table), so ceiling_eligible=True here is correct, not a weakening
    of the predicate."""
    central = _docked(
        concept_key="concept.central",
        confidence=0.6,
        signature="misc.c",
        ceiling_eligible=True,
    )
    peripheral = _docked(concept_key="concept.leaf", confidence=0.6, signature="misc.p")
    centrality = {"concept.central": 1.0, "concept.leaf": CENTRALITY_W_MIN}

    outcome = merge_detections((central, peripheral), centrality=centrality)

    assert outcome.ceiling_applied is True


def test_peripheral_only_docked_finding_does_not_set_ceiling():
    """When no docked finding sits on a maximally-central node, no ceiling."""
    # The map contains a highly central node, but NO docked finding attaches to
    # it — the only docked finding is on a peripheral node.
    peripheral = _docked(concept_key="concept.leaf", confidence=0.9, signature="misc.p")
    centrality = {"concept.central": 1.0, "concept.leaf": CENTRALITY_W_MIN}

    outcome = merge_detections((peripheral,), centrality=centrality)

    assert outcome.ceiling_applied is False


def test_ceiling_uses_max_centrality_present_in_map():
    """'Central' is derived from the map's max, not a hand-authored constant.

    Here the single docked finding IS the most central node present, so even
    though its absolute centrality (0.5) is modest, it trips the ceiling
    because nothing is more central AND it is ceiling_eligible (models a
    corroborated/deterministic central dock, A12)."""
    finding = _docked(concept_key="concept.a", confidence=0.5, ceiling_eligible=True)
    centrality = {"concept.a": 0.5, "concept.b": 0.4}

    outcome = merge_detections((finding,), centrality=centrality)

    assert outcome.ceiling_applied is True


# --------------------------------------------------------------------------- #
# A5: misconceptions[] rows carry the BARE misc.<code>; unkeyed excluded
# --------------------------------------------------------------------------- #
def test_keyed_row_canonical_key_is_bare_misc_code():
    """A5: canonical_key is exactly the finding's misc.<code> signature."""
    finding = _docked(
        concept_key="concept.gdp",
        confidence=0.9,
        signature="misc.includes_transfers",
        evidence_span="transfers are part of GDP",
    )

    outcome = merge_detections((finding,), centrality={"concept.gdp": 1.0})

    assert len(outcome.misconceptions) == 1
    row = outcome.misconceptions[0]
    assert row["canonical_key"] == "misc.includes_transfers"
    assert row["evidence_span"] == "transfers are part of GDP"
    assert row["confidence"] == pytest.approx(0.9)
    assert row["opposes"] is None


def test_canonical_key_is_never_double_prefixed():
    """A5: the bare code goes in verbatim — no re-prefixing of an already-keyed sig."""
    finding = _docked(signature="misc.sign_flip")

    outcome = merge_detections((finding,), centrality={"concept.gdp_identity": 1.0})

    assert outcome.misconceptions[0]["canonical_key"] == "misc.sign_flip"
    # It must NOT become "misc.misc.sign_flip" or carry the unkeyed prefix.
    assert not outcome.misconceptions[0]["canonical_key"].startswith("misc.misc")


def test_unkeyed_finding_is_penalized_but_not_emitted_as_keyed_row():
    """A5: an unkeyed:* docked finding counts toward penalty/ceiling but is
    NOT emitted as a keyed misconceptions[] row (no canonical_key row for it).
    ceiling_eligible is ORTHOGONAL to keying (A12/§4.6 note): this finding
    models a deterministic/corroborated dock whose signature happens to be
    unkeyed:* (e.g. a sympy_veto or a bank-corroborated-but-unvalidated-code
    case), so ceiling_eligible=True is set explicitly — only the KEYED-ROW
    emission is gated on the misc.<code> signature, not the ceiling."""
    unkeyed = _docked(
        concept_key="concept.unattributed",
        confidence=1.0,
        signature="unkeyed:42",
        ceiling_eligible=True,
    )
    centrality = {"concept.unattributed": 1.0}

    outcome = merge_detections((unkeyed,), centrality=centrality)

    # Penalty and ceiling DO reflect the unkeyed finding.
    assert outcome.misconception_penalty == pytest.approx(1.0 * 1.0 - (1.0 - SEVERITY_CLAMP))  # clamped
    assert outcome.ceiling_applied is True
    # But no keyed row is emitted for an unkeyed:* signature.
    assert all(
        row.get("canonical_key") != "unkeyed:42" for row in outcome.misconceptions
    )
    assert not any(
        (row.get("canonical_key") or "").startswith("unkeyed:")
        for row in outcome.misconceptions
    )
    # It is still carried in the ledger_findings (gate-cleared docked set).
    assert any(f.signature == "unkeyed:42" for f in outcome.ledger_findings)


def test_mixed_keyed_and_unkeyed_only_keyed_becomes_a_row():
    # Low centrality/confidence keeps the summed severity UNDER SEVERITY_CLAMP
    # (0.30) so this test asserts the raw contribution law, not the clamp:
    # 0.4*0.5 + 0.4*0.5 = 0.20 + 0.20 = 0.40 would clamp, so use 0.3 weights:
    # 0.3*0.5 + 0.3*0.5 = 0.15 + 0.15 = 0.30 (exactly at the clamp, unclamped).
    keyed = _docked(concept_key="concept.a", confidence=0.5, signature="misc.a")
    unkeyed = _docked(concept_key="concept.b", confidence=0.5, signature="unkeyed:7")
    centrality = {"concept.a": 0.3, "concept.b": 0.3}

    outcome = merge_detections((keyed, unkeyed), centrality=centrality)

    keys = [row["canonical_key"] for row in outcome.misconceptions]
    assert keys == ["misc.a"]
    # Both keyed AND unkeyed contribute to the penalty and the ledger.
    assert outcome.misconception_penalty == pytest.approx(0.3 * 0.5 + 0.3 * 0.5)
    assert len(outcome.ledger_findings) == 2


# --------------------------------------------------------------------------- #
# ledger_findings = docked set (with severity filled)
# --------------------------------------------------------------------------- #
def test_ledger_findings_is_the_docked_set_with_severity_filled():
    a = _docked(concept_key="concept.a", confidence=0.8, signature="misc.a")
    b = _docked(concept_key="concept.b", confidence=0.4, signature="unkeyed:9")
    centrality = {"concept.a": 0.5, "concept.b": 1.0}

    outcome = merge_detections((a, b), centrality=centrality)

    assert len(outcome.ledger_findings) == 2
    by_key = {f.concept_key: f for f in outcome.ledger_findings}
    assert by_key["concept.a"].severity == pytest.approx(0.5 * 0.8)
    assert by_key["concept.b"].severity == pytest.approx(1.0 * 0.4)
    # Docked status preserved; nothing downgraded.
    assert all(f.verdict == "misconception" and f.corroborated for f in outcome.ledger_findings)


# --------------------------------------------------------------------------- #
# Immutability
# --------------------------------------------------------------------------- #
def test_input_findings_are_not_mutated():
    """severity is filled via NEW copies; the input finding is untouched."""
    finding = _docked(concept_key="concept.a", confidence=0.8, severity=0.0)
    original_severity = finding.severity

    merge_detections((finding,), centrality={"concept.a": 0.5})

    assert finding.severity == original_severity  # input object unchanged


def test_outcome_is_frozen_mergeoutcome():
    finding = _docked()
    outcome = merge_detections((finding,), centrality={"concept.gdp_identity": 1.0})

    assert isinstance(outcome, MergeOutcome)
    with pytest.raises(dataclasses.FrozenInstanceError):
        outcome.misconception_penalty = 0.0  # type: ignore[misc]


def test_misconceptions_tuple_is_immutable_tuple_of_dicts():
    finding = _docked(signature="misc.x")
    outcome = merge_detections((finding,), centrality={"concept.gdp_identity": 1.0})

    assert isinstance(outcome.misconceptions, tuple)
    assert isinstance(outcome.ledger_findings, tuple)


# --------------------------------------------------------------------------- #
# A12: ceiling_applied reads ceiling_eligible, not centrality alone (the
# severity gradient / anti-dilution ceiling policy). docs/_archive/specs/
# 2026-07-08-apollo-misconception-corroboration-redesign.md §4.7/§8.
# --------------------------------------------------------------------------- #
def test_lone_judge_dock_subtracts_but_no_ceiling():
    """A ceiling_eligible=False dock (a lone-judge penalty-only dock, gate
    row 5) on the MAX-central concept still subtracts (the severity
    gradient, L2) but must NOT trip the ceiling."""
    finding = _docked(
        concept_key="concept.central",
        confidence=0.9,
        signature="misc.c",
        ceiling_eligible=False,
    )
    centrality = {"concept.central": 1.0}

    outcome = merge_detections((finding,), centrality=centrality)

    assert outcome.ceiling_applied is False
    assert outcome.misconception_penalty > 0.0


def test_bank_corroborated_dock_trips_ceiling():
    """A ceiling_eligible=True dock (gate row 1/2/3) on the max-central
    concept DOES trip the ceiling."""
    finding = _docked(
        concept_key="concept.central",
        confidence=0.9,
        signature="misc.c",
        ceiling_eligible=True,
    )
    centrality = {"concept.central": 1.0}

    outcome = merge_detections((finding,), centrality=centrality)

    assert outcome.ceiling_applied is True


def test_penalty_sums_all_docks_including_lone_judge():
    """penalty math is unchanged by the eligibility gate: a mix of an
    eligible and an ineligible dock both contribute to the summed/clamped
    penalty (L2 — every dock subtracts, only ceiling eligibility differs)."""
    eligible = _docked(
        concept_key="concept.a",
        confidence=0.5,
        signature="misc.a",
        ceiling_eligible=True,
    )
    ineligible = _docked(
        concept_key="concept.b",
        confidence=0.5,
        signature="misc.b",
        ceiling_eligible=False,
    )
    centrality = {"concept.a": 0.3, "concept.b": 0.3}

    outcome = merge_detections((eligible, ineligible), centrality=centrality)

    assert outcome.misconception_penalty == pytest.approx(0.3 * 0.5 + 0.3 * 0.5)


def test_keyed_rows_emitted_for_now_keyable_judge_docks():
    """A judge dock with a validated misc.<code> signature (now possible
    after the judge-keying redesign, A11) appears in outcome.misconceptions
    with the bare canonical_key."""
    finding = _docked(
        concept_key="concept.gdp",
        confidence=0.95,
        signature="misc.includes_transfers",
        source="judge",
        ceiling_eligible=False,
    )

    outcome = merge_detections((finding,), centrality={"concept.gdp": 1.0})

    assert len(outcome.misconceptions) == 1
    assert outcome.misconceptions[0]["canonical_key"] == "misc.includes_transfers"


def test_a13_regression_node_keyed_dock_is_central():
    """A ceiling_eligible=True dock carrying a node_id concept_key PRESENT in
    the centrality map at max -> central -> ceiling trips (mirrors what the
    gate hands merge for a bank-corroborated dock, A13)."""
    finding = _docked(
        concept_key="node.demand_curve",
        confidence=0.9,
        signature="misc.includes_transfers",
        ceiling_eligible=True,
    )
    centrality = {"node.demand_curve": 1.0}

    outcome = merge_detections((finding,), centrality=centrality)

    assert outcome.ceiling_applied is True


def test_a13_regression_str_concept_id_key_floors_not_central():
    """The SAME ceiling_eligible=True dock but carrying a str(concept_id) key
    ABSENT from the centrality map floors to CENTRALITY_W_MIN, is NOT
    maximally central -> ceiling_applied is False. Documents exactly why the
    docked representative must be node_id-keyed (A13, spec §4.7 load-bearing
    dependency): a bank finding's str(concept_id) key would silently
    under-report the ceiling if it were ever the docked representative."""
    finding = _docked(
        concept_key="42",  # str(concept_id) — absent from the centrality map
        confidence=0.9,
        signature="misc.includes_transfers",
        ceiling_eligible=True,
    )
    # The centrality map is keyed by node_id only (mirrors production
    # centrality.py); "42" is never a key in it, so it floors to
    # CENTRALITY_W_MIN, which is below this map's max (1.0).
    centrality = {"node.demand_curve": 1.0}

    outcome = merge_detections((finding,), centrality=centrality)

    assert outcome.ceiling_applied is False
