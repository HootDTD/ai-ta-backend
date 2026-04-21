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
from apollo.persistence.models import ApolloSession, KGEntry, Message, ProblemAttempt
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
    })
    assert r.status_code == 200, r.text
    start = r.json()
    session_id = start["session_id"]
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
    for axis in ("overall", "procedure", "justification", "simplification", "variables"):
        assert axis in done["rubric"], f"missing axis {axis!r} in rubric"
        assert "score" in done["rubric"][axis]
        assert "letter" in done["rubric"][axis]
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
    })
    assert r.status_code == 409
    assert r.json()["error_code"] == "no_matching_concept"
