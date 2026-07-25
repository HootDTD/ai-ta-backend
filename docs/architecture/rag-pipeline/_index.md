---
doc: ai-ta-backend/rag-pipeline/_index
description: Router for the Hoot /ask QA answer pipeline — retrieval, answer generation, prompts, retrieval-mode router.
owns: []
related: []
last_verified: 2026-07-25
stub: false
---

# rag-pipeline — the /ask QA answer pipeline

Canonical sequence: auth + workspace → vision transcription → chat memory →
retrieval-mode decision (NONE/AUGMENT/FRESH) → parse + keyword extraction →
hybrid RRF retrieval → rerank → store bias → context packing (citation markers) →
per-snippet scoring → tutor answer → citation formatting. **server.py is the only
production consumer; `_ask_pgvector` is the live handler** (the `Orchestrator` is
imported but never instantiated — see [orchestrator](orchestrator.md)).

## Retrieval-weights disambiguation
- **RRF rank-fusion (semantic + keyword), no per-arm weight** → [hybrid-search](hybrid-search.md) (`retrieval/hybrid_search.py`).
- **Post-fusion per-store-kind bias** → [store-bias](store-bias.md) (`retrieval/store_bias.py`).
- **The bias-weight config** → `platform/config-weights` (`config/weights.py`); teacher overrides enter via `server.py::_build_retrieval_weight_overrides` (`platform/http-server`).

## Retrieval
| Doc | One-liner · owns |
|---|---|
| [retrieve-pipeline](retrieve-pipeline.md) | single entry: hybrid→rerank→bias→pack · `retrieval/pipeline.py` |
| [hybrid-search](hybrid-search.md) | pgvector+FTS RRF fusion; HNSW halfvec contract · `retrieval/hybrid_search.py` |
| [reranker](reranker.md) | optional cross-encoder, fail-open · `retrieval/reranker.py` |
| [store-bias](store-bias.md) | additive per-store-kind bias · `retrieval/store_bias.py` |
| [context-packer](context-packer.md) | token-budget packing + citation markers · `retrieval/context_packer.py` |
| [document-visibility](document-visibility.md) | week-gated visible-doc predicate (shared) · `retrieval/document_visibility.py` |
| [citations-formatter](citations-formatter.md) | response structured citations + DOC_TYPE_LABELS · `citations/formatter.py` |

## Answer generation
| Doc | One-liner · owns |
|---|---|
| [main-ai](main-ai.md) | the LLM brain (parse/keyword/score/answer/format) · `ai/main_ai.py` |
| [orchestrator](orchestrator.md) | legacy/eval state machine, NOT live · `ai/orchestrator.py` |
| [vision](vision.md) | image transcription, fail-open · `ai/vision.py` |
| [solver](solver.md) | run_python sandbox, dormant · `ai/solver.py` |
| [streaming](streaming.md) | JsonStringFieldStreamer for SSE · `ai/streaming.py` |

## Prompts
| Doc | One-liner · owns |
|---|---|
| [prompts-keyword](prompts-keyword.md) | keyword-stage catalog + prompts barrel · `ai/prompts/` (6 + __init__) |
| [prompts-parse-relevance](prompts-parse-relevance.md) | parse + relevance-guard prompts · `parse_question.py`, `relevance_guard.py` |
| [prompts-answer](prompts-answer.md) | tutor + snippet-scorer prompts · `tutor.py`, `score_and_answer_snippet.py` |

## Retrieval-mode router
| Doc | One-liner · owns |
|---|---|
| [router-mode](router-mode.md) | decide_retrieval_mode (always-on) · `ai/router/mode.py` |
| [router-wiring](router-wiring.md) | server glue + session-cache integration · `ai/router/wiring.py` |
| [router-llm](router-llm.md) | strict-JSON stage-2 classifier · `ai/router/llm_router.py` |
| [router-deferred](router-deferred.md) | dormant embedding router (4 files) · `ai/router/embedding_router.py` … |
