---
doc: platform/config-settings
description: config/settings.py — per-request runtime configuration plus backend env-flag getters.
owns:
  - config/settings.py
  - config/__init__.py
related:
  - platform/http-server
  - rag-pipeline/hybrid-search
last_verified: 2026-07-28
stub: false
---

# platform/config-settings — runtime config + env getters

The single authority on these env-flag getters; `retrieval/`, `apollo/`, and
`indexing/` import them broadly and reference this doc rather than re-reading
`os.getenv`. `config/__init__.py` is empty glue.

## Interface

- `RequestConfig` dataclass + `from_env()` + `set_subject()` — the thread-safe
  per-request replacement for module globals; the HTTP server builds one per
  `/ask` so concurrent requests never share subject state.
- Subject-name precedence chain `default < meta < env < cli/server`:
  `set_subject_name` / `get_subject_name` / `get_subject_source` /
  `get_subject_priority` (module-global helpers retained for CLI callers).
- `get_citation_label()` (default `"Textbook"`), `get_runtime_dir()`
  (`RUNTIME_DIR` override, defaults to repo-root `./runtime`).
- pgvector/embedding: `use_pgvector_retrieval()`, `get_embedding_dim()`
  (default 3072), `get_embedding_model()` (default `text-embedding-3-large`),
  `get_supabase_db_url()`.
- Neo4j: `get_neo4j_uri/username/password/database()` + `neo4j_configured()`
  (True only when all four vars are set).
- Reranker: `rerankers_enabled()`, `get_reranker_model()`.
- Apollo grounding: `interaction1_enabled()` reads `INTERACTION1`, default off;
  gates whether a session's grounding bundle is BUILT
  (`apollo/conversation/session-init`).
- Grounding: `interaction2_enabled()` — default OFF; gates whether the Apollo
  grading path consumes a session's course-grounding bundle
  (`apollo/conversation/handlers/done`). Independent of `INTERACTION1`, which
  only gates whether that bundle is BUILT.
- Apollo concept scope: `interaction_concepts()` parses `INTERACTION_CONCEPTS`
  into a normalized concept-slug allowlist, and
  `interaction_allowed_for_concept(slug)` applies it to interaction features.
- Apollo remediation: `interaction3_enabled()` reads `INTERACTION3`, default off.
- Apollo Hoot-assist grading cap: `interaction5_enabled()` reads `INTERACTION5`,
  default off; gates whether the grading path credit-caps rubric nodes a Hoot
  lookup aside explained for the student (`apollo/conversation/handlers/done`).
  Same truthy parsing as `interaction1/2/3`.

## Data flow

Getters read `os.getenv` with typed defaults; `RequestConfig.from_env()` seeds
subject/citation/runtime-dir once per request. `_WIRE` (from `RETRIEVAL_WIRE_LOG`)
gates a one-time subject log line.

## Invariants & gotchas

- Prefer `RequestConfig` over the module-global subject state for anything
  request-scoped — the globals are process-wide and not concurrency-safe.
- `get_embedding_dim()` must match the model used at index time (halfvec HNSW
  index dimensionality); mismatches break retrieval.
- `INTERACTION_CONCEPTS` is comma-separated, with each slug stripped and
  casefolded. Unset, empty, and whitespace-only values produce an empty
  allowlist, which means no concept restriction and preserves existing behavior.

## Env flags

`RETRIEVAL_WIRE_LOG`, `RUNTIME_DIR`, `TEXTBOOK_SUBJECT`, `CITATION_LABEL`,
`USE_PGVECTOR_RETRIEVAL`, `EMBEDDING_DIM`, `OPENAI_EMBEDDING_MODEL`,
`SUPABASE_DB_URL`, `NEO4J_URI`/`NEO4J_USERNAME`/`NEO4J_PASSWORD`/`NEO4J_DATABASE`,
`RERANKERS_ENABLED`, `RERANKER_MODEL`, `INTERACTION1`, `INTERACTION2`, `INTERACTION3`,
`INTERACTION5`, `INTERACTION_CONCEPTS`.

## Related

`http-server` (builds a `RequestConfig` per `/ask`); `rag-pipeline/hybrid-search`
(consumes embedding + Neo4j getters).
