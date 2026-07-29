# Apollo Authored Problem/Solution Sets — Design

- **Date:** 2026-06-29
- **Branch:** `ApolloRun` (PR base: `staging`)
- **Status:** Design — awaiting user review before planning
- **Owner doc to update on landing:** `docs/architecture/apollo.md` (owns `apollo/**`),
  `docs/architecture/indexing.md` (owns `ocr/**`, `indexing/**`),
  `docs/architecture/_overview.md` (HTTP surface, config/env)
- **Frontend:** `ai-ta-teacher-ui` (separate repo, own branch + PR)

---

## 1. Goal

Teachers upload **paired** problem/solution documents as "sets":

```
[ Authored Problem Set 1 ]  ↔  [ Authored Solution Set 1 ]
[ + Add set ]  →  [ Problem Set 2 ] ↔ [ Solution Set 2 ], …
```

Each set = one problems doc + its corresponding solutions doc, explicitly paired by the UI.
Per set, the backend:

1. Indexes both docs (`AITADocument` + `aita_chunks`).
2. Scrapes candidate problems from the **problem** doc.
3. For each problem, resolves its **reference solution** from the **paired solution doc** in
   priority order:
   - **(a)** number/label match (`Problem 3` ↔ `Solution 3` / `3.`) → **extract** that
     printed solution → `solution_source='extracted'` (ground truth).
   - **(b)** else doc-scoped retrieval against the paired solution doc only → **extract** from
     the best match → `solution_source='extracted'`.
   - **(c)** else **generate** (existing path) → `solution_source='generated'`, flagged.
4. Runs the existing content-derived promotion gates → tier-2 teachable.

Pairing is deterministic by set, so grounding is scoped to exactly one solution doc — not the
whole course, not a fuzzy sweep.

## 2. Why this design

This is a **scoped variant of the scrape pipeline**, not the existing coupled "authored" path
(`provision_authored_problem`, which expects one problem with its solution supplied inline and does
no search). We scrape the problem doc but ground each problem against **only its paired solution
doc**, queried directly by `document_id`. Scoping by `document_id` deliberately eliminates the
generic scrape-path failure modes:

- **week-gating** (`retrieval/document_visibility.py::active_document_conditions` only gates
  `notes`/`slides`, but the gate is irrelevant when we query by `document_id`),
- **per-(week,kind) supersede** (`knowledge/teacher_weekly.py:1107–1130`),
- **whole-corpus grounding** picking the wrong solution.

Ground-truth references matter because the teach-back grader diffs the student KG against the
reference graph: `apollo/handlers/done.py:266` builds `reference_graph = problem.to_kg_graph(...)`,
then `apollo/overseer/coverage.py:331–442` grades student entries against `reference_nodes`. A
wrong/auto-generated reference ⇒ wrong grade ⇒ wrong XP. So generated references (branch c) and
OCR-suspect references are **flagged and held out of grading** until a teacher approves.

## 3. Decisions (settled with the user)

