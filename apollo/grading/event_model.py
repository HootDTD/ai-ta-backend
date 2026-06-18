"""WU-4B2 §6.4 step 16 — the frozen learner-model event value object.

Kept separate from ``events.py`` (the §6.5 decision-table LOGIC) so the data
shape is a tiny, dependency-light module — mirroring ``graph_compare/findings.py``
(the type) vs ``graph_compare/core.py`` (the logic).

A :class:`LearnerEvent` maps 1:1 onto the ``apollo_mastery_events`` columns
(spec §2): ``canonical_key``/``event_kind``/``score``/``confidence``/
``misconception_code``/``evidence_node_ids``/``reference_step_id``. The
``parser_confidence``/``grader_confidence``/``prior_belief``/``posterior_belief``/
``mastery_after`` columns are filled at the 3-state Bayesian belief update —
that is **WU-5A**, NOT this unit. This unit PRODUCES in-memory events ONLY;
persistence is WU-5A.

``LearnerEventKind`` is a ``StrEnum`` whose value-set is asserted equal to
``models.MASTERY_EVENT_KINDS`` (the §2 documentation tuple) — the two can never
drift (mirrors ``FindingKind`` vs ``FINDING_KINDS``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

# Bumped when the §6.5 mapping changes; WU-5A/persist reads it onto the event row
# provenance (NOT a DB column in v1 — carried for replay/version parity).
EVENT_CONVERSION_VERSION: str = "finding-to-event-v1"


class LearnerEventKind(StrEnum):
    """§2 mastery ``event_kind`` set. Value-set == ``models.MASTERY_EVENT_KINDS``
    (asserted by ``test_learner_event_kind_matches_mastery_event_kinds``)."""

    COVERED = "covered"
    MISSING = "missing"
    PARTIAL = "partial"
    MISCONCEPTION = "misconception"
    CORRECTED = "corrected"


@dataclass(frozen=True)
class LearnerEvent:
    """One in-memory learner-model event (§6.4 step 16).

    Maps onto ``apollo_mastery_events`` columns; WU-5A persists it atomically
    with the belief update and fills the parser/grader/belief columns (NOT this
    unit's concern). Immutable — the §6.5 table only ever RETURNS new events."""

    canonical_key: str
    event_kind: LearnerEventKind
    score: float | None = None
    confidence: float | None = None
    misconception_code: str | None = None       # misconception/corrected only
    evidence_node_ids: tuple[str, ...] = ()      # the finding's student_node_ids
    reference_step_id: str | None = None         # covered/missing — the ref node id
    diagnostic_flags: tuple[str, ...] = field(default_factory=tuple)
    # 'edge-gap' | 'mixed-understanding' — §6.8 diagnostics (WU-4C), carried as
    # data only; NOT a DB column.
