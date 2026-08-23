"""``wrongness`` joins the frozen coverage contract's optional keys (P3.2).

The contract is deliberately closed: ``validate_coverage_verdict`` rejects ANY
key it does not know, and `_to_coverage_verdict` validates on the way out of the
sole grading lane. That is a good property and a sharp edge — see
``test_wrongness_key_without_optional_keys_would_raise``, which exists to
document the exact failure mode the P3.2 build plan calls "the 503 tripwire":
shipping the emitter without widening ``_OPTIONAL_KEYS`` does not degrade the new
feature, it takes DONE DOWN FOR EVERY STUDENT, including the ones with no finding
at all. The two edits must always land in the same commit.

Structure of the value: ``{node_id: {contradicted, corrected_later, prompted}}``.
All three booleans, always, genuinely ``bool`` — a half-populated row would read
as ``False`` downstream, which is silently the difference between "the second
reader said no" and "the second reader was never asked".
"""

from __future__ import annotations

import pytest

from apollo.overseer.coverage_contract import (
    WRONGNESS_FLAGS,
    _validate_wrongness_map,
    validate_coverage_verdict,
)

pytestmark = pytest.mark.unit


def _valid() -> dict:
    return {
        "per_step": {"n": "covered"},
        "procedure_scores": {"n": 1.0},
        "confidences": {"n": 0.9},
        "negotiation_counts": {"dual": 0, "disputed": 0, "paraphrased": 0, "skipped": 0},
    }


def _flags(**overrides) -> dict:
    row = {"contradicted": False, "corrected_later": False, "prompted": False}
    row.update(overrides)
    return row


# --------------------------------------------------------------------------- #
# Accepted, and still optional
# --------------------------------------------------------------------------- #
def test_optional_wrongness_key_accepted():
    value = _valid()
    value["wrongness"] = {"n": _flags(contradicted=True)}
    validate_coverage_verdict(value)


def test_verdict_without_wrongness_is_still_valid():
    """Absent-safe both ways: the dormant graph lane emits none, every verdict
    written before today has none, and level 0 must never emit one."""
    validate_coverage_verdict(_valid())


def test_empty_wrongness_map_is_accepted():
    """The adjudicator was asked and corroborated nothing for any node — a real
    outcome, distinct from never having been asked (the absent key)."""
    value = _valid()
    value["wrongness"] = {}
    validate_coverage_verdict(value)


def test_wrongness_coexists_with_the_other_two_optional_keys():
    value = _valid()
    value["hoot_assisted"] = {"n": True}
    value["basis"] = {"n": "stated"}
    value["wrongness"] = {"n": _flags()}
    validate_coverage_verdict(value)


# --------------------------------------------------------------------------- #
# Rejected
# --------------------------------------------------------------------------- #
def test_malformed_wrongness_map_rejected():
    value = _valid()
    value["wrongness"] = ["n"]
    with pytest.raises(ValueError, match="wrongness must be a dict"):
        validate_coverage_verdict(value)


@pytest.mark.parametrize(
    "bad_row",
    [
        {"contradicted": True},  # partial row
        {"contradicted": True, "corrected_later": False},  # partial row
        dict(_flags(), extra=True),  # a fourth flag
        {"contradicted": True, "corrected_later": False, "surprise": False},  # renamed flag
        "not-a-dict",
    ],
)
def test_wrongness_row_key_set_must_be_exactly_the_three_flags(bad_row):
    value = _valid()
    value["wrongness"] = {"n": bad_row}
    with pytest.raises(ValueError, match="keys must be exactly"):
        validate_coverage_verdict(value)


@pytest.mark.parametrize("truthy", [1, 0, "true", None])
def test_wrongness_flags_must_be_genuine_booleans_never_coerced(truthy):
    """Same posture as ``hoot_assisted``: a stray ``1`` is contract drift, and a
    finding is the input to a student-visible consequence — it may never be
    manufactured by truthiness."""
    value = _valid()
    value["wrongness"] = {"n": _flags(contradicted=truthy)}
    with pytest.raises(ValueError, match="values must be booleans"):
        validate_coverage_verdict(value)


