# Apollo Structure-Aware Problem Finding (Phase 2) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace stage-1's one-LLM-call-per-micro-chunk scrape with reconstruct-sections → TOC-triage → batched whole-section scrape (+ bounded exhaustive fallback), then prove a real end-to-end promotion on calculus-volume-2.

**Architecture:** The 20,930-micro-chunk per-document scrape re-sends the scrape prompt once per ~17-char fragment (~92% wasted tokens, trips the 2M ceiling, never sees a whole problem). We regroup chunks into their real sections (via `section_path`/`chunk_type='heading'`), triage the section list once to rank + concept-label them, then scrape whole sections. Drop-in at one orchestrator call site — the new entrypoint returns the same `ScrapeResult`, so stages 2–5 are untouched.

**Tech Stack:** Python 3 / FastAPI, SQLAlchemy async + asyncpg (Supabase Postgres + pgvector), Neo4j (`:Canon`), pytest (`asyncio_mode = auto`). LLM calls are mocked in all Tier-1 tests (deterministic injected `chat_fn`); the real-LLM run is the final E2E task only.

**Design source of truth:** `docs/superpowers/specs/2026-06-23-apollo-structured-scrape-design.md`.

## Global Constraints

- Branch `ApolloRun`. NEVER push to `main`. Do NOT merge any PR — open it, report URL + CI, stop.
- No new packages without asking.
- Supabase: "staging" = `hjevtxdtrkxjcaaexdxt` (test DB). "Apollo" project = PROD — never write to prod.
- Keep structured-JSON-from-LLM + comprehensive per-stage debug logging; never weaken the FAIL-CLOSED `TagMintError` convention.
- The new scrape entrypoint MUST return the existing `ScrapeResult(candidates, scraped_count, parse_failures)` shape — stages 2–5 and the orchestrator's per-candidate/per-document decision logic do NOT change.
- The per-candidate idempotency key rides in the EXISTING `CandidateQuestion.chunk_content_hash` field (now `"<section_hash>.<ordinal>"`); `write_tier1_problems`, `_problem_code_for`, and the orchestrator's `_find_tier1_row_id` consume it verbatim and are NOT modified.
- Update `docs/architecture/apollo.md` (owner doc) in the SAME commit as the code change that affects it; bump its `last_verified` in the final task.
- End every commit message with the trailer:
  `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`
- TDD: write the failing test first, watch it fail for the RIGHT reason, then implement.

## File Structure

| File | Responsibility | Task |
|------|----------------|------|
| `apollo/provisioning/cost_constants.py` | Add `APOLLO_SCRAPE_MAX_SECTIONS`, `APOLLO_SCRAPE_MIN_CANDIDATES`, `structured_scrape_enabled()`. | 1 |
| `apollo/provisioning/tests/test_cost_constants.py` | Pin the new defaults + flag reader. | 1 |
| `apollo/provisioning/section_grouping.py` (new) | Pure: reconstruct `Section`s from chunk rows. | 2 |
| `apollo/provisioning/tests/test_section_grouping.py` (new) | Grouping unit tests. | 2 |
| `apollo/provisioning/section_triage.py` (new) | Pass-1 triage: rank + concept-label sections; fail-open. | 3 |
| `apollo/provisioning/tests/test_section_triage.py` (new) | Triage parse + fail-open tests. | 3 |
| `apollo/provisioning/scrape.py` | Add `scrape_section`, `scrape_document` (reconstruct→triage→scrape→fallback); section-scoped idempotency key. | 4 |
| `apollo/provisioning/tests/test_scrape.py` | Section-scrape + scrape_document unit tests. | 4 |
| `apollo/provisioning/orchestrator.py` | `_load_chunks` columns + `_ChunkView` fields; swap `scrape_questions`→`scrape_document`; `_SCRAPE_SYSTEM_PROMPT` whole-section update + `_TRIAGE_SYSTEM_PROMPT`. | 5 |
| `apollo/provisioning/__init__.py` | Re-export `scrape_document`. | 5 |
| `apollo/provisioning/tests/test_orchestrator.py` | Update `_patch_stages`/`_boom_scrape` stubs for the new entrypoint; wiring test. | 5 |
| `docs/architecture/apollo.md` | Owner doc: stage-1 structure-aware scrape; `last_verified`. | 6 |

---

### Task 1: Config constants + structured-scrape flag

**Files:**
- Modify: `apollo/provisioning/cost_constants.py`
- Test: `apollo/provisioning/tests/test_cost_constants.py`

**Interfaces:**
- Produces: `APOLLO_SCRAPE_MAX_SECTIONS: int` (default 120), `APOLLO_SCRAPE_MIN_CANDIDATES: int` (default 3), `structured_scrape_enabled() -> bool` (env `APOLLO_STRUCTURED_SCRAPE`, default ON). Consumed by Task 4 (`scrape_document`) and Task 5 (orchestrator wiring).

- [ ] **Step 1: Write the failing test**

Add to `apollo/provisioning/tests/test_cost_constants.py`:

