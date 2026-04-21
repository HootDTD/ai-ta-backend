# Apollo Teaching Rigor — Phase 2 (Gamification) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship Phase 2 of Apollo Teaching Rigor — the cosmetic XP + level gamification layer designed in Section 5 of the spec. Every Done response carries `xp_earned`, `level_before`, `level_after`, `level_up`. A new `apollo_student_progress` table persists cumulative XP + current level per student. The frontend surfaces a rubric-adjacent XP line on the Done screen, a subtle level-up celebration when a threshold is crossed, a level-titled greeting on the Apollo page, and an XP chip in the home-page header. Avatar cosmetics ride on a `data-level` attribute with CSS tier accents (no new image assets — graphic swap is a follow-on).

**Architecture:** A pure-function `apollo/overseer/xp.py` module owns all formula logic (difficulty multipliers, re-attempt discount, floor semantics, tier table, level lookup, threshold progress). A thin `apollo/persistence/progress_repo.py` wraps the `apollo_student_progress` row as upsert-on-done with a return payload shaped for the Done response. `handle_done` reads the student's difficulty off the `ProblemAttempt` row, derives `is_reattempt` (current attempt already has a `result`, or any other graded attempt for this `student_id` + `problem_id` exists), computes XP, applies it, and folds the result into the response. A new `GET /apollo/progress/{student_id}` endpoint lets the home page show XP/level without starting a session. The frontend extends `DoneResponse`, adds a progress fetcher, renders the XP line + level-up banner on `ApolloReportPanel`, greets by level title on `ApolloPageClient`, and chips XP in the home header. All logic lives outside the solver and outside the rubric — nothing about Phase 1's correctness changes.

**Tech Stack:** Python 3.12 · FastAPI · SQLAlchemy async · asyncpg · Pydantic v2 · pytest + pytest-asyncio · Next.js 15 App Router · TypeScript.

**Spec:** `docs/superpowers/specs/2026-04-21-apollo-teaching-rigor-design.md` (Section 5 — Gamification).

**Prior phase:** `docs/superpowers/plans/2026-04-21-apollo-teaching-rigor-phase-1.md` — shipped. Rubric card, letter grade, and procedure-as-axis are live.

**Branches:** `ApolloV2` in both `ai-ta-backend` and `ai-ta-student-ui`. **Do NOT merge to main.** Every task ends in an atomic commit on `ApolloV2`.

**Repository roots:**
- Backend: `/Users/ishaanbatra/Documents/GitHub/ai-ta-backend` — all paths below are relative to this unless prefixed `frontend:`.
- Frontend: `/Users/ishaanbatra/Documents/GitHub/ai-ta-student-ui` — paths prefixed `frontend:` are relative to this.

---

## Spec ↔ Reality Reconciliation (read before coding)

The spec was written assuming design intent; the shipped code is the ground truth. The following three divergences are **accepted as-is** for this phase — the plan codes to reality, not to the spec.

1. **Difficulty tiers:** spec Section 5 lists four difficulty multipliers (Beginner ×1.0, Easy ×1.25, Intermediate ×1.5, Challenging ×2.0). The database enum and problem JSONs only have **three** tiers: `intro`, `standard`, `hard` (see `database/migrations/009_apollo_slice0.sql:54` and `apollo/problems/bernoulli/problem_*.json`). Adding a fourth tier would require a new migration, re-authoring problems, and no user-visible surface exists yet to pick difficulty. The plan uses three multipliers:

    | DB value    | Multiplier | Max XP at 100 score |
    |-------------|-----------:|---------------------:|
    | `intro`     |       1.0  |                  100 |
    | `standard`  |       1.5  |                  150 |
    | `hard`      |       2.0  |                  200 |

    The spec's "Challenging A+ → 200 XP" cap is preserved by `hard × 100 = 200`. The four-tier variant can be added later by extending the CHECK constraint and the `DIFFICULTY_MULTIPLIERS` dict — neither breaks existing rows.

2. **Axis weights already differ from spec:** Phase 1 shipped with Procedure 0.60 / Justification 0.25 / Simplification 0.15 (Variables axis dropped). XP reads `rubric.overall.score` only, so Phase 2 is insensitive to axis weight changes.

3. **Re-attempt semantics in the current retry flow:** `handle_retry` (`apollo/handlers/lifecycle.py:14-19`) flips `phase` back to `TEACHING` but does **not** create a new `ProblemAttempt` row. A within-session retry therefore *overwrites* the existing attempt's `result` in `handle_done`. This plan treats both within-session retries and cross-session repeats as re-attempts for XP purposes: we check whether the current attempt row already has a non-null `result` (within-session retry) OR whether any other attempt row for the same `(student_id, problem_id)` has a non-null `result` (cross-session repeat).

---

## File Structure

**New files (backend):**
- `database/migrations/013_apollo_student_progress.sql` — the side table.
- `apollo/overseer/xp.py` — pure-function XP formula, level tiers, title lookup, next-threshold lookup.
- `apollo/overseer/tests/test_xp.py` — unit tests for xp.py (no DB, no LLM).
- `apollo/persistence/attempt_history.py` — `has_prior_graded_attempt` helper.
- `apollo/persistence/progress_repo.py` — `load_progress`, `apply_xp` repository functions.
- `apollo/persistence/tests/test_progress_model.py` — SQLAlchemy column / default round-trip.
- `apollo/persistence/tests/test_attempt_history.py` — re-attempt detection across sessions.
- `apollo/persistence/tests/test_progress_repo.py` — upsert, increment, level-up crossing.
- `apollo/handlers/progress.py` — `handle_get_progress` handler.
- `apollo/handlers/tests/test_progress.py` — handler unit tests.

**Modified files (backend):**
- `apollo/persistence/models.py` — add `StudentProgress` SQLAlchemy model.
- `apollo/handlers/done.py` — compute XP, upsert progress, fold into response.
- `apollo/handlers/tests/test_done.py` — assert new `xp_earned` / `level_before` / `level_after` / `level_up` fields; assert re-attempt discount; assert level-up flag.
- `apollo/api.py` — register `GET /apollo/progress/{student_id}` route.
- `apollo/tests/test_e2e_smoke.py` — add XP-present assertion to happy-path test.

**New files (frontend):**
- `frontend:app/api/apollo/progress/[student_id]/route.ts` — proxy to backend.

**Modified files (frontend):**
- `frontend:lib/apollo/api.ts` — extend `DoneResponse`; add `StudentProgress`, `Tier`, `getStudentProgress`.
- `frontend:components/apollo/ApolloReportPanel.tsx` — `+X XP earned` line + level-up banner.
- `frontend:app/apollo/ApolloPageClient.tsx` — level-titled greeting + `data-level` on `.apollo-avatar`.
- `frontend:app/page.tsx` — XP chip in header.
- `frontend:app/globals.css` — `.apollo-xp-line`, `.apollo-level-up`, confetti keyframes, `.apollo-avatar[data-level]` accents, `.apollo-xp-chip`.

---

## Conventions

- **TDD:** every backend task with testable logic writes the failing test first, runs it to see it fail, implements the minimum to pass, runs it again, commits.
- **Atomic commits:** one commit per task. Commit message format:
  ```
  <type>(apollo): <subject>

  <body if needed>

  Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
  ```
  Types: `feat`, `fix`, `test`, `chore`, `refactor`, `docs`.
- **Test runner:** backend tests use `pytest`. Always run with `-v --tb=short`.
- **In-memory DB for Python tests:** follow the existing pattern — `create_async_engine("sqlite+aiosqlite:///:memory:")` + `Base.metadata.create_all(..., tables=[...])`. Never touch Supabase in tests.
- **Never hit OpenAI in tests.** The only LLM caller in Phase 2 is the coverage matcher that Phase 1 already owns — we mock the Done-handler tests the same way Phase 1 did (`unittest.mock.patch("apollo.handlers.done.generate_diagnostic")`).
- **Imports:** always `from __future__ import annotations` at the top of new Python files.
- **Frontend:** no unit test harness exists. Verification runs `npm run lint` (from `/Users/ishaanbatra/Documents/GitHub/ai-ta-student-ui`) and `npm run build`. Manual browser verification is its own task at the end of the plan.
- **Never run destructive git commands** (reset --hard, force-push, branch -D). Ask before pushing.

---

## Task 1: `apollo_student_progress` migration + SQLAlchemy model

**Files:**
- Create: `database/migrations/013_apollo_student_progress.sql`
- Modify: `apollo/persistence/models.py` (add `StudentProgress`)
- Create: `apollo/persistence/tests/test_progress_model.py`

- [ ] **Step 1: Write the failing model test**

Create `apollo/persistence/tests/test_progress_model.py`:

```python
from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from apollo.persistence.models import StudentProgress
from database.models import Base


@pytest_asyncio.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(lambda sc: Base.metadata.create_all(sc, tables=[StudentProgress.__table__]))
    Session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with Session() as s:
        yield s
    await engine.dispose()


@pytest.mark.asyncio
async def test_student_progress_defaults(session):
    sp = StudentProgress(student_id="stu-1")
    session.add(sp)
    await session.commit()
    await session.refresh(sp)

    # Defaults match the spec.
    assert sp.xp_total == 0
    assert sp.level == 1
    assert sp.last_level_up_at is None


@pytest.mark.asyncio
async def test_student_progress_round_trip(session):
    session.add(StudentProgress(student_id="stu-2", xp_total=1700, level=4))
    await session.commit()

    loaded = (await session.execute(
        select(StudentProgress).where(StudentProgress.student_id == "stu-2")
    )).scalar_one()
    assert loaded.xp_total == 1700
    assert loaded.level == 4


@pytest.mark.asyncio
async def test_student_progress_student_id_is_primary_key(session):
    session.add(StudentProgress(student_id="stu-3"))
    await session.commit()

    # Inserting a second row with the same student_id should fail.
    session.add(StudentProgress(student_id="stu-3"))
    with pytest.raises(Exception):
        await session.commit()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest apollo/persistence/tests/test_progress_model.py -v --tb=short`
Expected: FAIL — `ImportError: cannot import name 'StudentProgress' from 'apollo.persistence.models'`.

- [ ] **Step 3: Add `StudentProgress` to `apollo/persistence/models.py`**

Open `apollo/persistence/models.py`. At the bottom of the file (after the `Index(...)` block at line 109-114), append:

