# AI-TA Backend

Python/FastAPI backend powering Hoot's intelligent tutoring system. Combines RAG, hybrid search (pgvector + FTS5), and LLMs to deliver citation-backed answers grounded in course materials.

> This repo is part of the Hoot AI-TA workspace. If a workspace-level
> `CLAUDE.md` wasn't loaded (session opened inside this repo), read
> `docs/shared-architecture/README.md` for the full cross-repo doc map.

## Doc tree — navigate docs first, code second

`docs/architecture/` describes this repo's code; each doc declares `owns:`
globs in its frontmatter and is the authority on those files:

- `docs/architecture/_overview.md` — bootstrap, HTTP surface, auth, config, vendors, ops entrypoints
- `docs/architecture/rag-pipeline.md` — `ai/` + `retrieval/` (QA pipeline)
- `docs/architecture/indexing.md` — `indexing/` + `ocr/` (ingestion)
- `docs/architecture/apollo.md` — `apollo/` (learning-by-teaching subsystem)
- `docs/architecture/domain-data.md` — `database/` + `chats/` + `knowledge/` + `reports/`

Cross-repo shared docs live in `docs/shared-architecture/` (conventions,
security, supabase, product-context, README navigation map).

**Drift contract:** before editing a source file, load its owner doc. After
editing code, update the owner doc in the same commit and bump
`last_verified`. Stale docs are worse than no docs.

## Architecture

- **Orchestrator** (`ai/orchestrator.py`): Coordinates the QA pipeline
- **Main AI** (`ai/main_ai.py`): LLM-based QA with structured JSON responses
- **Retrieval** (`retrieval/`): Hybrid search → query expansion → reranking → context packing
- **Indexing** (`indexing/`): Layout-aware PDF extraction, multimodal embeddings, FAISS + SQLite
- **Vision** (`ai/vision.py`): Image transcription (GPT-4V or Tesseract fallback)
- **Knowledge** (`knowledge/`): Knowledge store CRUD and organization
- **Reports** (`reports/`): PDF generation for teacher analytics
- **Auth** (`auth.py`): Supabase JWT validation and course membership enforcement
- **Chats** (`chats/`): Chat session/turn management with memory summarization
- **Database** (`database/`): SQLAlchemy async models, session management, migrations

## QA Pipeline (Data Flow)
1. Student submits question + optional images
2. Vision module transcribes images; keywords extracted
3. Semantic filter removes out-of-scope questions
4. Retrieval-mode orchestrator (always on): classifies each turn
   NONE / AUGMENT / FRESH against the session's cached bundle — NONE answers from
   cache (skips steps 5–7 and snippet scoring), AUGMENT runs a reduced top-up merged
   with the cache, FRESH is the full pipeline. Fails open to FRESH.
   Details: `docs/architecture/rag-pipeline.md` (step 3a).
5. Hybrid retrieval: pgvector semantic + PostgreSQL FTS lexical + query expansion
6. Reranking: importance scoring, relevance thresholding, duplicate removal
7. Context packing: snippet assembly, citation markers, token budget management
8. LLM generates answer with citation markers like [Textbook, p. 123]
9. Citations formatted and returned

## Key Tech Decisions
- Vector search: pgvector (PostgreSQL) + FAISS. Do not introduce other vector stores.
- LLM: the solver model is pinned in `config/models.py` (`MAIN_MODEL = gpt-5.1`, `MAIN_REASONING_EFFORT = low`) — changing it is a code change + deploy, not an env var. Vision fallback: Tesseract if GPT-4V unavailable.
- Database: Supabase PostgreSQL + pgvector, async via SQLAlchemy + asyncpg
- Deployment: Railway (GitHub integration; prod deploys `main`, staging deploys `staging`; Procfile defines web + worker processes). Heroku is abandoned — do not re-wire it.

## Environment
Config via `config/settings.py`. Copy `.env.example` to `.env` for required variables.

## Common Commands
```bash
pip install -r requirements.txt
python server.py                    # Start dev server (port 8000)
pytest tests/ -v --tb=short         # Run all tests
pytest tests/test_main_ai.py -v     # Run specific test module
```

## Coding Standards
- Keep each retrieval pipeline stage independent and independently testable
- All new features must include unit tests using the shared conftest harness (env fixtures + Testcontainers Postgres for DB paths; see tests/conftest.py)
- Use comprehensive debug logging for any pipeline stage
- Return structured JSON responses from the LLM layer — never raw text

## What NOT To Do
- Never modify .env files or commit secrets
- Never push directly to main — always use a feature branch. `main` is the pilot release branch: it moves only via staging→main promotion PRs and `hotfix/*` PRs (see `docs/branching.md`)
- Never bypass the semantic filter — scope enforcement is a core product requirement
- Never install new packages without confirming with me first
- Never remove or bypass citation marker generation — citations are non-negotiable
- Do not change the hybrid search fusion logic without running the full retrieval test suite first
