# Apollo Shadow Grader — Node-Recovery E2E Test & Grading-Gap Feedback

**Date:** 2026-06-26
**Author:** Claude (Opus 4.8) via orchestrated subagents
**Scope:** End-to-end live test of the Apollo graph-sim *shadow* grader after the node-recovery hardening, on **both** the econ (macro) and fluids (Bernoulli) question sets, with full per-metric score capture + Neo4j inspection.
**Branch under test:** `probe/macro-econ-node-recovery` (sits on the 4 phase commits `a58bdbf`→`6b4dc5d` of `feat/apollo-grader-node-recovery`).
**Inputs:** `docs/_archive/handoffs/2026-06-26-apollo-grader-node-recovery-handoff.md`, spec `docs/superpowers/specs/2026-06-23-apollo-shadow-grader-node-recovery-design.md`.

---

## 0. TL;DR

- **I captured the clean live per-metric score matrix the handoff said was never obtained.** All **15 econ attempts** (IDs 90–104) graded end-to-end with `error: null`, `comparison_runs=1` each, zero timeouts/500s. The 120 s read-timeout that crashed the prior sweep was raised to 300 s and the run completed cleanly. **Fluids** also graded after unblocking a routing bug (2 attempts, IDs 105–106).
- **CRITICAL finding — the shadow grader abstains on 100 % of attempts (17/17), by construction.** Phase 1c (added by *this* work) introduced a normalization-confidence abstention floor of **0.85**, which sits **above** the `llm` (0.75) and `fuzzy` (0.80) resolution-tier caps. Conceptual nodes (procedure / condition / definition / simplification) have no symbolic form and can *only* resolve via `llm`/`fuzzy`, so `normalization_confidence` (a MIN over scored nodes) is pinned ≤ 0.80 for any attempt with procedural content → auto-abstain. Low-coverage attempts instead trip `unresolved_rate > 0.35`. **The two gates tile the entire performance space — even two perfect-coverage econ attempts (93, 96) abstain.** Consequence: the graph-sim grade can never be certified or promoted, so the spec §10 promotion-calibration is impossible until this is recalibrated.
- **Phase 1a (derived@0.95) genuinely fires live but is currently moot.** Confirmed on econ attempts 99/100 (the rearranged GDP-deflator equation resolved `derived`/0.95). But the attempts abstain anyway (gap above), and the same derived path did **not** fire on the fluids Bernoulli simplification/rearranged equation — so the node-recovery win is real, narrow, and has zero effect on any live grade today.
- **Misconception detection is largely non-functional**, **resolver recall (~38 % unresolved on econ, 5/7 on fluids-strong) is the upstream bottleneck**, and **`scoping`/`procedure_order` are vacuously 1.0** on all 17 attempts.
- **Reassurance / framing:** these are *shadow*-grader gaps. The live student-facing grade is still the LLM matcher (`overseer/coverage.py`→`rubric.py`), which discriminates correctly (fluids strong = 80 / B+, weak = 0 / F; econ coverage gradient strong > partial > weak). Even with `APOLLO_GRAPH_SIM_LIVE_ENABLED=true` locally, the 100 % abstention means the graph-sim grade never actually promotes — students always fall back to the LLM grade. No student-facing regression. The gaps are about the shadow grader's (currently blocked) path to becoming the production grader.

---

## 1. What the implementation is (recap)

The Apollo **graph-sim grader** is a SHADOW subsystem: it is the *intended* production grader but the live student grade is still produced by the LLM matcher. It resolves a student's free-text equations/procedures against a canonical reference graph (node-recovery), builds student + reference canonical graphs, and scores 10 dimensions, persisting one row per attempt to Postgres `apollo_graph_comparison_runs` and student-graph + `RESOLVES_TO` data to Neo4j.

The node-recovery work added four phases (all present on the branch under test):