```python
def test_scrape_section_bounds_defaults():
    """Phase-2 structure-aware scrape bounds carry committed defaults."""
    import importlib

    import apollo.provisioning.cost_constants as cc

    importlib.reload(cc)
    assert cc.APOLLO_SCRAPE_MAX_SECTIONS == 120
    assert cc.APOLLO_SCRAPE_MIN_CANDIDATES == 3


def test_structured_scrape_enabled_default_on_and_overridable(monkeypatch):
    """The structured-scrape flag defaults ON and reads per-call (env-overridable)."""
    from apollo.provisioning.cost_constants import structured_scrape_enabled

    monkeypatch.delenv("APOLLO_STRUCTURED_SCRAPE", raising=False)
    assert structured_scrape_enabled() is True
    monkeypatch.setenv("APOLLO_STRUCTURED_SCRAPE", "0")
    assert structured_scrape_enabled() is False
    monkeypatch.setenv("APOLLO_STRUCTURED_SCRAPE", "true")
    assert structured_scrape_enabled() is True
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest apollo/provisioning/tests/test_cost_constants.py::test_scrape_section_bounds_defaults apollo/provisioning/tests/test_cost_constants.py::test_structured_scrape_enabled_default_on_and_overridable -v`
Expected: FAIL with `AttributeError` / `ImportError` (`APOLLO_SCRAPE_MAX_SECTIONS` / `structured_scrape_enabled` not defined).

- [ ] **Step 3: Add the constants + flag reader**

In `apollo/provisioning/cost_constants.py`, after the `MAX_ATTEMPTS` definition (~line 31), add:

```python
# Phase-2 structure-aware scrape bounds (env-overridable, pinned by tests).
# MAX_SECTIONS caps sections scraped per document (covers calculus's 60 sections
# with headroom; a per-document bound, not a global one). MIN_CANDIDATES is the
# "too thin" threshold below which the exhaustive fallback widens past the
# problem-likely sections.
APOLLO_SCRAPE_MAX_SECTIONS: int = int(os.getenv("APOLLO_SCRAPE_MAX_SECTIONS", "120"))
APOLLO_SCRAPE_MIN_CANDIDATES: int = int(os.getenv("APOLLO_SCRAPE_MIN_CANDIDATES", "3"))


def structured_scrape_enabled() -> bool:
    """Read per-call so a flag flip needs no restart. Default ON *within* the
    already-OFF auto-provisioning subsystem; set ``APOLLO_STRUCTURED_SCRAPE=0`` to
    revert stage 1 to the legacy per-chunk path."""
    return os.getenv("APOLLO_STRUCTURED_SCRAPE", "1").lower() in ("1", "true", "yes")
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest apollo/provisioning/tests/test_cost_constants.py -v`
Expected: PASS (the whole module, no regressions).

- [ ] **Step 5: Commit**

```bash
git add apollo/provisioning/cost_constants.py apollo/provisioning/tests/test_cost_constants.py
git commit -m "feat(apollo): add structure-aware scrape bounds + APOLLO_STRUCTURED_SCRAPE flag

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: `section_grouping.py` — reconstruct sections (pure)

`group_into_sections` regroups retrieval micro-chunks into the document's real
sections. A `chunk_type='heading'` chunk OR a change in non-empty `section_path`
opens a new section; body/equation chunks accumulate. No headings + no section_path
→ a single whole-document section (degrade path). Heading text becomes the section
title and is excluded from the section body text.

**Files:**
- Create: `apollo/provisioning/section_grouping.py`
- Test: `apollo/provisioning/tests/test_section_grouping.py`

**Interfaces:**
- Consumes: duck-typed chunk rows with attributes `id, content, document_id, page_number, section_path, chunk_type`, ordered by `id` ascending.
- Produces: `Section{title: str, document_id: int, page_start: int|None, page_end: int|None, text: str, source_content_hash: str, member_chunk_ids: tuple[int,...]}` (frozen dataclass); `group_into_sections(chunk_rows) -> list[Section]`; `section_content_hash(text) -> str`. Consumed by Tasks 3 and 4.

- [ ] **Step 1: Write the failing tests**

Create `apollo/provisioning/tests/test_section_grouping.py`:

```python
"""Phase-2 section reconstruction tests. PURE — no DB, no LLM, no network."""
from __future__ import annotations

from dataclasses import dataclass

