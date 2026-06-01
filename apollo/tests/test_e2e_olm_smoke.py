"""P3.12 — End-to-end Negotiable OLM smoke.

The legacy `test_e2e_smoke.py` is module-skipped pending a V3 rewrite.
This smoke verifies the OLM chain end-to-end across modules with stubbed
LLM and Neo4j boundaries:

    1. Student starts an attempt; parser writes a low-confidence node.
    2. Done-gate (P3.6 enabled) blocks with `review_required` listing
       that node.
    3. Student paraphrases the node — status flips to DUAL with a
       student_belief.
    4. Student skips a second flagged node — status DUAL, no belief.
    5. Coverage payload (P3.4) carries the student_belief for the
       paraphrased entry into the LLM payload.
    6. Done-gate clears (every flagged entry has a move).
    7. Diagnostic narration (P3.11) mentions the negotiation totals.

If any link breaks, the failing assertion pinpoints which join.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import MagicMock, patch

import json

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from apollo.errors import ReviewRequiredError
from apollo.handlers.done import _enforce_done_gate, _flagged_entries
from apollo.handlers.negotiate import (
    ChallengeRequest,
    ParaphraseRequest,
    handle_paraphrase,
    handle_skip,
)
from apollo.knowledge_graph.store import _node_to_neo4j_props
from apollo.ontology import KGGraph, NODE_LABELS, build_node
from apollo.overseer.coverage import compute_coverage
from apollo.overseer.diagnostic import _append_negotiation_line
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
# Fake Neo4j — same shape as the per-module fakes; lives next to this
# smoke for clarity. Pattern-matches on cypher from KGStore's negotiation
# methods.
# ---------------------------------------------------------------------------

class _Rec:
    def __init__(self, d): self._d = d
    def __getitem__(self, k): return self._d[k]


class _Sess:
    def __init__(self, nodes): self._n = nodes
    async def run(self, cypher: str, **params: Any):
        if "SET" in cypher and "RETURN n AS props" in cypher:
            key = (int(params["aid"]), str(params["nid"]))
            if key not in self._n:
                class _Empty:
                    async def single(self): return None
                return _Empty()
            n = self._n[key]
            if "n.status = 'DUAL', n.student_belief = $belief" in cypher:
                n["status"] = "DUAL"
                n["student_belief"] = params["belief"]
            elif "n.status = 'DUAL'" in cypher:
                n["status"] = "DUAL"
            elif "n.status = 'DISPUTED'" in cypher:
                n["status"] = "DISPUTED"
            class _R:
                async def single(_self):
                    return _Rec({
                        "props": dict(n["_bag"]) | {
                            "status": n["status"],
                            **({"student_belief": n["student_belief"]}
                               if n.get("student_belief") is not None else {}),
                        },
                        "labels": list(n["_labels"]),
                    })
            return _R()
        if "MATCH (n:_KGNode {attempt_id: $aid})" in cypher:
            aid = int(params["aid"])
            recs = []
            for (a, _), n in self._n.items():
                if a != aid: continue
                recs.append(_Rec({
                    "props": dict(n["_bag"]) | {
                        "status": n.get("status", "ACCEPTED"),
                        **({"student_belief": n["student_belief"]}
                           if n.get("student_belief") is not None else {}),
                    },
                    "labels": list(n["_labels"]),
                }))
            class _R:
                async def single(_self): return recs[0] if recs else None
                def __aiter__(_self):
                    async def gen():
                        for r in recs: yield r
                    return gen()
            return _R()
        # Edges + everything else: empty.
        class _R:
            async def single(_self): return None
            def __aiter__(_self):
                async def gen():
                    if False: yield
                return gen()
        return _R()


class _Neo:
    def __init__(self):
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
        yield _Sess(self.nodes)


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
async def attempt(db: AsyncSession):
    sess = ApolloSession(
        student_id="stu-1", concept_cluster_id="continuity",
        status=SessionStatus.active.value, phase=SessionPhase.TEACHING.value,
    )
    db.add(sess); await db.flush()
    a = ProblemAttempt(session_id=sess.id, problem_id="p1", difficulty="intro")
    db.add(a); await db.commit(); await db.refresh(sess); await db.refresh(a)
    return sess, a


# ---------------------------------------------------------------------------
# Smoke
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_negotiable_olm_chain_done_gate_to_coverage_to_narration(
    monkeypatch, db, attempt,
):
    """One pass through every join in the OLM chain. Each assertion
    pinpoints exactly which join broke if a regression lands."""
    monkeypatch.setenv("APOLLO_DONE_GATE_ENABLED", "1")

    sess, att = attempt
    neo = _Neo()

    # ---- Stage 1: parser writes nodes — one low-conf, one normal-conf,
    # plus an extra flagged via DISPUTED status to exercise both reasons.
    low_conf = build_node(
        node_type="equation", node_id="eq_low", attempt_id=att.id,
        source="parser",
        content={"symbolic": "A1*v1 - A2*v2", "label": "continuity"},
        parser_confidence=0.45,
    )
    disputed = build_node(
        node_type="definition", node_id="def_disp", attempt_id=att.id,
        source="parser",
        content={"concept": "density", "meaning": "stuff per place"},
        parser_confidence=0.95,
        status="DISPUTED",
    )
    high_conf = build_node(
        node_type="condition", node_id="c_ok", attempt_id=att.id,
        source="parser",
        content={"applies_when": "incompressible", "label": ""},
        parser_confidence=1.0,
    )
    for n in (low_conf, disputed, high_conf):
        neo.add_node(attempt_id=att.id, node=n)

    # ---- Stage 2: pre-move done-gate must block.
    pre_graph = await _read_graph(neo, att.id)
    flagged = _flagged_entries(pre_graph)
    assert {fid for n, _ in flagged for fid in [n.node_id]} == {"eq_low", "def_disp"}

    with pytest.raises(ReviewRequiredError) as exc_info:
        await _enforce_done_gate(db, attempt_id=att.id, graph=pre_graph)
    blocked_ids = {e["entry_id"] for e in exc_info.value.entries}
    assert blocked_ids == {"eq_low", "def_disp"}
    # Reasons surface correctly.
    reasons = {e["entry_id"]: e["reason"] for e in exc_info.value.entries}
    assert reasons["eq_low"] == "low_confidence"
    assert reasons["def_disp"] == "disputed"

    # ---- Stage 3: paraphrase the low-conf entry.
    out = await handle_paraphrase(
        db=db, neo=neo, session_id=sess.id, entry_id="eq_low",
        body=ParaphraseRequest(surface_form="ρ A v stays the same"),
    )
    assert out["entry"]["status"] == "DUAL"
    assert out["entry"]["student_belief"] == "ρ A v stays the same"

    # ---- Stage 4: skip the disputed entry.
    out = await handle_skip(
        db=db, neo=neo, session_id=sess.id, entry_id="def_disp",
    )
    assert out["entry"]["status"] == "DUAL"
    assert out["entry"]["student_belief"] is None

    # ---- Stage 5: done-gate clears.
    post_graph = await _read_graph(neo, att.id)
    # After moves, both flagged entries are now DUAL — _flagged_entries skips DUAL.
    assert _flagged_entries(post_graph) == []
    await _enforce_done_gate(db, attempt_id=att.id, graph=post_graph)  # no raise

    # ---- Stage 6: coverage payload carries student_belief into the LLM call.
    captured: dict = {}

    def _create(**kwargs):
        captured.setdefault("bodies", []).append(
            json.loads(kwargs["messages"][1]["content"])
        )
        return MagicMock(choices=[MagicMock(message=MagicMock(
            content='{"matches":[]}'
        ))])

    # A minimal reference graph with one equation triggers exactly one
    # binary-type LLM call (compute_coverage skips types with no refs).
    reference_graph = KGGraph(nodes=[
        build_node(
            node_type="equation", node_id="ref_eq", attempt_id=att.id,
            source="reference",
            content={"symbolic": "A1*v1 - A2*v2", "label": "continuity"},
        ),
    ])
    with patch("apollo.overseer.coverage.OpenAI") as mc:
        mc.return_value.chat.completions.create.side_effect = _create
        cov = compute_coverage(post_graph, reference_graph)

    # Among the bodies sent (one per binary type), find the equation body
    # and assert the paraphrased entry's student_belief reaches it.
    eq_bodies = [b for b in captured.get("bodies", []) if b.get("entry_type") == "equation"]
    assert eq_bodies, "expected one equation-type LLM call"
    student_entries = eq_bodies[0]["student_entries"]
    eq_low_payload = next(
        (e for e in student_entries
         if e.get("symbolic") == "A1*v1 - A2*v2"), None,
    )
    assert eq_low_payload is not None
    assert eq_low_payload["student_belief"] == "ρ A v stays the same"

    # ---- Stage 7: negotiation_counts reflect 1 paraphrase + 1 skip.
    counts = cov["negotiation_counts"]
    assert counts["paraphrased"] == 1
    assert counts["skipped"] == 1
    assert counts["disputed"] == 0  # def_disp was DUAL'd via skip
    assert counts["dual"] == 2

    # ---- Stage 8: diagnostic narration line names the totals.
    narrative = _append_negotiation_line("All set.", cov)
    assert "2 entries with Apollo:" in narrative
    assert "1 paraphrased" in narrative
    assert "1 skipped" in narrative
    # Generic phrasing — no entry id, no surface form leaked.
    assert "eq_low" not in narrative
    assert "def_disp" not in narrative
    assert "ρ A v" not in narrative


# ---------------------------------------------------------------------------
# Helper: read the per-attempt graph through the same code path the
# Done-gate uses, so the smoke exercises read_graph + the fake.
# ---------------------------------------------------------------------------

async def _read_graph(neo: _Neo, attempt_id: int) -> KGGraph:
    """Mimic KGStore.read_graph against the fake. Doesn't bother with
    edges — the gate doesn't read them."""
    nodes = []
    async with neo.session() as s:
        result = await s.run(
            "MATCH (n:_KGNode {attempt_id: $aid}) RETURN n AS props, labels(n) AS labels",
            aid=attempt_id,
        )
        async for rec in result:
            from apollo.knowledge_graph.store import _record_to_node
            nodes.append(_record_to_node(dict(rec["props"]), list(rec["labels"])))
    return KGGraph(nodes=nodes, edges=[])
