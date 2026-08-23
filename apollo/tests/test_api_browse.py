"""Endpoint tests for the student browse surface (GET /apollo/concepts,
GET /apollo/problems) and the standalone session entry (POST /apollo/sessions)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Table, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import apollo.auth_deps as deps
from apollo.api import get_neo4j_client, register_exception_handlers
from apollo.api import router as apollo_router
from apollo.conftest import TEST_SPACE_ID, TEST_USER_ID, TEST_USER_ID_2
from apollo.overseer.rubric import score_to_band
from apollo.persistence.models import (
    Concept,
    EntityPrereq,
    LearnerEntity,
    LearnerState,
    ProblemAttempt,
    StudentProgress,
    TutoringSession,
)
from apollo.persistence.models import (
    Problem as ProblemRecord,
)
from auth import AuthContext
from database.models import Base
from database.session import get_db_session

# SA stubs type ``__table__`` as FromClause; these are all concrete Tables.
TABLES = cast(
    "list[Table]",
    [
        Concept.__table__,
        ProblemRecord.__table__,
        TutoringSession.__table__,
        ProblemAttempt.__table__,
        StudentProgress.__table__,
        # Personalized selection (APOLLO_SESSION_PERSONALIZATION_ENABLED=1 in this
        # env) reads the learner profile at the selection seam; on the cold-start
        # empty-table path it degrades byte-identically to candidates[0].
        LearnerEntity.__table__,
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
    ProblemRecord.provenance) that SQLite's DDL compiler rejects
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
        subj = SimpleNamespace(slug="physics", display_name="Physics", search_space_id=TEST_SPACE_ID)
        concept = Concept(course_id=subj.search_space_id, subject_slug=subj.slug, subject_display_name=subj.display_name, slug="newton-2", display_name="Newton's Second Law")
        db.add(concept)
        await db.flush()
        cid = int(concept.id)  # type: ignore[arg-type]  # SA stubs expose .id as Column
        codes = []
        for i in range(n_teachable):
            code = f"p{i + 1}"
            codes.append(code)
            db.add(
                ProblemRecord.from_pydantic_payload(
                    _full_problem_payload(code, cid),
                    course_id=TEST_SPACE_ID,
                    concept_id=cid,
                    tier=2,
                )
            )
        db.add(
            ProblemRecord.from_pydantic_payload(
                _full_problem_payload("tier1", cid),
                course_id=TEST_SPACE_ID,
                concept_id=cid,
                tier=1,
            )
        )
        db.add(
            ProblemRecord.from_pydantic_payload(
                _full_problem_payload("quarantined", cid),
                course_id=TEST_SPACE_ID,
                concept_id=cid,
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
            subj = SimpleNamespace(slug="math", display_name="Math", search_space_id=TEST_SPACE_ID)
            db.add(Concept(course_id=subj.search_space_id, subject_slug=subj.slug, subject_display_name=subj.display_name, slug="limits", display_name="Limits"))
            await db.commit()

    asyncio.run(_seed_empty())
    client = TestClient(app)
    resp = client.get(
        f"/apollo/concepts?search_space_id={TEST_SPACE_ID}",
        headers={"Authorization": "Bearer tok"},
    )
    assert resp.status_code == 200
    assert resp.json() == {"concepts": []}


async def _seed_attempt(
    Session,
    *,
    user_id: str,
    problem_id: str,
    space_id: int = TEST_SPACE_ID,
    result: str | None = None,
    diagnostic_report: dict | None = None,
):
    async with Session() as db:
        problem = (
            await db.execute(
                select(ProblemRecord).where(ProblemRecord.problem_code == problem_id)
            )
        ).scalar_one()
        sess = TutoringSession(
            user_id=user_id,
            search_space_id=space_id,
            concept_id=problem.concept_id,
            status="ended",
            phase="REPORT",
            current_problem_id=problem.id,
        )
        db.add(sess)
        await db.flush()
        db.add(
            ProblemAttempt(
                session_id=sess.id,
                problem_id=problem.id,
                difficulty="intro",
                user_id=sess.user_id,
                course_id=sess.course_id,
                result=result,
                diagnostic_report=diagnostic_report,
            )
        )
        await db.commit()


def _graded_report(
    score: int, letter: str, *, raw_score: int | None = None, narrative: str = "…"
) -> dict:
    """A Done-shaped diagnostic_report whose served_overall is (score, letter).
    `raw_score` (default: different from score) keeps rubric.overall distinct
    so tests prove the snapshot — not the raw rubric — is what browse serves.
    `narrative` distinguishes which attempt's feedback rides with the grade."""
    raw = raw_score if raw_score is not None else max(0, score - 30)
    return {
        "narrative": narrative,
        "rubric": {"overall": {"score": raw, "letter": "F"}},
        "coverage": {},
        "served_overall": {"score": score, "letter": letter},
    }


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
    # an attempted-but-never-graded problem carries no grade
    assert by_id[codes[0]]["grade"] is None
    # student-safety: no solution or answer-shaped fields leak
    for p in problems:
        assert set(p) == {"id", "difficulty", "problem_text", "attempted", "grade"}


