# A3 — S1 Reference-Graph Judge Calibration Adjudication

**Lane:** A3 (diagnosis Q4 — S1 reference-graph judge calibration)
**Date:** 2026-07-03 · **Analyst:** campaign A3 (read-only)
**Inputs:** `campaign/out/f1c/s1-results.json` (229 items, 172 pass = 75.1%),
`campaign/out/f1/s1-results.json` (219 items, 160 pass = 73.1%),
`campaign/judges/s1_reference_graph.py`, seed DAGs under `apollo/subjects/**`,
edge ontology `apollo/ontology/edges.py`.

---

## 1. Failure inventory & run-to-run stability

f1c: **57 failures / 229** (18 node, 39 edge). f1: 59 failures / 219.
Overlap: **33 fail in BOTH runs, 24 only-f1c, 26 only-f1.** The judge is
**noisy** — only 33/57 (~58%) of f1c failures reproduce. This instability is
itself a calibration signal (see §3, judge mode J-INCONSISTENT).

By subject (f1c):

| Subject | Items | Fails | Node fails | Edge fails |
|---|---|---|---|---|
| linear_motion | 90 | 19 | 7 | 12 |
| macroeconomics | 82 | 21 | 8 | 13 |
| fluid_mechanics | 57 | 17 | 3 | 14 |
| **Total** | **229** | **57** | **18** | **39** |

---

## 2. Root-cause discoveries (drive the whole adjudication)

### 2A. Systemic edge-type MISLABEL — prereq edges emitted as PRECEDES (26 edges)

The S1 raw builder (`campaign/out/f1c/run_s1_s2.py:71-75`) reads every
`apollo_entity_prereqs` row and hardcodes:

```python
edges.append({"edge_type": "PRECEDES", "from_node_id": fk, "to_node_id": tk})
```

But per the production ontology (`apollo/ontology/edges.py:40-43`), **PRECEDES
is legal ONLY between `(procedure_step, procedure_step)` pairs**; generic
concept→concept prerequisite links must be **DEPENDS_ON**. The prereq rows are
stored `from=dependent, to=prerequisite` (matches the DAG `requires from->to`,
e.g. `kinetic_energy_density requires fluid_density`).

`topological_order` (`apollo/ontology/graph.py:101-144`) defines
PRECEDES A→B as "A comes **before** B." So the minted edge
`PRECEDES kinetic_energy_density → fluid_density` literally asserts "kinetic
energy density comes before fluid density" — **false** (density is the given
that precedes the derived quantity). The edge is **true as DEPENDS_ON, false as
PRECEDES.** The *content* of the graph is correct; the *edge_type label* is
wrong. This one harness bug generates **all 14 fluid edge fails + 12 of 13
macro edge fails = 26 edge failures.**

The judge catches this **inconsistently**: it FAILS `net_exports→exports` but
PASSES the structurally identical `gdp_expenditure→consumption` (both are
composite→component `requires` edges). It FAILS 14 fluid prereq edges but
PASSES `continuity_equation→fluid_velocity` (same reversal). Same root defect,
coin-flip verdict — the source of the run-to-run noise.

### 2B. Single-problem anchoring on a multi-problem subject graph (14 node fails)

The item carries `problem = {"problems": [all problems for the concept]}`
(`run_s1_s2.py:86`) — the graph spans **every** problem in the subject. But the
system prompt says *"auditing a minted reference solution against its source
problem … the authored reference solution"* (singular). The judge anchors on
ONE problem and rejects nodes that serve the OTHERS as "off-topic /
hallucinated." Every macro node fail and every fluid node fail is a **real DAG
node** rejected for not being exercised by the one problem the judge fixated on
(e.g. `gnp`, `gdp_deflator`, `base_year`, `real_gdp_growth` are all bona-fide
nodes in the macro DAGs; `gravitational_acceleration`,
`energy_conservation_fluid` are core nodes in the fluid DAG).

### 2C. linear_motion is a genuinely broken provisional graph (16 fails, real)

Node dump shows **triplicated encodings** of the same 2-problem solution:
`def_initial_velocity ≈ def1 ≈ var1 ≈ vm1`, etc. — three parallel naming
schemes (named / `defN,varN` / `vmN,ps1`) plus stray `def_velocity`,
`map_velocity`. The judge only FAILS the "4"-suffixed overflow nodes
(`def4,var4,proc3,proc4,vm4` — there is no 4th step in `v=v0+at`) and the
`_velocity` duplicates, while **passing the far larger duplication**
(`vm1/var1/def1/def_initial_velocity` all coexist, all PASS). So linear_motion's
true graph quality is *worse* than S1 shows; these are genuine seed defects
from the provisional auto-provision, not judge harshness.

