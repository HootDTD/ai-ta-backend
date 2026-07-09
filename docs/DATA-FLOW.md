# AI-TA Platform: Data Flow & Architecture

> Complete visualization of how data moves, where it's stored, and how it's used across the three-tier AI tutoring platform.

---

## System Overview

```
+-------------------------------+       +-------------------------------+
|     Student UI (Next.js)      |       |     Teacher UI (Next.js)      |
|         :3001                 |       |         :3002                 |
|  - Chat interface             |       |  - Upload course materials    |
|  - Image attachments          |       |  - Set active week            |
|  - Citation previews          |       |  - Tune retrieval weights     |
|  - AI use reports             |       |  - Generate invite links      |
|  - Join via invite link       |       |  - AI use reports             |
+----------|--------------------+       +----------|--------------------+
           |  Next.js API routes                   |  Next.js API routes
           |  (proxy layer)                        |  (proxy layer)
           +------------------+   +----------------+
                              |   |
                              v   v
                    +----------------------------+
                    |   FastAPI Backend (:8000)   |
                    |                            |
                    |  19 REST endpoints          |
                    |  SSE streaming (/ask/stream)|
                    |  Background upload worker   |
                    +---------|------------------+
                              |
              +---------------+---------------+
              |               |               |
              v               v               v
    +----------------+ +------------+ +----------------+
    | Supabase       | | OpenAI API | | Supabase       |
    | PostgreSQL     | |            | | Storage        |
    | + pgvector     | | - GPT-4o   | | (file buckets) |
    | + RLS          | | - Embed    | |                |
    | + FTS          | | - Vision   | |                |
    +----------------+ +------------+ +----------------+
```

---

## 1. Database Schema Map

### Tables & Relationships

```
auth.users (Supabase managed)
  |
  |-- 1:N --> course_memberships
  |             |-- user_id (UUID FK)
  |             |-- search_space_id (INT FK) ---> aita_search_spaces
  |             |-- role: 'student' | 'teacher'
  |
  |-- 1:N --> course_invite_links
  |             |-- created_by (UUID FK)
  |             |-- search_space_id (INT FK) ---> aita_search_spaces
  |             |-- code (TEXT UNIQUE)
  |             |-- role, max_uses, use_count, expires_at, is_active
  |
  |-- 1:N --> chat_sessions
  |             |-- user_id (UUID FK)
  |             |-- search_space_id (INT FK) ---> aita_search_spaces
  |             |-- chat_id (TEXT UNIQUE)
  |             |-- memory_summary (TEXT)       <-- LLM-generated summary
  |             |-- meta (JSONB)
  |             |
  |             |-- 1:N --> chat_turns
  |                          |-- turn_index (INT)
  |                          |-- role: user | assistant | tool | system
  |                          |-- content (TEXT)
  |                          |-- model (TEXT)         <-- which GPT model
  |                          |-- attachments (JSONB)  <-- base64 images
  |
  |-- 1:N --> teacher_uploads
                |-- uploaded_by (UUID FK)
                |-- search_space_id (INT FK) ---> aita_search_spaces
                |-- week (1-16), kind (notes|slides)
                |-- status: queued | processing | ready | failed | superseded
                |-- doc_id (FK) --> aita_documents
                |-- storage_key, ocr_provider, ocr_summary (JSONB)
                |-- artifact_manifest (JSONB)

aita_search_spaces (one per course/class)
  |-- id (SERIAL PK)
  |-- name, slug (UNIQUE), subject_name
  |-- weight_overrides (JSONB)     <-- per-course retrieval bias config
  |-- metadata (JSONB)
  |
  |-- 1:1 --> teacher_courses
  |             |-- current_week (1-16)
  |             |-- weights (JSONB)
  |             |-- weight_bounds (JSONB: {min, max})
  |
  |-- 1:N --> aita_documents
                |-- title, material_kind
                |-- content (TEXT), source_markdown
                |-- content_hash (UNIQUE)          <-- dedup key #1
                |-- unique_identifier_hash          <-- dedup key #2
                |-- embedding (vector(3072))        <-- OpenAI text-embedding-3-large
                |-- page_count, week, status (JSONB)
                |-- document_metadata (JSONB)
                |
                |-- 1:N --> aita_chunks
                             |-- content (TEXT)
                             |-- embedding (vector(3072))
                             |-- page_number (INT)
                             |-- section_path (TEXT)  <-- "Ch3 > 3.2 > Normal Shocks"
                             |-- chunk_type: body | heading | figure
                             |-- figure_id (TEXT)

teacher_upload_jobs (durable work queue)
  |-- upload_id (FK) --> teacher_uploads
  |-- state: queued | processing | completed | failed
  |-- lease_owner (TEXT)        <-- worker identity for distributed locking
  |-- lease_expires_at          <-- 5-min lease window
  |-- attempt_count, last_error
```

