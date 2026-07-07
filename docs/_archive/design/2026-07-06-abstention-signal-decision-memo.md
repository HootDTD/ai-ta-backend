# Decision Memo — Apollo Graph-Grader Abstention Signal (§10)

**For:** Monday 2026-07-06 human decision · **Author:** Worker-A orchestrator (Fable)
**Status:** decision-support only — this memo makes NO code change and NO commit. The humans decide.
**Sources cited:** `a1-recall-verification.md`, `a1-failure-taxonomy.md`, `a1-iter3-floor-matrix.md`
(all under `ai-ta-backend/.superpowers/sdd/`); `f2-freeze-scorecard.md`, `f2-misconception-zero-probe.md` (same dir);
`docs/_archive/experiments/2026-07-02-e2e-campaign-diagnosis.md`; `apollo/grading/abstention.py`;
branch `weekend/g1-abstention-denominator` (commit `91c2eeb`, unmerged, default-OFF).

---

## 1. The decision in one paragraph

The graph grader abstains on **100% of gradeable attempts** (F1c 31/31, F2 34/34), every one via the
single reason `unresolved_rate_above_threshold` (`unresolved_rate > 0.35`, `abstention.py:34,132`).
The weekend proved this is **not a resolver-recall failure** — on the frozen benchmark the resolver
credits **every node each persona was designed to teach** (expected-credit recall 0.88–1.0, zero
genuine misses across 31/31 attempts; `a1-recall-verification.md`). Abstention is a **denominator
artifact**: `unresolved_rate` divides over 5–34 over-segmented *student* nodes against a 2–7 node
*reference* set, so even a perfectly-covered attempt sits at 0.50–0.96 (`a1-recall-verification.md`
Task 2–3). Monday must decide **what signal replaces or reshapes this gate**, because **graph-graded
fraction stays 0% — i.e. the graph path ships zero throughput and can never be promoted over the LLM
grader — until the gate changes.** The weekend also proved the obvious knob (lowering the floor, with
or without the denominator-v2 fix) **cannot separate strong attempts from misconception controls**, so
this is a signal-design decision, not a threshold-tuning one.

## 2. What the data rules out — any `unresolved_rate` floor as a class separator

The iter-3 floor×class matrix (`a1-iter3-floor-matrix.md`) swept floors 0.35→0.80 on one real
NLI-backed replay with **both** G1 levers ON (denom-v2 `APOLLO_ABSTENTION_DENOM_V2` +
equivalence-resolution `APOLLO_EQUIV_RESOLUTION`). Result: **no floor grades a useful fraction of
strong/partial attempts without also grading a misconception control** (which grades WRONG — band
agreement ~0 on every control at every floor):

| floor | strong+partial graded | controls that grade | note |
|---|---|---|---|
| 0.35 (live) | 1/18 | none | current: 0% effective throughput |
| **0.40** | **2/18 (~11%)** | **none** | last zero-control floor; 1 strong agrees, 1 partial disagrees |
| 0.45 | 3/18 | [34] misconception | control leaks in |
| 0.50 | 5/18 | [6,33,34,49] | 3 misconception + 1 vague control grade |
| 0.75 | 14/18 | 10 controls | throughput only by grading everything |

Per-attempt rates **interleave**: strong 0.385–0.917 vs misconception-control 0.444–0.917
(`a1-iter3-floor-matrix.md` §sorted-rates). The denom-v2 lever helps but does not separate — it only
excludes student nodes whose *type* has no candidate; over-segmented residue (equation variants,
generic procs, definition prose) **shares a type** with the candidate set and controls teach the same
type-mix, so their rates land in the same band. **Conclusion: `unresolved_rate` at any threshold is a
volume signal, not a content signal. It cannot be the promotion gate.**

## 3. Options

Each: mechanism · weekend data · false-credit exposure · throughput · impl cost · what to measure first.

### (a) Ship floor 0.40 + both G1 lever flags — minimal-throughput stopgap
- **Mechanism:** turn `APOLLO_ABSTENTION_DENOM_V2` + `APOLLO_EQUIV_RESOLUTION` ON, set floor 0.40.
- **Data:** last zero-control floor; grades 2/18 strong+partial (1 strong *agreeing*, 1 partial
  *disagreeing*) (`a1-iter3-floor-matrix.md`).
- **False-credit exposure:** zero *new* leak (controls stay abstained at 0.40); but the 9 pre-existing
  `control_credit_leak` attempts are unchanged by the levers, so any control that ever grades still
  grades wrong. Bounds precision regardless (see §4).
- **Throughput:** ~11% of strong+partial. Still shadow-comparison-only in practice.
- **Impl cost:** LOW — flags exist, default-OFF, byte-identical when off (`91c2eeb`, 100% patch cov).
- **Measure first:** does the 1 graded partial's *disagreement* indicate the floor admits wrong grades
  even inside the "safe" band? Re-run band-agreement on the graded set before trusting it.

