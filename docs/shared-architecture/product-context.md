---
doc: shared/product-context
description: What Hoot is, its two user roles and UIs, the Apollo learning-by-teaching mode, the domain glossary, and the product invariants
owns: []
related:
  - ai-ta-backend/_overview
  - shared/security
last_verified: 2026-06-10
stub: false
---

# Product Context

## What Hoot is

Hoot is **NotebookLM for education**: a RAG-based AI teaching assistant. Teachers upload course materials (PDFs of textbooks, slides, notes); students ask questions in a chat UI and receive **citation-backed answers scoped strictly to that course's materials**. The system combines hybrid search (pgvector semantic + PostgreSQL full-text), LLM answer generation (GPT-4o family), and strict source-binding rules so the tutor never answers from background knowledge — if the materials don't cover something, it says so rather than fabricate (`ai-ta-backend/docs/DATA-FLOW.md` §11).

The platform is three tiers (`ai-ta-backend/docs/DATA-FLOW.md` System Overview):

- **ai-ta-backend** — FastAPI (:8000): ~19 REST endpoints, SSE streaming for `/ask/stream`, a background upload worker, Supabase PostgreSQL + pgvector + Storage, OpenAI for LLM/embedding/vision, Neo4j for Apollo knowledge graphs.
- **ai-ta-student-ui** — Next.js (:3001): chat with streaming answers, image attachments, citation preview chips, join-via-invite-link, AI use reports, and the Apollo mode (`ai-ta-student-ui/app/apollo/`).
- **ai-ta-teacher-ui** — Next.js (:3002): upload course materials by week and kind, set the active week, tune retrieval weights, generate invite links, AI use reports.

Both UIs talk to the backend exclusively through Next.js API-route proxies that forward the Supabase JWT.

## The two user roles

Role lives on `course_memberships.role` (`student` | `teacher`), keyed to Supabase `auth.users` and a course. Backend `auth.py` validates the JWT and enforces membership on every request; Row-Level Security mirrors this in the database.

- **Students** join a course by redeeming an invite link, ask questions (optionally with images, transcribed by the vision model), see answers with citation chips, can only access materials for weeks `<= current_week`, and can generate an AI use report documenting their AI interaction.
- **Teachers** create and configure a course: upload PDFs per week (1–16) and kind (notes/slides), set the current week (gating what students can see), tune per-material-kind retrieval weight boosts within bounds, and mint role-scoped invite links (code, max uses, expiry).

## The QA pipeline (student experience)

A student question flows through (`ai-ta-backend/CLAUDE.md`, DATA-FLOW §4): auth + membership check → image transcription → normalization → **semantic relevance filter** (out-of-scope questions are rejected) → keyword extraction and question parsing into a structured `ParsedTask` → hybrid retrieval (pgvector cosine + FTS, fused by Reciprocal Rank Fusion) → teacher-configured store bias → context packing into a token budget with citation markers assigned → GPT-4o generates the answer citing only allowed markers → citations extracted and returned over SSE as status events then a final answer + citations payload. Turns are persisted; conversations of 12+ turns get an LLM memory summary that feeds later context.

## Apollo: learning-by-teaching mode

Apollo (`ai-ta-backend/apollo/`, `ai-ta-student-ui/app/apollo/`) inverts the tutor relationship: the **student teaches a deliberately "ignorant" agent** how to solve a problem, per `ai-ta-backend/docs/apollo-redesign.md` Part 1:

