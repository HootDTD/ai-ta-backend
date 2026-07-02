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

## NLI model local cache + boot-time pre-warm (Task C2)

The Apollo NLI resolver tier (`apollo/resolution/nli_adjudicator.py`,
default ON — see `docs/architecture/apollo.md`) lazily downloads its
Hugging Face checkpoint (`cross-encoder/nli-deberta-v3-large` by default,
~1.7GB) on first use. For a campaign run that first use must NOT be a live
grading request — seed the local `HF_HOME` cache once, ahead of time:

```bash
# 1. Seed the cache (first run downloads from Hugging Face; ~1.7GB, needs network)
HF_HOME=./.hf-cache python -m campaign.infra.prewarm_nli

# 2. Confirm the cache actually serves the model with NO network (this is the
#    contract campaign runs depend on) — HF_HUB_OFFLINE=1 makes huggingface_hub
#    refuse any network call, so this only succeeds if the checkpoint files are
#    genuinely local:
HF_HOME=./.hf-cache HF_HUB_OFFLINE=1 python -m campaign.infra.prewarm_nli
```

`.hf-cache/` is gitignored (large binary checkpoint files, machine-local).

**Verified 2026-07-02** (throwaway venv, `torch==2.6.0+cpu` /
`transformers==4.57.6` — installing these into the shared dev interpreter was
avoided; see the deviation note below): cold run against an empty
`HF_HOME` → `load_seconds=88.66`, `first_classify_seconds=0.47` (network
download happens inside `load_seconds`). Second run against the SAME
`HF_HOME` with `HF_HUB_OFFLINE=1` → `load_seconds=4.50`,
`first_classify_seconds=0.44` — i.e. loads from local disk in ~5% of the
cold-run time with zero network calls, confirming "no first-request HF
download" (spec `2026-07-01-system-scores-outputs-design.md` §5).

To also warm the backend PROCESS itself at boot (not just the on-disk
cache), set `APOLLO_NLI_PREWARM=1` before starting `uvicorn` — `server.py`'s
startup hook then calls `apollo.resolution.nli_adjudicator.prewarm()`
before the app accepts requests. Default is OFF (prod boot is unchanged);
a prewarm failure is logged (`apollo_nli_prewarm_failed`) and never blocks
boot.

```bash
set -a; source .env.campaign; set +a
export APOLLO_NLI_PREWARM=1
export HF_HOME=./.hf-cache
uvicorn server:app --host 127.0.0.1 --port 8000
```

## Config snapshot/freeze + run context (Task C3)

`campaign/config.py::CampaignConfig` captures EVERY grading tunable that
feeds a campaign attempt's composite score: rubric axis weights
(`apollo.overseer.rubric.AXIS_WEIGHTS`), the letter-grade bands
(`LETTER_BANDS`), the active NLI model + its tuned params
(`load_nli_params()`), the §6.6 abstention thresholds
(`apollo.grading.abstention.ABSTENTION_THRESHOLDS`), and the boolean feature
flags that route an attempt down a different code path (clarification loop,
autoprovisioning, graph-sim live/shadow, learner decay/janitor/negotiation,
misconceptions, OLM invites, session personalization, structured scrape).

`CampaignConfig.capture_live()` reads the current process env + code
constants. `freeze(config, path)` writes a hash-stamped `config.json`
(`config_sha` = sha256 of the canonical JSON snapshot); `load_frozen(path)`
reloads it and raises `ValueError` if the file was tampered with (recomputed
hash != stored hash).

`campaign/runctx.py::RunContext.create(run_id, phase, out_root=...)` builds
`campaign/out/<run_id>/` for one campaign run:

- `config.json` — the frozen config (written on first `"tune"` create; left
  untouched on a resumed tune run)
- `attempts.jsonl` — append-only attempt log (created empty, never truncated
  by a re-create, so resuming a run keeps prior attempts)
- `artifacts/` — per-attempt artifact output directory

Two phases:

