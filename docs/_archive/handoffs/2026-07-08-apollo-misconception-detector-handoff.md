# Handoff — Apollo Misconception Detector (build + validation)

**Date:** 2026-07-08
**Branch:** `feat/apollo-misconception-detector` (cut off `origin/staging` @ `c5b5586`)
**Status:** Built, reviewed, unit-verified, committed, **flag-OFF**. Empirically a **no-op on real
data** — the judge defect is fixed but the detector still docks nothing because of the
corroboration gate + a broken `bank_pattern` tier. Next step decided (see §7): **fix
`bank_pattern` to corroborate the judge.**
**Nothing ships:** `APOLLO_MISCONCEPTION_DETECTOR` defaults OFF; the delivered grade is unchanged.

---

## 1. Goal

Fix the dead misconception signal in Apollo grading. On the `v2-qa-2026-07-08` campaign,
misconception detection fired **0/40** and ~45% of misconception-class attempts banded Strong
despite a taught error. Root cause: detection was welded to resolution (only a resolved `misc.*`
node counted). The delivered grade is the LLM rubric (`composite == overall`), and its
`misconception_penalty` socket was hardcoded `0.0`.

Design goal: a **separate, grader-agnostic misconception-detection stage** that runs parallel to
the coverage matcher and feeds the **live** penalty + the emergent promotion ledger.

## 2. Design decisions (from brainstorming — see spec)

- **Hybrid, judge-led detector** (not bank-only, not deterministic-only).
- **Severity-weighted subtract** merge with **graph-derived centrality** (no hand-authored
  weights) + an **anti-dilution ceiling** (a central misconception caps the band).
- **Scope:** live penalty (D1) + emergent-ledger feed (D2/D3). **Deferred:** D5
  (clarification-refuted → misconception). **Non-issue:** D6 — the resolver is already
  deterministic; the s4 variance is upstream `temperature=0.7` conversation generation, not a
  pairing bug (corrected in the defect ledger, no code).
- **Feed the LIVE grade**, not just the shadow graph lane (which abstains 100% and ships nothing).

Spec: `docs/_archive/specs/2026-07-08-apollo-misconception-detector-design.md`
Plan: `docs/_archive/plans/2026-07-08-apollo-misconception-detector-plan.md`

## 3. What was built

New package `apollo/overseer/misconception_detector/` (all immutable value objects, DI seams):

| Module | Purpose |
|---|---|
| `types.py` | Frozen VOs: `ConceptFinding`, `DetectionResult`, `MergeOutcome`, `JudgeRaw`, `JudgeFn`/`EmbedFn` Protocols |
| `config.py` | `detector_enabled()` (default OFF) + knobs: `TAU_FIRE=0.85`, `TAU_FIRE_VERBALIZED=0.90`, `SEVERITY_CLAMP=0.30`, `CENTRALITY_W_MIN=0.30`, `CEILING_COMPOSITE=0.84`, `BANK_SIM_FLOOR=0.80` (all `APOLLO_MISC_*` env-overridable) |
| `centrality.py` | `compute_centrality(reference_graph)` — cycle-safe (try/except on `topological_order`) |
| `sympy_veto.py` | Tier-1 deterministic equation sign-veto (reuses `apollo.resolution.tiers._symbolic_equiv`) against `eq:`-prefixed bank mutants |
| `bank_pattern.py` | Tier-1 CBM bank match vs raw student utterances (pgvector `match_by_embedding`, SQLite in-memory fallback) |
| `judge.py` | Tier-2 comparative LLM judge (`make_openai_judge` = new direct `client.chat.completions.create` with **structured outputs + logprobs**; `judge_concepts` maps to findings) |
| `gate.py` | Corroboration + dual-tau gate: dock vs `needs_clarification` route |
| `merge.py` | Severity-weighted subtract + ceiling → `MergeOutcome`; emits `canonical_key = misc.<code>` |
| `apply.py` | `apply_penalty` (composite, named-band ceiling) + `rubric_overall_after_penalty` (letter bands) |
| `detector.py` | `detect_misconceptions(...)` orchestrator (DI judge+embed) |

