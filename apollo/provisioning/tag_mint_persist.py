"""WU-3B2d stage-4 persistence helpers (PARALLELED from the seed script).

These generalize ``scripts/seed_apollo_learner_model.py``'s write pattern
(``_resolve_concept`` :137, ``_upsert_entity`` :165, ``_link_opposes`` :200,
``_insert_prereqs`` :232) for the AUTO-provisioning path: the concept comes from
an LLM tag (not the hardcoded ``_BERNOULLI_SLUG``) and the data comes from the
``ApprovedPair`` (not from disk). The seed script is WRAPPED, not imported — it
hardcodes bernoulli.

ALL persistence keys on the BIGINT ``apollo_concepts.id`` (resolved from the LLM
slug), never the slug (the §6 namespace contract). Idempotent: entity upsert on
``(concept_id, canonical_key)``; prereqs ``(from_entity_id, to_entity_id)``
SELECT-then-skip; concept symbol authoring first-writer-wins UNION.

Layer-1 prereqs deliberately retain the legacy dependent -> prerequisite
(``from depends on to``) direction. This module does not copy them into KG or
canonical DEPENDS_ON edges, whose unified direction is prerequisite -> dependent.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from apollo.persistence.learner_model_seed import EntitySpec
from apollo.persistence.models import Concept, EntityPrereq, KGEntity, Subject

_LOG = logging.getLogger(__name__)


async def resolve_or_create_concept(
    db: AsyncSession,
    *,
    search_space_id: int,
    slug: str,
    display_name: str,
) -> int:
    """Resolve the LLM-tagged concept slug to a BIGINT ``apollo_concepts.id`` for
    this course, CREATING the concept (under the course's first subject) if
    absent. Idempotent: re-resolves to the SAME id (the §6 namespace contract —
    key on BIGINT, never slug)."""
    concept_id = (
        await db.execute(
            select(Concept.id)
            .join(Subject, Subject.id == Concept.subject_id)
            .where(Subject.search_space_id == search_space_id)
            .where(Concept.slug == slug)
        )
    ).scalar_one_or_none()
    if concept_id is not None:
        return concept_id

    subject_id = (
        (
            await db.execute(
                select(Subject.id)
                .where(Subject.search_space_id == search_space_id)
                .order_by(Subject.id.asc())
            )
        )
        .scalars()
        .first()
    )
    if subject_id is None:
        subject = Subject(
            slug=f"auto-{search_space_id}",
            display_name="Auto-provisioned",
            search_space_id=search_space_id,
        )
        db.add(subject)
        await db.flush()
        subject_id = int(subject.id)

    concept = Concept(subject_id=subject_id, slug=slug, display_name=display_name)
    db.add(concept)
    await db.flush()
    return int(concept.id)


async def author_concept_symbols(
    db: AsyncSession,
    *,
    concept_id: int,
    symbols: Sequence[str],
    normalization: dict[str, str],
) -> list[str]:
    """First-writer-wins UNION of ``canonical_symbols``/``normalization_map`` onto
    the concept (§8B.5: a later material may ADD symbols but never REWRITE the
    first writer's canonical set). Returns the symbols authored/unioned THIS call
    (the ones not already present).

    ``canonical_symbols`` is stored in the ``CanonicalSymbols``-validatable shape
    ``{"symbols": [...], "description": {...}, ...}`` — the SAME shape the
    registry seed writes and the runtime reader
    (``apollo.subjects.curriculum_db.load_concept_definition`` →
    ``CanonicalSymbols.model_validate``) reads on every teaching session. The
    union is over the existing ``"symbols"`` LIST (first-writer-wins; new symbols
    are appended, existing entries are NEVER removed or reordered-away). Any
    other ``CanonicalSymbols`` keys already authored (``description``,
    ``subscript_convention``) are preserved untouched. ``normalization_map`` is a
    flat ``{alias: canonical}`` dict; existing keys are NEVER overwritten."""
    concept = (await db.execute(select(Concept).where(Concept.id == concept_id))).scalar_one()

    existing_canon = dict(concept.canonical_symbols or {})
    existing_norm = dict(concept.normalization_map or {})

    existing_symbols = list(existing_canon.get("symbols") or [])
    existing_set = set(existing_symbols)

    newly_authored: list[str] = []
    for sym in symbols:
        if sym not in existing_set:
            existing_set.add(sym)
            newly_authored.append(sym)
    for alias, canonical in normalization.items():
        existing_norm.setdefault(alias, canonical)

    # Immutable assignment of a NEW CanonicalSymbols-validatable dict (no in-place
    # mutation of the ORM column). Preserve any pre-authored description /
    # subscript_convention; only the symbol LIST is unioned.
    new_canon = dict(existing_canon)
    new_canon["symbols"] = sorted(existing_symbols + newly_authored)
    new_canon.setdefault("description", {})
    concept.canonical_symbols = new_canon  # type: ignore[assignment]
    concept.normalization_map = existing_norm  # type: ignore[assignment]
    await db.flush()
    return newly_authored


async def upsert_entity(
    db: AsyncSession,
    *,
    concept_id: int,
    spec: EntitySpec,
    scope_summary: str | None,
) -> tuple[int, bool]:
    """Upsert one entity keyed on ``(concept_id, canonical_key)``. Returns
    ``(entity_id, inserted)``. Idempotent — a re-run with the same key updates in
    place (no duplicate row). Mirrors seed ``_upsert_entity`` but also writes the
    dedup ladder's embedding source ``scope_summary``."""
    row = (
        await db.execute(
            select(KGEntity)
            .where(KGEntity.concept_id == concept_id)
            .where(KGEntity.canonical_key == spec.canonical_key)
        )
    ).scalar_one_or_none()

    if row is None:
        row = KGEntity(
            concept_id=concept_id,
            canonical_key=spec.canonical_key,
            kind=spec.kind,
            display_name=spec.display_name,
            payload=dict(spec.payload),
            aliases=list(spec.aliases),
            scope_summary=scope_summary,
        )
        db.add(row)
        await db.flush()
        return int(row.id), True

    row.kind = spec.kind  # type: ignore[assignment]
    row.display_name = spec.display_name  # type: ignore[assignment]
    row.payload = dict(spec.payload)  # type: ignore[assignment]
    row.aliases = list(spec.aliases)  # type: ignore[assignment]
    if scope_summary is not None:
        row.scope_summary = scope_summary  # type: ignore[assignment]
    await db.flush()
    return int(row.id), False


async def link_opposes(
    db: AsyncSession,
    *,
    concept_id: int,
    key_to_id: dict[str, int],
) -> int:
    """Second pass: resolve each misconception's ``payload.opposes_entity_key`` to
    ``opposes_entity_id`` now that every entity row exists. Returns the count
    linked. Raises ``KeyError`` via the caller's lookup when an opposes key
    resolves to no entity (the caller maps that to a fail-closed TagMintError)."""
    rows = (
        (
            await db.execute(
                select(KGEntity)
                .where(KGEntity.concept_id == concept_id)
                .where(KGEntity.kind == "misconception")
            )
        )
        .scalars()
        .all()
    )

    linked = 0
    for row in rows:
        payload = dict(row.payload or {})
        opposes_key = payload.get("opposes_entity_key")
        if not opposes_key:
            continue
        target_id = key_to_id[opposes_key]  # KeyError → caller raises TagMintError
        payload["opposes_entity_id"] = target_id
        row.payload = payload  # type: ignore[assignment]
        linked += 1
    await db.flush()
    return linked


async def drop_unlinkable_minted_misconceptions(
    db: AsyncSession,
    *,
    concept_id: int,
    key_to_id: dict[str, int],
    minted_entity_ids: dict[str, int],
) -> list[dict[str, int | str]]:
    """Delete THIS mint's misconception rows whose ``opposes_entity_key``
    resolves to no minted/merged entity, returning what was dropped.

    Scoped strictly to rows this mint created (``minted_entity_ids``): a
    pre-existing misconception with an unresolvable key keeps
    ``link_opposes``'s fail-closed contract, as does ``emergent/materialize.py``
    (the other ``link_opposes`` caller — never routed through here). Dropping
    creates no wrong link — unlike guessing a mapping — it only loses one
    enrichment edge, mirroring the 5b unresolvable-prereq drop policy."""
    minted_ids = set(minted_entity_ids.values())
    rows = (
        (
            await db.execute(
                select(KGEntity)
                .where(KGEntity.concept_id == concept_id)
                .where(KGEntity.kind == "misconception")
            )
        )
        .scalars()
        .all()
    )
    dropped: list[dict[str, int | str]] = []
    for row in rows:
        if int(row.id) not in minted_ids:
            continue
        opposes_key = dict(row.payload or {}).get("opposes_entity_key")
        if not opposes_key or opposes_key in key_to_id:
            continue
        dropped.append(
            {
                "canonical_key": str(row.canonical_key),
                "opposes_entity_key": str(opposes_key),
                "entity_id": int(row.id),
            }
        )
        await db.delete(row)
    if dropped:
        await db.flush()
    return dropped


async def _entities_concept_map(db: AsyncSession, entity_ids: Iterable[int]) -> dict[int, int]:
    """Map each ``KGEntity.id`` to its owning ``concept_id`` in ONE query. A missing
    id is simply absent from the map — the caller treats absence as out-of-scope
    (the fail-safe direction)."""
    ids = set(entity_ids)
    if not ids:
        return {}
    return {
        int(eid): int(cid)
        for eid, cid in (
            await db.execute(select(KGEntity.id, KGEntity.concept_id).where(KGEntity.id.in_(ids)))
        ).all()
    }


async def load_concept_entities(db: AsyncSession, *, concept_id: int) -> list[KGEntity]:
    """Load THIS concept's already-PERSISTED ``apollo_kg_entities`` rows (ascending
    ``id`` — first-writer-wins order), for the cross-mint deterministic content-
    equality pre-match (G4.3, cross-UPLOAD half). ``concept_id`` uniquely belongs to
    one subject / search space, so the concept filter alone reproduces
    ``resolve_candidate``'s course+concept dedup pool scope. Returns FULL rows so the
    caller can recompute each entity's ``_equivalence_signature`` (needs
    ``kind``/``display_name``/``payload``) — no embedding, no similarity."""
    return list(
        (
            await db.execute(
                select(KGEntity)
                .where(KGEntity.concept_id == concept_id)
                .order_by(KGEntity.id.asc())
            )
        )
        .scalars()
        .all()
    )


async def load_concept_prereq_adjacency(
    db: AsyncSession, *, concept_id: int, entity_ids: Iterable[int]
) -> dict[int, set[int]]:
    """Load PERSISTED ``apollo_entity_prereqs`` edges among ``entity_ids`` scoped
    to ``concept_id`` (BOTH endpoints must be owned by the concept), returned as
    an adjacency map ``from_entity_id -> {to_entity_id}``. Used to SEED a
    cross-mint acyclicity check (M2): the composite PK ``(from_entity_id,
    to_entity_id)`` permits both ``(A,B)`` and ``(B,A)`` with no DB-level
    acyclicity, so a SECOND mint into the same shared concept that drafts the
    REVERSE of an EARLIER mint's persisted edge (mint 1: A->B, mint 2: B->A) would
    otherwise pass — ``_acyclic_prereq_pairs`` starts its ``adj`` empty each call
    and never sees mint 1's already-committed edge. Scoped to ``entity_ids`` (the
    resolved entity ids of the CURRENT mint) — a cross-mint cycle can only involve
    entities the current draft actually names."""
    ids = set(entity_ids)
    if not ids:
        return {}
    from_entity = aliased(KGEntity)
    to_entity = aliased(KGEntity)
    rows = (
        await db.execute(
            select(EntityPrereq.from_entity_id, EntityPrereq.to_entity_id)
            .join(from_entity, from_entity.id == EntityPrereq.from_entity_id)
            .join(to_entity, to_entity.id == EntityPrereq.to_entity_id)
            .where(EntityPrereq.from_entity_id.in_(ids))
            .where(EntityPrereq.to_entity_id.in_(ids))
            .where(from_entity.concept_id == concept_id)
            .where(to_entity.concept_id == concept_id)
        )
    ).all()
    adj: dict[int, set[int]] = {}
    for from_id, to_id in rows:
        adj.setdefault(int(from_id), set()).add(int(to_id))
    return adj


def _reaches(adj: dict[int, set[int]], src: int, dst: int) -> bool:
    """Directed reachability over an adjacency map (DFS). Shared by the
    writer-boundary cycle guard below — kept local (not imported from
    ``tag_mint``) to avoid a circular import (``tag_mint`` imports this module)."""
    seen: set[int] = set()
    stack = [src]
    while stack:
        node = stack.pop()
        if node == dst:
            return True
        if node in seen:
            continue
        seen.add(node)
        stack.extend(adj.get(node, ()))
    return False


async def partition_prereqs_by_concept_scope(
    db: AsyncSession,
    *,
    concept_id: int,
    key_to_id: dict[str, int],
    pairs: Sequence[tuple[str, str]],
) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """Split resolvable prereq key-pairs into ``(in_concept, cross_concept)`` by
    whether BOTH endpoint entities are OWNED BY ``concept_id`` (audit bug #4).

    A dedup ``merged`` verdict can point ``key_to_id`` at a FOREIGN-concept entity
    id — that key is still 'resolvable', so an unminted-key drop never catches it.
    This is the single source of truth for the endpoint concept-scope check, reused
    by ``insert_prereqs`` (the writer boundary) AND by ``tag_and_mint`` BEFORE its
    acyclicity guard. Running it first matters: it keeps a foreign endpoint out of
    the acyclicity reachability graph, where it could otherwise act as a PHANTOM
    BRIDGE across two cross-concept edges and fake a cycle that discards a
    legitimate within-concept edge. A pair whose key is not in ``key_to_id`` raises
    KeyError (the caller's fail-closed contract for an unresolvable key)."""
    concept_of = await _entities_concept_map(db, (key_to_id[key] for pair in pairs for key in pair))
    in_concept: list[tuple[str, str]] = []
    cross_concept: list[tuple[str, str]] = []
    for from_key, to_key in pairs:
        if (
            concept_of.get(key_to_id[from_key]) == concept_id
            and concept_of.get(key_to_id[to_key]) == concept_id
        ):
            in_concept.append((from_key, to_key))
        else:
            cross_concept.append((from_key, to_key))
    return in_concept, cross_concept


async def insert_prereqs(
    db: AsyncSession,
    *,
    concept_id: int,
    key_to_id: dict[str, int],
    pairs: Sequence[tuple[str, str]],
) -> tuple[int, int, list[tuple[str, str]]]:
    """Insert prereq edges (SELECT-then-skip on ``(from_entity_id,
    to_entity_id)``). Returns ``(inserted, skipped, dropped)``.

    CONCEPT-SCOPE GUARD (audit bug #4, defense-in-depth at the WRITER boundary): via
    ``partition_prereqs_by_concept_scope``, every edge endpoint must resolve to an
    entity OWNED BY ``concept_id``. A dedup ``merged`` verdict can point
    ``key_to_id`` at a FOREIGN-concept entity id — that key is still 'resolvable',
    so the caller's unminted-key drop never catches it, and the edge would persist
    pointing into another concept's subgraph (dead weight the reader filters out at
    ``personalization_read.py:148-150``; and, because ``:Canon`` is a NODE-ONLY
    projection, the foreign target entity is absent from THIS concept's ``:Canon``
    nodes, so a student's correct step has no reference node to match). A
    cross-concept edge is DROPPED (surfaced via the return + a WARNING log), not
    inserted. The writer re-checks even though ``tag_and_mint`` already pre-filters
    — the guard must not trust its caller. With PR2's concept-scoped dedup this
    should never fire; if it does, an upstream scoping invariant regressed.

    PERSISTED-CYCLE GUARD (M2, defense-in-depth at the WRITER boundary): the
    composite PK ``(from_entity_id, to_entity_id)`` has no DB-level acyclicity, so
    two SEPARATE mints into the same shared concept can each pass their own
    in-mint ``_acyclic_prereq_pairs`` check yet together persist a cycle (mint 1:
    A->B; mint 2 drafts B->A into a graph mint 2 never re-derives mint 1's edges
    from). ``tag_and_mint`` now seeds its acyclicity check from
    ``load_concept_prereq_adjacency`` BEFORE calling this function, so this should
    already be cycle-free on arrival; this guard re-derives the FULL persisted
    concept subgraph itself and re-checks each edge before insert — the writer
    must not trust its caller here either. An edge whose insertion WOULD close a
    directed cycle against everything already committed for ``concept_id`` is
    DROPPED (surfaced via the return + a distinct WARNING log), not inserted.

    A pair whose key is not in ``key_to_id`` raises KeyError (the caller maps it to
    a TagMintError — an unresolvable key is a caller contract violation, distinct
    from a resolvable-but-foreign endpoint)."""
    kept, dropped = await partition_prereqs_by_concept_scope(
        db, concept_id=concept_id, key_to_id=key_to_id, pairs=pairs
    )
    kept_ids = {key_to_id[key] for pair in kept for key in pair}
    adj = await load_concept_prereq_adjacency(db, concept_id=concept_id, entity_ids=kept_ids)
    inserted = 0
    skipped = 0
    cyclic: list[tuple[str, str]] = []
    for from_key, to_key in kept:
        from_id = key_to_id[from_key]
        to_id = key_to_id[to_key]
        existing = (
            await db.execute(
                select(EntityPrereq)
                .where(EntityPrereq.from_entity_id == from_id)
                .where(EntityPrereq.to_entity_id == to_id)
            )
        ).scalar_one_or_none()
        if existing is not None:
            skipped += 1
            continue
        if from_id == to_id or _reaches(adj, to_id, from_id):
            cyclic.append((from_key, to_key))
            continue
        db.add(EntityPrereq(from_entity_id=from_id, to_entity_id=to_id))
        adj.setdefault(from_id, set()).add(to_id)
        inserted += 1
    await db.flush()
    if dropped:
        _LOG.warning(
            "tag_mint_dropped_cross_concept_prereqs",
            extra={
                "event": "tag_mint_dropped_cross_concept_prereqs",
                "concept_id": concept_id,
                "dropped": dropped,
            },
        )
    if cyclic:
        _LOG.warning(
            "tag_mint_dropped_persisted_cycle_prereqs",
            extra={
                "event": "tag_mint_dropped_persisted_cycle_prereqs",
                "concept_id": concept_id,
                "dropped": cyclic,
            },
        )
    return inserted, skipped, [*dropped, *cyclic]
