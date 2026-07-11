"""MisconceptionEntry carries opposes; _from_row reads it (F-struct Task 4)."""

from __future__ import annotations

import pytest

from apollo.overseer.misconception_bank import MisconceptionEntry, _from_row
from apollo.persistence.models import Misconception

pytestmark = pytest.mark.unit


def _row(**kw) -> Misconception:
    base = dict(
        id=1,
        concept_id=2,
        code="nominal_for_real",
        description="d",
        confusion_pair_a=None,
        confusion_pair_b=None,
        trigger_phrases=[],
        probe_question="p",
        rt_steps=[],
    )
    base.update(kw)
    m = Misconception()
    for k, v in base.items():
        setattr(m, k, v)
    return m


def test_entry_has_opposes_field() -> None:
    e = MisconceptionEntry(
        id=1,
        concept_id=2,
        code="c",
        description="d",
        confusion_pair=None,
        trigger_phrases=(),
        probe_question="p",
        rt_steps=(),
        opposes="def.real_basis",
    )
    assert e.opposes == "def.real_basis"


def test_from_row_reads_opposes() -> None:
    assert _from_row(_row(opposes="def.real_basis")).opposes == "def.real_basis"


def test_from_row_opposes_none_default() -> None:
    assert _from_row(_row(opposes=None)).opposes is None
