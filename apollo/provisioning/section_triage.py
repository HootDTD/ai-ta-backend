"""Phase 2 — Pass-1 TOC triage: rank sections by problem-likelihood + concept.

ONE cheap LLM call over the section TITLE list (plus light per-section stats) returns,
per section, whether it likely holds solvable problems, a priority, and a concept
guess. ``scrape_document`` scrapes high-priority sections first. FAILS OPEN: a
malformed/empty triage response yields every section at equal priority (degrades to
exhaustive Approach A) — triage NEVER aborts the run.

The injected ``chat_fn`` is the positional-string ``MeteredChat.scrape_chat_fn`` seam
(``chat_fn(payload) -> str``); MOCKED in Tier-1. The concept guess is a HINT only —
stage-4 ``tag_and_mint`` remains the authoritative concept resolver.
"""
from __future__ import annotations

import json
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from apollo.provisioning.section_grouping import Section

__all__ = ["SectionVerdict", "triage_sections", "build_triage_payload"]

_NUMERIC_IMPERATIVE = re.compile(
    r"\b(find|calculate|evaluate|compute|determine|solve)\b", re.IGNORECASE
)


@dataclass(frozen=True)
class SectionVerdict:
    section: Section
    is_problem_likely: bool
    priority: int  # higher = scrape sooner
    concept_slug: str
    concept_display: str


def _section_stats(section: Section) -> dict:
    text = section.text
    return {
        "title": section.title,
        "chars": len(text),
        "has_numeric_imperative": bool(_NUMERIC_IMPERATIVE.search(text))
        and any(ch.isdigit() for ch in text),
    }


def build_triage_payload(sections: Sequence[Section]) -> str:
    """The JSON string handed to the triage chat_fn: an indexed list of section
    titles + light stats. ``index`` is the section's position in ``sections``."""
    return json.dumps([{"index": i, **_section_stats(s)} for i, s in enumerate(sections)])


def _fail_open(sections: Sequence[Section]) -> list[SectionVerdict]:
    return [
        SectionVerdict(
            section=s, is_problem_likely=True, priority=0, concept_slug="", concept_display=""
        )
        for s in sections
    ]


def _as_int(value, default: int) -> int:
    return int(value) if isinstance(value, (int, float)) else default


def triage_sections(
    sections: Sequence[Section], *, chat_fn: Callable[[str], str]
) -> list[SectionVerdict]:
    """Rank sections via one cheap LLM call. Fails OPEN to all-equal-priority on any
    parse error. The model returns a JSON array of objects keyed by ``index`` with
    ``is_problem_likely`` (bool), ``priority`` (int), ``concept_slug``,
    ``concept_display``. A section the model omits defaults to problem-likely so the
    fallback still covers it."""
    if not sections:
        return []
    try:
        records = json.loads(chat_fn(build_triage_payload(sections)))
    except (json.JSONDecodeError, TypeError):
        return _fail_open(sections)
    if not isinstance(records, list):
        return _fail_open(sections)

    by_index: dict[int, dict] = {
        rec["index"]: rec
        for rec in records
        if isinstance(rec, dict) and isinstance(rec.get("index"), int)
    }
    return [
        SectionVerdict(
            section=s,
            is_problem_likely=bool(by_index.get(i, {}).get("is_problem_likely", True)),
            priority=_as_int(by_index.get(i, {}).get("priority", 0), 0),
            concept_slug=str(by_index.get(i, {}).get("concept_slug", "") or ""),
            concept_display=str(by_index.get(i, {}).get("concept_display", "") or ""),
        )
        for i, s in enumerate(sections)
    ]
