"""Attempt-scoped transcript behavior on the Done payload."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from apollo.handlers.done import _fetch_attempt_transcript, handle_done
from apollo.handlers.tests._done_fixtures import _old_path_patches

pytestmark = pytest.mark.unit


class _MessageResult:
    def __init__(self, rows):
        self.rows = rows

    def scalars(self):
        result = MagicMock()
        result.all.return_value = self.rows
        return result


def _message(role: str, content: str, turn_index: int) -> MagicMock:
    message = MagicMock()
    message.role = role
    message.content = content
    message.turn_index = turn_index
    return message


async def test_fetch_attempt_transcript_returns_ordered_shape():
    rows = [_message("student", "hello", 0), _message("apollo", "teach me", 1)]
    db = MagicMock()
    db.execute = AsyncMock(return_value=_MessageResult(rows))

    assert await _fetch_attempt_transcript(db, 42) == [
        {"role": "student", "content": "hello", "turn_index": 0},
        {"role": "apollo", "content": "teach me", "turn_index": 1},
    ]


async def test_fetch_attempt_transcript_soft_fails():
    db = MagicMock()
    db.execute = AsyncMock(side_effect=RuntimeError("db unavailable"))
    assert await _fetch_attempt_transcript(db, 42) == []


async def test_done_payload_includes_transcript():
    db, _session, _attempt, patches = _old_path_patches()
    patches = [p for p in patches if getattr(p, "attribute", None) != "_fetch_attempt_transcript"]
    transcript = [{"role": "student", "content": "hello", "turn_index": 0}]
    patches.append(
        patch(
            "apollo.handlers.done._fetch_attempt_transcript",
            new=AsyncMock(return_value=transcript),
        )
    )
    for patcher in patches:
        patcher.start()
    try:
        out = await handle_done(db=db, neo=MagicMock(), session_id=11)
    finally:
        for patcher in reversed(patches):
            patcher.stop()

    assert out["transcript"] == transcript