### Row-Level Security (RLS)

```
course_memberships  --> user sees only own rows (user_id = auth.uid())
chat_sessions       --> user owns their chats (user_id = auth.uid())
chat_turns          --> user accesses turns in their own sessions
teacher_courses     --> only teachers for that search_space
teacher_uploads     --> only teachers for that search_space
course_invite_links --> only teachers for that search_space
```

---

## 2. Authentication Flow

```
                     Student/Teacher UI
                            |
                 +----------+----------+
                 |                     |
            [Sign Up]            [Sign In]
                 |                     |
                 v                     v
   POST /auth/v1/signup    POST /auth/v1/token
   {email, password}       ?grant_type=password
                 |                     |
                 v                     v
         Supabase Auth         Supabase Auth
         (may require          (returns JWT)
          email confirm)              |
                                      v
                             StoredSession {
                               access_token,
                               refresh_token,
                               expires_at,
                               user_id,
                               user_email
                             }
                                      |
                          +-----------+----------+
                          |                      |
                   localStorage              Every API call:
                   key: "hoot_auth_           Authorization:
                    session_v1"               Bearer {token}
                          |                      |
                    On page load:                v
                    ensureActive           Backend auth.py
                    Session()              resolve_auth_context()
                          |                      |
                    if <30s to              Token cache
                    expiry:                 (60s TTL, hashed)
                          |                      |
                          v                 Cache miss:
                   POST /auth/v1/           GET /auth/v1/user
                   token?grant_type=        validate with
                   refresh_token            Supabase
```

### Auto-Enrollment

```
Student hits /ask with valid token but no course_membership
                    |
                    v
        AUTO_ENROLL_STUDENT_MEMBERSHIP=1?
                    |
            +-------+-------+
            |               |
           yes              no
            |               |
            v               v
    search_space_id      HTTP 403
    in whitelist?        "Not enrolled"
            |
     +------+------+
     |             |
    yes            no
     |             |
     v             v
  Create         HTTP 403
  membership
  (student role)
```

---

## 3. Document Ingestion Pipeline

### Teacher Upload Flow

```
Teacher UI                    Backend                          Database/Storage
    |                            |                                  |
    |  POST /teacher/upload      |                                  |
    |  FormData: {               |                                  |
    |    search_space_id,        |                                  |
    |    week, kind,             |                                  |
    |    title, file (PDF)       |                                  |
    |  }                         |                                  |
    +--------------------------->|                                  |
    |                            |  1. Verify teacher role          |
    |                            |  2. Save file to temp (UUID)     |
    |                            |  3. Create TeacherUpload         |
    |                            |     status='queued'              |
    |                            |----------------------------------+
    |                            |  4. Create TeacherUploadJob      |
    |                            |     state='queued'               |
    |  HTTP 202 Accepted         |----------------------------------+
    |<---------------------------+                                  |
    |                            |                                  |
    |  (polls /teacher/weeks     |                                  |
    |   every 4 seconds)         |                                  |
    |                            |                                  |
    |                    +-------+--------+                         |
    |                    | Upload Worker  |  (background loop)      |
    |                    | (separate proc)|                         |
    |                    +-------+--------+                         |
    |                            |                                  |
    |                            |  5. Lease job (5-min window)     |
    |                            |     lease_owner = worker_id      |
    |                            |                                  |
    |                            |  6. Load PDF pages (PyMuPDF)     |
    |                            |                                  |
    |                            |  7. Layout-aware extraction:     |
    |                            |     +---------------------------+|
    |                            |     | layout_multimodal_embedder||
    |                            |     |                           ||
    |                            |     | - Extract text per page   ||
    |                            |     | - Detect figures/tables   ||
    |                            |     | - OpenAI Vision (gpt-4o-  ||
    |                            |     |   mini) for image OCR     ||
    |                            |     | - Mathpix OCR (optional)  ||
    |                            |     | - Produce Items with:     ||
    |                            |     |   page, section_path,     ||
    |                            |     |   bbox, type, content     ||
    |                            |     +---------------------------+|
    |                            |                                  |
    |                            |  8. Chunk (Item -> Chunk 1:1):   |
    |                            |     document_chunker.py          |
    |                            |     preserves page numbers       |
    |                            |                                  |
    |                            |  9. Deduplication:               |
    |                            |     content_hash (SHA of text)   |
    |                            |     unique_id_hash (source+space)|
    |                            |     Skip if exact match exists   |
    |                            |                                  |
    |                            |  10. Embed (OpenAI):             |
    |                            |      text-embedding-3-large      |
    |                            |      -> vector(3072)             |
    |                            |                                  |
    |                            |  11. Persist:                    |
    |                            |      AITADocument (status=ready) |
    |                            |      AITAChunk[] with embeddings |
    |                            |------+-------------------------->|
    |                            |      |                           |
    |                            |  12. Update TeacherUpload        |
    |                            |      status='ready'              |
    |                            |      page_count, completed_at    |
    |                            |------+-------------------------->|
    |                            |                                  |
    |  (poll detects ready)      |                                  |
    |<---------------------------+                                  |
```

