# Plan: Apollo KG WU-3B — Bernoulli Layer-1 seed (entities + prereqs + aliases + misconceptions + ref-links + declared path)

**Goal:** A TDD-ordered, idempotent, course-scoped data seeder that converts the hand-authored bernoulli files into migration-026 Layer-1 rows (entities, prereqs, aliases, `canon.misc.*` entities with opposes-links) AND assigns reference-node→entity links + declares the single acceptable solution path for all 5 bernoulli problems, so the §6.1 reference-graph validation passes and WU-4A has a complete day-one fixture.
**Architecture:** Python/FastAPI backend, no HTTP surface. New seed module under `apollo/persistence/` + a NEW course-scoped seeder script under `scripts/`. Writes into migration-026 tables (`apollo_kg_entities`, `apollo_entity_prereqs`) and updates `apollo_concept_problems.payload` reference solutions with entity links + a declared path. A pure-Python conversion core (no DB) is unit-tested fast; the seed flow is tested on Testcontainers Postgres (WU-3A harness pattern).
**Tech stack:** Python 3.12, SQLAlchemy 2 async + asyncpg, pytest + pytest-asyncio, Testcontainers `pgvector/pgvector:pg16`. LLM: NONE used at seed time (deterministic conversion only — see Risks).

---
provides:
  - apollo/persistence/learner_model_seed.py — pure conversion functions (concept_dag/symbols/normalization/misconceptions/reference-solution → entity dicts + prereq pairs + ref-link/path-annotated reference solutions) + a reference-graph validation contract function (the §6.1 check WU-4A consumes)
  - scripts/seed_apollo_learner_model.py — course-scoped, idempotent seeder writing Layer-1 rows + annotating reference solutions
  - apollo/subjects/fluid_mechanics/concepts/bernoulli_principle/misconceptions.json — NEW bernoulli misconception authoring source (canon.misc.* + opposes target keys)
  - the migration-026-table row fixture (entities/prereqs/aliases/misconceptions+opposes/ref-links/declared path) WU-4A's grading core builds R_norm from
consumes:
  - migration 026 tables: apollo_kg_entities, apollo_entity_prereqs (created by WU-3A)
  - apollo_concepts / apollo_subjects / apollo_concept_problems (migration 018; subject is course-scoped via 026)
  - apollo/subjects/fluid_mechanics/concepts/bernoulli_principle/{concept_dag.json, canonical_symbols.json, normalization_map.json, problems/problem_*.json}
  - aita_search_spaces (course id resolution; bootstrap = MIN(id))
depends_on:
  - WU-3A (migration 026 + ORM models) — LANDED on this branch's base (feat/apollo-kg-wu3a-learner-model-migration)
  - scripts/seed_apollo_concept_registry.py — must have run first (creates the apollo_concepts/apollo_concept_problems rows this seeder reads); WU-3B is layered ON TOP
---

## Overview

WU-3A landed migration 026: the empty `apollo_kg_entities` / `apollo_entity_prereqs`
tables exist, plus the ORM (`apollo/persistence/models.py`: `KGEntity`, `EntityPrereq`,
`ENTITY_KINDS`). Nothing writes them yet. WU-3B is the **data fixture** that fills Layer 1
for the bootstrap bernoulli concept and annotates the existing problems so the grading core
(WU-4A) has a complete, validation-passing reference graph from day one.

Six things the seed must produce (spec §8 v1 seed + §5 + §6.1):
1. **concept entities** — one per `concept_dag.json` node (14), `kind='concept'`.
2. **variable entities** — one per `canonical_symbols.json` symbol (7), `kind='variable'`.
3. **prereq edges** — one per `concept_dag.json` edge (16), into `apollo_entity_prereqs`.
4. **aliases** — `normalization_map.json` (23 mappings) attached to the variable entities they normalize to.
5. **misconception entities** — `canon.misc.*`, `kind='misconception'`, each with an
   **opposes-link** to the entity it contradicts. (No bernoulli misconception authoring
   source exists today — WU-3B defines it.)
6. **reference-node→entity links + a single declared solution path** for each of the 5
   bernoulli problems, written so the §6.1 validation contract PASSES (every reference node
   carries an entity link AND ≥1 path is declared).

The seed is **course-scoped** (every row resolves to a `search_space_id` via
concept→subject→search_space) and **idempotent** (re-run is a no-op, no unique violation).

## Verified source-file facts (read 2026-06-15)

All counts verified by reading + `json.load` (UTF-8) against the actual files. Where the
spec's stated count differs, the ACTUAL file wins (binding constraint).

| Source file | Spec said | ACTUAL (verified) | Notes |
|---|---|---|---|
| `concept_dag.json` | 14 nodes / 16 edges | **14 nodes / 16 edges** ✓ | edge types: 15 `requires` + 1 `extends` (`horizontal_flow_simplification` extends `bernoulli_principle`). Node ids listed below. |
| `canonical_symbols.json` | 7 symbols | **7 symbols** ✓ | `["P","rho","v","A","h","g","Q"]` |
| `normalization_map.json` | 23 mappings | **23 mappings** ✓ | maps to **8** distinct targets: the 7 canonical symbols PLUS `q` (`"dynamic pressure" → "q"`). `q` is NOT a canonical symbol — see Risk R5. |
| `problems/problem_*.json` | 5 problems | **5 problems** ✓ | each has a `reference_solution` list of steps; reference node ids inventoried below. |
| bernoulli misconception source | (gap) | **DOES NOT EXIST** | `misconception_corpus.jsonl` is test-only (`apollo/overseer/tests/`). WU-3B creates `misconceptions.json` (D4). |

**concept_dag node ids (14):** `pressure`, `fluid_density`, `fluid_velocity`,
`cross_sectional_area`, `elevation`, `gravitational_acceleration`, `kinetic_energy_density`,
`gravitational_potential_density`, `energy_conservation_fluid`, `incompressibility_assumption`,
`continuity_equation`, `bernoulli_principle`, `horizontal_flow_simplification`,
`volumetric_flow_rate`.

**concept_dag edge shape:** `{"type": "requires"|"extends", "from": <id>, "to": <id>}`. A
`requires`/`extends` edge `from X to Y` means "X depends on / extends Y", which maps to
`apollo_entity_prereqs(from_entity_id=X, to_entity_id=Y)` (the ORM doc string: "from depends
on to"). All 16 edges become prereq rows regardless of `type` (both encode a dependency
direction for v1; the `extends` vs `requires` distinction is dropped in Layer 1, noted R6).

**reference node inventory (the ids that need entity links), per problem:**