def test_list_problems_serves_best_grade_from_served_overall(client_factory, monkeypatch):
    """Graded attempts surface {score, letter}. The BEST served grade wins
    across attempts, the snapshot takes precedence over the raw rubric
    overall, and another student's grade never leaks onto our cards."""
    app, Session = client_factory
    _auth_as(monkeypatch, TEST_USER_ID)
    concept_id, codes = asyncio.run(_seed_curriculum(Session, n_teachable=2))
    # p1: graded C, then A-, then a WORSE later attempt — the A- attempt must
    # win the card AND its feedback must ride along (best attempt, not latest)
    asyncio.run(
        _seed_attempt(
            Session,
            user_id=TEST_USER_ID,
            problem_id=codes[0],
            result="graded",
            diagnostic_report=_graded_report(60, "C", narrative="c-notes"),
        )
    )
    asyncio.run(
        _seed_attempt(
            Session,
            user_id=TEST_USER_ID,
            problem_id=codes[0],
            result="graded",
            diagnostic_report=_graded_report(88, "A-", narrative="a-notes"),
        )
    )
    asyncio.run(
        _seed_attempt(
            Session,
            user_id=TEST_USER_ID,
            problem_id=codes[0],
            result="graded",
            diagnostic_report=_graded_report(50, "F", narrative="worse-later"),
        )
    )
    # p2: only the OTHER student has a grade — ours must stay null
    asyncio.run(
        _seed_attempt(
            Session,
            user_id=TEST_USER_ID_2,
            problem_id=codes[1],
            result="graded",
            diagnostic_report=_graded_report(97, "A+"),
        )
    )

    client = TestClient(app)
    resp = client.get(
        f"/apollo/problems?search_space_id={TEST_SPACE_ID}&concept_id={concept_id}&difficulty=intro",
        headers={"Authorization": "Bearer tok"},
    )
    assert resp.status_code == 200
    by_id = {p["id"]: p for p in resp.json()["problems"]}
    # served_overall (88/A-) wins — NOT the raw rubric overall the report
    # stores — and feedback is the WINNING attempt's narrative
    assert by_id[codes[0]]["grade"] == {
        "score": 88,
        "letter": "A-",
        "band": score_to_band(88),
        "feedback": "a-notes",
    }
    assert by_id[codes[1]]["grade"] is None
    assert by_id[codes[1]]["attempted"] is False


def test_list_problems_grade_falls_back_to_rubric_overall(client_factory, monkeypatch):
    """Attempts graded before the served_overall snapshot existed fall back to
    the legacy rubric.overall; malformed / non-graded rows degrade to Tried."""
    app, Session = client_factory
    _auth_as(monkeypatch, TEST_USER_ID)
    concept_id, codes = asyncio.run(_seed_curriculum(Session, n_teachable=2))
    # p1: legacy row — rubric.overall only, no snapshot
    asyncio.run(
        _seed_attempt(
            Session,
            user_id=TEST_USER_ID,
            problem_id=codes[0],
            result="graded",
            diagnostic_report={"narrative": "…", "rubric": {"overall": {"score": 75, "letter": "B"}}},
        )
    )
    # p2: result says graded but the report is unusable + an abandoned attempt
    # that carries a report — neither may surface a grade or 500 the endpoint
    asyncio.run(
        _seed_attempt(
            Session,
            user_id=TEST_USER_ID,
            problem_id=codes[1],
            result="graded",
            diagnostic_report=None,
        )
    )
    asyncio.run(
        _seed_attempt(
            Session,
            user_id=TEST_USER_ID,
            problem_id=codes[1],
            result="abandoned",
            diagnostic_report=_graded_report(90, "A"),
        )
    )

    client = TestClient(app)
    resp = client.get(
        f"/apollo/problems?search_space_id={TEST_SPACE_ID}&concept_id={concept_id}&difficulty=intro",
        headers={"Authorization": "Bearer tok"},
    )
    assert resp.status_code == 200
    by_id = {p["id"]: p for p in resp.json()["problems"]}
    assert by_id[codes[0]]["grade"] == {
        "score": 75,
        "letter": "B",
        "band": score_to_band(75),
        "feedback": "…",
    }
    assert by_id[codes[1]]["grade"] is None
    assert by_id[codes[1]]["attempted"] is True


