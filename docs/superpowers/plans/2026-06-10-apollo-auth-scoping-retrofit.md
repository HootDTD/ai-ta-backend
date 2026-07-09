# Apollo Auth & Scoping Retrofit Implementation Plan (Learner-Model Phase 1)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the documented `/apollo/*` no-auth gap — every Apollo endpoint requires a Supabase bearer token, session endpoints verify ownership, session creation verifies course membership, and Apollo tables key off `user_id UUID` + `search_space_id` instead of unscoped `student_id TEXT`.

**Architecture:** New async auth dependencies in `apollo/auth_deps.py` reuse the existing `auth.py` machinery (`resolve_auth_context`, `has_membership`, auto-enroll). Migration 023 converts `apollo_sessions` and `apollo_student_progress` to the house identity pattern (mirrors `course_memberships`: `UUID(as_uuid=False)` + `search_space_id INTEGER FK`). The student UI already forwards `Authorization` through its proxy routes — it just needs to start sending the token and `search_space_id`.

**Tech Stack:** FastAPI + SQLAlchemy async (asyncpg/aiosqlite), Supabase Postgres + GoTrue, Next.js 15 App Router.

**Spec:** `docs/superpowers/specs/2026-06-10-apollo-kg-learner-model-architecture-decision.md` §8, §11 phase 1.

**Two repos are touched:** `ai-ta-backend` (Tasks 1–8) and `ai-ta-student-ui` (Task 9). Deploy together — the backend change breaks unauthenticated UI calls by design.

