"""WU-4C1 — build_turn_order unit tests (the two reads mocked).

``build_turn_order`` maps each KG node_id -> a monotone turn position sourced
from the node's Neo4j ``created_at`` (joined to tutoring-message ``turn_index``
where possible). A node with no ``created_at`` -> absent key (events.py tolerates
via ``_SENTINEL_TURN``). These tests mock ``KGStore.read_node_created_at`` (the
Neo4j read) and the Postgres ``Message`` read so the grouping/join logic is
verified in isolation.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from apollo.handlers.done_turn_order import build_turn_order
from apollo.ontology import KGGraph

pytestmark = pytest.mark.unit


def _db_with_messages(rows: list[tuple[int, str]]) -> MagicMock:
    """db.execute -> a result whose .all() returns (turn_index, created_at) rows."""
    db = MagicMock()

    class _Result:
        def all(self_inner):
            return list(rows)

    db.execute = AsyncMock(return_value=_Result())
    return db


def _patch_created_at(mapping: dict[str, str]):
    return patch(
        "apollo.handlers.done_turn_order.KGStore.read_node_created_at",
        new=AsyncMock(return_value=mapping),
    )


async def test_maps_node_ids_to_turn_index():
    """Two distinct created_at values (two turns) -> two positions; nodes in the
    same batch share a position; positions join to the student turn_index."""
    node_created = {
        "n1": "2026-06-18T00:00:01+00:00",
        "n2": "2026-06-18T00:00:01+00:00",
        "n3": "2026-06-18T00:00:05+00:00",
    }
    # student messages: turn 0 at t=00, turn 1 at t=04 (both <= the node ts above)
    msg_rows = [(0, "2026-06-18T00:00:00+00:00"), (1, "2026-06-18T00:00:04+00:00")]
    db = _db_with_messages(msg_rows)
    with _patch_created_at(node_created):
        out = await build_turn_order(db, MagicMock(), attempt_id=99, student_graph=KGGraph())
    # n1,n2 (the earlier batch) -> the latest turn at-or-before 00:00:01 == turn 0
    # n3 (00:00:05) -> the latest turn at-or-before == turn 1
    assert out == {"n1": 0, "n2": 0, "n3": 1}


async def test_node_with_no_created_at_absent():
    """A node missing from read_node_created_at -> absent key in the result."""
    node_created = {"n1": "2026-06-18T00:00:01+00:00"}  # n2 absent
    db = _db_with_messages([(0, "2026-06-18T00:00:00+00:00")])
    with _patch_created_at(node_created):
        out = await build_turn_order(db, MagicMock(), attempt_id=99, student_graph=KGGraph())
    assert "n1" in out
    assert "n2" not in out


async def test_empty_graph_returns_empty_map():
    db = _db_with_messages([(0, "2026-06-18T00:00:00+00:00")])
    with _patch_created_at({}):
        out = await build_turn_order(db, MagicMock(), attempt_id=99, student_graph=KGGraph())
    assert out == {}


async def test_no_messages_falls_back_to_ordinal():
    """No message rows -> bare ascending positions over the distinct created_at."""
    node_created = {
        "n1": "2026-06-18T00:00:05+00:00",
        "n2": "2026-06-18T00:00:01+00:00",
        "n3": "2026-06-18T00:00:05+00:00",
    }
    db = _db_with_messages([])  # no student messages
    with _patch_created_at(node_created):
        out = await build_turn_order(db, MagicMock(), attempt_id=99, student_graph=KGGraph())
    # distinct created_at sorted ascending: ...01 -> 0, ...05 -> 1
    assert out == {"n2": 0, "n1": 1, "n3": 1}


async def test_monotone_in_extraction_order():
    """The positions are monotone in created_at order regardless of dict order."""
    node_created = {"z": "2026-06-18T00:00:09+00:00", "a": "2026-06-18T00:00:01+00:00"}
    db = _db_with_messages([])
    with _patch_created_at(node_created):
        out = await build_turn_order(db, MagicMock(), attempt_id=99, student_graph=KGGraph())
    assert out["a"] < out["z"]