| Problem (`id`) | target | reference node ids (entry_type) |
|---|---|---|
| `bernoulli_horizontal_pipe_find_p2` (p01) | P2 | `continuity`(equation), `incompressibility`(condition), `bernoulli`(equation), `horizontal_simplification`(simplification), `plan_apply_continuity`(procedure_step), `plan_apply_horizontal_simplification`(procedure_step), `plan_solve_bernoulli_for_p2`(procedure_step) |
| `bernoulli_height_change_find_v2` (p02) | v2 | `bernoulli`(equation), `equal_pressure_simplification`(simplification), `plan_apply_equal_pressure_simplification`(procedure_step), `plan_set_v1_zero_and_solve_bernoulli`(procedure_step) |
| `continuity_area_change_find_v2` (p03) | v2 | `incompressibility`(condition), `continuity`(equation), `plan_invoke_incompressibility`(procedure_step), `plan_solve_continuity_for_v2`(procedure_step) |
| `volumetric_flow_rate_find_Q` (p04) | Q | `flow_rate_definition`(equation), `plan_apply_flow_rate_definition`(procedure_step) |
| `bernoulli_full_find_p2` (p05) | P2 | `incompressibility`(condition), `continuity`(equation), `bernoulli`(equation), `plan_apply_continuity_for_v2`(procedure_step), `plan_substitute_into_bernoulli`(procedure_step) |

The reference nodes are **equations/conditions/simplifications/procedure_steps** (NOT the
concept-dag concepts). So Layer 1 needs `kind` values beyond `concept`/`variable`:
`equation`, `condition`, `procedure`, `definition` — all allowed by the migration-026 CHECK
(`ENTITY_KINDS`). The minted reference-derived entities (D5) supply these.

**migration-026 facts (read `026_apollo_learner_model.sql` + `models.py`):**
- `apollo_kg_entities(id, concept_id FK→apollo_concepts, canonical_key, kind CHECK∈ENTITY_KINDS, display_name, payload JSONB default '{}', aliases JSONB default '[]', created_at, updated_at)`, `UNIQUE(concept_id, canonical_key)`.
- `ENTITY_KINDS = (concept, equation, condition, definition, procedure, variable, misconception)` (`apollo/persistence/models.py:53`), asserted equal to the SQL CHECK by `test_learner_model_allowlists.py`.
- `apollo_entity_prereqs(from_entity_id, to_entity_id)` composite PK, both FK→entities ON DELETE CASCADE.
- ORM declares NO CHECK constraints (repo convention); the migration SQL is the authority.

## Prior art (sibling modules)

- **`scripts/seed_apollo_concept_registry.py`** — the seeder convention to mirror:
  async `create_async_engine` + `async_sessionmaker`, `_upsert_*` helpers keyed on natural
  keys for idempotency, `--dry-run` rolls back, `--database-url` / `DATABASE_URL`, one
  `session.commit()` at the end, returns a stats dict. `_upsert_subject` (line 63) already
  reads the bootstrap course via `SELECT MIN(id) FROM aita_search_spaces` and leaves a
  `TODO(WU-3B)` — WU-3B is its continuation. **WU-3B does NOT modify this file's `_upsert_*`
  functions** (backward-compat: its tests `test_seed_subject_search_space.py` must stay green).
- **`apollo/overseer/misconception_bank.py:174` `upsert_entry`** — the existing
  curriculum-author-facing INSERT-or-UPDATE for `apollo_misconceptions` (raw SQL,
  `ON CONFLICT (concept_id, code) DO UPDATE`, `RETURNING id`). The misconception ENTITY seed
  (Layer 1 `canon.misc.*`) is a SEPARATE concern from the misconception BANK
  (`apollo_misconceptions`, migration 019, for the inference channel). WU-3B writes Layer-1
  `kind='misconception'` entity rows, NOT `apollo_misconceptions` rows — see D4/Risk R7.
- **`tests/database/test_apollo_learner_model_migration.py`** — the WU-3A Testcontainers
  harness this unit reuses verbatim for structure: `_pg_url` session fixture (from
  `tests/conftest.py`), `_chain_migrations()` (content-scoped migration selection),
  `_STUB_DDL` (`auth.users` + `aita_search_spaces` stubs), `mig_conn` per-test rollback
  fixture, `_expect_violation` savepoint helper, and the `_seed_space/_seed_subject/
  _seed_concept` seed helpers. WU-3B's DB test imports the same building blocks (or copies
  the small helpers) but seeds REAL bernoulli concept rows (not stubs) so it can run the seed.
- **`apollo/persistence/tests/test_seed_subject_search_space.py`** — the fast-unit pattern:
  in-memory SQLite, `Base.metadata.create_all(tables=[...])`, exercises one changed function
  off-DB. WU-3B's pure-conversion tests follow this (but most need NO DB at all — pure dict in/out).
- **`apollo/persistence/models.py`** — `ENTITY_KINDS` tuple + `KGEntity`/`EntityPrereq` ORM,
  the typed access layer the seeder writes through.

Naming conventions confirmed from prior art: snake_case files, `_upsert_*`/`_seed_*` private
async helpers, `seed()` public entrypoint returning a stats dict, `main(argv)` CLI wrapper.

## Key design decisions (planner picks, grounded)

### D1. New seeder script (NOT extend existing) — PICK: new `scripts/seed_apollo_learner_model.py`
**Decision:** a NEW script, separate from `seed_apollo_concept_registry.py`.
**Why:** the existing script seeds the migration-018 curriculum (`apollo_subjects`/`_concepts`/
`_concept_problems`); WU-3B seeds the migration-026 LEARNER MODEL on top and must run AFTER
it (it reads the concept rows the first script wrote). Two concerns, two scripts, layered —
matches the spec's "ADDS the Layer-1 seed on top" framing and keeps the first script's tests
untouched (backward-compat constraint). The new script reuses the bootstrap-course resolution
idiom (`MIN(aita_search_spaces.id)`) but takes the real per-course mapping seam from D7.

### D2. ref-node→entity link storage — PICK: keys embedded in `reference_solution` (payload), NOT a new mapping table
**Decision:** annotate each reference-solution step in `apollo_concept_problems.payload` with
an `entity_key` field (and the problem with a `declared_paths` field), in place. NO new table.
**Why (grounded in spec §2 + §8A criterion 7):** spec §2 explicitly offers "per-problem
mapping table OR keys embedded in `reference_solution` — planner picks; it must survive
problem edits and be queryable for the selection join". §8A criterion 7 binds the
`reference_solution` to live in `apollo_concept_problems.payload` "so the §6 grading core /
Layer-1 consume them from the DB". Embedding the link IN the payload means the link travels
with the reference solution automatically (survives edits — they are the same row) and needs
no extra join for the grading core, which already loads the payload. A separate table would
double-write and risk drift between the reference graph and its links. **Anti-scope guard:**
WU-3B does NOT add a new migration/table (scope files forbid touching migrations beyond
referencing 026; a mapping table would need DDL). Embedding is the no-DDL choice.
**Shape (added to each step):** `"entity_key": "<concept_slug>/<kind>.<slug>"`. The problem
gains a top-level `"declared_paths": [["<node_id>", ...]]` (D6) and a
`"layer1_seeded": true` idempotency marker (D7).
**Source-of-truth subtlety:** the runtime reads the DB row's payload, but the on-disk
`problem_*.json` is the authoring source the §8A `load_concept` seeder reads. To keep them
consistent and the seed re-runnable from a fresh DB, **the seeder annotates BOTH**: it writes
the entity-linked + path-annotated reference solution into the on-disk JSON files (so a fresh
`seed_apollo_concept_registry.py` re-import carries the links) AND updates the existing DB
payload rows in place (so an already-seeded DB gets them without a re-import). The on-disk
edit is small and additive (new keys only; no existing key changed) — see Task 4.

