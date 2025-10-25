from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


DOC_TYPE_LABELS = {
    "textbook": "Textbook",
    "slides": "Slides",
    "notes": "Notes",
    "homework": "Homework",
    "exams": "Exam",
    "exam": "Exam",
    "other": "Reference",
}


def _doc_type_label(kind: Optional[str]) -> str:
    if not kind:
        return "Textbook"
    key = str(kind).strip().lower()
    return DOC_TYPE_LABELS.get(key, "Textbook")


def _normalize_page(page: Any) -> Optional[int]:
    if isinstance(page, bool):  # guard bool subclass of int
        return None
    if isinstance(page, (int, float)):
        try:
            page_int = int(page)
        except (TypeError, ValueError):
            return None
        return page_int if page_int > 0 else None
    return None


def _normalize_bbox(bbox: Any) -> Optional[List[float]]:
    if bbox is None:
        return None
    if isinstance(bbox, (list, tuple)):
        try:
            return [float(x) for x in bbox]
        except (TypeError, ValueError):
            return None
    if isinstance(bbox, str):
        try:
            data = json.loads(bbox)
        except json.JSONDecodeError:
            return None
        return _normalize_bbox(data)
    return None


def build_citation_info(
    snippet: Any,
    row: Optional[Any],
    store_meta: Dict[str, Dict[str, Any]],
) -> Dict[str, Any]:
    row_get = row.get if hasattr(row, "get") else (lambda key, default=None: getattr(row, key, default))  # type: ignore

    store_key = row_get("store_key", None)
    store_kind = row_get("store_kind", None)
    if isinstance(store_key, float) and store_key != store_key:  # NaN guard
        store_key = None
    if isinstance(store_kind, float) and store_kind != store_kind:
        store_kind = None
    meta_entry = store_meta.get(store_key) if store_key else None

    doc_type = _doc_type_label(meta_entry.get("kind") if meta_entry else store_kind)

    file_name = getattr(snippet, "doc_short", None) or getattr(snippet, "doc_title", None)
    if not file_name:
        source_path = getattr(snippet, "source_path", "") or row_get("source_path", "")
        if source_path:
            file_name = Path(str(source_path)).name
        else:
            file_name = str(getattr(snippet, "id", "citation"))

    page = getattr(snippet, "page", None)
    if page is None:
        page = row_get("page", None)
    page_norm = _normalize_page(page)

    bbox = row_get("bbox", None)
    bbox_norm = _normalize_bbox(bbox)

    ocr_conf = None
    if meta_entry is not None:
        ocr_conf = meta_entry.get("average_confidence")
        if isinstance(ocr_conf, (int, float)):
            ocr_conf = float(ocr_conf)
        else:
            ocr_conf = None

    verified = doc_type == "Textbook"

    week_info = None
    if meta_entry:
        raw_week = meta_entry.get("week")
        try:
            week_val = int(raw_week)
            if week_val > 0:
                week_info = week_val
        except (TypeError, ValueError):
            week_info = None

    if doc_type in {"Notes", "Slides"} and week_info is not None:
        label = f"[{doc_type}, Week {week_info}, p. {page_norm if isinstance(page_norm, int) else '?'}]"
    else:
        label = f"[{doc_type}, p. {page_norm if isinstance(page_norm, int) else '?'}]"

    return {
        "doc_type": doc_type,
        "file": file_name,
        "page": page_norm,
        "bbox": bbox_norm,
        "ocr_conf": ocr_conf,
        "verified": verified,
        "label": label,
        "store_key": store_key,
    }


def format_citations(
    citations: Iterable[Dict[str, Any]],
    id_to_row: Optional[Dict[str, Any]],
    store_meta: Dict[str, Dict[str, Any]],
) -> Tuple[List[str], List[Dict[str, Any]]]:
    labels: List[str] = []
    structured: List[Dict[str, Any]] = []
    seen: set[Tuple[str, str, Optional[int]]] = set()

    for entry in citations:
        snippet = entry.get("snippet")
        if snippet is None:
            continue
        cid = entry.get("id")
        row = None
        if cid and id_to_row:
            row = id_to_row.get(cid)
        info = build_citation_info(snippet, row, store_meta)
        key = (info["doc_type"], info["file"], info["page"])
        if key in seen:
            continue
        seen.add(key)
        labels.append(info["label"])
        structured.append({k: info[k] for k in ("doc_type", "file", "page", "bbox", "ocr_conf", "verified", "label")})

    return labels, structured
