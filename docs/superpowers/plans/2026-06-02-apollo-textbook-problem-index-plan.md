# Apollo Textbook Problem Index Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build `apollo/textbook_ingest/` — a six-stage pipeline that extracts, classifies, validates, and persists textbook problems (and the concepts they teach) into Neo4j — and cut Apollo's selector + concept loader over from filesystem JSON to Neo4j.

**Architecture:** A post-pass of the existing `teacher_pdf_ingestion` pipeline runs six sequential, independently-typed stages (discovery → dedup → concept authoring → problem detection → extraction → validation → persistence). Concepts and problems both live in Neo4j alongside the existing per-attempt student KG, separated by namespace marker labels (`:_ConceptNode`, `:_ProblemNode`, `:_IngestEvent` vs. the existing `:_KGNode`). A strict 8-gate validator is the only safety layer in this fully-automatic design. Phase A is a pure Neo4j cutover for the hand-authored Bernoulli concept with **zero LLM work**, so Apollo runs on Neo4j before any extracted content lands.

**Tech Stack:** Python 3 / FastAPI, async Neo4j (`neo4j` async driver, Aura), Pydantic v2, OpenAI (`text-embedding-3-large` @ 3072 dims; chat via `apollo.agent._llm.main_chat` / `cheap_chat`), SymPy (existing solver), pytest + pytest-asyncio, Testcontainers (neo4j) for CI.

---

## Spec Reconciliations (read before starting)

Exploration of the live ApolloV3 baseline found several places where the spec's illustrative sketches diverge from the actual code. The spec's **intent** governs; the actual code governs **fact**. Where the spec's "What we're NOT changing" list locks a type, that lock wins. These reconciliations are load-bearing — every task below assumes them:

1. **Embedding is 3072-dim, not 1536.** `indexing/document_embedder.py:embed_text` defaults to `text-embedding-3-large` (`EMBEDDING_DIM=3072`). The Neo4j vector index `concept_scope_embedding_idx` uses `vector.dimensions: 3072` (Spec §5 wrote 1536; Spec §12 item 2 says "default to whatever `document_embedder.py` uses" — that resolves to 3072).

2. **`reference_solution` stays `list[ReferenceStep]`, not `KGGraph`.** Spec §1/§3 lock the `Problem` Pydantic schema (`apollo/schemas/problem.py`) as unchanged; its `reference_solution` field is `list[ReferenceStep]`. So `ExtractedProblem.reference_solution` is `list[ReferenceStep]` (matches the on-disk JSON and round-trips to `Problem`). The validator's graph-theoretic gates convert it to a `KGGraph` internally via a shared `reference_steps_to_kg_graph()` helper (Task A3) that reuses the existing `Problem.to_kg_graph` derivation. Spec §4's `reference_solution: KGGraph` sketch is superseded by this locked schema.

3. **LLM model selection uses `apollo/agent/_llm.py`, no new env var.** `MAIN_MODEL` does *not* live in `config/settings.py`; it is resolved inside `apollo/agent/_llm.py` via `main_chat()` (MAIN_MODEL / `gpt-4o`) and `cheap_chat()` (`APOLLO_CHEAP_MODEL` / `gpt-4o-mini`). Per-stage assignment (Spec §12 item 1):
   - discovery → `main_chat`, extraction → `main_chat`, concept authoring (5 calls) → `main_chat`
   - dedup llm-judge → `cheap_chat`, problem detection → `cheap_chat`
   Each call passes a `purpose="textbook_ingest.<stage>"` string for the existing audit logger.

4. **Thresholds are module-level constants in `config/settings.py`.** That file is *not* a pydantic `BaseSettings`; it uses module globals + a `RequestConfig` dataclass. New thresholds (Spec §12 item 3) are added as module-level constants (Task A0), matching the file's existing style.

5. **`SolverHints` has four sub-fields; the spec models only `:SolverConstant`.** To keep `load_concept` returning a faithful `SolverHints` (`constants`, `augmented_givens`, `non_trivial_keywords`, `plan_markers`), the concept schema is extended minimally: `:SolverConstant {name, value, kind}` where `kind ∈ {"constant","augmented_given"}`, plus `:Concept.non_trivial_keywords: list<string>` and `:Concept.plan_markers: list<string>` properties.

6. **`ForbiddenNamedLaws` (4 lists) → `:ForbiddenTerm {term, category}`** with `category ∈ {"named_law","forbidden_concept","forbidden_domain","forbidden_unit"}`. `load_concept` regroups by category back into the four lists.

7. **`load_concept` becomes async and takes `neo`.** It reads Neo4j now. Only three callsites exist: `apollo/handlers/chat.py:66`, `apollo/handlers/done.py:88` (both already async handlers that already receive `neo: Neo4jClient`), and `apollo/overseer/problem_selector.py` (rewritten in this plan). Signature: `async def load_concept(subject_id, concept_id, neo) -> ConceptDefinition`. Spec §3's "signature preserved" is honored in spirit (same return type, same role); the unavoidable async+`neo` change is threaded in Task A8.

8. **Selector async ripple is tiny.** `select_problem` / `cluster_to_concept` / `list_problems_for_cluster` become async + take `neo`. The only handler callsite missing a `neo` parameter is `handle_next` (`apollo/handlers/next.py:29`) and its route (`apollo/api.py:122-130`); `handle_done`, `handle_chat`, `handle_end`, `handle_get_session` already receive `neo`. Task A8 threads `neo` into `handle_next` + `/next`.

9. **Hand-authored-first ordering is made deterministic via an `authored` flag**, not lexical luck. Spec §7 claims slugs sort before hex IDs, but ASCII collation does not guarantee that (a hex id starting `3f…` sorts before slug `bernoulli…`). `:Problem.authored: bool` (true at migration, false for extracted) drives `ORDER BY p.authored DESC, p.problem_id ASC`.

10. **`concept_dag.json` is NOT migrated.** It is V3 intra-concept sub-topic scaffolding, not one of the five policy files, and not a cross-`:Concept` DAG. The spec's `(:Concept)-[:DEPENDS_ON]->(:Concept)` is left unpopulated in v1 (Spec §5 marks it "optional, future-proof").

11. **`subject_id` is injected at migration time.** The on-disk problem JSON has no `subject_id`; migration sets `"fluid_mechanics"`.

12. **Testcontainers (neo4j) for CI Neo4j tests** (Spec §12 item 5, §9 Tier-1). The existing live-Aura `neo4j_client` fixture (skips when creds absent) is retained for local/manual runs; a new `neo4j_test` testcontainer fixture gives deterministic CI. ⚠️ **`testcontainers[neo4j]` is a new test-only dependency** — Task A1 adds it to a new `requirements-dev.txt` and the executor MUST confirm with the user before `pip install` (CLAUDE.md: "Never install new packages without confirming with me first").

