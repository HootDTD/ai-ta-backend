"""WU-5A2 — pure unit test for the back-compat `stamp_graded_at(ts=...)` widening.

No Docker, no Neo4j container: a fake async Neo4j session captures the Cypher
params so we can assert exactly which `ts` string `stamp_graded_at` writes. The
widening must (a) keep the no-`ts` call writing a fresh `_utc_now_iso()` string,
(b) normalize a `datetime` to `.isoformat()`, and (c) pass a string `ts` through
verbatim — so Neo4j `graded_at` and Postgres `last_evidence_at` can carry the
IDENTICAL `done_ts` instant.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from apollo.knowledge_graph.store import KGStore

pytestmark = pytest.mark.unit


class _FakeResult:
    def __init__(self, stamped: int) -> None:
        self._stamped = stamped

    async def single(self) -> dict[str, int]:
        return {"stamped": self._stamped}


class _FakeNeoSession:
    """Captures the params of the single `s.run(...)` call."""

    def __init__(self) -> None:
        self.last_params: dict[str, Any] | None = None
        self.last_cypher: str | None = None

    async def run(self, cypher: str, **params: Any) -> _FakeResult:
        self.last_cypher = cypher
        self.last_params = params
        return _FakeResult(stamped=3)

    async def __aenter__(self) -> _FakeNeoSession:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None


class _FakeNeo:
    def __init__(self, session: _FakeNeoSession) -> None:
        self._session = session

    def session(self) -> _FakeNeoSession:
        return self._session


class _StubDb:
    """stamp_graded_at never touches Postgres."""


def _store() -> tuple[KGStore, _FakeNeoSession]:
    sess = _FakeNeoSession()
    return KGStore(_StubDb(), _FakeNeo(sess)), sess  # type: ignore[arg-type]


async def test_stamp_graded_at_normalizes_datetime_ts():
    store, sess = _store()
    dt = datetime(2026, 6, 18, 12, 0, 0, tzinfo=UTC)
    stamped = await store.stamp_graded_at(attempt_id=7, ts=dt)
    assert stamped == 3
    assert sess.last_params is not None
    assert sess.last_params["ts"] == dt.isoformat()
    assert sess.last_params["aid"] == 7


async def test_stamp_graded_at_passes_string_ts_through():
    store, sess = _store()
    iso = "2026-06-18T12:00:00+00:00"
    await store.stamp_graded_at(attempt_id=9, ts=iso)
    assert sess.last_params is not None
    assert sess.last_params["ts"] == iso


async def test_stamp_graded_at_no_ts_uses_utc_now():
    store, sess = _store()
    before = datetime.now(UTC)
    await store.stamp_graded_at(attempt_id=11)
    after = datetime.now(UTC)
    assert sess.last_params is not None
    written = datetime.fromisoformat(sess.last_params["ts"])
    # the fallback wrote a fresh now() between before and after
    assert before <= written <= after
