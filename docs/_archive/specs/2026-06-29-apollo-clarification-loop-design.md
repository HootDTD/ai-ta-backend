# Apollo Clarification Loop — Design Spec

**Date:** 2026-06-29
**Status:** Design approved in brainstorm; ready for implementation planning.
**Owner doc to reconcile on landing:** `docs/architecture/apollo.md`
**Supersedes / cancels:** `docs/_archive/plans/2026-06-23-apollo-grader-phase1b-plan.md`
(teacher-authored exact aliases — cancelled, see §11).
**Predecessor context:** `docs/_archive/handoffs/2026-06-29-apollo-clarification-loop-g2-handoff.md`
(the brainstorm handoff this spec resolves).

---

## 1. Problem

When a student explains an idea to Apollo in their own words and the grader's
matcher cannot confidently map that idea onto the problem's answer key, the idea
is marked **unresolved**. When the share of unresolved ideas crosses a gate
(`unresolved_rate > 0.35`, `apollo/grading/abstention.py`), the grader
**abstains** — it refuses to produce a Layer-3 grade. This is the **G2
resolver-recall** problem documented in the predecessor handoff and the
`2026-06-26-apollo-grader-node-recovery-e2e` feedback report.

Two facts from the investigation (handoff §2) anchor this design:

- **The abstention driver is articulated-but-unresolved ideas (MECH-B).** An
  idea the student *never mentioned* (MECH-A) produces no student node, shows up
  only as a coverage `missing_key`, and **does not** trip the abstention gate.
  So fixing abstention means fixing recall on ideas the student *did* express.
- **The resolver's last resort today is a single silent LLM guess**
  (`apollo/resolution/adjudication.py::adjudicate`) that maps an ambiguous node
  to a candidate or drops it to `unresolved` when unsure. That silent,
  precision-tuned guess is the low-recall step driving abstention.

The matcher fails on the common case "student said the right idea in different
words," because the resolver's tiers are **word/structure based** (exact →
SymPy-symbolic → derived → normalized-alias → RapidFuzz `token_set_ratio ≥ 0.9`;
see `apollo/resolution/tiers.py`). Paraphrases with low word overlap fall
through to the silent guess and usually to `unresolved`.

## 2. Goal

Convert articulated-but-ambiguous student ideas into a **decisive verdict** by
having Apollo ask the student a targeted, mid-conversation follow-up. Recall
comes from **meaning-based matching**; the credit decision comes from the
**student's answer**, not the machine's guess.

The mechanism is the reframed G2 solution: the matcher no longer has to *decide
correctly* (hard, high-precision); it only has to *notice plausibility* (easier,
high-recall, sloppy precision is fine because the student filters false
positives).

## 3. Non-goals (explicitly deferred / rejected)

- **The misconception-bank trust-gradient / promotion pipeline.** We emit
  structured misconception *evidence* rows only (§8). The bank pipeline is a
  separate, not-yet-designed effort (see memory `apollo-misconception-bank-decisions`).
- **The per-student misconception profile system.** We store evidence in a
  shape that *supports* a future rollup (§8); we do not build the profile here.
- **A Done-time coverage safety net.** Deferred (§10). It targets MECH-A /
  detector-misses, which do not drive abstention; revisit only if measurement
  shows live coverage is insufficient.
- **Phase 1b teacher-authored exact aliases.** Cancelled (§11) — this design
  obviates it.
- **Embedding auto-credit.** Rejected (§4) — embeddings never grant credit.

## 4. The core principle

> **Meaning-matching (embeddings) only ever routes an idea into the "ask the
> student" bucket. It never grants credit by itself.** Every credit is either
> *structurally proven* (exact text / equation-equality / declared derivation)
> **or** *student-confirmed*. Never "the vectors were close."

Rationale — the false-friend / negation trap: two sentences such as *"pressure
is higher where the fluid moves slower"* (correct) and *"…moves faster"* (the
classic misconception) are near-identical in both words and embedding space, yet
mean opposite things. Embeddings cannot separate them; a committed student
answer can. Letting embeddings auto-credit would credit the misconception. So
embeddings *detect*; the student *decides*.

This principle lets us **delete the silent LLM adjudication** and **cancel
Phase 1b** — both are subsumed by one mechanism: *embeddings notice → student
confirms.*

## 5. Architecture overview

The feature is two phases sharing one new persistent record:

1. **Live (per teaching turn):** detect ambiguous ideas, ask the student about
   them (answer-blind), and re-score the student's answer on the following turn.
