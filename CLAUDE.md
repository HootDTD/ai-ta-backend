# AI-TA Backend

Python/FastAPI backend powering Hoot's intelligent tutoring system. Combines RAG, hybrid search (pgvector + FTS5), and LLMs to deliver citation-backed answers grounded in course materials.

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
4. Hybrid retrieval: pgvector semantic + SQLite FTS5 lexical + query expansion
5. Reranking: importance scoring, relevance thresholding, duplicate removal
6. Context packing: snippet assembly, citation markers, token budget management
7. LLM generates answer with citation markers [S1], [S2], etc.
8. Citations formatted and returned

## Key Tech Decisions
- Vector search: pgvector (PostgreSQL) + FAISS. Do not introduce other vector stores.
- LLM: GPT-4o via MAIN_MODEL env var. Vision fallback: Tesseract if GPT-4V unavailable.
- Database: Supabase PostgreSQL + pgvector, async via SQLAlchemy + asyncpg
- Deployment: Railway (GitHub integration, deploys `ApolloV3`; Procfile defines web + worker processes). Heroku is abandoned — do not re-wire it.

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
- All new features must include unit tests using Supabase mock fixtures (see conftest.py)
- Use comprehensive debug logging for any pipeline stage
- Return structured JSON responses from the LLM layer — never raw text

## What NOT To Do
- Never modify .env files or commit secrets
- Never push directly to main — always use a feature branch
- Never bypass the semantic filter — scope enforcement is a core product requirement
- Never install new packages without confirming with me first
- Never remove or bypass citation marker generation — citations are non-negotiable
- Do not change the hybrid search fusion logic without running the full retrieval test suite first
