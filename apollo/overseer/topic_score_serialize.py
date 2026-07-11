"""JSON-shape serialization for :class:`TopicScoreResult` (2026-07-10 design
spec ``docs/superpowers/specs/2026-07-10-apollo-topic-score-design.md``
section 2/3).

ONE serializer, reused by BOTH the canonical artifact's ``scores.topic_score``
block (``apollo/handlers/artifact_writer.py``, flag-independent) and the
served ``student_response["topics"]`` payload (``apollo/handlers/done.py``,
under ``APOLLO_TOPIC_SCORE_SERVED``) — so the two surfaces can never drift in
field names. Field names are pinned exactly to the spec's §2 shape:

    {score, letter, coverage_component, misconception_dock, topics: [
        {canonical_key, display_name, credit, status, weight, misconceptions: [
            {canonical_key, resolved, dock_points, evidence_span}
        ]}
    ]}

Pure module: no IO. Kept separate from ``topic_score.py`` (the already-landed,
100%-covered pure-computation module) so this additive serialization concern
never touches that module's tested surface.
"""

from __future__ import annotations

from apollo.overseer.topic_score import TopicCredit, TopicMisconception, TopicScoreResult


def _serialize_misconception(misc: TopicMisconception) -> dict:
    return {
        "canonical_key": misc.canonical_key,
        "resolved": misc.resolved,
        "dock_points": misc.dock_points,
        "evidence_span": misc.evidence_span,
    }


def _serialize_topic(topic: TopicCredit) -> dict:
    return {
        "canonical_key": topic.canonical_key,
        "display_name": topic.display_name,
        "credit": topic.credit,
        "status": topic.status,
        "weight": topic.weight,
        "misconceptions": [_serialize_misconception(m) for m in topic.misconceptions],
    }


def serialize_topics(result: TopicScoreResult) -> list[dict]:
    """The ``topics[]`` list alone (spec §2 shape) — used directly for
    ``student_response["topics"]`` (spec §3), which is JUST the topics list,
    not the full result envelope."""
    return [_serialize_topic(t) for t in result.topics]


def serialize_topic_score(result: TopicScoreResult) -> dict:
    """The full ``topic_score`` block (spec §2/§3 artifact shape) —
    ``{score, letter, coverage_component, misconception_dock, topics}`` — used
    for the canonical artifact's ``scores.topic_score`` key."""
    return {
        "score": result.score,
        "letter": result.letter,
        "coverage_component": result.coverage_component,
        "misconception_dock": result.misconception_dock,
        "topics": serialize_topics(result),
    }


__all__ = ["serialize_topic_score", "serialize_topics"]
