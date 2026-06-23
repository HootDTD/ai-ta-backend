---
doc: shared/README
description: Umbrella navigation map for the Hoot AI-TA doc tree
owns: []
related: []
last_verified: 2026-06-10
stub: false
---

# Hoot AI-TA Architecture Docs — Umbrella Navigation

This file maps the doc tree across all Hoot sub-repos. It is committed to
`ai-ta-backend` because that's the natural host for cross-repo content.

**Rule: navigate docs first, code second.** Never read a source file to
understand the *system* — that's what these docs are for. Read code only to
make the change.

> **`_archive/` is transient — skip it.** `ai-ta-backend/docs/_archive/` holds
> handoffs, plans, specs, and experiment writeups. Do **not** load anything there
> unless you are resuming a specific task whose handoff/plan lives in it. It is
> not part of the architecture and is intentionally excluded from this map.

## In-scope repos (doc tree active)

### ai-ta-backend (Python/FastAPI backend)
- `ai-ta-backend/docs/architecture/_overview.md` — app bootstrap, HTTP surface, auth, config, vendor clients, and ops entrypoints
- `ai-ta-backend/docs/architecture/rag-pipeline.md` — QA answer pipeline: vision transcription, keyword extraction, pgvector+FTS hybrid retrieval with RRF, reranking, store bias, token-budget context packing, citation-disciplined answers (`ai/`, `retrieval/`)
- `ai-ta-backend/docs/architecture/indexing.md` — document ingestion: layout-aware PDF extraction, OpenAI embeddings, OCR fallbacks, pgvector persistence (`indexing/`, `indexers/`, `ocr/`)
- `ai-ta-backend/docs/architecture/apollo.md` — Apollo "student teaches the tutor" subsystem: GPT-4o utterance parsing into a typed Neo4j knowledge graph, confused-learner persona, Done-time diff grading (coverage + rubric + XP) (`apollo/`)
- `ai-ta-backend/docs/architecture/domain-data.md` — SQLAlchemy async models and session management, chat session/turn persistence with memory summarization, knowledge store CRUD, AI-use PDF reports (`database/`, `chats/`, `knowledge/`, `reports/`)

### ai-ta-student-ui (student web app — Next.js 15 App Router)
- `ai-ta-student-ui/docs/architecture/_overview.md` — config, entry layout, env vars, backend proxy pattern, Supabase auth client, shared lib utilities
- `ai-ta-student-ui/docs/architecture/pages.md` — all routes: Hoot chat home, invite-link join flow, Apollo teaching session, AI-use report viewer, `/api` proxy layer
- `ai-ta-student-ui/docs/architecture/components.md` — CitationChip, SpecialCharsPalette, and the `components/apollo/` subtree (chat, KG panel, negotiation pills, report, progress, done-gate)

### ai-ta-teacher-ui (teacher console — Next.js 15 App Router)
- `ai-ta-teacher-ui/docs/architecture/_overview.md` — config, entry layout, env vars, Supabase auth helper, BFF proxy pattern to the backend
- `ai-ta-teacher-ui/docs/architecture/pages.md` — teacher console, invite join flow, AI-use report viewer, `app/api/**` proxy routes

## Cross-repo shared content

- `ai-ta-backend/docs/shared-architecture/conventions.md` — coding, testing, CI, and branching conventions across all three repos
- `ai-ta-backend/docs/shared-architecture/security.md` — auth flow, membership/tenant enforcement, RLS posture, secrets rules
- `ai-ta-backend/docs/shared-architecture/supabase.md` — Supabase projects, schema map, pgvector/halfvec HNSW setup, migration workflow
- `ai-ta-backend/docs/shared-architecture/product-context.md` — what Hoot is, user roles, Apollo pedagogy, domain glossary, product invariants

## Active working docs (not architecture, but load-bearing)

- `ai-ta-backend/docs/apollo-redesign.md` — Apollo V3 gap analysis × academic literature map; source of the Class 1/2/3 fix taxonomy
- `ai-ta-backend/docs/claude_v3_checklist.md` — Apollo V3 flaws checklist; skip-marked tests reference its item numbers
- `ai-ta-backend/docs/DATA-FLOW.md` — end-to-end system data flow reference
- `ai-ta-backend/docs/TESTING-CI-PLAN.md` — testing/CI strategy and phase plan

## Doc format contract

Every architecture doc carries YAML frontmatter:

- `doc:` — its id (used in `related:` links)
- `owns:` — repo-relative globs for the source files the doc is the authority on
- `related:` — other doc ids worth loading alongside it
- `last_verified:` — bump this whenever you reconcile the doc against code
- `stub:` — `true` means the doc is a placeholder; don't trust it as ground truth

**Drift-prevention contract:** before editing a source file, load the doc that
owns it (check `owns:` globs). After editing code, reconcile the owner doc in
the same commit — update interfaces, flows, conventions that drifted, and bump
`last_verified`.
