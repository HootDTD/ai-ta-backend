"""P2.10 — End-to-end misconception flow smoke test.

The legacy `test_e2e_smoke.py` is module-skipped pending a V3 rewrite.
Rather than wait, this smoke verifies the misconception chain works
end-to-end across modules with stubbed LLM/embedding boundaries:

    inference → persona shift suffix → output filter →
    Message.metadata persistence → summarize_for_rubric →
    rubric axis → diagnostic narration line

If any link breaks, this test fails with a clear pointer to which
module regressed. Real LLMs and Neo4j are NOT exercised — those are
covered by their unit tests upstream.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from apollo.agent.apollo_llm import APOLLO_SYSTEM_PROMPT, draft_reply
from apollo.agent.leakage_judge import JudgeVerdict
from apollo.agent.output_filter import validate_or_raise
from apollo.handlers.chat import _metadata_to_signal, _signal_to_metadata
from apollo.ontology import build_node
from apollo.overseer.diagnostic import _append_misconception_line
from apollo.overseer.misconception import (
    MisconceptionSignal,
    infer_misconception,
    summarize_for_rubric,
)
from apollo.overseer.misconception_bank import MisconceptionEntry
from apollo.overseer.rubric import compute_rubric
from apollo.solver.sufficiency import SufficiencyVerdict
from apollo.subjects import load_concept


@pytest.fixture
def concept():
    return load_concept("fluid_mechanics", "bernoulli_principle")


def _bank_entry():
    return MisconceptionEntry(
        id=42,
        concept_id=7,
        code="no_density",
        description="treats fluid as having no density when computing flow",
        confusion_pair=("mass_flow", "volumetric_flow"),
        trigger_phrases=("ignore density",),
        probe_question="Hmm, would the answer change if density were different?",
        rt_steps=(
            "ask whether density matters here",
            "compare flow with vs without density",
        ),
    )


def _node():
    return build_node(
        node_type="equation",
        node_id="n1",
        attempt_id=1,
        source="parser",
        content={"symbolic": "A1*v1 - A2*v2", "label": "continuity"},
    )


@pytest.mark.asyncio
async def test_chain_inference_to_persona_to_filter_to_rubric_to_narration(
    monkeypatch, concept,
):
    """One pass through every join in the chain. Every assertion below
    checks an interface — if a module changes shape, exactly one
    assertion below pinpoints which join broke."""
    monkeypatch.setenv("APOLLO_MISCONCEPTION_ENABLED", "1")

    bank = _bank_entry()

    # Stage 1: inference. With high score and prior corroboration, fires socratic.
    async def stub_retriever(*, concept_id, query_embedding, k):
        return [(bank, 0.95)]

    prior = MisconceptionSignal(
        fired=True, state="probe", bank_code="no_density", confidence=0.6,
    )

    signal = await infer_misconception(
        db=None,
        concept_id=7,
        utterance="when speed changes, the flow rate stays the same right?",
        parsed_nodes=[_node()],
        sufficiency=SufficiencyVerdict(state="insufficient"),
        previous_signals=(prior,),
        generator=lambda *, utterance, parsed_nodes, next_premise_hint: (
            "the student is ignoring the role of density", "ev"
        ),
        verifier=lambda *, utterance, parsed_nodes, candidate_description: (0.95, "ok"),
        embedder=lambda text: [0.0] * 8,
        retriever=stub_retriever,
    )
    assert signal.fired
    assert signal.state == "socratic"
    assert signal.bank_code == "no_density"
    # Internal-only fields are present on the signal but must not leak downstream.
    assert signal.description == bank.description

    # Stage 2: persona shift draft_reply uses authored payload, not description.
    captured: dict = {}

    def fake_create(**kwargs):
        captured["messages"] = kwargs["messages"]
        m = MagicMock()
        m.choices = [MagicMock()]
        m.choices[0].message.content = (
            "Hmm, I'm wondering — would the answer change if "
            "density were different? Should we explore that?"
        )
        return m

    with patch("apollo.agent.apollo_llm.OpenAI") as mc:
        mc.return_value.chat.completions.create.side_effect = fake_create
        draft = draft_reply(
            history=[{"role": "user", "content": "u"}],
            kg_summary="(empty)",
            misconception=signal,
        )

    system_prompt = captured["messages"][0]["content"]
    # Authored rt_steps appear; description and bank_id do NOT.
    assert "ask whether density matters here" in system_prompt
    assert bank.description not in system_prompt
    assert "42" not in system_prompt
    # Default APOLLO suffix prefix preserved.
    assert system_prompt.startswith(APOLLO_SYSTEM_PROMPT)

    # Stage 3: output filter passes the clean draft and would block a leak.
    clean_judge = lambda **_: JudgeVerdict(
        leaks=False, offending_phrase=None, reason=None, confidence=0.0,
    )
    history = [{"role": "user", "content": "for incompressible flow, A1 v1 = A2 v2"}]
    kg = "facts: A1 v1 = A2 v2"

    out = validate_or_raise(
        draft, concept=concept, history=history, kg_summary=kg,
        judge=clean_judge, misconception=signal,
    )
    assert out == draft

    # And a leak is blocked. Use the bank description verbatim so the
    # substring match in the filter triggers.
    leaky = (
        f"I think you're somehow {bank.description}, which seems wrong."
    )
    from apollo.errors import FilterRejectedError
    with pytest.raises(FilterRejectedError):
        validate_or_raise(
            leaky, concept=concept, history=history, kg_summary=kg,
            judge=clean_judge, misconception=signal,
        )

    # Stage 4: serialize for Message.metadata; round-trip preserves the
    # PROBE-then-confirm-relevant fields and strips the rest.
    meta = _signal_to_metadata(signal)
    assert meta == {
        "fired": True, "state": "socratic",
        "bank_code": "no_density", "confidence": signal.confidence,
    }
    rehydrated = _metadata_to_signal({"misconception": meta})
    assert rehydrated is not None
    assert rehydrated.bank_code == "no_density"
    assert rehydrated.description is None  # internal-only field stripped

    # Stage 5: across an attempt, summarize_for_rubric produces the per-code
    # score map. Two firings, last 2 turns clean → "resolved".
    attempt_signals = [
        MisconceptionSignal(fired=True, state="probe",
                            bank_code="no_density", confidence=0.55),
        signal,  # socratic firing
        MisconceptionSignal(fired=False, state="default"),
        MisconceptionSignal(fired=False, state="default"),
    ]
    score_map = summarize_for_rubric(attempt_signals)
    assert score_map == {"no_density": 1.0}

    # Stage 6: rubric integrates the axis at 5%.
    refs = [
        build_node(node_type="procedure_step", node_id="p1", attempt_id=1,
                   source="reference",
                   content={"action": "use continuity", "purpose": "find v2"}),
    ]
    cov = {
        "per_step": {"p1": "covered"},
        "procedure_scores": {"p1": 1.0},
        "confidences": {"p1": 1.0},
    }
    rub = compute_rubric(cov, refs, misconception_scores=score_map)
    assert rub["misconception_corrected"]["present"] is True
    assert rub["misconception_corrected"]["detected"] == 1
    assert rub["misconception_corrected"]["resolved"] == 1
    assert rub["misconception_corrected"]["score"] == 100

    # Stage 7: diagnostic narration line appended deterministically.
    out_narrative = _append_misconception_line("Solid teaching.", rub)
    assert "1 suspected misconception;" in out_narrative
    assert "you resolved 1 of them" in out_narrative
    # And the sensitive description / bank_code never appear in the
    # student-visible narrative.
    assert bank.description not in out_narrative
    assert "no_density" not in out_narrative
