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
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apollo.persistence.learner_model_seed import EntitySpec
from apollo.persistence.models import Concept, EntityPrereq, KGEntity, Subject


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
        await db.execute(
            select(Subject.id)
            .where(Subject.search_space_id == search_space_id)
            .order_by(Subject.id.asc())
        )
    ).scalars().first()
    if subject_id is None:
        subject = Subject(
            slug=f"auto-{search_space_id}",
            display_name="Auto-provisioned",
            search_space_id=search_space_id,
        )
        db.add(subject)
        await db.flush()
        subject_id = subject.id

    concept = Concept(subject_id=subject_id, slug=slug, display_name=display_name)
    db.add(concept)
    await db.flush()
    return concept.id


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
    concept = (
        await db.execute(select(Concept).where(Concept.id == concept_id))
    ).scalar_one()

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
    concept.canonical_symbols = new_canon
    concept.normalization_map = existing_norm
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
        return row.id, True

    row.kind = spec.kind
    row.display_name = spec.display_name
    row.payload = dict(spec.payload)
    row.aliases = list(spec.aliases)
    if scope_summary is not None:
        row.scope_summary = scope_summary
    await db.flush()
    return row.id, False


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
        await db.execute(
            select(KGEntity)
            .where(KGEntity.concept_id == concept_id)
            .where(KGEntity.kind == "misconception")
        )
    ).scalars().all()

    linked = 0
    for row in rows:
        payload = dict(row.payload or {})
        opposes_key = payload.get("opposes_entity_key")
        if not opposes_key:
            continue
        target_id = key_to_id[opposes_key]  # KeyError → caller raises TagMintError
        payload["opposes_entity_id"] = target_id
        row.payload = payload
        linked += 1
    await db.flush()
    return linked


async def insert_prereqs(
    db: AsyncSession,
    *,
    key_to_id: dict[str, int],
    pairs: Sequence[tuple[str, str]],
) -> tuple[int, int]:
    """Insert prereq edges (SELECT-then-skip on ``(from_entity_id,
    to_entity_id)``). Returns ``(inserted, skipped)``. A pair whose key is not in
    ``key_to_id`` raises KeyError (the caller maps it to a TagMintError)."""
    inserted = 0
    skipped = 0
    for from_key, to_key in pairs:
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
        db.add(EntityPrereq(from_entity_id=from_id, to_entity_id=to_id))
        inserted += 1
    await db.flush()
    return inserted, skipped
