"""Empty-attempt guard + auto-done stamp for ``apollo/handlers/done.py``.

2026-08-07 bimodal-fix P0.1/P0.4:

* P0.1 — an attempt with ZERO persisted student messages must never be
  graded (defect I1: phantom browse/abandon rows were served F(0) with a
  narrative invented from the reference solution). ``handle_done`` raises
  ``EmptyAttemptError`` BEFORE any mutation: no freeze, no phase change, no
  commit, and the attempt row is untouched so a later real Done is not
  penalized as a reattempt.
* P0.4 — a Done decided by the questioning engine (``auto_done=True``)
  stamps ``auto_done: true`` into ``diagnostic_report``; a student Done
  leaves the report byte-identical (key absent).

Same no-Docker harness as the other Done unit tests (``_old_path_patches``).
"""

from __future__ import annotations

from contextlib import ExitStack
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from apollo.errors import EmptyAttemptError
from apollo.handlers.tests._done_fixtures import _old_path_patches

pytestmark = pytest.mark.unit


def _drop(patches, *attributes):
    return [p for p in patches if getattr(p, "attribute", None) not in attributes]


async def _run(*, student_message_count: int | None = None, auto_done: bool = False):
    """Drive ``handle_done`` through the harness. Returns
    ``(result_or_exc, db, sess, attempt, started)`` where ``started`` maps
    patch attribute names to their live mocks."""
    db, sess, attempt, patches = _old_path_patches()
    if student_message_count is not None:
        patches = _drop(patches, "_student_message_count")
        patches.append(
            patch(
                "apollo.handlers.done._student_message_count",
                new=AsyncMock(return_value=student_message_count),
            )
        )

    from apollo.handlers.done import handle_done

    started: dict[str, object] = {}
    with ExitStack() as stack:
        for p in patches:
            started[getattr(p, "attribute", repr(p))] = stack.enter_context(p)
        try:
            result = await handle_done(
                db=db, neo=MagicMock(), session_id=11, auto_done=auto_done
            )
        except Exception as exc:  # noqa: BLE001 - the guard test asserts on it
            result = exc
    return result, db, sess, attempt, started


async def test_zero_student_messages_raises_and_mutates_nothing():
    result, db, sess, attempt, started = await _run(student_message_count=0)

    assert isinstance(result, EmptyAttemptError)
    assert result.session_id == 11
    assert result.attempt_id == attempt.id
    # No mutation of any kind before the guard: no commit, no freeze, no
    # phase change, attempt row untouched (a marked row would flip
    # is_reattempt_in_session and dock XP on the real Done later).
    assert db.commit.await_count == 0
    assert started["freeze"].await_count == 0
    assert sess.phase == "TEACHING"
    assert attempt.result is None
    assert attempt.diagnostic_report is None


async def test_nonempty_attempt_grades_normally():
    result, db, sess, attempt, started = await _run(student_message_count=1)

    assert isinstance(result, dict)
    assert attempt.result == "graded"
    assert started["_student_message_count"].await_count == 1


async def test_auto_done_stamps_diagnostic_report():
    result, _db, _sess, attempt, _started = await _run(auto_done=True)

    assert isinstance(result, dict)
    assert attempt.diagnostic_report["auto_done"] is True


async def test_student_done_leaves_report_unstamped():
    result, _db, _sess, attempt, _started = await _run()

    assert isinstance(result, dict)
    assert "auto_done" not in attempt.diagnostic_report


async def test_empty_attempt_handler_maps_to_409():
    from apollo.api import empty_attempt_handler

    exc = EmptyAttemptError(session_id=11, attempt_id=99)
    response = await empty_attempt_handler(MagicMock(), exc)

    assert response.status_code == 409
    body = response.body.decode()
    assert '"empty_attempt"' in body
    assert '"session_id":11' in body
    assert '"attempt_id":99' in body
