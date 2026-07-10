"""Materialization tests (2026-07-10 emergent misconception map plan, Wave 3
T7, spec §5.5 D4, Q3).

``materialize_if_promotable`` is the eager τ_project → :Canon opposes-entity
step invoked from BOTH capture seams' own success/failure domain (Q3). It:

  1. Recomputes the signature's trust via the existing derived-on-read
     ``load_class_misconceptions`` (REUSE — no reimplemented trust math).
  2. No-ops below ``TAU_PROJECT``.
  3. Upserts an ``apollo_kg_entities kind='misconception'`` row via the
     existing ``tag_mint_persist.upsert_entity`` (REUSE).
  4. Links ``opposes`` via the existing ``tag_mint_persist.link_opposes``
     (REUSE), resolving the opposed reference entity's id by
     ``canonical_key == opposes_entity_key`` in the same concept. An absent
     opposed reference entity is logged + the link step is skipped, but the
     misconception entity is still materialized.
  5. Projects to ``:Canon`` via the existing ``canon_projection.project_canon``
     (REUSE, idempotent MERGE). ``neo=None`` skips step 5 with a logged note.

In-memory SQLite for the Postgres side (mirrors ``test_store.py`` /
``test_capture.py`` — SQLite does not enforce FKs, matching the store test
convention); a real local-Testcontainers Neo4j (``tc_neo4j``, imported from
``apollo/knowledge_graph/tests/conftest.py``) for the genuine idempotent
:Canon MERGE proof.
"""

from __future__ import annotations

import logging

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from apollo.emergent import materialize
from apollo.persistence.models import Concept, KGEntity, MisconceptionObservation, Subject
from database.models import Base

# `tc_neo4j` (the real-Neo4j Testcontainers fixture used by the :Canon seeder
# tests) is re-exported for this directory via this dir's own conftest.py --
# mirroring apollo/conftest.py's own re-export of tests.conftest's `_pg_url`
# / `db_session` (conftest fixtures are only visible down their own directory
# tree, so a directory that wants a sibling tree's fixture re-imports it).

_SPACE = 1
_CONCEPT = 7
_U1 = "a0000000-0000-4000-8000-000000000001"
_U2 = "b0000000-0000-4000-8000-000000000002"
_U3 = "c0000000-0000-4000-8000-000000000003"


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(
            lambda sc: Base.metadata.create_all(
                sc,
                tables=[
                    MisconceptionObservation.__table__,
                    KGEntity.__table__,
                    Concept.__table__,
                    Subject.__table__,
                ],
            )
        )
    Session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with Session() as s:
        yield s
    await engine.dispose()


async def _seed_subject_concept(db: AsyncSession) -> None:
    """Minimal Subject+Concept scaffold so load_entity_specs' JOIN resolves a
    search_space_id (SQLite does not enforce the FK, but the JOIN still needs
    matching rows)."""
    db.add(Subject(id=1, slug="econ", display_name="Econ", search_space_id=_SPACE))
    db.add(
        Concept(
            id=_CONCEPT,
            subject_id=1,
            slug="gdp",
            display_name="GDP",
        )
    )
    await db.flush()


async def _seed_observations(
    db: AsyncSession,
    *,
    signature: str,
    opposes: str,
    students: tuple[str, ...],
    confidence: float = 1.0,
) -> None:
    for i, u in enumerate(students):
        db.add(
            MisconceptionObservation(
                search_space_id=_SPACE,
                concept_id=_CONCEPT,
                signature=signature,
                user_id=u,
                attempt_id=100 + i,
                confidence=confidence,
                opposes=opposes,
                evidence_span=f"span-{i}",
                source="detector_unkeyed",
            )
        )
    await db.flush()


async def _seed_reference_entity(db: AsyncSession, *, canonical_key: str) -> int:
    row = KGEntity(
        concept_id=_CONCEPT,
        canonical_key=canonical_key,
        kind="definition",
        display_name=canonical_key,
        payload={},
        aliases=[],
    )
    db.add(row)
    await db.flush()
    return int(row.id)


async def _misconception_entities(db: AsyncSession) -> list[KGEntity]:
    rows = (
        (await db.execute(select(KGEntity).where(KGEntity.kind == "misconception")))
        .scalars()
        .all()
    )
    return list(rows)


# --------------------------------------------------------------------------- #
# concept_id is None -> no-op (mirrors aggregate_signatures' own NULL-concept
# no-op contract)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_concept_id_none_is_a_no_op(db):
    await _seed_subject_concept(db)
    await _seed_reference_entity(db, canonical_key="def.real_basis")
    await _seed_observations(
        db,
        signature="emergent.def.real_basis",
        opposes="def.real_basis",
        students=(_U1, _U2, _U3),
    )
    await db.commit()

    await materialize.materialize_if_promotable(
        db,
        None,
        search_space_id=_SPACE,
        concept_id=None,
        signature="emergent.def.real_basis",
        opposes_entity_key="def.real_basis",
    )

    assert await _misconception_entities(db) == []


