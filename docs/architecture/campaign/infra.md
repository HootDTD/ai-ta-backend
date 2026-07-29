---
doc: campaign/infra
description: Campaign DB bootstrap and teardown against LOCAL Docker Postgres + Neo4j.
owns:
  - campaign/infra/apply_migrations.py
  - campaign/infra/reset.py
  - campaign/infra/__init__.py
related:
  - database/legacy-migrations
  - database/models
last_verified: 2026-07-25
stub: false
---

# campaign/infra — local bootstrap + reset

Brings the campaign's local stack to a fresh, migrated state. LOCAL only —
callers pass local DSN/URIs; this module has no knowledge of remote credentials.

## Interface

- `apply_migrations.py`: DSN converters `to_asyncpg_dsn`/`to_sqlalchemy_dsn`;
  `migration_files(directory) -> list[Path]` (ordered discovery, raises
  `MigrationOrderError` on a duplicate number outside `KNOWN_DUP_NUMBERS={23}`);
  `ParsedMigration`/`_parse`; `AsyncpgConnLike` Protocol + `_default_connect` /
  `_fetch_applied` / `_apply_one`; `bootstrap_baseline(dsn)`
  (`Base.metadata.create_all` — the base tables never had a numbered migration);
  `apply_all(dsn, directory, *, connect) -> list[str]` (idempotent, tracked in
  `_campaign_migrations`); `_main(argv)` CLI.
- `reset.py`: `reset_postgres(dsn, …)` (DROP+recreate `public`, re-bootstrap,
  re-apply), `reset_neo4j(uri, auth, …)` (+ `_default_neo4j_wipe`
  `MATCH (n) DETACH DELETE n`), `reset_all(…)`, `_main()` CLI.

## Invariants & gotchas

- **This applies the numbered LEGACY chain shape** (`database/migrations/`,
  the default `directory`), distinct from the active `supabase/migrations/`
  chain — see [database/legacy-migrations](../database/legacy-migrations.md).
  It replays that chain on top of the CURRENT ORM baseline; every migration uses
  guarded DDL so replay is safe.
- The `023` double-mint is the only allowed duplicate number.

## Env flags

- `CAMPAIGN_DSN` / `SUPABASE_DB_URL`, `NEO4J_URI`, `NEO4J_USERNAME`,
  `NEO4J_PASSWORD` (CLI defaults).

## Non-source assets

`campaign/infra/docker-compose.neo4j.yml` and `env.campaign.example` (config,
not code — not owned).

## Related

- [database/legacy-migrations](../database/legacy-migrations.md),
  [database/models](../database/models.md) (`Base.metadata`).