| Phase | What it added | Status this test |
|---|---|---|
| 0 — resolver safety | type-gate so the LLM tier can't fire cross-type; explicit canonical merge-type; self-loop guard | not the focus; no violations observed |
| 1a — derived equation-alignment tier | symbolic solve-for-variable resolves algebraic rearrangements; cap **0.95** (`derived`) | **fires live (econ 99/100); did not fire on fluids Bernoulli** |
| 1b — exact-only alias channel | `exact_aliases` channel (`alias`, 0.92) separate from fuzzy | **never observed firing** (0 alias resolutions in 60+9 student nodes) |
| 1c — normalization-confidence abstention brake | abstain when `normalization_confidence < 0.85` | **fires on ~10/15 econ + 1/2 fluids; root cause of 100 % abstention** |

Method confidence-cap ladder: `exact 1.00 · symbolic 0.98 · derived 0.95 · alias 0.92 · fuzzy 0.80 · llm 0.75 · unresolved 0.00`. Abstention floor `0.85` (Phase 1c); also `unresolved_rate > 0.35`.

---

## 2. Test methodology

**Stack:** local Docker Supabase (Postgres :54322, migration 031 applied) + local Docker Neo4j (`bolt://127.0.0.1:7687`, 103 `:Canon` nodes = 62 econ ssid 3 + 41 fluids ssid 1) + venv Python 3.12.4 + real OpenAI calls. Flags: `APOLLO_GRAPH_SIM_SHADOW_ENABLED=true`, `APOLLO_GRAPH_SIM_LIVE_ENABLED=true`, `APOLLO_GRAPH_SIM_LAYER3_ENABLED=1`.

**Infra unblocks performed (test-only, on the throwaway probe branch / local DB):**
1. Started the exited local Neo4j container (`hoot-neo4j-local`) — the only boot blocker.
2. Raised the probe HTTP read-timeout `apollo_grade_probe.py::_post` 120 s → **300 s** (the fix for the crash that lost the prior matrix). This is the slow `/done` LLM-adjudicator call, not a grader change.
3. (Fluids only) Re-parented a junk autoprovisioned decoy concept (`bernoulli-equation`, id 4, 0 problems) off search-space 1 to unblock concept inference, **then reversed it** (final state byte-identical). See §5.

The `server.py` `.env.local` override (local-token fix) and migration 031 were already present. No remote DB was touched.

**What ran:**
- **Econ:** `scripts/run_macro_probe.py --skip-embed --skip-mining --tag .nr4` — self-boots uvicorn on :8001, seeds registry/learner-model/canon, drives **5 problems × {strong, partial, weak} = 15 attempts**. Output `scripts/_run_macro_nr4.log`; matrix `scripts/macro_probe_score_matrix.json` (snapshot `…nr4.json`).
- **Fluids:** manual uvicorn :8001 + `scripts/apollo_grade_probe.py --concept-slug bernoulli_principle --variations strong,weak --tag .fluids2` — **1 served problem (`bernoulli_height_change_find_v2`) × {strong, weak} = 2 attempts** (105–106). Only one fluids problem is scenario-wired (see §5/§7).

Each attempt = real LLM parse/reply/adjudication, a shadow graph-sim grade persisted to Postgres, and student graph + resolution written to Neo4j.

---

## 3. Results — Econ (macro), 15 attempts (IDs 90–104)

`cov`=coverage, `ncov`=node_coverage, `ecov`=edge_coverage, `use`=usage, `scp`=scoping, `ord`=procedure_order, `dep`=dependency, `snd`=soundness, `con`=contradiction, `bis`=bisimilarity, `nc`=normalization_confidence, `abst`=abstained, reason: **NC**=normalization_confidence_below_threshold, **UR**=unresolved_rate_above_threshold.

