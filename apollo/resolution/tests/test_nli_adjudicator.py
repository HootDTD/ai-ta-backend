from apollo.resolution.nli_adjudicator import (
    FakeNLIAdjudicator,
    NLIResult,
    TransformersNLIAdjudicator,
    normalize_nli_output,
)


def test_normalize_maps_by_label_case_insensitive_not_index():
    # deberta-v3 order is contradiction,entailment,neutral — must map by name.
    raw = [
        {"label": "ENTAILMENT", "score": 0.91},
        {"label": "neutral", "score": 0.06},
        {"label": "Contradiction", "score": 0.03},
    ]
    r = normalize_nli_output(raw, "m")
    assert r.label == "entailment"
    assert (r.entailment, r.neutral, r.contradiction) == (0.91, 0.06, 0.03)
    assert r.model_name == "m"


def test_normalize_handles_nested_single_input_list():
    raw = [
        [
            {"label": "neutral", "score": 0.8},
            {"label": "entailment", "score": 0.1},
            {"label": "contradiction", "score": 0.1},
        ]
    ]
    assert normalize_nli_output(raw, "m").label == "neutral"


def test_fake_adjudicator_returns_scripted():
    want = NLIResult("entailment", 0.9, 0.05, 0.05, "fake")
    fake = FakeNLIAdjudicator({("p", "h"): want})
    assert fake.classify(premise="p", hypothesis="h") is want


def test_transformers_adjudicator_init_stores_attributes():
    # Exercises __init__ without triggering _load() (no model download).
    adj = TransformersNLIAdjudicator("some-model", device="cpu")
    assert adj.model_name == "some-model"
    assert adj.device == "cpu"
    assert adj._pipe is None
