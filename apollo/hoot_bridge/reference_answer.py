"""Stateless bridge from an Apollo teaching turn to Hoot's QA pipeline.

Brief: styx/plans/hoot-apollo-04-ask-hoot-hint-lane.md ("ask Hoot" hint lane).
Gated by the `INTERACTION4` flag (default OFF; see `is_enabled` below) plus
the call-site `INTERACTION_CONCEPTS` allowlist in handlers/intent and chat.

This module composes retrieve_for_question() -> the compact aside-refresher
prompt (ai/prompts/apollo_aside.py, passed as ``system_prompt_override`` into
ai.main_ai.solve_with_bundle in place of the standalone-chat tutor prompt) ->
citation formatting (citations/formatter.py), the same retrieval + solve lane
Hoot's /ask uses, but with a mid-teaching-session refresher voice instead of
Hoot's headings/self-check chat structure. It deliberately does NOT go through
server.py's /ask route or
ai/router wiring (prepare_router_context, bundle cache, chat-turn
persistence) — those are welded to Hoot chat sessions Apollo does not have.

Heavy imports (retrieval, ai/main_ai, citations, DB models) are deferred into
`answer_reference_question` so importing this module (e.g. to read
`is_enabled()` from apollo.handlers.intent) stays cheap.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from apollo.schemas.problem import Problem
    from config.contracts import BundleSnippet

# Message-envelope contract consumed by the student UI (see the handler's
# response shape at apollo/handlers/chat.py::_execute_reference_question).
MESSAGE_KIND_REFERENCE_ASIDE = "reference_aside"

# TutoringMessage.intent tag stamped on the aside row so the adjudicator's
# `_full_transcript` (apollo/handlers/done.py) can exclude it from grading
# while the student's triggering question (untagged) stays in.
ASIDE_MESSAGE_INTENT_TAG = "reference_question_aside"

# Per-session cap: exceeding it gets a persona redirect instead of another
# lookup (brief: "Rate cap per session (e.g., 3 asides)").
MAX_ASIDES_PER_SESSION = 3

# TutoringSession.metadata_ key the aside count is persisted under. Shared
# between apollo/handlers/chat.py (increments it) and apollo/handlers/done.py
# (reads it into grading_provenance, additive per the brief) so the two
# handlers can't drift on the key name.
ASIDE_COUNT_SESSION_METADATA_KEY = "reference_question_aside_count"

_OUT_OF_SCOPE_TEXT = "That's outside what's covered in this course's materials."
_NOT_FOUND_TEXT = "Not found in the approved materials."
_UNSCORED_FALLBACK_LIMIT = 5

# ``ai.main_ai.format_answer`` appends a trailing "\n\nCitations: [..], [..]"
# enumeration to every non-empty answer (main_ai.py, end of ``format_answer``).
# For the aside card that in-text list is redundant: the structured ``citations``
# payload is returned separately (``_structured_citations`` below) and rendered as
# chips by the student UI. This regex matches ONLY that trailing single-line block
# (markers never contain a newline) anchored to the end of the text, so Hoot chat —
# which keeps the block — is untouched; the strip happens in this lane alone.
_TRAILING_CITATIONS_BLOCK = re.compile(r"\n\nCitations:[^\n]*\Z")


def _strip_trailing_citations_block(text: str) -> str:
    """Remove the trailing "Citations: [..]" enumeration ``format_answer`` appends.

    Aside-lane-only post-process: the structured ``citations`` payload carries the
    same markers for the UI, so the in-text list is redundant here. Returns text
    with no trailing block unchanged (the regex is anchored to the string end and
    only matches the single-line block ``format_answer`` produces).
    """
    return _TRAILING_CITATIONS_BLOCK.sub("", text.rstrip()).rstrip()


def _fallback_answer_from_snippets(snippets: list[Any]) -> tuple[str, list[str]]:
    """Render retrieval-ranked source text when model formatting is blank."""
    excerpts: list[str] = []
    markers: list[str] = []
    for snippet in snippets[:_UNSCORED_FALLBACK_LIMIT]:
        snippet_text = str(getattr(snippet, "text", "") or "").strip()
        if not snippet_text:
            continue
        marker = str(getattr(snippet, "citation_marker", "") or "").strip()
        excerpts.append(f"{snippet_text} {marker}".rstrip())
        if marker and marker not in markers:
            markers.append(marker)
    return ("\n\n".join(excerpts) or _NOT_FOUND_TEXT, markers)


def is_enabled() -> bool:
    """True iff INTERACTION4 is truthy. Default OFF.

    Live on prod Railway since 2026-07-30 (`INTERACTION4=true`,
    `INTERACTION_CONCEPTS=ethics`); local `.env` mirrors it for testing.
    """
    return os.getenv("INTERACTION4", "").lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class ReferenceAsideResult:
    """The Hoot-side answer for one "ask Hoot" hint-lane turn.

    Subject-agnostic and persona-free by design — apollo/handlers/chat.py
    wraps this in the persona resume line; this module never speaks in
    Apollo's confused-learner voice.
    """

    in_scope: bool
    text: str
    citations: list[dict[str, Any]]


async def _course_subject(db: AsyncSession, *, course_id: int) -> str:
    """Resolve the subject the relevance guard classifies against.

    Ladder: ``Course.subject_name`` → ``Course.name`` → global
    ``get_subject_name()`` (env TEXTBOOK_SUBJECT → "course/textbook").
    Best-effort like the keyword hints — a lookup failure degrades to the
    global fallback instead of killing the aside, because the subject is an
    advisory classifier input, not part of the answer path.
    """
    from config.settings import get_subject_name

    try:
        from sqlalchemy import select

        from database.models import Course

        row = (
            await db.execute(select(Course.subject_name, Course.name).where(Course.id == course_id))
        ).first()
    except Exception:  # noqa: BLE001 - advisory input, never fatal
        row = None
    if row is not None:
        subject = (row[0] or "").strip() or (row[1] or "").strip()
        if subject:
            return subject
    return get_subject_name()


# The problem statement can be long; the hint only needs enough text to anchor
# the classifier to the module's topic, not the full statement.
_TOPIC_HINT_TEXT_BUDGET = 240


def _topic_hint(problem: Any) -> str | None:
    """Compose "what the student is studying right now" from the problem.

    ``concept_id`` is the concept slug (e.g. "ethics") and ``problem_text`` the
    statement being taught — together they anchor the relevance guard to
    module-level content a bare course title can't convey. Returns None when
    either field is missing/non-string so malformed problems degrade to the
    subject-only guard instead of raising.
    """
    slug = getattr(problem, "concept_id", None)
    text = getattr(problem, "problem_text", None)
    if not isinstance(slug, str) or not slug.strip():
        return None
    if not isinstance(text, str) or not text.strip():
        return None
    topic = slug.strip().replace("-", " ").replace("_", " ")
    excerpt = " ".join(text.split())[:_TOPIC_HINT_TEXT_BUDGET]
    return f"{topic} — current problem: {excerpt}"


def _document_id_of(snippet: BundleSnippet) -> int | None:
    raw = (snippet.metadata or {}).get("document_id")
    try:
        return int(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def _filter_leaked_snippets(
    snippets: list[BundleSnippet], excluded_document_ids: set[int]
) -> list[BundleSnippet]:
    """Drop any snippet sourced from an excluded (solution-bearing) document.

    Pure post-retrieval filter — no DB access — so it's unit-testable in
    isolation from the (paired-solution-doc) resolution query below.
    """
    if not excluded_document_ids:
        return list(snippets)
    return [sn for sn in snippets if _document_id_of(sn) not in excluded_document_ids]


async def _excluded_document_ids(db: AsyncSession, *, course_id: int, problem: Problem) -> set[int]:
    """Resolve the set of document ids the hint lane must never ground on.

    Two sources, both keyed off `ProvisioningRun` — the authoritative
    problem/solution pairing registry (`material_kind` cannot distinguish
    problem vs. solution docs: both index as "other"):

    1. Every solution document ever paired for this course
       (`ProvisioningRun.solution_document_id`) — course-wide, doc-kind-level
       exclusion (brief: "exclude solution-bearing doc kinds").
    2. The current problem's own paired solution doc, resolved via its
       source `problem_document_id` — belt-and-suspenders (brief: "AND the
       current problem's paired solution doc by id").
    """
    from sqlalchemy import select

    from apollo.persistence.models import Problem as ProblemRow
    from apollo.persistence.models import ProvisioningRun

    excluded: set[int] = set()

    course_wide = (
        (
            await db.execute(
                select(ProvisioningRun.solution_document_id).where(
                    ProvisioningRun.search_space_id == course_id,
                    ProvisioningRun.solution_document_id.is_not(None),
                )
            )
        )
        .scalars()
        .all()
    )
    excluded.update(int(doc_id) for doc_id in course_wide if doc_id is not None)

    problem_database_id = getattr(problem, "database_id", None)
    if problem_database_id is not None:
        problem_row = await db.get(ProblemRow, problem_database_id)
        if problem_row is not None:
            provenance = cast("dict[str, Any]", problem_row.provenance or {})
            problem_document_id = provenance.get("document_id")
            if problem_document_id is not None:
                paired = (
                    await db.execute(
                        select(ProvisioningRun.solution_document_id).where(
                            ProvisioningRun.problem_document_id == int(problem_document_id),
                            ProvisioningRun.search_space_id == course_id,
                        )
                    )
                ).scalar_one_or_none()
                if paired is not None:
                    excluded.add(int(paired))

    return excluded


def _structured_citations(bundle: Any, used_markers: list[str]) -> list[dict[str, Any]]:
    """Structured citation payload for the surviving, used snippets.

    Mirrors server.py's `_structured_citations_from_bundle` (same shape:
    doc_type/file/page/bbox/ocr_conf/ocr_provider/teacher_upload_id/week/
    page_asset/raw_latex/verified/label) without importing server.py.
    """
    from citations.formatter import format_citations

    if not used_markers:
        return []
    used_set = {m.strip() for m in used_markers if isinstance(m, str) and m.strip()}
    if not used_set:
        return []

    entries: list[dict[str, Any]] = []
    id_to_row: dict[str, dict[str, Any]] = {}
    store_meta: dict[str, dict[str, Any]] = {}

    for sn in getattr(bundle, "snippets", []) or []:
        marker = getattr(sn, "citation_marker", "") or ""
        if marker not in used_set:
            continue
        sn_id = getattr(sn, "id", None)
        sn_meta = getattr(sn, "metadata", None) or {}
        sn_type = sn_meta.get("material_kind") or sn_meta.get("kind") or "other"
        store_key = str(
            sn_meta.get("teacher_upload_id")
            or sn_meta.get("document_id")
            or getattr(sn, "doc_title", None)
            or sn_id
            or sn_type
        )
        entries.append({"id": sn_id, "snippet": sn})
        if sn_id is not None:
            id_to_row[sn_id] = {
                "store_key": store_key,
                "store_kind": sn_type,
                "source_path": getattr(sn, "source_path", ""),
                "page": getattr(sn, "page", None),
                "bbox": sn_meta.get("bbox"),
            }
        if store_key not in store_meta:
            store_meta[store_key] = {
                "kind": sn_type,
                "week": sn_meta.get("week"),
                "average_confidence": sn_meta.get("ocr_confidence"),
                "ocr_provider": sn_meta.get("ocr_provider"),
                "teacher_upload_id": sn_meta.get("teacher_upload_id"),
                "page_asset": sn_meta.get("page_asset"),
                "raw_latex": sn_meta.get("raw_latex"),
            }

    _, structured = format_citations(entries, id_to_row, store_meta)
    return structured


async def answer_reference_question(
    *,
    db: AsyncSession,
    course_id: int,
    question: str,
    problem: Problem,
) -> ReferenceAsideResult:
    """Answer one student aside through Hoot's scoped, citation-backed QA lane.

    Stateless: no bundle cache, no chat-session/turn persistence — every call
    re-runs the full relevance-guard + retrieval + solve + format compose.
    Raises on genuine failures (network/DB/LLM errors); the caller
    (apollo/handlers/chat.py) is responsible for the apology-and-fall-through
    contract on exception — this function never swallows errors into a fake
    "not found" result, so failure vs. "genuinely out of scope" stay distinct.
    """
    from ai.main_ai import (
        check_question_relevance,
        extract_and_filter_keywords,
        format_answer,
        parse_question,
        solve_with_bundle,
    )
    from ai.prompts.apollo_aside import apollo_aside_prompt
    from config.contracts import ParsedTask, ResearchBundle, ResearchMetadata
    from retrieval.context_packer import _summarize_snippets
    from retrieval.pipeline import retrieve_for_question

    # Scope enforcement — the semantic filter / relevance guard never bypassed.
    # The guard classifies against the course's real subject plus what the
    # student is studying right now; with neither it judges the bare question
    # against the "course/textbook" placeholder and rejects in-scope questions
    # on phrasing alone (2026-07-30 surveillance-tools aside bug).
    subject = await _course_subject(db, course_id=course_id)
    relevance = check_question_relevance(
        question, subject=subject, current_topic=_topic_hint(problem)
    )
    if relevance["relevance"] == "none":
        return ReferenceAsideResult(in_scope=False, text=_OUT_OF_SCOPE_TEXT, citations=[])

    keyword_query = question
    if relevance["relevance"] == "partial" and relevance.get("on_topic_portion"):
        keyword_query = relevance["on_topic_portion"]
    try:
        _ctx_summary, filtered_terms = extract_and_filter_keywords(keyword_query, subject=subject)
    except Exception:  # noqa: BLE001 - keyword hints are optional, never fatal
        filtered_terms = []
    keywords: list[str] = []
    for entry in filtered_terms or []:
        if isinstance(entry, dict):
            term = entry.get("term") or entry.get("keyword") or entry.get("name")
            if isinstance(term, str) and term.strip():
                keywords.append(term.strip())
        elif isinstance(entry, str) and entry.strip():
            keywords.append(entry.strip())

    snippets, diagnostics = await retrieve_for_question(
        query=question,
        keywords=keywords,
        search_space_id=course_id,
        db_session=db,
    )

    excluded_ids = await _excluded_document_ids(db, course_id=course_id, problem=problem)
    snippets = _filter_leaked_snippets(snippets, excluded_ids)

    if not snippets:
        return ReferenceAsideResult(in_scope=True, text=_NOT_FOUND_TEXT, citations=[])

    equations, glossary, assumptions, _ = _summarize_snippets(snippets)
    allowed_markers = [sn.citation_marker for sn in snippets if sn.citation_marker]

    metadata = ResearchMetadata(
        keyword_iterations=[
            {"round": 1, "combined_query": diagnostics.get("combined_query", question)}
        ],
        attempted_terms=[question],
        final_query=diagnostics.get("combined_query", question),
        original_query=question,
        allowed_markers=allowed_markers,
        hit_count_sem=diagnostics.get("hit_count_sem", 0),
    )
    bundle = ResearchBundle(
        metadata=metadata,
        snippets=snippets,
        equations=equations,
        assumptions=assumptions,
        glossary=glossary,
        used_ids=[sn.id for sn in snippets],
        stats=diagnostics,
        provenance={"source": "hint_lane"},
        allowed_markers=allowed_markers,
        attempted_terms=[question],
    )

    try:
        parsed_task = parse_question(question)
    except ValueError:
        parsed_task = ParsedTask(problem_type="conceptual", asked_outputs=[question])
    solution = solve_with_bundle(
        parsed_task,
        bundle,
        system_prompt_override=apollo_aside_prompt(),
        unscored_fallback_limit=_UNSCORED_FALLBACK_LIMIT,
    )
    final = format_answer(solution, bundle, include_background=False)

    # The aside card renders `citations` as chips, so drop format_answer's
    # redundant in-text "Citations: [..]" enumeration for this lane only.
    text = _strip_trailing_citations_block(final.text or "")
    used_markers = final.citations
    if not text.strip():
        text, used_markers = _fallback_answer_from_snippets(snippets)
    citations = _structured_citations(bundle, used_markers)
    return ReferenceAsideResult(in_scope=True, text=text, citations=citations)


__all__ = [
    "ASIDE_COUNT_SESSION_METADATA_KEY",
    "ASIDE_MESSAGE_INTENT_TAG",
    "MAX_ASIDES_PER_SESSION",
    "MESSAGE_KIND_REFERENCE_ASIDE",
    "ReferenceAsideResult",
    "answer_reference_question",
    "is_enabled",
]
