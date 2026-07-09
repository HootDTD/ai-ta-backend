# Plan: Apollo KG WU-4C2 — calibration harness (§6.7) + constrained diagnostics (§6.8) + rubric mapping (§6.4)

**Goal:** Extend the WU-4C1 shadow Done chain so that, while shadow is on, it ALSO computes (a) the graph-sim rubric from findings, (b) calibration metrics comparing the shadow grade vs the OLD student-facing grade, and (c) a constrained findings-only diagnostic; gate PROMOTION of those to student-facing behind a dormant `APOLLO_GRAPH_SIM_LIVE_ENABLED` flag that stays OFF.
**Architecture:** Pure helpers in `apollo/grading/` (calibration, rubric_mapping, constrained diagnostic with an injected LLM callable) + a thin extension of the `done_grading.py` shadow chain + a dormant live-promote flag + LIVE-off byte-identical guard in `done.py`. NO DB schema change, NO events, NO mastery writes, NO Neo4j. PURE + LLM-MOCK only.
**Tech stack:** Python 3.12 / FastAPI, SQLAlchemy async (untouched here), pytest (`.venv/Scripts/python.exe -m pytest`), `class`-free frozen `@dataclass` value objects, diff-cover ≥95% vs `feat/apollo-kg-wu4c1-done-shadow-orchestration`.

---
provides:
  - apollo.grading.compute_calibration_metrics — shadow-vs-old agreement/divergence value object (CalibrationMetrics)
  - apollo.grading.findings_to_rubric_input — ShadowGradeResult -> compute_rubric coverage/misconception_scores/reference_nodes shape (RubricMappingInput)
  - apollo.grading.build_graph_sim_rubric — calls compute_rubric on the mapped input -> the graph-sim candidate rubric dict
  - apollo.grading.generate_constrained_diagnostic — §6.8 findings-only diagnostic (injectable llm callable) + post-check
  - apollo.handlers.done.APOLLO_GRAPH_SIM_LIVE_ENABLED flag accessor (_graph_sim_live_enabled) — dormant, OFF
  - ShadowGradeResult is EXTENDED on done_grading to additionally surface graph-sim rubric/metrics/diagnostic (calibration fields)
consumes:
  - apollo.handlers.done_grading.ShadowGradeResult (run_id, grade:GradeResult, audited:AuditedGrade, normalization_confidence, reference_graph_hash, opposes_map, turn_order)
  - apollo.graph_compare.core.GradeResult (10 *_score fields + findings tuple)
  - apollo.grading.audited_grade.AuditedGrade (findings, abstained, suppressed_event_kinds)
  - apollo.graph_compare.findings.Finding / FindingKind
  - apollo.graph_compare.canonical.ReferenceGraph / CanonicalNode (graph-sim reference nodes)
  - apollo.overseer.rubric.compute_rubric (FROZEN — only CALLED), score_to_letter, LETTER_BANDS
  - the OLD student-facing rubric dict computed in done.py (compute_coverage -> compute_rubric)
depends_on:
  - WU-4C1 (feat/apollo-kg-wu4c1-done-shadow-orchestration) — the shadow chain + ShadowGradeResult must land first
  - NO db plan — migration 028 is free but UNUSED (no schema change in this unit)
---

## Overview

