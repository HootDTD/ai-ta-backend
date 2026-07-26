# Plan: WU-4A2 — retired graph comparator simulation + scores → grade_attempt() → GradeResult

**Goal:** Add the deterministic score-math half of the §6 grading core — coverage (max-over-paths), soundness (contradictions-only), the 7 sub-scores, bisimilarity, in-memory findings, and the single `grade_attempt(student_canonical, reference_graph) -> GradeResult` callable — over WU-4A1's `CanonicalGraph`/`ReferenceGraph`, with zero DB/Neo4j/LLM in the score-math.
**Architecture:** New pure modules in `apollo/retired graph comparator/` (`coverage.py`, `soundness.py`, `scores.py`, `bisimilarity.py`, `findings.py`, `core.py`) + `__init__.py` re-export extension. Consumes WU-4A1 (`canonical.py`) + `apollo/resolution` (`Candidate.is_misconception`/`opposes_key`) + `apollo/ontology/edges.py` (`EdgeProvenance`/`EdgeType`). Mirrors `apollo/resolution/` package style (many small frozen-dataclass pure modules, one `__init__` seam).
**Tech stack:** Python 3.12 / FastAPI backend, pytest (no Testcontainers — pure in-memory unit tests), diff-cover patch gate, black/isort/ruff.

---
provides:
  - grade_attempt(student_canonical: CanonicalGraph, reference_graph: ReferenceGraph) -> GradeResult  (the single public callable WU-4C imports)
  - GradeResult (frozen dataclass; fields map 1:1 to apollo_graph_comparison_runs score columns + comparison_confidence + findings + comparison_version)
  - Finding (frozen dataclass) + FindingKind (enum, == models.FINDING_KINDS set)
  - COMPARISON_VERSION (str constant for the runs row comparison_version column)
  - coverage / soundness / scores / bisimilarity pure functions (package-internal, re-tested directly)
consumes:
  - apollo.retired graph comparator.canonical: CanonicalGraph, CanonicalNode, CanonicalEdge, ReferenceGraph, ReferencePathView (WU-4A1)
  - apollo.resolution.candidates: Candidate (is_misconception / opposes_key) — for contradiction detection metadata
  - apollo.ontology.edges: EdgeType, EdgeProvenance
  - apollo.persistence.models.FINDING_KINDS (the authoritative finding-kind enum string set — aligned, not re-derived)
depends_on:
  - WU-4A1 (feat/apollo-kg-wu4a1-canonical-build) — the diff-cover compare base; CanonicalGraph/ReferenceGraph must exist
  - migration 026 (already landed: apollo_graph_comparison_runs + ORM) — NO new migration in this unit
---

## Overview

WU-4A1 already lands the "build half": `build_student_canonical(student_graph, resolution) -> CanonicalGraph` (S_norm) and `build_reference_canonical(problem) -> ReferenceGraph` (R_norm). WU-4A2 is the "compare half": given those two immutable graphs, compute the §6.2 scores deterministically and emit in-memory findings. **No Neo4j, no Postgres, no LLM, no raw problem JSON re-read** — the inputs are already-canonical dataclasses, so every test is a pure in-memory unit test (no Testcontainers).

The handoff artifact is one frozen `GradeResult` carrying: the 3 top-line scores (`coverage_score`, `soundness_score`, `bisimilarity_score`), the 7 sub-scores (`node_coverage_score`, `edge_coverage_score`, `scoping_score`, `usage_score`, `procedure_order_score`, `dependency_score`, `contradiction_score`), `comparison_confidence` (==1.0 in v1, carried on the result NOT the persisted column), a `findings: tuple[Finding, ...]`, and `comparison_version` (`COMPARISON_VERSION`). WU-4B consumes `GradeResult.findings` for the transcript audit, abstention gates, finding→event conversion (§6.5), and the runs/findings Postgres persistence.