### (b) Expected-set coverage as the abstention signal — grade iff expected-credit recall ≥ X
- **Mechanism:** replace/augment the gate with "did the resolver credit ≥X of the nodes *present in the
  reference set*" — a reference-denominated recall, not a student-denominated rate. `a1-recall` already
  computes this per attempt (`expected_credited / reference_set_size`).
- **Data:** strong personas = **1.0 (10/10)**; partial 0.5–0.8; misconception 0.667–0.857; vague
  0.667–1.0 (`a1-recall-verification.md` Task 1). Strong separates cleanly at the *top*; but
  misconception/vague controls also score 0.67–0.86 because they DO teach most reference nodes
  correctly (their error is a *contradiction*, not an omission) — so a pure ≥X coverage gate at, say,
  0.9 would grade some vague controls.
- **False-credit exposure:** MEDIUM — coverage is content-read (better than volume) but does not detect
  the wrong thing a control teaches; must be paired with contradiction (see d).
- **Throughput:** HIGH for strong (all 10 clear any X≤1.0); partial throughput depends on X.
- **Impl cost:** MEDIUM — signal already computed in replay; needs to move into `audited_grade` +
  artifact, plus a decision on the reference-denominator source at live time.
- **Measure first:** the false-positive rate of a 0.9 coverage gate on the 6 vague + 7 misconception
  controls; whether reference-set size is reliably known at live grading time (it is for seeded
  subjects; WU-AAS mint dedup is still shaky — G4).

### (c) Misconception-findings-aware signal — abstain unless contradiction detection ran cleanly
- **Mechanism:** gate on the *misconception/contradiction* channel. A strong attempt should yield
  **zero** contradiction findings; a misconception control should yield **one** — that IS the
  separating content signal §4 demands. PR #94 (`879a0eb`, merged) fixed the `misc.`-key-prefix bug in
  candidate assembly; the F2 probe (`f2-misconception-zero-probe.md`) then root-caused why detection
  was *still* 0/8. **No longer theoretical — the defect is located and bounded.**
