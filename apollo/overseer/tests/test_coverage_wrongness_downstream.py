"""``coverage["wrongness"]`` is inert for every existing consumer (P3.2).

The key is additive carriage: the second reader's answers ride the coverage dict
from `transcript_coverage` to `done.py`, past four consumers that must not
notice. This is the level-1/2/3 inertness guarantee at the seam W1-B owns —
whatever the ladder later does with a finding, merely CARRYING one may not move
a grade, a rubric axis, an artifact row, or an aside cap.

Method throughout: build one coverage dict, copy it, add `wrongness` to the copy,
run both through the consumer, assert the outputs are equal. The negative case is
the point, so each assertion is against the pre-feature output, never against a
re-derived expectation.

Related, and deliberately NOT exercised here: the rubric absent-axis hazard
(`AXIS_WEIGHTS["misconception_corrected"]` flipping from absent to present
rescales every other axis by 0.95). Nothing in this module can feed that axis —
`compute_rubric` takes `misconception_scores` as its own keyword — and the test
below pins that carrying wrongness does not sneak into it.
"""

from __future__ import annotations

import pytest

from apollo.grading.artifact_build import build_llm_artifact
from apollo.ontology import KGGraph, build_node
from apollo.overseer.aside_penalty import apply_aside_caps
from apollo.overseer.coverage_contract import validate_coverage_verdict
from apollo.overseer.rubric import compute_rubric
from apollo.overseer.topic_score import compute_centrality, compute_topic_score

pytestmark = pytest.mark.unit


def _nodes():
    return [
        build_node(
            node_type="procedure_step",
            node_id=node_id,
            attempt_id=1,
            source="reference",
            content={"action": f"Do {node_id}", "purpose": ""},
        )
        for node_id in ("p1", "p2")
    ]


def _coverage(**extra) -> dict:
    base = {
        "per_step": {"p1": "covered", "p2": "missing"},
        "procedure_scores": {"p1": 1.0, "p2": 0.6},
        "confidences": {"p1": 0.9, "p2": 0.7},
        "negotiation_counts": {"dual": 0, "disputed": 0, "paraphrased": 0, "skipped": 0},
        "basis": {"p1": "stated", "p2": "implied"},
    }
    base.update(extra)
    return base


_WRONGNESS = {
    "p1": {"contradicted": True, "corrected_later": False, "prompted": False},
    "p2": {"contradicted": False, "corrected_later": True, "prompted": True},
}


def _pair() -> tuple[dict, dict]:
    """(without the key, with the key) — otherwise identical dicts."""
    return _coverage(), _coverage(wrongness=dict(_WRONGNESS))


def test_the_pair_is_identical_apart_from_the_one_key():
    without, with_key = _pair()
    validate_coverage_verdict(without)
    validate_coverage_verdict(with_key)
    assert with_key.pop("wrongness") == _WRONGNESS
    assert with_key == without


# --------------------------------------------------------------------------- #
# apply_aside_caps (INTERACTION5)
# --------------------------------------------------------------------------- #
def test_aside_caps_no_assist_path_is_untouched():
    without, with_key = _pair()
    assert apply_aside_caps(without) == (without, ())
    capped, assisted = apply_aside_caps(with_key)
    assert assisted == ()
    # Same object back — the no-aside path is byte-identical by identity.
    assert capped is with_key


def test_aside_caps_carry_wrongness_through_the_capping_rebuild():
    """`apply_aside_caps` rebuilds `per_step` and `procedure_scores` and carries
    every other map by reference; the corroboration answers must survive that,
    unchanged, because the cap is a penalty on credit, not a re-judgement."""
    without, with_key = _pair()
    without["hoot_assisted"] = {"p1": True}
    with_key["hoot_assisted"] = {"p1": True}

    capped_plain, assisted_plain = apply_aside_caps(without)
    capped_wrong, assisted_wrong = apply_aside_caps(with_key)

    assert assisted_plain == assisted_wrong == ("p1",)
    assert capped_wrong["wrongness"] == _WRONGNESS
    assert capped_wrong["procedure_scores"] == capped_plain["procedure_scores"]
    assert capped_wrong["per_step"] == capped_plain["per_step"]
    # The input dict was not mutated by the pass.
    assert with_key["procedure_scores"]["p1"] == pytest.approx(1.0)