| id | problem / var | cov | ncov | ecov | use | scp | ord | dep | snd | con | bis | nc | abst | reason |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 90 | gdp_identity / strong | 0.80 | 0.80 | 0.43 | 1.0 | **1.0** | **1.0** | 0.0 | 1.0 | 1.0 | 0.89 | **0.75** | ✔ | NC |
| 91 | gdp_identity / partial | 0.60 | 0.60 | 0.14 | 0.5 | 1.0 | 1.0 | 0.0 | 1.0 | 1.0 | 0.75 | 0.75 | ✔ | NC |
| 92 | gdp_identity / weak | 0.20 | 0.20 | 0.0 | 0.0 | 1.0 | 1.0 | 0.0 | 1.0 | 1.0 | 0.33 | 1.00 | ✔ | UR |
| 93 | net_exports_sign / **strong** | **1.00** | **1.00** | 0.50 | 1.0 | 1.0 | 1.0 | 0.33 | 1.0 | 1.0 | **1.00** | **0.75** | **✔** | **NC** |
| 94 | net_exports_sign / partial | 0.67 | 0.67 | 0.25 | 1.0 | 1.0 | 1.0 | 0.0 | 1.0 | 1.0 | 0.80 | 0.75 | ✔ | NC |
| 95 | net_exports_sign / weak | 0.00 | 0.00 | 0.0 | 0.0 | 1.0 | 1.0 | 0.0 | 1.0 | 1.0 | 0.00 | 1.00 | ✔ | UR |
| 96 | nnp_chain / **strong** | **1.00** | **1.00** | 0.25 | 1.0 | 1.0 | 1.0 | 0.2 | 1.0 | 1.0 | **1.00** | **0.75** | **✔** | **NC** |
| 97 | nnp_chain / partial | 0.80 | 0.80 | 0.13 | 0.0 | 1.0 | 1.0 | 0.4 | 1.0 | 1.0 | 0.89 | 0.75 | ✔ | NC |
| 98 | nnp_chain / weak | 0.20 | 0.20 | 0.0 | 0.0 | 1.0 | 1.0 | 0.0 | 1.0 | 1.0 | 0.33 | 1.00 | ✔ | UR |
| 99 | real_gdp_from_deflator / strong | 0.67 | 0.67 | 0.25 | **1.0** | 1.0 | 1.0 | 0.0 | 1.0 | 1.0 | 0.80 | 0.75 | ✔ | NC |
| 100 | real_gdp_from_deflator / partial | 0.67 | 0.67 | 0.25 | **1.0** | 1.0 | 1.0 | 0.0 | 1.0 | 1.0 | 0.80 | 0.75 | ✔ | NC |
| 101 | real_gdp_from_deflator / weak | 0.00 | 0.00 | 0.0 | 0.0 | 1.0 | 1.0 | 0.0 | 1.0 | 1.0 | 0.00 | 1.00 | ✔ | UR |
| 102 | real_gdp_growth / strong | 0.50 | 0.50 | 0.0 | 0.0 | 1.0 | 1.0 | 0.0 | 1.0 | 1.0 | 0.67 | 0.75 | ✔ | UR+NC |
| 103 | real_gdp_growth / partial | 0.25 | 0.25 | 0.0 | 0.0 | 1.0 | 1.0 | 0.0 | 1.0 | 1.0 | 0.40 | 1.00 | ✔ | UR |
| 104 | real_gdp_growth / weak | 0.00 | 0.00 | 0.0 | 0.0 | 1.0 | 1.0 | 0.0 | **0.5** | **0.5** | 0.00 | 0.80 | ✔ | UR+NC |

**Abstention split:** NC-only 8 · UR-only 5 · both 2 · **abstained 15/15**. **Resolution methods across 60 student nodes:** unresolved 23 (38 %), exact 19 (32 %), llm 15 (25 %), **derived 2 (3 %)**, fuzzy 1 (2 %), **symbolic 0, alias 0**.

Forensic highlights (evidence in `apollo_graph_comparison_runs/_findings` + Neo4j `RESOLVES_TO`):
- **Perfect-coverage attempts still abstain.** 93 & 96 have coverage 1.0 yet `nc=0.75` because their condition/procedure/definition nodes resolved only via `llm` (0.75). The MIN drags the whole attempt below the 0.85 floor.
- **Phase 1a confirmed live (99):** `realGDP - nomGDP/(PI/100)` → `derived`/0.95 → `eq.gdp_deflator`; `usage=1.0` independently backed by the matched `proc.rearrange_for_real_gdp -USES-> eq.gdp_deflator` edge. Real win — but the attempt abstains anyway (`nc=0.75` from its proc node).
- **Strong under-credited (102):** node_coverage 0.50 although all 4 reference nodes were genuinely taught — `def.real_basis` and `proc.apply_percent_change` were correct but went **unresolved**, plus one numeric-only form unresolved by design. The LLM rubric marks the same attempt fully covered (`missing=[]`); the two graders disagree *exactly* on the unresolved nodes. Pure resolver-recall artifact, zero scenario omissions.

