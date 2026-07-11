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
from apollo.persistence.models import Concept, ConceptProblem, Subject
from apollo.provisioning.problem_hash import problem_dup_hash
from apollo.provisioning.scrape import (
    CandidateQuestion,
    ScrapeResult,
    _normalize,
    _split_oversized_sections,
    chunk_content_hash,
    resolve_or_create_provisional_concept,
    scrape_document,
    scrape_questions,
    scrape_section,
    write_tier1_problems,
)
from apollo.provisioning.section_grouping import Section, group_into_sections
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
    label: str | None = None,
) -> dict:
    record = {
        "problem_text": problem_text,
        "given_values": {"P1": 200000.0, "v1": 2.0},
        "target_unknown": "P2",
        "difficulty": difficulty,
        "concept_slug": concept_slug,
    }
    if label is not None:
        record["label"] = label
    return record


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
    a = chunk_content_hash("Find  the  PRESSURE P2.")
    b = chunk_content_hash("find the pressure p2.")
    assert a == b
    c = chunk_content_hash("a different question entirely")
    assert c != a


def test_chunk_content_hash_is_sha256_hex():
    h = chunk_content_hash("anything")
    assert len(h) == 64
    assert all(ch in "0123456789abcdef" for ch in h)


def test_normalize_collapses_whitespace():
    assert _normalize("  A   B \n C ") == "a b c"


def test_scrape_parses_candidates():
    """A well-formed JSON array → CandidateQuestions; provenance
    (chunk_content_hash/document_id/page) comes from the CHUNK, not the LLM."""
    chunk = _Chunk(content="Find P2 in the pipe.", document_id=7, page_number=3)
    chat = _chat_per_chunk({chunk.content: json.dumps([_well_formed_record(label="Problem 3")])})
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
    assert cand.label == "Problem 3"
    # provenance from the chunk, not the LLM payload:
    assert cand.document_id == 7
    assert cand.page == 3
    assert cand.chunk_content_hash == chunk_content_hash(chunk.content)


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
    chat = _chat_per_chunk({chunk.content: json.dumps([_well_formed_record(difficulty="trivial")])})
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
    chat = _chat_per_chunk({chunk.content: json.dumps(["this is a string, not a record"])})
    result = _run(scrape_questions([chunk], chat_fn=chat))
    assert result.candidates == ()
    assert result.parse_failures == 1
    assert result.scraped_count == 0


def test_candidate_question_requires_fields():
    """CandidateQuestion validates its LLM-sourced fields (min_length etc.)."""
    with pytest.raises(Exception):  # noqa: B017
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


def test_candidate_question_accepts_optional_label():
    q = CandidateQuestion(
        problem_text="A beam of length L...",
        given_values={"L": 2.0},
        target_unknown="M",
        difficulty="standard",
        document_id=7,
        page=3,
        chunk_content_hash="a" * 64,
        concept_slug="provisional.inventory",
        label="Problem 3",
    )
    assert q.label == "Problem 3"

    q2 = CandidateQuestion(
        problem_text="x",
        given_values={},
        target_unknown="y",
        difficulty="intro",
        document_id=1,
        page=None,
        chunk_content_hash="b" * 64,
        concept_slug="provisional.inventory",
    )
    assert q2.label is None


# --------------------------------------------------------------------------- #
# Stage-1 prompt↔parser contract (the missing un-mocked-class test). PURE, no DB.
# --------------------------------------------------------------------------- #


def test_scrape_prompt_declares_candidate_question_fields():
    """The ``_SCRAPE_SYSTEM_PROMPT`` declares every LLM-SUPPLIED ``CandidateQuestion``
    field. The chunk-stamped provenance fields (``document_id``/``page``/
    ``chunk_content_hash`` — stamped from the CHUNK in ``_coerce_candidate``, NOT the
    LLM) are excluded; the minus-set is derived from that function's chunk-stamped
    args so it stays honest with the model. DISCRIMINATING: reverting the prompt to
    the vague one-liner (no field names) RED-flags."""
    from apollo.provisioning.orchestrator import _SCRAPE_SYSTEM_PROMPT

    # The fields _coerce_candidate stamps from the chunk (scrape.py:112-122) — the
    # LLM never supplies these, so the prompt does not declare them.
    chunk_stamped = {"document_id", "page", "chunk_content_hash"}
    llm_supplied = set(CandidateQuestion.model_fields) - chunk_stamped
    assert llm_supplied == {
        "problem_text",
        "given_values",
        "target_unknown",
        "difficulty",
        "concept_slug",
        "label",
    }
    for field in llm_supplied:
        assert field in _SCRAPE_SYSTEM_PROMPT, field


# --------------------------------------------------------------------------- #
# scrape_section + scrape_document (pure, no DB)
# --------------------------------------------------------------------------- #


