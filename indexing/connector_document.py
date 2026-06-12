from __future__ import annotations

"""Data transfer object for educational documents entering the indexing pipeline.

Ported from SurfSense's ConnectorDocument with AI-TA-specific fields:
  - material_kind: textbook / slides / homework / exams / notes / other
  - page_count: page count for display
  - week: teacher weekly upload week number
Removed: should_use_code_chunker, created_by_id (not needed for AI-TA)
"""

from pydantic import BaseModel, Field, field_validator

from .text_sanitization import sanitize_jsonable, strip_nul

VALID_MATERIAL_KINDS = frozenset(
    {"textbook", "slides", "homework", "exams", "notes", "other"}
)


class AITAConnectorDocument(BaseModel):
    """Canonical DTO produced by AI-TA's material upload pipeline."""

    title: str
    source_markdown: str
    unique_id: str  # SHA-256 of (source_pdf_path + search_space_id) or similar stable ID
    document_type: str = "EDUCATIONAL_FILE"

    # Which class/course this document belongs to (SearchSpace.id)
    search_space_id: int = Field(gt=0)

    # Educational material kind — drives retrieval store-bias weights
    material_kind: str = "other"

    # Optional: suppress summarization (PDF text is used directly as document content)
    should_summarize: bool = False

    # PDF page count (for display in teacher UI)
    page_count: int | None = None

    # Teacher weekly upload week number (None = base/permanent material)
    week: int | None = None

    # Arbitrary extra metadata (source_pdf path, OCR flags, legacy store ID, etc.)
    metadata: dict = {}

    @field_validator("title", "source_markdown", "unique_id", mode="before")
    @classmethod
    def no_nul_bytes(cls, v):
        # Postgres rejects \x00 in TEXT/VARCHAR; PDF/OCR extraction can emit it.
        if isinstance(v, str):
            return strip_nul(v)
        return v

    @field_validator("title", "source_markdown", "unique_id")
    @classmethod
    def not_empty(cls, v: str, info) -> str:
        if not v.strip():
            raise ValueError(f"{info.field_name} must not be empty or whitespace")
        return v

    @field_validator("metadata")
    @classmethod
    def no_nul_bytes_in_metadata(cls, v: dict) -> dict:
        # Postgres JSONB rejects NUL in strings the same way TEXT does.
        return sanitize_jsonable(v)

    @field_validator("material_kind")
    @classmethod
    def valid_kind(cls, v: str) -> str:
        if v not in VALID_MATERIAL_KINDS:
            return "other"
        return v
