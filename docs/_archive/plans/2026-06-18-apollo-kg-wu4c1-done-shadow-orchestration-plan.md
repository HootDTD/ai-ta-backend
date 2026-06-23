# WU-4C1 — Done re-orchestration in SHADOW: TDD implementation plan

**Date:** 2026-06-18
**Unit:** WU-4C1 (the first of the two WU-4C sub-units; WU-4C2 = calibration + diagnostics)
**Branch (already checked out):** `feat/apollo-kg-wu4c1-done-shadow-orchestration`
**Base / diff-cover compare branch:** `feat/apollo-kg-wu4b3-runs-findings-persistence`
**Spec:** `docs/superpowers/specs/2026-06-10-apollo-kg-learner-model-architecture-decision.md`
§6.4 (Done pipeline), §6.6 (abstention gates), §6.7 (shadow gate), §6.1/§5 (named
errors → HTTP), §8A (runtime curriculum payload).
**Split proposal (ground truth):** `docs/superpowers/plans/2026-06-18-apollo-kg-wu4c-split-proposal.md`
§0–§4 (full `done.py` audit + the 8 decisions). Read it first.
**Status:** PLAN ONLY — no code/tests written. This document is the deliverable.

---

## 0. One-paragraph statement of the unit

WU-4C1 wires the already-built graph-simulation grading chain
(`resolve → RESOLVES_TO → canonicalize → grade → audit → persist run+findings`)
into the LIVE `apollo/handlers/done.py`, **behind a named shadow flag
`APOLLO_GRAPH_SIM_SHADOW_ENABLED` (default OFF in prod, ON in test)**. It writes
NO new grading algorithm — every callee is DONE and frozen (only CALL them). When
the flag is OFF, `handle_done` is byte-identical to today. When ON, the new chain
runs AFTER the existing OLD-path grade/XP commit, persists a comparison run +
findings ALONGSIDE the unchanged OLD `compute_coverage`/`compute_rubric`
student-facing grade, sources `turn_order` (proving it queryable for WU-5A), and
registers the five named errors as HTTP handlers. Any step-5-onward failure sets
`attempt.learner_update_pending=true`, surfaces a named error, and **never voids
the already-committed student grade** (NO-FALLBACK, mirrors `RetentionError`).

---

## 1. Ground-truth facts verified against the real code (file:line)

These shape every decision below; all confirmed by reading the source on
2026-06-18.

1. **Live `handle_done`** (`apollo/handlers/done.py:188`) takes
   `*, db, neo, session_id`. Today's flow: load `sess` (196) +
   `_find_problem(db, sess.concept_id, sess.current_problem_id) -> Problem`
   (197); load latest `attempt` (199); read pre-freeze graph
   `store.read_graph(attempt_id=attempt.id)` (215); P3.6 Done-gate (216,
   flag-gated); `store.freeze(session_id)` (221); reference graph
   `problem.to_kg_graph(attempt_id=...)` (224); commit SOLVING phase (227);
   **OLD grade path**: `compute_coverage` (229) → `_attempt_misconception_scores`
   (236) → `compute_rubric` (239) → `generate_diagnostic` (245) → XP (262) →
   commit `attempt.result="graded"` + `diagnostic_report` + REPORT phase (268-276)
   → `apply_xp` (278) → `store.stamp_graded_at` (296, post-commit retention) →
   student-facing return `{rubric, diagnostic_narrative, coverage, progress, xp_*}`
   (298-322). **THIS RETURN IS THE LIVE CONTRACT WU-4C1 MUST NOT BREAK.**

2. **The existing flag pattern** is `_DONE_GATE_FLAG = "APOLLO_DONE_GATE_ENABLED"`
   + `_done_gate_enabled()` reading `os.environ.get(...).lower() in ("1","true","yes")`
   (`done.py:53,56-57`). WU-4C1 mirrors this exactly for the shadow flag.

