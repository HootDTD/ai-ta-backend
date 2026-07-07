"""Endpoint tests for the student browse surface (GET /apollo/concepts,
GET /apollo/problems) and the standalone session entry (POST /apollo/sessions)."""

from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import apollo.auth_deps as deps
from apollo.api import get_neo4j_client, register_exception_handlers
from apollo.api import router as apollo_router
from apollo.conftest import TEST_SPACE_ID, TEST_USER_ID, TEST_USER_ID_2
from apollo.persistence.models import (
    ApolloSession,
    Concept,
    ConceptProblem,
    ProblemAttempt,
    StudentProgress,
    Subject,
)
from auth import AuthContext
from database.models import Base
from database.session import get_db_session

TABLES = [
    Subject.__table__,
    Concept.__table__,
    ConceptProblem.__table__,
    ApolloSession.__table__,
    ProblemAttempt.__table__,
    StudentProgress.__table__,
]


def _full_problem_payload(code: str, concept_id: int, difficulty: str = "intro") -> dict:
    """A payload that survives Problem.model_validate (unlike
    minimal_problem_payload, which only carries id+difficulty)."""
    return {
        "id": code,
        "concept_id": str(concept_id),
        "difficulty": difficulty,
        "problem_text": f"Problem {code}: a cart accelerates from rest.",
        "given_values": {"m": 2.0, "a": 3.0},
        "target_unknown": "F",
        "reference_solution": [
            {
                "step": 1,
                "entry_type": "equation",
                "id": f"{code}-s1",
                "content": {"symbolic": "F = m*a"},
                "depends_on": [],
            }
        ],
    }


def _sqlite_create_all(sync_conn) -> None:
    """create_all(TABLES) on SQLite. A few curriculum columns carry a
    Postgres-only ``server_default=text("'{}'::jsonb")`` (e.g.
    ConceptProblem.provenance) that SQLite's DDL compiler rejects
    (``unrecognized token: ":"``). Those defaults only matter to the
    Postgres runtime — ORM inserts apply the Python-side ``default=dict``
    instead — so we null them out for the duration of CREATE TABLE and
    restore them afterward, leaving the shared model metadata untouched."""
    saved: list[tuple] = []
    for table in TABLES:
        for column in table.columns:
            sd = column.server_default
            if sd is not None and "::" in str(getattr(sd, "arg", "")):
                saved.append((column, sd))
                column.server_default = None
    try:
        Base.metadata.create_all(sync_conn, tables=TABLES)
    finally:
        for column, sd in saved:
            column.server_default = sd


@pytest.fixture
def client_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")

    async def _bootstrap():
        async with engine.begin() as conn:
            await conn.run_sync(_sqlite_create_all)

    asyncio.run(_bootstrap())
    Session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

    async def _override_db():
        async with Session() as s:
            yield s

    def _fake_neo():
        return None

    app = FastAPI()
    app.include_router(apollo_router)
    register_exception_handlers(app)
    app.dependency_overrides[get_db_session] = _override_db
    app.dependency_overrides[get_neo4j_client] = _fake_neo
    return app, Session


def _auth_as(monkeypatch, user_id: str) -> None:
    monkeypatch.setattr(
        deps, "resolve_auth_context",
        lambda _request: AuthContext(user_id=user_id, access_token="tok"),
    )
    async def _is_member(db, *, user_id, search_space_id):
        return True
    monkeypatch.setattr(deps, "has_membership", _is_member)


async def _seed_curriculum(Session, *, n_teachable: int = 2) -> tuple[int, list[str]]:
    """One subject + one concept in TEST_SPACE_ID with n teachable problems
    (tier=2), plus one tier-1 problem and one quarantined problem that must
    never surface. Returns (concept_id, teachable_problem_codes)."""
    from datetime import UTC, datetime

    async with Session() as db:
        subj = Subject(slug="physics", display_name="Physics", search_space_id=TEST_SPACE_ID)
        db.add(subj)
        await db.flush()
        concept = Concept(subject_id=subj.id, slug="newton-2", display_name="Newton's Second Law")
        db.add(concept)
        await db.flush()
        codes = []
        for i in range(n_teachable):
            code = f"p{i + 1}"
            codes.append(code)
            db.add(ConceptProblem(
                concept_id=concept.id, problem_code=code, difficulty="intro",
                payload=_full_problem_payload(code, concept.id), tier=2,
            ))
        db.add(ConceptProblem(
            concept_id=concept.id, problem_code="tier1", difficulty="intro",
            payload=_full_problem_payload("tier1", concept.id), tier=1,
        ))
        db.add(ConceptProblem(
            concept_id=concept.id, problem_code="quarantined", difficulty="intro",
            payload=_full_problem_payload("quarantined", concept.id), tier=2,
            quarantined_at=datetime.now(UTC),
        ))
        await db.commit()
        return concept.id, codes


def test_list_concepts_returns_teachable_concepts(client_factory, monkeypatch):
    app, Session = client_factory
    _auth_as(monkeypatch, TEST_USER_ID)
    concept_id, _ = asyncio.run(_seed_curriculum(Session))

    client = TestClient(app)
    resp = client.get(
        f"/apollo/concepts?search_space_id={TEST_SPACE_ID}",
        headers={"Authorization": "Bearer tok"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body == {
        "concepts": [
            {"concept_id": concept_id, "slug": "newton-2",
             "display_name": "Newton's Second Law"}
        ]
    }


def test_list_concepts_excludes_concept_without_teachable_problems(client_factory, monkeypatch):
    app, Session = client_factory
    _auth_as(monkeypatch, TEST_USER_ID)

    async def _seed_empty():
        async with Session() as db:
            subj = Subject(slug="math", display_name="Math", search_space_id=TEST_SPACE_ID)
            db.add(subj)
            await db.flush()
            db.add(Concept(subject_id=subj.id, slug="limits", display_name="Limits"))
            await db.commit()

    asyncio.run(_seed_empty())
    client = TestClient(app)
    resp = client.get(
        f"/apollo/concepts?search_space_id={TEST_SPACE_ID}",
        headers={"Authorization": "Bearer tok"},
    )
    assert resp.status_code == 200
    assert resp.json() == {"concepts": []}
