"""Apollo FastAPI router. Every endpoint requires Supabase bearer auth (see
apollo/auth_deps.py). Session-scoped routes are owner-gated via
require_session_owner; session creation is membership-gated via
require_course_member.

Mounted at /apollo in server.py. Each named error class from apollo.errors
is registered with an exception handler that surfaces the error as a
structured JSON response — NO FALLBACK behavior, just visible failure.
"""

from __future__ import annotations

import logging
from typing import Literal

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from apollo.auth_deps import (
    require_course_member,
    require_course_teacher,
    require_session_owner,
    require_user,
)
from apollo.errors import (
    CoverageGradingError,
    FilterRejectedError,
    InvalidPhaseError,
    KGEntryNotFoundError,
    KGUnavailableError,
    MalformedEquationError,
    NoMatchingConceptError,
    ParserCouldNotExtractError,
    PoolExhaustedError,
    ProblemNotFoundError,
    ResolutionInvalidOutputError,
    ResolutionUnavailableError,
    ReviewRequiredError,
    SessionFrozenError,
    TranscriptAuditUnavailableError,
)
from apollo.graph_compare.validator import (
    ReferenceGraphInvalidError,
    StudentGraphInvalidError,
)
from apollo.handlers.browse import handle_list_problems
from apollo.handlers.chat import handle_chat
from apollo.handlers.done import handle_done
from apollo.handlers.lifecycle import handle_end, handle_get_session, handle_retry
from apollo.handlers.negotiate import (
    ChallengeRequest,
    ParaphraseRequest,
    handle_challenge,
    handle_get_trace,
    handle_paraphrase,
    handle_skip,
)
from apollo.handlers.progress import handle_get_progress, handle_get_progress_detail
from apollo.hoot_bridge.session_init import init_session_direct, init_session_from_hoot
from apollo.persistence.neo4j_client import Neo4jClient
from apollo.projections.classroom import (
    DEFAULT_WINDOW_DAYS,
    mastery_heatmap,
    struggle_signals,
)
from apollo.provisioning.authored_sets.api import router as authored_sets_router
from apollo.provisioning.concepts_api import router as teacher_concepts_router
from apollo.provisioning.problem_generation.api import router as problem_generation_router
from apollo.subjects.curriculum_db import list_course_concepts
from auth import AuthContext
from database.session import get_db_session

_LOG = logging.getLogger(__name__)

# ----------------------------------------------------------------------
# Neo4j client — process singleton, lazily constructed.
# Used by handlers that need to read/write the per-attempt KG subgraph.
#
# Degraded mode: Neo4j is optional infrastructure for the student
# interaction (the served grade is the transcript LLM grader; the graph
# lane is shadow-only). `get_neo4j_client` therefore never raises — a
# construction failure (missing/bad env, Aura unreachable) is logged and
# returns None. NO NEGATIVE CACHING: a failure does not poison the
# singleton, so the next request retries construction fresh (env may be
# fixed / Aura may return). Handlers + KGStore degrade explicitly on None.
# ----------------------------------------------------------------------

_neo4j_client_singleton: Neo4jClient | None = None


def get_neo4j_client() -> Neo4jClient | None:
    global _neo4j_client_singleton
    if _neo4j_client_singleton is None:
        try:
            _neo4j_client_singleton = Neo4jClient.from_env()
        except Exception as exc:  # noqa: BLE001 - degrade, never 500 the request
            _LOG.warning("apollo_neo4j_client_construction_failed error=%s", exc)
            return None
    return _neo4j_client_singleton


async def require_neo4j_client(
    neo: Neo4jClient | None = Depends(get_neo4j_client),
) -> Neo4jClient:
    """Dependency for KG-native routes (authored-set provisioning) where a
    missing Neo4j client should surface a structured 503 rather than degrade
    silently — there is no meaningful Postgres-only fallback for teacher
    provisioning."""
    if neo is None:
        raise KGUnavailableError(stage="get_neo4j_client", last_error="client unavailable")
    return neo


async def close_neo4j_client() -> None:
    """Close the process-wide Neo4j driver. Wire to FastAPI's shutdown event."""
    global _neo4j_client_singleton
    if _neo4j_client_singleton is not None:
        await _neo4j_client_singleton.close()
        _neo4j_client_singleton = None


router = APIRouter(prefix="/apollo", tags=["apollo"])

router.include_router(authored_sets_router)
router.include_router(problem_generation_router)
router.include_router(teacher_concepts_router)


class FromHootRequest(BaseModel):
    search_space_id: int
    hoot_transcript: str
    # Hoot→Apollo handoff starts at 'intro' by default (see session_init
    # docstring). Optional so the one-click "Teach Apollo" button need not
    # send it; explicit callers may still override.
    difficulty: Literal["intro", "standard", "hard"] = "intro"


