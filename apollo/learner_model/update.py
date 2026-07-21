"""WU-5A1 §3 — pure orchestration of the belief update (no IO).

This unit is PURE: NO DB, NO LLM, NO Neo4j, NO containers, NO network. It threads
the ``belief.py`` math core into one immutable result:

  * :func:`apply_event` — one frozen ``LearnerEvent`` + a prior belief + the two
    confidence scalars + the timestamps -> a frozen :class:`BeliefUpdate`. The
    ONLY place ``q`` is formed is ``q = parser_confidence · grader_confidence`` —
    ``event.confidence`` (the resolution confidence) is NOT re-multiplied (the
    COVERED ``score`` is already resolution-scaled by the §6.5 converter, so
    folding ``event.confidence`` into q would double-count it). ``dt_days_since_last``
    is RECORDED but NO decay is applied (decay is WU-5B).
  * :func:`event_to_row_specs` — the WU-5A2 hand-off seam: maps an ``event`` +
    its ``BeliefUpdate`` onto the two frozen ``*RowSpec`` value objects. The
    identity ids (user/search_space/entity/attempt) are passed THROUGH (default
    ``None``); WU-5A2 supplies the resolved values before any write.

Builds NEW value objects, never mutates inputs.
"""

from __future__ import annotations

from datetime import datetime

from apollo.grading.event_model import LearnerEvent
from apollo.learner_model.belief import (
    COLD_START_PRIOR,
    bayes_update,
    confidence_of,
    damp,
    likelihood_for_event,
    mastery_of,
    misconception_code_of,
)
from apollo.learner_model.state_model import (
    BeliefUpdate,
    LearnerStateRowSpec,
    MasteryEventRowSpec,
)


def apply_event(
    event: LearnerEvent,
    *,
    prior_belief: tuple[float, float, float] | None,
    prior_last_evidence_at: datetime | None,
    parser_confidence: float,
    grader_confidence: float,
    done_ts: datetime,
) -> BeliefUpdate:
    """Apply one event to ``prior_belief`` and return a NEW :class:`BeliefUpdate`.

    Cold-start: a ``None`` prior falls back to ``COLD_START_PRIOR``. ``q`` is
    ``parser_confidence · grader_confidence`` ONLY. ``dt_days_since_last`` is the
    whole-day gap to ``done_ts`` (``None`` when there is no prior anchor); NO
    decay is applied here (WU-5B)."""
    prior = prior_belief if prior_belief is not None else COLD_START_PRIOR
    q = parser_confidence * grader_confidence
    likelihood = likelihood_for_event(event)
    damped = damp(likelihood, q)
    posterior = bayes_update(prior, damped)
    dt_days_since_last = (
        (done_ts - prior_last_evidence_at).days if prior_last_evidence_at is not None else None
    )
    return BeliefUpdate(
        prior_belief=prior,
        posterior_belief=posterior,
        mastery_after=mastery_of(posterior),
        confidence_after=confidence_of(posterior),
        misconception_code=misconception_code_of(posterior, event),
        parser_confidence=parser_confidence,
        grader_confidence=grader_confidence,
        dt_days_since_last=dt_days_since_last,
    )


def event_to_row_specs(
    event: LearnerEvent,
    update: BeliefUpdate,
    *,
    user_id: str | None = None,
    search_space_id: int | None = None,
    entity_id: int | None = None,
    attempt_id: int | None = None,
) -> tuple[MasteryEventRowSpec, LearnerStateRowSpec]:
    """Map ``event`` + ``update`` onto the two frozen row specs (the WU-5A2 seam).

    Belief/mastery/confidence/misconception/parser/grader come from ``update``;
    event_kind/score/reference_step_id/evidence_node_ids from ``event``; the
    identity ids pass through (default ``None`` — WU-5A1 does not resolve them)."""
    mastery_spec = MasteryEventRowSpec(
        user_id=user_id,
        search_space_id=search_space_id,
        entity_id=entity_id,
        attempt_id=attempt_id,
        event_kind=event.event_kind.value,
        score=event.score,
        misconception_code=update.misconception_code,
        parser_confidence=update.parser_confidence,
        grader_confidence=update.grader_confidence,
        reference_step_id=event.reference_step_id,
        prior_belief=update.prior_belief,
        posterior_belief=update.posterior_belief,
        mastery_after=update.mastery_after,
        dt_days_since_last=update.dt_days_since_last,
        evidence_node_ids=event.evidence_node_ids,
    )
    state_spec = LearnerStateRowSpec(
        belief=update.posterior_belief,
        mastery=update.mastery_after,
        confidence=update.confidence_after,
        misconception_code=update.misconception_code,
    )
    return mastery_spec, state_spec
