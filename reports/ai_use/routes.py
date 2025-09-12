from __future__ import annotations

from typing import Optional

import json
import os
from datetime import datetime
import importlib

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from .models import AIUseReportORM, AIUseReport, SessionLocal
from .service import build_evidence_pack, generate_report
from pydantic import BaseModel, Field


router = APIRouter()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


class CreateReportBody(BaseModel):
    style: str = Field("none", description="APA|MLA|IEEE|none")
    length: str = Field("brief", description="brief|full")


def _load_chat_from_fs(chat_id: str) -> dict:
    root = os.getenv("CHAT_STORE_DIR")
    if not root:
        raise ValueError("chat_loader not configured")
    path = os.path.join(root, f"{chat_id}.json")
    if not os.path.exists(path):
        raise ValueError("chat not found")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _resolve_chat_loader():
    """Resolve a chat loader function.

    Priority:
      1) CHAT_LOADER_FUNC env: "module.submod:funcname"
      2) CHAT_STORE_DIR env: fall back to filesystem JSON loader
    """
    func_path = os.getenv("CHAT_LOADER_FUNC")
    if func_path:
        try:
            mod_name, func_name = func_path.split(":", 1)
            mod = importlib.import_module(mod_name)
            func = getattr(mod, func_name)
            if not callable(func):
                raise TypeError("CHAT_LOADER_FUNC is not callable")
            return func
        except Exception as e:
            raise ValueError(f"failed to import CHAT_LOADER_FUNC: {e}")
    # fallback to filesystem loader if configured
    if os.getenv("CHAT_STORE_DIR"):
        return _load_chat_from_fs
    raise ValueError("chat_loader not configured")


@router.post("/reports/ai-use/{chat_id}")
def create_ai_use_report(
    chat_id: str,
    body: CreateReportBody,
    db: Session = Depends(get_db),
):
    try:
        loader = _resolve_chat_loader()
        evidence = build_evidence_pack(chat_id, body.style, body.length, chat_loader=loader)
    except ValueError as e:
        # chat not found or loader missing
        raise HTTPException(status_code=400, detail=str(e))

    # If evidence too large even after truncation (no turns remain)
    if not evidence.get("turns") and evidence.get("truncated"):
        raise HTTPException(status_code=413, detail="evidence too large")

    try:
        payload = generate_report(evidence, body.style, body.length)
    except Exception:
        # mask vendor errors
        raise HTTPException(status_code=500, detail="report generation failed")

    obj = AIUseReportORM(
        chat_id=chat_id,
        style=body.style,
        length=body.length,
        markdown=payload.get("markdown"),
        jsonld=payload.get("jsonld"),
        model_fingerprint=payload.get("model_fingerprint"),
        tool_calls=payload.get("tool_calls"),
        prompt_hashes=payload.get("prompt_hashes"),
    )
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return {
        "report_id": obj.id,
        "markdown": obj.markdown,
        "jsonld": obj.jsonld,
        "created_at": obj.created_at,
    }


@router.get("/reports/ai-use/{report_id}.pdf")
def get_ai_use_report_pdf(report_id: str, db: Session = Depends(get_db)):
    # Place this BEFORE the generic /{report_id} route so it isn't shadowed
    obj = db.get(AIUseReportORM, report_id)
    if not obj:
        raise HTTPException(status_code=404, detail="Report not found")
    try:
        # Lazy import to avoid hard dependency at app startup
        from .pdf import render_pdf_from_markdown  # type: ignore

        meta = {
            "title": "AI-use Report",
            "chat_id": obj.chat_id,
            "created_at": obj.created_at.isoformat() if obj.created_at else "",
            "truncated": bool((obj.jsonld or {}).get("evidence", {}).get("truncated")) if obj.jsonld else False,
        }
        pdf_bytes = render_pdf_from_markdown(obj.markdown or "", metadata=meta)
    except Exception:
        raise HTTPException(status_code=500, detail="failed to render PDF")

    from fastapi.responses import Response

    fname = f"ai-use-report-{report_id}.pdf"
    headers = {"Content-Disposition": f"attachment; filename=\"{fname}\""}
    return Response(content=pdf_bytes, media_type="application/pdf", headers=headers)


@router.get("/reports/ai-use/{report_id}")
def get_ai_use_report(report_id: str, db: Session = Depends(get_db)):
    obj = db.get(AIUseReportORM, report_id)
    if not obj:
        raise HTTPException(status_code=404, detail="Report not found")
    return AIUseReport.model_validate(obj)


@router.get("/reports/ai-use")
def list_ai_use_reports(limit: int = 10, db: Session = Depends(get_db)):
    q = db.query(AIUseReportORM).order_by(AIUseReportORM.created_at.desc()).limit(max(1, min(limit, 100)))
    rows = q.all()
    return [
        {
            "id": r.id,
            "chat_id": r.chat_id,
            "created_at": r.created_at,
            "style": r.style,
            "length": r.length,
        }
        for r in rows
    ]


 
