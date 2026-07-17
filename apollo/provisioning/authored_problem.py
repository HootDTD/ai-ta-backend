"""Synchronous provisioning for a single teacher-authored problem.

This is the live teacher path.  It is intentionally independent of the removed
background auto-provisioning queue and worker.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apollo.persistence.models import ConceptProblem
from apollo.provisioning.metered_chat import CostBudgetExceeded
from apollo.provisioning.pairing_gate import rejection_from_verdict, validate_pair
from apollo.provisioning.problem_hash import problem_dup_hash
from apollo.provisioning.promote import PromoteResult, promote
from apollo.provisioning.solution import (
    GroundingSpan,
    SolutionDraftError,
    build_authored_approved_pair,
    construct_authored_reference,
)
from apollo.provisioning.tag_mint import TagMintError, tag_and_mint
from apollo.schemas.problem import Problem


@dataclass(frozen=True)
class AuthoredProvisionResult:
    """Outcome for one synchronously provisioned authored problem."""

    outcome: str
    stage: str
    diagnostic: str = ""
    failed_gate: int | None = None


async def _no_retrieve(_question) -> tuple[GroundingSpan, ...]:
    return ()


async def _find_tier1_row_id(
    db: AsyncSession, *, concept_id: int, problem_code: str
) -> int | None:
    return (
        await db.execute(
            select(ConceptProblem.id)
            .where(ConceptProblem.concept_id == concept_id)
            .where(ConceptProblem.problem_code == problem_code)
        )
    ).scalar_one_or_none()


async def _concept_dup_hashes(db: AsyncSession, *, concept_id: int) -> set[str]:
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
            problem = Problem.model_validate(payload)
        except (ValidationError, ValueError):
            continue
        hashes.add(problem_dup_hash(problem))
    return hashes


async def provision_authored_problem(
    db: AsyncSession,
    neo,
    authored,
    *,
    search_space_id: int,
    ingest_concept_id: int,
    construct_chat_fn: Callable[..., str],
    judge_fn: Callable[..., str],
    tag_chat_fn: Callable[[str], str],
    embed_fn: Callable[[str], Sequence[float]],
) -> AuthoredProvisionResult:
    """Construct, verify, mint, and promote one teacher-authored problem.

    Candidate failures are returned to the authored-set result ledger.  They no
    longer write the worker-only ``apollo_rejected_problems`` audit table.
    """
    try:
        draft = await construct_authored_reference(authored, chat_fn=construct_chat_fn)
    except SolutionDraftError as exc:
        return AuthoredProvisionResult(outcome="rejected", stage="construct", diagnostic=str(exc))

    verdict = await validate_pair(authored, draft, retrieve_fn=_no_retrieve, judge_fn=judge_fn)
    rejection = rejection_from_verdict(verdict)
    if rejection is not None:
        return AuthoredProvisionResult(
            outcome="rejected", stage="pairing_gate", diagnostic=rejection.diagnostic
        )

    pair = build_authored_approved_pair(authored, draft, search_space_id=search_space_id)
    try:
        mint_plan = await tag_and_mint(db, pair, chat_fn=tag_chat_fn, embed_fn=embed_fn)
    except (TagMintError, CostBudgetExceeded) as exc:
        return AuthoredProvisionResult(outcome="rejected", stage="tag_mint", diagnostic=str(exc))

    concept_problem_id = await _find_tier1_row_id(
        db, concept_id=ingest_concept_id, problem_code=authored.problem_code
    )
    if concept_problem_id is None:
        raise RuntimeError(f"authored problem {authored.problem_code!r} has no Tier-1 row")

    existing_problem_hashes = await _concept_dup_hashes(db, concept_id=mint_plan.concept_id)
    result: PromoteResult = await promote(
        db,
        neo,
        problem=pair.problem,
        mint_plan=mint_plan,
        search_space_id=search_space_id,
        concept_problem_id=concept_problem_id,
        existing_problem_hashes=existing_problem_hashes,
    )
    if not result.promoted:
        return AuthoredProvisionResult(
            outcome="rejected",
            stage="promotion_lint",
            diagnostic=result.diagnostic,
            failed_gate=result.failed_gate,
        )
    return AuthoredProvisionResult(outcome="promoted", stage="ok")


__all__ = ["AuthoredProvisionResult", "provision_authored_problem"]
