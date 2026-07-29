---
plan: apollo-kg-wu3c2-resolver
work_unit: WU-3C2
branch: feat/apollo-kg-wu3c2-resolver
base_for_coverage: feat/apollo-kg-wu3c1-canon-projection
spec: docs/superpowers/specs/2026-06-10-apollo-kg-learner-model-architecture-decision.md  (§5, §6.3, §2, §3, §6.9)
owner_doc: docs/architecture/apollo.md
status: ready
created: 2026-06-16
provides:
  - apollo/resolution/ package — the §5 shared reference-anchored resolver (standalone, importable by WU-4A retired graph comparator)
  - resolve_attempt(...) -> ResolutionResult, with per-node ResolvedNode {resolution, resolved_key, method, confidence}
  - RESOLVES_TO edge writer + resolution node-field persistence in apollo/knowledge_graph/
  - ResolutionUnavailableError + ResolutionInvalidOutputError (apollo/errors.py)
consumes:
  - apollo/solver/sympy_exec.py parse_zero_form (symbolic tier — REUSE, no reimplementation)
  - apollo/agent/_llm.py main_chat (single LLM adjudication call)
  - apollo/ontology (KGGraph / Node / NodeType — student evidence graph)
  - WU-3B reference solutions (entity_key + content per step) + misconception entities (trigger_phrases + opposes)
  - WU-3C1 :Canon Neo4j nodes (RESOLVES_TO targets, keyed on entity.id)
  - rapidfuzz (already pinned: rapidfuzz>=3.12,<4) — fuzzy tier only
depends_on:
  - WU-3C1 (:Canon projection; store.py Layer-2 scoping/metadata; conftest tc_neo4j)
  - WU-3B (reference-solution entity_key links, declared_paths, misconception opposes/trigger aliases)
---

# Plan: WU-3C2 — §5 shared resolver + RESOLVES_TO edges + resolution node-fields

**Goal:** Build the one reference-anchored resolver that maps a student's per-attempt evidence nodes onto this problem's own reference nodes (+ course misconception entities), persist `RESOLVES_TO` edges to `:Canon` and resolution node-fields, with content-first tiers, structural corroboration, misconception competition, bounded global assignment, and one LLM adjudication.

**Architecture (data flow one-liner):** frozen student `KGGraph` + this problem's reference nodes + course `misc.*` entities → content tiers (exact → SymPy-symbolic → alias → fuzzy≥0.9) → structural propagation + neighborhood veto + type-compat hard constraint → misconception competition → bounded greedy global assignment → one LLM adjudication for the remainder → `ResolutionResult` → `RESOLVES_TO {method,confidence,resolved_at}` edges + resolution node-fields.

---

## 0. Mandatory context (read order, all grounded)

Read before writing any code — every decision below is grounded in these:

1. **Spec** `docs/superpowers/specs/2026-06-10-apollo-kg-learner-model-architecture-decision.md`:
   - **§5** (THE RESOLVER) — closed candidate set, content-first tiers, structural propagation + neighborhood veto + type-compat hard constraint, misconception competition, bounded greedy global assignment, one LLM adjudication "return empty when unsure", anti-over-normalization guardrails (polarity screen, no variant collapse, below-threshold→unresolved), mechanics (Done-time batched, one LLM call/attempt max, NO-FALLBACK, confidence caps by method).
   - **§6.3** — component rows: `normalizer.py` / `symbolic.py` / `resolver.py` are the §5 resolver; `EDGE_ALLOWED_PAIRS` includes `RESOLVES_TO: evidence→Canon`.
   - **§2** — `(:_KGNode)-[:RESOLVES_TO {method,confidence,resolved_at}]->(:Canon)`; Layer-2 resolution node-fields `resolution: resolved|unresolved|ambiguous`, `resolved_key`, `resolution_method`, `resolution_confidence`.
   - **§3** — confidence caps by method feed the damper: exact 1.00 · symbolic 0.98 · alias 0.92 · fuzzy 0.80 · LLM 0.75 · unresolved 0.00.
   - **§6.9** — the Bernoulli worked example = the canonical pure-unit fixture.
2. **Target source** (read end-to-end, listed §9): `apollo/solver/sympy_exec.py`, `apollo/agent/_llm.py`, `apollo/errors.py`, `apollo/knowledge_graph/store.py`, `apollo/knowledge_graph/canon_projection.py`, `apollo/ontology/{nodes,edges,graph}.py`, `apollo/persistence/learner_model_seed.py`, `apollo/persistence/models.py` (KGEntity/Concept/Subject/Misconception), `apollo/subjects/.../problems/problem_01.json`, `.../misconceptions.json`, `.../normalization_map.json`, `.../canonical_symbols.json`.
3. **RQ3 spike** is referenced by §4 only (parser recall); not needed for the resolver itself — no read required.

**Grounded candidate-set shape (verified in repo):**
- A problem's reference nodes come from `reference_solution[]` (WU-3B-annotated). Each step has `id`, `entry_type` (equation/condition/simplification/procedure_step/definition), `content` (`symbolic` for equations, `applies_when` for conditions, `transformation` for simplifications, `action` for procedure_steps, `label`), and an `entity_key` (e.g. `eq.bernoulli`, `cond.incompressibility`, `simp.horizontal_simplification`, `proc.plan_apply_continuity`). `entry_type→NodeType`: equation→equation, condition→condition, simplification→simplification, procedure_step→procedure_step, definition→definition.
- Course misconception entities come from `misconceptions.json` (`misc.*`), each carrying `trigger_phrases` (the fuzzy/alias aliases that make competition algorithmic) and `opposes` (the entity it contradicts). The §6.9 example also needs the authored `def.pressure_velocity_tradeoff` (from `learner_model_seed._AUTHORED_DEFINITIONS`), which a `misc.*` opposes.
- `normalization_map.json` (23 mappings) is already converted by WU-3B into `entity.aliases`; the **alias tier matches against these aliases**, NOT by re-reading the JSON.
- `:Canon` nodes (WU-3C1) are keyed on `entity.id` (surrogate). RESOLVES_TO targets a `:Canon` node by that key — so the resolver's output `resolved_key` must be resolvable to a `:Canon.key`. v1: the resolver works in `canonical_key` space (the reference node's `entity_key` / the misconception entity's `canonical_key`), and the RESOLVES_TO writer maps `canonical_key → :Canon.key` via a candidate→key map the caller supplies (the same scope already used by WU-3C1; no global Postgres lookup inside the writer).

## 1. Overview & where this fits

WU-3C2 is the **resolver third** of phase-3 (§12.3). WU-3C1 built the `:Canon` projection (write-only) and the Layer-2 scoping/metadata props and explicitly deferred "the §5 resolver, `RESOLVES_TO` edges, and `:Canon` reads" to **this** unit. WU-3B produced the reference-solution `entity_key` links, `declared_paths`, the misconception `opposes`/`trigger_phrases` aliases, and the normalization-map → `entity.aliases` conversion. WU-3A is the schema/ORM.

This unit delivers exactly three things and nothing more:
1. **The resolver** (`apollo/resolution/`): student evidence nodes → canonical targets, with per-node `{resolution, resolved_key, resolution_method, resolution_confidence}` and confidence caps by method.
2. **The persistence half** (`apollo/knowledge_graph/`): write `RESOLVES_TO {method,confidence,resolved_at}` edges from `:_KGNode` to `:Canon`, and persist the four resolution node-fields on Layer-2 nodes (idempotent MERGE).
3. **Two named errors** (`apollo/errors.py`): `ResolutionUnavailableError` (infra failure — must NOT void the grade), `ResolutionInvalidOutputError` (hallucinated LLM key not in the candidate set).