- `phase="tune"` freezes (or reuses) the live config for `run_id`.
- `phase="gate"` REQUIRES a config already frozen by a prior tune run for the
  same `run_id`, and calls `assert_live_matches_frozen` — if the live
  environment (any `APOLLO_*` tunable) has drifted from what was frozen, it
  raises `ConfigDivergedError` and refuses to run. This is the safety
  contract: a gate run must execute against exactly the settings it was
  calibrated against, never silently re-tuned settings.

On success, `RunContext.create` also exports the frozen `config_sha` into
`os.environ["APOLLO_CONFIG_SHA"]` so the campaign driver (Task D3) can stamp
it into every artifact's `versions.weights_version` without needing a
reference back to the `RunContext` object.

Tests: `campaign/tests/test_config.py`, `campaign/tests/test_runctx.py` — no
Docker required (pure dataclass/JSON/env logic). 100% line+branch coverage.

## Teacher provisioning drivers (Task D1)

`campaign/cast/subjects.py` is the campaign's subject registry: two seeded
incumbents already authored on disk (`fluid_mechanics`, `macroeconomics`),
and two subjects that go through the real WU-AAS teacher upload path
(`linear_motion` — new for this task; `held_out_subject` — a placeholder
provisioned only during the Task F2 gate phase, never in tune mode).

`campaign/cast/teacher.py` has both provisioning verbs:

- `provision_seeded(subject_key, dsn)` replays
  `scripts/seed_apollo_concept_registry.py` →
  `scripts/seed_apollo_learner_model.py --subject-slug <slug>` →
  `scripts/seed_canon_projection.py` as subprocesses against a LOCAL
  campaign DSN (e.g. `postgresql+asyncpg://postgres:postgres@127.0.0.1:57322/postgres`).
- `provision_authored(...)` drives the REAL teacher path end-to-end: a
  multipart problem+solution PDF upload to `POST /apollo/authored-sets`
  (bearer-token auth, `require_course_teacher`-gated), polls
  `GET /apollo/authored-sets/{set_id}` to a terminal status (`done` /
  `failed`), then approves every problem the orchestrator held for review
  via `POST .../problems/{problem_id}/approve`.

`campaign/cast/materials/generate_fixtures.py` builds the tiny (single-page,
plain-text) PDF pair checked in for the new `linear_motion` subject —
`campaign/cast/materials/linear_motion_{problem,solution}.pdf` — via
PyMuPDF (`fitz`, already pinned in `requirements.txt`); regenerate with
`python -m campaign.cast.materials.generate_fixtures` if the fixture
content ever needs to change.

Both drivers are pure request/flow logic over injected seams (a subprocess
runner for `provision_seeded`; an `httpx.AsyncClient` + sleep function for
`provision_authored`) and are unit-tested with fakes/`httpx.MockTransport`
in `campaign/tests/test_teacher_cast.py` — no Docker, DB, or running
backend required. **Not run against a live stack by this task** (that is
Phase F); the default *real* implementations of those seams
(`asyncio.create_subprocess_exec`, `asyncio.sleep`) are the only
pragma-excluded lines.

## Student personas with expected-ledger briefs (Task D2)

`campaign/cast/personas/` is the authored corpus of agent-student teaching
scripts. `schema.py` defines the pydantic contract:

- `ExpectedLedger{credited, unresolved, misconceptions, expects_clarification}`
  — the ledger outcome a persona attempt is authored to produce, keyed by
  real reference `canonical_key`s (`eq.*`/`cond.*`/`simp.*`/`def.*`/`proc.*`
  for nodes, `misc.*` for misconceptions — the same `entity_key` convention
  `apollo/subjects/AUTHORING.md` documents). `ExpectedLedger.to_ledger_dict()`
  converts to the exact `{credited, unresolved, misconceptions}` dict shape
  `campaign.judges.s3_student_fidelity.ledger_vs_expected` consumes as its
  `expected` argument — S3 is the judge that diffs the actual node ledger
  against this per-attempt ground truth (spec §4 "most important audit").