| # | Decision | Choice | Rationale |
|---|----------|--------|-----------|
| D1 | Indexing path | **Dedicated authored-set indexer**: reuse the indexing *core* (`TeacherPDFIngestor` → `prepare_for_indexing` → `embed_and_persist_chunks` → `finalize_document`), skip the weekly *wrapper* (supersede, week-activation, forced `status=ready`, generic auto-enqueue). **No synthetic weeks.** | The indexing core is identical either way; the wrapper's four side-effects all fight this feature (supersede clobbering across sets/docs, solution-leak to student RAG, double-provisioning, forced weekly semantics). |
| D2 | Doc visibility | Both docs **hidden** from student RAG (`status.state` ≠ `"ready"`). Grounding reads by `document_id` and ignores status. | Solutions must never be student-retrievable. Default-hidden for problem docs too in v1; trivially flippable later. |
| D3 | Trigger | **Submit → in-process background task → poll** (not pure-sync, not the full queue/worker). | Pure-sync risks gateway timeouts (per-page vision OCR = many sequential calls). In-process background keeps the queue/lease machinery out while staying timeout-safe. Core orchestration is trigger-agnostic so a future move to the worker queue is a wiring change only. |
| D4 | Problem↔solution correspondence | **Capture label in scrape**: add optional `label` to `CandidateQuestion` + scrape prompt; deterministic match to labeled blocks in the solution doc. | Most reliable; additive/backward-compatible change to the shared scrape. Ambiguity (0 or ≥2 matches) → fall through to retrieval (b). |
| D5 | OCR engine | **OpenAI vision provider** (`OCR_PROVIDER=openai`), pluggable via `ocr/factory.py`, reuses the existing `OPENAI_API_KEY`; returns transcription + self-reported confidence per block. | 2025–26 evidence: vision LLMs match/beat Mathpix on handwriting, cheaper; zero new vendor; the `OCRProvider` ABC is already pluggable. Mathpix is **not** configured on staging. |
| D6 | Trust gate | (a) + (b) → `extracted`, trusted; (c) → `generated`, flagged. | Matches handoff priority intent. |
| D7 | OCR verification | On **low** OCR confidence (not on a raw flag): run the generate path independently and **compare** extracted vs generated. **Material divergence → flag** `review_required`; agreement → trust. High-confidence skips the cross-check. | Low confidence ≠ wrong; an independent generation corroborates (or refutes) the OCR. Spends the extra LLM cost only when OCR is shaky. |
| D8 | "Differ" definition | **Material divergence** = different *final answer* OR a directly *contradictory core equation*. Equivalent-but-different procedures are **not** flagged. SymPy settles final-answer equivalence; an LLM judge handles the core-equation check. | Minimizes false flags while catching the OCR corruptions that matter (usually a changed number/answer). |
| D9 | Flagged → grading | A flagged reference is **held out of grading until the teacher approves** (kept at tier-1, not promoted to teachable; badged in the UI with both the OCR and generated solutions shown). | Don't silently grade students against a reference we already suspect. Reuses the existing tier-1/tier-2 selectability gate as the "held" mechanism. |
| D10 | Edge case | If the **problem** doc is *also* low-confidence OCR, the generated comparison is itself unreliable → flag regardless of agreement. | The comparison's own ground truth is shaky. |

## 4. Architecture

### 4.1 Reused unchanged (or additive-only)

- `apollo/provisioning/solution.py::find_or_generate` — **unchanged**. Its extract branch
  (`has_printed = any(s.carries_solution for s in spans)` → `solution_source='extracted'`) is
  activated simply by returning spans with `carries_solution=True`.
- `validate_pair`, `build_approved_pair`, `tag_and_mint`, `promote`, `promotion_lint` — unchanged.
- `scrape_document`, `write_tier1_problems`, `resolve_or_create_provisional_concept` — scrape gets
  an **additive optional** `label` field (generic path unaffected).
- `retrieval/hybrid_search.py::_build_semantic_cte` / `_build_keyword_cte` — reused via a
  doc-scoped wrapper (they are parameterized by `visible_doc_ids` / `base_conditions`).
- Indexing core (`indexing/indexing_service.py::prepare_for_indexing`,
  `indexing/checkpoint_indexer.py::embed_and_persist_chunks` / `finalize_document`,
  `knowledge/teacher_pdf_ingestion.py::TeacherPDFIngestor`) — reused **without** the weekly wrapper.
- Grading consumer (`done.py` / `coverage.py`) — unchanged; benefits from trusted references and
  the tier-1 hold on flagged ones.

### 4.2 New components

**N1 — OpenAI vision OCR provider** (`ocr/openai_vision.py`)
`OpenAIVisionOCRProvider(OCRProvider)` implementing `recognize(image_bytes, mime, dpi) -> OCRResult`.
One vision call per page → transcription (LaTeX/markdown) + a self-reported confidence; returns
`OCRResult(blocks=[OCRBlock(kind, text, confidence)])` so `average_confidence()` and the existing
`min_ocr_confidence` gating keep working. Selected in `ocr/factory.py` when `OCR_PROVIDER=openai`.
Model configurable via `APOLLO_OCR_MODEL` (default a vision-capable model; `gpt-5.1` supports
vision). Reuses `OPENAI_API_KEY`. Provider is OCR-engine-agnostic at the `OCRProvider` seam, so
swapping in Gemini Flash later is a new provider class only.

