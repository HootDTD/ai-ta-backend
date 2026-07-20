"""WU-3C1 — Layer-2 node scoping props + graded_at, offline (fake session).

Mirrors the fake-Neo4j-session pattern of ``test_store_write_nodes_dedup.py``.
A fake async session captures the ``$rows`` passed to the node CREATE so we can
assert on the injected scoping/timestamp props WITHOUT a live driver. Plus the
pure ``_record_to_node`` strip-test and the ``stamp_graded_at`` failure-path
(RetentionError) using a failing fake session. Test attempt_ids are NEGATIVE.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from apollo.conftest import TEST_SPACE_ID, TEST_USER_ID
from apollo.errors import RetentionError
from apollo.knowledge_graph.store import KGStore, _record_to_node
from apollo.ontology import build_node
from apollo.persistence.models import (
    TutoringSession,
    KGNegotiation,
    TutoringMessage,
    ProblemAttempt,
    SessionPhase,
    SessionStatus,
)
from database.models import Base

# ---------------------------------------------------------------------------
# Fake Neo4j — endpoint-existence read + node CREATE row capture.
# ---------------------------------------------------------------------------


class _FakeRecord:
    def __init__(self, data: dict[str, Any]) -> None:
        self._data = data

    def __getitem__(self, key: str) -> Any:
        return self._data[key]


class _FakeResult:
    def __init__(
        self, *, single: _FakeRecord | None = None, rows: list[_FakeRecord] | None = None
    ) -> None:
        self._single = single
        self._rows = rows or []

    async def single(self) -> _FakeRecord | None:
        return self._single

    def __aiter__(self):
        async def gen():
            for r in self._rows:
                yield r

        return gen()


class _FakeSession:
    def __init__(self, store: _FakeNeo4jClient) -> None:
        self._store = store

    async def run(self, cypher: str, **params: Any) -> _FakeResult:
        self._store.cyphers.append(cypher)
        if "WHERE n.node_id IN $ids" in cypher:
            return _FakeResult(rows=[])  # no existing nodes -> all are new
        if cypher.lstrip().startswith("UNWIND $rows AS row") and "CREATE (n:" in cypher:
            rows = params["rows"]
            self._store.created_node_batches.append([dict(r) for r in rows])
            return _FakeResult(single=_FakeRecord({"written": len(rows)}))
        if "SET n.graded_at" in cypher:
            self._store.stamped.append(dict(params))
            return _FakeResult(single=_FakeRecord({"stamped": self._store.stamp_count}))
        raise AssertionError(f"unsupported cypher in fake: {cypher!r}")


class _FakeNeo4jClient:
    def __init__(self) -> None:
        self.cyphers: list[str] = []
        self.created_node_batches: list[list[dict[str, Any]]] = []
        self.stamped: list[dict[str, Any]] = []
        self.stamp_count = 0

    @property
    def created_rows(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for batch in self.created_node_batches:
            out.extend(batch)
        return out

    @asynccontextmanager
    async def session(self):
        yield _FakeSession(self)


class _FailingNeo4jClient:
    """Every session.run raises — drives the RetentionError branch."""

    @asynccontextmanager
    async def session(self):
        yield self

    async def run(self, *a: Any, **kw: Any):
        raise RuntimeError("neo4j down")


class _RetentionRaisingNeo4jClient:
    """session.run raises a RetentionError DIRECTLY — drives the defensive
    `except RetentionError: raise` re-raise (so it is not double-wrapped)."""

    def __init__(self, err: RetentionError) -> None:
        self._err = err

    @asynccontextmanager
    async def session(self):
        yield self

    async def run(self, *a: Any, **kw: Any):
        raise self._err


# ---------------------------------------------------------------------------
# DB fixtures (SQLite, mirrors dedup test)
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        execution_options={"schema_translate_map": {"app": None, "internal": None}},
    )
    tables = [
        TutoringSession.__table__,
        ProblemAttempt.__table__,
        TutoringMessage.__table__,
        KGNegotiation.__table__,
    ]
    async with engine.begin() as conn:
        await conn.run_sync(lambda sc: Base.metadata.create_all(sc, tables=tables))
    Session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with Session() as s:
        yield s
    await engine.dispose()


@pytest_asyncio.fixture
async def attempt(db: AsyncSession):
    sess = TutoringSession(
        user_id=TEST_USER_ID,
        search_space_id=TEST_SPACE_ID,
        concept_id=1,
        status=SessionStatus.active.value,
        phase=SessionPhase.TEACHING.value,
    )
    db.add(sess)
    await db.flush()
    a = ProblemAttempt(session_id=sess.id, problem_id="p1", difficulty="intro")
    db.add(a)
    await db.commit()
    await db.refresh(a)
    return a


@pytest.fixture
def neo():
    return _FakeNeo4jClient()


@pytest.fixture
def store(db, neo):
    return KGStore(db=db, neo=neo)


def _eq(node_id, attempt_id, symbolic="A1*v1 - A2*v2", label="continuity"):
    return build_node(
        node_type="equation",
        node_id=node_id,
        attempt_id=attempt_id,
        source="parser",
        content={"symbolic": symbolic, "label": label},
    )


# ---------------------------------------------------------------------------
# Tests — write_nodes scoping
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_write_nodes_injects_user_id_and_search_space_id(store, neo, attempt):
    aid = attempt.id
    created = await store.write_nodes(
        attempt_id=aid,
        nodes=[_eq("eq1", aid)],
        source="parser",
        user_id="u-1",
        search_space_id=5,
    )
    assert created == 1
    row = neo.created_rows[0]
    assert row["user_id"] == "u-1"
    assert row["search_space_id"] == 5
    assert isinstance(row["created_at"], str)
    assert row["created_at"]  # non-empty ISO-8601 string


@pytest.mark.asyncio
async def test_write_nodes_omits_none_scoping_props(store, neo, attempt):
    aid = attempt.id
    created = await store.write_nodes(
        attempt_id=aid,
        nodes=[_eq("eq1", aid)],
        source="parser",
    )
    assert created == 1
    row = neo.created_rows[0]
    assert "user_id" not in row
    assert "search_space_id" not in row
    # created_at is ALWAYS stamped, even without scoping kwargs.
    assert isinstance(row["created_at"], str) and row["created_at"]


@pytest.mark.asyncio
async def test_write_nodes_backcompat_return_int(store, neo, attempt):
    aid = attempt.id
    created = await store.write_nodes(
        attempt_id=aid,
        nodes=[_eq("eq1", aid), _eq("eq2", aid, symbolic="P", label="bernoulli")],
        source="parser",
    )
    assert created == 2
    assert isinstance(created, int)


def test_record_to_node_strips_scoping_props():
    """Feeding the new scoping/timestamp props into _record_to_node must NOT
    leak them into node content (they are metadata, popped before rebuild)."""
    props = {
        "node_id": "eq1",
        "attempt_id": -700,
        "source": "parser",
        "symbolic": "A1*v1 - A2*v2",
        "label": "continuity",
        "user_id": "u-1",
        "search_space_id": 5,
        "created_at": "2026-06-16T00:00:00+00:00",
        "graded_at": "2026-06-16T01:00:00+00:00",
    }
    node = _record_to_node(props, ["Equation", "_KGNode"])
    assert node.node_id == "eq1"
    content = node.content.model_dump()
    assert "user_id" not in content
    assert "search_space_id" not in content
    assert "created_at" not in content
    assert "graded_at" not in content
    # genuine content survives
    assert content["symbolic"] == "A1*v1 - A2*v2"


# ---------------------------------------------------------------------------
# Tests — stamp_graded_at
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stamp_graded_at_returns_count(neo):
    neo.stamp_count = 3
    store = KGStore(db=None, neo=neo)  # type: ignore[arg-type]
    stamped = await store.stamp_graded_at(attempt_id=-501)
    assert stamped == 3
    # one scoped SET with an ISO-8601 timestamp param
    assert neo.stamped and neo.stamped[0]["aid"] == -501
    assert isinstance(neo.stamped[0]["ts"], str) and neo.stamped[0]["ts"]


@pytest.mark.asyncio
async def test_stamp_graded_at_failure_raises_retention_error():
    store = KGStore(db=None, neo=_FailingNeo4jClient())  # type: ignore[arg-type]
    with pytest.raises(RetentionError) as exc:
        await store.stamp_graded_at(attempt_id=-501)
    assert exc.value.attempt_id == -501
    assert "neo4j down" in exc.value.last_error


@pytest.mark.asyncio
async def test_stamp_graded_at_reraises_retention_error_unwrapped():
    """A RetentionError raised inside the session is re-raised verbatim (the
    defensive `except RetentionError: raise`), NOT re-wrapped with a new
    last_error."""
    inner = RetentionError(attempt_id=-777, last_error="already named")
    store = KGStore(db=None, neo=_RetentionRaisingNeo4jClient(inner))  # type: ignore[arg-type]
    with pytest.raises(RetentionError) as exc:
        await store.stamp_graded_at(attempt_id=-501)
    assert exc.value is inner  # re-raised, not re-wrapped
    assert exc.value.attempt_id == -777
