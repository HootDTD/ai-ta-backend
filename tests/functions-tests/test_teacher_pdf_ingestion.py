from __future__ import annotations

from pathlib import Path

from backend.ocr.provider import OCRBlock, OCRResult
from backend.knowledge.teacher_pdf_ingestion import (
    NativeBlock,
    NormalizedPage,
    NormalizedRegion,
    TeacherPDFIngestor,
    choose_mathpix_strategy,
    merge_page_models,
)


def test_choose_mathpix_strategy_skips_clean_digital_page():
    heuristic = choose_mathpix_strategy(
        native_text="This is a clean digital PDF page with plenty of searchable text " * 4,
        image_area_ratio=0.05,
        drawing_count=0,
        min_text_chars=120,
    )

    assert heuristic.needs_mathpix is False
    assert heuristic.reasons == []


def test_choose_mathpix_strategy_flags_low_text_image_page():
    heuristic = choose_mathpix_strategy(
        native_text="u l",
        image_area_ratio=0.82,
        drawing_count=1,
        min_text_chars=120,
    )

    assert heuristic.needs_mathpix is True
    assert "low_native_text" in heuristic.reasons
    assert "image_dominant_page" in heuristic.reasons


def test_merge_page_models_combines_native_and_mathpix_content():
    heuristic = choose_mathpix_strategy(
        native_text="Bernoulli relation",
        image_area_ratio=0.0,
        drawing_count=20,
        min_text_chars=120,
    )
    result = OCRResult(
        blocks=[
            OCRBlock(kind="text", text="p + rho g z + 1/2 rho v^2 = constant", confidence=0.9),
            OCRBlock(kind="latex", text=r"p + \rho g z + \frac{1}{2}\rho v^2 = C", confidence=0.8),
        ]
    )

    page = merge_page_models(
        page_number=2,
        page_bbox=[0.0, 0.0, 612.0, 792.0],
        native_blocks=[
            NativeBlock(
                text="Bernoulli relation",
                bbox=[10.0, 20.0, 110.0, 45.0],
                font_size=12.0,
                chunk_type="body",
            )
        ],
        heuristic=heuristic,
        mathpix_result=result,
    )

    assert page.extraction_mode == "native_plus_mathpix"
    assert "Bernoulli relation" in page.plain_text
    assert "rho g z" in page.plain_text
    assert "\\frac" in page.latex_text
    assert any(region.kind == "latex" for region in page.regions)


def test_page_to_items_creates_searchable_equation_chunk():
    ingestor = TeacherPDFIngestor()
    page = NormalizedPage(
        page_number=5,
        plain_text="The governing equation is shown below.",
        latex_text=r"\nabla \cdot \vec{V} = 0",
        regions=[
            NormalizedRegion(
                kind="latex",
                text=r"\nabla \cdot \vec{V} = 0",
                bbox=[0.0, 0.0, 612.0, 792.0],
                source="mathpix",
                confidence=0.95,
                chunk_type="equation",
            )
        ],
        ocr_confidence=0.95,
        extraction_mode="mathpix",
    )

    items = ingestor._page_to_items(
        doc_id="teacher:1:5:notes:10",
        source_pdf=Path("fixture.pdf"),
        page=page,
    )

    assert len(items) == 1
    assert items[0].type == "equation"
    assert "LaTeX" in items[0].text
    assert "\\nabla" in items[0].text
