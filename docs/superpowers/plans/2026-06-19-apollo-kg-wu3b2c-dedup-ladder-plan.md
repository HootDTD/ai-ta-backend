# WU-3B2c — Course-local dedup ladder — TDD implementation plan

**Date:** 2026-06-19
**Branch (already checked out):** `feat/apollo-kg-wu3b2c-dedup-ladder` — DO NOT branch/switch/push/PR.
**diff-cover compare branch:** `feat/apollo-kg-wu3b2b-promotion-lint`
**Spec:** `docs/superpowers/specs/2026-06-10-apollo-kg-learner-model-architecture-decision.md` §8B.5 (storage/observability), §8B.7 (testing — the cross-course non-merge proof), §1.4 (course isolation).
**Split-proposal (PINNED):** `docs/superpowers/plans/2026-06-19-apollo-kg-wu3b2-split-proposal.md` `### WU-3B2c` + ORCHESTRATOR ADJUDICATION rows #4, #11 (do NOT re-open).
**Planner class:** feller-planner-backend. **v1.**

---

## Overview

WU-3B2c is the §8B stage-5 **course-local dedup ladder**. It resolves ONE candidate
entity against THIS course's existing `apollo_kg_entities` inventory and returns a frozen
`DedupVerdict`, writing exactly one `apollo_dedup_decisions` row per resolution. The ladder
is `slug-exact → scope_summary embedding cosine → cheap_chat LLM-judge tiebreaker`, and the
**course-scope WHERE is applied BEFORE any similarity is computed** — never a global search
(§1.4). The embedding source is the NEW `apollo_kg_entities.scope_summary` TEXT column
(added by migration 030 / 3B2a), embedded ON THE FLY via an INJECTED `embed_fn`; there is no
persisted entity vector. The LLM judge is an INJECTED `judge_fn` so Tier-1 tests are
deterministic with zero network calls.

This unit is PURE-math + thin-injected-LLM at the ladder level, plus a real-PG course-scope
query and the dedup-decision write. It does NOT scrape, does NOT mint, does NOT upsert
entities — 3B2d calls `resolve_candidate` BEFORE its own upsert and acts on the verdict.

**Prerequisites already landed (verified):** 3B2a shipped migration 030's ORM — `KGEntity`
carries `scope_summary` (`apollo/persistence/models.py:405`), and `DedupDecision` exists with
all required columns (`models.py:702-735`). The `apollo/provisioning/` package exists from
3B2b (`__init__.py`, `promotion_lint.py`, `problem_hash.py`).

## Ground truth (verified file:line, 2026-06-19)

