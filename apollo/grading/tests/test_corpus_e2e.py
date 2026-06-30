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
    """The §6.9 row in v1: covered ×3 (2 plain + 1 audit-upgraded) + missing.

    The spec's §6.9 NARRATIVE (line 877) lists 'partial (velocity)', but v1's
    frozen §6.5 table has NO standalone-partial path for a shaky covered (the
    edge-gap partial is calibration-gated OFF, §6.2), so the would-be-partial
    surfaces as an audit-upgraded COVERED at <=0.75. Pin that deviation: EXACTLY
    ONE covered event is the shaky <=0.75 would-be-partial (cond.assumptions),
    the other two are full-confidence — so the corpus does not silently relabel a
    partial as an indistinct third covered."""
    fixture = _BY_NAME["bernoulli_capstone"]
    audited, events = fixture.run_chain()
    assert _event_kinds(events) == ("covered", "covered", "covered", "missing")
    assert audited.abstained is False
    covered = [e for e in events if e.event_kind.value == "covered"]
    shaky = [e for e in covered if e.confidence is not None and e.confidence <= 0.75]
    full = [e for e in covered if e.confidence is not None and e.confidence > 0.75]
    assert len(shaky) == 1  # the would-be-partial (audit-upgraded velocity/assumptions)
    assert shaky[0].canonical_key == "cond.assumptions"
    assert len(full) == 2  # the two plain full-confidence covered


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


def test_clarification_confirmed_node_avoids_abstention():
    """G2 proof: a node no deterministic tier matches would abstain the grader;
    a student-confirmed clarification resolves it (clarification@0.90) and the
    gate stays open. Deterministic — no live LLM/Neo4j/PG."""
    from apollo.grading.abstention import apply_abstention, unresolved_rate_of
    from apollo.ontology import KGGraph, build_node
    from apollo.resolution import resolve_attempt
    from apollo.resolution.candidates import Candidate

    node = build_node(
        node_type="condition",
        node_id="s_ambig",
        attempt_id=1,
        source="parser",
        content={"applies_when": "some vague phrasing no tier will match", "label": ""},
    )
    graph = KGGraph(nodes=[node])
    cands = (
        Candidate(
            canonical_key="cond.target",
            canon_key=1,
            node_type="condition",
            is_misconception=False,
            symbolic=None,
            aliases=(),
            display_name="the target idea",
            opposes_key=None,
            exact_aliases=(),
        ),
    )

    # WITHOUT clarification: the node is unresolved -> unresolved_rate gate abstains.
    base = resolve_attempt(graph, cands)
    assert unresolved_rate_of(base) > 0.35
    assert (
        apply_abstention(
            unresolved_rate=unresolved_rate_of(base),
            min_parser_confidence=1.0,
            normalization_confidence=1.0,
        ).abstained
        is True
    )

    # WITH a student-confirmed clarification: the node resolves -> gate stays open.
    confirmed = resolve_attempt(graph, cands, confirmed_resolutions={"s_ambig": "cond.target"})
    assert unresolved_rate_of(confirmed) <= 0.35
    assert (
        apply_abstention(
            unresolved_rate=unresolved_rate_of(confirmed),
            min_parser_confidence=1.0,
            normalization_confidence=1.0,
        ).abstained
        is False
    )
