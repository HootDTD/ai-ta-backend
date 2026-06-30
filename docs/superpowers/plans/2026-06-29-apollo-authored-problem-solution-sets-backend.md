# Apollo Authored Problem/Solution Sets — Backend Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let teachers upload paired problem/solution docs as "sets"; the backend indexes both, scrapes problems from the problem doc, grounds each problem's reference solution against ONLY its paired solution doc (label-match → doc-scoped retrieval → generate), verifies OCR-suspect references, and promotes trusted ones to tier-2 teachable.

**Architecture:** A scoped variant of the scrape pipeline. The grounding seam is the injectable `retrieve_fn` already threaded through `find_or_generate`/`validate_pair`: a new paired-solution `retrieve_fn` returns spans with `carries_solution=True` to activate the existing extract branch. Docs are indexed via the existing ingestion core (no weekly wrapper) using a new OpenAI-vision OCR provider for handwriting. Trigger is an in-process background task; results are polled. Frontend is a separate plan in `ai-ta-teacher-ui`.

**Tech Stack:** Python 3.11 / FastAPI / SQLAlchemy async + asyncpg / Supabase Postgres + pgvector / Neo4j (tests via Testcontainers `neo4j:5.25`) / OpenAI (vision + embeddings) / pytest.

## Global Constraints

- **Git:** remote `origin` = `https://github.com/HootDTD/ai-ta-backend.git`. Work only on branch `ApolloRun` (tracks `origin/ApolloRun`). After each task's final commit, **push** it: `git push origin ApolloRun`. Never commit/push to `main` or `staging`; never merge. The PR is opened only at the very end (Task 10 / post-implementation) with `gh pr create --base staging` — report the URL + CI and **stop; Ishaan merges every PR himself**. `git user = ishaanbatra`.
- Conventional commits, each ending with a trailer line:
  `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`
- **Ruff (CI-blocking on files added vs `origin/staging`):** run BOTH `ruff check <file>` AND
  `ruff format --check <file>` on every NEW file before committing.
- Never modify `.env`; never install packages without asking; never bypass citation/semantic-filter.
- **Drift contract:** update `docs/architecture/apollo.md` (owns `apollo/**`) and, for Tasks 4–5,
  `docs/architecture/indexing.md` (owns `ocr/**`, `indexing/**`), and for Task 9
  `docs/architecture/_overview.md` (HTTP surface/config) in the SAME commit; bump `last_verified`.
- Migrations: raw SQL in `database/migrations/NNN_*.sql`, applied in numeric order. Next free = **032**.
- `solution_source` ∈ {`extracted`,`generated`,`authored`} (CHECK in SQL). Per-problem outcome ∈
  {`promoted`,`rejected`,`held_for_review`}.
- Tests use the `db_session` fixture from `apollo/conftest.py` (real pgvector via savepoint; Docker-skip
  cleanly). Mirror seed helpers + `_FakeMeteredChat` in `apollo/provisioning/tests/test_orchestrator.py`.
- Hidden-from-student status sentinel: `AITADocument.status = {"state": "apollo_reference"}` (the student
  retrieval gate `active_document_conditions` requires `state == "ready"`). Grounding reads chunks by
  `document_id` and ignores status.
- Spec: `docs/superpowers/specs/2026-06-29-apollo-authored-problem-solution-sets-design.md`.

---

## Execution Protocol (chained Codex sessions)

This plan is implemented **one task per Codex session**. Each session does exactly one task, then hands off to the next session. Follow this every time:

1. **Read the required docs before touching code** (Codex does NOT auto-load `CLAUDE.md`): `CLAUDE.md` (project rules), `docs/shared-architecture/conventions.md`, this plan, the spec (`docs/superpowers/specs/2026-06-29-apollo-authored-problem-solution-sets-design.md`), and the **owner doc(s)** for the files your task touches per the drift contract — `docs/architecture/apollo.md` owns `apollo/**`; `docs/architecture/indexing.md` owns `ocr/**` + `indexing/**`; `docs/architecture/_overview.md` owns config + the HTTP surface.
2. **Pick the task:** the next task = the lowest-numbered task that still has unchecked `- [ ]` steps. Cross-check `git log --oneline` (each finished task ends in a commit).
3. **Implement only that one task**, its steps in order (TDD: failing test → run it fails → implement → run it passes → commit). Do not skip ahead or batch tasks.
4. **Tick the boxes:** as you finish each step, change its `- [ ]` to `- [x]` in this plan file, and include the plan file in that task's commit.
5. **Obey the Global Constraints** below (branch `ApolloRun`; `ruff check` AND `ruff format --check` on every new file; conventional commit + the `Co-Authored-By` trailer shown in each commit step; never push to main; never merge; don't touch `.env`; don't install packages — stop and ask).
6. **Resolve the NOTEs:** where a task says "confirm the name/helper," verify it against the code and make the change; record what it resolved to in your handoff so the next session doesn't re-derive it.
7. **Operational steps** (applying a migration to a remote DB, setting Railway env vars, opening a PR): if you lack access/credentials, **do not guess and never touch prod** — leave the step and flag it in your handoff for the human.
8. **Stop** after the task's final commit. Do **not** start the next task.
9. **Emit the next handoff:** as your final output **in chat (do NOT write it to a file)**, produce a HANDOFF PROMPT for the next Codex session containing:
   - the next task number + title,
   - the plan + spec paths,
   - this execution protocol (so the chain continues),
   - anything you discovered this session the next implementer needs (resolved symbol names, gotchas, plan deviations, and whether DB-backed tests ran or Docker-skipped),
   - the reminder: implement ONLY that one task, then emit the following handoff.

**Stop condition:** if a task's tests can't pass for a reason the plan didn't anticipate, or a NOTE resolves in a way that changes the design, STOP and surface it in the handoff instead of forcing a workaround.

---

## File Structure

**Create:**
- `database/migrations/032_apollo_authored_sets.sql` — pairing table.
- `ocr/openai_vision.py` — `OpenAIVisionOCRProvider(OCRProvider)`.
- `apollo/provisioning/authored_sets/__init__.py`
- `apollo/provisioning/authored_sets/indexing.py` — `index_authored_doc`.
- `apollo/provisioning/authored_sets/label_match.py` — label extraction + solution-label index.
- `apollo/provisioning/authored_sets/paired_retrieval.py` — `make_paired_solution_retrieve_fn`.
- `apollo/provisioning/authored_sets/verification.py` — `verify_against_generated`.
- `apollo/provisioning/authored_sets/orchestrator.py` — `run_authored_set_provisioning` + report types.
- `apollo/provisioning/authored_sets/api.py` — the 4 endpoints + background task (mounted by `apollo/api.py`).
- Tests: `apollo/persistence/tests/test_authored_sets_model.py`, `tests/unit/test_openai_vision_ocr.py`,
  `apollo/provisioning/tests/test_authored_label_match.py`,
  `apollo/provisioning/tests/test_authored_paired_retrieval.py`,
  `apollo/provisioning/tests/test_authored_verification.py`,
  `apollo/provisioning/tests/test_authored_set_orchestrator.py`,
  `apollo/provisioning/tests/test_authored_indexing.py`,
  `apollo/provisioning/tests/test_authored_api.py`.

**Modify:**
- `apollo/persistence/models.py` — add `AuthoredSet`.
- `apollo/provisioning/scrape.py` — add optional `label` to `CandidateQuestion` + prompt note.
- `ocr/factory.py` — select `OpenAIVisionOCRProvider` when `OCR_PROVIDER=openai`.
- `knowledge/teacher_pdf_ingestion.py` — widen the OCR-provider type hint to `OCRProvider`.
- `apollo/api.py` — include the authored-sets router.

---

## Task 1: Pairing persistence (migration 032 + AuthoredSet model)

**Files:**
- Create: `database/migrations/032_apollo_authored_sets.sql`
- Modify: `apollo/persistence/models.py` (add `AuthoredSet`, after `ConceptProblem` ~line 196)
- Test: `apollo/persistence/tests/test_authored_sets_model.py`

**Interfaces:**
- Produces: `AuthoredSet` ORM (`__tablename__="apollo_authored_sets"`) with columns
  `id:int, search_space_id:int, set_index:int, problem_document_id:int|None,
  solution_document_id:int|None, status:str, result_summary:dict, created_at, updated_at`.

- [x] **Step 1: Write the migration**

Create `database/migrations/032_apollo_authored_sets.sql`:

```sql
-- 032_apollo_authored_sets.sql — paired authored problem/solution sets (WU-AAS).
BEGIN;

CREATE TABLE IF NOT EXISTS apollo_authored_sets (
    id                    BIGSERIAL PRIMARY KEY,
    search_space_id       INTEGER NOT NULL REFERENCES aita_search_spaces(id) ON DELETE CASCADE,
    set_index             INTEGER NOT NULL,
    problem_document_id   BIGINT,
    solution_document_id  BIGINT,
    status                TEXT NOT NULL DEFAULT 'pending'
                          CHECK (status IN ('pending','indexing','provisioning','done','failed')),
    result_summary        JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (search_space_id, set_index)
);

CREATE INDEX IF NOT EXISTS apollo_authored_sets_space_idx
    ON apollo_authored_sets(search_space_id);

COMMIT;
```

- [x] **Step 2: Write the failing test**

Create `apollo/persistence/tests/test_authored_sets_model.py`:

```python
import pytest

from apollo.persistence.models import AuthoredSet


@pytest.mark.asyncio
async def test_authored_set_roundtrip(db_session):
    row = AuthoredSet(
        search_space_id=4,
        set_index=1,
        problem_document_id=101,
        solution_document_id=102,
        status="pending",
        result_summary={},
    )
    db_session.add(row)
    await db_session.flush()
    assert row.id is not None

    fetched = await db_session.get(AuthoredSet, row.id)
    assert fetched.search_space_id == 4
    assert fetched.set_index == 1
    assert fetched.solution_document_id == 102
    assert fetched.status == "pending"
    assert fetched.result_summary == {}
```

- [x] **Step 3: Run test to verify it fails**

Run: `pytest apollo/persistence/tests/test_authored_sets_model.py -v`
Expected: FAIL — `ImportError: cannot import name 'AuthoredSet'`.

- [x] **Step 4: Add the model**

In `apollo/persistence/models.py`, mirror the existing column style (`_JSONType`, `BigInteger().with_variant(Integer(), "sqlite")`, `TIMESTAMP(timezone=True)`). Insert after `ConceptProblem`:

