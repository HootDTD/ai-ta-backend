# Apollo Textbook Problem Index — Design Spec

**Date:** 2026-06-02
**Branch:** ApolloV3
**Status:** Draft awaiting user approval
**Parent context:** Builds on the V3 concept registry, typed ontology, and Neo4j persistence layer introduced in `ApolloV3` commit `a841616` ("1.0").

## 1. Problem Framing

Apollo's problem bank is currently a hand-authored set of five JSON files under `apollo/subjects/fluid_mechanics/concepts/bernoulli_principle/problems/`. Each problem ships with the prose statement, given values, target unknown, and a typed `reference_solution` graph that the grader weighs the student's KG against. Authoring one new concept is days of work; authoring one new problem within an existing concept is hours. The bank does not scale.

The repo already contains a working textbook indexing and retrieval stack (`indexing/`, `retrieval/`, `citations/`, `knowledge/teacher_pdf_ingestion.py`) built for the Q&A side of the system. The opportunity is to plug Apollo into that stack: when a teacher uploads a textbook PDF (or when the system's seed textbook is ingested), problems found inside the book — worked examples and end-of-chapter exercises — are extracted, classified, validated, and stored as Apollo problems automatically. Concepts that don't yet exist in Apollo's registry are discovered and authored from the textbook in the same pass.

This spec defines that ingest pipeline.

**What we're building:**

1. A new `apollo/textbook_ingest/` module that runs as a post-processing pass of the existing `teacher_pdf_ingestion` pipeline. Six sequential stages: concept **discovery**, concept-registry **authoring**, **problem detection**, **problem extraction**, **validation**, and **persistence**.
2. A dedup sub-stage between discovery and authoring that resolves concept candidates against the existing Neo4j-resident concept registry — first-writer-wins on policy files.
3. Neo4j schema additions for concepts (`:Concept` + `:CanonicalSymbol` + `:NormalizationEntry` + `:SolverConstant` + `:ForbiddenTerm`), problems (`:Problem` + reference-solution subgraphs), cluster aliases (`:ClusterAlias`), validator rejections (`:_RejectedProblem`), and ingest events (`:_IngestEvent`).
4. A migration of the hand-authored Bernoulli content from the on-disk JSON folder into Neo4j, runnable once, idempotent.
5. A rewrite of `apollo/overseer/problem_selector.py` to query Neo4j instead of the filesystem, plus a Neo4j-backed reimplementation of `apollo/subjects/__init__.py:load_concept()`.
6. A test layer covering per-stage unit tests, validator gate fixtures (positive + adversarial), dedup-resolver tests, selector tests, a migration regression test, and a tiered end-to-end smoke against a synthetic mini-textbook + a real textbook.

**What we're NOT changing:**

- The student-attempt knowledge-graph side of Neo4j (`:_KGNode` namespace, `attempt_id` scoping). Unchanged.
- The Apollo handlers (`chat.py`, `done.py`, `lifecycle.py`, `next.py`, `restart_problem.py`). They keep receiving `Problem` Pydantic objects from the selector and don't know the source changed.
- The grader (`apollo/overseer/coverage.py`, `apollo/overseer/rubric.py`). Its inputs and outputs are unchanged; only the storage of the reference_solution changes.
- The Pydantic ontology (`apollo/ontology/`). Reference-solution nodes use the same node/edge types as student-attempt nodes.
- The Pydantic `Problem` schema (`apollo/schemas/problem.py`).
- The 13 V3 test modules currently `pytest.skip`'d. Re-enabling those is `claude_v3_checklist.md` work, out of scope here.

## 2. Locked Decisions

| Decision | Choice | Rationale |
|---|---|---|
| Source of problems | Existing problems printed in textbooks (worked examples + exercises) | Most ambitious of the discussed framings; aligns with how textbooks already structure content |
| Human in the loop | None — fully automatic | User direction; safety burden moves to the validator |
| Source breadth | Any indexed document (seed textbook + teacher uploads) | Book-agnostic extraction is the central hard problem |
| Concept tagging | Pure LLM classification per problem | Most flexible; requires confidence handling at the dedup boundary |
| Extraction timing | Eager, at PDF upload time | Keeps Apollo's selector deterministic and cheap |
| Storage backend | Neo4j (problems and concepts both) | Native fit for the typed reference_solution graph; commits to the V3 Neo4j bet |
| Concept registry source | Indexer authors entire registry entries from textbook content | User direction; LLM authors teaching policy files |
| Concept identity | Global, dedup-merged across textbooks | Single canonical concept per teaching topic |
| Policy mutability | Locked at first-write (first-writer-wins on all 5 policy files) | Mutation would break previously-extracted problems |
| Concept-authoring LLM calls | One call per policy file (five short calls per new concept) | Accuracy + cost control |
| Reference-node label scheme | Same node labels as student KG, differentiated by `:_ProblemNode` secondary label | Unified ontology; max Neo4j-native |
| Concept policy nodes | Multi-node (`:CanonicalSymbol`, etc.) rather than JSON properties on `:Concept` | Max Neo4j-native; enables future cross-concept queries |
| Reference-subgraph scoping | Structural (reachable from `Problem` root via `HAS_REFERENCE_NODE`) rather than property-based | Max Neo4j-native |
| Gate 7 (solver) strategy | Path 1 — closure check only, no symbolic execution | Existing solver doesn't apply simplifications; Path 2 is a follow-on |
| Rejection retry | None — rejections are permanent until document re-ingest | Simpler semantics |
| Cluster-alias creation | Automatic on concept creation (discovered `subject_id` → `:ClusterAlias`) | Keeps Hoot's `cluster_id` abstraction working without manual wiring |
| Observability namespace | Single `:_IngestEvent` namespace for runs, errors, rejections, dedup decisions | Cleaner mental model than splitting by event type |

## 3. Architecture & Components

The pipeline is six sequential stages, each its own module under `apollo/textbook_ingest/`. Every stage has an explicit Pydantic input type and output type so stages can be tested and replaced in isolation.

```
PDF indexed (existing teacher_pdf_ingestion pipeline finishes)
        │
        ▼
┌─────────────────────────────────────────────┐
│ 1. discovery.py — find concept candidates    │
│    LLM reads TOC + section headings, proposes│
│    List[ConceptCandidate]                    │
└─────────────────────────────────────────────┘
        │
        ▼
┌─────────────────────────────────────────────┐
│ 1.5 concept_resolver.py — dedup pass         │
│    Per candidate: slug match → embedding     │
│    similarity → LLM-judge tiebreaker.        │
│    Returns List[ConceptResolution]           │
│    {kind: "new" | "matched_existing"}        │
└─────────────────────────────────────────────┘
        │
        ▼
┌─────────────────────────────────────────────┐
│ 2. concept_authoring.py — registry entries   │
│    For each "new" resolution, five LLM calls:│
│      canonical_symbols, normalization_map,   │
│      parser_prompt_template, solver_hints,   │
│      forbidden_named_laws                    │
└─────────────────────────────────────────────┘
        │
        ▼
┌─────────────────────────────────────────────┐
│ 3. problem_detector.py — find candidates     │
│    Per chunk: "worked_example" | "exercise"  │
│    | "neither" classifier                    │
└─────────────────────────────────────────────┘
        │
        ▼
┌─────────────────────────────────────────────┐
│ 4. problem_extractor.py — extract typed      │
│    Per candidate: problem_text, given_values,│
│    target_unknown, reference_solution graph, │
│    concept_id (must resolve to stage 1.5),   │
│    difficulty                                │
└─────────────────────────────────────────────┘
        │
        ▼
┌─────────────────────────────────────────────┐
│ 5. validator.py — strict gate (8 checks)     │
│    Schema → closure → DAG → symbol           │
│    consistency → procedure coherence →       │
│    sympy parse → equation-system closure     │
│    → dedup. Short-circuit on first fail.     │
└─────────────────────────────────────────────┘
        │
        ▼
┌─────────────────────────────────────────────┐
│ 6. writer.py — Neo4j writes                  │
│    Atomic per concept. New concepts (policy  │
│    + problems) commit together. Existing     │
│    concepts only receive new problems.       │
└─────────────────────────────────────────────┘
        │
        ▼
Apollo selector queries Neo4j (unchanged callsite shape)
```

**Why six modules:**
- Each stage has its own dominant failure mode. Concept authoring will evolve faster than problem extraction; we want to replace one without redoing the others.
- Each stage is independently measurable. Stage 3 has a recall/precision answer; stage 4 has a per-field accuracy answer.
- The validator is its own module because it is the safety layer of the fully-automatic design. It must be exercisable on hand-written fixtures without LLM stages running.

**What stays the same in Apollo code:**
- `apollo/handlers/*.py` — untouched. Selector callsites still receive a `Problem` Pydantic object.
- `apollo/knowledge_graph/store.py` — the student-attempt KG write path is unchanged. Stays in the `:_KGNode` namespace.
- `apollo/ontology/` — node types, edge types, allowed-pair table are unchanged. Reference-solution subgraphs reuse the same Pydantic types.
- `apollo/parser/`, `apollo/overseer/coverage.py`, `apollo/overseer/rubric.py` — consume `ConceptDefinition` via `load_concept()`. The function signature and return type are preserved; only its implementation changes (Neo4j read instead of filesystem read).

**What becomes a thin shim:**
- `apollo/subjects/__init__.py` — `load_concept(subject_id, concept_id)` becomes a Neo4j query that reconstructs the same `ConceptDefinition` object. `list_subjects()` and `list_concepts()` become `MATCH (c:Concept) RETURN DISTINCT ...` queries.

## 4. Data Flow

### Stage Pydantic types (in `apollo/textbook_ingest/types.py`)

```
Stage 1 (discovery)         ──►  List[ConceptCandidate]
Stage 1.5 (resolver)        ──►  List[ConceptResolution]
Stage 2 (concept_authoring) ──►  List[ConceptRegistryEntry]   (only for "new" resolutions)
Stage 3 (problem_detection) ──►  List[ProblemCandidate]
Stage 4 (extraction)        ──►  List[ExtractedProblem]
Stage 5 (validation)        ──►  List[ValidatedProblem] + List[RejectedProblem]
Stage 6 (writer)            ──►  Neo4j writes (atomic per concept)
```

```python
class ConceptCandidate(BaseModel):
    proposed_subject_id: str
    proposed_concept_id: str
    scope_summary: str
    source_chunk_ids: list[str]
    confidence: float

class ConceptResolution(BaseModel):
    kind: Literal["new", "matched_existing"]
    candidate: ConceptCandidate
    matched_concept_id: str | None
    matched_subject_id: str | None
    resolution_method: Literal["slug", "embedding", "llm_judge"] | None
    similarity_score: float | None

class ConceptRegistryEntry(BaseModel):
    subject_id: str
    concept_id: str
    canonical_symbols: CanonicalSymbols
    normalization_map: dict[str, str]
    parser_prompt_template: str
    solver_hints: SolverHints
    forbidden_named_laws: ForbiddenNamedLaws

class ProblemCandidate(BaseModel):
    source_document_id: str
    source_chunk_id: str
    source_page: int
    raw_text: str
    detected_kind: Literal["worked_example", "exercise"]
    confidence: float

class ExtractedProblem(BaseModel):
    source_document_id: str
    source_chunk_id: str
    source_page: int
    problem_text: str
    given_values: dict[str, float]
    target_unknown: str
    reference_solution: KGGraph
    concept_id: str
    subject_id: str
    difficulty: Literal["intro", "standard", "hard"]

class ValidatedProblem(ExtractedProblem):
    problem_id: str  # sha256(source_document_id + source_chunk_id)[:16]

class RejectedProblem(BaseModel):
    extracted: ExtractedProblem
    gate_failed: str
    gate_diagnostic: str
```

### Dedup resolver (Stage 1.5)

Order of operations per `ConceptCandidate`:
1. **Slug match.** Exact match on `proposed_concept_id` against Neo4j's existing `(:Concept)` nodes. Match → done.
2. **Embedding similarity.** Embed `scope_summary` (existing embedding model from the indexing layer). Query Neo4j's `concept_scope_embedding_idx` vector index for top-1 match. If cosine ≥ 0.85, candidate maps to that concept.
3. **LLM-judge tiebreaker.** For candidates with similarity in `[0.75, 0.85)`, one short LLM call: *"Concept A: {summary}. Concept B: {summary}. Are these the same teaching concept? yes/no."* Single-token response.
4. **No match** → emit `{kind: "new"}`; proceed to authoring.

Cost: 1 embedding per candidate + a fraction triggers the LLM tiebreaker.

### Idempotency

- **Re-ingesting the same PDF**: Stage 6 writer checks `MATCH (p:Problem {problem_id})` before insert. If exists → no-op. Already-stored problems are preserved exactly.
- **Different PDFs proposing the same concept**: dedup matches; new PDFs can add problems to the existing concept but cannot rewrite its policy files. First-writer-wins is enforced by the writer refusing to overwrite a `(:Concept)` node's policy children if `policy_frozen = true`.
- **Hand-authored concepts**: the migrated Bernoulli concept participates in dedup like any other and is policy-frozen at migration time.

## 5. Neo4j Storage Shape

### Namespace marker labels (three coexisting subgraph worlds)

```
:_KGNode         — per-student-attempt KG (existing V3, scoped by attempt_id)
:_ConceptNode    — concept registry (new, global)
:_ProblemNode    — extracted/authored problems + reference-solution subgraphs (new)
                   subgraph identity = reachability from :Problem root
:_IngestEvent    — observability namespace (runs, errors, rejections, dedup decisions)
```

Every query filters by the right marker label up front. Cleanup-by-attempt continues to work unchanged.

### Concept schema

```cypher
(:Concept:_ConceptNode {
   concept_id:             string,
   subject_id:             string,
   scope_summary:          string,
   scope_embedding:        list<float>,
   parser_prompt_template: string,
   created_at:             datetime,
   source_document_id:     string,
   policy_frozen:          bool
})

(:CanonicalSymbol:_ConceptNode { symbol, description, subscript_convention })
(:NormalizationEntry:_ConceptNode { natural_language, canonical_symbol })
(:SolverConstant:_ConceptNode { name, value })
(:ForbiddenTerm:_ConceptNode { term, category })

(:Concept)-[:HAS_SYMBOL]->(:CanonicalSymbol)
(:Concept)-[:HAS_NORMALIZATION]->(:NormalizationEntry)
(:Concept)-[:HAS_CONSTANT]->(:SolverConstant)
(:Concept)-[:FORBIDS]->(:ForbiddenTerm)
(:Concept)-[:DEPENDS_ON]->(:Concept)   // optional concept_dag, future-proof
```

### Problem schema

```cypher
(:Problem:_ProblemNode {
   problem_id:           string,
   subject_id:           string,
   concept_id:           string,
   difficulty:           string,
   problem_text:         string,
   given_values:         map<string, float>,
   target_unknown:       string,
   source_document_id:   string,
   source_page:          int,
   source_chunk_id:      string,
   extracted_at:         datetime
})

(:Problem)-[:HAS_REFERENCE_NODE]->(:Equation:_ProblemNode {...})
(:Problem)-[:HAS_REFERENCE_NODE]->(:Condition:_ProblemNode {...})
(:Problem)-[:HAS_REFERENCE_NODE]->(:Simplification:_ProblemNode {...})
(:Problem)-[:HAS_REFERENCE_NODE]->(:Definition:_ProblemNode {...})
(:Problem)-[:HAS_REFERENCE_NODE]->(:VariableMapping:_ProblemNode {...})
(:Problem)-[:HAS_REFERENCE_NODE]->(:ProcedureStep:_ProblemNode {...})
```

Reference-solution nodes use the **same** node labels as student-KG nodes (`:Equation`, `:Condition`, etc.), differentiated only by `:_ProblemNode` vs `:_KGNode`. Identity within a reference subgraph is by reachability from the `:Problem` root via `HAS_REFERENCE_NODE`, not by a scoping property on each node. Edges between reference-solution nodes use the same edge types (`PRECEDES`, `USES`, `DEPENDS_ON`, `SCOPES`) as student-KG edges.

### Cluster alias

```cypher
(:ClusterAlias { cluster_id })-[:RESOLVES_TO]->(:Concept)
```

Created automatically by stage 6 when a new concept is created. Seeded for `cluster_id: "fluid_mechanics"` → `bernoulli_principle` during migration.

### Observability namespace

```cypher
(:IngestRun:_IngestEvent {
   document_id, started_at, finished_at, status,
   concepts_discovered, concepts_created, concepts_merged,
   problems_detected, problems_extracted, problems_rejected, problems_accepted,
   errors_logged, llm_call_count, llm_token_count, estimated_cost_usd
})

(:RejectedProblem:_IngestEvent {
   source_document_id, source_page, source_chunk_id,
   gate_failed, gate_diagnostic,
   extracted_payload,  // serialized ExtractedProblem JSON
   rejected_at
})

(:IngestError:_IngestEvent {
   document_id, stage, error_class, error_message, stack_trace,
   context, occurred_at, retried_count
})

(:DedupDecision:_IngestEvent {
   candidate_concept_id, resolution, matched_concept_id,
   method, embedding_similarity, llm_judge_confidence, occurred_at
})
```

### Constraints + indexes (additions to `apollo/persistence/neo4j_schema.cypher`)

```cypher
CREATE CONSTRAINT concept_id_unique IF NOT EXISTS
  FOR (c:Concept) REQUIRE (c.subject_id, c.concept_id) IS UNIQUE;

CREATE CONSTRAINT problem_id_unique IF NOT EXISTS
  FOR (p:Problem) REQUIRE p.problem_id IS UNIQUE;

CREATE CONSTRAINT cluster_alias_unique IF NOT EXISTS
  FOR (a:ClusterAlias) REQUIRE a.cluster_id IS UNIQUE;

CREATE INDEX problem_concept_difficulty IF NOT EXISTS
  FOR (p:Problem) ON (p.subject_id, p.concept_id, p.difficulty);

CREATE INDEX conceptnode_scope IF NOT EXISTS
  FOR (n:_ConceptNode) ON (n.concept_id);

CREATE INDEX problemnode_root IF NOT EXISTS
  FOR (p:Problem) ON (p.problem_id);

CREATE VECTOR INDEX concept_scope_embedding_idx IF NOT EXISTS
  FOR (c:Concept) ON c.scope_embedding
  OPTIONS { indexConfig: { `vector.dimensions`: 1536, `vector.similarity_function`: 'cosine' }};
```

## 6. Validation Gates (Stage 5)

Eight gates, run in order. Short-circuits on first failure. Anything that fails any gate is dropped and logged to `:RejectedProblem`. With no human in the loop, the validator is strict-reject by default.

| # | Gate | Checks | Failure mode |
|---|---|---|---|
| 1 | Schema | Pydantic validation of the `ExtractedProblem` payload, every node, every edge | Missing fields, wrong types, edge types outside `EDGE_ALLOWED_PAIRS` |
| 2 | Reference closure | Every `depends_on` resolves; every edge source/target resolves | Dangling refs |
| 3 | DAG | Dependency graph is acyclic; every node reachable from at least one root | Cycles or orphan islands |
| 4 | Symbol consistency | Every symbol in every `Equation.symbolic`, `Equation.variables[]`, `given_values` keys, and `target_unknown` appears in the concept's `canonical_symbols` (or has been normalized first via the concept's `normalization_map`) | Foreign symbols → drop |
| 5 | Procedure coherence | `:ProcedureStep` nodes form a single `:PRECEDES` chain; every step's `uses_equations` references resolve; terminal step's `purpose` mentions or computes `target_unknown` | Broken chain or dangling equation references |
| 6 | Sympy parse | `sympy.sympify(symbolic)` succeeds for every `:Equation` node | Typos, unbalanced parens, undefined operators |
| 7 | Equation-system closure (Path 1) | Every symbol appearing in equations is either in `given_values`, equals `target_unknown`, or is claimed-cancelled by a `:Simplification` node's `transformation` text | Unsolvable system |
| 8 | Duplicate detection | `sha256(normalize(problem_text) + canonical(given_values) + target_unknown)` doesn't exist for this concept | Near-clone of existing problem |

