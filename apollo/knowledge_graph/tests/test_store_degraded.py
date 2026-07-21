"""Apollo Neo4j degraded mode — `KGStore` guard.

`KGStore.__init__` accepts `neo: Neo4jClient | None`. Every Neo4j-backed
public method must raise `KGUnavailableError` at entry when `neo is None`
(the `_require_neo` guard) — no partial work, no silent no-op. Postgres-only
methods (freeze/unfreeze/get_node_trace/the private session-lookup helpers)
must keep working unconditionally with `neo=None`.

Most of the `neo=None` cases are offline only (no Neo4j session ever
reached). A handful of regression tests below drive a HEALTHY minimal fake
driver through `read_node_created_at`/`read_node_graded_at`/`walk_chain`/
`delete_subgraph` to pin that `_require_neo`'s narrowed return value (not
`self.neo`) is what the method actually uses for its Neo4j session — the
guard must be transparent on the happy path, not just raise on `None`.

SQLite stands in for Postgres. Test attempt_ids are NEGATIVE
(apollo/conftest.py convention).
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from apollo.conftest import TEST_SPACE_ID, TEST_USER_ID
from apollo.errors import KGUnavailableError
from apollo.knowledge_graph.store import KGStore
from apollo.ontology import EdgeType, build_node
from apollo.persistence.models import (
    KGNegotiation,
    ProblemAttempt,
    SessionPhase,
    SessionStatus,
    TutoringMessage,
    TutoringSession,
)
from database.models import Base

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Minimal fake Neo4j driver — a healthy `neo` that always returns an empty
# result, just enough to prove `_require_neo`'s return value (not
# `self.neo`) is what each guarded method's `async with neo.session()`
# actually uses.
# ---------------------------------------------------------------------------


class _EmptyResult:
    async def single(self):
        return None

    def __aiter__(self):
        async def gen():
            return
            yield  # pragma: no cover - unreachable, satisfies generator syntax

        return gen()


class _FakeSession:
    async def run(self, cypher: str, **params: Any) -> _EmptyResult:
        return _EmptyResult()


class _FakeNeo4jClient:
    @asynccontextmanager
    async def session(self):
        yield _FakeSession()


@pytest.fixture
def healthy_neo():
    return _FakeNeo4jClient()


@pytest.fixture
def store_healthy(db, healthy_neo):
    return KGStore(db=db, neo=healthy_neo)


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
    a = ProblemAttempt(
        session_id=sess.id,
        problem_id=1,
        difficulty="intro",
        user_id=sess.user_id,
        course_id=sess.course_id,
    )
    db.add(a)
    await db.commit()
    await db.refresh(a)
    return a


@pytest.fixture
def store(db):
    return KGStore(db=db, neo=None)


def _eq(node_id, attempt_id):
    return build_node(
        node_type="equation",
        node_id=node_id,
        attempt_id=attempt_id,
        source="parser",
        content={"symbolic": "A1*v1 - A2*v2", "label": "continuity"},
    )


# ---------------------------------------------------------------------------
# Neo4j-backed public methods — every one raises KGUnavailableError on
# neo=None. Parametrized over (method_name, stage, kwargs_fn) so the whole
# surface is exercised uniformly.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_write_nodes_raises_on_none(store, attempt):
    with pytest.raises(KGUnavailableError) as exc_info:
        await store.write_nodes(
            attempt_id=attempt.id,
            nodes=[_eq("eq1", attempt.id)],
            source="parser",
        )
    assert exc_info.value.stage == "write_nodes"


@pytest.mark.asyncio
async def test_write_edges_raises_on_none(store, attempt):
    from apollo.ontology import Edge

    edge = Edge(
        edge_type=EdgeType.USES,
        from_node_id="a",
        to_node_id="b",
        attempt_id=attempt.id,
        source="parser",
    )
    with pytest.raises(KGUnavailableError) as exc_info:
        await store.write_edges(attempt_id=attempt.id, edges=[edge], source="parser")
    assert exc_info.value.stage == "write_edges"


@pytest.mark.asyncio
async def test_read_graph_raises_on_none(store, attempt):
    with pytest.raises(KGUnavailableError) as exc_info:
        await store.read_graph(attempt_id=attempt.id)
    assert exc_info.value.stage == "read_graph"


@pytest.mark.asyncio
async def test_read_node_created_at_raises_on_none(store, attempt):
    with pytest.raises(KGUnavailableError) as exc_info:
        await store.read_node_created_at(attempt_id=attempt.id)
    assert exc_info.value.stage == "read_node_created_at"


@pytest.mark.asyncio
async def test_read_node_graded_at_raises_on_none(store, attempt):
    with pytest.raises(KGUnavailableError) as exc_info:
        await store.read_node_graded_at(attempt_id=attempt.id)
    assert exc_info.value.stage == "read_node_graded_at"


@pytest.mark.asyncio
async def test_walk_chain_raises_on_none(store, attempt):
    with pytest.raises(KGUnavailableError) as exc_info:
        await store.walk_chain(
            attempt_id=attempt.id,
            start_node_id="eq1",
            edge_types=[EdgeType.PRECEDES],
        )
    assert exc_info.value.stage == "walk_chain"


@pytest.mark.asyncio
async def test_delete_subgraph_raises_on_none(store, attempt):
    with pytest.raises(KGUnavailableError) as exc_info:
        await store.delete_subgraph(attempt_id=attempt.id)
    assert exc_info.value.stage == "delete_subgraph"


@pytest.mark.asyncio
async def test_stamp_graded_at_raises_kg_unavailable_not_retention_error(store, attempt):
    """Degraded mode raises KGUnavailableError BEFORE the try/except that
    wraps other failures into RetentionError — the two must stay distinct
    even though done.py's call site catches both identically."""
    with pytest.raises(KGUnavailableError) as exc_info:
        await store.stamp_graded_at(attempt_id=attempt.id)
    assert exc_info.value.stage == "stamp_graded_at"


