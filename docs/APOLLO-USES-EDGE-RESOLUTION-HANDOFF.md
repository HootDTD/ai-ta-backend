# Handoff: Apollo edge grading — the derived-equation fix is correct but the LIVE blocker is structural (USES edge lands on an unresolved derived form)

**Date:** 2026-06-22
**Status:** Deterministic fix DONE + green, but **NOT committed**. Live probe ran
and **disproved** the live hypothesis: `edge_coverage_score`/`usage_score` are
**still 0**. Root cause is now isolated with concrete data. **Next step (paused
mid-brainstorm): test a NON-fluid subject (finance) before committing to any fix.**

Continues `docs/APOLLO-GRADING-EDGE-RESOLUTION-HANDOFF.md` (Fix A + the
derived-form open question) and `docs/APOLLO-GRAPH-GRADING-HANDOFF.md`.

---

## TL;DR

1. **Built + proved (deterministically) the "derive-from-simplifications" fix**:
   each `simplification.content` may carry an explicit `substitution: {var: expr}`
   map; `build_problem_candidates` merges all of them into the resolver's
   `symbolic_mappings`; the symbolic tier then resolves a pressure-cancelled
   Bernoulli to `eq.bernoulli`. 6 acceptance tests + 4 unit tests green, 100%
   patch coverage, full apollo suite green (1339 passed).
2. **Ran the live probe** (real LLM, local stack). It **did not move the live
   numbers** — strong attempt still `edge_coverage=0`, `usage=0`.
3. **Diagnosed why, with concrete data** (attempt_id=8): the student DID state
   Bernoulli (a node resolved to `eq.bernoulli`), but the procedure's **`USES`
   edge was attached to the unresolved SOLVED form `v2 = sqrt(2gh1)`** — not to
   the Bernoulli node. The fix was **inert** this run (no pressure-cancelled
   *equation* node was even produced).
4. **Reframed the architecture** (with the user): resolution is a *lossy hard
   gate* that decides node identity in isolation and drops unresolved-endpoint
   edges *before* any score runs. The real fix is in the **cleanser**, not the
   scorer. Two viable directions (below). **Fix F — parser wires USES to the
   governing equation — is REJECTED** (non-generic + fights the LLM).
5. **Decision pending**: run the system on a non-fluid subject (finance) first,
   because we're heavily fluid-biased and the bug is "real but niche."

---

## Part 1 — What was built (the deterministic fix)

**Contract (user-approved):** `applies_when` on a simplification may be a
*natural-language concept*, so it is NEVER parsed. Instead each
`simplification.content` carries an explicit `substitution: {var: expr}`.
`build_problem_candidates` collects every simplification's `substitution` (+ the
problem-level `symbolic_mappings`) into the resolver's `symbolic_mappings`; the
existing sign-exact symbolic tier (`tiers._symbolic_equiv`) then matches a derived
equation form. Mechanism is shared / subject-agnostic; the substitution is
structured authored data the provisioner emits.

**Code/data changed (uncommitted, on the working tree):**
- `apollo/graph_compare/problem_inputs.py` — new `_collect_symbolic_mappings`
  (problem-level wins on collision via `setdefault`). 100% patch coverage.
- `apollo/subjects/.../problems/problem_02.json` — `equal_pressure_simplification`
  gained `"substitution": {"P2": "P1"}`.
- `apollo/subjects/.../problems/problem_01.json` — `horizontal_simplification`
  gained `"substitution": {"h2": "h1"}`.
- Tests: `apollo/graph_compare/tests/test_derived_equation_resolution.py`,
  `test_derived_equation_portability.py` (real fluid `h1==h2` + a synthetic
  NON-fluid DC-circuit problem), plus 4 collector unit/guard tests in
  `test_problem_inputs.py` (conceptual-only → no mapping; collision precedence;
  multi-merge). `test_symbolic_mappings_read_from_problem` updated to
  `{"d":"2*r","h2":"h1"}`.
- Owner doc `docs/architecture/apollo.md` (~line 87) updated.

**Verification:** all 6 acceptance tests green; full `apollo/` suite 1339 passed,
156 skipped (pre-existing testcontainers/legacy-V2 skips). Note: a PRE-EXISTING
unrelated collection error exists in `apollo/provisioning/tests/test_scrape.py`
(`ImportError: chunk_content_hash`) — not ours.

**The fix is correct, but NARROW** — it only handles a *pressure-cancelled
equation node*. See Part 3 for why that wasn't enough live.

---