**Gate 7 explicit limitation:** The existing solver (`apollo/solver/forward_chain.py`) only consumes `equation` entries and does not apply `Condition` or `Simplification` nodes. A full end-to-end solvability proof (Path 2) would require either preprocessing the reference_solution to apply simplifications before handing to the solver, or building a symbolic-derivation walker over the procedure_steps. Both are deliberately out of scope for v1. Gate 7 as specified is a *closure* check on paper, not an end-to-end solve. The trade is honest: some malformed reference_solutions whose equations look coherent but don't actually produce the claimed answer will slip through. Acceptable for v1; upgrade to Path 2 is a measurable follow-on once the false-positive rate is known.

**Validator deliberate non-checks:**
- Symbol *meaning* beyond consistency (e.g. "P actually means pressure here, not power"). Irreducible failure mode of fully-automatic extraction.
- Pedagogical quality (too trivial, too hard, requires prerequisites).
- Plagiarism / copyright (handled upstream at the indexing layer).

## 7. Selector + Migration

### New selector (`apollo/overseer/problem_selector.py`, rewritten)

```python
async def select_problem(
    *, cluster_id: str, difficulty: str,
    attempted_ids: Sequence[str], neo: Neo4jClient,
) -> Problem:
    subject_id, concept_id = await cluster_to_concept(cluster_id, neo)
    query = """
    MATCH (p:Problem {subject_id: $s, concept_id: $c, difficulty: $d})
    WHERE NOT p.problem_id IN $attempted
    RETURN p ORDER BY p.problem_id ASC LIMIT 1
    """
    async with neo.session() as session:
        result = await session.run(query, ...)
        record = await result.single()
        if record is None:
            raise PoolExhaustedError(concept_cluster_id=cluster_id, difficulty=difficulty)
        return await _load_problem_from_neo4j(record["p"], neo)
```