**N2 — Authored-set indexer** (`apollo/provisioning/authored_sets/indexing.py`)
`index_authored_doc(db, *, search_space_id, file_bytes, title, role) -> int` (role ∈
`{"problem","solution"}`). Runs `TeacherPDFIngestor` → `prepare_for_indexing` →
`embed_and_persist_chunks` → `finalize_document`, then **overrides the doc status** to a hidden
sentinel (`{"state": "apollo_reference"}`) so `active_document_conditions` (requires
`state=="ready"`) excludes it from student RAG. Does **not** create a `TeacherUpload`, does **not**
supersede, does **not** call `_sync_week_activation`, does **not** enqueue generic provisioning.
Returns the `AITADocument.id`.

**N3 — Paired-solution retrieve_fn** (`apollo/provisioning/authored_sets/paired_retrieval.py`)
`make_paired_solution_retrieve_fn(db, *, solution_document_id, label_index) -> retrieve(question)`:
1. **Label match** — if `question.label` resolves in `label_index`, return that chunk(s) as
   `GroundingSpan(carries_solution=True, ...)` with `provenance.match_method="label"`.
2. **Doc-scoped retrieval** — else hybrid search over the solution doc only: reuse the CTE builders
   with `visible_doc_ids=[solution_document_id]` and
   `base_conditions=[AITAChunk.document_id == solution_document_id]` (**never**
   `active_document_conditions`). Return top spans with `carries_solution=True`,
   `match_method="retrieval"`.
3. **Empty** — else `()` → `find_or_generate` takes the generate branch (`generated`).
Every span carries `page` and the source page's `ocr_confidence` (read from
`AITADocument.document_metadata.page_debug[page].ocr_confidence`), so the verification step (N5) can
compute the grounding's min OCR confidence.

**N4 — Label matcher** (`apollo/provisioning/authored_sets/label_match.py`)
- `extract_problem_label(candidate) -> str | None` — prefer the scraped `label`; regex fallback on
  `problem_text`.
- `build_solution_label_index(solution_chunks) -> dict[str, list[chunk]]` — regex over solution
  chunk text for `Solution N`, `Problem N`, `N.`, `N)`, `Q N`, `Question N`, `Exercise N`,
  `Part (a)/(b)`; normalize to a canonical key.
- `match(label, index) -> list[chunk] | None` — **0 or ≥2 distinct blocks → `None`** (fall through
  to retrieval). Deterministic, fail-safe.
- **Scrape change:** add optional `label: str | None = None` to `CandidateQuestion`
  (`apollo/provisioning/scrape.py`) and instruct the scrape prompt to capture the printed label.

**N5 — OCR-confidence verification** (`apollo/provisioning/authored_sets/verification.py`)
After `find_or_generate` returns an `extracted` draft, compute the grounding's min OCR confidence.
If `< APOLLO_AUTHORED_OCR_CONF_THRESHOLD` (default `0.6`) **or** the problem doc is low-confidence
(D10): run the generate path independently, then compare (D8):
- **final answer** via SymPy (reuse `parse_zero_form` / `_derive_symbolic_answer` from
  `promotion_lint.py`),
- **core equation** via an LLM judge (`MeteredChat.cheap`).
Material divergence → return a verdict `{review_required: True, reason: "ocr_divergence",
generated_alt: <draft>}`. Agreement → `{review_required: False}`. High-confidence extractions skip
this entirely.

**N6 — Pairing persistence** (migration `database/migrations/032_apollo_authored_sets.sql` + model in
`apollo/persistence/models.py`)

