from __future__ import annotations

"""Hybrid teacher PDF ingestion: native PDF extraction with selective Mathpix fallback."""

import hashlib
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Optional

from ocr.mathpix import MathpixConfig, MathpixOCRProvider
from ocr.provider import OCRResult

try:  # pragma: no cover - import is exercised indirectly in tests
    import fitz  # type: ignore
except Exception:  # pragma: no cover
    fitz = None


def _normalize_text(text: str) -> str:
    return " ".join((text or "").split()).strip()


def _searchable_text(text: str) -> bool:
    normalized = _normalize_text(text)
    if not normalized:
        return False
    return any(ch.isalnum() for ch in normalized)


def _dedupe_key(text: str) -> str:
    normalized = _normalize_text(text).lower()
    return re.sub(r"[^a-z0-9]+", "", normalized)


def _char_trigrams(text: str) -> set[str]:
    """Return the set of character-level trigrams for a normalized string."""
    key = re.sub(r"[^a-z0-9]+", "", _normalize_text(text).lower())
    if len(key) < 3:
        return {key} if key else set()
    return {key[i : i + 3] for i in range(len(key) - 2)}


def _fuzzy_similar(a: str, b: str, threshold: float = 0.75) -> bool:
    """Return True if Jaccard similarity of character trigrams exceeds threshold."""
    trigrams_a = _char_trigrams(a)
    trigrams_b = _char_trigrams(b)
    if not trigrams_a and not trigrams_b:
        return True
    if not trigrams_a or not trigrams_b:
        return False
    intersection = len(trigrams_a & trigrams_b)
    union = len(trigrams_a | trigrams_b)
    return (intersection / union) >= threshold


def _math_symbol_ratio(text: str) -> float:
    normalized = _normalize_text(text)
    if not normalized:
        return 0.0
    symbols = sum(
        1
        for ch in normalized
        if ch in "=+-*/^_{}[]()<>|~" or ord(ch) > 127 and not ch.isalnum() and not ch.isspace()
    )
    return float(symbols) / float(max(1, len(normalized)))


def _bbox_list(bbox: Any) -> Optional[List[float]]:
    if not isinstance(bbox, (list, tuple)) or len(bbox) != 4:
        return None
    try:
        return [float(value) for value in bbox]
    except (TypeError, ValueError):
        return None


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass
class NativeBlock:
    text: str
    bbox: Optional[List[float]]
    font_size: float
    chunk_type: str


@dataclass
class PageHeuristic:
    normalized_native_chars: int
    image_dominant: bool
    equation_like: bool
    handwriting_like: bool
    needs_mathpix: bool
    reasons: List[str] = field(default_factory=list)


@dataclass
class NormalizedRegion:
    kind: str
    text: str
    bbox: Optional[List[float]]
    source: str
    confidence: Optional[float] = None
    chunk_type: str = "body"
    font_size: float = 0.0


@dataclass
class NormalizedPage:
    page_number: int
    plain_text: str
    latex_text: str
    regions: List[NormalizedRegion]
    ocr_confidence: Optional[float]
    extraction_mode: str
    warnings: List[str] = field(default_factory=list)
    asset: Dict[str, Any] = field(default_factory=dict)


@dataclass
class TeacherPDFIngestionConfig:
    render_dpi: int = 300
    min_text_chars: int = 120
    min_ocr_confidence: float = 0.4
    fuzzy_dedupe_threshold: float = 0.75

    @classmethod
    def from_env(cls) -> "TeacherPDFIngestionConfig":
        def _env_int(name: str, default: int) -> int:
            raw = (os.getenv(name) or "").strip()
            try:
                return int(raw) if raw else default
            except ValueError:
                return default

        def _env_float(name: str, default: float) -> float:
            raw = (os.getenv(name) or "").strip()
            try:
                return float(raw) if raw else default
            except ValueError:
                return default

        return cls(
            render_dpi=max(72, _env_int("TEACHER_UPLOAD_RENDER_DPI", 300)),
            min_text_chars=max(1, _env_int("TEACHER_MATHPIX_MIN_TEXT_CHARS", 120)),
            min_ocr_confidence=max(0.0, min(1.0, _env_float("TEACHER_MIN_OCR_CONFIDENCE", 0.4))),
            fuzzy_dedupe_threshold=max(0.0, min(1.0, _env_float("TEACHER_FUZZY_DEDUPE_THRESHOLD", 0.75))),
        )