- `_load_problem_from_neo4j` fetches the `:Problem` node, walks `HAS_REFERENCE_NODE` to pull the reference_solution subgraph, reconstructs the same `Problem` Pydantic object Apollo already uses.
- Selector becomes async because the Neo4j client is async. Handlers already pass `neo: Neo4jClient = Depends(get_neo4j_client)`.

### `cluster_to_concept` becomes a Neo4j query

```cypher
MATCH (a:ClusterAlias {cluster_id: $c})-[:RESOLVES_TO]->(concept:Concept)
RETURN concept.subject_id, concept.concept_id
```

The hardcoded `_CLUSTER_TO_CONCEPT` Python dict is removed. Seed alias for `"fluid_mechanics"` → Bernoulli concept is created during migration.

### `list_problems_for_cluster` (used by `done.py`, `lifecycle.py`)

```cypher
MATCH (p:Problem {subject_id: $s, concept_id: $c})
RETURN p.problem_id AS id, p.difficulty AS difficulty
```

Lightweight count/list query — never pulls reference_solution subgraphs.

### Determinism

Hand-authored problems keep their human-readable slug `problem_id` (e.g. `bernoulli_horizontal_pipe_find_p2`); they sort earlier than the hex-hashed IDs of ingest-extracted problems. Students see hand-authored content first.

