"""WU-5A1 §3 — frozen value-object shape tests for ``state_model.py``.

Pure imports — no DB, no LLM, no Neo4j, no network. Mirrors
``apollo/grading/tests/test_event_model.py`` (the ``FrozenInstanceError``
immutability convention) and ``test_persistence_specs.py`` (1:1 column-set
pins). These specs map onto ``app.learner_state`` / ``app.mastery_events``
(``apollo/persistence/models.py:785-899``, DB-13); WU-5A2 fills the identity
columns.
"""

from __future__ import annotations

import dataclasses

import pytest

from apollo.learner_model.state_model import (
    BeliefUpdate,
    LearnerStateRowSpec,
    MasteryEventRowSpec,
)


def test_belief_update_is_frozen():
    update = BeliefUpdate(
        prior_belief=(0.20, 0.60, 0.20),
        posterior_belief=(0.10, 0.50, 0.40),
        mastery_after=0.65,
        confidence_after=0.30,
        misconception_code=None,
        parser_confidence=0.9,
        grader_confidence=0.95,
        dt_days_since_last=7.0,
    )
    # All 8 fields readable.
    assert update.prior_belief == (0.20, 0.60, 0.20)
    assert update.posterior_belief == (0.10, 0.50, 0.40)
    assert update.mastery_after == 0.65
    assert update.confidence_after == 0.30
    assert update.misconception_code is None
    assert update.parser_confidence == 0.9
    assert update.grader_confidence == 0.95
    assert update.dt_days_since_last == 7.0
    with pytest.raises(dataclasses.FrozenInstanceError):
        update.mastery_after = 0.0  # type: ignore[misc]


def test_mastery_event_row_spec_entity_id_defaults_none():
    """Mirrors the ``FindingRowSpec.entity_id: int | None`` pattern: the identity
    columns default ``None`` because WU-5A1 does NOT resolve ``entity_id`` — WU-5A2
    fills it before any write."""
    spec = MasteryEventRowSpec()
    assert spec.user_id is None
    assert spec.search_space_id is None
    assert spec.entity_id is None
    assert spec.attempt_id is None
    assert spec.evidence_node_ids == ()
    with pytest.raises(dataclasses.FrozenInstanceError):
        spec.entity_id = 5  # type: ignore[misc]


def test_mastery_event_row_spec_full_field_set():
    """1:1 onto ``app.mastery_events`` non-id columns (models.py:837-899,
    DB-13). A swapped/missing field is caught here. ``negotiation_move`` is
    present (the table keeps this nullable column post-A6); ``misconception_code``
    is NOT (DB-13 dropped that column)."""
    field_names = {f.name for f in dataclasses.fields(MasteryEventRowSpec)}
    assert field_names == {
        "user_id",
        "search_space_id",
        "entity_id",
        "attempt_id",
        "event_kind",
        "score",
        "parser_confidence",
        "grader_confidence",
        "negotiation_move",
        "reference_step_id",
        "prior_belief",
        "posterior_belief",
        "mastery_after",
        "dt_days_since_last",
        "evidence_node_ids",
    }


def test_learner_state_row_spec_fields():
    """1:1 onto ``app.learner_state`` belief columns (models.py:785-823,
    DB-13). ``last_evidence_at`` is DELIBERATELY omitted — WU-5A2 sets it to
    ``done_ts`` at persist time. ``misconception_code`` is NOT a field (DB-13
    dropped that column). ``evidence_count`` defaults to 1 (this event is
    evidence #1 when the row is first created)."""
    field_names = {f.name for f in dataclasses.fields(LearnerStateRowSpec)}
    assert field_names == {
        "belief",
        "mastery",
        "confidence",
        "evidence_count",
    }
    assert "last_evidence_at" not in field_names
    assert "misconception_code" not in field_names
    spec = LearnerStateRowSpec(
        belief=(0.10, 0.50, 0.40),
        mastery=0.65,
        confidence=0.30,
    )
    assert spec.evidence_count == 1
    with pytest.raises(dataclasses.FrozenInstanceError):
        spec.mastery = 0.0  # type: ignore[misc]
