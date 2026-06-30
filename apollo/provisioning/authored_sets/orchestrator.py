"""Authored-set provisioning (WU-AAS).

Scrape a problem document, ground each candidate against ONLY its paired
solution document, verify OCR-suspect extractions, and promote trusted references.
Generated or suspect references stay tier-1 for teacher review.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apollo.persistence.models import ConceptProblem
from apollo.provisioning.authored_sets.label_match import build_solution_label_index
from apollo.provisioning.authored_sets.paired_retrieval import (
    chunk_ocr_confidence,
    load_solution_chunks,
    make_paired_solution_retrieve_fn,
)
from apollo.provisioning.authored_sets.verification import verify_against_generated
from apollo.provisioning.orchestrator import (
    _SCRAPE_SYSTEM_PROMPT,
    _TAG_MINT_SYSTEM_PROMPT,
    _TRIAGE_SYSTEM_PROMPT,
    APOLLO_SCRAPE_MAX_SECTIONS,
    APOLLO_SCRAPE_MIN_CANDIDATES,
    _load_chunks,
    structured_scrape_enabled,
)
from apollo.provisioning.pairing_gate import rejection_from_verdict, validate_pair
from apollo.provisioning.problem_hash import problem_dup_hash
from apollo.provisioning.promote import PromoteResult, promote
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
from apollo.schemas.problem import Problem

__all__ = ["ProblemResult", "ProvisioningReport", "run_authored_set_provisioning"]

_DEFAULT_CONF_THRESHOLD = 0.6


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


async def _find_tier1_row(
    db: AsyncSession, *, concept_id: int, chunk_content_hash: str
) -> ConceptProblem | None:
    return (
        await db.execute(
            select(ConceptProblem)
            .where(ConceptProblem.concept_id == concept_id)
            .where(ConceptProblem.problem_code == f"scrape.{chunk_content_hash}")
        )
    ).scalar_one_or_none()


async def _authored_concept_dup_hashes(db: AsyncSession, *, concept_id: int) -> set[str]:
    rows = (
        (
            await db.execute(
                select(ConceptProblem.payload)
                .where(ConceptProblem.concept_id == concept_id)
                .where(ConceptProblem.tier == 2)
            )
        )
        .scalars()
        .all()
    )
    hashes: set[str] = set()
    for payload in rows:
        try:
            hashes.add(problem_dup_hash(Problem.model_validate(payload)))
        except (ValidationError, ValueError):
            continue
    return hashes


async def run_authored_set_provisioning(
    db: AsyncSession,
    neo,
    *,
    search_space_id: int,
    problem_document_id: int,
    solution_document_id: int,
    metered_chat: Any,
    embed_fn: Callable[[str], Sequence[float]] | None = None,
    conf_threshold: float = _DEFAULT_CONF_THRESHOLD,
) -> ProvisioningReport:
    """Run trigger-agnostic provisioning for one problem/solution document pair."""
    if embed_fn is None:
        from indexing.document_embedder import embed_text as embed_fn  # type: ignore
    assert embed_fn is not None

    concept_id = await resolve_or_create_provisional_concept(db, search_space_id=search_space_id)
    solution_chunks = await load_solution_chunks(db, solution_document_id=solution_document_id)
    label_index = build_solution_label_index(solution_chunks)
    solution_page_conf = await chunk_ocr_confidence(db, document_id=solution_document_id)
    problem_page_conf = await chunk_ocr_confidence(db, document_id=problem_document_id)
    problem_low_conf = _doc_is_low_conf(problem_page_conf, conf_threshold)

    problem_chunks = await _load_chunks(db, document_id=problem_document_id)
    scrape_result = await scrape_document(
        problem_chunks,
        chat_fn=metered_chat.scrape_chat_fn(_SCRAPE_SYSTEM_PROMPT),
        triage_chat_fn=metered_chat.scrape_chat_fn(_TRIAGE_SYSTEM_PROMPT),
        max_sections=APOLLO_SCRAPE_MAX_SECTIONS,
        min_candidates=APOLLO_SCRAPE_MIN_CANDIDATES,
        structured=structured_scrape_enabled(),
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
                solution_document_id=solution_document_id,
                label_index=label_index,
                page_conf=solution_page_conf,
                problem_low_conf=problem_low_conf,
                metered_chat=metered_chat,
                embed_fn=embed_fn,
                conf_threshold=conf_threshold,
            )
        )

    counts = {"promoted": 0, "rejected": 0, "held_for_review": 0}
    for result in results:
        counts[result.outcome] = counts.get(result.outcome, 0) + 1
    return ProvisioningReport(problems=results, counts=counts)


async def _process_authored_candidate(
    db: AsyncSession,
    neo,
    *,
    candidate: Any,
    concept_id: int,
    search_space_id: int,
    solution_document_id: int,
    label_index: dict,
    page_conf: dict[int | None, float | None],
    problem_low_conf: bool,
    metered_chat: Any,
    embed_fn: Callable[[str], Sequence[float]],
    conf_threshold: float,
) -> ProblemResult:
    label = getattr(candidate, "label", None)
    retrieve_fn = make_paired_solution_retrieve_fn(
        db,
        solution_document_id=solution_document_id,
        label_index=label_index,
        page_conf=page_conf,
    )

    try:
        draft = await find_or_generate(
            db, candidate, retrieve_fn=retrieve_fn, chat_fn=metered_chat.main
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
    if draft.solution_source == "extracted":
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
        db, concept_id=concept_id, chunk_content_hash=candidate.chunk_content_hash
    )
    if review_required:
        reason = verdict.reason if verdict is not None else "generated_no_match"
        if tier1 is not None:
            tier1.provenance = {  # type: ignore[assignment]
                **(tier1.provenance or {}),
                "authored_review": {
                    "required": True,
                    "reason": reason,
                    "ocr_confidence": min_conf,
                    "match_method": match_method,
                    "ocr_draft": draft.model_dump(),
                    "generated_alt": verdict.generated_alt if verdict is not None else None,
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
    if tier1 is None:
        return ProblemResult(
            label=label,
            outcome="rejected",
            solution_source=draft.solution_source,
            match_method=match_method,
            ocr_confidence=min_conf,
            diagnostic="missing_tier1_row",
        )

    mint_plan = await tag_and_mint(
        db, pair, chat_fn=_tag_mint_chat_fn(metered_chat), embed_fn=embed_fn
    )
    existing_problem_hashes = await _authored_concept_dup_hashes(
        db, concept_id=mint_plan.concept_id
    )
    result: PromoteResult = await promote(
        db,
        neo,
        problem=pair.problem,
        mint_plan=mint_plan,
        search_space_id=search_space_id,
        concept_problem_id=int(tier1.id),
        existing_problem_hashes=existing_problem_hashes,
    )
    if not result.promoted:
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