3. **Chain callees (all DONE; signatures verified):**
   - `build_problem_candidates(problem: dict, misconceptions: dict, *, canon_key_by_canonical_key: dict[str,int]) -> ProblemInputs`
     (`graph_compare/problem_inputs.py:40`). Reads `problem["symbolic_mappings"]`
     (defaults `{}`). `ProblemInputs(candidates: tuple[Candidate,...], symbolic_mappings: dict)`.
   - `candidates_from_misconceptions(misc: dict, *, canon_key_by_canonical_key)`
     reads `misc["misconceptions"]` as a list of `{key, trigger_phrases, opposes,
     display_name}` dicts (`resolution/candidates.py:110`). **So done.py must hand
     `build_problem_candidates` a `misconceptions` dict shaped
     `{"misconceptions": [ {key, trigger_phrases, opposes, display_name}, ... ]}`,
     built from `misconception_bank.load_for_concept(db, concept_id=...)`.**
   - `resolve_attempt(student_graph, candidates, *, llm_adjudicator=None,
     fuzzy_threshold=0.9, symbolic_mappings=None) -> ResolutionResult`
     (`resolution/resolver.py:132`). Live path passes
     `llm_adjudicator=apollo.resolution.adjudication.main_chat_adjudicator`
     (`adjudication.py:74`). Raises `ResolutionInvalidOutputError` on hallucinated
     key, `ResolutionUnavailableError` on the LLM call infra failure.
   - `write_resolution(neo, attempt_id, result, *, resolved_at) -> ResolutionWriteResult`
     (`knowledge_graph/resolution_store.py:202`). Idempotent MERGE; raises
     `ResolutionUnavailableError(stage="write_resolves_to"|"persist_fields")`.
   - `validate_student_graph(student_graph) -> None`
     (`graph_compare/validator.py:50`); raises `StudentGraphInvalidError`.
   - `build_student_canonical(student_graph, resolution) -> CanonicalGraph`
     (`graph_compare/canonical.py:167`).
   - `build_reference_canonical(problem: dict) -> ReferenceGraph`
     (`graph_compare/canonical.py:100`); calls `validate_reference` → raises
     `ReferenceGraphInvalidError`. **Reads `problem["declared_paths"]` and per-step
     `step["entity_key"]` (canonical.py:114,154).**
   - `grade_attempt(student_canonical, reference_graph) -> GradeResult`
     (`graph_compare/core.py:78`).
   - `build_audited_grade(grade, *, transcript, resolution, student_nodes,
     candidates=(), reference_invalid=False, audit_fn=None) -> AuditedGrade`
     (`grading/audited_grade.py:165`). **CATCHES `TranscriptAuditUnavailableError`
     internally** (audited_grade.py:184) → suppress-all-missing abstention; the
     error never reaches the handler on the normal path. Live default `audit_fn`
     = `main_chat_auditor`; tests inject a stub.
   - `compute_normalization_confidence(audited_grade, resolution) -> float`
     (`grading/normalization_confidence.py:38`).
   - `reference_graph_hash(reference_graph) -> str` (`grading/reference_hash.py:38`).
   - `persist_comparison_run(db, *, attempt_id, user_id, search_space_id, grade,
     audited, normalization_confidence, reference_graph_hash) -> int`
     (`grading/persistence.py:197`). **Does NOT commit — `flush()` only; the caller
     owns the txn boundary** (persistence.py:217,240).
   - `build_opposes_map(candidates) -> Mapping[str,str]` (`grading/opposes.py:16`).

4. **CRITICAL — raw payload, not the parsed `Problem`.** `build_reference_canonical`
   / `build_problem_candidates` need `declared_paths` / `symbolic_mappings` /
   per-step `entity_key`. The runtime `Problem` pydantic schema
   (`schemas/problem.py`) declares ONLY `id/concept_id/difficulty/problem_text/
   given_values/target_unknown/reference_solution`, and `ReferenceStep` declares
   only `step/entry_type/id/content/depends_on` — **no `entity_key`, no
   `declared_paths`, no `symbolic_mappings`**. `Problem.model_validate(payload)`
   silently DROPS them. So WU-4C1 must source the **raw `ConceptProblem.payload`
   dict** (a parallel `select(ConceptProblem.payload)` filtered to the chosen
   `problem_code` under `sess.concept_id`) for the new chain, while keeping the
   parsed `Problem` for the OLD path (`to_kg_graph`, `reference_solution`,
   `problem_text`). `ConceptProblem` ORM: `concept_id`, `problem_code`, `payload`
   (`persistence/models.py:158-169`).

