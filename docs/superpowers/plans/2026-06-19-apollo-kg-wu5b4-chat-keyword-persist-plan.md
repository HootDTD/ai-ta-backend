# Plan: WU-5B4 — §10 chat-keyword JSONB persist (migration 029 + persist the already-computed `extract_and_filter_keywords` on `chat_turns`)

**Goal:** Persist the already-computed `extract_and_filter_keywords` output (≤8 concept terms) as one write-only JSONB column on `chat_turns`, so months of chat history can be backfilled offline as a class-level RQ5 signal — changing no answer, retrieval result, or score.
**Architecture:** ADD COLUMN (`chat_turns.keywords JSONB NOT NULL DEFAULT '[]'::jsonb`) + matching ORM column on `ChatTurn` + a backward-compatible `keywords=` parameter threaded into `chats.service.append_turn` + the orchestrator surfacing its already-computed term list to the persist site. Write-only; NO read/consumer path in v1.
**Tech stack:** Supabase Postgres 16 + pgvector (asyncpg + SQLAlchemy async), numbered raw-SQL migrations (`database/migrations/`, next-free = 029), Testcontainers `pgvector/pgvector:pg16` local harness, pytest + pytest-asyncio.

---
provides:
  - chat_turns.keywords (JSONB, NOT NULL DEFAULT '[]'::jsonb, write-only)
  - ChatTurn.keywords ORM column
  - chats.service.append_turn(keywords=...) backward-compatible parameter
  - database/migrations/029_chat_turn_keywords.sql
consumes:
  - chat_turns / ChatTurn (database/models.py:429, migration 005)
  - extract_and_filter_keywords output already on the orchestrator's ResearchBundle.found_terms (ai/orchestrator.py:591-604,703)
depends_on:
  - WU-5B3b janitor branch tip (compare branch feat/apollo-kg-wu5b3b-janitor-worker); migration 028 on disk so 029 is next-free
---

## Overview

§10 of the spec (`docs/superpowers/specs/2026-06-10-apollo-kg-learner-model-architecture-decision.md:1453-1466`) keeps Hoot-chat-as-evidence OUT of v1 because question-asking is sign-ambiguous, but ships ONE hedge so the data is not lost: the per-`/ask` `extract_and_filter_keywords` output (≤8 concept terms, "currently computed then discarded", spec L1463-1464) is persisted as **one JSONB column on chat turns** so "months of history can be backfilled offline" and used as a **class-level signal first … never a hard per-student negative**. §12 phase-5 (L1534) names this exact deliverable: "RQ5 hedge (persist chat keywords)". The WU-5B split-proposal (`docs/superpowers/plans/2026-06-18-apollo-kg-wu5b-split-proposal.md:84,163`) explicitly carved this §10 chat-keyword persist OUT of WU-5B2 (which shipped the §3 *negotiation* multiplier — a different RQ5 hedge) and deferred it to "a Hoot-chat unit". **WU-5B4 IS that unit.**

The keywords are ALREADY COMPUTED — `ai/orchestrator.py:591` runs `_ctx_summary, filtered_terms = extract_and_filter_keywords(keyword_query, subject=subject_name)`, normalizes them into `keywords: List[str]` (≤8 concept terms) at `:597-604`, uses them ONLY as retrieval hints (`:636/646/670/703`), sets them onto the returned `ResearchBundle.found_terms` (`:670,703`), and then discards them. This unit **persists that exact list** onto the `ChatTurn` row. It does **NOT** recompute keywords, change retrieval, change the LLM call, or add any read/consumer path. The column is **write-only in v1**.

**Scope shape (3 concerns, all small):**
1. **Migration 029 + ORM** — `chat_turns.keywords JSONB NOT NULL DEFAULT '[]'::jsonb` + the matching mapped column on `ChatTurn`. This is the load-bearing, fully real-PG-testable deliverable.
2. **`chats/service.py:append_turn`** — add a backward-compatible keyword-only param `keywords: List[str] | None = None` that writes `ChatTurn(keywords=...)` (defaulting to `[]`, mirroring the existing `attachments`/`citations` style).
3. **`ai/orchestrator.py`** — make the already-computed term list explicitly reachable by a persist site without re-deriving it. The orchestrator already exposes the list on `ResearchBundle.found_terms`; this unit pins that contract with a focused unit test (and, if the existing surface is judged insufficiently explicit, threads the list through unchanged — see "The threading map"). No behavior change.

**A load-bearing reality the plan is built around (verified, not assumed):** `docs/architecture/rag-pipeline.md:124` records that "`Orchestrator` is imported at `server.py:25` but never constructed" — the PRODUCTION `/ask` path is `server.py:_ask_pgvector` (line ~1433), which computes the same keywords independently (`server.py:1473-1482`) and carries them on its own `ResearchBundle.found_terms`. The persist edge that actually reaches `chat_turns` in production is `server.py:_append_assistant_turn_and_refresh` (`server.py:530,1852,2135`) → `append_turn(...)`. **`server.py` is NOT in this unit's scope file list.** WU-5B4 therefore ships the durable column + the `append_turn(keywords=...)` write-API + the orchestrator-side contract, and explicitly documents the `server.py` bundle→append_turn wiring as a one-line out-of-scope follow-up (see "Out-of-scope boundaries"). This keeps the unit inside its declared scope while making the column real, ORM-mapped, and ready for the production caller to fill.

## Structural prep (from neighborhood scan)

Neighborhood scanned (one ring out from the change path): `chat_turns` table, `ChatTurn` ORM, `chats/service.py:append_turn`, `ai/orchestrator.py:_iterative_research_pgvector`.

