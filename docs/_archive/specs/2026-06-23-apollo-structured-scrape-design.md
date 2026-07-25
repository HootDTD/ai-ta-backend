# Design: Structure-aware problem finding — Apollo auto-provisioning (Phase 2)

- **Date:** 2026-06-23
- **Branch:** `ApolloRun`
- **Status:** Approved (design); implementation plan to follow
- **Owner doc to update on implementation:** `docs/architecture/apollo.md`
- **Predecessor:** `docs/superpowers/specs/2026-06-22-apollo-autoprovision-phase1-design.md`
  (Phase 1 made the pipeline *run* end-to-end; this phase makes stage 1 *smart and
  efficient* and proves a real end-to-end promotion.)

## Context

Apollo's 6-stage auto-provisioning pipeline turns textbook content (`aita_chunks`)
into teachable problems with a machine-graded reference graph:

```
aita_chunks → [1] scrape → [2] find_or_generate → [3] validate_pair
            → [4] tag_and_mint → [5] promote (lint + project_canon → :Canon)
```

Stage 1 (`apollo/provisioning/scrape.py::scrape_questions`) makes **one LLM call
per chunk** (`scrape.py:146` loops `for chunk in chunks: chat_fn(chunk.content)`).
The orchestrator loads *every* chunk for the document (`orchestrator.py:_load_chunks`)
and scrapes them all.

### The measured problem (staging, project `hjevtxdtrkxjcaaexdxt`)

The two staging documents are large and chunked at **line/phrase granularity** for
retrieval — actively hostile to per-chunk scraping:

| Document | ss | chunks | avg chars/chunk | median chars | distinct sections | headings |
|---|---|---|---|---|---|---|
| fluidMechanics (doc 2) | 2 | 20,930 | 68 (~17 tok) | 17 | 244 | 256 |
| calculus-volume-2 (doc 5) | 3 | 12,918 | 67 | 31 | 60 | 66 |

Consequences of one-call-per-chunk on fluidMechanics:

- **~92% of spend is waste.** Every call re-sends the ~200-token scrape system
  prompt, then attaches a ~17-token fragment. The whole book is only ~355K tokens
  of real text but is shattered into 20,930 calls → ~4–6M input tokens, of which
  ~4.2M is the prompt re-sent 20,930×.
- **It does not finish.** Cumulative tokens cross the 2,000,000
  `PER_DOCUMENT_TOKEN_CEILING` (`cost_constants.py:27`) roughly half-way through →
  `CostBudgetExceeded` → the run **fails** (a cost-abort, not a clean promote).
  The ceiling is a runaway *circuit breaker* sized for normal-granularity docs; the
  per-chunk design self-inflicts the runaway.
- **Quality is hurt.** A real "given X, find Y" problem is scattered across many
  micro-chunks, so no single call ever sees a whole problem.
- **Latency:** ~21,000 sequential calls ≈ 1.5–3 hours wall-clock.

The chunks already carry structure the scrape ignores: `section_path` (heading
hierarchy, e.g. `"11.2 Entry Problem"`, `"12.8 Additional Example"`) and
`chunk_type ∈ {body, equation, heading}`. There is **no** explicit
problem/example/exercise chunk_type, and `section_path` is noisy (junk values like
`"1 3"` appear), so we cannot rely on metadata labels alone.

### Goal

