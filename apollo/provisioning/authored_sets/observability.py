"""Ingestion observability for authored sets (WU-AAS, lane B2.4 / G4.4).

The authored-set ingestion path (``api._run_set_background`` -> ``index_authored_doc``
-> ``run_authored_set_provisioning``) historically wrote NOTHING to
``apollo_ingest_runs`` / ``apollo_ingest_errors`` and persisted no per-page OCR
text, so the S2 ingestion audit ran on thin/absent inputs. This module owns the
observability writes for that path:

  * ``start_ingest_run`` — one ``apollo_ingest_runs`` row per authored-set ingestion,
    opened ``status='running'`` with ``started_at`` set. Metered LLM usage accrues
    on the SAME row (``MeteredChat`` mutates it in place), so the run's
    ``llm_calls`` / token / cost aggregates are populated for free.
  * ``persist_page_evidence`` — one ``apollo_ingest_page_evidence`` row per source
    page, carrying the recognized OCR text + self-reported confidence + extraction
    mode + the ``verify_path_fired`` low-confidence flag the S2 contract reads.
  * ``finalize_ingest_run`` — stamp the terminal status + timing + the
    scraped/promoted/rejected counts from the provisioning report.
  * ``record_ingest_error`` — one ``apollo_ingest_errors`` row per stage failure.

Every write flushes but does NOT commit — the caller (``_run_set_background``) owns
the transaction boundary, matching the rest of the authored-set path.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from apollo.persistence.models import IngestError, IngestPageEvidence, IngestRun

_LOG = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_CONF_THRESHOLD",
    "start_ingest_run",
    "persist_page_evidence",
    "finalize_ingest_run",
    "record_ingest_error",
    "page_ocr_text",
]

# Mirrors the orchestrator's per-candidate confidence threshold: a page at or below
# this self-reported OCR confidence is flagged as verify-path (OCR-suspect).
DEFAULT_CONF_THRESHOLD = 0.6


def page_ocr_text(page: Any) -> str:
    """The recognized text for one ingestion page, LaTeX appended to plain text.

    Duck-typed against ``knowledge.teacher_pdf_ingestion.NormalizedPage``
    (``plain_text`` / ``latex_text``); an object exposing neither yields ''."""
    plain = (getattr(page, "plain_text", "") or "").strip()
    latex = (getattr(page, "latex_text", "") or "").strip()
    parts = [p for p in (plain, latex) if p]
    return "\n\n".join(parts)


async def start_ingest_run(
    db: AsyncSession,
    *,
    search_space_id: int,
    document_id: int,
    content_hash: str | None = None,
) -> IngestRun:
    """Open an ``apollo_ingest_runs`` row (``status='running'``, ``started_at`` now)
    for one authored-set ingestion and flush so ``run.id`` is assigned."""
    run = IngestRun(
        search_space_id=search_space_id,
        document_id=document_id,
        content_hash=content_hash,
        status="running",
        started_at=datetime.now(UTC),
    )
    db.add(run)
    await db.flush()
    _LOG.info(
        "authored_ingest_run_started",
        extra={
            "event": "authored_ingest_run_started",
            "ingest_run_id": int(run.id),
            "search_space_id": search_space_id,
            "document_id": document_id,
        },
    )
    return run


async def persist_page_evidence(
    db: AsyncSession,
    *,
    ingest_run: IngestRun,
    search_space_id: int,
    document_id: int | None,
    role: str,
    pages: Sequence[Any],
    conf_threshold: float = DEFAULT_CONF_THRESHOLD,
) -> int:
    """Write one ``apollo_ingest_page_evidence`` row per ingestion page.

    ``pages`` are ``NormalizedPage``-shaped (``page_number`` / ``plain_text`` /
    ``latex_text`` / ``ocr_confidence`` / ``extraction_mode``). ``verify_path_fired``
    is set when a page's confidence is present and at or below ``conf_threshold``
    (an OCR-suspect page the S2 contract expects to route through verification).
    Returns the number of evidence rows written. Flushes; the caller commits."""
    written = 0
    for page in pages:
        confidence = getattr(page, "ocr_confidence", None)
        conf_val = float(confidence) if confidence is not None else None
        db.add(
            IngestPageEvidence(
                ingest_run_id=int(ingest_run.id),
                search_space_id=search_space_id,
                document_id=document_id,
                role=role,
                page_number=getattr(page, "page_number", None),
                ocr_text=page_ocr_text(page),
                ocr_confidence=conf_val,
                extraction_mode=getattr(page, "extraction_mode", None),
                verify_path_fired=(conf_val is not None and conf_val <= conf_threshold),
            )
        )
        written += 1
    await db.flush()
    _LOG.info(
        "authored_ingest_page_evidence",
        extra={
            "event": "authored_ingest_page_evidence",
            "ingest_run_id": int(ingest_run.id),
            "document_id": document_id,
            "role": role,
            "n_pages": written,
        },
    )
    return written


async def finalize_ingest_run(
    db: AsyncSession,
    *,
    ingest_run: IngestRun,
    status: str,
    n_pages: int | None = None,
    n_questions_scraped: int | None = None,
    n_promoted: int | None = None,
    n_rejected: int | None = None,
) -> None:
    """Stamp the terminal ``status`` + ``finished_at`` + any provided counts on the
    run row. Only non-None counts are written (a failed run may know none). Flushes;
    the caller commits."""
    ingest_run.status = status  # type: ignore[assignment]
    ingest_run.finished_at = datetime.now(UTC)  # type: ignore[assignment]
    if n_pages is not None:
        ingest_run.n_pages = n_pages  # type: ignore[assignment]
    if n_questions_scraped is not None:
        ingest_run.n_questions_scraped = n_questions_scraped  # type: ignore[assignment]
    if n_promoted is not None:
        ingest_run.n_promoted = n_promoted  # type: ignore[assignment]
    if n_rejected is not None:
        ingest_run.n_rejected = n_rejected  # type: ignore[assignment]
    await db.flush()
    _LOG.info(
        "authored_ingest_run_finalized",
        extra={
            "event": "authored_ingest_run_finalized",
            "ingest_run_id": int(ingest_run.id),
            "status": status,
            "n_pages": ingest_run.n_pages,
            "n_promoted": ingest_run.n_promoted,
            "n_rejected": ingest_run.n_rejected,
            "dedup_pressure": ingest_run.dedup_pressure,
        },
    )


async def record_ingest_error(
    db: AsyncSession,
    *,
    search_space_id: int,
    ingest_run: IngestRun | None,
    stage: str,
    exc: BaseException,
    context: dict[str, Any] | None = None,
) -> None:
    """Write one ``apollo_ingest_errors`` row for a stage failure.

    ``error_class`` is the exception's type name; ``context`` carries a bounded,
    PII-free summary (never the raw OCR/LLM bodies). ``ingest_run`` may be None if
    the run row was never opened (a failure before ``start_ingest_run``). Flushes;
    the caller commits."""
    ctx = dict(context or {})
    ctx.setdefault("message", str(exc))
    db.add(
        IngestError(
            ingest_run_id=int(ingest_run.id) if ingest_run is not None else None,
            search_space_id=search_space_id,
            stage=stage,
            error_class=type(exc).__name__,
            context=ctx,
        )
    )
    await db.flush()
    _LOG.info(
        "authored_ingest_error_recorded",
        extra={
            "event": "authored_ingest_error_recorded",
            "ingest_run_id": int(ingest_run.id) if ingest_run is not None else None,
            "search_space_id": search_space_id,
            "stage": stage,
            "error_class": type(exc).__name__,
        },
    )
