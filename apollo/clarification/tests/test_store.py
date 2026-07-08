"""Mock-based unit tests for apollo.clarification.store.

These tests cover the SQL-wrapping functions using a mocked AsyncSession so
they run without a Postgres database. The Postgres-backed integration tests
(real constraint checks, UPSERT semantics) live in
tests/database/test_clarification_store.py and run in CI's Docker job."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

from apollo.clarification.store import (
    load_asked_candidate_keys,
    load_asked_waiting,
    load_confirmed_resolutions,
    record_outcome,
    write_asked_waiting,
)


def _mock_db(**kwargs):
    db = MagicMock()
    for attr, val in kwargs.items():
        setattr(db, attr, val)
    return db


# ---------------------------------------------------------------------------
# write_asked_waiting
# ---------------------------------------------------------------------------


async def test_write_asked_waiting_calls_execute():
    """write_asked_waiting constructs the PG upsert and awaits db.execute."""
    db = _mock_db(execute=AsyncMock())
    await write_asked_waiting(
        db,
        attempt_id=1,
        session_id=2,
        user_id="u1",
        search_space_id=3,
        concept_id=4,
        node_id="n1",
        candidate_key="cond.bernoulli",
        probe_question="q?",
        original_statement="p drops where v rises",
        asked_turn=5,
    )
    db.execute.assert_awaited_once()


async def test_write_asked_waiting_null_concept_id():
    """concept_id=None is allowed (session without a concept assignment)."""
    db = _mock_db(execute=AsyncMock())
    await write_asked_waiting(
        db,
        attempt_id=1,
        session_id=2,
        user_id="u1",
        search_space_id=3,
        concept_id=None,
        node_id="n1",
        candidate_key="cond.bernoulli",
        probe_question="",
        original_statement="o",
        asked_turn=1,
    )
    db.execute.assert_awaited_once()


# ---------------------------------------------------------------------------
# load_asked_waiting
# ---------------------------------------------------------------------------


async def test_load_asked_waiting_returns_list_of_rows():
    """load_asked_waiting returns a list of Clarification rows."""
    row_mock = MagicMock(name="clarification_row")
    scalars_mock = MagicMock()
    scalars_mock.all.return_value = [row_mock]
    result_mock = MagicMock()
    result_mock.scalars.return_value = scalars_mock
    db = _mock_db(execute=AsyncMock(return_value=result_mock))

    rows = await load_asked_waiting(db, attempt_id=7)

    db.execute.assert_awaited_once()
    assert rows == [row_mock]


async def test_load_asked_waiting_empty_returns_empty_list():
    """load_asked_waiting returns [] when no rows exist."""
    scalars_mock = MagicMock()
    scalars_mock.all.return_value = []
    result_mock = MagicMock()
    result_mock.scalars.return_value = scalars_mock
    db = _mock_db(execute=AsyncMock(return_value=result_mock))

    rows = await load_asked_waiting(db, attempt_id=99)
    assert rows == []


# ---------------------------------------------------------------------------
# record_outcome
# ---------------------------------------------------------------------------


async def test_record_outcome_updates_row_fields():
    """record_outcome fetches the row by id and sets state/text/turn/updated_at."""
    row_mock = MagicMock(name="clarification_row")
    result_mock = MagicMock()
    result_mock.scalar_one.return_value = row_mock
    db = _mock_db(execute=AsyncMock(return_value=result_mock))

    await record_outcome(
        db,
        clarification_id=42,
        state="confirmed",
        clarification_text="pressure is lower where velocity is higher",
        answered_turn=3,
    )

    db.execute.assert_awaited_once()
    assert row_mock.state == "confirmed"
    assert row_mock.clarification_text == "pressure is lower where velocity is higher"
    assert row_mock.answered_turn == 3
    # updated_at is set to a real datetime (not a vacuous MagicMock attr)
    assert isinstance(row_mock.updated_at, datetime)


async def test_record_outcome_vague_clears_text():
    """record_outcome with clarification_text=None (vague outcome)."""
    row_mock = MagicMock(name="clarification_row")
    result_mock = MagicMock()
    result_mock.scalar_one.return_value = row_mock
    db = _mock_db(execute=AsyncMock(return_value=result_mock))

    await record_outcome(
        db,
        clarification_id=1,
        state="vague",
        clarification_text=None,
        answered_turn=2,
    )
    assert row_mock.state == "vague"
    assert row_mock.clarification_text is None


# ---------------------------------------------------------------------------
# load_confirmed_resolutions
# ---------------------------------------------------------------------------


async def test_load_confirmed_resolutions_returns_dict():
    """load_confirmed_resolutions returns {node_id: candidate_key} for confirmed rows."""
    result_mock = MagicMock()
    result_mock.all.return_value = [
        ("n1", "cond.bernoulli"),
        ("n2", "eq.k"),
    ]
    db = _mock_db(execute=AsyncMock(return_value=result_mock))

    mapping = await load_confirmed_resolutions(db, attempt_id=5)

    db.execute.assert_awaited_once()
    assert mapping == {"n1": "cond.bernoulli", "n2": "eq.k"}


async def test_load_confirmed_resolutions_empty():
    """load_confirmed_resolutions returns {} when no confirmed rows exist."""
    result_mock = MagicMock()
    result_mock.all.return_value = []
    db = _mock_db(execute=AsyncMock(return_value=result_mock))

    mapping = await load_confirmed_resolutions(db, attempt_id=99)
    assert mapping == {}


# ---------------------------------------------------------------------------
# load_asked_candidate_keys
# ---------------------------------------------------------------------------


async def test_load_asked_candidate_keys_returns_distinct_set():
    """load_asked_candidate_keys returns the set of distinct candidate_key values
    for any asked row this attempt (dedup + M4 attempt-cap counter)."""
    scalars_mock = MagicMock()
    scalars_mock.all.return_value = ["cond.bernoulli", "eq.k"]
    result_mock = MagicMock()
    result_mock.scalars.return_value = scalars_mock
    db = _mock_db(execute=AsyncMock(return_value=result_mock))

    keys = await load_asked_candidate_keys(db, attempt_id=7)

    db.execute.assert_awaited_once()
    assert keys == {"cond.bernoulli", "eq.k"}
    assert isinstance(keys, set)


async def test_load_asked_candidate_keys_empty_returns_empty_set():
    """load_asked_candidate_keys returns an empty set when no rows exist."""
    scalars_mock = MagicMock()
    scalars_mock.all.return_value = []
    result_mock = MagicMock()
    result_mock.scalars.return_value = scalars_mock
    db = _mock_db(execute=AsyncMock(return_value=result_mock))

    keys = await load_asked_candidate_keys(db, attempt_id=99)
    assert keys == set()
