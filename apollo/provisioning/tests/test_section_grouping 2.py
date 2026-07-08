"""Phase-2 section reconstruction tests. PURE — no DB, no LLM, no network."""

from __future__ import annotations

from dataclasses import dataclass

from apollo.provisioning.section_grouping import (
    group_into_sections,
)


@dataclass
class _Row:
    """Minimal aita_chunks duck-type: the attributes grouping reads."""

    id: int
    content: str
    document_id: int = 1
    page_number: int | None = None
    section_path: str | None = None
    chunk_type: str | None = "body"


def test_heading_chunk_opens_a_section():
    rows = [
        _Row(id=1, content="11.2 Entry Problem", chunk_type="heading", page_number=5),
        _Row(id=2, content="A pipe carries water.", page_number=5),
        _Row(id=3, content="Find P2 given P1=2e5.", page_number=6),
        _Row(id=4, content="11.3 Losses", chunk_type="heading", page_number=7),
        _Row(id=5, content="Friction reduces head.", page_number=7),
    ]
    sections = group_into_sections(rows)
    assert len(sections) == 2
    assert sections[0].title == "11.2 Entry Problem"
    # heading text is NOT part of the body text
    assert "Entry Problem" not in sections[0].text
    assert "A pipe carries water." in sections[0].text
    assert "Find P2" in sections[0].text
    assert sections[0].member_chunk_ids == (1, 2, 3)
    assert sections[0].page_start == 5
    assert sections[0].page_end == 6
    assert sections[1].title == "11.3 Losses"
    assert sections[1].member_chunk_ids == (4, 5)


def test_section_path_change_opens_a_section_without_heading():
    rows = [
        _Row(id=1, content="alpha body", section_path="1.1 Intro"),
        _Row(id=2, content="beta body", section_path="1.1 Intro"),
        _Row(id=3, content="gamma body", section_path="1.2 Next"),
    ]
    sections = group_into_sections(rows)
    assert len(sections) == 2
    assert sections[0].title == "1.1 Intro"
    assert sections[0].member_chunk_ids == (1, 2)
    assert sections[1].title == "1.2 Next"
    assert sections[1].member_chunk_ids == (3,)


def test_no_heading_no_section_path_degrades_to_one_section():
    rows = [
        _Row(id=1, content="line one", section_path=None, chunk_type="body"),
        _Row(id=2, content="line two", section_path="", chunk_type="body"),
    ]
    sections = group_into_sections(rows)
    assert len(sections) == 1
    assert sections[0].member_chunk_ids == (1, 2)
    assert "line one" in sections[0].text
    assert "line two" in sections[0].text


def test_source_content_hash_is_stable_and_normalized():
    a = [_Row(id=1, content="Find  the  PRESSURE.", chunk_type="body")]
    b = [_Row(id=9, content="find the pressure.", chunk_type="body")]  # re-indexed ids
    sa = group_into_sections(a)[0]
    sb = group_into_sections(b)[0]
    # whitespace/case-insensitive, id-independent → same hash (re-index stable)
    assert sa.source_content_hash == sb.source_content_hash
    assert len(sa.source_content_hash) == 64


def test_empty_input_returns_no_sections():
    assert group_into_sections([]) == []
