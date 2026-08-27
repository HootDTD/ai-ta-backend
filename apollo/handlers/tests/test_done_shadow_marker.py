"""Apollo P3.2 W3-B — the `shadow` marker on sub-level-3 persisted findings.

Wave 2 made the misconception array persist from wrongness level 1, so that the
level-**2** carried challenge and the decision-7 XP dedup have a record to read.
But `projections/classroom.top_misconceptions` and the `repeated_misconception`
attention flag LATERAL over that SAME column, and S10 puts every teacher surface
on rung **3** — level 1 serves nothing. Without a discriminator the two readings
are indistinguishable, and a level-1 dark bake would light up a teacher panel.

The discriminator is a marker on the WRITE side, not a level check on each read:
entries written below level 3 carry `"shadow": true`, teacher-facing readers
exclude them, and the S9 cross-attempt read stays deliberately marker-agnostic
(it is exactly the level-1/2 population it needs). One writer, so the two
readings cannot drift; and the marker rides the free-form JSONB payload, so
there is no migration.

These tests drive `_shadow_misconceptions` directly — the end-to-end write path
through `handle_done` is `test_done_wrongness_shadow_persistence.py`.
"""

from __future__ import annotations

import pytest

from apollo.handlers import done
from apollo.overseer import wrongness
from apollo.persistence import attempt_history

pytestmark = pytest.mark.unit

_BASE_KEYS = {"canonical_key", "resolved", "evidence_span"}


def _finding(
    node_id: str, *, corroborated: bool = True, resolved: bool = False
) -> wrongness.WrongnessFinding:
    return wrongness.WrongnessFinding(
        node_id=node_id,
        quote=f"quote for {node_id}",
        contradicts="reference clause",
        kind="opposite_direction",
        corroborated=corroborated,
        resolved=resolved,
        apollo_elicited=resolved,
        would_ceiling=False,
    )


@pytest.mark.parametrize("level", [1, 2])
def test_levels_1_and_2_mark_every_entry_shadow(level: int):
    entries = done._shadow_misconceptions([_finding("eq1"), _finding("eq2")], level=level)
    assert entries is not None
    assert [entry[done.SHADOW_MISCONCEPTION_KEY] for entry in entries] == [True, True]


@pytest.mark.parametrize("level", [3, 4])
def test_level_3_and_above_write_no_marker(level: int):
    """Rung 3 is where the record IS the teacher-facing one, so the absence of
    the marker is what turns the surfaces on."""
    entries = done._shadow_misconceptions([_finding("eq1")], level=level)
    assert entries is not None
    assert all(done.SHADOW_MISCONCEPTION_KEY not in entry for entry in entries)


def test_level_0_still_persists_nothing():
    assert done._shadow_misconceptions([_finding("eq1")], level=0) is None


def test_the_marker_is_the_only_difference_between_the_rungs():
    """Nothing else about the array may move with the level — a rung change must
    not silently alter what is recorded, only who may read it."""
    shadowed = done._shadow_misconceptions([_finding("eq1"), _finding("eq2")], level=1) or []
    served = done._shadow_misconceptions([_finding("eq1"), _finding("eq2")], level=3) or []
    stripped = [
        {k: v for k, v in entry.items() if k != done.SHADOW_MISCONCEPTION_KEY} for entry in shadowed
    ]
    assert stripped == served
    assert all(set(entry) == _BASE_KEYS for entry in served)
    assert all(set(entry) == _BASE_KEYS | {done.SHADOW_MISCONCEPTION_KEY} for entry in shadowed)


def test_the_marker_is_literally_true_not_a_truthy_string():
    """The teacher predicates compare `misc ->> 'shadow'` against the TEXT
    `'true'`, which is what Postgres renders a JSON boolean as. A string
    `"True"` here would render as `True` and slip past every filter."""
    entries = done._shadow_misconceptions([_finding("eq1")], level=1) or []
    assert entries[0][done.SHADOW_MISCONCEPTION_KEY] is True


def test_the_resolved_population_is_marked_too_below_level_3():
    """The XP-dedup rows (`resolved AND apollo_elicited`) are persisted from
    level 1 as well, and they are just as invisible to a teacher at that rung."""
    entries = (
        done._shadow_misconceptions([_finding("eq1", corroborated=False, resolved=True)], level=1)
        or []
    )
    assert entries == [
        {
            "canonical_key": "eq1",
            "resolved": True,
            "evidence_span": "quote for eq1",
            done.SHADOW_MISCONCEPTION_KEY: True,
        }
    ]


def test_the_s9_read_is_marker_agnostic_by_construction():
    """S9 (`prior_wrongness_findings`) powers the LEVEL-2 carried challenge, so
    it must return the very entries the teacher surfaces exclude. Its SQL names
    neither marker and its row mapping projects a fixed four-key dict, so an
    extra JSONB key can neither filter it nor leak through it. W1-C owns that
    file; this asserts it needed no edit."""
    sql = attempt_history._PRIOR_WRONGNESS_FINDINGS_SQL.text
    assert done.SHADOW_MISCONCEPTION_KEY not in sql
    assert "IS DISTINCT FROM" not in sql
    # ...and the only misconception keys it reads are the three base ones.
    assert sorted({"canonical_key", "evidence_span", "resolved"}) == sorted(
        key for key in _BASE_KEYS if f"misc ->> '{key}'" in sql
    )