def test_list_problems_grade_without_narrative_serves_null_feedback(client_factory, monkeypatch):
    """A graded report missing a usable narrative still serves its grade —
    feedback degrades to null alone, never the whole chip."""
    app, Session = client_factory
    _auth_as(monkeypatch, TEST_USER_ID)
    concept_id, codes = asyncio.run(_seed_curriculum(Session, n_teachable=1))
    report = _graded_report(82, "B+")
    del report["narrative"]
    asyncio.run(
        _seed_attempt(
            Session,
            user_id=TEST_USER_ID,
            problem_id=codes[0],
            result="graded",
            diagnostic_report=report,
        )
    )

    client = TestClient(app)
    resp = client.get(
        f"/apollo/problems?search_space_id={TEST_SPACE_ID}&concept_id={concept_id}&difficulty=intro",
        headers={"Authorization": "Bearer tok"},
    )
    assert resp.status_code == 200
    by_id = {p["id"]: p for p in resp.json()["problems"]}
    assert by_id[codes[0]]["grade"] == {
        "score": 82,
        "letter": "B+",
        "band": score_to_band(82),
        "feedback": None,
    }


def test_served_overall_from_report_edge_cases():
    """Direct contract of the extractor: reject shapes that would render a
    bogus chip instead of degrading to the plain Tried state."""
    from apollo.handlers.browse import served_overall_from_report

    assert served_overall_from_report(None) is None
    assert served_overall_from_report("not-a-dict") is None
    assert served_overall_from_report({}) is None
    assert served_overall_from_report({"rubric": {"overall": "F"}}) is None
    # score must be a real number (bool is not a grade) and letter a string
    assert served_overall_from_report({"served_overall": {"score": True, "letter": "A"}}) is None
    assert served_overall_from_report({"served_overall": {"score": 90}}) is None
    assert served_overall_from_report({"served_overall": {"letter": "A"}}) is None
    # snapshot beats the raw rubric when both exist
    assert served_overall_from_report(
        {
            "served_overall": {"score": 82, "letter": "B+"},
            "rubric": {"overall": {"score": 40, "letter": "F"}},
        }
    ) == {"score": 82, "letter": "B+", "band": score_to_band(82)}
    # legacy fallback still works
    assert served_overall_from_report({"rubric": {"overall": {"score": 60.0, "letter": "C"}}}) == {
        "score": 60.0,
        "letter": "C",
        "band": score_to_band(60),
    }
    # `band` is snapshot-first: a persisted token wins over re-derivation, so a
    # later cut move can never relabel a grade the student already saw...
    assert served_overall_from_report(
        {"served_overall": {"score": 90, "letter": "A", "band": "beginner"}}
    ) == {"score": 90, "letter": "A", "band": "beginner"}
    # ...but a token outside the wire vocabulary is not a snapshot, it is
    # corruption, and falls back to the score rather than being served through.
    assert served_overall_from_report(
        {"served_overall": {"score": 90, "letter": "A", "band": "Advanced!"}}
    ) == {"score": 90, "letter": "A", "band": score_to_band(90)}


