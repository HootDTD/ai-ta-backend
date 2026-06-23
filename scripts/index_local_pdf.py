"""Index ONE local PDF into the LOCAL pgvector store (aita_documents + aita_chunks).

A minimal, infra-light alternative to the full teacher-upload worker path: no
Supabase Storage, no GoTrue, no job queue, no Mathpix. It extracts the PDF to
layout Items with PyMuPDF, wraps them in an ``AITAConnectorDocument`` and runs
``AITAIndexingService.prepare_for_indexing`` + ``index_from_items`` against the
``SUPABASE_DB_URL`` Postgres so the resulting ``AITADocument`` lands in
``status={'state':'ready'}`` (which is exactly what ``active_document_conditions``
requires before retrieval / Apollo provisioning can see it).

It exists for the Macro Ch.6 graph-grading probe (docs/experiments/
2026-06-22-macro-graph-grading-probe/DESIGN.md): feed a text PDF straight into a
local course so the mining + faithfulness tests have real chunks to retrieve.

HARD LOCAL GUARD: refuses to run unless ``SUPABASE_DB_URL`` points at
127.0.0.1 / localhost. It builds its OWN ``create_async_engine`` with a
``NullPool`` (one short-lived connection, mirroring bootstrap_local_db.py) and
never reuses any app-level pooled engine — so it can never index into a remote
project by accident.

Usage (after dot-sourcing scripts/load_local_env.ps1 so SUPABASE_DB_URL is set)::

    python scripts/index_local_pdf.py --pdf path/to/ch6.pdf --search-space-id 3
    python scripts/index_local_pdf.py --pdf ch6.pdf --search-space-id 3 \
        --material-kind textbook --week none
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import logging
import os
import sys
from pathlib import Path
from types import SimpleNamespace

# Make the repo root importable when run as `python scripts/index_local_pdf.py`.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from database.models import AITADocument, DocumentStatus
from indexing.connector_document import AITAConnectorDocument
from indexing.indexing_service import AITAIndexingService
from knowledge.teacher_pdf_ingestion import TeacherPDFIngestor

try:  # pragma: no cover - exercised indirectly; tests patch this symbol.
    import fitz  # type: ignore
except Exception:  # pragma: no cover
    fitz = None

log = logging.getLogger(__name__)

_LOCAL_HOSTS: tuple[str, ...] = ("127.0.0.1", "localhost")


class LocalGuardError(RuntimeError):
    """Raised when SUPABASE_DB_URL does not point at a local Postgres."""


def is_local_db_url(url: str) -> bool:
    """True iff ``url`` targets a local Postgres (127.0.0.1 / localhost)."""
    return any(host in url for host in _LOCAL_HOSTS)


def guard_local_db_url(url: str) -> str:
    """Return ``url`` unchanged if local; otherwise raise ``LocalGuardError``.

    This is the single chokepoint that keeps the indexer from ever writing to a
    remote Supabase project — it mirrors the bootstrap_local_db.py refusal.
    """
    cleaned = (url or "").strip()
    if not cleaned:
        raise LocalGuardError(
            "SUPABASE_DB_URL is not set. Point it at the LOCAL Supabase stack."
        )
    if not is_local_db_url(cleaned):
        raise LocalGuardError(
            f"Refusing to run: SUPABASE_DB_URL does not look local: {cleaned!r}\n"
            "This script only indexes into a local Postgres (127.0.0.1 / localhost)."
        )
    return cleaned


def parse_week(raw: str | None) -> int | None:
    """Map the ``--week`` CLI value to the connector-doc ``week`` field.

    ``None`` / ``"none"`` / empty -> ``None`` (permanent, non-weekly material).
    Otherwise an int week number.
    """
    if raw is None:
        return None
    text = raw.strip().lower()
    if text in ("", "none", "null"):
        return None
    return int(text)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Index one local PDF into local pgvector (status=ready)."
    )
    parser.add_argument("--pdf", required=True, help="Path to the PDF to index.")
    parser.add_argument(
        "--search-space-id",
        required=True,
        type=int,
        help="SearchSpace.id (the course) to index into.",
    )
    parser.add_argument(
        "--material-kind",
        default="textbook",
        help="Material kind (textbook/slides/homework/exams/notes/other).",
    )
    parser.add_argument(
        "--week",
        default="none",
        help="Week number, or 'none' for permanent material (default).",
    )
    return parser.parse_args(argv)


def _doc_id_for(pdf_path: Path, search_space_id: int) -> str:
    """Stable short doc id from the PDF path + course (used as item id prefix)."""
    raw = f"{pdf_path.resolve()}:{search_space_id}"
    return "local-pdf:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _native_items(pdf_path: Path, doc_id: str) -> list[SimpleNamespace]:
    """Fallback extractor: one body Item per page from raw PyMuPDF page text.

    Used only when ``TeacherPDFIngestor.ingest`` is unavailable or yields no
    items (e.g. a text PDF needing no OCR). Each Item is duck-typed to satisfy
    ``items_to_chunk_texts`` (``text``/``raw_text``/``page``/``section_path``/
    ``type``/``figure_id``/``source_pdf``/``id``).
    """
    if fitz is None:
        raise RuntimeError("PyMuPDF (fitz) is required to extract the PDF")
    items: list[SimpleNamespace] = []
    with fitz.open(str(pdf_path)) as doc:  # type: ignore[arg-type]
        for index in range(len(doc)):
            page_number = index + 1
            text = (doc[index].get_text() or "").strip()
            if not text:
                continue
            items.append(
                SimpleNamespace(
                    id=f"{doc_id}:{page_number}:0",
                    page=page_number,
                    section_path=[],
                    type="body",
                    figure_id=None,
                    text=text,
                    raw_text=text,
                    source_pdf=str(pdf_path),
                )
            )
    return items


def extract_items(pdf_path: Path, doc_id: str) -> list[SimpleNamespace]:
    """Extract the PDF to layout Items, preferring the production ingestor.

    Tries ``TeacherPDFIngestor.ingest`` (native PyMuPDF; Mathpix only fires for
    image/equation-heavy pages and is skipped silently when unconfigured). If it
    raises or returns no items, falls back to per-page raw text so a plain text
    PDF still indexes without any storage/teacher context.
    """
    try:
        result = TeacherPDFIngestor().ingest(pdf_path, doc_id=doc_id)
        items = list(result.items)
        if items:
            return items
        log.warning("TeacherPDFIngestor produced no items; using native fallback.")
    except Exception as exc:  # noqa: BLE001 - any ingestor failure -> fallback.
        log.warning("TeacherPDFIngestor.ingest failed (%s); using native fallback.", exc)
    return _native_items(pdf_path, doc_id)


def build_connector_doc(
    *,
    pdf_path: Path,
    search_space_id: int,
    material_kind: str,
    week: int | None,
    items: list[SimpleNamespace],
) -> AITAConnectorDocument:
    """Assemble the ``AITAConnectorDocument`` DTO for the indexing service."""
    source_markdown = "\n\n".join(
        (getattr(item, "text", None) or getattr(item, "raw_text", None) or "").strip()
        for item in items
    ).strip()
    if not source_markdown:
        raise ValueError(f"No extractable text in {pdf_path} — nothing to index.")
    page_count = max(
        (getattr(item, "page", None) or 0 for item in items), default=0
    ) or None
    return AITAConnectorDocument(
        title=pdf_path.stem,
        source_markdown=source_markdown,
        unique_id=_doc_id_for(pdf_path, search_space_id),
        document_type="EDUCATIONAL_FILE",
        search_space_id=search_space_id,
        material_kind=material_kind,
        page_count=page_count,
        week=week,
        metadata={"source_pdf": str(pdf_path), "source_name": pdf_path.name},
    )


async def index_pdf(
    *,
    db_url: str,
    pdf_path: Path,
    search_space_id: int,
    material_kind: str,
    week: int | None,
) -> tuple[int, int]:
    """Extract + index ``pdf_path`` into local pgvector. Returns (doc_id, chunks).

    Owns the engine lifecycle: builds a NullPool ``create_async_engine`` on
    ``db_url``, runs ``prepare_for_indexing`` + ``index_from_items`` in one
    session, asserts the doc reached ``ready``, and disposes the engine.
    """
    doc_id = _doc_id_for(pdf_path, search_space_id)
    items = extract_items(pdf_path, doc_id)
    connector_doc = build_connector_doc(
        pdf_path=pdf_path,
        search_space_id=search_space_id,
        material_kind=material_kind,
        week=week,
        items=items,
    )

    engine = create_async_engine(db_url, poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            service = AITAIndexingService(session)
            prepared = await service.prepare_for_indexing([connector_doc])
            if not prepared:
                raise RuntimeError(
                    "prepare_for_indexing returned no document — already indexed "
                    "with identical content, or a concurrent insert race."
                )
            document: AITADocument = await service.index_from_items(
                prepared[0], connector_doc, items
            )
            if not DocumentStatus.is_state(document.status, DocumentStatus.READY):
                reason = DocumentStatus.get_failure_reason(document.status)
                raise RuntimeError(
                    f"Document {document.id} did not reach 'ready' "
                    f"(status={document.status!r}, reason={reason!r})."
                )
            return int(document.id), len(items)
    finally:
        await engine.dispose()


async def run(args: argparse.Namespace) -> int:
    """Validate inputs, run the indexer, and report the result. Returns exit code."""
    db_url = guard_local_db_url(os.getenv("SUPABASE_DB_URL", ""))
    pdf_path = Path(args.pdf)
    if not pdf_path.is_file():
        raise FileNotFoundError(f"PDF not found: {pdf_path}")

    doc_id, chunk_count = await index_pdf(
        db_url=db_url,
        pdf_path=pdf_path,
        search_space_id=args.search_space_id,
        material_kind=args.material_kind,
        week=parse_week(args.week),
    )
    print(
        f"OK: indexed {pdf_path.name} -> document_id={doc_id}, "
        f"chunks={chunk_count}, status=ready, search_space_id={args.search_space_id}"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO)
    args = parse_args(argv)
    try:
        return asyncio.run(run(args))
    except (LocalGuardError, FileNotFoundError, ValueError, RuntimeError) as exc:
        log.error("%s", exc)
        return 1


if __name__ == "__main__":  # pragma: no cover - module entrypoint
    raise SystemExit(main())
