"""Index authored problem/solution PDFs into hidden AITA documents (WU-AAS)."""

from __future__ import annotations

import asyncio
import logging
import shutil
import tempfile
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import Document, DocumentStatus
from database.session import get_async_session
from indexing.checkpoint_indexer import (
    build_doc_content,
    embed_and_persist_chunks,
    finalize_document,
)
from indexing.connector_document import AITAConnectorDocument
from indexing.document_chunker import items_to_chunk_texts
from indexing.document_embedder import embed_text
from indexing.document_hashing import (
    compute_content_hash,
    compute_unique_identifier_hash,
)
from indexing.indexing_service import AITAIndexingService

_LOG = logging.getLogger(__name__)

_HIDDEN_STATUS = DocumentStatus.PENDING


def _run_ingest(pdf_path: Path, *, doc_id: str):  # pragma: no cover - real PyMuPDF/OCR I/O
    """Run PyMuPDF-bound ingestion behind a test seam."""
    from knowledge.teacher_pdf_ingestion import TeacherPDFIngestor
    from ocr import get_ocr_provider_from_env

    ingestor = TeacherPDFIngestor(mathpix_provider=get_ocr_provider_from_env())
    return ingestor.ingest(pdf_path, doc_id=doc_id, upload_page_asset=None)


async def index_authored_doc(
    db: AsyncSession,
    *,
    search_space_id: int,
    file_bytes: bytes,
    title: str,
    set_index: int,
    role: str,
    page_sink: list | None = None,
) -> int:
    """Index raw authored PDF bytes and return the hidden ``Document.id``.

    The caller owns the outer transaction after this function returns. This path
    deliberately reuses only the indexing core, not the weekly upload wrapper:
    no Upload row, no supersede, no week activation, and no generic Apollo
    provisioning enqueue.

    When ``page_sink`` is provided, the transient per-page OCR result
    (``ingestion.pages`` — ``NormalizedPage`` objects carrying text + confidence +
    extraction mode) is appended to it so the caller can persist page-level OCR
    evidence (WU-AAS observability). Left ``None`` (the default), behavior is
    byte-identical to before — the ingest metadata still records per-page
    confidence via ``_page_debug``.
    """
    if role not in {"problem", "solution"}:
        raise ValueError("authored indexer: role must be 'problem' or 'solution'")

    work_dir = Path(tempfile.mkdtemp(prefix="authored_idx_"))
    pdf_path = work_dir / "document.pdf"
    pdf_path.write_bytes(file_bytes)

    try:
        doc_id = _authored_doc_id(search_space_id=search_space_id, set_index=set_index, role=role)
        # PyMuPDF extraction + per-page OCR (OpenAI vision calls) is synchronous
        # and slow; offload it so a multi-page handwritten PDF never stalls the
        # event loop serving concurrent tutoring requests.
        ingestion = await asyncio.to_thread(_run_ingest, pdf_path, doc_id=doc_id)
        if page_sink is not None:
            # Capture the transient per-page OCR pass for the caller's evidence
            # write BEFORE the no-items guard, so a page-bearing-but-chunkless doc
            # still surfaces its OCR text in the failure path.
            page_sink.extend(getattr(ingestion, "pages", None) or [])
        if not ingestion.items:
            raise ValueError(f"authored indexer: no chunks produced from {role} PDF")

        connector_doc = _connector_document(
            search_space_id=search_space_id,
            title=title,
            set_index=set_index,
            role=role,
            ingestion=ingestion,
        )
        service = AITAIndexingService(db)
        docs = await service.prepare_for_indexing([connector_doc])
        if not docs:
            # ``prepare_for_indexing`` returns [] when the doc is deduped away.
            # For authored sets this happens on a re-upload of identical PDF bytes:
            # ``_next_set_index`` always mints a fresh ``set_index`` (so a new
            # ``unique_id``), but the content_hash is set-index-independent, so the
            # service skips it as a content duplicate of the doc indexed by the
            # earlier set. Reuse that already-indexed, content-identical doc —
            # matching by unique_id first, then by content — instead of failing.
            existing = await _find_existing_indexed_doc(db, connector_doc)
            if existing is None:
                raise RuntimeError("authored indexer: prepare_for_indexing returned no doc")
            existing.status = _HIDDEN_STATUS
            await db.flush()
            return int(existing.id)

        document_id = int(docs[0].id)

        # The checkpointed chunk writer opens its own sessions, so the document
        # row must be committed and visible before chunk batches are persisted.
        await db.commit()

        chunk_pairs = items_to_chunk_texts(ingestion.items)
        if not chunk_pairs:
            raise ValueError(f"authored indexer: no chunk texts from {role} items")

        await embed_and_persist_chunks(
            session_factory=get_async_session,
            document_id=document_id,
            chunk_pairs=chunk_pairs,
            after_page=0,
        )

        doc_content = build_doc_content(chunk_pairs, fallback_title=title)
        await finalize_document(
            db,
            document_id=document_id,
            chunk_pairs=chunk_pairs,
            doc_content=doc_content,
            doc_embedding=embed_text(doc_content),
            page_count=ingestion.page_count,
        )

        doc = await db.get(Document, document_id)
        if doc is None:
            raise RuntimeError(f"authored indexer: indexed document {document_id} disappeared")
        doc.status = _HIDDEN_STATUS
        await db.flush()
        _LOG.info("Indexed authored %s document %s for set %s", role, document_id, set_index)
        return document_id
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


