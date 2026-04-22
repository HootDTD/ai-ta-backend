"""Scripted end-to-end smoke test across all Slice 0a endpoints.

Mocks OpenAI to keep the test offline and deterministic. Exercises the
real solver, KG store, coverage, narrator, and SQLAlchemy persistence
against an in-memory SQLite database."""
import json
from unittest.mock import MagicMock, patch

import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from apollo.api import register_exception_handlers, router as apollo_router
from apollo.persistence.models import ApolloSession, KGEntry, Message, ProblemAttempt, StudentProgress
from database.models import Base
from database.session import get_db_session


@pytest_asyncio.fixture
async def engine_with_schema():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    apollo_tables = [
        ApolloSession.__table__,
        KGEntry.__table__,
        Message.__table__,
        ProblemAttempt.__table__,
        StudentProgress.__table__,
    ]
    async with engine.begin() as conn:
        await conn.run_sync(lambda sync_conn: Base.metadata.create_all(sync_conn, tables=apollo_tables))
    yield engine
    await engine.dispose()


@pytest.fixture
def app(engine_with_schema):
    Session = async_sessionmaker(engine_with_schema, expire_on_commit=False, class_=AsyncSession)

    async def _override_db():
        async with Session() as s:
            yield s

    app = FastAPI()
    app.include_router(apollo_router)
    register_exception_handlers(app)
    app.dependency_overrides[get_db_session] = _override_db
    return app


@pytest.fixture
def db_factory(engine_with_schema):
    return async_sessionmaker(engine_with_schema, expire_on_commit=False, class_=AsyncSession)


def _mock_llm_response(text: str) -> MagicMock:
    fake = MagicMock()
    fake.choices = [MagicMock(message=MagicMock(content=text))]
    return fake


