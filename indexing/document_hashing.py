from __future__ import annotations

"""SHA-256 hashing for document deduplication.

Ported from SurfSense's indexing_pipeline/document_hashing.py with no logic changes.
"""

import hashlib

from .connector_document import AITAConnectorDocument


def compute_unique_identifier_hash(doc: AITAConnectorDocument) -> str:
    """Return a stable SHA-256 hash identifying a document by its source identity.

    Same document re-uploaded → same hash → deduplicated.
    Different classes with same document → different hashes (search_space_id scoped).
    """
    combined = f"{doc.document_type}:{doc.unique_id}:{doc.search_space_id}"
    return hashlib.sha256(combined.encode("utf-8")).hexdigest()


def compute_content_hash(doc: AITAConnectorDocument) -> str:
    """Return a SHA-256 hash of the document content scoped to its search space.

    Detects when content changes (e.g. teacher re-uploads revised slides).
    """
    combined = f"{doc.search_space_id}:{doc.source_markdown}"
    return hashlib.sha256(combined.encode("utf-8")).hexdigest()
