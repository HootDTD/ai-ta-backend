from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from apollo.clarification import resolve_turn


@pytest.mark.asyncio
async def test_resolve_pending_records_outcome_without_emergent_side_effect(monkeypatch):
    row = SimpleNamespace(id=3, candidate_key="eq.one", original_statement="original")
    monkeypatch.setattr(resolve_turn, "load_asked_waiting", AsyncMock(return_value=[row]))
    monkeypatch.setattr(
        resolve_turn,
        "rescore_clarification",
        lambda **kwargs: SimpleNamespace(outcome="refuted", confidence=0.9),
    )
    record = AsyncMock()
    monkeypatch.setattr(resolve_turn, "record_outcome", record)
    candidate = SimpleNamespace(canonical_key="eq.one", display_name="Equation one")

    db = object()
    await resolve_turn.resolve_pending_clarifications(
        db=db,
        attempt_id=8,
        student_message="correction",
        candidates=(candidate,),
        judge=object(),
        answered_turn=4,
        neo=object(),
    )

    record.assert_awaited_once_with(
        db,
        clarification_id=3,
        state="refuted",
        clarification_text="correction",
        answered_turn=4,
    )


@pytest.mark.asyncio
async def test_rescore_failure_leaves_pending_row_untouched(monkeypatch):
    row = SimpleNamespace(id=3, candidate_key="missing", original_statement="original")
    monkeypatch.setattr(resolve_turn, "load_asked_waiting", AsyncMock(return_value=[row]))

    def fail(**kwargs):
        raise RuntimeError("judge unavailable")

    monkeypatch.setattr(resolve_turn, "rescore_clarification", fail)
    record = AsyncMock()
    monkeypatch.setattr(resolve_turn, "record_outcome", record)
    await resolve_turn.resolve_pending_clarifications(
        db=object(),
        attempt_id=8,
        student_message="reply",
        candidates=(),
        judge=object(),
        answered_turn=4,
    )
    record.assert_not_awaited()