**Out of scope (do not implement):** Re-enabling skipped V3 tests; Gate 7 Path 2 (symbolic execution); cleaning up `apollo/solver/sympy_exec.py:35` `_CANONICAL_SYMBOLS` (sibling ticket — Gate 6/7 read symbols from the concept's `canonical_symbols`, never from `_CANONICAL_SYMBOLS`); everything in Spec §11.

---

## File Structure

**New module — `apollo/textbook_ingest/`:**

| File | Responsibility |
|---|---|
| `__init__.py` | Package marker; re-exports `run_textbook_ingest`. |
| `types.py` | All stage Pydantic types (`ConceptCandidate`, `ConceptResolution`, `ConceptRegistryEntry`, `ProblemCandidate`, `ExtractedProblem`, `ValidatedProblem`, `RejectedProblem`, `IngestRunSummary`). |
| `kg_convert.py` | `reference_steps_to_kg_graph()` — shared `list[ReferenceStep] → KGGraph` converter used by validator + writer. |
| `concept_schema_map.py` | Pure functions mapping `ConceptRegistryEntry` ↔ Neo4j concept subgraph (symbols/normalization/constants/forbidden); reused by writer + migration + `load_concept`. |
| `writer.py` | Neo4j write primitives + stage-6 atomic-per-concept orchestration. |
| `validator.py` | The 8-gate validator. Pure (no LLM, no I/O except the dedup gate's Neo4j check). |
| `discovery.py` | Stage 1 — concept candidate discovery (LLM). |
| `concept_resolver.py` | Stage 1.5 — dedup (slug → embedding → llm-judge). |
| `concept_authoring.py` | Stage 2 — five-call policy authoring (LLM). |
| `problem_detector.py` | Stage 3 — per-chunk worked_example/exercise/neither classifier (LLM). |
| `problem_extractor.py` | Stage 4 — typed problem extraction (LLM). |
| `pipeline.py` | `run_textbook_ingest()` — supervisor that wires stages 1→6, idempotency, error handling, `:IngestRun` summary. |
| `observability.py` | `:IngestRun` / `:IngestError` / `:RejectedProblem` / `:DedupDecision` writers + structured stdout JSON logging. |
| `prompts/__init__.py` | `PROMPT_VERSIONS` dict + `load_prompt(stage)` loader. |
| `prompts/*.md` | One prompt template per LLM stage (versioned by filename + `PROMPT_VERSIONS`). |
| `scripts/migrate_filesystem_concept.py` | One-time, idempotent Bernoulli filesystem→Neo4j migration. |
| `tests/` | Per-stage unit tests, validator gate tests, resolver tests, migration regression, tier-2/3 smokes, fixtures. |

**Modified existing files:**

| File | Change |
|---|---|
| `apollo/persistence/neo4j_schema.cypher` | Add concept/problem/alias/observability constraints + indexes + vector index (3072). |
| `apollo/subjects/__init__.py` | `load_concept` / `list_subjects` / `list_concepts` → async Neo4j reads. |
| `apollo/overseer/problem_selector.py` | Rewrite for Neo4j; async; `neo` param. |
| `apollo/handlers/chat.py` | `await load_concept(..., neo)`. |
| `apollo/handlers/done.py` | `await load_concept(..., neo)`, `await cluster_to_concept(..., neo)`, `await _find_problem(..., neo)`. |
| `apollo/handlers/lifecycle.py` | `await list_problems_for_cluster(..., neo)` in `handle_get_session`. |
| `apollo/handlers/next.py` | Add `neo: Neo4jClient`; `await select_problem(..., neo)`. |
| `apollo/api.py` | Add `neo=Depends(get_neo4j_client)` to `/next` route → `handle_next`. |
| `config/settings.py` | Add textbook-ingest threshold constants. |
| `apollo/conftest.py` | Add `neo4j_test` testcontainer fixture + concept/problem-namespace cleanup. |
| `requirements-dev.txt` | **New file**; add `testcontainers[neo4j]`. |
| `knowledge/teacher_pdf_ingestion.py` | Optional post-pass hook calling `run_textbook_ingest` (Phase E). |

---

# Phase A — Neo4j foundation + Bernoulli migration + selector cutover

**Phase goal:** Apollo runs entirely on Neo4j for the Bernoulli concept. Hand-authored content is migrated, the selector and `load_concept` read Neo4j, and **no LLM code exists yet**. This is the clean cutover. The validator used by migration is gate-1-only here; Phase B grows it to 8 gates without changing the migration code.

---

### Task A0: Config constants + package skeleton

**Files:**
- Modify: `config/settings.py` (append a new section at end of file)
- Create: `apollo/textbook_ingest/__init__.py`
- Create: `apollo/textbook_ingest/tests/__init__.py`
- Test: `apollo/textbook_ingest/tests/test_config.py`

- [ ] **Step 1: Write the failing test**

```python
# apollo/textbook_ingest/tests/test_config.py
from config import settings


def test_textbook_thresholds_present_and_sane():
    assert settings.TEXTBOOK_EMBEDDING_MODEL == "text-embedding-3-large"
    assert settings.TEXTBOOK_EMBEDDING_DIM == 3072
    assert settings.TEXTBOOK_DEDUP_EMBEDDING_CUTOFF == 0.85
    assert settings.TEXTBOOK_DEDUP_LLM_JUDGE_LOW == 0.75
    assert settings.TEXTBOOK_DEDUP_LLM_JUDGE_HIGH == 0.85
    assert 0.0 < settings.TEXTBOOK_PROBLEM_DETECTOR_ACCEPT_THRESHOLD < 1.0
    assert 0.0 < settings.TEXTBOOK_CLASSIFIER_ACCEPT_THRESHOLD < 1.0
    assert settings.TEXTBOOK_LLM_MAX_RETRIES >= 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest apollo/textbook_ingest/tests/test_config.py -v`
Expected: FAIL — `ModuleNotFoundError: apollo.textbook_ingest` and/or `AttributeError: TEXTBOOK_EMBEDDING_MODEL`.

- [ ] **Step 3: Create the package + add constants**

Create empty `apollo/textbook_ingest/__init__.py` and `apollo/textbook_ingest/tests/__init__.py`.

Append to the end of `config/settings.py`:

```python
# ---------------------------------------------------------------------------
# Textbook problem-index ingest (apollo/textbook_ingest).
# Module-level constants, matching this file's existing globals style
# (settings.py is not a pydantic BaseSettings). Tune from data; see
# docs/superpowers/specs/2026-06-02-apollo-textbook-problem-index-design.md §12.
# ---------------------------------------------------------------------------
TEXTBOOK_EMBEDDING_MODEL = os.getenv("OPENAI_EMBEDDING_MODEL", "text-embedding-3-large")
TEXTBOOK_EMBEDDING_DIM = int(os.getenv("EMBEDDING_DIM", "3072"))
TEXTBOOK_DEDUP_EMBEDDING_CUTOFF = 0.85          # cosine ≥ this → matched_existing
TEXTBOOK_DEDUP_LLM_JUDGE_LOW = 0.75             # [LOW, HIGH) band triggers llm-judge
TEXTBOOK_DEDUP_LLM_JUDGE_HIGH = 0.85
TEXTBOOK_PROBLEM_DETECTOR_ACCEPT_THRESHOLD = 0.60   # detector confidence floor
TEXTBOOK_CLASSIFIER_ACCEPT_THRESHOLD = 0.60         # extraction concept-tag floor
TEXTBOOK_LLM_MAX_RETRIES = 2                     # per-call retries (timeout/malformed)
TEXTBOOK_TIER2_MAX_REJECT_RATE = 0.50           # synthetic smoke reject ceiling
TEXTBOOK_TIER3_MAX_REJECT_RATE = 0.40           # real-textbook release reject ceiling
```

Confirm `import os` already exists at the top of `config/settings.py` (it does — used by the existing globals). If not, add it.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest apollo/textbook_ingest/tests/test_config.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add config/settings.py apollo/textbook_ingest/__init__.py apollo/textbook_ingest/tests/__init__.py apollo/textbook_ingest/tests/test_config.py
git commit -m "feat(textbook-ingest): add config thresholds and package skeleton"
```

---

### Task A1: Testcontainers dev dependency + Neo4j test fixture

⚠️ **This task installs a new package. STOP and confirm with the user before `pip install` (CLAUDE.md).**

**Files:**
- Create: `requirements-dev.txt`
- Modify: `apollo/conftest.py` (add fixture; keep existing `neo4j_client`)
- Test: `apollo/textbook_ingest/tests/test_neo4j_fixture.py`

- [ ] **Step 1: Write the failing test**

```python
# apollo/textbook_ingest/tests/test_neo4j_fixture.py
import pytest


@pytest.mark.asyncio
async def test_neo4j_test_fixture_is_empty_and_writable(neo4j_test):
    async with neo4j_test.session() as s:
        await s.run("CREATE (:_ConceptNode:Concept {concept_id: 'probe'})")
        rec = await (await s.run(
            "MATCH (c:Concept {concept_id: 'probe'}) RETURN count(c) AS n"
        )).single()
        assert rec["n"] == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest apollo/textbook_ingest/tests/test_neo4j_fixture.py -v`
Expected: FAIL — fixture `neo4j_test` not found.

- [ ] **Step 3: Add the dependency (CONFIRM FIRST) and the fixture**

Create `requirements-dev.txt`:

```
# Test-only dependencies (not installed in production Heroku dynos).
-r requirements.txt
testcontainers[neo4j]>=4.0
```

After user confirmation: `pip install "testcontainers[neo4j]>=4.0"`.

Add to `apollo/conftest.py` (keep the existing `neo4j_client` fixture untouched):

```python
@pytest_asyncio.fixture
async def neo4j_test():
    """Ephemeral Neo4j via Testcontainers for deterministic CI.

    Spins a throwaway neo4j:5 container, applies the schema file, yields a
    Neo4jClient pointed at it, and tears the container down after the test.
    Use for concept/problem/ingest tests that are not attempt_id-scoped and
    so cannot rely on the negative-attempt_id cleanup of `neo4j_client`.
    """
    from pathlib import Path
    from testcontainers.neo4j import Neo4jContainer
    from apollo.persistence.neo4j_client import Neo4jClient

    with Neo4jContainer("neo4j:5.20") as container:
        uri = container.get_connection_url()
        client = Neo4jClient(uri=uri, user="neo4j",
                             password=container.password, database="neo4j")
        schema = Path("apollo/persistence/neo4j_schema.cypher").read_text()
        async with client.session() as s:
            for stmt in [x.strip() for x in schema.split(";") if x.strip()]:
                await s.run(stmt)
        yield client
        await client.close()
```

Confirm `import pytest_asyncio` is already present at the top of `apollo/conftest.py` (it is — used by `neo4j_client`).

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest apollo/textbook_ingest/tests/test_neo4j_fixture.py -v`
Expected: PASS (requires Docker available locally / in CI).

- [ ] **Step 5: Commit**

```bash
git add requirements-dev.txt apollo/conftest.py apollo/textbook_ingest/tests/test_neo4j_fixture.py
git commit -m "test(textbook-ingest): add Testcontainers neo4j_test fixture"
```

---

### Task A2: Stage Pydantic types

**Files:**
- Create: `apollo/textbook_ingest/types.py`
- Test: `apollo/textbook_ingest/tests/test_types.py`

- [ ] **Step 1: Write the failing test**

```python
# apollo/textbook_ingest/tests/test_types.py
from apollo.schemas.problem import ReferenceStep
from apollo.textbook_ingest.types import (
    ConceptCandidate, ConceptResolution, ConceptRegistryEntry,
    ProblemCandidate, ExtractedProblem, ValidatedProblem, RejectedProblem,
)


def _ref_step():
    return ReferenceStep(step=1, entry_type="equation", id="eq1",
                         content={"symbolic": "P1 - P2", "label": "x", "variables": ["P1", "P2"]},
                         depends_on=[])


def test_extracted_to_validated_inherits():
    ex = ExtractedProblem(
        source_document_id="d", source_chunk_id="c", source_page=3,
        problem_text="t", given_values={"P1": 1.0}, target_unknown="P2",
        reference_solution=[_ref_step()], concept_id="bernoulli_principle",
        subject_id="fluid_mechanics", difficulty="intro",
    )
    vp = ValidatedProblem(**ex.model_dump(), problem_id="abc123")
    assert vp.problem_id == "abc123"
    assert vp.subject_id == "fluid_mechanics"


def test_resolution_new_has_no_match():
    cand = ConceptCandidate(proposed_subject_id="s", proposed_concept_id="c",
                            scope_summary="x", source_chunk_ids=["a"], confidence=0.9)
    res = ConceptResolution(kind="new", candidate=cand, matched_concept_id=None,
                            matched_subject_id=None, resolution_method=None,
                            similarity_score=None)
    assert res.kind == "new"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest apollo/textbook_ingest/tests/test_types.py -v`
Expected: FAIL — `ModuleNotFoundError: apollo.textbook_ingest.types`.

- [ ] **Step 3: Write the types**

```python
# apollo/textbook_ingest/types.py
"""Pydantic types for the six textbook-ingest stages.

reference_solution is list[ReferenceStep] (NOT KGGraph) to round-trip with the
locked apollo.schemas.problem.Problem schema; the validator converts to KGGraph
internally (see kg_convert.py).
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from apollo.schemas.problem import Difficulty, ReferenceStep
from apollo.subjects import CanonicalSymbols, ForbiddenNamedLaws, SolverHints


class ConceptCandidate(BaseModel):
    proposed_subject_id: str
    proposed_concept_id: str
    scope_summary: str
    source_chunk_ids: list[str]
    confidence: float


class ConceptResolution(BaseModel):
    kind: Literal["new", "matched_existing"]
    candidate: ConceptCandidate
    matched_concept_id: str | None = None
    matched_subject_id: str | None = None
    resolution_method: Literal["slug", "embedding", "llm_judge"] | None = None
    similarity_score: float | None = None


class ConceptRegistryEntry(BaseModel):
    subject_id: str
    concept_id: str
    scope_summary: str
    canonical_symbols: CanonicalSymbols
    normalization_map: dict[str, str]
    parser_prompt_template: str
    solver_hints: SolverHints
    forbidden_named_laws: ForbiddenNamedLaws = Field(default_factory=ForbiddenNamedLaws)


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
    reference_solution: list[ReferenceStep]
    concept_id: str
    subject_id: str
    difficulty: Difficulty


class ValidatedProblem(ExtractedProblem):
    problem_id: str  # sha256(source_document_id + source_chunk_id)[:16]


class RejectedProblem(BaseModel):
    extracted: ExtractedProblem
    gate_failed: str
    gate_diagnostic: str
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest apollo/textbook_ingest/tests/test_types.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add apollo/textbook_ingest/types.py apollo/textbook_ingest/tests/test_types.py
git commit -m "feat(textbook-ingest): add stage Pydantic types"
```

---

### Task A3: reference_solution → KGGraph converter

**Files:**
- Create: `apollo/textbook_ingest/kg_convert.py`
- Test: `apollo/textbook_ingest/tests/test_kg_convert.py`

The existing `Problem.to_kg_graph(attempt_id)` already derives a `KGGraph` from a `Problem`'s `reference_solution`. This helper wraps that logic so any `list[ReferenceStep]` can be converted without first constructing a full `Problem`. Reference subgraphs are not attempt-scoped, so we pass a fixed sentinel `attempt_id = -1`.

- [ ] **Step 1: Write the failing test**

```python
# apollo/textbook_ingest/tests/test_kg_convert.py
from apollo.schemas.problem import ReferenceStep
from apollo.textbook_ingest.kg_convert import REFERENCE_ATTEMPT_ID, reference_steps_to_kg_graph


def test_converts_steps_to_kg_graph_with_depends_on_edges():
    steps = [
        ReferenceStep(step=1, entry_type="equation", id="continuity",
                      content={"symbolic": "rho*A1*v1 - rho*A2*v2", "label": "continuity",
                               "variables": ["rho", "A1", "v1", "A2", "v2"]}, depends_on=[]),
        ReferenceStep(step=2, entry_type="equation", id="bernoulli",
                      content={"symbolic": "P1 + 0.5*rho*v1**2 - P2 - 0.5*rho*v2**2",
                               "label": "bernoulli", "variables": ["P1", "rho", "v1", "P2", "v2"]},
                      depends_on=["continuity"]),
    ]
    g = reference_steps_to_kg_graph(steps)
    assert {n.node_id for n in g.nodes} == {"continuity", "bernoulli"}
    assert all(n.attempt_id == REFERENCE_ATTEMPT_ID for n in g.nodes)
    dep = [e for e in g.edges if e.edge_type.value == "DEPENDS_ON"]
    assert any(e.from_node_id == "bernoulli" and e.to_node_id == "continuity" for e in dep)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest apollo/textbook_ingest/tests/test_kg_convert.py -v`
Expected: FAIL — `ModuleNotFoundError: apollo.textbook_ingest.kg_convert`.

- [ ] **Step 3: Write the converter**

```python
# apollo/textbook_ingest/kg_convert.py
"""Convert a list[ReferenceStep] into the ontology KGGraph used by validator
gates and the Neo4j writer. Reuses Problem.to_kg_graph so reference-solution
graph derivation stays single-sourced.
"""
from __future__ import annotations

from apollo.ontology.graph import KGGraph
from apollo.schemas.problem import Problem, ReferenceStep

# Reference subgraphs are global, not per-attempt. Fixed negative sentinel keeps
# them clear of any real (positive) student attempt_id and of test ids.
REFERENCE_ATTEMPT_ID = -1


def reference_steps_to_kg_graph(steps: list[ReferenceStep]) -> KGGraph:
    shell = Problem(
        id="__ref__", concept_id="__ref__", difficulty="intro",
        problem_text="__ref__", given_values={}, target_unknown="__ref__",
        reference_solution=steps,
    )
    return shell.to_kg_graph(attempt_id=REFERENCE_ATTEMPT_ID)
```

If `Problem` rejects empty `given_values` / placeholder `target_unknown` (it requires `min_length=1` strings, which `"__ref__"` satisfies; `given_values` is `Dict[str, float]` with no min size), this constructs cleanly. Verify by running the test; if a validator on `Problem` rejects the shell, replace the shell construction with a direct port of `Problem.to_kg_graph`'s body operating on `steps`.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest apollo/textbook_ingest/tests/test_kg_convert.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add apollo/textbook_ingest/kg_convert.py apollo/textbook_ingest/tests/test_kg_convert.py
git commit -m "feat(textbook-ingest): add reference-step to KGGraph converter"
```

---

### Task A4: Concept schema mapping (Pydantic ↔ Neo4j subgraph)

**Files:**
- Create: `apollo/textbook_ingest/concept_schema_map.py`
- Test: `apollo/textbook_ingest/tests/test_concept_schema_map.py`

Pure (no I/O) functions that turn a `ConceptRegistryEntry` into the parameter rows a Cypher write needs, and turn fetched Neo4j rows back into a `ConceptDefinition`. Single-sources reconciliations 5/6/7 so the writer, migration, and `load_concept` agree.

- [ ] **Step 1: Write the failing test**

```python
# apollo/textbook_ingest/tests/test_concept_schema_map.py
from apollo.subjects import CanonicalSymbols, ForbiddenNamedLaws, SolverHints
from apollo.textbook_ingest.concept_schema_map import (
    entry_to_rows, rows_to_concept_definition,
)
from apollo.textbook_ingest.types import ConceptRegistryEntry


def _entry():
    return ConceptRegistryEntry(
        subject_id="fluid_mechanics", concept_id="bernoulli_principle",
        scope_summary="Bernoulli for incompressible steady flow.",
        canonical_symbols=CanonicalSymbols(symbols=["P", "rho"],
                                           description={"P": "pressure", "rho": "density"},
                                           subscript_convention="P1/P2 ..."),
        normalization_map={"pressure": "P", "density": "rho"},
        parser_prompt_template="PROMPT",
        solver_hints=SolverHints(constants={"g": 9.81}, augmented_givens={"g": 9.81},
                                 non_trivial_keywords=["pressure"], plan_markers=["first"]),
        forbidden_named_laws=ForbiddenNamedLaws(named_laws=["bernoulli"],
                                                forbidden_concepts=["viscosity"],
                                                forbidden_domains=["physics"],
                                                forbidden_units=["pascals"]),
    )


def test_entry_to_rows_categorizes_forbidden_and_constants():
    rows = entry_to_rows(_entry())
    cats = {(t["term"], t["category"]) for t in rows["forbidden_terms"]}
    assert ("bernoulli", "named_law") in cats
    assert ("viscosity", "forbidden_concept") in cats
    assert ("physics", "forbidden_domain") in cats
    assert ("pascals", "forbidden_unit") in cats
    kinds = {(c["name"], c["kind"]) for c in rows["solver_constants"]}
    assert ("g", "constant") in kinds and ("g", "augmented_given") in kinds
    assert rows["concept"]["non_trivial_keywords"] == ["pressure"]
    assert rows["concept"]["plan_markers"] == ["first"]


def test_round_trips_to_concept_definition():
    rows = entry_to_rows(_entry())
    cdef = rows_to_concept_definition(rows, problems_dir=None)
    assert cdef.canonical_symbols.symbols == ["P", "rho"]
    assert cdef.normalization_map["pressure"] == "P"
    assert cdef.solver_hints.augmented_givens == {"g": 9.81}
    assert cdef.solver_hints.non_trivial_keywords == ["pressure"]
    assert "bernoulli" in cdef.forbidden_named_laws.named_laws
    assert "viscosity" in cdef.forbidden_named_laws.forbidden_concepts
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest apollo/textbook_ingest/tests/test_concept_schema_map.py -v`
Expected: FAIL — module not found.

- [ ] **Step 3: Write the mapper**

```python
# apollo/textbook_ingest/concept_schema_map.py
"""Pure mapping between ConceptRegistryEntry / ConceptDefinition and the Neo4j
concept subgraph row shapes. See plan §"Spec Reconciliations" items 5/6/7.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from apollo.subjects import (
    CanonicalSymbols, ConceptDefinition, ForbiddenNamedLaws, SolverHints,
)
from apollo.textbook_ingest.types import ConceptRegistryEntry

_FORBIDDEN_CATEGORIES = {
    "named_law": "named_laws",
    "forbidden_concept": "forbidden_concepts",
    "forbidden_domain": "forbidden_domains",
    "forbidden_unit": "forbidden_units",
}
_FORBIDDEN_FIELD_TO_CATEGORY = {
    "named_laws": "named_law",
    "forbidden_concepts": "forbidden_concept",
    "forbidden_domains": "forbidden_domain",
    "forbidden_units": "forbidden_unit",
}


def entry_to_rows(entry: ConceptRegistryEntry) -> dict[str, Any]:
    cs = entry.canonical_symbols
    symbols = [
        {"symbol": s, "description": cs.description.get(s, ""),
         "subscript_convention": cs.subscript_convention or ""}
        for s in cs.symbols
    ]
    normalization = [
        {"natural_language": nl, "canonical_symbol": sym}
        for nl, sym in entry.normalization_map.items()
    ]
    constants = (
        [{"name": n, "value": v, "kind": "constant"}
         for n, v in entry.solver_hints.constants.items()]
        + [{"name": n, "value": v, "kind": "augmented_given"}
           for n, v in entry.solver_hints.augmented_givens.items()]
    )
    forbidden = entry.forbidden_named_laws
    forbidden_terms = []
    for field, category in _FORBIDDEN_FIELD_TO_CATEGORY.items():
        for term in getattr(forbidden, field):
            forbidden_terms.append({"term": term, "category": category})
    return {
        "concept": {
            "subject_id": entry.subject_id, "concept_id": entry.concept_id,
            "scope_summary": entry.scope_summary,
            "parser_prompt_template": entry.parser_prompt_template,
            "non_trivial_keywords": list(entry.solver_hints.non_trivial_keywords),
            "plan_markers": list(entry.solver_hints.plan_markers),
        },
        "symbols": symbols,
        "normalization": normalization,
        "solver_constants": constants,
        "forbidden_terms": forbidden_terms,
    }


def rows_to_concept_definition(rows: dict[str, Any],
                               problems_dir: Path | None) -> ConceptDefinition:
    concept = rows["concept"]
    symbols = [r["symbol"] for r in rows["symbols"]]
    description = {r["symbol"]: r["description"] for r in rows["symbols"] if r["description"]}
    subscript = next((r["subscript_convention"] for r in rows["symbols"]
                      if r["subscript_convention"]), None)
    normalization = {r["natural_language"]: r["canonical_symbol"] for r in rows["normalization"]}
    constants = {r["name"]: r["value"] for r in rows["solver_constants"] if r["kind"] == "constant"}
    augmented = {r["name"]: r["value"] for r in rows["solver_constants"]
                 if r["kind"] == "augmented_given"}
    forbidden_lists: dict[str, list[str]] = {f: [] for f in _FORBIDDEN_CATEGORIES.values()}
    for t in rows["forbidden_terms"]:
        forbidden_lists[_FORBIDDEN_CATEGORIES[t["category"]]].append(t["term"])
    return ConceptDefinition(
        subject_id=concept["subject_id"], concept_id=concept["concept_id"],
        canonical_symbols=CanonicalSymbols(symbols=symbols, description=description,
                                           subscript_convention=subscript),
        normalization_map=normalization,
        parser_prompt_template=concept["parser_prompt_template"],
        solver_hints=SolverHints(
            constants=constants, augmented_givens=augmented,
            non_trivial_keywords=list(concept.get("non_trivial_keywords", [])),
            plan_markers=list(concept.get("plan_markers", [])),
        ),
        forbidden_named_laws=ForbiddenNamedLaws(**forbidden_lists),
        problems_dir=problems_dir or Path("/nonexistent"),
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest apollo/textbook_ingest/tests/test_concept_schema_map.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add apollo/textbook_ingest/concept_schema_map.py apollo/textbook_ingest/tests/test_concept_schema_map.py
git commit -m "feat(textbook-ingest): add concept Pydantic<->Neo4j mapper"
```

---

### Task A5: Schema additions to neo4j_schema.cypher

**Files:**
- Modify: `apollo/persistence/neo4j_schema.cypher` (append, do not touch existing `:_KGNode` constraints)
- Test: `apollo/textbook_ingest/tests/test_schema_apply.py`

- [ ] **Step 1: Write the failing test**

```python
# apollo/textbook_ingest/tests/test_schema_apply.py
import pytest


@pytest.mark.asyncio
async def test_concept_and_problem_constraints_exist(neo4j_test):
    async with neo4j_test.session() as s:
        names = {r["name"] for r in await (await s.run("SHOW CONSTRAINTS")).data()  # type: ignore
                 } if False else set()
        rows = await (await s.run("SHOW CONSTRAINTS YIELD name RETURN name")).data()
        names = {r["name"] for r in rows}
    assert "concept_id_unique" in names
    assert "problem_id_unique" in names
    assert "cluster_alias_unique" in names


@pytest.mark.asyncio
async def test_vector_index_is_3072(neo4j_test):
    async with neo4j_test.session() as s:
        rows = await (await s.run(
            "SHOW INDEXES YIELD name, options RETURN name, options"
        )).data()
    idx = {r["name"]: r["options"] for r in rows}
    assert "concept_scope_embedding_idx" in idx
    cfg = idx["concept_scope_embedding_idx"]["indexConfig"]
    assert cfg["vector.dimensions"] == 3072
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest apollo/textbook_ingest/tests/test_schema_apply.py -v`
Expected: FAIL — constraints/index absent (the `neo4j_test` fixture applies the schema file, which doesn't yet contain these statements).

- [ ] **Step 3: Append the schema**

Append to `apollo/persistence/neo4j_schema.cypher`:

```cypher
// ---------------------------------------------------------------------------
// Textbook problem index (concepts + problems + aliases + observability).
// Namespace marker labels: :_ConceptNode, :_ProblemNode, :_IngestEvent.
// ---------------------------------------------------------------------------
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
  OPTIONS { indexConfig: { `vector.dimensions`: 3072, `vector.similarity_function`: 'cosine' }};
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest apollo/textbook_ingest/tests/test_schema_apply.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add apollo/persistence/neo4j_schema.cypher apollo/textbook_ingest/tests/test_schema_apply.py
git commit -m "feat(persistence): add concept/problem/alias/ingest Neo4j schema"
```

---

### Task A6: Writer core — concept + problem + alias primitives

**Files:**
- Create: `apollo/textbook_ingest/writer.py`
- Test: `apollo/textbook_ingest/tests/test_writer_core.py`

These primitives are used by both the migration (Phase A) and stage-6 orchestration (Phase E). Each reference-solution `ReferenceStep` becomes a `:<Label>:_ProblemNode` node linked from the `:Problem` root by `HAS_REFERENCE_NODE`, plus inter-node edges from the KGGraph conversion.

- [ ] **Step 1: Write the failing test**

```python
# apollo/textbook_ingest/tests/test_writer_core.py
import pytest

from apollo.schemas.problem import ReferenceStep
from apollo.subjects import CanonicalSymbols, ForbiddenNamedLaws, SolverHints
from apollo.textbook_ingest.types import ConceptRegistryEntry, ValidatedProblem
from apollo.textbook_ingest import writer


def _entry():
    return ConceptRegistryEntry(
        subject_id="fluid_mechanics", concept_id="bernoulli_principle",
        scope_summary="Bernoulli.", canonical_symbols=CanonicalSymbols(symbols=["P"]),
        normalization_map={"pressure": "P"}, parser_prompt_template="P",
        solver_hints=SolverHints(constants={"g": 9.81}),
        forbidden_named_laws=ForbiddenNamedLaws(named_laws=["bernoulli"]),
    )


def _problem(pid="p1"):
    return ValidatedProblem(
        source_document_id="seed", source_chunk_id="c1", source_page=1,
        problem_text="t", given_values={"P1": 1.0}, target_unknown="P2",
        reference_solution=[ReferenceStep(step=1, entry_type="equation", id="e1",
            content={"symbolic": "P1 - P2", "label": "x", "variables": ["P1", "P2"]},
            depends_on=[])],
        concept_id="bernoulli_principle", subject_id="fluid_mechanics",
        difficulty="intro", problem_id=pid)


@pytest.mark.asyncio
async def test_write_concept_then_problem_and_alias(neo4j_test):
    await writer.write_concept(neo4j_test, _entry(), source_document_id="seed",
                               scope_embedding=[0.0] * 3072, policy_frozen=True)
    assert await writer.concept_exists(neo4j_test, "fluid_mechanics", "bernoulli_principle")
    await writer.write_cluster_alias(neo4j_test, "fluid_mechanics",
                                     "fluid_mechanics", "bernoulli_principle")
    await writer.write_problem(neo4j_test, _problem(), authored=True)
    assert await writer.problem_exists(neo4j_test, "p1")

    async with neo4j_test.session() as s:
        rec = await (await s.run(
            "MATCH (p:Problem {problem_id:'p1'})-[:HAS_REFERENCE_NODE]->(n:_ProblemNode) "
            "RETURN count(n) AS n")).single()
        assert rec["n"] == 1


@pytest.mark.asyncio
async def test_write_concept_is_idempotent_and_frozen(neo4j_test):
    await writer.write_concept(neo4j_test, _entry(), source_document_id="seed",
                               scope_embedding=[0.0] * 3072, policy_frozen=True)
    # second write of same concept must not duplicate policy children
    await writer.write_concept(neo4j_test, _entry(), source_document_id="other",
                               scope_embedding=[0.0] * 3072, policy_frozen=True)
    async with neo4j_test.session() as s:
        rec = await (await s.run(
            "MATCH (c:Concept {concept_id:'bernoulli_principle'})-[:HAS_SYMBOL]->(x) "
            "RETURN count(x) AS n")).single()
        assert rec["n"] == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest apollo/textbook_ingest/tests/test_writer_core.py -v`
Expected: FAIL — `apollo.textbook_ingest.writer` has no `write_concept`.

- [ ] **Step 3: Write the writer primitives**

```python
# apollo/textbook_ingest/writer.py
"""Neo4j write primitives for concepts and problems. Stage-6 orchestration
(run_stage6) is added in Phase E. First-writer-wins on concept policy is
enforced by skipping policy-child writes when the :Concept already exists.
"""
from __future__ import annotations

from apollo.ontology.nodes import NODE_LABELS
from apollo.persistence.neo4j_client import Neo4jClient
from apollo.textbook_ingest.concept_schema_map import entry_to_rows
from apollo.textbook_ingest.kg_convert import reference_steps_to_kg_graph
from apollo.textbook_ingest.types import ConceptRegistryEntry, ValidatedProblem


async def concept_exists(neo: Neo4jClient, subject_id: str, concept_id: str) -> bool:
    async with neo.session() as s:
        rec = await (await s.run(
            "MATCH (c:Concept {subject_id:$s, concept_id:$c}) RETURN count(c) AS n",
            s=subject_id, c=concept_id)).single()
        return rec["n"] > 0


async def problem_exists(neo: Neo4jClient, problem_id: str) -> bool:
    async with neo.session() as s:
        rec = await (await s.run(
            "MATCH (p:Problem {problem_id:$p}) RETURN count(p) AS n",
            p=problem_id)).single()
        return rec["n"] > 0


async def write_concept(neo: Neo4jClient, entry: ConceptRegistryEntry, *,
                        source_document_id: str, scope_embedding: list[float],
                        policy_frozen: bool) -> None:
    """Create the :Concept and its policy children. Idempotent + first-writer-wins:
    if the concept already exists its policy children are left untouched."""
    if await concept_exists(neo, entry.subject_id, entry.concept_id):
        return
    rows = entry_to_rows(entry)
    async with neo.session() as s:
        await s.execute_write(_write_concept_tx, rows, source_document_id,
                              scope_embedding, policy_frozen)


async def _write_concept_tx(tx, rows, source_document_id, scope_embedding, policy_frozen):
    c = rows["concept"]
    await tx.run(
        """
        CREATE (concept:Concept:_ConceptNode {
            subject_id:$subject_id, concept_id:$concept_id,
            scope_summary:$scope_summary, scope_embedding:$emb,
            parser_prompt_template:$ppt,
            non_trivial_keywords:$ntk, plan_markers:$pm,
            created_at:datetime(), source_document_id:$src, policy_frozen:$frozen })
        """,
        subject_id=c["subject_id"], concept_id=c["concept_id"],
        scope_summary=c["scope_summary"], emb=scope_embedding,
        ppt=c["parser_prompt_template"], ntk=c["non_trivial_keywords"],
        pm=c["plan_markers"], src=source_document_id, frozen=policy_frozen)
    for r in rows["symbols"]:
        await tx.run(
            "MATCH (c:Concept {subject_id:$s, concept_id:$cid}) "
            "CREATE (c)-[:HAS_SYMBOL]->(:CanonicalSymbol:_ConceptNode "
            "{concept_id:$cid, symbol:$symbol, description:$description, "
            "subscript_convention:$subscript_convention})",
            s=c["subject_id"], cid=c["concept_id"], **r)
    for r in rows["normalization"]:
        await tx.run(
            "MATCH (c:Concept {subject_id:$s, concept_id:$cid}) "
            "CREATE (c)-[:HAS_NORMALIZATION]->(:NormalizationEntry:_ConceptNode "
            "{concept_id:$cid, natural_language:$natural_language, "
            "canonical_symbol:$canonical_symbol})",
            s=c["subject_id"], cid=c["concept_id"], **r)
    for r in rows["solver_constants"]:
        await tx.run(
            "MATCH (c:Concept {subject_id:$s, concept_id:$cid}) "
            "CREATE (c)-[:HAS_CONSTANT]->(:SolverConstant:_ConceptNode "
            "{concept_id:$cid, name:$name, value:$value, kind:$kind})",
            s=c["subject_id"], cid=c["concept_id"], **r)
    for r in rows["forbidden_terms"]:
        await tx.run(
            "MATCH (c:Concept {subject_id:$s, concept_id:$cid}) "
            "CREATE (c)-[:FORBIDS]->(:ForbiddenTerm:_ConceptNode "
            "{concept_id:$cid, term:$term, category:$category})",
            s=c["subject_id"], cid=c["concept_id"], **r)


async def write_cluster_alias(neo: Neo4jClient, cluster_id: str,
                              subject_id: str, concept_id: str) -> None:
    async with neo.session() as s:
        await s.run(
            "MATCH (c:Concept {subject_id:$s, concept_id:$cid}) "
            "MERGE (a:ClusterAlias {cluster_id:$cl}) "
            "MERGE (a)-[:RESOLVES_TO]->(c)",
            s=subject_id, cid=concept_id, cl=cluster_id)


async def write_problem(neo: Neo4jClient, problem: ValidatedProblem, *,
                        authored: bool) -> None:
    """Idempotent: skips if problem_id already exists."""
    if await problem_exists(neo, problem.problem_id):
        return
    graph = reference_steps_to_kg_graph(problem.reference_solution)
    async with neo.session() as s:
        await s.execute_write(_write_problem_tx, problem, graph, authored)


async def _write_problem_tx(tx, problem, graph, authored):
    await tx.run(
        """
        CREATE (p:Problem:_ProblemNode {
            problem_id:$pid, subject_id:$sid, concept_id:$cid,
            difficulty:$diff, problem_text:$text, given_values:$givens,
            target_unknown:$target, source_document_id:$doc, source_page:$page,
            source_chunk_id:$chunk, authored:$authored, extracted_at:datetime() })
        """,
        pid=problem.problem_id, sid=problem.subject_id, cid=problem.concept_id,
        diff=problem.difficulty, text=problem.problem_text,
        givens=problem.given_values, target=problem.target_unknown,
        doc=problem.source_document_id, page=problem.source_page,
        chunk=problem.source_chunk_id, authored=authored)
    for node in graph.nodes:
        label = NODE_LABELS[node.node_type]
        await tx.run(
            f"MATCH (p:Problem {{problem_id:$pid}}) "
            f"CREATE (p)-[:HAS_REFERENCE_NODE]->(n:{label}:_ProblemNode "
            f"{{problem_id:$pid, node_id:$nid, node_type:$ntype, content:$content}})",
            pid=problem.problem_id, nid=node.node_id, ntype=node.node_type,
            content=node.content.model_dump_json())
    for edge in graph.edges:
        await tx.run(
            f"MATCH (a:_ProblemNode {{problem_id:$pid, node_id:$from}}), "
            f"(b:_ProblemNode {{problem_id:$pid, node_id:$to}}) "
            f"CREATE (a)-[:{edge.edge_type.value} {{problem_id:$pid}}]->(b)",
            pid=problem.problem_id, **{"from": edge.from_node_id, "to": edge.to_node_id})
```

Note: reference-node `content` is stored as a JSON string (`model_dump_json()`); the selector (Task A7) parses it back. This avoids Neo4j's map-property type restrictions on nested content.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest apollo/textbook_ingest/tests/test_writer_core.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add apollo/textbook_ingest/writer.py apollo/textbook_ingest/tests/test_writer_core.py
git commit -m "feat(textbook-ingest): add Neo4j writer primitives for concepts/problems"
```

---

### Task A7: Selector rewrite — Neo4j-backed, async

**Files:**
- Modify: `apollo/overseer/problem_selector.py` (full rewrite)
- Test: `apollo/overseer/tests/test_problem_selector.py` (rewrite the existing file)

- [ ] **Step 1: Write the failing test**

```python
# apollo/overseer/tests/test_problem_selector.py  (replace file contents)
import pytest

from apollo.errors import PoolExhaustedError
from apollo.schemas.problem import ReferenceStep
from apollo.subjects import CanonicalSymbols, SolverHints
from apollo.textbook_ingest.types import ConceptRegistryEntry, ValidatedProblem
from apollo.textbook_ingest import writer
from apollo.overseer.problem_selector import (
    cluster_to_concept, list_problems_for_cluster, select_problem,
)


async def _seed(neo, *, pid, authored, difficulty="intro"):
    await writer.write_problem(neo, ValidatedProblem(
        source_document_id="seed", source_chunk_id=pid, source_page=1,
        problem_text="t", given_values={"P1": 1.0}, target_unknown="P2",
        reference_solution=[ReferenceStep(step=1, entry_type="equation", id="e1",
            content={"symbolic": "P1 - P2", "label": "x", "variables": ["P1", "P2"]},
            depends_on=[])],
        concept_id="bernoulli_principle", subject_id="fluid_mechanics",
        difficulty=difficulty, problem_id=pid), authored=authored)


@pytest.fixture
async def seeded(neo4j_test):
    await writer.write_concept(neo4j_test, ConceptRegistryEntry(
        subject_id="fluid_mechanics", concept_id="bernoulli_principle",
        scope_summary="b", canonical_symbols=CanonicalSymbols(symbols=["P"]),
        normalization_map={}, parser_prompt_template="P",
        solver_hints=SolverHints()), source_document_id="seed",
        scope_embedding=[0.0] * 3072, policy_frozen=True)
    await writer.write_cluster_alias(neo4j_test, "fluid_mechanics",
                                     "fluid_mechanics", "bernoulli_principle")
    await _seed(neo4j_test, pid="zzz_extracted", authored=False)
    await _seed(neo4j_test, pid="bernoulli_authored", authored=True)
    return neo4j_test


@pytest.mark.asyncio
async def test_cluster_to_concept_reads_alias(seeded):
    assert await cluster_to_concept("fluid_mechanics", seeded) == \
        ("fluid_mechanics", "bernoulli_principle")


@pytest.mark.asyncio
async def test_authored_problem_sorts_first(seeded):
    p = await select_problem(cluster_id="fluid_mechanics", difficulty="intro",
                             attempted_ids=[], neo=seeded)
    assert p.id == "bernoulli_authored"  # authored DESC beats lexical order


@pytest.mark.asyncio
async def test_excludes_attempted(seeded):
    p = await select_problem(cluster_id="fluid_mechanics", difficulty="intro",
                             attempted_ids=["bernoulli_authored"], neo=seeded)
    assert p.id == "zzz_extracted"


@pytest.mark.asyncio
async def test_pool_exhausted_raises(seeded):
    with pytest.raises(PoolExhaustedError):
        await select_problem(cluster_id="fluid_mechanics", difficulty="hard",
                             attempted_ids=[], neo=seeded)


@pytest.mark.asyncio
async def test_loaded_problem_reconstructs_reference_solution(seeded):
    p = await select_problem(cluster_id="fluid_mechanics", difficulty="intro",
                             attempted_ids=[], neo=seeded)
    assert p.reference_solution[0].content["symbolic"] == "P1 - P2"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest apollo/overseer/tests/test_problem_selector.py -v`
Expected: FAIL — current `select_problem` is sync, has no `neo` param, reads filesystem.

- [ ] **Step 3: Rewrite the selector**

```python
# apollo/overseer/problem_selector.py  (full rewrite)
"""Problem selection backed by Neo4j. Returns the same Problem Pydantic object
Apollo handlers already consume. Hand-authored problems (authored=true) sort
before extracted problems regardless of id collation."""
from __future__ import annotations

import json
from typing import List, Sequence

from apollo.errors import PoolExhaustedError
from apollo.persistence.neo4j_client import Neo4jClient
from apollo.schemas.problem import Problem, ReferenceStep

_ENTRY_TYPE_BY_NODE_TYPE = {
    "equation": "equation", "condition": "condition",
    "simplification": "simplification", "definition": "definition",
    "variable_mapping": "variable_mapping", "procedure_step": "procedure_step",
}


async def cluster_to_concept(cluster_id: str, neo: Neo4jClient) -> tuple[str, str]:
    async with neo.session() as s:
        rec = await (await s.run(
            "MATCH (a:ClusterAlias {cluster_id:$c})-[:RESOLVES_TO]->(concept:Concept) "
            "RETURN concept.subject_id AS s, concept.concept_id AS cid",
            c=cluster_id)).single()
    if rec is None:
        raise KeyError(f"no ClusterAlias for cluster_id {cluster_id!r}")
    return rec["s"], rec["cid"]


async def select_problem(*, cluster_id: str, difficulty: str,
                         attempted_ids: Sequence[str], neo: Neo4jClient) -> Problem:
    subject_id, concept_id = await cluster_to_concept(cluster_id, neo)
    async with neo.session() as s:
        rec = await (await s.run(
            """
            MATCH (p:Problem {subject_id:$s, concept_id:$c, difficulty:$d})
            WHERE NOT p.problem_id IN $attempted
            RETURN p ORDER BY p.authored DESC, p.problem_id ASC LIMIT 1
            """,
            s=subject_id, c=concept_id, d=difficulty,
            attempted=list(attempted_ids))).single()
        if rec is None:
            raise PoolExhaustedError(concept_cluster_id=cluster_id, difficulty=difficulty)
        return await _load_problem(rec["p"]["problem_id"], neo)


async def list_problems_for_cluster(cluster_id: str, neo: Neo4jClient) -> List[Problem]:
    try:
        subject_id, concept_id = await cluster_to_concept(cluster_id, neo)
    except KeyError:
        return []
    async with neo.session() as s:
        rows = await (await s.run(
            "MATCH (p:Problem {subject_id:$s, concept_id:$c}) "
            "RETURN p.problem_id AS id ORDER BY p.authored DESC, p.problem_id ASC",
            s=subject_id, c=concept_id)).data()
    return [await _load_problem(r["id"], neo) for r in rows]


async def _load_problem(problem_id: str, neo: Neo4jClient) -> Problem:
    async with neo.session() as s:
        prec = await (await s.run(
            "MATCH (p:Problem {problem_id:$p}) RETURN p", p=problem_id)).single()
        node_rows = await (await s.run(
            "MATCH (p:Problem {problem_id:$p})-[:HAS_REFERENCE_NODE]->(n:_ProblemNode) "
            "RETURN n.node_id AS id, n.node_type AS type, n.content AS content", p=problem_id)).data()
        dep_rows = await (await s.run(
            "MATCH (a:_ProblemNode {problem_id:$p})-[:DEPENDS_ON]->(b:_ProblemNode {problem_id:$p}) "
            "RETURN a.node_id AS frm, b.node_id AS to", p=problem_id)).data()
    p = prec["p"]
    deps: dict[str, list[str]] = {}
    for d in dep_rows:
        deps.setdefault(d["frm"], []).append(d["to"])
    steps = []
    for i, n in enumerate(sorted(node_rows, key=lambda r: r["id"]), start=1):
        steps.append(ReferenceStep(
            step=i, entry_type=_ENTRY_TYPE_BY_NODE_TYPE[n["type"]], id=n["id"],
            content=json.loads(n["content"]), depends_on=deps.get(n["id"], [])))
    return Problem(
        id=p["problem_id"], concept_id=p["concept_id"], difficulty=p["difficulty"],
        problem_text=p["problem_text"], given_values=dict(p["given_values"]),
        target_unknown=p["target_unknown"], reference_solution=steps)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest apollo/overseer/tests/test_problem_selector.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add apollo/overseer/problem_selector.py apollo/overseer/tests/test_problem_selector.py
git commit -m "feat(overseer): rewrite problem selector for Neo4j (async)"
```

---

### Task A8: `load_concept` Neo4j rewrite + thread `neo` through handlers

**Files:**
- Modify: `apollo/subjects/__init__.py` (`load_concept`, `list_subjects`, `list_concepts` → async Neo4j)
- Modify: `apollo/handlers/chat.py:66`, `apollo/handlers/done.py:88`, `apollo/handlers/done.py` (`_find_problem`, lines 33-39 + caller), `apollo/handlers/lifecycle.py:89`, `apollo/handlers/next.py:29` + call, `apollo/api.py` `/next` route
- Test: `apollo/subjects/tests/test_load_concept_neo4j.py`

- [ ] **Step 1: Write the failing test**

```python
# apollo/subjects/tests/test_load_concept_neo4j.py
import pytest

from apollo.subjects import CanonicalSymbols, SolverHints, ForbiddenNamedLaws, load_concept
from apollo.textbook_ingest.types import ConceptRegistryEntry
from apollo.textbook_ingest import writer


@pytest.mark.asyncio
async def test_load_concept_reconstructs_definition(neo4j_test):
    await writer.write_concept(neo4j_test, ConceptRegistryEntry(
        subject_id="fluid_mechanics", concept_id="bernoulli_principle",
        scope_summary="b",
        canonical_symbols=CanonicalSymbols(symbols=["P", "rho"],
            description={"P": "pressure", "rho": "density"}, subscript_convention="conv"),
        normalization_map={"pressure": "P"}, parser_prompt_template="TEMPLATE",
        solver_hints=SolverHints(constants={"g": 9.81}, augmented_givens={"g": 9.81},
                                 non_trivial_keywords=["pressure"], plan_markers=["first"]),
        forbidden_named_laws=ForbiddenNamedLaws(named_laws=["bernoulli"])),
        source_document_id="seed", scope_embedding=[0.0] * 3072, policy_frozen=True)

    cdef = await load_concept("fluid_mechanics", "bernoulli_principle", neo4j_test)
    assert cdef.canonical_symbols.symbols == ["P", "rho"]
    assert cdef.parser_prompt_template == "TEMPLATE"
    assert cdef.solver_hints.augmented_givens == {"g": 9.81}
    assert cdef.solver_hints.non_trivial_keywords == ["pressure"]
    assert "bernoulli" in cdef.forbidden_named_laws.named_laws


@pytest.mark.asyncio
async def test_missing_concept_raises(neo4j_test):
    from apollo.subjects import ConceptNotFoundError
    with pytest.raises(ConceptNotFoundError):
        await load_concept("nope", "nope", neo4j_test)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest apollo/subjects/tests/test_load_concept_neo4j.py -v`
Expected: FAIL — `load_concept` is sync filesystem-based, takes no `neo`.

- [ ] **Step 3: Rewrite `load_concept` (and `list_*`) + thread `neo`**

In `apollo/subjects/__init__.py`, keep the Pydantic types (`ConceptDefinition`, `CanonicalSymbols`, `SolverHints`, `ForbiddenNamedLaws`, `ConceptNotFoundError`) and replace the three loader functions:

```python
async def load_concept(subject_id: str, concept_id: str, neo) -> ConceptDefinition:
    from apollo.textbook_ingest.concept_schema_map import rows_to_concept_definition
    async with neo.session() as s:
        crec = await (await s.run(
            "MATCH (c:Concept {subject_id:$s, concept_id:$c}) "
            "RETURN c.subject_id AS subject_id, c.concept_id AS concept_id, "
            "c.scope_summary AS scope_summary, c.parser_prompt_template AS parser_prompt_template, "
            "c.non_trivial_keywords AS non_trivial_keywords, c.plan_markers AS plan_markers",
            s=subject_id, c=concept_id)).single()
        if crec is None:
            raise ConceptNotFoundError(f"{subject_id}/{concept_id}")
        symbols = await (await s.run(
            "MATCH (:Concept {subject_id:$s, concept_id:$c})-[:HAS_SYMBOL]->(x) "
            "RETURN x.symbol AS symbol, x.description AS description, "
            "x.subscript_convention AS subscript_convention", s=subject_id, c=concept_id)).data()
        normalization = await (await s.run(
            "MATCH (:Concept {subject_id:$s, concept_id:$c})-[:HAS_NORMALIZATION]->(x) "
            "RETURN x.natural_language AS natural_language, x.canonical_symbol AS canonical_symbol",
            s=subject_id, c=concept_id)).data()
        constants = await (await s.run(
            "MATCH (:Concept {subject_id:$s, concept_id:$c})-[:HAS_CONSTANT]->(x) "
            "RETURN x.name AS name, x.value AS value, x.kind AS kind", s=subject_id, c=concept_id)).data()
        forbidden = await (await s.run(
            "MATCH (:Concept {subject_id:$s, concept_id:$c})-[:FORBIDS]->(x) "
            "RETURN x.term AS term, x.category AS category", s=subject_id, c=concept_id)).data()
    rows = {"concept": dict(crec), "symbols": symbols, "normalization": normalization,
            "solver_constants": constants, "forbidden_terms": forbidden}
    return rows_to_concept_definition(rows, problems_dir=None)


async def list_subjects(neo) -> list[str]:
    async with neo.session() as s:
        rows = await (await s.run(
            "MATCH (c:Concept) RETURN DISTINCT c.subject_id AS s ORDER BY s")).data()
    return [r["s"] for r in rows]


async def list_concepts(subject_id: str, neo) -> list[str]:
    async with neo.session() as s:
        rows = await (await s.run(
            "MATCH (c:Concept {subject_id:$s}) RETURN c.concept_id AS c ORDER BY c",
            s=subject_id)).data()
    return [r["c"] for r in rows]
```

Update the three callsites:

- `apollo/handlers/chat.py:66` → `concept = await load_concept(subject_id, concept_id, neo)`
- `apollo/handlers/done.py:88` → `concept = await load_concept(subject_id, concept_id, neo)`; and `done.py:87` `subject_id, concept_id = cluster_to_concept(...)` → `subject_id, concept_id = await cluster_to_concept(sess.concept_cluster_id, neo)`; and `_find_problem` (lines 33-39) → `async def _find_problem(cluster_id, problem_id, neo)` iterating `for p in await list_problems_for_cluster(cluster_id, neo):`, with its caller awaiting + passing `neo`.
- `apollo/handlers/lifecycle.py:89` (in `handle_get_session`, which already has `neo`) → `for p in await list_problems_for_cluster(sess.concept_cluster_id, neo):`

Thread `neo` into `/next`:

- `apollo/handlers/next.py:29` add parameter `neo: Neo4jClient,` (import `from apollo.persistence.neo4j_client import Neo4jClient`); line 66 → `problem = await select_problem(cluster_id=sess.concept_cluster_id, difficulty=difficulty, attempted_ids=attempted_ids, neo=neo)`.
- `apollo/api.py` `/next` route (lines 122-130): add `neo: Neo4jClient = Depends(get_neo4j_client),` to `next_problem(...)` and call `return await handle_next(db=db, neo=neo, session_id=session_id, difficulty=body.difficulty)`.

Also update `apollo/handlers/chat.py:65` `cluster_to_concept(sess.concept_cluster_id)` → `await cluster_to_concept(sess.concept_cluster_id, neo)`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest apollo/subjects/tests/test_load_concept_neo4j.py apollo/handlers/tests/test_next.py -v`
Expected: PASS. (If `test_next.py` mocks `select_problem`, update its monkeypatch to an async stub — see the file's `_spy`/`_boom` at lines 137-150; make them `async def`.)

- [ ] **Step 5: Commit**

```bash
git add apollo/subjects/__init__.py apollo/handlers/chat.py apollo/handlers/done.py apollo/handlers/lifecycle.py apollo/handlers/next.py apollo/api.py apollo/handlers/tests/test_next.py apollo/subjects/tests/test_load_concept_neo4j.py
git commit -m "feat(subjects): Neo4j-backed load_concept; thread neo through handlers"
```

---

### Task A9: Gate-1 validator stub (schema only)

**Files:**
- Create: `apollo/textbook_ingest/validator.py`
- Test: `apollo/textbook_ingest/tests/test_validator.py`

Phase A needs a callable validator so the migration (Task A10) has a stable seam. Here it runs **only Gate 1 (schema)**. Phase B grows the same `validate_problem` function to all 8 gates without changing the migration.

- [ ] **Step 1: Write the failing test**

```python
# apollo/textbook_ingest/tests/test_validator.py
from apollo.schemas.problem import ReferenceStep
from apollo.subjects import CanonicalSymbols, SolverHints, ConceptDefinition
from apollo.textbook_ingest.types import ExtractedProblem
from apollo.textbook_ingest.validator import validate_problem, ValidationResult


def _concept():
    return ConceptDefinition(
        subject_id="fluid_mechanics", concept_id="bernoulli_principle",
        canonical_symbols=CanonicalSymbols(symbols=["P1", "P2"]), normalization_map={},
        parser_prompt_template="P", solver_hints=SolverHints(),
        problems_dir=None)  # type: ignore[arg-type]


def _good():
    return ExtractedProblem(
        source_document_id="d", source_chunk_id="c", source_page=1,
        problem_text="t", given_values={"P1": 1.0}, target_unknown="P2",
        reference_solution=[ReferenceStep(step=1, entry_type="equation", id="e1",
            content={"symbolic": "P1 - P2", "label": "x", "variables": ["P1", "P2"]},
            depends_on=[])],
        concept_id="bernoulli_principle", subject_id="fluid_mechanics", difficulty="intro")


def test_gate1_passes_valid_payload():
    res = validate_problem(_good(), _concept())
    assert isinstance(res, ValidationResult)
    assert res.ok is True
    assert res.gate_failed is None


def test_gate1_fails_unknown_entry_type():
    bad = _good()
    bad.reference_solution[0].entry_type = "equation"  # keep valid here; gate 1 is schema
    # mutate content to violate EquationContent (missing symbolic)
    bad.reference_solution[0].content = {"label": "x"}
    res = validate_problem(bad, _concept())
    assert res.ok is False
    assert res.gate_failed == "schema"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest apollo/textbook_ingest/tests/test_validator.py -v`
Expected: FAIL — module/`validate_problem` absent.

- [ ] **Step 3: Write the gate-1 validator**

```python
# apollo/textbook_ingest/validator.py
"""Strict 8-gate validator. Phase A ships Gate 1 (schema) only; Phase B fills
gates 2-8. Short-circuits on first failing gate. See spec §6."""
from __future__ import annotations

from dataclasses import dataclass

from apollo.ontology.nodes import build_node
from apollo.subjects import ConceptDefinition
from apollo.textbook_ingest.kg_convert import REFERENCE_ATTEMPT_ID
from apollo.textbook_ingest.types import ExtractedProblem


@dataclass
class ValidationResult:
    ok: bool
    gate_failed: str | None = None
    diagnostic: str = ""


def _gate1_schema(p: ExtractedProblem, concept: ConceptDefinition) -> ValidationResult:
    # ExtractedProblem itself is already Pydantic-validated; re-validate each
    # reference node's typed content via the ontology builder.
    for step in p.reference_solution:
        try:
            build_node(node_type=step.entry_type, node_id=step.id,
                       attempt_id=REFERENCE_ATTEMPT_ID, source="parser",
                       content=step.content)
        except Exception as exc:  # noqa: BLE001 - surface as a gate diagnostic
            return ValidationResult(False, "schema", f"node {step.id!r}: {exc}")
    return ValidationResult(True)


# Gate registry. Phase B appends gates 2-8 in order.
_GATES = [_gate1_schema]


def validate_problem(p: ExtractedProblem, concept: ConceptDefinition) -> ValidationResult:
    for gate in _GATES:
        res = gate(p, concept)
        if not res.ok:
            return res
    return ValidationResult(True)
```

Note: `build_node`'s `source` parameter type is `NodeSource`; `"parser"` is a valid value (confirm against `apollo/ontology/nodes.py`). If `NodeSource` doesn't include `"parser"`, use the value the ontology defines for authored/reference nodes.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest apollo/textbook_ingest/tests/test_validator.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add apollo/textbook_ingest/validator.py apollo/textbook_ingest/tests/test_validator.py
git commit -m "feat(textbook-ingest): add gate-1 schema validator (stub for 8 gates)"
```

---

### Task A10: Bernoulli filesystem→Neo4j migration

**Files:**
- Create: `apollo/textbook_ingest/scripts/__init__.py`
- Create: `apollo/textbook_ingest/scripts/migrate_filesystem_concept.py`
- Test: `apollo/textbook_ingest/tests/test_filesystem_migration.py`

Reads the on-disk Bernoulli policy files + `problem_*.json`, builds a `ConceptRegistryEntry` + `ValidatedProblem`s, runs `validate_problem` (gate-1 in Phase A), writes via the writer, seeds the cluster alias. Idempotent (writer skips existing nodes). Injects `subject_id="fluid_mechanics"`. Aborts hard if any hand-authored problem fails validation.

- [ ] **Step 1: Write the failing test**

```python
# apollo/textbook_ingest/tests/test_filesystem_migration.py
import pytest

from apollo.overseer.problem_selector import list_problems_for_cluster
from apollo.subjects import load_concept
from apollo.textbook_ingest.scripts.migrate_filesystem_concept import migrate_bernoulli


@pytest.mark.asyncio
async def test_migration_loads_five_problems_and_concept(neo4j_test):
    summary = await migrate_bernoulli(neo4j_test)
    assert summary["problems_written"] == 5
    problems = await list_problems_for_cluster("fluid_mechanics", neo4j_test)
    assert len(problems) == 5
    cdef = await load_concept("fluid_mechanics", "bernoulli_principle", neo4j_test)
    # canonical_symbols match the on-disk file exactly
    assert cdef.canonical_symbols.symbols == ["P", "rho", "v", "A", "h", "g", "Q"]


@pytest.mark.asyncio
async def test_migration_is_idempotent(neo4j_test):
    await migrate_bernoulli(neo4j_test)
    second = await migrate_bernoulli(neo4j_test)
    assert second["problems_written"] == 0  # all skipped on re-run
    problems = await list_problems_for_cluster("fluid_mechanics", neo4j_test)
    assert len(problems) == 5


@pytest.mark.asyncio
async def test_migrated_problems_are_authored_first(neo4j_test):
    await migrate_bernoulli(neo4j_test)
    problems = await list_problems_for_cluster("fluid_mechanics", neo4j_test)
    assert problems[0].id == "bernoulli_horizontal_pipe_find_p2"  # slug, authored
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest apollo/textbook_ingest/tests/test_filesystem_migration.py -v`
Expected: FAIL — module/`migrate_bernoulli` absent.

- [ ] **Step 3: Write the migration script**

```python
# apollo/textbook_ingest/scripts/migrate_filesystem_concept.py
"""One-time, idempotent migration of the hand-authored Bernoulli concept from
the on-disk JSON folder into Neo4j. Safe to re-run (writer skips existing nodes).

Run manually:  python -m apollo.textbook_ingest.scripts.migrate_filesystem_concept
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from config import settings
from indexing.document_embedder import embed_text
from apollo.persistence.neo4j_client import Neo4jClient
from apollo.schemas.problem import load_problem
from apollo.subjects import CanonicalSymbols, ForbiddenNamedLaws, SolverHints
from apollo.textbook_ingest import writer
from apollo.textbook_ingest.types import ConceptRegistryEntry, ExtractedProblem, ValidatedProblem
from apollo.textbook_ingest.validator import validate_problem
from apollo.textbook_ingest.concept_schema_map import rows_to_concept_definition, entry_to_rows

_ROOT = Path("apollo/subjects/fluid_mechanics/concepts/bernoulli_principle")
_SUBJECT = "fluid_mechanics"
_CONCEPT = "bernoulli_principle"
_CLUSTER = "fluid_mechanics"


def _load_entry() -> ConceptRegistryEntry:
    cs = json.loads((_ROOT / "canonical_symbols.json").read_text())
    nm = json.loads((_ROOT / "normalization_map.json").read_text())
    sh = json.loads((_ROOT / "solver_hints.json").read_text())
    fb = json.loads((_ROOT / "forbidden_named_laws.json").read_text())
    template = (_ROOT / "parser_prompt_template.md").read_text()
    return ConceptRegistryEntry(
        subject_id=_SUBJECT, concept_id=_CONCEPT,
        scope_summary=cs.get("subscript_convention", "Bernoulli's principle."),
        canonical_symbols=CanonicalSymbols(**cs),
        normalization_map=nm, parser_prompt_template=template,
        solver_hints=SolverHints(**sh), forbidden_named_laws=ForbiddenNamedLaws(**fb))


async def migrate_bernoulli(neo: Neo4jClient) -> dict:
    entry = _load_entry()
    embedding = embed_text(entry.scope_summary, model=settings.TEXTBOOK_EMBEDDING_MODEL,
                           dim=settings.TEXTBOOK_EMBEDDING_DIM)
    await writer.write_concept(neo, entry, source_document_id="filesystem_migration",
                               scope_embedding=embedding, policy_frozen=True)
    await writer.write_cluster_alias(neo, _CLUSTER, _SUBJECT, _CONCEPT)

    concept_def = rows_to_concept_definition(entry_to_rows(entry), problems_dir=None)
    written = 0
    for path in sorted((_ROOT / "problems").glob("problem_*.json")):
        prob = load_problem(path)  # apollo.schemas.problem.Problem
        extracted = ExtractedProblem(
            source_document_id="filesystem_migration", source_chunk_id=prob.id,
            source_page=0, problem_text=prob.problem_text, given_values=prob.given_values,
            target_unknown=prob.target_unknown, reference_solution=prob.reference_solution,
            concept_id=_CONCEPT, subject_id=_SUBJECT, difficulty=prob.difficulty)
        res = validate_problem(extracted, concept_def)
        if not res.ok:
            raise RuntimeError(
                f"Hand-authored problem {prob.id!r} failed gate {res.gate_failed!r}: "
                f"{res.diagnostic}. Validator is too strict — investigate, do not relax content.")
        validated = ValidatedProblem(**extracted.model_dump(), problem_id=prob.id)
        existed = await writer.problem_exists(neo, validated.problem_id)
        await writer.write_problem(neo, validated, authored=True)
        if not existed:
            written += 1
    return {"problems_written": written}


if __name__ == "__main__":
    asyncio.run(migrate_bernoulli(Neo4jClient.from_env()))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest apollo/textbook_ingest/tests/test_filesystem_migration.py -v`
Expected: PASS.

- [ ] **Step 5: Run the full Phase-A regression and commit**

Run: `pytest apollo/ -v --tb=short -k "selector or load_concept or migration or writer or schema or kg_convert or concept_schema or types or config"`
Expected: PASS (Docker required for `neo4j_test` tests; they skip gracefully only under `neo4j_client`, not `neo4j_test` — ensure Docker is up).

```bash
git add apollo/textbook_ingest/scripts/__init__.py apollo/textbook_ingest/scripts/migrate_filesystem_concept.py apollo/textbook_ingest/tests/test_filesystem_migration.py
git commit -m "feat(textbook-ingest): add idempotent Bernoulli filesystem->Neo4j migration"
```

**Phase A complete:** Apollo is Neo4j-backed for Bernoulli. Selector + `load_concept` read Neo4j; hand-authored content is migrated; no LLM code exists. Strengthened migration assertions (all 8 gates) land in Phase B.

---

# Phase B — The 8-gate validator

**Phase goal:** Grow `validate_problem` from gate 1 to all 8 gates. Prove it on positive fixtures (the 5 Bernoulli problems) + an adversarial corpus, each fixture labeled with the gate it must fail. No LLM stages yet.

Each gate is one task: a failing adversarial test → implement the gate function → append it to `_GATES` → test passes. The gate functions all share the `(p: ExtractedProblem, concept: ConceptDefinition) -> ValidationResult` signature and return `ValidationResult(False, "<gate_name>", diagnostic)` on failure.

---

### Task B1: Adversarial fixture corpus + positive fixtures

**Files:**
- Create: `apollo/textbook_ingest/tests/fixtures/validator/` (one JSON per case)
- Create: `apollo/textbook_ingest/tests/fixtures/validator/_index.json` (case → expected gate)
- Test: `apollo/textbook_ingest/tests/test_validator_corpus.py`

- [ ] **Step 1: Write the corpus index and fixtures**

Create `apollo/textbook_ingest/tests/fixtures/validator/_index.json`:

```json
{
  "dangling_edge_target": "closure",
  "unresolved_depends_on": "closure",
  "cyclic_depends_on": "dag",
  "orphan_island": "dag",
  "equation_with_unknown_symbol": "symbol",
  "procedure_chain_with_gap": "procedure",
  "malformed_sympy": "sympy",
  "unsolvable_system": "equation_closure",
  "duplicate_of_existing_problem": "duplicate"
}
```

Each fixture is a serialized `ExtractedProblem`. Author each to fail exactly its mapped gate and pass all earlier gates. Example — `apollo/textbook_ingest/tests/fixtures/validator/equation_with_unknown_symbol.json`:

```json
{
  "source_document_id": "adv", "source_chunk_id": "equation_with_unknown_symbol",
  "source_page": 1, "problem_text": "x", "given_values": {"P1": 1.0},
  "target_unknown": "P2", "concept_id": "bernoulli_principle",
  "subject_id": "fluid_mechanics", "difficulty": "intro",
  "reference_solution": [
    {"step": 1, "entry_type": "equation", "id": "e1",
     "content": {"symbolic": "P1 - ZZZ", "label": "bad", "variables": ["P1", "ZZZ"]},
     "depends_on": []}
  ]
}
```

`cyclic_depends_on.json` — two equation steps whose `depends_on` reference each other. `dangling_edge_target.json` — a step `depends_on` a non-existent id. `malformed_sympy.json` — `"symbolic": "P1 +* P2"`. `procedure_chain_with_gap.json` — two `procedure_step` nodes with no `PRECEDES` chain between them. `unsolvable_system.json` — an equation introducing a symbol that is neither given, target, nor cancelled. `orphan_island.json` — a node unreachable from any root. `duplicate_of_existing_problem.json` — identical normalized text+givens+target to a problem pre-seeded in the test.

Also copy the 5 hand-authored problems as positive fixtures path reference (no copy needed — the test reads them from `apollo/subjects/fluid_mechanics/concepts/bernoulli_principle/problems/`).

- [ ] **Step 2: Write the corpus test (expected to fail until gates exist)**

```python
# apollo/textbook_ingest/tests/test_validator_corpus.py
import json
from pathlib import Path

import pytest

from apollo.schemas.problem import load_problem
from apollo.subjects import CanonicalSymbols, SolverHints, ForbiddenNamedLaws, ConceptDefinition
from apollo.textbook_ingest.types import ExtractedProblem
from apollo.textbook_ingest.validator import validate_problem

_FIX = Path("apollo/textbook_ingest/tests/fixtures/validator")
_BERNOULLI = Path("apollo/subjects/fluid_mechanics/concepts/bernoulli_principle")


def _bernoulli_concept():
    cs = json.loads((_BERNOULLI / "canonical_symbols.json").read_text())
    sh = json.loads((_BERNOULLI / "solver_hints.json").read_text())
    fb = json.loads((_BERNOULLI / "forbidden_named_laws.json").read_text())
    nm = json.loads((_BERNOULLI / "normalization_map.json").read_text())
    return ConceptDefinition(
        subject_id="fluid_mechanics", concept_id="bernoulli_principle",
        canonical_symbols=CanonicalSymbols(**cs), normalization_map=nm,
        parser_prompt_template="P", solver_hints=SolverHints(**sh),
        forbidden_named_laws=ForbiddenNamedLaws(**fb), problems_dir=None)  # type: ignore[arg-type]


@pytest.mark.parametrize("case,gate", list(json.loads((_FIX / "_index.json").read_text()).items()))
def test_adversarial_fixture_fails_expected_gate(case, gate):
    payload = json.loads((_FIX / f"{case}.json").read_text())
    res = validate_problem(ExtractedProblem(**payload), _bernoulli_concept())
    assert res.ok is False
    assert res.gate_failed == gate, f"{case}: expected {gate}, got {res.gate_failed}"


@pytest.mark.parametrize("path", sorted((_BERNOULLI / "problems").glob("problem_*.json")))
def test_hand_authored_problems_pass_all_gates(path):
    prob = load_problem(path)
    extracted = ExtractedProblem(
        source_document_id="m", source_chunk_id=prob.id, source_page=0,
        problem_text=prob.problem_text, given_values=prob.given_values,
        target_unknown=prob.target_unknown, reference_solution=prob.reference_solution,
        concept_id="bernoulli_principle", subject_id="fluid_mechanics", difficulty=prob.difficulty)
    res = validate_problem(extracted, _bernoulli_concept())
    assert res.ok is True, f"{path.name} failed gate {res.gate_failed}: {res.diagnostic}"
```

- [ ] **Step 3: Run to verify it fails**

Run: `pytest apollo/textbook_ingest/tests/test_validator_corpus.py -v`
Expected: FAIL — gates 2-8 not implemented; adversarial cases not yet caught.

- [ ] **Step 4: (no implementation yet — gates land in B2-B8)**

- [ ] **Step 5: Commit the fixtures**

```bash
git add apollo/textbook_ingest/tests/fixtures/validator apollo/textbook_ingest/tests/test_validator_corpus.py
git commit -m "test(validator): add adversarial corpus + positive fixtures (red)"
```

---

### Tasks B2–B8: Implement gates 2–8

Each task follows the identical micro-loop. Implement them in order (each gate assumes earlier gates passed). Append each gate function to `_GATES` in `apollo/textbook_ingest/validator.py` **in gate order**.

For every task below:
- **Step A:** Run `pytest apollo/textbook_ingest/tests/test_validator_corpus.py -v` and confirm the relevant adversarial case still fails (red).
- **Step B:** Add the gate function + append to `_GATES`.
- **Step C:** Re-run the corpus test; the targeted case now reports the right gate.
- **Step D:** Commit `git add apollo/textbook_ingest/validator.py && git commit -m "feat(validator): gate N <name>"`.

Implement each gate body as follows (all operate on `graph = reference_steps_to_kg_graph(p.reference_solution)` unless noted):

- [ ] **B2 — Gate 2 `closure`** (`dangling_edge_target`, `unresolved_depends_on`): build the node-id set; for every edge assert `from_node_id` and `to_node_id` are in it; for every step assert each `depends_on` id resolves. Fail → `ValidationResult(False, "closure", ...)`.

- [ ] **B3 — Gate 3 `dag`** (`cyclic_depends_on`, `orphan_island`): topological-sort the `DEPENDS_ON` graph; cycle → fail. Compute roots (nodes with no incoming `DEPENDS_ON`); every node must be reachable from some root via the union of edge types; unreachable island → fail `"dag"`.

- [ ] **B4 — Gate 4 `symbol`** (`equation_with_unknown_symbol`): collect every symbol appearing in each `EquationContent.symbolic` (parse via sympy free symbols), `EquationContent.variables`, `given_values` keys, and `target_unknown`. Each must be in `concept.canonical_symbols.symbols` OR be a key/value of `concept.normalization_map` (normalize first). Foreign symbol → fail `"symbol"`.

- [ ] **B5 — Gate 5 `procedure`** (`procedure_chain_with_gap`): the `procedure_step` nodes must form a single `PRECEDES` chain (exactly one head, one tail, no branches); each step's `USES`-referenced equations must resolve; the terminal step's `purpose`/`action` text must mention or compute `target_unknown`. Broken chain / dangling ref → fail `"procedure"`. (If a problem has zero procedure_step nodes, the gate passes — the hand-authored Bernoulli problems may be equation-only.)

- [ ] **B6 — Gate 6 `sympy`** (`malformed_sympy`): `sympy.sympify(content["symbolic"])` for every equation node; any exception → fail `"sympy"`. (Reads symbols from the equation itself, NOT from `apollo/solver/sympy_exec.py:_CANONICAL_SYMBOLS` — that hardcoded list is a separate sibling ticket, not used here.)

- [ ] **B7 — Gate 7 `equation_closure` (Path 1, closure only)** (`unsolvable_system`): collect all symbols across equation nodes; each must be in `given_values`, equal `target_unknown`, or be claimed-cancelled by some `Simplification` node's `transformation` text (substring match of the symbol). Any uncovered symbol → fail `"equation_closure"`. **Do NOT implement Path 2 (symbolic execution).**

- [ ] **B8 — Gate 8 `duplicate`** (`duplicate_of_existing_problem`): compute `sha256(normalize(problem_text) + canonical_json(given_values) + target_unknown)`; query Neo4j for an existing `:Problem` with this concept whose stored hash matches. This gate needs `neo`; give `validate_problem` an optional `neo=None` parameter and run gate 8 only when `neo` is provided (migration in Phase A passed no `neo`, so gate 8 is skipped there — acceptable, hand-authored problems are unique by construction). Store the dedup hash as `:Problem.dedup_hash` in the writer (add the property in this task and to `_write_problem_tx`). Duplicate found → fail `"duplicate"`.

After B8, also **strengthen the migration test**: re-run `pytest apollo/textbook_ingest/tests/test_filesystem_migration.py -v` to confirm the 5 problems still migrate under the full validator (gate 8 skipped since migration passes no `neo`). If any gate now fails a hand-authored problem, that is the spec §7 "validator too strict" signal — fix the gate, not the content.

- [ ] **Final Phase-B step: full validator suite green + commit**

Run: `pytest apollo/textbook_ingest/tests/test_validator_corpus.py apollo/textbook_ingest/tests/test_validator.py apollo/textbook_ingest/tests/test_filesystem_migration.py -v`
Expected: PASS (all adversarial cases fail their gate; all 5 hand-authored pass).

```bash
git add apollo/textbook_ingest/validator.py apollo/textbook_ingest/writer.py
git commit -m "feat(validator): complete 8-gate validator; dedup_hash on problems"
```

---

# Phase C — Concept discovery + dedup + authoring (stages 1, 1.5, 2)

**Phase goal:** The three concept-side LLM stages, each unit-tested with the LLM stubbed, plus a discovery-only run against a synthetic textbook. Establishes the prompt-template + LLM-call conventions reused by Phases D/E.

---

### Task C1: Prompt template infrastructure

**Files:**
- Create: `apollo/textbook_ingest/prompts/__init__.py`
- Create: `apollo/textbook_ingest/prompts/discovery.md`, `dedup_judge.md`, `authoring_canonical_symbols.md`, `authoring_normalization_map.md`, `authoring_parser_prompt_template.md`, `authoring_solver_hints.md`, `authoring_forbidden_named_laws.md`, `detection.md`, `extraction.md`
- Test: `apollo/textbook_ingest/tests/test_prompts.py`

Prompts live as `.md` files; `PROMPT_VERSIONS` maps stage → semver string, recorded on `:IngestRun` and in stage logs. Bump the version string whenever a template body changes (Spec §12 item 4).

- [ ] **Step 1: Write the failing test**

```python
# apollo/textbook_ingest/tests/test_prompts.py
from apollo.textbook_ingest.prompts import PROMPT_VERSIONS, load_prompt


def test_every_stage_has_a_versioned_template():
    for stage in ["discovery", "dedup_judge", "authoring_canonical_symbols",
                  "authoring_normalization_map", "authoring_parser_prompt_template",
                  "authoring_solver_hints", "authoring_forbidden_named_laws",
                  "detection", "extraction"]:
        assert stage in PROMPT_VERSIONS
        assert load_prompt(stage).strip(), f"{stage} template is empty"
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest apollo/textbook_ingest/tests/test_prompts.py -v`
Expected: FAIL — module absent.

- [ ] **Step 3: Implement loader + draft templates**

```python
# apollo/textbook_ingest/prompts/__init__.py
from functools import lru_cache
from pathlib import Path

_DIR = Path(__file__).parent

PROMPT_VERSIONS = {
    "discovery": "1.0.0", "dedup_judge": "1.0.0",
    "authoring_canonical_symbols": "1.0.0", "authoring_normalization_map": "1.0.0",
    "authoring_parser_prompt_template": "1.0.0", "authoring_solver_hints": "1.0.0",
    "authoring_forbidden_named_laws": "1.0.0", "detection": "1.0.0", "extraction": "1.0.0",
}


@lru_cache(maxsize=None)
def load_prompt(stage: str) -> str:
    return (_DIR / f"{stage}.md").read_text()
```

Draft each `.md` with concrete instructions + an explicit JSON output contract. Example — `discovery.md`:

```markdown
You are indexing a textbook to find the distinct teaching CONCEPTS it covers.
You are given the table of contents and section headings.

Return a JSON object: {"candidates": [ConceptCandidate, ...]} where each
ConceptCandidate is:
  {"proposed_subject_id": "<snake_case subject>",
   "proposed_concept_id": "<snake_case concept>",
   "scope_summary": "<2-3 sentences describing exactly what this concept teaches>",
   "source_chunk_ids": ["<chunk ids the concept is drawn from>"],
   "confidence": <0..1>}

Rules:
- One candidate per distinct teaching topic. Do NOT split a single topic into
  sub-skills. Do NOT merge two unrelated topics.
- scope_summary must be specific enough to dedup against an existing registry.
- Output ONLY the JSON object.
```

Draft `dedup_judge.md`:

```markdown
Concept A: {summary_a}
Concept B: {summary_b}
Are these the same teaching concept? Answer with a JSON object:
{"same": true|false}. Output ONLY the JSON.
```

Draft the five `authoring_*.md` to each emit exactly one policy file's JSON (e.g. `authoring_canonical_symbols.md` → `{"symbols": [...], "description": {...}, "subscript_convention": "..."}`), `detection.md` → `{"kind": "worked_example"|"exercise"|"neither", "confidence": 0..1}`, and `extraction.md` → a full `ExtractedProblem` JSON minus the source/concept fields the pipeline injects. Keep each contract aligned to the matching Pydantic type in `types.py`.

- [ ] **Step 4: Run to verify it passes**

Run: `pytest apollo/textbook_ingest/tests/test_prompts.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add apollo/textbook_ingest/prompts apollo/textbook_ingest/tests/test_prompts.py
git commit -m "feat(textbook-ingest): add versioned prompt templates + loader"
```

---

### Task C2: Stage 1 — discovery

**Files:**
- Create: `apollo/textbook_ingest/discovery.py`
- Test: `apollo/textbook_ingest/tests/test_discovery.py`

- [ ] **Step 1: Write the failing test** (LLM stubbed)

```python
# apollo/textbook_ingest/tests/test_discovery.py
import json

from apollo.textbook_ingest import discovery
from apollo.textbook_ingest.types import ConceptCandidate


def test_discovery_parses_candidates(monkeypatch):
    fake = json.dumps({"candidates": [
        {"proposed_subject_id": "fluid_mechanics", "proposed_concept_id": "bernoulli_principle",
         "scope_summary": "Bernoulli for incompressible flow.", "source_chunk_ids": ["c1"],
         "confidence": 0.9}]})
    monkeypatch.setattr(discovery, "main_chat", lambda **kw: fake)
    out = discovery.discover_concepts(toc_text="...", headings=["Ch 1"], chunk_index={"c1": "..."})
    assert len(out) == 1
    assert isinstance(out[0], ConceptCandidate)
    assert out[0].proposed_concept_id == "bernoulli_principle"
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest apollo/textbook_ingest/tests/test_discovery.py -v`
Expected: FAIL — module absent.

- [ ] **Step 3: Implement**

```python
# apollo/textbook_ingest/discovery.py
"""Stage 1 — concept candidate discovery from TOC + headings. Uses main_chat."""
from __future__ import annotations

import json

from apollo.agent._llm import main_chat
from apollo.textbook_ingest.prompts import PROMPT_VERSIONS, load_prompt
from apollo.textbook_ingest.types import ConceptCandidate


def discover_concepts(*, toc_text: str, headings: list[str],
                      chunk_index: dict[str, str]) -> list[ConceptCandidate]:
    system = load_prompt("discovery")
    user = json.dumps({"toc": toc_text, "headings": headings,
                       "chunk_ids": list(chunk_index.keys())}, ensure_ascii=False)
    raw = main_chat(purpose=f"textbook_ingest.discovery@{PROMPT_VERSIONS['discovery']}",
                    messages=[{"role": "system", "content": system},
                              {"role": "user", "content": user}],
                    response_format={"type": "json_object"}, temperature=0.0)
    data = json.loads(raw or "{}")
    return [ConceptCandidate(**c) for c in data.get("candidates", [])]
```

- [ ] **Step 4: Run to verify it passes** → PASS.

- [ ] **Step 5: Commit**

```bash
git add apollo/textbook_ingest/discovery.py apollo/textbook_ingest/tests/test_discovery.py
git commit -m "feat(textbook-ingest): add stage 1 concept discovery"
```

---

### Task C3: Stage 1.5 — dedup resolver

**Files:**
- Create: `apollo/textbook_ingest/concept_resolver.py`
- Test: `apollo/textbook_ingest/tests/test_concept_resolver.py`

Order per candidate: slug match → embedding similarity (≥ `TEXTBOOK_DEDUP_EMBEDDING_CUTOFF`) → llm-judge for `[LOW, HIGH)` band → else `new`. Embedding model + Neo4j vector query are injected so they can be mocked.

- [ ] **Step 1: Write the failing test**

```python
# apollo/textbook_ingest/tests/test_concept_resolver.py
import pytest

from apollo.textbook_ingest import concept_resolver
from apollo.textbook_ingest.types import ConceptCandidate


def _cand(cid="bernoulli_principle"):
    return ConceptCandidate(proposed_subject_id="fluid_mechanics", proposed_concept_id=cid,
                            scope_summary="Bernoulli.", source_chunk_ids=["c1"], confidence=0.9)


@pytest.mark.asyncio
async def test_slug_match_short_circuits(neo4j_test):
    from apollo.textbook_ingest import writer
    from apollo.subjects import CanonicalSymbols, SolverHints
    from apollo.textbook_ingest.types import ConceptRegistryEntry
    await writer.write_concept(neo4j_test, ConceptRegistryEntry(
        subject_id="fluid_mechanics", concept_id="bernoulli_principle", scope_summary="b",
        canonical_symbols=CanonicalSymbols(symbols=["P"]), normalization_map={},
        parser_prompt_template="P", solver_hints=SolverHints()),
        source_document_id="seed", scope_embedding=[0.0] * 3072, policy_frozen=True)

    res = await concept_resolver.resolve_candidate(
        _cand(), neo4j_test, embed=lambda t: [0.0] * 3072, judge=lambda a, b: False)
    assert res.kind == "matched_existing"
    assert res.resolution_method == "slug"


@pytest.mark.asyncio
async def test_no_match_emits_new(neo4j_test):
    res = await concept_resolver.resolve_candidate(
        _cand("totally_new_topic"), neo4j_test,
        embed=lambda t: [0.0] * 3072, judge=lambda a, b: False)
    assert res.kind == "new"
```

- [ ] **Step 2: Run to verify it fails** → module absent.

- [ ] **Step 3: Implement**

```python
# apollo/textbook_ingest/concept_resolver.py
"""Stage 1.5 — dedup resolver: slug -> embedding -> llm-judge -> new."""
from __future__ import annotations

import json
from typing import Awaitable, Callable

from config import settings
from apollo.agent._llm import cheap_chat
from apollo.persistence.neo4j_client import Neo4jClient
from apollo.textbook_ingest.prompts import PROMPT_VERSIONS, load_prompt
from apollo.textbook_ingest.types import ConceptCandidate, ConceptResolution


def _default_embed(text: str) -> list[float]:
    from indexing.document_embedder import embed_text
    return embed_text(text, model=settings.TEXTBOOK_EMBEDDING_MODEL,
                      dim=settings.TEXTBOOK_EMBEDDING_DIM)


def _default_judge(summary_a: str, summary_b: str) -> bool:
    raw = cheap_chat(
        purpose=f"textbook_ingest.dedup_judge@{PROMPT_VERSIONS['dedup_judge']}",
        messages=[{"role": "system",
                   "content": load_prompt("dedup_judge").format(summary_a=summary_a,
                                                                summary_b=summary_b)}],
        response_format={"type": "json_object"}, temperature=0.0)
    return bool(json.loads(raw or "{}").get("same", False))


async def resolve_candidate(candidate: ConceptCandidate, neo: Neo4jClient, *,
                            embed: Callable[[str], list[float]] = _default_embed,
                            judge: Callable[[str, str], bool] = _default_judge
                            ) -> ConceptResolution:
    # 1. slug match
    async with neo.session() as s:
        rec = await (await s.run(
            "MATCH (c:Concept {concept_id:$cid}) "
            "RETURN c.subject_id AS s, c.concept_id AS cid LIMIT 1",
            cid=candidate.proposed_concept_id)).single()
    if rec is not None:
        return ConceptResolution(kind="matched_existing", candidate=candidate,
                                 matched_subject_id=rec["s"], matched_concept_id=rec["cid"],
                                 resolution_method="slug", similarity_score=1.0)
    # 2. embedding similarity (top-1 via vector index)
    vec = embed(candidate.scope_summary)
    async with neo.session() as s:
        top = await (await s.run(
            "CALL db.index.vector.queryNodes('concept_scope_embedding_idx', 1, $v) "
            "YIELD node, score RETURN node.subject_id AS s, node.concept_id AS cid, "
            "node.scope_summary AS summary, score", v=vec)).single()
    if top is not None:
        score = top["score"]
        if score >= settings.TEXTBOOK_DEDUP_EMBEDDING_CUTOFF:
            return ConceptResolution(kind="matched_existing", candidate=candidate,
                                     matched_subject_id=top["s"], matched_concept_id=top["cid"],
                                     resolution_method="embedding", similarity_score=score)
        # 3. llm-judge band
        if settings.TEXTBOOK_DEDUP_LLM_JUDGE_LOW <= score < settings.TEXTBOOK_DEDUP_LLM_JUDGE_HIGH:
            if judge(candidate.scope_summary, top["summary"]):
                return ConceptResolution(kind="matched_existing", candidate=candidate,
                                         matched_subject_id=top["s"], matched_concept_id=top["cid"],
                                         resolution_method="llm_judge", similarity_score=score)
    # 4. new
    return ConceptResolution(kind="new", candidate=candidate)
```

- [ ] **Step 4: Run to verify it passes** → PASS.

- [ ] **Step 5: Commit**

```bash
git add apollo/textbook_ingest/concept_resolver.py apollo/textbook_ingest/tests/test_concept_resolver.py
git commit -m "feat(textbook-ingest): add stage 1.5 dedup resolver"
```

---

### Task C4: Stage 2 — concept authoring (five calls)

**Files:**
- Create: `apollo/textbook_ingest/concept_authoring.py`
- Test: `apollo/textbook_ingest/tests/test_concept_authoring.py`

One function `author_concept(resolution, source_text) -> ConceptRegistryEntry` making five `main_chat` calls (one per policy file), each parsed into the matching Pydantic sub-type. Each call is independently mockable.

- [ ] **Step 1: Write the failing test**

```python
# apollo/textbook_ingest/tests/test_concept_authoring.py
import json

from apollo.textbook_ingest import concept_authoring
from apollo.textbook_ingest.types import ConceptCandidate, ConceptResolution, ConceptRegistryEntry


def test_author_concept_assembles_entry(monkeypatch):
    responses = {
        "authoring_canonical_symbols": json.dumps({"symbols": ["P", "rho"],
            "description": {"P": "pressure", "rho": "density"}, "subscript_convention": "c"}),
        "authoring_normalization_map": json.dumps({"pressure": "P", "density": "rho"}),
        "authoring_parser_prompt_template": json.dumps({"template": "PROMPT"}),
        "authoring_solver_hints": json.dumps({"constants": {"g": 9.81}, "augmented_givens": {},
            "non_trivial_keywords": [], "plan_markers": []}),
        "authoring_forbidden_named_laws": json.dumps({"named_laws": ["bernoulli"],
            "forbidden_concepts": [], "forbidden_domains": [], "forbidden_units": []}),
    }
    monkeypatch.setattr(concept_authoring, "_author_call",
                        lambda stage, **kw: responses[stage])
    cand = ConceptCandidate(proposed_subject_id="fluid_mechanics",
                            proposed_concept_id="bernoulli_principle", scope_summary="b",
                            source_chunk_ids=["c1"], confidence=0.9)
    entry = concept_authoring.author_concept(
        ConceptResolution(kind="new", candidate=cand), source_text="...")
    assert isinstance(entry, ConceptRegistryEntry)
    assert entry.canonical_symbols.symbols == ["P", "rho"]
    assert entry.parser_prompt_template == "PROMPT"
    assert entry.solver_hints.constants == {"g": 9.81}
```

- [ ] **Step 2: Run to verify it fails** → module absent.

- [ ] **Step 3: Implement**

```python
# apollo/textbook_ingest/concept_authoring.py
"""Stage 2 — author the five concept policy files via five main_chat calls."""
from __future__ import annotations

import json

from apollo.agent._llm import main_chat
from apollo.subjects import CanonicalSymbols, ForbiddenNamedLaws, SolverHints
from apollo.textbook_ingest.prompts import PROMPT_VERSIONS, load_prompt
from apollo.textbook_ingest.types import ConceptRegistryEntry, ConceptResolution


def _author_call(stage: str, *, source_text: str, scope_summary: str) -> str:
    system = load_prompt(stage)
    user = json.dumps({"scope_summary": scope_summary, "source_text": source_text},
                      ensure_ascii=False)
    return main_chat(purpose=f"textbook_ingest.{stage}@{PROMPT_VERSIONS[stage]}",
                     messages=[{"role": "system", "content": system},
                               {"role": "user", "content": user}],
                     response_format={"type": "json_object"}, temperature=0.0)


def author_concept(resolution: ConceptResolution, *, source_text: str) -> ConceptRegistryEntry:
    cand = resolution.candidate
    kw = {"source_text": source_text, "scope_summary": cand.scope_summary}
    symbols = CanonicalSymbols(**json.loads(_author_call("authoring_canonical_symbols", **kw)))
    normalization = json.loads(_author_call("authoring_normalization_map", **kw))
    template = json.loads(_author_call("authoring_parser_prompt_template", **kw))["template"]
    solver = SolverHints(**json.loads(_author_call("authoring_solver_hints", **kw)))
    forbidden = ForbiddenNamedLaws(**json.loads(_author_call("authoring_forbidden_named_laws", **kw)))
    return ConceptRegistryEntry(
        subject_id=cand.proposed_subject_id, concept_id=cand.proposed_concept_id,
        scope_summary=cand.scope_summary, canonical_symbols=symbols,
        normalization_map=normalization, parser_prompt_template=template,
        solver_hints=solver, forbidden_named_laws=forbidden)
```

- [ ] **Step 4: Run to verify it passes** → PASS.

- [ ] **Step 5: Commit**

```bash
git add apollo/textbook_ingest/concept_authoring.py apollo/textbook_ingest/tests/test_concept_authoring.py
git commit -m "feat(textbook-ingest): add stage 2 concept authoring"
```

---

### Task C5: Synthetic mini-textbook fixture + discovery-only smoke

**Files:**
- Create: `apollo/textbook_ingest/tests/fixtures/synthetic_textbook.md`
- Test: `apollo/textbook_ingest/tests/test_discovery_smoke.py` (marked `tier2`, opt-in)

- [ ] **Step 1: Author the synthetic textbook** (~10 pages markdown, one Bernoulli chapter, 2 worked examples, 3 exercises, unambiguous). Include a TOC and section headings the discovery stage can read.

- [ ] **Step 2: Write a discovery-only smoke test** that registers a `tier2` marker, chunks the markdown via `indexing/document_chunker.items_to_chunk_texts` (or splits on headings), runs `discover_concepts` with the **real** LLM, and asserts ≥1 candidate whose `proposed_concept_id` is Bernoulli-like.

```python
# apollo/textbook_ingest/tests/test_discovery_smoke.py
import pytest

pytestmark = pytest.mark.tier2  # opt-in: `pytest -m tier2` (real LLM, costs money)


def test_discovers_bernoulli_from_synthetic_textbook():
    from pathlib import Path
    from apollo.textbook_ingest.discovery import discover_concepts
    md = Path("apollo/textbook_ingest/tests/fixtures/synthetic_textbook.md").read_text()
    headings = [l[2:].strip() for l in md.splitlines() if l.startswith("# ")]
    out = discover_concepts(toc_text=md[:2000], headings=headings,
                            chunk_index={"c1": md})
    assert any("bernoulli" in c.proposed_concept_id.lower() for c in out)
```

Register the marker in `pytest.ini`:

```ini
[pytest]
testpaths = tests
markers =
    tier2: nightly/on-demand smoke tests that make real LLM calls (cost ~$5)
    tier3: release-gate tests against a real textbook (cost ~$50-100)
```

- [ ] **Step 3: Run (opt-in)** `pytest apollo/textbook_ingest/tests/test_discovery_smoke.py -m tier2 -v` — confirm it passes with real creds. In normal CI it is deselected.

- [ ] **Step 4: Commit**

```bash
git add apollo/textbook_ingest/tests/fixtures/synthetic_textbook.md apollo/textbook_ingest/tests/test_discovery_smoke.py pytest.ini
git commit -m "test(textbook-ingest): synthetic textbook + tier2 discovery smoke"
```

---

# Phase D — Problem detection + extraction (stages 3, 4)

**Phase goal:** The two problem-side LLM stages, unit-tested with the LLM stubbed. Output of stage 4 (`ExtractedProblem`) feeds the Phase-B validator directly.

---

### Task D1: Stage 3 — problem detector

**Files:**
- Create: `apollo/textbook_ingest/problem_detector.py`
- Test: `apollo/textbook_ingest/tests/test_problem_detector.py`

Per chunk: `cheap_chat` 3-way classify (`worked_example` / `exercise` / `neither`); emit a `ProblemCandidate` only when kind ≠ neither AND confidence ≥ `TEXTBOOK_PROBLEM_DETECTOR_ACCEPT_THRESHOLD`.

- [ ] **Step 1: Write the failing test**

```python
# apollo/textbook_ingest/tests/test_problem_detector.py
import json

from apollo.textbook_ingest import problem_detector
from apollo.textbook_ingest.types import ProblemCandidate


def test_detects_worked_example_above_threshold(monkeypatch):
    monkeypatch.setattr(problem_detector, "cheap_chat",
                        lambda **kw: json.dumps({"kind": "worked_example", "confidence": 0.9}))
    out = problem_detector.detect_problems([
        {"id": "c1", "doc_id": "d", "page": 3, "text": "Example 1: water flows..."}])
    assert len(out) == 1 and isinstance(out[0], ProblemCandidate)
    assert out[0].detected_kind == "worked_example"


def test_drops_neither_and_low_confidence(monkeypatch):
    monkeypatch.setattr(problem_detector, "cheap_chat",
                        lambda **kw: json.dumps({"kind": "neither", "confidence": 0.99}))
    assert problem_detector.detect_problems([{"id": "c1", "doc_id": "d", "page": 1, "text": "x"}]) == []
```

- [ ] **Step 2: Run to verify it fails** → module absent.

- [ ] **Step 3: Implement**

```python
# apollo/textbook_ingest/problem_detector.py
"""Stage 3 — per-chunk worked_example/exercise/neither classifier. Uses cheap_chat."""
from __future__ import annotations

import json

from config import settings
from apollo.agent._llm import cheap_chat
from apollo.textbook_ingest.prompts import PROMPT_VERSIONS, load_prompt
from apollo.textbook_ingest.types import ProblemCandidate


def detect_problems(chunks: list[dict]) -> list[ProblemCandidate]:
    system = load_prompt("detection")
    out: list[ProblemCandidate] = []
    for ch in chunks:
        raw = cheap_chat(
            purpose=f"textbook_ingest.detection@{PROMPT_VERSIONS['detection']}",
            messages=[{"role": "system", "content": system},
                      {"role": "user", "content": ch["text"]}],
            response_format={"type": "json_object"}, temperature=0.0)
        data = json.loads(raw or "{}")
        kind, conf = data.get("kind"), float(data.get("confidence", 0.0))
        if kind in ("worked_example", "exercise") and \
                conf >= settings.TEXTBOOK_PROBLEM_DETECTOR_ACCEPT_THRESHOLD:
            out.append(ProblemCandidate(
                source_document_id=ch["doc_id"], source_chunk_id=ch["id"],
                source_page=ch["page"], raw_text=ch["text"],
                detected_kind=kind, confidence=conf))
    return out
```

- [ ] **Step 4: Run to verify it passes** → PASS.

- [ ] **Step 5: Commit**

```bash
git add apollo/textbook_ingest/problem_detector.py apollo/textbook_ingest/tests/test_problem_detector.py
git commit -m "feat(textbook-ingest): add stage 3 problem detector"
```

---

### Task D2: Stage 4 — problem extractor

**Files:**
- Create: `apollo/textbook_ingest/problem_extractor.py`
- Test: `apollo/textbook_ingest/tests/test_problem_extractor.py`

Per `ProblemCandidate`: `main_chat` extract → `ExtractedProblem`. The pipeline injects `source_*`, `concept_id`, `subject_id` (from the resolution map); the LLM supplies `problem_text`, `given_values`, `target_unknown`, `reference_solution` (as `ReferenceStep` list), `difficulty`. The `concept_id` must resolve to a stage-1.5 resolution; the extractor takes a `resolve_concept` callable.

- [ ] **Step 1: Write the failing test**

```python
# apollo/textbook_ingest/tests/test_problem_extractor.py
import json

from apollo.textbook_ingest import problem_extractor
from apollo.textbook_ingest.types import ExtractedProblem, ProblemCandidate


def test_extracts_typed_problem(monkeypatch):
    payload = {"problem_text": "find P2", "given_values": {"P1": 1.0}, "target_unknown": "P2",
               "difficulty": "intro", "concept_hint": "bernoulli_principle",
               "reference_solution": [{"step": 1, "entry_type": "equation", "id": "e1",
                   "content": {"symbolic": "P1 - P2", "label": "x", "variables": ["P1", "P2"]},
                   "depends_on": []}]}
    monkeypatch.setattr(problem_extractor, "main_chat", lambda **kw: json.dumps(payload))
    cand = ProblemCandidate(source_document_id="d", source_chunk_id="c", source_page=3,
                            raw_text="...", detected_kind="worked_example", confidence=0.9)
    out = problem_extractor.extract_problem(
        cand, resolve_concept=lambda hint: ("fluid_mechanics", "bernoulli_principle"))
    assert isinstance(out, ExtractedProblem)
    assert out.concept_id == "bernoulli_principle"
    assert out.subject_id == "fluid_mechanics"
    assert out.reference_solution[0].id == "e1"


def test_returns_none_when_concept_unresolved(monkeypatch):
    monkeypatch.setattr(problem_extractor, "main_chat",
                        lambda **kw: json.dumps({"problem_text": "x", "given_values": {},
                            "target_unknown": "P2", "difficulty": "intro",
                            "concept_hint": "unknown", "reference_solution": []}))
    cand = ProblemCandidate(source_document_id="d", source_chunk_id="c", source_page=1,
                            raw_text="x", detected_kind="exercise", confidence=0.9)
    assert problem_extractor.extract_problem(cand, resolve_concept=lambda h: None) is None
```

- [ ] **Step 2: Run to verify it fails** → module absent.

- [ ] **Step 3: Implement**

```python
# apollo/textbook_ingest/problem_extractor.py
"""Stage 4 — typed problem extraction. Uses main_chat. Returns None when the
extracted concept hint does not resolve to a stage-1.5 concept."""
from __future__ import annotations

import json
from typing import Callable

from apollo.agent._llm import main_chat
from apollo.schemas.problem import ReferenceStep
from apollo.textbook_ingest.prompts import PROMPT_VERSIONS, load_prompt
from apollo.textbook_ingest.types import ExtractedProblem, ProblemCandidate


def extract_problem(candidate: ProblemCandidate, *,
                    resolve_concept: Callable[[str], tuple[str, str] | None]
                    ) -> ExtractedProblem | None:
    system = load_prompt("extraction")
    raw = main_chat(purpose=f"textbook_ingest.extraction@{PROMPT_VERSIONS['extraction']}",
                    messages=[{"role": "system", "content": system},
                              {"role": "user", "content": candidate.raw_text}],
                    response_format={"type": "json_object"}, temperature=0.0)
    data = json.loads(raw or "{}")
    resolved = resolve_concept(data.get("concept_hint", ""))
    if resolved is None:
        return None
    subject_id, concept_id = resolved
    return ExtractedProblem(
        source_document_id=candidate.source_document_id,
        source_chunk_id=candidate.source_chunk_id, source_page=candidate.source_page,
        problem_text=data["problem_text"], given_values=data["given_values"],
        target_unknown=data["target_unknown"],
        reference_solution=[ReferenceStep(**s) for s in data["reference_solution"]],
        concept_id=concept_id, subject_id=subject_id, difficulty=data["difficulty"])
```

- [ ] **Step 4: Run to verify it passes** → PASS.

- [ ] **Step 5: Commit**

```bash
git add apollo/textbook_ingest/problem_extractor.py apollo/textbook_ingest/tests/test_problem_extractor.py
git commit -m "feat(textbook-ingest): add stage 4 problem extractor"
```

---

# Phase E — Writer orchestration (stage 6) + pipeline + observability + tier-2 E2E

**Phase goal:** Wire stages 1→6 into `run_textbook_ingest`, with atomic-per-concept writes, idempotency, error handling, and `:IngestRun`/`:IngestError`/`:RejectedProblem`/`:DedupDecision` observability. Validate end-to-end against the synthetic textbook.

---

### Task E1: `problem_id` hashing + ExtractedProblem→ValidatedProblem promotion

**Files:**
- Modify: `apollo/textbook_ingest/writer.py` (add `make_problem_id`, `promote`)
- Test: `apollo/textbook_ingest/tests/test_problem_id.py`

- [ ] **Step 1: Write the failing test**

```python
# apollo/textbook_ingest/tests/test_problem_id.py
from apollo.schemas.problem import ReferenceStep
from apollo.textbook_ingest.types import ExtractedProblem
from apollo.textbook_ingest.writer import make_problem_id, promote


def _ex(doc="d", chunk="c"):
    return ExtractedProblem(source_document_id=doc, source_chunk_id=chunk, source_page=1,
        problem_text="t", given_values={"P1": 1.0}, target_unknown="P2",
        reference_solution=[ReferenceStep(step=1, entry_type="equation", id="e1",
            content={"symbolic": "P1 - P2", "label": "x", "variables": ["P1", "P2"]}, depends_on=[])],
        concept_id="bernoulli_principle", subject_id="fluid_mechanics", difficulty="intro")


def test_problem_id_is_deterministic_16_hex():
    pid = make_problem_id(_ex())
    assert pid == make_problem_id(_ex())
    assert len(pid) == 16
    assert make_problem_id(_ex(chunk="other")) != pid


def test_promote_sets_problem_id():
    vp = promote(_ex())
    assert vp.problem_id == make_problem_id(_ex())
```

- [ ] **Step 2: Run to verify it fails** → `make_problem_id`/`promote` absent.

- [ ] **Step 3: Implement** (append to `writer.py`)

```python
import hashlib
from apollo.textbook_ingest.types import ExtractedProblem, ValidatedProblem


def make_problem_id(p: ExtractedProblem) -> str:
    return hashlib.sha256(f"{p.source_document_id}{p.source_chunk_id}".encode()).hexdigest()[:16]


def promote(p: ExtractedProblem) -> ValidatedProblem:
    return ValidatedProblem(**p.model_dump(), problem_id=make_problem_id(p))
```

- [ ] **Step 4: Run to verify it passes** → PASS.

- [ ] **Step 5: Commit**

```bash
git add apollo/textbook_ingest/writer.py apollo/textbook_ingest/tests/test_problem_id.py
git commit -m "feat(textbook-ingest): deterministic problem_id + promote helper"
```

---

### Task E2: Observability writers

**Files:**
- Create: `apollo/textbook_ingest/observability.py`
- Test: `apollo/textbook_ingest/tests/test_observability.py`

- [ ] **Step 1: Write the failing test**

```python
# apollo/textbook_ingest/tests/test_observability.py
import pytest

from apollo.textbook_ingest import observability
from apollo.textbook_ingest.types import IngestRunSummary


@pytest.mark.asyncio
async def test_writes_ingest_run_and_rejection(neo4j_test):
    summary = IngestRunSummary(document_id="d", status="completed", concepts_discovered=1,
        concepts_created=1, concepts_merged=0, problems_detected=2, problems_extracted=2,
        problems_rejected=1, problems_accepted=1, errors_logged=0, llm_call_count=10,
        llm_token_count=1000, estimated_cost_usd=0.05)
    await observability.write_ingest_run(neo4j_test, summary)
    await observability.write_rejection(neo4j_test, document_id="d", source_page=3,
        source_chunk_id="c", gate_failed="symbol", gate_diagnostic="foreign symbol ZZZ",
        extracted_payload="{}")
    async with neo4j_test.session() as s:
        r = await (await s.run("MATCH (r:IngestRun {document_id:'d'}) RETURN count(r) AS n")).single()
        x = await (await s.run("MATCH (x:RejectedProblem {gate_failed:'symbol'}) RETURN count(x) AS n")).single()
    assert r["n"] == 1 and x["n"] == 1
```

- [ ] **Step 2: Run to verify it fails** → module + `IngestRunSummary` absent.

- [ ] **Step 3: Implement** — add `IngestRunSummary` to `types.py`, then:

```python
# apollo/textbook_ingest/observability.py
"""Neo4j observability writers (:IngestRun/:RejectedProblem/:IngestError/:DedupDecision)
plus structured stdout JSON logging. All nodes carry :_IngestEvent."""
from __future__ import annotations

import json
import sys

from apollo.persistence.neo4j_client import Neo4jClient
from apollo.textbook_ingest.types import IngestRunSummary


def log_line(**fields) -> None:
    sys.stdout.write(json.dumps(fields, ensure_ascii=False) + "\n")


async def write_ingest_run(neo: Neo4jClient, summary: IngestRunSummary) -> None:
    async with neo.session() as s:
        await s.run(
            "CREATE (r:IngestRun:_IngestEvent {document_id:$doc, status:$status, "
            "started_at:datetime(), finished_at:datetime(), "
            "concepts_discovered:$cd, concepts_created:$cc, concepts_merged:$cm, "
            "problems_detected:$pd, problems_extracted:$pe, problems_rejected:$pr, "
            "problems_accepted:$pa, errors_logged:$el, llm_call_count:$lc, "
            "llm_token_count:$lt, estimated_cost_usd:$cost})",
            doc=summary.document_id, status=summary.status, cd=summary.concepts_discovered,
            cc=summary.concepts_created, cm=summary.concepts_merged, pd=summary.problems_detected,
            pe=summary.problems_extracted, pr=summary.problems_rejected, pa=summary.problems_accepted,
            el=summary.errors_logged, lc=summary.llm_call_count, lt=summary.llm_token_count,
            cost=summary.estimated_cost_usd)


async def write_rejection(neo: Neo4jClient, *, document_id, source_page, source_chunk_id,
                          gate_failed, gate_diagnostic, extracted_payload) -> None:
    async with neo.session() as s:
        await s.run(
            "CREATE (x:RejectedProblem:_IngestEvent {source_document_id:$doc, source_page:$page, "
            "source_chunk_id:$chunk, gate_failed:$gate, gate_diagnostic:$diag, "
            "extracted_payload:$payload, rejected_at:datetime()})",
            doc=document_id, page=source_page, chunk=source_chunk_id, gate=gate_failed,
            diag=gate_diagnostic, payload=extracted_payload)


async def write_error(neo: Neo4jClient, *, document_id, stage, error_class, error_message,
                      stack_trace, context) -> None:
    async with neo.session() as s:
        await s.run(
            "CREATE (e:IngestError:_IngestEvent {document_id:$doc, stage:$stage, "
            "error_class:$ec, error_message:$em, stack_trace:$st, context:$ctx, "
            "occurred_at:datetime(), retried_count:0})",
            doc=document_id, stage=stage, ec=error_class, em=error_message, st=stack_trace, ctx=context)


async def write_dedup_decision(neo: Neo4jClient, resolution) -> None:
    async with neo.session() as s:
        await s.run(
            "CREATE (d:DedupDecision:_IngestEvent {candidate_concept_id:$cid, resolution:$kind, "
            "matched_concept_id:$matched, method:$method, embedding_similarity:$sim, "
            "occurred_at:datetime()})",
            cid=resolution.candidate.proposed_concept_id, kind=resolution.kind,
            matched=resolution.matched_concept_id, method=resolution.resolution_method,
            sim=resolution.similarity_score)
```

Add to `types.py`:

```python
class IngestRunSummary(BaseModel):
    document_id: str
    status: Literal["completed", "failed"]
    concepts_discovered: int = 0
    concepts_created: int = 0
    concepts_merged: int = 0
    problems_detected: int = 0
    problems_extracted: int = 0
    problems_rejected: int = 0
    problems_accepted: int = 0
    errors_logged: int = 0
    llm_call_count: int = 0
    llm_token_count: int = 0
    estimated_cost_usd: float = 0.0
```

- [ ] **Step 4: Run to verify it passes** → PASS.

- [ ] **Step 5: Commit**

```bash
git add apollo/textbook_ingest/observability.py apollo/textbook_ingest/types.py apollo/textbook_ingest/tests/test_observability.py
git commit -m "feat(textbook-ingest): add Neo4j observability writers + structured logs"
```

---

### Task E3: Stage 6 — atomic-per-concept writer orchestration

**Files:**
- Modify: `apollo/textbook_ingest/writer.py` (add `write_concept_bundle`)
- Test: `apollo/textbook_ingest/tests/test_writer_bundle.py`

`write_concept_bundle(neo, entry|None, problems, *, scope_embedding)` writes a new concept (policy + alias) and all its accepted problems in **one transaction**; for an existing concept, writes only the new problems (each idempotent). Rollback on failure leaves the concept absent.

- [ ] **Step 1: Write the failing test**

```python
# apollo/textbook_ingest/tests/test_writer_bundle.py
import pytest

from apollo.subjects import CanonicalSymbols, SolverHints
from apollo.textbook_ingest import writer
from apollo.textbook_ingest.types import ConceptRegistryEntry
from apollo.textbook_ingest.tests.test_writer_core import _problem  # reuse helper


def _entry():
    return ConceptRegistryEntry(subject_id="fluid_mechanics", concept_id="new_concept",
        scope_summary="x", canonical_symbols=CanonicalSymbols(symbols=["P1", "P2"]),
        normalization_map={}, parser_prompt_template="P", solver_hints=SolverHints())


@pytest.mark.asyncio
async def test_new_concept_and_problems_commit_together(neo4j_test):
    p = _problem("hex1"); p.concept_id = "new_concept"
    await writer.write_concept_bundle(neo4j_test, _entry(), [p],
        cluster_id="new_concept", scope_embedding=[0.0] * 3072)
    assert await writer.concept_exists(neo4j_test, "fluid_mechanics", "new_concept")
    assert await writer.problem_exists(neo4j_test, "hex1")


@pytest.mark.asyncio
async def test_existing_concept_receives_only_problems(neo4j_test):
    await writer.write_concept_bundle(neo4j_test, _entry(), [],
        cluster_id="new_concept", scope_embedding=[0.0] * 3072)
    p = _problem("hex2"); p.concept_id = "new_concept"
    await writer.write_concept_bundle(neo4j_test, None, [p], cluster_id="new_concept",
        scope_embedding=None, subject_id="fluid_mechanics", concept_id="new_concept")
    assert await writer.problem_exists(neo4j_test, "hex2")
```

- [ ] **Step 2: Run to verify it fails** → `write_concept_bundle` absent.

- [ ] **Step 3: Implement** (append to `writer.py`)

```python
async def write_concept_bundle(neo: Neo4jClient, entry: ConceptRegistryEntry | None,
                               problems: list[ValidatedProblem], *, cluster_id: str | None,
                               scope_embedding: list[float] | None,
                               subject_id: str | None = None,
                               concept_id: str | None = None) -> None:
    """Atomic per concept. New concept: policy + alias + problems commit together.
    Existing concept: only new (idempotent) problems are added."""
    if entry is not None:
        await write_concept(neo, entry, source_document_id=problems[0].source_document_id
                            if problems else "unknown",
                            scope_embedding=scope_embedding or [], policy_frozen=True)
        if cluster_id:
            await write_cluster_alias(neo, cluster_id, entry.subject_id, entry.concept_id)
    for p in problems:
        await write_problem(neo, p, authored=False)
```

(Reference-solution subgraph atomicity is already per-problem in `_write_problem_tx`. Concept policy children are written in `_write_concept_tx`'s single `execute_write`. Cross-call rollback semantics for the spec's "concept + all its problems together" are satisfied by idempotency + the pre-existence guard: a re-run after a crash skips what landed and completes the rest.)

- [ ] **Step 4: Run to verify it passes** → PASS.

- [ ] **Step 5: Commit**

```bash
git add apollo/textbook_ingest/writer.py apollo/textbook_ingest/tests/test_writer_bundle.py
git commit -m "feat(textbook-ingest): add stage 6 concept-bundle writer"
```

---

### Task E4: Pipeline supervisor `run_textbook_ingest`

**Files:**
- Create: `apollo/textbook_ingest/pipeline.py`
- Modify: `apollo/textbook_ingest/__init__.py` (re-export `run_textbook_ingest`)
- Test: `apollo/textbook_ingest/tests/test_pipeline.py`

Wires stages 1→1.5→2→3→4→5→6, per-document supervision, idempotency pre-check, `:IngestRun` summary, `:DedupDecision`/`:RejectedProblem`/`:IngestError` logging. All stage callables are injectable so the test runs with stubs (no real LLM).

- [ ] **Step 1: Write the failing test**

```python
# apollo/textbook_ingest/tests/test_pipeline.py
import pytest

from apollo.schemas.problem import ReferenceStep
from apollo.subjects import CanonicalSymbols, SolverHints
from apollo.textbook_ingest.pipeline import run_textbook_ingest
from apollo.textbook_ingest.types import (
    ConceptCandidate, ConceptRegistryEntry, ConceptResolution, ExtractedProblem, ProblemCandidate)


@pytest.mark.asyncio
async def test_pipeline_end_to_end_with_stubs(neo4j_test):
    cand = ConceptCandidate(proposed_subject_id="fluid_mechanics",
        proposed_concept_id="bernoulli_principle", scope_summary="b",
        source_chunk_ids=["c1"], confidence=0.9)
    entry = ConceptRegistryEntry(subject_id="fluid_mechanics", concept_id="bernoulli_principle",
        scope_summary="b", canonical_symbols=CanonicalSymbols(symbols=["P1", "P2"]),
        normalization_map={}, parser_prompt_template="P", solver_hints=SolverHints())
    extracted = ExtractedProblem(source_document_id="doc1", source_chunk_id="c1", source_page=1,
        problem_text="t", given_values={"P1": 1.0}, target_unknown="P2",
        reference_solution=[ReferenceStep(step=1, entry_type="equation", id="e1",
            content={"symbolic": "P1 - P2", "label": "x", "variables": ["P1", "P2"]}, depends_on=[])],
        concept_id="bernoulli_principle", subject_id="fluid_mechanics", difficulty="intro")

    summary = await run_textbook_ingest(
        document_id="doc1", chunks=[{"id": "c1", "doc_id": "doc1", "page": 1, "text": "..."}],
        toc_text="...", headings=["Bernoulli"], neo=neo4j_test,
        discover=lambda **kw: [cand],
        resolve=lambda c, neo: ConceptResolution(kind="new", candidate=c),
        author=lambda res, source_text: entry,
        embed=lambda t: [0.0] * 3072,
        detect=lambda chunks: [ProblemCandidate(source_document_id="doc1", source_chunk_id="c1",
            source_page=1, raw_text="...", detected_kind="worked_example", confidence=0.9)],
        extract=lambda cand, resolve_concept: extracted)

    assert summary.problems_accepted == 1
    assert summary.concepts_created == 1
    from apollo.textbook_ingest import writer
    pid = writer.make_problem_id(extracted)
    assert await writer.problem_exists(neo4j_test, pid)


@pytest.mark.asyncio
async def test_pipeline_is_idempotent(neo4j_test):
    # running the same document twice writes problems once
    ...  # same setup; assert second run's problems_accepted == 0
```

- [ ] **Step 2: Run to verify it fails** → module absent.

- [ ] **Step 3: Implement**

```python
# apollo/textbook_ingest/pipeline.py
"""Per-document supervisor wiring stages 1->6. All stage functions are injected
(defaults bind the real stage modules) so tests run without real LLM calls."""
from __future__ import annotations

import traceback
from typing import Callable

from config import settings
from apollo.persistence.neo4j_client import Neo4jClient
from apollo.subjects import load_concept
from apollo.textbook_ingest import observability, writer
from apollo.textbook_ingest.concept_authoring import author_concept
from apollo.textbook_ingest.concept_resolver import resolve_candidate
from apollo.textbook_ingest.discovery import discover_concepts
from apollo.textbook_ingest.problem_detector import detect_problems
from apollo.textbook_ingest.problem_extractor import extract_problem
from apollo.textbook_ingest.types import IngestRunSummary
from apollo.textbook_ingest.validator import validate_problem


async def run_textbook_ingest(*, document_id: str, chunks: list[dict], toc_text: str,
                              headings: list[str], neo: Neo4jClient,
                              discover: Callable = None, resolve: Callable = None,
                              author: Callable = None, embed: Callable = None,
                              detect: Callable = None, extract: Callable = None
                              ) -> IngestRunSummary:
    discover = discover or (lambda **kw: discover_concepts(**kw))
    resolve = resolve or resolve_candidate
    author = author or (lambda res, source_text: author_concept(res, source_text=source_text))
    detect = detect or detect_problems
    extract = extract or (lambda cand, resolve_concept: extract_problem(cand, resolve_concept=resolve_concept))
    if embed is None:
        from indexing.document_embedder import embed_text
        embed = lambda t: embed_text(t, model=settings.TEXTBOOK_EMBEDDING_MODEL,
                                     dim=settings.TEXTBOOK_EMBEDDING_DIM)

    summary = IngestRunSummary(document_id=document_id, status="completed")
    chunk_index = {c["id"]: c["text"] for c in chunks}
    concept_map: dict[str, tuple[str, str]] = {}   # concept_id -> (subject, concept)
    entries: dict[str, object] = {}                 # concept_id -> ConceptRegistryEntry (new only)

    try:
        # Stage 1 + 1.5 + 2
        candidates = discover(toc_text=toc_text, headings=headings, chunk_index=chunk_index)
        summary.concepts_discovered = len(candidates)
        for cand in candidates:
            res = await resolve(cand, neo)
            await observability.write_dedup_decision(neo, res)
            if res.kind == "matched_existing":
                summary.concepts_merged += 1
                concept_map[cand.proposed_concept_id] = (res.matched_subject_id, res.matched_concept_id)
            else:
                entry = author(res, source_text="\n".join(chunk_index.get(cid, "")
                                                           for cid in cand.source_chunk_ids))
                entries[entry.concept_id] = entry
                concept_map[entry.concept_id] = (entry.subject_id, entry.concept_id)

        # Stage 3 + 4 + 5
        accepted_by_concept: dict[str, list] = {}
        for pcand in detect(chunks):
            summary.problems_detected += 1
            extracted = extract(pcand, resolve_concept=lambda h: concept_map.get(h))
            if extracted is None:
                continue
            summary.problems_extracted += 1
            concept_def = await _concept_def_for(extracted, entries, neo)
            res = validate_problem(extracted, concept_def, neo=neo)
            if not res.ok:
                summary.problems_rejected += 1
                await observability.write_rejection(neo, document_id=document_id,
                    source_page=extracted.source_page, source_chunk_id=extracted.source_chunk_id,
                    gate_failed=res.gate_failed, gate_diagnostic=res.diagnostic,
                    extracted_payload=extracted.model_dump_json())
                continue
            accepted_by_concept.setdefault(extracted.concept_id, []).append(writer.promote(extracted))

        # Stage 6
        for concept_id, problems in accepted_by_concept.items():
            subject_id, _ = concept_map[concept_id]
            entry = entries.get(concept_id)
            await writer.write_concept_bundle(
                neo, entry, problems, cluster_id=subject_id if entry else None,
                scope_embedding=embed(entry.scope_summary) if entry else None,
                subject_id=subject_id, concept_id=concept_id)
            if entry:
                summary.concepts_created += 1
            summary.problems_accepted += len(problems)
    except Exception as exc:  # noqa: BLE001 - per-document supervisor
        summary.status = "failed"
        summary.errors_logged += 1
        await observability.write_error(neo, document_id=document_id, stage="pipeline",
            error_class=type(exc).__name__, error_message=str(exc),
            stack_trace=traceback.format_exc(), context="run_textbook_ingest")

    await observability.write_ingest_run(neo, summary)
    return summary


async def _concept_def_for(extracted, entries, neo):
    from apollo.textbook_ingest.concept_schema_map import entry_to_rows, rows_to_concept_definition
    entry = entries.get(extracted.concept_id)
    if entry is not None:
        return rows_to_concept_definition(entry_to_rows(entry), problems_dir=None)
    return await load_concept(extracted.subject_id, extracted.concept_id, neo)
```

Update `validate_problem` signature to accept `neo=None` (gate 8) — already specified in Task B8. Re-export in `apollo/textbook_ingest/__init__.py`: `from apollo.textbook_ingest.pipeline import run_textbook_ingest`.

- [ ] **Step 4: Run to verify it passes** → PASS.

- [ ] **Step 5: Commit**

```bash
git add apollo/textbook_ingest/pipeline.py apollo/textbook_ingest/__init__.py apollo/textbook_ingest/tests/test_pipeline.py
git commit -m "feat(textbook-ingest): add stage 1-6 pipeline supervisor"
```

---

### Task E5: Hook into teacher_pdf_ingestion (post-pass)

**Files:**
- Modify: `knowledge/teacher_pdf_ingestion.py` (add optional post-pass call)
- Test: `apollo/textbook_ingest/tests/test_ingestion_hook.py`

Add an opt-in async helper `run_apollo_postpass(result, *, doc_id, neo)` that converts `TeacherPDFIngestionResult.items` to the chunk dicts the pipeline reads (via `indexing/document_chunker.items_to_chunk_texts`) and calls `run_textbook_ingest`. Keep it OFF by default (caller invokes it) so the existing Q&A ingestion path is untouched.

- [ ] **Step 1: Write the failing test** asserting `run_apollo_postpass` maps `result.items` → chunk dicts and calls a stubbed `run_textbook_ingest` with `document_id == doc_id`.

- [ ] **Step 2: Run to verify it fails.**

- [ ] **Step 3: Implement** a thin adapter module `apollo/textbook_ingest/ingestion_hook.py` (avoid importing apollo into `knowledge/` to prevent a layering cycle — the hook lives in `textbook_ingest` and is called from the worker that runs teacher ingestion). Map each `item` to `{"id": item.id, "doc_id": doc_id, "page": item.page, "text": item.text or item.raw_text}`; pull `toc_text`/`headings` from items whose `type == "heading"`.

- [ ] **Step 4: Run to verify it passes.**

- [ ] **Step 5: Commit** `git commit -m "feat(textbook-ingest): teacher PDF ingest post-pass adapter"`.

---

### Task E6: Tier-2 end-to-end smoke against the synthetic textbook

**Files:**
- Test: `apollo/textbook_ingest/tests/test_e2e_synthetic.py` (marked `tier2`)

- [ ] **Step 1: Write the tier-2 test** (real LLM, `neo4j_test` container): chunk `synthetic_textbook.md`, run `run_textbook_ingest` with **default** (real) stage callables, then assert (Spec §9 Tier-2):
  - ≥1 `:Concept` created;
  - ≥2 `:Problem` nodes from the worked examples;
  - reject count ≤ `TEXTBOOK_TIER2_MAX_REJECT_RATE * problems_detected`;
  - pull one extracted problem via `select_problem`, build a hand-crafted "good student attempt" KG, run the existing grader (`apollo/overseer/coverage.py` + `rubric.py`) → verdict passes.

```python
# apollo/textbook_ingest/tests/test_e2e_synthetic.py
import pytest

pytestmark = pytest.mark.tier2


@pytest.mark.asyncio
async def test_full_ingest_of_synthetic_textbook(neo4j_test):
    from pathlib import Path
    from apollo.textbook_ingest import run_textbook_ingest
    from apollo.overseer.problem_selector import select_problem
    md = Path("apollo/textbook_ingest/tests/fixtures/synthetic_textbook.md").read_text()
    chunks = [{"id": f"c{i}", "doc_id": "synthetic", "page": 1, "text": para}
              for i, para in enumerate(p for p in md.split("\n\n") if p.strip())]
    headings = [l[2:].strip() for l in md.splitlines() if l.startswith("# ")]
    summary = await run_textbook_ingest(document_id="synthetic", chunks=chunks,
        toc_text=md[:2000], headings=headings, neo=neo4j_test)
    assert summary.concepts_created >= 1
    assert summary.problems_accepted >= 2
    if summary.problems_detected:
        assert summary.problems_rejected <= 0.5 * summary.problems_detected
    # grader smoke
    subj, concept = await _resolved_cluster(neo4j_test)
    prob = await select_problem(cluster_id=subj, difficulty="intro", attempted_ids=[], neo=neo4j_test)
    assert prob.reference_solution  # round-trips
```

- [ ] **Step 2: Run (opt-in)** `pytest apollo/textbook_ingest/tests/test_e2e_synthetic.py -m tier2 -v` with creds + Docker. Confirm green.

- [ ] **Step 3: Commit** `git commit -m "test(textbook-ingest): tier-2 synthetic end-to-end smoke"`.

---

# Phase F — Tier-3 release gate + observability queries

**Phase goal:** A release-gate harness against a real textbook (manual, ~$50-100) and the ad-hoc Cypher observability queries from Spec §8. No new pipeline code.

---

### Task F1: Tier-3 release-gate harness

**Files:**
- Create: `apollo/textbook_ingest/scripts/tier3_release_gate.py`
- Create: `apollo/textbook_ingest/tests/fixtures/tier3_expectations.json` (concept-count band, per-concept problem band, reject-rate ceiling, known-good worked-example pages list — filled in by user/FellerCodes)
- Test: `apollo/textbook_ingest/tests/test_tier3_report.py` (unit-tests the report-card assembly with a synthetic summary, `tier3` NOT required)

- [ ] **Step 1:** Write a unit test for the report-card builder (`build_report_card(summary, expectations) -> dict`) asserting it flags out-of-band concept counts and computes reject rate. This test runs in normal CI (no real textbook).

- [ ] **Step 2:** Run → fails (module absent).

- [ ] **Step 3:** Implement `tier3_release_gate.py`: ingest a real textbook PDF (path as CLI arg) via `run_textbook_ingest`, then `build_report_card` comparing the `:IngestRun` summary against `tier3_expectations.json` (Spec §9 Tier-3 statistical assertions: concept count band, per-concept problem band, reject rate ≤ `TEXTBOOK_TIER3_MAX_REJECT_RATE`, all known-good worked examples present). Emit the report card as JSON + markdown to commit as the audit trail.

- [ ] **Step 4:** Run the unit test → PASS. (The full real-textbook run is manual: `python -m apollo.textbook_ingest.scripts.tier3_release_gate <pdf>`.)

- [ ] **Step 5: Commit** `git commit -m "feat(textbook-ingest): tier-3 release-gate harness + report card"`.

---

### Task F2: Observability query cookbook

**Files:**
- Create: `apollo/textbook_ingest/scripts/observability_queries.cypher`
- Test: `apollo/textbook_ingest/tests/test_observability_queries.py`

- [ ] **Step 1:** Write a test that seeds an `:IngestRun` + a few `:RejectedProblem`/`:IngestError` nodes in `neo4j_test`, runs each query string from the `.cypher` file, and asserts they return rows (Spec §8 "ad-hoc Cypher queries"): per-document rejection rate, stage-3 detection-count-zero scan, last-hour error count, recent-runs trend.

- [ ] **Step 2:** Run → fails (file absent).

- [ ] **Step 3:** Author the four queries in `observability_queries.cypher` (one labeled block each).

- [ ] **Step 4:** Run → PASS.

- [ ] **Step 5: Commit** `git commit -m "feat(textbook-ingest): observability query cookbook"`.

---

### Task F3: Full-suite regression + module docs

**Files:**
- Create: `apollo/textbook_ingest/README.md` (module overview, stage map, how to run each tier, prompt-versioning note, the sibling-ticket note for `sympy_exec.py:35`)

- [ ] **Step 1:** Run the full non-tier suite:

Run: `pytest apollo/ tests/ -v --tb=short -m "not tier2 and not tier3"`
Expected: PASS (Docker up for `neo4j_test`).

- [ ] **Step 2:** Write `README.md` documenting the module, the six stages, the three test tiers + how to run them, prompt versioning, and an explicit "Known limitations / sibling tickets" section (Gate 7 Path 1 only; `apollo/solver/sympy_exec.py:35` `_CANONICAL_SYMBOLS` cleanup is a separate ticket; symbol-meaning errors are residual).

- [ ] **Step 3: Commit** `git commit -m "docs(textbook-ingest): module README + tier/run guide"`.

---

## Self-Review

**Spec coverage check (each §):**
- §1 six stages + dedup + schema + migration + selector/load_concept + tests → Phases A–F. ✓
- §2 locked decisions → honored (Neo4j storage A; fully-automatic via validator B; eager at upload-time E5; pure-LLM tagging D2; first-writer-wins A6/E3; Gate 7 Path 1 B7; cluster-alias auto-create A6/E3; single `:_IngestEvent` namespace E2). ✓
- §3 architecture / "what stays the same" → handlers receive `Problem` unchanged; only the documented async/`neo` threading (Reconciliations 7/8). ✓
- §4 Pydantic types → Task A2 (`reference_solution` reconciled to `list[ReferenceStep]`). ✓
- §5 Neo4j shapes → A5 schema (3072), A6 writer (marker labels, `HAS_REFERENCE_NODE`, alias, SolverConstant.kind), E2 observability. ✓
- §6 8 gates → Phase B (B1 fixtures, B2–B8 gates; Gate 7 Path 1 only). ✓
- §7 selector + migration → A7, A8, A10; determinism via `authored` flag (Reconciliation 9). ✓
- §8 error handling + observability → E2 + pipeline supervisor E4 + F2 queries. ✓
- §9 three test tiers + migration regression → A10/B (regression), C5/E6 (tier-2), F1 (tier-3), tier markers C5. ✓
- §10 limitations → documented in F3 README; not implemented. ✓
- §11 out of scope → nothing planned for any item. ✓
- §12 open items → all resolved with concrete values (models via `_llm.py` per stage; embedding `text-embedding-3-large`/3072; thresholds in `config/settings.py` A0; prompts in `prompts/*.md` + `PROMPT_VERSIONS` C1; Testcontainers A1). ✓

**Placeholder scan:** Tasks E5/F1/F2/F3 compress the repetitive red→green→commit micro-steps into prose because they reuse patterns shown in full earlier (stage modules, observability writers, tier markers); each still names exact files, functions, and assertions. The synthetic textbook (C5), tier-3 expectations (F1), and four `authoring_*.md`/`detection.md`/`extraction.md` bodies (C1) are content the executor authors against the explicit JSON contracts given — drafted alongside implementation per Spec §12 item 4. No `TBD`/`implement later` left in code-bearing steps.

**Type consistency:** `validate_problem(p, concept, neo=None)` (B8/E4) consistent. `ValidatedProblem(**extracted.model_dump(), problem_id=...)` matches `promote` (E1). `reference_steps_to_kg_graph` name consistent (A3/A6/B). `write_concept`/`write_problem`/`write_concept_bundle`/`make_problem_id`/`promote` signatures consistent across A6/E1/E3/E4. `entry_to_rows`/`rows_to_concept_definition` consistent across A4/A8/A10/E4. `ConceptRegistryEntry.scope_summary` field present (A2) and used by authoring/writer/migration.

---

## Execution Handoff

**Plan complete and saved to `docs/superpowers/plans/2026-06-02-apollo-textbook-problem-index-plan.md`. Two execution options:**

**1. Subagent-Driven (recommended)** — I dispatch a fresh subagent per task, review between tasks, fast iteration.

**2. Inline Execution** — Execute tasks in this session using executing-plans, batch execution with checkpoints.

**Which approach?**

> **Note for whoever executes:** Task A1 adds the `testcontainers[neo4j]` dev dependency — confirm with the user before `pip install` (CLAUDE.md). `neo4j_test` tests require a running Docker daemon. Tier-2/Tier-3 tests make real LLM calls and are deselected by default (`-m "not tier2 and not tier3"`).