- [ ] **`chat_turns` fan-in** — consumers grepped: `chats/service.py` (CRUD primitives), `chats/routes.py` (import/upsert), `server.py` (the two `/ask` persist sites), `chats/bundle_cache.py`, plus read paths in `serialize_chat_session`. This is < 10 distinct write/consumer modules and the new column is **write-only** (no consumer reads it in v1), so no coupling-hub debt is introduced. **No prep needed.**
- [ ] **`append_turn` size** — `chats/service.py:98-147` is ~50 lines, one responsibility (lock session row, compute `turn_index = max+1`, build the `ChatTurn`). Adding one optional keyword-only param keeps it < 60 lines and one responsibility. Below the WMC threshold. **No prep needed.**
- [ ] **`ai/orchestrator.py:_iterative_research_pgvector`** (`:557-707`, ~150 lines) — exceeds the 50-line function threshold, BUT WU-5B4 must NOT refactor it (it is in the RAG retrieval hot path; any structural change risks the no-behavior-change invariant that is this unit's whole point). The keyword block (`:589-604`) is already isolated and the term list already reaches `bundle.found_terms`. Touching only the return surface (additive, identity-preserving) is the minimal change. **Debt noted but explicitly OUT of scope** — splitting that function is a separate RAG-pipeline task; flagging it here per the scan, not actioning it.
- [ ] **Access-policy sprawl** — `chat_turns` has exactly ONE RLS policy (`chat_turns_owner_rw`, migration 006), well under the 5-policy threshold. The new column inherits it (RLS is table-level; no per-column policy needed). **No prep needed.**

**Conclusion: neighborhood is clean for this unit.** Structural prep is 0 steps (0% of the plan), well under the 30% budget. The one flagged item (orchestrator function size) is deliberately deferred, not folded in.
- Verify: `.venv/Scripts/python.exe -c "import ast,sys; src=open('chats/service.py').read(); print('append_turn present:', 'def append_turn' in src)"`

## Prior art (existing migrations)

The change is a near-exact clone of TWO prior JSONB-column-on-`chat_turns` additions. Mirror them byte-for-byte in style.

- **`database/migrations/011_chat_turn_citations.sql:1-12`** — THE template. Adds `citations JSONB NOT NULL DEFAULT '[]'::jsonb` to `chat_turns` with a header comment explaining "computed at request time, returned to the client, then discarded — reloading lost them". WU-5B4's `keywords` column is the SAME shape and the SAME "computed then discarded, now persisted for offline use" rationale. The 029 migration body is this file with `citations`→`keywords` and a §10 rationale comment.
- **`database/models.py:454-455`** — the ORM style to mirror: `attachments = Column(JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb"))` and `citations = Column(JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb"))`. The new `keywords` column copies this exactly (`default=list` so ORM-side inserts default to `[]`; `server_default=text("'[]'::jsonb")` so raw/legacy SQL inserts are safe — RECON FACT 2's "message_metadata pattern that uses server_default so raw/legacy inserts are safe").
- **`database/migrations/005_chat_memory.sql:19-31`** — original `chat_turns` definition (`attachments JSONB NOT NULL DEFAULT '[]'::jsonb`); confirms the JSONB-array-default convention predates this unit.
- **Test harness prior art — `tests/database/test_apollo_learner_janitor_migration.py:1-290`** — the canonical forward-chain-on-real-PG migration test (creates a fresh DB on the session pgvector container, applies the in-order content-globbed migration chain via raw asyncpg, asserts `information_schema.columns` shape + idempotency, then ORM-round-trips on the savepoint `db_session`). WU-5B4's `tests/database/test_migration_029.py` mirrors this structure exactly, content-globbing migrations that touch `chat_sessions`/`chat_turns`.
- **`tests/database/test_migration_015.py` + `tests/database/test_models_015.py`** — the chat-table analog: 015 added JSONB/vector columns to `chat_sessions`; its tests assert column presence via `information_schema` (`db_session`) and ORM-class metadata (`ChatTurn.__table__.columns`). Reuse both assertion styles.

**Naming conventions confirmed:** migration files are `NNN_snake_case.sql` (029 → `029_chat_turn_keywords.sql`); ORM columns are snake_case; JSONB array columns use `default=list` + `server_default=text("'[]'::jsonb")`; no `down.sql` files exist in this repo — rollback lives as an inline comment block at the bottom of the migration (consistent with the repo having no down-migrations).

## Schema changes

**NEW file: `database/migrations/029_chat_turn_keywords.sql`** (copy-pasteable, mirrors `011_chat_turn_citations.sql` exactly):

```sql
-- 029_chat_turn_keywords.sql
-- §10 RQ5 hedge (spec docs/superpowers/specs/2026-06-10-apollo-kg-learner-model-
--   architecture-decision.md:1453-1466; §12 phase-5 L1534).
-- Persist the per-/ask extract_and_filter_keywords output (<=8 concept terms)
-- as ONE write-only JSONB column on chat_turns. Until now these keywords were
-- computed at request time as retrieval hints and then DISCARDED. Persisting
-- them lets months of chat history be backfilled offline as a CLASS-LEVEL
-- signal (aggregate concept coverage across a course) — never a hard
-- per-student negative, and with NO read/consumer path in v1 (write-only).

BEGIN;

ALTER TABLE chat_turns
    ADD COLUMN IF NOT EXISTS keywords JSONB NOT NULL DEFAULT '[]'::jsonb;

COMMIT;

-- Rollback:
-- BEGIN;
-- ALTER TABLE chat_turns DROP COLUMN IF EXISTS keywords;
-- COMMIT;
```

**Why this is zero-downtime / migration-safe (no 2-phase needed):**
- It is an ADD COLUMN with a constant server default — on Postgres 11+ a `DEFAULT` on `ADD COLUMN` is a metadata-only catalog change (no full-table rewrite, no long `ACCESS EXCLUSIVE` lock proportional to row count). Lock is a brief catalog `ACCESS EXCLUSIVE`, milliseconds even on a large `chat_turns`.
- `NOT NULL` is safe here precisely because a non-volatile `DEFAULT` is supplied in the same statement (existing rows read back `'[]'::jsonb` from the catalog default; no backfill `UPDATE` required). This is the migration-safety rule "never add a NOT NULL without a backfill-default" — satisfied by the literal default.
- `IF NOT EXISTS` makes it idempotent (re-applying the chain is a no-op), matching every other migration in the repo and required by the idempotency test.
- No data is moved, no column is dropped or retyped — there is nothing destructive, so the 2-phase column-change rule does not apply.

**EDIT: `database/models.py`** — add one mapped column to `ChatTurn` (after `:455`, the `citations` line), mirroring the `attachments`/`citations` style EXACTLY:

```python
    keywords = Column(JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb"))
```

(`JSONB` and `text` are already imported and used by sibling columns in this file — no new imports. `default=list` covers ORM-side inserts that omit `keywords`; `server_default` covers raw SQL inserts.)

## Access control (RLS)

`chat_turns` holds user data (it is keyed to a user via `chat_session_id → chat_sessions.user_id`). The new `keywords` column inherits the EXISTING table-level RLS — **no new policy, no new `ENABLE ROW LEVEL SECURITY` is required, and adding a column does NOT change row visibility.**

**Existing enforcement (verified, unchanged by this unit):**
- `database/migrations/005_chat_memory.sql:40` — `ALTER TABLE chat_turns ENABLE ROW LEVEL SECURITY;` (RLS already on).
- `database/migrations/006_membership_auth.sql:52-69` — policy `chat_turns_owner_rw ON chat_turns FOR ALL` scoped by a sub-select to `chat_sessions cs WHERE cs.id = chat_turns.chat_session_id AND cs.user_id = auth.uid()`. A turn is visible/writable only to the owner of its parent session.
- **Bypass role:** the backend connects via the Supabase service role (asyncpg DSN with the service key), which bypasses RLS by design — the application enforces ownership in `chats/service.py`/`server.py` via `get_chat_session_for_user(... user_id=auth.user_id)` and the FOR-UPDATE session lock before any turn write. RLS is the defense-in-depth layer for the PostgREST/anon path. State this explicitly: **the new column is readable/writable through the same service-role + app-layer ownership check; no anon/PostgREST consumer reads it (write-only column, no read path in v1).**

**RLS is a table-level row filter; a per-column GRANT/policy is unnecessary** — there is no scenario where a row is visible but the `keywords` column must be hidden. The column is default-deny-inherited: a tenant who cannot see the row cannot see its keywords.

**Positive test (authorized owner sees the row + its keywords):** asserted at the ORM round-trip layer (the backend's effective access path) — `tests/database/test_chat_turn_keywords_roundtrip.py::test_keywords_roundtrip_list_le_8` writes a `ChatTurn(keywords=[...])` under the session's owner and reads back the exact ≤8-term list (rows returned for the authorized write/read path).

**Negative test (different tenant gets zero rows):** `tests/database/test_chat_turn_keywords_rls.py::test_other_user_sees_zero_keyword_rows` — applies the 005+006 RLS policies on the migration-chain DB, sets a NON-service Postgres role with `SET LOCAL request.jwt.claims` (or `SET ROLE` + `set_config('request.jwt.claims', json, true)`) to a DIFFERENT `user_id`, and asserts `SELECT keywords FROM chat_turns WHERE chat_session_id = <owner's session>` returns ZERO rows. (Mirror the auth-claims-setting idiom already used in the repo's RLS tests; if no such idiom exists in `tests/database/`, the test sets `request.jwt.claims` via `set_config` on a raw asyncpg connection against the migration-chain DB where the 006 policy is live.) This proves the new column does not leak across tenants.

> Note: the negative RLS test runs against the **migration-chain DB** (where the 006 `chat_turns_owner_rw` policy is applied from the real SQL), NOT the `db_session` fixture (whose `Base.metadata.create_all` schema has no policies). This is the only place RLS is actually exercised, so the negative test MUST target the chain DB.

## ORM + service threading (public signatures)

All signatures keep backward compatibility (every new parameter is keyword-only with a safe default; every existing call site keeps working unchanged).

**`chats/service.py:append_turn`** — add ONE keyword-only parameter (place it after `citations`, before the body):

```python
async def append_turn(
    db_session: AsyncSession,
    *,
    chat_session_id: int,
    role: str,
    content: str,
    created_at: str | None = None,
    model: str | None = None,
    tool_name: str | None = None,
    tool_inputs: Dict[str, Any] | None = None,
    attachments: List[Dict[str, Any]] | None = None,
    citations: List[Dict[str, Any]] | None = None,
    keywords: List[str] | None = None,        # NEW — §10 RQ5 hedge; write-only
) -> ChatTurn:
```

In the `ChatTurn(...)` construction (`chats/service.py:133-145`), add the field, mirroring the `citations or []` style:

```python
    turn = ChatTurn(
        ...
        citations=citations or [],
        keywords=keywords or [],              # NEW — defaults to [] (never None)
    )
```

- **Backward-compat:** every existing caller (`chats/routes.py:150` save-chat import; `server.py:451` user-turn; `server.py:517` assistant-turn) omits `keywords` → defaults to `None` → coalesced to `[]` → identical to today's behavior. No caller is forced to change.
- **Immutability:** `keywords or []` constructs a NEW list (does not mutate the caller's list); the ORM column is assigned at construction, no in-place mutation of an existing row.
- **Defensive normalization (decision, LOCKED):** keep `append_turn` a thin persistence primitive — it does NOT re-filter or re-truncate; it trusts the caller's already-filtered ≤8-term list (the list is produced by `extract_and_filter_keywords`, which the spec L1463 already bounds to ≤8). A `None` becomes `[]`. (A belt-and-suspenders `list(keywords or [])[:8]` is an allowed deviation if the reviewer wants a hard cap at the persistence boundary — see "Deviations".)

**`database/models.py:ChatTurn`** — column added (see "Schema changes"). New public attribute: `ChatTurn.keywords` (JSONB, defaults `[]`).

**`ai/orchestrator.py`** — the already-computed `keywords: List[str]` (`:597-604`) is ALREADY assigned onto the returned bundle as `found_terms=keywords` (`:670` metadata `found_terms`, `:703` bundle-level `found_terms`). The orchestrator's public return contract (`ResearchBundle.found_terms` carries the ≤8 concept terms) is what a persist site consumes. **This unit's orchestrator change is contract-pinning only:** a focused unit test asserts `_iterative_research_pgvector(...).found_terms` equals the normalized ≤8-term list for a mocked `extract_and_filter_keywords`. If the reviewer judges `found_terms` too overloaded a name to also mean "persist-these-keywords", the allowed minimal additive change is to ALSO expose the same list under an explicit alias without removing `found_terms` (see "Deviations") — but the recommended path is to reuse `found_terms` unchanged (it already holds exactly this list) and add no new field, keeping the diff to the orchestrator at ZERO behavioral lines.

## The threading map (orchestrator -> persist site)

This is the exact data path the unit makes real, with file:line anchors (verified 2026-06-19):

```
extract_and_filter_keywords(keyword_query, subject)      ai/orchestrator.py:591
   -> filtered_terms                                     :591
   -> normalize to keywords: List[str]  (<=8 terms)      :597-604
   -> used as retrieval hints (retrieve_for_question)    :636/646/670/703   [UNCHANGED]
   -> set onto ResearchBundle.found_terms = keywords     :670 (metadata) / :703 (bundle)  [ALREADY EXISTS]
   ===================== persist edge (in production: server.py) =====================
   server.py:_ask_pgvector builds its own bundle         server.py:1473-1536  (found_terms also set)
   bundle in scope at the assistant-turn persist sites   server.py:1775,1796 (blocking) / :2089 (stream)
   -> _append_assistant_turn_and_refresh(...)            server.py:1852 / :2135
   -> append_turn(db, ..., keywords=bundle.found_terms)  chats/service.py:append_turn  [WU-5B4 write-API]
   -> ChatTurn(keywords=keywords or [])                  chats/service.py:133-145      [WU-5B4]
   -> chat_turns.keywords JSONB                           migration 029                [WU-5B4]
```

**What WU-5B4 owns on this path (in scope):** the orchestrator-side contract (`found_terms` carries the list — pinned by a test), the `append_turn(keywords=...)` write-API, the `ChatTurn.keywords` ORM column, and migration 029. The unit makes the persist site *able* to write the keywords.

**What WU-5B4 does NOT own (out of scope — `server.py` is not a scope file):** the single line in `server.py:_append_assistant_turn_and_refresh` (and its async body `:517`) that passes `bundle.found_terms` into `append_turn(keywords=...)`. That edge is the production wiring; it is a trivial follow-up in a server.py-touching unit. WU-5B4 documents it (see "Out-of-scope boundaries") and proves the WRITE-API works end-to-end at the service layer (a direct `append_turn(keywords=[...])` round-trip on real PG), NOT through the live `server.py` HTTP path.

**Why not edit server.py here?** The binding scope names `ai/orchestrator.py + chats/service.py` as the threading targets and excludes `server.py`. The orchestrator already carries the list to `found_terms`; the service layer is where the `ChatTurn` is built (RECON FACT 4). The orchestrator is, per `rag-pipeline.md:124`, not even constructed in production — so editing it to "thread" keywords it already exposes would be cosmetic. The honest, scope-faithful deliverable is: durable column + ORM + write-API + orchestrator contract test, with the server.py wire-up flagged as the documented next step. This is called out as a SIGNAL to the orchestrator (see status block) so it can schedule the server.py one-liner.

**Assistant-turn vs user-turn — which row carries the keywords?** The keywords describe the QUESTION (they are extracted from the user's question, `:585-593`), but they are computed DURING retrieval which runs AFTER the user turn is already committed (`server.py:1734` appends the user turn, then `:1796` runs retrieval). So in the production path the keywords are available only at the ASSISTANT-turn persist site (`:1852`). **Decision (LOCKED): persist on the ASSISTANT turn** (the turn whose retrieval produced them). This is a class-level coverage signal regardless of which turn row holds it; the assistant turn is where the bundle is in scope. Document this so the offline backfill query knows to read `keywords` off `role='assistant'` rows.

## Migration plan

Single-phase (non-destructive ADD COLUMN — see "Schema changes" for why no 2-phase is needed).

- [ ] **Phase 1 (and only phase) — migration file `database/migrations/029_chat_turn_keywords.sql`**
  - Create with the project convention (numbered raw SQL; there is no migration-generator CLI in this repo — files are hand-authored in `database/migrations/`, applied locally by the Testcontainers chain / `database/session.py` bootstrap). Write the file with the Write tool (content in "Schema changes").
  - Contents: `ALTER TABLE chat_turns ADD COLUMN IF NOT EXISTS keywords JSONB NOT NULL DEFAULT '[]'::jsonb;` wrapped in `BEGIN;`/`COMMIT;` with the §10 header comment and the inline rollback block.
  - Verify (local, on the Testcontainers chain DB built by the migration-029 test):
    `SELECT data_type, is_nullable, column_default FROM information_schema.columns WHERE table_name='chat_turns' AND column_name='keywords';`
    — expect `data_type=jsonb`, `is_nullable=NO`, `column_default='[]'::jsonb`.
  - Verify ORM picks it up: `ChatTurn.__table__.columns['keywords']` exists with `nullable=False`.
- [ ] **No Phase 2.** Nothing is dropped or retyped; the column is additive and write-only.

**Migration ordering / numbering:** 028 (`028_apollo_learner_janitor.sql`) is the on-disk top (RECON FACT 1; ORCHESTRATOR-VERIFIED — do not re-derive). 029 is next-free. There is NO 029 collision on disk (the historical 023 double-number `023_apollo_auth_scoping.sql` + `023_chunks_halfvec_hnsw.sql` is a known, separate artifact and does not affect 029). Confirm with `ls database/migrations/ | sort` before writing.

## Rollback plan

This repo has no `down.sql` files; rollback is an inline comment block in the migration (consistent with the repo convention — see `011`/`028`). Exact rollback SQL for migration 029:

```sql
-- Rollback for 029_chat_turn_keywords.sql:
BEGIN;
ALTER TABLE chat_turns DROP COLUMN IF EXISTS keywords;
COMMIT;
```

- **Safe to roll back any time:** the column is write-only with no consumer, so dropping it cannot break a read path. The only loss is the accumulated offline-backfill signal (acceptable — it is a hedge, not a live feature).
- **Code rollback:** revert the `ChatTurn.keywords` ORM line and the `append_turn(keywords=...)` param. Because both default to `[]`/`None`, reverting them is non-breaking even if some rows already carry keyword data (the data simply becomes unmapped/ignored, then dropped by the SQL rollback).
- **Forward-compat note:** if the SQL is rolled back but the ORM column is NOT reverted, `Base.metadata.create_all` (test harness) re-creates it, and production inserts would fail (`column "keywords" does not exist`). So roll back ORM + SQL together, or neither. The migration-029 idempotency test guarantees re-applying forward is a no-op.

## Test plan (TDD order)

**RED → GREEN, real tests first, no skips/xfail/assert-nothing.** All LLM/network calls are mocked deterministically (no live API). Tests marked `@pytest.mark.integration` require Docker (Testcontainers pgvector:pg16); the `_pg_url`/`db_session` fixtures already skip cleanly if Docker is down, but per the gate these MUST run GREEN-not-skipped on the executor's machine (Docker up). Unit tests need no Docker.

Write the tests in this order; each fails first against the unmodified tree, then the corresponding source edit turns it green.

### A. ORM-metadata unit tests (no DB) — `tests/database/test_models_029.py` (NEW)
1. **`test_chat_turn_has_keywords_column`** — asserts `"keywords" in {c.name for c in ChatTurn.__table__.columns}`. *Mocks:* none (class metadata only). *RED until* the ORM column is added.
2. **`test_chat_turn_keywords_column_shape`** — asserts the `keywords` column is `nullable is False`, its `type` is a `JSONB` instance, `default` callable is `list`, and `server_default.arg` text contains `'[]'::jsonb`. *Mocks:* none. *Asserts the exact citations/attachments-mirrored shape.*

### B. Service-layer write-API unit test (no DB) — `tests/chats/test_append_turn_keywords.py` (NEW)
3. **`test_append_turn_sets_keywords_on_built_turn`** — calls `append_turn(fake_session, chat_session_id=1, role="assistant", content="x", keywords=["energy","work"])` with a fake `AsyncSession` (an `AsyncMock` whose `execute` returns the lock row id `1` then `max_idx 0`), asserts the `ChatTurn` passed to `db_session.add(...)` has `.keywords == ["energy","work"]`. *Mocks:* `AsyncSession` via `AsyncMock`; the two `execute` results stubbed (`scalar_one_or_none → 1`, `scalar_one → 0`) so no real DB. *Proves the param threads to the row.*
4. **`test_append_turn_keywords_defaults_to_empty_list`** — same harness, call WITHOUT `keywords`; assert the built `ChatTurn.keywords == []` (the `keywords or []` coalesce), and that it is a NEW list object (immutability — not a shared default). *Mocks:* as above. *Proves backward-compat default + no shared mutable default.*
5. **`test_append_turn_keywords_none_coalesces_to_empty`** — pass `keywords=None` explicitly; assert `.keywords == []`. *Mocks:* as above. *Proves None-safety for legacy callers.*

### C. Orchestrator contract unit test (no DB, LLM mocked) — `tests/test_orchestrator_keywords.py` (NEW)
6. **`test_iterative_research_pgvector_carries_keywords_to_found_terms`** — construct an `Orchestrator` with a `ctx` carrying a `search_space_id` and a stub `db_session`; monkeypatch `ai.orchestrator.extract_and_filter_keywords` to return `("", [{"term":"momentum"},{"term":"impulse"},"force"])`; monkeypatch `ai.orchestrator.retrieve_for_question` (and `run_async`) to return `([], {"combined_query":"q"})` so retrieval is a no-op; monkeypatch `_check_question_relevance` to return `{"relevance":"full","on_topic_portion":""}`. Call `_iterative_research_pgvector("q", {})` and assert the returned `bundle.found_terms == ["momentum","impulse","force"]` (the normalized ≤8-term list, dict-`term` + bare-str both flattened per `:597-604`). *Mocks:* `extract_and_filter_keywords`, `retrieve_for_question`, `run_async`, `_check_question_relevance` — all monkeypatched; NO live OpenAI, NO DB. *Pins the orchestrator-side contract WU-5B4 relies on.*
7. **`test_keywords_capped_at_eight_terms`** — same harness, stub `extract_and_filter_keywords` to return 12 terms; assert `len(bundle.found_terms) <= 8` IF the orchestrator caps (verify against `:597-604` behavior — if the orchestrator does NOT cap and relies on `extract_and_filter_keywords`'s own ≤8 bound, assert the full list passes through unchanged and document that the ≤8 guarantee lives upstream). *Mocks:* as #6. *Pins the ≤8 invariant location — adjust the assertion to match the verified actual behavior; do NOT assert a cap the code does not have.*
8. **`test_keywords_empty_on_extractor_failure`** — stub `extract_and_filter_keywords` to raise; assert `bundle.found_terms == []` (the `except Exception: filtered_terms = []` fail-open at `:594-595`). *Mocks:* as #6. *Pins fail-open → empty list (so persistence writes `[]`, never crashes).*

### D. Migration-029 forward-chain on real PG — `tests/database/test_migration_029.py` (NEW, `@pytest.mark.integration`)
Mirror `tests/database/test_apollo_learner_janitor_migration.py` exactly (fresh DB on the session pgvector container; apply the in-order content-globbed `chat_*` migration chain via raw asyncpg; `_STUB_DDL` creates `auth.users` + `aita_search_spaces` the chain FKs reference). Content glob: migrations whose text matches `(CREATE TABLE IF NOT EXISTS|CREATE TABLE|ALTER TABLE)\s+chat_(sessions|turns)\b` (005, 006, 011, 015, 022, 029 auto-join). MIGRATED_DB_NAME = `chat_turn_keywords_migrations`.
9. **`test_migration_029_adds_keywords_column`** — query `information_schema.columns` for `table_name='chat_turns' AND column_name='keywords'`; assert `data_type='jsonb'`, `is_nullable='NO'`, `column_default` contains `'[]'::jsonb`. *Mocks:* none (raw asyncpg on the chain DB). *Proves the DDL applies in the real chain. MUST be GREEN not skipped.*
10. **`test_migration_029_is_idempotent`** — re-execute `029_chat_turn_keywords.sql` on the already-migrated DB; assert no error and the column still exists exactly once. *Mocks:* none. *Proves `IF NOT EXISTS` idempotency.*
11. **`test_existing_rows_default_to_empty_array`** — INSERT a `chat_turns` row using ONLY the pre-029 columns (raw SQL omitting `keywords`, simulating a legacy insert); assert `SELECT keywords FROM chat_turns WHERE id=...` returns `[]` (the server default). *Mocks:* none. *Proves the NOT NULL + server_default makes raw/legacy inserts safe (RECON FACT 2).*

### E. ChatTurn.keywords ORM round-trip on real PG — `tests/database/test_chat_turn_keywords_roundtrip.py` (NEW, `@pytest.mark.integration`)
Uses the `db_session` fixture (real pgvector, `Base.metadata.create_all` includes the new column). Seed a `ChatSession` (owner `user_id`, a `search_space_id` via the existing search-space seed helper) then `ChatTurn`s.
12. **`test_keywords_roundtrip_list_le_8`** — write a `ChatTurn(keywords=["a","b","c","d","e","f","g","h"])` (exactly 8); `commit`; re-read with `populate_existing=True`; assert the persisted `keywords` equals the exact 8-string list, in order. *Mocks:* none. *Proves the JSONB list round-trips (RECON-mandated ≤8-term list).*
13. **`test_keywords_default_empty_when_absent`** — write a `ChatTurn` WITHOUT `keywords` (omit the kwarg); `commit`; re-read; assert `keywords == []`. *Mocks:* none. *Proves the ORM `default=list` empty-default (RECON-mandated empty-default).*
14. **`test_keywords_via_append_turn_service`** — call `append_turn(db_session, chat_session_id=<seeded>, role="assistant", content="x", keywords=["entropy","heat"])`; `commit`; re-read the row by `turn_index`; assert `keywords == ["entropy","heat"]`. *Mocks:* none (real PG). *Proves the SERVICE write-API persists end-to-end (the orchestrator->chats/service threading target — a saved chat turn carries the computed keywords).*
15. **`test_keywords_unicode_and_multiword_terms`** — round-trip terms with spaces/unicode (`["mécanique","angular momentum"]`); assert exact. *Mocks:* none. *Proves JSONB handles realistic concept terms, not just ASCII tokens.*

### F. RLS negative test on the chain DB — `tests/database/test_chat_turn_keywords_rls.py` (NEW, `@pytest.mark.integration`)
16. **`test_other_user_sees_zero_keyword_rows`** — on the migration-chain DB (006 policy live): INSERT a `chat_sessions` row owned by user A and a `chat_turns` row with `keywords=["x"]`; open a connection acting as a NON-service role with `request.jwt.claims` set to user B (`set_config('request.jwt.claims', '{"sub":"<B-uuid>"}', true)`); assert `SELECT keywords FROM chat_turns WHERE chat_session_id=<A's session>` returns ZERO rows. Then set claims to user A and assert the row (and its `["x"]`) IS visible. *Mocks:* none (real RLS). *Proves the new column does not leak across tenants — the mandatory negative access-control test.*

### G. NO-BEHAVIOR-CHANGE assertion — `tests/test_keywords_no_behavior_change.py` (NEW, unit)
17. **`test_retrieval_output_identical_with_and_without_persist`** — the column is write-only; persisting `keywords` must not alter retrieval/answer. With the same mocked `extract_and_filter_keywords`/`retrieve_for_question`, call `_iterative_research_pgvector` and snapshot `bundle.snippets`, `bundle.used_ids`, `bundle.found_terms`, `bundle.metadata.final_query`. Assert these are byte-identical to a golden snapshot captured from the UNMODIFIED orchestrator behavior (i.e. adding the persist path changed nothing in the bundle). *Mocks:* as #6. *Proves the unit changes no answer/retrieval result/score — the RECON-mandated no-behavior-change guarantee.*
18. **`test_append_turn_keywords_does_not_touch_other_columns`** — `append_turn(..., keywords=[...])` vs `append_turn(...)` (no keywords) build `ChatTurn`s identical in every OTHER field (`role`, `content`, `turn_index`, `attachments`, `citations`, `model`). *Mocks:* fake `AsyncSession` (as #3). *Proves the new param is isolated — no side effect on existing columns.*

**Coverage of changed lines:** the changed lines are (a) `database/migrations/029_*.sql` — covered by tests 9-11 (the migration body executes in the chain); (b) `database/models.py` ChatTurn column — covered by tests 1-2 + 12-13; (c) `chats/service.py` append_turn param + ChatTurn field — covered by tests 3-5 + 14 + 18; (d) `ai/orchestrator.py` — if only the contract is pinned (recommended, zero behavioral change), the existing `:597-604,703` lines are exercised by tests 6-8 + 17 (note: these lines may already be covered by existing orchestrator tests — confirm `--cov=ai` includes `orchestrator.py` so diff-cover does not report a changed file at 0%; if the orchestrator edit is literally zero lines, there is nothing new to cover there). **Every changed line maps to ≥1 test above; no line relies on "hard to test".**

## Owner-doc updates

Drift contract: reconcile BOTH owner docs in the SAME commit as the code, bumping `last_verified` to **2026-06-19**.

> **`last_verified` date note:** the task's binding-constraints text says "2026-06-16" in one clause and "2026-06-19" in others; the STALE-NOTE resolves this explicitly — "workflow_hardenings 'last_verified 2026-06-16' is stale; use **2026-06-19**." Today is 2026-06-19. Use **2026-06-19** for both docs.

**`docs/architecture/domain-data.md`** (owner of `database/**` + `chats/**`):
- Frontmatter `last_verified: 2026-06-17` → `last_verified: 2026-06-19`.
- Line 50 (the `ChatTurn` field list) — append `keywords` to the column enumeration:
  `... \`attachments\` JSONB, \`citations\` JSONB, \`keywords\` JSONB (§10 RQ5 hedge — write-only ≤8 concept terms from \`extract_and_filter_keywords\`, persisted for offline class-level backfill; no read path in v1).`
- Line 66 (`append_turn` signature description) — note the new optional param:
  `append_turn(db, *, chat_session_id, role, content, ..., keywords=None) -> ChatTurn` … "accepts an optional write-only `keywords` list (≤8 concept terms) persisted to `chat_turns.keywords`; defaults to `[]`."
- Add a one-line migration note in the migrations/data-flow section: "029 — `chat_turns.keywords` JSONB (§10 RQ5 chat-keyword hedge, write-only)."

**`docs/architecture/rag-pipeline.md`** (owner of `ai/**`):
- Frontmatter `last_verified: 2026-06-12` → `last_verified: 2026-06-19`.
- Step 5 "Keyword extraction" (line ~99-100) — add a reconciling sentence: "As of WU-5B4 (§10 RQ5 hedge), the ≤8 extracted concept terms (carried on `ResearchBundle.found_terms`) are also PERSISTED to `chat_turns.keywords` at the assistant-turn write — a write-only offline-backfill signal; this does NOT change retrieval (keywords remain hints only)."
- If a "Keywords are hints, never substitutes" invariant line exists (line ~132), append: "…and are now additionally persisted (write-only) for §10 RQ5 backfill, with no effect on retrieval."

**No new module/route to register** (no new package; the new files live in existing packages `database/`, `chats/`, `ai/`, `tests/`). No `shared-architecture/README.md` change needed.

## Verification commands (ALL local — executor runs these)

Run from `ai-ta-backend/`. Python is `.venv/Scripts/python.exe` (py3.12). Docker MUST be UP for the integration gates. NO migration is applied to any remote DB — Testcontainers/local only.

- [ ] **Confirm 029 is next-free:** `ls database/migrations/ | sort | tail -3` — expect `027_...`, `028_apollo_learner_janitor.sql`, `029_chat_turn_keywords.sql`.
- [ ] **Gate 1 — no regression in the RAG/chat path (changed-file suites):**
  `.venv/Scripts/python.exe -m pytest tests/ -q`
  (or, at minimum, the changed-file modules: `tests/chats tests/database tests/test_orchestrator_keywords.py tests/test_keywords_no_behavior_change.py tests/database/test_models_029.py`) — expect all GREEN. This proves the user-turn/assistant-turn persist + orchestrator path is unbroken.
- [ ] **Gate 2 — REAL-INFRA (migration 029 forward-chain + ChatTurn.keywords JSONB round-trip), GREEN-not-skipped:**
  `.venv/Scripts/python.exe -m pytest tests/database -v`
  — the new `test_migration_029.py`, `test_chat_turn_keywords_roundtrip.py`, `test_chat_turn_keywords_rls.py` MUST run GREEN (a SKIP = Docker down = a FAIL of this gate). Verifies: migration-029 applies in the real `chat_*` chain; the JSONB column round-trips a ≤8-term list; empty-default on absent/legacy insert; cross-tenant RLS returns zero rows.
- [ ] **Manual real-PG spot check (optional, via psql in the Testcontainers DB):**
  `SELECT column_name, data_type, is_nullable, column_default FROM information_schema.columns WHERE table_name='chat_turns' AND column_name='keywords';`
  — expect one row: `keywords | jsonb | NO | '[]'::jsonb`.
- [ ] **Gate 3 — COVERAGE over the CHANGED packages (NOT apollo):**
  `.venv/Scripts/python.exe -m pytest --cov=chats --cov=ai --cov=database --cov-report=xml -q`
  then
  `.venv/Scripts/python.exe -m diff_cover.diff_cover_tool coverage.xml --compare-branch=feat/apollo-kg-wu5b3b-janitor-worker --fail-under=95`
  (or `diff-cover coverage.xml --compare-branch=feat/apollo-kg-wu5b3b-janitor-worker --fail-under=95` if the console script is on PATH).
  - **`chats`, `ai`, `database` MUST appear in `coverage.xml`.** If diff-cover reports a changed file at 0%, `--cov` omitted its package — FIX the `--cov` args (do NOT accept the 0%). `--cov=apollo` may be added if convenient but is NOT required for this unit (it touches no apollo file).
  - Expect: diff-cover ≥ 95% on changed lines vs `feat/apollo-kg-wu5b3b-janitor-worker`.
- [ ] **Lint (if wired):** `.venv/Scripts/python.exe -m ruff check database/models.py chats/service.py ai/orchestrator.py database/migrations/029_chat_turn_keywords.sql` — expect no new warnings. (There is no dedicated SQL linter in this repo; the migration is validated by the real-PG chain test in Gate 2.)
- [ ] **STOP.** The executor's job ends here. NO `supabase db push`, NO remote `apply_migration`, NO remote DDL/DML. Deploying 029 to the test/prod Supabase projects is the HUMAN/CI step below.

## Deploy handoff (HUMAN/CI only — never executed by feller)

These steps are for the human/CI deploy pipeline, NOT for any feller agent. All remote Supabase projects are read-only to agents.

1. **Rehearse on the TEST Supabase project first** (`hjevtxdtrkxjcaaexdxt` / `supabase-test`): apply `029_chat_turn_keywords.sql` against the test project. Note: per memory, the test DB has a known migration-numbering drift (023 collision) — confirm `chat_turns` exists and lacks `keywords` before applying; the `IF NOT EXISTS` guard makes a re-run safe.
2. **Sanity query on test:** `SELECT column_name, data_type, column_default FROM information_schema.columns WHERE table_name='chat_turns' AND column_name='keywords';` — expect `jsonb | '[]'::jsonb`. Then `SELECT count(*) FROM chat_turns WHERE keywords IS NULL;` — expect 0 (NOT NULL + default).
3. **Supabase advisor check** (lint) on test after apply — confirm no new RLS/security advisory regression on `chat_turns` (the existing `chat_turns_owner_rw` policy still covers the table; the new column adds no exposure).
4. **Promote to PROD Supabase** (`uduxdniieeqbljtwocxy` / `supabase`) only after test rehearsal is green and the PR merges through `staging → ApolloV3`. Re-run the same sanity queries against prod.
5. **Wire the production caller (separate server.py-touching change):** the one-line edit in `server.py:_append_assistant_turn_and_refresh` to pass `bundle.found_terms` into `append_turn(keywords=...)` is OUT of WU-5B4's scope (see "Out-of-scope boundaries"). Until that lands, the column exists and round-trips but is written `[]` by the live path. Schedule it as the immediate follow-on.

**Migration numbering caution for the deployer:** the on-disk chain has a historical double-`023`. 029 is unambiguous on disk, but the deployer should confirm the remote project's applied-migration ledger before pushing (the test and prod projects may be at different effective states per memory `staging-backend-targets-test-supabase`).

## Downstream consumers

Grep of the app source for anyone reading/writing the new column (run to confirm before/after):
```bash
grep -rn "\.keywords\b\|chat_turns\.keywords\|append_turn(" chats/ server.py ai/ retrieval/ reports/ knowledge/
```
Findings (verified 2026-06-19):
- **`chats/service.py:serialize_chat_session`** (`:186-200`) — serializes turn fields for the GET `/chats/{chat_id}` response. It currently does NOT emit `keywords`, and WU-5B4 does NOT add it (write-only; the column must not surface to the client UI in v1). **No change.** (If a future unit wants to expose it, that is a deliberate read-path addition, out of scope here.)
- **`server.py`** — the two `/ask` persist sites (`:1852`, `:2135`) call `append_turn` via `_append_assistant_turn_and_refresh`; they have `bundle` (carrying `found_terms`) in scope. These are the FUTURE writers of the column (out of scope, see boundaries). No current read of `keywords`.
- **`chats/routes.py:save_chat`** (`:150`) — the import/upsert path; replays client-supplied turns. It does NOT pass `keywords` (client payloads have none) → defaults to `[]`. **No change; backward-compatible.**
- **`chats/bundle_cache.py`** — caches snippet bundles, not turn rows; does not touch `keywords`. **No change.**
- **No SQL view, RPC, or report** references `chat_turns.keywords` (grep returns nothing; confirmed the column has zero consumers today).

**Net:** the column has exactly ZERO read consumers in v1 (by design — it is the offline-backfill hedge). The only writer this unit enables is the `append_turn(keywords=...)` service API; the production `server.py` wire-up is the documented follow-on.

## Out-of-scope boundaries (this unit)

Explicit lines WU-5B4 must NOT cross:

1. **NO `apollo/` changes.** This is a chats/RAG-pipeline + database concern (the §10 chat-keyword hedge), NOT the §3 negotiation multiplier (that shipped as WU-5B2). Do not touch any file under `apollo/`. Do not run `pytest apollo` as the gate (per the binding constraints — use the chats/ai/database suites).
2. **NO `server.py` edit.** `server.py` is not a scope file. The production wire-up (`bundle.found_terms → append_turn(keywords=...)`) is a documented follow-on (Deploy handoff #5). WU-5B4 ships the column + ORM + write-API + orchestrator contract; it does NOT light up the live `/ask` path. This is a deliberate scope boundary, surfaced as a SIGNAL so the orchestrator can schedule the one-liner.
3. **NO read/consumer path.** The column is write-only in v1. Do not add it to `serialize_chat_session`, any teacher dashboard, any report, or any learner-model fold. "Class-level signal first, never a hard per-student negative" (spec L1466) — there is NO online consumer.
4. **NO recompute / NO retrieval change.** Use the keywords ALREADY computed by `extract_and_filter_keywords` (RECON FACT 3). Do not add a second extraction call, do not change the LLM model/prompt, do not change `retrieve_for_question` behavior, do not change which keywords are passed as hints.
5. **NO new packages / dependencies** (binding constraint). All new files live in existing packages.
6. **NO migration applied to any remote DB** — Testcontainers/local only. Migration FILE only; remote apply is the human/CI deploy step.
7. **NO branch/PR operations** — work only on `feat/apollo-kg-wu5b4-chat-keyword-persist`; do not create/switch branches, push, or open PRs.
8. **NO refactor of the oversized `_iterative_research_pgvector`** (flagged in the neighborhood scan) — deferred to a separate RAG-pipeline task to protect the no-behavior-change invariant.
9. **The orchestrator change is contract-only (zero behavioral lines preferred).** Do not restructure the keyword block; the recommended diff to `ai/orchestrator.py` is effectively zero source lines (the list already reaches `found_terms`), with the contract pinned by tests. If an explicit alias field is added (a Deviation), it must be purely additive.

## Risks

Confidence-rated.

- **[HIGH confidence / LOW impact] The orchestrator may contribute ZERO changed lines, making "diff-cover on `ai/`" vacuous for this unit.** Because `found_terms` already carries the list, the recommended orchestrator change is no source edit. That is FINE — diff-cover only fails on UNCOVERED changed lines; if `ai/orchestrator.py` has no changed lines, it contributes nothing to the diff and cannot fail the gate. The risk is the executor adds an unnecessary field just to "have something to test" — DON'T. The orchestrator tests (6-8, 17) still run and prove the contract. If the executor DOES add an explicit alias field (Deviation), tests 6-7 cover it.
- **[HIGH / LOW] Lock duration on `ADD COLUMN` for a large `chat_turns`.** Postgres 11+ makes a constant-default `ADD COLUMN` metadata-only — a brief catalog `ACCESS EXCLUSIVE` lock (milliseconds), NOT a full rewrite. No backfill `UPDATE`. Estimated lock: < 50ms regardless of row count. Negligible.
- **[MEDIUM / MEDIUM] The RLS negative test is the trickiest test to get right.** Setting `request.jwt.claims` on a non-service connection against the chain DB requires the 006 policy to be live AND the connection to act as a role that does NOT bypass RLS. If the repo has no existing `request.jwt.claims`-setting idiom in `tests/database/`, the executor must write it carefully (use `set_config('request.jwt.claims', '{"sub":"<uuid>"}', true)` inside the same txn, and ensure the connecting role is not the superuser/owner — `SET ROLE` to a non-bypass role, or rely on the policy's `auth.uid()` reading the claim). Mitigation: the plan accepts an ALTERNATIVE positive-only enforcement-statement form ONLY if the RLS test proves infeasible on the harness — but the negative test is the access-control mandate, so spend the effort. If `auth.uid()` is a Supabase-specific function absent on the bare pgvector image, the executor must stub it (`CREATE FUNCTION auth.uid() RETURNS uuid AS $$ SELECT (current_setting('request.jwt.claims', true)::json->>'sub')::uuid $$ LANGUAGE sql;` in the test's `_STUB_DDL`) — this is the standard local-Supabase-RLS test shim. Document the shim in the test.
- **[MEDIUM / LOW] `db_session` fixture has NO RLS policies** (`Base.metadata.create_all` skips policies). So the ORM round-trip tests (12-15) prove persistence but NOT isolation; the RLS negative test (16) MUST run on the migration-chain DB where 006 is applied. The plan already routes the negative test to the chain DB — do not accidentally write it against `db_session`.
- **[MEDIUM / LOW] Coverage package omission.** If `--cov=database` is dropped, the migration file / models line shows 0% and diff-cover fails. The plan pins the exact `--cov=chats --cov=ai --cov=database` triad. The migration `.sql` itself is not a Python module — its "coverage" is the real-PG chain test executing it (diff-cover measures Python lines; the `.sql` changed lines are validated by Gate 2's GREEN integration run, and a `.sql` file is not counted by `coverage.py`). State in the PR that the migration's coverage is the integration test, per the CLAUDE.md DB-coverage clause (enumerate behaviors, not line coverage, for SQL).
- **[LOW / LOW] Assistant-turn vs user-turn placement.** Keywords describe the question but are computed post-user-turn; they land on the assistant turn (LOCKED). The offline backfill must read `keywords` off `role='assistant'` rows. Documented in the threading map + owner doc. A future change could also stamp the user turn, but that needs a second persist call (out of scope).
- **[LOW / NEGLIGIBLE] `extract_and_filter_keywords` ≤8 guarantee location.** Test 7 must assert the ACTUAL behavior (the orchestrator normalization at `:597-604` vs the extractor's own bound) — do NOT assert a cap the code does not enforce. The spec's ≤8 (L1463) is an upstream property; if the orchestrator does not re-cap, the test documents that and asserts pass-through.
- **[LOW] Compare-branch correctness.** diff-cover compares against `feat/apollo-kg-wu5b3b-janitor-worker` (NOT `origin/staging`). Ensure that ref is fetched locally (`git fetch origin feat/apollo-kg-wu5b3b-janitor-worker` if absent) or diff-cover errors with "unknown revision".

## Deviations I'd allow the executor

```
- ORM column name: `keywords` is preferred (mirrors the §10 column intent). If the
  reviewer wants a more specific name (`chat_keywords`, `concept_keywords`), allowed —
  but keep migration filename, SQL column, and ORM attribute in sync, and update the
  owner doc + tests to match.
- Hard ≤8 cap at the persistence boundary: `keywords or []` is the recommended thin
  form. A defensive `list(keywords or [])[:8]` in `append_turn` is ALLOWED (belt-and-
  suspenders for any non-orchestrator caller); if added, add a test asserting a 12-term
  input persists exactly 8.
- Orchestrator explicit alias: reusing `found_terms` unchanged is recommended (zero
  source lines). If the reviewer insists on an explicit, self-documenting carrier, an
  ADDITIVE field (e.g. a `chat_keywords` property returning `found_terms`) is allowed —
  must not remove or rename `found_terms`, must be covered by tests 6-7.
- Test file placement: new tests may live under `tests/`, `tests/chats/`, or
  `tests/database/` as the plan specifies; moving an individual test between these
  dirs is fine as long as integration tests stay under `tests/database/` (the marker +
  fixture convention) and unit tests carry `@pytest.mark.unit`.
- RLS test shim: the exact mechanism for the cross-tenant negative test (`SET ROLE` +
  `set_config` vs a stubbed `auth.uid()` function in `_STUB_DDL`) is left to the
  executor — any form that genuinely exercises the 006 policy and returns ZERO rows for
  a different `user_id` is acceptable.
- Migration header comment wording may be tightened, but MUST cite the spec §10/§12
  anchors and state "write-only, no read path in v1".
- If `pytest tests/ -q` (Gate 1) surfaces a PRE-EXISTING unrelated failure (not caused
  by this change), the executor may scope Gate 1 down to the changed-file modules listed
  in Gate 1 and note the pre-existing failure in the PR — do NOT fix unrelated breakage
  in this unit.
```

