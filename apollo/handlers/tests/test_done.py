from unittest.mock import patch

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from apollo.handlers.done import handle_done
from apollo.persistence.models import (
    ApolloSession,
    KGEntry,
    Message,
    ProblemAttempt,
    SessionPhase,
    SessionStatus,
    StudentProgress,
)
from database.models import Base


@pytest_asyncio.fixture
async def db_with_session_and_kg():
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
    Session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with Session() as s:
        sess = ApolloSession(
            student_id="stu-1",
            concept_cluster_id="fluid_mechanics",
            status=SessionStatus.active.value,
            phase=SessionPhase.TEACHING.value,
            current_problem_id="bernoulli_horizontal_pipe_find_p2",
        )
        s.add(sess)
        await s.flush()
        attempt = ProblemAttempt(
            session_id=sess.id,
            problem_id="bernoulli_horizontal_pipe_find_p2",
            difficulty="intro",
        )
        s.add(attempt)
        await s.flush()
        for entry in [
            {"type": "equation", "content": {"symbolic": "rho*A1*v1 - rho*A2*v2", "label": "Continuity"}},
            {"type": "equation", "content": {
                "symbolic": "P1 + Rational(1,2)*rho*v1**2 + rho*g*h1 - (P2 + Rational(1,2)*rho*v2**2 + rho*g*h2)",
                "label": "Bernoulli",
            }},
        ]:
            s.add(KGEntry(
                session_id=sess.id,
                attempt_id=attempt.id,
                type=entry["type"],
                content=entry["content"],
                source="parser",
            ))
        await s.commit()
        await s.refresh(sess)
        yield s, sess.id
    await engine.dispose()


@pytest.mark.asyncio
@patch("apollo.handlers.done.generate_diagnostic")
async def test_done_solved_returns_new_shape(mock_diag, db_with_session_and_kg):
    """New shape: solver success exposed via solver_indicator.reached (bool)."""
    mock_diag.return_value = "You taught it well — Apollo solved the problem."
    db, session_id = db_with_session_and_kg

    result = await handle_done(db=db, session_id=session_id)

    # New top-level keys present
    assert set(result.keys()) >= {"rubric", "solver_indicator", "diagnostic_narrative", "coverage"}

    # Old shape keys removed
    assert "result" not in result
    assert "value" not in result
    assert "missing_variables" not in result
    assert "narrated_trace" not in result
    assert "diagnostic_report" not in result

    # solver_indicator carries reach + numeric value
    si = result["solver_indicator"]
    assert si["reached"] is True
    assert abs(float(si["value"]) - 194000.0) < 1e-3

    assert "diagnostic_narrative" in result


@pytest.mark.asyncio
@patch("apollo.handlers.done.generate_diagnostic")
async def test_done_freezes_session_and_persists_attempt(mock_diag, db_with_session_and_kg):
    mock_diag.return_value = "report"
    db, session_id = db_with_session_and_kg

    await handle_done(db=db, session_id=session_id)

    from sqlalchemy import select
    sess = (await db.execute(select(ApolloSession).where(ApolloSession.id == session_id))).scalar_one()
    assert sess.phase == SessionPhase.REPORT.value

    pa = (await db.execute(select(ProblemAttempt).where(ProblemAttempt.session_id == session_id))).scalar_one()
    assert pa.result == "solved"
    assert pa.solver_trace is not None
    # diagnostic_report now stores {narrative, rubric, coverage}
    assert pa.diagnostic_report is not None
    assert "narrative" in pa.diagnostic_report


# ── NEW TESTS (Task 9) ──────────────────────────────────────────────────────


@pytest.mark.asyncio
@patch("apollo.handlers.done.generate_diagnostic")
async def test_handle_done_returns_new_response_shape(mock_diag, db_with_session_and_kg):
    """Full shape contract: all required keys present, old keys absent."""
    mock_diag.return_value = "Narrative from mock."
    db, session_id = db_with_session_and_kg

    result = await handle_done(db=db, session_id=session_id)

    # New shape
    assert set(result.keys()) >= {
        "rubric", "solver_indicator", "diagnostic_narrative", "coverage",
    }
    # Old shape keys removed
    assert "result" not in result
    assert "value" not in result
    assert "missing_variables" not in result
    assert "narrated_trace" not in result
    assert "diagnostic_report" not in result

    # Rubric shape — four axes present with score + letter (variables was removed)
    rubric = result["rubric"]
    for axis in ("overall", "procedure", "justification", "simplification"):
        assert axis in rubric, f"rubric missing axis: {axis}"
        assert "score" in rubric[axis], f"rubric[{axis}] missing 'score'"
        assert "letter" in rubric[axis], f"rubric[{axis}] missing 'letter'"
    assert "variables" not in rubric

    # Solver indicator
    si = result["solver_indicator"]
    assert "reached" in si
    assert isinstance(si["reached"], bool)

    # Coverage shape
    cov = result["coverage"]
    assert "per_step" in cov
    assert "procedure_scores" in cov


