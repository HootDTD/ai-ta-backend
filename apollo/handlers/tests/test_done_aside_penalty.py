"""INTERACTION5 Hoot-assist grading-cap wiring in ``apollo/handlers/done.py``.

Same no-Docker harness as the other Done unit tests (``_old_path_patches``):
collaborators are mocked, so these assert the WIRING — the flag/allowlist gate,
that ``hoot_asides`` reach the adjudicator, that ``apply_aside_caps`` runs on the
real coverage BEFORE the real topic score, the additive provenance block, and
that the cap pass is its own failure domain (never the 503 lane).

The topic score is REAL here (dropped from the base golden) so the cap's effect
on the served grade is observable: eq1 covered (credit 1.0) + c1 covered (credit
1.0) with equal centrality scores 100; capping the assisted eq1 to 0.5/partial
drops the served overall to 75.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.dialects import sqlite

from apollo.handlers.done import _aside_texts, handle_done
from apollo.handlers.tests._done_fixtures import _old_path_patches
from apollo.hoot_bridge.reference_answer import ASIDE_MESSAGE_INTENT_TAG
from apollo.ontology import KGGraph, build_node

pytestmark = pytest.mark.unit

_NEG = {"dual": 0, "disputed": 0, "paraphrased": 0, "skipped": 0}

_COVERAGE_WITH_ASSIST = {
    "per_step": {"eq1": "covered", "c1": "covered"},
    "procedure_scores": {"eq1": 1.0, "c1": 1.0},
    "confidences": {"eq1": 0.9, "c1": 0.9},
    "negotiation_counts": _NEG,
    "hoot_assisted": {"eq1": True, "c1": False},
}

_COVERAGE_NO_ASSIST = {
    "per_step": {"eq1": "covered", "c1": "covered"},
    "procedure_scores": {"eq1": 1.0, "c1": 1.0},
    "confidences": {"eq1": 0.9, "c1": 0.9},
    "negotiation_counts": _NEG,
}


def _reference_graph() -> KGGraph:
    eq = build_node(
        node_type="equation",
        node_id="eq1",
        attempt_id=99,
        source="reference",
        content={"symbolic": "P + 0.5*rho*v**2 = const", "label": "Bernoulli"},
    )
    cond = build_node(
        node_type="condition",
        node_id="c1",
        attempt_id=99,
        source="reference",
        content={"applies_when": "steady flow", "label": "steady"},
    )
    return KGGraph(nodes=[eq, cond], edges=[])


async def _run(
    monkeypatch,
    *,
    flag_on: bool,
    asides: tuple[str, ...],
    coverage: dict,
    cap_side_effect=None,
    write_mock=None,
    concept_slug: str = "bernoulli_principle",
):
    monkeypatch.delenv("INTERACTION_CONCEPTS", raising=False)
    if flag_on:
        monkeypatch.setenv("INTERACTION5", "1")
    else:
        monkeypatch.delenv("INTERACTION5", raising=False)

    db, _sess, _attempt, patches = _old_path_patches()
    db.begin_nested.return_value.__aenter__ = AsyncMock(return_value=None)
    db.begin_nested.return_value.__aexit__ = AsyncMock(return_value=False)
    graph = _reference_graph()

    async def _find_problem_with_graph(_db, _cid, _code, *, course_id):
        problem = MagicMock()
        problem.id = "p_code"
        problem.concept_id = concept_slug
        problem.problem_text = "text"
        problem.reference_solution = []
        problem.to_kg_graph.return_value = graph
        return problem

    # Drop the base golden stubs this test overrides: real graph, real coverage
    # dict, and REAL topic scoring (the cap's effect must be observable).
    drop = {"_find_problem", "compute_transcript_coverage_with_spans", "compute_topic_score"}
    patches = [p for p in patches if getattr(p, "attribute", None) not in drop]

    coverage_mock = AsyncMock(return_value=(dict(coverage), {}))
    aside_mock = AsyncMock(return_value=asides)

    patches += [
        patch(
            "apollo.handlers.done._find_problem",
            new=AsyncMock(side_effect=_find_problem_with_graph),
        ),
        patch("apollo.handlers.done.compute_transcript_coverage_with_spans", new=coverage_mock),
        patch("apollo.handlers.done._aside_texts", new=aside_mock),
    ]
    if cap_side_effect is not None:
        patches.append(
            patch(
                "apollo.handlers.done.apply_aside_caps", new=MagicMock(side_effect=cap_side_effect)
            )
        )
    if write_mock is not None:
        patches = [p for p in patches if getattr(p, "attribute", None) != "write_artifacts"]
        patches.append(patch("apollo.handlers.done.write_artifacts", new=write_mock))

    for p in patches:
        p.start()
    try:
        out = await handle_done(db=db, neo=MagicMock(), session_id=11)
    finally:
        for p in reversed(patches):
            p.stop()
    return out, coverage_mock, aside_mock


# --------------------------------------------------------------------------- #
# Flag ON + asides present -> capped grade + provenance + result flag
# --------------------------------------------------------------------------- #
async def test_flag_on_with_asides_caps_topic_score_and_writes_provenance(monkeypatch):
    write_mock = AsyncMock(return_value=None)
    out, coverage_mock, aside_mock = await _run(
        monkeypatch,
        flag_on=True,
        asides=("Bernoulli's equation relates pressure and velocity.",),
        coverage=_COVERAGE_WITH_ASSIST,
        write_mock=write_mock,
    )

    # Asides fetched and threaded into the adjudicator.
    aside_mock.assert_awaited_once()
    assert coverage_mock.await_args.kwargs["hoot_asides"] == (
        "Bernoulli's equation relates pressure and velocity.",
    )

    # eq1 capped 1.0 -> 0.5/partial, c1 untouched (1.0/covered): overall 75, not 100.
    assert out["rubric"]["overall"]["score"] == 75

    # Additive provenance block.
    assert out["grading_provenance"]["aside_penalty"] == {
        "enabled": True,
        "cap": 0.5,
        "assisted_node_ids": ["eq1"],
    }

    # The result's TopicScoreResult carries the additive per-topic flag (the seam
    # Agent D reads for feedback surfacing).
    topic_score = write_mock.await_args.kwargs["topic_score"]
    by_key = {t.canonical_key: t for t in topic_score.topics}
    assert by_key["eq1"].hoot_assisted is True
    assert by_key["c1"].hoot_assisted is False


async def test_capped_coverage_is_the_same_object_all_consumers_see(monkeypatch):
    """The served coverage is the capped one (rubric/topic/artifact/served all
    read the reassigned `coverage`)."""
    out, _coverage_mock, _aside_mock = await _run(
        monkeypatch,
        flag_on=True,
        asides=("aside text",),
        coverage=_COVERAGE_WITH_ASSIST,
    )

    assert out["coverage"]["procedure_scores"]["eq1"] == 0.5
    assert out["coverage"]["per_step"]["eq1"] == "missing"
    # Unassisted node untouched.
    assert out["coverage"]["per_step"]["c1"] == "covered"


# --------------------------------------------------------------------------- #
# Flag ON but no asides -> no cap, no provenance, byte-identical grade
# --------------------------------------------------------------------------- #
async def test_flag_on_no_asides_is_uncapped_and_has_no_provenance(monkeypatch):
    out, coverage_mock, aside_mock = await _run(
        monkeypatch,
        flag_on=True,
        asides=(),
        coverage=_COVERAGE_NO_ASSIST,
    )

    aside_mock.assert_awaited_once()
    assert coverage_mock.await_args.kwargs["hoot_asides"] == ()
    # No cap applied -> full score.
    assert out["rubric"]["overall"]["score"] == 100
    assert "aside_penalty" not in out["grading_provenance"]


# --------------------------------------------------------------------------- #
# Flag OFF + asides present in DB -> never fetched, byte-identical grade
# --------------------------------------------------------------------------- #
async def test_flag_off_never_fetches_asides_and_grade_is_untouched(monkeypatch):
    out, coverage_mock, aside_mock = await _run(
        monkeypatch,
        flag_on=False,
        asides=("this aside exists but the flag gates it out",),
        coverage=_COVERAGE_NO_ASSIST,
    )

    aside_mock.assert_not_awaited()
    assert coverage_mock.await_args.kwargs["hoot_asides"] == ()
    assert out["rubric"]["overall"]["score"] == 100
    assert "aside_penalty" not in out["grading_provenance"]


async def test_nonmatching_allowlist_gates_the_cap_off(monkeypatch):
    monkeypatch.setenv("INTERACTION_CONCEPTS", "ethics")
    monkeypatch.setenv("INTERACTION5", "1")

    db, _sess, _attempt, patches = _old_path_patches()
    db.begin_nested.return_value.__aenter__ = AsyncMock(return_value=None)
    db.begin_nested.return_value.__aexit__ = AsyncMock(return_value=False)
    graph = _reference_graph()

    async def _find_problem_with_graph(_db, _cid, _code, *, course_id):
        problem = MagicMock()
        problem.id = "p_code"
        problem.concept_id = "bernoulli_principle"  # not in the allowlist
        problem.problem_text = "text"
        problem.reference_solution = []
        problem.to_kg_graph.return_value = graph
        return problem

    drop = {"_find_problem", "compute_transcript_coverage_with_spans", "compute_topic_score"}
    patches = [p for p in patches if getattr(p, "attribute", None) not in drop]
    coverage_mock = AsyncMock(return_value=(dict(_COVERAGE_NO_ASSIST), {}))
    aside_mock = AsyncMock(return_value=("unused aside",))
    patches += [
        patch(
            "apollo.handlers.done._find_problem",
            new=AsyncMock(side_effect=_find_problem_with_graph),
        ),
        patch("apollo.handlers.done.compute_transcript_coverage_with_spans", new=coverage_mock),
        patch("apollo.handlers.done._aside_texts", new=aside_mock),
    ]
    for p in patches:
        p.start()
    try:
        out = await handle_done(db=db, neo=MagicMock(), session_id=11)
    finally:
        for p in reversed(patches):
            p.stop()

    aside_mock.assert_not_awaited()
    assert coverage_mock.await_args.kwargs["hoot_asides"] == ()
    assert out["rubric"]["overall"]["score"] == 100
    assert "aside_penalty" not in out["grading_provenance"]


# --------------------------------------------------------------------------- #
# Cap-pass failure domain: any exception -> uncapped grade still served (no 503)
# --------------------------------------------------------------------------- #
async def test_cap_pass_exception_serves_uncapped_grade(monkeypatch):
    out, _coverage_mock, aside_mock = await _run(
        monkeypatch,
        flag_on=True,
        asides=("aside text",),
        coverage=_COVERAGE_WITH_ASSIST,
        cap_side_effect=RuntimeError("boom"),
    )

    aside_mock.assert_awaited_once()
    # Cap raised -> coverage stayed the original uncapped verdict -> full score.
    assert out["rubric"]["overall"]["score"] == 100
    # Provenance still emitted (gate on + asides fetched), with no capped nodes.
    assert out["grading_provenance"]["aside_penalty"] == {
        "enabled": True,
        "cap": 0.5,
        "assisted_node_ids": [],
    }


async def test_aside_fetch_exception_degrades_to_uncapped(monkeypatch):
    """A fetch failure logs and swallows -> `hoot_asides` empty -> no cap, no
    provenance, grading proceeds (never the 503 lane)."""
    monkeypatch.delenv("INTERACTION_CONCEPTS", raising=False)
    monkeypatch.setenv("INTERACTION5", "1")

    db, _sess, _attempt, patches = _old_path_patches()
    db.begin_nested.return_value.__aenter__ = AsyncMock(return_value=None)
    db.begin_nested.return_value.__aexit__ = AsyncMock(return_value=False)
    graph = _reference_graph()

    async def _find_problem_with_graph(_db, _cid, _code, *, course_id):
        problem = MagicMock()
        problem.id = "p_code"
        problem.concept_id = "bernoulli_principle"
        problem.problem_text = "text"
        problem.reference_solution = []
        problem.to_kg_graph.return_value = graph
        return problem

    drop = {"_find_problem", "compute_transcript_coverage_with_spans", "compute_topic_score"}
    patches = [p for p in patches if getattr(p, "attribute", None) not in drop]
    coverage_mock = AsyncMock(return_value=(dict(_COVERAGE_NO_ASSIST), {}))
    patches += [
        patch(
            "apollo.handlers.done._find_problem",
            new=AsyncMock(side_effect=_find_problem_with_graph),
        ),
        patch("apollo.handlers.done.compute_transcript_coverage_with_spans", new=coverage_mock),
        patch(
            "apollo.handlers.done._aside_texts", new=AsyncMock(side_effect=RuntimeError("db down"))
        ),
    ]
    for p in patches:
        p.start()
    try:
        out = await handle_done(db=db, neo=MagicMock(), session_id=11)
    finally:
        for p in reversed(patches):
            p.stop()

    assert coverage_mock.await_args.kwargs["hoot_asides"] == ()
    assert out["rubric"]["overall"]["score"] == 100
    assert "aside_penalty" not in out["grading_provenance"]


# --------------------------------------------------------------------------- #
# _aside_texts query shape
# --------------------------------------------------------------------------- #
class _ScalarRows:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        m = MagicMock()
        m.all.return_value = list(self._rows)
        return m


async def test_aside_texts_selects_tagged_apollo_rows_in_turn_order():
    db = MagicMock()
    db.execute = AsyncMock(
        return_value=_ScalarRows(["A network effect means...", "Marginal cost is..."])
    )

    result = await _aside_texts(db, attempt_id=99)

    assert result == ("A network effect means...", "Marginal cost is...")

    statement = db.execute.await_args.args[0]
    sql = " ".join(
        str(
            statement.compile(dialect=sqlite.dialect(), compile_kwargs={"literal_binds": True})
        ).split()
    )
    assert "attempt_id = 99" in sql
    assert "app.tutoring_messages.role = 'apollo'" in sql
    assert f"app.tutoring_messages.intent = '{ASIDE_MESSAGE_INTENT_TAG}'" in sql
    assert sql.endswith("ORDER BY app.tutoring_messages.turn_index")
