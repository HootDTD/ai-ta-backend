# F1a stack state — for the follow-on corpus-running task

Everything below describes the LIVE state as left running at the end of the
F1a task (2026-07-02). **Do not stop these processes/containers** — reuse
them directly.

## Worktree

`C:\Users\ultra\OneDrive\TA-test\.worktrees\e2e-harness` (repo `ai-ta-backend`,
branch `feat/apollo-e2e-campaign-harness`). Never touch the sibling worktree
at `C:\Users\ultra\OneDrive\TA-test\ai-ta-backend`.

## Docker containers (all local-only, per campaign/README.md port scheme)

- Supabase project `e2e-harness` (via `supabase start`, config
  `supabase/config.toml`): containers named `supabase_*_e2e-harness`
  (db/auth/kong/rest/realtime/storage/studio/pg_meta/inbucket/edge_runtime).
  - API/gateway: `http://127.0.0.1:57321`
  - DB: `postgresql://postgres:postgres@127.0.0.1:57322/postgres`
  - Studio: `http://127.0.0.1:57323`
  - Analytics disabled (`[analytics] enabled = false` — see campaign/README.md).
  - Stop with `supabase stop` (do NOT run this — leave up).
- Neo4j: container `apollo-campaign-neo4j` (image `neo4j:5.25`), started via
  `docker compose -f campaign/infra/docker-compose.neo4j.yml up -d`.
  - Bolt: `bolt://127.0.0.1:57687`, HTTP: `http://127.0.0.1:57474`
  - Auth: `neo4j` / `campaignpass`
  - Stop with `docker compose -f campaign/infra/docker-compose.neo4j.yml down`
    (do NOT run this — leave up).

## Python environments

- **General/backend scripts (no torch needed)**: `/c/Users/ultra/anaconda3/python`
  (system default in PATH). Has sqlalchemy/asyncpg/fastapi/httpx/openai/etc.
  already installed. **Does NOT have torch/transformers** — do not use it to
  boot uvicorn or run anything importing `apollo.resolution.nli_adjudicator`.
  Used for: `provision_seeded`, `generate_fixtures`, `run_s1_s2.py`, ad-hoc DB
  scripts.
- **Backend server / anything needing NLI (torch)**: NEW venv created this
  task at `C:\Users\ultra\OneDrive\TA-test\.worktrees\e2e-harness\.venv-torch`
  (`.venv-torch/Scripts/python.exe`). Contents: `torch==2.6.0+cpu`,
  `transformers==4.57.6`, plus the full `requirements.txt`. On Windows this
  venv's `Scripts\python.exe` is a launcher STUB (new in CPython 3.11+) that
  spawns the REAL base interpreter (`anaconda3\python.exe`) as a CHILD
  process with the venv's site-packages correctly applied via `pyvenv.cfg`
  — this is expected; `Get-CimInstance Win32_Process` will show the child's
  `CommandLine` as the anaconda3 path, but the venv's torch/transformers ARE
  what's actually imported (verified: NLI prewarm succeeded, needs torch).
- HF model cache: `.hf-cache/` under the worktree root (gitignored),
  seeded via `HF_HOME=./.hf-cache .venv-torch/Scripts/python.exe -m
  campaign.infra.prewarm_nli` — confirmed working OFFLINE
  (`HF_HUB_OFFLINE=1` re-run also succeeds, `load_seconds≈4.5`).

## Backend server process

- Started via a throwaway launcher script (`/tmp/boot_uvicorn.sh`, POSIX
  path under Git Bash — recreate if gone) that: sources `.env.campaign`,
  overrides/exports the 7 Apollo flags from the task brief
  (`APOLLO_GRADING_ARTIFACT_ENABLED=1`, `APOLLO_GRAPH_SIM_SHADOW_ENABLED=1`,
  `APOLLO_CLARIFICATION_ENABLED=1`, `APOLLO_NLI_ENABLED=1`,
  `APOLLO_NLI_PREWARM=1`, `APOLLO_GRAPH_GRADER_LIVE=0`, `HF_HOME=./.hf-cache`)
  plus `NEO4J_URI=bolt://127.0.0.1:57687` / `NEO4J_USERNAME=neo4j` /
  `NEO4J_PASSWORD=campaignpass` / `NEO4J_DATABASE=neo4j` (not present in
  `env.campaign.example` — the seeding scripts and `Neo4jClient.from_env()`
  need these directly, not just the `NEO4J_URI`/`NEO4J_USERNAME`/
  `NEO4J_PASSWORD` names already there... actually they ARE there; just
  also exported at shell level for scripts run outside the app process),
  then `exec .venv-torch/Scripts/python.exe -m uvicorn server:app --host
  127.0.0.1 --port 8000`.
