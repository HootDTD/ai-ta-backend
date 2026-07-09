"""RED tests for the misconception-detector value objects (T1).

Frozen contract: ``docs/_archive/plans/2026-07-08-apollo-misconception-detector-plan.md``
section 2, amended by A1/A5 in the task prompt.
"""

from __future__ import annotations

import dataclasses

import pytest

from apollo.overseer.misconception_detector.types import (
    ConceptFinding,
    DetectionResult,
    JudgeConceptInput,
    JudgeRaw,
    MergeOutcome,
)


def _finding(**overrides) -> ConceptFinding:
    base = dict(
        concept_key="gdp_identity",
        verdict="misconception",
        confidence=0.9,
        severity=0.0,
        evidence_span="GDP includes transfers",
        signature="misc.includes_transfers",
        source="bank_pattern",
        corroborated=False,
    )
    base.update(overrides)
    return ConceptFinding(**base)


class TestConceptFinding:
    def test_constructs_with_all_fields(self):
        finding = _finding()
        assert finding.concept_key == "gdp_identity"
        assert finding.verdict == "misconception"
        assert finding.confidence == 0.9
        assert finding.severity == 0.0
        assert finding.evidence_span == "GDP includes transfers"
        assert finding.signature == "misc.includes_transfers"
        assert finding.source == "bank_pattern"
        assert finding.corroborated is False

    def test_is_frozen_raises_on_mutation(self):
        finding = _finding()
        with pytest.raises(dataclasses.FrozenInstanceError):
            finding.confidence = 0.1  # type: ignore[misc]

    def test_unkeyed_signature_shape(self):
        finding = _finding(signature="unkeyed:concept_42")
        assert finding.signature == "unkeyed:concept_42"


class TestDetectionResult:
    def test_empty_tuple_is_empty(self):
        result = DetectionResult(())
        assert result.is_empty is True

    def test_default_constructor_is_empty(self):
        result = DetectionResult()
        assert result.is_empty is True
        assert result.per_concept == ()

    def test_non_empty_is_not_empty(self):
        result = DetectionResult((_finding(),))
        assert result.is_empty is False

    def test_per_concept_is_a_tuple(self):
        result = DetectionResult((_finding(), _finding(concept_key="other")))
        assert isinstance(result.per_concept, tuple)

    def test_is_frozen(self):
        result = DetectionResult((_finding(),))
        with pytest.raises(dataclasses.FrozenInstanceError):
            result.per_concept = ()  # type: ignore[misc]


class TestMergeOutcome:
    def test_constructs_with_all_fields(self):
        finding = _finding(corroborated=True)
        outcome = MergeOutcome(
            misconception_penalty=0.12,
            misconceptions=({"canonical_key": "misc.includes_transfers", "evidence_span": "x", "confidence": 0.9, "opposes": None},),
            ceiling_applied=True,
            ledger_findings=(finding,),
        )
        assert outcome.misconception_penalty == 0.12
        assert isinstance(outcome.misconceptions, tuple)
        assert outcome.misconceptions[0]["canonical_key"] == "misc.includes_transfers"
        assert outcome.ceiling_applied is True
        assert isinstance(outcome.ledger_findings, tuple)
        assert outcome.ledger_findings[0] is finding

    def test_is_frozen(self):
        outcome = MergeOutcome(
            misconception_penalty=0.0,
            misconceptions=(),
            ceiling_applied=False,
            ledger_findings=(),
        )
        with pytest.raises(dataclasses.FrozenInstanceError):
            outcome.misconception_penalty = 1.0  # type: ignore[misc]


class TestJudgeRaw:
    def test_constructs_with_token_prob(self):
        raw = JudgeRaw(content='{"concepts": []}', verdict_token_prob=0.95)
        assert raw.content == '{"concepts": []}'
        assert raw.verdict_token_prob == 0.95

    def test_constructs_with_none_token_prob(self):
        raw = JudgeRaw(content='{"concepts": []}', verdict_token_prob=None)
        assert raw.verdict_token_prob is None

    def test_is_frozen(self):
        raw = JudgeRaw(content="{}", verdict_token_prob=None)
        with pytest.raises(dataclasses.FrozenInstanceError):
            raw.content = "{}"  # type: ignore[misc]


class TestJudgeConceptInput:
    def test_constructs_with_bank_entries_tuple(self):
        jci = JudgeConceptInput(
            concept_key="gdp_identity",
            correct_belief="GDP excludes transfer payments.",
            bank_entries=(),
        )
        assert jci.concept_key == "gdp_identity"
        assert jci.correct_belief == "GDP excludes transfer payments."
        assert isinstance(jci.bank_entries, tuple)

    def test_is_frozen(self):
        jci = JudgeConceptInput(concept_key="k", correct_belief="c", bank_entries=())
        with pytest.raises(dataclasses.FrozenInstanceError):
            jci.concept_key = "other"  # type: ignore[misc]


class TestConceptFindingCorroborationFields:
    """RED tests for the corroboration/keying redesign (A10-A12):
    ``docs/_archive/specs/2026-07-08-apollo-misconception-corroboration-redesign.md``
    §4.1, §4.6. Every existing tier constructor must keep compiling with ONLY
    the pre-existing required args — the three new fields are all defaulted.
    """

    def test_conceptfinding_new_fields_default(self):
        """Constructing with only the pre-existing required args must still
        work, and the three new fields must default to the documented values
        (guards every existing tier constructor keeps compiling)."""
        finding = _finding()
        assert finding.bank_code is None
        assert finding.bank_match_above_floor is True
        assert finding.ceiling_eligible is False

    def test_bank_code_signature_invariant_keyed(self):
        """bank_code is not None <=> signature == f'misc.{bank_code}'."""
        finding = _finding(
            bank_code="includes_transfers",
            signature="misc.includes_transfers",
        )
        assert finding.bank_code is not None
        assert finding.signature == f"misc.{finding.bank_code}"

    def test_bank_code_none_unkeyed_signature(self):
        """bank_code=None places no constraint on signature (LHS False)."""
        finding = _finding(bank_code=None, signature="unkeyed:node.x")
        assert finding.bank_code is None
        assert finding.signature == "unkeyed:node.x"

    def test_new_fields_frozen_mutation_raises(self):
        finding = _finding()
        with pytest.raises(dataclasses.FrozenInstanceError):
            finding.ceiling_eligible = True  # type: ignore[misc]
        with pytest.raises(dataclasses.FrozenInstanceError):
            finding.bank_code = "x"  # type: ignore[misc]
        with pytest.raises(dataclasses.FrozenInstanceError):
            finding.bank_match_above_floor = False  # type: ignore[misc]