- `PersonaAttempt{persona, subject, concept, problem_id, system_prompt,
  scripted_beats, clarification_policy, expected}` — one authored campaign
  attempt. The four archetypes (spec §5):
  - `strong` — teaches every reference node correctly (`expected.credited`
    = every node).
  - `partial` — teaches a subset and silently omits the rest (no ambiguous
    utterance for the omitted nodes — they just never come up; omitted keys
    land in `expected.unresolved`).
  - `misconception` — teaches most nodes correctly but asserts one
    misconception-bank entry (using one of its real `trigger_phrases`)
    instead of the node it `opposes`; that node's key moves to
    `expected.misconceptions`, not `expected.credited`/`unresolved`.
  - `vague_then_clarifies` — teaches most nodes correctly but is
    deliberately non-committal on one node (`expected.expects_clarification`
    is schema-enforced `True` for this archetype), forcing Apollo's
    clarification loop; `clarification_policy` (`answer_correctly` /
    `answer_wrong` / `stay_vague`) decides whether that node ends up
    credited (via the `clarification` resolution method) or unresolved.

`validate.py` cross-checks every authored persona file against the REAL
subject data on disk — `reference_keys_for()`/`misconception_keys_for()`
load the actual `apollo/subjects/<subject>/concepts/<concept>/` JSON (never
a hand-mintable key list), so an authoring typo or a subject-data edit that
drops a key fails the `test_whole_authored_corpus_validates_clean` test
loudly instead of silently poisoning S3/S4. Problems are looked up by their
internal `id` field (the real runtime problem identifier, e.g.
`bernoulli_horizontal_pipe_find_p2`), not by the positional
`problem_01.json` filename.

**Corpus authored so far** (`campaign/tests/test_persona_schema.py` gates
all of this):

| Subject | Personas | Notes |
|---|---|---|
| `fluid_mechanics` | 16 | all 5 `bernoulli_principle` problems; all 4 archetypes present |
| `macroeconomics` | 18 | all 5 problems across `gdp_components` + `nominal_vs_real_gdp`; all 4 archetypes present |
| `linear_motion` | 4 | one archetype each — see deviation note below |

