from __future__ import annotations

"""AI-TA indexing service — orchestrates the full document ingestion pipeline.

Ported from SurfSense's IndexingPipelineService with AI-TA adaptations:
- index_from_items() accepts pre-extracted Item objects from layout_multimodal_embedder
  instead of calling a generic text chunker on raw markdown.
- No LLM summarization step (PDF text is used directly).
- Preserves page_number, section_path, chunk_type on each Chunk for citations.
"""

from __future__ import annotations

import contextlib
import logging
from datetime import UTC, datetime

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..database.models import AITAChunk, AITADocument, DocumentStatus
from .connector_document import AITAConnectorDocument
from .document_chunker import items_to_chunk_texts
from .document_embedder import embed_text
from .document_hashing import compute_content_hash, compute_unique_identifier_hash
from .document_persistence import attach_chunks_to_document, rollback_and_persist_failure

log = logging.getLogger(__name__)


class AITAIndexingService:
    """Pipeline for indexing educational documents into pgvector.

    Usage:
        service = AITAIndexingService(session)
        docs = await service.prepare_for_indexing([connector_doc])
        for doc in docs:
            await service.index_from_items(doc, connector_doc, items)
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def prepare_for_indexing(
        self,
        connector_docs: list[AITAConnectorDocument],
    ) -> list[AITADocument]:
        """Persist new document records and detect changes.

        Returns only those documents that need (re-)indexing.
        Skips duplicates (same unique_identifier_hash + same content).
        Ported from SurfSense's prepare_for_indexing with race-condition handling.
        """
        documents: list[AITADocument] = []
        seen_hashes: set[str] = set()

        for connector_doc in connector_docs:
            try:
                unique_id_hash = compute_unique_identifier_hash(connector_doc)
                content_hash = compute_content_hash(connector_doc)

                if unique_id_hash in seen_hashes:
                    continue
                seen_hashes.add(unique_id_hash)

                # Check if document already exists by unique identity
                result = await self.session.execute(
                    select(AITADocument).filter(
                        AITADocument.unique_identifier_hash == unique_id_hash
                    )
                )
                existing = result.scalars().first()

                if existing is not None:
                    if existing.content_hash == content_hash:
                        # Content unchanged — just ensure it's in ready state
                        if not DocumentStatus.is_state(existing.status, DocumentStatus.READY):
                            existing.status = DocumentStatus.pending()
                            existing.updated_at = datetime.now(UTC)
                            documents.append(existing)
                        continue

                    # Content changed — mark for re-indexing
                    existing.title = connector_doc.title
                    existing.content_hash = content_hash
                    existing.source_markdown = connector_doc.source_markdown
                    existing.document_metadata = connector_doc.metadata
                    existing.material_kind = connector_doc.material_kind
                    existing.week = connector_doc.week
                    existing.updated_at = datetime.now(UTC)
                    existing.status = DocumentStatus.pending()
                    documents.append(existing)
                    log.info("Document '%s' content changed, re-queued.", connector_doc.title)
                    continue

                # Check for content duplicate in this search space (different source, same content)
                dup = await self.session.execute(
                    select(AITADocument).filter(
                        AITADocument.content_hash == content_hash
                    )
                )
                if dup.scalars().first() is not None:
                    log.debug("Skipping duplicate content for '%s'.", connector_doc.title)
                    continue

                # New document
                document = AITADocument(
                    title=connector_doc.title,
                    document_type=connector_doc.document_type,
                    material_kind=connector_doc.material_kind,
                    content="Pending...",
                    source_markdown=connector_doc.source_markdown,
                    content_hash=content_hash,
                    unique_identifier_hash=unique_id_hash,
                    document_metadata=connector_doc.metadata,
                    page_count=connector_doc.page_count,
                    week=connector_doc.week,
                    search_space_id=connector_doc.search_space_id,
                    updated_at=datetime.now(UTC),
                    status=DocumentStatus.pending(),
                )
                self.session.add(document)
                documents.append(document)
                log.info("Queued new document '%s'.", connector_doc.title)

            except Exception as e:
                log.error("Error preparing document '%s': %s", connector_doc.title, e)

        try:
            await self.session.commit()
            return documents
        except IntegrityError:
            # Race condition: concurrent worker committed same hash between our check and INSERT
            log.warning("Race condition on document insert, rolling back.")
            await self.session.rollback()
            return []
        except Exception as e:
            log.error("Batch prepare failed: %s", e)
            await self.session.rollback()
            return []

    async def index_from_items(
        self,
        document: AITADocument,
        connector_doc: AITAConnectorDocument,
        items: list,
    ) -> AITADocument:
        """Index pre-extracted Item objects from layout_multimodal_embedder.py.

        Each Item becomes one AITAChunk preserving page_number, section_path,
        and chunk_type for accurate citation markers.

        Args:
            document: The AITADocument record (status will be updated)
            connector_doc: The source DTO (for metadata)
            items: List of Item objects from layout_multimodal_embedder.py
        """
        try:
            document.status = DocumentStatus.processing()
            await self.session.commit()

            chunk_pairs = items_to_chunk_texts(items)
            if not chunk_pairs:
                raise ValueError("No chunk texts extracted from items — document may be empty.")

            # Document-level content: concatenate body/heading text, capped for embedding
            body_texts = [
                text for text, meta in chunk_pairs
                if meta.get("chunk_type") in ("body", "heading", "ocr", None)
            ]
            doc_content = " ".join(body_texts)[:2000] or connector_doc.title

            # Document-level embedding (coarse retrieval)
            doc_embedding = embed_text(doc_content)

            # Delete any stale chunks from a previous indexing pass
            await self.session.execute(
                delete(AITAChunk).where(AITAChunk.document_id == document.id)
            )

            # Create chunk objects
            chunks = []
            for text, meta in chunk_pairs:
                chunk_embedding = embed_text(text)
                chunks.append(AITAChunk(
                    content=text,
                    embedding=chunk_embedding,
                    page_number=meta.get("page_number"),
                    section_path=meta.get("section_path") or None,
                    chunk_type=meta.get("chunk_type") or "body",
                    figure_id=meta.get("figure_id"),
                ))

            document.content = doc_content
            document.embedding = doc_embedding
            document.page_count = connector_doc.page_count or (
                max((meta.get("page_number") or 0) for _, meta in chunk_pairs) or None
            )
            attach_chunks_to_document(document, chunks)
            document.updated_at = datetime.now(UTC)
            document.status = DocumentStatus.ready()
            await self.session.commit()
            log.info(
                "Indexed document '%s': %d chunks.", document.title, len(chunks)
            )

        except Exception as e:
            log.error("Indexing failed for document '%s': %s", document.title, e)
            await rollback_and_persist_failure(self.session, document, str(e)[:500])

        with contextlib.suppress(Exception):
            await self.session.refresh(document)

        return document