---

## 4. Results — Fluids (Bernoulli), 2 attempts (IDs 105–106)

Served problem `bernoulli_height_change_find_v2` (reference = 4 nodes: `eq.bernoulli`, `simp.equal_pressure_simplification` subst `{P2:P1}`, 2 procedure steps). Live LLM rubric **strong = 80 (B+), weak = 0 (F)** — discriminates fine.

| id | var | cov | ncov | ecov | use | scp | ord | dep | snd | snd_appl | con | bis | nc | abst | reasons |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 105 | strong | 0.50 | 0.50 | **0.0** | 0.0 | **1.0** | **1.0** | 0.0 | 0.5 | **false** | null | 0.50 | **0.75** | ✔ | UR, NC, misconception_bank_empty |
| 106 | weak | 0.25 | 0.25 | **0.0** | 0.0 | 1.0 | 1.0 | 0.0 | 0.25 | **false** | null | 1.00 | ✔ | ✔ | UR, misconception_bank_empty |

Neo4j (all student nodes `source=parser`):
- **105 strong:** 7 nodes, edges SCOPES:3 DEPENDS_ON:2 USES:1. **Only 2/7 resolved:** `eq.bernoulli`→`exact`/1.0, one ProcedureStep→`llm`/0.75. **Unresolved:** the `simplification` node ×2, the rearranged/derived 2nd equation, the condition, the definition.
- **106 weak:** 2 nodes, USES:1. **1/2 resolved** (`eq.bernoulli`→exact); the procedure unresolved. **No node resolved to a `misc.*` key.**

**Key fluids findings:**
- **Abstention generalizes** — both attempts abstain, same mechanism (105 pinned at `nc=0.75` by its lone `llm` proc node). Confirms the 100 % abstention is a structural property of resolver + floor, not an econ artifact.
- **Vacuous 1.0 generalizes** — `scoping=1.0` (the Bernoulli reference has **zero SCOPES edges**; the `P1==P2` condition is encoded as a `substitution` map, not a SCOPES edge) and `procedure_order=1.0` (≤1 proc on the winning path → 0 comparable ordered pairs).
- **`derived`/`alias`/`symbolic` did NOT fire on fluids.** The Bernoulli simplification and rearranged equation stayed unresolved → with endpoints unresolved, every reference edge fails the `(edge_type, from_key, to_key)` canonical-key match → `edge_coverage`/`usage`/`dependency` all collapse to 0.0 *even though the student emitted those edges*. Resolver recall is worse here than on econ.
- **Misconception bank empty for the fluids concept** — `apollo_misconceptions` table is unseeded for concept 3 (`soundness_applicable=false`, `contradiction=null`); soundness/bisimilarity are coverage-only fallbacks. (Concept 3 *does* have 2 misconception `:Canon` nodes — it's the pgvector bank table that's unseeded.)

---

## 5. The fluids routing bug (separate, upstream of grading)

The first fluids attempt failed with **HTTP 409 `pool_exhausted (cluster '4')`** on every variation — no grade. Root cause (confirmed in code + DB): `init_session_from_hoot` → `list_course_concepts(ssid=1)` (`apollo/subjects/curriculum_db.py:59`) returns all concepts whose subject is in search-space 1, with **no quarantine/tier filter at the concept level**. That set includes an **autoprovisioned decoy `bernoulli-equation` (concept id 4, subject `provisional-1`, 0 problems, created 2026-06-23)**. `infer_concept_id` (gpt-4o, temp 0) lexically prefers it over the real `bernoulli_principle` (id 3) because the legacy transcript repeatedly says "Bernoulli's **equation**." Id 4 has 0 problems → empty tier-2 pool → `PoolExhaustedError`. **Deterministic** (temp-0 + fixed transcript).