WU-4C1 (#36, DONE) wired the SHADOW Done chain `run_graph_simulation` that produces the frozen
`ShadowGradeResult` (run_id + GradeResult + AuditedGrade + normalization_confidence +
reference_graph_hash + opposes_map + turn_order). WU-4C2 EXTENDS that chain — entirely INSIDE the
shadow branch — to compute three NEW pure artifacts from the already-frozen `ShadowGradeResult`:

1. **Rubric mapping (§6.4 "Rubric mapping" task, lines 752-755).** The old `compute_rubric` consumes
   a `coverage` dict (`per_step` + `procedure_scores`) + `reference_nodes` + `misconception_scores`.
   `findings_to_rubric_input` maps `AuditedGrade.findings` + the graph-sim `ReferenceGraph` into that
   exact shape, so `compute_rubric` yields the **graph-sim rubric** — the candidate student-facing
   grade WHEN live is flipped. We MIRROR the OLD path's `compute_rubric(coverage, reference_nodes,
   misconception_scores=...)` call shape (done.py:277-281), only with graph-sim-derived inputs.

2. **Calibration metrics (§6.7).** `compute_calibration_metrics(shadow_rubric, old_rubric)` produces a
   frozen `CalibrationMetrics` value object: letter-grade agreement (do the two overall letters match),
   per-axis score deltas, overall score delta, and a coarse divergence flag. A human reads these later
   to decide whether to flip LIVE. NO new column — these are LOGGED + reused via JSONB
   (WU-4C key_decision #6); a dedicated calibration table is a later concern.

3. **Constrained diagnostic (§6.8).** `generate_constrained_diagnostic` is a NEW module distinct from
   the OLD `apollo/overseer/diagnostic.py` (which is FROZEN and stays on the OLD path). It takes
   FINDINGS ONLY (from `AuditedGrade`) — never the free transcript — runs an INJECTABLE llm callable
   (default real, MOCKED in every test), and applies a POST-CHECK that the narrative is grounded in
   those findings (claims no covered-where-findings-missing, introduces no misconceptions absent from
   findings). A failed post-check regenerates once, then falls back to a deterministic template
   rendering of the findings (NO-FALLBACK: logged, visible).

**Promotion flag.** `done.py` gains `APOLLO_GRAPH_SIM_LIVE_ENABLED` mirroring the shadow flag's
`os.environ.lower() in ("1","true","yes")` parsing. It STAYS OFF — flipped only after human
calibration, NEVER in this build. **LIVE OFF (always here):** the OLD `compute_coverage`/`compute_rubric`
grade + OLD `generate_diagnostic` narrative stay student-facing; the student-facing return is
byte-identical to WU-4C1's. **LIVE ON (future, BUILT but dormant + tested):** the graph-sim
rubric/diagnostic replace the student-facing `rubric`/`diagnostic_narrative`.

**Wiring rule:** metrics + rubric + diagnostic run INSIDE the shadow chain (so they only fire when
SHADOW is on); the LIVE flag gates only PROMOTION to student-facing, NOT computation. The unit does
NOT call `convert_findings_to_events`, write `apollo_mastery_events`/`apollo_learner_state` (WU-5A
boundary — asserted empty/uncalled), touch Neo4j, or run a container/DB/migration.

## Prior art (sibling modules)

- **Shadow flag + byte-identical guard:** `apollo/handlers/done.py:58-71` (`_GRAPH_SIM_SHADOW_FLAG`,
  `_graph_sim_shadow_enabled`) — the LIVE flag mirrors this EXACTLY (constant + accessor + env parse).
  Test pattern: `apollo/handlers/tests/test_done_shadow_flag.py:200-213`
  (`test_shadow_flag_parsing`, `test_flag_constant_name`) — the LIVE flag tests copy this verbatim.
- **Shadow chain extension point:** `apollo/handlers/done_grading.py:81-93` (`ShadowGradeResult`),
  `:144-251` (`run_graph_simulation`) — WU-4C2 adds fields to the frozen dataclass + computes them
  AFTER `persist_comparison_run`/commit (step 11), BEFORE the `return ShadowGradeResult(...)`.
  Test pattern: `apollo/handlers/tests/test_done_grading_unit.py` (`_all_callee_patches`,
  per-error pending forks, `test_no_mastery_events_written`).
- **compute_rubric call shape (the target to mirror):** `apollo/handlers/done.py:267-281`
  (`compute_coverage` -> `compute_rubric(coverage, reference_graph.nodes, misconception_scores=...)`).
  `compute_rubric` itself: `apollo/overseer/rubric.py:79-180` — reads `coverage["per_step"]`
  (`{node_id -> "covered"|...}`) + `coverage["procedure_scores"]` (`{node_id -> 0..1}`) + iterates
  `reference_nodes` for `.node_id`/`.node_type`; `misconception_scores={code -> 0.5|1.0}`.
- **OLD diagnostic (the thing we must NOT touch + the LLM-mock test pattern to copy):**
  `apollo/overseer/diagnostic.py:46-90` (`generate_diagnostic`, direct `OpenAI()` + try/except soft-fail).
  Test pattern: `apollo/overseer/tests/test_diagnostic.py:13-61` — `monkeypatch.setattr(diagnostic,
  "OpenAI", _Client)` / `@patch("apollo.overseer.diagnostic.OpenAI")`. WU-4C2's diagnostic does NOT
  use `OpenAI` directly — it takes an INJECTED `llm` callable (the cleaner seam), so tests inject a
  stub function, NOT a patched `OpenAI`.
- **Pure frozen value-object + builder convention:** `apollo/grading/event_model.py` (LearnerEvent),
  `apollo/grading/opposes.py` (tiny pure adapter), `apollo/grading/audited_grade.py`
  (frozen `AuditedGrade` + pure helpers). New modules follow this: small, `@dataclass(frozen=True)`,
  tuple fields, immutable returns.
- **Injected-LLM test stubs (no live API):** `apollo/grading/tests/_builders.py:148-179`
  (`found_audit_fn`/`notfound_audit_fn`/`raising_audit_fn`) — the diagnostic's `llm` stub mirrors
  these deterministic injected callables.
- **Letter/agreement helpers:** `apollo/overseer/rubric.py:46-66` (`LETTER_BANDS`, `score_to_letter`)
  — calibration reuses `score_to_letter` for letter agreement; NEVER reimplements bands.

## Structural prep (from neighborhood scan)

Files in the change path, with CBO (import count) / WMC (exported-callable count) proxies:

| File | Imports | Exported callables | Verdict |
|---|---|---|---|
| `apollo/handlers/done_grading.py` (EDIT) | ~22 import lines (grouped) | 5 (1 public `run_graph_simulation`, 4 `_private`) | OK — under thresholds; the EDIT adds ≤3 new imports (the 3 new grading helpers) + ≤2 fields + ≤1 private assembler. Stays < 800 lines (currently 271). |
| `apollo/handlers/done.py` (EDIT) | ~20 | `handle_done` + ~10 `_private` | OK — EDIT adds 1 flag constant + 1 accessor + a small LIVE-promote branch (≤15 lines). Stays well < 800 (currently 382). |
| `apollo/grading/__init__.py` (EDIT) | re-export hub | n/a (export list) | OK — additive exports only. |
| `apollo/grading/calibration.py` (NEW) | ~3 | 1 public + helpers | NEW small file. |
| `apollo/grading/rubric_mapping.py` (NEW) | ~5 | 2 public + helpers | NEW small file. |
| `apollo/grading/diagnostic.py` (NEW) | ~4 | 1 public + helpers | NEW small file. |

**Circular-import check:** `apollo/grading/` already imports `apollo/graph_compare/` and
`apollo/overseer/`? NO — `grading` imports `graph_compare`, `resolution`, `ontology`, `errors`,
`persistence`. The NEW `rubric_mapping.py` will import `apollo.overseer.rubric.compute_rubric` and
`apollo.overseer.rubric` does NOT import `apollo.grading` (it imports only `apollo.ontology` +
stdlib), so `grading -> overseer.rubric` introduces NO cycle. `diagnostic.py` (new) imports only
`apollo.graph_compare.findings` + stdlib (`logging`) + an injected callable — no overseer import,
no OpenAI import at module top is REQUIRED (the live default may lazily import `OpenAI` inside the
default callable to keep the module import-light and CI-safe). VERIFY before coding with the DI grep
below; if `overseer.rubric` ever imported `grading` it would be a cycle — it does not today.

**Coupling fan-in:** `done_grading.py` is imported by `done.py` only (fan-in 1). `done.py` is
imported by `apollo/handlers/__init__`/`api.py`/chat intent dispatch — a hub, but this EDIT is
purely additive (one dormant branch), so no fan-in risk is introduced.

**Verdict: neighborhood is clean.** No structural prep is prerequisite. Budget for prep = 0% of plan
steps.

## Layered tasks (TDD ORDER MATTERS)

> TDD discipline (binding): for EACH code module below, write the test file FIRST (RED), run it with
> `.venv/Scripts/python.exe -m pytest <test> -q` to confirm it fails for the RIGHT reason (the symbol
> doesn't exist yet — that is NOT an interpreter error), THEN implement to GREEN. No skip/xfail/
> assert-nothing. Every LLM call is an injected stub; no live OpenAI, no network, no container.

### 1. DB migration — NONE (justified)

No schema change. Calibration metrics are LOGGED + carried in-memory on the extended
`ShadowGradeResult`; the spec (WU-4C key_decision #6) says reuse JSONB / no dedicated calibration
table in this unit. Migration 028 is free but stays UNUSED. The `verify REAL-INFRA` gate must NOT
trigger (no changed files under `tests/database/`, `database/migrations/`, `apollo/knowledge_graph/`,
or `neo4j_client.py`). If a container is ever needed, STOP — the unit is mis-scoped.

### 2. Repository / data-access — NONE (justified)

No new DB read or write. All inputs are already-materialized in-memory objects: the
`ShadowGradeResult` (computed by the WU-4C1 chain) and the OLD rubric dict (already computed in
`done.py` before the shadow branch). No new query, no repository.

### 3. DTOs / value objects (the pure shapes)

Two NEW frozen dataclasses (created inside their owning modules, not a separate file):

- `RubricMappingInput` (in `rubric_mapping.py`): `coverage: dict`, `reference_nodes: tuple[RubricRefNode, ...]`,
  `misconception_scores: dict[str, float]`. The exact bag `compute_rubric` consumes.
- `RubricRefNode` (in `rubric_mapping.py`): the duck-typed reference-node adapter `compute_rubric`
  needs — `@dataclass(frozen=True)` with `node_id: str` and `node_type: str` ONLY (compute_rubric
  reads `r.node_id` + `r.node_type` and NOTHING else — verified rubric.py:112-133). This is the seam
  that bridges graph-sim `CanonicalNode.canonical_key` (which is NOT `node_id`) to the rubric's
  `Node`-shaped expectation WITHOUT importing the heavy `apollo.ontology.Node` pydantic union.
- `CalibrationMetrics` (in `calibration.py`): see task 5.
- `ConstrainedDiagnostic` (in `diagnostic.py`): `narrative: str`, `used_fallback: bool`,
  `regenerated: bool`, `post_check_failures: tuple[str, ...]` — the auditable diagnostic result.

Verify: `.venv/Scripts/python.exe -m pytest apollo/grading -q` (DTO import + field tests pass).

### 4. `apollo/grading/rubric_mapping.py` (NEW)

**Public signatures:**
```python
@dataclass(frozen=True)
class RubricRefNode:
    node_id: str
    node_type: str  # 'procedure_step'|'condition'|'simplification'|'equation'|'definition'|'variable_mapping'

