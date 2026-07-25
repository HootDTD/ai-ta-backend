---
doc: ai-ta-backend/platform/workspaces
description: workspaces/manager.py + db.py — course-workspace resolution (identifier → scoped materials + bias) with a TTL cache over the app.courses/app.documents tables
owns:
  - workspaces/manager.py
  - workspaces/db.py
  - workspaces/__init__.py
related:
  - ai-ta-backend/platform/http-server
  - ai-ta-backend/platform/config-weights
  - ai-ta-backend/database/models
  - ai-ta-backend/rag-pipeline/document-visibility
last_verified: 2026-07-25
stub: false
---

# platform/workspaces — course-workspace resolution

Maps an incoming class identifier to that course's retrieval scope (materials +
bias). Two substantive, tightly-coupled files (`__init__.py` is empty glue).

## Interface

- `WorkspaceMaterial` (frozen) — one retrievable material (id, kind, title,
  `index_path`, priority, optional `weight_override`).
- `ClassWorkspace` (frozen) — resolved scope with `sorted_materials()`
  (priority desc, then title) and `doc_sets()` (ordered index dirs).
- Errors `WorkspaceError` / `WorkspaceNotFound` / `WorkspaceConfigError`.
- `WorkspaceRepository` interface; `StaticWorkspaceRepository` (in-memory legacy
  fallback); `DBWorkspaceRepository` (db.py) — the primary repo.
- `WorkspaceManager` — TTL cache (`CLASS_WORKSPACE_CACHE_TTL`, default 300s)
  keyed by identifier + aliases; `get(identifier)`.
- `build_workspace_manager(static_config=None)` — factory: `DBWorkspaceRepository`
  primary + optional static fallback.

## Data flow

`DBWorkspaceRepository.load_workspace` bridges through `run_async` →
`_find_course` resolves the identifier as slug → case-insensitive name → integer
id against `app.courses`; `_load_materials` loads visible `app.documents` via
`active_document_conditions` (`rag-pipeline/document-visibility`), pulling the
course's `retrieval_weights` and current week off the merged row.

## Invariants & gotchas

- The cache stores the resolved workspace under all of its aliases
  (class name / slug / id, lowercased) so any identifier form is a hit.
- A `WorkspaceNotFound` from the primary falls back to the static repo; any other
  `WorkspaceError` propagates unless a fallback resolves it.
- `index_path` is `Path("")` on the pgvector path (no FAISS dirs) — callers must
  ignore it when pgvector is on.

## Env flags

`CLASS_WORKSPACE_CACHE_TTL`, `CLASS_INDEX_ROOT` (static-repo index root).

## Related

`http-server` (`_get_workspace_manager` / `/ask` resolution), `config-weights`
(override normalization), `database/models` (`Course`/`Document`),
`rag-pipeline/document-visibility` (the shared visibility predicate).
