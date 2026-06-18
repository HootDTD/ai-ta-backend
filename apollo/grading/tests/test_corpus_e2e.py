"""WU-4B3 §6.11 corpus — the executable chain, PURE (no container).

Drives every corpus fixture through ``build_audited_grade ->
convert_findings_to_events`` (deterministic resolution + injected ``audit_fn``,
NO live LLM, NO Neo4j, NO PG) and asserts the audited finding-kind multiset +
the produced event-kind multiset match the spec row. The §6.9 Bernoulli capstone,
the abstention row, and the polar near-miss are asserted on their own so a
regression is legible.
"""

from __future__ import annotations

import pytest

from apollo.grading.fixtures.corpus import CORPUS

_BY_NAME = {f.name: f for f in CORPUS}


def _finding_kinds(audited) -> tuple[str, ...]:
    return tuple(sorted(f.kind.value for f in audited.findings))


def _event_kinds(events) -> tuple[str, ...]:
    return tuple(sorted(e.event_kind.value for e in events))


@pytest.mark.parametrize("fixture", CORPUS, ids=[f.name for f in CORPUS])
def test_corpus_findings_match_spec(fixture):
    audited, _ = fixture.run_chain()
    assert _finding_kinds(audited) == fixture.expected_finding_kinds


@pytest.mark.parametrize("fixture", CORPUS, ids=[f.name for f in CORPUS])
def test_corpus_events_match_spec(fixture):
    _, events = fixture.run_chain()
    assert _event_kinds(events) == fixture.expected_event_kinds


def test_corpus_has_thirteen_fixtures():
    """12 §6.11 rows + the §6.9 capstone."""
    assert len(CORPUS) == 13
    assert "bernoulli_capstone" in _BY_NAME


def test_bernoulli_capstone_events():
    """The §6.9 row explicitly: covered ×3 (2 plain + 1 audit-upgraded) + missing."""
    fixture = _BY_NAME["bernoulli_capstone"]
    audited, events = fixture.run_chain()
    assert _event_kinds(events) == ("covered", "covered", "covered", "missing")
    assert audited.abstained is False


def test_high_unresolved_abstains_no_events():
    """The abstention fixture: abstained True AND convert_findings_to_events == ()."""
    fixture = _BY_NAME["high_unresolved_abstains"]
    audited, events = fixture.run_chain()
    assert audited.abstained is True
    assert events == ()


def test_polar_near_miss_resolves_to_misc_not_reference():
    """The contradiction is keyed on the misc.* key, never the lexically-close
    reference key (the §6.11 anti-false-positive row)."""
    fixture = _BY_NAME["polar_near_miss"]
    audited, events = fixture.run_chain()
    misconception_keys = [e.canonical_key for e in events if e.event_kind.value == "misconception"]
    assert misconception_keys == ["misc.pressure_speed"]
    assert "cond.pressure_speed" not in misconception_keys
