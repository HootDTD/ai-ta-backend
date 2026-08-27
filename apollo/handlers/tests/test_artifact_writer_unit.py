"""Unit seams for ``apollo/handlers/artifact_writer.py`` failure domains.

The savepoint/insert-conflict path is exercised end-to-end against real
Postgres in ``tests/database/test_done_artifact_route_postgres.py``; this
module covers the commit-stage failure branch, which needs a session double
(a real commit failure requires killing the connection mid-request).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from apollo.handlers.artifact_writer import write_artifacts

pytestmark = pytest.mark.asyncio


def _session_double(*, commit_error: Exception) -> MagicMock:
    """AsyncSession double: healthy savepoint + flush, failing commit."""
    db = MagicMock()
    nested = MagicMock()
    nested.__aenter__ = AsyncMock(return_value=nested)
    nested.__aexit__ = AsyncMock(return_value=False)
    db.begin_nested = MagicMock(return_value=nested)
    db.add = MagicMock()
    db.flush = AsyncMock()
    db.commit = AsyncMock(side_effect=commit_error)
    db.rollback = AsyncMock()
    return db


async def test_commit_failure_soft_fails_and_rolls_back():
    """A commit-stage failure must follow the soft-fail contract: log, roll
    back, return ``None`` — never raise into the Done response."""
    db = _session_double(commit_error=RuntimeError("connection died at commit"))
    attempt = SimpleNamespace(id=7)
    sess = SimpleNamespace(user_id="u", search_space_id=1, concept_id=2)

    with (
        patch("apollo.handlers.artifact_writer.build_llm_artifact", return_value={"p": 1}),
        patch("apollo.handlers.artifact_writer._artifact_row", return_value=MagicMock()),
    ):
        out = await write_artifacts(
            db,
            attempt=attempt,
            sess=sess,
            coverage={},
            rubric={},
            latency_ms=5,
        )

    assert out is None
    db.rollback.assert_awaited_once()