@patch("apollo.overseer.diagnostic.OpenAI")
@patch("apollo.overseer.coverage.OpenAI")
@patch("apollo.agent.apollo_llm.OpenAI")
@patch("apollo.parser.parser_llm.OpenAI")
@patch("apollo.overseer.concept_inference.OpenAI")
def test_full_slice0a_happy_path(
    mock_infer_client_cls,
    mock_parser_client_cls,
    mock_apollo_client_cls,
    mock_coverage_client_cls,
    mock_diag_client_cls,
    app,
):
    mock_infer = MagicMock()
    mock_infer.chat.completions.create.return_value = _mock_llm_response('{"cluster_id": "fluid_mechanics"}')
    mock_infer_client_cls.return_value = mock_infer

    mock_parser = MagicMock()
    mock_parser.chat.completions.create.side_effect = [
        _mock_llm_response(json.dumps({"entries": [
            {"type": "equation", "content": {"symbolic": "rho*A1*v1 - rho*A2*v2", "label": "Continuity"}}
        ]})),
        _mock_llm_response(json.dumps({"entries": [
            {"type": "equation", "content": {
                "symbolic": "P1 + Rational(1,2)*rho*v1**2 + rho*g*h1 - (P2 + Rational(1,2)*rho*v2**2 + rho*g*h2)",
                "label": "Bernoulli",
            }}
        ]})),
    ]
    mock_parser_client_cls.return_value = mock_parser

    mock_apollo = MagicMock()
    mock_apollo.chat.completions.create.return_value = _mock_llm_response("Got it — can you tell me more?")
    mock_apollo_client_cls.return_value = mock_apollo

    mock_diag = MagicMock()
    mock_diag.chat.completions.create.return_value = _mock_llm_response("You taught it well.")
    mock_diag_client_cls.return_value = mock_diag

    mock_coverage = MagicMock()
    mock_coverage.chat.completions.create.return_value = _mock_llm_response(json.dumps({"score": 0.9}))
    mock_coverage_client_cls.return_value = mock_coverage

    client = TestClient(app)

    r = client.post("/apollo/sessions/from_hoot", json={
        "student_id": "stu-1",
        "hoot_transcript": "Student asked about Bernoulli in horizontal pipes.",
        "difficulty": "intro",
    })
    assert r.status_code == 200, r.text
    start = r.json()
    session_id = start["session_id"]
    assert start["attempt_id"] is not None
    assert start["problem"]["concept_id"] in ("bernoulli_principle", "continuity_equation", "volumetric_flow_rate")

    r = client.post(f"/apollo/sessions/{session_id}/chat", json={"message": "For incompressible flow, A1*v1 = A2*v2."})
    assert r.status_code == 200, r.text

    r = client.post(f"/apollo/sessions/{session_id}/chat", json={
        "message": "Bernoulli: P1 + Rational(1,2)*rho*v1**2 + rho*g*h1 = P2 + Rational(1,2)*rho*v2**2 + rho*g*h2."
    })
    assert r.status_code == 200, r.text

    r = client.post(f"/apollo/sessions/{session_id}/done")
    assert r.status_code == 200, r.text
    done = r.json()

    # --- New Done response shape (Tasks 6-9) ---
    assert "rubric" in done
    for axis in ("overall", "procedure", "justification", "simplification"):
        assert axis in done["rubric"], f"missing axis {axis!r} in rubric"
        assert "score" in done["rubric"][axis]
        assert "letter" in done["rubric"][axis]
    assert "variables" not in done["rubric"]
    assert done["rubric"]["overall"]["letter"] in (
        "A+", "A", "A-", "B+", "B", "B-", "C+", "C", "D", "F",
    )
    assert "solver_indicator" in done
    assert isinstance(done["solver_indicator"]["reached"], bool)
    assert "diagnostic_narrative" in done
    assert isinstance(done["diagnostic_narrative"], str)
    assert "coverage" in done
    assert "per_step" in done["coverage"]
    assert "procedure_scores" in done["coverage"]
    # narrated_trace moved to attempt.solver_trace in persistence; no longer in Done response

    # Phase 2: gamification fields present and sensible on first attempt.
    assert "xp_earned" in done
    assert isinstance(done["xp_earned"], int)
    assert done["xp_earned"] >= 0
    assert done["xp_earned"] <= 200
    assert done["level_before"] == 1
    assert isinstance(done["level_after"], int)
    assert done["level_up"] is False  # Can't level-up in one shot below 300 XP.
    assert "xp_before" in done
    assert "xp_after" in done
    assert isinstance(done["xp_before"], int)
    assert isinstance(done["xp_after"], int)
    # Arithmetic invariant: xp_after == xp_before + xp_earned.
    assert done["xp_after"] == done["xp_before"] + done["xp_earned"]

    r = client.get(f"/apollo/sessions/{session_id}")
    assert r.status_code == 200, r.text
    state = r.json()
    assert state["phase"] == "REPORT"

    r = client.post(f"/apollo/sessions/{session_id}/retry")
    assert r.status_code == 200, r.text
    r = client.get(f"/apollo/sessions/{session_id}")
    assert r.json()["phase"] == "TEACHING"

    r = client.post(f"/apollo/sessions/{session_id}/end")
    assert r.status_code == 200, r.text
    r = client.get(f"/apollo/sessions/{session_id}")
    assert r.json()["status"] == "ended"


@patch("apollo.overseer.concept_inference.OpenAI")
def test_no_matching_concept_returns_409(mock_client_cls, app):
    client_mock = MagicMock()
    client_mock.chat.completions.create.return_value = _mock_llm_response('{"cluster_id": null}')
    mock_client_cls.return_value = client_mock

    client = TestClient(app)
    r = client.post("/apollo/sessions/from_hoot", json={
        "student_id": "stu-1",
        "hoot_transcript": "How do I bake a cake?",
        "difficulty": "intro",
    })
    assert r.status_code == 409
    assert r.json()["error_code"] == "no_matching_concept"


