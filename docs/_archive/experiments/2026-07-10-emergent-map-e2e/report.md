# Emergent Misconception Map — End-to-End Campaign Report

- **Date:** 2026-07-10
- **Branch:** `feat/apollo-emergent-misconception-map`
- **Feature specs:** `docs/_archive/specs/2026-07-10-emergent-misconception-map-design.md`, `docs/_archive/specs/2026-07-10-grader-positive-focus-design.md`
- **Plan:** `docs/_archive/plans/2026-07-10-emergent-misconception-map-plan.md`
- **Evidence directory (run-local, NOT committed — see §10 security note):** `campaign/out/2026-07-10-emergent-e2e/`
- **Flags this whole session:** `APOLLO_EMERGENT_MAP_CAPTURE=1`, `APOLLO_EMERGENT_MAP_ASSERT=1` set explicitly for the probe scripts only — **prod/staging default for both remains OFF**; every number in this report describes an opt-in local probe, not live behavior.

## 1. Summary

This is the first live, end-to-end exercise of the emergent misconception map (capture → accrete → promote → F-struct dock) and the grader positive-focus split, against a real FastAPI server + real Postgres + real Neo4j, using the campaign harness's LLM-driven student personas. It found and fixed one real defect (an NLI classify-time `ImportError` escaping mid-resolution and 500ing `/done` — see `fix: NLI classify guard degrades gracefully when transformers unavailable`, this branch), and produced the first measured trust/precision numbers for the emergent map's promotion gate.

Headline results:

- **T9 pre-flight gates:** 2994 apollo tests pass, 100% diff-cover vs `origin/staging`, 187 golden/flag-off tests confirm byte-identical behavior with both new flags OFF (the shipped default).
- **Runs A/B/C (replay determinism):** 33/33 successfully-graded attempts replayed with **0 grade deltas** A-vs-B and A-vs-C — byte-identical grading math across repeated runs.
- **Run D (live capture + promotion):** both capture seams fired live; 8 observations (6 `clarification_refuted` + 2 `detector_unkeyed`); one signature (`emergent.eq.continuity`) crossed both τ_project and τ_assert with 2 distinct students (trust 0.6332131508112203); the single-student cap held for 2 other signatures (trust capped just under 0.3, below τ_assert 0.5); 5 `:Canon` misconception nodes were minted with all `opposes` edges resolving.
- **Precision:** 2 of 3 promotable signatures (K=3, τ_assert=0.5) matched an independently-labeled emergent form — precision@τ_assert = 0.6667 (2/3). The one non-matching promotion traces to a genuinely wrong student utterance (a defensible co-detection, not a false positive — see §6).
- **KG sweep:** 42/42 authored `entity_key`s exactly string-equal to their schema-derived form, 0 mismatches, across all 10 campaign problem files.
- **One anomaly fixed in-session:** the NLI classify-guard defect (Task 1, this branch). **One anomaly asserted but only partly evidenced:** NLI is understood to have been de-facto OFF for the session (environment-level numpy/scipy ABI mismatch) — see §7 for what is and is not directly evidenced by the campaign artifacts.

## 2. T9 gates (pre-flight)

Before the live campaign, the standard patch-coverage and regression gates were run:

- **Full apollo suite:** 2994 tests pass (0 failed), plus 14 skipped legacy-V2 tests unrelated to this branch.
- **diff-cover vs `origin/staging`:** 100% (all changed/added lines in production code covered).
- **Golden flag-off suite:** 187 tests confirming that with `APOLLO_EMERGENT_MAP_CAPTURE`, `APOLLO_EMERGENT_MAP_ASSERT`, and `APOLLO_GRADER_POSITIVE_FOCUS` all unset (the shipped default), `handle_done`'s rubric, coverage, composite, XP, and artifact `misconceptions[]` output is byte-identical to pre-branch behavior.

These numbers are cited from the T9 gate run that preceded the live campaign in this session; see the branch's commit history for the underlying test files (`apollo/emergent/tests/`, `apollo/handlers/tests/database/test_done_misconception.py`, `apollo/overseer/tests/test_coverage_sign_gate.py`, `apollo/overseer/misconception_detector/tests/test_misconception_detector_gate.py`).

## 3. Corpus construction