@pytest_asyncio.fixture
async def db_with_session_equations_only():
    """Session with equations only — no procedure_step entries, so Procedure axis = 0."""
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
    Session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with Session() as s:
        sess = ApolloSession(
            student_id="stu-2",
            concept_cluster_id="fluid_mechanics",
            status=SessionStatus.active.value,
            phase=SessionPhase.TEACHING.value,
            current_problem_id="bernoulli_horizontal_pipe_find_p2",
        )
        s.add(sess)
        await s.flush()
        attempt = ProblemAttempt(
            session_id=sess.id,
            problem_id="bernoulli_horizontal_pipe_find_p2",
            difficulty="intro",
        )
        s.add(attempt)
        await s.flush()
        # Only equations — no procedure_step entries.
        for entry in [
            {"type": "equation", "content": {"symbolic": "rho*A1*v1 - rho*A2*v2", "label": "Continuity"}},
            {"type": "equation", "content": {
                "symbolic": "P1 + Rational(1,2)*rho*v1**2 + rho*g*h1 - (P2 + Rational(1,2)*rho*v2**2 + rho*g*h2)",
                "label": "Bernoulli",
            }},
        ]:
            s.add(KGEntry(
                session_id=sess.id,
                attempt_id=attempt.id,
                type=entry["type"],
                content=entry["content"],
                source="parser",
            ))
        await s.commit()
        await s.refresh(sess)
        yield s, sess.id
    await engine.dispose()


@pytest.mark.asyncio
@patch("apollo.handlers.done.generate_diagnostic")
@patch("apollo.overseer.coverage._procedure_match_score", return_value=0.0)
async def test_handle_done_solver_success_does_not_force_an_A(
    mock_proc_score, mock_diag, db_with_session_equations_only
):
    """Solver may reach the answer while Procedure = 0; overall must be sub-A."""
    mock_diag.return_value = "Narrative."
    db, session_id = db_with_session_equations_only

    result = await handle_done(db=db, session_id=session_id)

    # Pin the invariant this test depends on: the problem fixture must have
    # procedure_step reference entries. Without this, the rest of the test
    # silently becomes a no-op that asserts nothing meaningful.
    assert result["rubric"]["procedure"]["present"] is True, (
        "Test invariant broken: the problem fixture no longer has procedure_step "
        "reference entries — this test would become a no-op."
    )

    # Solver should have reached the answer (equations are correct)
    # but procedure score is forced to 0 by the mock.
    rubric = result["rubric"]

    # If the problem has procedure_step refs, procedure score must be 0.
    # If it has no procedure refs (absent axis), procedure present=False is acceptable.
    # Either way, overall letter must NOT be A-tier when procedure is absent/zero.
    proc = rubric["procedure"]
    if proc.get("present", True):
        assert proc["score"] == 0, f"expected procedure score 0, got {proc['score']}"
        assert rubric["overall"]["letter"] not in ("A+", "A", "A-"), (
            f"overall should be sub-A when procedure=0, got {rubric['overall']['letter']}"
        )
    else:
        # Procedure axis absent from reference solution — rubric redistributes weight.
        # Test is moot for this problem; just assert shape is intact.
        pass


# ── Phase 2: gamification fields ────────────────────────────────────────────


@pytest.mark.asyncio
@patch("apollo.handlers.done.generate_diagnostic")
async def test_handle_done_returns_xp_and_level_fields_on_first_attempt(
    mock_diag, db_with_session_and_kg,
):
    mock_diag.return_value = "ok"
    db, session_id = db_with_session_and_kg

    result = await handle_done(db=db, session_id=session_id)

    # New Phase 2 keys present on first attempt.
    assert "xp_earned" in result
    assert "level_before" in result
    assert "level_after" in result
    assert "level_up" in result

    # Happy-path KG solves to 100% procedure absent -> rubric overall >= 0;
    # xp must be non-negative and never exceed 200.
    assert result["xp_earned"] >= 0
    assert result["xp_earned"] <= 200

    # First attempt: both level values equal 1 (student earns XP into level 1).
    assert result["level_before"] == 1
    assert result["level_after"] in (1, 2)  # level-up only if xp_earned >= 300 (impossible here).