```python


class StudentProgress(Base):
    __tablename__ = "apollo_student_progress"

    student_id = Column(Text, primary_key=True)
    xp_total = Column(Integer, nullable=False, default=0)
    level = Column(Integer, nullable=False, default=1)
    last_level_up_at = Column(TIMESTAMP(timezone=True), nullable=True)
```

The `Base`, `Column`, `Text`, `Integer`, and `TIMESTAMP` imports already exist at the top of the file — no new imports needed.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest apollo/persistence/tests/test_progress_model.py -v --tb=short`
Expected: PASS (3 tests).

- [ ] **Step 5: Create the SQL migration**

Create `database/migrations/013_apollo_student_progress.sql`:

```sql
-- 013_apollo_student_progress.sql
-- Apollo teaching-rigor phase 2 (gamification): persistent XP + level per
-- student, keyed by the same student_id string stored on apollo_sessions.
-- Kept in a side table so gamification state is isolated from identity /
-- auth state and can be reset without touching core student records.

BEGIN;

CREATE TABLE IF NOT EXISTS apollo_student_progress (
    student_id        TEXT        PRIMARY KEY,
    xp_total          INTEGER     NOT NULL DEFAULT 0,
    level             INTEGER     NOT NULL DEFAULT 1,
    last_level_up_at  TIMESTAMPTZ NULL
);

COMMIT;
```

- [ ] **Step 6: Commit**

```bash
cd /Users/ishaanbatra/Documents/GitHub/ai-ta-backend
git add database/migrations/013_apollo_student_progress.sql apollo/persistence/models.py apollo/persistence/tests/test_progress_model.py
git commit -m "$(cat <<'EOF'
feat(apollo): add apollo_student_progress table for gamification

Phase 2: side table persisting per-student cumulative XP + current level.
Keyed by student_id (TEXT) matching apollo_sessions.student_id. Defaults
xp_total=0, level=1. Includes SQLAlchemy model + SQLite round-trip tests.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

Then push to `ApolloV2`:

```bash
git push origin ApolloV2
```

---

## Task 2: Pure-function XP formula module

**Files:**
- Create: `apollo/overseer/xp.py`
- Create: `apollo/overseer/tests/test_xp.py`

- [ ] **Step 1: Write the failing xp tests**

Create `apollo/overseer/tests/test_xp.py`:

```python
from __future__ import annotations

import math

import pytest

from apollo.overseer.xp import (
    DIFFICULTY_MULTIPLIERS,
    LEVEL_TIERS,
    REATTEMPT_MULTIPLIER,
    compute_xp_earned,
    level_from_xp,
    next_tier_threshold,
    title_for_level,
)


# ── Constants & tier table ────────────────────────────────────────────────

def test_difficulty_multipliers_cover_db_values():
    # Keys MUST match the three difficulty values in
    # database/migrations/009_apollo_slice0.sql's apollo_problem_attempts
    # CHECK constraint. Drift here produces silent zero-XP awards.
    assert set(DIFFICULTY_MULTIPLIERS) == {"intro", "standard", "hard"}
    assert DIFFICULTY_MULTIPLIERS["intro"] == 1.0
    assert DIFFICULTY_MULTIPLIERS["standard"] == 1.5
    assert DIFFICULTY_MULTIPLIERS["hard"] == 2.0


def test_reattempt_multiplier_is_one_quarter():
    assert REATTEMPT_MULTIPLIER == 0.25


def test_level_tiers_cover_five_levels_with_spec_thresholds():
    # Spec Section 5: [0, 300, 800, 1600, 3000] with titles.
    thresholds = [t.threshold for t in LEVEL_TIERS]
    assert thresholds == [0, 300, 800, 1600, 3000]
    titles = [t.title for t in LEVEL_TIERS]
    assert titles == [
        "Apollo Apprentice",
        "Apollo Adept",
        "Apollo Scholar",
        "Apollo Sage",
        "Apollo Archon",
    ]
    levels = [t.level for t in LEVEL_TIERS]
    assert levels == [1, 2, 3, 4, 5]


# ── compute_xp_earned ─────────────────────────────────────────────────────

def test_compute_xp_earned_intro_first_attempt():
    # A=90, intro×1.0, first attempt -> 90 XP.
    assert compute_xp_earned(overall_score=90, difficulty="intro", is_reattempt=False) == 90


def test_compute_xp_earned_standard_a_grade():
    # Spec example: A-grade Intermediate ≈ 135 XP. Our `standard` maps to 1.5.
    # 90 * 1.5 = 135.
    assert compute_xp_earned(overall_score=90, difficulty="standard", is_reattempt=False) == 135


def test_compute_xp_earned_hard_max_is_200():
    # Spec cap: max XP per session is 200 (Challenging A+).
    # Our `hard` multiplier is 2.0; 100 overall caps at 200.
    assert compute_xp_earned(overall_score=100, difficulty="hard", is_reattempt=False) == 200


def test_compute_xp_earned_reattempt_is_quarter():
    # 100 * 1.5 * 0.25 = 37.5 -> floor -> 37.
    assert compute_xp_earned(overall_score=100, difficulty="standard", is_reattempt=True) == 37


def test_compute_xp_earned_uses_floor_semantics():
    # 65 * 1.5 = 97.5 -> floor -> 97.
    assert compute_xp_earned(overall_score=65, difficulty="standard", is_reattempt=False) == 97


def test_compute_xp_earned_zero_grade_awards_zero():
    # Spec: No negative XP ever — a bad grade just earns less.
    assert compute_xp_earned(overall_score=0, difficulty="hard", is_reattempt=False) == 0


def test_compute_xp_earned_never_negative():
    # Even hostile inputs can't produce a negative XP value.
    assert compute_xp_earned(overall_score=-5, difficulty="intro", is_reattempt=False) == 0


def test_compute_xp_earned_rejects_unknown_difficulty():
    with pytest.raises(ValueError):
        compute_xp_earned(overall_score=80, difficulty="expert", is_reattempt=False)


# ── level_from_xp ─────────────────────────────────────────────────────────

def test_level_from_xp_boundary_values():
    assert level_from_xp(0) == 1
    assert level_from_xp(299) == 1
    assert level_from_xp(300) == 2
    assert level_from_xp(799) == 2
    assert level_from_xp(800) == 3
    assert level_from_xp(1599) == 3
    assert level_from_xp(1600) == 4
    assert level_from_xp(2999) == 4
    assert level_from_xp(3000) == 5
    assert level_from_xp(10_000) == 5


def test_level_from_xp_rejects_negative():
    with pytest.raises(ValueError):
        level_from_xp(-1)


# ── title_for_level / next_tier_threshold ─────────────────────────────────

def test_title_for_level_matches_tier_table():
    assert title_for_level(1) == "Apollo Apprentice"
    assert title_for_level(5) == "Apollo Archon"


def test_title_for_level_rejects_out_of_range():
    with pytest.raises(ValueError):
        title_for_level(0)
    with pytest.raises(ValueError):
        title_for_level(6)


def test_next_tier_threshold_returns_next_threshold_for_non_max_levels():
    assert next_tier_threshold(1) == 300
    assert next_tier_threshold(2) == 800
    assert next_tier_threshold(3) == 1600
    assert next_tier_threshold(4) == 3000


def test_next_tier_threshold_returns_none_at_max_level():
    assert next_tier_threshold(5) is None


def test_next_tier_threshold_rejects_out_of_range():
    with pytest.raises(ValueError):
        next_tier_threshold(0)
    with pytest.raises(ValueError):
        next_tier_threshold(6)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest apollo/overseer/tests/test_xp.py -v --tb=short`
Expected: FAIL — `ModuleNotFoundError: No module named 'apollo.overseer.xp'`.

- [ ] **Step 3: Create `apollo/overseer/xp.py`**

Create `apollo/overseer/xp.py`:

```python
"""XP formula + level tier table (Phase 2 gamification).

Pure-function, no DB, no LLM. Deterministic, auditable, reproducible.

XP formula:
    xp_earned = floor(max(0, overall_score) * difficulty_multiplier * reattempt_factor)

where difficulty_multiplier is a lookup by the three DB-canonical values
(intro / standard / hard) and reattempt_factor is 1.0 on first attempt,
REATTEMPT_MULTIPLIER (0.25) otherwise.

Levels are a 5-tier progression with cumulative XP thresholds drawn from
Section 5 of docs/superpowers/specs/2026-04-21-apollo-teaching-rigor-design.md."""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict, List, Optional


DIFFICULTY_MULTIPLIERS: Dict[str, float] = {
    "intro": 1.0,
    "standard": 1.5,
    "hard": 2.0,
}

REATTEMPT_MULTIPLIER: float = 0.25


@dataclass(frozen=True)
class LevelTier:
    level: int
    title: str
    threshold: int


# Ascending by threshold. `level_from_xp` scans in reverse for the
# highest tier whose threshold <= xp.
LEVEL_TIERS: List[LevelTier] = [
    LevelTier(level=1, title="Apollo Apprentice", threshold=0),
    LevelTier(level=2, title="Apollo Adept", threshold=300),
    LevelTier(level=3, title="Apollo Scholar", threshold=800),
    LevelTier(level=4, title="Apollo Sage", threshold=1600),
    LevelTier(level=5, title="Apollo Archon", threshold=3000),
]

_TIER_BY_LEVEL: Dict[int, LevelTier] = {t.level: t for t in LEVEL_TIERS}
_MAX_LEVEL: int = max(_TIER_BY_LEVEL)


def compute_xp_earned(
    *,
    overall_score: int,
    difficulty: str,
    is_reattempt: bool,
) -> int:
    """Compute XP awarded for one Done event.

    Clamps negative scores to 0 (no negative XP, ever). Raises ValueError
    on unknown difficulty to surface upstream drift loudly rather than
    silently zero-award."""
    if difficulty not in DIFFICULTY_MULTIPLIERS:
        raise ValueError(
            f"unknown difficulty {difficulty!r}; "
            f"expected one of {sorted(DIFFICULTY_MULTIPLIERS)}"
        )
    base = max(0, int(overall_score))
    mult = DIFFICULTY_MULTIPLIERS[difficulty]
    raw = base * mult
    if is_reattempt:
        raw *= REATTEMPT_MULTIPLIER
    return int(math.floor(raw))


def level_from_xp(xp: int) -> int:
    """Resolve the level number for a cumulative XP total."""
    if xp < 0:
        raise ValueError(f"xp must be non-negative; got {xp}")
    for tier in reversed(LEVEL_TIERS):
        if xp >= tier.threshold:
            return tier.level
    return 1  # Unreachable because tier[0].threshold == 0.


def title_for_level(level: int) -> str:
    """Return the cosmetic title for a level number (1..5)."""
    tier = _TIER_BY_LEVEL.get(level)
    if tier is None:
        raise ValueError(f"level {level} is out of range (1..{_MAX_LEVEL})")
    return tier.title


def next_tier_threshold(level: int) -> Optional[int]:
    """Return the XP needed to reach the next tier, or None at max level."""
    if level not in _TIER_BY_LEVEL:
        raise ValueError(f"level {level} is out of range (1..{_MAX_LEVEL})")
    if level >= _MAX_LEVEL:
        return None
    return _TIER_BY_LEVEL[level + 1].threshold
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest apollo/overseer/tests/test_xp.py -v --tb=short`
Expected: PASS (all tests).