**Standalone-by-design (binding):** §6.3 names the resolver `apollo/retired graph comparator/resolver.py`, but `retired graph comparator` is Phase-4 (WU-4A) and does not exist on this branch. Per the binding constraint, build the resolver in a **standalone `apollo/resolution/` package** so WU-4A's `retired graph comparator` imports it (`from apollo.resolution import resolve_attempt, ...`) rather than owning it. WU-4A's pipeline step 5 ("Resolve student nodes (§5)") and step 7 ("Write RESOLVES_TO edges") call this unit's public API.

The resolver does NOT grade, simulate, audit transcripts, emit learner events, or orchestrate the Done pipeline. It is matching + persistence + named errors only.

## 2. Prior art in repo

- **WU-3C1 `canon_projection.py`** (`apollo/knowledge_graph/canon_projection.py`) — the pattern to copy for the persistence half: frozen-dataclass pre-Neo4j spec (`CanonNodeSpec`), a pure DB-free mapping seam (`entity_to_canon_spec`/`canon_spec_to_row`), an idempotent `MERGE` Cypher, a `merge_specs` async seam extracted for direct container testing, and NO-FALLBACK wrap-and-raise (`CanonProjectionError(stage=...)`). RESOLVES_TO writing mirrors this exactly (frozen `ResolvesToEdgeSpec` → `_RESOLVES_TO_MERGE_CYPHER` → `write_resolves_to` seam).
- **WU-3C1 `store.py` `stamp_graded_at` / `_record_to_node`** — the idempotent in-place Neo4j `SET … RETURN count` pattern and the metadata-prop-stripping convention. Resolution node-fields follow the same shape: SET on `:_KGNode {attempt_id,node_id}`, and `_record_to_node` strips the four resolution props before content reconstruction so `read_graph` still round-trips byte-identically (the WU-3C1 deferred nit, folded in here — §11).
- **WU-3C1 test harness** — `apollo/knowledge_graph/tests/conftest.py` `tc_neo4j` (local Testcontainers `neo4j:5.25`, never Aura) for real-Neo4j tests; `test_canon_projection_mapping.py` for the pure-unit / fake-session pattern; `test_canon_projection_seeder_neo4j.py` for the real-Neo4j write/round-trip pattern. Copy both.
- **`sympy_exec.parse_zero_form`** — REUSE for the symbolic tier: parses `LHS = RHS` to a `LHS - RHS` zero-form under the canonical symbol context (`_CANONICAL_SYMBOLS`, `_local_dict`). Two zero-forms are structurally equivalent iff `simplify(a - b) == 0` (sign-exact). The declared mapping `d = 2r` is applied by `.subs(Symbol('d'), 2*Symbol('r'))` before comparison. `MalformedEquationError` already exists for unparseable input.
- **`agent/_llm.main_chat`** — REUSE for the single adjudication call; `purpose="resolution_adjudication"`, `response_format` = a strict JSON object, `temperature=0.0`. It already emits the `llm_call` audit log line. Mocked in tests via patching `apollo.resolution.<module>.main_chat`.
- **WU-3B `learner_model_seed.py`** — the source-of-truth for how reference nodes/misconceptions are shaped (`entity_key`, `opposes_entity_key`, alias placement); the resolver's candidate-set builder consumes these same shapes.
- **`store.write_edges` / `WriteEdgesResult`** — the structured-result + per-rejection-reason convention; `ResolutionResult` follows it (counts + per-node reasons, not exceptions, for the non-match data case).

## 3. Structural prep (neighborhood scan)

Scanned the change path (one ring out): `apollo/knowledge_graph/store.py`, `apollo/errors.py`, the new `apollo/resolution/` package, `apollo/agent/_llm.py`, `apollo/solver/sympy_exec.py`.

| Artifact | Metric | Reading | Action |
|---|---|---|---|
| `store.py` | 749 lines | **At the 800-line soft ceiling** (CLAUDE.md "small focused files <800"). Adding the RESOLVES_TO writer + resolution-field persistence (~80–120 lines) would push it over. | **Prep: do NOT add the writer to `store.py`.** Put RESOLVES_TO writing + resolution-field persistence in a **new sibling module** `apollo/knowledge_graph/resolution_store.py` (frozen-spec + MERGE + async seam, the canon_projection shape). `store._record_to_node` gains only the 4-prop strip (≤6 lines) so `read_graph` round-trips — that is the single necessary edit to `store.py`. This keeps both files focused and under ceiling. |
| `errors.py` | 166 lines, CBO low | Clean; two additive classes (~25 lines). | None — append two named errors. |
| `_llm.py` | 99 lines, 1 import (openai) | Clean; no edit (consume `main_chat` as-is). | None. |
| `sympy_exec.py` | 137 lines | Clean; REUSE `parse_zero_form`; **no edit** (binding: do not reimplement; do not modify the solver). The symbolic tier imports it. | None. |
| new `apollo/resolution/` | n/a | Greenfield. Keep each file <300 lines, one tier/concern per file (resolver orchestration, content tiers, structural pass, assignment, LLM adjudication, candidate model). | Split as in §9 so no file is a god-module. |

**Budget check:** structural prep = 1 routing decision (writer → new module instead of store.py) + the 4-prop strip. Well under 30% of plan steps. No split needed; proceed combined.

## 4. Public interfaces (signatures, backward-compat)

All new symbols are additive; no existing signature changes except the one
`store._record_to_node` strip (internal, behavior-preserving).

### 4.1 Candidate model — `apollo/resolution/candidates.py`

```python
RESOLUTION_METHODS = ("exact", "symbolic", "alias", "fuzzy", "llm", "unresolved")
# Confidence caps by method (§3 — the damper input). Frozen mapping.
METHOD_CONFIDENCE_CAP: dict[str, float] = {
    "exact": 1.00, "symbolic": 0.98, "alias": 0.92,
    "fuzzy": 0.80, "llm": 0.75, "unresolved": 0.00,
}

@dataclass(frozen=True)
class Candidate:
    """One resolution target in the closed candidate set for an attempt."""
    canonical_key: str            # 'eq.bernoulli', 'cond.incompressibility', 'misc.*'
    canon_key: int                # the :Canon node key (== apollo_kg_entities.id)
    node_type: NodeType           # for the type-compat hard constraint
    is_misconception: bool        # competes in every resolution (§5 guardrail)
    symbolic: str | None          # equations only — the symbolic tier input
    aliases: tuple[str, ...]      # alias-tier surface forms (incl. trigger_phrases)
    display_name: str
    opposes_key: str | None       # misconceptions: the entity they contradict

def build_candidate_set(
    *, reference_nodes: list[Candidate], misconception_entities: list[Candidate],
) -> tuple[Candidate, ...]:
    """Closed candidate set = this problem's reference nodes + course misc.* .
    Returns an immutable tuple; misconceptions are always appended so they
    compete in every resolution (§5)."""
```

The caller (WU-4A; here the tests) builds `Candidate`s from the WU-3B reference
solution + misconception entities + the WU-3C1 `:Canon` key map. A tiny adapter
`candidates_from_reference_solution(problem, *, canon_key_by_canonical_key)` and
`candidates_from_misconceptions(entities, *, canon_key_by_canonical_key)` live in
`candidates.py` so tests (and WU-4A) build the set deterministically.

### 4.2 Resolver result model — `apollo/resolution/result.py`