Make stage-1 problem finding **smart and efficient**, and prove a **full E2E that
actually functions** — a real auto-found problem clears stages 2–4 and promotes to
Tier-2 with `:Canon` nodes written — on **calculus-volume-2 (doc 5, ss=3)**, chosen
because it is ~2.6× denser in imperative-numeric content (589 vs 229 "find/solve …
〈number〉" chunks) than the theory-heavy fluids book.

## Approach (chosen: C — Hybrid)

Three approaches were considered; all share the core fix of grouping micro-chunks
back into sections and scraping per-section. They differ in how the "finding"
targets:

- **A — Batched-exhaustive:** group by section, scrape every section once. Simple,
  robust, ~100× cheaper; not targeted.
- **B — TOC-targeted:** LLM reads only the section-title list, scrapes just the
  sections it flags. Cheapest/smartest when titles are clean — **fragile** on the
  junk `section_path` values we measured.
- **C — Hybrid (chosen):** a cheap TOC-triage pass ranks + concept-labels sections;
  Pass 2 batched-scrapes the high-likelihood sections; an exhaustive fallback widens
  the net if a course comes up too thin. Smart + efficient + robust on messy/
  theory-heavy books. Concept tags fall out of the triage for free.

C is chosen because the corpus reality is *messy metadata + uneven problem density*,
and C is the only option that still produces results (rather than silently finding
nothing) under those conditions while remaining efficient.

## Architecture

### Data flow

```
load chunks (now incl. id, section_path, chunk_type, page_number, content)
  → reconstruct sections           [new: section_grouping.py]
  → triage sections (Pass 1)        [new: section_triage.py] → ranked sections + concept/section
  → scrape prioritized sections     [Pass 2, whole-section]  → candidates
  → fallback: widen to remaining sections if candidate count < MIN  (bounded by MAX)
  → ScrapeResult ──► (UNCHANGED) write_tier1 → find_or_generate → validate_pair
                                   → tag_and_mint → promote → :Canon
```

The new entrypoint returns the **same `ScrapeResult`** shape
(`candidates, scraped_count, parse_failures`), so the orchestrator's per-candidate
and per-document decision logic and stages 2–5 are untouched. This is a drop-in
replacement at exactly one call site (`orchestrator.run_provisioning`).

### Components (small, independently testable units)

1. **`apollo/provisioning/section_grouping.py`** — pure, no LLM.
   - `group_into_sections(chunk_rows) -> list[Section]`.
   - Walks chunks in `id` order; a `chunk_type='heading'` chunk (or a change in
     `section_path`) opens a new section; `body`/`equation` chunks accumulate under
     the current section.
   - `Section{title, page_range, text, source_content_hash, member_chunk_ids}`.
   - `source_content_hash` = sha256 of the normalized concatenated section text
     (deterministic from the index → stable across re-index).
   - Degrade rules: a document with no heading chunks → a single whole-document
     "section 0"; a junk/empty `section_path` → used as an opaque label, never an
     error.

2. **`apollo/provisioning/section_triage.py`** — Pass 1, injected `chat_fn`.
   - `triage_sections(sections, *, chat_fn) -> list[SectionVerdict]`.
   - One cheap call over the section **title list** plus light per-section stats
     (page span, has-equation-chunks, count of imperative-numeric body hits).
   - `SectionVerdict{section, is_problem_likely, priority, concept_slug, concept_display}`.
   - **Fails open:** malformed/empty triage JSON → every section returned as
     `is_problem_likely=True` at equal priority (degrades to Approach A). Triage
     never aborts the run.

3. **`apollo/provisioning/scrape.py`** (extended; existing helpers preserved).
   - `scrape_section(section, *, concept_hint, chat_fn) -> list[CandidateQuestion]`
     — whole-section prompt; same fail-soft contract as today (bad JSON / invalid
     candidate drops that record and increments `parse_failures`).
   - `scrape_document(chunk_rows, *, chat_fn, triage_chat_fn, max_sections, min_candidates)
     -> ScrapeResult` — orchestrates reconstruct → triage → scrape (priority order)
     → bounded fallback. New stage-1 entrypoint.
   - `write_tier1_problems`, `chunk_content_hash`, `CandidateQuestion`, `ScrapeResult`
     stay.

4. **`apollo/provisioning/orchestrator.py`** (wiring only).
   - `_load_chunks` selects `id, section_path, chunk_type` in addition to
     `content, document_id, page_number`; `_ChunkView` gains those fields.
   - Swap the `scrape_questions(...)` call for `scrape_document(...)`.
   - `_SCRAPE_SYSTEM_PROMPT` updated for whole-section input; add
     `_TRIAGE_SYSTEM_PROMPT`.

5. **`apollo/provisioning/cost_constants.py`** — add
   `APOLLO_SCRAPE_MAX_SECTIONS` (hard per-document cap on sections scraped) and
   `APOLLO_SCRAPE_MIN_CANDIDATES` (fallback-widen trigger), env-overridable with
   committed defaults pinned by tests (mirrors the existing constants).

### Key decisions (approved)

- **Idempotency re-key.** Today `problem_code = scrape.<chunk_content_hash>`. New:
  `scrape.<section_content_hash>.<ordinal>` — the section text is deterministic from
  the index (stable across re-index); the ordinal disambiguates multiple problems
  from one section. To keep the ordinal stable across re-runs (the LLM may emit a
  section's problems in a different order), candidates within a section are sorted
  deterministically — by `chunk_content_hash` of the normalized `problem_text` —
  before the ordinal is assigned. **No migration:** the subsystem is dormant
  (`APOLLO_AUTOPROVISION_ENABLED` OFF + 0 replicas), so no real auto-provisioned
  rows exist to convert. Any stale Tier-1 rows from prior staging test runs are
  irrelevant (different `problem_code` namespace; the dedup guard simply re-inserts
  fresh).
- **Concept tag is a hint, not authority.** Triage's `concept_slug` seeds the
  candidate; stage-4 `tag_and_mint` remains the authoritative concept resolver. This
  is strictly better signal than today's fragment-level guess and changes no
  downstream contract.
- **Safety flag.** Gate the new path behind `APOLLO_STRUCTURED_SCRAPE` (default ON
  *within* the already-OFF subsystem) so the per-chunk path is an instant revert.

## Error handling

- No-heading document → one whole-document section (still batched).
- Junk/empty `section_path` → opaque label; grouping never raises.
- Triage LLM failure → fail-open to exhaustive (all sections priority).
- Per-section scrape → fail-soft (drop record, increment `parse_failures`, continue)
  — identical semantics to the current per-chunk path.
- `APOLLO_SCRAPE_MAX_SECTIONS` bounds total sections scraped; `MeteredChat`'s
  `PER_DOCUMENT_TOKEN_CEILING` remains the ultimate backstop. With batching the whole
  calculus book is ~60 section calls + 1 triage call ≈ $0.05–0.15, far under the 2M
  ceiling — so the real E2E is now **safe to run**, which also unblocks Phase-1 Task
  5 Step 5.

## Testing

- **Tier-1 (mocked LLM, deterministic — `asyncio_mode = auto`, injected
  `chat_fn`/`embed_fn`):**
  - `section_grouping`: headings → sections; missing/junk `section_path`; no-heading
    whole-doc degrade; `source_content_hash` stability.
  - `section_triage`: parse ranked verdicts; **fail-open** on malformed triage JSON.
  - `scrape_section`: whole-section extraction; fail-soft on bad JSON / invalid
    candidate.
  - `scrape_document`: fallback-widen trigger fires when candidates < MIN; respects
    `MAX_SECTIONS`; section-hash idempotency (re-run inserts 0 Tier-1 rows).
  - integration: `scrape_document → ScrapeResult` proves the orchestrator wiring and
    stages 2–5 are unchanged.
- **Real-LLM E2E (the deliverable):** worker drain (`_drain_one`) on calculus doc 5,
  bounded by the section cap → confirm `n_promoted ≥ 1`, `apollo_concept_problems.tier
  == 2` on the tagged concept, and `:Canon` count increases. Confirm any
  non-promotions are legitimate content/lint rejects (`apollo_rejected_problems`),
  **not** `TagMintError`/`KeyError` aborts (`apollo_ingest_errors`).

## Scope — what we are NOT doing (YAGNI)

- No PDF re-parsing — reuse the existing index metadata (`section_path`,
  `chunk_type`).
- No new chunk-type classifier.
- No *generating* problems from pure theory — we **extract**; the existing
  `find_or_generate` still drafts the reference solution for a found problem.
- No changes to stages 2–5, the promotion lint, or the `:Canon` projection contract.

## Constraints (inherited)

- Branch `ApolloRun`. NEVER push to `main`. Do NOT merge any PR (owner merges).
- No new packages without asking.
- Supabase: "staging" = `hjevtxdtrkxjcaaexdxt` (test). "Apollo" project = PROD —
  never write to prod.
- Keep structured-JSON-from-LLM + per-stage debug logging; never weaken the
  FAIL-CLOSED `TagMintError` convention.
- Update `docs/architecture/apollo.md` (owner doc) in the SAME commit as the code
  that affects it; bump its `last_verified`.
- Commit trailer: `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`.
- TDD: failing test first, watch it fail for the right reason, then implement.

## Open questions for the implementation plan

- Exact default values for `APOLLO_SCRAPE_MAX_SECTIONS` / `APOLLO_SCRAPE_MIN_CANDIDATES`
  (pin via tests; calculus has 60 sections, so a default cap of ~80–120 covers a full
  book with headroom).
- Whether triage batches the title list when section count is large (fluids: 244) —
  a single call is fine at 60–250 titles; revisit only if a future corpus has
  thousands of sections.
