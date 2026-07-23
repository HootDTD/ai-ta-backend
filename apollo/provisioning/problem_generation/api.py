"""Teacher-gated batch API for generated problem variants and review."""

from __future__ import annotations

import logging
from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apollo.auth_deps import require_course_teacher, require_user
from apollo.persistence.models import (
    Concept,
    IngestRun,
    Problem,
    ProvisioningRun,
)
from apollo.provisioning.authored_sets.api import ApproveBody, approve_held_row
from apollo.provisioning.authored_sets.observability import (
    finalize_ingest_run,
    record_ingest_error,
    start_ingest_run,
)
from apollo.provisioning.metered_chat import MeteredChat
from apollo.provisioning.problem_generation.generator import (
    generate_problem_variants,
    generation_token_ceiling,
    problem_generation_enabled,
)
from apollo.provisioning.scrape import PROVISIONAL_CONCEPT_SLUG
from apollo.provisioning.tag_mint import ResolvedConcept
from database.session import get_async_session, get_db_session

_LOG = logging.getLogger(__name__)
_PROBLEM_TEXT_CAP = 2000


def _json_dict(value: Any) -> dict[str, Any]:
    """Runtime dict of a legacy JSON ``Column`` attribute (mypy sees the
    descriptor type, not the instance value)."""
    return value or {}


router = APIRouter(tags=["apollo-problem-generation"])


class GenerateVariantsBody(BaseModel):
    seed_problem_ids: list[int] = Field(min_length=1, max_length=50)
    count: int = Field(ge=1)


async def _get_generation_run(db: AsyncSession, run_id: int) -> ProvisioningRun | None:
    row = await db.get(ProvisioningRun, run_id)
    return row if row is not None and row.kind == "generation" else None


async def _course_concept_or_404(db: AsyncSession, concept_id: int) -> tuple[Concept, int]:
    row = (
        await db.execute(
            select(Concept, Concept.course_id)
            .where(Concept.id == concept_id)
        )
    ).first()
    if row is None or row[0].slug == PROVISIONAL_CONCEPT_SLUG:
        raise HTTPException(status_code=404, detail="Concept not found")
    return row[0], int(row[1])


@router.get("/problem-generation/concepts/{concept_id}/seeds")
async def list_generation_seeds(
    concept_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db_session),
) -> dict:
    """Selectable seeds for a generation run: the concept's teachable problems.

    Deliberately NOT flag-gated (read-only, mirrors the runs GETs) — the UI
    seed picker must work even while generation itself is toggled off.
    """
    _concept, search_space_id = await _course_concept_or_404(db, concept_id)
    auth = await require_user(request)
    await require_course_teacher(db=db, auth=auth, search_space_id=search_space_id)
    rows = (
        (
            await db.execute(
                select(Problem)
                .where(
                    Problem.course_id == search_space_id,
                    Problem.concept_id == concept_id,
                    Problem.tier == 2,
                    Problem.quarantined_at.is_(None),
                )
                .order_by(Problem.id.asc())
            )
        )
        .scalars()
        .all()
    )
    return {
        "seeds": [
            {
                "concept_problem_id": int(row.id),
                "problem_text": str(row.problem_text)[:_PROBLEM_TEXT_CAP],
                "difficulty": row.difficulty,
            }
            for row in rows
        ]
    }


@router.post("/problem-generation/concepts/{concept_id}/variants")
async def create_generation_run(
    concept_id: int,
    body: GenerateVariantsBody,
    request: Request,
    background: BackgroundTasks,
    db: AsyncSession = Depends(get_db_session),
) -> dict:
    if not problem_generation_enabled():
        raise HTTPException(status_code=403, detail="problem generation is disabled")
    _concept, search_space_id = await _course_concept_or_404(db, concept_id)
    auth = await require_user(request)
    await require_course_teacher(db=db, auth=auth, search_space_id=search_space_id)

    run = ProvisioningRun.generation(
        search_space_id=search_space_id,
        concept_id=concept_id,
        status="pending",
    )
    db.add(run)
    await db.flush()
    run_id = int(run.id)
    await db.commit()
    background.add_task(
        _run_generation_background,
        run_id,
        concept_id,
        search_space_id,
        list(body.seed_problem_ids),
        body.count,
    )
    return {"run_id": run_id, "status": "pending"}


