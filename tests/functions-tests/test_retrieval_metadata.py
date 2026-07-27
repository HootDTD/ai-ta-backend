from __future__ import annotations

from retrieval.context_packer import pack_context
from retrieval.document_visibility import build_chunk_metadata


def test_build_chunk_metadata_uses_page_specific_weekly_fields():
    metadata = {
        "teacher_upload_id": "55",
        "week": 6,
        "kind": "notes",
        "material_kind": "notes",
        "ocr_provider": "mathpix_selective_fallback",
        "page_debug": [
            {
                "page": 3,
                "ocr_confidence": 0.82,
                "extraction_mode": "native_plus_mathpix",
                "latex_text": r"\nabla \cdot \vec{V} = 0",
                "page_asset": {"storage_key": "teacher-uploads/55/page-0003.png"},
            }
        ],
    }

    chunk_meta = build_chunk_metadata(metadata, 3)

    assert chunk_meta["teacher_upload_id"] == "55"
    assert chunk_meta["week"] == 6
    assert chunk_meta["material_kind"] == "notes"
    assert chunk_meta["ocr_confidence"] == 0.82
    assert chunk_meta["page_asset"] == {"storage_key": "teacher-uploads/55/page-0003.png"}
    assert chunk_meta["raw_latex"] == r"\nabla \cdot \vec{V} = 0"


def test_pack_context_marks_weekly_slides_and_preserves_metadata():
    snippets = pack_context(
        [
            {
                "chunk_id": 101,
                "content": "Updated lecture note on control volume analysis.",
                "page_number": 8,
                "section_path": "Week 3",
                "chunk_type": "body",
                "figure_id": None,
                "doc_title": "Week 3 Slides",
                "material_kind": "slides",
                "week": 3,
                "source_path": "week3-slides.pdf",
                "final_score": 0.88,
                "metadata": {
                    "teacher_upload_id": "17",
                    "material_kind": "slides",
                    "week": 3,
                    "ocr_provider": "native_pdf",
                    "page_asset": {"storage_key": "teacher-uploads/17/page-0008.png"},
                },
            }
        ],
        token_budget=400,
        citation_label="Textbook",
    )

    assert len(snippets) == 1
    snippet = snippets[0]
    assert snippet.citation_marker == "[Week 3 Slides, Week 3, p. 8]"
    assert snippet.source_path == "week3-slides.pdf"
    assert snippet.metadata["teacher_upload_id"] == "17"
    assert snippet.metadata["page_asset"] == {"storage_key": "teacher-uploads/17/page-0008.png"}


def test_pack_context_keeps_kind_label_for_untitled_document():
    snippets = pack_context(
        [
            {
                "chunk_id": 102,
                "content": "Untitled lecture note.",
                "page_number": 9,
                "material_kind": "slides",
                "week": 3,
            }
        ],
        token_budget=400,
        citation_label="Textbook",
    )

    assert snippets[0].citation_marker == "[Slides, Week 3, p. 9]"


def test_pack_context_keeps_truncated_fallback_for_untitled_chunk_without_page():
    fallback = "A custom textbook label longer than thirty characters"
    snippets = pack_context(
        [
            {
                "chunk_id": 105,
                "content": "An untitled, page-free textbook passage.",
                "material_kind": "textbook",
            }
        ],
        token_budget=400,
        citation_label=fallback,
    )

    assert snippets[0].citation_marker == f"[{fallback[:30]}]"


def test_pack_context_truncates_titled_marker_to_30_characters():
    title = "A document title that is longer than thirty characters"
    snippets = pack_context(
        [
            {
                "chunk_id": 103,
                "content": "A titled textbook passage.",
                "page_number": 10,
                "doc_title": title,
                "material_kind": "textbook",
            }
        ],
        token_budget=400,
        citation_label="Textbook",
    )

    assert len(title[:30]) == 30
    assert snippets[0].citation_marker == f"[{title[:30]}, p. 10]"


def test_pack_context_uses_title_only_for_chunk_without_page():
    snippets = pack_context(
        [
            {
                "chunk_id": 104,
                "content": "A page-free titled reference.",
                "doc_title": "Course Reference",
                "material_kind": "other",
            }
        ],
        token_budget=400,
        citation_label="Textbook",
    )

    assert snippets[0].citation_marker == "[Course Reference]"
