# WU-4A split proposal — Apollo graph-simulation grading core

**Date:** 2026-06-17
**Author:** scoping pass (autonomous stacked-PR build)
**Spec:** `docs/superpowers/specs/2026-06-10-apollo-kg-learner-model-architecture-decision.md` §6 (grading core), §2 (grades tables), §3 (events/confidence caps), §5 (resolver)
**Status:** PROPOSAL ONLY — no code/tests/source touched. This document divides the XL unit WU-4A into sequential sub-units with clean seams; the per-sub-unit TDD plan is a separate later step.

---

## 0. What WU-4A is (and the sibling boundary it must not cross)

WU-4A is the **graph_compare core**: take a frozen student graph + an authored
reference graph, normalize both into canonical space, and compute the §6.2
scores deterministically. It is the callable core only — it does NOT
re-orchestrate `done.py` (that is WU-4C) and does NOT own the audit/abstention/
persistence/fixtures layer (that is WU-4B).

Phase-4 boundary (from the build roadmap, restated and held):

- **WU-4A (this unit)** = validator, canonical R_norm/S_norm builder,
  coverage (max over declared paths), soundness (contradictions only),
  node/edge sub-scores, and bisimilarity = `harmonic_mean(soundness, coverage)`.
- **WU-4B (later)** = *near*-bisimilarity refinement, transcript auditor,
  abstention gates, runs+findings persistence to Postgres, §6.11 adversarial
  fixtures as an executable corpus, finding→event conversion (§6.5 table).
- **WU-4C (later)** = `done.py` re-orchestration, rubric mapping
  (findings/sub-scores → `compute_rubric` input), constrained diagnostics in
  shadow, calibration-gate wiring.

### Where the 4A/4B line falls (decided, with spec citations)

1. **`bisimilarity_score = harmonic_mean(soundness, coverage)` is WU-4A.**
   §6.1 defines it as a pure function of the two sub-scores WU-4A already
   produces, with binding degenerate handling (`harmonic_mean(a,b)=0` when
   `a+b=0`, never NaN — §6.1). It is a one-function combination of 4A's own
   outputs; splitting it out would create a sub-unit that imports 4A only to
   call `harmonic_mean`. It lands in 4A.
   The §6.1 terminology note is explicit that "**near-bisimilarity** names the
   summary score" and "simulation names the conceptual frame" — i.e. the v1
   summary score IS the harmonic mean. There is no separate "near-bisimilarity
   *refinement*" algorithm specified for v1; the phrase distinguishes the
   weighted-asymmetric-coverage primitive from a formal transition-system
   bisimulation. **Conclusion: the full bisimilarity score is WU-4A; WU-4B owns
   no score-math refinement, only audit/abstention/persistence/events.**

2. **Transcript audit, abstention gates, finding→event conversion, persistence
   of runs/findings, and the §6.11 fixtures are WU-4B.** §6.4 steps 12, 14–17
   and §6.5/§6.6/§6.11 are all downstream of the scores. WU-4A emits *findings
   in memory* (covered/missing/contradiction/unsupported_extra/unresolved/
   matched_edge/missing_edge/alternative_path — the §2 `finding_kind` enum) but
   does NOT decide a `missing` event (that REQUIRES the negative transcript
   audit, §6.2/§6.5), does NOT apply abstention, and does NOT write to Postgres.