### 2D. Structural checker skips dangling edges (secondary)

`find_structural_defects` (`s1_reference_graph.py:66-70`) silently `continue`s
past any edge whose endpoint isn't a node, instead of flagging it. linear_motion's
dangling edges (referencing non-existent `vm4`, `eq4`, `ps1` targets across the
merged problems) therefore fall through to the noisy LLM instead of a
deterministic FAIL.

---

## 3. Recurring judge-failure MODES

| Mode | Description | Offending prompt language | Count |
|---|---|---|---|
| **J-SCOPE** | Rejects a valid node because *this* problem doesn't use it, though the graph spans all the subject's problems | "auditing a minted reference solution against its **source problem**"; "a real, correct step of **the** solution … not off-topic" | 14 |
| **J-EDGE-TEMPORAL** | Reads PRECEDES as strict temporal order and flags prerequisite (DEPENDS_ON) edges as "reversed"/"not a logical sequence" | "is the claimed PRECEDES/USES relationship **actually true given the two endpoints**?" | 26 |
| **J-INCONSISTENT** | Same defect class → opposite verdict across items/runs (net_exports vs gdp_expenditure; 33/57 reproduce) | (emergent from single-item, no rubric) | — |
| **J-VERBATIM** | Demands explicit textual mention; rejects standard domain concepts not spelled out | "not hallucinated"; "Answer **strictly from the given material**" | (overlaps J-SCOPE) |

Top graph-defect classes:
1. **Provisional-scrape node duplication + hallucinated overflow** (linear_motion):
   node ids `def4, var4, proc3, proc4, vm4` (overflow, no such step);
   `def_velocity, map_velocity` and the whole `vmN/defN/varN` triplication
   (near-duplicates). **~16 detected, more undetected.**
2. **Dangling edges** (linear_motion): PRECEDES edges into non-existent
   `vm4→eq1`, `vm3→eq4`, `eq4→ps1`, `vm1→eq2` across merged problems.
3. **(Harness, not seed)** concept-prereq edges mislabeled PRECEDES — content correct.

---

## 4. Full labeled adjudication table (57 items)

Verdict key: **H** = JUDGE-TOO-HARSH (fix prompt), **W** = GRAPH-WRONG (fix
graph/data), **A** = AMBIGUOUS. `stab` = BOTH (failed both runs) / f1c-only.

### fluid_mechanics (17) — 3 H, 14 W

| # | item | stab | V | justification |
|---|---|---|---|---|
|1|node concept.gravitational_acceleration|BOTH|H|Core DAG node; rejected only because *this* problem is horizontal. J-SCOPE.|
|2|node concept.gravitational_potential_density|BOTH|H|Real DAG node (ρgh), prereq of energy_conservation. J-SCOPE/J-VERBATIM.|
|3|node concept.energy_conservation_fluid|f1c|H|Real DAG node, prereq of bernoulli. J-VERBATIM ("not explicitly mentioned").|
|4|edge kinetic_energy_density→fluid_density|BOTH|W|Prereq mislabeled PRECEDES (2A). True as DEPENDS_ON.|
|5|edge kinetic_energy_density→fluid_velocity|BOTH|W|Same (2A).|
|6|edge gravitational_potential_density→fluid_density|BOTH|W|Same (2A).|
|7|edge gravitational_potential_density→gravitational_acceleration|BOTH|W|Same (2A).|
|8|edge gravitational_potential_density→elevation|BOTH|W|Same (2A).|
|9|edge energy_conservation_fluid→kinetic_energy_density|f1c|W|Same (2A).|
|10|edge energy_conservation_fluid→gravitational_potential_density|BOTH|W|Same (2A).|
|11|edge energy_conservation_fluid→pressure|BOTH|W|Same (2A); judge correct that direction reversed.|
|12|edge continuity_equation→cross_sectional_area|f1c|W|Same (2A).|
|13|edge continuity_equation→incompressibility_assumption|BOTH|W|Same (2A); judge correct.|
|14|edge bernoulli_principle→energy_conservation_fluid|f1c|W|Same (2A).|
|15|edge bernoulli_principle→incompressibility_assumption|BOTH|W|Same (2A); judge correct.|
|16|edge volumetric_flow_rate→fluid_velocity|f1c|W|Same (2A).|
|17|edge volumetric_flow_rate→cross_sectional_area|BOTH|W|Same (2A).|

### macroeconomics (21) — 8 H, 12 W, 1 A

