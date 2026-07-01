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

---

## Appendix — artifacts

- Probe script: `scripts/nli_grading_probe.py`
- Machine-readable per-node results: `resolution_results.json` (this directory)
- Draft tables: `resolution_draft.md` (this directory)
