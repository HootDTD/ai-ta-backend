"""P2.3 — apollo/overseer/misconception.py.

Covers the inference + verifier pipeline. All LLM/embedding calls are
stubbed via the module's DI hooks (generator, verifier, embedder,
retriever). No network, no DB.

Subject-agnostic contract: same signature-introspection check as the
bank loader. The function must take `concept_id: int` and never see a
subject or concept slug parameter.
"""
from __future__ import annotations

import inspect
import typing

import pytest

from apollo.ontology import build_node
from apollo.overseer.misconception import (
    TAU_FIRE,
    TAU_PROBE,
    MisconceptionSignal,
    infer_misconception,
    is_enabled,
)
from apollo.overseer.misconception_bank import MisconceptionEntry
from apollo.solver.sufficiency import SufficiencyVerdict


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _node():
    """Smallest possible typed Node for stubbed pipelines."""
    return build_node(
        node_type="equation",
        node_id="n1",
        attempt_id=1,
        source="parser",
        content={"symbolic": "x*y", "label": "test"},
    )


def _entry(*, code: str = "no_density",
           description: str = "treats fluid as having no density",
           probe: str = "Hmm, is density part of this?",
           rt_steps: tuple[str, ...] = ("ask about density",
                                        "compare with vs without")) -> MisconceptionEntry:
    return MisconceptionEntry(
        id=42,
        concept_id=7,
        code=code,
        description=description,
        confusion_pair=("mass_flow", "volumetric_flow"),
        trigger_phrases=("ignore density",),
        probe_question=probe,
        rt_steps=rt_steps,
    )


def _verdict(state: str = "insufficient") -> SufficiencyVerdict:
    return SufficiencyVerdict(state=state)  # type: ignore[arg-type]


def _retriever_returning(*pairs):
    """Build a stub RetrieverFn that returns the given (entry, similarity) list."""
    async def _stub(*, concept_id, query_embedding, k):
        return list(pairs)
    return _stub


def _generator_returning(description: str | None, evidence: str = "ev"):
    def _stub(*, utterance, parsed_nodes, next_premise_hint):
        return description, evidence
    return _stub


def _verifier_returning(score: float, reason: str = "ok"):
    def _stub(*, utterance, parsed_nodes, candidate_description):
        return score, reason
    return _stub


def _embedder_returning(vec: list[float] | None = None):
    def _stub(text: str):
        return vec if vec is not None else [0.0] * 8
    return _stub


# ---------------------------------------------------------------------------
# Subject-agnosticism contract — must hold no matter what
# ---------------------------------------------------------------------------

def test_infer_misconception_signature_is_subject_agnostic():
    """No parameter may be a subject- or concept-slug string. The DB FK
    `concept_id: int` is the only concept-level coupling allowed."""
    sig = inspect.signature(infer_misconception)
    forbidden = {"subject_id", "subject_slug", "concept_slug", "cluster_id"}
    assert not (forbidden & set(sig.parameters)), (
        "infer_misconception must not accept subject/concept slug params; "
        f"found {forbidden & set(sig.parameters)}"
    )
    assert "concept_id" in sig.parameters
    hints = typing.get_type_hints(infer_misconception)
    assert hints.get("concept_id") is int


# ---------------------------------------------------------------------------
# Stage 1: skip on sufficient
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_skips_when_kg_is_sufficient():
    """Sufficient KG = no productive misconception to probe — return default
    immediately and do not call the LLM."""
    calls: list[str] = []

    def gen(*, utterance, parsed_nodes, next_premise_hint):
        calls.append("generator")
        return "should not be called", "x"

    sig = await infer_misconception(
        db=None,
        concept_id=7,
        utterance="anything",
        parsed_nodes=[_node()],
        sufficiency=_verdict("sufficient"),
        generator=gen,
        verifier=_verifier_returning(0.99),
        embedder=_embedder_returning(),
        retriever=_retriever_returning((_entry(), 0.99)),
    )
    assert sig.fired is False
    assert sig.state == "default"
    assert "skip:sufficient" in sig.evidence
    assert calls == [], "generator must not run when KG is sufficient"


# ---------------------------------------------------------------------------
# Stage 2: generator says "no misconception"
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_default_when_generator_returns_none():
    sig = await infer_misconception(
        db=None,
        concept_id=7,
        utterance="A1 v1 = A2 v2 with density factored out",
        parsed_nodes=[_node()],
        sufficiency=_verdict("insufficient"),
        generator=_generator_returning(None),
        verifier=_verifier_returning(0.99),
        embedder=_embedder_returning(),
        retriever=_retriever_returning((_entry(), 0.99)),
    )
    assert sig.fired is False
    assert sig.state == "default"
    assert sig.description is None


# ---------------------------------------------------------------------------
# Stage 3: bank empty → still default, but description is recorded for analytics
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_default_when_bank_returns_no_match():
    sig = await infer_misconception(
        db=None,
        concept_id=7,
        utterance="utterance",
        parsed_nodes=[_node()],
        sufficiency=_verdict("insufficient"),
        generator=_generator_returning("a generated suspected error"),
        verifier=_verifier_returning(0.99),
        embedder=_embedder_returning(),
        retriever=_retriever_returning(),  # empty
    )
    assert sig.fired is False
    assert sig.state == "default"
    assert sig.description == "a generated suspected error"


# ---------------------------------------------------------------------------
# Stage 5 thresholds: probe band, fire band, below-probe band
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_below_tau_probe_does_not_fire():
    # combined = sqrt(sim * verifier_score)
    # pick sim and v so combined < TAU_PROBE
    sig = await infer_misconception(
        db=None,
        concept_id=7,
        utterance="utterance",
        parsed_nodes=[_node()],
        sufficiency=_verdict("insufficient"),
        generator=_generator_returning("desc"),
        verifier=_verifier_returning(0.10),  # low
        embedder=_embedder_returning(),
        retriever=_retriever_returning((_entry(), 0.20)),
    )
    assert sig.fired is False
    assert sig.confidence < TAU_PROBE
    assert sig.bank_code == "no_density"  # carried for analytics


