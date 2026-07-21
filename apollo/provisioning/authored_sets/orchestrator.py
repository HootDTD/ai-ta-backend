"""Authored-set provisioning (WU-AAS).

Scrape a problem document, ground each candidate against ONLY its paired
solution document, verify OCR-suspect extractions, and promote trusted references.
Generated or suspect references stay tier-1 for teacher review.

Combined Q&A documents invert the first two stages: with structure pairing ON,
the structure pass runs before scrape and the scraper receives only question-unit
slices. This ordering is a student-safety boundary because tier-1 problem text is
persisted immediately after scrape and cannot be repaired by later pairing gates.
Because scrape spend is unknown at that point, the combined pass uses the
structure module's 30k budget floor.

REVERSED PROVISIONING (default when the course has registered concepts): each
candidate is first CLASSIFIED against the course's premade concept list
(``concept_match``, NO_MATCH held for teacher review — never force-matched),
then its reference graph is DERIVED from the paired solution spans anchored to
the matched concept's vocabulary (``graph_derivation``). The mint + promote
pair runs inside ONE savepoint, so a lint rejection rolls back every KG row
the mint flushed (no orphaned entities). ``APOLLO_REVERSED_PROVISIONING=0``
reverts to the legacy LLM-tag-draft path.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Callable, Sequence
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_serializer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apollo.persistence.models import Concept
from apollo.persistence.models import Problem as ProblemRecord
from apollo.provisioning.authored_sets.graph_derivation import (
    DerivationError,
    derive_reference_graph,
)
from apollo.provisioning.authored_sets.label_match import build_solution_label_index
from apollo.provisioning.authored_sets.paired_retrieval import (
    chunk_ocr_confidence,
    load_solution_chunks,
    make_paired_solution_retrieve_fn,
)
from apollo.provisioning.authored_sets.structure_pass import (
    StructurePair,
    StructurePassResult,
    StructurePassSummary,
    StructureUnit,
    run_structure_pass,
)
from apollo.provisioning.authored_sets.verification import verify_against_generated
from apollo.provisioning.concept_match import ConceptMatch, match_concept
from apollo.provisioning.cost_constants import (
    APOLLO_SCRAPE_MAX_SECTIONS,
    APOLLO_SCRAPE_MIN_CANDIDATES,
    structure_pairing_mode,
    structured_scrape_enabled,
)
from apollo.provisioning.pairing_gate import rejection_from_verdict, validate_pair
from apollo.provisioning.problem_hash import problem_dup_hash
from apollo.provisioning.promote import PromoteHeldForReview, PromoteResult, promote
from apollo.provisioning.provisioning_schema import build_tag_schema
from apollo.provisioning.scrape import (
    resolve_or_create_provisional_concept,
    scrape_document,
    write_tier1_problems,
)
from apollo.provisioning.solution import (
    ReferenceSolutionDraft,
    SolutionDraftError,
    build_approved_pair,
    find_or_generate,
)
from apollo.provisioning.tag_mint import ResolvedConcept, TagMintError, tag_and_mint
from apollo.schemas.problem import Problem
from apollo.subjects.curriculum_db import RegisteredConcept, list_registered_concepts

__all__ = [
    "MintRejected",
    "ProblemResult",
    "ProvisioningReport",
    "reversed_provisioning_enabled",
    "run_authored_set_provisioning",
]

_SCRAPE_SYSTEM_PROMPT = (
    "You extract EVERY question a student could be asked to answer from one "
    "SECTION of course material, in ANY subject (textbook prose, worked "
    "examples, exercise sets, exam study guides, and review outlines all count; "
    "a section may contain zero, one, or many questions). Numeric solve-for "
    "exercises, convergence/divergence determinations, show-that/verify tasks, "
    "true/false items, define/explain/compare prompts, and open-ended "
    "study-guide or discussion questions ALL count as problems — for a question "
    'with no numeric answer, "target_unknown" is a short phrase naming what is '
    'asked (e.g. "convergence verdict", "definition of future shock") and '
    '"given_values" is {}.\n'
    "Return ONLY a JSON array - no prose, no explanation, no markdown code fences. "
    "Each array element is an object with EXACTLY these keys:\n"
    '  "problem_text": string - the full, self-contained problem statement.\n'
    '  "given_values": object mapping each stated known quantity\'s short symbol to '
    "its NUMERIC value (numbers only - no units, no strings); use {} if none.\n"
    '  "target_unknown": string - the single quantity or idea the problem asks '
    "to find.\n"
    '  "difficulty": exactly one of "intro", "standard", "hard".\n'
    '  "concept_slug": string - a short dotted/kebab concept id, e.g. '
    '"bernoulli-equation".\n'
    '  "label": the problem\'s printed number/label exactly as shown, e.g. '
    '"Problem 3", "Q3", "3.", or null if none.\n'
    "If the section truly contains no questions, return []."
)

_TRIAGE_SYSTEM_PROMPT = (
    "You triage a document's SECTIONS to find which likely contain questions a "
    "student could be asked to answer — quantitative exercises OR qualitative "
    "review/discussion questions. You receive a JSON array of sections, each with "
    'an "index", "title", "chars", and "has_numeric_imperative" flag.\n'
    "Return ONLY a JSON array - no prose, no markdown fences. Each element is an "
    "object with EXACTLY these keys:\n"
    '  "index": integer - echo the section\'s index.\n'
    '  "is_problem_likely": boolean - true if the section probably contains '
    "questions, practice problems, or worked examples.\n"
    '  "priority": integer 0-10 - higher = scrape sooner.\n'
    '  "concept_slug": string - a short dotted/kebab concept id for the section.\n'
    '  "concept_display": string - a human-readable concept label.\n'
    "Include EVERY index from the input exactly once."
)

_TAG_MINT_SYSTEM_PROMPT = (
    "You tag an already-approved problem (quantitative OR qualitative) with its "
    "canonical concept and the prerequisite edges between its solution entities.\n"
    "Return ONLY a JSON object - no prose, no explanation, no markdown code fences. "
    "The object has EXACTLY these keys:\n"
    '  "concept_slug": string - a short dotted/kebab concept id (e.g. '
    '"bernoulli-equation"). REQUIRED.\n'
    '  "display_name": string - a human-readable concept label; if unknown, repeat '
    "the concept_slug.\n"
    '  "prereqs": array of {"from": <entity-key>, "to": <entity-key>} objects naming '
    "prerequisite edges between the problem's minted entity keys; use [] if none."
)


class _ChunkView:
    __slots__ = ("id", "content", "document_id", "page_number", "section_path", "chunk_type")

    def __init__(self, id, content, document_id, page_number, section_path, chunk_type):  # noqa: A002
        self.id = id
        self.content = content
        self.document_id = document_id
        self.page_number = page_number
        self.section_path = section_path
        self.chunk_type = chunk_type


async def _load_chunks(db: AsyncSession, *, document_id: int) -> Sequence[_ChunkView]:
    from database.models import DocumentChunk

    rows = (
        await db.execute(
            select(
                DocumentChunk.id,
                DocumentChunk.content,
                DocumentChunk.document_id,
                DocumentChunk.page_number,
                DocumentChunk.section_path,
                DocumentChunk.chunk_type,
            )
            .where(DocumentChunk.document_id == document_id)
            .order_by(DocumentChunk.id.asc())
        )
    ).all()
    return [
        _ChunkView(r.id, r.content, r.document_id, r.page_number, r.section_path, r.chunk_type)
        for r in rows
    ]

_DEFAULT_CONF_THRESHOLD = 0.6
_LOG = logging.getLogger(__name__)
_ANSWER_LINE_MARKER = re.compile(
    r"^\s*(?:answer|solution|ans|key)\b\s*[:.\-]",
    re.IGNORECASE,
)


def reversed_provisioning_enabled() -> bool:
    """Kill switch: ``APOLLO_REVERSED_PROVISIONING=0`` reverts to the legacy
    (LLM-tag-draft) authored path. Default ON — reversed is the product model;
    it additionally activates only when the course has registered concepts."""
    return os.getenv("APOLLO_REVERSED_PROVISIONING", "1") != "0"


class MintRejected(Exception):
    """Internal control flow: ``promote`` returned a lint rejection INSIDE the
    mint+promote savepoint — raising unwinds the savepoint so the mint's
    flushed concept/KGEntity/prereq/dedup rows do NOT survive as orphans (the
    verified 17->33 entity-doubling bug), then the except arm reports the
    rejection normally."""

    def __init__(self, result: PromoteResult) -> None:
        super().__init__(result.diagnostic)
        self.result = result


class ProblemResult(BaseModel):
    """One authored-set candidate outcome."""

    model_config = ConfigDict(frozen=True)

    label: str | None = None
    outcome: str
    solution_source: str | None = None
    match_method: str | None = None
    ocr_confidence: float | None = None
    failed_gate: int | None = None
    diagnostic: str = ""
    review_required: bool = False
    reason: str | None = None
    concept_problem_id: int | None = None


class ProvisioningReport(BaseModel):
    """Bounded per-set provisioning report persisted by the Task 9 API."""

    model_config = ConfigDict(frozen=True)

    problems: list[ProblemResult] = Field(default_factory=list)
    counts: dict[str, int] = Field(default_factory=dict)
    structure_pass: StructurePassSummary | None = None
    combined_document: bool = False

    @model_serializer(mode="wrap")
    def _serialize_without_inactive_shadow(self, handler):  # noqa: ANN001
        """Keep flag-off ``model_dump`` output byte-compatible with WU-AAS."""
        data = handler(self)
        if self.structure_pass is None:
            data.pop("structure_pass", None)
        if not self.combined_document:
            data.pop("combined_document", None)
        return data


def _augmented_hold_payload(payload: dict | None, draft: ReferenceSolutionDraft) -> dict | None:
    """Apply accepted explain-why text; preserve the object on plain holds."""
    if not draft.augmented_problem_text:
        return payload
    updated = dict(payload or {})
    updated.setdefault("problem_text_original", updated.get("problem_text"))
    updated["problem_text"] = draft.augmented_problem_text
    if draft.augmented_target_unknown:
        updated.setdefault("target_unknown_original", updated.get("target_unknown"))
        updated["target_unknown"] = draft.augmented_target_unknown
    updated["augmented"] = "explain_why"
    return updated


def _tag_mint_chat_fn(metered_chat: Any) -> Callable[[str], str]:
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


def _doc_is_low_conf(page_conf: dict[int | None, float | None], threshold: float) -> bool:
    vals = [conf for conf in page_conf.values() if conf is not None]
    return bool(vals) and min(vals) < threshold


class _QuestionChunk(BaseModel):
    """Scrape-compatible view containing only question-unit source slices."""

    model_config = ConfigDict(frozen=True)

    id: int
    content: str
    document_id: int
    page_number: int | None = None
    section_path: str | None = None
    chunk_type: str | None = None


def _truncate_answer_line_tail(text: str) -> tuple[str, bool]:
    """Drop a question span's answer-marker line and all following text."""
    offset = 0
    for line in text.splitlines(keepends=True):
        if _ANSWER_LINE_MARKER.match(line):
            return text[:offset], True
        offset += len(line)
    return text, False


