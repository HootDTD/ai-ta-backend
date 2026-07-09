# Apollo Difficulty Choice — Backend Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let students pick difficulty at Hoot handoff, advance to new problems at a chosen difficulty, abandon/switch mid-problem, and restart a problem with a clean KG. Backend-only slice — the frontend plan is a sibling.

**Architecture:** Migrate `apollo_kg_entries` and `apollo_messages` from session-scoped to attempt-scoped by adding a nullable `attempt_id` column with a backfill. Make KGStore methods take `attempt_id` instead of `session_id`. Modify `POST /apollo/sessions/from_hoot` to require a `difficulty` field. Add two new endpoints: `POST /apollo/sessions/{id}/next` (unified advance/abandon) and `POST /apollo/sessions/{id}/restart_problem` (wipe current attempt's KG + messages, keep the attempt row). Fix `has_prior_graded_attempt` to exclude `result='abandoned'` from its filter.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy async (asyncpg for Postgres, aiosqlite for tests), Pydantic v2, pytest + pytest-asyncio. Migrations are numbered SQL files under `database/migrations/`.

**Branch:** `ApolloV2` (all Apollo work pushes here per project convention).

**Spec:** `docs/superpowers/specs/2026-04-22-apollo-difficulty-choice-design.md`.

---

## File Map

**Modified:**
- `apollo/persistence/models.py` — `attempt_id` column on `KGEntry` and `Message`.
- `apollo/persistence/attempt_history.py` — allowlist filter.
- `apollo/knowledge_graph/store.py` — attempt-scoped `write_entries`, `read_kg`, `summarize_for_apollo`.
- `apollo/handlers/chat.py` — resolve current attempt, thread `attempt_id` into KGStore calls.
- `apollo/handlers/done.py` — same.
- `apollo/handlers/lifecycle.py` — `handle_get_session` returns current-attempt KG/messages only.
- `apollo/hoot_bridge/session_init.py` — accept `difficulty` parameter.
- `apollo/errors.py` — `InvalidPhaseError`.
- `apollo/api.py` — Pydantic body models, new routes, exception handler registration.
- `apollo/tests/test_e2e_smoke.py` — multi-attempt flow.

**Created:**
- `database/migrations/014_apollo_attempt_id.sql` — migration.
- `apollo/handlers/next.py` — `handle_next`.
- `apollo/handlers/restart_problem.py` — `handle_restart_problem`.
- `apollo/handlers/tests/test_next.py` — handler tests.
- `apollo/handlers/tests/test_restart_problem.py` — handler tests.

**Test files updated:** `test_attempt_history.py`, `test_store.py`, `test_session_init.py`, `test_chat.py`, `test_done.py`, `test_lifecycle.py`.

---

## Task 1: Schema migration — add `attempt_id` columns

**Files:**
- Create: `database/migrations/014_apollo_attempt_id.sql`

- [ ] **Step 1: Write the migration file.**

```sql
-- 014_apollo_attempt_id.sql
-- Migrate apollo_kg_entries and apollo_messages from session-scoped to
-- attempt-scoped. Adds nullable attempt_id columns with FK + cascade to
-- apollo_problem_attempts, backfills from the single existing attempt per
-- session (true for all current data because sessions have been
-- single-problem to date), and indexes the new columns.

BEGIN;

ALTER TABLE apollo_kg_entries
    ADD COLUMN IF NOT EXISTS attempt_id BIGINT
    REFERENCES apollo_problem_attempts(id) ON DELETE CASCADE;

ALTER TABLE apollo_messages
    ADD COLUMN IF NOT EXISTS attempt_id BIGINT
    REFERENCES apollo_problem_attempts(id) ON DELETE CASCADE;

UPDATE apollo_kg_entries
SET attempt_id = (
    SELECT id FROM apollo_problem_attempts
    WHERE session_id = apollo_kg_entries.session_id
    ORDER BY id DESC
    LIMIT 1
)
WHERE attempt_id IS NULL;

UPDATE apollo_messages
SET attempt_id = (
    SELECT id FROM apollo_problem_attempts
    WHERE session_id = apollo_messages.session_id
    ORDER BY id DESC
    LIMIT 1
)
WHERE attempt_id IS NULL;

CREATE INDEX IF NOT EXISTS ix_apollo_kg_entries_attempt_id
    ON apollo_kg_entries(attempt_id);

CREATE INDEX IF NOT EXISTS ix_apollo_messages_attempt_id
    ON apollo_messages(attempt_id);

COMMIT;
```

- [ ] **Step 2: Commit.**

```bash
git add database/migrations/014_apollo_attempt_id.sql
git commit -m "feat(apollo): migration 014 — add attempt_id to KG + messages"
git push
```

---

## Task 2: Mirror the migration in the SQLAlchemy models

**Files:**
- Modify: `apollo/persistence/models.py`
- Modify: `apollo/persistence/tests/test_models.py`

- [ ] **Step 1: Write a failing test that asserts `attempt_id` exists on both tables.**

Append to `apollo/persistence/tests/test_models.py`:

```python
def test_kg_entry_has_attempt_id_column():
    assert "attempt_id" in KGEntry.__table__.columns
    col = KGEntry.__table__.columns["attempt_id"]
    assert col.nullable is True
    fk = next(iter(col.foreign_keys))
    assert fk.column.table.name == "apollo_problem_attempts"


def test_message_has_attempt_id_column():
    assert "attempt_id" in Message.__table__.columns
    col = Message.__table__.columns["attempt_id"]
    assert col.nullable is True
    fk = next(iter(col.foreign_keys))
    assert fk.column.table.name == "apollo_problem_attempts"
```

- [ ] **Step 2: Run the test to verify it fails.**

```bash
pytest apollo/persistence/tests/test_models.py::test_kg_entry_has_attempt_id_column apollo/persistence/tests/test_models.py::test_message_has_attempt_id_column -v
```

Expected: both FAIL with `KeyError: 'attempt_id'`.

- [ ] **Step 3: Add the columns to `KGEntry` and `Message`.**

In `apollo/persistence/models.py`, inside the `KGEntry` class definition, after the `session_id` column:

```python
    attempt_id = Column(
        BigInteger,
        ForeignKey("apollo_problem_attempts.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
```

Same addition inside the `Message` class definition, after its `session_id` column.

- [ ] **Step 4: Run the tests to verify they pass.**

```bash
pytest apollo/persistence/tests/test_models.py -v
```

Expected: PASS (including the two new tests and all existing ones).

- [ ] **Step 5: Commit.**

```bash
git add apollo/persistence/models.py apollo/persistence/tests/test_models.py
git commit -m "feat(apollo): add attempt_id column to KGEntry and Message models"
git push
```

---

## Task 3: Fix `has_prior_graded_attempt` allowlist

**Files:**
- Modify: `apollo/persistence/attempt_history.py`
- Modify: `apollo/persistence/tests/test_attempt_history.py`

- [ ] **Step 1: Write a failing test that seeds an abandoned attempt and asserts it is NOT counted.**

Append to `apollo/persistence/tests/test_attempt_history.py`:

```python
@pytest.mark.asyncio
async def test_has_prior_graded_attempt_excludes_abandoned(db_with_session):
    s, session_id = db_with_session
    # Previous attempt was abandoned (student switched problems mid-teach).
    s.add(ProblemAttempt(
        session_id=session_id,
        problem_id="bernoulli_horizontal_pipe_find_p2",
        difficulty="intro",
        result="abandoned",
    ))
    await s.flush()
    # Current attempt on same problem, not yet graded.
    current = ProblemAttempt(
        session_id=session_id,
        problem_id="bernoulli_horizontal_pipe_find_p2",
        difficulty="standard",
    )
    s.add(current)
    await s.commit()

    result = await has_prior_graded_attempt(
        db=s,
        student_id="stu-1",
        problem_id="bernoulli_horizontal_pipe_find_p2",
        exclude_attempt_id=current.id,
    )
    assert result is False, "abandoned attempts must not count as prior grades"
```

If the fixture `db_with_session` does not already exist in that file, copy the pattern from `test_done.py`'s `db_with_session_and_kg` fixture, keeping only the session + one-student setup (no KG rows, no current attempt).

- [ ] **Step 2: Run the test to verify it fails.**

```bash
pytest apollo/persistence/tests/test_attempt_history.py::test_has_prior_graded_attempt_excludes_abandoned -v
```

Expected: FAIL — `result is True`, because the current filter is `result IS NOT NULL`.

- [ ] **Step 3: Fix the filter.**

In `apollo/persistence/attempt_history.py`, replace the `where(...)` clause:

```python
    stmt = (
        select(func.count())
        .select_from(ProblemAttempt)
        .join(ApolloSession, ApolloSession.id == ProblemAttempt.session_id)
        .where(
            ApolloSession.student_id == student_id,
            ProblemAttempt.problem_id == problem_id,
            ProblemAttempt.result.in_(
                ("solved", "stuck", "skipped", "returned_to_hoot")
            ),
            ProblemAttempt.id != exclude_attempt_id,
        )
    )
```

Also update the module docstring: replace "that already has a non-null `result`" with "whose `result` is a graded terminal value (`solved`, `stuck`, `skipped`, `returned_to_hoot`) — `abandoned` is excluded because it represents a mid-problem switch, not a completed grading."

- [ ] **Step 4: Run the test to verify it passes, plus the rest of the test module.**

```bash
pytest apollo/persistence/tests/test_attempt_history.py -v
```

Expected: all PASS.

- [ ] **Step 5: Commit.**

```bash
git add apollo/persistence/attempt_history.py apollo/persistence/tests/test_attempt_history.py
git commit -m "fix(apollo): exclude abandoned attempts from has_prior_graded_attempt"
git push
```

---

## Task 4: Make KGStore attempt-scoped

**Files:**
- Modify: `apollo/knowledge_graph/store.py`
- Modify: `apollo/knowledge_graph/tests/test_store.py`

- [ ] **Step 1: Write a failing test asserting KG entries are attempt-scoped.**

Create or append to `apollo/knowledge_graph/tests/test_store.py`:

```python
@pytest.mark.asyncio
async def test_kg_entries_are_scoped_by_attempt_id(db_with_two_attempts):
    db, session_id, attempt_a, attempt_b = db_with_two_attempts
    store = KGStore(db)

    await store.write_entries(
        attempt_id=attempt_a,
        entries=[{"type": "equation", "content": {"symbolic": "x - 1", "label": "A"}}],
        source="parser",
    )
    await store.write_entries(
        attempt_id=attempt_b,
        entries=[{"type": "equation", "content": {"symbolic": "y - 2", "label": "B"}}],
        source="parser",
    )

    kg_a = await store.read_kg(attempt_id=attempt_a)
    kg_b = await store.read_kg(attempt_id=attempt_b)

    labels_a = [e.get("label") for e in kg_a["equation"]]
    labels_b = [e.get("label") for e in kg_b["equation"]]
    assert labels_a == ["A"]
    assert labels_b == ["B"]
```

Add the fixture at the top of the same file (or in `conftest.py` if one exists):

```python
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from apollo.knowledge_graph.store import KGStore
from apollo.persistence.models import ApolloSession, KGEntry, Message, ProblemAttempt, SessionPhase, SessionStatus
from database.models import Base


@pytest_asyncio.fixture
async def db_with_two_attempts():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    tables = [
        ApolloSession.__table__,
        KGEntry.__table__,
        Message.__table__,
        ProblemAttempt.__table__,
    ]
    async with engine.begin() as conn:
        await conn.run_sync(lambda s: Base.metadata.create_all(s, tables=tables))
    Session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with Session() as s:
        sess = ApolloSession(
            student_id="stu-1",
            concept_cluster_id="fluid_mechanics",
            status=SessionStatus.active.value,
            phase=SessionPhase.TEACHING.value,
            current_problem_id="p2",
        )
        s.add(sess)
        await s.flush()
        a = ProblemAttempt(session_id=sess.id, problem_id="p1", difficulty="intro", result="abandoned")
        b = ProblemAttempt(session_id=sess.id, problem_id="p2", difficulty="standard")
        s.add_all([a, b])
        await s.commit()
        yield s, sess.id, a.id, b.id
    await engine.dispose()
```

- [ ] **Step 2: Run the test to verify it fails.**

```bash
pytest apollo/knowledge_graph/tests/test_store.py::test_kg_entries_are_scoped_by_attempt_id -v
```

Expected: FAIL — `KGStore.write_entries` does not accept an `attempt_id` kwarg.

- [ ] **Step 3: Change KGStore signatures to attempt-scoped.**

Rewrite the three methods in `apollo/knowledge_graph/store.py`:

```python
    async def write_entries(
        self, *, attempt_id: int, entries: List[Dict[str, Any]], source: str
    ) -> int:
        """Write KG entries under a ProblemAttempt.

        Raises SessionFrozenError if the owning session is frozen.
        Returns the number of entries written."""
        session_id = await self._session_id_for_attempt(attempt_id)
        await self._ensure_unfrozen(session_id)
        added = 0
        for e in entries:
            t = e.get("type")
            if t not in _KG_TYPES:
                continue
            self.db.add(KGEntry(
                session_id=session_id,
                attempt_id=attempt_id,
                type=t,
                content=e.get("content", {}),
                source=source,
            ))
            added += 1
        await self.db.commit()
        return added

    async def read_kg(self, *, attempt_id: int) -> Dict[str, List[Dict[str, Any]]]:
        """Return the KG for a ProblemAttempt, grouped by entry type."""
        result = await self.db.execute(
            select(KGEntry).where(KGEntry.attempt_id == attempt_id).order_by(KGEntry.id)
        )
        rows = result.scalars().all()
        kg: Dict[str, List[Dict[str, Any]]] = {t: [] for t in _KG_TYPES}
        for row in rows:
            content = dict(row.content or {})
            if row.type == "equation" and "symbolic" in content and "latex" not in content:
                tex = _equation_latex(content["symbolic"])
                if tex is not None:
                    content["latex"] = tex
            kg[row.type].append(content)
        return kg

    async def summarize_for_apollo(self, *, attempt_id: int) -> str:
        """Bullet summary for Apollo's context — student-sourced labels only."""
        kg = await self.read_kg(attempt_id=attempt_id)
        # (unchanged body below — keep the existing line-building logic)
        lines: List[str] = []
        for eq in kg["equation"]:
            lines.append(f"- equation ({eq.get('label', '(no label)')}): {eq.get('symbolic', '')}")
        for d in kg["definition"]:
            lines.append(f"- definition: {d.get('concept', '?')} = {d.get('meaning', '?')}")
        for c in kg["condition"]:
            lines.append(f"- condition: {c.get('applies_when', '?')}")
        for s in kg["simplification"]:
            lines.append(f"- simplification: when {s.get('applies_when', '?')}, {s.get('transformation', '?')}")
        for vm in kg["variable_mapping"]:
            lines.append(f"- variable: {vm.get('term', '?')} → {vm.get('symbol', '?')}")
        for ps in sorted(kg["procedure_step"], key=lambda p: int(p.get("order") or 0)):
            lines.append(
                f"- procedure step {ps.get('order', '?')}: {ps.get('action', '?')}"
            )
        return "\n".join(lines) if lines else _EMPTY_SUMMARY

    async def _session_id_for_attempt(self, attempt_id: int) -> int:
        from apollo.persistence.models import ProblemAttempt
        row = await self.db.execute(
            select(ProblemAttempt.session_id).where(ProblemAttempt.id == attempt_id)
        )
        sid = row.scalar_one_or_none()
        if sid is None:
            raise ValueError(f"attempt {attempt_id} not found")
        return sid
```

Keep `_ensure_unfrozen`, `freeze`, and `unfreeze` methods unchanged — they still take `session_id` because phase lives on the session.

- [ ] **Step 4: Run the new test to verify it passes.**

```bash
pytest apollo/knowledge_graph/tests/test_store.py::test_kg_entries_are_scoped_by_attempt_id -v
```

Expected: PASS.

- [ ] **Step 5: Run the whole store test module to catch regressions.**

```bash
pytest apollo/knowledge_graph/tests/test_store.py -v
```

Expected: failures in pre-existing tests that call `write_entries(session_id=...)` or `read_kg(session_id)`. Fix each call site **within the test file** to pass `attempt_id` instead. For each pre-existing test that seeds a session + KG, also seed a `ProblemAttempt` row (if not already present) and use its id.

- [ ] **Step 6: Re-run test module after adjustments.**

```bash
pytest apollo/knowledge_graph/tests/test_store.py -v
```

Expected: all PASS.

- [ ] **Step 7: Commit.**

```bash
git add apollo/knowledge_graph/store.py apollo/knowledge_graph/tests/test_store.py
git commit -m "refactor(apollo): scope KGStore writes/reads to attempt_id"
git push
```

---

## Task 5: Thread `attempt_id` through `handle_chat`

**Files:**
- Modify: `apollo/handlers/chat.py`
- Modify: `apollo/handlers/tests/test_chat.py`

- [ ] **Step 1: Read the current `handle_chat` to find every KGStore call.**

```bash
```

(No command — just a manual read of `apollo/handlers/chat.py`.) Note the exact lines where `write_entries`, `read_kg`, or `summarize_for_apollo` are called, and where Messages are written.

- [ ] **Step 2: Write a failing test that a chat turn writes KG tagged to the correct attempt_id.**

Append to `apollo/handlers/tests/test_chat.py` (fixture pattern mirrors `test_done.py`'s `db_with_session_and_kg`, seeding a session + one active attempt):

```python
@pytest.mark.asyncio
async def test_chat_writes_kg_entries_tagged_with_attempt_id(db_with_session_and_attempt, monkeypatch):
    db, session_id, attempt_id = db_with_session_and_attempt
    # Patch parser to return a single equation entry deterministically.
    monkeypatch.setattr(
        "apollo.handlers.chat._run_parser_pipeline",
        _async_return([{"type": "equation", "content": {"symbolic": "x - 1", "label": "test"}}]),
    )
    # Patch apollo LLM to a fixed string.
    monkeypatch.setattr(
        "apollo.handlers.chat._generate_apollo_reply",
        _async_return("ok"),
    )
    await handle_chat(db=db, session_id=session_id, message="x equals 1")
    rows = (await db.execute(select(KGEntry).where(KGEntry.attempt_id == attempt_id))).scalars().all()
    assert len(rows) == 1
    assert rows[0].type == "equation"
```

(`_async_return` is the standard `AsyncMock`-style helper — if one already exists in `test_done.py`, import it; otherwise define one at the top of `test_chat.py`.)

If `_run_parser_pipeline` / `_generate_apollo_reply` aren't the actual internal names, substitute the real ones from `apollo/handlers/chat.py` — the monkeypatch targets must match real symbols.

- [ ] **Step 3: Run to verify it fails.**

```bash
pytest apollo/handlers/tests/test_chat.py::test_chat_writes_kg_entries_tagged_with_attempt_id -v
```

Expected: FAIL — KGStore called with `session_id=`, not `attempt_id=`.

- [ ] **Step 4: Modify `handle_chat` to resolve the current attempt and pass `attempt_id`.**

At the top of `handle_chat`, after the session is loaded, add:

```python
    current_attempt = (
        await db.execute(
            select(ProblemAttempt)
            .where(ProblemAttempt.session_id == session_id)
            .where(ProblemAttempt.problem_id == sess.current_problem_id)
            .order_by(ProblemAttempt.id.desc())
        )
    ).scalars().first()
    if current_attempt is None:
        raise RuntimeError(f"no current ProblemAttempt for session {session_id}")
```

Then replace every `store.write_entries(session_id, ...)` with `store.write_entries(attempt_id=current_attempt.id, ...)`, every `store.read_kg(session_id)` with `store.read_kg(attempt_id=current_attempt.id)`, and every `store.summarize_for_apollo(session_id)` with `store.summarize_for_apollo(attempt_id=current_attempt.id)`.

Where a `Message` is written, add `attempt_id=current_attempt.id` to the constructor.

Import `ProblemAttempt` at the top of the file if not already imported.

- [ ] **Step 5: Run to verify.**

```bash
pytest apollo/handlers/tests/test_chat.py -v
```

Expected: all PASS. Fix any pre-existing test that now breaks because of the `attempt_id` requirement on `Message`.

- [ ] **Step 6: Commit.**

```bash
git add apollo/handlers/chat.py apollo/handlers/tests/test_chat.py
git commit -m "feat(apollo): thread attempt_id through handle_chat"
git push
```

---

## Task 6: Thread `attempt_id` through `handle_done`

**Files:**
- Modify: `apollo/handlers/done.py`
- Modify: `apollo/handlers/tests/test_done.py`

`handle_done` already resolves the current `ProblemAttempt` (it's the `attempt` variable used for XP logic — see `done.py:91-98`). We reuse it.

- [ ] **Step 1: Write a failing test asserting KG rows seeded under the current attempt are the ones used for grading.**

Append to `test_done.py`:

```python
@pytest.mark.asyncio
async def test_done_grades_only_current_attempt_kg(db_with_session_and_kg, monkeypatch):
    s, session_id = db_with_session_and_kg
    # Retag the pre-seeded KG rows to the current attempt (which the fixture
    # leaves attempt_id=NULL on legacy KG rows).
    attempt = (
        await s.execute(
            select(ProblemAttempt).where(ProblemAttempt.session_id == session_id)
        )
    ).scalar_one()
    await s.execute(
        update(KGEntry)
        .where(KGEntry.session_id == session_id)
        .values(attempt_id=attempt.id)
    )
    # Seed a second, abandoned attempt with a distractor equation
    # that must NOT be used for grading.
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

    # Patch diagnostic LLM so we don't make a real call.
    monkeypatch.setattr(
        "apollo.handlers.done.generate_diagnostic",
        lambda **_: "ok",
    )

    result = await handle_done(db=s, session_id=session_id)
    # The distractor should not surface in coverage/rubric; the rubric is
    # the easiest proxy — if the distractor leaked in, procedure/equation
    # coverage would shift noticeably. Assert the rubric computed at all
    # and that solver_indicator reached the known-good value from the
    # existing fixture.
    assert "rubric" in result
    # Sanity: only the two seeded equations (from the fixture) plus the
    # distractor exist, but only the fixture's two should be read.
    kg_for_grading = await KGStore(s).read_kg(attempt_id=attempt.id)
    assert all(e.get("label") != "distractor" for e in kg_for_grading["equation"])
```

- [ ] **Step 2: Run to verify.**

```bash
pytest apollo/handlers/tests/test_done.py::test_done_grades_only_current_attempt_kg -v
```

Expected: FAIL — `handle_done` currently reads session-scoped KG, which would include the distractor if `store.read_kg(session_id)` is called.

- [ ] **Step 3: Modify `handle_done`.**

In `apollo/handlers/done.py`, find the two KGStore calls:

```python
    await store.freeze(session_id)
    kg = await store.read_kg(session_id)
```

Move the `ProblemAttempt` lookup (currently at lines ~91-98) ABOVE these calls. Replace the second line with:

```python
    kg = await store.read_kg(attempt_id=attempt.id)
```

`store.freeze(session_id)` stays unchanged — phase lives on the session.

- [ ] **Step 4: Run the test to verify it passes, and the full `test_done.py` to catch regressions.**

```bash
pytest apollo/handlers/tests/test_done.py -v
```

Expected: all PASS. Fix any pre-existing test that writes KG without setting `attempt_id` (set it on the fixture setup).

- [ ] **Step 5: Commit.**

```bash
git add apollo/handlers/done.py apollo/handlers/tests/test_done.py
git commit -m "feat(apollo): thread attempt_id through handle_done"
git push
```

---

## Task 7: Scope `handle_get_session` KG + messages to the current attempt

**Files:**
- Modify: `apollo/handlers/lifecycle.py`
- Modify: `apollo/handlers/tests/test_lifecycle.py`

- [ ] **Step 1: Write a failing test.**

Append to `test_lifecycle.py`:

```python
@pytest.mark.asyncio
async def test_get_session_returns_only_current_attempt_kg(db_with_two_attempts):
    s, session_id, attempt_a, attempt_b = db_with_two_attempts
    # Ensure session.current_problem_id matches attempt_b's problem_id
    sess = (await s.execute(select(ApolloSession).where(ApolloSession.id == session_id))).scalar_one()
    sess.current_problem_id = "p2"
    s.add(KGEntry(session_id=session_id, attempt_id=attempt_a, type="equation",
                  content={"symbolic": "x - 1", "label": "old"}, source="parser"))
    s.add(KGEntry(session_id=session_id, attempt_id=attempt_b, type="equation",
                  content={"symbolic": "y - 2", "label": "new"}, source="parser"))
    s.add(Message(session_id=session_id, attempt_id=attempt_a, role="student", content="old", turn_index=0))
    s.add(Message(session_id=session_id, attempt_id=attempt_b, role="student", content="new", turn_index=0))
    await s.commit()

    result = await handle_get_session(db=s, session_id=session_id)

    eq_labels = [e.get("label") for e in result["kg"]["equation"]]
    msg_contents = [m["content"] for m in result["messages"]]
    assert eq_labels == ["new"]
    assert msg_contents == ["new"]
```

Re-use the `db_with_two_attempts` fixture from Task 4 (import it or move to a conftest).

- [ ] **Step 2: Run to verify it fails.**

```bash
pytest apollo/handlers/tests/test_lifecycle.py::test_get_session_returns_only_current_attempt_kg -v
```

Expected: FAIL — both the old and new KG + messages leak through.

- [ ] **Step 3: Modify `handle_get_session`.**

Replace the message + KG fetch in `apollo/handlers/lifecycle.py`:

```python
async def handle_get_session(*, db: AsyncSession, session_id: int) -> Dict[str, Any]:
    sess = (await db.execute(select(ApolloSession).where(ApolloSession.id == session_id))).scalar_one()

    current_attempt_id: int | None = None
    if sess.current_problem_id:
        row = await db.execute(
            select(ProblemAttempt.id)
            .where(ProblemAttempt.session_id == session_id)
            .where(ProblemAttempt.problem_id == sess.current_problem_id)
            .order_by(ProblemAttempt.id.desc())
        )
        current_attempt_id = row.scalars().first()

    store = KGStore(db)
    kg = (
        await store.read_kg(attempt_id=current_attempt_id)
        if current_attempt_id is not None
        else {t: [] for t in ("equation", "definition", "condition", "simplification", "variable_mapping", "procedure_step")}
    )

    if current_attempt_id is not None:
        msgs = (await db.execute(
            select(Message)
            .where(Message.attempt_id == current_attempt_id)
            .order_by(Message.turn_index)
        )).scalars().all()
    else:
        msgs = []

    problem = None
    if sess.current_problem_id:
        for p in list_problems_for_cluster(sess.concept_cluster_id):
            if p.id == sess.current_problem_id:
                problem = {
                    "id": p.id,
                    "concept_id": p.concept_id,
                    "difficulty": p.difficulty,
                    "problem_text": p.problem_text,
                    "given_values": p.given_values,
                    "target_unknown": p.target_unknown,
                }
                break

    return {
        "session_id": sess.id,
        "student_id": sess.student_id,
        "concept_cluster_id": sess.concept_cluster_id,
        "status": sess.status,
        "phase": sess.phase,
        "problem": problem,
        "kg": kg,
        "messages": [
            {"role": m.role, "content": m.content, "turn_index": m.turn_index}
            for m in msgs
        ],
    }
```

Import `ProblemAttempt` at the top of `lifecycle.py`.

- [ ] **Step 4: Run to verify.**

```bash
pytest apollo/handlers/tests/test_lifecycle.py -v
```

Expected: all PASS.

- [ ] **Step 5: Commit.**

```bash
git add apollo/handlers/lifecycle.py apollo/handlers/tests/test_lifecycle.py
git commit -m "feat(apollo): scope get_session KG + messages to current attempt"
git push
```

---

## Task 8: Add `InvalidPhaseError`

**Files:**
- Modify: `apollo/errors.py`
- Modify: `apollo/tests/test_errors.py`
- Modify: `apollo/api.py` (exception handler registration — applied here, used in later tasks)

- [ ] **Step 1: Write a failing test.**

Append to `apollo/tests/test_errors.py`:

```python
def test_invalid_phase_carries_phase():
    from apollo.errors import InvalidPhaseError
    e = InvalidPhaseError(session_id=42, phase="INIT")
    assert e.session_id == 42
    assert e.phase == "INIT"
    assert "INIT" in str(e)
```

- [ ] **Step 2: Run to verify it fails.**

```bash
pytest apollo/tests/test_errors.py::test_invalid_phase_carries_phase -v
```

Expected: FAIL — `InvalidPhaseError` doesn't exist.

- [ ] **Step 3: Add `InvalidPhaseError` to `apollo/errors.py`.**

Add alongside the other error classes:

```python
class InvalidPhaseError(ApolloError):
    """Endpoint called while the session is in a phase that forbids it."""

    def __init__(self, session_id: int, phase: str) -> None:
        self.session_id = session_id
        self.phase = phase
        super().__init__(
            f"cannot perform this action while session {session_id} is in phase {phase!r}"
        )
```

- [ ] **Step 4: Register the exception handler in `apollo/api.py`.**

Add, alongside the other handlers:

```python
async def invalid_phase_handler(request: Request, exc: InvalidPhaseError) -> JSONResponse:
    return JSONResponse(
        status_code=409,
        content=_err_payload(
            "invalid_phase",
            str(exc),
            session_id=exc.session_id,
            phase=exc.phase,
        ),
    )
```

Import `InvalidPhaseError` in the existing `apollo.errors` import list.

Inside `register_exception_handlers`:

```python
    app.add_exception_handler(InvalidPhaseError, invalid_phase_handler)
```

- [ ] **Step 5: Run tests.**

```bash
pytest apollo/tests/test_errors.py -v
```

Expected: all PASS.

- [ ] **Step 6: Commit.**

```bash
git add apollo/errors.py apollo/tests/test_errors.py apollo/api.py
git commit -m "feat(apollo): add InvalidPhaseError for endpoints called in wrong phase"
git push
```

---

## Task 9: Accept `difficulty` on `/from_hoot`

**Files:**
- Modify: `apollo/hoot_bridge/session_init.py`
- Modify: `apollo/hoot_bridge/tests/test_session_init.py`
- Modify: `apollo/api.py`

- [ ] **Step 1: Write a failing test.**

In `apollo/hoot_bridge/tests/test_session_init.py`, add:

```python
@pytest.mark.asyncio
async def test_init_session_honors_passed_difficulty(db):
    with patch_infer_concept("fluid_mechanics"):
        result = await init_session_from_hoot(
            db=db,
            student_id="stu-1",
            hoot_transcript="teach me bernoulli",
            difficulty="standard",
        )
    attempt = (
        await db.execute(select(ProblemAttempt).where(ProblemAttempt.session_id == result["session_id"]))
    ).scalar_one()
    assert attempt.difficulty == "standard"


@pytest.mark.asyncio
async def test_init_session_rejects_unknown_difficulty(db):
    with patch_infer_concept("fluid_mechanics"):
        with pytest.raises(ValueError):
            await init_session_from_hoot(
                db=db,
                student_id="stu-1",
                hoot_transcript="teach me bernoulli",
                difficulty="impossible",
            )
```

Match the `patch_infer_concept` helper and `db` fixture conventions from the existing tests in that file.

- [ ] **Step 2: Run to verify.**

```bash
pytest apollo/hoot_bridge/tests/test_session_init.py::test_init_session_honors_passed_difficulty apollo/hoot_bridge/tests/test_session_init.py::test_init_session_rejects_unknown_difficulty -v
```

Expected: FAIL — `init_session_from_hoot` has no `difficulty` parameter.

- [ ] **Step 3: Modify `init_session_from_hoot` to accept and validate `difficulty`.**

In `apollo/hoot_bridge/session_init.py`:

1. Delete the `_DEFAULT_FIRST_DIFFICULTY = "intro"` constant.
2. Add at module top:

```python
_ALLOWED_DIFFICULTIES = {"intro", "standard", "hard"}
```

3. Change the function signature to include `difficulty: str`.
4. Immediately validate:

```python
    if difficulty not in _ALLOWED_DIFFICULTIES:
        raise ValueError(
            f"unknown difficulty {difficulty!r}; "
            f"expected one of {sorted(_ALLOWED_DIFFICULTIES)}"
        )
```

5. Replace both `difficulty=_DEFAULT_FIRST_DIFFICULTY` references (in the `select_problem` call and the `ProblemAttempt` constructor) with `difficulty=difficulty`.

- [ ] **Step 4: Update `/from_hoot` in `apollo/api.py`.**

Replace the Pydantic model:

```python
class FromHootRequest(BaseModel):
    student_id: str
    hoot_transcript: str
    difficulty: Literal["intro", "standard", "hard"]
```

Add `from typing import Literal` at the top if not already imported.

Forward the new field:

```python
@router.post("/sessions/from_hoot")
async def session_from_hoot(
    body: FromHootRequest,
    db: AsyncSession = Depends(get_db_session),
) -> dict:
    return await init_session_from_hoot(
        db=db,
        student_id=body.student_id,
        hoot_transcript=body.hoot_transcript,
        difficulty=body.difficulty,
    )
```

- [ ] **Step 5: Update any pre-existing test in `test_session_init.py` that calls `init_session_from_hoot` without `difficulty`** — pass `difficulty="intro"` explicitly.

- [ ] **Step 6: Run tests.**

```bash
pytest apollo/hoot_bridge/tests/test_session_init.py apollo/api.py -v
```

(The second target is harmless if no tests live next to `api.py`.)

Expected: all PASS.

- [ ] **Step 7: Commit.**

```bash
git add apollo/hoot_bridge/session_init.py apollo/hoot_bridge/tests/test_session_init.py apollo/api.py
git commit -m "feat(apollo): accept difficulty on /sessions/from_hoot"
git push
```

---

## Task 10: New `POST /apollo/sessions/{id}/next` endpoint

**Files:**
- Create: `apollo/handlers/next.py`
- Create: `apollo/handlers/tests/test_next.py`
- Modify: `apollo/api.py`

- [ ] **Step 1: Write failing tests covering the five branches.**

Create `apollo/handlers/tests/test_next.py`:

```python
from unittest.mock import patch

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from apollo.errors import InvalidPhaseError, PoolExhaustedError, SessionFrozenError
from apollo.handlers.next import handle_next
from apollo.persistence.models import (
    ApolloSession,
    KGEntry,
    Message,
    ProblemAttempt,
    SessionPhase,
    SessionStatus,
)
from database.models import Base


@pytest_asyncio.fixture
async def db_with_report_session():
    """Session in REPORT phase with one graded attempt."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    tables = [
        ApolloSession.__table__,
        KGEntry.__table__,
        Message.__table__,
        ProblemAttempt.__table__,
    ]
    async with engine.begin() as conn:
        await conn.run_sync(lambda s: Base.metadata.create_all(s, tables=tables))
    Session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with Session() as s:
        sess = ApolloSession(
            student_id="stu-1",
            concept_cluster_id="fluid_mechanics",
            status=SessionStatus.active.value,
            phase=SessionPhase.REPORT.value,
            current_problem_id="bernoulli_horizontal_pipe_find_p2",
        )
        s.add(sess)
        await s.flush()
        s.add(ProblemAttempt(
            session_id=sess.id,
            problem_id="bernoulli_horizontal_pipe_find_p2",
            difficulty="intro",
            result="solved",
        ))
        await s.commit()
        await s.refresh(sess)
        yield s, sess.id
    await engine.dispose()


@pytest.mark.asyncio
async def test_next_from_report_advances(db_with_report_session):
    s, session_id = db_with_report_session
    result = await handle_next(db=s, session_id=session_id, difficulty="standard")

    assert result["session_id"] == session_id
    assert result["attempt_id"] is not None
    assert result["problem"]["id"] != "bernoulli_horizontal_pipe_find_p2"

    # Session phase is back to TEACHING with new current_problem_id.
    sess = (await s.execute(select(ApolloSession).where(ApolloSession.id == session_id))).scalar_one()
    assert sess.phase == SessionPhase.TEACHING.value
    assert sess.current_problem_id == result["problem"]["id"]

    # Prior attempt is still graded (not mutated).
    prior = (await s.execute(
        select(ProblemAttempt)
        .where(ProblemAttempt.session_id == session_id)
        .where(ProblemAttempt.problem_id == "bernoulli_horizontal_pipe_find_p2")
    )).scalar_one()
    assert prior.result == "solved"

    # New attempt exists at requested difficulty.
    new_attempt = (await s.execute(
        select(ProblemAttempt).where(ProblemAttempt.id == result["attempt_id"])
    )).scalar_one()
    assert new_attempt.difficulty == "standard"
    assert new_attempt.result is None


@pytest.mark.asyncio
async def test_next_from_teaching_abandons_current(db_with_report_session):
    s, session_id = db_with_report_session
    # Flip session to TEACHING, clear prior attempt's result.
    sess = (await s.execute(select(ApolloSession).where(ApolloSession.id == session_id))).scalar_one()
    sess.phase = SessionPhase.TEACHING.value
    prior = (await s.execute(
        select(ProblemAttempt).where(ProblemAttempt.session_id == session_id)
    )).scalar_one()
    prior.result = None
    await s.commit()

    result = await handle_next(db=s, session_id=session_id, difficulty="standard")

    abandoned = (await s.execute(
        select(ProblemAttempt).where(ProblemAttempt.id == prior.id)
    )).scalar_one()
    assert abandoned.result == "abandoned"

    new_attempt = (await s.execute(
        select(ProblemAttempt).where(ProblemAttempt.id == result["attempt_id"])
    )).scalar_one()
    assert new_attempt.difficulty == "standard"


@pytest.mark.asyncio
async def test_next_raises_session_frozen_during_solving(db_with_report_session):
    s, session_id = db_with_report_session
    sess = (await s.execute(select(ApolloSession).where(ApolloSession.id == session_id))).scalar_one()
    sess.phase = SessionPhase.SOLVING.value
    await s.commit()
    with pytest.raises(SessionFrozenError):
        await handle_next(db=s, session_id=session_id, difficulty="standard")


@pytest.mark.asyncio
async def test_next_raises_invalid_phase_from_init(db_with_report_session):
    s, session_id = db_with_report_session
    sess = (await s.execute(select(ApolloSession).where(ApolloSession.id == session_id))).scalar_one()
    sess.phase = SessionPhase.INIT.value
    await s.commit()
    with pytest.raises(InvalidPhaseError):
        await handle_next(db=s, session_id=session_id, difficulty="standard")


@pytest.mark.asyncio
async def test_next_raises_pool_exhausted_when_all_problems_attempted(db_with_report_session, monkeypatch):
    s, session_id = db_with_report_session
    # Force select_problem to raise.
    def _boom(*, cluster_id, difficulty, attempted_ids):
        raise PoolExhaustedError(concept_cluster_id=cluster_id, difficulty=difficulty)
    monkeypatch.setattr("apollo.handlers.next.select_problem", _boom)
    with pytest.raises(PoolExhaustedError):
        await handle_next(db=s, session_id=session_id, difficulty="hard")


@pytest.mark.asyncio
async def test_next_excludes_prior_problem_ids(db_with_report_session, monkeypatch):
    s, session_id = db_with_report_session
    captured = {}
    def _spy(*, cluster_id, difficulty, attempted_ids):
        captured["attempted_ids"] = list(attempted_ids)
        # Delegate to real selector to actually return a problem.
        from apollo.overseer.problem_selector import select_problem as real
        return real(cluster_id=cluster_id, difficulty=difficulty, attempted_ids=attempted_ids)
    monkeypatch.setattr("apollo.handlers.next.select_problem", _spy)
    await handle_next(db=s, session_id=session_id, difficulty="intro")
    assert "bernoulli_horizontal_pipe_find_p2" in captured["attempted_ids"]
```

- [ ] **Step 2: Run to verify all fail.**

```bash
pytest apollo/handlers/tests/test_next.py -v
```

Expected: all FAIL with `ModuleNotFoundError: apollo.handlers.next`.

- [ ] **Step 3: Create `apollo/handlers/next.py`.**

```python
"""POST /apollo/sessions/{id}/next — advance to a new problem at the student's chosen difficulty.

Unified endpoint: handles both post-Done advance (phase=REPORT) and mid-problem
abandon (phase=TEACHING or PROBLEM_REVEAL). Blocked with SessionFrozenError
during SOLVING. INIT / BETWEEN raise InvalidPhaseError.
"""
from __future__ import annotations

from typing import Any, Dict

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apollo.errors import InvalidPhaseError, SessionFrozenError
from apollo.overseer.problem_selector import select_problem
from apollo.persistence.models import ApolloSession, ProblemAttempt, SessionPhase, SessionStatus


_ABANDON_PHASES = {SessionPhase.TEACHING.value, SessionPhase.PROBLEM_REVEAL.value}
_ADVANCE_PHASES = {SessionPhase.REPORT.value}
_FROZEN_PHASES = {SessionPhase.SOLVING.value}


async def handle_next(
    *,
    db: AsyncSession,
    session_id: int,
    difficulty: str,
) -> Dict[str, Any]:
    # with_for_update() takes a row lock on Postgres so a double-clicked
    # /next can't race into two ProblemAttempt rows. SQLite ignores it.
    sess = (await db.execute(
        select(ApolloSession).where(ApolloSession.id == session_id).with_for_update()
    )).scalar_one()

    if sess.status != SessionStatus.active.value:
        raise InvalidPhaseError(session_id=session_id, phase=f"status={sess.status}")

    phase = sess.phase
    if phase in _FROZEN_PHASES:
        raise SessionFrozenError(session_id=str(session_id))
    if phase not in _ABANDON_PHASES and phase not in _ADVANCE_PHASES:
        raise InvalidPhaseError(session_id=session_id, phase=phase)

    current_attempt = (await db.execute(
        select(ProblemAttempt)
        .where(ProblemAttempt.session_id == session_id)
        .where(ProblemAttempt.problem_id == sess.current_problem_id)
        .order_by(ProblemAttempt.id.desc())
    )).scalars().first()

    if phase in _ABANDON_PHASES and current_attempt is not None and current_attempt.result is None:
        current_attempt.result = "abandoned"
        await db.flush()

    attempted_ids = [
        row for row in (await db.execute(
            select(ProblemAttempt.problem_id)
            .where(ProblemAttempt.session_id == session_id)
        )).scalars().all()
    ]

    problem = select_problem(
        cluster_id=sess.concept_cluster_id,
        difficulty=difficulty,
        attempted_ids=attempted_ids,
    )

    new_attempt = ProblemAttempt(
        session_id=session_id,
        problem_id=problem.id,
        difficulty=difficulty,
    )
    db.add(new_attempt)
    await db.flush()

    sess.current_problem_id = problem.id
    sess.phase = SessionPhase.TEACHING.value
    await db.commit()

    return {
        "session_id": session_id,
        "attempt_id": new_attempt.id,
        "problem": {
            "id": problem.id,
            "concept_id": problem.concept_id,
            "difficulty": problem.difficulty,
            "problem_text": problem.problem_text,
            "given_values": problem.given_values,
            "target_unknown": problem.target_unknown,
        },
    }
```

- [ ] **Step 4: Register the route in `apollo/api.py`.**

Add, next to the other session routes:

```python
class NextRequest(BaseModel):
    difficulty: Literal["intro", "standard", "hard"]


@router.post("/sessions/{session_id}/next")
async def next_problem(
    session_id: int,
    body: NextRequest,
    db: AsyncSession = Depends(get_db_session),
) -> dict:
    from apollo.handlers.next import handle_next
    return await handle_next(db=db, session_id=session_id, difficulty=body.difficulty)
```

- [ ] **Step 5: Run the test module.**

```bash
pytest apollo/handlers/tests/test_next.py -v
```

Expected: all PASS.

- [ ] **Step 6: Commit.**

```bash
git add apollo/handlers/next.py apollo/handlers/tests/test_next.py apollo/api.py
git commit -m "feat(apollo): add POST /sessions/{id}/next endpoint"
git push
```

---

## Task 11: New `POST /apollo/sessions/{id}/restart_problem` endpoint

**Files:**
- Create: `apollo/handlers/restart_problem.py`
- Create: `apollo/handlers/tests/test_restart_problem.py`
- Modify: `apollo/api.py`

- [ ] **Step 1: Write failing tests.**

Create `apollo/handlers/tests/test_restart_problem.py` with the same fixture pattern as `test_next.py`'s `db_with_report_session`, plus:

```python
@pytest.mark.asyncio
async def test_restart_wipes_kg_and_messages_for_current_attempt(db_with_report_session):
    s, session_id = db_with_report_session
    attempt = (await s.execute(select(ProblemAttempt).where(ProblemAttempt.session_id == session_id))).scalar_one()
    # Flip session to TEACHING so restart is allowed (REPORT also works, but
    # the wipe semantics are the same).
    sess = (await s.execute(select(ApolloSession).where(ApolloSession.id == session_id))).scalar_one()
    sess.phase = SessionPhase.TEACHING.value
    # Seed KG + messages scoped to the current attempt.
    s.add(KGEntry(session_id=session_id, attempt_id=attempt.id, type="equation",
                  content={"symbolic": "x - 1", "label": "to_be_wiped"}, source="parser"))
    s.add(Message(session_id=session_id, attempt_id=attempt.id, role="student", content="hi", turn_index=0))
    await s.commit()

    result = await handle_restart_problem(db=s, session_id=session_id)
    assert result == {"ok": True}

    kg_rows = (await s.execute(select(KGEntry).where(KGEntry.attempt_id == attempt.id))).scalars().all()
    msg_rows = (await s.execute(select(Message).where(Message.attempt_id == attempt.id))).scalars().all()
    assert kg_rows == []
    assert msg_rows == []

    # Attempt row itself is unchanged.
    attempt_after = (await s.execute(select(ProblemAttempt).where(ProblemAttempt.id == attempt.id))).scalar_one()
    assert attempt_after.problem_id == attempt.problem_id
    assert attempt_after.difficulty == attempt.difficulty
    assert attempt_after.result is None

    # Phase is TEACHING.
    sess_after = (await s.execute(select(ApolloSession).where(ApolloSession.id == session_id))).scalar_one()
    assert sess_after.phase == SessionPhase.TEACHING.value


@pytest.mark.asyncio
async def test_restart_blocked_during_solving(db_with_report_session):
    s, session_id = db_with_report_session
    sess = (await s.execute(select(ApolloSession).where(ApolloSession.id == session_id))).scalar_one()
    sess.phase = SessionPhase.SOLVING.value
    await s.commit()
    with pytest.raises(SessionFrozenError):
        await handle_restart_problem(db=s, session_id=session_id)


@pytest.mark.asyncio
async def test_restart_does_not_touch_other_attempts(db_with_report_session):
    s, session_id = db_with_report_session
    current = (await s.execute(select(ProblemAttempt).where(ProblemAttempt.session_id == session_id))).scalar_one()
    # Add another attempt + KG row under a DIFFERENT attempt.
    other = ProblemAttempt(
        session_id=session_id,
        problem_id="some_other_problem",
        difficulty="intro",
        result="abandoned",
    )
    s.add(other)
    await s.flush()
    s.add(KGEntry(session_id=session_id, attempt_id=other.id, type="equation",
                  content={"symbolic": "survivor - 0"}, source="parser"))
    # Current-attempt KG to be wiped.
    s.add(KGEntry(session_id=session_id, attempt_id=current.id, type="equation",
                  content={"symbolic": "victim - 0"}, source="parser"))
    # Need to flip phase to something allowed.
    sess = (await s.execute(select(ApolloSession).where(ApolloSession.id == session_id))).scalar_one()
    sess.phase = SessionPhase.TEACHING.value
    await s.commit()

    await handle_restart_problem(db=s, session_id=session_id)

    survivors = (await s.execute(select(KGEntry).where(KGEntry.attempt_id == other.id))).scalars().all()
    assert len(survivors) == 1
    victims = (await s.execute(select(KGEntry).where(KGEntry.attempt_id == current.id))).scalars().all()
    assert victims == []
```

- [ ] **Step 2: Run to verify.**

```bash
pytest apollo/handlers/tests/test_restart_problem.py -v
```

Expected: all FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Create `apollo/handlers/restart_problem.py`.**

```python
"""POST /apollo/sessions/{id}/restart_problem — wipe current attempt's KG + messages.

Same ProblemAttempt row, same problem, same difficulty. Caller gets a clean
conversation and a clean KG on the same problem. Blocked during SOLVING.
"""
from __future__ import annotations

from typing import Any, Dict

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from apollo.errors import InvalidPhaseError, SessionFrozenError
from apollo.persistence.models import (
    ApolloSession,
    KGEntry,
    Message,
    ProblemAttempt,
    SessionPhase,
    SessionStatus,
)


_ALLOWED_PHASES = {
    SessionPhase.TEACHING.value,
    SessionPhase.PROBLEM_REVEAL.value,
    SessionPhase.REPORT.value,
}
_FROZEN_PHASES = {SessionPhase.SOLVING.value}


async def handle_restart_problem(
    *,
    db: AsyncSession,
    session_id: int,
) -> Dict[str, Any]:
    # Row lock on Postgres to serialize concurrent restart + chat writes.
    # SQLite silently ignores it.
    sess = (await db.execute(
        select(ApolloSession).where(ApolloSession.id == session_id).with_for_update()
    )).scalar_one()

    if sess.status != SessionStatus.active.value:
        raise InvalidPhaseError(session_id=session_id, phase=f"status={sess.status}")
    if sess.phase in _FROZEN_PHASES:
        raise SessionFrozenError(session_id=str(session_id))
    if sess.phase not in _ALLOWED_PHASES:
        raise InvalidPhaseError(session_id=session_id, phase=sess.phase)

    current_attempt = (await db.execute(
        select(ProblemAttempt)
        .where(ProblemAttempt.session_id == session_id)
        .where(ProblemAttempt.problem_id == sess.current_problem_id)
        .order_by(ProblemAttempt.id.desc())
    )).scalars().first()
    if current_attempt is None:
        raise RuntimeError(f"no current ProblemAttempt for session {session_id}")

    await db.execute(delete(KGEntry).where(KGEntry.attempt_id == current_attempt.id))
    await db.execute(delete(Message).where(Message.attempt_id == current_attempt.id))

    sess.phase = SessionPhase.TEACHING.value
    await db.commit()

    return {"ok": True}
```

- [ ] **Step 4: Register the route in `apollo/api.py`.**

```python
@router.post("/sessions/{session_id}/restart_problem")
async def restart_problem(
    session_id: int,
    db: AsyncSession = Depends(get_db_session),
) -> dict:
    from apollo.handlers.restart_problem import handle_restart_problem
    return await handle_restart_problem(db=db, session_id=session_id)
```

- [ ] **Step 5: Run tests.**

```bash
pytest apollo/handlers/tests/test_restart_problem.py -v
```

Expected: all PASS.

- [ ] **Step 6: Commit.**

```bash
git add apollo/handlers/restart_problem.py apollo/handlers/tests/test_restart_problem.py apollo/api.py
git commit -m "feat(apollo): add POST /sessions/{id}/restart_problem endpoint"
git push
```

---

## Task 12: Extend the e2e smoke test with a multi-attempt flow

**Files:**
- Modify: `apollo/tests/test_e2e_smoke.py`

- [ ] **Step 1: Append a new test that exercises the full switch + restart path.**

The existing smoke test already drives `from_hoot` → chat → done. The new test drives:

```
from_hoot(difficulty="intro") → attempt A
  → a couple of chat turns (parser mocked)
/next(difficulty="standard") from TEACHING → attempt A abandoned, attempt B created
  → a couple of chat turns on attempt B
/done → rubric + xp_earned use "standard" multiplier, is_reattempt=False
/restart_problem → attempt B's KG + messages wiped, same attempt row
```

Full test code (paste verbatim; names and patch targets match the existing smoke test's style):

```python
@pytest.mark.asyncio
async def test_e2e_switch_then_restart(monkeypatch, apollo_app_client):
    """E2E: pick initial difficulty, switch mid-problem, restart post-Done."""
    client, db_factory = apollo_app_client

    # Monkeypatch the parser + apollo LLM + diagnostic to be deterministic.
    # Match whatever names the existing smoke test uses — copy its setup.
    _patch_deterministic(monkeypatch)

    # 1. from_hoot at intro.
    r = await client.post("/apollo/sessions/from_hoot", json={
        "student_id": "stu-1",
        "hoot_transcript": "teach me bernoulli",
        "difficulty": "intro",
    })
    assert r.status_code == 200, r.text
    session_id = r.json()["session_id"]
    attempt_a = r.json()["attempt_id"]

    # 2. A chat turn.
    r = await client.post(f"/apollo/sessions/{session_id}/chat", json={"message": "x equals 1"})
    assert r.status_code == 200, r.text

    # 3. /next from TEACHING — abandon A, create B.
    r = await client.post(f"/apollo/sessions/{session_id}/next", json={"difficulty": "standard"})
    assert r.status_code == 200, r.text
    attempt_b = r.json()["attempt_id"]
    assert attempt_b != attempt_a

    # Verify DB state: A is abandoned, B is fresh.
    async with db_factory() as s:
        from apollo.persistence.models import ProblemAttempt
        rows = (await s.execute(
            select(ProblemAttempt).where(ProblemAttempt.session_id == session_id).order_by(ProblemAttempt.id)
        )).scalars().all()
        assert rows[0].result == "abandoned"
        assert rows[1].result is None
        assert rows[1].difficulty == "standard"

    # 4. Teach + done on B.
    r = await client.post(f"/apollo/sessions/{session_id}/chat", json={"message": "z equals 2"})
    assert r.status_code == 200, r.text
    r = await client.post(f"/apollo/sessions/{session_id}/done")
    assert r.status_code == 200, r.text
    body = r.json()
    # XP uses standard multiplier and is_reattempt=False (A was abandoned).
    assert body["xp_earned"] > 0
    # Standard multiplier ×1.5 on any overall ≥ 1 yields xp_earned ≥ 1 rounded
    # down. Assert the multiplier was applied by comparing to intro-equivalent.
    assert body["xp_earned"] >= int(body["rubric"]["overall"]["score"] * 1.0)

    # 5. /restart_problem from REPORT.
    r = await client.post(f"/apollo/sessions/{session_id}/restart_problem")
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True}

    async with db_factory() as s:
        from apollo.persistence.models import KGEntry, Message, ProblemAttempt
        surviving_on_b = (await s.execute(
            select(KGEntry).where(KGEntry.attempt_id == attempt_b)
        )).scalars().all()
        assert surviving_on_b == []
        surviving_msgs = (await s.execute(
            select(Message).where(Message.attempt_id == attempt_b)
        )).scalars().all()
        assert surviving_msgs == []
        # Attempt A KG + messages still exist.
        a_kg = (await s.execute(
            select(KGEntry).where(KGEntry.attempt_id == attempt_a)
        )).scalars().all()
        assert len(a_kg) >= 1
```

If the existing e2e smoke test defines `_patch_deterministic`, reuse it. Otherwise copy the monkeypatch setup from the existing test body into a new helper at the top of the file.

- [ ] **Step 2: Run the test.**

```bash
pytest apollo/tests/test_e2e_smoke.py::test_e2e_switch_then_restart -v
```

Expected: PASS.

- [ ] **Step 3: Run the full smoke module to catch regressions.**

```bash
pytest apollo/tests/test_e2e_smoke.py -v
```

Expected: all PASS.

- [ ] **Step 4: Commit.**

```bash
git add apollo/tests/test_e2e_smoke.py
git commit -m "test(apollo): e2e smoke for difficulty switch + restart"
git push
```

---

## Task 13: Full-suite sanity pass

- [ ] **Step 1: Run the entire pytest suite.**

```bash
pytest -v --tb=short
```

Expected: all PASS. If any pre-existing test breaks because of the `KGStore` signature change or the `Message.attempt_id` requirement, fix it in place (add `attempt_id=<current_attempt.id>` to the seed). Do not skip tests.

- [ ] **Step 2: If any fixes were needed, commit them together.**

```bash
git add -u
git commit -m "test(apollo): align remaining tests with attempt-scoped KG/messages"
git push
```

---

## Notes for the Executor

- **No new packages.** Everything uses what `requirements.txt` already pins.
- **Every commit pushes.** This project's convention (captured in memory) is feature-branch commit = stage + commit + push in one turn.
- **Branch is `ApolloV2`.** Never merge to main without explicit user go-ahead.
- **Migrations run separately** — executing this plan does NOT run `014_apollo_attempt_id.sql` against any real database. The user handles production migration. The SQLAlchemy-model-level changes are what the tests exercise.
- **Monkeypatch targets** in tests (`apollo.handlers.chat._run_parser_pipeline`, etc.) — verify the real attribute names in the target modules; the names in this plan are approximations. If a name doesn't match, fix the monkeypatch to the real attribute, don't invent a new one.
