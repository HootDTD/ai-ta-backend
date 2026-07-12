"""WU-3C1 — :Canon projection seeder.

Reads Layer-1 entities from Postgres (``apollo_kg_entities``, seeded by WU-3B)
and idempotently ``MERGE``s ``:Canon`` nodes in Neo4j. The projection is
WRITE-ONLY here — the §5 resolver, ``RESOLVES_TO`` edges, and ``:Canon`` reads
are WU-3C2 and explicitly out of scope.

Load-bearing isolation guarantee (§2 Neo4j subsection)
------------------------------------------------------
The ``:Canon`` node key is the BIGSERIAL surrogate id ``apollo_kg_entities.id``,
NEVER a synthesized ``f"{search_space_id}:{canonical_key}"``. Postgres
uniqueness is ``(concept_id, canonical_key)`` — so two DIFFERENT concepts can
each legitimately mint the same ``canonical_key`` (e.g. two fluid concepts both
minting ``eq.continuity``). A key synthesized from ``canonical_key`` would FUSE
those two distinct concepts into ONE ``:Canon`` node and cross-contaminate every
future ``RESOLVES_TO`` edge. The surrogate entity id makes fusion impossible and
survives ``canonical_key`` renames.

Rebuildability (§2)
-------------------
``:Canon`` is never authored in Neo4j; it is projected from Postgres and is
rebuildable at any time via :func:`project_canon` (or ``scripts/seed_canon_projection.py``).
The ``MERGE`` on ``key`` makes re-runs idempotent: a second projection over the
same entities yields the SAME node count (the ``SET`` re-applies identical
properties; no duplicates).

This is a node-only projection. It does not copy Layer-1 ``EntityPrereq`` rows
or mint any Neo4j DEPENDS_ON relationship, so no direction translation occurs.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apollo.errors import CanonProjectionError
from apollo.persistence.models import Concept, KGEntity, Subject
from apollo.persistence.neo4j_client import Neo4jClient

# ---------------------------------------------------------------------------
# Cypher — MERGE (not CREATE) on the surrogate `key` ONLY. MERGE-on-key is what
# makes re-runs idempotent and fusion impossible: same key -> same node.
# ---------------------------------------------------------------------------
_CANON_MERGE_CYPHER = (
    "UNWIND $rows AS row\n"
    "MERGE (c:Canon {key: row.key})\n"
    "SET c.search_space_id = row.search_space_id,\n"
    "    c.concept_id = row.concept_id,\n"
    "    c.canonical_key = row.canonical_key,\n"
    "    c.kind = row.kind,\n"
    "    c.display_name = row.display_name\n"
    "RETURN count(c) AS merged"
)


@dataclass(frozen=True)
class CanonNodeSpec:
    """Pre-Neo4j property bag for one ``:Canon`` node. Immutable (§ coding rule).

    ``key`` is the surrogate ``apollo_kg_entities.id`` — see the module
    docstring for why this, not a ``canonical_key``-derived key, is what
    prevents cross-concept fusion.
    """

    key: int  # == apollo_kg_entities.id (surrogate)
    search_space_id: int
    concept_id: int
    canonical_key: str
    kind: str
    display_name: str


@dataclass(frozen=True)
class CanonProjectionResult:
    """Structured outcome of one :func:`project_canon` run."""

    merged: int
    entity_count: int


def entity_to_canon_spec(
    entity_id: int,
    *,
    concept_id: int,
    search_space_id: int,
    canonical_key: str,
    kind: str,
    display_name: str,
) -> CanonNodeSpec:
    """The single Postgres-entity -> :Canon property-bag mapping point.

    Pure, DB-free, no Neo4j. ``key`` is the surrogate entity id verbatim.
    """
    return CanonNodeSpec(
        key=entity_id,
        search_space_id=search_space_id,
        concept_id=concept_id,
        canonical_key=canonical_key,
        kind=kind,
        display_name=display_name,
    )


def canon_spec_to_row(spec: CanonNodeSpec) -> dict[str, Any]:
    """Flatten a :class:`CanonNodeSpec` to the Cypher ``$rows`` property bag —
    the exact 6-key shape :data:`_CANON_MERGE_CYPHER` consumes."""
    return {
        "key": spec.key,
        "search_space_id": spec.search_space_id,
        "concept_id": spec.concept_id,
        "canonical_key": spec.canonical_key,
        "kind": spec.kind,
        "display_name": spec.display_name,
    }


async def load_entity_specs(
    db: AsyncSession,
    *,
    search_space_id: int | None = None,
    concept_id: int | None = None,
) -> list[CanonNodeSpec]:
    """Read ``apollo_kg_entities`` (joined to recover ``search_space_id``) into
    :class:`CanonNodeSpec` rows.

    Scope selector (the isolation invariant forbids a course-blind seed):
      * ``concept_id`` given -> only that concept's entities;
      * elif ``search_space_id`` given -> every concept under that course;
      * else -> ``ValueError`` (refuse a global unscoped projection).

    Misconception entities (``kind == "misconception"``) ARE included — no kind
    filtering. Wraps the read so any DB failure surfaces as
    ``CanonProjectionError(stage="load_entities", ...)`` (NO FALLBACK).
    """
    if concept_id is None and search_space_id is None:
        raise ValueError(
            "load_entity_specs requires a scope: pass concept_id or "
            "search_space_id (course-blind projection is forbidden)"
        )

    stmt = (
        select(
            KGEntity.id,
            KGEntity.concept_id,
            Subject.search_space_id,
            KGEntity.canonical_key,
            KGEntity.kind,
            KGEntity.display_name,
        )
        .join(Concept, Concept.id == KGEntity.concept_id)
        .join(Subject, Subject.id == Concept.subject_id)
    )
    if concept_id is not None:
        stmt = stmt.where(KGEntity.concept_id == concept_id)
    else:
        stmt = stmt.where(Subject.search_space_id == search_space_id)

    try:
        rows = (await db.execute(stmt)).all()
    except Exception as exc:  # noqa: BLE001 - surface as a named error
        raise CanonProjectionError(stage="load_entities", last_error=str(exc)) from exc

    return [
        entity_to_canon_spec(
            row.id,
            concept_id=row.concept_id,
            search_space_id=row.search_space_id,
            canonical_key=row.canonical_key,
            kind=row.kind,
            display_name=row.display_name,
        )
        for row in rows
    ]


async def merge_specs(neo: Neo4jClient, specs: list[CanonNodeSpec]) -> int:
    """Idempotently ``MERGE`` the given specs as ``:Canon`` nodes. Returns the
    ``merged`` count. Raises ``CanonProjectionError(stage="merge_canon", ...)``
    on any Neo4j failure (NO FALLBACK). Extracted as a seam so the Neo4j-write
    half can be tested directly against a container with hand-built specs."""
    rows = [canon_spec_to_row(s) for s in specs]
    if not rows:
        return 0
    try:
        async with neo.session() as s:
            result = await s.run(_CANON_MERGE_CYPHER, rows=rows)
            rec = await result.single()
            return int(rec["merged"]) if rec else 0
    except CanonProjectionError:
        raise
    except Exception as exc:  # noqa: BLE001 - surface as a named error
        raise CanonProjectionError(stage="merge_canon", last_error=str(exc)) from exc


async def project_canon(
    db: AsyncSession,
    neo: Neo4jClient,
    *,
    search_space_id: int | None = None,
    concept_id: int | None = None,
) -> CanonProjectionResult:
    """Project Layer-1 entities for one scope into idempotent ``:Canon`` nodes.

    Read-only on Postgres; one ``MERGE`` batch in one Neo4j session (no
    cross-store transaction — the projection is rebuildable precisely because
    it holds none). Raises ``CanonProjectionError`` on either side's failure.
    """
    specs = await load_entity_specs(db, search_space_id=search_space_id, concept_id=concept_id)
    merged = await merge_specs(neo, specs)
    return CanonProjectionResult(merged=merged, entity_count=len(specs))