def _compose_question_mask(
    problem_chunks: Sequence[Any], units: Sequence[StructureUnit]
) -> tuple[tuple[_QuestionChunk, ...], int]:
    """Mask a combined document to question spans without changing provenance.

    Multiple question units may overlap one persisted chunk. Their local ranges
    are merged in source order, while every answer/other range is omitted. Any
    explicit answer overlap is subtracted defensively so a malformed overlapping
    segmentation cannot pass a known answer span to the scraper.
    """
    ranges_by_chunk: dict[int, list[tuple[int, int]]] = {}
    answer_ranges_by_chunk: dict[int, list[tuple[int, int]]] = {}
    for unit in units:
        if unit.document_role != "problem" or unit.kind not in ("question", "answer"):
            continue
        target = ranges_by_chunk if unit.kind == "question" else answer_ranges_by_chunk
        for span in unit.block_spans:
            target.setdefault(span.chunk_id, []).append((span.start_char, span.end_char))

    masked: list[_QuestionChunk] = []
    answer_line_backstop_count = 0
    for chunk in problem_chunks:
        chunk_id = int(chunk.id)
        content = str(getattr(chunk, "content", "") or "")
        valid_ranges = sorted(
            (max(0, start), min(len(content), end))
            for start, end in ranges_by_chunk.get(chunk_id, ())
            if start < end and start < len(content) and end > 0
        )
        merged: list[tuple[int, int]] = []
        for start, end in valid_ranges:
            if merged and start <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], end))
            else:
                merged.append((start, end))
        answer_ranges = sorted(
            (max(0, start), min(len(content), end))
            for start, end in answer_ranges_by_chunk.get(chunk_id, ())
            if start < end and start < len(content) and end > 0
        )
        question_ranges: list[tuple[int, int]] = []
        for start, end in merged:
            remaining = [(start, end)]
            for answer_start, answer_end in answer_ranges:
                next_remaining: list[tuple[int, int]] = []
                for part_start, part_end in remaining:
                    if answer_end <= part_start or answer_start >= part_end:
                        next_remaining.append((part_start, part_end))
                        continue
                    if part_start < answer_start:
                        next_remaining.append((part_start, answer_start))
                    if answer_end < part_end:
                        next_remaining.append((answer_end, part_end))
                remaining = next_remaining
            question_ranges.extend(remaining)
        question_parts: list[str] = []
        for start, end in question_ranges:
            question_part, backstop_fired = _truncate_answer_line_tail(content[start:end])
            answer_line_backstop_count += int(backstop_fired)
            question_parts.append(question_part)
        question_text = "\n".join(question_parts)
        if not question_text.strip():
            continue
        masked.append(
            _QuestionChunk(
                id=chunk_id,
                content=question_text,
                document_id=int(chunk.document_id),
                page_number=getattr(chunk, "page_number", None),
                section_path=getattr(chunk, "section_path", None),
                chunk_type=getattr(chunk, "chunk_type", None),
            )
        )
    return tuple(masked), answer_line_backstop_count