```python
class AuthoredSet(Base):
    """Pairing of a problem doc + its solution doc for authored-set provisioning
    (WU-AAS). result_summary holds the bounded per-problem outcome list."""

    __tablename__ = "apollo_authored_sets"

    id = Column(
        BigInteger().with_variant(Integer(), "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    search_space_id = Column(
        Integer,
        ForeignKey("aita_search_spaces.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    set_index = Column(Integer, nullable=False)
    problem_document_id = Column(BigInteger, nullable=True)
    solution_document_id = Column(BigInteger, nullable=True)
    status = Column(Text, nullable=False, server_default=text("'pending'"), default="pending")
    result_summary = Column(_JSONType, nullable=False, server_default=text("'{}'::jsonb"), default=dict)
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(UTC))
    updated_at = Column(TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(UTC))

    __table_args__ = (
        UniqueConstraint("search_space_id", "set_index", name="uq_authored_set_per_space"),
    )
```

Confirm `BigInteger, ForeignKey, UniqueConstraint, Text, text, TIMESTAMP, Integer, datetime, UTC, _JSONType` are imported at the top of the file; add any missing import next to the existing ones.

- [x] **Step 5: Run test to verify it passes**

Run: `pytest apollo/persistence/tests/test_authored_sets_model.py -v`
Expected: PASS (or SKIP if Docker/pgvector is unavailable — acceptable; CI runs it).

- [x] **Step 6: Apply the migration to staging** *(skipped in this session: no `database.run_migrations` module exists, and no staging Supabase apply-migration access/tooling is available; human must apply `032_apollo_authored_sets.sql` to staging `hjevtxdtrkxjcaaexdxt`.)*