```python
@dataclass(frozen=True)
class ResolvedNode:
    node_id: str                  # the student :_KGNode id
    resolution: str               # 'resolved' | 'unresolved' | 'ambiguous'
    resolved_key: str | None      # canonical_key of the matched target, else None
    resolved_canon_key: int | None  # :Canon key for the RESOLVES_TO edge
    method: str                   # one of RESOLUTION_METHODS
    confidence: float             # already capped by METHOD_CONFIDENCE_CAP[method]

@dataclass(frozen=True)
class ResolutionResult:
    resolved: tuple[ResolvedNode, ...]      # one per student node (incl. unresolved)
    tier_counts: Mapping[str, int]          # per-method histogram (§6.7 tier distribution)
    llm_calls: int                          # MUST be <= 1 (one adjudication/attempt max)
    def edges(self) -> tuple[ResolvesToEdgeSpec, ...]: ...  # only resolved+canon_key nodes
```

### 4.3 Resolver entry point — `apollo/resolution/resolver.py`

```python
MAX_STUDENT_NODES = 150  # §5 bounded-assignment cap; over it -> abstain (no hang)

def resolve_attempt(
    student_graph: KGGraph,
    candidates: tuple[Candidate, ...],
    *,
    llm_adjudicator: Callable[[ResolutionLLMRequest], ResolutionLLMReply] | None = None,
    fuzzy_threshold: float = 0.9,
) -> ResolutionResult:
    """Resolve every student evidence node against the closed candidate set.
    Pure + synchronous + deterministic given a deterministic adjudicator.
    `llm_adjudicator` defaults to the real one-call main_chat wrapper; tests
    inject a deterministic stub. Raises ResolutionInvalidOutputError on a
    hallucinated LLM key; never raises for a non-match (that is `unresolved`
    DATA). Over MAX_STUDENT_NODES the whole attempt abstains (all `unresolved`,
    method 'unresolved')."""
```

Decomposition (each a small testable seam; all pure except the adjudicator):
- `tiers.py`: `match_exact`, `match_symbolic` (REUSE `parse_zero_form`), `match_alias`, `match_fuzzy` — each `(student_node, candidates) -> (Candidate, method, raw_score) | None`. `_fuzzy_ratio(a, b) -> float` is the only RapidFuzz abstraction (`rapidfuzz.fuzz.token_set_ratio(a, b) / 100.0`); threshold ≥ 0.9.
- `structural.py`: `propagate_and_veto(anchors, student_graph, candidates)` — neighbor prioritization, neighborhood-agreement confidence boost, type-compat HARD constraint, structural-incoherence veto.
- `competition.py`: `apply_misconception_competition(scored)` — a polar near-miss out-competes the lexically-close reference node when a `misc.*` candidate scores higher; `polarity_screen(student_text, candidate)` rejects direction-inverted fuzzy matches.
- `assignment.py`: `greedy_global_assignment(scored, cap=MAX_STUDENT_NODES)` — descending-score greedy, many-student→one-reference allowed, one-student-never-splits, deterministic tie-break by `(node_id, canonical_key)`.
- `adjudication.py`: `adjudicate(remaining, candidates, llm)` — at most ONE `main_chat` call for ALL remaining ambiguous nodes; "return empty when unsure"; hallucinated key → `ResolutionInvalidOutputError`.

### 4.4 Persistence — `apollo/knowledge_graph/resolution_store.py` (NEW module)

```python
@dataclass(frozen=True)
class ResolvesToEdgeSpec:
    node_id: str            # student :_KGNode id
    canon_key: int          # :Canon target key
    method: str
    confidence: float
    resolved_at: str        # ISO-8601 UTC

@dataclass(frozen=True)
class ResolutionFieldSpec:
    node_id: str
    resolution: str         # resolved | unresolved | ambiguous
    resolved_key: str | None
    resolution_method: str
    resolution_confidence: float

# Pure mapping seams (DB-free):
def resolved_node_to_edge_spec(rn: ResolvedNode, *, resolved_at: str) -> ResolvesToEdgeSpec | None
def resolved_node_to_field_spec(rn: ResolvedNode) -> ResolutionFieldSpec

async def write_resolves_to(neo, attempt_id: int, specs: list[ResolvesToEdgeSpec]) -> int
    """Idempotent MERGE of (:_KGNode {attempt_id,node_id})-[:RESOLVES_TO]->(:Canon {key})
    edges. Empty -> 0 without opening a session. Raises
    ResolutionUnavailableError(stage='write_resolves_to') on Neo4j failure."""

async def persist_resolution_fields(neo, attempt_id: int, specs: list[ResolutionFieldSpec]) -> int
    """SET resolution/resolved_key/resolution_method/resolution_confidence on each
    :_KGNode (None-omission for resolved_key when None). Idempotent overwrite.
    Raises ResolutionUnavailableError(stage='persist_fields') on Neo4j failure."""

async def write_resolution(neo, attempt_id, result: ResolutionResult, *, resolved_at: str) -> ResolutionWriteResult
    """Convenience: edges + fields in two idempotent passes. ResolutionWriteResult{edges, fields}."""
```

`EDGE_ALLOWED_PAIRS` in `apollo/ontology/edges.py` is **not** modified — that map
constrains intra-subgraph `:_KGNode→:_KGNode` edges, and `RESOLVES_TO` is a
`:_KGNode→:Canon` cross-label edge with a fixed shape, written by its own Cypher
(not through `store.write_edges`). §6.3's "EDGE_ALLOWED_PAIRS incl RESOLVES_TO
evidence→Canon" is satisfied by documenting the contract in the resolution_store
docstring + the owner doc; do NOT add a `RESOLVES_TO` member to `EdgeType` (it is
not a within-graph typed edge and adding it would force every `EDGE_ALLOWED_PAIRS`
consumer/test to handle a 5th type). [Deviation note in §16 if the executor finds a consumer that needs it.]

### 4.5 `store._record_to_node` (the only `store.py` edit)

Add `resolution`, `resolved_key`, `resolution_method`, `resolution_confidence`
to the metadata-strip block already stripping `user_id`/`search_space_id`/`created_at`/`graded_at`,
so `read_graph` round-trips byte-identically once resolution fields are present.
No signature change.

## 5. Pipeline shape — the resolver's internal stages

This unit owns stages 5 + 7 of the §6.4 Done pipeline (WU-4A owns the rest):

```
[frozen student KGGraph]                          owner: store.read_graph (WU-3C1, read-only here)
  → [build closed candidate set]                  owner: resolution/candidates.build_candidate_set
      (this problem's reference nodes + course misc.* entities)
  → [content tiers: exact→symbolic→alias→fuzzy≥0.9]  owner: resolution/tiers.py (symbolic REUSES sympy_exec.parse_zero_form)
  → [structural propagation + neighborhood veto + type-compat HARD]  owner: resolution/structural.py
  → [misconception competition + polarity screen]  owner: resolution/competition.py
  → [bounded greedy global assignment (cap 150)]   owner: resolution/assignment.py
  → [ONE LLM adjudication for the remainder]        owner: resolution/adjudication.py (REUSES agent/_llm.main_chat)
  → [ResolutionResult]                              owner: resolution/result.py
  → [RESOLVES_TO MERGE edges :_KGNode→:Canon]       owner: knowledge_graph/resolution_store.write_resolves_to
  → [resolution node-fields on :_KGNode]            owner: knowledge_graph/resolution_store.persist_resolution_fields
```

