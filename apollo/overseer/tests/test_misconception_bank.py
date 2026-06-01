"""P2.2 — misconception_bank loader tests.

Uses in-memory SQLite. The pgvector-only path (match_by_embedding) is
NOT exercised here — its SQL uses Postgres-specific operators
(`::halfvec`, `<=>`) that SQLite can't execute. That path is covered
by an integration test against the test Supabase project ref (separate,
out of unit suite).
"""
from __future__ import annotations

import json

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from apollo.overseer.misconception_bank import (
    MisconceptionEntry,
    load_for_concept,
)
from apollo.persistence.models import Concept, Misconception, Subject
from database.models import Base


@pytest_asyncio.fixture
async def db_with_seed():
    """In-memory SQLite seeded with one subject, one concept, two
    misconceptions for that concept, and one misconception for a second
    concept (so we can verify scoping)."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    tables = [
        Subject.__table__,
        Concept.__table__,
        Misconception.__table__,
    ]
    async with engine.begin() as conn:
        await conn.run_sync(
            lambda sync_conn: Base.metadata.create_all(sync_conn, tables=tables)
        )
    Session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

    async with Session() as s:
        subj = Subject(slug="generic_subject", display_name="Generic Subject")
        s.add(subj)
        await s.flush()

        concept_a = Concept(
            subject_id=subj.id,
            slug="concept_a",
            display_name="Concept A",
        )
        concept_b = Concept(
            subject_id=subj.id,
            slug="concept_b",
            display_name="Concept B",
        )
        s.add_all([concept_a, concept_b])
        await s.flush()

        s.add_all([
            Misconception(
                concept_id=concept_a.id,
                code="m_a_1",
                description="A common confusion in concept A",
                confusion_pair_a="alpha",
                confusion_pair_b="beta",
                trigger_phrases=["alpha equals beta", "alpha is beta"],
                probe_question="Hmm, are alpha and beta really the same?",
                rt_steps=["What if alpha doubled?", "Does beta scale with it?"],
            ),
            Misconception(
                concept_id=concept_a.id,
                code="m_a_2",
                description="Another concept-A confusion",
                confusion_pair_a=None,
                confusion_pair_b=None,
                trigger_phrases=[],
                probe_question="I'm not sure about this one — can you explain?",
                rt_steps=[],
            ),
            Misconception(
                concept_id=concept_b.id,
                code="m_b_1",
                description="Concept B has its own bag",
                confusion_pair_a=None,
                confusion_pair_b=None,
                trigger_phrases=["unrelated phrase"],
                probe_question="Hmm — that's different from what I thought.",
                rt_steps=[],
            ),
        ])
        await s.commit()

        yield s, concept_a.id, concept_b.id

    await engine.dispose()


@pytest.mark.asyncio
async def test_load_for_concept_returns_only_that_concept(db_with_seed):
    db, concept_a_id, concept_b_id = db_with_seed

    a_entries = await load_for_concept(db, concept_id=concept_a_id)
    assert len(a_entries) == 2
    assert {e.code for e in a_entries} == {"m_a_1", "m_a_2"}

    b_entries = await load_for_concept(db, concept_id=concept_b_id)
    assert len(b_entries) == 1
    assert b_entries[0].code == "m_b_1"


@pytest.mark.asyncio
async def test_load_for_concept_returns_empty_for_unknown_concept(db_with_seed):
    db, *_ = db_with_seed
    out = await load_for_concept(db, concept_id=99999)
    assert out == []


@pytest.mark.asyncio
async def test_entry_shape_is_frozen_and_typed(db_with_seed):
    db, concept_a_id, _ = db_with_seed
    entries = await load_for_concept(db, concept_id=concept_a_id)
    e = next(x for x in entries if x.code == "m_a_1")

    assert isinstance(e, MisconceptionEntry)
    # frozen — attempting to mutate fails
    with pytest.raises(Exception):
        e.code = "mutated"  # type: ignore[misc]

    assert e.confusion_pair == ("alpha", "beta")
    assert isinstance(e.trigger_phrases, tuple)
    assert "alpha equals beta" in e.trigger_phrases
    assert isinstance(e.rt_steps, tuple)
    assert e.rt_steps[0] == "What if alpha doubled?"


@pytest.mark.asyncio
async def test_entry_with_no_confusion_pair_returns_none(db_with_seed):
    db, concept_a_id, _ = db_with_seed
    entries = await load_for_concept(db, concept_id=concept_a_id)
    e = next(x for x in entries if x.code == "m_a_2")

    assert e.confusion_pair is None
    assert e.trigger_phrases == ()
    assert e.rt_steps == ()


def test_no_subject_or_concept_slug_in_function_signature():
    """Subject-agnosticism contract: the loader's only concept signal is
    the FK integer. No string-based class/subject identifiers."""
    import inspect

    from apollo.overseer.misconception_bank import (
        load_for_concept,
        match_by_embedding,
        upsert_entry,
    )

    for fn in (load_for_concept, match_by_embedding, upsert_entry):
        sig = inspect.signature(fn)
        param_names = set(sig.parameters)
        # No subject_id / concept_slug / cluster_id parameter names.
        forbidden = {"subject_id", "subject_slug", "concept_slug", "cluster_id"}
        leaks = forbidden & param_names
        assert not leaks, (
            f"{fn.__name__} parameter list leaks subject-coupling: {leaks}"
        )