Run (against staging `hjevtxdtrkxjcaaexdxt`, per the project's migration runner — confirm the exact command from how `031_*.sql` was applied; do NOT hand-edit prod):
`python -m database.run_migrations` (or the repo's documented runner). Verify `apollo_authored_sets` exists.

- [x] **Step 7: Commit**

```bash
git add database/migrations/032_apollo_authored_sets.sql apollo/persistence/models.py apollo/persistence/tests/test_authored_sets_model.py
ruff check apollo/persistence/tests/test_authored_sets_model.py && ruff format --check apollo/persistence/tests/test_authored_sets_model.py
git commit -m "feat(apollo): apollo_authored_sets table + AuthoredSet model (WU-AAS)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: Scrape captures the printed problem label

**Files:**
- Modify: `apollo/provisioning/scrape.py` (`CandidateQuestion` ~lines 80–96; the scrape system prompt
  text; the record→`CandidateQuestion` mapping in `scrape_questions`/section scrape)
- Test: extend `apollo/provisioning/tests/test_scrape.py`

**Interfaces:**
- Produces: `CandidateQuestion.label: str | None = None` (additive, optional — generic path unaffected).

- [x] **Step 1: Write the failing test**

Add to `apollo/provisioning/tests/test_scrape.py`:

```python
def test_candidate_question_accepts_optional_label():
    from apollo.provisioning.scrape import CandidateQuestion

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
    assert q2.label is None  # backward compatible
```

- [x] **Step 2: Run test to verify it fails**

Run: `pytest apollo/provisioning/tests/test_scrape.py::test_candidate_question_accepts_optional_label -v`
Expected: FAIL — `CandidateQuestion` has no field `label`.

- [x] **Step 3: Add the field + map it from the scraped record**

In `apollo/provisioning/scrape.py`, add to `CandidateQuestion` (after `concept_slug`):

```python
    label: str | None = None  # printed problem label/number, e.g. "Problem 3" (WU-AAS)
```

In the record→`CandidateQuestion` construction (in `scrape_questions`, and the section-scrape builder if separate), pass the label through, tolerating absence:

```python
        label=(str(rec.get("label")).strip() or None) if rec.get("label") else None,
```

In the scrape system prompt string (the `_SCRAPE_SYSTEM_PROMPT` consumed via
`metered_chat.scrape_chat_fn`, defined in `apollo/provisioning/orchestrator.py`), add one instruction
line to the JSON contract:

```
- "label": the problem's printed number/label exactly as shown (e.g. "Problem 3", "Q3", "3."), or null if none.
```

(Confirm where `_SCRAPE_SYSTEM_PROMPT` lives — `grep -rn "_SCRAPE_SYSTEM_PROMPT =" apollo/provisioning/` — and edit that literal. The field is optional, so existing scrape tests that omit `label` still pass.)

- [x] **Step 4: Run tests to verify they pass**

Run: `pytest apollo/provisioning/tests/test_scrape.py -v`
Expected: PASS (new test + all existing scrape tests).

- [x] **Step 5: Commit**

```bash
git add apollo/provisioning/scrape.py apollo/provisioning/orchestrator.py apollo/provisioning/tests/test_scrape.py
git commit -m "feat(apollo): scrape captures optional printed problem label (WU-AAS)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: Label matcher

**Files:**
- Create: `apollo/provisioning/authored_sets/__init__.py` (empty), `apollo/provisioning/authored_sets/label_match.py`
- Test: `apollo/provisioning/tests/test_authored_label_match.py`

**Interfaces:**
- Consumes: `CandidateQuestion` (Task 2) for `extract_problem_label`.
- Produces:
  - `normalize_label(raw: str) -> str | None` — canonical key, e.g. `"Problem 3"`/`"Q3"`/`"3."` → `"3"`.
  - `extract_problem_label(candidate) -> str | None` — prefer `candidate.label`, else regex `candidate.problem_text`.
  - `SolutionChunk` = `tuple[int, str, int | None]` (id, content, page).
  - `build_solution_label_index(chunks: Sequence[SolutionChunk]) -> dict[str, list[SolutionChunk]]`.
  - `match_solution_label(label: str | None, index: dict[str, list[SolutionChunk]]) -> list[SolutionChunk] | None`
    — returns the chunk list for a label with EXACTLY one distinct block; `None` on 0 or ≥2 (fall through).

- [x] **Step 1: Write the failing test**

Create `apollo/provisioning/tests/test_authored_label_match.py`:

```python
from types import SimpleNamespace

from apollo.provisioning.authored_sets.label_match import (
    build_solution_label_index,
    extract_problem_label,
    match_solution_label,
    normalize_label,
)


def test_normalize_label_variants():
    assert normalize_label("Problem 3") == "3"
    assert normalize_label("Q3") == "3"
    assert normalize_label("Question 12") == "12"
    assert normalize_label("3.") == "3"
    assert normalize_label("3)") == "3"
    assert normalize_label("Exercise 4(a)") == "4a"
    assert normalize_label("garbage") is None


def test_extract_problem_label_prefers_scraped_field():
    c = SimpleNamespace(label="Problem 7", problem_text="7. Find the moment ...")
    assert extract_problem_label(c) == "7"
    c2 = SimpleNamespace(label=None, problem_text="12. A cantilever ...")
    assert extract_problem_label(c2) == "12"
    c3 = SimpleNamespace(label=None, problem_text="A cantilever with no number")
    assert extract_problem_label(c3) is None


def test_index_and_match_single_block():
    chunks = [
        (10, "Solution 3\nWe begin by summing moments ...", 2),
        (11, "Problem 4 solution: integrate ...", 3),
    ]
    index = build_solution_label_index(chunks)
    hit = match_solution_label("3", index)
    assert hit is not None and hit[0][0] == 10


def test_match_ambiguous_returns_none():
    chunks = [
        (10, "Solution 3 first copy ...", 1),
        (12, "Solution 3 duplicate appears again ...", 5),
    ]
    index = build_solution_label_index(chunks)
    assert match_solution_label("3", index) is None  # >=2 distinct blocks -> fall through
    assert match_solution_label("99", index) is None  # 0 matches -> fall through
```

- [x] **Step 2: Run test to verify it fails**

Run: `pytest apollo/provisioning/tests/test_authored_label_match.py -v`
Expected: FAIL — module does not exist.

- [x] **Step 3: Implement the module**

Create `apollo/provisioning/authored_sets/__init__.py` (empty) and `apollo/provisioning/authored_sets/label_match.py`:

```python
"""Deterministic problem↔solution label matching for authored sets (WU-AAS).

Maps a scraped problem's printed label to the corresponding labelled block in the
paired solution doc. Ambiguity (0 or ≥2 distinct blocks) returns None so the caller
falls through to doc-scoped retrieval — never a guess."""

from __future__ import annotations

import re
from collections.abc import Sequence

__all__ = [
    "SolutionChunk",
    "normalize_label",
    "extract_problem_label",
    "build_solution_label_index",
    "match_solution_label",
]

SolutionChunk = tuple[int, str, "int | None"]  # (chunk_id, content, page_number)

# Problem-side: a leading label token. Solution-side: same tokens, plus "Solution N".
_LABEL_RE = re.compile(
    r"\b(?:problem|prob|question|ques|q|exercise|ex|solution|sol|ans(?:wer)?)\s*\.?\s*(\d{1,3})\s*(\([a-z]\)|[a-z]\b)?",
    re.IGNORECASE,
)
_LEADING_NUM_RE = re.compile(r"^\s*(\d{1,3})\s*(\([a-z]\)|[a-z])?\s*[.)]")


def normalize_label(raw: str | None) -> str | None:
    """Canonical key: digits + optional sub-part letter, e.g. 'Problem 3'->'3',
    'Exercise 4(a)'->'4a'. None if no number is present."""
    if not raw:
        return None
    m = _LABEL_RE.search(raw) or _LEADING_NUM_RE.match(raw)
    if not m:
        return None
    num = m.group(1)
    sub = (m.group(2) or "").strip("()").lower()
    return f"{num}{sub}" if sub else num


def extract_problem_label(candidate) -> str | None:
    """Prefer the scraped ``label``; else regex the leading token of problem_text."""
    scraped = normalize_label(getattr(candidate, "label", None))
    if scraped:
        return scraped
    return normalize_label(getattr(candidate, "problem_text", "") or "")


def build_solution_label_index(
    chunks: Sequence[SolutionChunk],
) -> dict[str, list[SolutionChunk]]:
    """Map a normalized label -> the distinct chunk(s) whose text opens with /
    contains that solution label. A chunk may register under multiple labels."""
    index: dict[str, list[SolutionChunk]] = {}
    for chunk in chunks:
        _cid, content, _page = chunk
        seen_here: set[str] = set()
        for m in _LABEL_RE.finditer(content or ""):
            key = normalize_label(m.group(0))
            if key and key not in seen_here:
                seen_here.add(key)
                index.setdefault(key, []).append(chunk)
    return index


def match_solution_label(
    label: str | None, index: dict[str, list[SolutionChunk]]
) -> list[SolutionChunk] | None:
    """Return the matched chunk(s) iff EXACTLY one distinct block carries the label;
    None on 0 or ≥2 (the deterministic fail-safe → caller uses retrieval)."""
    if not label:
        return None
    hits = index.get(label)
    if not hits:
        return None
    distinct_ids = {c[0] for c in hits}
    if len(distinct_ids) != 1:
        return None
    return hits
```

- [x] **Step 4: Run test to verify it passes**

Run: `pytest apollo/provisioning/tests/test_authored_label_match.py -v`
Expected: PASS.

- [x] **Step 5: Commit**

```bash
git add apollo/provisioning/authored_sets/__init__.py apollo/provisioning/authored_sets/label_match.py apollo/provisioning/tests/test_authored_label_match.py
ruff check apollo/provisioning/authored_sets/ apollo/provisioning/tests/test_authored_label_match.py && ruff format --check apollo/provisioning/authored_sets/ apollo/provisioning/tests/test_authored_label_match.py
git commit -m "feat(apollo): deterministic problem-solution label matcher (WU-AAS)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 4: OpenAI vision OCR provider

**Files:**
- Create: `ocr/openai_vision.py`
- Modify: `ocr/factory.py` (add `openai` branch), `knowledge/teacher_pdf_ingestion.py` (widen type hint)
- Test: `tests/unit/test_openai_vision_ocr.py`

**Interfaces:**
- Consumes: `ocr.provider.OCRProvider`, `OCRResult`, `OCRBlock`.
- Produces:
  - `OpenAIVisionOCRProvider(OCRProvider)` with `.recognize(image_bytes, mime=None, dpi=None) -> OCRResult`
    and classmethod `.from_env() -> OpenAIVisionOCRProvider`.
  - `ocr.factory.get_ocr_provider_from_env()` returns it when `OCR_PROVIDER=openai`.

- [x] **Step 1: Write the failing test**

Create `tests/unit/test_openai_vision_ocr.py`:

```python
import json
from unittest.mock import MagicMock

from ocr.openai_vision import OpenAIVisionOCRProvider


def _fake_client(payload: dict) -> MagicMock:
    client = MagicMock()
    msg = MagicMock()
    msg.content = json.dumps(payload)
    client.chat.completions.create.return_value = MagicMock(choices=[MagicMock(message=msg)])
    return client


def test_recognize_returns_block_with_confidence():
    client = _fake_client({"text": "x = \\frac{1}{2} g t^2", "confidence": 0.82})
    prov = OpenAIVisionOCRProvider(client=client, model="gpt-4o")
    result = prov.recognize(b"\x89PNG fake", mime="image/png")
    assert result.fused_text == "x = \\frac{1}{2} g t^2"
    assert result.blocks[0].confidence == 0.82
    # the image was sent as a data URL on an image_url content part
    sent = client.chat.completions.create.call_args.kwargs
    parts = sent["messages"][-1]["content"]
    assert any(p.get("type") == "image_url" for p in parts)


def test_recognize_unparseable_response_is_low_confidence_not_crash():
    client = MagicMock()
    msg = MagicMock(); msg.content = "not json"
    client.chat.completions.create.return_value = MagicMock(choices=[MagicMock(message=msg)])
    prov = OpenAIVisionOCRProvider(client=client, model="gpt-4o")
    result = prov.recognize(b"img", mime="image/png")
    assert result.fused_text == ""              # no usable text
    assert (result.average_confidence or 0.0) == 0.0
```

- [x] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_openai_vision_ocr.py -v`
Expected: FAIL — module does not exist.

- [x] **Step 3: Implement the provider**

Create `ocr/openai_vision.py`:

```python
"""OpenAI vision OCR provider (WU-AAS). Transcribes a rendered page image to
text/LaTeX with a self-reported confidence, behind the OCRProvider seam. Chosen for
handwritten solutions; reuses OPENAI_API_KEY. A non-JSON / failed response yields an
empty, zero-confidence result (never raises into ingestion — a degraded page is a
per-page no-op, mirroring the Mathpix provider's fail-soft contract)."""

from __future__ import annotations

import base64
import json
import logging
import os

from ocr.provider import OCRBlock, OCRProvider, OCRResult

_LOG = logging.getLogger(__name__)

_SYSTEM = (
    "You are an OCR engine for math/STEM worksheet and solution pages, including "
    "handwriting. Transcribe ALL visible content faithfully to Markdown with LaTeX for "
    "math ($...$ inline, $$...$$ display). Do NOT solve, summarize, or add anything not "
    "on the page. Respond ONLY as JSON: "
    '{"text": "<transcription>", "confidence": <0..1 how legible/certain you are>}.'
)
_DEFAULT_MODEL = "gpt-4o"


def _data_url(image_bytes: bytes, mime: str | None) -> str:
    enc = base64.b64encode(image_bytes).decode("ascii")
    return f"data:{mime or 'image/png'};base64,{enc}"


class OpenAIVisionOCRProvider(OCRProvider):
    def __init__(self, *, client=None, model: str | None = None) -> None:
        self._client = client
        self._model = model or _DEFAULT_MODEL

    @classmethod
    def from_env(cls) -> "OpenAIVisionOCRProvider":
        model = (os.getenv("APOLLO_OCR_MODEL") or _DEFAULT_MODEL).strip()
        return cls(client=None, model=model)

    def _ensure_client(self):
        if self._client is None:
            from openai import OpenAI

            self._client = OpenAI()
        return self._client

    def recognize(self, image_bytes: bytes, mime: str | None = None, dpi: int | None = None) -> OCRResult:
        try:
            client = self._ensure_client()
            resp = client.chat.completions.create(
                model=self._model,
                temperature=0.0,
                response_format={"type": "json_object"},
                messages=[
                    {"role": "system", "content": _SYSTEM},
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "Transcribe this page."},
                            {"type": "image_url", "image_url": {"url": _data_url(image_bytes, mime)}},
                        ],
                    },
                ],
            )
            raw = resp.choices[0].message.content or ""
            data = json.loads(raw)
            text = str(data.get("text") or "").strip()
            conf = data.get("confidence")
            conf = float(conf) if isinstance(conf, (int, float)) else 0.0
            conf = max(0.0, min(1.0, conf))
        except Exception as exc:  # noqa: BLE001 — fail-soft per the OCRProvider contract
            _LOG.warning("openai_vision_ocr_failed", extra={"event": "openai_vision_ocr_failed", "error": str(exc)})
            return OCRResult(blocks=[])
        if not text:
            return OCRResult(blocks=[])
        return OCRResult(blocks=[OCRBlock(kind="latex", text=text, confidence=conf)])
```

- [x] **Step 4: Wire the factory + widen the ingestor type hint**

In `ocr/factory.py`, extend `get_ocr_provider_from_env()`:

```python
    if provider == "openai":
        from .openai_vision import OpenAIVisionOCRProvider

        return OpenAIVisionOCRProvider.from_env()
```

In `knowledge/teacher_pdf_ingestion.py`, widen the type hint (non-breaking; keep the param NAME
`mathpix_provider` so the weekly caller is unaffected — the ingestor only calls `.recognize()`):

```python
from ocr.provider import OCRProvider  # add near the existing `from ocr.mathpix import ...`
...
    def __init__(
        self,
        config: Optional[TeacherPDFIngestionConfig] = None,
        *,
        mathpix_provider: Optional[OCRProvider] = None,  # any OCRProvider; only .recognize() is used
    ) -> None:
```

- [x] **Step 5: Run tests to verify they pass**

Run: `pytest tests/unit/test_openai_vision_ocr.py -v`
Expected: PASS. Also run `pytest tests/functions-tests/test_teacher_pdf_ingestion.py -v` to confirm the
type-hint widening broke nothing.

- [x] **Step 6: Commit**

```bash
git add ocr/openai_vision.py ocr/factory.py knowledge/teacher_pdf_ingestion.py tests/unit/test_openai_vision_ocr.py
ruff check ocr/openai_vision.py tests/unit/test_openai_vision_ocr.py && ruff format --check ocr/openai_vision.py tests/unit/test_openai_vision_ocr.py
git commit -m "feat(ocr): OpenAI vision OCR provider for handwriting (WU-AAS)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 5: Authored-set indexer

**Files:**
- Create: `apollo/provisioning/authored_sets/indexing.py`
- Test: `apollo/provisioning/tests/test_authored_indexing.py`

**Interfaces:**
- Consumes: `TeacherPDFIngestor`, `get_ocr_provider_from_env` (Task 4), the indexing core.
- Produces: `async index_authored_doc(db, *, search_space_id, file_bytes, title, set_index, role) -> int`
  (returns `AITADocument.id`; sets status `{"state": "apollo_reference"}`; deterministic `unique_id`
  per `(search_space_id, set_index, role)` so re-upload reuses the row).

- [x] **Step 1: Write the failing test (logic-level, OCR + heavy IO mocked)**

Create `apollo/provisioning/tests/test_authored_indexing.py`. This test asserts the orchestration wiring
by monkeypatching the ingestion + indexing primitives (the real PDF path is covered by the ss=4 E2E):

```python
import types

import pytest

import apollo.provisioning.authored_sets.indexing as idx


@pytest.mark.asyncio
async def test_index_authored_doc_sets_hidden_status(db_session, monkeypatch):
    # Fake ingestion result
    fake_ing = types.SimpleNamespace(
        items=[types.SimpleNamespace(id="i1")],
        source_markdown="Problem 1 ...",
        page_count=2,
        pages=[],
        artifact_manifest={"pages": []},
        ocr_provider="openai",
        ocr_summary={"openai_pages": 2},
        warning_count=0,
        warnings=[],
    )
    monkeypatch.setattr(idx, "_run_ingest", lambda *a, **k: fake_ing)
    monkeypatch.setattr(idx, "items_to_chunk_texts", lambda items: [("Problem 1 ...", {"page_number": 1, "chunk_type": "body"})])

    # Stub the indexing primitives to return a created doc id and no-op persist/finalize
    async def fake_prepare(self, docs):
        from database.models import AITADocument
        doc = AITADocument(title=docs[0].title, search_space_id=docs[0].search_space_id,
                           content="c", content_hash="h", unique_identifier_hash="u")
        db_session.add(doc); await db_session.flush()
        return [doc]
    monkeypatch.setattr(idx.AITAIndexingService, "prepare_for_indexing", fake_prepare)
    async def fake_embed(**k): return 1
    monkeypatch.setattr(idx, "embed_and_persist_chunks", fake_embed)
    async def fake_finalize(session, **k): return None
    monkeypatch.setattr(idx, "finalize_document", fake_finalize)
    monkeypatch.setattr(idx, "embed_text", lambda t: [0.0] * 8)

    doc_id = await idx.index_authored_doc(
        db_session, search_space_id=4, file_bytes=b"%PDF-1.4 fake",
        title="HW7 Problems", set_index=1, role="problem",
    )
    from database.models import AITADocument
    doc = await db_session.get(AITADocument, doc_id)
    assert doc.status == {"state": "apollo_reference"}
```

- [x] **Step 2: Run test to verify it fails**

Run: `pytest apollo/provisioning/tests/test_authored_indexing.py -v`
Expected: FAIL — module/`index_authored_doc` does not exist.

- [x] **Step 3: Implement the indexer**

Create `apollo/provisioning/authored_sets/indexing.py` (the `_run_ingest` seam exists so the test can
stub the synchronous, PyMuPDF-bound ingest without a real PDF):

```python
"""Index an authored problem/solution PDF into AITADocument + aita_chunks (WU-AAS).

Reuses the ingestion CORE (TeacherPDFIngestor + prepare_for_indexing +
embed_and_persist_chunks + finalize_document) but NONE of the weekly wrapper: no
TeacherUpload row, no supersede, no week activation, no generic auto-enqueue. The doc
is marked hidden from student RAG (status state != 'ready'); grounding reads chunks by
document_id and ignores status. Deterministic unique_id per (space, set, role) makes
re-upload idempotent."""

from __future__ import annotations

import logging
import shutil
import tempfile
import uuid
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from database.models import AITADocument
from database.session import get_async_session
from indexing.checkpoint_indexer import (
    build_doc_content,
    embed_and_persist_chunks,
    finalize_document,
)
from indexing.connector_document import AITAConnectorDocument
from indexing.document_chunker import items_to_chunk_texts
from indexing.document_embedder import embed_text
from indexing.indexing_service import AITAIndexingService

_LOG = logging.getLogger(__name__)

_HIDDEN_STATUS = {"state": "apollo_reference"}


def _run_ingest(pdf_path: Path, *, doc_id: str):
    """Synchronous PyMuPDF ingest, isolated for testability. Uses the env-selected
    OCR provider (OCR_PROVIDER=openai → OpenAI vision; see ocr/factory.py)."""
    from knowledge.teacher_pdf_ingestion import TeacherPDFIngestor
    from ocr import get_ocr_provider_from_env

    ingestor = TeacherPDFIngestor(mathpix_provider=get_ocr_provider_from_env())
    return ingestor.ingest(pdf_path, doc_id=doc_id, upload_page_asset=None)


async def index_authored_doc(
    db: AsyncSession,
    *,
    search_space_id: int,
    file_bytes: bytes,
    title: str,
    set_index: int,
    role: str,
) -> int:
    """Index raw PDF bytes; return the AITADocument id. Caller commits ``db``."""
    work_dir = Path(tempfile.mkdtemp(prefix="authored_idx_"))
    pdf_path = work_dir / "document.pdf"
    pdf_path.write_bytes(file_bytes)
    try:
        ingestion = _run_ingest(pdf_path, doc_id=f"authored:{search_space_id}:{set_index}:{role}")
        if not ingestion.items:
            raise ValueError(f"authored indexer: no chunks produced from {role} PDF")

        connector_doc = AITAConnectorDocument(
            title=title,
            source_markdown=ingestion.source_markdown or title,
            unique_id=f"authored-set:{search_space_id}:{set_index}:{role}",
            document_type="EDUCATIONAL_FILE",
            search_space_id=search_space_id,
            material_kind="other",
            should_summarize=False,
            page_count=ingestion.page_count,
            week=None,
            metadata={
                "authored_role": role,
                "authored_set_index": set_index,
                "ocr_provider": ingestion.ocr_provider,
                "ocr_summary": ingestion.ocr_summary,
                "warning_count": ingestion.warning_count,
                "artifact_manifest": ingestion.artifact_manifest,
            },
        )

        service = AITAIndexingService(db)
        docs = await service.prepare_for_indexing([connector_doc])
        if not docs:
            # Idempotent re-upload (same content_hash) — reuse the existing row.
            existing = await _find_doc_by_unique_id(db, connector_doc.unique_id)
            if existing is None:
                raise RuntimeError("authored indexer: prepare_for_indexing returned no doc")
            existing.status = dict(_HIDDEN_STATUS)
            await db.flush()
            return int(existing.id)

        document_id = int(docs[0].id)
        # Make the new row visible to embed_and_persist_chunks' own sessions.
        await db.commit()

        chunk_pairs = items_to_chunk_texts(ingestion.items)
        if not chunk_pairs:
            raise ValueError(f"authored indexer: no chunk texts from {role} items")

        await embed_and_persist_chunks(
            session_factory=get_async_session,
            document_id=document_id,
            chunk_pairs=chunk_pairs,
            after_page=0,
        )

        doc_content = build_doc_content(chunk_pairs, fallback_title=title)
        doc_embedding = embed_text(doc_content)
        await finalize_document(
            db,
            document_id=document_id,
            chunk_pairs=chunk_pairs,
            doc_content=doc_content,
            doc_embedding=doc_embedding,
            page_count=ingestion.page_count,
        )
        doc = await db.get(AITADocument, document_id)
        doc.status = dict(_HIDDEN_STATUS)  # override finalize's {"state": "ready"}
        await db.flush()
        return document_id
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


async def _find_doc_by_unique_id(db: AsyncSession, unique_id: str) -> AITADocument | None:
    from sqlalchemy import select

    from indexing.document_hashing import unique_identifier_hash  # confirm helper name

    h = unique_identifier_hash(unique_id)
    return (
        await db.execute(select(AITADocument).where(AITADocument.unique_identifier_hash == h))
    ).scalar_one_or_none()
```

> NOTE for the implementer: confirm the hashing helper in `indexing/document_hashing.py`
> (`grep -n "def " indexing/document_hashing.py`) and adjust `_find_doc_by_unique_id` to use the SAME
> hash `prepare_for_indexing` writes to `unique_identifier_hash`. If the names differ, match them.

- [x] **Step 4: Run test to verify it passes**

Run: `pytest apollo/provisioning/tests/test_authored_indexing.py -v`
Expected: PASS.

- [x] **Step 5: Commit**

```bash
git add apollo/provisioning/authored_sets/indexing.py apollo/provisioning/tests/test_authored_indexing.py
ruff check apollo/provisioning/authored_sets/indexing.py apollo/provisioning/tests/test_authored_indexing.py && ruff format --check apollo/provisioning/authored_sets/indexing.py apollo/provisioning/tests/test_authored_indexing.py
git commit -m "feat(apollo): authored-set PDF indexer (reuse core, no weekly wrapper) (WU-AAS)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 6: Paired-solution retrieve_fn (doc-scoped + label)

**Files:**
- Create: `apollo/provisioning/authored_sets/paired_retrieval.py`
- Test: `apollo/provisioning/tests/test_authored_paired_retrieval.py`

**Interfaces:**
- Consumes: `label_match` (Task 3), `GroundingSpan` (`apollo.provisioning.solution`),
  `chunk_content_hash` (`apollo.provisioning.scrape`), `_halfvec_cosine_distance`
  (`retrieval.hybrid_search`), `embed_text`, `AITAChunk`, `AITADocument`.
- Produces:
  - `async load_solution_chunks(db, *, solution_document_id) -> list[SolutionChunk]`
  - `chunk_ocr_confidence(db, *, document_id) -> dict[int|None, float|None]` (page → conf, from doc metadata)
  - `make_paired_solution_retrieve_fn(db, *, solution_document_id, label_index, page_conf, top_k=6)
    -> retrieve(question)` returning `tuple[GroundingSpan, ...]` with `carries_solution=True` and a
    `match_method`/`ocr_confidence`-bearing `provenance`-style attribute on each span via `GroundingSpan`
    fields (`document_id`, `page`, `chunk_content_hash`). The min OCR confidence is returned alongside
    via a closure attribute `retrieve.last_min_conf` (read by Task 7), OR recomputed by the caller from
    `page_conf` + returned span pages. **MUST NOT call `active_document_conditions`.**

- [x] **Step 1: Write the failing test**

Create `apollo/provisioning/tests/test_authored_paired_retrieval.py`:

```python
from types import SimpleNamespace

import pytest

from apollo.provisioning.authored_sets.label_match import build_solution_label_index
from apollo.provisioning.authored_sets.paired_retrieval import make_paired_solution_retrieve_fn


@pytest.mark.asyncio
async def test_label_branch_returns_carries_solution_span():
    chunks = [(10, "Solution 3\nSum moments: M = wL^2/8", 2)]
    index = build_solution_label_index(chunks)
    page_conf = {2: 0.91}
    retrieve = make_paired_solution_retrieve_fn(
        db=None, solution_document_id=55, label_index=index, page_conf=page_conf,
        _chunk_lookup={10: (10, "Solution 3\nSum moments: M = wL^2/8", 2)},
    )
    q = SimpleNamespace(label="Problem 3", problem_text="3. beam ...")
    spans = await retrieve(q)
    assert len(spans) == 1
    assert spans[0].carries_solution is True
    assert spans[0].document_id == 55
    assert spans[0].page == 2
    assert retrieve.last_min_conf == 0.91


@pytest.mark.asyncio
async def test_no_label_no_retrieval_hits_returns_empty(monkeypatch):
    index = {}
    retrieve = make_paired_solution_retrieve_fn(
        db=None, solution_document_id=55, label_index=index, page_conf={},
    )
    # force the retrieval branch to return nothing
    async def _empty(_db, _doc, _q, _k): return []
    monkeypatch.setattr(
        "apollo.provisioning.authored_sets.paired_retrieval._doc_scoped_semantic", _empty
    )
    q = SimpleNamespace(label=None, problem_text="unlabelled problem")
    spans = await retrieve(q)
    assert spans == ()
    assert retrieve.last_min_conf is None
```

- [x] **Step 2: Run test to verify it fails**

Run: `pytest apollo/provisioning/tests/test_authored_paired_retrieval.py -v`
Expected: FAIL — module does not exist.

- [x] **Step 3: Implement the module**

Create `apollo/provisioning/authored_sets/paired_retrieval.py`:

```python
"""Doc-scoped grounding for authored sets (WU-AAS).

The returned ``retrieve(question)`` grounds against ONLY the paired solution doc —
label-match first, else doc-scoped semantic top-k — and marks every span
``carries_solution=True`` so ``find_or_generate`` takes its EXTRACT branch
(``solution_source='extracted'``). It NEVER uses ``active_document_conditions`` (the
week-gated/whole-course path); it filters ``aita_chunks.document_id`` directly. Empty
grounding is honest (→ generate)."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apollo.provisioning.authored_sets.label_match import (
    SolutionChunk,
    build_solution_label_index,
    extract_problem_label,
    match_solution_label,
)
from apollo.provisioning.scrape import chunk_content_hash
from apollo.provisioning.solution import GroundingSpan

_LOG = logging.getLogger(__name__)

DEFAULT_PAIRED_TOP_K = 6


async def load_solution_chunks(db: AsyncSession, *, solution_document_id: int) -> list[SolutionChunk]:
    from database.models import AITAChunk

    rows = (
        await db.execute(
            select(AITAChunk.id, AITAChunk.content, AITAChunk.page_number)
            .where(AITAChunk.document_id == solution_document_id)
            .order_by(AITAChunk.id.asc())
        )
    ).all()
    return [(int(r.id), r.content or "", r.page_number) for r in rows]


async def page_ocr_confidence(db: AsyncSession, *, document_id: int) -> dict:
    """page_number -> ocr_confidence, read from AITADocument.document_metadata.page_debug."""
    from database.models import AITADocument

    doc = await db.get(AITADocument, document_id)
    meta = dict(getattr(doc, "document_metadata", None) or {})
    out: dict = {}
    for entry in meta.get("page_debug") or []:
        try:
            out[int(entry.get("page"))] = entry.get("ocr_confidence")
        except (TypeError, ValueError):
            continue
    return out


async def _doc_scoped_semantic(
    db: AsyncSession, solution_document_id: int, query_text: str, top_k: int
) -> list[SolutionChunk]:
    """Semantic top-k over ONE doc's chunks (no week gate, no fusion needed for a
    single small doc). Uses the codebase's proven halfvec cosine expression."""
    from database.models import AITAChunk
    from indexing.document_embedder import embed_text
    from retrieval.hybrid_search import _halfvec_cosine_distance

    qemb = embed_text(query_text)
    distance = _halfvec_cosine_distance(qemb)
    rows = (
        await db.execute(
            select(AITAChunk.id, AITAChunk.content, AITAChunk.page_number)
            .where(AITAChunk.document_id == solution_document_id)
            .order_by(distance)
            .limit(top_k)
        )
    ).all()
    return [(int(r.id), r.content or "", r.page_number) for r in rows]


def _spans_from_chunks(
    chunks: Sequence[SolutionChunk], *, solution_document_id: int, page_conf: dict
) -> tuple[tuple[GroundingSpan, ...], float | None]:
    spans = tuple(
        GroundingSpan(
            text=content,
            document_id=solution_document_id,
            page=page,
            chunk_content_hash=chunk_content_hash(content),
            carries_solution=True,
        )
        for (_cid, content, page) in chunks
        if (content or "").strip()
    )
    confs = [page_conf.get(page) for (_c, _t, page) in chunks if page_conf.get(page) is not None]
    min_conf = min(confs) if confs else None
    return spans, min_conf


def make_paired_solution_retrieve_fn(
    db: AsyncSession,
    *,
    solution_document_id: int,
    label_index: dict[str, list[SolutionChunk]],
    page_conf: dict,
    top_k: int = DEFAULT_PAIRED_TOP_K,
    _chunk_lookup: dict | None = None,
) -> Callable[..., Awaitable[tuple[GroundingSpan, ...]]]:
    """Build ``retrieve(question)``. Exposes ``retrieve.last_min_conf`` (min OCR
    confidence of the grounding used on the most recent call; None when no spans)."""

    async def retrieve(question) -> tuple[GroundingSpan, ...]:
        label = extract_problem_label(question)
        matched = match_solution_label(label, label_index)
        if matched is not None:
            spans, min_conf = _spans_from_chunks(
                matched, solution_document_id=solution_document_id, page_conf=page_conf
            )
            retrieve.last_min_conf = min_conf
            retrieve.last_match_method = "label"
            if spans:
                return spans

        query_text = getattr(question, "problem_text", "") or ""
        hits = await _doc_scoped_semantic(db, solution_document_id, query_text, top_k)
        spans, min_conf = _spans_from_chunks(
            hits, solution_document_id=solution_document_id, page_conf=page_conf
        )
        retrieve.last_min_conf = min_conf if spans else None
        retrieve.last_match_method = "retrieval" if spans else None
        return spans

    retrieve.last_min_conf = None
    retrieve.last_match_method = None
    return retrieve
```

> For the unit test's `_chunk_lookup` shortcut: the label branch uses the `matched` chunks directly
> (already full `SolutionChunk` tuples from the index), so `_doc_scoped_semantic` is only hit on the
> retrieval branch. The `_chunk_lookup` param in the test is illustrative; if unused by the final
> implementation, drop it from the test signature so the test calls `make_paired_solution_retrieve_fn`
> with the real kwargs only.

- [x] **Step 4: Run test to verify it passes**

Run: `pytest apollo/provisioning/tests/test_authored_paired_retrieval.py -v`
Expected: PASS. (The label-branch test needs no DB. Add a `db_session`-backed test that seeds two
chunks in one doc and asserts the retrieval branch returns `carries_solution=True` spans scoped to that
`document_id` only.)

- [x] **Step 5: Commit**

```bash
git add apollo/provisioning/authored_sets/paired_retrieval.py apollo/provisioning/tests/test_authored_paired_retrieval.py
ruff check apollo/provisioning/authored_sets/paired_retrieval.py apollo/provisioning/tests/test_authored_paired_retrieval.py && ruff format --check apollo/provisioning/authored_sets/paired_retrieval.py apollo/provisioning/tests/test_authored_paired_retrieval.py
git commit -m "feat(apollo): doc-scoped paired-solution retrieve_fn (label + semantic) (WU-AAS)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 7: OCR-confidence verification (generate-and-compare)

**Files:**
- Create: `apollo/provisioning/authored_sets/verification.py`
- Test: `apollo/provisioning/tests/test_authored_verification.py`

**Interfaces:**
- Consumes: `find_or_generate`, `ReferenceSolutionDraft` (`apollo.provisioning.solution`); `MeteredChat`.
- Produces:
  - `VerificationVerdict` (pydantic): `review_required: bool`, `reason: str | None`,
    `generated_alt: dict | None` (the generated draft as `.model_dump()`), `ocr_confidence: float | None`,
    `match_method: str | None`.
  - `async verify_against_generated(db, *, candidate, draft, min_conf, problem_low_conf, match_method,
    metered_chat, conf_threshold) -> VerificationVerdict`.

- [x] **Step 1: Write the failing test**

Create `apollo/provisioning/tests/test_authored_verification.py`:

```python
from types import SimpleNamespace

import pytest

from apollo.provisioning.authored_sets.verification import verify_against_generated
from apollo.provisioning.solution import ReferenceSolutionDraft


def _draft(answer):
    return ReferenceSolutionDraft(
        solution_source="extracted",
        reference_solution=[{"entry_type": "answer", "symbolic": f"x = {answer}"}],
        grounding=(),
        provenance={},
    )


class _FakeMC:
    def __init__(self, equivalent: bool):
        self._eq = equivalent

    def main(self, **k):  # generate path → a fixed generated draft
        import json
        return json.dumps({"reference_solution": [{"entry_type": "answer", "symbolic": "x = 5"}]})

    def cheap(self, **k):  # comparison judge
        import json
        return json.dumps({"equivalent": self._eq, "reason": "n/a"})


@pytest.mark.asyncio
async def test_high_confidence_skips_verification():
    v = await verify_against_generated(
        db=None, candidate=SimpleNamespace(problem_text="p"), draft=_draft("5"),
        min_conf=0.95, problem_low_conf=False, match_method="label",
        metered_chat=_FakeMC(True), conf_threshold=0.6,
    )
    assert v.review_required is False
    assert v.generated_alt is None


@pytest.mark.asyncio
async def test_low_confidence_divergence_flags(monkeypatch):
    # Make the independent generate produce a divergent answer; judge says not equivalent.
    import apollo.provisioning.authored_sets.verification as ver

    async def fake_generate(*a, **k):
        return _draft("999")  # different final answer
    monkeypatch.setattr(ver, "_independent_generate", fake_generate)

    v = await verify_against_generated(
        db=None, candidate=SimpleNamespace(problem_text="p"), draft=_draft("5"),
        min_conf=0.30, problem_low_conf=False, match_method="retrieval",
        metered_chat=_FakeMC(False), conf_threshold=0.6,
    )
    assert v.review_required is True
    assert v.reason == "ocr_divergence"
    assert v.generated_alt is not None


@pytest.mark.asyncio
async def test_low_confidence_agreement_trusts(monkeypatch):
    import apollo.provisioning.authored_sets.verification as ver

    async def fake_generate(*a, **k):
        return _draft("5")
    monkeypatch.setattr(ver, "_independent_generate", fake_generate)

    v = await verify_against_generated(
        db=None, candidate=SimpleNamespace(problem_text="p"), draft=_draft("5"),
        min_conf=0.30, problem_low_conf=False, match_method="label",
        metered_chat=_FakeMC(True), conf_threshold=0.6,
    )
    assert v.review_required is False
```

- [x] **Step 2: Run test to verify it fails**

Run: `pytest apollo/provisioning/tests/test_authored_verification.py -v`
Expected: FAIL — module does not exist.

- [x] **Step 3: Implement the module**

Create `apollo/provisioning/authored_sets/verification.py`:

```python
"""Low-OCR-confidence cross-check for authored references (WU-AAS).

When the extracted reference's grounding OCR'd below threshold (or the problem doc is
itself low-confidence), generate an INDEPENDENT solution and compare. Material
divergence (different final answer / contradictory core equation) → flag for review;
agreement → trust. High-confidence extractions skip this entirely (cost control)."""

from __future__ import annotations

import json
import logging

from pydantic import BaseModel

from apollo.provisioning.solution import ReferenceSolutionDraft, find_or_generate

_LOG = logging.getLogger(__name__)

_COMPARE_SYSTEM = (
    "You compare two worked solutions to the SAME problem. Decide if they are "
    "MATERIALLY equivalent: same final answer AND no directly contradictory core "
    "equation. Different-but-valid procedures that reach the same answer ARE "
    'equivalent. Respond ONLY as JSON: {"equivalent": <bool>, "reason": "<short>"}.'
)


class VerificationVerdict(BaseModel):
    review_required: bool
    reason: str | None = None
    generated_alt: dict | None = None
    ocr_confidence: float | None = None
    match_method: str | None = None


async def _empty_retrieve(_question):
    return ()


async def _independent_generate(db, candidate, *, chat_fn) -> ReferenceSolutionDraft:
    """Generate a reference from the problem alone (empty grounding → generate
    branch), fully independent of the OCR'd solution text."""
    return await find_or_generate(db, candidate, retrieve_fn=_empty_retrieve, chat_fn=chat_fn)


def _final_answer(draft: ReferenceSolutionDraft) -> str:
    for step in reversed(draft.reference_solution or []):
        sym = (step.get("symbolic") or "").strip()
        if sym:
            return sym
    return ""


async def verify_against_generated(
    db,
    *,
    candidate,
    draft: ReferenceSolutionDraft,
    min_conf: float | None,
    problem_low_conf: bool,
    match_method: str | None,
    metered_chat,
    conf_threshold: float,
) -> VerificationVerdict:
    low = (min_conf is not None and min_conf < conf_threshold) or problem_low_conf
    base = VerificationVerdict(review_required=False, ocr_confidence=min_conf, match_method=match_method)
    if not low:
        return base

    generated = await _independent_generate(db, candidate, chat_fn=metered_chat.main)

    # Cheap structural shortcut: identical normalized final answers → trust w/o judge.
    if _final_answer(generated) and _final_answer(generated) == _final_answer(draft):
        return base

    raw = metered_chat.cheap(
        purpose="authored_ocr_compare",
        messages=[
            {"role": "system", "content": _COMPARE_SYSTEM},
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "problem_text": getattr(candidate, "problem_text", ""),
                        "solution_a_extracted": draft.reference_solution,
                        "solution_b_generated": generated.reference_solution,
                    }
                ),
            },
        ],
        response_format={"type": "json_object"},
    )
    try:
        equivalent = bool(json.loads(raw).get("equivalent"))
    except (json.JSONDecodeError, TypeError, AttributeError):
        equivalent = False  # fail-closed → flag

    if equivalent:
        return base
    return VerificationVerdict(
        review_required=True,
        reason="ocr_divergence",
        generated_alt=generated.model_dump(),
        ocr_confidence=min_conf,
        match_method=match_method,
    )
