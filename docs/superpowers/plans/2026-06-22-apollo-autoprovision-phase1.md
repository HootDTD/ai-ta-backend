# Apollo Auto-Provisioning Phase 1 (Make It Run) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Apollo's 6-stage auto-provisioning pipeline run end-to-end — a scraped candidate clears stage 4 (`tag_and_mint`), reaches stage 5, promotes to Tier-2, and writes `:Canon` nodes.

**Architecture:** Fix the stage-4 BLOCKER (the LLM drafts prereq/opposes edges by *bare* reference-node id, but persistence keys on *prefixed* canonical keys → `KeyError` → whole-document abort) with one key-normalization helper (approach A). Harden the two whole-document-abort paths most likely to bite a real run into clean per-candidate rejects. Document the `apollo_entity_prereqs` two-kind contract (H4) and lock it with a reader guard test — no migration.

**Tech Stack:** Python 3 / FastAPI, SQLAlchemy async + asyncpg (Supabase Postgres + pgvector), Neo4j (`:Canon`), pytest (`asyncio_mode = auto`), SymPy. LLM calls are mocked in all Tier-1 tests (deterministic injected `chat_fn`/`embed_fn`).

## Global Constraints

- Branch `ApolloRun`. NEVER push to `main`. Do NOT merge any PR — the owner merges every PR himself (open the PR, report URL + CI, stop).
- No new packages without asking (`fitz`/PyMuPDF already available).
- Supabase: "staging" = `hjevtxdtrkxjcaaexdxt` (test DB). "Apollo" project = PROD — never write to prod.
- Keep structured-JSON-from-LLM + comprehensive per-stage debug logging; never weaken the FAIL-CLOSED `TagMintError` convention.
- Update `docs/architecture/apollo.md` (owner doc) in the SAME commit as the code change that affects it; bump its `last_verified` in the final task.
- End every commit message with the trailer:
  `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`
- TDD: write the failing test first, watch it fail for the RIGHT reason, then implement.
- Source of truth design: `docs/superpowers/specs/2026-06-22-apollo-autoprovision-phase1-design.md`.

## Deviations from the design (refinements found while planning)

1. **`misconceptions_to_entities` KeyError hardening → DEFERRED to Phase 2.** It is fully dormant in v1 (`build_approved_pair` always sends `misconceptions=[]`, `solution.py:332`) and touches the FROZEN §8 seed converter (`learner_model_seed.py`), whose error contract is better changed together with the H3 misconception *wiring* that actually exercises it. Zero live benefit now; real frozen-code blast radius. (The latent `link_opposes` key bug — H1 — is still fixed now in Task 1, because that path is exercised the moment misconceptions are ever wired.)
2. **Gate-5 `StopIteration` (`promotion_lint.py:212`) → defense-in-depth only, no dedicated test.** `run_promotion_lint` validates the `Problem` and builds the KG from it at gate 1 (`promotion_lint.py:334-344`) before gate 5 runs, so `chain[-1]` is always a real procedure step and the bare `next()` is **unreachable through the lint**. We still harden it with `next(..., None)` marked `# pragma: no cover`, matching the existing sibling convention at `promotion_lint.py:345`. Folded into Task 3.

These keep Phase 1 tight (the real ordering bug — `_annotate` running before gate-1 validation — IS fixed in Task 3) and avoid touching frozen code for dormant paths.

## File Structure

| File | Responsibility | Tasks |
|------|----------------|-------|
| `apollo/provisioning/tag_mint.py` | Stage 4. Add `_bare_id_aliases`; register bare→entity aliases in `key_to_id` before `link_opposes`/`insert_prereqs`. | 1 |
| `apollo/provisioning/tests/test_tag_mint.py` | Bare-id prereq + bare-opposes regression tests. | 1 |
| `apollo/provisioning/retrieval_adapter.py` | Skip hybrid-search rows missing `content` instead of `row["content"]` KeyError. | 2 |
| `apollo/provisioning/tests/test_retrieval_adapter.py` | Missing-content row regression test. | 2 |
| `apollo/provisioning/promote.py` | Guard `_annotate` → clean gate-1 reject (the real ordering bug). | 3 |
| `apollo/provisioning/promotion_lint.py` | Gate-5 `next(..., None)` defensive hardening. | 3 |
| `apollo/provisioning/tests/test_promote.py` | Malformed-problem clean-reject regression test. | 3 |
| `apollo/provisioning/tests/test_prereq_edge_kinds.py` (new) | H4 guard: within-concept filter consumes auto ref-node edges, excludes seed concept-level edges. | 4 |
| `docs/architecture/apollo.md` | Owner doc: stage-4 fix, `apollo_entity_prereqs` two-kind contract, deferred-items/known-gaps, `last_verified`. | 1,4,5 |