from apollo.provisioning.section_grouping import (
    Section,
    group_into_sections,
    section_content_hash,
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest apollo/provisioning/tests/test_section_grouping.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'apollo.provisioning.section_grouping'`.

- [ ] **Step 3: Implement the module**

Create `apollo/provisioning/section_grouping.py`:

```python
"""Phase 2 — reconstruct document sections from retrieval micro-chunks.

The layout-aware indexer splits a document into line/phrase-level ``aita_chunks``
(median ~17 chars) tuned for RETRIEVAL, plus ``chunk_type='heading'`` markers and a
``section_path`` label per chunk. The per-chunk scrape (stage 1) re-sent the scrape
system prompt once PER micro-chunk — ~92% wasted tokens, and no call ever saw a
whole problem. This module regroups those micro-chunks into the document's real
sections so stage 1 can scrape a whole section at once.

PURE: no DB, no LLM. Consumes duck-typed chunk rows (``id``, ``content``,
``document_id``, ``page_number``, ``section_path``, ``chunk_type``) ordered by
``id`` ascending (the orchestrator's ``_load_chunks`` order) and returns ordered
``Section`` records.
"""
from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from dataclasses import dataclass

__all__ = ["Section", "group_into_sections", "section_content_hash"]

_HEADING = "heading"


def _normalize(text: str) -> str:
    """Collapse whitespace, strip, lowercase — the content-stable hash input
    (mirrors ``scrape._normalize``; a local helper to keep this module pure)."""
    return re.sub(r"\s+", " ", text).strip().lower()


def section_content_hash(text: str) -> str:
    """sha256 hex of the normalized section text (64 lowercase hex chars). Keys on
    CONTENT not chunk id, so it survives a re-index that re-mints ids."""
    return hashlib.sha256(_normalize(text).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Section:
    """One reconstructed section. ``text`` is the concatenated member-chunk body
    (heading lines excluded — they live in ``title``)."""

    title: str
    document_id: int
    page_start: int | None
    page_end: int | None
    text: str
    source_content_hash: str
    member_chunk_ids: tuple[int, ...]


def group_into_sections(chunk_rows: Sequence) -> list[Section]:
    """Group micro-chunks into sections. A ``chunk_type='heading'`` chunk OR a
    change in non-empty ``section_path`` opens a new section; body/equation chunks
    accumulate. No headings and no ``section_path`` → a single whole-document
    section. The section ``title`` is the heading text (else the first non-empty
    ``section_path``, else ``"section"``); ``text`` excludes heading lines."""
    sections: list[Section] = []
    cur_title: str | None = None
    cur_path: str | None = None
    cur_body: list[str] = []
    cur_ids: list[int] = []
    cur_pages: list[int] = []
    cur_doc: int | None = None

    def _flush() -> None:
        nonlocal cur_title, cur_path, cur_body, cur_ids, cur_pages, cur_doc
        if not cur_ids:
            return
        text = "\n".join(cur_body)
        title = (cur_title or cur_path or "section").strip() or "section"
        sections.append(
            Section(
                title=title,
                document_id=int(cur_doc) if cur_doc is not None else -1,
                page_start=min(cur_pages) if cur_pages else None,
                page_end=max(cur_pages) if cur_pages else None,
                text=text,
                source_content_hash=section_content_hash(text or title),
                member_chunk_ids=tuple(cur_ids),
            )
        )
        cur_title = None
        cur_body = []
        cur_ids = []
        cur_pages = []
        cur_doc = None

    for row in chunk_rows:
        content = str(getattr(row, "content", "") or "")
        is_heading = getattr(row, "chunk_type", None) == _HEADING
        spath = (getattr(row, "section_path", None) or "").strip()
        path_changed = bool(spath) and spath != cur_path

        if is_heading or (path_changed and cur_ids):
            _flush()
        if spath:
            cur_path = spath
        if is_heading:
            cur_title = content.strip() or spath or cur_path
        elif cur_title is None and spath:
            cur_title = spath

        cur_ids.append(int(getattr(row, "id")))
        cur_doc = getattr(row, "document_id", cur_doc)
        page = getattr(row, "page_number", None)
        if page is not None:
            cur_pages.append(int(page))
        if not is_heading and content.strip():
            cur_body.append(content)

    _flush()
    return sections
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest apollo/provisioning/tests/test_section_grouping.py -v`
Expected: PASS (all five tests).

- [ ] **Step 5: Commit**

```bash
git add apollo/provisioning/section_grouping.py apollo/provisioning/tests/test_section_grouping.py
git commit -m "feat(apollo): reconstruct document sections from retrieval micro-chunks

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: `section_triage.py` — Pass-1 TOC triage (fail-open)

`triage_sections` makes ONE cheap LLM call over the section title list + light
stats, returning per-section problem-likelihood, priority, and a concept guess.
Fails OPEN (all sections equal priority) on any parse error — triage never aborts
the run.

**Files:**
- Create: `apollo/provisioning/section_triage.py`
- Test: `apollo/provisioning/tests/test_section_triage.py`

**Interfaces:**
- Consumes: `Section` (Task 2); an injected positional-string `chat_fn(payload: str) -> str` (the `MeteredChat.scrape_chat_fn` seam).
- Produces: `SectionVerdict{section: Section, is_problem_likely: bool, priority: int, concept_slug: str, concept_display: str}` (frozen dataclass); `triage_sections(sections, *, chat_fn) -> list[SectionVerdict]`; `build_triage_payload(sections) -> str`. Consumed by Task 4.

- [ ] **Step 1: Write the failing tests**

Create `apollo/provisioning/tests/test_section_triage.py`:

```python
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest apollo/provisioning/tests/test_section_triage.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'apollo.provisioning.section_triage'`.

- [ ] **Step 3: Implement the module**

Create `apollo/provisioning/section_triage.py`:

```python
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
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest apollo/provisioning/tests/test_section_triage.py -v`
Expected: PASS (all tests).

- [ ] **Step 5: Commit**

```bash
git add apollo/provisioning/section_triage.py apollo/provisioning/tests/test_section_triage.py
git commit -m "feat(apollo): add fail-open TOC triage to rank + concept-label sections

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: `scrape.py` — `scrape_section` + `scrape_document`

`scrape_section` scrapes one whole section (fail-soft, same contract as the per-chunk
path) and stamps the section-scoped idempotency key `"<section_hash>.<ordinal>"` into
each candidate's `chunk_content_hash`, with a deterministic ordinal (sort by content
hash of the normalized `problem_text`). `scrape_document` orchestrates
reconstruct → triage → scrape-prioritized-sections → bounded fallback, returning the
existing `ScrapeResult`. When `structured=False` it delegates to the legacy
`scrape_questions` per-chunk path.

**Files:**
- Modify: `apollo/provisioning/scrape.py`
- Test: `apollo/provisioning/tests/test_scrape.py`

**Interfaces:**
- Consumes: `group_into_sections`, `Section` (Task 2); `triage_sections` (Task 3); existing `CandidateQuestion`, `ScrapeResult`, `chunk_content_hash`, `scrape_questions`.
- Produces:
  - `scrape_section(section: Section, *, concept_hint: str, chat_fn: Callable[[str], str]) -> tuple[list[CandidateQuestion], int]` (candidates, parse_failures).
  - `scrape_document(chunk_rows, *, chat_fn, triage_chat_fn, max_sections: int, min_candidates: int, structured: bool = True) -> ScrapeResult`. Consumed by Task 5. Each candidate's `chunk_content_hash` is `"<section_hash>.<ordinal>"`; `concept_slug` is the LLM value or the triage `concept_hint` or `"provisional.inventory"`.

- [ ] **Step 1: Write the failing tests**

Add to `apollo/provisioning/tests/test_scrape.py` (after the existing pure tests, before the Real-PG helpers; reuse the module's `_run`, `_well_formed_record`, `_chat_per_chunk`):

```python
from apollo.provisioning.scrape import scrape_document, scrape_section
from apollo.provisioning.section_grouping import Section


def _section(*, title="6.2 Exercises", text="Find P2 in the pipe.", doc=7, page=3,
             shash="a" * 64) -> Section:
    return Section(
        title=title, document_id=doc, page_start=page, page_end=page, text=text,
        source_content_hash=shash, member_chunk_ids=(1, 2),
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
    rec = _well_formed_record()
    del rec["concept_slug"]
    cands, _ = scrape_section(sec, concept_hint="integration", chat_fn=lambda _t: json.dumps([rec]))
    assert cands[0].concept_slug == "integration"


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
    triage = lambda _p: json.dumps([{"index": 0, "is_problem_likely": True, "priority": 5,  # noqa: E731
                                     "concept_slug": "area", "concept_display": "Area"}])
    scrape = lambda _text: json.dumps([_well_formed_record(concept_slug="area")])  # noqa: E731
    result = _run(scrape_document(
        rows, chat_fn=scrape, triage_chat_fn=triage, max_sections=120, min_candidates=3,
    ))
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
    _run(scrape_document(rows, chat_fn=_scrape, triage_chat_fn=triage,
                         max_sections=1, min_candidates=99))
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
    _run(scrape_document(rows, chat_fn=_scrape, triage_chat_fn=triage,
                         max_sections=120, min_candidates=3))
    # both the likely AND the unlikely section were scraped (widened, still thin)
    assert len(scraped_titles) == 2


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
    result = _run(scrape_document(rows, chat_fn=chat, triage_chat_fn=lambda _p: "[]",
                                  max_sections=120, min_candidates=3, structured=False))
    assert result.scraped_count == 1
    # legacy path stamps the CHUNK content hash (no ".ordinal" section suffix)
    assert result.candidates[0].chunk_content_hash == chunk_content_hash("legacy chunk")
```

Add `from dataclasses import dataclass` and `from apollo.provisioning.scrape import chunk_content_hash` to the test imports if not already present (the module already imports `chunk_content_hash` and `dataclass`).

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest apollo/provisioning/tests/test_scrape.py -k "section or scrape_document" -v`
Expected: FAIL with `ImportError: cannot import name 'scrape_document'` (and `scrape_section`).

- [ ] **Step 3: Implement `scrape_section` + `scrape_document`**

In `apollo/provisioning/scrape.py`, add the imports near the top (after the existing imports):

```python
from apollo.provisioning.section_grouping import group_into_sections
from apollo.provisioning.section_triage import triage_sections
```

Add to `__all__`: `"scrape_document"`, `"scrape_section"`.

Add these functions after `scrape_questions` (~line 174):

```python
def _coerce_section_candidate(
    raw: Any, *, section, concept_hint: str
) -> CandidateQuestion | None:
    """Build a CandidateQuestion from one LLM record scraped from a whole SECTION.
    Provenance (document_id/page) comes from the SECTION; concept_slug falls back to
    the triage hint then the provisional concept. ``chunk_content_hash`` is a
    placeholder here — ``scrape_section`` re-stamps it with the section-scoped
    ``<section_hash>.<ordinal>`` key after deterministic ordering. Returns None
    (fail-soft) on any validation error."""
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
    ``'<section_hash>.<ordinal>'`` with the ordinal assigned over candidates sorted
    by the content hash of their normalized ``problem_text`` (so a re-run yields the
    SAME key↔problem mapping — re-index/replay idempotent)."""
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
        c.model_copy(update={"chunk_content_hash": f"{section.source_content_hash}.{i}"})
        for i, c in enumerate(built)
    ]
    return finalized, failures


async def scrape_document(
    chunk_rows: Sequence,
    *,
    chat_fn: Callable[..., str],
    triage_chat_fn: Callable[..., str],
    max_sections: int,
    min_candidates: int,
    structured: bool = True,
) -> ScrapeResult:
    """Structure-aware stage-1 scrape. Reconstructs sections, triages them once, then
    scrapes problem-likely sections first; a NOT-likely section is scraped only while
    candidates remain under ``min_candidates`` (the bounded exhaustive fallback), and
    no more than ``max_sections`` sections are scraped per document.

    When ``structured`` is False, delegates to the legacy per-chunk
    ``scrape_questions`` (the ``APOLLO_STRUCTURED_SCRAPE`` revert path)."""
    if not structured:
        return await scrape_questions(chunk_rows, chat_fn=chat_fn)

    sections = group_into_sections(chunk_rows)
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
```

Add a module logger near the top of `scrape.py` if absent: `import logging` and `_LOG = logging.getLogger(__name__)`.

- [ ] **Step 4: Run the new tests to verify they pass**

Run: `pytest apollo/provisioning/tests/test_scrape.py -v`
Expected: PASS — the new section/scrape_document tests AND all pre-existing scrape tests (the per-chunk `scrape_questions` path is unchanged).

- [ ] **Step 5: Commit**

```bash
git add apollo/provisioning/scrape.py apollo/provisioning/tests/test_scrape.py
git commit -m "feat(apollo): batched whole-section scrape with bounded fallback (scrape_document)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: Orchestrator wiring + prompts + re-export

Swap the orchestrator's stage-1 call from per-chunk `scrape_questions` to
`scrape_document`, load the section metadata columns, add the triage system prompt,
update the scrape system prompt for whole-section input, and update the orchestrator
tests' stage stubs so the existing suite stays green.

**Files:**
- Modify: `apollo/provisioning/orchestrator.py`
- Modify: `apollo/provisioning/__init__.py`
- Test: `apollo/provisioning/tests/test_orchestrator.py`

**Interfaces:**
- Consumes: `scrape_document` (Task 4); `APOLLO_SCRAPE_MAX_SECTIONS`, `APOLLO_SCRAPE_MIN_CANDIDATES`, `structured_scrape_enabled` (Task 1).
- Produces: `_ChunkView` now carries `id, section_path, chunk_type`; `_TRIAGE_SYSTEM_PROMPT` added; `run_provisioning` calls `scrape_document`. Stage 2–5 contracts unchanged.

- [ ] **Step 1: Update the orchestrator test stubs FIRST (they must drive the new entrypoint)**

In `apollo/provisioning/tests/test_orchestrator.py`, change `_patch_stages`'s inner `_scrape` stub and the patched name. Replace (the block at lines ~164-180):

```python
    async def _scrape(chunks, *, chat_fn):  # noqa: ANN001
        # exercise the injected chat_fn so a cost-abort scrape can raise.
        for ch in chunks:
            chat_fn(ch.content)
        return ScrapeResult(
            candidates=tuple(scrape_candidates),
            scraped_count=1 if scrape_candidates else 0,
            parse_failures=0,
        )
```

with:

```python
    async def _scrape(chunk_rows, *, chat_fn, triage_chat_fn, max_sections,
                      min_candidates, structured=True):  # noqa: ANN001
        # exercise the injected scrape chat_fn so a cost-abort scrape can raise.
        for ch in chunk_rows:
            chat_fn(ch.content)
        return ScrapeResult(
            candidates=tuple(scrape_candidates),
            scraped_count=1 if scrape_candidates else 0,
            parse_failures=0,
        )
```

And change the patch target (line ~180) from:

```python
    monkeypatch.setattr(orch, "scrape_questions", _scrape)
```

to:

```python
    monkeypatch.setattr(orch, "scrape_document", _scrape)
```

In `test_run_provisioning_unexpected_error_fails_run_terminally` (T-OR10), change the `_boom_scrape` signature and its patch (lines ~639-645):

```python
    async def _boom_scrape(chunk_rows, *, chat_fn, triage_chat_fn, max_sections,
                           min_candidates, structured=True):  # noqa: ANN001
        raise ValueError("totally unexpected")
    ...
    monkeypatch.setattr(orch, "scrape_document", _boom_scrape)
```

(The `_resolve_prov` line directly below stays as-is.)

- [ ] **Step 2: Add the wiring (integration) test**

Add to `apollo/provisioning/tests/test_orchestrator.py`:

```python
async def test_run_provisioning_uses_scrape_document_with_section_columns(db_session, monkeypatch):
    """Wiring proof: run_provisioning calls scrape_document, and _load_chunks now
    feeds section metadata (id/section_path/chunk_type) into the chunk rows.
    DISCRIMINATING: reverting _load_chunks to content-only RED-flags on the missing
    attribute access in this stub."""
    space, run_id, claimed = await _seed(db_session, slug="ordoc", n_chunks=2)
    concept_id = await _seed_concept(db_session, search_space_id=space)
    seen = {}

    async def _scrape_doc(chunk_rows, *, chat_fn, triage_chat_fn, max_sections,
                          min_candidates, structured=True):  # noqa: ANN001
        rows = list(chunk_rows)
        seen["has_section_attrs"] = all(
            hasattr(r, "id") and hasattr(r, "section_path") and hasattr(r, "chunk_type")
            for r in rows
        )
        seen["max_sections"] = max_sections
        seen["min_candidates"] = min_candidates
        return ScrapeResult(candidates=(), scraped_count=0, parse_failures=0)

    async def _resolve_prov(db, *, search_space_id):  # noqa: ANN001
        return concept_id

    monkeypatch.setattr(orch, "scrape_document", _scrape_doc)
    monkeypatch.setattr(orch, "resolve_or_create_provisional_concept", _resolve_prov)

    outcome = await _run(db_session, claimed)

    assert outcome.status == "succeeded"
    assert seen["has_section_attrs"] is True
    assert isinstance(seen["max_sections"], int)
    assert isinstance(seen["min_candidates"], int)
```

- [ ] **Step 3: Run the orchestrator tests to verify the new wiring test fails (others still pass after stub update)**

Run: `pytest apollo/provisioning/tests/test_orchestrator.py -v`
Expected: the new `test_run_provisioning_uses_scrape_document_with_section_columns` FAILS (orchestrator still imports/calls `scrape_questions`, and `_load_chunks` does not select section columns → the `hasattr` for `section_path`/`chunk_type` is False or the call name mismatches). The other tests now patch `orch.scrape_document`, which does not yet exist on the module → they FAIL too. This is expected; Step 4 makes them all pass.

- [ ] **Step 4: Implement the orchestrator wiring**

In `apollo/provisioning/orchestrator.py`:

(a) Change the scrape import (lines ~63-67) from:

```python
from apollo.provisioning.scrape import (
    resolve_or_create_provisional_concept,
    scrape_questions,
    write_tier1_problems,
)
```

to:

```python
from apollo.provisioning.cost_constants import (
    APOLLO_SCRAPE_MAX_SECTIONS,
    APOLLO_SCRAPE_MIN_CANDIDATES,
    structured_scrape_enabled,
)
from apollo.provisioning.scrape import (
    resolve_or_create_provisional_concept,
    scrape_document,
    write_tier1_problems,
)
```

(b) Replace the `_ChunkView` class (lines ~204-215) with the section-aware shape:

```python
class _ChunkView:
    """The minimal chunk shape the scrape reads. Now carries the section metadata
    (``id``/``section_path``/``chunk_type``) so ``group_into_sections`` can rebuild
    the document's sections. Selecting only these columns keeps the read cheap (no
    pgvector ``embedding``)."""

    __slots__ = ("id", "content", "document_id", "page_number", "section_path", "chunk_type")

    def __init__(self, id, content, document_id, page_number, section_path, chunk_type):  # noqa: A002
        self.id = id
        self.content = content
        self.document_id = document_id
        self.page_number = page_number
        self.section_path = section_path
        self.chunk_type = chunk_type
```

(c) Replace `_load_chunks` (lines ~218-232):

```python
async def _load_chunks(db: AsyncSession, *, document_id: int) -> Sequence[_ChunkView]:
    from database.models import AITAChunk

    rows = (
        await db.execute(
            select(
                AITAChunk.id,
                AITAChunk.content,
                AITAChunk.document_id,
                AITAChunk.page_number,
                AITAChunk.section_path,
                AITAChunk.chunk_type,
            )
            .where(AITAChunk.document_id == document_id)
            .order_by(AITAChunk.id.asc())
        )
    ).all()
    return [
        _ChunkView(r.id, r.content, r.document_id, r.page_number, r.section_path, r.chunk_type)
        for r in rows
    ]
```

(d) Replace the scrape call inside `run_provisioning` (the `try: scrape_result = await scrape_questions(...) except CostBudgetExceeded ...` block, lines ~287-294):

```python
        try:
            scrape_result = await scrape_document(
                chunks,  # type: ignore[arg-type]  # _ChunkView is the duck-typed shape grouping reads
                chat_fn=metered_chat.scrape_chat_fn(_SCRAPE_SYSTEM_PROMPT),
                triage_chat_fn=metered_chat.scrape_chat_fn(_TRIAGE_SYSTEM_PROMPT),
                max_sections=APOLLO_SCRAPE_MAX_SECTIONS,
                min_candidates=APOLLO_SCRAPE_MIN_CANDIDATES,
                structured=structured_scrape_enabled(),
            )
        except CostBudgetExceeded as exc:
            raise _cost_abort(exc, stage="scrape") from exc
```

(e) Update `_SCRAPE_SYSTEM_PROMPT` for whole-section input — change its first line (line ~86) from "a passage of course material" framing to section framing, KEEPING every declared field name (the `test_scrape_prompt_declares_candidate_question_fields` contract). Replace the first sentence:

```python
_SCRAPE_SYSTEM_PROMPT = (
    "You extract EVERY solvable quantitative practice problem from one SECTION of "
    "course material (textbook prose, worked examples, and exercise sets all count; "
    "a section may contain zero, one, or many problems).\n"
    "Return ONLY a JSON array - no prose, no explanation, no markdown code fences. "
    "Each array element is an object with EXACTLY these keys:\n"
    '  "problem_text": string - the full, self-contained problem statement.\n'
    '  "given_values": object mapping each stated known quantity\'s short symbol to '
    "its NUMERIC value (numbers only - no units, no strings); use {} if none.\n"
    '  "target_unknown": string - the single quantity the problem asks to find.\n'
    '  "difficulty": exactly one of "intro", "standard", "hard".\n'
    '  "concept_slug": string - a short dotted/kebab concept id, e.g. '
    '"bernoulli-equation".\n'
    "If the section contains no solvable problems, return []."
)

_TRIAGE_SYSTEM_PROMPT = (
    "You triage a textbook's SECTIONS to find which likely contain solvable "
    "quantitative practice problems. You receive a JSON array of sections, each with "
    'an "index", "title", "chars", and "has_numeric_imperative" flag.\n'
    "Return ONLY a JSON array - no prose, no markdown fences. Each element is an "
    "object with EXACTLY these keys:\n"
    '  "index": integer - echo the section\'s index.\n'
    '  "is_problem_likely": boolean - true if the section probably contains solvable '
    "problems or worked examples.\n"
    '  "priority": integer 0-10 - higher = scrape sooner.\n'
    '  "concept_slug": string - a short dotted/kebab concept id for the section.\n'
    '  "concept_display": string - a human-readable concept label.\n'
    "Include EVERY index from the input exactly once."
)
```

- [ ] **Step 5: Re-export `scrape_document`**

In `apollo/provisioning/__init__.py`, add `scrape_document` to the `scrape` imports and `__all__` alongside the existing `scrape_questions` re-export (find the line importing `scrape_questions` from `.scrape` and add `scrape_document` next to it; add `"scrape_document"` to `__all__`).

- [ ] **Step 6: Run the orchestrator + scrape + full provisioning suite to verify GREEN**

Run: `pytest apollo/provisioning/tests/test_orchestrator.py apollo/provisioning/tests/test_scrape.py -v`
Expected: PASS — all pre-existing orchestrator tests (now driving `scrape_document` via the updated stubs) AND the new wiring test.

Then run the whole package: `pytest apollo/provisioning/ -v --tb=short`
Expected: PASS (no regressions across the subsystem).

- [ ] **Step 7: Commit**

```bash
git add apollo/provisioning/orchestrator.py apollo/provisioning/__init__.py apollo/provisioning/tests/test_orchestrator.py
git commit -m "feat(apollo): wire structure-aware scrape_document into the orchestrator

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 6: Owner-doc update + full suite green + real-LLM E2E on calculus

**Files:**
- Modify (doc): `docs/architecture/apollo.md`
- (No new source.)

- [ ] **Step 1: Run the full provisioning suite**

Run: `pytest apollo/provisioning/ -v --tb=short`
Expected: PASS (all modules, including the new section_grouping / section_triage / scrape_document tests). Fix any regression before continuing.

- [ ] **Step 2: Update the owner doc (stage-1 description)**

In `docs/architecture/apollo.md`, in the auto-provisioning stage-1 / scrape description, add:

```
Stage 1 is STRUCTURE-AWARE (Phase 2): the orchestrator loads chunk section metadata
(`section_path`, `chunk_type`) and `scrape_document` (a) reconstructs the document's
sections via `section_grouping.group_into_sections`, (b) triages the section list
once (`section_triage.triage_sections`, fail-open) to rank + concept-label sections,
then (c) scrapes whole sections (`scrape_section`) — problem-likely first, widening
into the rest only while candidates < `APOLLO_SCRAPE_MIN_CANDIDATES`, capped at
`APOLLO_SCRAPE_MAX_SECTIONS`. This replaces the per-micro-chunk scrape (~92% of spend
was the prompt re-sent per ~17-char fragment, tripping the 2M ceiling). The
per-candidate idempotency key is `scrape.<section_hash>.<ordinal>` (deterministic
ordinal), carried in `CandidateQuestion.chunk_content_hash`; no migration (subsystem
dormant). `APOLLO_STRUCTURED_SCRAPE=0` reverts to the legacy per-chunk path. Triage's
concept is a HINT; `tag_and_mint` stays authoritative.
```

- [ ] **Step 3: Bump `last_verified`**

In `docs/architecture/apollo.md` frontmatter, set `last_verified: 2026-06-23`.

- [ ] **Step 4: Commit the doc + code reconciliation**

```bash
git add docs/architecture/apollo.md
git commit -m "docs(apollo): document the structure-aware stage-1 scrape (Phase 2)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

- [ ] **Step 5: Real-LLM end-to-end run on calculus (real LLM, local Neo4j, staging Supabase)**

Per `RUNBOOK.md`. Target document: **calculus-volume-2 (`document_id=5`, `search_space_id=3`)**. Enqueue a legitimate job via the enqueue seam (NOT hand-written queue rows), then drain ONE document through the real worker path. Capture the `:Canon` count before and after.

```bash
source ./apollo_run_env.sh
# Baseline :Canon count (Neo4j client): MATCH (n:Canon) RETURN count(n) AS before;
```

Enqueue + drain (one-shot, via the real worker functions — do NOT commit this helper):

```python
# scratchpad one-shot: enqueue calculus doc 5, then drain exactly one job.
import asyncio
from apollo.persistence.neo4j_client import Neo4jClient
from apollo.provisioning.enqueue import enqueue_provisioning_job
from apollo.provision_worker import _default_metered_factory, _drain_one, _reap_expired
from database.session import get_async_session

async def main():
    async with get_async_session() as s:
        job_id = await enqueue_provisioning_job(
            s, search_space_id=3, document_id=5, content_hash=None
        )
        await s.commit()
    print("enqueued job", job_id)
    neo = Neo4jClient.from_env()
    try:
        await _reap_expired(get_async_session)
        outcome = await _drain_one(
            neo, session_factory=get_async_session,
            metered_chat_factory=_default_metered_factory,
        )
        print("outcome", outcome)
    finally:
        await neo.close()

asyncio.run(main())
```

`scrape_document` caps the run at `APOLLO_SCRAPE_MAX_SECTIONS` (120) sections × one call each + one triage call — calculus has ~60 sections, so the whole document is bounded to ~$0.05–0.15, well under the 2M ceiling. (The token ceiling remains the ultimate backstop.)

```bash
# After: MATCH (n:Canon) RETURN count(n) AS after;   # expect after > before
```

Expected: a scraped candidate clears stage 4 (`tag_and_mint`, no `TagMintError`), reaches stage 5, promotes to Tier-2 (`apollo_concept_problems.tier == 2` on the real tagged concept), and `project_canon` writes `:Canon` nodes (count increases).

- [ ] **Step 6: Record the run outcome**

Record (in the PR description, and a short note here if iterating): the `run_id`, `n_promoted`, `n_rejected`, and the before/after `:Canon` counts. If candidates reject rather than promote, inspect `apollo_rejected_problems.failed_gate`/`diagnostic` and `apollo_ingest_errors` to confirm each rejection is a legitimate content/lint verdict — NOT a `TagMintError`/`KeyError` abort (which would mean a wiring regression). At least one promotion (or a clearly-explained legitimate-reject run on a verified-thin section set) is the success bar.

---

## Self-Review

**Spec coverage:**
- Approach C (reconstruct → triage → batched section scrape → bounded fallback) → Tasks 2, 3, 4. ✅
- Drop-in at one orchestrator call site, same `ScrapeResult` → Task 5 (stages 2–5 untouched; verified by the unchanged orchestrator suite). ✅
- Idempotency `scrape.<section_hash>.<ordinal>`, deterministic ordinal, no migration → Task 4 (`scrape_section` sort + `model_copy` re-stamp), rides existing `chunk_content_hash` field. ✅
- `APOLLO_SCRAPE_MAX_SECTIONS` / `APOLLO_SCRAPE_MIN_CANDIDATES` / `APOLLO_STRUCTURED_SCRAPE` → Task 1; consumed in Tasks 4–5. ✅
- Concept tag as hint (tag_and_mint authoritative) → Task 3 `concept_slug` flows to candidate; no stage-4 change. ✅
- Error handling: no-heading degrade (Task 2 test), triage fail-open (Task 3 test), per-section fail-soft (Task 4 test), cost-ceiling backstop preserved (orchestrator try/except retained). ✅
- Testing: Tier-1 mocked across Tasks 2–5; real-LLM E2E on calculus doc 5 → Task 6. ✅
- Owner-doc drift contract + `last_verified` → Task 6. ✅

**Placeholder scan:** No TBD/TODO; every code step shows the actual code; every test step shows the assertions and the expected fail/pass.

**Type consistency:** `Section` fields (Task 2) are consumed unchanged by `section_triage` (Task 3) and `scrape_section`/`scrape_document` (Task 4). `SectionVerdict.concept_slug` (Task 3) → `scrape_section(concept_hint=...)` (Task 4). `scrape_document(chunk_rows, *, chat_fn, triage_chat_fn, max_sections, min_candidates, structured)` signature is identical in Task 4 (definition), Task 5 (orchestrator call + test stubs), and Task 5's wiring test. `CandidateQuestion.chunk_content_hash` carries `"<section_hash>.<ordinal>"` and is consumed verbatim by the unchanged `write_tier1_problems` / `_find_tier1_row_id`. `ScrapeResult(candidates, scraped_count, parse_failures)` is returned by both `scrape_document` and the legacy `scrape_questions`.