### D3. opposes-link storage — PICK: `payload.opposes_entity_key` (resolved to id at seed time → `payload.opposes_entity_id`)
**Decision:** store the opposes-link in the misconception entity's `payload` JSONB, as both
the authored `opposes_entity_key` (stable, course-relative) and the resolved
`opposes_entity_id` (BIGINT, written once the opposed entity row exists).
**Why (grounded in spec §2 + §6.5):** spec §2 says "each misconception entity carries an
opposes-link to the entity it contradicts (column or payload field on `apollo_kg_entities` —
planner picks)". Migration 026 already shipped (WU-3A) with NO `opposes_entity_id` column —
adding one now would require a new migration, which is OUT of WU-3B's scope (no DDL). The
migration's own comment anticipates this: `payload JSONB ... opposes_entity_id (misconceptions)`
(line 89-90). So **payload is the only no-DDL option and the one the migration author
intended.** §6.5 needs it only to detect that two findings concern the same concept — a
payload lookup serves that as well as a column. Storing both the key (authoring-stable) and
the id (query-fast, resolved at seed time) means WU-4A can join on id without re-resolving.

### D4. misconception authoring source format — PICK: NEW `misconceptions.json` next to the other bernoulli source files
**Decision:** create
`apollo/subjects/fluid_mechanics/concepts/bernoulli_principle/misconceptions.json`, a small
hand-authored file in the same directory as `concept_dag.json` etc., listing the bernoulli
`canon.misc.*` entities. Do NOT derive from `apollo_misconceptions` rows (that table is the
inference bank, a different concern — R7) and do NOT reuse the test-only corpus.
**Format (each entry):**
```json
{
  "misconceptions": [
    {
      "key": "misc.pressure_velocity_same_direction",
      "display_name": "Pressure and velocity move together",
      "description": "Believes pressure rises when velocity rises (Bernoulli inverts this).",
      "opposes": "def.pressure_velocity_tradeoff",
      "trigger_phrases": ["pressure goes up when it speeds up", "they move together"]
    },
    {
      "key": "misc.density_ignored",
      "display_name": "Density can be ignored",
      "description": "Drops rho from the flow relations, treating the fluid as massless.",
      "opposes": "cond.incompressible",
      "trigger_phrases": ["ignore density", "density doesn't matter"]
    }
  ]
}
```
**Why:** the misconception entity needs (a) a stable `canonical_key` (`misc.*`), (b) a
human display name + description for diagnostics, (c) the `opposes` target key (D3), and
(d) trigger phrases that double as initial `aliases` so resolution can compete them (§5
"misconception entities compete in every resolution"). The format mirrors the existing
source-file style (plain JSON in the concept dir, read by the seeder, no LLM). The
`opposes` targets are reference-derived entity keys (D5: `def.pressure_velocity_tradeoff`,
`cond.incompressible`) so the opposed entity reliably exists in Layer 1.
**Minimum set (v1, grounded in §6.9 worked example + the corpus labels):** at least the two
above (`pressure_velocity_same_direction` opposing the pressure-velocity-tradeoff definition,
and `density_ignored` opposing the incompressibility condition). These are the two misconceptions
the §6.9 Bernoulli worked example and `misconception_corpus.jsonl` exercise. The executor
authors exactly the entries in the shipped `misconceptions.json`; the count is data, asserted
by the seed test as "every entry becomes one entity with a non-null `opposes_entity_id`".

### D5. canonical_key naming scheme + kinds — PICK: `<kind-prefix>.<slug>`, namespaced under the concept (uniqueness is per-concept)
**Decision:** `canonical_key` = `"<prefix>.<slug>"` where prefix encodes kind:
`concept.*` (concept-dag nodes), `var.*` (canonical symbols), `eq.*` (equations),
`cond.*` (conditions), `simp.*`→stored as `kind='condition'` (no `simplification` kind in
ENTITY_KINDS; simplifications are scoping conditions — map to `condition`, key prefix `simp.`),
`proc.*` (procedure_steps), `def.*` (definitions, `kind='definition'`), `misc.*`
(`kind='misconception'`). The `UNIQUE(concept_id, canonical_key)` is per-concept, so keys need
not be globally unique — all bernoulli entities share `concept_id` = the bernoulli concept row.
**kind mapping table (reference-solution `entry_type` → entity `kind`):**

| Source | entry_type / origin | entity `kind` | key prefix | example key |
|---|---|---|---|---|
| concept_dag node | — | `concept` | `concept.` | `concept.bernoulli_principle` |
| canonical symbol | — | `variable` | `var.` | `var.P` |
| reference node | `equation` | `equation` | `eq.` | `eq.continuity`, `eq.bernoulli` |
| reference node | `condition` | `condition` | `cond.` | `cond.incompressibility` |
| reference node | `simplification` | `condition` | `simp.` | `simp.horizontal` |
| reference node | `procedure_step` | `procedure` | `proc.` | `proc.apply_continuity` |
| misconceptions.json | — | `misconception` | `misc.` | `misc.density_ignored` |
| (the pressure-velocity tradeoff, §6.9) | authored `definition` | `definition` | `def.` | `def.pressure_velocity_tradeoff` |

**De-duplication across problems (critical):** `continuity`, `bernoulli`, `incompressibility`
appear in multiple problems with the SAME reference-node id. They must mint ONE entity each
(keyed by `eq.continuity` etc.), and every problem's reference node links to that single
entity. The seeder dedups reference-derived entities by `canonical_key` (per the spec §8
promotion-lint duplicate-check intent: "`constant_density`/`incompressible`/`rho_constant`
never become three entities"). Same id across problems ⇒ same key ⇒ one row, linked N times.
**`def.pressure_velocity_tradeoff`** is not a reference-node id in any problem but is the
opposes-target of a misconception (§6.9). The seeder mints it from a small authored
`definitions.json` (sibling file) OR inlines a tiny authored definitions list in the seed
module (D5 picks: inline a constant `_AUTHORED_DEFINITIONS` in the seed module — it is one
entry, not worth a file; documented + tested). The misconception `opposes` target must
resolve to a minted entity, so any `def.*` referenced by `misconceptions.json` is minted from
this authored list.

### D6. declared solution path representation — PICK: ordered list of reference-node ids, in `payload.declared_paths`
**Decision:** each problem's payload gains `"declared_paths": [[node_id, node_id, ...]]` — a
list of paths, each path an ordered list of the problem's own reference-node ids. v1 declares
exactly ONE path per problem (the degenerate case, spec §6.2). The order is the steps' `step`
field order (procedure order), e.g. p01: `[["continuity","incompressibility","bernoulli",
"horizontal_simplification","plan_apply_continuity","plan_apply_horizontal_simplification",
"plan_solve_bernoulli_for_p2"]]`.
**Why (grounded §6.1 + §6.2):** "A reference graph declares one or more acceptable solution
paths (v1 authors a single path)"; "an empty declared-path list is a reference-validation
failure that blocks grading at pipeline step 3 — which is why the §8 seed script must declare
paths for the bernoulli problems, or the day-one fixtures fail there." The schema supports
multiple paths (list of lists) from day one (retrofitting is expensive, §6.2) even though v1
populates one. Storing node ids (not entity ids) keeps the path human-readable and stable
against entity re-minting; the grading core resolves node id → `entity_key` → entity via the
same step annotation (D2). The path covers ALL of that problem's reference nodes (a complete
single path), so every node is on the declared path — required for the validation contract.

