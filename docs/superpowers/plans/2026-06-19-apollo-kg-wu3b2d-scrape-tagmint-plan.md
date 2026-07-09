# Plan: WU-3B2d — LLM scrape (stage 1) + tag/mint (stage 4) + author canonical_symbols

**Goal:** Read a document's already-embedded `AITAChunk` rows, scrape candidate questions → Tier-1 `apollo_concept_problems` rows keyed on a stable `chunk_content_hash`; and, given an approved (question, reference-solution) pair, tag concepts/entities, AUTHOR the concept's `canonical_symbols`/`normalization_map`, draft prereq edges, and mint reference + misconception entities by reusing the frozen §8 seed converters — resolving each candidate through 3B2c's dedup ladder before upsert.

**Architecture (pipeline shape, this unit's two stages):**
```
[existing AITAChunk rows] → scrape_questions (cheap_chat, MOCKED) → Tier-1 apollo_concept_problems (tier=1 EXPLICIT, provenance.chunk_content_hash, ON CONFLICT no-op)
[ApprovedPair fixture] → tag_and_mint (cheap_chat MOCKED + embed_fn injected) → author apollo_concepts.canonical_symbols/normalization_map → resolve_candidate (3B2c) → mint apollo_kg_entities (reference + kind='misconception') + apollo_entity_prereqs → MintPlan
```

**Tech stack:** FastAPI/SQLAlchemy async + asyncpg, Supabase Postgres (real-PG Testcontainers for the DB tests), OpenAI `cheap_chat`/`embed_text` INJECTED (mocked in Tier-1), Pydantic v2, the frozen `apollo/persistence/learner_model_seed.py` converters. NO new package.

---
provides:
  - `apollo/provisioning/scrape.py` — `CandidateQuestion` (Pydantic) + `ScrapeResult` + `async scrape_questions(chunks, *, chat_fn) -> ScrapeResult` + `async write_tier1_problems(db, candidates, *, concept_id, search_space_id) -> int`
  - `apollo/provisioning/tag_mint.py` — `ApprovedPair` / `MintPlan` (Pydantic) + `async tag_and_mint(db, pair, *, chat_fn, embed_fn) -> MintPlan`
  - the additive frozen-map extension `'variable_mapping': ('variable','varmap')` in `_ENTRY_TYPE_TO_KIND_PREFIX`
consumes:
  - `database.models.AITAChunk` (READ ONLY — never re-indexes)
  - `apollo.persistence.models` (`ConceptProblem`, `Concept`, `KGEntity`, `EntityPrereq`, `Subject`)
  - TWO frozen seed converters `reference_solution_to_entities` / `misconceptions_to_entities` (IMPORTED, not reimplemented). `concept_dag_to_prereqs` and `annotate_reference_solution` are NOT used by `tag_and_mint`: prereqs come from the LLM tag draft (auto-provisioning drafts them before a concept-DAG exists), and `annotate_reference_solution` is a promotion-time step owned by 3B2g.
  - `apollo.provisioning.resolve_candidate` (3B2c dedup ladder)
  - `apollo.agent._llm.cheap_chat` (injected `chat_fn`, returns `str`) + `indexing.document_embedder.embed_text` (injected `embed_fn`)
depends_on:
  - WU-3B2a (migration 030 ORM: `ConceptProblem.tier/provenance/search_space_id`, `KGEntity.scope_summary`) — ALREADY on this branch
  - WU-3B2c (`resolve_candidate`, `DedupVerdict`) — ALREADY on this branch
  - WU-3B2b (`run_promotion_lint` gate-1 mint-map sub-check is the downstream consumer of the frozen-map extension) — ALREADY on this branch
---

## Overview

WU-3B2d is the LLM-bearing front of the §8B auto-provisioning pipeline, fused from two §8B.2 stages by the planner/cost boundary (NOT by call order):

- **Stage 1 (SCRAPE):** one `cheap_chat` pass over a document's already-embedded `AITAChunk` rows → a list of `CandidateQuestion` records (provenance + LLM `difficulty` + problem fields). These are written to `apollo_concept_problems` as **Tier 1 inventory** — explicitly `tier=1`, NOT teachable. Keyed on a stable `chunk_content_hash` so a re-index (which re-mints `aita_chunks.id`) is a no-op.
- **Stage 4 (TAG/MINT):** given an ALREADY-approved `(question, reference_solution)` pair (an `ApprovedPair`; 3B2e produces a compatible value later, 3B2g wires the runtime order), `tag_and_mint` LLM-drafts the concept tag + prereq edges, **AUTHORS** the concept's `canonical_symbols`/`normalization_map` from the approved problem's symbol set (first-writer-wins; later problems UNION — NOT derive-from-promoted, which is circular because gate 4 runs *before* promotion), resolves each entity candidate through 3B2c's `resolve_candidate` dedup ladder, then mints reference + misconception EntitySpecs by REUSING the frozen seed converters and upserting them via the seed-script's persistence pattern.

`tag_and_mint` is TESTED IN ISOLATION against a hand-built `ApprovedPair` fixture — 3B2e is NOT built yet, and the runtime order (3B2g feeds 3B2e's `ApprovedPair` into `tag_and_mint`) is 3B2g's wiring, out of scope here.

The two load-bearing safety properties this unit must establish:
1. **The Tier-1 safety trap.** The `ConceptProblem` ORM `tier` DEFAULT is `2` (a 3B2a choice, correct for seeded teachable curriculum). A scraped inventory row that omits `tier` would therefore become **immediately teachable** — a safety hole. Scrape MUST insert `tier=1` EXPLICITLY, and a test must prove a scraped Tier-1 row is EXCLUDED by `list_problems_for_concept`.
2. **Gate 4 non-vacuity.** Gate 4 (the SOLE foreign-symbol guard, since SymPy gate 6 auto-creates unknown symbols) reads `apollo_concepts.canonical_symbols`/`normalization_map`, which are `default=dict` (EMPTY) for a fresh concept. `tag_and_mint` MUST author them so a 3B2b gate-4 over the minted concept is non-vacuous.

## Prior art in repo

- **3B2c dedup ladder** (`apollo/provisioning/dedup.py`, `resolve_candidate`) — the EXACT injection pattern this unit copies: sync `embed_fn`/`judge_fn` callables param-injected; course-scoped; writes one audit row. `tag_and_mint` CALLS `resolve_candidate` before each entity upsert. The candidate duck-type it consumes is `{canonical_key, scope_summary}` (dedup.py:103-111) — `tag_and_mint`'s minted EntitySpec → candidate adapter must expose those two attributes.
- **`scripts/seed_apollo_learner_model.py`** (`_resolve_concept` :137, `_upsert_entity` :165, `_link_opposes` :200, `_insert_prereqs` :232) — the persistence-write TEMPLATE to PARALLEL (generalized: concept from LLM tag, not the hardcoded `_BERNOULLI_SLUG`; reads from the `ApprovedPair`, not from disk). The seed script is WRAPPED, not imported — it hardcodes bernoulli.
- **Frozen pure converters** (`apollo/persistence/learner_model_seed.py`): `reference_solution_to_entities` :202, `misconceptions_to_entities` :240 (emits `opposes_entity_key` payload), `concept_dag_to_prereqs` :133, `annotate_reference_solution` :272. IMPORT, do NOT reimplement. `_ENTRY_TYPE_TO_KIND_PREFIX` :80-86 gets the single additive edit.
- **Mock-LLM test template** (`apollo/agent/tests/test_leakage_judge.py:31-36`): `patch(target, return_value=json.dumps(payload))`. Scrape/tag tests patch the injected `chat_fn` the same way (or pass a deterministic stub directly, mirroring 3B2c's `_judge_merged`).
- **Real-PG seeding helper** (`apollo/provisioning/tests/test_dedup.py:119-149` `_seed_course`): the SearchSpace → Subject → Concept → KGEntity build + the `db_session` fixture (re-exported in `apollo/conftest.py` from `tests/conftest.py:163`). Copy this for the real-PG upsert/idempotency tests.
- **3B2b gate-1 mint-map sub-check** (`apollo/provisioning/promotion_lint.py`): the DOWNSTREAM consumer that proves the frozen-map extension is load-bearing — a `variable_mapping` entry_type now passes the gate-1 mint-map membership sub-check instead of failing CLOSED.

## Structural prep (from neighborhood scan)

Neighborhood scanned: the two NEW files in the change path (`scrape.py`, `tag_mint.py`), the frozen-module edit (`learner_model_seed.py`), and their direct dependents (the seed converters, `resolve_candidate`, the ORM).

- [x] **None — neighborhood is clean.** Imports per new module stay ≤ 8 (scrape: `AITAChunk`, `ConceptProblem`, pydantic, hashlib, re, sqlalchemy select, typing — 7; tag_mint: the 4 seed converters via one import, `resolve_candidate`, the ORM models, pydantic, typing — ≤ 8). `learner_model_seed.py` is a single additive dict key (no responsibility added). No shared-state coupling: stages communicate via typed Pydantic records + the queue/DB contract, never via a shared module global. No retry sprawl (retry/DLQ is 3B2f/3B2g, out of scope). The seed-write pattern is PARALLELED into a small focused helper, not copied into a god-function.
- Budget: 0% of plan steps. No prep gate before feature work.
- Verify: `.venv/Scripts/python.exe -c "import ast,sys; [print(f) or sys.exit(1) for f in ['apollo/provisioning/scrape.py','apollo/provisioning/tag_mint.py'] if len([n for n in ast.walk(ast.parse(open(f).read())) if isinstance(n,(ast.Import,ast.ImportFrom))])>8]"` (after files exist; expects no output).

## Pipeline shape diagram

This unit owns TWO boxes of the §8B six-stage chain (stage 1 + stage 4). The trigger/enqueue/worker/queue boxes are 3B2g/3B2f (out of scope, shown for context in brackets).

```
[teacher upload → indexing → AITAChunk rows]   (FROZEN upstream — READ ONLY)
        │
        ▼
  scrape_questions(chunks, *, chat_fn)          ◄── STAGE 1 (this unit)
        │  owner: apollo/provisioning/scrape.py ; chat_fn = cheap_chat (injected, MOCKED in Tier-1)
        │  retry: NONE here (the queue-drain 3B2f owns retry/lease); a per-chunk LLM/parse failure
        │         drops that chunk's candidates and is surfaced as an error record to the caller (3B2g writes apollo_ingest_errors)
        │  failure mode: malformed/empty LLM JSON → that chunk yields ZERO candidates (fail-soft per chunk), never a partial half-parsed row
        ▼
  write_tier1_problems(db, candidates, *, concept_id, search_space_id)
        │  owner: apollo_concept_problems (tier=1 EXPLICIT, provenance={document_id,page,chunk_content_hash}, search_space_id denormalized)
        │  idempotency: UPSERT keyed on (search_space_id, document_id, chunk_content_hash) → re-run inserts ZERO rows
        │  failure mode: a DB error raises to the caller's transaction (3B2g owns commit/rollback)
        ▼
[ 3B2e: find-or-generate + pairing gate → ApprovedPair ]   (NOT built — fixture stands in)
        │
        ▼
  tag_and_mint(db, pair, *, chat_fn, embed_fn)  ◄── STAGE 4 (this unit)
        │  owner: apollo/provisioning/tag_mint.py
        │  author: apollo_concepts.canonical_symbols / normalization_map (first-writer-wins; later UNION)
        │  dedup: resolve_candidate (3B2c) per entity candidate BEFORE upsert
        │  mint: apollo_kg_entities (reference EntitySpecs + kind='misconception') + apollo_entity_prereqs
        │  chat_fn = cheap_chat (concept tag + prereq draft, MOCKED) ; embed_fn = embed_text (scope_summary embed, injected; resolve_candidate uses it)
        │  retry: NONE here (3B2f). failure mode: a hallucinated/unmappable LLM tag → raise a named error (NO silent mislink); the caller marks the run failed
        ▼
  MintPlan (typed return: concept_id resolved BIGINT, minted entity ids, prereq pairs, dedup verdicts)
        │
        ▼
[ 3B2g orchestrator: run_promotion_lint (3B2b) → flip Tier-2 → project_canon ]   (out of scope)
```

**Per-box Supabase/ownership table:**

| Box | Owns | Retry | Failure mode |
|-----|------|-------|--------------|
| `scrape_questions` | `apollo/provisioning/scrape.py`; injected `chat_fn` | none (3B2f) | malformed LLM JSON → 0 candidates for that chunk (fail-soft); never a partial row |
| `write_tier1_problems` | `apollo_concept_problems` (tier=1) | none | DB error → raise to caller's txn |
| `tag_and_mint` | `apollo/provisioning/tag_mint.py`; injected `chat_fn`+`embed_fn`; calls `resolve_candidate` | none (3B2f) | unmappable LLM tag → named raise (fail-closed, no mislink) |
| concept authoring | `apollo_concepts.canonical_symbols/normalization_map` | n/a | first-writer-wins; UNION on re-mint (idempotent) |
| entity mint | `apollo_kg_entities` (+ `kind='misconception'`), `apollo_entity_prereqs` | n/a | upsert on `(concept_id, canonical_key)`; prereq ON CONFLICT skip |

## Idempotency

**Stage 1 (scrape → Tier-1 write):**

- **Idempotency key:** `chunk_content_hash = sha256(_normalize(chunk.content)).hexdigest()` — a CONTENT hash, NOT the volatile `aita_chunks.id`. `_normalize` collapses internal whitespace + strips + lowercases (REUSE the exact normalization shape from `problem_hash.py:26-29` so behavior is consistent; a local helper, not an import, to keep `problem_hash` pure to its gate-8 role).
- **DB conflict target (RESOLVED against the real schema — load-bearing):** `apollo_concept_problems` has exactly ONE unique constraint usable as a conflict target: `UNIQUE (concept_id, problem_code)` (migration 018:86). 030 stores `chunk_content_hash` only INSIDE the `provenance` JSONB and adds NO unique index on it — so an `ON CONFLICT (search_space_id, document_id, chunk_content_hash)` would have no backing index and FAIL. **Resolution (no new migration):** the Tier-1 writer sets `problem_code = f"scrape.{chunk_content_hash}"` (a deterministic, content-derived code) and `provenance['chunk_content_hash'] = chunk_content_hash`, then `INSERT ... ON CONFLICT (concept_id, problem_code) DO NOTHING`. Because the provisional-inventory concept is per-`(subject, search_space)` and a scrape job is per-document under that concept, `(concept_id, problem_code)` is equivalent to the intended `(search_space_id, document_id, chunk_content_hash)` scope for the no-op, using the EXISTING constraint. NO new index, NO migration edit.
- **Why content-hash:** `embed_and_persist_chunks` "deletes and reinserts that page's chunks" on re-index, so `aita_chunks.id` is path-dependent; the content hash survives re-index (OPS-2). A re-uploaded UNCHANGED document re-mints chunk ids but the same content → the same hash → the same `problem_code` → the `ON CONFLICT DO NOTHING` no-ops.
- **Duplicate handling:** UPSERT via `sqlalchemy.dialects.postgresql.insert(...).on_conflict_do_nothing(index_elements=["concept_id", "problem_code"])`. A second `write_tier1_problems` on the same doc inserts **ZERO** new rows. The local Testcontainers DB applies 030 (and 018), so the test exercises the REAL `(concept_id, problem_code)` constraint. NOTE: `on_conflict_do_nothing` is Postgres-only; the real-PG `db_session` fixture is correct for these tests (SQLite is not used for the upsert tests, consistent with 3B2c).
- **Partial-progress recovery:** scrape is stateless per chunk; a crash mid-document leaves only the already-committed Tier-1 rows. The next invocation re-hashes every chunk and the `ON CONFLICT` skips the ones already written — so a re-run is a pure top-up, never a double-insert and never a string-append/counter-inflate.
- **Mutation-discriminating test (REQUIRED):** reverting `on_conflict_do_nothing` → a plain `insert` makes the re-run RAISE a unique-violation (or, if the test removes the unique reliance, duplicate); either way `test_scrape_rerun_is_noop` REDs. This proves the guard is load-bearing.

**Stage 4 (tag_and_mint):**

- **Idempotency key:** entity upsert keyed on `(concept_id BIGINT, canonical_key)` (the seed-script `_upsert_entity` pattern); prereq edges on `(from_entity_id, to_entity_id)` ON CONFLICT skip; concept authoring is first-writer-wins UNION on `canonical_symbols`/`normalization_map` keys.
- **Duplicate handling:** SKIP/UNION, never overwrite. A second `tag_and_mint` for the same approved problem re-resolves to the same BIGINT concept, the same entity ids, and unions an already-present symbol set → no new symbols. Misconception entities upsert on their `canon.misc.*` key.
- **Concept symbol authoring is path-independent:** the symbol set is derived from the approved problem's `given_values` keys + `target_unknown` + equation symbols (deterministic), so re-running yields the same authored map; later DIFFERENT problems UNION new symbols in (first-writer owns existing keys, never rewrites them — the §8B.5 "a later material may add problems but not rewrite canonical symbols" rule).
- **NAMESPACE (ADJ #6):** scrape mints a string slug for the concept; `tag_and_mint` resolves slug → BIGINT `apollo_concepts.id` (the `_resolve_concept`-style lookup, creating the concept row if absent) and keys ALL persistence on the BIGINT, never the slug. The seam below resolves how a Tier-1 scrape row gets its `concept_id`.

**SEAM resolution — how a Tier-1 scrape row gets its NOT-NULL `concept_id` (ADJ #8 / proposal seam #8):**
`apollo_concept_problems.concept_id` is NOT NULL BIGINT (018:79), but scrape is stage-1 and concept tagging is stage-4. Confirmed against §8B.2: stage 1 writes Tier-1 rows scoped by `search_space_id`; the concept inventory is the union of stage-4 tags. **Resolution (lightweight provisional concept at scrape):** `write_tier1_problems` resolves a single **provisional per-document inventory concept** for the course — slug `provisional.inventory` (a reserved slug), created once per `(subject, search_space)` if absent via the `_resolve_or_create_concept` helper — and assigns its BIGINT to the Tier-1 rows. Stage-4 `tag_and_mint` later RE-HOMES the promoted problem onto its real tagged concept (updates `concept_id` to the resolved real concept's BIGINT at mint). This keeps the NOT-NULL satisfied at scrape WITHOUT pretending a real concept tag exists pre-tag, and the provisional concept is never teachable (its rows are tier=1). The provisional concept carries empty `canonical_symbols` and is excluded from selection because only tier-2 rows are returned. This is the minimum-surface resolution; the alternative (defer the Tier-1 write to apollo_concept_problems until tagged) is REJECTED because §8B.2:1267 explicitly writes Tier-1 at scrape, before tagging.

## Model & cost declaration

This unit makes LLM/embedding calls ONLY through INJECTED callables. In Tier-1 tests they are MOCKED (zero tokens, zero network). The production wiring (3B2f's metered client / 3B2g) supplies the real `cheap_chat` + `embed_text`; per-document metering + the cost circuit-breaker is 3B2f's responsibility, NOT this unit's. The routing this unit assumes (ADJ #7):

| Call (this unit) | Model | Input size est. | Output size est. | Unit cost | Volume est. | Total est. |
|------------------|-------|-----------------|------------------|-----------|-------------|------------|
| Scrape pass | `cheap_chat` (gpt-4o-mini via `APOLLO_CHEAP_MODEL`) | ~1.5k tok/chunk | ~400 tok | $0.15 / $0.60 per 1M in/out | ~40 chunks/doc | ~$0.0094/doc |
| Concept-tag draft | `cheap_chat` (gpt-4o-mini) | ~1k tok | ~200 tok | $0.15 / $0.60 per 1M | 1/problem | ~$0.00027/problem |
| Prereq-edge draft | `cheap_chat` (gpt-4o-mini) | ~1k tok | ~200 tok | $0.15 / $0.60 per 1M | 1/problem | ~$0.00027/problem |
| scope_summary embed | `embed_text` (text-embedding-3-large, 3072d) | ~80 tok/entity | — | $0.13 per 1M | ~6 entities/problem | ~$0.0001/problem |

**Per-document projected cost (scrape + ~10 promoted problems' tag/mint):** ~$0.014/document.
**CLAUDE.md / project model defaults honored:** scrape + tags use the Apollo `cheap_chat` tier (gpt-4o-mini); embeddings use the repo-standard `text-embedding-3-large` @ 3072d (`indexing.document_embedder.embed_text`). NO model deviation — this unit does not introduce any new model, and does not call `main_chat` (find-or-generate's `main_chat` use is 3B2e). NOTE: the workspace CLAUDE.md names `text-embedding-3-large`/3072 + GPT-4o as the stack defaults; the ai-pipeline planner's generic `text-embedding-3-small`@512 default does NOT apply to this repo and is overridden by the project CLAUDE.md (which is authoritative).
**Budget ceiling (ADJ #7):** `PER_DOCUMENT_TOKEN_CEILING = 2_000_000` is a runaway circuit-breaker enforced in 3B2f, not here. The real cost control is the `APOLLO_AUTOPROVISION_ENABLED` flag (default OFF).

## Failure paths

External calls in this unit = the injected `chat_fn` (scrape pass, concept-tag, prereq-draft) and `embed_fn` (via `resolve_candidate`). Retry/lease/DLQ are 3B2f/3B2g; this unit's contract is to FAIL CLEANLY so the orchestrator can record + retry.

1. **Retry policy:** NONE inside scrape/tag_mint. The functions are pure given their injected callables; the queue-drain (3B2f) owns backoff/lease/attempt_count. A transient OpenAI error propagates as the underlying exception to the caller's `try` (3B2g maps it to an `apollo_ingest_errors` row + a `failed` run).
2. **Fallback behavior:**
   - **Scrape, per-chunk:** a malformed/empty LLM JSON for one chunk → that chunk yields ZERO `CandidateQuestion`s (fail-SOFT per chunk; the document's other chunks still scrape). Mirrors the `leakage_judge` soft-parse pattern but DROPS rather than fabricates. NEVER emits a half-parsed candidate. The dropped chunk is reported in the return (a `scraped_count` + an optional `parse_failures` count) so 3B2g can log it.
   - **tag_and_mint:** a hallucinated/unmappable LLM concept tag or an `opposes_entity_key` that resolves to no entity → raise a NAMED error (`TagMintError`, mirroring `SeedError`'s NO-FALLBACK convention). FAIL-CLOSED: a mislinked entity silently corrupts grading for every student, so minting MUST refuse rather than guess. The caller marks the run failed; no partial mint is committed (the caller owns the transaction).
3. **DLQ / error table:** failures surface to the caller; 3B2g writes `apollo_ingest_errors(stage='scrape'|'tag_mint', error_class, context)` and marks the `apollo_ingest_runs` row `failed`. This unit does NOT write those tables itself (it has no `ingest_run_id` — that is the orchestrator's; out of scope, consistent with 3B2c which also leaves the run-counter to 3B2g).
4. **Observability:** scrape returns a structured result `{candidates, scraped_count, parse_failures}`; `tag_and_mint` returns a `MintPlan` enumerating every minted/merged entity + the dedup verdicts (so 3B2g can log `event=tag_mint` with merge counts and the orchestrator's calibration checklist can audit them). The injected `cheap_chat` already emits its own per-call `event=llm_call` audit line (`_llm.py:_log_call`). No alert threshold is owned here (3B2g/3B2h own the run-level alerting).

## Security check

- **API keys from env only.** This unit calls NO LLM/embedding client directly — it receives injected `chat_fn`/`embed_fn`. The production `cheap_chat`/`embed_text` read `OPENAI_API_KEY` from env via the OpenAI SDK (`_llm.py:_client`, `document_embedder.py:_get_client`). No key is ever in a function arg, a DB row, or a log.
- **No secrets in code/config/rows/logs.** Nothing in `scrape.py`/`tag_mint.py` references a secret. The Tier-1 tests inject deterministic stubs, never a real client.
- **Service role / client-reachability:** this is server-side FastAPI pipeline code, never client-reachable. No new endpoint is added.
- **No PII in embedding inputs.** The only text embedded is `KGEntity.scope_summary` (authored from `display_name` + canonical symbols — academic concept text, NOT student PII). Scraped question text is course material, not learner data. Retention follows the existing `apollo_*` course-scoped CASCADE-on-search-space-delete policy (migration 030 FKs); no new retention surface.
- **No tokens/PII in logs.** The scrape/mint structured logs carry counts + ids + concept slugs only — no raw chunk text, no LLM raw response, no key. `_log_call` already logs only token COUNTS, not content.
- **Course isolation (§1.4):** every write is `search_space_id`/`concept_id`-scoped; `resolve_candidate` enforces the course-scope WHERE before any cosine. This unit never reads or writes across courses.

## Public signatures (this unit defines)

```python
# apollo/provisioning/scrape.py
from pydantic import BaseModel, Field

class CandidateQuestion(BaseModel):
    """One scraped question, pre-Tier-1-write. The scrape output type (3B2e
    consumes a compatible shape). Provenance keyed on chunk_content_hash."""
    problem_text: str = Field(min_length=1)
    given_values: dict[str, float]
    target_unknown: str = Field(min_length=1)
    difficulty: str  # 'intro' | 'standard' | 'hard' (LLM-assigned; validated against Problem.Difficulty)
    document_id: int
    page: int | None = None
    chunk_content_hash: str = Field(min_length=1)   # sha256 of normalized chunk content (idempotency key)
    concept_slug: str = Field(min_length=1)         # LLM's lightweight concept hint (string slug; resolved to BIGINT at write)

async def scrape_questions(
    chunks: Sequence["AITAChunk"],
    *,
    chat_fn: Callable[..., str],   # cheap_chat-shaped; returns str (JSON). MOCKED in Tier-1.
) -> "ScrapeResult": ...
# ScrapeResult: pydantic/dataclass {candidates: list[CandidateQuestion], scraped_count: int, parse_failures: int}

async def write_tier1_problems(
    db: AsyncSession,
    candidates: Sequence[CandidateQuestion],
    *,
    concept_id: int,            # BIGINT (the provisional-inventory concept resolved by the caller/helper)
    search_space_id: int,
) -> int: ...                   # number of rows actually inserted (0 on a full re-run)
```

```python
# apollo/provisioning/tag_mint.py
from pydantic import BaseModel

class ApprovedPair(BaseModel):
    """The 3B2e output shape tag_and_mint consumes (3B2e produces a compatible
    value LATER; tested here with a hand-built fixture). An approved
    (question, reference_solution) pair plus the resolved scope."""
    problem: dict          # a full §schemas.problem.Problem-validatable dict (id, concept_id slug, difficulty,
                           #   problem_text, given_values, target_unknown, reference_solution[...])
    search_space_id: int
    solution_source: str   # 'extracted' | 'generated' (carried for the Tier-2 payload; mint does not re-derive it)
    misconceptions: list[dict] = []   # optional misconceptions.json-shaped entries (key/opposes/trigger_phrases)

class MintPlan(BaseModel):
    """Typed result enumerating everything tag_and_mint did (observability +
    the 3B2g handoff). NO promotion here — 3B2g runs the lint over this."""
    concept_id: int                       # resolved BIGINT
    concept_slug: str
    authored_symbols: list[str]           # canonical_symbols authored/unioned this call
    minted_entity_ids: dict[str, int]     # canonical_key -> BIGINT entity id
    merged_entity_keys: list[str]         # keys resolve_candidate merged onto existing entities
    prereq_pairs: list[tuple[str, str]]   # (from_key, to_key) drafted + inserted
    misconception_keys: list[str]         # kind='misconception' entities minted (the DEVIATION storage)

async def tag_and_mint(
    db: AsyncSession,
    pair: ApprovedPair,
    *,
    chat_fn: Callable[..., str],                       # cheap_chat-shaped (concept tag + prereq draft). MOCKED.
    embed_fn: Callable[[str], Sequence[float]],        # embed_text-shaped (scope_summary embed via resolve_candidate). MOCKED.
) -> MintPlan: ...
```

**Backward-compat:** the ONLY edit to an existing public surface is the additive dict key in `_ENTRY_TYPE_TO_KIND_PREFIX`. `_entity_key_for_step` / `reference_solution_to_entities` signatures are UNCHANGED; they simply stop raising `KeyError` for `variable_mapping`. No existing caller's behavior changes (no seeded problem uses `variable_mapping` — the WU-6A2 `reference_entity_keys` golden vectors are byte-identical). `problem_selector.list_problems_for_concept` is NOT touched (the tier-2 predicate is already 3B2a's).

## Files to create / edit

**CREATE:**
- `apollo/provisioning/scrape.py` (NEW) — `CandidateQuestion` + `ScrapeResult` + `scrape_questions` + `write_tier1_problems` + the `_resolve_or_create_provisional_concept` helper + the local `_chunk_content_hash`/`_normalize` helpers. Target < 250 lines.
- `apollo/provisioning/tag_mint.py` (NEW) — `ApprovedPair` + `MintPlan` + `TagMintError` + `tag_and_mint` + the PARALLELED persistence helpers (`_resolve_or_create_concept`, `_author_concept_symbols`, `_upsert_entity_resolved`, `_insert_prereqs`, `_link_opposes`) generalized from the seed script. Target < 350 lines.
- `apollo/provisioning/tests/test_scrape.py` (NEW) — Tier-1 mocked-LLM scrape + real-PG write/idempotency/safety-trap tests.
- `apollo/provisioning/tests/test_tag_mint.py` (NEW) — Tier-1 mocked-LLM tag/mint + real-PG author/dedup/misconception/variable_mapping tests.

**EDIT:**
- `apollo/persistence/learner_model_seed.py` (FLAGGED frozen-module EDIT, additive only) — extend `_ENTRY_TYPE_TO_KIND_PREFIX` (:80-86) with `"variable_mapping": ("variable", "varmap")`.
- `docs/architecture/apollo.md` (owner doc) — register `scrape`/`tag_mint` in the `apollo/provisioning/` module-map row (line 39) + add two public-API bullets near line 78; note the frozen-map extension in the `learner_model_seed.py` row (line 46); document the misconception-storage DEVIATION; set `last_verified: 2026-06-19`.

**DO NOT TOUCH (frozen / other units):** `apollo/provisioning/promotion_lint.py`, `dedup.py`, `problem_hash.py` (read/import only); `apollo/overseer/problem_selector.py`; `scripts/seed_apollo_learner_model.py`; `database/models.py`; `apollo/agent/_llm.py`; `indexing/**`; any migration file (030 is already applied to the local DB via 3B2a; see Risks for the one conflict-index dependency). NEVER re-index, NEVER apply a migration to a remote DB.

## Step-by-step changes (TDD-ordered)

RED-first throughout. Every step writes the failing test(s) BEFORE the implementation. No skip/xfail; real-PG tests run GREEN-not-skipped via `db_session` (Docker-skip cleanly only when the daemon is down, but the WU gate REQUIRES them green like 3B2c).

- [ ] **Step 0 — Non-regression baseline (capture BEFORE any edit).**
  - Run `.venv/Scripts/python.exe -m pytest apollo/learner_model/tests/test_personalization_select.py -q` and confirm ALL GREEN. This is the load-bearing WU-6A2 anchor; the frozen-map extension must not move it.
  - Verify: exit 0, note the pass count.

- [ ] **Step 1 — Frozen-map extension (RED via a new mint test, then the one-line edit).**
  - Test first (in `test_tag_mint.py`): `test_variable_mapping_entry_type_mints` builds a reference step with `entry_type="variable_mapping"` and asserts `reference_solution_to_entities` returns an EntitySpec with `kind=="variable"` and `canonical_key` starting `"varmap."` (currently RED — `KeyError`).
  - Edit: `apollo/persistence/learner_model_seed.py:80-86` add `"variable_mapping": ("variable", "varmap"),`.
  - Re-run Step 0's anchor: STILL all green (byte-identity of `reference_entity_keys`).
  - Verify: `pytest apollo/provisioning/tests/test_tag_mint.py::test_variable_mapping_entry_type_mints apollo/learner_model/tests/test_personalization_select.py -q` → green.

- [ ] **Step 2 — Scrape pure layer (RED).** Write `test_scrape.py` pure tests: the `_chunk_content_hash` normalization (whitespace/case-insensitive, sha256 hex), `scrape_questions` parses a MOCKED `chat_fn` JSON into `CandidateQuestion`s, and a malformed-JSON chunk yields zero candidates + increments `parse_failures` (fail-soft). Then implement `CandidateQuestion`, `ScrapeResult`, `_normalize`, `_chunk_content_hash`, `scrape_questions`.
  - `chat_fn` is stubbed (a closure returning `json.dumps([...])`, per chunk; or a `patch` of the injected name). NO network.
  - Verify: those tests green.

- [ ] **Step 3 — Tier-1 write + the SAFETY TRAP (real-PG, RED).** Write the real-PG tests using a `_seed_course` helper copied from `test_dedup.py:119-149` (SearchSpace→Subject→Concept). Implement `_resolve_or_create_provisional_concept` + `write_tier1_problems` with the `on_conflict_do_nothing` UPSERT on `(search_space_id, document_id, chunk_content_hash)`, inserting `tier=1` EXPLICITLY, `provenance={document_id,page,chunk_content_hash}`, denormalized `search_space_id`.
  - Tests: `test_scrape_writes_tier1_rows_explicit` (asserts `row.tier == 1` and `provenance['chunk_content_hash']` set); `test_tier1_row_excluded_by_selector` (the safety trap — `list_problems_for_concept` returns it ONLY after flipping tier=2); `test_scrape_rerun_is_noop` (second `write_tier1_problems` inserts 0); `test_scrape_omitting_tier_would_be_teachable` is COVERED by the explicit-tier assertion + the selector-exclusion test (mutation: drop the explicit `tier=1` → selector test REDs because the ORM default=2 makes it teachable).
  - Verify: real-PG tests green.

- [ ] **Step 4 — Concept resolution + symbol authoring (real-PG, RED).** Write `test_tag_mint.py` real-PG tests for `_resolve_or_create_concept` (slug→BIGINT, creates if absent) and `_author_concept_symbols` (first-writer-wins UNION: author from the approved problem's `given_values` keys + `target_unknown` + equation free-symbols; a second DIFFERENT problem UNIONs new symbols and does NOT rewrite existing ones). Implement them.
  - Verify: green.

- [ ] **Step 5 — Mint via frozen converters + dedup (real-PG, RED).** Write tests that `tag_and_mint` calls the TWO reused frozen converters `reference_solution_to_entities` + `misconceptions_to_entities` (asserted by the rows they produce, not by mock-spy) — NOT `concept_dag_to_prereqs` (prereqs are LLM-drafted from the tag) and NOT `annotate_reference_solution` (a promotion-time step owned by 3B2g) — resolves each entity candidate through `resolve_candidate` (injected `embed_fn`/`judge_fn` stubs), upserts on `(concept_id, canonical_key)`, and returns a populated `MintPlan`. Implement `tag_and_mint` + `_upsert_entity_resolved` + `_insert_prereqs` + `_link_opposes`.
  - The candidate adapter: each minted EntitySpec → a `{canonical_key, scope_summary}` duck-typed object (`scope_summary` authored from `display_name` + symbols) passed to `resolve_candidate`; a `merged` verdict reuses the matched entity id instead of inserting.
  - Verify: green.

- [ ] **Step 6 — Gate-4 non-vacuity + misconception DEVIATION + variable_mapping gate (real-PG, RED).**
  - `test_tag_and_mint_authors_canonical_symbols` — after mint, reload the concept; assert `canonical_symbols` and `normalization_map` are NON-EMPTY (so a 3B2b gate-4 over it is non-vacuous).
  - `test_minted_misconception_is_kg_entity` — assert a `kind='misconception'` row exists in `apollo_kg_entities` carrying `payload['opposes_entity_key']` (the DEVIATION storage; NOT `apollo_misconceptions`).
  - `test_variable_mapping_passes_gate1_mintmap_subcheck` — build a Problem with a `variable_mapping` step, run `run_promotion_lint` over it with the authored symbols; assert gate-1's mint-map membership sub-check PASSES now (it would have failed CLOSED before Step 1's map extension).
  - Verify: green.

- [ ] **Step 7 — Owner-doc reconcile.** Update `docs/architecture/apollo.md` (module-map row 39 + two public-API bullets + the `learner_model_seed.py` row 46 frozen-map note + the misconception DEVIATION note); `last_verified: 2026-06-19`.
  - Verify: `grep -n "scrape_questions\|tag_and_mint\|varmap\|kind='misconception'" docs/architecture/apollo.md` returns the new lines.

- [ ] **Step 8 — Coverage gate + non-regression.**
  - `.venv/Scripts/python.exe -m pytest apollo/provisioning/tests/test_scrape.py apollo/provisioning/tests/test_tag_mint.py apollo/learner_model/tests/test_personalization_select.py -q` → all green.
  - `.venv/Scripts/python.exe -m pytest --cov=. --cov-report=xml` then `diff-cover coverage.xml --compare-branch=feat/apollo-kg-wu3b2c-dedup-ladder --fail-under=95`.

## Test list (write FIRST)

All LLM/embed deps MOCKED deterministically (`patch(target, return_value=json.dumps(...))` per `test_leakage_judge.py:31-36`, or a closure stub per `test_dedup.py`'s `_judge_merged`). Real-PG tests request `db_session` and must run GREEN-not-skipped.

**`apollo/provisioning/tests/test_scrape.py`**

Pure (no DB):
1. `test_chunk_content_hash_is_normalized` — two chunks differing only in whitespace/case produce the SAME hash; different content → different hash. Asserts the idempotency key is content-stable (survives re-index). Mock: none.
2. `test_chunk_content_hash_is_sha256_hex` — output is 64 lowercase hex chars. Mock: none.
3. `test_scrape_parses_candidates` — `chat_fn` returns a well-formed JSON array; assert `ScrapeResult.candidates` has the right `problem_text`/`given_values`/`target_unknown`/`difficulty`/`concept_slug`, and `chunk_content_hash`/`document_id`/`page` come from the chunk (NOT from the LLM). Mock: `chat_fn` closure returning `json.dumps([...])`.
4. `test_scrape_malformed_json_is_failsoft` — one chunk's `chat_fn` returns non-JSON; assert that chunk yields ZERO candidates, `parse_failures == 1`, and the OTHER chunks still scrape. Asserts no half-parsed row. Mock: per-chunk stub, one returns `"not json"`.
5. `test_scrape_difficulty_validated` — an LLM `difficulty` outside `{intro,standard,hard}` for one chunk drops that candidate (counted in `parse_failures`), never writes an invalid Tier-1 row. Mock: stub returning a bad difficulty.

Real-PG (`db_session`):
6. `test_scrape_writes_tier1_rows_explicit` — after `write_tier1_problems`, reload rows: assert `tier == 1` EXPLICIT, `provenance['chunk_content_hash']` == the candidate's hash, `provenance['document_id']`/`page` set, `search_space_id` denormalized. Mock: candidates built directly (no LLM). **DISCRIMINATING:** drop the explicit `tier=1` in impl → this + test 8 RED (ORM default=2).
7. `test_provisional_concept_resolved_and_notnull` — `concept_id` is a real BIGINT (the provisional-inventory concept), NOT NULL; re-calling resolves the SAME provisional concept id (created once). Mock: none.
8. `test_tier1_row_excluded_by_selector` — **THE SAFETY TRAP.** `list_problems_for_concept(db, concept_id=<provisional>)` returns `[]` for the scraped Tier-1 row; after flipping that row to `tier=2` it IS returned. Proves un-linted scraped problems are never teachable. Mock: none.
9. `test_scrape_rerun_is_noop` — **IDEMPOTENCY.** Call `write_tier1_problems` twice with the same candidates; the second returns 0 inserted and the row count is unchanged. **MUTATION-DISCRIMINATING:** reverting `on_conflict_do_nothing` → a plain insert makes the second call duplicate rows; this test REDs. Mock: none.
10. `test_scrape_rerun_after_reindex_is_noop` — re-build candidates with DIFFERENT `document_id`-chunk ids but identical CONTENT (same `chunk_content_hash`); second write still no-ops. Proves the content-hash key survives re-index (OPS-2). Mock: none.

**`apollo/provisioning/tests/test_tag_mint.py`**

Pure (no DB):
11. `test_variable_mapping_entry_type_mints` — (Step 1 RED→green) `reference_solution_to_entities` on a `variable_mapping` step → `kind=="variable"`, key `"varmap.<id>"`, no `KeyError`. Mock: none.
12. `test_approvedpair_and_mintplan_shapes` — Pydantic round-trip of `ApprovedPair`/`MintPlan`; required fields enforced. Mock: none.
13. `test_author_symbols_from_problem` — `_author_concept_symbols` derives the symbol set from `given_values` keys + `target_unknown` + equation free-symbols; result is non-empty + deterministic. Mock: none.

Real-PG (`db_session`):
14. `test_resolve_or_create_concept_slug_to_bigint` — slug → BIGINT; creates the concept if absent; re-resolves to the same id (idempotent). Asserts the §6 namespace contract (key on BIGINT, never slug). Mock: none.
15. `test_tag_and_mint_authors_canonical_symbols` — **GATE-4 NON-VACUITY.** After mint, reload the concept; assert `canonical_symbols` AND `normalization_map` are NON-EMPTY so a 3B2b gate-4 over it can actually fire. Mock: `chat_fn` returns a concept-tag + prereq draft JSON; `embed_fn`/`judge_fn` deterministic stubs.
16. `test_author_symbols_first_writer_wins_union` — a SECOND `tag_and_mint` with a different problem UNIONs new symbols and does NOT rewrite existing canonical symbols (the §8B.5 first-writer rule). Mock: as above, two pairs.
17. `test_tag_and_mint_mints_reference_entities` — the reference steps become `apollo_kg_entities` rows with the frozen converter's keys/kinds (eq./cond./proc./varmap.), reachable from the returned `MintPlan.minted_entity_ids`. Mock: as above.
18. `test_minted_misconception_is_kg_entity` — **THE DEVIATION.** An `apollo_kg_entities` row with `kind='misconception'` and `payload['opposes_entity_key']` exists (via `misconceptions_to_entities`); assert NO write to `apollo_misconceptions`. Mock: `ApprovedPair.misconceptions` fixture.
19. `test_tag_and_mint_dedups_via_resolve_candidate` — when a candidate entity's `scope_summary` matches an existing in-course entity (stub `embed_fn` → ≥0.92 cosine), `tag_and_mint` MERGES (reuses the existing id, no new row) and records it in `MintPlan.merged_entity_keys`. Mock: deterministic `embed_fn` mapping (mirrors `test_dedup.py`'s `_unit_at_cosine`).
20. `test_tag_and_mint_prereqs_inserted` — drafted prereq pairs land in `apollo_entity_prereqs` (ON CONFLICT skip on re-run). Mock: `chat_fn` prereq-draft JSON.
21. `test_tag_and_mint_idempotent` — running the same `ApprovedPair` twice inserts no new entities/prereqs and unions no new symbols. Mock: as above.
22. `test_tag_and_mint_unmappable_tag_raises` — a hallucinated `opposes_entity_key` resolving to no entity → `TagMintError`, FAIL-CLOSED, no partial commit visible after rollback. Mock: `chat_fn`/fixture with a bad opposes key.
23. `test_variable_mapping_passes_gate1_mintmap_subcheck` — build a `Problem` with a `variable_mapping` step + author its symbols, run `run_promotion_lint`; assert gate-1's mint-map sub-check PASSES (pre-Step-1 it failed CLOSED). Ties the frozen-map extension to its 3B2b consumer. Mock: none (lint is pure).

**Non-regression anchor (run explicitly, NOT a new test file):**
24. `apollo/learner_model/tests/test_personalization_select.py` — ALL GREEN before AND after the frozen-map edit (the WU-6A2 `reference_entity_keys` reconstruction-parity golden vectors; byte-identity).

## Owner-doc updates

`docs/architecture/apollo.md` is the owner of `apollo/**`. Reconcile in the SAME commit as the code; set `last_verified: 2026-06-19` (already 2026-06-19 in frontmatter — keep it; if it had drifted, set it).

1. **Module-map row 39** (`apollo/provisioning/`): append `scrape.py`, `tag_mint.py` to the file list, and a paragraph registering **WU-3B2d** — scrape (`scrape_questions` → Tier-1 `apollo_concept_problems` keyed on `chunk_content_hash`, `tier=1` EXPLICIT, the provisional-inventory concept seam) and tag/mint (`tag_and_mint` authors `apollo_concepts.canonical_symbols`/`normalization_map` first-writer-wins, reuses the frozen seed converters, dedups via `resolve_candidate`, mints reference entities + the `kind='misconception'` DEVIATION). State the injected `chat_fn`/`embed_fn` (Tier-1 network-free) and that scrape READS `AITAChunk` and NEVER re-indexes.
2. **Two public-API bullets near line 78** (after the `resolve_candidate` bullet): `apollo.provisioning.scrape_questions(...) -> ScrapeResult` and `apollo.provisioning.tag_and_mint(...) -> MintPlan` with their signatures + the §1.4 course-scoping + the idempotency keys.
3. **`learner_model_seed.py` row 46**: add a sentence that WU-3B2d extends `_ENTRY_TYPE_TO_KIND_PREFIX` with `variable_mapping → (variable, varmap)` (additive; the WU-6A2 `reference_entity_keys` parity is unaffected because no seeded problem uses it) and that 3B2b's gate-1 mint-map sub-check now ACCEPTS `variable_mapping`.
4. **Misconception-storage DEVIATION note** (in the provisioning paragraph): auto-minted misconceptions are stored as `apollo_kg_entities kind='misconception'` (a valid ENTITY_KIND) via the frozen `misconceptions_to_entities` converter — a DELIBERATE, orchestrator-SIGNED deviation from the literal §8B.2:1291 `apollo_misconceptions` table (migration-019's table needs NOT-NULL Socratic `probe_question`/`rt_steps` auto-provisioning v1 cannot responsibly author; the runtime `opposes_map` is structurally empty in v1, so the channel is dormant). Reference ADJ #2.

If the frozen-map edit's owner doc differs from `apollo.md` (it does NOT — `learner_model_seed.py` is under `apollo/**`, owned by apollo.md), reconcile that doc too. No other owner doc is touched (no `domain-data.md` edit — this unit does NOT touch `teacher_weekly.py`/`indexing`).

## Verification

Interpreter: `.venv/Scripts/python.exe` (a bare-`python` ImportError is interpreter selection, not a blocker).

- [ ] **Non-regression anchor (load-bearing):** `.venv/Scripts/python.exe -m pytest apollo/learner_model/tests/test_personalization_select.py -q` → ALL GREEN both before and after the frozen-map edit.
- [ ] **Unit + real-PG suite:** `.venv/Scripts/python.exe -m pytest apollo/provisioning/tests/test_scrape.py apollo/provisioning/tests/test_tag_mint.py -q` → all green (real-PG tests green-not-skipped; Docker up).
- [ ] **Manual smoke (the safety trap):** seed a course + scrape one chunk → `list_problems_for_concept` returns `[]` (Tier-1 excluded); flip tier=2 → it appears. Asserted by `test_tier1_row_excluded_by_selector`.
- [ ] **Idempotency replay:** `write_tier1_problems` twice → second returns 0 (`test_scrape_rerun_is_noop`); `tag_and_mint` twice → no new entities/symbols (`test_tag_and_mint_idempotent`).
- [ ] **Dry-run cost calculation:** scrape ≈ `n_chunks × (1.5k×$0.15 + 0.4k×$0.60)/1e6`; tag/mint ≈ `n_problems × ~$0.0004`. For a 40-chunk doc with 10 promoted problems ≈ $0.014/doc. (No live tokens — Tier-1 is fully mocked.)
- [ ] **DLQ test (this unit's slice):** `test_scrape_malformed_json_is_failsoft` (bad chunk drops, others survive, `parse_failures` counted) + `test_tag_and_mint_unmappable_tag_raises` (fail-closed `TagMintError`). The `apollo_ingest_errors` WRITE is 3B2g's — out of scope; this unit proves it FAILS CLEANLY for the orchestrator to record.
- [ ] **Backpressure test:** N/A in this unit — rate-limit/batching/cost-ceiling backpressure is 3B2f's metered client + queue-drain. This unit's scrape is a per-chunk loop with no fan-out; documented as out-of-scope, not silently skipped.
- [ ] **Mutation discipline (independent):** (a) revert the `variable_mapping` map key → `test_variable_mapping_*` RED, 6A2 anchor stays green; (b) revert `on_conflict_do_nothing` → `test_scrape_rerun_is_noop` RED; (c) drop the explicit `tier=1` → `test_tier1_row_excluded_by_selector` RED (ORM default=2 leaks).
- [ ] **Coverage gate:** `.venv/Scripts/python.exe -m pytest --cov=. --cov-report=xml` then `diff-cover coverage.xml --compare-branch=feat/apollo-kg-wu3b2c-dedup-ladder --fail-under=95` → ≥ 95% on changed lines.

## Out-of-scope boundaries (this unit)

- **NO find-or-generate / pairing gate (stage 2 + 3 = 3B2e).** `tag_and_mint` consumes an ALREADY-approved `ApprovedPair` (hand-built fixture here). This unit does NOT retrieve, generate, or validate solutions.
- **NO promotion / lint / `:Canon` (stage 6 = 3B2b's logic, 3B2g's wiring).** `tag_and_mint` returns a `MintPlan`; it does NOT call `run_promotion_lint`, does NOT flip tier=2, does NOT call `project_canon`, does NOT write `apollo_rejected_problems`. (Test 23 CALLS `run_promotion_lint` only to PROVE the gate-1 mint-map sub-check passes for `variable_mapping` — it does not wire promotion into the pipeline.)
- **NO queue/worker/trigger/metering (3B2f + 3B2g).** No `apollo_provisioning_jobs` claim/lease, no `apollo_ingest_runs` counters, no `apollo_ingest_errors` writes, no SIGTERM worker shell, no `teacher_weekly.py` enqueue hook, no `PER_DOCUMENT_TOKEN_CEILING` circuit-breaker, no metered LLM wrapper. The injected `chat_fn`/`embed_fn` are plain callables; 3B2f supplies the metered ones.
- **NO quarantine / shadow verification (3B2h).** No `quarantined_at` filter, no anomaly statistic.
- **NO migration.** 030 is already applied by 3B2a (ORM present on this branch). This unit writes through the existing ORM only and uses the EXISTING `UNIQUE (concept_id, problem_code)` (018) as the idempotency conflict target — NO new index, NO migration edit. (Earlier drafts assumed a `(search_space_id, document_id, chunk_content_hash)` unique index; verified it does NOT exist, so the writer derives `problem_code` from the hash instead — see Idempotency.)
- **NO re-indexing.** Reads `database.models.AITAChunk` only.
- **NO new package** (ADJ #8). Uses stdlib `hashlib`/`re`/`json`, pydantic, sqlalchemy, and the existing converters/`resolve_candidate`. If implementation tempts a new dep → BLOCK + escalate.
- **NO `problem_selector.py` edit** — the tier-2 predicate is already 3B2a's; this unit only RELIES on it for the safety-trap test.

## Risks

Confidence-rated.

- **[HIGH confidence / LOW residual risk] The Tier-1 safety trap.** The ORM `tier` default is 2; a missed explicit `tier=1` silently makes scraped inventory teachable. Mitigated by `test_tier1_row_excluded_by_selector` + the explicit-tier assertion + the mutation that REDs if the explicit value is dropped. This is the single highest-blast-radius bug in the unit.
- **[HIGH confidence] Idempotency conflict target.** RESOLVED against the real schema: no `(search_space_id, document_id, chunk_content_hash)` unique index exists; the writer uses the existing `UNIQUE (concept_id, problem_code)` with a hash-derived `problem_code`. Risk is now low — the test runs against the real constraint on the local 030+018 DB. Residual: if a FUTURE unit wants the literal triple-index, that is a 3B2a/3B2g migration concern, NOT this unit's (flagged for the orchestrator).
- **[MEDIUM] The provisional-inventory concept seam.** The §8B.2 stage-1-before-stage-4 ordering forces a placeholder `concept_id` for Tier-1 rows. The plan resolves it with a reserved `provisional.inventory` per-course concept and a stage-4 re-home. If the orchestrator prefers the alternative (defer the Tier-1 write until tagged), that contradicts §8B.2:1267 and is REJECTED here — but it is the one genuine design call the orchestrator may want to confirm. Low correctness risk either way because provisional rows are tier=1 (never selected).
- **[MEDIUM] `ApprovedPair` shape stability.** 3B2e is not built; `tag_and_mint` is tested against a hand-built fixture. If 3B2e's eventual output shape diverges, 3B2g's wiring adapts it — `tag_and_mint`'s contract is pinned here (a `Problem`-validatable `problem` dict + scope + misconceptions). Risk contained because the runtime order is 3B2g's job, explicitly out of scope.
- **[MEDIUM] LLM-output schema (scrape + tag).** Real LLM JSON may not match the assumed schema. Mitigated by fail-soft per-chunk drop (scrape) + fail-closed `TagMintError` (tag). Real-LLM robustness is a Tier-2/Tier-3 (nightly/release) harness concern, scaffolded-not-run per ADJ #10 — not gated in this PR.
- **[LOW] Cost surprise.** Tier-1 is fully mocked (zero tokens). Production per-doc cost (~$0.014) is well under the 2M-token circuit-breaker; the real control is the flag-OFF default. The metered client that ENFORCES the ceiling is 3B2f, not here.
- **[LOW] External API availability.** No live API call in this unit (injected callables). Transient OpenAI failures in production propagate to 3B2f/3B2g's retry/lease — out of scope.
- **[LOW] Schema-lock during deploy.** No DDL in this unit → no lock. (030 was already deployed by 3B2a.)
- **[LOW] Frozen-map regression.** The additive `variable_mapping` key cannot change any seeded-problem behavior (none use it); the 6A2 anchor test (#24) proves byte-identity. Confidence very high.

## Deviations I'd allow the executor

- **`ScrapeResult` as a frozen dataclass instead of Pydantic** — fine; the public boundary that needs validation is `CandidateQuestion` (LLM-sourced) and `ApprovedPair`/`MintPlan`. `ScrapeResult` is an internal aggregate; either is acceptable. Keep `CandidateQuestion` Pydantic (it validates LLM output).
- **`problem_code` derivation format** — `f"scrape.{chunk_content_hash}"` vs `f"scrape.{document_id}.{chunk_content_hash[:32]}"` are both acceptable as long as it is (a) deterministic from content, (b) collision-safe within the concept, and (c) the `ON CONFLICT (concept_id, problem_code)` no-op holds. Do NOT include `aita_chunks.id` (breaks re-index idempotency).
- **Where `scope_summary` is authored for the dedup candidate** — composing it from `display_name` + the entity's canonical symbols (the ADJ #11 source) is the intent; the exact template string is the executor's call.
- **Splitting `tag_mint.py` if it nears 350 lines** — extract the persistence helpers into `apollo/provisioning/tag_mint_persist.py` (many-small-files rule). Acceptable as long as the public surface (`tag_and_mint`, `ApprovedPair`, `MintPlan`) stays in `tag_mint.py`.
- **The concept-tag / prereq-draft prompt wording** — executor's call; the TEST asserts behavior over a mocked response, not the prompt text.
- **NOT allowed without orchestrator sign-off:** any new package; any migration edit (local or remote); writing to `apollo_misconceptions` (the DEVIATION is `kind='misconception'` entities ONLY); calling `run_promotion_lint`/`project_canon`/flipping tier=2 in production code (3B2g); re-indexing or reading/writing across `search_space_id`; changing `_ENTRY_TYPE_TO_KIND_PREFIX` beyond the single additive `variable_mapping` key; touching `problem_selector.py` or `teacher_weekly.py`.