| Fact | Anchor | Consequence for 3B2c |
|---|---|---|
| `KGEntity.scope_summary` exists, nullable TEXT | `apollo/persistence/models.py:405` | embedding source; a NULL `scope_summary` on an in-course entity must be skipped (no embed) — guard it |
| `DedupDecision` ORM: `ingest_run_id`(nullable), `search_space_id`(NOT NULL), `concept_id`(nullable), `candidate_key`(NOT NULL), `method`, `similarity`(Float, nullable), `verdict`, `matched_entity_id`(nullable) | `models.py:702-735` | the row written per resolution; `similarity=None` for slug tier; CHECKs are SQL-only |
| `embed_text(text, model=None, dim=None) -> list[float]` is **SYNC** | `indexing/document_embedder.py:37-50` | `embed_fn` is a SYNC callable `str -> Sequence[float]`; call it directly (no await) inside the async `resolve_candidate` |
| `cheap_chat(*, purpose, messages, response_format=None, temperature=0.0, model=None) -> str` is **SYNC** | `apollo/agent/_llm.py:51-73` | `judge_fn` is a SYNC callable returning a verdict string; inject a thin adapter, never call `cheap_chat` directly in this module |
| Course-scope chain | `KGEntity.concept_id → Concept.id` (`models.py:390`); `Concept.subject_id → Subject.id` (`models.py:146`); `Subject.search_space_id` (`models.py:129`) | the course-scope WHERE: restrict `apollo_kg_entities` to concepts whose subject's `search_space_id == :search_space_id` |
| `apollo_kg_entities` unique key is `(concept_id, canonical_key)` per-concept, NOT global | `models.py:409-413` | slug-tier exact match is on `(concept_id, candidate.canonical_key)` within this course |
| `KGEntity.canonical_key` is the slug carrier | `models.py:396` | the candidate must expose a `canonical_key` for the slug tier |
| Real-PG fixture builds schema from ORM `create_all`, NOT migration replay | `tests/conftest.py:139-159` | `scope_summary` + `apollo_dedup_decisions` are present in real-PG tests because 3B2a's ORM declares them; this unit's tests use `db_session`, not the migration test |
| `db_session` (savepoint rollback) re-exported to apollo tests | `apollo/conftest.py:24` | request `db_session` directly in `apollo/provisioning/tests/test_dedup.py` |
| numpy 2.4.4 present | `.venv/Scripts/python.exe -c "import numpy"` | cosine via numpy (or stdlib `math`); NO new package (ADJ #8) |
| Seeding template for course-scoped real-PG apollo rows | `tests/database/test_resolution_resolves_to_postgres.py:35-58` | mirror `SearchSpace → Subject → Concept → KGEntity` flush chain in the cross-course test |
| Mock template: `patch(...cheap_chat, return_value=json.dumps(...))` | `apollo/agent/tests/test_leakage_judge.py:31-36` | in 3B2c, prefer DIRECT injection of `judge_fn`/`embed_fn` stubs over `patch` — they are constructor/param args |

## Public API (PINNED — backward-compat surface for 3B2d)

Frozen by the split-proposal `### WU-3B2c` + ADJ #4/#11. Do NOT widen or rename.

```python
# apollo/provisioning/dedup.py
from dataclasses import dataclass

@dataclass(frozen=True)
class DedupVerdict:
    verdict: str                     # 'merged' | 'distinct'
    method: str                      # 'slug' | 'embedding' | 'llm_judge'
    similarity: float | None         # cosine for embedding/llm_judge tiers; None for slug
    matched_entity_id: int | None    # KGEntity.id merged onto; None when distinct

async def resolve_candidate(
    db,                              # AsyncSession
    *,
    search_space_id: int,
    concept_id: int,                 # BIGINT apollo_concepts.id (NEVER the slug — ADJ #6)
    candidate,                       # carries .canonical_key (slug) + .scope_summary (text)
    embed_fn,                        # SYNC Callable[[str], Sequence[float]]  (inject embed_text)
    judge_fn,                        # SYNC Callable[..., str] -> 'merged'|'distinct' (inject cheap_chat adapter)
    ingest_run_id: int | None = None,  # stamped onto the dedup_decisions row when present
) -> DedupVerdict: ...
```

**Candidate contract (consumed, NOT defined here):** `resolve_candidate` reads two
attributes off `candidate` — `candidate.canonical_key: str` (the slug for the slug tier and
the `candidate_key` audit column) and `candidate.scope_summary: str` (the text embedded for
the embedding tier). The plan does NOT introduce a new candidate type; it duck-types these
two attributes. The test suite defines a tiny local `_Candidate` namedtuple/dataclass fixture
with exactly those two fields. 3B2d owns the real candidate type and passes it in.

**`judge_fn` adapter contract:** `judge_fn` is called with the candidate's `scope_summary`
and the best in-course matched entity's `scope_summary` (the two texts to compare) plus the
course `search_space_id`; it returns the bare string `'merged'` or `'distinct'`. In Tier-1
tests `judge_fn` is a fixed stub lambda. In production 3B2d/3B2g wraps `cheap_chat` (prompt:
"are these the same concept FOR THIS COURSE?") and parses its reply to that bare string. The
adapter — not `resolve_candidate` — owns prompt text + JSON parsing, keeping the ladder pure
and the judge swappable.

## Out-of-scope boundaries (this unit only)

EXPLICITLY NOT in WU-3B2c (each is a downstream unit — do not implement, do not stub-with-logic):

- **No scrape / no mint / no upsert** of entities or problems (3B2d). `resolve_candidate`
  reads the inventory and returns a verdict + writes ONE audit row; it never INSERTs/UPDATEs
  `apollo_kg_entities` or `apollo_concept_problems`.
- **No solution find-or-generate / pairing gate** (3B2e).
- **No queue-drain / metered LLM client / cost budget** (3B2f). `embed_fn`/`judge_fn` are
  injected; this unit does not meter tokens.
- **No trigger / worker shell / orchestrator / observability run-counter updates** (3B2g).
  3B2c writes the `apollo_dedup_decisions` audit row but does NOT increment
  `apollo_ingest_runs.n_dedup_merged` (that aggregate is 3B2g's; `ingest_run_id` is merely
  stamped onto the decision row when the caller supplies it).
- **No quarantine** (3B2h).
- **No real OpenAI / no real embedder / no real `cheap_chat` call** anywhere in the test
  suite (ADJ #10 — Tier-1 only, no LLM tokens in CI). Both fns are injected stubs.
- **No new package** (ADJ #8). Cosine uses numpy (present) or stdlib `math`. If any step here
  seems to want a package, STOP and escalate — do not install.
- **No migration** (030 already landed; this unit consumes its ORM).
- **No edit to `apollo/agent/_llm.py`, `indexing/document_embedder.py`, or the frozen seed
  module** — they are injected/imported, never modified.

## Files to create / edit

All inside the binding scope list — no other files may change.

| File | Action | Contents |
|---|---|---|
| `apollo/provisioning/dedup_constants.py` | **NEW** | `EMBED_MERGE_THRESHOLD = 0.92`, `EMBED_JUDGE_BAND = (0.82, 0.92)` (lower-inclusive, upper-exclusive), `_DISTINCT_BELOW = 0.82` (== band lower bound, named for readability). Module docstring cites ADJ #4 + "config-driven, calibration-tunable". Optional env override read at import (e.g. `float(os.getenv("APOLLO_DEDUP_MERGE_THRESHOLD", "0.92"))`) — but keep defaults the pinned numbers. No logic, no imports beyond `os`. |
| `apollo/provisioning/dedup.py` | **NEW** | `DedupVerdict` frozen dataclass + cosine helper + the course-scope query + `async resolve_candidate(...)` running the ladder and writing one `DedupDecision`. <800 lines (will be ~150). |
| `apollo/provisioning/__init__.py` | **EDIT** | add `from apollo.provisioning.dedup import DedupVerdict, resolve_candidate` and extend `__all__` to include `"DedupVerdict", "resolve_candidate"`. Mirror the existing flat re-export style. |
| `apollo/provisioning/tests/test_dedup.py` | **NEW** | the full Tier-1 + real-PG suite below. |
| `docs/architecture/apollo.md` | **EDIT** | register `dedup.py`/`dedup_constants.py` in the module-map row + Public-interfaces; bump `last_verified` (already `2026-06-19`). See Owner-doc section. |

**Internal structure of `dedup.py` (pure helpers kept separately testable):**

- `_cosine(a, b) -> float` — numpy dot / (norm·norm); returns `0.0` when either vector is
  all-zero (guard div-by-zero) so a degenerate embedding can never spuriously merge.
- `async _in_course_entities(db, *, search_space_id, concept_id) -> list[KGEntity]` — the
  COURSE-SCOPE query (see invariant section). Returns ONLY entities whose concept's subject's
  `search_space_id` matches AND whose `scope_summary IS NOT NULL`. This is the single
  load-bearing course filter; it runs BEFORE any `_cosine` call.
- `async _record_decision(db, *, search_space_id, concept_id, candidate_key, method, similarity, verdict, matched_entity_id, ingest_run_id) -> None` — constructs and `db.add(DedupDecision(...))` + `await db.flush()` (one row). Immutable: builds a new ORM row, never mutates inputs.
- `async resolve_candidate(...)` — orchestrates: slug tier → embedding tier → judge tier,
  records exactly one decision on whichever tier terminates, returns the `DedupVerdict`.

## The ladder (course-scoped, first-writer-wins)

`resolve_candidate` runs the in-course inventory through three tiers, short-circuiting on the
first that produces a verdict, and writes EXACTLY ONE `apollo_dedup_decisions` row:

1. **SLUG tier** — among the in-course entities (from `_in_course_entities`), find one whose
   `canonical_key == candidate.canonical_key`. If found →
   `DedupVerdict('merged', 'slug', None, matched.id)`; record `method='slug', similarity=None`.
   *(Note: the slug match itself does not require a non-null `scope_summary`; resolve the slug
   set from the same course-scoped concept set but WITHOUT the `scope_summary IS NOT NULL`
   filter — see invariant. Only the embedding tier needs non-null summaries.)*
2. **EMBEDDING tier** — embed `candidate.scope_summary` once via `embed_fn`; for each in-course
   entity WITH a non-null `scope_summary`, embed it via `embed_fn` and compute `_cosine`. Take
   the MAX cosine and its entity:
   - `max_cos >= EMBED_MERGE_THRESHOLD (0.92)` → `merged`, `method='embedding'`,
     `similarity=max_cos`, `matched_entity_id=best.id`.
   - `EMBED_JUDGE_BAND` i.e. `0.82 <= max_cos < 0.92` → ESCALATE to the judge tier (carry
     `best` + `max_cos` forward).
   - `max_cos < 0.82` → `distinct`, `method='embedding'`, `similarity=max_cos`,
     `matched_entity_id=None`.
   - If there are NO in-course entities with a `scope_summary` (empty embed set) → `distinct`,
     `method='embedding'`, `similarity=None`, `matched_entity_id=None` (nothing to compare).
3. **LLM-JUDGE tier (tiebreaker)** — only reached from the band. Call
   `judge_fn(candidate.scope_summary, best.scope_summary, search_space_id=search_space_id)`
   → `'merged'` or `'distinct'`. Record `method='llm_judge'`, `similarity=max_cos` (the band
   cosine that triggered the escalation, for the audit trail), `matched_entity_id=best.id` on
   merge else `None`.

**First-writer-wins determinism:** when multiple in-course entities tie on the embedding tier,
break the tie deterministically by LOWEST `KGEntity.id` (the earliest-written entity wins —
matches "a later material may add problems but not rewrite a concept's established vocabulary",
§8B.2:1297). The MAX-cosine selection therefore uses `max(..., key=lambda pair: (cos, -id))`
so equal cosines resolve to the smallest id. The slug tier likewise picks the lowest-id match
if (defensively) more than one exists. This makes the verdict a pure function of DB state +
inputs — the property the determinism tests pin.

## Course-scope invariant (load-bearing §1.4)

THE load-bearing assertion of this unit (§8B.7). The course filter MUST be a WHERE clause that
runs BEFORE any cosine is computed — never a global similarity search filtered afterward.

`_in_course_entities` issues (SQLAlchemy `select`):

```
select(KGEntity)
  .join(Concept, Concept.id == KGEntity.concept_id)
  .join(Subject, Subject.id == Concept.subject_id)
  .where(Subject.search_space_id == search_space_id)
  .where(KGEntity.scope_summary.is_not(None))   # embedding tier only; see slug note above
```

The `concept_id` argument is used to scope the SLUG tier exact-match (same concept) and is the
value stamped onto the decision row; the EMBEDDING/JUDGE comparison is over the whole COURSE's
inventory (all concepts in this `search_space_id`) per §8B.5 "course-scoped dedup". The
invariant is: **two courses that each contain an entity with byte-identical `scope_summary`
text (hence identical deterministic mock embeddings) MUST resolve `distinct`, because the
`search_space_id` WHERE removes the other course's entity from the candidate set BEFORE cosine
is ever consulted.** A mutation that drops the `Subject.search_space_id` predicate causes a
cross-course false-merge — the cross-course test must RED on exactly that mutation.

## TDD task order

Strict RED→GREEN. Tests are REAL and DISCRIMINATING (no skip/xfail/assert-nothing).

1. **RED — constants.** Write `test_constants_are_pinned` asserting `EMBED_MERGE_THRESHOLD == 0.92` and `EMBED_JUDGE_BAND == (0.82, 0.92)`. Create `dedup_constants.py` to GREEN it. (Cheap anchor so a later threshold drift is caught at the constant, independent of routing.)
2. **RED — cosine helper + `DedupVerdict` shape.** Write the pure-math tests (`test_cosine_*`, `test_dedupverdict_is_frozen`). Create `dedup.py` with `_cosine` + the frozen dataclass to GREEN.
3. **RED — slug tier (real-PG).** Write `test_slug_exact_match_merges`. Implement `_in_course_entities` (slug variant) + the slug branch + `_record_decision` to GREEN.
4. **RED — embedding band routing (Tier-1, mock embeddings).** Write the four band tests (`>=0.92 merged`, `0.82<=c<0.92 escalates`, `<0.82 distinct`, `empty-inventory distinct`). Implement the embedding branch to GREEN.
5. **RED — judge tier.** Write `test_judge_merges` + `test_judge_distincts` with a fixed `judge_fn` stub. Implement the escalation branch to GREEN.
6. **RED — the decision-row write (real-PG), one row per path.** Write the per-tier `dedup_decisions` row assertions. Confirm `_record_decision` writes exactly one row with the right `method/similarity/verdict/matched_entity_id/candidate_key/search_space_id/concept_id/ingest_run_id`.
7. **RED — THE cross-course non-merge proof (real-PG, load-bearing).** Write `test_cross_course_identical_embeddings_stay_distinct`. Ensure the course-scope WHERE makes it GREEN; verify it goes RED if the `search_space_id` predicate is removed (mutation note in the test docstring).
8. **RED — determinism / first-writer-wins.** Write `test_embedding_tie_breaks_to_lowest_entity_id` + `test_resolve_is_deterministic_repeat`. Confirm GREEN.
9. **Wire `__init__.py`** re-export; add `test_public_api_reexport`.
10. **Owner doc** `apollo.md` update (same commit).
11. **Coverage gate** — run diff-cover; fill any uncovered changed line with a real assertion (never a pragma unless genuinely unreachable defense-in-depth, documented inline like `promotion_lint.py:353`).

## Full test list (Tier-1 unit + real-PG)

File: `apollo/provisioning/tests/test_dedup.py`. Module docstring states: Tier-1 only, no
network — `embed_fn`/`judge_fn` are deterministic injected stubs; real-PG tests use
`db_session` (Docker-skip clean). Two stub helpers at module top:

- `_embed(text) -> list[float]`: a DETERMINISTIC mock embedder. Map a small set of fixture
  texts to fixed unit-ish vectors so cosines are controllable. E.g. a dict
  `{"A": [1,0,0], "B": [0,1,0], "A-near": [0.95, 0.31, 0], ...}` returning the vector for
  the text, default `[1,0,0]` for unknowns. Identical text → identical vector (the property
  the cross-course test relies on). NOT a call to `embed_text`.
- `_judge_merged` / `_judge_distinct`: fixed stubs `lambda *a, **k: "merged"` / `"distinct"`.
- `_Candidate` dataclass/namedtuple fixture with `canonical_key: str`, `scope_summary: str`.
- `_seed_course(db, *, slug, entities)`: mirrors `test_resolution_resolves_to_postgres.py:35-58`
  — creates `SearchSpace → Subject → Concept`, then `KGEntity` rows
  `(canonical_key, kind, display_name, scope_summary)`; returns `(search_space_id, concept_id, {canonical_key: entity_id})`.

### Tier-1 pure (no DB) — `_cosine`, constants, dataclass

| Test | Asserts | Mocking |
|---|---|---|
| `test_constants_are_pinned` | `EMBED_MERGE_THRESHOLD == 0.92`; `EMBED_JUDGE_BAND == (0.82, 0.92)`. DISCRIMINATING: a moved threshold REDs here. | none |
| `test_cosine_identical_is_one` | `_cosine([1,0,0],[1,0,0]) == pytest.approx(1.0)`. | none |
| `test_cosine_orthogonal_is_zero` | `_cosine([1,0,0],[0,1,0]) == pytest.approx(0.0)`. | none |
| `test_cosine_zero_vector_is_zero_not_nan` | `_cosine([0,0,0],[1,0,0]) == 0.0` (no div-by-zero / NaN). | none |
| `test_cosine_is_magnitude_invariant` | `_cosine([2,0,0],[5,0,0]) == pytest.approx(1.0)`. | none |
| `test_dedupverdict_is_frozen` | constructing then setting an attr raises `FrozenInstanceError`; field order/types match the pinned API. | none |

### Tier-1 ladder routing (real-PG `db_session`, mock embed/judge)

Each seeds ONE course and resolves a candidate; assertions cover the verdict AND the single
audit row. `pytestmark = pytest.mark.asyncio`; real-PG tests request `db_session`.

| Test | Setup | Asserts (verdict) | Asserts (dedup_decisions row) |
|---|---|---|---|
| `test_slug_exact_match_merges` | in-course entity `canonical_key='eq.bernoulli'`, candidate same key | `verdict='merged', method='slug', similarity is None, matched_entity_id == that entity id` | exactly 1 row: `method='slug', similarity IS NULL, verdict='merged', matched_entity_id==id, candidate_key=='eq.bernoulli', search_space_id/concept_id set` |
| `test_embedding_at_threshold_merges` | candidate embeds to cosine `>= 0.92` vs one in-course entity (use `_embed` map giving exactly 0.92 or above) | `merged, method='embedding', similarity==max_cos (>=0.92), matched_entity_id==id` | 1 row `method='embedding', similarity≈max_cos, verdict='merged'`; **also assert the boundary 0.92 is INCLUSIVE (merges)** |
| `test_embedding_in_band_escalates_to_judge` | cosine in `[0.82, 0.92)`; `judge_fn=_judge_merged` | `merged, method='llm_judge', similarity==band_cos, matched_entity_id==id` | 1 row `method='llm_judge'` (NOT 'embedding') — proves escalation, not an embedding-merge |
| `test_embedding_below_band_is_distinct` | cosine `< 0.82`; assert the judge is NEVER called (pass a `judge_fn` that raises) | `distinct, method='embedding', similarity==max_cos, matched_entity_id is None` | 1 row `method='embedding', verdict='distinct', matched_entity_id IS NULL`. DISCRIMINATING: if `0.82` lower-bound moves, routing flips → RED |
| `test_band_lower_bound_0_82_escalates_not_distinct` | cosine == exactly `0.82`; `judge_fn=_judge_distinct` | `distinct, method='llm_judge'` (0.82 is IN-band, escalates; judge says distinct) | 1 row `method='llm_judge', verdict='distinct'`. Pins the inclusive lower / the band semantics |
| `test_judge_distinct_path` | cosine in band; `judge_fn=_judge_distinct` | `distinct, method='llm_judge', matched_entity_id is None` | 1 row `method='llm_judge', verdict='distinct', similarity==band_cos` |
| `test_empty_inventory_is_distinct` | course has NO entity with a `scope_summary` (or none at all); no slug match | `distinct, method='embedding', similarity is None, matched_entity_id is None` | 1 row `method='embedding', verdict='distinct', similarity IS NULL` |
| `test_null_scope_summary_entities_are_skipped` | one in-course entity with `scope_summary=NULL` that would slug-MISS and one with a summary; candidate matches the summary one by embedding | resolves against the non-null one only; the NULL one never embedded (would KeyError in `_embed` if attempted) | row matches the non-null entity |

### Tier-1 — THE load-bearing cross-course proof (real-PG)

| Test | Asserts |
|---|---|
| `test_cross_course_identical_embeddings_stay_distinct` | Seed TWO courses (`course-a`, `course-b`), EACH with one "incompressible" entity whose `scope_summary` is the SAME text (→ identical `_embed` vector). Resolve a candidate with that SAME text against `course-a`'s `search_space_id`/`concept_id`. Assert `verdict=='distinct'` and `matched_entity_id` is `None` (or, if a same-course slug were present it'd be excluded by design — here courses differ). The merge-by-embedding would fire IF the other course's entity were in scope; it is NOT, because `_in_course_entities` applies the `search_space_id` WHERE first. The dedup_decisions row is `search_space_id == course-a` only. **Docstring pins the mutation: dropping `.where(Subject.search_space_id == search_space_id)` makes this RED (a cross-course false-merge) — the orchestrator's independent-mutation check.** |

### Tier-1 — determinism / first-writer-wins (real-PG)

| Test | Asserts |
|---|---|
| `test_embedding_tie_breaks_to_lowest_entity_id` | two in-course entities with IDENTICAL `scope_summary` (→ equal max cosine, both `>= 0.92`); assert `matched_entity_id` is the LOWER id (earliest writer wins). RED if the tie-break key changes. |
| `test_resolve_is_deterministic_repeat` | calling `resolve_candidate` twice with the same DB state + inputs yields equal `DedupVerdict`s and writes a SECOND identical-shaped audit row (proves no hidden mutation of inputs/state changes the verdict). |

### Tier-1 — public surface

| Test | Asserts |
|---|---|
| `test_public_api_reexport` | `from apollo.provisioning import DedupVerdict, resolve_candidate` works and they are the same objects as in `apollo.provisioning.dedup`. |

**Coverage note:** every branch of `resolve_candidate` (slug-hit, embed-merge, band-escalate,
embed-distinct, empty-inventory, judge-merge, judge-distinct) plus `_cosine`'s zero-guard is
covered by a row above → ≥95% patch coverage. The only acceptable `# pragma: no cover` is a
genuinely-unreachable defensive guard, documented inline with WHY (mirroring
`promotion_lint.py:353`); do NOT pragma a reachable branch to game the gate.

## Owner-doc updates (drift contract)

Owner doc: `docs/architecture/apollo.md`. Its `owns:` already includes `apollo/**`, so no
glob change is needed. `last_verified` is already `2026-06-19` — keep it (re-confirm, do not
regress). Two edits in the SAME commit as the code:

1. **Module-map row (line 39).** The `apollo/provisioning/` row currently lists
   `__init__.py, promotion_lint.py, problem_hash.py` and describes only WU-3B2b. Append
   `dedup.py, dedup_constants.py` to the key-files cell and add a sentence to the role cell:
   > **WU-3B2c — the §8B.5 course-local dedup ladder.**
   > `resolve_candidate(db, *, search_space_id, concept_id, candidate, embed_fn, judge_fn, ingest_run_id=None) -> DedupVerdict(verdict, method, similarity, matched_entity_id)`
   > runs slug → `scope_summary`-embedding cosine → injected LLM-judge, course-scoped via the
   > `Subject.search_space_id` WHERE applied BEFORE similarity (§1.4 — two courses never merge),
   > first-writer-wins (lowest entity id), writing one `apollo_dedup_decisions` row per
   > resolution. Embedding source is the on-the-fly-embedded `KGEntity.scope_summary` (no
   > persisted vector). `embed_fn`/`judge_fn` injected (3B2d/3B2f own the real wiring).
   > Constants `EMBED_MERGE_THRESHOLD=0.92` / `EMBED_JUDGE_BAND=(0.82,0.92)` in `dedup_constants.py`.
2. **Public interfaces section.** Add a `resolve_candidate` / `DedupVerdict` entry beside the
   existing `run_promotion_lint` entry, with the pinned signature and the course-scope
   invariant one-liner.

Keep additions terse and factual; do not restate the whole ladder. Verify with
`grep -n "resolve_candidate" docs/architecture/apollo.md` (must appear).

## Risks (confidence-rated)

- **[HIGH confidence / load-bearing] Course-scope WHERE ordering.** The single most important
  correctness property. Mitigation: `_in_course_entities` is the ONLY path to candidates and
  it applies the `search_space_id` predicate in SQL; `_cosine` is never called on a raw global
  set. The cross-course test RED-on-mutation pins it. LOW risk of slipping past review.
- **[MEDIUM] `embed_fn`/`judge_fn` are SYNC, called inside an async fn.** `embed_text` and
  `cheap_chat` are synchronous (verified). Calling a sync fn inside `async resolve_candidate`
  is fine (no await); do NOT `await embed_fn(...)`. If a future caller injects an async fn this
  breaks — out of scope for v1 (the pinned API types both as sync). Documented in the API note.
- **[MEDIUM] Boundary semantics of the band.** `>=0.92` merges (inclusive), `0.82<=c<0.92`
  escalates (lower-inclusive, upper-exclusive), `<0.82` distinct. Two boundary tests
  (`test_embedding_at_threshold_merges` @0.92, `test_band_lower_bound_0_82_escalates_not_distinct`
  @0.82) pin the inclusivity so a `>`/`>=` slip REDs.
- **[MEDIUM] Empty-/null-summary inventory.** A course with no `scope_summary` rows must return
  `distinct` (not crash, not merge). Covered by `test_empty_inventory_is_distinct` +
  `test_null_scope_summary_entities_are_skipped`.
- **[LOW] Docker-down real-PG skip.** `db_session` skips cleanly if Docker is down — but the
  gate REQUIRES the real-PG tests run GREEN-not-skipped. The executor MUST run with Docker up
  (`pgvector/pgvector:pg16`) and confirm the dedup tests are collected+passed, not skipped.
- **[LOW] `apollo_dedup_decisions` CHECK constraints are SQL-only.** Real-PG tests run on the
  ORM `create_all` schema (no SQL CHECK). The plan's `method`/`verdict` values are already in
  the allowed vocab, so this is moot; do NOT add ORM CHECKs (repo convention).
- **[LOW] Determinism under multiprocessing.** `clear_cache()`-style global state is NOT a
  concern here (no SymPy). numpy cosine is pure. Tie-break by lowest id removes the only
  nondeterminism source.

## Deviations the executor may take

- **Cosine impl:** numpy OR stdlib `math` — either is fine (no new package). Prefer numpy
  (present, vectorized) but `math.sqrt(sum(...))` is acceptable if it reads cleaner.
- **Stub-embedding representation:** the `_embed` map's exact vectors are the executor's
  choice as long as the resulting cosines land in the intended bands (compute and assert the
  cosine explicitly in-test rather than hard-coding a fragile float). It is acceptable to
  derive band vectors with a tiny helper that builds a unit vector at a target cosine angle.
- **`_record_decision` flush vs commit:** use `db.flush()` (the savepoint fixture rolls back);
  do not `commit()` unless mirroring an existing apollo write helper — flush suffices to read
  the row back in-test.
- **Candidate duck-typing vs a tiny shared type:** the executor MAY define a minimal frozen
  `Candidate`-like input in `dedup.py` IF 3B2d's needs are clearer at build time, but the
  PINNED `resolve_candidate` signature and the two consumed attributes
  (`canonical_key`, `scope_summary`) must not change. Default: duck-type, define the fixture
  type only in the test.
- **Extra internal helpers / private fns** are fine if each stays covered and files stay
  <800 lines.
- NOT negotiable: the public API signature, the 0.92/0.82 constants, the course-scope-before-
  similarity ordering, one-row-per-resolution, no new package, no migration, no scrape/mint.

## Verification commands

Run with the repo interpreter `.venv/Scripts/python.exe` and Docker UP (real-PG tier).

```bash
# Tier-1 + real-PG dedup suite (must be GREEN, NOT skipped):
.venv/Scripts/python.exe -m pytest apollo/provisioning/tests/test_dedup.py -v --tb=short

# Confirm the provisioning package still imports + re-exports:
.venv/Scripts/python.exe -c "from apollo.provisioning import DedupVerdict, resolve_candidate; print('ok')"

# Owner-doc registration present:
grep -n "resolve_candidate" docs/architecture/apollo.md

# Patch coverage gate (>=95% vs the 3B2b branch):
.venv/Scripts/python.exe -m pytest --cov=. --cov-report=xml apollo/provisioning/tests/test_dedup.py
diff-cover coverage.xml --compare-branch=feat/apollo-kg-wu3b2b-promotion-lint --fail-under=95
```

Notes: a bare `python` ImportError is interpreter-selection, not a blocker — use the `.venv`
interpreter. If the real-PG tests report SKIPPED, Docker is down; the gate is NOT satisfied
until they run GREEN. NEVER apply migrations to any remote DB. No new package — if a step
seems to need one, STOP and escalate to the orchestrator.
