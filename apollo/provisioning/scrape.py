"""WU-3B2d stage 1 — LLM scrape → Tier-1 ``apollo_concept_problems`` inventory.

``scrape_questions`` runs ONE injected ``chat_fn`` pass over a document's already-
embedded ``DocumentChunk`` rows and parses each chunk's JSON into typed
``CandidateQuestion`` records (provenance + LLM difficulty + problem fields).
``write_tier1_problems`` persists those candidates as **Tier-1 inventory** — rows
that are explicitly ``tier=1`` and therefore NOT teachable (the §8B safety trap:
the ORM ``tier`` default is 2, so an omitted explicit value would silently make
scraped inventory selectable).

Idempotency key: structured scraping stamps each candidate from the stable document
id plus a CONTENT hash of its own normalized problem text, NOT a section ordinal or
the volatile ``internal.document_chunks.id``. The composite is folded into a deterministic
``problem_code`` and the writer SKIPS a row whose ``(concept_id, problem_code)``
already exists (an identical re-run inserts ZERO rows). The provisional-inventory
concept (slug ``provisional.inventory``, created once per ``(subject,
search_space)``) satisfies the NOT-NULL ``concept_id`` at scrape time; stage-4
``tag_and_mint`` re-homes a promoted problem onto its real tagged concept.

Failure mode (per chunk, fail-SOFT): a malformed/empty LLM JSON, or a candidate
that fails ``CandidateQuestion`` validation (e.g. an out-of-range difficulty),
yields ZERO candidates for that chunk and increments ``parse_failures`` — never a
half-parsed row. A DB error raises to the caller's transaction (3B2g owns
commit/rollback). NO network: ``chat_fn`` is injected (mocked in Tier-1).
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from database.models import DocumentChunk

from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apollo.persistence.models import Concept, ConceptProblem, Subject
from apollo.provisioning.cost_constants import APOLLO_SCRAPE_SECTION_CHAR_CAP
from apollo.provisioning.section_grouping import Section, group_into_sections, section_content_hash
from apollo.provisioning.section_triage import triage_sections
from apollo.schemas.problem import Difficulty

_LOG = logging.getLogger(__name__)

__all__ = [
    "CandidateQuestion",
    "ScrapeResult",
    "chunk_content_hash",
    "scrape_document",
    "scrape_questions",
    "scrape_section",
    "write_tier1_problems",
]

# The reserved per-course inventory concept slug (the SEAM resolution: a Tier-1
# row needs a NOT-NULL concept_id before stage-4 tagging exists).
PROVISIONAL_CONCEPT_SLUG = "provisional.inventory"
_SECTION_CHAR_OVERLAP = 200


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
    label: str | None = None  # printed problem label/number, e.g. "Problem 3" (WU-AAS)


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
    chunk: DocumentChunk,
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
            label=(str(raw.get("label")).strip() or None) if raw.get("label") else None,
        )
    except ValidationError:
        return None


async def scrape_questions(
    chunks: Sequence[DocumentChunk],
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


def _coerce_section_candidate(raw: Any, *, section, concept_hint: str) -> CandidateQuestion | None:
    """Build a CandidateQuestion from one LLM record scraped from a whole SECTION.
    Provenance (document_id/page) comes from the SECTION; concept_slug falls back to
    the triage hint then the provisional concept. ``chunk_content_hash`` is a
    placeholder here — ``scrape_section`` re-stamps it with the content-derived
    ``<document_id>.q<problem_text_hash32>`` key after deterministic ordering.
    Returns None (fail-soft) on any validation error."""
    if not isinstance(raw, dict):
        return None
    try:
        return CandidateQuestion(
            problem_text=raw.get("problem_text", ""),
            given_values=raw.get("given_values", {}),
            target_unknown=raw.get("target_unknown", ""),
            difficulty=raw.get("difficulty", ""),
            document_id=int(section.document_id),
            page=section.page_start,
            chunk_content_hash="0",  # placeholder; re-stamped in scrape_section
            concept_slug=(raw.get("concept_slug") or concept_hint or PROVISIONAL_CONCEPT_SLUG),
            label=(str(raw.get("label")).strip() or None) if raw.get("label") else None,
        )
    except (ValidationError, ValueError, TypeError):
        return None


def scrape_section(
    section, *, concept_hint: str, chat_fn: Callable[..., str]
) -> tuple[list[CandidateQuestion], int]:
    """Scrape one whole section via a single ``chat_fn`` pass. Returns
    ``(candidates, parse_failures)``. Fail-soft: malformed JSON / a non-array / an
    invalid candidate drops that record and increments ``parse_failures``. Each
    surviving candidate's ``chunk_content_hash`` is stamped
    ``'<document_id>.q<problem_text_hash32>'``. Candidates remain sorted by the
    content hash of normalized ``problem_text`` for stable output order, while the
    key itself is order-independent across LLM replays."""
    raw = chat_fn(section.text)
    try:
        records = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return [], 1
    if not isinstance(records, list):
        return [], 1

    built: list[CandidateQuestion] = []
    failures = 0
    for record in records:
        cand = _coerce_section_candidate(record, section=section, concept_hint=concept_hint)
        if cand is None:
            failures += 1
            continue
        built.append(cand)

    built.sort(key=lambda c: chunk_content_hash(c.problem_text))
    finalized = [
        c.model_copy(
            update={
                # Content-derived, order-INDEPENDENT key: document scope + the
                # question's own text hash. NOT the section ordinal — the LLM is
                # not order-stable across runs, so a positional key re-binds old
                # rows to different questions (the prod cross-run misalignment).
                "chunk_content_hash": (
                    f"{c.document_id}.q{chunk_content_hash(c.problem_text)[:32]}"
                )
            }
        )
        for c in built
    ]
    return finalized, failures


def _section_window(
    section: Section,
    *,
    text: str,
    member_chunk_ids: Sequence[int],
    pages: Sequence[int],
) -> Section:
    return Section(
        title=section.title,
        document_id=section.document_id,
        page_start=min(pages) if pages else section.page_start,
        page_end=max(pages) if pages else section.page_end,
        text=text,
        source_content_hash=section_content_hash(text or section.title),
        member_chunk_ids=tuple(member_chunk_ids),
    )


def _character_windows(section: Section, *, char_cap: int) -> list[Section]:
    overlap = min(_SECTION_CHAR_OVERLAP, max(0, char_cap // 4))
    step = max(1, char_cap - overlap)
    windows: list[Section] = []
    start = 0
    while start < len(section.text):
        text = section.text[start : start + char_cap]
        windows.append(
            _section_window(
                section,
                text=text,
                member_chunk_ids=section.member_chunk_ids,
                pages=(),
            )
        )
        if start + char_cap >= len(section.text):
            break
        start += step
    return windows


def _split_oversized_section(
    section: Section,
    *,
    rows_by_id: dict[int, Any],
    char_cap: int,
) -> list[Section]:
    if len(section.text) <= char_cap:
        return [section]

    member_rows = [
        rows_by_id[chunk_id]
        for chunk_id in section.member_chunk_ids
        if chunk_id in rows_by_id
    ]
    body_rows = [
        row
        for row in member_rows
        if getattr(row, "chunk_type", None) != "heading"
        and str(getattr(row, "content", "") or "").strip()
    ]
    if not body_rows or any(getattr(row, "page_number", None) is None for row in body_rows):
        return _character_windows(section, char_cap=char_cap)

    pages: list[tuple[int, str, tuple[int, ...]]] = []
    for row in body_rows:
        page = int(row.page_number)
        content = str(row.content).strip()
        row_id = int(row.id)
        if pages and pages[-1][0] == page:
            old_page, old_text, old_ids = pages[-1]
            pages[-1] = (old_page, f"{old_text}\n{content}", (*old_ids, row_id))
        else:
            pages.append((page, content, (row_id,)))

    windows: list[Section] = []
    current_pages: list[int] = []
    current_texts: list[str] = []
    current_ids: list[int] = []

    def _flush() -> None:
        if not current_texts:
            return
        windows.append(
            _section_window(
                section,
                text="\n".join(current_texts),
                member_chunk_ids=current_ids,
                pages=current_pages,
            )
        )
        current_pages.clear()
        current_texts.clear()
        current_ids.clear()

    for page, page_text, page_ids in pages:
        if len(page_text) > char_cap:
            _flush()
            page_section = _section_window(
                section,
                text=page_text,
                member_chunk_ids=page_ids,
                pages=(page,),
            )
            windows.extend(_character_windows(page_section, char_cap=char_cap))
            continue
        combined_length = len(page_text) + sum(len(text) for text in current_texts)
        combined_length += len(current_texts)  # joining newlines
        if current_texts and combined_length > char_cap:
            _flush()
        current_pages.append(page)
        current_texts.append(page_text)
        current_ids.extend(page_ids)
    _flush()
    return windows


def _split_oversized_sections(
    sections: Sequence[Section], chunk_rows: Sequence, *, char_cap: int
) -> list[Section]:
    if char_cap <= 0:
        return list(sections)
    rows_by_id = {int(row.id): row for row in chunk_rows}
    windows: list[Section] = []
    for section in sections:
        windows.extend(
            _split_oversized_section(
                section,
                rows_by_id=rows_by_id,
                char_cap=char_cap,
            )
        )
    return windows


async def scrape_document(
    chunk_rows: Sequence,
    *,
    chat_fn: Callable[..., str],
    triage_chat_fn: Callable[..., str],
    max_sections: int,
    min_candidates: int,
    structured: bool = True,
    section_char_cap: int = APOLLO_SCRAPE_SECTION_CHAR_CAP,
) -> ScrapeResult:
    """Structure-aware stage-1 scrape. Reconstructs sections, triages them once, then
    scrapes problem-likely sections first; a NOT-likely section is scraped only while
    candidates remain under ``min_candidates`` (the bounded exhaustive fallback), and
    no more than ``max_sections`` sections are scraped per document. Before triage,
    every section over ``section_char_cap`` is split on page boundaries, with
    overlapping character windows as the no-page/single-oversized-page fallback.

    When ``structured`` is False, delegates to the legacy per-chunk
    ``scrape_questions`` (the ``APOLLO_STRUCTURED_SCRAPE`` revert path)."""
    if not structured:
        return await scrape_questions(chunk_rows, chat_fn=chat_fn)

    sections = _split_oversized_sections(
        group_into_sections(chunk_rows),
        chunk_rows,
        char_cap=section_char_cap,
    )
    if not sections:
        return ScrapeResult(candidates=(), scraped_count=0, parse_failures=0)

    verdicts = triage_sections(sections, chat_fn=triage_chat_fn)
    # problem-likely first, then by priority desc, then original order (stable).
    order = sorted(
        range(len(verdicts)),
        key=lambda i: (not verdicts[i].is_problem_likely, -verdicts[i].priority, i),
    )

    candidates: list[CandidateQuestion] = []
    scraped_count = 0
    parse_failures = 0
    scraped_sections = 0

    for i in order:
        if scraped_sections >= max_sections:
            break
        verdict = verdicts[i]
        # Fallback gate: once the problem-likely sections are exhausted, only keep
        # widening into NOT-likely sections while we are still below min_candidates.
        if not verdict.is_problem_likely and len(candidates) >= min_candidates:
            break
        section_cands, failures = scrape_section(
            verdict.section, concept_hint=verdict.concept_slug, chat_fn=chat_fn
        )
        scraped_sections += 1
        parse_failures += failures
        if section_cands:
            scraped_count += 1
            candidates.extend(section_cands)

    # Same-key candidates (an identical question scraped from two overlapping
    # windows) map to ONE tier-1 row — keep the first so the row is processed once.
    seen: set[str] = set()
    deduped = [
        c
        for c in candidates
        if not (c.chunk_content_hash in seen or seen.add(c.chunk_content_hash))
    ]
    candidates = deduped

    _LOG.info(
        "provisioning_scrape_document",
        extra={
            "event": "provisioning_scrape_document",
            "sections_total": len(sections),
            "sections_scraped": scraped_sections,
            "candidates": len(candidates),
            "parse_failures": parse_failures,
        },
    )
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
    """A deterministic ``scrape.<candidate key>`` problem_code — for the structured
    path ``scrape.<document_id>.q<problem_text_hash32>``; the legacy per-chunk path
    still stamps the plain chunk content hash.

    The document-scoped content key uses the EXISTING ``(concept_id,
    problem_code)`` uniqueness (migration 018) as the idempotency target — NO new
    index or migration. It does not embed ``internal.document_chunks.id``: a re-index may re-mint
    chunks, while the document row and normalized question text remain stable."""
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
