# Handoff — Apollo Misconception Detector: the recall gap (why 7/16 still miss)

**Date:** 2026-07-08
**Branch:** `feat/apollo-judge-authoritative-gate` (off `feat/apollo-misconception-detector`, local-only — neither is on `origin`). HEAD `a982476`. **Not pushed. Flag `APOLLO_MISCONCEPTION_DETECTOR` default OFF (flag-OFF byte-identical).**
**Status:** Detector is **live-verified working** for the first time (0→9 detections, false-Strong 7→4, 0/4 control FP), but **recall is 9/16**. This handoff hands off the *recall gap*: theories for the misses + a plan to test them.
**Predecessor context:** [[apollo-misconception-detector]] memory; spec `docs/_archive/specs/2026-07-08-apollo-misconception-corroboration-redesign.md`; plan sibling in `docs/_archive/plans/`.

---

## 1. Where we are (the win)

The detector was a NO-OP three times. Root cause of the 3rd no-op (confirmed by instrumentation): **the LLM judge was structurally blind to the student** — `judge.py::_user_prompt` carried no student text and `detector.py::detect_misconceptions` never passed `student_utterances` to `_run_judge`. It emitted uniform `needs_clarification`@0.7 / empty code on *every* node, identically on misconception attempts and controls.

**Fix (commit `a982476`):** threaded `student_utterances` into `judge_concepts` → `_user_prompt` (attempt-level `student_explanation` field) and `_run_judge`, and rewrote `_SYSTEM_PROMPT` to ground every verdict in the student's actual words and name `misconception_code` decisively.

**Live re-validation (full gpt-4o judge, 20 recorded attempts, local Docker PG 57322 + Neo4j 57687):**
```
misconception-class attempts: 16; >=1 misconception detected: 9   (was 0)
false-Strong on misconception-class: baseline=7 -> after-penalty=4 (was 7->7)
CONTROL false positives: 0/4                                       (unchanged, good)
```
Suite 2613 pass / 14 skip (+5 tests, 0 regressions); patch cov 100% (51/51 lines) vs base branch.

## 2. The exact gap

| Bucket | attempt_ids | notes |
|---|---|---|
| **Docked (9)** | 75, 81, 105, 108, 109, 110, 111, 113, 116 | each penalty ~0.27–0.30, **correct** bank code, band drops |
| **Missed misconception-class (7)** | **88, 95, 100, 112** (nominal_for_real), **102** (density_ignored), **114, 115** | see below |
| **Controls (clean & correct)** | 77, 89, 97, 106 | penalty 0, no misconceptions |

