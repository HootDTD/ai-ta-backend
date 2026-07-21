"""handle_get_progress_detail: per-concept mastery + recent graded attempts."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from apollo.conftest import TEST_SPACE_ID, TEST_USER_ID, TEST_USER_ID_2
from apollo.handlers.progress import handle_get_progress_detail
from apollo.persistence.models import (
    Concept,
    LearnerEntity,
    LearnerState,
    Problem,
    ProblemAttempt,
    StudentProgress,
    TutoringSession,
)
from database.models import Base

TABLES = [
    StudentProgress.__table__,
    Concept.__table__,
    Problem.__table__,
    LearnerEntity.__table__,
    LearnerState.__table__,
    TutoringSession.__table__,
    ProblemAttempt.__table__,
]


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        execution_options={"schema_translate_map": {"app": None, "internal": None}},
    )
    async with engine.begin() as conn:
        await conn.run_sync(lambda sc: Base.metadata.create_all(sc, tables=TABLES))
    Session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with Session() as s:
        yield s
    await engine.dispose()


async def _seed_concept(db, *, slug: str, name: str) -> int:
    c = Concept(
        course_id=TEST_SPACE_ID,
        subject_slug=f"subj-{slug}",
        subject_display_name=slug,
        slug=slug,
        display_name=name,
    )
    db.add(c)
    await db.flush()
    return int(c.id)  # type: ignore[arg-type]  # SA stubs expose .id as Column


async def _seed_mastery(db, *, concept_id: int, values: list[float]) -> None:
    for i, m in enumerate(values):
        ent = LearnerEntity(
            course_id=TEST_SPACE_ID,
            concept_id=concept_id,
            canonical_key=f"k{concept_id}-{i}",
            kind="quantity",
            display_name=f"Entity {concept_id}-{i}",
        )
        db.add(ent)
        await db.flush()
        db.add(
            LearnerState(
                user_id=TEST_USER_ID,
                search_space_id=TEST_SPACE_ID,
                entity_id=ent.id,
                belief=[0.2, 0.8],
                mastery=m,
                confidence=0.9,
                evidence_count=1,
            )
        )
    await db.commit()


async def _seed_problem(db: AsyncSession, *, concept_id: int, problem_code: str) -> int:
    problem = Problem.from_inventory_payload(
        {
            "id": problem_code,
            "difficulty": "intro",
            "problem_text": "Explain",
            "given_values": {},
            "target_unknown": "",
        },
        course_id=TEST_SPACE_ID,
        concept_id=concept_id,
        tier=2,
    )
    db.add(problem)
    await db.flush()
    return int(problem.id)


async def _seed_graded_attempt(
    db,
    *,
    concept_id: int,
    problem_id: str,
    score: int,
    letter: str,
    user_id: str = TEST_USER_ID,
    when: datetime | None = None,
) -> None:
    problem_database_id = await _seed_problem(
        db, concept_id=concept_id, problem_code=problem_id
    )
    sess = TutoringSession(
        user_id=user_id,
        search_space_id=TEST_SPACE_ID,
        concept_id=concept_id,
        status="ended",
        phase="REPORT",
        current_problem_id=problem_database_id,
    )
    db.add(sess)
    await db.flush()
    db.add(
        ProblemAttempt(
            session_id=sess.id,
            problem_id=problem_database_id,
            difficulty="intro",
            user_id=sess.user_id,
            course_id=sess.course_id,
            result="graded",
            diagnostic_report={
                "rubric": {"overall": {"score": score, "letter": letter}},
                "narrative": "...",
            },
            created_at=when or datetime.now(UTC),
        )
    )
    await db.commit()


async def test_detail_mastery_grouped_per_concept(db):
    c1 = await _seed_concept(db, slug="newton-2", name="Newton's Second Law")
    c2 = await _seed_concept(db, slug="energy", name="Energy Conservation")
    await _seed_mastery(db, concept_id=c1, values=[0.2, 0.6])
    await _seed_mastery(db, concept_id=c2, values=[0.9])

    out = await handle_get_progress_detail(
        db=db, user_id=TEST_USER_ID, search_space_id=TEST_SPACE_ID
    )
    assert out["user_id"] == TEST_USER_ID  # base payload preserved
    mastery = {m["concept_id"]: m for m in out["detail"]["mastery"]}
    assert mastery[c1]["mastery_avg"] == 0.4
    assert mastery[c1]["entity_count"] == 2
    assert mastery[c2]["mastery_avg"] == 0.9
    assert mastery[c1]["display_name"] == "Newton's Second Law"


async def test_detail_recent_attempts_graded_only_newest_first(db):
    c1 = await _seed_concept(db, slug="newton-2", name="Newton's Second Law")
    old = datetime.now(UTC) - timedelta(days=2)
    await _seed_graded_attempt(
        db, concept_id=c1, problem_id="p-old", score=60, letter="C", when=old
    )
    await _seed_graded_attempt(db, concept_id=c1, problem_id="p-new", score=85, letter="A-")
    # ungraded attempt and another student's attempt must not appear
    live_problem_id = await _seed_problem(db, concept_id=c1, problem_code="p-live")
    sess = TutoringSession(
        user_id=TEST_USER_ID,
        search_space_id=TEST_SPACE_ID,
        concept_id=c1,
        status="active",
        phase="TEACHING",
        current_problem_id=live_problem_id,
    )
    db.add(sess)
    await db.flush()
    db.add(
        ProblemAttempt(
            session_id=sess.id,
            problem_id=live_problem_id,
            difficulty="intro",
            user_id=sess.user_id,
            course_id=sess.course_id,
        )
    )
    await db.commit()
    await _seed_graded_attempt(
        db, concept_id=c1, problem_id="p-other", score=99, letter="A+", user_id=TEST_USER_ID_2
    )

    out = await handle_get_progress_detail(
        db=db, user_id=TEST_USER_ID, search_space_id=TEST_SPACE_ID
    )
    attempts = out["detail"]["recent_attempts"]
    assert [a["problem_id"] for a in attempts] == ["p-new", "p-old"]
    assert attempts[0]["score"] == 85
    assert attempts[0]["letter"] == "A-"
    assert attempts[0]["concept_display_name"] == "Newton's Second Law"