@dataclass(frozen=True)
class RubricMappingInput:
    coverage: dict            # {"per_step": {node_id: "covered"|"missing"|"partial"}, "procedure_scores": {node_id: float}}
    reference_nodes: tuple[RubricRefNode, ...]
    misconception_scores: dict[str, float]

def findings_to_rubric_input(
    shadow_result: ShadowGradeResult, reference_graph: ReferenceGraph
) -> RubricMappingInput: ...

def build_graph_sim_rubric(
    shadow_result: ShadowGradeResult, reference_graph: ReferenceGraph
) -> dict: ...   # == compute_rubric(input.coverage, input.reference_nodes, misconception_scores=input.misconception_scores)
```

**Derivation (from `AuditedGrade.findings` + `ReferenceGraph`):** the reference Node identity in the
graph-sim world is `canonical_key` (CanonicalNode has no `node_id`). So we KEY the coverage dict on
`canonical_key` and build `RubricRefNode(node_id=ref.canonical_key, node_type=ref.node_type)` for
every `reference_graph.nodes` entry — `per_step`/`procedure_scores` are then keyed consistently on
the same `canonical_key`, satisfying `compute_rubric`'s `r.node_id` lookups.

- **per_step** (per reference node `canonical_key`):
  - COVERED_NODE finding for that key (including audit-upgraded covered, which is a COVERED_NODE with
    the `AUDIT_UPGRADE_MESSAGE` marker) -> `"covered"`.
  - MISSING_NODE finding for that key -> `"missing"`.
  - reference key with neither covered nor missing (off the winning path / unresolved) ->
    `"missing"` (conservative default; rubric's binary axes count only `== "covered"`).
- **procedure_scores** (only `procedure_step` reference nodes): for a covered procedure key use
  `finding.confidence` if not None else `1.0`; for a missing procedure key use `0.0`. (Rubric reads
  `procedure_scores.get(node_id, 0.0)` and clamps via `_finite_score`, so an absent key is safe but
  we populate it explicitly for auditability.)
- **misconception_scores** (`{bank_code -> 0.5 | 1.0}`, the rubric's existing contract,
  rubric.py:83-96): derive from CONTRADICTION findings on `misc.*` keys vs the §6.5 corrected/conflict
  outcome. Binding mapping mirroring `compute_rubric`'s doc:
  - a CONTRADICTION finding whose misconception key has a COVERED finding on the entity it opposes
    (via `shadow_result.opposes_map`) AND the covered came LATER (turn_order) = RESOLVED -> `1.0`.
  - a CONTRADICTION with no resolving covered (or contradiction-later) = detected-unresolved -> `0.5`.
  - never-detected misconceptions do not appear (no key) — rubric treats the axis as absent.
  Reuse the SAME turn-order comparison primitive shape WU-4B2 uses (`min` over `student_node_ids`,
  absent -> sentinel); a small private `_resolution_state(misc_finding, covered_finding, turn_order)`
  helper — do NOT import `apollo.grading.events` internals (keep it a tiny local pure function to
  avoid coupling to the event decision table; the table is for EVENTS, this is for the rubric AXIS).
  CAVEAT (RECON #7): `opposes_map` is structurally EMPTY today, so in practice every detected
  misconception maps to `0.5` (detected-unresolved). The code path for `1.0` MUST still exist and be
  unit-tested with a SYNTHETIC opposes_map fixture (so flipping opposes later needs no rubric change).

**Immutability:** builds NEW dicts/tuples; never mutates `shadow_result`/`reference_graph`.

Verify: `.venv/Scripts/python.exe -m pytest apollo/grading/tests/test_rubric_mapping.py -q`.

### 5. `apollo/grading/calibration.py` (NEW)

**Public signature:**
```python
@dataclass(frozen=True)
class AxisDelta:
    axis: str            # 'overall'|'procedure'|'justification'|'simplification'|'misconception_corrected'
    old_score: int | None
    shadow_score: int | None
    delta: int | None    # shadow - old; None if either axis absent on a side

@dataclass(frozen=True)
class CalibrationMetrics:
    letter_agreement: bool          # old["overall"]["letter"] == shadow["overall"]["letter"]
    old_overall_letter: str
    shadow_overall_letter: str
    overall_score_delta: int        # shadow.overall.score - old.overall.score
    axis_deltas: tuple[AxisDelta, ...]   # per axis present on EITHER side, deterministic order
    divergent: bool                 # |overall_score_delta| > DIVERGENCE_SCORE_THRESHOLD OR not letter_agreement
    comparison_version: str         # CALIBRATION_VERSION

def compute_calibration_metrics(*, old_rubric: dict, shadow_rubric: dict) -> CalibrationMetrics: ...
```

- `CALIBRATION_VERSION = "calibration-v1"` module constant (single source; carried for replay parity).
- `DIVERGENCE_SCORE_THRESHOLD = 10` (integer points; a hand-set v1 calibration knob, documented like
  `CONTRADICTION_UNIT_PENALTY`). `divergent` is the human-review trip wire (§6.7 "one bad grade
  becoming persistent memory is the failure this prevents").
- Letter agreement compares `["overall"]["letter"]`; reuse `apollo.overseer.rubric.score_to_letter`
  ONLY if a letter must be derived from a raw score — both rubric dicts already carry letters, so
  read them directly and do NOT recompute.
- Axis iteration is over the FIXED axis order from `rubric.AXIS_WEIGHTS` keys + `"overall"`; for each,
  read `{"score","present"}` from each side. An axis `present=False` on a side contributes
  `old_score=None`/`shadow_score=None` and `delta=None` (never a spurious 0-vs-0 "agreement").
- **NO new column / NO persistence here.** The metrics are returned in-memory; the LOGGING is done by
  the `done_grading.py` caller (task 7) via the existing module logger pattern.

**Immutability:** pure over two dicts; returns a frozen object; mutates neither input.

Verify: `.venv/Scripts/python.exe -m pytest apollo/grading/tests/test_calibration.py -q`.

### 6. `apollo/grading/diagnostic.py` (NEW — §6.8 constrained)

**Public signatures:**
```python
DiagnosticLLM = Callable[[DiagnosticRequest], str]   # injected; returns the narrative string

@dataclass(frozen=True)
class DiagnosticFinding:               # the SANITIZED, findings-only view handed to the LLM
    kind: str                          # finding kind value
    canonical_key: str | None
    evidence_spans: tuple[str, ...]    # quoted spans ONLY (no free transcript)
    message: str | None

@dataclass(frozen=True)
class DiagnosticRequest:
    findings: tuple[DiagnosticFinding, ...]
    covered_keys: tuple[str, ...]
    missing_keys: tuple[str, ...]
    misconception_keys: tuple[str, ...]

@dataclass(frozen=True)
class ConstrainedDiagnostic:
    narrative: str
    used_fallback: bool
    regenerated: bool
    post_check_failures: tuple[str, ...]

