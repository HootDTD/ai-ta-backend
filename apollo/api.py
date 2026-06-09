"""Apollo FastAPI router. Endpoints stubbed to 501 until implementations land.

Mounted at /apollo in server.py. Each named error class from apollo.errors
is registered with an exception handler that surfaces the error as a
structured JSON response — NO FALLBACK behavior, just visible failure.
"""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from apollo.errors import (
    ApolloError,
    CoverageGradingError,
    FilterRejectedError,
    InvalidPhaseError,
    KGEntryNotFoundError,
    MalformedEquationError,
    NoMatchingConceptError,
    ParserCouldNotExtractError,
    PoolExhaustedError,
    ReviewRequiredError,
    SessionFrozenError,
)
from apollo.handlers.chat import handle_chat
from apollo.handlers.done import handle_done
from apollo.handlers.lifecycle import handle_end, handle_get_session, handle_retry
from apollo.handlers.negotiate import (
    ChallengeRequest,
    ParaphraseRequest,
    SkipRequest,
    handle_challenge,
    handle_get_trace,
    handle_paraphrase,
    handle_skip,
)
from apollo.handlers.progress import handle_get_progress
from apollo.hoot_bridge.session_init import init_session_from_hoot
from apollo.persistence.neo4j_client import Neo4jClient
from database.session import get_db_session


# ----------------------------------------------------------------------
# Neo4j client — process singleton, lazily constructed.
# Used by handlers that need to read/write the per-attempt KG subgraph.
# ----------------------------------------------------------------------

_neo4j_client_singleton: Neo4jClient | None = None


def get_neo4j_client() -> Neo4jClient:
    global _neo4j_client_singleton
    if _neo4j_client_singleton is None:
        _neo4j_client_singleton = Neo4jClient.from_env()
    return _neo4j_client_singleton


async def close_neo4j_client() -> None:
    """Close the process-wide Neo4j driver. Wire to FastAPI's shutdown event."""
    global _neo4j_client_singleton
    if _neo4j_client_singleton is not None:
        await _neo4j_client_singleton.close()
        _neo4j_client_singleton = None

router = APIRouter(prefix="/apollo", tags=["apollo"])


class FromHootRequest(BaseModel):
    student_id: str
    hoot_transcript: str
    # Hoot→Apollo handoff starts at 'intro' by default (see session_init
    # docstring). Optional so the one-click "Teach Apollo" button need not
    # send it; explicit callers may still override.
    difficulty: Literal["intro", "standard", "hard"] = "intro"


class ChatRequest(BaseModel):
    message: str


class NextRequest(BaseModel):
    difficulty: Literal["intro", "standard", "hard"]


@router.post("/sessions/from_hoot")
async def session_from_hoot(
    body: FromHootRequest,
    db: AsyncSession = Depends(get_db_session),
) -> dict:
    return await init_session_from_hoot(
        db=db,
        student_id=body.student_id,
        hoot_transcript=body.hoot_transcript,
        difficulty=body.difficulty,
    )


@router.get("/sessions/{session_id}")
async def get_session(
    session_id: int,
    db: AsyncSession = Depends(get_db_session),
    neo: Neo4jClient = Depends(get_neo4j_client),
) -> dict:
    return await handle_get_session(db=db, neo=neo, session_id=session_id)


@router.post("/sessions/{session_id}/chat")
async def chat(
    session_id: int,
    body: ChatRequest,
    db: AsyncSession = Depends(get_db_session),
    neo: Neo4jClient = Depends(get_neo4j_client),
) -> dict:
    return await handle_chat(db=db, neo=neo, session_id=session_id, message=body.message)


@router.post("/sessions/{session_id}/done")
async def done(
    session_id: int,
    db: AsyncSession = Depends(get_db_session),
    neo: Neo4jClient = Depends(get_neo4j_client),
) -> dict:
    return await handle_done(db=db, neo=neo, session_id=session_id)


@router.post("/sessions/{session_id}/retry")
async def retry(
    session_id: int,
    db: AsyncSession = Depends(get_db_session),
) -> dict:
    return await handle_retry(db=db, session_id=session_id)


