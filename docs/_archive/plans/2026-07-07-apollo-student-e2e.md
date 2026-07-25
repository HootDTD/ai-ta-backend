# Apollo Student-Facing E2E Baseline — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Students on staging can enter Apollo without a Hoot conversation — pick concept, difficulty, and problem; teach; resolve the done-gate review; get graded; continue to a next problem; and see a progress dashboard — per the spec at `docs/superpowers/specs/2026-07-07-apollo-student-e2e-design.md`.

**Architecture:** Backend (ai-ta-backend) adds two read endpoints (`GET /apollo/concepts`, `GET /apollo/problems`), one create endpoint (`POST /apollo/sessions` — `init_session_from_hoot` minus transcript inference), and a `detail` block on `GET /apollo/progress`. Student UI (ai-ta-student-ui) adds a browse screen, wires the dormant KG-negotiation pills and `DoneGateModal`, adds Next/Restart controls, and a progress dashboard page. No grading, chat, or retrieval logic changes.

**Tech Stack:** FastAPI + SQLAlchemy async (backend), pytest with in-memory SQLite `TestClient` harness; Next.js 15 App Router + TypeScript (UI), no test framework (verify via `npm run lint` + `npm run build`).

## Global Constraints

- Two repos: backend `/Users/ishaanbatra/Documents/GitHub/ai-ta-backend` (branch `weekend/apollo-student-e2e`, PR base `staging`); UI `/Users/ishaanbatra/Documents/GitHub/ai-ta-student-ui` (create branch `apollo-e2e-baseline` off `main`, PR base `main`). Never merge PRs — open them, report URL + CI status, stop.
- Never expose `reference_solution` (or any `Problem` field beyond `id`, `difficulty`, `problem_text`) in browse responses. Session responses keep their existing shape.
- Difficulty literals everywhere: `"intro" | "standard" | "hard"`.
- No new packages in either repo. No new test framework in the UI repo.
- Backend drift contract: every backend task's commit also updates `docs/architecture/apollo.md` (endpoint list / behavior) and bumps its `last_verified` date to 2026-07-07.
- Backend tests: run `pytest apollo/ -v --tb=short` (SQLite tests need no Docker). UI verification: `npm run lint && npm run build` from the UI repo root.
- Env flags are NOT set in code or .env files — staging flags (`APOLLO_DONE_GATE_ENABLED=1`, `APOLLO_GRADING_ARTIFACT_ENABLED=1`) are set on Railway in the ops phase (Task 12).
- The UI repo has no `typecheck` script; `next build` is the type gate.

---

# Part A — Backend (ai-ta-backend)

### Task 1: `GET /apollo/concepts`

**Files:**
- Modify: `apollo/api.py` (imports + new endpoint after the `progress` endpoint, ~line 214)
- Test: `apollo/tests/test_api_browse.py` (new)
- Modify: `docs/architecture/apollo.md` (add endpoint row, bump `last_verified`)

**Interfaces:**
- Consumes: `list_course_concepts(db, *, search_space_id) -> list[ConceptRow]` from `apollo/subjects/curriculum_db.py` (ConceptRow: `concept_id: int, slug: str, display_name: str`; only concepts with tier-2 non-quarantined problems).
- Produces: `GET /apollo/concepts?search_space_id=N` → `{"concepts": [{"concept_id": int, "slug": str, "display_name": str}]}`. Auth: bearer token + course membership (auto-enroll like from_hoot). Tasks 5–7 (UI) rely on this exact shape.

- [ ] **Step 1: Write the failing test**

Create `apollo/tests/test_api_browse.py`. The harness is the same pattern as `apollo/tests/test_api_auth.py:41-71` (in-memory SQLite + real router + overridden deps), with the curriculum tables added:

```python
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


@pytest.fixture
def client_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")

    async def _bootstrap():
        async with engine.begin() as conn:
            await conn.run_sync(lambda sc: Base.metadata.create_all(sc, tables=TABLES))

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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest apollo/tests/test_api_browse.py -v --tb=short`
Expected: both tests FAIL with 404 (route `/apollo/concepts` does not exist yet — FastAPI returns `{"detail": "Not Found"}`).

- [ ] **Step 3: Implement the endpoint**

In `apollo/api.py`, add to the imports block (after the `from apollo.provisioning.authored_sets.api import ...` line):

```python
from apollo.subjects.curriculum_db import list_course_concepts
```

Add the endpoint immediately after the `GET /progress` endpoint (`apollo/api.py:207-213`):

```python
@router.get("/concepts")
async def list_concepts(
    search_space_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db_session),
) -> dict:
    """Student browse surface: the course's teachable concepts (tier-2,
    non-quarantined problem pool — same predicate as session entry)."""
    auth = await require_user(request)
    await require_course_member(db=db, auth=auth, search_space_id=search_space_id)
    rows = await list_course_concepts(db, search_space_id=search_space_id)
    return {
        "concepts": [
            {"concept_id": r.concept_id, "slug": r.slug, "display_name": r.display_name}
            for r in rows
        ]
    }
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest apollo/tests/test_api_browse.py -v --tb=short`
Expected: 2 passed.

- [ ] **Step 5: Update the owner doc and commit**

In `docs/architecture/apollo.md`: add `GET /apollo/concepts` to the HTTP-surface/endpoint listing (browse surface, membership-gated, wraps `list_course_concepts`) and bump `last_verified` in the frontmatter to `2026-07-07`.

```bash
cd /Users/ishaanbatra/Documents/GitHub/ai-ta-backend
git add apollo/api.py apollo/tests/test_api_browse.py docs/architecture/apollo.md
git commit -m "feat(apollo): GET /apollo/concepts — student browse surface for teachable concepts"
```

---

### Task 2: `GET /apollo/problems`

**Files:**
- Create: `apollo/handlers/browse.py`
- Modify: `apollo/api.py` (import + endpoint)
- Test: `apollo/tests/test_api_browse.py` (append)
- Modify: `docs/architecture/apollo.md`

**Interfaces:**
- Consumes: `list_problems_for_concept(db, *, concept_id) -> list[Problem]` (`apollo/overseer/problem_selector.py:34`; tier-2 + non-quarantined, `Problem.model_validate` on payloads), `list_course_concepts` (course-scope check), `NoMatchingConceptError(transcript_summary: str)` (`apollo/errors.py:52`, maps to 409), models `ProblemAttempt`, `ApolloSession`.
- Produces: `handle_list_problems(db, *, user_id, search_space_id, concept_id, difficulty=None) -> dict` and `GET /apollo/problems?search_space_id=N&concept_id=N[&difficulty=intro]` → `{"problems": [{"id": str, "difficulty": str, "problem_text": str, "attempted": bool}]}`. NO `reference_solution`, `given_values`, or `target_unknown`. Task 3 reuses the concept-scope check idiom; UI Tasks 5–7 rely on the response shape.

- [ ] **Step 1: Write the failing tests**

Append to `apollo/tests/test_api_browse.py`:

```python
async def _seed_attempt(Session, *, user_id: str, problem_id: str, space_id: int = TEST_SPACE_ID):
    async with Session() as db:
        sess = ApolloSession(
            user_id=user_id, search_space_id=space_id,
            status="ended", phase="REPORT", current_problem_id=problem_id,
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
            db.add(ConceptProblem(
                concept_id=concept.id, problem_code="f1", difficulty="intro",
                payload=_full_problem_payload("f1", concept.id), tier=2,
            ))
            await db.commit()
            return concept.id

    foreign_id = asyncio.run(_seed_foreign())
    client = TestClient(app)
    resp = client.get(
        f"/apollo/problems?search_space_id={TEST_SPACE_ID}&concept_id={foreign_id}",
        headers={"Authorization": "Bearer tok"},
    )
    assert resp.status_code == 409
    assert resp.json()["error_code"] == "no_matching_concept"
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest apollo/tests/test_api_browse.py -v --tb=short -k problems`
Expected: both FAIL with 404 (route not defined).

- [ ] **Step 3: Implement the handler**

Create `apollo/handlers/browse.py`:

```python
"""Student browse surface: list a course's teachable problems for one concept.

Read-only. The eligibility predicate is the selector's (tier-2 +
non-quarantined, via list_problems_for_concept); the course-scope check is
the same candidate set session entry uses (list_course_concepts), so a
concept_id from another course 409s instead of leaking cross-course problems.

Student-safety invariant: the response carries ONLY {id, difficulty,
problem_text, attempted} — never reference_solution / given_values /
target_unknown."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apollo.errors import NoMatchingConceptError
from apollo.overseer.problem_selector import list_problems_for_concept
from apollo.persistence.models import ApolloSession, ProblemAttempt
from apollo.subjects.curriculum_db import list_course_concepts


async def handle_list_problems(
    db: AsyncSession,
    *,
    user_id: str,
    search_space_id: int,
    concept_id: int,
    difficulty: str | None = None,
) -> dict[str, Any]:
    candidates = await list_course_concepts(db, search_space_id=search_space_id)
    if concept_id not in {c.concept_id for c in candidates}:
        raise NoMatchingConceptError(
            f"concept_id={concept_id} is not teachable in course {search_space_id}"
        )

    pool = await list_problems_for_concept(db, concept_id=concept_id)
    if difficulty is not None:
        pool = [p for p in pool if p.difficulty == difficulty]

    attempted_ids = set(
        (
            await db.execute(
                select(ProblemAttempt.problem_id)
                .join(ApolloSession, ProblemAttempt.session_id == ApolloSession.id)
                .where(
                    ApolloSession.user_id == user_id,
                    ApolloSession.search_space_id == search_space_id,
                )
                .distinct()
            )
        )
        .scalars()
        .all()
    )

    return {
        "problems": [
            {
                "id": p.id,
                "difficulty": p.difficulty,
                "problem_text": p.problem_text,
                "attempted": p.id in attempted_ids,
            }
            for p in pool
        ]
    }
```

