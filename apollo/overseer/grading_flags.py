"""Feature switches shared by grading surfaces unrelated to detection."""

from __future__ import annotations

import os

_TRUTHY = frozenset({"1", "true", "yes", "on"})


def _enabled(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in _TRUTHY


def topic_score_served_enabled() -> bool:
    return _enabled("APOLLO_TOPIC_SCORE_SERVED")


def transcript_grader_enabled() -> bool:
    return _enabled("APOLLO_TRANSCRIPT_GRADER")


__all__ = ["topic_score_served_enabled", "transcript_grader_enabled"]
