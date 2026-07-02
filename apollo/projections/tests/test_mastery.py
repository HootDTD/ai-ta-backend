"""Campaign-plan Task B2 — pure-math + pure-helper tests for
``apollo.projections.mastery``. DB-backed behavior (entity resolution,
idempotence, upsert) is covered by
``tests/database/test_artifact_mastery_postgres.py``."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from apollo.projections.mastery import (
    EVENT_KIND,
    _belief_for,
    _ledger_entity_keys,
    _normalization_confidence,
    ewma_alpha,
    ewma_mastery,
)

pytestmark = pytest.mark.unit


def _artifact(**overrides) -> SimpleNamespace:
    base = dict(
        concept_id=7,
        attempt_id=1,
        user_id="u1",
        search_space_id=1,
        scores={"composite": 0.6},
        abstention=None,
        node_ledger=[],
        created_at=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


# ---------------------------------------------------------------------------
# ewma_alpha (env override + malformed fallback), mirrors composite.load_weights
# ---------------------------------------------------------------------------


def test_ewma_alpha_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("APOLLO_MASTERY_EWMA_ALPHA", raising=False)
    assert ewma_alpha() == 0.3


def test_ewma_alpha_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APOLLO_MASTERY_EWMA_ALPHA", "0.5")
    assert ewma_alpha() == 0.5


def test_ewma_alpha_malformed_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APOLLO_MASTERY_EWMA_ALPHA", "not-a-float")
    assert ewma_alpha() == 0.3


# ---------------------------------------------------------------------------
# ewma_mastery — pure math + cold start
# ---------------------------------------------------------------------------


def test_ewma_mastery_blends_prior_and_composite() -> None:
    assert ewma_mastery(composite=1.0, prior_mastery=0.0, alpha=0.3) == pytest.approx(0.3)
    assert ewma_mastery(composite=0.0, prior_mastery=1.0, alpha=0.3) == pytest.approx(0.7)


@pytest.mark.parametrize("alpha", [0.0, 0.3, 0.5, 1.0])
def test_ewma_mastery_cold_start_equals_composite(alpha: float) -> None:
    """Cold start: prior_mastery == composite (no prior LearnerState row) means
    the EWMA collapses to the composite itself, for ANY alpha."""
    composite = 0.732
    assert ewma_mastery(composite=composite, prior_mastery=composite, alpha=alpha) == pytest.approx(
        composite
    )


def test_ewma_mastery_clamps_to_unit_interval() -> None:
    assert ewma_mastery(composite=2.0, prior_mastery=2.0, alpha=0.3) == 1.0
    assert ewma_mastery(composite=-1.0, prior_mastery=-1.0, alpha=0.3) == 0.0


# ---------------------------------------------------------------------------
# _belief_for — the (p_misc, p_shaky, p_mastered) simplex encoding
# ---------------------------------------------------------------------------


def test_belief_for_mastery_of_roundtrip() -> None:
    from apollo.learner_model.belief import mastery_of

    for m in (0.0, 0.25, 0.5, 0.75, 1.0):
        belief = _belief_for(m)
        assert belief[1] == 0.0
        assert mastery_of(tuple(belief)) == pytest.approx(m)


def test_belief_for_clamps_out_of_range() -> None:
    assert _belief_for(1.5) == [0.0, 0.0, 1.0]
    assert _belief_for(-0.5) == [1.0, 0.0, 0.0]


# ---------------------------------------------------------------------------
# _ledger_entity_keys — credited/misconception only, deduped, in order
# ---------------------------------------------------------------------------


def test_ledger_entity_keys_filters_and_dedupes() -> None:
    artifact = _artifact(
        node_ledger=[
            {"canonical_key": "eq.bernoulli", "status": "credited"},
            {"canonical_key": "stu_node_1", "status": "unresolved"},
            {"canonical_key": "misc.reversal", "status": "misconception"},
            {"canonical_key": "eq.bernoulli", "status": "credited"},  # dup
            {"canonical_key": None, "status": "credited"},  # no key -> skipped
        ]
    )
    assert _ledger_entity_keys(artifact) == ["eq.bernoulli", "misc.reversal"]


def test_ledger_entity_keys_empty_ledger() -> None:
    assert _ledger_entity_keys(_artifact(node_ledger=[])) == []


# ---------------------------------------------------------------------------
# _normalization_confidence — abstention block readout with default
# ---------------------------------------------------------------------------


def test_normalization_confidence_present() -> None:
    artifact = _artifact(abstention={"normalization_confidence": 0.82})
    assert _normalization_confidence(artifact) == pytest.approx(0.82)


def test_normalization_confidence_none_abstention_defaults_to_full_confidence() -> None:
    assert _normalization_confidence(_artifact(abstention=None)) == 1.0


def test_normalization_confidence_missing_key_defaults_to_full_confidence() -> None:
    assert _normalization_confidence(_artifact(abstention={})) == 1.0


def test_normalization_confidence_explicit_none_value_defaults_to_full_confidence() -> None:
    artifact = _artifact(abstention={"normalization_confidence": None})
    assert _normalization_confidence(artifact) == 1.0


def test_event_kind_is_distinct_from_wu5a_kinds() -> None:
    from apollo.persistence.models import MASTERY_EVENT_KINDS

    assert EVENT_KIND not in MASTERY_EVENT_KINDS
