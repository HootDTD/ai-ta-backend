"""§4 regression + invariant gates. One test (or thin class) per gate ID.

| Gate | Spec §4 row | Threshold | Asserted here by |
|---|---|---|---|
| **G-FIX** | Self-correction regression: attempts **86, 167, 124** as named fixtures | 167 not selected in any of 4 samples | `test_gate_G_FIX_*` |
| **G-CEL** | Celebrated-node invariant | 0 consequences on ``understood`` + verified-quote nodes, **by construction** | `test_gate_G_CEL_*` |
| **P-1** | A ``min()`` can never produce a D or an F | property | `test_gate_P1_*` |
| **P-2** | Applied once, never repeated subtraction | property | `test_gate_P2_*` |
| **P-3** | No double jeopardy with INTERACTION5's flat 0.5 aside cap | property | `test_gate_P3_*` |

**G-FIX is the reason S2 became S2′.** P3.1 §6's original suspect rule
(``state == 'conflicting' AND credit >= 0.6``) selects exactly attempts **86**
(a zero-transcript artifact) and **167** (a student self-correcting after two
probes) on this corpus — **2 false positives, 0 true ones**. The tests below run
BOTH predicates over the committed prod ledgers so the 0/2 claim is executable
rather than cited, and so a future edit that quietly relaxes S2′ back toward S2
fails here.

167's ledger is worth reading before changing anything: its ``q19_map_roots``
row is ``conflicting`` at credit 0.9, and its LAST evidence entry is a clean
claim — the self-correction sits in the middle. S2′ keys on evidence recency and
``corrected_later``, never on the sticky final state, which is why it refuses it
twice over.

Underlying slice evidence: ``apollo/overseer/tests/test_wrongness_predicate.py``
(the S2′ truth table), ``.../test_topic_score_ceiling.py`` (P-1/P-2/P-3 on the
scorer), ``campaign/tests/test_turn_replay_fixtures.py`` (the fixtures' PII and
schema gates).
"""

from __future__ import annotations

import pytest

from apollo.ontology import KGGraph
from apollo.overseer import wrongness
from apollo.overseer.rubric import score_to_letter
from apollo.overseer.tests._topic_score_corpus import equation
from apollo.overseer.topic_score import (
    CEILING_UNCORRECTED,
    compute_centrality,
    compute_topic_score,
)
from campaign import turn_replay

pytestmark = pytest.mark.unit

_FIXTURES = {fixture.name: fixture for fixture in turn_replay.load_fixtures()}
_SELF_CORRECTION = "So I guess I was wrong about governance"


# --------------------------------------------------------------------------- #
# G-FIX — the three named regression fixtures                                  #
# --------------------------------------------------------------------------- #


def _s2_naive(fixture) -> set[str]:
    """P3.1 §6's ORIGINAL suspect rule: sticky state + credit floor.

    Reimplemented here on purpose — it is the rule the amendment RETIRED, so
    there is no production symbol to import, and a local copy is the only way to
    keep the comparison executable.
    """
    credits = fixture.recorded.get("topic_credits") or {}
    return {
        row.reference_node_id
        for row in fixture.recorded_ledger_rows()
        if row.state == "conflicting"
        and float(credits.get(row.reference_node_id, 0.0)) >= wrongness.MIN_CORROBORATED_CREDIT
    }


def _s2_prime(fixture) -> tuple[wrongness.WrongnessFinding, ...]:
    """S2′ over the SAME recorded ledger, with the fixture's own second reader."""
    verdicts = fixture.adjudicator_output.get("verdicts") or []
    second_reader = {
        verdict["node_id"]: {
            "contradicted": bool(verdict.get("contradicted")),
            "corrected_later": bool(verdict.get("corrected_later")),
            "prompted": bool(verdict.get("prompted")),
        }
        for verdict in verdicts
        if verdict.get("node_id")
    }
    graded = frozenset(
        node.node_id
        for node in fixture.problem.to_kg_graph(attempt_id=-1).nodes
        if node.node_type in {"equation", "condition", "simplification", "procedure_step"}
    )
    return wrongness.select_findings(
        findings=wrongness.ledger_findings(fixture.recorded_ledger_rows()),
        credits={
            key: float(value)
            for key, value in (fixture.recorded.get("topic_credits") or {}).items()
        },
        second_reader=second_reader,
        graded_node_ids=graded,
        raw_score=int(fixture.recorded.get("served_score") or 0),
    )


