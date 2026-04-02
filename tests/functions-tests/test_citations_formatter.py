from __future__ import annotations

import types
from typing import Any

from citations.formatter import format_citations


def _snippet(sn_id: str, page: int, doc_short: str, source_path: str = "file.pdf") -> Any:
    return types.SimpleNamespace(
        id=sn_id,
        page=page,
        doc_short=doc_short,
        source_path=source_path,
        doc_title=None,
    )


def test_format_citations_labels_and_verification():
    slide_row = {
        "store_kind": "slides",
        "store_key": "/idx/slides",
        "page": 3,
        "bbox": [0, 0, 100, 200],
    }
    textbook_row = {
        "store_kind": None,
        "store_key": "/idx/textbook",
        "page": 10,
        "bbox": None,
    }
    store_meta = {
        "/idx/slides": {"kind": "slides", "average_confidence": 0.8},
        "/idx/textbook": {"kind": "textbook", "average_confidence": None},
    }
    citations = [
        {"id": "s1", "snippet": _snippet("s1", 3, "Slides Doc")},
        {"id": "s1_dup", "snippet": _snippet("s1", 3, "Slides Doc")},
        {"id": "t1", "snippet": _snippet("t1", 10, "Main Text")},
    ]

    id_to_row = {
        "s1": slide_row,
        "s1_dup": slide_row,
        "t1": textbook_row,
    }

    labels, structured = format_citations(citations, id_to_row, store_meta)

    assert labels[0] == "[Slides, p. 3]"
    assert "[Textbook, p. 10]" in labels
    assert len(structured) == 2  # duplicate slide entry deduped

    slide_entry = next(entry for entry in structured if entry["doc_type"] == "Slides")
    assert slide_entry["verified"] is False
    assert slide_entry["ocr_conf"] == 0.8
    assert slide_entry["bbox"] == [0.0, 0.0, 100.0, 200.0]

    text_entry = next(entry for entry in structured if entry["doc_type"] == "Textbook")
    assert text_entry["verified"] is True
    assert text_entry["ocr_conf"] is None


def test_format_citations_includes_weekly_teacher_metadata():
    slide_row = {
        "store_kind": "slides",
        "store_key": "upload-42",
        "page": 7,
        "bbox": None,
    }
    store_meta = {
        "upload-42": {
            "kind": "slides",
            "week": 4,
            "average_confidence": 0.91,
            "ocr_provider": "mathpix_selective_fallback",
            "teacher_upload_id": "42",
            "page_asset": {"storage_key": "teacher-uploads/42/page-0007.png"},
            "raw_latex": r"\int_0^1 x^2 \, dx",
        }
    }
    citations = [{"id": "s1", "snippet": _snippet("s1", 7, "Week 4 Slides")}]
    id_to_row = {"s1": slide_row}

    labels, structured = format_citations(citations, id_to_row, store_meta)

    assert labels == ["[Slides, Week 4, p. 7]"]
    assert structured[0]["week"] == 4
    assert structured[0]["ocr_provider"] == "mathpix_selective_fallback"
    assert structured[0]["teacher_upload_id"] == "42"
    assert structured[0]["page_asset"] == {"storage_key": "teacher-uploads/42/page-0007.png"}
    assert structured[0]["raw_latex"] == r"\int_0^1 x^2 \, dx"
