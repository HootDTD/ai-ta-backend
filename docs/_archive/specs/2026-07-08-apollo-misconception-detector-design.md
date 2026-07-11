# Apollo Misconception Detector — Design Spec

**Date:** 2026-07-08
**Status:** Design approved (brainstorming) → pending spec review → implementation plan
**Scope:** Apollo student grading, `ai-ta-backend`
**Owner doc to reconcile on landing:** `docs/architecture/apollo.md` (drift contract)
**Supersedes/extends:** `docs/_archive/design/2026-07-06-abstention-signal-decision-memo.md` (option d),
`docs/_archive/specs/2026-07-05-emergent-misconception-store-design.md` (read-side consumer)

---

## 1. Problem

Apollo's misconception signal is dead end-to-end. On the 40-attempt `v2-qa-2026-07-08`
campaign, misconception detection fired **0/40**, and ~45% of misconception-class attempts
on trusted macro data banded **Strong** despite carrying a taught error.

Root cause (from the code map, §3): **detection is welded to resolution.** The only thing that
marks a misconception is `graph_compare/soundness.py::contradiction_nodes` — it counts student
nodes whose `canonical_key` starts with `misc.`, which requires the resolver to have *matched*
the student's assertion onto a `misc.*` candidate. So:

- An **additive** wrong belief (student covers every correct node *and* asserts a wrong extra,
  e.g. "GDP includes transfer payments") has no reference node to contradict → invisible.
- A **sign-reversed** equation (`NX = M − X`) gets *credited to the correct node* by a
  high-confidence tier → counted as coverage, never as a contradiction.
- A **conceptual** error stated as an omission ("used nominal where real was required") resolves
  to *missing*, and a missing node carries zero penalty.
- A **clarification answered wrong** withholds credit but emits no negative signal.

Additionally, on the **delivered** (live) path the grade is the LLM rubric:
`build_llm_artifact` sets `composite = rubric.overall/100`, and hardcodes
`misconception_penalty = 0.0` and `misconceptions: []`. The rubric's `misconception_corrected`
axis is fed only by `apollo_messages.metadata["misconception"]`, which the v1 chat path never
writes (guarded by `test_chat_no_signals.py`). So the live grade has **no misconception term at
all**, and even the axis that exists (weight 0.05) is too weak to move a band.

**The delivered grade is the LLM rubric; the graph-sim grader is a shadow that abstains 100%
(`GRAPH_GRADER_LIVE` OFF). Any fix that only touches the shadow grader does not change a single
student's grade.**

## 2. Goals / Non-goals

**Goals (this build):**

- G1. A **separate, grader-agnostic misconception-detection stage** that runs parallel to the
  coverage matcher and produces a per-concept misconception signal — decoupled from resolution.
- G2. Feed that signal into the **live** delivered grade via the existing `misconception_penalty`
  socket, so it moves the band a real student sees (closes D1).
- G3. Catch the **additive** (D3) and **sign-reversed equation** (D4) classes that coverage
  structurally cannot see.
- G4. Feed gated detections into the **emergent observation ledger** so the promotion / trust-
  gradient pipeline finally receives rows (closes the D2/D3 starvation).
- G5. Ship behind a **default-OFF flag**, validated by replaying the 40-attempt campaign before
  any environment flip.

**Non-goals (explicitly out of scope — deferred or separate work):**