| # | item | stab | V | justification |
|---|---|---|---|---|
|18|node concept.income_from_abroad|f1c|H|Real node in gdp_components DAG (feeds gnp). J-SCOPE.|
|19|node concept.gnp|f1c|H|Real DAG node. J-SCOPE ("not involved in GDP expenditure").|
|20|node concept.base_year|BOTH|H|Real node in nominal_vs_real DAG. J-SCOPE.|
|21|node concept.gdp_deflator|f1c|H|Real DAG node. J-SCOPE/J-VERBATIM.|
|22|node concept.percentage_change|BOTH|H|Real node (feeds real_gdp_growth). J-SCOPE.|
|23|node concept.real_gdp_growth|BOTH|H|Real DAG node. J-SCOPE.|
|24|node var.deflator|BOTH|H|Legit variable for the deflator problem. J-SCOPE.|
|25|node var.PI (price index)|BOTH|H|Legit variable for nominal-vs-real problem. J-SCOPE.|
|26|edge net_exports→exports|BOTH|W|Prereq mislabeled PRECEDES (2A); judge correct on reversal.|
|27|edge gnp→gdp_expenditure|BOTH|W|`extends` mislabeled PRECEDES, reversed (2A).|
|28|edge gnp→income_from_abroad|BOTH|W|Prereq mislabeled/reversed (2A).|
|29|edge nnp→gnp|f1c|W|`extends` reversed (2A); judge correct (gnp computed first).|
|30|edge nnp→depreciation|BOTH|W|Prereq reversed (2A); judge correct.|
|31|edge nominal_gdp→gross_domestic_product|BOTH|W|Prereq reversed (2A).|
|32|edge gdp_deflator→nominal_gdp|BOTH|W|Prereq reversed (2A).|
|33|edge gdp_deflator→base_year|BOTH|W|Prereq reversed (2A).|
|34|edge real_gdp→nominal_gdp|BOTH|W|Prereq reversed (2A); judge correct.|
|35|edge real_gdp→gdp_deflator|BOTH|W|Prereq reversed (2A).|
|36|edge real_gdp_growth→real_gdp|BOTH|W|Prereq reversed (2A).|
|37|edge real_gdp_growth→percentage_change|f1c|W|Prereq reversed (2A).|
|38|edge base_year→price_index|BOTH|A|Direction is defensibly correct (base year defines the index), but rejected on relevance grounds (J-SCOPE). Defensible either way.|

### linear_motion (19) — 3 H, 16 W

| # | item | stab | V | justification |
|---|---|---|---|---|
|39|node def.def4|f1c|W|Overflow node — no 4th step in v=v0+at. Provisional-scrape hallucination.|
|40|node varmap.var4|f1c|W|Overflow duplicate (var1-3 already cover the mapping).|
|41|node proc.proc3|f1c|W|Overflow proc step; provisional scrape.|
|42|node proc.proc4|f1c|W|Hallucinated proc step.|
|43|node varmap.vm4|f1c|W|Overflow duplicate of the vm-series.|
|44|node def.def_velocity|BOTH|H|A "velocity" definition is reasonable in a velocity problem; judge over-strict. (Also a near-dup, but not clearly hallucinated.)|
|45|node varmap.map_velocity|BOTH|W|Near-duplicate of map_initial_velocity / vm-series. Duplication defect.|
|46|edge def.def4→varmap.var4|f1c|W|Both endpoints are overflow nodes.|
|47|edge varmap.var2→eq.eq1|f1c|H|var-mapping→equation is a legitimate USES-style feed; judge rejects order pedantically. J-EDGE-TEMPORAL.|
|48|edge varmap.var1→eq.eq2|BOTH|H|Judge *admits* "it is correct that var1 precedes eq2" then rejects on id-confusion. Self-contradictory → harsh.|
|49|edge varmap.var4→eq.eq2|f1c|W|Source var4 is an overflow node.|
|50|edge varmap.vm1→eq.eq2|f1c|W|Cross-scheme dangling edge (merged-problem id collision).|
|51|edge varmap.vm2→eq.eq3|f1c|W|Dangling / cross-scheme.|
|52|edge varmap.vm3→eq.eq4|f1c|W|Dangling (eq4 overflow).|
|53|edge eq.eq4→proc.ps1|f1c|W|eq4 overflow + ps1 stray node.|
|54|edge varmap.vm1→eq.eq1|BOTH|W|Cross-scheme id collision (2D).|
|55|edge varmap.vm2→eq.eq1|f1c|W|Cross-scheme dangling.|
|56|edge varmap.vm3→eq.eq1|BOTH|W|Cross-scheme dangling.|
|57|edge varmap.vm4→eq.eq1|f1c|W|vm4 overflow source.|

---

## 5. Aggregate

| Bucket | Count | % |
|---|---|---|
| JUDGE-TOO-HARSH (H) | 14 | 24.6% |
| GRAPH-WRONG (W) | 42 | 73.7% |
| AMBIGUOUS (A) | 1 | 1.8% |