- [ ] **Step 5: Commit**

```bash
git add apollo/overseer/xp.py apollo/overseer/tests/test_xp.py
git commit -m "$(cat <<'EOF'
feat(apollo): add pure-function XP + level tier module

Phase 2: apollo/overseer/xp.py owns the XP formula
(floor(overall_score * difficulty_multiplier * reattempt_factor))
and the 5-tier level table. Three-tier multipliers match the
db-canonical difficulty values (intro/standard/hard). No DB, no LLM —
grades stay deterministic and auditable.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
git push origin ApolloV2
```

---

## Task 3: Re-attempt detection helper

**Files:**
- Create: `apollo/persistence/attempt_history.py`
- Create: `apollo/persistence/tests/test_attempt_history.py`

- [ ] **Step 1: Write the failing attempt-history tests**

Create `apollo/persistence/tests/test_attempt_history.py`:

```python
from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from apollo.persistence.attempt_history import has_prior_graded_attempt
from apollo.persistence.models import (
    ApolloSession,
    ProblemAttempt,
    SessionPhase,
    SessionStatus,
)
from database.models import Base


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(lambda sc: Base.metadata.create_all(
            sc, tables=[ApolloSession.__table__, ProblemAttempt.__table__]
        ))
    Session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with Session() as s:
        yield s
    await engine.dispose()


async def _mk_session(db: AsyncSession, student_id: str) -> ApolloSession:
    s = ApolloSession(
        student_id=student_id,
        concept_cluster_id="fluid_mechanics",
        status=SessionStatus.active.value,
        phase=SessionPhase.TEACHING.value,
        current_problem_id="p1",
    )
    db.add(s)
    await db.flush()
    return s


async def _mk_attempt(db: AsyncSession, *, session_id: int, problem_id: str,
                     result: str | None) -> ProblemAttempt:
    a = ProblemAttempt(
        session_id=session_id,
        problem_id=problem_id,
        difficulty="intro",
        result=result,
    )
    db.add(a)
    await db.flush()
    return a


@pytest.mark.asyncio
async def test_returns_false_when_no_prior_attempts(db):
    sess = await _mk_session(db, "stu-1")
    current = await _mk_attempt(db, session_id=sess.id, problem_id="p1", result=None)
    await db.commit()

    assert await has_prior_graded_attempt(
        db=db,
        student_id="stu-1",
        problem_id="p1",
        exclude_attempt_id=current.id,
    ) is False


@pytest.mark.asyncio
async def test_returns_true_when_prior_session_has_graded_attempt(db):
    # First session/attempt: graded (solved).
    sess_a = await _mk_session(db, "stu-2")
    await _mk_attempt(db, session_id=sess_a.id, problem_id="p1", result="solved")

    # Second session/attempt: pending grade.
    sess_b = await _mk_session(db, "stu-2")
    current = await _mk_attempt(db, session_id=sess_b.id, problem_id="p1", result=None)
    await db.commit()

    assert await has_prior_graded_attempt(
        db=db,
        student_id="stu-2",
        problem_id="p1",
        exclude_attempt_id=current.id,
    ) is True


@pytest.mark.asyncio
async def test_ignores_other_students(db):
    sess_other = await _mk_session(db, "stu-other")
    await _mk_attempt(db, session_id=sess_other.id, problem_id="p1", result="solved")

    sess_mine = await _mk_session(db, "stu-me")
    current = await _mk_attempt(db, session_id=sess_mine.id, problem_id="p1", result=None)
    await db.commit()

    assert await has_prior_graded_attempt(
        db=db,
        student_id="stu-me",
        problem_id="p1",
        exclude_attempt_id=current.id,
    ) is False


@pytest.mark.asyncio
async def test_ignores_other_problems(db):
    sess = await _mk_session(db, "stu-3")
    await _mk_attempt(db, session_id=sess.id, problem_id="p-other", result="solved")

    sess2 = await _mk_session(db, "stu-3")
    current = await _mk_attempt(db, session_id=sess2.id, problem_id="p1", result=None)
    await db.commit()

    assert await has_prior_graded_attempt(
        db=db,
        student_id="stu-3",
        problem_id="p1",
        exclude_attempt_id=current.id,
    ) is False


@pytest.mark.asyncio
async def test_excludes_current_attempt_id(db):
    # Only one row exists and it has a result. We must not count ourselves
    # as a prior attempt.
    sess = await _mk_session(db, "stu-4")
    current = await _mk_attempt(db, session_id=sess.id, problem_id="p1", result="solved")
    await db.commit()

    assert await has_prior_graded_attempt(
        db=db,
        student_id="stu-4",
        problem_id="p1",
        exclude_attempt_id=current.id,
    ) is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest apollo/persistence/tests/test_attempt_history.py -v --tb=short`
Expected: FAIL — `ModuleNotFoundError: No module named 'apollo.persistence.attempt_history'`.

- [ ] **Step 3: Create `apollo/persistence/attempt_history.py`**

Create `apollo/persistence/attempt_history.py`:

```python
"""Attempt-history queries used by the XP awarder.

A 're-attempt' for XP purposes is any Done event on a problem the student
has previously been graded on — across all their sessions. We detect it
by looking for any other ProblemAttempt row (joined through ApolloSession
to the student_id) for the same problem_id that already has a non-null
`result`. The current attempt id is excluded so a within-session retry —
which overwrites the same row after Phase 1's /retry endpoint — is
handled upstream by checking the attempt's own `result` before this call."""
from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from apollo.persistence.models import ApolloSession, ProblemAttempt


async def has_prior_graded_attempt(
    *,
    db: AsyncSession,
    student_id: str,
    problem_id: str,
    exclude_attempt_id: int,
) -> bool:
    """True iff another graded ProblemAttempt exists for this (student, problem)."""
    stmt = (
        select(func.count())
        .select_from(ProblemAttempt)
        .join(ApolloSession, ApolloSession.id == ProblemAttempt.session_id)
        .where(
            ApolloSession.student_id == student_id,
            ProblemAttempt.problem_id == problem_id,
            ProblemAttempt.result.is_not(None),
            ProblemAttempt.id != exclude_attempt_id,
        )
    )
    count = (await db.execute(stmt)).scalar_one()
    return count > 0
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest apollo/persistence/tests/test_attempt_history.py -v --tb=short`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add apollo/persistence/attempt_history.py apollo/persistence/tests/test_attempt_history.py
git commit -m "$(cat <<'EOF'
feat(apollo): add cross-session re-attempt detection helper

has_prior_graded_attempt joins ProblemAttempt to ApolloSession to answer
"has this student ever been graded on this problem before?" — excluding
the caller's own attempt row. Combined with an upstream check on the
current attempt's result (handles within-session retry), this covers
both re-attempt modes for XP discounting.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
git push origin ApolloV2
```

---

## Task 4: Progress repository (load + apply XP)

**Files:**
- Create: `apollo/persistence/progress_repo.py`
- Create: `apollo/persistence/tests/test_progress_repo.py`

- [ ] **Step 1: Write the failing repo tests**

Create `apollo/persistence/tests/test_progress_repo.py`:

```python
from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from apollo.persistence.models import StudentProgress
from apollo.persistence.progress_repo import apply_xp, load_progress
from database.models import Base


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(lambda sc: Base.metadata.create_all(
            sc, tables=[StudentProgress.__table__]
        ))
    Session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with Session() as s:
        yield s
    await engine.dispose()


@pytest.mark.asyncio
async def test_load_progress_creates_default_row_for_new_student(db):
    sp = await load_progress(db=db, student_id="stu-new")
    assert sp.student_id == "stu-new"
    assert sp.xp_total == 0
    assert sp.level == 1

    # Row was persisted.
    row = (await db.execute(
        select(StudentProgress).where(StudentProgress.student_id == "stu-new")
    )).scalar_one()
    assert row.xp_total == 0


@pytest.mark.asyncio
async def test_load_progress_returns_existing_row(db):
    db.add(StudentProgress(student_id="stu-1", xp_total=420, level=2))
    await db.commit()

    sp = await load_progress(db=db, student_id="stu-1")
    assert sp.xp_total == 420
    assert sp.level == 2


@pytest.mark.asyncio
async def test_apply_xp_zero_delta_is_idempotent(db):
    result = await apply_xp(db=db, student_id="stu-2", xp_delta=0)
    assert result == {
        "xp_before": 0,
        "xp_after": 0,
        "level_before": 1,
        "level_after": 1,
        "level_up": False,
    }

    # A second zero-delta call still leaves the row at (0, 1).
    result2 = await apply_xp(db=db, student_id="stu-2", xp_delta=0)
    assert result2["xp_after"] == 0
    assert result2["level_up"] is False


@pytest.mark.asyncio
async def test_apply_xp_increments_and_returns_before_after(db):
    result = await apply_xp(db=db, student_id="stu-3", xp_delta=90)
    assert result["xp_before"] == 0
    assert result["xp_after"] == 90
    assert result["level_before"] == 1
    assert result["level_after"] == 1
    assert result["level_up"] is False


