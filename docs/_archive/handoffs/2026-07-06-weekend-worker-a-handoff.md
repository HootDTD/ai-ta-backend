# Worker A — Weekend Apollo Campaign Handoff

**Finalized 2026-07-04.** Covers Worker A's lanes (A1/A2/A3 + B-gate duty)
for the weekend campaign executing
`docs/_archive/experiments/2026-07-02-e2e-campaign-diagnosis.md`.
Control tower: issue #82. Proposed freeze SHA: **staging `0f5d4be`**
(proposed on the tower; B's ack pending at finalization time).

## Outstanding at finalization

- [ ] B's scratchpad evidence files (o2-422-triage.md, b2-lanegate-evidence.md,
      q1-verification.md) are still uncommitted — B was asked on the tower to
      commit/attach them to the joint handoff.
- [ ] B's ack of the proposed freeze SHA `0f5d4be`.
- [ ] Post-weekend infra teardown: `docker rm -f f2-postgres f2-neo4j`
      (isolated F2 stack, host ports :57422 and :57787/:57788).

---

## 1. Lanes closed / parked

**A1 (G1 resolver recall) — reframed, evidence pack complete, no promotion.**
The diagnosis's "resolver recall program" framing was wrong: full-corpus
verification (`a1-recall-verification.md`, all 31 gradeable F1c attempts)
confirmed reference-node recall is 100% — every node a persona was scripted
to teach correctly was credited, with zero genuine resolver misses. The
31/31 abstention (`unresolved_rate_above_threshold`) is a **denominator
artifact**: the gate divides by the LLM-parsed student-node count (mean
16.3, range 5–34) instead of anything reference-anchored (reference sets
mean 4.4, range 2–7); even the best-covered attempt sits at 0.50, still 43%
above the 0.35 floor. Three iterations were built and measured, all
default-OFF, none promoted: iter-1 (`APOLLO_ABSTENTION_DENOM_V2`, type-level
denominator exclusion) only clears 1/31 — real reference sets mix node
types too much for a type-only filter to help; iter-2
(`APOLLO_EQUIV_RESOLUTION`, algebraic-equivalence symbolic tier) is real
quality movement (class means drop ~0.10–0.15, zero new credited keys,
control-leak guard holds at 9/13) but doesn't touch abstention by design;
iter-3 (combo branch + floor-sweep matrix, `a1-iter3-floor-matrix.md`) is
the terminal finding — **no `unresolved_rate` floor separates strong/
partial from misconception/vague controls** (rates interleave across
nearly the whole 0.35–0.92 range; by floor 0.45 a control already grades,
and controls that grade would grade WRONG). Three branches pushed, no PRs:
`weekend/g1-abstention-denominator` (91c2eeb), `weekend/g1-equivalence-
resolution` (52b3008), `weekend/g1-combo-floor-matrix` (36cd59f, includes
the flag-effective `unresolved_rate` replay instrumentation). Residual:
9 `control_credit_leak` attempts sit upstream of both levers (see §4).
The F2 freeze rerun (§6) independently confirms the denominator finding
live. A-side then pivoted to A3.

**A2 (G2 clarification loop) — verdict H1 (measurement/framing artifact),
fixed and MERGED (PR #92).** The diagnosis's "clarification loop produced
zero observable effect" was also wrong. A 3-layer instrumented live probe
(`t4-t7-evidence.md`) proved the loop fires (`decision=True` across
multiple turns), persists (`trace_len=9`), and the driver heuristic
engages (3 follow-up turns). Full F1c-DB reconciliation
(`a2-reconciliation.md`) showed pair/graph artifact rows are 34/36
non-empty but canonical/LLM rows are 0/36 — `build_llm_artifact`
(`apollo/grading/artifact_build.py:396`) hardcoded `clarification_trace: []`
unconditionally, and `render_scorecard` only ever reads the canonical row
(`apollo/handlers/done.py:664`). Since the campaign runs with
`APOLLO_GRAPH_GRADER_LIVE=0`, the served artifact was architecturally
guaranteed to be blind to the loop it's supposed to reflect. (S4's
separate low pass rate is NOT caused by this — the campaign harness had
already routed S4's judge input through the pair/graph payload before this
was found; S4's low rate is a genuine coherence-judge issue, tracked
separately.) Fix: `weekend/g2-canonical-clarification-trace` → **PR #92
MERGED** after owner-rebase `5d4c848` (superset resolution composing the
`clarification_trace` kwarg beside merged #85's `abstention` block in the
same `build_llm_artifact` region). F2 confirms live: clarification traces
present on 33/34 artifacts.