### Deduplication Logic

```
New document arrives
        |
        v
compute_content_hash(text)
        |
        v
SELECT WHERE content_hash = ?
        |
    +---+---+
    |       |
  exists   new
    |       |
    v       v
  SKIP    compute_unique_identifier_hash(source_markdown, search_space_id)
            |
            v
          SELECT WHERE unique_identifier_hash = ?
            |
        +---+---+
        |       |
      exists   new
        |       |
        v       v
      UPDATE  INSERT
      (re-embed, (new document
       update    + chunks)
       status)
```

---

## 4. Question-Answering Pipeline (RAG)

### End-to-End: Student Asks a Question

```
Student UI                        Backend                           External
    |                                |                                 |
    | POST /ask/stream               |                                 |
    | {chat_id, search_space_id,     |                                 |
    |  question, attachments[]}      |                                 |
    +------------------------------->|                                  |
    |                                |                                  |
    |                    +-----------+----------+                       |
    |                    | 1. AUTH & MEMBERSHIP  |                       |
    |                    |    resolve_auth_ctx() |                       |
    |                    |    check membership   |                       |
    |                    |    (auto-enroll?)     |                       |
    |                    +-----------+----------+                       |
    |                                |                                  |
    |                    +-----------+----------+                       |
    |                    | 2. IMAGE PROCESSING   |                       |
    |                    |    (if attachments)   |                       |
    |                    |    Vision API         |-----> OpenAI gpt-4o-mini
    |                    |    -> transcribe      |<----- image description
    |                    |    append to question |                       |
    |                    +-----------+----------+                       |
    |                                |                                  |
    |  SSE: event=status             |                                  |
    |  "Analyzing question..."       |                                  |
    |<-------------------------------|                                  |
    |                                |                                  |
    |                    +-----------+----------+                       |
    |                    | 3. QUESTION PARSING   |                       |
    |                    |    (ai/main_ai.py)    |                       |
    |                    |                       |                       |
    |                    | a) normalize_query()  |                       |
    |                    |    clean quotes,      |                       |
    |                    |    dashes, whitespace  |                       |
    |                    |                       |                       |
    |                    | b) check_relevance()  |-----> OpenAI gpt-4o
    |                    |    -> full/partial/   |<----- {relevance, reason}
    |                    |       none            |                       |
    |                    |                       |                       |
    |                    | c) extract_keywords() |-----> OpenAI gpt-4o
    |                    |    multi-step:        |<----- keywords list
    |                    |    extract -> expand  |                       |
    |                    |    -> score -> filter |                       |
    |                    |                       |                       |
    |                    | d) parse_question()   |-----> OpenAI gpt-4o
    |                    |    -> ParsedTask:     |<----- {problem_type,
    |                    |    problem_type,      |        asked_outputs,
    |                    |    asked_outputs,     |        knowns,
    |                    |    knowns, etc.       |        constraints}
    |                    +-----------+----------+                       |
    |                                |                                  |
    |  SSE: event=status             |                                  |
    |  "Searching materials..."      |                                  |
    |<-------------------------------|                                  |
    |                                |                                  |
    |                    +-----------+----------+                       |
    |                    | 4. RETRIEVAL          |                       |
    |                    |    (retrieval/        |                       |
    |                    |     pipeline.py)      |                       |
    |                    |                       |                       |
    |                    | See "Retrieval        |                       |
    |                    |  Pipeline Detail"     |                       |
    |                    |  section below        |                       |
    |                    +-----------+----------+                       |
    |                                |                                  |
    |  SSE: event=status             |                                  |
    |  "Generating answer..."        |                                  |
    |<-------------------------------|                                  |
    |                                |                                  |
    |                    +-----------+----------+                       |
    |                    | 5. ANSWER GENERATION  |                       |
    |                    |    (Orchestrator)     |                       |
    |                    |                       |                       |
    |                    | Tutor system prompt + |-----> OpenAI gpt-4o
    |                    | packed snippets +     |<----- answer with
    |                    | chat memory +         |       citations
    |                    | parsed question       |                       |
    |                    |                       |                       |
    |                    | Validation round      |                       |
    |                    | (up to 2 retries)     |                       |
    |                    +-----------+----------+                       |
    |                                |                                  |
    |                    +-----------+----------+                       |
    |                    | 6. CITATION FORMAT    |                       |
    |                    |    Extract structured |                       |
    |                    |    citations from     |                       |
    |                    |    answer text        |                       |
    |                    |    [Label, p. N]      |                       |
    |                    +-----------+----------+                       |
    |                                |                                  |
    |                    +-----------+----------+                       |
    |                    | 7. CHAT PERSISTENCE   |                       |
    |                    |    Upsert session     |------> PostgreSQL
    |                    |    Append user turn   |                       |
    |                    |    Append asst turn   |                       |
    |                    |    Maybe summarize    |-----> OpenAI gpt-4o
    |                    |    memory (if 12+     |<----- summary text
    |                    |    turns)             |                       |
    |                    +-----------+----------+                       |
    |                                |                                  |
    |  SSE: event=answer             |                                  |
    |  {answer, citations[]}         |                                  |
    |<-------------------------------|                                  |
```

