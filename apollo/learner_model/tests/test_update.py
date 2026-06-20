"""WU-5A1 §3 — orchestration tests for ``update.apply_event`` / ``event_to_row_specs``.

Pure imports — no DB, no LLM, no Neo4j, no network. The end-to-end pins (the
absorbing-zero rescue and the misconception two-step) drive ``apply_event`` only
through its PUBLIC surface; every expected number is the hand-computed golden
constant (independently reproduced), never the function's own output. The
q-no-double-count test re-implements the posterior formula INLINE (it does NOT
call ``apply_event`` to produce its own expectation).
"""

from __future__ import annotations

import dataclasses
import math
from datetime import UTC, datetime

import pytest

from apollo.grading.event_model import LearnerEvent, LearnerEventKind
from apollo.learner_model.belief import (
    COLD_START_PRIOR,
    bayes_update,
    confidence_of,
    damp,
    likelihood_for_event,
    mastery_of,
)
from apollo.learner_model.state_model import (
    BeliefUpdate,
    LearnerStateRowSpec,
    MasteryEventRowSpec,
)
from apollo.learner_model.update import apply_event, event_to_row_specs

_DONE_TS = datetime(2026, 6, 8, tzinfo=UTC)
_PRIOR_TS = datetime(2026, 6, 1, tzinfo=UTC)


def _covered(score: float, *, confidence: float | None = None) -> LearnerEvent:
    return LearnerEvent(
        canonical_key="k.x",
        event_kind=LearnerEventKind.COVERED,
        score=score,
        confidence=confidence,
    )


def _misconception(code: str | None = "misc.density_ignored") -> LearnerEvent:
    return LearnerEvent(
        canonical_key="k.x",
        event_kind=LearnerEventKind.MISCONCEPTION,
        misconception_code=code,
    )


def test_apply_event_q_no_double_count():
    """``q = parser·grader`` ONLY. ``event.confidence`` (resolution confidence)
    is a red herring that must NOT enter q. Expected posterior is hand-computed
    INLINE as normalize(prior ⊙ damp(L(0.8), 0.855))."""
    event = _covered(0.8, confidence=0.5)  # confidence=0.5 must be ignored
    update = apply_event(
        event,
        prior_belief=COLD_START_PRIOR,
        prior_last_evidence_at=None,
        parser_confidence=0.9,
        grader_confidence=0.95,
        done_ts=_DONE_TS,
    )
    # Independent hand-computation of the expected posterior (q = 0.9*0.95).
    q = 0.9 * 0.95
    expected = bayes_update(COLD_START_PRIOR, damp(likelihood_for_event(event), q))
    assert q == 0.855  # event.confidence (0.5) is NOT in q
    for got, want in zip(update.posterior_belief, expected, strict=True):
        assert math.isclose(got, want, abs_tol=1e-4)
    # And the exact golden 6-dp vector.
    for got, want in zip(update.posterior_belief, (0.079491, 0.648885, 0.271624), strict=True):
        assert math.isclose(got, want, abs_tol=1e-4)


def test_apply_event_cold_start_when_prior_none():
    update = apply_event(
        _covered(0.5),
        prior_belief=None,
        prior_last_evidence_at=None,
        parser_confidence=1.0,
        grader_confidence=1.0,
        done_ts=_DONE_TS,
    )
    assert update.prior_belief == COLD_START_PRIOR


def test_apply_event_populates_belief_update():
    update = apply_event(
        _covered(0.7),
        prior_belief=COLD_START_PRIOR,
        prior_last_evidence_at=_PRIOR_TS,
        parser_confidence=0.9,
        grader_confidence=0.8,
        done_ts=_DONE_TS,
    )
    assert isinstance(update, BeliefUpdate)
    assert math.isclose(sum(update.posterior_belief), 1.0, abs_tol=1e-9)
    assert update.mastery_after == mastery_of(update.posterior_belief)
    assert update.confidence_after == confidence_of(update.posterior_belief)
    assert update.parser_confidence == 0.9
    assert update.grader_confidence == 0.8
    assert update.dt_days_since_last == 7  # 2026-06-01 -> 2026-06-08


def test_absorbing_zero_rescue_perfect_then_misconception():
    """Perfect covered (s=1, q=1) from cold-start leaves p_misc > 0 (the floor);
    a SUBSEQUENT misconception then MOVES p_misc up — proving the floored path
    (without the floor both p_misc paths would be 0.0 forever)."""
    first = apply_event(
        _covered(1.0),
        prior_belief=COLD_START_PRIOR,
        prior_last_evidence_at=None,
        parser_confidence=1.0,
        grader_confidence=1.0,
        done_ts=_DONE_TS,
    )
    for got, want in zip(first.posterior_belief, (0.0185, 0.0556, 0.9259), strict=True):
        assert math.isclose(got, want, abs_tol=1e-3)
    assert first.posterior_belief[0] > 0
    second = apply_event(
        _misconception(),
        prior_belief=first.posterior_belief,
        prior_last_evidence_at=None,
        parser_confidence=1.0,
        grader_confidence=1.0,
        done_ts=_DONE_TS,
    )
    assert math.isclose(second.posterior_belief[0], 0.1875, abs_tol=1e-3)
    assert second.posterior_belief[0] > first.posterior_belief[0]  # moved UP