# --------------------------------------------------------------------------- #
# Below tau_project -> no-op
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_below_tau_project_no_op_no_entity_minted(db):
    await _seed_subject_concept(db)
    await _seed_reference_entity(db, canonical_key="def.real_basis")
    # Single student -> trust caps at ~0.33 * confidence, well under
    # TAU_PROJECT=0.2 only if confidence is low; use a low confidence so a
    # single observation stays under tau_project regardless of K.
    await _seed_observations(
        db,
        signature="emergent.def.real_basis",
        opposes="def.real_basis",
        students=(_U1,),
        confidence=0.1,
    )
    await db.commit()

    await materialize.materialize_if_promotable(
        db,
        None,
        search_space_id=_SPACE,
        concept_id=_CONCEPT,
        signature="emergent.def.real_basis",
        opposes_entity_key="def.real_basis",
    )

    assert await _misconception_entities(db) == []


# --------------------------------------------------------------------------- #
# At/above tau_project -> mints entity + opposes link + idempotent :Canon MERGE
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_at_tau_project_mints_entity_with_opposes_link(db):
    await _seed_subject_concept(db)
    ref_id = await _seed_reference_entity(db, canonical_key="def.real_basis")
    # 3 distinct students at confidence 1.0 -> trust = 1.0 -> comfortably
    # above TAU_PROJECT (0.2).
    await _seed_observations(
        db,
        signature="emergent.def.real_basis",
        opposes="def.real_basis",
        students=(_U1, _U2, _U3),
    )
    await db.commit()

    await materialize.materialize_if_promotable(
        db,
        None,
        search_space_id=_SPACE,
        concept_id=_CONCEPT,
        signature="emergent.def.real_basis",
        opposes_entity_key="def.real_basis",
    )
    await db.commit()

    minted = await _misconception_entities(db)
    assert len(minted) == 1
    entity = minted[0]
    assert entity.canonical_key == "emergent.def.real_basis"
    assert entity.kind == "misconception"
    assert entity.payload["opposes_entity_key"] == "def.real_basis"
    assert entity.payload["opposes_entity_id"] == ref_id


@pytest.mark.asyncio
async def test_recrossing_is_idempotent_one_entity_row(db):
    await _seed_subject_concept(db)
    await _seed_reference_entity(db, canonical_key="def.real_basis")
    await _seed_observations(
        db,
        signature="emergent.def.real_basis",
        opposes="def.real_basis",
        students=(_U1, _U2, _U3),
    )
    await db.commit()

    kwargs = dict(
        search_space_id=_SPACE,
        concept_id=_CONCEPT,
        signature="emergent.def.real_basis",
        opposes_entity_key="def.real_basis",
    )
    await materialize.materialize_if_promotable(db, None, **kwargs)
    await db.commit()
    await materialize.materialize_if_promotable(db, None, **kwargs)
    await db.commit()

    minted = await _misconception_entities(db)
    assert len(minted) == 1  # upsert_entity keys on (concept_id, canonical_key)


# --------------------------------------------------------------------------- #
# Opposed reference entity absent -> log + skip link, still materialize
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_opposed_entity_absent_skips_link_still_materializes(db, caplog):
    await _seed_subject_concept(db)
    # NOTE: no reference entity seeded for "def.missing".
    await _seed_observations(
        db,
        signature="emergent.def.missing",
        opposes="def.missing",
        students=(_U1, _U2, _U3),
    )
    await db.commit()

    with caplog.at_level(logging.WARNING):
        await materialize.materialize_if_promotable(
            db,
            None,
            search_space_id=_SPACE,
            concept_id=_CONCEPT,
            signature="emergent.def.missing",
            opposes_entity_key="def.missing",
        )
    await db.commit()

    minted = await _misconception_entities(db)
    assert len(minted) == 1
    entity = minted[0]
    assert entity.canonical_key == "emergent.def.missing"
    assert entity.payload.get("opposes_entity_id") is None
    assert "opposes_entity_missing" in caplog.text


# --------------------------------------------------------------------------- #
# neo=None skips projection without error
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_neo_none_skips_projection_without_error(db, caplog):
    await _seed_subject_concept(db)
    await _seed_reference_entity(db, canonical_key="def.real_basis")
    await _seed_observations(
        db,
        signature="emergent.def.real_basis",
        opposes="def.real_basis",
        students=(_U1, _U2, _U3),
    )
    await db.commit()

    with caplog.at_level(logging.INFO):
        # Must not raise even though neo is None.
        await materialize.materialize_if_promotable(
            db,
            None,
            search_space_id=_SPACE,
            concept_id=_CONCEPT,
            signature="emergent.def.real_basis",
            opposes_entity_key="def.real_basis",
        )
    assert "canon_projection_skipped" in caplog.text


