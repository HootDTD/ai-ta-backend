# Apollo NLI Resolver Tier — Live-Model Grading Probe

**Date:** 2026-06-30
**Branch:** `feat/apollo-nli-resolver-tier`
**Author:** live E2E probe (real `cross-encoder/nli-deberta-v3-small`, local Docker stack)

---

## TL;DR

The NLI resolver tier is **correct and safe**, and when the reference data is
well-formed it does exactly its job — recovering genuine student paraphrases
that the lexical tiers miss, at high confidence, with **zero false credits**.
Its measured impact on the *current problem corpus* is **capped by a
data-authoring gap**, not by the tier: most conceptual reference nodes
(`procedure_step` / `simplification` / some `definition`) ship with **no
`content.label`**, so their surface text falls back to the canonical key string
(e.g. `"proc.plan_apply_continuity"`). The resolver's content-overlap floor
correctly refuses to run NLI against a key string, so those nodes can never be
recovered — by NLI *or* the lexical tiers.

**Proven by a controlled experiment:** adding one real label to a currently
key-only node flips the *same* student paraphrase from `unresolved` →
`resolved (method=nli, conf 0.88)`. The blocker is the missing label, full stop.

| Problem | NLI-eligible refs | unresolved-rate OFF → ON | recovered by NLI | misconception veto | **false credits** |
|---|---|---|---|---|---|
| bernoulli_01 | 5 | 0.75 → **0.50** | 1 (`cond.incompressibility`) | did not fire | **0** |
| econ_a (gdp_components_01) | 3 | 0.75 → **0.50** | 1 (`cond.final_goods_only`) | **fired** (`misc.includes_transfers`) | **0** |
| econ_b (nominal_vs_real_02) | 3 | 0.75 → 0.75 | 0 (all refs key-only) | did not fire | **0** |

> **Update (Run 2, §10):** filling `content.label` on all 9 key-only nodes confirms
> the label is *necessary but not sufficient*. It unblocks the floor (the model is now
> called on every node), but recovery then depends on the adjudicator: the small
> `nli-deberta-v3-small` cleanly recovers **condition** paraphrases and declines
> **procedure/definition** ones (contradiction/neutral). The "one label flips it to
> resolved @nli" clincher below holds for condition-type nodes; the bottleneck for the
> rest has shifted from **data** to **model capacity + the polarity screen**.

---

## 1. Environment

| Component | Detail |
|---|---|
| Stack | **Local Docker** (not the test Supabase): `pgvector/pgvector:pg16`, `neo4j:5.25`, both via Testcontainers |
| Docker | Desktop daemon v29.5.3 |
| NLI model | `cross-encoder/nli-deberta-v3-small` (as `nli_config.NLI_MODEL_NAME`), CPU |
| torch | **2.6.0+cpu** — the initially-pinned `2.12.1+cpu` is a bad Windows wheel (`c10.dll` `WinError 1114` init failure); dropped to the stable 2.6.0. Deps pinned in `requirements.txt`. |
| transformers | 4.57.6 |
| Flag | `APOLLO_NLI_ENABLED` — probe runs the resolver with an explicit `NLIContext` (flag independent) |

`resolve_attempt` is a pure, in-memory function, so the resolution comparison
needs no DB/Docker. The containers are used to validate the full persistence
chain (§6).

## 2. Real-model behavior (smoke test)

The model classifies domain statements correctly out of the box:

