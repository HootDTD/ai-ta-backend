"""WU-5A1 §3 — frozen value objects for the 3-state Bayesian belief update.

This unit is PURE: NO DB, NO LLM, NO Neo4j, NO containers. These dataclasses are
the pre-DB mapping objects WU-5A2's repository half consumes — they carry NO
session/engine, NO id/created_at (the DB owns those). Mirrors the shipped
``apollo/grading/persistence.py`` ``RunRowSpec``/``FindingRowSpec`` pattern
(``builds NEW spec objects, never mutates inputs``).

  * :class:`BeliefUpdate` — the in-memory result of one ``apply_event`` call: the
    prior + posterior belief, the mastery/confidence readouts, the (two-step)
    misconception code, the parser/grader confidences that formed ``q``, and the
    recorded-but-not-applied ``dt_days_since_last`` (decay is WU-5B).
  * :class:`MasteryEventRowSpec` — 1:1 onto ``app.mastery_events`` non-id
    columns (``models.py:837-899``, DB-13). The identity columns
    (``user_id``/``search_space_id``/``entity_id``/``attempt_id``) default
    ``None`` because WU-5A1 does NOT resolve them — WU-5A2 fills them before any
    write (mirrors ``FindingRowSpec.entity_id: int | None``). ``entity_id`` is
    REQUIRED non-``None`` before a write, but that gate is WU-5A2's. Carries
    ``negotiation_move`` (the table keeps this nullable column post-A6; writers
    pass ``None``) and NOT ``misconception_code`` (DB-13 dropped that column —
    misconceptions are tracked via ``event_kind`` instead).
  * :class:`LearnerStateRowSpec` — 1:1 onto ``app.learner_state`` belief
    columns (``models.py:785-823``, DB-13). ``last_evidence_at`` is DELIBERATELY
    NOT a field here: it is a persist-time concern WU-5A2 sets to ``done_ts``.
    Carries NO ``misconception_code`` (DB-13 dropped that column from the table).
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Default belief used as the spec field default (so a spec is constructible
# without a belief). The authoritative cold-start prior lives in ``belief.py``.
_DEFAULT_BELIEF: tuple[float, float, float] = (0.20, 0.60, 0.20)


@dataclass(frozen=True)
class BeliefUpdate:
    """The immutable result of one §3 belief update (``apply_event``). Never
    mutated — ``apply_event`` returns a NEW instance per event."""

    prior_belief: tuple[float, float, float]
    posterior_belief: tuple[float, float, float]
    mastery_after: float
    confidence_after: float
    misconception_code: str | None
    parser_confidence: float
    grader_confidence: float
    dt_days_since_last: float | None


@dataclass(frozen=True)
class MasteryEventRowSpec:
    """Pure pre-DB value object, 1:1 onto ``app.mastery_events`` non-id columns
    (``models.py:837-899``, DB-13). The identity columns default ``None`` —
    WU-5A1 does not resolve ids; WU-5A2 fills them. ``negotiation_move`` mirrors
    the still-live nullable DDL column (writers pass ``None``, matching
    ``apollo/projections/mastery.py``); there is NO ``misconception_code`` field
    — DB-13 dropped that column from the table. Immutable."""

    user_id: str | None = None
    search_space_id: int | None = None
    entity_id: int | None = None
    attempt_id: int | None = None
    event_kind: str = ""
    score: float | None = None
    parser_confidence: float | None = None
    grader_confidence: float | None = None
    negotiation_move: str | None = None
    reference_step_id: str | None = None
    prior_belief: tuple[float, float, float] = _DEFAULT_BELIEF
    posterior_belief: tuple[float, float, float] = _DEFAULT_BELIEF
    mastery_after: float = 0.0
    dt_days_since_last: float | None = None
    evidence_node_ids: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class LearnerStateRowSpec:
    """Pure pre-DB value object, 1:1 onto ``app.learner_state`` belief columns
    (``models.py:785-823``, DB-13). ``last_evidence_at`` is intentionally
    OMITTED — it is a persist-time concern WU-5A2 sets to ``done_ts``. There is
    NO ``misconception_code`` field — DB-13 dropped that column from the table.
    ``evidence_count`` defaults to 1 (this event is the first piece of evidence
    when the row is created). Immutable."""

    belief: tuple[float, float, float]
    mastery: float
    confidence: float
    evidence_count: int = 1
