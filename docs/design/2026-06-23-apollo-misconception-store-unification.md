---
title: Apollo misconception store unification (close the D5 dual-storage footgun)
date: 2026-06-23
status: implementation-ready
owner-doc: docs/architecture/apollo.md
spec-lineage:
  - docs/superpowers/specs/2026-06-10-apollo-kg-learner-model-architecture-decision.md (§8B, D5)
  - docs/superpowers/specs/2026-03-27-eval-system-design.md
chosen-option: C — read-adapter union inside load_for_concept (no migration, no writer change)
migration-number: none consumed (next free remains 031)
branching: cut `fix/apollo-misconception-read-union` off `staging`; PR into `staging`; never touch `ApolloV3`
---

# Apollo misconception store unification — design

## 0. TL;DR

Apollo stores misconceptions in **two** Postgres tables that never sync:

- **Store 1 — `apollo_misconceptions`** (migration 019): the *only* store the
  Done/soundness **grading** path reads (`misconception_bank.load_for_concept`).
- **Store 2 — `apollo_kg_entities` rows with `kind='misconception'`** (migration
  026, altered by 030): the store the **learner model / personalization / canon
  projection / auto-provisioning** read & write.

Seeding the learner model (store 2) does **not** populate store 1, so grading
sees zero misconceptions unless a *second*, disconnected seeder
(`scripts/_macro_seed_misconceptions.py`) is also run by hand. That is the **D5
dual-storage footgun**.

The investigation proved this is a **read-time gap, not a write-time
divergence**: the sole live grading consumer (`done_grading._misconceptions_dict`,
lines 130–140) already **discards** `probe_question` / `rt_steps` and **hard-codes
`opposes=None`**, and *no live Socratic loop consumes those fields at all* (the
per-turn confused-learner path was deleted in `chat.py` v1; `infer_misconception`
is dead). Grading provably needs only `code`, `description`, and
`trigger_phrases` — all of which store 2 already carries
(`canonical_key` / `payload.description` / `aliases`).

**Chosen fix (Option C):** a Python **read-adapter union** inside
`load_for_concept` that folds store-2 `kind='misconception'` rows into the
existing `MisconceptionEntry` shape (store 1 winning on
`code == canonical_key`, Socratic fields synthesized empty). **No migration. No
schema change. No writer change. No behavior change to conflict-pair detection.**
One function body changes; one pure projector helper is added; tests are mostly
pure-unit plus one integration regression lock.

---

## 1. Problem statement — the D5 dual-storage footgun

### 1.1 Two stores, one bridge, no sync

| | Store 1 | Store 2 |
|---|---|---|
| Table | `apollo_misconceptions` | `apollo_kg_entities` (`kind='misconception'`) |
| Migration | `019_apollo_misconceptions.sql` | `026_apollo_learner_model.sql` (+ `030` adds `scope_summary`) |
| Stable key | `code` (`UNIQUE(concept_id, code)`) | `canonical_key` (`UNIQUE(concept_id, canonical_key)`) |
| Read by | **grading only** | learner model / personalization / canon projection / dedup |
| Written by | macro seeder (`_macro_seed_misconceptions.py`) | learner-model seeder + auto-provision mint |

The **only** bridge between them is a *string equality* consumed at grade time:
grading resolves a store-1 `code` against store-2's
`canon_key_by_canonical_key` map. See `apollo/handlers/done_grading.py:194`:

```python
# done_grading.py:189-200 (excerpt)
specs = await load_entity_specs(db, search_space_id=..., concept_id=sess.concept_id)
canon_key_by_canonical_key = {spec.canonical_key: spec.key for spec in specs}
inputs = build_problem_candidates(
    problem_payload,
    misconceptions,                         # <- built from STORE 1 only
    canon_key_by_canonical_key=canon_key_by_canonical_key,  # <- STORE 2 surrogate ids
)
```

`misconceptions` here is built from **store 1 only** (`load_for_concept`,
`done_grading.py:186-187`). Nothing copies store 2 → store 1. **Seeding the
learner model does not make grading see a single misconception.**

### 1.2 The footgun, in the seeder's own words

`scripts/_macro_seed_misconceptions.py:4-10` (the only writer of store 1 that
runs in practice) documents the trap verbatim:

```text
WHY: the graph-grading Done chain loads misconceptions via
    apollo.overseer.misconception_bank.load_for_concept — which reads the
    apollo_misconceptions TABLE, NOT the apollo_kg_entities rows the
    learner-model seeder mints. So without this, the macro weak variations have
    no misconception candidates to resolve to, and the soundness/contradiction
    dimension is vacuously 1.0.
```

So a developer who "seeds Apollo" by running the learner-model seeder
(`scripts/seed_apollo_learner_model.py`) gets a fully populated learner model
and a **silently vacuous grader** (soundness == 1.0 because the candidate set is
empty). The two authoring actions are disconnected; forgetting the second one is
the footgun.

### 1.3 The split, with file:line evidence

**Grading reads store 1 only:**

- `apollo/handlers/done_grading.py:186` →
  `entries = await load_for_concept(db, concept_id=sess.concept_id)`
- `apollo/overseer/misconception_bank.py:83-88` —
  `select(Misconception).where(Misconception.concept_id == concept_id)` (store 1
  ORM model only; no `KGEntity` query).
- `apollo/handlers/done_grading.py:120-140` — `_misconceptions_dict` projects
  each `MisconceptionEntry` to `{key, trigger_phrases, opposes, display_name}`
  and **drops `probe_question` / `rt_steps`**, **hard-codes `opposes: None`**.

**Learner model / personalization / canon projection read store 2 only:**

- `apollo/learner_model/personalization_read.py:91-95` — `read_learner_profile`
  selects `id, canonical_key FROM apollo_kg_entities WHERE concept_id=?` (no kind
  filter; includes misconceptions).
- `apollo/knowledge_graph/canon_projection.py:117-159` — `load_entity_specs`
  reads all kg_entities for the scope (`:131` explicitly includes
  misconceptions) → `canon_key_by_canonical_key`.