def test_misconception_two_step():
    """First misconception from cold-start: p_misc≈0.4839 (argmax but UNFLAGGED).
    Second from that posterior: p_misc≈0.7475 (FLAGGED -> returns the code)."""
    first = apply_event(
        _misconception(),
        prior_belief=COLD_START_PRIOR,
        prior_last_evidence_at=None,
        parser_confidence=1.0,
        grader_confidence=1.0,
        done_ts=_DONE_TS,
    )
    assert math.isclose(first.posterior_belief[0], 0.4839, abs_tol=1e-3)
    assert first.misconception_code is None  # argmax but < 0.5 threshold
    second = apply_event(
        _misconception(),
        prior_belief=first.posterior_belief,
        prior_last_evidence_at=None,
        parser_confidence=1.0,
        grader_confidence=1.0,
        done_ts=_DONE_TS,
    )
    assert math.isclose(second.posterior_belief[0], 0.7475, abs_tol=1e-3)
    assert second.misconception_code == "misc.density_ignored"


def test_dt_days_since_last_computed():
    update = apply_event(
        _covered(0.5),
        prior_belief=COLD_START_PRIOR,
        prior_last_evidence_at=_PRIOR_TS,
        parser_confidence=1.0,
        grader_confidence=1.0,
        done_ts=_DONE_TS,
    )
    assert update.dt_days_since_last == 7


def test_dt_days_none_when_no_prior_anchor():
    update = apply_event(
        _covered(0.5),
        prior_belief=COLD_START_PRIOR,
        prior_last_evidence_at=None,
        parser_confidence=1.0,
        grader_confidence=1.0,
        done_ts=_DONE_TS,
    )
    assert update.dt_days_since_last is None


def test_no_decay_multiply_in_v1():
    """Δt is RECORDED but NOT applied — decay is WU-5B. Two calls identical except
    ``done_ts`` 30 days apart (same None anchor) yield IDENTICAL posteriors."""
    near = apply_event(
        _covered(0.6),
        prior_belief=COLD_START_PRIOR,
        prior_last_evidence_at=None,
        parser_confidence=0.9,
        grader_confidence=0.9,
        done_ts=datetime(2026, 6, 8, tzinfo=UTC),
    )
    far = apply_event(
        _covered(0.6),
        prior_belief=COLD_START_PRIOR,
        prior_last_evidence_at=None,
        parser_confidence=0.9,
        grader_confidence=0.9,
        done_ts=datetime(2026, 7, 8, tzinfo=UTC),
    )
    assert near.posterior_belief == far.posterior_belief


def test_event_to_row_specs_maps_distinct_fields():
    """Distinct field values so a swapped mapping is caught (mirrors
    test_persistence_specs.py). Identity kwargs pass through; defaults None."""
    event = LearnerEvent(
        canonical_key="k.density",
        event_kind=LearnerEventKind.COVERED,
        score=0.8,
        misconception_code=None,
        evidence_node_ids=("n1", "n2"),
        reference_step_id="ref.7",
    )
    update = BeliefUpdate(
        prior_belief=(0.20, 0.60, 0.20),
        posterior_belief=(0.11, 0.52, 0.37),
        mastery_after=0.63,
        confidence_after=0.41,
        misconception_code="misc.code",
        parser_confidence=0.91,
        grader_confidence=0.83,
        dt_days_since_last=5.0,
    )
    mastery_spec, state_spec = event_to_row_specs(
        event,
        update,
        user_id="user-1",
        search_space_id=3,
        entity_id=42,
        attempt_id=7,
    )
    assert isinstance(mastery_spec, MasteryEventRowSpec)
    assert isinstance(state_spec, LearnerStateRowSpec)
    # Identity kwargs pass through.
    assert mastery_spec.user_id == "user-1"
    assert mastery_spec.search_space_id == 3
    assert mastery_spec.entity_id == 42
    assert mastery_spec.attempt_id == 7
    # Event-sourced fields.
    assert mastery_spec.event_kind == "covered"
    assert mastery_spec.score == 0.8
    assert mastery_spec.reference_step_id == "ref.7"
    assert mastery_spec.evidence_node_ids == ("n1", "n2")
    # Update-sourced fields (belief / mastery / parser / grader / dt / misc).
    assert mastery_spec.prior_belief == (0.20, 0.60, 0.20)
    assert mastery_spec.posterior_belief == (0.11, 0.52, 0.37)
    assert mastery_spec.mastery_after == 0.63
    assert mastery_spec.parser_confidence == 0.91
    assert mastery_spec.grader_confidence == 0.83
    assert mastery_spec.dt_days_since_last == 5.0
    assert mastery_spec.misconception_code == "misc.code"
    assert mastery_spec.negotiation_move is None  # NULL v1
    # LearnerStateRowSpec.
    assert state_spec.belief == (0.11, 0.52, 0.37)
    assert state_spec.mastery == 0.63
    assert state_spec.confidence == 0.41
    assert state_spec.misconception_code == "misc.code"
    assert state_spec.evidence_count == 1
    # Both frozen.
    with pytest.raises(dataclasses.FrozenInstanceError):
        mastery_spec.score = 0.0  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        state_spec.mastery = 0.0  # type: ignore[misc]


def test_event_to_row_specs_identity_defaults_none():
    """Omitting the identity kwargs leaves them None (WU-5A1 does not resolve ids)."""
    event = _covered(0.5)
    update = apply_event(
        event,
        prior_belief=COLD_START_PRIOR,
        prior_last_evidence_at=None,
        parser_confidence=1.0,
        grader_confidence=1.0,
        done_ts=_DONE_TS,
    )
    mastery_spec, _ = event_to_row_specs(event, update)
    assert mastery_spec.user_id is None
    assert mastery_spec.search_space_id is None
    assert mastery_spec.entity_id is None
    assert mastery_spec.attempt_id is None


def test_apply_event_does_not_mutate_inputs():
    prior = (0.20, 0.60, 0.20)
    apply_event(
        _covered(0.5),
        prior_belief=prior,
        prior_last_evidence_at=None,
        parser_confidence=1.0,
        grader_confidence=1.0,
        done_ts=_DONE_TS,
    )
    assert prior == (0.20, 0.60, 0.20)  # unchanged