---

### Task 1: Key normalization (BLOCKER + H1) — approach A

The LLM tag prompt (`orchestrator.py:100-111`) tells the model to use "minted entity keys" but never shows the prefix scheme; `build_tag_schema` types `from`/`to` as free strings (`provisioning_schema.py:110-119`). So the model authors **bare** reference-node ids (`bernoulli`, `solve_p2`), while `key_to_id` is built from **prefixed** canonical keys (`eq.bernoulli`, `proc.solve_p2`; `learner_model_seed.py:198-201,227`). `insert_prereqs` (`tag_mint_persist.py:217`) and `link_opposes` (`tag_mint_persist.py:197`) do a hard `key_to_id[...]` → `KeyError` → `TagMintError` → whole-document abort. Fix: register bare-id aliases pointing at the same entity ids, reusing the frozen `_entity_key_for_step` so the alias matches what was minted. Genuinely-unknown keys still `KeyError → TagMintError` (fail-closed preserved).

**Files:**
- Modify: `apollo/provisioning/tag_mint.py`
- Test: `apollo/provisioning/tests/test_tag_mint.py`
- Modify (doc): `docs/architecture/apollo.md`

**Interfaces:**
- Consumes: `_entity_key_for_step(step: dict) -> str` from `apollo.persistence.learner_model_seed` (returns `f"{prefix}.{id}"`).
- Produces: `_bare_id_aliases(problem: dict) -> dict[str, str]` (bare reference-node id → prefixed canonical_key) in `tag_mint.py`. `tag_and_mint`'s signature and `MintPlan` are unchanged; `insert_prereqs`/`link_opposes` are unchanged.

- [x] **Step 1: Write the failing tests**

Add to `apollo/provisioning/tests/test_tag_mint.py` (after `test_tag_and_mint_prereqs_inserted`, ~line 530):

```python
async def test_tag_and_mint_prereqs_accept_bare_ids(db_session):
    """REGRESSION (BLOCKER): the LLM tag prompt never sees the canonical-key
    prefix scheme, so it drafts prereqs by BARE reference-node id (solve_p2 /
    bernoulli), not the prefixed canonical_key (proc.solve_p2 / eq.bernoulli).
    tag_and_mint must resolve the bare ids to the minted entities and insert the
    edge. DISCRIMINATING: reverting the bare-id alias REDs with TagMintError
    ('prereq draft references an unminted entity key')."""
    ss_id, _subj = await _seed_course(db_session, slug="c-bareid")
    pair = _approved_pair(search_space_id=ss_id)
    tag = _tag_payload(prereqs=[["solve_p2", "bernoulli"]])  # BARE ids
    plan = await tag_and_mint(
        db_session, pair, chat_fn=_chat_returning(tag), embed_fn=_embed_distinct
    )
    from_id = plan.minted_entity_ids["proc.solve_p2"]
    to_id = plan.minted_entity_ids["eq.bernoulli"]
    edges = (
        (
            await db_session.execute(
                select(EntityPrereq)
                .where(EntityPrereq.from_entity_id == from_id)
                .where(EntityPrereq.to_entity_id == to_id)
            )
        )
        .scalars()
        .all()
    )
    assert len(edges) == 1


async def test_tag_and_mint_links_opposes_bare_id(db_session):
    """REGRESSION (H1): link_opposes shares the BLOCKER's bare/prefixed key bug.
    A misconception whose opposes names a reference node by BARE id (bernoulli)
    must link to the minted entity (eq.bernoulli), not raise. DISCRIMINATING:
    reverting the bare-id alias REDs with TagMintError ('misconception opposes an
    unknown entity key')."""
    ss_id, _subj = await _seed_course(db_session, slug="c-bareopp")
    misc = [
        {
            "key": "misc.pressure_follows_speed",
            "display_name": "Pressure follows speed",
            "description": "thinks higher speed means higher pressure",
            "opposes": "bernoulli",  # BARE id (not eq.bernoulli)
            "trigger_phrases": ["pressure goes up with speed"],
        }
    ]
    pair = _approved_pair(search_space_id=ss_id, misconceptions=misc)
    plan = await tag_and_mint(
        db_session, pair, chat_fn=_chat_returning(_tag_payload()), embed_fn=_embed_distinct
    )
    rows = (
        (
            await db_session.execute(
                select(KGEntity)
                .where(KGEntity.concept_id == plan.concept_id)
                .where(KGEntity.kind == "misconception")
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    payload = rows[0].payload
    assert payload["opposes_entity_key"] == "bernoulli"  # raw draft key preserved
    assert payload["opposes_entity_id"] == plan.minted_entity_ids["eq.bernoulli"]
```

