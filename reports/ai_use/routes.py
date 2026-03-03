from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from .models import create_report, get_report, list_reports
from .service import build_evidence_pack, generate_report as gen_report
from backend.vendors import supabase_client as sb

router = APIRouter()


class CreateReportBody(BaseModel):
    style: str = Field("none", description="APA|MLA|IEEE|none")
    length: str = Field("brief", description="brief|full")


def _load_chat_from_supabase(chat_id: str) -> dict:
    """Load a chat transcript from Supabase chat_sessions + chat_turns."""
    session = sb.select_one("chat_sessions", {"chat_id": f"eq.{chat_id}"})
    if not session:
        raise ValueError("chat not found")
    session_id = session["id"]
    turns = sb.select("chat_turns", {
        "chat_session_id": f"eq.{session_id}",
        "order": "created_at.asc",
    })
    return {
        "chat_id": chat_id,
        "meta": session.get("meta", {}),
        "turns": turns,
    }


@router.post("/reports/ai-use/{chat_id}")
def create_ai_use_report(chat_id: str, body: CreateReportBody):
    try:
        evidence = build_evidence_pack(
            chat_id, body.style, body.length,
            chat_loader=_load_chat_from_supabase,
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
def get_ai_use_report_pdf(report_id: str):
    obj = get_report(report_id)
    if not obj:
        raise HTTPException(status_code=404, detail="Report not found")
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
def get_ai_use_report_detail(report_id: str):
    obj = get_report(report_id)
    if not obj:
        raise HTTPException(status_code=404, detail="Report not found")
    return obj


@router.get("/reports/ai-use")
def list_ai_use_reports_endpoint(limit: int = 10):
    return list_reports(limit=limit)