### Retrieval Pipeline Detail

```
retrieve_for_question(query, keywords, search_space_id)
    |
    v
+-----------------------------------------------------+
| STEP 1: Embed query                                  |
|   OpenAI text-embedding-3-large -> vector(3072)      |
+-----------------------------------------------------+
    |
    v
+-----------------------------------------------------+
| STEP 2: Hybrid Search (hybrid_search.py)             |
|                                                      |
|   CTE 1 - Semantic Search (_build_semantic_cte):     |
|   inner subquery semantic_candidates:                |
|     SELECT chunk.id, distance                        |
|     FROM aita_chunks                                 |
|     WHERE document.search_space_id = ?               |
|       AND document.status->>'state' = 'ready'        |
|     ORDER BY embedding::halfvec <=> query_halfvec    |
|     LIMIT top_k*5                                    |
|   outer: rank() OVER (ORDER BY distance)             |
|   (HNSW index engaged only if hnsw.iterative_scan;  |
|    currently exact scan — recall 100%)               |
|                                                      |
|   CTE 2 - Keyword Search (_build_keyword_cte):       |
|   inner subquery keyword_candidates:                 |
|     SELECT chunk.id, ts_rank_cd(tsvec, tsquery)      |
|     FROM aita_chunks                                 |
|     WHERE to_tsvector(content) @@ plainto_tsquery(?) |
|       AND document.search_space_id = ?               |
|     ORDER BY ts_rank_cd DESC                         |
|     LIMIT top_k*5                                    |
|   outer: rank() OVER (ORDER BY ts_rank DESC)         |
|   (Inner LIMIT before rank() → top-N heapsort:      |
|    measured 3,938 ms → 113 ms on largest class)      |
|                                                      |
|   FUSION (Reciprocal Rank Fusion):                   |
|   FULL OUTER JOIN semantic + keyword                  |
|   score = 1/(60 + rank_sem) + 1/(60 + rank_kw)      |
|   ORDER BY score DESC                                |
+-----------------------------------------------------+
    |
    v
+-----------------------------------------------------+
| STEP 3: Reranking (optional, disabled by default)    |
|   cross-encoder model re-scores chunks               |
+-----------------------------------------------------+
    |
    v
+-----------------------------------------------------+
| STEP 4: Store Bias (store_bias.py)                   |
|   Apply material_kind weight adjustments:            |
|                                                      |
|   Default weights:                                   |
|     textbook:  +0.12                                 |
|     slides:    +0.06                                 |
|     notes:     +0.06                                 |
|     exercises: +0.00                                 |
|     other:     +0.00                                 |
|                                                      |
|   Teacher can override via retrieval-weights API     |
|   Bounded by weight_bounds (configurable min/max)    |
+-----------------------------------------------------+
    |
    v
+-----------------------------------------------------+
| STEP 5: Context Packing (context_packer.py)          |
|   Token budget: ~6000 tokens                         |
|                                                      |
|   For each chunk (ranked by score):                  |
|     - Count tokens (tiktoken)                        |
|     - If fits in budget: add to bundle               |
|     - Assign citation marker:                        |
|       [<Citation_Label>, p. <page>]                  |
|     - Track source metadata                          |
|                                                      |
|   Output: List[BundleSnippet]                        |
|     - id, text, page, section_path                   |
|     - citation_marker, final_score                   |
|     - doc_title, source_path                         |
+-----------------------------------------------------+
    |
    v
ResearchBundle {
  snippets: BundleSnippet[],
  metadata: ResearchMetadata,
  allowed_markers: ["[Textbook, p. 42]", ...],
  found_terms, not_found_terms, ...
}
```

