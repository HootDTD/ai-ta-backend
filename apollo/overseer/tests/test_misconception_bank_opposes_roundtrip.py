"""Real-Postgres round-trip for `opposes` through the misconception bank
loader (F-struct Task 4).

The unit tests in ``test_misconception_bank_opposes.py`` cover
``MisconceptionEntry`` construction and ``_from_row`` against a plain ORM
object — but two changed lines only run against real Postgres/pgvector and
would otherwise be uncovered by the patch-coverage gate: the
``opposes=r["opposes"]`` row-build inside ``match_by_embedding``'s raw-SQL
path, and the ``opposes`` bind in ``upsert_entry``'s SQL. This module uses the
``db_session`` real-pgvector Testcontainers fixture (re-exported in
``apollo/conftest.py``, same session-scoped container as
``tests/database/test_apollo_misconception_opposes_migration.py``'s
migration-038 test) to upsert a misconception with `opposes` set and read it
back through both `load_for_concept` (exercises `_from_row`) and
`match_by_embedding` (exercises the raw-SQL row-build), asserting the value
round-trips through the DB. Docker-skips cleanly if the daemon is down.

``description_embedding`` is deliberately NOT part of the `Misconception` ORM
model (see its docstring in ``apollo/persistence/models.py`` — the runtime
uses raw SQL for that column so pgvector's SQLAlchemy adapter isn't a hard
dep), so ``Base.metadata.create_all`` — what the shared session-scoped
``_pg_url`` fixture uses to build the schema — never creates it, nor the
`UNIQUE (concept_id, code)` constraint from migration 019 that `upsert_entry`'s
`ON CONFLICT` targets. The `_patch_embedding_column` fixture below applies
both directly (idempotent `ADD COLUMN IF NOT EXISTS` / `CREATE INDEX
IF NOT EXISTS` / a guarded constraint add) so `upsert_entry` and
`match_by_embedding`'s real pgvector query path are exercised here too.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from apollo.overseer.misconception_bank import (
    load_for_concept,
    match_by_embedding,
    upsert_entry,
)
from apollo.persistence.models import Concept, Subject
from database.models import SearchSpace

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
async def _patch_embedding_column(db_session):
    """Add the raw-SQL-only `description_embedding` column (migration 019)
    on top of the ORM-derived schema, once per test's transaction. Idempotent
    so it's safe to run in every test in this module."""
    await db_session.execute(
        text(
            "ALTER TABLE apollo_misconceptions ADD COLUMN IF NOT EXISTS "
            "description_embedding vector(3072)"
        )
    )
    await db_session.execute(
        text(
            "CREATE INDEX IF NOT EXISTS apollo_misconceptions_embedding_hnsw_idx "
            "ON apollo_misconceptions "
            "USING hnsw ((description_embedding::halfvec(3072)) halfvec_cosine_ops)"
        )
    )
    await db_session.execute(
        text(
            "DO $$ BEGIN "
            "ALTER TABLE apollo_misconceptions "
            "ADD CONSTRAINT apollo_misconceptions_concept_id_code_key "
            "UNIQUE (concept_id, code); "
            "EXCEPTION WHEN duplicate_table THEN NULL; END $$"
        )
    )
    # created_at/updated_at have a Python-side `default=` on the ORM column
    # (applied only when SQLAlchemy builds the INSERT), not a server default —
    # upsert_entry's raw-SQL INSERT never sets them, so migration 019's
    # `DEFAULT now()` is needed here too.
    await db_session.execute(
        text("ALTER TABLE apollo_misconceptions ALTER COLUMN created_at SET DEFAULT now()")
    )
    await db_session.execute(
        text("ALTER TABLE apollo_misconceptions ALTER COLUMN updated_at SET DEFAULT now()")
    )


async def _seed_concept(db, *, slug: str) -> int:
    space = SearchSpace(name=f"Course {slug}", slug=slug, subject_name="Economics")
    db.add(space)
    await db.flush()
    subject = Subject(slug=f"subj-{slug}", display_name="Subject", search_space_id=space.id)
    db.add(subject)
    await db.flush()
    concept = Concept(subject_id=subject.id, slug=f"concept-{slug}", display_name="Concept")
    db.add(concept)
    await db.flush()
    return int(concept.id)


async def test_opposes_round_trips_through_upsert_and_load_for_concept(db_session) -> None:
    concept_id = await _seed_concept(db_session, slug="opposes-load")

    row_id = await upsert_entry(
        db_session,
        concept_id=concept_id,
        code="nominal_for_real",
        description="Confuses nominal GDP with real GDP",
        description_embedding=None,
        confusion_pair_a=None,
        confusion_pair_b=None,
        trigger_phrases=["nominal is real"],
        probe_question="Does inflation matter here?",
        rt_steps=["What happens if prices rise but output doesn't?"],
        opposes="def.real_basis",
    )
    await db_session.commit()
    assert row_id > 0

    entries = await load_for_concept(db_session, concept_id=concept_id)
    assert len(entries) == 1
    assert entries[0].opposes == "def.real_basis"


async def test_opposes_round_trips_through_upsert_and_match_by_embedding(db_session) -> None:
    concept_id = await _seed_concept(db_session, slug="opposes-match")

    embedding = [0.0] * 3072
    embedding[0] = 1.0

    await upsert_entry(
        db_session,
        concept_id=concept_id,
        code="pressure_velocity_same_direction",
        description="Believes pressure and velocity increase together",
        description_embedding=embedding,
        confusion_pair_a=None,
        confusion_pair_b=None,
        trigger_phrases=[],
        probe_question="What happens to pressure as velocity rises?",
        rt_steps=[],
        opposes="law.bernoulli_conservation",
    )
    await db_session.commit()

    matches = await match_by_embedding(
        db_session, concept_id=concept_id, query_embedding=embedding, k=3
    )
    assert len(matches) == 1
    entry, similarity = matches[0]
    assert entry.opposes == "law.bernoulli_conservation"
    assert similarity == pytest.approx(1.0, abs=1e-4)


async def test_opposes_defaults_to_none_when_not_provided(db_session) -> None:
    concept_id = await _seed_concept(db_session, slug="opposes-default")

    await upsert_entry(
        db_session,
        concept_id=concept_id,
        code="no_opposes_link",
        description="A misconception authored without a structural co-key",
        description_embedding=None,
        confusion_pair_a=None,
        confusion_pair_b=None,
        trigger_phrases=[],
        probe_question="p",
        rt_steps=[],
    )
    await db_session.commit()

    entries = await load_for_concept(db_session, concept_id=concept_id)
    assert len(entries) == 1
    assert entries[0].opposes is None