def _result_summary(result) -> dict:
    records = []
    for record in result.records:
        item = asdict(record)
        item["reasons"] = list(item.get("reasons") or [])
        records.append(item)
    return {
        "requested": result.requested,
        "written": list(result.written),
        "dropped": dict(result.dropped),
        "records": records,
    }


async def _run_generation_background(
    run_id: int,
    concept_id: int,
    search_space_id: int,
    seed_problem_ids: list[int],
    count: int,
) -> None:
    ingest_run_id: int | None = None
    try:
        async with get_async_session() as db:
            run = await _get_generation_run(db, run_id)
            if run is None:
                return
            run.status = "running"
            ingest_run = await start_ingest_run(
                db,
                search_space_id=search_space_id,
                document_id=None,
            )
            ingest_run_id = int(ingest_run.id)
            run.ingest_run_id = ingest_run_id
            await db.commit()

            result = await generate_problem_variants(
                db,
                concept_id=concept_id,
                seed_problem_ids=seed_problem_ids,
                count=count,
                metered_chat=MeteredChat(
                    ingest_run=ingest_run,
                    document_id=None,
                    ceiling=generation_token_ceiling(),
                ),
                search_space_id=search_space_id,
            )
            await finalize_ingest_run(
                db,
                ingest_run=ingest_run,
                status="succeeded",
                n_questions_scraped=result.requested,
                n_promoted=0,
                n_rejected=sum(result.dropped.values()),
            )
            run = await _get_generation_run(db, run_id)
            if run is None:
                return
            run.result_summary = _result_summary(result)
            run.status = "succeeded"
            await db.commit()
    except Exception as exc:  # noqa: BLE001 - persist failure, never escape background task
        _LOG.exception("problem_generation_background_failed", extra={"run_id": run_id})
        try:
            async with get_async_session() as db:
                failed_run = (
                    await db.get(IngestRun, ingest_run_id) if ingest_run_id is not None else None
                )
                if failed_run is not None and failed_run.status != "failed":
                    await finalize_ingest_run(db, ingest_run=failed_run, status="failed")
                await record_ingest_error(
                    db,
                    search_space_id=search_space_id,
                    ingest_run=failed_run,
                    stage="problem_generation",
                    exc=exc,
                    context={"generation_run_id": run_id},
                )
                run = await _get_generation_run(db, run_id)
                if run is not None:
                    run.status = "failed"
                    run.result_summary = {**(run.result_summary or {}), "error": str(exc)}
                await db.commit()
        except Exception:  # noqa: BLE001 - a background task must never leak recovery failures
            _LOG.exception(
                "problem_generation_failure_persistence_failed",
                extra={"run_id": run_id, "ingest_run_id": ingest_run_id},
            )


@router.get("/problem-generation/runs")
async def list_generation_runs(
    request: Request,
    search_space_id: int,
    db: AsyncSession = Depends(get_db_session),
) -> dict:
    auth = await require_user(request)
    await require_course_teacher(db=db, auth=auth, search_space_id=search_space_id)
    rows = (
        (
            await db.execute(
                select(ProvisioningRun)
                .where(
                    ProvisioningRun.search_space_id == search_space_id,
                    ProvisioningRun.kind == "generation",
                )
                .order_by(ProvisioningRun.created_at.desc(), ProvisioningRun.id.desc())
            )
        )
        .scalars()
        .all()
    )
    return {
        "runs": [
            {
                "run_id": int(row.id),
                "concept_id": int(row.concept_id),
                "status": row.status,
                "created_at": row.created_at.isoformat(),
                "requested": _json_dict(row.result_summary).get("requested"),
                "written_count": len(_json_dict(row.result_summary).get("written") or []),
                "dropped": _json_dict(row.result_summary).get("dropped"),
            }
            for row in rows
        ]
    }


