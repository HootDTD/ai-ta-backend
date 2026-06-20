"""WU-3B2d — scrape (stage 1) tests (Tier-1 unit + real-PG).

Tier-1 ONLY — NO network. The scrape LLM (``chat_fn``) is a DETERMINISTIC
injected stub (a closure returning ``json.dumps([...])`` per chunk); there is NO
real OpenAI / ``cheap_chat`` call anywhere in this module (ADJ #10). Real-PG
tests request the ``db_session`` fixture (re-exported in ``apollo/conftest.py``)
and Docker-skip cleanly when the daemon is down — but the WU-3B2d gate REQUIRES
they run GREEN-not-skipped (like 3B2c).

DISCRIMINATING by design (independent-mutation discipline):
  * ``test_scrape_writes_tier1_rows_explicit`` + ``test_tier1_row_excluded_by_selector``
    RED if the explicit ``tier=1`` is dropped (the ORM default=2 leaks a teachable
    inventory row — the highest-blast-radius bug in the unit).
  * ``test_scrape_rerun_is_noop`` REDs if the SELECT-then-skip idempotency guard
    is reverted to a plain insert (duplicate rows).
  * ``test_chunk_content_hash_is_normalized`` pins the content-hash key.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import pytest
from sqlalchemy import select

from apollo.overseer.problem_selector import list_problems_for_concept
from apollo.persistence.models import Concept, ConceptProblem
from apollo.provisioning.scrape import (
    CandidateQuestion,
    ScrapeResult,
    _chunk_content_hash,
    _normalize,
    resolve_or_create_provisional_concept,
    scrape_questions,
    write_tier1_problems,
)
from apollo.persistence.models import Subject
from database.models import SearchSpace

# pytest.ini sets asyncio_mode = auto.


# --------------------------------------------------------------------------- #
# Stub chunk + stub chat_fn (NO network)
# --------------------------------------------------------------------------- #


@dataclass
class _Chunk:
    """A minimal AITAChunk duck-type: the three attributes scrape reads."""

    content: str
    document_id: int
    page_number: int | None = None


def _well_formed_record(
    *,
    problem_text: str = "Water flows through a pipe; find P2.",
    difficulty: str = "intro",
    concept_slug: str = "bernoulli_principle",
) -> dict:
    return {
        "problem_text": problem_text,
        "given_values": {"P1": 200000.0, "v1": 2.0},
        "target_unknown": "P2",
        "difficulty": difficulty,
        "concept_slug": concept_slug,
    }


def _chat_per_chunk(by_content: dict[str, str]):
    """A cheap_chat-shaped stub returning a fixed JSON string keyed on the chunk
    content (so different chunks get different mocked responses)."""

    def _chat(content, *_a, **_k) -> str:
        return by_content[content]

    return _chat


# --------------------------------------------------------------------------- #
# Pure (no DB) — content hash + parse + fail-soft
# --------------------------------------------------------------------------- #


def test_chunk_content_hash_is_normalized():
    """Two chunks differing only in whitespace/case hash IDENTICALLY; different
    content hashes differently. The idempotency key is content-stable (survives a
    re-index that re-mints chunk ids)."""
    a = _chunk_content_hash("Find  the  PRESSURE P2.")
    b = _chunk_content_hash("find the pressure p2.")
    assert a == b
    c = _chunk_content_hash("a different question entirely")
    assert c != a


def test_chunk_content_hash_is_sha256_hex():
    h = _chunk_content_hash("anything")
    assert len(h) == 64
    assert all(ch in "0123456789abcdef" for ch in h)


def test_normalize_collapses_whitespace():
    assert _normalize("  A   B \n C ") == "a b c"


def test_scrape_parses_candidates():
    """A well-formed JSON array → CandidateQuestions; provenance
    (chunk_content_hash/document_id/page) comes from the CHUNK, not the LLM."""
    chunk = _Chunk(content="Find P2 in the pipe.", document_id=7, page_number=3)
    chat = _chat_per_chunk({chunk.content: json.dumps([_well_formed_record()])})
    result = _run(scrape_questions([chunk], chat_fn=chat))
    assert isinstance(result, ScrapeResult)
    assert result.scraped_count == 1
    assert result.parse_failures == 0
    assert len(result.candidates) == 1
    cand = result.candidates[0]
    assert cand.problem_text == "Water flows through a pipe; find P2."
    assert cand.target_unknown == "P2"
    assert cand.difficulty == "intro"
    assert cand.concept_slug == "bernoulli_principle"
    # provenance from the chunk, not the LLM payload:
    assert cand.document_id == 7
    assert cand.page == 3
    assert cand.chunk_content_hash == _chunk_content_hash(chunk.content)


def test_scrape_malformed_json_is_failsoft():
    """One chunk returns non-JSON → ZERO candidates for it, parse_failures += 1,
    and the OTHER chunk still scrapes. No half-parsed row."""
    good = _Chunk(content="good chunk", document_id=1)
    bad = _Chunk(content="bad chunk", document_id=1)
    chat = _chat_per_chunk(
        {
            good.content: json.dumps([_well_formed_record()]),
            bad.content: "not json at all",
        }
    )
    result = _run(scrape_questions([good, bad], chat_fn=chat))
    assert result.scraped_count == 1  # only the good chunk
    assert result.parse_failures == 1
    assert len(result.candidates) == 1
    assert result.candidates[0].document_id == 1


def test_scrape_non_array_payload_is_failsoft():
    """A JSON object (not an array) is a parse failure, not a crash."""
    chunk = _Chunk(content="obj chunk", document_id=1)
    chat = _chat_per_chunk({chunk.content: json.dumps({"not": "an array"})})
    result = _run(scrape_questions([chunk], chat_fn=chat))
    assert result.candidates == ()
    assert result.parse_failures == 1
    assert result.scraped_count == 0


def test_scrape_difficulty_validated():
    """An LLM difficulty outside {intro,standard,hard} drops that candidate
    (counted in parse_failures), never writing an invalid Tier-1 row."""
    chunk = _Chunk(content="bad difficulty chunk", document_id=2)
    chat = _chat_per_chunk(
        {chunk.content: json.dumps([_well_formed_record(difficulty="trivial")])}
    )
    result = _run(scrape_questions([chunk], chat_fn=chat))
    assert result.candidates == ()
    assert result.parse_failures == 1
    assert result.scraped_count == 0


def test_scrape_drops_one_bad_keeps_one_good_in_same_chunk():
    """A chunk whose array holds one valid + one invalid record keeps the valid
    one and counts the invalid as a parse failure."""
    chunk = _Chunk(content="mixed chunk", document_id=4)
    chat = _chat_per_chunk(
        {
            chunk.content: json.dumps(
                [_well_formed_record(), _well_formed_record(difficulty="impossible")]
            )
        }
    )
    result = _run(scrape_questions([chunk], chat_fn=chat))
    assert len(result.candidates) == 1
    assert result.parse_failures == 1
    assert result.scraped_count == 1


def test_scrape_non_dict_record_is_failsoft():
    """An array element that is not an object (a bare string) drops fail-soft —
    ``_coerce_candidate`` returns None (covers the non-dict guard)."""
    chunk = _Chunk(content="non-dict element chunk", document_id=3)
    chat = _chat_per_chunk(
        {chunk.content: json.dumps(["this is a string, not a record"])}
    )
    result = _run(scrape_questions([chunk], chat_fn=chat))
    assert result.candidates == ()
    assert result.parse_failures == 1
    assert result.scraped_count == 0


def test_candidate_question_requires_fields():
    """CandidateQuestion validates its LLM-sourced fields (min_length etc.)."""
    with pytest.raises(Exception):
        CandidateQuestion(
            problem_text="",  # min_length=1 → invalid
            given_values={},
            target_unknown="P2",
            difficulty="intro",
            document_id=1,
            page=None,
            chunk_content_hash="abc",
            concept_slug="c",
        )


# --------------------------------------------------------------------------- #
# Real-PG helpers
# --------------------------------------------------------------------------- #


def _candidate(
    *,
    document_id: int = 11,
    page: int | None = 2,
    content_hash: str = "hash-aaa",
    concept_slug: str = "bernoulli_principle",
    difficulty: str = "intro",
) -> CandidateQuestion:
    return CandidateQuestion(
        problem_text="Find the downstream pressure P2.",
        given_values={"P1": 200000.0, "v1": 2.0},
        target_unknown="P2",
        difficulty=difficulty,
        document_id=document_id,
        page=page,
        chunk_content_hash=content_hash,
        concept_slug=concept_slug,
    )


async def _seed_course(db, *, slug: str):
    """Seed SearchSpace -> Subject for one course (the provisional concept is
    resolved by the writer). Returns search_space_id."""
    space = SearchSpace(name=f"Course {slug}", slug=slug, subject_name="Physics")
    db.add(space)
    await db.flush()
    subj = Subject(slug=f"s-{slug}", display_name="Sub", search_space_id=space.id)
    db.add(subj)
    await db.flush()
    return space.id


async def _rows_for(db, *, concept_id: int):
    return (
        (
            await db.execute(
                select(ConceptProblem).where(ConceptProblem.concept_id == concept_id)
            )
        )
        .scalars()
        .all()
    )


# --------------------------------------------------------------------------- #
# Real-PG — provisional concept + Tier-1 write + the SAFETY TRAP
# --------------------------------------------------------------------------- #


async def test_provisional_concept_resolved_and_notnull(db_session):
    """The provisional-inventory concept is a real BIGINT, created once;
    re-resolving returns the SAME id."""
    ss_id = await _seed_course(db_session, slug="c-prov")
    cid1 = await resolve_or_create_provisional_concept(
        db_session, search_space_id=ss_id
    )
    cid2 = await resolve_or_create_provisional_concept(
        db_session, search_space_id=ss_id
    )
    assert isinstance(cid1, int)
    assert cid1 == cid2
    concept = (
        await db_session.execute(select(Concept).where(Concept.id == cid1))
    ).scalar_one()
    assert concept.slug == "provisional.inventory"
    # provisional concept carries EMPTY canonical symbols (never teachable signal).
    assert concept.canonical_symbols in (None, {}, {})


async def test_provisional_concept_creates_subject_when_absent(db_session):
    """A course with NO Subject still resolves a provisional concept — the helper
    creates a provisional Subject first (covers the no-subject branch)."""
    space = SearchSpace(name="No-subject course", slug="c-nosubj", subject_name="X")
    db_session.add(space)
    await db_session.flush()
    cid = await resolve_or_create_provisional_concept(
        db_session, search_space_id=space.id
    )
    assert isinstance(cid, int)
    concept = (
        await db_session.execute(select(Concept).where(Concept.id == cid))
    ).scalar_one()
    subj = (
        await db_session.execute(
            select(Subject).where(Subject.id == concept.subject_id)
        )
    ).scalar_one()
    assert subj.search_space_id == space.id


async def test_scrape_writes_tier1_rows_explicit(db_session):
    """After write_tier1_problems: tier == 1 EXPLICIT, provenance carries the
    chunk_content_hash/document_id/page, search_space_id denormalized.
    DISCRIMINATING: dropping the explicit tier=1 → the ORM default=2 makes this
    (and the selector test) RED."""
    ss_id = await _seed_course(db_session, slug="c-write")
    cid = await resolve_or_create_provisional_concept(db_session, search_space_id=ss_id)
    cand = _candidate(content_hash="hash-write-1")
    inserted = await write_tier1_problems(
        db_session, [cand], concept_id=cid, search_space_id=ss_id
    )
    assert inserted == 1
    rows = await _rows_for(db_session, concept_id=cid)
    assert len(rows) == 1
    row = rows[0]
    assert row.tier == 1  # EXPLICIT, not the teachable default
    assert row.provenance["chunk_content_hash"] == "hash-write-1"
    assert row.provenance["document_id"] == cand.document_id
    assert row.provenance["page"] == cand.page
    assert row.search_space_id == ss_id
    assert row.problem_code == "scrape.hash-write-1"


async def test_tier1_row_excluded_by_selector(db_session):
    """THE SAFETY TRAP. A scraped Tier-1 row is EXCLUDED by
    ``list_problems_for_concept`` (the tier-2 gate); after flipping it to tier=2 it
    IS returned. Proves un-linted scraped inventory is never teachable."""
    ss_id = await _seed_course(db_session, slug="c-trap")
    cid = await resolve_or_create_provisional_concept(db_session, search_space_id=ss_id)
    # A Tier-1 row needs a Problem-validatable payload to be returnable post-flip;
    # write one then enrich its payload to a full Problem so the post-flip read
    # parses (the selector validates payload through Problem.model_validate).
    cand = _candidate(content_hash="hash-trap-1")
    await write_tier1_problems(db_session, [cand], concept_id=cid, search_space_id=ss_id)
    rows = await _rows_for(db_session, concept_id=cid)
    assert len(rows) == 1
    row = rows[0]
    # give it a Problem-validatable payload so post-flip selection can parse it.
    row.payload = {
        "id": row.problem_code,
        "concept_id": "bernoulli_principle",
        "difficulty": "intro",
        "problem_text": "Find P2.",
        "given_values": {"P1": 200000.0},
        "target_unknown": "P2",
        "reference_solution": [
            {
                "step": 1,
                "entry_type": "equation",
                "id": "eq1",
                "content": {"symbolic": "P1 - P2"},
                "depends_on": [],
            }
        ],
    }
    await db_session.flush()

    # Tier-1 → excluded.
    assert await list_problems_for_concept(db_session, concept_id=cid) == []

    # Flip to tier=2 → now returned.
    row.tier = 2
    await db_session.flush()
    teachable = await list_problems_for_concept(db_session, concept_id=cid)
    assert len(teachable) == 1
    assert teachable[0].id == row.problem_code


async def test_scrape_rerun_is_noop(db_session):
    """IDEMPOTENCY. write_tier1_problems twice with the same candidate → the
    second inserts 0 and the row count is unchanged. MUTATION-DISCRIMINATING:
    reverting the SELECT-then-skip guard to a plain insert duplicates the row."""
    ss_id = await _seed_course(db_session, slug="c-rerun")
    cid = await resolve_or_create_provisional_concept(db_session, search_space_id=ss_id)
    cand = _candidate(content_hash="hash-rerun-1")
    first = await write_tier1_problems(
        db_session, [cand], concept_id=cid, search_space_id=ss_id
    )
    second = await write_tier1_problems(
        db_session, [cand], concept_id=cid, search_space_id=ss_id
    )
    assert first == 1
    assert second == 0
    rows = await _rows_for(db_session, concept_id=cid)
    assert len(rows) == 1


async def test_scrape_rerun_after_reindex_is_noop(db_session):
    """The content-hash key survives a re-index (OPS-2): a second write with a
    DIFFERENT document_id but the SAME chunk_content_hash still no-ops."""
    ss_id = await _seed_course(db_session, slug="c-reidx")
    cid = await resolve_or_create_provisional_concept(db_session, search_space_id=ss_id)
    cand_a = _candidate(document_id=100, content_hash="hash-shared")
    cand_b = _candidate(document_id=999, content_hash="hash-shared")  # re-indexed
    await write_tier1_problems(db_session, [cand_a], concept_id=cid, search_space_id=ss_id)
    second = await write_tier1_problems(
        db_session, [cand_b], concept_id=cid, search_space_id=ss_id
    )
    assert second == 0
    rows = await _rows_for(db_session, concept_id=cid)
    assert len(rows) == 1


async def test_scrape_writes_multiple_distinct(db_session):
    """Two candidates with DIFFERENT content hashes write two distinct rows."""
    ss_id = await _seed_course(db_session, slug="c-multi")
    cid = await resolve_or_create_provisional_concept(db_session, search_space_id=ss_id)
    cands = [
        _candidate(content_hash="hash-m1"),
        _candidate(content_hash="hash-m2"),
    ]
    inserted = await write_tier1_problems(
        db_session, cands, concept_id=cid, search_space_id=ss_id
    )
    assert inserted == 2
    rows = await _rows_for(db_session, concept_id=cid)
    assert len(rows) == 2


# --------------------------------------------------------------------------- #
# Tiny sync runner for the pure async scrape tests (scrape_questions is async but
# touches no DB; run it on a throwaway loop so the pure tests stay mark-free).
# --------------------------------------------------------------------------- #


def _run(coro):
    import asyncio

    return asyncio.run(coro)


# --------------------------------------------------------------------------- #
# Public-API re-export surface (the package-level import paths apollo.md advertises)
# --------------------------------------------------------------------------- #


def test_scrape_public_api_reexport():
    """``from apollo.provisioning import scrape_questions, write_tier1_problems,
    CandidateQuestion, ScrapeResult`` returns the SAME objects as the ``scrape``
    module — the package-level paths apollo.md documents must resolve.
    DISCRIMINATING: drop a re-export from ``apollo/provisioning/__init__.py`` and
    this REDs."""
    from apollo.provisioning import (
        CandidateQuestion as ReexportCandidateQuestion,
        ScrapeResult as ReexportScrapeResult,
        scrape_questions as reexport_scrape_questions,
        write_tier1_problems as reexport_write_tier1_problems,
    )
    from apollo.provisioning import scrape as scrape_mod

    assert ReexportCandidateQuestion is scrape_mod.CandidateQuestion
    assert ReexportScrapeResult is scrape_mod.ScrapeResult
    assert reexport_scrape_questions is scrape_mod.scrape_questions
    assert reexport_write_tier1_problems is scrape_mod.write_tier1_problems
