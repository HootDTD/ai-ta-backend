---
doc: database/_index
description: Router for core persistence — the database/ + supabase/ slice, with the schema truth hierarchy and the add-a-column recipe.
owns: []
related: []
last_verified: 2026-07-25
stub: false
---

# database/ — core persistence & migrations

The `database/` + `supabase/` slice carved out of the old `domain-data.md`
monolith (`chats/`, `knowledge/`, `reports/` moved to their own domains).

| Leaf | One-liner | Owns |
|---|---|---|
| [models](models.md) | Core SQLAlchemy 2 ORM (course/document/upload/chat) | `database/models.py`, `__init__.py` |
| [session](session.md) | Async engine/session + run_async bridge + DB-08b RLS | `database/session.py` |
| [transforms](transforms.md) | Column-value coercion helpers | `database/transforms.py` |
| [legacy-migrations](legacy-migrations.md) | FROZEN numbered chain (provenance only) | `database/migrations/**` (sentinel + manifest) |
| [supabase-migrations](supabase-migrations.md) | ACTIVE timestamped chain + config/seed | `supabase/migrations/**`, `config.toml`, `seed.sql`, `.gitignore` |

## Cross-cutting invariants (truth hierarchy)

- **`supabase/migrations/` (timestamped) = the CURRENT forward schema** — the
  28-table app/internal target (18 `app` / 10 `internal`, forced-RLS, enforced
  `app_runtime` role per DB-08b/c). `database/models.py` ORM MUST track it.
- **`database/migrations/` 001–047 = FROZEN legacy provenance only** —
  checksum-locked, retired Python runners. Never treat the numbered chain as live.
- Three distinct `models.py` owners: [models](models.md) (core), apollo
  learner/grading ([apollo/persistence/models](../apollo/persistence/models.md)),
  reports (`reports/ai-use-models`). No overlap; `models.py` owns the FILE while
  chat/teacher **behavior** is narrated by those domains via `related`.

## Recipe — add a column + migration (D21)

To add a column on a tutoring/learner table: change the ORM model
([apollo/persistence/models](../apollo/persistence/models.md)) **and** add a
migration to the **active supabase chain**
([supabase-migrations](supabase-migrations.md)) in the SAME commit — never the
frozen legacy chain. Those two docs carry directional `related:` back-links.
For a core table (`app.courses`/`documents`/`chat_messages`/…) the ORM lives in
[models](models.md); the same active-chain rule applies.
