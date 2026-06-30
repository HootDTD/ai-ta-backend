# Apollo Clarification Loop Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let Apollo ask a targeted, answer-blind follow-up when a student's idea is plausibly-but-ambiguously correct, then use the student's committed answer (not a silent machine guess) to resolve the idea at grading — fixing the G2 resolver-recall driver of abstention.

**Architecture:** Two phases share one new Postgres table (`apollo_clarifications`). **Live (per teaching turn):** embedding similarity flags ambiguous residual nodes → Apollo weaves an answer-blind probe into its reply → an `asked_waiting` row is persisted. **Next turn:** a focused AI judge re-scores the student's committed answer → `confirmed`/`refuted`/`vague`. **Done (grading):** confirmed outcomes feed `resolve_attempt` as an authoritative `clarification` method @0.90, and the silent LLM adjudication is removed. Embeddings only ever *route to a question* — they never grant credit.

**Tech Stack:** Python 3 / FastAPI / SQLAlchemy 2.0 async (asyncpg) / Supabase Postgres + pgvector / OpenAI `text-embedding-3-large` (3072d) + `gpt-4o` / pytest + Testcontainers.

## Global Constraints

- **Patch coverage ≥ 95%** on changed lines, measured `diff-cover coverage.xml --compare-branch=origin/staging --fail-under=95`. CI enforces it (`.github/workflows/ci.yml` integration job). No new line ships untested.
- **No live model calls in CI.** Every AI/embedding dependency is a **callable Protocol with a defaulted real impl**, injected; tests pass deterministic stubs (mirror `Adjudicator` / `LeakageJudge` / `audit_fn` patterns).
- **Migrations are local-only for agents.** Author `032_apollo_clarifications.sql`, verify on **local Docker Postgres** (`db_session` Testcontainer fixture). NEVER apply to any remote Supabase project — test→prod is a human/CI step. Each Apollo migration carries the "DO NOT auto-apply" banner.
- **Apollo RLS convention (NOT the spec's §10 wording):** `ENABLE ROW LEVEL SECURITY` with **zero policies** (owner-connection backend exempt; anon key denied). Course isolation is app-layer via `search_space_id`. Do not write `service_role` grants or `auth.uid()` student-read policies — no Apollo table uses them.
- **Branch:** `feat/apollo-clarification-loop` (off `staging`, already up to date with `origin/staging`). Conventional commits, no attribution footer.
- **Immutability / small files:** frozen dataclasses, new logic in the focused `apollo/clarification/` package (one responsibility per file).
- **Drift contract:** on landing, reconcile `docs/architecture/apollo.md` (owner of `apollo/`) and `docs/architecture/domain-data.md` (owner of the DB schema) in the same change; bump `last_verified`.

## Calibration parameters (defaults — tunable, not placeholders)

| Parameter | Symbol / constant | Default |
|---|---|---|
| Detector cosine band | `T_AMBIG` | `0.50` |
| Follow-ups per turn | `MAX_PROBES_PER_TURN` | `3` |
| Follow-ups per idea | (UNIQUE `(attempt_id, node_id)`) | `1` |
| `clarification` confidence cap | `METHOD_CONFIDENCE_CAP["clarification"]` | `0.90` |
| Prioritization | rubric weight, then cosine | — |

---

## Spec → reality corrections folded into this plan

These three spec assumptions were wrong against the current tree and are corrected below:

1. **Migration head is `031`, not `~024`.** New file is `032_apollo_clarifications.sql` (Task 1).
2. **The leakage judge is NOT wired into the chat path today** (spec §6.4 assumes it is; `test_chat_no_signals.py:42` asserts `validate_or_raise(` is absent). The primary anti-leak guarantee is **structural**: `draft_reply` is answer-blind and the probe *hint* never carries the candidate's value (unit-tested in Task 7). **Per the user's decision, we ALSO wire the leakage judge as a narrow, soft-fail-open backstop on clarification replies only (Task 14)** — implemented via a new `guard_clarification_reply` (NOT the raising `validate_or_raise`), so the v1 guard string stays satisfied; the guard test is narrowed to allow this one intentional, clarification-scoped check.
3. **The macro probe scripts were deleted** (`scripts/apollo_grade_probe.py` / `_macro_scenarios.py`, commit `b0f6524`; preserved on branch `experiment/macro-graph-grading-probe`). The CI-gating E2E is the deterministic `apollo/grading/tests/test_corpus_e2e.py`; we extend that (Task 17) and note the live probe as an optional manual restore.

Two more deviations, with rationale, that the executor should keep:

- **Candidate-embedding cache key.** Spec §6.3 says key on `reference_hash.py`. The chat path does not build a `ReferenceGraph`, only the candidate set. We key the cache on a self-contained `candidate_set_hash(candidates)` (sha256 over each candidate's identity fields) — same invalidation semantics (the candidate set is *derived* from the reference + misconceptions), no extra canonicalization per turn. (Task 6)
- **Column names** follow repo convention: `user_id UUID` (the student) + `search_space_id INTEGER` (course isolation), not the spec's bare `student_id`. (Task 1)

---

## File Structure

**New package `apollo/clarification/`** (each file one responsibility):

| File | Responsibility |
|---|---|
| `apollo/clarification/__init__.py` | Package facade — re-export public API. |
| `apollo/clarification/embedding.py` | `Embedder` Protocol, `default_embedder`, `candidate_surface_texts`, `candidate_set_hash`, `CandidateEmbeddingCache`, `cosine`. |
| `apollo/clarification/detector.py` | `FlaggedNode` dataclass, `detect_ambiguous_nodes`, `T_AMBIG`. |
| `apollo/clarification/probe.py` | `build_probe_hint` (answer-blind, dimension-not-value). |
| `apollo/clarification/pacing.py` | `MAX_PROBES_PER_TURN`, `select_probes` (rubric weight → cosine). |
| `apollo/clarification/rescorer.py` | `ClarificationJudge` Protocol, `RescoreOutcome`, `rescore_clarification`, `default_clarification_judge`. |
| `apollo/clarification/candidate_assembly.py` | `load_problem_candidates` (shared async candidate-set build). |
| `apollo/clarification/store.py` | async CRUD: `write_asked_waiting`, `load_asked_waiting`, `record_outcome`, `load_confirmed_resolutions`. |
| `apollo/clarification/turn.py` | `run_clarification_detection` — per-turn detect→probe→persist orchestration. |
| `apollo/clarification/resolve_turn.py` | `resolve_pending_clarifications` — next-turn re-score orchestration. |
| `apollo/clarification/leak_guard.py` | `guard_clarification_reply` — soft-fail-open leakage-judge backstop on clarification replies. |
| `apollo/clarification/tests/` | unit tests per module. |

**Modified files:**

| File | Change |
|---|---|
| `database/migrations/032_apollo_clarifications.sql` | New table + RLS-stopgap. |
| `apollo/persistence/models.py` | `Clarification` ORM model + `CLARIFICATION_STATES` tuple. |
| `apollo/resolution/candidates.py` | Add `"clarification"` method + `0.90` cap. |
| `apollo/resolution/resolver.py` | `confirmed_resolutions` param (authoritative, pre-tier); remove `llm_adjudicator`; add public `find_residual_nodes`. |
| `apollo/resolution/adjudication.py` | Retire `adjudicate`/`main_chat_adjudicator`/`Adjudicator`/request-reply types. |
| `apollo/resolution/__init__.py` | Drop adjudication exports; export `find_residual_nodes`. |
| `apollo/agent/apollo_llm.py` | `draft_reply` gains `clarification_hints` kwarg. |
| `apollo/handlers/chat.py` | Wire detection+probing+persistence; wire next-turn re-scoring. |
| `apollo/handlers/done_grading.py` | Drop `llm_adjudicator`; load + pass `confirmed_resolutions`. |
| `apollo/handlers/done.py` | Thread `confirmed_resolutions` into `run_graph_simulation`. |
| `apollo/handlers/tests/test_chat_no_signals.py` | Narrow the v1 anti-signals guard to allow the clarification-scoped leak backstop. |
| `apollo/grading/tests/_builders.py` | Add clarification stub factories (reuse in tests). |
| `apollo/grading/tests/test_corpus_e2e.py` | Add a clarification-confirmed scenario. |
| `docs/architecture/apollo.md` | Drift reconciliation. |
| `docs/architecture/domain-data.md` | Register new table. |

**Dependency order:** DB (1) → resolver core (2–4) → live building blocks (5–11) → store (12, needs 1) → chat wiring + leak guard (13, 14, 15) → grading wiring (16) → docs + E2E (17). Tasks 2–11 are pure and independently testable without the DB or live calls.

---

### Task 1: `apollo_clarifications` table — migration + ORM model + local-PG tests

**Files:**
- Create: `database/migrations/032_apollo_clarifications.sql`
- Modify: `apollo/persistence/models.py` (add `Clarification` + `CLARIFICATION_STATES`)
- Test: `apollo/persistence/tests/test_clarification_model.py` (allowlist + ORM shape, SQLite)
- Test: `tests/database/test_apollo_clarifications_migration.py` (round-trip + constraints, local Postgres)

**Interfaces:**
- Produces: ORM `Clarification` (table `apollo_clarifications`) with columns `id, attempt_id, session_id, user_id, search_space_id, concept_id, node_id, candidate_key, state, probe_question, original_statement, clarification_text, asked_turn, answered_turn, created_at, updated_at`; module tuple `CLARIFICATION_STATES = ("asked_waiting", "confirmed", "refuted", "vague")`. Consumed by Task 12 (store) and Task 1's own tests.

- [ ] **Step 1: Write the migration SQL**

Create `database/migrations/032_apollo_clarifications.sql`:

```sql
-- 032_apollo_clarifications.sql
-- Apollo clarification loop (G2): one row per ambiguous student idea Apollo
-- probed with an answer-blind follow-up. State machine:
--   asked_waiting -> {confirmed | refuted | vague}  (terminal; one probe per idea)
-- A `confirmed` row resolves the node at grading via the `clarification` method
-- (cap 0.90). A `refuted` row is misconception evidence (no credit). RLS follows
-- the Apollo default-deny stopgap (mirror 022/026): ENABLE, no policies, the
-- owner-connection backend is exempt; app-layer scoping is search_space_id.
--
-- DEPLOY-TIME RECONCILIATION: numbered migration; agents apply to LOCAL Docker
-- Postgres only. Test-project rehearsal then prod is a human/CI step. DO NOT
-- auto-apply to any remote Supabase project.

BEGIN;

CREATE TABLE IF NOT EXISTS apollo_clarifications (
    id                 BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    attempt_id         BIGINT NOT NULL
        REFERENCES apollo_problem_attempts(id) ON DELETE CASCADE,
    session_id         BIGINT NOT NULL
        REFERENCES apollo_sessions(id) ON DELETE CASCADE,
    user_id            UUID NOT NULL
        REFERENCES auth.users(id) ON DELETE CASCADE,
    search_space_id    INTEGER NOT NULL
        REFERENCES aita_search_spaces(id) ON DELETE CASCADE,
    concept_id         BIGINT
        REFERENCES apollo_concepts(id) ON DELETE SET NULL,
    node_id            TEXT NOT NULL,
    candidate_key      TEXT NOT NULL,
    state              TEXT NOT NULL DEFAULT 'asked_waiting'
        CHECK (state IN ('asked_waiting', 'confirmed', 'refuted', 'vague')),
    probe_question     TEXT NOT NULL,
    original_statement TEXT NOT NULL,
    clarification_text TEXT,
    asked_turn         INTEGER NOT NULL,
    answered_turn      INTEGER,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- One follow-up per idea per attempt (spec §10 state machine).
    CONSTRAINT apollo_clarifications_attempt_node_uniq UNIQUE (attempt_id, node_id)
);

CREATE INDEX IF NOT EXISTS apollo_clarifications_attempt_state_idx
    ON apollo_clarifications(attempt_id, state);

CREATE INDEX IF NOT EXISTS apollo_clarifications_user_concept_idx
    ON apollo_clarifications(user_id, concept_id);

-- RLS stopgap (mirror migrations 022/026): default-deny to PostgREST, no
-- policies. The owner-connection backend is exempt; the anon/public key cannot
-- read/write. App-layer tenant scoping (auth.py + search_space_id) enforces.
ALTER TABLE apollo_clarifications ENABLE ROW LEVEL SECURITY;

COMMENT ON TABLE apollo_clarifications IS
    'Apollo clarification loop (G2): one row per ambiguous student idea probed '
    'with an answer-blind follow-up; confirmed rows resolve at grading via the '
    'clarification method (0.90), refuted rows are misconception evidence.';

COMMIT;
```

- [ ] **Step 2: Add the ORM model + states tuple**

In `apollo/persistence/models.py`, after the `Misconception` class (near line 216), add the states tuple near the other allowlists and the model. The imports `BigInteger, Integer, Text, TIMESTAMP, Column, ForeignKey, UniqueConstraint, Index, UUID, datetime, UTC` already exist in this module (used by `ApolloSession`/`GraphComparisonRun`); reuse `_JSONType` is not needed here.

```python
# Mirrors the SQL CHECK in migration 032. Asserted equal by the allowlist test.
CLARIFICATION_STATES: tuple[str, ...] = ("asked_waiting", "confirmed", "refuted", "vague")


class Clarification(Base):
    """One probed ambiguous student idea (Apollo clarification loop, migration
    032). State machine asked_waiting -> {confirmed|refuted|vague}; UNIQUE on
    (attempt_id, node_id) enforces one follow-up per idea. ``user_id`` declares
    NO ORM FK (auth.users is Supabase-managed, absent from Base.metadata),
    mirroring GraphComparisonRun."""

    __tablename__ = "apollo_clarifications"

    id = Column(BigInteger().with_variant(Integer(), "sqlite"), primary_key=True, autoincrement=True)
    attempt_id = Column(
        BigInteger, ForeignKey("apollo_problem_attempts.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    session_id = Column(
        BigInteger, ForeignKey("apollo_sessions.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    user_id = Column(UUID(as_uuid=False), nullable=False, index=True)
    search_space_id = Column(
        Integer, ForeignKey("aita_search_spaces.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    concept_id = Column(
        BigInteger, ForeignKey("apollo_concepts.id", ondelete="SET NULL"), nullable=True,
    )
    node_id = Column(Text, nullable=False)
    candidate_key = Column(Text, nullable=False)
    state = Column(Text, nullable=False, server_default=text("'asked_waiting'"), default="asked_waiting")
    probe_question = Column(Text, nullable=False)
    original_statement = Column(Text, nullable=False)
    clarification_text = Column(Text, nullable=True)
    asked_turn = Column(Integer, nullable=False)
    answered_turn = Column(Integer, nullable=True)
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(UTC))
    updated_at = Column(TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(UTC))

    __table_args__ = (
        UniqueConstraint("attempt_id", "node_id", name="apollo_clarifications_attempt_node_uniq"),
    )
```

If `text` is not already imported in this module, add `from sqlalchemy import text` to the existing sqlalchemy import block (it is used by `ProblemAttempt.server_default=text(...)`, so it is already present — verify).

- [ ] **Step 3: Write the allowlist + ORM-shape test (SQLite, no Docker)**

Create `apollo/persistence/tests/test_clarification_model.py`:

```python
from apollo.persistence.models import CLARIFICATION_STATES, Clarification


def test_clarification_states_allowlist_matches_sql_check():
    # Mirror of migration 032's CHECK (state IN (...)). Keep in lockstep.
    assert CLARIFICATION_STATES == ("asked_waiting", "confirmed", "refuted", "vague")


def test_clarification_table_columns():
    cols = Clarification.__table__.columns
    expected = {
        "id", "attempt_id", "session_id", "user_id", "search_space_id",
        "concept_id", "node_id", "candidate_key", "state", "probe_question",
        "original_statement", "clarification_text", "asked_turn", "answered_turn",
        "created_at", "updated_at",
    }
    assert set(cols.keys()) == expected
    assert cols["clarification_text"].nullable is True
    assert cols["answered_turn"].nullable is True
    assert cols["node_id"].nullable is False


def test_clarification_unique_attempt_node():
    uniques = {
        tuple(c.name for c in con.columns)
        for con in Clarification.__table__.constraints
        if con.__class__.__name__ == "UniqueConstraint"
    }
    assert ("attempt_id", "node_id") in uniques
```

- [ ] **Step 4: Run the SQLite tests — expect pass after the model exists**

Run: `pytest apollo/persistence/tests/test_clarification_model.py -v`
Expected: 3 passed.

- [ ] **Step 5: Write the local-Postgres migration/round-trip test**

Create `tests/database/test_apollo_clarifications_migration.py` (mirrors `tests/database/test_apollo_comparison_run_persistence.py`; uses the `db_session` Testcontainer fixture, which `pytest.skip`s if Docker is down). The `db_session` engine ran `Base.metadata.create_all`, so the `Clarification` table exists once the model is defined.

```python
import pytest
from sqlalchemy import select

from apollo.persistence.models import Clarification

pytestmark = pytest.mark.integration


async def _seed_parents(db_session):
    # Minimal parent rows so FKs resolve. Reuse the helpers the sibling
    # persistence tests use (search space, auth user, concept, session, attempt).
    # See tests/database/test_apollo_comparison_run_persistence.py for the exact
    # fixture builders; import and call them here rather than re-deriving.
    from tests.database._apollo_db_fixtures import seed_attempt_chain
    return await seed_attempt_chain(db_session)


async def test_clarification_roundtrip_and_state_transition(db_session):
    ctx = await _seed_parents(db_session)
    row = Clarification(
        attempt_id=ctx.attempt_id, session_id=ctx.session_id, user_id=ctx.user_id,
        search_space_id=ctx.search_space_id, concept_id=ctx.concept_id,
        node_id="s1", candidate_key="cond.bernoulli", state="asked_waiting",
        probe_question="Which way does pressure go?", original_statement="pressure changes",
        asked_turn=2,
    )
    db_session.add(row)
    await db_session.flush()

    fetched = (await db_session.execute(
        select(Clarification).where(Clarification.attempt_id == ctx.attempt_id)
    )).scalar_one()
    assert fetched.state == "asked_waiting"
    assert fetched.clarification_text is None

    fetched.state = "confirmed"
    fetched.clarification_text = "pressure is lower where it moves faster"
    fetched.answered_turn = 4
    await db_session.flush()
    assert fetched.answered_turn == 4


async def test_one_clarification_per_node_per_attempt(db_session):
    from sqlalchemy.exc import IntegrityError
    ctx = await _seed_parents(db_session)
    a = Clarification(attempt_id=ctx.attempt_id, session_id=ctx.session_id, user_id=ctx.user_id,
                      search_space_id=ctx.search_space_id, concept_id=ctx.concept_id,
                      node_id="s1", candidate_key="k", state="asked_waiting",
                      probe_question="q", original_statement="o", asked_turn=1)
    db_session.add(a)
    await db_session.flush()
    b = Clarification(attempt_id=ctx.attempt_id, session_id=ctx.session_id, user_id=ctx.user_id,
                      search_space_id=ctx.search_space_id, concept_id=ctx.concept_id,
                      node_id="s1", candidate_key="k2", state="asked_waiting",
                      probe_question="q2", original_statement="o2", asked_turn=3)
    db_session.add(b)
    with pytest.raises(IntegrityError):
        await db_session.flush()
```

If `tests/database/_apollo_db_fixtures.py::seed_attempt_chain` does not exist, create it in this step by extracting the parent-seeding code already inline in `tests/database/test_apollo_comparison_run_persistence.py` into a reusable helper returning a small dataclass with `attempt_id, session_id, user_id, search_space_id, concept_id`. (DRY — the comparison-run test seeds the identical chain.)

- [ ] **Step 6: Run the DB test (or confirm clean skip without Docker)**

Run: `pytest tests/database/test_apollo_clarifications_migration.py -v`
Expected: 2 passed with Docker; cleanly skipped (`_pg_url` skip) without Docker.

- [ ] **Step 7: Commit**

```bash
git add database/migrations/032_apollo_clarifications.sql apollo/persistence/models.py \
        apollo/persistence/tests/test_clarification_model.py \
        tests/database/test_apollo_clarifications_migration.py \
        tests/database/_apollo_db_fixtures.py
git commit -m "feat(apollo): add apollo_clarifications table + ORM model (clarification loop)"
```

---

### Task 2: Add the `clarification` resolution method @0.90

**Files:**
- Modify: `apollo/resolution/candidates.py` (lines 24–44: `RESOLUTION_METHODS`, `METHOD_CONFIDENCE_CAP`)
- Test: `apollo/resolution/tests/test_candidates.py` (add cases)

**Interfaces:**
- Produces: `"clarification"` ∈ `RESOLUTION_METHODS`; `METHOD_CONFIDENCE_CAP["clarification"] == 0.90`. Consumed by Task 3 (resolver applies the method).

- [ ] **Step 1: Write the failing test**

Add to `apollo/resolution/tests/test_candidates.py`:

```python
from apollo.resolution.candidates import METHOD_CONFIDENCE_CAP, RESOLUTION_METHODS


def test_clarification_method_registered_at_0_90():
    assert "clarification" in RESOLUTION_METHODS
    assert METHOD_CONFIDENCE_CAP["clarification"] == 0.90


def test_clarification_sits_below_proven_tiers_above_floor():
    # Strong AI-read judgment: below derived(0.95)/alias(0.92), above the 0.85
    # normalization-confidence abstention floor, and above fuzzy(0.80)/llm(0.75).
    cap = METHOD_CONFIDENCE_CAP
    assert cap["alias"] > cap["clarification"] > cap["fuzzy"]
    assert cap["clarification"] >= 0.85
```

- [ ] **Step 2: Run it — expect fail**

Run: `pytest apollo/resolution/tests/test_candidates.py::test_clarification_method_registered_at_0_90 -v`
Expected: FAIL — `"clarification"` not in tuple / KeyError.

- [ ] **Step 3: Add the method + cap**

In `apollo/resolution/candidates.py`, insert `"clarification"` into `RESOLUTION_METHODS` (after `"alias"`, before `"fuzzy"` — keep the tuple ordered by confidence) and add the cap:

```python
RESOLUTION_METHODS: tuple[str, ...] = (
    "exact",
    "symbolic",
    "derived",
    "alias",
    "clarification",
    "fuzzy",
    "llm",
    "unresolved",
)

METHOD_CONFIDENCE_CAP: dict[str, float] = {
    "exact": 1.00,
    "symbolic": 0.98,
    "derived": 0.95,
    "alias": 0.92,
    "clarification": 0.90,
    "fuzzy": 0.80,
    "llm": 0.75,
    "unresolved": 0.00,
}
```

- [ ] **Step 4: Run tests — expect pass**

Run: `pytest apollo/resolution/tests/test_candidates.py -v`
Expected: all pass (including any existing test that enumerates `RESOLUTION_METHODS` — update those if they assert an exact tuple).

- [ ] **Step 5: Commit**

```bash
git add apollo/resolution/candidates.py apollo/resolution/tests/test_candidates.py
git commit -m "feat(apollo): register clarification resolution method (cap 0.90)"
```

---

### Task 3: `resolve_attempt` accepts authoritative `confirmed_resolutions`

**Files:**
- Modify: `apollo/resolution/resolver.py` (signature lines 139–146; body 153–212; helpers `_resolved` 215–223)
- Test: `apollo/resolution/tests/test_resolver.py`

**Interfaces:**
- Consumes: `METHOD_CONFIDENCE_CAP["clarification"]` (Task 2).
- Produces: `resolve_attempt(student_graph, candidates, *, confirmed_resolutions: dict[str, str] | None = None, llm_adjudicator=None, fuzzy_threshold=0.9, symbolic_mappings=None)`. `confirmed_resolutions` maps `node_id -> candidate_key`. A node in this map whose `candidate_key` is in the set and is `type_compatible` resolves via method `"clarification"` (confidence 0.90), applied **before** the tiers and **authoritative** (overrides any tier hit). Consumed by Task 16 (`done_grading`).

- [ ] **Step 1: Write the failing tests**

Add to `apollo/resolution/tests/test_resolver.py` (reuse the module's existing `_node` / candidate builders):

```python
from apollo.grading.abstention import unresolved_rate_of
from apollo.resolution.resolver import resolve_attempt


def test_confirmed_resolution_resolves_via_clarification_at_0_90():
    graph = _graph([_node("s1", node_type="condition", text="pressure changes")])
    cands = _cands_with("cond.bernoulli", node_type="condition")
    result = resolve_attempt(graph, cands, confirmed_resolutions={"s1": "cond.bernoulli"})
    rn = {r.node_id: r for r in result.resolved}["s1"]
    assert rn.resolution == "resolved"
    assert rn.method == "clarification"
    assert rn.confidence == 0.90
    assert rn.resolved_key == "cond.bernoulli"
    # The whole point: a confirmed node drops OUT of unresolved_rate.
    assert unresolved_rate_of(result) == 0.0


def test_confirmed_resolution_overrides_a_weaker_tier_hit():
    # Even if fuzzy would have matched, clarification is authoritative.
    graph = _graph([_node("s1", node_type="condition", text="slower means higher")])
    cands = _cands_with("cond.bernoulli", node_type="condition", aliases=("slower means higher",))
    result = resolve_attempt(graph, cands, confirmed_resolutions={"s1": "cond.bernoulli"})
    rn = {r.node_id: r for r in result.resolved}["s1"]
    assert rn.method == "clarification"


def test_confirmed_resolution_ignored_when_type_incompatible_or_unknown_key():
    graph = _graph([_node("s1", node_type="condition", text="x")])
    cands = _cands_with("eq.mass", node_type="equation")  # wrong type
    result = resolve_attempt(graph, cands, confirmed_resolutions={"s1": "eq.mass", "s2": "nope"})
    rn = {r.node_id: r for r in result.resolved}["s1"]
    assert rn.resolution == "unresolved"  # type-incompatible -> not credited
```

Add `_graph`, `_cands_with` helpers if the module lacks them (a `KGGraph` wrapping the nodes; a candidate tuple builder). Follow the existing fixtures in the file.

- [ ] **Step 2: Run — expect fail**

Run: `pytest apollo/resolution/tests/test_resolver.py -k confirmed_resolution -v`
Expected: FAIL — `resolve_attempt() got an unexpected keyword argument 'confirmed_resolutions'`.

- [ ] **Step 3: Implement**

In `apollo/resolution/resolver.py`, add the param to the signature and apply confirmed resolutions before the tier loop. Import `type_compatible` is already used (line 202 region via `structural`).

Signature:

```python
def resolve_attempt(
    student_graph: KGGraph,
    candidates: tuple[Candidate, ...],
    *,
    confirmed_resolutions: dict[str, str] | None = None,
    fuzzy_threshold: float = 0.9,
    symbolic_mappings: dict[str, str] | None = None,
) -> ResolutionResult:
```

(Note: `llm_adjudicator` is removed here — that removal is Task 4. If executing Task 3 before Task 4, keep `llm_adjudicator` in the signature and only ADD `confirmed_resolutions`; do not reorder the two tasks' edits. The block below shows the post-Task-3 body assuming `llm_adjudicator` still present.)

Body — after `maps = ...` and the `MAX_STUDENT_NODES` guard, build the authoritative set, then **skip** confirmed nodes in the tier loop and stamp them in step 4:

```python
    confirmed = confirmed_resolutions or {}
    by_key = {c.canonical_key: c for c in candidates}

    # Authoritative clarification resolutions (the student committed an answer to
    # a pointed question). Applied BEFORE the tiers; type-compat still enforced.
    clarified: dict[str, Candidate] = {}
    for node in nodes:
        key = confirmed.get(node.node_id)
        if key is None:
            continue
        cand = by_key.get(key)
        if cand is not None and type_compatible(node.node_type, cand):
            clarified[node.node_id] = cand

    # 1) Content tiers — skip nodes already clarified.
    matches_by_node: dict[str, list[ScoredMatch]] = {}
    for n in nodes:
        if n.node_id in clarified:
            continue
        hit = _content_match(n, candidates, fuzzy_threshold=fuzzy_threshold, symbolic_mappings=maps)
        if hit is not None:
            matches_by_node[n.node_id] = [hit]
```

In step 4 (the per-node result build), add the clarified branch FIRST:

```python
    for n in nodes:
        if n.node_id in clarified:
            resolved_nodes.append(_resolved(n.node_id, clarified[n.node_id], "clarification"))
        elif n.node_id in assigned:
            m = assigned[n.node_id]
            resolved_nodes.append(_resolved(n.node_id, m.candidate, m.method))
        elif n.node_id in llm_resolved and type_compatible(n.node_type, llm_resolved[n.node_id]):
            resolved_nodes.append(_resolved(n.node_id, llm_resolved[n.node_id], "llm"))
        else:
            resolved_nodes.append(_unresolved(n))
```

`_resolved(..., "clarification")` already returns `confidence=METHOD_CONFIDENCE_CAP["clarification"]` = 0.90 (no change to `_resolved`).

- [ ] **Step 4: Run — expect pass**

Run: `pytest apollo/resolution/tests/test_resolver.py -k confirmed_resolution -v`
Expected: 3 passed.

- [ ] **Step 5: Add the abstention-floor interaction test**

Add to `apollo/resolution/tests/test_resolver.py` (or a focused grading test) proving a clarification-confirmed conceptual node clears the nc floor — the spec §13 / brainstorm §5.3 hard constraint:

```python
def test_clarification_confidence_clears_normalization_floor():
    from apollo.grading.abstention import ABSTENTION_THRESHOLDS
    from apollo.grading.normalization_confidence import (
        RESOLUTION_CEILING_DEFAULT,
        _type_normalized_confidence,
    )
    cap = 0.90  # clarification cap
    # Conceptual node: ceiling 0.75 -> type-normalized = min(1.0, 0.90/0.75) = 1.0.
    nc = _type_normalized_confidence("condition", cap)
    assert nc == 1.0
    assert nc >= ABSTENTION_THRESHOLDS["min_normalization_confidence"]  # 0.85
    assert RESOLUTION_CEILING_DEFAULT == 0.75
```

Run: `pytest apollo/resolution/tests/test_resolver.py::test_clarification_confidence_clears_normalization_floor -v` → pass.

- [ ] **Step 6: Commit**

```bash
git add apollo/resolution/resolver.py apollo/resolution/tests/test_resolver.py
git commit -m "feat(apollo): resolve_attempt applies authoritative clarification resolutions (0.90)"
```

---

### Task 4: Remove the silent LLM adjudication

**Files:**
- Modify: `apollo/resolution/resolver.py` (drop `llm_adjudicator` param + step-3 invocation + the `llm` result branch + import lines 30–33)
- Modify: `apollo/resolution/adjudication.py` (retire `adjudicate`, `main_chat_adjudicator`, `Adjudicator`, `ResolutionLLMRequest`, `ResolutionLLMReply`)
- Modify: `apollo/resolution/__init__.py` (drop adjudication re-exports if any)
- Modify: `apollo/handlers/done_grading.py` (line 228 call: drop `llm_adjudicator=main_chat_adjudicator`; drop its import line 72-region)
- Test: `apollo/resolution/tests/test_resolver.py`, delete/replace `apollo/resolution/tests/test_adjudication.py`

**Interfaces:**
- Produces: `resolve_attempt(student_graph, candidates, *, confirmed_resolutions=None, fuzzy_threshold=0.9, symbolic_mappings=None)` — no `llm_adjudicator`. A post-tier, non-clarified node stays `unresolved` (no LLM guess). Consumed by Task 16.

- [ ] **Step 1: Write the failing/guard test**

Add to `apollo/resolution/tests/test_resolver.py`:

```python
import inspect

from apollo.resolution import resolver


def test_resolver_has_no_llm_adjudicator():
    sig = inspect.signature(resolver.resolve_attempt)
    assert "llm_adjudicator" not in sig.parameters


def test_unmatched_node_stays_unresolved_no_llm():
    graph = _graph([_node("s1", node_type="condition", text="totally unrelated prose")])
    cands = _cands_with("cond.bernoulli", node_type="condition")
    result = resolve_attempt(graph, cands)
    rn = {r.node_id: r for r in result.resolved}["s1"]
    assert rn.resolution == "unresolved"
    assert result.llm_calls == 0
```

- [ ] **Step 2: Run — expect fail on the signature guard**

Run: `pytest apollo/resolution/tests/test_resolver.py::test_resolver_has_no_llm_adjudicator -v`
Expected: FAIL (param still present).

- [ ] **Step 3: Strip the adjudicator from the resolver**

In `apollo/resolution/resolver.py`:
- Remove the import block (lines ~30–33) bringing in `Adjudicator`, `ResolutionLLMRequest`/`adjudicate` from `apollo.resolution.adjudication`.
- Remove `llm_adjudicator` from the signature.
- Delete step 3 (lines ~187–194: the `remaining`/`adjudicate` block); set `llm_calls = 0` unconditionally and drop `llm_resolved`.
- In step 4, delete the `elif n.node_id in llm_resolved ...` branch.

Resulting step-3/step-4 region:

```python
    assigned = outcome.assignment

    # No live LLM adjudication: a post-tier, non-clarified node stays unresolved
    # (the clarification loop now occupies the band the silent guess used to).
    llm_calls = 0

    resolved_nodes: list[ResolvedNode] = []
    for n in nodes:
        if n.node_id in clarified:
            resolved_nodes.append(_resolved(n.node_id, clarified[n.node_id], "clarification"))
        elif n.node_id in assigned:
            m = assigned[n.node_id]
            resolved_nodes.append(_resolved(n.node_id, m.candidate, m.method))
        else:
            resolved_nodes.append(_unresolved(n))
```

- [ ] **Step 4: Retire the adjudication module symbols**

In `apollo/resolution/adjudication.py`, delete `adjudicate`, `main_chat_adjudicator`, `ResolutionLLMRequest`, `ResolutionLLMReply`, `Adjudicator`, `_build_messages`, and the `_RESPONSE_FORMAT`/`_PURPOSE` consts. If nothing remains, delete the file and remove its line from any `__init__.py`. Remove the `"llm"` method usages only if no longer reachable — but **keep `"llm"` in `RESOLUTION_METHODS`/`METHOD_CONFIDENCE_CAP`** (harmless; some persisted rows/tests may reference it; removing it is out of scope).

In `apollo/resolution/__init__.py`, drop any re-export of the retired symbols. In `apollo/handlers/done_grading.py`, delete the `from apollo.resolution... import main_chat_adjudicator` (or the `from apollo.resolution.adjudication import main_chat_adjudicator`) and remove `llm_adjudicator=main_chat_adjudicator,` from the `resolve_attempt(...)` call at line 228.

- [ ] **Step 5: Update/delete adjudication tests**

Delete `apollo/resolution/tests/test_adjudication.py` (its subject is gone). Grep for other references and update: `git grep -n "llm_adjudicator\|main_chat_adjudicator\|adjudicate\|ResolutionLLMRequest"` — fix each test that injects a stub adjudicator (they should now call `resolve_attempt` without it and expect `unresolved`).

- [ ] **Step 6: Run the resolution + grading suites**

Run: `pytest apollo/resolution -v && pytest apollo/handlers/tests -k done -v`
Expected: green (after updating the few affected tests).

- [ ] **Step 7: Commit**

```bash
git add apollo/resolution apollo/handlers/done_grading.py
git commit -m "refactor(apollo): remove silent LLM adjudication from resolution path"
```

---

### Task 5: Embedding helper + candidate-embedding cache

**Files:**
- Create: `apollo/clarification/embedding.py`
- Create: `apollo/clarification/__init__.py`
- Test: `apollo/clarification/tests/test_embedding.py`, `apollo/clarification/tests/__init__.py`

**Interfaces:**
- Consumes: `Candidate` (from `apollo.resolution`); `indexing.document_embedder.embed_texts` (lazy import).
- Produces:
  - `Embedder = Callable[[list[str]], list[list[float]]]` (batched).
  - `def default_embedder(texts: list[str]) -> list[list[float]]` (wraps `embed_texts`; lazy import).
  - `def candidate_surface_texts(candidate: Candidate) -> tuple[str, ...]` — `display_name` + `aliases` + `exact_aliases`, deduped, non-empty.
  - `def candidate_set_hash(candidates: tuple[Candidate, ...]) -> str` — deterministic sha256 over identity fields.
  - `class CandidateEmbeddingCache` with `def vectors_for(self, candidates, *, embedder) -> dict[str, list[list[float]]]` (canonical_key → list of surface vectors), memoized per `candidate_set_hash`.
  - `def cosine(a: list[float], b: list[float]) -> float`.

- [ ] **Step 1: Write the failing tests**

Create `apollo/clarification/tests/__init__.py` (empty) and `apollo/clarification/tests/test_embedding.py`:

```python
import math

from apollo.clarification.embedding import (
    CandidateEmbeddingCache,
    candidate_set_hash,
    candidate_surface_texts,
    cosine,
)
from apollo.resolution.candidates import Candidate


def _cand(key, display, aliases=(), exact=()):
    return Candidate(
        canonical_key=key, canon_key=1, node_type="condition", is_misconception=False,
        symbolic=None, aliases=aliases, display_name=display, opposes_key=None, exact_aliases=exact,
    )


def test_surface_texts_dedupes_and_drops_empty():
    c = _cand("k", "Pressure rises", aliases=("Pressure rises", "p up"), exact=("",))
    assert candidate_surface_texts(c) == ("Pressure rises", "p up")


def test_candidate_set_hash_is_stable_and_sensitive():
    a = (_cand("k", "x"),)
    assert candidate_set_hash(a) == candidate_set_hash((_cand("k", "x"),))
    assert candidate_set_hash(a) != candidate_set_hash((_cand("k", "y"),))


def test_cosine_basic():
    assert cosine([1.0, 0.0], [1.0, 0.0]) == 1.0
    assert abs(cosine([1.0, 0.0], [0.0, 1.0])) < 1e-9


def test_cache_embeds_each_surface_once_and_memoizes():
    calls = {"n": 0}

    def stub(texts):
        calls["n"] += 1
        return [[float(len(t)), 1.0] for t in texts]

    cands = (_cand("k1", "abc"), _cand("k2", "de", aliases=("fff",)))
    cache = CandidateEmbeddingCache()
    v1 = cache.vectors_for(cands, embedder=stub)
    v2 = cache.vectors_for(cands, embedder=stub)  # memoized -> no second embed
    assert calls["n"] == 1
    assert set(v1) == {"k1", "k2"}
    assert len(v1["k2"]) == 2  # "de" + "fff"
    assert v1 == v2
```

- [ ] **Step 2: Run — expect import failure**

Run: `pytest apollo/clarification/tests/test_embedding.py -v`
Expected: FAIL (module missing).

- [ ] **Step 3: Implement**

Create `apollo/clarification/embedding.py`:

```python
"""Embedding detector primitives for the Apollo clarification loop.

Meaning-matching (cosine over text-embedding-3-large) only ever ROUTES an idea
into the 'ask the student' bucket — it never grants credit (spec §4). Candidate
surface embeddings are precomputed and memoized per candidate-set hash so the
detector pays one batched student-side embed per turn.
"""

from __future__ import annotations

import hashlib
import json
import math
from typing import Callable

from apollo.resolution.candidates import Candidate

Embedder = Callable[[list[str]], list[list[float]]]


def default_embedder(texts: list[str]) -> list[list[float]]:
    """Project-wide text-embedding-3-large path (batched). Lazy-imported so test
    collection never touches the OpenAI SDK."""
    from indexing.document_embedder import embed_texts

    return embed_texts(texts)


def candidate_surface_texts(candidate: Candidate) -> tuple[str, ...]:
    """The texts whose meaning identifies this candidate: display name + aliases
    + exact aliases, order-preserving dedupe, empties dropped."""
    seen: dict[str, None] = {}
    for t in (candidate.display_name, *candidate.aliases, *candidate.exact_aliases):
        t = (t or "").strip()
        if t and t not in seen:
            seen[t] = None
    return tuple(seen)


def candidate_set_hash(candidates: tuple[Candidate, ...]) -> str:
    """Deterministic sha256 over the candidate identity fields — the cache key.
    Tracks the same invalidation surface as the reference (the candidate set is
    derived from the reference + misconceptions)."""
    payload = sorted(
        [c.canonical_key, str(c.node_type), c.display_name, list(c.aliases), list(c.exact_aliases)]
        for c in candidates
    )
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return "clarcache-v1:" + hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def cosine(a: list[float], b: list[float]) -> float:
    num = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return num / (na * nb)


class CandidateEmbeddingCache:
    """Memoizes candidate surface embeddings per candidate-set hash."""

    def __init__(self) -> None:
        self._by_hash: dict[str, dict[str, list[list[float]]]] = {}

    def vectors_for(
        self, candidates: tuple[Candidate, ...], *, embedder: Embedder
    ) -> dict[str, list[list[float]]]:
        key = candidate_set_hash(candidates)
        cached = self._by_hash.get(key)
        if cached is not None:
            return cached
        flat_texts: list[str] = []
        spans: list[tuple[str, int, int]] = []
        for c in candidates:
            surfaces = candidate_surface_texts(c)
            start = len(flat_texts)
            flat_texts.extend(surfaces)
            spans.append((c.canonical_key, start, len(flat_texts)))
        vectors = embedder(flat_texts) if flat_texts else []
        result: dict[str, list[list[float]]] = {}
        for canonical_key, start, end in spans:
            result.setdefault(canonical_key, []).extend(vectors[start:end])
        self._by_hash[key] = result
        return result
```

Create `apollo/clarification/__init__.py`:

```python
"""Apollo clarification loop (G2): embeddings notice -> student confirms."""

from apollo.clarification.embedding import (
    CandidateEmbeddingCache,
    Embedder,
    candidate_set_hash,
    candidate_surface_texts,
    cosine,
    default_embedder,
)

__all__ = [
    "CandidateEmbeddingCache",
    "Embedder",
    "candidate_set_hash",
    "candidate_surface_texts",
    "cosine",
    "default_embedder",
]
```

- [ ] **Step 4: Run — expect pass**

Run: `pytest apollo/clarification/tests/test_embedding.py -v`
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add apollo/clarification/
git commit -m "feat(apollo): candidate-embedding cache + cosine detector primitives"
```

---

### Task 6: The detector — embedding banding

**Files:**
- Create: `apollo/clarification/detector.py`
- Modify: `apollo/resolution/resolver.py` (add public `find_residual_nodes`), `apollo/resolution/__init__.py` (export it)
- Test: `apollo/clarification/tests/test_detector.py`, `apollo/resolution/tests/test_resolver.py`

**Interfaces:**
- Consumes: `student_surface_text` (tiers), `CandidateEmbeddingCache`, `cosine`, `Embedder` (Task 5).
- Produces:
  - In resolver: `def find_residual_nodes(nodes, candidates, *, fuzzy_threshold=0.9, symbolic_mappings=None) -> list[Node]` — the nodes no deterministic tier confidently matched (wraps `_content_match`).
  - `@dataclass(frozen=True) class FlaggedNode: node: Node; candidate: Candidate; cosine: float`.
  - `T_AMBIG = 0.50`.
  - `def detect_ambiguous_nodes(residual_nodes, candidates, *, embedder, cache, t_ambig=T_AMBIG) -> list[FlaggedNode]`. Empty list on empty input or any embedder exception (fail-safe).

- [ ] **Step 1: Write the failing tests**

Add to `apollo/resolution/tests/test_resolver.py`:

```python
def test_find_residual_nodes_returns_only_unmatched():
    from apollo.resolution.resolver import find_residual_nodes
    matched = _node("s1", node_type="condition", text="exact alias text")
    residual = _node("s2", node_type="condition", text="some paraphrase")
    cands = _cands_with("cond.k", node_type="condition", aliases=("exact alias text",))
    out = find_residual_nodes([matched, residual], cands)
    assert [n.node_id for n in out] == ["s2"]
```

Create `apollo/clarification/tests/test_detector.py`:

```python
from apollo.clarification.detector import FlaggedNode, T_AMBIG, detect_ambiguous_nodes
from apollo.clarification.embedding import CandidateEmbeddingCache
from apollo.resolution.candidates import Candidate
# Reuse the resolution test node builder.
from apollo.resolution.tests.test_resolver import _node


def _cand(key, display):
    return Candidate(canonical_key=key, canon_key=1, node_type="condition",
                     is_misconception=False, symbolic=None, aliases=(), display_name=display,
                     opposes_key=None, exact_aliases=())


def _embedder_for(mapping):
    # Deterministic: map known text -> vector; default orthogonal.
    def stub(texts):
        return [mapping.get(t, [0.0, 1.0]) for t in texts]
    return stub


def test_flags_node_in_ambiguous_band_with_top_candidate():
    node = _node("s1", node_type="condition", text="pressure drops when faster")
    cand = _cand("cond.bernoulli", "pressure is lower where flow is faster")
    emb = _embedder_for({
        "pressure drops when faster": [1.0, 0.0],
        "pressure is lower where flow is faster": [0.9, 0.1],  # cosine ~0.994 >= 0.50
    })
    flagged = detect_ambiguous_nodes([node], (cand,), embedder=emb, cache=CandidateEmbeddingCache())
    assert len(flagged) == 1
    assert flagged[0].candidate.canonical_key == "cond.bernoulli"
    assert flagged[0].cosine >= T_AMBIG


def test_leaves_node_below_band():
    node = _node("s1", node_type="condition", text="off topic")
    cand = _cand("cond.bernoulli", "pressure lower where faster")
    emb = _embedder_for({"off topic": [0.0, 1.0], "pressure lower where faster": [1.0, 0.0]})
    assert detect_ambiguous_nodes([node], (cand,), embedder=emb, cache=CandidateEmbeddingCache()) == []


def test_empty_inputs_and_embedder_failure_are_no_ops():
    cand = _cand("k", "x")
    assert detect_ambiguous_nodes([], (cand,), embedder=lambda t: [], cache=CandidateEmbeddingCache()) == []

    def boom(texts):
        raise RuntimeError("openai 503")

    node = _node("s1", node_type="condition", text="anything")
    assert detect_ambiguous_nodes([node], (cand,), embedder=boom, cache=CandidateEmbeddingCache()) == []
```

- [ ] **Step 2: Run — expect fail**

Run: `pytest apollo/clarification/tests/test_detector.py apollo/resolution/tests/test_resolver.py::test_find_residual_nodes_returns_only_unmatched -v`
Expected: FAIL (modules/functions missing).

- [ ] **Step 3: Implement `find_residual_nodes` in resolver.py**

```python
def find_residual_nodes(
    nodes: list[Node],
    candidates: tuple[Candidate, ...],
    *,
    fuzzy_threshold: float = 0.9,
    symbolic_mappings: dict[str, str] | None = None,
) -> list[Node]:
    """Nodes no deterministic tier confidently matched (the clarification
    detector's input). Pure: reuses ``_content_match`` so banding stays in
    lockstep with grading-time resolution."""
    maps = symbolic_mappings if symbolic_mappings is not None else {}
    residual: list[Node] = []
    for n in nodes:
        if _content_match(n, candidates, fuzzy_threshold=fuzzy_threshold, symbolic_mappings=maps) is None:
            residual.append(n)
    return residual
```

Export it from `apollo/resolution/__init__.py` (`from apollo.resolution.resolver import ..., find_residual_nodes` and add to `__all__`).

Create `apollo/clarification/detector.py`:

```python
"""Embedding-similarity detector — flags residual student nodes that are
plausibly (not confidently) a candidate idea, for an answer-blind follow-up.
High recall by design: a false positive costs only a question the student
dismisses. It NEVER credits (spec §4)."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from apollo.clarification.embedding import CandidateEmbeddingCache, Embedder, cosine
from apollo.knowledge_graph.schema import Node  # adjust to the actual Node import path
from apollo.resolution.candidates import Candidate
from apollo.resolution.tiers import student_surface_text

_LOG = logging.getLogger(__name__)

T_AMBIG = 0.50  # calibration default (spec §15); recall-tuned, tolerant precision.


@dataclass(frozen=True)
class FlaggedNode:
    node: Node
    candidate: Candidate
    cosine: float


def detect_ambiguous_nodes(
    residual_nodes: list[Node],
    candidates: tuple[Candidate, ...],
    *,
    embedder: Embedder,
    cache: CandidateEmbeddingCache,
    t_ambig: float = T_AMBIG,
) -> list[FlaggedNode]:
    """For each residual node, the top candidate by max cosine over its surface
    forms; flag when cosine >= t_ambig. Fail-safe: empty list on any embedder
    error (the turn proceeds with no probe)."""
    if not residual_nodes or not candidates:
        return []
    try:
        cand_vectors = cache.vectors_for(candidates, embedder=embedder)
        texts = [student_surface_text(n) for n in residual_nodes]
        node_vectors = embedder(texts)
    except Exception as exc:  # noqa: BLE001 - fail safe, never block teaching
        _LOG.warning("clarification_detect_embed_failed error=%s", exc)
        return []

    by_key = {c.canonical_key: c for c in candidates}
    flagged: list[FlaggedNode] = []
    for node, nvec in zip(residual_nodes, node_vectors):
        best_key, best_cos = None, -1.0
        for key, surfaces in cand_vectors.items():
            for svec in surfaces:
                c = cosine(nvec, svec)
                if c > best_cos:
                    best_cos, best_key = c, key
        if best_key is not None and best_cos >= t_ambig:
            flagged.append(FlaggedNode(node=node, candidate=by_key[best_key], cosine=best_cos))
    return flagged
```

**Note:** confirm the real `Node` import path during Step 3 — the resolution code imports `Node` from the KG schema module (`git grep -n "^from .*import .*\bNode\b" apollo/resolution/tiers.py`). Use that exact path.

- [ ] **Step 4: Run — expect pass**

Run: `pytest apollo/clarification/tests/test_detector.py apollo/resolution/tests/test_resolver.py -k "residual or detector or confirmed" -v`
Expected: pass.

- [ ] **Step 5: Commit**

```bash
git add apollo/clarification/detector.py apollo/resolution/resolver.py apollo/resolution/__init__.py apollo/clarification/tests/test_detector.py apollo/resolution/tests/test_resolver.py
git commit -m "feat(apollo): embedding-similarity ambiguous-node detector + residual filter"
```

---

### Task 7: Answer-blind probe-hint construction

**Files:**
- Create: `apollo/clarification/probe.py`
- Test: `apollo/clarification/tests/test_probe.py`

**Interfaces:**
- Consumes: `Node`, `Candidate`.
- Produces: `def build_probe_hint(node: Node, candidate: Candidate) -> str` — a short steering instruction naming the **dimension to pin down**, derived from `node.node_type`; it MUST NOT contain the candidate's claim/value/display_name/aliases/symbolic.

- [ ] **Step 1: Write the failing tests (the no-leak guarantee is the core assertion)**

Create `apollo/clarification/tests/test_probe.py`:

```python
from apollo.clarification.probe import build_probe_hint
from apollo.resolution.candidates import Candidate
from apollo.resolution.tests.test_resolver import _node


def _cand(node_type, display, aliases=(), symbolic=None):
    return Candidate(canonical_key="cond.k", canon_key=1, node_type=node_type,
                     is_misconception=False, symbolic=symbolic, aliases=aliases,
                     display_name=display, opposes_key=None, exact_aliases=())


def test_hint_never_leaks_candidate_value():
    cand = _cand("condition", "pressure is LOWER where flow is faster",
                 aliases=("inverse pressure-velocity",), symbolic="P+0.5*rho*v^2=const")
    node = _node("s1", node_type="condition", text="pressure and speed are related")
    hint = build_probe_hint(node, cand)
    leaky = ["LOWER", "lower", "inverse pressure-velocity", "P+0.5", cand.display_name, *cand.aliases]
    for token in leaky:
        assert token not in hint
    assert hint  # non-empty steering


def test_hint_names_the_dimension_per_node_type():
    assert "direction" in build_probe_hint(_node("s1", node_type="condition", text="x"),
                                           _cand("condition", "d")).lower()
    assert "variable" in build_probe_hint(_node("s2", node_type="equation", text="x"),
                                          _cand("equation", "d")).lower()
    assert "define" in build_probe_hint(_node("s3", node_type="definition", text="x"),
                                        _cand("definition", "d")).lower()
```

- [ ] **Step 2: Run — expect fail**

Run: `pytest apollo/clarification/tests/test_probe.py -v`
Expected: FAIL (module missing).

- [ ] **Step 3: Implement**

Create `apollo/clarification/probe.py`:

```python
"""Answer-blind probe hints. Rule (spec §6.4): reveal the DIMENSION to pin down,
never the value. The hint is steering for the confused-classmate generator; it
must never carry the candidate's claim — the student already named the entities,
so we only ask them to COMMIT, not confirm a revealed answer."""

from __future__ import annotations

from apollo.knowledge_graph.schema import Node  # match detector.py's Node import
from apollo.resolution.candidates import Candidate

# Per node type: the dimension to make the student commit to. NONE of these
# strings reference the specific candidate — only the kind of thing to pin down.
_HINT_BY_TYPE: dict[str, str] = {
    "condition": "Make the student commit to the DIRECTION of the relationship "
    "they just described (which way it goes), without telling them which is correct.",
    "equation": "Ask which VARIABLE they would solve for, or how two quantities "
    "trade off, without stating the relationship yourself.",
    "simplification": "Ask under what CONDITION the step they described applies, "
    "without naming the condition.",
    "definition": "Ask the student to DEFINE the term in their own words, without "
    "giving the definition.",
    "procedure_step": "Ask the student to state the next ACTION explicitly, without "
    "performing it for them.",
    "variable_mapping": "Ask which real-world quantity their symbol stands for, "
    "without mapping it yourself.",
}

_FALLBACK = (
    "Ask the student to make their last idea more precise and commit to a specific "
    "claim, without telling them what the right answer is."
)


def build_probe_hint(node: Node, candidate: Candidate) -> str:
    """Answer-free steering string for one flagged idea. Derived purely from the
    node type; the ``candidate`` arg disambiguates which idea is being probed for
    the caller's bookkeeping but is intentionally NOT rendered into the hint."""
    return _HINT_BY_TYPE.get(node.node_type, _FALLBACK)
```

- [ ] **Step 4: Run — expect pass**

Run: `pytest apollo/clarification/tests/test_probe.py -v`
Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add apollo/clarification/probe.py apollo/clarification/tests/test_probe.py
git commit -m "feat(apollo): answer-blind probe-hint construction (dimension, never value)"
```

---

### Task 8: Pacing & prioritization

**Files:**
- Create: `apollo/clarification/pacing.py`
- Test: `apollo/clarification/tests/test_pacing.py`

**Interfaces:**
- Consumes: `FlaggedNode` (Task 6).
- Produces:
  - `MAX_PROBES_PER_TURN = 3`.
  - `def rubric_weight_for(node_type: str) -> float` — maps node type → axis → `AXIS_WEIGHTS` (0.0 for ungraded types).
  - `def select_probes(flagged: list[FlaggedNode], *, limit: int = MAX_PROBES_PER_TURN) -> list[FlaggedNode]` — dedupe by `node.node_id` (1/idea), sort by `(rubric_weight desc, cosine desc)`, take `limit`.

- [ ] **Step 1: Write the failing tests**

Create `apollo/clarification/tests/test_pacing.py`:

```python
from apollo.clarification.detector import FlaggedNode
from apollo.clarification.pacing import MAX_PROBES_PER_TURN, rubric_weight_for, select_probes
from apollo.resolution.candidates import Candidate
from apollo.resolution.tests.test_resolver import _node


def _flag(node_id, node_type, cos):
    n = _node(node_id, node_type=node_type, text="t")
    c = Candidate(canonical_key="k", canon_key=1, node_type=node_type, is_misconception=False,
                  symbolic=None, aliases=(), display_name="d", opposes_key=None, exact_aliases=())
    return FlaggedNode(node=n, candidate=c, cosine=cos)


def test_rubric_weight_prefers_graded_axes():
    assert rubric_weight_for("procedure_step") > rubric_weight_for("condition") > 0.0
    assert rubric_weight_for("equation") == 0.0  # ungraded axis -> falls back to cosine


def test_caps_at_three_and_orders_by_weight_then_cosine():
    flags = [
        _flag("a", "definition", 0.99),    # weight 0.0
        _flag("b", "procedure_step", 0.51),  # highest weight
        _flag("c", "condition", 0.80),
        _flag("d", "condition", 0.90),
    ]
    out = select_probes(flags)
    assert len(out) == MAX_PROBES_PER_TURN
    assert [f.node.node_id for f in out] == ["b", "d", "c"]  # proc > cond(0.90) > cond(0.80)


def test_one_per_idea_dedupe_by_node_id():
    flags = [_flag("a", "condition", 0.9), _flag("a", "condition", 0.95)]
    out = select_probes(flags)
    assert [f.node.node_id for f in out] == ["a"]
    assert out[0].cosine == 0.95  # keeps the stronger duplicate
```

- [ ] **Step 2: Run — expect fail**

Run: `pytest apollo/clarification/tests/test_pacing.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement**

Create `apollo/clarification/pacing.py`:

```python
"""Pacing for the clarification loop (spec §6.5): at most 3 follow-ups per turn,
one per idea, prioritized by rubric weight then detector cosine."""

from __future__ import annotations

from apollo.clarification.detector import FlaggedNode
from apollo.overseer.rubric import AXIS_WEIGHTS, _axis_for

MAX_PROBES_PER_TURN = 3


def rubric_weight_for(node_type: str) -> float:
    """The graded-axis weight for a node type (0.0 for ungraded types — those
    fall back to cosine ordering)."""
    axis = _axis_for(node_type)
    if axis is None:
        return 0.0
    return AXIS_WEIGHTS.get(axis, 0.0)


def select_probes(
    flagged: list[FlaggedNode], *, limit: int = MAX_PROBES_PER_TURN
) -> list[FlaggedNode]:
    """One per idea (dedupe by node_id, keeping the strongest cosine), then the
    top ``limit`` by (rubric weight desc, cosine desc)."""
    best_by_node: dict[str, FlaggedNode] = {}
    for f in flagged:
        cur = best_by_node.get(f.node.node_id)
        if cur is None or f.cosine > cur.cosine:
            best_by_node[f.node.node_id] = f
    ordered = sorted(
        best_by_node.values(),
        key=lambda f: (rubric_weight_for(f.node.node_type), f.cosine),
        reverse=True,
    )
    return ordered[:limit]
```

- [ ] **Step 4: Run — expect pass**

Run: `pytest apollo/clarification/tests/test_pacing.py -v`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add apollo/clarification/pacing.py apollo/clarification/tests/test_pacing.py
git commit -m "feat(apollo): clarification pacing (<=3/turn, 1/idea, rubric-weight priority)"
```

---

### Task 9: `draft_reply` gains an answer-blind `clarification_hints` kwarg

**Files:**
- Modify: `apollo/agent/apollo_llm.py` (`draft_reply`, signature lines 93–100; message assembly 118–138)
- Test: `apollo/agent/tests/test_apollo_llm.py` (add cases)

**Interfaces:**
- Produces: `draft_reply(history, kg_summary, *, problem_text=None, model=None, history_summary=None, clarification_hints: list[str] | None = None) -> str`. When hints are present, they are appended as one extra **system** message steering Apollo to weave the question(s) into its confused reply; absent → byte-identical to today.

- [ ] **Step 1: Write the failing tests**

Add to `apollo/agent/tests/test_apollo_llm.py` (patch the OpenAI client the module uses, as the existing tests do):

```python
def test_clarification_hints_added_as_system_message(monkeypatch):
    captured = {}

    class _Resp:
        choices = [type("C", (), {"message": type("M", (), {"content": "ok"})()})()]

    def _fake_create(**kwargs):
        captured["messages"] = kwargs["messages"]
        return _Resp()

    # Mirror how the existing apollo_llm tests stub the client.
    monkeypatch.setattr("apollo.agent.apollo_llm.OpenAI", lambda: type(
        "X", (), {"chat": type("Y", (), {"completions": type("Z", (), {"create": staticmethod(_fake_create)})()})()})())

    draft_reply(history=[{"role": "user", "content": "hi"}], kg_summary="k",
                clarification_hints=["Make the student commit to the DIRECTION."])
    sys_texts = [m["content"] for m in captured["messages"] if m["role"] == "system"]
    assert any("DIRECTION" in t for t in sys_texts)


def test_no_hints_is_unchanged(monkeypatch):
    captured = {}
    # ... same stub as above ...
    draft_reply(history=[{"role": "user", "content": "hi"}], kg_summary="k")
    sys_texts = [m["content"] for m in captured["messages"] if m["role"] == "system"]
    assert all("clarif" not in t.lower() for t in sys_texts)
```

(Use whatever client-stub helper the existing `test_apollo_llm.py` already defines — reuse it rather than re-deriving the monkeypatch.)

- [ ] **Step 2: Run — expect fail**

Run: `pytest apollo/agent/tests/test_apollo_llm.py -k clarification -v`
Expected: FAIL.

- [ ] **Step 3: Implement**

In `apollo/agent/apollo_llm.py`, add the kwarg and a system message. Place a module constant near `APOLLO_SYSTEM_PROMPT`:

```python
_CLARIFICATION_PREFIX = (
    "You have a few things you're unsure about and want to ask your study partner. "
    "Work these clarifying questions naturally into your reply, in your own confused "
    "voice. Ask them to commit to a specific answer; do NOT state the answer yourself:\n"
)
```

Signature → add `clarification_hints: list[str] | None = None`. In the message-assembly block, before `messages.extend(history)`:

```python
    if clarification_hints:
        joined = "\n".join(f"- {h}" for h in clarification_hints)
        messages.append({"role": "system", "content": _CLARIFICATION_PREFIX + joined})
```

- [ ] **Step 4: Run — expect pass**

Run: `pytest apollo/agent/tests/test_apollo_llm.py -v`
Expected: pass.

- [ ] **Step 5: Commit**

```bash
git add apollo/agent/apollo_llm.py apollo/agent/tests/test_apollo_llm.py
git commit -m "feat(apollo): draft_reply accepts answer-blind clarification_hints"
```

---

### Task 10: Shared candidate-set assembly helper

**Files:**
- Create: `apollo/clarification/candidate_assembly.py`
- Test: `apollo/clarification/tests/test_candidate_assembly.py`

**Interfaces:**
- Consumes: `load_for_concept` (`apollo.overseer.misconception_bank`), `_misconceptions_dict` (`apollo.handlers.done_grading`), `load_entity_specs` (`apollo.knowledge_graph.canon_projection`), `build_problem_candidates` (`apollo.graph_compare.problem_inputs`).
- Produces: `async def load_problem_candidates(db, *, search_space_id: int, concept_id: int | None, problem_payload: dict) -> ProblemInputs` — the same `candidates` + `symbolic_mappings` the Done path builds (recipe from `done_grading.py:186–219`), reusable by the chat path.

- [ ] **Step 1: Write the failing test**

Create `apollo/clarification/tests/test_candidate_assembly.py`:

```python
import pytest

from apollo.clarification.candidate_assembly import load_problem_candidates


async def test_assembles_candidates_from_problem_and_bank(monkeypatch):
    # Stub the three async loaders so no DB/LLM is touched.
    async def fake_load_for_concept(db, *, concept_id):
        return []  # empty bank -> only reference candidates

    class _Spec:
        def __init__(self, ck, k): self.canonical_key, self.key = ck, k

    async def fake_load_entity_specs(db, *, search_space_id, concept_id):
        return [_Spec("cond.bernoulli", 7)]

    monkeypatch.setattr("apollo.clarification.candidate_assembly.load_for_concept", fake_load_for_concept)
    monkeypatch.setattr("apollo.clarification.candidate_assembly.load_entity_specs", fake_load_entity_specs)

    problem = {"reference_solution": [
        {"entry_type": "condition", "canonical_key": "cond.bernoulli",
         "content": {"applies_when": "flow is faster", "aliases": []}},
    ]}
    inputs = await load_problem_candidates(object(), search_space_id=1, concept_id=2, problem_payload=problem)
    keys = {c.canonical_key for c in inputs.candidates}
    assert "cond.bernoulli" in keys
```

- [ ] **Step 2: Run — expect fail**

Run: `pytest apollo/clarification/tests/test_candidate_assembly.py -v`
Expected: FAIL (module missing).

- [ ] **Step 3: Implement**

Create `apollo/clarification/candidate_assembly.py`:

```python
"""Shared candidate-set assembly. The chat (clarification) path and the Done
(grading) path build the SAME closed candidate set; this centralizes the recipe
that previously lived inline in done_grading.py (load bank -> dict -> specs ->
build_problem_candidates) so both call one function (DRY)."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from apollo.graph_compare.problem_inputs import ProblemInputs, build_problem_candidates
from apollo.handlers.done_grading import _misconceptions_dict
from apollo.knowledge_graph.canon_projection import load_entity_specs
from apollo.overseer.misconception_bank import load_for_concept


async def load_problem_candidates(
    db: AsyncSession,
    *,
    search_space_id: int,
    concept_id: int | None,
    problem_payload: dict,
) -> ProblemInputs:
    """Assemble the closed candidate set (reference nodes + course misconceptions)
    plus the per-problem symbolic mappings, exactly as the grading path does."""
    entries = await load_for_concept(db, concept_id=concept_id)
    misconceptions = _misconceptions_dict(entries)
    specs = await load_entity_specs(db, search_space_id=search_space_id, concept_id=concept_id)
    canon_key_by_canonical_key = {spec.canonical_key: spec.key for spec in specs}
    return build_problem_candidates(
        problem_payload, misconceptions, canon_key_by_canonical_key=canon_key_by_canonical_key
    )
```

(If importing `_misconceptions_dict` from `done_grading` risks a future import cycle once chat wiring lands, promote it to a small public helper in `apollo/graph_compare/problem_inputs.py` and import from there instead. Verify no cycle with `python -c "import apollo.clarification.candidate_assembly"`.)

- [ ] **Step 4: Run — expect pass**

Run: `pytest apollo/clarification/tests/test_candidate_assembly.py -v`
Expected: pass.

- [ ] **Step 5: Commit**

```bash
git add apollo/clarification/candidate_assembly.py apollo/clarification/tests/test_candidate_assembly.py
git commit -m "feat(apollo): shared candidate-set assembly helper (chat + done reuse)"
```

---

### Task 11: The re-scorer (three-way verdict over a committed answer)

**Files:**
- Create: `apollo/clarification/rescorer.py`
- Test: `apollo/clarification/tests/test_rescorer.py`

**Interfaces:**
- Consumes: `main_chat` (`apollo.agent._llm`) for the default judge.
- Produces:
  - `RescoreOutcome = Literal["confirmed", "refuted", "vague"]`.
  - `@dataclass(frozen=True) class ClarificationRequest: original_statement: str; clarification_text: str; candidate_display: str`.
  - `ClarificationJudge = Callable[[ClarificationRequest], RescoreOutcome]`.
  - `def default_clarification_judge(request) -> RescoreOutcome` (one `main_chat` call, json, temp 0; raises `ResolutionUnavailableError`-style named error on infra failure — reuse the existing apollo error type).
  - `def rescore_clarification(*, original_statement, clarification_text, candidate_display, judge) -> RescoreOutcome`.

- [ ] **Step 1: Write the failing tests**

Create `apollo/clarification/tests/test_rescorer.py`:

```python
import pytest

from apollo.clarification.rescorer import (
    ClarificationRequest,
    rescore_clarification,
)


def _judge(outcome):
    def fn(request: ClarificationRequest):
        assert request.clarification_text  # judge sees the committed answer
        return outcome
    return fn


@pytest.mark.parametrize("outcome", ["confirmed", "refuted", "vague"])
def test_passes_through_three_way_verdict(outcome):
    got = rescore_clarification(
        original_statement="pressure and speed are related",
        clarification_text="pressure is lower where it moves faster",
        candidate_display="inverse pressure-velocity",
        judge=_judge(outcome),
    )
    assert got == outcome


def test_judge_failure_propagates_named_error():
    from apollo.errors import ResolutionUnavailableError

    def boom(request):
        raise ResolutionUnavailableError(stage="clarification_rescore", last_error="503")

    with pytest.raises(ResolutionUnavailableError):
        rescore_clarification(
            original_statement="o", clarification_text="c", candidate_display="d", judge=boom,
        )
```

- [ ] **Step 2: Run — expect fail**

Run: `pytest apollo/clarification/tests/test_rescorer.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement**

Create `apollo/clarification/rescorer.py`:

```python
"""Re-score a clarified idea (spec §7). Reads (original ambiguous statement +
the student's committed clarification + the one candidate idea) and rules
correct / wrong / vague. This is NOT the deleted silent guess (which guessed
from nothing): it judges a committed answer to a pointed question — far more
decidable. DI'd + stubbed in tests; no live model in CI."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Callable, Literal

from apollo.agent._llm import main_chat
from apollo.errors import ResolutionUnavailableError

_LOG = logging.getLogger(__name__)

RescoreOutcome = Literal["confirmed", "refuted", "vague"]
_RESPONSE_FORMAT = {"type": "json_object"}
_PURPOSE = "clarification_rescore"
_VALID = {"confirmed", "refuted", "vague"}


@dataclass(frozen=True)
class ClarificationRequest:
    original_statement: str
    clarification_text: str
    candidate_display: str


ClarificationJudge = Callable[[ClarificationRequest], RescoreOutcome]


def _build_messages(request: ClarificationRequest) -> list[dict[str, str]]:
    system = (
        "You judge whether a student's clarified explanation matches a target idea. "
        "Reply strict JSON {\"verdict\": \"confirmed\"|\"refuted\"|\"vague\"}. "
        "confirmed = the clarification correctly expresses the target idea; "
        "refuted = it states the opposite or a wrong claim; "
        "vague = noncommittal / unclear. Judge meaning, not wording."
    )
    user = json.dumps({
        "original_statement": request.original_statement,
        "clarification": request.clarification_text,
        "target_idea": request.candidate_display,
    })
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def default_clarification_judge(request: ClarificationRequest) -> RescoreOutcome:
    try:
        raw = main_chat(
            purpose=_PURPOSE, messages=_build_messages(request),
            response_format=_RESPONSE_FORMAT, temperature=0.0,
        )
        verdict = str(json.loads(raw or "{}").get("verdict", "vague"))
        return verdict if verdict in _VALID else "vague"  # unknown -> no credit
    except ResolutionUnavailableError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ResolutionUnavailableError(stage="clarification_rescore", last_error=str(exc)) from exc


def rescore_clarification(
    *,
    original_statement: str,
    clarification_text: str,
    candidate_display: str,
    judge: ClarificationJudge,
) -> RescoreOutcome:
    return judge(ClarificationRequest(
        original_statement=original_statement,
        clarification_text=clarification_text,
        candidate_display=candidate_display,
    ))
```

Verify `ResolutionUnavailableError(stage=..., last_error=...)` is the actual constructor (it is, per `adjudication.py` usage). Confirm `main_chat` signature matches (`purpose=`, `messages=`, `response_format=`, `temperature=`).

- [ ] **Step 4: Run — expect pass**

Run: `pytest apollo/clarification/tests/test_rescorer.py -v`
Expected: pass.

- [ ] **Step 5: Commit**

```bash
git add apollo/clarification/rescorer.py apollo/clarification/tests/test_rescorer.py
git commit -m "feat(apollo): clarification re-scorer (confirmed/refuted/vague judge)"
```

---

### Task 12: Clarification store (async DB CRUD)

**Files:**
- Create: `apollo/clarification/store.py`
- Test: `tests/database/test_clarification_store.py` (local Postgres `db_session`)

**Interfaces:**
- Consumes: `Clarification` ORM (Task 1).
- Produces:
  - `async def write_asked_waiting(db, *, attempt_id, session_id, user_id, search_space_id, concept_id, node_id, candidate_key, probe_question, original_statement, asked_turn) -> None` (idempotent on `(attempt_id, node_id)` — `ON CONFLICT DO NOTHING`).
  - `async def load_asked_waiting(db, *, attempt_id) -> list[Clarification]`.
  - `async def record_outcome(db, *, clarification_id, state, clarification_text, answered_turn) -> None`.
  - `async def load_confirmed_resolutions(db, *, attempt_id) -> dict[str, str]` (node_id → candidate_key for `state='confirmed'`). Consumed by Task 16.

- [ ] **Step 1: Write the failing test**

Create `tests/database/test_clarification_store.py`:

```python
import pytest

from apollo.clarification import store
from apollo.persistence.models import Clarification

pytestmark = pytest.mark.integration


async def test_write_load_record_confirm_cycle(db_session):
    from tests.database._apollo_db_fixtures import seed_attempt_chain
    ctx = await seed_attempt_chain(db_session)
    await store.write_asked_waiting(
        db_session, attempt_id=ctx.attempt_id, session_id=ctx.session_id, user_id=ctx.user_id,
        search_space_id=ctx.search_space_id, concept_id=ctx.concept_id, node_id="s1",
        candidate_key="cond.bernoulli", probe_question="which way?", original_statement="p~v", asked_turn=2,
    )
    # Idempotent: a second asked_waiting for the same (attempt, node) is a no-op.
    await store.write_asked_waiting(
        db_session, attempt_id=ctx.attempt_id, session_id=ctx.session_id, user_id=ctx.user_id,
        search_space_id=ctx.search_space_id, concept_id=ctx.concept_id, node_id="s1",
        candidate_key="other", probe_question="again?", original_statement="p~v", asked_turn=2,
    )
    waiting = await store.load_asked_waiting(db_session, attempt_id=ctx.attempt_id)
    assert len(waiting) == 1

    await store.record_outcome(
        db_session, clarification_id=waiting[0].id, state="confirmed",
        clarification_text="lower where faster", answered_turn=4,
    )
    confirmed = await store.load_confirmed_resolutions(db_session, attempt_id=ctx.attempt_id)
    assert confirmed == {"s1": "cond.bernoulli"}
    assert await store.load_asked_waiting(db_session, attempt_id=ctx.attempt_id) == []
```

- [ ] **Step 2: Run — expect fail (or skip without Docker)**

Run: `pytest tests/database/test_clarification_store.py -v`
Expected: FAIL (module missing) with Docker; skip without.

- [ ] **Step 3: Implement**

Create `apollo/clarification/store.py`:

```python
"""Persistence for the clarification loop. Idempotent asked_waiting writes (one
follow-up per idea via the UNIQUE(attempt_id, node_id) constraint); terminal
outcome recording; confirmed-resolution loading for grading."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from apollo.persistence.models import Clarification


async def write_asked_waiting(
    db: AsyncSession,
    *,
    attempt_id: int,
    session_id: int,
    user_id: str,
    search_space_id: int,
    concept_id: int | None,
    node_id: str,
    candidate_key: str,
    probe_question: str,
    original_statement: str,
    asked_turn: int,
) -> None:
    """Insert an asked_waiting row; no-op if this (attempt, node) already has one."""
    stmt = (
        pg_insert(Clarification)
        .values(
            attempt_id=attempt_id, session_id=session_id, user_id=user_id,
            search_space_id=search_space_id, concept_id=concept_id, node_id=node_id,
            candidate_key=candidate_key, state="asked_waiting", probe_question=probe_question,
            original_statement=original_statement, asked_turn=asked_turn,
        )
        .on_conflict_do_nothing(constraint="apollo_clarifications_attempt_node_uniq")
    )
    await db.execute(stmt)


async def load_asked_waiting(db: AsyncSession, *, attempt_id: int) -> list[Clarification]:
    rows = await db.execute(
        select(Clarification).where(
            Clarification.attempt_id == attempt_id,
            Clarification.state == "asked_waiting",
        )
    )
    return list(rows.scalars().all())


async def record_outcome(
    db: AsyncSession,
    *,
    clarification_id: int,
    state: str,
    clarification_text: str | None,
    answered_turn: int,
) -> None:
    row = (await db.execute(
        select(Clarification).where(Clarification.id == clarification_id)
    )).scalar_one()
    row.state = state
    row.clarification_text = clarification_text
    row.answered_turn = answered_turn
    row.updated_at = datetime.now(UTC)


async def load_confirmed_resolutions(db: AsyncSession, *, attempt_id: int) -> dict[str, str]:
    rows = await db.execute(
        select(Clarification.node_id, Clarification.candidate_key).where(
            Clarification.attempt_id == attempt_id,
            Clarification.state == "confirmed",
        )
    )
    return {node_id: candidate_key for node_id, candidate_key in rows.all()}
```

**SQLite note:** `pg_insert(...).on_conflict_do_nothing` is Postgres-only. Tests that must run on SQLite (the unit job) should test the store via the `db_session` Postgres fixture (integration), OR add a dialect branch using `sqlite_insert` when `db.bind.dialect.name == "sqlite"`. Since this store is exercised on the Postgres `db_session`, keep the pg form; if a unit-tier test is needed, add the dialect branch in this step.

- [ ] **Step 4: Run — expect pass (or skip)**

Run: `pytest tests/database/test_clarification_store.py -v`
Expected: pass with Docker.

- [ ] **Step 5: Commit**

```bash
git add apollo/clarification/store.py tests/database/test_clarification_store.py
git commit -m "feat(apollo): clarification store (asked_waiting/outcome/confirmed-resolutions)"
```

---

### Task 13: Wire detection + probing into `handle_chat` (live)

**Files:**
- Modify: `apollo/handlers/chat.py` (seam after `student_graph` at line 284, before `draft_reply` at 289)
- Create: `apollo/clarification/turn.py` (orchestration, kept out of the handler so it is unit-testable without the DB)
- Test: `apollo/clarification/tests/test_turn.py`, `apollo/handlers/tests/test_chat_clarification.py`

**Interfaces:**
- Consumes: `load_problem_candidates` (10), `find_residual_nodes` (6), `detect_ambiguous_nodes` (6), `build_probe_hint` (7), `select_probes` (8), `write_asked_waiting` (12), `draft_reply` `clarification_hints` (9).
- Produces: `async def run_clarification_detection(db, *, parsed_nodes, candidates, symbolic_mappings, embedder, cache, attempt_id, session_id, user_id, search_space_id, concept_id, asked_turn) -> list[str]` — returns the probe **hints** to pass into `draft_reply`, and persists the `asked_waiting` rows. Fail-safe: returns `[]` on any internal failure.

- [ ] **Step 1: Write the failing orchestration test**

Create `apollo/clarification/tests/test_turn.py` (pure, stubbed embedder + an in-memory store double or monkeypatched `write_asked_waiting`):

```python
from apollo.clarification import turn
from apollo.clarification.embedding import CandidateEmbeddingCache
from apollo.resolution.candidates import Candidate
from apollo.resolution.tests.test_resolver import _node


def _cand(key, display, node_type="condition"):
    return Candidate(canonical_key=key, canon_key=1, node_type=node_type, is_misconception=False,
                     symbolic=None, aliases=(), display_name=display, opposes_key=None, exact_aliases=())


async def test_detection_returns_hints_and_persists(monkeypatch):
    writes = []

    async def fake_write(db, **kw):
        writes.append(kw)

    monkeypatch.setattr(turn, "write_asked_waiting", fake_write)

    node = _node("s1", node_type="condition", text="pressure and speed related")
    cand = _cand("cond.bernoulli", "pressure lower where faster")

    def emb(texts):
        # student text and candidate surface both ~ [1,0] -> high cosine.
        return [[1.0, 0.0] for _ in texts]

    hints = await turn.run_clarification_detection(
        db=object(), parsed_nodes=[node], candidates=(cand,), symbolic_mappings={},
        embedder=emb, cache=CandidateEmbeddingCache(), attempt_id=1, session_id=1,
        user_id="u", search_space_id=1, concept_id=2, asked_turn=2,
    )
    assert hints and "direction" in hints[0].lower()
    assert len(writes) == 1
    assert writes[0]["node_id"] == "s1"
    assert writes[0]["candidate_key"] == "cond.bernoulli"


async def test_detection_failsafe_returns_empty(monkeypatch):
    def boom(texts):
        raise RuntimeError("503")

    hints = await turn.run_clarification_detection(
        db=object(), parsed_nodes=[_node("s1", node_type="condition", text="x")],
        candidates=(_cand("k", "d"),), symbolic_mappings={}, embedder=boom,
        cache=CandidateEmbeddingCache(), attempt_id=1, session_id=1, user_id="u",
        search_space_id=1, concept_id=2, asked_turn=2,
    )
    assert hints == []
```

- [ ] **Step 2: Run — expect fail**

Run: `pytest apollo/clarification/tests/test_turn.py -v`
Expected: FAIL (module missing).

- [ ] **Step 3: Implement the orchestrator**

Create `apollo/clarification/turn.py`:

```python
"""Per-turn clarification orchestration, factored out of handle_chat so it is
unit-testable without the DB/HTTP stack. Fail-safe throughout: any failure
returns no hints and persists nothing — teaching never blocks (spec §12)."""

from __future__ import annotations

import logging

from apollo.clarification.detector import detect_ambiguous_nodes
from apollo.clarification.embedding import CandidateEmbeddingCache, Embedder
from apollo.clarification.pacing import select_probes
from apollo.clarification.probe import build_probe_hint
from apollo.clarification.store import write_asked_waiting
from apollo.resolution import find_residual_nodes
from apollo.resolution.candidates import Candidate
from apollo.resolution.tiers import student_surface_text

_LOG = logging.getLogger(__name__)


async def run_clarification_detection(
    *,
    db,
    parsed_nodes: list,
    candidates: tuple[Candidate, ...],
    symbolic_mappings: dict[str, str],
    embedder: Embedder,
    cache: CandidateEmbeddingCache,
    attempt_id: int,
    session_id: int,
    user_id: str,
    search_space_id: int,
    concept_id: int | None,
    asked_turn: int,
) -> list[str]:
    """Detect ambiguous residual nodes, persist asked_waiting rows, and return
    the answer-blind probe hints for draft_reply. Returns [] on any failure or
    when no candidates exist."""
    if not parsed_nodes or not candidates:
        return []
    try:
        residual = find_residual_nodes(parsed_nodes, candidates, symbolic_mappings=symbolic_mappings)
        flagged = detect_ambiguous_nodes(residual, candidates, embedder=embedder, cache=cache)
        chosen = select_probes(flagged)
        hints: list[str] = []
        for f in chosen:
            await write_asked_waiting(
                db, attempt_id=attempt_id, session_id=session_id, user_id=user_id,
                search_space_id=search_space_id, concept_id=concept_id, node_id=f.node.node_id,
                candidate_key=f.candidate.canonical_key, probe_question="",
                original_statement=student_surface_text(f.node), asked_turn=asked_turn,
            )
            hints.append(build_probe_hint(f.node, f.candidate))
        return hints
    except Exception as exc:  # noqa: BLE001 - never block teaching
        _LOG.warning("clarification_detection_failed attempt_id=%s error=%s", attempt_id, exc)
        return []
```

(Note: `probe_question` is stored empty at ask time because Apollo phrases the actual question inside `draft_reply`; the hint is the durable record of what was asked. If a later task wants the verbatim question, capture `validated` post-draft and update the row — out of scope here.)

- [ ] **Step 4: Wire into `handle_chat`**

In `apollo/handlers/chat.py`, between line 287 (`history_for_llm = ...`) and the `draft_reply` call (289), add (using a module-level `_CLARIFICATION_CACHE = CandidateEmbeddingCache()` and `default_embedder`):

```python
    # ---- Clarification loop: detect ambiguous residual ideas, weave answer-blind
    # probes into Apollo's reply (spec §6). Fail-safe — never blocks teaching.
    clarification_hints: list[str] = []
    try:
        problem_payload = problem.model_dump(mode="json")  # confirm shape vs build_problem_candidates
        inputs = await load_problem_candidates(
            db, search_space_id=int(sess.search_space_id),
            concept_id=sess.concept_id, problem_payload=problem_payload,
        )
        clarification_hints = await run_clarification_detection(
            db=db, parsed_nodes=nodes, candidates=inputs.candidates,
            symbolic_mappings=inputs.symbolic_mappings, embedder=default_embedder,
            cache=_CLARIFICATION_CACHE, attempt_id=current_attempt.id, session_id=session_id,
            user_id=str(sess.user_id), search_space_id=int(sess.search_space_id),
            concept_id=sess.concept_id, asked_turn=next_idx + 1,
        )
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("clarification_setup_failed session_id=%s error=%s", session_id, exc)

    validated = draft_reply(
        history=history_for_llm,
        kg_summary=kg_summary,
        problem_text=problem.problem_text,
        history_summary=history_summary,
        clarification_hints=clarification_hints or None,
    )
```

`next_idx` is computed at line 298 today; move the `next_idx = await _next_turn_index(db, session_id)` line up to before this block so `asked_turn=next_idx + 1` (the apollo-reply turn index) is available. The asked_waiting rows and the two `Message` rows commit together at line 307 (`await db.commit()`), so detection writes are atomic with the turn.

Add imports at the top of `chat.py`:

```python
from apollo.clarification import CandidateEmbeddingCache, default_embedder
from apollo.clarification.candidate_assembly import load_problem_candidates
from apollo.clarification.turn import run_clarification_detection

_CLARIFICATION_CACHE = CandidateEmbeddingCache()
```

- [ ] **Step 5: Write the handler integration test**

Create `apollo/handlers/tests/test_chat_clarification.py` mirroring the existing chat handler tests (they already build a `db`/`neo`/session fixture). Patch `apollo.handlers.chat.default_embedder` with a deterministic stub and assert an `asked_waiting` row is written and the reply path still returns. Keep it minimal — the deep logic is covered by `test_turn.py`.

- [ ] **Step 6: Run**

Run: `pytest apollo/clarification/tests/test_turn.py apollo/handlers/tests/test_chat_clarification.py -v`
Expected: pass. Then `pytest apollo/handlers/tests/test_chat_no_signals.py -v` to confirm the v1 anti-leak guard still passes (we did not add `validate_or_raise`).

- [ ] **Step 7: Commit**

```bash
git add apollo/clarification/turn.py apollo/handlers/chat.py apollo/clarification/tests/test_turn.py apollo/handlers/tests/test_chat_clarification.py
git commit -m "feat(apollo): wire live clarification detection + probing into handle_chat"
```

---

### Task 14: Leakage-judge backstop on clarification replies

**Files:**
- Create: `apollo/clarification/leak_guard.py`
- Modify: `apollo/handlers/chat.py` (apply the guard after `draft_reply` when probes were used)
- Modify: `apollo/handlers/tests/test_chat_no_signals.py` (narrow the v1 guard)
- Test: `apollo/clarification/tests/test_leak_guard.py`

**Interfaces:**
- Consumes: `LeakageJudge`, `JudgeVerdict`, `llm_leakage_judge`, `CONFIDENCE_THRESHOLD` (`apollo.agent.leakage_judge`); `ConceptDefinition`; `draft_reply` (9).
- Produces: `def guard_clarification_reply(*, draft: str, concept, history, kg_summary, regenerate_without_probes: Callable[[], str], judge: LeakageJudge | None = None) -> str`. Runs the judge over the drafted reply; if `verdict.leaks and verdict.confidence >= CONFIDENCE_THRESHOLD`, returns `regenerate_without_probes()` (re-draft with `clarification_hints=None`); on a judge exception, returns `draft` unchanged (soft-fail-open, spec §12). This is the user-requested second line of defense — it does NOT use the raising `validate_or_raise`, so it never blocks teaching.

- [ ] **Step 1: Write the failing tests**

Create `apollo/clarification/tests/test_leak_guard.py`:

```python
from apollo.agent.leakage_judge import JudgeVerdict
from apollo.clarification.leak_guard import guard_clarification_reply


def _concept():
    # Reuse the ConceptDefinition fixture builder the leakage_judge tests use
    # (apollo/agent/tests/test_leakage_judge.py). Import it rather than re-deriving.
    from apollo.agent.tests.test_leakage_judge import _concept_fixture
    return _concept_fixture()


def test_confident_leak_redrafts_without_probes():
    def judge(*, draft, concept, history, kg_summary):
        return JudgeVerdict(leaks=True, offending_phrase="lower", reason="x", confidence=0.9)
    out = guard_clarification_reply(
        draft="...the pressure is lower...", concept=_concept(), history=[], kg_summary="k",
        regenerate_without_probes=lambda: "SAFE REPLY", judge=judge,
    )
    assert out == "SAFE REPLY"


def test_low_confidence_leak_is_kept():
    def judge(**kw):
        return JudgeVerdict(leaks=True, offending_phrase="maybe", reason="x", confidence=0.3)
    out = guard_clarification_reply(
        draft="kept probe", concept=_concept(), history=[], kg_summary="k",
        regenerate_without_probes=lambda: "UNUSED", judge=judge,
    )
    assert out == "kept probe"  # below CONFIDENCE_THRESHOLD (0.6)


def test_clean_reply_is_kept():
    def judge(**kw):
        return JudgeVerdict(leaks=False, offending_phrase=None, reason=None, confidence=1.0)
    out = guard_clarification_reply(
        draft="clean probe", concept=_concept(), history=[], kg_summary="k",
        regenerate_without_probes=lambda: "UNUSED", judge=judge,
    )
    assert out == "clean probe"


def test_judge_error_soft_fail_open():
    def judge(**kw):
        raise RuntimeError("503")
    out = guard_clarification_reply(
        draft="original", concept=_concept(), history=[], kg_summary="k",
        regenerate_without_probes=lambda: "REGEN", judge=judge,
    )
    assert out == "original"  # spec §12: soft fail open, never block teaching
```

If `apollo/agent/tests/test_leakage_judge.py` has no reusable `_concept_fixture`, create a tiny local builder in this test that constructs a minimal `ConceptDefinition` (match its real constructor — read `apollo/agent/leakage_judge.py`'s `ConceptDefinition` import).

- [ ] **Step 2: Run — expect fail**

Run: `pytest apollo/clarification/tests/test_leak_guard.py -v`
Expected: FAIL (module missing).

- [ ] **Step 3: Implement**

Create `apollo/clarification/leak_guard.py`:

```python
"""Leakage-judge backstop for clarification replies (spec §6.4). The answer-blind
generator + the probe-hint no-leak test (Task 7) are the PRIMARY guarantee; this
is the second line: if the judge confidently flags a leak in a reply that carried
a probe, re-draft WITHOUT the probes rather than risk revealing the answer.
Soft-fail-open (spec §12): a judge error leaves the reply unchanged — teaching is
never blocked. Uses the judge callable directly, NOT the raising validate_or_raise."""

from __future__ import annotations

import logging
from typing import Callable

from apollo.agent.leakage_judge import (
    CONFIDENCE_THRESHOLD,
    JudgeVerdict,
    LeakageJudge,
    llm_leakage_judge,
)
from apollo.knowledge_graph.concept import ConceptDefinition  # match leakage_judge's import

_LOG = logging.getLogger(__name__)


def guard_clarification_reply(
    *,
    draft: str,
    concept: ConceptDefinition,
    history: list[dict[str, str]],
    kg_summary: str,
    regenerate_without_probes: Callable[[], str],
    judge: LeakageJudge | None = None,
) -> str:
    judge_fn: LeakageJudge = judge or llm_leakage_judge
    try:
        verdict: JudgeVerdict = judge_fn(
            draft=draft, concept=concept, history=history, kg_summary=kg_summary,
        )
    except Exception as exc:  # noqa: BLE001 - soft fail open (spec §12)
        _LOG.warning("clarification_leak_judge_failed error=%s", exc)
        return draft
    if verdict.leaks and verdict.confidence >= CONFIDENCE_THRESHOLD:
        _LOG.warning(
            "clarification_probe_leak_detected phrase=%s confidence=%.2f",
            verdict.offending_phrase, verdict.confidence,
        )
        return regenerate_without_probes()
    return draft
```

Confirm the real import path of `ConceptDefinition` by matching the import line at the top of `apollo/agent/leakage_judge.py` (use that exact module).

- [ ] **Step 4: Run — expect pass**

Run: `pytest apollo/clarification/tests/test_leak_guard.py -v`
Expected: 4 passed.

- [ ] **Step 5: Wire into `handle_chat`**

In `apollo/handlers/chat.py`, immediately after the `validated = draft_reply(...)` call from Task 13 (which now passes `clarification_hints=clarification_hints or None`), add the backstop — only when probes were actually used:

```python
    if clarification_hints:
        validated = guard_clarification_reply(
            draft=validated, concept=concept, history=history_for_llm, kg_summary=kg_summary,
            regenerate_without_probes=lambda: draft_reply(
                history=history_for_llm, kg_summary=kg_summary,
                problem_text=problem.problem_text, history_summary=history_summary,
            ),
        )
```

`concept` is already in scope (loaded at chat.py:221). Add the import:

```python
from apollo.clarification.leak_guard import guard_clarification_reply
```

- [ ] **Step 6: Narrow the v1 anti-signals guard test**

Read `apollo/handlers/tests/test_chat_no_signals.py`. Its `validate_or_raise(` assertion still holds (we did not add it). If it also asserts the leakage judge / `leakage_judge` import is absent from `chat.py`, update that assertion to permit the single clarification-scoped `guard_clarification_reply` call, adding a comment: the v1 "no output filter" stance is preserved for the general path; the clarification leak backstop is an intentional, narrowly-scoped exception (user decision, 2026-06-29). Keep the assertion that the raising `validate_or_raise` is still absent.

- [ ] **Step 7: Run the guard + handler tests**

Run: `pytest apollo/handlers/tests/test_chat_no_signals.py apollo/clarification/tests/test_leak_guard.py apollo/handlers/tests/test_chat_clarification.py -v`
Expected: pass.

- [ ] **Step 8: Commit**

```bash
git add apollo/clarification/leak_guard.py apollo/handlers/chat.py \
        apollo/handlers/tests/test_chat_no_signals.py apollo/clarification/tests/test_leak_guard.py
git commit -m "feat(apollo): leakage-judge backstop on clarification replies (soft-fail-open)"
```

---

### Task 15: Wire next-turn re-scoring into `handle_chat`

**Files:**
- Modify: `apollo/handlers/chat.py` (early in the teaching path, after `parse_utterance`/before detection)
- Create: `apollo/clarification/resolve_turn.py` (re-score orchestration, unit-testable)
- Test: `apollo/clarification/tests/test_resolve_turn.py`

**Interfaces:**
- Consumes: `load_asked_waiting` (12), `record_outcome` (12), `rescore_clarification` (11), `candidate display lookup` from the candidate set (10/their `display_name`).
- Produces: `async def resolve_pending_clarifications(db, *, attempt_id, student_message, candidates, judge, answered_turn) -> None` — for each `asked_waiting` row, re-score the student's new message against the candidate; record `confirmed`/`refuted`/`vague` (+ `clarification_text=student_message`). Fail-safe: on judge failure leave the row `asked_waiting`.

- [ ] **Step 1: Write the failing test**

Create `apollo/clarification/tests/test_resolve_turn.py`:

```python
from apollo.clarification import resolve_turn
from apollo.resolution.candidates import Candidate


class _Row:
    def __init__(self, node_id, candidate_key, original):
        self.id = 1
        self.node_id = node_id
        self.candidate_key = candidate_key
        self.original_statement = original


def _cand(key, display):
    return Candidate(canonical_key=key, canon_key=1, node_type="condition", is_misconception=False,
                     symbolic=None, aliases=(), display_name=display, opposes_key=None, exact_aliases=())


async def test_records_confirmed_outcome(monkeypatch):
    recorded = {}

    async def fake_load(db, *, attempt_id):
        return [_Row("s1", "cond.bernoulli", "p~v")]

    async def fake_record(db, *, clarification_id, state, clarification_text, answered_turn):
        recorded.update(state=state, text=clarification_text, turn=answered_turn)

    monkeypatch.setattr(resolve_turn, "load_asked_waiting", fake_load)
    monkeypatch.setattr(resolve_turn, "record_outcome", fake_record)

    await resolve_turn.resolve_pending_clarifications(
        db=object(), attempt_id=1, student_message="lower where faster",
        candidates=(_cand("cond.bernoulli", "inverse p-v"),),
        judge=lambda req: "confirmed", answered_turn=4,
    )
    assert recorded["state"] == "confirmed"
    assert recorded["text"] == "lower where faster"


async def test_judge_failure_leaves_waiting(monkeypatch):
    from apollo.errors import ResolutionUnavailableError

    async def fake_load(db, *, attempt_id):
        return [_Row("s1", "k", "o")]

    calls = {"record": 0}

    async def fake_record(db, **kw):
        calls["record"] += 1

    def boom(req):
        raise ResolutionUnavailableError(stage="clarification_rescore", last_error="x")

    monkeypatch.setattr(resolve_turn, "load_asked_waiting", fake_load)
    monkeypatch.setattr(resolve_turn, "record_outcome", fake_record)
    await resolve_turn.resolve_pending_clarifications(
        db=object(), attempt_id=1, student_message="m", candidates=(_cand("k", "d"),),
        judge=boom, answered_turn=4,
    )
    assert calls["record"] == 0  # left asked_waiting; no terminal write
```

- [ ] **Step 2: Run — expect fail**

Run: `pytest apollo/clarification/tests/test_resolve_turn.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement**

Create `apollo/clarification/resolve_turn.py`:

```python
"""Next-turn re-scoring: the student's reply is judged against each pending
clarification's target idea and the outcome recorded (spec §7). A refuted row is
the misconception evidence record (spec §8). Fail-safe: a judge failure leaves
the row asked_waiting (never credit on failure)."""

from __future__ import annotations

import logging

from apollo.clarification.rescorer import ClarificationJudge, rescore_clarification
from apollo.clarification.store import load_asked_waiting, record_outcome
from apollo.resolution.candidates import Candidate

_LOG = logging.getLogger(__name__)


async def resolve_pending_clarifications(
    *,
    db,
    attempt_id: int,
    student_message: str,
    candidates: tuple[Candidate, ...],
    judge: ClarificationJudge,
    answered_turn: int,
) -> None:
    display_by_key = {c.canonical_key: c.display_name for c in candidates}
    for row in await load_asked_waiting(db, attempt_id=attempt_id):
        try:
            outcome = rescore_clarification(
                original_statement=row.original_statement,
                clarification_text=student_message,
                candidate_display=display_by_key.get(row.candidate_key, row.candidate_key),
                judge=judge,
            )
        except Exception as exc:  # noqa: BLE001 - leave asked_waiting, never credit on failure
            _LOG.warning("clarification_rescore_failed id=%s error=%s", row.id, exc)
            continue
        await record_outcome(
            db, clarification_id=row.id, state=outcome,
            clarification_text=student_message, answered_turn=answered_turn,
        )
```

- [ ] **Step 4: Wire into `handle_chat`**

In `apollo/handlers/chat.py`, after `parse_utterance` and the candidate set is available (reuse the `inputs.candidates` from Task 13 — compute the candidate set ONCE per turn and use it for both re-scoring and detection). Re-scoring must run against the **current** student `message`. Place it before detection so a just-answered idea isn't re-detected. Use `default_clarification_judge`:

```python
    await resolve_pending_clarifications(
        db=db, attempt_id=current_attempt.id, student_message=message,
        candidates=inputs.candidates, judge=default_clarification_judge,
        answered_turn=next_idx,  # the student-message turn index
    )
```

Add imports:

```python
from apollo.clarification.resolve_turn import resolve_pending_clarifications
from apollo.clarification.rescorer import default_clarification_judge
```

Wrap in the same fail-safe try/except as detection. The `record_outcome` mutations commit with the turn at line 307.

- [ ] **Step 5: Run**

Run: `pytest apollo/clarification/tests/test_resolve_turn.py apollo/handlers/tests -k clarification -v`
Expected: pass.

- [ ] **Step 6: Commit**

```bash
git add apollo/clarification/resolve_turn.py apollo/handlers/chat.py apollo/clarification/tests/test_resolve_turn.py
git commit -m "feat(apollo): wire next-turn clarification re-scoring into handle_chat"
```

---

### Task 16: Feed confirmed clarifications into grading (Done)

**Files:**
- Modify: `apollo/handlers/done_grading.py` (`run_graph_simulation` sig 167–176; `resolve_attempt` call 228)
- Modify: `apollo/handlers/done.py` (the `run_graph_simulation(...)` call ~388–395)
- Test: `apollo/handlers/tests/test_done_grading_clarification.py`

**Interfaces:**
- Consumes: `load_confirmed_resolutions` (12); `resolve_attempt(confirmed_resolutions=...)` (3).
- Produces: `run_graph_simulation` loads `confirmed_resolutions = await load_confirmed_resolutions(db, attempt_id=int(attempt.id))` and passes it into `resolve_attempt`. No new public param needed (it loads from the DB it already holds).

- [ ] **Step 1: Write the failing test**

Create `apollo/handlers/tests/test_done_grading_clarification.py`. The cleanest seam: assert `run_graph_simulation` calls `resolve_attempt` with `confirmed_resolutions` equal to what `load_confirmed_resolutions` returns. Use `monkeypatch` to stub both `load_confirmed_resolutions` and `resolve_attempt`, capturing the kwarg:

```python
import apollo.handlers.done_grading as dg


async def test_confirmed_resolutions_threaded_into_resolve(monkeypatch):
    captured = {}

    async def fake_load(db, *, attempt_id):
        return {"s1": "cond.bernoulli"}

    def fake_resolve(student_graph, candidates, **kw):
        captured.update(kw)
        raise _StopHere()  # short-circuit after the call we care about

    class _StopHere(Exception):
        pass

    monkeypatch.setattr(dg, "load_confirmed_resolutions", fake_load)
    monkeypatch.setattr(dg, "resolve_attempt", fake_resolve)
    # ... build the minimal attempt/sess/graph fixtures the other done_grading
    #     tests use, call run_graph_simulation, expect _StopHere ...
    # assert captured["confirmed_resolutions"] == {"s1": "cond.bernoulli"}
```

(Model this on the existing `done_grading` tests' fixture setup; the assertion is `captured["confirmed_resolutions"] == {"s1": "cond.bernoulli"}`.)

- [ ] **Step 2: Run — expect fail**

Run: `pytest apollo/handlers/tests/test_done_grading_clarification.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement**

In `apollo/handlers/done_grading.py`, add the import:

```python
from apollo.clarification.store import load_confirmed_resolutions
```

Inside `run_graph_simulation`, just before the `resolve_attempt(...)` call (line 228), load the confirmed map, and pass it (the `llm_adjudicator=main_chat_adjudicator` kwarg was already removed in Task 4):

```python
        confirmed_resolutions = await load_confirmed_resolutions(db, attempt_id=int(attempt.id))
        # Step 5 — resolve (clarification-confirmed nodes are authoritative; no LLM guess).
        resolution = resolve_attempt(
            student_graph,
            inputs.candidates,
            confirmed_resolutions=confirmed_resolutions,
            fuzzy_threshold=0.9,
            symbolic_mappings=inputs.symbolic_mappings,
        )
```

No change to `done.py`'s call is required (the load happens inside `run_graph_simulation`). If a future reviewer prefers the load in `handle_done`, thread a `confirmed_resolutions=` param instead — but the in-handler load keeps the signature minimal.

- [ ] **Step 4: Run**

Run: `pytest apollo/handlers/tests/test_done_grading_clarification.py apollo/handlers/tests -k done -v`
Expected: pass.

- [ ] **Step 5: Commit**

```bash
git add apollo/handlers/done_grading.py apollo/handlers/tests/test_done_grading_clarification.py
git commit -m "feat(apollo): grading consumes confirmed clarification resolutions"
```

---

### Task 17: Docs drift reconciliation + deterministic E2E validation

**Files:**
- Modify: `docs/architecture/apollo.md` (owner of `apollo/`)
- Modify: `docs/architecture/domain-data.md` (owner of the DB schema)
- Modify: `apollo/grading/tests/_builders.py` (add clarification stub factories)
- Modify: `apollo/grading/tests/test_corpus_e2e.py` (add a clarification-confirmed scenario)
- Test: the corpus E2E run

**Interfaces:**
- Produces: updated owner docs (drift contract satisfied); a deterministic corpus E2E proving a clarification-confirmed node lowers `unresolved_rate` and avoids abstention.

- [ ] **Step 1: Add stub factories to `_builders.py`**

Mirror the `found_/notfound_/raising_audit_fn` pattern:

```python
def confirming_clarification_judge(outcome: str = "confirmed"):
    """A deterministic ClarificationJudge returning a fixed verdict."""
    def _fn(request):
        return outcome
    return _fn
```

- [ ] **Step 2: Add a clarification scenario to the corpus E2E**

In `apollo/grading/tests/test_corpus_e2e.py`, add a case that resolves a node via `confirmed_resolutions={node_id: candidate_key}` through `resolve_attempt` and asserts (a) `unresolved_rate_of(result)` drops below the `0.35` gate, and (b) `apply_abstention(...)` with the resulting `normalization_confidence` does NOT abstain. This is the CI-gating, deterministic proof the spec §13 asks for — no live LLM/Neo4j/PG.

```python
def test_clarification_confirmed_node_avoids_abstention():
    from apollo.grading.abstention import apply_abstention, unresolved_rate_of
    # Build a small attempt with one otherwise-unresolvable conceptual node,
    # confirm it via clarification, and show the gate stays open.
    graph = _corpus_graph_with_one_ambiguous_condition()
    cands = _corpus_candidates()
    result = resolve_attempt(graph, cands, confirmed_resolutions={"s_ambig": "cond.target"})
    assert unresolved_rate_of(result) <= 0.35
    ab = apply_abstention(
        unresolved_rate=unresolved_rate_of(result), min_parser_confidence=1.0,
        normalization_confidence=1.0,
    )
    assert ab.abstained is False
```

(Reuse existing corpus fixtures/builders in that test module; `_corpus_graph_with_one_ambiguous_condition` / `_corpus_candidates` are thin wrappers over them.)

- [ ] **Step 3: Reconcile `docs/architecture/apollo.md`**

Document, in the grader/resolution section: the live clarification loop (detect → answer-blind probe → next-turn re-score), the `clarification` resolution method and its 0.90 cap, the removal of silent LLM adjudication, and the new `apollo_clarifications` table. Update any `owns:` globs to include `apollo/clarification/**`. Bump `last_verified` to today.

- [ ] **Step 4: Register the table in `docs/architecture/domain-data.md`**

Add `apollo_clarifications` (migration 032) to the schema inventory with its columns, the `(attempt_id, node_id)` uniqueness, the RLS-stopgap note, and the state machine. Bump `last_verified`.

- [ ] **Step 5: Run the full apollo suite + the patch-coverage gate**

```bash
pytest tests apollo -q --cov --cov-report=xml
git fetch --no-tags origin staging
diff-cover coverage.xml --compare-branch=origin/staging --fail-under=95
```
Expected: apollo suite green; diff-cover ≥ 95% on the changed lines. Address any sub-95% files by adding the missing-line tests before proceeding.

- [ ] **Step 6: Commit**

```bash
git add docs/architecture/apollo.md docs/architecture/domain-data.md apollo/grading/tests/_builders.py apollo/grading/tests/test_corpus_e2e.py
git commit -m "docs(apollo): reconcile clarification loop in architecture; add E2E gate"
```

- [ ] **Step 7 (optional, manual — not CI-gated): live macro probe**

To exercise the full live path end-to-end, restore the deleted harness from the preserved branch into an untracked location and run it against a local stack (server :8000 + Neo4j + Postgres):

```bash
git show experiment/macro-graph-grading-probe:scripts/apollo_grade_probe.py > /tmp/apollo_grade_probe.py
git show experiment/macro-graph-grading-probe:scripts/_macro_scenarios.py > /tmp/_macro_scenarios.py
# seed concept registry + learner model + canon projection first, then run.
```
Do NOT commit the harness to a gated branch (it lands at 0% coverage and fails the patch gate — the reason it was removed). Record `unresolved_rate` / abstention before vs after in the PR description.

---

## Self-Review

**1. Spec coverage** (spec section → task):
- §1–§4 problem/goal/principle → motivation, encoded across Tasks 3/4/6/7/11.
- §5 architecture overview → Tasks 13/14/15/16 (live + done seams).
- §6.1 candidate set → Task 10. §6.2 confident credit unchanged → Task 6 (`find_residual_nodes` only feeds residuals). §6.3 detector + cache → Tasks 5/6. §6.4 answer-blind probe → Tasks 7/9 + leakage-judge backstop 14. §6.5 pacing → Task 8. §6.6 persist → Tasks 1/12/13.
- §7 re-scoring → Tasks 11/15. §8 misconception evidence → Task 1 (the `refuted` row IS the record) + Task 15 (records it).
- §9 grading changes (clarification method authoritative pre-tier; silent adjudication removed) → Tasks 2/3/4/16.
- §10 data model → Task 1 (with the documented column-name + RLS corrections).
- §11 cancels/replaces → Task 4 (adjudication removed); Phase 1b stays cancelled (no work).
- §12 failure handling → fail-safe blocks in Tasks 6/11/13/14/15.
- §13 testing → every task is TDD; DB on local Postgres; determinism via DI stubs; corpus E2E in Task 17.
- §14 drift contract → Task 17. §15 calibration params → encoded as named constants (Tasks 2/6/8) with the params table above.

**2. Placeholder scan:** No "TBD/handle errors/similar to Task N". Two spots intentionally say "confirm the exact `Node` import path" (Task 6) and "confirm `problem.model_dump` shape vs `build_problem_candidates`" (Task 13) — these are **verification steps with a concrete command**, not deferred work, because the precise import/shape can only be read at the file. The executor resolves them in-step.

**3. Type consistency:** `confirmed_resolutions: dict[str, str]` (node_id→candidate_key) is consistent across Tasks 3, 12 (`load_confirmed_resolutions` return), and 16 (call). `FlaggedNode(node, candidate, cosine)` consistent across Tasks 6/8/13. `RescoreOutcome`/state strings `"confirmed"|"refuted"|"vague"` consistent across Tasks 1 (`CLARIFICATION_STATES`), 11, 15. `Embedder = Callable[[list[str]], list[list[float]]]` (batched) consistent across Tasks 5/6/13. `clarification_hints: list[str] | None` consistent across Tasks 9/13.

## Open decisions surfaced for the human (do not block; defaults chosen)

1. **Leakage judge — RESOLVED (user decision 2026-06-29): wire it.** The spec assumes it backstops chat; it didn't, and a guard test forbade it. We now wire it narrowly via `guard_clarification_reply` (Task 14) — soft-fail-open, clarification-replies-only, non-raising — keeping the structural answer-blind guarantee (Task 7) as the primary line and narrowing `test_chat_no_signals.py`.
2. **Column naming:** `user_id`/`search_space_id` (repo convention) instead of the spec's bare `student_id`. Profile-rollup queries key on `(user_id, concept_id)`.
3. **Cache key:** `candidate_set_hash` (self-contained) instead of `reference_graph_hash` (would require building the reference canonical per chat turn). Equivalent invalidation.
