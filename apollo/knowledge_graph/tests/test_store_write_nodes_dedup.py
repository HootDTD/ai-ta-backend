"""WU-2B: `KGStore.write_nodes` cross-turn de-dup by id.

A turn that re-asserts a prior-turn node id must NOT mint a duplicate Neo4j
node — the edge endpoint should resolve to the existing node. `write_nodes`
fetches existing ids once and CREATEs only the genuinely new subset; reused
ids are skipped and NOT counted as added (fair-coverage contract).

Offline tests only. A fake Neo4j session holds the per-attempt node store,
answers the endpoint-existence read, and records node CREATE batches. Live
round-trips stay in `test_store_neo4j.py`. Test attempt_ids are NEGATIVE.
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
from apollo.knowledge_graph.store import KGStore, _node_to_neo4j_props
from apollo.ontology import build_node
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
# Fake Neo4j — endpoint-existence read + node CREATE counting.
# ---------------------------------------------------------------------------


class _FakeRecord:
    def __init__(self, data: dict[str, Any]) -> None:
        self._data = data

    def __getitem__(self, key: str) -> Any:
        return self._data[key]


class _FakeResult:
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
        if "WHERE n.node_id IN $ids" in cypher:
            aid = int(params["aid"])
            wanted = list(params["ids"])
            rows = [
                _FakeRecord({"id": nid})
                for (a, nid) in self._store.nodes
                if a == aid and nid in wanted
            ]
            return _FakeResult(rows=rows)
        if cypher.lstrip().startswith("UNWIND $rows AS row") and "CREATE (n:" in cypher:
            rows = params["rows"]
            for row in rows:
                self._store.nodes[(int(row["attempt_id"]), row["node_id"])] = dict(row)
            self._store.created_node_batches.append(list(rows))
            return _FakeResult(single=_FakeRecord({"written": len(rows)}))
        raise AssertionError(f"unsupported cypher in fake: {cypher!r}")


class _FakeNeo4jClient:
    def __init__(self) -> None:
        self.nodes: dict[tuple[int, str], dict[str, Any]] = {}
        self.cyphers: list[str] = []
        self.created_node_batches: list[list] = []

    def seed_node(self, *, attempt_id: int, node) -> None:
        bag = _node_to_neo4j_props(node)
        bag["attempt_id"] = attempt_id
        self.nodes[(attempt_id, node.node_id)] = bag

    @property
    def created_ids(self) -> set[str]:
        out: set[str] = set()
        for batch in self.created_node_batches:
            out |= {r["node_id"] for r in batch}
        return out

    @asynccontextmanager
    async def session(self):
        yield _FakeSession(self)


# ---------------------------------------------------------------------------
# DB fixtures
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


def _eq(node_id, attempt_id, symbolic="A1*v1 - A2*v2", label="continuity"):
    return build_node(
        node_type="equation", node_id=node_id, attempt_id=attempt_id,
        source="parser", content={"symbolic": symbolic, "label": label},
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_write_nodes_creates_new_nodes(store, neo, attempt, caplog):
    aid = attempt.id
    nodes = [_eq("eq1", aid), _eq("eq2", aid, symbolic="P + rho*g*h", label="bernoulli")]
    with caplog.at_level(logging.INFO, logger="apollo.knowledge_graph.store"):
        created = await store.write_nodes(attempt_id=aid, nodes=nodes, source="parser")
    assert created == 2
    assert neo.created_ids == {"eq1", "eq2"}
    msgs = " ".join(r.getMessage() for r in caplog.records)
    assert "write_nodes" in msgs and "created=2" in msgs and "reused=0" in msgs


@pytest.mark.asyncio
async def test_write_nodes_skips_existing_id(store, neo, attempt, caplog):
    aid = attempt.id
    neo.seed_node(attempt_id=aid, node=_eq("eq1", aid))
    batch = [_eq("eq1", aid), _eq("eq2", aid, symbolic="P + rho*g*h", label="bernoulli")]
    with caplog.at_level(logging.INFO, logger="apollo.knowledge_graph.store"):
        created = await store.write_nodes(attempt_id=aid, nodes=batch, source="parser")
    assert created == 1  # only eq2 created
    assert neo.created_ids == {"eq2"}
    assert "eq1" not in neo.created_ids
    msgs = " ".join(r.getMessage() for r in caplog.records)
    assert "created=1" in msgs and "reused=1" in msgs


@pytest.mark.asyncio
async def test_write_nodes_reused_not_counted_as_added(store, neo, attempt):
    aid = attempt.id
    neo.seed_node(attempt_id=aid, node=_eq("eq1", aid))
    created = await store.write_nodes(
        attempt_id=aid, nodes=[_eq("eq1", aid)], source="parser",
    )
    assert created == 0
    assert neo.created_node_batches == []  # nothing CREATEd


@pytest.mark.asyncio
async def test_write_nodes_all_new_unchanged_behavior(store, neo, attempt):
    aid = attempt.id
    nodes = [_eq("eq1", aid), _eq("eq2", aid, label="bernoulli", symbolic="P")]
    created = await store.write_nodes(attempt_id=aid, nodes=nodes, source="parser")
    assert created == len(nodes)


@pytest.mark.asyncio
async def test_write_nodes_empty_returns_zero(store, neo, attempt):
    created = await store.write_nodes(attempt_id=attempt.id, nodes=[], source="parser")
    assert created == 0
    assert neo.cyphers == []


@pytest.mark.asyncio
async def test_write_nodes_respects_freeze(store, neo, db, attempt):
    aid = attempt.id
    sess = (await db.execute(
        select(ApolloSession).where(ApolloSession.id == attempt.session_id)
    )).scalar_one()
    sess.phase = SessionPhase.SOLVING.value
    await db.commit()
    with pytest.raises(SessionFrozenError):
        await store.write_nodes(attempt_id=aid, nodes=[_eq("eq1", aid)], source="parser")
    assert neo.cyphers == []