- [x] **Step 2: Run the tests to verify they fail**

Run: `pytest apollo/provisioning/tests/test_tag_mint.py::test_tag_and_mint_prereqs_accept_bare_ids apollo/provisioning/tests/test_tag_mint.py::test_tag_and_mint_links_opposes_bare_id -v`
Expected: BOTH FAIL with `TagMintError` ("prereq draft references an unminted entity key 'solve_p2'" / "misconception opposes an unknown entity key 'bernoulli'").

- [x] **Step 3: Add the helper to `tag_mint.py`**

Extend the existing `learner_model_seed` import (currently `EntitySpec, misconceptions_to_entities, reference_solution_to_entities`) to add `_entity_key_for_step`:

```python
from apollo.persistence.learner_model_seed import (
    EntitySpec,
    _entity_key_for_step,
    misconceptions_to_entities,
    reference_solution_to_entities,
)
```

Add this function near the other module-level helpers (e.g. after `_parse_tag`, ~line 194):

```python
def _bare_id_aliases(problem: dict) -> dict[str, str]:
    """Map each reference-step BARE id to its PREFIXED canonical_key.

    The LLM tag prompt (orchestrator._TAG_MINT_SYSTEM_PROMPT) never reveals the
    canonical-key prefix scheme, so the model drafts prereq/opposes edges using
    the bare reference-node id (``bernoulli``) rather than the minted canonical
    key (``eq.bernoulli``). This recovers ``bare -> {prefix}.{id}`` by REUSING
    the frozen §8 ``_entity_key_for_step``, so the alias is byte-identical to what
    ``reference_solution_to_entities`` minted. A step whose ``entry_type`` is
    outside the frozen mint map is skipped (gate 1 fails it closed at promotion)."""
    aliases: dict[str, str] = {}
    for step in problem.get("reference_solution", []):
        try:
            aliases[step["id"]] = _entity_key_for_step(step)
        except (KeyError, TypeError):
            continue
    return aliases
```

- [x] **Step 4: Register the aliases in `tag_and_mint`**

In `tag_and_mint`, immediately AFTER the mint/merge loop populates `key_to_id` (after the `for spec in all_specs:` loop ends, ~line 266) and BEFORE the `# --- 5a. Link misconception opposes` block, insert:

```python
    # Register BARE-id aliases so an LLM prereq/opposes draft that names a
    # reference node by its bare id (bernoulli) resolves to the SAME entity as its
    # prefixed canonical_key (eq.bernoulli). The LLM never sees the prefix scheme,
    # so it authors bare ids; without this the hard key_to_id[...] lookup in
    # insert_prereqs / link_opposes KeyErrors and the whole document aborts (the
    # BLOCKER). setdefault never shadows a real canonical key. A genuinely-unknown
    # key (in neither the canonical nor the bare set) still raises KeyError ->
    # TagMintError (fail-closed) downstream.
    for bare_id, canonical_key in _bare_id_aliases(problem).items():
        if canonical_key in key_to_id:
            key_to_id.setdefault(bare_id, key_to_id[canonical_key])
```

- [x] **Step 5: Run the new tests + the full tag_mint suite to verify GREEN (no regressions)**