Unblock used for this test (reversible, local DB only): `UPDATE apollo_concepts SET subject_id=2 WHERE id=4` (re-parent decoy to a disjoint unused course), run, then `UPDATE … SET subject_id=1` to restore. Final state verified byte-identical; econ data untouched.

This is **upstream of grading** but blocks the entire fluids graph-grading path on the current seed. It is also a real product concern: autoprovisioning emits empty "provisional" concepts that can hijack concept inference.

---

## 6. Grading gaps found (severity-ranked, evidence-backed)

### G1 — CRITICAL: the shadow grader abstains on 100 % of attempts, by construction
**Evidence:** 17/17 attempts abstained (15 econ + 2 fluids), incl. perfect-coverage econ 93 & 96. **Mechanism:** Phase 1c's `normalization_confidence` floor (**0.85**) > the `llm` (0.75) and `fuzzy` (0.80) tier caps. Conceptual nodes (procedure/condition/definition/simplification) have no symbolic form, so they can only resolve via `llm`/`fuzzy` (or not at all); `normalization_confidence = MIN` over scored nodes ⇒ any attempt with procedural content is pinned ≤ 0.80 ⇒ NC abstain. Thin attempts instead exceed `unresolved_rate > 0.35` ⇒ UR abstain. The two gates **tile the whole performance space**: high coverage → NC, low coverage → UR; nothing passes. Note this floor was *introduced by the work under test* (Phase 1c) — the brake's intent is sound, its **calibration** is not.
**Impact:** the graph-sim grade can never be certified/promoted; spec §10 promotion-calibration (shadow-vs-LLM agreement, zero upward bias) is **impossible** to capture until fixed.
**Fix directions:** (a) recalibrate the floor below the `llm` cap (e.g. ≤ 0.70) or make it tier-aware; and/or (b) give conceptual nodes a higher-confidence resolution path (`symbolic`/`alias`) so they can clear the floor legitimately; and/or (c) exclude `llm`-only conceptual nodes from the MIN, or use a percentile/weighted aggregate instead of a hard MIN.

### G2 — HIGH: resolver recall is the upstream bottleneck
**Evidence:** 38 % of econ student nodes unresolved; 5/7 unresolved on fluids-strong; `symbolic`/`alias` tiers never fired at all. **Drivers:** procedure steps with empty `label`/`symbolic` (parser emits no canonical text → only `llm` can match), wrong-form equations going unresolved (econ 98 `NNP-GNP`, 101 `×PI/100`), numeric-only instantiations (econ growth), and simplification nodes not resolving (fluids).
**Impact:** under-resolution is the common cause behind G1 (UR + the `llm`/`fuzzy` pin), the under-credited strong answers (G5/H5), the collapsed edge metrics (fluids `edge`/`usage`/`dep`=0.0), and the missed misconceptions (G4).

### G3 — HIGH: Phase 1a works but is moot and does not generalize
**Evidence:** `derived`/0.95 fires on econ 99/100 (correct) but the attempts abstain anyway (G1); the *same* derived path does **not** resolve the fluids Bernoulli simplification or rearranged equation. The `alias` (1b) channel never fired in 69 student nodes.
**Impact:** the headline node-recovery win currently changes **no live grade**. It needs (a) G1 fixed before it can matter, and (b) extension to the simplification/substitution form used by the fluids reference.

### G4 — HIGH: misconception detection is largely non-functional
**Evidence (econ):** 4/5 weak attempts get a **false `soundness=1.0`** — the misconception-bearing student node fails to resolve to the *existing* `misc.*` key (resolver recall, **not** an empty bank; `soundness_applicable=true` for all 5). Only econ 104 connected, via `fuzzy`/0.80. Wrong *equations* (98, 101) go unresolved rather than matching a `misc.*`, so math errors are invisible to soundness. **Evidence (fluids):** `apollo_misconceptions` table unseeded for concept 3 → `soundness_applicable=false`, soundness is coverage-only.
**Impact:** the soundness/contradiction axis cannot reliably catch wrong reasoning today. Needs (a) better misconception resolution recall, (b) the `apollo_misconceptions` table seeded for fluids concepts, (c) a path for wrong equations to map to misconceptions instead of silently going unresolved.