@pytest.mark.asyncio
async def test_probe_band_fires_in_probe_state():
    # combined in [TAU_PROBE, TAU_FIRE)
    # sim=0.6, v=0.6 -> combined = 0.6, between 0.5 and 0.75
    sig = await infer_misconception(
        db=None,
        concept_id=7,
        utterance="utterance",
        parsed_nodes=[_node()],
        sufficiency=_verdict("insufficient"),
        generator=_generator_returning("desc"),
        verifier=_verifier_returning(0.6),
        embedder=_embedder_returning(),
        retriever=_retriever_returning((_entry(), 0.6)),
    )
    assert sig.fired is True
    assert sig.state == "probe"
    assert TAU_PROBE <= sig.confidence < TAU_FIRE
    # Authored payload, not generator description, drives the persona shift
    assert sig.probe == "Hmm, is density part of this?"
    assert sig.rt_steps == ("ask about density", "compare with vs without")


@pytest.mark.asyncio
async def test_fire_band_without_prior_corroboration_demotes_to_probe():
    """PROBE-then-confirm: first detection of a code is never socratic."""
    sig = await infer_misconception(
        db=None,
        concept_id=7,
        utterance="utterance",
        parsed_nodes=[_node()],
        sufficiency=_verdict("insufficient"),
        previous_signals=(),
        generator=_generator_returning("desc"),
        verifier=_verifier_returning(0.95),
        embedder=_embedder_returning(),
        retriever=_retriever_returning((_entry(code="no_density"), 0.95)),
    )
    assert sig.fired is True
    assert sig.state == "probe", (
        "first detection of a bank_code must demote socratic→probe per "
        "PROBE-then-confirm"
    )
    assert sig.confidence >= TAU_FIRE


@pytest.mark.asyncio
async def test_fire_band_with_prior_corroboration_escalates_to_socratic():
    prior = MisconceptionSignal(
        fired=True,
        state="probe",
        bank_code="no_density",
        confidence=0.6,
    )
    sig = await infer_misconception(
        db=None,
        concept_id=7,
        utterance="utterance",
        parsed_nodes=[_node()],
        sufficiency=_verdict("insufficient"),
        previous_signals=(prior,),
        generator=_generator_returning("desc"),
        verifier=_verifier_returning(0.95),
        embedder=_embedder_returning(),
        retriever=_retriever_returning((_entry(code="no_density"), 0.95)),
    )
    assert sig.fired is True
    assert sig.state == "socratic"


@pytest.mark.asyncio
async def test_corroboration_must_match_bank_code():
    """Prior detection of *another* misconception must not be enough to
    escalate this one to socratic — that would let two unrelated probes
    chain into a strong intervention."""
    prior = MisconceptionSignal(
        fired=True,
        state="probe",
        bank_code="some_other_misconception",
        confidence=0.6,
    )
    sig = await infer_misconception(
        db=None,
        concept_id=7,
        utterance="utterance",
        parsed_nodes=[_node()],
        sufficiency=_verdict("insufficient"),
        previous_signals=(prior,),
        generator=_generator_returning("desc"),
        verifier=_verifier_returning(0.95),
        embedder=_embedder_returning(),
        retriever=_retriever_returning((_entry(code="no_density"), 0.95)),
    )
    assert sig.state == "probe", (
        "corroboration must be code-specific — unrelated prior probe does "
        "not escalate"
    )


# ---------------------------------------------------------------------------
# Internal-only safety: candidate description is not the only signal
# returned; authored bank entry payload drives the persona shift, not
# the generator output.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_authored_payload_used_not_generator_description():
    """description on the signal carries the BANK description (used for
    leak-check + analytics), not the generator's free-text candidate."""
    bank_desc = "treats fluid as having no density"
    sig = await infer_misconception(
        db=None,
        concept_id=7,
        utterance="u",
        parsed_nodes=[_node()],
        sufficiency=_verdict("insufficient"),
        generator=_generator_returning("some-other-text-from-generator"),
        verifier=_verifier_returning(0.6),
        embedder=_embedder_returning(),
        retriever=_retriever_returning((_entry(description=bank_desc), 0.6)),
    )
    assert sig.fired is True
    assert sig.description == bank_desc


# ---------------------------------------------------------------------------
# Soft-fail: verifier exception → score 0 → default state. We simulate
# this by injecting a verifier that returns a 0 score, which is exactly
# what _default_verifier does on parse error.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_zero_score_verifier_does_not_fire():
    sig = await infer_misconception(
        db=None,
        concept_id=7,
        utterance="u",
        parsed_nodes=[_node()],
        sufficiency=_verdict("insufficient"),
        generator=_generator_returning("desc"),
        verifier=_verifier_returning(0.0, reason="parse-error-soft-fail"),
        embedder=_embedder_returning(),
        retriever=_retriever_returning((_entry(), 0.95)),
    )
    assert sig.fired is False


# ---------------------------------------------------------------------------
# is_enabled flag default
# ---------------------------------------------------------------------------

def test_is_enabled_defaults_off(monkeypatch):
    monkeypatch.delenv("APOLLO_MISCONCEPTION_ENABLED", raising=False)
    assert is_enabled() is False


def test_is_enabled_recognizes_truthy_values(monkeypatch):
    for val in ("1", "true", "True", "YES", "on"):
        monkeypatch.setenv("APOLLO_MISCONCEPTION_ENABLED", val)
        assert is_enabled() is True, f"failed on {val!r}"