def _log_answer_line_backstop(count: int) -> None:
    if count:
        _LOG.warning(
            "authored_set_combined_answer_line_backstop",
            extra={
                "event": "authored_set_combined_answer_line_backstop",
                "count": count,
            },
        )


def _question_only_chunks(
    problem_chunks: Sequence[Any], units: Sequence[StructureUnit]
) -> tuple[_QuestionChunk, ...]:
    question_chunks, backstop_count = _compose_question_mask(problem_chunks, units)
    _log_answer_line_backstop(backstop_count)
    return question_chunks


def _has_problem_answers(result: StructurePassResult) -> bool:
    return any(unit.kind == "answer" and unit.document_role == "problem" for unit in result.units)


def _solution_chunks_from_problem(
    problem_chunks: Sequence[Any],
) -> list[tuple[int, str, int | None]]:
    return [
        (
            int(chunk.id),
            str(getattr(chunk, "content", "") or ""),
            getattr(chunk, "page_number", None),
        )
        for chunk in problem_chunks
    ]


async def _find_tier1_row(
    db: AsyncSession, *, concept_id: int, course_id: int, chunk_content_hash: str
) -> ProblemRecord | None:
    return (
        await db.execute(
            select(ProblemRecord)
            .where(ProblemRecord.course_id == course_id)
            .where(ProblemRecord.concept_id == concept_id)
            .where(ProblemRecord.problem_code == f"scrape.{chunk_content_hash}")
        )
    ).scalar_one_or_none()


