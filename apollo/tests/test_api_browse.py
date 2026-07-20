"""Endpoint tests for the student browse surface (GET /apollo/concepts,
GET /apollo/problems) and the standalone session entry (POST /apollo/sessions)."""

from __future__ import annotations

import asyncio
from typing import cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Table
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import apollo.auth_deps as deps
from apollo.api import get_neo4j_client, register_exception_handlers
from apollo.api import router as apollo_router
from apollo.conftest import TEST_SPACE_ID, TEST_USER_ID, TEST_USER_ID_2
from apollo.persistence.models import (
    TutoringSession,
    Concept,
    ConceptProblem,
    EntityPrereq,
    KGEntity,
    LearnerState,
    ProblemAttempt,
    StudentProgress,
    Subject,
)
from auth import AuthContext
from database.models import Base
from database.session import get_db_session

# SA stubs type ``__table__`` as FromClause; these are all concrete Tables.
TABLES = cast(
    "list[Table]",
    [
        Subject.__table__,
        Concept.__table__,
        ConceptProblem.__table__,
        TutoringSession.__table__,
        ProblemAttempt.__table__,
        StudentProgress.__table__,
        # Personalized selection (APOLLO_SESSION_PERSONALIZATION_ENABLED=1 in this
        # env) reads the learner profile at the selection seam; on the cold-start
        # empty-table path it degrades byte-identically to candidates[0].
        KGEntity.__table__,
        LearnerState.__table__,
        EntityPrereq.__table__,
    ],
)


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
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        execution_options={"schema_translate_map": {"app": None, "internal": None}},
    )

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
        deps,
        "resolve_auth_context",
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
        cid = int(concept.id)  # type: ignore[arg-type]  # SA stubs expose .id as Column
        codes = []
        for i in range(n_teachable):
            code = f"p{i + 1}"
            codes.append(code)
            db.add(
                ConceptProblem(
                    concept_id=cid,
                    problem_code=code,
                    difficulty="intro",
                    payload=_full_problem_payload(code, cid),
                    tier=2,
                )
            )
        db.add(
            ConceptProblem(
                concept_id=cid,
                problem_code="tier1",
                difficulty="intro",
                payload=_full_problem_payload("tier1", cid),
                tier=1,
            )
        )
        db.add(
            ConceptProblem(
                concept_id=cid,
                problem_code="quarantined",
                difficulty="intro",
                payload=_full_problem_payload("quarantined", cid),
                tier=2,
                quarantined_at=datetime.now(UTC),
            )
        )
        await db.commit()
        return cid, codes


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
            {"concept_id": concept_id, "slug": "newton-2", "display_name": "Newton's Second Law"}
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


async def _seed_attempt(Session, *, user_id: str, problem_id: str, space_id: int = TEST_SPACE_ID):
    async with Session() as db:
        sess = TutoringSession(
            user_id=user_id,
            search_space_id=space_id,
            status="ended",
            phase="REPORT",
            current_problem_id=problem_id,
        )
        db.add(sess)
        await db.flush()
        db.add(ProblemAttempt(session_id=sess.id, problem_id=problem_id, difficulty="intro"))
        await db.commit()


def test_list_problems_filters_and_flags_attempted(client_factory, monkeypatch):
    app, Session = client_factory
    _auth_as(monkeypatch, TEST_USER_ID)
    concept_id, codes = asyncio.run(_seed_curriculum(Session, n_teachable=2))
    asyncio.run(_seed_attempt(Session, user_id=TEST_USER_ID, problem_id=codes[0]))
    # another student's attempt must NOT mark it attempted for us
    asyncio.run(_seed_attempt(Session, user_id=TEST_USER_ID_2, problem_id=codes[1]))

    client = TestClient(app)
    resp = client.get(
        f"/apollo/problems?search_space_id={TEST_SPACE_ID}&concept_id={concept_id}&difficulty=intro",
        headers={"Authorization": "Bearer tok"},
    )
    assert resp.status_code == 200
    problems = resp.json()["problems"]
    by_id = {p["id"]: p for p in problems}
    # tier-1 + quarantined rows never surface
    assert set(by_id) == set(codes)
    assert by_id[codes[0]]["attempted"] is True
    assert by_id[codes[1]]["attempted"] is False
    # student-safety: no solution or answer-shaped fields leak
    for p in problems:
        assert set(p) == {"id", "difficulty", "problem_text", "attempted"}