def test_gate_G_FIX_the_retired_rule_really_does_select_86_and_167():
    """The control. Without this, "S2′ selects neither" would be satisfied by a
    predicate that selects nothing anywhere — including the true positives."""
    selected = {name: _s2_naive(fixture) for name, fixture in _FIXTURES.items()}

    assert selected["attempt_086_zero_transcript"] == {"q19_map_roots"}
    assert selected["attempt_167_self_correction"] == {"q19_map_roots"}
    # 124's conflicting node sits at credit 0.3 — below the floor — so even the
    # retired rule spares it. Recorded so the 0/2 arithmetic stays legible.
    assert selected["attempt_124_conflicting_graded"] == set()


@pytest.mark.parametrize("name", sorted(_FIXTURES))
def test_gate_G_FIX_s2_prime_selects_nothing_on_any_named_fixture(name):
    """0 selections where the retired rule scored 2 false positives — including
    attempt 167, which **must not be selected in any of 4 samples**. Playback is
    deterministic, so one run IS all four."""
    assert _s2_prime(_FIXTURES[name]) == ()


def test_gate_G_FIX_attempt_167_keeps_the_self_correction_verbatim():
    """The regression IS the sentence. A future PII scrub that paraphrases it
    would silently gut this gate, so the substring is pinned here as well as in
    the fixture suite."""
    fixture = _FIXTURES["attempt_167_self_correction"]
    quotes = [
        entry.get("quote", "")
        for row in fixture.ledger_payload
        for entry in row.get("evidence") or []
    ]

    assert any(_SELF_CORRECTION in quote for quote in quotes)


def test_gate_G_FIX_167_is_refused_twice_over_even_if_the_producer_had_labelled_it():
    """The forward-looking half. Prod ran pre-P3.2, so the recorded ledger
    carries no label at all — which means the test above would also pass on a
    predicate that simply never fires. Label the self-correction by hand and
    show S2′ still refuses it, for BOTH of its independent reasons.
    """
    latest_and_corrected = wrongness.select_findings(
        findings=(
            wrongness.LedgerFinding(
                node_id="q19_map_roots",
                wrongness=wrongness.WRONGNESS_MATERIAL,
                quote=_SELF_CORRECTION,
                contradicts="governance is a distinct PAPA root",
                kind="reversal",
                turn_id=9,
                is_latest_evidence=True,
                state="conflicting",
                times_asked=2,
                last_asked_turn=7,
            ),
        ),
        credits={"q19_map_roots": 0.9},
        second_reader={
            "q19_map_roots": {"contradicted": True, "corrected_later": True, "prompted": True}
        },
        graded_node_ids={"q19_map_roots"},
        raw_score=90,
    )
    (finding,) = latest_and_corrected
    assert finding.corroborated is False, "corrected_later must veto corroboration"
    assert finding.resolved is True
    assert finding.apollo_elicited is True, "Apollo asked first — this is the XP-bonus shape"

    superseded = wrongness.select_findings(
        findings=(
            wrongness.LedgerFinding(
                node_id="q19_map_roots",
                wrongness=wrongness.WRONGNESS_MATERIAL,
                quote=_SELF_CORRECTION,
                contradicts="governance is a distinct PAPA root",
                kind="reversal",
                turn_id=9,
                is_latest_evidence=False,
                state="conflicting",
                times_asked=2,
                last_asked_turn=7,
            ),
        ),
        credits={"q19_map_roots": 0.9},
        second_reader={
            "q19_map_roots": {"contradicted": True, "corrected_later": False, "prompted": True}
        },
        graded_node_ids={"q19_map_roots"},
        raw_score=90,
    )
    assert superseded[0].corroborated is False, "a superseded quote must never corroborate"


async def test_gate_G_FIX_attempt_86_is_refused_by_the_empty_attempt_guard_first(monkeypatch):
    """Defect I7: 86 has ZERO student messages and still recorded A(95) on a
    0.95-credit node. The P0.1 guard refuses it before any wrongness machinery
    runs, at every rung — which is why "final state ``conflicting`` ⇒ dock"
    scored 0/2 rather than 1/2."""
    fixture = _FIXTURES["attempt_086_zero_transcript"]
    assert fixture.student_turn_ids == ()

    for level in (0, 1, 2, 3, 4):
        monkeypatch.setenv("APOLLO_WRONGNESS_LEVEL", str(level))
        replay = await turn_replay.replay_recorded(fixture)
        assert replay.refusal == turn_replay.EMPTY_ATTEMPT_REFUSAL
        assert replay.turns == ()
        assert replay.grade is None


