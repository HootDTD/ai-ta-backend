from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import apollo.hoot_bridge.session_init as session_init
from apollo.hoot_bridge.session_init import _create_session_with_problem
from apollo.persistence.models import ProblemAttempt, TutoringSession
from apollo.schemas.problem import Problem, ReferenceStep
from config.contracts import BundleSnippet
from config.settings import interaction_allowed_for_concept, interaction_concepts
from database.models import Base

_USER_ID = "a1111111-1111-4111-8111-111111111111"
_COURSE_ID = 17
_CONCEPT_ID = 23

_PROBLEM = Problem(
    id="grounding-problem",
    database_id=101,
    concept_id="bernoulli_principle",
    difficulty="intro",
    problem_text="Explain why pressure falls as flow speed rises.",
    given_values={},
    target_unknown="pressure",
    reference_solution=[
        ReferenceStep(
            step=1,
            entry_type="definition",
            id="def1",
            content={"term": "Bernoulli principle"},
        )
    ],
)


def _snippet(snippet_id: str, *, metadata: dict | None = None) -> BundleSnippet:
    return BundleSnippet(
        id=snippet_id,
        type="body",
        page=12,
        section_path="3.2",
        text=f"Grounding text {snippet_id}",
        figure_id=None,
        why="hit",
        source_path="course.pdf",
        doc_title="Course Text",
        doc_short="Course Text",
        citation_marker="[Textbook, p. 12]",
        final_score={"final": 0.9},
        metadata=metadata or {},
    )


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("", frozenset()),
        ("ethics", frozenset({"ethics"})),
        ("ethics,bernoulli_principle", frozenset({"ethics", "bernoulli_principle"})),
        ("  ethics , bernoulli_principle  ", frozenset({"ethics", "bernoulli_principle"})),
        ("Ethics,BERNOULLI_Principle", frozenset({"ethics", "bernoulli_principle"})),
    ],
)
def test_interaction_concepts_parses_allowlist(monkeypatch, raw, expected):
    monkeypatch.setenv("INTERACTION_CONCEPTS", raw)

    assert interaction_concepts() == expected


def test_interaction_concepts_unset_is_unrestricted(monkeypatch):
    monkeypatch.delenv("INTERACTION_CONCEPTS", raising=False)

    assert interaction_concepts() == frozenset()
    assert interaction_allowed_for_concept("any_concept")


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        execution_options={"schema_translate_map": {"app": None, "internal": None}},
    )
    async with engine.begin() as conn:
        await conn.run_sync(
            lambda sync_conn: Base.metadata.create_all(
                sync_conn,
                tables=[TutoringSession.__table__, ProblemAttempt.__table__],
            )
        )
    session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with session_factory() as session:
        session.info["test_session_factory"] = session_factory
        yield session
    if session_init._GROUNDING_TASKS:
        await asyncio.gather(*tuple(session_init._GROUNDING_TASKS), return_exceptions=True)
    await engine.dispose()


async def _create(db: AsyncSession) -> dict:
    return await _create_session_with_problem(
        db,
        user_id=_USER_ID,
        search_space_id=_COURSE_ID,
        concept_id=_CONCEPT_ID,
        difficulty="intro",
        problem=_PROBLEM,
        concept_title="Bernoulli Principle",
        concept_aliases=["bernoulli_principle"],
    )


async def _load_bundle(db: AsyncSession, session_id: int) -> dict | None:
    # Full-row loads (db.get) on this SQLite harness trip a pre-existing UUID
    # result-processor failure on learning_activities.user_id; a single-column
    # select verifies the persisted bundle without that load path.
    result = await db.execute(
        select(TutoringSession.grounding_bundle).where(TutoringSession.id == session_id)
    )
    return result.scalar_one()


def _use_test_session_factory(db: AsyncSession, monkeypatch) -> None:
    monkeypatch.setattr(
        session_init,
        "_get_session_factory",
        lambda: db.info["test_session_factory"],
    )


async def _run_grounding_task() -> None:
    tasks = tuple(session_init._GROUNDING_TASKS)
    assert len(tasks) == 1
    await asyncio.gather(*tasks)


@pytest.mark.asyncio
async def test_grounding_bundle_persisted_after_session_creation(db, monkeypatch):
    monkeypatch.setenv("INTERACTION1", "true")
    monkeypatch.delenv("INTERACTION_CONCEPTS", raising=False)
    _use_test_session_factory(db, monkeypatch)
    retrieve = AsyncMock(return_value=([_snippet("safe")], {"hit_count_raw": 1}))
    monkeypatch.setattr("apollo.hoot_bridge.session_init.retrieve_for_question", retrieve)

    result = await _create(db)

    # Session creation returns before the background retrieval begins.
    retrieve.assert_not_awaited()
    await _run_grounding_task()
    bundle = await _load_bundle(db, result["session_id"])
    assert bundle is not None
    assert bundle["snippets"][0]["id"] == "safe"
    assert bundle["diag"]["student_safe_snippets"] == 1
    assert bundle["retrieval_version"] == "retrieve_for_question:v1"
    assert bundle["built_at"]
    retrieve.assert_awaited_once()
    assert retrieve.await_args.kwargs["db_session"] is not db
    assert retrieve.await_args.kwargs == {
        "query": "Bernoulli Principle. Explain why pressure falls as flow speed rises.",
        "keywords": ["Bernoulli Principle", "bernoulli_principle"],
        "search_space_id": _COURSE_ID,
        "db_session": retrieve.await_args.kwargs["db_session"],
        "top_k": 8,
        "token_budget": 2500,
        "citation_label": "Textbook",
    }