### Migration of the hand-authored Bernoulli bank

Script: `apollo/textbook_ingest/scripts/migrate_filesystem_concept.py`.

- Walks `apollo/subjects/fluid_mechanics/concepts/bernoulli_principle/`.
- Reads the five policy JSON files → writes equivalent Neo4j nodes via the writer.
- Reads each `problem_*.json` → constructs an `ExtractedProblem`-equivalent payload → runs the validator → writes to Neo4j.
- All hand-authored problems must pass all 8 gates. If they don't, the migration aborts with a hard error (this means the validator is too strict, not that the content is wrong).
- Runs once, idempotent. Re-running skips already-migrated nodes.
- Run before any teacher ingest after first ApolloV3 + Neo4j deployment. The on-disk folder is treated as source of truth until migration succeeds; after that, Neo4j is authoritative.

## 8. Error Handling + Observability

### Failure class table

| Class | Where | Disposition |
|---|---|---|
| LLM timeout / 5xx / rate limit | Stages 1–4 | Retry with backoff, max N attempts per call. Then fail the candidate; log `:IngestError`. |
| LLM output malformed (invalid JSON / schema) | Stages 1–4 | One reprompt with the schema error as feedback. Second failure → drop candidate; log payload. |
| Validator gate failure | Stage 5 | Expected. Drop candidate; log `:RejectedProblem`. |
| Neo4j write failure | Stage 6 | Per-concept transaction. Rollback this concept (registry + all its problems together). Continue with next concept. Log `:IngestError`. |
| Uncaught exception | Any stage | Per-document supervisor catches. Log `:IngestError` with stack. Mark document ingest as `failed`. Other documents in queue continue. |
| Already-ingested document | Pre-stage | Idempotency check: if any `problem_id` for `(source_document_id, source_chunk_id)` already exists, skip. |

