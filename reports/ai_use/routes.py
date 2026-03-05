from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from backend.auth import resolve_auth_context
from backend.chats.service import get_chat_session_for_user, serialize_chat_session
from backend.database.session import get_async_session, run_async
from .models import create_report, get_report, list_reports
from .service import build_evidence_pack, generate_report as gen_report

router = APIRouter()


class CreateReportBody(BaseModel):
    style: str = Field("none", description="APA|MLA|IEEE|none")
    length: str = Field("brief", description="brief|full")


def _user_owns_chat(user_id: str, chat_id: str) -> bool:
    async def _run() -> bool:
        async with get_async_session() as db_session:
            session = await get_chat_session_for_user(
                db_session,
                chat_id=chat_id,
                user_id=user_id,
            )
            return session is not None

    return bool(run_async(_run()))


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


def _require_owned_report(report_id: str, *, user_id: str) -> dict:
    obj = get_report(report_id)
    if not obj:
        raise HTTPException(status_code=404, detail="Report not found")
    chat_id = str(obj.get("chat_id") or "").strip()
    if not chat_id:
        raise HTTPException(status_code=404, detail="Report not found")
    if not _user_owns_chat(user_id, chat_id):
        raise HTTPException(status_code=403, detail="Forbidden")
    return obj


@router.post("/reports/ai-use/{chat_id}")
def create_ai_use_report(chat_id: str, body: CreateReportBody, request: Request):
    auth = resolve_auth_context(request)
    if not _user_owns_chat(auth.user_id, chat_id):
        raise HTTPException(status_code=403, detail="Forbidden")

    try:
        evidence = build_evidence_pack(
            chat_id, body.style, body.length,
            chat_loader=lambda cid: _load_chat_for_user(cid, auth.user_id),
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    if not evidence.get("turns") and evidence.get("truncated"):
        raise HTTPException(status_code=413, detail="evidence too large")

    try:
        payload = gen_report(evidence, body.style, body.length)
    except Exception:
        raise HTTPException(status_code=500, detail="report generation failed")

    obj = create_report(
        chat_id=chat_id,
        style=body.style,
        length=body.length,
        markdown=payload.get("markdown"),
        jsonld=payload.get("jsonld"),
        model_fingerprint=payload.get("model_fingerprint"),
        tool_calls=payload.get("tool_calls"),
        prompt_hashes=payload.get("prompt_hashes"),
    )
    return {
        "report_id": obj["id"],
        "markdown": obj.get("markdown"),
        "jsonld": obj.get("jsonld"),
        "created_at": obj.get("created_at"),
    }


@router.get("/reports/ai-use/{report_id}.pdf")
def get_ai_use_report_pdf(report_id: str, request: Request):
    auth = resolve_auth_context(request)
    obj = _require_owned_report(report_id, user_id=auth.user_id)
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
        raise HTTPException(status_code=500, detail="failed to render PDF")

    from fastapi.responses import Response

    fname = f"ai-use-report-{report_id}.pdf"
    headers = {"Content-Disposition": f"attachment; filename=\"{fname}\""}
    return Response(content=pdf_bytes, media_type="application/pdf", headers=headers)


@router.get("/reports/ai-use/{report_id}")
def get_ai_use_report_detail(report_id: str, request: Request):
    auth = resolve_auth_context(request)
    return _require_owned_report(report_id, user_id=auth.user_id)


@router.get("/reports/ai-use")
def list_ai_use_reports_endpoint(request: Request, limit: int = 10):
    auth = resolve_auth_context(request)
    rows = list_reports(limit=limit)
    out = []
    seen: dict[str, bool] = {}
    for row in rows:
        chat_id = str((row or {}).get("chat_id") or "").strip()
        if not chat_id:
            continue
        allowed = seen.get(chat_id)
        if allowed is None:
            allowed = _user_owns_chat(auth.user_id, chat_id)
            seen[chat_id] = allowed
        if allowed:
            out.append(row)
    return out