def generate_constrained_diagnostic(
    audited: AuditedGrade, *, llm: DiagnosticLLM | None = None
) -> ConstrainedDiagnostic: ...
```

- **Input = findings only.** Build `DiagnosticRequest` from `audited.findings`: covered_keys =
  COVERED_NODE keys, missing_keys = MISSING_NODE keys, misconception_keys = CONTRADICTION keys.
  `evidence_spans` are carried per finding (the resolver/audit quoted spans) — the raw transcript is
  NEVER passed. This is the structural guarantee from §6.8 ("structurally unable to re-grade").
- **Injected `llm`.** Default `llm = _default_diagnostic_llm` (a module function that lazily imports
  `OpenAI` inside its body — NOT at module top — and makes ONE chat call; mirrors the OLD diagnostic's
  shape but isolated). Every test injects a deterministic stub `llm`; no live API, no `OpenAI` import
  fires in CI. The default function carries its own try/except -> on any exception it raises a NAMED
  internal signal that triggers the template fallback (no soft-fail string leaking into the narrative).
- **POST-CHECK (binding, §6.8):** after the LLM returns, validate the narrative against the findings:
  1. it must NOT claim covered what findings mark missing (a `missing_key`'s display token appearing in
     a "you covered/taught/explained X" assertion) — checked with a conservative, case-insensitive
     keyword screen over the missing keys; a hit is a `post_check_failures` entry.
  2. it must NOT introduce a misconception not in `misconception_keys`.
  3. (soft) it should reference evidence spans where they exist — recorded as a WARNING-level note,
     NOT a hard failure (§6.8 says "where they exist"; absence of spans is allowed).
  The post-check is deterministic and PURE (no LLM) — fully unit-testable.
- **Regenerate-once-then-template (NO-FALLBACK, logged + visible):** on a HARD post-check failure (1
  or 2), call `llm` ONE more time with a stricter instruction; if it STILL fails, return
  `_template_narrative(request)` — a deterministic rendering of the findings (e.g. "Covered: …;
  Missing: …; Misconceptions flagged: …") with `used_fallback=True`. Log `_LOG.warning` at each
  failure (visible, never silent). If `llm` itself raises both times -> template fallback,
  `used_fallback=True`.
- The diagnostic NEVER re-grades, NEVER reads the rubric, NEVER sees reference structure beyond the
  finding keys.

**Immutability + purity of the checker:** the post-check + template are pure functions over the
request; only the `llm` call is impure (and injected).

Verify: `.venv/Scripts/python.exe -m pytest apollo/grading/tests/test_diagnostic.py -q`.

### 7. `apollo/handlers/done_grading.py` (EDIT — extend the shadow chain)

**Change 1 — extend `ShadowGradeResult` (frozen, ADDITIVE fields):** add three fields AFTER the
existing seven (additive keyword fields with no default would break WU-4C1 constructors in tests, so
the executor must update the WU-4C1 unit-test stubs OR give the new fields safe defaults — choose:
make them REQUIRED and update the two `ShadowGradeResult(...)` construction sites (`done_grading.py`
return + any test that builds one directly), because a half-populated calibration result is a silent
bug we do not want):
```python
@dataclass(frozen=True)
class ShadowGradeResult:
    run_id: int
    grade: GradeResult
    audited: AuditedGrade
    normalization_confidence: float
    reference_graph_hash: str
    opposes_map: Mapping[str, str]
    turn_order: Mapping[str, int]
    # WU-4C2 — graph-sim candidate grade + calibration + constrained diagnostic
    graph_sim_rubric: dict
    calibration: CalibrationMetrics
    diagnostic: ConstrainedDiagnostic
```

**Change 2 — `run_graph_simulation` signature gains the OLD rubric for calibration.** The OLD rubric
is computed in `done.py` BEFORE the shadow branch; thread it in as a new keyword-only arg
`old_rubric: dict`:
```python
async def run_graph_simulation(
    db, neo, *, attempt, sess, student_graph, problem_payload, old_rubric: dict,
) -> ShadowGradeResult | None: ...
```
Computation order — INSIDE the existing `try:` cross-store window, AFTER `db.commit()` of the run-txn
(current line 234) and AFTER `build_opposes_map`/`build_turn_order` (so the assembled result has
`opposes_map`/`turn_order`), BEFORE the `return ShadowGradeResult(...)`:
1. assemble the partial `ShadowGradeResult` data (run_id, grade, audited, norm_conf, ref_hash,
   opposes_map, turn_order) — already present.
2. `graph_sim_rubric = build_graph_sim_rubric(<the partial result data>, reference_graph)` — note
   `reference_graph` is the local `build_reference_canonical(...)` result already in scope (line 199).
   To avoid a chicken-and-egg with the frozen dataclass, the rubric-mapping fns take the RAW pieces
   (audited findings + opposes_map + turn_order + reference_graph), NOT a fully-built ShadowGradeResult
   — REVISE task 4 signatures to accept those pieces (see "Deviations"). Cleanest: pass
   `findings_to_rubric_input(audited=audited, reference_graph=reference_graph, opposes_map=opposes_map,
   turn_order=turn_order)`.
3. `calibration = compute_calibration_metrics(old_rubric=old_rubric, shadow_rubric=graph_sim_rubric)`.
4. `diagnostic = generate_constrained_diagnostic(audited, llm=main_chat_diagnostic_llm)` — the live
   default injected callable (a NEW `apollo/grading/diagnostic.py` module-level default, importable;
   mirrors how `main_chat_auditor`/`main_chat_adjudicator` are imported at the top of done_grading).
5. `_LOG.info("graph_sim_calibration", extra={...})` — log letter_agreement + overall_score_delta +
   divergent (the §6.7 tracked metrics). Add a module logger `_LOG = logging.getLogger(__name__)`.
6. `return ShadowGradeResult(... + graph_sim_rubric=..., calibration=..., diagnostic=...)`.

These steps run INSIDE the existing `try:` so any failure routes through the EXISTING NO-FALLBACK
forks (sets `learner_update_pending`, re-raises) — calibration/rubric/diagnostic are pure + the
diagnostic's llm is soft-failing (it never raises past the template fallback), so in practice they do
not trip pending; but a defensive unexpected error still follows the WU-4C1 contract. Do NOT add new
except branches.

**Boundary asserts preserved:** still does NOT call `convert_findings_to_events`, still writes NO
`apollo_mastery_events`/`apollo_learner_state`. The `test_no_mastery_events_written` guard stays green.

Verify: `.venv/Scripts/python.exe -m pytest apollo/handlers/tests/test_done_grading_unit.py -q`.

### 8. `apollo/handlers/done.py` (EDIT — live-promote flag + LIVE-off byte-identical guard)

**Change 1 — the dormant LIVE flag (mirror the shadow flag exactly):**
```python
# WU-4C2 — the PROMOTE-to-live flag (default OFF EVERYWHERE incl. test; flipped only
# after human calibration review, NEVER in this build). When OFF, the student-facing
# rubric/diagnostic are the OLD-path values (byte-identical to WU-4C1). When ON, the
# graph-sim rubric + constrained diagnostic from the shadow chain REPLACE them.
_GRAPH_SIM_LIVE_FLAG: str = "APOLLO_GRAPH_SIM_LIVE_ENABLED"

def _graph_sim_live_enabled() -> bool:
    return os.environ.get(_GRAPH_SIM_LIVE_FLAG, "").lower() in ("1", "true", "yes")