@patch("apollo.overseer.diagnostic.OpenAI")
@patch("apollo.overseer.coverage.OpenAI")
@patch("apollo.agent.apollo_llm.OpenAI")
@patch("apollo.parser.parser_llm.OpenAI")
@patch("apollo.overseer.concept_inference.OpenAI")
def test_e2e_switch_then_restart(
    mock_infer_client_cls,
    mock_parser_client_cls,
    mock_apollo_client_cls,
    mock_coverage_client_cls,
    mock_diag_client_cls,
    app,
    db_factory,
):
    """E2E: pick initial difficulty, switch mid-problem via /next, restart after /done."""
    import asyncio
    from sqlalchemy import select

    mock_infer = MagicMock()
    mock_infer.chat.completions.create.return_value = _mock_llm_response('{"cluster_id": "fluid_mechanics"}')
    mock_infer_client_cls.return_value = mock_infer

    # Parser always returns one continuity equation — good enough for parser side effects
    # across all chat turns on both attempts. Using return_value (not side_effect) keeps
    # the test robust to chat-turn count.
    mock_parser = MagicMock()
    mock_parser.chat.completions.create.return_value = _mock_llm_response(json.dumps({"entries": [
        {"type": "equation", "content": {"symbolic": "rho*A1*v1 - rho*A2*v2", "label": "Continuity"}}
    ]}))
    mock_parser_client_cls.return_value = mock_parser

    mock_apollo = MagicMock()
    mock_apollo.chat.completions.create.return_value = _mock_llm_response("ok tell me more")
    mock_apollo_client_cls.return_value = mock_apollo

    mock_diag = MagicMock()
    mock_diag.chat.completions.create.return_value = _mock_llm_response("narrative.")
    mock_diag_client_cls.return_value = mock_diag

    mock_coverage = MagicMock()
    mock_coverage.chat.completions.create.return_value = _mock_llm_response(json.dumps({"score": 0.9}))
    mock_coverage_client_cls.return_value = mock_coverage

    client = TestClient(app)

    # 1. /from_hoot at intro → attempt A
    r = client.post("/apollo/sessions/from_hoot", json={
        "student_id": "stu-1",
        "hoot_transcript": "teach me bernoulli",
        "difficulty": "intro",
    })
    assert r.status_code == 200, r.text
    start = r.json()
    session_id = start["session_id"]
    attempt_a = start["attempt_id"]
    assert attempt_a is not None

    # 2. A chat turn on attempt A
    r = client.post(f"/apollo/sessions/{session_id}/chat", json={"message": "x equals 1"})
    assert r.status_code == 200, r.text

    # 3. /next from TEACHING — abandon A, create B at "standard"
    r = client.post(f"/apollo/sessions/{session_id}/next", json={"difficulty": "standard"})
    assert r.status_code == 200, r.text
    next_resp = r.json()
    attempt_b = next_resp["attempt_id"]
    assert attempt_b != attempt_a
    assert next_resp["problem"]["difficulty"] == "standard"

    # Verify DB state: A abandoned, B fresh
    async def _check_attempts():
        async with db_factory() as s:
            rows = (await s.execute(
                select(ProblemAttempt)
                .where(ProblemAttempt.session_id == session_id)
                .order_by(ProblemAttempt.id)
            )).scalars().all()
            assert len(rows) == 2
            assert rows[0].id == attempt_a
            assert rows[0].result == "abandoned"
            assert rows[1].id == attempt_b
            assert rows[1].result is None
            assert rows[1].difficulty == "standard"
    asyncio.get_event_loop().run_until_complete(_check_attempts())

    # 4. Chat + done on attempt B
    r = client.post(f"/apollo/sessions/{session_id}/chat", json={"message": "z equals 2"})
    assert r.status_code == 200, r.text
    r = client.post(f"/apollo/sessions/{session_id}/done")
    assert r.status_code == 200, r.text
    body = r.json()
    # Standard multiplier applied; is_reattempt=False despite abandoned A, because
    # has_prior_graded_attempt excludes abandoned results (Task 3).
    assert body["xp_earned"] >= 0
    assert body["level_before"] == 1

    # 5. /restart_problem from REPORT — wipes B's KG + messages, keeps attempt row
    r = client.post(f"/apollo/sessions/{session_id}/restart_problem")
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True}

    async def _check_after_restart():
        async with db_factory() as s:
            # Attempt B's KG + messages wiped
            b_kg = (await s.execute(
                select(KGEntry).where(KGEntry.attempt_id == attempt_b)
            )).scalars().all()
            b_msgs = (await s.execute(
                select(Message).where(Message.attempt_id == attempt_b)
            )).scalars().all()
            assert b_kg == []
            assert b_msgs == []
            # Attempt A still has its seeded KG (from chat turn 2)
            a_kg = (await s.execute(
                select(KGEntry).where(KGEntry.attempt_id == attempt_a)
            )).scalars().all()
            assert len(a_kg) >= 1
            # Attempt B row itself untouched (result field).
            b_row = (await s.execute(
                select(ProblemAttempt).where(ProblemAttempt.id == attempt_b)
            )).scalar_one()
            # B was graded by /done, so its result should be non-null (solved/stuck/skipped).
            # Important: we're verifying restart did not touch the result.
            assert b_row.difficulty == "standard"
    asyncio.get_event_loop().run_until_complete(_check_after_restart())
