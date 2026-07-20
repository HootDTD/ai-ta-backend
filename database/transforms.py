"""Typed boundary transforms for the target database models."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

DOCUMENT_STATUSES = frozenset({"queued", "processing", "ready", "failed"})
UPLOAD_STATUSES = frozenset({"queued", "processing", "ready", "failed", "superseded"})
UPLOAD_JOB_STATES = frozenset({"queued", "leased", "processing", "completed", "failed"})


def chat_keywords_to_text_array(value: Any) -> list[str]:
    """Normalize a legacy JSON string array for ``app.chat_messages``."""
    if value is None:
        return []
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError("chat keywords must be a JSON string array") from exc
    if not isinstance(value, (list, tuple)) or any(not isinstance(item, str) for item in value):
        raise ValueError("chat keywords must be a string array")
    return list(value)


def chat_keywords_to_json_array(value: Any) -> str:
    """Serialize a target text array for legacy/export boundaries."""
    return json.dumps(chat_keywords_to_text_array(value), ensure_ascii=False)


def merge_course_settings(
    search_weights: Mapping[str, Any] | None,
    teacher_weights: Mapping[str, Any] | None,
    weight_bounds: Mapping[str, Any] | None,
    current_week: Any,
) -> dict[str, Any]:
    """Build target ``app.courses`` settings; teacher weights take precedence."""
    weights = dict(search_weights or {})
    weights.update(teacher_weights or {})
    bounds = weight_bounds or {}
    minimum = _bounded_float(bounds.get("min"), default=0.0)
    maximum = _bounded_float(bounds.get("max"), default=1.0)
    if minimum > maximum:
        minimum, maximum = 0.0, 1.0
    try:
        week = int(current_week)
    except (TypeError, ValueError):
        week = 1
    if not 1 <= week <= 16:
        week = 1
    return {
        "current_week": week,
        "retrieval_weights": weights,
        "retrieval_weight_min": minimum,
        "retrieval_weight_max": maximum,
    }


def document_status_columns(value: Mapping[str, Any] | str | None) -> tuple[str, str | None]:
    """Promote a legacy status envelope into typed status/reason columns."""
    if isinstance(value, Mapping):
        state = str(value.get("state") or "ready")
        reason = value.get("reason")
    else:
        state = str(value or "ready")
        reason = None
    if state == "pending":
        state = "queued"
    if state not in DOCUMENT_STATUSES:
        state = "ready"
    failure_reason = str(reason)[:500] if state == "failed" and reason is not None else None
    return state, failure_reason


def normalize_upload_status(value: str) -> str:
    return _checked_state(value, UPLOAD_STATUSES, "upload status")


def normalize_upload_job_state(value: str) -> str:
    return _checked_state(value, UPLOAD_JOB_STATES, "upload job state")


def _checked_state(value: str, allowed: frozenset[str], label: str) -> str:
    state = str(value).strip().lower()
    if state not in allowed:
        raise ValueError(f"Unknown {label}: {value!r}")
    return state


def _bounded_float(value: Any, *, default: float) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return min(1.0, max(0.0, result))


__all__ = [
    "chat_keywords_to_json_array",
    "chat_keywords_to_text_array",
    "document_status_columns",
    "merge_course_settings",
    "normalize_upload_job_state",
    "normalize_upload_status",
]
