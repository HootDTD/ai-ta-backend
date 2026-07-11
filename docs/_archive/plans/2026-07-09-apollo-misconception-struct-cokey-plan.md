# Implementation Plan — Apollo misconception detector structural co-key ("F-struct")

> **For agentic workers — REQUIRED SUB-SKILL:** execute this plan under
> `superpowers:subagent-driven-development` (or `superpowers:executing-plans` if
> run in a fresh session with review checkpoints). Every task is a strict
> RED → GREEN → COMMIT loop; do not batch tasks, do not skip the failing-test
> step.

**Design spec:** `docs/_archive/specs/2026-07-09-apollo-misconception-struct-cokey-design.md`
**Owner doc (drift):** `docs/architecture/apollo.md`
**Branch:** `feat/apollo-misc-trace` (@ `dbcc81e`)

**Goal (1 sentence):** Make misconception docks deterministic by letting the
graph name the misconception the judge only localized — a confident
`wrong`/`misconception` judge verdict at reference node X whose `entity_key`
matches some bank entry's `opposes` docks via the existing co-key machinery.

**Architecture (2-3 sentences):** The judge tier already localizes error to a
reference node reliably (`clear` vs `wrong`), but names the misconception
unreliably. We thread the authored `entity_key` (problem step → runtime `Node`)
and `opposes` (misconceptions.json → `apollo_misconceptions` → runtime
`MisconceptionEntry`) through to the gate, which — behind a new sub-flag — docks a
localized-but-unnamed verdict when a bank entry opposes that node. The gate stays
pure: the caller pre-resolves `entity_key → node_id → bank_code` into an
`opposes_index` it passes in.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy async (Postgres + SQLite test
variant), Pydantic v2, pytest / pytest-asyncio, asyncpg + Testcontainers pgvector
for the migration test, diff-cover for patch coverage.

---

## Global Constraints (verbatim — apply to EVERY task)

* **flag-OFF byte-identical behavior + output.** With `APOLLO_MISC_STRUCT_COKEY`
  unset/falsy the detector's penalty, `misconceptions[]`, composite, rubric, and
  trace output are byte-identical to today. The migration adds a nullable column
  only (no backfill / no default change), so it is a no-op until the flag flips.
* **Patch coverage ≥95% on changed lines vs `origin/staging`:**
  `pytest --cov --cov-report=xml` then
  `diff-cover coverage.xml --compare-branch=origin/staging --fail-under=95`.
  Full `pytest apollo/` with zero regressions. CI enforces this on PRs
  (`.github/workflows/ci.yml`, integration job).
