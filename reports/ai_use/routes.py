from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field

from auth import resolve_auth_context
from chats.service import get_chat_session_for_user, serialize_chat_session
from database.models import ChatSession
from database.session import get_async_session, run_async

from .models import AIUsageReport, create_report, get_report_for_user
from .service import build_evidence_pack
from .service import generate_report as gen_report

router = APIRouter()


class CreateReportBody(BaseModel):
    style: str = Field("none", description="APA|MLA|IEEE|none")
    length: str = Field("brief", description="brief|full")


def _load_owned_chat_session(chat_id: str, user_id: str) -> ChatSession | None:
    """Return the caller's own ``ChatSession`` row for ``chat_id``, or ``None``.

    This is the ONLY source of truth for a report's course scope:
    ``create_ai_use_report`` reads ``course_id`` off the returned row, never
    from client input. ``user_id`` must already be the trusted, authenticated
    caller id.
    """

    async def _run() -> ChatSession | None:
        async with get_async_session() as db_session:
            return await get_chat_session_for_user(
                db_session,
                chat_id=chat_id,
                user_id=user_id,
            )

    return run_async(_run())


def _load_chat_for_user(chat_id: str, user_id: str) -> dict:
    async def _run() -> dict:
        async with get_async_session() as db_session:
            return await serialize_chat_session(
                db_session,
                chat_id=chat_id,
                user_id=user_id,
            )

    try:
        return run_async(_run())
    except ValueError as exc:
        raise ValueError("chat not found") from exc


def _serialize_report(row: AIUsageReport) -> dict:
    return {
        "id": row.id,
        "chat_id": row.chat_id,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "style": row.style,
        "length": row.length,
        "markdown": row.markdown,
        "jsonld": row.jsonld,
        "model_fingerprint": row.model_fingerprint,
        "tool_calls": row.tool_calls,
        "prompt_hashes": list(row.prompt_hashes or []),
    }


def _get_owned_report(report_id: str, *, user_id: str) -> AIUsageReport:
    """Owner-scoped read: the query itself is filtered by user_id, so a
    report belonging to someone else 404s exactly like a missing one."""

    async def _run() -> AIUsageReport | None:
        async with get_async_session() as db_session:
            return await get_report_for_user(db_session, report_id=report_id, user_id=user_id)

    row = run_async(_run())
    if row is None:
        raise HTTPException(status_code=404, detail="Report not found")
    return row


@router.post("/reports/ai-use/{chat_id}")
def create_ai_use_report(chat_id: str, body: CreateReportBody, request: Request):
    auth = resolve_auth_context(request)
    session = _load_owned_chat_session(chat_id, auth.user_id)
    if session is None:
        raise HTTPException(status_code=403, detail="Forbidden")

    try:
        evidence = build_evidence_pack(
            chat_id, body.style, body.length,
            chat_loader=lambda cid: _load_chat_for_user(cid, auth.user_id),
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    if not evidence.get("turns") and evidence.get("truncated"):
        raise HTTPException(status_code=413, detail="evidence too large")

    try:
        payload = gen_report(evidence, body.style, body.length)
    except Exception:
        raise HTTPException(status_code=500, detail="report generation failed") from None

    async def _persist() -> AIUsageReport:
        async with get_async_session() as db_session:
            return await create_report(
                db_session,
                user_id=auth.user_id,
                course_id=session.course_id,
                chat_id=chat_id,
                style=body.style,
                length=body.length,
                markdown=payload.get("markdown"),
                jsonld=payload.get("jsonld"),
                model_fingerprint=payload.get("model_fingerprint"),
                tool_calls=payload.get("tool_calls"),
                prompt_hashes=payload.get("prompt_hashes"),
            )

    row = run_async(_persist())
    return {
        "report_id": row.id,
        "markdown": row.markdown,
        "jsonld": row.jsonld,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


@router.get("/reports/ai-use/{report_id}.pdf")
def get_ai_use_report_pdf(report_id: str, request: Request):
    auth = resolve_auth_context(request)
    obj = _serialize_report(_get_owned_report(report_id, user_id=auth.user_id))
    try:
        from .pdf import render_pdf_from_markdown  # type: ignore

        meta = {
            "title": "AI-use Report",
            "chat_id": obj.get("chat_id", ""),
            "created_at": obj.get("created_at", ""),
            "truncated": bool((obj.get("jsonld") or {}).get("evidence", {}).get("truncated")),
        }
        pdf_bytes = render_pdf_from_markdown(obj.get("markdown") or "", metadata=meta)
    except Exception:
        raise HTTPException(status_code=500, detail="failed to render PDF") from None

    fname = f"ai-use-report-{report_id}.pdf"
    headers = {"Content-Disposition": f"attachment; filename=\"{fname}\""}
    return Response(content=pdf_bytes, media_type="application/pdf", headers=headers)


@router.get("/reports/ai-use/{report_id}")
def get_ai_use_report_detail(report_id: str, request: Request):
    auth = resolve_auth_context(request)
    return _serialize_report(_get_owned_report(report_id, user_id=auth.user_id))
