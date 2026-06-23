# Handoff: Apollo auto-provisioning — prompts fixed, but promotion still blocked by the retrieval-grounding stub

**Date:** 2026-06-22
**Branches/PRs:**
- `fix/apollo-provisioning-prompts` (cut from `staging`)
- **PR #58 — MERGED** into `staging` (merge commit `ba26329`): the Stage-1/2/4 prompt fixes.
- **Stage-3 fix (`87495f7`) is pushed to the branch but NOT merged** — PR #58 closed before it landed. **It needs a fresh PR into `staging`.**
- Supersedes/extends: `docs/APOLLO-AUTOPROVISION-LLM-STUBS-HANDOFF.md` (the original stub diagnosis).

---

## TL;DR

The Apollo auto-provisioning generation prompts (stages 1–4) are now schema-explicit
and **verified working against a real LLM**. But uploading a textbook **still
promotes ZERO tier-2 problems**. The blocker is NOT the prompts and NOT the PDF
content — it is a **separate, deliberate v1 stub**: `_default_retrieve_fn`
(`apollo/provisioning/orchestrator.py:572-577`) returns `()`, so the solution
grounding is always empty, so the stage-3 faithfulness judge marks every claim
"not entailed by the grounding" and **rejects every candidate**. Until the
retrieval-grounding adapter is implemented, the pipeline cannot promote anything.

A secondary issue: ~1-in-5 candidates still fails stage 2 with `SolutionDraftError`,
which aborts the whole document run (a `_PerDocumentError`).

---

## What was done and pushed

### In `staging` now (PR #58, merged as `ba26329`)
Commits `5a1c6a9` + `43ee094`:
- **New `apollo/provisioning/provisioning_schema.py`** — `build_solution_schema()` /
  `build_tag_schema()` derive their `json_schema` from the Pydantic models
  (`ReferenceStep.model_fields`, the `_parse_tag` keys). Single source of truth,
  mirroring `apollo/parser/extraction_schema.py`.
- **Stage 1 (scrape)** — `_SCRAPE_SYSTEM_PROMPT` rewritten schema-explicit (was a
  vague one-liner). Already in the working tree from the prior session; this PR
  added its contract test.