def _section(
    *, title="6.2 Exercises", text="Find P2 in the pipe.", doc=7, page=3, shash="a" * 64
) -> Section:
    return Section(
        title=title,
        document_id=doc,
        page_start=page,
        page_end=page,
        text=text,
        source_content_hash=shash,
        member_chunk_ids=(1, 2),
    )


def test_scrape_section_stamps_section_scoped_hash():
    """A section yielding two problems stamps chunk_content_hash =
    '<section_hash>.<ordinal>' with a DETERMINISTIC ordinal (sorted by problem_text
    hash), and provenance (document_id/page) comes from the SECTION."""
    sec = _section(shash="b" * 64, doc=7, page=3)
    chat = lambda _text: json.dumps(  # noqa: E731
        [
            _well_formed_record(problem_text="Zebra problem find P2."),
            _well_formed_record(problem_text="Apple problem find P2."),
        ]
    )
    cands, failures = scrape_section(sec, concept_hint="fluids", chat_fn=chat)
    assert failures == 0
    assert len(cands) == 2
    assert {c.chunk_content_hash for c in cands} == {f"{'b' * 64}.0", f"{'b' * 64}.1"}
    assert all(c.document_id == 7 for c in cands)
    assert all(c.page == 3 for c in cands)
    # ordinal is deterministic: re-running yields the SAME hash↔problem mapping
    cands2, _ = scrape_section(sec, concept_hint="fluids", chat_fn=chat)
    map1 = {c.problem_text: c.chunk_content_hash for c in cands}
    map2 = {c.problem_text: c.chunk_content_hash for c in cands2}
    assert map1 == map2


def test_scrape_section_uses_concept_hint_when_llm_omits():
    sec = _section()
    rec = _well_formed_record(label="Q7")
    del rec["concept_slug"]
    cands, _ = scrape_section(sec, concept_hint="integration", chat_fn=lambda _t: json.dumps([rec]))
    assert cands[0].concept_slug == "integration"
    assert cands[0].label == "Q7"


def test_scrape_section_failsoft_on_bad_json():
    sec = _section()
    cands, failures = scrape_section(sec, concept_hint="c", chat_fn=lambda _t: "not json")
    assert cands == []
    assert failures == 1


def test_scrape_document_groups_triages_and_scrapes():
    """End-to-end (mocked): two body chunks under one heading → one section →
    triage marks it likely → section scrape yields a candidate."""

    @dataclass
    class _Row:
        id: int
        content: str
        document_id: int = 5
        page_number: int | None = 1
        section_path: str | None = None
        chunk_type: str | None = "body"

    rows = [
        _Row(id=1, content="6.2 Exercises", chunk_type="heading"),
        _Row(id=2, content="A region bounded by curves."),
        _Row(id=3, content="Find the area."),
    ]

    def triage(_p):
        return json.dumps(
            [
                {
                    "index": 0,
                    "is_problem_likely": True,
                    "priority": 5,
                    "concept_slug": "area",
                    "concept_display": "Area",
                }
            ]
        )

    def scrape(_text):
        return json.dumps([_well_formed_record(concept_slug="area")])

    result = _run(
        scrape_document(
            rows,
            chat_fn=scrape,
            triage_chat_fn=triage,
            max_sections=120,
            min_candidates=3,
        )
    )
    assert isinstance(result, ScrapeResult)
    assert result.scraped_count == 1
    assert len(result.candidates) == 1
    assert result.candidates[0].concept_slug == "area"
    # the section-scoped key namespace
    assert result.candidates[0].chunk_content_hash.endswith(".0")


def test_scrape_document_respects_max_sections():
    """max_sections caps the number of sections scraped (cost bound)."""

    @dataclass
    class _Row:
        id: int
        content: str
        document_id: int = 5
        page_number: int | None = 1
        section_path: str | None = None
        chunk_type: str | None = "heading"

    # 3 single-heading sections; cap at 1 → only one section is scraped.
    rows = [_Row(id=i, content=f"Section {i}") for i in range(3)]
    calls = {"n": 0}

    def _scrape(_text):
        calls["n"] += 1
        return json.dumps([_well_formed_record()])

    triage = lambda _p: json.dumps(  # noqa: E731
        [{"index": i, "is_problem_likely": True, "priority": 0} for i in range(3)]
    )
    _run(
        scrape_document(
            rows, chat_fn=_scrape, triage_chat_fn=triage, max_sections=1, min_candidates=99
        )
    )
    assert calls["n"] == 1  # capped


