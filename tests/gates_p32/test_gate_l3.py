"""§4 gates for LEVEL 3 — surfacing without scoring. One test per gate ID.

| Gate | Spec §4 row | Threshold | Asserted here by |
|---|---|---|---|
| **G-L3** | Level-3 inertness **including the rubric absent-axis flip** (§2.5) | whole-blob exact | `test_gate_G_L3_*` |
| **G-L3b** | Contradiction named only at the corroborated rung; reveal count stays within ``MAX_REFERENCE_TEXT_REVEALS = 2`` | 0 violations | `test_gate_G_L3b_*` |

**The sharpening (plan R6), stated so nobody reads §4's shorthand as violated.**
"Whole-blob byte-identical" is literally unsatisfiable at level 3 — populating
``topics[].misconceptions`` IS level 3. Exactly ONE additive path is permitted
(``_harness.PERMITTED_ADDITIVE_PATHS``); everything outside it is compared
UNPROJECTED, and three fields are additionally asserted verbatim:

* ``score_details["llm_rubric"]`` — the §2.5 absent-axis hazard. The instant
  anything feeds ``_attempt_misconception_scores``,
  ``AXIS_WEIGHTS["misconception_corrected"] = 0.05`` flips from absent to
  present, every other axis rescales by 0.95, and ``llm_rubric.overall`` moves
  (100/100/100 + an unresolved 50 -> 97.5, not 100) — one rung BELOW the level
  labelled "score". A projected comparison would hide it, so this one is
  literal.
* ``topic_score["misconception_dock"] == 0.0``.
* ``served_overall`` unchanged.

Level 4 (dark, unreachable — nothing sets it) is asserted to be the ONLY rung
where the fingerprint may differ.

Underlying slice evidence this gate layer re-asserts:
``apollo/handlers/tests/test_done_rubric_axis_inertness.py`` (the axis),
``apollo/handlers/tests/test_done_wrongness_containers.py`` (the containers),
``apollo/overseer/tests/test_topic_score_ceiling.py`` (the arithmetic).
"""

from __future__ import annotations

import pytest

from apollo.handlers.tests import _wrongness_fixtures as wf
from apollo.ontology import KGGraph
from apollo.overseer.narrative_consistency import MAX_REFERENCE_NAME_QUOTES
from apollo.overseer.rubric import AXIS_WEIGHTS
from apollo.overseer.tests._topic_score_corpus import condition, equation
from apollo.overseer.topic_narrative import build_topic_narrative_prompt
from apollo.overseer.topic_score import (
    MAX_REFERENCE_TEXT_REVEALS,
    compute_centrality,
    compute_topic_score,
)
from apollo.overseer.wrongness import WrongnessFinding
from tests.gates_p32._harness import blob, run_gate_done, score_fingerprint

pytestmark = pytest.mark.unit

_INERT_LEVELS = (0, 1, 2, 3)


# --------------------------------------------------------------------------- #
# G-L3 — inertness under the projection                                        #
# --------------------------------------------------------------------------- #


async def test_gate_G_L3_fingerprint_equal_across_levels_0_through_3(monkeypatch):
    runs = {level: await run_gate_done(monkeypatch, level=level) for level in _INERT_LEVELS}
    fingerprints = {level: score_fingerprint(run.score_details) for level, run in runs.items()}

    assert len(set(fingerprints.values())) == 1, fingerprints


async def test_gate_G_L3_llm_rubric_is_compared_verbatim_not_projected(monkeypatch):
    """§2.5's absent-axis hazard, asserted where it would actually surface."""
    baseline = await run_gate_done(monkeypatch, level=0)

    for level in (1, 2, 3, 4):
        run = await run_gate_done(monkeypatch, level=level)
        assert blob(run.score_details["llm_rubric"]) == blob(baseline.score_details["llm_rubric"])
        assert run.score_details["llm_rubric"]["misconception_corrected"]["present"] is False
        assert run.score_details["llm_rubric"]["overall"] == {"score": 100, "letter": "A+"}

    assert "misconception_corrected" in AXIS_WEIGHTS, "the axis this gate guards no longer exists"


@pytest.mark.parametrize("level", _INERT_LEVELS)
async def test_gate_G_L3_misconception_dock_stays_zero_below_level_4(monkeypatch, level):
    run = await run_gate_done(monkeypatch, level=level)

    assert run.topic_score["misconception_dock"] == 0.0
    assert all(misconception["dock_points"] == 0.0 for misconception in run.topic_misconceptions)