```

- [x] **Step 4: Run test to verify it passes**

Run: `pytest apollo/provisioning/tests/test_authored_verification.py -v`
Expected: PASS.

- [x] **Step 5: Commit**

```bash
git add apollo/provisioning/authored_sets/verification.py apollo/provisioning/tests/test_authored_verification.py
ruff check apollo/provisioning/authored_sets/verification.py apollo/provisioning/tests/test_authored_verification.py && ruff format --check apollo/provisioning/authored_sets/verification.py apollo/provisioning/tests/test_authored_verification.py
git commit -m "feat(apollo): low-OCR-confidence generate-and-compare verification (WU-AAS)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 8: Orchestration (`run_authored_set_provisioning`)

**Files:**
- Create: `apollo/provisioning/authored_sets/orchestrator.py`
- Test: `apollo/provisioning/tests/test_authored_set_orchestrator.py`

**Interfaces:**
- Consumes: Tasks 3/6/7; `scrape_document`, `write_tier1_problems`,
  `resolve_or_create_provisional_concept` (`apollo.provisioning.scrape`); `find_or_generate`,
  `build_approved_pair` (`apollo.provisioning.solution`); `validate_pair`, `rejection_from_verdict`
  (`apollo.provisioning.pairing_gate`); `tag_and_mint` (`apollo.provisioning.tag_mint`); `promote`
  (`apollo.provisioning.promote`); `MeteredChat`; `problem_dup_hash` (`apollo.provisioning.problem_hash`);
  `ConceptProblem` (`apollo.persistence.models`).