### G5 — MEDIUM: vacuous 1.0 sub-scores + diagnostic-only edge scores mislead readers
**Evidence:** `scoping=1.0` and `procedure_order=1.0` on all 17 attempts — vacuous (no SCOPES reference edges anywhere; monotonic scripts never produce order inversions). Per code, the edge sub-scores (`edge_coverage`/`usage`/`scoping`/`procedure_order`/`dependency`) are **diagnostic-only — they do not move `coverage`/`soundness`/`bisimilarity`**.
**Impact:** a 1.0 reads as "perfect" when it means "nothing to grade." Any promotion dashboard must annotate vacuous dimensions and not aggregate them into a headline.

### G6 — MEDIUM: concept-inference mis-route blocks fluids entirely (autoprovision pollution)
**Evidence:** §5 — a 0-problem provisional concept hijacks gpt-4o concept inference → 409 on every fluids attempt, deterministically.
**Fix:** filter the `list_course_concepts` / `infer_concept_id` candidate set to concepts with a non-empty teachable (tier-2) pool, so empty provisional concepts can't capture the route.

### G7 — LOW / by-design (document, don't necessarily fix)
- `mastery_events=0` everywhere — Layer-3 belief write is gated off (`APOLLO_GRAPH_SIM_LAYER3_ENABLED`); by design.
- Numeric-only equation forms stay unresolved — Phase 1a is symbolic-only by design.
- `dependency_score` structurally near-zero (≤0.4) — students never verbalize the dense `DEPENDS_ON` edges the reference expects; also depresses bisimilarity.
- The `transcript_audit` recall booster raises the LLM-rubric coverage but is invisible to `normalization_confidence` (its covered-node findings carry empty `student_node_ids`), so it cannot lift an attempt past the NC gate.

---

## 7. What works (validated positives)

- ✅ **Clean live per-metric matrix captured** for all 15 econ attempts — closes the handoff's open §5 item. Timeout fix (120→300 s) eliminated the crash.
- ✅ **Both infra fixes confirmed live** (0×401 auth, 0×schema/UndefinedColumn across 17 attempts).
- ✅ **Shadow grade computes end-to-end** on every attempt (`comparison_runs=1`), persisting to Postgres + Neo4j with `dropped=0 invalid=0`.
- ✅ **Phase 1a derived@0.95 resolution fires live** (econ 99/100) and is independently corroborated by the matched USES edge.
- ✅ **The live student-facing grade (LLM matcher) discriminates correctly** — fluids 80/0, econ strong>partial>weak coverage gradient — so there is no student-facing regression; the gaps are confined to the (gated) shadow path.
- ✅ **Grade-math purity held** — the 4 phases changed resolution recall / abstention, not the scoring formula (coverage formula intact: e.g. real_gdp_from_deflator coverage 0.667 matches the deterministic harness).

---

## 8. Recommendations / next steps (priority order)

1. **Recalibrate the Phase-1c abstention floor relative to the tier caps (unblocks everything).** Pick a floor below the `llm` cap (or make it tier/percentile-aware) and re-run this exact probe; expect non-abstaining strong attempts. This is the prerequisite for §10 promotion-calibration.
2. **Raise conceptual-node resolution recall** (procedure/condition/definition/simplification): parser should emit canonical text for procedure steps; add `symbolic`/`alias` coverage for simplifications/substitutions (extend Phase 1a to the substitution-map form the fluids Bernoulli reference uses). This simultaneously fixes G2/G3/G4/G5 inputs.
3. **Seed `apollo_misconceptions` for fluids concepts** and improve misconception resolution so weak attempts stop getting false `soundness=1.0`.
4. **Filter empty provisional concepts out of concept inference** (G6) so fluids grades without a manual DB tweak.
5. **Annotate vacuous dimensions** in any shadow-vs-LLM report; never aggregate diagnostic-only edge scores into a headline grade.
6. **Then** capture the §10 shadow-vs-LLM agreement numbers — only meaningful once (1) lands.
7. **Broaden the fluids harness** (optional): there is no fluids orchestrator and only `bernoulli_height_change_find_v2` is scenario-wired. Draft strong/partial/weak scenarios for `bernoulli_full_find_p2` + `continuity_area_change_find_v2` are prepared in this session's scratchpad (`_fluids_scenarios.py`, `fluids-run-plan.md`) for a future broader sweep.

