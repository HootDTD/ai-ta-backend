# Grader Positive-Focus — Design Memo

- **Date:** 2026-07-10
- **Status:** Design (companion to the Emergent Misconception Map plan; tasks live in that plan's Wave 5)
- **Owner doc (drift target):** `docs/architecture/apollo.md`
- **Plan:** `docs/_archive/plans/2026-07-10-emergent-misconception-map-plan.md` (task T-W5a)
- **Scope guard:** minimal + additive; no grading-math refactor; **no** changes to composite/abstention gates (`apollo/grading/composite.py`, `apollo/grading/abstention.py` — PR #105 territory); byte-identical when the new flag is OFF.

---

## 1. Owner's decision

The coverage grader should **credit the GOOD** in a student's explanation; the misconception **detector + emergent map** should be the **sole authority on docking the BAD**. This memo enumerates every wrongness-penalty path in the *current live grader* (`compute_coverage → compute_rubric` on a Done click), then designs a flag-gated neutralization that routes all penalty authority through the detector/misconception-feedback channel.

## 2. Ground truth (verified against `origin/staging`, 2026-07-10)

**The coverage/rubric grader itself already docks nothing for wrongness.** This is the load-bearing finding — it makes the change small.

- `compute_coverage` (`apollo/overseer/coverage.py:463-575`) is pure credit-assignment: per-node covered/missing + partial procedure credit. It contains **no penalty/subtraction**. Its only downgrades are *credit-denials* (refusing credit it's unsure of), not penalties:
  - Binary-confidence floor `_BINARY_CONFIDENCE_FLOOR=0.5` (`coverage.py:67`, applied `coverage.py:534-544`): a low-confidence `covered=True` is demoted to `missing`. Credit-denial, not a subtraction.
  - `_sign_gate_equation_verdicts` (`coverage.py:258-328`, called `coverage.py:215-220`, **already gated by `detector_enabled()`**): a sign-reversed equation the LLM marked covered is forced to `covered=False`. Credit-denial tied to student wrongness — the one coverage-side path that is wrongness-*driven*.
- `compute_rubric` (`apollo/overseer/rubric.py:79-180`) is a pure weighted aggregation. It contains **zero** contradiction/penalty/dock/cap logic (grep-confirmed). The `misconception_corrected` axis (`rubric.py:137-147`, 5% weight `rubric.py:39-44`) is **credit-only** on its face: resolved=1.0, unresolved=0.5, never-detected=absent (`rubric.py:92-93`). But because an *unresolved* (0.5) contribution on the 5% axis scores below the attempt's other axes, its presence can pull `overall` slightly DOWN vs the axis being absent (absent → weight redistributes to 60/25/15). So it is a **mild wrongness-sensitive** path worth neutralizing for a true credit-only grade.

**All live wrongness penalties come from the misconception detector's `MergeOutcome`, applied at exactly two sites, both fed by the same number** (`detection_outcome` computed once at `done.py:565`):

| # | Penalty site | file:line | What it does | Gated by |
|---|---|---|---|---|
| P1 | Rubric letter-band dock | `apollo/handlers/done.py:566` → `apollo/overseer/misconception_detector/apply.py:65-99` (`rubric_overall_after_penalty`) | `new_score = max(0, min(100, original_score - round(penalty*100)))`; recomputes letter → **downgrades the band** the student sees; flows to XP (`done.py:622-626`) + diagnostic (`done.py:605-610`) | `detector_enabled()` (`done.py:529`) |
| P2 | Scorecard composite dock + ceiling | `apollo/grading/artifact_build.py:450` (`build_llm_artifact`) → `apollo/overseer/misconception_detector/apply.py:42-62` (`apply_penalty`) | `composite = composite - penalty`, then if `ceiling_applied` cap at `CEILING_COMPOSITE=0.84` (below Strong). Drives `render_scorecard`. **Same `penalty` number as P1, applied a second time to a different score.** | `detector_enabled()` (produces outcome) + `_grading_artifact_enabled()` (`done.py:843`) |
| P3 | Coverage sign-gate | `apollo/overseer/coverage.py:215-220` (`_sign_gate_equation_verdicts`) | Forces a sign-reversed equation from covered→missing (credit-denial on wrongness) | `detector_enabled()` (already) |
| P4 | P2.8 misconception axis drag | `apollo/handlers/done.py:504` (`_attempt_misconception_scores`) → `apollo/overseer/rubric.py:137-147` | An unresolved in-session misconception (0.5 on the 5% axis) can lower `overall` vs the axis being absent | `APOLLO_MISCONCEPTION_ENABLED` (per-turn inference, `apollo/overseer/misconception.py:503-505`) — the older Class-2 path, independent of the detector |

**Penalty-computation chain feeding P1+P2** (context, not itself a "grader penalty"): `detect_misconceptions` → `gate_findings` → `compute_centrality` → `merge_detections` (`done.py:532-565`), `merge.py:98-99` computes `penalty = min(SEVERITY_CLAMP=0.30, Σ severity)`, and `_any_central` sets `ceiling_applied`. Soft-fail: any exception → `detection_outcome=None` → unpenalized (`done.py:599-603`).

**A dormant THIRD penalty engine exists but is unreachable live** (all flags OFF everywhere): the graph-sim contradiction penalty — `apollo/graph_compare/soundness.py:38,52-78` (`CONTRADICTION_UNIT_PENALTY=0.5`, `soundness = 1 - min(1, n*0.5)`), surfaced by `graph_compare/scores.py:74-78`, and the subtractive `apollo/grading/composite.py:73-91` (`raw = w_n*node + w_e*edge - p*misc_penalty`) used only in `build_graph_artifact`. Gated behind `APOLLO_GRAPH_SIM_SHADOW_ENABLED`/`_LIVE_ENABLED`/`APOLLO_GRAPH_GRADER_LIVE` (`done.py:97,104,143`), never served today. **Out of scope for this memo** (it's not a live coverage-grader penalty), but flagged: if that path is ever promoted, it needs the same flag treatment, and its subtraction lives in `composite.py` which is PR #105 territory — do not touch here.

### 2.1 Reframing (important)
Because `APOLLO_MISCONCEPTION_DETECTOR` is OFF in prod today, the live coverage grader is **already penalty-free**. The stated goal — "coverage grader stops docking, detector becomes sole authority" — is really: **when the detector is turned on, ensure penalty authority flows through exactly one channel and the coverage/rubric grader adds no wrongness-sensitivity of its own.** The concrete work is (a) collapse the double-application of the same penalty (P1 vs P2) to a single deliberate channel, and (b) remove the coverage/rubric grader's residual wrongness-sensitivity (P3, P4) — all behind one flag, default OFF.

## 3. Design — flag-gated neutralization

**New flag:** `APOLLO_GRADER_POSITIVE_FOCUS` (default OFF, call-time read, `_TRUTHY` set, mirroring `detector_enabled`/`emergent_map_capture_enabled`). Add to `apollo/overseer/misconception_detector/config.py` (or a small `apollo/grading/positive_focus.py` — decide in T-W5a; the detector config module is the natural home since every neutralized path is detector-adjacent).

When **ON**, penalty authority flows exclusively through the detector/misconception-feedback path (the artifact `misconceptions[]` + the emergent map's future asserts), and the *served letter grade + coverage* are credit-focused:

- **P1 (rubric band dock) — neutralized.** At `done.py:566`, skip `rubric = rubric_overall_after_penalty(rubric, detection_outcome)` when positive-focus ON. The served rubric/band/XP is credit-only. `detection_outcome` is **still** computed and **still** threaded into `write_artifacts` (`done.py:855`) so the *feedback record* (`misconceptions[]`, and downstream the emergent ledger + map) retains full fidelity — the BAD is named, just not subtracted from the served band.
- **P2 (composite dock) — decision: RETAIN as the single penalty channel, OR neutralize alongside P1.** Two coherent options; **recommend RETAIN-P2-as-sole-channel** (documented default): the misconception penalty survives on the *scorecard composite* (the detector's owned deduction), while the *coverage grader's* letter band (P1) is credit-only. This makes the detector the sole authority on the composite while the coverage grade credits the good — matching the owner's framing precisely. If the owner instead wants the served grade fully penalty-free everywhere, neutralize P2 too by gating `artifact_build.py:450`'s `apply_penalty` call on the flag. **T-W5a implements RETAIN-P2 by default; the P2-also-off variant is a one-line flag guard the owner can request.** Either way, no change to `composite.py` math (only whether `apply_penalty` is invoked). **Orchestrator adjudication (2026-07-10):** the P2-also-off variant flag guard was deliberately NOT built in T-W5a — `artifact_build.py` is untouched by this task, P2 is unconditionally retained, and the variant remains a future option to implement only if the owner explicitly requests it.
- **P3 (coverage sign-gate) — neutralized.** At `coverage.py:215`, when positive-focus ON, skip `_sign_gate_equation_verdicts` so a sign error is not a coverage credit-denial by the grader — that wrongness is the detector's to name (it already localizes sign errors via the sympy_veto tier). Coverage credits the equation the student produced; the detector docks it in the feedback channel.
- **P4 (P2.8 axis drag) — neutralized to credit-only.** When positive-focus ON, filter `misconception_scores` so only *resolved* (1.0) contributions enter the axis (drop 0.5 unresolved), OR pass `misconception_scores=None`. Recommend **drop-0.5** (keeps the "you corrected a misconception" positive credit, removes the unresolved drag) — this is the most literal reading of "credit the good." Implemented at `done.py:504-511` (filter the dict before `compute_rubric`).

When **OFF**, every path is byte-identical to today: no flag read changes any value; P1–P4 all run exactly as now.

### 3.1 Non-goals (explicit)
- No change to `compute_coverage`/`compute_rubric` grading math (only *whether* P3/P4 inputs are wrongness-sensitive, via flag-guarded input filtering / call-skip).
- No change to `apply_penalty`/`rubric_overall_after_penalty` internals (only *whether* they are called).
- **No change to composite or abstention gates** (`composite.py`, `abstention.py`) — PR #105 territory.
- No change to the dormant graph-sim contradiction penalty (§2, out of scope).
- Zero behavior change when the flag is OFF.

## 4. Test strategy (TDD; in plan T-W5a)
- **Flag OFF golden byte-identity:** with detector ON and OFF, assert `handle_done`'s rubric, coverage, composite, XP, and artifact `misconceptions[]` are identical to base — for each of P1–P4's code paths.
- **Flag ON, per path:** P1 not applied (served rubric == pre-penalty rubric); P2 retained (composite still docked) under the default; P3 skipped (sign-reversed equation stays coverage-credited but detector still emits the finding); P4 credit-only (unresolved misconception no longer lowers overall; resolved still credits).
- **Feedback fidelity:** with the flag ON, `detection_outcome.misconceptions[]` is still populated and reaches the artifact — the BAD is recorded even though it's not docked from the served band.
- **Scope assertion:** `composite.py` and `abstention.py` are untouched (diff check).

## 5. Interactions
- **With the emergent map (this memo's sibling plan):** the emergent map's τ_assert asserts become *additional* entries in the same detector feedback channel (`candidate_assembly` → detector candidate → `MergeOutcome`). Positive-focus does not gate those — it gates whether that `MergeOutcome` reduces the *served coverage band*. Under RETAIN-P2, a promoted emergent misconception still docks the composite; under P2-also-off it only informs. This is consistent: the detector/map remains the sole penalty authority; positive-focus only decides which *served surface* that authority touches.
- **With PR #105 (not merged):** #105 tunes the abstention/coverage-min gate in `composite.py`/`abstention.py`; positive-focus removes wrongness-sensitivity *before* the composite and never edits those files. Orthogonal; no code conflict expected.