- `apollo/provisioning/tag_mint_persist.py:169-202` — `link_opposes` selects
  `KGEntity WHERE concept_id=? AND kind='misconception'`.

**Writers are split too (no writer touches both tables):**

- Store 1: `scripts/_macro_seed_misconceptions.py:83-90` (raw ORM `s.add`).
- Store 2 seed: `apollo/persistence/learner_model_seed.py:247-265`
  (`misconceptions_to_entities`) persisted by
  `scripts/seed_apollo_learner_model.py`.
- Store 2 auto: `apollo/provisioning/tag_mint.py:234-266` via
  `tag_mint_persist.py:126` (`upsert_entity`). **Mints zero today** —
  `apollo/provisioning/solution.py:332` passes `misconceptions=[]`, and the
  orchestrator never injects any (`orchestrator.py:488-490`).
- The "obvious" dual-write entry point `misconception_bank.upsert_entry`
  (`misconception_bank.py:174-237`) is **defined but never called** in the live
  path (only referenced by its `__all__` export and an importability smoke test;
  the seeder uses raw ORM `s.add`, not `upsert_entry`).

### 1.4 Why the Socratic-field asymmetry is a *non-issue* for the fix

The reason store 2 exists at all is the documented deviation at
`apollo/provisioning/tag_mint.py:36-40`: auto-provisioning **cannot responsibly
author** the NOT-NULL-without-default `probe_question` (a Socratic confused-tutee
voice), so the mint path writes kg_entities instead of store 1. The
investigation confirms those Socratic fields have **zero live readers**:

- `apollo/handlers/chat.py:280-283` — v1 per-turn loop is "nodify + dumb reply.
  **No sufficiency, misconception, OLM-invite, or output filter.**"
- `apollo/overseer/misconception.py:295` `infer_misconception` (the only code
  that reads `probe_question` / `rt_steps` into a `MisconceptionSignal`) has
  **no live caller** (grep: only its own def + `__all__`).
- Even on the write side, the macro seeder already stores `rt_steps=[]` and
  `probe_question=entry.get("probe_question", "")`
  (`_macro_seed_misconceptions.py:88-89`) — i.e. the Socratic payload is mostly
  vacuous in store 1 today anyway.

So a fix that *reads back* kg_entities-origin misconceptions with **empty
Socratic fields** loses nothing any live consumer reads. This is the keystone
that makes Option C correct rather than a hack.

---

## 2. Chosen approach and why

### 2.1 Decision: Option C — read-adapter union inside `load_for_concept`

Make grading's `load_for_concept` return the **union** of:

1. store-1 `apollo_misconceptions` rows (existing query), **plus**
2. store-2 `apollo_kg_entities` rows with `kind='misconception'`, projected into
   `MisconceptionEntry` with synthesized safe defaults
   (`probe_question=""`, `rt_steps=()`, `confusion_pair=None`),

**deduplicated by `code` / `canonical_key`, store 1 winning** on conflict (it
carries real Socratic fields when hand-authored). The function signature, return
type (`list[MisconceptionEntry]`), and frozen-dataclass contract are unchanged,
so `_misconceptions_dict` and every other caller are untouched.

The result: **seeding the learner model (store 2) — and tomorrow's
auto-provisioned `kind='misconception'` mints — are automatically visible to
grading**, with no schema change, no writer change, no migration, and no
behavior change to conflict-pair detection.

### 2.2 The four load-bearing observations that force Option C

1. `done_grading._misconceptions_dict` (lines 130-140) already projects
   `MisconceptionEntry` → `{key, trigger_phrases, opposes, display_name}` and
   **drops `probe_question` / `rt_steps` entirely**. Grading provably needs only
   `code`, `description`, `trigger_phrases`.
2. The readers enumeration confirms the Socratic fields have **zero live
   consumers** (chat.py v1 deleted the loop; `infer_misconception` is dead).
3. `_misconceptions_dict` already hard-codes `opposes: None` — so routing
   grading through store 2 (which carries a real `payload.opposes_entity_key`)
   would *activate* dormant opposes-conflict detection, a behavior change to
   avoid in v1. Option C keeps `opposes=None` and changes nothing.
4. `MisconceptionEntry` is the clean seam: a frozen dataclass that
   `load_for_concept` returns and `_misconceptions_dict` consumes. Anything that
   produces a `list[MisconceptionEntry]` is grading-compatible.

### 2.3 Why the key-identity invariant makes the dedup correct

Grading already relies on `code == canonical_key` (the `misc.<name>`
convention): `_macro_seed_misconceptions.py:73` comments
`code = entry["key"]  # 'misc.<name>' — the load-bearing prefix`, and
`misconceptions_to_entities` sets `canonical_key=entry["key"]`
(`learner_model_seed.py:255`). Both seeders read the *same on-disk*
`misconceptions.json` and use the same `key`. So a store-1 `code` and its
store-2 twin `canonical_key` are byte-identical — exactly the join grading needs
at `done_grading.py:194`, and exactly the dedup key Option C uses. Dedup is
therefore a string-equality merge on a key both stores already agree on.

### 2.4 Rejected alternatives

#### (A) UNIFY — single table / grading reads kg_entities

Collapse to one store, or point grading at `apollo_kg_entities`.

**Rejected — the embedding is the killer.** Grading's `match_by_embedding`
(`misconception_bank.py:91-171`) depends on `description_embedding vector(3072)`
+ its HNSW halfvec index, and `apollo_kg_entities` has **no persisted vector at
all** (migration 030 comment, line 45: "NO persisted vector"; `scope_summary` is
embedded on the fly). Unifying onto kg_entities means *adding* that column +
HNSW index + a backfill — all to serve a NN-search path that is itself **dead
live** (only caller is `infer_misconception`). Unifying onto
`apollo_misconceptions` means relocating `kind` / `canonical_key` /
`payload.opposes_entity_key` / `aliases` / `scope_summary` and rewiring three
store-2 readers (K1 `link_opposes`, K2 `load_entity_specs`, K3
`read_learner_profile`), changing the id↔key inventory they depend on. Either
direction is a multi-week schema project to fix a read gap, and it would have to
relax store 1's NOT-NULL `probe_question`, permanently weakening it.

