"""WU-3B2c — the §8B.5 course-local dedup ladder.

``resolve_candidate`` resolves ONE candidate entity against THIS course's
existing ``apollo_kg_entities`` inventory and returns a frozen ``DedupVerdict``,
writing exactly ONE ``apollo_dedup_decisions`` audit row per resolution. The
ladder is:

    slug-exact  ->  scope_summary embedding cosine  ->  injected LLM-judge

The COURSE-SCOPE WHERE (``Subject.search_space_id == :search_space_id``) is
applied in SQL BEFORE any cosine is ever computed (§1.4) — never a global
similarity search filtered afterward. Two courses that each hold a byte-identical
``scope_summary`` therefore NEVER merge, because the other course's entity is
removed from the candidate set first.

Embedding source is the on-the-fly-embedded ``KGEntity.scope_summary`` TEXT
column (migration 030 / WU-3B2a); there is NO persisted entity vector. The
embedder (``embed_fn``) and the LLM judge (``judge_fn``) are INJECTED SYNC
callables so Tier-1 tests are deterministic with zero network calls — production
wiring (3B2d/3B2f) supplies ``embed_text`` and a ``cheap_chat`` adapter.

Out of scope (downstream units — NOT here): scrape/mint/upsert of entities or
problems (3B2d), solution pairing (3B2e), metering/queue-drain (3B2f), the
``apollo_ingest_runs.n_dedup_merged`` aggregate and the worker shell (3B2g),
quarantine (3B2h). This unit reads the inventory + writes ONE audit row.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apollo.persistence.models import Concept, DedupDecision, KGEntity, Subject
from apollo.provisioning.dedup_constants import (
    EMBED_JUDGE_BAND,
    EMBED_MERGE_THRESHOLD,
)

__all__ = ["DedupVerdict", "resolve_candidate"]


@dataclass(frozen=True)
class DedupVerdict:
    """The immutable result of a single course-local dedup resolution.

    * ``verdict``           — ``'merged'`` | ``'distinct'``
    * ``method``            — the tier that decided: ``'slug'`` | ``'embedding'``
                              | ``'llm_judge'``
    * ``similarity``        — the cosine for the embedding/llm_judge tiers;
                              ``None`` for the slug tier and the empty-inventory
                              distinct case
    * ``matched_entity_id`` — ``KGEntity.id`` merged onto; ``None`` when distinct
    """

    verdict: str
    method: str
    similarity: float | None
    matched_entity_id: int | None


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    """Cosine similarity of two vectors.

    Returns ``0.0`` (never ``NaN``) when either vector is all-zero, so a
    degenerate embedding can never spuriously merge. Pure stdlib ``math`` — no
    new package (ADJ #8).
    """
    dot = 0.0
    norm_a = 0.0
    norm_b = 0.0
    for x, y in zip(a, b, strict=True):
        dot += x * y
        norm_a += x * x
        norm_b += y * y
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a**0.5 * norm_b**0.5)


async def _in_course_entities(
    db: AsyncSession,
    *,
    search_space_id: int,
    require_summary: bool,
) -> list[KGEntity]:
    """THE load-bearing course filter (§1.4).

    Returns the entities whose concept's subject belongs to ``search_space_id``,
    ordered by ascending ``KGEntity.id`` (earliest writer first — the
    deterministic first-writer-wins order). The ``Subject.search_space_id`` WHERE
    runs in SQL BEFORE any cosine is computed; removing it causes a cross-course
    false-merge (the property ``test_cross_course_*`` pins).

    ``require_summary=True`` additionally drops ``scope_summary IS NULL`` rows
    (the embedding tier needs a text to embed); the slug tier passes ``False``
    because a slug match does not require a summary.
    """
    stmt = (
        select(KGEntity)
        .join(Concept, Concept.id == KGEntity.concept_id)
        .join(Subject, Subject.id == Concept.subject_id)
        .where(Subject.search_space_id == search_space_id)
        .order_by(KGEntity.id.asc())
    )
    if require_summary:
        stmt = stmt.where(KGEntity.scope_summary.is_not(None))
    return list((await db.execute(stmt)).scalars().all())


async def _record_decision(
    db: AsyncSession,
    *,
    search_space_id: int,
    concept_id: int,
    candidate_key: str,
    method: str,
    similarity: float | None,
    verdict: str,
    matched_entity_id: int | None,
    ingest_run_id: int | None,
) -> None:
    """Construct and flush EXACTLY ONE ``apollo_dedup_decisions`` row.

    Immutable: builds a new ORM row, never mutates its inputs. ``flush`` (not
    ``commit``) suffices — the savepoint test fixture rolls back, and 3B2d/3B2g
    own the surrounding transaction in production.
    """
    db.add(
        DedupDecision(
            ingest_run_id=ingest_run_id,
            search_space_id=search_space_id,
            concept_id=concept_id,
            candidate_key=candidate_key,
            method=method,
            similarity=similarity,
            verdict=verdict,
            matched_entity_id=matched_entity_id,
        )
    )
    await db.flush()


async def resolve_candidate(
    db: AsyncSession,
    *,
    search_space_id: int,
    concept_id: int,
    candidate,
    embed_fn: Callable[[str], Sequence[float]],
    judge_fn: Callable[..., str],
    ingest_run_id: int | None = None,
) -> DedupVerdict:
    """Resolve ``candidate`` against this course's inventory; return a verdict.

    Runs the slug -> embedding -> judge ladder, short-circuiting on the first
    tier that decides, and writes EXACTLY ONE audit row on that tier. ``embed_fn``
    and ``judge_fn`` are SYNC callables (``embed_text`` / a ``cheap_chat``
    adapter) — called directly, NOT awaited.

    ``candidate`` is duck-typed: ``candidate.canonical_key`` (the slug + the
    ``candidate_key`` audit column) and ``candidate.scope_summary`` (the text the
    embedding tier embeds). 3B2d owns the real candidate type.
    """
    candidate_key = candidate.canonical_key

    # --- SLUG tier ---------------------------------------------------------- #
    # Exact canonical_key match within the course (no scope_summary required).
    # First-writer-wins: the ascending-id order means the earliest match wins if
    # (defensively) more than one slug somehow collides.
    slug_pool = await _in_course_entities(
        db, search_space_id=search_space_id, require_summary=False
    )
    for ent in slug_pool:
        if ent.canonical_key == candidate_key:
            await _record_decision(
                db,
                search_space_id=search_space_id,
                concept_id=concept_id,
                candidate_key=candidate_key,
                method="slug",
                similarity=None,
                verdict="merged",
                matched_entity_id=int(ent.id),
                ingest_run_id=ingest_run_id,
            )
            return DedupVerdict(
                verdict="merged",
                method="slug",
                similarity=None,
                matched_entity_id=int(ent.id),
            )

    # --- EMBEDDING tier ----------------------------------------------------- #
    embed_pool = await _in_course_entities(
        db, search_space_id=search_space_id, require_summary=True
    )
    if not embed_pool:
        # Nothing to compare against -> distinct (similarity unknown).
        await _record_decision(
            db,
            search_space_id=search_space_id,
            concept_id=concept_id,
            candidate_key=candidate_key,
            method="embedding",
            similarity=None,
            verdict="distinct",
            matched_entity_id=None,
            ingest_run_id=ingest_run_id,
        )
        return DedupVerdict(
            verdict="distinct",
            method="embedding",
            similarity=None,
            matched_entity_id=None,
        )

    cand_vec = embed_fn(candidate.scope_summary)
    # Pick the MAX cosine; ties break to the LOWEST entity id (-id maximised).
    best = max(
        embed_pool,
        key=lambda ent: (_cosine(cand_vec, embed_fn(str(ent.scope_summary))), -ent.id),
    )
    max_cos = _cosine(cand_vec, embed_fn(str(best.scope_summary)))

    band_low, _band_high = EMBED_JUDGE_BAND

    if max_cos >= EMBED_MERGE_THRESHOLD:
        await _record_decision(
            db,
            search_space_id=search_space_id,
            concept_id=concept_id,
            candidate_key=candidate_key,
            method="embedding",
            similarity=max_cos,
            verdict="merged",
            matched_entity_id=int(best.id),
            ingest_run_id=ingest_run_id,
        )
        return DedupVerdict(
            verdict="merged",
            method="embedding",
            similarity=max_cos,
            matched_entity_id=int(best.id),
        )

    if max_cos < band_low:
        await _record_decision(
            db,
            search_space_id=search_space_id,
            concept_id=concept_id,
            candidate_key=candidate_key,
            method="embedding",
            similarity=max_cos,
            verdict="distinct",
            matched_entity_id=None,
            ingest_run_id=ingest_run_id,
        )
        return DedupVerdict(
            verdict="distinct",
            method="embedding",
            similarity=max_cos,
            matched_entity_id=None,
        )

    # --- LLM-JUDGE tier (band tiebreaker: band_low <= max_cos < merge) ------ #
    decision = judge_fn(
        candidate.scope_summary,
        best.scope_summary,
        search_space_id=search_space_id,
    )
    merged = decision == "merged"
    matched_id = int(best.id) if merged else None
    await _record_decision(
        db,
        search_space_id=search_space_id,
        concept_id=concept_id,
        candidate_key=candidate_key,
        method="llm_judge",
        similarity=max_cos,
        verdict="merged" if merged else "distinct",
        matched_entity_id=matched_id,
        ingest_run_id=ingest_run_id,
    )
    return DedupVerdict(
        verdict="merged" if merged else "distinct",
        method="llm_judge",
        similarity=max_cos,
        matched_entity_id=matched_id,
    )
