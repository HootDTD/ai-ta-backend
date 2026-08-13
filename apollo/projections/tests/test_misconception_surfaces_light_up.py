"""Apollo P3.2 §2.5 — the teacher surfaces that were BUILT AND EMPTY.

Plan W3-B's verify-first inventory: four containers were already wired to read
the canonical artifact's `misconceptions` array and returned nothing only
because nothing wrote it. This module is the executable record that they needed
**no change** — `scorecard._watch_out`, `scorecard._watch_out_status`,
`classroom.top_misconceptions` and `mastery`'s ledger routing are all asserted
against the exact key shape `artifact_build._artifact_misconceptions` emits, so
a future rename on either side fails here instead of silently emptying a
teacher panel.

The ONE thing that did change is the level gate: because the array is persisted
from wrongness level 1 (`done._shadow_misconceptions`) rather than 3, the
classroom aggregate needed the `shadow` exclusion to stay on S10's rung. The
structural half of that is pinned here; the behavioural half (real SQL against
real Postgres) is `tests/database/test_wrongness_teacher_surfaces_postgres.py`.
"""

from __future__ import annotations

import inspect
from typing import Any

import pytest

from apollo.grading import artifact_build
from apollo.handlers import done
from apollo.overseer.topic_score import TopicCredit, TopicMisconception, TopicScoreResult
from apollo.persistence import attempt_history, models
from apollo.projections import classroom, mastery, scorecard
from apollo.projections import performance_insights as pi

pytestmark = pytest.mark.unit

# The three keys the builder emits and every reader below consumes.
_ARTIFACT_MISCONCEPTION_KEYS = {"canonical_key", "resolved", "evidence_span"}


def _artifact_misconceptions() -> list[dict[str, Any]]:
    """The array as the REAL builder produces it from a level-3 topic score."""
    result = TopicScoreResult(
        score=90,
        letter="A-",
        coverage_component=0.9,
        misconception_dock=0.0,
        topics=(
            TopicCredit(
                canonical_key="eq1",
                display_name="Bernoulli",
                credit=0.9,
                status="covered",
                weight=1.0,
                misconceptions=(
                    TopicMisconception(
                        canonical_key="eq1",
                        resolved=False,
                        dock_points=0.0,
                        evidence_span="pressure rises when speed rises",
                    ),
                ),
            ),
        ),
    )
    return artifact_build._artifact_misconceptions(result)


# --- scorecard: no edit needed ---------------------------------------------


def test_scorecard_watch_out_reads_artifact_misconceptions():
    """`_watch_out` maps `canonical_key` -> `key` and `evidence_span` -> `quote`
    straight off the builder's array. Verified against the real builder output,
    not a hand-written dict, so a key rename on either side fails here."""
    misconceptions = _artifact_misconceptions()
    assert {key for entry in misconceptions for key in entry} == _ARTIFACT_MISCONCEPTION_KEYS

    rendered = scorecard.render_scorecard(
        {"scores": {"composite": 0.9}, "misconceptions": misconceptions}
    )
    assert rendered["watch_out"] == [{"key": "eq1", "quote": "pressure rises when speed rises"}]


def test_scorecard_watch_out_status_stays_checked_without_the_empty_bank_marker():
    """The cold-start marker is a GRAPH-grader-era abstention field; a P3.2
    artifact carries none, so a populated `watch_out` reads `checked` and needs
    no note. (The empty-bank branch itself is unchanged — see test_scorecard.)"""
    rendered = scorecard.render_scorecard(
        {"scores": {"composite": 0.9}, "misconceptions": _artifact_misconceptions()}
    )
    assert rendered["watch_out_status"] == scorecard.WATCH_OUT_CHECKED
    assert rendered["watch_out_note"] is None


# --- classroom: the SQL shape, and the ONE change ---------------------------


def test_classroom_top_misconceptions_sql_shape_matches_artifact_keys():
    """The aggregate unrolls `grader_payload -> 'misconceptions'` and keys on
    `misc ->> 'canonical_key'` — the exact column and key the builder writes.
    Read off the SOURCE, because the SQL is inline in the coroutine: a rename on
    either side breaks this instead of silently emptying a teacher panel."""
    source = inspect.getsource(classroom.struggle_signals)
    assert "jsonb_array_elements(a.grader_payload -> 'misconceptions')" in source
    assert "misc ->> 'canonical_key' AS key" in source
    assert "canonical_key" in _ARTIFACT_MISCONCEPTION_KEYS


def test_classroom_excludes_shadow_and_resolved_entries():
    """The one additive change to the aggregate. `IS DISTINCT FROM 'true'` (not
    `!= 'true'`) is load-bearing: `->>` on an absent key is NULL, and `NULL !=
    'true'` is NULL, which would drop every pre-P3.2 row from the count."""
    predicate = classroom._TEACHER_VISIBLE_MISCONCEPTION_SQL
    assert "(misc ->> 'shadow') IS DISTINCT FROM 'true'" in predicate
    assert "(misc ->> 'resolved') IS DISTINCT FROM 'true'" in predicate


def test_the_shadow_marker_key_is_spelled_the_same_everywhere():
    """Three modules spell this key: the writer (`done`) and the two teacher
    readers. The readers re-spell rather than import it, to stay out of the
    `done` -> Neo4j import chain (the convention `scorecard.py` already uses for
    `artifact_build`'s status markers), so the pin lives here."""
    assert done.SHADOW_MISCONCEPTION_KEY == "shadow"
    assert classroom.SHADOW_MISCONCEPTION_KEY == done.SHADOW_MISCONCEPTION_KEY
    assert pi.SHADOW_MISCONCEPTION_KEY == done.SHADOW_MISCONCEPTION_KEY
    assert f"misc ->> '{done.SHADOW_MISCONCEPTION_KEY}'" in (
        classroom._TEACHER_VISIBLE_MISCONCEPTION_SQL
    )


def test_the_s9_cross_attempt_read_is_marker_agnostic():
    """S9 powers the LEVEL-2 carried challenge and the XP dedup, both of which
    read what levels 1-2 wrote. It must therefore INCLUDE shadow-marked entries
    — the opposite of the teacher surfaces. Structural proof: its SQL has no
    predicate on either marker, and its row mapping ignores unknown keys."""
    sql = attempt_history._PRIOR_WRONGNESS_FINDINGS_SQL.text
    assert done.SHADOW_MISCONCEPTION_KEY not in sql
    assert "IS DISTINCT FROM" not in sql


# --- mastery: no edit needed ------------------------------------------------


def test_mastery_routes_misconception_ledger_rows():
    """`misconception` is already an entity-bearing ledger status AND already a
    permitted mastery event kind, so a wrongness finding that lands in the node
    ledger routes with no change."""
    assert "misconception" in mastery._LEDGER_STATUSES_WITH_ENTITY
    assert "misconception" in models.MASTERY_EVENT_KINDS
    assert "corrected" in models.MASTERY_EVENT_KINDS

    row = type("Row", (), {"node_ledger": [{"status": "misconception", "canonical_key": "eq1"}]})()
    assert mastery._ledger_entity_keys(row) == ["eq1"]
