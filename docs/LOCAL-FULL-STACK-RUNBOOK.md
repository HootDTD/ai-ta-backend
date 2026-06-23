# Local full-stack runbook — textbook → question bank → Apollo

Run the entire pipeline locally in Docker with **all functions active**:
embed a textbook → auto-provision the Apollo question bank → teach Apollo.
Nothing here touches the cloud (test or prod) Supabase or Neo4j Aura.

**Topology**
- **Supabase CLI local stack** (`supabase start`) → Postgres `:54322`, Auth +
  Storage + PostgREST `:54321`, Studio `:54323`. Provides the JWT auth and
  Storage the upload path needs.
- **Local Neo4j** (`docker-compose.local.yml`) → Bolt `:7687`, Browser `:7474`
  (the graph visualization). Isolated from Neo4j Aura.
- **4 processes**: `web` (uvicorn), `worker` (textbook ingest),
  `apollo-provision` (question-bank generation), `apollo-janitor` (optional).

**Env model (no `.env` edits)**
`server.py` loads `.env` then `.env.local` (override). `.env` keeps your real
`OPENAI_API_KEY` + cloud creds untouched; `.env.local` (gitignored, no secrets)
points everything at local infra and flips every feature flag on. Workers don't
read `.env`, so each worker terminal dot-sources `scripts/load_local_env.ps1`.

---

## 0. Prerequisites (one-time)
- Docker Desktop running. ✅ (verified)
- Python venv with backend deps. ✅ (`.venv`, PyMuPDF confirmed)
- **Supabase CLI** — NOT installed yet. Install (PowerShell, via `!`):
  ```powershell
  # scoop route (official on Windows):
  Set-ExecutionPolicy -Scope CurrentUser RemoteSigned -Force
  irm get.scoop.sh | iex
  scoop bucket add supabase https://github.com/supabase/scoop-bucket.git
  scoop install supabase
  ```
  (Fallback: download `supabase_windows_amd64.zip` from
  github.com/supabase/cli/releases, unzip, add `supabase.exe` to PATH.)

## 1. Start local Supabase
```powershell
cd ai-ta-backend
supabase init          # creates supabase/config.toml (first time only)
supabase start         # pulls images, boots the stack (~1-2 min first run)
supabase status        # prints API URL, anon key, service_role key
```
Paste the **anon key** and **service_role key** from `supabase status` into
`.env.local` (`SUPABASE_ANON_KEY`, `SUPABASE_SERVICE_ROLE_KEY`).

## 2. Start local Neo4j
```powershell
docker compose -f docker-compose.local.yml up -d
# Browser: http://localhost:7474  (neo4j / local_password123)
```

## 3. Build the schema (ORM create_all, not the SQL migrations)
```powershell
. .\scripts\load_local_env.ps1
python scripts\bootstrap_local_db.py    # refuses any non-local DB URL
```

## 4. Seed a teacher + course, and upload the smoke PDF
```powershell
python scripts\make_smoke_pdf.py        # -> scripts/smoke_bernoulli.pdf (done)
# (seed + upload helper script added in the next step of the build)
```

## 5. Run the processes (one per terminal, each dot-sources the env)
```powershell
. .\scripts\load_local_env.ps1 ; uvicorn server:app --port 8000           # web
. .\scripts\load_local_env.ps1 ; python -m teacher_upload_worker          # ingest
. .\scripts\load_local_env.ps1 ; python -m apollo.provision_worker        # question bank
. .\scripts\load_local_env.ps1 ; python -m apollo.learner_janitor_worker  # (optional)
```

## 6. Observe the question bank being created
- `worker` log: download → extract → embed → READY, then enqueues a provisioning job.
- `apollo-provision` log: scrape → solve → pair → tag/mint → promote.
- Postgres (Studio `:54323` or psql):
  ```sql
  select tier, count(*) from apollo_concept_problems group by tier;   -- expect tier=2 rows
  select id, slug, display_name from apollo_concepts;                 -- auto-created from questions
  ```
- Neo4j Browser `:7474`: `MATCH (n:Canon) RETURN n LIMIT 50;`

## 7. Teach Apollo
Start a session (`POST /apollo/sessions/from_hoot`) for the course and confirm a
problem is served from `apollo_concept_problems` (tier 2).

---

### Notes
- **Cost**: provisioning calls OpenAI (embeddings + GPT-4o). The 1-page smoke
  PDF is cents; the full `subjects/Fluid Mechanics/fluidMechanics.pdf` is the
  scaled-up run once the loop is green.
- **Mathpix**: off (native PyMuPDF extraction only).
- **Migrations**: this runbook builds the schema from ORM models. Rehearsing the
  numbered SQL migrations (001..030, incl. the 023 collision) against the TEST
  Supabase project is a separate task.
- **Reset**: `supabase stop` + `docker compose -f docker-compose.local.yml down -v`.