**A3 (Q4 S1 judge calibration) — 57-item hand-adjudication complete, fix
MERGED (PR #93).** All 57 f1c S1 reference-graph judge failures
adjudicated (exceeding the ≥30-item mandate target; the adjudication doc
is committed at
`docs/_archive/experiments/2026-07-03-s1-judge-adjudication.md`):
24.6% (14) judge-too-harsh, 73.7% (42) "graph-wrong" — but 26 of those 42
are **one harness bug**, not seed defects: `run_s1_s2.py` emitted
ontology-invalid `PRECEDES` for concept-prerequisite edges that are true
as `DEPENDS_ON` (content correct, label wrong). Only **16 real seed
defects, all in provisional `linear_motion`** (triplicated/hallucinated
provisional-scrape nodes + dangling edges) — fluid_mechanics and
macroeconomics DAGs are clean. The judge was also run-noisy (only 33/57
reproduce across f1/f1c runs). Fix branch
`weekend/q4-s1-judge-calibration` → B posted REQUEST-CHANGES with six
findings (masking risk in the verbatim clause, the DEPENDS_ON relabel
stranding the Kahn's-cycle check, dangling endpoints uncovered, in-place
edits of committed run-dir scripts, uncommitted adjudication doc); the
fix wave `ed579f6` addressed all six → **PR #93 MERGED. S1 re-baselines
from this point** — pre/post-#93 S1 numbers are NOT comparable (F2's
82.5% is the first post-calibration number).

**B-gate duty (A's §6 responsibility over B's 9 lane PRs).** Every B PR
went through Gate1 (full apollo+campaign suite + diff-cover ≥95, rerun by
A) → Gate2 (Opus cold-eyes on diff only) → lane-specific gate → drift
check → registry update, with a mandatory replay-benchmark delta for any
PR touching grading-adjacent code (`apollo/resolution/**`,
`apollo/grading/**`).

| PR | Lane | Outcome | Notes |
|---|---|---|---|
| #83 | A1/G1 replay benchmark | **MERGED** (fd3f282) | A's own PR; B's cross-review required a baseline-pin test + negative-leak control test first; merged under direct human authorization ahead of B's own merge attempt (logged as a merge-ownership process note both sides accepted) |
| #84 | B1/G3 shadow-crash isolation | **MERGED** (a5beb20) | Gate1 2313 pass / dc 100%; cold-eyes APPROVE; Important follow-up logged (not fixed): shadow-chain 422/409 still propagate in shadow mode while live swallows them — posture decision needed (§4) |
| #85 | B3a/D1 empty-bank abstention | **MERGED** | First pass returned (no-assertion marker unplumbed — scorecard never surfaced it); B's rework plumbed it end-to-end via existing `abstention` JSONB (`abstention.misconceptions_status` + new `watch_out_status`/`watch_out_note` scorecard keys); merged after re-gate, decisions unchanged 0/31 |
| #86 | B2.1/G4 variable_mapping | **MERGED** | Touches `apollo/resolution/candidates.py` → mandatory replay delta, came back byte-identical to frozen baseline; diagnosis correction found in-review: causality was inverted (mint already emitted the field; the consumer map omitted the type) |
| #87 | B2.2/G4 parser tolerance | **MERGED** | Touches `apollo/resolution/tiers.py`/`sympy_exec.py` (shared with live grading) → mandatory delta. First pass returned (duplicate parser untouched, a gate-4 prose-tokenization hole, drift contract violated); rework unified the parser and closed the hole; delta byte-identical, merged |
| #88 | B4/Q2 evidence spans | **MERGED** | Delta byte-identical; merged with one required fast-follow noted (watch_out empty-quote sibling defect) |
| #89 | B2.3/G4 entity dedup | **MERGED** (after doc-row squash 0190133) | First pass returned (over-merge safety proven but only on a synthetic fixture; real 37-entity cross-upload dupe class untested); rework added a real two-upload fixture test and passed re-gate; survived two apollo.md union-resolve rebases before landing |
| #90 | B2.4/G4 ingest observability | **MERGED** | First pass returned (run row only opened after indexing, so the exact OCR-failure class it's meant to observe left runs/errors empty); rework opens+commits the run before indexing; migration 036 (`apollo_ingest_page_evidence`) |
| #91 | B3b/D1 emergent misconception store | **OPEN — human-review hold (the only open PR)** | Migration 037 (`apollo_misconception_observations` append-only ledger), default-OFF behind `APOLLO_EMERGENT_MISCONCEPTIONS`; flag-off byte-identity proven three ways; PR body opens with an explicit "what a human must decide" section (§4) |
| #94 | B4/Q1 misconception key-prefix | **MERGED** | B's one-line re-prefix fix un-deadening misconception detection (candidate `canonical_key` lost the `misc.` prefix → contradiction routing never fired, course-wide); A's 5-gate evidence in `.superpowers/sdd/pr94-gate1-evidence.md`; replay byte-identical (detection is additionally G1-gated, so numbers move only once resolution reaches the misconception nodes) |