def test_list_problems_rejects_foreign_concept(client_factory, monkeypatch):
    """A concept belonging to another course must 409, not leak problems."""
    app, Session = client_factory
    _auth_as(monkeypatch, TEST_USER_ID)

    async def _seed_foreign() -> int:
        async with Session() as db:
            subj = Subject(slug="chem", display_name="Chem", search_space_id=TEST_SPACE_ID + 99)
            db.add(subj)
            await db.flush()
            concept = Concept(subject_id=subj.id, slug="acids", display_name="Acids")
            db.add(concept)
            await db.flush()
            cid = int(concept.id)  # type: ignore[arg-type]  # SA stubs expose .id as Column
            db.add(
                ConceptProblem(
                    concept_id=cid,
                    problem_code="f1",
                    difficulty="intro",
                    payload=_full_problem_payload("f1", cid),
                    tier=2,
                )
            )
            await db.commit()
            return cid

    foreign_id = asyncio.run(_seed_foreign())
    client = TestClient(app)
    resp = client.get(
        f"/apollo/problems?search_space_id={TEST_SPACE_ID}&concept_id={foreign_id}",
        headers={"Authorization": "Bearer tok"},
    )
    assert resp.status_code == 409
    assert resp.json()["error_code"] == "no_matching_concept"


def test_create_session_with_explicit_problem(client_factory, monkeypatch):
    app, Session = client_factory
    _auth_as(monkeypatch, TEST_USER_ID)
    concept_id, codes = asyncio.run(_seed_curriculum(Session))

    client = TestClient(app)
    resp = client.post(
        "/apollo/sessions",
        json={
            "search_space_id": TEST_SPACE_ID,
            "concept_id": concept_id,
            "difficulty": "intro",
            "problem_id": codes[1],
        },
        headers={"Authorization": "Bearer tok"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["problem"]["id"] == codes[1]
    assert "reference_solution" not in body["problem"]
    # teaching flow needs these — lock the contract
    assert "given_values" in body["problem"]
    assert "target_unknown" in body["problem"]
    assert isinstance(body["session_id"], int)
    assert isinstance(body["attempt_id"], int)

    # session row exists, TEACHING phase, correct concept binding
    async def _check():
        async with Session() as db:
            from sqlalchemy import select

            row = (
                await db.execute(
                    select(TutoringSession).where(TutoringSession.id == body["session_id"])
                )
            ).scalar_one()
            assert row.phase == "TEACHING"
            assert row.status == "active"
            assert row.concept_id == concept_id
            assert row.current_problem_id == codes[1]

    asyncio.run(_check())


def test_create_session_unknown_problem_404s(client_factory, monkeypatch):
    app, Session = client_factory
    _auth_as(monkeypatch, TEST_USER_ID)
    concept_id, _ = asyncio.run(_seed_curriculum(Session))

    client = TestClient(app)
    resp = client.post(
        "/apollo/sessions",
        json={
            "search_space_id": TEST_SPACE_ID,
            "concept_id": concept_id,
            "difficulty": "intro",
            "problem_id": "does-not-exist",
        },
        headers={"Authorization": "Bearer tok"},
    )
    assert resp.status_code == 404
    assert resp.json()["error_code"] == "problem_not_found"


def test_create_session_without_problem_id_selects_from_pool(client_factory, monkeypatch):
    app, Session = client_factory
    _auth_as(monkeypatch, TEST_USER_ID)
    concept_id, codes = asyncio.run(_seed_curriculum(Session))

    client = TestClient(app)
    resp = client.post(
        "/apollo/sessions",
        json={
            "search_space_id": TEST_SPACE_ID,
            "concept_id": concept_id,
            "difficulty": "intro",
        },
        headers={"Authorization": "Bearer tok"},
    )
    assert resp.status_code == 200
    assert resp.json()["problem"]["id"] in codes


def test_create_session_ends_prior_active_session(client_factory, monkeypatch):
    app, Session = client_factory
    _auth_as(monkeypatch, TEST_USER_ID)
    concept_id, codes = asyncio.run(_seed_curriculum(Session))

    client = TestClient(app)
    first = client.post(
        "/apollo/sessions",
        json={
            "search_space_id": TEST_SPACE_ID,
            "concept_id": concept_id,
            "difficulty": "intro",
            "problem_id": codes[0],
        },
        headers={"Authorization": "Bearer tok"},
    ).json()
    second = client.post(
        "/apollo/sessions",
        json={
            "search_space_id": TEST_SPACE_ID,
            "concept_id": concept_id,
            "difficulty": "intro",
            "problem_id": codes[1],
        },
        headers={"Authorization": "Bearer tok"},
    ).json()

    async def _check():
        async with Session() as db:
            from sqlalchemy import select

            old = (
                await db.execute(
                    select(TutoringSession).where(TutoringSession.id == first["session_id"])
                )
            ).scalar_one()
            new = (
                await db.execute(
                    select(TutoringSession).where(TutoringSession.id == second["session_id"])
                )
            ).scalar_one()
            assert old.status == "ended"
            assert new.status == "active"

    asyncio.run(_check())


def test_create_session_rejects_unknown_difficulty_at_seam(client_factory):
    """The request schema Literal already 422s bad difficulties at the HTTP
    edge; the handler seam keeps its own guard for non-HTTP callers. Lock it."""
    from apollo.hoot_bridge.session_init import init_session_direct

    app, Session = client_factory

    async def _call():
        async with Session() as db:
            with pytest.raises(ValueError, match="unknown difficulty"):
                await init_session_direct(
                    db=db,
                    user_id=TEST_USER_ID,
                    search_space_id=TEST_SPACE_ID,
                    concept_id=1,
                    difficulty="impossible",
                )

    asyncio.run(_call())


def test_progress_detail_via_endpoint(client_factory, monkeypatch):
    """GET /apollo/progress?search_space_id=N routes to the per-course detail
    handler (concept mastery + recent attempts) instead of the global summary."""
    app, Session = client_factory
    _auth_as(monkeypatch, TEST_USER_ID)
    concept_id, codes = asyncio.run(_seed_curriculum(Session))
    asyncio.run(_seed_attempt(Session, user_id=TEST_USER_ID, problem_id=codes[0]))

    client = TestClient(app)
    resp = client.get(
        f"/apollo/progress?search_space_id={TEST_SPACE_ID}",
        headers={"Authorization": "Bearer tok"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["user_id"] == TEST_USER_ID
    assert set(body["detail"]) == {"mastery", "recent_attempts"}
    # no learner state seeded, and the plain (ungraded) attempt must not surface
    assert body["detail"]["mastery"] == []
    assert body["detail"]["recent_attempts"] == []


def test_create_session_rejects_foreign_concept(client_factory, monkeypatch):
    """Starting a session against a concept from another course must 409,
    not bind the session to a foreign concept."""
    app, Session = client_factory
    _auth_as(monkeypatch, TEST_USER_ID)

    async def _seed_foreign() -> int:
        async with Session() as db:
            subj = Subject(slug="chem", display_name="Chem", search_space_id=TEST_SPACE_ID + 99)
            db.add(subj)
            await db.flush()
            concept = Concept(subject_id=subj.id, slug="acids", display_name="Acids")
            db.add(concept)
            await db.flush()
            cid = int(concept.id)  # type: ignore[arg-type]  # SA stubs expose .id as Column
            db.add(
                ConceptProblem(
                    concept_id=cid,
                    problem_code="f1",
                    difficulty="intro",
                    payload=_full_problem_payload("f1", cid),
                    tier=2,
                )
            )
            await db.commit()
            return cid

    foreign_id = asyncio.run(_seed_foreign())
    client = TestClient(app)
    resp = client.post(
        "/apollo/sessions",
        json={
            "search_space_id": TEST_SPACE_ID,
            "concept_id": foreign_id,
            "difficulty": "intro",
        },
        headers={"Authorization": "Bearer tok"},
    )
    assert resp.status_code == 409
    assert resp.json()["error_code"] == "no_matching_concept"
