"""WU-5B3a-1 — H3: one real full drain reading the frozen graph from REAL Neo4j.

The ONLY test that exercises the real Neo4j read path end-to-end through the
drain: ``build_rerun_inputs`` (``read_graph`` + ``read_node_graded_at``) and
``run_graph_simulation`` (``write_resolution`` + the canonicalize/grade/audit
chain) all hit a real ``neo4j:5.25`` Testcontainers graph. ONLY the LLM is stubbed
(``main_chat_adjudicator`` / ``main_chat_auditor``); Neo4j is NOT.

It lives under ``tests/database/`` so the Testcontainers ``neo4j_client``
(``tests/conftest.py``) is the fixture in scope — NOT the live-Aura ``neo4j_client``
defined in ``apollo/conftest.py`` (which skips when ``NEO4J_URI`` is unset).

The load-bearing assertion: the persisted ``LearnerState.last_evidence_at`` equals
the STAMPED ``graded_at`` instant (the frozen Neo4j done_ts), NOT ``now()`` —
proving the belief Δt anchor reads the frozen instant the drain rebuilt from
durable state.

MUST RUN GREEN (not skip) with Docker up — a skip is a FAIL of the real-infra gate.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from unittest.mock import patch

import pytest
from sqlalchemy import select

from apollo.conftest import TEST_USER_ID
from apollo.handlers import learner_janitor
from apollo.handlers.learner_janitor import DrainResult, drain_pending_attempts
from apollo.knowledge_graph.store import (
    _NODE_CREATE_CYPHER,
    KGStore,
    _node_to_neo4j_props,
)
from apollo.ontology import NODE_LABELS, build_node
from apollo.persistence.models import (
    ApolloSession,
    KGEntity,
    LearnerState,
    Message,
    ProblemAttempt,
    SessionPhase,
    SessionStatus,
)
from apollo.subjects.tests._curriculum_fixtures import (
    load_bernoulli_problem_payloads,
    seed_course,
)

pytestmark = pytest.mark.integration

_CONTINUITY = "eq.continuity"
_GRADED_AT = datetime(2026, 6, 18, 0, 0, 2, tzinfo=UTC)
_INTRO = [p for p in load_bernoulli_problem_payloads() if p["difficulty"] == "intro"]


def _adjudicator_stub(_request):
    return {"stu_continuity": _CONTINUITY}


def _auditor_stub(_request):
    return {"spans": {}}


def _student_node(attempt_id: int):
    return build_node(
        node_type="equation",
        node_id="stu_continuity",
        attempt_id=attempt_id,
        source="parser",
        content={
            "symbolic": "rho*A1*v1 - rho*A2*v2",
            "label": "continuity",
            "variables": [],
        },
        parser_confidence=0.95,
    )


async def _write_student_node(neo, attempt_id: int) -> None:
    """Write the per-attempt student node directly to real Neo4j (mirrors the
    store-test ``_write_nodes_direct``) so ``read_graph`` returns it."""
    node = _student_node(attempt_id)
    async with neo.session() as s:
        label = NODE_LABELS[node.node_type]
        row = _node_to_neo4j_props(node)
        row["source"] = "parser"
        row["attempt_id"] = attempt_id
        await s.run(_NODE_CREATE_CYPHER[label], rows=[row])


def _good_report() -> dict:
    return {
        "narrative": "n",
        "rubric": {"overall": {"letter": "B", "score": 0.8}},
        "coverage": {},
    }


async def _seed_pg(db) -> tuple[int, ProblemAttempt]:
    sid, cid, codes = await seed_course(
        db,
        subject_slug="fluid_mechanics",
        concept_slug="bernoulli_principle",
        problems=_INTRO,
    )
    db.add(
        KGEntity(
            concept_id=cid,
            canonical_key=_CONTINUITY,
            kind="equation",
            display_name="Continuity",
            payload={},
            aliases=[],
        )
    )
    await db.flush()
    sess = ApolloSession(
        user_id=TEST_USER_ID,
        search_space_id=sid,
        concept_id=cid,
        status=SessionStatus.active.value,
        phase=SessionPhase.REPORT.value,
        current_problem_id=codes[0],
    )
    db.add(sess)
    await db.flush()
    attempt = ProblemAttempt(
        session_id=sess.id,
        problem_id=codes[0],
        difficulty="intro",
        result="graded",
        diagnostic_report=_good_report(),
        learner_update_pending=True,
    )
    db.add(attempt)
    await db.flush()
    db.add(
        Message(
            session_id=sess.id,
            attempt_id=attempt.id,
            role="student",
            content="continuity says rho A1 v1 equals rho A2 v2",
            turn_index=0,
        )
    )
    await db.flush()
    await db.commit()
    return sess.id, attempt


@asynccontextmanager
async def _bound_session(db):
    yield db


async def test_full_drain_reads_frozen_graph_from_real_neo4j(db_session, neo4j_client, monkeypatch):
    monkeypatch.setenv("APOLLO_GRAPH_SIM_SHADOW_ENABLED", "true")
    monkeypatch.setenv("APOLLO_GRAPH_SIM_LAYER3_ENABLED", "1")

    _sid, attempt = await _seed_pg(db_session)

    # Write the per-attempt student subgraph to REAL Neo4j + stamp a real graded_at.
    await _write_student_node(neo4j_client, attempt.id)
    store = KGStore(db_session, neo4j_client)
    stamped = await store.stamp_graded_at(attempt_id=attempt.id, ts=_GRADED_AT)
    assert stamped == 1  # the one node carries the frozen done_ts

    monkeypatch.setattr(learner_janitor, "get_async_session", lambda: _bound_session(db_session))

    # Stub ONLY the LLM; Neo4j is the real Testcontainers graph.
    with (
        patch("apollo.handlers.done_grading.main_chat_adjudicator", new=_adjudicator_stub),
        patch("apollo.handlers.done_grading.main_chat_auditor", new=_auditor_stub),
    ):
        result = await drain_pending_attempts(neo4j_client, limit=1)

    assert result == DrainResult(
        claimed=1,
        succeeded=1,
        dead_lettered=0,
        retried=0,
        deferred=0,
        backlog_remaining=0,
    )

    refreshed = (
        await db_session.execute(
            select(ProblemAttempt)
            .where(ProblemAttempt.id == attempt.id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    assert refreshed.learner_update_pending is False

    # The Δt anchor is the FROZEN Neo4j graded_at instant the drain rebuilt — NOT
    # now(). build_rerun_inputs read the real graded_at off the Testcontainers node.
    state = (
        await db_session.execute(select(LearnerState).where(LearnerState.user_id == TEST_USER_ID))
    ).scalar_one()
    assert state.last_evidence_at == _GRADED_AT
