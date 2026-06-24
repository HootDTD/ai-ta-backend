# Design: Macro Ch.6 graph-grading probe

Approved 2026-06-22. This is the spec the build works against.

## Architecture (data flow)

```
ch6.pdf ──[scripts/index_local_pdf.py]──▶ local pgvector
                 (aita_documents + aita_chunks, status={'state':'ready'},
                  material_kind='textbook', week=None, search_space_id=<macro course>)
                                          │
   RAG relevance test #1 (mining) ───────┤  apollo/provisioning/scrape.py::scrape_questions
                                          │  over §6.1–6.2 chunks → candidate questions
                                          │  (does hybrid-search surface the GDP/NNP/real-GDP Qs?)
                                          ▼
   hand-authored reference KGs (R_norm) ──┤  apollo/subjects/macroeconomics/** → seed×3 → question bank
                                          │
   RAG relevance test #2 (faithfulness) ──┤  apollo/provisioning/pairing_gate.py::validate_pair
                                          │  (is each authored reference solution entailed by retrieved spans?)
                                          ▼
   3 variations/Q (strong/partial/weak) ──▶ real parser (/chat) → student KG (S_norm)
                                          ▼
                  apollo/graph_compare grade_attempt(S_norm, R_norm)
                                          ▼
              apollo_graph_comparison_runs (10 scores + abstention) ──▶ RESULTS.md
```

## Subjects & concepts

`apollo/subjects/macroeconomics/concepts/`:

### Concept A — `gdp_components` (§6.1 "Measuring GDP")
Canonical symbols: `GDP, C, INV, G, X, M, NX, GNP, NNP, DEP, RIN, ROUT`.

> **`INV` not `I` for investment** — `I` is SymPy's imaginary unit and parses to a
> non-free symbol (it silently drops investment from the equation). The content
> author verified this and used `INV` everywhere (symbol, given_values key,
> equation, aliases). Downstream (probe scenarios, seed) all use `INV`.

**Q1 `gdp_identity`** (Table 6.4 Work-It-Out) — given `C=400, INV=60, G=120,
X=100, M=120` → `GDP=560`.
- `eq.net_exports`: `NX - (X - M)`
- `eq.gdp_expenditure`: `GDP - (C + INV + G + NX)`
- `cond.final_goods_only` (`applies_when`: only final goods/services count;
  transfers, used & intermediate goods excluded)
- `proc.compute_net_exports` (order 1, uses `eq.net_exports`)
- `proc.sum_components` (order 2, uses `eq.gdp_expenditure`)
- declared path covers all; `SCOPES`: cond→eq.gdp_expenditure.

**Q2 `net_exports_sign`** — given `X=100, M=120` → `NX=-20` (deficit).
- `eq.net_exports`: `NX - (X - M)`
- `cond.trade_deficit` (`applies_when`: imports exceed exports → negative net
  exports / trade deficit) — the **polarity** probe (surplus vs deficit).
- `proc.subtract_imports` (order 1, uses `eq.net_exports`).

**Q3 `nnp_chain`** — given `GDP=560, RIN=10, ROUT=8, DEP=40` → `NNP=522`.
- `eq.gnp`: `GNP - (GDP + RIN - ROUT)`
- `eq.nnp`: `NNP - (GNP - DEP)`
- `def.depreciation` (`concept`: depreciation; `meaning`: capital worn out over
  the year; subtracted from GNP to get NNP)
- `proc.compute_gnp` (order 1, uses `eq.gnp`)
- `proc.subtract_depreciation` (order 2, uses `eq.nnp`; `depends_on` proc.compute_gnp)
- exercises a **multi-equation** `DEPENDS_ON` (eq.nnp depends_on eq.gnp) + 2-step
  `USES`/`PRECEDES` chain.

### Concept B — `nominal_vs_real_gdp` (§6.2–6.3)
Canonical symbols: `nomGDP, realGDP, deflator, PI, growth, g1, g2`.

**Q4 `real_gdp_from_deflator`** — **the case-3 trap.** Given `nomGDP=543.3,
PI=19.0` → `realGDP=2859.5`.
- `eq.gdp_deflator` (the canonical base equation): `deflator - (nomGDP/realGDP)*100`
- `simp.deflator_is_price_index` (`applies_when`: the price index *is* the GDP
  deflator; `substitution: {"PI": "deflator"}`) — the explicit mapping that lets
  the symbolic tier recognise the rearranged form, the direct analog of the
  Bernoulli pressure-cancel `{P2: P1}` fix.
- `proc.rearrange_for_real_gdp` (order 1, uses `eq.gdp_deflator`; action:
  rearrange the deflator definition to `realGDP = nomGDP/(PI/100)` and substitute).
- The **strong** student variation states the *rearranged* form
  `realGDP - nomGDP/(PI/100)`; we observe whether it resolves to `eq.gdp_deflator`.