### D7. course-scoping + idempotency mechanism — PICK: resolve concept by (subject.search_space_id, concept.slug); idempotent upsert keyed on (concept_id, canonical_key)
**Decision:**
- **Course scoping:** the seeder takes a `search_space_id` (CLI `--search-space-id`, default =
  `MIN(aita_search_spaces.id)` bootstrap course, matching the migration backfill + the existing
  registry seeder). It resolves the bernoulli concept as
  `apollo_concepts JOIN apollo_subjects ON subject_id WHERE apollo_subjects.search_space_id = :sid
  AND apollo_concepts.slug = 'bernoulli_principle'`. Every entity it writes inherits course
  ownership through `concept_id → subject → search_space_id` (no `search_space_id` column on
  `apollo_kg_entities` — it is inherited, per migration 026 design). Two courses each get their
  OWN entity rows (different `concept_id`) — proven by the two-course isolation test.
- **Idempotency:** every entity write is an upsert keyed on `(concept_id, canonical_key)` —
  `SELECT` existing, `UPDATE` in place (display_name/payload/aliases) or `INSERT`. Every prereq
  write is `INSERT ... ON CONFLICT (from_entity_id, to_entity_id) DO NOTHING` (composite PK).
  Reference-solution annotation is keyed by the `payload.layer1_seeded` marker (skip if already
  set) AND is itself idempotent (re-annotating writes the same keys). A second full run inserts
  zero new rows and raises zero unique violations. The stats dict reports
  `{entities_inserted, entities_updated, prereqs_inserted, prereqs_skipped, problems_annotated}`
  so the idempotency test asserts second-run inserts == 0.

## Structural prep (from neighborhood scan)

Files in the change path scanned (imports / exported symbols / coupling):
- `scripts/seed_apollo_concept_registry.py` — 8 imports, ~7 module functions, ~255 lines. NOT
  edited by WU-3B (new sibling script). Clean.
- `apollo/persistence/models.py` — 559 lines, ~13 ORM classes; NOT edited (WU-3B reads it).
  It is a coupling hub (imported widely) but WU-3B only imports `KGEntity`, `EntityPrereq`,
  `Concept`, `Subject`, `ConceptProblem`, `ENTITY_KINDS` — read-only, no new coupling.
- `apollo/persistence/learner_model_seed.py` — NEW, will be <400 lines (pure functions + a thin
  DB write layer). Keep the pure conversion core import-free of SQLAlchemy so the fast tests
  need no DB (the conversion functions take dicts, return dicts/dataclasses).
- `apollo/subjects/.../bernoulli_principle/problems/problem_*.json` — data files, additive edit.
- New test files — net-new.

**Result: neighborhood is clean.** No file in the change path exceeds the >8-import / >20-method
/ circular-import / >10-fan-in thresholds in a way WU-3B worsens. No structural prep required.
- Verify: `rg -c "^(import|from) " apollo/persistence/learner_model_seed.py` stays < 12 once written.

## Layered tasks (ORDER MATTERS — TDD: tests first per layer)

### 0. (No migration) — reference WU-3A
- [ ] No DDL in WU-3B. Migration 026 (WU-3A) is the schema; this unit writes ROWS only.
- Justification for "no migration layer" (per layered-task-order rule): all target tables
  (`apollo_kg_entities`, `apollo_entity_prereqs`) and columns (`apollo_concept_problems.payload`)
  already exist on this branch's base. The opposes-link and ref-links use existing JSONB columns
  (D2/D3) precisely so NO new DDL is needed (and the scope forbids touching migrations).
- Verify: `psql ... -c "\d apollo_kg_entities"` shows the table (already true on the test harness).

### 1. Misconception + definitions authoring source (data files) — write the DATA the tests assert against, FIRST
- [ ] Create `apollo/subjects/fluid_mechanics/concepts/bernoulli_principle/misconceptions.json`
      (D4 format) with ≥2 entries (`misc.pressure_velocity_same_direction` → opposes
      `def.pressure_velocity_tradeoff`; `misc.density_ignored` → opposes `cond.incompressible`).
- [ ] The single authored definition `def.pressure_velocity_tradeoff` is the opposes target of a
      misconception but is not a problem reference node — it is supplied by the
      `_AUTHORED_DEFINITIONS` constant in the seed module (D5), NOT a separate file.
- Verify: `python -c "import json,io;json.load(io.open('.../misconceptions.json',encoding='utf-8'))"` parses; every `opposes` target is either a `def.*`/`cond.*`/`eq.*` key the seed will mint.

### 2. Pure conversion core — `apollo/persistence/learner_model_seed.py` (NO SQLAlchemy import in this layer)
Write the failing fast tests first (Task list below), then implement these pure functions.
Each takes plain dicts (the parsed JSON) and returns plain dataclasses/dicts — no DB, no LLM.
- [ ] `concept_dag_to_entities(dag: dict) -> list[EntitySpec]` — one `EntitySpec(canonical_key=
      "concept.<id>", kind="concept", display_name=<label>, payload={"scope_boundary": [...]},
      aliases=[])` per node (14).
- [ ] `concept_dag_to_prereqs(dag: dict) -> list[tuple[str, str]]` — one
      `("concept.<from>", "concept.<to>")` per edge (16), independent of edge `type`.
- [ ] `symbols_to_entities(symbols: dict, normalization: dict) -> list[EntitySpec]` — one
      `EntitySpec(canonical_key="var.<sym>", kind="variable", display_name=<description[sym]>,
      aliases=<phrases normalizing to sym>)` per canonical symbol (7). Aliases are the
      `normalization_map` keys whose value == that symbol. The `q` (dynamic pressure) target is
      NOT a canonical symbol ⇒ its 1 mapping is dropped with a logged warning (R5), OR a
      `var.q` entity is minted — PICK: **mint `var.q`** so no alias is silently lost (kind
      variable, display "dynamic pressure"); documented + tested. Net: 8 variable entities, all
      23 mappings placed.
- [ ] `reference_solution_to_entities(problem: dict) -> list[EntitySpec]` — one EntitySpec per
      reference node (kind+key from D5 mapping), `display_name` from `content.label` (fallback to
      the node id humanized), `payload` carrying `symbolic`/`applies_when`/`transformation`/`order`
      as present. Dedup by `canonical_key` happens at the seed-flow layer (Task 4), not here.
- [ ] `misconceptions_to_entities(misc: dict) -> list[EntitySpec]` — one `EntitySpec(
      canonical_key="misc.<...>", kind="misconception", payload={"opposes_entity_key": <opposes>},
      aliases=<trigger_phrases>)` per entry.
- [ ] `authored_definitions() -> list[EntitySpec]` — returns `_AUTHORED_DEFINITIONS` (the single
      `def.pressure_velocity_tradeoff`).
