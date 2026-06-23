# Handoff: Apollo auto-provisioning is scaffolding-complete but NOT LLM-functional

**Date:** 2026-06-22
**Branch:** `feat/apollo-kg-wu3b2h-quarantine-shadow`
**How found:** first real end-to-end local run of the textbook → question-bank →
Apollo pipeline (see `docs/LOCAL-FULL-STACK-RUNBOOK.md`).

## TL;DR

The Apollo auto-provisioning pipeline (WU-3B2*) — scrape → solve → pair →
tag/mint → promote — **does not produce a question bank when run against a real
LLM.** Every LLM-facing prompt in the pipeline is a one-line stub that passes CI
only because the unit tests inject perfectly-shaped *mocked* `chat_fn` responses.
The orchestration around them (queue, leasing, idempotency, persistence, gating,
metering, Neo4j projection) is real and works. **The generation itself is the
unimplemented part.**

This means: even fully deployed (migrations applied) and flag-on
(`APOLLO_AUTOPROVISION_ENABLED=1` + worker scaled), uploading a textbook yields
**zero** teachable (tier-2) problems.

## Evidence (live run, local Supabase + local Neo4j)

Two textbook uploads of a small Bernoulli problem PDF, observed via
`apollo_ingest_runs`:

| run | document | status | n_questions_scraped | n_promoted | failure |
|---|---|---|---|---|---|
| 1 | doc 1 | succeeded | **0** | 0 | scrape returned nothing (pre-fix) |
| 2 | doc 2 | **failed** | 4 | 0 | `apollo_ingest_errors`: `find_or_generate / SolutionDraftError` |

## Root-cause pattern

Each stage takes an **injected `chat_fn` / `judge_fn`** seam. The provisioning
unit tests (`apollo/provisioning/tests/*`) inject a mock that returns ideal
JSON, so the **real prompt strings are never exercised** and the **schema
contract between prompt and parser is never validated end-to-end**. The 95%
patch-coverage gate is satisfied entirely by mocked tests. This is a
**test-design gap**, not only a prompt gap — the fix must include at least one
un-mocked (live-LLM, CI-skippable) test per stage so this class of bug is caught.

## Stage-by-stage status

### 1. Scrape — `apollo/provisioning/orchestrator.py:82` (`_SCRAPE_SYSTEM_PROMPT`) — FIXED
- **Was:** `"Extract candidate practice questions from the course passage as a JSON array of objects."`
- Real `gpt-4o-mini` returned a ```` ```json ```` -fenced array whose objects used
  `question`/`parameters` keys → `json.loads` failed on the fence, and even
  unfenced the keys don't match `CandidateQuestion`
  (`problem_text`, `given_values`, `target_unknown`, `difficulty`, `concept_slug`).
  → 0 candidates.
- **Fix applied:** rewrote the prompt to specify the exact `CandidateQuestion`
  schema and forbid prose/markdown fences. Verified live: `n_questions_scraped`
  **0 → 4**. ⚠️ **No un-mocked test yet** — `test_scrape.py` still mocks `chat_fn`.

### 2. Solution generate/extract — `apollo/provisioning/solution.py:219-223, 246-249` — BROKEN (not fixed)
- Prompt: `"...produce a reference solution as a JSON object with a 'reference_solution' list of typed steps."` — never specifies the step schema.
- `Problem.reference_solution` requires a list of **`ReferenceStep`**
  (`apollo/schemas/problem.py:40`): `{step:int, entry_type: equation|definition|
  condition|simplification|variable_mapping|procedure_step, id:str, content:dict
  (per-type, validated by the ontology), depends_on:[ids]}`, plus model-validators
  (depends_on must resolve; `procedure_step.order` must be 1..N contiguous;
  `uses_equations` must resolve to equation ids).
- Real `gpt-4o` returned steps shaped `{type, formula, explanation, substitutions}`
  → **24 `Problem` validation errors** (every required field missing) →
  `SolutionDraftError` → run fails. The scrape fix simply surfaced this next stub.

### 3. Pairing gate — `apollo/provisioning/pairing_gate.py:132-141` — UNVERIFIED (never reached)
- Injected `judge_fn`; user message is `json.dumps(payload)`. Same mocked-in-tests
  pattern; not exercised live because stage 2 fails first.

### 4. Tag / mint — `apollo/provisioning/orchestrator.py:345-360` + `tag_mint.py:181-192` — BROKEN by inspection
- `_tag_mint_chat_fn` builds `messages=[{"role":"user","content": json.dumps(problem)}]`
  with **NO system prompt at all**, yet `_parse_tag` requires a JSON object with
  `concept_slug` (+ `display_name`, prereq edges). A real LLM has no instruction
  to produce that shape → `TagMintError`.

### 5. Promote (8-gate lint + `:Canon`) — `apollo/provisioning/promote.py` / `promotion_lint.py` — UNVERIFIED
- Downstream of all the above; never reached live.

## Recommended fix approach (when prioritized)

1. **Use OpenAI structured outputs** (`response_format={"type":"json_schema", ...}`)
   generated from the actual Pydantic models (`CandidateQuestion`, a
   `ReferenceStep`/`Problem`-derived schema, the tag/`ApprovedPair` shape) instead
   of free-text "return JSON" instructions. This makes the prompt↔parser contract
   machine-enforced.
2. **Few-shot** each stage with one fully-worked example (a seeded Bernoulli
   problem from `apollo/subjects/.../problems/*.json` is an ideal exemplar).
3. **Add one un-mocked integration test per stage** (mark it e.g. `@pytest.mark.live`
   so CI can skip without an API key, but it exists and runs on demand) asserting
   the real model output validates against the stage's schema. This is the missing
   safety net that allowed all of the above to ship green.
4. Re-run the local runbook to confirm tier-2 problems land in
   `apollo_concept_problems` and `:Canon` nodes appear in Neo4j.

## Changes made in this session (kept)

- `apollo/provisioning/orchestrator.py` — `_SCRAPE_SYSTEM_PROMPT` rewritten
  (schema-explicit, fence-forbidding). Uncommitted; **needs an un-mocked test**.
- Local-dev scaffolding (gitignored / new): `.env.local`, `docker-compose.local.yml`,
  `scripts/{bootstrap_local_db,make_smoke_pdf,load_local_env,local_e2e_smoke}.py`,
  `server.py` `.env.local` shim, `docs/LOCAL-FULL-STACK-RUNBOOK.md`.

## Owner-doc note

`docs/architecture/apollo.md` (owns `apollo/`) describes the provisioning pipeline
as built; it should gain a **"Known limitation: generation prompts are stubs,
validated only against mocked `chat_fn`; not LLM-functional as of 2026-06-22"**
caveat in the WU-3B2 section. (Deferred — that file is large; annotate when next
editing it.)