@router.post("/sessions/{session_id}/next")
async def next_problem(
    session_id: int,
    body: NextRequest,
    db: AsyncSession = Depends(get_db_session),
) -> dict:
    from apollo.handlers.next import handle_next
    return await handle_next(db=db, session_id=session_id, difficulty=body.difficulty)


@router.post("/sessions/{session_id}/restart_problem")
async def restart_problem(
    session_id: int,
    db: AsyncSession = Depends(get_db_session),
    neo: Neo4jClient = Depends(get_neo4j_client),
) -> dict:
    from apollo.handlers.restart_problem import handle_restart_problem
    return await handle_restart_problem(db=db, neo=neo, session_id=session_id)


@router.post("/sessions/{session_id}/end")
async def end(
    session_id: int,
    db: AsyncSession = Depends(get_db_session),
    neo: Neo4jClient = Depends(get_neo4j_client),
) -> dict:
    return await handle_end(db=db, neo=neo, session_id=session_id)


@router.get("/progress/{student_id}")
async def progress(
    student_id: str,
    db: AsyncSession = Depends(get_db_session),
) -> dict:
    return await handle_get_progress(db=db, student_id=student_id)


# ----------------------------------------------------------------------
# P3 — Negotiable OLM. Three move endpoints + trace lookup.
# ----------------------------------------------------------------------

@router.post("/sessions/{session_id}/kg/{entry_id}/challenge")
async def negotiate_challenge(
    session_id: int,
    entry_id: str,
    body: ChallengeRequest,
    db: AsyncSession = Depends(get_db_session),
    neo: Neo4jClient = Depends(get_neo4j_client),
) -> dict:
    return await handle_challenge(
        db=db, neo=neo, session_id=session_id, entry_id=entry_id, body=body,
    )


@router.post("/sessions/{session_id}/kg/{entry_id}/paraphrase")
async def negotiate_paraphrase(
    session_id: int,
    entry_id: str,
    body: ParaphraseRequest,
    db: AsyncSession = Depends(get_db_session),
    neo: Neo4jClient = Depends(get_neo4j_client),
) -> dict:
    return await handle_paraphrase(
        db=db, neo=neo, session_id=session_id, entry_id=entry_id, body=body,
    )


@router.post("/sessions/{session_id}/kg/{entry_id}/skip")
async def negotiate_skip(
    session_id: int,
    entry_id: str,
    db: AsyncSession = Depends(get_db_session),
    neo: Neo4jClient = Depends(get_neo4j_client),
) -> dict:
    return await handle_skip(
        db=db, neo=neo, session_id=session_id, entry_id=entry_id,
    )


@router.get("/sessions/{session_id}/kg/{entry_id}/trace")
async def negotiate_trace(
    session_id: int,
    entry_id: str,
    db: AsyncSession = Depends(get_db_session),
    neo: Neo4jClient = Depends(get_neo4j_client),
) -> dict:
    return await handle_get_trace(
        db=db, neo=neo, session_id=session_id, entry_id=entry_id,
    )


# ----------------------------------------------------------------------
# Exception handlers — surface every Apollo error as a structured JSON
# response. NO FALLBACK: each error type gets its own HTTP status + code.
# ----------------------------------------------------------------------

def _err_payload(code: str, message: str, **extra: object) -> dict:
    payload = {"error_code": code, "message": message}
    payload.update(extra)
    return payload


async def parser_could_not_extract_handler(request: Request, exc: ParserCouldNotExtractError) -> JSONResponse:
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


async def no_matching_concept_handler(request: Request, exc: NoMatchingConceptError) -> JSONResponse:
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
    # ContextOverflowError lives in apollo.agent.apollo_llm; import lazily
    # to avoid a circular import in api.py's top-level module load.
    from apollo.agent.apollo_llm import ContextOverflowError
    app.add_exception_handler(ContextOverflowError, context_overflow_handler)
    app.add_exception_handler(SessionFrozenError, session_frozen_handler)
    app.add_exception_handler(KGEntryNotFoundError, kg_entry_not_found_handler)
    app.add_exception_handler(ReviewRequiredError, review_required_handler)
