# Workflow: how this test was run

This documents the **exact orchestration** used, so the experiment is
reproducible. Sections marked _(planned)_ are finalized after the live run.

## Orchestration phases

1. **Scout (parallel readers).** A 6-agent `Workflow` mapped the Apollo building
   blocks (subject/problem schema + seeders, reference & student KG construction,
   resolution, S_norm/R_norm grading, the RAG/relevance pathway, textbook
   embedding, and the probe harness) into structured maps. Outcome: the pipeline
   is ~85% subject-agnostic; fluid coupling is isolated to the learner-model
   seeder driver, the probe, and one antonym list.
2. **Design.** Textbook read for gradeable equations; four decisions taken with
   the user (see `DESIGN.md`); design approved.
3. **Build (parallel subagents).** A `Workflow` fanned out disjoint-file agents:
   - `content-gdp-components` — authors concept A tree + Q1–Q3 + misconceptions,
     self-verifies each problem via the pure validators.
   - `content-nominal-real` — authors concept B tree + Q4–Q5 (the case-3 trap) +
     misconceptions, self-verifies.
   - `seeder-generalize` — parameterizes `seed_apollo_learner_model.py` + tests.
   - `scripts-and-antonyms` — `index_local_pdf.py`, macro antonyms in
     `competition.py`, + tests.
   - `probe-and-orchestrator` (depends on content) — macro `SCENARIOS` (15
     variations), generalized `apollo_grade_probe.py`, `run_macro_probe.py`.
4. **Local run (agent-driven, sequential).** Commands below.
5. **Analysis.** Score matrix → `RESULTS.md`.

## Division of labor

| Agent-authored (code/data/tests) | Agent-run (local Docker, OpenAI) |
|---|---|
| macro subject tree, 5 problem JSONs | `index_local_pdf.py` (embed ch.6) |
| generalized seeder + probe + scripts | seed ×3 (registry → learner-model → canon) |
| macro antonyms | mining (`scrape_questions`) + faithfulness (`validate_pair`) |
| all unit tests | boot `:8001`, run probe (15 attempts), read scores |

## Local stack prerequisites (already up at run time)

- Supabase local: `127.0.0.1:54321` (Auth) / `:54322` (Postgres)
- Neo4j local: `bolt://127.0.0.1:7687` (`hoot-neo4j-local`)
- Backend venv: `ai-ta-backend/.venv/Scripts/python.exe` (has `asyncpg`)
- Env: `.env` (secrets) + `.env.local` (local URLs + all `APOLLO_GRAPH_SIM_*`
  flags ON). Load via `. .\scripts\load_local_env.ps1`.

## Run commands — the ACTUAL working sequence (2026-06-23)

All from `ai-ta-backend/`, project venv. Every PowerShell call dot-sources the
env (tool sessions don't persist env) and forces `DATABASE_URL=SUPABASE_DB_URL`
(seeders read `DATABASE_URL`; the stack reads `SUPABASE_DB_URL`).

```powershell
. .\scripts\load_local_env.ps1 ; $env:DATABASE_URL = $env:SUPABASE_DB_URL
$py = ".venv\Scripts\python.exe"

# 1. dedicated, ISOLATED macro course (returns MACRO_SPACE_ID = 3)
& $py scripts\_macro_setup_course.py

# 2. registry seed (creates macro subject/concepts/problems; subject backfilled to MIN id=1)
& $py -m scripts.seed_apollo_concept_registry --database-url $env:SUPABASE_DB_URL

# 3. PIN the macro subject to its own course (overrides the MIN backfill)
& $py scripts\_macro_setup_course.py          # now aligns apollo_subjects -> space 3

# 4. embed Ch.6 into local pgvector for course 3 (562 chunks, status=ready)
& $py scripts\index_local_pdf.py --pdf "<...>\free_short_macro_textbook_openstax_ch6.pdf" `
      --search-space-id 3 --material-kind textbook --week none

# 5. learner-model seed — MUST pass --search-space-id 3 (else it defaults to the
#    MIN=1 fluids course and errors "no 'macroeconomics' subject for search_space_id=1")
& $py scripts\seed_apollo_learner_model.py --subject-slug macroeconomics `
      --search-space-id 3 --database-url $env:SUPABASE_DB_URL

# 6. :Canon projection SCOPED to the macro course (NOT the MIN default)
& $py -m scripts.seed_canon_projection --search-space-id 3 --database-url $env:SUPABASE_DB_URL

# 7. RAG relevance pathway test (hybrid retrieval over the Ch.6 corpus)
& $py scripts\_macro_mine.py --search-space-id 3

# 8. boot a fresh server on :8001 from the working tree (picks up the macro antonyms)
& $py -c "import uvicorn; uvicorn.run('server:app', host='127.0.0.1', port=8001)"   # background

# 9. the 15-attempt sweep (WEB_BASE_URL pins it to :8001)
$env:WEB_BASE_URL = "http://127.0.0.1:8001"
& $py scripts\apollo_grade_probe.py --macro --subject-slug macroeconomics `
      --variations strong,partial,weak --tag .macro1
```

> **Re-runs:** after the one-time setup above, `scripts\run_macro_probe.py
> --skip-embed --tag <t>` bundles seed→boot→probe→score-matrix (its seed step is
> now scoped to the macro course).

## Gotchas hit and resolved

- **`search_space_id` alignment.** The registry seeder backfills a new subject to
  `MIN(aita_search_spaces.id)` (the fluids course). For an isolated test you must
  create a dedicated course AND pin `apollo_subjects.macroeconomics.search_space_id`
  to it (`_macro_setup_course.py`), then pass `--search-space-id` to the
  learner-model seed AND canon projection. Missing this is the #1 failure mode.
- **`I` is SymPy's imaginary unit** → investment is `INV` everywhere (symbol,
  given_values, equation, aliases). Caught by the content author's parse probe.
- **Unique problem serving.** `session_init` serves the *first problem at the
  requested difficulty* in the inferred concept. Two problems sharing a
  `(concept, difficulty)` collide. Fix: distinct difficulty per problem within a
  concept — gdp_components {intro, standard, hard}, nominal_vs_real_gdp {standard,
  hard} — a deterministic routing key, not a pedagogical claim.
- **DB URL env var split:** engine reads `SUPABASE_DB_URL`; seeders read
  `DATABASE_URL`. Set `DATABASE_URL = SUPABASE_DB_URL` per session.
- **`tier` must be 2** (teachable) — the ORM seeder default handles it.
- **`entity_key` + `declared_paths`** are authored into the problem JSONs (and
  re-minted idempotently by the learner-model seed) — required by
  `build_reference_canonical`.
- **Background probe stdout is block-buffered** — progress shows on completion;
  poll `apollo_graph_comparison_runs` for live progress instead.
- **RAG relevance is exercised via hybrid retrieval** (`_macro_mine.py`), not the
  per-chunk `scrape_questions` (which needs a prompt-wrapping `chat_fn`); the
  retrieval path is the production relevance pathway and is the bounded test.