### Per-ingest summary

One `:IngestRun` node per PDF run, populated by stage 6 at the end of the pipeline. Lets us answer "is the pipeline trending well" by aggregating recent runs.

### Structured stdout logs

Every stage emits JSON log lines at start, finish, per-candidate. Lands in Heroku log drain (existing deployment). Per-line shape:

```json
{"ts": "...", "stage": "extraction", "document_id": "...",
 "candidate_idx": 12, "result": "rejected|accepted|errored",
 "gate_failed": "symbol", "duration_ms": 432, "llm_calls": 1}
```

Neo4j summaries are durable observability; logs are for live debugging.

### Things we'd want to alert on later (not built in v1)

- Per-document rejection rate > 70% — extractor producing junk for this book.
- Stage-3 problem-detection count = 0 on a non-trivial PDF — detector broken or document genuinely empty.
- Last-hour `:IngestError` count > threshold — infra failure.

For v1 these are ad-hoc Cypher queries against `:IngestRun` and `:IngestError`.

### Retry policy

- LLM calls within a stage: yes, with backoff.
- Whole-document re-ingest after a crash: no. Manual replay so we see the stack before redoing work.
- Rejected problems if validator logic changes: no. Stays rejected until document is explicitly re-ingested.
- Re-ingesting a previously-successful document: idempotent (writer skips existing `problem_id`s).