```sql
CREATE TABLE IF NOT EXISTS apollo_authored_sets (
    id                    BIGSERIAL PRIMARY KEY,
    search_space_id       INTEGER NOT NULL REFERENCES aita_search_spaces(id) ON DELETE CASCADE,
    set_index             INTEGER NOT NULL,
    problem_document_id   BIGINT,
    solution_document_id  BIGINT,
    status                TEXT NOT NULL DEFAULT 'pending',   -- pending|indexing|provisioning|done|failed
    result_summary        JSONB NOT NULL DEFAULT '{}'::jsonb, -- per-problem results (bounded)
    created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (search_space_id, set_index)
);
CREATE INDEX IF NOT EXISTS apollo_authored_sets_space_idx ON apollo_authored_sets(search_space_id);
```

Per-problem results live in `result_summary` JSONB (a set has a handful of problems — bounded), so
no second table. Per-problem review metadata also rides in
`apollo_concept_problems.provenance` (`authored_set_id`, `match_method`, `ocr_confidence`,
`review_required`, `reason`). Flagged problems stay **tier-1** (held).

**N7 — Orchestration** (`apollo/provisioning/authored_sets/orchestrator.py`)
`run_authored_set_provisioning(db, neo, *, search_space_id, problem_document_id,
solution_document_id, metered_chat, embed_fn=None) -> ProvisioningReport` (trigger-agnostic):

```
resolve/create provisional concept
load solution chunks  → build_solution_label_index
load problem chunks   → scrape_document(label-aware) → write_tier1_problems
for each candidate:
    retrieve_fn = make_paired_solution_retrieve_fn(db, solution_document_id, label_index)
    draft   = find_or_generate(..., retrieve_fn, chat_fn=metered_chat.main)   # extract | generate
    verdict = verify_against_generated(draft, candidate, ...)                 # N5 (low-conf only)
    pair_v  = validate_pair(..., retrieve_fn, judge_fn=metered_chat.cheap)    # faithfulness
    if rejection_from_verdict(pair_v):            → 'rejected'(diagnostic);  continue
    if draft.solution_source == 'generated' OR verdict.review_required:
        # defer all KG mutation until a teacher approves
        write provenance(review_required, reason, ocr_confidence, match_method, generated_alt)
        on the tier-1 row → 'held_for_review';  continue
    pair = build_approved_pair(...); mint = tag_and_mint(...); promote(...) → 'promoted' | 'rejected'
return ProvisioningReport(per-problem results)
```

Implemented as a sibling to `_process_candidate` (`_process_authored_candidate`) to keep the generic
path untouched while sharing the stage helpers. Per-problem outcome enum extends to
`promoted | rejected | held_for_review`. **Held problems run no `tag_and_mint`/`promote`** (no
canonical-KG mutation for unverified references); the **approve** endpoint runs
`build_approved_pair → tag_and_mint → promote` on the teacher's chosen reference (OCR or generated).

**N8 — Endpoints** (`apollo/api.py`, teacher-gated: `require_user` + `require_course_member`)

| Method | Path | Purpose |
|--------|------|---------|
| POST | `/apollo/authored-sets` | multipart: problem PDF + solution PDF + `search_space_id`. Indexes both (hidden), persists pairing (`status=pending`, next `set_index`), launches the in-process background task, returns `{set_id, set_index, status}`. |
| GET | `/apollo/authored-sets?search_space_id=` | list sets + statuses for the course. |
| GET | `/apollo/authored-sets/{set_id}` | set detail + `result_summary`: per problem `{label, outcome, failed_gate, diagnostic, solution_source, match_method, ocr_confidence, review_required, reason, generated_alt?}`. |
| POST | `/apollo/authored-sets/{set_id}/problems/{problem_id}/approve` | resolve a `held_for_review` problem: `reference ∈ {ocr, generated}` → promote to tier-2. |

**N9 — Frontend** (`ai-ta-teacher-ui`, separate PR)
Paired upload section (`[Problem Set N] ↔ [Solution Set N]`), `[+ Add set]`, results view with
per-problem rows, a **"Needs review"** badge, and the approve action (shows OCR vs generated
side-by-side). Proxy routes under `app/api/teacher/authored-sets/` mirroring
`app/api/teacher/upload/route.ts`. Map the existing teacher upload page for styling/section
conventions first.