# --------------------------------------------------------------------------- #
# compute_rubric — including the absent-axis hazard
# --------------------------------------------------------------------------- #
def test_rubric_output_is_identical_with_and_without_wrongness():
    without, with_key = _pair()
    nodes = _nodes()
    assert compute_rubric(with_key, nodes) == compute_rubric(without, nodes)


def test_carrying_wrongness_never_activates_the_misconception_axis():
    """THE hazard (§2.5): the misconception axis is *present* iff its dict is
    non-empty, and activating it rescales every other axis by 0.95 — moving
    `score_details.llm_rubric.overall` one level below the one labelled "score".
    Coverage carriage is not a route into it: `misconception_scores` is a
    separate keyword only `done._attempt_misconception_scores` fills."""
    _without, with_key = _pair()
    nodes = _nodes()
    inert = compute_rubric(with_key, nodes)
    activated = compute_rubric(with_key, nodes, misconception_scores={"some_code": 0.5})

    assert inert != activated, "guard test is meaningless if the axis never moves anything"
    assert inert == compute_rubric(with_key, nodes, misconception_scores=None)
    assert inert == compute_rubric(with_key, nodes, misconception_scores={})


# --------------------------------------------------------------------------- #
# compute_topic_score — the lane that would eventually carry the ceiling
# --------------------------------------------------------------------------- #
def test_topic_score_is_identical_with_and_without_wrongness():
    without, with_key = _pair()
    nodes = _nodes()
    centrality = compute_centrality(KGGraph(nodes=nodes))

    plain = compute_topic_score(coverage=without, reference_nodes=nodes, centrality=centrality)
    carried = compute_topic_score(coverage=with_key, reference_nodes=nodes, centrality=centrality)

    assert carried == plain
    assert carried.score == plain.score
    assert carried.letter == plain.letter
    assert carried.misconception_dock == plain.misconception_dock == 0.0
    assert all(topic.misconceptions == () for topic in carried.topics)


def test_topic_score_unaffected_by_a_contradicted_true_row():
    """Level 1/2/3 inertness in the direction that matters: a corroborated
    contradiction on a fully credited node still costs nothing here. Only level
    4's ceiling — built dark, unreachable, and not this module — may ever move
    the number."""
    without, with_key = _pair()
    with_key["wrongness"]["p1"]["contradicted"] = True
    nodes = _nodes()
    centrality = compute_centrality(KGGraph(nodes=nodes))

    assert compute_topic_score(
        coverage=with_key, reference_nodes=nodes, centrality=centrality
    ) == compute_topic_score(coverage=without, reference_nodes=nodes, centrality=centrality)


# --------------------------------------------------------------------------- #
# build_llm_artifact — the canonical record every teacher surface reads
# --------------------------------------------------------------------------- #
def _artifact(coverage: dict) -> dict:
    return build_llm_artifact(
        coverage=coverage,
        rubric={"overall": {"score": 60.0}},
        latency_ms=12,
        clarification_trace=[],
    )


def test_artifact_is_identical_with_and_without_wrongness():
    without, with_key = _pair()
    assert _artifact(with_key) == _artifact(without)


def test_artifact_misconceptions_stay_empty_while_only_carrying_the_key():
    """`artifact_build` hardwires `misconceptions: []` today, and the containers
    downstream of it (`scorecard._watch_out`, `classroom.top_misconceptions`) are
    live SQL over that list. Carrying a corroboration answer must not light them
    up — populating them is level 3's job, wired elsewhere."""
    _without, with_key = _pair()
    assert _artifact(with_key)["misconceptions"] == []


# --------------------------------------------------------------------------- #
# The whole chain, in the order done.py runs it
# --------------------------------------------------------------------------- #
def test_full_downstream_chain_produces_identical_output():
    without, with_key = _pair()
    nodes = _nodes()
    centrality = compute_centrality(KGGraph(nodes=nodes))

    def _run(coverage: dict) -> tuple:
        capped, assisted = apply_aside_caps(coverage)
        rubric = compute_rubric(capped, nodes)
        topics = compute_topic_score(coverage=capped, reference_nodes=nodes, centrality=centrality)
        artifact = build_llm_artifact(
            coverage=capped,
            rubric=rubric,
            latency_ms=12,
            clarification_trace=[],
            topic_score=topics,
        )
        return assisted, rubric, topics, artifact

    assert _run(with_key) == _run(without)