- **Exact command**, if you need to relaunch:
  ```bash
  cd "C:\Users\ultra\OneDrive\TA-test\.worktrees\e2e-harness"
  set -a; source .env.campaign; set +a
  export APOLLO_GRAPH_SIM_SHADOW_ENABLED=1 APOLLO_CLARIFICATION_ENABLED=1 \
         APOLLO_NLI_ENABLED=1 APOLLO_NLI_PREWARM=1 APOLLO_GRAPH_GRADER_LIVE=0 \
         APOLLO_GRADING_ARTIFACT_ENABLED=1 HF_HOME=./.hf-cache \
         NEO4J_URI="bolt://127.0.0.1:57687" NEO4J_USERNAME="neo4j" \
         NEO4J_PASSWORD="campaignpass" NEO4J_DATABASE="neo4j"
  .venv-torch/Scripts/python.exe -m uvicorn server:app --host 127.0.0.1 --port 8000
  ```
- **Currently running as**: stub PID 69956 (`.venv-torch` launcher) ->
  real worker PID 94644 (anaconda3 image, venv site-packages active).
  Verify liveness: `curl -s http://localhost:8000/healthz` -> `{"status":"ok"}`.
  Log evidence captured this run: `apollo_nli_prewarm_complete seconds=6.53`
  (warm-cache boot; cold boot was `seconds=49.09` per an earlier standalone
  `prewarm_nli` run — see `campaign/README.md`).
- stdout/stderr redirected to `/tmp/uvicorn.log` (POSIX path under Git Bash;
  `C:\Users\ultra\AppData\Local\Temp\...` in some tool contexts — if that
  file is gone, the process is still running, just re-tail via `docker`-style
  `/proc` isn't available on Windows; use `curl healthz` as the liveness
  check instead of the log file).

## Data already provisioned (do not re-provision from scratch)

- `aita_search_spaces` id=1, slug=`campaign-course` — the one course/search
  space every seeded row below hangs off.
- Teacher user: Supabase auth id `a24c9bb7-8469-4b4f-b05c-0f67bcc7149b`,
  email `campaign-teacher@example.com` / password `CampaignTeacher123!`,
  `course_memberships` row role=`teacher` for `search_space_id=1`. Mint a
  fresh bearer token any time via
  `POST http://127.0.0.1:57321/auth/v1/token?grant_type=password` with the
  anon key (`SUPABASE_API_KEY` in `.env.campaign`) and those credentials.
- Seeded subjects (via `campaign.cast.teacher.provision_seeded`):
  `fluid_mechanics` (concept_id=1, 41 entities) and `macroeconomics`
  (concept_id=2 `gdp_components` 38 entities, concept_id=3
  `nominal_vs_real_gdp` 24 entities). All projected to Neo4j `:Canon`.
- WU-AAS-authored `linear_motion`: real concept_id=5 (`slug='linear-motion'`,
  filed under the `fluid_mechanics` subject — see
  `provisioning-notes.md` Finding C), 37 `apollo_kg_entities` rows (heavily
  duplicated — Finding D), 2 promoted problems (`concept_problem_id` 11 and
  12, from authored-sets 2 and 3 respectively; authored-set 1 was a fully
  rejected dry run kept as evidence). Projected to Neo4j `:Canon` under
  `concept_id: 5` automatically by the provisioning orchestrator (no manual
  `seed_canon_projection` step needed for the WU-AAS path).
- `held_out_subject` — **not provisioned** (by design; F2 gate-phase-only
  per the plan).

## Verification commands (copy-paste)

```bash
curl -s http://localhost:8000/healthz
docker exec -i $(docker ps --filter "name=supabase_db_e2e-harness" -q) \
  psql -U postgres -d postgres -c "SELECT slug FROM apollo_subjects;"
docker exec -i apollo-campaign-neo4j cypher-shell -u neo4j -p campaignpass \
  "MATCH (n:Canon) RETURN n.concept_id, count(n)"
```

## Known gaps for the follow-on task

- `apollo_ingest_runs` has ZERO rows for this run — the authored-sets
  pipeline does not use that table (S2's `low_confidence_threshold`/
  `verify_path_fired` contract has no real data source here; see
  `provisioning-notes.md`).
- linear_motion persona files under
  `campaign/cast/personas/linear_motion/` were **not** reconciled to real
  minted keys (ambiguous 1:1 mapping — see `provisioning-notes.md`); they
  remain PROVISIONAL and routed via `validate.py::PROVISIONAL_SUBJECTS`.
- `campaign/orchestrate.py` does not exist on this branch (Phase F1/F2 not
  yet implemented) — `campaign/out/f1/run_s1_s2.py` is ad-hoc glue, not a
  reusable CLI.