async def test_gate_G_L3_served_overall_never_moves_below_level_4(monkeypatch):
    baseline = await run_gate_done(monkeypatch, level=0)

    for level in (1, 2, 3):
        run = await run_gate_done(monkeypatch, level=level)
        assert (
            run.diagnostic_report["served_overall"] == baseline.diagnostic_report["served_overall"]
        )
        assert (
            run.student_response["rubric"]["overall"]
            == (baseline.student_response["rubric"]["overall"])
        )


async def test_gate_G_L3_level_4_is_the_only_rung_the_fingerprint_may_differ_at(monkeypatch):
    """The dark ceiling. Built, unreachable (nothing sets 4), and proved to be
    the ONLY place the projection is allowed to disagree — which is what makes
    the levels-0-3 equality above a claim about the ladder rather than about
    this fixture."""
    baseline = await run_gate_done(monkeypatch, level=0)
    ceiling = await run_gate_done(monkeypatch, level=4)

    assert score_fingerprint(ceiling.score_details) != score_fingerprint(baseline.score_details)
    assert ceiling.topic_score["score"] == 84
    assert ceiling.topic_score["letter"] == "B+"
    assert ceiling.topic_score["misconception_dock"] == 16.0


async def test_gate_G_L3_containers_populate_at_3_and_nowhere_below(monkeypatch):
    """The positive half — level 3 must actually DO something, or "inert" is
    being proved about a feature that never turned on."""
    for level in (0, 1, 2):
        run = await run_gate_done(monkeypatch, level=level)
        assert run.topic_misconceptions == []
        assert run.served_misconceptions == []
        assert run.student_response["scorecard"]["watch_out"] == []

    surfaced = await run_gate_done(monkeypatch, level=3)
    assert [m["canonical_key"] for m in surfaced.topic_misconceptions] == ["eq1"]
    assert [m["canonical_key"] for m in surfaced.served_misconceptions] == ["eq1"]
    assert surfaced.student_response["scorecard"]["watch_out"]


# --------------------------------------------------------------------------- #
# G-L3b — named only at the corroborated rung, inside the shared reveal budget  #
# --------------------------------------------------------------------------- #


def _mixed_rung_ledger() -> tuple[wf.LedgerRow, ...]:
    """``eq1`` stands uncorrected (corroborated); ``c1`` was fixed later.

    Both are graded, both credited 1.0, both carry a material label and a
    verbatim span — so the ONLY thing separating them is the second reader's
    ``corrected_later``, which is exactly the rung boundary G-L3b polices.
    """
    return (
        wf.LedgerRow("eq1", evidence=wf.tagged_evidence()),
        wf.LedgerRow(
            "c1",
            evidence=wf.tagged_evidence(turn_id=5, quote="Steady flow only."),
        ),
    )


def _mixed_rung_second_reader() -> dict[str, dict[str, bool]]:
    return {
        "eq1": {"contradicted": True, "corrected_later": False, "prompted": True},
        "c1": {"contradicted": True, "corrected_later": True, "prompted": True},
    }


async def test_gate_G_L3b_only_the_corroborated_rung_is_ever_named(monkeypatch):
    """A resolved finding is REAL (it earns the decision-7 XP bonus and it is
    recorded internally) and is still never named to anyone. Naming it would be
    the celebrate-then-punish trust break S2′ exists to prevent."""
    run = await run_gate_done(
        monkeypatch,
        level=3,
        ledger=_mixed_rung_ledger(),
        wrongness_map=_mixed_rung_second_reader(),
    )

    assert [m["canonical_key"] for m in run.topic_misconceptions] == ["eq1"]
    assert [m["canonical_key"] for m in run.served_misconceptions] == ["eq1"]
    assert [entry["key"] for entry in run.student_response["scorecard"]["watch_out"]] == ["eq1"]
    # ...while the INTERNAL record keeps both, because the XP dedup and the L2c
    # carry both key on the resolved population (wave-2 F-17). Persisted is a
    # SUPERSET of served; only the served half is student-facing.
    assert sorted(entry["canonical_key"] for entry in run.shadow_misconceptions or ()) == [
        "c1",
        "eq1",
    ]


