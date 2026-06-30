"""Teacher-gated HTTP surface for authored problem/solution sets (WU-AAS).

POST indexes both docs hidden from student retrieval, persists the pairing, and
runs provisioning in an in-process background task. GET endpoints poll status
and result summaries; approve promotes a held reference chosen by the teacher.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Literal

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    Form,
    HTTPException,
    Request,
    UploadFile,
)
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from apollo.auth_deps import require_course_member, require_user
from apollo.persistence.models import AuthoredSet, ConceptProblem
from apollo.provisioning.authored_sets.indexing import index_authored_doc
from apollo.provisioning.authored_sets.orchestrator import (
    _authored_concept_dup_hashes,
    _tag_mint_chat_fn,
    run_authored_set_provisioning,
)
from apollo.provisioning.metered_chat import MeteredChat
from apollo.provisioning.promote import promote
from apollo.provisioning.solution import ReferenceSolutionDraft, build_approved_pair
from apollo.provisioning.tag_mint import tag_and_mint
from database.session import get_async_session, get_db_session
from indexing.document_embedder import embed_text

_LOG = logging.getLogger(__name__)

router = APIRouter(tags=["apollo-authored-sets"])


class ApproveBody(BaseModel):
    reference: Literal["ocr", "generated"] = "ocr"


def get_neo4j_client():
    """Late import avoids a module cycle while keeping a test patch seam."""
    from apollo.api import get_neo4j_client as _get_neo4j_client

    return _get_neo4j_client()


async def _next_set_index(db: AsyncSession, search_space_id: int) -> int:
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


async def _run_set_background(
    *,
    set_id: int,
    search_space_id: int,
    set_index: int,
    problem_bytes: bytes,
    problem_title: str,
    solution_bytes: bytes,
    solution_title: str,
) -> None:
    """Own a fresh session; request-scoped sessions are closed before this runs."""
    try:
        async with get_async_session() as db:
            await _set_status(db, set_id, "indexing")
            problem_document_id = await index_authored_doc(
                db,
                search_space_id=search_space_id,
                file_bytes=problem_bytes,
                title=problem_title,
                set_index=set_index,
                role="problem",
            )
            solution_document_id = await index_authored_doc(
                db,
                search_space_id=search_space_id,
                file_bytes=solution_bytes,
                title=solution_title,
                set_index=set_index,
                role="solution",
            )

            row = await db.get(AuthoredSet, set_id)
            if row is None:
                return
            row.problem_document_id = problem_document_id
            row.solution_document_id = solution_document_id
            row.status = "provisioning"
            row.updated_at = datetime.now(UTC)
            await db.commit()

            report = await run_authored_set_provisioning(
                db,
                get_neo4j_client(),
                search_space_id=search_space_id,
                problem_document_id=problem_document_id,
                solution_document_id=solution_document_id,
                metered_chat=_make_metered_chat(document_id=problem_document_id),
            )
            row = await db.get(AuthoredSet, set_id)
            if row is None:
                return
            row.result_summary = report.model_dump()
            row.status = "done"
            row.updated_at = datetime.now(UTC)
            await db.commit()
    except Exception as exc:  # noqa: BLE001 - persist failed status, never escape task
        _LOG.exception("authored_set_background_failed", extra={"set_id": set_id})
        async with get_async_session() as db:
            await _set_status(db, set_id, "failed", diagnostic=str(exc))


def _make_metered_chat(*, document_id: int) -> MeteredChat:
    ingest_run = SimpleNamespace(
        id=None,
        llm_calls=0,
        llm_tokens_in=0,
        llm_tokens_out=0,
        llm_cost_usd=0.0,
    )
    return MeteredChat(ingest_run=ingest_run, document_id=document_id)


async def _set_status(
    db: AsyncSession,
    set_id: int,
    status: str,
    *,
    diagnostic: str | None = None,
) -> None:
    row = await db.get(AuthoredSet, set_id)
    if row is None:
        return
    row.status = status  # type: ignore[assignment]
    row.updated_at = datetime.now(UTC)  # type: ignore[assignment]
    if diagnostic is not None:
        row.result_summary = {**(row.result_summary or {}), "error": diagnostic}  # type: ignore[assignment]
    await db.commit()


@router.get("/authored-sets")
async def list_authored_sets(
    request: Request,
    search_space_id: int,
    db: AsyncSession = Depends(get_db_session),
) -> dict:
    auth = await require_user(request)
    await require_course_member(db=db, auth=auth, search_space_id=search_space_id)
    rows = (
        (
            await db.execute(
                select(AuthoredSet)
                .where(AuthoredSet.search_space_id == search_space_id)
                .order_by(AuthoredSet.set_index.asc())
            )
        )
        .scalars()
        .all()
    )
    return {
        "sets": [
            {
                "set_id": int(row.id),
                "set_index": row.set_index,
                "status": row.status,
                "problem_document_id": row.problem_document_id,
                "solution_document_id": row.solution_document_id,
            }
            for row in rows
        ]
    }


@router.get("/authored-sets/{set_id}")
async def get_authored_set(
    set_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db_session),
) -> dict:
    auth = await require_user(request)
    row = await db.get(AuthoredSet, set_id)
    if row is None:
        raise HTTPException(status_code=404, detail="authored set not found")
    await require_course_member(db=db, auth=auth, search_space_id=int(row.search_space_id))
    return {
        "set_id": int(row.id),
        "set_index": row.set_index,
        "status": row.status,
        "problem_document_id": row.problem_document_id,
        "solution_document_id": row.solution_document_id,
        "result_summary": row.result_summary or {},
    }


@router.post("/authored-sets/{set_id}/problems/{problem_id}/approve")
async def approve_held_problem(
    set_id: int,
    problem_id: int,
    body: ApproveBody,
    request: Request,
    db: AsyncSession = Depends(get_db_session),
) -> dict:
    auth = await require_user(request)
    authored_set = await db.get(AuthoredSet, set_id)
    if authored_set is None:
        raise HTTPException(status_code=404, detail="authored set not found")
    await require_course_member(
        db=db,
        auth=auth,
        search_space_id=int(authored_set.search_space_id),
    )

    row = await db.get(ConceptProblem, problem_id)
    review = (row.provenance or {}).get("authored_review") if row is not None else None  # type: ignore[call-overload]
    if row is None or not review or not review.get("required"):
        raise HTTPException(status_code=409, detail="problem is not held for review")

    chosen = (
        review.get("generated_alt") if body.reference == "generated" else review.get("ocr_draft")
    )
    if chosen is None:
        raise HTTPException(status_code=422, detail=f"no '{body.reference}' reference stored")
    draft = ReferenceSolutionDraft.model_validate(chosen)

    candidate = _candidate_from_row(row)
    pair = build_approved_pair(
        candidate,
        draft,
        search_space_id=int(authored_set.search_space_id),
    )
    metered_chat = _make_metered_chat(document_id=int(authored_set.problem_document_id or 0))
    mint_plan = await tag_and_mint(
        db,
        pair,
        chat_fn=_tag_mint_chat_fn(metered_chat),
        embed_fn=embed_text,
    )
    existing_hashes = await _authored_concept_dup_hashes(db, concept_id=mint_plan.concept_id)
    result = await promote(
        db,
        get_neo4j_client(),
        problem=pair.problem,
        mint_plan=mint_plan,
        search_space_id=int(authored_set.search_space_id),
        concept_problem_id=problem_id,
        existing_problem_hashes=existing_hashes,
    )
    if result.promoted:
        row.provenance = {  # type: ignore[assignment]
            **(row.provenance or {}),
            "authored_review": {
                **review,
                "required": False,
                "approved_reference": body.reference,
            },
        }
        await db.commit()
    return {
        "promoted": result.promoted,
        "failed_gate": result.failed_gate,
        "diagnostic": result.diagnostic,
    }


def _candidate_from_row(row: ConceptProblem) -> SimpleNamespace:
    payload: dict = row.payload or {}  # type: ignore[assignment]
    provenance: dict = row.provenance or {}  # type: ignore[assignment]
    return SimpleNamespace(
        problem_text=payload.get("problem_text", ""),
        given_values=payload.get("given_values", {}) or {},
        target_unknown=payload.get("target_unknown", ""),
        difficulty=payload.get("difficulty", row.difficulty),
        chunk_content_hash=provenance.get("chunk_content_hash", ""),
        concept_slug=payload.get("concept_slug", "provisional.inventory"),
        label=payload.get("label"),
        document_id=provenance.get("document_id"),
        page=provenance.get("page"),
    )