- Each student utterance is parsed by an LLM (GPT-4o, JSON, temp 0) into typed **Nodes and Edges** written to a **per-attempt knowledge-graph subgraph in Neo4j** (6 node types; 4 edge types: PRECEDES, USES, DEPENDS_ON, SCOPES).
- Apollo's replies are drafted under a strict-ignorance system prompt, then pass an **output filter** that rejects domain terms the student hasn't introduced (allowlisted by the student's history + KG content).
- When the student clicks **Done**, the KG is frozen; a reference graph derived from the problem JSON drives **coverage** (LLM-as-judge calls per equation/condition/simplification/procedure step), a deterministic **rubric** aggregation (60/25/15 procedure/justification/simplification), a narrated **diagnostic**, and a SymPy **forward-chain solve** of the student's own equations.
- The student UI shows the live KG panel, problem panel, chat, a post-Done report panel, and a student progress card (levels/avatar) — see `ai-ta-student-ui/app/apollo/ApolloPageClient.tsx`. Sessions support end, retry, and negotiation actions (paraphrase, challenge, skip) per `ai-ta-backend/apollo/api.py`.
- Failures are surfaced as named structured errors (`FilterRejectedError`, `SessionFrozenError`, `PoolExhaustedError`, ...) — no silent fallback.

## Domain glossary

| Term | Meaning |
|---|---|
| **Course / search space** | One class. Stored as `aita_search_spaces` (name, slug, subject, `weight_overrides`); "search_space_id" is the course key throughout the API. Paired 1:1 with `teacher_courses` (current_week, weights, weight_bounds). |
| **Course membership** | Row linking a Supabase `auth.users` user to a search space with role `student` or `teacher`. |
| **Invite link** | Teacher-minted code (`course_invite_links`) with role, max uses, and expiry; redeemed by students at `/join/{code}` to create a membership. |
| **Knowledge store** | The course's indexed material corpus, managed by `ai-ta-backend/knowledge/` (knowledge store CRUD and organization). |
| **Document / chunk** | `aita_documents` (one uploaded material: content, dedup hashes, `vector(3072)` embedding) and its `aita_chunks` (page number, `section_path` like "Ch3 > 3.2", `chunk_type` body/heading/figure, embedding). |
| **Teacher upload / upload job** | `teacher_uploads` tracks an uploaded file (week, kind, status queued→processing→ready/failed/superseded); `teacher_upload_jobs` is the durable work queue with worker leases that the background worker drains. |
| **Current week** | Teacher-set pointer (1–16) gating which weeks' materials students can access. |
| **Chat session / chat turn** | `chat_sessions` (one conversation, with `memory_summary`) and ordered `chat_turns` (role, content, model, image attachments). |
| **Memory summary** | LLM summary of a 12+-turn conversation, truncated to 3000 chars, prepended to future context. |
| **Citation marker / citation** | The marker assigned to each packed snippet (`[Textbook, p. 42]`); the answer may cite only `allowed_markers`, and structured citations (label, file, page, OCR confidence) are returned alongside the answer and rendered as chips. |
| **Semantic filter / relevance check** | The LLM scope gate classifying a question as full/partial/none relevance to the course; out-of-scope questions are rejected. |
| **ResearchBundle / BundleSnippet** | Retrieval output contract: packed snippets with scores and markers, plus metadata, `allowed_markers`, found/not-found terms, warnings, coverage gaps. |
| **Store bias / retrieval weights** | Per-material-kind score boosts (textbook +0.12 default, etc.), teacher-tunable within `weight_bounds`. |
| **Apollo session / attempt** | One learning-by-teaching run against one problem; has phases, a freeze on Done, retry, and end (`apollo/handlers/lifecycle.py`). |
| **Apollo KG** | The per-attempt Neo4j subgraph of typed nodes/edges built from parsed student utterances. |
| **Coverage / rubric / diagnostic** | The Done-time grading stack: LLM-judged coverage vs. the reference graph, deterministic rubric weights, narrated diagnostic, plus a SymPy solve of the student's equations. |
| **AI use report** | Student-generated GPT-4o-mini report (markdown + JSON-LD, with model fingerprint and prompt hashes) documenting their AI interaction; exportable as PDF/JSON/Markdown. |

## Key product invariants

1. **Scope enforcement** — answers come only from the course's own materials. The semantic filter is never bypassed, retrieval is always filtered by `search_space_id` and `status = ready`, and week gating limits students to released material.
2. **Citations are non-negotiable** — every factual claim carries an exact citation marker from `allowed_markers`; no background knowledge, no generalizing from special cases; insufficient sources yield "the provided materials do not cover this," never fabrication (DATA-FLOW §11).
3. **Membership before access** — every request resolves the Supabase JWT and checks course membership (auto-enroll only via explicit env whitelist); RLS backs this at the database layer.
4. **Structured outputs everywhere** — the LLM layer returns schema-shaped JSON (parsed questions, citations, Apollo nodes/edges, reports), never free text the system must guess at.
5. **Apollo fails loudly** — named structured errors surface to the UI; no fallback behavior masks a broken grading or parsing step.