- **Data (probe verdict):** cause (b) for fluid+macro — **resolution structurally cannot match
  paraphrased misconception utterances to `misc.*` candidates.** The NLI tier's misconception branch
  (`apollo/resolution/nli_resolution.py::match_nli_semantic`) is **veto-only**: entailment ≥0.98
  against a `misc.*` hypothesis blocks reference credit and returns `None`, but never *positively
  resolves* the misconception candidate. The lexical/fuzzy tiers require near-verbatim
  `trigger_phrases` overlap that paraphrased student text essentially never hits (attempt 1: node text
  "density of the fluid" vs bank phrase "ignore density"; attempt 33: "transfer payments … contribute
  to GDP" misses every `includes_transfers` trigger). Plus cause (a), linear_motion only: its bank was
  never seeded in F2 provisioning (bank correct for fluid+macro, 6 rows).
- **Enabling fix (bounded, known):** a **positive-certify path for `misc.*` in the NLI tier**,
  symmetric to the existing reference-certify branch, with its own threshold choice + linear_motion
  bank seeding. A tier extension, not a redesign — and NOT G1-gated (the probe disproved
  "detection is resolution-recall-gated": candidates are present and correctly keyed; only the
  positive-match path is missing).
- **False-credit exposure:** LOW-if-calibrated — the one signal that reads *what* was taught. Known
  hazard: the certify threshold must be picked against the polarity false-positive already observed
  (attempt 44, a *strong* persona, got a spurious `misc.nominal_for_real` hit at 0.9 on a *correct*
  nominal-GDP explanation — a certify path lowers the bar for exactly this collision class). §4 leak
  floor still applies.
- **Throughput:** as a separator it grades nothing by itself — it makes controls distinguishable so
  (b)/(d) can open throughput safely.
- **Impl cost:** MEDIUM — one bounded NLI-tier branch + threshold calibration + linear_motion seeding
  + tests.
- **Measure first (validation probe, now concrete):** implement the certify path, **rerun the 8
  misconception-control attempts** expecting ≥1 `contradiction_finding` each, AND rerun the 11 strong
  attempts expecting **zero** new findings (the attempt-44 polarity collision is the regression to
  watch). Pick the threshold from that confusion matrix, not a priori.

### (d) Composite gate — coverage ≥ X AND zero contradictions AND rate in band
- **Mechanism:** grade iff (expected-set coverage ≥ X) AND (contradiction findings == 0) AND
  (unresolved_rate within a loose sanity band, e.g. ≤0.95 to catch pure-noise transcripts).
- **Data:** synthesizes (b)+(c); the matrix shows neither component alone separates, but coverage reads
  omission and contradiction reads commission — together they cover both control failure modes. Not yet
  measured as a conjunction on the corpus.
- **False-credit exposure:** LOWEST *by design* (both content axes gated) — inherits (c)'s
  now-bounded certify-path work (and its attempt-44 calibration hazard) plus the §4 leak floor.
- **Throughput:** likely strong-only at first; expands as detection matures.
- **Impl cost:** HIGH — three signals wired + one integration test matrix; most surface area. But
  (c)'s root-cause probe removed the biggest unknown: the contradiction half is a bounded NLI-tier
  extension, not open-ended diagnosis.
- **Measure first:** the joint confusion matrix (strong vs each control class) on the frozen corpus
  once (c)'s certify path lands and its 8-control/11-strong validation probe passes; pick X from
  that, not a priori.

### (e) Keep shadow-only — no abstention change — until clarification-loop G2 data accumulates
- **Mechanism:** ship nothing to the gate; graph grader stays SHADOW (`APOLLO_GRAPH_GRADER_LIVE=0`),
  LLM grade continues serving. Let the clarification loop (G2, fixed in F2: traces on 33/34 artifacts,
  vague recall → 1.0) accumulate real resolution/clarification data, then revisit with more evidence.
- **Data:** F2 confirms the loop now fires and persists; the resolver-recall texture is understood; but
  no separating signal is *proven* yet, so any gate shipped now is a guess.
- **False-credit exposure:** ZERO (nothing served from the graph path).
- **Throughput:** ZERO graph-graded — but that is already true today; this option is honest about it.
- **Impl cost:** ZERO.
- **Measure first:** nothing blocking; the cost is calendar time and the risk that "more data" doesn't
  by itself produce a separator (§4 says it won't unless the signal changes).

## 4. The control problem, stated honestly

On **every signal measured so far** — `unresolved_rate` (any denominator), graph composite, parser/
normalization confidence, node-type mix — **misconception-control personas overlap strong personas**.
The matrix is unambiguous: strong rates 0.385–0.917 sit *inside* misconception rates 0.444–0.917
(`a1-iter3-floor-matrix.md` §1–2). The controls overlap because they are *good* transcripts that teach
most reference nodes correctly and differ only in the **content of the one thing they get wrong** — a
contradiction, not an omission or a volume difference. **Therefore any viable gate MUST read content
(coverage of the right nodes and/or detection of the wrong claim), never volume.** That eliminates the
entire `unresolved_rate` family as the separator and points at options (b)/(c)/(d).

**The precision ceiling.** The corpus carries **9 `control_credit_leak` attempts** (F1c baseline;
narrowed to **6–7 non-provisional** in F2 — 3× `cond.incompressibility`, 2 macro, 1 vague;
`f2-freeze-scorecard.md` §control_credit_leak). These are **upstream resolver leaks** — a control
persona getting one extra reference key credited that it should not — and they are **identical
flags-on vs flags-off** (`a1-iter3-floor-matrix.md` §4), i.e. no abstention signal creates or removes
them. **Implication: no abstention gate can be more precise than the resolver underneath it.** Even a
perfect content-reading gate will pass ~6–7 attempts carrying a wrong credit, because the *credit* was
already wrong before abstention ran. Fixing the leak is a `apollo/resolution/` task upstream of every
option here; until it lands, any promotion claim must quote the leak count as the precision floor.

## 5. Orchestrator's recommendation (NOT a decision)

**This section is the orchestrator's read, offered for the humans to accept, amend, or reject.**

1. **Do NOT ship any `unresolved_rate` floor as the promotion gate.** The data forecloses it (§2, §4).
   If a stopgap is wanted to unblock *shadow comparison plumbing only*, option (a) at floor 0.40 is the
   only defensible one (zero new control exposure), and must be labelled "instrument, not promotion."
2. **Fund the content signal — option (b)+(c) converging on (d), now with a cleared path.** The 0/8
   root cause is found (`f2-misconception-zero-probe.md`): the NLI misconception branch is veto-only
   and never positively certifies a `misc.*` match, and lexical tiers need near-verbatim trigger
   phrases. The enabling fix is bounded — a symmetric positive-certify path + threshold + linear_motion
   bank seeding — with no G1 prerequisite. This materially strengthens the coverage+contradiction
   composite (d) as the destination: both halves are now concrete, measured-or-boundable work.
3. **The Monday authorization is now a build+validate, not a diagnosis:** implement the `misc.*`
   certify path and run the defined validation probe — rerun the 8 misconception controls (expect ≥1
   `contradiction_finding` each) and the 11 strong attempts (expect zero new findings; attempt 44's
   polarity false-positive is the regression sentinel). That confusion matrix sets the certify
   threshold and unlocks (d).
4. **Treat the 6–7 control_credit_leak as the hard precision floor** and open a parallel
   `apollo/resolution/` leak-fix lane — no gate can beat the resolver beneath it.
5. **Default if undecided: option (e)** (stay shadow-only). Shipping the graph grade live over the LLM
   grade on any current signal would serve wrong grades to students; zero throughput is strictly safer
   than that until a content signal is measured, not assumed.

---

## 6. ADDENDUM — 2026-07-07 Monday decision + calibration outcome

**Human decision (product owner, 2026-07-07):** coverage + misconception composite gate —
abstain only when resolved reference coverage is ~zero AND no misconception finding was
detected. This is PR #100's option-(d) gate with two calibrated changes, landed on
`feat/apollo-composite-gate-calibration`:

1. **`APOLLO_COMPOSITE_COVERAGE_MIN` default 0.6 → 0.1.** Measured on the frozen F1c corpus
   (flags `APOLLO_NLI_ENABLED=1`, `APOLLO_NLI_MISC_POSITIVE_CERTIFY=1`,
   `APOLLO_ABSTENTION_COMPOSITE=1`; staging `e91fbea` + replay-row instrumentation;
   `campaign/out/f1c/replay-certify-composite-e91fbea.json`): the a-priori 0.6 abstained
   19/31 gradeable attempts, including 5/10 strong personas. Every correct-persona attempt's
   resolver-only `node_coverage_score` sat at >= 0.20 (strong 0.2–0.75, partial 0.2–1.0);
   the smallest non-zero signal on the longest declared path (7 nodes) is ~0.14. 0.1 therefore
   encodes "the resolver credited essentially nothing". At 0.1 the gate grades 31/31 on this
   corpus (0/31 under the old `unresolved_rate` gate); a true zero-signal attempt still abstains.
2. **>= 1 contradiction finding now GRANTS grading at low coverage** (it already never forced
   abstention). A detected misconception is gradeable content signal — the commission channel
   of option (d). Detection recall is still 1/7 on this corpus (`misc.nominal_for_real`
   on attempt 36 was the only hit; certify path fired with ZERO spurious hits on 24
   non-misconception attempts — the attempt-44 polarity sentinel stayed clean), so this branch
   is mostly future-proofing until the detection lane matures.

**Why not post-audit (credited-ledger) coverage — measured this time.** §3 option (b)'s
"strong = 1.0" evidence was the POST-transcript-audit credited fraction. Corpus-wide
measurement (persisted findings of the 2026-07-07 replay) shows that signal SATURATES:
post-audit coverage = 1.0 for 29/31 attempts INCLUDING all 6 vague controls, 6/7 misconception
controls, and 4/8 partial personas that deliberately OMITTED nodes (e.g. attempt 40: taught 3,
audit-credited to 4/4). The §6.3 transcript audit over-credits (rescues nodes the persona never
taught) — usable as event-rescue at 0.75 confidence, NOT as a gate/score signal (it would
launder auditor generosity into credit). **The gate deliberately reads PRE-audit resolver
coverage.** The audit over-credit itself needs its own investigation lane (S3's judge misses it
because S3 audits the ledger against the transcript, and the transcript usually CONTAINS
adjacent text the auditor accepts).

**What the gate does and does not claim.** At 0.1 the gate is a no-signal guard, not a class
separator — §4's conclusion stands (misconception controls interleave with strong personas on
every volume/coverage signal; only detection separates them, and it is 1/7). Graded controls
grade on coverage alone until detection matures. The graph path's composite SCORES remain
deflated (strong-persona composites 0.12–0.49: pre-audit node coverage, plus weak
`edge_coverage` — 19/31 zero, max 0.25, since edges need both endpoints resolved) — band
agreement vs the LLM grader is NOT claimed; that is the resolver-recall (clarification-loop)
+ edge-resolution lanes, not a threshold problem. Note also the scale ceiling: max composite
= 0.6·1.0+0.25·1.0 = 0.85 = the Strong band cut, so Strong is unreachable short of perfection.
Full decomposition + attack order:
`docs/_archive/handoffs/2026-07-07-apollo-composite-score-deflation-handoff.md`.

**Verification:** `campaign/out/f1c/replay-composite-calibrated-0p1.json` (0 abstentions /
31 attempts, 0 `degrading_without_nli`, volume-gate reasons still recorded as audit metadata).
Next lanes, in order of leverage: (1) transcript-audit over-credit probe; (2) misconception
detection recall (6 missed verbatim-trigger personas — resolution cannot match paraphrase, see
§3(c)); (3) `control_credit_leak` resolver fix (§4 precision floor, 9 attempts, unchanged);
(4) edge-coverage weakness (19/31 zero, max 0.25 — measured 2026-07-07; edges require both
endpoints resolved so edge recall compounds node recall; check edge-matching of
resolved-endpoint edges separately).