async def test_gate_G_FIX_attempt_124_recorded_shape_is_asserted_as_is(monkeypatch):
    """124 is the "conflicting on a GRADED node" fixture, and its recorded shape
    is the point: a conflicting node at credit 0.3 — below S2′'s floor — beside a
    credited one. Pinned unmodified so a fixture edit that "tidies" it away
    cannot pass silently."""
    fixture = _FIXTURES["attempt_124_conflicting_graded"]
    rows = {row.reference_node_id: row for row in fixture.recorded_ledger_rows()}

    assert rows["q2_four_impairments"].state == "conflicting"
    assert fixture.recorded["topic_credits"]["q2_four_impairments"] == 0.3
    assert fixture.recorded["served_score"] == 60
    assert fixture.recorded["served_letter"] == "C"

    monkeypatch.setenv("APOLLO_WRONGNESS_LEVEL", "2")
    replay = await turn_replay.replay_recorded(fixture)
    assert replay.grade is not None
    assert sum(turn.done_gate_fired for turn in replay.turns) == 0


# --------------------------------------------------------------------------- #
# G-CEL — the celebrated-node invariant                                        #
# --------------------------------------------------------------------------- #


def _celebrated_row(node_id: str) -> object:
    """``understood``, probed, with a verbatim quote and NO wrongness label."""

    class _Row:
        reference_node_id = node_id
        state = "understood"
        times_asked = 1
        last_asked_turn = 2
        evidence = [{"turn_id": 3, "quote": "The pressure drops where the flow speeds up."}]

    return _Row()


def test_gate_G_CEL_a_celebrated_node_yields_no_finding_by_construction():
    """ "Celebrated" = ``understood`` + a verified quote + no contradiction. The
    invariant is structural, not conditional: ``select_findings`` skips every
    ``wrongness == "none"`` entry with a ``continue``, so a celebrated node
    cannot produce a finding to have a consequence attached to."""
    findings = wrongness.ledger_findings([_celebrated_row("eq1"), _celebrated_row("c1")])

    assert len(findings) == 2
    assert {finding.wrongness for finding in findings} == {wrongness.WRONGNESS_NONE}
    assert {finding.state for finding in findings} == {"understood"}
    assert (
        wrongness.select_findings(
            findings=findings,
            credits={"eq1": 1.0, "c1": 1.0},
            # Even a corroborator that volunteers "contradicted" cannot create
            # one: the tally is the sole PRODUCER and the adjudicator is only
            # ever a corroborator.
            second_reader={
                "eq1": {"contradicted": True, "corrected_later": False, "prompted": True},
                "c1": {"contradicted": True, "corrected_later": False, "prompted": True},
            },
            graded_node_ids={"eq1", "c1"},
            raw_score=100,
        )
        == ()
    )


def test_gate_G_CEL_a_celebrated_node_is_never_offered_to_the_corroborator():
    """The other half: no candidate map entry, so the adjudicator is never even
    ASKED about a celebrated node — its schema, prompt and user message stay
    byte-identical for that attempt."""
    findings = wrongness.ledger_findings([_celebrated_row("eq1")])

    assert wrongness.candidate_quotes(findings, graded_node_ids={"eq1"}) == {}


def test_gate_G_CEL_a_contradicted_node_is_not_celebrated_and_re_enters_jurisdiction():
    """The invariant must not degenerate into "nothing is ever selected". A node
    carrying a material label is by definition NOT celebrated, and it is
    selected — which is exactly the P3.1 §6 blind spot P3.2 repairs (a paragraph
    dump's nodes are all ``understood`` + quoted, so the narrow audit is
    forbidden to touch them; a contradicted node is not)."""

    class _Contested:
        reference_node_id = "eq1"
        state = "understood"
        times_asked = 1
        last_asked_turn = 2
        evidence = [
            {
                "turn_id": 4,
                "quote": "Pressure rises wherever the flow speeds up.",
                "wrongness": wrongness.WRONGNESS_MATERIAL,
                "contradicts": "P + 0.5*rho*v**2 = const",
                "kind": "reversal",
            }
        ]

    findings = wrongness.ledger_findings([_Contested()])
    (selected,) = wrongness.select_findings(
        findings=findings,
        credits={"eq1": 1.0},
        second_reader={"eq1": {"contradicted": True, "corrected_later": False, "prompted": True}},
        graded_node_ids={"eq1"},
        raw_score=100,
    )

    assert selected.corroborated is True
    assert wrongness.candidate_quotes(findings, graded_node_ids={"eq1"}) == {
        "eq1": "Pressure rises wherever the flow speeds up."
    }


# --------------------------------------------------------------------------- #
# P-1 / P-2 / P-3 — the three student-protective ceiling properties            #
# --------------------------------------------------------------------------- #


def _graded_graph(count: int) -> KGGraph:
    return KGGraph(nodes=[equation(f"eq.{i}", f"E{i}") for i in range(count)], edges=[])