### Cost tracking

`:IngestRun.llm_call_count`, `.llm_token_count`, `.estimated_cost_usd` are aggregates per document. Per-stage rollups derivable from the log stream.

## 9. Testing Strategy

The V3 branch turned off the existing test suite for the rewrite. This project is the right moment to put a real test layer back in for the parts it touches. Three tiers, scaled by cost.

### Tier 1 — Every PR, free, runs in seconds

- **Per-stage unit tests** (`apollo/textbook_ingest/tests/test_<stage>.py`). LLM calls stubbed. Input/output Pydantic-type assertions.
- **Validator gate tests** (`apollo/textbook_ingest/tests/test_validator.py`) — the most important test file in this project:
  - **Positive fixtures**: all five hand-authored Bernoulli problems must pass all 8 gates.
  - **Adversarial fixtures**: a corpus of intentionally-broken payloads, each labeled with the gate it should fail.
    Examples: `equation_with_unknown_symbol.json` → gate 4, `cyclic_depends_on.json` → gate 3, `dangling_edge_target.json` → gate 2, `malformed_sympy.json` → gate 6, `procedure_chain_with_gap.json` → gate 5, `duplicate_of_existing_problem.json` → gate 8.
- **Dedup resolver tests** (`apollo/textbook_ingest/tests/test_concept_resolver.py`). Embedding model mocked with deterministic vectors. Cover slug-match short-circuit, embedding match, LLM-judge tiebreaker.
- **Selector tests** (`apollo/overseer/tests/test_problem_selector.py`). Reuse the existing V3 skip-marked file. Rewrite for Neo4j. Use the existing `apollo/conftest.py` Neo4j client fixture (skip when creds missing) plus a Testcontainers fallback for CI.

