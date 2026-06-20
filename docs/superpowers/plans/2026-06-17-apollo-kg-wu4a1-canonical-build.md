# Plan: WU-4A1 — graph_compare validation + canonical S_norm/R_norm construction

**Goal:** Build the two canonical graphs (`S_norm`, `R_norm`) and validate the raw student + reference graphs — and STOP before any score (scores are WU-4A2).
**Architecture:** New pure package `apollo/graph_compare/` (mirrors `apollo/resolution/`). Consumes WU-3C2 resolver, WU-3B `validate_reference_graph`, `apollo.ontology`. No DB, no Neo4j, no LLM in the core.
**Tech stack:** Python 3.12, FastAPI backend, pytest + pytest-asyncio (`asyncio_mode=auto`), `dataclass(frozen=True)` DTOs, diff-cover patch gate vs `feat/apollo-kg-wu3d-runtime-cutover`.

---
provides:
  - apollo.graph_compare package seam (`__init__.py`)
  - CanonicalNode, CanonicalGraph, ReferenceGraph (frozen dataclasses)
  - build_student_canonical(student_graph, resolution) -> CanonicalGraph
  - build_reference_canonical(problem) -> ReferenceGraph
  - validate_student_graph(student_graph) ; reference side reuses validate_reference_graph
  - build_problem_candidates(problem, misconceptions, *, canon_key_by_canonical_key) -> ProblemInputs (candidates + symbolic_mappings)
  - StudentGraphInvalidError, ReferenceGraphInvalidError
consumes:
  - apollo.resolution (resolve_attempt, build_candidate_set, candidates_from_reference_solution, candidates_from_misconceptions, Candidate, ResolutionResult, ResolvedNode)
  - apollo.persistence.learner_model_seed.validate_reference_graph (REUSE for reference side)
  - apollo.ontology (KGGraph, Node, Edge, EdgeType, EDGE_ALLOWED_PAIRS, NodeType)
  - problem dict shape (reference_solution[*].content.symbolic + depends_on + declared_paths + NEW symbolic_mappings key)
depends_on:
  - WU-3C2 resolver (already landed) ; WU-3B seeder + validate_reference_graph (already landed) ; migration 026 grades tables/ORM (already landed)
---

## Overview

WU-4A1 is the build half of the §6 grading core. It turns `(frozen student KGGraph, ResolutionResult, problem dict)` into two immutable canonical graphs plus per-path reference views, and validates both raw graphs first. It computes NO scores, runs NO simulation, persists nothing, and calls neither Neo4j nor any LLM. The handoff artifact to WU-4A2 is the `CanonicalGraph` + `ReferenceGraph` pair.

This is a **NestJS-style layered plan adapted to Python/FastAPI**. There is no controller/HTTP layer in this unit (the Done route that calls graph_compare is WU-4C). The "layers" here are: schema seam (no migration) → DTOs (frozen dataclasses) → pure builders (the "service" layer) → package seam (`__init__`) → owner-doc reconcile. There is no repository or endpoint layer because WU-4A1 is a pure in-memory transform; this is justified explicitly in Risks.