Two sub-groups in the misses:
- **The pedagogically critical cluster: 88, 95, 100, 112** — all `nominal_for_real`, all still band **Strong** → these are the **4 residual false-Strong**. This is the real target.
- **102** (`density_ignored`, fluids) — bands Developing, gated=7 but no dock.
- **114, 115** — already band **Beginning** (baseline 0.49/0.42). Not false-Strong; low-priority (a missed dock here doesn't over-credit anyone). Few findings (gated 1–2).

**Key puzzle:** `nominal_for_real` **docks on 105 but misses on 88/95/100/112** — same concept, same bank. Whatever differs between 105 and the other four is the crux.

## 3. How the dock decision works (the paths that matter for recall)

Gate (`gate.py`, spec §5 truth-table). For a judge finding `J` to produce a **dock**, one of:
- **Row 3 (co-key):** `J.bank_code` set (judge named a *valid* bank code) **AND** a `bank_pattern` finding shares that `bank_code` (floor-free — `bank_by_code` index, A13) **AND** `J` clears its **routed tau** (`TAU_FIRE=0.85` on the logprob path / `TAU_FIRE_VERBALIZED=0.90` on the verbalized path). → DOCK, ceiling-eligible.
- **Row 5 (lone solo):** `J.bank_code` set **AND** `J` clears **both** routed-tau **and** `TAU_SOLO_JUDGE=0.90`. → DOCK, penalty-only (never ceilings).
- Everything else (unkeyed, or sub-tau) → `needs_clarification` or drop. **No dock.**

`bank_pattern` **does** emit the correct code below-floor for these concepts (measured: `nominal_for_real`@0.582 on 88, `density_ignored`@0.465 on 102), and co-key is floor-free. So for the co-key path, **the only two things standing between a miss and a dock are: (a) does the judge NAME the valid code, and (b) does it CLEAR its routed tau.** That narrows the theories sharply.

## 4. Evidence gathered

- **Pre-fix probe** (blind judge, attempts 88/110/102 + controls 106/97): uniform `needs_clarification`@0.7, `code=''` everywhere; `bank_pattern` correct top-1 every time. (This is what proved the blind-judge root cause.)
- **Post-fix validation** (§1/§2 tables): 9 dock, 7 miss, controls clean.
- **NOT YET DONE (the #1 next action):** a *post-fix* probe of the **misses** (88/95/100/112/102) vs the **docked** `nominal_for_real` (105) to see what the judge NOW returns per node (verdict / `misconception_code` / confidence / logprob-vs-verbalized path). Until we have this, the theories below are hypotheses, not conclusions.

## 5. Theories for the misses (ranked; each with the test that confirms/kills it)

- **T1 — Judge names the code but confidence is SUB-TAU (most likely).** Post-fix the judge may correctly say `misconception` + `nominal_for_real` on 88/95/100/112 but at a confidence below its routed tau (0.85 logprob / **0.90 verbalized**) — so co-key/solo can't fire. Pre-fix these ran the *verbalized* path at 0.7. **105 may have docked because it hit the logprob path or expressed the misconception more blatantly (higher conf).** *Test:* post-fix probe — compare `confidence` + `verdict_token_prob_present` on the misses vs 105. If code is named but conf < tau → it's tau/confidence, not detection.
- **T2 — Judge still HEDGES to `needs_clarification` on subtler statements.** The `nominal_for_real` students who miss may phrase the confusion less overtly than 105, so gpt-4o won't commit to `misconception`. *Test:* same probe — is the verdict `misconception`+code or still `needs_clarification`/`clear`?
- **T3 — Verbalized-tau (0.90) too strict / logprobs unavailable.** If the judge is forced onto the verbalized path (`verdict_token_prob=None`) and returns ~0.7–0.89, the 0.90 verbalized floor blocks it even with a named code. *Test:* check `verdict_token_prob_present` on the misses; count how many would dock at a lower verbalized floor **without** flipping any control.
- **T4 — Per-node questioning dilutes confidence for a concept-level misconception (structural).** The judge is asked per reference-graph *node* (`growth_rate`, `real_basis`, …); a cross-cutting misconception like `nominal_for_real` doesn't attach cleanly to one node, so confidence spreads/lowers. *Test:* if T1–T3 don't fully explain it, try a **misconception-centric** ask (bank proposes candidate codes → judge confirms a targeted yes/no "is the student making error X?" — LLMs commit far more readily to a binary than to open-ended naming). This is the bigger change.
- **T5 (analysis, already supported):** co-key isn't the blocker — `bank_pattern` already emits the correct below-floor code, and co-key is floor-free, so the gate reduces to "judge names code + clears routed-tau." This *confirms* the fight is entirely in the judge's verdict/confidence (T1–T4), not in bank/gate wiring.

## 6. Proposed plan for next session

1. **Localize (cheap, do first):** post-fix probe of `88, 95, 100, 112, 102, 105` — capture per-node `verdict`, `misconception_code`, `confidence`, `verdict_token_prob_present`. Diagnose which of T1/T2/T3 dominates. Compare 105 (docked) directly against 88/95/100/112 (missed).
2. **Fix by branch:**
   - *If named-but-sub-tau (T1/T3):* calibrate `TAU_FIRE_VERBALIZED` / `TAU_SOLO_JUDGE` on the labeled 20-set (ROC/PR, **not** hand-picked), OR relax the *co-key* path to not require routed-tau when two tiers already agree on the same code (justifiable — corroboration is itself evidence). **HARD CONSTRAINT: control FP must stay 0/4** — re-check controls after any tau change (controls currently return `clear`/low-conf, so a modest loosening looks safe, but verify).
   - *If still hedging (T2/T4):* harden the judge prompt for decisiveness, OR switch to **misconception-centric confirmation** (bank proposes code → judge confirms yes/no). Keep keying/validation intact.
3. **Re-validate live** (same harness). Success = false-Strong drops below 4 **AND** control FP stays 0.
4. **Then:** close the `sympy_veto` bank_code nit (§9), open the PR, and treat final tau values as a pre-env-flip calibration pass.

Don't over-fit to n=20. Any tau loosening that helps recall must be checked against all 4 controls (and ideally the broader campaign later — controls are thin).

## 7. Reproduction

**Stack:** Docker Postgres `127.0.0.1:57322` + Neo4j `bolt://127.0.0.1:57687` (were up ~35h; **may not persist** — re-seed the local stack if down). OpenAI key + DSNs live in repo-root `ai-ta-backend/.env.campaign` (auto-loaded by the harness).

**Live validation (NLI OFF avoids the DeBERTa/pagefile crash; the detector doesn't need NLI):**
```bash
REPO=/c/Users/ultra/OneDrive/TA-test/ai-ta-backend
PYTHONPATH="$REPO" APOLLO_MISCONCEPTION_DETECTOR=1 APOLLO_NLI_PREWARM=0 APOLLO_NLI_ENABLED=0 \
  python -m campaign.validate_misconception_detector > out.json 2> out.log
# out.json = JSON then a "\n=== SUMMARY ===" block; split on that marker to json.loads the first part.
```
Attempt ids: `75,77,81,88,89,95,97,100,102,105,106,108,109,110,111,112,113,114,115,116`.

**Diagnostic probe** (this session's copy was in an ephemeral scratchpad — recreate ~40 lines): mirror the harness's per-attempt input reconstruction (`db.get(ProblemAttempt/ApolloSession)`, `KGStore.read_graph`, `list_problems_for_concept` → `problem.to_kg_graph`, `Message.content` utterances, `load_for_concept` for the bank), then **wrap** `make_openai_judge()` to capture `JudgeRaw.content`, run `detect_misconceptions(...)`, and print per finding: `verdict, confidence, signature, bank_code, verdict_token_prob_present` plus the raw judge rows (`verdict`, `misconception_code`, `confidence`) and `gate_findings(...)` output. Set `TARGETS=(88,95,100,112,102,105)`.

## 8. Key files

- `apollo/overseer/misconception_detector/judge.py` — `_user_prompt` (now includes `student_explanation`), `_SYSTEM_PROMPT`, `judge_concepts` (has `student_utterances`), `_finding_from_row` (validates code → keys `bank_code`/`misc.<code>`), `_JSON_SCHEMA` (has `misconception_code`), dual-tau via `verdict_token_prob`.
- `apollo/overseer/misconception_detector/detector.py` — `detect_misconceptions` (passes `student_utterances` to `_run_judge`), `_judge_concept_inputs` (keys by `node_id`, gives every node the FULL bank).
- `apollo/overseer/misconception_detector/gate.py` — the §5 truth-table, `bank_by_code` co-key (A13), `_docked`/`_needs_clarification` (`dataclasses.replace`).
- `apollo/overseer/misconception_detector/config.py` — `TAU_FIRE=0.85`, `TAU_FIRE_VERBALIZED=0.90`, `TAU_SOLO_JUDGE=0.90`, `BANK_SIM_FLOOR=0.80` (all `APOLLO_MISC_*` env-overridable).
- `apollo/overseer/misconception_detector/bank_pattern.py` — emits best match even below floor, tagged `bank_match_above_floor`.
- `apollo/overseer/misconception_detector/{types.py,merge.py}` — `ConceptFinding` fields (`bank_code`, `bank_match_above_floor`, `ceiling_eligible`); merge ceiling reads `ceiling_eligible`.
- Harness `campaign/validate_misconception_detector.py`; owner doc `docs/architecture/apollo.md`.

## 9. Open nits / caveats

- **`sympy_veto` invariant nit (latent, from the opus review):** `sympy_veto.py` builds `signature=misc.<code>` but doesn't set `bank_code` → violates the `bank_code ⟺ misc.<code>` invariant. No live impact (all consumers key off `signature`), but close it before the env flip. Also add a one-line comment in `gate.py` that truth-table row 4 is unreachable-by-construction (folds into the lone-judge path).
- **Evidence is thin:** n=20, single run, temp=0, controls n=4. Treat tau changes as needing broader validation before a prod env flip.
- **The gate/keying/corroboration design is correct** — it was validated by this run. The remaining work is entirely in the judge's verdict/confidence behavior (T1–T4), not the gate.
