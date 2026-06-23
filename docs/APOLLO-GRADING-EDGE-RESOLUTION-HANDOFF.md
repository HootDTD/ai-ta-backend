# Handoff: Apollo edge grading — Fix A landed, but `edge_coverage` still 0 live (student-edge resolution gap)

**Date:** 2026-06-22
**Status:** Fix A (reference-side USES/PRECEDES edges) is **MERGED to `staging`**
(PR #59, `staging @ 22b9d08`, commit `c9dac7c`). **This handoff is the next
session's starting point** for the second, downstream blocker Fix A surfaced — the
one that keeps `edge_coverage_score`/`usage_score` at 0 on a live Bernoulli run. It
is the confirmation of the "Not yet verified" open question in
`APOLLO-GRAPH-GRADING-HANDOFF.md`. **Jump to "How to verify your fix — the tests"
for the acceptance criteria + the exact tests to run.**

---

## TL;DR

`build_reference_canonical` now emits `USES` (from `uses_equations`) and `PRECEDES`
(from procedure `order`) edges, edge-symmetric with `Problem.to_kg_graph` — so the
reference graph finally carries the structural targets. **Verified live**: the
strong attempt's persisted findings show 7 `missing_edge` rows including the 2 new
`USES` and 1 new `PRECEDES` edges, and `usage_score` moved from the old **vacuous
1.0 → a real 0.0**.

**But `edge_coverage_score` is still 0** because **every structural edge the student
emits is dropped from S_norm for having an unresolved endpoint.** The student
reasons in a *chain of derived equations* (full Bernoulli → pressure-cancelled form
→ solved form); only the full form resolves to `eq.bernoulli`, and the parser wires
the `USES`/`SCOPES`/`DEPENDS_ON` edges to the **derived, unresolved** forms.
`build_student_canonical` drops any edge with an unresolved endpoint → 0 matched
edges → `edge_coverage = usage = 0`.

This is a **parser/resolution-layer problem**, independent of and downstream from
Fix A. Fix A is necessary (the reference targets now exist) but not sufficient.

---

## Evidence (probe `--tag .fixA1`, served `bernoulli_height_change_find_v2`, attempt_id=6 strong)

Ran `apollo_grade_probe.py` against a server booted from the Fix A branch (port
:8001), real OpenAI, graph-sim LIVE on. Strong = B+ (4/4 nodes covered), weak = F.

**graph-sim run scores (strong / weak), vs the pre-fix handoff baseline:**

| metric | strong | weak | pre-fix baseline |
|---|---|---|---|
| `node_coverage_score` | 0.75 | 0.25 | 0.50 / 0.25 |
| `usage_score` | **0.0** | 0.0 | **1.0 / 1.0 (vacuous)** |
| `edge_coverage_score` | **0.0** | 0.0 | 0 / 0 (dead) |
| `dependency_score` | 0.0 | 0.0 | 0 / 0 |
| `scoping_score` | 1.0 | 1.0 | 1.0 / 1.0 (vacuous; no ref SCOPES) |
| `procedure_order_score` | 1.0 | 1.0 | — |
| `abstained` | True | False | True / False (unchanged) |

`usage` flipping 1.0→0.0 is the proof Fix A took effect: the reference now has
real `USES` edges, so the dimension is measured instead of vacuous.

**Strong attempt findings:** `covered_node=4`, `unresolved=4`, `missing_edge=7`,
`matched_edge=0`. The 7 `missing_edge` rows (all `explicit`) are exactly R_norm's
edges, **including the Fix A additions**:
```
proc.plan_apply_equal_pressure_simplification -USES->    eq.bernoulli          (NEW)
proc.plan_set_v1_zero_and_solve_bernoulli     -USES->    eq.bernoulli          (NEW)
proc.plan_apply_equal_pressure_simplification -PRECEDES-> proc.plan_set_v1_...  (NEW)
eq.bernoulli -DEPENDS_ON-> simp.equal_pressure_simplification
eq.bernoulli -DEPENDS_ON-> proc.plan_apply_equal_pressure_simplification
simp.equal_pressure_simplification -DEPENDS_ON-> proc.plan_apply_equal_pressure_simplification
proc.plan_apply_equal_pressure_simplification -DEPENDS_ON-> proc.plan_set_v1_...
```

**The student graph (Neo4j, attempt 6) — three equation forms, only one resolves:**

| node | form (symbolic/surface) | resolution |
|---|---|---|
| `stu_95715dd54fc4` | `P1 + ½ρv1² + ρgh1 − (P2 + ½ρv2² + ρgh2)` (full Bernoulli) | **resolved → eq.bernoulli** |
| `stu_f40a68763632` | `½ρv1² + ρgh1 − (½ρv2² + ρgh2)` (P1,P2 cancelled) | **unresolved** |
| `stu_6448a96f1ae3` | `v2 = sqrt(2*g*h1)` (solved) | **unresolved** |

**Every student edge attaches to an unresolved node**, so all are dropped from
S_norm before matching:
```
(ProcedureStep stu_14c077…) -USES->      (Equation stu_f40a…  UNRESOLVED)   # the simplified form, not bernoulli
(Condition     stu_d19871…) -SCOPES->    (Equation stu_f40a…  UNRESOLVED)
(Simplification stu_9a3d9…) -SCOPES->    (Equation stu_f40a…  UNRESOLVED)
(Condition     stu_2551…)   -SCOPES->    (Equation stu_f40a…  UNRESOLVED)
(Equation      stu_f40a…)   -DEPENDS_ON->(Equation stu_95715… resolved)     # from-endpoint unresolved
(Equation      stu_95715…)  -DEPENDS_ON->(Definition stu_a6ac… UNRESOLVED)
```
The one resolved equation (`stu_95715…` = eq.bernoulli) has **no** edge whose other
endpoint also resolves, so nothing survives normalization.

## Root cause (two compounding effects)

1. **Students reason by transforming equations.** The parser faithfully emits a
   *chain* of equation nodes (governing eq → simplified eq → solved eq). The
   reference authors a single canonical equation entity (`eq.bernoulli`); the
   derived forms have no authored entity to resolve to, so they stay unresolved.
2. **Structural edges attach to the derived forms, not the canonical node.** The
   procedure's `USES` edge points at the *simplified* equation it operates on
   (`stu_f40a…`), which is both unresolved and (even if it resolved) not
   `eq.bernoulli`. `build_student_canonical`'s edge-normalization loop
   ("both endpoints must resolve, else DROP and count") then discards it.

So the reference `USES`/`PRECEDES`/`DEPENDS_ON` edges have nothing to match, and
`edge_coverage` stays 0 even though the targets now exist.

## Fix directions (next session — NOT yet attempted)

Ordered by leverage / decreasing safety:

- **D — resolve derived equation forms to their parent entity.** Teach resolution
  to recognize a simplified/rearranged Bernoulli (`stu_f40a…`) as `eq.bernoulli`
  (symbolic-subsumption / canonicalization of the student's equation chain). Then
  the `USES` edge endpoint resolves and matches. Highest payoff, hardest: needs a
  symbolic-equivalence/derivation check, not string match. Look at
  `apollo/resolution/` (`resolve_attempt`, `candidates`, the symbolic matcher) and
  the per-problem `symbolic_mappings`.
- **E — merge the student's equation chain before resolution**, collapsing
  full/simplified/solved forms of the same governing equation into one node whose
  edges are rewired to the representative, then resolve that. Touches the parser
  graph-assembly / a pre-resolution merge pass.
- **F — wire procedure `USES` edges to the canonical (governing) equation** rather
  than the derived form the step manipulates. A parser-prompt / edge-emission
  change; cheaper but changes what `USES` *means* for students.
- Re-examine the **abstention rule** in the same pass: strong still abstains
  (`abstained=True`), so its Layer-3 writes are suppressed even when it's the better
  answer (handoff finding #2, still open).

Whatever the approach: **spot-check a built `S_norm` for a strong attempt and
confirm the `USES` edge survives normalization with both endpoints resolved BEFORE
trusting `edge_coverage`** — that is the single check that was missing.

## How to verify your fix — the tests

Two layers: the **live Bernoulli question-probe** (the same harness that found
this) and a **deterministic regression** (required by the repo's 95% patch-coverage
contract — do NOT ship on the manual probe alone; it uses a real LLM and is noisy).

### A. Live end-to-end — the Bernoulli "question" tests

Fixtures = the seeded tier-2 Bernoulli problems + the probe driver:
- **The questions:** 5 hand-authored problems in
  `apollo/subjects/fluid_mechanics/concepts/bernoulli_principle/problems/`. The
  probe is deterministically served **`problem_02` = `bernoulli_height_change_find_v2`**
  (intro, cold-start pick); its reference now carries 2 `USES` + 1 `PRECEDES` edges
  (Fix A). The other 4 (incl. multi-equation `bernoulli_full_find_p2`) are good
  extra stressors.
- **The strong/weak explanations** are authored in
  `scripts/apollo_grade_probe.py` → `SCENARIOS["bernoulli_height_change_find_v2"]`
  (strong teaches all 4 reference nodes; weak gives only the Bernoulli equation).
- **Stack + run** (Neo4j `hoot-neo4j-local`, supabase local `:54322`, `:Canon`=41
  seeded — re-seed via the 3 idempotent scripts listed in
  `APOLLO-GRAPH-GRADING-HANDOFF.md` if the local DB was wiped):
  ```bash
  # boot a server from YOUR branch on a spare port (loads .env + .env.local):
  python -c "from dotenv import load_dotenv; load_dotenv('<repo>/.env'); \
    load_dotenv('<repo>/.env.local', override=True); import uvicorn; \
    uvicorn.run('server:app', host='127.0.0.1', port=8001)"
  WEB_BASE_URL=http://127.0.0.1:8001 python scripts/apollo_grade_probe.py --mode both --tag .resfix
  ```
- **PASS criteria — STRONG attempt** (in `apollo_graph_comparison_runs`):
  `edge_coverage_score > 0` **AND** `usage_score > 0`; and in
  `apollo_graph_comparison_findings` at least one `matched_edge` (e.g.
  `proc.plan_apply_equal_pressure_simplification -USES-> eq.bernoulli`); and the
  strong `USES` edge's equation endpoint is **resolved** (no longer in the
  `unresolved` findings). **Fix-A-only baseline today (attempt_id 6):** all of
  those are 0 and the endpoint `stu_f40a…` is UNRESOLVED — that is what you must flip.
- **Inspect:** `apollo_graph_comparison_runs` / `_findings` (`finding_kind` column)
  by `attempt_id`; Neo4j `MATCH (n:_KGNode {attempt_id:N})` for node resolution
  (`n.resolution`, `n.resolved_key`) + edges. (See this handoff's Evidence section
  for the exact queries that produced the baseline.)

### B. Deterministic regression (REQUIRED) — use Fix A's tests as the template

- **Scoring is already proven correct** by
  `apollo/graph_compare/tests/test_scores.py::test_real_problem_uses_precedes_dims_score_when_student_matches`
  (a matching S_norm `USES` edge → `usage==1.0`, `edge_coverage>0`). So your gap is
  strictly UPSTREAM of scoring.
- **The missing spot-check (write this):** feed a student graph whose Bernoulli
  equation is stated in a **derived form** (`½ρv1²+ρgh1-(½ρv2²+ρgh2)`) plus a `USES`
  edge to it, run resolution + `build_student_canonical`, and assert the `USES` edge
  **survives** with both endpoints resolved to `(proc.*, eq.bernoulli)`. This is
  exactly the check that was missing. Put it under `apollo/resolution/tests/`
  (resolution) and/or `apollo/graph_compare/tests/test_student_canonical.py`.
- **End-to-end unit:** `build_reference_canonical(problem_02)` + the resolved
  S_norm → `grade_attempt(...)` → assert `edge_coverage_score > 0`. Mirror the
  builders in `apollo/graph_compare/tests/_builders.py`.

## Pointers

- **Fix A code:** `apollo/graph_compare/canonical.py` (`build_reference_canonical`,
  the USES/PRECEDES blocks); tests in `apollo/graph_compare/tests/`
  (`test_reference_canonical.py`, `test_scores.py`).
- **Edge drop:** `build_student_canonical` in the same file — the loop that drops
  edges with an unresolved endpoint (`dropped_edge_count`).
- **Resolution:** `apollo/resolution/` (`resolve_attempt`, `candidates`, symbolic
  matching). **Parser:** `apollo/parser/` (equation-node + USES-edge emission).
- **Owner doc:** `docs/architecture/apollo.md` (now records R_norm as
  edge-symmetric; should gain a note that *student-side* edge resolution is the
  remaining gap once D/E/F lands).
- **Prior handoff:** `docs/APOLLO-GRAPH-GRADING-HANDOFF.md` (finding #1 = this).