class SessionCreateRequest(BaseModel):
    search_space_id: int
    concept_id: int
    # Standalone entry: the student explicitly picks — no default.
    difficulty: Literal["intro", "standard", "hard"]
    problem_id: str | None = None


class ChatRequest(BaseModel):
    message: str


class NextRequest(BaseModel):
    difficulty: Literal["intro", "standard", "hard"]


@router.post("/sessions/from_hoot")
async def session_from_hoot(
    body: FromHootRequest,
    request: Request,
    db: AsyncSession = Depends(get_db_session),
) -> dict:
    auth = await require_user(request)
    await require_course_member(db=db, auth=auth, search_space_id=body.search_space_id)
    return await init_session_from_hoot(
        db=db,
        user_id=auth.user_id,
        search_space_id=body.search_space_id,
        hoot_transcript=body.hoot_transcript,
        difficulty=body.difficulty,
    )


@router.post("/sessions")
async def create_session(
    body: SessionCreateRequest,
    request: Request,
    db: AsyncSession = Depends(get_db_session),
) -> dict:
    """Standalone Apollo entry (no Hoot transcript): explicit concept +
    difficulty + optional specific problem."""
    auth = await require_user(request)
    await require_course_member(db=db, auth=auth, search_space_id=body.search_space_id)
    return await init_session_direct(
        db=db,
        user_id=auth.user_id,
        search_space_id=body.search_space_id,
        concept_id=body.concept_id,
        difficulty=body.difficulty,
        problem_id=body.problem_id,
    )


# `auth` is injected for its gate side-effect (401/403/404 via
# require_session_owner). Handlers don't need the identity — ownership
# is the only check required at this layer.
@router.get("/sessions/{session_id}")
async def get_session(
    session_id: int,
    db: AsyncSession = Depends(get_db_session),
    neo: Neo4jClient | None = Depends(get_neo4j_client),
    auth: AuthContext = Depends(require_session_owner),
) -> dict:
    return await handle_get_session(db=db, neo=neo, session_id=session_id)


@router.post("/sessions/{session_id}/chat")
async def chat(
    session_id: int,
    body: ChatRequest,
    db: AsyncSession = Depends(get_db_session),
    neo: Neo4jClient | None = Depends(get_neo4j_client),
    auth: AuthContext = Depends(require_session_owner),
) -> dict:
    return await handle_chat(db=db, neo=neo, session_id=session_id, message=body.message)


@router.post("/sessions/{session_id}/done")
async def done(
    session_id: int,
    db: AsyncSession = Depends(get_db_session),
    neo: Neo4jClient | None = Depends(get_neo4j_client),
    auth: AuthContext = Depends(require_session_owner),
) -> dict:
    return await handle_done(db=db, neo=neo, session_id=session_id)


@router.post("/sessions/{session_id}/retry")
async def retry(
    session_id: int,
    db: AsyncSession = Depends(get_db_session),
    auth: AuthContext = Depends(require_session_owner),
) -> dict:
    return await handle_retry(db=db, session_id=session_id)


@router.post("/sessions/{session_id}/next")
async def next_problem(
    session_id: int,
    body: NextRequest,
    db: AsyncSession = Depends(get_db_session),
    auth: AuthContext = Depends(require_session_owner),
) -> dict:
    from apollo.handlers.next import handle_next

    return await handle_next(db=db, session_id=session_id, difficulty=body.difficulty)


@router.post("/sessions/{session_id}/restart_problem")
async def restart_problem(
    session_id: int,
    db: AsyncSession = Depends(get_db_session),
    neo: Neo4jClient | None = Depends(get_neo4j_client),
    auth: AuthContext = Depends(require_session_owner),
) -> dict:
    from apollo.handlers.restart_problem import handle_restart_problem

    return await handle_restart_problem(db=db, neo=neo, session_id=session_id)


@router.post("/sessions/{session_id}/end")
async def end(
    session_id: int,
    db: AsyncSession = Depends(get_db_session),
    neo: Neo4jClient | None = Depends(get_neo4j_client),
    auth: AuthContext = Depends(require_session_owner),
) -> dict:
    return await handle_end(db=db, neo=neo, session_id=session_id)


@router.get("/progress")
async def progress(
    request: Request,
    search_space_id: int | None = None,
    db: AsyncSession = Depends(get_db_session),
) -> dict:
    auth = await require_user(request)
    if search_space_id is None:
        return await handle_get_progress(db=db, user_id=auth.user_id)
    await require_course_member(db=db, auth=auth, search_space_id=search_space_id)
    return await handle_get_progress_detail(
        db=db, user_id=auth.user_id, search_space_id=search_space_id
    )