async def _authored_concept_dup_hashes(
    db: AsyncSession, *, concept_id: int, course_id: int
) -> set[str]:
    rows = (
        (
            await db.execute(
                select(ProblemRecord, Concept.slug)
                .join(Concept, Concept.id == ProblemRecord.concept_id)
                .where(ProblemRecord.course_id == course_id)
                .where(ProblemRecord.concept_id == concept_id)
                .where(ProblemRecord.tier == 2)
            )
        )
        .all()
    )
    hashes: set[str] = set()
    for row, concept_slug in rows:
        try:
            hashes.add(
                problem_dup_hash(
                    Problem.model_validate(row.to_pydantic_payload(concept_slug=concept_slug))
                )
            )
        except (ValidationError, ValueError):
            continue
    return hashes


async def run_authored_set_provisioning(
    db: AsyncSession,
    neo,
    *,
    search_space_id: int,
    problem_document_id: int,
    solution_document_id: int | None,
    metered_chat: Any,
    combined_document: bool = False,
    embed_fn: Callable[[str], Sequence[float]] | None = None,
    conf_threshold: float = _DEFAULT_CONF_THRESHOLD,
) -> ProvisioningReport:
    """Run trigger-agnostic provisioning for one problem document, optionally
    paired with a solution document. ``solution_document_id=None`` (no solution
    PDF uploaded) normally means every candidate grounds against no solution
    spans. With structure pairing ON, ``combined_document=True`` (the API's
    same-hash handoff) or a problem-only upload is segmented before scrape; only
    usable question spans are masked before scrape, including when the answer-line
    backstop is the only signal separating an answer tail. Answer units are still
    required to activate intra-document pairing; otherwise the existing
    generate-and-hold path remains in effect."""
    if embed_fn is None:
        from indexing.document_embedder import embed_text as embed_fn  # type: ignore
    assert embed_fn is not None

    concept_id = await resolve_or_create_provisional_concept(db, search_space_id=search_space_id)
    registered = await list_registered_concepts(db, search_space_id=search_space_id)
    reversed_mode = reversed_provisioning_enabled() and bool(registered)
    effective_solution_document_id = solution_document_id
    solution_chunks = await load_solution_chunks(
        db, solution_document_id=effective_solution_document_id
    )
    label_index = build_solution_label_index(solution_chunks)
    solution_page_conf = await chunk_ocr_confidence(db, document_id=effective_solution_document_id)
    problem_page_conf = await chunk_ocr_confidence(db, document_id=problem_document_id)
    problem_low_conf = _doc_is_low_conf(problem_page_conf, conf_threshold)

    problem_chunks = await _load_chunks(db, document_id=problem_document_id)
    structure_mode = structure_pairing_mode()
    structure_summary: StructurePassSummary | None = None
    active_structure_pairs: Sequence[StructurePair] = ()
    scrape_chunks: Sequence[Any] = problem_chunks
    structure_only = False
    # An explicit API handoff snapshots the earlier ON decision and must never
    # fall back to treating the problem doc as an ordinary whole-chunk solution.
    combined_probe = combined_document or (
        structure_mode == "on"
        and (solution_document_id is None or solution_document_id == problem_document_id)
    )

    if combined_probe:
        # The pass must precede scrape in the only mode where answers may share
        # the problem document. scrape_spend is unknowable here, so 0 selects
        # the structure pass's documented 30k floor. This result is reused for
        # both masking and pairing and is never run a second time.
        try:
            structure_result = run_structure_pass(
                problem_chunks=problem_chunks,
                metered_chat=metered_chat,
                scrape_spend=0,
            )
            structure_summary = structure_result.summary()
            question_chunks, backstop_count = _compose_question_mask(
                problem_chunks, structure_result.units
            )
            _log_answer_line_backstop(backstop_count)
            has_problem_answers = _has_problem_answers(structure_result)
            if (
                not structure_result.budget_exhausted
                and question_chunks
                and (has_problem_answers or backstop_count)
            ):
                scrape_chunks = question_chunks
            if not structure_result.budget_exhausted and has_problem_answers and question_chunks:
                effective_solution_document_id = problem_document_id
                solution_chunks = _solution_chunks_from_problem(problem_chunks)
                solution_page_conf = problem_page_conf
                # Regex and semantic retrieval operate on whole persisted chunks.
                # In combined mode that would re-introduce question/answer mixed
                # text, so only exact answer-block structure spans are eligible.
                label_index = {}
                active_structure_pairs = structure_result.pairs
                structure_only = True
            else:
                effective_solution_document_id = None
                solution_chunks = []
                solution_page_conf = {}
                label_index = {}
        except Exception:  # noqa: BLE001 - segmentation failure restores old flow
            effective_solution_document_id = None
            solution_chunks = []
            solution_page_conf = {}
            label_index = {}
            _LOG.exception(
                "authored_set_structure_pass_failed",
                extra={
                    "event": "authored_set_structure_pass_failed",
                    "problem_document_id": problem_document_id,
                    "solution_document_id": solution_document_id,
                    "mode": structure_mode,
                },
            )

    scrape_tokens_before: int | None = None
    if structure_mode != "off" and not combined_probe:
        try:
            scrape_tokens_before = metered_chat.cumulative_tokens()
        except Exception:  # noqa: BLE001 - shadow setup must not affect provisioning
            _LOG.exception(
                "authored_set_structure_pass_failed",
                extra={
                    "event": "authored_set_structure_pass_failed",
                    "problem_document_id": problem_document_id,
                    "solution_document_id": solution_document_id,
                    "mode": structure_mode,
                },
            )
    scrape_result = await scrape_document(
        scrape_chunks,
        chat_fn=metered_chat.scrape_chat_fn(_SCRAPE_SYSTEM_PROMPT),
        triage_chat_fn=metered_chat.scrape_chat_fn(_TRIAGE_SYSTEM_PROMPT),
        max_sections=APOLLO_SCRAPE_MAX_SECTIONS,
        min_candidates=APOLLO_SCRAPE_MIN_CANDIDATES,
        structured=structured_scrape_enabled(),
    )
    if not combined_probe and structure_mode != "off" and scrape_tokens_before is not None:
        try:
            scrape_spend = max(0, metered_chat.cumulative_tokens() - scrape_tokens_before)
            structure_result = run_structure_pass(
                problem_chunks=problem_chunks,
                solution_chunks=solution_chunks,
                metered_chat=metered_chat,
                scrape_spend=scrape_spend,
            )
            structure_summary = structure_result.summary()
            if structure_mode == "on" and not structure_result.budget_exhausted:
                active_structure_pairs = structure_result.pairs
        except Exception:  # noqa: BLE001 - shadow work must never affect provisioning
            _LOG.exception(
                "authored_set_structure_pass_failed",
                extra={
                    "event": "authored_set_structure_pass_failed",
                    "problem_document_id": problem_document_id,
                    "solution_document_id": solution_document_id,
                    "mode": structure_mode,
                },
            )
    await write_tier1_problems(
        db,
        scrape_result.candidates,
        concept_id=concept_id,
        search_space_id=search_space_id,
    )

    results: list[ProblemResult] = []
    for candidate in scrape_result.candidates:
        results.append(
            await _process_authored_candidate(
                db,
                neo,
                candidate=candidate,
                concept_id=concept_id,
                search_space_id=search_space_id,
                solution_document_id=effective_solution_document_id,
                label_index=label_index,
                page_conf=solution_page_conf,
                problem_low_conf=problem_low_conf,
                metered_chat=metered_chat,
                embed_fn=embed_fn,
                conf_threshold=conf_threshold,
                registered=registered,
                reversed_mode=reversed_mode,
                solution_chunks=solution_chunks,
                structure_pairs=active_structure_pairs,
                structure_only=structure_only,
            )
        )

    counts = {"promoted": 0, "rejected": 0, "held_for_review": 0}
    for result in results:
        counts[result.outcome] = counts.get(result.outcome, 0) + 1
    return ProvisioningReport(
        problems=results,
        counts=counts,
        structure_pass=structure_summary,
        combined_document=structure_only,
    )