Per-stage failure mode:
- **candidate set empty** (no reference nodes): every student node `unresolved`; no edges. (Reference-graph validation is WU-3B/WU-4A's gate, not the resolver's — the resolver tolerates an empty set as data.)
- **content/structural/competition/assignment**: pure functions — a non-match is `unresolved` DATA (no edge, logged), never an exception.
- **over MAX_STUDENT_NODES**: whole attempt abstains to `unresolved` (no unbounded solve, no hang).
- **LLM adjudication**: hallucinated key → `ResolutionInvalidOutputError` (hard); infra/timeout/empty-content → `ResolutionUnavailableError` (does NOT void the grade).
- **RESOLVES_TO / field write**: Neo4j failure → `ResolutionUnavailableError`; idempotent MERGE means a crashed mid-write is safe to retry.

## 6. Idempotency

- **Unit of work / key:** one **attempt** (`attempt_id`). Within it, each `RESOLVES_TO` edge is keyed by `(attempt_id, student node_id, :Canon key)`; each resolution-field write is keyed by `(attempt_id, node_id)`.
- **Content-addressing preference (§ idempotency rule):** the matching itself is content-addressed — the symbolic tier compares SymPy zero-forms (path-independent), the alias/fuzzy tiers compare surface text. The resolver is a **pure function** of `(student_graph, candidates)` given a deterministic adjudicator, so re-running on the same input yields the same `ResolutionResult` (no row-id dependence, no RNG, deterministic tie-breaks).
- **Duplicate handling:** `RESOLVES_TO` is written with `MERGE (a)-[:RESOLVES_TO]->(c)` then `SET` the three props — a second run **overwrites** identical props, never duplicates the edge (mirrors WU-3C1 `:Canon` MERGE). Resolution fields are `SET` (overwrite). Re-resolution after a reference edit changes the props in place; it never appends.
- **Partial-progress recovery:** because every write is an idempotent MERGE/SET, a worker that crashed after writing some edges re-runs the whole attempt's resolution and converges to the same state. This is exactly the §6.4 "step-7 RESOLVES_TO are idempotent MERGEs; edges orphaned by a mid-pipeline crash are harmless and reconciled on retry" contract. The `learner_update_pending` flag (set by WU-4A's orchestrator, not here) covers the cross-store window.
- **Common-mistake check:** no auto-increment key used for identity (node_id + canon-key, not DB serial); no string append (SET overwrite); no counter increment; MERGE not CREATE.

## 7. Failure paths (NO-FALLBACK)

Two named errors (append to `apollo/errors.py`, mirroring `CanonProjectionError`/`RetentionError`):

```python
class ResolutionUnavailableError(ApolloError):
    """Resolver INFRASTRUCTURE failure (LLM adjudication call failed/timed out,
    or a Neo4j RESOLVES_TO / resolution-field write failed). NO FALLBACK and —
    critically — must NOT void the earned grade: at Done the grade/XP are already
    committed when resolution runs, so this surfaces loud while the caller sets
    learner_update_pending=true and the next Done/janitor retry re-runs resolution
    idempotently (§5 NO-FALLBACK, §6.4 transaction story). `stage` ∈
    {'llm_adjudication','write_resolves_to','persist_fields'}."""
    def __init__(self, *, stage: str, last_error: str) -> None: ...

class ResolutionInvalidOutputError(ApolloError):
    """The LLM adjudication call returned a key that is NOT in the closed
    candidate set (a hallucination). Hard error (§5) — the resolver must never
    fabricate a target. Carries the offending key + the allowed candidate keys
    for the audit log."""
    def __init__(self, *, returned_key: str, allowed_keys: tuple[str, ...]) -> None: ...
```

| External call | Retry policy | Fallback | DLQ / error sink | Observability |
|---|---|---|---|---|
| **LLM adjudication** (`main_chat`, 1 call/attempt MAX) | No in-resolver retry (one call/attempt is binding). Transient failure → `ResolutionUnavailableError(stage='llm_adjudication')`. | None inside the resolver. The Done-orchestrator (WU-4A) catches it, commits the grade, sets `learner_update_pending`, retries the whole resolution later. | `learner_update_pending=true` on the attempt is the retry queue (WU-4A owns the flag write). | `_llm` already logs `llm_call`; resolver logs `resolution_adjudication nodes=N` and `resolution_llm_invalid returned=… ` on the hallucination path. |
| **`RESOLVES_TO` write / field persist** (Neo4j) | No retry here; idempotent MERGE makes a higher-level retry safe. Failure → `ResolutionUnavailableError(stage='write_resolves_to'|'persist_fields')`. | None; grade already committed. | Same `learner_update_pending` retry path. | Log `resolution_write attempt_id=… edges=… fields=…`. |
| **Per-node non-match** | n/a — DATA, not error. | `resolution='unresolved'`, no edge, no event. | Counts toward the per-run unresolved-rate metric (consumed by WU-4A abstention gate; this unit only records it in `tier_counts`). | Log `resolution_unresolved attempt_id=… node_id=… ` at debug; `tier_counts` returned. |

No "best effort" anywhere: matches are capped-confidence DATA; infra failures are named errors that preserve the grade; hallucinations are hard errors.

## 8. Security check

- **API key:** the LLM call goes through `agent/_llm.main_chat`, which constructs `OpenAI()` reading `OPENAI_API_KEY` from env only. The resolver never sees or passes a key. ✓
- **No secrets in rows/logs:** resolver logs node ids, canonical keys, method, confidence, and tier counts — never the API key, never raw transcript free-text beyond what is already in node content. The adjudication prompt contains node content + candidate display names/aliases (curriculum vocabulary), not student PII (no name/email — §2/RQ7: typed claims keyed by opaque ids only). ✓
- **No new PII in embeddings:** this unit does no embedding. ✓
- **Scoping / isolation invariant (§1.4):** the candidate set is built from **this attempt's** problem reference nodes + **this course's** misconception entities; the RESOLVES_TO target `:Canon` nodes already carry `search_space_id` (WU-3C1). The resolver never reaches across courses — the closed candidate set is the isolation boundary. The Neo4j writes match on `attempt_id` (already course-scoped via the Layer-2 node props) and a supplied `canon_key`, never a course-blind global lookup. ✓
- **Service-role / client reach:** all code is backend-only (resolver + Neo4j writer); nothing client-reachable. ✓
- **NO-FALLBACK preserved:** failure modes raise named errors registered for handling by the unit that wires the route (WU-4A / §8B), exactly as WU-3C1 deferred handler registration. This unit does NOT touch `api.py`. ✓

## 9. Files to create / edit

**Create (new `apollo/resolution/` package):**
- `apollo/resolution/__init__.py` — re-exports `resolve_attempt`, `build_candidate_set`, `Candidate`, `ResolvedNode`, `ResolutionResult`, `METHOD_CONFIDENCE_CAP`, the two adapters.
- `apollo/resolution/candidates.py` — `Candidate`, `RESOLUTION_METHODS`, `METHOD_CONFIDENCE_CAP`, `build_candidate_set`, `candidates_from_reference_solution`, `candidates_from_misconceptions`.
- `apollo/resolution/result.py` — `ResolvedNode`, `ResolutionResult`.
- `apollo/resolution/tiers.py` — `match_exact`/`match_symbolic`/`match_alias`/`match_fuzzy`, `_fuzzy_ratio` (the only RapidFuzz site).
- `apollo/resolution/structural.py` — `propagate_and_veto`, type-compat constraint, neighborhood boost.
- `apollo/resolution/competition.py` — `apply_misconception_competition`, `polarity_screen`.
- `apollo/resolution/assignment.py` — `greedy_global_assignment`.
- `apollo/resolution/adjudication.py` — `adjudicate`, `ResolutionLLMRequest`/`ResolutionLLMReply`, the real `main_chat` wrapper.
- `apollo/resolution/resolver.py` — `resolve_attempt` (orchestrates the above), `MAX_STUDENT_NODES`.
- `apollo/resolution/tests/__init__.py` + the pure-unit test modules (§11).