**Q5 `real_gdp_growth`** — given `g1=2859.5, g2=13598.5` → `growth≈376`.
- `eq.growth_rate`: `growth - ((g2 - g1)/g1)*100`
- `def.real_basis` (`concept`: real GDP; `meaning`: inflation-adjusted, so growth
  measures quantity not price change)
- `proc.apply_percent_change` (order 1, uses `eq.growth_rate`).

> **Equation authoring rule:** every `content.symbolic` is a SymPy-parseable
> **zero-form** (`LHS - (RHS)`); multi-letter macro symbols (`GDP`, `nomGDP`,
> `deflator`) parse as single free Symbols (fine); avoid SymPy-reserved names.

## Misconceptions (`misconceptions.json`, prefix `misc.` is load-bearing)

- A: `misc.includes_transfers` (counts transfer payments / used goods in GDP;
  opposes `cond.final_goods_only`); `misc.gross_for_net` (forgets depreciation /
  conflates GDP-GNP-NNP; opposes `def.depreciation`).
- B: `misc.nominal_for_real` (uses nominal GDP as real / forgets to deflate;
  opposes `def.real_basis`); `misc.deflate_wrong_direction` (multiplies by the
  price index instead of dividing; opposes `eq.gdp_deflator` — caught by polarity).

## Polarity antonyms (append to `competition.py::_DIRECTION_ANTONYMS`)

`(surplus, deficit)`, `(rises, falls)`, `(rise, fall)`, `(appreciate, depreciate)`,
`(appreciates, depreciates)`, `(expansionary, contractionary)`,
`(inflation, deflation)`, `(gross, net)`, `(nominal, real)`, `(multiply, divide)`.

## 3 variations per question

For each of the 5 questions, author `strong` / `partial` / `weak` teaching
transcripts (lists of student `/chat` messages), faithful to the Ch.6 prose:
- **strong** — complete, correct derivation (for Q4: the rearranged real-GDP form).
- **partial** — correct but missing one reference node/edge (e.g. omits the
  condition, or states the equation without the procedure step).
- **weak** — contains the concept's misconception (e.g. counts transfers, uses
  nominal for real, deflates the wrong direction) → should resolve to a `misc.*`
  and drop soundness.

## Build items (each Python change ships with tests — 95% patch gate)

1. **`apollo/subjects/macroeconomics/**`** — subject + 2 concept trees (6
   metadata files + `misconceptions.json` each) + 5 problem JSONs. Every problem
   self-verified with `Problem.model_validate` + `validate_reference_graph` +
   `build_reference_canonical` (pure, no DB).
2. **Generalize `scripts/seed_apollo_learner_model.py`** — `--subject-slug` /
   `--concept-slug` (default: all concepts of the subject), drop the
   `_BERNOULLI_*` hardcoding; the conversion core is already generic.
3. **`scripts/index_local_pdf.py`** (new) — PyMuPDF extract → `AITAConnectorDocument(
   material_kind='textbook', week=None)` → `AITAIndexingService.index_from_items`
   against `SUPABASE_DB_URL` (local). Local-only guard.
4. **Generalize `scripts/apollo_grade_probe.py`** — macro `SCENARIOS` (3
   variations × 5 Qs), parametric concept slug + course resolution, N-mode.
5. **`apollo/resolution/competition.py`** — append macro antonyms.
6. **`scripts/run_macro_probe.py`** (new orchestrator) — env-load → bootstrap
   check → embed → seed×3 → mine → faithfulness → boot :8001 → probe(15) → read
   `apollo_graph_comparison_runs` → write the score matrix.

## Run plan (agent-driven, local Docker)

`WORKFLOW.md` has the exact commands.

> **Difficulty is a routing key, not a pedagogical claim.** `session_init` serves
> the *first problem at the requested difficulty* in the inferred concept, so two
> problems sharing a `(concept, difficulty)` collide (only one is ever served).
> To grade all 5 uniquely, each problem gets a distinct difficulty within its
> concept: gdp_components → {gdp_identity: intro, net_exports_sign: standard,
> nnp_chain: hard}; nominal_vs_real_gdp → {real_gdp_from_deflator: standard,
> real_gdp_growth: hard}. The probe's `_difficulty_for` mirrors this.

## Headline metric (case-3)

For **Q4 strong**: if the rearranged real-GDP form fails to resolve →
`edge_coverage`/`usage` drop *despite a correct answer* → case-3 reproduces on
macro (general bug). If the `{PI: deflator}` substitution resolves it →
`edge_coverage`/`usage` go positive → the derived-form fix generalizes beyond
fluids. Reported as a per-variation score matrix in `RESULTS.md`, with the Q4
strong row called out.

## Housekeeping

The derived-equation fix is **uncommitted** on `fix/apollo-retrieval-grounding`;
the macro work is almost entirely new files so it doesn't disturb it. Stays on
this branch as experimental scaffolding; a clean `staging`-based PR is a later
decision.