async def _process_authored_candidate(
    db: AsyncSession,
    neo,
    *,
    candidate: Any,
    concept_id: int,
    search_space_id: int,
    solution_document_id: int | None,
    label_index: dict,
    page_conf: dict[int | None, float | None],
    problem_low_conf: bool,
    metered_chat: Any,
    embed_fn: Callable[[str], Sequence[float]],
    conf_threshold: float,
    registered: Sequence[RegisteredConcept] = (),
    reversed_mode: bool = False,
    solution_chunks: Sequence[tuple[int, str, int | None]] = (),
    structure_pairs: Sequence[StructurePair] = (),
    structure_only: bool = False,
) -> ProblemResult:
    label = getattr(candidate, "label", None)
    retrieve_fn = make_paired_solution_retrieve_fn(
        db,
        solution_document_id=solution_document_id,
        label_index=label_index,
        page_conf=page_conf,
        solution_chunks=solution_chunks,
        structure_pairs=structure_pairs,
        structure_only=structure_only,
    )

    # --- Reversed provisioning: closed-list concept match FIRST ------------- #
    match: ConceptMatch | None = None
    resolved: ResolvedConcept | None = None
    if reversed_mode:
        match = await match_concept(
            getattr(candidate, "problem_text", "") or "",
            registered,
            chat_fn=metered_chat.main,
        )
        if match.no_match:
            # NEVER force-matched: hold for teacher review with the match
            # evidence; no draft, no mint, no KG mutation.
            tier1 = await _find_tier1_row(
                db,
                concept_id=concept_id,
                course_id=search_space_id,
                chunk_content_hash=candidate.chunk_content_hash,
            )
            if tier1 is not None:
                tier1.provenance = {  # type: ignore[assignment]
                    **(tier1.provenance or {}),
                    "authored_review": {
                        "required": True,
                        "reason": "no_matching_concept",
                        "concept_match": match.model_dump(),
                    },
                }
                await db.flush()
            return ProblemResult(
                label=label,
                outcome="held_for_review",
                review_required=True,
                reason="no_matching_concept",
                concept_problem_id=int(tier1.id) if tier1 is not None else None,
            )
        resolved = ResolvedConcept(concept_id=int(match.concept_id or 0), slug=str(match.slug))

    # --- Draft: derive from the paired solution (reversed) or legacy F-o-G -- #
    derived_extras: dict[str, Any] = {}
    draft: ReferenceSolutionDraft | None = None
    if reversed_mode and resolved is not None:
        spans = tuple(await retrieve_fn(candidate))
        draft_source = (
            "llm_paired"
            if getattr(retrieve_fn, "last_match_method", None) == "structure"
            else "extracted"
        )
        solution_spans = tuple(s for s in spans if s.carries_solution)
        if solution_spans:
            concept_row = await db.get(Concept, resolved.concept_id)
            try:
                derived = await derive_reference_graph(
                    candidate,
                    solution_spans,
                    concept_slug=resolved.slug,
                    concept_display_name=(
                        str(concept_row.display_name) if concept_row is not None else resolved.slug
                    ),
                    canonical_symbols=(
                        dict(concept_row.canonical_symbols or {}) if concept_row is not None else {}
                    ),
                    normalization_map=(
                        dict(concept_row.normalization_map or {}) if concept_row is not None else {}
                    ),
                    chat_fn=metered_chat.main,
                )
            except DerivationError as exc:
                return ProblemResult(
                    label=label,
                    outcome="rejected",
                    match_method=getattr(retrieve_fn, "last_match_method", None),
                    ocr_confidence=getattr(retrieve_fn, "last_min_conf", None),
                    diagnostic=f"derivation_error: {exc}",
                )
            draft = ReferenceSolutionDraft(
                solution_source=draft_source,
                reference_solution=derived.reference_solution,
                grounding=solution_spans,
                provenance={
                    "chunk_content_hash": getattr(candidate, "chunk_content_hash", None),
                    "derivation": {"retried": derived.retried},
                },
            )
            derived_extras = {
                "concept_id": resolved.slug,
                "target_unknown": derived.target_unknown
                or (getattr(candidate, "target_unknown", "") or ""),
                "symbolic_mappings": derived.symbolic_mappings,
                "bound_variables": derived.bound_variables,
            }
        # No paired-solution span found: fall through to the legacy path below
        # (its generated draft is held for review — today's semantics).

    if draft is None:
        try:
            draft = await find_or_generate(
                db,
                candidate,
                retrieve_fn=retrieve_fn,
                chat_fn=metered_chat.main,
                augment_recall=True,
            )
        except SolutionDraftError as exc:
            return ProblemResult(
                label=label,
                outcome="rejected",
                diagnostic=f"solution_draft_error: {exc}",
            )

    match_method = getattr(retrieve_fn, "last_match_method", None)
    min_conf = getattr(retrieve_fn, "last_min_conf", None)
    verdict = None
    if draft.solution_source in ("extracted", "llm_paired"):
        verdict = await verify_against_generated(
            db,
            candidate=candidate,
            draft=draft,
            min_conf=min_conf,
            problem_low_conf=problem_low_conf,
            match_method=match_method,
            metered_chat=metered_chat,
            conf_threshold=conf_threshold,
        )

    if draft.solution_source in ("extracted", "llm_paired"):
        pair_verdict = await validate_pair(
            candidate,
            draft,
            retrieve_fn=retrieve_fn,
            judge_fn=metered_chat.cheap,
        )
        rejection = rejection_from_verdict(pair_verdict)
        if rejection is not None:
            return ProblemResult(
                label=label,
                outcome="rejected",
                solution_source=draft.solution_source,
                match_method=match_method,
                ocr_confidence=min_conf,
                diagnostic=rejection.diagnostic,
            )

    review_required = draft.solution_source == "generated" or bool(
        verdict and verdict.review_required
    )
    tier1 = await _find_tier1_row(
        db,
        concept_id=concept_id,
        course_id=search_space_id,
        chunk_content_hash=candidate.chunk_content_hash,
    )
    if review_required:
        reason = verdict.reason if verdict is not None else "generated_no_match"
        if tier1 is not None:
            if draft.augmented_problem_text:
                augmented = _augmented_hold_payload(
                    tier1.to_pydantic_payload(concept_slug=candidate.concept_slug), draft
                )
                if augmented is not None:
                    tier1.apply_inventory_payload(augmented)
            tier1.provenance = {  # type: ignore[assignment]
                **(tier1.provenance or {}),
                "authored_review": {
                    "required": True,
                    "reason": reason,
                    "ocr_confidence": min_conf,
                    "match_method": match_method,
                    "ocr_draft": draft.model_dump(),
                    "generated_alt": verdict.generated_alt if verdict is not None else None,
                    # Reversed provisioning: the matched concept rides along so
                    # the approve path can thread it as resolved_concept.
                    "concept_match": match.model_dump() if match is not None else None,
                    "augmented": ("explain_why" if draft.augmented_problem_text else None),
                },
            }
            await db.flush()
        return ProblemResult(
            label=label,
            outcome="held_for_review",
            solution_source=draft.solution_source,
            match_method=match_method,
            ocr_confidence=min_conf,
            review_required=True,
            reason=reason,
            concept_problem_id=int(tier1.id) if tier1 is not None else None,
        )

    pair = build_approved_pair(candidate, draft, search_space_id=search_space_id)
    if derived_extras:
        # The derived top-level keys (matched concept slug, target_unknown,
        # symbolic_mappings, bound_variables) ride the problem dict so
        # promote's annotate spreads them into the persisted payload and lint
        # gate 7 sees bound_variables.
        pair = pair.model_copy(update={"problem": {**pair.problem, **derived_extras}})
    if tier1 is None:
        return ProblemResult(
            label=label,
            outcome="rejected",
            solution_source=draft.solution_source,
            match_method=match_method,
            ocr_confidence=min_conf,
            diagnostic="missing_tier1_row",
        )

    try:
        # Mint AND promote ride ONE nested SAVEPOINT — mint is TRANSACTIONAL
        # with promotion. Two rollback triggers:
        #   * a fail-closed TagMintError (raised AFTER tag_mint has already
        #     flushed concept / KGEntity / apollo_dedup_decisions rows for this
        #     candidate);
        #   * a LINT REJECTION from ``promote`` (e.g. the gate-8 duplicate
        #     check) — previously the mint's flushed rows survived the run's
        #     end-of-run commit as orphaned KG rows (unreachable by any
        #     promoted problem, yet live dedup targets for the NEXT
        #     candidate; verified as a 17->33 entity doubling). ``MintRejected``
        #     unwinds the savepoint, then the except arm reports the rejection.
        # A ``CanonProjectionError`` from ``promote`` still propagates as a
        # run-level failure: its PG writes roll back with the savepoint and the
        # already-MERGEd ``:Canon`` nodes are idempotent/node-only (harmless).
        async with db.begin_nested():
            mint_kwargs: dict[str, Any] = (
                {"resolved_concept": resolved} if resolved is not None else {}
            )
            mint_plan = await tag_and_mint(
                db,
                pair,
                chat_fn=_tag_mint_chat_fn(metered_chat),
                embed_fn=embed_fn,
                **mint_kwargs,
            )
            existing_problem_hashes = await _authored_concept_dup_hashes(
                db, concept_id=mint_plan.concept_id, course_id=search_space_id
            )
            result: PromoteResult = await promote(
                db,
                neo,
                problem=pair.problem,
                mint_plan=mint_plan,
                search_space_id=search_space_id,
                concept_problem_id=int(tier1.id),
                existing_problem_hashes=existing_problem_hashes,
                solution_source=pair.solution_source,
            )
            if not result.promoted:
                raise MintRejected(result)
    except TagMintError as exc:
        # The mint draft for THIS problem is unusable (e.g. the LLM prereq draft
        # names an unminted entity key). Reject just this candidate — mirroring
        # the SolutionDraftError handling above — instead of letting it abort the
        # whole set. (Infra-level CanonProjectionError from promote is left to
        # propagate: that is a run-level failure by design.)
        return ProblemResult(
            label=label,
            outcome="rejected",
            solution_source=draft.solution_source,
            match_method=match_method,
            ocr_confidence=min_conf,
            diagnostic=f"tag_mint_error: {exc}",
            concept_problem_id=int(tier1.id),
        )
    except MintRejected as rejected:
        result = rejected.result
        if isinstance(result, PromoteHeldForReview):
            tier1.provenance = {  # type: ignore[assignment]
                **(tier1.provenance or {}),
                "authored_review": {
                    "required": True,
                    "reason": "promotion_lint_unresolved",
                    "failed_gate": result.failed_gate,
                    "diagnostic": result.diagnostic,
                    "ocr_confidence": min_conf,
                    "match_method": match_method,
                    "ocr_draft": draft.model_dump(),
                    "generated_alt": verdict.generated_alt if verdict is not None else None,
                    "concept_match": match.model_dump() if match is not None else None,
                    "augmented": "explain_why" if draft.augmented_problem_text else None,
                },
            }
            await db.flush()
            return ProblemResult(
                label=label,
                outcome="held_for_review",
                solution_source=draft.solution_source,
                match_method=match_method,
                ocr_confidence=min_conf,
                failed_gate=result.failed_gate,
                diagnostic=result.diagnostic,
                review_required=True,
                reason="promotion_lint_unresolved",
                concept_problem_id=int(tier1.id),
            )
        return ProblemResult(
            label=label,
            outcome="rejected",
            solution_source=draft.solution_source,
            match_method=match_method,
            ocr_confidence=min_conf,
            failed_gate=result.failed_gate,
            diagnostic=result.diagnostic,
            concept_problem_id=int(tier1.id),
        )
    return ProblemResult(
        label=label,
        outcome="promoted",
        solution_source=draft.solution_source,
        match_method=match_method,
        ocr_confidence=min_conf,
        concept_problem_id=int(tier1.id),
    )