**Create (persistence half):**
- `apollo/knowledge_graph/resolution_store.py` — `ResolvesToEdgeSpec`/`ResolutionFieldSpec`/`ResolutionWriteResult`, `_RESOLVES_TO_MERGE_CYPHER`, `_RESOLUTION_FIELDS_CYPHER`, pure mapping seams, `write_resolves_to`/`persist_resolution_fields`/`write_resolution`.
- Real-Neo4j tests in `apollo/knowledge_graph/tests/` (§11) — reuse this dir's `tc_neo4j` conftest.

**Edit (minimal):**
- `apollo/errors.py` — append `ResolutionUnavailableError`, `ResolutionInvalidOutputError`.
- `apollo/knowledge_graph/store.py` — `_record_to_node`: strip the 4 resolution props (≤6 lines). No other change.
- `docs/architecture/apollo.md` — owner-doc updates (§12), `last_verified: 2026-06-16`.

**Test placement (per binding constraint):**
- pure-unit (mocked LLM) → `apollo/resolution/tests/**` and `apollo/knowledge_graph/tests/**` (the mapping/spec tests).
- real-Neo4j (Testcontainers) → `apollo/knowledge_graph/tests/**` (RESOLVES_TO writes, field round-trip) using `tc_neo4j`. The deeper DB-integration RESOLVES_TO test also lands as a `tests/database/**` module if it needs Postgres `:Canon` seeding via `project_canon` end-to-end (§11 test 28).

**Do NOT touch:** `apollo/api.py`, `apollo/handlers/done.py` (WU-4A orchestration), `apollo/solver/sympy_exec.py` (reuse only), `apollo/agent/_llm.py` (consume only), `apollo/ontology/edges.py` (no `RESOLVES_TO` EdgeType), migrations, remote Neo4j/Supabase.

## 10. TDD-ordered step-by-step

Write the test FIRST for each step (RED), then the minimal implementation (GREEN), then refactor. No skip/xfail/empty-assert. LLM mocked deterministically; Neo4j via `tc_neo4j` container (never Aura).

