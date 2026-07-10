"""Authored-set provisioning (WU-AAS).

Scrape a problem document, ground each candidate against ONLY its paired
solution document, verify OCR-suspect extractions, and promote trusted references.
Generated or suspect references stay tier-1 for teacher review.

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

import os
from collections.abc import Callable, Sequence
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apollo.persistence.models import Concept, ConceptProblem
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
from apollo.provisioning.authored_sets.verification import verify_against_generated
from apollo.provisioning.concept_match import ConceptMatch, match_concept
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

_DEFAULT_CONF_THRESHOLD = 0.6


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
    registered = await list_registered_concepts(db, search_space_id=search_space_id)
    reversed_mode = reversed_provisioning_enabled() and bool(registered)
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
                registered=registered,
                reversed_mode=reversed_mode,
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
    registered: Sequence[RegisteredConcept] = (),
    reversed_mode: bool = False,
) -> ProblemResult:
    label = getattr(candidate, "label", None)
    retrieve_fn = make_paired_solution_retrieve_fn(
        db,
        solution_document_id=solution_document_id,
        label_index=label_index,
        page_conf=page_conf,
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
                db, concept_id=concept_id, chunk_content_hash=candidate.chunk_content_hash
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
                solution_source="extracted",
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
                    # Reversed provisioning: the matched concept rides along so
                    # the approve path can thread it as resolved_concept.
                    "concept_match": match.model_dump() if match is not None else None,
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
        #     promoted ConceptProblem, yet live dedup targets for the NEXT
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