```

**Change 2 — thread `old_rubric` into the shadow call + the dormant LIVE promotion.** In the existing
`if _graph_sim_shadow_enabled():` block (lines 369-379), capture the shadow result and conditionally
promote:
```python
if _graph_sim_shadow_enabled():
    problem_payload = await _find_problem_payload(...)
    shadow = await run_graph_simulation(
        db, neo, attempt=attempt, sess=sess,
        student_graph=student_graph, problem_payload=problem_payload,
        old_rubric=rubric,                       # <-- the OLD student-facing rubric
    )
    # LIVE promotion (dormant — flag OFF in this build). Built + tested, never active.
    if shadow is not None and _graph_sim_live_enabled():
        student_response["rubric"] = shadow.graph_sim_rubric
        student_response["diagnostic_narrative"] = shadow.diagnostic.narrative
```
- **LIVE OFF (the only state in this build):** `student_response` is byte-identical to WU-4C1's
  OLD-path dict. The regression-guard test asserts this (same discipline as 4C1's shadow-off guard).
- **LIVE ON (dormant, tested):** the two keys are REPLACED by graph-sim values; nothing else changes
  (coverage/progress/XP stay OLD-path). The `coverage` key stays the OLD coverage (the graph-sim has
  no `coverage` dict in the student-facing shape; out of scope to replace it).
- Promotion happens AFTER the shadow chain returns successfully; if the shadow chain raised (e.g.
  pending), the LIVE branch is never reached and the OLD grade stands — exactly the §6.4 staged-txn
  guarantee.

Verify: `.venv/Scripts/python.exe -m pytest apollo/handlers/tests/test_done_shadow_flag.py
apollo/handlers/tests/test_done_live_flag.py -q`.

### 9. `apollo/grading/__init__.py` (EDIT — additive exports)

Add, ADDITIVELY (WU-4B1/4B2/4B3 names stay):
```python
from apollo.grading.calibration import (
    CALIBRATION_VERSION, AxisDelta, CalibrationMetrics, compute_calibration_metrics,
)
from apollo.grading.rubric_mapping import (
    RubricMappingInput, RubricRefNode, build_graph_sim_rubric, findings_to_rubric_input,
)
from apollo.grading.diagnostic import (
    ConstrainedDiagnostic, DiagnosticFinding, DiagnosticRequest,
    generate_constrained_diagnostic, main_chat_diagnostic_llm,
)
```
and append each name to `__all__`. A `test_package_seam_wu4c2.py` asserts every new name is importable
off `apollo.grading`.

Verify: `.venv/Scripts/python.exe -m pytest apollo/grading/tests/test_package_seam_wu4c2.py -q`.

### 10. Tests (full list) — see "Test list" section below for per-test assertions + mocking.

## Per-"endpoint" (call-seam) contracts

This unit adds NO HTTP endpoint. The affected HTTP surface is the EXISTING `POST
/apollo/sessions/{id}/done`, whose contract is UNCHANGED when both flags are at their build-state
defaults. The contract table records the build-state behavior + the dormant LIVE branch.

### Endpoint: POST /apollo/sessions/{id}/done (unchanged surface; new dormant branch)

| Field | Value |
|-------|-------|
| Method | POST |
| Path | `/apollo/sessions/{id}/done` |
| Auth | `require_session_owner` (unchanged) |
| Request | none (path param `id`; existing) |
| Response (SHADOW off OR on, LIVE off — the build state) | byte-identical to WU-4C1: `{rubric(OLD), diagnostic_narrative(OLD), coverage(OLD), progress{...}, ...flat XP}` |
| Response (LIVE on — dormant, future) | same shape, but `rubric` = graph-sim rubric and `diagnostic_narrative` = constrained-diagnostic narrative; `coverage`/`progress`/XP unchanged |
| Success status | 200 (unchanged) |
| Error statuses | unchanged from WU-4C1: 422 `review_required`/`student_graph_invalid`, 409 `reference_graph_invalid`, 503 `resolution_unavailable`/`transcript_audit_unavailable`, 500 `resolution_invalid_output` |
| Side effects | unchanged: OLD grade/XP/retention commit; SHADOW persists comparison run+findings; **WU-4C2 adds NO new DB write** — calibration is logged + in-memory only |
| Idempotent | unchanged (shadow re-run supersedes; calibration recompute is pure) |

### Internal call seams (the real WU-4C2 contracts)

| Seam | Signature | Pure? | LLM? |
|---|---|---|---|
| `findings_to_rubric_input` | `(*, audited, reference_graph, opposes_map, turn_order) -> RubricMappingInput` | yes | no |
| `build_graph_sim_rubric` | `(*, audited, reference_graph, opposes_map, turn_order) -> dict` (calls FROZEN `compute_rubric`) | yes | no |
| `compute_calibration_metrics` | `(*, old_rubric, shadow_rubric) -> CalibrationMetrics` | yes | no |
| `generate_constrained_diagnostic` | `(audited, *, llm=None) -> ConstrainedDiagnostic` | post-check pure; one injected llm call | yes (injected/mocked) |
| `_graph_sim_live_enabled` | `() -> bool` | yes | no |
| `run_graph_simulation` (extended) | `(db, neo, *, attempt, sess, student_graph, problem_payload, old_rubric) -> ShadowGradeResult \| None` | DB/Neo4j (existing) | injected (existing + new diag llm) |

## DI discovery findings

The grading package uses **module-level function injection**, NOT a DI container (no NestJS-equivalent
provider registry — this is FastAPI + plain modules). Confirmed across 3 siblings:

1. **Where is the live LLM callable registered?** As a module-level function imported at the call
   site, exactly like `main_chat_auditor` (`apollo/grading/transcript_audit.py`, imported into
   `done_grading.py:48`) and `main_chat_adjudicator` (`apollo/resolution/adjudication.py`, imported
   into `done_grading.py:68`). WU-4C2 follows this: `main_chat_diagnostic_llm` lives in
   `apollo/grading/diagnostic.py` and is imported at the top of `done_grading.py`, passed as
   `llm=main_chat_diagnostic_llm`.
2. **What's the injection token?** The plain callable object (no string/symbol/factory token). The
   default param value is `None` -> the function resolves the live default internally (mirrors
   `build_audited_grade(..., audit_fn=None)` -> `main_chat_auditor` default), OR the caller passes the
   live callable explicitly. Decision: the diagnostic's default is the explicit live callable passed
   from `done_grading.py` (so the test patches it at the `done_grading` import site, the established
   pattern in `test_done_grading_unit.py`).
3. **Singleton / request-scoped / transient?** Stateless module-level function — effectively
   singleton, no per-request state (matches `main_chat`/`cheap_chat` in `apollo/agent/_llm.py`). The
   new value objects are frozen/immutable, so no scope concern.

Sibling conventions AGREE — no conflict to flag. The one deliberate divergence: the §6.8 diagnostic
takes an INJECTED `llm` callable rather than calling `OpenAI()` directly like the OLD
`overseer/diagnostic.py` — RECON #3 mandates this (the OLD path's `generate_diagnostic` OpenAI usage
is FROZEN and untouched).

## Transaction scope decisions

**No new transaction.** WU-4C2 writes to ZERO tables. The only DB write in the shadow chain is the
WU-4C1 `persist_comparison_run` + its `db.commit()` (run-txn, owned by `run_graph_simulation`,
unchanged). Calibration/rubric/diagnostic run AFTER that commit, are pure (rubric/calibration) or
soft-failing-injected (diagnostic), and produce only in-memory + logged output. No external service
is called alongside a DB write that would need compensation — the diagnostic's injected llm runs
AFTER the run-txn commit and cannot corrupt persisted state (its failure falls back to a template
narrative; it never writes). The §6.4 staged-transaction story is therefore UNCHANGED: grade commits
first, shadow persists second, WU-4C2 computes third on already-durable data.

## Error contract decisions

- **No new exception class.** WU-4C2 introduces zero new error types. The shadow chain's existing
  named errors (`ResolutionUnavailableError` 503, `TranscriptAuditUnavailableError` 503,
  `ResolutionInvalidOutputError` 500, `StudentGraphInvalidError` 422, `ReferenceGraphInvalidError`
  409 — all registered in `apollo/api.py` `register_exception_handlers`, documented in
  `docs/architecture/apollo.md:31`) continue to catch any failure in the extended `try:` window.
- **The constrained diagnostic NEVER raises past its own boundary.** Per §6.8, an llm failure or a
  twice-failed post-check returns a TEMPLATE narrative with `used_fallback=True` (logged at WARNING,
  visible). This is the SAME NO-FALLBACK-but-degrade discipline as the OLD diagnostic's soft-fail and
  the transcript-audit boundary — it must never bubble an exception into the Done route and void the
  grade. This is asserted by a test (`test_diagnostic_llm_raises_falls_back_to_template`).
- **Calibration/rubric-mapping** are pure and total: empty findings, absent axes, and the
  structurally-empty `opposes_map` (RECON #7) all produce a valid `CalibrationMetrics` /
  `RubricMappingInput` (never a `KeyError`/`ZeroDivisionError`). Degenerate cases are defined, not
  discovered (mirrors the §6.1 harmonic-mean discipline): an empty graph-sim rubric overall == the
  rubric module's own `total_weight == 0 -> 0` path, so `compute_rubric` already yields a valid dict.
- **Final HTTP response shape to the client is UNCHANGED** when LIVE is off — frontend error handlers
  and the success envelope are untouched (the regression-guard test pins this byte-for-byte).

## Downstream consumers

- **WU-5A (belief update)** reads `ShadowGradeResult` — the ADDED fields (`graph_sim_rubric`,
  `calibration`, `diagnostic`) are additive and do not break its consumption of `grade`/`audited`/
  `opposes_map`/`turn_order`. WU-5A still owns `convert_findings_to_events` + the mastery writes;
  WU-4C2 must not pre-empt them (asserted: no events, no mastery rows).
- **Frontend (student UI + teacher UI):** the `POST /done` response shape is UNCHANGED in this build
  (LIVE off). Grep confirms the consumers of the Done payload read `rubric`/`diagnostic_narrative`/
  `progress` (e.g. `ai-ta-student-ui` Apollo report view). Because LIVE is off, no frontend change is
  needed or made here. When LIVE is flipped later (separate human-gated decision), the graph-sim
  rubric must match the OLD rubric SHAPE (same `{overall, procedure, justification, simplification,
  misconception_corrected}` keys) so the FE renders without change — `build_graph_sim_rubric` calls
  the SAME `compute_rubric`, guaranteeing shape parity. No frontend files are touched by this unit
  (scope is `ai-ta-backend/` only).
- **Calibration log consumer:** a human reviewer reads the `graph_sim_calibration` log line (and the
  persisted comparison runs/findings from WU-4B3) to decide the LIVE flip. No automated consumer.

## Owner-doc updates (`docs/architecture/apollo.md`, set `last_verified: 2026-06-18`)

The owner doc already declares `owns: apollo/grading/**` + `apollo/**` and `last_verified: 2026-06-18`.
Reconcile IN THE SAME WORK:

1. **Module map — `apollo/grading/` row (line 38):** append a WU-4C2 paragraph: the three new modules
   (`calibration.py`, `rubric_mapping.py`, `diagnostic.py`), their public callables, and the binding
   that `diagnostic.py` is the §6.8 CONSTRAINED findings-only diagnostic with an INJECTED llm +
   post-check + regenerate-once-then-template fallback — DISTINCT from the FROZEN
   `overseer/diagnostic.py` (which stays on the OLD path).
2. **Module map — `apollo/handlers/` row (line 32):** note that `done_grading.py`'s `ShadowGradeResult`
   gains `graph_sim_rubric`/`calibration`/`diagnostic`, `run_graph_simulation` gains the `old_rubric`
   kwarg, and `done.py` gains the dormant `APOLLO_GRAPH_SIM_LIVE_ENABLED` promote flag (OFF in this
   build; LIVE-off return is byte-identical to WU-4C1).
3. **Public interfaces — `POST /done` row (line 60):** append: when SHADOW is on, the chain ALSO
   computes the graph-sim rubric + §6.7 calibration metrics (letter agreement + per-axis deltas,
   LOGGED, no new column) + the §6.8 constrained diagnostic; the dormant
   `APOLLO_GRAPH_SIM_LIVE_ENABLED` (OFF) gates PROMOTION of the graph-sim rubric/diagnostic to
   student-facing — the build-state response is byte-identical.
4. **Core types section (after line 98):** add `CalibrationMetrics`/`AxisDelta`/`RubricMappingInput`/
   `RubricRefNode`/`ConstrainedDiagnostic`/`DiagnosticRequest` frozen value objects + the
   `compute_calibration_metrics`/`findings_to_rubric_input`/`build_graph_sim_rubric`/
   `generate_constrained_diagnostic` signatures, and the `CALIBRATION_VERSION = "calibration-v1"` /
   `DIVERGENCE_SCORE_THRESHOLD = 10` constants.
5. Confirm `last_verified: 2026-06-18` (already set — leave as-is; do not regress).

No other owner doc is touched (no DB/migration change -> `domain-data.md` untouched; no FE change).

## Test list (write FIRST, RED -> GREEN; every LLM injected/mocked, no live API, no container)

**`apollo/grading/tests/test_rubric_mapping.py`** (pure):
- `test_covered_finding_maps_per_step_covered` — a COVERED_NODE for key `k` -> `input.coverage["per_step"]["k"] == "covered"`.
- `test_missing_finding_maps_per_step_missing` — a MISSING_NODE for `k` -> `"missing"`.
- `test_reference_key_with_no_finding_defaults_missing` — a reference node with no finding -> `"missing"`.
- `test_procedure_score_covered_uses_confidence` — covered procedure_step with `confidence=0.92` -> `procedure_scores["k"] == 0.92`; covered with `confidence=None` -> `1.0`.
- `test_procedure_score_missing_is_zero` — missing procedure_step -> `0.0`.
- `test_reference_nodes_are_rubricrefnodes_keyed_on_canonical_key` — every `reference_graph.nodes` entry -> a `RubricRefNode(node_id=canonical_key, node_type=...)`; assert `compute_rubric` accepts them (duck-type: only `.node_id`/`.node_type` read).
- `test_misconception_detected_unresolved_scores_half` — a CONTRADICTION on `misc.x` with EMPTY opposes_map (the real today-state) -> `misconception_scores["misc.x"] == 0.5`.
- `test_misconception_resolved_scores_one_with_synthetic_opposes` — SYNTHETIC `opposes_map={"misc.x":"cond.y"}` + COVERED on `cond.y` LATER than the contradiction (turn_order) -> `misconception_scores["misc.x"] == 1.0`.
- `test_never_detected_misconception_absent` — no CONTRADICTION -> `misconception_scores == {}` (axis absent).
- `test_build_graph_sim_rubric_matches_compute_rubric` — `build_graph_sim_rubric(...)` deep-equals `compute_rubric(input.coverage, input.reference_nodes, misconception_scores=input.misconception_scores)` (proves we MIRROR the OLD call shape; no reimplementation).
- `test_audit_upgraded_covered_counts_as_covered` — a COVERED_NODE carrying `AUDIT_UPGRADE_MESSAGE` for a missing reference key -> `per_step == "covered"` (the upgrade is honored).
- `test_empty_findings_yield_valid_input` — empty `audited.findings` + non-empty reference -> all per_step `"missing"`, empty misconception_scores, valid `compute_rubric` result (no crash).
- `test_inputs_not_mutated` — assert `audited`/`reference_graph` are unchanged after the call (immutability).
Mocking: pure — use `_builders.py` helpers (`covered_finding`/`missing_finding`/`contradiction_finding`/`covered_finding_with_nodes`/`audited`/`turn_order_of`) + a tiny local `reference_graph` builder (a `ReferenceGraph` with `CanonicalNode`s — construct directly, frozen).

**`apollo/grading/tests/test_calibration.py`** (pure):
- `test_letter_agreement_true_when_letters_match` — old/shadow both overall `"B"` -> `letter_agreement is True`.
- `test_letter_agreement_false_when_letters_differ` — old `"A"` vs shadow `"C"` -> `False` AND `divergent is True`.
- `test_overall_score_delta_is_shadow_minus_old` — old 70 shadow 85 -> `overall_score_delta == 15`.
- `test_axis_delta_present_both_sides` — both have `procedure` present -> an `AxisDelta(axis="procedure", delta=shadow-old)`.
- `test_axis_delta_absent_one_side_is_none` — axis present old, absent shadow -> `delta is None`, `shadow_score is None` (no spurious 0-agreement).
- `test_divergent_trips_on_score_threshold` — letters match but `|delta| == 11 (> 10)` -> `divergent is True`.
- `test_not_divergent_when_close_and_letters_match` — letters match, `|delta| == 3` -> `divergent is False`.
- `test_calibration_version_constant` — `CALIBRATION_VERSION == "calibration-v1"` and is carried on the result.
- `test_inputs_not_mutated` — both rubric dicts unchanged after the call.
Mocking: pure — build the two rubric dicts as literals matching the rubric shape; no LLM/DB.

**`apollo/grading/tests/test_diagnostic.py`** (LLM injected, no live API):
- `test_request_built_from_findings_only` — inject a stub llm that CAPTURES its `DiagnosticRequest`; assert it carries covered/missing/misconception keys from findings and NO raw transcript field exists on the request type.
- `test_happy_path_returns_llm_narrative` — stub llm returns a clean narrative grounded in findings -> `narrative` == that string, `used_fallback is False`, `regenerated is False`, `post_check_failures == ()`.
- `test_post_check_flags_claimed_covered_for_missing` — findings mark `k` missing; stub llm asserts "you covered k" -> first call fails post-check -> regenerated once.
- `test_regenerate_once_then_template_on_repeated_failure` — stub llm returns a bad narrative BOTH times -> `used_fallback is True`, `regenerated is True`, `narrative == _template_narrative(...)`, exactly TWO llm calls (assert call count via a counting stub).
- `test_template_lists_covered_missing_misconceptions` — the fallback template names covered/missing/misconception keys (deterministic, pure).
- `test_diagnostic_llm_raises_falls_back_to_template` — stub llm RAISES -> `used_fallback is True`, no exception escapes, narrative is the template (NO-FALLBACK boundary).
- `test_introduced_misconception_not_in_findings_fails_post_check` — narrative names a misconception not in `misconception_keys` -> post-check failure recorded.
- `test_no_live_openai_import` — assert the module does NOT import `OpenAI` at top level (`"OpenAI" not in module top-level names`) so importing the module is CI-safe; the live default lazily imports it inside `main_chat_diagnostic_llm`.
- `test_evidence_span_absence_is_soft_not_hard` — a narrative with no span reference but otherwise grounded -> NO hard failure, `used_fallback is False`.
Mocking: inject deterministic `llm` stub callables (counting / raising / bad-then-bad / clean) modeled on `_builders.py:found_audit_fn` etc. Patch nothing live.

**`apollo/handlers/tests/test_done_grading_unit.py`** (EDIT — extend existing; all callees mocked):
- `test_shadow_result_carries_calibration_fields` — after a happy-path `run_graph_simulation`, the returned `ShadowGradeResult` has `graph_sim_rubric`/`calibration`/`diagnostic` set (mock `build_graph_sim_rubric`/`compute_calibration_metrics`/`generate_constrained_diagnostic` at the `done_grading` import site, assert each called once with the right kwargs: `old_rubric` forwarded, `audited`/`reference_graph`/`opposes_map`/`turn_order` forwarded).
- `test_old_rubric_threaded_to_calibration` — assert `compute_calibration_metrics` got `old_rubric=<the dict passed into run_graph_simulation>`.
- `test_diagnostic_llm_injected` — assert `generate_constrained_diagnostic` got `llm=dg.main_chat_diagnostic_llm`.
- `test_calibration_logged` — patch the module `_LOG`; assert an info log fired with letter_agreement/overall_score_delta/divergent (or assert via caplog).
- `test_no_mastery_events_written` (KEEP) — still asserts `convert_findings_to_events` never called.
- UPDATE `_all_callee_patches` to add the three new callees; UPDATE the happy-path assertions; UPDATE any direct `ShadowGradeResult(...)` construction in tests to supply the three new required fields (or use a `_shadow_result()` builder).
Mocking: extend `_all_callee_patches` with `build_graph_sim_rubric`/`compute_calibration_metrics`/`generate_constrained_diagnostic` (+ `main_chat_diagnostic_llm` referenced as `dg.main_chat_diagnostic_llm`).

**`apollo/handlers/tests/test_done_live_flag.py`** (NEW; OLD-path collaborators mocked, no live LLM):
- `test_live_flag_parsing` — copy `test_shadow_flag_parsing`: truthy `("1","true","TRUE","Yes","yes")` -> True; falsy/`unset` -> False.
- `test_live_flag_constant_name` — `done_mod._GRAPH_SIM_LIVE_FLAG == "APOLLO_GRAPH_SIM_LIVE_ENABLED"`.
- `test_live_off_return_byte_identical` — SHADOW on, LIVE off (or unset): `run_graph_simulation` returns a sentinel `ShadowGradeResult` with a distinct `graph_sim_rubric`/`diagnostic`, but `out["rubric"]`/`out["diagnostic_narrative"]` are the OLD-path values (byte-identical guard). Mirrors `test_shadow_flag_off_return_byte_identical`.
- `test_live_off_default_when_unset` — neither flag manipulated beyond shadow-on -> LIVE defaults OFF -> OLD-path values.
- `test_live_on_promotes_graph_sim_rubric_and_diagnostic` — SHADOW on + LIVE on: `out["rubric"] is shadow.graph_sim_rubric` and `out["diagnostic_narrative"] == shadow.diagnostic.narrative`; `out["coverage"]`/`out["progress"]` STAY OLD-path.
- `test_live_on_but_shadow_returns_none_keeps_old` — LIVE on but `run_graph_simulation` returns `None` -> OLD-path values stand (no AttributeError).
- `test_old_rubric_forwarded_to_shadow` — assert `run_graph_simulation` got `old_rubric=<the OLD rubric dict>` kwarg.
Mocking: reuse `_old_path_patches()` from `test_done_shadow_flag.py` (patch `run_graph_simulation` with an `AsyncMock` returning a fabricated `ShadowGradeResult`); `monkeypatch` both flags.

**`apollo/grading/tests/test_package_seam_wu4c2.py`** (NEW): import every new public name off `apollo.grading`; assert presence in `__all__`.

Coverage target: every NEW line in the three modules + the `done_grading`/`done` edits is exercised
(the `1.0`/`0.5` misconception branches, the regenerate + template + raise branches, LIVE on/off both
sides, axis-absent deltas). Run:
`.venv/Scripts/python.exe -m pytest apollo -q --tb=short` then
`.venv/Scripts/python.exe -m pytest --cov=apollo --cov-report=xml -q` then
`diff-cover coverage.xml --compare-branch=feat/apollo-kg-wu4c1-done-shadow-orchestration --fail-under=95`.
(The `apollo/tests/test_api_auth.py` faulthandler "access violation" dumps are PRE-EXISTING non-fatal
aiosqlite/TestClient noise — the process survives to 100% and writes coverage.xml; NOT a blocker.)

## Risks

| # | Risk | Confidence it bites | Mitigation |
|---|---|---|---|
| 1 | `compute_rubric` reads more than `.node_id`/`.node_type` off reference nodes (would break the `RubricRefNode` duck-type). | LOW — verified rubric.py:112-133 reads ONLY those two attrs. | Pin with `test_reference_nodes_are_rubricrefnodes_keyed_on_canonical_key` (passes RubricRefNodes through the REAL `compute_rubric`); if it ever reads more, switch `RubricRefNode` to construct real `apollo.ontology.build_node(...)` minimal Nodes. |
| 2 | `opposes_map` empty today (RECON #7) means the `1.0` resolved-misconception branch is never hit by real data -> dead-code coverage gap. | MEDIUM | Test it with a SYNTHETIC `opposes_map` fixture (`test_misconception_resolved_scores_one_with_synthetic_opposes`) so the branch is covered + future-proof; document that real data lands at `0.5`. |
| 3 | Extending the FROZEN `ShadowGradeResult` with REQUIRED fields breaks any direct constructor in WU-4C1 tests. | MEDIUM | Audit all `ShadowGradeResult(` construction sites (the `done_grading.py` return + any test); add a `_shadow_result()` test builder; update them in the same change. (Grep confirms the only production site is `done_grading.py:243`.) |
| 4 | The constrained diagnostic post-check is a heuristic keyword screen — a false "claimed covered" could force needless regeneration/template. | LOW-MEDIUM | Keep the screen CONSERVATIVE (only flags an explicit covered-verb adjacent to a missing key token); soft-fail to template is harmless (the OLD diagnostic stays student-facing while LIVE is off); tests pin both the catch and the no-false-positive case. |
| 5 | Threading `old_rubric` changes `run_graph_simulation`'s signature -> the WU-4C1 shadow-flag test (`test_shadow_flag_on_invokes_chain`) asserts the call kwargs. | MEDIUM | Update that test's kwarg assertion (it already inspects `shadow.await_args.kwargs`); add `old_rubric` to the expected set. Listed under task 8 verify. |
| 6 | Lazy `OpenAI` import inside `main_chat_diagnostic_llm` could still pull a heavy import chain at CALL time in prod. | LOW | The live default is only invoked when SHADOW is on (test config); `_default` mirrors the OLD diagnostic which already imports `OpenAI` lazily-enough. The injected-stub tests never trigger it. |
| 7 | diff-cover counts the dormant LIVE-on branch + template fallback as changed lines; if untested they fail the 95% gate. | MEDIUM | The test list explicitly covers LIVE-on promotion (`test_live_on_promotes_...`), the template fallback (`test_regenerate_once_then_template_on_repeated_failure`), and the raise path — every new branch has a test. |

## Out-of-scope boundaries (HARD — do not cross)

- **NO learner-model writes.** Do NOT call `convert_findings_to_events`, do NOT write
  `apollo_mastery_events` or `apollo_learner_state`. That is the WU-5A boundary (asserted empty).
- **NO flipping LIVE.** `APOLLO_GRAPH_SIM_LIVE_ENABLED` STAYS OFF in every config (prod, test,
  conftest). The LIVE-on branch is BUILT + unit-tested via `monkeypatch`, never enabled by default.
- **NO touching the OLD path.** `apollo/overseer/diagnostic.py`/`rubric.py`/`coverage.py` are FROZEN —
  only `compute_rubric`/`score_to_letter` are CALLED, never edited. The OLD `generate_diagnostic`
  OpenAI path is untouched.
- **NO Neo4j / DB / migration / container.** Migration 028 stays unused. No file under
  `tests/database/`, `database/migrations/`, `apollo/knowledge_graph/`, or `neo4j_client.py` changes.
- **NO new dependency install.** Use stdlib + existing imports only.
- **NO calibration table / new column.** Metrics are logged + in-memory (WU-4C key_decision #6).
- **NO frontend change.** Scope is `ai-ta-backend/` only; LIVE-off keeps the Done payload shape.
- **NO branch/PR/push.** Work on the checked-out `feat/apollo-kg-wu4c2-calibration-diagnostics` only.
- **NO calibration/rubric dependence on conflict/corrected events existing** (RECON #7 caveat): the
  rubric mapping must produce valid output with the structurally-empty `opposes_map`.

## Deviations I'd allow the executor

- **Rubric-mapping argument shape:** task 4 first sketched `findings_to_rubric_input(shadow_result,
  reference_graph)`, but task 7 shows the cleaner call site passes the RAW pieces
  (`audited`/`reference_graph`/`opposes_map`/`turn_order`) to avoid a chicken-and-egg with the frozen
  `ShadowGradeResult`. The executor SHOULD use the raw-pieces keyword signature
  `findings_to_rubric_input(*, audited, reference_graph, opposes_map, turn_order)` and the matching
  `build_graph_sim_rubric(*, audited, reference_graph, opposes_map, turn_order)`. Either consistent
  choice is acceptable as long as it is keyword-only and pure.
- **Where the new frozen dataclasses live:** co-locating `RubricRefNode`/`RubricMappingInput` in
  `rubric_mapping.py`, `CalibrationMetrics`/`AxisDelta` in `calibration.py`, and the diagnostic value
  objects in `diagnostic.py` is preferred (mirrors `event_model.py` co-location), but a shared tiny
  `grading/calibration_types.py` is acceptable if a circular import forces it (it should not).
- **`misconception_scores` resolution heuristic:** the `1.0`-vs-`0.5` rule may be simplified to "any
  CONTRADICTION present for the key -> `0.5` unless a later opposed COVERED exists -> `1.0`"; the
  executor may use the WU-4B2 `_turn_position` primitive's SHAPE (min-over-node-ids) but must NOT
  import `apollo.grading.events` internals — copy the 3-line helper to keep the rubric axis decoupled
  from the event decision table.
- **`DIVERGENCE_SCORE_THRESHOLD` value:** `10` is a hand-set v1 knob; the executor may pick a
  documented value in `[8, 15]` if a test fixture reads more naturally, as long as it is a named
  module constant and the tests use the constant (not a magic literal).
- **Logging field names:** the `_LOG.info("graph_sim_calibration", extra={...})` field set is
  illustrative; the executor may shape `extra` to match the repo's existing structured-log convention
  as long as `letter_agreement`, `overall_score_delta`, and `divergent` appear.
- **Template narrative wording:** the deterministic fallback text is not load-bearing; any wording
  that names covered/missing/misconception keys deterministically is fine.