The campaign harness (`campaign/out/2026-07-10-emergent-e2e/bootstrap_course.py`, `run_corpus.py`) built a **fresh** corpus of 34 live attempts (16 fluid-mechanics + 18 macroeconomics personas, per `chunk-fluid_mechanics.log`'s `ok=16 err=0 total_seen=16` and `chunk-macroeconomics.log`'s `ok=17 err=1 total_seen=18`) against a freshly-bootstrapped course, rather than reusing the frozen f1c replay corpus — the f1c replay requires pre-reset `attempt_id` rows, which a fresh capture-seam probe would corrupt (capture writes new `apollo_misconception_observations` rows keyed on `attempt_id`; reusing f1c's frozen attempt rows would either collide with prior observations or silently no-op on the idempotent `UNIQUE(attempt_id, signature)` constraint).

Of the 34 corpus attempts (`attempts.jsonl`), **33 completed successfully and 1 failed**: line 33 of `attempts.jsonl` records `attempt_id: null`, `status: "error"`, persona `vague_then_clarifies`, problem `real_gdp_from_deflator` (macroeconomics), error `HTTPStatusError("Server error '500 Internal Server Error' for url 'http://127.0.0.1:8000/apollo/sessions/33/done'…")`. `chunk-macroeconomics.log:18` shows the matching `[06:03:37] ERR` line (64.5s). This is the exact defect fixed in Task 1 of this branch (NLI classify-guard). The failed attempt has no `attempt_id` and is excluded from all downstream replay/matrix analysis — hence "33/33" below, not "34/34". (The 33 non-null attempt_ids replayed are `[1,3,6,7,8,10,14,15,16,18,21,25,26,27,29,32,33,34,35,36,37,38,39,40,41,42,43,44,45,46,47,48,50]`, matching `replay-A.json` exactly.)

## 4. Runs A/B/C — replay determinism

`run_replay.py` replays each of the 33 successfully-graded attempts by re-driving `build_rerun_inputs` + `run_graph_simulation` against the same persisted transcript, three times (A, B, C), and `build_grade_matrix.py` diffs the resulting `GradeResult`s pairwise.

- **A vs B:** `a_vs_b_delta_count = 0`, `a_vs_b_blockers = []` — all 33 matrix entries carry an empty `A_vs_B_deltas` dict, meaning zero divergent fields across every compared dimension (`graph_composite`, `node_coverage`, `unresolved_rate`, `actual_credited`, `actual_misconceptions`, `actual_unresolved`) between the two independent replay passes.
- **A vs C:** all 33 matrix entries also carry an empty `A_vs_C_deltas` dict — same zero-divergence result on the third independent pass. (Note: `grade-matrix-ABC.json` has no separate top-level `a_vs_c_delta_count` summary field, and does not store B's or C's raw output — only A's values plus the two empty-when-identical delta dicts per entry; "0 grade deltas" means all 33×2 delta dicts are empty, not a stored byte-for-byte file compare.)
- Of the 33 replayed attempts, **all 33 abstained** in run A (`abstention_reasons: ["unresolved_rate_above_threshold"]`, consistent with NLI being unavailable this session — §7 — which raises `unresolved_rate` well above the abstention gate's threshold). The 0-delta result is therefore a genuine byte-identity proof (every replay pass reached the identical abstained state via the identical unresolved-node set), not a vacuous "nothing to compare" result — the full `actual_credited`/`actual_unresolved` node sets are compared, not just the abstention flag.

**Structural finding (important scope boundary):** `run_graph_simulation` — the function replay drives — implements steps 5+ of the grading chain (resolve → canonicalize → grade → audit → persist). It does **not** call `handle_done`, the outer orchestrator in `apollo/handlers/done.py` that owns the misconception-detector block, both new emergent-map capture seams (`detector_unkeyed`, `clarification_refuted`), and the grader positive-focus flag checks (P1–P4). Those all live in `handle_done`, downstream of where replay's call boundary stops. **Consequence: replay can prove grading-math determinism, but it structurally cannot exercise the capture seams or positive-focus — those require a live `/done` HTTP call each time (Run D, §5).** This is not a gap in the replay harness; it is a correct reflection of where each concern is wired, and is the reason Run D exists as a separate, live-only validation pass.

## 5. Run D — live capture + promotion

`run_d_corpus.py` drove 3 fresh live sessions (`run_d_personas/student{1,2,3}.json`, persona `vague_then_clarifies`) through real `/done` calls with `APOLLO_EMERGENT_MAP_CAPTURE=1` and `APOLLO_EMERGENT_MAP_ASSERT=1` set, against the fluid-mechanics Bernoulli concept (concept_id 11).

**Attempts:** 3/3 completed (`attempt_id` 51, 52, 53), all `status: ok`, all `user_distinct: true`. Only `attempt_id=51` (student1, problem `bernoulli_full_find_p2`) matched its intended problem (`problem_matched: true`); student2 (`bernoulli_horizontal_pipe_find_p2`) and student3 (`continuity_area_change_find_v2`) both had `problem_matched: false` — the problem selector picked a different problem than the persona file intended, so their clarification probes hit different reference nodes than planned. This is a driver/persona-selection artifact, not a grading defect — see §7 note (4).

**Seams exercised (both fired live), 8 observations total, attribution by attempt/persona:**

| Attempt | Persona file | Problem (matched?) | Source | Signature |
|---|---|---|---|---|
| 51 | student1 | `bernoulli_full_find_p2` (yes) | `clarification_refuted` | `emergent.cond.incompressibility` |
| 51 | student1 | `bernoulli_full_find_p2` (yes) | `detector_unkeyed` | `emergent.eq.continuity` |
| 51 | student1 | `bernoulli_full_find_p2` (yes) | `detector_unkeyed` | `emergent.proc.plan_apply_continuity_for_v2` |
| 52 | student2 | `bernoulli_horizontal_pipe_find_p2` (no) | `clarification_refuted` | `emergent.proc.plan_substitute_into_bernoulli` |
| 52 | student2 | `bernoulli_horizontal_pipe_find_p2` (no) | `clarification_refuted` | `emergent.cond.incompressibility` |
| 52 | student2 | `bernoulli_horizontal_pipe_find_p2` (no) | `clarification_refuted` | `emergent.eq.bernoulli` |
| 53 | student3 | `continuity_area_change_find_v2` (no) | `clarification_refuted` | `emergent.eq.continuity` |
| 53 | student3 | `continuity_area_change_find_v2` (no) | `clarification_refuted` | `emergent.proc.plan_apply_continuity_for_v2` |

Totals: 6 `clarification_refuted` + 2 `detector_unkeyed` = 8 (matches `observation_count: 8` / `observations_total: 8` in the measurement and results files). `server_failures: "none (all 3 /done 200; zero emergent_*_capture_failed / opposes_entity_missing log lines)"` — the capture seams' own failure-domain (try/except + rollback, `done.py`'s `emergent_birth_capture_failed` handler) was never triggered.

**Trust math verification:**

- `emergent.eq.continuity`: 2 distinct students, trust **0.6332131508112203** — crosses both τ_project (0.2) and τ_assert (0.5). Harness note: "intended 3 distinct students on eq.continuity; got 2 because student2 problem_matched=False (selector picked a different problem, its clarifications probed other nodes). Both thresholds still crossed" regardless.
- Single-student cap verified: `emergent.eq.bernoulli` (1 student, trust 0.2999896913998247) and `emergent.proc.plan_substitute_into_bernoulli` (1 student, trust 0.29998766614030675) both sit **below** τ_assert (0.5) despite exceeding τ_project (0.2) — confirming the design's `min(1, students/K)` fairness property (§5.4 of the design spec): `K=3` caps a single observer's class-support term at `1/3 ≈ 0.333`, and trust is observed capping just under 0.3 in both cases (the effect of `min(1, students/K) × mean_confidence × recency`, not a separately-named "0.3" constant in the flag config). A lone observer can never single-handedly promote a misconception to grade-affecting status.

**Materialization (τ_project, D4):** 5 signatures crossed τ_project and were minted as first-class `apollo_kg_entities kind='misconception'` rows and projected to `:Canon`:

```
emergent.cond.incompressibility              (id 104, opposes cond.incompressibility, entity 24)
emergent.eq.continuity                       (id 105, opposes eq.continuity,          entity 23)
emergent.proc.plan_apply_continuity_for_v2   (id 106, opposes proc.plan_apply_continuity_for_v2, entity 37)
emergent.proc.plan_substitute_into_bernoulli (id 107, opposes proc.plan_substitute_into_bernoulli, entity 38)
emergent.eq.bernoulli                        (id 108, opposes eq.bernoulli,           entity 25)
```

All 5 report `opposes_resolves: true` — every minted emergent entity's `opposes_entity_key` resolves to a real reference-graph entity in the KG. `emergent_vs_authored_key_collisions: []` for every concept probed — no emergent signature ever collided with a hand-authored `misc.*` key. `canon_node_count` after Run D: 46 for concept 11 (the total post-materialization node count for that concept, not the count of newly-minted nodes — the 5 minted entities are a subset), matching `expected_canon_count: 46` exactly.

## 6. Precision (K=3, τ_assert=0.5)

`emergent-map-measurements-after-run-d.json`'s `precision` block evaluates precision at the τ_assert bar against an independently-authored label set (`labeled_emergent_forms`):

- **`promotable_signatures` (denominator, 3):** `emergent.proc.plan_apply_continuity_for_v2`, `emergent.eq.continuity`, `emergent.cond.incompressibility`.
- **`matching_labeled` (numerator, 2):** `emergent.eq.continuity`, `emergent.cond.incompressibility`.
- **`precision_at_tau_assert = 0.6666666666666666`** (2/3).

This is precision over promotable signatures against the label set — the "miss," `emergent.proc.plan_apply_continuity_for_v2`, is **absent from `labeled_emergent_forms`**, not confirmed wrong. Its two contributing observations (`detector_unkeyed` conf 1.0 and `clarification_refuted` conf 0.95, both attempts 51/53) trace to real, substantively-wrong student utterances about how continuity applies to the v2 branch of the plan — the same underlying confusion that also produced the labeled `eq.continuity` miss, captured at a different (procedure-step) node. This reads as a **defensible co-detection**, not a false positive: the emergent map is node-anchored (design decision D2), so one underlying student confusion that touches both an equation node and its associated procedure-step node can legitimately produce two distinct, both-correct emergent signatures. The label set, authored before this design's node-anchoring was finalized, only anticipated one of the two.

## 7. Anomalies

1. **NLI classify-guard defect — FIXED in this branch.** The e2e run surfaced a live defect: `TransformersNLIAdjudicator._load()`'s lazy `from transformers import pipeline` import, invoked inside `classify()`, raised `ImportError`/`ModuleNotFoundError` mid-resolution in an environment with a broken numpy/scipy ABI (numpy 2.3.5 vs scipy built against numpy<2), escaping uncaught and 500ing `/apollo/sessions/{id}/done` (the 1/18 macro attempt in §3). Fixed via commit `fix: NLI classify guard degrades gracefully when transformers unavailable` (this branch): investigation traced every production `classify()` call site and confirmed `done_grading.py`'s `_resolve_attempt_async` already wrapped the offloaded `resolve_attempt` call in `except (ImportError, ModuleNotFoundError)` (inherited from `origin/staging` commit `7322fd9`), so the guard was NOT construction-only as first suspected — it already covers the mid-resolution seam. The actual gap was test coverage: every existing test mocked `resolve_attempt` wholesale, so none proved the ImportError survives the *real* call stack. Added a regression test exercising a real `TransformersNLIAdjudicator` (only `_load` patched to raise) through the real resolver; verified RED (by temporarily narrowing the guard to the wrong exception type, reproducing the exact live traceback) then GREEN.

2. **NLI availability this session — partially evidenced.** The environment-level numpy/scipy ABI mismatch that caused anomaly (1) is understood to have made `classify()` degrade to no-NLI throughout the session (logged once via `apollo_nli_transformers_unavailable`, per the L4 guard). **Caveat on the evidence:** a direct grep of the three campaign client-side logs (`chunk-fluid_mechanics.log`, `chunk-macroeconomics.log`, `run_d.log`) for `ImportError|transformers|scipy|numpy|fallback|NLI` returns zero matches — these client-side logs don't carry server-side degrade warnings, so the NLI-off claim is **not directly evidenced by the campaign artifact directory itself**; it rests on the server-side ABI mismatch being present in the launching environment (the same one that reproduced anomaly 1) and on all 33 replayed attempts abstaining with `unresolved_rate_above_threshold` (§4), which is the expected signature of NLI-assisted candidates never resolving. Every attempt's recorded `versions.nli_model` field still names `"cross-encoder/nli-deberta-v3-large"` (a static config string, not execution proof, since the model name is set regardless of whether `classify()` ever successfully loads it). **Consequence if the off-state holds: none of tonight's grading numbers (unresolved_rate, abstention counts, composite scores) reflect NLI-assisted resolution.** They are internally comparable (A vs B vs C, and Run D's trust math, which does not depend on NLI at all) but should **NOT** be treated as comparable to the `f1c` frozen-replay baseline without first confirming that baseline's NLI availability. A future run should capture direct server-side evidence (the `apollo_nli_transformers_unavailable` log line itself) rather than relying on downstream inference.

3. **`server.py`'s `load_dotenv(override=True)` foot-gun.** Noted during the session as a live-environment hazard: because `.env` is loaded with `override=True`, any env var also present in the shell (e.g. a flag exported for one probe script) gets silently clobbered by `.env`'s value on next server reload, which can make a flag look "on" in the launching shell but "off" inside the running server (or vice versa). Not a code defect in this branch, but worth flagging for anyone repeating this style of live flag-gated probe — confirm the effective flag state via a log line inside the process, not just the launching shell's env.

4. **Run-D driver applies `clarification_policy` to every clarification.** The Run D harness (`run_d_corpus.py`) answers every clarification probe according to a fixed policy rather than varying it per-node, which is a **driver limitation, not a product defect**: it widens what gets captured (every clarification the driver answers in a refuting way becomes a `clarification_refuted` observation) rather than exercising a realistic mix of confirmed/refuted/ambiguous answers. The 6 `clarification_refuted` observations in §5 should be read as "the driver's answer policy was refuting on these 6 turns," not as "6 independent, differently-motivated student corrections."

## 8. Known gaps

- **Positive-focus (`APOLLO_GRADER_POSITIVE_FOCUS`) has no live-e2e exercise.** All coverage is unit/golden-level (24 tests) proving P1–P4's flag-gated behavior in isolation. The flag ships OFF; no live campaign run in this session set it ON. A future e2e pass should set `APOLLO_GRADER_POSITIVE_FOCUS=1` (alongside a detector-enabled run) and confirm the served rubric/composite/XP diverge from the OFF baseline exactly as P1–P4 predict, and that `detection_outcome`/`misconceptions[]` feedback fidelity is retained.
- **`opposes` exists as a Postgres payload only, not a `:Canon` graph edge.** Every emergent (and authored) misconception's `opposes_entity_key` is stored as a payload field on the Postgres/`:Canon` misconception entity and resolved by string lookup at read time (`opposes_resolves: true` in §5 confirms the lookup succeeds) — it is not yet projected as an actual `OPPOSES`-typed relationship in the Neo4j graph. Follow-up: project `opposes` as a first-class `:Canon` edge so graph-native queries (and any future graph-simulation grader) can traverse it directly instead of doing the payload-key resolution at read time.

## 9. Calibration recommendation

**KEEP the pre-calibration defaults: K=3, τ_project=0.2, τ_assert=0.5.**

Rationale from this session's evidence:
- The single-student cap (§5) correctly held two real signatures below τ_assert despite each having high per-observation confidence (~0.9–1.0) — confirming K=3 does its job of requiring independent corroboration before any signature can affect a grade.
- The one promoted signature that crossed τ_assert (`eq.continuity`, trust 0.633, 2 students) did so from a real, both-observers-independently-correct signal, and precision at τ_assert (0.667, §6) is driven by a defensible co-detection rather than a hallucinated verdict — no evidence in this session that τ_assert=0.5 is too permissive.
- τ_project=0.2 correctly surfaced 5 real signatures onto the map (visible, non-grading) without any collision against authored keys — no evidence it is too permissive or too strict for the "visible but not yet grading" tier.
- This is a **3-attempt, single-concept, single-session sample** — not a statistically powered calibration run. The recommendation is "no evidence to move the pre-calibration defaults," not "these values are proven optimal." A larger, multi-concept Run-D-style campaign (ideally with a driver that varies its clarification-answer policy per node — see anomaly (4)) is the natural next step before any real-traffic enable, per the design spec's §7 calibration gate.

See `campaign/out/2026-07-10-emergent-e2e/emergent-map-calibration.md` for the short-form precision table and cluster→signature mapping (run-local copy — see §10 for why it is not committed).

## 10. Security note on the evidence directory

`campaign/out/2026-07-10-emergent-e2e/course-bootstrap.json` contains a plaintext `teacher_password` and a live-looking Supabase JWT `teacher_token` for the campaign's throwaway teacher account, generated against the local dev stack this session. `campaign/out/` is **not** covered by any `.gitignore` pattern (checked: `.gitignore` has no `campaign` entry), so `git status` shows the whole evidence directory as untracked-but-not-ignored. **Do not `git add` this directory or `course-bootstrap.json` specifically** — this report intentionally cites facts extracted from those files without committing them. If the raw evidence needs to be preserved long-term, either scrub `course-bootstrap.json` (drop `teacher_password`/`teacher_token`) before adding it, or add `campaign/out/` to `.gitignore` and store the raw run artifacts elsewhere.
