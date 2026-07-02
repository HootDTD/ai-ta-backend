# Apollo E2E grading campaign — local stack (Task C1)

Fully local-Docker infrastructure for the campaign: a local Supabase stack
(Postgres + auth + gateway) and a local Neo4j 5.25 container, plus a
migration applier and reset scripts. Nothing here ever touches remote
Supabase (prod `uduxdniieeqbljtwocxy` or test `hjevtxdtrkxjcaaexdxt`) or
Neo4j Aura — nothing but literal `127.0.0.1` DSNs/URIs at fixed local ports.

## Port deviations from the plan sketch

The plan's draft used Supabase/Neo4j defaults (54321/54322/... and
7687/7474). This dev machine already runs two other local Supabase stacks
(project `ai-ta-backend` on 54321-54327, and a `maestro-nestjs` project on
55321-55324) plus assorted `testcontainers` containers on ephemeral ports.
To guarantee zero collision, the campaign moves every port:

| Service              | Plan sketch | Campaign (actual)        |
|----------------------|-------------|---------------------------|
| Supabase API/gateway | 54321       | **57321**                 |
| Supabase DB          | 54322       | **57322**                 |
| Supabase shadow DB   | 54320       | **57320**                 |
| Supabase pooler      | 54329       | **57329**                 |
| Supabase Studio      | 54323       | **57323**                 |
| Supabase Inbucket    | 54324       | **57324**                 |
| Supabase Analytics   | 54327       | disabled (see below)      |
| Neo4j bolt           | 7687        | **57687**                 |
| Neo4j http           | 7474        | **57474**                 |

`supabase/config.toml` (`project_id = "e2e-harness"`) carries the Postgres
port changes; `campaign/infra/docker-compose.neo4j.yml` carries the Neo4j
ones (override via `$NEO4J_BOLT_PORT` / `$NEO4J_HTTP_PORT` if 57687/57474
ever collide on a given machine).

**Analytics disabled:** `[analytics] enabled = false` in `supabase/config.toml`.
On this Windows/Docker-Desktop host, Supabase's `vector` log-forwarder
container can never reach a healthy state (it needs the Docker Engine API
exposed over TCP, which isn't enabled by default — see the CLI's own
`WARNING: Analytics on Windows requires Docker daemon exposed on
tcp://localhost:2375`). With analytics on, `storage`/`rest`/`realtime`/
`studio`/`pg_meta` all block forever waiting on `vector`'s healthcheck and
never start. None of those services are needed for this task (only Postgres
is required for the health-route boot; `auth`/gateway are needed later for
D3's JWT minting), so analytics is off. If a future task needs Storage (e.g.
D1's WU-AAS PDF upload path) and it's still blocked, re-enable analytics and
either expose the Docker daemon on TCP per Supabase's Windows guide, or
research `vector`'s alternate log source config.

## Bring-up (fresh boot from nothing)

```bash
cd ai-ta-backend   # or the campaign worktree root

# 1. Local Supabase stack (Postgres + auth + gateway; project id "e2e-harness")
supabase start
# Note the printed DB URL + anon/service keys (also: `supabase status -o json`).

# 2. Local Neo4j
docker compose -f campaign/infra/docker-compose.neo4j.yml up -d

# 3. Apply every database/migrations/*.sql, in order, to the local DB.
#    (Also bootstraps the SQLAlchemy ORM baseline first — see "Why a baseline
#    step" below.)
python -m campaign.infra.apply_migrations \
  --dsn "postgresql+asyncpg://postgres:postgres@127.0.0.1:57322/postgres" \
  --dir database/migrations

# 4. Environment
cp campaign/infra/env.campaign.example .env.campaign
# fill OPENAI_API_KEY and SUPABASE_SERVICE_ROLE_KEY from `supabase status -o json`

# 5. Boot the backend against the local stack (env vars, not `.env` — server.py
#    loads `.env` by default; export the campaign vars directly or copy
#    .env.campaign to .env for a throwaway local run).
set -a; source .env.campaign; set +a
uvicorn server:app --host 127.0.0.1 --port 8000

# 6. Verify
curl -s localhost:8000/healthz   # {"status": "ok"}
```

Expected after step 3: table `_campaign_migrations` in the local DB has one
row per applied `database/migrations/*.sql` file (31 files as of migration
033, including the known `023` duplicate pair — see `KNOWN_DUP_NUMBERS` in
`campaign/infra/apply_migrations.py`).

**Verified 2026-07-02** on this host: fresh `supabase start` + Neo4j compose
up + `apply_migrations` (31/31 applied) + `uvicorn server:app` against the
resulting env → `GET /healthz` → `200 {"status":"ok"}`, and `GET /classes`
(a real query against the freshly-migrated `aita_search_spaces` table) →
`200 []`.

## Why a baseline step before replaying migrations

`database/migrations/` only starts at `004`: the base tables
(`aita_search_spaces`, `aita_documents`, ...) were never given a numbered
migration — the real bootstrap path (used by `tests/conftest.py`'s
`_pg_url` fixture and, historically, prod) is
`Base.metadata.create_all` from `database/models.py` (which `apollo/persistence/models.py`
extends via the same shared `Base`). Every migration from `004` onward
assumes that baseline schema already exists and is written with guarded DDL
(`CREATE TABLE IF NOT EXISTS`, `ADD COLUMN IF NOT EXISTS`,
`DROP COLUMN IF EXISTS`, etc. — verified: every file under
`database/migrations/` uses at least one `IF [NOT] EXISTS` guard), so
replaying them on top of the CURRENT ORM schema (rather than a
migration-by-migration historical replay) is safe and idempotent.
`campaign.infra.apply_migrations.bootstrap_baseline()` does exactly that:
`CREATE EXTENSION IF NOT EXISTS vector` + `Base.metadata.create_all`, mirroring
the test fixture. `campaign.infra.reset.reset_postgres()` calls it
automatically after dropping the schema.

## Reset between runs

```bash
python -c "
import asyncio
from campaign.infra.reset import reset_all
asyncio.run(reset_all(
    pg_dsn='postgresql://postgres:postgres@127.0.0.1:57322/postgres',
    neo4j_uri='bolt://127.0.0.1:57687',
    neo4j_auth=('neo4j', 'campaignpass'),
))
"
```

Or via the CLI shims: `python -m campaign.infra.reset --dsn ... --neo4j-uri ...`.

## Supabase CLI availability

`supabase` CLI 2.109.0 was already installed on this machine (`scoop`
shim). No install step was needed for this task; if it's ever missing,
`npx supabase@latest init` / `scoop install supabase` both work, or fall
back to a plain `docker-compose` Postgres image + `apply_migrations.py`
against it directly (the migration applier has no Supabase-CLI dependency —
it only needs an asyncpg-reachable Postgres).

## Stopping the stack

```bash
supabase stop                                                    # Postgres/auth/gateway
docker compose -f campaign/infra/docker-compose.neo4j.yml down   # Neo4j
```

Both are scoped to this campaign's containers only (`project_id =
"e2e-harness"` / container name `apollo-campaign-neo4j`) — neither touches
any other local Supabase project or Neo4j container on the machine.