@router.get("/concepts")
async def list_concepts(
    search_space_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db_session),
) -> dict:
    """Student browse surface: the course's teachable concepts (tier-2,
    non-quarantined problem pool — same predicate as session entry)."""
    auth = await require_user(request)
    await require_course_member(db=db, auth=auth, search_space_id=search_space_id)
    rows = await list_course_concepts(db, search_space_id=search_space_id)
    return {
        "concepts": [
            {"concept_id": r.concept_id, "slug": r.slug, "display_name": r.display_name}
            for r in rows
        ]
    }


@router.get("/problems")
async def list_problems(
    search_space_id: int,
    concept_id: int,
    request: Request,
    difficulty: Literal["intro", "standard", "hard"] | None = None,
    db: AsyncSession = Depends(get_db_session),
) -> dict:
    auth = await require_user(request)
    await require_course_member(db=db, auth=auth, search_space_id=search_space_id)
    return await handle_list_problems(
        db=db,
        user_id=auth.user_id,
        search_space_id=search_space_id,
        concept_id=concept_id,
        difficulty=difficulty,
    )


# ----------------------------------------------------------------------
# P3 — Negotiable OLM. Three move endpoints + trace lookup.
# ----------------------------------------------------------------------


@router.post("/sessions/{session_id}/kg/{entry_id}/challenge")
async def negotiate_challenge(
    session_id: int,
    entry_id: str,
    body: ChallengeRequest,
    db: AsyncSession = Depends(get_db_session),
    neo: Neo4jClient | None = Depends(get_neo4j_client),
    auth: AuthContext = Depends(require_session_owner),
) -> dict:
    return await handle_challenge(
        db=db,
        neo=neo,
        session_id=session_id,
        entry_id=entry_id,
        body=body,
    )


@router.post("/sessions/{session_id}/kg/{entry_id}/paraphrase")
async def negotiate_paraphrase(
    session_id: int,
    entry_id: str,
    body: ParaphraseRequest,
    db: AsyncSession = Depends(get_db_session),
    neo: Neo4jClient | None = Depends(get_neo4j_client),
    auth: AuthContext = Depends(require_session_owner),
) -> dict:
    return await handle_paraphrase(
        db=db,
        neo=neo,
        session_id=session_id,
        entry_id=entry_id,
        body=body,
    )


@router.post("/sessions/{session_id}/kg/{entry_id}/skip")
async def negotiate_skip(
    session_id: int,
    entry_id: str,
    db: AsyncSession = Depends(get_db_session),
    neo: Neo4jClient | None = Depends(get_neo4j_client),
    auth: AuthContext = Depends(require_session_owner),
) -> dict:
    return await handle_skip(
        db=db,
        neo=neo,
        session_id=session_id,
        entry_id=entry_id,
    )


@router.get("/sessions/{session_id}/kg/{entry_id}/trace")
async def negotiate_trace(
    session_id: int,
    entry_id: str,
    db: AsyncSession = Depends(get_db_session),
    neo: Neo4jClient | None = Depends(get_neo4j_client),
    auth: AuthContext = Depends(require_session_owner),
) -> dict:
    return await handle_get_trace(
        db=db,
        neo=neo,
        session_id=session_id,
        entry_id=entry_id,
    )


# ----------------------------------------------------------------------
# Campaign-plan Task B3 — teacher-facing classroom projections (spec §2):
# pure read-side aggregation over apollo_learner_state / apollo_grading_
# artifacts, no new inference. Teacher-gated (require_course_teacher) rather
# than require_course_member -- these expose every student's state.
# ----------------------------------------------------------------------


@router.get("/teacher/classroom/{search_space_id}/heatmap")
async def classroom_heatmap(
    search_space_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db_session),
) -> dict:
    auth = await require_user(request)
    await require_course_teacher(db=db, auth=auth, search_space_id=search_space_id)
    rows = await mastery_heatmap(db, search_space_id=search_space_id)
    return {"rows": rows}


@router.get("/teacher/classroom/{search_space_id}/struggles")
async def classroom_struggles(
    search_space_id: int,
    request: Request,
    window_days: int = DEFAULT_WINDOW_DAYS,
    db: AsyncSession = Depends(get_db_session),
) -> dict:
    auth = await require_user(request)
    await require_course_teacher(db=db, auth=auth, search_space_id=search_space_id)
    return await struggle_signals(
        db, search_space_id=search_space_id, window_days=window_days,
    )


