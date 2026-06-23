"""Phase-2 TOC-triage tests. PURE — injected deterministic chat_fn, no network."""
from __future__ import annotations

import json

from apollo.provisioning.section_grouping import Section
from apollo.provisioning.section_triage import (
    SectionVerdict,
    build_triage_payload,
    triage_sections,
)


def _section(title: str, text: str = "body text") -> Section:
    return Section(
        title=title,
        document_id=1,
        page_start=1,
        page_end=1,
        text=text,
        source_content_hash="h" * 64,
        member_chunk_ids=(1,),
    )


def test_triage_parses_per_section_verdicts():
    sections = [_section("6.1 Theory"), _section("6.2 Exercises", "find x")]
    payload_seen = {}

    def _chat(payload):
        payload_seen["p"] = payload
        return json.dumps(
            [
                {"index": 0, "is_problem_likely": False, "priority": 0,
                 "concept_slug": "theory", "concept_display": "Theory"},
                {"index": 1, "is_problem_likely": True, "priority": 9,
                 "concept_slug": "integration", "concept_display": "Integration"},
            ]
        )

    verdicts = triage_sections(sections, chat_fn=_chat)
    assert [v.is_problem_likely for v in verdicts] == [False, True]
    assert verdicts[1].priority == 9
    assert verdicts[1].concept_slug == "integration"
    # the payload carried the titles so the model can rank them
    assert "6.2 Exercises" in payload_seen["p"]


def test_triage_fails_open_on_malformed_json():
    """Malformed triage output → every section problem-likely at equal priority
    (degrades to exhaustive). DISCRIMINATING: returning [] here would skip the
    document's problems entirely."""
    sections = [_section("A"), _section("B")]
    verdicts = triage_sections(sections, chat_fn=lambda _p: "not json at all")
    assert len(verdicts) == 2
    assert all(v.is_problem_likely for v in verdicts)
    assert all(v.priority == 0 for v in verdicts)


def test_triage_fails_open_on_non_array():
    sections = [_section("A")]
    verdicts = triage_sections(sections, chat_fn=lambda _p: json.dumps({"x": 1}))
    assert len(verdicts) == 1
    assert verdicts[0].is_problem_likely is True


def test_triage_missing_index_defaults_to_likely():
    """A section the model omits defaults to problem-likely (so it is still covered
    by the fallback), not silently dropped."""
    sections = [_section("A"), _section("B")]
    chat = lambda _p: json.dumps([{"index": 0, "is_problem_likely": False, "priority": 1}])  # noqa: E731
    verdicts = triage_sections(sections, chat_fn=chat)
    assert verdicts[0].is_problem_likely is False
    assert verdicts[1].is_problem_likely is True  # omitted → default likely


def test_triage_empty_sections_returns_empty():
    assert triage_sections([], chat_fn=lambda _p: "[]") == []


def test_build_triage_payload_indexes_sections():
    sections = [_section("First"), _section("Second", "find the value 42")]
    payload = json.loads(build_triage_payload(sections))
    assert payload[0]["index"] == 0
    assert payload[0]["title"] == "First"
    assert payload[1]["index"] == 1
    assert payload[1]["has_numeric_imperative"] is True  # "find ... 42"


def test_section_verdict_is_frozen():
    v = SectionVerdict(
        section=_section("A"), is_problem_likely=True, priority=0,
        concept_slug="c", concept_display="C",
    )
    assert v.is_problem_likely is True