@pytest.mark.asyncio
async def test_allowlist_non_matching_concept_does_not_call_retrieval(db, monkeypatch):
    monkeypatch.setenv("INTERACTION1", "true")
    monkeypatch.setenv("INTERACTION_CONCEPTS", "ethics")
    retrieve = AsyncMock()
    monkeypatch.setattr("apollo.hoot_bridge.session_init.retrieve_for_question", retrieve)

    result = await _create(db)

    bundle = await _load_bundle(db, result["session_id"])
    assert bundle is None
    retrieve.assert_not_awaited()


@pytest.mark.asyncio
async def test_allowlist_matching_concept_builds_bundle(db, monkeypatch):
    monkeypatch.setenv("INTERACTION1", "true")
    monkeypatch.setenv("INTERACTION_CONCEPTS", "BERNOULLI_PRINCIPLE")
    _use_test_session_factory(db, monkeypatch)
    retrieve = AsyncMock(return_value=([_snippet("safe")], {"hit_count_raw": 1}))
    monkeypatch.setattr("apollo.hoot_bridge.session_init.retrieve_for_question", retrieve)

    result = await _create(db)

    await _run_grounding_task()
    bundle = await _load_bundle(db, result["session_id"])
    assert bundle is not None
    assert bundle["snippets"][0]["id"] == "safe"
    retrieve.assert_awaited_once()


@pytest.mark.asyncio
async def test_retrieval_failure_does_not_fail_session_creation(db, monkeypatch):
    monkeypatch.setenv("INTERACTION1", "1")
    _use_test_session_factory(db, monkeypatch)
    monkeypatch.setattr(
        "apollo.hoot_bridge.session_init.retrieve_for_question",
        AsyncMock(side_effect=RuntimeError("retrieval unavailable")),
    )

    result = await _create(db)

    await _run_grounding_task()
    bundle = await _load_bundle(db, result["session_id"])
    assert bundle is None


@pytest.mark.asyncio
async def test_solution_bearing_snippets_are_dropped_before_persist(db, monkeypatch):
    monkeypatch.setenv("INTERACTION1", "on")
    _use_test_session_factory(db, monkeypatch)
    snippets = [
        _snippet("safe"),
        _snippet("authored-solution", metadata={"authored_role": "solution"}),
        _snippet("answer-key", metadata={"doc_kind": "answer_key"}),
    ]
    monkeypatch.setattr(
        "apollo.hoot_bridge.session_init.retrieve_for_question",
        AsyncMock(return_value=(snippets, {"chunks_in_context": 3})),
    )

    result = await _create(db)

    await _run_grounding_task()
    bundle = await _load_bundle(db, result["session_id"])
    assert [item["id"] for item in bundle["snippets"]] == ["safe"]
    assert bundle["diag"]["solution_snippets_dropped"] == 2


@pytest.mark.asyncio
async def test_flag_off_does_not_call_retrieval(db, monkeypatch):
    monkeypatch.delenv("INTERACTION1", raising=False)
    retrieve = AsyncMock()
    monkeypatch.setattr("apollo.hoot_bridge.session_init.retrieve_for_question", retrieve)

    result = await _create(db)

    bundle = await _load_bundle(db, result["session_id"])
    assert bundle is None
    retrieve.assert_not_awaited()


@pytest.mark.asyncio
async def test_background_build_logs_and_stops_when_session_disappeared(db, monkeypatch):
    _use_test_session_factory(db, monkeypatch)
    retrieve = AsyncMock()
    monkeypatch.setattr("apollo.hoot_bridge.session_init.retrieve_for_question", retrieve)

    await session_init._build_grounding_bundle_in_background(
        session_id=999_999,
        search_space_id=_COURSE_ID,
        concept_title="Bernoulli Principle",
        concept_aliases=["bernoulli_principle"],
        problem=_PROBLEM,
    )

    retrieve.assert_not_awaited()


@pytest.mark.asyncio
async def test_background_build_swallows_factory_errors(monkeypatch):
    def _boom():
        raise RuntimeError("factory unavailable")

    monkeypatch.setattr(session_init, "_get_session_factory", _boom)

    # Must not raise: the background task is best-effort by contract.
    await session_init._build_grounding_bundle_in_background(
        session_id=1,
        search_space_id=_COURSE_ID,
        concept_title="Bernoulli Principle",
        concept_aliases=["bernoulli_principle"],
        problem=_PROBLEM,
    )
