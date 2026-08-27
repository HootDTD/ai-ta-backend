"""`basis` is persisted, not just logged (2026-08-08).

The adjudicator has always declared, per rubric item, WHY it credited what it
credited: `basis ∈ {stated, used, implied, absent}`. Until now that judgement
died in a log line — `transcript_coverage_credit node_id=… basis=…` — which is
concurrent, unaligned to attempts, and gone after log retention.

That cost a real decision. The 2026-08-08 replay found 39 of 669 credits sitting
at `absent|0.6`: the model itself declaring there is no evidence and crediting
anyway. `absent|0.6` is ~6× narrower than the `covered == False` signal a code
clamp would have keyed on — and clamping on `covered` was simulated over the same
106 attempts and re-bimodalizes the grade, because `covered` means FULLY covered
and is the normal shape of a genuine partial. The narrower signal could not be
sized per attempt for one reason only: nothing persisted it.

So `basis` joins the verdict as an OPTIONAL, additive per-node map, keyed exactly
like `procedure_scores`, and rides into every `node_ledger` row. It gates
nothing: instrument first, size second, gate third.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from apollo.ontology import KGGraph, build_node
from apollo.overseer.aside_penalty import apply_aside_caps
from apollo.overseer.coverage_contract import validate_coverage_verdict
from apollo.overseer.transcript_coverage import compute_transcript_coverage_with_spans

pytestmark = pytest.mark.unit


def _valid() -> dict:
    return {
        "per_step": {"n": "covered"},
        "procedure_scores": {"n": 1.0},
        "confidences": {"n": 0.9},
        "negotiation_counts": {"dual": 0, "disputed": 0, "paraphrased": 0, "skipped": 0},
    }


# --------------------------------------------------------------------------- #
# The frozen contract accepts it, and still accepts its absence
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("basis", ["stated", "used", "implied", "absent"])
def test_validator_accepts_every_declared_basis(basis):
    value = _valid()
    value["basis"] = {"n": basis}
    validate_coverage_verdict(value)


def test_validator_still_accepts_a_verdict_without_basis():
    """Absent-safe: the dormant graph lane and every artifact written before
    2026-08-08 carry no `basis`, and both must stay valid."""
    validate_coverage_verdict(_valid())


def test_validator_rejects_basis_not_a_dict():
    value = _valid()
    value["basis"] = ["stated"]
    with pytest.raises(ValueError, match="basis must be a dict"):
        validate_coverage_verdict(value)


@pytest.mark.parametrize("bad_map", [{"n": "guessed"}, {"n": 1}, {"n": None}, {123: "stated"}])
def test_validator_rejects_a_basis_outside_the_declared_enum(bad_map):
    """The four values are the adjudicator's own structured-output enum. A fifth
    value means the schema and the contract have drifted apart — that is a
    contract violation, not something to pass through to the ledger."""
    value = _valid()
    value["basis"] = bad_map
    with pytest.raises(ValueError, match="basis must map string node ids to one of"):
        validate_coverage_verdict(value)


def test_basis_is_permitted_but_a_stray_key_still_is_not():
    value = _valid()
    value["basis"] = {"n": "stated"}
    value["surprise"] = {"n": True}
    with pytest.raises(ValueError, match="coverage keys must be exactly"):
        validate_coverage_verdict(value)


# --------------------------------------------------------------------------- #
# The live lane emits it, keyed exactly like procedure_scores
# --------------------------------------------------------------------------- #
def _problem() -> SimpleNamespace:
    return SimpleNamespace(problem_text="Explain the framework")


def _graph(*node_ids: str) -> KGGraph:
    return KGGraph(
        nodes=[
            build_node(
                node_type="procedure_step",
                node_id=node_id,
                attempt_id=1,
                source="reference",
                content={"action": f"Do {node_id}", "purpose": ""},
            )
            for node_id in node_ids
        ]
    )


def _verdict(node_id: str, *, credit: float, basis: str, covered: bool = True) -> dict:
    return {
        "node_id": node_id,
        "covered": covered,
        "credit": credit,
        "confidence": 0.8,
        "evidence_span": "the student said this",
        "prompted": False,
        "corrected_later": False,
        "basis": basis,
    }


def _client(payload: dict) -> MagicMock:
    client = MagicMock()
    client.chat.completions.create.return_value = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(payload)))]
    )
    return client


async def _coverage_for(payload: dict, graph: KGGraph) -> dict:
    with patch("apollo.overseer.transcript_coverage.bounded_client", return_value=_client(payload)):
        coverage, _spans = await compute_transcript_coverage_with_spans(
            [("student", "the student said this")], graph, _problem()
        )
    return dict(coverage)


@pytest.mark.asyncio
async def test_live_verdict_carries_every_nodes_declared_basis():
    payload = {
        "verdicts": [
            _verdict("p1", credit=1.0, basis="stated"),
            _verdict("p2", credit=0.85, basis="used"),
            _verdict("p3", credit=0.6, basis="implied"),
            _verdict("p4", credit=0.0, basis="absent", covered=False),
        ]
    }
    coverage = await _coverage_for(payload, _graph("p1", "p2", "p3", "p4"))
    assert coverage["basis"] == {
        "p1": "stated",
        "p2": "used",
        "p3": "implied",
        "p4": "absent",
    }
    # Keyed EXACTLY like the credit map — the pairing is the whole point.
    assert set(coverage["basis"]) == set(coverage["procedure_scores"])


@pytest.mark.asyncio
async def test_the_absent_yet_credited_cell_is_now_visible_in_the_verdict():
    """The 5.8% of credits the replay could only find in a log line: the model
    declares `absent` and still awards 0.6. Nothing here reacts to it — it is
    recorded so the next arm can size a gate instead of guessing."""
    payload = {"verdicts": [_verdict("p1", credit=0.6, basis="absent", covered=False)]}
    coverage = await _coverage_for(payload, _graph("p1"))
    assert coverage["basis"]["p1"] == "absent"
    assert coverage["procedure_scores"]["p1"] == pytest.approx(0.6)


@pytest.mark.asyncio
async def test_a_node_with_no_verdict_is_omitted_from_basis_too():
    """Abstain-not-zero (P0.5): an omitted node leaves EVERY coverage map, so
    `basis` must not invent a row for it either."""
    payload = {"verdicts": [_verdict("p1", credit=1.0, basis="stated")]}
    coverage = await _coverage_for(payload, _graph("p1", "p2"))
    assert "p2" not in coverage["basis"]
    assert set(coverage["basis"]) == set(coverage["procedure_scores"]) == {"p1"}


@pytest.mark.asyncio
async def test_an_off_enum_basis_is_dropped_and_never_fails_the_grading(caplog):
    """A diagnostic must not be able to 503 a grading the student earned.

    The strict schema makes an off-enum basis near-impossible, but if one ever
    arrives the node loses its basis row and keeps its credit — the alternative
    is a contract ValueError becoming `CoverageGradingError` and a "try again"
    card over a field nothing scores on.
    """
    payload = {
        "verdicts": [
            _verdict("p1", credit=1.0, basis="guessed"),
            _verdict("p2", credit=0.6, basis="implied"),
        ]
    }
    with caplog.at_level("WARNING", logger="apollo.overseer.transcript_coverage"):
        coverage = await _coverage_for(payload, _graph("p1", "p2"))
    assert coverage["basis"] == {"p2": "implied"}
    assert coverage["procedure_scores"]["p1"] == pytest.approx(1.0)
    assert "transcript_coverage_basis_off_enum" in caplog.text


@pytest.mark.asyncio
async def test_basis_survives_the_hoot_aside_cap_pass():
    """`apply_aside_caps` rebuilds two maps and carries the rest by reference;
    a capped node keeps the basis the adjudicator declared, because the cap is a
    penalty on the credit, not a re-judgement of the evidence."""
    payload = {"verdicts": [_verdict("p1", credit=1.0, basis="stated")]}
    coverage = await _coverage_for(payload, _graph("p1"))
    coverage["hoot_assisted"] = {"p1": True}
    capped, assisted = apply_aside_caps(coverage)
    assert assisted == ("p1",)
    assert capped["basis"] == {"p1": "stated"}
    assert capped["procedure_scores"]["p1"] == pytest.approx(0.5)