- Produces:
  - `ProblemResult` (pydantic): `label, outcome ('promoted'|'rejected'|'held_for_review'),
    solution_source, match_method, ocr_confidence, failed_gate, diagnostic, review_required, reason,
    concept_problem_id`.
  - `ProvisioningReport` (pydantic): `problems: list[ProblemResult]`, `counts: dict`.
  - `async run_authored_set_provisioning(db, neo, *, search_space_id, problem_document_id,
    solution_document_id, metered_chat, embed_fn=None, conf_threshold=...) -> ProvisioningReport`.

- [ ] **Step 1: Write the failing test (happy path: label-extract → promote)**

Create `apollo/provisioning/tests/test_authored_set_orchestrator.py`. Reuse the seed helpers and
`_FakeMeteredChat` from `test_orchestrator.py` (import or copy them), seed a problem doc with one chunk
and a solution doc with a matching `Solution 1` chunk, then:

```python
import pytest

from apollo.provisioning.authored_sets.orchestrator import run_authored_set_provisioning
# Reuse helpers: _seed_search_space, _seed_chunk, _FakeMeteredChat  (mirror test_orchestrator.py)


@pytest.mark.asyncio
async def test_authored_set_label_extract_promotes(db_session, neo4j_client, monkeypatch):
    space = await _seed_search_space(db_session, slug="aas1")
    prob_doc = await _seed_doc_with_chunk(db_session, space, "1. A beam length L, load w. Find max moment M.")
    sol_doc = await _seed_doc_with_chunk(
        db_session, space, "Solution 1\nM = w*L^2/8 by summing moments.", ocr_conf=0.95,
    )

    report = await run_authored_set_provisioning(
        db_session, neo4j_client,
        search_space_id=space,
        problem_document_id=prob_doc,
        solution_document_id=sol_doc,
        metered_chat=_FakeAuthoredMC(),  # scrape→1 candidate w/ label "1"; solution_extract→a valid graph; judges→approve
    )
    assert len(report.problems) == 1
    r = report.problems[0]
    assert r.solution_source == "extracted"
    assert r.match_method == "label"
    assert r.outcome in {"promoted", "rejected"}  # depends on lint; assert promoted with a clean fake graph
```