* **Migration = numbered SQL, authored + tested on a LOCAL Docker Postgres /
  Testcontainers ONLY.** Agents NEVER apply migrations to any remote Supabase
  (test or prod) — remote rehearsal + prod apply is a human/CI step. For the DB
  change, enumerate the constraint/column/seed behaviors and test ≥95% of them
  locally (per the repo's DB test contract).
* **Drift:** update owner doc `docs/architecture/apollo.md` in the same work +
  bump `last_verified` to 2026-07-09.

---

## File Structure (every file created/modified + its one responsibility)

| File | C/M | Responsibility |
|------|-----|----------------|
| `apollo/ontology/nodes.py` | M | Add `entity_key: str \| None` to `_NodeBase`; thread it through `build_node`. |
| `apollo/schemas/problem.py` | M | Add `entity_key` to `ReferenceStep`; pass it into `build_node` in `to_kg_graph` so reference nodes carry it. |
| `database/migrations/038_apollo_misconception_opposes.sql` | C | Add nullable `opposes TEXT` column to `apollo_misconceptions`. |
| `apollo/persistence/models.py` | M | Add `opposes = Column(Text, nullable=True)` to ORM `Misconception`. |
| `apollo/overseer/misconception_bank.py` | M | Add `opposes` to `MisconceptionEntry` + `_from_row` + `match_by_embedding` row-build + `upsert_entry` SQL. |
| `apollo/persistence/misconception_bank_seed.py` | M | Add `opposes` to `MisconceptionBankSpec` + `misconception_entry_to_bank_spec`. |
| `scripts/seed_apollo_misconceptions.py` | M | Thread `spec.opposes` into the `upsert_entry` call. |
| `apollo/overseer/misconception_detector/config.py` | M | Add `APOLLO_MISC_STRUCT_COKEY` flag + `struct_cokey_enabled()`. |
| `apollo/overseer/misconception_detector/opposes_index.py` | C | Pure `build_opposes_index(reference_graph, bank_entries) → dict[node_id, bank_code]`. |
| `apollo/overseer/misconception_detector/gate.py` | M | New structural co-key branch in `_gate_one_concept`; `opposes_index` param on `gate_findings`. |
| `apollo/overseer/misconception_detector/trace.py` | M | Record structural match: `row3s_struct_cokey_dock` label + `struct_opposes_code`/`docked_via` fields. |
| `apollo/handlers/done.py` | M | Build `opposes_index` (flag-gated) + pass to `gate_findings`; extend trace call. |
| `campaign/validate_misconception_detector.py` | M | Build + pass `opposes_index`; extend trace call for the re-validation run. |
| `docs/architecture/apollo.md` | M | Document the structural co-key path; bump `last_verified`. |
| Test files (per task) | C | See each task. |

---

## Task order

Node field → schema plumbing → migration → ORM/loader/seeder → config flag →
opposes_index → gate structural path → trace extension → done.py wiring → drift
doc → live re-validation.

---

## Task 1 — `entity_key` on the runtime node

**Files:**
* Modify `apollo/ontology/nodes.py` (`_NodeBase` ~L39-57; `build_node` ~L163-192).
* Create `apollo/ontology/tests/test_nodes_entity_key.py`.

**Interfaces:**
* Produces: `_NodeBase.entity_key: str | None` (default `None`); `build_node(..., entity_key: str | None = None) -> Node`.

Steps:

- [ ] Write failing test `apollo/ontology/tests/test_nodes_entity_key.py`:

```python
"""entity_key plumbing on the runtime node (F-struct Task 1)."""
from __future__ import annotations

import pytest

from apollo.ontology.nodes import DefinitionNode, build_node

pytestmark = pytest.mark.unit


def test_node_base_defaults_entity_key_none() -> None:
    node = build_node(
        node_type="definition",
        node_id="real_basis",
        attempt_id=1,
        source="reference",
        content={"concept": "real GDP", "meaning": "inflation-adjusted"},
    )
    assert node.entity_key is None


def test_build_node_threads_entity_key() -> None:
    node = build_node(
        node_type="definition",
        node_id="real_basis",
        attempt_id=1,
        source="reference",
        content={"concept": "real GDP", "meaning": "inflation-adjusted"},
        entity_key="def.real_basis",
    )
    assert isinstance(node, DefinitionNode)
    assert node.entity_key == "def.real_basis"
    # Round-trips through pydantic serialization unchanged.
    assert node.model_dump()["entity_key"] == "def.real_basis"
```

- [ ] Run `pytest apollo/ontology/tests/test_nodes_entity_key.py -q` — expect **FAIL** (`build_node` has no `entity_key` kwarg / field absent).
- [ ] Minimal implementation in `apollo/ontology/nodes.py`. Add to `_NodeBase` (after `student_belief`):

```python
    # F-struct: the canonical entity/concept key this node maps to
    # (authoring name `entity_key`, e.g. "def.real_basis"). Populated on
    # REFERENCE nodes only (Problem.to_kg_graph); parser/system/legacy nodes
    # stay None. Lets the structural co-key gate name the misconception a bank
    # entry `opposes` for this node. Default None keeps every non-reference and
    # pre-F-struct node byte-identical.
    entity_key: str | None = Field(default=None)
```

Add the kwarg to `build_node` (signature + the `cls(...)` call):

```python
def build_node(
    *,
    node_type: NodeType,
    node_id: str,
    attempt_id: int,
    source: NodeSource,
    content: dict,
    parser_confidence: float = 1.0,
    status: NodeStatus = "ACCEPTED",
    student_belief: str | None = None,
    entity_key: str | None = None,
) -> Node:
    ...
    return cls(
        node_id=node_id,
        attempt_id=attempt_id,
        source=source,
        parser_confidence=parser_confidence,
        status=status,
        student_belief=student_belief,
        entity_key=entity_key,
        content=content,  # type: ignore[arg-type]
    )
```

- [ ] Run `pytest apollo/ontology/tests/test_nodes_entity_key.py -q` — expect **PASS**.
- [ ] Run `pytest apollo/ontology/ -q` — expect **PASS** (no regression on existing node tests).
- [ ] Commit: `feat(apollo): carry entity_key on the runtime KG node (F-struct 1/11)`

---

## Task 2 — `ReferenceStep.entity_key` survives `model_validate`; `to_kg_graph` populates it

**Files:**
* Modify `apollo/schemas/problem.py` (`ReferenceStep` ~L39-44; `to_kg_graph` `build_node` call ~L124-130).
* Create `apollo/schemas/tests/test_problem_entity_key.py`.

**Interfaces:**
* Consumes: `ReferenceStep.entity_key: str | None` from problem JSON.
* Produces: `Problem.to_kg_graph(...)` reference nodes carry `entity_key`.

Steps:

- [ ] Write failing test `apollo/schemas/tests/test_problem_entity_key.py`:

```python
"""entity_key survives Problem validation and reaches the reference graph."""
from __future__ import annotations

import pytest

from apollo.schemas.problem import Problem

pytestmark = pytest.mark.unit

_PROBLEM = {
    "id": "p1",
    "concept_id": "nominal_vs_real_gdp",
    "difficulty": "hard",
    "problem_text": "compute real growth",
    "reference_solution": [
        {
            "step": 1,
            "entry_type": "definition",
            "id": "real_basis",
            "content": {"concept": "real GDP", "meaning": "inflation-adjusted"},
            "entity_key": "def.real_basis",
        },
        {
            "step": 2,
            "entry_type": "procedure_step",
            "id": "do_it",
            "content": {"action": "subtract", "purpose": "isolate", "order": 1},
            "depends_on": ["real_basis"],
            "entity_key": "proc.do_it",
        },
    ],
}


def test_reference_step_keeps_entity_key() -> None:
    prob = Problem.model_validate(_PROBLEM)
    step = next(s for s in prob.reference_solution if s.id == "real_basis")
    assert step.entity_key == "def.real_basis"


def test_to_kg_graph_reference_nodes_carry_entity_key() -> None:
    prob = Problem.model_validate(_PROBLEM)
    graph = prob.to_kg_graph(attempt_id=7)
    by_id = {n.node_id: n for n in graph.nodes}
    assert by_id["real_basis"].entity_key == "def.real_basis"
    assert by_id["do_it"].entity_key == "proc.do_it"


def test_missing_entity_key_defaults_none() -> None:
    payload = {
        "id": "p2",
        "concept_id": "c",
        "difficulty": "hard",
        "problem_text": "x",
        "reference_solution": [
            {
                "step": 1,
                "entry_type": "equation",
                "id": "e",
                "content": {"symbolic": "a-b"},
            }
        ],
    }
    graph = Problem.model_validate(payload).to_kg_graph(attempt_id=1)
    assert graph.nodes[0].entity_key is None
```

- [ ] Run `pytest apollo/schemas/tests/test_problem_entity_key.py -q` — expect **FAIL** (`ReferenceStep` drops `entity_key`; nodes have `None`).
- [ ] Minimal implementation in `apollo/schemas/problem.py`. Add to `ReferenceStep`:

```python
class ReferenceStep(BaseModel):
    step: int = Field(ge=1)
    entry_type: EntryType
    id: str = Field(min_length=1)
    content: dict[str, Any]
    depends_on: list[str] = Field(default_factory=list)
    # F-struct: canonical entity key authored per step (e.g. "def.real_basis").
    # Optional so pre-seeded / non-layer1 problems validate unchanged; when
    # present it flows onto the reference Node in to_kg_graph.
    entity_key: str | None = None
```

In `to_kg_graph`, pass it into `build_node`:

```python
            node = build_node(
                node_type=step.entry_type,  # type: ignore[arg-type]
                node_id=step.id,
                attempt_id=attempt_id,
                source="reference",
                content=content,
                entity_key=step.entity_key,
            )
```

- [ ] Run `pytest apollo/schemas/tests/test_problem_entity_key.py -q` — expect **PASS**.
- [ ] Run `pytest apollo/schemas/ -q` — expect **PASS** (validators unchanged).
- [ ] Commit: `feat(apollo): keep entity_key through Problem validation onto reference nodes (F-struct 2/11)`

---

## Task 3 — Migration 038: `opposes` column on `apollo_misconceptions`

**Files:**
* Create `database/migrations/038_apollo_misconception_opposes.sql`.
* Create `tests/database/test_apollo_misconception_opposes_migration.py`.

**Interfaces:**
* Produces: `apollo_misconceptions.opposes TEXT NULL`.

Steps:

- [ ] Write failing test `tests/database/test_apollo_misconception_opposes_migration.py` (models the 037 harness — `test_apollo_misconception_observations_migration.py`):

```python
"""Real-Postgres test for apollo_misconceptions.opposes (migration 038).

Applies 019 then 038 verbatim to a fresh DB on the session pgvector container
and asserts the opposes column exists, is nullable, defaults NULL, and accepts a
value — enumerating the column's add/nullable/insert behaviors per the DB test
contract. Mirrors test_apollo_misconception_observations_migration.py.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import asyncpg
import pytest
import pytest_asyncio
from sqlalchemy.engine import make_url

pytestmark = pytest.mark.integration

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "database" / "migrations"
MIGRATED_DB_NAME = "apollo_misconception_opposes_migrations"
MIGRATION_019 = MIGRATIONS_DIR / "019_apollo_misconceptions.sql"
MIGRATION_038 = MIGRATIONS_DIR / "038_apollo_misconception_opposes.sql"

# 019 references apollo_concepts + needs pgvector; stub the concept parent and
# create the extension (the container image ships pgvector).
_STUB_DDL = """
CREATE EXTENSION IF NOT EXISTS vector;
CREATE TABLE apollo_concepts (id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY);
"""


def _plain_dsn(sqlalchemy_url: str, database: str) -> str:
    url = make_url(sqlalchemy_url).set(drivername="postgresql", database=database)
    return url.render_as_string(hide_password=False)


@pytest.fixture(scope="session")
def _migrated_dsn(_pg_url: str):
    base_db = make_url(_pg_url).database
    assert base_db
    admin_dsn = _plain_dsn(_pg_url, base_db)
    migrated_dsn = _plain_dsn(_pg_url, MIGRATED_DB_NAME)

    async def _setup() -> None:
        admin = await asyncpg.connect(admin_dsn)
        try:
            await admin.execute(f'DROP DATABASE IF EXISTS "{MIGRATED_DB_NAME}"')
            await admin.execute(f'CREATE DATABASE "{MIGRATED_DB_NAME}"')
        finally:
            await admin.close()
        conn = await asyncpg.connect(migrated_dsn)
        try:
            await conn.execute(_STUB_DDL)
            await conn.execute(MIGRATION_019.read_text(encoding="utf-8"))
            await conn.execute(MIGRATION_038.read_text(encoding="utf-8"))
        finally:
            await conn.close()

    asyncio.run(_setup())
    yield migrated_dsn


@pytest_asyncio.fixture
async def mig_conn(_migrated_dsn: str):
    conn = await asyncpg.connect(_migrated_dsn)
    tr = conn.transaction()
    await tr.start()
    try:
        yield conn
    finally:
        await tr.rollback()
        await conn.close()


@pytest.mark.asyncio
async def test_opposes_column_exists_and_is_nullable(mig_conn) -> None:
    row = await mig_conn.fetchrow(
        """
        SELECT data_type, is_nullable
        FROM information_schema.columns
        WHERE table_name = 'apollo_misconceptions' AND column_name = 'opposes'
        """
    )
    assert row is not None, "opposes column missing"
    assert row["data_type"] == "text"
    assert row["is_nullable"] == "YES"


@pytest.mark.asyncio
async def test_opposes_defaults_null_and_accepts_value(mig_conn) -> None:
    await mig_conn.execute("INSERT INTO apollo_concepts DEFAULT VALUES")
    cid = await mig_conn.fetchval("SELECT id FROM apollo_concepts LIMIT 1")
    # NULL (default) row.
    await mig_conn.execute(
        "INSERT INTO apollo_misconceptions (concept_id, code, description, probe_question) "
        "VALUES ($1, 'm1', 'd', 'p')",
        cid,
    )
    assert await mig_conn.fetchval(
        "SELECT opposes FROM apollo_misconceptions WHERE code = 'm1'"
    ) is None
    # Explicit value row.
    await mig_conn.execute(
        "INSERT INTO apollo_misconceptions (concept_id, code, description, probe_question, opposes) "
        "VALUES ($1, 'm2', 'd', 'p', 'def.real_basis')",
        cid,
    )
    assert await mig_conn.fetchval(
        "SELECT opposes FROM apollo_misconceptions WHERE code = 'm2'"
    ) == "def.real_basis"
```

- [ ] Run `pytest tests/database/test_apollo_misconception_opposes_migration.py -q` (needs the pgvector container fixture `_pg_url` — the integration lane) — expect **FAIL** (migration file absent).
- [ ] Create `database/migrations/038_apollo_misconception_opposes.sql`:

```sql
-- 038_apollo_misconception_opposes.sql
-- F-struct (structural co-key): add the per-entry `opposes` link to the
-- RUNTIME misconception bank (apollo_misconceptions, migration 019). The link
-- already exists in the on-disk misconceptions.json source and in the emergent
-- store (apollo_misconception_observations.opposes, migration 037) + the
-- apollo_kg_entities opposes_entity_key payload — only the detector's bank
-- lacked it. Nullable: most banks have no opposes; NULL means "no structural
-- scope" and the structural co-key gate path never fires for that entry.
--
-- Numbering: on-disk max was 037; this takes 038.
--
-- DEPLOY-TIME RECONCILIATION: numbered migration; agents apply to LOCAL Docker
-- Postgres only. Test-project rehearsal then prod is a human/CI step. DO NOT
-- auto-apply to any remote Supabase project.

BEGIN;

ALTER TABLE apollo_misconceptions
    ADD COLUMN IF NOT EXISTS opposes TEXT;

COMMENT ON COLUMN apollo_misconceptions.opposes IS
    'F-struct: canonical entity_key of the reference node this misconception '
    'contradicts (e.g. def.real_basis), or NULL. Seeded from misconceptions.json '
    '"opposes". Read by the structural co-key gate path to NAME a misconception '
    'the judge only localized.';

COMMIT;
```

- [ ] Run `pytest tests/database/test_apollo_misconception_opposes_migration.py -q` — expect **PASS**.
- [ ] Commit: `feat(db): migration 038 — opposes column on apollo_misconceptions (F-struct 3/11)`

---

## Task 4 — ORM + loader carry `opposes`

**Files:**
* Modify `apollo/persistence/models.py` (`Misconception` ~L227-251).
* Modify `apollo/overseer/misconception_bank.py` (`MisconceptionEntry` L30-42; `_from_row` L45-68; `match_by_embedding` SQL + row-build L117-171; `upsert_entry` L174-237).
* Create `apollo/overseer/tests/test_misconception_bank_opposes.py`.

**Interfaces:**
* Produces: `MisconceptionEntry.opposes: str | None`; `Misconception.opposes` column; `upsert_entry(..., opposes: str | None = None)`.

Steps:

- [ ] Write failing test `apollo/overseer/tests/test_misconception_bank_opposes.py`:

```python
"""MisconceptionEntry carries opposes; _from_row reads it (F-struct Task 4)."""
from __future__ import annotations

import pytest

from apollo.overseer.misconception_bank import MisconceptionEntry, _from_row
from apollo.persistence.models import Misconception

pytestmark = pytest.mark.unit


def _row(**kw) -> Misconception:
    base = dict(
        id=1, concept_id=2, code="nominal_for_real", description="d",
        confusion_pair_a=None, confusion_pair_b=None, trigger_phrases=[],
        probe_question="p", rt_steps=[],
    )
    base.update(kw)
    m = Misconception()
    for k, v in base.items():
        setattr(m, k, v)
    return m


def test_entry_has_opposes_field() -> None:
    e = MisconceptionEntry(
        id=1, concept_id=2, code="c", description="d", confusion_pair=None,
        trigger_phrases=(), probe_question="p", rt_steps=(),
        opposes="def.real_basis",
    )
    assert e.opposes == "def.real_basis"


def test_from_row_reads_opposes() -> None:
    assert _from_row(_row(opposes="def.real_basis")).opposes == "def.real_basis"


def test_from_row_opposes_none_default() -> None:
    assert _from_row(_row(opposes=None)).opposes is None
```

- [ ] Run `pytest apollo/overseer/tests/test_misconception_bank_opposes.py -q` — expect **FAIL** (`MisconceptionEntry` has no `opposes`; `_from_row` doesn't set it).
- [ ] Implement. In `apollo/persistence/models.py` `Misconception`, add after `rt_steps`:

```python
    # F-struct (migration 038): canonical entity_key of the reference node this
    # misconception opposes (e.g. "def.real_basis"), or NULL. Read by the
    # structural co-key gate path.
    opposes = Column(Text, nullable=True)
```

In `apollo/overseer/misconception_bank.py`, add to `MisconceptionEntry`:

```python
    rt_steps: tuple[str, ...]
    opposes: str | None = None
```

Set it in `_from_row` (in the `MisconceptionEntry(...)` return):

```python
        rt_steps=tuple(rt),
        opposes=row.opposes,
    )
```

Add `m.opposes` to the `match_by_embedding` SELECT column list and the inline
`MisconceptionEntry(...)` build:

```python
            m.rt_steps,
            m.opposes,
```
```python
            rt_steps=tuple(rt),
            opposes=r["opposes"],
        )
```

Extend `upsert_entry` — add the `opposes` param, INSERT column, VALUES bind, and
ON CONFLICT SET:

```python
    trigger_phrases: list[str],
    probe_question: str,
    rt_steps: list[str],
    opposes: str | None = None,
) -> int:
```
INSERT column list: add `opposes` after `rt_steps`; VALUES: add `:opposes`;
ON CONFLICT SET: add `opposes = EXCLUDED.opposes,`; bind dict: add
`"opposes": opposes,`.

- [ ] Run `pytest apollo/overseer/tests/test_misconception_bank_opposes.py -q` — expect **PASS**.
- [ ] Run `pytest apollo/overseer/ -q` — expect **PASS**.
- [ ] Commit: `feat(apollo): thread opposes through the misconception bank loader/ORM (F-struct 4/11)`

---

## Task 5 — Seeder carries `opposes` from `misconceptions.json`

**Files:**
* Modify `apollo/persistence/misconception_bank_seed.py` (`MisconceptionBankSpec` L45-57; `misconception_entry_to_bank_spec` L73-90).
* Modify `scripts/seed_apollo_misconceptions.py` (`_seed_concept` `upsert_entry` call L146-158).
* Modify `apollo/persistence/tests/test_misconception_bank_seed.py` (add cases).

**Interfaces:**
* Consumes: `misconceptions.json` entry `"opposes"`.
* Produces: `MisconceptionBankSpec.opposes: str | None`.

Steps:

- [ ] Add failing tests to `apollo/persistence/tests/test_misconception_bank_seed.py`:

```python
def test_bank_spec_carries_opposes() -> None:
    from apollo.persistence.misconception_bank_seed import (
        misconception_entry_to_bank_spec,
    )
    spec = misconception_entry_to_bank_spec(
        {"key": "misc.nominal_for_real", "description": "d",
         "opposes": "def.real_basis"}
    )
    assert spec.code == "nominal_for_real"
    assert spec.opposes == "def.real_basis"


def test_bank_spec_opposes_defaults_none() -> None:
    from apollo.persistence.misconception_bank_seed import (
        misconception_entry_to_bank_spec,
    )
    spec = misconception_entry_to_bank_spec({"key": "misc.x", "description": "d"})
    assert spec.opposes is None
```

- [ ] Run `pytest apollo/persistence/tests/test_misconception_bank_seed.py -q` — expect **FAIL**.
- [ ] Implement. In `misconception_bank_seed.py`, add to `MisconceptionBankSpec`:

```python
    rt_steps: tuple[str, ...] = field(default_factory=tuple)
    opposes: str | None = None
```

In `misconception_entry_to_bank_spec`, add to the returned spec:

```python
        rt_steps=tuple(entry.get("rt_steps", [])),
        opposes=entry.get("opposes"),
    )
```

In `scripts/seed_apollo_misconceptions.py::_seed_concept`, add to the
`upsert_entry` call:

```python
            rt_steps=list(spec.rt_steps),
            opposes=spec.opposes,
        )
```

- [ ] Run `pytest apollo/persistence/tests/test_misconception_bank_seed.py -q` — expect **PASS**.
- [ ] Commit: `feat(apollo): seed opposes into apollo_misconceptions from misconceptions.json (F-struct 5/11)`

---

## Task 6 — `APOLLO_MISC_STRUCT_COKEY` sub-flag

**Files:**
* Modify `apollo/overseer/misconception_detector/config.py` (add flag env + `struct_cokey_enabled()` near `trace_enabled` L70-79).
* Modify `apollo/overseer/misconception_detector/tests/test_config.py` (add cases; if absent, create it).

**Interfaces:**
* Produces: `STRUCT_COKEY_FLAG_ENV: str`; `struct_cokey_enabled() -> bool`.

Steps:

- [ ] Write failing test (append to the detector config test module):

```python
def test_struct_cokey_flag_default_off(monkeypatch) -> None:
    from apollo.overseer.misconception_detector.config import struct_cokey_enabled
    monkeypatch.delenv("APOLLO_MISC_STRUCT_COKEY", raising=False)
    assert struct_cokey_enabled() is False


@pytest.mark.parametrize("val,expected", [("1", True), ("true", True), ("on", True), ("0", False), ("", False)])
def test_struct_cokey_flag_truthy(monkeypatch, val, expected) -> None:
    from apollo.overseer.misconception_detector.config import struct_cokey_enabled
    monkeypatch.setenv("APOLLO_MISC_STRUCT_COKEY", val)
    assert struct_cokey_enabled() is expected
```

- [ ] Run that test — expect **FAIL** (`struct_cokey_enabled` undefined).
- [ ] Implement in `config.py` (after `trace_enabled`):

```python
# F-struct sub-flag (structural co-key). SEPARATE from FLAG_ENV, default OFF:
# when OFF, gate_findings receives an empty opposes_index and behavior/output is
# byte-identical. When ON (and APOLLO_MISCONCEPTION_DETECTOR is ON), a confident
# wrong/misconception judge verdict at a reference node whose entity_key is
# opposed by a bank entry docks via the existing co-key machinery.
STRUCT_COKEY_FLAG_ENV: str = "APOLLO_MISC_STRUCT_COKEY"


def struct_cokey_enabled() -> bool:
    """True iff APOLLO_MISC_STRUCT_COKEY is truthy (default OFF). Read at call
    time (never cached), same _TRUTHY set as detector_enabled."""
    return os.environ.get(STRUCT_COKEY_FLAG_ENV, "").strip().lower() in _TRUTHY
```

- [ ] Run the test — expect **PASS**.
- [ ] Commit: `feat(apollo): APOLLO_MISC_STRUCT_COKEY sub-flag (F-struct 6/11)`

---

## Task 7 — `build_opposes_index` (pure caller-side resolver)

**Files:**
* Create `apollo/overseer/misconception_detector/opposes_index.py`.
* Create `apollo/overseer/misconception_detector/tests/test_opposes_index.py`.

**Interfaces:**
* Consumes: `reference_graph: KGGraph`, `bank_entries: tuple[MisconceptionEntry, ...]`.
* Produces: `build_opposes_index(reference_graph, bank_entries) -> dict[str, str]` — `{node_id: bank_code}` for every reference node whose `entity_key` is opposed by ≥1 bank entry (tiebreak: lowest `code`).

Steps:

- [ ] Write failing test `apollo/overseer/misconception_detector/tests/test_opposes_index.py`:

```python
"""Pure node_id->bank_code resolver for the structural co-key (F-struct Task 7)."""
from __future__ import annotations

import pytest

from apollo.ontology import KGGraph
from apollo.ontology.nodes import build_node
from apollo.overseer.misconception_bank import MisconceptionEntry
from apollo.overseer.misconception_detector.opposes_index import build_opposes_index

pytestmark = pytest.mark.unit


def _entry(code: str, opposes: str | None) -> MisconceptionEntry:
    return MisconceptionEntry(
        id=1, concept_id=1, code=code, description="d", confusion_pair=None,
        trigger_phrases=(), probe_question="p", rt_steps=(), opposes=opposes,
    )


def _ref_graph() -> KGGraph:
    return KGGraph(
        nodes=[
            build_node(node_type="definition", node_id="real_basis", attempt_id=1,
                       source="reference",
                       content={"concept": "real GDP", "meaning": "m"},
                       entity_key="def.real_basis"),
            build_node(node_type="equation", node_id="growth_rate", attempt_id=1,
                       source="reference", content={"symbolic": "a-b"},
                       entity_key="eq.growth_rate"),
        ],
        edges=[],
    )


def test_maps_node_id_to_opposing_bank_code() -> None:
    idx = build_opposes_index(_ref_graph(), (_entry("nominal_for_real", "def.real_basis"),))
    assert idx == {"real_basis": "nominal_for_real"}


def test_entry_without_opposes_ignored() -> None:
    assert build_opposes_index(_ref_graph(), (_entry("x", None),)) == {}


def test_opposes_no_matching_node_ignored() -> None:
    assert build_opposes_index(_ref_graph(), (_entry("x", "def.absent"),)) == {}


def test_node_without_entity_key_never_matched() -> None:
    g = KGGraph(nodes=[build_node(node_type="equation", node_id="n", attempt_id=1,
                                  source="reference", content={"symbolic": "a"})],
                edges=[])
    assert build_opposes_index(g, (_entry("x", None),)) == {}


def test_multi_opposes_same_node_lowest_code_wins() -> None:
    entries = (_entry("zeta", "def.real_basis"), _entry("alpha", "def.real_basis"))
    assert build_opposes_index(_ref_graph(), entries) == {"real_basis": "alpha"}
```

- [ ] Run `pytest apollo/overseer/misconception_detector/tests/test_opposes_index.py -q` — expect **FAIL** (module absent).
- [ ] Implement `apollo/overseer/misconception_detector/opposes_index.py`:

```python
"""Pure resolver: reference-node node_id -> opposing bank code (F-struct).

Keeps ``gate.py`` pure and node-shape-agnostic. The caller (done.py / the
campaign harness) has both the reference graph and the loaded bank in hand — the
same place ``centrality`` is computed — so it pre-resolves each bank entry's
``opposes`` (an ``entity_key``) to the reference node carrying that key, then
hands the gate a ``node_id``-keyed map matching the gate's own ``concept_key``
keying.

Design decision D3 (multi-opposes): if >1 bank entry opposes the SAME node, the
lexicographically-lowest ``code`` wins (deterministic; the labeled cluster is
1:1, so this only guards nondeterminism). No IO, no LLM, no DB.
"""
from __future__ import annotations

from apollo.ontology import KGGraph
from apollo.overseer.misconception_bank import MisconceptionEntry


def build_opposes_index(
    reference_graph: KGGraph,
    bank_entries: tuple[MisconceptionEntry, ...],
) -> dict[str, str]:
    """Return ``{node_id: bank_code}`` for every reference node whose
    ``entity_key`` is opposed by at least one bank entry. On a tie, the lowest
    ``code`` wins. Nodes without an ``entity_key`` and entries without
    ``opposes`` contribute nothing."""
    key_to_node_id: dict[str, str] = {
        n.entity_key: n.node_id for n in reference_graph.nodes if n.entity_key
    }
    index: dict[str, str] = {}
    for entry in bank_entries:
        if not entry.opposes:
            continue
        node_id = key_to_node_id.get(entry.opposes)
        if node_id is None:
            continue
        existing = index.get(node_id)
        if existing is None or entry.code < existing:
            index[node_id] = entry.code
    return index


__all__ = ["build_opposes_index"]
```

- [ ] Run `pytest apollo/overseer/misconception_detector/tests/test_opposes_index.py -q` — expect **PASS**.
- [ ] Commit: `feat(apollo): build_opposes_index node_id->bank_code resolver (F-struct 7/11)`

---

## Task 8 — Gate structural co-key path

**Files:**
* Modify `apollo/overseer/misconception_detector/gate.py` (`gate_findings` L69-112; `_gate_one_concept` L115-169).
* Create `apollo/overseer/misconception_detector/tests/test_gate_struct_cokey.py`.

**Interfaces:**
* Consumes: `gate_findings(findings, *, opposes_index: dict[str, str] = {}, tau_fire=..., tau_verbalized=..., tau_solo=...)`.
* Produces: for a `wrong`/`misconception` judge finding at node X with `bank_code is None` and `opposes_index[X.concept_key]` present and routed-tau cleared → a docked finding (`verdict="misconception"`, `corroborated=True`, `ceiling_eligible=True`, `bank_code=<code>`, `signature="misc.<code>"`).

Steps:

- [ ] Write failing test `apollo/overseer/misconception_detector/tests/test_gate_struct_cokey.py`:

```python
"""Structural co-key gate path (F-struct Task 8)."""
from __future__ import annotations

import pytest

from apollo.overseer.misconception_detector.gate import gate_findings
from apollo.overseer.misconception_detector.types import ConceptFinding

pytestmark = pytest.mark.unit


def _judge(concept_key: str, verdict: str, conf: float, bank_code=None) -> ConceptFinding:
    return ConceptFinding(
        concept_key=concept_key, verdict=verdict, confidence=conf, severity=0.0,
        evidence_span="nominal is fine", signature=(f"misc.{bank_code}" if bank_code else f"unkeyed:{concept_key}"),
        source="judge", corroborated=False, verdict_token_prob_present=True,
        bank_code=bank_code,
    )


def test_wrong_verdict_with_opposes_docks_keyed() -> None:
    # The miss signature: judge localizes (wrong@~1.0) but named NO code.
    findings = (_judge("real_basis", "wrong", 1.0, bank_code=None),)
    out = gate_findings(findings, opposes_index={"real_basis": "nominal_for_real"})
    assert len(out) == 1
    rep = out[0]
    assert rep.verdict == "misconception"
    assert rep.corroborated is True
    assert rep.ceiling_eligible is True
    assert rep.bank_code == "nominal_for_real"
    assert rep.signature == "misc.nominal_for_real"


def test_clear_verdict_never_structural_docks() -> None:
    # Control safety: a clear verdict never enters the structural branch.
    findings = (_judge("real_basis", "clear", 1.0, bank_code=None),)
    out = gate_findings(findings, opposes_index={"real_basis": "nominal_for_real"})
    assert out == ()


def test_needs_clarification_never_structural_docks() -> None:
    findings = (_judge("real_basis", "needs_clarification", 1.0, bank_code=None),)
    out = gate_findings(findings, opposes_index={"real_basis": "nominal_for_real"})
    assert out == ()


def test_sub_routed_tau_wrong_does_not_structural_dock() -> None:
    findings = (_judge("real_basis", "wrong", 0.50, bank_code=None),)  # < 0.85
    out = gate_findings(findings, opposes_index={"real_basis": "nominal_for_real"})
    assert out == ()


def test_no_opposes_entry_leaves_prior_behavior() -> None:
    # Empty opposes_index (flag OFF) => byte-identical prior behavior: a lone
    # unkeyed wrong@1.0 drops (row 8).
    findings = (_judge("real_basis", "wrong", 1.0, bank_code=None),)
    out = gate_findings(findings, opposes_index={})
    assert out == ()


def test_judge_named_code_takes_existing_path_no_double() -> None:
    # Judge already named the code -> existing lone-solo path, NOT structural.
    # opposes_index present, but bank_code is not None so structural is skipped.
    findings = (_judge("real_basis", "misconception", 1.0, bank_code="nominal_for_real"),)
    out = gate_findings(findings, opposes_index={"real_basis": "nominal_for_real"})
    assert len(out) == 1
    assert out[0].bank_code == "nominal_for_real"
    # Docked once (single representative), no duplication.
```

- [ ] Run `pytest apollo/overseer/misconception_detector/tests/test_gate_struct_cokey.py -q` — expect **FAIL** (`gate_findings` has no `opposes_index` kwarg).
- [ ] Implement in `gate.py`. Add `opposes_index` param to `gate_findings` (default empty) and thread it into `_gate_one_concept`:

```python
def gate_findings(
    findings: tuple[ConceptFinding, ...],
    *,
    opposes_index: dict[str, str] | None = None,
    tau_fire: float = TAU_FIRE,
    tau_verbalized: float = TAU_FIRE_VERBALIZED,
    tau_solo: float = TAU_SOLO_JUDGE,
) -> tuple[ConceptFinding, ...]:
    ...
    opposes = opposes_index or {}
    ...
    for group in anchors.values():
        outcome = _gate_one_concept(
            group,
            bank_by_code=bank_by_code,
            opposes_index=opposes,
            tau_fire=tau_fire,
            tau_verbalized=tau_verbalized,
            tau_solo=tau_solo,
        )
```

In `_gate_one_concept`, add the `opposes_index: dict[str, str]` param and insert
the structural branch AFTER the `corroborating_bank` block and BEFORE the
existing `if best_judge.bank_code is not None:` lone-keyed block — but only for
the `bank_code is None` case. Cleanest placement: replace the tail
(`# No bank corroboration available...` onward) with:

```python
    # No bank corroboration available for this judge's named code.
    if best_judge.bank_code is not None:
        if solo_ok:
            return _docked(best_judge, ceiling_eligible=False)
        if routed_ok:
            return _needs_clarification(best_judge)
        return None

    # F-struct structural co-key: judge LOCALIZED (wrong/misconception at this
    # node) but named NO code; a bank entry opposes this node's entity_key.
    # The GRAPH names it. Control-safe: `clear`/`needs_clarification` never
    # reach here (they are not wrong/misconception verdicts). Gated on
    # routed_ok so a hedged localization can't dock.
    struct_code = opposes_index.get(best_judge.concept_key)
    if (
        struct_code is not None
        and routed_ok
        and best_judge.verdict in ("wrong", "misconception")
    ):
        return _struct_docked(best_judge, bank_code=struct_code)

    # Row 7: lone UNKEYED judge clearing routed tau -> clarify, never docks.
    if routed_ok:
        return _needs_clarification(best_judge)
    # Row 8: lone UNKEYED judge sub-routed-tau -> drop.
    return None
```

Add the `_struct_docked` builder next to `_docked`:

```python
def _struct_docked(finding: ConceptFinding, *, bank_code: str) -> ConceptFinding:
    """Structural co-key dock (F-struct): the judge localized the error to this
    node and the graph named it via a bank entry's `opposes`. Reuses row-3
    co-key semantics — ceiling-eligible + bank-keyed so merge emits the artifact
    misconceptions[] row. Sets bank_code + signature so `merge._is_bank_keyed`
    picks it up."""
    return dataclasses.replace(
        finding,
        verdict="misconception",
        corroborated=True,
        ceiling_eligible=True,
        bank_code=bank_code,
        signature=f"misc.{bank_code}",
    )
```

- [ ] Run `pytest apollo/overseer/misconception_detector/tests/test_gate_struct_cokey.py -q` — expect **PASS**.
- [ ] Run `pytest apollo/overseer/misconception_detector/ -q` — expect **PASS** (existing gate tests unchanged: `opposes_index` defaults empty ⇒ identical old behavior).
- [ ] Commit: `feat(apollo): structural co-key gate path (F-struct 8/11)`

---

## Task 9 — Trace records the structural match

**Files:**
* Modify `apollo/overseer/misconception_detector/trace.py` (`_gate_row_label` L53-87; `build_node_traces` row-build L153-205; add param for `opposes_index`).
* Modify the trace test module (add a structural case).

**Interfaces:**
* Consumes: `opposes_index: dict[str, str]` (threaded through `trace_attempt` / `build_node_traces`).
* Produces: each trace row gains `struct_opposes_code: str | None` + `docked_via: "judge_named" | "struct_opposes" | "none"`; `gate_row` can be `row3s_struct_cokey_dock`.

Steps:

- [ ] Add a failing test asserting a `wrong`+opposes node traces `gate_row == "row3s_struct_cokey_dock"`, `docked_via == "struct_opposes"`, `struct_opposes_code == "nominal_for_real"`, and a control node traces `docked_via == "none"`. (Mirror the existing trace test's construction of a `reference_graph` + `DetectionResult` + `gated` tuple, adding `entity_key` on the node and passing `opposes_index={"real_basis": "nominal_for_real"}`.)
- [ ] Run it — expect **FAIL**.
- [ ] Implement: thread `opposes_index: dict[str, str] | None = None` through `trace_attempt` → `build_node_traces`. In the per-node loop, compute:

```python
        struct_code = (opposes_index or {}).get(key)
        gate_rep = gated_by_key.get(key)
        if gate_rep is not None and gate_rep.verdict == "misconception":
            if corroborating_bank is not None or (best_judge is not None and best_judge.bank_code is not None):
                docked_via = "judge_named"
            elif struct_code is not None:
                docked_via = "struct_opposes"
            else:
                docked_via = "judge_named"  # sympy/other keyed dock
        else:
            docked_via = "none"
```

Add `struct_opposes_code` + `docked_via` to the emitted row dict. Extend
`_gate_row_label` to return `row3s_struct_cokey_dock` when `gated_verdict ==
"misconception"`, `best_judge` is unkeyed (`bank_code is None`), `corroborating_bank
is None`, and `struct_code is not None` (add `struct_code` as a param).

- [ ] Run the test — expect **PASS**; run `pytest apollo/overseer/misconception_detector/ -q` — expect **PASS** (trace is instrumentation; existing rows gain two keys but the default `opposes_index=None` keeps `struct_opposes_code=None`, `docked_via` correct).
- [ ] Commit: `feat(apollo): trace the structural co-key match + dock path (F-struct 9/11)`

---

## Task 10 — Wire `opposes_index` in `done.py`; drift doc

**Files:**
* Modify `apollo/handlers/done.py` (detector block L503-557).
* Modify `docs/architecture/apollo.md` (misconception-detector section; frontmatter `last_verified`).
* Create `apollo/handlers/tests/test_done_struct_cokey.py` (or extend an existing done detector test).

**Interfaces:**
* Consumes: `struct_cokey_enabled()`, `build_opposes_index`, loaded bank entries.
* Produces: `gate_findings(detection.per_concept, opposes_index=...)` with the index built only when the sub-flag is ON; else empty (byte-identical).

Steps:

- [ ] Write a failing test that, with `APOLLO_MISCONCEPTION_DETECTOR=1` +
  `APOLLO_MISC_STRUCT_COKEY=1`, a stub judge returning `wrong`/no-code at the
  `real_basis` node + a bank entry with `opposes="def.real_basis"` produces a
  docked `nominal_for_real` in the artifact's `misconceptions[]`; and with the
  sub-flag OFF the same inputs dock nothing (byte-identical). (Use the existing
  done detector test harness / fixtures; assert on `detection_outcome.misconceptions`.)
- [ ] Run it — expect **FAIL**.
- [ ] Implement in `done.py`. Inside the `if detector_enabled():` block, after
  `detection = await detect_misconceptions(...)` and before `gated =
  gate_findings(...)`:

```python
            opposes_index: dict[str, str] = {}
            if struct_cokey_enabled():
                from apollo.overseer.misconception_detector.opposes_index import (
                    build_opposes_index,
                )
                bank_entries = await _load_bank_entries(db, concept_id=sess.concept_id)
                opposes_index = build_opposes_index(reference_graph, bank_entries)
            gated = gate_findings(detection.per_concept, opposes_index=opposes_index)
```

Use the module's existing bank-load helper (`load_for_concept` via a small
soft-failing wrapper mirroring `detector._load_bank`, or reuse it directly) —
name it `_load_bank_entries` and soft-fail to `()`. Import
`struct_cokey_enabled` from `config`. Pass `opposes_index=opposes_index` into the
`trace_attempt(...)` call too (Task 9 param).

- [ ] Update `docs/architecture/apollo.md`: in the misconception-detector
  section document the structural co-key path (judge localizes / graph names via
  `opposes`), the new `entity_key` node field, the `opposes` bank column
  (migration 038), the `APOLLO_MISC_STRUCT_COKEY` sub-flag, and the
  `build_opposes_index` seam. Bump frontmatter `last_verified: 2026-07-09`.
- [ ] Run the test — expect **PASS**; run `pytest apollo/handlers/ -q` — expect **PASS**.
- [ ] Commit: `feat(apollo): wire structural co-key into done.py + reconcile apollo.md (F-struct 10/11)`

---

## Task 11 — Live re-validation over the labeled 20-set

**Files:**
* Modify `campaign/validate_misconception_detector.py` (build + pass `opposes_index` L262; extend `trace_attempt` call L283-293).

**Interfaces:**
* Consumes: the reconstructed `reference_graph` + loaded bank per attempt.
* Produces: with `APOLLO_MISC_TRACE=1 APOLLO_MISC_STRUCT_COKEY=1`, attempts 88/95/112 dock (`misconceptions_found` includes `misc.nominal_for_real`) and control FP stays 0/4.

Steps:

- [ ] Modify the harness: in `_run`, after `detection = await
  detect_misconceptions(...)`, build the index (import
  `build_opposes_index` + `struct_cokey_enabled`; load the bank via
  `load_for_concept(db, concept_id=sess.concept_id)`), then
  `gated = gate_findings(detection.per_concept, opposes_index=opposes_index)`.
  Pass `opposes_index=opposes_index` into the `trace_attempt(...)` call.
- [ ] Run (LOCAL Docker stack + Neo4j + OpenAI key present):

```bash
APOLLO_MISCONCEPTION_DETECTOR=1 APOLLO_MISC_STRUCT_COKEY=1 APOLLO_MISC_TRACE=1 \
  python -m campaign.validate_misconception_detector
```

  **Expected SUMMARY:** `false-Strong on misconception-class: baseline=N ->
  after-penalty=M` with **88, 95, 112 no longer in the false-Strong set**
  (their `misconceptions_found` contains `misc.nominal_for_real`), and
  `strong/partial CONTROL attempts: ...; false positives ...: 0`. Inspect
  `campaign/out/misconception_trace.jsonl`: the `real_basis` node rows for
  88/95/112 show `gate_row: "row3s_struct_cokey_dock"`, `docked_via:
  "struct_opposes"`, `gate_decision: "dock"`; control rows show `docked_via:
  "none"`.
- [ ] If the judge/DB is unavailable, the harness prints
  `mode=deterministic_only` loudly — re-run with the real judge + a seeded local
  bank (run `python -m scripts.seed_apollo_misconceptions --subject-slug
  macroeconomics --concept-slug nominal_vs_real_gdp` against the local DB first
  so `opposes` is populated). Record the SUMMARY in the PR description.
- [ ] Run the full gate:

```bash
pytest apollo/ -q
pytest --cov --cov-report=xml -q
diff-cover coverage.xml --compare-branch=origin/staging --fail-under=95
```

  Expect: `apollo/` green, `diff-cover` reports **≥95%** patch coverage.
- [ ] Commit: `feat(apollo): re-validate structural co-key over labeled 20-set (F-struct 11/11)`

---

## Self-Review

**Spec coverage check** — every spec §2 part has a task:
* §2.1 node plumbing → Tasks 1 (node) + 2 (schema). ✔
* §2.2 bank plumbing → Tasks 3 (migration 038) + 4 (ORM/loader) + 5 (seeder). ✔
* §2.3 gate structural path → Task 8 (with the pure `opposes_index` seam in Task 7 per D5). ✔
* §2.4 trace → Task 9. ✔
* §2.5 flag → Task 6 (defined), Tasks 8/10/11 (consumed). ✔
* §5 re-validation → Task 11 (88/95/112 dock; control FP 0/4). ✔
* Design decisions D1-D7 → D1 Task 1, D2 Task 8 (`_judge_clears_tau` reuse), D3 Task 7 (lowest-code tiebreak), D4 Task 8 (`bank_code is None` guard), D5 Task 7 (`opposes_index` seam), D6 Task 3 (038), D7 Task 6 (flag name). ✔

**Placeholder scan** — every code step carries real code (test bodies + impl
snippets + exact SQL); every run step carries the exact command + expected
PASS/FAIL/summary. No `TODO`, `...impl...`, or `<fill in>` remains in an
implementation body. Trace-impl (Task 9) gives the concrete `docked_via`
computation + label rule rather than prose. ✔

**Type-consistency check** across tasks:
* `entity_key: str | None` — identical on `_NodeBase`, `build_node` kwarg,
  `ReferenceStep`. ✔
* `opposes: str | None` — identical on `MisconceptionEntry`,
  `MisconceptionBankSpec`, ORM `Misconception.opposes` (Text nullable),
  `upsert_entry` kwarg, migration `opposes TEXT`. ✔
* `opposes_index: dict[str, str]` (`{node_id: bank_code}`) — produced by
  `build_opposes_index` (Task 7), consumed by `gate_findings` (Task 8), `done.py`
  (Task 10), `trace` (Task 9), harness (Task 11). Same key/value semantics
  everywhere. ✔
* Docked representative: `verdict="misconception"`, `corroborated=True`,
  `ceiling_eligible=True`, `bank_code=<code>`, `signature="misc.<code>"` — the
  exact shape `merge._is_bank_keyed` + `_keyed_row` + `_any_central` already
  consume, so no merge/apply change is needed. ✔

**Flag-OFF invariant** — with `APOLLO_MISC_STRUCT_COKEY` OFF, `done.py` passes an
empty `opposes_index`, `gate_findings`'s new branch never fires (empty dict
lookup), the migration column is unread (NULL), and trace `docked_via` defaults
correctly — behavior + output byte-identical. ✔ (No fix needed.)