---

## 5. Chat Memory System

```
Turn 1:  User asks question
Turn 2:  Assistant answers (with citations)
Turn 3:  User asks follow-up
Turn 4:  Assistant answers
  ...
Turn 11: User asks another follow-up
Turn 12: Assistant answers
         |
         v
   TRIGGER: turn_count >= 12
         |
         v
   +----------------------------------+
   | Memory Summarization             |
   |                                  |
   | 1. Fetch all turns               |
   | 2. Send to GPT-4o:              |
   |    "Summarize this conversation  |
   |     preserving key facts,        |
   |     conclusions, and context"    |
   | 3. Truncate to 3000 chars       |
   | 4. Store in chat_sessions.      |
   |    memory_summary               |
   +----------------------------------+
         |
         v
   Next /ask request with same chat_id:
   +----------------------------------+
   | Context Assembly                 |
   |                                  |
   | System prompt includes:          |
   |   1. memory_summary (if exists)  |
   |   2. Last 8 turns (window)       |
   |   3. Retrieved snippets          |
   |   4. Current question            |
   +----------------------------------+

Config:
  CHAT_MEMORY_WINDOW_TURNS     = 8    (recent turns sent to LLM)
  CHAT_MEMORY_SUMMARY_TRIGGER  = 12   (when to summarize)
  CHAT_MEMORY_SUMMARY_MAX_CHARS = 3000 (truncation limit)
```

---

## 6. Invite Link Flow

```
Teacher UI                       Backend                        Database
    |                               |                              |
    | POST /invite-links            |                              |
    | {search_space_id, role}       |                              |
    +------------------------------>|                              |
    |                               | 1. Verify teacher role       |
    |                               | 2. Deactivate existing       |
    |                               |    links (same space+role)   |
    |                               | 3. Generate code:            |
    |                               |    secrets.token_urlsafe(16) |
    |                               | 4. Insert invite_link        |
    |                               |----+------------------------>|
    | {code, url, ...}              |    |                         |
    |<------------------------------+    |                         |
    |                               |    |                         |
    | Teacher copies URL:           |    |                         |
    | {STUDENT_APP_URL}/join/{code} |    |                         |
    |                               |    |                         |
    |                               |    |                         |
    | ============================  |    |                         |
    |                               |    |                         |
Student UI                          |    |                         |
    |                               |    |                         |
    | Navigate to /join/{code}      |    |                         |
    +------------------------------>|    |                         |
    |                               |    |                         |
    | GET /invite-links/resolve/    |    |                         |
    |     {code}                    |    |                         |
    +------------------------------>|    |                         |
    |                               | 5. Check: is_active,        |
    |                               |    use_count < max_uses,    |
    |                               |    expires_at > now         |
    |                               |----+------- SELECT -------->|
    | {course_name, role}           |    |                         |
    |<------------------------------+    |                         |
    |                               |    |                         |
    | POST /invite-links/redeem/    |    |                         |
    |     {code}                    |    |                         |
    | Auth: Bearer {token}          |    |                         |
    +------------------------------>|    |                         |
    |                               | 6. Validate token           |
    |                               | 7. Create membership        |
    |                               |    (user_id, space, role)   |
    |                               |----+--- INSERT ------------>|
    |                               | 8. Increment use_count      |
    |                               |----+--- UPDATE ------------>|
    | {success, course_name, role}  |    |                         |
    |<------------------------------+    |                         |
```

