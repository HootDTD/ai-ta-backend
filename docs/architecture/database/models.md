---
doc: database/models
description: Core SQLAlchemy 2 ORM models (course/document/upload/chat) with post-#194 app/internal names.
owns:
  - database/models.py
  - database/__init__.py
related:
  - database/session
  - database/transforms
  - database/supabase-migrations
  - chats/service
  - knowledge/teacher-weekly
  - indexing/indexing-service
last_verified: 2026-07-25
stub: false
---

# database/models — core ORM

The core pgvector-backed ORM. **Use the ACTUAL class names** — the legacy
`AITADocument`/`SearchSpace`/`TeacherUpload`/`TeacherCourse`/`ChatTurn` names are
gone (Appendix A #13/#14). This is a known monolith (613 lines, R2): a future
code-split would enable finer doc ownership, but the bijection maps the whole
file here today.

## Interface

Base building blocks: `Base(DeclarativeBase)`, `BaseModel` (int `id`),
`TimestampMixin`, `DocumentType(StrEnum)`, `DocumentStatus` (string-state helper
with `ready()`/`pending()`→`"queued"`/`processing()`/`failed(reason)`,
`is_state()`, `get_failure_reason()`), `EMBEDDING_DIM` (from env, 3072).

Per-model compact table (class → `schema.table` → notable columns):

| Class | Table | Notes |
|---|---|---|
| `Course` | `app.courses` | `current_week` SmallInt CHECK 1..16, `retrieval_weights` JSONB, weight min/max bounds — legacy `TeacherCourse` FOLDED IN here |
| `Document` | `app.documents` | `material_kind`, `content`, `source_markdown`, `content_hash` uniq, `unique_identifier_hash` uniq, `embedding` `Vector(EMBEDDING_DIM)`, `metadata` JSONB, `week`, `status` String(20), FK `course_id` |
| `DocumentChunk` | `internal.document_chunks` | `content`, `embedding` Vector, `page_number`, `section_path`, `chunk_type`, `figure_id`, denormalized `course_id`, FK `document_id` |
| `CourseMembership` | `app.course_memberships` | composite PK (`user_id`, `course_id`), `role` |
| `CourseInvite` | `app.course_invites` | `code` uniq, `role`, `max_uses`/`use_count`, `expires_at` |
| `Upload` | `app.uploads` | `week`, `kind`, `status`, `storage_key`, `document_id`, `artifact_manifest` JSONB, `ocr_details`, `is_latest` |
| `UploadJob` | `internal.upload_jobs` | lease queue: `state`, `lease_owner`, `lease_expires_at`, `attempt_count` |
| `LearningActivity` | `app.learning_activities` | polymorphic base (`polymorphic_on=modality`, abstract) |
| `ChatSession` | (LearningActivity) | `polymorphic_identity="chat"`; `messages` relationship |
| `ChatMessage` | `app.chat_messages` | `turn_index`, `role`, `content`, `citations` JSONB, `keywords` ARRAY(Text) write-only; uniq (`learning_activity_id`,`turn_index`) |
| `ChatSessionSnippet` | `internal.chat_session_snippets` | bundle/citation cache, composite PK |
| `ChatRouterDecision` | `internal.chat_routing_decisions` | per-`/ask` router telemetry |

## Invariants & gotchas

- **`status` is a String(20) state, not a JSON envelope** — compare via
  `DocumentStatus.is_state()`; `DocumentStatus.pending()` returns `"queued"`.
- **Vector dim comes from `EMBEDDING_DIM` env (3072)** — not hard-coded.
- **No `chat_sessions`/`chat_turns` tables** — chat rides `learning_activities`
  (polymorphic) + `app.chat_messages` (`ChatMessage`, NOT `ChatTurn`).
- The ORM declares CHECK constraints on `Course` only; the active migration SQL
  is the DDL authority (see [supabase-migrations](supabase-migrations.md)).
- `database/__init__.py` is empty (glue).

## Related

- [session](session.md), [transforms](transforms.md) (column coercion),
  [supabase-migrations](supabase-migrations.md) (DDL authority).
- Behavior narrated elsewhere: [chats/service](../chats/service.md),
  [knowledge/teacher-weekly](../knowledge/teacher-weekly.md),
  [indexing/indexing-service](../indexing/indexing-service.md).
