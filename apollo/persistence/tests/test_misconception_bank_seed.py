"""Pure unit tests for ``apollo.persistence.misconception_bank_seed`` (the
``apollo_misconceptions`` TABLE bank conversion core — distinct from
``learner_model_seed.misconceptions_to_entities``, which mints
``kind='misconception'`` KG entities from the same source file).

NO DB, NO network, NO embeddings — pure dict-in/dataclass-out conversion.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from apollo.persistence.misconception_bank_seed import (
    MisconceptionBankSpec,
    misconception_entry_to_bank_spec,
    misconceptions_json_to_bank_specs,
)

pytestmark = pytest.mark.unit

_REPO = Path(__file__).resolve().parents[3]
_BERNOULLI_MISC = (
    _REPO
    / "apollo"
    / "subjects"
    / "fluid_mechanics"
    / "concepts"
    / "bernoulli_principle"
    / "misconceptions.json"
)
_GDP_MISC = (
    _REPO
    / "apollo"
    / "subjects"
    / "macroeconomics"
    / "concepts"
    / "gdp_components"
    / "misconceptions.json"
)


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_code_strips_misc_prefix():
    entry = {"key": "misc.density_ignored", "description": "d"}
    spec = misconception_entry_to_bank_spec(entry)
    assert spec.code == "density_ignored"


def test_trigger_phrases_copied_verbatim():
    entry = {
        "key": "misc.x",
        "description": "d",
        "trigger_phrases": ["a", "b", "c"],
    }
    spec = misconception_entry_to_bank_spec(entry)
    assert spec.trigger_phrases == ("a", "b", "c")


def test_missing_trigger_phrases_defaults_empty():
    spec = misconception_entry_to_bank_spec({"key": "misc.x", "description": "d"})
    assert spec.trigger_phrases == ()


def test_probe_question_falls_back_to_generated_when_absent():
    spec = misconception_entry_to_bank_spec(
        {"key": "misc.x", "description": "believes X causes Y."}
    )
    assert spec.probe_question  # NOT NULL column — never blank
    assert "believes X causes Y." in spec.probe_question


def test_probe_question_uses_authored_value_when_present():
    spec = misconception_entry_to_bank_spec(
        {"key": "misc.x", "description": "d", "probe_question": "Wait, really?"}
    )
    assert spec.probe_question == "Wait, really?"


def test_rt_steps_defaults_empty_tuple():
    spec = misconception_entry_to_bank_spec({"key": "misc.x", "description": "d"})
    assert spec.rt_steps == ()


def test_rt_steps_uses_authored_list_when_present():
    spec = misconception_entry_to_bank_spec(
        {"key": "misc.x", "description": "d", "rt_steps": ["step1", "step2"]}
    )
    assert spec.rt_steps == ("step1", "step2")


def test_confusion_pair_absent_by_default():
    spec = misconception_entry_to_bank_spec({"key": "misc.x", "description": "d"})
    assert spec.confusion_pair_a is None
    assert spec.confusion_pair_b is None


def test_confusion_pair_authored_when_present():
    spec = misconception_entry_to_bank_spec(
        {"key": "misc.x", "description": "d", "confusion_pair": ["mass_flow", "volumetric_flow"]}
    )
    assert spec.confusion_pair_a == "mass_flow"
    assert spec.confusion_pair_b == "volumetric_flow"


def test_confusion_pair_ignored_when_malformed_length():
    spec = misconception_entry_to_bank_spec(
        {"key": "misc.x", "description": "d", "confusion_pair": ["only_one"]}
    )
    assert spec.confusion_pair_a is None
    assert spec.confusion_pair_b is None


def test_misconceptions_json_to_bank_specs_preserves_order():
    misc = {
        "misconceptions": [
            {"key": "misc.a", "description": "first"},
            {"key": "misc.b", "description": "second"},
        ]
    }
    specs = misconceptions_json_to_bank_specs(misc)
    assert [s.code for s in specs] == ["a", "b"]
    assert isinstance(specs[0], MisconceptionBankSpec)


def test_misconceptions_json_to_bank_specs_empty_when_no_key():
    assert misconceptions_json_to_bank_specs({}) == []


def test_real_bernoulli_misconceptions_convert_cleanly():
    specs = misconceptions_json_to_bank_specs(_read_json(_BERNOULLI_MISC))
    assert {s.code for s in specs} == {
        "pressure_velocity_same_direction",
        "density_ignored",
    }
    for spec in specs:
        assert spec.description
        assert spec.probe_question
        assert spec.trigger_phrases


def test_real_gdp_components_misconceptions_convert_cleanly():
    specs = misconceptions_json_to_bank_specs(_read_json(_GDP_MISC))
    assert {s.code for s in specs} == {"includes_transfers", "gross_for_net"}
    for spec in specs:
        assert spec.description
        assert spec.probe_question
