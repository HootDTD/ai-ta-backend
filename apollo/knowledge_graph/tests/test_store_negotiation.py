"""P3.1 + P3.2 — Negotiable OLM round-trip + KGStore negotiation methods.

P3.1 (pure-function adapters): `_node_to_neo4j_props` and `_record_to_node`
preserve the new `status` / `student_belief` fields on every node type.

P3.2 (KGStore methods): `mark_node_disputed`, `paraphrase_node`, `skip_node`,
`get_node_trace` — each move updates Neo4j status and appends an audit row
in apollo_kg_negotiations.

A small in-test fake Neo4j replaces the live driver. It pattern-matches on
the SET-clause strings the methods emit (DISPUTED / DUAL / DUAL+belief) and
returns a stub record matching the live driver's shape. The live tests in
`test_store_neo4j.py` cover the real driver; these offline tests cover the
contract.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from apollo.conftest import TEST_SPACE_ID, TEST_USER_ID
from apollo.errors import KGEntryNotFoundError, SessionFrozenError
from apollo.knowledge_graph.store import (
    KGStore,
    _node_to_neo4j_props,
    _record_to_node,
)
from apollo.ontology import NODE_LABELS, build_node
from apollo.persistence.models import (
    ApolloSession,
    KGNegotiation,
    Message,
    ProblemAttempt,
    SessionPhase,
    SessionStatus,
)
from database.models import Base
from sqlalchemy import select


def _node(**overrides):
    base = dict(
        node_type="equation",
        node_id="eq1",
        attempt_id=42,
        source="parser",
        content={"symbolic": "A1*v1 - A2*v2", "label": "continuity"},
    )
    base.update(overrides)
    return build_node(**base)


def _round_trip(node):
    """Adapter pair contract: props out + props in must reconstruct."""
    bag = _node_to_neo4j_props(node)
    label = NODE_LABELS[node.node_type]
    return _record_to_node(bag, [label, "_KGNode"])


def test_default_node_round_trips_with_accepted_status():
    """Pre-P3 baseline: parser-authored node round-trips with no surprise."""
    n = _node()
    out = _round_trip(n)
    assert out.status == "ACCEPTED"
    assert out.student_belief is None
    assert out.parser_confidence == 1.0
    assert out.content.symbolic == "A1*v1 - A2*v2"


def test_disputed_status_round_trips():
    n = _node(status="DISPUTED")
    out = _round_trip(n)
    assert out.status == "DISPUTED"
    assert out.student_belief is None


def test_dual_status_with_student_belief_round_trips():
    n = _node(status="DUAL", student_belief="A1 v1 = A2 v2 (in my words)")
    out = _round_trip(n)
    assert out.status == "DUAL"
    assert out.student_belief == "A1 v1 = A2 v2 (in my words)"


def test_student_belief_omitted_from_props_when_none():
    """Neo4j has no native NULL; we omit the property rather than write
    a literal None. Round-trip recovers None either way."""
    n = _node(student_belief=None)
    bag = _node_to_neo4j_props(n)
    assert "student_belief" not in bag


def test_legacy_record_with_no_status_or_belief_defaults_to_accepted():
    """Pre-P3 nodes in Neo4j (written before migration 021 deployed) have
    no `status` or `student_belief` properties. Reading them back must
    default to ACCEPTED + None — the same pre-P3 baseline."""
    legacy_bag = {
        "node_id": "eq1",
        "attempt_id": 42,
        "source": "parser",
        "parser_confidence": 0.85,
        "symbolic": "x - y",
        "label": "",
        "variables": [],
    }
    out = _record_to_node(legacy_bag, ["Equation", "_KGNode"])
    assert out.status == "ACCEPTED"
    assert out.student_belief is None
    assert out.parser_confidence == 0.85


def test_unknown_status_value_falls_back_to_accepted():
    """Defensive coercion: a stray status value (e.g. typo'd manually)
    must not crash the read path. Coverage and Done-gate re-author the
    correct status on the next move."""
    bad_bag = {
        "node_id": "eq1",
        "attempt_id": 42,
        "source": "parser",
        "parser_confidence": 1.0,
        "status": "WHATEVER",
        "symbolic": "x - y",
        "label": "",
        "variables": [],
    }
    out = _record_to_node(bad_bag, ["Equation", "_KGNode"])
    assert out.status == "ACCEPTED"


def test_all_three_node_types_round_trip_status():
    """Status/student_belief live on _NodeBase, so every node type carries
    them. Spot-check three to lock the contract."""
    for kind, content in [
        ("equation", {"symbolic": "x", "label": ""}),
        ("definition", {"concept": "density", "meaning": "mass per volume"}),
        ("procedure_step", {"action": "write continuity", "purpose": ""}),
    ]:
        n = build_node(
            node_type=kind, node_id="x", attempt_id=1, source="parser",
            content=content, status="DUAL", student_belief="...",
        )
        out = _round_trip(n)
        assert out.status == "DUAL", f"failed for {kind}"
        assert out.student_belief == "...", f"failed for {kind}"


# ===========================================================================
# P3.2 — KGStore negotiation methods
# ===========================================================================


class _FakeRecord:
    """Minimal record object mimicking neo4j.AsyncRecord — only `__getitem__`
    is used by KGStore."""

    def __init__(self, data: dict[str, Any]) -> None:
        self._data = data

    def __getitem__(self, key: str) -> Any:
        return self._data[key]


class _FakeResult:
    def __init__(self, record: _FakeRecord | None) -> None:
        self._rec = record

    async def single(self) -> _FakeRecord | None:
        return self._rec


class _FakeNeo4jSession:
    """Tiny driver session mimicking the bits KGStore touches.

    Holds a node store keyed by `(attempt_id, node_id) -> props bag`.
    Pattern-matches on the SET clause to apply the right mutation. Only
    handles the cypher shapes our negotiation methods emit; anything else
    raises so a stale test catches drift early.
    """

    def __init__(self, nodes: dict[tuple[int, str], dict[str, Any]]) -> None:
        self._nodes = nodes

    async def run(self, cypher: str, **params: Any) -> _FakeResult:
        aid = int(params["aid"])
        nid = str(params["nid"])
        key = (aid, nid)
        if key not in self._nodes:
            return _FakeResult(None)

        node = self._nodes[key]
        if "n.status = 'DUAL', n.student_belief = $belief" in cypher:
            node["status"] = "DUAL"
            node["student_belief"] = params["belief"]
        elif "n.status = 'DUAL'" in cypher:
            node["status"] = "DUAL"
        elif "n.status = 'DISPUTED'" in cypher:
            node["status"] = "DISPUTED"
        else:
            raise AssertionError(f"unsupported cypher in fake: {cypher!r}")

        # KGStore reads `props` and `labels` off the record.
        return _FakeResult(_FakeRecord({
            "props": dict(node["_bag"]) | {
                "status": node["status"],
                **({"student_belief": node["student_belief"]}
                   if node.get("student_belief") is not None else {}),
            },
            "labels": list(node["_labels"]),
        }))


class _FakeNeo4jClient:
    """Replaces Neo4jClient. Pre-populated with a per-attempt subgraph."""

    def __init__(self) -> None:
        self.nodes: dict[tuple[int, str], dict[str, Any]] = {}

    def add_node(self, *, attempt_id: int, node):
        bag = _node_to_neo4j_props(node)
        bag["source"] = node.source
        bag["attempt_id"] = attempt_id
        label = NODE_LABELS[node.node_type]
        self.nodes[(attempt_id, node.node_id)] = {
            "_bag": bag,
            "_labels": [label, "_KGNode"],
            "status": node.status,
            "student_belief": node.student_belief,
        }

    @asynccontextmanager
    async def session(self):
        yield _FakeNeo4jSession(self.nodes)


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
        user_id=TEST_USER_ID, search_space_id=TEST_SPACE_ID, concept_cluster_id="continuity",
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


def _seed(neo: _FakeNeo4jClient, *, attempt_id: int):
    """Seed an equation node and a definition node into the fake graph."""
    eq = build_node(
        node_type="equation", node_id="eq1", attempt_id=attempt_id,
        source="parser",
        content={"symbolic": "A1*v1 - A2*v2", "label": "continuity"},
        parser_confidence=0.55,
    )
    df = build_node(
        node_type="definition", node_id="d1", attempt_id=attempt_id,
        source="parser",
        content={"concept": "density", "meaning": "mass per volume"},
    )
    neo.add_node(attempt_id=attempt_id, node=eq)
    neo.add_node(attempt_id=attempt_id, node=df)


# ---------------------------------------------------------------------------
# mark_node_disputed (CHALLENGE)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_mark_node_disputed_sets_status_and_logs(store, neo, db, attempt):
    _seed(neo, attempt_id=attempt.id)
    out = await store.mark_node_disputed(
        attempt_id=attempt.id, node_id="eq1",
        reason="you misheard, it's volumetric not mass",
    )
    assert out.status == "DISPUTED"
    assert neo.nodes[(attempt.id, "eq1")]["status"] == "DISPUTED"

    rows = (await db.execute(
        select(KGNegotiation).where(KGNegotiation.entry_id == "eq1")
    )).scalars().all()
    assert len(rows) == 1
    assert rows[0].move == "challenge"
    assert rows[0].actor == "student"
    assert rows[0].payload == {"reason": "you misheard, it's volumetric not mass"}


@pytest.mark.asyncio
async def test_mark_node_disputed_raises_when_node_missing(store, attempt):
    with pytest.raises(KGEntryNotFoundError):
        await store.mark_node_disputed(
            attempt_id=attempt.id, node_id="ghost", reason="?",
        )


# ---------------------------------------------------------------------------
# paraphrase_node (SUPPLY-PARAPHRASE)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_paraphrase_node_sets_dual_and_belief(store, neo, db, attempt):
    _seed(neo, attempt_id=attempt.id)
    out = await store.paraphrase_node(
        attempt_id=attempt.id, node_id="eq1",
        surface_form="rho * A * v is constant in pipes",
    )
    assert out.status == "DUAL"
    assert out.student_belief == "rho * A * v is constant in pipes"

    rows = (await db.execute(
        select(KGNegotiation).where(KGNegotiation.entry_id == "eq1")
    )).scalars().all()
    assert len(rows) == 1
    assert rows[0].move == "paraphrase"
    assert rows[0].payload == {
        "surface_form": "rho * A * v is constant in pipes",
    }


@pytest.mark.asyncio
async def test_paraphrase_preserves_structural_content(store, neo, attempt):
    _seed(neo, attempt_id=attempt.id)
    out = await store.paraphrase_node(
        attempt_id=attempt.id, node_id="eq1", surface_form="my way",
    )
    # Structural fields untouched — only status + belief flipped.
    assert out.content.symbolic == "A1*v1 - A2*v2"
    assert out.content.label == "continuity"


# ---------------------------------------------------------------------------
# skip_node (SKIP)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_skip_node_sets_dual_no_belief(store, neo, db, attempt):
    _seed(neo, attempt_id=attempt.id)
    out = await store.skip_node(attempt_id=attempt.id, node_id="d1")
    assert out.status == "DUAL"
    assert out.student_belief is None

    rows = (await db.execute(
        select(KGNegotiation).where(KGNegotiation.entry_id == "d1")
    )).scalars().all()
    assert len(rows) == 1
    assert rows[0].move == "skip"
    assert rows[0].payload == {}


# ---------------------------------------------------------------------------
# get_node_trace
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_node_trace_orders_moves_chronologically(store, neo, db, attempt):
    _seed(neo, attempt_id=attempt.id)
    await store.mark_node_disputed(
        attempt_id=attempt.id, node_id="eq1", reason="r1",
    )
    await store.paraphrase_node(
        attempt_id=attempt.id, node_id="eq1", surface_form="s1",
    )
    await store.skip_node(attempt_id=attempt.id, node_id="eq1")

    trace = await store.get_node_trace(attempt_id=attempt.id, node_id="eq1")
    assert trace["node_id"] == "eq1"
    assert [m["move"] for m in trace["moves"]] == [
        "challenge", "paraphrase", "skip",
    ]
    assert trace["moves"][0]["payload"]["reason"] == "r1"
    assert trace["moves"][1]["payload"]["surface_form"] == "s1"


@pytest.mark.asyncio
async def test_get_node_trace_includes_latest_student_message(
    store, neo, db, attempt,
):
    _seed(neo, attempt_id=attempt.id)
    db.add(Message(
        session_id=attempt.session_id, attempt_id=attempt.id,
        role="student", content="A1 v1 = A2 v2", turn_index=0,
    ))
    db.add(Message(
        session_id=attempt.session_id, attempt_id=attempt.id,
        role="apollo", content="ok!", turn_index=1,
    ))
    db.add(Message(
        session_id=attempt.session_id, attempt_id=attempt.id,
        role="student", content="density doesn't matter here", turn_index=2,
    ))
    await db.commit()

    trace = await store.get_node_trace(attempt_id=attempt.id, node_id="eq1")
    # Latest student message — turn_index=2.
    assert trace["source_utterance"] == "density doesn't matter here"


@pytest.mark.asyncio
async def test_get_node_trace_returns_empty_when_no_moves(store, neo, attempt):
    _seed(neo, attempt_id=attempt.id)
    trace = await store.get_node_trace(attempt_id=attempt.id, node_id="eq1")
    assert trace["moves"] == []
    assert trace["source_utterance"] is None


# ---------------------------------------------------------------------------
# Freeze contract
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_negotiation_blocked_when_session_frozen(store, neo, db, attempt):
    _seed(neo, attempt_id=attempt.id)
    # Freeze the session.
    sess = (await db.execute(
        select(ApolloSession).where(ApolloSession.id == attempt.session_id)
    )).scalar_one()
    sess.phase = SessionPhase.SOLVING.value
    await db.commit()

    with pytest.raises(SessionFrozenError):
        await store.mark_node_disputed(
            attempt_id=attempt.id, node_id="eq1", reason="?",
        )
    with pytest.raises(SessionFrozenError):
        await store.paraphrase_node(
            attempt_id=attempt.id, node_id="eq1", surface_form="?",
        )
    with pytest.raises(SessionFrozenError):
        await store.skip_node(attempt_id=attempt.id, node_id="eq1")