- [ ] `annotate_reference_solution(problem: dict, key_for_node: Callable[[str], str]) -> dict` —
      returns a NEW problem dict (immutable; no in-place mutation per coding-style) with each
      step gaining `"entity_key"`, the problem gaining `"declared_paths"` (D6, one complete
      ordered path) and `"layer1_seeded": true`. `key_for_node` maps a reference-node id to its
      minted `canonical_key`.
- `EntitySpec` is a `@dataclass(frozen=True)` (immutability rule) with
  `canonical_key, kind, display_name, payload (Mapping), aliases (tuple)`.
- Verify: `pytest apollo/persistence/tests/test_learner_model_seed_convert.py -q` GREEN;
  `python -c "import apollo.persistence.learner_model_seed"` imports without sqlalchemy loaded
  (the pure layer is import-light).

### 3. Reference-graph validation contract function (§6.1) — the WU-4A gate, in the same module
- [ ] `validate_reference_graph(problem: dict) -> ReferenceGraphValidation` — pure function over
      an annotated problem payload. Returns a frozen result
      `ReferenceGraphValidation(ok: bool, missing_entity_links: tuple[str,...],
      undeclared_paths: bool, errors: tuple[str,...])`.
- [ ] Contract (binding, from §6.1 / §6.6): `ok` is True iff **(a)** every reference-solution
      step has a non-empty `entity_key` AND **(b)** `declared_paths` is present and non-empty AND
      **(c)** every node id in every declared path is a real reference-node id of the problem AND
      **(d)** every reference-node id appears on ≥1 declared path. Any failure ⇒ `ok=False` with a
      populated reasons tuple (this is what would "block grading" at WU-4A pipeline step 3).
- This function is the executable form of "the fixture this unit ships must PASS §6.1". WU-4A
  imports it; WU-3B's seed test asserts every seeded bernoulli problem passes it.
- Verify: `pytest apollo/persistence/tests/test_reference_graph_validation.py -q` GREEN.

### 4. Course-scoped seeder script — `scripts/seed_apollo_learner_model.py` (DB write layer)
Write the failing real-PG seed tests first (Task list below), then implement.
- [ ] `async def seed(database_url, *, search_space_id=None, dry_run=False) -> dict` — the public
      entrypoint. Mirrors `seed_apollo_concept_registry.seed`'s engine/sessionmaker/commit shape.
- [ ] Resolves the bernoulli concept by `(search_space_id or MIN(spaces), slug='bernoulli_principle')`
      (D7). Errors loud (`SeedError`) if no such concept (the registry seeder must have run first).
- [ ] Reads the on-disk bernoulli source files (reusing the `_REGISTRY_ROOT` path idiom), calls
      the Task-2 pure converters, dedups reference-derived + concept + variable + misconception +
      definition EntitySpecs by `canonical_key` (one row per key), and upserts via
      `_upsert_entity(session, concept_id, spec)` keyed on `(concept_id, canonical_key)`.
- [ ] Second pass: resolve `payload.opposes_entity_key` → `opposes_entity_id` (now that all rows
      exist) and write it back to misconception payloads (D3).
- [ ] Writes prereq edges via `INSERT ... ON CONFLICT DO NOTHING` after mapping
      `canonical_key → entity_id`.
- [ ] Annotates each problem's `reference_solution` (Task-2 `annotate_reference_solution`) and
      writes the annotated payload back to `apollo_concept_problems.payload` for THIS course's
      concept (idempotent: skip if `payload.layer1_seeded` already true, but re-annotation is a
      no-op anyway). ALSO writes the annotated JSON back to the on-disk `problem_*.json`
      (additive keys only) so a fresh registry re-import carries the links (D2).
- [ ] `main(argv)` CLI: `--database-url`, `--search-space-id`, `--dry-run`, `--verbose`; returns
      a stats dict. Same arg style as the registry seeder.
- [ ] NEW exception `SeedError(RuntimeError)` in the seed module (named error per spec NO-FALLBACK
      convention; not a new global filter — this is a script).
- Verify: `pytest tests/database/test_seed_apollo_learner_model.py -q` GREEN (Testcontainers);
  `python -m scripts.seed_apollo_learner_model --dry-run --database-url <local>` prints stats.

### 5. Owner-doc updates (same work, drift contract)
- [ ] `docs/architecture/apollo.md` (owns `apollo/**`) — document the new
      `apollo/persistence/learner_model_seed.py` module (pure converters + the §6.1
      `validate_reference_graph` contract), the new `misconceptions.json` source file, and the
      reference-solution payload annotation (`entity_key`/`declared_paths`/`layer1_seeded`). Set
      `last_verified: 2026-06-15`.
- [ ] `docs/architecture/_overview.md` (owns `scripts/**`) — add
      `scripts/seed_apollo_learner_model.py` to the scripts inventory line (currently lists three
      one-shot tools). Set `last_verified: 2026-06-15`.
- [ ] `docs/architecture/domain-data.md` — the seeder writes into migration-026 tables; append a
      one-line note to the 026 narrative that WU-3B's seeder populates Layer-1 rows + annotates
      reference solutions. Set `last_verified: 2026-06-15`. (Per task instruction: update this
      doc because the seeder interacts with the 026 schema it narrates.)
- Verify: `rg "learner_model_seed|misconceptions.json|layer1_seeded" docs/architecture/` returns hits; all three docs show `last_verified: 2026-06-15`.

## Full test list (names + asserts + LLM mock)

**LLM mock posture (binding):** WU-3B uses NO LLM at all — the seed is a deterministic
conversion of hand-authored files. There is nothing to mock; tests assert exact dict/row
output. If the executor finds an unavoidable LLM call (there should be none), it must be
mocked deterministically with a fixed return — but the plan's design has zero LLM calls
(Risk R3 records why no embeddings are computed here either).

### Fast pure-unit tests — `apollo/persistence/tests/test_learner_model_seed_convert.py` (no DB, no LLM)