def test_scrape_document_fallback_widens_when_thin():
    """A section triaged NOT-likely is still scraped when candidates < MIN
    (the exhaustive fallback), but skipped once MIN is met."""

    @dataclass
    class _Row:
        id: int
        content: str
        document_id: int = 5
        page_number: int | None = 1
        section_path: str | None = None
        chunk_type: str | None = "heading"

    rows = [_Row(id=1, content="Likely"), _Row(id=2, content="Unlikely")]
    scraped_titles = []

    def _scrape(text):
        scraped_titles.append(text)
        return "[]"  # no candidates anywhere → stays under MIN → fallback widens

    triage = lambda _p: json.dumps(  # noqa: E731
        [
            {"index": 0, "is_problem_likely": True, "priority": 5},
            {"index": 1, "is_problem_likely": False, "priority": 0},
        ]
    )
    _run(
        scrape_document(
            rows, chat_fn=_scrape, triage_chat_fn=triage, max_sections=120, min_candidates=3
        )
    )
    # both the likely AND the unlikely section were scraped (widened, still thin)
    assert len(scraped_titles) == 2


def test_scrape_document_stops_widening_once_min_met():
    """The complementary half of the fallback contract: once candidates reach
    min_candidates, a NOT-likely section is NOT scraped (the cost-bound gate).
    DISCRIMINATING: flipping the gate to `continue`/`>`/removing the break would
    scrape the unlikely section too and fail this."""

    @dataclass
    class _Row:
        id: int
        content: str
        document_id: int = 5
        page_number: int | None = 1
        section_path: str | None = None
        chunk_type: str | None = "heading"

    rows = [_Row(id=1, content="Likely"), _Row(id=2, content="Unlikely")]
    scrape_calls = {"n": 0}

    def _scrape(_text):
        scrape_calls["n"] += 1
        return json.dumps([_well_formed_record()])  # each scraped section yields 1 candidate

    triage = lambda _p: json.dumps(  # noqa: E731
        [
            {"index": 0, "is_problem_likely": True, "priority": 5},
            {"index": 1, "is_problem_likely": False, "priority": 0},
        ]
    )
    result = _run(
        scrape_document(
            rows, chat_fn=_scrape, triage_chat_fn=triage, max_sections=120, min_candidates=1
        )
    )
    # the likely section met min_candidates=1, so the NOT-likely section is gated out
    assert scrape_calls["n"] == 1
    assert result.scraped_count == 1
    assert len(result.candidates) == 1


def test_scrape_document_structured_false_uses_legacy_per_chunk():
    """structured=False routes to the legacy per-chunk scrape_questions path."""

    @dataclass
    class _Row:
        id: int
        content: str
        document_id: int = 5
        page_number: int | None = 1
        section_path: str | None = None
        chunk_type: str | None = "body"

    rows = [_Row(id=1, content="legacy chunk")]
    chat = _chat_per_chunk({"legacy chunk": json.dumps([_well_formed_record()])})
    result = _run(
        scrape_document(
            rows,
            chat_fn=chat,
            triage_chat_fn=lambda _p: "[]",
            max_sections=120,
            min_candidates=3,
            structured=False,
        )
    )
    assert result.scraped_count == 1
    # legacy path stamps the CHUNK content hash (no ".ordinal" section suffix)
    assert result.candidates[0].chunk_content_hash == chunk_content_hash("legacy chunk")


def test_scrape_document_splits_oversized_section_and_overlap_dedups_downstream():
    """A flat 6k section is scraped in overlapping windows. Every question is
    recovered; the overlap-straddling one has one downstream problem hash."""

    @dataclass
    class _Row:
        id: int
        content: str
        document_id: int = 5
        page_number: int | None = None
        section_path: str | None = None
        chunk_type: str | None = "body"

    chars = ["x"] * 6000
    markers = {
        500: "[QUESTION_ONE]",
        2400: "[OVERLAP_QUESTION]",
        3500: "[QUESTION_TWO]",
        5500: "[QUESTION_THREE]",
    }
    for offset, marker in markers.items():
        chars[offset : offset + len(marker)] = marker
    rows = [_Row(id=1, content="".join(chars))]
    scrape_calls: list[str] = []

    def _scrape(text: str) -> str:
        scrape_calls.append(text)
        records = []
        for marker in markers.values():
            if marker in text:
                records.append(_well_formed_record(problem_text=f"Recover {marker}"))
        return json.dumps(records)

    result = _run(
        scrape_document(
            rows,
            chat_fn=_scrape,
            triage_chat_fn=lambda _payload: "not-json",
            max_sections=120,
            min_candidates=99,
            section_char_cap=2500,
        )
    )

    assert len(scrape_calls) == 3
    assert {candidate.problem_text for candidate in result.candidates} == {
        f"Recover {marker}" for marker in markers.values()
    }
    overlap = [
        candidate
        for candidate in result.candidates
        if candidate.problem_text == "Recover [OVERLAP_QUESTION]"
    ]
    assert len(overlap) == 2
    deduped = {problem_dup_hash(candidate): candidate for candidate in result.candidates}
    assert len(deduped) == 4


