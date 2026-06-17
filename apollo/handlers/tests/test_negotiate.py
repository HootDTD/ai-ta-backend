"""P3.3 — Negotiable OLM endpoint handlers.

Tests `handle_challenge`, `handle_paraphrase`, `handle_skip`, and
`handle_get_trace` end-to-end through the in-memory SQLite + a fake
Neo4j client. Validation rules on the Pydantic request models are
tested directly.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

import pytest
import pytest_asyncio
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from apollo.conftest import TEST_SPACE_ID, TEST_USER_ID
from apollo.errors import InvalidPhaseError, KGEntryNotFoundError
from apollo.handlers.negotiate import (
    ChallengeRequest,
    ParaphraseRequest,
    SkipRequest,
    handle_challenge,
    handle_get_trace,
    handle_paraphrase,
    handle_skip,
)
from apollo.knowledge_graph.store import _node_to_neo4j_props
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

# ---------------------------------------------------------------------------
# Fake Neo4j — mirrors the one in test_store_negotiation. Lives alongside
# the handler test so each surface owns its own fake without an import.
# ---------------------------------------------------------------------------

class _FakeRecord:
    def __init__(self, data: dict[str, Any]) -> None:
        self._data = data
    def __getitem__(self, key: str) -> Any:
        return self._data[key]


class _FakeResult:
    def __init__(self, record: _FakeRecord | None) -> None:
        self._rec = record
    async def single(self) -> _FakeRecord | None:
        return self._rec
    def __aiter__(self):
        async def gen():
            if self._rec is not None:
                yield self._rec
        return gen()


class _FakeNeo4jSession:
    def __init__(self, nodes: dict[tuple[int, str], dict[str, Any]]) -> None:
        self._nodes = nodes

    async def run(self, cypher: str, **params: Any) -> _FakeResult:
        # Negotiation SET-and-RETURN paths.
        if "SET" in cypher and "RETURN n AS props" in cypher:
            aid = int(params["aid"]); nid = str(params["nid"])
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
            return _FakeResult(_FakeRecord({
                "props": dict(node["_bag"]) | {
                    "status": node["status"],
                    **({"student_belief": node["student_belief"]}
                       if node.get("student_belief") is not None else {}),
                },
                "labels": list(node["_labels"]),
            }))

        # read_graph paths — the snapshot helper calls this.
        if "MATCH (n:_KGNode {attempt_id: $aid})" in cypher and "RETURN n AS props" in cypher:
            aid = int(params["aid"])
            records: list[_FakeRecord] = []
            for (a, _), node in self._nodes.items():
                if a != aid:
                    continue
                records.append(_FakeRecord({
                    "props": dict(node["_bag"]) | {
                        "status": node.get("status", "ACCEPTED"),
                        **({"student_belief": node["student_belief"]}
                           if node.get("student_belief") is not None else {}),
                    },
                    "labels": list(node["_labels"]),
                }))

            class _MultiResult:
                def __init__(self, recs):
                    self._recs = recs
                async def single(self):
                    return self._recs[0] if self._recs else None
                def __aiter__(self):
                    async def gen():
                        for r in self._recs: yield r
                    return gen()

            return _MultiResult(records)  # type: ignore[return-value]

        if "MATCH (a:_KGNode" in cypher:
            # No edges in fake.
            class _Empty:
                def __aiter__(self):
                    async def gen():
                        if False: yield
                    return gen()
                async def single(self): return None
            return _Empty()  # type: ignore[return-value]

        raise AssertionError(f"unsupported cypher in fake: {cypher!r}")


class _FakeNeo4jClient:
    def __init__(self) -> None:
        self.nodes: dict[tuple[int, str], dict[str, Any]] = {}

    def add_node(self, *, attempt_id: int, node):
        bag = _node_to_neo4j_props(node)
        bag["source"] = node.source
        bag["attempt_id"] = attempt_id
        self.nodes[(attempt_id, node.node_id)] = {
            "_bag": bag,
            "_labels": [NODE_LABELS[node.node_type], "_KGNode"],
            "status": node.status,
            "student_belief": node.student_belief,
        }

    @asynccontextmanager
    async def session(self):
        yield _FakeNeo4jSession(self.nodes)


# ---------------------------------------------------------------------------
# Fixtures
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
async def session(db: AsyncSession):
    s = ApolloSession(
        user_id=TEST_USER_ID, search_space_id=TEST_SPACE_ID, concept_id=1,
        status=SessionStatus.active.value, phase=SessionPhase.TEACHING.value,
    )
    db.add(s)
    await db.flush()
    a = ProblemAttempt(session_id=s.id, problem_id="p1", difficulty="intro")
    db.add(a)
    await db.commit()
    await db.refresh(s); await db.refresh(a)
    return s, a


@pytest.fixture
def neo():
    return _FakeNeo4jClient()


def _seed(neo: _FakeNeo4jClient, attempt_id: int):
    eq = build_node(
        node_type="equation", node_id="eq1", attempt_id=attempt_id,
        source="parser",
        content={"symbolic": "A1*v1 - A2*v2", "label": "continuity"},
        parser_confidence=0.45,
    )
    neo.add_node(attempt_id=attempt_id, node=eq)


# ---------------------------------------------------------------------------
# handle_challenge
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_challenge_returns_updated_entry_and_kg_envelope(db, session, neo):
    _, attempt = session
    _seed(neo, attempt.id)

    out = await handle_challenge(
        db=db, neo=neo, session_id=attempt.session_id, entry_id="eq1",
        body=ChallengeRequest(reason="that's not what I said"),
    )
    assert out["move"] == "challenge"
    assert out["entry"]["status"] == "DISPUTED"
    assert out["entry"]["node_id"] == "eq1"
    # KG envelope re-read from the (fake) graph.
    assert "nodes" in out["kg"]
    assert any(n["node_id"] == "eq1" for n in out["kg"]["nodes"])

    rows = (await db.execute(
        select(KGNegotiation).where(KGNegotiation.entry_id == "eq1")
    )).scalars().all()
    assert len(rows) == 1
    assert rows[0].move == "challenge"
    assert rows[0].payload == {"reason": "that's not what I said"}


@pytest.mark.asyncio
async def test_challenge_404_for_unknown_entry(db, session, neo):
    _, attempt = session
    _seed(neo, attempt.id)
    with pytest.raises(KGEntryNotFoundError):
        await handle_challenge(
            db=db, neo=neo, session_id=attempt.session_id, entry_id="ghost",
            body=ChallengeRequest(reason="?"),
        )


@pytest.mark.asyncio
async def test_challenge_invalid_phase_when_no_attempt(db, neo):
    """Sessions with no ProblemAttempt — student hasn't started a problem
    yet — get InvalidPhaseError. The exception handler maps to 409."""
    s = ApolloSession(
        user_id=TEST_USER_ID, search_space_id=TEST_SPACE_ID, concept_id=1,
        status=SessionStatus.active.value, phase=SessionPhase.TEACHING.value,
    )
    db.add(s); await db.commit(); await db.refresh(s)
    with pytest.raises(InvalidPhaseError):
        await handle_challenge(
            db=db, neo=neo, session_id=s.id, entry_id="eq1",
            body=ChallengeRequest(reason="?"),
        )


# ---------------------------------------------------------------------------
# handle_paraphrase
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_paraphrase_sets_dual_with_belief(db, session, neo):
    _, attempt = session
    _seed(neo, attempt.id)

    out = await handle_paraphrase(
        db=db, neo=neo, session_id=attempt.session_id, entry_id="eq1",
        body=ParaphraseRequest(surface_form="rho A v stays the same"),
    )
    assert out["move"] == "paraphrase"
    assert out["entry"]["status"] == "DUAL"
    assert out["entry"]["student_belief"] == "rho A v stays the same"


@pytest.mark.asyncio
async def test_paraphrase_404_for_unknown_entry(db, session, neo):
    _, attempt = session
    _seed(neo, attempt.id)
    with pytest.raises(KGEntryNotFoundError):
        await handle_paraphrase(
            db=db, neo=neo, session_id=attempt.session_id, entry_id="missing",
            body=ParaphraseRequest(surface_form="x"),
        )


# ---------------------------------------------------------------------------
# handle_skip
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_skip_sets_dual_no_belief(db, session, neo):
    _, attempt = session
    _seed(neo, attempt.id)

    out = await handle_skip(
        db=db, neo=neo, session_id=attempt.session_id, entry_id="eq1",
    )
    assert out["move"] == "skip"
    assert out["entry"]["status"] == "DUAL"
    assert out["entry"]["student_belief"] is None


# ---------------------------------------------------------------------------
# handle_get_trace
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_trace_returns_chronological_moves(db, session, neo):
    _, attempt = session
    _seed(neo, attempt.id)
    await handle_challenge(
        db=db, neo=neo, session_id=attempt.session_id, entry_id="eq1",
        body=ChallengeRequest(reason="r1"),
    )
    await handle_skip(
        db=db, neo=neo, session_id=attempt.session_id, entry_id="eq1",
    )
    trace = await handle_get_trace(
        db=db, neo=neo, session_id=attempt.session_id, entry_id="eq1",
    )
    assert [m["move"] for m in trace["moves"]] == ["challenge", "skip"]
    assert trace["node_id"] == "eq1"


# ---------------------------------------------------------------------------
# Pydantic validation
# ---------------------------------------------------------------------------

def test_challenge_reason_min_length():
    with pytest.raises(ValidationError):
        ChallengeRequest(reason="")


def test_challenge_reason_max_length_500():
    """Long reasons are rejected — keeps the FE textarea contract honest."""
    with pytest.raises(ValidationError):
        ChallengeRequest(reason="x" * 501)
    # 500 is OK.
    ChallengeRequest(reason="x" * 500)


def test_paraphrase_surface_form_min_length():
    with pytest.raises(ValidationError):
        ParaphraseRequest(surface_form="")


def test_paraphrase_surface_form_max_length_1000():
    with pytest.raises(ValidationError):
        ParaphraseRequest(surface_form="y" * 1001)
    ParaphraseRequest(surface_form="y" * 1000)


def test_skip_request_rejects_extra_fields():
    """SkipRequest with extras forbidden — guards against accidental
    payload leaking through (which would silently be ignored under the
    SQLAlchemy default JSONB pass-through)."""
    SkipRequest()  # fine
    with pytest.raises(ValidationError):
        SkipRequest(reason="not allowed")  # type: ignore[call-arg]
