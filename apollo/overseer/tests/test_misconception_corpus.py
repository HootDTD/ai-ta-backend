"""P2.4 — Adversarial corpus regression for the misconception pipeline.

Exercises `infer_misconception` against a labeled JSONL of 20
utterances (10 misconception, 10 correct) and asserts:

    recall    >= 0.80   on the misconception cases (TAU_FIRE)
    specificity >= 0.95  on the correct cases (TAU_PROBE)

This is a *deterministic* regression: the generator and verifier are
stubs that mimic the production pipeline's contract using simple
keyword heuristics over the bank's `trigger_phrases`. No LLM, no
network. Calibration on real LLMs happens post-deploy via the
`APOLLO_MISCONCEPTION_ENABLED` flag and the same corpus is replayed
against a real model.

The corpus is fluid-mechanics-themed for the pilot — but lives under
tests/, not under the runtime code path. The runtime never reads this
file. Adding a new class never requires touching this corpus; future
classes get their own corpus alongside their own bank rows.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from apollo.ontology import build_node
from apollo.overseer.misconception import (
    TAU_FIRE,
    TAU_PROBE,
    infer_misconception,
)
from apollo.overseer.misconception_bank import MisconceptionEntry
from apollo.solver.sufficiency import SufficiencyVerdict


_CORPUS_PATH = Path(__file__).parent / "misconception_corpus.jsonl"


def _load_corpus() -> list[dict]:
    rows: list[dict] = []
    with _CORPUS_PATH.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rows.append(json.loads(line))
    return rows


def _bank() -> list[MisconceptionEntry]:
    """Five authored bank entries spanning the corpus' misconception
    codes. Triggers are the keywords the stub generator and verifier
    use to detect a match."""
    return [
        MisconceptionEntry(
            id=1, concept_id=7, code="no_density",
            description="treats fluid as having no density when computing flow",
            confusion_pair=None,
            trigger_phrases=("ignore density", "density doesn't really matter",
                             "doesn't matter for the flow"),
            probe_question="hmm, does density matter here?",
            rt_steps=("ask about density", "compare with vs without"),
        ),
        MisconceptionEntry(
            id=2, concept_id=7, code="pressure_velocity_same_direction",
            description="thinks pressure and velocity move together not inversely",
            confusion_pair=None,
            trigger_phrases=("lower pressure means lower speed",
                             "pressure drops then automatically the velocity"),
            probe_question="hmm, do pressure and velocity move together?",
            rt_steps=("ask the relationship",),
        ),
        MisconceptionEntry(
            id=3, concept_id=7, code="mass_volume_flow_conflation",
            description="conflates mass flow and volumetric flow as equivalent",
            confusion_pair=None,
            trigger_phrases=("volumetric and mass flow are interchangeable",
                             "flow rate is always perfectly the same in every pipe"),
            probe_question="are mass flow and volume flow the same thing?",
            rt_steps=("ask about units",),
        ),
        MisconceptionEntry(
            id=4, concept_id=7, code="bernoulli_for_compressible",
            description="applies Bernoulli to compressible flow",
            confusion_pair=None,
            trigger_phrases=("bernoulli works fine even when the fluid is highly compressible",
                             "compressibility doesn't affect bernoulli"),
            probe_question="does Bernoulli need incompressibility?",
            rt_steps=("check the assumptions",),
        ),
        MisconceptionEntry(
            id=5, concept_id=7, code="viscosity_negligible",
            description="treats viscosity as universally negligible",
            confusion_pair=None,
            trigger_phrases=("no friction at all in real pipes",
                             "viscosity terms can always be dropped"),
            probe_question="is viscosity always droppable?",
            rt_steps=("ask when it matters",),
        ),
    ]


def _stub_generator(bank: list[MisconceptionEntry]):
    """Stub generator: scans the utterance for any bank trigger phrase.
    Mirrors what the MISTAKE-style cheap LLM does at production scale —
    surfaces ONE candidate suspected error in a free-text sentence."""
    def _gen(*, utterance: str, parsed_nodes, next_premise_hint):
        u = utterance.lower()
        for entry in bank:
            for trigger in entry.trigger_phrases:
                if trigger.lower() in u:
                    return entry.description, f"trigger: {trigger}"
        return None, "no-trigger-match"
    return _gen


def _stub_verifier(bank: list[MisconceptionEntry]):
    """Stub verifier: scores high when the utterance contains a trigger
    phrase whose entry's description is being verified. Mirrors
    Macina-style verify-then-generate at the contract level."""
    def _ver(*, utterance, parsed_nodes, candidate_description):
        u = utterance.lower()
        for entry in bank:
            if entry.description != candidate_description:
                continue
            for trigger in entry.trigger_phrases:
                if trigger.lower() in u:
                    return 0.95, f"matched trigger {trigger}"
        return 0.05, "no match"
    return _ver


def _stub_retriever_for(bank: list[MisconceptionEntry], utterance: str):
    """Stub retriever: ranks bank entries by trigger-phrase overlap with
    the current utterance. Mimics what pgvector cosine ANN does at
    production scale: the most-similar authored entry surfaces first.

    Built per-utterance because the production retriever sees the
    candidate embedding (which encodes the utterance's content) — the
    deterministic stub uses the utterance text directly.
    """
    u = utterance.lower()

    def _score(entry: MisconceptionEntry) -> float:
        for trigger in entry.trigger_phrases:
            if trigger.lower() in u:
                return 0.95
        return 0.05

    ranked = sorted(bank, key=_score, reverse=True)

    async def _retrieve(*, concept_id, query_embedding, k):
        return [(e, _score(e)) for e in ranked[:k]]

    return _retrieve


def _node():
    return build_node(
        node_type="equation",
        node_id="n1",
        attempt_id=1,
        source="parser",
        content={"symbolic": "A1*v1 - A2*v2", "label": "x"},
    )


@pytest.mark.asyncio
async def test_corpus_recall_and_specificity():
    """Replay every corpus row through the inference pipeline. Compute
    recall on misconception rows and specificity on correct rows.
    Targets: recall >= 0.80, specificity >= 0.95.

    A row is "fired" if the resulting signal has fired=True (probe or
    socratic). The PROBE-then-confirm gate is bypassed for this test
    by passing a corroborating prior signal for every row — this
    measures the threshold layer in isolation, which is what the
    corpus is designed to calibrate.
    """
    corpus = _load_corpus()
    assert len(corpus) == 20, "corpus must be exactly 20 utterances"
    assert sum(1 for r in corpus if r["category"] == "misconception") == 10
    assert sum(1 for r in corpus if r["category"] == "correct") == 10

    bank = _bank()
    gen = _stub_generator(bank)
    ver = _stub_verifier(bank)

    # Always-corroborate: every row has a prior signal of every code, so
    # the PROBE-then-confirm gate never demotes. This isolates the
    # threshold/match layer from the gate layer (covered by the unit
    # tests in test_misconception.py).
    prior_signals = tuple(
        # MisconceptionSignal placeholder for each code
        __import__("apollo.overseer.misconception", fromlist=["MisconceptionSignal"])
        .MisconceptionSignal(
            fired=True, state="probe", bank_code=e.code, confidence=0.6,
        )
        for e in bank
    )

    true_positives = 0
    false_positives = 0
    false_negatives = 0
    true_negatives = 0
    misconception_count = 0
    correct_count = 0

    for row in corpus:
        signal = await infer_misconception(
            db=None,
            concept_id=7,
            utterance=row["utterance"],
            parsed_nodes=[_node()],
            sufficiency=SufficiencyVerdict(state="insufficient"),
            previous_signals=prior_signals,
            generator=gen,
            verifier=ver,
            embedder=lambda text: [0.0] * 8,
            retriever=_stub_retriever_for(bank, row["utterance"]),
        )
        if row["category"] == "misconception":
            misconception_count += 1
            if signal.fired:
                true_positives += 1
            else:
                false_negatives += 1
        else:
            correct_count += 1
            if signal.fired:
                false_positives += 1
            else:
                true_negatives += 1

    recall = true_positives / misconception_count
    specificity = true_negatives / correct_count

    assert recall >= 0.80, (
        f"recall {recall:.2f} below 0.80 target — "
        f"TP={true_positives}, FN={false_negatives}"
    )
    assert specificity >= 0.95, (
        f"specificity {specificity:.2f} below 0.95 target — "
        f"FP={false_positives}, TN={true_negatives}"
    )


def test_corpus_file_is_well_formed():
    """Every line is parseable, every row has the expected keys, every
    label is either null or a non-empty string, every category is one
    of the two allowed values."""
    rows = _load_corpus()
    for i, r in enumerate(rows):
        assert set(r.keys()) >= {"utterance", "label", "category"}, (
            f"row {i} missing keys: {r}"
        )
        assert r["category"] in ("misconception", "correct"), (
            f"row {i} has bad category: {r['category']}"
        )
        if r["category"] == "misconception":
            assert isinstance(r["label"], str) and r["label"].strip(), (
                f"row {i} misconception case must have a non-empty label"
            )
        else:
            assert r["label"] is None, (
                f"row {i} correct case must have null label; got {r['label']!r}"
            )


def test_thresholds_match_module_constants():
    """Document-as-test: the targets in this corpus assume the current
    TAU_PROBE / TAU_FIRE values. If those constants change, recalibrate
    the corpus and update this test's thresholds."""
    assert TAU_PROBE == 0.5, "if TAU_PROBE changed, recalibrate this corpus"
    assert TAU_FIRE == 0.75, "if TAU_FIRE changed, recalibrate this corpus"