def test_feedback_from_report_edge_cases():
    """Direct contract of the feedback extractor: only a non-empty narrative
    string is served; every other shape degrades to None."""
    from apollo.handlers.browse import feedback_from_report

    assert feedback_from_report(None) is None
    assert feedback_from_report("not-a-dict") is None
    assert feedback_from_report({}) is None
    assert feedback_from_report({"narrative": None}) is None
    assert feedback_from_report({"narrative": {"headline": "structured"}}) is None
    assert feedback_from_report({"narrative": "   "}) is None
    assert feedback_from_report({"narrative": "You explained value well."}) == (
        "You explained value well."
    )


def test_list_problems_rejects_foreign_concept(client_factory, monkeypatch):
    """A concept belonging to another course must 409, not leak problems."""
    app, Session = client_factory
    _auth_as(monkeypatch, TEST_USER_ID)

    async def _seed_foreign() -> int:
        async with Session() as db:
            subj = SimpleNamespace(slug="chem", display_name="Chem", search_space_id=TEST_SPACE_ID + 99)
            concept = Concept(course_id=subj.search_space_id, subject_slug=subj.slug, subject_display_name=subj.display_name, slug="acids", display_name="Acids")
            db.add(concept)
            await db.flush()
            cid = int(concept.id)  # type: ignore[arg-type]  # SA stubs expose .id as Column
            db.add(
                ProblemRecord.from_pydantic_payload(
                    _full_problem_payload("f1", cid),
                    course_id=TEST_SPACE_ID + 99,
                    concept_id=cid,
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
            row = (
                await db.execute(
                    select(TutoringSession).where(TutoringSession.id == body["session_id"])
                )
            ).scalar_one()
            assert row.phase == "TEACHING"
            assert row.status == "active"
            assert row.concept_id == concept_id
            selected_problem_id = (
                await db.execute(
                    select(ProblemRecord.id).where(
                        ProblemRecord.concept_id == concept_id,
                        ProblemRecord.problem_code == codes[1],
                    )
                )
            ).scalar_one()
            assert row.current_problem_id == selected_problem_id

    asyncio.run(_check())


def test_create_session_response_does_not_wait_for_grounding(client_factory, monkeypatch):
    from threading import Event, Thread

    import apollo.hoot_bridge.session_init as session_init

    app, Session = client_factory
    _auth_as(monkeypatch, TEST_USER_ID)
    concept_id, codes = asyncio.run(_seed_curriculum(Session))
    monkeypatch.setenv("INTERACTION1", "true")
    monkeypatch.setattr(session_init, "_get_session_factory", lambda: Session)

    retrieval_started = Event()
    release_retrieval = Event()
    retrieval_finished = Event()

    async def _blocked_retrieval(**_kwargs):
        retrieval_started.set()
        try:
            await asyncio.to_thread(release_retrieval.wait)
            return [], {}
        finally:
            retrieval_finished.set()

    monkeypatch.setattr(session_init, "retrieve_for_question", _blocked_retrieval)
    response_done = Event()
    outcome = {}

    with TestClient(app) as client:
        def _post():
            try:
                outcome["response"] = client.post(
                    "/apollo/sessions",
                    json={
                        "search_space_id": TEST_SPACE_ID,
                        "concept_id": concept_id,
                        "difficulty": "intro",
                        "problem_id": codes[0],
                    },
                    headers={"Authorization": "Bearer tok"},
                )
            except BaseException as exc:
                outcome["error"] = exc
            finally:
                response_done.set()

        request_thread = Thread(target=_post)
        request_thread.start()
        assert retrieval_started.wait(timeout=2)
        returned_before_retrieval = response_done.wait(timeout=0.5)
        release_retrieval.set()
        request_thread.join(timeout=2)
        assert retrieval_finished.wait(timeout=2)

    assert returned_before_retrieval
    assert "error" not in outcome
    assert outcome["response"].status_code == 200


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
            subj = SimpleNamespace(slug="chem", display_name="Chem", search_space_id=TEST_SPACE_ID + 99)
            concept = Concept(course_id=subj.search_space_id, subject_slug=subj.slug, subject_display_name=subj.display_name, slug="acids", display_name="Acids")
            db.add(concept)
            await db.flush()
            cid = int(concept.id)  # type: ignore[arg-type]  # SA stubs expose .id as Column
            db.add(
                ProblemRecord.from_pydantic_payload(
                    _full_problem_payload("f1", cid),
                    course_id=TEST_SPACE_ID + 99,
                    concept_id=cid,
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