# --------------------------------------------------------------------------- #
# Own-failure-domain: project_canon raises -> observation stays durable,
# exception swallowed+logged, grade untouched (caller-visible: no raise).
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_project_canon_failure_is_swallowed_entity_still_committed(db, caplog, monkeypatch):
    await _seed_subject_concept(db)
    await _seed_reference_entity(db, canonical_key="def.real_basis")
    await _seed_observations(
        db,
        signature="emergent.def.real_basis",
        opposes="def.real_basis",
        students=(_U1, _U2, _U3),
    )
    await db.commit()

    async def _boom(*_a, **_kw):
        raise RuntimeError("neo4j unreachable")

    monkeypatch.setattr(materialize, "project_canon", _boom)

    class _AnyNeo:
        pass

    with caplog.at_level(logging.WARNING):
        # Must not raise -- materialization failure is its own domain.
        await materialize.materialize_if_promotable(
            db,
            _AnyNeo(),
            search_space_id=_SPACE,
            concept_id=_CONCEPT,
            signature="emergent.def.real_basis",
            opposes_entity_key="def.real_basis",
        )
    await db.commit()

    minted = await _misconception_entities(db)
    assert len(minted) == 1  # entity + link already flushed before projection ran
    assert "canon_projection_failed" in caplog.text


# --------------------------------------------------------------------------- #
# Real-Neo4j container: idempotent :Canon MERGE (second call -> same node
# count) — the genuine graph-write proof the SQLite-only tests above cannot
# give.
# --------------------------------------------------------------------------- #


@pytest.mark.integration
@pytest.mark.asyncio
async def test_canon_projection_idempotent_second_call_same_node_count(db, tc_neo4j):
    await _seed_subject_concept(db)
    await _seed_reference_entity(db, canonical_key="def.real_basis")
    await _seed_observations(
        db,
        signature="emergent.def.real_basis",
        opposes="def.real_basis",
        students=(_U1, _U2, _U3),
    )
    await db.commit()

    kwargs = dict(
        search_space_id=_SPACE,
        concept_id=_CONCEPT,
        signature="emergent.def.real_basis",
        opposes_entity_key="def.real_basis",
    )

    async def _count_canon() -> int:
        async with tc_neo4j.session() as s:
            rec = await (await s.run("MATCH (c:Canon) RETURN count(c) AS n")).single()
            return int(rec["n"])

    await materialize.materialize_if_promotable(db, tc_neo4j, **kwargs)
    await db.commit()
    first_count = await _count_canon()

    # Re-crossing (e.g. another observation pushes trust further) re-projects
    # harmlessly -- MERGE on the same surrogate id, no duplicate node.
    await materialize.materialize_if_promotable(db, tc_neo4j, **kwargs)
    await db.commit()
    second_count = await _count_canon()

    # project_canon projects the WHOLE concept scope (idempotent, full-scope
    # by design -- canon_projection.py's own contract), so both the seeded
    # reference entity and the newly-minted misconception entity land as
    # :Canon nodes: 2, not 1. The idempotency proof is first_count ==
    # second_count (re-crossing MERGEs the same 2 nodes, no duplicates).
    assert first_count == 2
    assert second_count == 2

    async with tc_neo4j.session() as s:
        rec = await (
            await s.run("MATCH (c:Canon {canonical_key: $k}) RETURN c AS c", k="emergent.def.real_basis")
        ).single()
        node = dict(rec["c"])
    assert node["kind"] == "misconception"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_canon_projection_does_not_disturb_existing_reference_nodes(db, tc_neo4j):
    """Quality bar: projecting the new misconception node must not touch the
    existing reference-entity :Canon nodes already projected for the concept
    -- node counts for the concept are unchanged except the new misconception
    node."""
    await _seed_subject_concept(db)
    await _seed_reference_entity(db, canonical_key="def.real_basis")
    ref2_id = await _seed_reference_entity(db, canonical_key="eq.newton2")
    await _seed_observations(
        db,
        signature="emergent.def.real_basis",
        opposes="def.real_basis",
        students=(_U1, _U2, _U3),
    )
    await db.commit()

    # Pre-project the reference entities directly (simulating a prior,
    # independent :Canon projection run for this concept).
    from apollo.knowledge_graph.canon_projection import project_canon

    await project_canon(db, tc_neo4j, concept_id=_CONCEPT)

    async def _count_canon() -> int:
        async with tc_neo4j.session() as s:
            rec = await (await s.run("MATCH (c:Canon) RETURN count(c) AS n")).single()
            return int(rec["n"])

    before = await _count_canon()
    assert before == 2  # the two reference entities

    await materialize.materialize_if_promotable(
        db,
        tc_neo4j,
        search_space_id=_SPACE,
        concept_id=_CONCEPT,
        signature="emergent.def.real_basis",
        opposes_entity_key="def.real_basis",
    )
    await db.commit()

    after = await _count_canon()
    assert after == 3  # +1 new misconception node, existing 2 untouched

    async with tc_neo4j.session() as s:
        rec = await (
            await s.run("MATCH (c:Canon {canonical_key: $k}) RETURN c AS c", k="eq.newton2")
        ).single()
        untouched = dict(rec["c"])
    assert untouched["key"] == ref2_id
    assert untouched["kind"] == "definition"


# --------------------------------------------------------------------------- #
# Flag coupling is the caller's job (T2/T3 wiring tests) -- materialize.py
# itself has no flag read, matching the capture module's own design (pure
# helper, flag lives at the call site).
# --------------------------------------------------------------------------- #