@pytest.mark.asyncio
async def test_apply_xp_flags_level_up_when_threshold_crossed(db):
    # Seed at xp=250 (level 1). Adding 100 crosses the 300 boundary → level 2.
    db.add(StudentProgress(student_id="stu-4", xp_total=250, level=1))
    await db.commit()

    result = await apply_xp(db=db, student_id="stu-4", xp_delta=100)
    assert result["xp_before"] == 250
    assert result["xp_after"] == 350
    assert result["level_before"] == 1
    assert result["level_after"] == 2
    assert result["level_up"] is True

    # Row mutated in place.
    row = (await db.execute(
        select(StudentProgress).where(StudentProgress.student_id == "stu-4")
    )).scalar_one()
    assert row.xp_total == 350
    assert row.level == 2
    assert row.last_level_up_at is not None


@pytest.mark.asyncio
async def test_apply_xp_does_not_set_last_level_up_at_when_no_level_change(db):
    db.add(StudentProgress(student_id="stu-5", xp_total=10, level=1))
    await db.commit()

    await apply_xp(db=db, student_id="stu-5", xp_delta=50)

    row = (await db.execute(
        select(StudentProgress).where(StudentProgress.student_id == "stu-5")
    )).scalar_one()
    assert row.last_level_up_at is None


@pytest.mark.asyncio
async def test_apply_xp_rejects_negative_delta(db):
    with pytest.raises(ValueError):
        await apply_xp(db=db, student_id="stu-6", xp_delta=-5)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest apollo/persistence/tests/test_progress_repo.py -v --tb=short`
Expected: FAIL — `ModuleNotFoundError: No module named 'apollo.persistence.progress_repo'`.

- [ ] **Step 3: Create `apollo/persistence/progress_repo.py`**

Create `apollo/persistence/progress_repo.py`:

```python
"""Repository for apollo_student_progress rows.

Two public functions:
  - load_progress: return the student's progress row, creating a default
    (0 XP, level 1) row if missing.
  - apply_xp: add xp_delta, recompute level, stamp last_level_up_at on
    level change, and return a before/after summary suitable for the
    Done response's `xp_earned` / `level_before` / `level_after` /
    `level_up` fields.

Both functions commit. Callers should not wrap them in a nested
transaction — handle_done commits separately after updating the
problem attempt + session phase."""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Dict

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apollo.overseer.xp import level_from_xp
from apollo.persistence.models import StudentProgress


async def load_progress(*, db: AsyncSession, student_id: str) -> StudentProgress:
    row = (await db.execute(
        select(StudentProgress).where(StudentProgress.student_id == student_id)
    )).scalar_one_or_none()
    if row is not None:
        return row
    row = StudentProgress(student_id=student_id, xp_total=0, level=1)
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