2. **Done (grading):** feed the clarification outcomes into the shadow
   graph-grader's resolution, and remove the silent adjudication.

Both graders benefit. The shadow graph-grader (`resolve_attempt`,
`apollo/handlers/done_grading.py:228`) consumes the clarification outcomes
directly. The currently-live LLM grader (`compute_coverage`,
`apollo/handlers/done.py:272`) consumes the now-richer transcript and student
graph. The improvement therefore lands regardless of which grader is promoted.

```
teaching turn (handle_chat)
  parse utterance -> student nodes               [existing]
  ┌─ NEW: clarification detect ──────────────────────────────┐
  │  load candidate set (refs + misconceptions)               │
  │  deterministic tiers -> confident? silent, no question    │
  │  residual nodes -> embedding match vs candidates          │
  │    ambiguous band -> flag + answer-free probe hint        │
  │    too far        -> leave                                 │
  │  pick up to 3 by rubric weight -> probe hints             │
  └───────────────────────────────────────────────────────────┘
  draft_reply (answer-blind) weaves in probe(s)    [existing generator]
  leakage_judge backstop                           [existing]
  record asked-and-waiting rows                     [NEW table]

next student turn
  parse reply -> nodes                              [existing]
  for each asked-and-waiting idea:
    re-score (orig statement + clarification + candidate)
      -> confirmed | refuted(+evidence) | vague     [NEW]

/done (grading)
  resolve_attempt(..., confirmed_resolutions=...)   [MODIFIED]
    confirmed -> method "clarification" @ 0.90
    refuted/vague -> unresolved
    silent adjudication REMOVED
```

## 6. Live detection & probing (per turn)

**Seam.** In `apollo/handlers/chat.py::handle_chat`, after `store.write_nodes`
builds the accumulated `student_graph` (chat.py:284) and before `draft_reply`
(chat.py:289).

### 6.1 Candidate set
Build the closed candidate set for the current problem — reference-solution
ideas + course misconceptions (`apollo/resolution/candidates.py::build_candidate_set`,
already used in the grading path). ~15–25 candidates. This is the one piece the
chat path does not build today; it must be assembled per turn (or cached on the
attempt for the problem's lifetime).

### 6.2 Confident credit — unchanged
The turn's new nodes run through the existing deterministic tiers (exact →
symbolic → derived → alias → fuzzy ≥ 0.9). A confident match is left to resolve
silently at grading **with no question** (brainstorm Q2). Embeddings do not
create a new confident tier; the confident band stays deterministic.

### 6.3 The detector — embedding similarity
For each **residual** node (one no deterministic tier confidently matched):

- Compare the node's surface text (`apollo/resolution/tiers.py::student_surface_text`)
  against each candidate's surface forms by **embedding cosine similarity**,
  using the stack's `text-embedding-3-large`.
- **Candidate embeddings are precomputed and cached**, keyed on the existing
  `apollo/grading/reference_hash.py` so the cache invalidates when the reference
  changes. Misconception trigger phrases are embedded alongside.
- **Student embeddings:** embed the turn's residual nodes in **one batched
  call**.
- **Band:** `cosine ≥ T_ambig` → **ambiguous** (probe); below → **leave**
  (genuine gap / off-topic, not probed). There is no upper cut — embeddings
  never credit, so a very high cosine just means "very likely worth asking."
- `T_ambig` is a **calibration parameter**, recall-tuned (tolerant precision).
  Proposed starting default **0.50**, validated against the existing E2E probe
  corpus (`scripts/apollo_grade_probe.py`, `scripts/_macro_scenarios.py`)
  during planning; owned by the calibration effort (spec §10 of the grader
  master spec), not frozen here.
- The detector records, per flagged node, the **top candidate** (highest cosine)
  — needed to build the probe hint and to re-score later.

The detector is **high-recall by design**: false positives cost only a question
the student dismisses.

### 6.4 The probe — answer-blind
**Rule: reveal the dimension, not the value.** The student already named the
entities; we never reveal *which way it goes* / *what the value is*.

- The detector emits an **answer-free probe hint** for each flagged node — a
  short instruction naming the *dimension to pin down*, derived from the node
  type and the (hidden) candidate, e.g. "make the student commit to the
  **direction** of the relationship they described," "ask which **variable**
  they would solve for," "ask them to **define the term** in their own words."
  The hint must never contain the candidate's claim/value.
