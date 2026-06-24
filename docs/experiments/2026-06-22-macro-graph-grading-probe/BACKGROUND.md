# Background: the system under test

## Apollo, in one paragraph

Apollo is Hoot's "learning-by-teaching" mode: the student teaches a
deliberately-confused AI learner. As the student explains, a parser
(`apollo/parser/parser_llm.py::parse_utterance`, one GPT-4o structured-outputs
call per turn) extracts a typed **knowledge graph** into Neo4j — 6 node types
(`equation`, `condition`, `simplification`, `definition`, `variable_mapping`,
`procedure_step`) and 4 edge types (`PRECEDES`, `USES`, `DEPENDS_ON`, `SCOPES`).
When the student clicks **Done**, the graph is graded against an authored
reference solution.

## The grading pipeline (the §6.4 Done chain)

`apollo/handlers/done_grading.py::run_graph_simulation`, in order:

1. **Build candidates** — `build_problem_candidates(problem, misconceptions)`
   produces the **closed** match-target set: *this problem's reference entities +
   the course's misconceptions* (and a per-problem `symbolic_mappings` table
   collected from problem-level `symbolic_mappings` + each simplification step's
   `content.substitution`).
2. **`validate_student_graph`** — a 422 structural gate (single attempt_id, edge
   endpoints exist, `EDGE_ALLOWED_PAIRS`, no `PRECEDES` cycle).
3. **`resolve_attempt`** — *cleanser #1 (identity).* Each student node is matched
   to a candidate via tiers: `exact` → `symbolic` (SymPy sign-exact, equations
   only) → `alias` → `fuzzy` (RapidFuzz ≥0.9), under a **hard same-node-type**
   constraint + misconception competition + one optional LLM adjudication. Every
   node ends `resolved` (with a method-capped confidence) or **`unresolved`**.
4. **`build_student_canonical` (S_norm)** — *cleanser #2 (structure).* Merge
   student nodes by `resolved_key`; normalize edges to canonical endpoints; and
   **DROP every edge with an unresolved endpoint** (counted in
   `dropped_edge_count`). ← *this is where a dropped derived-form node takes its
   `USES` edge with it.*
5. **`build_reference_canonical` (R_norm)** — built only from the problem's own
   `reference_solution`: nodes keyed on `entity_key`; `DEPENDS_ON` from
   `depends_on`; `USES` from procedure `content.uses_equations`; `PRECEDES`
   auto-derived over procedure `content.order`; one `ReferencePathView` per
   `declared_paths` entry.
6. **`grade_attempt(S_norm, R_norm)`** — pure scoring.
7. audit → abstention → persist runs/findings.

## The scores (all in `apollo_graph_comparison_runs`)

| Score | Definition |
|---|---|
| `coverage_score` | fraction of the winning declared path's reference keys present in S_norm (pure **set membership** of resolved keys) |
| `soundness_score` | `1 − 0.5·(#S_norm nodes whose key starts `misc.`)`, floored at 0 |
| `bisimilarity_score` | harmonic mean of soundness and coverage |
| `node_coverage_score` | = coverage_score (v1) |
| `edge_coverage_score` | weighted matched/total reference edges (explicit=1.0, inferred=0.5) |
| `scoping_score` | edge-coverage restricted to `SCOPES` edges |
| `usage_score` | edge-coverage restricted to `USES` edges |
| `procedure_order_score` | `1 − inversions/comparable` over the winning path order |
| `dependency_score` | direction-loose `DEPENDS_ON` coverage |
| `contradiction_score` | = soundness_score (v1) |

**Abstention** (`apollo/grading/abstention.py`): the **only** gate that sets
`abstained=True` is `unresolved_rate > 0.35`. Low parser confidence (<0.6) and
low misconception confidence (<0.8) merely add a reason and suppress specific
finding kinds.

**Two interpretation traps** (load-bearing for reading our results):
- **Vacuous 1.0** — every edge sub-score returns **1.0** when the *reference*
  has zero edges of that type. So a 1.0 on `usage`/`scoping` can mean "not
  tested," not "correct." Our reference KGs must include real `USES`/`PRECEDES`/
  `SCOPES`/`DEPENDS_ON` edges to make those dimensions *discriminate*.
- **Coverage is gated entirely by resolution** — a correct explanation that
  fails to resolve scores 0 coverage despite being right.

## The "case-3" failure (why we built this)

From `APOLLO-USES-EDGE-RESOLUTION-HANDOFF.md`: `unresolved` conflates three
cases — (1) genuinely wrong, (2) correct-but-out-of-scope, and (3) **a
derived/solved form of an in-scope equation**. Case 3 is the bug: in the live
Bernoulli probe (attempt 8) the student stated Bernoulli *and* solved it to
`v2 = sqrt(2·g·h1)`; the parser hung the procedure's `USES` edge on the
**solved form**, which is not SymPy-equivalent to Bernoulli under any
substitution, so it stayed unresolved → the `USES` edge was dropped from S_norm
→ `edge_coverage = usage = 0` for a strong answer.

The deterministic fix (uncommitted on the working tree) lets each simplification
carry an explicit `substitution: {var: expr}` map that the symbolic tier uses to
resolve a *pressure-cancelled* form — but it's narrow (only fires when that
specific equation node is produced) and the parse is non-deterministic.

**This experiment asks: does case-3 reproduce on a non-fluid subject?** Ch.6's
GDP-deflator equation (`deflator = (nominalGDP/realGDP)·100`) rearranges into the
real-GDP formula (`realGDP = nominalGDP/(priceIndex/100)`). Those are equivalent
*equations* but not equivalent *zero-form expressions* (one is solved for
`realGDP`, the other for `deflator`) — the exact case-3 shape, in macroeconomics.

## Why OpenStax Macro Ch.6 specifically

- **Open-licensed** (CC BY 4.0) — author reference KGs + student scenarios over
  it freely; not a copyrighted scrape.
- **Quantitative** — Apollo needs equations + numeric `given_values:dict[str,float]`.
  Ch.6 supplies the GDP identity, NNP chain, nominal↔real conversion, and growth
  rate, all with worked numeric examples.
- **Contains a clean derived form** — the deflator↔real-GDP rearrangement.
