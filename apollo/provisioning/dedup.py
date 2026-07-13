"""WU-3B2c — the §8B.5 course-local dedup ladder.

``resolve_candidate`` resolves ONE candidate entity against THIS course's
existing ``apollo_kg_entities`` inventory and returns a frozen ``DedupVerdict``,
writing exactly ONE ``apollo_dedup_decisions`` audit row per resolution. The
ladder is:

    slug-exact  ->  scope_summary embedding cosine  ->  injected LLM-judge

The candidate POOL is scoped in SQL BEFORE any cosine is ever computed (§1.4 +
PR2 — the 2026-06-30 false-merge fix) — never a global similarity search filtered
afterward. ``_in_course_entities`` applies, first: ``Subject.search_space_id``
(two COURSES never merge, even on a byte-identical ``scope_summary``), AND
``KGEntity.concept_id == :concept_id`` (two CONCEPTS of one course never merge —
no foreign-set binding / cross-concept fusion); plus ``exclude_entity_ids`` drops
entities minted earlier in the SAME mint, so two distinct nodes of one problem
(the ``m≡M`` fusion) cannot fuse against each other.

Embedding source is the on-the-fly-embedded ``KGEntity.scope_summary`` TEXT
column (migration 030 / WU-3B2a); there is NO persisted entity vector. The
embedder (``embed_fn``) and the LLM judge (``judge_fn``) are INJECTED SYNC
callables so Tier-1 tests are deterministic with zero network calls — production
wiring (3B2d/3B2f) supplies ``embed_text`` and a ``cheap_chat`` adapter.

Out of scope (downstream units — NOT here): scrape/mint/upsert of entities or
problems (3B2d), solution pairing (3B2e), metering/queue-drain (3B2f), the
``apollo_ingest_runs.n_dedup_merged`` aggregate and the worker shell (3B2g),
quarantine (3B2h). This unit reads the inventory, writes ONE audit row, and
increments the run's JSON dedup-pressure gauge in the same flow.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apollo.persistence.models import Concept, DedupDecision, IngestRun, KGEntity, Subject
from apollo.provisioning.dedup_constants import (
    EMBED_JUDGE_BAND,
    EMBED_MERGE_THRESHOLD,
)

__all__ = ["DedupVerdict", "resolve_candidate", "is_false_merge_risk"]


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
    concept_id: int,
    require_summary: bool,
    exclude_entity_ids: set[int] | None = None,
) -> list[KGEntity]:
    """THE load-bearing dedup-pool filter.

    Returns the candidate pool for ``resolve_candidate``: the entities of THIS
    ``concept_id`` (within ``search_space_id``), ordered by ascending
    ``KGEntity.id`` (earliest writer first — the deterministic first-writer-wins
    order). Scoping (PR2 — the 2026-06-30 dedup false-merge fix):

    * ``Subject.search_space_id`` keeps two COURSES apart (the ``test_cross_course_*``
      property); it runs in SQL BEFORE any cosine.
    * ``KGEntity.concept_id == concept_id`` keeps two CONCEPTS of one course apart,
      so a candidate never slug-/cosine-merges into a sibling or earlier-set
      concept (the audit's foreign-set binding + cross-concept fusions).
    * ``exclude_entity_ids`` drops entities minted earlier in the SAME mint, so two
      distinct nodes of one problem (``m``, ``M``) cannot fuse against each other —
      only PRE-EXISTING entities (from prior mints) are legitimate dedup targets.

    ``require_summary=True`` additionally drops ``scope_summary IS NULL`` rows
    (the embedding tier needs a text to embed); the slug tier passes ``False``
    because a slug match does not require a summary.
    """
    stmt = (
        select(KGEntity)
        .join(Concept, Concept.id == KGEntity.concept_id)
        .join(Subject, Subject.id == Concept.subject_id)
        .where(Subject.search_space_id == search_space_id)
        .where(KGEntity.concept_id == concept_id)
        .order_by(KGEntity.id.asc())
    )
    if require_summary:
        stmt = stmt.where(KGEntity.scope_summary.is_not(None))
    if exclude_entity_ids:
        stmt = stmt.where(KGEntity.id.not_in(exclude_entity_ids))
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
    """Construct one audit row and increment its run's dedup-pressure gauge.

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
    if ingest_run_id is not None:
        run = await db.get(IngestRun, ingest_run_id, with_for_update=True)
        if run is None:
            raise RuntimeError(f"dedup ingest_run {ingest_run_id} not found")

        pressure = dict(run.dedup_pressure or {})
        pressure["total_candidates"] = int(pressure.get("total_candidates", 0)) + 1
        if method == "slug" and verdict == "merged":
            pressure["exact_merges"] = int(pressure.get("exact_merges", 0)) + 1
        if method == "embedding":
            counter = "embedding_merges" if verdict == "merged" else "embedding_distinct"
            pressure[counter] = int(pressure.get(counter, 0)) + 1
            if verdict == "merged":
                per_concept = dict(pressure.get("per_concept", {}))
                concept_key = str(concept_id)
                per_concept[concept_key] = int(per_concept.get(concept_key, 0)) + 1
                pressure["per_concept"] = per_concept

        embedding_merges = int(pressure.get("embedding_merges", 0))
        embedding_distinct = int(pressure.get("embedding_distinct", 0))
        embedding_decisions = embedding_merges + embedding_distinct
        pressure["embedding_merge_ratio"] = (
            embedding_merges / embedding_decisions if embedding_decisions else 0.0
        )
        pressure.setdefault("per_concept", {})
        run.dedup_pressure = pressure  # type: ignore[assignment]
    await db.flush()


def is_false_merge_risk(candidate_key: str, existing_key: str) -> bool:
    """True if variable/concept keys differ in casing, subscripts, or numbers
    while sharing the same base symbol, indicating they represent distinct quantities
    that embedding models might falsely collapse.
    Example: 'm' vs 'M', 'v_1' vs 'V_1', 'v_1' vs 'v_2', 'v' vs 'v_1'.
    """
    cand_cleaned = candidate_key.split(".")[-1]
    exist_cleaned = existing_key.split(".")[-1]

    # Extract alphabetical base (ignoring digits and underscores)
    cand_base = "".join(re.findall(r"[A-Za-z]+", cand_cleaned))
    exist_base = "".join(re.findall(r"[A-Za-z]+", exist_cleaned))

    # If they don't even share the same base character(s) (case-insensitive), they are not a risk of false merge
    # because the embedding model or text representation is different enough (e.g., 'm' vs 'v' is fine).
    if cand_base.lower() != exist_base.lower():
        return False

    # Extract numeric parts or suffixes
    cand_nums = "".join(re.findall(r"\d+", cand_cleaned))
    exist_nums = "".join(re.findall(r"\d+", exist_cleaned))

    # If the base characters are the same (case-insensitive):
    # Case 1: They have different casing (e.g. 'm' vs 'M', 'v_1' vs 'V_1')
    if cand_cleaned != exist_cleaned:
        # If they differ in alphabetical casing
        if cand_base != exist_base:
            return True
        # Case 2: They have different numbers/subscripts (e.g. 'v_1' vs 'v_2', 'v' vs 'v_1')
        if cand_nums != exist_nums:
            return True

    return False


async def resolve_candidate(
    db: AsyncSession,
    *,
    search_space_id: int,
    concept_id: int,
    candidate,
    embed_fn: Callable[[str], Sequence[float]],
    judge_fn: Callable[..., str],
    ingest_run_id: int | None = None,
    exclude_entity_ids: set[int] | None = None,
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
        db,
        search_space_id=search_space_id,
        concept_id=concept_id,
        require_summary=False,
        exclude_entity_ids=exclude_entity_ids,
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
    raw_embed_pool = await _in_course_entities(
        db,
        search_space_id=search_space_id,
        concept_id=concept_id,
        require_summary=True,
        exclude_entity_ids=exclude_entity_ids,
    )
    embed_pool = [
        ent for ent in raw_embed_pool if not is_false_merge_risk(candidate_key, ent.canonical_key)
    ]
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