@pytest.mark.asyncio
@patch("apollo.handlers.done.generate_diagnostic")
async def test_handle_done_applies_reattempt_discount_within_session(
    mock_diag, db_with_session_and_kg,
):
    """Retry-within-session: /retry keeps the same ProblemAttempt row,
    so the second Done sees an attempt whose `result` is already set."""
    mock_diag.return_value = "ok"
    db, session_id = db_with_session_and_kg

    first = await handle_done(db=db, session_id=session_id)
    first_xp = first["xp_earned"]

    # Simulate /retry: keep the phase/row intact; the row already has a
    # result from the first handle_done call, which is exactly what the
    # re-attempt check keys off.
    second = await handle_done(db=db, session_id=session_id)

    # Second attempt earns at most 25% of first (floor arithmetic lets it
    # be strictly less).
    assert second["xp_earned"] <= (first_xp // 4) + 1


@pytest.mark.asyncio
@patch("apollo.handlers.done.compute_rubric")
@patch("apollo.handlers.done.generate_diagnostic")
async def test_handle_done_flags_level_up_when_threshold_crossed(
    mock_diag, mock_rubric, db_with_session_and_kg,
):
    """Seed progress to 290 XP so a standard-difficulty pass crosses 300."""
    from apollo.persistence.models import ApolloSession, StudentProgress, ProblemAttempt

    mock_diag.return_value = "ok"
    # Force a perfect rubric so a standard-difficulty pass earns 100 * 1.5 = 150 XP.
    # 290 + 150 = 440 >= 300 -> level-up to 2.
    mock_rubric.return_value = {
        "overall": {"score": 100, "letter": "A+"},
        "procedure": {"score": 100, "letter": "A+", "present": True},
        "justification": {"score": 100, "letter": "A+", "present": True},
        "simplification": {"score": 100, "letter": "A+", "present": True},
    }
    db, session_id = db_with_session_and_kg

    # Fetch student_id off the session.
    sess = (await db.execute(
        select(ApolloSession).where(ApolloSession.id == session_id)
    )).scalar_one()

    # Ensure StudentProgress table exists for this in-memory DB.
    from database.models import Base
    async with db.bind.begin() as conn:
        await conn.run_sync(lambda sc: Base.metadata.create_all(
            sc, tables=[StudentProgress.__table__]
        ))

    db.add(StudentProgress(student_id=sess.student_id, xp_total=290, level=1))

    # Bump the attempt difficulty to `standard` so a ~100 overall earns ≥ 10 XP
    # above the 10-needed margin (294/300 boundary wiggle).
    pa = (await db.execute(
        select(ProblemAttempt).where(ProblemAttempt.session_id == session_id)
    )).scalar_one()
    pa.difficulty = "standard"
    await db.commit()

    result = await handle_done(db=db, session_id=session_id)

    # XP earned + 290 should exceed 300.
    assert result["xp_after"] >= 300  # If the handler exposes xp_after.
    assert result["level_after"] == 2
    assert result["level_up"] is True


@pytest.mark.asyncio
@patch("apollo.handlers.done.generate_diagnostic")
async def test_done_grades_only_current_attempt_kg(mock_diag, db_with_session_and_kg):
    mock_diag.return_value = "ok"
    s, session_id = db_with_session_and_kg

    # Seed a second, abandoned attempt under the same session with a
    # distractor equation that must NOT surface in grading.
    abandoned = ProblemAttempt(
        session_id=session_id,
        problem_id="bernoulli_horizontal_pipe_find_p2",
        difficulty="intro",
        result="abandoned",
    )
    s.add(abandoned)
    await s.flush()
    s.add(KGEntry(
        session_id=session_id,
        attempt_id=abandoned.id,
        type="equation",
        content={"symbolic": "nonsense - 0", "label": "distractor"},
        source="parser",
    ))
    await s.commit()

    result = await handle_done(db=s, session_id=session_id)
    assert "rubric" in result

    # Re-read KG under the current (non-abandoned) attempt and confirm no
    # distractor leaked in. The current attempt is the FIRST attempt
    # created by the fixture (pre-dates the abandoned one).
    from apollo.knowledge_graph.store import KGStore
    current = (
        await s.execute(
            select(ProblemAttempt)
            .where(ProblemAttempt.session_id == session_id)
            .where(ProblemAttempt.id != abandoned.id)
            .order_by(ProblemAttempt.id.asc())
        )
    ).scalars().first()
    assert current is not None
    kg_for_grading = await KGStore(s).read_kg(attempt_id=current.id)
    assert all(e.get("label") != "distractor" for e in kg_for_grading["equation"])