5. **`canon_key_by_canonical_key` source.** Build it from
   `canon_projection.load_entity_specs(db, *, search_space_id=, concept_id=) -> list[CanonNodeSpec]`
   (`knowledge_graph/canon_projection.py:117`, course-scoped — refuses an
   unscoped read, line 135). Map `spec.canonical_key -> spec.key` (the
   `apollo_kg_entities.id` surrogate). An absent entity link → key not in the map
   → `Candidate.canon_key=-1` → no Layer-1 surrogate (tolerated;
   `persistence.py` stores `entity_id=None`). `load_entity_specs` wraps DB errors
   as `CanonProjectionError` (already HTTP-registered? NO — see §5 note; it
   surfaces but is not in WU-4C1's five — see Risk #6).

6. **`turn_order` sourcing — the §4.3 gap, RESOLVED here.**
   `convert_findings_to_events` (WU-5A's consumer) takes
   `turn_order: Mapping[node_id, int]` (`grading/events.py:68`). But:
   - The in-memory `Node` (`ontology/nodes.py`) carries **no** turn/`created_at`
     field; node ids are random `stu_{uuid4().hex[:12]}` (`parser/parser_llm.py:337`)
     — they do NOT encode the turn.
   - `KGStore.read_graph` **strips** the Neo4j `created_at` prop
     (`store.py:147`), so the turn signal is invisible through `read_graph`.
   - The only per-node temporal signal that physically exists is the Neo4j node
     property `created_at` (an ISO-8601 string), stamped on EVERY node by
     `write_nodes` (`store.py:320`), one timestamp per turn's write batch.
   - `apollo_messages.turn_index` (`models.py:255`, NOT NULL) is the Postgres
     turn ordinal, but there is **no stored node→message FK** (the negotiation
     table bridges by `entry_id`/`node_id`, but only for negotiated nodes).
   **Decision (spec §6.5 sanctions "turn order from node `created_at`"):**
   `build_turn_order` reads each `:_KGNode`'s `created_at` property directly from
   Neo4j (a tiny NEW read, NOT a schema change, NOT via `read_graph`), groups
   node ids by distinct `created_at` value (one value per write batch = one turn),
   sorts the distinct values ascending, and assigns each group a monotonic
   position `0,1,2,…`. A node with no `created_at` (pre-WU-3C1 legacy, or absent)
   → **absent key** in the returned map (events.py tolerates via `_SENTINEL_TURN`).
   To bind the position to the actual `turn_index` rather than a bare monotonic
   counter, the read ALSO joins each distinct `created_at` to the nearest
   `apollo_messages.turn_index` for the attempt (the student turn whose message
   row's `created_at` is ≤ the node `created_at`); when that join is empty (no
   message rows), fall back to the bare ascending-index position. Either way the
   RESULT is a `Mapping[node_id, int]` that is monotone in extraction order —
   which is all `events.py` needs (it compares positions, never absolute values).
   **WU-4C1 SOURCES + proves it queryable; WU-5A CONSUMES it.**

7. **`read_graph` and `created_at`.** Because `_record_to_node` strips
   `created_at` (store.py:147), `build_turn_order` CANNOT reuse `read_graph`. Two
   options: (a) a tiny new Cypher read inside `done_turn_order.py` opening its own
   `neo.session()` (`MATCH (n:_KGNode {attempt_id:$aid}) RETURN n.node_id, n.created_at`),
   or (b) add a `KGStore.read_node_created_at(*, attempt_id) -> dict[str,str]`
   method to `store.py`. **Decision: option (b)** — a small focused read method
   on `KGStore` (the owner of all Neo4j subgraph reads) keeps `done_turn_order.py`
   DB-free over a typed input and matches the scope-file note ("EDIT store.py ONLY
   IF read_graph must surface the per-node turn"). It is a READ addition, never a
   schema change. `build_turn_order` then takes the
   `node_created_at: Mapping[str,str]` + `turn_index_by_created_at` it assembles.
   (If review prefers minimizing the store.py surface, option (a) is the documented
   fallback — both are pure reads.)

8. **Staged transaction (§6.4).** The OLD grade/XP commits FIRST (today's
   `done.py` already does this at lines 276/278). The new chain runs AFTER. RESOLVES_TO
   is idempotent MERGE. `persist_comparison_run` flushes; **WU-4C1 owns the commit**
   for the run txn (a `db.commit()` after the persist call). Any step-5+ failure →
   set `attempt.learner_update_pending=true` + commit that flag + re-raise the named
   error. The retry re-runs FROM resolution idempotently (RESOLVES_TO MERGE +
   persist supersede).

9. **No migration.** 026 shipped runs/findings + `learner_update_pending`; 027
   dropped `concept_cluster_id`. Latest migration on disk = `027_*`. WU-4C1 needs
   NO DDL.

10. **The five named errors exist** in `apollo/errors.py`
    (`ResolutionUnavailableError:169`, `TranscriptAuditUnavailableError:189`,
    `ResolutionInvalidOutputError:211`) and `apollo/graph_compare/validator.py`
    (`StudentGraphInvalidError:30`, `ReferenceGraphInvalidError:40`). NONE are
    HTTP-registered. `apollo/api.py::register_exception_handlers` (line 402)
    registers handlers via `app.add_exception_handler`; the pattern is a per-error
    async handler returning `JSONResponse(status_code=…, content=_err_payload(code,
    msg, **extra))` (api.py:258-262, 264-417). `coverage_grading_handler` (api.py:373,
    503) is the prior art for an infra/no-fallback 503.

---

## 2. Public API this unit exposes (frozen / immutable)

```python
# apollo/handlers/done_grading.py  (NEW)
from dataclasses import dataclass
from collections.abc import Mapping
from apollo.graph_compare.core import GradeResult
from apollo.grading.audited_grade import AuditedGrade

@dataclass(frozen=True)
class ShadowGradeResult:
    """The frozen handoff WU-4C2 (calibration metrics) and WU-5A (belief update)
    both read. Carries everything the live chain computed for the shadow run."""
    run_id: int
    grade: GradeResult
    audited: AuditedGrade
    normalization_confidence: float
    reference_graph_hash: str
    opposes_map: Mapping[str, str]
    turn_order: Mapping[str, int]

async def run_graph_simulation(
    db: AsyncSession,
    neo: Neo4jClient,
    *,
    attempt: ProblemAttempt,
    sess: ApolloSession,
    student_graph: KGGraph,
    problem_payload: dict,
) -> ShadowGradeResult | None:
    """Run the full §6.4 chain (steps 1–9, 13, 15-call) in SHADOW and persist the
    comparison run + findings (caller owns the surrounding txn; this calls
    persist_comparison_run which flushes, then db.commit()). Returns the
    ShadowGradeResult on success. On ANY step-5+ infra failure (named error) it
    sets attempt.learner_update_pending=True, commits that flag, and RE-RAISES the
    named error — it NEVER returns a partial result and NEVER voids the grade.
    Returns None ONLY when the chain is a no-op for non-error reasons (see §3.4)."""

# apollo/handlers/done_turn_order.py  (NEW)
async def build_turn_order(
    db: AsyncSession,
    neo: Neo4jClient,
    *,
    attempt_id: int,
    student_graph: KGGraph,
) -> dict[str, int]:
    """Map each KG node_id → a monotone turn position sourced from the node's
    Neo4j created_at (joined to apollo_messages.turn_index where possible). A node
    with no created_at → absent key (events.py tolerates via _SENTINEL_TURN)."""

# apollo/knowledge_graph/store.py  (EDIT — tiny READ addition, NOT a schema change)
class KGStore:
    async def read_node_created_at(self, *, attempt_id: int) -> dict[str, str]:
        """node_id → created_at ISO string for every :_KGNode in the subgraph.
        A read-only helper for build_turn_order (read_graph strips created_at)."""
```

`apollo/handlers/done.py` keeps its existing public `handle_done(*, db, neo,
session_id)` signature UNCHANGED — the new chain is an internal,
flag-gated call inserted after the OLD grade/XP commit.

---

## 3. Files to create / edit

### 3.1 `apollo/handlers/done_grading.py` (NEW)

Holds `ShadowGradeResult` + `run_graph_simulation` + the staged-txn / NO-FALLBACK
discipline, so `done.py` stays well under 800 lines and the chain is
unit-testable in isolation.

Internal flow of `run_graph_simulation` (each numbered to §6.4):
1. Build `misconceptions` dict: `misconception_bank.load_for_concept(db,
   concept_id=sess.concept_id)` → `{"misconceptions": [{"key": e.code,
   "trigger_phrases": list(e.trigger_phrases), "opposes": <opposes>, "display_name":
   <name>}, ...]}`. (Map the `MisconceptionEntry` fields onto the dict shape
   `candidates_from_misconceptions` reads — see §1.3. The `opposes` source: the
   bank row does not carry an `opposes` column today, so v1 reads it from the
   Layer-1 entity payload via `load_entity_specs` if present, else `None` — a
   missing opposes-link just means no conflict-pair detection, tolerated.)
2. Build `canon_key_by_canonical_key`: `load_entity_specs(db,
   search_space_id=sess.search_space_id, concept_id=sess.concept_id)` →
   `{spec.canonical_key: spec.key}`.
3. `inputs = build_problem_candidates(problem_payload, misconceptions,
   canon_key_by_canonical_key=...)`.
4. `validate_student_graph(student_graph)` (step 4 gate — raises
   `StudentGraphInvalidError`). NOTE: validation runs on the RAW student graph
   BEFORE resolution; a failure here is a 422 and must NOT have written anything
   to Neo4j yet (so `learner_update_pending` is NOT set for a pure validation
   422 — see §3.4 decision).
5. `resolution = resolve_attempt(student_graph, inputs.candidates,
   llm_adjudicator=main_chat_adjudicator, fuzzy_threshold=0.9,
   symbolic_mappings=inputs.symbolic_mappings)`.
6. `write_resolution(neo, attempt.id, resolution, resolved_at=<iso now>)`
   (idempotent MERGE; raises `ResolutionUnavailableError`).
7. `student_canonical = build_student_canonical(student_graph, resolution)`;
   `reference_graph = build_reference_canonical(problem_payload)` (raises
   `ReferenceGraphInvalidError`).
8. `grade = grade_attempt(student_canonical, reference_graph)`.
9. Read transcript + student nodes: transcript =
   `select(Message.content).where(attempt_id==).order_by(turn_index)` joined into
   one string; `student_nodes = tuple(student_graph.nodes)`. Then
   `audited = build_audited_grade(grade, transcript=transcript,
   resolution=resolution, student_nodes=student_nodes, candidates=inputs.candidates,
   reference_invalid=False, audit_fn=main_chat_auditor)`. (audit_fn is the live
   default; tests inject a stub. The `TranscriptAuditUnavailableError` is caught
   INSIDE build_audited_grade — it does not surface here.)
10. `normalization_confidence = compute_normalization_confidence(audited, resolution)`;
    `ref_hash = reference_graph_hash(reference_graph)`.
11. `run_id = await persist_comparison_run(db, attempt_id=attempt.id,
    user_id=sess.user_id, search_space_id=sess.search_space_id, grade=grade,
    audited=audited, normalization_confidence=normalization_confidence,
    reference_graph_hash=ref_hash)`; then `await db.commit()` (WU-4C1 owns the
    run-txn commit boundary).
12. `opposes_map = build_opposes_map(inputs.candidates)`;
    `turn_order = await build_turn_order(db, neo, attempt_id=attempt.id,
    student_graph=student_graph)`.
13. Return `ShadowGradeResult(run_id, grade, audited, normalization_confidence,
    ref_hash, opposes_map, turn_order)`.

**NO step 16/17:** WU-4C1 does NOT call `convert_findings_to_events` and does NOT
write `apollo_mastery_events`/`apollo_learner_state` (those are WU-5A). It carries
`opposes_map` + `turn_order` in the frozen result so WU-5A can consume them.

**Staged-txn / NO-FALLBACK wrapper:** `run_graph_simulation` wraps steps 5–13 in a
`try/except` over the named infra errors that must not void the grade
(`ResolutionUnavailableError`, `ResolutionInvalidOutputError`,
`ReferenceGraphInvalidError`, plus any unexpected Exception in the cross-store
window). On catch: `attempt.learner_update_pending = True`; `await db.commit()`;
re-raise the original named error (so `done.py`/the API layer surfaces the right
HTTP status). The grade/XP are already committed by the OLD path before
`run_graph_simulation` is even called, so re-raising never voids them.

### 3.2 `apollo/handlers/done_turn_order.py` (NEW)

`build_turn_order` per §1.6 decision:
- `node_created_at = await store.read_node_created_at(attempt_id=...)` (or
  `KGStore(db, neo).read_node_created_at(...)`).
- Read `select(Message.turn_index, Message.created_at).where(attempt_id==,
  role=="student").order_by(turn_index)` to build the `created_at → turn_index`
  reference points.
- Group node ids by distinct `created_at`; sort the distinct values; assign each
  group the `turn_index` of the latest student message at-or-before that
  `created_at`, else the group's ascending ordinal.
- Nodes with no `created_at` are omitted from the map (absent key).
- Pure over the two reads; returns `dict[str,int]`. No Neo4j write.

### 3.3 `apollo/handlers/done.py` (EDIT)

- Add module constant `_GRAPH_SIM_SHADOW_FLAG = "APOLLO_GRAPH_SIM_SHADOW_ENABLED"`
  + `_graph_sim_shadow_enabled()` mirroring `_done_gate_enabled()` (done.py:53-57).
  Do NOT add the promote-to-live flag (WU-4C2).
- Add a parallel raw-payload read helper
  `_find_problem_payload(db, *, concept_id, problem_code) -> dict` doing
  `select(ConceptProblem.payload).join(Concept).where(Concept.id==concept_id,
  ConceptProblem.problem_code==problem_code)` (the raw `payload`, NOT the parsed
  `Problem`). Keep `_find_problem` (the parsed `Problem`) for the OLD path.
- Insert the shadow call AFTER the existing post-commit retention write (after
  `store.stamp_graded_at`, done.py:296) — the grade/XP are fully durable by then.
  Guard: `if _graph_sim_shadow_enabled(): ... run_graph_simulation(...)`. The
  return dict (298-322) is built BEFORE the shadow call and is NOT modified by it
  — the student-facing payload is byte-identical to today whether the flag is on
  or off and whether the chain succeeds. (Placing the shadow call AFTER the return
  is built but BEFORE `return` means: on a shadow failure the named error is
  raised and surfaces as the right HTTP status; on success the same dict returns.
  An ALTERNATIVE — capture the dict, run shadow, return — is equivalent; the
  binding rule is the dict is constructed from OLD-path values ONLY.)
- The shadow call passes `problem_payload=_find_problem_payload(...)`,
  `student_graph=student_graph` (the already-read `pre_freeze_graph`), `attempt`,
  `sess`.
- **Keep `done.py` < 800 lines.** The chain body lives in `done_grading.py`;
  done.py only adds the flag, the raw-payload read, and the ~6-line guarded call.

### 3.4 `done.py` / `done_grading.py` error-routing decision (binding)

- `StudentGraphInvalidError` (422) and `ReferenceGraphInvalidError` (409) are
  raised BEFORE any Neo4j write in the chain (validate_student_graph is step 4;
  build_reference_canonical validates before building). They block the SHADOW
  grade. **They do NOT set `learner_update_pending`** (nothing cross-store was
  written; there is nothing to retry until the bad graph/reference is fixed). They
  DO surface as the right HTTP status. The OLD student grade is already committed,
  so it is not voided. — *Note:* `ReferenceGraphInvalidError` "blocks grading"
  per §6.6; in SHADOW v1 it blocks only the shadow run (the OLD path already
  graded the student), and surfaces as a visible 409 so the bad reference is
  noticed.
- `ResolutionUnavailableError` / `ResolutionInvalidOutputError` /
  `TranscriptAuditUnavailableError(defensive re-raise)` are infra failures in the
  cross-store window (step 5+). They **DO** set `learner_update_pending=true`,
  commit that flag, and re-raise (503/500). `TranscriptAuditUnavailableError`
  normally never reaches here (caught inside `build_audited_grade`); the handler
  exists for a defensive re-raise.

### 3.5 `apollo/api.py` (EDIT)

Add five async handlers + five registrations, following `_err_payload`
(api.py:258-262) and the `coverage_grading_handler` (503) prior art:

| Error | Status | `error_code` | extra fields |
|---|---|---|---|
| `ResolutionUnavailableError` | 503 | `resolution_unavailable` | `stage`, `last_error` |
| `TranscriptAuditUnavailableError` | 503 | `transcript_audit_unavailable` | `stage`, `last_error` |
| `ResolutionInvalidOutputError` | 500 | `resolution_invalid_output` | `returned_key`, `allowed_key_count` (NOT the full key list — keep payload bounded) |
| `StudentGraphInvalidError` | 422 | `student_graph_invalid` | `reasons` |
| `ReferenceGraphInvalidError` | 409 | `reference_graph_invalid` | `reasons` |

Import `StudentGraphInvalidError`/`ReferenceGraphInvalidError` from
`apollo.graph_compare.validator`; the other three from `apollo.errors`. Register
all five in `register_exception_handlers` (api.py:402) via
`app.add_exception_handler`. Two 503s mirror the no-fallback contract; the user
message is "grading is computing in the background; your grade is saved" — i.e.
the SHADOW failure must read as non-fatal to the student.

### 3.6 `apollo/knowledge_graph/store.py` (EDIT — tiny READ addition)

Add `KGStore.read_node_created_at(*, attempt_id) -> dict[str,str]`:
`MATCH (n:_KGNode {attempt_id:$aid}) RETURN n.node_id AS node_id, n.created_at AS created_at`,
returning `{node_id: created_at}` skipping null `created_at`. Read-only, opens one
`neo.session()`. NOT a schema change. Reconcile the owner doc's store.py paragraph.

### 3.7 `docs/architecture/apollo.md` (EDIT — owner-doc reconcile)

- `apollo/handlers/` row: add `done_grading.py`, `done_turn_order.py`; update the
  `done.py` description to "freeze → OLD grade/XP/retention (student-facing,
  unchanged) → **shadow graph-simulation chain behind
  `APOLLO_GRAPH_SIM_SHADOW_ENABLED`** (resolve → RESOLVES_TO → canonicalize →
  grade → audit → persist run+findings; sets `learner_update_pending` + named
  error on any step-5+ infra failure, NEVER voids the grade; sources `turn_order`;
  does NOT write `apollo_mastery_events`/`apollo_learner_state` — that is WU-5A,
  and does NOT promote the shadow grade to student-facing — that flag is WU-4C2)".
- `apollo/` (root) row: append the five new error handlers + statuses to the
  exception-handler inventory.
- `apollo/knowledge_graph/` row: note `KGStore.read_node_created_at` (the
  turn_order read).
- The `POST /apollo/sessions/{id}/done` route row: note the shadow split + the
  five new error responses.
- Bump `last_verified` (already `2026-06-18`; keep/confirm 2026-06-18).

---

## 4. TDD test list (write tests FIRST, RED → GREEN)

No skip/xfail/tautology. LLM mocked deterministically everywhere
(`llm_adjudicator` stub into `resolve_attempt`, `audit_fn` stub into
`build_audited_grade`, `main_chat_adjudicator`/`main_chat_auditor` patched at the
`done_grading` import site). The real-PG (`db_session`) and real-Neo4j
(`tc_neo4j`) gates MUST run green-not-skipped (Docker up).

### 4.1 `apollo/handlers/tests/test_done_shadow_flag.py` (unit, the LOAD-BEARING guard)

- `test_shadow_flag_off_return_byte_identical` — **the single most important
  test.** With `APOLLO_GRAPH_SIM_SHADOW_ENABLED` unset/false, drive `handle_done`
  with the OLD-path collaborators mocked (compute_coverage/compute_rubric/
  generate_diagnostic/compute_xp_earned/apply_xp/stamp_graded_at) and assert the
  returned dict equals a frozen golden dict captured from the SAME mocked inputs
  WITHOUT the new code path ever importing/calling `run_graph_simulation`. Patch
  `apollo.handlers.done.run_graph_simulation` with a `MagicMock` and assert
  `not called`. (Proves: flag off → student grade provably untouched.)
- `test_shadow_flag_on_invokes_chain` — flag on; `run_graph_simulation` patched to
  return a sentinel `ShadowGradeResult`; assert it WAS called once with
  `student_graph`/`attempt`/`sess`/`problem_payload`, and the returned dict is
  STILL the OLD-path dict (chain result is not merged into the student response).
- `test_shadow_flag_parsing` — `_graph_sim_shadow_enabled()` true for
  `"1"/"true"/"yes"` (case-insensitive), false otherwise (mirrors the done-gate
  flag test).

### 4.2 `apollo/handlers/tests/test_done_grading_unit.py` (unit, chain orchestration with all callees mocked)

- `test_run_graph_simulation_happy_path_calls_chain_in_order` — patch every callee
  (`build_problem_candidates`, `resolve_attempt`, `write_resolution`,
  `validate_student_graph`, `build_student_canonical`, `build_reference_canonical`,
  `grade_attempt`, `build_audited_grade`, `compute_normalization_confidence`,
  `reference_graph_hash`, `persist_comparison_run`, `build_opposes_map`,
  `build_turn_order`); assert call order + that `resolve_attempt` got
  `llm_adjudicator=main_chat_adjudicator` and `symbolic_mappings=inputs.symbolic_mappings`,
  and `persist_comparison_run` got `user_id=sess.user_id`,
  `search_space_id=sess.search_space_id`. Assert `db.commit` called after persist.
  Assert the returned `ShadowGradeResult` carries the mocked `run_id`,
  `opposes_map`, `turn_order`.
- `test_raw_payload_passed_not_parsed_problem` (the §1.4 regression guard) — feed a
  `problem_payload` dict carrying `declared_paths`, `symbolic_mappings`, and per-step
  `entity_key`; assert the EXACT dict object reaching `build_reference_canonical`
  and `build_problem_candidates` still has those three keys (i.e. it was NOT round-
  tripped through `Problem.model_validate`/`model_dump`). Spy on the args.
- `test_resolution_unavailable_sets_pending_and_reraises` — `write_resolution`
  raises `ResolutionUnavailableError`; assert `attempt.learner_update_pending` set
  True, `db.commit` called, and the error re-raised (NO-FALLBACK).
- `test_resolution_invalid_output_sets_pending_and_reraises` — `resolve_attempt`
  raises `ResolutionInvalidOutputError`; same assertion.
- `test_student_graph_invalid_does_not_set_pending` — `validate_student_graph`
  raises `StudentGraphInvalidError`; assert `learner_update_pending` NOT set,
  error re-raised (nothing cross-store written).
- `test_reference_graph_invalid_does_not_set_pending` — `build_reference_canonical`
  raises `ReferenceGraphInvalidError`; assert `learner_update_pending` NOT set,
  error re-raised.
- `test_audit_fn_injected` — assert `build_audited_grade` received a non-None
  `audit_fn` and `candidates=inputs.candidates`, `reference_invalid=False`.
- `test_no_mastery_events_written` (boundary, unit-level) — assert
  `convert_findings_to_events` is NOT imported/called by `run_graph_simulation`
  (patch it on the module and assert not called).

### 4.3 `apollo/handlers/tests/test_done_turn_order_unit.py` (unit, with the two reads mocked)

- `test_maps_node_ids_to_turn_index` — given `read_node_created_at` →
  `{n1:t0, n2:t0, n3:t1}` (two turns) and student-message rows at those
  timestamps, assert `{n1:0, n2:0, n3:1}` (or the joined `turn_index`),
  monotone in extraction order.
- `test_node_with_no_created_at_absent` — a node id missing from
  `read_node_created_at` → absent key in the result (events.py tolerates).
- `test_empty_graph_returns_empty_map` — no nodes → `{}`.
- `test_no_messages_falls_back_to_ordinal` — node `created_at`s present but no
  message rows → bare ascending positions.

### 4.4 `apollo/api/tests/test_apollo_error_handlers.py` (unit, ASGI app, no DB)

Construct a minimal FastAPI app, `register_exception_handlers(app)`, mount a route
per error that raises it, drive via `httpx.ASGITransport`/`TestClient`. For each
of the five: assert the HTTP status (503/503/500/422/409) and the `_err_payload`
shape (`error_code` + `message` + the listed extras). One test per error =
5 tests. Plus `test_all_five_registered` asserting each error type is in
`app.exception_handlers`.

### 4.5 `tests/database/test_done_shadow_route_postgres.py` (real-PG gate — `db_session`)

Reuses the `seed_course` / `load_bernoulli_problem_payloads` /
`_curriculum_fixtures` harness (as `test_done_concept_db.py` does) so the raw
`ConceptProblem.payload` (carrying `declared_paths`/`symbolic_mappings`/`entity_key`)
is real. OLD-path collaborators and the Neo4j calls (`write_resolution`,
`read_node_created_at`, `read_graph`) are mocked/stubbed at the boundary; the
graph-compare/grading callees run REAL (pure); `audit_fn` + `llm_adjudicator`
are injected stubs. Tests:
- `test_shadow_on_persists_run_and_findings_old_grade_unchanged` (LOAD-BEARING #2)
  — flag on, happy path: after `handle_done` a row exists in
  `apollo_graph_comparison_runs` for the attempt (+ ≥1 `_findings` row); the
  returned dict's `rubric`/`coverage` are the OLD-path values; **assert
  `apollo_mastery_events` is EMPTY for the user** (the 4C/5A boundary) and
  `apollo_learner_state` is EMPTY.
- `test_reference_invalid_returns_409_old_grade_committed` (LOAD-BEARING #3) — feed
  a payload whose reference fails validation (drop `declared_paths`); assert
  `ReferenceGraphInvalidError` surfaces (409 via the handler in an integration
  client OR the raised error in a direct call), and the attempt row already has
  `result="graded"` (OLD grade committed, NOT voided), and `learner_update_pending`
  is False, and NO comparison run row was written.
- `test_resolution_unavailable_503_pending_grade_committed` (LOAD-BEARING #4) —
  stub `write_resolution` to raise `ResolutionUnavailableError`; assert the error
  surfaces (503), `attempt.learner_update_pending` is True in the DB, the OLD grade
  is committed (`result="graded"`), and the grade dict was the OLD one.
- `test_retry_supersedes_run_idempotent` (LOAD-BEARING #5, PG half) — run the
  shadow chain twice for the same attempt; assert exactly ONE
  `apollo_graph_comparison_runs` row at `(attempt_id, comparison_version)` (persist
  supersede; no UNIQUE crash).
- `test_turn_order_query_runs_on_real_pg` — seed real `apollo_messages` rows +
  stub `read_node_created_at`; assert `build_turn_order` returns the expected map
  using the real `Message.turn_index` join.

### 4.6 `apollo/knowledge_graph/tests/test_done_shadow_neo4j.py` (real-Neo4j gate — `tc_neo4j`)

- `test_resolves_to_idempotent_on_retry` (LOAD-BEARING #5, Neo4j half) — write
  nodes + a `:Canon` node; call `write_resolution` twice via the chain path; assert
  exactly one `RESOLVES_TO` edge (MERGE idempotent), mirroring
  `test_resolution_store_neo4j.py::test_re_resolution_is_idempotent_merge`.
- `test_read_node_created_at_round_trip` — `write_nodes` (which stamps
  `created_at`); assert `KGStore.read_node_created_at` returns
  `{node_id: <iso created_at>}` for every node, and that two nodes written in one
  batch share the same `created_at` (proving the per-turn grouping signal exists).
- `test_read_node_created_at_empty_subgraph` — no nodes → `{}`.

### 4.7 Coverage / negative-control

- Every new branch (each `except`, the flag on/off fork, the
  `learner_update_pending` set/not-set forks, the `created_at`-absent fork) is hit
  by a named test above. Target: ≥95% patch coverage measured by
  `diff-cover coverage.xml --compare-branch=feat/apollo-kg-wu4b3-runs-findings-persistence
  --fail-under=95`. No `# pragma: no cover` except a genuinely-unreachable
  defensive branch (documented inline).

---

## 5. Verification commands (run before claiming done)

```bash
# Unit + real-PG + real-Neo4j (Docker up; both gates green-not-skipped)
pytest apollo/handlers/tests/test_done_shadow_flag.py \
       apollo/handlers/tests/test_done_grading_unit.py \
       apollo/handlers/tests/test_done_turn_order_unit.py \
       apollo/api/tests/test_apollo_error_handlers.py \
       tests/database/test_done_shadow_route_postgres.py \
       apollo/knowledge_graph/tests/test_done_shadow_neo4j.py -v
# Whole suite + coverage
pytest --cov=apollo --cov-report=xml -q
diff-cover coverage.xml \
  --compare-branch=feat/apollo-kg-wu4b3-runs-findings-persistence --fail-under=95
```

Confirm the real-PG and Neo4j tests RAN (not skipped) — a skip is a FAIL of the
dual-infra gate.

---

## 6. Risks & uncertainties

1. **HIGHEST — live-Done non-breakage.** Mitigated by the shadow flag (default
   OFF), the `test_shadow_flag_off_return_byte_identical` guard, building the
   return dict from OLD-path values only, and the staged-txn NO-FALLBACK (the
   grade commits before the chain). The chain body lives in `done_grading.py` so a
   crash in WU-4C1 code can only fire when the flag is on.
2. **HIGH — `misconceptions` dict shape mismatch.** `candidates_from_misconceptions`
   reads `{key, trigger_phrases, opposes, display_name}`, but
   `misconception_bank.MisconceptionEntry` has `code/description/trigger_phrases/
   probe_question/…` and no `opposes`/`display_name`. The mapping in §3.1 step 1
   must translate field names (`code → key`, derive `display_name`, source
   `opposes` from the Layer-1 entity payload or `None`). A missing `opposes` just
   disables conflict-pair detection for that misconception (tolerated; no
   `apollo_mastery_events` written in 4C1 anyway). **Verify the exact opposes
   source during implementation** (entity payload `opposes_entity_key` per the
   §2 schema, or the bank — pick the one actually populated by the bernoulli
   seed). Add a unit test pinning the mapping.
3. **MEDIUM — `turn_order` semantics.** `events.py` only COMPARES positions, so a
   monotone-in-extraction-order map suffices; binding to the literal `turn_index`
   is a nicety, not a correctness requirement, and is proven queryable here, not
   consumed. The `created_at`-grouping is the spec-sanctioned source (§6.5). The
   only real failure mode is two turns sharing an identical `created_at` string
   (sub-second ties) collapsing into one position — acceptable for v1 (it only
   makes a conflict "ambiguous", the safe default).
4. **MEDIUM — `read_graph` strips `created_at`.** Resolved by the new
   `read_node_created_at` read (NOT a schema change). If review prefers zero
   store.py edits, the §1.7 fallback (a self-contained Cypher read in
   `done_turn_order.py`) is equivalent.
5. **MEDIUM — `CanonProjectionError`** can be raised by `load_entity_specs` (step
   2). It is NOT one of the five WU-4C1 errors and is NOT HTTP-registered today. In
   the shadow chain it is an infra failure: catch it in the NO-FALLBACK wrapper,
   set `learner_update_pending`, re-raise. It currently has no handler, so it would
   surface as a generic 500 — acceptable for v1 SHADOW (a registered handler is a
   tiny follow-up, out of WU-4C1's named-five scope). Document the gap.
6. **LOW — two extra Done-time LLM calls** (resolver adjudication + transcript
   audit) on the live path when the flag is on. Per-attempt, not per-turn
   (~$0.004/turn measured in the RQ3 spike). Every test injects stubs; no live
   OpenAI.
7. **LOW — `_find_problem_payload` join.** `ConceptProblem.problem_code` is the
   join key; `sess.current_problem_id` is the code today (it matches `Problem.id`
   in `_find_problem`). Pin with a real-PG test that the payload read returns the
   SAME problem as the parsed `_find_problem`.

---

## 7. Out-of-scope boundaries (WU-4C2 / WU-5A — DO NOT cross)

- **WU-4C2:** §6.7 calibration METRICS computation; §6.4 rubric MAPPING
  (findings/sub-scores → `compute_rubric`); §6.8 constrained diagnostic; the
  `APOLLO_GRAPH_SIM_LIVE_ENABLED` promote-to-live flag (stays unbuilt here).
- **WU-5A:** calling `convert_findings_to_events` on the live path; writing
  `apollo_mastery_events` / `apollo_learner_state`; the 3-state Bayesian belief
  update; the all-or-nothing event txn. WU-4C1 PROVES `apollo_mastery_events` is
  EMPTY after a Done (the boundary test).
- **No new migration** (026/027 already shipped everything WU-4C1 touches; next
  free is 028, unused).
- **DO NOT modify** `apollo/graph_compare/**`, `apollo/grading/**`,
  `apollo/resolution/**` internals (frozen — only CALL them). The one permitted
  edit outside `apollo/handlers/` + `apollo/api.py` is the tiny READ method on
  `apollo/knowledge_graph/store.py` and the owner doc.

---

## 8. Build-order note

WU-4C1 is one TDD-executor pass: 2 new modules (`done_grading.py`,
`done_turn_order.py`) + a focused `done.py`/`api.py`/`store.py` edit + 6 test
files (incl. real-PG + real-Neo4j). It trips BOTH the `tests/database` real-PG
gate AND the Neo4j gate — the only WU-4C sub-unit that does. WU-4C2 branches off
WU-4C1's tip; WU-5A off WU-4C2.