async def test_gate_G_L3b_an_uncorroborated_finding_names_nothing(monkeypatch):
    """Fail-safe = miss: the corroborator's silence removes a consequence, never
    creates one, so an absent second-reader row surfaces nothing at all."""
    run = await run_gate_done(monkeypatch, level=3, wrongness_map={})

    assert run.topic_misconceptions == []
    assert run.served_misconceptions == []
    assert run.student_response["scorecard"]["watch_out"] == []


def _low_credit_graph() -> KGGraph:
    return KGGraph(
        nodes=[
            equation("eq.one", "First"),
            equation("eq.two", "Second"),
            condition("c.one", "steady flow"),
            condition("c.two", "incompressible"),
        ],
        edges=[],
    )


def _finding(node_id: str) -> WrongnessFinding:
    return WrongnessFinding(
        node_id=node_id,
        quote=f"student words about {node_id}",
        contradicts="the reference clause",
        kind="reversal",
        corroborated=True,
        resolved=False,
        apollo_elicited=True,
        would_ceiling=False,
    )


def test_gate_G_L3b_reveal_budget_is_shared_and_a_finding_never_widens_it():
    """A wrongness line is a THIRD reveal channel and must JOIN the existing
    budget — otherwise the union across best-grade-wins retries (``browse`` is
    best-grade-wins, ``restart_problem`` is reachable from REPORT) becomes a
    recitable reference solution.
    """
    graph = _low_credit_graph()
    coverage = {"per_step": {node.node_id: "missing" for node in graph.nodes}}
    kwargs = {
        "coverage": coverage,
        "reference_nodes": graph.nodes,
        "centrality": compute_centrality(graph),
    }

    plain = compute_topic_score(**kwargs)
    surfaced = compute_topic_score(
        **kwargs, misconceptions={node.node_id: _finding(node.node_id) for node in graph.nodes}
    )

    def reveals(result):
        return [topic.canonical_key for topic in result.topics if topic.reference_text]

    assert len(reveals(plain)) <= MAX_REFERENCE_TEXT_REVEALS
    assert reveals(surfaced) == reveals(plain), "the misconception lines widened the reveal set"
    assert MAX_REFERENCE_NAME_QUOTES == MAX_REFERENCE_TEXT_REVEALS, (
        "the name-quote budget stopped being the reference-text budget — the two "
        "channels can now widen independently"
    )


def test_gate_G_L3b_the_narrator_names_a_contradiction_only_when_one_is_supplied():
    """``_format_topic_line`` already renders the line and
    ``_TOPIC_SYSTEM_PROMPT`` already carries "if none are supplied, say nothing
    at all" — so the gate is that the DATA, not the prompt, decides."""
    graph = _low_credit_graph()
    coverage = {"per_step": {node.node_id: "missing" for node in graph.nodes}}
    kwargs = {
        "coverage": coverage,
        "reference_nodes": graph.nodes,
        "centrality": compute_centrality(graph),
    }

    _system, plain_user = build_topic_narrative_prompt(
        compute_topic_score(**kwargs), problem_text="p"
    )
    _system, surfaced_user = build_topic_narrative_prompt(
        compute_topic_score(**kwargs, misconceptions={"eq.one": _finding("eq.one")}),
        problem_text="p",
    )

    assert "Misconception" not in plain_user
    assert surfaced_user.count("* Misconception (uncorrected)") == 1
    assert "student words about eq.one" in surfaced_user


def test_gate_G_L3b_the_named_span_is_always_the_students_own_verbatim_words():
    """Tally-tier quotes are 465/465 = 100% verbatim-accurate because
    ``_verbatim_span`` enforces it; adjudicator spans are 0/284 and are
    diagnostic only. The named span therefore rides the finding's own quote and
    never the reference clause it contradicts."""
    graph = _low_credit_graph()
    result = compute_topic_score(
        coverage={"per_step": {node.node_id: "covered" for node in graph.nodes}},
        reference_nodes=graph.nodes,
        centrality=compute_centrality(graph),
        misconceptions={"eq.one": _finding("eq.one")},
    )

    (misconception,) = next(
        topic.misconceptions for topic in result.topics if topic.canonical_key == "eq.one"
    )
    assert misconception.evidence_span == "student words about eq.one"
    assert "the reference clause" not in (misconception.evidence_span or "")
