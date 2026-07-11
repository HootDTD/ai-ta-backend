from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from apollo.provisioning.section_grouping import group_into_sections
from knowledge.teacher_pdf_ingestion import (
    NativeBlock,
    NormalizedPage,
    NormalizedRegion,
    TeacherPDFIngestor,
    choose_mathpix_strategy,
    merge_page_models,
)
from ocr.provider import OCRBlock, OCRResult


def _native_text_block(text: str, *, size: float | str = 12.0, flags: int | str = 0) -> dict:
    return {
        "type": 0,
        "bbox": [0.0, 0.0, 500.0, 20.0],
        "lines": [{"spans": [{"text": text, "size": size, "flags": flags}]}],
    }


class _NativePage:
    rect = SimpleNamespace(width=612.0, height=792.0)

    def __init__(self, blocks: list[dict]) -> None:
        self._blocks = blocks

    def get_text(self, _format: str) -> dict:
        return {"blocks": self._blocks}


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


def test_mgmt_body_size_numbered_headers_group_without_promoting_sample_questions():
    """MGMT-shaped guide: flat body-size metadata still finds short outline
    headers, while numbered questions in a Sample Questions list stay body text."""
    ingestor = TeacherPDFIngestor()
    page_1, _, _ = ingestor._extract_native_page(
        _NativePage(
            [
                _native_text_block("1. Technology and Society"),
                _native_text_block("Explain how technology shapes institutions."),
                _native_text_block("• Compare technological determinism and social construction."),
                _native_text_block("Sample Questions"),
                _native_text_block(
                    "1. The standardization and distribution of shared culture is referred to as massification."
                ),
                _native_text_block(
                    "Answer: Massification describes the spread of standardized culture."
                ),
                _native_text_block("2) Which perspective emphasizes social choices?"),
                _native_text_block("- Social construction of technology."),
                _native_text_block("3. Shared standardized culture is called massification."),
                _native_text_block("Answer: This is another prose answer, not a new section."),
            ]
        )
    )
    page_2, _, _ = ingestor._extract_native_page(
        _NativePage(
            [
                _native_text_block("2. Strategy"),
                _native_text_block("• Explain the resource-based view."),
                _native_text_block("• Compare deliberate and emergent strategy."),
            ]
        )
    )

    blocks = page_1 + page_2
    heading_texts = [block.text for block in blocks if block.chunk_type == "heading"]
    assert heading_texts == ["1. Technology and Society", "2. Strategy"]

    rows = [
        SimpleNamespace(
            id=index,
            content=block.text,
            document_id=38200,
            page_number=1 if index <= len(page_1) else 2,
            section_path=None,
            chunk_type=block.chunk_type,
        )
        for index, block in enumerate(blocks, start=1)
    ]
    sections = group_into_sections(rows)
    assert [section.title for section in sections] == [
        "1. Technology and Society",
        "2. Strategy",
    ]
    assert "standardization and distribution" in sections[0].text
    assert "Which perspective" in sections[0].text
    assert "Shared standardized culture" in sections[0].text


def test_body_size_bold_short_line_is_heading():
    ingestor = TeacherPDFIngestor()
    blocks, _, _ = ingestor._extract_native_page(
        _NativePage([_native_text_block("Strategic Analysis", flags=16)])
    )
    assert blocks[0].chunk_type == "heading"


def test_existing_large_font_heading_detection_is_unchanged():
    ingestor = TeacherPDFIngestor()
    blocks, _, _ = ingestor._extract_native_page(
        _NativePage([_native_text_block("Course Overview", size=14.0)])
    )
    assert blocks[0].chunk_type == "heading"


def test_malformed_font_metadata_falls_back_to_plain_body_text():
    ingestor = TeacherPDFIngestor()

    blocks, _, _ = ingestor._extract_native_page(
        _NativePage(
            [_native_text_block("Metadata-free body text", size="unknown", flags="unknown")]
        )
    )

    assert blocks == [
        NativeBlock(
            text="Metadata-free body text",
            bbox=[0.0, 0.0, 500.0, 20.0],
            font_size=0.0,
            chunk_type="body",
        )
    ]


def test_strong_numbered_heading_ends_sample_question_context():
    ingestor = TeacherPDFIngestor()

    blocks, _, _ = ingestor._extract_native_page(
        _NativePage(
            [
                _native_text_block("Sample Questions"),
                _native_text_block("1. Brief prompt"),
                _native_text_block("- Supporting choice"),
                _native_text_block("2. Strategy", flags=16),
                _native_text_block("Discussion of strategic frameworks."),
                _native_text_block("3. Next Topic"),
                _native_text_block("Ordinary body text."),
            ]
        )
    )

    chunk_types = {block.text: block.chunk_type for block in blocks}
    assert chunk_types["1. Brief prompt"] == "body"
    assert chunk_types["2. Strategy"] == "heading"
    assert chunk_types["3. Next Topic"] == "heading"