Pipeline coverage (§6.4): WU-4A1 owns **step 4** (validate raw student graph), **step 8** (build S_norm), **step 9** (build R_norm per declared path), and the candidate/symbolic-mappings assembly that feeds **step 5** (resolution — itself already built in WU-3C2 and invoked by the caller, not by 4A1's builders). Steps 10–13 (scores) are WU-4A2; steps 12, 14–18 are WU-4B/4C.

## Prior art (sibling modules)

The `apollo/resolution/` package is the exact template: many small pure modules, one `__init__` re-exporting the public seam, frozen dataclasses, pure/DB-free core, co-located `tests/` package.

- **Package seam:** `apollo/resolution/__init__.py:14-35` — `from apollo.resolution.<mod> import ...` then a flat `__all__`. Mirror verbatim for `apollo/graph_compare/__init__.py`.
- **Frozen DTO + immutability:** `apollo/resolution/result.py:21-63` (`ResolvedNode`, `ResolutionResult` — `@dataclass(frozen=True)`, tuple fields, derived helper methods like `resolved_edges()`). `apollo/persistence/learner_model_seed.py:41-65` (`EntitySpec`, `ReferenceGraphValidation` — frozen, `Mapping`/tuple fields).
- **Named-error convention (NO-FALLBACK):** `apollo/persistence/learner_model_seed.py:67-70` (`SeedError(RuntimeError)`); the resolver's `ResolutionUnavailableError`/`ResolutionInvalidOutputError` (in `apollo/errors.py`, documented `apollo.md:119`). New errors `StudentGraphInvalidError`/`ReferenceGraphInvalidError` follow this — named exceptions carrying structured reason fields, raised loudly, never swallowed.
- **Test conventions:** `apollo/resolution/tests/test_resolver.py` and `test_candidates.py` — co-located `apollo/<pkg>/tests/test_*.py`, pure helpers `_cand(...)`/`_node(...)` building `Candidate`/`Node`, real bernoulli JSON loaded from disk via `Path(__file__).resolve().parents[2] / "subjects" / ...`, LLM mocked by a deterministic `_stub(request)` callable injected as `llm_adjudicator` (no live OpenAI). pytest config: `testpaths = tests apollo`, `asyncio_mode = auto`, `--strict-markers`, markers `unit`/`integration` (`pytest.ini`).
- **DB integration test harness:** `tests/database/test_seed_apollo_learner_model.py:34-90` — `pytestmark = pytest.mark.integration`, per-test fresh pgvector DB built from a content-scoped migration chain + 026, `auth.users`/`aita_search_spaces` stubs. This is the only WU-4A1 test that hits real PG (the seeder round-trip test) and fires the DB gate.

## Structural prep (from neighborhood scan)

Neighborhood scan of the change path (files modified or one ring out):

- `apollo/resolution/__init__.py` — 5 imports, 9 exports. Clean. **Not modified** (consumed only).
- `apollo/persistence/learner_model_seed.py` — 2 imports (stdlib only), ~12 functions. Clean. **Not modified** (`validate_reference_graph` reused).
- `apollo/ontology/{graph,nodes,edges}.py` — graph.py 4 imports, nodes.py 3, edges.py 3. All well under thresholds. **Not modified** (consumed).
- `apollo/subjects/.../problems/problem_01.json` — data file, one additive key. No coupling.
- `scripts/seed_apollo_learner_model.py` — 6 imports of the pure core + 5 ORM models, ~10 functions. Under thresholds. **Verified not modified** — see Decision 4 (round-trips the new key with zero code change).
- New `apollo/graph_compare/*.py` — created fresh, each file targeted < 200 lines.

No file in the change path exceeds CBO > 8 or WMC > 20. No circular-import risk: `graph_compare` imports `resolution`/`ontology`/`persistence` but none of those import `graph_compare` (grepped — `graph_compare` does not yet exist and nothing references it). **Conclusion: neighborhood is clean. No structural prep required.**

## Decision log (binding, from the split proposal)

1. **No migration.** Migration 026 already created `apollo_graph_comparison_runs` / `apollo_graph_comparison_findings` + ORM (`GraphComparisonRun`, `GraphComparisonFinding`). WU-4A1 persists nothing anyway. Persisted column is `normalization_confidence` (NOT `comparison_confidence`) — irrelevant to 4A1 (no persistence). Verified in `apollo.md:38` and the proposal §1.
2. **`symbolic_mappings` lives as a JSON object key on the problem file** (NO migration, NO new table). `build_problem_candidates` reads it, default `{}` when absent. `problem_01.json` gets `{"d": "2*r"}`. The §6.9 `A = pi*r**2` → `eq.circular_area` case needs exactly this (resolver requires it; the global `d=2r` default was removed — `resolver.py:147-151`).
3. **R_norm is built from the problem's OWN `reference_solution[*].content.symbolic` + `depends_on` + `declared_paths`** — NEVER the deduped `apollo_kg_entities` payload. `problem_01.json` owns its `eq.continuity` symbolic `"rho*A1*v1 - rho*A2*v2"` (lines 36-42). The WU-3B dedup heads-up applies only to the seeder's entity rows, not to the per-problem reference solution. Regression test guards this.
4. **The seeder round-trips `symbolic_mappings` with ZERO code change.** Verified: `scripts/seed_apollo_concept_registry.py:_scan_registry` reads the entire problem JSON verbatim into `apollo_concept_problems.payload` (line 179 `json.loads(p.read_text())`), so a new top-level key on disk lands in the DB payload. The learner-model seeder reads payload from the DB and passes it through `annotate_reference_solution` (`learner_model_seed.py:272-299`), which does `dict(problem)` (shallow copy preserving every existing key) and only ADDS `entity_key`/`declared_paths`/`layer1_seeded`. So `symbolic_mappings` survives both seeders and the disk-rewrite side effect (`_annotate_problems` with `write_disk=True` writes the annotated dict, which still carries the key). One DB test asserts this round-trip; no seeder code is edited.
5. **The pure core takes already-computed inputs.** `build_student_canonical` takes a `ResolutionResult` (already produced by the caller's `resolve_attempt`) + the in-memory `KGGraph`; `build_reference_canonical` takes the problem dict. `store.read_graph` wiring and the live `resolve_attempt` call are deferred to WU-4C. This keeps the core pure and the 95% patch gate achievable without containers.
6. **Structural neighborhood corroboration was DEFERRED in WU-3C2** — do not rely on it. The resolver wires type-compat + misconception competition only. 4A1 consumes the `ResolutionResult` as-is.
7. **Unresolved nodes are RETAINED as findings-input; unresolved edges are DROPPED from comparison.** (§6.3 `canonical_graph.py`.) 4A1 produces these as data on `CanonicalGraph`; it emits no `Finding` objects (the `Finding` dataclass is WU-4A2).

## Layered tasks (TDD-ordered)

**TDD law for every code task below: write the listed test(s) FIRST, run them RED, then write the minimal implementation to GREEN. No skip/xfail/empty-assert. Each task ends with the verify command.** Order matters: DTOs before builders, validator before builders that assume valid input, builders before the package `__init__` that re-exports them.

### 1. DB migration
- [ ] **None.** Decision 1: migration 026 already shipped the grades tables + ORM. WU-4A1 persists nothing. No new DDL, no migration file.
- Verify: `git -C ai-ta-backend status database/migrations/` shows no new file.

### 2. symbolic_mappings seam (problem JSON edit + seeder round-trip)
- [ ] Edit `apollo/subjects/fluid_mechanics/concepts/bernoulli_principle/problems/problem_01.json`: add a top-level key `"symbolic_mappings": {"d": "2*r"}` (additive; place it adjacent to `given_values` for readability). The §6.9 worked example needs `d = 2r` so `A = pi*r**2` resolves to `eq.circular_area`.
- [ ] Write the DB round-trip test (Task-12 in the test list) FIRST — it is `tests/database/test_*` and fires the DB gate.
- [ ] Verify the seeder needs no edit (Decision 4). Do NOT touch `scripts/seed_apollo_*.py` or `learner_model_seed.py`.
- Verify: `pytest tests/database/test_graph_compare_symbolic_mappings_roundtrip.py -m integration` (real-PG container) green.

### 3. Frozen DTOs (`apollo/graph_compare/canonical.py` — types only, first pass)
- [ ] Write `test_canonical_types.py` FIRST (immutability + field shape).
- [ ] File: `apollo/graph_compare/canonical.py`. Define the three frozen dataclasses ONLY in this task (`CanonicalNode`, `CanonicalGraph`, `ReferenceGraph`). Builders land in tasks 5/6 in the same file.
- Signatures: see "Public signatures (exact)".
- Verify: `pytest apollo/graph_compare/tests/test_canonical_types.py`.

### 4. Validator (`apollo/graph_compare/validator.py`)
- [ ] Write `test_validator.py` FIRST (all rows in the test list).
- [ ] File: `apollo/graph_compare/validator.py`.
  - `StudentGraphInvalidError(ValueError)` and `ReferenceGraphInvalidError(ValueError)` — frozen reason fields (`reasons: tuple[str, ...]`).
  - `validate_student_graph(student_graph: KGGraph) -> None` — raises `StudentGraphInvalidError` on any grammar violation, else returns None. Checks: every edge's `(from_node_type, to_node_type)` is in `EDGE_ALLOWED_PAIRS[edge_type]` (look the endpoint node types up from the graph's `node_index()` because parser edges may carry `None` types — re-derive, do not trust the optional field); every edge endpoint exists in the node set; `attempt_id` uniformity (all nodes + edges share ONE `attempt_id`); no PRECEDES cycle (call `student_graph.topological_order(EdgeType.PRECEDES)` inside try/except `ValueError` → re-raise as `StudentGraphInvalidError`); required node fields are guaranteed by Pydantic at construction so we only re-check cross-node invariants.
  - `validate_reference(problem: dict) -> None` — delegates to `validate_reference_graph(problem)` (REUSE, `learner_model_seed.py:307`); if `.ok` is False, raise `ReferenceGraphInvalidError(reasons=result.errors)`. Do NOT reimplement §6.1.
- Verify: `pytest apollo/graph_compare/tests/test_validator.py`.

### 5. Reference canonical builder (`build_reference_canonical` in `canonical.py`)
- [ ] Write `test_reference_canonical.py` FIRST.
- [ ] `build_reference_canonical(problem: dict) -> ReferenceGraph`. Steps:
  1. Call `validate_reference(problem)` first (raises `ReferenceGraphInvalidError` on bad reference — the §6.6 "reference fails validation → block grading" gate).
  2. Build one `CanonicalNode` per `reference_solution` step, keyed on its `entity_key` (the canonical key), carrying `node_type` (mapped from `entry_type` via the same `_ENTRY_TYPE_TO_NODE_TYPE` table the resolver's `candidates.py` uses — import it, do not duplicate), the step's `content.symbolic` (equations) / `applies_when` / `transformation`, and `source_ref_ids=(step["id"],)`.
  3. Build edges from each step's `depends_on` list: a directed dependency edge `(dependency_step_id → step_id)` mapped to canonical keys. These become the R_norm DAG.
  4. Build the per-path views: for each path in `problem["declared_paths"]`, produce a `ReferencePathView` (ordered tuple of canonical keys, mapped from the path's reference-node ids via the step→entity_key map). v1 authors one path; a synthetic two-path problem in tests covers the multi-path branch.
  5. **NEVER read the deduped entity payload.** Read only `problem["reference_solution"]`, `depends_on`, `declared_paths`.
- Verify: `pytest apollo/graph_compare/tests/test_reference_canonical.py`.

### 6. Student canonical builder (`build_student_canonical` in `canonical.py`)
- [ ] Write `test_student_canonical.py` FIRST.
- [ ] `build_student_canonical(student_graph: KGGraph, resolution: ResolutionResult) -> CanonicalGraph`. Steps:
  1. Build a `{node_id -> ResolvedNode}` index from `resolution.resolved`.
  2. Partition student nodes: resolved (`resolution == "resolved"` AND `resolved_key is not None`) vs unresolved.
  3. **Merge** resolved nodes by `resolved_key`: all student nodes sharing one `resolved_key` collapse into ONE `CanonicalNode` whose `source_node_ids` is the tuple of raw `node_id`s (sorted for determinism) and whose `evidence_spans` is the tuple of each source node's surface text (reuse `apollo.resolution.tiers.student_surface_text` — import it). Carry the merged node's `node_type` (all merged share it — type-compat guarantees this) and `resolved_key`/`method`/`confidence` (from the ResolvedNode; on conflict keep the highest-confidence method deterministically).
  4. **Normalize edges after endpoint resolution:** for each student edge, map both endpoints through the node_id→resolved_key index. If BOTH endpoints resolved, emit a canonical edge `(from_key → to_key)` carrying `edge_type` + `provenance`. If EITHER endpoint is unresolved, **DROP** the edge from comparison (Decision 7) but record it in `dropped_edge_count` (data, not silent).
  5. **Retain unresolved nodes** as `unresolved_nodes`: a tuple of `(node_id, surface_text)` — findings-input for WU-4A2, NOT dropped, NOT scored here.
  6. Return `CanonicalGraph(nodes=..., edges=..., unresolved_nodes=..., dropped_edge_count=...)`. All tuples, frozen.
- Verify: `pytest apollo/graph_compare/tests/test_student_canonical.py`.

### 7. Problem-inputs assembly (`apollo/graph_compare/problem_inputs.py`)
- [ ] Write `test_problem_inputs.py` FIRST.
- [ ] File: `apollo/graph_compare/problem_inputs.py`. Define a small frozen `ProblemInputs(candidates: tuple[Candidate, ...], symbolic_mappings: dict[str, str])` and:
  - `build_problem_candidates(problem: dict, misconceptions: dict, *, canon_key_by_canonical_key: dict[str, int]) -> ProblemInputs`. Steps:
    1. `refs = candidates_from_reference_solution(problem, canon_key_by_canonical_key=...)` (REUSE).
    2. `miscs = candidates_from_misconceptions(misconceptions, canon_key_by_canonical_key=...)` (REUSE).
    3. `candidates = build_candidate_set(reference_nodes=refs, misconception_entities=miscs)` (REUSE).
    4. `symbolic_mappings = dict(problem.get("symbolic_mappings", {}))` (default `{}` when absent — Decision 2; return a NEW dict, never alias the problem's).
    5. Return `ProblemInputs(candidates, symbolic_mappings)`.
  - This is the seam where the per-problem `symbolic_mappings` table is resolved to pass into `resolve_attempt(student_graph, inputs.candidates, symbolic_mappings=inputs.symbolic_mappings)`. WU-4A1 does NOT call `resolve_attempt` itself (the caller / WU-4C does); it only assembles the inputs.
- Verify: `pytest apollo/graph_compare/tests/test_problem_inputs.py`.

### 8. Package `__init__` public API (`apollo/graph_compare/__init__.py`)
- [ ] Write `test_package_seam.py` FIRST (asserts every public name imports from `apollo.graph_compare`).
- [ ] File: `apollo/graph_compare/__init__.py`. Re-export the public seam (see "Public signatures"), with a flat `__all__`. Add `apollo/graph_compare/tests/__init__.py` if the resolution tests package carries one (check `apollo/resolution/tests/` for an `__init__.py`; mirror it).
- Verify: `pytest apollo/graph_compare/tests/test_package_seam.py` and `python -c "import apollo.graph_compare as g; print(g.__all__)"`.

### 9. Owner-doc reconciliation
- [ ] Edit `docs/architecture/apollo.md` (Decision: drift contract). Add `apollo/graph_compare/**` to the `owns:` glob list (it currently owns `apollo/**` via the wildcard — confirm the wildcard already covers it; if so, add an explicit module-map row instead of a glob change). Add a Module-map table row for `apollo/graph_compare/` describing the WU-4A1 surface. Add a "Key service entry points" bullet for `build_student_canonical`/`build_reference_canonical`/`validate_student_graph`/`build_problem_candidates`. Note that scores/findings are WU-4A2. Bump `last_verified:` to `2026-06-17`.
- Verify: `grep -n "graph_compare" docs/architecture/apollo.md` shows the new rows; frontmatter `last_verified: 2026-06-17`.

## Public signatures (exact)

All dataclasses are `@dataclass(frozen=True)`. All collection fields are tuples (immutable). These are the EXACT signatures the executor must produce; downstream WU-4A2 consumes them.

```python
# apollo/graph_compare/canonical.py

from dataclasses import dataclass
from apollo.ontology.edges import EdgeType, EdgeProvenance
from apollo.ontology.graph import KGGraph
from apollo.ontology.nodes import NodeType
from apollo.resolution import ResolutionResult

@dataclass(frozen=True)
class CanonicalNode:
    """One canonical-space node. For S_norm: the merge of all student nodes that
    resolved to `canonical_key`. For R_norm: one reference step."""
    canonical_key: str                  # the resolved/entity key (the comparison identity)
    node_type: NodeType
    source_node_ids: tuple[str, ...]    # raw student node-ids merged here (sorted); for R_norm: the ref step id(s)
    evidence_spans: tuple[str, ...]     # surface text per source node (durable provenance, §7); () for R_norm
    symbolic: str | None = None         # equations only
    method: str | None = None           # resolution method for S_norm nodes (None for R_norm)
    confidence: float | None = None     # method-cap confidence for S_norm nodes (None for R_norm)

@dataclass(frozen=True)
class CanonicalEdge:
    """A normalized edge in canonical space (endpoints are canonical_keys)."""
    edge_type: EdgeType
    from_key: str
    to_key: str
    provenance: EdgeProvenance          # carried from the student edge ('explicit'|'inferred')

@dataclass(frozen=True)
class CanonicalGraph:
    """S_norm. Resolved+merged nodes, normalized edges, retained unresolved nodes."""
    nodes: tuple[CanonicalNode, ...]
    edges: tuple[CanonicalEdge, ...]
    unresolved_nodes: tuple[tuple[str, str], ...]   # (raw node_id, surface_text) — findings-input, NOT scored here
    dropped_edge_count: int                          # edges dropped because an endpoint was unresolved

@dataclass(frozen=True)
class ReferencePathView:
    """One declared acceptable solution path, as an ordered tuple of canonical keys."""
    canonical_keys: tuple[str, ...]

@dataclass(frozen=True)
class ReferenceGraph:
    """R_norm. Per-problem reference nodes + dependency DAG + declared-path views."""
    nodes: tuple[CanonicalNode, ...]
    edges: tuple[CanonicalEdge, ...]            # dependency edges from each step's depends_on
    paths: tuple[ReferencePathView, ...]        # one per declared_paths entry (>= 1)

def build_student_canonical(student_graph: KGGraph, resolution: ResolutionResult) -> CanonicalGraph: ...
def build_reference_canonical(problem: dict) -> ReferenceGraph: ...
```

```python
# apollo/graph_compare/validator.py

from apollo.ontology.graph import KGGraph

class StudentGraphInvalidError(ValueError):
    def __init__(self, reasons: tuple[str, ...]) -> None: ...
    reasons: tuple[str, ...]

class ReferenceGraphInvalidError(ValueError):
    def __init__(self, reasons: tuple[str, ...]) -> None: ...
    reasons: tuple[str, ...]

def validate_student_graph(student_graph: KGGraph) -> None: ...   # raises StudentGraphInvalidError
def validate_reference(problem: dict) -> None: ...                # delegates to validate_reference_graph; raises ReferenceGraphInvalidError
```

```python
# apollo/graph_compare/problem_inputs.py

from dataclasses import dataclass, field
from apollo.resolution import Candidate

@dataclass(frozen=True)
class ProblemInputs:
    candidates: tuple[Candidate, ...]
    symbolic_mappings: dict[str, str] = field(default_factory=dict)   # per-problem; {} when absent

def build_problem_candidates(
    problem: dict,
    misconceptions: dict,
    *,
    canon_key_by_canonical_key: dict[str, int],
) -> ProblemInputs: ...
```

```python
# apollo/graph_compare/__init__.py  (public seam; flat __all__)
from apollo.graph_compare.canonical import (
    CanonicalNode, CanonicalEdge, CanonicalGraph, ReferencePathView, ReferenceGraph,
    build_student_canonical, build_reference_canonical,
)
from apollo.graph_compare.validator import (
    validate_student_graph, validate_reference,
    StudentGraphInvalidError, ReferenceGraphInvalidError,
)
from apollo.graph_compare.problem_inputs import ProblemInputs, build_problem_candidates
__all__ = [...]  # every name above
```

**Backward-compat note:** WU-4A1 introduces only NEW names; it edits no existing public signature. `resolve_attempt` and `validate_reference_graph` are consumed unchanged. The only data change is one additive JSON key (`symbolic_mappings`) which all existing readers ignore.

## Full test list

All in `apollo/graph_compare/tests/` except the one DB round-trip test (in `tests/database/`). Every test name below is concrete; "asserts" states the behavior pinned; "deps" states how external deps are handled. Helpers `_node(...)`/`_cand(...)` are copied from `apollo/resolution/tests/test_resolver.py:38-55` (build `Node` via `build_node`, `Candidate` directly). A small `_resolved(node_id, key, method, conf)` / `_unresolved(node_id)` factory builds `ResolvedNode`s and a `_resolution(*rns)` builds a `ResolutionResult` (these inputs are hand-built — 4A1 does NOT run `resolve_attempt`, so no LLM is involved at all in the builder tests).

### test_canonical_types.py (Task 3)
1. `test_canonical_node_is_frozen` — mutating `CanonicalNode.canonical_key` raises `dataclasses.FrozenInstanceError`. Deps: none.
2. `test_canonical_graph_is_frozen` — same for `CanonicalGraph`. Deps: none.
3. `test_reference_graph_is_frozen` — same for `ReferenceGraph`. Deps: none.
4. `test_canonical_node_defaults` — `symbolic`/`method`/`confidence` default to `None`; tuple fields default-construct empty where applicable. Deps: none.

### test_validator.py (Task 4)
5. `test_valid_student_graph_passes` — a well-formed graph (proc PRECEDES proc, proc USES equation, condition SCOPES equation) → `validate_student_graph` returns None (no raise). Deps: in-memory `KGGraph`.
6. `test_illegal_edge_endpoint_raises` — an `equation SCOPES condition` edge (SCOPES must originate from condition/simplification, per `EDGE_ALLOWED_PAIRS`) → `StudentGraphInvalidError`; `.reasons` mentions the offending edge. **Build the bad edge bypassing the `Edge` Pydantic validator** (the `Edge` model rejects it at construction when types are set — construct with `from_node_type=None`/`to_node_type=None` so the model allows it, then the validator re-derives types from the graph and rejects). Deps: in-memory.
7. `test_edge_endpoint_missing_node_raises` — an edge referencing a `to_node_id` not in `nodes` → `StudentGraphInvalidError`. Deps: in-memory.
8. `test_precedes_cycle_raises` — two procedure_step nodes with PRECEDES edges A→B and B→A → `StudentGraphInvalidError` (caught from `topological_order`'s `ValueError`). Deps: in-memory.
9. `test_mixed_attempt_id_raises` — nodes/edges carrying two different `attempt_id`s → `StudentGraphInvalidError` (scoping uniformity). Deps: in-memory.
10. `test_validate_reference_delegates_ok` — a fully-annotated `problem_01.json` (loaded from disk) → `validate_reference` returns None. Deps: real bernoulli JSON from disk (no DB; `validate_reference_graph` is pure).
11. `test_validate_reference_missing_entity_link_raises` — a problem dict with a reference step lacking `entity_key` → `ReferenceGraphInvalidError`; `.reasons` is exactly `validate_reference_graph(...).errors`. Deps: hand-built dict.
12. `test_validate_reference_undeclared_paths_raises` — a problem dict with empty `declared_paths` → `ReferenceGraphInvalidError`. Deps: hand-built dict.

### test_reference_canonical.py (Task 5)
13. `test_reference_canonical_from_problem01` — `build_reference_canonical(problem_01)` yields 7 nodes keyed by the 7 `entity_key`s; `eq.continuity` node carries symbolic `"rho*A1*v1 - rho*A2*v2"`; node_types map correctly (`simp.*` → `simplification`, `proc.*` → `procedure_step`). Deps: real problem_01.json.
14. `test_reference_canonical_depends_on_becomes_edges` — `eq.bernoulli` `depends_on: ["incompressibility"]` produces a canonical dependency edge `cond.incompressibility → eq.bernoulli`. Deps: real problem_01.json.
15. `test_reference_canonical_single_declared_path_view` — problem_01 yields exactly one `ReferencePathView` whose `canonical_keys` are the 7 entity keys in declared-path order. Deps: real problem_01.json.
16. `test_reference_canonical_multi_path_views` — a SYNTHETIC two-path problem (hand-built dict with two entries in `declared_paths`) yields two `ReferencePathView`s, each mapping its own node-id sequence to canonical keys (covers the multi-path branch for the 95% gate). Deps: hand-built dict.
17. `test_reference_canonical_uses_own_reference_solution_not_dedup` — **REGRESSION GUARD (Decision 3):** two hand-built problems both minting `eq.continuity` with DIFFERENT symbolic forms (`"rho*A1*v1 - rho*A2*v2"` vs `"A1*v1 - A2*v2"`); assert each `build_reference_canonical` result's `eq.continuity` node carries ITS OWN symbolic, never a shared/deduped value. Deps: two hand-built dicts.
18. `test_reference_canonical_invalid_reference_raises` — a problem failing `validate_reference_graph` (missing entity link) → `build_reference_canonical` raises `ReferenceGraphInvalidError` (the §6.6 block-grading gate fires before any build). Deps: hand-built dict.

### test_student_canonical.py (Task 6)
19. `test_two_nodes_same_target_merge_into_one_node` — **KEY (§6.3):** two student nodes (`d1` "density stays the same", `d2` "constant density") both resolving to `cond.incompressibility` → ONE `CanonicalNode` with `source_node_ids == ("d1", "d2")` (sorted) and `evidence_spans` carrying BOTH surface texts. Deps: hand-built `KGGraph` + hand-built `ResolutionResult`.
20. `test_unresolved_node_retained_not_dropped` — **KEY (§6.3):** a student node resolving to `unresolved` is NOT a `CanonicalNode` but APPEARS in `unresolved_nodes` as `(node_id, surface_text)`. Deps: hand-built.
21. `test_unresolved_incident_edges_dropped` — **KEY (§6.3):** an edge incident to an unresolved node is dropped from `edges` and `dropped_edge_count == 1`; an edge between two resolved nodes is kept. Deps: hand-built.
22. `test_resolved_edge_endpoints_normalized_to_keys` — a student PRECEDES edge `p1→p2` where `p1→proc.a`, `p2→proc.b` becomes a `CanonicalEdge(PRECEDES, "proc.a", "proc.b")` carrying the student edge's provenance. Deps: hand-built.
23. `test_merge_carries_highest_confidence_method_deterministically` — two nodes resolving to the same key via different methods (`alias` 0.92 vs `fuzzy` 0.80) → the merged node's `method`/`confidence` is the higher-confidence one, deterministic across runs. Deps: hand-built.
24. `test_empty_student_graph_yields_empty_canonical` — empty `KGGraph` + empty `ResolutionResult` → `CanonicalGraph` with empty tuples and `dropped_edge_count == 0` (no crash; the §6.1 empty-student degenerate input — scored 0/1/0 in WU-4A2, not here). Deps: hand-built.
25. `test_builder_is_pure_same_input_same_output` — two calls on identical inputs produce equal `CanonicalGraph`s (immutable, deterministic ordering). Deps: hand-built.

### test_problem_inputs.py (Task 7)
26. `test_build_problem_candidates_assembles_closed_set` — `build_problem_candidates(problem_01, misconceptions, ...)` returns `ProblemInputs` whose `candidates` include all 7 reference candidates + both misconceptions (delegates to the reused builders). Deps: real problem_01.json + misconceptions.json from disk.
27. `test_symbolic_mappings_read_from_problem` — **KEY (Decision 2):** problem_01 (now carrying `{"d": "2*r"}`) → `inputs.symbolic_mappings == {"d": "2*r"}`. Deps: real problem_01.json.
28. `test_symbolic_mappings_default_empty_when_absent` — a problem dict with NO `symbolic_mappings` key → `inputs.symbolic_mappings == {}` (a NEW dict, not aliasing the problem). Deps: hand-built dict.
29. `test_symbolic_mappings_plumbed_makes_circular_area_resolvable` — **KEY (Decision 2, §6.9):** build inputs from a problem carrying `{"d": "2*r"}` with an `eq.circular_area` candidate (`symbolic="A = pi*d**2/4"`); feed `inputs.candidates` + `inputs.symbolic_mappings` into `resolve_attempt(student_graph_with_A_eq_pi_r2, ...)`; assert the `A = pi*r**2` student node resolves to `eq.circular_area` via the `symbolic` method. **This is the end-to-end proof that 4A1's seam correctly plumbs the per-problem table into the resolver's symbolic tier.** Deps: in-memory `resolve_attempt` (CI-safe: `llm_adjudicator=None`, resolves on the symbolic tier alone, `llm_calls == 0` — no live API).
30. `test_problem_inputs_is_frozen` — `ProblemInputs` mutation raises `FrozenInstanceError`. Deps: none.

### test_package_seam.py (Task 8)
31. `test_public_api_importable_from_package` — every name in the `__all__` imports directly from `apollo.graph_compare`. Deps: none.
32. `test_all_matches_exports` — `apollo.graph_compare.__all__` equals the sorted set of public names (no stray export, no missing). Deps: none.

### tests/database/test_graph_compare_symbolic_mappings_roundtrip.py (Task 2 — DB gate)
33. `test_symbolic_mappings_round_trips_into_concept_problem_payload` — **DB integration (`@pytest.mark.integration`).** Reuse the harness from `tests/database/test_seed_apollo_learner_model.py` (fresh pgvector DB, migration chain + 026, `auth.users`/`aita_search_spaces` stubs, real bernoulli rows). Run `seed_apollo_concept_registry.seed(...)` then `seed_apollo_learner_model.seed(..., write_disk=False)`; query `apollo_concept_problems.payload` for the bernoulli problem and assert `payload["symbolic_mappings"] == {"d": "2*r"}` survived BOTH seeders (proves Decision 4 with zero seeder code change). `write_disk=False` so the test never rewrites the on-disk JSON. Deps: real Postgres container (the project DB gate fires here — enumerate it in the PR description as a real-PG test, not a line-coverage exemption).

**Coverage note:** Tests 5–32 are pure in-memory unit tests over `KGGraph` + hand-built `ResolutionResult` + problem dicts — no containers, no LLM, no network. Test 29 uses the real `resolve_attempt` with `llm_adjudicator=None` (CI-safe, resolves on the symbolic tier, makes NO live call). Test 33 is the single real-PG test. Target: 100% patch coverage; the multi-path branch (test 16), the unresolved-edge-drop branch (test 21), and the merge branch (test 19) are the three branches most at risk of being missed — each has a dedicated test.

## Mocking strategy

- **No LLM, no network, no Neo4j in any unit test.** The builders take already-computed inputs. `ResolutionResult` is hand-built directly (frozen dataclass) — no need to run the resolver. Test 29 is the ONLY test that invokes `resolve_attempt`, and it passes `llm_adjudicator=None` (the CI-safe default — when None and nodes remain unresolved after content tiers, NO live call fires; `resolver.py:142-145`). The circular-area case resolves on the symbolic tier (`llm_calls == 0`), so even test 29 makes zero API calls.
- **Real JSON, not mocked JSON, where the contract is the JSON shape.** Tests 10, 13–15, 26–27 load the real `problem_01.json` / `misconceptions.json` from disk via `Path(__file__).resolve().parents[2] / "subjects" / "fluid_mechanics" / "concepts" / "bernoulli_principle"` (the exact pattern in `test_candidates.py:30-42`). This pins the builder against the real authored data, not a fiction.
- **Hand-built dicts for branch coverage** (tests 11–12, 16–18, 28): synthetic problem dicts exercise the invalid-reference, multi-path, and dedup-regression branches that the single real problem cannot.
- **DB test (test 33):** reuse the existing Testcontainers pgvector harness from `tests/database/test_seed_apollo_learner_model.py` verbatim (per-test fresh DB, migration chain + 026, FK stubs). No remote DB — local Docker only (CLAUDE.md DB gate: agents never apply migrations to any remote project).
- **`Edge` Pydantic bypass for the illegal-endpoint test (test 6):** the `Edge` model validates `EDGE_ALLOWED_PAIRS` at construction WHEN `from_node_type`/`to_node_type` are set (`edges.py:71-86`). To test the validator's own re-derivation, construct the bad edge with both type fields left `None` (the model then skips the pair check), so the bad pair reaches `validate_student_graph`, which re-derives the types from the graph's `node_index()` and rejects it. Document this in a test comment.

## Owner-doc updates

`docs/architecture/apollo.md` (the `owns: apollo/**` authority for this code):
- **Module-map table:** add a row for `apollo/graph_compare/` listing `validator.py`, `canonical.py`, `problem_inputs.py`, `__init__.py` and describing the WU-4A1 surface: validate raw student graph (grammar/endpoints/attempt-id uniformity/no-PRECEDES-cycle) + reuse `validate_reference_graph` for the reference side; build `S_norm` (merge student nodes by resolved key, normalize edges, drop unresolved edges, retain unresolved nodes) and `R_norm` (per-problem reference_solution + depends_on + declared_paths, per-path views) — NO scores (WU-4A2). State that the package mirrors `apollo/resolution/` (pure, DB-free, frozen dataclasses) and consumes `apollo.resolution` + `validate_reference_graph`.
- **Key service entry points:** add bullets for `validate_student_graph`/`validate_reference`, `build_student_canonical(student_graph, resolution) -> CanonicalGraph`, `build_reference_canonical(problem) -> ReferenceGraph`, `build_problem_candidates(problem, misconceptions, *, canon_key_by_canonical_key) -> ProblemInputs` (assembles the closed candidate set + reads the per-problem `symbolic_mappings`).
- **Core types:** add `CanonicalNode`/`CanonicalEdge`/`CanonicalGraph`/`ReferencePathView`/`ReferenceGraph` to the "Core types" list with their fields.
- **Conventions/NO-FALLBACK:** add `StudentGraphInvalidError`/`ReferenceGraphInvalidError` to the named-error list (deliberately NOT HTTP-registered in this unit — the Done route is WU-4C). A reference-validation failure blocks grading (§6.6); a raw-student grammar violation is the §6.4 step-4 gate.
- **Subjects note:** record that `problem_01.json` now carries an additive `symbolic_mappings` key (`{"d": "2*r"}`), read by `build_problem_candidates` and round-tripped by both seeders unchanged (Decision 4).
- **Frontmatter:** bump `last_verified: 2026-06-17`.

## Risks

- **[LOW] No repository/endpoint/controller layer in this unit.** Justified: WU-4A1 is a pure in-memory transform with no persistence and no HTTP surface (per the split proposal §3 SEAM and Decision 5). The Done route, `store.read_graph` wiring, and the live `resolve_attempt` call are WU-4C. Skipping those layers is correct, not an omission.
- **[MEDIUM] `_ENTRY_TYPE_TO_NODE_TYPE` duplication risk.** `build_reference_canonical` must map `entry_type → NodeType`. The table already exists in `apollo/resolution/candidates.py:47-53` (`_ENTRY_TYPE_TO_NODE_TYPE`, module-private). Import it (`from apollo.resolution.candidates import _ENTRY_TYPE_TO_NODE_TYPE`) rather than re-defining, to avoid divergence. Importing a `_`-private name across modules is a minor smell; the alternative (duplicating the 5-entry table) is worse drift. If the executor prefers, promote it to a public name in `candidates.py` in this unit (additive, no behavior change) — but that touches a resolution file, so confirm scope. **Recommend: import the private name with a comment; do NOT edit candidates.py.**
- **[MEDIUM] Merge confidence/method tie-break must be deterministic.** When two student nodes resolve to the same key via different methods, the merged node's `method`/`confidence` must be chosen deterministically (highest `METHOD_CONFIDENCE_CAP`, then a stable tie-break on method name). Non-determinism here would flake test 23 and corrupt WU-4A2's scoring inputs. Pin it explicitly (sort by `(-confidence, method)`).
- **[LOW] `evidence_spans` surface-text source.** Reuse `apollo.resolution.tiers.student_surface_text(node)` for the span text so S_norm spans match the resolver's matching text exactly. Importing from `tiers` (not `__init__`) is fine — it is a stable pure helper. Spans are the durable provenance (§7); node-ids are best-effort.
- **[LOW] Multi-path branch only reachable via a synthetic problem.** v1 authors one declared path, so the max-over-paths machinery's multi-path branch (test 16) needs a hand-built two-path problem to hit 95%. Already in the test list; flagged so the executor does not assume the real fixtures cover it.
- **[LOW] DB test fires the project DB gate.** Test 33 is real-PG (Testcontainers, local Docker). Enumerate it explicitly in the PR description per CLAUDE.md (DB changes use behavior enumeration, not line coverage). No migration is applied to any remote project.
- **[LOW] Budget check.** Structural prep is 0 steps (neighborhood clean). No risk of the 30% structural-prep budget being exceeded.

## Out-of-scope boundaries (this unit)

WU-4A1 does NOT (these are WU-4A2 / 4B / 4C):
- Compute ANY score (coverage, soundness, bisimilarity, the seven sub-scores) — WU-4A2.
- Define `Finding`/`FindingKind`/`GradeResult`/`grade_attempt`/`COMPARISON_VERSION` — WU-4A2.
- Run the coverage `R ⊑ S` or soundness `S ⊑ R` simulation passes — WU-4A2.
- Emit learner-model events, run the transcript audit, apply abstention gates, or persist runs/findings — WU-4B.
- Re-orchestrate `done.py`, map findings → rubric input, wire `store.read_graph`, or call `resolve_attempt` on the live path — WU-4C.
- Touch `done.py`, `overseer/coverage.py`, `overseer/rubric.py`, `api.py`, or any remote infra — explicitly forbidden by the binding constraints.
- Add any migration or new package, or install any dependency.
- Persist `normalization_confidence`/`comparison_confidence` or write to the grades tables (4A1 persists nothing).

## Deviations I'd allow the executor

- **DTO field naming:** `source_node_ids` vs `raw_node_ids`, `evidence_spans` vs `spans` — pick consistent names; the proposal says "raw node-ids + evidence spans," either spelling is fine if used uniformly and reflected in the owner doc.
- **`_ENTRY_TYPE_TO_NODE_TYPE` sourcing:** import the resolution private name (recommended) OR promote it to public in `candidates.py` (additive). Either is acceptable; do not silently duplicate the table.
- **File split inside `canonical.py`:** if `canonical.py` approaches 200 lines, the executor may split the student builder and reference builder into `student_canonical.py` / `reference_canonical.py` (mirrors resolution's many-small-files style) — keep the public seam in `__init__` identical.
- **`validate_reference` naming:** `validate_reference` vs `validate_reference_graph_for_compare` — avoid colliding with the reused `validate_reference_graph`; any non-colliding name is fine. The proposal's published API used `validate_student_graph` as the one student entry point; keep that exact name.
- **Test helper placement:** shared `_node`/`_cand`/`_resolution` factories may live in a small `apollo/graph_compare/tests/_factories.py` or be copied per-file; either is acceptable.
- **Extra defensive tests** beyond the 33 listed (e.g. a `definition`/`variable_mapping` node-type round-trip) are welcome if they raise patch coverage; never add skip/xfail to pad coverage.

The executor must NOT deviate on: no migration, no new package, no edit to the forbidden files, no live LLM/network/remote-DB call, the exact public names `build_student_canonical`/`build_reference_canonical`/`validate_student_graph`/`build_problem_candidates`/`StudentGraphInvalidError`/`ReferenceGraphInvalidError`, R_norm reading only the problem's own `reference_solution`, and the 95% patch gate.