@dataclass
class TeacherPDFIngestionResult:
    items: List[SimpleNamespace]
    source_markdown: str
    page_count: int
    pages: List[NormalizedPage]
    artifact_manifest: Dict[str, Any]
    ocr_provider: str
    ocr_summary: Dict[str, Any]
    warning_count: int
    warnings: List[str]


def build_teacher_mathpix_provider(render_dpi: int) -> Optional[MathpixOCRProvider]:
    app_id = (os.getenv("MATHPIX_APP_ID") or "").strip()
    app_key = (os.getenv("MATHPIX_APP_KEY") or "").strip()
    if not app_id or not app_key:
        return None
    endpoint = (os.getenv("MATHPIX_ENDPOINT") or "https://api.mathpix.com/v3/text").strip()
    return MathpixOCRProvider(
        MathpixConfig(
            app_id=app_id,
            app_key=app_key,
            endpoint=endpoint,
            dpi=int(render_dpi),
        )
    )


def choose_mathpix_strategy(
    *,
    native_text: str,
    image_area_ratio: float,
    drawing_count: int,
    min_text_chars: int,
) -> PageHeuristic:
    normalized_native = _normalize_text(native_text)
    normalized_chars = len(normalized_native)
    image_dominant = image_area_ratio >= 0.45
    equation_like = _math_symbol_ratio(normalized_native) >= 0.08 or (
        drawing_count >= 12 and normalized_chars < (min_text_chars * 2)
    )
    handwriting_like = image_dominant and normalized_chars < max(20, min_text_chars // 2)

    reasons: List[str] = []
    if normalized_chars == 0:
        reasons.append("no_usable_text_layer")
    if 0 < normalized_chars < min_text_chars:
        reasons.append("low_native_text")
    if image_dominant:
        reasons.append("image_dominant_page")
    if equation_like:
        reasons.append("equation_or_handwriting_heavy")
    if handwriting_like and "equation_or_handwriting_heavy" not in reasons:
        reasons.append("equation_or_handwriting_heavy")

    return PageHeuristic(
        normalized_native_chars=normalized_chars,
        image_dominant=image_dominant,
        equation_like=equation_like,
        handwriting_like=handwriting_like,
        needs_mathpix=bool(reasons),
        reasons=reasons,
    )


def merge_page_models(
    *,
    page_number: int,
    page_bbox: List[float],
    native_blocks: List[NativeBlock],
    heuristic: PageHeuristic,
    mathpix_result: Optional[OCRResult],
    warnings: Optional[List[str]] = None,
    asset: Optional[Dict[str, Any]] = None,
    min_ocr_confidence: float = 0.4,
    fuzzy_dedupe_threshold: float = 0.75,
) -> NormalizedPage:
    native_regions = [
        NormalizedRegion(
            kind="text",
            text=block.text,
            bbox=block.bbox,
            source="native",
            confidence=1.0,
            chunk_type=block.chunk_type,
            font_size=block.font_size,
        )
        for block in native_blocks
        if _searchable_text(block.text)
    ]
    native_text = "\n\n".join(region.text for region in native_regions if region.text).strip()
    page_warnings = list(warnings or [])

    mathpix_plain = ""
    mathpix_latex = ""
    mathpix_conf = None
    ocr_below_threshold = False
    if mathpix_result is not None:
        mathpix_conf = mathpix_result.average_confidence
        if mathpix_conf is not None and mathpix_conf < min_ocr_confidence:
            ocr_below_threshold = True
            page_warnings.append(
                f"Page {page_number}: Mathpix confidence {mathpix_conf:.2f} "
                f"below threshold {min_ocr_confidence:.2f} — OCR result discarded"
            )
        else:
            plain_parts = []
            latex_parts = []
            for block in mathpix_result.blocks or []:
                text = (block.text or "").strip()
                if not text:
                    continue
                if block.kind == "latex":
                    latex_parts.append(text)
                else:
                    plain_parts.append(text)
            mathpix_plain = "\n\n".join(plain_parts).strip()
            mathpix_latex = "\n\n".join(latex_parts).strip()

    def _is_duplicate(a: str, b: str) -> bool:
        """Check if two texts are duplicates using fuzzy trigram similarity."""
        if not a or not b:
            return False
        key_a = _dedupe_key(a)
        key_b = _dedupe_key(b)
        if key_a == key_b:
            return True
        return _fuzzy_similar(a, b, threshold=fuzzy_dedupe_threshold)

    plain_parts_out: List[str] = []
    if native_text:
        plain_parts_out.append(native_text)
    if mathpix_plain:
        if not native_text or not _is_duplicate(native_text, mathpix_plain):
            plain_parts_out.append(mathpix_plain)
    plain_text = "\n\n".join(part for part in plain_parts_out if part).strip()

    regions = list(native_regions)
    if mathpix_plain and (not native_text or not _is_duplicate(native_text, mathpix_plain)):
        regions.append(
            NormalizedRegion(
                kind="text",
                text=mathpix_plain,
                bbox=list(page_bbox),
                source="mathpix",
                confidence=mathpix_conf,
                chunk_type="body",
            )
        )
    if mathpix_latex:
        regions.append(
            NormalizedRegion(
                kind="latex",
                text=mathpix_latex,
                bbox=list(page_bbox),
                source="mathpix",
                confidence=mathpix_conf,
                chunk_type="equation",
            )
        )

    if mathpix_result is None:
        extraction_mode = "native"
        ocr_confidence = 1.0 if native_text else None
    elif ocr_below_threshold:
        extraction_mode = "native_ocr_rejected"
        ocr_confidence = mathpix_conf
    elif native_text:
        extraction_mode = "native_plus_mathpix"
        ocr_confidence = mathpix_conf
    else:
        extraction_mode = "mathpix"
        ocr_confidence = mathpix_conf

    if not plain_text and mathpix_latex:
        plain_text = mathpix_latex

    return NormalizedPage(
        page_number=page_number,
        plain_text=plain_text,
        latex_text=mathpix_latex,
        regions=regions,
        ocr_confidence=ocr_confidence,
        extraction_mode=extraction_mode,
        warnings=page_warnings,
        asset=dict(asset or {}),
    )


class TeacherPDFIngestor:
    def __init__(
        self,
        config: Optional[TeacherPDFIngestionConfig] = None,
        *,
        mathpix_provider: Optional[MathpixOCRProvider] = None,
    ) -> None:
        self.config = config or TeacherPDFIngestionConfig.from_env()
        self.mathpix_provider = mathpix_provider if mathpix_provider is not None else build_teacher_mathpix_provider(
            self.config.render_dpi
        )

    def ingest(
        self,
        pdf_path: Path,
        *,
        doc_id: str,
        upload_page_asset: Optional[Callable[[int, bytes, int, int], Dict[str, Any]]] = None,
    ) -> TeacherPDFIngestionResult:
        if fitz is None:
            raise RuntimeError("PyMuPDF is required for teacher PDF ingestion")

        pdf_path = Path(pdf_path)
        items: List[SimpleNamespace] = []
        pages: List[NormalizedPage] = []
        warnings: List[str] = []
        artifact_pages: List[Dict[str, Any]] = []

        with fitz.open(str(pdf_path)) as doc:  # type: ignore[arg-type]
            page_count = len(doc)
            for index in range(page_count):
                page = doc[index]
                page_number = index + 1
                page_bbox = [0.0, 0.0, float(page.rect.width), float(page.rect.height)]

                native_blocks, native_text, image_ratio = self._extract_native_page(page)
                heuristic = choose_mathpix_strategy(
                    native_text=native_text,
                    image_area_ratio=image_ratio,
                    drawing_count=self._count_drawings(page),
                    min_text_chars=self.config.min_text_chars,
                )

                page_warnings: List[str] = []
                asset_meta: Dict[str, Any] = {}
                png_bytes: Optional[bytes] = None
                if heuristic.needs_mathpix or upload_page_asset is not None:
                    try:
                        png_bytes, width, height = self._render_page_png(page)
                        asset_meta = {"width": width, "height": height}
                        if upload_page_asset is not None:
                            try:
                                asset_meta.update(upload_page_asset(page_number, png_bytes, width, height))
                            except Exception as exc:
                                page_warnings.append(f"Page {page_number}: failed to store page image ({exc})")
                    except Exception as exc:
                        page_warnings.append(f"Page {page_number}: failed to render page ({exc})")

                mathpix_result: Optional[OCRResult] = None
                if heuristic.needs_mathpix:
                    if self.mathpix_provider is None:
                        page_warnings.append(
                            f"Page {page_number}: Mathpix unavailable for recovery ({', '.join(heuristic.reasons)})"
                        )
                    elif png_bytes is None:
                        page_warnings.append(f"Page {page_number}: Mathpix skipped because rendered image is missing")
                    else:
                        try:
                            mathpix_result = self.mathpix_provider.recognize(
                                image_bytes=png_bytes,
                                mime="image/png",
                                dpi=self.config.render_dpi,
                            )
                        except Exception as exc:
                            page_warnings.append(f"Page {page_number}: Mathpix failed ({exc})")

                normalized_page = merge_page_models(
                    page_number=page_number,
                    page_bbox=page_bbox,
                    native_blocks=native_blocks,
                    heuristic=heuristic,
                    mathpix_result=mathpix_result,
                    warnings=page_warnings,
                    asset=asset_meta,
                    min_ocr_confidence=self.config.min_ocr_confidence,
                    fuzzy_dedupe_threshold=self.config.fuzzy_dedupe_threshold,
                )
                pages.append(normalized_page)
                warnings.extend(page_warnings)
                artifact_pages.append(
                    {
                        "page": page_number,
                        "width": asset_meta.get("width"),
                        "height": asset_meta.get("height"),
                        "bucket": asset_meta.get("bucket"),
                        "storage_key": asset_meta.get("storage_key"),
                        "extraction_mode": normalized_page.extraction_mode,
                        "ocr_confidence": normalized_page.ocr_confidence,
                        "warnings": list(page_warnings),
                    }
                )
                items.extend(self._page_to_items(doc_id=doc_id, source_pdf=pdf_path, page=normalized_page))

        source_markdown = "\n\n".join(page.plain_text for page in pages if _searchable_text(page.plain_text)).strip()
        mathpix_pages = sum(1 for page in pages if page.extraction_mode in {"mathpix", "native_plus_mathpix"})
        ocr_rejected_pages = sum(1 for page in pages if page.extraction_mode == "native_ocr_rejected")
        summary = {
            "page_count": len(pages),
            "native_pages": sum(1 for page in pages if page.extraction_mode == "native"),
            "mathpix_pages": sum(1 for page in pages if page.extraction_mode == "mathpix"),
            "native_plus_mathpix_pages": sum(1 for page in pages if page.extraction_mode == "native_plus_mathpix"),
            "ocr_rejected_pages": ocr_rejected_pages,
            "warning_pages": sum(1 for page in pages if page.warnings),
            "searchable_pages": sum(1 for page in pages if _searchable_text(page.plain_text) or page.latex_text),
            "partial_extraction": bool(warnings),
        }
        ocr_provider = "mathpix_selective_fallback" if mathpix_pages else "native_pdf"
        return TeacherPDFIngestionResult(
            items=items,
            source_markdown=source_markdown,
            page_count=len(pages),
            pages=pages,
            artifact_manifest={"pages": artifact_pages},
            ocr_provider=ocr_provider,
            ocr_summary=summary,
            warning_count=len(warnings),
            warnings=warnings,
        )

    def _extract_native_page(self, page: Any) -> tuple[List[NativeBlock], str, float]:
        page_dict = page.get_text("dict") or {}
        blocks: List[NativeBlock] = []
        image_area = 0.0
        page_area = max(1.0, float(page.rect.width * page.rect.height))

        for block in page_dict.get("blocks") or []:
            block_type = int(block.get("type", 0) or 0)
            bbox = _bbox_list(block.get("bbox"))
            if block_type == 1:
                if bbox is not None:
                    image_area += max(0.0, (bbox[2] - bbox[0]) * (bbox[3] - bbox[1]))
                continue
            if block_type != 0:
                continue

            line_texts: List[str] = []
            font_sizes: List[float] = []
            for line in block.get("lines") or []:
                parts: List[str] = []
                for span in line.get("spans") or []:
                    text = str(span.get("text") or "")
                    if text:
                        parts.append(text)
                    try:
                        font_sizes.append(float(span.get("size") or 0.0))
                    except (TypeError, ValueError):
                        continue
                joined = "".join(parts).strip()
                if joined:
                    line_texts.append(joined)

            block_text = "\n".join(line_texts).strip()
            if not _searchable_text(block_text):
                continue

            avg_font_size = float(sum(font_sizes) / len(font_sizes)) if font_sizes else 0.0
            chunk_type = "body"
            normalized = _normalize_text(block_text)
            if avg_font_size >= 14.0 and len(normalized) <= 140:
                chunk_type = "heading"
            elif _math_symbol_ratio(normalized) >= 0.12:
                chunk_type = "equation"

            blocks.append(
                NativeBlock(
                    text=block_text,
                    bbox=bbox,
                    font_size=avg_font_size,
                    chunk_type=chunk_type,
                )
            )

        native_text = "\n\n".join(block.text for block in blocks).strip()
        return blocks, native_text, float(image_area / page_area)

    def _count_drawings(self, page: Any) -> int:
        try:
            return len(page.get_drawings() or [])
        except Exception:
            return 0

    def _render_page_png(self, page: Any) -> tuple[bytes, int, int]:
        matrix = fitz.Matrix(self.config.render_dpi / 72.0, self.config.render_dpi / 72.0)
        pix = page.get_pixmap(matrix=matrix, alpha=False)
        return pix.tobytes("png"), int(pix.width), int(pix.height)

    def _page_to_items(self, *, doc_id: str, source_pdf: Path, page: NormalizedPage) -> List[SimpleNamespace]:
        items: List[SimpleNamespace] = []
        current_section: List[str] = []
        seen: set[str] = set()

        for region_index, region in enumerate(page.regions):
            text = (region.text or "").strip()
            if not _searchable_text(text):
                continue

            item_text = text
            chunk_type = region.chunk_type or "body"
            if region.kind == "latex":
                chunk_type = "equation"
                if page.plain_text and _dedupe_key(page.plain_text) != _dedupe_key(text):
                    item_text = f"{page.plain_text}\n\nLaTeX:\n{text}"

            key = _dedupe_key(item_text)
            if key and key in seen:
                continue
            if key:
                seen.add(key)

            if chunk_type == "heading":
                current_section = [_normalize_text(text)[:120]]

            item_id = f"{doc_id}:{page.page_number}:{region_index}"
            items.append(
                SimpleNamespace(
                    id=item_id,
                    doc_id=doc_id,
                    page=page.page_number,
                    type=chunk_type,
                    section_path=list(current_section),
                    bbox=region.bbox,
                    text=item_text,
                    raw_text=text,
                    caption=None,
                    figure_id=None,
                    neighbors=[],
                    parents=[],
                    diagram_json=None,
                    sha256=_sha256_text(f"{item_id}:{item_text}"),
                    source_pdf=str(source_pdf),
                )
            )

        return items


__all__ = [
    "NormalizedPage",
    "NormalizedRegion",
    "PageHeuristic",
    "TeacherPDFIngestor",
    "TeacherPDFIngestionConfig",
    "TeacherPDFIngestionResult",
    "build_teacher_mathpix_provider",
    "choose_mathpix_strategy",
    "merge_page_models",
    "_fuzzy_similar",
    "_char_trigrams",
]