# ----------------------------------------------------------------------
# Exception handlers — surface every Apollo error as a structured JSON
# response. NO FALLBACK: each error type gets its own HTTP status + code.
# ----------------------------------------------------------------------


def _err_payload(code: str, message: str, **extra: object) -> dict:
    payload = {"error_code": code, "message": message}
    payload.update(extra)
    return payload


async def parser_could_not_extract_handler(
    request: Request, exc: ParserCouldNotExtractError
) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content=_err_payload(
            "parser_could_not_extract",
            str(exc),
            utterance=exc.utterance,
        ),
    )


async def filter_rejected_handler(request: Request, exc: FilterRejectedError) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content=_err_payload(
            "filter_rejected",
            str(exc),
            rejected_term=exc.rejected_term,
            kg=exc.kg,
        ),
    )


async def malformed_equation_handler(request: Request, exc: MalformedEquationError) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content=_err_payload(
            "malformed_equation",
            str(exc),
            entry_id=exc.entry_id,
            symbolic=exc.symbolic,
            parse_error=exc.parse_error,
        ),
    )


async def no_matching_concept_handler(
    request: Request, exc: NoMatchingConceptError
) -> JSONResponse:
    return JSONResponse(
        status_code=409,
        content=_err_payload(
            "no_matching_concept",
            "Apollo doesn't cover this topic yet.",
            transcript_summary=exc.transcript_summary,
        ),
    )


async def pool_exhausted_handler(request: Request, exc: PoolExhaustedError) -> JSONResponse:
    return JSONResponse(
        status_code=409,
        content=_err_payload(
            "pool_exhausted",
            str(exc),
            concept_cluster_id=exc.concept_cluster_id,
            difficulty=exc.difficulty,
        ),
    )


async def session_frozen_handler(request: Request, exc: SessionFrozenError) -> JSONResponse:
    return JSONResponse(
        status_code=409,
        content=_err_payload(
            "session_frozen",
            str(exc),
            session_id=exc.session_id,
        ),
    )


async def invalid_phase_handler(request: Request, exc: InvalidPhaseError) -> JSONResponse:
    return JSONResponse(
        status_code=409,
        content=_err_payload(
            "invalid_phase",
            str(exc),
            session_id=exc.session_id,
            phase=exc.phase,
        ),
    )


async def review_required_handler(request: Request, exc: ReviewRequiredError) -> JSONResponse:
    """P3.6 OLM Done-gate. Flagged entries need a negotiation move before
    grading proceeds. The FE renders a review modal listing each entry."""
    return JSONResponse(
        status_code=422,
        content=_err_payload(
            "review_required",
            str(exc),
            review_required=exc.entries,
        ),
    )


async def kg_entry_not_found_handler(request: Request, exc: KGEntryNotFoundError) -> JSONResponse:
    """P3 — Negotiable OLM. Targeted entry doesn't exist in the per-attempt
    subgraph (stale FE state, race with parser, or bad entry_id)."""
    return JSONResponse(
        status_code=404,
        content=_err_payload(
            "kg_entry_not_found",
            str(exc),
            attempt_id=exc.attempt_id,
            node_id=exc.node_id,
        ),
    )


async def problem_not_found_handler(request: Request, exc: ProblemNotFoundError) -> JSONResponse:
    """Standalone entry named a problem that is not in the teachable pool
    (stale browse list, quarantined since, or bad id)."""
    return JSONResponse(
        status_code=404,
        content=_err_payload(
            "problem_not_found",
            str(exc),
            problem_id=exc.problem_id,
            concept_id=exc.concept_id,
        ),
    )


async def kg_unavailable_handler(request: Request, exc: KGUnavailableError) -> JSONResponse:
    """Degraded mode: Neo4j is unreachable / mid-call driver failure on a
    KG-native route (negotiation moves, restart_problem, authored-set
    provisioning). Distinct from the NO-FALLBACK family — this is
    infrastructure optionality, not a grading bug — but still a loud,
    structured 503 rather than a silent downgrade."""
    return JSONResponse(
        status_code=503,
        content=_err_payload(
            "kg_unavailable",
            "The knowledge-graph panel is temporarily unavailable — your "
            "session is unaffected; try again shortly.",
            stage=exc.stage,
            last_error=exc.last_error,
        ),
    )


async def coverage_grading_handler(request: Request, exc: CoverageGradingError) -> JSONResponse:
    """Item #10: 503 surfaces the no-fallback contract — the UI shows
    "grading unavailable, try again" instead of receiving a downgraded
    grade silently."""
    return JSONResponse(
        status_code=503,
        content=_err_payload(
            "coverage_grading_failed",
            "Grading is temporarily unavailable. Please try again in a moment.",
            stage=exc.stage,
            last_error=exc.last_error,
        ),
    )