**D4 live fix** (`apollo/overseer/coverage.py`): dropped the `_BATCH_BINARY_PROMPT` clause "Sign
flips and algebraic rearrangements are equivalent" and pre-gate equation coverage through a
**zero-form sign check** (`_sign_reversed_zero_form` compares the student zero-form against the
reference's sign-reversed zero-form `L+R`, not the buggy `-1*(...)` string). Flag-gated.

**Wiring** (all guarded so flag-OFF is byte-identical):
- `apollo/handlers/done.py::handle_done` — new parallel `detect → gate → merge` stage after the
  rubric, `rubric = rubric_overall_after_penalty(...)`, threads `detection_outcome` into
  `write_artifacts`; whole block try/except-swallowed (soft-fail → HTTP 200).
- `apollo/grading/artifact_build.py::build_llm_artifact(detection_outcome=None)` — populates the
  `misconception_penalty` socket (was `0.0`) + `misconceptions[]` (was `[]`) + `apply_penalty`.
- `apollo/handlers/artifact_writer.py::write_artifacts(detection_outcome=None)` — threads it; the
  existing emergent-ledger writer picks up the now-populated `misconceptions[]`.
- `docs/architecture/apollo.md` — drift reconciled (owner doc).

## 4. What was tested

- **Unit/wiring/regression:** 10 detector test files + wiring + flag-OFF golden + ledger-feed.
- **Cold-eyes review (Opus):** found and we fixed —
  - **HIGH:** the D4 sign-gate was a NO-OP on real `=`-bearing equations (wrapped ref in
    `-1*(...)`, which `parse_zero_form` split on `=` → garbage; the test only used `=`-free
    fixtures). Fixed to zero-form comparison + a real `NX = X-M` / `NX = M-X` regression test.
  - **HIGH:** dual-tau never enforced — `ConceptFinding` dropped the token-prob-vs-verbalized
    origin bit, so `TAU_FIRE_VERBALIZED` was dead code. Fixed via a `verdict_token_prob_present`
    bit + routing test.
  - **LOW:** `has_detection` guard ignored `ceiling_applied` (unreachable today). Fixed + test.
- **Coverage:** **99% patch coverage** vs `origin/staging`, every changed file ≥95%
  (`coverage.py`/`sympy_veto.py` brought to 100% via targeted defensive-branch tests).
- **Full suite:** `pytest apollo/` → **2562 passed, 14 pre-existing skips**, no regressions.

## 5. Validation (real data, 2 cycles) — the important part

Harness: `campaign/validate_misconception_detector.py` — full-judge A/B over the **20 recorded
attempts** (ids 75,77,81,88,89,95,97,100,102,105,106,108,109,110,111,112,113,114,115,116) on the
**live local Docker stack** (Postgres `127.0.0.1:57322`, Neo4j `bolt://127.0.0.1:57687`), real
`make_openai_judge` (OpenAI key from `campaign/out/v2-qa-2026-07-08/campaign.env`). A/B = same
inputs, penalty applied (flag-ON) vs not (flag-OFF). The detector does **not** need the heavy
resolver NLI/DeBERTa (that's the graph-sim shadow), so no pagefile-crash risk.
Report: `docs/_archive/experiments/2026-07-08-misconception-detector-validation.md`

**Cycle 1 → 0/20 docks.** Root cause (reproduced in isolation, deterministic at temp=0):
`make_openai_judge` asked gpt-4o for `{"concepts":[...]}` but the model **collapsed to a single
flat object**; `judge_concepts` required a list → **soft-failed to all-clear on every attempt**.
The DI stub in the unit tests returned the ideal array shape, hiding this ("the mock lied").

**Judge fix (committed `d411dfb`):** OpenAI **structured outputs** (strict `json_schema` forces
the array) + a tolerant `_normalize_rows` parser (wraps a flat object into a 1-element list) + a
regression test feeding the **real collapsed shape**. Verified live: the judge now returns real
varied verdicts (0.70 / 0.95 / 0.97 / 0.64 / 0.996), not the all-clear collapse.

**Cycle 2 (after judge fix) → STILL 0/20 docks.** Reason moved from a *bug* to a **design
interaction**:
- `gate.py` (by the safety design) **never docks a lone judge finding** — it needs a
  deterministic `sympy_veto` hit OR ≥2 tiers agreeing.
- On this data **only the judge fires**: `bank_pattern` = 0, `sympy_veto` = 0. So nothing
  corroborates the judge → nothing docks. All judge signatures were `unkeyed:<concept_id>`.

**`bank_pattern` root-caused (live measurement):** it cosine-matches the utterance against the
misconception **`description_embedding` only** (never `trigger_phrases`). Measured similarities
for the correct codes: attempt 88 = **0.582**, 95 = **0.614**, 110 = **0.675** — all correctly
pick the right code as top match but fall **0.13–0.22 below the 0.80 floor**. Utterance-vs-
trigger-phrase scores ~0.06–0.13 higher (0.645–0.707) but is never embedded; sibling codes on the
same concept sit at ~0.74 cross-similarity, so **0.80 is uncalibrated for this domain**.

**`sympy_veto`:** inert — no `eq:`-prefixed sign mutants are seeded in the bank (confirmed:
`grep "eq:" apollo/subjects/.../misconceptions.json` empty). Note the D4 coverage fix denies
credit to reversed equations *without* mutants; mutants are only needed for the *named* sign-
misconception penalty.

## 6. Results summary

| Metric | Baseline (flag-OFF) | Detector-ON (after judge fix) |
|---|---|---|
| Attempts docked (any penalty) | — | **0/20** |
| Misconception-class detected | 0/16 | **0/16** (no change) |
| False-Strong on misconception attempts | 7 | **7** (no change) |
| Strong-control false positives | — | **0/4** (non-vacuous: attempt 77 judge = clear @0.996) |

**Net:** the judge defect is closed and verified; the detector introduces **no regression and no
control false-positives**, but its core value is **undelivered** on this dataset. The improvement
hypothesis is **unproven, not refuted** — it never got to dock. Bottleneck precisely localized to
**corroboration gate + `bank_pattern` threshold/representation + unseeded `sympy_veto`**.

## 7. Next steps (decision made: "Fix `bank_pattern` to corroborate")

The personas assert **known, banked** codes (`includes_transfers`, `nominal_for_real`,
`gross_for_net`, `density_ignored`), so the intended corroborator is `bank_pattern`. Plan:

1. **Fix `bank_pattern` (primary):**
   - Match the utterance embedding against **`trigger_phrases`** (short, high-signal), not just
     `description_embedding`. Requires indexing trigger-phrase embeddings (schema/seed touch) or
     embedding them at query time.
   - **Calibrate `BANK_SIM_FLOOR`** off a labeled ROC/PR pass over the 20-set — do NOT hand-pick.
     Caution: correct-code utterance sims (~0.65–0.71 vs trigger phrases) sit *near* sibling
     cross-sims (~0.74) — the floor + representation must separate signal from sibling noise.
   - Result: judge + bank corroborate on banked misconceptions → **keyed docks → bands move**;
     novel/unbanked misconceptions stay lone-judge → clarification route (safety gate preserved).
2. **Seed `sympy_veto` mutants (parallel):** author `eq:`-prefixed sign-reversal mutants for the
   equation misconceptions (net_exports, deflate_wrong_direction) so the deterministic sign tier
   can dock the sign cases on its own.
3. **Re-validate** with `campaign/validate_misconception_detector.py` — measure false-Strong drop
   AND **strong-control specificity at scale** (only 4 controls today — thin).
4. **PR to `staging`** (flag-OFF) once it demonstrably improves grading with zero control FP.

The corroboration design means **only banked misconceptions dock** (judge+bank); novel ones route
to clarification by intent. If that recall is too low, the alternative fork (rejected for now) was
to **let a high-confidence bank-keyed lone judge dock** — faster recall, but reintroduces the
lone-LLM false-alarm risk the gate was built to prevent.

## 8. Git state

- `d411dfb` fix: judge structured-outputs + tolerant parse + validation harness/report
- `479a104` feat: the detector (11 modules + wiring + D4 + drift), 99% patch cov, flag-OFF
- `7583105` docs: design spec
- base `c5b5586` = `origin/staging`
- **Unrelated:** `06adca6` on `feat/apollo-clarification-v2-ranker` = a preserved WIP snapshot of
  in-flight problem-data (not part of this work; committed so it wasn't lost when branching).
- Untracked and intentionally uncommitted: `campaign/out/v2-qa-2026-07-08/` (session/replay
  output, incl. a live OpenAI key in `campaign.env` — do not commit).

## 9. Caveats / honesty notes

- Validation is **n=20, single run, temp=0.0, one dataset** — control specificity thinly evidenced.
- The structured-outputs + `logprobs=True` "compose" claim rests on OpenAI's documented contract
  (not re-verified with a fresh live call in-sandbox); the verbalized-confidence fallback
  (`verdict_token_prob=None` → `TAU_FIRE_VERBALIZED`) covers the no-logprob case either way.
- The live Docker stack was up ~32h during validation — it **may not persist**; re-validation may
  need the stack re-seeded.
- Harmless NumPy/pandas ABI warning from the neo4j driver's optional pandas import (env NumPy 2.x
  vs pandas built on 1.x) — non-fatal, unrelated.

## 10. Key entry points for whoever resumes

- Detector: `apollo/overseer/misconception_detector/` (start at `detector.py`, then `gate.py`,
  `merge.py`).
- The blocker: `bank_pattern.py::detect_bank_pattern` + `apollo/overseer/misconception_bank.py::match_by_embedding` (description-only match) + `config.py::BANK_SIM_FLOOR`.
- Gate contract: `gate.py` (lone judge → `needs_clarification`, never docks).
- Validation: `campaign/validate_misconception_detector.py`; report in
  `docs/_archive/experiments/2026-07-08-misconception-detector-validation.md`.
- Live grade path: `done.py::handle_done` → `artifact_writer.py::write_artifacts` →
  `artifact_build.py::build_llm_artifact` (the `misconception_penalty` socket).