---

## 7. Teacher Configuration Flow

### Retrieval Weight Tuning

```
Teacher UI                       Backend                      Database
    |                               |                            |
    | GET /teacher/retrieval-       |                            |
    |     weights?search_space_id=X |                            |
    +------------------------------>|                            |
    |                               | SELECT teacher_courses     |
    |                               | WHERE search_space_id=X    |
    |                               |---+----------------------->|
    |                               |   |                        |
    | {weights, defaults, bounds}   |   |                        |
    |<------------------------------+   |                        |
    |                               |   |                        |
    | User adjusts sliders:         |   |                        |
    |  textbook:  0.12 -> 0.20      |   |                        |
    |  slides:    0.06 -> 0.10      |   |                        |
    |  notes:     0.06 -> 0.02      |   |                        |
    |                               |   |                        |
    | POST /teacher/retrieval-      |   |                        |
    |      weights                  |   |                        |
    | {search_space_id, weights}    |   |                        |
    +------------------------------>|   |                        |
    |                               | UPDATE teacher_courses     |
    |                               | SET weights = {...}        |
    |                               |---+----------------------->|
    | {updated weights}             |   |                        |
    |<------------------------------+   |                        |
    |                               |   |                        |

Effect on retrieval:
  When student asks question in this course,
  hybrid_search results get adjusted:
    chunk.score += weights[chunk.material_kind]

  Example:
    Textbook chunk score: 0.72 + 0.20 = 0.92  (boosted)
    Slide chunk score:    0.80 + 0.10 = 0.90
    Note chunk score:     0.85 + 0.02 = 0.87  (reduced)
```

### Weekly Material Management

```
Teacher UI                       Backend
    |                               |
    | GET /teacher/weeks            |
    |   ?search_space_id=X          |
    +------------------------------>|
    |                               |
    | Response: CourseState {       |
    |   current_week: 5,           |
    |   total_weeks: 16,           |
    |   weeks: [                   |
    |     {                        |
    |       week: 1,               |
    |       notes: [UploadSummary],|  <-- 0 or more per kind
    |       slides: [UploadSummary]|
    |     },                       |
    |     ...                      |
    |     {                        |
    |       week: 16,              |
    |       notes: [],             |
    |       slides: []             |
    |     }                        |
    |   ]                          |
    | }                            |
    |<-----------------------------+
    |                               |
    | POST /teacher/weeks/current   |
    | {search_space_id, week: 8}    |
    +------------------------------>|
    |                               |
    | Effect: Students can only     |
    | access materials for          |
    | weeks <= current_week         |
```

---

## 8. AI Use Report Generation

```
Student UI                      Backend                        External
    |                               |                              |
    | User clicks "Generate         |                              |
    |  AI Use Report" button        |                              |
    |                               |                              |
    | POST /reports/ai-use/{id}     |                              |
    | {chat_id, style, length}      |                              |
    +------------------------------>|                              |
    |                               | 1. Fetch chat turns          |
    |                               | 2. Build evidence bundle     |
    |                               |    (token budget: 8000)      |
    |                               | 3. Generate report markdown  |
    |                               |    via GPT-4o-mini           |-----> OpenAI
    |                               | 4. Generate JSON-LD          |<----- report
    |                               |    structured data           |
    |                               | 5. Store report              |
    | {id, markdown, jsonld,        |                              |
    |  model_fingerprint,           |                              |
    |  prompt_hashes}               |                              |
    |<------------------------------+                              |
    |                               |                              |
    | Navigate to /report/{id}      |                              |
    |                               |                              |
    | Export options:                |                              |
    |   - Markdown download         |                              |
    |   - JSON download             |                              |
    |   - PDF export (WeasyPrint)   |                              |
    |   - Copy to clipboard         |                              |
```

---

## 9. Frontend Proxy Layer

Both UIs use Next.js API routes as a proxy to avoid CORS and hide backend URLs.