### Tier 2 — Nightly + on-demand, ~$5/run

End-to-end smoke against a **synthetic mini-textbook** (`apollo/textbook_ingest/tests/fixtures/synthetic_textbook.md`): ~10 pages, one chapter on Bernoulli's principle with two worked examples and three exercises, hand-authored to be unambiguous. The test:

1. Runs the existing indexing layer against the synthetic textbook (or feeds chunks directly).
2. Runs the textbook-ingest pipeline end-to-end with real LLM calls.
3. Asserts: ≥1 concept created, ≥2 `:Problem` nodes from the worked examples, validator reject count below threshold.
4. Pulls one extracted problem, runs the existing Apollo grader against a hand-crafted "good student attempt" KG. Grade must come out passing.

CI cron + on-demand before merging anything under `apollo/textbook_ingest/`.

### Tier 3 — Release gate, ~$50-100/run

Full ingest against a **real fluid mechanics textbook** (the first deploy target). Run manually when:
- Validator gate logic changes.
- A stage's LLM prompt changes.
- Before a production deploy.

Statistical assertions, not exact:
- Concept count within expected band.
- Per-concept problem count within expected band.
- Validator reject rate below threshold (initially 40%; tune from data).
- A hand-curated list of "known good" worked examples (pages identified by you/FellerCodes) must appear in the bank.