## Part 2 — How to run the live probe (it's all wired up)

The agent CANNOT run these — the auto-mode classifier blocks local-DB writes
(mis-reads `127.0.0.1:54322` as remote prod) AND blocks editing settings to
self-grant permission. So the **user runs them from a terminal**. Two helper
scripts were added:

- `scripts/run_local_probe.py` — one-shot: guards DB-is-local → re-seeds the
  concept registry → boots the server on `:8001` from the working tree → runs
  `apollo_grade_probe.py --mode both` → prints the graph-sim scores.
- `scripts/inspect_attempt.py <attempt_id>` — read-only dump of an attempt's
  Neo4j nodes (with `resolution`/`resolved_key`) + edges (with endpoint
  resolution) + the PG `apollo_graph_comparison_runs`/`_findings`.

**Run (PowerShell, from `ai-ta-backend/`, project venv):**
```
.venv/Scripts/python.exe scripts/run_local_probe.py
.venv/Scripts/python.exe scripts/inspect_attempt.py 8      # strong attempt
```

**CRITICAL gotchas learned the hard way:**
- **Use the project venv** `.venv/Scripts/python.exe` — the anaconda base python
  has no `asyncpg` (unit tests mock the DB; the probe needs a real connection).
- **Re-seed is mandatory.** The live grading path reads the problem from
  `apollo_concept_problems.payload` (DB), **NOT** the JSON files. Edits to the
  problem JSONs only take effect after `seed_apollo_concept_registry` re-runs.
  The seed needs `DATABASE_URL` set (it does NOT read `SUPABASE_DB_URL`);
  `run_local_probe.py` maps it across.
- Local stack must be up: `supabase_db` `:54322`, `hoot-neo4j-local` `:7687`,
  `:Canon`=41. Flags already on in `.env`:
  `APOLLO_GRAPH_SIM_SHADOW_ENABLED/LIVE_ENABLED/LAYER3_ENABLED`.

---

## Part 3 — The live result + concrete diagnosis (attempt_id=8, STRONG)

**Probe outcome:** discriminates (strong B+ / 80 vs weak F / 0), but on the
acceptance criteria: strong `edge_coverage_score=0.0`, `usage_score=0.0`,
`abstained=True (unresolved_rate_above_threshold)`. **The fix did not move the
live numbers.**

**`inspect_attempt.py 8` nodes (the smoking gun):**

| node | kind | surface | resolution |
|---|---|---|---|
| `stu_ec50c6575a86` | Equation | `P1+½ρv1²+ρgh1-(P2+½ρv2²+ρgh2)` (FULL) | **resolved `eq.bernoulli`** (exact) |
| `stu_0cfabd4cfb2f` | Definition | "Bernoulli's equation" | **resolved `eq.bernoulli`** (llm) |
| `stu_708885292cb4` | Equation | `v2 = sqrt(2*g*h1)` (SOLVED) | **unresolved** |
| `stu_21b188931468` | ProcedureStep | "substitute v1=0… solve for v2" | resolved `proc.plan_set_v1_zero_and_solve_bernoulli` (llm) |
| `stu_7b54a74a7e21` | Simplification | "P1 = P2" | **unresolved** |
| `stu_3bbbb4f38580` | Condition | "open to the atmosphere" | **unresolved** |
| `stu_a8d3128056cd` | Simplification | "the reservoir is wide" | resolved `proc.plan_set_…` (llm) |

**Edges:**
```
(P1=P2 simp, UNRESOLVED) -SCOPES->     (FULL eq, eq.bernoulli)
(condition, UNRESOLVED)  -SCOPES->     (FULL eq, eq.bernoulli)
(FULL eq, eq.bernoulli)  -DEPENDS_ON-> (Definition, eq.bernoulli)
(proc, proc.plan_set_…)  -USES->       (SOLVED eq "v2=sqrt(2gh1)", UNRESOLVED)   <-- THE PROBLEM
```

**The finding, precisely:** everything needed for a matched USES edge EXISTED —
a proc resolved to `proc.plan_set_v1_zero_and_solve_bernoulli`, a node resolved
to `eq.bernoulli`, and the reference literally has
`proc.plan_set_v1_zero_and_solve_bernoulli -USES-> eq.bernoulli` (it's in the
`missing_edge` findings). **But the parser hung the proc's `USES` edge on the
SOLVED form, not on the Bernoulli node.** The Bernoulli node has SCOPES/DEPENDS_ON
edges but **no incoming USES**. So no student USES edge survives normalization
with both endpoints resolved → `edge_coverage = usage = 0`.