def _generation_review(row: Problem) -> dict:
    provenance = dict(row.provenance or {})
    authored_review = provenance.get("authored_review")
    review = authored_review if isinstance(authored_review, dict) else {}
    round_trip = provenance.get("round_trip")
    projected = {
        "variation_operator": provenance.get("variation_operator"),
        "aig_seed_id": provenance.get("aig_seed_id"),
        "model": provenance.get("model"),
        "round_trip": (
            {
                "verdict": round_trip.get("verdict"),
                "diagnostic": round_trip.get("diagnostic"),
            }
            if isinstance(round_trip, dict)
            else None
        ),
        "authored_review": {"required": bool(review.get("required"))},
        "ocr_draft": _trim_ocr_draft(review.get("ocr_draft")),
    }
    if "qualitative_rubric" in provenance:
        projected["qualitative_rubric"] = provenance["qualitative_rubric"]
    return projected


def _trim_ocr_draft(draft: object) -> dict | None:
    if not isinstance(draft, dict):
        return None
    return {
        "solution_source": draft.get("solution_source"),
        "reference_solution": draft.get("reference_solution"),
    }


async def _generation_problems(
    db: AsyncSession,
    summary: dict,
    *,
    full_text: bool = False,
) -> list[dict]:
    ids = [int(value) for value in summary.get("written") or []]
    if not ids:
        return []
    rows = {
        int(row.id): row
        for row in (
            await db.execute(select(Problem).where(Problem.id.in_(ids)))
        ).scalars()
    }
    problems = []
    for problem_id in ids:
        row = rows.get(problem_id)
        if row is None:
            continue
        text = str(row.problem_text or "")
        truncated = not full_text and len(text) > _PROBLEM_TEXT_CAP
        problems.append(
            {
                "concept_problem_id": problem_id,
                "problem_text": text[:_PROBLEM_TEXT_CAP] if truncated else text,
                "problem_text_truncated": truncated,
                "difficulty": row.difficulty,
                "tier": row.tier,
                "review": _generation_review(row),
            }
        )
    return problems


@router.get("/problem-generation/runs/{run_id}")
async def get_generation_run(
    run_id: int,
    request: Request,
    full_text: bool = False,
    db: AsyncSession = Depends(get_db_session),
) -> dict:
    auth = await require_user(request)
    run = await _get_generation_run(db, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="generation run not found")
    await require_course_teacher(db=db, auth=auth, search_space_id=int(run.search_space_id))
    ingest_run = (
        await db.get(IngestRun, int(run.ingest_run_id)) if run.ingest_run_id is not None else None
    )
    summary = dict(run.result_summary or {})
    return {
        "run_id": int(run.id),
        "concept_id": int(run.concept_id),
        "status": run.status,
        "ingest_run": (
            {
                "llm_calls": ingest_run.llm_calls,
                "llm_tokens_in": ingest_run.llm_tokens_in,
                "llm_tokens_out": ingest_run.llm_tokens_out,
                "llm_cost_usd": str(ingest_run.llm_cost_usd),
            }
            if ingest_run is not None
            else None
        ),
        "result_summary": summary,
        "problems": await _generation_problems(db, summary, full_text=full_text),
    }


@router.post("/problem-generation/problems/{problem_id}/approve")
async def approve_generated_problem(
    problem_id: int,
    body: ApproveBody,
    request: Request,
    db: AsyncSession = Depends(get_db_session),
) -> dict:
    auth = await require_user(request)
    row = await db.get(Problem, problem_id)
    provenance = dict(row.provenance or {}) if row is not None else {}
    if row is None or provenance.get("source") != "generated":
        raise HTTPException(status_code=404, detail="generated problem not found")
    await require_course_teacher(db=db, auth=auth, search_space_id=int(row.course_id))
    review = provenance.get("authored_review")
    if not isinstance(review, dict) or review.get("required") is not True:
        raise HTTPException(status_code=409, detail="problem is not held for review")
    if body.reference == "generated" and review.get("generated_alt") is None:
        raise HTTPException(status_code=409, detail="no 'generated' reference stored")
    concept_slug = await db.scalar(select(Concept.slug).where(Concept.id == row.concept_id))
    return await approve_held_row(
        db,
        row=row,
        review=review,
        reference=body.reference,
        search_space_id=int(row.course_id),
        resolved_concept=ResolvedConcept(
            concept_id=int(row.concept_id),
            slug=str(concept_slug or ""),
        ),
        document_id=int(provenance.get("document_id") or 0),
        stage="approve_generated_problem",
    )
