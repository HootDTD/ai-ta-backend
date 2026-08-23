"""P3.2 producer: `_decode_updates` is where the wrongness contract is enforced.

Three rules live here because a static JSON-schema `required` list cannot
express any of them (§2.1): the conditional `contradiction`, the off-enum
coercion, and the `missing`-status coercion. The fourth rule — the verbatim
gate — is NOT re-implemented: the pre-existing L298 drop already covers every
wrongness-bearing row, and one of these tests exists to prove exactly that.
"""

from __future__ import annotations

import pytest

from apollo.smart_questions import unified

pytestmark = pytest.mark.unit

_TRANSCRIPT = [("student", "raising the price raises demand too")]
_VALID_IDS = {"n1"}


def _decode(**item_overrides):
    item = {
        "node_id": "n1",
        "status": "conflicting",
        "evidence": {"turn_id": 0, "quote": "raising the price raises demand too"},
    }
    item.update(item_overrides)
    return unified._decode_updates(
        {"tally_updates": [item]}, valid_ids=_VALID_IDS, transcript=_TRANSCRIPT
    )


def _contradiction(**overrides):
    payload = {"reference_clause": "a price rise lowers demand", "kind": "inverted relationship"}
    payload.update(overrides)
    return payload


# --------------------------------------------------------------------------- #
# The conditional `contradiction`
# --------------------------------------------------------------------------- #
def test_wrongness_and_contradiction_decode_together():
    (update,) = _decode(wrongness="contradicts_material", contradiction=_contradiction())
    assert update.wrongness == "contradicts_material"
    assert update.contradiction == unified.Contradiction(
        reference_clause="a price rise lowers demand", kind="inverted relationship"
    )


@pytest.mark.parametrize(
    "contradiction",
    [
        None,
        {},
        "a price rise lowers demand",
        {"kind": "inverted relationship"},
        {"reference_clause": "   ", "kind": "inverted relationship"},
        {"reference_clause": "a price rise lowers demand", "kind": None},
    ],
    ids=["null", "empty", "string", "no-clause", "blank-clause", "kind-not-a-string"],
)
def test_wrongness_requires_contradiction_object(caplog, contradiction):
    """A finding with nothing to point at is not a finding — the WHOLE update is
    dropped, never kept with the label silently stripped."""
    with caplog.at_level("WARNING"):
        assert _decode(wrongness="contradicts_material", contradiction=contradiction) == ()
    assert "apollo_wrongness_missing_contradiction" in caplog.text


def test_absent_contradiction_key_drops_the_update(caplog):
    with caplog.at_level("WARNING"):
        assert _decode(wrongness="contradicts_self") == ()
    assert "apollo_wrongness_missing_contradiction" in caplog.text


# --------------------------------------------------------------------------- #
# Coercions
# --------------------------------------------------------------------------- #
def test_absent_wrongness_defaults_to_none():
    """Level 0 sends no `wrongness` field at all, and its updates stay valid."""
    (update,) = _decode()
    assert update.wrongness == "none"
    assert update.contradiction is None


@pytest.mark.parametrize(
    "value",
    ["contradicts", "CONTRADICTS_MATERIAL", "", 1, True, None, {}, ["none"]],
    ids=["unknown", "wrong-case", "blank", "int", "bool", "null", "dict", "list"],
)
def test_off_enum_wrongness_coerces_to_none(caplog, value):
    with caplog.at_level("WARNING"):
        (update,) = _decode(wrongness=value, contradiction=_contradiction())
    assert update.wrongness == "none"
    assert update.contradiction is None
    assert "apollo_wrongness_off_enum" in caplog.text


def test_missing_status_forces_wrongness_none():
    """A `missing` node carries no student claim, so there is nothing to
    contradict — and a `contradicts_material` on `missing` is impossible by
    construction, since the verbatim gate only spares evidence-less updates
    when the status IS `missing`."""
    (update,) = _decode(
        status="missing",
        evidence=None,
        wrongness="contradicts_material",
        contradiction=_contradiction(),
    )
    assert update.status == "missing"
    assert update.wrongness == "none"
    assert update.contradiction is None


def test_kind_is_free_form_and_never_validated():
    """`kind` is observability-only: no vocabulary, no bank, no code gate. Any
    string survives — there is nothing for the model to hallucinate against."""
    for kind in ("inverted relationship", "?!", "a" * 400, "категория", "0"):
        (update,) = _decode(
            wrongness="contradicts_material", contradiction=_contradiction(kind=kind)
        )
        assert update.contradiction is not None
        assert update.contradiction.kind == kind


def test_empty_kind_is_accepted_but_a_blank_reference_clause_is_not():
    (update,) = _decode(wrongness="contradicts_material", contradiction=_contradiction(kind=""))
    assert update.contradiction is not None
    assert update.contradiction.kind == ""


# --------------------------------------------------------------------------- #
# The verbatim gate is RIDDEN, not re-implemented
# --------------------------------------------------------------------------- #
def test_unverbatim_wrongness_span_is_dropped_by_existing_gate(caplog):
    """G-L1b: zero rows with `wrongness != "none"` and no surviving span.

    The drop is the PRE-EXISTING `status != "missing" and evidence is None`
    gate (`apollo_question_evidence_rejected`), not a second wrongness-specific
    check — assert its log line, so a future refactor that removes it fails here.
    """
    with caplog.at_level("WARNING"):
        updates = _decode(
            evidence={"turn_id": 0, "quote": "words the student never typed"},
            wrongness="contradicts_material",
            contradiction=_contradiction(),
        )
    assert updates == ()
    assert "apollo_question_evidence_rejected" in caplog.text
    assert "apollo_wrongness_missing_contradiction" not in caplog.text


def test_surviving_span_is_the_students_raw_text_not_the_models_rendering():
    (update,) = _decode(
        evidence={"turn_id": 0, "quote": "Raising the price, raises demand too!"},
        wrongness="contradicts_material",
        contradiction=_contradiction(),
    )
    assert update.evidence is not None
    assert update.evidence.quote == "raising the price raises demand too"


def test_a_dropped_update_never_reaches_the_ledger_with_a_state_change():
    """The drop is the whole update, so a rejected finding cannot silently
    launder its `state` write into the tally either."""
    updates = unified._decode_updates(
        {
            "tally_updates": [
                {
                    "node_id": "n1",
                    "status": "conflicting",
                    "evidence": {"turn_id": 0, "quote": "raising the price raises demand too"},
                    "wrongness": "contradicts_material",
                    "contradiction": None,
                }
            ]
        },
        valid_ids=_VALID_IDS,
        transcript=_TRANSCRIPT,
    )
    assert updates == ()