async def apply_xp(
    *,
    db: AsyncSession,
    student_id: str,
    xp_delta: int,
) -> Dict[str, Any]:
    if xp_delta < 0:
        raise ValueError(f"xp_delta must be non-negative; got {xp_delta}")

    row = await load_progress(db=db, student_id=student_id)
    xp_before = row.xp_total
    level_before = row.level

    xp_after = xp_before + xp_delta
    level_after = level_from_xp(xp_after)
    level_up = level_after > level_before

    row.xp_total = xp_after
    row.level = level_after
    if level_up:
        row.last_level_up_at = datetime.now(UTC)

    await db.commit()

    return {
        "xp_before": xp_before,
        "xp_after": xp_after,
        "level_before": level_before,
        "level_after": level_after,
        "level_up": level_up,
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest apollo/persistence/tests/test_progress_repo.py -v --tb=short`
Expected: PASS (7 tests).

- [ ] **Step 5: Commit**

```bash
git add apollo/persistence/progress_repo.py apollo/persistence/tests/test_progress_repo.py
git commit -m "$(cat <<'EOF'
feat(apollo): add progress repository for XP upserts

load_progress returns an existing row or seeds (0, level 1).
apply_xp increments xp_total, recomputes level, stamps
last_level_up_at on crossing, and returns a before/after summary
shaped for the Done response.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
git push origin ApolloV2
```

---

## Task 5: Wire XP/level into `handle_done`

**Files:**
- Modify: `apollo/handlers/done.py`
- Modify: `apollo/handlers/tests/test_done.py`

- [ ] **Step 1: Add the failing handle_done XP tests**

Open `apollo/handlers/tests/test_done.py`. Append to the bottom of the file:

```python
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
@patch("apollo.handlers.done.generate_diagnostic")
async def test_handle_done_flags_level_up_when_threshold_crossed(
    mock_diag, db_with_session_and_kg,
):
    """Seed progress to 290 XP so a standard-difficulty pass crosses 300."""
    from apollo.persistence.models import ApolloSession, StudentProgress, ProblemAttempt

    mock_diag.return_value = "ok"
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
```

Note: the third test references `result["xp_after"]`. Our response shape from Task 5 Step 4 does expose `xp_after` (useful for the UI progress bar). If the handler implementation in Step 4 chooses to omit `xp_after` and only return `xp_earned`, change this assertion to `assert result["xp_earned"] + 290 >= 300`.

- [ ] **Step 2: Run tests to confirm they fail**

Run: `pytest apollo/handlers/tests/test_done.py -v --tb=short`
Expected: FAIL — the three new tests fail with `KeyError: 'xp_earned'` (or similar) because `handle_done` does not yet emit gamification fields.

- [ ] **Step 3: Wire XP into `handle_done`**

Open `apollo/handlers/done.py`. Replace the full file with:

```python
"""POST /apollo/sessions/{id}/done — freeze, solve, grade, narrate, award XP."""
from __future__ import annotations

from typing import Any, Dict

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apollo.knowledge_graph.store import KGStore
from apollo.overseer.coverage import compute_coverage
from apollo.overseer.diagnostic import generate_diagnostic
from apollo.overseer.problem_selector import list_problems_for_cluster
from apollo.overseer.rubric import compute_rubric
from apollo.overseer.xp import compute_xp_earned
from apollo.persistence.attempt_history import has_prior_graded_attempt
from apollo.persistence.models import ApolloSession, ProblemAttempt, SessionPhase
from apollo.persistence.progress_repo import apply_xp
from apollo.schemas.problem import Problem
from apollo.solver.forward_chain import solve_kg_against_problem
from apollo.solver.sympy_exec import _format_value_text


def _find_problem(cluster_id: str, problem_id: str) -> Problem:
    for p in list_problems_for_cluster(cluster_id):
        if p.id == problem_id:
            return p
    raise RuntimeError(f"problem {problem_id!r} not in bank for cluster {cluster_id!r}")


def _serializable_trace(trace: list) -> list:
    out = []
    for entry in trace:
        out.append({k: (str(v) if k == "value" else v) for k, v in entry.items()})
    return out


def _display_value(val) -> str | None:
    if val is None:
        return None
    return _format_value_text(val)


async def handle_done(*, db: AsyncSession, session_id: int) -> Dict[str, Any]:
    store = KGStore(db)

    sess = (await db.execute(select(ApolloSession).where(ApolloSession.id == session_id))).scalar_one()
    problem = _find_problem(sess.concept_cluster_id, sess.current_problem_id)

    await store.freeze(session_id)

    kg = await store.read_kg(session_id)
    sess.phase = SessionPhase.SOLVING.value
    await db.commit()

    augmented_givens = dict(problem.given_values)
    augmented_givens.setdefault("g", 9.81)
    for ref in problem.reference_solution:
        if ref.entry_type == "simplification":
            aw = (ref.content.get("applies_when") or "").lower().replace(" ", "")
            if "h1==h2" in aw:
                augmented_givens.setdefault("h1", 0.0)
                augmented_givens.setdefault("h2", 0.0)

    solver_result = solve_kg_against_problem(kg, {
        "id": problem.id,
        "given_values": augmented_givens,
        "target_unknown": problem.target_unknown,
    })

    reference_steps = [s.model_dump() for s in problem.reference_solution]
    coverage = compute_coverage(kg, reference_steps)
    rubric = compute_rubric(coverage, reference_steps)

    diagnostic_narrative = generate_diagnostic(
        coverage=coverage,
        solver_result=solver_result,
        reference_steps=reference_steps,
        problem_text=problem.problem_text,
        rubric=rubric,
    )

    solver_indicator: Dict[str, Any] = {
        "reached": solver_result["status"] == "solved",
    }
    value_str = _display_value(solver_result.get("value"))
    if value_str is not None:
        solver_indicator["value"] = value_str
    if solver_result.get("missing_variables"):
        solver_indicator["missing"] = solver_result["missing_variables"]

    attempt = (
        await db.execute(
            select(ProblemAttempt)
            .where(ProblemAttempt.session_id == session_id)
            .where(ProblemAttempt.problem_id == problem.id)
            .order_by(ProblemAttempt.id.desc())
        )
    ).scalars().first()

    # ── Phase 2: award XP based on the rubric score + difficulty.
    #
    # Re-attempt detection covers two cases:
    #   (a) Within-session retry: the /retry endpoint keeps the same
    #       ProblemAttempt row, so its `result` is already set from a
    #       previous Done call.
    #   (b) Cross-session repeat: another graded attempt exists for the
    #       same (student_id, problem_id).
    is_reattempt_in_session = attempt.result is not None
    is_reattempt_cross_session = await has_prior_graded_attempt(
        db=db,
        student_id=sess.student_id,
        problem_id=problem.id,
        exclude_attempt_id=attempt.id,
    )
    is_reattempt = is_reattempt_in_session or is_reattempt_cross_session

    xp_earned = compute_xp_earned(
        overall_score=rubric["overall"]["score"],
        difficulty=attempt.difficulty,
        is_reattempt=is_reattempt,
    )

    attempt.result = solver_result["status"]
    attempt.solver_trace = {
        "trace": _serializable_trace(solver_result["trace"]),
        "value": value_str,
        "missing_variables": solver_result.get("missing_variables", []),
    }
    attempt.diagnostic_report = {
        "narrative": diagnostic_narrative,
        "rubric": rubric,
        "coverage": coverage,
    }
    sess.phase = SessionPhase.REPORT.value
    await db.commit()

    # Apply XP after the attempt + session row updates have committed so
    # that a progress-repo failure never leaves the attempt ungraded.
    progress = await apply_xp(
        db=db,
        student_id=sess.student_id,
        xp_delta=xp_earned,
    )

    return {
        "rubric": rubric,
        "solver_indicator": solver_indicator,
        "diagnostic_narrative": diagnostic_narrative,
        "coverage": coverage,
        "xp_earned": xp_earned,
        "xp_before": progress["xp_before"],
        "xp_after": progress["xp_after"],
        "level_before": progress["level_before"],
        "level_after": progress["level_after"],
        "level_up": progress["level_up"],
    }
```

- [ ] **Step 4: Extend the test fixture so `StudentProgress.__table__` exists**

The existing `db_with_session_and_kg` fixture (test_done.py lines ~19-57) only creates four tables. The handler now also touches `StudentProgress`. Open `apollo/handlers/tests/test_done.py` and locate the fixture:

```python
    apollo_tables = [
        ApolloSession.__table__,
        KGEntry.__table__,
        Message.__table__,
        ProblemAttempt.__table__,
    ]
```

Change it to:

```python
    apollo_tables = [
        ApolloSession.__table__,
        KGEntry.__table__,
        Message.__table__,
        ProblemAttempt.__table__,
        StudentProgress.__table__,
    ]
```

And update the imports near the top of the file:

```python
from apollo.persistence.models import (
    ApolloSession,
    KGEntry,
    Message,
    ProblemAttempt,
    SessionPhase,
    SessionStatus,
)
```

to:

```python
from apollo.persistence.models import (
    ApolloSession,
    KGEntry,
    Message,
    ProblemAttempt,
    SessionPhase,
    SessionStatus,
    StudentProgress,
)
```

If the file has a *second* fixture with its own tables list (test_done.py line ~157), update it the same way.

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest apollo/handlers/tests/test_done.py -v --tb=short`
Expected: PASS — all existing tests AND the three new Phase 2 tests pass.

- [ ] **Step 6: Commit**

```bash
git add apollo/handlers/done.py apollo/handlers/tests/test_done.py
git commit -m "$(cat <<'EOF'
feat(apollo): award XP and level on handle_done

Phase 2 wires compute_xp_earned + apply_xp into the Done handler.
Detects re-attempt via (a) current attempt row already has a result
(within-session retry) or (b) another graded attempt exists for the
same (student_id, problem_id). Response gains xp_earned, xp_before,
xp_after, level_before, level_after, level_up.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
git push origin ApolloV2
```

---

## Task 6: New endpoint `GET /apollo/progress/{student_id}`

The home page and Apollo greeting need current XP/level without starting a session.

**Files:**
- Create: `apollo/handlers/progress.py`
- Create: `apollo/handlers/tests/test_progress.py`
- Modify: `apollo/api.py` — register the route.

- [ ] **Step 1: Write the failing progress handler tests**

Create `apollo/handlers/tests/test_progress.py`:

```python
from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from apollo.handlers.progress import handle_get_progress
from apollo.persistence.models import StudentProgress
from database.models import Base


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(lambda sc: Base.metadata.create_all(
            sc, tables=[StudentProgress.__table__]
        ))
    Session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with Session() as s:
        yield s
    await engine.dispose()


@pytest.mark.asyncio
async def test_get_progress_unknown_student_returns_defaults(db):
    payload = await handle_get_progress(db=db, student_id="new-student")
    assert payload == {
        "student_id": "new-student",
        "xp_total": 0,
        "level": 1,
        "title": "Apollo Apprentice",
        "next_tier_threshold": 300,
    }


@pytest.mark.asyncio
async def test_get_progress_known_student_returns_stored(db):
    db.add(StudentProgress(student_id="veteran", xp_total=1800, level=4))
    await db.commit()

    payload = await handle_get_progress(db=db, student_id="veteran")
    assert payload == {
        "student_id": "veteran",
        "xp_total": 1800,
        "level": 4,
        "title": "Apollo Sage",
        "next_tier_threshold": 3000,
    }


@pytest.mark.asyncio
async def test_get_progress_max_level_student_has_null_next_threshold(db):
    db.add(StudentProgress(student_id="max", xp_total=3500, level=5))
    await db.commit()

    payload = await handle_get_progress(db=db, student_id="max")
    assert payload["level"] == 5
    assert payload["next_tier_threshold"] is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest apollo/handlers/tests/test_progress.py -v --tb=short`
Expected: FAIL — `ModuleNotFoundError: No module named 'apollo.handlers.progress'`.

- [ ] **Step 3: Create `apollo/handlers/progress.py`**

Create `apollo/handlers/progress.py`:

```python
"""GET /apollo/progress/{student_id} — surface XP + level for the UI."""
from __future__ import annotations

from typing import Any, Dict

from sqlalchemy.ext.asyncio import AsyncSession

from apollo.overseer.xp import next_tier_threshold, title_for_level
from apollo.persistence.progress_repo import load_progress


async def handle_get_progress(
    *,
    db: AsyncSession,
    student_id: str,
) -> Dict[str, Any]:
    row = await load_progress(db=db, student_id=student_id)
    return {
        "student_id": row.student_id,
        "xp_total": row.xp_total,
        "level": row.level,
        "title": title_for_level(row.level),
        "next_tier_threshold": next_tier_threshold(row.level),
    }
```

- [ ] **Step 4: Register the route in `apollo/api.py`**

Open `apollo/api.py`. Add the import near the other handler imports (around line 23):

```python
from apollo.handlers.progress import handle_get_progress
```

Then add this route handler before the exception-handler section (after the `end` route at line 91):

```python
@router.get("/progress/{student_id}")
async def progress(
    student_id: str,
    db: AsyncSession = Depends(get_db_session),
) -> dict:
    return await handle_get_progress(db=db, student_id=student_id)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest apollo/handlers/tests/test_progress.py -v --tb=short`
Expected: PASS (3 tests).

- [ ] **Step 6: Commit**

```bash
git add apollo/handlers/progress.py apollo/handlers/tests/test_progress.py apollo/api.py
git commit -m "$(cat <<'EOF'
feat(apollo): add GET /apollo/progress/{student_id} endpoint

Returns xp_total, level, cosmetic title, and next-tier threshold.
Unknown students get a default (0 XP, level 1) row seeded via the
progress repo so the UI can render a consistent chip.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
git push origin ApolloV2
```

---

## Task 7: Extend frontend API client + proxy route

**Files:**
- Modify: `frontend:lib/apollo/api.ts`
- Create: `frontend:app/api/apollo/progress/[student_id]/route.ts`

- [ ] **Step 1: Extend `DoneResponse` and add progress types / fetcher**

Open `frontend:lib/apollo/api.ts`. Replace the `DoneResponse` interface (lines 80-88) with:

```typescript
export interface DoneResponse {
  rubric: Rubric;
  solver_indicator: SolverIndicator;
  diagnostic_narrative: string;
  coverage: {
    per_step: Record<string, string>;
    procedure_scores: Record<string, number>;
  };
  // Phase 2 gamification (may be absent if backend is mid-deploy).
  xp_earned?: number;
  xp_before?: number;
  xp_after?: number;
  level_before?: number;
  level_after?: number;
  level_up?: boolean;
}

export interface StudentProgress {
  student_id: string;
  xp_total: number;
  level: number;
  title: string;
  next_tier_threshold: number | null;
}
```

At the bottom of the file (after `endSession`), append:

```typescript
export async function getStudentProgress(studentId: string): Promise<StudentProgress> {
  const res = await fetch(`/api/apollo/progress/${encodeURIComponent(studentId)}`);
  return (await _handle(res)) as StudentProgress;
}
```

- [ ] **Step 2: Create the Next.js proxy route**

Create `frontend:app/api/apollo/progress/[student_id]/route.ts`:

```typescript
export const runtime = 'nodejs';

export async function GET(
  req: Request,
  ctx: { params: Promise<{ student_id: string }> },
) {
  const rawBackend = process.env.AI_TA_API_BASE_URL;
  const backend = rawBackend ? rawBackend.replace(/\/+$/, '') : '';
  if (!backend) {
    return new Response('AI_TA_API_BASE_URL missing', { status: 500 });
  }

  const { student_id } = await ctx.params;
  const authHeader = req.headers.get('authorization');
  const headers: Record<string, string> = {};
  if (authHeader) headers.Authorization = authHeader;

  const resp = await fetch(
    `${backend}/apollo/progress/${encodeURIComponent(student_id)}`,
    { method: 'GET', headers, cache: 'no-store' },
  );

  return new Response(resp.body, {
    status: resp.status,
    headers: {
      'Content-Type': resp.headers.get('content-type') ?? 'application/json',
      'Cache-Control': 'no-store',
    },
  });
}
```

- [ ] **Step 3: Lint + build**

```bash
cd /Users/ishaanbatra/Documents/GitHub/ai-ta-student-ui
npm run lint
npm run build
```

Expected: no errors. If Next.js complains about missing Supabase env vars during build, that's the existing behavior Phase 1 also hit — not a regression.

- [ ] **Step 4: Commit**

```bash
cd /Users/ishaanbatra/Documents/GitHub/ai-ta-student-ui
git add lib/apollo/api.ts app/api/apollo/progress/
git commit -m "$(cat <<'EOF'
feat(apollo): extend DoneResponse + add progress fetcher

Phase 2: DoneResponse gains optional xp_earned/xp_before/xp_after/
level_before/level_after/level_up. New StudentProgress type + proxy
route at /api/apollo/progress/[student_id] that forwards to the
backend's GET /apollo/progress/{student_id}.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
git push origin ApolloV2
```

---

## Task 8: XP line + level-up banner on `ApolloReportPanel`

**Files:**
- Modify: `frontend:components/apollo/ApolloReportPanel.tsx`
- Modify: `frontend:app/globals.css`

- [ ] **Step 1: Replace `ApolloReportPanel.tsx` with the gamified version**

Open `frontend:components/apollo/ApolloReportPanel.tsx`. Replace the entire file with:

```tsx
"use client";

import { Fragment } from "react";
import { InlineMath } from "react-katex";
import "katex/dist/katex.min.css";

import type { DoneResponse, Rubric, RubricAxis } from "@/lib/apollo/api";

interface Props {
  report: DoneResponse;
  onRetry: () => void;
  onEnd: () => void;
  busy?: boolean;
}

const BAR_CELLS = 8;
const PASS_SCORE = 75;

const AXIS_LABELS: Record<keyof Omit<Rubric, "overall">, string> = {
  procedure: "Procedure",
  justification: "Justification",
  simplification: "Simplification",
};

function renderWithMath(text: string) {
  const parts = text.split(/(\$[^$]+\$)/g);
  return parts.map((part, i) => {
    if (part.startsWith("$") && part.endsWith("$")) {
      const tex = part.slice(1, -1);
      return <InlineMath key={i} math={tex} />;
    }
    return <Fragment key={i}>{part}</Fragment>;
  });
}

function AxisRow({ label, axis }: { label: string; axis: RubricAxis }) {
  const filled = Math.round((axis.score / 100) * BAR_CELLS);
  const bar = "█".repeat(filled) + "▒".repeat(BAR_CELLS - filled);
  return (
    <div
      className="apollo-rubric__row"
      data-present={axis.present === false ? "false" : "true"}
    >
      <span>{label}</span>
      <span>{axis.letter}</span>
      <span>({axis.score})</span>
      <span aria-hidden>{bar}</span>
    </div>
  );
}

function solverOutcomeText(
  si: DoneResponse["solver_indicator"],
): string {
  if (si.reached) {
    return si.value
      ? `Apollo reached the answer: ${si.value} ✓`
      : "Apollo reached the answer ✓";
  }
  const missing = si.missing && si.missing.length > 0
    ? ` — missing: ${si.missing.join(", ")}`
    : "";
  return `Apollo got stuck${missing}`;
}

export default function ApolloReportPanel({ report, onRetry, onEnd, busy }: Props) {
  const { rubric, solver_indicator, diagnostic_narrative } = report;
  const tone = rubric.overall.score >= PASS_SCORE ? "success" : "danger";

  // Gamification fields are optional so older backend deploys still render.
  const xpEarned = report.xp_earned;
  const levelUp = report.level_up === true;
  const levelAfter = report.level_after;

  return (
    <section className="notice" data-tone={tone}>
      <div className="eyebrow">Teaching grade</div>
      <strong style={{ fontSize: "1.25rem" }}>
        {rubric.overall.letter} ({rubric.overall.score})
      </strong>

      <div className="apollo-rubric">
        <AxisRow label={AXIS_LABELS.procedure} axis={rubric.procedure} />
        <AxisRow label={AXIS_LABELS.justification} axis={rubric.justification} />
        <AxisRow label={AXIS_LABELS.simplification} axis={rubric.simplification} />
      </div>

      <p className="note" style={{ margin: "0.5rem 0" }}>
        {solverOutcomeText(solver_indicator)}
      </p>

      {typeof xpEarned === "number" && (
        <p className="apollo-xp-line">+{xpEarned} XP earned</p>
      )}

      {levelUp && typeof levelAfter === "number" && (
        <div className="apollo-level-up" role="status" aria-live="polite">
          <span className="apollo-level-up__confetti" aria-hidden>🎉</span>
          <span>
            Level up! You&apos;re now <strong>level {levelAfter}</strong>.
          </span>
        </div>
      )}

      <details open>
        <summary>Diagnostic narrative</summary>
        <div
          className="prose"
          style={{ whiteSpace: "pre-wrap", margin: "0.5rem 0 0" }}
        >
          {diagnostic_narrative.split("\n").map((line, i) => (
            <div key={i}>{renderWithMath(line)}</div>
          ))}
        </div>
      </details>

      <div className="composer-foot">
        <button
          onClick={onRetry}
          disabled={busy}
          type="button"
          className="ui-button ui-button--primary ui-button--small"
        >
          Teach more and retry
        </button>
        <button
          onClick={onEnd}
          disabled={busy}
          type="button"
          className="ui-button ui-button--small"
        >
          End session
        </button>
      </div>
    </section>
  );
}
```

- [ ] **Step 2: Add CSS for the new elements**

Open `frontend:app/globals.css`. Find a reasonable location in the Apollo CSS block (near the existing `.apollo-rubric__row` rules — search for `apollo-rubric` and append just below). Add:

```css
.apollo-xp-line {
  margin: 0.5rem 0;
  font-weight: 600;
  font-size: 0.95rem;
  color: var(--accent, currentColor);
}

.apollo-level-up {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  margin: 0.5rem 0;
  padding: 0.65rem 0.8rem;
  border: 1px solid var(--border);
  background: linear-gradient(90deg, rgba(255, 215, 0, 0.12), rgba(255, 215, 0, 0.02));
  border-radius: 6px;
  position: relative;
  overflow: hidden;
  animation: apolloLevelUpSlide 450ms cubic-bezier(0.22, 1, 0.36, 1);
}

.apollo-level-up__confetti {
  display: inline-block;
  font-size: 1.15rem;
  animation: apolloLevelUpPop 600ms ease-out 120ms both;
}

@keyframes apolloLevelUpSlide {
  from { opacity: 0; transform: translateY(6px); }
  to   { opacity: 1; transform: translateY(0); }
}

@keyframes apolloLevelUpPop {
  0%   { transform: scale(0.4) rotate(-10deg); }
  60%  { transform: scale(1.25) rotate(8deg); }
  100% { transform: scale(1) rotate(0deg); }
}
```

- [ ] **Step 3: Lint + build**

```bash
cd /Users/ishaanbatra/Documents/GitHub/ai-ta-student-ui
npm run lint
npm run build
```

Expected: no errors.

- [ ] **Step 4: Commit**

```bash
git add components/apollo/ApolloReportPanel.tsx app/globals.css
git commit -m "$(cat <<'EOF'
feat(apollo): render XP line + level-up banner on Done screen

Phase 2: ApolloReportPanel renders '+N XP earned' below the solver
outcome and a subtle confetti-style level-up banner when
report.level_up is true. Uses pure CSS keyframes (slide + pop) —
no new runtime deps. Fields are optional so older backend deploys
still render the rubric cleanly.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
git push origin ApolloV2
```

---

## Task 9: Level-titled greeting + avatar `data-level` on Apollo page

**Files:**
- Modify: `frontend:app/apollo/ApolloPageClient.tsx`
- Modify: `frontend:components/apollo/ApolloChat.tsx`
- Modify: `frontend:app/globals.css`

- [ ] **Step 1: Extend `ApolloPageClient.tsx` to fetch + display progress**

Open `frontend:app/apollo/ApolloPageClient.tsx`. Replace the full file with:

```tsx
"use client";

import { useEffect, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";

import {
  ApolloApiError,
  endSession,
  finishTeaching,
  getSessionState,
  getStudentProgress,
  retryProblem,
  type ApolloKG,
  type ApolloSessionState,
  type DoneResponse,
  type StudentProgress,
} from "@/lib/apollo/api";
import ApolloChat from "@/components/apollo/ApolloChat";
import ApolloErrorSurface from "@/components/apollo/ApolloErrorSurface";
import ApolloKGPanel from "@/components/apollo/ApolloKGPanel";
import ApolloProblemPanel from "@/components/apollo/ApolloProblemPanel";
import ApolloReportPanel from "@/components/apollo/ApolloReportPanel";

export default function ApolloPageClient() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const sessionId = Number(searchParams.get("session"));

  const returnLink = (
    <button
      type="button"
      className="apollo-return-link"
      onClick={() => router.push("/")}
    >
      ← Return to Hoot
    </button>
  );

  const [state, setState] = useState<ApolloSessionState | null>(null);
  const [kg, setKg] = useState<ApolloKG | null>(null);
  const [report, setReport] = useState<DoneResponse | null>(null);
  const [progress, setProgress] = useState<StudentProgress | null>(null);
  const [error, setError] = useState<ApolloApiError | Error | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!sessionId) return;
    getSessionState(sessionId)
      .then((s) => {
        setState(s);
        setKg(s.kg);
        // Fetch progress for the greeting + avatar level. Non-blocking;
        // errors fall back silently (greeting renders level 1 defaults).
        getStudentProgress(s.student_id)
          .then(setProgress)
          .catch(() => setProgress(null));
      })
      .catch((e) => setError(e as Error));
  }, [sessionId]);

  // After any Done event, refresh progress so the greeting/avatar update
  // on a level-up without requiring a page reload.
  useEffect(() => {
    if (!report || !state) return;
    getStudentProgress(state.student_id)
      .then(setProgress)
      .catch(() => {});
  }, [report, state]);

  async function handleDone() {
    if (!sessionId) return;
    setBusy(true);
    setError(null);
    try {
      const r = await finishTeaching(sessionId);
      setReport(r);
    } catch (e) {
      setError(e as Error);
    } finally {
      setBusy(false);
    }
  }

  async function handleRetry() {
    if (!sessionId) return;
    setBusy(true);
    setError(null);
    try {
      await retryProblem(sessionId);
      setReport(null);
      const fresh = await getSessionState(sessionId);
      setState(fresh);
      setKg(fresh.kg);
    } catch (e) {
      setError(e as Error);
    } finally {
      setBusy(false);
    }
  }

  async function handleEnd() {
    if (!sessionId) return;
    setBusy(true);
    try {
      await endSession(sessionId);
      setReport(null);
      const fresh = await getSessionState(sessionId);
      setState(fresh);
    } catch (e) {
      setError(e as Error);
    } finally {
      setBusy(false);
    }
  }

  if (!sessionId) {
    return (
      <main className="apollo-page">
        <nav className="apollo-page__nav">{returnLink}</nav>
        <div className="apollo-page__main">
          <div className="notice" data-tone="danger">
            Missing ?session=N query parameter.
          </div>
        </div>
      </main>
    );
  }

  if (!state) {
    return (
      <main className="apollo-page">
        <nav className="apollo-page__nav">{returnLink}</nav>
        <div className="apollo-page__main">
          <div className="card">
            <span>Loading session…</span>
          </div>
        </div>
      </main>
    );
  }

  if (state.status === "ended") {
    return (
      <main className="apollo-page">
        <nav className="apollo-page__nav">{returnLink}</nav>
        <div className="apollo-page__main">
          <div className="module">
            <h1 className="section-title">Session ended</h1>
            <p className="lede">You&apos;ve ended this Apollo session.</p>
          </div>
        </div>
      </main>
    );
  }

  const levelForAvatar = progress?.level ?? 1;
  const titleForGreeting = progress?.title ?? "Apollo Apprentice";

  return (
    <main className="apollo-page" data-apollo-level={levelForAvatar}>
      <nav className="apollo-page__nav">{returnLink}</nav>
      <div className="apollo-page__main">
        <p className="apollo-greeting">
          Welcome back, <strong>{titleForGreeting}</strong>.
        </p>
        <ApolloProblemPanel problem={state.problem} />
        <ApolloErrorSurface error={error} onDismiss={() => setError(null)} />
        {report ? (
          <ApolloReportPanel report={report} onRetry={handleRetry} onEnd={handleEnd} busy={busy} />
        ) : (
          <ApolloChat
            sessionId={sessionId}
            initialMessages={state.messages.map((m) => ({ role: m.role, content: m.content }))}
            onKgUpdate={(newKg) => setKg(newKg)}
            onDoneClicked={handleDone}
            disabled={busy}
          />
        )}
      </div>
      <aside className="apollo-page__aside">{kg && <ApolloKGPanel kg={kg} />}</aside>
    </main>
  );
}
```

- [ ] **Step 2: Add greeting + avatar-level CSS**

Open `frontend:app/globals.css`. Append to the end of the Apollo block (or anywhere after the existing `.apollo-avatar` rule at line 502):

```css
.apollo-greeting {
  margin: 0 0 0.75rem;
  font-size: 0.95rem;
  color: var(--muted, currentColor);
}

/* Avatar cosmetic accents per level tier. Asset swap is a follow-on; for
   now we shift the hue/saturation of the existing owl image so higher
   tiers look visibly distinct without requiring new art. */
.apollo-page[data-apollo-level="2"] .apollo-avatar {
  filter: invert(var(--owl-invert)) hue-rotate(12deg) saturate(1.05);
}
.apollo-page[data-apollo-level="3"] .apollo-avatar {
  filter: invert(var(--owl-invert)) hue-rotate(25deg) saturate(1.10);
}
.apollo-page[data-apollo-level="4"] .apollo-avatar {
  filter: invert(var(--owl-invert)) hue-rotate(40deg) saturate(1.15)
          drop-shadow(0 0 3px rgba(255, 215, 0, 0.4));
}
.apollo-page[data-apollo-level="5"] .apollo-avatar {
  filter: invert(var(--owl-invert)) hue-rotate(55deg) saturate(1.25)
          drop-shadow(0 0 5px rgba(255, 215, 0, 0.65));
}
```

Note: `.apollo-avatar` currently has a `mix-blend-mode` rule that inherits from the parent; leaving it untouched and only overriding `filter` keeps compatibility with dark/light themes (see `--owl-invert` / `--owl-blend` at globals.css:43-44).

- [ ] **Step 3: Lint + build**

```bash
cd /Users/ishaanbatra/Documents/GitHub/ai-ta-student-ui
npm run lint
npm run build
```

Expected: no errors.

- [ ] **Step 4: Commit**

```bash
git add app/apollo/ApolloPageClient.tsx app/globals.css
git commit -m "$(cat <<'EOF'
feat(apollo): greet by level + tier avatar accents on Apollo page

Phase 2: ApolloPageClient fetches student progress on mount and
re-fetches after Done so a level-up updates the greeting without a
reload. Greeting reads 'Welcome back, Apollo <Title>'. Avatar cosmetic
variants ride on data-apollo-level on the <main> — a hue/saturation
shift per tier plus a gold drop-shadow at L4/L5. No new assets.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
git push origin ApolloV2
```

---

## Task 10: XP chip on the Home page (`app/page.tsx`)

The home header already renders a class dropdown on the left, the Hoot brand in the center, and a menu on the right. We'll add an XP chip between the class dropdown and the brand (on the same left flex cluster) so it's visible whenever the student is signed in. The chip hides itself if the progress fetch fails.

**Files:**
- Modify: `frontend:app/page.tsx`
- Modify: `frontend:app/globals.css`

- [ ] **Step 1: Fetch progress on auth-ready and render the chip**

Open `frontend:app/page.tsx`.

Extend the Apollo import near line 24 so it includes the progress helpers:

```typescript
import { startSessionFromHoot, ApolloApiError, getStudentProgress, type StudentProgress } from '@/lib/apollo/api';
```

Add a new piece of state alongside the other Apollo-related state (near line 208 where `apolloError` and `apolloStarting` live):

```typescript
  const [apolloProgress, setApolloProgress] = useState<StudentProgress | null>(null);
```

Add a new effect after the existing `useEffect` that reacts to `[accessToken, authReady]` (around line 386). Place it directly under that block:

```typescript
  useEffect(() => {
    if (!authReady || !session?.user_id) {
      setApolloProgress(null);
      return;
    }
    let cancelled = false;
    getStudentProgress(session.user_id)
      .then((p) => { if (!cancelled) setApolloProgress(p); })
      .catch(() => { if (!cancelled) setApolloProgress(null); });
    return () => { cancelled = true; };
  }, [authReady, session?.user_id]);
```

Find the header's left cluster (search for `<div className="flex items-center gap-2 relative z-[1]">` at roughly line 1074-1135). The cluster currently contains the sidebar-open button + the class dropdown. Immediately AFTER the closing `</div>` of the class dropdown's `<AnimatePresence>` block (right after `)}</AnimatePresence>` and before the outer closing `</div>` that wraps the whole left cluster), insert:

```tsx
            {apolloProgress && (
              <div
                className="apollo-xp-chip"
                title={`Level ${apolloProgress.level} · ${apolloProgress.xp_total} XP`}
                aria-label={`${apolloProgress.title}, ${apolloProgress.xp_total} experience points`}
              >
                <span className="apollo-xp-chip__level">L{apolloProgress.level}</span>
                <span className="apollo-xp-chip__xp">{apolloProgress.xp_total} XP</span>
              </div>
            )}
```

The chip sits in the left cluster so it's naturally responsive with the flexbox.

- [ ] **Step 2: Add chip CSS**

Open `frontend:app/globals.css`. Append near the other `.apollo-*` rules:

```css
.apollo-xp-chip {
  display: inline-flex;
  align-items: center;
  gap: 0.35rem;
  height: 1.75rem;
  padding: 0 0.55rem;
  border: 1px solid var(--border);
  border-radius: 999px;
  background: var(--card-fill);
  font-size: 0.75rem;
  line-height: 1;
  white-space: nowrap;
}

.apollo-xp-chip__level {
  font-weight: 700;
  letter-spacing: 0.02em;
}

.apollo-xp-chip__xp {
  color: var(--muted, currentColor);
}
```

- [ ] **Step 3: Lint + build**

```bash
cd /Users/ishaanbatra/Documents/GitHub/ai-ta-student-ui
npm run lint
npm run build
```

Expected: no errors.

- [ ] **Step 4: Commit**

```bash
git add app/page.tsx app/globals.css
git commit -m "$(cat <<'EOF'
feat(apollo): add XP chip to Hoot home-page header

Phase 2: on auth-ready, fetch student progress via
GET /apollo/progress/{student_id} and render a compact chip
(LN · N XP) between the class dropdown and the brand. Falls back
silently if the fetch fails so the chip never blocks the page.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
git push origin ApolloV2
```

---

## Task 11: Update e2e smoke test for Phase 2 response shape

**Files:**
- Modify: `apollo/tests/test_e2e_smoke.py`

- [ ] **Step 1: Read the existing e2e test to locate the Done assertion**

Read `apollo/tests/test_e2e_smoke.py` in full. The happy-path test calls `/apollo/sessions/{id}/done` and asserts the response shape. We add:

1. `StudentProgress` to the in-memory tables list.
2. XP-field assertions on the Done response.

- [ ] **Step 2: Extend the in-memory table set**

In `apollo/tests/test_e2e_smoke.py`, find the `apollo_tables = [ ... ]` list (around line 24). It currently has four tables. Add `StudentProgress`:

```python
    apollo_tables = [
        ApolloSession.__table__,
        KGEntry.__table__,
        Message.__table__,
        ProblemAttempt.__table__,
        StudentProgress.__table__,
    ]
```

And update the import line near the top (currently at line 16):

```python
from apollo.persistence.models import (
    ApolloSession,
    KGEntry,
    Message,
    ProblemAttempt,
    StudentProgress,
)
```

- [ ] **Step 3: Add XP-field assertions to the happy-path Done response**

Locate the assertion block that checks the `/done` response. Add these assertions where the other `done_json[...]` assertions live (if the test doesn't already hold the Done response in a variable, introduce one — the shape of this file already captures the response; just grep for `done` POST).

Assertions to add:

```python
    # Phase 2: gamification fields present and sensible on first attempt.
    assert "xp_earned" in done_json
    assert isinstance(done_json["xp_earned"], int)
    assert done_json["xp_earned"] >= 0
    assert done_json["xp_earned"] <= 200
    assert done_json["level_before"] == 1
    assert isinstance(done_json["level_after"], int)
    assert done_json["level_up"] is False  # Can't level-up in one shot below 300 XP.
```

If the existing test doesn't bind the response JSON (it might assert inline), refactor minimally to introduce a `done_json = done_resp.json()` line before the new assertions. Keep every prior assertion intact.

- [ ] **Step 4: Run the smoke test**

Run: `pytest apollo/tests/test_e2e_smoke.py -v --tb=short`
Expected: PASS — existing assertions hold AND the new XP-field assertions pass.

- [ ] **Step 5: Run the full backend test suite once**

Run: `pytest apollo/ -v --tb=short`
Expected: all Apollo tests pass. (The non-Apollo test tree is out of scope for this plan.)

- [ ] **Step 6: Commit**

```bash
cd /Users/ishaanbatra/Documents/GitHub/ai-ta-backend
git add apollo/tests/test_e2e_smoke.py
git commit -m "$(cat <<'EOF'
test(apollo): e2e smoke asserts Phase 2 XP/level fields on Done

Adds StudentProgress to the in-memory table set and asserts that
the /done response carries xp_earned (0..200), level_before=1,
level_after, and level_up=False on a first attempt.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
git push origin ApolloV2
```

---

## Task 12: Apply migration 013 to the live Supabase

The plan so far has only touched code + in-memory SQLite. The production `apollo_student_progress` table doesn't exist until migration 013 is applied.

This task is a manual DBA-style step — ask the user to confirm before executing.

- [ ] **Step 1: Ask the user for the go-ahead**

Surface the intent to the user: the migration is the only step that touches prod data. Confirm they want it applied *now* (vs. batched with a later deploy).

- [ ] **Step 2: Apply the migration**

Two valid paths — pick whichever matches how migrations 009–012 were applied:

(a) **Via the Supabase SQL editor** (most likely): paste the contents of `database/migrations/013_apollo_student_progress.sql` into the editor and run it. The `BEGIN;` / `COMMIT;` envelope makes it atomic.

(b) **Via `psql`** against `SUPABASE_DB_URL` from `.env`:

```bash
psql "$SUPABASE_DB_URL" -f database/migrations/013_apollo_student_progress.sql
```

- [ ] **Step 3: Verify the table exists**

```sql
SELECT column_name, data_type, is_nullable, column_default
FROM information_schema.columns
WHERE table_name = 'apollo_student_progress'
ORDER BY ordinal_position;
```

Expected:

| column_name      | data_type                   | is_nullable | column_default |
|------------------|-----------------------------|-------------|----------------|
| student_id       | text                        | NO          | —              |
| xp_total         | integer                     | NO          | 0              |
| level            | integer                     | NO          | 1              |
| last_level_up_at | timestamp with time zone    | YES         | —              |

- [ ] **Step 4: (No commit — this is a runtime action, not a code change.)**

Note the migration application in the chat / deploy log. If Phase 1 used a `.planning/` state file for migrations, mirror its convention.

---

## Task 13: Manual browser verification

Spin up the dev environment and walk through the four gamification surfaces end-to-end. This is the only way to catch CSS layout regressions before users do.

- [ ] **Step 1: Start the backend**

```bash
cd /Users/ishaanbatra/Documents/GitHub/ai-ta-backend
python server.py
```

Expected: server listens on `:8000`. If it fails to boot, do NOT push — investigate first.

- [ ] **Step 2: Start the frontend**

```bash
cd /Users/ishaanbatra/Documents/GitHub/ai-ta-student-ui
npm run dev
```

Expected: dev server on `:3001`.

- [ ] **Step 3: Home-page chip verification**

- Sign in with any test student account.
- Confirm the XP chip appears in the header to the right of the class dropdown. On a fresh student it should read `L1 · 0 XP`.
- Hover the chip: tooltip shows `Level 1 · 0 XP`.
- If the chip is missing, check the browser devtools Network tab for `/api/apollo/progress/...`. If it 500s, the backend migration from Task 12 is likely not applied.

- [ ] **Step 4: Apollo-page greeting + avatar verification**

- From the home page, send any message and click "Teach Apollo what you just learned."
- On the Apollo page, confirm the greeting reads `Welcome back, Apollo Apprentice.` above the problem panel.
- The owl avatar in the chat should look visually like the L1 default (no hue shift).

- [ ] **Step 5: First-attempt Done verification**

- Teach Apollo through a full solve for the first problem (use the Phase 1 Bernoulli reference flow).
- Hit Done. On the report:
  - Rubric card shows (Phase 1 behavior).
  - `+N XP earned` line appears below the solver outcome with a plausible value (intro × overall_score, floor).
  - No level-up banner (student is still in level 1).
- Reload the home page. XP chip now reads `L1 · N XP` with the value matching.

- [ ] **Step 6: Re-attempt discount verification**

- On the report screen, click "Teach more and retry."
- Teach through again and hit Done a second time with the same score.
- Assert: the `+N XP earned` line is visibly smaller than the first pass (roughly ¼).

- [ ] **Step 7: Level-up verification**

- Use a SQL editor to seed the test account's XP to 290:

```sql
INSERT INTO apollo_student_progress (student_id, xp_total, level, last_level_up_at)
VALUES ('<YOUR_TEST_STUDENT_ID>', 290, 1, NULL)
ON CONFLICT (student_id) DO UPDATE SET xp_total = 290, level = 1;
```

- Reload the home page — chip reads `L1 · 290 XP`.
- Start an Apollo session and do a full Done pass on a problem whose attempt difficulty is `intro` (the default). A middling A-range score puts you over 300.
- Confirm the level-up banner renders with the slide-in + confetti pop animation.
- Back on the home page, the chip now reads `L2 · <new total> XP`. Reload the Apollo page — the greeting reads `Welcome back, Apollo Adept.` and the owl avatar has a subtle hue shift.

- [ ] **Step 8: Report findings**

If any step fails visually or behaviorally, STOP and file a note in the PR description. Do not paper over cosmetic regressions — the spec explicitly calls for "subtle celebration, confetti not fireworks." A broken level-up feels worse than no level-up.

---

## Task 14: Open the PR

- [ ] **Step 1: Confirm both branches are pushed**

```bash
cd /Users/ishaanbatra/Documents/GitHub/ai-ta-backend && git status && git log --oneline -15
cd /Users/ishaanbatra/Documents/GitHub/ai-ta-student-ui && git status && git log --oneline -15
```

Working tree clean. Both branches tip = ApolloV2.

- [ ] **Step 2: Open the backend PR**

```bash
cd /Users/ishaanbatra/Documents/GitHub/ai-ta-backend
gh pr create --base main --head ApolloV2 --title "Apollo Teaching Rigor Phase 2: gamification" --body "$(cat <<'EOF'
## Summary
- XP formula (`overall_score × difficulty_multiplier × reattempt_factor`, floored) and 5-tier level table land as a pure-function module.
- `apollo_student_progress` side table persists cumulative XP + level per student (migration 013).
- `handle_done` awards XP and returns `xp_earned`, `xp_before`, `xp_after`, `level_before`, `level_after`, `level_up` on every Done.
- New `GET /apollo/progress/{student_id}` endpoint surfaces current state without starting a session.
- Re-attempt detection covers both within-session retries (current ProblemAttempt row already graded) and cross-session repeats.

## Spec
See `docs/superpowers/specs/2026-04-21-apollo-teaching-rigor-design.md` §5 and `docs/superpowers/plans/2026-04-21-apollo-teaching-rigor-phase-2.md`.

## Reality notes
- Difficulty multipliers use the three DB values (intro 1.0 / standard 1.5 / hard 2.0), not the spec's four-tier naming.
- Axis weights remain as Phase 1 shipped (0.60 / 0.25 / 0.15).

## Test plan
- [ ] `pytest apollo/ -v --tb=short` green locally
- [ ] Migration 013 applied to Supabase
- [ ] Home-page XP chip, Apollo greeting, Done-screen `+N XP`, and level-up banner verified in-browser
- [ ] Re-attempt discount visually confirmed (second Done < first Done)

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 3: Open the frontend PR**

```bash
cd /Users/ishaanbatra/Documents/GitHub/ai-ta-student-ui
gh pr create --base main --head ApolloV2 --title "Apollo Teaching Rigor Phase 2 (UI): XP chip, level-up, greeting" --body "$(cat <<'EOF'
## Summary
- Extends `DoneResponse` with optional XP/level fields and adds `getStudentProgress` + a Next.js proxy route at `/api/apollo/progress/[student_id]`.
- `ApolloReportPanel` renders `+N XP earned` and a slide-in level-up banner with a subtle confetti pop.
- `ApolloPageClient` greets by level title and drives avatar hue/saturation accents via `data-apollo-level` on the page root.
- Home page fetches progress on auth-ready and renders an `LN · N XP` chip in the header.

## Spec
See `docs/superpowers/specs/2026-04-21-apollo-teaching-rigor-design.md` §5.

## Test plan
- [ ] `npm run lint` and `npm run build` green
- [ ] Signed-in student sees XP chip on home
- [ ] First Done shows `+N XP earned` under the rubric card
- [ ] Second Done on same problem earns ≈¼ of the first (re-attempt discount)
- [ ] Seeding a student to 290 XP and doing one full pass triggers the level-up banner and updates the greeting

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 4: Paste the two PR URLs into the conversation so the user can review.**

---

## Self-Review

Spec coverage check (against §5 of `2026-04-21-apollo-teaching-rigor-design.md`):

| Spec item | Task | Status |
|---|---|---|
| XP formula: `floor(overall_score × difficulty_multiplier)` | Task 2 | ✅ |
| Difficulty multipliers | Task 2 | ✅ (3-tier reconciliation noted) |
| Max XP per session = 200 | Task 2 | ✅ (`hard × 100 = 200`) |
| Re-attempt rule: 25% discount | Tasks 2, 3, 5 | ✅ |
| No negative XP | Task 2 | ✅ (`max(0, …)`) |
| 5-tier level table | Task 2 | ✅ |
| Student picks difficulty; no gating by level | — | Preserved: XP reads difficulty off the existing ProblemAttempt row, no gating added. |
| `apollo_student_progress` side table with given columns | Task 1 | ✅ |
| Upsert in `handle_done`; row replaces xp_total + level; level_up flag | Tasks 4, 5 | ✅ |
| Done response gains `xp_earned`, `level_before`, `level_after`, `level_up` | Task 5 | ✅ (plus `xp_before`/`xp_after` for the UI progress bar) |
| Student home: XP chip, current level title, avatar variant | Task 10 (chip) + Task 9 (level title + avatar accent) | ✅ |
| Apollo page greeting changes with level | Task 9 | ✅ |
| Done screen: `+78 XP earned` line under the rubric card, subtle level-up celebration | Task 8 | ✅ (CSS keyframes; no libs) |
| Phase boundary — nothing blocks Phase 1 from having landed | — | ✅: every new field is optional on the frontend type; the backend response adds fields without breaking existing Phase 1 keys. |

Placeholder scan: no "TBD", no "add appropriate error handling", no "similar to Task N." Every step has runnable code or a runnable command.

Type / signature consistency:
- `compute_xp_earned(overall_score: int, difficulty: str, is_reattempt: bool) -> int` — same name and positional-free signature used in Task 2 tests, Task 5 wiring.
- `apply_xp(db, student_id, xp_delta)` return dict shape is consistent between Task 4 tests, Task 5 wiring, and Task 5 response payload.
- `has_prior_graded_attempt(db, student_id, problem_id, exclude_attempt_id)` — same name and kwargs used in Tasks 3 and 5.
- `DoneResponse` optional fields (`xp_earned`, `xp_before`, `xp_after`, `level_before`, `level_after`, `level_up`) — same names used in lib/apollo/api.ts (Task 7) and ApolloReportPanel.tsx (Task 8).
- `StudentProgress` shape matches between backend handler (Task 6) and frontend type (Task 7).

No redundant work vs. Phase 1: the plan never re-touches rubric/coverage/diagnostic/parser. It *reads* `rubric.overall.score` and `ProblemAttempt.difficulty`, both already stable after Phase 1.
