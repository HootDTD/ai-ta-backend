"""WU-3B2d stage 1 — LLM scrape → Tier-1 ``apollo_concept_problems`` inventory.

``scrape_questions`` runs ONE injected ``chat_fn`` pass over a document's already-
embedded ``AITAChunk`` rows and parses each chunk's JSON into typed
``CandidateQuestion`` records (provenance + LLM difficulty + problem fields).
``write_tier1_problems`` persists those candidates as **Tier-1 inventory** — rows
that are explicitly ``tier=1`` and therefore NOT teachable (the §8B safety trap:
the ORM ``tier`` default is 2, so an omitted explicit value would silently make
scraped inventory selectable).

Idempotency key: a CONTENT hash of the chunk text (``chunk_content_hash``), NOT
the volatile ``aita_chunks.id`` — so a re-index that re-mints chunk ids is a
no-op. The hash is folded into a deterministic ``problem_code`` and the writer
SKIPS a row whose ``(concept_id, problem_code)`` already exists (a re-run inserts
ZERO rows). The provisional-inventory concept (slug ``provisional.inventory``,
created once per ``(subject, search_space)``) satisfies the NOT-NULL
``concept_id`` at scrape time; stage-4 ``tag_and_mint`` re-homes a promoted
problem onto its real tagged concept.

Failure mode (per chunk, fail-SOFT): a malformed/empty LLM JSON, or a candidate
that fails ``CandidateQuestion`` validation (e.g. an out-of-range difficulty),
yields ZERO candidates for that chunk and increments ``parse_failures`` — never a
half-parsed row. A DB error raises to the caller's transaction (3B2g owns
commit/rollback). NO network: ``chat_fn`` is injected (mocked in Tier-1).
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from database.models import AITAChunk

from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apollo.persistence.models import Concept, ConceptProblem, Subject
from apollo.schemas.problem import Difficulty

__all__ = [
    "CandidateQuestion",
    "ScrapeResult",
    "chunk_content_hash",
    "scrape_questions",
    "write_tier1_problems",
]

# The reserved per-course inventory concept slug (the SEAM resolution: a Tier-1
# row needs a NOT-NULL concept_id before stage-4 tagging exists).
PROVISIONAL_CONCEPT_SLUG = "provisional.inventory"


def _normalize(text: str) -> str:
    """Collapse internal whitespace, strip, lowercase — so two chunks differing
    only by whitespace/case hash IDENTICALLY (the idempotency key is content-
    stable, surviving a re-index). Mirrors ``problem_hash._normalize_text`` shape;
    a LOCAL helper, not an import, to keep ``problem_hash`` pure to its gate-8
    role."""
    return re.sub(r"\s+", " ", text).strip().lower()


def chunk_content_hash(content: str) -> str:
    """sha256 hex of the normalized chunk content (64 lowercase hex chars)."""
    return hashlib.sha256(_normalize(content).encode("utf-8")).hexdigest()


class CandidateQuestion(BaseModel):
    """One scraped question, pre-Tier-1-write. The scrape output type (3B2e
    consumes a compatible shape). Provenance keyed on ``chunk_content_hash``.

    ``difficulty`` is validated against the ``Problem.Difficulty`` literal set, so
    an out-of-range LLM value fails validation and that candidate is dropped (the
    fail-soft path) rather than writing an invalid Tier-1 row."""

    problem_text: str = Field(min_length=1)
    given_values: dict[str, float]
    target_unknown: str = Field(min_length=1)
    difficulty: Difficulty  # 'intro' | 'standard' | 'hard'
    document_id: int
    page: int | None = None
    chunk_content_hash: str = Field(min_length=1)
    concept_slug: str = Field(min_length=1)


@dataclass(frozen=True)
class ScrapeResult:
    """Immutable aggregate of one document's scrape. ``scraped_count`` is the
    number of chunks that yielded >=1 candidate; ``parse_failures`` is the number
    of (chunk, candidate) parse/validation drops the caller (3B2g) can log."""

    candidates: tuple[CandidateQuestion, ...]
    scraped_count: int
    parse_failures: int


def _coerce_candidate(
    raw: Any,
    *,
    chunk: AITAChunk,
) -> CandidateQuestion | None:
    """Build a ``CandidateQuestion`` from one LLM record, stamping the chunk-
    derived provenance (``document_id``/``page``/``chunk_content_hash`` come from
    the CHUNK, never the LLM). Returns ``None`` (a fail-soft drop) on any
    validation error."""
    if not isinstance(raw, dict):
        return None
    content_hash = chunk_content_hash(str(chunk.content))
    try:
        return CandidateQuestion(
            problem_text=raw.get("problem_text", ""),
            given_values=raw.get("given_values", {}),
            target_unknown=raw.get("target_unknown", ""),
            difficulty=raw.get("difficulty", ""),
            document_id=int(chunk.document_id),
            page=chunk.page_number,  # type: ignore[arg-type]  # nullable col
            chunk_content_hash=content_hash,
            concept_slug=raw.get("concept_slug", ""),
        )
    except ValidationError:
        return None


async def scrape_questions(
    chunks: Sequence[AITAChunk],
    *,
    chat_fn: Callable[..., str],
) -> ScrapeResult:
    """Scrape candidate questions from a document's chunks via one ``chat_fn``
    pass PER CHUNK. ``chat_fn`` is injected (cheap_chat-shaped; returns a JSON
    string array). MOCKED in Tier-1 — NO network here.

    Per-chunk fail-soft: a malformed/empty JSON response, a non-array payload, or
    any candidate that fails ``CandidateQuestion`` validation drops that
    record and increments ``parse_failures``; the document's other chunks still
    scrape. A chunk that yields >=1 candidate increments ``scraped_count``."""
    candidates: list[CandidateQuestion] = []
    scraped_count = 0
    parse_failures = 0

    for chunk in chunks:
        raw = chat_fn(chunk.content)
        try:
            records = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            parse_failures += 1
            continue
        if not isinstance(records, list):
            parse_failures += 1
            continue

        chunk_candidates: list[CandidateQuestion] = []
        for record in records:
            cand = _coerce_candidate(record, chunk=chunk)
            if cand is None:
                parse_failures += 1
                continue
            chunk_candidates.append(cand)

        if chunk_candidates:
            scraped_count += 1
            candidates.extend(chunk_candidates)

    return ScrapeResult(
        candidates=tuple(candidates),
        scraped_count=scraped_count,
        parse_failures=parse_failures,
    )


async def resolve_or_create_provisional_concept(
    db: AsyncSession,
    *,
    search_space_id: int,
) -> int:
    """Resolve (create once if absent) the per-course provisional-inventory
    concept and return its BIGINT id. The Tier-1 rows hang off this concept so the
    NOT-NULL ``concept_id`` is satisfied at scrape time; the concept is never
    teachable (its rows are tier=1) and carries empty canonical_symbols.

    Course-scoped via ``Subject.search_space_id``; resolves the (first) subject of
    the course and reuses or creates a ``provisional.inventory`` concept under it.
    Idempotent: re-calling returns the SAME concept id."""
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
            slug=f"provisional-{search_space_id}",
            display_name="Provisional inventory",
            search_space_id=search_space_id,
        )
        db.add(subject)
        await db.flush()
        subject_id = int(subject.id)

    concept_id = (
        await db.execute(
            select(Concept.id)
            .where(Concept.subject_id == subject_id)
            .where(Concept.slug == PROVISIONAL_CONCEPT_SLUG)
        )
    ).scalar_one_or_none()
    if concept_id is not None:
        return concept_id

    concept = Concept(
        subject_id=subject_id,
        slug=PROVISIONAL_CONCEPT_SLUG,
        display_name="Provisional inventory",
    )
    db.add(concept)
    await db.flush()
    return int(concept.id)


def _problem_code_for(candidate: CandidateQuestion) -> str:
    """A deterministic, content-derived problem_code so the no-op uses the
    EXISTING ``(concept_id, problem_code)`` uniqueness (migration 018) as the
    idempotency target — NO new index, NO migration. Does NOT embed
    ``aita_chunks.id`` (that would break re-index idempotency)."""
    return f"scrape.{candidate.chunk_content_hash}"


def _tier1_payload(candidate: CandidateQuestion) -> dict:
    """The Tier-1 row payload — a minimal inventory record. NOT a full Problem
    (no reference_solution yet — that is authored at stage-4 tag/mint)."""
    return {
        "id": _problem_code_for(candidate),
        "concept_id": candidate.concept_slug,
        "difficulty": candidate.difficulty,
        "problem_text": candidate.problem_text,
        "given_values": dict(candidate.given_values),
        "target_unknown": candidate.target_unknown,
    }


async def write_tier1_problems(
    db: AsyncSession,
    candidates: Sequence[CandidateQuestion],
    *,
    concept_id: int,
    search_space_id: int,
) -> int:
    """Persist scraped candidates as Tier-1 inventory rows. Returns the number of
    rows ACTUALLY inserted (0 on a full re-run).

    Each row is written with ``tier=1`` EXPLICIT (the safety trap — the ORM
    default is 2/teachable), a content-derived ``problem_code``, denormalized
    ``search_space_id``, and ``provenance={document_id, page, chunk_content_hash}``.
    Idempotency: a candidate whose ``(concept_id, problem_code)`` already exists is
    SKIPPED (the SELECT-then-skip guard — the seed-script ``_upsert_entity``
    pattern; the existing migration-018 ``UNIQUE (concept_id, problem_code)`` is
    not declared in the create_all-built test ORM, so application-level dedup is
    the portable guard). A re-run inserts ZERO rows; dropping the guard would
    duplicate rows (the mutation-discriminating property)."""
    inserted = 0
    for candidate in candidates:
        problem_code = _problem_code_for(candidate)
        existing = (
            await db.execute(
                select(ConceptProblem.id)
                .where(ConceptProblem.concept_id == concept_id)
                .where(ConceptProblem.problem_code == problem_code)
            )
        ).scalar_one_or_none()
        if existing is not None:
            continue

        db.add(
            ConceptProblem(
                concept_id=concept_id,
                problem_code=problem_code,
                difficulty=candidate.difficulty,
                payload=_tier1_payload(candidate),
                tier=1,  # EXPLICIT: never inherit the teachable default (2)
                solution_source=None,
                provenance={
                    "document_id": candidate.document_id,
                    "page": candidate.page,
                    "chunk_content_hash": candidate.chunk_content_hash,
                },
                search_space_id=search_space_id,
            )
        )
        inserted += 1

    await db.flush()
    return inserted