Run output: `:IngestRun` summary + a generated report card. Commit the report card with each release as the audit trail.

### Migration regression test

`test_filesystem_migration.py`. Runs the Bernoulli on-disk → Neo4j migration into a fresh Neo4j fixture, then asserts:

- All 5 hand-authored problems are queryable by the selector.
- The Bernoulli concept's `canonical_symbols` match `canonical_symbols.json` exactly.
- For every problem, the loaded `reference_solution` Pydantic object equals the on-disk JSON.
- The existing Apollo grader produces the same verdict against a fixed student-KG input as it did against the on-disk version.

This test stays in the suite forever as a guard against accidentally breaking the migrator on future re-runs.

### V3 test re-enablement

Out of scope here. Tracked separately as `claude_v3_checklist.md` work. New tests in this project follow the same conventions (same `conftest.py`, same `NEGATIVE attempt_id` fixture convention, same async pytest pattern) so they conform when the V3 re-enablement happens.

### Deliberate test non-goals

- **Quality of LLM output content.** Whether the parser_prompt_template the LLM authored is *pedagogically good* — not testable without a human.
- **Real-world recall.** "Did we find all the problems in this textbook?" — v1 is manual spot-check.

## 10. Known Limitations & Follow-Ons

| Item | Reason | Follow-on path |
|---|---|---|
| Gate 7 is closure-only (Path 1) | Existing solver doesn't apply simplifications/conditions; Path 2 (preprocessor) is meaningful new code | Build simplification preprocessor in validator; promote gate 7 to end-to-end solve |
| `apollo/solver/sympy_exec.py:35` has a hardcoded `_CANONICAL_SYMBOLS` list (V2 Bernoulli-only) | Predates V3 concept registry | Make solver concept-aware (read from `ConceptDefinition.canonical_symbols`); separate cleanup, not blocking this spec |
| Symbol-meaning errors slip through (e.g. "P used for power, not pressure") | Irreducible failure of fully-automatic extraction without human judgment | Accept as dominant residual error rate; add human review queue if rates demand |
| Rejected problems don't auto-retry on validator changes | First-writer-wins simplicity | Manual document re-ingest |
| Concept policy is immutable after first write | Mutation would break previously-extracted problems | If a concept needs updates, future tooling to migrate problems to a new concept |
| No alerting layer | v1 scope | Ad-hoc Cypher queries against `:IngestRun` + `:IngestError`; build alerts later |
| No labeled-recall benchmark | v1 scope | Curate a labeled recall corpus; add to Tier 3 |
| 13 V3 test modules remain `pytest.skip`'d | Pre-existing condition; `claude_v3_checklist.md` work | Separate project |

## 11. Out of Scope

- Re-enabling the 13 skipped V3 tests.
- Re-architecting `apollo/solver/` to be concept-aware (beyond the existing concept-driven plumbing; the hardcoded `_CANONICAL_SYMBOLS` cleanup is its own ticket).
- Human-review queue for extracted problems.
- LLM-generated novel problems (the second framing discussed during brainstorming — distinct from extracting existing textbook content).
- Cross-concept problems (problems that span more than one concept_id).
- Pedagogical quality scoring.
- Plagiarism / copyright detection (handled at the indexing layer, not here).
- Per-tenant concept registries (multi-tenant isolation of concepts across schools).
- Frontend changes to Apollo's student or teacher UIs.

## 12. Open Items for Implementation Plan

These are decided in this spec but need concrete implementation choices:

- LLM model selection per stage (concept discovery, concept authoring, problem detection, problem extraction, classification, dedup judge). Default to the existing `MAIN_MODEL` env var unless cost or quality data suggests otherwise.
- Embedding model for `scope_embedding`. Default to whatever the existing `indexing/document_embedder.py` uses, for consistency.
- Concrete confidence thresholds (dedup embedding cutoff at 0.85, llm-judge band `[0.75, 0.85)`, problem-detector accept threshold, classification accept threshold). Initial values land in `config/settings.py`; tune from data.
- Per-stage prompt templates. Drafted alongside implementation.
- Testcontainers vs. live Aura for tier-1 selector tests in CI. Resolve when the writing-plans phase scopes CI work.
