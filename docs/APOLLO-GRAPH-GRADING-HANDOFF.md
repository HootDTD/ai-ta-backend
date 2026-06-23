# Handoff: Apollo Neo4j graph-grading — works coarsely, structural edge scoring is dead

**Date:** 2026-06-22
**Working tree branch when written:** `fix/apollo-provisioning-prompts` (the
*provisioning-fix* session's branch — see "Separation" below). All artifacts here
are **new untracked files**; nothing tracked was modified.
**How found:** first controlled end-to-end test of the Apollo grading path
("graph RAG") on the local stack, seeding the 5 hand-authored Bernoulli problems
as a mock question bank to **bypass the broken auto-provisioning generation**
(that generation work is tracked separately in
`APOLLO-AUTOPROVISION-LLM-STUBS-HANDOFF.md`).

---

## TL;DR

The Apollo grading pipeline (parser → student KG in Neo4j → Done read-back →
reference graph → coverage + graph-sim → rubric → diagnostic → Layer-3 belief
writes → `:Canon` projection) is **fully wired and runs end-to-end against real
OpenAI**, and at the **letter-grade level it discriminates correctly** (a strong
explanation gets B+, a weak one gets F — 3/3 runs). **But the *graph* part of the
grade does no work**, plus several sub-systems are miscalibrated:

1. **Structural edge scoring is dead by construction** — `edge_coverage_score` and
   `dependency_score` are **0 in every run, even for a strong graph that has
   edges**. Root-caused below; this is the headline issue.
2. **graph-sim abstains on the *better* answer** (`abstained=True` on strong,
   `False` on weak) → the two students get graded by different engines.
3. **XP is inverted** — an F earned more XP than a B+ on a clean first-attempt run.
4. **The classic LLM coverage matcher is noisy** — identical text scored 4/4 then
   2/4.
5. **Correct answers ceiling at B+** (no path to A).

**Verdict: coarsely good, not yet trustworthy.** The single highest-leverage fix
is #1 (the structural grade is the whole justification for a graph approach).

---

## Goal & scope

Test "how the Neo4j graph RAG grading works and whether it is good," **without**
waiting on the broken question-generation LLM. We injected a real mock question
bank (the 5 schema-valid Bernoulli problems already in `apollo/subjects/`) as
tier-2, then drove real teaching sessions with a **strong** (near-complete) and a
**weak** (partial) explanation and compared the grades + the underlying Neo4j /
Postgres evidence.

## Local environment (what's running)

- **Web API** (uvicorn) on `:8000`; **Neo4j 5.25** container `hoot-neo4j-local`
  (Bolt `:7687`, Browser `:7474`, `neo4j/local_password123`); **Supabase CLI
  stack** (Postgres `:54322`, Studio `:54323`).
- `.env.local` (gitignored) points everything at local infra and has the
  **graph-sim core already ON**: `APOLLO_GRAPH_SIM_SHADOW_ENABLED=true`,
  `APOLLO_GRAPH_SIM_LIVE_ENABLED=true` (the graph-sim rubric *replaces* the
  student-facing one), `APOLLO_GRAPH_SIM_LAYER3_ENABLED=1`,
  `APOLLO_MISCONCEPTION_ENABLED=1`, `APOLLO_SESSION_PERSONALIZATION_ENABLED=1`.
  Real `OPENAI_API_KEY` comes from `.env` (untouched).
- Venv: `ai-ta-backend/.venv` (Python 3.12, neo4j 5.28, asyncpg, etc.).

## Current seeded state (verified 2026-06-22)

- `apollo_concept_problems`: **5 tier-2** Bernoulli problems (concept_id=3,
  `bernoulli_principle`, course `search_space_id=1` = `smoke-fluids`) + 4 leftover
  tier-1 junk rows from the failed provisioning run (ignored).
- `apollo_kg_entities`: **41** Layer-1 entities; `:Canon`: **41** Neo4j nodes.
- `apollo_graph_comparison_runs`: 5 (probe attempts 1-5).
- Probe users (dedicated, namespaced): `apollo.probe.strong*@example.com`,
  `apollo.probe.weak*@example.com`, all enrolled in course 1.

## How it was set up (reproducible from a clean DB)

Seed chain (idempotent, no LLM, bypasses provisioning). Run from `ai-ta-backend`
with `DATABASE_URL` = local async URL and `NEO4J_*` set (or dot-source
`scripts/load_local_env.ps1`):
```bash
DATABASE_URL="postgresql+asyncpg://postgres:postgres@127.0.0.1:54322/postgres"
# 1) concept + 5 problems -> tier-2 (ConceptProblem.tier ORM default=2)
python -m scripts.seed_apollo_concept_registry
# 2) Layer-1: 41 entities, 16 prereqs, annotate problem payloads (needed for
#    :Canon AND the graph-sim fixture gate). --no-write-disk keeps tracked JSON clean.
python -m scripts.seed_apollo_learner_model --no-write-disk
# 3) project :Canon nodes into Neo4j (reads Layer-1)
NEO4J_URI=bolt://127.0.0.1:7687 NEO4J_USERNAME=neo4j NEO4J_PASSWORD=local_password123 \
NEO4J_DATABASE=neo4j python -m scripts.seed_canon_projection
```

## The reusable harness — `scripts/apollo_grade_probe.py` (NEW)

Self-contained driver. Provisions two **dedicated cold-start users** (so session
personalization serves both the same problem), enrols them in the bernoulli
course, drives `from_hoot → chat×N → done → end` via the live API, then dumps
Neo4j + `apollo_graph_comparison_runs`/`_findings` evidence to
`scripts/apollo_grade_probe_report.json`. Loads `.env`+`.env.local` itself.
```bash
python scripts/apollo_grade_probe.py --mode both --tag .r3   # fresh tag = fresh first-attempt users
python scripts/apollo_grade_probe.py --mode strong           # one mode
```
Served problem (deterministic intro pick, sorted by id, cold-start personalization)
= **`bernoulli_height_change_find_v2`** (problem_02): reservoir-drain / Torricelli,
v2≈19.8 m/s. Reference graph = 4 nodes: `bernoulli`(eq) → `equal_pressure_
simplification`(simp) → `plan_apply_equal_pressure_simplification`(proc) →
`plan_set_v1_zero_and_solve_bernoulli`(proc). Strong = 3 turns teaching all of it;
weak = 1 turn giving only the Bernoulli equation.

## Results (clean first-attempt run, `--tag .r2`)

| signal | strong | weak | discriminates? |
|---|---|---|---|
| student-facing rubric overall | **80 (B+)** | **0 (F)** | ✅ |
| graph-sim `coverage_score` | 0.50 | 0.25 | ✅ |
| graph-sim `bisimilarity_score` | 0.667 | 0.40 | ✅ |
| graph-sim `node_coverage_score` | 0.50 | 0.25 | ✅ |
| graph-sim `edge_coverage_score` | **0.0** | **0.0** | ❌ dead |
| graph-sim `dependency_score` | **0.0** | **0.0** | ❌ dead |
| graph-sim `usage_score` / `scoping_score` | 1.0 / 1.0 | 1.0 / 1.0 | ❌ vacuous |
| graph-sim `abstained` | **True** | False | ⚠️ abstains on better answer |
| Neo4j student nodes / edges | 7 / `DEPENDS_ON,USES,SCOPES` | 2 / `USES` | ✅ written+read |
| `graded_at` stamped | all | all | ✅ retention works |
| Layer-3 mastery_events | 0 (abstained) | 4 | ⚠️ inconsistent |
| **xp_earned** | **36** | **44** | ❌ **inverted** |

Classic LLM coverage (`overseer/coverage.py`, separate engine) was noisy:
identical strong text scored **4/4, then 2/4, then 2/4** across three runs; the
`plan_apply_equal_pressure_simplification` step's partial score swung 0.7 → 0.0.

## Findings

1. **Graph topology isn't graded** (the big one — root-caused below).
   `edge_coverage` + `dependency` = 0 in every run incl. the strong graph that
   *has* `DEPENDS_ON`+`USES` edges. Discrimination comes only from node coverage +
   procedure-order.
2. **Abstains on the stronger answer.** `abstained=True` for strong, `False` for
   weak; abstained strong wrote 0 Layer-3 mastery events, weak wrote 4. Check the
   abstention trigger and `normalization_confidence` (0.75 both).
3. **XP inverted.** First-attempt F earned 44 XP vs B+'s 36. `apollo/overseer/xp.py`
   should derive XP from the final promoted overall score, after graph-sim LIVE
   replacement — it currently tracks the noisy classic procedure-coverage mean.
4. **Noisy coverage matcher** (`apollo/overseer/coverage.py`) — high variance +
   false negatives even at temperature 0.
5. **B+ ceiling** for a fully-correct answer.
6. **What works:** tier-2 selection, parser→Neo4j per-attempt `:_KGNode` writes,
   Done read-back, reference-graph derivation, `graded_at` retention stamps,
   `:Canon` projection (41), comparison-run persistence, and an accurate diagnostic
   narrative that names the missing steps.

---

## ROOT CAUSE: why `edge_coverage_score` & `dependency_score` are always 0

Traced through `apollo/graph_compare/` (`graph-compare-v1`). Edge matching keys on
`(edge_type, from_key, to_key)` in canonical-key space (`scores.py:79-101`,
`core.py:174-188`). **Not a crash — the edge dimensions are dead by construction.**
Two compounding causes, both confirmed by persisted findings:

**Cause 1 — the reference canonical graph only ever has `DEPENDS_ON` edges.**
`build_reference_canonical` (`canonical.py:142-154`) builds edges **solely** from
each step's `depends_on` array. It discards the `USES` (`uses_equations`) and
procedure-order (`PRECEDES`) structure that the problem encodes — structure that
`Problem.to_kg_graph` *does* emit and that the parser *does* extract from students
(strong attempt had `USES:1, SCOPES:3, DEPENDS_ON:1` in Neo4j). Consequence:
- `usage_score`/`scoping_score` are **vacuously 1.0** (no reference edges of that
  type → `scores.py:92-93`) — meaningless, not real matches;
- `edge_coverage` (all ref edges) collapses onto the same `DEPENDS_ON`-only set as
  `dependency` (redundant), and the student's real `USES`/`PRECEDES` edges have
  nothing to match against.

**Cause 2 — the one live edge dimension is the authored dependency DAG, which
students never restate.** Matching a reference `DEPENDS_ON` edge needs the student
to emit a `DEPENDS_ON` edge between the *same two resolved canonical keys* (e.g.
`eq.bernoulli → simp.equal_pressure_simplification`). Teaching in prose, students
state concepts, not dependency edges; and any student edge touching an unresolved
node is **dropped** (`canonical.py:225-241`).

**Decisive evidence (persisted; attempt_id=4 strong / 5 weak):**
```
apollo_graph_comparison_runs:
  strong  nc=0.5  ec=0 dep=0 sc=1 us=1 cov=0.5  abstained=t
  weak    nc=0.25 ec=0 dep=0 sc=1 us=1 cov=0.25 abstained=f
apollo_graph_comparison_findings:
  strong = 4 covered_node, 4 missing_edge, 3 unresolved, 0 matched_edge
  weak   = 1 covered_node, 3 missing_node, 4 missing_edge, 0 matched_edge
  missing_edge (both) = problem_02's 4 authored DEPENDS_ON deps, e.g.
    eq.bernoulli -DEPENDS_ON-> simp.equal_pressure_simplification
```
The strong attempt **covered all 4 reference nodes** yet matched **0 of 4**
reference edges — it had only 1 `DEPENDS_ON` edge (vs 4 needed) and 3 nodes
unresolved. (Run-row `nc=0.5` is the *pre-audit* score; the 4 `covered_node`
findings include 2 transcript-audit upgrades — persistence stores the audited
finding set, see `apollo/grading/persistence.py` docstring. Unrelated to edges.)

**Smoking gun:** `scores.py` computes `usage`/`scoping` dimensions as if the
reference could carry `USES`/`SCOPES` edges, but `build_reference_canonical` never
creates them → those dimensions are permanently dead.

### Fix direction (recommended: A; NOT yet applied)

- **A — make `build_reference_canonical` edge-symmetric with `Problem.to_kg_graph`:**
  also emit `USES` edges (from `uses_equations`) and `PRECEDES` edges (from
  procedure `order`). Then the student's real `USES`/`PRECEDES` edges can match →
  `usage`/`edge_coverage`/`procedure_order` get true signal instead of vacuous-1.0
  / structural-0. Files: `apollo/graph_compare/canonical.py` (builder),
  `apollo/graph_compare/scores.py` (verify weighting), tests in
  `apollo/graph_compare/tests/`. Add a regression test: a student graph WITH
  matching `USES`/`PRECEDES` edges must score those dims > 0.
- **B — stop a dead dimension dragging the grade:** drop / near-zero-weight
  `dependency` in the rubric aggregation (WU-4C; its own docstring calls it
  "LOWEST weight"), since authored `DEPENDS_ON` isn't student-reproducible.
- Avoid C (inferring student `DEPENDS_ON` from the reference DAG) — inflates scores
  and defeats the measurement.

---

## Recommended next steps (prioritized)

1. **Fix the edge/dependency structural grade** (root cause above) — do **A**, add
   the regression test, re-run `apollo_grade_probe.py` and confirm
   `edge_coverage_score > 0` for the strong graph. **Highest leverage.**
2. **Re-point XP at the final overall score** (`apollo/overseer/xp.py`) and re-run
   to confirm B+ > F in XP.
3. **Re-examine the abstention rule** so it doesn't fire on rich/correct graphs;
   check its interaction with `normalization_confidence`.
4. **Stabilize / de-noise the coverage matcher** (`apollo/overseer/coverage.py`),
   or lean on the (more stable) graph-sim metrics for the student grade.
5. **Promote `apollo_grade_probe.py` into a CI regression** asserting
   `strong.overall > weak.overall` and non-zero edge dims.

## Artifacts & pointers

- **Driver:** `ai-ta-backend/scripts/apollo_grade_probe.py` (`--mode`, `--tag`).
- **Latest run dump:** `ai-ta-backend/scripts/apollo_grade_probe_report.json`.
- **Seed scripts:** `scripts/seed_apollo_concept_registry.py`,
  `scripts/seed_apollo_learner_model.py`, `scripts/seed_canon_projection.py`.
- **Grading code:** `apollo/graph_compare/{canonical,scores,core,coverage,
  bisimilarity,soundness,findings}.py`; orchestration `apollo/handlers/
  done.py` + `done_grading.py`; persistence `apollo/grading/persistence.py`;
  classic path `apollo/overseer/{coverage,rubric,diagnostic,xp}.py`.
- **DB evidence:** `apollo_graph_comparison_runs` / `apollo_graph_comparison_findings`
  (graph-sim audit), `apollo_mastery_events` (Layer-3).
- **Neo4j:** per-attempt `(:_KGNode {attempt_id})` student subgraphs + `:Canon`
  nodes. Browser `:7474`. `MATCH (n:_KGNode {attempt_id:4}) RETURN n;`
- **Owner doc:** `docs/architecture/apollo.md` (owns `apollo/`) still describes
  grading as built — should gain a "structural edge dims dead by construction"
  note when this is fixed.

## Separation note

The working tree is currently on `fix/apollo-provisioning-prompts` (a parallel
session is fixing the question-generation prompts — different files:
`apollo/provisioning/{orchestrator,solution,tag_mint,pairing_gate,promote}.py`).
Everything in this handoff is **read/drive-only + new untracked files**; no
provisioning file was touched and no worker/web process was restarted. The probe's
DB/Neo4j writes live under dedicated `apollo.probe.*` users / fresh sessions and
do not interfere. If you want these probe artifacts on their own branch, they're
uncommitted — `git stash`/checkout a fresh branch and commit there.

## Not yet verified / open questions

- Whether **Fix A** actually lifts the structural dims depends on the parser
  reliably extracting `USES`/`PRECEDES` student edges whose endpoints *resolve* to
  the reference keys — spot-check a built `S_norm` before trusting the numbers.
- The XP inversion was reproduced once cleanly; confirm the exact formula path
  (`xp.py` input) before fixing.
- Only `bernoulli_height_change_find_v2` (intro) was exercised. The other 4
  problems (incl. the multi-equation `bernoulli_full_find_p2`, standard) and a
  deliberately misconception-laden explanation are untested — good next stressors.