Run: `pytest apollo/provisioning/tests/test_tag_mint.py -v`
Expected: PASS — including the new bare-id tests AND the pre-existing fail-closed tests (`opposes: "eq.does_not_exist"` and `prereqs: ["eq.bernoulli", "eq.nonexistent"]` still raise `TagMintError`, because those keys are in neither the canonical nor the bare set).

- [x] **Step 6: Update the owner doc**

In `docs/architecture/apollo.md`, in the stage-4 / `tag_and_mint` description, add one sentence:

```
Stage 4 normalizes the LLM tag draft's BARE reference-node ids (e.g. `bernoulli`)
to their prefixed canonical keys (`eq.bernoulli`) via `_bare_id_aliases` (reusing
the frozen `_entity_key_for_step`) before `insert_prereqs`/`link_opposes`, because
the tag prompt never exposes the prefix scheme. Genuinely-unmappable keys still
fail closed as `TagMintError`.
```

- [x] **Step 7: Commit**

```bash
git add apollo/provisioning/tag_mint.py apollo/provisioning/tests/test_tag_mint.py docs/architecture/apollo.md
git commit -m "fix(apollo): resolve LLM bare-id prereq/opposes keys in stage 4 (BLOCKER+H1)"
```

---

### Task 2: Robustness — retrieval adapter skips content-less rows

`make_course_retrieve_fn`'s closure reads `row["content"]` twice (`retrieval_adapter.py:55,58`). A hybrid-search row missing `content` raises `KeyError`, which the orchestrator's catch-all (`orchestrator.py:341`) maps to a per-DOCUMENT abort. Skip such rows instead.

**Files:**
- Modify: `apollo/provisioning/retrieval_adapter.py`
- Test: `apollo/provisioning/tests/test_retrieval_adapter.py`

**Interfaces:**
- Consumes: `make_course_retrieve_fn(db, *, search_space_id, top_k=...) -> retrieve(question)`; `retrieval_adapter.AITAHybridSearchRetriever` (importable for monkeypatching).
- Produces: unchanged signature; the returned `retrieve` now yields only spans for rows with non-empty `content`.

- [x] **Step 1: Write the failing test**

Add to `apollo/provisioning/tests/test_retrieval_adapter.py`:

```python
async def test_retrieve_skips_rows_missing_content(db_session, monkeypatch):
    """REGRESSION: a hybrid_search row lacking a 'content' key must be SKIPPED,
    not crash the whole document with a KeyError (the orchestrator maps an
    unexpected exception to a per-DOCUMENT abort). DISCRIMINATING: reverting to
    row['content'] REDs with KeyError."""
    from apollo.provisioning import retrieval_adapter

    async def _fake_search(self, query_text, top_k):
        return [
            {"content": "good chunk", "document_id": 1, "page_number": 2},
            {"document_id": 9, "page_number": 3},  # NO 'content' key
        ]

    monkeypatch.setattr(
        retrieval_adapter.AITAHybridSearchRetriever, "hybrid_search", _fake_search
    )
    retrieve = retrieval_adapter.make_course_retrieve_fn(db_session, search_space_id=1)

    class _Q:
        problem_text = "find downstream pressure P2"
        chunk_content_hash = "abc"

    spans = await retrieve(_Q())
    assert len(spans) == 1
    assert spans[0].text == "good chunk"
```

- [x] **Step 2: Run the test to verify it fails**

Run: `pytest apollo/provisioning/tests/test_retrieval_adapter.py::test_retrieve_skips_rows_missing_content -v`
Expected: FAIL with `KeyError: 'content'`.

- [x] **Step 3: Implement the guard**

In `apollo/provisioning/retrieval_adapter.py`, replace the `spans = tuple(...)` comprehension (lines 53-62) with:

```python
        spans = tuple(
            GroundingSpan(
                text=content,
                document_id=row.get("document_id"),
                page=row.get("page_number"),
                chunk_content_hash=chunk_content_hash(content),
                carries_solution=False,
            )
            for row in rows
            # Skip a row with no usable text rather than KeyError on row["content"]
            # — an unexpected exception here aborts the WHOLE document (orchestrator
            # catch-all); a missing chunk is a per-span no-op, not a doc failure.
            if (content := (row.get("content") or "").strip())
        )
```