- N1. **D5** — the clarification-refuted → misconception wiring (case #4). Bespoke, no external
  precedent, needs its own calibration. Deferred to a later increment.
- N2. The **omission** form of the conceptual error (student uses nominal figures but never
  asserts "nominal = real"). That is a coverage/rubric-completeness problem, not a detection
  problem, and no detector method covers it (see §3 research finding).
- N3. **D6** — resolver-v2 nondeterminism. The code map proved resolver-v2 is deterministic given
  fixed input; the observed variance is upstream conversation generation (`draft_reply`
  `temperature=0.7` + LLM-simulated student), not a pairing bug. **Action: correct the defect
  ledger, no engineering.**
- N4. Promoting the graph-sim grader to live (`GRAPH_GRADER_LIVE` stays OFF).

## 3. Evidence basis

Two parallel investigations ground this design (full memos preserved in the session transcript):

**Code map** (5 readers, apollo grading subsystem):
- Delivered path & the `misconception_penalty=0.0` socket: `apollo/handlers/done.py::handle_done`
  (coverage @396, rubric @403-410), `apollo/grading/artifact_build.py::build_llm_artifact`
  (composite=overall/100 @420-431, penalty 0.0 @427, `misconceptions:[]` @497).
- Detection chokepoint: `apollo/graph_compare/soundness.py::contradiction_nodes` /
  `is_misconception_key` (41-49). Fires only on a resolved `misc.*` node.
- The D4 **live** root cause is a prompt bug, not NLI: `apollo/overseer/coverage.py`
  `_BATCH_BINARY_PROMPT` (81-122) instructs GPT-4o verbatim *"equation: … Sign flips and
  algebraic rearrangements are equivalent."* The resolver's SymPy check
  (`apollo/resolution/tiers.py::_symbolic_equiv`, 165-207) is already sign-exact; its NLI tier
  **excludes** equation nodes (`NLI_NODE_TYPES`).
- Dead-but-reusable: `apollo/overseer/misconception_bank.py::match_by_embedding` (91-171) exists
  but is never called from grading.
- Emergent ledger writer already built & idempotent:
  `apollo/emergent/store.py::record_observations_from_canonical` (102-157), signature via
  `_signature_for` (52-59); call site `apollo/handlers/artifact_writer.py:214-241` gated by
  `emergent_misconceptions_enabled()`. Read-side bank↔emergent bridge already exists in
  `apollo/clarification/candidate_assembly.py`. **The loop is starved of rows, not unbuilt.**
- Prior art: `2026-07-06-abstention-signal-decision-memo.md` already picked "coverage +
  misconception composite"; the NLI positive-certify path (`APOLLO_NLI_MISC_POSITIVE_CERTIFY`)
  was built but recall is 1/7 because it lives *inside the resolver's node-matching*. A
  detector-side LLM-rubric penalty is **greenfield relative to the archive.**

**Research sweep** (10 lenses → synthesis → adversarial stress-test against 5 real cases). Key
conclusions:
- No single method is load-bearing. Case×method matrix: the comparative **judge** catches the
  additive case, **SymPy** deterministically kills the sign case, **neither** touches the
  clarification-refuted case (bespoke wiring), and the conceptual-omission case is largely
  uncovered by any detector.
- A lone LLM detector runs ~8:1 false alarms at realistic prevalence (PPV ~10.9%) → an
  **agreement gate + clarification routing is mandatory, not optional.**
- Use the **verdict-token probability** for the strict gate, not verbalized confidence
  (Reasoning's Razor: CoT models are overconfident at strict thresholds).
- Every precision method needs a **labeled Apollo calibration set** — which the campaign gives us
  (40 attempts, known expected class, personas checked in).
- Sentence-level, not atomic-claim, decomposition (MiniCheck: atomic = no gain, 2-4× cost).
- Equation sign errors need a **symbolic** check; even a cross-encoder NLI is fooled by operand
  order — so "swap to cross-encoder" is *not* sufficient for equations.

## 4. Architecture

A new **parallel detection stage** in `done.py::handle_done`, running alongside (not inside)
`compute_coverage`. The coverage matcher is unchanged; the detector only ever **caps or
subtracts**, never adds credit.

```
done.py::handle_done
├─ compute_coverage            (unchanged — the matcher)
└─ detect_misconceptions       (NEW parallel stage)  ── apollo/overseer/misconception_detector/
     ├─ Tier 1 — deterministic, always-on, ~0 false-positive
     │    ├─ equation sign-veto (reuse apollo/resolution/tiers.py::_symbolic_equiv)
     │    │    + fix coverage.py _BATCH_BINARY_PROMPT (drop the sign-flip clause; pre-gate
     │    │      equation nodes through SymPy)                                     → D4
     │    └─ CBM bank-pattern match (reuse misconception_bank.py::match_by_embedding,
     │         run against raw student utterances, independent of node resolution) → D3 anchor
     └─ Tier 2 — one comparative "correct-answer-trap" LLM judge, GATED
          fed: problem text + reference correct-belief + the concept's bank entries
          out (per concept): {clear | needs_clarification | misconception | wrong}
                             + evidence_span + verdict_token_prob
          gate: verdict-token probability threshold, calibrated on the campaign set
                • a deterministic Tier-1 hit, OR a cleared judge flag with ≥2 agreeing
                  signals                                               → dock
                • lone / low-confidence judge flag                      → clarification route,
                                                                          never dock
   ↓ merge  (apollo/overseer/misconception_detector/merge.py)
   severity-weighted subtract + central-misconception ceiling
     severity = f(verdict confidence, graph-derived node centrality)   (no hand-authored weights)
   ↓ feeds
   ├─ live  misconception_penalty + misconceptions[]   (artifact_build.py socket)   → D1
   └─ emergent observation ledger (gated detections)   (store.py writer)            → D2/D3
```

### 4.1 Component: detection stage (`apollo/overseer/misconception_detector/`)

New package (many-small-files per coding style). Grader-agnostic: input is the frozen student
graph + transcript utterances + reference solution + the concept's bank entries; output is a
`DetectionResult` value object (immutable):

```
DetectionResult = {
  per_concept: [ ConceptFinding(
      concept_key, verdict, confidence, severity, evidence_span,
      signature,        # misc.<code> if bank-matched, else unkeyed:<concept_id>
      source,           # sympy_veto | bank_pattern | judge
      corroborated: bool,
  ) ],
}
```

- **`sympy_veto.py`** — for equation-type student nodes, run `_symbolic_equiv` against the
  reference and against pre-authored sign/direction mutants of any bank equation. A mutant match
  (or a non-match of a claimed-equivalent form) is a deterministic, named sign misconception.
  Also the locus of the **D4 prompt fix**: `coverage.py::_batch_binary_match` must gate equation
  coverage through SymPy before the LLM, and `_BATCH_BINARY_PROMPT` must drop the sign-flip
  clause. (This half is separable enough to ship as its own small PR — see §8.)
- **`bank_pattern.py`** — reuse `misconception_bank.py::match_by_embedding` against raw student
  utterances (not resolved nodes), so an additive assertion that never displaced a reference node
  is still checked against the bank. Precision second opinion; abstains on no match.
- **`judge.py`** — one comparative LLM call per graded concept (batched), mirroring
  `apollo/overseer/diagnostic.py` structure: OpenAI via `apollo.agent._llm.main_chat`
  (gpt-4o, temp 0.0, `response_format=json_object`), with an injected `Fn`-Protocol DI seam so CI
  runs without a live model. Prompt holds the student answer side-by-side with (a) the correct-
  belief statement and (b) the concept's bank entries, forced to the 4-way output. Returns the
  verdict-token logprob for the gate. Malformed output soft-fails to `clear` (mirrors
  `diagnostic.py` try/except) — a judge crash must never break grading.

### 4.2 Component: gate + corroboration (`gate.py`)

- Gate on `verdict_token_prob >= τ_fire`, where `τ_fire` is **calibrated on the 40-attempt
  campaign labeled set** to hit a target precision on the strong controls (77, 89, 106 must not
  be flagged).
- **Corroboration:** a *dock* requires ≥2 independent detectors to agree (e.g. bank_pattern +
  judge, or sympy_veto alone counts as deterministic-corroborated). A single un-corroborated
  judge flag → `needs_clarification` route (feeds the existing clarification loop when enabled),
  **never docks**.

### 4.3 Component: merge (`merge.py`) — severity-weighted subtract + ceiling

- **Severity, no hand-authoring:** `severity = w(centrality) × confidence`, where `centrality`
  is derived from the reference graph already built for the attempt — a node on the main solution
  path / with high coverage weight / with downstream dependents is central; a leaf is peripheral.
- **Penalty:** `misconception_penalty = Σ severity_i` over corroborated concept findings,
  clamped to a sane max. Applied as a subtract on the composite.
- **Anti-dilution ceiling:** if any **central** concept carries a corroborated misconception, the
  band is **capped below Strong** regardless of surrounding correct coverage. The subtract gives
  graduation; the ceiling defeats "pad with correct content to soften the hit."
- Wired into `build_llm_artifact`: populate `misconception_penalty` (was 0.0) and
  `misconceptions[]` (was `[]`), then recompute `composite` as
  `renorm(overall/100 − penalty)` with the ceiling applied. Band cuts unchanged
  (Strong 0.85 / Proficient 0.70 / Developing 0.50).

### 4.4 Component: emergent-store feed

Gated corroborated detections are written to `apollo_misconception_observations` via the existing
`record_observations_from_canonical` path (or a thin sibling writer keyed the same way), using the
`misc.<code>` / `unkeyed:<concept_id>` signature scheme so bank-detected and node-contradiction-
detected occurrences of the same misconception aggregate under one trust score. Only gate-cleared
detections feed the ledger; the promotion `TAU` thresholds do the rest of the filtering
(consistent with the approved trust-gradient store design). No new table.

## 5. Data model

**No new tables.** Reuses `apollo_misconceptions` (static bank), `apollo_misconception_observations`
(emergent ledger, migration 037), and the `misconception_penalty` / `misconceptions[]` fields the
artifact already carries. Pre-authored equation **mutants** for sign-veto are added to the
per-concept bank content (offline, once per concept), not a schema change.

## 6. Flags

- **New:** `APOLLO_MISCONCEPTION_DETECTOR` (default **OFF**) gates the entire stage. When OFF,
  `done.py` is byte-identical to today (penalty stays 0.0).
- Interacts with `APOLLO_EMERGENT_MISCONCEPTIONS` (the ledger-feed half only fires when both are
  ON) and `APOLLO_NLI_MISC_POSITIVE_CERTIFY` (independent; the new detector supersedes it as the
  primary signal but does not require disabling it).
- Ships OFF in prod; environment flip is a human step after campaign validation passes.

## 7. Validation & acceptance

Primary harness: **replay the 40-attempt `v2-qa-2026-07-08` campaign** with the detector ON,
using `campaign/replay.py` (`build_rerun_inputs` → run). Fully local; no live grades touched.

Acceptance gate:
- **False-Strong on misconception-class attempts drops materially** from the 45% baseline
  (target: ≥ half reduction) **without** flagging the strong controls (77, 89, 106 must stay at
  their correct high band — zero false positives on the controls is the hard constraint).
- The five stress-test cases behave: additive-bank (110) and sign-reversed (114) are caught;
  the correct control (77) is untouched; conceptual-omission and clarification cases are
  documented as out-of-scope misses (N1/N2), not silent failures.
- Gated detections produce **non-zero** rows in `apollo_misconception_observations` (closes the
  D2 "0 rows" starvation) on the S1 alice/bob/cara repeat scenario.

## 8. Testing (95% patch-coverage contract)

- DI seam on the LLM judge (`Fn` Protocol) so CI runs offline; SymPy/bank tiers unit-tested
  against real inputs.
- Golden cases from the checked-in personas (e.g. `v2q_s2_gwen__net_exports_sign.json`) locked as
  regression fixtures.
- Assertions: (a) an additive-bank attempt that covers all nodes + asserts a bank misconception
  yields `misconception_penalty > 0` and band < Strong; (b) a sign-reversed equation never scores
  `covered` (SymPy gate) regardless of the mocked LLM; (c) a genuine sign-preserving rearrangement
  still passes (no over-correction); (d) a clean strong attempt is byte-identical to today
  (no regression) — detector emits nothing; (e) judge malformed-output soft-fails without crashing
  grading.
- The **D4 prompt/SymPy-gate** fix carries its own coverage as a standalone unit even if PR'd
  separately.
- `diff-cover` vs `origin/staging` ≥ 95% on all changed lines.

## 9. Risks & open questions (for the implementation plan)

- **R1. Extraction quality dominates FP risk.** CBM/SymPy precision depends on LLM claim/equation
  extraction from short prose. Measure extraction quality separately.
- **R2. Calibration-set transfer.** Ported precision numbers don't transfer; `τ_fire` and the
  severity `w(centrality)` curve must be tuned on our campaign set, not assumed.
- **R3. Severity formula constants** (centrality weighting, penalty clamp, ceiling trigger
  threshold) are TBD in the plan; start conservative (favor false-negatives over false-positives)
  and tune against the controls.
- **R4. Judge latency.** One gated LLM call per concept adds seconds on top of an already-100s+
  pipeline; acceptable per the research latency analysis, but keep the judge conditional (Tier 1
  abstains first) and batched.
- **R5. Bank sharpness.** Bank entries must be sharp, checkable propositions; a fuzzy bank poisons
  every downstream method. Audit the seeded macro bank before trusting judge-fed-bank precision.

## 10. Out-of-scope reconciliation

- **D5** (clarification-refuted → misconception): deferred (N1). The detector's `needs_clarification`
  route is the forward-compatible hook for it.
- **D6** (nondeterminism): non-issue (N3) — correct the defect ledger's locus from "graph
  pairing/resolution enumeration" to "campaign harness turn-generation nondeterminism."
- **Conceptual omission** (N2): route to coverage/rubric-completeness work, tracked separately.

## 11. Drift contract

On landing, reconcile `docs/architecture/apollo.md` in the same commit: register the new
`misconception_detector` package, document the parallel-stage data flow, the `misconception_penalty`
wiring, the new flag, and bump `last_verified`.
