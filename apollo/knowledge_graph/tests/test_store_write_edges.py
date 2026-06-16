"""WU-2B: `KGStore.write_edges` validates endpoint existence + EDGE_ALLOWED_PAIRS
BEFORE issuing any CREATE, and emits a structured `write_edges` log
(written/dropped/invalid + per-edge reason) — NO silent drop.

Offline tests only. A fake Neo4j session (extending the negotiation-test fake)
holds the per-attempt node store, answers the endpoint-existence read, and
RECORDS every cypher run in order so a test can assert validation preceded the
CREATE. Live round-trips stay in `test_store_neo4j.py` (skipped without
NEO4J_URI) and do NOT count toward the patch gate.

Test attempt_ids are NEGATIVE (cleanup contract in `apollo/conftest.py`).
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from apollo.conftest import TEST_SPACE_ID, TEST_USER_ID
from apollo.errors import SessionFrozenError
from apollo.knowledge_graph.store import (
    KGStore,
    WriteEdgesResult,
    _node_to_neo4j_props,
)
from apollo.ontology import NODE_LABELS, Edge, EdgeType, build_node
from apollo.persistence.models import (
    ApolloSession,
    KGNegotiation,
    Message,
    ProblemAttempt,
    SessionPhase,
    SessionStatus,
)
from database.models import Base


# ---------------------------------------------------------------------------
# Fake Neo4j — extends the negotiation fake with: endpoint-existence read,
# edge CREATE counting, and an ordered cypher log.
# ---------------------------------------------------------------------------


class _FakeRecord:
    def __init__(self, data: dict[str, Any]) -> None:
        self._data = data

    def __getitem__(self, key: str) -> Any:
        return self._data[key]


class _FakeResult:
    """Supports both `.single()` (count reads) and async iteration (id reads)."""

    def __init__(self, *, single: _FakeRecord | None = None,
                 rows: list[_FakeRecord] | None = None) -> None:
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
    def __init__(self, store: "_FakeNeo4jClient") -> None:
        self._store = store

    async def run(self, cypher: str, **params: Any) -> _FakeResult:
        self._store.cyphers.append(cypher)
        # Endpoint-existence read.
        if "WHERE n.node_id IN $ids" in cypher:
            aid = int(params["aid"])
            wanted = list(params["ids"])
            present = [
                _FakeRecord({"id": nid})
                for (a, nid) in self._store.nodes
                if a == aid and nid in wanted
            ]
            return _FakeResult(rows=present)
        # Edge CREATE: count how many rows have BOTH endpoints present.
        if cypher.lstrip().startswith("UNWIND $rows AS row") and "CREATE (a)-[e:" in cypher:
            rows = params["rows"]
            written = 0
            for row in rows:
                aid = int(row["attempt_id"])
                if (aid, row["from_node_id"]) in self._store.nodes and \
                        (aid, row["to_node_id"]) in self._store.nodes:
                    written += 1
            self._store.created_edges.append((cypher, rows))
            return _FakeResult(single=_FakeRecord({"written": written}))
        raise AssertionError(f"unsupported cypher in fake: {cypher!r}")


class _FakeNeo4jClient:
    def __init__(self) -> None:
        self.nodes: dict[tuple[int, str], dict[str, Any]] = {}
        self.cyphers: list[str] = []
        self.created_edges: list[tuple[str, list]] = []

    def add_node(self, *, attempt_id: int, node) -> None:
        bag = _node_to_neo4j_props(node)
        bag["attempt_id"] = attempt_id
        self.nodes[(attempt_id, node.node_id)] = bag

    @asynccontextmanager
    async def session(self):
        yield _FakeSession(self)


# ---------------------------------------------------------------------------
# DB fixtures (SQLite for Postgres-backed freeze check)
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    tables = [
        ApolloSession.__table__,
        ProblemAttempt.__table__,
        Message.__table__,
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
    sess = ApolloSession(
        user_id=TEST_USER_ID, search_space_id=TEST_SPACE_ID,
        concept_cluster_id="continuity",
        status=SessionStatus.active.value, phase=SessionPhase.TEACHING.value,
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


# ---------------------------------------------------------------------------
# Node / edge builders
# ---------------------------------------------------------------------------


def _step(node_id, attempt_id):
    return build_node(
        node_type="procedure_step", node_id=node_id, attempt_id=attempt_id,
        source="parser", content={"action": "apply continuity", "purpose": "find v2"},
    )


def _equation(node_id, attempt_id):
    return build_node(
        node_type="equation", node_id=node_id, attempt_id=attempt_id,
        source="parser", content={"symbolic": "A1*v1 - A2*v2", "label": "continuity"},
    )


def _condition(node_id, attempt_id):
    return build_node(
        node_type="condition", node_id=node_id, attempt_id=attempt_id,
        source="parser", content={"applies_when": "incompressible flow"},
    )


def _edge(edge_type, from_id, to_id, attempt_id, from_t, to_t):
    return Edge(
        edge_type=edge_type, from_node_id=from_id, to_node_id=to_id,
        attempt_id=attempt_id, source="parser",
        from_node_type=from_t, to_node_type=to_t,
    )


def _seed(neo, attempt_id, *nodes):
    for n in nodes:
        neo.add_node(attempt_id=attempt_id, node=n)


def _is_create(cypher: str) -> bool:
    return "CREATE (a)-[e:" in cypher


def _is_existence(cypher: str) -> bool:
    return "WHERE n.node_id IN $ids" in cypher


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_write_edges_validates_before_create(store, neo, attempt):
    aid = attempt.id
    _seed(neo, aid, _step("s0", aid), _equation("e0", aid))
    edge = _edge(EdgeType.USES, "s0", "e0", aid, "procedure_step", "equation")

    result = await store.write_edges(attempt_id=aid, edges=[edge], source="parser")

    assert isinstance(result, WriteEdgesResult)
    assert result.written == 1
    assert result.dropped == 0
    assert result.invalid == 0
    # The existence read ran BEFORE any CREATE.
    existence_idx = next(i for i, c in enumerate(neo.cyphers) if _is_existence(c))
    create_idx = next(i for i, c in enumerate(neo.cyphers) if _is_create(c))
    assert existence_idx < create_idx


@pytest.mark.asyncio
async def test_write_edges_drops_edge_with_absent_endpoint_and_logs(store, neo, attempt, caplog):
    aid = attempt.id
    _seed(neo, aid, _step("s0", aid))  # e0 absent
    edge = _edge(EdgeType.USES, "s0", "MISSING", aid, "procedure_step", "equation")

    with caplog.at_level(logging.INFO, logger="apollo.knowledge_graph.store"):
        result = await store.write_edges(attempt_id=aid, edges=[edge], source="parser")

    assert result.written == 0
    assert result.dropped == 1
    assert any("endpoint_absent" in r for (_e, r) in result.reasons)
    # No CREATE cypher ran.
    assert not any(_is_create(c) for c in neo.cyphers)
    msgs = " ".join(rec.getMessage() for rec in caplog.records)
    assert "write_edge_rejected" in msgs
    assert "endpoint_absent" in msgs
    assert "write_edges" in msgs and "dropped=1" in msgs


@pytest.mark.asyncio
async def test_write_edges_rejects_disallowed_pair_and_logs(store, neo, attempt, caplog):
    aid = attempt.id
    _seed(neo, aid, _equation("e0", aid), _condition("c0", aid))
    # SCOPES is condition->equation; reverse equation->condition is disallowed.
    # The `Edge` validator forbids constructing the reversed pair directly, so
    # build a valid SCOPES edge and flip the endpoint types via model_copy to
    # simulate an edge that reaches the store with a disallowed pair (e.g. a
    # reference-graph edge whose types were resolved after construction).
    edge = _edge(EdgeType.SCOPES, "c0", "e0", aid, "condition", "equation")
    edge = edge.model_copy(update={
        "from_node_id": "e0", "to_node_id": "c0",
        "from_node_type": "equation", "to_node_type": "condition",
    })

    with caplog.at_level(logging.INFO, logger="apollo.knowledge_graph.store"):
        result = await store.write_edges(attempt_id=aid, edges=[edge], source="parser")

    assert result.invalid == 1
    assert result.written == 0
    assert any("disallowed_pair" in r for (_e, r) in result.reasons)
    assert not any(_is_create(c) for c in neo.cyphers)
    msgs = " ".join(rec.getMessage() for rec in caplog.records)
    assert "disallowed_pair" in msgs


@pytest.mark.asyncio
async def test_write_edges_unknown_endpoint_type_is_invalid(store, neo, attempt, caplog):
    aid = attempt.id
    _seed(neo, aid, _step("s0", aid), _equation("e0", aid))
    # Build a structurally-valid Edge but strip a node type to simulate a
    # reference-graph edge with an unknown endpoint type.
    edge = _edge(EdgeType.USES, "s0", "e0", aid, "procedure_step", "equation")
    edge = edge.model_copy(update={"from_node_type": None})

    with caplog.at_level(logging.INFO, logger="apollo.knowledge_graph.store"):
        result = await store.write_edges(attempt_id=aid, edges=[edge], source="parser")

    assert result.invalid == 1
    assert result.written == 0
    assert any("unknown_endpoint_type" in r for (_e, r) in result.reasons)
    assert not any(_is_create(c) for c in neo.cyphers)


@pytest.mark.asyncio
async def test_write_edges_mixed_batch_writes_valid_drops_rest(store, neo, attempt):
    aid = attempt.id
    _seed(neo, aid,
          _step("s0", aid), _equation("e0", aid),
          _condition("c0", aid))
    good = _edge(EdgeType.USES, "s0", "e0", aid, "procedure_step", "equation")
    absent = _edge(EdgeType.USES, "s0", "GONE", aid, "procedure_step", "equation")
    disallowed = _edge(EdgeType.SCOPES, "c0", "e0", aid, "condition", "equation").model_copy(
        update={
            "from_node_id": "e0", "to_node_id": "c0",
            "from_node_type": "equation", "to_node_type": "condition",
        }
    )

    result = await store.write_edges(
        attempt_id=aid, edges=[good, absent, disallowed], source="parser",
    )

    assert result.written == 1
    assert result.dropped == 1
    assert result.invalid == 1
    # Exactly one CREATE batch ran, and it was for the USES type.
    assert len(neo.created_edges) == 1
    created_cypher = neo.created_edges[0][0]
    assert "CREATE (a)-[e:USES" in created_cypher


@pytest.mark.asyncio
async def test_write_edges_persists_scopes_edge(store, neo, attempt):
    aid = attempt.id
    _seed(neo, aid, _condition("c0", aid), _equation("e0", aid))
    edge = _edge(EdgeType.SCOPES, "c0", "e0", aid, "condition", "equation")

    result = await store.write_edges(attempt_id=aid, edges=[edge], source="parser")

    assert result.written == 1
    assert len(neo.created_edges) == 1
    assert "CREATE (a)-[e:SCOPES" in neo.created_edges[0][0]


@pytest.mark.asyncio
async def test_write_edges_empty_returns_zero_result(store, neo, attempt):
    result = await store.write_edges(attempt_id=attempt.id, edges=[], source="parser")
    assert isinstance(result, WriteEdgesResult)
    assert (result.written, result.dropped, result.invalid) == (0, 0, 0)
    # No Neo4j call at all (only the Postgres freeze check ran).
    assert neo.cyphers == []


def test_write_edges_result_int_coercion():
    assert int(WriteEdgesResult(written=3, dropped=1, invalid=2)) == 3
    assert int(WriteEdgesResult()) == 0


@pytest.mark.asyncio
async def test_write_edges_respects_freeze(store, neo, db, attempt):
    aid = attempt.id
    _seed(neo, aid, _step("s0", aid), _equation("e0", aid))
    sess = (await db.execute(
        select(ApolloSession).where(ApolloSession.id == attempt.session_id)
    )).scalar_one()
    sess.phase = SessionPhase.SOLVING.value
    await db.commit()

    edge = _edge(EdgeType.USES, "s0", "e0", aid, "procedure_step", "equation")
    with pytest.raises(SessionFrozenError):
        await store.write_edges(attempt_id=aid, edges=[edge], source="parser")
    # No Neo4j touched once frozen.
    assert neo.cyphers == []


@pytest.mark.asyncio
async def test_existing_node_ids_empty_input_no_neo4j_call(store, neo):
    async with neo.session() as s:
        present = await store._existing_node_ids(s, attempt_id=-1, ids=[])
    assert present == set()
    assert neo.cyphers == []