3. **Pipeline ownership (§6.4):** WU-4A owns steps **4, 8, 9, 10, 11, 13** (raw
   validation, build S_norm, build R_norm per path, coverage simulation,
   soundness simulation, bisimilarity). It consumes step 5 (resolution — already
   built, WU-3C2) as a dependency. Steps 1–3, 6–7, 12, 14–18 belong to other
   units (freeze/load are handler concerns reused by WU-4C; 6–7 reference
   normalization-as-validation reuses WU-3B's `validate_reference_graph` and
   WU-3C2's `write_resolution`; 12/14–18 are WU-4B/4C).

---

## 1. Ground truth discovered (load-bearing facts that shape the split)

These were verified against the real code/migrations, not assumed:

- **Migration 026 (`database/migrations/026_apollo_learner_model.sql`) ALREADY
  created both grades tables** (`apollo_graph_comparison_runs`,
  `apollo_graph_comparison_findings`) and the `learner_update_pending` column,
  **and the ORM models already exist** (`apollo/persistence/models.py`
  `GraphComparisonRun` line 479, `GraphComparisonFinding` line 527). So **WU-4A
  needs NO migration** — the next free number (028) is reserved for a different
  need if one surfaces (see §4 risk on `symbolic_mappings`). The spec's "exact
  DDL in the plan" (§2) is already satisfied. *The prompt's hypothesis that 4A
  might need migration 028 for the grades table is FALSE — it exists.*

- **Column-name reality vs spec §2 prose.** §2 prose lists a
  `comparison_confidence` column; the **actual table/ORM has
  `normalization_confidence` (NOT NULL)** and the sub-score columns
  (`node_coverage_score`, `edge_coverage_score`, `scoping_score`, `usage_score`,
  `procedure_order_score`, `dependency_score`, `contradiction_score`, all
  nullable). §3 reconciles this: `grader_confidence = normalization_confidence ×
  comparison_confidence`, and "**`comparison_confidence` is defined as 1.0 in
  v1**". So WU-4A computes a `comparison_confidence` *value* (1.0 in v1) used in
  the event damper later, but the **persisted column is `normalization_confidence`**.
  Naming must follow the existing schema — do not rename the column.

- **The resolver is built and standalone (WU-3C2).**
  `from apollo.resolution import resolve_attempt, build_candidate_set,
  candidates_from_reference_solution, candidates_from_misconceptions, Candidate,
  ResolvedNode, ResolutionResult, METHOD_CONFIDENCE_CAP`. Signature:
  `resolve_attempt(student_graph: KGGraph, candidates: tuple[Candidate, ...], *,
  llm_adjudicator=None, fuzzy_threshold=0.9, symbolic_mappings: dict[str,str] |
  None = None) -> ResolutionResult`. **WU-4A is the caller that must pass the
  per-problem `symbolic_mappings`** (the global `d=2r` default was removed; with
  none supplied the symbolic tier does no substitution).

- **`symbolic_mappings` has NO per-problem home yet.** `problem_01.json` has
  `given_values`, `declared_paths`, `reference_solution[*].content.symbolic`,
  but **no `symbolic_mappings` key** (`grep` confirms only resolver/seed
  reference it). The Bernoulli worked example (§6.9) needs `d = 2r` for "A =
  πr²" → `canon.eq.circular_area`. This is a real seam WU-4A must resolve: a
  per-problem mapping table that the candidate/reference builder reads. Options
  in §4.

- **The reference graph is per-problem and order/structure-rich.** Each
  `reference_solution` step has `id`, `entry_type`
  (equation|condition|simplification|procedure_step|definition), `content`
  (`symbolic` / `applies_when` / `transformation` / `order` / `variables`),
  `depends_on` (the DAG that becomes R_norm's edges), `entity_key`, `step`.
  `declared_paths` is a list of ordered node-id lists (v1: exactly one path).

- **HEADS-UP confirmed (WU-3B):** the deduped `apollo_kg_entities` payload for
  `eq.continuity` is row-order-dependent across problems, but **each problem's
  `reference_solution[*].content.symbolic` is the problem's OWN truth.** The
  R_norm builder MUST read `problem["reference_solution"]` (+ `declared_paths` +
  `depends_on`), NEVER the deduped entity payload. Confirmed: `problem_01.json`
  carries its own `eq.continuity` symbolic `"rho*A1*v1 - rho*A2*v2"`.

- **`validate_reference_graph(problem) -> ReferenceGraphValidation`** already
  exists (`apollo/persistence/learner_model_seed.py:307`) and is the executable
  §6.1 contract (entity links present, declared_paths non-empty, paths
  reference real nodes, every node covered by ≥1 path). **WU-4A's `validator.py`
  reuses this for the reference side** and adds only the *raw student graph*
  grammar validation (the §6.3 `validator.py` row).

- **Graph primitives exist.** `KGGraph` (`apollo/ontology/graph.py`) gives
  `by_type`, `outgoing/incoming`, `neighbors`, `topological_order(edge_type)`
  (Kahn, raises on cycle), `precedes_chain`. `EDGE_ALLOWED_PAIRS`
  (`apollo/ontology/edges.py`) is the endpoint-pair table the §6.3 validator
  reuses verbatim. `type_compatible` (`apollo/resolution/structural.py`) is the
  hard type constraint.

- **`done.py` (WU-4C's target) currently calls `compute_coverage` (LLM-based,
  `apollo/overseer/coverage.py`) + `compute_rubric`
  (`apollo/overseer/rubric.py`).** §6.7 says `compute_coverage` is NOT deleted —
  it runs in **shadow**. WU-4A must NOT touch `done.py`, `coverage.py`, or
  `rubric.py`; it only provides a new callable the later units wire in.

- **Existing structure to imitate:** the `apollo/resolution/` package is the
  template for `apollo/graph_compare/` — many small pure modules, one `__init__`
  re-exporting the public seam, frozen dataclasses, pure/DB-free core with a
  separate persistence sibling. This is exactly the §6.3 module list.

---

## 2. Recommendation

**Split WU-4A into TWO sub-units (WU-4A1, WU-4A2). No third sub-unit.**

Rationale: the natural seam is **canonicalization (build the two canonical
graphs) vs. scoring (compare them)**. Each side is independently testable at
≥95% patch coverage, each is roughly an "L", and the seam is a single immutable
data structure (`CanonicalGraph`) plus the resolution result. A three-way split
(validator / canonical / scoring) was considered and rejected: the validator is
small (raw-student grammar + reuse of the existing reference validator) and
binds so tightly to "what can be built into a canonical graph" that it belongs
with the builder; isolating it would create a sub-unit too thin to justify its
own branch + CI cycle, and would force WU-4A2 to re-establish builder context.

- **WU-4A1 — validation + canonical graph construction** (validator, candidate
  wiring, S_norm/R_norm builders, the `symbolic_mappings` per-problem seam).
  Owns everything up to and including the two `CanonicalGraph` objects.
- **WU-4A2 — simulation + scores** (coverage max-over-paths, soundness
  contradictions-only, node/edge/scoping/usage/procedure-order/dependency/
  contradiction sub-scores, bisimilarity harmonic mean, in-memory findings, and
  the single `GradeResult` the whole core returns). Owns everything from the two
  canonical graphs to the scores.

Why this seam and not "coverage vs soundness": coverage and soundness share the
entire canonical-graph substrate and the per-path machinery; splitting them
would duplicate setup and leave neither independently meaningful (a coverage
score with no soundness score is not a gradeable result). Build-vs-compare is
the only seam where each side is a complete, testable artifact.

---

## 3. Sub-units

### WU-4A1 — Validation + canonical graph construction

**One-line:** Validate the raw student graph and the reference graph, then build
`S_norm` and `R_norm` (canonical, per-path) from resolution + the per-problem
reference solution.

**SEAM (owns vs WU-4A2):** WU-4A1 owns everything that turns *(frozen student
graph, resolution result, problem dict)* into two immutable `CanonicalGraph`
objects + the per-path R_norm views. It does NOT compute any score. The handoff
artifact is the `CanonicalGraph` dataclass (+ a `ReferenceGraph` carrying the
declared-path structure). WU-4A2 consumes those and never re-reads Neo4j or the
raw problem JSON.

**Scope — files to create (`apollo/graph_compare/`):**

- `apollo/graph_compare/__init__.py` — package seam; re-exports the public API
  as it grows (mirrors `apollo/resolution/__init__.py`).
- `apollo/graph_compare/validator.py` — the §6.3 `validator.py` row for the
  **raw student graph only**: node/edge types, endpoint pairs (reuse
  `EDGE_ALLOWED_PAIRS`), scoping consistency (`attempt_id` uniformity), no
  invalid PRECEDES cycles (reuse `KGGraph.topological_order` which already
  raises on cycle), required fields. For the **reference side it reuses
  `validate_reference_graph`** from `learner_model_seed.py` (do NOT reimplement
  §6.1). Raises a named `ReferenceGraphInvalidError` (reference failure blocks
  grading, §6.6) and a distinct `StudentGraphInvalidError` for raw-student
  grammar violations.
- `apollo/graph_compare/canonical.py` — `CanonicalNode` / `CanonicalGraph` /
  `ReferenceGraph` frozen dataclasses + `build_student_canonical(student_graph,
  resolution) -> CanonicalGraph` and `build_reference_canonical(problem) ->
  ReferenceGraph`. Merges student nodes resolving to the same target
  (preserving source raw node-ids + evidence spans), normalizes edges after
  endpoint resolution, **drops unresolved edges from comparison, retains
  unresolved nodes as findings-input** (§6.3 `canonical_graph.py`). R_norm is
  built from `problem["reference_solution"]` + `depends_on` + `declared_paths`
  — **never the deduped entity payload** (the WU-3B heads-up).
- `apollo/graph_compare/problem_inputs.py` (small) — the `symbolic_mappings`
  per-problem seam + candidate-set assembly: reads the problem's reference
  solution + course misconceptions and builds the closed candidate set via the
  existing `build_candidate_set` / `candidates_from_reference_solution` /
  `candidates_from_misconceptions`, and resolves the per-problem
  `symbolic_mappings` table to pass into `resolve_attempt`. This is where the
  `d = 2r` seam is decided (see §4 risk).

**Public API exposed for WU-4A2 / 4B / 4C:**

```python
# apollo/graph_compare (after WU-4A1)
from apollo.graph_compare import (
    CanonicalGraph, CanonicalNode, ReferenceGraph,
    build_student_canonical, build_reference_canonical,
    validate_student_graph,        # reference validation reuses validate_reference_graph
    build_problem_candidates,      # candidate set + symbolic_mappings for resolve_attempt
    ReferenceGraphInvalidError, StudentGraphInvalidError,
)
```

**Dependencies:** WU-3C2 resolver (`apollo.resolution`), WU-3B
`validate_reference_graph`, `apollo.ontology` (KGGraph/Node/Edge/
EDGE_ALLOWED_PAIRS), the problem JSON shape. No DB, no Neo4j, no LLM in the pure
core (the resolver is injected/called by the caller; 4A1's builders take an
already-computed `ResolutionResult`).

**Migration?** **No.** Tables + ORM exist (migration 026). The only schema-shaped
question is `symbolic_mappings`' home (§4) — preferred resolution is a JSON key
on the problem file (no migration), with migration 028 as the fallback only if a
DB-backed per-problem mapping table is chosen.

**Real-infra tests?** **None required for 4A1.** The builders are pure over
in-memory `KGGraph` + `ResolutionResult` + problem dict, like the resolver
suite. Reference validation reuses an already-real-infra-tested function. (A
thin real-Neo4j read test belongs to whoever wires `store.read_graph` into the
core — that is the 4A2 orchestrator entrypoint or WU-4C, not the pure builder.)

**Key behavioral tests:**
- Two student nodes resolving to the same target merge into one
  `CanonicalNode` carrying both raw node-ids + both evidence spans (the
  "density stays the same" + "constant density" case, §6.3).
- Unresolved student node retained as findings-input, NOT dropped; its incident
  edges dropped from comparison.
- R_norm built from the problem's OWN `reference_solution[*].content.symbolic`
  (regression guard for the WU-3B dedup heads-up): construct two problems that
  both mint `eq.continuity` with different symbolic forms; assert each R_norm
  carries its own.
- `declared_paths` → per-path R_norm views; multi-path problem yields multiple
  views (schema supports it even though v1 authors one).
- Raw student graph with an illegal edge endpoint (`equation SCOPES condition`)
  → `StudentGraphInvalidError`; reference missing an entity link →
  `ReferenceGraphInvalidError` (delegated to `validate_reference_graph`).
- `symbolic_mappings` plumbed: a problem declaring `{"d": "2*r"}` makes
  "A = πr²" resolvable to circular-area (via the resolver's symbolic tier).
- **§6.11 fixtures that land in 4A1:** none as full executable fixtures (those
  are WU-4B's corpus), but the *building-block* cases above directly
  pre-stage "nonstandard notation / heavy paraphrase → resolved", "reference
  omits a valid assumption the student states → unsupported_extra" (the extra
  is a canonical node with no R match — produced here, scored in 4A2), and
  "vague pronouns → unresolved" (retained here).

---

### WU-4A2 — Simulation + scores

**One-line:** Compare `S_norm` against `R_norm` to produce coverage (max over
declared paths), soundness (contradictions only), the seven sub-scores,
bisimilarity, and the in-memory findings — the callable grading core WU-4C wires
into `done.py`.

**SEAM (owns vs WU-4B):** WU-4A2 owns the deterministic score-math and emits
**in-memory findings** (the §2 `finding_kind` set). It does NOT run the
transcript audit, does NOT decide a `missing` *event*, does NOT apply abstention
gates, does NOT persist to Postgres, and does NOT convert findings→events
(§6.5). The handoff artifact is a single immutable `GradeResult` (scores +
findings + the v1 `comparison_confidence = 1.0` + a `comparison_version`
constant). WU-4B consumes `GradeResult.findings` to run the audit, gate, persist
the run/findings rows, and produce events.

**Scope — files to create (`apollo/graph_compare/`):**

- `apollo/graph_compare/coverage.py` — `R ⊑ S` per declared path; coverage =
  **max over paths** of node-coverage (§6.2 binding). A student on a valid
  alternative path is graded against the path they took, never punished against
  the authored one.
- `apollo/graph_compare/soundness.py` — `S ⊑ R`, **contradictions only**:
  `soundness_score = 1 − penalty(contradictions)`, contradiction = resolution
  to `canon.misc.*` (negative knowledge), detected via the resolver's
  `Candidate.is_misconception`/`opposes_key` and the misconception competition
  already done at resolve-time. **Unsupported extras and unresolved nodes carry
  ZERO soundness penalty** (§6.2).
- `apollo/graph_compare/scores.py` — the seven sub-scores: `node_coverage`,
  `edge_coverage`, `scoping`, `usage`, `procedure_order` (penalize only
  *inversions* of true reference PRECEDES, never absence — §6.2),
  `dependency` (lowest weight; loose any→any DEPENDS_ON grammar), `contradiction`.
  Edges are **diagnostic-grade in v1** — sub-scores only, never events; `explicit`
  outweighs `inferred` (provenance already on `Edge`).
- `apollo/graph_compare/bisimilarity.py` — `harmonic_mean(soundness, coverage)`
  with the binding degenerate handling (`= 0` when `a+b = 0`, never NaN; empty
  student graph → coverage 0, soundness 1, bisimilarity 0; §6.1).
- `apollo/graph_compare/findings.py` — the in-memory `Finding` dataclass
  (covered_node / missing_node / matched_edge / missing_edge /
  unsupported_extra / contradiction / unresolved / alternative_path) and the
  reducers that emit them from the simulation passes. **No event conversion
  here** (§6.5 table is WU-4B).
- `apollo/graph_compare/core.py` — `grade_attempt(student_canonical,
  reference_graph) -> GradeResult` (the §6.4 steps 10/11/13 orchestration over
  pure inputs) + `GradeResult` frozen dataclass. This is the single public
  callable WU-4C imports.

**Public API exposed for WU-4B / 4C:**

```python
from apollo.graph_compare import (
    grade_attempt,                 # CanonicalGraph + ReferenceGraph -> GradeResult
    GradeResult, Finding, FindingKind,
    COMPARISON_VERSION,            # the comparison_version constant for the runs row
)
# GradeResult fields map 1:1 to apollo_graph_comparison_runs columns:
#   coverage_score, soundness_score, bisimilarity_score,
#   node_coverage_score, edge_coverage_score, scoping_score, usage_score,
#   procedure_order_score, dependency_score, contradiction_score,
#   comparison_confidence (==1.0 v1), findings: tuple[Finding, ...]
# normalization_confidence is supplied by the caller from the resolution result
# (method-cap confidences); GradeResult carries the comparison_confidence only.
```

**Dependencies:** stacks on WU-4A1's branch (consumes `CanonicalGraph` /
`ReferenceGraph`). No new external deps.

**Migration?** **No** (see WU-4A1).

**Real-infra tests?** **None required for the pure core.** All inputs are
in-memory dataclasses. Persistence of `GradeResult` → `apollo_graph_comparison_runs`/
`_findings` is **WU-4B** (that is where real-PG round-trip tests for the grades
tables live). If WU-4A2 chooses to expose a thin `read_graph`→`grade` integration
entrypoint for smoke purposes, that one test uses real-Neo4j; otherwise defer
the read wiring to WU-4C.

**Key behavioral tests (incl. §6.11 split):**
- Coverage = max over paths: a valid alternative path scores via that path with
  zero false missings (§6.11 "valid alternative solution path").
- Soundness contradictions-only: "reference omits a valid assumption the
  student states" → `unsupported_extra`, **zero soundness penalty** (§6.11);
  "misconception not present in `canon.misc.*`" → `unsupported_extra`, NOT
  contradiction (§6.11, honest non-detection).
- Contradiction: a node resolving to `canon.misc.*` → `contradiction` finding,
  soundness penalized; "wrong final answer, mostly correct concepts" → covered
  nodes + one contradiction (§6.11).
- Degenerate: empty student graph → coverage 0 / soundness 1 / bisimilarity 0,
  no NaN (§6.1 binding).
- `procedure_order_score` penalizes a true PRECEDES inversion but NOT a missing
  stated order (§6.2).
- Sub-scores never produce events (assert findings carry no event kinds);
  edge gaps are diagnostic flags only (the §6.5 `partial` variant stays
  calibration-gated/OFF).
- "correct final answer, thin explanation" → low coverage, high soundness, no
  misconception (§6.11) — a pure-score fixture (no audit needed).

**§6.11 fixtures explicitly DEFERRED to WU-4B** (need audit/abstention/events,
which 4A does not own): "parser misses a key sentence → transcript audit finds
the span" (audit), "conflicting statements first/last → corrected/misconception"
(decision-table turn order, §6.5), "high-unresolved-rate → abstention" (gate).
WU-4A2 produces the *findings* those fixtures start from; WU-4B asserts the
*events*.

---

## 4. Risks, uncertainties, and spec ambiguities to resolve

1. **HIGHEST: `symbolic_mappings` has no per-problem home.** The resolver
   requires it (no global `d=2r` default), the Bernoulli worked example (§6.9)
   needs `d = 2r`, but `problem_01.json` has no such key and nothing in the
   codebase declares one. **Resolve in WU-4A1.** Preferred: add a
   `symbolic_mappings` JSON object to the problem file (e.g. `{"d": "2*r"}`),
   read by `build_problem_candidates` — **no migration, problem files are
   already hand-authored and source-controlled.** Fallback (only if a
   DB-queryable mapping is needed for the §8 minting workflow): migration 028
   adds a per-problem mapping table. **Decision needed before WU-4A1 planning.**
   Note: adding the key to `problem_*.json` may interact with the WU-3B
   seeder's disk-rewrite side effect (flagged in the WU-3B review NIT) — verify
   the seeder round-trips the new key.

2. **`comparison_confidence` (spec §2 prose) vs `normalization_confidence`
   (actual column).** Reconciled in §3 (`grader_confidence =
   normalization_confidence × comparison_confidence`, `comparison_confidence =
   1.0` in v1). **WU-4A2's `GradeResult` carries `comparison_confidence`
   (the score-math's own confidence, 1.0 in v1, <1.0 only on ambiguous global
   assignment / path tie); the persisted `normalization_confidence` column is
   supplied by the persistence layer (WU-4B) from the resolution method-caps.**
   Do NOT rename the column. Document this mapping in the owner doc.

3. **`done.py` / `architecture/apollo.md` drift.** WU-4A adds a new package but
   does not yet wire it (WU-4C does). The drift-prevention contract still
   requires registering `apollo/graph_compare/` in the owner doc
   (`docs/architecture/apollo.md`, `owns:` globs) when the package lands, even
   though `done.py` is untouched. Confirm the doc's `owns:` covers
   `apollo/graph_compare/**` in WU-4A1.

4. **Findings carry Neo4j node-ids the spec calls "best-effort" (§7).** The
   `student_node_ids`/`reference_node_ids` on findings are durable for the
   in-memory `GradeResult`, but `evidence_spans` are the durable provenance
   (node-ids dangle after pruning). WU-4A only populates them; persistence
   semantics are WU-4B. No action for 4A beyond carrying both.

5. **Where `store.read_graph` is called.** §6.4 step 2 loads the frozen student
   graph from Neo4j. WU-4A's pure builders take an already-loaded `KGGraph`, so
   the read call lives in the orchestrator (WU-4C) or a thin 4A2 entrypoint.
   Keep the pure core DB-free (matches the resolver's design and keeps the 95%
   patch gate achievable without containers). **Recommend: read wiring is WU-4C;
   4A exposes only pure callables.**

6. **`alternative_path` finding semantics in a single-path v1.** §2 lists
   `alternative_path` as a finding kind, but v1 authors one path (§6.2). WU-4A2
   must define it now (schema supports multi-path from day one) even though the
   bernoulli fixtures exercise the degenerate single-path case — test with a
   synthetic two-path problem so the max-over-paths branch is covered for the
   95% gate.

---

## 5. Build order (stacked branches)

1. **WU-4A1** branches off the current Phase-4 base (the WU-3D/WU-3C tip).
   Lands `validator.py`, `canonical.py`, `problem_inputs.py`, `__init__.py` +
   the `symbolic_mappings` decision. Independently green at ≥95% patch coverage
   (pure unit tests, no containers).
2. **WU-4A2** branches off WU-4A1. Lands `coverage.py`, `soundness.py`,
   `scores.py`, `bisimilarity.py`, `findings.py`, `core.py`. Independently green
   at ≥95% patch coverage (pure unit tests over 4A1's dataclasses).
3. WU-4B branches off WU-4A2 (audit/abstention/persistence/events/fixtures).
4. WU-4C branches off WU-4B (`done.py` re-orchestration, rubric mapping, shadow
   diagnostics).

Each sub-unit is one TDD-executor pass (an "L"): WU-4A1 ≈ 4 new modules + ~6
test files; WU-4A2 ≈ 6 new modules + ~8 test files. Neither is another XL.

---

## 6. Doc/coverage obligations (per the project contracts)

- Patch coverage ≥95% on changed lines (`diff-cover` vs `origin/staging`),
  enforced per sub-unit. Pure modules make this achievable without real-infra.
- Reconcile `docs/architecture/apollo.md` in the same commit each sub-unit lands
  (register `apollo/graph_compare/**`, bump `last_verified`).
- No migration in WU-4A (026 already shipped the tables + ORM). If risk #1
  forces a DB-backed `symbolic_mappings` table, that is migration 028 and a
  documented, local-only, file-then-CI-rehearsed change — but the JSON-key path
  avoids it.