`held_out_subject` has **no** persona files yet — per plan D2/F2, held-out
personas are authored only after that subject is minted during the Task F2
gate phase (its subject key isn't even chosen yet).

**Deviation — `linear_motion` is PROVISIONAL, not fully real.** The plan
(D2) defers WU-AAS persona authoring to F2 "after those subjects are minted
(their canonical keys don't exist yet)". This task's brief asked for
concrete `linear_motion` personas now, but the real WU-AAS ingest path is
LLM-driven parsing — there is no `apollo/subjects/linear_motion/` tree to
validate against because the actual canonical keys aren't determined until
the real PDF (`campaign/cast/materials/linear_motion_*.pdf`, Task D1) is
actually uploaded and parsed. To ground *something* real now rather than
inventing free-floating keys, `campaign/cast/personas/linear_motion/
reference/kinematics_constant_acceleration/` hand-authors a provisional
reference solution + misconception bank that mirrors the exact worked
arithmetic in the fixture PDF (`v = v0 + a*t`, `x = v0*t + (1/2)*a*t^2`)
using the same `entity_key` convention. `validate.py`'s
`PROVISIONAL_SUBJECTS = frozenset({"linear_motion"})` routes lookups there
instead of `apollo/subjects/`. **This is explicitly not guaranteed to match
the keys the real LLM parser assigns once F2 actually mints the subject** —
reconcile (or regenerate) these 4 persona files against the real minted set
in F2; until then treat `linear_motion` attempts as a schema/pipeline smoke
test, not a calibration input. Only 4 personas (one per archetype) are
authored for it — the single fixture problem doesn't support the 15-25
range the plan wants per subject; that range is met by `fluid_mechanics`
(16) and `macroeconomics` (18), the two subjects with real reference graphs
to author a full corpus against. Per-attempt paraphrase variation at D3
session-driver runtime (not yet built) is expected to multiply the
effective attempt count per subject beyond this fixed brief count.

## S1-S5 stage-audit judges (Task E1)

`campaign/judges/` implements the spec §4 validation philosophy — "validate
stages, not vibes": each judge sees ONLY its own stage's input/output and
returns a `Verdict(item_id, ok, reason)` per item plus a deterministic
`JudgeResult(stage, verdicts, passed, total, pass_rate, extra)`. Gate-bar
comparison (95/95/95/90/90%-precision from the spec table) is left to E3's
`campaign/report.py` — this package only produces pass rates + evidence.

`campaign/judges/base.py`:

- `verdict_schema(name)` — the strict, closed `{ok: bool, reason: str}`
  `json_schema` payload every judge shares (mirrors the provisioning
  precedent, `apollo/provisioning/provisioning_schema.py`'s strict builders).
- `JudgeLLM` — the async seam (`judge_item(system_prompt, user_prompt,
  schema) -> dict`) every judge calls through; `OpenAIJudgeClient` is the
  live implementation (one `gpt-4o` chat call per item, offloaded to a
  thread so the async judge pipeline never blocks). Its network call is
  `# pragma: no cover` — never exercised by unit tests, matching the D1
  precedent of pragma-excluding only the real I/O seam.
- `StageJudge` — the base class: `build_items(raw)` (pure, unit-tested
  without an LLM), `user_prompt(item)`, `judge(raw)` (drives the LLM calls +
  aggregation). `aggregate(verdicts)` is the shared gate math
  (`pass_rate = ok/total`, and an empty item set aggregates to `0.0`, never
  a vacuous 100%).

One module per stage, each taking already-loaded run-dir data (a judge never
does its own file I/O — callers load `attempts.jsonl` / subject dumps via
`load_jsonl` and hand in plain dicts, keeping `build_items` trivially unit
testable against fixtures):

- **S1** (`s1_reference_graph.py`) — items = every node + edge of each
  provisioned subject's minted reference graph, judged against that
  subject's problem statement. Duplicate-node-id and PRECEDES-cycle checks
  are CODE (`find_structural_defects`, a Kahn's-algorithm pass mirroring
  `KGGraph.topological_order`), never sent to the LLM.
- **S2** (`s2_ingestion.py`) — items = WU-AAS `(page_ref, scraped_label,
  paired_solution)` triples; the low-confidence -> verify-path contract
  (`check_verify_path_fired`) is a pure boolean read-back against each
  item's recorded `ocr_confidence`/`low_confidence_threshold`/
  `verify_path_fired`, folded into the same pass rate as the LLM verdicts.
- **S3** (`s3_student_fidelity.py`) — **the most important audit**: one item
  per `credited`/`misconception`/`unresolved` node-ledger entry, judged
  against the FULL attempt transcript (catches both phantom credits and
  missed/resolver-recall gaps). `ledger_vs_expected` is a separate pure
  diff against the persona's authored `ExpectedLedger` (D2) — reported via
  `JudgeResult.extra["ledger_vs_expected"]`, not blended into the LLM pass
  rate (it audits persona-authoring agreement, a different question).
  Skips ledger entries with statuses this stage does not audit (e.g. no
  status yet) rather than raising.
- **S4** (`s4_apollo_coherence.py`) — one item per SAMPLED SESSION (the
  spec's bar is "coherent on >=90% of sampled sessions", not per-utterance):
  did Apollo's confused-learner questions/clarifications target what the
  ledger later marks unresolved/misconceived, and did grading honor
  clarification credits.
- **S5** (`s5_misconceptions.py`) — one item per asserted misconception
  (precision-focused, per spec). `misconception_recall` is a separate pure
  diff against `expected.misconceptions`, reported via
  `JudgeResult.extra["recall"]` and explicitly NOT gated (spec: "recall
  reported, not gated").

Tests: `campaign/tests/test_judges.py` — a `FakeLLM` records every call and
returns canned/overridable verdicts; nothing touches the network. 99%
line+branch coverage (the two uncovered branches are defensive loop-exit
edges with no observable behavior difference).

## Gate evaluation report generator (Task E3)

`campaign/report.py` computes every spec §4 promotion gate for one campaign
run and emits `GATE-REPORT.md` + `scoreboard.json`. It is pure logic — no DB,
no Neo4j, no LLM, no filesystem in `build_report()` itself — so it is fully
unit-tested against fixture data (`campaign/tests/test_report.py`, 100%
line+branch coverage).

**Deviation from the plan sketch:** this worktree branched directly off
`staging`, not off `feat/apollo-canonical-artifact` (Phase A/B ran in a
different worktree in parallel). `apollo.persistence.models.GradingArtifact`,
`apollo.grading.composite`, and `apollo.projections.scorecard` therefore do
not exist on this branch. `build_report()` takes plain-dict attempt records
shaped like the eventual canonical-artifact/scorecard payload instead of
importing those modules (see the module docstring for the exact shape); once
the branches are combined, a thin adapter can map real `GradingArtifact` rows
into that same dict shape without touching this module's gate logic. The
`BANDS` student-scorecard thresholds are likewise a local copy of the B1
design (Strong/Proficient/Developing/Beginning at 0.85/0.70/0.50/0.0) since
`apollo.projections` isn't importable here yet.

Inputs `build_report()` takes (all plain data, matching the E1/E2 output
shapes and `campaign.config.CampaignConfig`):

- `judge_results: Mapping[str, JudgeResult]` — one S1-S5 result per stage
  (Task E1's `campaign.judges.*` output). A missing stage is reported and
  gated as a failure, never silently skipped.
- `attempts: Sequence[dict]` — one paired graph+LLM record per Done-click:
  `{attempt_id, subject, band, grading_latency_ms, shadow_succeeded,
  shadow_abstained, graph_composite, llm_composite}`.
- `adjudication_verdicts: Sequence[dict]` — Fable's per-packet output
  (`{attempt_id, verdict: "sane"|"not_sane"|"not_sane_harmful", reason}`;
  any verdict string containing `"harmful"` trips the zero-harmful gate).
- `config: CampaignConfig` + `config_sha: str` — recorded as report
  provenance (which weights/thresholds these numbers were measured under).
- `subject_kinds: Mapping[str, str]` — `{subject_key: "seeded"|"wu_aas"|"held_out"}`
  for the breadth gate (≥4 subjects incl. ≥1 WU-AAS + ≥1 held-out).
- `event_loop_stall_warnings: Sequence[str]` — backend stderr lines the
  driver captured, for the ops gate's stall check.

Gates computed (named constants `S1_BAR=0.95`, `S2_BAR=0.95`, `S3_BAR=0.95`,
`S4_BAR=0.90`, `S5_PRECISION_BAR=0.90`, `ADJUDICATION_SANE_BAR=0.95`,
`GRAPH_GRADED_BAR=0.70`, `LATENCY_P95_MS_BAR=15000`, `MIN_SUBJECTS=4`):
S1-S5 stage pass rates vs. their bars; Fable sane-rate + zero-harmful;
per-subject graph-graded fraction — computed counterfactually as
`shadow_succeeded and not shadow_abstained` so shadow-mode tuning runs (where
the LLM is always served) still measure the metric the promotion decision
needs; ops (p95 `grading_latency_ms` ≤ 15s, zero stall warnings); breadth
(≥4 subjects incl. WU-AAS + held-out). `paired_comparison()` reports (not
gates) band-agreement rate, mean signed delta, and the top-10 most divergent
attempts. An empty judge/adjudication/attempt set never silently reads as a
pass — zero items always fails its gate.

`write_report(report, out_dir)` writes `GATE-REPORT.md` (human-readable,
failures listed as the literal next work queue per spec §5 exit criteria)
and `scoreboard.json` (machine-readable) under `out_dir`.