```
Browser                  Next.js API Route              FastAPI Backend
   |                          |                              |
   | fetch('/api/ask/stream', |                              |
   |   {body, headers})       |                              |
   +------------------------->|                              |
   |                          | const backend =              |
   |                          |   process.env.AI_TA_API_     |
   |                          |   BASE_URL                   |
   |                          |                              |
   |                          | fetch(`${backend}/ask/       |
   |                          |   stream`, {                 |
   |                          |   headers: {                 |
   |                          |     Authorization:           |
   |                          |       req.headers.auth,      |
   |                          |     'Content-Type':          |
   |                          |       'application/json'     |
   |                          |   },                         |
   |                          |   body: req.body             |
   |                          | })                           |
   |                          +----------------------------->|
   |                          |                              |
   |                          |  Response (SSE stream)       |
   |                          |<-----------------------------+
   |  Stream forwarded        |                              |
   |<-------------------------+                              |

Headers always set:
  Cache-Control: no-store
  Authorization: Bearer {token}  (forwarded from client)
```

---

## 10. Data Contracts (Key Structures)

### BundleSnippet (what the LLM sees)

```python
{
  "id": "chunk-uuid",
  "type": "body",                           # body | heading | figure
  "page": 42,
  "section_path": "Ch3 > 3.2 > Normal Shocks",
  "text": "A normal shock wave is a...",
  "figure_id": null,
  "citation_marker": "[Textbook, p. 42]",   # auto-generated
  "final_score": 0.847,
  "doc_title": "Fluid Mechanics 9th Ed",
  "source_path": "textbook/fluid_mech.pdf",
  "metadata": {
    "material_kind": "textbook",
    "week": null,
    "ocr_confidence": 0.95
  }
}
```

### ResearchBundle (retrieval output)

```python
{
  "metadata": {
    "question": "Is flow through a normal shock isentropic?",
    "problem_type": "yes_no",
    "k_sem": 20,          # top-k for semantic search
    "k_lex": 20,          # top-k for keyword search
    "token_budget": 6000,
    "found_terms": ["normal shock", "isentropic"],
    "not_found_terms": ["entropy change"],
    "subject": "Fluid Mechanics"
  },
  "snippets": [BundleSnippet, ...],
  "allowed_markers": ["[Textbook, p. 42]", "[Slides, Week 3]"],
  "warnings": [],
  "coverage_gaps": []
}
```

### SSE Response (what the frontend receives)

```
event: status
data: {"message": "Analyzing question..."}

event: status
data: {"message": "Searching course materials..."}

event: status
data: {"message": "Generating answer..."}

event: answer
data: {
  "answer": "## Answer\n\nNo, flow through a normal shock is **not** isentropic [Textbook, p. 42]...",
  "citations": [
    {
      "label": "Textbook, p. 42",
      "doc_type": "textbook",
      "file": "fluid_mech.pdf",
      "page": 42,
      "ocr_conf": 0.95,
      "thumb": null
    }
  ]
}
```

---

## 11. Tutor Prompt: Source-Binding Rules

The tutor prompt (`ai/prompts/tutor.py`, ~400 lines) enforces strict citation discipline:

```
SOURCE EXCERPTS provided to LLM:
  [1] [Textbook, p. 42]  score=0.85
      "A normal shock wave causes an increase in entropy..."

  [2] [Slides, Week 3]   score=0.72
      "Normal shocks: irreversible process, entropy increases..."

RULES:
  1. Every factual claim requires exact citation marker
  2. No background knowledge allowed
  3. No generalizing from special cases
  4. Preserve exact qualifiers ("normal shock" not just "shock")
  5. Claim-level citations, not paragraph-level
  6. If sources insufficient: say "the provided materials
     do not cover this" (never fabricate)

ADAPTIVE FORMATS:
  yes/no       -> Answer first, 1-2 sentences + citations
  definitional -> One-sentence definition, clarify subtypes
  procedural   -> Overview + numbered steps with required knowns
  comparative  -> Main difference first, then contrast lines
  derivation   -> Result first, then logical chain
  multipart    -> Labeled answers per sub-question in order
```

---

## 12. Environment & Configuration Summary