| Student premise | Reference hypothesis | Verdict |
|---|---|---|
| "the fluid speeds up where the pipe narrows" | "velocity increases as cross-sectional area decreases" | **entailment 0.941** ✓ (genuine paraphrase — lexical tiers would miss it) |
| "pressure rises when the fluid moves faster" | "pressure decreases as velocity increases" | **contradiction 0.999** ✓ (inverse-relationship misconception) |
| "assume the flow does not change over time" | "the flow is steady" | neutral 0.666 — the small model misses this domain paraphrase |
| "the pipe is painted blue" | "the flow is incompressible" | contradiction 0.898 — over-contradicts unrelated text (harmless: the resolver's content-overlap floor + shortlist never pair unrelated text) |

Takeaway: the model nails clear paraphrases and inverse-relationship
contradictions; it is weaker on indirect domain paraphrases (expected for a
`-small` NLI model).

## 3. Resolution comparison — real problems, NLI OFF vs ON

Method: build the closed candidate set from each problem's `reference_solution`
+ that concept's `misconceptions.json`; construct a student teach-back graph
with (a) genuine paraphrases of the conceptual reference nodes, (b) a
near-verbatim control, (c) a misconception paraphrase; run `resolve_attempt`
with `nli_ctx=None` (OFF) and with a real `NLIContext` (ON).

### 3.1 The two clean recoveries (labeled reference nodes)

| Node | Student surface (paraphrase) | token_set_ratio vs ref | OFF | ON | NLI entailment |
|---|---|---|---|---|---|
| `cond.incompressibility` (bernoulli) | "the incompressibility assumption means the fluid preserves constant density as it moves through the pipe" | 0.53 (lexical miss) | unresolved | **resolved `nli` @0.88** | **0.974** |
| `cond.final_goods_only` (econ_a) | "only final goods and services are tallied; intermediate inputs, second-hand sales, and transfer payments are omitted" | 0.88 (lexical miss) | unresolved | **resolved `nli` @0.88** | **0.992** |

Both reference nodes have a real `content.label` ("Incompressibility
assumption", "Final goods and services only"). The lexical tiers missed the
paraphrase (`token_set_ratio` < 0.9); NLI recovered it at ≥0.97 entailment.

### 3.2 Controls behave (NLI does not disturb resolved nodes)

- bernoulli "density is constant" → `exact` @1.0 both OFF and ON.
- econ_a "only final goods and services produced this year are counted" → `exact` @1.0 both.
- econ_b "nominal gdp is the same as real gdp" → `alias` @0.92 both (matched the misconception trigger phrase).

Every already-resolved node is **byte-identical** OFF vs ON — consistent with
the recall-only design (NLI runs only after the lexical tier finds nothing).

## 4. The data blocker — key-only display names (verified)

8 of the 11 NLI-eligible reference nodes across the three problems have **no
`content.label`**, so `candidates_from_reference_solution` falls back to the
canonical key as `display_name`:

| Problem | conceptual node | has `content.label`? | NLI reachable? |
|---|---|---|---|
| bernoulli_01 | `cond.incompressibility` | ✅ "Incompressibility assumption" | yes → recovered |
| bernoulli_01 | `simp.horizontal_simplification` | ❌ key-only | no (floor blocks) |
| bernoulli_01 | `proc.plan_apply_continuity` / `..._horizontal_simplification` / `..._solve_bernoulli_for_p2` | ❌ key-only | no |
| econ_a | `cond.final_goods_only` | ✅ "Final goods and services only" | yes → recovered |
| econ_a | `proc.compute_net_exports` / `proc.sum_components` | ❌ key-only | no |
| econ_b | `def.real_basis` / `proc.compute_real_change` / `proc.apply_percent_change` | ❌ key-only | no |

For a key-only node the resolver's positive content-overlap floor
(`_content_tokens(student) ∩ _content_tokens("proc.compute_real_change") = ∅`)
fires, so **NLI is never even called**. econ_b has *zero* labeled conceptual
nodes, which is exactly why its unresolved-rate is unchanged (0.75 → 0.75).

**This is a production condition, not a probe artifact — and not a code bug.**
Verified: the production grading path builds reference candidates via
`load_problem_candidates_with_soundness` → `build_problem_candidates` →
`candidates_from_reference_solution` — the SAME function the probe used. The DB
lookup (`load_entity_specs`) in that path supplies only the integer canon-key
for edge projection, NOT display names/aliases. So production reads reference
surface text from the problem JSON's `content.label` (fallback = canonical key),
byte-identical to the probe. Every component behaves correctly by design
(`content.get("label") or canonical_key` fallback; the floor refusing to run NLI
on a key string; the lexical tiers not matching a key string). The gap is a
**data-authoring** one: the conceptual `reference_solution` steps were authored
without a `content.label`.

**Consequence for the CURRENT grader (NLI-off):** the probe's OFF column shows
every key-only conceptual node is already `unresolved` under the lexical-only
resolver — i.e. a student who correctly teaches back one of these steps in any
phrasing cannot be credited for it TODAY. The missing labels cap grading
coverage on conceptual steps now; NLI simply can't rescue them either.

Note this hurts the **lexical** tiers too — no student phrasing can
alias/fuzzy-match a key string. And feeding the key string to NLI directly is
actively misleading: the model reads `"def.real_basis"` as a claim and returns
**contradiction 0.939** against a correct student definition of real GDP.

### 4.1 Controlled proof — a label unblocks NLI

Same student paraphrase ("use conservation of mass to link how fast the water
is going at the two openings"), the only variable is the reference label on
`proc.plan_apply_continuity`:

| Reference display_name | NLI OFF | NLI ON |
|---|---|---|
| `proc.plan_apply_continuity` (current, key-only) | unresolved | **unresolved** |
| "Apply the continuity equation to relate the inlet and outlet flow speeds" (real label added) | unresolved | **resolved `nli` @0.88** |

The blocker is the missing label, not the tier.

## 5. Misconception veto — real but model-limited

| Problem | misconception | student paraphrase | NLI entailment vs misconception | veto |
|---|---|---|---|---|
| econ_a | `misc.includes_transfers` | "welfare disbursements and resale transactions ought to be added to the GDP tally…" | **0.958** | ✅ **fired** — student correctly denied credit |
| bernoulli | `misc.pressure_velocity_same_direction` | "fluid pressure climbs higher as the flow velocity increases" | 0.140 (neutral) | did not fire |
| econ_b | `misc.nominal_for_real` | "current dollar output tells us as much as the inflation-corrected figure…" | 0.025 (neutral) | did not fire |

The veto works when the small model detects the entailment (econ_a). It missed
two indirect paraphrases — but critically, **in every case the misconception
paraphrase stayed `unresolved` (no false credit)**: a missed veto means "not
flagged as a misconception," never "wrongly credited to a reference."

## 6. Safety results

- **False credits: 0 / 11** — no student node resolved to the wrong reference under NLI ON.
- **Byte-identical when off / on for already-resolved nodes** — every `exact`/`alias` control is unchanged; NLI only ever converts an `unresolved` node.
- **Recall-only invariant held** — NLI fired only where the lexical tier returned nothing.
- **Whole grading path on the local stack: ✅** With the Docker daemon up, the
  previously-skipped integration tests execute — the full
  resolution→canonicalize→grade→audit→persist chain runs against real
  `pgvector:pg16` + `neo4j:5.25` containers: **`1911 passed, 14 skipped, 0
  failed` in 98s** (the 14 skips are legacy-V2 / sympy-version-gated, not
  Docker-gated). This run is NLI-off (the suite's autouse `_force_nli_off`
  guard) — it establishes the whole-path baseline is green on the real stack;
  NLI-on behavior is measured at the resolver in §3–5.

## 7. Conclusions

1. **The tier is production-safe and works as designed.** Where the reference
   node carries real text, NLI recovers genuine paraphrases at 0.97–0.99
   entailment, caps confidence at 0.88, and never mis-credits. The recall-only
   placement and the veto-first ordering both behave.
2. **The realized win on the current corpus is small (2 of 11 nodes) — and the
   cause is data, not code.** Most conceptual reference steps lack a
   `content.label`; the resolver correctly refuses to match a canonical-key
   string, so both the lexical tiers *and* NLI are blocked on them.
3. **The `-small` NLI model is the second limiter.** It reliably catches clear
   paraphrases and inverse-relationship contradictions but misses indirect
   domain paraphrases; a larger NLI checkpoint would raise both recall and
   veto rates.

## 8. Recommendations (before flipping default-ON)

1. **Author `content.label` on every conceptual reference step** (`procedure_step`,
   `simplification`, `definition`, `condition`) across the problem corpus. This
   is the single highest-leverage fix — it unblocks the lexical tiers *and* NLI.
   (The equation tiers are unaffected; they key off `symbolic`.)
2. Re-run this probe after (1) to measure the true recovery rate on
   well-formed data.
3. Consider a larger NLI checkpoint (or a domain-tuned one) if veto recall on
   indirect misconception paraphrases matters.
4. The ≥0.95-precision calibration gate (Task 11/12) still governs the
   default-ON flip; the dev set should be expanded with real corpus pairs once
   labels exist (see the branch's calibration caveats).

## 9. Interactive clarification session (Apollo asks / student answers)

Driven on the REAL OpenAI embedder + REAL NLI model, using the same primitives
`run_clarification_detection` uses (`find_residual_nodes` →
`detect_ambiguous_nodes` → `build_probe_hint`), on a Bernoulli teach-back.

Student teach-back (two conditions, stated in the student's own words):
- `stu_paraphrase`: "the liquid keeps the same density all the way through the pipe"
- `stu_vague`: "the fluid doesn't really change as it flows"

**Apollo's turn (NLI ON):** `stu_paraphrase` stays residual — it shares no
content token with the reference label "Incompressibility assumption" (the
student describes the *meaning*; the label is the *term*), so the composer's
content-overlap floor declines to run NLI (the precision guard). Apollo probes
it with an **answer-blind** steering hint (per spec §6.4 it never reveals the
answer):
> "Make the student commit to the DIRECTION of the relationship they just described (which way it goes), without telling them which is correct."

**Student's turn (I answer):** I commit the idea to the incompressibility
condition; the answer is applied as a `confirmed_resolution`:
- `stu_paraphrase` → resolved, method=**clarification**, conf **0.90**, key `cond.incompressibility`
- `stu_vague` → resolved, method=**clarification**, conf **0.90**, key `cond.incompressibility`

**Takeaway — NLI and clarification are complementary, not redundant:**
- NLI auto-resolves paraphrases that reuse ≥1 of a *labeled* reference's words
  (grading probe: `cond.incompressibility` @ entailment 0.974), removing a
  clarification turn.
- A **zero-token-overlap** paraphrase is deliberately *not* auto-resolved — the
  content floor requires a shared token for precision — so it falls through to
  clarification, where the student's answer resolves it @ 0.90. (This is a real
  precision/recall knob in the composer, not a bug.)
- **Key-only** reference nodes (§4) can't be reached by either tier's surface
  match, so they rely entirely on clarification.

Driver: `scripts/nli_clarification_session.py` (no args = Apollo's probes;
`--answer node_id=candidate_key` = the student's answer).

---

## 10. Run 2 — `content.label` filled (data gap closed)

**Change (data only):** authored a faithful `content.label` on **all 9**
previously key-only NLI-eligible reference steps across the three probe
problems, then re-ran `scripts/nli_grading_probe.py` unchanged. The nodes
labeled:

- **bernoulli_01:** `simp.horizontal_simplification`, `proc.plan_apply_continuity`,
  `proc.plan_apply_horizontal_simplification`, `proc.plan_solve_bernoulli_for_p2`
- **gdp_components_01:** `proc.compute_net_exports`, `proc.sum_components`
- **nominal_vs_real_02:** `def.real_basis`, `proc.compute_real_change`,
  `proc.apply_percent_change`

**Why this is safe (verified):** no lexical tier reads `display_name` — `match_exact`
compares the student `label` vs `canonical_key`; alias/fuzzy read `aliases`/`exact_aliases`;
symbolic reads `symbolic`. `display_name` (= `content.label`) is consumed **only** by
`candidate_surface_texts()` → the NLI semantic shortlist + hypothesis (and the
clarification embedder). The reference graph is byte-identical too: `to_kg_graph`
strips all but `action`/`purpose` for procedure_steps, and `SimplificationContent`/
`DefinitionContent` silently ignore the extra `label` (no `extra="forbid"`). Guardrail:
`test_problem_inputs`, `test_reference_canonical`, `test_scores`, `test_derived_equation_*`,
`test_candidates`, `test_corpus_e2e`, `test_problem_validator` — **97 passed**, NLI-off
resolution unchanged.

**Result — the summary table is byte-identical to Run 1:**

| Problem | NLI-elig | UR OFF → ON | recovered | veto | false credits |
|---|---|---|---|---|---|
| bernoulli_01 | 5 | 0.75 → 0.50 | 1 (`cond.incompressibility`) | — | 0 |
| gdp_components_01 | 3 | 0.75 → 0.50 | 1 (`cond.final_goods_only`) | fired (`misc.includes_transfers` 0.958) | 0 |
| nominal_vs_real_02 | 3 | 0.75 → 0.75 | 0 | — | 0 |

**But the mechanism moved — this is the real finding.** Pre-label, the 4 newly
labeled probe nodes were **blocked before the model** ("CONTENT-TOKEN FLOOR FAILS —
NLI tier structurally cannot certify"). Post-label the floor passes and the model
**is actually called** on every one. So `content.label` did exactly its job — it moved
these nodes from *structurally unmatchable* to *model-adjudicated*. Recovery is now
gated by two things **downstream of the data**, and the small model declines them:

| Node | shared tokens / floor | polarity | NLI verdict | outcome |
|---|---|---|---|---|
| `b_nli_simp` (simplification) | passes | **negation_mismatch** (student "*no* elevation change" vs affirmative label) | ent **0.9505** / con 0.0008 — *would pass* | **abstain at polarity guard** (before the model score counts) |
| `ea_nli_proc` (procedure) | passes ("trade balance") | ok | **contradiction** con 0.627 | declined by model |
| `eb_nli_def` (definition) | passes ("real GDP") | ok | **contradiction** con 0.954 (label's "not … prices" negation trips the small model) | declined by model |
| `eb_nli_proc` (procedure) | passes (later/earlier/absolute/change) | ok | **neutral** ent 0.012 | declined by model |

The two clean recoveries (`cond.incompressibility` 0.974, `cond.final_goods_only` 0.992)
are the **condition** nodes that already had labels — the model is excellent on
condition paraphrases and weak/erratic on procedure/definition ones.

**Conclusion — the label gap is necessary but not sufficient.** Filling it is the
correct, required first step (nodes now reach adjudication; still **zero false
credits**, veto still fires). The bottleneck has now **shifted from data to model
capacity + the polarity screen**:

1. `cross-encoder/nli-deberta-v3-small` mislabels equivalent procedure/definition
   paraphrases as contradiction/neutral. A stronger adjudicator (nli-deberta-v3-**large**,
   or an LLM entailment call) is the highest-leverage next test.
2. Author labels **without internal negations** — the "not … prices" clause in
   `def.real_basis`'s label drove con to 0.95; an affirmative rephrasing should score better.
3. The polarity screen's `negation_mismatch` is over-conservative for litotes
   ("no elevation change" ≡ "constant height") — a candidate refinement, orthogonal to labels.

---

## 11. Run 3 — isolating the three levers (model / negation / polarity)

Run 2 shifted the bottleneck from data (labels) to **model capacity + the
polarity screen**. Run 3 isolates each lever with a controlled A/B, running the
same probe under both `nli-deberta-v3-small` and `nli-deberta-v3-large` (the
large model downloads/loads in ~30 s on CPU; the probe now selects the model via
`NLI_PROBE_MODEL`).

### 11.1 Direct model comparison (the pairs the small model declined)

| pair | small model | large model |
|---|---|---|
| `b_nli_cond` (condition) | entail 0.974 ✓ | entail 0.974 ✓ |
| `b_nli_simp` (simplification) | entail 0.951 (polarity-blocked) | entail **0.9953** (polarity-blocked) |
| `ea_nli_cond` (condition) | entail 0.992 ✓ | entail **0.723** ✗ (below 0.87) |
| `ea_nli_proc` (procedure) | **contradiction 0.627** | neutral 0.970 |
| `eb_nli_def` (definition) | **contradiction 0.954** | entail **0.986** |
| `eb_nli_proc` (procedure) | neutral 0.012 | neutral 0.999 |

The large model is **not a drop-in win**: it fixes the worst small-model error
(`eb_nli_def`: 0.95 contradiction → 0.99 entailment) but *under-scores*
`ea_nli_cond` (0.72 < the 0.87 threshold), losing a recovery the small model
made. The 0.87 threshold is small-model-calibrated; a large-model deployment
needs threshold re-tuning.

### 11.2 Lever 2 — negation in the label (polarity `negation_mismatch`)

`eb_nli_def` stayed `unresolved` under the large model *despite* 0.986
entailment. Root cause (traced, not the veto): the authored label contained
"…**not** a change in prices", and the polarity pre-screen fires
`negation_mismatch` when the label carries a negation the student's phrasing
does not — **before the model score is consulted**. Rewriting the label
affirmatively ("…reflects a change in the quantity of output produced") clears
the screen; the large model then resolves `eb_nli_def` → `nli` @ 0.88 (small
model still mis-calls it contradiction — a genuine capacity gap, negation or
not). **Authoring rule: reference labels must be affirmative — no internal
negations.**

### 11.3 Lever 3 — polarity litotes window (`apollo/resolution/polarity.py`)

`b_nli_simp` entailed strongly under both models (0.95 / 0.9953) yet abstained:
the student said "**no elevation change**" (litotes ≡ "constant height") and the
polarity guard's litotes exception only matched `no`/`not` *immediately* followed
by a null-change word ("no change"), missing the intervening noun. Fix: scan a
**bounded window** (`_LITOTES_WINDOW = 3`) after `no`/`not` for a null-change
word, so "no elevation change" / "no significant difference" pass to NLI while a
distant negation ("does not increase … pressure change") still fires. TDD: 3 new
tests (2 litotes-allow, 1 bounded-window-still-blocks); full `test_polarity.py`
green (9/9). With the fix, `b_nli_simp` recovers under **both** models.

### 11.4 Final lever-isolated scorecard (probe-exercised nodes)

| config | bernoulli | econ_a | econ_b | recovered | **false credits** |
|---|---|---|---|---|---|
| small — labels only (Run 2) | 1 | 1 | 0 | 2 | **0** |
| large — labels only | 1 | 0 | 0 | 1 | **0** |
| large — + affirmative label | 1 | 0 | 1 | 2 | **0** |
| **small — + label + polarity** | **2** | 1 | 0 | **3** | **0** |
| **large — + label + polarity** | **2** | 0 | 1 | **3** | **0** |

Both final configs recover 3, on **different nodes**: both get
`b_nli_cond`+`b_nli_simp`; small adds `ea_nli_cond` (large under-scores it),
large adds `eb_nli_def` (small mis-calls it). `ea_nli_proc` / `eb_nli_proc` stay
neutral under both — genuinely vague paraphrases that correctly fall through to
**clarification** rather than auto-resolve. **Zero false credits in every
configuration; the misconception veto keeps firing** (`ea_misc` 0.958 small /
0.997 large).

### 11.5 Corpus propagation

`content.label` was backfilled on **all remaining NLI-eligible steps** so the
whole corpus is well-formed, not just the three probe problems: **14 affirmative
labels across 7 files** (bernoulli 02–05, gdp_components 02–03, nominal_vs_real
01). Verification: every problem passes `load_problem` schema validation; **0
NLI-eligible steps remain label-less**; full apollo suite **1915 passed / 0
failed** (one seed-convert test updated — a labeled proc step now surfaces its
label instead of the humanized-id fallback, which is the intended, better
behavior; the `_humanize` fallback keeps dedicated coverage via a synthetic
label-less step).

### 11.6 Recommendations (updated)

1. **Author all reference labels affirmatively** — no internal negations (§11.2).
   Applied across the corpus here.
2. **A model swap requires re-tuning `min_entailment`** — large under-scores
   `ea_nli_cond` at 0.72 (§11.1). Do not swap the model without re-calibrating
   the threshold on a labeled validation set.
3. **The vague-paraphrase tail belongs to clarification, not NLI** — both models
   correctly abstain on `ea_nli_proc`/`eb_nli_proc`; the clarification loop is the
   right recovery path there (§9).

### 11.7 Full per-node evidence (final config, both models)

Final config = **labels backfilled + affirmative label + polarity-litotes fix**.
`OFF` = deterministic lexical tiers only (identical for both models). `ON` shows
the composer's method→key and the raw NLI entailment/contradiction. Every node's
`OFF` is unresolved except the controls; **no config produced a false credit**.

| problem | node | type | OFF | small — ON (ent / con) | large — ON (ent / con) | takeaway |
|---|---|---|---|---|---|---|
| bernoulli_01 | `b_nli_cond` | condition | unresolved | **nli** → cond.incompressibility (0.974 / 0.003) | **nli** → cond.incompressibility (0.974 / 0.001) | both recover — models agree |
| bernoulli_01 | `b_nli_simp` | simplification | unresolved | **nli** → simp.horizontal_simplification (0.951 / 0.001) | **nli** → simp.horizontal_simplification (0.995 / 0.000) | both recover **only after the polarity-litotes fix** |
| bernoulli_01 | `b_ctrl` | condition | exact | exact → cond.incompressibility | exact → cond.incompressibility | control (lexical, model-independent) |
| bernoulli_01 | `b_misc` | definition | unresolved | unresolved (0.140 / 0.002, neutral) | unresolved (0.955 / 0.000, **entailment → veto fires**) | never credited; large model *also* catches the misconception |
| econ_a_01 | `ea_nli_cond` | condition | unresolved | **nli** → cond.final_goods_only (0.992 / 0.001) | **unresolved** (0.723 / 0.001) | **large under-scores below the 0.87 threshold** → lost recovery |
| econ_a_01 | `ea_nli_proc` | procedure | unresolved | unresolved (0.031 / 0.627, contradiction) | unresolved (0.001 / 0.029, neutral) | both decline — heavily-reworded paraphrase → clarification |
| econ_a_01 | `ea_ctrl` | condition | exact | exact → cond.final_goods_only | exact → cond.final_goods_only | control |
| econ_a_01 | `ea_misc` | definition | unresolved | unresolved (0.958, **VETO**) | unresolved (0.997, **VETO**) | misconception veto fires under both |
| econ_b_02 | `eb_nli_def` | definition | unresolved | unresolved (0.009 / 0.987, contradiction) | **nli** → def.real_basis (0.989 / 0.001) | **large recovers; small mis-calls it a contradiction** |
| econ_b_02 | `eb_nli_proc` | procedure | unresolved | unresolved (0.012 / 0.004, neutral) | unresolved (0.001 / 0.000, neutral) | both decline — vague paraphrase → clarification |
| econ_b_02 | `eb_ctrl` | definition | alias | alias → misc.nominal_for_real | alias → misc.nominal_for_real | control (lexical alias) |
| econ_b_02 | `eb_misc` | definition | unresolved | unresolved (0.025, neutral) | unresolved (0.013, neutral) | never credited |

Two model-capacity signals stand out beyond the summary counts: (a) on **recall**
the large model is sharper on definitions (`eb_nli_def` 0.99 vs the small model's
0.99 *contradiction*) but blunter on one condition (`ea_nli_cond` 0.72 vs 0.99);
(b) on the **veto** side the large model is more sensitive — it entails the
pressure-velocity misconception (`b_misc`) at 0.955 and fires the veto, where the
small model missed it at 0.140. Both directions reinforce §11.1: the large model
is *differently calibrated*, not strictly better, so `min_entailment` (and
plausibly `misconception_veto_entailment`) must be re-tuned per model.

### 11.8 Authored-label inventory (23 labels across 10 files)

Every one is affirmative (no internal negation) and faithful to the step's
`action`/`applies_when`/`concept`+`meaning`. The four probed nodes are marked ★
(they drive the §11.4 scorecard); the rest complete the corpus for production.

**fluid_mechanics / bernoulli_principle**
- `problem_01` · `simp.horizontal_simplification` ★ — "Horizontal pipe: both sections are at the same height, so the gravitational potential-energy terms cancel"
- `problem_01` · `proc.plan_apply_continuity` — "Apply the continuity equation to solve for the outlet velocity v2"
- `problem_01` · `proc.plan_apply_horizontal_simplification` — "Use the horizontal-pipe assumption to cancel the gravity terms in Bernoulli's equation"
- `problem_01` · `proc.plan_solve_bernoulli_for_p2` — "Substitute the known values into the simplified Bernoulli equation and solve for the section-2 pressure P2"
- `problem_02` · `simp.equal_pressure_simplification` — "Both ends are open to the atmosphere so the pressures are equal, and the pressure terms cancel in Bernoulli's equation"
- `problem_02` · `proc.plan_apply_equal_pressure_simplification` — "Recognize that both ends are at atmospheric pressure, so the pressure terms cancel in Bernoulli's equation"
- `problem_02` · `proc.plan_set_v1_zero_and_solve_bernoulli` — "Set the reservoir inlet velocity to zero and solve the simplified Bernoulli equation for the outlet velocity v2"
- `problem_03` · `proc.plan_invoke_incompressibility` — "Invoke the incompressibility assumption so the continuity equation reduces to A1 times v1 equals A2 times v2"
- `problem_03` · `proc.plan_solve_continuity_for_v2` — "Substitute the known areas and inlet velocity into the continuity equation and solve for the outlet velocity v2"
- `problem_04` · `proc.plan_apply_flow_rate_definition` — "Evaluate the volumetric flow rate Q = A times v by substituting the given area and velocity"
- `problem_05` · `proc.plan_apply_continuity_for_v2` — "Apply the continuity equation with the known areas and inlet velocity to solve for the outlet velocity v2"
- `problem_05` · `proc.plan_substitute_into_bernoulli` — "Substitute the known quantities into Bernoulli's equation and solve for the pressure P2 at the narrow section"

**macroeconomics / gdp_components**
- `problem_01` · `proc.compute_net_exports` ★ — "Compute net exports (the trade balance) by subtracting imports from exports"
- `problem_01` · `proc.sum_components` — "Sum the four expenditure components — consumption, investment, government purchases, and net exports — to obtain GDP"
- `problem_02` · `proc.subtract_imports` — "Compute net exports (the trade balance) by subtracting imports from exports, then read off the sign"
- `problem_03` · `def.depreciation` — "Depreciation is the value of capital used up during the year; subtracting it from a gross measure yields the corresponding net measure"
- `problem_03` · `proc.compute_gnp` — "Add net income earned from abroad to GDP to obtain gross national product (GNP)"
- `problem_03` · `proc.subtract_depreciation` — "Subtract depreciation from GNP to obtain net national product (NNP)"

**macroeconomics / nominal_vs_real_gdp**
- `problem_01` · `simp.deflator_is_price_index` — "Treat the quoted price index as the GDP deflator so the deflator definition can be solved for real GDP"
- `problem_01` · `proc.rearrange_for_real_gdp` — "Rearrange the GDP deflator definition to real GDP = nominal GDP divided by (PI/100) and substitute the given values"
- `problem_02` · `def.real_basis` ★ — "Real GDP is inflation-adjusted, so a change in it reflects a change in the quantity of output produced" (affirmative rewrite of the original "…not a change in prices" — §11.2)
- `problem_02` · `proc.compute_real_change` ★ — "Compute the absolute change in real GDP by subtracting the earlier value from the later value"
- `problem_02` · `proc.apply_percent_change` — "Convert the real change to a percentage by dividing by the earlier value and multiplying by 100"

**Code touched this run:** `apollo/resolution/polarity.py` (bounded litotes
window), `scripts/nli_grading_probe.py` (model selection via `NLI_PROBE_MODEL`,
model-tagged outputs), `apollo/resolution/tests/test_polarity.py` (+4 tests: two
litotes-allow, one bounded-window-still-blocks, one non-`no`/`not` negation),
`apollo/persistence/tests/test_learner_model_seed_convert.py` (label vs humanize
split), `docs/architecture/apollo.md` (owner-doc reconcile + `last_verified`),
and `content.label` on all NLI-eligible steps across the 10 problem files.
`requirements-nli.txt` documents the optional large-model install. Full apollo
suite **1915 passed / 0 failed**; `polarity.py` patch coverage 100%.

---

## Appendix — artifacts

- Probe script: `scripts/nli_grading_probe.py` (select the model via
  `NLI_PROBE_MODEL`; default is the production `cross-encoder/nli-deberta-v3-small`)
- Per-model machine-readable per-node results (this directory):
  `resolution_results_nli-deberta-v3-small.json`,
  `resolution_results_nli-deberta-v3-large.json`
- Per-model draft tables (this directory):
  `resolution_draft_nli-deberta-v3-small.md`,
  `resolution_draft_nli-deberta-v3-large.md`
- The final small/large runs are the label-backfilled + affirmative-label +
  polarity-litotes configuration (§11.4).