**Why the fix was inert here:** the pressure-cancelled *equation* node (the fix's
target) was NOT produced this run — turn-2 ("P1=P2, terms cancel") became a
`Simplification "P1=P2"` node, not an equation. The full form resolved via
`exact` anyway. The solved form `v2=sqrt(2gh1)` is **not symbolically equivalent**
to Bernoulli under any substitution (it's the *answer*, via v1=0/h2=0 + solve),
so no `{var:expr}` mapping resolves it.

**Meta-lesson (reinforces `feedback_concrete_basis_before_hypothesis`):** the
deterministic gate passed but live failed because **the LLM parse is
non-deterministic** — the graph shape (and which equation form the USES edge
lands on) changes every run.

---

## Part 4 — The pipeline order (where the loss happens)

The §6.4 Done chain (`apollo/handlers/done_grading.py::run_graph_simulation`),
in order. **Bisimilarity and all scores run LAST, on the already-cleansed graph:**

1. **KG built** (parser, during chat → Neo4j). Minor cleanse here: the store
   rejects type-invalid edges (`EDGE_ALLOWED_PAIRS`).
2. `validate_student_graph` — a 422 GATE, not a transform.
3. **`resolve_attempt`** — *cleanser #1 (identity)*: each node → `resolved_key`
   or `unresolved`. (`apollo/resolution/resolver.py`)
4. `write_resolution` — persist to Neo4j.
5. **`build_student_canonical` (S_norm) + `build_reference_canonical` (R_norm)**
   — *cleanser #2 (structure)*: merge nodes by `resolved_key`, normalize edges,
   **DROP every edge with an unresolved endpoint.** ← the solved-form USES edge
   dies HERE. (`apollo/graph_compare/canonical.py`)
6. **`grade_attempt`** — NOW scores run (bisimilarity, node/edge_coverage, usage,
   …) over S_norm vs R_norm. (`apollo/graph_compare/core.py`)
7. audit → abstention → persist runs/findings → rubric/calibration.

The scorer is blameless — it faithfully scores whatever survives the two
cleansers. **The fix must live in a cleanser (step 3 or 5), not the scorer.**

---

## Part 5 — Architectural understanding (the real problem)

**Resolution decides node identity in ISOLATION, before the edge context that
would inform it exists — then discards what it can't name.** In attempt 8 the
LLM adjudicator *correctly* judged `v2=sqrt(2gh1)` is not literally Bernoulli's
equation, and dropped it — taking its USES edge with it.

**`unresolved` conflates three very different cases:**
1. genuinely wrong / nonsense,
2. **correct but out of scope** for this problem (e.g. continuity in a
   Bernoulli-only problem — the closed candidate set is *this problem's reference
   entities + course misconceptions* ONLY; correct off-topic course entities are
   NOT candidates → unresolved; note misconceptions ARE course-scoped),
3. a **derived/solved form of an in-scope entity** (the solved Bernoulli).

**User decision:** cases 1 and 2 are FINE to leave unresolved. **Case 3 is NOT —
a derived form of an in-scope equation should resolve to that equation.**

**Key realization:** case 3 alone fixes attempt 8. `build_student_canonical`
merges nodes by `resolved_key`. If the solved form ALSO resolved to
`eq.bernoulli`, it merges with the full-form node into ONE `eq.bernoulli` node,
and the existing USES edge normalizes to
`proc.plan_set_v1_zero_and_solve_bernoulli -USES-> eq.bernoulli` → matches the
reference → `edge_coverage`/`usage` go positive. **No parser change needed.**

The cleanest signal that the solved form "is" Bernoulli is NOT symbolic
equivalence (fails) — it's **context**: it sits in the USES slot of a proc that
resolved to a `…solve_bernoulli` step whose reference USES-target is
`eq.bernoulli`.

---

## Part 6 — Candidate fix directions

**REJECTED — Fix F (parser wires USES to the governing equation):** non-generic
(a Bernoulli-shaped assumption; other classes legitimately USES derived
equations) AND it fights the non-deterministic parser LLM we deliberately
delegated edge-attachment to.

**Direction A — Resolve case-3 (derived/solved forms) to the governing entity.**
Then merge + edge-normalization do the rest (proven above). Open sub-question:
the *rule*. Candidates — (i) **context/structural**: the proc the equation is
USES-attached to resolved to a step whose reference target is X → resolve the
equation to X; (ii) **symbolic-derivation**: allow `solve()`/substitution of
given values, not just sign-exact equivalence; (iii) **LLM on the assembled
subgraph** (much more reliable than judging a node in isolation, which is the
call that failed). Risk: over-crediting — needs a principled guardrail.

**Direction B — Two-layer redesign (user-favored framing).** Stop discarding
unresolved nodes: keep EVERY student node as first-class (a *standalone* identity
when it matches no reference entity) so its edges survive cleanser #2; move
reference-matching into a separate, **context-aware/tolerant** matching layer
that decides per-edge whether the student's structural claim matches the
reference's. Reshapes ONE boundary (`build_student_canonical` ↔ `grade_attempt`)
— both layers already exist. Risk: the tolerance dial (too loose → credit wrong
equations; too tight → today's brittleness). This also cleanly separates
cases 1/2/3 instead of lumping them.

**Coupled, separate issue — abstention.** Even a fixed USES edge may still
abstain (`unresolved_rate_above_threshold`; 3/7 unresolved in attempt 8 — the
solved form, the "P1=P2" simp, the "open to atmosphere" condition). Note also
non-equation nodes (simplification/condition) resolve poorly: the symbolic tier
is equation-only and reference simps/conditions carry no aliases, so they only
resolve via `exact` or the one LLM call. Fixing `edge_coverage` and stopping
abstention likely need to happen together.

---

## Part 7 — THE NEXT STEP (paused mid-brainstorm)

**Before committing to A or B, test a NON-fluid subject** — we are heavily
fluid-biased and this bug is "real but niche." Plan (user's words): find a short
textbook, scrape questions, author the reference answer-KGs ourselves, generate
student answers, and run the FULL pipeline from scratch on a fresh subject.

Constraints already established for that effort:
- **Apollo is fundamentally an equation + SymPy graph engine** → the finance
  topic must be QUANTITATIVE (Time-Value-of-Money, bond pricing, NPV/IRR, CAPM),
  not qualitative.
- **Use an open-licensed source** — OpenStax "Principles of Finance" (CC BY) —
  NOT a copyrighted textbook scrape. Author reference KGs + student scenarios.
- A new subject slots into `apollo/subjects/finance/concepts/<concept>/problems/
  *.json` + `misconceptions.json` + concept metadata (same schema as fluids;
  seed walks `apollo/subjects/<subject>/concepts/<concept>/…`).
- `apollo_grade_probe.py` is Bernoulli-hardcoded (`SCENARIOS`) — generalize it
  (or add finance scenarios) for the new subject.
- Execution stays USER-RUN (classifier blocks agent DB writes).

**Open brainstorm question (unanswered — the run got interrupted to write this
handoff):** what is the PRIMARY goal of the finance run — (a) generality sanity
check (does the engine work at all on non-fluids), (b) reproduce the case-3 bug
(is it general or fluid-specific), or (c) hunt new failure modes? This decides
how to design the problems and what to measure.

---

## Pointers

- **Fix code:** `apollo/graph_compare/problem_inputs.py` (`_collect_symbolic_mappings`).
- **Tests:** `apollo/graph_compare/tests/test_derived_equation_resolution.py`,
  `test_derived_equation_portability.py`, `test_problem_inputs.py`.
- **Diagnostics:** `scripts/run_local_probe.py`, `scripts/inspect_attempt.py`.
- **Pipeline:** `apollo/handlers/done_grading.py` (the §6.4 chain),
  `apollo/resolution/resolver.py` + `apollo/resolution/tiers.py` (resolution +
  symbolic tier + the LLM adjudication that judged the solved form unresolved),
  `apollo/graph_compare/canonical.py` (S_norm/R_norm; the edge-drop),
  `apollo/graph_compare/core.py` (`grade_attempt` + bisimilarity).
- **Candidate set scope:** `apollo/resolution/candidates.py`
  (`candidates_from_reference_solution` + `candidates_from_misconceptions`).
- **Owner doc:** `docs/architecture/apollo.md`. **Prior handoffs:**
  `docs/APOLLO-GRADING-EDGE-RESOLUTION-HANDOFF.md`,
  `docs/APOLLO-GRAPH-GRADING-HANDOFF.md`.
- **Branch note:** the fix is UNCOMMITTED; current branch
  `fix/apollo-provisioning-prompts` is mis-scoped — cut a fresh branch off
  `staging` before committing.
- **Live data:** strong=attempt_id 8, weak=attempt_id 9 (probe tag `.resfix2`,
  users `apollo.probe.{strong,weak}.resfix2@example.com`).