- **Generation stays in the existing answer-blind generator**
  (`apollo/agent/apollo_llm.py::draft_reply`). It already only knows what the
  student has taught it; we pass it the probe hint as steering, never the
  reference. Apollo phrases the question naturally, in its confused-classmate
  voice.
- The existing **leakage judge** (`apollo/agent/leakage_judge.py`) backstops
  every reply, including clarification questions, with its current
  soft-fail-open behavior.

### 6.5 Pacing (brainstorm Q5.4)
- **Up to 3 follow-up questions per turn.**
- **One follow-up per idea** — never re-ask the same idea.
- When more than 3 ideas are flagged, choose by **rubric weight**
  (`apollo/overseer/rubric.py`); fall back to detector cosine when no weight is
  available.

### 6.6 Persist
For each probed idea, write an `asked-and-waiting` row to `apollo_clarifications`
(§7) carrying the node, the top candidate, the generated question, and the turn
index.

## 7. Re-scoring the student's answer (next turn)

On the following teaching turn, the student's reply is parsed as usual. For each
`asked-and-waiting` idea, a **small focused AI check** reads
**(original ambiguous statement + the student's clarification + the one
candidate idea)** and returns one of:

- **correct** → state `confirmed`. Credited at grading (§9).
- **wrong** → state `refuted`. **No credit**, and emit a **misconception
  evidence** record (§8).
- **vague / unclear** → state `vague`. **No credit.**

This is *not* the deleted silent guess: that guessed from nothing; this judges a
**committed answer to a pointed question** — a far more decidable call.
Embeddings cannot do this step (they cannot tell correct from its negation);
word/structure matching cannot (the clarification is free text). A focused AI
judgment is the only mechanism that can rule on correctness, and it is now doing
so over high-information input.

For grading, **wrong and vague collapse to the same outcome: no credit /
unresolved.** "Unresolved" therefore now means *"we asked directly and the
student could not confirm it"* — earned, not gamed.

The re-scorer, embedder, and leak judge are all **dependency-injected and
stubbed in tests** (mirroring the existing `Adjudicator` / `LeakageJudge`
patterns) so no live model call fires in CI and behavior stays deterministic.

## 8. Misconception evidence & profile-readiness (brainstorm Q5.6)

A `refuted` outcome is a uniquely valuable signal — it distinguishes "student
never addressed this" from "student actively believes the opposite." We capture
it **without affecting the current grade**:

- The `refuted` row in `apollo_clarifications` *is* the evidence record. It
  carries enough to roll up into a future per-student misconception profile:
  `student_id`, `concept_id`, the candidate it contradicts (`candidate_key`),
  the student's wrong claim (original statement + clarification text), and
  timestamps.
- No write to the misconception bank and no profile aggregation are performed
  here — those are deferred (§3). The data shape simply does not foreclose them.

## 9. Grading changes (Done)

`apollo/resolution/resolver.py::resolve_attempt` gains the clarification
outcomes as an input (e.g. `confirmed_resolutions: dict[node_id ->
(candidate_key, "clarification", 0.90)]`, threaded from `done_grading.py`):

- **Confirmed** nodes resolve via a **new `clarification` method**, confidence
  **0.90** (brainstorm Q5.3). Add `"clarification"` to `RESOLUTION_METHODS` and
  `METHOD_CONFIDENCE_CAP["clarification"] = 0.90` in
  `apollo/resolution/candidates.py`. 0.90 clears the `min_normalization_confidence`
  abstention floor of **0.85** with margin (so a correctly-clarified idea is not
  then discarded by the quality gate), and sits below the deterministic "proven"
  tiers (exact 1.0 / symbolic 0.98 / derived 0.95) and just under alias 0.92,
  reflecting that it is a strong but AI-read judgment, not a structural proof.
- Confirmed resolutions are applied **before** the deterministic tiers and are
  authoritative (the student committed).
- **The silent LLM adjudication is removed** from the residual path: a node not
  resolved by a confident tier and not clarification-confirmed stays
  `unresolved`. (`adjudicate` / `main_chat_adjudicator` and the
  `llm_adjudicator` parameter are retired; update the resolver tests that pass a
  stub adjudicator.)

Net effect on the abstention metric: genuinely-correct ideas the student can
confirm move `unresolved → resolved`, lowering `unresolved_rate` **at its
source**; ideas the student cannot confirm remain honestly unresolved. The gate
becomes *valid*, not merely numerically lower.

## 10. Data model — `apollo_clarifications`

