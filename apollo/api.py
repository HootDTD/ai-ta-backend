"""Apollo FastAPI router. Endpoints stubbed to 501 until implementations land.

Mounted at /apollo in server.py. Each named error class from apollo.errors
is registered with an exception handler that surfaces the error as a
structured JSON response — NO FALLBACK behavior, just visible failure.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from apollo.errors import (
    ApolloError,
    FilterRejectedError,
    MalformedEquationError,
    NoMatchingConceptError,
    ParserCouldNotExtractError,
    PoolExhaustedError,
    SessionFrozenError,
)
from apollo.handlers.chat import handle_chat
from apollo.handlers.done import handle_done
from apollo.hoot_bridge.session_init import init_session_from_hoot
from database.session import get_db_session

router = APIRouter(prefix="/apollo", tags=["apollo"])


class FromHootRequest(BaseModel):
    student_id: str
    hoot_transcript: str


class ChatRequest(BaseModel):
    message: str


@router.post("/sessions/from_hoot")
async def session_from_hoot(
    body: FromHootRequest,
    db: AsyncSession = Depends(get_db_session),
) -> dict:
    return await init_session_from_hoot(
        db=db,
        student_id=body.student_id,
        hoot_transcript=body.hoot_transcript,
    )


@router.get("/sessions/{session_id}")
async def get_session(session_id: int) -> dict:
    raise HTTPException(status_code=501, detail="not implemented")


@router.post("/sessions/{session_id}/chat")
async def chat(
    session_id: int,
    body: ChatRequest,
    db: AsyncSession = Depends(get_db_session),
) -> dict:
    return await handle_chat(db=db, session_id=session_id, message=body.message)


@router.post("/sessions/{session_id}/done")
async def done(
    session_id: int,
    db: AsyncSession = Depends(get_db_session),
) -> dict:
    return await handle_done(db=db, session_id=session_id)


@router.post("/sessions/{session_id}/retry")
async def retry(session_id: int) -> dict:
    raise HTTPException(status_code=501, detail="not implemented")


@router.post("/sessions/{session_id}/end")
async def end(session_id: int) -> dict:
    raise HTTPException(status_code=501, detail="not implemented")


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


def register_exception_handlers(app) -> None:
    """Register all Apollo exception handlers onto the FastAPI app."""
    app.add_exception_handler(ParserCouldNotExtractError, parser_could_not_extract_handler)
    app.add_exception_handler(FilterRejectedError, filter_rejected_handler)
    app.add_exception_handler(MalformedEquationError, malformed_equation_handler)
    app.add_exception_handler(NoMatchingConceptError, no_matching_concept_handler)
    app.add_exception_handler(PoolExhaustedError, pool_exhausted_handler)
    app.add_exception_handler(SessionFrozenError, session_frozen_handler)