#### (B) SYNC / DUAL-WRITE — every writer hits both tables (+ one-time backfill)

Make every misconception writer fan out to both stores; backfill once so they
agree at cutover.

**Rejected — it forces the irresponsible write the codebase already refused.**
The auto-mint path (`tag_mint`) provably *cannot* author `probe_question` — the
documented deviation at `tag_mint.py:36-40` is the entire reason store 2 exists.
B either violates that decision or writes empty `probe_question=''` everywhere,
re-introducing the footgun's *spirit* (every **future** writer must remember the
twin table, or grading silently goes blind again). The footgun **moves** rather
than closes. It also needs a populated-table backfill migration (031) with the
two-phase NOT-NULL landmine.

### 2.5 Tradeoff table

| Dimension | (A) UNIFY | (B) DUAL-WRITE | **(C) READ-ADAPTER UNION (chosen)** |
|---|---|---|---|
| **Blast radius** | **Largest** — grading reader, `match_by_embedding` (needs new `vector(3072)`+HNSW on kg_entities), `read_learner_profile` (K3), `load_entity_specs` (K2), `link_opposes` (K1), both seeders, auto-mint, Neo4j canon projection; changes K2/K3 id↔key inventory. | **Medium-large** — all 3 live writers (#1 macro seeder, #3 learner seeder, #4 tag_mint) + one-time backfill; footgun moves to "add a writer → forget the twin." | **Smallest** — one function body (`load_for_concept`) + one pure helper. Zero writers, zero schema, zero other readers. `_misconceptions_dict` consumes the result unchanged. |
| **Migration complexity** | **High** — 031 must reconcile one canonical key, **relocate `vector(3072)` + HNSW**, satisfy NOT-NULL-no-default `probe_question` for existing/auto rows, two-phase NOT-NULL tighten on a populated table; multiple constraint-branch tests. | **Low-medium** — a 031 backfill migration (copy store-2 misconceptions → store-1 with synthesized `probe_question=''`); still a populated-table backfill landmine. | **None** — no 031 at all. Pure code change behind the existing 95% patch gate. |
| **NOT-NULL Socratic (`probe_question` no-default, `rt_steps`)** | Must drop/relax NOT-NULL on `probe_question` — permanently weakens store 1. | Must synthesize `probe_question=''`/`rt_steps='[]'` at every kg-origin write; NOT-NULL stays but is satisfied with empties everywhere. | **Cleanest** — defaults synthesized **in memory** at read time, never persisted. Store 1's NOT-NULL is untouched and still enforces real authoring when a human hand-authors. kg-only rows simply read back with empty Socratic fields — exactly what `_misconceptions_dict` discards anyway. |
| **Compat with FUTURE auto-provisioning (writes kg_entities)** | Native *iff* grading reads kg_entities — but pays the embedding/inventory upheaval. | Forces `tag_mint` to *also* write store 1 — the exact write `tag_mint.py:36-40` calls irresponsible (or empty-string writes that re-create the footgun). | **Best** — the day auto-provisioning mints `kind='misconception'` rows (today: zero, `solution.py:332`), grading sees them **for free** through the union, no `probe_question` ever required. Forward-compatible *by construction*; respects the deviation note instead of fighting it. |
| **Test cost (95% patch, LOCAL only)** | **Highest** — 031 constraint-branch tests, embedding/HNSW relocation tests on pgvector container, regressions for K1/K2/K3 inventory + opposes-activation. | **High** — backfill migration test (populated pre-031 + `_expect_violation` SAVEPOINT) + a dual-write test per writer + cross-table idempotency. | **Lowest** — mostly pure-unit (one projector), plus **one** `db_session` integration test (the D5 regression lock). Reuses `test_misconception_bank.py`'s ORM-seed pattern. No migration test. |
| **Behavioral risk (opposes / conflict-pair)** | Activates dormant opposes-conflict detection (store 2 carries real `opposes_entity_key`; grading currently forces `opposes=None`). Must validate. | Same risk if grading later reads the synced store. | **Controlled** — projector emits `confusion_pair=None`, `_misconceptions_dict` keeps forcing `opposes: None` → **zero behavior change**. opposes can be wired later as a deliberate, separately-tested step. |
| **Reversibility** | Hard (schema + data migrated). | Hard (data backfilled into both). | **Trivial** — one function; revert the diff. |

### 2.6 One-paragraph rationale

The dual-storage footgun looks like a write-time divergence but the
readers/grading investigation proves it is a **read-time** gap: the sole live
grading consumer (`done_grading._misconceptions_dict`) already discards
`probe_question` / `rt_steps` and forces `opposes=None`, and no live Socratic
loop consumes those fields at all (deleted in `chat.py` v1; `infer_misconception`
dead). So grading needs only `code` / `description` / `trigger_phrases`, all of
which store 2 (`apollo_kg_entities` `kind='misconception'`) carries via
`canonical_key` / `payload.description` / `aliases`. The minimal correct fix is a
Python union inside `load_for_concept` that folds store-2 misconceptions into the
existing `MisconceptionEntry` shape (store 1 winning on `code == canonical_key`,
Socratic fields synthesized empty), making "seed the learner model" — and
tomorrow's auto-provisioned mints — automatically visible to grading, with no
schema change, no writer change, no migration, no behavior change to conflict-pair
detection, and the lowest test cost. It respects, rather than fights, the
documented `tag_mint.py:36-40` deviation.

---

## 3. Migration

### 3.1 No migration is consumed

**Option C requires NO migration.** This is a primary reason it wins. The on-disk
directory tops out at `030_apollo_autoprovisioning.sql`, so the next free number
is **031** — and Option C **does not consume it**. It remains free for whoever
needs it next.

(Verified on disk: the migrations directory ends
`027_apollo_drop_concept_cluster_id.sql`, `028_apollo_learner_janitor.sql`,
`029_chat_turn_keywords.sql`, `030_apollo_autoprovisioning.sql`, `__init__.py`.
The historical `023_*` two-file collision — `023_apollo_auth_scoping.sql` +
`023_chunks_halfvec_hnsw.sql` — confirms numbering is by filename prefix, but is
not relevant here since 031 is untouched.)

### 3.2 For the record: what 031 WOULD have contained under A or B (NOT applied)

This block exists only to document the cost Option C avoids. **Do not apply it.**
Had Option B (dual-write) been chosen, a `031_apollo_misconception_backfill.sql`
would be required to make the two stores agree at cutover, and it would carry the
two-phase NOT-NULL landmine because `apollo_misconceptions.probe_question` is
`NOT NULL` with **no default**:

```sql
-- 031_apollo_misconception_backfill.sql  [REFERENCE ONLY — NOT PART OF OPTION C; DO NOT APPLY]
-- Would copy every store-2 kind='misconception' row into store-1 with a
-- synthesized empty probe_question. Shown to document why C avoids it.
--
-- DEPLOY-TIME RECONCILIATION (read before applying anywhere — DO NOT auto-apply):
--   * On-disk migrations top out at 030_apollo_autoprovisioning.sql; 031 is the next free number.
--   * This file is applied to LOCAL Docker Postgres ONLY by feller agents. Rehearsal on the
--     TEST Supabase project then prod is a human/CI step.
--   * BACKFILL LANDMINE: probe_question is NOT NULL with NO default — every inserted row MUST
--     supply it; we synthesize '' which permanently degrades store-1 authoring quality. This is
--     the exact concession Option C avoids by synthesizing in memory at READ time instead.
--
-- BEGIN;
-- INSERT INTO apollo_misconceptions
--     (concept_id, code, description, trigger_phrases, probe_question, rt_steps)
-- SELECT e.concept_id,
--        e.canonical_key,
--        COALESCE(e.payload->>'description', e.display_name),
--        COALESCE(e.aliases, '[]'::jsonb),
--        '',                       -- synthesized empty (the landmine)
--        '[]'::jsonb
-- FROM apollo_kg_entities e
-- WHERE e.kind = 'misconception'
-- ON CONFLICT (concept_id, code) DO NOTHING;   -- store 1 wins, idempotent
-- COMMIT;
```

**Option C deletes the need for the above entirely** — the union does the same
join in memory, every read, with store 1 always winning and nothing persisted.

---

## 4. Concrete code diffs

> These diffs are the implementation contract. **Do not edit the real source
> files from this doc** — apply them on the feature branch (§7). Line anchors are
> as of 2026-06-23.

### 4.1 `apollo/overseer/misconception_bank.py` — the whole fix

Add the `KGEntity` import, add a pure projector `_entry_from_kg_entity`, and
rewrite `load_for_concept` to union store 1 ∪ store 2 (store 1 wins on `code`).
`match_by_embedding` and `upsert_entry` are untouched.

```diff
--- a/apollo/overseer/misconception_bank.py
+++ b/apollo/overseer/misconception_bank.py
@@ -22,7 +22,7 @@ import json
 from sqlalchemy import select, text
 from sqlalchemy.ext.asyncio import AsyncSession

-from apollo.persistence.models import Misconception
+from apollo.persistence.models import KGEntity, Misconception

 _LOG = logging.getLogger(__name__)

@@ -66,21 +66,73 @@ def _from_row(row: Misconception) -> MisconceptionEntry:
         rt_steps=tuple(rt),
     )


+def _entry_from_kg_entity(row: KGEntity) -> MisconceptionEntry:
+    """Project an ``apollo_kg_entities`` ``kind='misconception'`` row into the
+    grading-facing :class:`MisconceptionEntry` (store-2 -> store-1 read adapter).
+
+    Closes the D5 dual-storage footgun at the read seam: seeding the learner
+    model (or, in future, auto-provisioning mints) becomes visible to grading
+    without a second seeder run.
+
+    Field mapping:
+      * ``code``            <- ``canonical_key`` (the ``misc.<name>`` identity
+        grading joins on at ``done_grading.py:194``; store-1 ``code`` and
+        store-2 ``canonical_key`` are byte-identical by convention).
+      * ``description``     <- ``payload['description']`` (fallback
+        ``display_name``).
+      * ``trigger_phrases`` <- ``aliases``.
+      * ``confusion_pair``  <- ``None`` (kg_entities has no structured pair; keeps
+        ``_misconceptions_dict`` forcing ``opposes=None`` -> zero conflict-pair
+        behavior change).
+      * ``probe_question``/``rt_steps`` are **synthesized empty**. No live
+        grading consumer reads them (``_misconceptions_dict`` drops them); the
+        live tutoring Socratic loop is not wired in v1
+        (``chat.py:280-283``). This is why auto-provisioning's documented
+        inability to author them (``tag_mint.py:36-40``) is a non-issue here.
+
+    ``payload``/``aliases`` may arrive as JSON strings (SQLite/raw paths); guard
+    exactly as :func:`_from_row` does for ``trigger_phrases``/``rt_steps``.
+    """
+    payload = row.payload or {}
+    if isinstance(payload, str):
+        payload = json.loads(payload)
+    aliases = row.aliases or []
+    if isinstance(aliases, str):
+        aliases = json.loads(aliases)
+
+    description = payload.get("description") or row.display_name
+
+    return MisconceptionEntry(
+        id=int(row.id),
+        concept_id=int(row.concept_id),
+        code=row.canonical_key,
+        description=description,
+        confusion_pair=None,
+        trigger_phrases=tuple(aliases),
+        probe_question="",
+        rt_steps=(),
+    )
+
+
 async def load_for_concept(
     db: AsyncSession,
     *,
     concept_id: int,
 ) -> list[MisconceptionEntry]:
-    """Return every authored misconception for a concept.
-
-    Used by the inference pipeline as the candidate set for retrieval.
-    Pure read; safe to cache per-concept at the caller's discretion
-    (signal: bank changes when an author edits the table, which is
-    rare relative to chat-turn frequency).
-    """
-    rows = (
-        await db.execute(
-            select(Misconception).where(Misconception.concept_id == concept_id)
-        )
-    ).scalars().all()
-    return [_from_row(r) for r in rows]
+    """Return every authored misconception for a concept, as the UNION of the
+    two Apollo misconception stores (D5 footgun closed at the read seam):
+
+      1. store 1 -- ``apollo_misconceptions`` rows (authoritative; carries the
+         real Socratic fields when hand-authored), then
+      2. store 2 -- ``apollo_kg_entities`` ``kind='misconception'`` rows,
+         projected via :func:`_entry_from_kg_entity` with empty Socratic fields.
+
+    **Dedup rule:** keyed by ``code`` == ``canonical_key``; **store 1 wins** on
+    conflict (a store-2 twin never overwrites a hand-authored store-1 row). A
+    store-2 misconception with no store-1 twin is included with empty
+    ``probe_question``/``rt_steps`` -- exactly the fields the sole live grading
+    consumer (``done_grading._misconceptions_dict``) discards.
+
+    Pure read; safe to cache per-concept at the caller's discretion (the bank
+    changes only when an author edits a table, rare vs chat-turn frequency).
+    """
+    entries_by_code: dict[str, MisconceptionEntry] = {}
+
+    # Store 1 first so it claims each `code` (authoritative).
+    store1_rows = (
+        await db.execute(
+            select(Misconception).where(Misconception.concept_id == concept_id)
+        )
+    ).scalars().all()
+    for r in store1_rows:
+        entry = _from_row(r)
+        entries_by_code[entry.code] = entry
+
+    # Store 2: add only misconceptions whose canonical_key is not already
+    # claimed by store 1 (store 1 wins).
+    store2_rows = (
+        await db.execute(
+            select(KGEntity).where(
+                KGEntity.concept_id == concept_id,
+                KGEntity.kind == "misconception",
+            )
+        )
+    ).scalars().all()
+    for r in store2_rows:
+        if r.canonical_key in entries_by_code:
+            continue
+        entry = _entry_from_kg_entity(r)
+        entries_by_code[entry.code] = entry
+
+    return list(entries_by_code.values())
```

Note on `match_by_embedding` / `upsert_entry`: intentionally **unchanged**. The
embedding NN path is dead live (only caller is `infer_misconception`) and does
not see kg-only rows — acceptable because that path is unused. `upsert_entry`
remains dead. A one-line code comment to that effect is optional and omitted to
keep the diff minimal.

### 4.2 `apollo/handlers/done_grading.py` — **no change required**

`_misconceptions_dict` (lines 130-140) already builds the grading dict from
`MisconceptionEntry` and forces `opposes: None`. Because Option C keeps the
projector emitting `confusion_pair=None` and the union returns the same
dataclass, conflict-pair behavior is **byte-for-byte unchanged**. This is
asserted by a test (§6), not edited.

### 4.3 Owner-doc reconciliation (drift contract — same commit)

Per the drift-prevention contract, reconcile the owner doc in the same commit.
`docs/architecture/apollo.md` owns `apollo/**`.

```diff
--- a/docs/architecture/apollo.md
+++ b/docs/architecture/apollo.md
@@ (the misconception_bank / Done-grading section)
-`misconception_bank.load_for_concept` reads the `apollo_misconceptions` table
-(store 1) and returns `list[MisconceptionEntry]`. Grading consumes it via
-`done_grading._misconceptions_dict`. The learner-model graph stores
-misconceptions separately as `apollo_kg_entities kind='misconception'` (store
-2); the two stores are NOT synced (D5 footgun): seeding the learner model does
-not populate grading.
+`misconception_bank.load_for_concept` returns `list[MisconceptionEntry]` as the
+UNION of store 1 (`apollo_misconceptions`) and store 2 (`apollo_kg_entities`
+`kind='misconception'`), deduped by `code == canonical_key` with **store 1
+winning**; Socratic fields (`probe_question`/`rt_steps`) are synthesized empty
+for store-2-only rows. This closes the **D5 dual-storage footgun at the read
+seam** (2026-06-23): seeding the learner model — or, in future,
+auto-provisioning mints (`tag_mint.py`) — is now automatically visible to
+grading, with no schema/migration/writer change. Grading consumes the result
+via `done_grading._misconceptions_dict`, which still forces `opposes=None`
+(conflict-pair detection unchanged). The embedding NN path (`match_by_embedding`)
+is unchanged and does not see store-2-only rows (dead live).
@@ (frontmatter)
-last_verified: <prior date>
+last_verified: 2026-06-23
```

The two seeders get a one-line pointer (comment change only, no behavior) so the
disconnected-seeder trap is documented as closed:

```diff
--- a/scripts/_macro_seed_misconceptions.py
+++ b/scripts/_macro_seed_misconceptions.py
@@ -9,6 +9,11 @@
 (load_for_concept doesn't use it; only the embedding-retrieval channel does).
+
+NOTE (2026-06-23, D5 read-union): grading's load_for_concept now UNIONs store 1
+with store-2 (apollo_kg_entities kind='misconception'), so running ONLY the
+learner-model seeder (scripts/seed_apollo_learner_model.py) is already
+sufficient for grading to see misconceptions. This seeder remains useful when you
+want real Socratic probe_question/rt_steps in store 1 (store 1 wins the union).
```

```diff
--- a/scripts/seed_apollo_learner_model.py
+++ b/scripts/seed_apollo_learner_model.py
@@ (module docstring)
+# NOTE (2026-06-23, D5 read-union): seeding store-2 kind='misconception' rows
+# here is now ALSO visible to Done grading via misconception_bank.load_for_concept
+# (it unions store 1 with store 2, store 1 winning). You no longer need to run
+# scripts/_macro_seed_misconceptions.py separately just for grading to work.
```

---

## 5. Backfill plan for existing data

### 5.1 The headline: Option C needs no data migration

Because the union runs in memory on **every** `load_for_concept` call, **existing
data needs no copy**. The instant the code ships:

- Any course whose store-2 (`apollo_kg_entities kind='misconception'`) is already
  seeded becomes visible to grading with **no further action**.
- Any course whose store-1 (`apollo_misconceptions`) is already seeded keeps
  working exactly as before (store 1 wins the union; identical rows returned).

This is the key operational advantage over Options A/B, which both require a
populated-table data migration.

### 5.2 Fluids courses (the bernoulli content)

- **Store 1 today:** hand-authored `misconceptions.json` carries real
  `probe_question` / `rt_steps`; seeded into `apollo_misconceptions` via the
  fluid path. These rows **win the union** (full Socratic fields preserved).
- **Store 2 today:** the learner-model seeder mints the matching
  `kind='misconception'` kg_entities (`canonical_key == code`).
- **Effect of the union:** for fluids, every misconception already exists in both
  stores with identical keys, so the union returns the **store-1** row (real
  Socratic fields). **Zero change in behavior or content for fluids.** No backfill
  action. The dedup test (§6, case "seed-both") is the regression lock for this.

### 5.3 Macro course (OpenStax Ch. 6)

- **Store 1 today:** only populated if `scripts/_macro_seed_misconceptions.py`
  was run by hand; when run, it stores `probe_question=entry.get(...,"")` and
  `rt_steps=[]` (mostly empty Socratic fields already).
- **Store 2 today:** the learner-model seeder mints `kind='misconception'`
  kg_entities for macro.
- **Effect of the union:**
  - If the macro store-1 seeder was **never run** (the common footgun state),
    grading now sees the macro misconceptions **via store 2** automatically —
    this is precisely the bug being fixed. No action required.
  - If it **was** run, those store-1 rows win the union (same empty Socratic
    fields as the store-2 projection would synthesize) — identical grading
    candidate set, no dupes.
- **Recommended one-time cleanup (optional, not required):** since store 2 now
  feeds grading, the macro store-1 seeder
  (`scripts/_macro_seed_misconceptions.py`) is **no longer necessary** for
  grading. Leave existing rows in place (harmless; they win the union). New macro
  concepts need only the learner-model seeder.

### 5.4 Verification (LOCAL only, read-only)

On a **local** Docker Postgres (never a remote Supabase), confirm the union per
concept after deploy:

```sql
-- Count store-1 vs store-2 misconceptions per concept; the union returns
-- COUNT(store1) + COUNT(store2 with canonical_key NOT IN store1.code).
SELECT c.id AS concept_id,
       (SELECT count(*) FROM apollo_misconceptions m WHERE m.concept_id = c.id) AS store1,
       (SELECT count(*) FROM apollo_kg_entities e
          WHERE e.concept_id = c.id AND e.kind = 'misconception') AS store2,
       (SELECT count(*) FROM apollo_kg_entities e
          WHERE e.concept_id = c.id AND e.kind = 'misconception'
            AND e.canonical_key NOT IN (
                SELECT m.code FROM apollo_misconceptions m WHERE m.concept_id = c.id
            )) AS store2_only_added
FROM apollo_concepts c
ORDER BY c.id;
```

`store1 + store2_only_added` is the count `load_for_concept` will now return. For
fluids, `store2_only_added` should be ~0 (store 1 already covers them). For macro
in the footgun state, `store1 = 0` and `store2_only_added = store2` (the bug,
now fixed).

---

## 6. Test plan (>=95% patch coverage; LOCAL docker only)

### 6.1 Harness facts (confirmed)

- **DB harness = Testcontainers (ephemeral pgvector `pg16`), NOT a remote
  Supabase and NOT `supabase db reset`.** `tests/conftest.py` provides
  session-scoped `_pg_url` (`CREATE EXTENSION vector` + `Base.metadata.create_all`)
  and function-scoped `db_session` (savepoint rollback per test). Both **skip
  cleanly** when Docker is down. `apollo/conftest.py:24` re-exports them;
  `TEST_SPACE_ID = 1` (`apollo/conftest.py:33`).
- **The union query is ORM-only** (`select(KGEntity)` / `select(Misconception)`,
  no pgvector operators), so the existing `test_misconception_bank.py` **in-memory
  SQLite** pattern works for the integration cases too — keeping them fast and
  Docker-optional. (The pgvector-only `match_by_embedding` path is NOT exercised
  by Option C.)
- Markers are registered in `pytest.ini` (`unit` / `integration` / `e2e` /
  `slow` / `llm`; `--strict-markers`). **Register no new markers** — reuse
  `unit` / `integration`.
- Coverage gate (CLAUDE.md): `pytest --cov --cov-report=xml` then
  `diff-cover coverage.xml --compare-branch=origin/staging --fail-under=95`.

### 6.2 Changed lines that must be covered (the patch surface)

1. `_entry_from_kg_entity` (new function) — every line, including both branches
   of the `payload`/`aliases` `isinstance(..., str)` guards and the
   `description = payload.get("description") or row.display_name` fallback (both
   sides).
2. `load_for_concept` rewrite — the store-1 loop, the store-2 loop, the
   `if r.canonical_key in entries_by_code: continue` dedup branch (both taken and
   not-taken), and the final `list(...)`.
3. The `KGEntity` import line (covered by importing the module).

Target: **100% of the changed lines** (comfortably >= 95% patch).

### 6.3 Pure-unit tests (marker `unit`, NO DB) — carries most of the patch

New file: `apollo/overseer/tests/test_misconception_bank_projection.py`. These
build `KGEntity` instances in memory (no session) and call `_entry_from_kg_entity`
directly.

| Case | Asserts | Covers |
|---|---|---|
| `test_projection_maps_core_fields` | `KGEntity(canonical_key='misc.foo', payload={'description':'D'}, aliases=['a','b'])` → `code=='misc.foo'`, `description=='D'`, `trigger_phrases==('a','b')`. | `canonical_key→code`, `payload.description→description`, `aliases→trigger_phrases`. |
| `test_projection_synthesizes_empty_socratic` | `probe_question == ''`, `rt_steps == ()`, `confusion_pair is None`. | The synthesized-defaults lines (the keystone of the design). |
| `test_projection_description_falls_back_to_display_name` | `payload={}` (no `description`), `display_name='Pressure≠Velocity'` → `description=='Pressure≠Velocity'`. | The `or row.display_name` fallback branch. |
| `test_projection_description_empty_string_falls_back` | `payload={'description': ''}` → `description==display_name` (empty string is falsy → fallback). | The falsy-`description` edge of the `or` fallback. |
| `test_projection_tolerates_json_string_payload_and_aliases` | `payload='{"description":"D"}'` (str), `aliases='["x"]'` (str) → parsed; `description=='D'`, `trigger_phrases==('x',)`. | Both `isinstance(..., str)` JSON-decode branches. |
| `test_projection_handles_null_payload_and_aliases` | `payload=None`, `aliases=None` → `description==display_name`, `trigger_phrases==()`. | The `or {}` / `or []` guards. |
| `test_projection_id_and_concept_id_coerced_int` | string-ish ids coerce via `int(...)`. | `id=int(...)`, `concept_id=int(...)`. |

### 6.4 Integration tests (marker `integration`, `db_session` or SQLite) — the D5 regression lock

Extend `apollo/overseer/tests/test_misconception_bank.py` (reuse its
`Subject(search_space_id=TEST_SPACE_ID)` + `Concept` seed pattern; add
`KGEntity` to the created tables). Because the union query is ORM-only, the
existing in-memory SQLite engine is sufficient; the `db_session` pgvector
container fixture is acceptable too (use it if running the whole apollo
integration suite). Seed the FK chain first (Subject → Concept), exactly as the
existing fixture does.

| # | Case | Setup | Assert | Why it matters |
|---|---|---|---|---|
| I1 | **seed store-2 only (the footgun fix)** | Seed **only** `KGEntity(concept_id=A, kind='misconception', canonical_key='misc.foo', aliases=['p1','p2'], payload={'description':'desc'})`. No store-1 row. | `load_for_concept(concept_id=A)` returns exactly one entry with `code=='misc.foo'`, `trigger_phrases==('p1','p2')`, `description=='desc'`, **`probe_question==''`**, **`rt_steps==()`**. | This is the D5 footgun **fixed**: store-2-only seeding now feeds grading. |
| I2 | **empty bank** | Seed Subject + Concept, **no misconceptions in either store**. | `load_for_concept(concept_id=A) == []`. | Explicitly required: the empty-bank case (grading degrades to vacuous soundness, but no crash / no phantom rows). |
| I3 | **seed-once-sees-both (dual presence, store 1 wins)** | Seed **both** a store-1 `Misconception(code='misc.foo', probe_question='real?', rt_steps=['s1'], trigger_phrases=['t1'])` **and** a store-2 `KGEntity(canonical_key='misc.foo', aliases=['x'])`, same concept. | Exactly **one** entry returned for `'misc.foo'`; it is the **store-1** row (`probe_question=='real?'`, `rt_steps==('s1',)`, `trigger_phrases==('t1',)`); **no duplicate**. | Required "seed-once-sees-both" case; locks the dedup + store-1-precedence rule (protects fluids, §5.2). |
| I4 | **mixed: some twinned, some store-2-only** | Concept has store-1 `misc.a` (real fields) + store-2 `misc.a` (twin) + store-2 `misc.b` (no twin). | Returns **2** entries: `misc.a` is the store-1 row (real `probe_question`); `misc.b` is the projected store-2 row (`probe_question==''`). | Exercises the dedup branch **both taken (a) and not-taken (b)** in one call → covers the `continue`. |
| I5 | **concept scoping preserved across the union** | store-2 `kind='misconception'` rows on concept A and concept B; query A. | Only A's misconception(s) returned; B's excluded. | The union must not leak across concepts (mirrors the existing store-1 scoping test). |
| I6 | **non-misconception kg_entities ignored** | Seed `KGEntity(kind='equation')` and `KGEntity(kind='concept')` on concept A alongside one `kind='misconception'`. | Only the `kind='misconception'` row is projected; equation/concept rows are absent. | Locks the `KGEntity.kind == 'misconception'` filter (guards K2/K3 inventory: we read only misconception rows). |
| I7 | **idempotent / stable across repeated reads** | Any seeded state; call `load_for_concept` twice. | Both calls return equal entry sets (no accumulation, pure read). | Confirms the read is side-effect-free (matches the existing seeder's idempotency contract). |

### 6.5 Unchanged-behavior assertion for `done_grading` (marker `unit`)

Add one test (in the misconception-bank projection test file, or alongside the
existing `done_grading` tests) that calls `_misconceptions_dict` with a mixed
list (one store-1-origin `MisconceptionEntry` with real fields + one
projected store-2-origin entry with empty Socratic fields) and asserts the output
dict still has `opposes: None` for every item and the expected
`{key, trigger_phrases, opposes, display_name}` shape — proving Option C changes
**zero** grading behavior (§4.2).

### 6.6 No migration test

Option C adds no migration, so **no `test_apollo_*_migration.py` is written**.
(Had A/B been chosen, the `tests/database/test_apollo_learner_model_migration.py`
pattern — content-scoped chain, `_STUB_DDL` for `auth.users` /
`aita_search_spaces`, the `_expect_violation` SAVEPOINT helper, and a populated
pre-031 backfill DB — would be copied wholesale. It is not needed here.)

### 6.7 Coverage command (run locally before PR)

```bash
cd ai-ta-backend
pytest --cov --cov-report=xml \
  apollo/overseer/tests/test_misconception_bank.py \
  apollo/overseer/tests/test_misconception_bank_projection.py
diff-cover coverage.xml --compare-branch=origin/staging --fail-under=95
```

(Or run the full suite: `pytest --cov --cov-report=xml` then the same
`diff-cover`. CI re-runs this on the PR per
`ai-ta-backend/.github/workflows/ci.yml`.)

---

## 7. Rollout note (branching model)

Per CLAUDE.md, `ApolloV3` is the **LIVE/prod** branch — never branch off it,
never commit to it. `staging` is the integration branch.

1. **Branch:** cut `fix/apollo-misconception-read-union` **off `staging`**:
   ```bash
   git fetch origin
   git switch -c fix/apollo-misconception-read-union origin/staging
   ```
2. **Implement** the diffs in §4 (misconception_bank.py + the owner-doc /
   seeder-comment reconciliation, in the **same commit** per the drift contract).
3. **Test** locally with Docker available (§6.7). Confirm
   `diff-cover ... --fail-under=95` passes on the changed lines. The whole apollo
   suite must stay green (`pytest apollo`).
4. **PR into `staging`** (never into `ApolloV3`). PR body must:
   - State no migration / no schema change / no writer change.
   - Note the behavior contract: grading candidate set is now the union; store 1
     wins on `code == canonical_key`; conflict-pair detection unchanged
     (`opposes` still `None`).
   - List the test cases (I1 footgun-fix, I2 empty-bank, I3 seed-both, I4–I7) and
     the `diff-cover` result.
5. **Promotion to prod is a separate `staging → ApolloV3` PR**, done by a human
   after staging soak — **not** part of this change.
6. **Deploy/DB note:** there is **nothing to apply to any Supabase project** for
   this change (no migration). Railway staging backend points at TEST Supabase;
   prod at prod — neither needs a DDL step. Agents never apply migrations to
   remote Supabase regardless; here there is none to apply.

### Reversibility

Option C is a single function plus a pure helper. If staging soak surfaces any
problem (e.g. an unexpected store-2 misconception inflating a grading candidate
set), revert the one commit — store 1 grading returns to its prior behavior with
no data to unwind.

---

## 8. What a unification MUST NOT break (acceptance invariants)

Carried from the readers investigation; each is asserted by a test above.

1. **`done_grading._misconceptions_dict` shape is preserved** — Option C returns
   the same `list[MisconceptionEntry]`; the dict still emits
   `{key, trigger_phrases, opposes, display_name}` (§4.2, §6.5).
2. **`code == canonical_key` identity holds** — the dedup key and the
   `done_grading.py:194` join key are the same string; the projector sets
   `code = canonical_key` (I3/I4).
3. **`opposes` stays `None`** — projector sets `confusion_pair=None`;
   `_misconceptions_dict` keeps forcing `opposes: None`; conflict-pair detection
   stays dormant (§2.2 obs 3, §6.5).
4. **`trigger_phrases → aliases` survives** — store-2 `aliases` map to
   `trigger_phrases` (the resolution signal in `tiers.py`); store-1
   `trigger_phrases` map through `_from_row` unchanged (I1, I3).
5. **Store-1 NOT-NULL `probe_question`/`rt_steps` untouched** — synthesized in
   memory only; the table constraint still forces real authoring when a human
   hand-authors (§2.5 row 3).
6. **K2/K3 entity inventory unchanged** — Option C only *reads* kg_entities; it
   adds/removes/renames nothing, so `read_learner_profile` and
   `load_entity_specs` see an identical `canonical_key` set (I6 guards the
   read-only, misconception-filtered nature).

---

## 9. File index (absolute paths)

Changed by this work:
- `C:/Users/ultra/OneDrive/TA-test/ai-ta-backend/apollo/overseer/misconception_bank.py` (projector + union)
- `C:/Users/ultra/OneDrive/TA-test/ai-ta-backend/docs/architecture/apollo.md` (drift reconcile + `last_verified`)
- `C:/Users/ultra/OneDrive/TA-test/ai-ta-backend/scripts/_macro_seed_misconceptions.py` (docstring note)
- `C:/Users/ultra/OneDrive/TA-test/ai-ta-backend/scripts/seed_apollo_learner_model.py` (docstring note)
- `C:/Users/ultra/OneDrive/TA-test/ai-ta-backend/apollo/overseer/tests/test_misconception_bank.py` (I1–I7)
- `C:/Users/ultra/OneDrive/TA-test/ai-ta-backend/apollo/overseer/tests/test_misconception_bank_projection.py` (new; pure-unit)

Referenced (not changed):
- `C:/Users/ultra/OneDrive/TA-test/ai-ta-backend/apollo/handlers/done_grading.py` (`_misconceptions_dict` :120-140; reader :186-200)
- `C:/Users/ultra/OneDrive/TA-test/ai-ta-backend/apollo/persistence/models.py` (`Misconception` :192-216, `KGEntity` :382-413)
- `C:/Users/ultra/OneDrive/TA-test/ai-ta-backend/apollo/persistence/learner_model_seed.py` (`misconceptions_to_entities` :247-265)
- `C:/Users/ultra/OneDrive/TA-test/ai-ta-backend/apollo/provisioning/tag_mint.py` (deviation note :36-40)
- `C:/Users/ultra/OneDrive/TA-test/ai-ta-backend/apollo/provisioning/solution.py` (`misconceptions=[]` :332)
- `C:/Users/ultra/OneDrive/TA-test/ai-ta-backend/database/migrations/019_apollo_misconceptions.sql`
- `C:/Users/ultra/OneDrive/TA-test/ai-ta-backend/database/migrations/026_apollo_learner_model.sql`
- `C:/Users/ultra/OneDrive/TA-test/ai-ta-backend/database/migrations/030_apollo_autoprovisioning.sql` (current max; next free = 031, unused)
- `C:/Users/ultra/OneDrive/TA-test/ai-ta-backend/tests/conftest.py` (`_pg_url` / `db_session`)
- `C:/Users/ultra/OneDrive/TA-test/ai-ta-backend/apollo/conftest.py` (re-export + `TEST_SPACE_ID`)
