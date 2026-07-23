"""WU-4C1 — turn-order sourcing for the Done shadow chain (§1.6 / spec §6.5).

``convert_findings_to_events`` (WU-5A's consumer) needs a
``turn_order: Mapping[node_id, int]`` that is MONOTONE in extraction order
(events.py only COMPARES positions, never absolute values). But the only
per-node temporal signal that physically exists is the Neo4j node property
``created_at`` (one ISO-8601 value per write batch = one turn), and ``read_graph``
strips it. ``build_turn_order`` therefore:

  1. reads each node's ``created_at`` via the tiny ``KGStore.read_node_created_at``
     read (NOT ``read_graph``, NOT a schema change);
  2. reads student ``app.tutoring_messages.turn_index`` reference points for the
     attempt;
  3. groups node ids by distinct ``created_at``, sorts the distinct values
     ascending, and assigns each group the ``turn_index`` of the latest student
     message at-or-before that ``created_at`` — else the group's ascending ordinal
     when there are no message rows to join against.

A node with no ``created_at`` -> ABSENT key (omitted by ``read_node_created_at``).
WU-4C1 SOURCES + proves this queryable; WU-5A CONSUMES it. Pure over the two
reads; no Neo4j write. Returns a NEW ``dict`` (immutable style).
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apollo.knowledge_graph.store import KGStore
from apollo.ontology import KGGraph
from apollo.persistence.models import TutoringMessage
from apollo.persistence.neo4j_client import Neo4jClient


async def _read_student_turn_points(db: AsyncSession, *, attempt_id: int) -> list[tuple[int, str]]:
    """Return ``(turn_index, created_at_iso)`` for each student message of the
    attempt, ordered by ``turn_index``. ``created_at`` is normalized to an ISO
    string so it compares against the node ``created_at`` ISO strings."""
    rows = (
        await db.execute(
            select(TutoringMessage.turn_index, TutoringMessage.created_at)
            .where(TutoringMessage.attempt_id == attempt_id)
            .where(TutoringMessage.role == "student")
            .order_by(TutoringMessage.turn_index)
        )
    ).all()
    points: list[tuple[int, str]] = []
    for turn_index, created_at in rows:
        iso = created_at.isoformat() if hasattr(created_at, "isoformat") else str(created_at)
        points.append((int(turn_index), iso))
    return points


def _position_for(
    created_at: str,
    *,
    distinct_sorted: list[str],
    turn_points: list[tuple[int, str]],
) -> int:
    """Position for one distinct ``created_at`` value.

    When student message rows exist, bind to the ``turn_index`` of the LATEST
    student message at-or-before this ``created_at``; if none is at-or-before
    (the node predates every message), use the earliest message's turn_index.
    With NO message rows, fall back to the bare ascending ordinal of the value
    among the distinct node ``created_at`` values. Either way the result is
    monotone in ``created_at`` order."""
    if not turn_points:
        return distinct_sorted.index(created_at)
    eligible = [ti for ti, ts in turn_points if ts <= created_at]
    if eligible:
        return max(eligible)
    return turn_points[0][0]


async def build_turn_order(
    db: AsyncSession,
    neo: Neo4jClient,
    *,
    attempt_id: int,
    student_graph: KGGraph,
) -> dict[str, int]:
    """Map each KG ``node_id`` -> a monotone turn position sourced from the node's
    Neo4j ``created_at`` (joined to tutoring-message ``turn_index`` where possible).

    A node with no ``created_at`` -> absent key (events.py tolerates via
    ``_SENTINEL_TURN``). ``student_graph`` is accepted for interface symmetry with
    the chain (it carries the node id universe) but the authoritative temporal
    signal is the Neo4j ``created_at`` read.
    """
    store = KGStore(db, neo)
    node_created_at = await store.read_node_created_at(attempt_id=attempt_id)
    if not node_created_at:
        return {}

    turn_points = await _read_student_turn_points(db, attempt_id=attempt_id)
    distinct_sorted = sorted(set(node_created_at.values()))

    return {
        node_id: _position_for(
            created_at,
            distinct_sorted=distinct_sorted,
            turn_points=turn_points,
        )
        for node_id, created_at in node_created_at.items()
    }