- **Stage 2 (`solution.py`)** — schema-explicit extract/generate prompts; switched
  `response_format` `json_object` → `json_schema`. `Problem.model_validate` stays
  the inner-dict enforcer (the per-`entry_type` `content` dict can't be strict).
- **Stage 4 (`orchestrator._tag_mint_chat_fn`)** — added `_TAG_MINT_SYSTEM_PROMPT`
  + threaded `json_schema` (previously sent NO system prompt at all).
- Deterministic contract tests (`test_provisioning_schema.py`, +scrape/+orchestrator)
  asserting each stage's declared schema == the Pydantic model fields. No live-LLM
  tests (by request). Patch coverage 100%; CI green; `docs/architecture/apollo.md`
  reconciled.

### Pushed but NOT merged — needs a new PR
Commit `87495f7` on `fix/apollo-provisioning-prompts`:
- **Stage 3 (pairing gate)** — `_judge_or_fail_closed` was calling the judge with
  `response_format=json_object`, **no system prompt, and no "json" token**, so the
  live call 400'd (`'messages' must contain the word 'json'`) and every pair
  fail-closed to a REJECT. Fix: added `_PAIRING_PHASE_A/B_SYSTEM_PROMPT` (declaring
  the exact `paired`/`confidence` and `claims:[{claim,entailed}]` keys) + strict
  `json_schema` (`build_pairing_phase_a_schema`/`build_pairing_phase_b_schema`),
  with contract + wiring tests. Verified live (the 400s are gone; the judge now
  runs both phases and returns genuine verdicts). **Action: open a PR for this
  commit into `staging`** (PR #58 already merged without it).

---

## Live verification evidence (local Supabase + local Neo4j + real OpenAI)

All runs on the Bernoulli smoke PDF. `tier=2 total=5` throughout is **pre-seeded
curriculum** (`solution_source` null), NOT provisioning output — measure promotion
by `n_promoted` and new tier-2 rows with `solution_source` set.

| run | doc | after which fix | scraped | promoted | rejected | error |
|----|----|----------------|---------|----------|----------|-------|
| 2 | 2 (5 chunks, problems-only) | original (pre-fix) | 4 | 0 | 0 | `SolutionDraftError` (dies at stage 2 immediately) |
| 3 | 2 | stage-2 fix | 4 | 0 | 3 | stage-3 **400** "must contain word json" → all rejected + 1 `SolutionDraftError` |
| 4 | 2 | stage-3 fix | 4 | 0 | 2 | stage-3 400s GONE; 2 **genuine** `unfaithful_claims` rejections + 1 `SolutionDraftError` (12 LLM calls, $0.027) |
| 7 | 3 (15 chunks, **theory + problems**) | stage-3 fix + grounded PDF | 5 | **0** | 3 | all 3 `unfaithful_claims` + 1 `SolutionDraftError` (25 LLM calls, $0.038) |

Progression proves each prompt fix worked: stage 2 went 0→4/5 valid solutions;
stage 3 went from 400-on-every-call to running cleanly with real verdicts. **But
promotion stayed 0 at every step.**

---

## The remaining problem

**Zero tier-2 teachable problems are ever promoted**, even with all four prompts
correct and a content-rich source PDF. Run 7's three rejections are all
`apollo_rejected_problems.reason = 'unfaithful_claims'` — "one or more solution
claims are not entailed by the grounding."

---

## Root cause (code-proven, not hypothesized)

**`_default_retrieve_fn` is a v1 stub that returns no grounding**
(`apollo/provisioning/orchestrator.py:572-577`):

```python
async def _default_retrieve_fn(question) -> Sequence[GroundingSpan]:
    """... v1 returns no spans (the generate branch grounds on the question
    alone); the real hybrid-retrieval adapter is a Tier-2 nightly concern."""
    return ()
```

Trace the consequence through the pipeline:

1. `find_or_generate(db, candidate, retrieve_fn, chat_fn)` (`solution.py`) calls
   `retrieve_fn(question)` → `()`. With no spans, it takes the **generate** branch
   and grounds on the question text alone; `draft.grounding = ()`.
2. `validate_pair(question, draft, retrieve_fn, judge_fn)` (`pairing_gate.py:180-183`)
   sees `draft.grounding` empty, so it re-grounds via `retrieve_fn(question)` → `()`
   again → `grounding_text = _grounding_text(()) = ""`.
3. **Phase B faithfulness** asks the judge to decide whether each decomposed claim
   is "entailed by the grounding" — against an **empty string**. Nothing is
   entailed by nothing → `faithful=False` → `unfaithful_claims` → **reject**.

This is structural: with `_default_retrieve_fn` returning `()`, the faithfulness
gate rejects **every** candidate regardless of the source document. The 15 embedded
theory chunks of doc 3 are never retrieved as grounding — `_default_retrieve_fn`
does not query `aita_chunks` at all.

### Why the PDF update was decisive (and disproved the first hypothesis)
The initial hypothesis was "the 1-page PDF's grounding is too thin." We tested it
by rebuilding the smoke PDF with a full theory section (Bernoulli + continuity
equations, assumptions, special cases, a fully worked example) — `doc 3`, 15 chunks.
Run 7 **still** rejected all candidates as `unfaithful_claims`. That falsified the
thin-grounding theory and pointed straight at the retrieval stub: the grounding the
judge sees is empty no matter what the PDF contains, because nothing retrieves it.

---

## Secondary issue — the stage-2 straggler

~1-in-5 candidates still fails `find_or_generate` with `SolutionDraftError`
(run 7: 1 of 5). This raises a `_PerDocumentError` (`orchestrator.py:447-448`) that
**aborts the entire document run**, even though other candidates may have produced
valid solutions. Two questions for the owner:
- Is one bad candidate aborting the whole document's provisioning the intended
  behavior, or should a single `SolutionDraftError` be a per-candidate rejection
  (like the pairing-gate rejections) so the rest of the document still promotes?
- Is the straggler a genuinely hard candidate, or a residual gap in the non-strict
  stage-2 schema? (Needs inspection of that candidate's generated payload — not yet
  done; deferred per scope.)

---

## Recommended fix (the real unblock)

Implement `_default_retrieve_fn` against the **existing hybrid retrieval pipeline**
(`retrieval/`, pgvector + FTS over `aita_chunks`) so the generator and the
faithfulness judge get real course grounding:

- For a candidate `question`, retrieve top-K chunks **scoped to the candidate's
  course** (`search_space_id`) from `aita_chunks`, and return them as
  `GroundingSpan(text=..., document_id=..., page=..., chunk_content_hash=...)`.
- Consider detecting printed worked solutions to set `carries_solution=True` so the
  cheaper **extract** path in `find_or_generate` can fire when the source already
  contains a solution (the theory PDF's "Worked example" is exactly such content).
- Bound span count / token budget (the metered ceiling already exists in
  `MeteredChat`).
- Reuse the course-scoping discipline that the rest of provisioning uses
  (`Subject.search_space_id`), so grounding never crosses courses.

This is a larger change than the prompt fixes and is flagged in-code as a "Tier-2
nightly concern" — it deserves its own design + PR. After it lands, re-run the live
test (below) and confirm `n_promoted > 0` with new tier-2 rows carrying
`solution_source`, and `:Canon` nodes appearing in Neo4j.

---

## How to reproduce (local test harness — LOCAL-ONLY, not committed)

Local-dev scaffolding (gitignored / untracked; intentionally excluded from PRs):
- `scripts/make_smoke_pdf.py` — now generates a 2-page PDF: **page 1 reference/theory
  (the grounding basis), page 2 problems**. Regenerate: `python scripts/make_smoke_pdf.py`.
- `scripts/local_e2e_smoke.py` — uploads the PDF as a textbook (creates a new
  `aita_documents` row; the ingest worker embeds it and auto-enqueues a provisioning job).
- `.feller/tasks/2026-06-22-fix-apollo-provisioning-prompts/verify_reprovision.py` —
  a one-shot drain (`enqueue → claim → run_provisioning(real MeteredChat) → complete/fail`)
  that prints the outcome + tier deltas. Run: `python <that file> <document_id>`.

Runbook: `docs/LOCAL-FULL-STACK-RUNBOOK.md`. Env: dot-source `scripts/load_local_env.ps1`
(loads `.env` secrets + `.env.local` local overrides) before any process.

**Gotchas observed:**
- The live `apollo.provision_worker` process was **stopped** during this session (it
  was running stale code and would otherwise claim jobs with the old prompts).
  Restart it (`python -m apollo.provision_worker` with env loaded) once retrieval is
  fixed — but note `fail_job` re-queues a failed job to `pending` until
  `attempt_count >= MAX_ATTEMPTS`, so a worker will loop on a still-failing job.
- `enqueue_provisioning_job` collides (returns `None`) when an OPEN job already
  exists for the document (partial-unique-index). To re-test a doc, terminalize open
  jobs first:
  `update apollo_provisioning_jobs set state='failed', lease_owner=null, lease_expires_at=null where state in ('pending','running');`
- `claim_provisioning_job` is FIFO-earliest across ALL open jobs, not per-document —
  clear stragglers before draining a specific doc.

Useful queries (local Postgres `postgresql://postgres:postgres@127.0.0.1:54322/postgres`):
```sql
select tier, count(*), count(*) filter (where solution_source is not null) gen
  from apollo_concept_problems group by tier;            -- promotion check
select id, document_id, status, n_questions_scraped, n_promoted, n_rejected
  from apollo_ingest_runs order by id desc;              -- per-run outcome
select rejected_stage, diagnostic, payload from apollo_rejected_problems
  where ingest_run_id = <run>;                           -- rejection reasons
```

---

## Status summary

| Layer | Status | Where |
|---|---|---|
| Stage 1 scrape prompt | ✅ fixed, merged | PR #58 (`staging`) |
| Stage 2 solution prompt | ✅ fixed, merged | PR #58 (`staging`) |
| Stage 4 tag/mint prompt | ✅ fixed, merged (unreached live) | PR #58 (`staging`) |
| Stage 3 pairing prompt/400 | ✅ fixed, **needs a new PR** | `87495f7` on branch |
| **Retrieval grounding** (`_default_retrieve_fn`) | ❌ **v1 stub `()` → faithfulness rejects all** | `orchestrator.py:572-577` |
| Stage-2 straggler | ⚠️ open question (aborts whole run) | `orchestrator.py:447-448` |

**Bottom line:** the prompt-stub work is done and correct, but auto-provisioning
will keep promoting **zero** problems until `_default_retrieve_fn` retrieves real
grounding from the course's embedded chunks. That retrieval adapter is the next
piece of work.

## Owner-doc note
`docs/architecture/apollo.md` (owns `apollo/`) was reconciled for the stage-1/2/4
prompts (PR #58) and the stage-3 pairing prompt (`87495f7`). When the retrieval
adapter lands, update the `find_or_generate` / `validate_pair` entries to drop the
"v1 returns no spans" caveat and describe the real grounding source, and bump
`last_verified`.
