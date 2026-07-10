"""Teacher-facing concept authoring API (WU-TCA).

Teachers write their course's concept list directly (name + description)
instead of relying on scrape-minted concepts. An authored concept is a bare
``apollo_concepts`` row — that is already a first-class citizen everywhere it
matters:

- reversed provisioning's closed-list matcher targets EVERY non-provisional
  concept row (``curriculum_db.list_registered_concepts``), problems or not;
- the student browse list (``curriculum_db.list_course_concepts``) only shows
  concepts once a teachable problem attaches, so authoring ahead of content
  never surfaces an empty concept to students.

Concept creation reuses ``tag_mint_persist.resolve_or_create_concept`` (the §6
namespace contract: key on BIGINT id, never slug) and then fills the
migration-038 ``description`` column the matcher prompt renders.

Deletion is deliberately conservative: a concept with ANY problems or KG
entities 409s — tearing down provisioned content belongs to the authored-set
delete flow, not this editor.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from apollo.auth_deps import require_course_teacher, require_user
from apollo.persistence.models import Concept, ConceptProblem, KGEntity, Subject
from apollo.provisioning.concept_match import norm_slug
from apollo.provisioning.scrape import PROVISIONAL_CONCEPT_SLUG
from apollo.provisioning.tag_mint_persist import resolve_or_create_concept
from database.session import get_db_session

_LOG = logging.getLogger(__name__)

router = APIRouter(tags=["apollo-teacher-concepts"])

_MAX_NAME_LEN = 200
_MAX_DESCRIPTION_LEN = 4000
_MAX_SLUG_LEN = 80


class ConceptCreateBody(BaseModel):
    search_space_id: int
    display_name: str = Field(min_length=1, max_length=_MAX_NAME_LEN)
    description: str = Field(default="", max_length=_MAX_DESCRIPTION_LEN)


class ConceptUpdateBody(BaseModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=_MAX_NAME_LEN)
    description: str | None = Field(default=None, max_length=_MAX_DESCRIPTION_LEN)


def mint_slug(display_name: str) -> str:
    """Underscore-style slug from a human name (registry convention —
    ``norm_slug`` keys hyphens and underscores identically, so authored slugs
    stay mergeable with registry/scrape-minted ones)."""
    slug = re.sub(r"[^a-z0-9]+", "_", display_name.strip().lower()).strip("_")
    return slug[:_MAX_SLUG_LEN].rstrip("_")


async def _course_concept_or_404(db: AsyncSession, concept_id: int) -> tuple[Concept, int]:
    """Load a concept + its course id; 404 when absent or provisional.

    The provisional-inventory row is a scrape seam, not curriculum — it is
    invisible to this API (404, not 403, to avoid leaking its existence)."""
    row = (
        await db.execute(
            select(Concept, Subject.search_space_id)
            .join(Subject, Subject.id == Concept.subject_id)
            .where(Concept.id == concept_id)
        )
    ).first()
    if row is None or row[0].slug == PROVISIONAL_CONCEPT_SLUG:
        raise HTTPException(status_code=404, detail="Concept not found")
    return row[0], int(row[1])


def _concept_payload(concept: Concept, *, problem_count: int, teachable_count: int) -> dict:
    return {
        "id": int(concept.id),
        "slug": concept.slug,
        "display_name": concept.display_name,
        "description": concept.description,
        "problem_count": problem_count,
        "has_teachable_problems": teachable_count > 0,
        "created_at": concept.created_at.isoformat() if concept.created_at else None,
        "updated_at": concept.updated_at.isoformat() if concept.updated_at else None,
    }


@router.get("/teacher/concepts")
async def list_teacher_concepts(
    search_space_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db_session),
) -> dict:
    """Every authored/registered concept of the course (provisional excluded),
    with problem counts so the UI can gate deletion client-side too."""
    auth = await require_user(request)
    await require_course_teacher(db=db, auth=auth, search_space_id=search_space_id)

    problem_count = func.count(ConceptProblem.id)
    teachable_count = func.count(ConceptProblem.id).filter(
        ConceptProblem.tier == 2, ConceptProblem.quarantined_at.is_(None)
    )
    result = await db.execute(
        select(Concept, problem_count, teachable_count)
        .join(Subject, Subject.id == Concept.subject_id)
        .outerjoin(ConceptProblem, ConceptProblem.concept_id == Concept.id)
        .where(
            Subject.search_space_id == search_space_id,
            Concept.slug != PROVISIONAL_CONCEPT_SLUG,
        )
        .group_by(Concept.id)
        .order_by(Concept.id)
    )
    concepts = [
        _concept_payload(concept, problem_count=int(pc), teachable_count=int(tc))
        for concept, pc, tc in result.all()
    ]
    return {"search_space_id": search_space_id, "concepts": concepts}


@router.post("/teacher/concepts", status_code=201)
async def create_teacher_concept(
    body: ConceptCreateBody,
    request: Request,
    db: AsyncSession = Depends(get_db_session),
) -> dict:
    auth = await require_user(request)
    await require_course_teacher(db=db, auth=auth, search_space_id=body.search_space_id)

    slug = mint_slug(body.display_name)
    if not slug:
        raise HTTPException(status_code=400, detail="Concept name must contain letters or digits")
    if slug == PROVISIONAL_CONCEPT_SLUG or norm_slug(slug) == norm_slug(PROVISIONAL_CONCEPT_SLUG):
        raise HTTPException(status_code=400, detail="Reserved concept name")

    # 409 on an existing concept with the same normalized slug — the teacher
    # retyping a name should edit the existing row, not silently merge into it.
    existing = (
        (
            await db.execute(
                select(Concept.slug)
                .join(Subject, Subject.id == Concept.subject_id)
                .where(Subject.search_space_id == body.search_space_id)
            )
        )
        .scalars()
        .all()
    )
    if any(norm_slug(s) == norm_slug(slug) for s in existing):
        raise HTTPException(status_code=409, detail=f"A concept with slug '{slug}' already exists")

    concept_id = await resolve_or_create_concept(
        db,
        search_space_id=body.search_space_id,
        slug=slug,
        display_name=body.display_name.strip(),
    )
    concept = (await db.execute(select(Concept).where(Concept.id == concept_id))).scalar_one()
    concept.description = body.description.strip()  # type: ignore[assignment]
    concept.updated_at = datetime.now(UTC)  # type: ignore[assignment]
    await db.commit()

    _LOG.info(
        "teacher-concepts: created concept_id=%s slug=%s ss=%s by user=%s",
        concept_id,
        slug,
        body.search_space_id,
        auth.user_id,
    )
    return _concept_payload(concept, problem_count=0, teachable_count=0)


@router.patch("/teacher/concepts/{concept_id}")
async def update_teacher_concept(
    concept_id: int,
    body: ConceptUpdateBody,
    request: Request,
    db: AsyncSession = Depends(get_db_session),
) -> dict:
    """Edit name/description. The slug is stable across renames — provisioned
    problems, KG entities, and match decisions key on it (§6: consumers key on
    the BIGINT id, but logs/decisions render slugs; a silent re-slug would
    orphan those references)."""
    auth = await require_user(request)
    concept, search_space_id = await _course_concept_or_404(db, concept_id)
    await require_course_teacher(db=db, auth=auth, search_space_id=search_space_id)

    if body.display_name is not None:
        concept.display_name = body.display_name.strip()  # type: ignore[assignment]
    if body.description is not None:
        concept.description = body.description.strip()  # type: ignore[assignment]
    concept.updated_at = datetime.now(UTC)  # type: ignore[assignment]
    await db.commit()

    counts = (
        await db.execute(
            select(
                func.count(ConceptProblem.id),
                func.count(ConceptProblem.id).filter(
                    ConceptProblem.tier == 2, ConceptProblem.quarantined_at.is_(None)
                ),
            ).where(ConceptProblem.concept_id == concept_id)
        )
    ).one()
    return _concept_payload(concept, problem_count=int(counts[0]), teachable_count=int(counts[1]))


@router.delete("/teacher/concepts/{concept_id}")
async def delete_teacher_concept(
    concept_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db_session),
) -> dict:
    auth = await require_user(request)
    concept, search_space_id = await _course_concept_or_404(db, concept_id)
    await require_course_teacher(db=db, auth=auth, search_space_id=search_space_id)

    problem_count = (
        await db.execute(
            select(func.count(ConceptProblem.id)).where(ConceptProblem.concept_id == concept_id)
        )
    ).scalar_one()
    entity_count = (
        await db.execute(select(func.count(KGEntity.id)).where(KGEntity.concept_id == concept_id))
    ).scalar_one()
    if int(problem_count) or int(entity_count):
        raise HTTPException(
            status_code=409,
            detail=(
                "Concept has provisioned content "
                f"({int(problem_count)} problems, {int(entity_count)} KG entities); "
                "delete its authored sets first"
            ),
        )

    await db.delete(concept)
    await db.commit()
    _LOG.info(
        "teacher-concepts: deleted concept_id=%s slug=%s ss=%s by user=%s",
        concept_id,
        concept.slug,
        search_space_id,
        auth.user_id,
    )
    return {"deleted": True, "id": concept_id}
