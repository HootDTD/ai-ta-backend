"""Done-side unit coverage for INTERACTION4 reference-question asides."""

from __future__ import annotations

from contextlib import ExitStack
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.dialects import sqlite

from apollo.handlers.done import _full_transcript, handle_done
from apollo.handlers.tests._done_fixtures import _old_path_patches
from apollo.hoot_bridge.reference_answer import (
    ASIDE_COUNT_SESSION_METADATA_KEY,
    ASIDE_MESSAGE_INTENT_TAG,
)

pytestmark = pytest.mark.unit


class _TranscriptRows:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return list(self._rows)


async def test_adjudicator_transcript_excludes_aside_but_keeps_triggering_question():
    db = MagicMock()
    db.execute = AsyncMock(
        return_value=_TranscriptRows(
            [
                ("student", "Wait, what is a network effect?"),
                ("apollo", "Okay, so how does that fit into what you were teaching me?"),
                ("student", "It becomes more useful as more people join."),
            ]
        )
    )

    transcript = await _full_transcript(db, attempt_id=99)

    assert transcript == (
        ("student", "Wait, what is a network effect?"),
        ("apollo", "Okay, so how does that fit into what you were teaching me?"),
        ("student", "It becomes more useful as more people join."),
    )
    assert all("A network effect means" not in content for _, content in transcript)

    statement = db.execute.await_args.args[0]
    compiled = statement.compile(
        dialect=sqlite.dialect(),
        compile_kwargs={"literal_binds": True},
    )
    sql = " ".join(str(compiled).split())
    assert "attempt_id = 99" in sql
    assert f"intent IS NULL OR app.tutoring_messages.intent != '{ASIDE_MESSAGE_INTENT_TAG}'" in sql
    assert sql.endswith("ORDER BY app.tutoring_messages.turn_index")


async def _run_done_with_session_metadata(metadata):
    db, sess, _attempt, patches = _old_path_patches()
    if metadata is not None:
        sess.metadata_ = metadata

    with ExitStack() as stack:
        for patcher in patches:
            stack.enter_context(patcher)
        result = await handle_done(db=db, neo=MagicMock(), session_id=11)
    return result, sess


async def test_grading_provenance_reports_reference_asides_used():
    result, _sess = await _run_done_with_session_metadata({ASIDE_COUNT_SESSION_METADATA_KEY: 2})

    assert result["grading_provenance"]["reference_question_asides_used"] == 2


async def test_grading_provenance_defaults_asides_to_zero_without_metadata():
    result, sess = await _run_done_with_session_metadata(None)

    assert not hasattr(sess, "metadata_")
    assert result["grading_provenance"]["reference_question_asides_used"] == 0
