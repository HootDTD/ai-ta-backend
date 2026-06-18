"""WU-4B2 — the learner-event value-object + kind-enum parity tests.

Pure imports — no DB, no LLM, no Neo4j, no network. The parity test mirrors
``test_finding_kind_unchanged``: the ``LearnerEventKind`` value-set can never
drift from the §2 ``models.MASTERY_EVENT_KINDS`` documentation tuple.
"""

from __future__ import annotations

import dataclasses

import pytest

from apollo.grading.event_model import (
    EVENT_CONVERSION_VERSION,
    LearnerEvent,
    LearnerEventKind,
)
from apollo.persistence.models import MASTERY_EVENT_KINDS


def test_learner_event_kind_matches_mastery_event_kinds():
    """The §2 parity lock: the StrEnum value-set == models.MASTERY_EVENT_KINDS
    (mirrors graph_compare.test_finding_kind_unchanged)."""
    assert {k.value for k in LearnerEventKind} == set(MASTERY_EVENT_KINDS)


def test_learner_event_is_frozen():
    """A LearnerEvent is immutable; defaults are empty/None per the §2 shape."""
    event = LearnerEvent(canonical_key="eq.x", event_kind=LearnerEventKind.COVERED)
    assert event.score is None
    assert event.confidence is None
    assert event.misconception_code is None
    assert event.evidence_node_ids == ()
    assert event.reference_step_id is None
    assert event.diagnostic_flags == ()
    with pytest.raises(dataclasses.FrozenInstanceError):
        event.score = 1.0  # type: ignore[misc]


def test_event_conversion_version_constant():
    """The single source of truth for the §6.5 mapping version string."""
    assert EVENT_CONVERSION_VERSION == "finding-to-event-v1"