def _finding(node_id: str, *, resolved: bool = False) -> wrongness.WrongnessFinding:
    return wrongness.WrongnessFinding(
        node_id=node_id,
        quote=f"student words about {node_id}",
        contradicts="the reference clause",
        kind="reversal",
        corroborated=True,
        resolved=resolved,
        apollo_elicited=True,
        would_ceiling=True,
    )


def _score(nodes, credits, **kwargs):
    graph = KGGraph(nodes=nodes, edges=[])
    return compute_topic_score(
        coverage={
            "per_step": {node.node_id: "covered" for node in nodes},
            "procedure_scores": credits,
        },
        reference_nodes=nodes,
        centrality=compute_centrality(graph),
        **kwargs,
    )


@pytest.mark.parametrize("count", [1, 2, 3, 5])
def test_gate_P1_the_ceiling_can_never_manufacture_a_d_or_an_f(count):
    """A ``min()`` bounded at ``CEILING_UNCORRECTED`` cannot push any attempt
    below B+ that was not already there. Asserted over the whole 0-100 raw range
    rather than on the fixture, because the property is arithmetic."""
    assert score_to_letter(CEILING_UNCORRECTED) == "B+"
    for raw in range(101):
        served = min(raw, CEILING_UNCORRECTED)
        if score_to_letter(served) in {"D+", "D", "D-", "F"}:
            assert score_to_letter(raw) == score_to_letter(served), raw

    nodes = _graded_graph(count).nodes
    ceilinged = _score(
        nodes,
        {node.node_id: 1.0 for node in nodes},
        misconceptions={nodes[0].node_id: _finding(nodes[0].node_id)},
        ceiling_active=True,
    )
    assert ceilinged.letter not in {"D+", "D", "D-", "F"}
    assert ceilinged.score >= CEILING_UNCORRECTED - 0


def test_gate_P2_the_ceiling_is_applied_once_not_repeated_subtraction():
    """Three uncorrected findings cost exactly what one costs. A per-finding
    subtraction would compound into the D/F band P-1 forbids."""
    nodes = _graded_graph(3).nodes
    credits = {node.node_id: 1.0 for node in nodes}

    one = _score(nodes, credits, misconceptions={"eq.0": _finding("eq.0")}, ceiling_active=True)
    three = _score(
        nodes,
        credits,
        misconceptions={node.node_id: _finding(node.node_id) for node in nodes},
        ceiling_active=True,
    )

    assert one.score == three.score == CEILING_UNCORRECTED
    assert one.misconception_dock == three.misconception_dock
    # ...and the attributed lines still sum EXACTLY to the dock, so the teacher
    # view can never show a total that disagrees with its own breakdown.
    for result in (one, three):
        lines = [
            misconception.dock_points
            for topic in result.topics
            for misconception in topic.misconceptions
        ]
        assert sum(lines) == pytest.approx(result.misconception_dock, abs=1e-9)


def test_gate_P3_no_double_jeopardy_with_the_interaction5_aside_cap():
    """INTERACTION5 caps a Hoot-assisted topic at 0.5 credit. S2′'s floor is
    0.6. The two levers therefore cannot stack ON THE SAME NODE — a capped node
    is below the floor and contributes no finding at all. Asserted on the
    predicate (where the floor lives), not merely on the arithmetic."""
    capped = wrongness.LedgerFinding(
        node_id="eq.0",
        wrongness=wrongness.WRONGNESS_MATERIAL,
        quote="a claim about eq.0",
        contradicts="the reference clause",
        kind="reversal",
        turn_id=4,
        is_latest_evidence=True,
        state="understood",
        times_asked=1,
        last_asked_turn=2,
    )
    reader = {"eq.0": {"contradicted": True, "corrected_later": False, "prompted": True}}

    (at_cap,) = wrongness.select_findings(
        findings=(capped,),
        credits={"eq.0": 0.5},  # exactly INTERACTION5's flat aside cap
        second_reader=reader,
        graded_node_ids={"eq.0"},
        raw_score=100,
    )
    (above_floor,) = wrongness.select_findings(
        findings=(capped,),
        credits={"eq.0": wrongness.MIN_CORROBORATED_CREDIT},
        second_reader=reader,
        graded_node_ids={"eq.0"},
        raw_score=100,
    )

    assert at_cap.corroborated is False
    assert at_cap.would_ceiling is False
    assert above_floor.corroborated is True
    assert 0.5 < wrongness.MIN_CORROBORATED_CREDIT, (
        "the aside cap reached the corroboration floor — the two levers can now stack"
    )