| Test name | Asserts | Mock |
|---|---|---|
| `test_concept_dag_to_entities_one_per_node` | reads the REAL `concept_dag.json`; returns exactly 14 EntitySpecs, all `kind='concept'`, keys `concept.<id>` for each node id; `concept.bernoulli_principle` present; `scope_boundary` carried in payload for `bernoulli_principle`. | none |
| `test_concept_dag_to_prereqs_one_per_edge` | returns exactly 16 `(from_key,to_key)` pairs; `("concept.bernoulli_principle","concept.energy_conservation_fluid")` present; the lone `extends` edge `("concept.horizontal_flow_simplification","concept.bernoulli_principle")` present; no dependence on edge `type`. | none |
| `test_symbols_to_entities_seven_canonical_plus_q` | returns 8 variable EntitySpecs (`var.P`..`var.Q` + `var.q`); `var.P.kind=='variable'`; display names from `description`. | none |
| `test_symbols_aliases_from_normalization_map` | all 23 normalization keys are placed as aliases on the variable matching their target; `"static pressure"` ∈ `var.P.aliases`, `"flow speed"` ∈ `var.v.aliases`, `"dynamic pressure"` ∈ `var.q.aliases`; total alias count across variables == 23. | none |
| `test_reference_solution_to_entities_kinds_and_keys` | for REAL `problem_01.json`: 7 EntitySpecs; `eq.continuity` kind `equation` carries `payload.symbolic`; `cond.incompressibility` kind `condition` carries `applies_when`; `simp.horizontal*` mapped to `kind='condition'` with `simp.` prefix; procedure ids → `kind='procedure'` `proc.*`. | none |
| `test_reference_entities_dedup_shared_ids_across_problems` | converting problems 01+03+05 and deduping by `canonical_key` yields ONE `eq.continuity`, ONE `eq.bernoulli`, ONE `cond.incompressibility` (not 2-3 each). | none |
| `test_misconceptions_to_entities` | reads REAL `misconceptions.json`; each entry → 1 EntitySpec `kind='misconception'`, key `misc.*`, `payload.opposes_entity_key` set, trigger phrases become aliases. | none |
| `test_authored_definitions_includes_pressure_velocity_tradeoff` | `authored_definitions()` returns ≥1 spec including `def.pressure_velocity_tradeoff` (`kind='definition'`) — the misconception opposes-target. | none |
| `test_annotate_reference_solution_adds_entity_keys_and_path` | `annotate_reference_solution(problem_01, key_for_node)` returns a NEW dict (original unchanged — immutability); every step has `entity_key`; problem has `declared_paths` (one path) covering all 7 node ids in step order; `layer1_seeded` true. | stub `key_for_node` |
| `test_annotate_is_immutable` | the input problem dict is byte-identical (deep-equal) after the call; the result is a different object. | none |
| `test_every_misconception_opposes_target_is_minted` | the union of all minted keys (concept+var+ref+def) contains every `opposes_entity_key` referenced by `misconceptions.json` (guards a dangling opposes-link before it reaches the DB). | none |

### Reference-graph validation tests — `apollo/persistence/tests/test_reference_graph_validation.py` (no DB)

| Test name | Asserts | Mock |
|---|---|---|
| `test_validate_passes_on_fully_annotated_problem` | a problem with every step `entity_key` set + a complete `declared_paths` → `ok is True`, empty reasons. | none |
| `test_validate_fails_missing_entity_link` | drop one step's `entity_key` → `ok is False`, the node id in `missing_entity_links`. | none |
| `test_validate_fails_empty_declared_paths` | `declared_paths=[]` (or absent) → `ok is False`, `undeclared_paths is True` (the §6.1 "empty path list blocks grading" case). | none |
| `test_validate_fails_path_references_unknown_node` | a path listing a node id not in the reference solution → `ok is False`, error mentions the bad id. | none |
| `test_validate_fails_node_not_on_any_path` | a reference node absent from every declared path → `ok is False` (coverage of all nodes is required). | none |

### Real-PG seed tests — `tests/database/test_seed_apollo_learner_model.py` (Testcontainers, marked `integration`)

Harness: copy the WU-3A pattern — `_pg_url` session fixture, `_chain_migrations()` + `_STUB_DDL`
+ 026, a fresh migrated DB. UNLIKE WU-3A this test must seed REAL `apollo_subjects`/
`apollo_concepts`/`apollo_concept_problems` rows for bernoulli (the seeder reads them), so add a
`_seed_bernoulli_curriculum(conn, space_id)` helper that inserts a subject `fluid_mechanics`
(course-scoped), a concept `bernoulli_principle`, and the 5 problems (load the real
`problem_*.json` payloads). Then run `seed(dsn, search_space_id=...)` via SQLAlchemy and assert
on the rows. Per-test isolation: each test builds on its own DB or wraps in a rollback like WU-3A.

| Test name | Asserts | Mock |
|---|---|---|
| `test_seed_creates_concept_entities_one_per_dag_node` | after `seed`, `count(apollo_kg_entities WHERE concept_id=:bernoulli AND kind='concept') == 14`; `concept.bernoulli_principle` present. | none (no LLM) |
| `test_seed_creates_variable_entities_from_symbols` | `count(... kind='variable') == 8`; `var.P` present with display 'pressure'. | none |
| `test_seed_creates_prereq_edges_one_per_dag_edge` | `count(apollo_entity_prereqs)` for the 14 concept entities == 16; the bernoulli→energy_conservation edge resolves to real entity ids. | none |
| `test_seed_populates_aliases_from_normalization_map` | the `var.P` row's `aliases` JSONB contains 'static pressure'; summed alias length across variable rows == 23. | none |
| `test_seed_creates_misconception_entities_with_opposes_id` | every `kind='misconception'` row has `payload->>'opposes_entity_id'` non-null AND it points at a real entity row whose `canonical_key` equals the authored `opposes` key (e.g. `misc.density_ignored` opposes the `cond.incompressible*` entity). | none |
| `test_seed_links_every_reference_node_to_an_entity` | for each of the 5 problems, every reference-solution step in the stored `apollo_concept_problems.payload` has a non-empty `entity_key` resolving to an existing `apollo_kg_entities.canonical_key` for this concept. | none |
| `test_seed_declares_one_path_per_problem` | each problem payload has `declared_paths` length ≥1, path 0 non-empty, covering every reference node id. | none |
| `test_seeded_problem_passes_reference_graph_validation` | **(the WU-4A gate)** load each seeded problem payload from the DB, call `validate_reference_graph` → `ok is True` for all 5. This is the binding fixture assertion. | none |
| `test_seed_is_idempotent_second_run_inserts_nothing` | run `seed` twice on the same DB; second run's stats show `entities_inserted==0`, `prereqs_inserted==0`; total entity/prereq counts unchanged; NO `UniqueViolationError` raised. | none |
| `test_seed_is_course_scoped_two_courses_do_not_collide` | seed two courses (each with its own `fluid_mechanics` subject + bernoulli concept under a distinct `search_space_id`); assert each course's bernoulli concept owns its OWN 14 concept entities (different `concept_id`), and deleting course-1's space cascades only course-1's entities (course-2 intact). | none |
| `test_seed_errors_when_concept_missing` | run `seed` against a DB with the space but NO bernoulli concept → raises `SeedError` (the registry seeder must run first). | none |
| `test_dedup_shared_reference_entities_single_row` | after seeding all 5 problems, `count(... canonical_key='eq.continuity') == 1` even though continuity appears in problems 01/03/05 — proves cross-problem dedup. | none |

**Coverage note (CLAUDE.md ≥95% patch coverage vs `feat/apollo-kg-wu3a-learner-model-migration`):**
the pure converters + `validate_reference_graph` are fully covered by the fast suite (every
branch: the `q`-not-canonical path, the simplification→condition path, each validation failure
reason). The DB write layer (`seed`, `_upsert_entity`, opposes resolution, prereq insert,
payload annotation, `SeedError`) is covered by the Testcontainers suite. CLI `main()` is
covered by one fast test calling `main(["--dry-run","--database-url","sqlite+..."])`? — NO,
`main` needs Postgres; instead cover `main`'s arg-parsing + missing-URL branch with a fast test
(`test_main_requires_database_url` asserting return code 2) and the happy path via the dry-run
DB test. diff-cover must pass ≥95 on the changed lines.

## Public signatures (backward-compat noted)