## 5. Data flow (per set)

```
Teacher submits Set N (problem.pdf, solution.pdf, search_space_id)
  → POST /apollo/authored-sets
      → index_authored_doc(problem)  → problem_document_id   (hidden)
      → index_authored_doc(solution) → solution_document_id  (hidden, OpenAI-vision OCR)
      → INSERT apollo_authored_sets(status=pending)
      → background task: run_authored_set_provisioning(...)
            status: indexing → provisioning → done|failed
            result_summary ← per-problem results
  → Frontend polls GET /apollo/authored-sets/{set_id}
      → renders per-problem outcomes + "Needs review" badges
  → Teacher clicks Approve on a held problem
      → POST .../approve → promote → tier-2 teachable
```

## 6. Error handling

- Per-**problem** failures (scrape/solution/lint/validate errors) reject that problem and continue
  the set (mirrors `_process_candidate`).
- Per-**set** failures (indexing failure, DB/embedding error) set `status=failed` with a diagnostic
  in `result_summary`.
- OCR degraded (no Mathpix/OpenAI key, or vision call fails) → solution chunks empty → label match
  and retrieval return nothing → `generated` + flagged (never a silent bad reference).
- Background task exceptions are captured into `status=failed` (never crash the web process).
- Apollo error types map through the existing `apollo/api.py` exception handlers.

## 7. Testing

**Unit (pytest, Supabase mock fixtures in `conftest.py`; Neo4j via Testcontainers `neo4j:5.25`):**
- OpenAI vision provider (mock OpenAI client): block/confidence shaping, error → empty result.
- Label matcher: each supported format; 0-match and ≥2-match → `None`.
- Paired retrieve_fn: doc-scoped only (asserts **no** `active_document_conditions`),
  `carries_solution=True`, label vs retrieval branch, empty → generate.
- Verification: agree → trust; diverge → flag; problem-doc low-confidence → flag regardless.
- Orchestrator: extract path → `extracted`; no-match → `generated`+held; verified-divergent → held;
  clean → promoted; lint-fail → rejected.
- Persistence model + migration; endpoints (auth gating, results shape, approve flips tier).

**E2E (staging `hjevtxdtrkxjcaaexdxt`, ss=4 "AAE 333 E2E Test"):** a real AAE HW set with a
**handwritten** solution, once `OCR_PROVIDER=openai` is set on staging. Assert: problems extracted,
references grounded to the paired solution doc, handwritten low-confidence ones flagged/held,
promoted ones tier-2 teachable.

## 8. Prerequisites & ops

- **Staging env** (`ai-ta-backend`, and `ai-ta-backend-worker` if/when async): set
  `OCR_PROVIDER=openai`; optional `APOLLO_OCR_MODEL`, `APOLLO_AUTHORED_OCR_CONF_THRESHOLD=0.6`.
  (Mathpix vars are absent today — verified 2026-06-29.)
- **Migration:** `032_apollo_authored_sets.sql` applied to staging before E2E.
- **Ruff (CI-blocking on files added vs `origin/staging`):** run `ruff check` **and**
  `ruff format --check` on every new file before commit.
- **Drift contract:** update `docs/architecture/apollo.md` + `indexing.md` + `_overview.md` in the
  same commits as the code; bump `last_verified`.

## 9. Out of scope / follow-ups

- Full async queue/worker path (in-process background is v1; core is trigger-agnostic).
- Full teacher editing of references (v1 = approve OCR or generated).
- Exposing problem docs to student RAG (default hidden in v1).
- Gemini Flash OCR provider (cheaper at scale; drop-in at the `OCRProvider` seam).
- Generic AAE scrape-path verification run remains paused.

## 10. Open items for user confirmation at spec review

1. **D3 trigger refinement** — confirm the in-process-background-task + poll model (vs strict
   inline-sync) given the OCR timeout reality.
2. **D2 problem-doc visibility** — confirm both docs hidden from student RAG in v1.
3. **Approve action scope** — confirm v1 approve = pick OCR-or-generated (no inline editing yet).