In `apollo/api.py`, add the import (next to the other handler imports):

```python
from apollo.handlers.browse import handle_list_problems
```

Add the endpoint directly after the `GET /concepts` endpoint, using `Literal` (already imported in api.py):

```python
@router.get("/problems")
async def list_problems(
    search_space_id: int,
    concept_id: int,
    request: Request,
    difficulty: Literal["intro", "standard", "hard"] | None = None,
    db: AsyncSession = Depends(get_db_session),
) -> dict:
    auth = await require_user(request)
    await require_course_member(db=db, auth=auth, search_space_id=search_space_id)
    return await handle_list_problems(
        db=db,
        user_id=auth.user_id,
        search_space_id=search_space_id,
        concept_id=concept_id,
        difficulty=difficulty,
    )
```

- [ ] **Step 4: Run the tests**

Run: `pytest apollo/tests/test_api_browse.py -v --tb=short`
Expected: 4 passed (Task 1's two still green).

- [ ] **Step 5: Update owner doc and commit**

Add `GET /apollo/problems` + `apollo/handlers/browse.py` to `docs/architecture/apollo.md` (note the student-safety invariant: no `reference_solution` in browse responses).

```bash
git add apollo/handlers/browse.py apollo/api.py apollo/tests/test_api_browse.py docs/architecture/apollo.md
git commit -m "feat(apollo): GET /apollo/problems — teachable problem browse with attempted flags, no solution leakage"
```

---

### Task 3: `POST /apollo/sessions` (standalone entry)

**Files:**
- Modify: `apollo/hoot_bridge/session_init.py` (extract shared creation core, add `init_session_direct`)
- Modify: `apollo/errors.py` (new `ProblemNotFoundError`)
- Modify: `apollo/api.py` (request model, endpoint, 404 handler registration)
- Test: `apollo/tests/test_api_browse.py` (append)
- Modify: `docs/architecture/apollo.md`

**Interfaces:**
- Consumes: everything `init_session_from_hoot` consumes (`apollo/hoot_bridge/session_init.py:39-109`), plus `list_problems_for_concept`, `list_course_concepts`, `NoMatchingConceptError`.
- Produces:
  - `ProblemNotFoundError(*, problem_id: str, concept_id: int)` in `apollo/errors.py` → 404 `{"error_code": "problem_not_found", ...}`.
  - `init_session_direct(db, *, user_id, search_space_id, concept_id, difficulty, problem_id=None) -> dict` returning `{"session_id", "attempt_id", "problem": {id, concept_id, difficulty, problem_text, given_values, target_unknown}}` — byte-compatible with `init_session_from_hoot`'s return.
  - `POST /apollo/sessions` with body `{"search_space_id": int, "concept_id": int, "difficulty": "intro"|"standard"|"hard", "problem_id"?: str}` (difficulty REQUIRED — the student always picks). UI Task 5 relies on this exact contract.

- [ ] **Step 1: Write the failing tests**

Append to `apollo/tests/test_api_browse.py`:

```python
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
    assert isinstance(body["session_id"], int)
    assert isinstance(body["attempt_id"], int)

    # session row exists, TEACHING phase, correct concept binding
    async def _check():
        async with Session() as db:
            from sqlalchemy import select
            row = (
                await db.execute(
                    select(ApolloSession).where(ApolloSession.id == body["session_id"])
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
        json={"search_space_id": TEST_SPACE_ID, "concept_id": concept_id,
              "difficulty": "intro", "problem_id": codes[0]},
        headers={"Authorization": "Bearer tok"},
    ).json()
    second = client.post(
        "/apollo/sessions",
        json={"search_space_id": TEST_SPACE_ID, "concept_id": concept_id,
              "difficulty": "intro", "problem_id": codes[1]},
        headers={"Authorization": "Bearer tok"},
    ).json()

    async def _check():
        async with Session() as db:
            from sqlalchemy import select
            old = (
                await db.execute(
                    select(ApolloSession).where(ApolloSession.id == first["session_id"])
                )
            ).scalar_one()
            new = (
                await db.execute(
                    select(ApolloSession).where(ApolloSession.id == second["session_id"])
                )
            ).scalar_one()
            assert old.status == "ended"
            assert new.status == "active"

    asyncio.run(_check())
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest apollo/tests/test_api_browse.py -v --tb=short -k create_session`
Expected: all 4 FAIL with 404/405 (route not defined).

- [ ] **Step 3: Add `ProblemNotFoundError`**

In `apollo/errors.py`, add after `PoolExhaustedError` (line ~71):

```python
class ProblemNotFoundError(ApolloError):
    """Standalone session entry named a problem_id that is not in the
    concept's teachable pool (bad id, tier-1, quarantined, or another
    concept's problem). Surfaces as 404 to the FE."""

    def __init__(self, *, problem_id: str, concept_id: int) -> None:
        self.problem_id = problem_id
        self.concept_id = concept_id
        super().__init__(
            f"Problem {problem_id!r} not found in teachable pool of concept {concept_id}"
        )
```

- [ ] **Step 4: Extract the shared creation core and add `init_session_direct`**

In `apollo/hoot_bridge/session_init.py`, refactor so both entries share the row-creation block (currently lines 67-109 of `init_session_from_hoot`). The file becomes:

```python
"""Apollo session initialization: Hoot handoff + standalone entry.

init_session_from_hoot — the original Hoot→Apollo handoff. The transcript is
used for exactly one thing: infer_concept_id picks the concept. Everything
else is shared with the standalone path.

init_session_direct — WU-E2E standalone entry (2026-07-07 spec). The student
explicitly picks concept_id (validated against the course's teachable set) and
optionally a specific problem_id (validated against the concept's teachable
pool). No LLM call, no transcript.

Both raise NoMatchingConceptError / PoolExhaustedError (409) —
init_session_direct additionally raises ProblemNotFoundError (404).
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from apollo.errors import NoMatchingConceptError, ProblemNotFoundError
from apollo.overseer.concept_inference import infer_concept_id
from apollo.overseer.problem_selector import (
    list_problems_for_concept,
    select_problem_personalized,
)
from apollo.persistence.models import (
    ApolloSession,
    ProblemAttempt,
    SessionPhase,
    SessionStatus,
)
from apollo.schemas.problem import Problem
from apollo.subjects.curriculum_db import list_course_concepts

_ALLOWED_DIFFICULTIES = {"intro", "standard", "hard"}


async def _create_session_with_problem(
    db: AsyncSession,
    *,
    user_id: str,
    search_space_id: int,
    concept_id: int,
    difficulty: str,
    problem: Problem,
) -> dict[str, Any]:
    """Shared tail of both entries: end any active session, create the
    TEACHING session + first attempt, commit, return the FE payload.
    Moved verbatim from init_session_from_hoot (WU-3D shape unchanged)."""
    await db.execute(
        update(ApolloSession)
        .where(
            ApolloSession.user_id == user_id,
            ApolloSession.status == SessionStatus.active.value,
        )
        .values(status=SessionStatus.ended.value)
    )
    await db.flush()

    session = ApolloSession(
        user_id=user_id,
        search_space_id=search_space_id,
        concept_id=concept_id,
        status=SessionStatus.active.value,
        phase=SessionPhase.TEACHING.value,
        current_problem_id=problem.id,
    )
    db.add(session)
    await db.flush()

    attempt = ProblemAttempt(
        session_id=session.id,
        problem_id=problem.id,
        difficulty=difficulty,
    )
    db.add(attempt)
    await db.flush()
    attempt_id = attempt.id
    await db.commit()

    return {
        "session_id": session.id,
        "attempt_id": attempt_id,
        "problem": {
            "id": problem.id,
            "concept_id": problem.concept_id,
            "difficulty": problem.difficulty,
            "problem_text": problem.problem_text,
            "given_values": problem.given_values,
            "target_unknown": problem.target_unknown,
        },
    }


async def init_session_from_hoot(
    *,
    db: AsyncSession,
    user_id: str,
    search_space_id: int,
    hoot_transcript: str,
    difficulty: str,
) -> dict[str, Any]:
    if difficulty not in _ALLOWED_DIFFICULTIES:
        raise ValueError(
            f"unknown difficulty {difficulty!r}; expected one of {sorted(_ALLOWED_DIFFICULTIES)}"
        )

    candidates = await list_course_concepts(db, search_space_id=search_space_id)
    concept_id = infer_concept_id(
        transcript=hoot_transcript,
        candidates=candidates,
    )

    problem = await select_problem_personalized(
        db,
        user_id=user_id,
        search_space_id=search_space_id,
        concept_id=concept_id,
        difficulty=difficulty,
        attempted_ids=[],
    )
    return await _create_session_with_problem(
        db,
        user_id=user_id,
        search_space_id=search_space_id,
        concept_id=concept_id,
        difficulty=difficulty,
        problem=problem,
    )


async def init_session_direct(
    *,
    db: AsyncSession,
    user_id: str,
    search_space_id: int,
    concept_id: int,
    difficulty: str,
    problem_id: str | None = None,
) -> dict[str, Any]:
    if difficulty not in _ALLOWED_DIFFICULTIES:
        raise ValueError(
            f"unknown difficulty {difficulty!r}; expected one of {sorted(_ALLOWED_DIFFICULTIES)}"
        )

    candidates = await list_course_concepts(db, search_space_id=search_space_id)
    if concept_id not in {c.concept_id for c in candidates}:
        raise NoMatchingConceptError(
            f"concept_id={concept_id} is not teachable in course {search_space_id}"
        )

    if problem_id is not None:
        pool = await list_problems_for_concept(db, concept_id=concept_id)
        problem = next((p for p in pool if p.id == problem_id), None)
        if problem is None:
            raise ProblemNotFoundError(problem_id=problem_id, concept_id=concept_id)
    else:
        problem = await select_problem_personalized(
            db,
            user_id=user_id,
            search_space_id=search_space_id,
            concept_id=concept_id,
            difficulty=difficulty,
            attempted_ids=[],
        )

    return await _create_session_with_problem(
        db,
        user_id=user_id,
        search_space_id=search_space_id,
        concept_id=concept_id,
        difficulty=difficulty,
        problem=problem,
    )
```

Note: when `problem_id` is given, the chosen problem's own `difficulty` may differ from the request's `difficulty` field; the attempt records the REQUESTED difficulty and the problem keeps its own — same behavior as `handle_next`. Do not "fix" this.

- [ ] **Step 5: Add the endpoint + 404 handler**

In `apollo/api.py`:

Extend the `apollo.errors` import block with `ProblemNotFoundError`, and extend the session_init import:

```python
from apollo.hoot_bridge.session_init import init_session_direct, init_session_from_hoot
```

Add the request model after `FromHootRequest` (~line 103):

```python
class SessionCreateRequest(BaseModel):
    search_space_id: int
    concept_id: int
    # Standalone entry: the student explicitly picks — no default.
    difficulty: Literal["intro", "standard", "hard"]
    problem_id: str | None = None
```

Add the endpoint after `session_from_hoot` (~line 128):

```python
@router.post("/sessions")
async def create_session(
    body: SessionCreateRequest,
    request: Request,
    db: AsyncSession = Depends(get_db_session),
) -> dict:
    """Standalone Apollo entry (no Hoot transcript): explicit concept +
    difficulty + optional specific problem."""
    auth = await require_user(request)
    await require_course_member(db=db, auth=auth, search_space_id=body.search_space_id)
    return await init_session_direct(
        db=db,
        user_id=auth.user_id,
        search_space_id=body.search_space_id,
        concept_id=body.concept_id,
        difficulty=body.difficulty,
        problem_id=body.problem_id,
    )
```

In the exception-handler section (near `kg_entry_not_found_handler`, ~line 434), add:

```python
async def problem_not_found_handler(request: Request, exc: ProblemNotFoundError) -> JSONResponse:
    """Standalone entry named a problem that is not in the teachable pool
    (stale browse list, quarantined since, or bad id)."""
    return JSONResponse(
        status_code=404,
        content=_err_payload(
            "problem_not_found",
            str(exc),
            problem_id=exc.problem_id,
            concept_id=exc.concept_id,
        ),
    )
```

And register it inside `register_exception_handlers` alongside the existing `app.add_exception_handler(...)` calls:

```python
    app.add_exception_handler(ProblemNotFoundError, problem_not_found_handler)
```

- [ ] **Step 6: Run the tests — new and regression**

Run: `pytest apollo/tests/test_api_browse.py apollo/hoot_bridge/ apollo/tests/test_api_auth.py -v --tb=short`
Expected: all pass (the from_hoot refactor is behavior-preserving; its existing tests are the regression gate).

- [ ] **Step 7: Update owner doc and commit**

Document `POST /apollo/sessions`, `init_session_direct`, `ProblemNotFoundError`/404 in `docs/architecture/apollo.md`.

```bash
git add apollo/hoot_bridge/session_init.py apollo/errors.py apollo/api.py apollo/tests/test_api_browse.py docs/architecture/apollo.md
git commit -m "feat(apollo): POST /apollo/sessions — standalone entry with explicit concept/difficulty/problem"
```

---

### Task 4: Progress `detail` block

**Files:**
- Modify: `apollo/handlers/progress.py`
- Modify: `apollo/api.py` (progress endpoint gains optional `search_space_id`)
- Test: `apollo/handlers/tests/test_progress_detail.py` (new)
- Modify: `docs/architecture/apollo.md`

**Interfaces:**
- Consumes: `handle_get_progress` (unchanged), models `LearnerState` (mastery per `(user, space, entity)`), `KGEntity` (`entity_id → concept_id`), `Concept` (display names), `ProblemAttempt.diagnostic_report` (JSONB `{"rubric": {"overall": {"score", "letter"}, ...}, ...}` — always written on grade, artifact flag irrelevant), `ApolloSession` (user/space/concept scoping).
- Produces: `handle_get_progress_detail(*, db, user_id, search_space_id) -> dict` = the `handle_get_progress` payload plus:

```
"detail": {
  "mastery": [{"concept_id": int, "display_name": str, "mastery_avg": float, "entity_count": int}],
  "recent_attempts": [{"attempt_id": int, "problem_id": str, "concept_id": int|null,
                        "concept_display_name": str|null, "difficulty": str,
                        "score": int|null, "letter": str|null, "created_at": str}]
}
```

`GET /apollo/progress?search_space_id=N` returns this; without the param the payload is byte-identical to today (Hoot-side callers unaffected). UI Tasks 5 and 10 rely on these shapes.

- [ ] **Step 1: Write the failing tests**

Create `apollo/handlers/tests/test_progress_detail.py`:

```python
"""handle_get_progress_detail: per-concept mastery + recent graded attempts."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from apollo.conftest import TEST_SPACE_ID, TEST_USER_ID, TEST_USER_ID_2
from apollo.handlers.progress import handle_get_progress_detail
from apollo.persistence.models import (
    ApolloSession,
    Concept,
    KGEntity,
    LearnerState,
    ProblemAttempt,
    StudentProgress,
    Subject,
)
from database.models import Base

TABLES = [
    StudentProgress.__table__,
    Subject.__table__,
    Concept.__table__,
    KGEntity.__table__,
    LearnerState.__table__,
    ApolloSession.__table__,
    ProblemAttempt.__table__,
]


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(lambda sc: Base.metadata.create_all(sc, tables=TABLES))
    Session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with Session() as s:
        yield s
    await engine.dispose()


async def _seed_concept(db, *, slug: str, name: str) -> int:
    subj = Subject(slug=f"subj-{slug}", display_name=slug, search_space_id=TEST_SPACE_ID)
    db.add(subj)
    await db.flush()
    c = Concept(subject_id=subj.id, slug=slug, display_name=name)
    db.add(c)
    await db.flush()
    return c.id


async def _seed_mastery(db, *, concept_id: int, values: list[float]) -> None:
    for i, m in enumerate(values):
        ent = KGEntity(
            concept_id=concept_id, canonical_key=f"k{concept_id}-{i}",
            kind="quantity", display_name=f"Entity {concept_id}-{i}",
        )
        db.add(ent)
        await db.flush()
        db.add(LearnerState(
            user_id=TEST_USER_ID, search_space_id=TEST_SPACE_ID, entity_id=ent.id,
            belief=[0.2, 0.8], mastery=m, confidence=0.9, evidence_count=1,
        ))
    await db.commit()


async def _seed_graded_attempt(
    db, *, concept_id: int, problem_id: str, score: int, letter: str,
    user_id: str = TEST_USER_ID, when: datetime | None = None,
) -> None:
    sess = ApolloSession(
        user_id=user_id, search_space_id=TEST_SPACE_ID, concept_id=concept_id,
        status="ended", phase="REPORT", current_problem_id=problem_id,
    )
    db.add(sess)
    await db.flush()
    db.add(ProblemAttempt(
        session_id=sess.id, problem_id=problem_id, difficulty="intro",
        result="graded",
        diagnostic_report={"rubric": {"overall": {"score": score, "letter": letter}},
                           "narrative": "..."},
        created_at=when or datetime.now(UTC),
    ))
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
    await _seed_graded_attempt(db, concept_id=c1, problem_id="p-old",
                               score=60, letter="C", when=old)
    await _seed_graded_attempt(db, concept_id=c1, problem_id="p-new",
                               score=85, letter="A-")
    # ungraded attempt and another student's attempt must not appear
    sess = ApolloSession(user_id=TEST_USER_ID, search_space_id=TEST_SPACE_ID,
                         concept_id=c1, status="active", phase="TEACHING",
                         current_problem_id="p-live")
    db.add(sess)
    await db.flush()
    db.add(ProblemAttempt(session_id=sess.id, problem_id="p-live", difficulty="intro"))
    await db.commit()
    await _seed_graded_attempt(db, concept_id=c1, problem_id="p-other",
                               score=99, letter="A+", user_id=TEST_USER_ID_2)

    out = await handle_get_progress_detail(
        db=db, user_id=TEST_USER_ID, search_space_id=TEST_SPACE_ID
    )
    attempts = out["detail"]["recent_attempts"]
    assert [a["problem_id"] for a in attempts] == ["p-new", "p-old"]
    assert attempts[0]["score"] == 85
    assert attempts[0]["letter"] == "A-"
    assert attempts[0]["concept_display_name"] == "Newton's Second Law"
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest apollo/handlers/tests/test_progress_detail.py -v --tb=short`
Expected: ImportError — `handle_get_progress_detail` does not exist.

- [ ] **Step 3: Implement the handler**

Replace `apollo/handlers/progress.py` with:

```python
"""GET /apollo/progress — XP + level, plus an optional per-course detail block.

Base payload (no search_space_id): unchanged since P1 — {user_id, xp_total,
level, title, next_tier_threshold}. Callers that don't pass a course get a
byte-identical response to the pre-detail era.

Detail block (search_space_id given): per-concept mastery averaged over
apollo_learner_state (entity → concept via apollo_kg_entities.concept_id) and
the 10 most recent GRADED attempts read from ProblemAttempt.diagnostic_report
(always written on grade — no dependency on APOLLO_GRADING_ARTIFACT_ENABLED)."""

from __future__ import annotations

from typing import Any, Dict

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from apollo.overseer.xp import next_tier_threshold, title_for_level
from apollo.persistence.models import (
    ApolloSession,
    Concept,
    KGEntity,
    LearnerState,
    ProblemAttempt,
)
from apollo.persistence.progress_repo import load_progress

RECENT_ATTEMPTS_LIMIT = 10


async def handle_get_progress(
    *,
    db: AsyncSession,
    user_id: str,
) -> Dict[str, Any]:
    row = await load_progress(db=db, user_id=user_id)
    return {
        "user_id": row.user_id,
        "xp_total": row.xp_total,
        "level": row.level,
        "title": title_for_level(row.level),
        "next_tier_threshold": next_tier_threshold(row.level),
    }


async def handle_get_progress_detail(
    *,
    db: AsyncSession,
    user_id: str,
    search_space_id: int,
) -> Dict[str, Any]:
    base = await handle_get_progress(db=db, user_id=user_id)

    mastery_rows = (
        await db.execute(
            select(
                KGEntity.concept_id,
                Concept.display_name,
                func.avg(LearnerState.mastery),
                func.count(LearnerState.entity_id),
            )
            .join(KGEntity, LearnerState.entity_id == KGEntity.id)
            .join(Concept, KGEntity.concept_id == Concept.id)
            .where(
                LearnerState.user_id == user_id,
                LearnerState.search_space_id == search_space_id,
            )
            .group_by(KGEntity.concept_id, Concept.display_name)
            .order_by(Concept.display_name)
        )
    ).all()

    attempt_rows = (
        await db.execute(
            select(ProblemAttempt, ApolloSession.concept_id, Concept.display_name)
            .join(ApolloSession, ProblemAttempt.session_id == ApolloSession.id)
            .outerjoin(Concept, ApolloSession.concept_id == Concept.id)
            .where(
                ApolloSession.user_id == user_id,
                ApolloSession.search_space_id == search_space_id,
                ProblemAttempt.result == "graded",
            )
            .order_by(ProblemAttempt.created_at.desc())
            .limit(RECENT_ATTEMPTS_LIMIT)
        )
    ).all()

    recent = []
    for attempt, concept_id, display_name in attempt_rows:
        report = attempt.diagnostic_report or {}
        overall = (report.get("rubric") or {}).get("overall") or {}
        recent.append(
            {
                "attempt_id": attempt.id,
                "problem_id": attempt.problem_id,
                "concept_id": concept_id,
                "concept_display_name": display_name,
                "difficulty": attempt.difficulty,
                "score": overall.get("score"),
                "letter": overall.get("letter"),
                "created_at": attempt.created_at.isoformat(),
            }
        )

    base["detail"] = {
        "mastery": [
            {
                "concept_id": concept_id,
                "display_name": display_name,
                "mastery_avg": round(float(avg), 3),
                "entity_count": int(count),
            }
            for concept_id, display_name, avg, count in mastery_rows
        ],
        "recent_attempts": recent,
    }
    return base
```

- [ ] **Step 4: Wire the endpoint param**

In `apollo/api.py`, extend the progress import and replace the `GET /progress` endpoint:

```python
from apollo.handlers.progress import handle_get_progress, handle_get_progress_detail
```

```python
@router.get("/progress")
async def progress(
    request: Request,
    search_space_id: int | None = None,
    db: AsyncSession = Depends(get_db_session),
) -> dict:
    auth = await require_user(request)
    if search_space_id is None:
        return await handle_get_progress(db=db, user_id=auth.user_id)
    await require_course_member(db=db, auth=auth, search_space_id=search_space_id)
    return await handle_get_progress_detail(
        db=db, user_id=auth.user_id, search_space_id=search_space_id
    )
```

- [ ] **Step 5: Run new tests + progress regressions**

Run: `pytest apollo/handlers/tests/test_progress_detail.py apollo/handlers/tests/test_progress.py apollo/tests/test_api_auth.py -v --tb=short`
Expected: all pass.

- [ ] **Step 6: Full backend suite, owner doc, commit**

Run: `pytest apollo/ -v --tb=short -m "not integration"`
Expected: all pass (integration tests need Docker; run them too if Docker is up: `pytest apollo/ -v --tb=short`).

Update `docs/architecture/apollo.md` (progress endpoint's optional `search_space_id` + detail shape).

```bash
git add apollo/handlers/progress.py apollo/api.py apollo/handlers/tests/test_progress_detail.py docs/architecture/apollo.md
git commit -m "feat(apollo): per-course progress detail — concept mastery + recent graded attempts"
git push -u origin weekend/apollo-student-e2e
```

---

# Part B — Student UI (ai-ta-student-ui)

All tasks in `/Users/ishaanbatra/Documents/GitHub/ai-ta-student-ui`. First create the branch:

```bash
cd /Users/ishaanbatra/Documents/GitHub/ai-ta-student-ui
git checkout main && git pull && git checkout -b apollo-e2e-baseline
```

Verification for every UI task: `npm run lint && npm run build` (no test framework in this repo — do NOT add one).

### Task 5: API client + proxy routes

**Files:**
- Modify: `lib/apollo/api.ts`
- Create: `app/api/apollo/concepts/route.ts`
- Create: `app/api/apollo/problems/route.ts`
- Create: `app/api/apollo/sessions/route.ts`
- Create: `app/api/apollo/sessions/[id]/next/route.ts`
- Create: `app/api/apollo/sessions/[id]/restart_problem/route.ts`
- Modify: `app/api/apollo/progress/route.ts` (forward the query string)

**Interfaces:**
- Consumes: backend endpoints from Tasks 1–4 (exact shapes in those tasks' Produces blocks); existing `apolloHeaders`/`_handle`/`ApolloProblem` in `lib/apollo/api.ts`.
- Produces (used by Tasks 6–10):

```ts
listConcepts(searchSpaceId: number): Promise<{ concepts: ApolloConceptSummary[] }>
listProblems(searchSpaceId: number, conceptId: number, difficulty?: ApolloDifficulty): Promise<{ problems: ApolloProblemSummary[] }>
startSession(searchSpaceId: number, conceptId: number, difficulty: ApolloDifficulty, problemId?: string): Promise<StartSessionResponse>
nextProblem(sessionId: number, difficulty: ApolloDifficulty): Promise<StartSessionResponse>
restartProblem(sessionId: number): Promise<Record<string, unknown>>
getStudentProgressDetailed(searchSpaceId: number): Promise<StudentProgressDetailed>
```

- [ ] **Step 1: Add types and client functions to `lib/apollo/api.ts`**

Append after the existing `getStudentProgress` function:

```ts
// ---------------------------------------------------------------------------
// Standalone entry + browse surface (2026-07-07 e2e baseline)
// ---------------------------------------------------------------------------

export type ApolloDifficulty = "intro" | "standard" | "hard";

export interface ApolloConceptSummary {
  concept_id: number;
  slug: string;
  display_name: string;
}

export interface ApolloProblemSummary {
  id: string;
  difficulty: string;
  problem_text: string;
  attempted: boolean;
}

export interface StartSessionResponse {
  session_id: number;
  attempt_id: number;
  problem: ApolloProblem;
}

export interface ConceptMastery {
  concept_id: number;
  display_name: string;
  mastery_avg: number;
  entity_count: number;
}

export interface RecentAttempt {
  attempt_id: number;
  problem_id: string;
  concept_id: number | null;
  concept_display_name: string | null;
  difficulty: string;
  score: number | null;
  letter: string | null;
  created_at: string;
}

export interface ProgressDetail {
  mastery: ConceptMastery[];
  recent_attempts: RecentAttempt[];
}

export interface StudentProgressDetailed extends StudentProgress {
  detail: ProgressDetail;
}

export async function listConcepts(
  searchSpaceId: number,
): Promise<{ concepts: ApolloConceptSummary[] }> {
  const res = await fetch(`/api/apollo/concepts?search_space_id=${searchSpaceId}`, {
    headers: apolloHeaders(),
  });
  return (await _handle(res)) as { concepts: ApolloConceptSummary[] };
}

export async function listProblems(
  searchSpaceId: number,
  conceptId: number,
  difficulty?: ApolloDifficulty,
): Promise<{ problems: ApolloProblemSummary[] }> {
  const qs = new URLSearchParams({
    search_space_id: String(searchSpaceId),
    concept_id: String(conceptId),
  });
  if (difficulty) qs.set("difficulty", difficulty);
  const res = await fetch(`/api/apollo/problems?${qs.toString()}`, {
    headers: apolloHeaders(),
  });
  return (await _handle(res)) as { problems: ApolloProblemSummary[] };
}

export async function startSession(
  searchSpaceId: number,
  conceptId: number,
  difficulty: ApolloDifficulty,
  problemId?: string,
): Promise<StartSessionResponse> {
  const res = await fetch("/api/apollo/sessions", {
    method: "POST",
    headers: apolloHeaders(true),
    body: JSON.stringify({
      search_space_id: searchSpaceId,
      concept_id: conceptId,
      difficulty,
      ...(problemId ? { problem_id: problemId } : {}),
    }),
  });
  return (await _handle(res)) as StartSessionResponse;
}

export async function nextProblem(
  sessionId: number,
  difficulty: ApolloDifficulty,
): Promise<StartSessionResponse> {
  const res = await fetch(`/api/apollo/sessions/${sessionId}/next`, {
    method: "POST",
    headers: apolloHeaders(true),
    body: JSON.stringify({ difficulty }),
  });
  return (await _handle(res)) as StartSessionResponse;
}

export async function restartProblem(
  sessionId: number,
): Promise<Record<string, unknown>> {
  const res = await fetch(`/api/apollo/sessions/${sessionId}/restart_problem`, {
    method: "POST",
    headers: apolloHeaders(true),
    body: "{}",
  });
  return (await _handle(res)) as Record<string, unknown>;
}

export async function getStudentProgressDetailed(
  searchSpaceId: number,
): Promise<StudentProgressDetailed> {
  const res = await fetch(`/api/apollo/progress?search_space_id=${searchSpaceId}`, {
    headers: apolloHeaders(),
  });
  return (await _handle(res)) as StudentProgressDetailed;
}
```

Also add `"problem_not_found"` to the `ApolloErrorCode` union (before `"unknown"`).

- [ ] **Step 2: Create the GET proxy routes**

`app/api/apollo/concepts/route.ts` (the repo's inline proxy pattern — no shared helper exists; GET forwards the query string):

```ts
export const runtime = 'nodejs';

export async function GET(req: Request) {
  const rawBackend = process.env.AI_TA_API_BASE_URL;
  const backend = rawBackend ? rawBackend.replace(/\/+$/, '') : '';
  if (!backend) return new Response('AI_TA_API_BASE_URL missing', { status: 500 });

  const authHeader = req.headers.get('authorization');
  const headers: Record<string, string> = {};
  if (authHeader) headers.Authorization = authHeader;

  const search = new URL(req.url).search;
  const resp = await fetch(`${backend}/apollo/concepts${search}`, { headers });
  return new Response(resp.body, {
    status: resp.status,
    headers: {
      'Content-Type': resp.headers.get('content-type') ?? 'application/json',
      'Cache-Control': 'no-store',
    },
  });
}
```

`app/api/apollo/problems/route.ts` — identical file with `/apollo/problems${search}` as the target URL.

- [ ] **Step 3: Create the POST proxy routes**

`app/api/apollo/sessions/route.ts`:

```ts
export const runtime = 'nodejs';

export async function POST(req: Request) {
  const rawBackend = process.env.AI_TA_API_BASE_URL;
  const backend = rawBackend ? rawBackend.replace(/\/+$/, '') : '';
  if (!backend) return new Response('AI_TA_API_BASE_URL missing', { status: 500 });

  const body = await req.text();
  const authHeader = req.headers.get('authorization');
  const headers: Record<string, string> = { 'Content-Type': 'application/json' };
  if (authHeader) headers.Authorization = authHeader;

  const resp = await fetch(`${backend}/apollo/sessions`, { method: 'POST', headers, body });
  return new Response(resp.body, {
    status: resp.status,
    headers: {
      'Content-Type': resp.headers.get('content-type') ?? 'application/json',
      'Cache-Control': 'no-store',
    },
  });
}
```

`app/api/apollo/sessions/[id]/next/route.ts` (mirror of the existing `[id]/chat/route.ts` with `/next` as the path segment):

```ts
export const runtime = 'nodejs';

export async function POST(req: Request, ctx: { params: Promise<{ id: string }> }) {
  const rawBackend = process.env.AI_TA_API_BASE_URL;
  const backend = rawBackend ? rawBackend.replace(/\/+$/, '') : '';
  if (!backend) return new Response('AI_TA_API_BASE_URL missing', { status: 500 });

  const body = await req.text();
  const authHeader = req.headers.get('authorization');
  const headers: Record<string, string> = { 'Content-Type': 'application/json' };
  if (authHeader) headers.Authorization = authHeader;

  const { id } = await ctx.params;
  const resp = await fetch(`${backend}/apollo/sessions/${encodeURIComponent(id)}/next`, {
    method: 'POST', headers, body,
  });
  return new Response(resp.body, {
    status: resp.status,
    headers: {
      'Content-Type': resp.headers.get('content-type') ?? 'application/json',
      'Cache-Control': 'no-store',
    },
  });
}
```

`app/api/apollo/sessions/[id]/restart_problem/route.ts` — identical with `/restart_problem` as the path segment.

- [ ] **Step 4: Forward the query string on the progress proxy**

Open `app/api/apollo/progress/route.ts`. Its GET currently targets `${backend}/apollo/progress` with no query. Change the fetch target to append the incoming query string:

```ts
  const search = new URL(req.url).search;
  const resp = await fetch(`${backend}/apollo/progress${search}`, { headers });
```

(If the handler's request parameter is currently unnamed or typed differently, adjust minimally to expose `req: Request`.)

- [ ] **Step 5: Verify and commit**

Run: `npm run lint && npm run build`
Expected: no errors (warnings acceptable if pre-existing).

```bash
git add lib/apollo/api.ts app/api/apollo/
git commit -m "feat(apollo): client + proxy routes for browse, standalone sessions, next/restart, progress detail"
```

---

### Task 6: Browse screen

**Files:**
- Create: `components/apollo/ApolloBrowse.tsx`
- Modify: `app/apollo/ApolloPageClient.tsx` (render Browse when no `?session=`)
- Modify: `app/page.tsx` (nav entry to `/apollo?class=<id>`)
- Modify: `app/globals.css` (browse classes, appended at end)

**Interfaces:**
- Consumes: `listConcepts`, `listProblems`, `startSession`, `getStudentProgress`, `ApolloApiError`, types from Task 5; existing `ApolloErrorSurface` (`components/apollo/ApolloErrorSurface.tsx`, props `{error, onDismiss}`).
- Produces: `<ApolloBrowse classId={number} onStarted={(sessionId: number) => void} />`. URL contract: `/apollo?class=N` = browse for course N; `/apollo?session=M&class=N` = live session (Task 10's dashboard links back with `?class=N`).

- [ ] **Step 1: Create `components/apollo/ApolloBrowse.tsx`**

```tsx
"use client";

// Standalone Apollo entry (2026-07-07 e2e baseline): concept → difficulty →
// problem picker. No Hoot transcript, no LLM inference — a deterministic
// browse over the course's teachable pool.

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import {
  ApolloConceptSummary,
  ApolloDifficulty,
  ApolloProblemSummary,
  StudentProgress,
  getStudentProgress,
  listConcepts,
  listProblems,
  startSession,
} from "@/lib/apollo/api";
import ApolloErrorSurface from "./ApolloErrorSurface";
import ApolloProgressCard from "./ApolloProgressCard";

const DIFFICULTIES: ApolloDifficulty[] = ["intro", "standard", "hard"];
const PREVIEW_CHARS = 180;

interface Props {
  classId: number;
  onStarted: (sessionId: number) => void;
}

export default function ApolloBrowse({ classId, onStarted }: Props) {
  const [concepts, setConcepts] = useState<ApolloConceptSummary[] | null>(null);
  const [conceptId, setConceptId] = useState<number | null>(null);
  const [difficulty, setDifficulty] = useState<ApolloDifficulty>("intro");
  const [problems, setProblems] = useState<ApolloProblemSummary[] | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const [progress, setProgress] = useState<StudentProgress | null>(null);

  useEffect(() => {
    // Compact progress header (spec §UI) — non-blocking, browse renders without it.
    getStudentProgress().then(setProgress).catch(() => {});
    listConcepts(classId)
      .then((r) => {
        setConcepts(r.concepts);
        if (r.concepts.length > 0) setConceptId(r.concepts[0].concept_id);
      })
      .catch((e) => setError(e));
  }, [classId]);

  useEffect(() => {
    if (conceptId === null) return;
    setProblems(null);
    listProblems(classId, conceptId, difficulty)
      .then((r) => setProblems(r.problems))
      .catch((e) => setError(e));
  }, [classId, conceptId, difficulty]);

  const start = useCallback(
    async (problemId?: string) => {
      if (conceptId === null) return;
      setBusy(true);
      setError(null);
      try {
        const res = await startSession(classId, conceptId, difficulty, problemId);
        onStarted(res.session_id);
      } catch (e) {
        setError(e);
        setBusy(false);
      }
    },
    [classId, conceptId, difficulty, onStarted],
  );

  if (concepts === null && error === null) {
    return <div className="apollo-browse__loading">Loading concepts…</div>;
  }

  return (
    <div className="apollo-browse">
      <header className="apollo-browse__header">
        <h1 className="apollo-browse__title">Teach Apollo</h1>
        <p className="apollo-browse__subtitle">
          Pick a concept and a problem, then teach Apollo how to solve it.
        </p>
        <ApolloProgressCard progress={progress} />
        <Link className="apollo-browse__progress-link" href={`/apollo/progress?class=${classId}`}>
          View my progress →
        </Link>
      </header>

      <ApolloErrorSurface error={error} onDismiss={() => setError(null)} />

      {concepts !== null && concepts.length === 0 && (
        <div className="apollo-browse__empty">
          No teachable concepts in this course yet. Check back soon!
        </div>
      )}

      {concepts !== null && concepts.length > 0 && (
        <div className="apollo-browse__columns">
          <nav className="apollo-browse__concepts" aria-label="Concepts">
            {concepts.map((c) => (
              <button
                key={c.concept_id}
                className={`apollo-browse__concept ${
                  c.concept_id === conceptId ? "apollo-browse__concept--active" : ""
                }`}
                onClick={() => setConceptId(c.concept_id)}
              >
                {c.display_name}
              </button>
            ))}
          </nav>

          <section className="apollo-browse__problems">
            <div className="apollo-browse__difficulties" role="tablist">
              {DIFFICULTIES.map((d) => (
                <button
                  key={d}
                  role="tab"
                  aria-selected={d === difficulty}
                  className={`apollo-browse__difficulty ${
                    d === difficulty ? "apollo-browse__difficulty--active" : ""
                  }`}
                  onClick={() => setDifficulty(d)}
                >
                  {d}
                </button>
              ))}
              <button
                className="apollo-browse__surprise kg-pill__btn-secondary"
                disabled={busy || conceptId === null}
                onClick={() => start()}
              >
                Surprise me
              </button>
            </div>

            {problems === null && <div className="apollo-browse__loading">Loading problems…</div>}
            {problems !== null && problems.length === 0 && (
              <div className="apollo-browse__empty">
                No {difficulty} problems for this concept yet — try another difficulty.
              </div>
            )}
            <ul className="apollo-browse__cards">
              {(problems ?? []).map((p) => (
                <li key={p.id} className="apollo-browse__card">
                  <p className="apollo-browse__card-text">
                    {p.problem_text.length > PREVIEW_CHARS
                      ? `${p.problem_text.slice(0, PREVIEW_CHARS)}…`
                      : p.problem_text}
                  </p>
                  <div className="apollo-browse__card-footer">
                    {p.attempted && <span className="apollo-browse__tried">tried</span>}
                    <button
                      className="kg-pill__btn-primary"
                      disabled={busy}
                      onClick={() => start(p.id)}
                    >
                      Start teaching
                    </button>
                  </div>
                </li>
              ))}
            </ul>
          </section>
        </div>
      )}
    </div>
  );
}
```

- [ ] **Step 2: Route browse vs. session in `ApolloPageClient.tsx`**

In `app/apollo/ApolloPageClient.tsx`, after the existing `const sessionId = Number(searchParams.get("session"));` (line ~29), add:

```tsx
  const classId = Number(searchParams.get("class"));
```

Replace the existing "Missing ?session=N" guard branch with:

```tsx
  if (!sessionId) {
    if (classId) {
      return (
        <main className="apollo-page">
          <ApolloBrowse
            classId={classId}
            onStarted={(sid) => router.replace(`/apollo?session=${sid}&class=${classId}`)}
          />
        </main>
      );
    }
    return (
      <main className="apollo-page">
        <p>Open Apollo from your class page so we know which course you&apos;re in.</p>
      </main>
    );
  }
```

Add the import at the top: `import ApolloBrowse from "@/components/apollo/ApolloBrowse";`. The component already has `router` (used by the return link); if not in scope at that point, ensure `const router = useRouter();` appears before the guard.

- [ ] **Step 3: Entry link on the Hoot page**

In `app/page.tsx`, locate the existing "Teach Apollo" button (the JSX that calls `startApollo` — search for `startApollo`). Immediately after that button element, add a sibling:

```tsx
<button
  type="button"
  className="apollo-browse-entry"
  onClick={() => router.push(`/apollo?class=${selectedClassId}`)}
  disabled={!selectedClassId}
>
  Practice with Apollo
</button>
```

(`router` and `selectedClassId` are already in scope — `startApollo` uses both.)

- [ ] **Step 4: Styles**

Append to `app/globals.css` (single-stylesheet convention — BEM like the existing apollo classes):

```css
/* --- Apollo browse (standalone entry, 2026-07-07) ------------------- */
.apollo-browse { max-width: 960px; margin: 0 auto; padding: 24px 16px; }
.apollo-browse__title { font-size: 1.6rem; font-weight: 700; }
.apollo-browse__subtitle { color: var(--muted, #667); margin-top: 4px; }
.apollo-browse__progress-link { display: inline-block; margin-top: 8px; font-size: 0.9rem; }
.apollo-browse__columns { display: flex; gap: 20px; margin-top: 20px; align-items: flex-start; }
.apollo-browse__concepts { display: flex; flex-direction: column; gap: 6px; min-width: 220px; }
.apollo-browse__concept { text-align: left; padding: 10px 12px; border-radius: 10px; border: 1px solid transparent; background: transparent; cursor: pointer; }
.apollo-browse__concept--active { border-color: var(--accent, #4a6cf7); background: rgba(74, 108, 247, 0.08); font-weight: 600; }
.apollo-browse__problems { flex: 1; }
.apollo-browse__difficulties { display: flex; gap: 8px; align-items: center; margin-bottom: 12px; }
.apollo-browse__difficulty { padding: 6px 14px; border-radius: 999px; border: 1px solid var(--border, #dcdce4); background: transparent; cursor: pointer; text-transform: capitalize; }
.apollo-browse__difficulty--active { background: var(--accent, #4a6cf7); color: #fff; border-color: var(--accent, #4a6cf7); }
.apollo-browse__surprise { margin-left: auto; }
.apollo-browse__cards { list-style: none; display: flex; flex-direction: column; gap: 12px; padding: 0; }
.apollo-browse__card { border: 1px solid var(--border, #dcdce4); border-radius: 12px; padding: 14px 16px; }
.apollo-browse__card-text { margin: 0 0 10px; line-height: 1.45; }
.apollo-browse__card-footer { display: flex; align-items: center; gap: 10px; justify-content: flex-end; }
.apollo-browse__tried { font-size: 0.75rem; padding: 2px 8px; border-radius: 999px; background: rgba(0, 0, 0, 0.07); }
.apollo-browse__loading, .apollo-browse__empty { padding: 24px 0; color: var(--muted, #667); }
.apollo-browse-entry { margin-left: 8px; }
```

- [ ] **Step 5: Verify and commit**

Run: `npm run lint && npm run build`
Expected: clean.

Manual smoke (optional now, mandatory in Task 11): `npm run dev`, sign in as the staging test student, visit `http://localhost:3001/apollo?class=<F2 search_space_id>`, confirm concepts render and Start creates a session.

```bash
git add components/apollo/ApolloBrowse.tsx app/apollo/ApolloPageClient.tsx app/page.tsx app/globals.css
git commit -m "feat(apollo): standalone browse screen — concept/difficulty/problem picker"
```

---

### Task 7: Interactive KG panel + touched tracking

**Files:**
- Modify: `components/apollo/ApolloKGPanel.tsx` (new `onEntryTouched` prop)
- Modify: `app/apollo/ApolloPageClient.tsx` (mount with session wiring)

**Interfaces:**
- Consumes: `ApolloKGPanel` props `{kg, sessionId?, pulseEntryId?, onKgUpdated?}` (`components/apollo/ApolloKGPanel.tsx:20-34`); `MaybePill` currently drops the entry argument (`onUpdated={(_, kg) => onUpdated?.(kg)}`).
- Produces: `ApolloKGPanel` gains `onEntryTouched?: (entryId: string) => void`, fired with `entry.node_id` on every successful negotiation move. `ApolloPageClient` holds `touched: Set<string>` + `pulseEntryId: string | null` state — Task 8's done-gate consumes both.

- [ ] **Step 1: Add `onEntryTouched` to the panel**

In `components/apollo/ApolloKGPanel.tsx`, extend the Props interface:

```ts
interface Props {
  kg: ApolloKG;
  sessionId?: number;
  pulseEntryId?: string | null;
  onKgUpdated?: (kg: ApolloKG) => void;
  onEntryTouched?: (entryId: string) => void;
}
```

Thread it through to `MaybePill` (which receives the panel's props — follow the existing plumbing) and change the pill callback from `onUpdated={(_, kg) => onUpdated?.(kg)}` to:

```tsx
onUpdated={(entry, kg) => {
  onEntryTouched?.(entry.node_id);
  onUpdated?.(kg);
}}
```

- [ ] **Step 2: Mount with full wiring in `ApolloPageClient.tsx`**

Add state near the existing `kg` state:

```tsx
  const [pulseEntryId, setPulseEntryId] = useState<string | null>(null);
  const [touched, setTouched] = useState<Set<string>>(new Set());
```

Replace the aside mount `{kg && <ApolloKGPanel kg={kg} />}` with:

```tsx
{kg && (
  <ApolloKGPanel
    kg={kg}
    sessionId={sessionId}
    pulseEntryId={pulseEntryId}
    onKgUpdated={(newKg) => setKg(newKg)}
    onEntryTouched={(id) =>
      setTouched((prev) => {
        const next = new Set(prev);
        next.add(id);
        return next;
      })
    }
  />
)}
```

- [ ] **Step 3: Verify and commit**

Run: `npm run lint && npm run build`
Expected: clean. (`pulseEntryId`/`touched` are consumed in Task 8; if lint flags `pulseEntryId`/`setPulseEntryId`/`touched` as unused, that resolves in Task 8 — commit the two tasks together in that case, otherwise commit now.)

```bash
git add components/apollo/ApolloKGPanel.tsx app/apollo/ApolloPageClient.tsx
git commit -m "feat(apollo): activate KG negotiation pills in the session view + touched-entry tracking"
```

---

### Task 8: Done-gate review flow

**Files:**
- Modify: `app/apollo/ApolloPageClient.tsx` (`handleDone` catch + modal mount + resume banner)
- Modify: `components/apollo/ApolloErrorSurface.tsx` (`review_required` copy)
- Modify: `app/globals.css` (resume-banner class)

**Interfaces:**
- Consumes: 422 body `{"error_code": "review_required", "message": str, "review_required": ReviewRequiredEntry[]}` (backend `apollo/api.py` `review_required_handler`; FE `_handle` puts the whole body in `err.extra`); `DoneGateModal` props `{entries, touched, onJumpTo, onClose, onRetry}` (`components/apollo/DoneGateModal.tsx:14-21`, CSS already in globals.css); Task 7's `touched` + `setPulseEntryId`.
- Produces: the complete done-gate loop — Done → modal → jump-to-entry (modal closes, pill pulses) → negotiation move fills `touched` → floating "Resume review" button reopens modal → all touched → Re-submit Done.

- [ ] **Step 1: Wire `handleDone` and the modal state**

In `app/apollo/ApolloPageClient.tsx`, add imports:

```tsx
import DoneGateModal from "@/components/apollo/DoneGateModal";
import type { ReviewRequiredEntry } from "@/lib/apollo/api";
```

Add state:

```tsx
  const [gateEntries, setGateEntries] = useState<ReviewRequiredEntry[] | null>(null);
  const [gateOpen, setGateOpen] = useState(false);
```

Replace the body of `handleDone` (currently: `finishTeaching` → `setReport`, generic catch) with:

```tsx
  const handleDone = useCallback(async () => {
    setBusy(true);
    setError(null);
    try {
      const r = await finishTeaching(sessionId);
      setReport(r);
      setGateEntries(null);
      setGateOpen(false);
    } catch (e) {
      if (e instanceof ApolloApiError && e.errorCode === "review_required") {
        const entries = (e.extra["review_required"] as ReviewRequiredEntry[]) ?? [];
        setGateEntries(entries);
        setTouched(new Set());
        setGateOpen(true);
      } else {
        setError(e);
      }
    } finally {
      setBusy(false);
    }
  }, [sessionId]);
```

(`ApolloApiError` is already imported in this file via `@/lib/apollo/api` — if not, add it.)

- [ ] **Step 2: Mount the modal + resume banner**

Inside the returned JSX, after the `<ApolloErrorSurface … />` line, add:

```tsx
{gateEntries && gateOpen && (
  <DoneGateModal
    entries={gateEntries}
    touched={touched}
    onJumpTo={(entryId) => {
      setGateOpen(false);
      setPulseEntryId(entryId);
    }}
    onClose={() => setGateOpen(false)}
    onRetry={() => {
      setGateOpen(false);
      void handleDone();
    }}
  />
)}
{gateEntries && !gateOpen && !report && (
  <button
    type="button"
    className="apollo-gate-resume"
    onClick={() => setGateOpen(true)}
  >
    Resume review ({gateEntries.filter((e) => !touched.has(e.entry_id)).length} left)
  </button>
)}
```

Append to `app/globals.css`:

```css
.apollo-gate-resume { position: fixed; bottom: 20px; right: 20px; z-index: 40; padding: 10px 16px; border-radius: 999px; background: var(--accent, #4a6cf7); color: #fff; border: none; cursor: pointer; box-shadow: 0 4px 14px rgba(0, 0, 0, 0.18); }
```

- [ ] **Step 3: `review_required` fallback copy in `ApolloErrorSurface.tsx`**

Add a case to `titleFor`:

```ts
    case "review_required":
      return "A quick review is needed before grading";
```

And to `detailFor`:

```ts
    case "review_required":
      return "Some things Apollo heard need your confirmation. Use the ? ✎ ↩ buttons in “Apollo’s understanding” to review them, then press Done again.";
```

(This is a fallback only — the modal intercepts the error before it reaches the surface; the case guarantees the path can never render "Something went wrong.")

- [ ] **Step 4: Verify and commit**

Run: `npm run lint && npm run build`
Expected: clean.

```bash
git add app/apollo/ApolloPageClient.tsx components/apollo/ApolloErrorSurface.tsx app/globals.css
git commit -m "feat(apollo): done-gate review flow — modal, jump-to-entry pulse, resume banner, error fallback"
```

---

### Task 9: Next problem + Restart controls

**Files:**
- Modify: `components/apollo/ApolloReportPanel.tsx` (`onNext` prop + button)
- Modify: `app/apollo/ApolloPageClient.tsx` (`handleNext`, `handleRestart`, restart button)

**Interfaces:**
- Consumes: `nextProblem(sessionId, difficulty) -> StartSessionResponse`, `restartProblem(sessionId)` from Task 5; `ApolloReportPanel` props `{report, onRetry, onEnd, busy?}` (`components/apollo/ApolloReportPanel.tsx:9-14`); `getSessionState` for post-action refresh.
- Produces: `ApolloReportPanel` props gain `onNext: () => void`; report screen shows "Next problem" (primary), "Teach more and retry", "End session". Session header gains a "Start over" button (confirm-guarded).

- [ ] **Step 1: Add the Next button to the report panel**

In `components/apollo/ApolloReportPanel.tsx`, extend Props:

```ts
interface Props {
  report: DoneResponse;
  onRetry: () => void;
  onEnd: () => void;
  onNext: () => void;
  busy?: boolean;
}
```

In the buttons row (lines ~118-135), add BEFORE the existing "Teach more and retry" button, using the same button classes the existing pair uses (copy the retry button's className):

```tsx
<button type="button" onClick={onNext} disabled={busy}>
  Next problem
</button>
```

Make "Next problem" the visually primary action (give it the class currently on the retry button if the two buttons are styled primary/secondary; demote retry to the secondary class).

- [ ] **Step 2: Handlers + wiring in `ApolloPageClient.tsx`**

Add next to `handleRetry`:

```tsx
  const handleNext = useCallback(async () => {
    setBusy(true);
    setError(null);
    try {
      const difficulty = (state?.problem?.difficulty ?? "intro") as ApolloDifficulty;
      await nextProblem(sessionId, difficulty);
      const fresh = await getSessionState(sessionId);
      setState(fresh);
      setKg(fresh.kg);
      setReport(null);
      setGateEntries(null);
      setTouched(new Set());
      setPulseEntryId(null);
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  }, [sessionId, state]);

  const handleRestart = useCallback(async () => {
    if (!window.confirm("Start this problem over? Apollo forgets everything you taught it for this attempt.")) {
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await restartProblem(sessionId);
      const fresh = await getSessionState(sessionId);
      setState(fresh);
      setKg(fresh.kg);
      setReport(null);
      setGateEntries(null);
      setTouched(new Set());
      setPulseEntryId(null);
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  }, [sessionId]);
```

Extend the api import with `nextProblem, restartProblem` and the type import with `ApolloDifficulty`. Pass `onNext={handleNext}` to `<ApolloReportPanel …>`. In the session (non-report) branch, add a restart control to the nav row that holds the return link:

```tsx
<button
  type="button"
  className="apollo-restart-btn"
  onClick={handleRestart}
  disabled={busy}
>
  Start over
</button>
```

Append to `app/globals.css`:

```css
.apollo-restart-btn { margin-left: auto; padding: 6px 12px; border-radius: 8px; border: 1px solid var(--border, #dcdce4); background: transparent; cursor: pointer; font-size: 0.85rem; }
```

- [ ] **Step 3: Verify and commit**

Run: `npm run lint && npm run build`
Expected: clean.

```bash
git add components/apollo/ApolloReportPanel.tsx app/apollo/ApolloPageClient.tsx app/globals.css
git commit -m "feat(apollo): next-problem and restart controls in the session flow"
```

---

### Task 10: Progress dashboard page

**Files:**
- Create: `app/apollo/progress/page.tsx`
- Create: `app/apollo/progress/ProgressClient.tsx`
- Modify: `app/globals.css`

**Interfaces:**
- Consumes: `getStudentProgressDetailed(searchSpaceId)` → `StudentProgressDetailed` (Task 5); `ApolloProgressCard` (`components/apollo/ApolloProgressCard.tsx`, prop `progress: StudentProgress | null`).
- Produces: `/apollo/progress?class=N` — level/XP header, per-concept mastery bars, recent-attempts list, empty state, back-link to `/apollo?class=N`.

- [ ] **Step 1: Create the page shell**

`app/apollo/progress/page.tsx` (mirrors `app/apollo/page.tsx`'s Suspense pattern — `useSearchParams` requires it):

```tsx
import { Suspense } from "react";
import ProgressClient from "./ProgressClient";

export default function ApolloProgressPage() {
  return (
    <Suspense fallback={<main style={{ padding: 24 }}>Loading progress…</main>}>
      <ProgressClient />
    </Suspense>
  );
}
```

- [ ] **Step 2: Create the client component**

`app/apollo/progress/ProgressClient.tsx`:

```tsx
"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import {
  StudentProgressDetailed,
  getStudentProgressDetailed,
} from "@/lib/apollo/api";
import ApolloProgressCard from "@/components/apollo/ApolloProgressCard";
import ApolloErrorSurface from "@/components/apollo/ApolloErrorSurface";

export default function ProgressClient() {
  const searchParams = useSearchParams();
  const classId = Number(searchParams.get("class"));
  const [data, setData] = useState<StudentProgressDetailed | null>(null);
  const [error, setError] = useState<unknown>(null);

  useEffect(() => {
    if (!classId) return;
    getStudentProgressDetailed(classId)
      .then(setData)
      .catch(setError);
  }, [classId]);

  if (!classId) {
    return (
      <main className="apollo-progress-page">
        <p>Open your progress from the Apollo page so we know which course you&apos;re in.</p>
      </main>
    );
  }

  const detail = data?.detail;
  const isEmpty =
    detail !== undefined &&
    detail.mastery.length === 0 &&
    detail.recent_attempts.length === 0;

  return (
    <main className="apollo-progress-page">
      <nav className="apollo-page__nav">
        <Link href={`/apollo?class=${classId}`}>← Back to Apollo</Link>
      </nav>
      <h1 className="apollo-progress-page__title">My progress</h1>
      <ApolloErrorSurface error={error} onDismiss={() => setError(null)} />
      <ApolloProgressCard progress={data} />

      {isEmpty && (
        <div className="apollo-progress-page__empty">
          Nothing here yet — teach Apollo your first problem and your progress
          will show up.
        </div>
      )}

      {detail && detail.mastery.length > 0 && (
        <section className="apollo-progress-page__section">
          <h2>Concept mastery</h2>
          <ul className="apollo-mastery">
            {detail.mastery.map((m) => (
              <li key={m.concept_id} className="apollo-mastery__row">
                <span className="apollo-mastery__name">{m.display_name}</span>
                <span className="apollo-mastery__bar">
                  <span
                    className="apollo-mastery__fill"
                    style={{ width: `${Math.round(m.mastery_avg * 100)}%` }}
                  />
                </span>
                <span className="apollo-mastery__pct">
                  {Math.round(m.mastery_avg * 100)}%
                </span>
              </li>
            ))}
          </ul>
        </section>
      )}

      {detail && detail.recent_attempts.length > 0 && (
        <section className="apollo-progress-page__section">
          <h2>Recent attempts</h2>
          <ul className="apollo-attempts">
            {detail.recent_attempts.map((a) => (
              <li key={a.attempt_id} className="apollo-attempts__row">
                <span className="apollo-attempts__concept">
                  {a.concept_display_name ?? "—"}
                </span>
                <span className="apollo-attempts__difficulty">{a.difficulty}</span>
                <span className="apollo-attempts__grade">
                  {a.letter ?? "?"}{a.score !== null ? ` (${a.score})` : ""}
                </span>
                <span className="apollo-attempts__date">
                  {new Date(a.created_at).toLocaleDateString()}
                </span>
              </li>
            ))}
          </ul>
        </section>
      )}
    </main>
  );
}
```

- [ ] **Step 3: Styles**

Append to `app/globals.css`:

```css
/* --- Apollo progress dashboard --------------------------------------- */
.apollo-progress-page { max-width: 720px; margin: 0 auto; padding: 24px 16px; }
.apollo-progress-page__title { font-size: 1.4rem; font-weight: 700; margin: 12px 0; }
.apollo-progress-page__section { margin-top: 28px; }
.apollo-progress-page__empty { margin-top: 24px; color: var(--muted, #667); }
.apollo-mastery { list-style: none; padding: 0; display: flex; flex-direction: column; gap: 10px; }
.apollo-mastery__row { display: flex; align-items: center; gap: 12px; }
.apollo-mastery__name { flex: 0 0 220px; }
.apollo-mastery__bar { flex: 1; height: 10px; border-radius: 999px; background: rgba(0, 0, 0, 0.08); overflow: hidden; }
.apollo-mastery__fill { display: block; height: 100%; border-radius: 999px; background: var(--accent, #4a6cf7); }
.apollo-mastery__pct { flex: 0 0 48px; text-align: right; font-variant-numeric: tabular-nums; }
.apollo-attempts { list-style: none; padding: 0; display: flex; flex-direction: column; gap: 8px; }
.apollo-attempts__row { display: flex; gap: 12px; align-items: baseline; padding: 8px 0; border-bottom: 1px solid var(--border, #dcdce4); }
.apollo-attempts__concept { flex: 1; }
.apollo-attempts__difficulty { text-transform: capitalize; color: var(--muted, #667); }
.apollo-attempts__grade { font-weight: 600; }
.apollo-attempts__date { color: var(--muted, #667); font-size: 0.85rem; }
```

Note: `ApolloProgressCard` takes `StudentProgress | null` — `StudentProgressDetailed` extends it, so passing `data` type-checks. If the build complains about the extra `detail` key, pass `progress={data}` unchanged (structural typing accepts supersets).

- [ ] **Step 4: Verify and commit**

Run: `npm run lint && npm run build`
Expected: clean.

```bash
git add app/apollo/progress/ app/globals.css
git commit -m "feat(apollo): student progress dashboard — concept mastery + recent attempts"
```

---

# Part C — Ship + verify (ops)

### Task 11: Phase-1 staging E2E (existing F2 content)

No code files — this is the verification gate for Parts A+B. Prerequisites: backend branch deployed to a staging service (or run reviewable via PR preview), UI `npm run dev` pointing `AI_TA_API_BASE_URL` at the staging backend.

- [ ] **Step 1: Push branches, open both PRs**

```bash
cd /Users/ishaanbatra/Documents/GitHub/ai-ta-backend
git push -u origin weekend/apollo-student-e2e
gh pr create --base staging --title "feat(apollo): standalone student entry — browse endpoints, POST /sessions, progress detail" --body "Implements the backend half of docs/superpowers/specs/2026-07-07-apollo-student-e2e-design.md ..."

cd /Users/ishaanbatra/Documents/GitHub/ai-ta-student-ui
git push -u origin apollo-e2e-baseline
gh pr create --base main --title "feat(apollo): standalone Apollo section — browse, done-gate flow, next/restart, progress dashboard"
```

Report both PR URLs + CI status. STOP — Ishaan merges. Do not push further commits to these branches after announcing them.

- [ ] **Step 2: After merge + staging deploy — set the flags**

On Railway staging, on the backend web service (ai-ta-* prefixed service in the hoot-ai-ta project):

```
APOLLO_DONE_GATE_ENABLED=1
APOLLO_GRADING_ARTIFACT_ENABLED=1
```

Confirm no other Apollo flags were introduced (clarification/shadow/NLI/emergent stay unset).

- [ ] **Step 3: Manual E2E checklist (test student on the F2 linear-motion course)**

1. Sign in as the test student, open `/apollo?class=<F2 search_space_id>` — concepts render.
2. Switch difficulty tabs — problem lists change; a previously attempted problem shows "tried".
3. "Start teaching" on a specific problem — session opens on that exact problem; "Surprise me" also works.
4. Teach 2–3 sentences — KG panel fills; pills show `?  ✎  ↩  …` buttons (interactive), trace opens.
5. Challenge one entry (mark it disputed), then click "I'm done teaching" — DoneGateModal appears listing the disputed entry; "Re-submit Done" disabled.
6. Jump to entry — modal closes, pill pulses/scrolls; make a move (paraphrase or skip); "Resume review (0 left)" → modal → Re-submit Done — grading proceeds.
7. Report shows rubric + narrative + XP; "Next problem" loads a fresh problem in the same session; teach + Done again (no disputes) — grades directly.
8. "Start over" (confirm dialog) resets the current problem attempt.
9. `/apollo/progress?class=N` — mastery bars nonzero (artifact flag was on for the graded attempts), recent attempts listed newest-first.
10. Session-expiry sanity: open `/apollo?class=N` again — Browse shows (old session was auto-ended by the new-session entry in step 3's semantics; no dead end).

Any failure: fix on a NEW branch/PR (no accumulator pushes to announced PRs).

### Task 12: Phase-2/3 — new course content + onboarding (when materials arrive)

- [ ] **Step 1: Corpus check** — if any materials are scans/handwriting, set `OCR_PROVIDER=openai` on the staging teacher-upload worker service BEFORE uploading.
- [ ] **Step 2: Teacher upload** — create the new course + upload materials through the teacher UI; watch the teacher-upload worker logs until indexing completes.
- [ ] **Step 3: Provisioning run** — confirm no provisioning worker holds a lease (single-drainer rule: check `apollo_provision_*` lease state / Railway worker replicas before any manual drain), then run provisioning for the new `search_space_id`. Spot-check: `GET /apollo/concepts?search_space_id=<new>` returns concepts; problem counts per concept ≥ 3; read 2–3 promoted `problem_text`s for quality.
- [ ] **Step 4: Thin-concept fallback** — concepts with < 2 teachable problems: upload more source material and re-run, or accept launching without them.
- [ ] **Step 5: Invite link** — teacher UI → create student invite link for the new course; redeem in a clean browser with a brand-new account via `/join/<code>`; run the full Task 11 Step-3 checklist once on the new course.
- [ ] **Step 6: Launch** — hand invite links to students. Monitor Railway backend logs + Supabase `apollo_sessions` / `apollo_problem_attempts` for the first sessions.

---

## Plan self-review notes (kept for the executor)

- Spec coverage: concepts/problems/sessions endpoints (Tasks 1–3), progress detail (4), proxy+client (5), browse (6), KG interactivity as done-gate dependency (7), done-gate flow (8), next/restart (9), dashboard (10), flags+Phase-1 E2E (11), Phases 2–3 (12). Parked items from the spec are absent by design.
- The from_hoot refactor (Task 3) is the only change to existing backend behavior; its regression gate is the existing hoot_bridge + api_auth suites.
- `attempted` flag and recent-attempts read `ProblemAttempt` via `ApolloSession` — no dependency on the artifact flag; mastery bars DO depend on `APOLLO_GRADING_ARTIFACT_ENABLED=1` (set in Task 11 before students arrive; earlier graded attempts won't backfill mastery).
- UI has no test framework (verified) — `npm run lint && npm run build` is the automated gate; Task 11 Step 3 is the behavioral gate.
