---
doc: ai-ta-backend/rag-pipeline
description: QA answer pipeline — vision transcription, LLM keyword extraction, pgvector+FTS hybrid retrieval with RRF, reranking, store bias, token-budget context packing, and citation-disciplined tutor answers.
owns:
  - ai/**
  - retrieval/**
related:
  - ai-ta-backend/indexing
  - ai-ta-backend/domain-data
  - shared/supabase
last_verified: 2026-06-10
stub: false
---

## Module map and file landmarks

### `retrieval/` — the pgvector retrieval pipeline (7 files)

| File | Role |
|------|------|
| `retrieval/__init__.py` | Exports the single public entry point `retrieve_for_question`. |
| `retrieval/pipeline.py` | `retrieve_for_question()` — orchestrates hybrid search → rerank → store bias → context packing. The only function callers should use. |
| `retrieval/hybrid_search.py` | `AITAHybridSearchRetriever` — pgvector cosine + PostgreSQL FTS fused with Reciprocal Rank Fusion (RRF, k=60). Ported from SurfSense, adapted to chunk-level results. |
| `retrieval/reranker.py` | `AITARerankerService` — optional cross-encoder reranking via the `rerankers` library. Falls back to RRF order when disabled or on any error. |
| `retrieval/store_bias.py` | `apply_store_biases()` — post-rerank additive boost per material kind (textbook +0.12, slides +0.06, …). |
| `retrieval/context_packer.py` | `pack_context()` — greedy token-budget packing into `BundleSnippet`s with citation markers; `_summarize_snippets()` — regex extraction of equations/glossary/assumptions. |
| `retrieval/document_visibility.py` | `active_document_conditions()` — SQL filters for which documents are searchable (ready status + week gating); `build_chunk_metadata()`. Also imported by `workspaces/db.py`. |

### `ai/` — LLM orchestration layer (~21 files)

| File | Role |
|------|------|
| `ai/main_ai.py` (1697 lines) | All OpenAI chat-completion wrapper functions: `parse_question`, `check_question_relevance`, `extract_and_filter_keywords`, `solve_with_bundle` (the main tutor call), `format_answer` (citation enforcement), plus debug-file writers. |
| `ai/orchestrator.py` (980 lines) | `Orchestrator` class — a sequential state machine (`run()`) wrapping retrieve→parse→dump. **Legacy/eval path**: imported by `server.py` (line 25) but never instantiated there; the live `/ask` handlers call `_ask_pgvector` directly. Contains the `EVAL_MODE` context-pack dump hook used by the eval system. |
| `ai/vision.py` | `vision_transcribe()` (OpenAI vision → pytesseract fallback) and `vision_direct_answer()` (unused by `/ask`). |
| `ai/solver.py` | `run_python()` — sandboxed code exec (numpy/sympy/pint, forbidden builtins). **Dormant**: imported by `main_ai.py` but `solve_with_bundle` enforces conceptual-only mode (no code, `final_answers={}`). |
| `ai/prompts/` (11 files) | One module per prompt; `__init__.py` re-exports all. Key: `tutor.py` (the 186-line tutor system prompt — citation discipline, length rules, CYU rules), `concept_extraction.py` (keyword extraction), `relevance_guard.py` (full/partial/none scope classification), `score_and_answer_snippet.py` (per-snippet scoring). |
| `ai/router/` (5 files) | Two-stage query router: `embedding_router.py` (Stage 1 cosine-vs-seed-utterances), `llm_router.py` (Stage 2 gpt-4o-mini with strict JSON schema), `orchestrator.py` (`route()` combiner), `routes.py` (6-route registry with per-route `top_k`/rerank flags), `seeds.json`. **Not wired into server.py** — only `tests/router/` imports it. Built for a future retrieval-mode (NONE/AUGMENT/FRESH) cache feature. |

## Public interfaces

```python
# retrieval/pipeline.py — THE retrieval entry point
async def retrieve_for_question(
    query: str,                      # original (memory-augmented) question
    keywords: list[str],             # hints appended to query, NOT standalone targets
    search_space_id: int,            # class id
    db_session: AsyncSession,
    weight_overrides: Optional[dict[str, float]] = None,  # per-workspace kind biases
    top_k: int = 20,
    token_budget: int = 6000,
    citation_label: Optional[str] = None,
) -> tuple[list[BundleSnippet], dict[str, Any]]   # (snippets, diagnostics)

# retrieval/hybrid_search.py
class AITAHybridSearchRetriever:
    def __init__(self, db_session: AsyncSession, search_space_id: int)
    async def hybrid_search(self, query_text: str, top_k: int = 60,
                            material_kind: Optional[str] = None) -> list[dict]
    # dict keys: chunk_id, content, score, page_number, section_path, chunk_type,
    #            figure_id, document_id, doc_title, material_kind, week, metadata, ...

# retrieval/reranker.py
class AITARerankerService:
    @classmethod
    def get_instance(cls) -> "AITARerankerService"   # lazy global model load
    def rerank(self, query: str, chunks: list[dict]) -> list[dict]

# retrieval/store_bias.py
def apply_store_biases(chunks, weight_overrides=None) -> list[dict]  # adds 'final_score', re-sorts

# retrieval/context_packer.py
def pack_context(ranked_chunks, token_budget=6000, citation_label=None) -> list[BundleSnippet]
def _summarize_snippets(snippets) -> tuple[equations, glossary, assumptions, boundary_conditions]

# ai/main_ai.py — key exports
def parse_question(user_query, subject=None) -> ParsedTask
def check_question_relevance(question, subject=None) -> dict   # {relevance: full|partial|none, on_topic_portion, off_topic_portion, reason}
def extract_and_filter_keywords(question, subject=None) -> tuple[str, list[dict]]  # (context_summary, [{term, relevance}] max 8)
def solve_with_bundle(parsed_task, bundle, hint=None, subject=None) -> ProposedSolution
def format_answer(solution, bundle, *, include_background=True, citation_label=None, subject=None) -> FinalAnswer

# ai/vision.py
def vision_transcribe(image_paths: Sequence[str]) -> str
def vision_direct_answer(image_paths, question_hint="") -> str
```

Key dataclasses (`config/contracts.py`): `BundleSnippet` (id, type, page, section_path, text, citation_marker, final_score, metadata — `validate()` requires a non-empty citation_marker), `ResearchBundle` (metadata, snippets, equations, glossary, allowed_markers, provenance, …), `ParsedTask`, `ProposedSolution` (steps: str, final_answers: dict, equations_used, assumptions), `FinalAnswer` (text, citations).

## Main data flows

End-to-end QA flow as actually wired (`POST /ask` at `server.py:post_ask`, line ~1526; `POST /ask/stream` at `server.py:post_ask_stream` is the same pipeline with SSE status events):

1. **Auth + workspace** — `server.py:post_ask` validates question/attachments, `_require_course_membership(request, search_space_id)`, loads workspace (provides `class_name`, `subject_name`, `search_space_id`).
2. **Vision transcription** — if images: `_save_attachments` → `ai/vision.py:vision_transcribe` (model `VISION_MODEL`, default `gpt-4o-mini`; pytesseract fallback) → `ai/main_ai.py:extract_keywords(image_text)` distills it; result appended to the question as `q_effective` (image text truncated to 500 chars as fallback).
3. **Chat memory** — `_load_memory_and_append_user_turn` prepends `memory_context` so `q_effective = "{memory}\n\nCurrent question:\n{q}"`.
4. **Parallel parse + retrieval** — a 2-worker `ThreadPoolExecutor` runs `ai/main_ai.py:parse_question(q_effective)` (model `PARSER_MODEL`, default gpt-4o → `ParsedTask`) concurrently with step 5.
5. **Keyword extraction (query expansion)** — `server.py:_ask_pgvector` (line ~1433) calls `ai/main_ai.py:extract_and_filter_keywords` — ONE LLM call (concept_extraction prompt) returning ≤8 ranked textbook-index-style terms. Failure → empty keyword list (retrieval still runs on the raw question).
6. **Hybrid retrieval** — `retrieval/pipeline.py:retrieve_for_question`: builds `combined_query = question + " " + top-6 keywords` (keywords are appended hints, never substitutes), then `AITAHybridSearchRetriever.hybrid_search(combined_query, top_k=top_k*3)`: embeds the query (`indexing/document_embedder.py:embed_text`, `text-embedding-3-large` @ 3072 dims, LRU-cached), then runs two module-helper CTEs (`_build_semantic_cte` / `_build_keyword_cte` in `retrieval/hybrid_search.py`) over `aita_chunks` joined to `aita_documents` filtered by `document_visibility.active_document_conditions` (status `ready` + weekly notes/slides gated to `TeacherUpload.week <= current_week`). Each CTE places the `ORDER BY distance`/`ts_rank` + `LIMIT top_k*5` in an inner subquery (`semantic_candidates`/`keyword_candidates`) so the `rank() OVER (…)` window computes over only those ≤n rows; PostgreSQL then uses an exact top-N heapsort (measured 3,938 ms → 113 ms on the largest class, 5,844 chunks, EXPLAIN ANALYZE). The HNSW index (`idx_aita_chunks_embedding_hnsw`, codified in migration 023) deliberately does NOT engage — the document-visibility pre-filter blocks it without `hnsw.iterative_scan`; the scan stays exact so recall is unchanged; index engagement remains deferred. The final outer query FULL OUTER JOINs the two CTEs and scores `1/(60+sem_rank) + 1/(60+kw_rank)`. RRF fusion logic, output columns, and rank-direction semantics are unchanged.
7. **Reranking** — `retrieval/reranker.py:AITARerankerService.rerank(query, raw_chunks)` using the ORIGINAL question (not the keyword-expanded query). No-op unless `RERANKERS_ENABLED=true`.
8. **Store bias** — `retrieval/store_bias.py:apply_store_biases` adds per-kind boost (env/workspace overridable) producing `final_score`, re-sorts, then `pipeline.py` slices to `top_k`.
9. **Context packing with citation markers** — `retrieval/context_packer.py:pack_context`: greedy accumulation until 85% of token budget (tiktoken `cl100k_base`, fallback len/4), dedupe by chunk_id, builds markers like `[Textbook, p. 123]`, `[Slides, Week 3, p. 7]` (week included only for notes/slides), or `[<doc title ≤30 chars>]` when no page. Labels from `citations/formatter.py:DOC_TYPE_LABELS` keyed by material_kind; textbook uses `CITATION_LABEL` env (default "Textbook").
10. **Bundle assembly** — `server.py:_ask_pgvector` runs `context_packer._summarize_snippets` (regex equations/glossary/assumptions) and builds a `ResearchBundle` with `allowed_markers` = all snippet markers.
11. **Per-snippet scoring** — `ai/main_ai.py:solve_with_bundle`: parallel `_score_and_answer_snippet` calls (ThreadPool, `_citation_pool_size()`, default cap `CITATION_WORKERS`=24, model `CITATION_SCORER_MODEL` default gpt-4o) score each snippet; pool cap 24 > default snippet count (K_SEM=20) so all snippets score in one wave (was two waves at cap=12, measured 8.5s). `CITATION_SCORER_MODEL` is the only override knob — the legacy `PARSER_MODEL` fallback was removed from scorer model resolution (live A/B: gpt-4o median 2.54s/call vs mini 3.30s; gpt-4o is the default). Blended score `0.6*relevance + 0.4*directness`, multiplied by snippet importance; snippets below `CITATION_SCORE_FLOOR` (default 0.3) are dropped from the excerpts sent to the tutor.
12. **LLM answer** — `solve_with_bundle` sends the tutor system prompt (`ai/prompts/tutor.py`) + Task JSON + score-sorted SourceExcerpts to `MAIN_MODEL` (default `gpt-5`, `reasoning_effort=MAIN_REASONING_EFFORT` default `high`; non-gpt-5 models use temperature 0). Response is structured JSON: `{not_relevant, steps (single Markdown string), final_answers, equations_used, assumptions}`. `not_relevant=true` short-circuits to "This question is not relevant to the course scope." `final_answers` is forced to `{}` (conceptual-only mode — no numeric computation).
13. **Citation formatting** — `ai/main_ai.py:format_answer`: matches markers against the allowed set via regex `\[[^,\[\]]+,\s*p\.\s*[^\]]+\]`, strips unknown markers, rotates allowed markers onto any prose paragraph the LLM left uncited (code blocks/`$$`/headings exempt), and appends a `Citations: ...` trailer. Returns `FinalAnswer(text, citations)`.
14. **Response** — `server.py:_structured_citations_from_bundle` builds citation objects; answer + citations persisted as assistant chat turn and returned (JSON for `/ask`, SSE `answer` event for `/ask/stream`).

**Scope filtering — where it actually happens.** The pre-retrieval LLM relevance guard (`check_question_relevance`, full/partial/none, fail-open to "full") lives in `ai/orchestrator.py:Orchestrator._retrieve` — but that path is NOT used by the live `/ask` handlers (`server.py` contains no relevance call). In production, scope enforcement happens at answer time via the tutor prompt's RELEVANCE CHECK section and the `not_relevant` short-circuit in `solve_with_bundle` (step 12). Partial-relevance splitting (`RelevanceNote` injection in `solve_with_bundle`) only fires when `bundle.provenance.relevance_level == "partial"`, which only the Orchestrator path sets.

**Eval flow.** `Orchestrator.run` (`ai/orchestrator.py:831`) is the batch/eval entry: parse → `_retrieve` (with relevance guard + retry loop doubling token_budget/k on bundle-validation failure) → with `EVAL_MODE=true`, dumps the context pack to `EVAL_DUMP_PATH` (default `../system-upgraderrrr/context_packs/`) and skips the LLM.

## Key dependencies

What `ai/` + `retrieval/` import:
- `config/contracts.py` — all pipeline dataclasses; `config/settings.py` — subject name, citation label, `rerankers_enabled()`, `get_reranker_model()`, `RequestConfig`; `config/weights.py` — store-kind weight defaults and env parsing.
- `database/models.py` (`AITAChunk`, `AITADocument`, `TeacherCourse`, `TeacherUpload`) and `database/session.py` (`get_async_session`, `run_async`) — retrieval SQL.
- `indexing/document_embedder.py:embed_text` — query embedding (same model/dims as document indexing; do not let these drift).
- `citations/formatter.py:DOC_TYPE_LABELS` — marker labels per material kind.
- External: `openai` (all LLM calls are sync `client.chat.completions.create` with `response_format=json_object`), `tiktoken`, optional `rerankers`, `pint`, `pytesseract`, `numpy` (router + solver).

What imports these modules:
- `server.py` — the only production consumer: `vision_transcribe`, `parse_question`, `extract_keywords`, `solve_with_bundle`, `format_answer`, `retrieve_for_question` (via `_ask_pgvector`), `_summarize_snippets`. `Orchestrator` is imported at `server.py:25` but never constructed.
- `workspaces/db.py` — imports `retrieval.document_visibility.active_document_conditions`.
- `tests/` — `tests/router/*` (the only consumer of `ai/router/`), `tests/functions-tests/`, `tests/integration/`.

## Non-obvious conventions

- **Env vars** — models: `MAIN_MODEL` (default `gpt-5`), `MAIN_REASONING_EFFORT` (`high`), `PARSER_MODEL` (`gpt-4o`; drives parsing, scope-filter, and keyword calls — NOT scoring), `KEYWORD_MODEL` (`gpt-4o`; keyword extraction only — live A/B showed gpt-4o-mini ~2x slower), `CITATION_SCORER_MODEL` (`gpt-4o`; snippet scoring; no PARSER_MODEL fallback), `VISION_MODEL` (`gpt-4o-mini`), `VISION_ANSWER_MODEL`, `OPENAI_EMBEDDING_MODEL` (`text-embedding-3-large`), `EMBEDDING_DIM` (3072), `ROUTER_MODEL` (`gpt-4o-mini`). Tuning: `K_SEM` (20, server-side top_k), `TOKEN_BUDGET` (6000), `CITATION_SCORE_FLOOR` (0.3), `CITATION_WORKERS` (24, pool cap for snippet scoring — cap > K_SEM so all snippets score in one wave), `RERANKERS_ENABLED` (false), `RERANKER_MODEL` (`cross-encoder`), `RETRIEVAL_STORE_WEIGHT_{TEXTBOOK,SLIDES,NOTES,HOMEWORK,EXAMS,OTHER}`, `CITATION_LABEL` (`Textbook`), `TEXTBOOK_SUBJECT`, `GENERAL_FILTER_MODE` (`lenient`), `TERM_SEMANTIC_MODE` (`hybrid`), `RETRIEVAL_WIRE_LOG` (off; any other value prints `[Main AI -> ...]` wire lines that `/ask` scrapes from stdout into the response `logs` field), `EVAL_MODE`/`EVAL_DUMP_PATH`, debug: `QA_DEBUG`/`AI_TA_DEBUG`/`TRACE_IO`. Solver request options: `PROMPT_CACHE_KEY` (default `aita-solver:<model>`; OpenAI prefix-cache routing on both solve paths), `OPENAI_SERVICE_TIER` (env-gated; passed as `service_tier`), `MAIN_VERBOSITY` (env-gated; passed as `text.verbosity` on the streaming Responses path for reasoning models only).
- **Fusion invariants** (per repo CLAUDE.md, do not change without running the retrieval test suite): RRF constant 60; candidate pools of `top_k*5` per CTE; pipeline fetches `top_k*3` from hybrid search to give the reranker headroom; reranker scores against the original question while hybrid search uses the keyword-expanded query; store bias is additive and applied AFTER reranking; context packer uses only 85% of the token budget.
- **Keywords are hints, never substitutes** — `retrieve_for_question` appends ≤6 keywords to the original question so bad extraction can't kill retrieval. The original question always anchors semantic search.
- **Citation markers are the contract** between retrieval and answer formatting: created once in `pack_context`, carried on `BundleSnippet.citation_marker`, whitelisted in `bundle.allowed_markers`, enforced/rotated in `format_answer`. `BundleSnippet.validate()` rejects empty markers. Citations are non-negotiable (CLAUDE.md).
- **Fail-open philosophy** — every LLM helper (relevance guard, keyword extraction, synonym proposal, reranker load) catches all exceptions and degrades gracefully (guard → "full", keywords → `[]`, reranker → RRF order) rather than failing the request.
- **Sync-on-purpose** — `/ask` is a sync `def` endpoint (FastAPI auto-threads it); retrieval coroutines run via `database.session.run_async` on a shared background loop so asyncpg connections persist. Comment at `server.py:1523` warns not to convert to `async def`.
- **`WIRE` NameError latent bug** — `ai/main_ai.py:filter_general_terms` references `WIRE` (line ~682) but `WIRE` is only defined in `ai/orchestrator.py`; when wire logging would fire, the resulting NameError is swallowed by the enclosing try/except, silently returning unfiltered terms.
- **Debug artifacts** — pipeline writes `parsed_task.json`, `bundle.json`, `proof.json` to CWD (Orchestrator path) and `runtime/debug/{citations,miniresponses}.json`, `debug/main_ai_*.txt` (when debug flags set).
- **Week gating** — notes/slides chunks are invisible until the teacher's `current_week` reaches the upload's week (`document_visibility.active_document_conditions`); textbooks and non-weekly kinds are always visible once `status.state == "ready"`.

## Product context

Hoot is a citation-grounded teaching assistant: students ask questions (optionally with photos of problems) and get tutor-style answers built ONLY from teacher-uploaded course materials, with every factual claim cited back to a page. The pipeline's design priorities, in order: (1) never hallucinate beyond the excerpts (tutor prompt's source-boundedness + traceability check), (2) always cite (marker enforcement in `format_answer`), (3) respect teacher control (week gating, per-workspace material-kind weights), (4) degrade gracefully (fail-open guards) so students always get an answer attempt. Numeric computation is deliberately disabled (conceptual-only mode) — the assistant teaches concepts and procedures, it does not do students' homework arithmetic. The `ai/router/` two-stage router and retrieval-mode cache (NONE/AUGMENT/FRESH) are built and tested but not yet wired into the request path.
