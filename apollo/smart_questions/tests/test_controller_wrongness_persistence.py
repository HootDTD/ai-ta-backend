"""S2 — the persisted evidence entry, in both of its two shapes.

`app.question_opportunities.evidence` is a free-form JSONB array
(`__evidence__array_check` asserts only ``jsonb_typeof = 'array'``), so P3.2
needs **no migration**: the finding rides as a tagged, dedup-appended entry.
The contract has two halves and both are load-bearing:

* ``wrongness == "none"`` writes exactly ``{"turn_id", "quote"}`` — byte-identical
  to every entry written before P3.2, so level 0 leaves the column untouched and
  the dedup rule keeps matching historical rows;
* otherwise it writes ``{"turn_id", "quote", "wrongness", "contradicts", "kind"}``.

Every downstream reader of this column keys on ``quote`` alone, so both shapes
must read identically — pinned here against the real `done` helpers rather than
a copy of them.
"""

from __future__ import annotations

import pytest

from apollo.handlers.done import _latest_student_quote, _probed_node_ids
from apollo.smart_questions import controller
from apollo.smart_questions.unified import Contradiction, EvidenceQuote, TallyUpdate

pytestmark = pytest.mark.unit


class _Row:
    """A `QuestionOpportunity`-shaped stand-in (the columns these readers touch)."""

    def __init__(self, node_id="n1", state="missing", times_asked=0, evidence=None):
        self.reference_node_id = node_id
        self.state = state
        self.times_asked = times_asked
        self.evidence = evidence if evidence is not None else []


class _DB:
    def __init__(self):
        self.added: list[object] = []

    def add(self, row):
        self.added.append(row)


def _apply(*updates, rows=None):
    return controller._apply_tally_updates(
        _DB(),
        course_id=1,
        session_id=2,
        attempt_id=3,
        rows=list(rows or []),
        updates=tuple(updates),
    )


def _clean_update(**overrides):
    values = {
        "node_id": "n1",
        "status": "understood",
        "evidence": EvidenceQuote(0, "a price rise lowers demand"),
    }
    values.update(overrides)
    return TallyUpdate(**values)


def _wrong_update(**overrides):
    values = {
        "node_id": "n1",
        "status": "conflicting",
        "evidence": EvidenceQuote(2, "raising the price raises demand too"),
        "wrongness": "contradicts_material",
        "contradiction": Contradiction(
            reference_clause="a price rise lowers demand", kind="inverted relationship"
        ),
    }
    values.update(overrides)
    return TallyUpdate(**values)


# --------------------------------------------------------------------------- #
# The two shapes
# --------------------------------------------------------------------------- #
def test_none_wrongness_writes_two_key_entry_byte_identical():
    row = _Row()
    _apply(_clean_update(), rows=[row])
    assert row.evidence == [{"turn_id": 0, "quote": "a price rise lowers demand"}]
    assert list(row.evidence[0]) == ["turn_id", "quote"]


def test_material_wrongness_writes_tagged_entry():
    row = _Row()
    _apply(_wrong_update(), rows=[row])
    assert row.evidence == [
        {
            "turn_id": 2,
            "quote": "raising the price raises demand too",
            "wrongness": "contradicts_material",
            "contradicts": "a price rise lowers demand",
            "kind": "inverted relationship",
        }
    ]
    assert row.state == "conflicting"


def test_a_label_without_a_contradiction_still_writes_the_plain_entry():
    """Defence in depth: `_decode_updates` already drops such an update, so this
    combination can only arrive from a hand-built `TallyUpdate`. It must never
    produce a half-tagged entry that a downstream reader would treat as a
    finding with no clause to point at."""
    row = _Row()
    _apply(_wrong_update(contradiction=None), rows=[row])
    assert list(row.evidence[0]) == ["turn_id", "quote"]


def test_tagged_entry_dedup_append():
    """Dedup is on the WHOLE dict, unchanged from pre-P3.2: re-asserting the same
    finding appends nothing, but the same quote later carrying a label is a new
    observation and does append."""
    row = _Row()
    _apply(_wrong_update(), _wrong_update(), rows=[row])
    assert len(row.evidence) == 1
    plain = TallyUpdate(
        node_id="n1",
        status="tentative",
        evidence=EvidenceQuote(2, "raising the price raises demand too"),
    )
    _apply(plain, rows=[row])
    assert len(row.evidence) == 2
    assert list(row.evidence[1]) == ["turn_id", "quote"]


def test_a_new_row_minted_this_turn_carries_the_tagged_entry():
    by_id = _apply(_wrong_update())
    assert by_id["n1"].evidence[0]["wrongness"] == "contradicts_material"


# --------------------------------------------------------------------------- #
# Downstream readers are shape-agnostic
# --------------------------------------------------------------------------- #
def test_done_latest_student_quote_still_reads_tagged_entries():
    row = _Row()
    _apply(_clean_update(), _wrong_update(), rows=[row])
    assert len(row.evidence) == 2
    assert _latest_student_quote(row.evidence) == "raising the price raises demand too"


def test_done_probed_node_ids_unchanged_by_tagged_entries():
    plain, tagged = _Row(node_id="a"), _Row(node_id="b")
    _apply(_clean_update(node_id="a"), rows=[plain])
    _apply(_wrong_update(node_id="b"), rows=[tagged])
    assert _probed_node_ids([plain, tagged]) == frozenset({"a", "b"})


def test_tally_state_rebuild_ignores_the_extra_keys():
    """`_build_tally_state` feeds the next turn's payload; a tagged entry must
    round-trip as the same `EvidenceQuote` a plain one does."""
    row = _Row()
    _apply(_wrong_update(), rows=[row])
    assert controller._evidence_rows(row.evidence) == (
        EvidenceQuote(2, "raising the price raises demand too"),
    )