- [x] **Step 4: Run the test to verify it passes**

Run: `pytest apollo/provisioning/tests/test_retrieval_adapter.py -v`
Expected: PASS (the whole module, no regressions).

- [x] **Step 5: Commit**

```bash
git add apollo/provisioning/retrieval_adapter.py apollo/provisioning/tests/test_retrieval_adapter.py
git commit -m "fix(apollo): skip content-less grounding rows instead of aborting the document"
```

---

### Task 3: Robustness — promote annotates safely; gate-5 defensive hardening

`promote` calls `_annotate(problem, mint_plan)` at `promote.py:105` BEFORE `run_promotion_lint`'s gate-1 schema validation (`promotion_lint.py:334`). `_annotate` → `_entity_key_for_step` reads `step["id"]`/`step["entry_type"]` and indexes the frozen mint map, so a malformed step (missing `id`, or an `entry_type` outside the map) raises `KeyError` and surfaces as a per-DOCUMENT abort instead of the clean gate-1 reject the lint would have produced. Guard it. Also harden gate-5's unreachable bare `next()` (see Deviation #2).

**Files:**
- Modify: `apollo/provisioning/promote.py`
- Modify: `apollo/provisioning/promotion_lint.py`
- Test: `apollo/provisioning/tests/test_promote.py`

**Interfaces:**
- Consumes: `promote(db, neo, *, problem: dict, mint_plan: MintPlan, search_space_id: int, concept_problem_id: int, existing_problem_hashes) -> PromoteResult`. A malformed-problem reject returns BEFORE any DB/Neo4j access (the `_annotate` call is the first statement), so the test passes `neo=None`.
- Produces: `promote` returns `PromoteResult(promoted=False, failed_gate=1, diagnostic=...)` on a malformed problem (was: raised `KeyError`).

- [x] **Step 1: Write the failing test**

Add to `apollo/provisioning/tests/test_promote.py`:

```python
async def test_promote_rejects_malformed_problem_cleanly(db_session):
    """REGRESSION: _annotate runs BEFORE run_promotion_lint's gate-1 validation,
    so a step whose entry_type is outside the frozen mint map would KeyError in
    _annotate and surface as a per-DOCUMENT abort. promote must convert it to a
    clean gate-1 rejection (one bad candidate must not sink the document).
    DISCRIMINATING: removing the guard REDs with KeyError."""
    from apollo.provisioning.promote import promote
    from apollo.provisioning.tag_mint import MintPlan

    problem = {
        "id": "scrape.bad",
        "concept_id": "bernoulli_principle",
        "difficulty": "intro",
        "problem_text": "x",
        "given_values": {},
        "target_unknown": "P2",
        "reference_solution": [
            {"step": 1, "entry_type": "NOT_A_REAL_TYPE", "id": "x", "content": {}},
        ],
    }
    mint_plan = MintPlan(
        concept_id=1,
        concept_slug="bernoulli_principle",
        authored_symbols=[],
        minted_entity_ids={},
        merged_entity_keys=[],
        prereq_pairs=[],
        misconception_keys=[],
    )
    result = await promote(
        db_session,
        None,
        problem=problem,
        mint_plan=mint_plan,
        search_space_id=1,
        concept_problem_id=1,
        existing_problem_hashes=set(),
    )
    assert result.promoted is False
    assert result.failed_gate == 1
```

- [x] **Step 2: Run the test to verify it fails**

Run: `pytest apollo/provisioning/tests/test_promote.py::test_promote_rejects_malformed_problem_cleanly -v`
Expected: FAIL with `KeyError: 'NOT_A_REAL_TYPE'`.

- [x] **Step 3: Guard `_annotate` in `promote`**

In `apollo/provisioning/promote.py`, replace the first line of `promote`'s body (`annotated = _annotate(problem, mint_plan)`, line 105) with:

```python
    try:
        annotated = _annotate(problem, mint_plan)
    except (KeyError, TypeError) as exc:
        # _annotate runs BEFORE run_promotion_lint's gate-1 schema validation, so
        # a malformed problem (a step missing id/entry_type, or an entry_type
        # outside the frozen mint map) would KeyError here and surface to the
        # orchestrator as an unexpected-exception WHOLE-DOCUMENT abort. Convert it
        # to the clean gate-1 rejection the lint produces for the cases that reach
        # it — one bad candidate must not sink the document.
        _LOG.info(
            "provisioning_promote_rejected",
            extra={
                "event": "provisioning_promote_rejected",
                "concept_problem_id": concept_problem_id,
                "failed_gate": 1,
            },
        )
        return PromoteResult(
            promoted=False,
            failed_gate=1,
            diagnostic=f"gate 1: malformed problem rejected before annotation: {exc}",
        )
```

- [x] **Step 4: Harden the gate-5 `next()` (defense-in-depth)**

In `apollo/provisioning/promotion_lint.py`, replace line 212:

```python
    terminal = next(s for s in _proc_steps(problem) if s.id == terminal_id)
```

with:

```python
    terminal = next((s for s in _proc_steps(problem) if s.id == terminal_id), None)
    if terminal is None:  # pragma: no cover - defense in depth: gate 1 builds the
        # KG from the validated problem, so chain[-1] is always a real proc step.
        return f"gate 5: terminal step {terminal_id!r} not found among procedure steps"
```

- [x] **Step 5: Run the tests to verify GREEN (no regressions)**

Run: `pytest apollo/provisioning/tests/test_promote.py apollo/provisioning/tests/test_promotion_lint.py -v`
Expected: PASS.

- [x] **Step 6: Commit**

```bash
git add apollo/provisioning/promote.py apollo/provisioning/promotion_lint.py apollo/provisioning/tests/test_promote.py
git commit -m "fix(apollo): convert malformed-problem aborts to clean per-candidate rejects"
```

---

### Task 4: H4 — `apollo_entity_prereqs` two-kind contract (guard test + doc)

The seed path writes concept→concept edges (`concept_dag_to_prereqs`, `learner_model_seed.py:144`); the auto path writes ref-node→ref-node edges. `apollo_entity_prereqs` is a bare composite-PK table (`models.py:416-431`) with no label column. The two kinds answer different questions and we keep both; the label is DERIVABLE from endpoint `apollo_kg_entities.kind`, and `read_learner_profile`'s within-concept filter (`personalization_read.py:146-155`) already excludes the cross-concept concept-level edges. Lock that with a guard test — no migration.

**Files:**
- Create: `apollo/provisioning/tests/test_prereq_edge_kinds.py`
- Modify (doc): `docs/architecture/apollo.md`

**Interfaces:**
- Consumes: `read_learner_profile(db, *, user_id: str, search_space_id: int, concept_id: int) -> LearnerProfile` (`.prereq_edges: tuple[tuple[int,int], ...]`).
- Produces: no code change — a behavior-lock test + documented contract.

- [ ] **Step 1: Write the guard test (must pass against current code — it locks behavior we rely on)**

Create `apollo/provisioning/tests/test_prereq_edge_kinds.py`:

```python
"""H4 — the apollo_entity_prereqs two-kind contract guard.

apollo_entity_prereqs legitimately holds BOTH concept-level edges (seed path,
concept->concept) AND ref-node-level edges (auto path, ref-node->ref-node).
read_learner_profile's WITHIN-CONCEPT filter must consume the auto ref-node edges
and EXCLUDE the seed concept-level cross-concept edges, so prereq readers never
conflate the two kinds. DISCRIMINATING: dropping the within-concept .in_()
predicates in read_learner_profile would pull the cross-concept edge in and RED.

Tier-1 real-PG: requires the db_session fixture (re-exported in apollo/conftest.py)
and Docker-skips cleanly when the daemon is down.
"""
from __future__ import annotations

from apollo.learner_model.personalization_read import read_learner_profile
from apollo.persistence.models import Concept, EntityPrereq, KGEntity, Subject
from database.models import SearchSpace


async def test_within_concept_filter_excludes_concept_level_edges(db_session):
    space = SearchSpace(name="Course h4", slug="c-h4", subject_name="Physics")
    db_session.add(space)
    await db_session.flush()
    subj = Subject(slug="s-h4", display_name="Sub", search_space_id=space.id)
    db_session.add(subj)
    await db_session.flush()

    concept_a = Concept(subject_id=subj.id, slug="bernoulli", display_name="Bernoulli")
    concept_b = Concept(subject_id=subj.id, slug="fluids", display_name="Fluids")
    db_session.add_all([concept_a, concept_b])
    await db_session.flush()

    # concept A: two ref-node entities (the AUTO shape) + a concept-kind entity.
    eq = KGEntity(
        concept_id=concept_a.id, canonical_key="eq.bernoulli", kind="equation",
        display_name="Bernoulli eq", payload={}, aliases=[], scope_summary="x",
    )
    proc = KGEntity(
        concept_id=concept_a.id, canonical_key="proc.solve_p2", kind="procedure",
        display_name="Solve P2", payload={}, aliases=[], scope_summary="x",
    )
    concept_ent_a = KGEntity(
        concept_id=concept_a.id, canonical_key="concept.bernoulli", kind="concept",
        display_name="Bernoulli", payload={}, aliases=[], scope_summary="x",
    )
    # concept B: a concept-kind entity (the cross-concept SEED edge target).
    concept_ent_b = KGEntity(
        concept_id=concept_b.id, canonical_key="concept.fluids", kind="concept",
        display_name="Fluids", payload={}, aliases=[], scope_summary="x",
    )
    db_session.add_all([eq, proc, concept_ent_a, concept_ent_b])
    await db_session.flush()

    db_session.add_all([
        EntityPrereq(from_entity_id=proc.id, to_entity_id=eq.id),  # AUTO, within A
        EntityPrereq(from_entity_id=concept_ent_a.id, to_entity_id=concept_ent_b.id),  # SEED, A->B
    ])
    await db_session.flush()

    profile = await read_learner_profile(
        db_session, user_id="u-h4", search_space_id=space.id, concept_id=concept_a.id
    )
    # Only the within-concept auto ref-node edge survives; the cross-concept
    # concept-level edge (one endpoint in concept B) is excluded.
    assert profile.prereq_edges == ((proc.id, eq.id),)
```

- [ ] **Step 2: Run the guard test to confirm it passes (behavior is already correct; this locks it)**

Run: `pytest apollo/provisioning/tests/test_prereq_edge_kinds.py -v`
Expected: PASS. (If it FAILS, stop — the within-concept assumption the H4 decision rests on is wrong; re-open the H4 design before proceeding.)

- [ ] **Step 3: Document the contract in the owner doc**

In `docs/architecture/apollo.md`, near the `apollo_entity_prereqs` / prereq description, add:

```
`apollo_entity_prereqs` holds TWO kinds of edge, distinguished by endpoint
`apollo_kg_entities.kind`: CONCEPT-LEVEL edges (seed path, `concept_dag_to_prereqs`
— both endpoints `kind='concept'`, cross-concept curriculum ordering) and
REF-NODE-LEVEL edges (auto path, `tag_and_mint` — within-problem step structure).
Both are valid; there is intentionally no discriminator column. `read_learner_profile`
loads only WITHIN-CONCEPT edges, so it consumes the auto ref-node edges and
excludes the seed cross-concept concept-level edges — the two kinds never conflate
in `prereqs_mastered`. Guard: `tests/test_prereq_edge_kinds.py`. (An explicit
`edge_kind` column is a possible Phase-2+ change, not required for this contract.)
```

- [ ] **Step 4: Commit**

```bash
git add apollo/provisioning/tests/test_prereq_edge_kinds.py docs/architecture/apollo.md
git commit -m "docs(apollo): lock the apollo_entity_prereqs two-kind contract (H4) with a guard test"
```

---

### Task 5: Full suite green + owner-doc reconciliation + end-to-end local run

**Files:**
- Modify (doc): `docs/architecture/apollo.md`
- (No new source.)

- [ ] **Step 1: Run the full provisioning suite**

Run: `pytest apollo/provisioning/ -v --tb=short`
Expected: PASS (all modules, including the four new/changed tests). Fix any regression before continuing.

- [ ] **Step 2: Add the deferred-items / known-gaps note to the owner doc**

In `docs/architecture/apollo.md`, add a "Phase 1 scope / known gaps" note in the apollo auto-provisioning section:

```
Auto-provisioning Phase 1 makes the pipeline RUN end-to-end (BLOCKER+H1 key
normalization, abort->reject robustness, H4 two-kind prereq contract). DEFERRED:
H2 (require `content.symbolic` for equation steps), H3 misconception WIRING
(`misconceptions=[]` today; gated by `APOLLO_MISCONCEPTION_ENABLED`, a no-op until
chat wiring), the `misconceptions_to_entities` KeyError->named-error hardening
(dormant; deferred with H3 to avoid touching the frozen §8 converter), H5 (dedup
re-embeds the course pool per candidate), unbounded per-doc scrape fan-out, the
scrape-all-chunks vs week-gated-grounding scope asymmetry, the dedup cosine NaN
guard, and the LOW hygiene items. Safe to defer: the subsystem is dormant in prod
(`APOLLO_AUTOPROVISION_ENABLED` OFF + 0 replicas), so no auto-provisioned data
exists to be affected.
```

- [ ] **Step 3: Bump `last_verified`**

In `docs/architecture/apollo.md` frontmatter, confirm `last_verified: 2026-06-22` (update to the actual implementation date if later).

- [ ] **Step 4: Commit the doc reconciliation**

```bash
git add docs/architecture/apollo.md
git commit -m "docs(apollo): reconcile owner doc with Phase 1 auto-provisioning scope + deferred items"
```

- [ ] **Step 5: End-to-end local run (real LLM, local Neo4j, staging Supabase)**

Per `RUNBOOK.md` (gitignored, repo root). Capture the `:Canon` count before and after:

```bash
source ./apollo_run_env.sh
# Baseline :Canon count (cypher-shell or the project's Neo4j client):
#   MATCH (n:Canon) RETURN count(n) AS before;
python scripts/drain_one_provision.py
#   MATCH (n:Canon) RETURN count(n) AS after;   # expect after > before
```

Expected: a scraped candidate clears stage 4 (`tag_and_mint`, no `TagMintError`), reaches stage 5, promotes to Tier-2 (`apollo_concept_problems.tier == 2` on the real tagged concept), and `project_canon` writes `:Canon` nodes (count increases). Cost ~$0.05.

- [ ] **Step 6: Record the run outcome**

Note in the PR description (and a short comment in this plan if iterating): the run id, `n_promoted`, and the before/after `:Canon` counts. If a candidate rejects rather than promotes, inspect `apollo_rejected_problems.failed_gate`/`diagnostic` and `apollo_ingest_errors` to confirm the rejection is a legitimate content/lint verdict (expected on some chunks) — not a `TagMintError`/`KeyError` abort (which would mean a Phase-1 fix regressed).

---

## Self-Review

**Spec coverage:**
- BLOCKER + H1 → Task 1. ✅
- H4 (keep both, clearly labeled; no migration) → Task 4 (guard test + documented contract). ✅
- Robustness downgrades → Task 2 (`retrieval_adapter`), Task 3 (`_annotate` ordering bug + gate-5 defensive). `misconceptions_to_entities` hardening and gate-5 testing explicitly refined/deferred — see "Deviations" (design Component 3 updated to match). ✅
- Success criterion (`pytest apollo/provisioning/ -v` green + E2E `:Canon`) → Task 5. ✅
- Owner-doc drift contract → doc edits folded into Tasks 1, 4, 5. ✅
- Deferred items recorded → Task 5 Step 2. ✅

**Placeholder scan:** No TBD/TODO; every code step shows the exact code; every test step shows the assertion and the expected fail/pass output.

**Type consistency:** `_bare_id_aliases(problem: dict) -> dict[str, str]` (Task 1) consumed only within `tag_and_mint`. `PromoteResult(promoted, failed_gate, diagnostic)` (Task 3) matches `promote.py:58-64`. `LearnerProfile.prereq_edges: tuple[tuple[int,int], ...]` (Task 4) matches `personalization_read.py:70`. `MintPlan` fields in the Task 3 test match `tag_mint.py:88-98`. `GroundingSpan(text, document_id, page, chunk_content_hash, carries_solution)` (Task 2) matches `retrieval_adapter.py:54-60`.
