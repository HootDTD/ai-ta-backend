"""Teacher-gated HTTP surface for authored problem/solution sets (WU-AAS).

POST indexes both docs hidden from student retrieval, persists the pairing, and
runs provisioning in an in-process background task. GET endpoints poll status
and result summaries; approve promotes a held reference chosen by the teacher.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from decimal import Decimal
from functools import partial
from types import SimpleNamespace
from typing import Any, Literal

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
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from apollo.auth_deps import require_course_teacher, require_user
from apollo.errors import KGUnavailableError
from apollo.persistence.models import (
    ApolloSession,
    AuthoredSet,
    Concept,
    ConceptProblem,
    DedupDecision,
    EntityPrereq,
    IngestPageEvidence,
    IngestRun,
    KGEntity,
    LearnerState,
    MasteryEvent,
    Misconception,
    RejectedProblem,
    Subject,
)
from apollo.provisioning.authored_sets.indexing import index_authored_doc
from apollo.provisioning.authored_sets.observability import (
    finalize_ingest_run,
    persist_page_evidence,
    record_ingest_error,
    start_ingest_run,
)
from apollo.provisioning.authored_sets.orchestrator import (
    MintRejected,
    ProblemResult,
    _authored_concept_dup_hashes,
    _tag_mint_chat_fn,
    run_authored_set_provisioning,
)
from apollo.provisioning.authored_sets.rehoming import (
    claim_rehoming_job,
    complete_rehoming_job,
    enqueue_rehoming,
    fail_rehoming_job,
    run_rehoming,
)
from apollo.provisioning.cost_constants import structure_pairing_mode
from apollo.provisioning.ingest import (
    AuthoredProblem,
    authored_problem_code,
    classify_completeness,
    ingest_authored_problems,
)
from apollo.provisioning.metered_chat import MeteredChat
from apollo.provisioning.orchestrator import provision_authored_problem
from apollo.provisioning.path_enumeration import enumerate_strategy_paths
from apollo.provisioning.problem_hash import problem_dup_hash
from apollo.provisioning.promote import (
    PromoteHeldForReview,
    promote,
    promote_typed_confirmed,
)
from apollo.provisioning.scrape import resolve_or_create_provisional_concept
from apollo.provisioning.solution import ReferenceSolutionDraft, build_approved_pair
from apollo.provisioning.tag_mint import ResolvedConcept, TagMintError, tag_and_mint
from apollo.schemas.problem import Problem
from database.models import AITADocument
from database.session import get_async_session, get_db_session
from indexing.document_embedder import embed_text

_LOG = logging.getLogger(__name__)

_SAME_DOC_GUARD_NOTE = (
    "solution PDF ignored: identical content to the problem PDF "
    "(content_hash match) — treated as no solution provided, "
    "so reference solutions are generated and held for review"
)

router = APIRouter(tags=["apollo-authored-sets"])


class ApproveBody(BaseModel):
    reference: Literal["ocr", "generated"] = "ocr"


class ManualProblemBody(BaseModel):
    problem_text: str = Field(min_length=1)
    solution_text: str | None = None


class ManualAuthoredSetBody(BaseModel):
    search_space_id: int
    problems: list[ManualProblemBody] = Field(min_length=1)
    replace_problem_id: int | None = None


class AssignConceptBody(BaseModel):
    concept_id: int


class ReferenceSolutionEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    content: dict[str, Any]


class ProblemEditBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    problem_text: str | None = Field(default=None, min_length=1)
    reference_solution: list[ReferenceSolutionEdit] | None = None

    @model_validator(mode="after")
    def _at_least_one_edit(self) -> ProblemEditBody:
        if self.problem_text is None and self.reference_solution is None:
            raise ValueError("at least one editable field is required")
        return self


def get_neo4j_client():
    """Late import avoids a module cycle while keeping a test patch seam.

    Returns `Neo4jClient | None` — degraded mode (WU Neo4j-degraded): the
    process-wide client may fail to construct. Authored-set provisioning is
    Neo4j-native (teacher-facing, no meaningful Postgres-only fallback), so
    every call site below routes the result through `_require_neo` to raise
    a structured `KGUnavailableError` (503 `kg_unavailable`) on `None`,
    rather than silently operating on a missing client."""
    from apollo.api import get_neo4j_client as _get_neo4j_client

    return _get_neo4j_client()


def _require_neo(neo, *, stage: str):
    """Raise `KGUnavailableError` when `get_neo4j_client()` degraded to
    `None`. A thin, synchronous, easily-patched guard (mirrors
    `KGStore._require_neo`) — kept local rather than importing
    `apollo.api.require_neo4j_client` because that dependency is `async def`
    and shaped for FastAPI's `Depends` resolution, not a plain call from a
    background task or a route body."""
    if neo is None:
        raise KGUnavailableError(stage=stage, last_error="client unavailable")
    return neo


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
    solution: UploadFile | None = File(None),
    search_space_id: int = Form(...),
    db: AsyncSession = Depends(get_db_session),
) -> dict:
    """A solution PDF is optional: the intended teacher flow (product decision)
    is upload a problem doc WITHOUT solutions and let the pipeline auto-generate
    reference solutions, which stay held-for-review for teacher approval. A
    solution PDF, when provided, still enables the label-matched extract path."""
    auth = await require_user(request)
    await require_course_teacher(db=db, auth=auth, search_space_id=search_space_id)

    problem_bytes = await problem.read()
    solution_bytes = await solution.read() if solution is not None else None
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
        solution_title=solution.filename if solution is not None else None,
    )
    return {"set_id": set_id, "set_index": set_index, "status": "pending"}


@router.post("/authored-sets/manual")
async def create_manual_authored_set(
    body: ManualAuthoredSetBody,
    request: Request,
    background: BackgroundTasks,
    db: AsyncSession = Depends(get_db_session),
) -> dict:
    auth = await require_user(request)
    await require_course_teacher(
        db=db,
        auth=auth,
        search_space_id=body.search_space_id,
    )
    if body.replace_problem_id is not None:
        previous = await db.get(ConceptProblem, body.replace_problem_id)
        confirmation = (
            (previous.provenance or {}).get("typed_confirmation") if previous is not None else None
        )
        if (
            previous is None
            or int(previous.search_space_id or 0) != body.search_space_id
            or not isinstance(confirmation, dict)
            or confirmation.get("status") != "awaiting_teacher_confirmation"
        ):
            raise HTTPException(status_code=409, detail="replacement draft is not pending")
        await _replace_problem_result(
            db,
            problem_id=int(previous.id),
            outcome="discarded",
            reason="edit_resubmitted",
        )
        await db.delete(previous)
    authored = [_manual_authored_problem(problem) for problem in body.problems]
    set_index = await _next_set_index(db, body.search_space_id)
    row = AuthoredSet(
        search_space_id=body.search_space_id,
        set_index=set_index,
        status="pending",
        result_summary={"source": "manual", "problems": [], "counts": {}},
    )
    db.add(row)
    await db.flush()
    set_id = int(row.id)
    await db.commit()

    background.add_task(
        _run_manual_set_background,
        set_id=set_id,
        search_space_id=body.search_space_id,
        authored=[problem.model_dump() for problem in authored],
    )
    return {"set_id": set_id, "set_index": set_index, "status": "pending"}


def _manual_authored_problem(problem: ManualProblemBody) -> AuthoredProblem:
    statement = problem.problem_text.strip()
    if not statement:
        raise HTTPException(status_code=422, detail="problem_text must not be blank")
    solution = problem.solution_text.strip() if problem.solution_text is not None else None
    if solution == "":
        solution = None
    return AuthoredProblem(
        problem_code=authored_problem_code(statement),
        concept_slug="provisional.inventory",
        statement=statement,
        difficulty="standard",
        solution=solution,
        worked_procedure=None,
        given_values={},
        target_unknown="",
        completeness=classify_completeness(solution, None),
    )


async def _run_manual_set_background(
    *,
    set_id: int,
    search_space_id: int,
    authored: list[dict[str, Any]],
) -> None:
    """Provision typed manual problems with the existing authored pipeline."""
    ingest_run_id: int | None = None
    try:
        async with get_async_session() as db:
            row = await db.get(AuthoredSet, set_id)
            if row is None:
                return
            row.status = "provisioning"
            row.updated_at = datetime.now(UTC)
            ingest_run = await start_ingest_run(
                db,
                search_space_id=search_space_id,
                document_id=None,
            )
            ingest_run_id = int(ingest_run.id)
            row.result_summary = {
                "source": "manual",
                "ingest_run_id": ingest_run_id,
                "problems": [],
                "counts": {},
            }
            concept_id = await resolve_or_create_provisional_concept(
                db, search_space_id=search_space_id
            )
            concept = await db.get(Concept, concept_id)
            if concept is None:
                raise RuntimeError("manual authored set has no provisional concept")
            subject_id = int(concept.subject_id)
            await db.commit()

            problems = [AuthoredProblem.model_validate(problem) for problem in authored]
            await ingest_authored_problems(
                db,
                [problem.model_dump() for problem in problems],
                subject_id=subject_id,
                concept_id=concept_id,
                search_space_id=search_space_id,
            )

            metered_chat = _make_metered_chat(document_id=None, ingest_run=ingest_run)
            neo = _require_neo(get_neo4j_client(), stage="run_manual_set_provisioning")
            results: list[ProblemResult] = []
            for problem in problems:
                concept_problem_id = (
                    await db.execute(
                        select(ConceptProblem.id)
                        .where(ConceptProblem.concept_id == concept_id)
                        .where(ConceptProblem.problem_code == problem.problem_code)
                        .limit(1)
                    )
                ).scalar_one_or_none()
                if concept_problem_id is None:
                    raise RuntimeError(
                        f"manual authored problem {problem.problem_code!r} has no Tier-1 row"
                    )
                result = await provision_authored_problem(
                    db,
                    neo,
                    problem,
                    search_space_id=search_space_id,
                    ingest_concept_id=concept_id,
                    construct_chat_fn=metered_chat.main,
                    judge_fn=metered_chat.cheap,
                    tag_chat_fn=_tag_mint_chat_fn(metered_chat),
                    embed_fn=embed_text,
                    run=ingest_run,
                )
                results.append(
                    ProblemResult(
                        outcome=result.outcome,
                        solution_source="authored",
                        failed_gate=result.failed_gate,
                        diagnostic=result.diagnostic,
                        reason=result.stage,
                        concept_problem_id=int(concept_problem_id),
                    )
                )

            counts = {
                "promoted": 0,
                "rejected": 0,
                "held_for_review": 0,
                "awaiting_teacher_confirmation": 0,
            }
            for result in results:
                counts[result.outcome] = counts.get(result.outcome, 0) + 1
            await finalize_ingest_run(
                db,
                ingest_run=ingest_run,
                status="succeeded",
                n_questions_scraped=len(problems),
                n_promoted=counts["promoted"],
                n_rejected=counts["rejected"],
            )
            row = await db.get(AuthoredSet, set_id)
            if row is None:
                return
            row.result_summary = {
                "source": "manual",
                "ingest_run_id": ingest_run_id,
                "problems": [result.model_dump() for result in results],
                "counts": counts,
            }
            # Migration 032's existing status vocabulary already has the right
            # non-terminal value. Per-problem confirmation state lives in the
            # result ledger/provenance; no temporary set-level state is needed.
            row.status = "provisioning" if counts["awaiting_teacher_confirmation"] else "done"
            row.updated_at = datetime.now(UTC)
            await db.commit()
    except Exception as exc:  # noqa: BLE001 - persist failed status, never escape task
        _LOG.exception("manual_authored_set_background_failed", extra={"set_id": set_id})
        async with get_async_session() as db:
            failed_run = (
                await db.get(IngestRun, ingest_run_id) if ingest_run_id is not None else None
            )
            if failed_run is not None:
                if failed_run.status != "failed":
                    await finalize_ingest_run(db, ingest_run=failed_run, status="failed")
                await record_ingest_error(
                    db,
                    search_space_id=search_space_id,
                    ingest_run=failed_run,
                    stage="manual_authored_set_ingest",
                    exc=exc,
                    context={"set_id": set_id},
                )
                await db.commit()
            await _set_status(db, set_id, "failed", diagnostic=str(exc))


async def _run_set_background(
    *,
    set_id: int,
    search_space_id: int,
    set_index: int,
    problem_bytes: bytes,
    problem_title: str,
    solution_bytes: bytes | None,
    solution_title: str | None,
) -> None:
    """Own a fresh session; request-scoped sessions are closed before this runs.

    ``solution_bytes`` is optional (B1): the teacher may upload a problem PDF
    alone, in which case solution-role indexing is skipped. With structure
    pairing ON, provisioning first probes that document for combined answer
    units; otherwise candidates fall through to generated-and-held references."""
    ingest_run_id: int | None = None
    # OCR-observability: capture each doc's transient per-page OCR pass so the
    # ingest run + page-level evidence tables (empty before WU-AAS observability)
    # are populated for the S2 audit — on the SUCCESS path AND, crucially, when an
    # indexing stage raises. The page lists live outside the `async with` so the
    # except handler can still persist whatever was captured after the try session
    # rolled back.
    problem_pages: list = []
    solution_pages: list = []
    problem_document_id: int | None = None
    # The raw id solution indexing produced (even if the same-doc guard below
    # discards it for pairing) — kept so page evidence still reflects what was
    # actually OCR'd. ``solution_document_id`` (below) is the PAIRING decision.
    indexed_solution_document_id: int | None = None
    solution_document_id: int | None = None
    combined_document = False
    same_doc_guard_note: str | None = None
    evidence_persisted = False
    try:
        async with get_async_session() as db:
            await _set_status(db, set_id, "indexing")
            # Open the ingest run BEFORE indexing and COMMIT it, so an OCR/indexing
            # failure (bad PDF, "no chunks produced") still leaves a durable run row
            # + error + captured page evidence instead of both observability tables
            # staying empty (the S2 "insufficient info" failure class). document_id
            # is NULL until problem indexing mints it (migration 036).
            ingest_run = await start_ingest_run(
                db, search_space_id=search_space_id, document_id=None
            )
            ingest_run_id = int(ingest_run.id)
            await db.commit()

            problem_document_id = await index_authored_doc(
                db,
                search_space_id=search_space_id,
                file_bytes=problem_bytes,
                title=problem_title,
                set_index=set_index,
                role="problem",
                page_sink=problem_pages,
            )
            # The run's document handle is the problem doc (parity with the queue
            # path + what the GET surface looks it up by). Stamp it now that it
            # exists; committed with the provisioning-status transition below.
            ingest_run.document_id = problem_document_id
            problem_content_hash = await _doc_content_hash(db, problem_document_id)
            ingest_run.content_hash = problem_content_hash

            if solution_bytes is not None:
                indexed_solution_document_id = await index_authored_doc(
                    db,
                    search_space_id=search_space_id,
                    file_bytes=solution_bytes,
                    title=solution_title or f"Solution Set {set_index}",
                    set_index=set_index,
                    role="solution",
                    page_sink=solution_pages,
                )
                # B2: a solution upload identical to the problem doc (teacher
                # uploaded the SAME file as both roles) grounds questions against
                # their own prose instead of a real worked solution. Reuse the
                # content_hash the ingest already computed to detect it and treat
                # the pairing as absent (never NULL out of caution: only an EXACT
                # content match).
                solution_content_hash = await _doc_content_hash(db, indexed_solution_document_id)
                if (
                    problem_content_hash is not None
                    and solution_content_hash == problem_content_hash
                ):
                    if structure_pairing_mode() == "on":
                        combined_document = True
                        solution_document_id = problem_document_id
                        _LOG.info(
                            "authored_set_same_doc_combined_mode",
                            extra={
                                "event": "authored_set_same_doc_combined_mode",
                                "set_id": set_id,
                                "search_space_id": search_space_id,
                                "problem_document_id": problem_document_id,
                                "indexed_solution_document_id": indexed_solution_document_id,
                            },
                        )
                    else:
                        _LOG.warning(
                            "authored_set_same_doc_solution_guard",
                            extra={
                                "event": "authored_set_same_doc_solution_guard",
                                "set_id": set_id,
                                "search_space_id": search_space_id,
                                "problem_document_id": problem_document_id,
                                "solution_document_id": indexed_solution_document_id,
                                "content_hash": problem_content_hash,
                            },
                        )
                        same_doc_guard_note = _SAME_DOC_GUARD_NOTE
                else:
                    solution_document_id = indexed_solution_document_id

            n_pages = await persist_page_evidence(
                db,
                ingest_run=ingest_run,
                search_space_id=search_space_id,
                document_id=problem_document_id,
                role="problem",
                pages=problem_pages,
            )
            if solution_bytes is not None:
                n_pages += await persist_page_evidence(
                    db,
                    ingest_run=ingest_run,
                    search_space_id=search_space_id,
                    document_id=indexed_solution_document_id,
                    role="solution",
                    pages=solution_pages,
                )
            # Set n_pages here so it is committed with the evidence below and stays
            # correct even if provisioning later fails (the success finalize does
            # NOT re-write it — avoids the redundant double-write).
            ingest_run.n_pages = n_pages

            row = await db.get(AuthoredSet, set_id)
            if row is None:
                return
            row.problem_document_id = problem_document_id
            row.solution_document_id = solution_document_id
            row.status = "provisioning"
            row.updated_at = datetime.now(UTC)
            await db.commit()
            # Evidence + run.document_id + n_pages are now durable: the except
            # handler must NOT re-persist page evidence (it would duplicate rows).
            evidence_persisted = True

            report = await run_authored_set_provisioning(
                db,
                _require_neo(get_neo4j_client(), stage="run_authored_set_provisioning"),
                search_space_id=search_space_id,
                problem_document_id=problem_document_id,
                solution_document_id=solution_document_id,
                combined_document=combined_document,
                metered_chat=_make_metered_chat(
                    document_id=problem_document_id, ingest_run=ingest_run
                ),
            )
            counts = report.counts or {}
            await finalize_ingest_run(
                db,
                ingest_run=ingest_run,
                status="succeeded",
                n_questions_scraped=len(report.problems),
                n_promoted=counts.get("promoted", 0),
                n_rejected=counts.get("rejected", 0),
            )
            row = await db.get(AuthoredSet, set_id)
            if row is None:
                return
            if report.combined_document:
                # Covers both the explicit same-hash handoff and a problem-only
                # upload whose pre-scrape probe discovered answer units.
                row.solution_document_id = problem_document_id
            elif combined_document:
                # The same-hash probe found no usable combined structure (or
                # failed/breached budget): restore the original guard outcome.
                row.solution_document_id = None
                same_doc_guard_note = _SAME_DOC_GUARD_NOTE
                _LOG.warning(
                    "authored_set_same_doc_solution_guard",
                    extra={
                        "event": "authored_set_same_doc_solution_guard",
                        "set_id": set_id,
                        "search_space_id": search_space_id,
                        "problem_document_id": problem_document_id,
                        "solution_document_id": indexed_solution_document_id,
                        "reason": "combined_segmentation_unavailable",
                    },
                )
            result_summary = report.model_dump()
            if same_doc_guard_note is not None:
                result_summary["same_doc_solution_guard"] = same_doc_guard_note
            row.result_summary = result_summary
            row.status = "done"
            row.updated_at = datetime.now(UTC)
            await db.commit()
    except Exception as exc:  # noqa: BLE001 - persist failed status, never escape task
        _LOG.exception("authored_set_background_failed", extra={"set_id": set_id})
        async with get_async_session() as db:
            # A fresh session: the failed run's own session may be poisoned. The run
            # row was committed before indexing, so it is durable here. Mark it
            # failed, persist any page evidence captured before the raise (unless it
            # was already committed on the success path), and record the stage error
            # so the observability tables reflect the failure instead of staying
            # empty.
            if ingest_run_id is not None:
                failed_run = await db.get(IngestRun, ingest_run_id)
                if failed_run is not None:
                    failed_pages: int | None = None
                    if not evidence_persisted:
                        failed_pages = await persist_page_evidence(
                            db,
                            ingest_run=failed_run,
                            search_space_id=search_space_id,
                            document_id=problem_document_id,
                            role="problem",
                            pages=problem_pages,
                        )
                        if solution_bytes is not None:
                            failed_pages += await persist_page_evidence(
                                db,
                                ingest_run=failed_run,
                                search_space_id=search_space_id,
                                document_id=indexed_solution_document_id,
                                role="solution",
                                pages=solution_pages,
                            )
                    if failed_run.status != "failed":
                        await finalize_ingest_run(
                            db, ingest_run=failed_run, status="failed", n_pages=failed_pages
                        )
                    await record_ingest_error(
                        db,
                        search_space_id=search_space_id,
                        ingest_run=failed_run,
                        stage="authored_set_ingest",
                        exc=exc,
                        context={"set_id": set_id},
                    )
                else:
                    # The run row vanished (should not happen after the early
                    # commit) — still record the error, unlinked, so the failure is
                    # not silently lost.
                    await record_ingest_error(
                        db,
                        search_space_id=search_space_id,
                        ingest_run=None,
                        stage="authored_set_ingest",
                        exc=exc,
                        context={"set_id": set_id},
                    )
                # Explicit commit: the run/error/evidence writes above must persist
                # independently of _set_status, which early-returns UNCOMMITTED if
                # the AuthoredSet row has since vanished.
                await db.commit()
            await _set_status(db, set_id, "failed", diagnostic=str(exc))


async def _doc_content_hash(db: AsyncSession, document_id: int) -> str | None:
    """The indexed document's content hash — recorded on the ingest run so an
    unchanged re-upload is identifiable (parity with the queue path's run rows)."""
    doc = await db.get(AITADocument, document_id)
    return getattr(doc, "content_hash", None) if doc is not None else None


def _make_metered_chat(
    *, document_id: int | None, ingest_run: IngestRun | None = None
) -> MeteredChat:
    """Build the metered LLM client for a run.

    When ``ingest_run`` is a real ``apollo_ingest_runs`` row (the ingestion path),
    metered LLM usage accrues on it in place, so the run's llm_calls/token/cost
    aggregates persist. The approve endpoint has no run row, so it falls back to a
    throwaway namespace whose metering is discarded.
    """
    run = (
        ingest_run
        if ingest_run is not None
        else SimpleNamespace(
            id=None,
            llm_calls=0,
            llm_tokens_in=0,
            llm_tokens_out=0,
            # Decimal, not float: ``cost_usd_for`` returns Decimal and ``record_usage``
            # does ``llm_cost_usd += <Decimal>`` — a float seed raises
            # "unsupported operand type(s) for +=: 'float' and 'decimal.Decimal'"
            # on the first metered LLM call, failing the whole authored-set run.
            llm_cost_usd=Decimal("0"),
        )
    )
    return MeteredChat(ingest_run=run, document_id=document_id)


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
    await require_course_teacher(db=db, auth=auth, search_space_id=search_space_id)
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


# Per-page ``ocr_text`` cap for the GET list surface. A run can carry dozens of
# pages, each with a full page of recognized text/LaTeX; returning every page's
# body unbounded bloats the teacher/S2 detail payload. The list view truncates to
# this many chars (with ``ocr_text_truncated`` flagged) and callers that need the
# full page body pass ``?full_ocr=true`` for a deliberate deep fetch.
_LIST_OCR_TEXT_CAP = 2000


@router.get("/authored-sets/{set_id}")
async def get_authored_set(
    set_id: int,
    request: Request,
    full_ocr: bool = False,
    full_text: bool = False,
    db: AsyncSession = Depends(get_db_session),
) -> dict:
    auth = await require_user(request)
    row = await db.get(AuthoredSet, set_id)
    if row is None:
        raise HTTPException(status_code=404, detail="authored set not found")
    await require_course_teacher(db=db, auth=auth, search_space_id=int(row.search_space_id))
    summary: dict = dict(row.result_summary or {})
    summary_ingest_run_id = summary.get("ingest_run_id")
    ingest_run, pages = await _load_ingest_evidence(
        db,
        row.problem_document_id,
        full_ocr=full_ocr,
        ingest_run_id=(
            int(summary_ingest_run_id) if isinstance(summary_ingest_run_id, int) else None
        ),
    )
    if isinstance(summary.get("problems"), list):
        summary["problems"] = await _enrich_problem_reviews(
            db,
            summary["problems"],
            full_text=full_text,
            ingest_run_id=ingest_run["id"] if ingest_run is not None else None,
        )
    return {
        "set_id": int(row.id),
        "set_index": row.set_index,
        "status": row.status,
        "problem_document_id": row.problem_document_id,
        "solution_document_id": row.solution_document_id,
        "result_summary": summary,
        # WU-AAS observability: the ingest run + per-page OCR evidence so the S2
        # audit consumes REAL inputs (page_ref, ocr text, confidence,
        # verify_path_fired) instead of thin/absent ones.
        "ingest_run": ingest_run,
        "pages": pages,
    }


# The stored draft dicts (``ReferenceSolutionDraft.model_dump()``) carry grounding
# spans (raw chunk text) and internal provenance; the review surface exposes ONLY
# what the teacher must read to decide — the draft's nature and its steps.
_REVIEW_DRAFT_KEYS = ("solution_source", "reference_solution")


def _trim_review_draft(draft: object) -> dict | None:
    if not isinstance(draft, dict):
        return None
    return {key: draft.get(key) for key in _REVIEW_DRAFT_KEYS}


def _review_dict(provenance: dict | None) -> dict | None:
    """Whitelisted ``provenance["authored_review"]`` projection for the GET
    surface: the CURRENT hold state (``required`` flips false on approval, so the
    UI can recompute counts instead of trusting the frozen ``result_summary``)
    plus, only while the hold is active, the trimmed stored draft(s) the teacher
    is choosing between. Nothing else in provenance leaks into the response."""
    review = (provenance or {}).get("authored_review")
    if not isinstance(review, dict):
        return None
    out: dict = {
        "required": bool(review.get("required")),
        "reason": review.get("reason"),
        "approved_reference": review.get("approved_reference"),
        "augmented": review.get("augmented"),
    }
    if out["required"]:
        out["ocr_draft"] = _trim_review_draft(review.get("ocr_draft"))
        out["generated_alt"] = _trim_review_draft(review.get("generated_alt"))
    return out


def _bounded_problem_text(text: str, *, full_text: bool) -> tuple[str, bool]:
    truncated = not full_text and len(text) > _LIST_OCR_TEXT_CAP
    return (text[:_LIST_OCR_TEXT_CAP] if truncated else text), truncated


def _stored_solution_text(payload: dict) -> str | None:
    direct = payload.get("solution_text") or payload.get("solution")
    if isinstance(direct, str) and direct:
        return direct
    authored = payload.get("authored")
    if isinstance(authored, dict):
        solution = authored.get("solution")
        if isinstance(solution, str) and solution:
            return solution
    return None


def _stored_reference_solution(payload: dict) -> object | None:
    reference = payload.get("reference_solution")
    if reference is not None:
        return reference
    for key in ("draft", "ocr_draft", "generated_alt"):
        draft = payload.get(key)
        if isinstance(draft, dict) and draft.get("reference_solution") is not None:
            return draft["reference_solution"]
    return None


async def _enrich_problem_reviews(
    db: AsyncSession,
    problems: list,
    *,
    full_text: bool = False,
    ingest_run_id: int | None = None,
) -> list:
    """Add live concept payloads and ordered rejection-audit payloads.

    Concept-backed entries receive the current question, reference solution, and
    whitelisted review state. Rejected entries are paired in result order with this
    run's rejection audit rows, projecting their real columns plus stored
    question/solution fields. Live concept payload values take precedence when both
    sources exist; unmatched old-shape entries pass through unchanged.
    """
    ids = [
        int(p["concept_problem_id"])
        for p in problems
        if isinstance(p, dict) and p.get("concept_problem_id") is not None
    ]
    rows: dict[int, ConceptProblem] = {}
    if ids:
        rows = {
            int(r.id): r
            for r in (
                await db.execute(select(ConceptProblem).where(ConceptProblem.id.in_(ids)))
            ).scalars()
        }
    rejected_rows: list[RejectedProblem] = []
    if ingest_run_id is not None:
        rejected_rows = list(
            (
                await db.execute(
                    select(RejectedProblem)
                    .where(RejectedProblem.ingest_run_id == ingest_run_id)
                    .order_by(RejectedProblem.id.asc())
                )
            )
            .scalars()
            .all()
        )
    rejected_iter = iter(rejected_rows)
    enriched: list = []
    for problem in problems:
        rejected_row = (
            next(rejected_iter, None)
            if isinstance(problem, dict) and problem.get("outcome") == "rejected"
            else None
        )
        if not isinstance(problem, dict):
            enriched.append(problem)
            continue

        entry = dict(problem)
        if rejected_row is not None:
            rejected_payload = dict(rejected_row.payload or {})
            entry["rejected_problem_id"] = int(rejected_row.id)
            entry["rejected_stage"] = rejected_row.rejected_stage
            if entry.get("failed_gate") is None:
                entry["failed_gate"] = rejected_row.failed_gate
            if not entry.get("diagnostic"):
                entry["diagnostic"] = rejected_row.diagnostic
            raw_text = rejected_payload.get("problem_text") or rejected_payload.get("statement")
            if isinstance(raw_text, str):
                displayed_text, truncated = _bounded_problem_text(raw_text, full_text=full_text)
                entry["problem_text"] = displayed_text
                entry["problem_text_truncated"] = truncated
            reference_solution = _stored_reference_solution(rejected_payload)
            if reference_solution is not None:
                entry["reference_solution"] = reference_solution
            solution_text = _stored_solution_text(rejected_payload)
            if solution_text is not None:
                entry["solution_text"] = solution_text

        cp_id = problem.get("concept_problem_id")
        row = rows.get(int(cp_id)) if cp_id is not None else None
        if row is not None:
            payload = dict(row.payload or {})
            provenance = dict(row.provenance or {})
            text = str(payload.get("problem_text") or "")
            displayed_text, truncated = _bounded_problem_text(text, full_text=full_text)
            entry = {
                **entry,
                "problem_text": displayed_text,
                "problem_text_truncated": truncated,
                "review": _review_dict(provenance),
                "confirmation": provenance.get("typed_confirmation"),
                "rehoming": provenance.get("typed_rehoming"),
            }
            if "reference_solution" in payload:
                entry["reference_solution"] = payload["reference_solution"]
            solution_text = _stored_solution_text(payload)
            if solution_text is not None:
                entry["solution_text"] = solution_text
            enriched.append(entry)
            continue
        enriched.append(entry)
    return enriched


async def _load_ingest_evidence(
    db: AsyncSession,
    problem_document_id: int | None,
    *,
    full_ocr: bool = False,
    ingest_run_id: int | None = None,
) -> tuple[dict | None, list[dict]]:
    """Return an authored-set ingest run plus its per-page OCR evidence.

    Manual sets supply ``ingest_run_id`` because they have no source document;
    PDF sets resolve the latest run for ``problem_document_id``. ``(None, [])``
    means the set never produced a run (still indexing, or pre-observability).

    Each page's ``ocr_text`` is truncated to ``_LIST_OCR_TEXT_CAP`` chars (with
    ``ocr_text_truncated`` flagged) unless ``full_ocr`` is set — the list surface
    stays bounded while a deliberate ``?full_ocr=true`` fetch gets the full body."""
    if ingest_run_id is not None:
        run = await db.get(IngestRun, ingest_run_id)
    elif problem_document_id is not None:
        run = (
            await db.execute(
                select(IngestRun)
                .where(IngestRun.document_id == int(problem_document_id))
                .order_by(IngestRun.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
    else:
        run = None
    if run is None:
        return None, []

    run_dict = {
        "id": int(run.id),
        "status": run.status,
        "n_pages": run.n_pages,
        "n_questions_scraped": run.n_questions_scraped,
        "n_promoted": run.n_promoted,
        "n_rejected": run.n_rejected,
        "llm_calls": run.llm_calls,
        "llm_tokens_in": run.llm_tokens_in,
        "llm_tokens_out": run.llm_tokens_out,
        "content_hash": run.content_hash,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
    }

    evidence = (
        (
            await db.execute(
                select(IngestPageEvidence)
                .where(IngestPageEvidence.ingest_run_id == int(run.id))
                .order_by(IngestPageEvidence.id.asc())
            )
        )
        .scalars()
        .all()
    )
    pages = [_page_dict(ev, full_ocr=full_ocr) for ev in evidence]
    return run_dict, pages


def _page_dict(ev: IngestPageEvidence, *, full_ocr: bool) -> dict:
    """Shape one page-evidence row for the GET surface, capping ``ocr_text`` unless
    ``full_ocr`` (see ``_LIST_OCR_TEXT_CAP``)."""
    text = ev.ocr_text or ""
    truncated = not full_ocr and len(text) > _LIST_OCR_TEXT_CAP
    return {
        "role": ev.role,
        # document_id is NULL when indexing failed before minting a doc (a page
        # captured for a chunkless PDF on the failure path).
        "document_id": int(ev.document_id) if ev.document_id is not None else None,
        "page_number": ev.page_number,
        # page_ref is the S2 judge's stable per-page handle (role + page).
        "page_ref": f"{ev.role}:p{ev.page_number}",
        "ocr_text": text[:_LIST_OCR_TEXT_CAP] if truncated else text,
        "ocr_text_truncated": truncated,
        "ocr_text_chars": len(text),
        "ocr_confidence": ev.ocr_confidence,
        "extraction_mode": ev.extraction_mode,
        "verify_path_fired": ev.verify_path_fired,
    }


def _problem_for_dup_hash(row: ConceptProblem, payload: dict) -> Problem:
    """Build the exact gate-8 hash input, including for pre-promotion Tier-1 rows."""
    try:
        return Problem.model_validate(payload)
    except ValidationError:
        # Gate 8 hashes only problem_text/given_values/target_unknown. Held Tier-1
        # rows legitimately have no reference_solution yet, so supply a neutral
        # schema-valid step solely to call the same frozen hash function promote()
        # uses. It cannot affect the resulting hash.
        return Problem.model_validate(
            {
                "id": str(payload.get("id") or row.problem_code),
                "concept_id": str(payload.get("concept_id") or "provisional.inventory"),
                "difficulty": payload.get("difficulty") or row.difficulty,
                "problem_text": payload.get("problem_text"),
                "given_values": payload.get("given_values") or {},
                "target_unknown": payload.get("target_unknown") or "",
                "reference_solution": [
                    {
                        "step": 1,
                        "entry_type": "definition",
                        "id": "__dedup_hash_placeholder__",
                        "content": {"term": "placeholder", "definition": "placeholder"},
                        "depends_on": [],
                    }
                ],
            }
        )


async def _edit_collision_exists(
    db: AsyncSession,
    *,
    row: ConceptProblem,
    updated_payload: dict,
) -> bool:
    updated_hash = problem_dup_hash(_problem_for_dup_hash(row, updated_payload))
    other_payloads = (
        (
            await db.execute(
                select(ConceptProblem.payload)
                .where(ConceptProblem.concept_id == int(row.concept_id))
                .where(ConceptProblem.tier == 2)
                .where(ConceptProblem.id != int(row.id))
            )
        )
        .scalars()
        .all()
    )
    for payload in other_payloads:
        try:
            if problem_dup_hash(Problem.model_validate(payload)) == updated_hash:
                return True
        except (ValidationError, ValueError, TypeError):
            continue
    return False


def _edited_reference_solution(
    existing: object,
    edits: list[ReferenceSolutionEdit],
) -> list[dict]:
    if not isinstance(existing, list) or not all(isinstance(step, dict) for step in existing):
        raise HTTPException(status_code=422, detail="problem has no editable reference solution")

    existing_by_id = {str(step.get("id")): step for step in existing}
    edit_ids = [edit.id for edit in edits]
    edit_id_set = set(edit_ids)
    if len(edit_id_set) != len(edit_ids):
        raise HTTPException(status_code=422, detail="reference_solution contains duplicate ids")
    existing_ids = set(existing_by_id)
    if edit_id_set != existing_ids:
        unknown = sorted(edit_id_set - existing_ids)
        missing = sorted(existing_ids - edit_id_set)
        parts = []
        if unknown:
            parts.append(f"unknown ids: {', '.join(unknown)}")
        if missing:
            parts.append(f"missing ids: {', '.join(missing)}")
        raise HTTPException(status_code=422, detail="; ".join(parts))

    content_by_id = {edit.id: dict(edit.content) for edit in edits}
    updated: list[dict] = []
    for step in existing:
        step_id = str(step["id"])
        old_content = step.get("content")
        old_order = old_content.get("order") if isinstance(old_content, dict) else None
        new_order = content_by_id[step_id].get("order")
        if old_order != new_order and (old_order is not None or new_order is not None):
            raise HTTPException(
                status_code=422,
                detail=f"reference_solution step {step_id!r} order is immutable",
            )
        updated.append({**step, "content": content_by_id[step_id]})
    return updated


@router.patch("/authored-sets/problems/{concept_problem_id}")
async def edit_authored_problem(
    concept_problem_id: int,
    body: ProblemEditBody,
    request: Request,
    db: AsyncSession = Depends(get_db_session),
) -> dict:
    auth = await require_user(request)
    row = await db.get(ConceptProblem, concept_problem_id)
    if row is None or row.search_space_id is None:
        raise HTTPException(status_code=404, detail="problem not found")
    await require_course_teacher(
        db=db,
        auth=auth,
        search_space_id=int(row.search_space_id),
    )
    confirmation = (row.provenance or {}).get("typed_confirmation")
    if isinstance(confirmation, dict) and confirmation.get("status") == (
        "awaiting_teacher_confirmation"
    ):
        raise HTTPException(
            status_code=409,
            detail="typed drafts are edited by fresh manual resubmission",
        )

    payload = dict(row.payload or {})
    if body.problem_text is not None:
        problem_text = body.problem_text.strip()
        if not problem_text:
            raise HTTPException(status_code=422, detail="problem_text must not be blank")
        payload["problem_text"] = problem_text
    if body.reference_solution is not None:
        payload["reference_solution"] = _edited_reference_solution(
            payload.get("reference_solution"), body.reference_solution
        )
        try:
            Problem.model_validate(payload)
        except ValidationError as exc:
            raise HTTPException(
                status_code=422,
                detail=f"invalid reference_solution content: {exc}",
            ) from exc

    if await _edit_collision_exists(db, row=row, updated_payload=payload):
        raise HTTPException(
            status_code=409,
            detail="another problem with the same content already exists",
        )

    row.payload = payload  # type: ignore[assignment]
    await db.commit()
    return (
        await _enrich_problem_reviews(
            db,
            [{"concept_problem_id": int(row.id)}],
            full_text=True,
        )
    )[0]


async def _protected_concepts(db: AsyncSession, concept_ids: list[int]) -> set[int]:
    """Of the candidate concepts, return those that must NOT be torn down because
    they carry a Postgres footprint beyond the deleted set. STRICT / conservative:
    ANY single signal spares the whole concept — under-tearing-down is the safe
    direction (it only leaves KG behind; it never destroys data or crashes). Signals:

      * ``apollo_sessions.concept_id`` — a student opened this concept. This is ALSO
        the only ``ON DELETE RESTRICT`` FK into ``apollo_concepts`` (migration 018),
        so sparing session-bound concepts is what keeps ``DELETE apollo_concepts``
        from hard-failing the whole delete.
      * ``apollo_learner_state`` / ``apollo_mastery_events`` keyed on the concept's
        entities — the durable learner belief snapshot + the append-only grading /
        model-refit corpus. Both CASCADE from ``apollo_kg_entities``, so tearing the
        concept down would silently destroy them.
      * ``apollo_misconceptions.concept_id`` — a seed-authored misconception bank
        (CASCADE → silent loss); its presence marks a shared/seed concept.
      * an INBOUND cross-concept prereq edge from a SURVIVING concept — an entity of
        a concept NOT in this teardown batch depends on one of this concept's
        entities, so this concept is a prerequisite the surviving curriculum graph
        still needs; deleting it would corrupt that concept's prereq chain. (A
        dependency from a FELLOW candidate does NOT spare it — both go away
        together, which is what lets a whole corrupted set's concepts be cleared.)
    """
    protected: set[int] = set()

    async def _collect(stmt) -> None:
        protected.update(int(c) for c in (await db.execute(stmt)).scalars().all() if c is not None)

    await _collect(
        select(ApolloSession.concept_id).where(ApolloSession.concept_id.in_(concept_ids)).distinct()
    )
    await _collect(
        select(Misconception.concept_id).where(Misconception.concept_id.in_(concept_ids)).distinct()
    )
    await _collect(
        select(KGEntity.concept_id)
        .join(LearnerState, LearnerState.entity_id == KGEntity.id)
        .where(KGEntity.concept_id.in_(concept_ids))
        .distinct()
    )
    await _collect(
        select(KGEntity.concept_id)
        .join(MasteryEvent, MasteryEvent.entity_id == KGEntity.id)
        .where(KGEntity.concept_id.in_(concept_ids))
        .distinct()
    )
    # Inbound cross-concept prereq: FROM a SURVIVING concept's entity TO this
    # concept's entity (``from`` depends on ``to``). "Surviving" excludes the
    # concepts still slated for teardown — i.e. candidates NOT already spared by a
    # signal above (``still_candidate``). Excluding the already-``protected`` set
    # matters: a concept spared by e.g. a session is a survivor whose OWN
    # prerequisites must also be kept, so its dependency must spare its target. A
    # dependency from a fellow STILL-candidate does not spare (both clear together,
    # so a corrupted set's mutually-linked concepts still go). RESIDUAL (accepted as
    # safe KG drift, never data loss): this is a single pass, not a fixpoint, so a
    # concept protected ONLY transitively through this same prereq relation is not
    # re-fed as a survivor — a deep prereq chain among orphans may shed one edge.
    still_candidate = [cid for cid in concept_ids if cid not in protected]
    prereq_target = aliased(KGEntity)
    prereq_source = aliased(KGEntity)
    await _collect(
        select(prereq_target.concept_id)
        .join(EntityPrereq, EntityPrereq.to_entity_id == prereq_target.id)
        .join(prereq_source, prereq_source.id == EntityPrereq.from_entity_id)
        .where(prereq_target.concept_id.in_(still_candidate))
        .where(prereq_source.concept_id.notin_(still_candidate))
        .distinct()
    )
    return protected


async def _concepts_with_canon_history(neo, concept_ids: list[int]) -> set[int]:
    """Return the subset of ``concept_ids`` whose ``:Canon`` nodes carry at least one
    incoming ``RESOLVES_TO`` edge — i.e. real student grading history. Read-only; a
    concept with such history must NOT be torn down. Callers pass a non-empty list
    (an ``UNWIND []`` would be a harmless no-op regardless)."""
    async with neo.session() as s:
        result = await s.run(
            "UNWIND $cids AS cid\n"
            "MATCH (c:Canon {concept_id: cid})<-[:RESOLVES_TO]-()\n"
            "RETURN DISTINCT cid AS cid",
            cids=concept_ids,
        )
        return {int(rec["cid"]) async for rec in result}


async def _detach_delete_canon(neo, concept_ids: list[int]) -> None:
    """DETACH DELETE each concept's ``:Canon`` nodes, GUARDED by the absence of an
    incoming ``RESOLVES_TO`` so student grading history is never destroyed (a
    RESOLVES_TO that appeared after the read below is still spared). Idempotent — no
    shared txn with Postgres, and a re-run over already-gone nodes is a no-op.
    Callers pass a non-empty list (an ``UNWIND []`` would be a harmless no-op)."""
    async with neo.session() as s:
        await s.run(
            "UNWIND $cids AS cid\n"
            "MATCH (c:Canon {concept_id: cid})\n"
            "WHERE NOT (c)<-[:RESOLVES_TO]-()\n"
            "DETACH DELETE c",
            cids=concept_ids,
        )


_IN_FLIGHT_STATUSES = frozenset({"pending", "indexing", "provisioning"})


@router.delete("/authored-sets/{set_id}")
async def delete_authored_set(
    set_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db_session),
) -> dict:
    """Remove an authored set and everything it produced.

    Deletable in any TERMINAL status (``done`` / ``failed``) — the motivation is
    clearing failed/stuck runs that otherwise pile up on the teacher console
    with no way to remove them. Rejected with 409 while the set is IN-FLIGHT
    (``pending`` / ``indexing`` / ``provisioning``): ``_run_set_background``
    writes Neo4j ``:Canon`` nodes per-candidate outside the Postgres
    transaction, so deleting mid-run would orphan ``:Canon`` nodes this
    endpoint's teardown can never see (it only reads the finished
    ``result_summary``). To recover a dead in-flight run (e.g. the worker
    crashed), mark the set ``failed`` first — the API itself doesn't provide
    that yet (no stuck-run watchdog exists), so today that's a manual status
    update. Cascade for the terminal-state delete that IS allowed:

      * the ConceptProblems this set minted (recorded per-problem in
        ``result_summary``), plus unreferenced Tier-1 leftovers whose provenance
        pins them to this set's problem document — deleting the rows is what pulls
        the content out of tutoring (the student selector filters ``tier == 2 AND
        concept_id``). ``removed_problems`` counts both referenced and swept rows;
      * the two hidden reference documents (chunks cascade via the
        ``aita_chunks`` ON DELETE CASCADE FK);
      * the ``apollo_authored_sets`` row itself.

    Per-concept KG teardown is STRICTLY scoped to concepts this set fully ORPHANED.
    A concept is torn down ONLY if it has ZERO footprint of any kind beyond this
    set: no remaining ``ConceptProblem``s, no Postgres student/seed footprint
    (``_protected_concepts`` — sessions, learner_state, mastery_events,
    misconceptions, or an inbound cross-concept prereq), AND no ``:Canon`` student
    ``RESOLVES_TO`` history. For those (and ONLY those) the reference graph a plain
    delete used to leave behind is torn down: ``apollo_dedup_decisions`` +
    ``apollo_concepts`` (KGEntity + ``apollo_entity_prereqs`` cascade) in Postgres,
    and the guarded ``:Canon`` nodes in Neo4j. Every ambiguous case spares the
    concept — under-tearing-down only leaves KG behind, whereas over-tearing-down
    would 500 (the ``apollo_sessions`` RESTRICT FK) or destroy student data.
    """
    auth = await require_user(request)
    row = await db.get(AuthoredSet, set_id)
    if row is None:
        raise HTTPException(status_code=404, detail="authored set not found")
    await require_course_teacher(db=db, auth=auth, search_space_id=int(row.search_space_id))
    if row.status in _IN_FLIGHT_STATUSES:
        raise HTTPException(
            status_code=409,
            detail=f"authored set is still {row.status}; mark it failed before deleting",
        )

    problems = (row.result_summary or {}).get("problems") or []  # type: ignore[call-overload]
    problem_ids = sorted(
        {
            int(p["concept_problem_id"])
            for p in problems
            if isinstance(p, dict) and p.get("concept_problem_id") is not None
        }
    )
    # Sweep set-scoped Tier-1 leftovers result_summary never referenced (rejected
    # candidates and dead runs record no concept_problem_id): inventory rows whose
    # provenance pins them to THIS set's problem document. Promoted rows are tier=2
    # by the promote flip, so the tier==1 filter can never touch live content.
    if row.problem_document_id is not None:
        tier1_rows = (
            await db.execute(
                select(ConceptProblem.id, ConceptProblem.provenance)
                .where(ConceptProblem.tier == 1)
                .where(ConceptProblem.search_space_id == int(row.search_space_id))
            )
        ).all()
        leftover_ids = {
            int(r.id)
            for r in tier1_rows
            if (r.provenance or {}).get("document_id") == int(row.problem_document_id)
        }
        problem_ids = sorted({*problem_ids, *leftover_ids})
    removed_problems = 0
    # Capture the concepts these problems belong to BEFORE deleting the rows — once
    # they are gone the concept link is unrecoverable.
    affected_concept_ids: list[int] = []
    if problem_ids:
        affected_concept_ids = [
            int(c)
            for c in (
                await db.execute(
                    select(ConceptProblem.concept_id)
                    .where(ConceptProblem.id.in_(problem_ids))
                    .distinct()
                )
            )
            .scalars()
            .all()
        ]
        res = await db.execute(delete(ConceptProblem).where(ConceptProblem.id.in_(problem_ids)))
        removed_problems = res.rowcount or 0

    doc_ids = [int(d) for d in (row.problem_document_id, row.solution_document_id) if d is not None]
    removed_documents = 0
    if doc_ids:
        res = await db.execute(delete(AITADocument).where(AITADocument.id.in_(doc_ids)))
        removed_documents = res.rowcount or 0

    # --- Full KG teardown for concepts this set ORPHANED --------------------- #
    # STRICT / conservative: a concept is torn down ONLY if it has ZERO footprint of
    # any kind beyond this set — no remaining problems, no Postgres student/seed
    # footprint (_protected_concepts), and no :Canon RESOLVES_TO grading history.
    # Under-tearing-down only leaves KG behind; over-tearing-down would 500 or
    # destroy student data, so every ambiguous case spares the concept.
    orphaned_concept_ids: list[int] = []
    neo = None
    if affected_concept_ids:
        # PG-orphan candidates: affected concepts with NO remaining ConceptProblem
        # (the set's deletes above are visible in this uncommitted transaction).
        surviving = {
            int(c)
            for c in (
                await db.execute(
                    select(ConceptProblem.concept_id)
                    .where(ConceptProblem.concept_id.in_(affected_concept_ids))
                    .distinct()
                )
            )
            .scalars()
            .all()
        }
        protected = await _protected_concepts(
            db, [cid for cid in affected_concept_ids if cid not in surviving]
        )
        candidates = [
            cid for cid in affected_concept_ids if cid not in surviving and cid not in protected
        ]
        if candidates:
            neo = _require_neo(get_neo4j_client(), stage="delete_authored_set")
            with_history = await _concepts_with_canon_history(neo, candidates)
            orphaned_concept_ids = [cid for cid in candidates if cid not in with_history]
            if orphaned_concept_ids:
                # dedup_decisions FK is ON DELETE SET NULL, so delete explicitly
                # BEFORE the concept; deleting apollo_concepts cascades KGEntity and
                # apollo_entity_prereqs. apollo_sessions (the only RESTRICT FK) is
                # already spared by _protected_concepts, so this never hard-fails.
                await db.execute(
                    delete(DedupDecision).where(DedupDecision.concept_id.in_(orphaned_concept_ids))
                )
                await db.execute(delete(Concept).where(Concept.id.in_(orphaned_concept_ids)))

    await db.delete(row)
    await db.commit()

    # Neo4j shares no txn with Postgres — run the guarded :Canon teardown AFTER the
    # PG commit so a Neo4j failure cannot roll back the PG delete. Idempotent.
    if orphaned_concept_ids and neo is not None:
        await _detach_delete_canon(neo, orphaned_concept_ids)

    _LOG.info(
        "authored_set_deleted",
        extra={
            "event": "authored_set_deleted",
            "set_id": set_id,
            "removed_problems": removed_problems,
            "removed_documents": removed_documents,
            "removed_concepts": len(orphaned_concept_ids),
        },
    )
    return {
        "deleted": True,
        "removed_problems": removed_problems,
        "removed_documents": removed_documents,
        "removed_concepts": len(orphaned_concept_ids),
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
    await require_course_teacher(
        db=db,
        auth=auth,
        search_space_id=int(authored_set.search_space_id),
    )

    row = await db.get(ConceptProblem, problem_id)
    if row is None or not _problem_belongs_to_set(authored_set, row, problem_id):
        # 404, not 403/409: don't leak whether problem_id exists at all, and
        # don't let a caller who cleared the course-membership gate use a
        # foreign set_id/problem_id pairing to promote into a search space
        # they don't control (cross-tenant IDOR — the problem must be one
        # THIS set actually minted, per its own result_summary, and must
        # live in the SAME search space as the set).
        raise HTTPException(status_code=404, detail="problem not found in this authored set")

    review = (row.provenance or {}).get("authored_review")  # type: ignore[call-overload]
    if not review or not review.get("required"):
        raise HTTPException(status_code=409, detail="problem is not held for review")
    if review.get("reason") == "no_matching_concept":
        # A NO_MATCH hold stores no draft to promote — the teacher must add the
        # concept to the course's premade list and re-upload the set.
        raise HTTPException(
            status_code=409,
            detail="problem matched no registered concept; add the concept to "
            "the course list and re-upload the set",
        )

    chosen = (
        review.get("generated_alt") if body.reference == "generated" else review.get("ocr_draft")
    )
    if chosen is None:
        raise HTTPException(status_code=422, detail=f"no '{body.reference}' reference stored")
    # Reversed provisioning: a hold that carries the closed-list match threads
    # it as resolved_concept so the approve-time mint never re-drafts a tag.
    stored_match = review.get("concept_match") or {}
    resolved = (
        ResolvedConcept(
            concept_id=int(stored_match["concept_id"]), slug=str(stored_match.get("slug") or "")
        )
        if stored_match.get("concept_id") is not None
        else None
    )
    return await approve_held_row(
        db,
        row=row,
        review=review,
        reference=body.reference,
        search_space_id=int(authored_set.search_space_id),
        resolved_concept=resolved,
        document_id=int(authored_set.problem_document_id or 0),
        stage="approve_held_problem",
    )


async def approve_held_row(
    db: AsyncSession,
    *,
    row: ConceptProblem,
    review: dict,
    reference: Literal["ocr", "generated"],
    search_space_id: int,
    resolved_concept: ResolvedConcept | None,
    document_id: int,
    stage: str,
) -> dict:
    """Mint and promote one validated held row within a rollback savepoint."""
    chosen = review.get("generated_alt") if reference == "generated" else review.get("ocr_draft")
    if chosen is None:
        raise HTTPException(status_code=422, detail=f"no '{reference}' reference stored")
    draft = ReferenceSolutionDraft.model_validate(chosen)
    pair = build_approved_pair(
        _candidate_from_row(row),
        draft,
        search_space_id=search_space_id,
    )
    metered_chat = _make_metered_chat(document_id=document_id)
    try:
        # Mint + promote ride ONE savepoint (mirrors the orchestrator): a lint
        # rejection or fail-closed TagMintError rolls back every KG row the
        # mint flushed instead of orphaning it into the commit below.
        async with db.begin_nested():
            mint_kwargs: dict = (
                {"resolved_concept": resolved_concept} if resolved_concept is not None else {}
            )
            mint_plan = await tag_and_mint(
                db,
                pair,
                chat_fn=_tag_mint_chat_fn(metered_chat),
                embed_fn=embed_text,
                **mint_kwargs,
            )
            existing_hashes = await _authored_concept_dup_hashes(
                db, concept_id=mint_plan.concept_id
            )
            result = await promote(
                db,
                _require_neo(get_neo4j_client(), stage=stage),
                problem=pair.problem,
                mint_plan=mint_plan,
                search_space_id=search_space_id,
                concept_problem_id=int(row.id),
                existing_problem_hashes=existing_hashes,
                solution_source=pair.solution_source,
                path_enumerator=(
                    partial(enumerate_strategy_paths, chat_fn=metered_chat.cheap)
                    if hasattr(metered_chat, "cheap")
                    else None
                ),
            )
            if not result.promoted:
                raise MintRejected(result)
    except TagMintError as exc:
        return {
            "promoted": False,
            "failed_gate": None,
            "diagnostic": f"tag_mint_error: {exc}",
        }
    except MintRejected as rejected:
        result = rejected.result
    if result.promoted:
        row.provenance = {  # type: ignore[assignment]
            **(row.provenance or {}),
            "authored_review": {
                **review,
                "required": False,
                "approved_reference": reference,
            },
        }
        await db.commit()
    return {
        "promoted": result.promoted,
        "failed_gate": result.failed_gate,
        "diagnostic": result.diagnostic,
    }


def _problem_belongs_to_set(
    authored_set: AuthoredSet, problem: ConceptProblem, problem_id: int
) -> bool:
    """True iff ``problem`` is one this ``authored_set`` actually minted.

    Two independent checks, both required: the id must appear in the set's own
    ``result_summary["problems"]`` list (the orchestrator's per-problem outcome
    ledger — see ``run_authored_set_provisioning`` / ``_run_set_background``),
    AND the row's own ``search_space_id`` must match the set's — belt-and-
    suspenders against a stale/corrupted ``result_summary`` pointing at a
    problem that has since moved (or was minted) into a different course.
    """
    minted_ids = {
        int(p["concept_problem_id"])
        for p in (authored_set.result_summary or {}).get("problems") or []  # type: ignore[union-attr]
        if isinstance(p, dict) and p.get("concept_problem_id") is not None
    }
    if problem_id not in minted_ids:
        return False
    return int(problem.search_space_id) == int(authored_set.search_space_id)


def typed_confirmation_expiry_at(_constructed_at: str | None = None) -> None:
    """Disabled-by-default expiry/reminder configuration stub.

    Pending typed drafts intentionally remain actionable indefinitely. A future
    reminder product can replace this no-op without introducing lifecycle state
    today.
    """
    return None


def _replace_result_entry(
    authored_set: AuthoredSet,
    *,
    problem_id: int,
    outcome: str,
    reason: str,
    diagnostic: str = "",
    failed_gate: int | None = None,
) -> bool:
    summary = dict(authored_set.result_summary or {})
    raw_problems = summary.get("problems")
    if not isinstance(raw_problems, list):
        return False
    replaced = False
    problems: list[Any] = []
    for raw in raw_problems:
        if not isinstance(raw, dict) or raw.get("concept_problem_id") != problem_id:
            problems.append(raw)
            continue
        problems.append(
            {
                **raw,
                "outcome": outcome,
                "reason": reason,
                "diagnostic": diagnostic,
                "failed_gate": failed_gate,
            }
        )
        replaced = True
    if not replaced:
        return False
    counts: dict[str, int] = {}
    for item in problems:
        if isinstance(item, dict) and isinstance(item.get("outcome"), str):
            key = item["outcome"]
            counts[key] = counts.get(key, 0) + 1
    summary["problems"] = problems
    summary["counts"] = counts
    authored_set.result_summary = summary
    authored_set.status = (
        "provisioning" if counts.get("awaiting_teacher_confirmation", 0) > 0 else "done"
    )
    authored_set.updated_at = datetime.now(UTC)
    return True


async def _replace_problem_result(
    db: AsyncSession,
    *,
    problem_id: int,
    outcome: str,
    reason: str,
    diagnostic: str = "",
    failed_gate: int | None = None,
) -> AuthoredSet | None:
    """Update the one authored-set ledger entry that owns ``problem_id``."""
    rows = (
        (
            await db.execute(
                select(AuthoredSet).order_by(AuthoredSet.created_at.desc(), AuthoredSet.id.desc())
            )
        )
        .scalars()
        .all()
    )
    for authored_set in rows:
        if _replace_result_entry(
            authored_set,
            problem_id=problem_id,
            outcome=outcome,
            reason=reason,
            diagnostic=diagnostic,
            failed_gate=failed_gate,
        ):
            return authored_set
    return None


async def _typed_problem_hashes(
    db: AsyncSession,
    *,
    search_space_id: int,
    exclude_problem_id: int,
) -> set[str]:
    """Course-wide duplicate inventory for a draft not yet assigned a concept."""
    payloads = (
        (
            await db.execute(
                select(ConceptProblem.payload)
                .where(ConceptProblem.search_space_id == search_space_id)
                .where(ConceptProblem.tier == 2)
                .where(ConceptProblem.id != exclude_problem_id)
            )
        )
        .scalars()
        .all()
    )
    hashes: set[str] = set()
    for payload in payloads:
        try:
            hashes.add(problem_dup_hash(Problem.model_validate(payload)))
        except (ValidationError, ValueError, TypeError):
            continue
    return hashes


async def _typed_set_problem(
    db: AsyncSession,
    *,
    set_id: int,
    problem_id: int,
    auth: Any,
) -> tuple[AuthoredSet, ConceptProblem]:
    authored_set = await db.get(AuthoredSet, set_id)
    if authored_set is None:
        raise HTTPException(status_code=404, detail="authored set not found")
    await require_course_teacher(
        db=db,
        auth=auth,
        search_space_id=int(authored_set.search_space_id),
    )
    row = await db.get(ConceptProblem, problem_id)
    if row is None or not _problem_belongs_to_set(authored_set, row, problem_id):
        raise HTTPException(status_code=404, detail="problem not found in this authored set")
    return authored_set, row


async def _record_confirmation_rejection(
    db: AsyncSession,
    *,
    authored_set: AuthoredSet,
    row: ConceptProblem,
    failed_gate: int | None,
    diagnostic: str,
) -> None:
    ingest_run_id = (authored_set.result_summary or {}).get("ingest_run_id")
    db.add(
        RejectedProblem(
            ingest_run_id=int(ingest_run_id) if isinstance(ingest_run_id, int) else None,
            search_space_id=int(authored_set.search_space_id),
            concept_id=int(row.concept_id),
            failed_gate=failed_gate,
            rejected_stage="typed_confirmation_promotion",
            diagnostic=diagnostic,
            payload={
                "problem_text": (row.payload or {}).get("problem_text", ""),
                "reason": "duplicate" if failed_gate == 8 else "solve_and_check",
            },
        )
    )
    if isinstance(ingest_run_id, int):
        run = await db.get(IngestRun, ingest_run_id)
        if run is not None:
            run.n_rejected = int(run.n_rejected or 0) + 1


async def _run_rehoming_job_background(*, job_id: int) -> None:
    """Immediate worker for a durable job; a failed attempt remains retryable."""
    async with get_async_session() as db:
        claimed = await claim_rehoming_job(
            db,
            job_id=job_id,
            lease_owner="authored-api-background",
            lease_seconds=1800,
        )
    if claimed is None:
        return
    async with get_async_session() as db:
        resolved: ResolvedConcept | None = None
        if claimed.requested_concept_id is not None:
            concept = await db.get(Concept, claimed.requested_concept_id)
            resolved = ResolvedConcept(
                concept_id=claimed.requested_concept_id,
                slug=str(concept.slug if concept is not None else ""),
            )
        metered = _make_metered_chat(document_id=None)
        ok = await run_rehoming(
            db,
            get_neo4j_client(),
            problem_id=claimed.problem_id,
            chat_fn=_tag_mint_chat_fn(metered),
            embed_fn=embed_text,
            resolved_concept=resolved,
            job_id=claimed.job_id,
        )
        if ok:
            await complete_rehoming_job(db, job_id=claimed.job_id)
            return
        row = await db.get(ConceptProblem, claimed.problem_id)
        state = (row.provenance or {}).get("typed_rehoming") if row is not None else {}
        diagnostic = state.get("diagnostic", "rehoming failed") if isinstance(state, dict) else ""
        await fail_rehoming_job(db, job_id=claimed.job_id, error=str(diagnostic))


@router.post("/authored-sets/{set_id}/problems/{problem_id}/confirm")
async def confirm_typed_problem(
    set_id: int,
    problem_id: int,
    request: Request,
    background: BackgroundTasks,
    db: AsyncSession = Depends(get_db_session),
) -> dict:
    """Approve one constructed typed draft, then enqueue non-blocking re-homing."""
    auth = await require_user(request)
    authored_set, row = await _typed_set_problem(
        db,
        set_id=set_id,
        problem_id=problem_id,
        auth=auth,
    )
    confirmation = (row.provenance or {}).get("typed_confirmation")
    if not isinstance(confirmation, dict) or confirmation.get("status") != (
        "awaiting_teacher_confirmation"
    ):
        raise HTTPException(status_code=409, detail="typed draft is not awaiting confirmation")

    confirmed_at = datetime.now(UTC).isoformat()
    result = await promote_typed_confirmed(
        db,
        problem=dict(row.payload or {}),
        concept_problem_id=int(row.id),
        existing_problem_hashes=await _typed_problem_hashes(
            db,
            search_space_id=int(authored_set.search_space_id),
            exclude_problem_id=int(row.id),
        ),
        confirmed_by=str(auth.user_id),
        confirmed_at=confirmed_at,
    )
    if result.promoted:
        row.provenance = {
            **(row.provenance or {}),
            "typed_confirmation": {
                **confirmation,
                "status": "teacher_confirmed",
                "confirmed_by": str(auth.user_id),
                "confirmed_at": confirmed_at,
            },
        }
        job_id = await enqueue_rehoming(db, row)
        _replace_result_entry(
            authored_set,
            problem_id=problem_id,
            outcome="promoted",
            reason="rehoming_pending",
        )
        ingest_run_id = (authored_set.result_summary or {}).get("ingest_run_id")
        if isinstance(ingest_run_id, int):
            run = await db.get(IngestRun, ingest_run_id)
            if run is not None:
                run.n_promoted = int(run.n_promoted or 0) + 1
        await db.commit()
        background.add_task(_run_rehoming_job_background, job_id=job_id)
        return {
            "promoted": True,
            "failed_gate": None,
            "diagnostic": "",
            "rehoming": "rehoming_pending",
            "job_id": job_id,
        }

    outcome = "held_for_review" if isinstance(result, PromoteHeldForReview) else "rejected"
    reason = (
        "solve_unresolved"
        if isinstance(result, PromoteHeldForReview)
        else ("duplicate" if result.failed_gate == 8 else "solve_refuted")
    )
    row.provenance = {
        **(row.provenance or {}),
        "typed_confirmation": {
            **confirmation,
            "status": "teacher_confirmed_not_promoted",
            "confirmed_by": str(auth.user_id),
            "confirmed_at": confirmed_at,
            "diagnostic": result.diagnostic,
        },
    }
    _replace_result_entry(
        authored_set,
        problem_id=problem_id,
        outcome=outcome,
        reason=reason,
        diagnostic=result.diagnostic,
        failed_gate=result.failed_gate,
    )
    if outcome == "rejected":
        await _record_confirmation_rejection(
            db,
            authored_set=authored_set,
            row=row,
            failed_gate=result.failed_gate,
            diagnostic=result.diagnostic,
        )
    await db.commit()
    return {
        "promoted": False,
        "outcome": outcome,
        "failed_gate": result.failed_gate,
        "diagnostic": result.diagnostic,
    }


@router.post("/authored-sets/{set_id}/problems/{problem_id}/discard")
async def discard_typed_problem(
    set_id: int,
    problem_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db_session),
) -> dict:
    auth = await require_user(request)
    authored_set, row = await _typed_set_problem(
        db,
        set_id=set_id,
        problem_id=problem_id,
        auth=auth,
    )
    confirmation = (row.provenance or {}).get("typed_confirmation")
    if not isinstance(confirmation, dict) or confirmation.get("status") != (
        "awaiting_teacher_confirmation"
    ):
        raise HTTPException(status_code=409, detail="typed draft is not awaiting confirmation")
    _replace_result_entry(
        authored_set,
        problem_id=problem_id,
        outcome="discarded",
        reason="teacher_discarded",
    )
    await db.delete(row)
    await db.commit()
    return {"discarded": True, "problem_id": problem_id}


async def _validate_existing_concept(
    db: AsyncSession,
    *,
    concept_id: int,
    search_space_id: int,
) -> Concept:
    concept = await db.get(Concept, concept_id)
    subject = await db.get(Subject, int(concept.subject_id)) if concept is not None else None
    if concept is None or subject is None or int(subject.search_space_id) != search_space_id:
        raise HTTPException(status_code=404, detail="concept not found in this course")
    return concept


async def _queue_rehoming_action(
    *,
    set_id: int,
    problem_id: int,
    requested_concept_id: int | None,
    request: Request,
    background: BackgroundTasks,
    db: AsyncSession,
) -> dict:
    auth = await require_user(request)
    authored_set, row = await _typed_set_problem(
        db,
        set_id=set_id,
        problem_id=problem_id,
        auth=auth,
    )
    if row.tier != 2:
        raise HTTPException(status_code=409, detail="problem is not promoted")
    if requested_concept_id is not None:
        await _validate_existing_concept(
            db,
            concept_id=requested_concept_id,
            search_space_id=int(authored_set.search_space_id),
        )
    job_id = await enqueue_rehoming(
        db,
        row,
        requested_concept_id=requested_concept_id,
    )
    await db.commit()
    background.add_task(_run_rehoming_job_background, job_id=job_id)
    return {"rehoming": "rehoming_pending", "job_id": job_id}


@router.post("/authored-sets/{set_id}/problems/{problem_id}/rehoming/retry")
async def retry_typed_rehoming(
    set_id: int,
    problem_id: int,
    request: Request,
    background: BackgroundTasks,
    db: AsyncSession = Depends(get_db_session),
) -> dict:
    return await _queue_rehoming_action(
        set_id=set_id,
        problem_id=problem_id,
        requested_concept_id=None,
        request=request,
        background=background,
        db=db,
    )


@router.post("/authored-sets/{set_id}/problems/{problem_id}/rehoming/assign")
async def assign_typed_problem_concept(
    set_id: int,
    problem_id: int,
    body: AssignConceptBody,
    request: Request,
    background: BackgroundTasks,
    db: AsyncSession = Depends(get_db_session),
) -> dict:
    return await _queue_rehoming_action(
        set_id=set_id,
        problem_id=problem_id,
        requested_concept_id=body.concept_id,
        request=request,
        background=background,
        db=db,
    )


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
