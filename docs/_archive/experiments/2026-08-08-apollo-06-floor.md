# The 0.6 content floor: measured, rejected, reverted (2026-08-08)

Branch `feat/apollo-grading-p1-score-shape`. Transient record for the durable
invariant in `architecture/apollo/overseer/transcript-coverage.md` ("two levers
against the too-cheap 0.6 were measured and REJECTED"). Nothing here is a
description of current code — the floor is **not** in the tree.

## What was tried

`f625bcf` appended `_ZERO_FLOOR_RULE` to the adjudication system prompt, ahead of
the calibration exemplars: 0.6+ requires the student's own words to carry part of
the item's mechanism/definition/claim, with `evidence_span` as the operational
test, plus five named contentless shapes scored 0, plus one counter-exemplar
(attempt 189) defending the other direction. No code gate — deliberately.

The target was the wave-3 audit's false-pass pile: 13 replayed attempts that went
prod-F → C(60) on under 250 characters of student text, including one whose whole
contribution was `not sure` (8 chars) and one that was a 27-char list opener
naming nothing.

## Method (the part that matters for any future round)

**`gpt-5.1` at temperature 0 is not deterministic on this pipeline.** Two samples
of *identical code* over the same 43 attempts move **7.0–18.6% of letters** and
**6.7–13.3% of node credits**, with per-attempt score swings up to 60 points.
Single-draw before/after comparisons are not evidence here.

So: 4 full samples per arm over 43 attempts (pre-floor `1b990d6` vs hardened
`378a789`), plus 3 extra samples per arm on the 13-attempt pile (7 there). 468
live calls, 0 adjudication errors, 0 credit snaps, 0 enum downgrades. Claims were
tested against the within-arm spread and, where possible, against **deterministic
4/4-in-both-arms** node changes rather than p-values (a 4-vs-4 permutation test
floors at p = 0.0286, which carries no information beyond "ranges disjoint").

## Result: an inversion

| set | effect of the floor |
|---|---|
| A — contentless pile (n=13) | **zero effect**; 0 deterministic node changes. Zero-credits *fell* 10/126 → 3/126. 173 stayed C(60) 7/7 (span = `not sure`); 174 stayed C(60) 7/7; 35 got 0.6 with **no evidence span at all** 7/7; 100 moved **+15** and stabilised at C(60) |
| B — genuine partials (n=22) | mid credits 29/30/33/31 → 28/28/27/27; zeros 7/7/5/4 → 10/9/10/9; ranges disjoint |
| C — strong transcripts (n=10) | stayed in band, but A+ fell 7/7/6/7 → 6/5/4/5; 120 and 167 went A+(100) → A−(85) |

Of 90 node-slots measured 4×4 in both arms, **6 changed deterministically and 5
moved DOWN**: 73/`q14_contrast_with_examples` 0.6→0.0, 158/`q13_information_game_ante`
0.6→0.0, 158/`q13_library_to_paid_db` 0.6→0.0, 158/`q13_drop_outs` 0.85→0.6,
159/`q9_contrast_tangible` 1.0→0.85; only 65/`q22_enumerate_technologies` moved up.

Attempt-level, across the 43: 14 down, 6 up, 22 unchanged, net −3.80 points. The
three largest movers are all downward and all genuine partials — 158 C(63)→F(8)
(−55), 73 B−(73)→D(32) (−41), 71 D(46 avg)→F(23). The largest upward mover is a
19-character contentless attempt.

At the distribution level the floor did **nothing**: C band 34 (wave 3) →
34/34/35/36; F 10 → 11; 9 of 106 letters differ, inside the identical-code noise.

The targeted counter-exemplar also failed: 189's `q15_map_to_indignities` never
once landed on the intended 0.6 (pre-floor `[0, 0, 0.85, 1.0]`, hardened
`[0, 0.85, 0, 0]`).

## Why — two prompt self-contradictions, BOTH of which survive the revert

1. **The leniency sentence.** `build_system_prompt` assembles
   `base + <floor> + exemplars`, and `base` still ends: *"reach for 0.85 and 0.6
   whenever the work is genuinely between the extremes … Lean toward crediting
   genuine understanding rather than withholding it for imperfect wording."* Any
   floor appended after that is arguing with an un-caveated instruction, and the
   model resolved toward leniency exactly on the short transcripts.
2. **The span test vs its own exemplar.** The floor said *"if the best span you
   can quote from the student is not itself a claim about this item's substance,
   the credit is 0"* — and three sentences later the first 0.6 exemplar describes
   a student who *"says only that inequality gets worse, never naming who is
   affected or the consequence the item states."* That exemplar **is** attempt
   158's shape. The rule and its own calibrating exemplar collide on precisely the
   case the exemplar exists to protect, and the model resolved toward the rule.

A future floor must resolve both **in the draft**, not measure them afterwards.

## The clamp that was proposed instead, and why it must not ship

The re-validation's recommendation was "clamp credit to 0 when `covered` is
False". Simulated over the full 106-attempt composite (score function
reconstructed and verified to reproduce all 422 recorded scores exactly):

| distribution | F | D | C | B | A | median | σ |
|---|---:|---:|---:|---:|---:|---:|---:|
| prod (old bands) | 48 | 5 | 3 | 5 | 45 | 56 | 41.9 |
| v2 hardened (new bands) | 11 | 6 | 34 | 16 | 39 | 72 | 28.5 |
| v2 + clamp | 41 | 12 | 11 | 3 | 39 | 49 | 40.6 |

`covered` in this adjudicator means **fully** covered, so 104 of the 114 mid
credits carry `covered=False`, including 30 of the 39 on genuine partials. The
clamp is a scalpel on the pile and a hammer on everything between the extremes —
the exact failure P1 exists to remove.

The narrower candidate is `basis ∈ {stated, used, implied, absent}`: across 669
wave-3 credit log lines, `absent|0.6` is 39 (5.8%) versus ~30% for
`covered=False|0.6`, and genuine partials land on `implied|0.6` (71) and
`stated|0.6` (36). It could not be sized per attempt because it was logged but
never persisted — which is what the `basis` persistence work (same day) fixes.
**Instrument first, size second, gate third.**

## Standing constraints (unchanged)

* P1 code and the P1.4 re-authored rubric bank **ship together**. The false-pass
  pile is real and still unaddressed; reverting the floor returns to a known-bad
  state, not a good one.
* 33 of 106 attempts still sit on a single graded node, where the grade *is* one
  credit mapped through the bands.
* The arm-C bracket `[73, 86]` stands; the 86 figure must not be quoted.
* Part of the F reduction is definitional: the P1.5 rescale (F `[0,50)`→`[0,30)`,
  C `[60,65)`→`[50,65)`) relabels 15 of 106 prod attempts with no model call.

Raw artifacts (per-call rows, significance, noise, root cause, audit recheck) are
outside the repo, in the replay scratchpad under `replay/run-p1/harden/`.