@pytest.mark.asyncio
async def test_mark_node_disputed_raises_on_none(store, attempt):
    with pytest.raises(KGUnavailableError) as exc_info:
        await store.mark_node_disputed(
            attempt_id=attempt.id,
            node_id="eq1",
            reason="wrong",
        )
    assert exc_info.value.stage == "_set_node_status_neo4j"


@pytest.mark.asyncio
async def test_paraphrase_node_raises_on_none(store, attempt):
    with pytest.raises(KGUnavailableError) as exc_info:
        await store.paraphrase_node(
            attempt_id=attempt.id,
            node_id="eq1",
            surface_form="my words",
        )
    assert exc_info.value.stage == "_set_node_status_neo4j"


@pytest.mark.asyncio
async def test_skip_node_raises_on_none(store, attempt):
    with pytest.raises(KGUnavailableError) as exc_info:
        await store.skip_node(attempt_id=attempt.id, node_id="eq1")
    assert exc_info.value.stage == "_set_node_status_neo4j"


@pytest.mark.asyncio
async def test_summarize_for_apollo_raises_on_none(store, attempt):
    """Goes through read_graph — covered transitively, no separate guard."""
    with pytest.raises(KGUnavailableError) as exc_info:
        await store.summarize_for_apollo(attempt_id=attempt.id)
    assert exc_info.value.stage == "read_graph"


# ---------------------------------------------------------------------------
# Postgres-only methods — MUST keep working with neo=None.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_freeze_unfreeze_work_with_neo_none(store, attempt, db):
    await store.freeze(attempt.session_id)
    refreshed = await db.get(TutoringSession, attempt.session_id)
    assert refreshed.phase == SessionPhase.PROBLEM_REVEAL.value

    await store.unfreeze(attempt.session_id)
    refreshed2 = await db.get(TutoringSession, attempt.session_id)
    assert refreshed2.phase == SessionPhase.TEACHING.value


@pytest.mark.asyncio
async def test_get_node_trace_works_with_neo_none(store, attempt):
    """Postgres-only read (KGNegotiation + TutoringMessage); no attempt to touch
    self.neo at all."""
    out = await store.get_node_trace(attempt_id=attempt.id, node_id="eq1")
    assert out["node_id"] == "eq1"
    assert out["moves"] == []
    assert out["source_utterance"] is None


@pytest.mark.asyncio
async def test_session_id_for_attempt_works_with_neo_none(store, attempt):
    sid = await store._session_id_for_attempt(attempt.id)
    assert sid == attempt.session_id


@pytest.mark.asyncio
async def test_ensure_unfrozen_works_with_neo_none(store, attempt):
    # TEACHING phase (fixture default) does not raise.
    await store._ensure_unfrozen(attempt.session_id)


# ---------------------------------------------------------------------------
# Healthy-client regression: `_require_neo`'s NARROWED return value (not
# `self.neo`) is what each method actually sessions against.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_node_created_at_uses_narrowed_neo(store_healthy, attempt):
    out = await store_healthy.read_node_created_at(attempt_id=attempt.id)
    assert out == {}


@pytest.mark.asyncio
async def test_read_node_graded_at_uses_narrowed_neo(store_healthy, attempt):
    out = await store_healthy.read_node_graded_at(attempt_id=attempt.id)
    assert out == {}


@pytest.mark.asyncio
async def test_walk_chain_uses_narrowed_neo(store_healthy, attempt):
    out = await store_healthy.walk_chain(
        attempt_id=attempt.id,
        start_node_id="eq1",
        edge_types=[EdgeType.PRECEDES],
    )
    assert out == []


@pytest.mark.asyncio
async def test_delete_subgraph_uses_narrowed_neo(store_healthy, attempt):
    # No exception — the fake session's run() is reached and returns cleanly.
    await store_healthy.delete_subgraph(attempt_id=attempt.id)