- [ ] **Step 1 — Named errors (RED→GREEN).**
  - Test first: `apollo/resolution/tests/test_resolution_errors.py` (or fold into `apollo/knowledge_graph/tests/` like WU-3C1's error tests) — asserts both classes subclass `ApolloError`, carry their attrs, and render them in `str()`.
  - Impl: append `ResolutionUnavailableError(stage,last_error)` + `ResolutionInvalidOutputError(returned_key,allowed_keys)` to `apollo/errors.py`.
  - Verify: `pytest apollo/resolution/tests/test_resolution_errors.py -q`.

- [ ] **Step 2 — Candidate model + builder (pure).**
  - Test first: `test_candidates.py` — `build_candidate_set` appends misconceptions; `candidates_from_reference_solution(problem_01)` yields a Candidate per reference step with correct `node_type`/`canonical_key`/`symbolic`; `candidates_from_misconceptions` carries `aliases`(=trigger_phrases) + `opposes_key`; `METHOD_CONFIDENCE_CAP` values exact match §3.
  - Impl: `candidates.py`.
  - Verify: `pytest apollo/resolution/tests/test_candidates.py -q`.

- [ ] **Step 3 — Result model (pure).**
  - Test first: `test_result.py` — `ResolutionResult.edges()` emits only resolved nodes with a `canon_key`; `tier_counts` histogram; frozen dataclasses.
  - Impl: `result.py`.

- [ ] **Step 4 — Content tiers (pure; the heart).**
  - Test first: `test_tiers.py` — one assertion per tier (exact key; symbolic `A=πr²`↔`circular_area` with `d=2r`; alias `density is constant`→`cond.incompressibility` via aliases; fuzzy ≥0.9 via `_fuzzy_ratio`); below-threshold returns None; `_fuzzy_ratio` normalizes to 0..1.
  - Impl: `tiers.py` (symbolic REUSES `parse_zero_form`; `_fuzzy_ratio` = `token_set_ratio/100`).

- [ ] **Step 5 — Structural propagation + type-compat veto (pure).**
  - Test first: `test_structural.py` — anchor's neighbors prioritized; neighborhood agreement boosts confidence; type-compat HARD (condition never resolves to an equation candidate even at high text score); incoherent mapping vetoed.
  - Impl: `structural.py`.

- [ ] **Step 6 — Misconception competition + polarity screen (pure).**
  - Test first: `test_competition.py` — "pressure increases with speed" resolves to `misc.pressure_velocity_same_direction`, NOT the lexically-close `def.pressure_velocity_tradeoff`; polarity screen rejects a direction-inverted fuzzy match.
  - Impl: `competition.py`.

- [ ] **Step 7 — Bounded greedy global assignment (pure).**
  - Test first: `test_assignment.py` — many student nodes → one reference node (paraphrase) allowed; one student node never splits; descending-score order; deterministic tie-break; >150 nodes → abstain (no hang).
  - Impl: `assignment.py`.

- [ ] **Step 8 — LLM adjudication (mocked).**
  - Test first: `test_adjudication.py` — exactly one `main_chat` call for N remaining nodes (patch `apollo.resolution.adjudication.main_chat`, assert `call_count == 1`); "return empty when unsure" → those stay unresolved; hallucinated key → `ResolutionInvalidOutputError`; transient error → `ResolutionUnavailableError(stage='llm_adjudication')`.
  - Impl: `adjudication.py` (strict JSON `response_format`, `temperature=0.0`, `purpose="resolution_adjudication"`).

- [ ] **Step 9 — `resolve_attempt` orchestration + confidence caps (mocked LLM).**
  - Test first: `test_resolver.py` — the §6.9 worked example end-to-end (the 5 mappings); each resolved node's confidence == `METHOD_CONFIDENCE_CAP[method]` (exact/symbolic/alias/fuzzy/llm); below-threshold node → `unresolved`/0.0/no edge; `result.llm_calls <= 1`; >150 nodes abstains.
  - Impl: `resolver.py` + `__init__.py` re-exports.

- [ ] **Step 10 — Resolution persistence specs (pure mapping).**
  - Test first: `apollo/knowledge_graph/tests/test_resolution_store_mapping.py` — `resolved_node_to_edge_spec` returns None for unresolved / no-canon-key, else the 5-field spec; `resolved_node_to_field_spec` always returns the 4-field spec; Cypher uses `MERGE` (not CREATE) for the edge; field Cypher SETs the 4 props; None-omission for `resolved_key`.
  - Impl: `resolution_store.py` pure half + Cypher constants.

- [ ] **Step 11 — RESOLVES_TO write + field round-trip (real Neo4j, `tc_neo4j`).**
  - Test first: `apollo/knowledge_graph/tests/test_resolution_store_neo4j.py` — seed a `:_KGNode` + a `:Canon`, `write_resolves_to`, assert one `(:_KGNode)-[:RESOLVES_TO {method,confidence,resolved_at}]->(:Canon)`; `persist_resolution_fields` then `read_graph` round-trips the typed node byte-identically (the 4 props stripped); idempotent re-resolution (MERGE → same edge count); empty specs → 0 without opening a session; Neo4j failure (fake failing client) → `ResolutionUnavailableError`.
  - Impl: `resolution_store.py` async half; `store._record_to_node` 4-prop strip.

- [ ] **Step 12 — Folded WU-3C1 deferred nit (real Neo4j).**
  - Test first: in `test_resolution_store_neo4j.py` (or a focused `test_store_scoping_roundtrip_neo4j.py`) — `write_nodes` with scoping props then `read_graph` asserts the scoping props persisted on the raw node AND stripped from the reconstructed typed node content. (The WU-3C1 deferred nit, explicitly folded into this unit.)
  - Impl: none beyond Step 11's `_record_to_node` change (the strip already covers it); the test pins the behavior.

- [ ] **Step 13 — Owner-doc reconcile + `last_verified`.**
  - Edit `docs/architecture/apollo.md` per §12; set `last_verified: 2026-06-16`.
  - Verify: `grep -n "WU-3C2\|RESOLVES_TO\|apollo/resolution" docs/architecture/apollo.md`.

## 11. Full test list

Every test below is REAL (no skip/xfail/empty-assert). Mocking: the LLM is the
deterministic stub `llm_adjudicator` injected into `resolve_attempt` (or
`@patch("apollo.resolution.adjudication.main_chat")` for the adjudication unit
tests) — **no live OpenAI call ever fires**. Neo4j integration uses the local
`tc_neo4j` Testcontainers fixture — **never Aura**. One assertion-per-behavior.

### A. Pure-unit — errors (`apollo/resolution/tests/test_resolution_errors.py`)
1. `test_resolution_unavailable_error_is_apollo_error_and_carries_stage` — subclass + `.stage`/`.last_error` + `str()` contains both. (no mocks)
2. `test_resolution_invalid_output_error_carries_returned_and_allowed_keys` — `.returned_key`/`.allowed_keys` + `str()` contains the bad key.

### B. Pure-unit — candidates (`test_candidates.py`)
3. `test_build_candidate_set_appends_misconceptions` — misc.* always present so they compete. (no mocks)
4. `test_candidates_from_reference_solution_problem01` — 7 candidates; `eq.bernoulli` has `symbolic`, `node_type=='equation'`; `cond.incompressibility` `node_type=='condition'`; keys match `entity_key`. (loads `problem_01.json`)
5. `test_candidates_from_misconceptions_carry_aliases_and_opposes` — `misc.pressure_velocity_same_direction` has `trigger_phrases` as `aliases`, `opposes_key=='def.pressure_velocity_tradeoff'`, `is_misconception=True`. (loads `misconceptions.json`)
6. `test_method_confidence_caps_match_spec` — `{exact:1.0, symbolic:0.98, alias:0.92, fuzzy:0.80, llm:0.75, unresolved:0.0}` exactly.

### C. Pure-unit — result model (`test_result.py`)
7. `test_resolution_result_edges_only_resolved_with_canon_key` — unresolved + canon-keyless nodes excluded from `edges()`.
8. `test_resolution_result_tier_counts_histogram` — counts per method sum to node count.
9. `test_result_models_are_frozen` — `FrozenInstanceError` on attr assign.

### D. Pure-unit — content tiers (`test_tiers.py`)
10. `test_exact_tier_matches_identical_key` — exact `canonical_key`/identical content → method `exact`.
11. `test_symbolic_tier_circular_area_with_d_eq_2r` — student `A = pi*r**2` ↔ `eq.circular_area` (`A = pi*d**2/4`) is equivalent under `d=2r` via `parse_zero_form` + `.subs`; `simplify(a-b)==0`. (REUSE sympy_exec; no mock)
12. `test_symbolic_tier_sign_exact_rejects_inverted` — `A = -pi*r**2` does NOT match (sign-exact).
13. `test_alias_tier_density_is_constant_to_incompressibility` — "density is constant" matches `cond.incompressibility` via its alias set.
14. `test_fuzzy_tier_above_threshold` — a ≥0.9 `_fuzzy_ratio` paraphrase matches; method `fuzzy`.
15. `test_fuzzy_tier_below_threshold_returns_none` — <0.9 → None (no snap).
16. `test_fuzzy_ratio_normalized_0_to_1` — `_fuzzy_ratio('abc','abc')==1.0`, disjoint < 0.9.

### E. Pure-unit — structural (`test_structural.py`)
17. `test_structural_propagation_prioritizes_anchor_neighbors` — given an anchored match, the anchor's edge-neighbor candidates are scored first.
18. `test_neighborhood_agreement_boosts_confidence` — corroborated mapping gets a higher score than the same match in isolation.
19. `test_type_compatibility_hard_constraint` — a `condition` student node never resolves to an `equation` candidate even at top text score (cross-type forbidden).
20. `test_structurally_incoherent_mapping_vetoed` — an inconsistent neighbor mapping is rejected.

### F. Pure-unit — competition (`test_competition.py`)
21. `test_polar_near_miss_resolves_to_misconception_not_reference` — "pressure increases with speed" → `misc.pressure_velocity_same_direction`, NOT the lexically-close `def.pressure_velocity_tradeoff`. (the §6.11 adversarial fixture)
22. `test_polarity_screen_rejects_direction_inverted_fuzzy` — a high-fuzzy but direction-inverted candidate is screened out.

### G. Pure-unit — assignment (`test_assignment.py`)
23. `test_many_students_merge_into_one_reference_paraphrase` — two student nodes both → one reference node (allowed).
24. `test_one_student_never_splits` — a single student node maps to at most one target.
25. `test_descending_score_order_and_deterministic_tiebreak` — assignment is greedy by score; ties break on `(node_id, canonical_key)` (run twice → identical).
26. `test_over_cap_abstains_no_hang` — 151 synthetic student nodes → all `unresolved` (method `unresolved`), returns promptly (pathological graph routed to abstention, not an unbounded solve).

### H. Pure-unit — adjudication (mocked LLM) (`test_adjudication.py`)
27. `test_one_llm_call_max_for_all_remaining` — `@patch(...main_chat)`; N ambiguous nodes → `main_chat.call_count == 1`.
28. `test_llm_return_empty_keeps_unresolved` — stub returns `{}` → those nodes stay unresolved.
29. `test_llm_hallucinated_key_raises_invalid_output` — stub returns a key not in candidates → `ResolutionInvalidOutputError` with `returned_key` + `allowed_keys`.
30. `test_llm_transient_failure_raises_unavailable` — stub raises → `ResolutionUnavailableError(stage='llm_adjudication')`.
31. `test_llm_resolved_node_capped_at_0_75` — an LLM-resolved node has confidence == 0.75.

### I. Pure-unit — resolver orchestration (mocked LLM) (`test_resolver.py`)
32. `test_worked_example_6_9_end_to_end` — the §6.9 student graph resolves: density-is-constant→`cond.incompressibility` (alias), A=πr²→`eq.circular_area` (symbolic), use-Bernoulli→`eq.bernoulli` (exact/alias), four-times-speed→`proc.compute_v2`, pressure-lower-at-narrow→`def.pressure_velocity_tradeoff`; LLM stub never needed (asserts `llm_calls==0`). [Fixture builds the candidate set from the bernoulli reference + the authored def + misc entities.]
33. `test_confidence_equals_method_cap_per_node` — each resolved node's confidence == its method's cap.
34. `test_below_threshold_node_is_unresolved_data_no_edge` — a vague pronoun node → `unresolved`/0.0/method `unresolved`; absent from `edges()`; logged, no exception.
35. `test_result_llm_calls_at_most_one` — across a graph needing adjudication, `result.llm_calls == 1`.
36. `test_resolver_is_pure_same_input_same_output` — two runs on the same `(graph, candidates)` with the same stub → identical `ResolutionResult` (idempotency/determinism).
37. `test_resolver_over_cap_abstains` — >150 nodes → whole-attempt abstention; `llm_calls==0`.
38. `test_empty_candidate_set_all_unresolved` — no reference nodes → every student node unresolved; no edges.

### J. Pure-unit — persistence mapping (`apollo/knowledge_graph/tests/test_resolution_store_mapping.py`)
39. `test_resolved_node_to_edge_spec_skips_unresolved` — unresolved / canon-keyless → None.
40. `test_resolved_node_to_edge_spec_shape` — resolved node → `ResolvesToEdgeSpec{node_id,canon_key,method,confidence,resolved_at}`.
41. `test_resolved_node_to_field_spec_always_present` — every node → the 4-field spec (incl. unresolved).
42. `test_resolves_to_cypher_uses_merge_not_create` — `MERGE` present, `CREATE (` for the edge absent (idempotency guard, mirrors WU-3C1 test 137).
43. `test_resolution_fields_cypher_sets_four_props` — Cypher SETs `resolution`/`resolved_key`/`resolution_method`/`resolution_confidence`.
44. `test_persist_fields_empty_returns_zero_without_session` — empty spec list short-circuits to 0; `_ExplodingNeo.session` never entered (mirrors WU-3C1 test 184).
45. `test_write_resolves_to_neo4j_failure_raises_unavailable` — fake failing client → `ResolutionUnavailableError(stage='write_resolves_to')` (fake async session; no live infra, mirrors WU-3C1 test 141).
46. `test_persist_fields_neo4j_failure_raises_unavailable` — same for `persist_fields`.

### K. Real-Neo4j (Testcontainers `tc_neo4j`) (`apollo/knowledge_graph/tests/test_resolution_store_neo4j.py`)
47. `test_write_resolves_to_creates_typed_edge` — seed `:_KGNode` + `:Canon`; `write_resolves_to` → exactly one `(:_KGNode)-[:RESOLVES_TO {method,confidence,resolved_at}]->(:Canon {key})`.
48. `test_persist_resolution_fields_round_trip` — `persist_resolution_fields` then `KGStore.read_graph` reconstructs the typed node byte-identically (4 resolution props stripped, content unchanged).
49. `test_re_resolution_is_idempotent_merge` — running `write_resolution` twice → same edge count (1), props overwritten not duplicated.
50. `test_resolution_field_props_persist_on_node` — raw Cypher read shows the 4 props on the node after `persist_resolution_fields`.
51. `test_write_resolution_edges_and_fields_counts` — `ResolutionWriteResult{edges,fields}` matches the resolved/persisted counts.

### L. Real-Neo4j — folded WU-3C1 deferred nit (`tc_neo4j`)
52. `test_write_nodes_scoping_props_persist_and_strip_on_read` — `KGStore.write_nodes(..., user_id, search_space_id)` then raw read shows `user_id`/`search_space_id`/`created_at` on the node, AND `read_graph` returns a typed node whose content has none of them (persisted-and-stripped). (The explicit WU-3C1 carry-over.)

### M. DB-integration (optional end-to-end, `tests/database/test_resolution_resolves_to_postgres.py`)
53. `test_resolves_to_targets_projected_canon` — seed Layer-1 entities in Postgres, `project_canon` (WU-3C1) into `tc_neo4j`, build candidates with the real `:Canon` keys, resolve the §6.9 student graph, `write_resolution`, assert each `RESOLVES_TO` lands on the correct `:Canon` node by key. (Proves the resolver's `resolved_canon_key` matches the projection's surrogate key — the full WU-3B→3C1→3C2 chain. Uses the Postgres + `tc_neo4j` harness; mark `integration`.)

**Coverage note:** the pure-unit suites (A–J) exercise every branch of the
resolver and the persistence mapping. The real-Neo4j suites (K–M) cover the
write/round-trip/idempotency branches that need a live graph. Together they hold
patch coverage ≥ 95% vs `feat/apollo-kg-wu3c1-canon-projection`; any genuinely
unreachable defensive branch (e.g. a re-raise of an already-named error) is
covered by a fake-client test (45/46), as WU-3C1 did for its `merge_specs`
re-raise.

## 12. Owner-doc updates (drift contract)

Edit `docs/architecture/apollo.md` in the SAME work; set `last_verified: 2026-06-16`. Concretely:

1. **Module map** — add a new `apollo/resolution/` row: "the §5 shared reference-anchored resolver (WU-3C2), standalone so WU-4A `retired graph comparator` imports it: `resolve_attempt(student_graph, candidates, *, llm_adjudicator=None)` → `ResolutionResult`; content-first tiers (exact→SymPy-symbolic via `parse_zero_form`→alias→RapidFuzz≥0.9) + structural propagation/neighborhood veto + type-compat HARD constraint + misconception competition + bounded greedy assignment (cap 150) + one LLM adjudication via `main_chat` (`return empty when unsure`, hallucination→`ResolutionInvalidOutputError`); confidence caps by method (exact 1.0/symbolic 0.98/alias 0.92/fuzzy 0.80/LLM 0.75/unresolved 0.0)."
2. **`apollo/knowledge_graph/` row** — extend: "**`resolution_store.py`** (WU-3C2) writes `(:_KGNode)-[:RESOLVES_TO {method,confidence,resolved_at}]->(:Canon)` (idempotent MERGE) and persists the four Layer-2 resolution node-fields (`resolution`/`resolved_key`/`resolution_method`/`resolution_confidence`); `store._record_to_node` now also strips those four props so `read_graph` round-trips byte-identically. `RESOLVES_TO` is a `:_KGNode→:Canon` cross-label edge written by its own Cypher — NOT a member of `EdgeType`/`EDGE_ALLOWED_PAIRS`." Update the trailing "The §5 resolver, RESOLVES_TO edges, and :Canon reads are WU-3C2" sentence to "delivered in WU-3C2 (`apollo/resolution/` + `resolution_store.py`)."
3. **Public interfaces** — add `apollo.resolution.resolve_attempt(...) -> ResolutionResult` and `apollo.knowledge_graph.resolution_store.write_resolution(neo, attempt_id, result, *, resolved_at) -> ResolutionWriteResult` (+ `write_resolves_to`/`persist_resolution_fields`).
4. **Neo4j shape** — note the new `:_KGNode→:Canon` `RESOLVES_TO` edge + the four resolution props on `:_KGNode` (server-side, stripped on read like the other metadata).
5. **NO-FALLBACK policy** — add `ResolutionUnavailableError` (infra; surfaces without voiding the grade; `learner_update_pending` retry path; handlers registered by WU-4A, not here) and `ResolutionInvalidOutputError` (hard, hallucinated key) to the named-error list, mirroring the WU-3C1 note that these are deliberately NOT registered as HTTP handlers in this unit.
6. **Tests** — add the LIVE resolver/persistence test modules to the "LIVE (non-skipped)" list with the mocking note (LLM stub; `tc_neo4j` container; never Aura).
7. If `domain-data.md` owns `tests/database/**`, add the optional test 53 module there; otherwise note it under apollo's test list. (Check `owns:` globs before editing a second doc.)

## 13. Verification

- [ ] **Full suite green:** `pytest apollo/resolution apollo/knowledge_graph/tests -q` (pure-unit + container tests; Docker required for `tc_neo4j`).
- [ ] **Patch coverage gate:** `pytest --cov=apollo --cov-report=xml` then `diff-cover coverage.xml --compare-branch=feat/apollo-kg-wu3c1-canon-projection --fail-under=95`.
- [ ] **Manual smoke (the §6.9 fixture):** build the candidate set from `problem_01.json` + `misconceptions.json` + the authored def, feed the §6.9 student graph, assert the 5 expected mappings + each method's confidence cap (test 32/33).
- [ ] **Idempotency replay:** run `resolve_attempt` twice on the same input → identical `ResolutionResult` (test 36); run `write_resolution` twice on `tc_neo4j` → same edge count (test 49).
- [ ] **"DLQ"/error-sink test:** transient LLM failure → `ResolutionUnavailableError`, grade-preservation contract documented; Neo4j write failure → `ResolutionUnavailableError` (tests 30/45/46). Hallucinated key → `ResolutionInvalidOutputError` (test 29).
- [ ] **Backpressure test:** 151-node graph → abstain promptly, no hang, one LLM call max never exceeded (tests 26/37).
- [ ] **No-live-network proof:** `grep -rn "OpenAI()\|main_chat\|NEO4J_URI" apollo/resolution apollo/knowledge_graph/tests/test_resolution_*` shows every LLM path is patched/injected and every Neo4j path uses `tc_neo4j` (no Aura).
- [ ] **Import-only seam:** `python -c "from apollo.resolution import resolve_attempt"` (WU-4A's import contract).
- [ ] **Owner doc:** `last_verified: 2026-06-16`; `grep` for `WU-3C2`/`RESOLVES_TO`/`apollo/resolution` returns the new rows.
- [ ] **File ceiling:** `wc -l apollo/knowledge_graph/store.py` still < 800 (we added ~6 lines, not the writer); each new `apollo/resolution/*.py` < 300.

## 14. Risks

| # | Risk | Confidence | Mitigation |
|---|---|---|---|
| 1 | **Symbolic-tier symbol context.** `parse_zero_form`'s `_CANONICAL_SYMBOLS` lacks `r`/`d` (circle radius/diameter). The §6.9 `A=πr²↔πd²/4` test needs `r` and `d` parseable. | MEDIUM | The symbolic tier passes a local symbol dict that EXTENDS `_local_dict()` with the equation's declared variables + the declared mapping symbols (`d`,`r`), WITHOUT modifying `sympy_exec.py` (build the extra symbols in `tiers.py` and substitute before comparison; if `parse_expr` needs them in `local_dict`, wrap `parse_zero_form` by pre-registering them). Test 11/12 pin this. If the wrapper proves insufficient, fall back to comparing under `sympify` with an explicit `locals` dict in `tiers.py` (still no solver edit). |
| 2 | **`:Canon` key vs `canonical_key` space.** The resolver matches in `canonical_key` space but RESOLVES_TO needs the surrogate `:Canon.key`. | LOW | `Candidate.canon_key` carries the surrogate key from the start (built by the adapter from the WU-3C1 projection); `resolved_canon_key` flows straight to the edge spec. Test 53 proves the chain against a real projection. |
| 3 | **Cost surprise.** One `main_chat` (gpt-4o) call per attempt at Done. | LOW | Bounded by construction: at most ONE call/attempt (tests 27/35), only for the post-tier remainder, Done-time not per-turn. ~$0.004/call order (RQ3 spike scale). No new budget line; within the existing Apollo Done-path LLM budget (§11: three constrained-verification roles). |
| 4 | **External-API availability (OpenAI).** Adjudication call could fail. | LOW | `ResolutionUnavailableError` + grade-never-voided + `learner_update_pending` idempotent retry (the whole point of the NO-FALLBACK design). |
| 5 | **Schema lock during deploy.** | NONE | This unit ships NO migration (RESOLVES_TO + resolution props are schemaless Neo4j; the `:Canon(key)` constraint already shipped in WU-3C1). No Postgres DDL, no lock. |
| 6 | **Over-normalization regression** (collapsing `bernoulli_full`/`bernoulli_horizontal` variants). | MEDIUM | Variants stay distinct candidates (separate `entity_key`s in the reference solution); the resolver never merges candidates, only maps students onto them. Polarity screen + type-compat + below-threshold→unresolved are tested (19/22/34). Add a variant-distinctness assertion to test 4 if the executor sees risk. |
| 7 | **EDGE_ALLOWED_PAIRS expectation.** A reviewer may expect §6.3's "RESOLVES_TO evidence→Canon" to be an `EdgeType` member. | LOW | Documented as a cross-label edge with its own Cypher (§4.4); rationale in the owner doc. Deviation in §16 if a real consumer needs the enum member. |

## 15. Explicit out-of-scope (anti-scope, binding)

This unit is ONLY the resolver + RESOLVES_TO + resolution fields + named errors. NOT in scope:
- **Grading / graph simulation** (coverage/soundness/bisimilarity, sub-scores) — WU-4A `apollo/retired graph comparator/`.
- **Transcript auditor / abstention gates / missing-node gate** — WU-4B.
- **Learner-model events / belief update / decision table / Layer-3** — WU-5A.
- **Done-pipeline orchestration** (`done.py` step ordering, freeze, `learner_update_pending` write, the cross-store transaction staging) — WU-4A.
- **Reference normalization beyond identity** (v1 reference graph is canonical by construction; fuzzy reference work is v2).
- **`:Canon` projection / seeding** — WU-3C1 (consumed, not built).
- **Reference-graph validation** (`validate_reference_graph`) — WU-3B (the resolver tolerates an empty/invalid set as data; blocking is the orchestrator's gate).
- **`api.py` handler registration** for the two new errors — the unit that wires a triggering route (WU-4A) registers them, exactly as WU-3C1 deferred its two errors' handlers.
- **Any migration** (no Postgres DDL; no remote Neo4j/Supabase writes).
- **New packages** beyond `rapidfuzz` (already pinned). Do NOT install anything.
- **`apollo/solver/sympy_exec.py` changes** — REUSE `parse_zero_form` only.
- **`EdgeType`/`EDGE_ALLOWED_PAIRS` changes** — RESOLVES_TO is not a within-graph typed edge.

## 16. Deviations I'd allow the executor

- **Package shape:** if a single `apollo/resolution/resolver.py` + `apollo/resolution/tiers.py` is cleaner than the 6-file split AND every file stays < 300 lines and one-concern, collapse `structural.py`/`competition.py`/`assignment.py` into `resolver.py`'s helpers — provided each behavior still has its own named test. Do NOT collapse `tiers.py`, `adjudication.py`, `candidates.py`, `result.py` (distinct seams the tests + WU-4A target).
- **RESOLVES_TO as an `EdgeType`:** if the executor finds a concrete consumer (e.g. WU-4A `validator.py`'s grammar already enumerates a `RESOLVES_TO: evidence→Canon` row per §6.3) that genuinely needs the enum member, adding `RESOLVES_TO` to `EdgeType` + `EDGE_ALLOWED_PAIRS` IS allowed — but then every existing `EDGE_ALLOWED_PAIRS` test/consumer must be updated in the same change and `store.write_edges` must keep refusing `:Canon` endpoints (it matches `:_KGNode` only). Default remains: keep it out of the enum.
- **Symbolic wrapper location:** the extra-symbol handling for tier-2 (risk 1) may live as a thin `_symbolic_equiv(a, b, *, mappings)` helper in `tiers.py`; do NOT push it into `sympy_exec.py`.
- **Fuzzy scorer choice:** `token_set_ratio` vs `ratio` — either is fine behind `_fuzzy_ratio`, pick whichever passes test 14/15 deterministically on the §6.9 paraphrases; document the choice in the docstring.
- **Test file granularity:** merging closely-related pure-unit modules (e.g. `test_result.py` into `test_resolver.py`) is fine as long as every numbered behavior keeps a distinct test function name.
- **Adjudicator default:** whether `resolve_attempt`'s default adjudicator is a module-level real-`main_chat` wrapper or `None`-meaning-skip is the executor's call — but tests MUST inject a stub and the live path MUST be reachable only with an explicit adjudicator (no accidental live call in CI).