def _authored_doc_id(*, search_space_id: int, set_index: int, role: str) -> str:
    return f"authored:{search_space_id}:{set_index}:{role}"


def _connector_document(
    *,
    search_space_id: int,
    title: str,
    set_index: int,
    role: str,
    ingestion,
) -> AITAConnectorDocument:
    return AITAConnectorDocument(
        title=title,
        source_markdown=ingestion.source_markdown or title,
        unique_id=f"authored-set:{search_space_id}:{set_index}:{role}",
        document_type="EDUCATIONAL_FILE",
        search_space_id=search_space_id,
        material_kind="other",
        should_summarize=False,
        page_count=ingestion.page_count,
        week=None,
        metadata={
            "authored_role": role,
            "authored_set_index": set_index,
            "ocr_provider": ingestion.ocr_provider,
            "ocr_summary": ingestion.ocr_summary,
            "warning_count": ingestion.warning_count,
            "artifact_manifest": ingestion.artifact_manifest,
            # Per-page OCR confidence — the signal the authored-set verification
            # path reads via ``chunk_ocr_confidence`` to cross-check low-confidence
            # (e.g. handwritten) extractions. Mirrors the weekly ingestor's shape.
            "page_debug": _page_debug(ingestion),
        },
    )


def _page_debug(ingestion) -> list[dict]:
    """Per-page OCR debug entries (page number + confidence + extraction mode)."""
    return [
        {
            "page": page.page_number,
            "ocr_confidence": page.ocr_confidence,
            "extraction_mode": getattr(page, "extraction_mode", None),
        }
        for page in getattr(ingestion, "pages", None) or []
    ]


async def _find_existing_indexed_doc(
    db: AsyncSession,
    connector_doc: AITAConnectorDocument,
) -> Document | None:
    """Locate an already-indexed doc this connector deduped against.

    Tries source identity (``unique_identifier_hash``) first, then content
    identity (``content_hash``) — the latter is what a fresh-``set_index``
    re-upload of identical bytes collides on.
    """
    unique_hash = compute_unique_identifier_hash(connector_doc)
    result = await db.execute(
        select(Document).where(Document.unique_identifier_hash == unique_hash)
    )
    by_unique = result.scalar_one_or_none()
    if by_unique is not None:
        return by_unique

    content_hash = compute_content_hash(connector_doc)
    result = await db.execute(
        select(Document)
        .where(Document.content_hash == content_hash)
        .order_by(Document.id.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()