Add a second test: a low-confidence solution chunk (`ocr_conf=0.2`) with a divergent fake generate →
`outcome == "held_for_review"`, `review_required is True`, and assert NO Neo4j `:Canon` node was minted
(query `neo4j_client`), proving the hold defers KG mutation.

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest apollo/provisioning/tests/test_authored_set_orchestrator.py -v`
Expected: FAIL — module does not exist.

- [ ] **Step 3: Implement the orchestrator**

Create `apollo/provisioning/authored_sets/orchestrator.py`:

```python
"""Authored-set provisioning (WU-AAS): scrape the problem doc, ground each problem
against ONLY the paired solution doc, verify OCR-suspect references, promote trusted
ones. A sibling of the generic run_provisioning that swaps the grounding source and
adds the 'held_for_review' outcome. Trigger-agnostic (called by the in-process
background task today; a worker later)."""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apollo.persistence.models import ConceptProblem
from apollo.provisioning.authored_sets.label_match import build_solution_label_index
from apollo.provisioning.authored_sets.paired_retrieval import (
    load_solution_chunks,
    make_paired_solution_retrieve_fn,
    page_ocr_confidence,
)
from apollo.provisioning.authored_sets.verification import verify_against_generated
from apollo.provisioning.pairing_gate import rejection_from_verdict, validate_pair
from apollo.provisioning.problem_hash import problem_dup_hash
from apollo.provisioning.promote import promote
from apollo.provisioning.provisioning_schema import build_tag_schema
from apollo.provisioning.scrape import (
    resolve_or_create_provisional_concept,
    scrape_document,
    write_tier1_problems,
)
from apollo.provisioning.solution import (
    SolutionDraftError,
    build_approved_pair,
    find_or_generate,
)
from apollo.provisioning.tag_mint import tag_and_mint

_LOG = logging.getLogger(__name__)

_DEFAULT_CONF_THRESHOLD = 0.6
# Reuse the same prompts/limits the generic path uses (import the module-level literals
# from apollo.provisioning.orchestrator to stay DRY):
from apollo.provisioning.orchestrator import (  # noqa: E402
    APOLLO_SCRAPE_MAX_SECTIONS,
    APOLLO_SCRAPE_MIN_CANDIDATES,
    _SCRAPE_SYSTEM_PROMPT,
    _TAG_MINT_SYSTEM_PROMPT,
    _TRIAGE_SYSTEM_PROMPT,
    structured_scrape_enabled,
)


class ProblemResult(BaseModel):
    label: str | None = None
    outcome: str  # promoted | rejected | held_for_review
    solution_source: str | None = None
    match_method: str | None = None
    ocr_confidence: float | None = None
    failed_gate: int | None = None
    diagnostic: str = ""
    review_required: bool = False
    reason: str | None = None
    concept_problem_id: int | None = None


class ProvisioningReport(BaseModel):
    problems: list[ProblemResult] = []
    counts: dict = {}