**NEW — `apollo/persistence/learner_model_seed.py` (pure layer, no SQLAlchemy import):**
```python
from dataclasses import dataclass
from collections.abc import Mapping, Callable

@dataclass(frozen=True)
class EntitySpec:
    canonical_key: str
    kind: str                     # one of ENTITY_KINDS
    display_name: str
    payload: Mapping[str, object] # frozen-ish; copied on write
    aliases: tuple[str, ...]

@dataclass(frozen=True)
class ReferenceGraphValidation:
    ok: bool
    missing_entity_links: tuple[str, ...]
    undeclared_paths: bool
    errors: tuple[str, ...]

class SeedError(RuntimeError): ...

def concept_dag_to_entities(dag: dict) -> list[EntitySpec]: ...
def concept_dag_to_prereqs(dag: dict) -> list[tuple[str, str]]: ...   # (from_key, to_key)
def symbols_to_entities(symbols: dict, normalization: dict) -> list[EntitySpec]: ...
def reference_solution_to_entities(problem: dict) -> list[EntitySpec]: ...
def misconceptions_to_entities(misc: dict) -> list[EntitySpec]: ...
def authored_definitions() -> list[EntitySpec]: ...
def annotate_reference_solution(
    problem: dict, key_for_node: Callable[[str], str]
) -> dict: ...                                     # returns a NEW dict (immutable)
def validate_reference_graph(problem: dict) -> ReferenceGraphValidation: ...
```

**NEW — `scripts/seed_apollo_learner_model.py` (DB write layer):**
```python
async def seed(
    database_url: str,
    *,
    search_space_id: int | None = None,   # default = MIN(aita_search_spaces.id)
    dry_run: bool = False,
) -> dict[str, int]: ...                   # {"entities_inserted","entities_updated",
                                           #  "prereqs_inserted","prereqs_skipped",
                                           #  "misconceptions_linked","problems_annotated"}
def main(argv: list[str] | None = None) -> int: ...  # CLI; mirrors registry seeder
# private: _resolve_concept(session, search_space_id) -> int
#          _upsert_entity(session, concept_id, spec: EntitySpec) -> tuple[int, bool]  # (id, inserted)
#          _link_opposes(session, concept_id) -> int
#          _insert_prereqs(session, key_to_id, pairs) -> tuple[int, int]
#          _annotate_problems(session, concept_id, write_disk: bool) -> int
```

**Backward-compat (binding):** `scripts/seed_apollo_concept_registry.py` is NOT modified — its
public functions (`seed`, `_upsert_subject`, `_upsert_concept`, `_upsert_problem`, `_scan_registry`,
`main`) keep their signatures, so `test_seed_subject_search_space.py` stays green. The
`apollo/persistence/models.py` ORM is read-only here; no model edits, so all WU-3A model tests
(`test_learner_model_models.py`, `test_learner_model_allowlists.py`) stay green. The on-disk
`problem_*.json` edits are ADDITIVE (new keys: `entity_key` per step, `declared_paths` +
`layer1_seeded` per problem) — no existing key removed/renamed, so the registry seeder's
re-import of those files (which copies the whole payload) keeps working and now carries links.

## Idempotency + course-scoping contract

**Idempotency (re-run = no-op):**
- Entities: `SELECT WHERE (concept_id, canonical_key)` → UPDATE in place or INSERT. Re-run
  finds every row and UPDATEs (display/payload/aliases identical) → 0 inserts.
- Prereqs: `INSERT ... ON CONFLICT (from_entity_id, to_entity_id) DO NOTHING`. Re-run → 0 rows.
- Opposes-link: idempotent UPDATE of `payload.opposes_entity_id` (resolves to the same id).
- Problem annotation: `annotate_reference_solution` is deterministic; re-writing the same
  payload is a no-op. The `payload.layer1_seeded` marker lets the seeder short-circuit but the
  annotation itself is safe to repeat.
- Stats assertion: `entities_inserted == 0` and `prereqs_inserted == 0` on the second run
  (`test_seed_is_idempotent_second_run_inserts_nothing`).

**Course-scoping (isolation invariant §1.4):**
- Every entity is written under the bernoulli concept of ONE course (resolved by
  `subject.search_space_id`). `apollo_kg_entities` has no `search_space_id` column — ownership
  is inherited through `concept_id → apollo_subjects.search_space_id` (migration-026 design).
- Two courses each have their own `fluid_mechanics` subject + `bernoulli_principle` concept
  (distinct `concept_id`) ⇒ distinct entity rows ⇒ no collision. The per-concept
  `UNIQUE(concept_id, canonical_key)` allows the same `canonical_key` (`eq.continuity`) under
  two different concepts. Proven by `test_seed_is_course_scoped_two_courses_do_not_collide`.
- Deleting a course (`aita_search_spaces` row) cascades the whole chain
  (subject→concept→entities→prereqs) for that course only — asserted in the same test.

## §6.1 reference-graph validation contract (the WU-4A fixture gate)

`validate_reference_graph(problem)` is the executable form of the spec's "reference-graph
validation (every reference node MUST carry an entity link AND paths MUST be declared or
grading is blocked)". It returns `ok=True` ONLY when all four hold:

1. **Every reference node carries an entity link** — each step in `reference_solution` has a
   non-empty `entity_key`. (§6.1 / §5 "every reference node must carry an entity link".)