New Postgres/Supabase table (alongside the existing `apollo_misconceptions` and
`apollo_graph_comparison_runs` tables). One row per probed idea.

| Column | Type | Notes |
|---|---|---|
| `id` | bigint PK | |
| `attempt_id` | bigint FK → problem attempt | scope of the clarification |
| `session_id` | bigint FK → apollo session | |
| `student_id` | uuid / bigint | for profile rollup |
| `concept_id` | bigint | for profile rollup |
| `node_id` | text | the student KG node probed |
| `candidate_key` | text | the top candidate (the idea being disambiguated) |
| `state` | enum | `asked_waiting` / `confirmed` / `refuted` / `vague` |
| `probe_question` | text | the answer-blind question Apollo asked |
| `original_statement` | text | the student's ambiguous utterance text |
| `clarification_text` | text \| null | the student's answer (null while waiting) |
| `asked_turn` | int | turn index of the question |
| `answered_turn` | int \| null | turn index of the answer |
| `created_at` / `updated_at` | timestamptz | |

State machine: `asked_waiting → {confirmed | refuted | vague}` (terminal). One
follow-up per idea, so no re-entry from a terminal state within an attempt.

DB delivery follows the workspace contract: a **numbered migration** (next free
number; current head is ~024 — confirm at planning time), **rehearsed on the
test Supabase project**, never applied to any remote project by an agent.
Access-control: RLS consistent with the sibling Apollo tables (a student reads
only their own rows; service-role writes).

## 11. What this cancels / replaces

- **Silent LLM adjudication** (`apollo/resolution/adjudication.py`) — removed
  from the resolution path (§9).
- **Phase 1b teacher-authored exact aliases** — cancelled. Its purpose
  (bridging "student said it differently") is served better by embeddings +
  student confirmation, with no teacher burden. The `Candidate.exact_aliases`
  field and `match_alias_all`'s exact-alias reads may remain (they are harmless
  and already merged), but no teacher-authoring workflow is built and the
  cancelled plan is archived.

## 12. Failure handling (fail safe — never block teaching)

- **Embedding call fails** → skip detection for the turn; Apollo replies
  normally. No probe, no crash.
- **Re-scorer call fails** → leave the idea `asked_waiting`/unresolved; never
  credit on failure. (May retry once; default outcome is no-credit.)
- **Leakage judge** → unchanged soft-fail-open.
- **Candidate set unavailable** (e.g. no reference) → detection is a no-op; the
  turn proceeds as today.

## 13. Testing

- **Unit (95% patch coverage, per workspace contract):** detector banding
  (confident / ambiguous / leave), probe-hint construction (asserts no
  answer/value leaks into the hint), re-scorer three-way outcomes, pacing limits
  (≤3/turn, 1/idea), resolver `clarification`-method resolution at 0.90 and the
  abstention-floor interaction, removal of the adjudication path.
- **Determinism:** embedder, re-scorer, and leak judge injected as stubs; no
  live model calls in CI.
- **DB:** enumerate every RLS policy × role, the state-machine transitions, and
  each constraint, tested on a **local Docker Postgres** (Testcontainers /
  `supabase db reset`). No remote apply by agents.
- **Regression:** the existing resolver/grading suites must stay green after the
  adjudication removal (update the few tests that inject `llm_adjudicator`).
- **End-to-end validation:** re-run `scripts/apollo_grade_probe.py` on the macro
  corpus to confirm `unresolved_rate` and abstention fall for confirmable
  attempts and stay appropriately high for non-confirmable ones.

## 14. Drift contract

On landing, reconcile `docs/architecture/apollo.md` (owner of `apollo/`) in the
same change: document the live clarification loop, the `clarification`
resolution method and its 0.90 cap, the removal of silent adjudication, the new
`apollo_clarifications` table, and bump `last_verified`. Register the new table
in `docs/architecture/domain-data.md` if it owns the DB schema globs.

## 15. Calibration parameters (defined defaults, tunable — not placeholders)

| Parameter | Default | Owner |
|---|---|---|
| `T_ambig` (detector cosine band) | 0.50 | grader calibration (§10 master spec); validate on probe corpus at planning |
| follow-ups per turn | 3 | this spec (Q5.4) |
| follow-ups per idea | 1 | this spec (Q5.4) |
| `clarification` confidence cap | 0.90 | this spec (Q5.3) |
| prioritization signal | rubric weight, then cosine | this spec (Q5.4) |