def _tag_mint_chat_fn(metered_chat) -> Callable[[str], str]:
    def _chat_fn(prompt: str) -> str:
        return metered_chat.cheap(
            purpose="tag_mint",
            messages=[
                {"role": "system", "content": _TAG_MINT_SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            response_format={"type": "json_schema", "json_schema": build_tag_schema()},
        )

    return _chat_fn


async def _find_tier1_row(db: AsyncSession, *, concept_id: int, chunk_content_hash: str):
    rows = (
        await db.execute(
            select(ConceptProblem).where(ConceptProblem.concept_id == concept_id)
        )
    ).scalars().all()
    for row in rows:
        if (row.provenance or {}).get("chunk_content_hash") == chunk_content_hash:
            return row
    return None


async def _concept_dup_hashes(db: AsyncSession, *, concept_id: int) -> set[str]:
    rows = (
        await db.execute(
            select(ConceptProblem.payload).where(ConceptProblem.concept_id == concept_id)
        )
    ).scalars().all()
    hashes: set[str] = set()
    for payload in rows:
        try:
            hashes.add(problem_dup_hash(payload))
        except Exception:  # noqa: BLE001 — a malformed legacy row must not abort the run
            continue
    return hashes


async def run_authored_set_provisioning(
    db: AsyncSession,
    neo,
    *,
    search_space_id: int,
    problem_document_id: int,
    solution_document_id: int,
    metered_chat,
    embed_fn: Callable[[str], Sequence[float]] | None = None,
    conf_threshold: float = _DEFAULT_CONF_THRESHOLD,
) -> ProvisioningReport:
    if embed_fn is None:
        from indexing.document_embedder import embed_text as embed_fn  # type: ignore

    concept_id = await resolve_or_create_provisional_concept(db, search_space_id=search_space_id)

    sol_chunks = await load_solution_chunks(db, solution_document_id=solution_document_id)
    label_index = build_solution_label_index(sol_chunks)
    page_conf = await page_ocr_confidence(db, document_id=solution_document_id)
    problem_low_conf = _doc_is_low_conf(await page_ocr_confidence(db, document_id=problem_document_id), conf_threshold)

    from apollo.provisioning.orchestrator import _load_chunks  # the by-document_id loader

    prob_chunks = await _load_chunks(db, document_id=problem_document_id)
    scrape_result = await scrape_document(
        prob_chunks,
        chat_fn=metered_chat.scrape_chat_fn(_SCRAPE_SYSTEM_PROMPT),
        triage_chat_fn=metered_chat.scrape_chat_fn(_TRIAGE_SYSTEM_PROMPT),
        max_sections=APOLLO_SCRAPE_MAX_SECTIONS,
        min_candidates=APOLLO_SCRAPE_MIN_CANDIDATES,
        structured=structured_scrape_enabled(),
    )
    await write_tier1_problems(
        db, scrape_result.candidates, concept_id=concept_id, search_space_id=search_space_id
    )

    results: list[ProblemResult] = []
    for candidate in scrape_result.candidates:
        results.append(
            await _process_authored_candidate(
                db, neo,
                candidate=candidate,
                concept_id=concept_id,
                search_space_id=search_space_id,
                solution_document_id=solution_document_id,
                label_index=label_index,
                page_conf=page_conf,
                problem_low_conf=problem_low_conf,
                metered_chat=metered_chat,
                embed_fn=embed_fn,
                conf_threshold=conf_threshold,
            )
        )

    counts: dict = {"promoted": 0, "rejected": 0, "held_for_review": 0}
    for r in results:
        counts[r.outcome] = counts.get(r.outcome, 0) + 1
    return ProvisioningReport(problems=results, counts=counts)


def _doc_is_low_conf(page_conf: dict, threshold: float) -> bool:
    vals = [c for c in page_conf.values() if c is not None]
    return bool(vals) and min(vals) < threshold


async def _process_authored_candidate(
    db, neo, *, candidate, concept_id, search_space_id, solution_document_id,
    label_index, page_conf, problem_low_conf, metered_chat, embed_fn, conf_threshold,
) -> ProblemResult:
    label = getattr(candidate, "label", None)
    retrieve_fn = make_paired_solution_retrieve_fn(
        db, solution_document_id=solution_document_id, label_index=label_index, page_conf=page_conf,
    )

    # Stage 2 — find_or_generate (extract via carries_solution, else generate).
    try:
        draft = await find_or_generate(db, candidate, retrieve_fn=retrieve_fn, chat_fn=metered_chat.main)
    except SolutionDraftError as exc:
        return ProblemResult(label=label, outcome="rejected", diagnostic=f"solution_draft_error: {exc}")

    match_method = getattr(retrieve_fn, "last_match_method", None)
    min_conf = getattr(retrieve_fn, "last_min_conf", None)

    # Stage 2b — OCR cross-check (only when extracted + low confidence).
    verdict = await verify_against_generated(
        db, candidate=candidate, draft=draft, min_conf=min_conf,
        problem_low_conf=problem_low_conf, match_method=match_method,
        metered_chat=metered_chat, conf_threshold=conf_threshold,
    ) if draft.solution_source == "extracted" else None

    # Stage 3 — faithfulness.
    pair_verdict = await validate_pair(
        candidate, draft, retrieve_fn=retrieve_fn, judge_fn=metered_chat.cheap
    )
    rej = rejection_from_verdict(pair_verdict)
    if rej is not None:
        return ProblemResult(
            label=label, outcome="rejected", solution_source=draft.solution_source,
            match_method=match_method, ocr_confidence=min_conf, diagnostic=rej.diagnostic,
        )

    review_required = draft.solution_source == "generated" or bool(verdict and verdict.review_required)
    tier1 = await _find_tier1_row(db, concept_id=concept_id, chunk_content_hash=candidate.chunk_content_hash)

    if review_required:
        # Hold: store drafts for the approve endpoint; DO NOT tag_and_mint/promote.
        if tier1 is not None:
            tier1.provenance = {
                **(tier1.provenance or {}),
                "authored_review": {
                    "required": True,
                    "reason": (verdict.reason if verdict else "generated_no_match"),
                    "ocr_confidence": min_conf,
                    "match_method": match_method,
                    "ocr_draft": draft.model_dump(),
                    "generated_alt": (verdict.generated_alt if verdict else None),
                },
            }
            await db.flush()
        return ProblemResult(
            label=label, outcome="held_for_review", solution_source=draft.solution_source,
            match_method=match_method, ocr_confidence=min_conf, review_required=True,
            reason=(verdict.reason if verdict else "generated_no_match"),
            concept_problem_id=(int(tier1.id) if tier1 is not None else None),
        )

    # Stage 4/5 — promote.
    pair = build_approved_pair(candidate, draft, search_space_id=search_space_id)
    mint_plan = await tag_and_mint(db, pair, chat_fn=_tag_mint_chat_fn(metered_chat), embed_fn=embed_fn)
    existing = await _concept_dup_hashes(db, concept_id=mint_plan.concept_id)
    result = await promote(
        db, neo,
        problem=pair.problem,
        mint_plan=mint_plan,
        search_space_id=search_space_id,
        concept_problem_id=(int(tier1.id) if tier1 is not None else None),
        existing_problem_hashes=existing,
    )
    if not result.promoted:
        return ProblemResult(
            label=label, outcome="rejected", solution_source=draft.solution_source,
            match_method=match_method, ocr_confidence=min_conf,
            failed_gate=result.failed_gate, diagnostic=result.diagnostic,
            concept_problem_id=(int(tier1.id) if tier1 is not None else None),
        )
    return ProblemResult(
        label=label, outcome="promoted", solution_source=draft.solution_source,
        match_method=match_method, ocr_confidence=min_conf,
        concept_problem_id=(int(tier1.id) if tier1 is not None else None),
    )
```

> NOTE: confirm the exact names of the importable module-level literals in
> `apollo/provisioning/orchestrator.py` (`grep -nE "^_SCRAPE_SYSTEM_PROMPT|^_TRIAGE_SYSTEM_PROMPT|^_TAG_MINT_SYSTEM_PROMPT|^APOLLO_SCRAPE_MAX_SECTIONS|^APOLLO_SCRAPE_MIN_CANDIDATES|def structured_scrape_enabled|def _load_chunks" apollo/provisioning/orchestrator.py`). If any are defined elsewhere (e.g. a prompts module or `cost_constants`), import from the real source. Do not redefine them.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest apollo/provisioning/tests/test_authored_set_orchestrator.py -v`
Expected: PASS (needs Docker pgvector + Testcontainers Neo4j; SKIP cleanly otherwise — CI runs it).

- [ ] **Step 5: Commit**

```bash
git add apollo/provisioning/authored_sets/orchestrator.py apollo/provisioning/tests/test_authored_set_orchestrator.py
ruff check apollo/provisioning/authored_sets/orchestrator.py apollo/provisioning/tests/test_authored_set_orchestrator.py && ruff format --check apollo/provisioning/authored_sets/orchestrator.py apollo/provisioning/tests/test_authored_set_orchestrator.py
git commit -m "feat(apollo): run_authored_set_provisioning (scoped grounding + hold-for-review) (WU-AAS)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 9: Endpoints + background task + approve

**Files:**
- Create: `apollo/provisioning/authored_sets/api.py`
- Modify: `apollo/api.py` (include the router)
- Test: `apollo/provisioning/tests/test_authored_api.py`

**Interfaces:**
- Consumes: `index_authored_doc` (Task 5), `run_authored_set_provisioning` (Task 8), `AuthoredSet`
  (Task 1), `MeteredChat`, `require_user`, `require_course_member`, `get_db_session`, `get_neo4j_client`,
  `get_async_session`.
- Produces: an `APIRouter` (no prefix; mounted under `/apollo` by `apollo/api.py`) with:
  - `POST /authored-sets` (multipart: `problem` file, `solution` file, `search_space_id`)
  - `GET /authored-sets?search_space_id=`
  - `GET /authored-sets/{set_id}`
  - `POST /authored-sets/{set_id}/problems/{problem_id}/approve` (body `{reference: "ocr"|"generated"}`)

- [ ] **Step 1: Write the failing test (handler-level, auth + heavy work stubbed)**

Create `apollo/provisioning/tests/test_authored_api.py`. Test the create handler builds the set row,
schedules the task, and returns `{set_id, set_index, status}`; and that the background runner transitions
status. Call the handler functions directly with a stub `BackgroundTasks` and monkeypatched auth/indexer:

```python
import pytest

import apollo.provisioning.authored_sets.api as aapi


class _BG:
    def __init__(self): self.tasks = []
    def add_task(self, fn, *a, **k): self.tasks.append((fn, a, k))


@pytest.mark.asyncio
async def test_create_set_persists_and_schedules(db_session, monkeypatch):
    monkeypatch.setattr(aapi, "require_user", _fake_require_user)          # returns an auth ctx
    monkeypatch.setattr(aapi, "require_course_member", _fake_require_member)  # no-op
    bg = _BG()
    resp = await aapi.create_authored_set(
        request=_FakeRequest(), background=bg,
        problem=_FakeUpload(b"%PDF p"), solution=_FakeUpload(b"%PDF s"),
        search_space_id=4, db=db_session,
    )
    assert resp["status"] == "pending"
    assert resp["set_index"] == 1
    assert len(bg.tasks) == 1  # background provisioning scheduled

    from apollo.persistence.models import AuthoredSet
    row = await db_session.get(AuthoredSet, resp["set_id"])
    assert row.search_space_id == 4
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest apollo/provisioning/tests/test_authored_api.py -v`
Expected: FAIL — module/handler does not exist.

- [ ] **Step 3: Implement the endpoints + background runner**

Create `apollo/provisioning/authored_sets/api.py`:

```python
"""Teacher-gated HTTP surface for authored problem/solution sets (WU-AAS).

POST indexes both docs (hidden), persists the pairing, and runs provisioning in an
in-process background task (timeout-safe; no worker/queue). GET endpoints poll the
result; approve resolves a held problem by promoting the chosen reference."""

from __future__ import annotations

import logging

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, Request, UploadFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apollo.auth_deps import require_course_member, require_user
from apollo.persistence.models import AuthoredSet
from apollo.provisioning.authored_sets.indexing import index_authored_doc
from apollo.provisioning.authored_sets.orchestrator import run_authored_set_provisioning
from apollo.provisioning.metered_chat import MeteredChat
from database.session import get_async_session, get_db_session

_LOG = logging.getLogger(__name__)

router = APIRouter(tags=["apollo-authored-sets"])


async def _next_set_index(db: AsyncSession, search_space_id: int) -> int:
    from sqlalchemy import func

    cur = (
        await db.execute(
            select(func.coalesce(func.max(AuthoredSet.set_index), 0)).where(
                AuthoredSet.search_space_id == search_space_id
            )
        )
    ).scalar_one()
    return int(cur) + 1


@router.post("/authored-sets")
async def create_authored_set(
    request: Request,
    background: BackgroundTasks,
    problem: UploadFile = File(...),
    solution: UploadFile = File(...),
    search_space_id: int = Form(...),
    db: AsyncSession = Depends(get_db_session),
) -> dict:
    auth = await require_user(request)
    await require_course_member(db=db, auth=auth, search_space_id=search_space_id)

    problem_bytes = await problem.read()
    solution_bytes = await solution.read()
    set_index = await _next_set_index(db, search_space_id)

    row = AuthoredSet(search_space_id=search_space_id, set_index=set_index, status="pending")
    db.add(row)
    await db.flush()
    set_id = int(row.id)
    await db.commit()

    background.add_task(
        _run_set_background,
        set_id=set_id,
        search_space_id=search_space_id,
        set_index=set_index,
        problem_bytes=problem_bytes,
        problem_title=problem.filename or f"Problem Set {set_index}",
        solution_bytes=solution_bytes,
        solution_title=solution.filename or f"Solution Set {set_index}",
    )
    return {"set_id": set_id, "set_index": set_index, "status": "pending"}


async def _run_set_background(*, set_id, search_space_id, set_index, problem_bytes,
                              problem_title, solution_bytes, solution_title) -> None:
    """Owns its OWN session (the request's db is closed by now). Never raises out."""
    from apollo.api import get_neo4j_client

    try:
        async with get_async_session() as db:
            await _set_status(db, set_id, "indexing")
            prob_id = await index_authored_doc(
                db, search_space_id=search_space_id, file_bytes=problem_bytes,
                title=problem_title, set_index=set_index, role="problem",
            )
            sol_id = await index_authored_doc(
                db, search_space_id=search_space_id, file_bytes=solution_bytes,
                title=solution_title, set_index=set_index, role="solution",
            )
            row = await db.get(AuthoredSet, set_id)
            row.problem_document_id, row.solution_document_id, row.status = prob_id, sol_id, "provisioning"
            await db.commit()

            report = await run_authored_set_provisioning(
                db, get_neo4j_client(),
                search_space_id=search_space_id,
                problem_document_id=prob_id,
                solution_document_id=sol_id,
                metered_chat=MeteredChat(),
            )
            row = await db.get(AuthoredSet, set_id)
            row.result_summary = report.model_dump()
            row.status = "done"
            await db.commit()
    except Exception as exc:  # noqa: BLE001 — surface as failed status, never crash the worker thread
        _LOG.exception("authored_set_background_failed", extra={"set_id": set_id})
        async with get_async_session() as db2:
            await _set_status(db2, set_id, "failed", diagnostic=str(exc))


async def _set_status(db, set_id, status, *, diagnostic: str | None = None) -> None:
    row = await db.get(AuthoredSet, set_id)
    if row is None:
        return
    row.status = status
    if diagnostic is not None:
        row.result_summary = {**(row.result_summary or {}), "error": diagnostic}
    await db.commit()


@router.get("/authored-sets")
async def list_authored_sets(
    request: Request, search_space_id: int, db: AsyncSession = Depends(get_db_session)
) -> dict:
    auth = await require_user(request)
    await require_course_member(db=db, auth=auth, search_space_id=search_space_id)
    rows = (
        await db.execute(
            select(AuthoredSet).where(AuthoredSet.search_space_id == search_space_id)
            .order_by(AuthoredSet.set_index.asc())
        )
    ).scalars().all()
    return {"sets": [
        {"set_id": int(r.id), "set_index": r.set_index, "status": r.status,
         "problem_document_id": r.problem_document_id, "solution_document_id": r.solution_document_id}
        for r in rows
    ]}


@router.get("/authored-sets/{set_id}")
async def get_authored_set(
    set_id: int, request: Request, db: AsyncSession = Depends(get_db_session)
) -> dict:
    auth = await require_user(request)
    row = await db.get(AuthoredSet, set_id)
    if row is None:
        raise HTTPException(status_code=404, detail="authored set not found")
    await require_course_member(db=db, auth=auth, search_space_id=int(row.search_space_id))
    return {
        "set_id": int(row.id), "set_index": row.set_index, "status": row.status,
        "problem_document_id": row.problem_document_id,
        "solution_document_id": row.solution_document_id,
        "result_summary": row.result_summary or {},
    }
```

Then implement the approve endpoint in the same file:

```python
from pydantic import BaseModel

from apollo.persistence.models import ConceptProblem
from apollo.provisioning.authored_sets.orchestrator import _concept_dup_hashes, _tag_mint_chat_fn
from apollo.provisioning.promote import promote
from apollo.provisioning.solution import ReferenceSolutionDraft, build_approved_pair
from apollo.provisioning.tag_mint import tag_and_mint
from indexing.document_embedder import embed_text


class _ApproveBody(BaseModel):
    reference: str = "ocr"  # "ocr" | "generated"


@router.post("/authored-sets/{set_id}/problems/{problem_id}/approve")
async def approve_held_problem(
    set_id: int, problem_id: int, body: _ApproveBody, request: Request,
    db: AsyncSession = Depends(get_db_session),
) -> dict:
    from apollo.api import get_neo4j_client

    auth = await require_user(request)
    aset = await db.get(AuthoredSet, set_id)
    if aset is None:
        raise HTTPException(status_code=404, detail="authored set not found")
    await require_course_member(db=db, auth=auth, search_space_id=int(aset.search_space_id))

    row = await db.get(ConceptProblem, problem_id)
    review = (row.provenance or {}).get("authored_review") if row else None
    if row is None or not review or not review.get("required"):
        raise HTTPException(status_code=409, detail="problem is not held for review")

    chosen = review.get("generated_alt") if body.reference == "generated" else review.get("ocr_draft")
    if chosen is None:
        raise HTTPException(status_code=422, detail=f"no '{body.reference}' reference stored")
    draft = ReferenceSolutionDraft.model_validate(chosen)

    from types import SimpleNamespace

    payload = row.payload or {}
    candidate = SimpleNamespace(
        problem_text=payload.get("problem_text", ""),
        given_values=payload.get("given_values", {}) or {},
        target_unknown=payload.get("target_unknown", ""),
        chunk_content_hash=(row.provenance or {}).get("chunk_content_hash", ""),
        concept_slug=payload.get("concept_slug", "provisional.inventory"),
        label=payload.get("label"),
    )
    pair = build_approved_pair(candidate, draft, search_space_id=int(aset.search_space_id))
    mint_plan = await tag_and_mint(db, pair, chat_fn=_tag_mint_chat_fn(MeteredChat()), embed_fn=embed_text)
    existing = await _concept_dup_hashes(db, concept_id=mint_plan.concept_id)
    result = await promote(
        db, get_neo4j_client(), problem=pair.problem, mint_plan=mint_plan,
        search_space_id=int(aset.search_space_id), concept_problem_id=problem_id,
        existing_problem_hashes=existing,
    )
    if result.promoted:
        row.provenance = {**(row.provenance or {}),
                          "authored_review": {**review, "required": False, "approved_reference": body.reference}}
        await db.commit()
    return {"promoted": result.promoted, "failed_gate": result.failed_gate, "diagnostic": result.diagnostic}
```

- [ ] **Step 4: Mount the router**

In `apollo/api.py`, after the `router = APIRouter(prefix="/apollo", ...)` line, include the sub-router:

```python
from apollo.provisioning.authored_sets.api import router as authored_sets_router

router.include_router(authored_sets_router)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest apollo/provisioning/tests/test_authored_api.py -v`
Expected: PASS. Also run the whole authored-sets suite:
`pytest apollo/provisioning/tests/test_authored_*.py tests/unit/test_openai_vision_ocr.py apollo/persistence/tests/test_authored_sets_model.py -v`

- [ ] **Step 6: Commit**

```bash
git add apollo/provisioning/authored_sets/api.py apollo/api.py apollo/provisioning/tests/test_authored_api.py
ruff check apollo/provisioning/authored_sets/api.py apollo/provisioning/tests/test_authored_api.py && ruff format --check apollo/provisioning/authored_sets/api.py apollo/provisioning/tests/test_authored_api.py
git commit -m "feat(apollo): authored-sets endpoints + background provisioning + approve (WU-AAS)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 10: Docs drift + full-suite gate

**Files:**
- Modify: `docs/architecture/apollo.md`, `docs/architecture/indexing.md`, `docs/architecture/_overview.md`

- [ ] **Step 1: Update owner docs**

- `docs/architecture/apollo.md`: add an "Authored problem/solution sets (WU-AAS)" subsection describing
  `apollo/provisioning/authored_sets/*`, the `apollo_authored_sets` table, the `extracted`/`held_for_review`
  semantics, and the endpoints. Bump `last_verified`.
- `docs/architecture/indexing.md`: document `ocr/openai_vision.py` + `OCR_PROVIDER=openai` and the
  authored-set indexer's reuse of the indexing core. Bump `last_verified`.
- `docs/architecture/_overview.md`: add the 4 endpoints + the new env vars (`OCR_PROVIDER`,
  `APOLLO_OCR_MODEL`, `APOLLO_AUTHORED_OCR_CONF_THRESHOLD`). Bump `last_verified`.

- [ ] **Step 2: Run the full provisioning + retrieval suites**

Run:
`pytest apollo/provisioning/ apollo/persistence/ tests/unit/test_openai_vision_ocr.py -v --tb=short`
`pytest tests/ -k retrieval -v` (the retrieval suite — confirms Task 6's reuse of `_halfvec_cosine_distance` didn't regress anything).
Expected: PASS/SKIP (no failures).

- [ ] **Step 3: Commit**

```bash
git add docs/architecture/apollo.md docs/architecture/indexing.md docs/architecture/_overview.md
git commit -m "docs(apollo): document authored problem/solution sets (WU-AAS)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
git push origin ApolloRun
```

---

## Post-implementation: staging E2E (ss=4 AAE)

1. Set on Railway `ai-ta-backend` (staging): `OCR_PROVIDER=openai` (optionally `APOLLO_OCR_MODEL`,
   `APOLLO_AUTHORED_OCR_CONF_THRESHOLD=0.6`).
2. Apply migration 032 to staging (Task 1 Step 6).
3. Open a PR (`gh pr create --base staging`); report URL + CI; do NOT merge.
4. Via the teacher UI (or curl) upload a real AAE HW problem PDF + its handwritten solution PDF as Set 1;
   poll `GET /apollo/authored-sets/{id}`; assert: problems extracted, references grounded to the paired
   solution doc, handwritten low-confidence ones `held_for_review`, clean ones `promoted` (tier-2).

## Self-review notes (coverage map)

- Spec N1 → Task 4; N2 → Task 5; N3 → Task 6; N4 → Tasks 2+3; N5 → Task 7; N6 → Task 1;
  N7 → Task 8; N8 → Task 9; docs/drift → Task 10. Frontend (N9) → separate plan in `ai-ta-teacher-ui`.
- Trust gate (D6/D7/D9): extract vs generate (Task 6/8), verification flag (Task 7), tier-1 hold +
  approve (Task 8/9). Held problems never `tag_and_mint`/`promote` until approval (KG integrity).
- Doc-scoping never touches `active_document_conditions` (Task 6) — the week-gate bug stays out.