def test_scrape_document_page_splits_any_oversized_section():
    """The guard applies to an oversized section even when another section is
    present, and keeps complete pages together instead of introducing overlap."""

    @dataclass
    class _Row:
        id: int
        content: str
        page_number: int
        chunk_type: str = "body"
        document_id: int = 5
        section_path: str | None = None

    rows = [
        _Row(id=1, content="1. Short", page_number=1, chunk_type="heading"),
        _Row(id=2, content="short body", page_number=1),
        _Row(id=3, content="2. Large", page_number=2, chunk_type="heading"),
        _Row(id=4, content="A" * 1400, page_number=2),
        _Row(id=5, content="B" * 1400, page_number=3),
    ]
    scrape_calls: list[str] = []

    def _scrape(text: str) -> str:
        scrape_calls.append(text)
        return "[]"

    _run(
        scrape_document(
            rows,
            chat_fn=_scrape,
            triage_chat_fn=lambda _payload: "not-json",
            max_sections=120,
            min_candidates=99,
            section_char_cap=2000,
        )
    )

    assert scrape_calls == ["short body", "A" * 1400, "B" * 1400]


def test_split_oversized_section_aggregates_rows_and_windows_a_single_large_page():
    """Rows from one page stay together before an over-cap page falls back to
    overlapping character windows; a following small page remains intact."""

    @dataclass
    class _Row:
        id: int
        content: str
        page_number: int
        chunk_type: str = "body"
        document_id: int = 5
        section_path: str | None = None

    rows = [
        _Row(id=1, content="A" * 70, page_number=1),
        _Row(id=2, content="B" * 70, page_number=1),
        _Row(id=3, content="C" * 20, page_number=2),
    ]
    sections = group_into_sections(rows)

    windows = _split_oversized_sections(sections, rows, char_cap=100)

    assert [len(window.text) for window in windows] == [100, 66, 20]
    assert [(window.page_start, window.page_end) for window in windows] == [
        (1, 1),
        (1, 1),
        (2, 2),
    ]
    assert [window.member_chunk_ids for window in windows] == [(1, 2), (1, 2), (3,)]


def test_split_oversized_sections_disabled_returns_original_sections():
    section = _section(text="A" * 100)

    assert _split_oversized_sections([section], [], char_cap=0) == [section]


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
        difficulty=difficulty,  # type: ignore[arg-type]
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
        (await db.execute(select(ConceptProblem).where(ConceptProblem.concept_id == concept_id)))
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
    cid1 = await resolve_or_create_provisional_concept(db_session, search_space_id=ss_id)
    cid2 = await resolve_or_create_provisional_concept(db_session, search_space_id=ss_id)
    assert isinstance(cid1, int)
    assert cid1 == cid2
    concept = (await db_session.execute(select(Concept).where(Concept.id == cid1))).scalar_one()
    assert concept.slug == "provisional.inventory"
    # provisional concept carries EMPTY canonical symbols (never teachable signal).
    assert concept.canonical_symbols in (None, {}, {})


async def test_provisional_concept_creates_subject_when_absent(db_session):
    """A course with NO Subject still resolves a provisional concept — the helper
    creates a provisional Subject first (covers the no-subject branch)."""
    space = SearchSpace(name="No-subject course", slug="c-nosubj", subject_name="X")
    db_session.add(space)
    await db_session.flush()
    cid = await resolve_or_create_provisional_concept(db_session, search_space_id=space.id)
    assert isinstance(cid, int)
    concept = (await db_session.execute(select(Concept).where(Concept.id == cid))).scalar_one()
    subj = (
        await db_session.execute(select(Subject).where(Subject.id == concept.subject_id))
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
    inserted = await write_tier1_problems(db_session, [cand], concept_id=cid, search_space_id=ss_id)
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
    first = await write_tier1_problems(db_session, [cand], concept_id=cid, search_space_id=ss_id)
    second = await write_tier1_problems(db_session, [cand], concept_id=cid, search_space_id=ss_id)
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
    second = await write_tier1_problems(db_session, [cand_b], concept_id=cid, search_space_id=ss_id)
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
    inserted = await write_tier1_problems(db_session, cands, concept_id=cid, search_space_id=ss_id)
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
    )
    from apollo.provisioning import (
        ScrapeResult as ReexportScrapeResult,
    )
    from apollo.provisioning import scrape as scrape_mod
    from apollo.provisioning import (
        scrape_questions as reexport_scrape_questions,
    )
    from apollo.provisioning import (
        write_tier1_problems as reexport_write_tier1_problems,
    )

    assert ReexportCandidateQuestion is scrape_mod.CandidateQuestion
    assert ReexportScrapeResult is scrape_mod.ScrapeResult
    assert reexport_scrape_questions is scrape_mod.scrape_questions
    assert reexport_write_tier1_problems is scrape_mod.write_tier1_problems