Per subject (H / W / A):
- fluid_mechanics: 3 / 14 / 0
- macroeconomics: 8 / 12 / 1
- linear_motion: 3 / 16 / 0

**Critical caveat on the 42 "GRAPH-WRONG":** they are NOT 42 independent seed
defects. 26 of them (all fluid + 12 macro edges) are **one** harness bug — the
`edge_type="PRECEDES"` mislabel of prerequisite edges (§2A) — where the graph's
*content is correct* and only the edge-type label is wrong. The genuinely
broken *seed data* is confined to **linear_motion's 16 provisional-scrape
defects** (duplication + hallucinated overflow + dangling edges). fluid and
macro DAGs are clean.

---

## 6. Recommendations

### (i) Prompt-calibration changes (fix the judge-too-harsh + the harness read)

1. **Reframe edge semantics (highest leverage — recovers ~26 edges).** The
   system prompt asks *"is the claimed PRECEDES/USES relationship actually true
   given the two endpoints?"* and the judge reads PRECEDES as temporal order.
   Concept→concept edges are prerequisite/**DEPENDS_ON** dependencies. Add:
   *"A concept→concept edge encodes a prerequisite dependency (the FROM concept
   depends on / is built from the TO concept). Judge whether that dependency is
   true — do NOT judge temporal/derivation ordering, and do NOT flag it as
   'reversed'."* (Root fix is the minter emitting DEPENDS_ON — see (ii)-harness.)
2. **State the multi-problem scope (recovers all 14 node H).** Replace
   *"auditing a minted reference solution against its source problem … the
   authored reference solution"* with *"the graph spans ALL of the subject's
   problems (provided as a list). A node is VALID if it is a real, correct step
   in ANY of the provided problems; do NOT reject a node merely because one
   particular problem does not use it."*
3. **Allow standard domain knowledge (kills J-VERBATIM).** Soften *"Answer
   strictly from the given material … not hallucinated"* to *"A node is valid if
   it is a correct concept for this domain, even if not spelled out verbatim in
   a problem statement; reject only genuine hallucinations, off-domain content,
   or exact duplicates."*

### (ii) Graph / data fixes

- **Harness (biggest single win, 26 edges):** `run_s1_s2.py:75` — emit
  `edge_type="DEPENDS_ON"` for `apollo_entity_prereqs` rows (reserve PRECEDES
  for procedure_step pairs), per `apollo/ontology/edges.py`. No seed DAG change
  needed; fluid & macro DAGs are already correct.
- **Seed data — linear_motion only (re-provision; it is flagged PROVISIONAL):**
  remove hallucinated overflow nodes `def.def4, varmap.var4, proc.proc3,
  proc.proc4, varmap.vm4`; de-duplicate the triplicated encoding
  (`def_initial_velocity`/`def1`/`var1`/`vm1` … collapse to one scheme;
  drop `def.def_velocity`, `varmap.map_velocity`); drop the dangling
  cross-scheme edges (`vm1→eq1`, `vm2→eq1`, `vm3→eq1`, `vm4→eq1`,
  `vm3→eq4`, `eq4→ps1`, `vm1→eq2`, `varmap.var4→eq2`, `def4→var4`).
- **Structural checker:** `find_structural_defects` should FLAG (not `continue`
  past) edges with a missing endpoint — deterministically catches the
  linear_motion dangling edges instead of the coin-flip LLM.

### (iii) Honest post-calibration S1 estimate

- Fixing (i) + the harness edge_type fix makes **fluid_mechanics ≈ 57/57 (100%)**
  and **macroeconomics ≈ 20–21/21 recovered → ~99–100%**. The authored subjects
  are essentially clean — their 75% is an artifact of the harness mislabel plus
  judge over-harshness, not real graph quality.
- **linear_motion stays genuinely defective** (~16 real seed defects, plus more
  duplication the judge doesn't even catch). Until re-provisioned, its ~79%
  (71/90) is if anything optimistic.
- **Overall S1 post-calibration ≈ (229 − ~16 residual linear defects) / 229 ≈
  92–93%** — still short of the 95% bar, but the entire residual gap lives in the
  provisional linear_motion subject. **Restricted to the two authored subjects
  (fluid + macro): ≈ 138–139 / 139 ≈ 99–100%, comfortably above 95%.**

**Bottom line:** S1's 75.1% does **not** reflect bad authored graphs. ~46% of
the failures are one harness edge-type bug (content correct), ~25% are judge
over-harshness, and the only real graph-quality problem is the provisional
linear_motion scrape. Fix the minter edge_type + the three prompt changes and
the 95% bar is met (indeed ~100%) on the authored subjects; the linear_motion
subject needs re-provisioning before its number is trustworthy.