The §6.2 binding rules this unit implements:
- **Coverage = MAX over declared paths** of node-coverage (`R_norm ⊑ S_norm` on the path's keys). A student on a valid alternative path is graded against the path they took — never punished against the authored one.
- **Soundness = 1 − penalty(contradictions ONLY)** (`S_norm ⊑ R_norm`). A contradiction is an S_norm node whose `canonical_key` is a misconception key (prefix `misc.`). **Unsupported extras and unresolved nodes carry ZERO soundness penalty.**
- **7 sub-scores**, with `procedure_order` penalizing only true-PRECEDES inversions (never absence), `dependency` the lowest weight (loose any→any DEPENDS_ON), and **edges diagnostic-grade only — never events; explicit outweighs inferred.**
- **Bisimilarity = harmonic_mean(soundness, coverage)**, `=0` when `a+b==0`, never NaN. Empty student graph → coverage 0, soundness 1, bisimilarity 0.

## Prior art (sibling modules)

- **`apollo/resolution/` package layout** — the direct template for this work: many small pure modules each with one frozen-dataclass result + pure functions, one `__init__.py` re-export seam, `from __future__ import annotations`, tuple (immutable) collection fields. Mirror it exactly. (`apollo/resolution/result.py:21-63` frozen `ResolvedNode`/`ResolutionResult`; `apollo/resolution/candidates.py:56-73` frozen `Candidate`.)
- **WU-4A1 `apollo/retired graph comparator/canonical.py:43-93`** — the EXACT input shapes: `CanonicalNode{canonical_key, node_type, source_node_ids, evidence_spans, symbolic, method, confidence}`, `CanonicalEdge{edge_type, from_key, to_key, provenance}`, `CanonicalGraph{nodes, edges, unresolved_nodes, dropped_edge_count}`, `ReferencePathView{canonical_keys}`, `ReferenceGraph{nodes, edges, paths}`.
- **WU-4A1 test patterns `apollo/retired graph comparator/tests/test_student_canonical.py:24-67`** — the hand-built fixture helpers (`_node`, `_edge`, `_resolved`, `_resolution`). WU-4A2 tests build `CanonicalGraph`/`ReferenceGraph` directly (one ring closer — no resolver, no KGGraph needed for the score tests), so define local `_cnode`/`_cedge`/`_snorm`/`_rgraph`/`_path` builders in a shared test helper.
- **`apollo/retired graph comparator/tests/test_package_seam.py:13-57`** — the seam-completeness test pattern (`_EXPECTED` set, `__all__` equality, "same object not a shadow"). Extend it (do NOT rewrite) for the new exports.
- **`apollo/resolution/candidates.py:35-42` (`METHOD_CONFIDENCE_CAP`)** — the per-method confidence cap convention; `CanonicalNode.confidence` is already capped, so a covered finding's confidence is read straight off the node (no re-capping).
- **`apollo/persistence/models.py:64-73` (`FINDING_KINDS`)** — the AUTHORITATIVE finding-kind string set (open enum, documentation-only). `FindingKind` enum values MUST equal this set 1:1: `covered_node, missing_node, matched_edge, missing_edge, unsupported_extra, contradiction, unresolved, alternative_path`. A test asserts the enum value-set equals `FINDING_KINDS`.
- **`apollo/persistence/models.py:479-524` (`GraphComparisonRun`)** — the persisted column names `GradeResult` maps to 1:1. NOTE the schema-real divergence: the persisted confidence column is `normalization_confidence` (NOT NULL), supplied by WU-4B from resolution method-caps; `comparison_confidence` is the score-math's own value (1.0 v1), carried on `GradeResult` but NOT persisted by name here.

## Structural prep (from neighborhood scan)

Change-path files scanned (the files this unit creates or imports, one ring out):

- `apollo/retired graph comparator/__init__.py` — 3 import groups, ~13 names today; adding ~5 more keeps it well under any threshold. CLEAN.
- `apollo/retired graph comparator/canonical.py` — 7 imports, 5 dataclasses + 3 functions. CLEAN (consumed, not edited).
- `apollo/resolution/candidates.py` — 2 imports, 3 functions + `Candidate`. CLEAN (consumed, not edited — `Candidate` only referenced in test fixtures and docstrings; the score-math reads the `misc.` key prefix off `CanonicalNode.canonical_key`, so `core.py` does NOT import `Candidate` at all).
- `apollo/ontology/edges.py` — small, stable. CLEAN.
- All new modules are GREENFIELD (do not exist yet); each is budgeted < 200 lines.

No circular-import risk: `core.py` imports `coverage`/`soundness`/`scores`/`bisimilarity`/`findings`/`canonical`; none of those import `core`. `findings.py` imports only `ontology.edges` + stdlib. The import DAG is strictly acyclic.

No coupling-hub risk: nothing imports the new modules yet except the package `__init__` (WU-4C wires `grade_attempt` into `done.py` LATER; out of scope).

**Verdict: neighborhood is clean. No structural prep step required.** (0% of plan steps — well under the 30% budget.)

## Layered tasks (TDD ORDER — tests RED first, then GREEN)

**Discipline (binding):** for each task, write the REAL test file FIRST and run it to confirm it FAILS for the right reason (RED), then write the minimal module to pass (GREEN), then refactor. No skip/xfail, no assert-nothing tests. All inputs are hand-built frozen dataclasses; there is no LLM/network to mock (the score-math is pure). Verify each task with `pytest apollo/retired graph comparator/tests/<file> -v` and, after all tasks, the patch gate below.

There is NO repository / DTO / controller / module-wiring layer here — this is a pure Python library unit, not an HTTP feature. The NestJS-style layering maps to: findings vocabulary → score passes → orchestrator → package seam → owner-doc. The "DB migration" layer is explicitly absent (justified in Out-of-scope: migration 026 already shipped the tables + ORM).

### 1. Findings model (findings.py) — the in-memory vocabulary

- [ ] Test first: `apollo/retired graph comparator/tests/test_findings.py`
- [ ] File: `apollo/retired graph comparator/findings.py` (NEW)
- Defines `FindingKind` (a `StrEnum`, mirroring `EdgeType`'s `StrEnum` style) with EXACTLY the 8 members of `apollo.persistence.models.FINDING_KINDS`: `COVERED_NODE="covered_node"`, `MISSING_NODE="missing_node"`, `MATCHED_EDGE="matched_edge"`, `MISSING_EDGE="missing_edge"`, `UNSUPPORTED_EXTRA="unsupported_extra"`, `CONTRADICTION="contradiction"`, `UNRESOLVED="unresolved"`, `ALTERNATIVE_PATH="alternative_path"`.
- Defines `Finding` (`@dataclass(frozen=True)`): `kind: FindingKind`, `canonical_key: str | None` (the entity/concept the finding concerns — None for pure edge findings), `student_node_ids: tuple[str, ...] = ()`, `reference_node_ids: tuple[str, ...] = ()`, `evidence_spans: tuple[str, ...] = ()`, `score: float | None = None`, `confidence: float | None = None`, `message: str | None = None`. (These map onto `GraphComparisonFinding` columns so WU-4B persists them with no reshaping; `student_edge_ids`/`reference_edge_ids` columns exist but edges here are diagnostic-only and keyed by `from_key→to_key` text, carried in `message`, so the edge-id tuples stay `()`.)
- Pure reducer helpers (each returns a NEW tuple, no mutation), to be CONSUMED by the score passes:
  - `covered_finding(node: CanonicalNode) -> Finding` (kind COVERED_NODE; carries node's evidence_spans + confidence + source_node_ids).
  - `missing_finding(ref_node: CanonicalNode) -> Finding` (kind MISSING_NODE; carries reference_node_ids; score 0.0; NO event decision — that is WU-4B).
  - `contradiction_finding(node: CanonicalNode) -> Finding` (kind CONTRADICTION; the misconception key + evidence; score 0.0).
  - `unsupported_extra_finding(node: CanonicalNode) -> Finding` (kind UNSUPPORTED_EXTRA; NO penalty marker — diagnostic only).
  - `unresolved_finding(node_id: str, surface: str) -> Finding` (kind UNRESOLVED; from `CanonicalGraph.unresolved_nodes`).
  - `matched_edge_finding`/`missing_edge_finding(edge: CanonicalEdge) -> Finding` (diagnostic; `message` carries `"<from_key> -<TYPE>-> <to_key>"` and provenance).
  - `alternative_path_finding(path_index: int, canonical_keys: tuple[str, ...]) -> Finding` (kind ALTERNATIVE_PATH; emitted when the winning coverage path is NOT path 0 — schema supports multi-path from day one).
- Verify: `pytest apollo/retired graph comparator/tests/test_findings.py -v`

### 2. Coverage pass (coverage.py) — R ⊑ S, max over paths

- [ ] Test first: `apollo/retired graph comparator/tests/test_coverage.py`
- [ ] File: `apollo/retired graph comparator/coverage.py` (NEW)
- `@dataclass(frozen=True) PathCoverage`: `path_index: int`, `covered_keys: tuple[str, ...]`, `missing_keys: tuple[str, ...]`, `score: float` (covered/total for THAT path; total==0 → score 1.0 vacuously to avoid div-by-zero on a degenerate empty path, but empty path lists never reach here — `ReferenceGraph.paths` is length ≥ 1).
- `coverage_per_path(student: CanonicalGraph, reference: ReferenceGraph) -> tuple[PathCoverage, ...]`: for each `ReferencePathView`, a reference key is covered iff it is in `{n.canonical_key for n in student.nodes}` (set membership — `R ⊑ S` per path). Returns one `PathCoverage` per path, in path order.
- `coverage_result(student, reference) -> tuple[float, PathCoverage, tuple[PathCoverage, ...]]`: returns `(max_score, winning_path_coverage, all_path_coverages)`. The winner is the path with the highest `score`; ties broken by lowest `path_index` (deterministic). **Empty student graph → every path scores 0.0 → coverage 0.0** (binding §6.1; never NaN because each path has ≥1 key so the denominator is never 0 in the real case, and the degenerate all-empty-paths case is guarded).
- This module computes scores ONLY; finding emission for covered/missing nodes happens in `core.py` against the WINNING path (so a node missing on path A but the student took path B is NOT a false missing).
- Verify: `pytest apollo/retired graph comparator/tests/test_coverage.py -v`

### 3. Soundness pass (soundness.py) — S ⊑ R, contradictions only

- [ ] Test first: `apollo/retired graph comparator/tests/test_soundness.py`
- [ ] File: `apollo/retired graph comparator/soundness.py` (NEW)
- `is_misconception_key(key: str) -> bool`: `key.startswith("misc.")` (the minted prefix — confirmed in `misconceptions.json` keys `misc.density_ignored` etc.; the spec's `canon.misc.*` prose maps to the actual `misc.*` mint). Single chokepoint constant `MISCONCEPTION_KEY_PREFIX = "misc."` so the rule is documented once.
- `contradiction_nodes(student: CanonicalGraph) -> tuple[CanonicalNode, ...]`: the S_norm nodes whose `canonical_key` is a misconception key. (Resolution already competed misconceptions at resolve-time, §5; reaching S_norm as a `misc.*` key IS the resolved contradiction.)
- `soundness_score(student: CanonicalGraph) -> float`: `1 - penalty(n_contradictions)`. Penalty function (binding, documented): `penalty(k) = min(1.0, k * CONTRADICTION_UNIT_PENALTY)` with `CONTRADICTION_UNIT_PENALTY = 0.5` (each contradiction halves toward 0; 2+ contradictions floor at 0.0). **Unsupported extras (S_norm nodes whose key is NOT in any reference path AND not a misconception) contribute ZERO penalty; unresolved nodes contribute ZERO penalty.** Empty student graph → 0 contradictions → soundness 1.0 (vacuously sound; §6.1 binding).
- The unit penalty value is a documented v1 constant (NOT "TBD"); flagged in Risks as the one calibration knob, but it is concretely 0.5 here and the executor must use exactly that.
- Verify: `pytest apollo/retired graph comparator/tests/test_soundness.py -v`

### 4. Sub-scores (scores.py) — the 7 rubric sub-scores

- [ ] Test first: `apollo/retired graph comparator/tests/test_scores.py`
- [ ] File: `apollo/retired graph comparator/scores.py` (NEW)
- `@dataclass(frozen=True) SubScores`: `node_coverage: float`, `edge_coverage: float`, `scoping: float`, `usage: float`, `procedure_order: float`, `dependency: float`, `contradiction: float`. (One field per `apollo_graph_comparison_runs` `*_score` column, minus the names — `core.py` maps these onto the column-named `GradeResult` fields.)
- `compute_sub_scores(student, reference, winning_path: PathCoverage) -> SubScores`. Each sub-score is a pure ratio in [0,1] over the canonical graphs (NEVER emits an event):
  - `node_coverage` = winning path's covered/total (same numerator as coverage — the node dimension).
  - `edge_coverage` = matched reference edges / total reference edges, where an edge matches iff an S_norm edge with the same `(edge_type, from_key, to_key)` exists. **explicit outweighs inferred:** an explicit student edge match counts 1.0, an inferred-only match counts `INFERRED_EDGE_WEIGHT = 0.5` (documented constant). Reference with zero edges → 1.0 (vacuous, no edges to cover).
  - `scoping` = matched SCOPES edges / reference SCOPES edges (condition/simplification→equation); vacuous 1.0 when reference has none.
  - `usage` = matched USES edges / reference USES edges (procedure_step→equation); vacuous 1.0 when none.
  - `procedure_order` = **penalize only INVERSIONS of true reference PRECEDES**: for each reference PRECEDES pair (A precedes B) where BOTH A and B are present in S_norm, an inversion is an S_norm PRECEDES edge B→A (the student stated the reverse order). Score = `1 - inversions/comparable_pairs`; a MISSING stated order (student never stated A→B) is NOT penalized; vacuous 1.0 when no comparable ordered pairs exist. NOTE the reference DAG carries dependency edges as `DEPENDS_ON` (per WU-4A1's `build_reference_canonical`), so true reference PRECEDES order is derived from the reference path SEQUENCE (the `ReferencePathView.canonical_keys` order over procedure_step nodes), and inversions are checked against S_norm's PRECEDES edges — documented in the module docstring.
  - `dependency` = LOWEST weight; loose any→any DEPENDS_ON: matched DEPENDS_ON edges / reference DEPENDS_ON edges, direction-loose (an edge matches if the unordered key pair matches). Vacuous 1.0 when none. (The lowest-weight nature is a rubric-aggregation concern downstream — WU-4C; here it is just computed.)
  - `contradiction` = the soundness contradiction dimension as a [0,1] sub-score: `1 - penalty(n_contradictions)` (same penalty as soundness — they are the same dimension surfaced both as a top-line and a sub-score per the schema; documented that they are intentionally equal in v1).
- Documented constants: `INFERRED_EDGE_WEIGHT = 0.5`. All "vacuous → 1.0" cases are explicit, never NaN.
- Verify: `pytest apollo/retired graph comparator/tests/test_scores.py -v`

### 5. Bisimilarity (bisimilarity.py) — harmonic mean

- [ ] Test first: `apollo/retired graph comparator/tests/test_bisimilarity.py`
- [ ] File: `apollo/retired graph comparator/bisimilarity.py` (NEW)
- `harmonic_mean(a: float, b: float) -> float`: `0.0 if (a + b) == 0 else 2 * a * b / (a + b)`. **Binding: returns 0.0 when `a + b == 0`, NEVER NaN** (§6.1 — a NaN in a `REAL NOT NULL` column poisons aggregates). No other branch.
- `bisimilarity_score(soundness: float, coverage: float) -> float` = `harmonic_mean(soundness, coverage)` (thin alias for readable call sites + a named seam).
- Verify: `pytest apollo/retired graph comparator/tests/test_bisimilarity.py -v`

### 6. Orchestration (core.py) — grade_attempt + GradeResult + COMPARISON_VERSION

- [ ] Test first: `apollo/retired graph comparator/tests/test_core.py`
- [ ] File: `apollo/retired graph comparator/core.py` (NEW)
- `COMPARISON_VERSION: str = "graph-compare-v1"` — the constant for the `apollo_graph_comparison_runs.comparison_version` column (a re-run at the same version is a supersede per the UNIQUE constraint). Single source of truth; WU-4B reads it off `GradeResult.comparison_version`.
- `@dataclass(frozen=True) GradeResult` — fields map 1:1 to `apollo_graph_comparison_runs` score columns:
  - `coverage_score: float`, `soundness_score: float`, `bisimilarity_score: float`
  - `node_coverage_score: float`, `edge_coverage_score: float`, `scoping_score: float`, `usage_score: float`, `procedure_order_score: float`, `dependency_score: float`, `contradiction_score: float`
  - `comparison_confidence: float` (==1.0 v1; carried, NOT the persisted `normalization_confidence` column — WU-4B supplies that from resolution method-caps)
  - `findings: tuple[Finding, ...]`
  - `comparison_version: str` (defaults to `COMPARISON_VERSION`)
- `grade_attempt(student_canonical: CanonicalGraph, reference_graph: ReferenceGraph) -> GradeResult` — the §6.4 steps 10/11/13 orchestration over PURE inputs (NO Neo4j read, NO resolver call — those are WU-4C):
  1. `coverage_result(...)` → `(coverage_score, winning_path, all_paths)` (step 10).
  2. `soundness_score(...)` (step 11).
  3. `compute_sub_scores(..., winning_path)`.
  4. `bisimilarity_score(soundness, coverage)` (step 13).
  5. Emit findings (the in-memory §2 finding set, NO event conversion):
     - `covered_finding` per covered key on the winning path (look up the S_norm node for evidence/confidence).
     - `missing_finding` per missing key on the WINNING path ONLY (never against a non-taken path — the max-over-paths guarantee that yields zero false missings).
     - `alternative_path_finding` when `winning_path.path_index != 0` (the student took a declared alternative).
     - `contradiction_finding` per `contradiction_nodes(...)` member.
     - `unsupported_extra_finding` per S_norm node whose key is neither a misconception nor in ANY reference path (matched nothing — honestly NOT a contradiction).
     - `unresolved_finding` per `student_canonical.unresolved_nodes` entry.
     - `matched_edge_finding` / `missing_edge_finding` (diagnostic-only) from the edge passes.
  6. `comparison_confidence = 1.0` (v1 binding).
  7. Return the frozen `GradeResult` (findings as a deterministically-ordered tuple: covered → missing → alternative_path → contradiction → unsupported_extra → unresolved → matched_edge → missing_edge, each group sorted by canonical_key/message for reproducibility).
- **Assert no event kinds leak:** findings only ever carry `FindingKind` values; there is no `events`/`event_kind` field anywhere on `GradeResult` or `Finding`. (A test asserts this structurally.)
- Verify: `pytest apollo/retired graph comparator/tests/test_core.py -v`

### 7. Package seam (__init__.py) — re-exports

- [ ] Test: extend `apollo/retired graph comparator/tests/test_package_seam.py` (`_EXPECTED` set + same-object checks) — do NOT rewrite the file.
- [ ] File: `apollo/retired graph comparator/__init__.py` (EDIT — add imports + `__all__` entries)
- Add re-exports: `grade_attempt`, `GradeResult`, `Finding`, `FindingKind`, `COMPARISON_VERSION` (from `core` + `findings`). Keep all existing WU-4A1 exports.
- Verify: `pytest apollo/retired graph comparator/tests/test_package_seam.py -v`

### 8. Owner-doc reconciliation (drift contract)

- [ ] File: `docs/architecture/apollo.md` (EDIT — same commit as the code)
- Extend the `apollo/retired graph comparator/` module-map row (line 36) with the WU-4A2 scoring modules + the `grade_attempt`/`GradeResult` API. Add `grade_attempt` to "Key service entry points". Add `GradeResult`/`Finding`/`FindingKind`/`COMPARISON_VERSION` to "Core types". Set `last_verified: 2026-06-17` (already 2026-06-17 in frontmatter — confirm it stays).
- The `owns:` glob `apollo/retired graph comparator/**` already exists (line 6) — no glob change needed.
- Verify: `grep -n "grade_attempt\|GradeResult" docs/architecture/apollo.md` returns the new lines.

### 1. Findings model (findings.py) — the in-memory vocabulary

### 2. Coverage pass (coverage.py) — R ⊑ S, max over paths

### 3. Soundness pass (soundness.py) — S ⊑ R, contradictions only

### 4. Sub-scores (scores.py) — the 7 rubric sub-scores

### 5. Bisimilarity (bisimilarity.py) — harmonic mean

### 6. Orchestration (core.py) — grade_attempt + GradeResult + COMPARISON_VERSION

### 7. Package seam (__init__.py) — re-exports

### 8. Owner-doc reconciliation (drift contract)

## Public signatures (full)

```python
# apollo/retired graph comparator/findings.py
from enum import StrEnum
from dataclasses import dataclass
from apollo.retired graph comparator.canonical import CanonicalNode, CanonicalEdge

class FindingKind(StrEnum):
    COVERED_NODE = "covered_node"
    MISSING_NODE = "missing_node"
    MATCHED_EDGE = "matched_edge"
    MISSING_EDGE = "missing_edge"
    UNSUPPORTED_EXTRA = "unsupported_extra"
    CONTRADICTION = "contradiction"
    UNRESOLVED = "unresolved"
    ALTERNATIVE_PATH = "alternative_path"

@dataclass(frozen=True)
class Finding:
    kind: FindingKind
    canonical_key: str | None = None
    student_node_ids: tuple[str, ...] = ()
    reference_node_ids: tuple[str, ...] = ()
    evidence_spans: tuple[str, ...] = ()
    score: float | None = None
    confidence: float | None = None
    message: str | None = None

def covered_finding(node: CanonicalNode) -> Finding: ...
def missing_finding(ref_node: CanonicalNode) -> Finding: ...
def contradiction_finding(node: CanonicalNode) -> Finding: ...
def unsupported_extra_finding(node: CanonicalNode) -> Finding: ...
def unresolved_finding(node_id: str, surface: str) -> Finding: ...
def matched_edge_finding(edge: CanonicalEdge) -> Finding: ...
def missing_edge_finding(edge: CanonicalEdge) -> Finding: ...
def alternative_path_finding(path_index: int, canonical_keys: tuple[str, ...]) -> Finding: ...

# apollo/retired graph comparator/coverage.py
@dataclass(frozen=True)
class PathCoverage:
    path_index: int
    covered_keys: tuple[str, ...]
    missing_keys: tuple[str, ...]
    score: float

def coverage_per_path(student: CanonicalGraph, reference: ReferenceGraph) -> tuple[PathCoverage, ...]: ...
def coverage_result(student: CanonicalGraph, reference: ReferenceGraph) -> tuple[float, PathCoverage, tuple[PathCoverage, ...]]: ...

# apollo/retired graph comparator/soundness.py
MISCONCEPTION_KEY_PREFIX: str = "misc."
CONTRADICTION_UNIT_PENALTY: float = 0.5
def is_misconception_key(key: str) -> bool: ...
def contradiction_nodes(student: CanonicalGraph) -> tuple[CanonicalNode, ...]: ...
def contradiction_penalty(n: int) -> float: ...   # min(1.0, n * CONTRADICTION_UNIT_PENALTY)
def soundness_score(student: CanonicalGraph) -> float: ...

# apollo/retired graph comparator/scores.py
INFERRED_EDGE_WEIGHT: float = 0.5
@dataclass(frozen=True)
class SubScores:
    node_coverage: float
    edge_coverage: float
    scoping: float
    usage: float
    procedure_order: float
    dependency: float
    contradiction: float

def compute_sub_scores(student: CanonicalGraph, reference: ReferenceGraph, winning_path: PathCoverage) -> SubScores: ...

# apollo/retired graph comparator/bisimilarity.py
def harmonic_mean(a: float, b: float) -> float: ...          # 0.0 when a+b==0, never NaN
def bisimilarity_score(soundness: float, coverage: float) -> float: ...

# apollo/retired graph comparator/core.py
COMPARISON_VERSION: str = "graph-compare-v1"
@dataclass(frozen=True)
class GradeResult:
    coverage_score: float
    soundness_score: float
    bisimilarity_score: float
    node_coverage_score: float
    edge_coverage_score: float
    scoping_score: float
    usage_score: float
    procedure_order_score: float
    dependency_score: float
    contradiction_score: float
    comparison_confidence: float          # ==1.0 in v1 (carried, NOT persisted by name)
    findings: tuple[Finding, ...]
    comparison_version: str = COMPARISON_VERSION

def grade_attempt(student_canonical: CanonicalGraph, reference_graph: ReferenceGraph) -> GradeResult: ...
```

Backward-compat: this unit ADDS only. No existing WU-4A1 signature changes; `__init__.py` keeps every existing export and appends. `canonical.py`, `candidates.py`, `validator.py`, `problem_inputs.py` are NOT edited.

## Full test list

All tests are pure in-memory unit tests over hand-built `CanonicalGraph`/`ReferenceGraph` dataclasses. **No LLM, no Neo4j, no Postgres, no resolver run — there is nothing to mock** (the score-math has zero external deps; this is the design property that makes the 95% patch gate achievable without containers). A shared `apollo/retired graph comparator/tests/_builders.py` provides `_cnode`, `_cedge`, `_snorm`, `_rnode`, `_rgraph`, `_path` factory helpers.

### test_findings.py
- `test_finding_kind_enum_matches_models_finding_kinds` — `{k.value for k in FindingKind} == set(models.FINDING_KINDS)` (the enum is the authoritative set, no drift).
- `test_covered_finding_carries_evidence_and_confidence` — `covered_finding(node)` has kind COVERED_NODE, `canonical_key`==node key, `evidence_spans`==node spans, `confidence`==node confidence, `student_node_ids`==node.source_node_ids.
- `test_missing_finding_score_zero_no_event` — `missing_finding(ref)` has kind MISSING_NODE, score 0.0, `reference_node_ids` set, and NO event field on the dataclass (assert `not hasattr(Finding, "event_kind")` via fields).
- `test_contradiction_finding_carries_misconception_key` — kind CONTRADICTION, score 0.0, `canonical_key` is the `misc.*` key, evidence carried.
- `test_unsupported_extra_finding_no_penalty_marker` — kind UNSUPPORTED_EXTRA, score is None (no penalty), evidence carried.
- `test_unresolved_finding_from_node_id_surface` — kind UNRESOLVED, `student_node_ids`==(node_id,), evidence==(surface,).
- `test_matched_and_missing_edge_findings_diagnostic_only` — kinds MATCHED_EDGE/MISSING_EDGE, `message` carries `"<from> -<TYPE>-> <to>"` + provenance, edge-id tuples empty (diagnostic only).
- `test_alternative_path_finding_records_index_and_keys` — kind ALTERNATIVE_PATH, message names the path index, canonical_keys carried.
- `test_finding_is_frozen` — assigning to a field raises `FrozenInstanceError`.

### test_coverage.py
- `test_full_coverage_single_path` — S_norm covers all path keys → score 1.0, no missing.
- `test_partial_coverage_single_path` — S_norm covers 2 of 4 path keys → score 0.5, missing == the 2 absent keys.
- `test_empty_student_graph_coverage_zero` — empty S_norm, one path → every path scores 0.0, `coverage_result` max 0.0 (NO NaN; the path has keys so denominator>0).
- `test_max_over_two_paths_picks_better` — SYNTHETIC two-path problem; S_norm matches path B fully, path A partially → `coverage_result` returns path B's 1.0 and the winning `PathCoverage.path_index==1`. (Covers the max-over-paths branch — the §6.6 "valid alternative path" case.)
- `test_valid_alternative_path_zero_false_missings` — student fully on path B; the winning path's `missing_keys`==() even though path A keys are absent from S_norm (the binding "never punished against the authored path").
- `test_tie_broken_by_lowest_path_index` — both paths score equally → winner is path_index 0 (determinism).
- `test_coverage_is_pure_same_input_same_output` — two calls equal.

### test_soundness.py
- `test_no_contradiction_soundness_one` — S_norm with only reference-key nodes → soundness 1.0.
- `test_empty_student_soundness_one_vacuous` — empty S_norm → soundness 1.0 (vacuously sound; §6.1).
- `test_single_contradiction_penalized` — one `misc.*` node → soundness 0.5 (`1 - 0.5`).
- `test_two_contradictions_floor_zero` — two `misc.*` nodes → soundness 0.0 (penalty capped at 1.0).
- `test_unsupported_extra_zero_soundness_penalty` — S_norm has a node whose key is NOT in any reference path and NOT a misconception → soundness 1.0 (the "reference omits a valid assumption the student states" case → unsupported_extra, ZERO penalty).
- `test_unresolved_nodes_zero_soundness_penalty` — `unresolved_nodes` populated, zero `misc.*` nodes → soundness 1.0.
- `test_misconception_not_in_canon_misc_is_not_contradiction` — a wrong-but-unenumerated claim resolves to a NON-`misc.` key (or stays unresolved) → NOT counted as a contradiction → soundness 1.0 (honest non-detection; §6.11 "misconception not present in canon.misc.*").
- `test_is_misconception_key_prefix` — `is_misconception_key("misc.density_ignored")` True; `is_misconception_key("cond.incompressibility")` False; `is_misconception_key("misc")` False (needs the dot).

### test_scores.py
- `test_node_coverage_equals_winning_path_ratio` — matches the winning `PathCoverage.score`.
- `test_edge_coverage_explicit_full_match` — all reference edges matched by EXPLICIT S_norm edges → 1.0.
- `test_edge_coverage_inferred_weighted_lower` — a reference edge matched ONLY by an inferred S_norm edge counts `INFERRED_EDGE_WEIGHT` (0.5), so edge_coverage < 1.0 (explicit outweighs inferred).
- `test_edge_coverage_missing_edge_lowers_score` — a reference edge with no S_norm match lowers the ratio.
- `test_scoping_score_matches_scopes_edges` — SCOPES dimension isolated.
- `test_usage_score_matches_uses_edges` — USES dimension isolated.
- `test_procedure_order_penalizes_true_inversion` — reference path orders A before B; S_norm has a PRECEDES edge B→A (reverse) → procedure_order < 1.0 (penalize the inversion).
- `test_procedure_order_no_penalty_for_missing_stated_order` — student states NO PRECEDES edges at all though both A,B present → procedure_order == 1.0 (absence is NOT penalized; §6.2 binding).
- `test_dependency_score_direction_loose` — a DEPENDS_ON edge matches the reference pair regardless of direction; lowest-weight dimension computed.
- `test_contradiction_subscore_equals_soundness_dimension` — `SubScores.contradiction == 1 - contradiction_penalty(n)` (same dimension surfaced as a sub-score).
- `test_vacuous_dimensions_are_one_not_nan` — reference with zero edges of a type → that sub-score is 1.0, never NaN/ZeroDivisionError.
- `test_sub_scores_pure_same_input_same_output` — determinism.

### test_bisimilarity.py
- `test_harmonic_mean_typical` — `harmonic_mean(1.0, 0.5) == pytest.approx(2/3)`.
- `test_harmonic_mean_both_one` — `== 1.0`.
- `test_harmonic_mean_zero_sum_returns_zero_not_nan` — `harmonic_mean(0.0, 0.0) == 0.0` AND `not math.isnan(...)` (the binding degenerate; §6.1).
- `test_harmonic_mean_one_zero` — `harmonic_mean(1.0, 0.0) == 0.0` (a+b≠0 but product 0 → 0; no NaN).
- `test_bisimilarity_score_delegates_to_harmonic_mean` — alias equals `harmonic_mean(s, c)`.

### test_core.py — the end-to-end pure-score fixtures (§6.11 subset that needs NO audit/abstention/events)
- `test_grade_result_fields_map_to_run_columns` — `GradeResult` has exactly the 10 score fields named identically to `apollo_graph_comparison_runs` columns + `comparison_confidence` + `findings` + `comparison_version`; assert via dataclass field names vs the ORM column set (regression guard against schema drift).
- `test_comparison_confidence_is_one_in_v1` — `grade_attempt(...).comparison_confidence == 1.0`.
- `test_comparison_version_constant` — `GradeResult().comparison_version == COMPARISON_VERSION == "graph-compare-v1"`.
- `test_empty_student_graph_degenerate` — empty S_norm vs a real R_norm → coverage 0.0, soundness 1.0, bisimilarity 0.0, NO NaN anywhere (math.isnan False on all 10 scores) (§6.1 binding).
- `test_correct_answer_thin_explanation` — S_norm covers ~1 of N keys, no misconception → LOW coverage, HIGH soundness (1.0), bisimilarity low, zero contradiction findings (§6.11 "correct final answer, thin explanation").
- `test_wrong_answer_mostly_correct_concepts` — S_norm covers several keys AND has one `misc.*` node → covered findings + exactly one contradiction finding; soundness penalized (0.5) (§6.11 "wrong final answer, mostly correct concepts").
- `test_reference_omits_valid_assumption_student_states` — S_norm has an extra non-reference non-misconception node → one UNSUPPORTED_EXTRA finding, ZERO soundness penalty (soundness 1.0) (§6.11).
- `test_misconception_not_in_bank_is_unsupported_extra_not_contradiction` — extra node with a non-`misc.` key matching nothing → UNSUPPORTED_EXTRA, NOT CONTRADICTION (§6.11 honest non-detection).
- `test_valid_alternative_path_via_path_b` — SYNTHETIC two-path problem, student fully on path B → coverage 1.0, an ALTERNATIVE_PATH finding present, ZERO MISSING_NODE findings (max-over-paths; §6.11 "valid alternative solution path").
- `test_missing_findings_only_against_winning_path` — student partial on the winning path → MISSING_NODE findings list exactly the winning path's missing keys, never another path's keys.
- `test_no_event_kinds_in_findings` — every finding's `kind` is a `FindingKind` member; `GradeResult`/`Finding` expose NO `event`/`event_kind` field (assert via `dataclasses.fields`) — sub-scores NEVER produce events (binding; §6.2 edge demotion + §6.5 is WU-4B).
- `test_edge_gaps_are_diagnostic_findings_only` — a missing reference edge produces a MISSING_EDGE finding (diagnostic) and lowers `edge_coverage_score`, but produces NO node/event finding and does NOT alter coverage/soundness/bisimilarity (edges diagnostic-grade only; §6.2).
- `test_procedure_order_inversion_end_to_end` — reverse-ordered PRECEDES in S_norm → `procedure_order_score` < 1.0 in the GradeResult; a missing order → 1.0 (re-asserts the §6.2 rule through the orchestrator).
- `test_findings_deterministically_ordered` — two calls produce an equal `findings` tuple (frozen + sorted).
- `test_grade_attempt_is_pure` — two calls on identical inputs return equal `GradeResult`.

### test_package_seam.py (extend, do not rewrite)
- Extend `_EXPECTED` with `grade_attempt`, `GradeResult`, `Finding`, `FindingKind`, `COMPARISON_VERSION`.
- `test_all_matches_exports` already asserts `sorted(__all__) == sorted(_EXPECTED)` (now includes the new names).
- Add same-object checks: `gc.grade_attempt is core.grade_attempt`, `gc.Finding is findings.Finding`, etc.

## Contradiction-detection contract

- A contradiction is detected STRUCTURALLY, never by LLM judgment (§6.2): an S_norm `CanonicalNode` whose `canonical_key` starts with `MISCONCEPTION_KEY_PREFIX = "misc."`. The misconception competition already ran at resolve-time (§5, WU-3C2) — a node reaching S_norm with a `misc.*` key IS a resolved contradiction.
- Key-reality note: the spec prose says `canon.misc.*`; the ACTUAL minted key prefix (verified in `apollo/subjects/.../misconceptions.json` — `misc.density_ignored`, `misc.pressure_velocity_same_direction`) is `misc.`. The single `MISCONCEPTION_KEY_PREFIX` constant documents this once. (`learner_model_seed.misconceptions_to_entities` mints `kind="misconception"` with `key` = the `misc.*` source key.)
- `grade_attempt` takes ONLY `(student_canonical, reference_graph)` — it does NOT receive the `Candidate` set, so `is_misconception`/`opposes_key` are NOT available at score time. This is fine: detection needs only the key prefix (already on `CanonicalNode`), and the `opposes` link is needed ONLY for the §6.5 `corrected`/turn-order event rows, which are WU-4B. WU-4A2 therefore emits a `contradiction` FINDING (not an event) and never needs `opposes`.
- Unknown wrong claims (a misconception NOT enumerated in the bank) resolve to a non-`misc.` key or stay unresolved → they are `unsupported_extra` or `unresolved`, NEVER `contradiction` (honest non-detection; §6.10 risk 4, §6.11).

## Degenerate / NaN contract

- `harmonic_mean(a, b)` returns `0.0` when `a + b == 0`, else `2ab/(a+b)`. NEVER NaN (a NaN written to `REAL NOT NULL` poisons aggregates; §6.1). Tested directly + through `grade_attempt`.
- Empty student graph (`CanonicalGraph(nodes=(), edges=(), unresolved_nodes=(), dropped_edge_count=0)`) → coverage 0.0 (every path has ≥1 key, all uncovered), soundness 1.0 (zero contradictions, vacuously sound), bisimilarity 0.0 (`harmonic_mean(1.0, 0.0)`). Binding §6.1.
- Every sub-score with an empty denominator (reference has zero edges of that type) returns 1.0 (vacuous), never a `ZeroDivisionError` or NaN. Each such branch has a dedicated assertion in `test_vacuous_dimensions_are_one_not_nan`.
- `ReferenceGraph.paths` is guaranteed length ≥ 1 by WU-4A1's `build_reference_canonical` + `validate_reference` (the §6.6 gate blocks an empty declared-path list upstream), so `coverage_result` never faces an empty path list. A defensive guard + `# pragma: no cover` note is acceptable if the executor wants belt-and-suspenders, but the documented contract is "paths ≥ 1 always".

## Edge "diagnostic-only, never events" contract

- Edges contribute to `edge_coverage_score`, `scoping_score`, `usage_score`, `procedure_order_score`, `dependency_score` (rubric sub-scores) and to MATCHED_EDGE/MISSING_EDGE FINDINGS (diagnostic feedback) ONLY. They NEVER produce a learner-model event and NEVER alter coverage/soundness/bisimilarity. (§6.2 edge demotion: edge recall is unproven; an edge gap must not move Layer 3.)
- `explicit` provenance outweighs `inferred`: an inferred-only edge match counts `INFERRED_EDGE_WEIGHT = 0.5`. `dependency_score` is the loosest (any→any, direction-loose) and is documented as the lowest-weight dimension for the downstream rubric aggregation (the weighting itself is WU-4C; here it is just computed).
- `test_no_event_kinds_in_findings` + `test_edge_gaps_are_diagnostic_findings_only` enforce this structurally — no `event_kind` field exists anywhere in this unit, and an edge gap provably does not change the three top-line scores.

## Owner-doc updates

File: `docs/architecture/apollo.md` (the `apollo/**` owner; `owns:` already covers `apollo/retired graph comparator/**`). Same commit as the code (drift contract).

1. **Module-map row (line 36, `apollo/retired graph comparator/`)** — append after the WU-4A1 description: list `coverage.py`, `soundness.py`, `scores.py`, `bisimilarity.py`, `findings.py`, `core.py` as the **§6 grading-core COMPARE half (WU-4A2)**; state that it consumes the two WU-4A1 canonical graphs and produces `GradeResult` + in-memory `Finding`s; reiterate the binding rules (coverage max-over-paths; soundness contradictions-only via `misc.` key; 7 sub-scores; procedure_order penalizes only inversions; edges diagnostic-only/explicit>inferred; bisimilarity harmonic-mean with `a+b==0 → 0` no-NaN); and the SEAM vs WU-4B (no audit, no abstention, no finding→event, no persistence).
2. **Key service entry points (after line 76)** — add:
   `apollo.retired graph comparator.grade_attempt(student_canonical: CanonicalGraph, reference_graph: ReferenceGraph) -> GradeResult` (WU-4A2; the §6.4 steps 10/11/13 over PURE inputs — coverage max-over-paths, soundness contradictions-only, 7 sub-scores, bisimilarity; emits in-memory `Finding`s; `comparison_confidence==1.0` v1; NO Neo4j/PG/LLM, NO event conversion — that is WU-4B; the single callable WU-4C wires into `done.py`).
3. **Core types (after line 90)** — add `GradeResult` (the 10 score fields named 1:1 to `apollo_graph_comparison_runs` columns + `comparison_confidence` + `findings` + `comparison_version`; `comparison_confidence` is the score-math's own value, the persisted column is `normalization_confidence` supplied by WU-4B), `Finding`/`FindingKind` (the §2 finding-kind set == `models.FINDING_KINDS`), `COMPARISON_VERSION` (`"graph-compare-v1"`).
4. **`last_verified`** — confirm `2026-06-17` in the frontmatter (already set; if a later edit bumped it, keep 2026-06-17).

## Downstream consumers

- **WU-4B** (next in the stack): consumes `GradeResult.findings` to run the transcript audit, apply abstention gates, convert findings→events via the §6.5 decision table, and persist the run/findings rows. Reads `COMPARISON_VERSION` for the runs row + supersede semantics. Persists `normalization_confidence` (from resolution method-caps) — NOT a WU-4A2 concern.
- **WU-4C**: imports `grade_attempt` and wires it into `done.py` (re-orchestration), reading the frozen student graph from Neo4j and calling `resolve_attempt` to build the `CanonicalGraph` first. Also maps `GradeResult` sub-scores/findings into `compute_rubric` input.
- **No frontend consumer** for this unit. The `apollo/retired graph comparator` package is not HTTP-mounted (the Done route is WU-4C). A frontend grep for the URL patterns is N/A — `grade_attempt` is an internal Python callable, no new route. (Confirmed: `apollo/api.py` has no `grade`/`retired graph comparator` route, and the only Done route `POST /apollo/sessions/{id}/done` still calls the legacy `compute_coverage`/`compute_rubric` shadow path until WU-4C rewires it.)

## Risks

- **HIGH — penalty constants are v1 calibration knobs, fixed here.** `CONTRADICTION_UNIT_PENALTY = 0.5` and `INFERRED_EDGE_WEIGHT = 0.5` are hand-set v1 values (the spec §6.6 says gate values are "hand-set v1, tuned from calibration data"). They are CONCRETE here (not "TBD"); the executor MUST use exactly these. They are documented module constants so WU-4B/calibration can retune in one place. Confidence: the VALUES may change post-calibration, but the STRUCTURE (linear capped penalty, weighted edge match) is binding.
- **MEDIUM — misconception key prefix `misc.` vs spec `canon.misc.*`.** Detection keys off `"misc."` (the actual mint, verified). If a future course mints a non-misconception key that happens to start `misc.` this would false-positive; mitigated because Layer-1 entity kinds are allowlisted and `misc.*` is reserved for `kind="misconception"` (`learner_model_seed.misconceptions_to_entities`). Documented in the contradiction contract.
- **MEDIUM — procedure-order source of truth.** WU-4A1's `build_reference_canonical` emits dependency edges as `DEPENDS_ON` (not `PRECEDES`), so true reference ORDER for the procedure_order sub-score is derived from the `ReferencePathView.canonical_keys` SEQUENCE (over procedure_step keys), and inversions are checked against S_norm's `PRECEDES` edges. This is the only place the path sequence (not just its set) is load-bearing for a sub-score. Flagged so the executor reads the reference order from the path, not from a non-existent reference PRECEDES edge set. Confidence HIGH that the path sequence is the right source (it is the declared solution order).
- **LOW — `alternative_path` finding in a single-path v1.** v1 authors one path, so `alternative_path_finding` only fires when `winning_path.path_index != 0`. The synthetic two-path tests (`test_max_over_two_paths_picks_better`, `test_valid_alternative_path_via_path_b`) cover that branch for the 95% gate even though the bernoulli fixtures are single-path. The schema (`ReferenceGraph.paths` tuple) already supports multi-path from day one.
- **LOW — `node_coverage` vs `coverage` duplication.** `node_coverage_score` equals `coverage_score` in v1 (coverage IS the node dimension; there is no separate non-node coverage). Documented as intentional; both columns exist in the schema and both are populated. The `contradiction` sub-score likewise equals the soundness contradiction dimension — same note.
- **LOW — diff-cover compare base.** Patch coverage is measured vs `feat/apollo-kg-wu4a1-canonical-build` (NOT `origin/staging`), because this branch stacks on 4A1; comparing to staging would count all of 4A1's lines as "changed". The executor runs `diff-cover coverage.xml --compare-branch=feat/apollo-kg-wu4a1-canonical-build --fail-under=95` (target 100%).

## Out-of-scope boundaries (this unit)

- **NO DB migration.** Migration 026 already shipped `apollo_graph_comparison_runs`/`_findings` + the ORM (`GraphComparisonRun`/`GraphComparisonFinding`). This unit writes NO migration and touches NO `database/migrations/`. (Justifies the absent "DB migration" layer.)
- **NO persistence.** Writing `GradeResult` → `apollo_graph_comparison_runs` and findings → `apollo_graph_comparison_findings` is WU-4B. No `normalization_confidence` computation here (WU-4B supplies it from resolution method-caps). No Postgres, no SQLAlchemy import in this unit.
- **NO transcript audit, NO abstention gates, NO finding→event conversion (§6.5), NO §6.11 executable fixture corpus.** All WU-4B. WU-4A2 produces the in-memory `Finding`s those start from. The §6.11 fixtures requiring audit/abstention/events (parser-miss→audit, conflicting-statements turn order, high-unresolved→abstention) are DEFERRED; only the pure-score subset (alternative path, thin explanation, wrong-answer-correct-concepts, omitted assumption, misconception-not-in-bank, empty graph, procedure inversion) is tested here.
- **NO Neo4j read, NO resolver call, NO LLM call.** `grade_attempt` takes already-canonical inputs. The `store.read_graph` + `resolve_attempt` wiring is WU-4C. The score-math is PURE.
- **Do NOT touch:** `apollo/handlers/done.py`, `apollo/overseer/coverage.py`, `apollo/overseer/rubric.py`, `apollo/api.py`, and WU-4A1's already-landed `canonical.py`/`validator.py`/`problem_inputs.py` (EXCEPT the `__init__.py` re-export extension). No new packages, no new dependencies.

## Deviations I'd allow the executor

- **Penalty-function shape**, if a test reveals the linear-capped form is awkward: the executor may use a different MONOTONE non-increasing penalty (e.g. `1 - 0.5**n`) PROVIDED single-contradiction → 0.5, two+ → near/at 0, zero → 1.0 (the tested anchors hold). Document the choice in the module.
- **Finding ordering key**, as long as it is deterministic and the `test_findings_deterministically_ordered` anchor holds (the exact group order is a readability choice, not a contract).
- **Helper decomposition** inside each module (extra private `_helpers`) — fine, keep modules < 200 lines and pure.
- **Splitting `coverage.py`'s `PathCoverage` selection** into a tiny `_select_winning_path` helper if it aids the multi-path test — fine.
- **`StrEnum` vs `Enum` for `FindingKind`** — `StrEnum` preferred (matches `EdgeType`; lets `finding.kind == "covered_node"` work for WU-4B's persistence string column), but `Enum` with explicit `.value` is acceptable if the `== FINDING_KINDS` test still passes.
- **NOT negotiable:** the harmonic-mean `a+b==0 → 0` no-NaN rule; coverage = max-over-paths; soundness penalizes contradictions ONLY (unsupported/unresolved zero penalty); edges never produce events; `comparison_confidence == 1.0` in v1; `GradeResult` field names matching the runs columns 1:1; tests written FIRST and genuinely failing before implementation.