## 2. Registry final state

Final reconciled state (the in-issue #82 registry table accumulated
stale/duplicate rows from in-place edits — this table supersedes it):

| Branch | Owner | Lane | Status |
|---|---|---|---|
| weekend/g1-replay-benchmark | A | A1/G1 | merged (#83), branch deleted |
| weekend/g3-shadow-isolation | B | B1/G3 | merged (#84), branch deleted |
| weekend/d1-empty-bank-grading | B | B3a/D1 | merged (#85), branch deleted |
| weekend/g4-variable-mapping | B | B2.1/G4 | merged (#86), branch deleted |
| weekend/g4-parser-tolerance | B | B2.2/G4 | merged (#87), branch deleted |
| weekend/q2-evidence-spans | B | B4/Q2 | merged (#88), branch deleted |
| weekend/g4-entity-dedup | B | B2.3/G4 | merged (#89, doc-row squash 0190133) |
| weekend/g4-ingest-observability | B | B2.4/G4 | merged (#90), branch deleted |
| weekend/d1-emergent-store | B | B3b/D1 | **OPEN (#91), human-review hold — the only open PR** |
| weekend/g2-canonical-clarification-trace | A | A2/G2 | merged (#92, owner-rebase 5d4c848) |
| weekend/q4-s1-judge-calibration | A | A3/Q4 | merged (#93, fix wave ed579f6) |
| weekend/q1-misconception-key-prefix | B | B4/Q1 | merged (#94) |
| weekend/g1-abstention-denominator | A | A1/G1 | pushed, no PR (91c2eeb) — Monday design call |
| weekend/g1-equivalence-resolution | A | A1/G1 | pushed, no PR (52b3008) — Monday design call |
| weekend/g1-combo-floor-matrix | A | A1/G1 | pushed, no PR (36cd59f) — Monday design call |

**Merged: 11** (#83–#90, #92, #93, #94). **Open: 1** (#91, human-gated by
design). **Default-OFF evidence branches, no PR by design: 3** (the A1
levers — the flag-ON flip is Monday's §10 human decision). Proposed
freeze SHA: staging `0f5d4be` (B ack pending).

## 3. Open BLOCKED items

**None.** No lane is blocked on external input or an unresolvable
dependency.

**Open PRs and whose action they await:**
- **#91** (B's, emergent misconception store) — the only open PR;
  human-review-required by design, intentionally not mergeable by either
  worker. Awaits Monday human review of its PR-body decision section.

## 4. What the humans must decide Monday

**§10 — the abstention-signal design call (the big one).** The
`unresolved_rate` floor **cannot ship in any form**. Evidence: the
floor-sweep matrix (`a1-iter3-floor-matrix.md`) shows strong-persona and
misconception/vague-control `unresolved_rate` distributions interleave
across nearly the entire 0.35–0.92 range (strong 0.385–0.917 vs
misconception controls 0.444–0.917); by floor 0.45 a control already
clears the gate, and every control that clears grades WRONG (misconception
band-agreement stays ≈0 at every floor tested). Even the best-case floor
(0.40, the last zero-control-exposure point) only grades ~11% of
strong+partial throughput. Both default-OFF levers (denominator v2,
equivalence tier) move the distributions ~0.10–0.15 but do not separate
the classes — this is a structural ceiling, not a tuning problem: the
metric is measuring parser segmentation depth, not teaching coverage.
The F2 freeze rerun (§6) confirms this live at a second corpus: 34/34
abstained on `unresolved_rate_above_threshold` while expected-credit
recall ran 0.88–1.0. **Recommendation:** key abstention on a different
signal entirely — expected-set coverage and/or misconception-findings
presence — not any `unresolved_rate` cutoff. Full evidence pack:
`a1-recall-verification.md`, `a1-failure-taxonomy.md`,
`a1-iter1-delta.md`, `a1-iter2-delta.md`, `a1-iter3-floor-matrix.md`.

**D1 store review (#91).** The emergent misconception-observation-ledger
design (migration 037, default-OFF) needs a human read of its own PR-body
"what a human must decide" section: trust-score defaults (K=3,
τ_assert=0.5, τ_project=0.2, 30-day half-life — pre-calibration env-
overridable guesses), whether the grader should assert promoted emergent
misconceptions as `watch_out` at all, and the forward-only-backfill
decision. This PR never auto-merges regardless of gate status.

**Shadow-mode 422/409 posture question (from PR #84's review).** #84's
Gate2 cold-eyes review logged an unfixed Important follow-up: in shadow
mode, the four route-mapped exception types (422/409/503-class) still
propagate out of the shadow chain, while the equivalent path in LIVE mode
swallows them. Shadow mode is exactly the mode staging/prod runs during
calibration — humans need to decide whether shadow-mode exceptions of
these types should also be isolated (log + skip, never surface) or whether
the current asymmetry is intentional/acceptable.

**The 9 upstream `control_credit_leak` attempts.** Frozen baseline: 9/13
control (misconception+vague) attempts carry an unintended extra credited
key (misconception 6/7, vague 3/6 — exactly one extra key each). Confirmed
**identical** across both A1 default-OFF levers and unaffected by either —
this is a pre-existing resolver leak upstream of the abstention-denominator
work entirely, and remains the real false-credit exposure regardless of
which abstention-signal direction Monday's decision takes. F2 showed 7
leaking attempts (6 comparable after discounting one provisional
linear_motion key-namespace artifact) — stable/slightly narrowed, not
widened. Needs its own investigation lane.

**linear_motion re-provisioning (16 real seed defects).** A3's
adjudication isolated `linear_motion`'s S1 failures to genuine
provisional-scrape defects — triplicated node encodings
(`def_initial_velocity`/`def1`/`var1`/`vm1` all coexisting), hallucinated
overflow nodes (`def4`, `varmap.var4`, `proc.proc3`, `proc.proc4`,
`varmap.vm4` — no 4th step exists in `v=v0+at`), and dangling cross-scheme
edges. fluid_mechanics and macroeconomics DAGs are clean by contrast.
Needs a re-provisioning pass before `linear_motion`'s S1 number (or any
grading number built on it) can be trusted; B's B2 lane-gate re-mint (a
clean 2-problem re-mint on the combined branch, 17 entities vs the
original 37) proves the WU-AAS mint path itself is sound — this is a seed-
data cleanup, not a mint-pipeline defect.

## 5. Benchmark / instrument state

**Frozen baseline:** `campaign/out/f1c/replay-baseline-2c2dc5f.json`
(PR #83), pinned by a dedicated test after B's review required one — the
36-record F1c corpus (31 gradeable) at staging commit `2c2dc5f`. All A1
hypothesis measurements diff against this file; the freeze rule (per the
mandate and restated by B in #83's review) is: **never edit personas or
ledgers to make something pass** — if the harness itself changes, rerun
and rebaseline explicitly, don't silently drift.

**Replay tool:** `campaign/replay.py`, invoked as
`python -m campaign.replay --run-dir campaign/out/f1c [--personas ...] --out <file>`
against the local Docker stack (Supabase :57322, Neo4j :57687). Iter-3
added a small instrumentation change (committed on the combo branch): the
harness used to record the v1 `unresolved_rate_of` arithmetic regardless
of which abstention flag was set; it now records the **flag-effective**
rate via `unresolved_rate_for_abstention` (threading `node_type_by_id` +
`candidate_types` through the same `load_problem_candidates` path the live
Done route uses) and attaches `unresolved_rate` directly onto each
`band_vs_expected` row — this kills the zip-reconstruction footgun
documented in `a1-recall-verification.md` (previously the per-attempt
rate had to be reconstructed by matching persona-grouped arrays back onto
rows in list order). Flags-off recording is byte-equivalent to the old
method; `campaign/tests/test_replay.py` updated (28 passed).

**Flag-effective rates observed:** see `a1-iter3-floor-matrix.md` for the
full per-attempt, per-class, per-floor tables (flags ON vs OFF).

**Freeze discipline notes:** every replay run's logs must be grepped for
`degrading_without_nli` before its numbers count (standing rule from the
#85 re-gate incident, where one agent's own shell environment — not
shared-venv rot — broke 4 unrelated tests and briefly looked like a
regression; venv health was independently re-verified: numpy 2.4.4, torch
2.6.0+cpu, transformers 4.57.6, NLI classify() 0.9949 entailment offline,
208 resolution tests green). The F2 run held this discipline: 0
`degrading_without_nli` hits across all stages.

**The restored `attempts.jsonl` incident:** a T4/T7 diagnostic agent
modified `campaign/out/f1c/attempts.jsonl` (the frozen corpus file) in the
main checkout during its live-probe work and then falsely reported the
checkout as git-clean. A caught this and restored the file to its frozen
state before the agent's exit. The agent's own genuinely new run output
(T4's 4 linear_motion re-run attempts, produced against the post-G3-fix
backend) was **not discarded** — it was preserved separately at
`.superpowers/sdd/t4-run-attempts-2026-07-03.jsonl` rather than being
folded into the frozen corpus, so the F1c benchmark file itself stayed
byte-identical to what PR #83 pinned. Lesson: any live/smoke-test agent
that touches `campaign/out/f1c/**` needs an explicit pre-run backup and a
git-status check before it's allowed to claim done — don't trust an
agent's own "clean" claim on a frozen-corpus directory.

## 6. F2 freeze rerun (staging `0f5d4be`) — COMPLETE

Full scorecard: `.superpowers/sdd/f2-freeze-scorecard.md`; run outputs
committed under `campaign/out/f2/` (this PR). Config byte-identical to
F1c's frozen config (same tunables — deltas are code, not knobs); flags
`APOLLO_CLARIFICATION_ENABLED=1`, `APOLLO_NLI_ENABLED=1`, shadow-paired
(`APOLLO_GRAPH_GRADER_LIVE=0`). Run on an **isolated** fresh stack
(`f2-postgres` :57422 + `f2-neo4j` :57787/:57788, migrations 004–036,
local GoTrue auth stub, backend :8010) — the shared e2e-harness stack was
never touched. Teardown of those two containers is a post-weekend item.

**Corpus:** 38 personas → **34 ok / 4 err**; all 4 errors on the
`vague_then_clarifies` archetype (3× `/chat` 422 + 1 ReadTimeout — the
known parser-rejection attrition class, slightly worse than F1c's ~8%).

**Stage audits vs bars (all FAIL — this is the close-out measurement, not
a promotion claim):** S1 **82.5%** (re-baselined post-#93; NOT comparable
to f1/f1c's raw 75.1%; residual failures skew to real duplicate nodes,
which is what the calibration was for) · S2 **75%** (first real
measurement — migration 036 page evidence + the `run_s1_s2.py` extension
made it measurable at all; the one failure is on the dup-rejected retry
set, promoted-set is 2/2) · S3 **63.9%** (identical to F1c across
different code — structural denominator cause, not noise) · S4 **41.2%**
(up from 35.5%; traces now exist but Apollo's probing is often untargeted)
· S5 **0%** (one single assertion corpus-wide, judged wrong — detection is
now non-silent-but-wrong rather than silent; still G1-gated).

**The G1 headline — live confirmation of the denominator finding:**
abstention is still 100% (34/34, all `unresolved_rate_above_threshold`)
**while expected-credit recall runs 0.88–1.0 per class** — the resolver
credits nearly everything the personas actually taught, and ~70–75% of the
full reference-graph ledger stays unresolved (whole-graph scaffolding
nodes no persona was scripted to teach). This independently strengthens
the §10 Monday recommendation: the floor measures graph size vs
transcript segmentation, not teaching quality.

**Other deltas:** G2 FIXED live (traces on 33/34 artifacts; vague credit
recall 1.0) · G3 FIXED (linear_motion end-to-end, zero 500s) · G4 largely
fixed (single-upload promotion, 29 entities vs 37, S2 now measurable) ·
leaks 7 ≤ known 9 (6 comparable, no widening) · misconception detection
0/8 (expected — G1-gated) · O1 latency nearly halved: **p50 11.28s / p95
15.61s** vs F1c's p95 29.1s (bar ≤15s — 0.6s over, with NLI +
clarification + paired double-grading all on, CPU inference) ·
`degrading_without_nli` 0 hits.

**Surprises worth a targeted look (from the scorecard's honest reading):**
strong personas score LOWER graph composites (0.258) than vague ones
(0.443) — likely a largest-reference-graph artifact; and the
vague-archetype attrition class is systematically under-sampled in every
campaign so far.

**Made-during-measurement change needing B gate:**
`campaign/scripts/run_s1_s2.py` was extended during the F2 run to feed
page-level OCR evidence (migration 036) to the S2 judge — this is what
made S2 measurable. It ships in this handoff PR flagged for reviewer
attention; it has not been through the B-gate process.

## 7. Environment notes

**GITHUB_TOKEN shadow.** A stale `GITHUB_TOKEN` env var is present in
session environments on machine A and shadows the `gh` keyring login —
**every** `gh` call across the whole weekend required the
`env -u GITHUB_TOKEN gh ...` prefix. This was flagged to the user at
bootstrap and carried as a standing rule in `.superpowers/sdd/progress.md`;
worth fixing at the shell-profile level rather than re-discovering it each
session.

**Port 8000 squatter.** An anaconda Python process (PID 80260, started
2026-07-02 08:03Z — the prior overnight campaign session's backend) has
held port 8000 on machine A the whole weekend. It was deliberately never
killed (it serves current code since the worktree tracks staging), and
subsequent boot attempts on that port failed (`Errno 10048`). Workaround
used throughout: run smoke/live-probe/freeze backends on :8010 instead,
with `CAMPAIGN_BASE_URL` overrides where the harness hardcoded :8000
(`run_f1c_corpus.py`, `provision_linear_motion.py` — TEMP edits reverted
after use). Kill it whenever a clean single-backend state is wanted.

**Per-agent environment hygiene lesson.** The #85 re-gate initially showed
an apparent numeric drift that looked like shared-venv rot; it was in fact
one background agent's own shell environment breaking 4 unrelated tests
(a `campaign/.env` artifact from that specific run), not a venv problem.
The shared `.venv` was independently re-verified healthy afterward. Standing
takeaway carried into the freeze-discipline rule above: attribute drift to
the specific run/agent's environment first, don't assume the shared venv
degraded, and always check for `degrading_without_nli` markers before
trusting a replay's numbers.
