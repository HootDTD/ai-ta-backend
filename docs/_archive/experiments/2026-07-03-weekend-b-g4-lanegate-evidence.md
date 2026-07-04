# B2 Lane-Gate Verification Evidence

**Date:** 2026-07-02
**Code under test:** worktree `wt-lanegate` @ `f6276f9`
(branch `weekend-local/b2-lanegate-combo` = staging + PRs #84,#85,#86,#87,#89;
combo commits include G3 shadow isolation `4045637`/`f8503bb`, D1 empty-bank
`73d9886`, G4 parser tolerance `cf7e14c`, G4 variable_mapping contract
`b18ffb8`, G4 entity dedup + equation payloads `1f9a471`).
**Stack:** local Supabase (`supabase_db_e2e-harness` @ 127.0.0.1:57322) +
Neo4j (`apollo-campaign-neo4j` @ bolt://127.0.0.1:57687), backend on combo
code at 127.0.0.1:8000.

**Verdict: GATE PASS on all five sub-checks.**

---

## Backend restart (combo code)

- Killed staging: `pkill -f "uvicorn campaign_launch:app"` (was PID 75132).
  A pre-existing stale `apollo_run_launch:app` (PID 83464, from Jun 22) holds
  the wildcard `*:8000`; my combo binds the more-specific `127.0.0.1:8000`,
  which wins for loopback requests (confirmed: all healthz/mint/persona
  requests land in the combo log).
- Launcher `campaign_launch_combo.py` (scratchpad): `sys.path` -> wt-lanegate;
  `.env.campaign` + `HF_HOME` kept at the MAIN repo (absolute paths).
- Start cmd:
  `SSL_CERT_FILE=$(.venv/bin/python -m certifi) .venv/bin/python -m uvicorn campaign_launch_combo:app --app-dir <scratchpad> --host 127.0.0.1 --port 8000`
  -> PID **85966**. `apollo_nli_prewarm_complete seconds=8.19`,
  `/healthz` = `{"status":"ok"}`.
- **Backend LEFT RUNNING on combo code (PID 85966) at end of run. DB NOT reset.**

---

## Step 1 — Mint (WU-AAS teacher path)

Driver: `b2_mint.py` (scratchpad). Minted a FRESH teacher token (the F1c-saved
`course-bootstrap.json` token was expired ~11h). Real path:
`provision_authored` -> multipart `POST /apollo/authored-sets` (problem+solution
PDF) -> poll to terminal -> approve held. Fixtures from wt-lanegate:
`campaign/cast/materials/linear_motion_{problem,solution}.pdf`.
`search_space_id=1` (B0 smoke state reused, NOT a new space).

### Terminal status
- **Attempt 1 (set_id=1): `status=done`, BOTH problems minted AND auto-promoted**
  (`concept_problem_id` 11, 12; `review_required=false`; `match_method=retrieval`;
  `solution_source=extracted`). `counts: {promoted:2, rejected:0, held_for_review:0}`.
  No retry needed. (Contrast F1a: 3 upload attempts; F1c: 2 attempts.)

### Entities per problem — vs F1 failure mode

| Metric | F1 (provisioning-notes.md) | **B2 combo** |
|---|---|---|
| concept filed under | `apollo_concepts.id=5` slug `linear-motion` | **id=5 slug `linear-motion`** (same) |
| KG entities for the 2 problems | **37** (≈2× bloat) | **17** (in F1's own "~15-18 meaningful" target) |
| dup candidates per role | 2–4 (eq_motion/eq_velocity_formula/eq1/eq2; def1..4 + named; varmap var1..4/vm1..3 + named; proc dups) | collapsed — 2 eq / 3 def / 3 proc / 7 var / 2 cond |
| equation payloads | **NULL for every row** (Finding E) | **populated** (see below) |
| distinct equations disambiguable | no (eq2 display_name said "final velocity") | yes ("Velocity equation" / "Distance equation") |

Kind counts under concept_id=5 (Postgres `apollo_kg_entities`):
`condition 2, definition 3, equation 2, procedure 3, variable 7 = 17`.
Neo4j `:Canon {concept_id:5}` = **17** nodes, kinds identical (projection consistent).

### Equation payloads present (F1 Finding E fixed)
```
eq.eq1  "Velocity equation"  payload.equation = "v = v_0 + a*t"
        symbolic="v = v_0 + a*t", variables=[v,v_0,a,t], provenance=[{node_id:eq1,...}]
        scope_summary = "equation -a*t + v - v_0 | kind equation"   <- sympy-parsed canonical form
eq.eq2  "Distance equation"  payload.equation = "d = v0*t + (1/2)*a*t**2"
        symbolic=..., variables=[d,v0,t,a], provenance=[{node_id:eq2,...}]
        scope_summary = "equation -a*t**2/2 + d - t*v0 | kind equation"
```
variable_mapping steps present as 7 `entry_type=variable_mapping` variable entities;
2 `simplification` conditions carry `applies_when`/`transformation`; 3
`procedure_step` procedures carry `order`. (Dedup collapse happens at mint-plan
time — `apollo/provisioning/tag_mint.py` `collapsed_entity_keys`, collapsing
pre-persist; that is why DB `aliases` are `[]` yet the count is 17 not 37. The
two distinct equations are correctly NOT collapsed.)

Full set JSON: `b2_set_1.json`. Raw mint log: `b2_mint.log`.

---

## Step 2 — Persona attempt (strong archetype)

Driver: `b2_persona.py` (scratchpad). ONE persona
`personas/linear_motion/strong__cyclist_accel_v_and_distance.json` through the
real session flow (reuses `campaign/cast/student.py` seams: HttpxApolloClient,
default_chat_fn (real OpenAI), SqlArtifactReader, mint_student_token,
run_attempt). `difficulty="intro"`.

- `status=ok`, `session_id=17`, `attempt_id=33`, `transcript turns=10`.
- `problem_matched=False` — EXPECTED: linear_motion is PROVISIONAL, the
  persona's authored `problem_id` never matches the scrape-hash minted problem
  ids (documented in run_f1c_corpus `_difficulty_for`). The `409 Conflict` on
  `/apollo/sessions/17/next` in `b2_persona.log` is the client-side `_resolve_problem`
  re-roll for that mismatch — caught, attempt proceeds. NOT a grader error.

---

## Step 3 — Gate verification

### 3a. `POST .../done` returns 200 (not 500) — **PASS**
Backend log: `127.0.0.1:60700 - "POST /apollo/sessions/17/done HTTP/1.1" 200 OK`.
`done_response` present with full grading payload (rubric, coverage, progress,
xp_*, scorecard). **Zero `500 Internal` across the entire combo run**
(`grep -c "500 Internal" backend-combo.log` = 0).

### 3b. Graph (pair/shadow) artifact exists and grader RAN — no crash — **PASS**
DB `apollo_grading_artifacts` for attempt 33 (both under concept_id=5):

| id | role | grader_used | abstained | graph_failure | reasons | composite |
|---|---|---|---|---|---|---|
| 33 | canonical | llm_fallback | (n/a) | — | [] | 0.56 |
| 34 | **pair** | **graph** | **true** | **null** | **["unresolved_rate_above_threshold"]** | 0.0 |

- Pair artifact `grader_used="graph"`, full `node_ledger` + `edge_ledger`
  populated. Reference node **`eq.eq1` CREDITED** (confidence 0.75,
  evidence_span `v = v_0 + a \cdot t`) — the grader matched the student's
  velocity equation to the minted entity **because the equation payload is now
  populated** (the exact reconciliation F1's NULL payloads blocked).
- `abstention = {abstained:true, reasons:["unresolved_rate_above_threshold"],
  graph_failure:null, normalization_confidence:1.0}` — abstained for a
  LEGITIMATE grading reason (the mandate's named example), **not** a crash.
- **No `shadow_failure:` marker. No variable_mapping KeyError. No
  GraphSimDegradation marker. No Traceback in the grading path.** (String scan
  of the full artifact JSON for shadow_failure/GraphSimDegradation/KeyError/
  variable_mapping/Traceback/degrad = all False.)
- `graph_sim_calibration` INFO logged (shadow grader executed). Grading
  latency 10329 ms.
- Acceptable marker recorded: `WARNING soundness_not_applicable_empty_bank`
  — the D1 fix: concept_id=5 has no `apollo_misconceptions` bank rows yet, so
  the soundness axis is marked not-applicable and graded normally (explicit
  marker, not a crash). This is expected for a freshly-minted concept.

### 3c. Reconciliation quality — honest persona-key reconciliation now POSSIBLE — **PASS**
- Entity count sane: **17 for 2 problems** (not 37).
- Dedup collapsed duplicates (2 distinct equations kept, same-role dups gone;
  `tag_mint.collapsed_entity_keys` mechanism).
- Equation payloads attached with symbolic forms + sympy-parsed canonical
  scope summaries.
- The pair grader demonstrably reconciled a real student utterance to a real
  minted reference node (`eq.eq1` credited). Full 1:1 persona-key scoring is
  not attempted here because the linear_motion personas remain PROVISIONAL
  (expected-ledger keys authored against a hand graph) — that is the documented
  provisional gap, not a mint defect. An honest reconciliation is now possible.

---

## Artifacts (scratchpad)
- `campaign_launch_combo.py` — combo launcher
- `b2_mint.py` / `b2_mint.log` / `b2_set_1.json` — mint
- `b2_persona.py` / `b2_persona.log` / `b2_persona_attempt.json` — persona attempt
- `backend-combo.log` — combo backend log
- this file: `b2-lanegate-evidence.md`