---

## 9. Artifacts & reproduction

Saved under `ai-ta-backend/scripts/` (the `.nr4`/`.fluids2` snapshots are the authoritative post-fix captures; the bare `macro_probe_score_matrix.json` is now the clean nr4 run, no longer the stale nr2):

| Path | What |
|---|---|
| `scripts/macro_probe_score_matrix.nr4.json` | clean econ 15-attempt score matrix (§3) |
| `scripts/apollo_grade_probe_report.macro.nr4.json` | econ per-attempt report (rubric + graphsim evidence + Neo4j) |
| `scripts/_run_macro_nr4.log` | econ live-run log (15× `comparison_runs=1`, 0 errors) |
| `scripts/apollo_grade_probe_report.fluids2.json` | fluids report, attempts 105/106 (§4) |
| `scripts/_fluids_server2.log` | fluids uvicorn boot log |

**Reproduce econ:** ensure local stack up (`docker start hoot-neo4j-local`; Supabase :54322 at migration 031), then `./.venv/Scripts/python.exe scripts/run_macro_probe.py --skip-embed --skip-mining --tag .nrX`. **Reproduce fluids:** exclude the decoy concept 4 from ssid 1 (§5), boot `uvicorn server:app --port 8001`, then `./.venv/Scripts/python.exe scripts/apollo_grade_probe.py --concept-slug bernoulli_principle --variations strong,weak --tag .fluidsX`.

**Inspect a graded attempt in Neo4j** (parameterize `$aid`):
```cypher
MATCH (n:_KGNode {attempt_id:$aid})-[r:RESOLVES_TO]->(c:Canon)
RETURN n.node_id, c.canonical_key, r.method, r.confidence ORDER BY r.method;
MATCH (n:_KGNode {attempt_id:$aid}) WHERE coalesce(n.resolution,'unresolved')<>'resolved'
RETURN [l IN labels(n) WHERE l<>'_KGNode'][0] AS kind, n.node_id;   -- unresolved nodes
```
**Inspect the shadow grade in Postgres:**
```sql
SELECT attempt_id, coverage_score, soundness_score, soundness_applicable,
       normalization_confidence, abstained, abstention_reasons
FROM apollo_graph_comparison_runs WHERE attempt_id BETWEEN 90 AND 106 ORDER BY attempt_id;
```

## 10. Appendix — the abstention arithmetic

```
normalization_confidence = MIN( method_cap(node) for node in scored_backing_nodes )
abstain  if  normalization_confidence < 0.85   (Phase 1c)   ── "NC"
   or   if  unresolved_rate          > 0.35                 ── "UR"

method_cap:  exact 1.00 · symbolic 0.98 · derived 0.95 · alias 0.92 · fuzzy 0.80 · llm 0.75

Conceptual nodes (procedure/condition/definition/simplification) reach only {llm 0.75, fuzzy 0.80, unresolved}.
  ⇒ any attempt that SCORES a conceptual node ⇒ nc ≤ 0.80 < 0.85 ⇒ NC abstain   (high-coverage path)
  ⇒ any attempt that resolves ONLY equations  ⇒ the rest unresolved ⇒ UR abstain (low-coverage path)
  ⇒ 0.85 floor is unsatisfiable on this resolver ⇒ 100 % abstention.   (proven: 17/17)
```

**Attempt map.** econ: 90 gdp_identity/strong · 91 …/partial · 92 …/weak · 93 net_exports_sign/strong · 94/95 partial/weak · 96 nnp_chain/strong · 97/98 partial/weak · 99 real_gdp_from_deflator/strong · 100/101 partial/weak · 102 real_gdp_growth/strong · 103/104 partial/weak. fluids: 105 bernoulli_height_change/strong · 106 …/weak.
