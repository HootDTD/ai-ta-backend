# F1c stack state — for the follow-on (F2) task

Live state as left running at the end of the F1c post-fix corpus re-run
(2026-07-02). **Do not stop these processes/containers** — reuse them.

## Worktree

`C:\Users\ultra\OneDrive\TA-test\.worktrees\e2e-harness` (repo `ai-ta-backend`,
branch `feat/apollo-e2e-campaign-harness`). Never touch the sibling worktree
at `C:\Users\ultra\OneDrive\TA-test\ai-ta-backend`. Nothing pushed.

## Docker containers (unchanged from F1a — see campaign/out/f1/stack-state.md)

- Supabase project `e2e-harness`: gateway `http://127.0.0.1:57321`, DB
  `postgresql://postgres:postgres@127.0.0.1:57322/postgres`, Studio :57323.
- Neo4j `apollo-campaign-neo4j`: bolt `bolt://127.0.0.1:57687`, http :57474,
  auth `neo4j`/`campaignpass`.

Both were left UP through the F1c reset — only the Postgres `public` schema
was dropped/re-migrated (`campaign.infra.reset.reset_all`, 35 migrations
re-applied through `035_apollo_learner_state_space_idx.sql`) and Neo4j was
`MATCH (n) DETACH DELETE n`-wiped, then everything re-provisioned.

## Backend server process (NEW code, post 3cf239f/3e38f25/7604612/cb8b6da)

- Launcher: `/tmp/boot_uvicorn_f1c.sh` (Git Bash; recreate from the "Exact
  command" block in `campaign/out/f1/stack-state.md` — identical env: the 7
  Apollo flags + NEO4J_* + `HF_HOME=./.hf-cache`, `.env.campaign` sourced).
- Running as: launcher stub PID **35040** (`.venv-torch\Scripts\python.exe`)
  -> real worker PID **80260** (anaconda3 image, venv site-packages active).
  NLI prewarm completed at boot (`apollo_nli_prewarm_complete seconds=12.91`).
- Log: `/tmp/uvicorn_f1c.log` (Git Bash path). Liveness:
  `curl -s http://localhost:8000/healthz` -> `{"status":"ok"}`.

## Python environments (unchanged from F1a)

- anaconda3 python (PATH default): scripts/judges/provisioning, NO torch.
- `.venv-torch/Scripts/python.exe`: backend server (torch/transformers).
- `.hf-cache/`: NLI checkpoint, offline-verified.

## Data provisioned this run (fresh mint, IDs differ from F1a!)

- `aita_search_spaces` id=1 slug=`campaign-course` (recreated by
  `campaign/out/f1c/bootstrap_course.py`).
- Teacher: auth id `a24c9bb7-8469-4b4f-b05c-0f67bcc7149b`
  (`campaign-teacher@example.com` / `CampaignTeacher123!`), membership
  role=teacher on search_space 1. **Beware**: the GoTrue admin list-by-email
  endpoint does NOT filter by email — derive user ids from the JWT `sub`
  claim (see bootstrap_course.py comment for the bug this caused).
- Seeded subjects (`provision_seeded`, now INCLUDING the 3e38f25
  misconception-bank step): `fluid_mechanics` (concept_id=1, 41 entities,
  bank=2 rows), `macroeconomics` (concept_id=2/3, 62 entities, bank=4 rows).
  All projected to Neo4j `:Canon` (103 nodes).
- WU-AAS `linear_motion`: concept_id=5 (`slug='linear-motion'`), 2 promoted
  problems (`concept_problem_id` 11=Problem 1(a), 12=Problem 1(b)) from
  authored-sets 1 and 2. Sets 3-5 are rejected-retry noise (see
  `provisioning-notes.md`). 37 entities under concept_id=5 (duplication
  defect unchanged from F1a Finding D).
- Student identities: one per persona, `f1c-<subject>-<persona-file>@campaign.local`
  / `CampaignStudentF1c123!`, auto-enrolled role=student.
- `held_out_subject`: NOT provisioned (F2 gate-phase-only, unchanged).

## Run artifacts (campaign/out/f1c/)

`config.json` (frozen tune-phase snapshot, sha
`11197bb69d8ae7f5559d27ad5b251fce83d19eec9263e570a5a39e2a88cbb386` — flags
correctly captured live this time, unlike f1's), `attempts.jsonl` (36
records), `s1..s5-results.json`, `GATE-REPORT.md`, `scoreboard.json`,
`adjudication-packets.jsonl`, `sanity-checks-output.txt`, chunk logs,
driver scripts (adapted copies of the f1 ones).

## Known gaps for F2 (new + carried over)

- **linear_motion grading is DOWN (systemic, NEW finding):** every
  `POST /apollo/sessions/{id}/done` for the WU-AAS-minted linear-motion
  concept 500s with `KeyError: 'variable_mapping'` at
  `apollo/resolution/candidates.py:106` (`_ENTRY_TYPE_TO_NODE_TYPE` has no
  mapping for the `variable_mapping` entity kind the WU-AAS mint produces
  — seeded subjects never mint that kind, so only WU-AAS subjects hit it).
  Chunk aborted after 2/2 systemic errors: linear_motion has ZERO ok
  attempts this run. Fix the mapping (or exclude varmap entries from
  reference-candidate assembly) before F2's held-out WU-AAS subject —
  it will hit the same crash.
- **Abstention is still 100%** (31/31 graph rows): `misconception_bank_empty`
  is gone (bank seeding works) but `unresolved_rate_above_threshold` binds
  every attempt — the known G2 resolver-recall blocker, not fixed by any of
  the F1c commits. Graph-graded (counterfactual) fraction is 0.0 everywhere.
- 3 per-attempt 422s (`/chat`), ALL on `vague_then_clarifies` personas —
  Apollo's parser/filter rejection path (semantic 422 handlers in
  `apollo/api.py`) trips on deliberately vague utterances. Not systemic but
  archetype-correlated: the vague archetype loses ~19% of its attempts.
- `apollo_ingest_runs` still has zero rows (S2 verify-path contract still
  has no real data source — carried from F1a).
- linear_motion persona files remain PROVISIONAL
  (`validate.py::PROVISIONAL_SUBJECTS`) — expected-ledger keys not
  reconciled to the real mint (same ambiguity as F1a, worse now that
  grading 500s before an artifact is even written).
- `campaign/orchestrate.py` still does not exist — all F1c drivers are
  ad-hoc copies under `campaign/out/f1c/`.