def test_wrongness_keys_must_be_string_node_ids():
    value = _valid()
    value["wrongness"] = {123: _flags()}
    with pytest.raises(ValueError, match="wrongness must map string node ids"):
        validate_coverage_verdict(value)


def test_unknown_extra_key_still_rejected():
    """Widening the optional set for ``wrongness`` must not have opened the
    contract generally."""
    value = _valid()
    value["wrongness"] = {"n": _flags()}
    value["surprise"] = {"n": True}
    with pytest.raises(ValueError, match="coverage keys must be exactly"):
        validate_coverage_verdict(value)


def test_validate_wrongness_map_directly_rejects_non_dict():
    with pytest.raises(ValueError, match="wrongness must be a dict"):
        _validate_wrongness_map("not-a-dict")


def test_flag_names_are_the_single_source_of_truth():
    """``WRONGNESS_FLAGS`` is what the emitter builds rows from and what the
    validator checks them against — one constant, so they cannot drift apart."""
    assert set(WRONGNESS_FLAGS) == {"contradicted", "corrected_later", "prompted"}
    value = _valid()
    value["wrongness"] = {"n": dict.fromkeys(WRONGNESS_FLAGS, False)}
    validate_coverage_verdict(value)


# --------------------------------------------------------------------------- #
# THE TRIPWIRE
# --------------------------------------------------------------------------- #
def test_wrongness_key_without_optional_keys_would_raise(monkeypatch):
    """Documents the failure mode, so the next person cannot split the commit.

    ``_OPTIONAL_KEYS`` is the allowlist; the closed contract rejects everything
    else. Emitting ``coverage["wrongness"]`` from
    ``transcript_coverage._to_coverage_verdict`` while this frozenset still reads
    ``{"hoot_assisted", "basis"}`` makes the validator raise INSIDE the sole
    grading lane — which is not a degraded wrongness feature, it is a hard
    failure on EVERY Done, including every attempt with no finding at all. The
    emitter and the allowlist ship together, always.
    """
    value = _valid()
    value["wrongness"] = {"n": _flags(contradicted=True)}
    # Sanity: with the allowlist as shipped, this is fine.
    validate_coverage_verdict(value)

    monkeypatch.setattr(
        "apollo.overseer.coverage_contract._OPTIONAL_KEYS",
        frozenset({"hoot_assisted", "basis"}),
    )
    with pytest.raises(ValueError, match="coverage keys must be exactly"):
        validate_coverage_verdict(value)


@pytest.mark.asyncio
async def test_the_tripwire_reaches_the_live_grading_lane(monkeypatch):
    """The same split, exercised end to end: a plain, finding-free adjudication
    stops producing a grade at all."""
    import json
    from types import SimpleNamespace
    from unittest.mock import MagicMock, patch

    from apollo.ontology import KGGraph, build_node
    from apollo.overseer.transcript_coverage import compute_transcript_coverage_with_spans

    graph = KGGraph(
        nodes=[
            build_node(
                node_type="procedure_step",
                node_id="p1",
                attempt_id=1,
                source="reference",
                content={"action": "Do p1", "purpose": ""},
            )
        ]
    )
    payload = {
        "verdicts": [
            {
                "node_id": "p1",
                "covered": True,
                "credit": 1.0,
                "confidence": 0.9,
                "evidence_span": "the student said this",
                "prompted": False,
                "corrected_later": False,
                "basis": "stated",
                "contradicted": False,
            }
        ]
    }
    client = MagicMock()
    client.chat.completions.create.return_value = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(payload)))]
    )
    monkeypatch.setattr(
        "apollo.overseer.coverage_contract._OPTIONAL_KEYS",
        frozenset({"hoot_assisted", "basis"}),
    )
    with patch("apollo.overseer.transcript_coverage.bounded_client", return_value=client):
        with pytest.raises(ValueError, match="coverage keys must be exactly"):
            await compute_transcript_coverage_with_spans(
                [("student", "the student said this")],
                graph,
                SimpleNamespace(problem_text="Explain the framework"),
                wrongness_candidates={"p1": "the student said this"},
            )