```
+------------------+---------------------+---------------------------+
| Variable         | Where               | Purpose                   |
+------------------+---------------------+---------------------------+
| OPENAI_API_KEY   | backend .env        | All LLM & embedding calls |
| PARSER_MODEL     | backend .env        | Parsing/scope/keywords (gpt-4o); does NOT drive scoring |
| KEYWORD_MODEL    | backend .env        | Keyword extraction (gpt-4o; mini ~2x slower) |
| CITATION_SCORER_MODEL | backend .env   | Snippet scoring (gpt-4o; no PARSER_MODEL fallback) |
| CITATION_WORKERS | backend .env        | Scoring pool cap (24; >K_SEM → one wave) |
| SOLVER_MODEL     | backend .env        | Answer generation (gpt-4o)|
| PROMPT_CACHE_KEY | backend .env        | OpenAI prefix-cache key (aita-solver:<model>) |
| OPENAI_SERVICE_TIER | backend .env     | Optional service tier for solver requests |
| MAIN_VERBOSITY   | backend .env        | text.verbosity for streaming reasoning models |
| VISION_MODEL     | backend .env        | Image transcription       |
| REPORTS_MODEL    | backend .env        | Report gen (gpt-4o-mini)  |
| EMBEDDING_MODEL  | backend .env        | text-embedding-3-large    |
| EMBEDDING_DIM    | backend .env        | 3072 (must match model)   |
+------------------+---------------------+---------------------------+
| SUPABASE_URL     | all .envs           | Supabase project endpoint |
| SUPABASE_API_KEY | backend .env        | Server-side Supabase key  |
| SUPABASE_DB_URL  | backend .env        | Direct PostgreSQL conn    |
| SERVICE_ROLE_KEY | backend .env        | Supabase admin access     |
| ANON_KEY         | frontend .envs      | Client-side Supabase auth |
+------------------+---------------------+---------------------------+
| RETRIEVAL_BACKEND| backend .env        | "supabase" (pgvector)     |
| RERANKERS_ENABLED| backend .env        | false (cross-encoder off) |
| RETRIEVAL_WIRE_  | backend .env        | "on" for debug logging    |
| LOG              |                     |                           |
+------------------+---------------------+---------------------------+
| CORS_ALLOW_      | backend .env        | localhost:3001,3002       |
| ORIGINS          |                     |                           |
| PORT             | backend .env        | 8000                      |
+------------------+---------------------+---------------------------+
| AUTO_ENROLL_     | backend .env        | 0 = disabled              |
| STUDENT_MEMBER   |                     |                           |
| CHAT_MEMORY_     | backend .env        | 8 turns window            |
| WINDOW_TURNS     |                     |                           |
| CHAT_MEMORY_     | backend .env        | 12 turns to trigger       |
| SUMMARY_TRIGGER  |                     |                           |
| TEACHER_TOTAL_   | backend .env        | 16 weeks per course       |
| WEEKS            |                     |                           |
+------------------+---------------------+---------------------------+
| AI_TA_API_BASE_  | frontend .envs      | Backend proxy target      |
| URL              |                     | (http://localhost:8000)   |
| STUDENT_APP_URL  | teacher UI .env     | For invite link URLs      |
| SHOW_CITATION_   | student UI .env     | "1" to show previews      |
| PREVIEWS         |                     |                           |
+------------------+---------------------+---------------------------+
```

---

## 13. Full Request Lifecycle (Everything Together)

```
1. Teacher uploads PDF
   -> stored in Supabase Storage
   -> background worker extracts + chunks + embeds
   -> AITADocument + AITAChunk rows with vectors in PostgreSQL

2. Teacher sets current_week = 5
   -> students see materials for weeks 1-5

3. Teacher adjusts weights (textbook: 0.20, slides: 0.10)
   -> stored in teacher_courses.weights JSONB

4. Student signs in (Supabase Auth)
   -> JWT stored in localStorage
   -> forwarded as Bearer token on every request

5. Student joins class via invite link
   -> course_membership created (student role)

6. Student asks: "Is flow through a normal shock isentropic?"
   |
   v
   a) Auth verified, membership checked
   b) Question normalized, relevance checked (full)
   c) Keywords extracted: ["normal shock", "isentropic", "entropy"]
   d) Question parsed: {type: "yes_no", asked: ["is_isentropic"]}
   e) Query embedded: text-embedding-3-large -> vector(3072)
   f) Hybrid search: semantic (cosine) + keyword (FTS) + RRF fusion
   g) Store bias applied: textbook chunks boosted +0.20
   h) Context packed: top chunks within 6000 token budget
   i) Tutor prompt assembled with snippets + chat memory
   j) GPT-4o generates answer with strict citations
   k) Answer validated, citations extracted
   l) Chat turn persisted (user + assistant)
   m) Memory summarized if 12+ turns
   n) SSE stream delivers answer + structured citations to UI

7. Student views answer with citation chips
   -> clicks [Textbook, p. 42] -> sees source metadata

8. Student generates AI Use Report
   -> GPT-4o-mini summarizes chat interaction
   -> PDF/JSON/Markdown export available
```