**Pre-existing facts you need (verified 2026-06-10):**
- `auth.py` exposes `AuthContext(user_id, access_token)`, `resolve_auth_context(request)` (sync, uses `requests`), `has_membership(db, *, user_id, search_space_id, role=None)` (async), `auto_enroll_student_membership(db, *, user_id, search_space_id)` (async). `server.py:316` has `_require_course_membership` — sync, server-route-only; do NOT import it into apollo (it uses `run_async`, which deadlocks inside async endpoints).
- The student UI already sends the Supabase user UUID as `student_id` (`app/page.tsx:588`: `session?.user_id`), so prod `student_id` values are castable to `uuid`. All `/api/apollo/*` proxy routes already forward the `Authorization` header.
- House identity pattern (`database/models.py:193`): `Column(UUID(as_uuid=False), ...)` from `sqlalchemy.dialects.postgresql`; works on SQLite tests but **binds must be valid UUID strings** — test fixtures must use UUID literals, not `"stu-1"`.
- Migrations are hand-numbered SQL in `database/migrations/`; latest is 022. Rehearse on the test Supabase project (`hjevtxdtrkxjcaaexdxt`) before prod (`uduxdniieeqbljtwocxy`). Prod application requires explicit user confirmation.
- Many apollo tests are module-level skip-marked legacy ("Tracked in claude_v3_checklist.md item 1") — do not unskip them; only fix fixtures in tests that currently run.
- XP decision: `apollo_student_progress` stays **global per student** (gamification is the student's own; no cross-student correlation), keyed by `user_id`. The classroom-isolation invariant applies to concepts/graphs/mastery, which arrive in later phases.

---

### Task 0: Branches

- [ ] **Step 1: Backend branch**

```bash
git -C ai-ta-backend checkout updated-schemas
git -C ai-ta-backend checkout -b feat/apollo-auth-scoping
```

- [ ] **Step 2: Student UI branch**

```bash
git -C ai-ta-student-ui checkout -b feat/apollo-auth-scoping
```

---

### Task 1: Migration 023 — `apollo_sessions` + `apollo_student_progress` re-keying

**Files:**
- Create: `database/migrations/023_apollo_auth_scoping.sql`

- [ ] **Step 1: Inspect prod row counts (read-only) so the destructive backfill is informed**

Run via the `supabase` MCP `execute_sql` (read-only query, prod):

```sql
SELECT (SELECT count(*) FROM apollo_sessions)          AS sessions,
       (SELECT count(*) FROM apollo_sessions
         WHERE student_id !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$') AS non_uuid_sessions,
       (SELECT count(*) FROM apollo_student_progress)  AS progress_rows;
```

Expected: single-digit counts (prod was down until recently). **If `sessions` > 50, STOP and ask the user before proceeding** — the DELETE steps below assume near-empty tables.

- [ ] **Step 2: Write the migration**

Create `database/migrations/023_apollo_auth_scoping.sql`:

```sql
-- 023_apollo_auth_scoping.sql
-- Learner-model Phase 1 (spec: 2026-06-10-apollo-kg-learner-model-architecture-decision.md §8).
-- Closes the security.md "Known gaps" item: apollo_* tables keyed by loose
-- student_id TEXT with no course scoping. Re-keys apollo_sessions by
-- (user_id UUID -> auth.users, search_space_id -> aita_search_spaces) and
-- apollo_student_progress by user_id UUID (XP stays global per student).
--
-- DESTRUCTIVE: rows whose student_id is not a real auth.users UUID, or whose
-- user has no course membership to attribute a search_space_id from, are
-- dev/test artifacts and are deleted. Verified near-empty in prod before
-- applying (plan Task 1 Step 1). Rehearse on the test project first.

BEGIN;

-- ---------------------------------------------------------------------------
-- apollo_sessions: identity + course scoping
-- ---------------------------------------------------------------------------
ALTER TABLE apollo_sessions
    ADD COLUMN IF NOT EXISTS user_id UUID,
    ADD COLUMN IF NOT EXISTS search_space_id INTEGER;

-- The student UI has always sent the Supabase auth user id as student_id.
UPDATE apollo_sessions
SET user_id = student_id::uuid
WHERE user_id IS NULL
  AND student_id ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$';

-- Best-effort course attribution from the user's membership (legacy rows
-- predate course scoping; pilot users belong to one course).
UPDATE apollo_sessions s
SET search_space_id = cm.search_space_id
FROM course_memberships cm
WHERE s.search_space_id IS NULL
  AND s.user_id IS NOT NULL
  AND cm.user_id = s.user_id;

DELETE FROM apollo_sessions WHERE user_id IS NULL OR search_space_id IS NULL;

ALTER TABLE apollo_sessions
    ALTER COLUMN user_id SET NOT NULL,
    ALTER COLUMN search_space_id SET NOT NULL,
    ADD CONSTRAINT apollo_sessions_user_id_fkey
        FOREIGN KEY (user_id) REFERENCES auth.users(id) ON DELETE CASCADE,
    ADD CONSTRAINT apollo_sessions_search_space_id_fkey
        FOREIGN KEY (search_space_id) REFERENCES aita_search_spaces(id) ON DELETE CASCADE;

CREATE INDEX IF NOT EXISTS ix_apollo_sessions_user_id
    ON apollo_sessions(user_id);
CREATE INDEX IF NOT EXISTS ix_apollo_sessions_search_space_id
    ON apollo_sessions(search_space_id);

-- One active session per student (same semantics as before, new key).
DROP INDEX IF EXISTS ix_apollo_sessions_unique_active_per_student;
CREATE UNIQUE INDEX ix_apollo_sessions_unique_active_per_user
    ON apollo_sessions(user_id) WHERE status = 'active';

DROP INDEX IF EXISTS ix_apollo_sessions_student;
ALTER TABLE apollo_sessions DROP COLUMN student_id;

-- ---------------------------------------------------------------------------
-- apollo_student_progress: re-key by auth UUID (XP stays global per student)
-- ---------------------------------------------------------------------------
ALTER TABLE apollo_student_progress
    ADD COLUMN IF NOT EXISTS user_id UUID;

UPDATE apollo_student_progress
SET user_id = student_id::uuid
WHERE user_id IS NULL
  AND student_id ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$';

DELETE FROM apollo_student_progress WHERE user_id IS NULL;

ALTER TABLE apollo_student_progress
    ALTER COLUMN user_id SET NOT NULL,
    DROP CONSTRAINT apollo_student_progress_pkey,
    ADD PRIMARY KEY (user_id),
    DROP COLUMN student_id;

COMMIT;
```

- [ ] **Step 3: Rehearse on the TEST Supabase project**

Apply via the `supabase-test` MCP `apply_migration` (name: `023_apollo_auth_scoping`). Note: the test project must already have migrations ≤022 applied; if `apollo_sessions` is missing there, apply the missing earlier migrations first (009, 013, 014, 016, 017, 018, 020, 021 contain the apollo tables).

- [ ] **Step 4: Verify the rehearsal**

Run on `supabase-test`:

```sql
SELECT column_name, data_type, is_nullable
FROM information_schema.columns
WHERE table_name = 'apollo_sessions' AND column_name IN ('user_id', 'search_space_id', 'student_id');
```

Expected: `user_id | uuid | NO`, `search_space_id | integer | NO`, and **no** `student_id` row.

- [ ] **Step 5: Commit (file only — prod application happens in Task 10 with user confirmation)**

```bash
git -C ai-ta-backend add database/migrations/023_apollo_auth_scoping.sql
git -C ai-ta-backend commit -m "feat: migration 023 — apollo_sessions/progress keyed by user_id UUID + search_space_id"
```

---

### Task 2: SQLAlchemy models

**Files:**
- Modify: `apollo/persistence/models.py` (lines 16–30 imports, 125–161 `ApolloSession`, 201–207 index, 210–216 `StudentProgress`)
- Test: `apollo/persistence/tests/test_models_auth_scoping.py` (create; create `apollo/persistence/tests/__init__.py` if missing)

- [ ] **Step 1: Write the failing test**

```python
"""Phase-1 auth retrofit: apollo models carry the house identity pattern."""
from __future__ import annotations

from apollo.persistence.models import ApolloSession, StudentProgress


def test_apollo_session_has_user_and_space_columns():
    cols = ApolloSession.__table__.columns
    assert "user_id" in cols and not cols["user_id"].nullable
    assert "search_space_id" in cols and not cols["search_space_id"].nullable
    assert "student_id" not in cols
    fks = {fk.target_fullname for fk in cols["search_space_id"].foreign_keys}
    assert "aita_search_spaces.id" in fks


def test_student_progress_keyed_by_user_id():
    cols = StudentProgress.__table__.columns
    assert cols["user_id"].primary_key
    assert "student_id" not in cols


def test_unique_active_index_uses_user_id():
    idx = {i.name: i for i in ApolloSession.__table__.indexes}
    active = idx["ix_apollo_sessions_unique_active_per_user"]
    assert active.unique
    assert [c.name for c in active.columns] == ["user_id"]
```

- [ ] **Step 2: Run it — expect FAIL**

```bash
cd ai-ta-backend && pytest apollo/persistence/tests/test_models_auth_scoping.py -v
```

Expected: 3 failures (`KeyError: 'user_id'` etc.).

- [ ] **Step 3: Implement the model changes**

In `apollo/persistence/models.py`:

(a) Add to the imports block:

```python
from sqlalchemy.dialects.postgresql import JSONB, UUID
```

(replacing the existing `from sqlalchemy.dialects.postgresql import JSONB` line).

(b) In `ApolloSession`, replace

```python
    student_id = Column(Text, nullable=False, index=True)
```

with

```python
    # Phase-1 auth retrofit: real Supabase identity + course scoping (the
    # classroom-isolation invariant). Mirrors course_memberships' types.
    user_id = Column(UUID(as_uuid=False), nullable=False, index=True)
    search_space_id = Column(
        Integer,
        ForeignKey("aita_search_spaces.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
```

(c) Replace the unique-active index block (lines 201–207) with:

```python
Index(
    "ix_apollo_sessions_unique_active_per_user",
    ApolloSession.user_id,
    unique=True,
    postgresql_where=(ApolloSession.status == "active"),
    sqlite_where=(ApolloSession.status == "active"),
)
```

(d) In `StudentProgress`, replace

```python
    student_id = Column(Text, primary_key=True)
```

with

```python
    user_id = Column(UUID(as_uuid=False), primary_key=True)
```

- [ ] **Step 4: Run the new test — expect PASS**

```bash
pytest apollo/persistence/tests/test_models_auth_scoping.py -v
```

- [ ] **Step 5: Commit**

```bash
git -C ai-ta-backend add apollo/persistence/models.py apollo/persistence/tests/
git -C ai-ta-backend commit -m "feat: apollo models keyed by user_id UUID + search_space_id"
```

---

### Task 3: Test-fixture sweep (every running test that constructs sessions/progress)

The model change breaks every fixture using `student_id=` or non-UUID ids. SQLite + `UUID(as_uuid=False)` requires valid UUID strings.

**Files (each currently running, from grep — skip-marked modules excluded):**
- Modify: `apollo/handlers/tests/test_chat.py:34`
- Modify: `apollo/handlers/tests/test_history.py:40`
- Modify: `apollo/handlers/tests/test_restart_problem.py:40`
- Modify: `apollo/handlers/tests/test_done_gate.py:129`
- Modify: `apollo/handlers/tests/test_olm_invite.py:99`
- Modify: `apollo/handlers/tests/test_done.py:43,183,342`
- Modify: `apollo/handlers/tests/test_lifecycle.py:34`
- Modify: `apollo/handlers/tests/test_next.py:41`
- Modify: `apollo/handlers/tests/test_negotiate.py:179,250`
- Modify: `apollo/handlers/tests/test_progress.py` (rewritten in Task 5)
- Modify: `apollo/knowledge_graph/tests/test_store.py:38,172`
- Modify: `apollo/knowledge_graph/tests/test_store_negotiation.py:257`
- Modify: `apollo/hoot_bridge/tests/test_session_init.py` (rewritten in Task 4)

- [ ] **Step 1: Define shared UUID constants**

Add to `apollo/conftest.py` (top level, after existing imports):

```python
# Phase-1 auth retrofit: SQLite + UUID(as_uuid=False) binds must be valid
# UUID strings. Tests use these constants instead of "stu-1"-style ids.
TEST_USER_ID = "00000000-0000-4000-8000-000000000001"
TEST_USER_ID_2 = "00000000-0000-4000-8000-000000000002"
TEST_SPACE_ID = 1
```

- [ ] **Step 2: Sweep each listed file**

In every listed file, change each `ApolloSession(...)` constructor from the pattern

```python
ApolloSession(student_id="stu-1", ...)
```

to

```python
ApolloSession(user_id=TEST_USER_ID, search_space_id=TEST_SPACE_ID, ...)
```

importing the constants: `from apollo.conftest import TEST_SPACE_ID, TEST_USER_ID`. Where a test uses a second student (`test_done.py:183` uses `student_id="stu-2"`), use `TEST_USER_ID_2`. Where a test reads `sess.student_id` (`test_done.py:330-342` adds `StudentProgress(student_id=sess.student_id, ...)`), change to `StudentProgress(user_id=sess.user_id, ...)`.

Note: SQLite fixtures that `create_all` only specific tables (e.g. `tables=[ApolloSession.__table__, ...]`) do not need `aita_search_spaces` created — SQLite doesn't enforce FKs by default and these tests never join through it.

- [ ] **Step 3: Run the apollo suite — only Task-4/5 modules may still fail**

```bash
pytest apollo/ -v --tb=short 2>&1 | tail -30
```

Expected: everything green except `apollo/hoot_bridge/tests/test_session_init.py` and `apollo/handlers/tests/test_progress.py` (their *signatures* change in Tasks 4–5; if they pass untouched here, fine). The repo-wide `tests/` suite is checked in Task 8.

- [ ] **Step 4: Commit**

```bash
git -C ai-ta-backend add apollo/
git -C ai-ta-backend commit -m "test: apollo fixtures use user_id UUID + search_space_id"
```

---

### Task 4: `init_session_from_hoot` takes (user_id, search_space_id)

**Files:**
- Modify: `apollo/hoot_bridge/session_init.py`
- Test: `apollo/hoot_bridge/tests/test_session_init.py`

- [ ] **Step 1: Update the tests first (failing)**

In `apollo/hoot_bridge/tests/test_session_init.py`, change every call

```python
await init_session_from_hoot(db=db, student_id="stu-1", hoot_transcript=..., difficulty=...)
```

to

```python
await init_session_from_hoot(
    db=db, user_id=TEST_USER_ID, search_space_id=TEST_SPACE_ID,
    hoot_transcript=..., difficulty=...,
)
```

(import the constants from `apollo.conftest`). Where the test asserts on the created row, assert `session.user_id == TEST_USER_ID` and `session.search_space_id == TEST_SPACE_ID`. Add one new test:

```python
@pytest.mark.asyncio
async def test_new_session_ends_only_this_users_active_session(db, monkeypatch):
    _mock_inference_and_selection(monkeypatch)  # reuse the module's existing mock helper
    first = await init_session_from_hoot(
        db=db, user_id=TEST_USER_ID, search_space_id=TEST_SPACE_ID,
        hoot_transcript="bernoulli", difficulty="intro",
    )
    other = await init_session_from_hoot(
        db=db, user_id=TEST_USER_ID_2, search_space_id=TEST_SPACE_ID,
        hoot_transcript="bernoulli", difficulty="intro",
    )
    # A second session for user 1 must end user 1's first session, not user 2's.
    await init_session_from_hoot(
        db=db, user_id=TEST_USER_ID, search_space_id=TEST_SPACE_ID,
        hoot_transcript="bernoulli", difficulty="intro",
    )
    s1 = await db.get(ApolloSession, first["session_id"])
    s2 = await db.get(ApolloSession, other["session_id"])
    assert s1.status == "ended"
    assert s2.status == "active"
```

(If the module has no reusable mock helper, copy the OpenAI mocking pattern already present in its existing tests into a small `_mock_inference_and_selection(monkeypatch)` function rather than duplicating it per test.)

- [ ] **Step 2: Run — expect FAIL** (`TypeError: unexpected keyword argument 'user_id'`)

```bash
pytest apollo/hoot_bridge/tests/test_session_init.py -v
```

- [ ] **Step 3: Implement**

In `apollo/hoot_bridge/session_init.py`, change the signature and the two usages:

```python
async def init_session_from_hoot(
    *,
    db: AsyncSession,
    user_id: str,
    search_space_id: int,
    hoot_transcript: str,
    difficulty: str,
) -> Dict[str, Any]:
```

stale-session update:

```python
    await db.execute(
        update(ApolloSession)
        .where(
            ApolloSession.user_id == user_id,
            ApolloSession.status == SessionStatus.active.value,
        )
        .values(status=SessionStatus.ended.value)
    )
```

session construction:

```python
    session = ApolloSession(
        user_id=user_id,
        search_space_id=search_space_id,
        concept_cluster_id=cluster_id,
        status=SessionStatus.active.value,
        phase=SessionPhase.TEACHING.value,
        current_problem_id=problem.id,
    )
```

- [ ] **Step 4: Run — expect PASS**

```bash
pytest apollo/hoot_bridge/tests/test_session_init.py -v
```

- [ ] **Step 5: Commit**

```bash
git -C ai-ta-backend add apollo/hoot_bridge/
git -C ai-ta-backend commit -m "feat: session init takes authenticated user_id + search_space_id"
```

---

### Task 5: Rename the internal `student_id` plumbing (progress, XP, attempt history, lifecycle)

**Files:**
- Modify: `apollo/persistence/progress_repo.py:26-48`
- Modify: `apollo/persistence/attempt_history.py:23-33`
- Modify: `apollo/handlers/progress.py`
- Modify: `apollo/handlers/done.py:258,282`
- Modify: `apollo/handlers/lifecycle.py:103`
- Test: `apollo/handlers/tests/test_progress.py`, `apollo/handlers/tests/test_done.py`, `apollo/handlers/tests/test_lifecycle.py`

- [ ] **Step 1: Update `test_progress.py` first (failing)**

Rewrite the three tests to the new keyword and response key (fixture unchanged apart from imports):

```python
from apollo.conftest import TEST_USER_ID

@pytest.mark.asyncio
async def test_get_progress_unknown_student_returns_defaults(db):
    payload = await handle_get_progress(db=db, user_id=TEST_USER_ID)
    assert payload == {
        "user_id": TEST_USER_ID,
        "xp_total": 0,
        "level": 1,
        "title": "Apollo Apprentice",
        "next_tier_threshold": 300,
    }
```

and analogously for the "known student" (insert `StudentProgress(user_id=TEST_USER_ID, xp_total=1800, level=4)`) and "max level" tests.

- [ ] **Step 2: Run — expect FAIL**

```bash
pytest apollo/handlers/tests/test_progress.py -v
```

- [ ] **Step 3: Implement the renames**

- `apollo/persistence/progress_repo.py`: rename the `student_id` keyword to `user_id` in `load_progress` and `apply_xp`; the row constructor becomes `StudentProgress(user_id=user_id, xp_total=0, level=1)`; the `where` becomes `StudentProgress.user_id == user_id`.
- `apollo/persistence/attempt_history.py`: rename the `student_id` parameter to `user_id`; the filter becomes `ApolloSession.user_id == user_id`.
- `apollo/handlers/progress.py`: rename the parameter (`user_id: str`), pass it through, and return `"user_id": row.user_id` instead of `"student_id"`.
- `apollo/handlers/done.py:258` (`has_prior_graded_attempt`) and `:282` (`apply_xp`): pass `user_id=sess.user_id`.
- `apollo/handlers/lifecycle.py:103`: the session payload key becomes `"user_id": sess.user_id` (drop `student_id`); also add `"search_space_id": sess.search_space_id` on the next line — the UI needs it for course context.

- [ ] **Step 4: Run the three affected modules — expect PASS**

```bash
pytest apollo/handlers/tests/test_progress.py apollo/handlers/tests/test_done.py apollo/handlers/tests/test_lifecycle.py -v
```

(Fix any assertion in `test_lifecycle.py` that checked the old `student_id` response key.)

- [ ] **Step 5: Commit**

```bash
git -C ai-ta-backend add apollo/
git -C ai-ta-backend commit -m "refactor: user_id replaces student_id across apollo progress/XP/lifecycle"
```

---

### Task 6: Auth dependencies — `apollo/auth_deps.py`

**Files:**
- Create: `apollo/auth_deps.py`
- Test: `apollo/tests/test_auth_deps.py`

- [ ] **Step 1: Write the failing tests**

Create `apollo/tests/test_auth_deps.py`:

```python
"""Unit tests for the Apollo auth dependencies (Phase-1 retrofit).

resolve_auth_context is monkeypatched — these tests cover the ownership and
membership logic, not GoTrue token validation (auth.py owns that).
"""
from __future__ import annotations

import pytest
import pytest_asyncio
from fastapi import HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import apollo.auth_deps as deps
from apollo.conftest import TEST_SPACE_ID, TEST_USER_ID, TEST_USER_ID_2
from apollo.persistence.models import ApolloSession
from auth import AuthContext
from database.models import Base


def _fake_request() -> Request:
    return Request(scope={"type": "http", "headers": []})


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(
            lambda sc: Base.metadata.create_all(sc, tables=[ApolloSession.__table__])
        )
    Session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with Session() as s:
        yield s
    await engine.dispose()


@pytest.fixture
def as_user(monkeypatch):
    def _set(user_id: str):
        monkeypatch.setattr(
            deps, "resolve_auth_context",
            lambda _request: AuthContext(user_id=user_id, access_token="tok"),
        )
    return _set


async def _make_session(db: AsyncSession, user_id: str) -> int:
    row = ApolloSession(
        user_id=user_id, search_space_id=TEST_SPACE_ID,
        status="active", phase="TEACHING",
    )
    db.add(row)
    await db.commit()
    return row.id


@pytest.mark.asyncio
async def test_owner_passes(db, as_user):
    as_user(TEST_USER_ID)
    sid = await _make_session(db, TEST_USER_ID)
    auth = await deps.require_session_owner(sid, _fake_request(), db)
    assert auth.user_id == TEST_USER_ID


@pytest.mark.asyncio
async def test_non_owner_gets_403(db, as_user):
    as_user(TEST_USER_ID_2)
    sid = await _make_session(db, TEST_USER_ID)
    with pytest.raises(HTTPException) as exc:
        await deps.require_session_owner(sid, _fake_request(), db)
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_missing_session_gets_404(db, as_user):
    as_user(TEST_USER_ID)
    with pytest.raises(HTTPException) as exc:
        await deps.require_session_owner(999999, _fake_request(), db)
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_member_check_403_without_membership(db, as_user, monkeypatch):
    async def _no(*args, **kwargs):
        return False
    monkeypatch.setattr(deps, "has_membership", _no)
    monkeypatch.setattr(deps, "auto_enroll_student_membership", _no)
    with pytest.raises(HTTPException) as exc:
        await deps.require_course_member(
            db=db,
            auth=AuthContext(user_id=TEST_USER_ID, access_token="tok"),
            search_space_id=TEST_SPACE_ID,
        )
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_member_check_passes_with_membership(db, monkeypatch):
    async def _yes(*args, **kwargs):
        return True
    monkeypatch.setattr(deps, "has_membership", _yes)
    await deps.require_course_member(
        db=db,
        auth=AuthContext(user_id=TEST_USER_ID, access_token="tok"),
        search_space_id=TEST_SPACE_ID,
    )  # no raise
```

- [ ] **Step 2: Run — expect FAIL** (`ModuleNotFoundError: apollo.auth_deps`)

```bash
pytest apollo/tests/test_auth_deps.py -v
```

- [ ] **Step 3: Implement `apollo/auth_deps.py`**

```python
"""FastAPI auth dependencies for the /apollo router (Phase-1 retrofit).

Closes the security.md "Known gaps" item: /apollo/* previously took identity
from the request body with no token validation. Every endpoint now requires a
Supabase bearer token; session-scoped endpoints verify the caller owns the
session; session creation verifies course membership.

Deliberately NOT reusing server.py's _require_course_membership — that helper
is sync and drives its own event loop via run_async, which deadlocks inside
async endpoints. These dependencies are natively async and reuse the same
auth.py primitives.
"""
from __future__ import annotations

import asyncio

from fastapi import Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apollo.persistence.models import ApolloSession
from auth import (
    AuthContext,
    auto_enroll_student_membership,
    has_membership,
    resolve_auth_context,
)
from database.session import get_db_session


async def require_user(request: Request) -> AuthContext:
    """Resolve the bearer token to an AuthContext (401 on failure).

    resolve_auth_context uses the blocking `requests` client (with its own
    TTL cache), so it runs off the event loop.
    """
    try:
        return await asyncio.to_thread(resolve_auth_context, request)
    except HTTPException:
        raise
    except RuntimeError as exc:  # missing SUPABASE_* config
        raise HTTPException(
            status_code=500, detail="Server auth configuration error"
        ) from exc


async def require_course_member(
    *,
    db: AsyncSession,
    auth: AuthContext,
    search_space_id: int,
) -> None:
    """403 unless the user belongs to the course.

    Mirrors server.py semantics: authenticated users may auto-enroll as
    students where the env allows it (AUTO_ENROLL_STUDENT_MEMBERSHIP).
    """
    if await has_membership(db, user_id=auth.user_id, search_space_id=search_space_id):
        return
    enrolled = await auto_enroll_student_membership(
        db, user_id=auth.user_id, search_space_id=search_space_id
    )
    if enrolled and await has_membership(
        db, user_id=auth.user_id, search_space_id=search_space_id
    ):
        return
    raise HTTPException(status_code=403, detail="Forbidden for this course")


async def require_session_owner(
    session_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db_session),
) -> AuthContext:
    """401/403/404 gate for /apollo/sessions/{session_id}/* endpoints.

    FastAPI resolves session_id from the path. Returns the AuthContext so
    handlers can use the validated identity.
    """
    auth = await require_user(request)
    row = (
        (await db.execute(select(ApolloSession).where(ApolloSession.id == session_id)))
        .scalars()
        .first()
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Session not found")
    if str(row.user_id) != str(auth.user_id):
        raise HTTPException(status_code=403, detail="Not your session")
    return auth
```

- [ ] **Step 4: Run — expect PASS**

```bash
pytest apollo/tests/test_auth_deps.py -v
```

- [ ] **Step 5: Commit**

```bash
git -C ai-ta-backend add apollo/auth_deps.py apollo/tests/test_auth_deps.py
git -C ai-ta-backend commit -m "feat: async auth dependencies for the apollo router"
```

---

### Task 7: Wire auth into `apollo/api.py`

**Files:**
- Modify: `apollo/api.py`
- Test: `apollo/tests/test_api_auth.py` (create)

- [ ] **Step 1: Write the failing router-level test**

Create `apollo/tests/test_api_auth.py`:

```python
"""Router-level auth wiring: no token -> 401; owner token -> handler runs.

Uses TestClient against a minimal app with the real router. The DB and
resolve_auth_context are overridden; handlers behind the gates are not
exercised deeply here (their own tests cover behavior).
"""
from __future__ import annotations

import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import apollo.auth_deps as deps
from apollo.api import register_exception_handlers, router as apollo_router
from apollo.conftest import TEST_SPACE_ID, TEST_USER_ID
from apollo.persistence.models import ApolloSession, StudentProgress
from database.models import Base
from database.session import get_db_session


@pytest.fixture
def client_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")

    async def _bootstrap():
        async with engine.begin() as conn:
            await conn.run_sync(
                lambda sc: Base.metadata.create_all(
                    sc, tables=[ApolloSession.__table__, StudentProgress.__table__]
                )
            )

    import asyncio
    asyncio.run(_bootstrap())

    Session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

    async def _override_db():
        async with Session() as s:
            yield s

    app = FastAPI()
    app.include_router(apollo_router)
    register_exception_handlers(app)
    app.dependency_overrides[get_db_session] = _override_db
    return app, Session


def test_progress_requires_token(client_factory):
    app, _ = client_factory
    r = TestClient(app).get("/apollo/progress")
    assert r.status_code == 401


def test_progress_with_token_returns_defaults(client_factory, monkeypatch):
    from auth import AuthContext
    app, _ = client_factory
    monkeypatch.setattr(
        deps, "resolve_auth_context",
        lambda _request: AuthContext(user_id=TEST_USER_ID, access_token="tok"),
    )
    r = TestClient(app).get(
        "/apollo/progress", headers={"Authorization": "Bearer tok"}
    )
    assert r.status_code == 200
    assert r.json()["user_id"] == TEST_USER_ID


def test_session_endpoint_403_for_non_owner(client_factory, monkeypatch):
    import asyncio
    from auth import AuthContext

    app, Session = client_factory

    async def _seed() -> int:
        async with Session() as s:
            row = ApolloSession(
                user_id="00000000-0000-4000-8000-0000000000ff",
                search_space_id=TEST_SPACE_ID,
                status="active",
                phase="TEACHING",
            )
            s.add(row)
            await s.commit()
            return row.id

    sid = asyncio.run(_seed())
    monkeypatch.setattr(
        deps, "resolve_auth_context",
        lambda _request: AuthContext(user_id=TEST_USER_ID, access_token="tok"),
    )
    r = TestClient(app).get(
        f"/apollo/sessions/{sid}", headers={"Authorization": "Bearer tok"}
    )
    assert r.status_code == 403
```

- [ ] **Step 2: Run — expect FAIL** (today `/apollo/progress` is `/apollo/progress/{student_id}` and nothing returns 401)

```bash
pytest apollo/tests/test_api_auth.py -v
```

- [ ] **Step 3: Wire the router**

In `apollo/api.py`:

(a) Add imports:

```python
from apollo.auth_deps import require_course_member, require_session_owner, require_user
from auth import AuthContext
```

(b) Replace `FromHootRequest` (identity now comes from the token; course from the body):

```python
class FromHootRequest(BaseModel):
    search_space_id: int
    hoot_transcript: str
    difficulty: Literal["intro", "standard", "hard"] = "intro"
```

(c) Replace the `from_hoot` endpoint:

```python
@router.post("/sessions/from_hoot")
async def session_from_hoot(
    body: FromHootRequest,
    request: Request,
    db: AsyncSession = Depends(get_db_session),
) -> dict:
    auth = await require_user(request)
    await require_course_member(db=db, auth=auth, search_space_id=body.search_space_id)
    return await init_session_from_hoot(
        db=db,
        user_id=auth.user_id,
        search_space_id=body.search_space_id,
        hoot_transcript=body.hoot_transcript,
        difficulty=body.difficulty,
    )
```

(d) Every session-scoped route gains the ownership dependency. The pattern, shown on `get_session` — apply identically to `chat`, `done`, `retry`, `next_problem`, `restart_problem`, `end`, `negotiate_challenge`, `negotiate_paraphrase`, `negotiate_skip`, `negotiate_trace`:

```python
@router.get("/sessions/{session_id}")
async def get_session(
    session_id: int,
    db: AsyncSession = Depends(get_db_session),
    neo: Neo4jClient = Depends(get_neo4j_client),
    auth: AuthContext = Depends(require_session_owner),
) -> dict:
    return await handle_get_session(db=db, neo=neo, session_id=session_id)
```

(handlers keep their signatures — the dependency is a pure gate; `session_id` reaches it via the path).

(e) Replace the progress endpoint (identity from the token, never from the path):

```python
@router.get("/progress")
async def progress(
    request: Request,
    db: AsyncSession = Depends(get_db_session),
) -> dict:
    auth = await require_user(request)
    return await handle_get_progress(db=db, user_id=auth.user_id)
```

- [ ] **Step 4: Run — expect PASS**

```bash
pytest apollo/tests/test_api_auth.py apollo/tests/test_auth_deps.py -v
```

- [ ] **Step 5: Commit**

```bash
git -C ai-ta-backend add apollo/api.py apollo/tests/test_api_auth.py
git -C ai-ta-backend commit -m "feat: every /apollo endpoint requires auth; ownership + membership gates"
```

---

### Task 8: Full backend suite + docs reconciliation (drift contract)

**Files:**
- Modify: `docs/architecture/apollo.md` (routes table + public interfaces + conventions)
- Modify: `docs/shared-architecture/security.md` (Known gaps + checklist)

- [ ] **Step 1: Run the whole backend suite**

```bash
cd ai-ta-backend && pytest tests/ apollo/ -v --tb=short 2>&1 | tail -40
```

Expected: green (server-level integration tests don't touch apollo routes unauthenticated; if any do, update them with the Task-7 override pattern).

- [ ] **Step 2: Update `docs/architecture/apollo.md`**

- In the routes table: `POST /apollo/sessions/from_hoot` body becomes `{search_space_id, hoot_transcript, difficulty="intro"}`, note "identity from bearer token; membership checked"; `GET /apollo/progress/{student_id}` becomes `GET /apollo/progress` ("token-derived identity"); add to each session-scoped row "owner-gated (`require_session_owner`)".
- Module map: add `auth_deps.py` to the `apollo/` (root) row.
- Key dependencies: add a line that all routes require Supabase bearer auth via `auth.py` primitives.
- `apollo_sessions`/`apollo_student_progress` mentions: `user_id UUID` + `search_space_id` (migration 023), partial unique index renamed.
- Bump `last_verified: 2026-06-10` (or today's date at execution).

- [ ] **Step 3: Update `docs/shared-architecture/security.md`**

- Remove the "Known gaps" bullet about `/apollo/*` no-auth (lines ~85-87) and replace with a short paragraph: `/apollo/*` is authenticated as of migration 023 / Phase 1 — token via `resolve_auth_context`, ownership via `apollo/auth_deps.require_session_owner`, membership at session creation; identity never comes from the request body.
- Update the checklist line (~119) "If touching `/apollo/*`, note the no-auth gap" to "If touching `/apollo/*`, preserve the auth_deps gates — identity comes from the token only."

- [ ] **Step 4: Commit**

```bash
git -C ai-ta-backend add docs/
git -C ai-ta-backend commit -m "docs: apollo.md + security.md reflect authenticated /apollo surface"
```

---

### Task 9: Student UI — send the token, course id, and use the new progress route

**Files (repo `ai-ta-student-ui`):**
- Modify: `lib/apollo/api.ts`
- Modify: `app/page.tsx:583-595` (`startApollo`)
- Modify: `app/apollo/ApolloPageClient.tsx:47-69`
- Create: `app/api/apollo/progress/route.ts`
- Delete: `app/api/apollo/progress/[student_id]/route.ts`
- Modify: `docs/architecture/pages.md` (proxy route list) + `docs/architecture/components.md` if it documents the apollo api client

There is no test runner configured in this repo (verify with `npm run` — if a `test` script exists, mirror these changes with tests); verification is `npx tsc --noEmit` + the Task-10 manual e2e.

- [ ] **Step 1: Auth headers in `lib/apollo/api.ts`**

At the top, import the existing helpers and add a private header builder:

```typescript
import { authHeaders, loadStoredSession } from "@/app/lib/auth";

function apolloHeaders(json: boolean = false): HeadersInit {
  const session = loadStoredSession();
  return authHeaders(session?.access_token, json);
}
```

(`authHeaders(token, includeJsonContentType)` already exists in `app/lib/auth.ts:165`. If the `@/app/lib/auth` alias doesn't resolve, use the relative import `../../app/lib/auth`.)

Then update every fetch:
- JSON POSTs (`startSessionFromHoot`, `sendChat`, `challengeEntry`, `paraphraseEntry`, `skipEntry`): `headers: apolloHeaders(true)` (replacing the literal `{ "content-type": "application/json" }`).
- Body-less requests (`getSessionState`, `finishTeaching`, `retryProblem`, `endSession`, `getEntryTrace`, `getStudentProgress`): add `headers: apolloHeaders()`.

- [ ] **Step 2: New signatures in `lib/apollo/api.ts`**

```typescript
export async function startSessionFromHoot(
  searchSpaceId: number,
  hootTranscript: string,
): Promise<{ session_id: number; problem: ApolloProblem }> {
  const res = await fetch("/api/apollo/sessions/from_hoot", {
    method: "POST",
    headers: apolloHeaders(true),
    body: JSON.stringify({ search_space_id: searchSpaceId, hoot_transcript: hootTranscript }),
  });
  return (await _handle(res)) as { session_id: number; problem: ApolloProblem };
}

export async function getStudentProgress(): Promise<StudentProgress> {
  const res = await fetch("/api/apollo/progress", { headers: apolloHeaders() });
  return (await _handle(res)) as StudentProgress;
}
```

Type updates in the same file: `ApolloSessionState.student_id` becomes `user_id: string` plus `search_space_id: number`; `StudentProgress.student_id` becomes `user_id: string`.

- [ ] **Step 3: Call sites**

`app/page.tsx` `startApollo` (line ~588): replace

```typescript
const studentId = session?.user_id ?? 'unknown';
const { session_id } = await startSessionFromHoot(studentId, transcript);
```

with

```typescript
if (!selectedClassId) {
  setApolloError('Pick a class before starting Apollo.');
  return;
}
const { session_id } = await startSessionFromHoot(selectedClassId, transcript);
```

(`selectedClassId` is the page's existing course state, already used by `/api/chats?search_space_id=` at line 411 — confirm the exact state variable name in the file and use it.)

`app/apollo/ApolloPageClient.tsx`: both progress calls drop their argument — `getStudentProgress().then(setProgress)` (lines 55 and 66); the `state.student_id` reads disappear with them.

- [ ] **Step 4: Proxy route swap**

Create `app/api/apollo/progress/route.ts`:

```typescript
export const runtime = 'nodejs';

export async function GET(req: Request) {
  const rawBackend = process.env.AI_TA_API_BASE_URL;
  const backend = rawBackend ? rawBackend.replace(/\/+$/, '') : '';
  if (!backend) {
    return new Response('AI_TA_API_BASE_URL missing', { status: 500 });
  }

  const authHeader = req.headers.get('authorization');
  const headers: Record<string, string> = {};
  if (authHeader) headers.Authorization = authHeader;
  const resp = await fetch(`${backend}/apollo/progress`, { headers });

  return new Response(resp.body, {
    status: resp.status,
    headers: {
      'Content-Type': resp.headers.get('content-type') ?? 'application/json',
      'Cache-Control': 'no-store',
    },
  });
}
```

Delete the old dynamic route:

```bash
git -C ai-ta-student-ui rm -r "app/api/apollo/progress/[student_id]"
```

- [ ] **Step 5: Type-check**

```bash
cd ai-ta-student-ui && npx tsc --noEmit
```

Expected: clean. Any remaining `student_id` usages it flags (e.g. in `ApolloProgressCard` props) get the same `user_id` rename.

- [ ] **Step 6: Update UI owner docs + commit**

Update `docs/architecture/pages.md` (the `/api/apollo` proxy list: `progress/[student_id]` → `progress`; from_hoot body change) and bump `last_verified`.

```bash
git -C ai-ta-student-ui add -A
git -C ai-ta-student-ui commit -m "feat: apollo client sends bearer auth + search_space_id; token-derived progress"
```

---

### Task 10: End-to-end verification + prod migration

- [ ] **Step 1: Apply migration 023 to PROD Supabase — requires explicit user confirmation**

Present the Task-1 Step-1 prod row counts to the user and get a yes before running `apply_migration` (name `023_apollo_auth_scoping`) on the **prod** `supabase` MCP server. Do not proceed without confirmation.

- [ ] **Step 2: Boot the stack locally**

```bash
# terminal 1
cd ai-ta-backend && python server.py        # :8000
# terminal 2
cd ai-ta-student-ui && npm run dev          # :3001
```

- [ ] **Step 3: Manual e2e checks**

1. Signed out, `curl http://localhost:8000/apollo/progress` → `401`.
2. Sign in on :3001, pick a class, chat once, click "Teach Apollo" → session starts (network tab: `from_hoot` request has `Authorization` header and `search_space_id` in the body).
3. Teach a turn, click Done → report renders; progress card shows XP (the `/api/apollo/progress` call succeeds).
4. From a second account (not enrolled or different user), `curl -H "Authorization: Bearer <user2 token>" http://localhost:8000/apollo/sessions/<user1 session id>` → `403`.

- [ ] **Step 4: Merge/PR per house git flow**

Push both branches with `-u`; open PRs (backend targets its working base branch; UI likewise). PR bodies list the breaking API changes: `from_hoot` body shape, `GET /apollo/progress`, and the `user_id` response keys.

---

## Self-review notes (kept honest)

- **Spec coverage:** §8 scoping rules → Tasks 6–7 (ownership + membership, identity never from body); §8 table fixes → Tasks 1–2; spec's "student UI updated to authenticated calls" → Task 9; security.md reconciliation → Task 8. Neo4j property scoping (`user_id`/`search_space_id` on Layer-2 nodes) is **Phase 3** scope per the spec, not this plan.
- **Known seam:** `handle_get_session`'s response key change (`student_id` → `user_id`) is consumed only by the Apollo page (`ApolloSessionState`); Task 9 updates the type and the two reads.
- **Legacy skip-marked tests** (`test_e2e_smoke.py`, `test_e2e_olm_smoke.py`, etc.) still reference `student_id=` in dead code — leave them; they're rewritten wholesale in the tracked test-rewrite phase.