# ----------------------------------------------------------------------
# WU-4C1 — the five Done shadow-chain named errors -> HTTP. The two 503s
# mirror the no-fallback contract; the user message reads as non-fatal to the
# student ("your grade is saved; grading is computing in the background"),
# because the SHADOW failure NEVER voids the already-committed student grade.
# ----------------------------------------------------------------------


async def resolution_unavailable_handler(
    request: Request, exc: ResolutionUnavailableError
) -> JSONResponse:
    return JSONResponse(
        status_code=503,
        content=_err_payload(
            "resolution_unavailable",
            "Your grade is saved. Deeper grading is computing in the background — please try again shortly.",
            stage=exc.stage,
            last_error=exc.last_error,
        ),
    )


async def transcript_audit_unavailable_handler(
    request: Request, exc: TranscriptAuditUnavailableError
) -> JSONResponse:
    return JSONResponse(
        status_code=503,
        content=_err_payload(
            "transcript_audit_unavailable",
            "Your grade is saved. Deeper grading is computing in the background — please try again shortly.",
            stage=exc.stage,
            last_error=exc.last_error,
        ),
    )


async def resolution_invalid_output_handler(
    request: Request, exc: ResolutionInvalidOutputError
) -> JSONResponse:
    """The adjudicator returned a key outside the closed candidate set (a
    hallucination). The payload is bounded: the COUNT of allowed keys, never the
    full list."""
    return JSONResponse(
        status_code=500,
        content=_err_payload(
            "resolution_invalid_output",
            str(exc),
            returned_key=exc.returned_key,
            allowed_key_count=len(exc.allowed_keys),
        ),
    )


async def student_graph_invalid_handler(
    request: Request, exc: StudentGraphInvalidError
) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content=_err_payload(
            "student_graph_invalid",
            str(exc),
            reasons=list(exc.reasons),
        ),
    )


async def reference_graph_invalid_handler(
    request: Request, exc: ReferenceGraphInvalidError
) -> JSONResponse:
    return JSONResponse(
        status_code=409,
        content=_err_payload(
            "reference_graph_invalid",
            str(exc),
            reasons=list(exc.reasons),
        ),
    )


async def context_overflow_handler(request: Request, exc) -> JSONResponse:
    """Item #2: surface token-budget overflow as 503 instead of silently
    truncating Apollo's context."""
    return JSONResponse(
        status_code=503,
        content=_err_payload(
            "context_overflow",
            "This session has grown too long for Apollo to keep up — please start a fresh session.",
            tokens=getattr(exc, "tokens", None),
            budget=getattr(exc, "budget", None),
        ),
    )


def register_exception_handlers(app) -> None:
    """Register all Apollo exception handlers onto the FastAPI app."""
    app.add_exception_handler(ParserCouldNotExtractError, parser_could_not_extract_handler)
    app.add_exception_handler(FilterRejectedError, filter_rejected_handler)
    app.add_exception_handler(InvalidPhaseError, invalid_phase_handler)
    app.add_exception_handler(MalformedEquationError, malformed_equation_handler)
    app.add_exception_handler(NoMatchingConceptError, no_matching_concept_handler)
    app.add_exception_handler(PoolExhaustedError, pool_exhausted_handler)
    app.add_exception_handler(CoverageGradingError, coverage_grading_handler)
    app.add_exception_handler(KGUnavailableError, kg_unavailable_handler)
    # ContextOverflowError lives in apollo.agent.apollo_llm; import lazily
    # to avoid a circular import in api.py's top-level module load.
    from apollo.agent.apollo_llm import ContextOverflowError

    app.add_exception_handler(ContextOverflowError, context_overflow_handler)
    app.add_exception_handler(SessionFrozenError, session_frozen_handler)
    app.add_exception_handler(KGEntryNotFoundError, kg_entry_not_found_handler)
    app.add_exception_handler(ProblemNotFoundError, problem_not_found_handler)
    app.add_exception_handler(ReviewRequiredError, review_required_handler)
    # WU-4C1 — the five Done shadow-chain named errors.
    app.add_exception_handler(ResolutionUnavailableError, resolution_unavailable_handler)
    app.add_exception_handler(TranscriptAuditUnavailableError, transcript_audit_unavailable_handler)
    app.add_exception_handler(ResolutionInvalidOutputError, resolution_invalid_output_handler)
    app.add_exception_handler(StudentGraphInvalidError, student_graph_invalid_handler)
    app.add_exception_handler(ReferenceGraphInvalidError, reference_graph_invalid_handler)