2. **Paths are declared** — `declared_paths` exists, is a list, and is non-empty. (§6.1 "an
   empty declared-path list is a reference-validation failure that blocks grading at pipeline
   step 3 — which is why the §8 seed script must declare paths".)
3. **Paths reference only real nodes** — every id in every path is a reference-node id of this
   problem (no dangling path entries).
4. **Every node is covered by ≥1 path** — no reference node is left off all declared paths
   (v1's single path is complete, so this holds by construction; the check guards future
   multi-path edits).

WU-4A imports `validate_reference_graph` and runs it at its pipeline step 3; if any seeded
problem returned `ok=False`, WU-4A would block grading. WU-3B's seed test
`test_seeded_problem_passes_reference_graph_validation` asserts all 5 seeded bernoulli problems
return `ok=True` — that is the contract WU-4A depends on, made green here.

## Owner-doc updates

Per the drift contract (CLAUDE.md), reconcile in the same work and bump `last_verified` to
2026-06-15:

- **`docs/architecture/apollo.md`** (owns `apollo/**`): add `apollo/persistence/learner_model_seed.py`
  to the module map (pure converters + `validate_reference_graph`); note the new
  `bernoulli_principle/misconceptions.json` authoring source; document that bernoulli
  `problem_*.json` reference solutions now carry `entity_key` per step + `declared_paths` +
  `layer1_seeded`. Cross-reference WU-3B and the spec §8 v1 seed.
- **`docs/architecture/_overview.md`** (owns `scripts/**`): extend the `scripts/` inventory line
  (currently "Three one-shot tools") to include `seed_apollo_learner_model.py` (course-scoped,
  idempotent, layers Layer-1 rows + reference-solution links on top of the registry seeder).
- **`docs/architecture/domain-data.md`**: append to the migration-026 narrative that WU-3B's
  seeder is what populates `apollo_kg_entities` / `apollo_entity_prereqs` and annotates
  `apollo_concept_problems.payload` with entity links + declared paths.

## Risks

Confidence-rated (HIGH = likely to bite, plan mitigates; LOW = noted, unlikely).

- **[MEDIUM] R1 — on-disk JSON write-back couples the seeder to the filesystem.** D2 has the
  seeder edit `problem_*.json` in place so a fresh registry re-import carries links. This writes
  source-controlled files at seed time, which is unusual for a DB seeder. Mitigation: gate the
  disk write behind a `--write-disk/--no-write-disk` flag (default ON), and the executor commits
  the annotated JSONs as part of the PR (they are deterministic). If the reviewer objects, fall
  back to DB-only annotation (the runtime reads the DB payload anyway, §8A criterion 7) — the
  on-disk write is a convenience for re-import, not a correctness requirement. Tests cover the
  DB payload regardless of the disk flag.
- **[MEDIUM] R2 — opposes-target must be minted before the link resolves.** A misconception's
  `opposes` key (e.g. `def.pressure_velocity_tradeoff`, `cond.incompressible`) must correspond
  to a real minted entity. `def.pressure_velocity_tradeoff` is supplied by `_AUTHORED_DEFINITIONS`;
  `cond.incompressible` must match the key minted from the problems' `incompressibility`
  condition. NOTE the naming mismatch: the reference node id is `incompressibility` → key
  `cond.incompressibility`, but the spec §6.9 / corpus call it `incompressible`. The executor
  MUST make `misconceptions.json`'s `opposes` use the ACTUAL minted key (`cond.incompressibility`),
  not the spec's prose `incompressible`. `test_every_misconception_opposes_target_is_minted`
  (fast) catches a mismatch before the DB; `test_seed_creates_misconception_entities_with_opposes_id`
  (DB) confirms the resolved id is non-null.
- **[MEDIUM] R3 — no embeddings seeded for misconception entities.** The misconception
  inference channel (`apollo_misconceptions`, migration 019) uses `description_embedding`
  (pgvector, computed via an embedding API). WU-3B's `canon.misc.*` Layer-1 entities are a
  DIFFERENT table (`apollo_kg_entities`) and the spec's resolution (§5) competes them by
  alias/symbolic/fuzzy match, NOT embeddings — so NO embedding is computed at seed time (keeps
  the seed deterministic + LLM-free). If WU-4A later needs embeddings on entities, that is a
  follow-on. Recorded so the executor does not add an embedding call.
- **[LOW] R4 — spec count drift.** Spec §8 says "14 nodes / 16 edges" and the file matches; one
  early Read view truncated a node line, but `json.load` confirmed 14/16. Variable count is 8
  (7 canonical + `var.q`), not 7, because of the `q` dynamic-pressure mapping (R5). Plan +
  tests use the ACTUAL counts (binding constraint: follow the file).
- **[LOW] R5 — `q` (dynamic pressure) is in `normalization_map` but not `canonical_symbols`.**
  Decision D5: mint `var.q` so the alias is not silently lost; net 8 variable entities, all 23
  mappings placed. Alternative (drop+log) is the deviation in the next section.
- **[LOW] R6 — `extends` vs `requires` edge type is dropped in Layer 1.** `apollo_entity_prereqs`
  has no type column; both become prereq edges. The one `extends` edge
  (horizontal_flow_simplification → bernoulli_principle) is preserved as a prereq. If a future
  unit needs the distinction it lives in `concept_dag` JSON on the concept row still. Acceptable
  for v1 (the spec's Layer-1 prereq closure is type-agnostic).
- **[LOW] R7 — two "misconception" stores must not be conflated.** `apollo_misconceptions`
  (migration 019, inference bank, embeddings) ≠ Layer-1 `kind='misconception'` entities
  (migration 026, resolution competitors). WU-3B writes ONLY the latter. The plan names both to
  prevent the executor cross-wiring them.
- **[LOW] R8 — Testcontainers/Docker required for the DB suite.** The `_pg_url` fixture skips
  cleanly if Docker is absent, but the hardened verify stage REQUIRES these tests green (not
  skipped). The executor must run the DB suite with Docker up; CI runs the integration job.

## Out-of-scope boundaries (this unit)

WU-3B is the DATA FIXTURE only. Explicitly NOT in this unit:
- **NO `:Canon` projection / resolver / `RESOLVES_TO`** (WU-3C). No Neo4j writes at all.
- **NO grading core** (`apollo/graph_compare/`, coverage/soundness/bisimilarity, transcript
  audit, abstention gates, decision table) — WU-4A. WU-3B only ships the
  `validate_reference_graph` contract WU-4A consumes; it does not grade.
- **NO §8A runtime cutover** (WU-3D): no deleting `_AVAILABLE_CLUSTERS`/`_CLUSTER_TO_CONCEPT`,
  no `load_concept` DB rewrite, no `apollo_sessions.concept_id` population, no async-loader
  ripple. The seeder writes rows; the runtime read-path change is a separate unit.
- **NO §8B auto-provisioning** (materials → Apollo pipeline, eight-gate lint, dedup ladder).
- **NO Layer-3 learner math** (belief update, decay, events) — that is phase 5; WU-3B writes
  Layer-1 only and touches none of `apollo_learner_state`/`apollo_mastery_events`.
- **NO new migration / DDL** — uses existing migration-026 tables + existing JSONB columns.
- **NO LLM/embedding calls** — deterministic conversion of hand-authored files only.
- **NO edits to `seed_apollo_concept_registry.py`** or `apollo/persistence/models.py` (read-only).
- **NO touching files outside the scope list** (the seeder script, the new seed module, the
  bernoulli source/problem JSONs, the new misconceptions.json, the two test files, the three
  owner docs).

## Deviations I'd allow the executor

- **`var.q` handling (R5):** if the executor prefers, drop the single `q` mapping with a logged
  warning instead of minting `var.q` (7 variable entities, 22 placed aliases). Either is
  defensible; the plan picks mint-`var.q` to lose nothing. Update the affected test counts to match.
- **`misconceptions.json` content:** the exact misconception entries + their `opposes` targets
  are authoring data; the executor may add more bernoulli misconceptions (e.g. a continuity
  misconception) as long as every `opposes` target resolves to a minted key and the tests assert
  "every entry → one entity with non-null `opposes_entity_id`" rather than a hard count.
- **`_AUTHORED_DEFINITIONS` location:** inline constant in the seed module (plan default) vs a
  sibling `definitions.json` source file — either is fine; pick the file form if more
  definitions appear. Keep it LLM-free and tested.
- **On-disk write-back flag (R1):** default ON is the plan's pick; the executor may default it
  OFF and rely on DB-only annotation if review prefers not to mutate source JSON at seed time.
  Tests must still assert the DB payload is annotated either way.
- **DB-test harness reuse:** copy the small WU-3A helpers into the new test file vs import them
  from the WU-3A module — either is acceptable; copying avoids coupling two test modules. The
  `_pg_url` session fixture MUST be reused (it is the shared container).
- **NOT negotiable:** layered order (data files → pure converters → validation contract → DB
  seeder → docs); tests-first; no LLM; no new DDL; course-scoping + idempotency assertions;
  the `validate_reference_graph` contract returning `ok=True` for all 5 seeded problems.

