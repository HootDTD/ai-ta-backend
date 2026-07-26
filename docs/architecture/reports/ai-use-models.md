---
doc: reports/ai-use-models
description: reports/ai_use/models.py — the AIUsageReport SQLAlchemy ORM model (app.ai_usage_reports) plus its typed async, owner-scoped repository
owns:
  - reports/ai_use/models.py
related:
  - database/models
  - database/supabase-migrations
  - reports/ai-use-routes
last_verified: 2026-07-25
stub: false
---

# reports/ai-use-models — AIUsageReport ORM

The typed SQLAlchemy repository that replaced the legacy anon-key PostgREST CRUD;
every function takes the caller's own request-scoped `AsyncSession`.

## Interface

- `AIUsageReport(Base)` — maps `app.ai_usage_reports`
  (`__tablename__="ai_usage_reports"`, `{"schema": "app"}`): `id` (UUID str PK),
  `user_id` (UUID, not null, **no ORM FK** — `auth.users` is Supabase-managed),
  `course_id` (FK `app.courses.id` ON DELETE CASCADE), `chat_id` (FK
  `app.learning_activities.external_id` ON DELETE CASCADE), `style`/`length`/
  `markdown`/`jsonld`/`model_fingerprint`/`tool_calls`/`prompt_hashes`/
  `created_at`, plus two `Index`es in `__table_args__`.
- `create_report(db, *, user_id, course_id, chat_id, …) -> AIUsageReport`.
- `get_report_for_user(db, *, report_id, user_id) -> AIUsageReport | None` —
  owner-scoped read.

## Data flow

`ai-use-routes` calls `create_report` (insert + commit + refresh) and
`get_report_for_user` (owner-scoped select). Imports `Base` from
`database/models`; `jsonld`/`tool_calls` use JSONB with a SQLite JSON variant for
unit tests.

## Invariants & gotchas

- **DDL authority is the migration `supabase/migrations/…_create_app_schema_v1.sql`**
  (owned by `database/supabase-migrations`) — this ORM documents the mapping
  ONLY, and the two must not drift; the ORM declares no CHECK constraints (SQL is
  the CHECK authority, verified in `tests/database/test_app_schema_v1.py`).
- **Append-only from the app's perspective** — there is no update path in code.
- `get_report_for_user` returns `None` for both a missing report AND another
  user's report — the two are deliberately indistinguishable (no existence leak).

## Related

`database/models` (`Base`), `database/supabase-migrations` (the DDL authority —
the add-a-column recipe), `reports/ai-use-routes` (the caller).
